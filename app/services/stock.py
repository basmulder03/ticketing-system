"""Live stock accounting for ``TicketType``: computing how many tickets
remain, and the row-locked check used by checkout (Milestone 2) to prevent
overselling under concurrent buyers.

Stock math treats every ``Ticket`` row belonging to a non-cancelled,
non-expired ``Order`` as "still holding its stock" — see
``app.models.enums.OrderStatus`` docstring for the exact status list this
excludes.
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
    """Count live (non-cancelled/non-expired) ``Ticket`` rows per
    ``TicketType`` id.

    Returns a dict that omits any id with zero tickets — callers should
    treat a missing key as 0 (via ``dict.get(id, 0)``) rather than expect
    every requested id to be present.
    """
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
    """Compute and attach the live sold count onto each of ``ticket_types``
    so its ``.remaining`` property reflects real stock (see
    ``TicketType.attach_sold_count``/``TicketType.remaining``).

    Callers building an API response that includes ``remaining`` for one or
    more ``TicketType`` rows fetched outside the checkout transaction MUST
    call this first — without it, ``.remaining`` silently falls back to
    "nothing sold yet" (see that property's docstring).
    """
    if not ticket_types:
        return
    counts = await sold_counts_for_ticket_types(session, [tt.id for tt in ticket_types])
    for ticket_type in ticket_types:
        ticket_type.attach_sold_count(counts.get(ticket_type.id, 0))


class TicketTypeNotFoundError(Exception):
    """Raised by :func:`reserve_stock` when one or more requested
    TicketType ids don't exist (e.g. deleted between validation and the
    locking query, or a caller skipped validation)."""

    def __init__(self, missing_ids: Sequence[uuid.UUID]) -> None:
        self.missing_ids = list(missing_ids)
        super().__init__(f"TicketType(s) not found: {self.missing_ids}")


class InsufficientStockError(Exception):
    """Raised by :func:`reserve_stock` when a requested quantity exceeds a
    TicketType's real remaining stock, computed under its row lock."""

    def __init__(self, ticket_type_id: uuid.UUID, requested: int, remaining: int) -> None:
        self.ticket_type_id = ticket_type_id
        self.requested = requested
        self.remaining = remaining
        super().__init__(f"Requested {requested} of ticket type {ticket_type_id}, only {remaining} remain.")


async def reserve_stock(
    session: AsyncSession, quantities_by_ticket_type_id: dict[uuid.UUID, int]
) -> dict[uuid.UUID, TicketType]:
    """Lock the given ``TicketType`` rows and verify enough stock remains
    for each requested quantity — the race-safety-critical step for
    checkout, per PROJECT_BRIEF.md's Security & Ops section ("critical at
    the sales-live moment").

    Does NOT create any ``Ticket`` rows itself (that's the caller's job,
    inside the same transaction — see
    ``app.services.checkout.perform_checkout``) and does NOT commit or roll
    back — the caller controls the transaction boundary.

    Concurrency behavior (read this before touching this function):
    ``SELECT ... FOR UPDATE`` locks every referenced ``TicketType`` row for
    the duration of the caller's transaction. A concurrent checkout that
    also calls this function for an overlapping ``TicketType`` id blocks at
    this query until the first transaction commits or rolls back, at which
    point it re-reads the now-current sold count via
    :func:`sold_counts_for_ticket_types` — so it can never oversell, as
    long as every code path that creates ``Ticket`` rows goes through this
    function first (checkout is currently the only such path). Rows are
    locked in ascending id order (a single query with ``ORDER BY id``)
    specifically so two concurrent multi-ticket-type checkouts that overlap
    on more than one ``TicketType`` always attempt to acquire locks in the
    same order, avoiding a lock-ordering deadlock.

    ``execution_options(populate_existing=True)`` is load-bearing, not a
    stylistic default — the same SQLAlchemy identity-map staleness bug
    found and fixed in ``app.services.order_payment._lock_order`` and
    ``app.services.invoicing._allocate_invoice_number`` also applies here,
    independently found and reproduced by security-reviewer:
    ``app.services.checkout.perform_checkout`` already reads these same
    ``TicketType`` rows via a PLAIN (non-locking) query earlier in the SAME
    session (to resolve Show/Event/EventConfig for sales-timing checks)
    before calling this function. Without ``populate_existing``, the
    identity map would return those already-loaded objects as-is once this
    query's ``WHERE`` clause matches the same primary keys — the
    ``SELECT ... FOR UPDATE`` still genuinely locks the rows at the DB
    level, but ``locked[id].price``/``.quantity_available`` would be the
    STALE pre-lock values, not the fresh, correctly-serialized ones a
    blocked transaction is entitled to see after acquiring the lock.
    Reproduced directly: a concurrent admin price/capacity edit committed
    while a checkout was blocked on this row lock was invisible to the
    checkout even after it acquired the lock, without this fix.
    ``sold_counts_for_ticket_types`` below was already safe on its own (a
    fresh aggregate query, not a cached counter attribute) — this fix
    covers the two attributes read directly off the locked ORM objects
    instead (``quantity_available`` here, ``price`` in
    ``app.services.checkout``).

    Raises :class:`TicketTypeNotFoundError` if any id doesn't exist, or
    :class:`InsufficientStockError` (naming the first short type found) if
    any requested quantity exceeds what's actually left.
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
