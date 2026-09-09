"""Admin-only Stats & Reporting routes (Milestone 8): a per-Event sales/
revenue/scan-in dashboard and a CSV export for accounting, per
PROJECT_BRIEF.md's Stats & Reporting section.

Admin-only (``require_admin``, not ``require_admin_or_agent``) throughout,
matching every other financial route in this app (``app.api.routes.orders``,
in particular ``list_orders``/``download_invoice_pdf``): sales figures and
per-order financial detail are financial/business data, not the
"content-type data" (events, shows, ticket types, theme fields, email
template content) the brief scopes agent keys to.

Nested under ``/api/v1/events/{event_id}/...``, mirroring
``app.api.routes.orders.list_router``'s convention for Event-scoped
backoffice data views.
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
    """Parse and resolve ``event_id``, 404ing (never 400ing on a malformed
    id) if it doesn't name a real Event — same convention as every other
    Event-scoped route in this app (see ``app.api.routes._utils.
    parse_uuid_or_404``)."""
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
    """Sales-per-show/ticket-type, online-vs-door revenue split, gross
    revenue, and scan-in-rate dashboard for one Event.

    See ``app.services.stats.get_event_stats``/module docstring for the
    exact aggregation queries and the two judgment calls behind this
    response's numbers: which ``OrderStatus`` values count as settled
    "revenue" (only ``paid``), and why there is no net-of-fees figure at
    all (gross revenue only — a deliberate, documented scope reduction,
    not a missing feature).
    """
    event = await _get_event_or_404(session, event_id)
    return await get_event_stats(session, event_id=event.id)


@router.get("/orders/export.csv")
async def export_orders_csv(
    event_id: str,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Download a CSV of every Order under this Event, one row per Order,
    for accounting reconciliation against bank/Mollie statements.

    Unfiltered by status (see ``app.services.stats.
    get_event_orders_for_export`` docstring) — every Order is included, and
    the ``status`` column lets the accountant filter out anything
    non-settled themselves. Encoded UTF-8 with a BOM (``utf-8-sig``) rather
    than plain UTF-8: buyer names/addresses may contain non-ASCII
    characters (this app supports Dutch out of the box), and Excel on
    Windows — the realistic tool an accountant opens this file with — only
    reliably auto-detects UTF-8 without a manual import step when a BOM is
    present.
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
