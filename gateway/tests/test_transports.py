import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from alkera_gateway.server import http_app


@pytest.mark.asyncio
async def test_mcp_and_cli_http_share_the_gateway(gateway):
    app = http_app(gateway)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/call", json={"tool": "sql_query", "args": {"sql": "select 1 as value"}}
            )
            assert response.json()["result"]["rows"] == [[1]]
            async with streamable_http_client("http://test/mcp/", http_client=client) as (
                read_stream,
                write_stream,
                _,
            ):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert len(tools.tools) == 17
                    result = await session.call_tool("sql_query", {"sql": "select 2 as value"})
                    assert result.structuredContent["result"]["rows"] == [[2]]
                    assert not result.isError
