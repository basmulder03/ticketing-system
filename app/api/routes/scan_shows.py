"""Scanner-facing Show lookup (Milestone 7 frontend): backs the show-picker
page (``GET /scan``) and threads a Show's parent ``event_id``/name into the
camera-scanning page (``GET /scan/{show_id}``) — see
``app.schemas.scan_shows`` for why this is its own module rather than
reusing ``app.api.routes.shows``' admin/agent-only, event-nested CRUD
routes.

Gated by the existing ``require_scanner_or_admin`` dependency (imported,
never modified here — see ``app.api.deps`` for its docstring) so a
scanner-role human can reach exactly these two read-only routes plus the
actual scan route, and nothing else content- or payment-adjacent.
"""

import uuid
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import Principal, require_scanner_or_admin
from app.api.routes._utils import parse_uuid_or_404
from app.db.session import get_session
from app.models.show import Show
from app.schemas.scan_shows import ScannableShowOut

router = APIRouter(prefix="/api/v1/scan/shows", tags=["scan"])

# How far into the future the show-picker list looks. Scoped to "near-term"
# per this milestone's brief ("reasonable to scope this to upcoming/
# near-term Shows only... your call on the exact filter, but document it")
# rather than listing every Show ever created, which would grow unbounded
# for a long-lived install. 60 days comfortably covers "the run currently
# selling/playing" for this app's target use case (single shows/short runs)
# without staff having to page/search. The single-show detail route below
# (used by the actual scanning page) deliberately does NOT apply this
# window — a show already reachable by a direct link/QR context should
# never 404 just because it fell out of the picker's near-term list.
_LOOKAHEAD = timedelta(days=60)
# A show that started earlier today is still worth showing (doors staff
# often start scanning before midnight rolls over on a show whose ``date``
# is technically "today"); a small one-day grace window avoids a show
# vanishing from the picker moments after midnight while it's still
# actively letting people in.
_LOOKBACK = timedelta(days=1)


def _to_out(show: Show) -> ScannableShowOut:
    return ScannableShowOut(
        id=str(show.id),
        event_id=str(show.event_id),
        event_name=show.event.name,
        date=show.date,
        doors_time=show.doors_time,
        start_time=show.start_time,
        venue_name=show.venue_name,
        status=show.status,
    )


@router.get("")
async def list_scannable_shows(
    principal: Principal = Depends(require_scanner_or_admin),
    session: AsyncSession = Depends(get_session),
) -> list[ScannableShowOut]:
    """List near-term Shows (see :data:`_LOOKAHEAD`/:data:`_LOOKBACK`) a
    scanner/admin principal may pick to start scanning for, earliest first.

    Deliberately not filtered by ``PublishStatus`` — a draft Show (e.g. a
    private/test run, or a show an admin hasn't published sales for yet but
    still wants to door-test scanning against) is just as scannable as a
    published one; "draft" only governs public checkout visibility
    elsewhere, not entry validation.
    """
    today = datetime.now(UTC).date()
    window_start: date = today - _LOOKBACK
    window_end: date = today + _LOOKAHEAD
    result = await session.execute(
        select(Show)
        .where(Show.date >= window_start, Show.date <= window_end)
        .options(selectinload(Show.event))
        .order_by(Show.date, Show.doors_time)
    )
    return [_to_out(show) for show in result.scalars().all()]


@router.get("/{show_id}")
async def get_scannable_show(
    show_id: str,
    principal: Principal = Depends(require_scanner_or_admin),
    session: AsyncSession = Depends(get_session),
) -> ScannableShowOut:
    """Fetch one Show by id for the camera-scanning page — no date-window
    filter (see :func:`list_scannable_shows`'s docstring), 404 for an
    unknown/malformed id, mirroring ``app.api.routes.scan.scan``'s own
    404 handling for the same ``show_id`` path segment."""
    parsed_show_id: uuid.UUID = parse_uuid_or_404(show_id, detail="Show not found.")
    result = await session.execute(
        select(Show).where(Show.id == parsed_show_id).options(selectinload(Show.event))
    )
    show = result.scalar_one_or_none()
    if show is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Show not found.")
    return _to_out(show)
