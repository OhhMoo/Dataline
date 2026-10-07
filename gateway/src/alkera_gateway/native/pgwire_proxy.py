"""An audited PostgreSQL wire-protocol proxy: the front door of a Postgres persona.

    serve(("0.0.0.0", 5439), ("127.0.0.1", 5432),
          translate=redshift_sql.translate, tool="redshift.statement", actor="redshift")

The Redshift plug (gateway/docker/redshift_start.py) is a PostgreSQL server on
loopback with this proxy in front of it, on the port the agent's clients dial.
An HTTP proxy cannot see the Postgres wire protocol, and the Postgres log tailer
writes its rows only at teardown, so the audit lives here: every statement a
client runs is ONE 9.2.2 row (`tool`), written server-side BEFORE the statement
is forwarded - so the row is ordered before anything the agent does next.

WHAT THE PROXY DOES, message by message (protocol 3.0):

  SSLRequest / GSSENCRequest   answered `N`: the cluster does not require SSL
                               (the box's onboarding says so, and the clients
                               connect with ssl=False / sslmode=disable).
  CancelRequest                relayed to the upstream as is.
  StartupMessage               relayed with only the parameters PostgreSQL
                               knows: redshift_connector sends its own
                               (client_protocol_version, driver_version,
                               os_version, ...), which Postgres would reject as
                               unknown settings. Authentication then passes
                               through untouched (SCRAM or MD5, the upstream's).
  ParameterStatus (server)     `parameters` overrides the server's own values
                               (the persona's server_version).
  Query ('Q')                  translated; ONE row; forwarded.
  Parse ('P')                  translated; ONE row; forwarded. The statement is
                               remembered by name: its first Execute is the run
                               that row stands for. (A row at Parse, not at
                               Execute, because a statement the server rejects
                               at parse - an unknown relation - is never
                               executed, and it was still the agent's action.)
  Bind ('B'), Execute ('E')    the portal -> statement map; every LATER Execute
                               of a statement is ONE more row, so a prepared
                               statement a driver re-executes from its cache
                               (redshift_connector does) is a row each time.
  everything else              relayed.

A REFUSAL (the translator raises a `Refusal`) is written as an error row and
then sent to the upstream as a statement that fails at parse time - a call to a
function that does not exist, named for this refusal - whose ErrorResponse the
proxy rewrites on the way back into the refusal's own (SQLSTATE `sqlstate`, the
refusal's message). Routing it through the server keeps the protocol exactly
right for free: the error arrives where the server would have sent it, the
extended protocol skips to the next Sync, and an open transaction is aborted
just as an error aborts it on Redshift.

Accepted connections are counted into a `serve.DestinationLog` when one is
given, and every statement the proxy handles is appended to `statement_log`
(JSONL, the proxy's private record) right after its audit row - the 1:1
check the e2e makes between the two.
"""
from __future__ import annotations

import asyncio
import json
import re
import struct
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

SSL_REQUEST = 80877103
GSSENC_REQUEST = 80877104
CANCEL_REQUEST = 80877102
PROTOCOL_3 = 196608

#: Startup parameters relayed to PostgreSQL; the rest are a driver's own.
STARTUP_KEEP = frozenset({"user", "database", "application_name", "options",
                          "client_encoding", "datestyle", "timezone", "intervalstyle",
                          "extra_float_digits", "search_path"})
SENTINEL = "alkera_refused_"
_SENTINEL_RE = re.compile(SENTINEL + r"[0-9a-f]{16}")
CHUNK = 1 << 16


class Refusal(Exception):
    """A statement the persona refuses: an ErrorResponse with `sqlstate` and
    `str(self)` as its message, and an error row."""

    sqlstate = "0A000"


@dataclass
class Prepared:
    sql: str
    translated: str
    refused: bool = False
    #: True from Parse until the first Execute, which the Parse row stands for;
    #: every later Execute writes its own row.
    parse_row_unspent: bool = False


# --------------------------------------------------------------------------- wire helpers
def message(kind: bytes, body: bytes) -> bytes:
    return kind + struct.pack("!I", len(body) + 4) + body


def error_response(sqlstate: str, text: str, severity: str = "ERROR") -> bytes:
    fields = b"".join(code + value.encode("utf-8") + b"\0" for code, value in (
        (b"S", severity), (b"V", severity), (b"C", sqlstate), (b"M", text)))
    return message(b"E", fields + b"\0")


