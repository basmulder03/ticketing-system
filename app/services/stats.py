"""Stats & Reporting aggregation (Milestone 8), per PROJECT_BRIEF.md's Stats
& Reporting section: "sales per show/ticket type, revenue split online vs.
door, gross vs. net of fees, scan-in rate" plus a CSV export for accounting.

Two independent judgment calls are baked into this module — read both
before changing any query here:

1. **Which ``OrderStatus`` values count as "revenue"?** Only ``PAID``.
   ``PENDING``/``PENDING_DOOR`` represent a buyer's intent (and, for
   ``pending``/``pending_door``, currently-held stock — see
   ``app.models.enums.OrderStatus`` docstring) but no money has actually
   changed hands yet: a Mollie payment can still fail/expire, and a
   door-payment reservation can simply never show up. ``CANCELLED``/
   ``EXPIRED`` obviously never settled either. Counting anything but
   ``PAID`` as "revenue" would make the dashboard's headline number
   overstate real income and disagree with what actually lands in the
   event organizer's bank/Mollie account — so :data:`_REVENUE_STATUSES`
   below is deliberately just ``(OrderStatus.PAID,)``, used by
   :func:`_revenue_by_payment_method` (which backs both
   ``EventStatsOut.revenue_by_payment_method`` and
   ``EventStatsOut.revenue_total``) and by the CSV export's ``status``
   column (which is included as-is, unfiltered, so an accountant can see
   and filter out non-settled orders themselves — see
   :func:`get_event_orders_for_export`).

   This is DELIBERATELY a different population from the "sales per
   show/ticket type" figures in :func:`get_event_stats`'s per-``TicketType``
   breakdown, which per the brief's own instruction reuse
   ``app.services.stock.sold_counts_for_ticket_types``'s existing "live"
   counting rule (every non-``cancelled``/non-``expired`` Order, i.e.
   INCLUDING still-``pending``/``pending_door`` ones) so that number stays
   consistent with what the rest of the backoffice already calls "sold"
   (e.g. ``TicketType.remaining``). The two figures will not sum to the
   same total while an Event has pending orders in flight, and that's
   intentional, not a bug — see ``app.schemas.stats.TicketTypeStatsOut``
   docstring for the same note surfaced on the API contract itself.

2. **Gross revenue only — no net-of-fees anywhere.** Mollie's Payment
   resource has no per-payment fee field reachable via a simple per-event
   API key (the credential model this entire app is built around, see
   ``app.models.event_config.EventConfig``); real fee data only lives
   behind Mollie's Balance Transactions API, which needs an OAuth/
   advanced-access token this app has no concept of. Building that
   credential model is out of scope for this milestone (a deliberate,
   user-approved scope reduction). So this module never computes, guesses,
   or calls out to Mollie for a fee figure — ``revenue`` throughout this
   module and its schemas IS gross revenue, full stop.
"""

import uuid
from decimal import Decimal
from typing import TypedDict

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import OrderStatus, PaymentMethod
from app.models.order import Order
from app.models.show import Show
from app.models.ticket import Ticket
from app.schemas.stats import (
    EventStatsOut,
    PaymentMethodRevenueOut,
    ShowStatsOut,
    TicketTypeStatsOut,
)
from app.services.stock import sold_counts_for_ticket_types

__all__ = [
    "CSV_COLUMNS",
    "OrderExportRow",
    "build_order_export_rows",
    "get_event_orders_for_export",
    "get_event_stats",
]

_REVENUE_STATUSES: tuple[OrderStatus, ...] = (OrderStatus.PAID,)
"""The only ``OrderStatus`` values that represent settled money — see this
module's docstring, point 1, for the full reasoning."""

_RELEASED_STATUSES: tuple[OrderStatus, ...] = (OrderStatus.CANCELLED, OrderStatus.EXPIRED)
"""Mirrors ``app.services.stock``'s own (module-private) constant of the
same name/value: statuses whose Tickets no longer hold stock and should not
be counted as "sold"/"scanned" either. Kept as a local copy rather than
importing that module's underscore-prefixed constant across module
boundaries — it's small, stable (tied 1:1 to ``OrderStatus``'s own
documented stock-release semantics), and re-deriving it here avoids
reaching into another module's private name."""


