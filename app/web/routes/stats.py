"""Backoffice stats dashboard for one event (logic lives in the JSON API).
Admin-only: revenue isn't for scanners. The CSV link goes straight to the
JSON API export (a plain GET, authenticated by the session cookie).
"""


from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import Response

from app.api.deps import Principal
from app.core.templating import templates
from app.web.api_client import api_error_detail, internal_api_client
from app.web.csrf import attach_csrf_cookie, read_or_generate_csrf_token
from app.web.deps import require_web_admin

router = APIRouter(tags=["backoffice-stats"])


@router.get("/events/{event_id}/stats", response_model=None)
async def event_stats(
    request: Request, event_id: str, principal: Principal = Depends(require_web_admin)
) -> Response:
    """Render the dashboard. A stats-fetch failure shows an error banner instead
    of crashing. A CSRF token is issued for the header's logout form.
    """
    async with internal_api_client(request) as client:
        event_response = await client.get(f"/api/v1/events/{event_id}")
        if event_response.status_code == 404:
            raise HTTPException(status_code=404, detail="Event not found.")
        event = event_response.json()

        stats_response = await client.get(f"/api/v1/events/{event_id}/stats")

    if stats_response.status_code == 200:
        stats = stats_response.json()
        stats_error = None
    else:
        stats = None
        stats_error = api_error_detail(stats_response, "Could not load stats for this event.")

    token = read_or_generate_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "backoffice/event_stats.html",
        {
            "principal": principal,
            "event": event,
            "stats": stats,
            "stats_error": stats_error,
            "csrf_token": token,
        },
    )
    attach_csrf_cookie(response, token)
    return response
