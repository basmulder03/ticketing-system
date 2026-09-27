"""Backoffice login/logout pages.

Proxies credential checking to the existing JSON ``/api/v1/auth/login`` /
``/api/v1/auth/logout`` routes via ``app.web.api_client`` — password
verification, session-cookie issuance, and audit logging (``admin_user.login``)
all stay defined exactly once in ``app.api.routes.auth``. This module only
renders the HTML form and forwards the ``Set-Cookie``/``Set-Cookie``-clearing
headers from that JSON response onto the browser-facing redirect.

Milestone 7 adds :func:`_apply_role_default`: a ``scanner``-role session
landing on this module's own ``"/events"`` default is retargeted to the
show-picker (``/scan``) instead, since ``/events`` is admin-only.

Post-launch fix adds :func:`setup_page`/:func:`setup_submit`: the initial-
admin-account creation page, proxying to ``POST /api/v1/auth/setup`` the
same way ``login_submit`` proxies to ``/login`` — see
``app.api.routes.auth`` module docstring for why this exists. ``login_page``
also redirects here automatically while no ``AdminUser`` exists yet, so a
fresh deployment's very first visitor to ``/login`` lands on account
creation instead of a login form for an account that doesn't exist.
"""

import re
from urllib.parse import urlsplit

import httpx
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


def _apply_role_default(safe_next: str, role: str | None) -> str:
    """``/events`` is unreachable for a ``scanner``-role session
    (``require_web_admin`` excludes that role — see ``app.web.deps``), so a
    scanner landing there via this module's default would immediately
    bounce straight back through the login redirect. Retarget the
    show-picker page (``/scan``, Milestone 7) instead, but ONLY when
    ``safe_next`` is still the unmodified ``"/events"`` default.

    This reuses ``"/events"`` itself as the "no explicit ``next`` was
    requested" sentinel, matching this module's existing (pre-Milestone 7)
    behavior: ``login_page`` already seeds the login form's hidden ``next``
    field with the literal string ``"/events"`` whenever no ``?next=``
    query param was present, so by the time a value reaches here there is
    no way to distinguish "no next was given" from "next=/events was
    explicitly given" anyway — treating both the same is safe (and
    correct) here specifically because ``/events`` is never a *useful*
    explicit destination for a scanner account regardless of intent, unlike
    an arbitrary same-site path a caller might genuinely want preserved.
    """
    return "/scan" if safe_next == "/events" and role == "scanner" else safe_next


@router.get("/login", response_model=None)
async def login_page(request: Request, next: str = "/events") -> Response:
    """Render the login form. Already-authenticated visitors are bounced
    straight to their destination rather than shown the form again (checked
    via the existing ``/api/v1/auth/me`` route, not reimplemented here).

    While this deployment has no ``AdminUser`` at all yet, redirects to
    ``/setup`` instead — there is nothing to log in as, so showing a login
    form here would just be a dead end (see this module's docstring)."""
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

    role = api_response.json().get("role")
    redirect = RedirectResponse(url=_apply_role_default(safe_next, role), status_code=303)
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


def _setup_error_detail(response: httpx.Response) -> str:
    """Same convention as every other web-route module's copy of this
    helper (``app.web.routes.shows``, ``.events``, ``.orders``): a 422
    from Pydantic's own request-body validation returns ``detail`` as a
    list of error objects, not a string — fall back to a generic message
    rather than rendering a Python list repr on the setup form."""
    try:
        detail = response.json().get("detail", "Could not create the admin account.")
    except Exception:  # noqa: BLE001 - response body may not be JSON at all
        return "Could not create the admin account."
    return detail if isinstance(detail, str) else "Could not create the admin account."


@router.get("/setup", response_model=None)
async def setup_page(request: Request) -> Response:
    """Render the initial-admin-account creation form — post-launch fix,
    see this module's docstring. Permanently redirects to ``/login``
    once any ``AdminUser`` exists (this is a one-time page, not a general
    "create an admin" form — that lives in the authenticated backoffice,
    ``app.web.routes.admin_users``)."""
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
    """Validate the CSRF token and that both password fields match, then
    delegate account creation to the JSON setup route and forward its
    session cookie onto the browser — same shape as :func:`login_submit`.

    The ``password != confirm_password`` check is purely a web-form UX
    nicety (catching a typo before it locks the deployer out of the
    account they just created) — the JSON API itself takes no
    ``confirm_password`` field at all, since a raw API caller has no
    "confirm" field to duplicate."""
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
            {"error": _setup_error_detail(api_response), "csrf_token": token},
            status_code=422,
        )
        attach_csrf_cookie(response, token)
        return response

    redirect = RedirectResponse(url="/events", status_code=303)
    for raw_cookie in api_response.headers.get_list("set-cookie"):
        redirect.headers.append("set-cookie", raw_cookie)
    return redirect
