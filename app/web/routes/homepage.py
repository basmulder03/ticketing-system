"""The public site root (``GET /``) — post-launch fix, per the user's
NOTES: "There should be a homepage on the route, where different things
can happen, and event can be set to the default event, which causes that
event page to automagically open to the correct page, or show an overview
of the app, what it can do, which events are currently able to have shows
booked on... From there make it possible to go to the sign-in screen."

Before this, ``/`` unconditionally redirected straight into the
backoffice (``app.web.routes.events.index``, now removed) — every visitor,
buyer or admin, landed on an admin-only page and, if unauthenticated, was
immediately bounced to ``/login``. There was no public-facing entry point
at all. This module replaces that with a real one:

- If an Event is both marked ``is_default_event`` AND ``published`` (see
  ``app.models.event.Event.is_default_event``), a visitor to ``/`` is
  redirected straight to that Event's own page — the "automagically
  open" behavior the NOTES describe.
- Otherwise, renders a plain directory of every published Event plus a
  link to the admin sign-in page (``/login``) — same proxy-to-the-
  existing-JSON-API shape as every other public-site page (see
  ``app.web.routes.public_site``'s own module docstring), backed by
  ``GET /api/v1/public/homepage`` (``app.api.routes.public.
  get_public_homepage``).
"""

from fastapi import APIRouter, Request
from starlette.responses import RedirectResponse, Response

from app.core.public_templating import public_templates
from app.web.public_api_client import public_api_client
from app.web.public_context import resolve_locale

router = APIRouter(tags=["homepage"])


@router.get("/", response_model=None)
async def homepage(request: Request) -> Response:
    locale = resolve_locale(request)
    async with public_api_client(request) as client:
        response = await client.get("/api/v1/public/homepage")
    body = response.json()

    if body["default_event_slug"]:
        return RedirectResponse(url=f"/e/{body['default_event_slug']}", status_code=303)

    return public_templates.TemplateResponse(
        request,
        "public/homepage.html",
        {"locale": locale, "events": body["events"]},
    )
