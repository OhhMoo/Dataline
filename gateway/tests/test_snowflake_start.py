"""gateway/docker/snowflake_start.py: the fakesnow upstream on a private unix socket.

fakesnow answers the wire protocol with no credential check and no audit; the TLS
proxy in front of it is the door. In a container the agent shares (the Harbor image),
the upstream's loopback port would be a second door, so ALKERA_SNOWFLAKE_UPSTREAM_SOCKET
moves it to a unix socket in a directory only the service's user can enter. Unset,
the route is exactly what it was.

    gateway/.venv/bin/python -m pytest -q gateway/tests/test_snowflake_start.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import stat
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

DOCKER = Path(__file__).resolve().parents[1] / "docker"


def _start():
    spec = importlib.util.spec_from_file_location("snowflake_start_for_tests",
                                                  DOCKER / "snowflake_start.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_without_the_socket_the_route_is_unchanged(monkeypatch):
    start = _start()
    monkeypatch.delenv(start.UPSTREAM_SOCKET_ENV, raising=False)
    monkeypatch.delenv("FAKESNOW_UPSTREAM", raising=False)
    assert start.upstream_route() == (f"http://127.0.0.1:{start.UPSTREAM_PORT}", None)
    monkeypatch.setenv("FAKESNOW_UPSTREAM", "http://elsewhere:1")
    assert start.upstream_route() == ("http://elsewhere:1", None)


def test_the_socket_directory_is_made_private(tmp_path):
    start = _start()
    sock = tmp_path / "run" / "fakesnow.sock"
    (tmp_path / "run").mkdir(mode=0o755)
    sock.write_text("stale")
    start.private_socket_dir(sock)
    assert stat.S_IMODE((tmp_path / "run").stat().st_mode) == 0o700
    assert not sock.exists()


def test_the_proxy_reaches_the_upstream_through_the_socket(tmp_path, monkeypatch):
    """A stand-in upstream served on the socket (uvicorn `uds`, as upstream() serves
    fakesnow), the real proxy app routed by upstream_route(): two wire requests,
    concurrent ones too, arrive - and the socket's directory admits only its owner."""
    from alkera_gateway import snowflake_proxy

    start = _start()
    sock = tmp_path / "run" / "fakesnow.sock"
    start.private_socket_dir(sock)
    seen = []

    async def login(request: Request):
        seen.append(request.url.path)
        await asyncio.sleep(0.05)
        return JSONResponse({"success": True, "data": {"token": "t"}})

    upstream = Starlette(routes=[Route("/session/v1/login-request", login,
                                       methods=["POST"])])
    server = uvicorn.Server(uvicorn.Config(upstream, uds=str(sock), log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    try:
        monkeypatch.setenv(start.UPSTREAM_SOCKET_ENV, str(sock))
        url, transport = start.upstream_route()
        assert url == "http://fakesnow" and transport is not None
        app = snowflake_proxy.proxy_app(url, transport=transport)

        async def drive():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="https://snowflake.internal") as client:
                one = await client.post("/session/v1/login-request", content=b"{}")
                many = await asyncio.gather(*(client.post("/session/v1/login-request",
                                                          content=b"{}") for _ in range(4)))
            return [one, *many]

        answers = asyncio.run(drive())
        assert [a.status_code for a in answers] == [200] * 5
        assert all(a.json()["success"] for a in answers)
        assert seen == ["/session/v1/login-request"] * 5
        assert stat.S_IMODE(sock.parent.stat().st_mode) == 0o700
        assert sock.parent.stat().st_uid == os.geteuid()
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_snowflake_tls_bare_dial_is_counted(tmp_path, monkeypatch):
    """The native door (serve_native's listener, as native_listener() gives it to
    uvicorn.run: TLS on PORT, AuditedH11Protocol) counts a TCP dial that never
    sends a ClientHello, as the plain-TCP doors count theirs
    (CONTAINMENT_2026-10-07.md: snowflake.internal:8443 read `count: 0` after the
    box probe had connected)."""
    import json
    import socket

    from alkera_gateway.native import serve
    from alkera_gateway.native.tls import mint

    start = _start()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    mint(start.HOSTNAME, tmp_path / "trust", private_dir=tmp_path / "tls",
         alt_names=start.ALT_NAMES)
    destinations = tmp_path / "audit" / "destinations.snowflake.jsonl"
    monkeypatch.setattr(start, "TLS", tmp_path / "tls")
    monkeypatch.setattr(start, "PORT", port)
    monkeypatch.setattr(start, "upstream_route", lambda: ("http://127.0.0.1:9", None))
    monkeypatch.setenv(start.DESTINATIONS_ENV, str(destinations))
    monkeypatch.setattr(serve.AuditedH11Protocol, "destinations", None)

    app, options = start.native_listener(host="127.0.0.1")
    assert options["port"] == port and options["ssl_certfile"].startswith(str(tmp_path))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", **options))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    def counted() -> int:
        rows = [json.loads(line) for line in destinations.read_text().splitlines()]
        return {(r["host"], r["port"]): r["count"] for r in rows}[(start.NATIVE_HOST, port)]

    try:
        assert counted() == 0  # declared before the first socket opens
        deadline = time.monotonic() + 20
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        with socket.create_connection(("127.0.0.1", port), timeout=10):
            pass  # no ClientHello: the handshake never starts
        deadline = time.monotonic() + 10
        while counted() < 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.5)
        assert counted() == 1
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_a_socket_directory_of_another_user_is_refused(tmp_path, monkeypatch):
    start = _start()
    real = Path.stat

    class Foreign:
        st_uid = os.geteuid() + 1

    monkeypatch.setattr(Path, "stat", lambda self, *a, **kw: Foreign()
                        if self == tmp_path / "run" else real(self, *a, **kw))
    with pytest.raises(SystemExit, match="not this user's"):
        start.private_socket_dir(tmp_path / "run" / "fakesnow.sock")
