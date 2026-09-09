"""Backoffice UI for agent API-key account management (create/list/revoke).

Closes a genuine functional gap: ``app.api.routes.agent_accounts`` (create/
list/revoke agent accounts) has existed since the very first milestone with
zero backoffice UI, so the only way to hand an AI agent/tool a scoped API
key was a raw ``curl`` call. This module is the thin web-layer proxy to that
JSON API — same in-process-``httpx`` pattern as every other ``app.web.routes``
module (``app.web.api_client.internal_api_client``), no business logic
duplicated here.

**One-time-key-reveal design (the one place this module deliberately breaks
from the rest of the backoffice's "POST -> redirect-with-flash -> GET" flow):**
``POST /api/v1/admin/agent-accounts`` returns the raw API key exactly once,
in its JSON response body — it is never stored (only ``AgentAccount.key_hash``
is persisted, see that model's docstring) and structurally cannot be shown
again. A redirect-then-flash round trip (the pattern every other backoffice
write action uses, see ``app.web.flash.redirect_with_flash``) would force
that secret through a URL query string, where it would land in server access
logs, browser history, and the ``Referer`` header of any link the flash-laden
page happens to render — an unacceptable leak of a live credential purely for
UI consistency. So :func:`create_agent_account_web` renders the list template
directly from the POST response instead of redirecting, passing the freshly
minted key through the template context only. A page reload after creation
re-submits the same form (the browser's normal "resubmit form?" prompt) and
hits the API's own name-uniqueness conflict (409) rather than ever
re-displaying the key — so "you cannot see this again" is actually true, not
just a UI suggestion. Validation/conflict errors (empty name, duplicate name)
carry no secret and use the normal redirect-with-flash pattern like every
other route here.
"""

from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from app.api.deps import Principal
from app.core.templating import templates
from app.web.api_client import internal_api_client
from app.web.csrf import attach_csrf_cookie, read_or_generate_csrf_token, verify_csrf
from app.web.deps import require_web_admin
from app.web.flash import redirect_with_flash

router = APIRouter(tags=["backoffice-agent-accounts"])


def _error_detail(response: Any, fallback: str) -> str:
    """Best-effort extraction of a JSON API error's ``detail`` string,
    falling back to a generic message if the body isn't the expected shape
    (mirrors ``app.web.routes.orders._error_detail`` exactly)."""
    try:
        detail = response.json().get("detail", fallback)
    except Exception:  # noqa: BLE001 - response body may not be JSON at all
        return fallback
    return detail if isinstance(detail, str) else fallback


async def _render_list(
    request: Request,
    principal: Principal,
    *,
    new_agent_key: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    """Fetch the current agent-account list and render the list page.

    Shared by the plain ``GET`` and by :func:`create_agent_account_web`'s
    direct (non-redirect) render on success — see this module's docstring
    for why creation can't use the usual redirect-with-flash pattern.
    """
    async with internal_api_client(request) as client:
        accounts_response = await client.get("/api/v1/admin/agent-accounts")

    if accounts_response.status_code == 200:
        accounts = accounts_response.json()
        accounts_error = None
    else:
        accounts = []
        accounts_error = _error_detail(accounts_response, "Could not load agent accounts.")

    token = read_or_generate_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "backoffice/agent_accounts_list.html",
        {
            "principal": principal,
            "accounts": accounts,
            "accounts_error": accounts_error,
            "new_agent_key": new_agent_key,
            "csrf_token": token,
            "flash": request.query_params.get("flash"),
            "flash_kind": request.query_params.get("flash_kind", "success"),
        },
        status_code=status_code,
    )
    attach_csrf_cookie(response, token)
    return response


@router.get("/agent-accounts", response_model=None)
async def agent_accounts_list(request: Request, principal: Principal = Depends(require_web_admin)) -> Response:
    """List every agent account (never includes a raw API key — see
    ``app.api.routes.agent_accounts.list_agent_accounts``)."""
    return await _render_list(request, principal)


@router.post("/agent-accounts", response_model=None)
async def create_agent_account_web(
    request: Request,
    principal: Principal = Depends(require_web_admin),
    csrf_token: str = Form(...),
    name: str = Form(...),
) -> Response:
    """Create a new agent account and reveal its API key exactly once.

    See this module's docstring for why this renders the list page directly
    (with the new key in context) instead of the usual redirect-with-flash —
    the short version: the key must never pass through a URL.
    """
    verify_csrf(request, csrf_token)
    clean_name = name.strip()
    if not clean_name:
        return redirect_with_flash("/agent-accounts", "A name is required.", kind="error")

    async with internal_api_client(request) as client:
        resp = await client.post("/api/v1/admin/agent-accounts", json={"name": clean_name})

    if resp.status_code == 409:
        return redirect_with_flash(
            "/agent-accounts",
            _error_detail(resp, "An agent account with this name already exists."),
            kind="error",
        )
    if resp.status_code >= 400:
        return redirect_with_flash(
            "/agent-accounts", _error_detail(resp, "Could not create the agent account."), kind="error"
        )

    created = resp.json()
    return await _render_list(
        request,
        principal,
        new_agent_key={
            "name": created["name"],
            "key_prefix": created["key_prefix"],
            "api_key": created["api_key"],
        },
        status_code=201,
    )


@router.post("/agent-accounts/{agent_id}/revoke")
async def revoke_agent_account_web(
    request: Request,
    agent_id: str,
    principal: Principal = Depends(require_web_admin),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    """Proxy to ``POST /api/v1/admin/agent-accounts/{agent_id}/revoke``.

    Idempotent, same as the API route it wraps: revoking an already-revoked
    account is a no-op rather than an error, so this always redirects with a
    success flash unless the account genuinely doesn't exist.
    """
    verify_csrf(request, csrf_token)
    redirect_path = "/agent-accounts"

    async with internal_api_client(request) as client:
        resp = await client.post(f"/api/v1/admin/agent-accounts/{agent_id}/revoke")

    if resp.status_code == 404:
        return redirect_with_flash(redirect_path, "Agent account not found.", kind="error")
    if resp.status_code >= 400:
        return redirect_with_flash(
            redirect_path, _error_detail(resp, "Could not revoke this agent account."), kind="error"
        )

    return redirect_with_flash(redirect_path, "Agent account revoked.", kind="success")
