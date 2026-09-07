"""Minimal double-submit-cookie CSRF protection for backoffice HTML forms.

Per PROJECT_BRIEF.md's Security & Ops section ("CSRF protection on all
backoffice forms"). No CSRF mechanism existed anywhere in the codebase
before this milestone added the first HTML forms — the JSON API under
``app.api.routes`` has none either (it currently relies on the admin
session cookie alone). This is a first pass scoped to the server-rendered
forms added in ``app.web.routes.*``; flagged for `security-reviewer` to
audit, and to weigh in on whether the JSON API itself also needs a matching
guard for non-browser callers that could otherwise be tricked into
cross-site form posts against it directly.

Pattern: a random token is stored in an httponly cookie. Every page that
renders a form reads (or creates) that token and embeds it as a hidden
form field. On POST, the submitted field must match the cookie — a
cross-site form post can't read/set the victim's cookie, so it can't
produce a matching pair.
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
