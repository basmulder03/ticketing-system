"""Backoffice audit-log viewer (read-only): filters by actor type/action/
target type, plus keyset ("load older") pagination past the API's per-page
limit — see ``app.api.routes.audit_log`` for why keyset rather than offset.
"""

import json
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from app.api.deps import Principal
from app.core.templating import templates
from app.web.api_client import api_error_detail, internal_api_client
from app.web.csrf import attach_csrf_cookie, read_or_generate_csrf_token
from app.web.deps import require_web_admin

router = APIRouter(tags=["backoffice-audit-log"])

_ALLOWED_LIMITS = (50, 100, 200, 500)
"""Choices for "show N per page" (a plain GET form, no JS)."""

_DEFAULT_LIMIT = 100

# Mirrors app.models.enums.ActorType; the API validates the value.
ACTOR_TYPE_CHOICES = [
    ("human", "Human admin"),
    ("ai_agent", "AI agent"),
    ("system", "Automated system process"),
]


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

    actor_type = request.query_params.get("actor_type", "")
    if actor_type not in {value for value, _ in ACTOR_TYPE_CHOICES}:
        actor_type = ""
    action = request.query_params.get("action", "").strip()
    target_type = request.query_params.get("target_type", "").strip()
    before = request.query_params.get("before", "").strip()

    api_params: dict[str, str] = {"limit": str(limit)}
    if actor_type:
        api_params["actor_type"] = actor_type
    if action:
        api_params["action"] = action
    if target_type:
        api_params["target_type"] = target_type
    if before:
        api_params["before"] = before

    async with internal_api_client(request) as client:
        resp = await client.get("/api/v1/admin/audit-log", params=api_params)

    if resp.status_code == 200:
        page = resp.json()
        entries = [_with_detail_display(entry) for entry in page["entries"]]
        has_more = page["has_more"]
        entries_error = None
    else:
        entries = []
        has_more = False
        entries_error = api_error_detail(resp, "Could not load the audit log.")

    # The "load older" link's cursor is the last (oldest) row on this page;
    # every other filter/limit is carried over unchanged.
    older_query = dict(api_params)
    older_query.pop("before", None)
    if entries:
        older_query["before"] = entries[-1]["id"]
    older_url = f"/audit-log?{urlencode(older_query)}" if has_more else None

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
            "actor_type": actor_type,
            "actor_type_choices": ACTOR_TYPE_CHOICES,
            "action": action,
            "target_type": target_type,
            "is_filtered": bool(actor_type or action or target_type),
            "is_paged": bool(before),
            "has_more": has_more,
            "older_url": older_url,
            "csrf_token": token,
        },
    )
    attach_csrf_cookie(response, token)
    return response
