"""Web-route auth: reuses ``app.api.deps`` but turns auth failures into a
redirect to ``/login`` instead of a JSON 401/403.
"""

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, get_current_principal, require_admin, require_scanner_or_admin
from app.db.session import get_session


class WebAuthRequired(Exception):
    """No valid session; ``app.main`` redirects to ``/login?next=<path>``."""

    def __init__(self, next_path: str) -> None:
        self.next_path = next_path
        super().__init__("Admin login required.")


async def require_web_admin(request: Request, session: AsyncSession = Depends(get_session)) -> Principal:
    """Admin-role session required (no agents, no scanners)."""
    try:
        principal = await get_current_principal(request, session)
        return await require_admin(principal)
    except HTTPException as exc:
        raise WebAuthRequired(next_path=str(request.url.path)) from exc


async def require_web_scanner_or_admin(
    request: Request, session: AsyncSession = Depends(get_session)
) -> Principal:
    """Admin or scanner session (never agents). Only for the scanner pages
    (``/scan``, ``/scan/<show_id>``); everything else uses :func:`require_web_admin`.
    """
    try:
        principal = await get_current_principal(request, session)
        return await require_scanner_or_admin(principal)
    except HTTPException as exc:
        raise WebAuthRequired(next_path=str(request.url.path)) from exc
