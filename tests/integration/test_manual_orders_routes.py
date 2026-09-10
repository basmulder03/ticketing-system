"""Integration tests for ``POST /api/v1/shows/{show_id}/manual-orders``
(``app.api.routes.orders.create_manual_order_route``) — the post-launch
fix, per the user's NOTES: "For people without a computer or phone, allow
for an admin to create/do things with tickets, like creating them for a
show, e.g. without having the payment process (of course with the correct
audit logging)."

Admin-only scoping (agent/scanner/unauthenticated all rejected) is covered
by the shared table in ``tests/integration/test_deps_admin_scoping.py`` —
not repeated here. This file covers the route's own behavior: the happy
path's full downstream effect sequence (mirrors ``test_mark_order_paid_
route.py``'s), skipping the confirmation email when no buyer_email was
given, bypassing every buyer-facing checkout gate, real stock enforcement,
and the two distinct audit entries this creates.
"""

import uuid
from collections.abc import Awaitable, Callable

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLogEntry
from app.models.enums import OrderStatus, PublishStatus
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.invoice import Invoice
from app.models.order import Order
from app.models.show import Show
from app.models.ticket import Ticket
from app.models.ticket_type import TicketType
from tests.integration.conftest import SeededAdmin, fetch_latest_mailpit_message_to


async def _login_admin(client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]) -> SeededAdmin:
    seeded = await make_admin_user()
    login = await client.post(
        "/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password}
    )
    assert login.status_code == 200
    return seeded


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


