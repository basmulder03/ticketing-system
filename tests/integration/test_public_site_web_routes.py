"""Integration tests for the public-site HTML surface
(``app.web.routes.public_site``): the themed landing page (published-slug
and preview-token variants), the real HTML-form checkout submission (as
opposed to the JSON API tested directly in ``test_checkout_validation.py``),
and the order-confirmation cookie hand-off (``app.web.order_confirmation``).

Exercised over real HTTP via the ASGI test client against a real Postgres
DB, per PROJECT_BRIEF.md's Testing section.
"""

import json
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from httpx import AsyncClient

from app.models.enums import PaymentMethod, PublishStatus, ThemeFont
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.show import Show
from app.models.theme import Theme
from app.models.ticket_type import TicketType
from app.web.order_confirmation import ORDER_CONFIRMATION_COOKIE

_PAST = datetime.now(UTC) - timedelta(days=1)
_FUTURE = datetime.now(UTC) + timedelta(days=1)


def _checkout_form(
    *,
    show_id: str,
    ticket_type_id: str,
    quantity: int = 1,
    payment_method: str = "door",
    buyer_name: str = "Buyer Name",
    buyer_email: str = "buyer@example.test",
    buyer_address: str = "1 Test Street",
) -> dict[str, str]:
    return {
        "show_choice": show_id,
        f"qty_{ticket_type_id}": str(quantity),
        "buyer_name": buyer_name,
        "buyer_email": buyer_email,
        "buyer_address": buyer_address,
        "payment_method": payment_method,
    }


# --- Landing page: published slug vs. draft/preview -----------------------


async def test_published_landing_page_renders_200_with_theme_applied(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
    make_theme: Callable[..., Awaitable[Theme]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED, name="Christmas Passion")
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    await make_ticket_type(show_id=show.id)
    await make_event_config(event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR])
    await make_theme(
        event_id=event.id,
        primary_color="#123abc",
        secondary_color="#fefefe",
        accent_color="#ff00ff",
        font_choice=ThemeFont.LORA,
        custom_css=".event-content h1 { text-transform: uppercase; }",
    )

    response = await client.get(f"/e/{event.slug}")

    assert response.status_code == 200
    body = response.text
    assert "Christmas Passion" in body
    # Fixed theme fields spot-checked in the rendered <style> block.
    assert "#123abc" in body
    assert "#fefefe" in body
    assert "#ff00ff" in body
    # Sanitized custom CSS appended verbatim.
    assert "text-transform: uppercase" in body


async def test_draft_event_slug_404s(
    client: AsyncClient, make_event: Callable[..., Awaitable[Event]]
) -> None:
    event = await make_event(status=PublishStatus.DRAFT)
    response = await client.get(f"/e/{event.slug}")
    assert response.status_code == 404


async def test_unknown_slug_404s(client: AsyncClient) -> None:
    response = await client.get("/e/no-such-event")
    assert response.status_code == 404


async def test_preview_page_200s_for_draft_event_with_noindex_meta(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
) -> None:
    event = await make_event(status=PublishStatus.DRAFT)
    await make_show(event_id=event.id, status=PublishStatus.DRAFT)

    response = await client.get(f"/preview/{event.preview_token}")

    assert response.status_code == 200
    assert '<meta name="robots" content="noindex, nofollow">' in response.text


async def test_preview_page_404s_for_wrong_token(
    client: AsyncClient, make_event: Callable[..., Awaitable[Event]]
) -> None:
    await make_event(status=PublishStatus.DRAFT)
    response = await client.get("/preview/totally-made-up-token")
    assert response.status_code == 404


# --- JSON-LD and Open Graph ------------------------------------------------


async def test_landing_page_json_ld_is_present_and_well_formed(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED, name="Christmas Passion")
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED, venue_name="Het Kruispunt")
    await make_ticket_type(show_id=show.id, name="Adult", price=Decimal("15.00"), quantity_available=10)
    await make_event_config(event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR])

    response = await client.get(f"/e/{event.slug}")
    body = response.text

    start = body.index('<script type="application/ld+json">') + len('<script type="application/ld+json">')
    end = body.index("</script>", start)
    payload = json.loads(body[start:end])

    assert isinstance(payload, list) and len(payload) == 1
    entry = payload[0]
    assert entry["@type"] == "Event"
    assert entry["name"] == "Christmas Passion"
    assert "startDate" in entry
    assert entry["location"]["name"] == "Het Kruispunt"
    assert len(entry["offers"]) == 1
    assert entry["offers"][0]["availability"] == "https://schema.org/InStock"


