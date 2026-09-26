"""The subject container's only way off the machine: an allowlisting CONNECT proxy.

The model's CLI needs the inference API, and nothing else. The `subject`
container sits on internal networks only (no default route), so every byte it
sends off-box goes through this process, which:

  * accepts HTTP CONNECT to host:port pairs in ALKERA_EGRESS_ALLOW and nothing
    else - no plain HTTP, no other host, no IP literals unless listed;
  * logs one JSON line per connection to stdout (allowed or refused, bytes each
    way, duration), which the harness saves as `<rollout>/container/egress.jsonl`
    at teardown.

TLS is not intercepted: the proxy sees the hostname the client asked for and the
byte counts, never the content, so the credential in the request headers stays
between the CLI and the API.

Stdlib only; runs on the subject image as `nobody`.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

ALLOW = {entry.strip().lower() for entry in
         os.environ.get("ALKERA_EGRESS_ALLOW", "api.anthropic.com:443").split(",")
         if entry.strip()}
PORT = int(os.environ.get("ALKERA_EGRESS_PORT", "3128"))


def log(**row) -> None:
    row["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    sys.stdout.write(json.dumps(row, sort_keys=True) + "\n")
    sys.stdout.flush()


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
               counter: list[int]) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            counter[0] += len(data)
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:                                  # noqa: BLE001
            pass


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")
    started = time.time()
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=30)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError,
            asyncio.LimitOverrunError, ConnectionError):
        writer.close()
        return
    line = head.split(b"\r\n", 1)[0].decode("latin-1")
    parts = line.split()
    method = parts[0].upper() if parts else ""
    target = parts[1].lower() if len(parts) > 1 else ""
    if method != "CONNECT" or target not in ALLOW:
        log(event="refused", method=method, target=target[:200], peer=str(peer))
        writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()
        return
    host, _, port = target.rpartition(":")
    try:
        up_reader, up_writer = await asyncio.wait_for(
            asyncio.open_connection(host, int(port)), timeout=30)
    except (OSError, asyncio.TimeoutError) as exc:
        log(event="upstream_failed", target=target, error=str(exc)[:200])
        writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()
        return
    writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
    await writer.drain()
    up, down = [0], [0]
    await asyncio.gather(pipe(reader, up_writer, up), pipe(up_reader, writer, down))
    log(event="allowed", target=target, bytes_up=up[0], bytes_down=down[0],
        seconds=round(time.time() - started, 2), peer=str(peer))


async def main() -> None:
    server = await asyncio.start_server(handle, "0.0.0.0", PORT)
    log(event="listening", port=PORT, allow=sorted(ALLOW))
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
