"""Integration tests for ``app.services.order_expiry.sweep_stale_orders``
(the proactive stale-``pending``/``pending_door``-Order cleanup sweep — see
that module's docstring for the full design).

Mirrors ``tests/integration/test_order_payment_service.py``'s conventions:
real Orders/Tickets built through ``perform_checkout`` (door method, no
Mollie call), then their ``status``/``created_at`` forced directly on the
ORM row to set up each boundary condition — plain service-layer testing,
no HTTP client needed.

Scope, per this module's own docstring: ``sweep_stale_orders`` reuses
``app.services.order_payment.release_order_stock`` for the actual status
flip/stock release/audit-log mechanics — that function's own row-locking,
idempotency, and never-downgrades-a-paid-order guarantees are already
thoroughly exercised in ``test_order_payment_service.py`` (including
``test_release_order_stock_can_expire_a_pending_door_order``) and are NOT
re-proven here. These tests only exercise the SWEEP's own job: finding the
right Order ids under each policy and calling that function for them.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, time, timedelta

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
from app.services.order_expiry import DEFAULT_PENDING_ORDER_TTL, sweep_stale_orders
from app.services.stock import sold_counts_for_ticket_types

_PAST = datetime.now(UTC) - timedelta(days=1)
_SWEEP_NOW = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)


async def _make_order(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
    *,
    status: OrderStatus,
    created_at: datetime | None = None,
    show_date: object = None,
    show_start_time: time | None = None,
    quantity_available: int = 5,
) -> tuple[Order, TicketType]:
    """A real Order + Ticket(s) via ``perform_checkout`` (door method — no
    Mollie call needed), then ``status``/``created_at`` forced directly on
    the ORM row, same pattern as ``test_order_payment_service.py``'s
    ``_make_pending_order``. ``show_date``/``show_start_time`` let a test
    place the Order's Show relative to ``_SWEEP_NOW`` for the
    ``pending_door`` policy."""
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(
        event_id=event.id,
        status=PublishStatus.PUBLISHED,
        date=show_date or (_SWEEP_NOW.date() + timedelta(days=30)),
        start_time=show_start_time or time(20, 0),
    )
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=quantity_available)
    await make_event_config(
        event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR]
    )
    result = await perform_checkout(
        db_session,
        items=[CheckoutItemInput(ticket_type_id=ticket_type.id, quantity=1)],
        buyer_name="Buyer",
        buyer_email=f"buyer-{uuid.uuid4().hex}@example.test",
        buyer_address="1 Test Street",
        language="en",
        payment_method=PaymentMethod.DOOR,
        preview_token=None,
    )
    order = result.order
    order.status = status
    if created_at is not None:
        order.created_at = created_at
    await db_session.commit()
    await db_session.refresh(order)
    return order, ticket_type


async def _audit_count(db_session: AsyncSession, *, action: str, target_id: uuid.UUID) -> int:
    result = await db_session.execute(
        select(func.count())
        .select_from(AuditLogEntry)
        .where(AuditLogEntry.action == action)
        .where(AuditLogEntry.target_id == str(target_id))
    )
    return result.scalar_one()


async def _status_of(db_session: AsyncSession, order_id: uuid.UUID) -> OrderStatus:
    result = await db_session.execute(select(Order.status).where(Order.id == order_id))
    return result.scalar_one()


# --- pending (Mollie) TTL boundary -----------------------------------------


async def test_pending_order_just_under_the_ttl_cutoff_is_not_swept(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    order, _ = await _make_order(
        db_session,
        make_event,
        make_show,
        make_ticket_type,
        make_event_config,
        status=OrderStatus.PENDING,
        created_at=_SWEEP_NOW - DEFAULT_PENDING_ORDER_TTL + timedelta(minutes=1),
    )

    result = await sweep_stale_orders(db_session, now=_SWEEP_NOW)
    await db_session.commit()

    assert order.id not in result.expired_pending_order_ids
    assert result.total_expired == 0
    assert await _status_of(db_session, order.id) == OrderStatus.PENDING


async def test_pending_order_just_over_the_ttl_cutoff_is_swept_and_releases_stock(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    order, ticket_type = await _make_order(
        db_session,
        make_event,
        make_show,
        make_ticket_type,
        make_event_config,
        status=OrderStatus.PENDING,
        created_at=_SWEEP_NOW - DEFAULT_PENDING_ORDER_TTL - timedelta(minutes=1),
        quantity_available=1,
    )
    sold_before = await sold_counts_for_ticket_types(db_session, [ticket_type.id])
    assert sold_before.get(ticket_type.id, 0) == 1  # sold out before sweep

    result = await sweep_stale_orders(db_session, now=_SWEEP_NOW)
    await db_session.commit()

    assert result.expired_pending_order_ids == [order.id]
    assert result.expired_pending_door_order_ids == []
    assert result.total_expired == 1
    assert await _status_of(db_session, order.id) == OrderStatus.EXPIRED
    assert await _audit_count(db_session, action="order.expired", target_id=order.id) == 1

    sold_after = await sold_counts_for_ticket_types(db_session, [ticket_type.id])
    assert sold_after.get(ticket_type.id, 0) == 0  # stock genuinely released, not just a status flip


# --- pending_door: show-time boundary, NOT a fixed TTL ----------------------


async def test_pending_door_order_tied_to_a_future_show_is_not_swept_even_if_old(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """Deliberately far older than the pending TTL by ``created_at`` alone,
    but tied to a Show that hasn't happened yet — proving this genuinely
    uses the show-date policy, not any fixed TTL."""
    order, _ = await _make_order(
        db_session,
        make_event,
        make_show,
        make_ticket_type,
        make_event_config,
        status=OrderStatus.PENDING_DOOR,
        created_at=_SWEEP_NOW - timedelta(days=365),
        show_date=_SWEEP_NOW.date() + timedelta(days=1),
        show_start_time=time(20, 0),
    )

    result = await sweep_stale_orders(db_session, now=_SWEEP_NOW)
    await db_session.commit()

    assert order.id not in result.expired_pending_door_order_ids
    assert result.total_expired == 0
    assert await _status_of(db_session, order.id) == OrderStatus.PENDING_DOOR


async def test_pending_door_order_tied_to_a_past_show_is_swept_and_releases_stock(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    order, ticket_type = await _make_order(
        db_session,
        make_event,
        make_show,
        make_ticket_type,
        make_event_config,
        status=OrderStatus.PENDING_DOOR,
        show_date=_SWEEP_NOW.date() - timedelta(days=1),
        show_start_time=time(20, 0),
        quantity_available=1,
    )

    result = await sweep_stale_orders(db_session, now=_SWEEP_NOW)
    await db_session.commit()

    assert result.expired_pending_door_order_ids == [order.id]
    assert result.expired_pending_order_ids == []
    assert result.total_expired == 1
    assert await _status_of(db_session, order.id) == OrderStatus.EXPIRED
    assert await _audit_count(db_session, action="order.expired", target_id=order.id) == 1

    sold_after = await sold_counts_for_ticket_types(db_session, [ticket_type.id])
    assert sold_after.get(ticket_type.id, 0) == 0


# --- Never touches paid/cancelled/expired orders; leaves genuinely-fresh
#     pending orders alone -- all in ONE sweep call -------------------------


async def test_sweep_only_touches_the_intended_orders_in_one_call(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    stale_pending, _ = await _make_order(
        db_session,
        make_event,
        make_show,
        make_ticket_type,
        make_event_config,
        status=OrderStatus.PENDING,
        created_at=_SWEEP_NOW - DEFAULT_PENDING_ORDER_TTL - timedelta(hours=1),
    )
    fresh_pending, _ = await _make_order(
        db_session,
        make_event,
        make_show,
        make_ticket_type,
        make_event_config,
        status=OrderStatus.PENDING,
        created_at=_SWEEP_NOW - timedelta(hours=1),
    )
    lapsed_door, _ = await _make_order(
        db_session,
        make_event,
        make_show,
        make_ticket_type,
        make_event_config,
        status=OrderStatus.PENDING_DOOR,
        show_date=_SWEEP_NOW.date() - timedelta(days=2),
        show_start_time=time(20, 0),
    )
    future_door, _ = await _make_order(
        db_session,
        make_event,
        make_show,
        make_ticket_type,
        make_event_config,
        status=OrderStatus.PENDING_DOOR,
        show_date=_SWEEP_NOW.date() + timedelta(days=2),
        show_start_time=time(20, 0),
    )
    paid, _ = await _make_order(
        db_session,
        make_event,
        make_show,
        make_ticket_type,
        make_event_config,
        status=OrderStatus.PAID,
        created_at=_SWEEP_NOW - timedelta(days=100),
    )
    cancelled, _ = await _make_order(
        db_session,
        make_event,
        make_show,
        make_ticket_type,
        make_event_config,
        status=OrderStatus.CANCELLED,
        created_at=_SWEEP_NOW - timedelta(days=100),
    )
    expired, _ = await _make_order(
        db_session,
        make_event,
        make_show,
        make_ticket_type,
        make_event_config,
        status=OrderStatus.EXPIRED,
        created_at=_SWEEP_NOW - timedelta(days=100),
    )

    result = await sweep_stale_orders(db_session, now=_SWEEP_NOW)
    await db_session.commit()

    assert set(result.expired_pending_order_ids) == {stale_pending.id}
    assert set(result.expired_pending_door_order_ids) == {lapsed_door.id}
    assert result.total_expired == 2

    assert await _status_of(db_session, stale_pending.id) == OrderStatus.EXPIRED
    assert await _status_of(db_session, lapsed_door.id) == OrderStatus.EXPIRED
    assert await _status_of(db_session, fresh_pending.id) == OrderStatus.PENDING
    assert await _status_of(db_session, future_door.id) == OrderStatus.PENDING_DOOR
    assert await _status_of(db_session, paid.id) == OrderStatus.PAID
    assert await _status_of(db_session, cancelled.id) == OrderStatus.CANCELLED
    assert await _status_of(db_session, expired.id) == OrderStatus.EXPIRED
    # The pre-existing `expired` order must not get a second audit entry.
    assert await _audit_count(db_session, action="order.expired", target_id=expired.id) == 0


# --- Idempotency: sweeping twice with the same `now` --------------------


async def test_sweeping_twice_with_the_same_now_does_not_double_audit_or_error(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    order, _ = await _make_order(
        db_session,
        make_event,
        make_show,
        make_ticket_type,
        make_event_config,
        status=OrderStatus.PENDING,
        created_at=_SWEEP_NOW - DEFAULT_PENDING_ORDER_TTL - timedelta(hours=1),
    )

    first = await sweep_stale_orders(db_session, now=_SWEEP_NOW)
    await db_session.commit()
    assert first.expired_pending_order_ids == [order.id]

    second = await sweep_stale_orders(db_session, now=_SWEEP_NOW)
    await db_session.commit()

    # The now-EXPIRED order no longer matches the PENDING/PENDING_DOOR
    # candidate query at all, so a second sweep doesn't even attempt to
    # re-release it.
    assert second.expired_pending_order_ids == []
    assert second.total_expired == 0
    assert await _status_of(db_session, order.id) == OrderStatus.EXPIRED
    assert await _audit_count(db_session, action="order.expired", target_id=order.id) == 1


# --- Audit attribution -------------------------------------------------


async def test_swept_orders_are_attributed_to_the_system_principal(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    order, _ = await _make_order(
        db_session,
        make_event,
        make_show,
        make_ticket_type,
        make_event_config,
        status=OrderStatus.PENDING,
        created_at=_SWEEP_NOW - DEFAULT_PENDING_ORDER_TTL - timedelta(hours=1),
    )

    await sweep_stale_orders(db_session, now=_SWEEP_NOW)
    await db_session.commit()

    result = await db_session.execute(
        select(AuditLogEntry).where(
            AuditLogEntry.action == "order.expired", AuditLogEntry.target_id == str(order.id)
        )
    )
    entry = result.scalar_one()
    assert entry.actor_type == ActorType.SYSTEM