async def test_landing_page_includes_open_graph_tags(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await make_event(
        status=PublishStatus.PUBLISHED, name="Christmas Passion", description="A festive concert."
    )
    await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    await make_event_config(event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR])

    response = await client.get(f"/e/{event.slug}")
    body = response.text

    assert '<meta property="og:title" content="Christmas Passion">' in body
    assert '<meta property="og:description" content="A festive concert.">' in body
    assert '<meta property="og:url" content="' in body
    assert f"/e/{event.slug}" in body
    assert 'name="twitter:card"' in body


# --- Sold-out ticket types render disabled, not just qty=0 ----------------


async def test_sold_out_ticket_type_has_no_quantity_input(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, name="Adult", quantity_available=1)
    await make_event_config(event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR])

    # Buy the last remaining ticket via the real checkout flow.
    checkout = await client.post(
        f"/e/{event.slug}/checkout",
        data=_checkout_form(show_id=str(show.id), ticket_type_id=str(ticket_type.id)),
    )
    assert checkout.status_code == 303

    response = await client.get(f"/e/{event.slug}")
    body = response.text
    assert f'name="qty_{ticket_type.id}"' not in body
    assert "Sold out" in body


async def test_available_ticket_type_has_a_quantity_input(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=5)
    await make_event_config(event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR])

    response = await client.get(f"/e/{event.slug}")
    assert f'name="qty_{ticket_type.id}"' in response.text


# --- Checkout form submission: happy path ---------------------------------


async def test_checkout_form_happy_path_creates_order_and_redirects(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, price=Decimal("15.00"), quantity_available=10)
    await make_event_config(event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR])

    response = await client.post(
        f"/e/{event.slug}/checkout",
        data=_checkout_form(show_id=str(show.id), ticket_type_id=str(ticket_type.id), quantity=2),
    )

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/order-confirmation/")

    confirmation = await client.get(location)
    assert confirmation.status_code == 200
    assert "Buyer Name" in confirmation.text
    assert "buyer@example.test" in confirmation.text


async def test_checkout_form_all_zero_quantities_is_rejected_with_no_items_error(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=10)
    await make_event_config(event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR])

    response = await client.post(
        f"/e/{event.slug}/checkout",
        data=_checkout_form(show_id=str(show.id), ticket_type_id=str(ticket_type.id), quantity=0),
    )

    assert response.status_code == 422
    assert "Please select at least one ticket before continuing." in response.text


# --- Checkout form submission: distinct error mapping per CheckoutError ---


async def test_checkout_form_sales_paused_shows_paused_message(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED, sales_paused=True)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=10)
    await make_event_config(event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR])

    response = await client.post(
        f"/e/{event.slug}/checkout",
        data=_checkout_form(show_id=str(show.id), ticket_type_id=str(ticket_type.id)),
    )

    assert response.status_code == 403
    assert "Sales are currently paused for this event. Please check back later." in response.text
    # Not confused with any of the other three mapped rejection messages.
    assert "not on sale yet" not in response.text
    assert "not enough tickets remain" not in response.text


async def test_checkout_form_sales_not_live_shows_not_live_message(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=10)
    await make_event_config(event_id=event.id, sales_live_at=_FUTURE, enabled_payment_methods=[PaymentMethod.DOOR])

    response = await client.post(
        f"/e/{event.slug}/checkout",
        data=_checkout_form(show_id=str(show.id), ticket_type_id=str(ticket_type.id)),
    )

    assert response.status_code == 403
    assert "Tickets are not on sale yet for this event." in response.text
    assert "currently paused" not in response.text


async def test_checkout_form_sold_out_shows_sold_out_message(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=1)
    await make_event_config(event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR])

    response = await client.post(
        f"/e/{event.slug}/checkout",
        data=_checkout_form(show_id=str(show.id), ticket_type_id=str(ticket_type.id), quantity=5),
    )

    assert response.status_code == 409
    assert "Sorry, not enough tickets remain for one of the ticket types you selected." in response.text


async def test_checkout_form_payment_method_not_enabled_gets_its_own_message(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """Fixed gap: PaymentMethodNotEnabledCheckoutError (422) now gets its
    own distinct message rather than falling through to the generic one —
    and must never show one of the other three specific messages either
    (paused/not-live/sold-out), which would actively mislead the buyer
    about why their order was rejected."""
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=10)
    await make_event_config(event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR])

    response = await client.post(
        f"/e/{event.slug}/checkout",
        data=_checkout_form(
            show_id=str(show.id), ticket_type_id=str(ticket_type.id), payment_method="mollie"
        ),
    )

    assert response.status_code == 422
    assert "isn&#39;t available for this event" in response.text
    assert "process your order" not in response.text
    assert "currently paused" not in response.text
    assert "not on sale yet" not in response.text
    assert "not enough tickets remain" not in response.text


