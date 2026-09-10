"""Integration tests for ``GET /`` (``app.web.routes.homepage.homepage``)
— the post-launch fix replacing the old unconditional ``/`` -> ``/events``
redirect (see that module's docstring). Drives the real HTML page, not
the JSON API directly (covered by ``test_homepage.py``).
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


async def test_homepage_redirects_to_the_default_event_when_set_and_published(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
) -> None:
    await _login(client, await make_admin_user())
    event = await make_event(
        name="Default Event", slug="homepage-web-default", status=PublishStatus.PUBLISHED
    )
    await client.post(f"/api/v1/events/{event.id}/set-default")

    response = await client.get("/")

    assert response.status_code == 303
    assert response.headers["location"] == "/e/homepage-web-default"


async def test_homepage_renders_directory_when_no_default_is_set(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
) -> None:
    await make_event(
        name="Christmas Passion",
        slug="homepage-web-listed",
        description="A festive concert.",
        status=PublishStatus.PUBLISHED,
    )
    await make_event(name="Draft Event", slug="homepage-web-draft", status=PublishStatus.DRAFT)

    response = await client.get("/")

    assert response.status_code == 200
    assert "Christmas Passion" in response.text
    assert "A festive concert." in response.text
    assert 'href="/e/homepage-web-listed"' in response.text
    assert "Draft Event" not in response.text
    assert 'href="/login"' in response.text


async def test_homepage_renders_directory_when_the_default_event_is_still_draft(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_event: Callable[..., Awaitable[Event]],
) -> None:
    await _login(client, await make_admin_user())
    event = await make_event(
        name="Draft Default", slug="homepage-web-draft-default", status=PublishStatus.DRAFT
    )
    await client.post(f"/api/v1/events/{event.id}/set-default")

    response = await client.get("/")

    assert response.status_code == 200
    assert "Draft Default" not in response.text


async def test_homepage_shows_a_no_events_message_when_nothing_is_published(
    client: AsyncClient,
) -> None:
    response = await client.get("/")

    assert response.status_code == 200
    assert "no events" in response.text.lower()
