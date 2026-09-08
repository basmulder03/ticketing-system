"""Integration tests for ``POST /api/v1/orders/{order_id}/mark-paid``
(``app.api.routes.orders.mark_paid``, Milestone 6) — the backoffice
counterpart to the automatic Mollie webhook confirmation, per
PROJECT_BRIEF.md's Manual Payment Handling section.

Admin-only scoping (agent/scanner/unauthenticated all rejected) is covered
by the shared table in ``tests/integration/test_deps_admin_scoping.py`` —
not repeated here. This file covers the route's own behavior:
  - 404 for an unknown order id.
  - The happy path's full downstream effect sequence (mirrors the Mollie
    webhook's — see ``app.api.routes.orders.mark_paid``'s docstring):
    ticket QR signing, invoice issuance, an audit entry attributed to the
    real admin (not ``SYSTEM_PRINCIPAL``), and a confirmation email landing
    in Mailpit with both PDF attachments (mirrors
    ``test_ticket_delivery.py``'s real-Mailpit-round-trip pattern).
  - Idempotency across repeat calls: second call reports ``already_paid``,
    no duplicate audit entry, no duplicate Invoice, no second email sent.
  - Recovery from ``cancelled``/``expired`` starting statuses, per the
    route's documented "works from ANY non-paid status" behavior.
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

from app.models.audit_log import AuditLogEntry
from app.models.enums import OrderStatus, PaymentMethod, PublishStatus
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.invoice import Invoice
from app.models.order import Order
from app.models.show import Show
from app.models.ticket import Ticket
from app.models.ticket_type import TicketType
from tests.integration.conftest import (
    SeededAdmin,
    fetch_latest_mailpit_message_to,
    fetch_mailpit_attachment,
)

_PAST = datetime.now(UTC) - timedelta(days=1)
_MARK_PAID_ACTION = "order.mark_paid"


# --- Shared setup helpers ----------------------------------------------------


async def _login_admin(client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]) -> SeededAdmin:
    seeded = await make_admin_user()
    login = await client.post(
        "/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password}
    )
    assert login.status_code == 200
    return seeded


async def _checkout_door_order(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
    *,
    buyer_email: str | None = None,
) -> str:
    """Create a real, published Event/Show/TicketType and check out a
    ``payment_method=door`` order via the real HTTP checkout route (starts
    life ``pending_door`` as of Milestone 6 — see
    ``app.services.checkout.perform_checkout``). Returns the order id."""
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=10)
    await make_event_config(
        event_id=event.id,
        sales_live_at=_PAST,
        enabled_payment_methods=[PaymentMethod.DOOR],
    )

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
    body = response.json()
    assert body["status"] == "pending_door"
    return str(body["id"])


def _patch_aiosmtplib_send_success(monkeypatch: pytest.MonkeyPatch) -> list[EmailMessage]:
    """Same helper as ``test_ticket_delivery.py``'s: monkeypatch
    ``aiosmtplib.send`` to succeed without a real network call, recording
    every call for assertions."""
    calls: list[EmailMessage] = []

    async def _fake_send(message: EmailMessage, **kwargs: object) -> tuple[dict[str, object], str]:
        calls.append(message)
        return {}, "OK"

    monkeypatch.setattr(aiosmtplib, "send", _fake_send)
    return calls


async def _order_by_id(db_session: AsyncSession, order_id: str) -> Order:
    result = await db_session.execute(select(Order).where(Order.id == uuid.UUID(order_id)))
    return result.scalar_one()


async def _audit_count(db_session: AsyncSession, *, action: str, order_id: uuid.UUID) -> int:
    result = await db_session.execute(
        select(func.count())
        .select_from(AuditLogEntry)
        .where(AuditLogEntry.action == action)
        .where(AuditLogEntry.target_id == str(order_id))
    )
    return result.scalar_one()


async def _invoice_count(db_session: AsyncSession, order_id: uuid.UUID) -> int:
    result = await db_session.execute(select(func.count()).select_from(Invoice).where(Invoice.order_id == order_id))
    return result.scalar_one()


# --- 404 ----------------------------------------------------------------------


async def test_returns_404_for_unknown_order_id(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login_admin(client, make_admin_user)

    response = await client.post(f"/api/v1/orders/{uuid.uuid4()}/mark-paid", json={"method_label": "cash"})

    assert response.status_code == 404


# --- Happy path: full downstream effect sequence -------------------------------


async def test_marks_a_pending_door_order_paid_with_full_downstream_effects(
    client: AsyncClient,
    db_session: AsyncSession,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    seeded_admin = await _login_admin(client, make_admin_user)
    buyer_email = f"mark-paid-{uuid.uuid4().hex}@example.test"
    order_id = await _checkout_door_order(
        client, make_event, make_show, make_ticket_type, make_event_config, buyer_email=buyer_email
    )

    response = await client.post(
        f"/api/v1/orders/{order_id}/mark-paid",
        json={"method_label": "SumUp card terminal", "reason": "Paid at the door"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["already_paid"] is False
    assert body["order"]["status"] == "paid"

    order = await _order_by_id(db_session, order_id)
    assert order.status == OrderStatus.PAID

    # Tickets are signed — a real HMAC QR token, not None (see
    # app.services.ticket_delivery.sign_order_tickets).
    tickets_result = await db_session.execute(select(Ticket).where(Ticket.order_id == order.id))
    tickets = tickets_result.scalars().all()
    assert len(tickets) == 1
    assert tickets[0].qr_token

    # Exactly one Invoice was issued for the order.
    assert await _invoice_count(db_session, order.id) == 1

    # Exactly one order.mark_paid audit entry, attributed to the REAL admin
    # (never SYSTEM_PRINCIPAL — see app.services.order_payment module
    # docstring), recording the method_label/reason.
    audit_result = await db_session.execute(
        select(AuditLogEntry)
        .where(AuditLogEntry.action == _MARK_PAID_ACTION)
        .where(AuditLogEntry.target_id == str(order.id))
    )
    entries = audit_result.scalars().all()
    assert len(entries) == 1
    assert entries[0].actor_id == seeded_admin.user.id
    detail = entries[0].detail
    assert detail is not None
    assert detail["method"] == "SumUp card terminal"
    assert detail["reason"] == "Paid at the door"

    # Confirmation email landed in Mailpit with both PDF attachments.
    message = await fetch_latest_mailpit_message_to(buyer_email)
    attachments = message["Attachments"]
    assert len(attachments) == 2
    attachments_by_name = {a["FileName"]: a for a in attachments}
    assert f"tickets-{order_id}.pdf" in attachments_by_name
    assert f"invoice-{order_id}.pdf" in attachments_by_name
    for attachment in attachments_by_name.values():
        pdf_bytes = await fetch_mailpit_attachment(message["ID"], attachment["PartID"])
        assert pdf_bytes.startswith(b"%PDF")

    await db_session.refresh(order)
    assert order.confirmation_email_sent_at is not None


# --- Idempotency ---------------------------------------------------------------


async def test_second_call_on_an_already_paid_order_is_a_no_op(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    await _login_admin(client, make_admin_user)
    order_id = await _checkout_door_order(client, make_event, make_show, make_ticket_type, make_event_config)
    send_calls = _patch_aiosmtplib_send_success(monkeypatch)

    first = await client.post(f"/api/v1/orders/{order_id}/mark-paid", json={"method_label": "cash"})
    assert first.status_code == 200, first.text
    assert first.json()["already_paid"] is False
    assert len(send_calls) == 1

    second = await client.post(
        f"/api/v1/orders/{order_id}/mark-paid", json={"method_label": "cash", "reason": "clicked twice"}
    )
    assert second.status_code == 200, second.text
    assert second.json()["already_paid"] is True
    assert second.json()["order"]["status"] == "paid"

    # No re-triggered send.
    assert len(send_calls) == 1, "a repeat mark-paid call must never re-send the confirmation email"

    order = await _order_by_id(db_session, order_id)
    assert order.status == OrderStatus.PAID
    assert await _audit_count(db_session, action=_MARK_PAID_ACTION, order_id=order.id) == 1
    assert await _invoice_count(db_session, order.id) == 1


# --- Recovery from cancelled/expired -------------------------------------------


async def test_recovers_a_cancelled_order(
    client: AsyncClient,
    db_session: AsyncSession,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """Documented allowed manual-recovery case: staff can still mark a
    lapsed cancelled order paid (e.g. a late bank transfer for an order
    whose Mollie payment had already been cancelled)."""
    await _login_admin(client, make_admin_user)
    order_id = await _checkout_door_order(client, make_event, make_show, make_ticket_type, make_event_config)
    order = await _order_by_id(db_session, order_id)
    order.status = OrderStatus.CANCELLED
    await db_session.commit()

    response = await client.post(
        f"/api/v1/orders/{order_id}/mark-paid",
        json={"method_label": "bank_transfer", "reason": "late payment recovered"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["already_paid"] is False
    assert body["order"]["status"] == "paid"

    await db_session.refresh(order)
    assert order.status == OrderStatus.PAID
    assert await _invoice_count(db_session, order.id) == 1
    tickets_result = await db_session.execute(select(Ticket).where(Ticket.order_id == order.id))
    assert all(t.qr_token for t in tickets_result.scalars().all())


async def test_recovers_an_expired_order(
    client: AsyncClient,
    db_session: AsyncSession,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    await _login_admin(client, make_admin_user)
    order_id = await _checkout_door_order(client, make_event, make_show, make_ticket_type, make_event_config)
    order = await _order_by_id(db_session, order_id)
    order.status = OrderStatus.EXPIRED
    await db_session.commit()

    response = await client.post(
        f"/api/v1/orders/{order_id}/mark-paid",
        json={"method_label": "cash", "reason": "recovered a lapsed order"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["already_paid"] is False
    assert body["order"]["status"] == "paid"

    await db_session.refresh(order)
    assert order.status == OrderStatus.PAID
    assert await _invoice_count(db_session, order.id) == 1
    tickets_result = await db_session.execute(select(Ticket).where(Ticket.order_id == order.id))
    assert all(t.qr_token for t in tickets_result.scalars().all())


async def test_recovering_a_cancelled_order_is_rejected_if_its_stock_was_resold(
    client: AsyncClient,
    db_session: AsyncSession,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """security-reviewer finding (Milestone 6): a cancelled/expired Order's
    stock is released back to the pool the moment it lapses (see
    ``app.services.stock``'s ``_RELEASED_STATUSES``) and can legitimately be
    resold to a DIFFERENT buyer. Recovering the ORIGINAL, now-lapsed order
    via mark-as-paid after that resale must be rejected (409) rather than
    silently re-activating a second, conflicting Ticket for a TicketType
    that's already sold out — see
    ``app.services.order_payment._verify_stock_for_resurrected_order``."""
    await _login_admin(client, make_admin_user)

    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=1)
    await make_event_config(
        event_id=event.id,
        sales_live_at=_PAST,
        enabled_payment_methods=[PaymentMethod.DOOR],
    )

    def _checkout_payload(buyer_suffix: str) -> dict[str, object]:
        return {
            "buyer_name": f"Buyer {buyer_suffix}",
            "buyer_email": f"resold-race-{buyer_suffix}-{uuid.uuid4().hex}@example.test",
            "buyer_address": "1 Test Street",
            "language": "en",
            "payment_method": "door",
            "items": [{"ticket_type_id": str(ticket_type.id), "quantity": 1}],
        }

    # Order A takes the only ticket, then lapses (e.g. its Mollie payment
    # would have expired — simulated directly here since only the
    # post-lapse state matters for this test).
    first_response = await client.post("/api/v1/public/checkout", json=_checkout_payload("A"))
    assert first_response.status_code == 201, first_response.text
    order_a_id = first_response.json()["id"]

    order_a = await _order_by_id(db_session, order_a_id)
    order_a.status = OrderStatus.CANCELLED
    await db_session.commit()

    # A different buyer legitimately purchases the now-freed last ticket.
    second_response = await client.post("/api/v1/public/checkout", json=_checkout_payload("B"))
    assert second_response.status_code == 201, second_response.text
    order_b_id = second_response.json()["id"]

    # Recovering Order A must now be rejected — its stock has already been
    # resold to Order B.
    recover_response = await client.post(
        f"/api/v1/orders/{order_a_id}/mark-paid",
        json={"method_label": "bank_transfer", "reason": "late payment, attempting recovery"},
    )
    assert recover_response.status_code == 409, recover_response.text

    await db_session.refresh(order_a)
    assert order_a.status == OrderStatus.CANCELLED, "must not be resurrected once its stock was resold"
    assert await _invoice_count(db_session, order_a.id) == 0
    tickets_result = await db_session.execute(select(Ticket).where(Ticket.order_id == order_a.id))
    assert all(t.qr_token is None for t in tickets_result.scalars().all())

    # Order B, the legitimate sale, is completely unaffected.
    order_b = await _order_by_id(db_session, order_b_id)
    assert order_b.status == OrderStatus.PENDING_DOOR
