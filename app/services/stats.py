"""Stats dashboard aggregation and the accounting CSV export.

- Revenue counts only ``PAID`` orders (pending payments can still fail).
  Per-ticket-type "sold" uses the stock rule instead (includes pending), so
  the two figures differ while orders are pending — by design.
- Revenue is gross only: Mollie's fee data needs an OAuth API this app's
  per-event keys can't reach.
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
"""Statuses that represent settled money."""

_RELEASED_STATUSES: tuple[OrderStatus, ...] = (OrderStatus.CANCELLED, OrderStatus.EXPIRED)
"""Local copy of ``app.services.stock``'s private constant."""


async def _scanned_counts_for_ticket_types(
    session: AsyncSession, ticket_type_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Scanned tickets per TicketType, on live orders only (like the stock count)."""
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
    """Paid revenue and order count per payment method; every method is included, even at zero."""
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
    """Dashboard payload for one event. Assumes the caller already 404'd on a
    missing event; an event with no shows gives empty, zeroed results.
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
    """One CSV row per order (the granularity accountants reconcile against).
    ``total`` is the exact stored decimal string.
    """

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
"""Export column order (``csv.DictWriter`` fieldnames)."""


def _ticket_type_summary(tickets: list[Ticket]) -> str:
    """``"Adult x2; Child x1"``, in first-seen order. Needs ``ticket_type``
    eager-loaded.
    """
    counts: dict[str, int] = {}
    for ticket in tickets:
        name = ticket.ticket_type.name
        counts[name] = counts.get(name, 0) + 1
    return "; ".join(f"{name} x{quantity}" for name, quantity in counts.items())


def build_order_export_rows(orders: list[Order]) -> list[OrderExportRow]:
    """Shape loaded orders into CSV rows. Pure, so it's testable without a DB."""
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
    """Every order for the event, newest first, with what the export needs loaded.
    Unfiltered: the ``status`` column lets the accountant filter.
    """
    result = await session.execute(
        select(Order)
        .where(Order.event_id == event_id)
        .options(selectinload(Order.tickets).selectinload(Ticket.ticket_type), selectinload(Order.invoice))
        .order_by(Order.created_at.desc())
    )
    return list(result.scalars().unique().all())
