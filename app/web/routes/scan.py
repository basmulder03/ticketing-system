"""Scanner pages: the show picker (``/scan``) and camera page (``/scan/<id>``).

Scanner or admin sessions. Uses its own minimal templates (not the backoffice
layout) for one-handed use. Show data comes from the scanner-reachable
``/api/v1/scan/shows`` routes. The scan itself is posted by ``static/scan.js``
straight to the JSON API (a JSON body, so no CSRF token needed).
"""


from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import Response

from app.api.deps import Principal
from app.core.templating import templates
from app.web.api_client import api_error_detail, internal_api_client
from app.web.csrf import attach_csrf_cookie, read_or_generate_csrf_token
from app.web.deps import require_web_scanner_or_admin

router = APIRouter(tags=["scan-app"])


@router.get("/scan", response_model=None)
async def scan_show_picker(
    request: Request, principal: Principal = Depends(require_web_scanner_or_admin)
) -> Response:
    """Near-term shows to pick from before scanning."""
    async with internal_api_client(request) as client:
        shows_response = await client.get("/api/v1/scan/shows")

    if shows_response.status_code == 200:
        shows = shows_response.json()
        shows_error = None
    else:
        shows = []
        shows_error = api_error_detail(shows_response, "Could not load shows.")

    # For the header's logout form only.
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
    """The camera page for one show; 404 for an unknown show. Always issues a
    CSRF token: ``scan.js`` builds an inline mark-as-paid form for admins on an
    unpaid result, and it needs one.
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
