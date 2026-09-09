"""Backoffice UI for ``AdminUser`` (human backoffice account) management.

Closes the same kind of gap as ``app.web.routes.agent_accounts`` and
``app.web.routes.audit_log``: ``app.api.routes.admin_users`` (create/list/
deactivate/reactivate/reset-password) landed with no backoffice UI at all.
This module is its thin web-layer proxy, same in-process-``httpx`` pattern
as every other ``app.web.routes`` module (``app.web.api_client.
internal_api_client``) — no business logic duplicated here, every safety
property (self-deactivation guard, self-reset current-password requirement)
is enforced by the API and this module just surfaces the result.

**Do not confuse this with agent-account management.** An ``AdminUser`` is a
human who logs in with an email/password (``admin`` or ``scanner`` role); an
``AgentAccount`` (``app.web.routes.agent_accounts``) is a named API key for
an AI agent/tool. They are unrelated account systems with unrelated UIs.

**Self-row UX for deactivation:** rather than render a "Deactivate" button
that would always 409 against the caller's own row (the API's
self-deactivation guard, see ``app.api.routes.admin_users`` module
docstring), the list template simply omits that action for whichever row's
``id`` matches ``principal.id``. This is a UI convenience only — the API
enforces the real guard independently, so this is not a security boundary,
just avoiding a button that can never succeed.

**Reset-password form shape depends on whose row it's on**, mirroring the
API's own distinction (``app.api.routes.admin_users.
reset_admin_user_password``): the form rendered for the logged-in admin's
own row includes a ``current_password`` field; every other row's form omits
it entirely, since the API neither needs nor wants it for an admin-assisted
reset of someone else's account.
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

router = APIRouter(tags=["backoffice-admin-users"])

_ROLE_CHOICES = (("admin", "Admin"), ("scanner", "Scanner"))
"""Options for the create-account role <select> — mirrors ``AdminRole``
(``app.models.enums.AdminRole``) without importing the API-layer enum into
the web layer, matching how other web routes pass template-facing choice
tuples (e.g. ``app.web.routes.shows``'s ``status_choices``)."""


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
    status_code: int = 200,
) -> Response:
    """Fetch the current admin-user list and render the list page.

    Shared by the plain ``GET`` and by every write action below on failure
    (so a validation/conflict error can redirect back here with a flash),
    matching ``app.web.routes.agent_accounts``'s ``_render_list`` shape.
    """
    async with internal_api_client(request) as client:
        accounts_response = await client.get("/api/v1/admin/admin-users")

    if accounts_response.status_code == 200:
        accounts = accounts_response.json()
        accounts_error = None
    else:
        accounts = []
        accounts_error = _error_detail(accounts_response, "Could not load admin users.")

    token = read_or_generate_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "backoffice/admin_users_list.html",
        {
            "principal": principal,
            "accounts": accounts,
            "accounts_error": accounts_error,
            "role_choices": _ROLE_CHOICES,
            "csrf_token": token,
            "flash": request.query_params.get("flash"),
            "flash_kind": request.query_params.get("flash_kind", "success"),
        },
        status_code=status_code,
    )
    attach_csrf_cookie(response, token)
    return response


@router.get("/admin-users", response_model=None)
async def admin_users_list(request: Request, principal: Principal = Depends(require_web_admin)) -> Response:
    """List every backoffice ``AdminUser`` account (never includes a
    password hash — see ``app.api.routes.admin_users.list_admin_users``)."""
    return await _render_list(request, principal)


@router.post("/admin-users", response_model=None)
async def create_admin_user_web(
    request: Request,
    principal: Principal = Depends(require_web_admin),
    csrf_token: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    role: str = Form("admin"),
) -> Response:
    """Create a new ``AdminUser`` account.

    Surfaces the API's 409 (duplicate email) as a clean flash message rather
    than a raw 500 — see ``app.api.routes.admin_users.create_admin_user``'s
    docstring for why that route returns 409 in the first place. Email/
    password are otherwise forwarded as-is; lowercasing and hashing both
    happen server-side in the API, not duplicated here.
    """
    verify_csrf(request, csrf_token)
    clean_email = email.strip()
    if not clean_email:
        return redirect_with_flash("/admin-users", "An email address is required.", kind="error")

    async with internal_api_client(request) as client:
        resp = await client.post(
            "/api/v1/admin/admin-users",
            json={"email": clean_email, "password": password, "role": role},
        )

    if resp.status_code == 409:
        return redirect_with_flash(
            "/admin-users", _error_detail(resp, "An admin user with this email already exists."), kind="error"
        )
    if resp.status_code >= 400:
        return redirect_with_flash(
            "/admin-users", _error_detail(resp, "Could not create the admin user."), kind="error"
        )

    created = resp.json()
    return redirect_with_flash("/admin-users", f"Admin user “{created['email']}” was created.", kind="success")


