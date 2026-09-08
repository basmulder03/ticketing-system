"""Gating correctness for Milestone 5 Invoice issuance, exercised through
the real Mollie webhook route (``app.api.routes.public.mollie_webhook``) —
mirrors ``tests/integration/test_mollie_webhook.py``'s mocking-boundary
decisions (``create_mollie_payment``/``fetch_mollie_payment_status``
monkeypatched, no real Mollie account).

Proves: an Invoice is only ever created once an Order genuinely becomes
``paid`` — never for ``pending`` (still in progress), ``cancelled``, or
``expired`` — and never more than once for the same Order regardless of how
many times payment-confirmation logic runs against it (a retried webhook
delivery).
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.checkout as checkout_module
from app.models.enums import OrderStatus, PaymentMethod, PublishStatus
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.invoice import Invoice
from app.models.order import Order
from app.models.show import Show
from app.models.ticket_type import TicketType
from app.services.mollie import MolliePaymentCreated

_PAST = datetime.now(UTC) - timedelta(days=1)


async def _setup_mollie_pending_order(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> tuple[Order, str]:
    payment_id = f"tr_test_{uuid.uuid4().hex[:16]}"

    async def _fake_create_mollie_payment(**kwargs: object) -> MolliePaymentCreated:
        return MolliePaymentCreated(payment_id=payment_id, checkout_url="https://www.mollie.com/checkout/fake")

    monkeypatch.setattr(checkout_module, "create_mollie_payment", _fake_create_mollie_payment)

    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=5)
    await make_event_config(
        event_id=event.id,
        sales_live_at=_PAST,
        mollie_test_api_key="test_dummy_key_never_used_over_network",
        enabled_payment_methods=[PaymentMethod.MOLLIE],
    )

    response = await client.post(
        "/api/v1/public/checkout",
        json={
            "buyer_name": "Buyer",
            "buyer_email": f"buyer-{uuid.uuid4().hex}@example.test",
            "buyer_address": "1 Test Street",
            "language": "en",
            "payment_method": "mollie",
            "items": [{"ticket_type_id": str(ticket_type.id), "quantity": 1}],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "pending"

    result = await db_session.execute(select(Order).where(Order.id == uuid.UUID(body["id"])))
    order = result.scalar_one()
    return order, payment_id


def _patch_fetch_status(monkeypatch: pytest.MonkeyPatch, status_value: str) -> None:
    async def _fake_fetch(*, api_key: str, payment_id: str) -> str:
        return status_value

    monkeypatch.setattr("app.api.routes.public.fetch_mollie_payment_status", _fake_fetch)


async def _invoice_count_for_order(session: AsyncSession, order_id: uuid.UUID) -> int:
    result = await session.execute(select(func.count()).select_from(Invoice).where(Invoice.order_id == order_id))
    return result.scalar_one()


# --- No Invoice for a non-paid Order ----------------------------------------


@pytest.mark.parametrize("in_progress_status", ["open", "pending", "authorized"])
async def test_no_invoice_is_created_while_the_order_is_still_pending(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
    in_progress_status: str,
) -> None:
    order, payment_id = await _setup_mollie_pending_order(
        client, db_session, monkeypatch, make_event, make_show, make_ticket_type, make_event_config
    )
    _patch_fetch_status(monkeypatch, in_progress_status)

    response = await client.post("/api/v1/public/mollie-webhook", data={"id": payment_id})
    assert response.status_code == 200

    await db_session.refresh(order)
    assert order.status == OrderStatus.PENDING
    assert await _invoice_count_for_order(db_session, order.id) == 0


@pytest.mark.parametrize(
    ("mollie_status", "expected_order_status"),
    [
        ("failed", OrderStatus.CANCELLED),
        ("canceled", OrderStatus.CANCELLED),
        ("expired", OrderStatus.EXPIRED),
    ],
)
async def test_no_invoice_is_created_for_a_failed_cancelled_or_expired_order(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
    mollie_status: str,
    expected_order_status: OrderStatus,
) -> None:
    order, payment_id = await _setup_mollie_pending_order(
        client, db_session, monkeypatch, make_event, make_show, make_ticket_type, make_event_config
    )
    _patch_fetch_status(monkeypatch, mollie_status)

    response = await client.post("/api/v1/public/mollie-webhook", data={"id": payment_id})
    assert response.status_code == 200

    await db_session.refresh(order)
    assert order.status == expected_order_status
    assert await _invoice_count_for_order(db_session, order.id) == 0


# --- Exactly one Invoice ever, no matter how many confirmation attempts ----


async def test_duplicate_paid_webhook_deliveries_result_in_exactly_one_invoice(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """Mirrors ``test_mollie_webhook.py``'s idempotency pattern, applied to
    Invoice issuance specifically (rather than just ``Order.status``)."""
    order, payment_id = await _setup_mollie_pending_order(
        client, db_session, monkeypatch, make_event, make_show, make_ticket_type, make_event_config
    )
    _patch_fetch_status(monkeypatch, "paid")

    first = await client.post("/api/v1/public/mollie-webhook", data={"id": payment_id})
    assert first.status_code == 200
    await db_session.refresh(order)
    assert order.status == OrderStatus.PAID
    assert await _invoice_count_for_order(db_session, order.id) == 1

    result = await db_session.execute(select(Invoice).where(Invoice.order_id == order.id))
    first_invoice = result.scalar_one()

    second = await client.post("/api/v1/public/mollie-webhook", data={"id": payment_id})
    assert second.status_code == 200

    assert await _invoice_count_for_order(db_session, order.id) == 1

    result = await db_session.execute(select(Invoice).where(Invoice.order_id == order.id))
    second_invoice = result.scalar_one()
    assert second_invoice.id == first_invoice.id
    assert second_invoice.number == first_invoice.number


async def test_triplicate_paid_webhook_deliveries_still_yield_exactly_one_invoice(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    order, payment_id = await _setup_mollie_pending_order(
        client, db_session, monkeypatch, make_event, make_show, make_ticket_type, make_event_config
    )
    _patch_fetch_status(monkeypatch, "paid")

    for _ in range(3):
        response = await client.post("/api/v1/public/mollie-webhook", data={"id": payment_id})
        assert response.status_code == 200

    assert await _invoice_count_for_order(db_session, order.id) == 1
