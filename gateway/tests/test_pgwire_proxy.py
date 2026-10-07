"""The audited Postgres wire proxy (alkera_gateway.native.pgwire_proxy).

    gateway/.venv/bin/python -m pytest gateway/tests/test_pgwire_proxy.py -q

A FAKE UPSTREAM stands in for PostgreSQL: it speaks just enough of protocol 3.0
to answer a startup, a Query and the extended-protocol messages, and for every
message it receives it records how many rows the rollout's action log held AT
THAT MOMENT - which is how "the row is written before the upstream sees the
statement" is checked, not inferred. A raw socket stands in for the client, so
the bytes on both sides are the test's own. No docker, no real server: the real
PostgreSQL behind the real proxy is tasks/harness/redshift_e2e_check.py's.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import socket
import struct
import tempfile
import threading
from pathlib import Path

import pytest

from alkera_gateway import redshift_sql
from alkera_gateway.native import pgwire_proxy as pw
from alkera_gateway.native.audit import ROW_KEYS
from alkera_gateway.native.serve import DestinationLog

TOOL = "redshift.statement"


# --------------------------------------------------------------------------- wire
def _msg(kind: bytes, body: bytes) -> bytes:
    return kind + struct.pack("!I", len(body) + 4) + body


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("closed")
        data += chunk
    return data


def _read_msg(sock: socket.socket) -> tuple[bytes, bytes]:
    head = _recv_exact(sock, 5)
    (length,) = struct.unpack("!I", head[1:])
    return head[:1], _recv_exact(sock, length - 4)


def _until_ready(sock: socket.socket) -> list[tuple[bytes, bytes]]:
    out = []
    while True:
        kind, body = _read_msg(sock)
        out.append((kind, body))
        if kind == b"Z":
            return out


def _startup(params: dict[str, str]) -> bytes:
    body = struct.pack("!I", pw.PROTOCOL_3) + b"".join(
        k.encode() + b"\0" + v.encode() + b"\0" for k, v in params.items()) + b"\0"
    return struct.pack("!I", len(body) + 4) + body


# --------------------------------------------------------------------------- fake upstream
class FakeUpstream:
    """Records (kind, body, rows in the audit log on arrival) per message.

    Listens on a unix socket (`path`), as the persona's server does, or on TCP
    (`port`). Like PostgreSQL it keeps each connection's named statements: a
    Parse of a name that is taken is an error (42P05), and after an error in
    the extended protocol everything up to the next Sync is skipped."""

    def __init__(self, audit: Path, path: Path | None = None):
        self.audit = audit
        self.received: list[tuple[bytes, bytes, int]] = []
        self.startups: list[dict[str, str]] = []
        if path is not None:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.bind(str(path))
            self.address: str | tuple[str, int] = str(path)
        else:
            self.sock = socket.socket()
            self.sock.bind(("127.0.0.1", 0))
            self.address = ("127.0.0.1", self.sock.getsockname()[1])
        self.sock.listen()
        threading.Thread(target=self._accept, daemon=True).start()

    def rows(self) -> int:
        return len(self.audit.read_text().splitlines()) if self.audit.exists() else 0

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket):
        try:
            (length,) = struct.unpack("!I", _recv_exact(conn, 4))
            body = _recv_exact(conn, length - 4)
            self.startups.append(pw.parse_startup(body))
            conn.sendall(_msg(b"R", struct.pack("!I", 0)) + _msg(b"Z", b"I"))
            skipping = False
            named: set[str] = set()
            while True:
                kind, body = _read_msg(conn)
                self.received.append((kind, body, self.rows()))
                refused = pw.SENTINEL.encode() in body
                if kind == b"X":
                    return
                if kind == b"Q":
                    if refused:
                        name = body[body.index(b"alkera"):body.index(b"(")].decode()
                        conn.sendall(pw.error_response(
                            "42883", f"function {name}() does not exist") + _msg(b"Z", b"I"))
                    else:
                        conn.sendall(_msg(b"C", b"SELECT 0\0") + _msg(b"Z", b"I"))
                elif kind == b"S":
                    skipping = False
                    conn.sendall(_msg(b"Z", b"I"))
                elif skipping:
                    continue
                elif kind == b"P":
                    statement = body[:body.index(b"\0")].decode()
                    if refused:
                        name = body[body.index(b"alkera"):body.index(b"(")].decode()
                        conn.sendall(pw.error_response(
                            "42883", f"function {name}() does not exist"))
                        skipping = True
                    elif statement and statement in named:
                        conn.sendall(pw.error_response(
                            "42P05", f'prepared statement "{statement}" already exists'))
                        skipping = True
                    else:
                        if statement:
                            named.add(statement)
                        conn.sendall(_msg(b"1", b""))
                elif kind == b"C":
                    named.discard(body[1:body.index(b"\0", 1)].decode())
                    conn.sendall(_msg(b"3", b""))
                elif kind == b"B":
                    conn.sendall(_msg(b"2", b""))
                elif kind == b"D":
                    conn.sendall(_msg(b"n", b""))
                elif kind == b"E":
                    conn.sendall(_msg(b"C", b"SELECT 0\0"))
        except (ConnectionError, OSError):
            return
        finally:
            conn.close()


# --------------------------------------------------------------------------- proxy
@pytest.fixture(params=["unix", "tcp"])
def stack(request, tmp_path, monkeypatch):
    audit = tmp_path / "audit" / "actions.jsonl"
    monkeypatch.setenv("ALKERA_NATIVE_AUDIT", str(audit))
    monkeypatch.setenv("ALKERA_VIRTUAL_TIME", "2026-03-02T00:00:00Z")
    # a short directory: a unix socket's path is limited to ~107 bytes
    sockets = Path(tempfile.mkdtemp(prefix="pgw"))
    upstream = FakeUpstream(audit, sockets / ".s.PGSQL.5432"
                            if request.param == "unix" else None)
    destinations = DestinationLog(tmp_path / "audit" / "destinations.redshift.jsonl")
    destinations.declare("redshift.internal", 0)
    statements = tmp_path / "data" / "statements.jsonl"
    started = threading.Event()
    box: dict = {}

    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        box["loop"] = loop
        proxy = pw.ProxyServer(upstream.address, translate=redshift_sql.translate,
                               tool=TOOL, actor="redshift", destinations=destinations,
                               statement_log=statements, refuse=redshift_sql.Unsupported,
                               startup_filter=redshift_sql.startup_parameters)
        server = loop.run_until_complete(proxy.start("127.0.0.1", 0))
        box["port"] = server.sockets[0].getsockname()[1]
        # the declared row is keyed by the port the proxy listens on
        destinations.declare("redshift.internal", box["port"])
        started.set()
        loop.run_forever()
        server.close()
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert started.wait(10)
    yield {"port": box["port"], "upstream": upstream, "audit": audit,
           "destinations": destinations, "statements": statements}
    box["loop"].call_soon_threadsafe(box["loop"].stop)
    thread.join(10)
    upstream.sock.close()
    shutil.rmtree(sockets, ignore_errors=True)


def _connect(port: int, *, ssl_first: bool = False, params=None) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    if ssl_first:
        sock.sendall(struct.pack("!II", 8, pw.SSL_REQUEST))
        assert _recv_exact(sock, 1) == b"N"
    sock.sendall(_startup(params or {"user": "alkera", "database": "dev"}))
    _until_ready(sock)
    return sock


def _parse(name: bytes, sql: bytes) -> bytes:
    return _msg(b"P", name + b"\0" + sql + b"\0" + struct.pack("!H", 0))


def _bind(statement: bytes, portal: bytes = b"") -> bytes:
    return _msg(b"B", portal + b"\0" + statement + b"\0" + struct.pack("!HHH", 0, 0, 0))


def _execute(portal: bytes = b"", max_rows: int = 0) -> bytes:
    return _msg(b"E", portal + b"\0" + struct.pack("!I", max_rows))


SYNC = _msg(b"S", b"")


def _rows(audit: Path) -> list[dict]:
    return [json.loads(line) for line in audit.read_text().splitlines()] \
        if audit.exists() else []


def _sent(upstream: FakeUpstream, kind: bytes) -> list[tuple[bytes, int]]:
    return [(body, rows) for k, body, rows in upstream.received if k == kind]


# --------------------------------------------------------------------------- tests
def test_proxy_audits_before_forwarding(stack):
    sock = _connect(stack["port"])
    sock.sendall(_msg(b"Q", b"SELECT TOP 3 order_id FROM warehouse.fct_orders\0"))
    _until_ready(sock)
    sock.sendall(_msg(b"Q", b"SELECT 1\0"))
    _until_ready(sock)
    sock.close()
    queries = _sent(stack["upstream"], b"Q")
    assert len(queries) == 2
    rows = _rows(stack["audit"])
    assert [r["args"]["sql"] for r in rows] == [
        "SELECT TOP 3 order_id FROM warehouse.fct_orders", "SELECT 1"]
    # the upstream saw statement k only once row k (seq k) was in the log
    for k, (_, rows_on_arrival) in enumerate(queries, start=1):
        assert rows_on_arrival == k
        assert rows[k - 1]["seq"] == k
    assert all(set(r) == set(ROW_KEYS) and r["tool"] == TOOL for r in rows)
    assert all("actor:redshift" in r["flags"] for r in rows)
    # what went upstream is the translation; the row keeps what the client sent
    assert queries[0][0] == b"SELECT order_id FROM warehouse.fct_orders LIMIT 3\0"
    assert rows[0]["result"] == {"forwarded": True,
                                 "translated": "SELECT order_id FROM warehouse.fct_orders "
                                               "LIMIT 3"}
    assert rows[0]["op_class"] == "read" and rows[0]["vts"] == "2026-03-02T00:00:00Z"
    assert rows[1]["result"] == {"forwarded": True, "translated": None}


def test_extended_protocol_parse_is_translated(stack):
    sock = _connect(stack["port"])
    parse = b"s1\0SELECT NVL(order_amount, 0) FROM warehouse.fct_orders\0" + \
        struct.pack("!H", 0)
    bind = b"\0s1\0" + struct.pack("!HHH", 0, 0, 0)
    execute = b"\0" + struct.pack("!I", 0)
    sock.sendall(_msg(b"P", parse) + _msg(b"H", b"") + _msg(b"D", b"Ss1\0") +
                 _msg(b"S", b""))
    _until_ready(sock)
    # a driver's cached prepared statement, executed twice: two rows, no re-Parse
    for _ in range(2):
        sock.sendall(_msg(b"B", bind) + _msg(b"E", execute) + _msg(b"S", b""))
        _until_ready(sock)
    sock.close()
    (parsed, rows_at_parse), = _sent(stack["upstream"], b"P")
    assert b"COALESCE(order_amount, 0)" in parsed and b"NVL" not in parsed
    assert parsed.startswith(b"s1\0") and parsed.endswith(struct.pack("!H", 0))
    assert rows_at_parse == 1                  # the row is in before the server parses
    executes = _sent(stack["upstream"], b"E")
    # the Parse row stands for the first run; the cached re-run writes its own
    assert [rows for _, rows in executes] == [1, 2]
    rows = _rows(stack["audit"])
    assert [r["args"]["sql"] for r in rows] == \
        ["SELECT NVL(order_amount, 0) FROM warehouse.fct_orders"] * 2
    logged = [json.loads(line) for line in stack["statements"].read_text().splitlines()]
    assert [entry["via"] for entry in logged] == ["parse", "execute"]


def test_a_statement_the_server_rejects_at_parse_is_still_a_row(stack):
    sock = _connect(stack["port"])
    sock.sendall(_msg(b"P", b"\0SELECT * FROM svv_external_schemas\0" + struct.pack("!H", 0)) +
                 _msg(b"S", b""))
    _until_ready(sock)
    sock.close()
    rows = _rows(stack["audit"])
    assert [r["args"]["sql"] for r in rows] == ["SELECT * FROM svv_external_schemas"]
    assert not _sent(stack["upstream"], b"E")


def test_refusal_is_a_named_0a000_error_and_an_error_row(stack):
    sock = _connect(stack["port"])
    sock.sendall(_msg(b"Q", b"UNLOAD ('select 1') TO 's3://bucket/x' IAM_ROLE 'arn'\0"))
    replies = _until_ready(sock)
    errors = [pw.error_fields(body) for kind, body in replies if kind == b"E"]
    assert len(errors) == 1
    fields = dict(errors[0])
    assert fields[b"C"] == "0A000"
    assert fields[b"M"] == "Redshift feature not available in this environment: UNLOAD"
    # extended protocol: the error arrives where the server sends it, then Sync
    sock.sendall(_msg(b"P", b"\0CREATE TABLE t (s SUPER)\0" + struct.pack("!H", 0)) +
                 _msg(b"B", b"\0\0" + struct.pack("!HHH", 0, 0, 0)) +
                 _msg(b"E", b"\0" + struct.pack("!I", 0)) + _msg(b"S", b""))
    replies = _until_ready(sock)
    errors = [dict(pw.error_fields(body)) for kind, body in replies if kind == b"E"]
    assert [e[b"M"] for e in errors] == [
        "Redshift feature not available in this environment: SUPER"]
    sock.close()
    rows = _rows(stack["audit"])
    assert [(r["exit"], r["error"]["code"]) for r in rows] == [("error", "0A000")] * 2
    assert rows[0]["result"] == {"forwarded": False, "translated": None}
    # the refused statement itself never reached the server
    assert not any(b"UNLOAD" in body or b"SUPER" in body
                   for _, body, _ in stack["upstream"].received)


def test_ssl_request_is_answered_no_and_driver_parameters_are_dropped(stack):
    sock = _connect(stack["port"], ssl_first=True, params={
        "user": "alkera", "database": "dev", "application_name": "probe",
        "client_protocol_version": "2", "driver_version": "Redshift Python Driver 2.1.17",
        "os_version": "Linux", "driver_discovery_version": "1"})
    sock.close()
    assert stack["upstream"].startups[-1] == {"user": "alkera", "database": "dev",
                                              "application_name": "probe"}


def test_every_accepted_connection_is_a_destination(stack):
    for _ in range(3):
        _connect(stack["port"]).close()
    rows = json.loads(Path(stack["destinations"].path).read_text().splitlines()[-1])
    assert rows["host"] == "redshift.internal" and rows["count"] == 3


def test_statement_log_is_one_to_one_with_the_rows(stack):
    sock = _connect(stack["port"])
    for sql in (b"SELECT 1", b"COPY t FROM 's3://b/k' IAM_ROLE 'arn'", b"SELECT GETDATE()"):
        sock.sendall(_msg(b"Q", sql + b"\0"))
        _until_ready(sock)
    sock.close()
    logged = [json.loads(line) for line in stack["statements"].read_text().splitlines()]
    assert [entry["sql"] for entry in logged] == \
        [r["args"]["sql"] for r in _rows(stack["audit"])]
    assert [entry["forwarded"] for entry in logged] == [True, False, True]


def test_error_rewrite_leaves_the_servers_own_errors_alone():
    session = pw._Session.__new__(pw._Session)
    session.refusals = {}
    body = pw.error_response("42P01", 'relation "nope" does not exist')[5:]
    assert session._rewrite_error(body) is None


# --------------------------------------------------------------------------- default deny
def test_function_call_is_audited_and_refused(stack):
    sock = _connect(stack["port"])
    # FunctionCall: function oid, 0 argument formats, 1 argument, result text
    call = struct.pack("!IHHI", 2617, 0, 1, 4) + b"test" + struct.pack("!H", 0)
    sock.sendall(_msg(b"F", call))
    replies = _until_ready(sock)                     # E + Z: what a fastpath client awaits
    errors = [dict(pw.error_fields(body)) for kind, body in replies if kind == b"E"]
    assert [e[b"C"] for e in errors] == ["0A000"]
    assert errors[0][b"M"] == ("Redshift feature not available in this environment: "
                               "FunctionCall (the fastpath protocol)")
    sock.sendall(_msg(b"Q", b"SELECT 1\0"))          # and the session goes on
    _until_ready(sock)
    sock.close()
    assert not _sent(stack["upstream"], b"F")         # never reached the server
    rows = _rows(stack["audit"])
    assert rows[0]["args"]["sql"] == ("/* FunctionCall (the fastpath protocol), "
                                      "function oid 2617 */")
    assert (rows[0]["exit"], rows[0]["error"]["code"]) == ("error", "0A000")
    assert "protocol_message" in rows[0]["flags"]
    assert [r["args"]["sql"] for r in rows[1:]] == ["SELECT 1"]


def test_any_unlisted_frontend_message_is_audited_and_refused(stack):
    sock = _connect(stack["port"])
    for kind in (b"d", b"c", b"f", b"j"):            # CopyData/Done/Fail, and nothing at all
        sock.sendall(_msg(kind, b"payload\0"))
        replies = _until_ready(sock)
        assert [k for k, _ in replies] == [b"E", b"Z"], kind
    sock.close()
    assert not any(k in (b"d", b"c", b"f", b"j") for k, _, _ in stack["upstream"].received)
    rows = _rows(stack["audit"])
    assert len(rows) == 4 and all(r["exit"] == "error" for r in rows)
    assert "frontend message type b'j'" in rows[3]["error"]["message"]
    assert set(pw.RELAYED) == {b"D", b"C", b"H", b"S", b"X", b"p"}


# --------------------------------------------------------------------------- statement map
def test_a_rejected_reparse_keeps_the_statement_the_server_kept(stack):
    """Reviewer's repro: s1 is a DELETE; a second Parse of s1 is refused by the
    server (the name is taken); the Bind/Executes that follow run the DELETE,
    and their rows must say so."""
    sock = _connect(stack["port"])
    delete = b"DELETE FROM warehouse.fct_orders WHERE order_id = 1"
    sock.sendall(_parse(b"s1", delete) + _bind(b"s1") + _execute() + SYNC)
    _until_ready(sock)
    sock.sendall(_parse(b"s1", b"SELECT 1") + SYNC)
    replies = _until_ready(sock)
    assert [dict(pw.error_fields(b))[b"C"] for k, b in replies if k == b"E"] == ["42P05"]
    for _ in range(2):
        sock.sendall(_bind(b"s1") + _execute() + SYNC)
        _until_ready(sock)
    sock.close()
    rows = [r["args"]["sql"] for r in _rows(stack["audit"])]
    assert rows == [delete.decode(), "SELECT 1", delete.decode(), delete.decode()]


def test_a_pipelined_batch_binds_the_parse_before_it(stack):
    """Parse, Bind, Execute, Sync in one write (psycopg 3 and asyncpg do this):
    the Execute is the parsed statement's first run, which its Parse row
    stands for; the next batch's Parse of the unnamed statement replaces it."""
    sock = _connect(stack["port"])
    sock.sendall(_parse(b"", b"SELECT 1") + _bind(b"") + _execute() + SYNC +
                 _parse(b"", b"SELECT 2") + _bind(b"") + _execute() + _bind(b"") +
                 _execute() + SYNC)
    _until_ready(sock)
    _until_ready(sock)
    sock.close()
    assert [r["args"]["sql"] for r in _rows(stack["audit"])] == \
        ["SELECT 1", "SELECT 2", "SELECT 2"]