async def test_create_manual_order_happy_path_with_email_sends_confirmation(
    client: AsyncClient,
    db_session: AsyncSession,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    seeded_admin = await _login_admin(client, make_admin_user)
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=10)
    await make_event_config(event_id=event.id)
    buyer_email = f"walkin-{uuid.uuid4().hex}@example.test"

    response = await client.post(
        f"/api/v1/shows/{show.id}/manual-orders",
        json={
            "buyer_name": "Walk-up Buyer",
            "buyer_email": buyer_email,
            "buyer_address": "1 Test Street",
            "items": [{"ticket_type_id": str(ticket_type.id), "quantity": 2}],
            "method_label": "cash",
            "reason": "box office walk-up sale",
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "paid"
    assert body["payment_method"] == "manual"
    assert len(body["tickets"]) == 2

    order = await _order_by_id(db_session, body["id"])
    assert order.status == OrderStatus.PAID

    tickets_result = await db_session.execute(select(Ticket).where(Ticket.order_id == order.id))
    tickets = tickets_result.scalars().all()
    assert len(tickets) == 2
    assert all(t.qr_token for t in tickets)

    invoice_count = await db_session.execute(
        select(func.count()).select_from(Invoice).where(Invoice.order_id == order.id)
    )
    assert invoice_count.scalar_one() == 1

    assert await _audit_count(db_session, action="order.create_manual", order_id=str(order.id)) == 1
    assert await _audit_count(db_session, action="order.mark_paid", order_id=str(order.id)) == 1

    create_entry = (
        await db_session.execute(
            select(AuditLogEntry)
            .where(AuditLogEntry.action == "order.create_manual")
            .where(AuditLogEntry.target_id == str(order.id))
        )
    ).scalar_one()
    assert create_entry.actor_id == seeded_admin.user.id
    assert create_entry.detail is not None
    assert create_entry.detail["show_id"] == str(show.id)

    # `fetch_latest_mailpit_message_to`'s own Mailpit-side "to:" query
    # filter was found (in this session) to silently return the whole
    # unfiltered mailbox rather than actually filtering by recipient — so
    # this asserts on order-specific attachment filenames (mirrors
    # ``test_mark_order_paid_route.py``'s own convention) rather than a
    # generic attachment count, which a stale unrelated message could
    # coincidentally satisfy too.
    message = await fetch_latest_mailpit_message_to(buyer_email)
    attachment_names = {a["FileName"] for a in message["Attachments"]}
    assert f"tickets-{body['id']}.pdf" in attachment_names
    assert f"invoice-{body['id']}.pdf" in attachment_names

    await db_session.refresh(order)
    assert order.confirmation_email_sent_at is not None


async def test_create_manual_order_without_email_uses_placeholder_and_skips_confirmation(
    client: AsyncClient,
    db_session: AsyncSession,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
) -> None:
    """Per the user's NOTES, this route exists FOR buyers with no computer
    or phone — who may have no email address to give at all."""
    await _login_admin(client, make_admin_user)
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=10)

    response = await client.post(
        f"/api/v1/shows/{show.id}/manual-orders",
        json={
            "buyer_name": "No Email Buyer",
            "items": [{"ticket_type_id": str(ticket_type.id), "quantity": 1}],
            "method_label": "comp",
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["buyer_email"].endswith("@no-email.invalid")
    assert body["status"] == "paid"

    order = await _order_by_id(db_session, body["id"])
    assert order.confirmation_email_sent_at is None, "no email address -- nothing should be sent"


async def test_create_manual_order_bypasses_draft_and_sales_gates(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
) -> None:
    """A public checkout against a draft Event or before sales_live_at
    would be rejected -- an admin issuing a manual ticket is a trusted
    staff action and is never subject to those gates."""
    await _login_admin(client, make_admin_user)
    event = await make_event(status=PublishStatus.DRAFT)
    show = await make_show(event_id=event.id, status=PublishStatus.DRAFT)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=10)
    # Deliberately no EventConfig at all for this Event -- a public
    # checkout would 404/422 well before reaching stock; this route must
    # not care.

    response = await client.post(
        f"/api/v1/shows/{show.id}/manual-orders",
        json={
            "buyer_name": "Draft Show Buyer",
            "items": [{"ticket_type_id": str(ticket_type.id), "quantity": 1}],
            "method_label": "cash",
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "paid"


async def test_create_manual_order_returns_409_when_stock_is_insufficient(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
) -> None:
    await _login_admin(client, make_admin_user)
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=1)

    first = await client.post(
        f"/api/v1/shows/{show.id}/manual-orders",
        json={
            "buyer_name": "First Buyer",
            "items": [{"ticket_type_id": str(ticket_type.id), "quantity": 1}],
            "method_label": "cash",
        },
    )
    assert first.status_code == 201, first.text

    second = await client.post(
        f"/api/v1/shows/{show.id}/manual-orders",
        json={
            "buyer_name": "Second Buyer",
            "items": [{"ticket_type_id": str(ticket_type.id), "quantity": 1}],
            "method_label": "cash",
        },
    )
    assert second.status_code == 409


async def test_create_manual_order_rejects_ticket_type_from_a_different_show(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
) -> None:
    await _login_admin(client, make_admin_user)
    event = await make_event(status=PublishStatus.PUBLISHED)
    show_a = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    show_b = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type_on_b = await make_ticket_type(show_id=show_b.id, quantity_available=10)

    response = await client.post(
        f"/api/v1/shows/{show_a.id}/manual-orders",
        json={
            "buyer_name": "Buyer",
            "items": [{"ticket_type_id": str(ticket_type_on_b.id), "quantity": 1}],
            "method_label": "cash",
        },
    )

    assert response.status_code == 422


async def test_create_manual_order_returns_404_for_unknown_show(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login_admin(client, make_admin_user)

    response = await client.post(
        f"/api/v1/shows/{uuid.uuid4()}/manual-orders",
        json={
            "buyer_name": "Buyer",
            "items": [{"ticket_type_id": str(uuid.uuid4()), "quantity": 1}],
            "method_label": "cash",
        },
    )

    assert response.status_code == 404


async def test_create_manual_order_returns_404_for_unknown_ticket_type(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
) -> None:
    await _login_admin(client, make_admin_user)
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)

    response = await client.post(
        f"/api/v1/shows/{show.id}/manual-orders",
        json={
            "buyer_name": "Buyer",
            "items": [{"ticket_type_id": str(uuid.uuid4()), "quantity": 1}],
            "method_label": "cash",
        },
    )

    assert response.status_code == 404


async def test_create_manual_order_rejects_empty_items(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
) -> None:
    await _login_admin(client, make_admin_user)
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)

    response = await client.post(
        f"/api/v1/shows/{show.id}/manual-orders",
        json={"buyer_name": "Buyer", "items": [], "method_label": "cash"},
    )

    assert response.status_code == 422
