"""Backoffice Stats & Reporting dashboard (Milestone 8): a per-Event sales/
revenue/scan-in view rendered on top of the JSON API backend-builder
already shipped (``app.api.routes.stats``, ``app.services.stats``). This
module has no aggregation logic of its own — it fetches the Event (for the
breadcrumb/nav) and the stats payload, then hands both to a template,
exactly the same "thin web route, real logic stays in the JSON API" shape
as ``app.web.routes.orders.orders_list``.

Gated by ``require_web_admin`` (never ``require_web_scanner_or_admin``):
revenue/sold/scan figures are financial/business data, same admin-only
scoping as the Orders list page and the underlying JSON API route itself
(``app.api.routes.stats.get_stats`` uses ``require_admin``, not
``require_admin_or_agent``) — a scanner-role session has no business
seeing an event's revenue.

The template's "Download CSV" link points straight at
``GET /api/v1/events/{event_id}/orders/export.csv`` — no web-layer proxy
route for it, same reasoning as the Orders list page's existing "Download
invoice" links (``app.templates.backoffice.orders_list``): a plain GET
with no CSRF-relevant side effect, already authenticated by the admin
session cookie the browser sends directly to the JSON API.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import Response

from app.api.deps import Principal
from app.core.templating import templates
from app.web.api_client import internal_api_client
from app.web.csrf import attach_csrf_cookie, read_or_generate_csrf_token
from app.web.deps import require_web_admin

router = APIRouter(tags=["backoffice-stats"])


def _error_detail(response: Any, fallback: str) -> str:
    try:
        detail = response.json().get("detail", fallback)
    except Exception:  # noqa: BLE001 - response body may not be JSON at all
        return fallback
    return detail if isinstance(detail, str) else fallback


@router.get("/events/{event_id}/stats", response_model=None)
async def event_stats(
    request: Request, event_id: str, principal: Principal = Depends(require_web_admin)
) -> Response:
    """Render the sales/revenue/scan-in dashboard for one Event.

    Fetches the Event and the aggregated stats payload
    (``GET /api/v1/events/{event_id}/stats``) in the same request, mirroring
    ``app.web.routes.orders.orders_list``'s two-call shape. A stats-fetch
    failure degrades to a visible error banner (empty dashboard) rather
    than crashing the whole page — defensive against a future regression
    in that route, not an expected outcome for an admin session that just
    resolved the Event successfully.

    A CSRF token is issued/attached here even though this page's own
    content has no form of its own: ``backoffice/base.html``'s header
    always renders a "Log out" form, which needs one, same as every other
    backoffice page.
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
        stats_error = _error_detail(stats_response, "Could not load stats for this event.")

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
