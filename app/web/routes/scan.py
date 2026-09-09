"""Scanning-app HTML pages (Milestone 7): the show-picker (``GET /scan``)
and the actual camera-scanning page (``GET /scan/{show_id}``), the frontend
half of the door-scanning feature whose backend (``app.services.scan``,
``app.api.routes.scan``) was built directly against ``ScanOutcome``.

Gated by ``require_web_scanner_or_admin`` (``app.web.deps``) rather than
``require_web_admin`` — the whole point of this module is to give
``scanner``-role sessions (which every other backoffice HTML page
deliberately excludes) somewhere to go. Deliberately its own minimal
template family (``templates/scan/*.html``, its own ``scan/base.html``),
NOT extending ``backoffice/base.html`` — per this milestone's brief
("minimal chrome... one-handed use"), dragging in the full backoffice nav/
header would work against that, and a scanner-role session can't use most
of that nav anyway (it 403s on nearly everything else in the backoffice).

Both routes read their Show data from the new, scanner-reachable
``GET /api/v1/scan/shows`` / ``GET /api/v1/scan/shows/{show_id}`` routes
(``app.api.routes.scan_shows``) — NOT the existing admin/agent-only
``app.api.routes.shows`` routes, which a scanner-role session cannot call
(``require_admin_or_agent`` excludes scanner-role humans entirely; see that
dependency's docstring). The actual scan-a-QR-code POST is issued directly
from the browser (client-side JS in ``static/scan.js``) straight to
``POST /api/v1/shows/{show_id}/scan`` — not proxied through this module —
since that call carries no form body this app needs to translate (a bare
JSON ``{"token": ...}``) and the admin session cookie already authenticates
it directly, same-origin, with no CSRF token needed (see
``app.web.csrf``'s module docstring for why the JSON API doesn't use the
double-submit-cookie scheme the HTML forms in this module do).
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import Response

from app.api.deps import Principal
from app.core.templating import templates
from app.web.api_client import internal_api_client
from app.web.csrf import attach_csrf_cookie, read_or_generate_csrf_token
from app.web.deps import require_web_scanner_or_admin

router = APIRouter(tags=["scan-app"])


def _error_detail(response: Any, fallback: str) -> str:
    try:
        detail = response.json().get("detail", fallback)
    except Exception:  # noqa: BLE001 - response body may not be JSON at all
        return fallback
    return detail if isinstance(detail, str) else fallback


@router.get("/scan", response_model=None)
async def scan_show_picker(
    request: Request, principal: Principal = Depends(require_web_scanner_or_admin)
) -> Response:
    """List near-term Shows (see ``app.api.routes.scan_shows.
    list_scannable_shows`` for the exact window) for a scanner/admin to
    pick from before scanning starts — this is what makes "correct show/
    date" a real, staff-driven choice rather than something inferred."""
    async with internal_api_client(request) as client:
        shows_response = await client.get("/api/v1/scan/shows")

    if shows_response.status_code == 200:
        shows = shows_response.json()
        shows_error = None
    else:
        shows = []
        shows_error = _error_detail(shows_response, "Could not load shows.")

    # Issued here purely to back the shared header partial's "Log out" form
    # (see templates/scan/_header.html) -- this page has no other form.
    token = read_or_generate_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "scan/picker.html",
        {"principal": principal, "shows": shows, "shows_error": shows_error, "csrf_token": token},
    )
    attach_csrf_cookie(response, token)
    return response


@router.get("/scan/{show_id}", response_model=None)
async def scan_page(
    request: Request, show_id: str, principal: Principal = Depends(require_web_scanner_or_admin)
) -> Response:
    """The camera-scanning page for one specific Show.

    404s for an unknown/malformed ``show_id``, mirroring
    ``app.api.routes.scan.scan``'s own 404 handling for the same path
    segment (via ``app.api.routes.scan_shows.get_scannable_show``, which
    itself mirrors that same 404 shape) — a scanner should never be able to
    reach a scan screen bound to a show that doesn't exist.

    A CSRF token is issued here even though this page's own GET does
    nothing with one: the inline mark-as-paid form rendered by
    ``scan/scan.html`` for ADMIN-role viewers on an ``unpaid`` result
    (built client-side once a scan response comes back — see
    ``static/scan.js``) needs a valid token to embed, and generating it
    lazily inside that client-side code would mean either a second round
    trip or duplicating ``read_or_generate_csrf_token``'s cookie logic in
    JS. Issuing/attaching it unconditionally here (same as every other
    backoffice page that renders any form) is simpler and has no
    downside — a scanner-role viewer's page also carries the token but
    never gets a form that uses it (see ``scan/scan.html``).
    """
    async with internal_api_client(request) as client:
        show_response = await client.get(f"/api/v1/scan/shows/{show_id}")

    if show_response.status_code == 404:
        raise HTTPException(status_code=404, detail="Show not found.")
    if show_response.status_code != 200:
        raise HTTPException(status_code=502, detail="Could not load this show.")

    token = read_or_generate_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "scan/scan.html",
        {
            "principal": principal,
            "show": show_response.json(),
            "csrf_token": token,
        },
    )
    attach_csrf_cookie(response, token)
    return response
