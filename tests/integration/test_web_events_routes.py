"""Integration tests for the backoffice Event CRUD web surface
(``app/web/routes/events.py``, this branch's Event-CRUD section):
``GET/POST /events/new``, ``GET/POST /events/{event_id}/edit``, and
``POST /events/{event_id}/delete``.

Per this task's discipline note, this file does NOT re-test the underlying
JSON API's own business logic (slug uniqueness, FK-restrict-on-delete) —
that already lives in ``tests/integration/test_events_routes.py`` and
``tests/integration/test_cascade_delete.py``. It only covers what the WEB
layer adds: form-to-API translation, error-flash surfacing, the
diff-against-current-state PATCH logic, CSRF, ``require_web_admin`` scoping,
and template rendering (the preview-link affordance).

Mirrors ``tests/integration/test_web_orders_routes.py``'s conventions:
``_api_login`` bypasses the web login form/CSRF (this file's focus is the
CRUD routes, not the login flow), and CSRF/scoping tests are written
per-route rather than via a shared table (this file has none, same as that
one).
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from app.core.config import get_settings
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


# --- Create: happy path / error surfacing -----------------------------------


async def test_create_event_happy_path_redirects_to_edit_page_with_success_flash(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())
    token = await _get_csrf(client, "/events/new")

    response = await client.post(
        "/events/new",
        data={
            "csrf_token": token,
            "name": "Christmas Passion",
            "slug": "christmas-passion",
            "description": "An event",
            "status": "draft",
        },
    )

    assert response.status_code == 303
    location = response.headers["location"]
    assert "/edit?flash=Event%20created.&flash_kind=success" in location
    # Confirms the new event's real id is in the redirect target, not a
    # hardcoded/incorrect path.
    edit_page = await client.get(location)
    assert edit_page.status_code == 200
    assert "Christmas Passion" in edit_page.text


async def test_create_event_duplicate_slug_surfaces_the_real_api_409_message(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())
    token = await _get_csrf(client, "/events/new")
    first = await client.post(
        "/events/new",
        data={"csrf_token": token, "name": "First", "slug": "dup-slug", "description": "", "status": "draft"},
    )
    assert first.status_code == 303 and "flash_kind=success" in first.headers["location"]

    second = await client.post(
        "/events/new",
        data={"csrf_token": token, "name": "Second", "slug": "dup-slug", "description": "", "status": "draft"},
    )

    assert second.status_code == 303
    location = second.headers["location"]
    assert location.startswith("/events/new?flash=")
    assert "flash_kind=error" in location
    # The real API 409 wording (app.api.routes.events.create_event), not a
    # generic message.
    assert "An%20event%20with%20this%20slug%20already%20exists." in location


# --- Edit: happy path / no-op diff / 404 ------------------------------------


async def test_edit_event_happy_path_persists_and_flashes_success(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event(name="Original Name", slug="original-slug", status=PublishStatus.DRAFT)
    token = await _get_csrf(client, f"/events/{event.id}/edit")

    response = await client.post(
        f"/events/{event.id}/edit",
        data={
            "csrf_token": token,
            "name": "New Name",
            "slug": event.slug,
            "description": "",
            "status": "draft",
            "sales_paused": "true",
        },
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/events/{event.id}/edit?flash=Event%20saved.&flash_kind=success"

    after = await client.get(f"/api/v1/events/{event.id}")
    assert after.status_code == 200
    body = after.json()
    assert body["name"] == "New Name"
    assert body["sales_paused"] is True


async def test_edit_event_with_identical_values_sends_no_patch_and_flashes_no_changes(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
) -> None:
    """The route diffs the submitted form against the event's current state
    and only PATCHes changed fields (see ``app.web.routes.events.
    update_event``'s docstring) — submitting identical values must not issue
    a PATCH at all, and the route must flash "No changes to save.", not
    "Event saved."."""
    await _api_login(client, await make_admin_user())
    event = await make_event(
        name="Same Event", slug="same-event", description="desc", status=PublishStatus.DRAFT, sales_paused=False
    )
    token = await _get_csrf(client, f"/events/{event.id}/edit")

    patch_calls: list[str] = []
    from typing import Any

    from httpx import AsyncClient as _RealAsyncClient

    real_patch = _RealAsyncClient.patch

    async def _tracking_patch(self: _RealAsyncClient, url: str, **kwargs: Any) -> Any:
        patch_calls.append(url)
        return await real_patch(self, url, **kwargs)

    monkeypatch.setattr(_RealAsyncClient, "patch", _tracking_patch)

    response = await client.post(
        f"/events/{event.id}/edit",
        data={
            "csrf_token": token,
            "name": "Same Event",
            "slug": "same-event",
            "description": "desc",
            "status": "draft",
            # sales_paused checkbox omitted entirely, matching the form's
            # unchecked-checkbox submission shape.
        },
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/events/{event.id}/edit?flash=No%20changes%20to%20save.&flash_kind=success"
    assert not any(f"/api/v1/events/{event.id}" in call for call in patch_calls)


async def test_edit_unknown_event_returns_404_via_flash_to_events_list(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())
    unknown_id = str(uuid.uuid4())
    # GET on the edit form itself 404s directly (see edit_event_form).
    get_response = await client.get(f"/events/{unknown_id}/edit")
    assert get_response.status_code == 404

    token = await _get_csrf(client, "/events")
    post_response = await client.post(
        f"/events/{unknown_id}/edit",
        data={
            "csrf_token": token,
            "name": "X",
            "slug": "x-slug",
            "description": "",
            "status": "draft",
        },
    )
    assert post_response.status_code == 303
    location = post_response.headers["location"]
    assert location.startswith("/events?flash=")
    assert "flash_kind=error" in location
    assert "Event%20not%20found." in location


# --- Delete: happy path / 409-with-real-orders / 404 ------------------------


async def test_delete_event_with_no_orders_succeeds(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event(name="Deletable Event")
    token = await _get_csrf(client, f"/events/{event.id}/edit")

    response = await client.post(f"/events/{event.id}/delete", data={"csrf_token": token})

    assert response.status_code == 303
    assert response.headers["location"] == "/events?flash=Event%20deleted.&flash_kind=success"
    assert (await client.get(f"/api/v1/events/{event.id}")).status_code == 404


async def _create_event_with_a_real_sold_ticket(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> Event:
    """A real, published Event/Show/TicketType with one real door-paid
    order's Ticket attached under it — enough to trip the DB's
    ``ON DELETE RESTRICT`` on ``Ticket.ticket_type_id`` and force the
    events-delete route's 409 path, mirroring ``test_web_orders_routes.
    py``'s ``_create_pending_door_order`` helper (door checkout needs no
    Mollie webhook round trip to produce a real Ticket row)."""
    event = await make_event(status=PublishStatus.PUBLISHED, name="Event With Sold Tickets")
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

    return event


async def test_delete_event_with_real_sold_tickets_surfaces_409_as_flash_not_crash(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    await _api_login(client, await make_admin_user())
    event = await _create_event_with_a_real_sold_ticket(client, make_event, make_show, make_ticket_type, make_event_config)
    token = await _get_csrf(client, f"/events/{event.id}/edit")

    response = await client.post(f"/events/{event.id}/delete", data={"csrf_token": token})

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(f"/events/{event.id}/edit?flash=")
    assert "flash_kind=error" in location
    assert "Cannot%20delete%3A%20this%20event%20has%20ticket%20types%20with%20existing%20orders." in location
    # The event must still exist — the delete was genuinely blocked, not
    # partially applied.
    assert (await client.get(f"/api/v1/events/{event.id}")).status_code == 200


async def test_delete_unknown_event_returns_404_flash(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())
    token = await _get_csrf(client, "/events")

    response = await client.post(f"/events/{uuid.uuid4()}/delete", data={"csrf_token": token})

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/events?flash=")
    assert "flash_kind=error" in location
    assert "Event%20not%20found." in location


# --- CSRF --------------------------------------------------------------------


async def test_create_event_missing_csrf_is_rejected(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())

    response = await client.post(
        "/events/new", data={"name": "X", "slug": "x-csrf-missing", "description": "", "status": "draft"}
    )

    assert response.status_code == 422


async def test_create_event_wrong_csrf_is_rejected_403(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())
    await _get_csrf(client, "/events/new")

    response = await client.post(
        "/events/new",
        data={"csrf_token": "wrong-token", "name": "X", "slug": "x-csrf-wrong", "description": "", "status": "draft"},
    )

    assert response.status_code == 403


async def test_update_event_missing_csrf_is_rejected(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()

    response = await client.post(
        f"/events/{event.id}/edit",
        data={"name": event.name, "slug": event.slug, "description": "", "status": "draft"},
    )

    assert response.status_code == 422


async def test_delete_event_missing_csrf_is_rejected(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()

    response = await client.post(f"/events/{event.id}/delete", data={})

    assert response.status_code == 422


async def test_delete_event_wrong_csrf_is_rejected_403(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event()
    await _get_csrf(client, f"/events/{event.id}/edit")

    response = await client.post(f"/events/{event.id}/delete", data={"csrf_token": "wrong-token"})

    assert response.status_code == 403


# --- require_web_admin scoping ------------------------------------------------


async def test_events_new_form_unauthenticated_redirects_to_login(client: AsyncClient) -> None:
    response = await client.get("/events/new")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


async def test_events_new_form_unreachable_by_scanner_role(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    seeded = await make_admin_user(role=AdminRole.SCANNER)
    await _api_login(client, seeded)

    response = await client.get("/events/new")

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


async def test_create_event_unauthenticated_redirects_to_login(client: AsyncClient) -> None:
    response = await client.post(
        "/events/new", data={"csrf_token": "irrelevant", "name": "X", "slug": "unauth-x", "description": "", "status": "draft"}
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


async def test_delete_event_unreachable_by_scanner_role(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    event = await make_event()
    seeded = await make_admin_user(role=AdminRole.SCANNER)
    await _api_login(client, seeded)

    response = await client.post(f"/events/{event.id}/delete", data={"csrf_token": "irrelevant"})

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


# --- Template: preview-link copy affordance ----------------------------------


async def test_edit_page_renders_the_correct_preview_link_url(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event(name="Preview Link Event")

    response = await client.get(f"/events/{event.id}/edit")

    assert response.status_code == 200
    event_response = await client.get(f"/api/v1/events/{event.id}")
    preview_token = event_response.json()["preview_token"]
    expected_url = f"{get_settings().public_base_url}/preview/{preview_token}"
    assert expected_url in response.text
    assert f'value="{expected_url}"' in response.text


async def test_events_list_renders_the_correct_preview_link_data_attribute(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], make_event: Callable[..., Awaitable[Event]]
) -> None:
    await _api_login(client, await make_admin_user())
    event = await make_event(name="List Preview Event")

    response = await client.get("/events")

    assert response.status_code == 200
    event_response = await client.get(f"/api/v1/events/{event.id}")
    preview_token = event_response.json()["preview_token"]
    expected_url = f"{get_settings().public_base_url}/preview/{preview_token}"
    assert f'data-copy-value="{expected_url}"' in response.text
