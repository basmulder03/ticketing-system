"""Integration tests for locale-aware rendering on the real public landing
page (``app.web.routes.public_site``), covering the ``?lang=`` override and
locale-aware date/time/currency formatting added in the Dutch-translations
commit (see ``app/i18n/formatting.py``, ``app/core/public_templating.py``).

Exercised over real HTTP via the ASGI test client against a real Postgres
DB, per PROJECT_BRIEF.md's Testing section — mirrors the convention in
``test_public_site_web_routes.py``.
"""

import datetime as dt
from collections.abc import Awaitable, Callable
from decimal import Decimal

from httpx import AsyncClient

from app.models.enums import PaymentMethod, PublishStatus
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.show import Show
from app.models.ticket_type import TicketType

_PAST = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
_SHOW_DATE = dt.date(2026, 12, 18)
_DOORS_TIME = dt.time(19, 30)
_START_TIME = dt.time(20, 0)


async def _make_published_show_with_known_datetime(
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> Event:
    event = await make_event(status=PublishStatus.PUBLISHED, name="Christmas Passion")
    show = await make_show(
        event_id=event.id,
        status=PublishStatus.PUBLISHED,
        date=_SHOW_DATE,
        doors_time=_DOORS_TIME,
        start_time=_START_TIME,
    )
    await make_ticket_type(show_id=show.id, name="Adult", price=Decimal("15.00"), quantity_available=10)
    await make_event_config(event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR])
    return event


async def test_landing_page_lang_nl_shows_dutch_formatted_date_time_and_price(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await _make_published_show_with_known_datetime(
        make_event, make_show, make_ticket_type, make_event_config
    )

    response = await client.get(f"/e/{event.slug}?lang=nl")

    assert response.status_code == 200
    body = response.text
    assert '<html lang="nl">' in body
    # Show-picker date label, per format_date's nl convention.
    assert "18 december 2026" in body
    # Doors/start times, per format_time's nl (24-hour) convention.
    assert "19:30" in body
    assert "20:00" in body
    # Ticket price, per format_currency's nl convention (comma decimal, space after symbol).
    assert "€ 15,00" in body
    # None of the English-formatted equivalents should also be present.
    assert "December 18, 2026" not in body
    assert "7:30 PM" not in body
    assert "8:00 PM" not in body
    assert "€15.00" not in body


async def test_landing_page_lang_en_shows_english_formatted_date_time_and_price(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await _make_published_show_with_known_datetime(
        make_event, make_show, make_ticket_type, make_event_config
    )

    response = await client.get(f"/e/{event.slug}?lang=en")

    assert response.status_code == 200
    body = response.text
    assert '<html lang="en">' in body
    assert "December 18, 2026" in body
    assert "7:30 PM" in body
    assert "8:00 PM" in body
    assert "€15.00" in body
    assert "18 december 2026" not in body
    assert "€ 15,00" not in body


async def test_landing_page_default_locale_with_no_lang_param_is_english(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """No ``?lang=`` param and no locale cookie/Accept-Language header on
    the bare test client falls through to ``app.i18n.DEFAULT_LOCALE``
    ("en"), per ``resolve_locale``'s documented precedence."""
    event = await _make_published_show_with_known_datetime(
        make_event, make_show, make_ticket_type, make_event_config
    )

    response = await client.get(f"/e/{event.slug}")

    assert response.status_code == 200
    assert '<html lang="en">' in response.text
    assert "December 18, 2026" in response.text


async def test_language_switcher_links_preserve_other_query_params(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """``with_query_param`` (``app/core/public_templating.py``) rebuilds the
    switcher's href from the current request's full query string, only
    overriding ``lang`` — verified here against a page loaded with an extra
    ``ticket_type`` deep-link param (per PROJECT_BRIEF.md's Sharing
    section), which must survive into both switcher links."""
    event = await _make_published_show_with_known_datetime(
        make_event, make_show, make_ticket_type, make_event_config
    )
    show_response = await client.get(f"/e/{event.slug}?lang=nl")
    ticket_type_id = show_response.text.split('name="qty_', 1)[1].split('"', 1)[0]

    response = await client.get(f"/e/{event.slug}?lang=nl&ticket_type={ticket_type_id}")
    body = response.text

    assert f"lang=en&amp;ticket_type={ticket_type_id}" in body or f"ticket_type={ticket_type_id}&amp;lang=en" in body
    assert f"lang=nl&amp;ticket_type={ticket_type_id}" in body or f"ticket_type={ticket_type_id}&amp;lang=nl" in body
