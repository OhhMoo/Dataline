"""TLS front door for the connector wire protocol, with a fixed local upstream."""

import os

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

WIRE_PATHS = {
    "/session/v1/login-request",
    "/session",
    "/session/heartbeat",
    "/queries/v1/query-request",
    "/queries/v1/abort-request",
    "/monitoring/queries/telemetry",
}


def proxy_app(upstream: str, transport=None):
    async def forward(request: Request):
        if request.url.path not in WIRE_PATHS:
            return JSONResponse({"success": False, "message": "Unsupported wire route"}, 404)
        body = await request.body()
        if len(body) > 2_097_152:
            return JSONResponse({"success": False, "message": "Request too large"}, 413)
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
            return JSONResponse(
                {
                    "success": False,
                    "message": "Local warehouse failed",
                    "code": "390000",
                    "data": {},
                },
                200,
            )
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
