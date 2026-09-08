"""Regression test for a third instance of the SQLAlchemy identity-map
staleness bug — found independently by security-reviewer while auditing
Milestone 5, in ``app.services.stock.reserve_stock`` (fixed alongside this
test). See that function's docstring, and the sibling bugs/fixes in
``app.services.order_payment._lock_order`` and
``app.services.invoicing._allocate_invoice_number``, for the full pattern.

``app.services.checkout.perform_checkout`` reads the requested
``TicketType`` rows via a PLAIN (non-locking) query BEFORE calling
:func:`app.services.stock.reserve_stock`, which then row-locks the same
rows (``SELECT ... FOR UPDATE``) to check remaining stock. Without
``execution_options(populate_existing=True)`` on that locking query, an
admin's concurrent price edit — committed to the row after the checkout's
plain read but before (or while blocked on) the checkout's locking
read — would genuinely be serialized at the DB level but NOT be reflected
in the Python object the checkout goes on to compute ``Order.total``
from: the checkout would silently charge the buyer the STALE, pre-edit
price despite the row lock having correctly waited for the edit to
commit first.

This test forces exactly that interleaving: it delays
``app.services.checkout.reserve_stock`` (monkeypatched to wait on an
``asyncio.Event`` right at its own entry point — i.e. AFTER
``perform_checkout``'s plain ``TicketType`` read has already happened, but
BEFORE ``reserve_stock``'s own locking query runs) long enough for a
second, genuinely separate ``AsyncSession`` to commit a real price change
to the same row, then releases it — so the checkout's locking query
really does race a real concurrent write, not a simulated one.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.checkout as checkout_module
from app.models.enums import PaymentMethod, PublishStatus
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.show import Show
from app.models.ticket_type import TicketType
from app.services.stock import reserve_stock as real_reserve_stock


async def test_checkout_total_reflects_a_price_edit_committed_while_blocked_on_the_row_lock(
    client: AsyncClient,
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=5, price=Decimal("10.00"))
    await make_event_config(event_id=event.id, enabled_payment_methods=[PaymentMethod.DOOR])

    entered_reserve_stock = asyncio.Event()
    release_reserve_stock = asyncio.Event()

    async def _delayed_reserve_stock(
        session: AsyncSession, quantities: dict[uuid.UUID, int]
    ) -> dict[uuid.UUID, TicketType]:
        entered_reserve_stock.set()
        await release_reserve_stock.wait()
        return await real_reserve_stock(session, quantities)

    monkeypatch.setattr(checkout_module, "reserve_stock", _delayed_reserve_stock)

    checkout_task = asyncio.create_task(
        client.post(
            "/api/v1/public/checkout",
            json={
                "buyer_name": "Buyer",
                "buyer_email": f"price-race-{uuid.uuid4().hex}@example.test",
                "buyer_address": "1 Test Street",
                "language": "en",
                "payment_method": PaymentMethod.DOOR.value,
                "items": [{"ticket_type_id": str(ticket_type.id), "quantity": 1}],
            },
        )
    )

    # Wait until the checkout has done perform_checkout's plain TicketType
    # read and is blocked right before reserve_stock's own locking query —
    # the exact precondition the identity-map bug needs.
    await asyncio.wait_for(entered_reserve_stock.wait(), timeout=5)

    # A genuinely separate session (not the checkout's) commits a real
    # price change to the same row. No lock is held yet at this point (the
    # checkout's session hasn't reached reserve_stock's locking query), so
    # this commits immediately rather than blocking.
    result = await db_session.execute(select(TicketType).where(TicketType.id == ticket_type.id))
    live_ticket_type = result.scalar_one()
    live_ticket_type.price = Decimal("99.00")
    await db_session.commit()

    release_reserve_stock.set()
    response = await checkout_task

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["total"] == "99.00", (
        "checkout must charge the price as of the moment it actually acquired the row lock "
        f"(99.00), not the stale value it cached during its earlier plain read (10.00) — got {body['total']}"
    )
