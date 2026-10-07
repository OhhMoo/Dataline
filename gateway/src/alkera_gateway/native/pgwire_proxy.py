"""An audited PostgreSQL wire-protocol proxy: the front door of a Postgres persona.

    serve(("0.0.0.0", 5439), "/redshift-data/run/.s.PGSQL.5432",
          translate=redshift_sql.translate, tool="redshift.statement", actor="redshift",
          refuse=redshift_sql.Unsupported, startup_filter=redshift_sql.startup_parameters)

The Redshift plug (gateway/docker/redshift_start.py) is a PostgreSQL server that
listens on a private unix socket only, with this proxy in front of it on the
port the agent's clients dial. An HTTP proxy cannot see the Postgres wire
protocol, and the Postgres log tailer writes its rows only at teardown, so the
audit lives here: every statement a client runs is ONE 9.2.2 row (`tool`),
written server-side BEFORE the statement is forwarded - so the row is ordered
before anything the agent does next.

WHAT THE PROXY DOES, message by message (protocol 3.0). DEFAULT DENY: a frontend
message is relayed only if it is listed here; any other type is written as an
error row and refused (FRONTEND_REFUSED names the known ones).

  SSLRequest / GSSENCRequest   answered `N`: the cluster does not require SSL
                               (the box's onboarding says so, and the clients
                               connect with ssl=False / sslmode=disable).
  CancelRequest                relayed to the upstream as is.
  StartupMessage               relayed with only the parameters PostgreSQL
                               knows (STARTUP_KEEP: redshift_connector sends its
                               own client_protocol_version, driver_version, ...,
                               which Postgres would reject), then through the
                               persona's `startup_filter` (the `options`
                               parameter's `-c` settings); a refused one is a
                               FATAL and an error row. Authentication then
                               passes through untouched (the upstream's MD5).
  ParameterStatus (server)     `parameters` overrides the server's own values
                               (the persona's server_version).
  Query ('Q')                  translated; ONE row; forwarded.
  Parse ('P')                  translated; ONE row; forwarded. The row stands for
                               the statement's first run. (At Parse, not at
                               Execute, because a statement the server rejects
                               at parse - an unknown relation - never runs, and
                               it was still the agent's action.) The name ->
                               statement map changes only when the server says
                               ParseComplete: a Parse it rejects (the name is
                               taken) leaves the old statement where it was.
  Bind ('B')                   binds the portal to the statement the SERVER
                               will see: a Parse earlier in the same batch, else
                               the confirmed map (waiting for an earlier batch's
                               answers first, so a pipelined Bind cannot be
                               attributed to a Parse the server then rejected).
  Execute ('E')                the FIRST Execute after a Bind is one run: ONE row
                               (none for the run a Parse row stands for); more
                               Executes on the same portal (a row limit, then
                               "fetch the rest") are the same run, no row.
  Describe, Close, Flush,
  Sync, Terminate, Password    relayed.
  anything else                ONE error row, then refused: the message is
                               replaced by a Query that fails, so the client
                               gets ErrorResponse + ReadyForQuery - what it
                               waits for after a FunctionCall ('F').

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
from collections import deque
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
#: Frontend messages relayed as they are (Q, P, B and E are handled).
RELAYED = frozenset({b"D", b"C", b"H", b"S", b"X", b"p"})
#: Names for the frontend messages the proxy refuses (any unlisted type is
#: refused too, under its type byte).
FRONTEND_REFUSED = {
    b"F": "FunctionCall (the fastpath protocol)",
    b"d": "CopyData (COPY is not imitated)",
    b"c": "CopyDone (COPY is not imitated)",
    b"f": "CopyFail (COPY is not imitated)",
}
SENTINEL = "alkera_refused_"
_SENTINEL_RE = re.compile(SENTINEL + r"[0-9a-f]{16}")
CHUNK = 1 << 16

#: Where the upstream is: (host, port) over TCP, or a unix socket's path.
Upstream = tuple[str, int] | str


class Refusal(Exception):
    """A statement the persona refuses: an ErrorResponse with `sqlstate` and
    `str(self)` as its message, and an error row."""

    sqlstate = "0A000"


def _refusal(feature: str) -> Refusal:
    return Refusal(f"not supported by this server: {feature}")


@dataclass
class Prepared:
    sql: str
    translated: str
    refused: bool = False
    #: True from Parse until the first run, which the Parse row stands for;
    #: every later run writes its own row.
    parse_row_unspent: bool = False


@dataclass
class Portal:
    statement: Prepared | None
    statement_name: str
    ran: bool = False


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


async def open_upstream(upstream: Upstream):
    if isinstance(upstream, str):
        return await asyncio.open_unix_connection(upstream)
    return await asyncio.open_connection(*upstream)


# --------------------------------------------------------------------------- the proxy
class ProxyServer:
    """One listening proxy; `handle` serves one client connection."""

    def __init__(self, upstream: Upstream, *, translate: Callable[[str], str],
                 tool: str, actor: str, dialect: str = "redshift",
                 destinations=None, statement_log: Path | None = None,
                 record: Callable[..., dict] | None = None,
                 parameters: dict[str, str] | None = None,
                 refuse: Callable[[str], Refusal] = _refusal,
                 startup_filter: Callable[[dict[str, str]], dict[str, str]] | None = None):
        self.upstream = upstream
        self.translate = translate
        self.tool = tool
        self.actor = actor
        self.dialect = dialect
        # ParameterStatus values the persona reports instead of the server's
        # (Redshift's `server_version` is 8.0.2, and redshift_connector parses
        # it: Debian's "17.11 (Debian 17.11-0+deb13u1)" is an InvalidVersion).
        self.parameters = dict(parameters or {})
        self.refuse = refuse
        self.startup_filter = startup_filter
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
        protocol = via in ("message", "startup")
        seen = observation(sql, self.dialect) if sql.strip() and not protocol else None
        result = {"forwarded": refused is None,
                  "translated": translated if translated not in (None, sql) else None}
        flags = list(seen.flags) if seen is not None else []
        if protocol:
            flags.append(f"protocol_{via}")
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
            return sql, self.refuse(f"statement the proxy could not translate "
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
        # name -> statement, as the server has confirmed it (ParseComplete)
        self.prepared: dict[str, Prepared] = {}
        # (batch, name, statement) forwarded, not yet confirmed or dropped
        self.pending: deque[tuple[int, str, Prepared]] = deque()
        self.batch = 0          # Syncs and Queries forwarded: each ends a batch
        self.answered = 0       # ReadyForQuery seen: batches the server finished
        self.changed = asyncio.Condition()
        self.portals: dict[str, Portal] = {}
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
            self.up_r, self.up_w = await open_upstream(self.proxy.upstream)
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
        """The client's StartupMessage, filtered - or None (cancel relayed,
        refused, or gone)."""
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
                    _, w = await open_upstream(self.proxy.upstream)
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
            if self.proxy.startup_filter is not None:
                try:
                    kept = self.proxy.startup_filter(kept)
                except Refusal as refusal:
                    await self.proxy.audit(
                        f"/* startup parameters {json.dumps(kept, sort_keys=True)} */",
                        translated=None, refused=refusal, via="startup",
                        started=time.monotonic())
                    self.client_w.write(error_response(refusal.sqlstate, str(refusal),
                                                       "FATAL"))
                    await self.client_w.drain()
                    return None
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
                name, _ = _cstring(body, at)
                self.portals[portal] = Portal(await self._resolve(name), name)
            elif kind == b"E":
                portal, _ = _cstring(body, 0)
                await self._execute(portal, started)
            elif kind == b"C" and body[:1] in (b"S", b"P"):
                name, _ = _cstring(body, 1)
                if body[:1] == b"S":
                    self.prepared.pop(name, None)
                    self.pending = deque(e for e in self.pending
                                         if not (e[0] == self.batch and e[1] == name))
                else:
                    self.portals.pop(name, None)
            elif kind not in RELAYED:
                kind, body = b"Q", await self._refuse_message(kind, body, started)
            self.up_w.write(message(kind, body))
            await self.up_w.drain()
            if kind in (b"Q", b"S"):
                self.batch += 1
            if kind == b"X":
                return

    def _refusal_sql(self, refusal: Refusal) -> str:
        token = SENTINEL + uuid.uuid4().hex[:16]
        self.refusals[token] = refusal
        return f"SELECT {token}()"

    async def _refuse_message(self, kind: bytes, body: bytes, started: float) -> bytes:
        """Default deny: a frontend message the proxy does not relay is one
        error row and a Query that fails with the refusal."""
        name = FRONTEND_REFUSED.get(kind) or f"frontend message type {kind!r}"
        what = name
        if kind == b"F" and len(body) >= 4:
            what = f"{name}, function oid {struct.unpack('!I', body[:4])[0]}"
        refusal = self.proxy.refuse(name)
        await self.proxy.audit(f"/* {what} */", translated=None, refused=refusal,
                               via="message", started=started)
        return self._refusal_sql(refusal).encode("utf-8") + b"\0"

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
            self.pending.append((self.batch, name, Prepared(sql, sql, refused=True)))
            # The parameter count goes with the text: nothing past the error
            # is processed before the next Sync.
            return name.encode() + b"\0" + self._refusal_sql(refusal).encode() + b"\0" + \
                struct.pack("!H", 0)
        self.pending.append((self.batch, name,
                             Prepared(sql, translated, parse_row_unspent=True)))
        return name.encode() + b"\0" + translated.encode("utf-8") + b"\0" + tail

    async def _resolve(self, name: str) -> Prepared | None:
        """The statement the server will bind `name` to: a Parse of it earlier
        in this batch (if that Parse fails, the server skips the Bind too),
        else what the server has confirmed - once every earlier batch is
        answered, so a pipelined Parse the server rejects is not believed."""
        current = [p for b, n, p in self.pending if b == self.batch and n == name]
        if current:
            return current[-1]
        async with self.changed:
            await self.changed.wait_for(
                lambda: not any(b < self.batch for b, _, _ in self.pending))
        return self.prepared.get(name)

    async def _execute(self, name: str, started: float) -> None:
        portal = self.portals.get(name)
        if portal is not None and portal.ran:
            return                               # the same run, fetching more rows
        if portal is not None:
            portal.ran = True
        statement = portal.statement if portal is not None else None
        if statement is None:
            # A portal over a statement this proxy never saw parsed (a SQL-level
            # PREPARE), or no portal at all: still one row, naming it.
            label = portal.statement_name if portal is not None else name
            await self.proxy.audit(f"EXECUTE {label}", translated=None, refused=None,
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
            out, at, settled = [], 0, False
            while len(buffer) - at >= 5:
                (length,) = struct.unpack("!I", buffer[at + 1:at + 5])
                end = at + 1 + length
                if end > len(buffer):
                    break
                kind = buffer[at:at + 1]
                if kind == b"1" and self.pending:          # ParseComplete
                    _, name, statement = self.pending.popleft()
                    self.prepared[name] = statement
                    settled = True
                elif kind == b"Z":                         # ReadyForQuery
                    self.answered += 1
                    self.pending = deque(e for e in self.pending if e[0] >= self.answered)
                    settled = True
                if kind == b"E" and SENTINEL.encode() in buffer[at + 5:end]:
                    out.append(self._rewrite_error(buffer[at + 5:end]) or buffer[at:end])
                elif kind == b"S" and self.proxy.parameters:
                    out.append(self._rewrite_parameter(buffer[at + 5:end]) or buffer[at:end])
                else:
                    out.append(buffer[at:end])
                at = end
            buffer = buffer[at:]
            if settled:
                async with self.changed:
                    self.changed.notify_all()
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


def serve(listen: tuple[str, int], upstream: Upstream, *,
          translate: Callable[[str], str], tool: str, actor: str,
          dialect: str = "redshift", destinations=None, statement_log: Path | None = None,
          parameters: dict[str, str] | None = None,
          refuse: Callable[[str], Refusal] = _refusal,
          startup_filter: Callable[[dict[str, str]], dict[str, str]] | None = None,
          on_listening: Callable[[asyncio.base_events.Server], None] | None = None) -> None:
    """Listen on `listen`, relay to `upstream`, audit every statement. Blocks."""

    async def main() -> None:
        proxy = ProxyServer(upstream, translate=translate, tool=tool, actor=actor,
                            dialect=dialect, destinations=destinations,
                            statement_log=statement_log, parameters=parameters,
                            refuse=refuse, startup_filter=startup_filter)
        server = await proxy.start(*listen)
        if on_listening is not None:
            on_listening(server)
        async with server:
            await server.serve_forever()

    asyncio.run(main())
