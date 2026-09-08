"""Integration tests for the backoffice Orders web surface
(``app/web/routes/orders.py``, Milestone 4): the orders-list page
(``GET /events/{event_id}/orders``) and the resend form handler
(``POST /events/{event_id}/orders/{order_id}/resend``).

Mirrors ``tests/integration/test_web_backoffice_routes.py``'s conventions
(``_api_login`` bypassing the web login form/CSRF for tests whose focus is
something else, per-page unauthenticated-redirect tests rather than a
shared scoping table — that file has no such table for the web layer) and
``tests/integration/test_ticket_delivery.py``'s setup pattern (a real
Mollie checkout + webhook, with ``create_mollie_payment``/
``fetch_mollie_payment_status`` monkeypatched and ``aiosmtplib.send``
monkeypatched) for producing a real, paid Order with a real total.

The orders-list rendering test is the regression test for the real
``order.total`` float-coercion bug fixed alongside this milestone's other
work (see ``app/templates/backoffice/orders_list.html``'s ``|float``
filter and this module's own module docstring in
``app/web/routes/orders.py``): ``OrderOut.total`` arrives through the
web-to-API proxy as a JSON STRING (pydantic v2's default ``Decimal`` JSON
encoding), and Jinja's ``'%.2f' % order.total`` crashes on a string unless
coerced through ``|float`` first — this must render 200 with the correctly
formatted total, not 500.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import aiosmtplib
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.checkout as checkout_module
from app.models.enums import PaymentMethod, PublishStatus
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.order import Order
from app.models.show import Show
from app.models.ticket_type import TicketType
from app.services.mollie import MolliePaymentCreated
from app.web.csrf import CSRF_COOKIE_NAME
from tests.integration.conftest import SeededAdmin

_PAST = datetime.now(UTC) - timedelta(days=1)


async def _api_login(client: AsyncClient, seeded: SeededAdmin) -> None:
    response = await client.post(
        "/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password}
    )
    assert response.status_code == 200


def _patch_aiosmtplib_send_success(monkeypatch: pytest.MonkeyPatch) -> list[EmailMessage]:
    calls: list[EmailMessage] = []

    async def _fake_send(message: EmailMessage, **kwargs: object) -> tuple[dict[str, object], str]:
        calls.append(message)
        return {}, "OK"

    monkeypatch.setattr(aiosmtplib, "send", _fake_send)
    return calls


def _patch_fetch_status(monkeypatch: pytest.MonkeyPatch, status_value: str) -> None:
    async def _fake_fetch(*, api_key: str, payment_id: str) -> str:
        return status_value

    monkeypatch.setattr("app.api.routes.public.fetch_mollie_payment_status", _fake_fetch)


async def _create_paid_order(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> tuple[str, str]:
    """Real, published Event/Show/TicketType, checked out via
    ``payment_method=mollie`` through the real HTTP checkout route
    (``create_mollie_payment`` monkeypatched — no real network call), then
    confirmed ``paid`` through the real webhook route (``fetch_mollie_
    payment_status`` monkeypatched). Returns (event_id, order_id)."""
    payment_id = f"tr_test_{uuid.uuid4().hex[:16]}"

    async def _fake_create_mollie_payment(**kwargs: object) -> MolliePaymentCreated:
        return MolliePaymentCreated(payment_id=payment_id, checkout_url="https://www.mollie.com/checkout/fake")

    monkeypatch.setattr(checkout_module, "create_mollie_payment", _fake_create_mollie_payment)

    event = await make_event(status=PublishStatus.PUBLISHED, name="Paid Order Event")
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=5)
    await make_event_config(
        event_id=event.id,
        sales_live_at=_PAST,
        mollie_test_api_key="test_dummy_key_never_used_over_network",
        enabled_payment_methods=[PaymentMethod.MOLLIE],
    )

    checkout_response = await client.post(
        "/api/v1/public/checkout",
        json={
            "buyer_name": "Paid Buyer",
            "buyer_email": f"paid-{uuid.uuid4().hex}@example.test",
            "buyer_address": "1 Test Street",
            "language": "en",
            "payment_method": "mollie",
            "items": [{"ticket_type_id": str(ticket_type.id), "quantity": 1}],
        },
    )
    assert checkout_response.status_code == 201, checkout_response.text
    order_id = checkout_response.json()["id"]

    _patch_fetch_status(monkeypatch, "paid")
    webhook_response = await client.post("/api/v1/public/mollie-webhook", data={"id": payment_id})
    assert webhook_response.status_code == 200

    result = await db_session.execute(select(Order).where(Order.id == uuid.UUID(order_id)))
    order = result.scalar_one()
    assert order.status == "paid"

    return str(event.id), order_id


async def _create_pending_mollie_order(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
    *,
    event: Event,
    show: Show,
) -> str:
    """A second, still-``pending`` mollie order under the SAME event/show,
    so the orders-list page's resend-button-visibility test can assert on
    two rows in the same table."""
    payment_id = f"tr_test_{uuid.uuid4().hex[:16]}"

    async def _fake_create_mollie_payment(**kwargs: object) -> MolliePaymentCreated:
        return MolliePaymentCreated(payment_id=payment_id, checkout_url="https://www.mollie.com/checkout/fake")

    monkeypatch.setattr(checkout_module, "create_mollie_payment", _fake_create_mollie_payment)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=5)

    response = await client.post(
        "/api/v1/public/checkout",
        json={
            "buyer_name": "Pending Buyer",
            "buyer_email": f"pending-{uuid.uuid4().hex}@example.test",
            "buyer_address": "1 Test Street",
            "language": "en",
            "payment_method": "mollie",
            "items": [{"ticket_type_id": str(ticket_type.id), "quantity": 1}],
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "pending"
    return str(response.json()["id"])


# --- Unauthenticated access ---------------------------------------------


async def test_unauthenticated_orders_list_redirects_to_login(
    client: AsyncClient, make_event: Callable[..., Awaitable[Event]]
) -> None:
    event = await make_event()

    response = await client.get(f"/events/{event.id}/orders")

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


async def test_unauthenticated_resend_redirects_to_login(
    client: AsyncClient, make_event: Callable[..., Awaitable[Event]]
) -> None:
    event = await make_event()

    response = await client.post(
        f"/events/{event.id}/orders/{uuid.uuid4()}/resend", data={"csrf_token": "irrelevant"}
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


# --- Orders list rendering: the order.total float-coercion regression ---


async def test_orders_list_renders_real_paid_order_with_formatted_total_not_500(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    await _api_login(client, await make_admin_user())
    event_id, order_id = await _create_paid_order(
        client, db_session, monkeypatch, make_event, make_show, make_ticket_type, make_event_config
    )

    response = await client.get(f"/events/{event_id}/orders")

    assert response.status_code == 200
    assert "Paid Buyer" in response.text
    assert order_id[:8] in response.text
    # The default TicketType price from `make_ticket_type` is 15.00 (see
    # conftest.py) with 1 ticket ordered -> total 15.00.
    assert "&euro;15.00" in response.text


async def test_orders_list_shows_resend_button_only_for_paid_orders(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    await _api_login(client, await make_admin_user())
    event_id, paid_order_id = await _create_paid_order(
        client, db_session, monkeypatch, make_event, make_show, make_ticket_type, make_event_config
    )
    event_result = await db_session.execute(select(Event).where(Event.id == uuid.UUID(event_id)))
    event = event_result.scalar_one()
    show_result = await db_session.execute(select(Show).where(Show.event_id == event.id))
    show = show_result.scalars().first()
    assert show is not None
    pending_order_id = await _create_pending_mollie_order(
        client, monkeypatch, make_event, make_show, make_ticket_type, make_event_config, event=event, show=show
    )

    response = await client.get(f"/events/{event_id}/orders")

    assert response.status_code == 200
    resend_action_paid = f'action="/events/{event_id}/orders/{paid_order_id}/resend"'
    resend_action_pending = f'action="/events/{event_id}/orders/{pending_order_id}/resend"'
    assert resend_action_paid in response.text
    assert resend_action_pending not in response.text
    assert "Only paid orders have a confirmation email to resend." in response.text


# --- Resend form: CSRF ----------------------------------------------------


async def test_resend_form_with_missing_csrf_is_rejected(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()

    response = await client.post(f"/events/{event.id}/orders/{uuid.uuid4()}/resend", data={})

    assert response.status_code == 422


async def test_resend_form_with_wrong_csrf_is_rejected_403(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()
    page = await client.get(f"/events/{event.id}/orders")
    assert page.status_code == 200 and client.cookies.get(CSRF_COOKIE_NAME)

    response = await client.post(
        f"/events/{event.id}/orders/{uuid.uuid4()}/resend", data={"csrf_token": "wrong-token"}
    )

    assert response.status_code == 403


# --- Resend form: happy path / 404 / 409 ----------------------------------


async def test_resend_happy_path_redirects_with_success_flash_and_sends_a_second_email(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    await _api_login(client, await make_admin_user())
    send_calls = _patch_aiosmtplib_send_success(monkeypatch)
    event_id, order_id = await _create_paid_order(
        client, db_session, monkeypatch, make_event, make_show, make_ticket_type, make_event_config
    )
    assert len(send_calls) == 1  # the automatic payment-confirmation send

    page = await client.get(f"/events/{event_id}/orders")
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert page.status_code == 200 and token

    response = await client.post(
        f"/events/{event_id}/orders/{order_id}/resend", data={"csrf_token": token}
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/events/{event_id}/orders?flash=Confirmation%20email%20resent.&flash_kind=success"
    assert len(send_calls) == 2, "the resend action must trigger a genuinely new send"


async def test_resend_returns_404_flash_for_unknown_order(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()
    page = await client.get(f"/events/{event.id}/orders")
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert page.status_code == 200 and token

    response = await client.post(
        f"/events/{event.id}/orders/{uuid.uuid4()}/resend", data={"csrf_token": token}
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/events/{event.id}/orders?flash=")
    assert "flash_kind=error" in response.headers["location"]
    assert "Order%20not%20found." in response.headers["location"]


async def test_resend_returns_409_flash_for_a_not_yet_paid_order(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    await make_event_config(
        event_id=event.id,
        sales_live_at=_PAST,
        mollie_test_api_key="test_dummy_key_never_used_over_network",
        enabled_payment_methods=[PaymentMethod.MOLLIE],
    )
    pending_order_id = await _create_pending_mollie_order(
        client, monkeypatch, make_event, make_show, make_ticket_type, make_event_config, event=event, show=show
    )
    page = await client.get(f"/events/{event.id}/orders")
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert page.status_code == 200 and token

    response = await client.post(
        f"/events/{event.id}/orders/{pending_order_id}/resend", data={"csrf_token": token}
    )

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(f"/events/{event.id}/orders?flash=")
    assert "flash_kind=error" in location
    assert "Only%20paid%20orders%20have%20a%20confirmation%20email%20to%20resend." in location
