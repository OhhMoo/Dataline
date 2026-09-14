"""Real Postgres and DuckDB through both gateway transports."""

import asyncio
import tempfile
from pathlib import Path

import duckdb
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from alkera_gateway import Gateway, GatewayConfig
from alkera_gateway.audit import read_rows
from alkera_gateway.records import canonical, operation_record
from alkera_gateway.server import http_app

CALLS = [
    ("sql_execute", {"sql": "CREATE TABLE warehouse.connector_check(id INTEGER, n DECIMAL(10,2))"}),
    ("sql_execute", {"sql": "INSERT INTO warehouse.connector_check VALUES (1, 12.50), (2, 5.00)"}),
    ("sql_query", {"sql": "SELECT * FROM warehouse.connector_check ORDER BY id"}),
    (
        "sql_execute",
        {
            "sql": "DELETE FROM warehouse.connector_check WHERE id=2; "
            "INSERT INTO warehouse.connector_check VALUES (3, 7.00)"
        },
    ),
    ("sql_execute", {"sql": "UPDATE warehouse.connector_check SET n=15.25 WHERE id=1"}),
    ("sql_query", {"sql": "SELECT * FROM warehouse.connector_check ORDER BY id", "limit": 1}),
    ("sql_execute", {"sql": "DROP TABLE warehouse.connector_check"}),
]


async def parity(root, backend, transport):
    directory = root / f"{backend}-{transport}"
    directory.mkdir()
    db = directory / "warehouse.duckdb"
    with duckdb.connect(":memory:") as conn:
        conn.execute(f"ATTACH '{db}' AS wh")
        conn.execute("CREATE SCHEMA wh.warehouse")
    config = GatewayConfig(
        workspace=directory / "workspace",
        warehouse=db,
        warehouse_backend=backend,
        audit_dir=directory / "audit",
        initial_world_fp="integration-fixture",
        episode_id="parity",
    )
    gateway = Gateway(config)
    app = http_app(gateway)
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            if transport == "cli":
                for tool, args in CALLS:
                    result = (await client.post("/call", json={"tool": tool, "args": args})).json()
                    assert result["ok"], result
            else:
                async with streamable_http_client("http://test/mcp/", http_client=client) as (
                    r,
                    w,
                    _,
                ):
                    async with ClientSession(r, w) as session:
                        await session.initialize()
                        for tool, args in CALLS:
                            result = await session.call_tool(tool, args)
                            assert not result.isError, result
    finally:
        gateway.close()
    return [canonical(operation_record(row)) for row in read_rows(gateway.audit.actions)]


async def live_surfaces():
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8080", timeout=30) as client:

        async def call(tool, **args):
            response = (await client.post("/call", json={"tool": tool, "args": args})).json()
            assert response["ok"], response
            return response["result"]

        assert (await call("docs_get", path="docs/metrics.md"))["lines"]
        assert (await call("bi_get", dashboard="revenue"))["metrics"]
        before = await call("bi_refresh", dashboard="revenue")
        await call("sql_execute", sql="UPDATE warehouse.items SET amount=amount+1 WHERE id=1")
        assert await call("bi_cached", dashboard="revenue") == before
        after = await call("bi_refresh", dashboard="revenue")
        assert before["panels"] != after["panels"]
        await call("sql_execute", sql="UPDATE warehouse.items SET amount=amount-1 WHERE id=1")
        first = await call(
            "s3_put",
            bucket="landing",
            key="check.bin",
            body_base64="AP+A",
            metadata={"delivery": "1"},
        )
        assert (await call("s3_get", bucket="landing", key="check.bin"))["body_base64"] == "AP+A"
        second = await call("s3_put", bucket="landing", key="check.bin", body_base64="AQID")
        assert first["etag"] != second["etag"]
        assert (await call("s3_list", bucket="landing", prefix="check"))["objects"]
        await call("s3_delete", bucket="landing", key="check.bin")
        assert not (await call("s3_list", bucket="landing", prefix="check"))["objects"]
        bad = await client.post(
            "/call",
            json={
                "tool": "sql_execute",
                "args": {
                    "sql": "UPDATE warehouse.items SET amount=999; "
                    "INSERT INTO missing_table VALUES (1)"
                },
            },
        )
        assert not bad.json()["ok"]
        rows = (await call("sql_query", sql="SELECT amount FROM warehouse.items ORDER BY id"))[
            "rows"
        ]
        assert rows[0][0]["value"] == "10.50", rows
        for sql in ["SELECT pg_read_file('/etc/passwd')", "CREATE EXTENSION dblink"]:
            response = await client.post(
                "/call", json={"tool": "sql_execute", "args": {"sql": sql}}
            )
            assert not response.json()["ok"], response.json()


async def main():
    with tempfile.TemporaryDirectory() as temp:
        records = [
            await parity(Path(temp), backend, transport)
            for backend in ("duckdb", "postgres")
            for transport in ("cli", "mcp")
        ]
        for record in records[1:]:
            assert record == records[0], (records[0], record)
    await live_surfaces()
    print(
        '{"parity":"4 backend/transport combinations, 7 calls each",'
        '"surfaces":"docs, BI staleness, binary S3 overwrite/delete",'
        '"postgres":"atomic rollback and server-file denial passed"}'
    )


if __name__ == "__main__":
    asyncio.run(main())