async def _scanned_counts_for_ticket_types(
    session: AsyncSession, ticket_type_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Count ``Ticket`` rows with ``scanned_at IS NOT NULL`` per
    ``TicketType`` id, restricted to tickets still held by a live
    (non-cancelled/non-expired) Order — mirrors
    ``app.services.stock.sold_counts_for_ticket_types``'s query shape
    exactly, just with the extra ``scanned_at`` filter.

    The live-order restriction is belt-and-suspenders rather than
    load-bearing in practice: ``app.services.scan`` only ever sets
    ``scanned_at`` for a ticket on a ``paid`` Order (see
    ``app.models.enums.ScanOutcome.UNPAID``), and nothing in this app
    transitions a ``paid`` Order back to ``cancelled``/``expired``, so a
    scanned ticket on a released Order should not occur — but the filter
    costs nothing and keeps this function correct even if that invariant
    ever changes.
    """
    if not ticket_type_ids:
        return {}
    stmt = (
        select(Ticket.ticket_type_id, func.count(Ticket.id))
        .join(Order, Order.id == Ticket.order_id)
        .where(Ticket.ticket_type_id.in_(ticket_type_ids))
        .where(Order.status.notin_(_RELEASED_STATUSES))
        .where(Ticket.scanned_at.isnot(None))
        .group_by(Ticket.ticket_type_id)
    )
    result = await session.execute(stmt)
    return {row[0]: row[1] for row in result.all()}


async def _revenue_by_payment_method(session: AsyncSession, event_id: uuid.UUID) -> list[PaymentMethodRevenueOut]:
    """Sum settled (``paid``-order) revenue and order count per
    ``PaymentMethod`` for one Event.

    Always returns one entry per :class:`~app.models.enums.PaymentMethod`
    value, in enum declaration order, even for a method with zero paid
    orders (revenue ``0.00``, count ``0``) — see
    ``app.schemas.stats.PaymentMethodRevenueOut`` docstring for why.
    """
    stmt = (
        select(Order.payment_method, func.count(Order.id), func.coalesce(func.sum(Order.total), 0))
        .where(Order.event_id == event_id)
        .where(Order.status.in_(_REVENUE_STATUSES))
        .group_by(Order.payment_method)
    )
    result = await session.execute(stmt)
    totals: dict[PaymentMethod, tuple[int, Decimal]] = {
        row[0]: (row[1], Decimal(row[2])) for row in result.all()
    }
    return [
        PaymentMethodRevenueOut(
            payment_method=method,
            order_count=totals.get(method, (0, Decimal("0.00")))[0],
            revenue=totals.get(method, (0, Decimal("0.00")))[1],
        )
        for method in PaymentMethod
    ]


async def get_event_stats(session: AsyncSession, *, event_id: uuid.UUID) -> EventStatsOut:
    """Build the full Milestone 8 dashboard payload for one Event: sales per
    Show/TicketType, the online-vs-door settled-revenue split, gross total
    revenue, and sold-vs-scanned (scan-in) figures.

    Does not itself verify the Event exists — callers (the route layer)
    are expected to have already 404'd on a missing Event, matching every
    other Event-scoped service function in this app. An Event with no Shows
    yields an empty ``shows`` list and all-zero totals, not an error.
    """
    result = await session.execute(
        select(Show).where(Show.event_id == event_id).options(selectinload(Show.ticket_types)).order_by(Show.date)
    )
    shows = list(result.scalars().unique().all())

    ticket_type_ids = [ticket_type.id for show in shows for ticket_type in show.ticket_types]
    sold_counts = await sold_counts_for_ticket_types(session, ticket_type_ids)
    scanned_counts = await _scanned_counts_for_ticket_types(session, ticket_type_ids)

    show_stats: list[ShowStatsOut] = []
    for show in shows:
        ticket_type_stats: list[TicketTypeStatsOut] = []
        for ticket_type in show.ticket_types:
            sold = sold_counts.get(ticket_type.id, 0)
            scanned = scanned_counts.get(ticket_type.id, 0)
            ticket_type_stats.append(
                TicketTypeStatsOut(
                    id=str(ticket_type.id),
                    name=ticket_type.name,
                    sold=sold,
                    revenue=ticket_type.price * sold,
                    scanned=scanned,
                )
            )
        show_stats.append(
            ShowStatsOut(
                id=str(show.id),
                date=show.date,
                venue_name=show.venue_name,
                ticket_types=ticket_type_stats,
                sold_total=sum(item.sold for item in ticket_type_stats),
                scanned_total=sum(item.scanned for item in ticket_type_stats),
            )
        )

    revenue_by_method = await _revenue_by_payment_method(session, event_id)
    revenue_total = sum((entry.revenue for entry in revenue_by_method), Decimal("0.00"))

    return EventStatsOut(
        event_id=str(event_id),
        shows=show_stats,
        revenue_total=revenue_total,
        revenue_by_payment_method=revenue_by_method,
        sold_total=sum(show.sold_total for show in show_stats),
        scanned_total=sum(show.scanned_total for show in show_stats),
    )


class OrderExportRow(TypedDict):
    """One CSV row of the accounting export — one row per ``Order``, the
    granularity an accountant reconciling against bank/Mollie statements
    actually needs (matching how ``mollie_payment_id`` is the exact key
    Mollie's own dashboard/settlement reports use). Never float, never
    rounded — ``total`` is the decimal-string of ``Order.total`` exactly as
    stored, mirroring how ``app.models.invoice.Invoice.line_items`` stores
    money as decimal strings rather than a numeric JSON type."""

    order_id: str
    created_at: str
    status: str
    payment_method: str
    mollie_payment_id: str
    invoice_number: str
    buyer_name: str
    buyer_email: str
    ticket_types: str
    total: str


CSV_COLUMNS: tuple[str, ...] = (
    "order_id",
    "created_at",
    "status",
    "payment_method",
    "mollie_payment_id",
    "invoice_number",
    "buyer_name",
    "buyer_email",
    "ticket_types",
    "total",
)
"""Column order for the accounting CSV export — also the ``fieldnames``
passed to ``csv.DictWriter`` by ``app.api.routes.stats.export_orders_csv``,
kept here (next to :class:`OrderExportRow`) rather than re-derived from the
TypedDict at the route layer so the header order is explicit and stable."""


def _ticket_type_summary(tickets: list[Ticket]) -> str:
    """Render an Order's tickets as a single human-readable cell, e.g.
    ``"Adult x2; Child x1"`` — grouped by TicketType name, in first-seen
    order. Requires each ``Ticket.ticket_type`` to already be loaded (see
    :func:`get_event_orders_for_export`'s eager-load options)."""
    counts: dict[str, int] = {}
    for ticket in tickets:
        name = ticket.ticket_type.name
        counts[name] = counts.get(name, 0) + 1
    return "; ".join(f"{name} x{quantity}" for name, quantity in counts.items())


def build_order_export_rows(orders: list[Order]) -> list[OrderExportRow]:
    """Pure (no DB access) shaping of already-loaded ``Order`` rows into
    :class:`OrderExportRow` dicts, ready to hand to ``csv.DictWriter``.

    Kept separate from :func:`get_event_orders_for_export` so the row
    shaping itself is trivially unit-testable without a database — the
    only DB-dependent step is the fetch.
    """
    rows: list[OrderExportRow] = []
    for order in orders:
        rows.append(
            OrderExportRow(
                order_id=str(order.id),
                created_at=order.created_at.isoformat(),
                status=order.status.value,
                payment_method=order.payment_method.value,
                mollie_payment_id=order.mollie_payment_id or "",
                invoice_number=order.invoice.formatted_number if order.invoice is not None else "",
                buyer_name=order.buyer_name,
                buyer_email=order.buyer_email,
                ticket_types=_ticket_type_summary(order.tickets),
                total=str(order.total),
            )
        )
    return rows


async def get_event_orders_for_export(session: AsyncSession, *, event_id: uuid.UUID) -> list[Order]:
    """Fetch every Order under ``event_id``, most recent first, with the
    relationships :func:`build_order_export_rows` needs eagerly loaded
    (``tickets.ticket_type``, ``invoice``).

    Deliberately unfiltered by status (every Order is included, exactly
    matching ``app.api.routes.orders.list_orders``'s own "no
    filtering/sorting" choice) — the CSV's ``status`` column lets an
    accountant filter out non-settled orders themselves rather than this
    export silently hiding rows, which would make it a worse reconciliation
    tool, not a better one.
    """
    result = await session.execute(
        select(Order)
        .where(Order.event_id == event_id)
        .options(selectinload(Order.tickets).selectinload(Ticket.ticket_type), selectinload(Order.invoice))
        .order_by(Order.created_at.desc())
    )
    return list(result.scalars().unique().all())
