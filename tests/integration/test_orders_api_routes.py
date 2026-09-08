"""Integration tests for ``GET /api/v1/events/{event_id}/orders``
(``app.api.routes.orders.list_orders``, Milestone 4).

Admin-only scoping (agent/scanner/unauthenticated all rejected) is covered
by the shared table in ``tests/integration/test_deps_admin_scoping.py`` —
not repeated here. This file covers the route's own listing behavior: event
scoping (only that event's orders, never another event's), most-recent-
first ordering, 404 for an unknown event, empty list for an event with no
orders yet, and the ``OrderOut`` response shape including nested tickets.
"""

import uuid
from collections.abc import Awaitable, Callable

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import PaymentMethod, PublishStatus
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.show import Show
from app.models.ticket_type import TicketType
from tests.integration.conftest import SeededAdmin

_PAST = "2000-01-01T00:00:00+00:00"


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
    buyer_email: str | None = None,
) -> tuple[str, str, str]:
    """Create a real, published Event/Show/TicketType and check out a
    ``payment_method=door`` order against it via the real HTTP checkout
    route (the simplest payment method that needs no Mollie mocking).
    Returns (event_id, order_id, ticket_type_id)."""
    from datetime import UTC, datetime, timedelta

    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=10)
    await make_event_config(
        event_id=event.id,
        sales_live_at=datetime.now(UTC) - timedelta(days=1),
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
    return str(event.id), body["id"], str(ticket_type.id)


async def test_returns_404_for_unknown_event_id(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login_admin(client, make_admin_user)

    response = await client.get(f"/api/v1/events/{uuid.uuid4()}/orders")

    assert response.status_code == 404


async def test_returns_empty_list_for_event_with_no_orders_yet(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
) -> None:
    await _login_admin(client, make_admin_user)
    event = await make_event()

    response = await client.get(f"/api/v1/events/{event.id}/orders")

    assert response.status_code == 200
    assert response.json() == []


async def test_returns_only_this_events_orders_not_another_events(
    client: AsyncClient,
    db_session: AsyncSession,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    await _login_admin(client, make_admin_user)
    event_a_id, order_a_id, _ = await _checkout_door_order(
        client, make_event, make_show, make_ticket_type, make_event_config
    )
    event_b_id, order_b_id, _ = await _checkout_door_order(
        client, make_event, make_show, make_ticket_type, make_event_config
    )

    response_a = await client.get(f"/api/v1/events/{event_a_id}/orders")
    response_b = await client.get(f"/api/v1/events/{event_b_id}/orders")

    assert response_a.status_code == 200 and response_b.status_code == 200
    ids_a = {order["id"] for order in response_a.json()}
    ids_b = {order["id"] for order in response_b.json()}
    assert ids_a == {order_a_id}
    assert ids_b == {order_b_id}


async def test_orders_are_ordered_most_recent_first(
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

    order_ids: list[str] = []
    for _ in range(3):
        response = await client.post(
            "/api/v1/public/checkout",
            json={
                "buyer_name": "Buyer",
                "buyer_email": f"buyer-{uuid.uuid4().hex}@example.test",
                "buyer_address": "1 Test Street",
                "language": "en",
                "payment_method": "door",
                "items": [{"ticket_type_id": str(ticket_type.id), "quantity": 1}],
            },
        )
        assert response.status_code == 201
        order_ids.append(response.json()["id"])

    response = await client.get(f"/api/v1/events/{event.id}/orders")

    assert response.status_code == 200
    listed_ids = [order["id"] for order in response.json()]
    assert listed_ids == list(reversed(order_ids))


async def test_order_out_shape_including_nested_tickets(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    await _login_admin(client, make_admin_user)
    event_id, order_id, ticket_type_id = await _checkout_door_order(
        client, make_event, make_show, make_ticket_type, make_event_config, buyer_email="shape@example.test"
    )

    response = await client.get(f"/api/v1/events/{event_id}/orders")

    assert response.status_code == 200
    [order] = response.json()
    assert order["id"] == order_id
    assert order["event_id"] == event_id
    # A `door` order is not settled by checkout itself at this milestone
    # (mark-as-paid/`pending_door` reconciliation is Milestone 6 scope — see
    # app.services.checkout.perform_checkout's docstring: "a `door` order
    # skips this entirely and stays PENDING") — so it's still plain
    # `pending`, not `pending_door`, here.
    assert order["status"] == "pending"
    assert order["payment_method"] == "door"
    assert order["buyer_name"] == "Door Buyer"
    assert order["buyer_email"] == "shape@example.test"
    assert order["buyer_address"] == "1 Test Street"
    assert order["language"] == "en"
    # Decimal fields serialize as JSON strings (pydantic v2 default), not
    # floats — the exact shape ``orders_list.html``'s ``|float`` coercion
    # fix (this milestone's regression) exists to handle.
    assert order["total"] == "15.00"
    assert "created_at" in order
    assert order["mollie_checkout_url"] is None

    assert len(order["tickets"]) == 1
    [ticket] = order["tickets"]
    assert ticket["ticket_type_id"] == ticket_type_id
    assert ticket["ticket_type_name"]
    assert ticket["price"] == "15.00"
    # An unpaid order's ticket has not been signed yet (see
    # app.services.ticket_delivery.sign_order_tickets — only called once an
    # order is actually paid).
    assert ticket["qr_token"] is None
