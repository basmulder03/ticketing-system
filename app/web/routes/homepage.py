"""The public site root (``/``).

Redirects to the default event while it's set and published; otherwise shows
a directory of published events with a link to the admin sign-in.
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
