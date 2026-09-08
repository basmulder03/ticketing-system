"""THE concurrency test for Milestone 5's sequential invoice numbering —
this milestone's equivalent of ``test_checkout_concurrency.py``'s
stock-decrement race test, explicitly required by PROJECT_BRIEF.md's
Testing section ("a duplicate/colliding invoice number is a real accounting
problem"). Structure deliberately mirrors that file closely.

Fires genuinely concurrent HTTP requests (via ``asyncio.gather``) at the
real ``POST /api/v1/public/mollie-webhook`` route over the ASGI app, one
per pre-created ``pending`` Order (each with its own Mollie payment id) for
the SAME Event. Each request resolves ``app.db.session.get_session``
independently (a fresh ``AsyncSession``/asyncpg connection per request, from
the shared pool), so this exercises real overlapping Postgres transactions
and the real ``SELECT ... FOR UPDATE`` row lock on ``EventConfig`` in
``app.services.invoicing._allocate_invoice_number`` — not an in-process mock
and not sequential calls dressed up to look concurrent.

Mocking-boundary decisions mirror ``test_mollie_webhook.py``/
``test_ticket_delivery.py``: ``create_mollie_payment``/
``fetch_mollie_payment_status`` are monkeypatched (no real Mollie account),
and ``aiosmtplib.send`` is monkeypatched to a fast no-op success — the
webhook route dispatches the confirmation email (with a real weasyprint PDF
render) as part of its normal flow, and that email step is orthogonal to
what this test verifies (invoice-number allocation happens strictly BEFORE
the transaction commits, inside the same DB transaction as the payment-
status flip — see ``app.services.invoicing`` module docstring), so mocking
the SMTP send keeps this test's runtime and flakiness surface focused on
the actual race being tested.

The whole scenario is repeated across several fresh Events (each with its
own client, to stay under the per-IP checkout rate limit) so a flaky/
false-green pass is unlikely to slip through in CI — same discipline
``test_checkout_concurrency.py`` uses.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import aiosmtplib
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.checkout as checkout_module
from app.models.enums import PaymentMethod, PublishStatus
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.invoice import Invoice
from app.models.show import Show
from app.models.ticket_type import TicketType
from app.services.mollie import MolliePaymentCreated

_PAST = datetime.now(UTC) - timedelta(days=1)
_CONCURRENT_ORDERS = 8
_ITERATIONS = 3


def _checkout_payload(ticket_type_id: uuid.UUID, buyer_suffix: str) -> dict[str, object]:
    return {
        "buyer_name": f"Concurrent Buyer {buyer_suffix}",
        "buyer_email": f"invoice-race-{buyer_suffix}-{uuid.uuid4().hex}@example.test",
        "buyer_address": "1 Race Condition Lane",
        "language": "en",
        "payment_method": "mollie",
        "items": [{"ticket_type_id": str(ticket_type_id), "quantity": 1}],
    }


async def test_concurrent_payment_confirmations_yield_unique_gapless_invoice_numbers(
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
        # Several TicketTypes under the same Show/Event, per the milestone's
        # concurrency-test guidance — orders are spread across both so this
        # isn't just "one TicketType's row lock", proving the numbering race
        # is genuinely scoped to the EventConfig, not incidentally to a
        # single TicketType's own stock lock.
        ticket_type_a = await make_ticket_type(
            show_id=show.id, name="Adult", quantity_available=_CONCURRENT_ORDERS
        )
        ticket_type_b = await make_ticket_type(
            show_id=show.id, name="Child", quantity_available=_CONCURRENT_ORDERS
        )
        await make_event_config(
            event_id=event.id,
            sales_live_at=_PAST,
            mollie_test_api_key="test_dummy_key_never_used_over_network",
            enabled_payment_methods=[PaymentMethod.MOLLIE],
        )

        async with client_factory(f"invoice-race-{iteration}-{uuid.uuid4().hex[:8]}") as client:
            # Sequentially create N distinct `pending` Orders (each with its
            # own Mollie payment id) via the real checkout route — this part
            # is deliberately NOT concurrent; only the payment-confirmation
            # step below is the thing under test, mirroring how
            # test_checkout_concurrency.py only fires the checkout itself
            # concurrently, not its setup.
            payment_ids: list[str] = []
            order_ids: list[str] = []
            for i in range(_CONCURRENT_ORDERS):
                payment_id = f"tr_test_{uuid.uuid4().hex[:16]}"

                async def _fake_create_mollie_payment(
                    _payment_id: str = payment_id, **kwargs: object
                ) -> MolliePaymentCreated:
                    return MolliePaymentCreated(
                        payment_id=_payment_id, checkout_url="https://www.mollie.com/checkout/fake"
                    )

                monkeypatch.setattr(checkout_module, "create_mollie_payment", _fake_create_mollie_payment)

                ticket_type = ticket_type_a if i % 2 == 0 else ticket_type_b
                response = await client.post(
                    "/api/v1/public/checkout",
                    json=_checkout_payload(ticket_type.id, f"{iteration}-{i}"),
                )
                assert response.status_code == 201, response.text
                body = response.json()
                assert body["status"] == "pending"
                payment_ids.append(payment_id)
                order_ids.append(body["id"])

            assert len(set(payment_ids)) == _CONCURRENT_ORDERS
            assert len(set(order_ids)) == _CONCURRENT_ORDERS

            async def _fake_fetch(*, api_key: str, payment_id: str) -> str:
                return "paid"

            monkeypatch.setattr("app.api.routes.public.fetch_mollie_payment_status", _fake_fetch)

            # THE concurrent part: N genuinely simultaneous webhook
            # deliveries, each confirming a DIFFERENT Order for the SAME
            # Event — every one of them must hit
            # _allocate_invoice_number's row-locked EventConfig read, so
            # only one can proceed at a time despite firing together.
            responses = await asyncio.gather(
                *[client.post("/api/v1/public/mollie-webhook", data={"id": pid}) for pid in payment_ids]
            )

        statuses = sorted(r.status_code for r in responses)
        assert all(r.status_code == 200 for r in responses), (
            f"iteration {iteration}: expected all 200s, got statuses={statuses}"
        )

        result = await db_session.execute(select(Invoice).where(Invoice.event_id == event.id))
        invoices = result.scalars().all()
        assert len(invoices) == _CONCURRENT_ORDERS, (
            f"iteration {iteration}: expected exactly {_CONCURRENT_ORDERS} Invoice rows, "
            f"got {len(invoices)}"
        )

        numbers = sorted(inv.number for inv in invoices)
        assert len(set(numbers)) == _CONCURRENT_ORDERS, (
            f"iteration {iteration}: invoice numbers must be unique, got {numbers}"
        )
        assert numbers == list(range(1, _CONCURRENT_ORDERS + 1)), (
            f"iteration {iteration}: expected a gapless 1..{_CONCURRENT_ORDERS} sequence "
            f"(a rolled-back attempt never allocates a number at all), got {numbers}"
        )

        # Each Invoice belongs to exactly one of this iteration's Orders —
        # no Invoice was issued twice for the same Order, and none leaked
        # onto an unrelated one.
        invoice_order_ids = {str(inv.order_id) for inv in invoices}
        assert invoice_order_ids == set(order_ids)

        config_result = await db_session.execute(select(EventConfig).where(EventConfig.event_id == event.id))
        config = config_result.scalar_one()
        assert config.next_invoice_number == _CONCURRENT_ORDERS + 1, (
            f"iteration {iteration}: expected next_invoice_number == N+1, got {config.next_invoice_number}"
        )
