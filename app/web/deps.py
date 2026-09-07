"""Auth adapter for server-rendered backoffice web routes.

Reuses the exact same admin-session-cookie validation and admin-role gate
already defined in ``app.api.deps`` (``get_current_principal``,
``require_admin``) rather than reimplementing session/role logic — the
only thing this module adds is translating an auth failure into an HTML
redirect to ``/login`` instead of a JSON 401/403, since these are pages a
human loads in a browser, not API calls.
"""

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, get_current_principal, require_admin
from app.db.session import get_session


class WebAuthRequired(Exception):
    """Raised when a backoffice web page is requested without a valid admin
    session. Caught by the exception handler registered in ``app.main``,
    which redirects to ``/login?next=<original path>``."""

    def __init__(self, next_path: str) -> None:
        self.next_path = next_path
        super().__init__("Admin login required.")


async def require_web_admin(request: Request, session: AsyncSession = Depends(get_session)) -> Principal:
    """Dependency for backoffice HTML routes: a valid admin session is required.

    Deliberately excludes agent principals and scanner-role admins, same
    scoping as ``app.api.deps.require_admin`` — the backoffice HTML UI is a
    human-admin surface only (agents only ever use the JSON API/API-key
    path; scanners get their own narrower scanning-app UI in a later
    milestone).
    """
    try:
        principal = await get_current_principal(request, session)
        return await require_admin(principal)
    except HTTPException as exc:
        raise WebAuthRequired(next_path=str(request.url.path)) from exc
