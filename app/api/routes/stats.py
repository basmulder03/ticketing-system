"""Stats dashboard and accounting CSV for one event. Admin-only, like all
financial routes.
"""

import csv
import io

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, require_admin
from app.api.routes._utils import parse_uuid_or_404
from app.db.session import get_session
from app.models.event import Event
from app.schemas.stats import EventStatsOut
from app.services.stats import (
    CSV_COLUMNS,
    build_order_export_rows,
    get_event_orders_for_export,
    get_event_stats,
)

router = APIRouter(prefix="/api/v1/events/{event_id}", tags=["stats"])


async def _get_event_or_404(session: AsyncSession, event_id: str) -> Event:
    """Resolve ``event_id`` or 404."""
    parsed_id = parse_uuid_or_404(event_id, detail="Event not found.")
    event = await session.get(Event, parsed_id)
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found.")
    return event


@router.get("/stats")
async def get_stats(
    event_id: str,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> EventStatsOut:
    """Dashboard payload; see ``app.services.stats`` for what counts as revenue."""
    event = await _get_event_or_404(session, event_id)
    return await get_event_stats(session, event_id=event.id)


@router.get("/orders/export.csv")
async def export_orders_csv(
    event_id: str,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """CSV of every order (unfiltered; accountants filter on ``status``). Written as
    ``utf-8-sig`` so Excel on Windows detects the encoding of non-ASCII names.
    """
    event = await _get_event_or_404(session, event_id)
    orders = await get_event_orders_for_export(session, event_id=event.id)
    rows = build_order_export_rows(orders)

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(CSV_COLUMNS))
    writer.writeheader()
    writer.writerows(rows)
    csv_bytes = buffer.getvalue().encode("utf-8-sig")

    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="orders-{event.slug}.csv"'},
    )
