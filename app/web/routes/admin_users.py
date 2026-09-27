"""Backoffice management of human accounts (admin/scanner). Not agents — see
``agent_accounts``.

The API enforces the safety rules; the UI just avoids dead ends: no
"Deactivate" button on your own row, and the reset form asks for your
current password only on your own row.
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

router = APIRouter(tags=["backoffice-admin-users"])

_ROLE_CHOICES = (("admin", "Admin"), ("scanner", "Scanner"))
"""Role options for the create form (mirrors ``AdminRole``)."""


async def _render_list(
    request: Request,
    principal: Principal,
    *,
    status_code: int = 200,
) -> Response:
    """Fetch accounts and render the list page."""
    async with internal_api_client(request) as client:
        accounts_response = await client.get("/api/v1/admin/admin-users")

    if accounts_response.status_code == 200:
        accounts = accounts_response.json()
        accounts_error = None
    else:
        accounts = []
        accounts_error = api_error_detail(accounts_response, "Could not load admin users.")

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
    """All accounts (never password hashes)."""
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
    """Create an account; a duplicate email flashes the API's 409."""
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
            "/admin-users", api_error_detail(resp, "An admin user with this email already exists."), kind="error"
        )
    if resp.status_code >= 400:
        return redirect_with_flash(
            "/admin-users", api_error_detail(resp, "Could not create the admin user."), kind="error"
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
    """Deactivate an account; the API's self-deactivation 409 is flashed if reached (e.g. a stale tab)."""
    verify_csrf(request, csrf_token)
    redirect_path = "/admin-users"

    async with internal_api_client(request) as client:
        resp = await client.post(f"/api/v1/admin/admin-users/{admin_user_id}/deactivate")

    if resp.status_code == 404:
        return redirect_with_flash(redirect_path, "Admin user not found.", kind="error")
    if resp.status_code >= 400:
        return redirect_with_flash(
            redirect_path, api_error_detail(resp, "Could not deactivate this admin user."), kind="error"
        )

    return redirect_with_flash(redirect_path, "Admin user deactivated.", kind="success")


@router.post("/admin-users/{admin_user_id}/reactivate")
async def reactivate_admin_user_web(
    request: Request,
    admin_user_id: str,
    principal: Principal = Depends(require_web_admin),
    csrf_token: str = Form(...),
) -> RedirectResponse:
    """Reactivate an account (shown on every row, including your own)."""
    verify_csrf(request, csrf_token)
    redirect_path = "/admin-users"

    async with internal_api_client(request) as client:
        resp = await client.post(f"/api/v1/admin/admin-users/{admin_user_id}/reactivate")

    if resp.status_code == 404:
        return redirect_with_flash(redirect_path, "Admin user not found.", kind="error")
    if resp.status_code >= 400:
        return redirect_with_flash(
            redirect_path, api_error_detail(resp, "Could not reactivate this admin user."), kind="error"
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
    """Reset a password. An empty ``current_password`` (other-user form) becomes
    ``None``; the API decides whether it was required and 401s if needed.
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
            api_error_detail(resp, "Current password is required and must be correct to reset your own password."),
            kind="error",
        )
    if resp.status_code >= 400:
        return redirect_with_flash(
            redirect_path, api_error_detail(resp, "Could not reset this admin user's password."), kind="error"
        )

    return redirect_with_flash(redirect_path, "Password reset.", kind="success")
