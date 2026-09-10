"""Integration tests for ``POST /api/v1/public/mollie-webhook``
(``app.api.routes.public.mollie_webhook``).

Mocking-boundary decision: this webhook's whole job is to re-fetch a
payment's status from Mollie's own API and trust *that*, never the webhook
body (see ``app.services.mollie`` module docstring) — so a real Mollie
payment (created against a real, if test-mode, Mollie account) would be
needed to observe every status this suite needs to control (``paid``,
``failed``, ``expired``, ``canceled``), and this project has no real Mollie
account configured at all (per PROJECT_BRIEF.md's "no real Mollie account
configured" note). These tests therefore monkeypatch
``app.api.routes.public.fetch_mollie_payment_status`` directly (the name as
imported into the route module, not ``app.services.mollie``'s own
namespace, since Python's ``from x import y`` binds a separate reference in
the importing module) rather than mocking at the ``httpx`` boundary —
that's the smallest, most direct point of control for "Mollie says this
payment's status is X" and keeps these tests readable without needing to
fabricate realistic Mollie JSON response bodies. ``create_mollie_payment``
(used only during checkout, not the webhook) is monkeypatched the same way
in the setup helper below, so these Orders are created through the real
``POST /api/v1/public/checkout`` route (payment_method=mollie) without an
actual outbound call to Mollie.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.checkout as checkout_module
from app.models.audit_log import AuditLogEntry
from app.models.enums import MollieMode, OrderStatus, PaymentMethod, PublishStatus
from app.models.event import Event
from app.models.event_config import EventConfig
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
    *,
    quantity_available: int = 5,
) -> tuple[Order, str]:
    """Create a real, published Event/Show/TicketType with a Mollie key
    configured, then check out via ``payment_method=mollie`` through the
    real HTTP route — with ``create_mollie_payment`` monkeypatched so no
    real network call happens, but the Order still ends up in the exact
    state a real Mollie checkout would leave it in: ``pending``, with a
    ``mollie_payment_id`` set. Returns the persisted Order and that payment
    id (the webhook's lookup key).
    """
    payment_id = f"tr_test_{uuid.uuid4().hex[:16]}"

    async def _fake_create_mollie_payment(**kwargs: object) -> MolliePaymentCreated:
        return MolliePaymentCreated(payment_id=payment_id, checkout_url="https://www.mollie.com/checkout/fake")

    monkeypatch.setattr(checkout_module, "create_mollie_payment", _fake_create_mollie_payment)

    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=quantity_available)
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
            "buyer_email": "buyer@example.test",
            "buyer_address": "1 Test Street",
            "language": "en",
            "payment_method": "mollie",
            "items": [{"ticket_type_id": str(ticket_type.id), "quantity": 1}],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["payment_redirect_url"] == "https://www.mollie.com/checkout/fake"

    result = await db_session.execute(select(Order).where(Order.id == uuid.UUID(body["id"])))
    order = result.scalar_one()
    assert order.mollie_payment_id == payment_id
    return order, payment_id


def _patch_fetch_status(monkeypatch: pytest.MonkeyPatch, status_value: str) -> None:
    async def _fake_fetch(*, api_key: str, payment_id: str) -> str:
        return status_value

    monkeypatch.setattr("app.api.routes.public.fetch_mollie_payment_status", _fake_fetch)


async def _mark_paid_audit_count(db_session: AsyncSession, order_id: uuid.UUID) -> int:
    result = await db_session.execute(
        select(func.count())
        .select_from(AuditLogEntry)
        .where(AuditLogEntry.action == "order.mark_paid")
        .where(AuditLogEntry.target_id == str(order_id))
    )
    return result.scalar_one()


# --- THE idempotency test (PROJECT_BRIEF.md: "sending the same webhook
# twice must be explicitly tested") ------------------------------------------


async def test_duplicate_paid_webhook_delivery_is_idempotent(
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

    first_response = await client.post("/api/v1/public/mollie-webhook", data={"id": payment_id})
    assert first_response.status_code == 200

    await db_session.refresh(order)
    assert order.status == OrderStatus.PAID
    assert await _mark_paid_audit_count(db_session, order.id) == 1

    # The identical webhook delivered again (Mollie retry / buyer reload).
    second_response = await client.post("/api/v1/public/mollie-webhook", data={"id": payment_id})
    assert second_response.status_code == 200, "a retried webhook must never surface as a failure to Mollie"

    await db_session.refresh(order)
    assert order.status == OrderStatus.PAID, "must not be double-transitioned or errored on the second delivery"
    assert await _mark_paid_audit_count(db_session, order.id) == 1, "exactly ONE audit entry, not two"


async def test_webhook_reconciles_with_the_mode_pinned_at_payment_creation_not_the_current_config(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """security-reviewer finding: an admin flipping EventConfig.mollie_mode
    (test<->live) while an Order is still pending must not break
    reconciliation for that in-flight Order. The Order created here pins
    mollie_mode=test (make_event_config's default); after checkout, the
    event's config is flipped to live with NO live key set. If the webhook
    incorrectly re-read the live config, it would try (and fail) to
    resolve a live key. Fixed behavior: it uses Order.mollie_mode (test)
    and the webhook succeeds using the test key that was actually used to
    create the payment.
    """
    order, payment_id = await _setup_mollie_pending_order(
        client, db_session, monkeypatch, make_event, make_show, make_ticket_type, make_event_config
    )
    assert order.mollie_mode is not None and order.mollie_mode.value == "test"

    # Flip the event's config to live, with no live key configured — if
    # reconciliation used this live config instead of the pinned Order
    # mode, resolve_mollie_api_key would return None and the route would
    # 502 rather than reconcile.
    await db_session.refresh(order, attribute_names=["event"])
    config_result = await db_session.execute(
        select(EventConfig).where(EventConfig.event_id == order.event_id)
    )
    config = config_result.scalar_one()
    config.mollie_mode = MollieMode.LIVE
    await db_session.commit()

    seen_api_keys: list[str] = []

    async def _fake_fetch(*, api_key: str, payment_id: str) -> str:
        seen_api_keys.append(api_key)
        return "paid"

    monkeypatch.setattr("app.api.routes.public.fetch_mollie_payment_status", _fake_fetch)

    response = await client.post("/api/v1/public/mollie-webhook", data={"id": payment_id})

    assert response.status_code == 200
    assert seen_api_keys == ["test_dummy_key_never_used_over_network"], (
        "must reconcile with the TEST key pinned on the Order, not attempt to resolve a (unset) live key"
    )
    await db_session.refresh(order)
    assert order.status == OrderStatus.PAID


async def test_triplicate_paid_webhook_delivery_still_stays_idempotent(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """Belt-and-braces beyond the brief's minimum "twice" requirement: a
    third delivery (Mollie's real retry policy can attempt more than one
    retry) must stay just as safe."""
    order, payment_id = await _setup_mollie_pending_order(
        client, db_session, monkeypatch, make_event, make_show, make_ticket_type, make_event_config
    )
    _patch_fetch_status(monkeypatch, "paid")

    for _ in range(3):
        response = await client.post("/api/v1/public/mollie-webhook", data={"id": payment_id})
        assert response.status_code == 200

    await db_session.refresh(order)
    assert order.status == OrderStatus.PAID
    assert await _mark_paid_audit_count(db_session, order.id) == 1


# --- Failure-status mapping (item 7) ----------------------------------------


@pytest.mark.parametrize(
    ("mollie_status", "expected_order_status"),
    [
        ("failed", OrderStatus.CANCELLED),
        ("canceled", OrderStatus.CANCELLED),
        ("expired", OrderStatus.EXPIRED),
    ],
)
async def test_webhook_maps_terminal_failure_statuses_correctly(
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


@pytest.mark.parametrize("in_progress_status", ["open", "pending", "authorized"])
async def test_webhook_leaves_order_pending_for_in_progress_mollie_statuses(
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


# --- Route edge cases (item 7) -----------------------------------------------


async def test_webhook_with_unknown_payment_id_returns_200_safe_no_op(client: AsyncClient) -> None:
    """An id this app has no matching Order for (stale/foreign webhook,
    or Mollie itself retrying something already forgotten) must be a
    silent 200 — never a 404/500 that would make Mollie keep retrying
    forever."""
    response = await client.post(
        "/api/v1/public/mollie-webhook", data={"id": f"tr_unknown_{uuid.uuid4().hex}"}
    )
    assert response.status_code == 200


async def test_webhook_with_missing_id_field_returns_400_not_500(client: AsyncClient) -> None:
    response = await client.post("/api/v1/public/mollie-webhook", data={})
    assert response.status_code == 400


async def test_webhook_with_empty_id_field_returns_400_not_500(client: AsyncClient) -> None:
    response = await client.post("/api/v1/public/mollie-webhook", data={"id": ""})
    assert response.status_code == 400


async def test_webhook_returns_502_when_mollie_status_fetch_fails(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """A genuine transient failure (Mollie unreachable) must surface as a
    502 so Mollie's own retry mechanism tries again later, rather than a
    silently-swallowed payment confirmation."""
    from app.services.mollie import MollieApiError

    order, payment_id = await _setup_mollie_pending_order(
        client, db_session, monkeypatch, make_event, make_show, make_ticket_type, make_event_config
    )

    async def _fake_fetch_raises(*, api_key: str, payment_id: str) -> str:
        raise MollieApiError("simulated network failure")

    monkeypatch.setattr("app.api.routes.public.fetch_mollie_payment_status", _fake_fetch_raises)

    response = await client.post("/api/v1/public/mollie-webhook", data={"id": payment_id})
    assert response.status_code == 502

    await db_session.refresh(order)
    assert order.status == OrderStatus.PENDING
