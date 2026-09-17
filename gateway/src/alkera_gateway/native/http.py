"""Bounded body parsing for native routes that execute in the worker pool."""

import json
from typing import Annotated

from fastapi import Depends, HTTPException, Request

MAX_BODY_BYTES = 1_048_576


async def request_body(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise HTTPException(413, "Request too large")
        body.extend(chunk)
    return bytes(body)


async def json_body(request: Request) -> dict:
    raw = await request_body(request)
    try:
        body = json.loads(raw) if raw else {}
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Invalid JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected a JSON object")
    return body


RawBody = Annotated[bytes, Depends(request_body)]
JSONBody = Annotated[dict, Depends(json_body)]
