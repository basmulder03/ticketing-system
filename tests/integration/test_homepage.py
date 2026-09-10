"""Integration tests for ``GET /api/v1/public/homepage``
(``app.api.routes.public.get_public_homepage``) — the post-launch fix, per
the user's NOTES: "There should be a homepage on the route... event can be
set to the default event... or show an overview of the app... which
events are currently able to have shows booked on."

The web-layer route this backs (``GET /`` — redirect-or-render) is tested
separately in ``test_homepage_web_routes.py``. The default-event mechanism
itself (``set-default``/``unset-default``, mutual exclusivity) is tested in
``test_events_routes.py``.
"""

from collections.abc import Awaitable, Callable

from httpx import AsyncClient

from app.models.enums import PublishStatus
from app.models.event import Event
from tests.integration.conftest import SeededAdmin


async def _login(client: AsyncClient, seeded: SeededAdmin) -> None:
    response = await client.post(
        "/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password}
    )
    assert response.status_code == 200


async def test_homepage_lists_only_published_events(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
) -> None:
    await make_event(name="Published Event", slug="homepage-published", status=PublishStatus.PUBLISHED)
    await make_event(name="Draft Event", slug="homepage-draft", status=PublishStatus.DRAFT)

    response = await client.get("/api/v1/public/homepage")

    assert response.status_code == 200
    body = response.json()
    slugs = {e["slug"] for e in body["events"]}
    assert slugs == {"homepage-published"}
    assert body["default_event_slug"] is None


async def test_homepage_returns_empty_list_and_no_default_when_nothing_is_published(
    client: AsyncClient,
) -> None:
    response = await client.get("/api/v1/public/homepage")

    assert response.status_code == 200
    body = response.json()
    assert body["events"] == []
    assert body["default_event_slug"] is None


async def test_homepage_default_event_slug_populated_when_set_and_published(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
) -> None:
    await _login(client, await make_admin_user())
    event = await make_event(name="Default Event", slug="homepage-default", status=PublishStatus.PUBLISHED)
    await client.post(f"/api/v1/events/{event.id}/set-default")

    response = await client.get("/api/v1/public/homepage")

    assert response.status_code == 200
    assert response.json()["default_event_slug"] == "homepage-default"


async def test_homepage_default_event_slug_is_none_while_the_default_event_is_still_draft(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
) -> None:
    """A default Event set in advance, before publishing, has no visible
    effect yet — see Event.is_default_event's docstring."""
    await _login(client, await make_admin_user())
    event = await make_event(name="Draft Default", slug="homepage-draft-default", status=PublishStatus.DRAFT)
    await client.post(f"/api/v1/events/{event.id}/set-default")

    response = await client.get("/api/v1/public/homepage")

    assert response.status_code == 200
    assert response.json()["default_event_slug"] is None
    assert response.json()["events"] == []


async def test_homepage_event_summary_includes_description_but_no_theme_or_shows(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
) -> None:
    await make_event(
        name="Described Event",
        slug="homepage-described",
        description="A festive concert.",
        status=PublishStatus.PUBLISHED,
    )

    response = await client.get("/api/v1/public/homepage")

    assert response.status_code == 200
    event_summary = response.json()["events"][0]
    assert set(event_summary.keys()) == {"name", "slug", "description"}
    assert event_summary["description"] == "A festive concert."
