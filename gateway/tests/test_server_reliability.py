"""Failure, concurrency and bounded-work regressions for both transports."""

import asyncio
import gzip
import json
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from importlib.metadata import PackageNotFoundError

import httpx
import pytest

from alkera_gateway import Gateway
from alkera_gateway import audit as mcp_audit
from alkera_gateway.audit import validate_trace
from alkera_gateway.native import audit, context, serve
from alkera_gateway.native.context import ServiceError
from alkera_gateway.native.services import metabase
from alkera_gateway.server import http_app
from alkera_gateway.snowflake_proxy import proxy_app


async def test_audit_begin_failure_marks_gateway_unavailable(gateway, monkeypatch):
    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(gateway.audit, "begin", fail)
    with pytest.raises(OSError, match="disk full"):
        gateway.call("sql_query", {"sql": "select 1"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=http_app(gateway)), base_url="http://test"
    ) as client:
        assert (await client.get("/health")).status_code == 503
        call = await client.post("/call", json={"tool": "sql_query", "args": {}})
        assert call.status_code == 503
    assert not gateway.audit.actions.exists()


def test_sql_gateway_works_without_optional_dbt(world, tmp_path, monkeypatch):
    from conftest import config

    real_version = mcp_audit.version

    def installed(name):
        if name in {"dbt-core", "dbt-duckdb"}:
            raise PackageNotFoundError(name)
        return real_version(name)

    monkeypatch.setattr(mcp_audit, "version", installed)
    instance = Gateway(config(world, tmp_path / "sql-audit"))
    try:
        assert instance.call("sql_query", {"sql": "select 1"})["ok"]
    finally:
        instance.close()


def test_disk_telemetry_is_sampled_and_expires(gateway, monkeypatch):
    scans = []
    now = [10.0]
    monkeypatch.setattr("alkera_gateway.core.time.monotonic", lambda: now[0])
    monkeypatch.setattr(gateway, "_measure_disk_mb", lambda: scans.append(1) or len(scans))
    assert gateway._disk_mb() == 1
    assert gateway._disk_mb() == 1
    now[0] += 5
    assert gateway._disk_mb() == 2


@pytest.mark.parametrize("contents", [None, b"", b"{"])
def test_explicit_native_config_never_falls_back(tmp_path, monkeypatch, contents):
    path = tmp_path / "config.json"
    if contents is not None:
        path.write_bytes(contents)
    monkeypatch.setenv(context.CONFIG_ENV, str(path))
    with pytest.raises((FileNotFoundError, ValueError)):
        context.process_context()


def test_native_audit_handles_large_rows_and_refuses_torn_append(tmp_path, monkeypatch):
    path = tmp_path / "actions.jsonl"
    monkeypatch.setenv("ALKERA_NATIVE_AUDIT", str(path))
    args = dict(tool="test.read", args={}, op_class="read", targets=["test"])
    audit.record(**args, result="x" * 30_000)
    audit.record(**args, result="y" * 30_000)
    assert audit.last_sequence(path) == 2
    assert len(validate_trace(path)) == 2
    with path.open("ab") as stream:
        stream.write(b'{"seq":3')
    damaged = path.read_bytes()
    with pytest.raises(ValueError, match="Incomplete"):
        audit.record(**args)
    assert path.read_bytes() == damaged


def test_destination_log_concurrent_flush_keeps_every_accept(tmp_path):
    log = serve.DestinationLog(tmp_path / "destinations.jsonl")
    log.declare("jira.internal", 8080)
    with ThreadPoolExecutor(max_workers=8) as workers:
        list(workers.map(log.accepted, [8080] * 40))
    assert json.loads(log.path.read_text())["count"] == 40


async def test_native_query_leaves_event_loop_responsive(monkeypatch):
    started, release = threading.Event(), threading.Event()

    def resolve(*args):
        started.set()
        release.wait(timeout=3)
        raise ServiceError(404, "card_not_found", "test card")

    monkeypatch.setattr(metabase, "_resolve", resolve)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=metabase.app), base_url="http://test"
    ) as client:
        query = asyncio.create_task(client.post("/api/card/1/query", json={}))
        try:
            async with asyncio.timeout(2):
                while not started.is_set():
                    await asyncio.sleep(0.01)
            assert not query.done(), "blocking query ran on the shared event loop"
            assert (await client.get("/missing")).status_code == 404
        finally:
            release.set()
            await query


@pytest.mark.parametrize("body", [b"[]", b"null", b"{"])
async def test_native_json_shapes_are_client_errors(body):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=metabase.app), base_url="http://test"
    ) as client:
        response = await client.post("/api/card/1/query", content=body)
    assert response.status_code == 400


async def test_proxy_rejects_stream_and_gzip_before_forwarding():
    def upstream(request):
        pytest.fail("oversized request reached the warehouse")

    async def chunks():
        yield b"x" * 2_097_152
        yield b"x"
        pytest.fail("read beyond the request limit")

    app = proxy_app("http://upstream", httpx.MockTransport(upstream))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post("/queries/v1/query-request", content=chunks())).status_code == 413
        compressed = gzip.compress(b"x" * 2_097_153)
        response = await client.post(
            "/queries/v1/query-request", content=compressed, headers={"content-encoding": "gzip"}
        )
        assert response.status_code == 413


async def test_native_port_conflict_closes_previously_bound_sockets(tmp_path):
    with socket.create_server(("127.0.0.1", 0)) as occupied:
        with socket.create_server(("127.0.0.1", 0)) as available:
            free_port = available.getsockname()[1]
        mounted = [
            serve.Mounted("first", "localhost", free_port, serve.stub_app("first"), "stub", False),
            serve.Mounted("busy", "localhost", occupied.getsockname()[1],
                          serve.stub_app("busy"), "stub", False),
        ]
        with pytest.raises(OSError):
            log = serve.DestinationLog(tmp_path / "dest.jsonl")
            await serve.serve_all(mounted, "127.0.0.1", log)
        with socket.create_server(("127.0.0.1", free_port)):
            pass


async def test_native_listener_failure_stops_its_peers(tmp_path, monkeypatch):
    servers = []

    class Server:
        should_exit = False

        def __init__(self, config):
            servers.append(self)

        async def serve(self, sockets):
            if self is servers[0]:
                raise RuntimeError("listener failed")
            while not self.should_exit:
                await asyncio.sleep(0.01)

    monkeypatch.setattr(serve, "_Server", Server)
    mounted = [serve.Mounted(name, "localhost", 0, serve.stub_app(name), "stub", False)
               for name in ("first", "second")]
    with pytest.raises(RuntimeError, match="listener failed"):
        await serve.serve_all(mounted, "127.0.0.1", serve.DestinationLog(tmp_path / "dest.jsonl"))
    assert all(server.should_exit for server in servers)
