"""Integration tests for ``/api/v1/shows/{show_id}/ticket-types`` CRUD,
authenticated as a real admin, against a real DB.

Non-admin/non-agent access is covered in ``test_deps_content_scoping.py``.
"""

from collections.abc import Awaitable, Callable
from decimal import Decimal

from httpx import AsyncClient

from tests.integration.conftest import SeededAdmin

_SHOW_BODY = {
    "date": "2026-12-18",
    "doors_time": "19:30:00",
    "start_time": "20:00:00",
    "venue_name": "Het Kruispunt",
    "venue_address": "Kerkstraat 1, Landsmeer",
    "capacity": 200,
}


async def _login(client: AsyncClient, seeded: SeededAdmin) -> None:
    response = await client.post("/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password})
    assert response.status_code == 200


async def _create_show(client: AsyncClient, slug: str = "test-event") -> str:
    event = await client.post("/api/v1/events", json={"name": "Test Event", "slug": slug})
    event_id = event.json()["id"]
    show = await client.post(f"/api/v1/events/{event_id}/shows", json=_SHOW_BODY)
    return str(show.json()["id"])


async def test_create_ticket_type_defaults_service_fee_included_true(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    show_id = await _create_show(client)

    response = await client.post(
        f"/api/v1/shows/{show_id}/ticket-types",
        json={"name": "Adult", "price": "15.00", "quantity_available": 100},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["show_id"] == show_id
    assert body["name"] == "Adult"
    assert Decimal(body["price"]) == Decimal("15.00")
    assert body["service_fee_included"] is True
    assert body["quantity_available"] == 100


async def test_remaining_equals_quantity_available(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    """Per ``TicketType.remaining``'s docstring: no Order/Ticket model exists
    yet (Milestone 3+), so ``remaining`` is always exactly
    ``quantity_available`` for now — this pins that current behavior so a
    regression is caught, and is the one test that MUST be revisited once
    real stock math lands."""
    await _login(client, await make_admin_user())
    show_id = await _create_show(client)

    created = await client.post(
        f"/api/v1/shows/{show_id}/ticket-types",
        json={"name": "Adult", "price": "15.00", "quantity_available": 42},
    )

    assert created.json()["remaining"] == 42 == created.json()["quantity_available"]

    fetched = await client.get(f"/api/v1/shows/{show_id}/ticket-types/{created.json()['id']}")
    assert fetched.json()["remaining"] == 42


async def test_create_ticket_type_service_fee_toggle_can_be_disabled(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    show_id = await _create_show(client)

    response = await client.post(
        f"/api/v1/shows/{show_id}/ticket-types",
        json={"name": "Child", "price": "10.00", "quantity_available": 50, "service_fee_included": False},
    )

    assert response.status_code == 201
    assert response.json()["service_fee_included"] is False


async def test_update_ticket_type_service_fee_toggle(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    show_id = await _create_show(client)
    created = await client.post(
        f"/api/v1/shows/{show_id}/ticket-types",
        json={"name": "Adult", "price": "15.00", "quantity_available": 100},
    )
    ticket_type_id = created.json()["id"]

    response = await client.patch(
        f"/api/v1/shows/{show_id}/ticket-types/{ticket_type_id}", json={"service_fee_included": False}
    )

    assert response.status_code == 200
    assert response.json()["service_fee_included"] is False


async def test_create_ticket_type_rejects_negative_price(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    show_id = await _create_show(client)

    response = await client.post(
        f"/api/v1/shows/{show_id}/ticket-types",
        json={"name": "Adult", "price": "-1.00", "quantity_available": 100},
    )

    assert response.status_code == 422


async def test_create_ticket_type_rejects_negative_quantity(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    show_id = await _create_show(client)

    response = await client.post(
        f"/api/v1/shows/{show_id}/ticket-types",
        json={"name": "Adult", "price": "10.00", "quantity_available": -1},
    )

    assert response.status_code == 422


async def test_create_ticket_type_allows_zero_quantity(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    """Zero is a legitimate "sold out from the start" / placeholder state,
    distinct from a negative (invalid) quantity."""
    await _login(client, await make_admin_user())
    show_id = await _create_show(client)

    response = await client.post(
        f"/api/v1/shows/{show_id}/ticket-types",
        json={"name": "Adult", "price": "10.00", "quantity_available": 0},
    )

    assert response.status_code == 201
    assert response.json()["quantity_available"] == 0
    assert response.json()["remaining"] == 0


async def test_ticket_type_is_scoped_to_its_parent_show(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    show_a = await _create_show(client, slug="event-a")
    show_b = await _create_show(client, slug="event-b")
    created = await client.post(
        f"/api/v1/shows/{show_a}/ticket-types",
        json={"name": "Adult", "price": "15.00", "quantity_available": 100},
    )
    ticket_type_id = created.json()["id"]

    cross_show = await client.get(f"/api/v1/shows/{show_b}/ticket-types/{ticket_type_id}")
    same_show = await client.get(f"/api/v1/shows/{show_a}/ticket-types/{ticket_type_id}")

    assert cross_show.status_code == 404
    assert same_show.status_code == 200


async def test_delete_ticket_type_returns_204_and_it_is_gone(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    show_id = await _create_show(client)
    created = await client.post(
        f"/api/v1/shows/{show_id}/ticket-types",
        json={"name": "Adult", "price": "15.00", "quantity_available": 100},
    )
    ticket_type_id = created.json()["id"]

    response = await client.delete(f"/api/v1/shows/{show_id}/ticket-types/{ticket_type_id}")
    assert response.status_code == 204

    follow_up = await client.get(f"/api/v1/shows/{show_id}/ticket-types/{ticket_type_id}")
    assert follow_up.status_code == 404
