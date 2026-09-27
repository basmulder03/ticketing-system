"""Stock accounting: remaining counts, and the row-locked reservation that
prevents overselling. Tickets on non-cancelled/expired orders hold stock.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import OrderStatus
from app.models.order import Order
from app.models.ticket import Ticket
from app.models.ticket_type import TicketType

_RELEASED_STATUSES = (OrderStatus.CANCELLED, OrderStatus.EXPIRED)


async def sold_counts_for_ticket_types(
    session: AsyncSession, ticket_type_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Live ticket counts per TicketType id; ids with zero are omitted."""
    if not ticket_type_ids:
        return {}
    stmt = (
        select(Ticket.ticket_type_id, func.count(Ticket.id))
        .join(Order, Order.id == Ticket.order_id)
        .where(Ticket.ticket_type_id.in_(ticket_type_ids))
        .where(Order.status.notin_(_RELEASED_STATUSES))
        .group_by(Ticket.ticket_type_id)
    )
    result = await session.execute(stmt)
    return {row[0]: row[1] for row in result.all()}


async def attach_remaining(session: AsyncSession, ticket_types: Sequence[TicketType]) -> None:
    """Attach live sold counts so ``.remaining`` is correct. Required before
    returning ``remaining`` for rows fetched outside checkout.
    """
    if not ticket_types:
        return
    counts = await sold_counts_for_ticket_types(session, [tt.id for tt in ticket_types])
    for ticket_type in ticket_types:
        ticket_type.attach_sold_count(counts.get(ticket_type.id, 0))


class TicketTypeNotFoundError(Exception):
    """A requested TicketType id doesn't exist."""

    def __init__(self, missing_ids: Sequence[uuid.UUID]) -> None:
        self.missing_ids = list(missing_ids)
        super().__init__(f"TicketType(s) not found: {self.missing_ids}")


class InsufficientStockError(Exception):
    """A requested quantity exceeds the stock remaining under the lock."""

    def __init__(self, ticket_type_id: uuid.UUID, requested: int, remaining: int) -> None:
        self.ticket_type_id = ticket_type_id
        self.requested = requested
        self.remaining = remaining
        super().__init__(f"Requested {requested} of ticket type {ticket_type_id}, only {remaining} remain.")


async def reserve_stock(
    session: AsyncSession, quantities_by_ticket_type_id: dict[uuid.UUID, int]
) -> dict[uuid.UUID, TicketType]:
    """Lock the TicketType rows and verify stock for each quantity. Creates no
    tickets and doesn't commit — the caller does both in the same transaction.

    Concurrency (read before editing):

    - ``FOR UPDATE`` makes overlapping checkouts queue; the next one re-counts
      after the lock, so no oversell — as long as every ticket-creating path
      calls this first.
    - Rows are locked in id order so multi-type checkouts can't deadlock.
    - ``populate_existing=True`` is load-bearing: checkout already loaded these
      rows unlocked, and without it SQLAlchemy's identity map would hand back
      the *stale* pre-lock ``price``/``quantity_available``.

    Raises :class:`TicketTypeNotFoundError` or :class:`InsufficientStockError`.
    """
    if not quantities_by_ticket_type_id:
        return {}
    ticket_type_ids = sorted(quantities_by_ticket_type_id.keys())
    result = await session.execute(
        select(TicketType)
        .where(TicketType.id.in_(ticket_type_ids))
        .order_by(TicketType.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    locked = {tt.id: tt for tt in result.scalars().all()}
    missing = [tid for tid in ticket_type_ids if tid not in locked]
    if missing:
        raise TicketTypeNotFoundError(missing)

    sold_counts = await sold_counts_for_ticket_types(session, ticket_type_ids)
    for ticket_type_id in ticket_type_ids:
        requested = quantities_by_ticket_type_id[ticket_type_id]
        remaining = locked[ticket_type_id].quantity_available - sold_counts.get(ticket_type_id, 0)
        if requested > remaining:
            raise InsufficientStockError(ticket_type_id, requested, max(remaining, 0))
    return locked
