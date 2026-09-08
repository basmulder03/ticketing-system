"""THE concurrency test for ``app.services.order_payment.mark_order_paid``'s
idempotency guarantee — PROJECT_BRIEF.md's "a retried webhook must never
double-issue tickets" requirement, exercised at the level that actually
matters: genuinely concurrent duplicate deliveries, not sequential ones.

``test_mollie_webhook.py``'s
``test_duplicate_paid_webhook_delivery_is_idempotent`` already covers
SEQUENTIAL retries (one request fully completes — commits — before the
second is sent). That does NOT exercise the identity-map bug this file
was written to close permanently: each sequential request gets a brand
new ``AsyncSession`` with an empty identity map, so
``app.services.order_payment._lock_order``'s ``SELECT ... FOR UPDATE``
never has a stale, already-loaded ``Order`` object sitting in the same
session's identity map to return unrefreshed.

Genuinely concurrent duplicate deliveries do: the webhook route
(``app.api.routes.public.mollie_webhook``) reads the ``Order`` via a PLAIN
(non-locking) query by ``mollie_payment_id`` BEFORE calling
``mark_order_paid``, populating that request's session's identity map.
When N duplicate deliveries for the SAME Order fire together, N-1 of them
block on ``_lock_order``'s row lock until the first commits — and, without
``execution_options(populate_existing=True)`` on that locking query (fixed
in ``app.services.order_payment._lock_order`` — see its docstring for the
full bug writeup), every blocked request would resume holding the STALE
pre-lock ``PENDING`` status it cached during its own earlier plain read,
not the fresh ``PAID`` status the row lock just gave it exclusive,
up-to-date access to. That produced multiple ``already_paid=False``
results and multiple ``order.mark_paid`` audit entries for one Order under
real concurrency — reproduced directly via an isolated script before the
fix, confirmed closed after. This test proves it's closed at the real
HTTP request-handling layer, not just in a scratch repro.

Structure mirrors ``test_invoicing_concurrency.py``: real HTTP requests via
``asyncio.gather`` against the real ``POST /api/v1/public/mollie-webhook``
route, repeated across several fresh Orders/iterations so a flaky/
false-green pass is unlikely to slip through in CI.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import aiosmtplib
import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.checkout as checkout_module
from app.models.audit_log import AuditLogEntry
from app.models.enums import OrderStatus, PaymentMethod, PublishStatus
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.order import Order
from app.models.show import Show
from app.models.ticket_type import TicketType
from app.services.mollie import MolliePaymentCreated

_PAST = datetime.now(UTC) - timedelta(days=1)
_CONCURRENT_DELIVERIES = 8
_ITERATIONS = 3


async def _mark_paid_audit_count(db_session: AsyncSession, order_id: uuid.UUID) -> int:
    result = await db_session.execute(
        select(func.count())
        .select_from(AuditLogEntry)
        .where(AuditLogEntry.action == "order.mark_paid")
        .where(AuditLogEntry.target_id == str(order_id))
    )
    return result.scalar_one()


async def test_concurrent_duplicate_webhook_deliveries_for_one_order_are_idempotent(
    db_session: AsyncSession,
    client_factory: Callable[[str | None], AsyncClient],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_send(message: object, **kwargs: object) -> tuple[dict[str, object], str]:
        return {}, "OK"

    monkeypatch.setattr(aiosmtplib, "send", _fake_send)

    for iteration in range(_ITERATIONS):
        event = await make_event(status=PublishStatus.PUBLISHED)
        show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
        ticket_type = await make_ticket_type(show_id=show.id, quantity_available=5)
        await make_event_config(
            event_id=event.id,
            sales_live_at=_PAST,
            mollie_test_api_key="test_dummy_key_never_used_over_network",
            enabled_payment_methods=[PaymentMethod.MOLLIE],
        )

        payment_id = f"tr_test_{uuid.uuid4().hex[:16]}"

        async def _fake_create_mollie_payment(
            _payment_id: str = payment_id, **kwargs: object
        ) -> MolliePaymentCreated:
            return MolliePaymentCreated(payment_id=_payment_id, checkout_url="https://www.mollie.com/checkout/fake")

        monkeypatch.setattr(checkout_module, "create_mollie_payment", _fake_create_mollie_payment)

        async with client_factory(f"order-paid-race-{iteration}-{uuid.uuid4().hex[:8]}") as client:
            response = await client.post(
                "/api/v1/public/checkout",
                json={
                    "buyer_name": "Concurrent Buyer",
                    "buyer_email": f"order-paid-race-{iteration}-{uuid.uuid4().hex}@example.test",
                    "buyer_address": "1 Race Condition Lane",
                    "language": "en",
                    "payment_method": "mollie",
                    "items": [{"ticket_type_id": str(ticket_type.id), "quantity": 1}],
                },
            )
            assert response.status_code == 201, response.text
            body = response.json()
            assert body["status"] == "pending"
            order_id = uuid.UUID(body["id"])

            async def _fake_fetch(*, api_key: str, payment_id: str) -> str:
                return "paid"

            monkeypatch.setattr("app.api.routes.public.fetch_mollie_payment_status", _fake_fetch)

            # THE concurrent part: N genuinely simultaneous webhook
            # deliveries, all for the SAME Order/payment id — every one of
            # them races on _lock_order's row-locked Order read.
            responses = await asyncio.gather(
                *[
                    client.post("/api/v1/public/mollie-webhook", data={"id": payment_id})
                    for _ in range(_CONCURRENT_DELIVERIES)
                ]
            )

        statuses = sorted(r.status_code for r in responses)
        assert all(r.status_code == 200 for r in responses), (
            f"iteration {iteration}: expected all 200s (a retried webhook must never surface as a failure to "
            f"Mollie), got statuses={statuses}"
        )

        result = await db_session.execute(select(Order).where(Order.id == order_id))
        order = result.scalar_one()
        assert order.status == OrderStatus.PAID

        audit_count = await _mark_paid_audit_count(db_session, order_id)
        assert audit_count == 1, (
            f"iteration {iteration}: expected exactly ONE order.mark_paid audit entry despite "
            f"{_CONCURRENT_DELIVERIES} concurrent duplicate deliveries, got {audit_count}"
        )