def _cstring(body: bytes, at: int) -> tuple[str, int]:
    end = body.index(b"\0", at)
    return body[at:end].decode("utf-8", "replace"), end + 1


def parse_startup(body: bytes) -> dict[str, str]:
    """`key\\0value\\0...\\0` after the protocol version."""
    params: dict[str, str] = {}
    at = 4
    while at < len(body) and body[at] != 0:
        key, at = _cstring(body, at)
        value, at = _cstring(body, at)
        params[key] = value
    return params


def startup_message(params: dict[str, str]) -> bytes:
    body = struct.pack("!I", PROTOCOL_3) + b"".join(
        k.encode("utf-8") + b"\0" + v.encode("utf-8") + b"\0" for k, v in params.items()) + b"\0"
    return struct.pack("!I", len(body) + 4) + body


def error_fields(body: bytes) -> list[tuple[bytes, str]]:
    out, at = [], 0
    while at < len(body) and body[at] != 0:
        code = body[at:at + 1]
        value, at = _cstring(body, at + 1)
        out.append((code, value))
    return out


def observation(sql: str, dialect: str):
    """op_class / targets for one statement: the gateway's own classifier, the
    Postgres tailer's verb fallback behind it (an unparsed statement is still
    one row, flagged, never dropped) - what snowflake_start.py does."""
    from ..records import Observation, ToolError
    from ..sql import inspect_sql
    from .tailers.postgres import fallback_observation

    found = Observation()
    try:
        inspect_sql(sql, found, read_only=False, dialect=dialect)
    except ToolError:
        found = fallback_observation(sql, dialect=dialect)
    except Exception:  # noqa: BLE001 - a classifier bug must not drop the row
        found = fallback_observation(sql, dialect=dialect)
        found.flags.append("classifier_error")
    return found


# --------------------------------------------------------------------------- the proxy
class ProxyServer:
    """One listening proxy; `handle` serves one client connection."""

    def __init__(self, upstream: tuple[str, int], *, translate: Callable[[str], str],
                 tool: str, actor: str, dialect: str = "redshift",
                 destinations=None, statement_log: Path | None = None,
                 record: Callable[..., dict] | None = None,
                 parameters: dict[str, str] | None = None):
        self.upstream = upstream
        self.translate = translate
        self.tool = tool
        self.actor = actor
        self.dialect = dialect
        # ParameterStatus values the persona reports instead of the server's
        # (Redshift's `server_version` is 8.0.2, and redshift_connector parses
        # it: Debian's "17.11 (Debian 17.11-0+deb13u1)" is an InvalidVersion).
        self.parameters = dict(parameters or {})
        self.destinations = destinations
        self.statement_log = Path(statement_log) if statement_log else None
        self._record = record
        # One audit row and its statement-log line at a time, across connections,
        # so the two records list statements in the same order.
        self._audit_lock = asyncio.Lock()
        self._file_lock = threading.Lock()

    def record_fn(self):
        if self._record is None:
            from .audit import record

            self._record = record
        return self._record

    async def start(self, host: str, port: int) -> asyncio.base_events.Server:
        return await asyncio.start_server(self.handle, host, port)

    # ---------------------------------------------------------------- the row
    def _write_row(self, sql: str, *, translated: str | None, refused: Refusal | None,
                   via: str, latency_ms: float) -> None:
        seen = observation(sql, self.dialect) if sql.strip() else None
        result = {"forwarded": refused is None,
                  "translated": translated if translated not in (None, sql) else None}
        flags = list(seen.flags) if seen is not None else []
        if result["translated"] is not None:
            flags.append("translated")
        self.record_fn()(
            tool=self.tool, args={"sql": sql},
            op_class=seen.op_class if seen is not None else "exec",
            targets=list(seen.targets) if seen is not None else [],
            read_targets=list(seen.read_targets) if seen is not None else [],
            exit="error" if refused is not None else "ok",
            error={"code": refused.sqlstate, "message": str(refused)} if refused else None,
            latency_ms=latency_ms, result=result, flags=flags, actor=self.actor)
        if self.statement_log is not None:
            line = json.dumps({"sql": sql, "via": via, "forwarded": refused is None},
                              sort_keys=True)
            with self._file_lock:
                self.statement_log.parent.mkdir(parents=True, exist_ok=True)
                with open(self.statement_log, "a", encoding="utf-8") as stream:
                    stream.write(line + "\n")

    async def audit(self, sql: str, *, translated: str | None, refused: Refusal | None,
                    via: str, started: float) -> None:
        latency = (time.monotonic() - started) * 1000
        async with self._audit_lock:
            await asyncio.to_thread(self._write_row, sql, translated=translated,
                                    refused=refused, via=via, latency_ms=latency)

    def _translate(self, sql: str) -> tuple[str, Refusal | None]:
        try:
            return self.translate(sql), None
        except Refusal as exc:
            return sql, exc
        except Exception as exc:  # noqa: BLE001 - never a silent pass-through
            return sql, Refusal(f"statement the proxy could not translate "
                                f"({type(exc).__name__}: {exc})")

    # ---------------------------------------------------------------- a connection
    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("sockname")
        if self.destinations is not None and sock:
            self.destinations.accepted(int(sock[1]))
        session = _Session(self, reader, writer)
        try:
            await session.run()
        except (ConnectionError, asyncio.IncompleteReadError, OSError):
            pass
        finally:
            await session.close()


