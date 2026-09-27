"""Idempotent order payment transitions, shared by every caller (Mollie
webhook, manual mark-as-paid, demo payments, manual orders) so manual and
automatic confirmations trigger identical effects.

Each function locks the Order (``FOR UPDATE``) before reading its status, so
concurrent callers (e.g. a retried webhook) can't both apply a transition:
the second waits, then sees the new status and no-ops.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal
from app.models.enums import ActorType, OrderStatus
from app.models.order import Order
from app.models.ticket import Ticket
from app.services.audit import record_audit_entry
from app.services.stock import reserve_stock

SYSTEM_PRINCIPAL = Principal(
    actor_type=ActorType.SYSTEM,
    id=uuid.UUID(int=0),
    name="system:mollie-webhook",
    role=None,
)
"""Audit actor for automated transitions (webhook reconciliation, preview
sandbox payments). ``SYSTEM`` rather than ``HUMAN`` so it's never mistaken for
staff activity, and never ``AI_AGENT`` (agents are kept away from payments).
The all-zero id can't collide with a real user.
"""


@dataclass(frozen=True)
class MarkOrderPaidResult:
    """Result of :func:`mark_order_paid`."""

    order: Order
    already_paid: bool
    """``True`` if the order was already paid: nothing was written. Callers
    must skip downstream effects (emails, invoice) in that case, so a retried
    webhook or double click never re-sends anything.
    """


async def _lock_order(session: AsyncSession, order_id: uuid.UUID) -> Order:
    """Load and ``FOR UPDATE``-lock the order. Raises ``NoResultFound`` if missing.

    ``populate_existing=True`` is load-bearing: the webhook already loaded this
    order unlocked in the same session, and without it the identity map would
    hand back the stale pre-lock ``status`` — letting duplicate concurrent
    webhooks each process the payment (reproduced before this fix).
    """
    result = await session.execute(
        select(Order).where(Order.id == order_id).with_for_update().execution_options(populate_existing=True)
    )
    return result.scalar_one()


async def _verify_stock_for_resurrected_order(session: AsyncSession, order: Order) -> None:
    """Before reviving a CANCELLED/EXPIRED order, check its stock wasn't resold.

    A lapsed order's stock is released and may have been sold to someone else;
    reviving it blindly would oversell. Re-reserves the order's own ticket
    quantities through ``reserve_stock`` (the order is still excluded from the
    count at this point). Raises ``InsufficientStockError`` /
    ``TicketTypeNotFoundError`` — callers must propagate, not swallow.
    """
    result = await session.execute(select(Ticket.ticket_type_id).where(Ticket.order_id == order.id))
    quantities: dict[uuid.UUID, int] = {}
    for (ticket_type_id,) in result.all():
        quantities[ticket_type_id] = quantities.get(ticket_type_id, 0) + 1
    if not quantities:
        return
    await reserve_stock(session, quantities)


async def mark_order_paid(
    session: AsyncSession,
    *,
    order_id: uuid.UUID,
    principal: Principal,
    method_label: str,
    reason: str | None = None,
) -> MarkOrderPaidResult:
    """Idempotently mark the order paid and write exactly one audit entry.

    Works from any non-paid status. Reviving a CANCELLED/EXPIRED order first
    re-checks its stock (see :func:`_verify_stock_for_resurrected_order`) and
    may raise; callers must treat that as a real conflict. Doesn't commit.
    """
    order = await _lock_order(session, order_id)
    if order.status == OrderStatus.PAID:
        return MarkOrderPaidResult(order=order, already_paid=True)

    if order.status in (OrderStatus.CANCELLED, OrderStatus.EXPIRED):
        await _verify_stock_for_resurrected_order(session, order)

    order.status = OrderStatus.PAID
    detail: dict[str, Any] = {"method": method_label}
    if reason:
        detail["reason"] = reason
    await record_audit_entry(
        session,
        principal,
        action="order.mark_paid",
        target_type="Order",
        target_id=str(order.id),
        detail=detail,
    )
    await session.flush()
    return MarkOrderPaidResult(order=order, already_paid=False)


_RELEASABLE_STATUSES = (OrderStatus.PENDING, OrderStatus.PENDING_DOOR)
"""Statuses that still hold stock and can be released. ``PENDING_DOOR`` is
included so the expiry sweep can reuse :func:`release_order_stock`.
"""


async def release_order_stock(
    session: AsyncSession,
    *,
    order_id: uuid.UUID,
    new_status: OrderStatus,
    principal: Principal,
    reason: str,
) -> Order:
    """Idempotently move a PENDING/PENDING_DOOR order to ``new_status``
    (CANCELLED or EXPIRED). The status change alone releases its stock.

    A no-op for any other status — in particular it never downgrades a paid
    order, so a late "expired" webhook after "paid" is harmless.
    """
    order = await _lock_order(session, order_id)
    if order.status not in _RELEASABLE_STATUSES:
        return order

    order.status = new_status
    await record_audit_entry(
        session,
        principal,
        action=f"order.{new_status.value}",
        target_type="Order",
        target_id=str(order.id),
        detail={"reason": reason},
    )
    await session.flush()
    return order
