import argparse
import asyncio

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .records import canonical


async def run(url: str):
    async with streamable_http_client(url) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            result = await session.call_tool("sql_query", {"sql": "select 1 as value"})
            if result.isError or result.structuredContent["result"]["rows"] != [[1]]:
                raise RuntimeError("MCP SQL smoke call failed")
            return {"ok": True, "tools": len(tools.tools)}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="alkera-mcp-smoke")
    parser.add_argument("url", nargs="?", default="http://127.0.0.1:8080/mcp/")
    args = parser.parse_args(argv)
    print(canonical(asyncio.run(run(args.url))).decode("utf-8"))


if __name__ == "__main__":
    main()
