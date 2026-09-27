"""Backoffice login/logout and first-run setup pages.

Credential checks, cookie issuance and auditing happen in the JSON API
(``app.api.routes.auth``); these routes render the forms and forward the
``Set-Cookie`` headers. ``/login`` redirects to ``/setup`` while no admin
exists, and a scanner's default landing page is ``/scan``.
"""

import re
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from app.core.templating import templates
from app.web.api_client import api_error_detail, internal_api_client
from app.web.csrf import attach_csrf_cookie, read_or_generate_csrf_token, verify_csrf

router = APIRouter(tags=["backoffice-auth"])

_CONTROL_OR_BACKSLASH = re.compile(r"[\\\t\r\n]")
"""Backslashes and TAB/CR/LF. Browsers normalize these before parsing, so
``/\\evil.com`` becomes ``//evil.com`` — reject rather than try to fix up.
"""


def _safe_next(candidate: str) -> str:
    """Open-redirect guard: allow only same-site relative paths, falling back to
    ``/events``. A prefix check plus ``urlsplit`` both have to agree.
    """
    if _CONTROL_OR_BACKSLASH.search(candidate):
        return "/events"
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/events"
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc:
        return "/events"
    return candidate


def _apply_role_default(safe_next: str, role: str | None) -> str:
    """Send scanners to ``/scan`` instead of the admin-only ``/events`` default.

    ``"/events"`` doubles as the "no ``next`` given" sentinel; treating an
    explicit ``next=/events`` the same is fine, since it's useless for scanners.
    """
    return "/scan" if safe_next == "/events" and role == "scanner" else safe_next


@router.get("/login", response_model=None)
async def login_page(request: Request, next: str = "/events") -> Response:
    """Render the login form. Already logged in → redirect onward; no admin yet →
    redirect to ``/setup``.
    """
    async with internal_api_client(request) as client:
        me_response = await client.get("/api/v1/auth/me")
    if me_response.status_code == 200:
        role = me_response.json().get("role")
        return RedirectResponse(url=_apply_role_default(_safe_next(next), role), status_code=303)

    async with internal_api_client(request) as client:
        setup_required_response = await client.get("/api/v1/auth/setup-required")
    if setup_required_response.json().get("setup_required"):
        return RedirectResponse(url="/setup", status_code=303)

    token = read_or_generate_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "backoffice/login.html",
        {"error": None, "next": _safe_next(next), "csrf_token": token},
    )
    attach_csrf_cookie(response, token)
    return response


@router.post("/login", response_model=None)
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
    next: str = Form("/events"),
) -> Response:
    """Check CSRF, log in via the JSON API, forward its session cookie."""
    verify_csrf(request, csrf_token)
    safe_next = _safe_next(next)

    async with internal_api_client(request) as client:
        api_response = await client.post("/api/v1/auth/login", json={"email": email, "password": password})

    if api_response.status_code != 200:
        token = read_or_generate_csrf_token(request)
        response = templates.TemplateResponse(
            request,
            "backoffice/login.html",
            {"error": "Invalid email or password.", "next": safe_next, "csrf_token": token},
            status_code=401,
        )
        attach_csrf_cookie(response, token)
        return response

    role = api_response.json().get("role")
    redirect = RedirectResponse(url=_apply_role_default(safe_next, role), status_code=303)
    for raw_cookie in api_response.headers.get_list("set-cookie"):
        redirect.headers.append("set-cookie", raw_cookie)
    return redirect


@router.post("/logout")
async def logout(request: Request, csrf_token: str = Form(...)) -> RedirectResponse:
    """Log out via the JSON API, then redirect to ``/login``."""
    verify_csrf(request, csrf_token)
    async with internal_api_client(request) as client:
        api_response = await client.post("/api/v1/auth/logout")

    redirect = RedirectResponse(url="/login", status_code=303)
    for raw_cookie in api_response.headers.get_list("set-cookie"):
        redirect.headers.append("set-cookie", raw_cookie)
    return redirect


@router.get("/setup", response_model=None)
async def setup_page(request: Request) -> Response:
    """First-run admin form; redirects to ``/login`` once any admin exists."""
    async with internal_api_client(request) as client:
        setup_required_response = await client.get("/api/v1/auth/setup-required")
    if not setup_required_response.json().get("setup_required"):
        return RedirectResponse(url="/login", status_code=303)

    token = read_or_generate_csrf_token(request)
    response = templates.TemplateResponse(
        request, "backoffice/setup.html", {"error": None, "csrf_token": token}
    )
    attach_csrf_cookie(response, token)
    return response


@router.post("/setup", response_model=None)
async def setup_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    csrf_token: str = Form(...),
) -> Response:
    """Check CSRF and that the passwords match (a typo here would lock out the
    deployer), create the admin via the JSON API, forward its session cookie.
    """
    verify_csrf(request, csrf_token)

    if password != confirm_password:
        token = read_or_generate_csrf_token(request)
        response = templates.TemplateResponse(
            request,
            "backoffice/setup.html",
            {"error": "Passwords do not match.", "csrf_token": token},
            status_code=422,
        )
        attach_csrf_cookie(response, token)
        return response

    async with internal_api_client(request) as client:
        api_response = await client.post("/api/v1/auth/setup", json={"email": email, "password": password})

    if api_response.status_code != 201:
        token = read_or_generate_csrf_token(request)
        response = templates.TemplateResponse(
            request,
            "backoffice/setup.html",
            {"error": api_error_detail(api_response, "Could not create the admin account."), "csrf_token": token},
            status_code=422,
        )
        attach_csrf_cookie(response, token)
        return response

    redirect = RedirectResponse(url="/events", status_code=303)
    for raw_cookie in api_response.headers.get_list("set-cookie"):
        redirect.headers.append("set-cookie", raw_cookie)
    return redirect
