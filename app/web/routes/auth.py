"""Backoffice login/logout pages.

Proxies credential checking to the existing JSON ``/api/v1/auth/login`` /
``/api/v1/auth/logout`` routes via ``app.web.api_client`` — password
verification, session-cookie issuance, and audit logging (``admin_user.login``)
all stay defined exactly once in ``app.api.routes.auth``. This module only
renders the HTML form and forwards the ``Set-Cookie``/``Set-Cookie``-clearing
headers from that JSON response onto the browser-facing redirect.
"""

import re
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from app.core.templating import templates
from app.web.api_client import internal_api_client
from app.web.csrf import attach_csrf_cookie, read_or_generate_csrf_token, verify_csrf

router = APIRouter(tags=["backoffice-auth"])

_CONTROL_OR_BACKSLASH = re.compile(r"[\\\t\r\n]")
"""Matches a backslash or an embedded TAB/CR/LF. Browsers normalize
backslashes to forward slashes and strip embedded TAB/CR/LF while parsing a
URL (per the WHATWG URL spec) BEFORE evaluating its scheme/authority — so
``/\\evil.com`` or ``/\\t/evil.com`` become ``//evil.com`` (a protocol-
relative absolute URL) by the time a browser follows the redirect, even
though a same-site-looking prefix check on the raw string would not catch
either. Reject outright rather than trying to "fix up" the value."""


def _safe_next(candidate: str) -> str:
    """Only allow same-site relative redirect targets — never an
    attacker-supplied absolute/protocol-relative URL, and never a value a
    browser could reinterpret into one after its own normalization (see
    :data:`_CONTROL_OR_BACKSLASH`) — open-redirect guard.

    Belt-and-suspenders: a plain prefix check catches the obvious
    ``http://`` / ``//`` cases, and :func:`urlsplit` independently confirms
    the parsed result carries no scheme or network location of its own.
    """
    if _CONTROL_OR_BACKSLASH.search(candidate):
        return "/events"
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/events"
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc:
        return "/events"
    return candidate


@router.get("/login", response_model=None)
async def login_page(request: Request, next: str = "/events") -> Response:
    """Render the login form. Already-authenticated visitors are bounced
    straight to their destination rather than shown the form again (checked
    via the existing ``/api/v1/auth/me`` route, not reimplemented here)."""
    async with internal_api_client(request) as client:
        me_response = await client.get("/api/v1/auth/me")
    if me_response.status_code == 200:
        return RedirectResponse(url=_safe_next(next), status_code=303)

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
    """Validate the CSRF token, then delegate credential checking to the
    JSON login route and forward its session cookie onto the browser."""
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

    redirect = RedirectResponse(url=safe_next, status_code=303)
    for raw_cookie in api_response.headers.get_list("set-cookie"):
        redirect.headers.append("set-cookie", raw_cookie)
    return redirect


@router.post("/logout")
async def logout(request: Request, csrf_token: str = Form(...)) -> RedirectResponse:
    """Clear the admin session via the JSON logout route, then redirect to login."""
    verify_csrf(request, csrf_token)
    async with internal_api_client(request) as client:
        api_response = await client.post("/api/v1/auth/logout")

    redirect = RedirectResponse(url="/login", status_code=303)
    for raw_cookie in api_response.headers.get_list("set-cookie"):
        redirect.headers.append("set-cookie", raw_cookie)
    return redirect
