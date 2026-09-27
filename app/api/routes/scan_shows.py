"""Show lookup for the scanner's show picker and scan page. Scanner or admin
only, and read-only — scanners get nothing else content- or payment-related.
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

# Picker window: 60 days ahead keeps the list short for long-lived installs.
# The single-show route deliberately ignores it.
_LOOKAHEAD = timedelta(days=60)
# One day back, so a show being scanned doesn't vanish at midnight.
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
    """Shows in the near-term window, earliest first. Not filtered by publish
    status: draft shows can still be scanned (e.g. test runs).
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
    """One show for the scan page — no date window, so a direct link never 404s for age."""
    parsed_show_id: uuid.UUID = parse_uuid_or_404(show_id, detail="Show not found.")
    result = await session.execute(
        select(Show).where(Show.id == parsed_show_id).options(selectinload(Show.event))
    )
    show = result.scalar_one_or_none()
    if show is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Show not found.")
    return _to_out(show)
