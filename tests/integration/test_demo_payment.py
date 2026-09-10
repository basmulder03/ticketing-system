"""Integration tests for the ``demo`` payment provider's JSON API surface
(``app.api.routes.public``'s ``/demo-payment/{order_id}`` routes) — the
post-launch fix, per the user's NOTES, adding a real, admin-selectable
payment method any event can enable for demo/test purposes, without
external calls or credentials. See
``app.services.checkout._initiate_demo_payment`` for the full design
rationale, and ``app.api.routes.public._get_pending_demo_order_or_404``
for the access-control model these tests exercise: an order's own UUID
(reachable only via the checkout response / demo-payment redirect) is the
sole "possession" proof, gated by ``payment_method=demo`` AND
``status=pending`` so it becomes unreachable within seconds of settling
and can never be used against a real mollie/door order.

Mirrors ``test_mollie_webhook.py``'s and ``test_mark_order_paid_route.py``'s
established patterns: real HTTP checkout to reach a real pending Order,
real Mailpit round trip for the confirmation email, real DB assertions for
Ticket signing / Invoice issuance / audit entries / stock release.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.audit_log import AuditLogEntry
from app.models.enums import OrderStatus, PaymentMethod, PublishStatus
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.invoice import Invoice
from app.models.order import Order
from app.models.show import Show
from app.models.ticket import Ticket
from app.models.ticket_type import TicketType
from app.services.stock import sold_counts_for_ticket_types
from tests.integration.conftest import fetch_latest_mailpit_message_to

_PAST = datetime.now(UTC) - timedelta(days=1)


async def _checkout_demo_order(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
    *,
    buyer_email: str | None = None,
    quantity_available: int = 10,
    quantity: int = 1,
) -> tuple[str, str]:
    """Create a real, published Event/Show/TicketType with ``demo`` enabled,
    then check out via the real HTTP checkout route. Returns
    ``(order_id, ticket_type_id)``."""
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=quantity_available)
    await make_event_config(
        event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DEMO]
    )

    response = await client.post(
        "/api/v1/public/checkout",
        json={
            "buyer_name": "Demo Buyer",
            "buyer_email": buyer_email or f"demo-{uuid.uuid4().hex}@example.test",
            "buyer_address": "1 Test Street",
            "language": "en",
            "payment_method": "demo",
            "items": [{"ticket_type_id": str(ticket_type.id), "quantity": quantity}],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "pending"
    expected_prefix = f"{get_settings().public_base_url.rstrip('/')}/demo-payment/"
    assert body["payment_redirect_url"] == f"{expected_prefix}{body['id']}"
    return body["id"], str(ticket_type.id)


async def _order_by_id(db_session: AsyncSession, order_id: str) -> Order:
    result = await db_session.execute(select(Order).where(Order.id == uuid.UUID(order_id)))
    return result.scalar_one()


async def _audit_count(db_session: AsyncSession, *, action: str, order_id: str) -> int:
    result = await db_session.execute(
        select(func.count())
        .select_from(AuditLogEntry)
        .where(AuditLogEntry.action == action)
        .where(AuditLogEntry.target_id == order_id)
    )
    return result.scalar_one()


# --- GET /demo-payment/{order_id}: summary read ------------------------------


async def test_get_demo_payment_returns_summary_for_a_pending_demo_order(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    order_id, _ = await _checkout_demo_order(client, make_event, make_show, make_ticket_type, make_event_config)

    response = await client.get(f"/api/v1/public/demo-payment/{order_id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["order_id"] == order_id
    assert body["buyer_name"] == "Demo Buyer"
    assert len(body["items"]) == 1
    assert body["items"][0]["quantity"] == 1


async def test_get_demo_payment_404s_for_unknown_order_id(client: AsyncClient) -> None:
    response = await client.get(f"/api/v1/public/demo-payment/{uuid.uuid4()}")
    assert response.status_code == 404


async def test_get_demo_payment_404s_for_malformed_order_id(client: AsyncClient) -> None:
    response = await client.get("/api/v1/public/demo-payment/not-a-uuid")
    assert response.status_code == 404


async def test_get_demo_payment_404s_for_a_non_demo_order(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """A real ``door`` order's id must never be usable against the
    demo-payment routes, even though it's an equally pending Order —
    proves the access-control gate checks ``payment_method``, not just
    ``status``."""
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=10)
    await make_event_config(event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR])

    checkout = await client.post(
        "/api/v1/public/checkout",
        json={
            "buyer_name": "Door Buyer",
            "buyer_email": "door@example.test",
            "buyer_address": "1 Test Street",
            "language": "en",
            "payment_method": "door",
            "items": [{"ticket_type_id": str(ticket_type.id), "quantity": 1}],
        },
    )
    assert checkout.status_code == 201
    order_id = checkout.json()["id"]

    response = await client.get(f"/api/v1/public/demo-payment/{order_id}")
    assert response.status_code == 404


# --- POST /demo-payment/{order_id}/complete: settle -------------------------


async def test_complete_demo_payment_marks_order_paid_with_full_downstream_effects(
    client: AsyncClient,
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    buyer_email = f"demo-complete-{uuid.uuid4().hex}@example.test"
    order_id, _ = await _checkout_demo_order(
        client, make_event, make_show, make_ticket_type, make_event_config, buyer_email=buyer_email
    )

    response = await client.post(f"/api/v1/public/demo-payment/{order_id}/complete")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "paid"

    order = await _order_by_id(db_session, order_id)
    assert order.status == OrderStatus.PAID

    tickets_result = await db_session.execute(select(Ticket).where(Ticket.order_id == order.id))
    tickets = tickets_result.scalars().all()
    assert len(tickets) == 1
    assert tickets[0].qr_token

    invoice_count = await db_session.execute(
        select(func.count()).select_from(Invoice).where(Invoice.order_id == order.id)
    )
    assert invoice_count.scalar_one() == 1

    assert await _audit_count(db_session, action="order.mark_paid", order_id=order_id) == 1

    message = await fetch_latest_mailpit_message_to(buyer_email)
    attachments = message["Attachments"]
    assert len(attachments) == 2


async def test_complete_demo_payment_404s_once_already_settled(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """A completed demo order becomes unreachable — proves the gate re-checks
    ``status=pending`` on every call, not just at page-render time, so a
    replayed/double-submitted complete action can't be exploited."""
    order_id, _ = await _checkout_demo_order(client, make_event, make_show, make_ticket_type, make_event_config)

    first = await client.post(f"/api/v1/public/demo-payment/{order_id}/complete")
    assert first.status_code == 200

    second = await client.post(f"/api/v1/public/demo-payment/{order_id}/complete")
    assert second.status_code == 404


