"""Idempotent Order payment-status transitions, shared by every caller that
can confirm or fail a payment: the Mollie webhook (Milestone 3, automatic —
see ``app.api.routes.public.mollie_webhook``) and the backoffice manual
mark-as-paid action (Milestone 6, staff-triggered) — per
PROJECT_BRIEF.md's Manual Payment Handling section: manually marking an
order paid "triggers the same downstream effects as an automatic Mollie
payment confirmation". Both call :func:`mark_order_paid`; nothing about
payment-status transition logic should live anywhere else.

Every function here row-locks the ``Order`` by id (``SELECT ... FOR
UPDATE``, mirroring ``app.services.stock.reserve_stock``'s locking
discipline) before reading its current status, so two concurrent callers
acting on the same Order (most realistically: Mollie retrying an
undelivered webhook) can never both observe the pre-transition status and
both apply the transition + write an audit entry. The second caller always
blocks until the first commits, then sees the already-updated status and
no-ops.
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
"""Audit-log actor used for automated, non-human-initiated Order status
transitions (Mollie webhook reconciliation; the preview-mode simulated
checkout).

Uses ``ActorType.SYSTEM`` (added alongside this module) rather than
``ActorType.HUMAN``: security-reviewer flagged that labeling an automated
payment reconciliation as a human action would let it be silently
misclassified as staff activity by any future audit-log view that
segments "actions by staff" — exactly the kind of misattribution the
brief's audit model exists to prevent, just from a different direction
than the "never merge an Agent action into a generic system actor" case
the brief names explicitly. ``ActorType.AI_AGENT`` remains deliberately
unused here too: that value specifically means "an AgentAccount API key,"
which the brief scopes away from financial data and payment credentials —
reusing it for an automatic payment confirmation would blur that same
privilege boundary from the other side. The sentinel all-zero
``actor_id`` can never collide with a real ``AdminUser`` id (a
``uuid.uuid4()`` value), and the ``system:``-prefixed name makes the
entry unmistakable even before ``actor_type`` is considered.
"""


@dataclass(frozen=True)
class MarkOrderPaidResult:
    """Result of :func:`mark_order_paid`."""

    order: Order
    already_paid: bool
    """``True`` if the Order was already ``PAID`` when this was called — a
    safe no-op, nothing new was written (no status change, no audit
    entry). Callers that trigger downstream effects (ticket/invoice email
    dispatch — Milestones 4/5) on top of this function MUST gate that on
    ``already_paid is False``, so a retried webhook or a duplicate
    mark-as-paid click never re-sends a ticket email."""


async def _lock_order(session: AsyncSession, order_id: uuid.UUID) -> Order:
    """Fetch and row-lock the ``Order`` with ``order_id`` for the duration
    of the caller's transaction. Raises ``sqlalchemy.exc.NoResultFound`` via
    ``scalar_one`` if it doesn't exist — callers are expected to have
    already resolved the Order's existence before calling into this
    module (e.g. the webhook route's own lookup by ``mollie_payment_id``).

    ``execution_options(populate_existing=True)`` is load-bearing, not a
    stylistic default — found and fixed via a genuine, reproduced bug: the
    webhook route (``app.api.routes.public.mollie_webhook``) already reads
    this same Order via a PLAIN (non-locking) query earlier in the SAME
    session, before calling into this module at all, to look it up by
    ``mollie_payment_id``. Without ``populate_existing``, SQLAlchemy's
    identity map returns that already-loaded Python object as-is when this
    query's WHERE clause matches the same primary key — the
    ``SELECT ... FOR UPDATE`` is still genuinely sent to Postgres and
    genuinely serializes concurrent transactions at the DB level, but the
    in-memory ``status`` attribute is NOT refreshed from that query's
    result, so a transaction that was blocked waiting for another one to
    commit resumes holding the STALE pre-lock status it cached earlier —
    reading ``PENDING`` even though the row it just locked is actually
    ``PAID``. Reproduced directly: 8 genuinely concurrent duplicate webhook
    deliveries for the same Order, before this fix, produced multiple
    ``already_paid=False`` results and multiple ``order.mark_paid`` audit
    entries for one Order — exactly the double-processing the brief's
    "a retried webhook must never double-issue tickets" requirement exists
    to prevent. See ``app.services.invoicing._allocate_invoice_number`` for
    the sibling bug this was found alongside (same root cause, same fix).
    """
    result = await session.execute(
        select(Order).where(Order.id == order_id).with_for_update().execution_options(populate_existing=True)
    )
    return result.scalar_one()


async def _verify_stock_for_resurrected_order(session: AsyncSession, order: Order) -> None:
    """Guard against overselling when recovering a ``CANCELLED``/
    ``EXPIRED`` Order back to ``PAID`` (staff correction, or a late/
    out-of-order Mollie webhook).

    security-reviewer finding (Milestone 6): ``app.services.stock``
    excludes ``CANCELLED``/``EXPIRED`` orders from
    ``sold_counts_for_ticket_types``, so the moment an Order lapses, its
    Tickets' stock is released and can be legitimately resold to a
    DIFFERENT buyer by a concurrent/later checkout. Without this check,
    resurrecting the lapsed Order via :func:`mark_order_paid` would
    re-activate its Tickets unconditionally, regardless of whether that
    stock has since been resold — producing two valid, QR-signed Tickets
    for one seat/capacity slot, a genuine overselling bug, not just a
    bookkeeping one.

    Reuses :func:`app.services.stock.reserve_stock` itself (rather than
    duplicating its row-locking/verification logic) with quantities
    derived from the Order's OWN existing Tickets: since ``order`` is
    still ``CANCELLED``/``EXPIRED`` at the moment this runs (the status
    flip happens after this check succeeds), ``sold_counts_for_ticket_types``
    correctly excludes this Order's own Tickets from the count already in
    flight, so this call verifies there is genuinely enough headroom to
    re-admit them — under the exact same row lock discipline used to
    prevent overselling at checkout. Raises
    :class:`app.services.stock.InsufficientStockError` (or, pathologically,
    :class:`app.services.stock.TicketTypeNotFoundError`) if not — callers
    MUST let this propagate rather than swallowing it, since silently
    proceeding would be exactly the bug this exists to prevent.

    A no-op (and no lock taken) for an Order with no Tickets at all, which
    should not occur in practice but is handled defensively rather than
    crashing a payment confirmation.
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
    """Idempotently transition the Order with ``order_id`` to
    ``OrderStatus.PAID`` and record exactly one audit entry for the
    transition.

    Safe to call multiple times for the same Order: if it's already
    ``PAID`` (whether from an earlier call of this same function, a prior
    webhook delivery, or a prior manual mark-as-paid), this is a no-op —
    see :class:`MarkOrderPaidResult`. Works from any non-``PAID`` starting
    status (``PENDING``, ``PENDING_DOOR``, even a previously
    ``CANCELLED``/``EXPIRED`` order being manually recovered by staff —
    Milestone 6 territory, not restricted here since a manual override of a
    lapsed order is a legitimate staff action) — EXCEPT that recovering a
    ``CANCELLED``/``EXPIRED`` order first re-verifies its stock is still
    available (see :func:`_verify_stock_for_resurrected_order`) and raises
    ``app.services.stock.InsufficientStockError``/``TicketTypeNotFoundError``
    if that stock has since been resold to someone else — callers (the
    manual mark-as-paid route, the Mollie webhook) MUST handle these as a
    genuine conflict, not a bug to work around. ``PENDING``/``PENDING_DOOR``
    orders never released their stock in the first place, so no re-check is
    needed for them.

    Does not commit — the caller controls the transaction boundary
    alongside whatever else it's persisting (e.g. the webhook route commits
    once after reconciling).
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
"""Starting statuses :func:`release_order_stock` is willing to transition
out of — both are, per ``app.models.enums.OrderStatus``'s docstring, "still
holding its reserved stock" statuses: ``PENDING`` (a Mollie payment in
flight) and ``PENDING_DOOR`` (a buyer's pay-at-the-door reservation for a
future show). Widened from ``PENDING``-only (Milestone 3) to also include
``PENDING_DOOR`` in Milestone 10 (``app.services.order_expiry``), so the
stale-order sweep can reuse this one function for both kinds of lapsed
reservation instead of duplicating its row-locking/idempotency/never-
downgrades-a-paid-order guarantees for a second, near-identical status
flip — see that module for why ``PENDING_DOOR`` orders need a very
different TTL policy (tied to the Show's own start time, not a fixed
duration) even though the release mechanics themselves are identical."""


async def release_order_stock(
    session: AsyncSession,
    *,
    order_id: uuid.UUID,
    new_status: OrderStatus,
    principal: Principal,
    reason: str,
) -> Order:
    """Idempotently transition a still-``PENDING``/``PENDING_DOOR`` Order to
    ``new_status`` (``OrderStatus.CANCELLED`` or ``OrderStatus.EXPIRED``)
    after a failed/expired/canceled Mollie payment, a lapsed pay-at-the-door
    reservation (``app.services.order_expiry``), or a staff cancellation.

    Releasing reserved stock needs no separate mechanism beyond this status
    flip: ``app.services.stock.sold_counts_for_ticket_types`` already
    excludes both ``CANCELLED`` and ``EXPIRED`` orders from stock counts, so
    the moment this commits, this Order's Tickets stop counting against
    their TicketTypes' remaining stock.

    A safe no-op if the Order is not currently in one of
    :data:`_RELEASABLE_STATUSES` — in particular this NEVER downgrades an
    already-``PAID`` order, even if a stale/late "expired" webhook is
    delivered after a separate "paid" webhook for the same Order was already
    processed first; whichever reconciliation call is processed first under
    the row lock wins, and ``Order.status`` only ever leaves
    ``PENDING``/``PENDING_DOOR`` once.
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
