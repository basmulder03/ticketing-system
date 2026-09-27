"""Backoffice audit-log viewer (read-only).

The API only supports a ``limit`` (max 500): no filters or pagination, so
entries beyond the newest 500 can't be shown. Filtering client-side over a
truncated window would be misleading, so this page doesn't pretend to.
"""

import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from app.api.deps import Principal
from app.core.templating import templates
from app.web.api_client import internal_api_client
from app.web.csrf import attach_csrf_cookie, read_or_generate_csrf_token
from app.web.deps import require_web_admin

router = APIRouter(tags=["backoffice-audit-log"])

_ALLOWED_LIMITS = (50, 100, 200, 500)
"""Choices for "show N most recent" (a plain GET form, no JS)."""

_DEFAULT_LIMIT = 100


def _with_detail_display(entry: dict[str, Any]) -> dict[str, Any]:
    """Add a pretty-printed ``detail`` for the template's ``<pre>``. Done in
    Python because this Jinja environment has no ``tojson`` filter; the output is
    still autoescaped (``detail`` can hold user-supplied strings).
    """
    detail = entry.get("detail")
    entry["detail_display"] = json.dumps(detail, indent=2, sort_keys=True) if detail else None
    return entry


@router.get("/audit-log", response_model=None)
async def audit_log_view(request: Request, principal: Principal = Depends(require_web_admin)) -> Response:
    """Newest entries first; an invalid ``limit`` snaps to an allowed option."""
    raw_limit = request.query_params.get("limit", "")
    try:
        limit = int(raw_limit)
    except ValueError:
        limit = _DEFAULT_LIMIT
    if limit not in _ALLOWED_LIMITS:
        limit = _DEFAULT_LIMIT

    async with internal_api_client(request) as client:
        resp = await client.get("/api/v1/admin/audit-log", params={"limit": limit})

    if resp.status_code == 200:
        entries = [_with_detail_display(entry) for entry in resp.json()]
        entries_error = None
    else:
        entries = []
        entries_error = "Could not load the audit log."

    token = read_or_generate_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "backoffice/audit_log.html",
        {
            "principal": principal,
            "entries": entries,
            "entries_error": entries_error,
            "limit": limit,
            "allowed_limits": _ALLOWED_LIMITS,
            "csrf_token": token,
        },
    )
    attach_csrf_cookie(response, token)
    return response
