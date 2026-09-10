"""Integration tests for the ``demo`` payment provider's HTML surface
(``app.web.routes.demo_payment``): the interstitial page a ``demo``
checkout redirects the buyer to, and its two form-submission actions.

Mirrors ``test_public_site_web_routes.py``'s checkout-form-submission
pattern: real HTML-form POST to ``/e/{slug}/checkout``, following the
redirect chain a real browser would, asserting against rendered page text
rather than the JSON API directly (that's covered by
``test_demo_payment.py``).
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient

from app.core.config import get_settings
from app.models.enums import PaymentMethod, PublishStatus
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.show import Show
from app.models.ticket_type import TicketType

_PAST = datetime.now(UTC) - timedelta(days=1)
_settings = get_settings()


def _checkout_form(
    *,
    show_id: str,
    ticket_type_id: str,
    quantity: int = 1,
    payment_method: str = "demo",
    buyer_name: str = "Demo Buyer",
    buyer_email: str = "demo-buyer@example.test",
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


async def _start_demo_checkout(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> str:
    """Submit the real HTML checkout form with payment_method=demo and
    return the demo-payment page path the buyer is redirected to."""
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=10)
    await make_event_config(event_id=event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DEMO])

    response = await client.post(
        f"/e/{event.slug}/checkout",
        data=_checkout_form(show_id=str(show.id), ticket_type_id=str(ticket_type.id)),
    )
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    # `_initiate_demo_payment` builds an absolute URL (see
    # app.services.checkout module docstring) the same way Mollie's own
    # checkout_url is absolute — strip the origin so this test can drive
    # the in-process ASGI test client with a plain path.
    assert "/demo-payment/" in location
    return location[location.index("/demo-payment/") :]


async def test_demo_checkout_redirects_to_the_demo_payment_page(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    demo_page_url = await _start_demo_checkout(
        client, make_event, make_show, make_ticket_type, make_event_config
    )

    response = await client.get(demo_page_url)

    assert response.status_code == 200
    assert "Demo Buyer" in response.text
    assert "Simulate successful payment" in response.text
    assert "Simulate failed payment" in response.text


async def test_demo_payment_page_404s_for_unknown_order(client: AsyncClient) -> None:
    response = await client.get(f"/demo-payment/{uuid.uuid4()}")
    assert response.status_code == 404
    assert "Demo payment unavailable" in response.text


async def test_completing_demo_payment_redirects_to_order_confirmation(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    demo_page_url = await _start_demo_checkout(
        client, make_event, make_show, make_ticket_type, make_event_config
    )
    order_id = demo_page_url.rsplit("/", 1)[-1]

    response = await client.post(f"/demo-payment/{order_id}/complete")

    assert response.status_code == 303
    assert response.headers["location"] == f"/order-confirmation/{order_id}"

    confirmation = await client.get(response.headers["location"])
    assert confirmation.status_code == 200
    assert "Demo Buyer" in confirmation.text


async def test_failing_demo_payment_redirects_to_order_confirmation(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    demo_page_url = await _start_demo_checkout(
        client, make_event, make_show, make_ticket_type, make_event_config
    )
    order_id = demo_page_url.rsplit("/", 1)[-1]

    response = await client.post(f"/demo-payment/{order_id}/fail")

    assert response.status_code == 303
    assert response.headers["location"] == f"/order-confirmation/{order_id}"


async def test_demo_payment_action_404s_once_already_settled(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    demo_page_url = await _start_demo_checkout(
        client, make_event, make_show, make_ticket_type, make_event_config
    )
    order_id = demo_page_url.rsplit("/", 1)[-1]

    first = await client.post(f"/demo-payment/{order_id}/complete")
    assert first.status_code == 303

    second = await client.post(f"/demo-payment/{order_id}/complete")
    assert second.status_code == 404


async def test_completing_demo_payment_while_rate_limited_shows_a_retry_notice_not_unavailable(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    """Found by manual smoke-testing: the demo-payment complete/fail
    actions deliberately share ``checkout_rate_limiter`` with the checkout
    endpoint itself (see ``app.web.routes.demo_payment`` module
    docstring), so a 429 from the proxied JSON call is a real, expected
    outcome — not a sign the order is gone. The web route must NOT
    collapse that into the "demo payment unavailable" 404 page (which
    would wrongly tell a merely-rate-limited buyer their order was lost);
    it must show the interstitial again with a retry notice, and the
    order must still be completable once the limiter window clears."""
    demo_page_url = await _start_demo_checkout(
        client, make_event, make_show, make_ticket_type, make_event_config
    )
    order_id = demo_page_url.rsplit("/", 1)[-1]

    # The checkout above already consumed one slot in this IP's shared
    # bucket; burn the rest with filler checkouts against a second event
    # (same client/IP, so the same bucket) so the very next call is
    # guaranteed to be the one that trips the limiter.
    filler_event = await make_event(status=PublishStatus.PUBLISHED)
    filler_show = await make_show(event_id=filler_event.id, status=PublishStatus.PUBLISHED)
    filler_ticket_type = await make_ticket_type(show_id=filler_show.id, quantity_available=100)
    await make_event_config(
        event_id=filler_event.id, sales_live_at=_PAST, enabled_payment_methods=[PaymentMethod.DOOR]
    )
    remaining = _settings.checkout_rate_limit_per_minute - 1
    for _ in range(remaining):
        filler = await client.post(
            f"/e/{filler_event.slug}/checkout",
            data=_checkout_form(
                show_id=str(filler_show.id),
                ticket_type_id=str(filler_ticket_type.id),
                payment_method="door",
            ),
        )
        assert filler.status_code == 303, filler.text

    response = await client.post(f"/demo-payment/{order_id}/complete")

    assert response.status_code == 502, response.text
    assert "Demo payment unavailable" not in response.text
    assert "Demo Buyer" in response.text
    assert "try again" in response.text.lower()
