"""Integration tests for ``POST /api/v1/orders/{order_id}/erase-pii``
(``app.api.routes.orders.erase_pii``, Milestone 9's GDPR PII erasure — see
``app.services.gdpr.erase_order_pii``'s module docstring for the full
design).

Admin-only scoping (agent/scanner/unauthenticated all rejected) is covered
by the shared table in ``tests/integration/test_deps_admin_scoping.py`` —
not repeated here. This file covers the route's own behavior:
  - No-invoice orders erase immediately, no ``confirm`` needed.
  - Invoiced orders require ``confirm: true`` (409 without it, buyer
    fields unchanged; 200 with it).
  - Idempotency: a second call on an already-erased order is a safe no-op
    (same placeholders, no error) — a second truthful audit entry is
    correct here, not a bug (see ``app.services.gdpr`` module docstring).
  - The audit entry's ``detail`` never leaks the erased PII.
  - The Order row, its Tickets, and its Invoice (if any) survive erasure
    untouched apart from the three buyer fields.
  - 404 for an unknown/malformed order id.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLogEntry
from app.models.enums import PaymentMethod, PublishStatus
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.invoice import Invoice
from app.models.order import Order
from app.models.show import Show
from app.models.ticket import Ticket
from app.models.ticket_type import TicketType
from tests.integration.conftest import SeededAdmin

_PAST = datetime.now(UTC) - timedelta(days=1)
_ERASED_ACTION = "order.pii_erased"


# --- Shared setup helpers ----------------------------------------------------


async def _login_admin(client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]) -> None:
    seeded = await make_admin_user()
    login = await client.post(
        "/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password}
    )
    assert login.status_code == 200


async def _checkout_door_order(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
    *,
    buyer_name: str = "Real Buyer",
    buyer_email: str | None = None,
    buyer_address: str = "1 Test Street",
) -> str:
    """Check out a real, unpaid ``payment_method=door`` order — no Invoice
    yet, since Milestone 5 only issues one on payment confirmation."""
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
            "buyer_name": buyer_name,
            "buyer_email": buyer_email or f"gdpr-{uuid.uuid4().hex}@example.test",
            "buyer_address": buyer_address,
            "language": "en",
            "payment_method": "door",
            "items": [{"ticket_type_id": str(ticket_type.id), "quantity": 1}],
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _checkout_simulated_paid_order(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
    *,
    buyer_name: str = "Real Buyer",
    buyer_email: str | None = None,
    buyer_address: str = "1 Test Street",
) -> str:
    """Check out via a draft Event's preview token with
    ``payment_method=mollie`` and no Mollie key configured, taking the
    preview-mode simulated-payment path (see
    ``app.services.checkout._initiate_mollie_payment``) — a genuinely
    ``paid`` Order with its Invoice already issued, no Mollie mocking
    needed (mirrors ``test_invoice_download_route.py``'s helper)."""
    event = await make_event(status=PublishStatus.DRAFT)
    show = await make_show(event_id=event.id, status=PublishStatus.DRAFT)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=10)
    await make_event_config(event_id=event.id, enabled_payment_methods=[PaymentMethod.MOLLIE])

    response = await client.post(
        "/api/v1/public/checkout",
        json={
            "buyer_name": buyer_name,
            "buyer_email": buyer_email or f"gdpr-paid-{uuid.uuid4().hex}@example.test",
            "buyer_address": buyer_address,
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


async def _order_by_id(db_session: AsyncSession, order_id: str) -> Order:
    result = await db_session.execute(select(Order).where(Order.id == uuid.UUID(order_id)))
    return result.scalar_one()


async def _audit_entries(db_session: AsyncSession, *, order_id: uuid.UUID) -> list[AuditLogEntry]:
    result = await db_session.execute(
        select(AuditLogEntry)
        .where(AuditLogEntry.action == _ERASED_ACTION)
        .where(AuditLogEntry.target_id == str(order_id))
        .order_by(AuditLogEntry.created_at)
    )
    return list(result.scalars().all())


async def _invoice_count(db_session: AsyncSession, order_id: uuid.UUID) -> int:
    result = await db_session.execute(select(func.count()).select_from(Invoice).where(Invoice.order_id == order_id))
    return result.scalar_one()


async def _ticket_count(db_session: AsyncSession, order_id: uuid.UUID) -> int:
    result = await db_session.execute(select(func.count()).select_from(Ticket).where(Ticket.order_id == order_id))
    return result.scalar_one()


# --- 404 -----------------------------------------------------------------


async def test_returns_404_for_unknown_order_id(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login_admin(client, make_admin_user)

    response = await client.post(f"/api/v1/orders/{uuid.uuid4()}/erase-pii", json={})

    assert response.status_code == 404


async def test_returns_404_for_malformed_order_id(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login_admin(client, make_admin_user)

    response = await client.post("/api/v1/orders/not-a-uuid/erase-pii", json={})

    assert response.status_code == 404


# --- No invoice: erases immediately, no confirm needed --------------------


async def test_erases_an_order_with_no_invoice_immediately_without_confirm(
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

    response = await client.post(f"/api/v1/orders/{order_id}/erase-pii", json={})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["had_invoice"] is False
    assert body["order"]["id"] == order_id

    order = await _order_by_id(db_session, order_id)
    assert order.buyer_name == "[erased]"
    assert order.buyer_email == f"erased-{order.id}@erased.invalid"
    assert order.buyer_address == "[erased]"

    # Exactly one audit entry, and its detail carries no PII.
    entries = await _audit_entries(db_session, order_id=order.id)
    assert len(entries) == 1
    assert entries[0].detail is None


# --- Invoiced order: confirmation friction ---------------------------------


async def test_erasing_an_invoiced_order_without_confirm_is_rejected_and_leaves_fields_unchanged(
    client: AsyncClient,
    db_session: AsyncSession,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    await _login_admin(client, make_admin_user)
    order_id = await _checkout_simulated_paid_order(
        client, make_event, make_show, make_ticket_type, make_event_config, buyer_name="Invoiced Buyer"
    )

    response = await client.post(f"/api/v1/orders/{order_id}/erase-pii", json={})

    assert response.status_code == 409, response.text
    assert "invoice" in response.json()["detail"].lower()

    order = await _order_by_id(db_session, order_id)
    assert order.buyer_name == "Invoiced Buyer"
    assert order.buyer_email != f"erased-{order.id}@erased.invalid"
    assert order.buyer_address != "[erased]"
    assert await _audit_entries(db_session, order_id=order.id) == []


async def test_erasing_an_invoiced_order_with_confirm_false_is_also_rejected(
    client: AsyncClient,
    db_session: AsyncSession,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    await _login_admin(client, make_admin_user)
    order_id = await _checkout_simulated_paid_order(
        client, make_event, make_show, make_ticket_type, make_event_config, buyer_name="Invoiced Buyer"
    )

    response = await client.post(f"/api/v1/orders/{order_id}/erase-pii", json={"confirm": False})

    assert response.status_code == 409, response.text
    order = await _order_by_id(db_session, order_id)
    assert order.buyer_name == "Invoiced Buyer"


async def test_erasing_an_invoiced_order_with_confirm_true_succeeds(
    client: AsyncClient,
    db_session: AsyncSession,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    await _login_admin(client, make_admin_user)
    order_id = await _checkout_simulated_paid_order(
        client, make_event, make_show, make_ticket_type, make_event_config, buyer_name="Invoiced Buyer"
    )
    invoice_count_before = await _invoice_count(db_session, uuid.UUID(order_id))
    ticket_count_before = await _ticket_count(db_session, uuid.UUID(order_id))
    invoice_before = (
        await db_session.execute(select(Invoice).where(Invoice.order_id == uuid.UUID(order_id)))
    ).scalar_one()
    invoice_number = invoice_before.number

    # First, unconfirmed attempt is rejected (retried below with confirm).
    rejected = await client.post(f"/api/v1/orders/{order_id}/erase-pii", json={})
    assert rejected.status_code == 409

    response = await client.post(f"/api/v1/orders/{order_id}/erase-pii", json={"confirm": True})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["had_invoice"] is True

    order = await _order_by_id(db_session, order_id)
    assert order.buyer_name == "[erased]"
    assert order.buyer_email == f"erased-{order.id}@erased.invalid"
    assert order.buyer_address == "[erased]"

    # Order/Tickets/Invoice all still exist, unchanged in count, invoice
    # number untouched — only the three buyer fields were overwritten.
    assert await _invoice_count(db_session, order.id) == invoice_count_before == 1
    assert await _ticket_count(db_session, order.id) == ticket_count_before == 1
    await db_session.refresh(order, attribute_names=["invoice"])
    assert order.invoice is not None
    assert order.invoice.number == invoice_number

    entries = await _audit_entries(db_session, order_id=order.id)
    assert len(entries) == 1
    assert entries[0].detail is None


# --- Idempotency -------------------------------------------------------------


async def test_second_erase_call_on_an_already_erased_order_is_a_safe_no_op(
    client: AsyncClient,
    db_session: AsyncSession,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """Per ``app.services.gdpr``'s module docstring: calling erase-pii
    again on an already-erased Order re-applies the same fixed
    placeholders harmlessly and appends a second (truthful) audit entry —
    that second entry is correct behavior, not a bug, so this deliberately
    does NOT assert exactly-one audit entry after the second call (unlike
    the single-call assertions above)."""
    await _login_admin(client, make_admin_user)
    order_id = await _checkout_door_order(client, make_event, make_show, make_ticket_type, make_event_config)

    first = await client.post(f"/api/v1/orders/{order_id}/erase-pii", json={})
    assert first.status_code == 200, first.text

    second = await client.post(f"/api/v1/orders/{order_id}/erase-pii", json={})
    assert second.status_code == 200, second.text

    order = await _order_by_id(db_session, order_id)
    assert order.buyer_name == "[erased]"
    assert order.buyer_email == f"erased-{order.id}@erased.invalid"
    assert order.buyer_address == "[erased]"

    entries = await _audit_entries(db_session, order_id=order.id)
    assert len(entries) == 2
    assert all(entry.detail is None for entry in entries)
