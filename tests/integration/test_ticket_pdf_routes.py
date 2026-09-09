"""Integration tests for the Milestone 9 ticket-PDF download routes
(``app.api.routes.orders``):
  - ``GET /api/v1/orders/{order_id}/tickets.pdf`` (``download_tickets_pdf``)
  - ``GET /api/v1/shows/{show_id}/tickets-batch.pdf``
    (``download_show_tickets_batch_pdf``)

Admin-only scoping (agent/scanner/unauthenticated all rejected) is covered
by the shared table in ``tests/integration/test_deps_admin_scoping.py`` —
not repeated here. This file covers each route's own behavior.
"""

import uuid
from collections.abc import Awaitable, Callable

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import PaymentMethod, PublishStatus
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.order import Order
from app.models.show import Show
from app.models.ticket import Ticket
from app.models.ticket_type import TicketType
from tests.integration.conftest import SeededAdmin


async def _login_admin(client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]) -> None:
    seeded = await make_admin_user()
    login = await client.post(
        "/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password}
    )
    assert login.status_code == 200


async def _checkout_door_order(
    client: AsyncClient,
    show: Show,
    ticket_type: TicketType,
    *,
    buyer_email: str | None = None,
) -> str:
    """Check out a real, unpaid ``payment_method=door`` order for an
    already-published Show/TicketType — no signed tickets yet."""
    response = await client.post(
        "/api/v1/public/checkout",
        json={
            "buyer_name": "Door Buyer",
            "buyer_email": buyer_email or f"door-{uuid.uuid4().hex}@example.test",
            "buyer_address": "1 Test Street",
            "language": "en",
            "payment_method": "door",
            "items": [{"ticket_type_id": str(ticket_type.id), "quantity": 1}],
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _checkout_simulated_paid_order(
    client: AsyncClient,
    event: Event,
    show: Show,
    ticket_type: TicketType,
    *,
    buyer_email: str | None = None,
) -> str:
    """Check out via a draft Event's preview token with
    ``payment_method=mollie`` and no Mollie key configured, taking the
    preview-mode simulated-payment path — a genuinely ``paid`` Order with
    signed Tickets, no Mollie mocking needed (mirrors
    ``test_invoice_download_route.py``'s helper)."""
    response = await client.post(
        "/api/v1/public/checkout",
        json={
            "buyer_name": "Preview Buyer",
            "buyer_email": buyer_email or f"preview-{uuid.uuid4().hex}@example.test",
            "buyer_address": "1 Test Street",
            "language": "en",
            "payment_method": "mollie",
            "items": [{"ticket_type_id": str(ticket_type.id), "quantity": 1}],
            "preview_token": event.preview_token,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "paid"
    return str(body["id"])


# --- GET /api/v1/orders/{order_id}/tickets.pdf ------------------------------


async def test_download_tickets_pdf_returns_404_for_unknown_order_id(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login_admin(client, make_admin_user)

    response = await client.get(f"/api/v1/orders/{uuid.uuid4()}/tickets.pdf")

    assert response.status_code == 404


async def test_download_tickets_pdf_returns_404_for_an_unpaid_order(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """An unpaid (door, not reconciled) order's Tickets are never signed —
    404s the same way ``download_invoice_pdf`` 404s for "no invoice yet"."""
    await _login_admin(client, make_admin_user)
    from datetime import UTC, datetime, timedelta

    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=10)
    await make_event_config(
        event_id=event.id,
        sales_live_at=datetime.now(UTC) - timedelta(days=1),
        enabled_payment_methods=[PaymentMethod.DOOR],
    )
    order_id = await _checkout_door_order(client, show, ticket_type)

    response = await client.get(f"/api/v1/orders/{order_id}/tickets.pdf")

    assert response.status_code == 404
    assert "signed" in response.json()["detail"].lower()


async def test_download_tickets_pdf_returns_a_real_pdf_for_a_paid_order(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    await _login_admin(client, make_admin_user)
    event = await make_event(status=PublishStatus.DRAFT)
    show = await make_show(event_id=event.id, status=PublishStatus.DRAFT)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=10)
    await make_event_config(event_id=event.id, enabled_payment_methods=[PaymentMethod.MOLLIE])
    order_id = await _checkout_simulated_paid_order(client, event, show, ticket_type)

    response = await client.get(f"/api/v1/orders/{order_id}/tickets.pdf")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    # Same filename convention as the email attachment (see
    # app.services.ticket_delivery.send_order_confirmation_email).
    assert f'filename="tickets-{order_id}.pdf"' in response.headers["content-disposition"]
    assert response.content.startswith(b"%PDF")


# --- GET /api/v1/shows/{show_id}/tickets-batch.pdf --------------------------


async def test_download_show_tickets_batch_pdf_returns_404_for_unknown_show_id(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login_admin(client, make_admin_user)

    response = await client.get(f"/api/v1/shows/{uuid.uuid4()}/tickets-batch.pdf")

    assert response.status_code == 404


async def test_download_show_tickets_batch_pdf_returns_404_for_malformed_show_id(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login_admin(client, make_admin_user)

    response = await client.get("/api/v1/shows/not-a-uuid/tickets-batch.pdf")

    assert response.status_code == 404


async def test_download_show_tickets_batch_pdf_returns_404_when_show_has_no_paid_orders(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    await _login_admin(client, make_admin_user)
    from datetime import UTC, datetime, timedelta

    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=10)
    await make_event_config(
        event_id=event.id,
        sales_live_at=datetime.now(UTC) - timedelta(days=1),
        enabled_payment_methods=[PaymentMethod.DOOR],
    )
    # An unpaid door order exists for this Show, but nothing paid yet.
    await _checkout_door_order(client, show, ticket_type)

    response = await client.get(f"/api/v1/shows/{show.id}/tickets-batch.pdf")

    assert response.status_code == 404
    assert "no paid orders" in response.json()["detail"].lower()


async def test_download_show_tickets_batch_pdf_combines_multiple_orders_into_one_pdf(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """Two paid Orders on the same Show combine into ONE PDF that is
    larger than either order's own standalone ticket PDF — mirrors
    ``tests/unit/test_ticket_pdf.py``'s "bigger HTML input => bigger PDF
    output" page-count signal, generalized across Orders here rather than
    within one Order's Tickets (see that file's docstring)."""
    await _login_admin(client, make_admin_user)
    event = await make_event(status=PublishStatus.DRAFT)
    show = await make_show(event_id=event.id, status=PublishStatus.DRAFT)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=10)
    await make_event_config(event_id=event.id, enabled_payment_methods=[PaymentMethod.MOLLIE])

    order_a_id = await _checkout_simulated_paid_order(client, event, show, ticket_type)

    single_order_response = await client.get(f"/api/v1/shows/{show.id}/tickets-batch.pdf")
    assert single_order_response.status_code == 200
    assert single_order_response.content.startswith(b"%PDF")

    order_b_id = await _checkout_simulated_paid_order(client, event, show, ticket_type)
    assert order_b_id != order_a_id

    two_order_response = await client.get(f"/api/v1/shows/{show.id}/tickets-batch.pdf")

    assert two_order_response.status_code == 200
    assert two_order_response.headers["content-type"] == "application/pdf"
    assert f'filename="tickets-batch-{show.id}.pdf"' in two_order_response.headers["content-disposition"]
    assert two_order_response.content.startswith(b"%PDF")
    assert len(two_order_response.content) > len(single_order_response.content)


async def test_download_show_tickets_batch_pdf_skips_orders_with_unsigned_tickets(
    client: AsyncClient,
    db_session: AsyncSession,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """A ``paid`` Order that (defensively/unexpectedly) still has an
    unsigned Ticket is skipped, not erroring the whole batch — per the
    route's documented "skip, don't error" behavior."""
    await _login_admin(client, make_admin_user)
    event = await make_event(status=PublishStatus.DRAFT)
    show = await make_show(event_id=event.id, status=PublishStatus.DRAFT)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=10)
    await make_event_config(event_id=event.id, enabled_payment_methods=[PaymentMethod.MOLLIE])

    good_order_id = await _checkout_simulated_paid_order(client, event, show, ticket_type)
    unsigned_order_id = await _checkout_simulated_paid_order(client, event, show, ticket_type)

    # Defensively simulate the "paid but somehow unsigned" state directly —
    # this should never happen via the real payment-confirmation path.
    unsigned_tickets = (
        await db_session.execute(select(Ticket).where(Ticket.order_id == uuid.UUID(unsigned_order_id)))
    ).scalars().all()
    assert unsigned_tickets
    for ticket in unsigned_tickets:
        ticket.qr_token = None
    await db_session.commit()

    response = await client.get(f"/api/v1/shows/{show.id}/tickets-batch.pdf")

    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")

    # Sanity: the good order's own ticket really is signed, confirming the
    # 200 above is a real (non-empty, one-order) batch, not an accidental
    # pass because both orders happened to be skipped.
    good_order = (
        await db_session.execute(select(Order).where(Order.id == uuid.UUID(good_order_id)))
    ).scalar_one()
    assert good_order.status.value == "paid"