def test_a_portal_fetched_in_parts_is_one_run(stack):
    sock = _connect(stack["port"])
    sock.sendall(_parse(b"s1", b"SELECT order_id FROM warehouse.fct_orders") + SYNC)
    _until_ready(sock)
    sock.sendall(_bind(b"s1") + _execute(max_rows=100) + _execute(max_rows=100) +
                 _execute(max_rows=100) + SYNC)       # first run: the Parse row's
    _until_ready(sock)
    sock.sendall(_bind(b"s1") + _execute(max_rows=100) + _execute(max_rows=100) + SYNC)
    _until_ready(sock)                                # second run: one more row
    sock.close()
    assert len(_rows(stack["audit"])) == 2
    assert len(_sent(stack["upstream"], b"E")) == 5


# --------------------------------------------------------------------------- startup
def test_startup_options_are_filtered_like_set(stack):
    _connect(stack["port"], params={
        "user": "alkera", "database": "dev",
        "options": "-c query_group=etl -c search_path=warehouse"}).close()
    assert stack["upstream"].startups[-1]["options"] == "-c search_path=warehouse"
    sock = socket.create_connection(("127.0.0.1", stack["port"]), timeout=10)
    sock.sendall(_startup({"user": "alkera", "database": "dev",
                           "options": "-c standard_conforming_strings=off"}))
    kind, body = _read_msg(sock)
    sock.close()
    fields = dict(pw.error_fields(body))
    assert kind == b"E" and fields[b"S"] == "FATAL" and fields[b"C"] == "0A000"
    assert "standard_conforming_strings" in fields[b"M"]
    assert len(stack["upstream"].startups) == 1      # never reached the server
    row = _rows(stack["audit"])[-1]
    assert row["exit"] == "error" and "protocol_startup" in row["flags"]
