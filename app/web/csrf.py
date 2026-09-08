"""Minimal double-submit-cookie CSRF protection for backoffice HTML forms.

Per PROJECT_BRIEF.md's Security & Ops section ("CSRF protection on all
backoffice forms"). This module protects the server-rendered forms under
``app.web.routes.*`` — those genuinely submit as
``application/x-www-form-urlencoded``/``multipart/form-data`` from a
browser, which a malicious cross-site page could replicate without
needing JavaScript, so an explicit anti-CSRF token is required there.

**Decision (Milestone 4 security review): the JSON API under
``app.api.routes`` does NOT need this same mechanism**, and deliberately
doesn't have one. Reasoning, verified rather than assumed:
1. Every write route requires a Pydantic-modeled JSON body
   (``Content-Type: application/json``, real JSON structure) — a plain
   HTML form (the classic CSRF vector) can only submit
   ``application/x-www-form-urlencoded``/``multipart/form-data``, which
   FastAPI's body parsing rejects before any handler code runs. A pure
   HTML-form CSRF attack is structurally impossible against these routes.
2. The remaining vector — a cross-site page using ``fetch()``/XHR with
   ``credentials: "include"`` to send real JSON — doesn't work either:
   the admin session cookie is ``SameSite=Lax`` (``app/api/routes/auth.py``),
   and Lax cookies are withheld from cross-site subresource requests
   (fetch/XHR/iframe/JS-driven form submission), attaching only to
   top-level navigation. Such a request would carry no session cookie at
   all and get rejected as unauthenticated before reaching any handler.
Together these are a complete, not incidental, defense for a JSON-only
API — this is the same reasoning already applied to the public checkout
endpoint's no-CSRF decision (Milestone 2 review), extended here to the
authenticated JSON surface. Revisit this decision only if an
``api/routes`` endpoint is ever added that accepts a non-JSON body
reachable by a simple cross-site request (form-encoded, no
``Content-Type`` restriction), which would reopen vector 1.

Pattern used here (web forms only): a random token is stored in an
httponly cookie. Every page that renders a form reads (or creates) that
token and embeds it as a hidden form field. On POST, the submitted field
must match the cookie — a cross-site form post can't read/set the
victim's cookie, so it can't produce a matching pair.
"""

import hmac
import secrets

from fastapi import HTTPException, Request, Response, status

from app.core.config import get_settings

CSRF_COOKIE_NAME = "beacon_csrf"


def read_or_generate_csrf_token(request: Request) -> str:
    """Return the CSRF token for this browser session, generating a fresh
    one (not yet persisted — see :func:`attach_csrf_cookie`) if none is set
    yet. Split from cookie-setting so callers can embed the value in a
    server-rendered form before the response object exists."""
    existing = request.cookies.get(CSRF_COOKIE_NAME)
    if existing:
        return existing
    return secrets.token_urlsafe(32)


def attach_csrf_cookie(response: Response, token: str) -> None:
    """Set the CSRF cookie on an outgoing response, mirroring the admin
    session cookie's security flags (httponly, samesite=lax, secure outside
    development) — see ``app.api.routes.auth.login``."""
    settings = get_settings()
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=settings.app_env != "development",
    )


def verify_csrf(request: Request, submitted_token: str) -> None:
    """Raise 403 unless ``submitted_token`` matches this request's CSRF cookie."""
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    if not cookie_token or not submitted_token or not hmac.compare_digest(cookie_token, submitted_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid or missing CSRF token.")
