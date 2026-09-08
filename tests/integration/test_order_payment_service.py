"""Integration coverage for ``app.services.order_payment`` — the shared,
idempotent, row-locked Order payment-status transitions used by both the
Mollie webhook (``test_mollie_webhook.py``) and (Milestone 6) manual
mark-as-paid.

Orders/Tickets are created via the real ``perform_checkout`` service
(``payment_method=door``, which never touches Mollie) rather than
constructed by hand, so these tests exercise ``mark_order_paid``/
``release_order_stock`` against a realistic Order+Ticket graph. Starting
statuses this milestone's checkout flow doesn't itself produce (e.g.
``pending_door``, or a lapsed ``cancelled``/``expired`` order being
recovered) are set directly on the ORM row — see each test's setup.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLogEntry
from app.models.enums import ActorType, OrderStatus, PaymentMethod, PublishStatus
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.order import Order
from app.models.show import Show
from app.models.ticket_type import TicketType
from app.services.checkout import CheckoutItemInput, perform_checkout
from app.services.order_payment import SYSTEM_PRINCIPAL, mark_order_paid, release_order_stock
from app.services.stock import sold_counts_for_ticket_types

_PAST = datetime.now(UTC) - timedelta(days=1)


async def _make_pending_order(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
    *,
    quantity_available: int = 5,
) -> tuple[Order, TicketType]:
    """A real ``pending`` Order + Ticket(s), created via the checkout
    service (door method — no Mollie involvement needed for these tests)
    then forced back to plain ``PENDING``.

    As of Milestone 6, ``payment_method=door`` checkout itself produces
    ``PENDING_DOOR`` (see ``app.services.checkout.perform_checkout``), not
    plain ``PENDING`` — but most tests in this file exist to exercise the
    generic, payment-method-agnostic ``PENDING`` transition path (in
    particular ``release_order_stock``, which is Mollie-webhook-specific
    and only ever acts on a still-``PENDING`` Order, per its own
    docstring; a ``pending_door`` Order never reaches it in production
    since door orders have no Mollie payment to fail). Overriding the
    status directly on the ORM row after checkout — same pattern this file
    already uses for ``pending_door``/``cancelled``/``expired`` starting
    states below — keeps this helper producing a realistic Order+Ticket
    graph without a real Mollie call, while still testing plain ``PENDING``
    where that's genuinely what the function under test expects.
    """
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=quantity_available)
    await make_event_config(
        event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR]
    )
    result = await perform_checkout(
        db_session,
        items=[CheckoutItemInput(ticket_type_id=ticket_type.id, quantity=1)],
        buyer_name="Buyer",
        buyer_email="buyer@example.test",
        buyer_address="1 Test Street",
        language="en",
        payment_method=PaymentMethod.DOOR,
        preview_token=None,
    )
    result.order.status = OrderStatus.PENDING
    await db_session.commit()
    await db_session.refresh(result.order)
    return result.order, ticket_type


async def _audit_count(db_session: AsyncSession, *, action: str, target_id: uuid.UUID) -> int:
    result = await db_session.execute(
        select(func.count())
        .select_from(AuditLogEntry)
        .where(AuditLogEntry.action == action)
        .where(AuditLogEntry.target_id == str(target_id))
    )
    return result.scalar_one()


# --- mark_order_paid --------------------------------------------------------


async def test_mark_order_paid_transitions_pending_to_paid_with_one_audit_entry(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    order, _ = await _make_pending_order(db_session, make_event, make_show, make_ticket_type, make_event_config)
    assert order.status == OrderStatus.PENDING

    result = await mark_order_paid(
        db_session, order_id=order.id, principal=SYSTEM_PRINCIPAL, method_label="mollie", reason="test"
    )
    await db_session.commit()

    assert result.already_paid is False
    assert result.order.status == OrderStatus.PAID
    assert await _audit_count(db_session, action="order.mark_paid", target_id=order.id) == 1


async def test_mark_order_paid_transitions_pending_door_to_paid(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """No checkout code path sets ``pending_door`` yet (Milestone 6) — set
    it directly to exercise the transition ``mark_order_paid`` must support
    once the door-payment flow lands."""
    order, _ = await _make_pending_order(db_session, make_event, make_show, make_ticket_type, make_event_config)
    order.status = OrderStatus.PENDING_DOOR
    await db_session.commit()

    result = await mark_order_paid(
        db_session, order_id=order.id, principal=SYSTEM_PRINCIPAL, method_label="door", reason="test"
    )
    await db_session.commit()

    assert result.already_paid is False
    assert result.order.status == OrderStatus.PAID


async def test_mark_order_paid_on_already_paid_order_is_a_no_op(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    order, _ = await _make_pending_order(db_session, make_event, make_show, make_ticket_type, make_event_config)
    first = await mark_order_paid(
        db_session, order_id=order.id, principal=SYSTEM_PRINCIPAL, method_label="mollie", reason="first"
    )
    await db_session.commit()
    assert first.already_paid is False

    second = await mark_order_paid(
        db_session, order_id=order.id, principal=SYSTEM_PRINCIPAL, method_label="mollie", reason="second"
    )
    await db_session.commit()

    assert second.already_paid is True
    assert second.order.status == OrderStatus.PAID
    # Exactly one audit entry total — the second (no-op) call wrote nothing.
    assert await _audit_count(db_session, action="order.mark_paid", target_id=order.id) == 1


async def test_mark_order_paid_recovers_a_cancelled_order(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """Documented allowed manual-recovery case: staff can still mark a
    lapsed cancelled/expired order paid (e.g. a late bank transfer for an
    order whose Mollie payment had already expired)."""
    order, _ = await _make_pending_order(db_session, make_event, make_show, make_ticket_type, make_event_config)
    order.status = OrderStatus.CANCELLED
    await db_session.commit()

    result = await mark_order_paid(
        db_session, order_id=order.id, principal=SYSTEM_PRINCIPAL, method_label="bank_transfer", reason="recovered"
    )
    await db_session.commit()

    assert result.already_paid is False
    assert result.order.status == OrderStatus.PAID
    assert await _audit_count(db_session, action="order.mark_paid", target_id=order.id) == 1


async def test_mark_order_paid_recovers_an_expired_order(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    order, _ = await _make_pending_order(db_session, make_event, make_show, make_ticket_type, make_event_config)
    order.status = OrderStatus.EXPIRED
    await db_session.commit()

    result = await mark_order_paid(
        db_session, order_id=order.id, principal=SYSTEM_PRINCIPAL, method_label="cash", reason="recovered"
    )
    await db_session.commit()

    assert result.order.status == OrderStatus.PAID


# --- release_order_stock -----------------------------------------------------


async def test_release_order_stock_transitions_pending_to_cancelled_with_audit_entry(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    order, _ = await _make_pending_order(db_session, make_event, make_show, make_ticket_type, make_event_config)

    result = await release_order_stock(
        db_session,
        order_id=order.id,
        new_status=OrderStatus.CANCELLED,
        principal=SYSTEM_PRINCIPAL,
        reason="Mollie payment status: canceled.",
    )
    await db_session.commit()

    assert result.status == OrderStatus.CANCELLED
    assert await _audit_count(db_session, action="order.cancelled", target_id=order.id) == 1


async def test_release_order_stock_transitions_pending_to_expired_with_audit_entry(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    order, _ = await _make_pending_order(db_session, make_event, make_show, make_ticket_type, make_event_config)

    result = await release_order_stock(
        db_session,
        order_id=order.id,
        new_status=OrderStatus.EXPIRED,
        principal=SYSTEM_PRINCIPAL,
        reason="Mollie payment status: expired.",
    )
    await db_session.commit()

    assert result.status == OrderStatus.EXPIRED
    assert await _audit_count(db_session, action="order.expired", target_id=order.id) == 1


async def test_release_order_stock_is_a_no_op_on_a_non_pending_order(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    order, _ = await _make_pending_order(db_session, make_event, make_show, make_ticket_type, make_event_config)
    order.status = OrderStatus.PAID
    await db_session.commit()

    result = await release_order_stock(
        db_session,
        order_id=order.id,
        new_status=OrderStatus.EXPIRED,
        principal=SYSTEM_PRINCIPAL,
        reason="late/stale expired webhook",
    )
    await db_session.commit()

    assert result.status == OrderStatus.PAID
    assert await _audit_count(db_session, action="order.expired", target_id=order.id) == 0


async def test_release_order_stock_never_downgrades_an_already_paid_order(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """The exact race the brief flags: a 'paid' webhook is reconciled
    first, then a stale/late 'expired' webhook for the same payment arrives
    afterwards. The Order must stay paid, never flip back to expired."""
    order, _ = await _make_pending_order(db_session, make_event, make_show, make_ticket_type, make_event_config)
    paid_result = await mark_order_paid(
        db_session, order_id=order.id, principal=SYSTEM_PRINCIPAL, method_label="mollie", reason="paid first"
    )
    await db_session.commit()
    assert paid_result.order.status == OrderStatus.PAID

    released = await release_order_stock(
        db_session,
        order_id=order.id,
        new_status=OrderStatus.EXPIRED,
        principal=SYSTEM_PRINCIPAL,
        reason="stale expired webhook delivered after paid",
    )
    await db_session.commit()

    assert released.status == OrderStatus.PAID


# --- Stock is genuinely released, not just the Order status flipped --------


async def test_release_order_stock_genuinely_frees_up_sold_out_stock(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """A quantity_available=1 TicketType, one Order buying that last
    ticket (reads as sold out), released via ``release_order_stock`` — the
    live sold count for that TicketType must go back to 0 (remaining back
    to 1), confirming this is real stock release, not just a status flip
    with stale accounting.
    """
    order, ticket_type = await _make_pending_order(
        db_session, make_event, make_show, make_ticket_type, make_event_config, quantity_available=1
    )

    sold_before = await sold_counts_for_ticket_types(db_session, [ticket_type.id])
    assert sold_before.get(ticket_type.id, 0) == 1  # sold out

    await release_order_stock(
        db_session,
        order_id=order.id,
        new_status=OrderStatus.EXPIRED,
        principal=SYSTEM_PRINCIPAL,
        reason="Mollie payment status: expired.",
    )
    await db_session.commit()

    sold_after = await sold_counts_for_ticket_types(db_session, [ticket_type.id])
    assert sold_after.get(ticket_type.id, 0) == 0
    remaining_after = ticket_type.quantity_available - sold_after.get(ticket_type.id, 0)
    assert remaining_after == 1


async def test_system_principal_audit_entries_use_system_actor_type(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """security-reviewer's Milestone 3 pass flagged that attributing
    automated payment reconciliation to ``ActorType.HUMAN`` (even with a
    ``system:``-prefixed name) risked silent misclassification as staff
    activity in any future audit-log view segmented by actor type. Fixed
    by adding a real ``ActorType.SYSTEM`` value — this test locks in the
    corrected attribution so a future regression back to ``HUMAN`` is
    caught here, not discovered later in an audit report."""
    order, _ = await _make_pending_order(db_session, make_event, make_show, make_ticket_type, make_event_config)

    await mark_order_paid(
        db_session, order_id=order.id, principal=SYSTEM_PRINCIPAL, method_label="mollie", reason="test"
    )
    await db_session.commit()

    result = await db_session.execute(
        select(AuditLogEntry)
        .where(AuditLogEntry.action == "order.mark_paid")
        .where(AuditLogEntry.target_id == str(order.id))
    )
    entry = result.scalar_one()
    assert entry.actor_type == ActorType.SYSTEM
    assert entry.actor_name == "system:mollie-webhook"
