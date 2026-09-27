"""Backoffice agent API-key management (create/list/revoke).

Creation breaks the usual POST → redirect-with-flash pattern on purpose: the
raw key would then travel in a URL (access logs, history, ``Referer``). So
the page renders directly from the POST response, with the key only in the
template context. Reloading resubmits the form and hits a 409, so the key is
truly never shown again.
"""


from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from app.api.deps import Principal
from app.core.templating import templates
from app.web.api_client import api_error_detail, internal_api_client
from app.web.csrf import attach_csrf_cookie, read_or_generate_csrf_token, verify_csrf
from app.web.deps import require_web_admin
from app.web.flash import redirect_with_flash

router = APIRouter(tags=["backoffice-agent-accounts"])


async def _render_list(
    request: Request,
    principal: Principal,
    *,
    new_agent_key: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    """Render the list page (used by the GET and by a successful create)."""
    async with internal_api_client(request) as client:
        accounts_response = await client.get("/api/v1/admin/agent-accounts")

    if accounts_response.status_code == 200:
        accounts = accounts_response.json()
        accounts_error = None
    else:
        accounts = []
        accounts_error = api_error_detail(accounts_response, "Could not load agent accounts.")

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
    """All agent accounts (never raw keys)."""
    return await _render_list(request, principal)


@router.post("/agent-accounts", response_model=None)
async def create_agent_account_web(
    request: Request,
    principal: Principal = Depends(require_web_admin),
    csrf_token: str = Form(...),
    name: str = Form(...),
) -> Response:
    """Create an agent and show its key once, rendered directly — never via a URL."""
    verify_csrf(request, csrf_token)
    clean_name = name.strip()
    if not clean_name:
        return redirect_with_flash("/agent-accounts", "A name is required.", kind="error")

    async with internal_api_client(request) as client:
        resp = await client.post("/api/v1/admin/agent-accounts", json={"name": clean_name})

    if resp.status_code == 409:
        return redirect_with_flash(
            "/agent-accounts",
            api_error_detail(resp, "An agent account with this name already exists."),
            kind="error",
        )
    if resp.status_code >= 400:
        return redirect_with_flash(
            "/agent-accounts", api_error_detail(resp, "Could not create the agent account."), kind="error"
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
    """Revoke a key. Idempotent; only an unknown account is an error."""
    verify_csrf(request, csrf_token)
    redirect_path = "/agent-accounts"

    async with internal_api_client(request) as client:
        resp = await client.post(f"/api/v1/admin/agent-accounts/{agent_id}/revoke")

    if resp.status_code == 404:
        return redirect_with_flash(redirect_path, "Agent account not found.", kind="error")
    if resp.status_code >= 400:
        return redirect_with_flash(
            redirect_path, api_error_detail(resp, "Could not revoke this agent account."), kind="error"
        )

    return redirect_with_flash(redirect_path, "Agent account revoked.", kind="success")
