"""In-process HTTP client for backoffice web routes to call the existing
JSON API (``app.api.routes.*``) without duplicating any of its validation,
sanitization, or business logic.

Uses httpx's ASGI transport bound directly to this same app instance — no
real network hop, no extra process — so the page/form routes under
``app.web.routes`` stay thin (render templates, translate HTML form fields
<-> JSON) while every actual rule (hex color validation, CSS sanitization,
AA contrast calculation, audit logging, image validation) continues to
live in exactly one place: ``app.api.routes.themes`` and the services it
calls.

The ``app.main`` import is deferred to call time (not module load time) to
avoid a circular import: ``app.main`` imports and mounts this package's
routers, so importing ``app.main.app`` at module scope here would run
before ``app.main`` has finished defining ``app``.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import Request


@asynccontextmanager
async def internal_api_client(request: Request) -> AsyncIterator[httpx.AsyncClient]:
    """An ``httpx.AsyncClient`` bound to this app's own ASGI app, forwarding
    the incoming request's cookies (the admin session cookie) so the JSON
    API's own auth re-validates the same principal on every call — defense
    in depth, not just a convenience shortcut.
    """
    from app.main import app as asgi_app

    transport = httpx.ASGITransport(app=asgi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://internal", cookies=dict(request.cookies)
    ) as client:
        yield client
