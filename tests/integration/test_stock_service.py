"""Direct coverage of ``app.services.stock`` — the row-locked stock
accounting the checkout race-safety guarantee is built on. Needs a real DB
(``Ticket``/``Order`` join queries), so this lives in ``tests/integration/``
rather than ``tests/unit/`` despite testing fairly isolable logic.
"""

import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import OrderStatus, PaymentMethod, PublishStatus
from app.models.event import Event
from app.models.order import Order
from app.models.show import Show
from app.models.ticket import Ticket
from app.models.ticket_type import TicketType
from app.services.stock import attach_remaining, sold_counts_for_ticket_types


async def _make_order_with_tickets(
    session: AsyncSession,
    *,
    event_id: uuid.UUID,
    ticket_type_id: uuid.UUID,
    status: OrderStatus,
    quantity: int = 1,
) -> Order:
    order = Order(
        event_id=event_id,
        buyer_name="Buyer",
        buyer_email="buyer@example.test",
        buyer_address="1 Test Street",
        status=status,
        payment_method=PaymentMethod.DOOR,
        total=Decimal("10.00"),
        language="en",
    )
    session.add(order)
    await session.flush()
    for _ in range(quantity):
        session.add(Ticket(order_id=order.id, ticket_type_id=ticket_type_id))
    await session.commit()
    await session.refresh(order)
    return order


async def test_sold_counts_excludes_cancelled_and_expired_orders(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=100)

    await _make_order_with_tickets(
        db_session, event_id=event.id, ticket_type_id=ticket_type.id, status=OrderStatus.PENDING, quantity=1
    )
    await _make_order_with_tickets(
        db_session, event_id=event.id, ticket_type_id=ticket_type.id, status=OrderStatus.PAID, quantity=2
    )
    await _make_order_with_tickets(
        db_session,
        event_id=event.id,
        ticket_type_id=ticket_type.id,
        status=OrderStatus.PENDING_DOOR,
        quantity=1,
    )
    await _make_order_with_tickets(
        db_session, event_id=event.id, ticket_type_id=ticket_type.id, status=OrderStatus.CANCELLED, quantity=5
    )
    await _make_order_with_tickets(
        db_session, event_id=event.id, ticket_type_id=ticket_type.id, status=OrderStatus.EXPIRED, quantity=7
    )

    counts = await sold_counts_for_ticket_types(db_session, [ticket_type.id])
    # 1 (pending) + 2 (paid) + 1 (pending_door) = 4; cancelled/expired excluded.
    assert counts[ticket_type.id] == 4


async def test_sold_counts_omits_ticket_types_with_zero_sold(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=10)

    counts = await sold_counts_for_ticket_types(db_session, [ticket_type.id])
    assert ticket_type.id not in counts
    assert counts.get(ticket_type.id, 0) == 0


async def test_sold_counts_for_empty_input_returns_empty_dict(db_session: AsyncSession) -> None:
    assert await sold_counts_for_ticket_types(db_session, []) == {}


async def test_attach_remaining_wires_live_count_onto_remaining_property(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=10)

    await _make_order_with_tickets(
        db_session, event_id=event.id, ticket_type_id=ticket_type.id, status=OrderStatus.PAID, quantity=3
    )

    await attach_remaining(db_session, [ticket_type])
    assert ticket_type.remaining == 7


async def test_attach_remaining_on_empty_list_is_a_no_op(db_session: AsyncSession) -> None:
    # Must not raise for an empty sequence.
    await attach_remaining(db_session, [])


async def test_remaining_falls_back_to_quantity_available_when_never_attached(
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=42)

    # A freshly fetched TicketType that never went through attach_remaining
    # / attach_sold_count must fail safe: report full stock, not crash and
    # not silently report 0.
    assert ticket_type.remaining == 42


async def test_remaining_never_goes_negative_even_if_oversold(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=1)

    await _make_order_with_tickets(
        db_session, event_id=event.id, ticket_type_id=ticket_type.id, status=OrderStatus.PAID, quantity=5
    )

    await attach_remaining(db_session, [ticket_type])
    assert ticket_type.remaining == 0
