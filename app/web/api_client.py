"""In-process client for web routes to call the JSON API, so validation and
business rules live only in ``app.api.routes``.

``app.main`` is imported at call time: importing it at module scope would be
circular, since ``app.main`` mounts these web routers.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import Request


@asynccontextmanager
async def internal_api_client(request: Request) -> AsyncIterator[httpx.AsyncClient]:
    """Client bound to this app, forwarding the caller's cookies so the API
    re-checks the same principal on every call."""
    from app.main import app as asgi_app

    transport = httpx.ASGITransport(app=asgi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://internal", cookies=dict(request.cookies)
    ) as client:
        yield client


def api_error_detail(response: httpx.Response, fallback: str) -> str:
    """A readable message from a JSON API error response.

    Handles a plain-string ``detail`` and Pydantic's 422 list of field
    errors; anything else (non-JSON, unexpected shape) returns ``fallback``.
    """
    try:
        payload = response.json()
    except ValueError:
        return fallback
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        messages = []
        for err in detail:
            if not isinstance(err, dict):
                continue
            field = ".".join(str(part) for part in err.get("loc", []) if part != "body")
            msg = err.get("msg") or "Invalid value."
            messages.append(f"{field}: {msg}" if field else msg)
        if messages:
            return "; ".join(messages)
    return fallback