# --- Order confirmation cookie hand-off ------------------------------------


async def test_order_confirmation_cookie_is_httponly_and_scoped_to_its_path(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=10)
    await make_event_config(event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR])

    response = await client.post(
        f"/e/{event.slug}/checkout",
        data=_checkout_form(show_id=str(show.id), ticket_type_id=str(ticket_type.id)),
    )
    assert response.status_code == 303

    set_cookie_headers = response.headers.get_list("set-cookie")
    order_cookie_header = next(h for h in set_cookie_headers if h.startswith(f"{ORDER_CONFIRMATION_COOKIE}="))
    assert "HttpOnly" in order_cookie_header
    assert "Path=/order-confirmation" in order_cookie_header
    assert "SameSite=lax" in order_cookie_header or "samesite=lax" in order_cookie_header.lower()


async def test_order_confirmation_without_cookie_does_not_leak_order_data(
    client: AsyncClient,
    client_factory: Callable[[str | None], AsyncClient],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=10)
    await make_event_config(event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR])

    checkout = await client.post(
        f"/e/{event.slug}/checkout",
        data=_checkout_form(show_id=str(show.id), ticket_type_id=str(ticket_type.id)),
    )
    order_id = checkout.headers["location"].removeprefix("/order-confirmation/")

    # A different "visitor" (no cookie jar carried over) hits the same URL.
    async with client_factory("someone-else") as other_client:
        response = await other_client.get(f"/order-confirmation/{order_id}")

    assert response.status_code == 404
    assert "Buyer Name" not in response.text
    assert "buyer@example.test" not in response.text
    assert "Order confirmation unavailable" in response.text


async def test_order_confirmation_with_mismatched_order_id_in_cookie_does_not_leak_data(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=10)
    await make_event_config(event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR])

    # Order A: sets the real cookie for order A on `client`.
    checkout_a = await client.post(
        f"/e/{event.slug}/checkout",
        data=_checkout_form(
            show_id=str(show.id), ticket_type_id=str(ticket_type.id), buyer_name="Buyer A", buyer_email="a@example.test"
        ),
    )
    assert checkout_a.status_code == 303

    # Order B: a second, distinct order under the same event.
    checkout_b = await client.post(
        f"/e/{event.slug}/checkout",
        data=_checkout_form(
            show_id=str(show.id), ticket_type_id=str(ticket_type.id), buyer_name="Buyer B", buyer_email="b@example.test"
        ),
    )
    assert checkout_b.status_code == 303
    order_b_id = checkout_b.headers["location"].removeprefix("/order-confirmation/")

    # `client`'s cookie jar now holds order B's cookie (the most recent
    # checkout). Visiting order A's confirmation URL with that mismatched
    # cookie must not leak order B's data under order A's id, nor vice versa.
    order_a_id = checkout_a.headers["location"].removeprefix("/order-confirmation/")
    response = await client.get(f"/order-confirmation/{order_a_id}")

    assert response.status_code == 404
    assert "Buyer A" not in response.text
    assert "Buyer B" not in response.text
    assert order_b_id != order_a_id


async def test_order_confirmation_renders_correct_data_from_the_real_cookie(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, name="Adult", price=Decimal("15.00"), quantity_available=10)
    await make_event_config(event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR])

    checkout = await client.post(
        f"/e/{event.slug}/checkout",
        data=_checkout_form(show_id=str(show.id), ticket_type_id=str(ticket_type.id), quantity=2),
    )
    response = await client.get(checkout.headers["location"])

    assert response.status_code == 200
    assert "Buyer Name" in response.text
    assert "buyer@example.test" in response.text
    assert "1 Test Street" in response.text
    assert "Adult" in response.text
    assert "30.00" in response.text  # 2 x 15.00


# --- Sitemap/robots: still correct against the real /e/ scheme ------------


async def test_sitemap_lists_public_landing_page_urls_and_is_valid_xml(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED, slug="christmas-passion-2026")
    response = await client.get("/sitemap.xml")
    assert response.status_code == 200
    root = ET.fromstring(response.text)
    assert root.tag.endswith("urlset")
    assert f"/e/{event.slug}" in response.text
