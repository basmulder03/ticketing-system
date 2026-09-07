"""Checkout validation order and error-type coverage for
``app.services.checkout.perform_checkout`` (Milestone 2), exercised both
directly against the service (for exact error-type/attribute assertions)
and through the real ``POST /api/v1/public/checkout`` route (for the HTTP
status/detail every ``CheckoutError`` subclass actually translates to —
see ``app.services.checkout.CheckoutError`` and
``app.api.routes.public.checkout``).
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import PaymentMethod, PublishStatus
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.show import Show
from app.models.ticket_type import TicketType
from app.services.checkout import (
    CheckoutItemInput,
    EventNotAvailableCheckoutError,
    InsufficientStockCheckoutError,
    MixedShowCheckoutError,
    PaymentMethodNotEnabledCheckoutError,
    SalesNotLiveCheckoutError,
    SalesPausedCheckoutError,
    TicketTypesNotFoundCheckoutError,
    perform_checkout,
)

_PAST = datetime.now(UTC) - timedelta(days=1)
_FUTURE = datetime.now(UTC) + timedelta(days=1)


def _payload(
    ticket_type_ids: list[uuid.UUID],
    *,
    payment_method: PaymentMethod = PaymentMethod.DOOR,
    preview_token: str | None = None,
    quantity: int = 1,
) -> dict[str, object]:
    return {
        "buyer_name": "Buyer",
        "buyer_email": "buyer@example.test",
        "buyer_address": "1 Test Street",
        "language": "en",
        "payment_method": payment_method.value,
        "items": [{"ticket_type_id": str(tid), "quantity": quantity} for tid in ticket_type_ids],
        "preview_token": preview_token,
    }


async def _live_setup(
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
    *,
    quantity_available: int = 5,
    enabled_payment_methods: list[PaymentMethod] | None = None,
) -> tuple[Event, Show, TicketType]:
    """A published Event/Show with one TicketType and a live-sales config —
    the baseline "everything should succeed" fixture each rejection test
    tweaks one thing on."""
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=quantity_available)
    await make_event_config(
        event_id=event.id,
        sales_live_at=_PAST,
        enabled_payment_methods=enabled_payment_methods
        if enabled_payment_methods is not None
        else [PaymentMethod.DOOR, PaymentMethod.MOLLIE],
    )
    return event, show, ticket_type


# --- Direct service-level tests: exact error types/attributes ------------


async def test_perform_checkout_happy_path_creates_pending_order(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    _, _, ticket_type = await _live_setup(make_event, make_show, make_ticket_type, make_event_config)

    order = await perform_checkout(
        db_session,
        items=[CheckoutItemInput(ticket_type_id=ticket_type.id, quantity=2)],
        buyer_name="Buyer",
        buyer_email="buyer@example.test",
        buyer_address="1 Test Street",
        language="en",
        payment_method=PaymentMethod.DOOR,
        preview_token=None,
    )
    await db_session.commit()
    assert order.total == ticket_type.price * 2


async def test_ticket_type_not_found_raises_typed_error(db_session: AsyncSession) -> None:
    with pytest.raises(TicketTypesNotFoundCheckoutError):
        await perform_checkout(
            db_session,
            items=[CheckoutItemInput(ticket_type_id=uuid.uuid4(), quantity=1)],
            buyer_name="Buyer",
            buyer_email="buyer@example.test",
            buyer_address="1 Test Street",
            language="en",
            payment_method=PaymentMethod.DOOR,
            preview_token=None,
        )


async def test_mixed_show_order_is_rejected(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    show_a = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    show_b = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    tt_a = await make_ticket_type(show_id=show_a.id)
    tt_b = await make_ticket_type(show_id=show_b.id)
    await make_event_config(
        event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR]
    )

    with pytest.raises(MixedShowCheckoutError):
        await perform_checkout(
            db_session,
            items=[
                CheckoutItemInput(ticket_type_id=tt_a.id, quantity=1),
                CheckoutItemInput(ticket_type_id=tt_b.id, quantity=1),
            ],
            buyer_name="Buyer",
            buyer_email="buyer@example.test",
            buyer_address="1 Test Street",
            language="en",
            payment_method=PaymentMethod.DOOR,
            preview_token=None,
        )


async def test_draft_event_without_preview_token_is_rejected(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await make_event(status=PublishStatus.DRAFT)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id)
    await make_event_config(
        event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR]
    )

    with pytest.raises(EventNotAvailableCheckoutError):
        await perform_checkout(
            db_session,
            items=[CheckoutItemInput(ticket_type_id=ticket_type.id, quantity=1)],
            buyer_name="Buyer",
            buyer_email="buyer@example.test",
            buyer_address="1 Test Street",
            language="en",
            payment_method=PaymentMethod.DOOR,
            preview_token=None,
        )


async def test_draft_show_without_preview_token_is_rejected(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.DRAFT)
    ticket_type = await make_ticket_type(show_id=show.id)
    await make_event_config(
        event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR]
    )

    with pytest.raises(EventNotAvailableCheckoutError):
        await perform_checkout(
            db_session,
            items=[CheckoutItemInput(ticket_type_id=ticket_type.id, quantity=1)],
            buyer_name="Buyer",
            buyer_email="buyer@example.test",
            buyer_address="1 Test Street",
            language="en",
            payment_method=PaymentMethod.DOOR,
            preview_token=None,
        )


async def test_valid_preview_token_bypasses_draft_sales_live_and_paused_gates(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await make_event(status=PublishStatus.DRAFT, sales_paused=True)
    show = await make_show(event_id=event.id, status=PublishStatus.DRAFT)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=1)
    await make_event_config(
        event_id=event.id, sales_live_at=_FUTURE, enabled_payment_methods=[PaymentMethod.DOOR]
    )

    order = await perform_checkout(
        db_session,
        items=[CheckoutItemInput(ticket_type_id=ticket_type.id, quantity=1)],
        buyer_name="Buyer",
        buyer_email="buyer@example.test",
        buyer_address="1 Test Street",
        language="en",
        payment_method=PaymentMethod.DOOR,
        preview_token=event.preview_token,
    )
    await db_session.commit()
    assert order.id is not None


async def test_preview_token_still_enforces_payment_method_enabled(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await make_event(status=PublishStatus.DRAFT)
    show = await make_show(event_id=event.id, status=PublishStatus.DRAFT)
    ticket_type = await make_ticket_type(show_id=show.id)
    # Mollie not enabled — only door is.
    await make_event_config(
        event_id=event.id, sales_live_at=_FUTURE, enabled_payment_methods=[PaymentMethod.DOOR]
    )

    with pytest.raises(PaymentMethodNotEnabledCheckoutError):
        await perform_checkout(
            db_session,
            items=[CheckoutItemInput(ticket_type_id=ticket_type.id, quantity=1)],
            buyer_name="Buyer",
            buyer_email="buyer@example.test",
            buyer_address="1 Test Street",
            language="en",
            payment_method=PaymentMethod.MOLLIE,
            preview_token=event.preview_token,
        )


async def test_preview_token_still_enforces_stock(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await make_event(status=PublishStatus.DRAFT)
    show = await make_show(event_id=event.id, status=PublishStatus.DRAFT)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=1)
    await make_event_config(
        event_id=event.id, sales_live_at=_FUTURE, enabled_payment_methods=[PaymentMethod.DOOR]
    )

    with pytest.raises(InsufficientStockCheckoutError) as exc_info:
        await perform_checkout(
            db_session,
            items=[CheckoutItemInput(ticket_type_id=ticket_type.id, quantity=5)],
            buyer_name="Buyer",
            buyer_email="buyer@example.test",
            buyer_address="1 Test Street",
            language="en",
            payment_method=PaymentMethod.DOOR,
            preview_token=event.preview_token,
        )
    assert exc_info.value.remaining >= 0


async def test_wrong_preview_token_does_not_bypass_draft_gate(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await make_event(status=PublishStatus.DRAFT)
    show = await make_show(event_id=event.id, status=PublishStatus.DRAFT)
    ticket_type = await make_ticket_type(show_id=show.id)
    await make_event_config(
        event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR]
    )

    with pytest.raises(EventNotAvailableCheckoutError):
        await perform_checkout(
            db_session,
            items=[CheckoutItemInput(ticket_type_id=ticket_type.id, quantity=1)],
            buyer_name="Buyer",
            buyer_email="buyer@example.test",
            buyer_address="1 Test Street",
            language="en",
            payment_method=PaymentMethod.DOOR,
            preview_token="not-the-real-token",
        )


async def test_sales_not_live_yet_is_rejected(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id)
    await make_event_config(
        event_id=event.id, sales_live_at=_FUTURE, enabled_payment_methods=[PaymentMethod.DOOR]
    )

    with pytest.raises(SalesNotLiveCheckoutError):
        await perform_checkout(
            db_session,
            items=[CheckoutItemInput(ticket_type_id=ticket_type.id, quantity=1)],
            buyer_name="Buyer",
            buyer_email="buyer@example.test",
            buyer_address="1 Test Street",
            language="en",
            payment_method=PaymentMethod.DOOR,
            preview_token=None,
        )


async def test_sales_paused_is_rejected(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED, sales_paused=True)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id)
    await make_event_config(
        event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR]
    )

    with pytest.raises(SalesPausedCheckoutError):
        await perform_checkout(
            db_session,
            items=[CheckoutItemInput(ticket_type_id=ticket_type.id, quantity=1)],
            buyer_name="Buyer",
            buyer_email="buyer@example.test",
            buyer_address="1 Test Street",
            language="en",
            payment_method=PaymentMethod.DOOR,
            preview_token=None,
        )


async def test_payment_method_not_enabled_is_rejected(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    _, _, ticket_type = await _live_setup(
        make_event,
        make_show,
        make_ticket_type,
        make_event_config,
        enabled_payment_methods=[PaymentMethod.DOOR],
    )

    with pytest.raises(PaymentMethodNotEnabledCheckoutError):
        await perform_checkout(
            db_session,
            items=[CheckoutItemInput(ticket_type_id=ticket_type.id, quantity=1)],
            buyer_name="Buyer",
            buyer_email="buyer@example.test",
            buyer_address="1 Test Street",
            language="en",
            payment_method=PaymentMethod.MOLLIE,
            preview_token=None,
        )


async def test_insufficient_stock_reports_non_negative_remaining(
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    _, _, ticket_type = await _live_setup(
        make_event, make_show, make_ticket_type, make_event_config, quantity_available=2
    )

    with pytest.raises(InsufficientStockCheckoutError) as exc_info:
        await perform_checkout(
            db_session,
            items=[CheckoutItemInput(ticket_type_id=ticket_type.id, quantity=10)],
            buyer_name="Buyer",
            buyer_email="buyer@example.test",
            buyer_address="1 Test Street",
            language="en",
            payment_method=PaymentMethod.DOOR,
            preview_token=None,
        )
    assert exc_info.value.remaining == 2
    assert exc_info.value.remaining >= 0
    assert exc_info.value.ticket_type_id == ticket_type.id


# --- HTTP-level tests: exact status codes the route returns --------------


async def test_checkout_route_404s_for_unknown_ticket_type(client: AsyncClient) -> None:
    response = await client.post("/api/v1/public/checkout", json=_payload([uuid.uuid4()]))
    assert response.status_code == 404


async def test_checkout_route_404s_for_draft_event_without_token(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await make_event(status=PublishStatus.DRAFT)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id)
    await make_event_config(
        event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR]
    )

    response = await client.post("/api/v1/public/checkout", json=_payload([ticket_type.id]))
    assert response.status_code == 404


async def test_checkout_route_201s_for_draft_event_with_valid_preview_token(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await make_event(status=PublishStatus.DRAFT)
    show = await make_show(event_id=event.id, status=PublishStatus.DRAFT)
    ticket_type = await make_ticket_type(show_id=show.id)
    await make_event_config(
        event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR]
    )

    response = await client.post(
        "/api/v1/public/checkout",
        json=_payload([ticket_type.id], preview_token=event.preview_token),
    )
    assert response.status_code == 201


async def test_checkout_route_403s_when_sales_not_live(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id)
    await make_event_config(
        event_id=event.id, sales_live_at=_FUTURE, enabled_payment_methods=[PaymentMethod.DOOR]
    )

    response = await client.post("/api/v1/public/checkout", json=_payload([ticket_type.id]))
    assert response.status_code == 403


async def test_checkout_route_403s_when_sales_paused(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED, sales_paused=True)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id)
    await make_event_config(
        event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR]
    )

    response = await client.post("/api/v1/public/checkout", json=_payload([ticket_type.id]))
    assert response.status_code == 403


async def test_checkout_route_422s_when_payment_method_not_enabled(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id)
    await make_event_config(
        event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR]
    )

    response = await client.post(
        "/api/v1/public/checkout",
        json=_payload([ticket_type.id], payment_method=PaymentMethod.MOLLIE),
    )
    assert response.status_code == 422


async def test_checkout_route_409s_on_insufficient_stock(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=1)
    await make_event_config(
        event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR]
    )

    response = await client.post(
        "/api/v1/public/checkout", json=_payload([ticket_type.id], quantity=2)
    )
    assert response.status_code == 409
    assert "1" in response.json()["detail"]