class _Session:
    def __init__(self, proxy: ProxyServer, reader, writer):
        self.proxy = proxy
        self.client_r, self.client_w = reader, writer
        self.up_r = self.up_w = None
        self.prepared: dict[str, Prepared] = {}
        self.portals: dict[str, str] = {}
        self.refusals: dict[str, Refusal] = {}

    async def close(self) -> None:
        for w in (self.up_w, self.client_w):
            if w is not None:
                try:
                    w.close()
                    await w.wait_closed()
                except (ConnectionError, OSError, RuntimeError):
                    pass

    async def run(self) -> None:
        startup = await self._startup()
        if startup is None:
            return
        try:
            self.up_r, self.up_w = await asyncio.open_connection(*self.proxy.upstream)
        except OSError as exc:
            self.client_w.write(error_response("08006", f"the warehouse is not reachable: "
                                                        f"{exc}", "FATAL"))
            await self.client_w.drain()
            return
        self.up_w.write(startup)
        await self.up_w.drain()
        forward = asyncio.create_task(self._client_to_upstream())
        back = asyncio.create_task(self._upstream_to_client())
        done, pending = await asyncio.wait({forward, back}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(
                    exc, (ConnectionError, asyncio.IncompleteReadError, OSError)):
                raise exc

    async def _startup(self) -> bytes | None:
        """The client's StartupMessage, filtered - or None (cancel relayed, or gone)."""
        while True:
            head = await self.client_r.readexactly(8)
            length, code = struct.unpack("!II", head)
            rest = await self.client_r.readexactly(length - 8) if length > 8 else b""
            if code in (SSL_REQUEST, GSSENC_REQUEST):
                self.client_w.write(b"N")
                await self.client_w.drain()
                continue
            if code == CANCEL_REQUEST:
                try:
                    _, w = await asyncio.open_connection(*self.proxy.upstream)
                    w.write(head + rest)
                    await w.drain()
                    w.close()
                except OSError:
                    pass
                return None
            if code != PROTOCOL_3:
                self.client_w.write(error_response(
                    "08P01", f"unsupported frontend protocol {code >> 16}.{code & 0xFFFF}",
                    "FATAL"))
                await self.client_w.drain()
                return None
            params = parse_startup(head[4:] + rest)
            kept = {k: v for k, v in params.items() if k.lower() in STARTUP_KEEP}
            return startup_message(kept)

    # ---------------------------------------------------------------- client -> server
    async def _client_to_upstream(self) -> None:
        while True:
            head = await self.client_r.readexactly(5)
            kind = head[:1]
            (length,) = struct.unpack("!I", head[1:])
            body = await self.client_r.readexactly(length - 4)
            started = time.monotonic()
            if kind == b"Q":
                body = await self._query(body, started)
            elif kind == b"P":
                body = await self._parse(body, started)
            elif kind == b"B":
                portal, at = _cstring(body, 0)
                statement, _ = _cstring(body, at)
                self.portals[portal] = statement
            elif kind == b"E":
                portal, _ = _cstring(body, 0)
                await self._execute(portal, started)
            elif kind == b"C" and body[:1] in (b"S", b"P"):
                name, _ = _cstring(body, 1)
                (self.prepared if body[:1] == b"S" else self.portals).pop(name, None)
            self.up_w.write(message(kind, body))
            await self.up_w.drain()
            if kind == b"X":
                return

    def _refusal_sql(self, refusal: Refusal) -> str:
        token = SENTINEL + uuid.uuid4().hex[:16]
        self.refusals[token] = refusal
        return f"SELECT {token}()"

    async def _query(self, body: bytes, started: float) -> bytes:
        sql = body[:-1].decode("utf-8", "replace") if body.endswith(b"\0") else \
            body.decode("utf-8", "replace")
        translated, refusal = self.proxy._translate(sql)
        await self.proxy.audit(sql, translated=translated, refused=refusal, via="query",
                               started=started)
        out = self._refusal_sql(refusal) if refusal is not None else translated
        return out.encode("utf-8") + b"\0"

    async def _parse(self, body: bytes, started: float) -> bytes:
        name, at = _cstring(body, 0)
        sql, at = _cstring(body, at)
        tail = body[at:]
        translated, refusal = self.proxy._translate(sql)
        await self.proxy.audit(sql, translated=None if refusal else translated,
                               refused=refusal, via="parse", started=started)
        if refusal is not None:
            self.prepared[name] = Prepared(sql, sql, refused=True)
            # The parameter count goes with the text: nothing past the error
            # is processed before the next Sync.
            return name.encode() + b"\0" + self._refusal_sql(refusal).encode() + b"\0" + \
                struct.pack("!H", 0)
        self.prepared[name] = Prepared(sql, translated, parse_row_unspent=True)
        return name.encode() + b"\0" + translated.encode("utf-8") + b"\0" + tail

    async def _execute(self, portal: str, started: float) -> None:
        name = self.portals.get(portal)
        statement = self.prepared.get(name) if name is not None else None
        if statement is None:
            # A portal over a statement this proxy never saw parsed (a SQL-level
            # PREPARE): still one row, naming it.
            await self.proxy.audit(f"EXECUTE {name or portal}", translated=None, refused=None,
                                   via="execute", started=started)
            return
        if statement.refused:
            return
        if statement.parse_row_unspent:
            statement.parse_row_unspent = False          # the Parse row is this run's
            return
        await self.proxy.audit(statement.sql, translated=statement.translated, refused=None,
                               via="execute", started=started)

    # ---------------------------------------------------------------- server -> client
    async def _upstream_to_client(self) -> None:
        buffer = b""
        while True:
            chunk = await self.up_r.read(CHUNK)
            if not chunk:
                return
            buffer += chunk
            out, at = [], 0
            while len(buffer) - at >= 5:
                (length,) = struct.unpack("!I", buffer[at + 1:at + 5])
                end = at + 1 + length
                if end > len(buffer):
                    break
                if buffer[at:at + 1] == b"E" and SENTINEL.encode() in buffer[at + 5:end]:
                    out.append(self._rewrite_error(buffer[at + 5:end]) or buffer[at:end])
                elif buffer[at:at + 1] == b"S" and self.proxy.parameters:
                    out.append(self._rewrite_parameter(buffer[at + 5:end]) or buffer[at:end])
                else:
                    out.append(buffer[at:end])
                at = end
            buffer = buffer[at:]
            if out:
                self.client_w.write(b"".join(out))
                await self.client_w.drain()

    def _rewrite_parameter(self, body: bytes) -> bytes | None:
        try:
            name, at = _cstring(body, 0)
        except ValueError:
            return None
        value = self.proxy.parameters.get(name)
        if value is None:
            return None
        return message(b"S", name.encode() + b"\0" + value.encode() + b"\0")

    def _rewrite_error(self, body: bytes) -> bytes | None:
        fields = error_fields(body)
        text = next((v for c, v in fields if c == b"M"), "")
        found = _SENTINEL_RE.search(text)
        refusal = self.refusals.pop(found.group(0), None) if found else None
        if refusal is None:
            return None
        return error_response(refusal.sqlstate, str(refusal))


def serve(listen: tuple[str, int], upstream: tuple[str, int], *,
          translate: Callable[[str], str], tool: str, actor: str,
          dialect: str = "redshift", destinations=None, statement_log: Path | None = None,
          parameters: dict[str, str] | None = None,
          on_listening: Callable[[asyncio.base_events.Server], None] | None = None) -> None:
    """Listen on `listen`, relay to `upstream`, audit every statement. Blocks."""

    async def main() -> None:
        proxy = ProxyServer(upstream, translate=translate, tool=tool, actor=actor,
                            dialect=dialect, destinations=destinations,
                            statement_log=statement_log, parameters=parameters)
        server = await proxy.start(*listen)
        if on_listening is not None:
            on_listening(server)
        async with server:
            await server.serve_forever()

    asyncio.run(main())
