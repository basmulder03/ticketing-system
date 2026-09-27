"""Double-submit-cookie CSRF protection for backoffice HTML forms.

A random token lives in an httponly cookie and is echoed as a hidden form
field; a cross-site post can't read the cookie, so it can't match.

The JSON API deliberately doesn't use this. Its write routes only accept a
JSON body, which a plain cross-site form can't send; and the session cookie
is ``SameSite=Lax``, so a cross-site ``fetch()`` arrives unauthenticated.
Revisit if an API route ever accepts form-encoded bodies.
"""

import hmac
import secrets

from fastapi import HTTPException, Request, Response, status

from app.core.config import get_settings

CSRF_COOKIE_NAME = "beacon_csrf"


def read_or_generate_csrf_token(request: Request) -> str:
    """The session's CSRF token, or a new one (saved later by
    :func:`attach_csrf_cookie`), so a form can embed it before the response exists.
    """
    existing = request.cookies.get(CSRF_COOKIE_NAME)
    if existing:
        return existing
    return secrets.token_urlsafe(32)


def attach_csrf_cookie(response: Response, token: str) -> None:
    """Set the CSRF cookie with the same flags as the session cookie."""
    settings = get_settings()
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=settings.app_env != "development",
    )


def verify_csrf(request: Request, submitted_token: str) -> None:
    """403 unless the submitted token matches the cookie."""
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    if not cookie_token or not submitted_token or not hmac.compare_digest(cookie_token, submitted_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid or missing CSRF token.")