@router.post("/admin-users/{admin_user_id}/deactivate")
async def deactivate_admin_user_web(
    request: Request,
    admin_user_id: str,
    principal: Principal = Depends(require_web_admin),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    """Proxy to ``POST /api/v1/admin/admin-users/{id}/deactivate``.

    The list template never renders this action for the caller's own row
    (see this module's docstring), so the API's self-deactivation 409
    should not normally be reachable here — but it is still surfaced as a
    flash rather than left to bubble up as an unhandled error, in case this
    is ever hit directly (e.g. a stale page, a second tab).
    """
    verify_csrf(request, csrf_token)
    redirect_path = "/admin-users"

    async with internal_api_client(request) as client:
        resp = await client.post(f"/api/v1/admin/admin-users/{admin_user_id}/deactivate")

    if resp.status_code == 404:
        return redirect_with_flash(redirect_path, "Admin user not found.", kind="error")
    if resp.status_code >= 400:
        return redirect_with_flash(
            redirect_path, _error_detail(resp, "Could not deactivate this admin user."), kind="error"
        )

    return redirect_with_flash(redirect_path, "Admin user deactivated.", kind="success")


@router.post("/admin-users/{admin_user_id}/reactivate")
async def reactivate_admin_user_web(
    request: Request,
    admin_user_id: str,
    principal: Principal = Depends(require_web_admin),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    """Proxy to ``POST /api/v1/admin/admin-users/{id}/reactivate``. No
    self/last-admin restriction applies (see the API route's docstring), so
    this action is rendered for every row, including the caller's own."""
    verify_csrf(request, csrf_token)
    redirect_path = "/admin-users"

    async with internal_api_client(request) as client:
        resp = await client.post(f"/api/v1/admin/admin-users/{admin_user_id}/reactivate")

    if resp.status_code == 404:
        return redirect_with_flash(redirect_path, "Admin user not found.", kind="error")
    if resp.status_code >= 400:
        return redirect_with_flash(
            redirect_path, _error_detail(resp, "Could not reactivate this admin user."), kind="error"
        )

    return redirect_with_flash(redirect_path, "Admin user reactivated.", kind="success")


@router.post("/admin-users/{admin_user_id}/reset-password", response_model=None)
async def reset_admin_user_password_web(
    request: Request,
    admin_user_id: str,
    principal: Principal = Depends(require_web_admin),
    csrf_token: str = Form(...),
    new_password: str = Form(...),
    current_password: str = Form(""),
) -> Response:
    """Proxy to ``POST /api/v1/admin/admin-users/{id}/reset-password``.

    ``current_password`` is submitted as an empty string (rather than
    omitted) by the "reset someone else's password" form variant, since
    that form doesn't render the field at all — normalized to ``None``
    here before forwarding, matching what the API's schema expects for a
    non-self reset (see ``app.schemas.admin_user.
    AdminUserResetPasswordRequest``). The API independently decides whether
    a current password was actually required (self-reset only) and returns
    401 if it was required but missing/wrong; that 401 is surfaced as a
    flash here rather than a raw error page.
    """
    verify_csrf(request, csrf_token)
    redirect_path = "/admin-users"

    payload: dict[str, str] = {"new_password": new_password}
    if current_password:
        payload["current_password"] = current_password

    async with internal_api_client(request) as client:
        resp = await client.post(
            f"/api/v1/admin/admin-users/{admin_user_id}/reset-password", json=payload
        )

    if resp.status_code == 404:
        return redirect_with_flash(redirect_path, "Admin user not found.", kind="error")
    if resp.status_code == 401:
        return redirect_with_flash(
            redirect_path,
            _error_detail(resp, "Current password is required and must be correct to reset your own password."),
            kind="error",
        )
    if resp.status_code >= 400:
        return redirect_with_flash(
            redirect_path, _error_detail(resp, "Could not reset this admin user's password."), kind="error"
        )

    return redirect_with_flash(redirect_path, "Password reset.", kind="success")
