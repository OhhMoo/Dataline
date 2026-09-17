"""TLS front door for the connector wire protocol, with a fixed local upstream.

THE STATEMENT LOG. fakesnow keeps no query history file and
`docker/snowflake_fidelity.py` logs only warnings, so on the Snowflake side of
the warehouse there was nothing for a native tailer to read: the SQL an agent
ran through `snowsql` or `snowflake-connector-python` passed through this
process and left no trace (native/AUDIT_REQUESTS.md item 3). When
`ALKERA_SNOWFLAKE_STATEMENT_LOG` names a path, `forward` appends one JSON
object per executed statement to it:

    {"vts": "2026-03-05T09:00:00Z",      the episode's virtual clock, read the
                                          way native.audit reads it
     "sql": "select 1",                   the statement, as the client sent it
     "rows": 1,                           rows the warehouse answered with, or
                                          null when it refused
     "error": null,                       the failure message, or null
     "latency_ms": 4.21}                  wall time this proxy spent on it

Five keys for the four things a 9.2.2 row needs - the outcome is flattened into
`rows` and `error` so a tailer maps one key to one `native.audit.record`
argument and never has to interpret a union. Nothing else about the file is
this module's business: `native/tailers/` is where a reader of it belongs, and
that is not this file's owner (INTEGRATION_GATEWAY.md records the format and
says so). Unset, not one byte is written and this module behaves exactly as it
did.
"""

import gzip
import io
import json
import os
import time
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .native.audit import virtual_time
from .records import canonical

WIRE_PATHS = {
    "/session/v1/login-request",
    "/session",
    "/session/heartbeat",
    "/queries/v1/query-request",
    "/queries/v1/abort-request",
    "/monitoring/queries/telemetry",
}

# The one wire route that carries SQL. Everything else here is session
# management and telemetry, and logging it would put rows in the agent's action
# log for connecting rather than for doing anything.
QUERY_PATH = "/queries/v1/query-request"
STATEMENT_LOG_ENV = "ALKERA_SNOWFLAKE_STATEMENT_LOG"


def decoded(body: bytes, encoding: str) -> bytes:
    """The request body as JSON bytes. The connector may have gzipped it."""
    if "gzip" in encoding.lower():
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
                expanded = stream.read(2_097_152 + 1)
            if len(expanded) > 2_097_152:
                raise ValueError("Expanded request too large")
            return expanded
        except (OSError, EOFError):
            return b""
    return body


def statement_of(body: bytes, encoding: str) -> str | None:
    """`sqlText` out of a query-request body, or None if there is none to read."""
    try:
        payload = json.loads(decoded(body, encoding) or b"{}")
    except (ValueError, UnicodeError):
        return None
    return payload.get("sqlText") if isinstance(payload, dict) else None


def outcome(content: bytes) -> tuple[int | None, str | None]:
    """A query-response body -> (rows, error).

    `data.total` is what the wire protocol reports and `rowset` is what it
    carries; the length is used only when `total` is absent, so a truncated
    read is never reported as a smaller result than the server gave.
    """
    try:
        payload = json.loads(content or b"{}")
    except (ValueError, UnicodeError):
        return None, "unreadable response body"
    if not isinstance(payload, dict):
        return None, "unreadable response body"
    if not payload.get("success", True):
        return None, str(payload.get("message") or payload.get("code") or "query failed")
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        return None, "unreadable response data"
    total = data.get("total")
    if isinstance(total, (int, float)):
        return int(total), None
    rowset = data.get("rowset")
    return (len(rowset) if isinstance(rowset, list) else None), None


def log_statement(sql: str, rows: int | None, error: str | None, latency_ms: float) -> None:
    """Append one line to `$ALKERA_SNOWFLAKE_STATEMENT_LOG`, or do nothing.

    Failing to write must never fail the query the agent asked for - the proxy
    is in the data path - so an unwritable log is silently skipped here and
    caught where it matters instead: a tailer that finds no file reports the
    plane unobserved, which is the loud end of the same signal.
    """
    path = os.environ.get(STATEMENT_LOG_ENV)
    if not path:
        return
    entry = {
        "vts": virtual_time(),
        "sql": sql,
        "rows": rows,
        "error": error,
        "latency_ms": round(latency_ms, 3),
    }
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "ab") as stream:
            stream.write(canonical(entry) + b"\n")
            stream.flush()
    except OSError:
        return


def proxy_app(upstream: str, transport=None):
    async def forward(request: Request):
        if request.url.path not in WIRE_PATHS:
            return JSONResponse({"success": False, "message": "Unsupported wire route"}, 404)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 2_097_152:
                return JSONResponse({"success": False, "message": "Request too large"}, 413)
            body.extend(chunk)
        body = bytes(body)
        try:
            expanded = decoded(body, request.headers.get("content-encoding", ""))
        except ValueError:
            return JSONResponse({"success": False, "message": "Request too large"}, 413)
        sql = (statement_of(expanded, "")
               if request.url.path == QUERY_PATH else None)
        started = time.monotonic()
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() in {"content-type", "content-encoding", "authorization", "accept"}
        }
        try:
            async with httpx.AsyncClient(
                transport=transport, timeout=10, trust_env=False
            ) as client:
                response = await client.request(
                    request.method,
                    upstream + request.url.path,
                    params=request.query_params,
                    content=body,
                    headers=headers,
                )
        except httpx.HTTPError:
            if sql is not None:
                log_statement(sql, None, "Local warehouse unavailable",
                              (time.monotonic() - started) * 1000)
            return JSONResponse(
                {
                    "success": False,
                    "message": "Local warehouse unavailable",
                    "code": "390000",
                    "data": {},
                },
                200,
            )
        if response.status_code >= 500:
            if sql is not None:
                log_statement(sql, None, "Local warehouse failed",
                              (time.monotonic() - started) * 1000)
            return JSONResponse(
                {
                    "success": False,
                    "message": "Local warehouse failed",
                    "code": "390000",
                    "data": {},
                },
                200,
            )
        if sql is not None:
            rows, error = outcome(response.content)
            log_statement(sql, rows, error, (time.monotonic() - started) * 1000)
        return Response(
            response.content,
            response.status_code,
            media_type=response.headers.get("content-type", "application/json"),
        )

    return Starlette(routes=[Route("/{path:path}", forward, methods=["GET", "POST"])])


def main():
    import uvicorn

    uvicorn.run(
        proxy_app(os.environ.get("FAKESNOW_UPSTREAM", "http://127.0.0.1:64616")),
        host="0.0.0.0",
        port=8443,
        ssl_certfile="/tls/server.crt",
        ssl_keyfile="/tls/server.key",
    )


if __name__ == "__main__":
    main()
