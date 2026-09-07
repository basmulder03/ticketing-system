"""Minimal backoffice events index page.

Full Event CRUD UI is not this milestone's scope (Milestone 1.5 is Theme
editing only) — this list exists so there is somewhere to navigate from
after logging in, and a place to click into an event's theme editor. It
reads the existing ``GET /api/v1/events`` JSON route via the in-process
client, nothing more.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from app.api.deps import Principal
from app.core.templating import templates
from app.web.api_client import internal_api_client
from app.web.csrf import attach_csrf_cookie, read_or_generate_csrf_token
from app.web.deps import require_web_admin

router = APIRouter(tags=["backoffice-events"])


@router.get("/")
async def index() -> RedirectResponse:
    return RedirectResponse(url="/events", status_code=303)


@router.get("/events", response_model=None)
async def events_list(request: Request, principal: Principal = Depends(require_web_admin)) -> Response:
    """List all events with a link into each one's theme editor."""
    async with internal_api_client(request) as client:
        events_response = await client.get("/api/v1/events")
    events = events_response.json()

    token = read_or_generate_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "backoffice/events_list.html",
        {
            "principal": principal,
            "events": events,
            "csrf_token": token,
            "flash": request.query_params.get("flash"),
            "flash_kind": request.query_params.get("flash_kind", "success"),
        },
    )
    attach_csrf_cookie(response, token)
    return response
