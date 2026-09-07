"""In-process HTTP client for PUBLIC web routes (``app.web.routes.public_site``)
to call the existing public JSON API (``app.api.routes.public``) without
duplicating its validation/business logic — same in-process ASGI-transport
pattern as ``app.web.api_client.internal_api_client``.

Deliberately a separate, small client rather than reusing
``internal_api_client`` directly: that helper's ``httpx.ASGITransport`` is
built with its default ``client=("127.0.0.1", 123)``, so every call through
it is attributed to the same fake loopback address regardless of who the
real visitor is. That's harmless for the backoffice (a handful of trusted
admins), but would silently defeat ``app.core.rate_limit.checkout_rate_limiter``
for the public checkout endpoint — every buyer's checkout attempt,
regardless of their real IP, would count against one shared bucket keyed to
that fake address, which is both non-compliant with PROJECT_BRIEF.md's "rate
limiting on checkout... endpoints" requirement and a way one misbehaving
client could 429-lock out every other real buyer. This client instead
forwards the incoming public request's real client address into the ASGI
transport's ``client`` tuple, so the checkout route's rate limiter (keyed by
``request.client.host``) sees the real visitor.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import Request


@asynccontextmanager
async def public_api_client(request: Request) -> AsyncIterator[httpx.AsyncClient]:
    """An ``httpx.AsyncClient`` bound to this app's own ASGI app, forwarding
    the real inbound request's client (IP, port) so downstream per-IP rate
    limiting keys off the actual visitor, not this in-process proxy hop.
    """
    from app.main import app as asgi_app

    client_host = request.client.host if request.client else "127.0.0.1"
    client_port = request.client.port if request.client else 0
    transport = httpx.ASGITransport(app=asgi_app, client=(client_host, client_port))
    async with httpx.AsyncClient(transport=transport, base_url="http://internal") as client:
        yield client
