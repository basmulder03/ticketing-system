"""Integration tests for the Milestone 9 large-display "beamer/TV"
countdown view (``app.web.routes.public_site``):
  - ``GET /e/{slug}/beamer`` (``public_beamer_page``)
  - ``GET /preview/{token}/beamer`` (``preview_beamer_page``)

Reuses the same slug/preview-token access-control code path as the landing
page (``app.web.routes.public_site.public_landing_page``/
``preview_landing_page``), so this file mirrors
``tests/integration/test_public_site_web_routes.py``'s draft/preview
access-control test structure rather than re-deriving it. This file covers
only the server-rendered HTML/access-control/status-code layer — the
client-side countdown tick interval itself is not meaningfully testable via
pytest (see the Milestone 9 brief).
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient

from app.models.enums import PublishStatus
from app.models.event import Event
from app.models.show import Show

_PAST_DATE = (datetime.now(UTC) - timedelta(days=1)).date()
_FUTURE_DATE = (datetime.now(UTC) + timedelta(days=1)).date()


# --- Default Show selection --------------------------------------------------


async def test_published_event_no_show_param_defaults_to_soonest_upcoming_show(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    # A past Show (doors already opened) and a future Show (doors haven't
    # opened yet) both under the same Event — the future one must win.
    await make_show(
        event_id=event.id,
        status=PublishStatus.PUBLISHED,
        date=_PAST_DATE,
        venue_name="Past Venue",
    )
    await make_show(
        event_id=event.id,
        status=PublishStatus.PUBLISHED,
        date=_FUTURE_DATE,
        venue_name="Future Venue",
    )

    response = await client.get(f"/e/{event.slug}/beamer")

    assert response.status_code == 200
    assert "Future Venue" in response.text
    assert "Past Venue" not in response.text


async def test_explicit_show_query_param_renders_that_show_specifically(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    default_show = await make_show(
        event_id=event.id, status=PublishStatus.PUBLISHED, date=_FUTURE_DATE, venue_name="Default Venue"
    )
    other_show = await make_show(
        event_id=event.id,
        status=PublishStatus.PUBLISHED,
        date=_FUTURE_DATE + timedelta(days=1),
        venue_name="Other Venue",
    )
    assert default_show.id != other_show.id

    response = await client.get(f"/e/{event.slug}/beamer", params={"show": str(other_show.id)})

    assert response.status_code == 200
    assert "Other Venue" in response.text
    assert "Default Venue" not in response.text


async def test_invalid_show_query_param_falls_back_to_the_default_show_rather_than_404ing(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    await make_show(event_id=event.id, status=PublishStatus.PUBLISHED, date=_FUTURE_DATE, venue_name="Real Venue")

    response = await client.get(f"/e/{event.slug}/beamer", params={"show": "00000000-0000-0000-0000-000000000000"})

    assert response.status_code == 200
    assert "Real Venue" in response.text


# --- Draft/preview access control -------------------------------------------


async def test_draft_event_beamer_via_slug_404s(
    client: AsyncClient, make_event: Callable[..., Awaitable[Event]]
) -> None:
    event = await make_event(status=PublishStatus.DRAFT)
    response = await client.get(f"/e/{event.slug}/beamer")
    assert response.status_code == 404


async def test_unknown_slug_beamer_404s(client: AsyncClient) -> None:
    response = await client.get("/e/no-such-event/beamer")
    assert response.status_code == 404


async def test_preview_token_beamer_200s_for_a_draft_events_real_token(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
) -> None:
    event = await make_event(status=PublishStatus.DRAFT)
    await make_show(event_id=event.id, status=PublishStatus.DRAFT, date=_FUTURE_DATE, venue_name="Preview Venue")

    response = await client.get(f"/preview/{event.preview_token}/beamer")

    assert response.status_code == 200
    assert "Preview Venue" in response.text


async def test_preview_token_beamer_404s_for_a_wrong_token(
    client: AsyncClient, make_event: Callable[..., Awaitable[Event]]
) -> None:
    await make_event(status=PublishStatus.DRAFT)
    response = await client.get("/preview/totally-made-up-token/beamer")
    assert response.status_code == 404


# --- All Shows' doors already passed: still 200s with the "doors open" state -


async def test_all_shows_doors_passed_still_renders_200_with_doors_open_state(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    await make_show(event_id=event.id, status=PublishStatus.PUBLISHED, date=_PAST_DATE, venue_name="Past Venue")

    response = await client.get(f"/e/{event.slug}/beamer")

    assert response.status_code == 200
    body = response.text
    assert "Past Venue" in body
    # Server-computed initial state for the no-JS render: the countdown
    # block is hidden, and the standing "doors open" paragraph is NOT
    # hidden.
    assert 'id="beamer-countdown" class="beamer-countdown" hidden' in body
    assert 'id="beamer-doors-open" class="beamer-doors-open" >' in body


async def test_upcoming_show_doors_not_passed_shows_the_ticking_countdown_not_the_doors_open_state(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    await make_show(event_id=event.id, status=PublishStatus.PUBLISHED, date=_FUTURE_DATE, venue_name="Future Venue")

    response = await client.get(f"/e/{event.slug}/beamer")

    assert response.status_code == 200
    body = response.text
    assert 'id="beamer-countdown" class="beamer-countdown" \n' in body
    assert 'id="beamer-doors-open" class="beamer-doors-open" hidden>' in body


# --- noindex meta tag: always on, published or preview ----------------------


async def test_published_beamer_page_has_noindex_meta_tag(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    await make_show(event_id=event.id, status=PublishStatus.PUBLISHED, date=_FUTURE_DATE)

    response = await client.get(f"/e/{event.slug}/beamer")

    assert response.status_code == 200
    assert '<meta name="robots" content="noindex, nofollow">' in response.text


async def test_preview_beamer_page_has_noindex_meta_tag(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
) -> None:
    event = await make_event(status=PublishStatus.DRAFT)
    await make_show(event_id=event.id, status=PublishStatus.DRAFT, date=_FUTURE_DATE)

    response = await client.get(f"/preview/{event.preview_token}/beamer")

    assert response.status_code == 200
    assert '<meta name="robots" content="noindex, nofollow">' in response.text
