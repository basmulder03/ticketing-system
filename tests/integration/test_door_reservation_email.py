"""Integration tests for the door-payment reservation-confirmation email
(``app.services.door_reservation_email``, ``app.services.email_render.
render_door_payment_confirmation_email``) — a post-launch fix for a real
gap found via the user's own manual testing: a ``payment_method="door"``
Order previously got NO email at all until it was later paid/scanned in at
the venue, leaving the buyer with no proof of what they'd ordered if they
lost the order-confirmation browser session.

Dispatched from the real ``POST /api/v1/public/checkout`` route
(``app.api.routes.public.checkout``) immediately after a door Order is
created — every test here drives that real HTTP route, not the service
function directly, so a regression in the wiring itself (the ``elif
result.order.payment_method == PaymentMethod.DOOR`` branch) would be
caught too.

Mocking-boundary decisions, mirroring ``tests/integration/test_ticket_delivery.py``:
- Most tests monkeypatch ``aiosmtplib.send`` directly.
- One test performs a real send against the local Mailpit sink and asserts
  on the actual delivered message via Mailpit's HTTP API.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import aiosmtplib
import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.checkout as checkout_module
from app.core.config import get_settings
from app.models.audit_log import AuditLogEntry
from app.models.enums import PaymentMethod, PublishStatus
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.show import Show
from app.models.ticket_type import TicketType
from app.services.mollie import MolliePaymentCreated
from tests.integration.conftest import fetch_latest_mailpit_message_to

_PAST = datetime.now(UTC) - timedelta(days=1)
_SENT_ACTION = "order.door_confirmation_email.sent"
_FAILED_ACTION = "order.door_confirmation_email.failed"


def _payload(
    ticket_type_id: uuid.UUID, *, buyer_email: str, quantity: int = 1, payment_method: str = "door"
) -> dict[str, object]:
    return {
        "buyer_name": "Jamie O'Brien",
        "buyer_email": buyer_email,
        "buyer_address": "1 Test Street",
        "language": "en",
        "payment_method": payment_method,
        "items": [{"ticket_type_id": str(ticket_type_id), "quantity": quantity}],
        "preview_token": None,
    }


async def _setup_published_show(
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
    *,
    smtp_host: str | None = get_settings().seed_smtp_host,
) -> tuple[Event, TicketType]:
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=5)
    await make_event_config(
        event_id=event.id,
        sales_live_at=_PAST,
        enabled_payment_methods=[PaymentMethod.DOOR, PaymentMethod.MOLLIE],
        smtp_host=smtp_host,
    )
    return event, ticket_type


async def _audit_action_count(db_session: AsyncSession, order_id: uuid.UUID, action: str) -> int:
    result = await db_session.execute(
        select(func.count()).select_from(AuditLogEntry).where(AuditLogEntry.action == action).where(
            AuditLogEntry.target_id == str(order_id)
        )
    )
    return result.scalar_one()


async def test_door_checkout_dispatches_reservation_confirmation_email(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    _event, ticket_type = await _setup_published_show(make_event, make_show, make_ticket_type, make_event_config)
    buyer_email = f"door-buyer-{uuid.uuid4().hex[:8]}@example.test"

    sent: list[EmailMessage] = []

    async def _fake_send(message: EmailMessage, **kwargs: object) -> None:
        sent.append(message)

    monkeypatch.setattr(aiosmtplib, "send", _fake_send)

    response = await client.post(
        "/api/v1/public/checkout", json=_payload(ticket_type.id, buyer_email=buyer_email, quantity=2)
    )
    assert response.status_code == 201
    order_id = uuid.UUID(response.json()["id"])

    assert len(sent) == 1
    message = sent[0]
    assert message["To"] == buyer_email
    assert "door" in message["Subject"].lower()
    # No ticket/invoice PDF attached — an unpaid door reservation must
    # never carry a real, scannable ticket (see this module's docstring
    # and app.services.email_render.render_door_payment_confirmation_email).
    assert list(message.iter_attachments()) == []

    assert await _audit_action_count(db_session, order_id, _SENT_ACTION) == 1
    assert await _audit_action_count(db_session, order_id, _FAILED_ACTION) == 0


async def test_mollie_door_checkout_does_not_dispatch_door_confirmation_email(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """A real (non-preview) ``payment_method=mollie`` checkout must not
    trigger the door-reservation email at all -- it isn't a door order, and
    (being real, non-simulated Mollie) it isn't paid yet either, so no
    email of any kind fires until the webhook later confirms payment."""
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=5)
    await make_event_config(
        event_id=event.id,
        sales_live_at=_PAST,
        mollie_test_api_key="test_dummy_key_never_used_over_network",
        enabled_payment_methods=[PaymentMethod.MOLLIE],
    )
    buyer_email = f"mollie-buyer-{uuid.uuid4().hex[:8]}@example.test"

    async def _fake_create_mollie_payment(**kwargs: object) -> MolliePaymentCreated:
        return MolliePaymentCreated(payment_id="tr_test_fake", checkout_url="https://www.mollie.com/checkout/fake")

    monkeypatch.setattr(checkout_module, "create_mollie_payment", _fake_create_mollie_payment)

    sent: list[EmailMessage] = []

    async def _fake_send(message: EmailMessage, **kwargs: object) -> None:
        sent.append(message)

    monkeypatch.setattr(aiosmtplib, "send", _fake_send)

    response = await client.post(
        "/api/v1/public/checkout", json=_payload(ticket_type.id, buyer_email=buyer_email, payment_method="mollie")
    )
    assert response.status_code == 201
    order_id = uuid.UUID(response.json()["id"])

    assert sent == []
    assert await _audit_action_count(db_session, order_id, _SENT_ACTION) == 0


async def test_door_checkout_reservation_email_content_lands_in_mailpit(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """A real send against the local Mailpit sink, asserting on the actual
    delivered message content -- not just 'did send() get called'."""
    _event, ticket_type = await _setup_published_show(make_event, make_show, make_ticket_type, make_event_config)
    buyer_email = f"mailpit-door-{uuid.uuid4().hex[:8]}@example.test"

    response = await client.post(
        "/api/v1/public/checkout", json=_payload(ticket_type.id, buyer_email=buyer_email, quantity=2)
    )
    assert response.status_code == 201

    message = await fetch_latest_mailpit_message_to(buyer_email)
    assert "door" in message["Subject"].lower()
    html_body = message["HTML"]
    assert "Jamie" in html_body
    assert message["Attachments"] == []


async def test_door_checkout_still_succeeds_when_smtp_is_not_configured(
    client: AsyncClient,
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """Checkout itself must never fail just because the reservation email
    couldn't be sent (e.g. an event that hasn't configured SMTP yet) — same
    best-effort guarantee as the paid-order-confirmation email."""
    _event, ticket_type = await _setup_published_show(
        make_event, make_show, make_ticket_type, make_event_config, smtp_host=None
    )
    buyer_email = f"no-smtp-{uuid.uuid4().hex[:8]}@example.test"

    response = await client.post("/api/v1/public/checkout", json=_payload(ticket_type.id, buyer_email=buyer_email))
    assert response.status_code == 201
    order_id = uuid.UUID(response.json()["id"])

    assert await _audit_action_count(db_session, order_id, _FAILED_ACTION) == 1
    assert await _audit_action_count(db_session, order_id, _SENT_ACTION) == 0
