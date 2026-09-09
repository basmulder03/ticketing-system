"""Concurrency regression test flagged by an independent security review
of ``app.services.order_expiry``: does the stale-order sweep ever race a
REAL Mollie webhook payment confirmation for the same Order, and if so,
can the sweep discard a payment that genuinely just succeeded?

Both paths share ``app.services.order_payment._lock_order`` (row-locked,
``populate_existing=True`` — the exact fix already validated for this
codebase's earlier identity-map staleness bugs), so whichever side
acquires the lock first should win, and the OTHER side's function
(``mark_order_paid`` no-ops if already ``paid``;
``release_order_stock``/the sweep's resurrection path via
``mark_order_paid``'s ``CANCELLED``/``EXPIRED`` recovery no-ops or
re-resolves correctly) should never leave a real payment lost. This test
proves that directly, with a real Order whose ``created_at`` is
deliberately back-dated well past the sweep's TTL — simulating a checkout
that LOOKS abandoned right up until the buyer's payment actually lands —
racing a genuine concurrent sweep call via ``asyncio.gather``, not a
simulated/sequential approximation.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.checkout as checkout_module
from app.db.session import async_session_factory
from app.models.enums import OrderStatus, PaymentMethod, PublishStatus
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.order import Order
from app.models.show import Show
from app.models.ticket_type import TicketType
from app.services.mollie import MolliePaymentCreated
from app.services.order_expiry import sweep_stale_orders
from app.services.stock import sold_counts_for_ticket_types

_PAST = datetime.now(UTC) - timedelta(days=1)
_STALE_CREATED_AT = datetime.now(UTC) - timedelta(hours=12)
_ITERATIONS = 3


async def test_sweep_racing_a_real_webhook_never_loses_a_genuine_payment(
    client: AsyncClient,
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for iteration in range(_ITERATIONS):
        payment_id = f"tr_test_{uuid.uuid4().hex[:16]}"

        async def _fake_create_mollie_payment(
            _payment_id: str = payment_id, **kwargs: object
        ) -> MolliePaymentCreated:
            return MolliePaymentCreated(payment_id=_payment_id, checkout_url="https://www.mollie.com/checkout/fake")

        monkeypatch.setattr(checkout_module, "create_mollie_payment", _fake_create_mollie_payment)

        event = await make_event(status=PublishStatus.PUBLISHED)
        show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
        ticket_type = await make_ticket_type(show_id=show.id, quantity_available=1)
        await make_event_config(
            event_id=event.id,
            sales_live_at=_PAST,
            mollie_test_api_key="test_dummy_key_never_used_over_network",
            enabled_payment_methods=[PaymentMethod.MOLLIE],
        )

        response = await client.post(
            "/api/v1/public/checkout",
            json={
                "buyer_name": "Race Buyer",
                "buyer_email": f"race-{iteration}-{uuid.uuid4().hex}@example.test",
                "buyer_address": "1 Test Street",
                "language": "en",
                "payment_method": "mollie",
                "items": [{"ticket_type_id": str(ticket_type.id), "quantity": 1}],
            },
        )
        assert response.status_code == 201, response.text
        order_id = uuid.UUID(response.json()["id"])

        # Back-date created_at well past the sweep's default 6h TTL --
        # this Order now LOOKS like a stale, abandoned checkout, right up
        # until the buyer's real payment confirmation arrives.
        order = await db_session.get(Order, order_id)
        assert order is not None
        order.created_at = _STALE_CREATED_AT
        await db_session.commit()

        async def _fake_fetch(*, api_key: str, payment_id: str) -> str:
            return "paid"

        monkeypatch.setattr("app.api.routes.public.fetch_mollie_payment_status", _fake_fetch)

        async def _run_sweep() -> None:
            async with async_session_factory() as sweep_session:
                await sweep_stale_orders(sweep_session, now=datetime.now(UTC))
                await sweep_session.commit()

        webhook_response, _ = await asyncio.gather(
            client.post("/api/v1/public/mollie-webhook", data={"id": payment_id}),
            _run_sweep(),
        )

        assert webhook_response.status_code == 200, (
            f"iteration {iteration}: a retried/raced webhook must never surface as a failure — {webhook_response.text}"
        )

        await db_session.refresh(order)
        assert order.status == OrderStatus.PAID, (
            f"iteration {iteration}: a real, successful payment must never be lost to a concurrently-running "
            f"stale-order sweep, regardless of which side won the row lock — got {order.status}"
        )

        sold = await sold_counts_for_ticket_types(db_session, [ticket_type.id])
        assert sold.get(ticket_type.id, 0) == 1, (
            f"iteration {iteration}: exactly one ticket must still count as sold — got {sold}"
        )
