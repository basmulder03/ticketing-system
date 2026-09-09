"""Backoffice UI for the audit-log viewer (read-only).

Closes the same kind of gap as ``app.web.routes.agent_accounts``:
``app.api.routes.audit_log`` (a single admin-only ``GET`` route) has existed
since the first milestone with no backoffice UI at all — this module is its
thin web-layer proxy, same in-process-``httpx`` pattern as every other
``app.web.routes`` module.

**What the API actually supports (as of this milestone):** a single
``limit`` query parameter, clamped server-side to ``[1, 500]``
(see ``app.api.routes.audit_log.list_audit_log``) — there is no actor/
action/date filter and no offset/cursor pagination. This page is built
around exactly that shape: one "how many recent entries" control, no
filter form for parameters the API doesn't have. Faking client-side
filtering over an already-truncated ``limit``-bounded result set would be
actively misleading (an admin filtering by actor could easily miss entries
that fall outside the fetched window), so this deliberately does not do
that either. **Flagged as a real near-term scaling limitation** for
`backend-builder`: once the audit log has more than ~500 rows of history,
this page structurally cannot show older entries — actor/action/date
filtering and real pagination belong on the API route itself, not
worked around here.
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
"""Options offered by the "show N most recent" control — a subset of the
API's full ``[1, 500]`` clamp range, kept small and fixed rather than a free-
text field so this stays a plain GET-with-query-param form (no client JS)."""

_DEFAULT_LIMIT = 100


def _with_detail_display(entry: dict[str, Any]) -> dict[str, Any]:
    """Add a pretty-printed JSON string for ``entry["detail"]`` (or ``None``)
    for the template to render inside a ``<pre>`` block.

    Formatted here in Python rather than via a Jinja ``tojson`` filter: this
    project's ``app.core.templating.templates`` is a plain
    ``fastapi.templating.Jinja2Templates`` instance with no custom filters
    registered (unlike Flask, which adds ``tojson`` by default), so relying
    on that filter existing would be a silent landmine. The resulting string
    is still passed through Jinja's normal autoescaping when interpolated
    with ``{{ }}`` — necessary since ``detail`` can contain admin/agent-
    supplied strings (e.g. an Order buyer's name in an erase-PII entry).
    """
    detail = entry.get("detail")
    entry["detail_display"] = json.dumps(detail, indent=2, sort_keys=True) if detail else None
    return entry


@router.get("/audit-log", response_model=None)
async def audit_log_view(request: Request, principal: Principal = Depends(require_web_admin)) -> Response:
    """Render the most recent audit-log entries, newest first.

    ``limit`` is read from the query string (a plain GET form control, no
    JS framework) and snapped to the nearest supported option in
    :data:`_ALLOWED_LIMITS` if missing or invalid, rather than passed
    through unvalidated to the API.
    """
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
