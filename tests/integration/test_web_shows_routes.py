"""Integration tests for the backoffice Show + nested-TicketType CRUD web
surface (``app/web/routes/shows.py``, this branch's Show/TicketType-CRUD
section): ``GET/POST /events/{event_id}/shows`` and its nested
create/edit/delete routes for both Shows and TicketTypes.

Per this task's discipline note, does NOT re-test the underlying JSON API's
own business logic (field constraints, ``ON DELETE RESTRICT`` itself) —
that lives in ``tests/integration/test_shows_routes.py``, ``tests/
integration/test_ticket_types_routes.py``, and ``tests/integration/
test_cascade_delete.py``. Focuses on what the WEB layer adds: form
translation, the diff-against-orig-hidden-fields PATCH logic, error-flash
surfacing (including the real conflict messages when a Show/TicketType has
real sold Tickets under it), CSRF, and ``require_web_admin`` scoping.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient

from app.models.enums import AdminRole, PaymentMethod, PublishStatus
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.show import Show
from app.models.ticket_type import TicketType
from app.web.csrf import CSRF_COOKIE_NAME
from tests.integration.conftest import SeededAdmin

_PAST = datetime.now(UTC) - timedelta(days=1)


async def _api_login(client: AsyncClient, seeded: SeededAdmin) -> None:
    response = await client.post(
        "/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password}
    )
    assert response.status_code == 200


async def _get_csrf(client: AsyncClient, path: str) -> str:
    page = await client.get(path)
    assert page.status_code == 200
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert token
    return token


_SHOW_FORM = {
    "date": "2026-12-18",
    "doors_time": "19:30",
    "start_time": "20:00",
    "venue_name": "Test Venue",
    "venue_address": "1 Test Street",
    "capacity": "100",
    "status": "draft",
}


# --- Show: create/edit/delete happy paths -------------------------------------


async def test_create_show_happy_path_redirects_with_success_flash(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()
    token = await _get_csrf(client, f"/events/{event.id}/shows")

    response = await client.post(f"/events/{event.id}/shows", data={**_SHOW_FORM, "csrf_token": token})

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(f"/events/{event.id}/shows?open=")
    assert "flash=Show%20added.&flash_kind=success" in location

    shows_response = await client.get(f"/api/v1/events/{event.id}/shows")
    assert shows_response.status_code == 200
    assert len(shows_response.json()) == 1


async def test_edit_show_sends_only_changed_fields_and_no_op_flashes_no_changes(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]], make_show: Callable[..., Awaitable[Show]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()
    show = await make_show(event_id=event.id, venue_name="Original Venue")
    token = await _get_csrf(client, f"/events/{event.id}/shows")

    orig = {
        "orig_date": str(show.date),
        "orig_doors_time": show.doors_time.strftime("%H:%M"),
        "orig_start_time": show.start_time.strftime("%H:%M"),
        "orig_venue_name": show.venue_name,
        "orig_venue_address": show.venue_address,
        "orig_capacity": str(show.capacity),
        "orig_status": show.status.value,
    }

    # No-op submit: every visible field identical to its orig_ twin.
    no_op_response = await client.post(
        f"/events/{event.id}/shows/{show.id}/edit",
        data={
            "csrf_token": token,
            "date": str(show.date),
            "doors_time": show.doors_time.strftime("%H:%M"),
            "start_time": show.start_time.strftime("%H:%M"),
            "venue_name": show.venue_name,
            "venue_address": show.venue_address,
            "capacity": str(show.capacity),
            "status": show.status.value,
            **orig,
        },
    )
    assert no_op_response.status_code == 303
    assert "flash=No%20changes%20to%20save.&flash_kind=success" in no_op_response.headers["location"]

    # Real edit: venue_name changed.
    response = await client.post(
        f"/events/{event.id}/shows/{show.id}/edit",
        data={
            "csrf_token": token,
            "date": str(show.date),
            "doors_time": show.doors_time.strftime("%H:%M"),
            "start_time": show.start_time.strftime("%H:%M"),
            "venue_name": "New Venue Name",
            "venue_address": show.venue_address,
            "capacity": str(show.capacity),
            "status": show.status.value,
            **orig,
        },
    )
    assert response.status_code == 303
    assert "flash=Show%20updated.&flash_kind=success" in response.headers["location"]

    updated = await client.get(f"/api/v1/events/{event.id}/shows")
    assert updated.json()[0]["venue_name"] == "New Venue Name"


async def test_delete_show_with_no_ticket_types_succeeds(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]], make_show: Callable[..., Awaitable[Show]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()
    show = await make_show(event_id=event.id)
    token = await _get_csrf(client, f"/events/{event.id}/shows")

    response = await client.post(f"/events/{event.id}/shows/{show.id}/delete", data={"csrf_token": token})

    assert response.status_code == 303
    assert response.headers["location"] == f"/events/{event.id}/shows?flash=Show%20deleted.&flash_kind=success"


async def test_delete_show_returns_404_flash_for_unknown_show(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()
    token = await _get_csrf(client, f"/events/{event.id}/shows")

    response = await client.post(f"/events/{event.id}/shows/{uuid.uuid4()}/delete", data={"csrf_token": token})

    assert response.status_code == 303
    location = response.headers["location"]
    assert "flash_kind=error" in location
    assert "Show%20not%20found." in location


# --- Show duplicate ---------------------------------------------------------


async def test_duplicate_show_happy_path_redirects_with_success_flash(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    await make_ticket_type(show_id=show.id, name="Adult")
    token = await _get_csrf(client, f"/events/{event.id}/shows")

    response = await client.post(f"/events/{event.id}/shows/{show.id}/duplicate", data={"csrf_token": token})

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(f"/events/{event.id}/shows?open=")
    assert "flash=Show%20duplicated.&flash_kind=success" in location

    shows_response = await client.get(f"/api/v1/events/{event.id}/shows")
    assert shows_response.status_code == 200
    shows = shows_response.json()
    assert len(shows) == 2
    duplicate = next(s for s in shows if s["id"] != str(show.id))
    assert duplicate["status"] == "draft"

    ticket_types = (await client.get(f"/api/v1/shows/{duplicate['id']}/ticket-types")).json()
    assert [tt["name"] for tt in ticket_types] == ["Adult"]


async def test_duplicate_show_returns_404_flash_for_unknown_show(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()
    token = await _get_csrf(client, f"/events/{event.id}/shows")

    response = await client.post(
        f"/events/{event.id}/shows/{uuid.uuid4()}/duplicate", data={"csrf_token": token}
    )

    assert response.status_code == 303
    location = response.headers["location"]
    assert "flash_kind=error" in location
    assert "Show%20not%20found." in location


async def test_duplicate_show_missing_csrf_is_rejected(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()
    show = await make_show(event_id=event.id)

    response = await client.post(f"/events/{event.id}/shows/{show.id}/duplicate", data={})

    assert response.status_code == 422


# --- Show delete: blocked by real sold tickets under a nested TicketType -----


async def _create_show_with_a_real_sold_ticket(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> tuple[Event, Show]:
    event = await make_event(status=PublishStatus.PUBLISHED, name="Show Delete Conflict Event")
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=5)
    await make_event_config(
        event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR]
    )

    checkout = await client.post(
        "/api/v1/public/checkout",
        json={
            "buyer_name": "Door Buyer",
            "buyer_email": f"door-{uuid.uuid4().hex}@example.test",
            "buyer_address": "1 Test Street",
            "language": "en",
            "payment_method": "door",
            "items": [{"ticket_type_id": str(ticket_type.id), "quantity": 1}],
        },
    )
    assert checkout.status_code == 201, checkout.text
    order_id = checkout.json()["id"]
    mark_paid = await client.post(f"/api/v1/orders/{order_id}/mark-paid", json={"method_label": "cash"})
    assert mark_paid.status_code == 200, mark_paid.text

    return event, show


async def test_delete_show_with_real_sold_tickets_surfaces_409_as_flash_not_crash(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    await _api_login(client, await make_admin_user())
    event, show = await _create_show_with_a_real_sold_ticket(
        client, make_event, make_show, make_ticket_type, make_event_config
    )
    token = await _get_csrf(client, f"/events/{event.id}/shows")

    response = await client.post(f"/events/{event.id}/shows/{show.id}/delete", data={"csrf_token": token})

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(f"/events/{event.id}/shows?flash=")
    assert "flash_kind=error" in location
    assert "Cannot%20delete%3A%20this%20show%20has%20ticket%20types%20with%20existing%20orders." in location
    still_there = await client.get(f"/api/v1/events/{event.id}/shows")
    assert len(still_there.json()) == 1


async def test_delete_ticket_type_blocked_by_sold_tickets_surfaces_a_clean_409_not_500(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event(status=PublishStatus.PUBLISHED, name="TicketType Delete Conflict Event")
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=5)
    await make_event_config(
        event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR]
    )
    checkout = await client.post(
        "/api/v1/public/checkout",
        json={
            "buyer_name": "Door Buyer",
            "buyer_email": f"door-{uuid.uuid4().hex}@example.test",
            "buyer_address": "1 Test Street",
            "language": "en",
            "payment_method": "door",
            "items": [{"ticket_type_id": str(ticket_type.id), "quantity": 1}],
        },
    )
    assert checkout.status_code == 201, checkout.text
    order_id = checkout.json()["id"]
    mark_paid = await client.post(f"/api/v1/orders/{order_id}/mark-paid", json={"method_label": "cash"})
    assert mark_paid.status_code == 200, mark_paid.text
    token = await _get_csrf(client, f"/events/{event.id}/shows")

    response = await client.post(
        f"/events/{event.id}/shows/{show.id}/ticket-types/{ticket_type.id}/delete",
        data={"csrf_token": token},
    )

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(f"/events/{event.id}/shows?open={show.id}&flash=")
    assert "flash_kind=error" in location
    assert "Cannot%20delete%3A%20this%20ticket%20type%20has%20existing%20orders." in location
    still_there = await client.get(f"/api/v1/shows/{show.id}/ticket-types")
    assert len(still_there.json()) == 1


# --- TicketType: create/edit/delete happy paths -------------------------------


async def test_create_ticket_type_happy_path(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]], make_show: Callable[..., Awaitable[Show]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()
    show = await make_show(event_id=event.id)
    token = await _get_csrf(client, f"/events/{event.id}/shows")

    response = await client.post(
        f"/events/{event.id}/shows/{show.id}/ticket-types",
        data={
            "csrf_token": token,
            "name": "Adult",
            "price": "15.00",
            "quantity_available": "50",
            "service_fee_included": "true",
        },
    )

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(f"/events/{event.id}/shows?open={show.id}")
    assert "flash=Ticket%20type%20added.&flash_kind=success" in location
    ticket_types = await client.get(f"/api/v1/shows/{show.id}/ticket-types")
    assert len(ticket_types.json()) == 1


async def test_edit_ticket_type_no_op_flashes_no_changes_and_real_edit_persists(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()
    show = await make_show(event_id=event.id)
    ticket_type = await make_ticket_type(show_id=show.id, name="Adult", price="15.00")
    token = await _get_csrf(client, f"/events/{event.id}/shows")

    orig = {
        "orig_name": ticket_type.name,
        "orig_price": str(ticket_type.price),
        "orig_quantity_available": str(ticket_type.quantity_available),
        "orig_service_fee_included": "true" if ticket_type.service_fee_included else "false",
    }

    no_op = await client.post(
        f"/events/{event.id}/shows/{show.id}/ticket-types/{ticket_type.id}/edit",
        data={
            "csrf_token": token,
            "name": ticket_type.name,
            "price": str(ticket_type.price),
            "quantity_available": str(ticket_type.quantity_available),
            "service_fee_included": "true" if ticket_type.service_fee_included else "false",
            **orig,
        },
    )
    assert no_op.status_code == 303
    assert "flash=No%20changes%20to%20save.&flash_kind=success" in no_op.headers["location"]

    response = await client.post(
        f"/events/{event.id}/shows/{show.id}/ticket-types/{ticket_type.id}/edit",
        data={
            "csrf_token": token,
            "name": "Child",
            "price": str(ticket_type.price),
            "quantity_available": str(ticket_type.quantity_available),
            "service_fee_included": "true" if ticket_type.service_fee_included else "false",
            **orig,
        },
    )
    assert response.status_code == 303
    assert "flash=Ticket%20type%20updated.&flash_kind=success" in response.headers["location"]

    ticket_types = await client.get(f"/api/v1/shows/{show.id}/ticket-types")
    assert ticket_types.json()[0]["name"] == "Child"


async def test_delete_ticket_type_with_no_sold_tickets_succeeds(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()
    show = await make_show(event_id=event.id)
    ticket_type = await make_ticket_type(show_id=show.id)
    token = await _get_csrf(client, f"/events/{event.id}/shows")

    response = await client.post(
        f"/events/{event.id}/shows/{show.id}/ticket-types/{ticket_type.id}/delete",
        data={"csrf_token": token},
    )

    assert response.status_code == 303
    assert "flash=Ticket%20type%20deleted.&flash_kind=success" in response.headers["location"]


# --- Issue tickets manually --------------------------------------------------


async def test_issue_tickets_manually_happy_path_creates_a_paid_order(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()
    show = await make_show(event_id=event.id)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=10)
    token = await _get_csrf(client, f"/events/{event.id}/shows")

    response = await client.post(
        f"/events/{event.id}/shows/{show.id}/manual-orders",
        data={
            "csrf_token": token,
            "buyer_name": "Walk-up Buyer",
            f"qty_{ticket_type.id}": "2",
            "method_label": "cash",
            "reason": "box office walk-up sale",
        },
    )

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(f"/events/{event.id}/shows?open={show.id}")
    assert "flash=Tickets%20issued.&flash_kind=success" in location

    orders = await client.get(f"/api/v1/events/{event.id}/orders")
    assert orders.status_code == 200
    assert len(orders.json()) == 1
    assert orders.json()[0]["status"] == "paid"
    assert orders.json()[0]["payment_method"] == "manual"


async def test_issue_tickets_manually_requires_buyer_name(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()
    show = await make_show(event_id=event.id)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=10)
    token = await _get_csrf(client, f"/events/{event.id}/shows")

    response = await client.post(
        f"/events/{event.id}/shows/{show.id}/manual-orders",
        data={"csrf_token": token, "buyer_name": "", f"qty_{ticket_type.id}": "1", "method_label": "cash"},
    )

    assert response.status_code == 303
    location = response.headers["location"]
    assert "flash_kind=error" in location
    assert "Buyer%20name%20is%20required." in location


async def test_issue_tickets_manually_requires_at_least_one_selected_ticket(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()
    show = await make_show(event_id=event.id)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=10)
    token = await _get_csrf(client, f"/events/{event.id}/shows")

    response = await client.post(
        f"/events/{event.id}/shows/{show.id}/manual-orders",
        data={
            "csrf_token": token,
            "buyer_name": "Buyer",
            f"qty_{ticket_type.id}": "0",
            "method_label": "cash",
        },
    )

    assert response.status_code == 303
    location = response.headers["location"]
    assert "flash_kind=error" in location
    assert "Select%20at%20least%20one%20ticket" in location


async def test_issue_tickets_manually_missing_csrf_is_rejected(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()
    show = await make_show(event_id=event.id)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=10)

    response = await client.post(
        f"/events/{event.id}/shows/{show.id}/manual-orders",
        data={"buyer_name": "Buyer", f"qty_{ticket_type.id}": "1", "method_label": "cash"},
    )

    assert response.status_code == 403


# --- CSRF ----------------------------------------------------------------------


async def test_create_show_missing_csrf_is_rejected(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()

    response = await client.post(f"/events/{event.id}/shows", data=_SHOW_FORM)

    assert response.status_code == 422


async def test_create_show_wrong_csrf_is_rejected_403(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()
    await _get_csrf(client, f"/events/{event.id}/shows")

    response = await client.post(f"/events/{event.id}/shows", data={**_SHOW_FORM, "csrf_token": "wrong-token"})

    assert response.status_code == 403


async def test_delete_ticket_type_missing_csrf_is_rejected(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()
    show = await make_show(event_id=event.id)
    ticket_type = await make_ticket_type(show_id=show.id)

    response = await client.post(
        f"/events/{event.id}/shows/{show.id}/ticket-types/{ticket_type.id}/delete", data={}
    )

    assert response.status_code == 422


# --- require_web_admin scoping --------------------------------------------------


async def test_shows_manage_page_unauthenticated_redirects_to_login(
    client: AsyncClient, make_event: Callable[..., Awaitable[Event]]
) -> None:
    event = await make_event()

    response = await client.get(f"/events/{event.id}/shows")

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


async def test_shows_manage_page_unreachable_by_scanner_role(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    event = await make_event()
    seeded = await make_admin_user(role=AdminRole.SCANNER)
    await _api_login(client, seeded)

    response = await client.get(f"/events/{event.id}/shows")

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


async def test_create_show_unauthenticated_redirects_to_login(
    client: AsyncClient, make_event: Callable[..., Awaitable[Event]]
) -> None:
    event = await make_event()

    response = await client.post(f"/events/{event.id}/shows", data={**_SHOW_FORM, "csrf_token": "irrelevant"})

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")