async def test_complete_demo_payment_404s_for_unknown_order_id(client: AsyncClient) -> None:
    response = await client.post(f"/api/v1/public/demo-payment/{uuid.uuid4()}/complete")
    assert response.status_code == 404


# --- POST /demo-payment/{order_id}/fail: cancel + release stock -------------


async def test_fail_demo_payment_cancels_order_and_releases_stock(
    client: AsyncClient,
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    order_id, ticket_type_id = await _checkout_demo_order(
        client, make_event, make_show, make_ticket_type, make_event_config, quantity_available=3
    )

    sold_before = await sold_counts_for_ticket_types(db_session, [uuid.UUID(ticket_type_id)])
    assert sold_before.get(uuid.UUID(ticket_type_id), 0) == 1

    response = await client.post(f"/api/v1/public/demo-payment/{order_id}/fail")

    assert response.status_code == 204

    order = await _order_by_id(db_session, order_id)
    assert order.status == OrderStatus.CANCELLED

    sold_after = await sold_counts_for_ticket_types(db_session, [uuid.UUID(ticket_type_id)])
    assert sold_after.get(uuid.UUID(ticket_type_id), 0) == 0, "cancelling a demo order must release its stock"


async def test_fail_demo_payment_404s_once_already_settled(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    order_id, _ = await _checkout_demo_order(client, make_event, make_show, make_ticket_type, make_event_config)

    first = await client.post(f"/api/v1/public/demo-payment/{order_id}/fail")
    assert first.status_code == 204

    second = await client.post(f"/api/v1/public/demo-payment/{order_id}/fail")
    assert second.status_code == 404


async def test_fail_demo_payment_404s_for_unknown_order_id(client: AsyncClient) -> None:
    response = await client.post(f"/api/v1/public/demo-payment/{uuid.uuid4()}/fail")
    assert response.status_code == 404
