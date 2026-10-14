"""serve.py's TLS doors count a dial at accept, not after the handshake.

On a TLS listener asyncio calls the protocol's `connection_made` only once the
handshake has completed, so a dial that never finishes one (a TCP probe, a port
scan, a client that does not trust the bundle) was invisible to the destination
audit (gateway/docker/evidence/CONTAINMENT_2026-10-07.md, "Reachable destinations
that are not audited doors"). The count now happens in the listener's protocol
factory, which asyncio calls at accept; a connection that does complete the
handshake must still be counted once, not twice.

    cd gateway && .venv/bin/python -m pytest -q tests/test_tls_destinations.py
"""
from __future__ import annotations

import asyncio
import contextlib
import socket
import ssl
import threading
import time
import urllib.request
from pathlib import Path

from alkera_gateway.native import serve
from alkera_gateway.native.tls import mint

HOST = "tls-door.internal"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def listening(port: int) -> bool:
    """Is `port` in LISTEN? Read from /proc/net/tcp, never dialled (a readiness dial
    would be a destination count of its own)."""
    rows = (line.split() for line in
            Path("/proc/net/tcp").read_text().splitlines()[1:])
    return any(row[1].endswith(f":{port:04X}") and row[3] == "0A" for row in rows)


@contextlib.contextmanager
def serving(mounted: list[serve.Mounted], log: serve.DestinationLog, allowed=()):
    """serve.py's own `serve_all`, on its own loop in a thread, until the block ends.

    Ready once every port is in LISTEN: `serve_all` binds the whole set before any
    uvicorn server starts, and a connection made in between waits in the listen
    backlog and is accepted once its server does."""
    loop = asyncio.new_event_loop()
    task = loop.create_task(serve.serve_all(mounted, "127.0.0.1", log, allowed=allowed))
    thread = threading.Thread(target=lambda: loop.run_until_complete(
        asyncio.gather(task, return_exceptions=True)), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while not all(listening(item.port) for item in mounted):
            assert thread.is_alive() and time.monotonic() < deadline, "never listened"
            time.sleep(0.05)
        yield
    finally:
        loop.call_soon_threadsafe(task.cancel)
        thread.join(timeout=20)
        loop.close()


def count(log: serve.DestinationLog, port: int) -> int:
    return {row["port"]: row["count"] for row in log.rows()}[port]


def settled(log: serve.DestinationLog, port: int, *, at_least: int,
            timeout: float = 10.0) -> int:
    """The port's count once it reaches `at_least`, then given time to overshoot."""
    deadline = time.monotonic() + timeout
    while count(log, port) < at_least and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.5)  # a second count, if there is one, lands well inside this
    return count(log, port)


def bare_dial(port: int) -> None:
    """A TCP connect and close: no ClientHello, no byte sent."""
    with socket.create_connection(("127.0.0.1", port), timeout=10):
        pass


def tls_door(tmp_path) -> tuple[serve.Mounted, serve.DestinationLog, ssl.SSLContext]:
    trust = tmp_path / "trust"
    certfile, keyfile = mint(HOST, trust, private_dir=tmp_path / "private")
    port = free_port()
    item = serve.Mounted("tlsdoor", HOST, port, serve.stub_app("tlsdoor"), "stub", False,
                         tls=True, certificate=lambda: (certfile, keyfile))
    log = serve.DestinationLog(tmp_path / "destinations.jsonl")
    log.declare(HOST, port)
    return item, log, ssl.create_default_context(cafile=str(trust / "server.crt"))


def test_tls_dial_without_handshake_is_counted_once(tmp_path):
    item, log, _ = tls_door(tmp_path)
    with serving([item], log):
        bare_dial(item.port)
        assert settled(log, item.port, at_least=1) == 1
    row = log.rows()[0]
    assert row["host"] == HOST and row["first_ts"] and row["last_ts"]


def test_completed_tls_request_is_counted_once(tmp_path):
    """Review focus 1: a dial that completes its handshake is one destination count,
    not one at accept plus one in connection_made."""
    item, log, trusted = tls_door(tmp_path)
    with serving([item], log):
        request = urllib.request.Request(f"https://localhost:{item.port}/health",
                                         headers={"Connection": "close"})
        with urllib.request.urlopen(request, context=trusted, timeout=15) as answer:
            assert answer.status == 200
        assert settled(log, item.port, at_least=1) == 1


def test_plain_listener_counting_unchanged(tmp_path):
    """A plain listener counts in connection_made, by the accepted socket's local
    port, as it always has: a bare dial is one, a request on its own connection is
    another, and nothing is counted twice."""
    port = free_port()
    item = serve.Mounted("plain", "plain.internal", port, serve.stub_app("plain"),
                         "stub", False)
    log = serve.DestinationLog(tmp_path / "destinations.jsonl")
    log.declare("plain.internal", port)
    with serving([item], log):
        bare_dial(port)
        assert settled(log, port, at_least=1) == 1
        request = urllib.request.Request(f"http://127.0.0.1:{port}/health",
                                         headers={"Connection": "close"})
        with urllib.request.urlopen(request, timeout=15) as answer:
            assert answer.status == 200
        assert settled(log, port, at_least=2) == 2
