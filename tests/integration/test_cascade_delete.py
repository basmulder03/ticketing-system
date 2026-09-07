"""Deleting an ``Event`` removes everything under it: its ``EventConfig``
(1:1), its ``Show``s, and each Show's ``TicketType``s.

Verified two ways per PROJECT_BRIEF.md's instruction to test what the
implementation actually does rather than assume: through the API (the
``DELETE /api/v1/events/{event_id}`` route, exercising
``session.delete(event)`` + the ORM's ``cascade="all, delete-orphan"`` on
``Event.config``/``Event.shows`` and ``Show.ticket_types`` — see
``app/models/event.py`` and ``app/models/show.py``), and directly against
the DB in ``db_session`` to prove the rows are actually gone, not just
unreachable via the now-404ing parent routes.
"""

from collections.abc import Awaitable, Callable

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.show import Show
from app.models.ticket_type import TicketType
from tests.integration.conftest import SeededAdmin


async def _login(client: AsyncClient, seeded: SeededAdmin) -> None:
    response = await client.post("/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password})
    assert response.status_code == 200


async def test_deleting_an_event_cascades_to_config_shows_and_ticket_types(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    db_session: AsyncSession,
) -> None:
    await _login(client, await make_admin_user())

    event = await client.post("/api/v1/events", json={"name": "Cascade Test", "slug": "cascade-test"})
    event_id = event.json()["id"]

    config = await client.put(f"/api/v1/events/{event_id}/config", json={"smtp_host": "mailpit"})
    assert config.status_code == 200

    show = await client.post(
        f"/api/v1/events/{event_id}/shows",
        json={
            "date": "2026-12-18",
            "doors_time": "19:30:00",
            "start_time": "20:00:00",
            "venue_name": "Venue",
            "venue_address": "1 Street",
            "capacity": 100,
        },
    )
    show_id = show.json()["id"]

    ticket_type = await client.post(
        f"/api/v1/shows/{show_id}/ticket-types",
        json={"name": "Adult", "price": "15.00", "quantity_available": 50},
    )
    ticket_type_id = ticket_type.json()["id"]

    # Sanity: everything exists before the delete.
    assert (await db_session.get(EventConfig, config.json()["id"])) is not None
    assert (await db_session.get(Show, show_id)) is not None
    assert (await db_session.get(TicketType, ticket_type_id)) is not None

    delete_response = await client.delete(f"/api/v1/events/{event_id}")
    assert delete_response.status_code == 204

    # Gone via the API (parent route 404s).
    assert (await client.get(f"/api/v1/events/{event_id}")).status_code == 404

    # Gone at the DB level too — not merely unreachable via a now-404ing
    # parent route, but the rows themselves no longer exist.
    assert (await db_session.get(Event, event_id)) is None
    assert (await db_session.get(EventConfig, config.json()["id"])) is None
    assert (await db_session.get(Show, show_id)) is None
    assert (await db_session.get(TicketType, ticket_type_id)) is None


async def test_deleting_a_show_cascades_to_its_ticket_types_but_not_the_event(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    db_session: AsyncSession,
) -> None:
    await _login(client, await make_admin_user())

    event = await client.post("/api/v1/events", json={"name": "Show Cascade Test", "slug": "show-cascade-test"})
    event_id = event.json()["id"]
    show = await client.post(
        f"/api/v1/events/{event_id}/shows",
        json={
            "date": "2026-12-18",
            "doors_time": "19:30:00",
            "start_time": "20:00:00",
            "venue_name": "Venue",
            "venue_address": "1 Street",
            "capacity": 100,
        },
    )
    show_id = show.json()["id"]
    ticket_type = await client.post(
        f"/api/v1/shows/{show_id}/ticket-types",
        json={"name": "Adult", "price": "15.00", "quantity_available": 50},
    )
    ticket_type_id = ticket_type.json()["id"]

    delete_response = await client.delete(f"/api/v1/events/{event_id}/shows/{show_id}")
    assert delete_response.status_code == 204

    assert (await db_session.get(TicketType, ticket_type_id)) is None
    assert (await db_session.get(Show, show_id)) is None
    # The parent Event is untouched by deleting one of its Shows.
    assert (await db_session.get(Event, event_id)) is not None
    assert (await client.get(f"/api/v1/events/{event_id}")).status_code == 200


async def test_deleting_a_second_shows_ticket_types_does_not_affect_the_first(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    db_session: AsyncSession,
) -> None:
    """Guards against an over-eager cascade wiring that deletes siblings,
    not just descendants."""
    await _login(client, await make_admin_user())

    event = await client.post("/api/v1/events", json={"name": "Sibling Test", "slug": "sibling-test"})
    event_id = event.json()["id"]
    show_a = await client.post(
        f"/api/v1/events/{event_id}/shows",
        json={
            "date": "2026-12-18",
            "doors_time": "19:30:00",
            "start_time": "20:00:00",
            "venue_name": "Venue A",
            "venue_address": "1 Street",
            "capacity": 100,
        },
    )
    show_b = await client.post(
        f"/api/v1/events/{event_id}/shows",
        json={
            "date": "2026-12-19",
            "doors_time": "19:30:00",
            "start_time": "20:00:00",
            "venue_name": "Venue B",
            "venue_address": "2 Street",
            "capacity": 100,
        },
    )
    show_a_id = show_a.json()["id"]
    show_b_id = show_b.json()["id"]
    ticket_type_a = await client.post(
        f"/api/v1/shows/{show_a_id}/ticket-types",
        json={"name": "Adult", "price": "15.00", "quantity_available": 50},
    )
    ticket_type_a_id = ticket_type_a.json()["id"]

    delete_response = await client.delete(f"/api/v1/events/{event_id}/shows/{show_b_id}")
    assert delete_response.status_code == 204

    # Show A and its ticket type are untouched.
    assert (await db_session.get(Show, show_a_id)) is not None
    assert (await db_session.get(TicketType, ticket_type_a_id)) is not None


async def test_deleting_an_event_without_a_config_still_succeeds(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    """An Event's EventConfig is optional (only created via PUT) — deleting
    an Event that never had one configured must not error."""
    await _login(client, await make_admin_user())
    event = await client.post("/api/v1/events", json={"name": "No Config", "slug": "no-config-event"})
    event_id = event.json()["id"]

    response = await client.delete(f"/api/v1/events/{event_id}")

    assert response.status_code == 204
