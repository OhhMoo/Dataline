"""The TLS front door, and the statement log a fakesnow tailer would read.

The log is the only place the SQL an agent runs over the Snowflake wire is
visible on the server side: fakesnow keeps no query history and
`docker/snowflake_fidelity.py` logs only warnings, so without it the Snowflake
plane is unauditable (native/AUDIT_REQUESTS.md item 3). What is asserted below
is the *format* - `native/tailers/` is not this file's owner, and a tailer
written against a format nobody pinned is a tailer that breaks silently.
"""

import gzip
import json

import httpx
import pytest

from alkera_gateway.snowflake_proxy import STATEMENT_LOG_ENV, proxy_app


async def test_wire_routes_forward_body_and_reject_rest():
    requests = []

    def upstream(request):
        requests.append(request)
        return httpx.Response(200, json={"success": True})

    app = proxy_app("http://fake", httpx.MockTransport(upstream))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://tls"
    ) as c:
        response = await c.post("/session/v1/login-request?requestId=1", content=b'{"data":{}}')
        assert response.json()["success"]
        assert requests[0].content == b'{"data":{}}'
        assert requests[0].url.params["requestId"] == "1"
        assert (await c.post("/api/v2/statements")).status_code == 404
        assert len(requests) == 1


async def test_upstream_failure_is_a_bounded_wire_error():
    app = proxy_app("http://fake", httpx.MockTransport(lambda _: httpx.Response(500)))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://tls"
    ) as c:
        response = await c.post("/queries/v1/query-request")
        assert response.status_code == 200
        assert not response.json()["success"]


QUERY = "/queries/v1/query-request"


def answering(payload, status=200):
    """An upstream that answers every request with one body."""
    return httpx.MockTransport(lambda _: httpx.Response(status, json=payload))


def written(path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


async def post(app, path, **kwargs) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://tls"
    ) as client:
        return await client.post(path, **kwargs)


@pytest.fixture
def log(tmp_path, monkeypatch):
    path = tmp_path / "statements.jsonl"
    monkeypatch.setenv(STATEMENT_LOG_ENV, str(path))
    return path


async def test_an_executed_statement_lands_in_the_log_with_its_four_facts(log):
    """vts, sql, outcome, latency - the four a 9.2.2 row cannot be built without."""
    app = proxy_app("http://fake", answering(
        {"success": True, "data": {"total": 2, "rowset": [[1], [2]]}}))
    response = await post(app, QUERY, content=json.dumps({"sqlText": "select 1"}).encode())
    assert response.json()["success"]

    entries = written(log)
    assert len(entries) == 1
    entry = entries[0]
    assert set(entry) == {"vts", "sql", "rows", "error", "latency_ms"}
    assert entry["sql"] == "select 1"
    assert entry["rows"] == 2 and entry["error"] is None
    # The episode's virtual clock, read the way native.audit reads it - never a
    # wall clock, or the row it becomes is stamped outside the world's time.
    assert entry["vts"] == "2026-03-05T09:00:00Z"
    assert isinstance(entry["latency_ms"], float)


async def test_the_log_reads_a_gzipped_request_body(log):
    """`snowflake-connector-python` compresses what it sends; an unparsed body
    would silently log nothing at all for every real client."""
    app = proxy_app("http://fake", answering({"success": True, "data": {"rowset": []}}))
    body = gzip.compress(json.dumps({"sqlText": "select count(*) from orders"}).encode())
    await post(app, QUERY, content=body, headers={"content-encoding": "gzip"})
    assert [entry["sql"] for entry in written(log)] == ["select count(*) from orders"]
    assert written(log)[0]["rows"] == 0


async def test_a_refused_statement_is_logged_as_an_error_and_not_as_zero_rows(log):
    """A failure that read as `rows: 0` would become a row saying the agent
    successfully read nothing, which is a different fact about the world."""
    app = proxy_app("http://fake", answering(
        {"success": False, "message": "SQL compilation error: ORDERS", "code": "002003"}))
    await post(app, QUERY, content=json.dumps({"sqlText": "select * from orders"}).encode())
    entry = written(log)[0]
    assert entry["rows"] is None
    assert entry["error"] == "SQL compilation error: ORDERS"

    unreachable = proxy_app("http://fake", httpx.MockTransport(
        lambda _: (_ for _ in ()).throw(httpx.ConnectError("refused"))))
    await post(unreachable, QUERY, content=json.dumps({"sqlText": "select 2"}).encode())
    assert written(log)[1]["error"] == "Local warehouse unavailable"


async def test_only_the_route_that_carries_sql_is_logged(log):
    """A row per session heartbeat would put the agent's connection in its own
    action log; logging is the query route and nothing else."""
    app = proxy_app("http://fake", answering({"success": True, "data": {}}))
    await post(app, "/session/v1/login-request", content=b'{"data":{}}')
    await post(app, "/session/heartbeat", content=b"{}")
    await post(app, "/monitoring/queries/telemetry", content=b"{}")
    assert written(log) == []


async def test_nothing_is_written_when_the_variable_is_unset(tmp_path, monkeypatch):
    """Unset is the default everywhere but the native stack, and it has to be
    byte-for-byte the behaviour this module had before the log existed."""
    monkeypatch.delenv(STATEMENT_LOG_ENV, raising=False)
    app = proxy_app("http://fake", answering({"success": True, "data": {"total": 1}}))
    response = await post(app, QUERY, content=json.dumps({"sqlText": "select 1"}).encode())
    assert response.json()["success"]
    assert list(tmp_path.iterdir()) == []
