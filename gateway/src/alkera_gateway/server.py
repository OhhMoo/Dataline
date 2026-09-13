from contextlib import asynccontextmanager

import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from .records import canonical
from .tools import TOOLS


def mcp_server(gateway):
    server = Server("alkera-gateway")

    @server.list_tools()
    async def list_tools():
        return [
            types.Tool(name=name, description=description, inputSchema=model.model_json_schema())
            for name, (model, description) in TOOLS.items()
        ]

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict):
        result = await anyio.to_thread.run_sync(gateway.call, name, arguments, "mcp")
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=canonical(result).decode("utf-8"))],
            structuredContent=result,
            isError=not result["ok"],
        )

    return server


def http_app(gateway):
    manager = StreamableHTTPSessionManager(
        mcp_server(gateway),
        json_response=True,
        stateless=True,
        max_request_body_size=gateway.config.max_payload_bytes + 4096,
    )

    @asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield

    async def call(request: Request):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > gateway.config.max_payload_bytes + 4096:
                return JSONResponse({"error": "request_too_large"}, status_code=413)
        import json

        try:
            payload = json.loads(data)
            if not isinstance(payload, dict) or set(payload) != {"tool", "args"}:
                raise ValueError("Expected tool and args")
            if not isinstance(payload["tool"], str) or not isinstance(payload["args"], dict):
                raise ValueError("Expected a tool name and an argument object")
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        result = await anyio.to_thread.run_sync(
            gateway.call, payload["tool"], payload["args"], "cli"
        )
        return JSONResponse(result)

    async def health(request):
        return JSONResponse({"ok": gateway.healthy, "tools": len(TOOLS)})

    return Starlette(
        lifespan=lifespan,
        routes=[
            Route("/call", call, methods=["POST"]),
            Route("/health", health),
            Mount("/mcp", app=manager.handle_request),
        ],
    )


async def serve_stdio(gateway):
    server = mcp_server(gateway)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
