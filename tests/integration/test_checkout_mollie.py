"""Integration tests for the Mollie-specific parts of checkout
(``app.services.checkout._initiate_mollie_payment``): the preview-mode
simulated/sandbox payment path, and rollback-on-payment-initiation-failure.

Mocking-boundary decision: the simulated-sandbox-path tests need to prove a
*real* Mollie call is never even attempted, which a real network call
(however predictably it would fail) can't demonstrate — so those
monkeypatch ``app.services.checkout.create_mollie_payment`` to raise if
called at all, turning "no external call attempted" into something the
test actively enforces rather than just hopes for. The
payment-initiation-*failure*-and-rollback test, by contrast, makes a real
network call to Mollie's real API with a deliberately invalid key — no
mock — matching this project's established precedent
(``tests/integration/test_event_configs_routes.py``'s Mollie
connection-test coverage): a bad key predictably and quickly gets rejected
by Mollie's own API, so there's no need to fabricate that response, and it
proves the real ``httpx`` error-handling path in
``app.services.mollie.create_mollie_payment`` end-to-end. Never a call that
could create a real Mollie payment (POSTing with a bad key never succeeds
in creating one).
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.checkout as checkout_module
from app.models.enums import PaymentMethod, PublishStatus
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.order import Order
from app.models.show import Show
from app.models.ticket_type import TicketType
from app.services.mollie import MolliePaymentCreated
from app.services.stock import sold_counts_for_ticket_types

_PAST = datetime.now(UTC) - timedelta(days=1)


def _checkout_payload(ticket_type_id: object, *, preview_token: str | None) -> dict[str, object]:
    return {
        "buyer_name": "Buyer",
        "buyer_email": "buyer@example.test",
        "buyer_address": "1 Test Street",
        "language": "en",
        "payment_method": "mollie",
        "items": [{"ticket_type_id": str(ticket_type_id), "quantity": 1}],
        "preview_token": preview_token,
    }


def _forbid_real_mollie_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard that fails the test loudly if the real Mollie create-payment
    call is ever attempted — the simulated-sandbox path must never reach
    it."""

    async def _boom(**kwargs: object) -> MolliePaymentCreated:
        raise AssertionError(
            "create_mollie_payment must not be called for the preview-mode simulated-payment path"
        )

    monkeypatch.setattr(checkout_module, "create_mollie_payment", _boom)


# --- Sandbox eligible: draft + valid preview token + no key at all ---------


async def test_draft_preview_mollie_checkout_simulates_paid_with_no_external_call(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    _forbid_real_mollie_calls(monkeypatch)

    event = await make_event(status=PublishStatus.DRAFT)
    show = await make_show(event_id=event.id, status=PublishStatus.DRAFT)
    ticket_type = await make_ticket_type(show_id=show.id)
    # No Mollie key configured at all.
    await make_event_config(
        event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.MOLLIE]
    )

    response = await client.post(
        "/api/v1/public/checkout",
        json=_checkout_payload(ticket_type.id, preview_token=event.preview_token),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "paid"
    assert body["mollie_checkout_url"] is None


async def test_draft_preview_mollie_checkout_simulation_writes_no_mollie_payment_id(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    _forbid_real_mollie_calls(monkeypatch)

    event = await make_event(status=PublishStatus.DRAFT)
    show = await make_show(event_id=event.id, status=PublishStatus.DRAFT)
    ticket_type = await make_ticket_type(show_id=show.id)
    await make_event_config(
        event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.MOLLIE]
    )

    response = await client.post(
        "/api/v1/public/checkout",
        json=_checkout_payload(ticket_type.id, preview_token=event.preview_token),
    )
    assert response.status_code == 201

    result = await db_session.execute(select(Order).where(Order.id == uuid.UUID(response.json()["id"])))
    order = result.scalar_one()
    assert order.mollie_payment_id is None


# --- Sandbox NOT eligible once published, even with a technically-valid
# (but now-irrelevant) preview token -----------------------------------------


async def test_published_event_mollie_checkout_with_stale_preview_token_does_not_simulate(
    client: AsyncClient,
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """Mirrors Milestone 2's established 'preview token doesn't bypass
    sales gates once published' pattern: the same no-key-configured
    scenario against a fully published Event/Show must NOT auto-simulate a
    paid order — it must cleanly reject (502, no Mollie key to even
    attempt a call with), never silently issue a paid order for a real
    buyer."""
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=5)
    # No Mollie key configured at all — same as the draft/sandbox case above.
    await make_event_config(
        event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.MOLLIE]
    )

    response = await client.post(
        "/api/v1/public/checkout",
        # A genuinely valid token (matches event.preview_token) — but the
        # event is published, so it must grant no special treatment at all.
        json=_checkout_payload(ticket_type.id, preview_token=event.preview_token),
    )

    assert response.status_code == 502
    assert "Mollie" in response.json()["detail"]

    orders = await db_session.execute(select(Order).where(Order.event_id == event.id))
    assert orders.scalars().all() == [], "no order should be left behind by a rejected checkout"

    sold = await sold_counts_for_ticket_types(db_session, [ticket_type.id])
    assert sold.get(ticket_type.id, 0) == 0


# --- Checkout rollback on a real payment-initiation failure -----------------


async def test_checkout_rolls_back_entirely_when_mollie_rejects_the_payment_request(
    client: AsyncClient,
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """Real network call to Mollie's real API with an obviously-invalid
    key (published event — no preview-sandbox path applies): Mollie
    rejects the create-payment request, and the ENTIRE checkout
    transaction — including the stock reservation — must roll back. No
    Order row persists, and the TicketType's remaining stock is
    unaffected."""
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=5)
    await make_event_config(
        event_id=event.id,
        sales_live_at=_PAST,
        mollie_test_api_key="test_this_is_definitely_not_a_real_mollie_key_000",
        enabled_payment_methods=[PaymentMethod.MOLLIE],
    )

    sold_before = await sold_counts_for_ticket_types(db_session, [ticket_type.id])
    assert sold_before.get(ticket_type.id, 0) == 0

    response = await client.post(
        "/api/v1/public/checkout", json=_checkout_payload(ticket_type.id, preview_token=None)
    )

    assert response.status_code == 502, response.text

    orders_count = await db_session.execute(
        select(func.count()).select_from(Order).where(Order.event_id == event.id)
    )
    assert orders_count.scalar_one() == 0, "no Order row should persist after a rolled-back checkout"

    sold_after = await sold_counts_for_ticket_types(db_session, [ticket_type.id])
    assert sold_after.get(ticket_type.id, 0) == 0, "stock reservation must have been rolled back too"
