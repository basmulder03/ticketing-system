"""Integration tests for ``/api/v1/events/{event_id}/shows`` CRUD,
authenticated as a real admin, against a real DB.

Non-admin/non-agent access is covered in ``test_deps_content_scoping.py``.
"""

from collections.abc import Awaitable, Callable

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


async def _create_event(client: AsyncClient, slug: str = "test-event") -> str:
    response = await client.post("/api/v1/events", json={"name": "Test Event", "slug": slug})
    assert response.status_code == 201
    return str(response.json()["id"])


async def test_create_show_defaults_to_draft(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    event_id = await _create_event(client)

    response = await client.post(f"/api/v1/events/{event_id}/shows", json=_SHOW_BODY)

    assert response.status_code == 201
    body = response.json()
    assert body["event_id"] == event_id
    assert body["status"] == "draft"
    assert body["venue_name"] == "Het Kruispunt"
    assert body["capacity"] == 200


async def test_create_show_under_unknown_event_returns_404(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())

    response = await client.post(
        "/api/v1/events/00000000-0000-0000-0000-000000000000/shows", json=_SHOW_BODY
    )

    assert response.status_code == 404


async def test_create_show_rejects_zero_or_negative_capacity(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    event_id = await _create_event(client)

    for bad_capacity in (0, -5):
        response = await client.post(
            f"/api/v1/events/{event_id}/shows", json={**_SHOW_BODY, "capacity": bad_capacity}
        )
        assert response.status_code == 422


async def test_list_shows_ordered_by_date(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    event_id = await _create_event(client)
    await client.post(f"/api/v1/events/{event_id}/shows", json={**_SHOW_BODY, "date": "2026-12-20"})
    await client.post(f"/api/v1/events/{event_id}/shows", json={**_SHOW_BODY, "date": "2026-12-18"})

    response = await client.get(f"/api/v1/events/{event_id}/shows")

    assert response.status_code == 200
    dates = [show["date"] for show in response.json()]
    assert dates == ["2026-12-18", "2026-12-20"]


async def test_show_is_scoped_to_its_parent_event(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    """A Show created under Event A is not reachable via Event B's path,
    even if its raw id is guessed correctly."""
    await _login(client, await make_admin_user())
    event_a = await _create_event(client, slug="event-a")
    event_b = await _create_event(client, slug="event-b")
    created = await client.post(f"/api/v1/events/{event_a}/shows", json=_SHOW_BODY)
    show_id = created.json()["id"]

    cross_event = await client.get(f"/api/v1/events/{event_b}/shows/{show_id}")
    same_event = await client.get(f"/api/v1/events/{event_a}/shows/{show_id}")

    assert cross_event.status_code == 404
    assert same_event.status_code == 200


async def test_update_show_status_transitions_draft_to_published(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    event_id = await _create_event(client)
    created = await client.post(f"/api/v1/events/{event_id}/shows", json=_SHOW_BODY)
    show_id = created.json()["id"]

    response = await client.patch(f"/api/v1/events/{event_id}/shows/{show_id}", json={"status": "published"})

    assert response.status_code == 200
    assert response.json()["status"] == "published"


async def test_partial_update_show_leaves_omitted_fields_unchanged(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    event_id = await _create_event(client)
    created = await client.post(f"/api/v1/events/{event_id}/shows", json=_SHOW_BODY)
    show_id = created.json()["id"]

    response = await client.patch(
        f"/api/v1/events/{event_id}/shows/{show_id}", json={"capacity": 300}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["capacity"] == 300
    assert body["venue_name"] == "Het Kruispunt"


async def test_duplicate_show_copies_fields_and_ticket_types_and_always_starts_draft(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    event_id = await _create_event(client)
    created = await client.post(
        f"/api/v1/events/{event_id}/shows", json={**_SHOW_BODY, "status": "published"}
    )
    source_show_id = created.json()["id"]
    await client.post(
        f"/api/v1/shows/{source_show_id}/ticket-types",
        json={"name": "Adult", "price": "15.00", "service_fee_included": True, "quantity_available": 100},
    )
    await client.post(
        f"/api/v1/shows/{source_show_id}/ticket-types",
        json={"name": "Child", "price": "5.00", "service_fee_included": False, "quantity_available": 50},
    )

    response = await client.post(f"/api/v1/events/{event_id}/shows/{source_show_id}/duplicate")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["id"] != source_show_id
    assert body["event_id"] == event_id
    # Source was published — the duplicate must NOT inherit that; publishing
    # a duplicate is always a separate, explicit action.
    assert body["status"] == "draft"
    assert body["date"] == _SHOW_BODY["date"]
    assert body["venue_name"] == _SHOW_BODY["venue_name"]
    assert body["capacity"] == _SHOW_BODY["capacity"]

    ticket_types = (await client.get(f"/api/v1/shows/{body['id']}/ticket-types")).json()
    names_and_prices = {(tt["name"], tt["price"]) for tt in ticket_types}
    assert names_and_prices == {("Adult", "15.00"), ("Child", "5.00")}
    for tt in ticket_types:
        assert tt["remaining"] == tt["quantity_available"], "a duplicate carries no sales, so nothing is sold yet"

    # The source Show/TicketTypes are untouched by duplicating them.
    source_ticket_types = (await client.get(f"/api/v1/shows/{source_show_id}/ticket-types")).json()
    assert len(source_ticket_types) == 2


async def test_duplicate_show_under_unknown_event_returns_404(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    event_id = await _create_event(client)
    created = await client.post(f"/api/v1/events/{event_id}/shows", json=_SHOW_BODY)
    show_id = created.json()["id"]

    response = await client.post(
        f"/api/v1/events/00000000-0000-0000-0000-000000000000/shows/{show_id}/duplicate"
    )

    assert response.status_code == 404


async def test_duplicate_unknown_show_returns_404(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    event_id = await _create_event(client)

    response = await client.post(
        f"/api/v1/events/{event_id}/shows/00000000-0000-0000-0000-000000000000/duplicate"
    )

    assert response.status_code == 404


async def test_duplicate_show_is_scoped_to_its_parent_event(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    """A Show under Event A cannot be duplicated via Event B's path, even
    with its raw id guessed correctly — same scoping every other
    Show route already enforces."""
    await _login(client, await make_admin_user())
    event_a = await _create_event(client, slug="event-a")
    event_b = await _create_event(client, slug="event-b")
    created = await client.post(f"/api/v1/events/{event_a}/shows", json=_SHOW_BODY)
    show_id = created.json()["id"]

    cross_event = await client.post(f"/api/v1/events/{event_b}/shows/{show_id}/duplicate")

    assert cross_event.status_code == 404


async def test_delete_show_returns_204_and_it_is_gone(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _login(client, await make_admin_user())
    event_id = await _create_event(client)
    created = await client.post(f"/api/v1/events/{event_id}/shows", json=_SHOW_BODY)
    show_id = created.json()["id"]

    response = await client.delete(f"/api/v1/events/{event_id}/shows/{show_id}")
    assert response.status_code == 204

    follow_up = await client.get(f"/api/v1/events/{event_id}/shows/{show_id}")
    assert follow_up.status_code == 404
