"""In-process client for public web routes to call the public JSON API.

Separate from ``internal_api_client`` because it forwards the visitor's real
IP: otherwise every buyer would share one rate-limit bucket, and one bad
client could lock everyone out of checkout.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import Request


@asynccontextmanager
async def public_api_client(request: Request) -> AsyncIterator[httpx.AsyncClient]:
    """Client bound to this app, forwarding the visitor's real address for rate limiting."""
    from app.main import app as asgi_app

    client_host = request.client.host if request.client else "127.0.0.1"
    client_port = request.client.port if request.client else 0
    transport = httpx.ASGITransport(app=asgi_app, client=(client_host, client_port))
    async with httpx.AsyncClient(transport=transport, base_url="http://internal") as client:
        yield client
