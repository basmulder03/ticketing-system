"""Integration tests for Milestone 4's email dispatch orchestration
(``app.services.ticket_delivery``), its two real call sites (the Mollie
webhook — ``app.api.routes.public.mollie_webhook`` — and the admin resend
route — ``app.api.routes.orders.resend_confirmation_email``), against a
real Postgres DB.

Mocking-boundary decisions, mirroring ``tests/integration/test_mollie_webhook.py``:
- Orders are created and moved to ``paid`` through the real HTTP checkout
  and webhook routes, with ``create_mollie_payment``/
  ``fetch_mollie_payment_status`` monkeypatched (no real Mollie account —
  see that file's docstring for the established precedent).
- Most tests here monkeypatch ``aiosmtplib.send`` directly — the smallest,
  most direct point of control for "did exactly one real send attempt
  happen, and with what content" without needing a real Mailpit round trip
  for every single scenario (idempotency, failure handling, freshness of
  the computed "days until show" value).
- ONE test (``test_confirmation_email_lands_in_mailpit_with_adversarial_content_sanitized``)
  deliberately does NOT monkeypatch the send step at all: it performs a
  real send against the local Mailpit sink and asserts on the actual
  delivered message via Mailpit's HTTP API (see
  ``tests/integration/conftest.py``'s ``fetch_latest_mailpit_message_to``)
  — PROJECT_BRIEF.md's "email content generation checked against actual
  rendered output... rendered against Mailpit ... not just 'did send() get
  called'".
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
import app.services.ticket_delivery as ticket_delivery_module
from app.core.config import get_settings
from app.models.audit_log import AuditLogEntry
from app.models.email_template import EmailTemplate
from app.models.enums import EmailTemplateType, OrderStatus, PaymentMethod, PublishStatus
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.order import Order
from app.models.show import Show
from app.models.ticket_type import TicketType
from app.services.mollie import MolliePaymentCreated
from tests.integration.conftest import (
    SeededAdmin,
    fetch_latest_mailpit_message_to,
    fetch_mailpit_attachment,
)

_PAST = datetime.now(UTC) - timedelta(days=1)
_SENT_ACTION = "order.confirmation_email.sent"
_FAILED_ACTION = "order.confirmation_email.failed"


# --- Shared setup helpers ----------------------------------------------------


async def _setup_mollie_pending_order(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
    *,
    buyer_email: str | None = None,
    smtp_host: str | None = get_settings().seed_smtp_host,
) -> tuple[Order, str]:
    """Same pattern as ``test_mollie_webhook.py``'s helper of the same
    name: a real, published Event/Show/TicketType checked out via
    ``payment_method=mollie`` through the real HTTP route, with
    ``create_mollie_payment`` monkeypatched so no real network call
    happens. Returns the persisted (still-``pending``) Order and its
    Mollie payment id.

    ``smtp_host`` defaults to ``Settings.seed_smtp_host`` (``"mailpit"``
    inside docker-compose, ``"localhost"`` when ``SEED_SMTP_HOST`` is
    overridden for a native/CI run) rather than a hardcoded hostname —
    see ``make_event_config``'s docstring in ``conftest.py`` for why a
    hardcoded ``"mailpit"`` here silently broke every real-send test the
    moment the suite ran outside the docker-compose network.
    """
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
        smtp_host=smtp_host,
    )

    response = await client.post(
        "/api/v1/public/checkout",
        json={
            "buyer_name": "Buyer",
            "buyer_email": buyer_email or f"buyer-{uuid.uuid4().hex}@example.test",
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


def _patch_aiosmtplib_send_success(monkeypatch: pytest.MonkeyPatch) -> list[EmailMessage]:
    """Monkeypatch ``aiosmtplib.send`` to succeed without a real network
    call, recording every call's ``EmailMessage`` for assertions. Returns
    the (initially empty) list messages are appended to."""
    calls: list[EmailMessage] = []

    async def _fake_send(message: EmailMessage, **kwargs: object) -> tuple[dict[str, object], str]:
        calls.append(message)
        return {}, "OK"

    monkeypatch.setattr(aiosmtplib, "send", _fake_send)
    return calls


def _patch_aiosmtplib_send_raises(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> list[int]:
    call_count: list[int] = []

    async def _fake_send(message: object, **kwargs: object) -> None:
        call_count.append(1)
        raise exc

    monkeypatch.setattr(aiosmtplib, "send", _fake_send)
    return call_count


async def _audit_action_count(db_session: AsyncSession, order_id: uuid.UUID, action: str) -> int:
    result = await db_session.execute(
        select(func.count())
        .select_from(AuditLogEntry)
        .where(AuditLogEntry.action == action)
        .where(AuditLogEntry.target_id == str(order_id))
    )
    return result.scalar_one()


async def _login_admin(client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]) -> None:
    seeded = await make_admin_user()
    login = await client.post(
        "/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password}
    )
    assert login.status_code == 200


# --- Exactly-once dispatch (item 4) -----------------------------------------


async def test_confirmation_email_dispatched_exactly_once_across_duplicate_webhook_deliveries(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """Mirrors ``test_mollie_webhook.py``'s idempotency pattern, but proves
    the NEW Milestone 4 consumer of ``already_paid``: the email send itself
    must be attempted exactly once, not twice, across a retried webhook
    delivery for the same Order."""
    order, payment_id = await _setup_mollie_pending_order(
        client, db_session, monkeypatch, make_event, make_show, make_ticket_type, make_event_config
    )
    _patch_fetch_status(monkeypatch, "paid")
    send_calls = _patch_aiosmtplib_send_success(monkeypatch)

    first = await client.post("/api/v1/public/mollie-webhook", data={"id": payment_id})
    assert first.status_code == 200
    assert len(send_calls) == 1

    second = await client.post("/api/v1/public/mollie-webhook", data={"id": payment_id})
    assert second.status_code == 200
    assert len(send_calls) == 1, "a retried webhook must never trigger a second send attempt"

    await db_session.refresh(order)
    assert order.status == OrderStatus.PAID
    assert await _audit_action_count(db_session, order.id, _SENT_ACTION) == 1


async def test_confirmation_email_dispatched_exactly_once_across_triplicate_webhook_deliveries(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    _order, payment_id = await _setup_mollie_pending_order(
        client, db_session, monkeypatch, make_event, make_show, make_ticket_type, make_event_config
    )
    _patch_fetch_status(monkeypatch, "paid")
    send_calls = _patch_aiosmtplib_send_success(monkeypatch)

    for _ in range(3):
        response = await client.post("/api/v1/public/mollie-webhook", data={"id": payment_id})
        assert response.status_code == 200

    assert len(send_calls) == 1


# --- SMTP/render failure never rolls back the payment (item 5) -------------


async def test_smtp_send_failure_does_not_roll_back_payment_and_is_audited(
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
    _patch_aiosmtplib_send_raises(monkeypatch, aiosmtplib.SMTPConnectError("simulated SMTP outage"))

    response = await client.post("/api/v1/public/mollie-webhook", data={"id": payment_id})

    # The webhook route itself must still report success to Mollie — a
    # buyer-facing email failure is never Mollie's problem to retry.
    assert response.status_code == 200

    await db_session.refresh(order)
    assert order.status == OrderStatus.PAID, "a real, already-confirmed payment must never be rolled back"
    assert order.confirmation_email_sent_at is None

    assert await _audit_action_count(db_session, order.id, "order.mark_paid") == 1
    assert await _audit_action_count(db_session, order.id, _FAILED_ACTION) == 1
    assert await _audit_action_count(db_session, order.id, _SENT_ACTION) == 0


async def test_pdf_render_failure_does_not_roll_back_payment_and_is_audited(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """A rendering bug (weasyprint, template substitution, etc.) must be
    treated exactly like an SMTP failure — see
    ``app.services.ticket_delivery.send_order_confirmation_email``'s
    docstring, which cites the real weasyprint/pydyf version-mismatch bug
    this broad-except was added for."""
    order, payment_id = await _setup_mollie_pending_order(
        client, db_session, monkeypatch, make_event, make_show, make_ticket_type, make_event_config
    )
    _patch_fetch_status(monkeypatch, "paid")

    def _boom(**kwargs: object) -> bytes:
        raise RuntimeError("simulated weasyprint/pydyf failure")

    monkeypatch.setattr(ticket_delivery_module, "render_tickets_pdf", _boom)
    send_calls = _patch_aiosmtplib_send_success(monkeypatch)

    response = await client.post("/api/v1/public/mollie-webhook", data={"id": payment_id})
    assert response.status_code == 200

    await db_session.refresh(order)
    assert order.status == OrderStatus.PAID
    assert order.confirmation_email_sent_at is None
    assert len(send_calls) == 0, "must never attempt to send an email built from a failed PDF render"
    assert await _audit_action_count(db_session, order.id, _FAILED_ACTION) == 1


async def test_smtp_not_configured_fails_cleanly_and_is_audited(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    order, payment_id = await _setup_mollie_pending_order(
        client,
        db_session,
        monkeypatch,
        make_event,
        make_show,
        make_ticket_type,
        make_event_config,
        smtp_host=None,
    )
    _patch_fetch_status(monkeypatch, "paid")

    response = await client.post("/api/v1/public/mollie-webhook", data={"id": payment_id})
    assert response.status_code == 200

    await db_session.refresh(order)
    assert order.status == OrderStatus.PAID
    assert order.confirmation_email_sent_at is None
    assert await _audit_action_count(db_session, order.id, _FAILED_ACTION) == 1


# --- Admin resend action (item 6) -------------------------------------------


async def test_resend_returns_404_for_unknown_order(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login_admin(client, make_admin_user)
    response = await client.post(f"/api/v1/orders/{uuid.uuid4()}/resend-confirmation-email")
    assert response.status_code == 404


async def test_resend_returns_409_for_a_not_yet_paid_order(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    order, _payment_id = await _setup_mollie_pending_order(
        client, db_session, monkeypatch, make_event, make_show, make_ticket_type, make_event_config
    )
    await _login_admin(client, make_admin_user)

    response = await client.post(f"/api/v1/orders/{order.id}/resend-confirmation-email")
    assert response.status_code == 409


async def test_resend_resends_a_paid_order_and_updates_confirmation_email_sent_at(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    order, payment_id = await _setup_mollie_pending_order(
        client, db_session, monkeypatch, make_event, make_show, make_ticket_type, make_event_config
    )
    _patch_fetch_status(monkeypatch, "paid")
    send_calls = _patch_aiosmtplib_send_success(monkeypatch)

    webhook_response = await client.post("/api/v1/public/mollie-webhook", data={"id": payment_id})
    assert webhook_response.status_code == 200
    assert len(send_calls) == 1

    await db_session.refresh(order)
    first_sent_at = order.confirmation_email_sent_at
    assert first_sent_at is not None

    await _login_admin(client, make_admin_user)
    resend_response = await client.post(f"/api/v1/orders/{order.id}/resend-confirmation-email")
    assert resend_response.status_code == 200
    assert resend_response.json() == {"sent": True}

    # Unlike the automatic payment-confirmation path (gated on
    # `already_paid`), an explicit admin resend is NOT gated — every call
    # dispatches a genuinely new send.
    assert len(send_calls) == 2

    await db_session.refresh(order)
    assert order.confirmation_email_sent_at is not None
    assert order.confirmation_email_sent_at >= first_sent_at

    assert await _audit_action_count(db_session, order.id, _SENT_ACTION) == 2


async def test_resend_recomputes_days_until_show_fresh_not_a_cached_value(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """``Order`` has no "days until show"/countdown column at all (see
    ``app.models.order.Order`` — only ``confirmation_email_sent_at`` is
    tracked), so there is nothing on the Order itself that COULD go stale.
    This test proves the call-site behavior directly: ``compute_days_until_show``
    (see ``app.services.email_render``) is invoked fresh on every render,
    so a resend's rendered "time until the show" line reflects whatever is
    true *at resend time*, not whatever was true at the initial send.
    """
    order, payment_id = await _setup_mollie_pending_order(
        client, db_session, monkeypatch, make_event, make_show, make_ticket_type, make_event_config
    )
    _patch_fetch_status(monkeypatch, "paid")
    send_calls = _patch_aiosmtplib_send_success(monkeypatch)

    day_values = iter([10, 3])

    def _fake_compute_days_until_show(show_date: object, *, now: object = None) -> int:
        return next(day_values)

    monkeypatch.setattr(
        "app.services.email_render.compute_days_until_show", _fake_compute_days_until_show
    )

    webhook_response = await client.post("/api/v1/public/mollie-webhook", data={"id": payment_id})
    assert webhook_response.status_code == 200
    assert len(send_calls) == 1
    initial_body = send_calls[0].get_body(("plain",))
    assert initial_body is not None
    assert "10 days to go" in initial_body.get_content()

    await _login_admin(client, make_admin_user)
    resend_response = await client.post(f"/api/v1/orders/{order.id}/resend-confirmation-email")
    assert resend_response.status_code == 200
    assert len(send_calls) == 2
    resend_body = send_calls[1].get_body(("plain",))
    assert resend_body is not None
    resend_text = resend_body.get_content()
    assert "3 days to go" in resend_text
    assert "10 days to go" not in resend_text


# --- Real Mailpit content check (adversarial buyer content, item 2 + 5) ----


async def test_confirmation_email_lands_in_mailpit_with_adversarial_content_sanitized(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """A real, unmocked send against Mailpit: an admin/agent-authored
    ``EmailTemplate`` substituting an adversarial ``buyer_name`` (HTML AND
    a CRLF header-injection attempt combined) plus an adversarial
    TicketType name — verifying against Mailpit's HTTP API what a real
    recipient's inbox would actually contain, not just that ``send()`` was
    called."""
    event = await make_event(status=PublishStatus.DRAFT)
    show = await make_show(event_id=event.id, status=PublishStatus.DRAFT)
    ticket_type = await make_ticket_type(show_id=show.id, name='<script>alert(1)</script>')
    await make_event_config(
        event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.MOLLIE]
    )

    async def _forbid_real_mollie(**kwargs: object) -> MolliePaymentCreated:
        raise AssertionError("preview-mode simulated payment must never call the real Mollie API")

    monkeypatch.setattr(checkout_module, "create_mollie_payment", _forbid_real_mollie)

    template = EmailTemplate(
        event_id=event.id,
        language="en",
        template_type=EmailTemplateType.ORDER_CONFIRMATION_TICKET.value,
        subject="Tickets for {{event_name}} — {{buyer_name}}",
        body="<p>Hi {{buyer_name}},</p><p>Thanks for {{unknown_placeholder}}!</p>",
    )
    db_session.add(template)
    await db_session.commit()

    buyer_email = f"e2e-{uuid.uuid4().hex}@example.test"
    adversarial_buyer_name = "<b>Evil</b> Buyer\r\nBcc: evil@evil.test"

    response = await client.post(
        "/api/v1/public/checkout",
        json={
            "buyer_name": adversarial_buyer_name,
            "buyer_email": buyer_email,
            "buyer_address": "1 Test Street",
            "language": "en",
            "payment_method": "mollie",
            "items": [{"ticket_type_id": str(ticket_type.id), "quantity": 1}],
            "preview_token": event.preview_token,
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "paid"

    message = await fetch_latest_mailpit_message_to(buyer_email)

    subject = message["Subject"]
    # Subject is a plain-text destination (escape_html=False) — HTML in it
    # is inert (a mail client shows it as literal text, never renders it),
    # so only the CRLF/header-injection guard applies here, not HTML
    # escaping (see app.services.email_placeholders module docstring).
    assert "\r" not in subject and "\n" not in subject, "CRLF must never reach a real email header"
    assert "Bcc: evil@evil.test" in subject, "CRLF stripped but the rest of the value survives, merged"

    html_body = message["HTML"]
    assert "<script>alert(1)</script>" not in html_body, "adversarial TicketType name must never be unescaped"
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_body
    assert "<b>Evil</b>" not in html_body, "adversarial buyer_name HTML must be escaped in the HTML body"
    assert "&lt;b&gt;Evil&lt;/b&gt;" in html_body
    assert "\r\nBcc:" not in html_body

    text_body = message["Text"]
    # An unknown placeholder in admin/agent-authored body text degrades to
    # showing the literal placeholder text, never a crash or silent drop.
    assert "{{unknown_placeholder}}" in text_body

    attachments = message["Attachments"]
    assert len(attachments) == 1
    assert attachments[0]["ContentType"] == "application/pdf"

    pdf_bytes = await fetch_mailpit_attachment(message["ID"], attachments[0]["PartID"])
    assert pdf_bytes.startswith(b"%PDF")

    order_result = await db_session.execute(select(Order).where(Order.buyer_email == buyer_email))
    order = order_result.scalar_one()
    assert order.confirmation_email_sent_at is not None
