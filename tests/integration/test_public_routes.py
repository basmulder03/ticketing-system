"""Public, unauthenticated read routes (``app.api.routes.public``):
published event/show/ticket-type data by slug, draft-gating via the
preview token, and that ``TicketType.remaining`` in the response reflects
real live stock (not just ``quantity_available``).
"""

from collections.abc import Awaitable, Callable
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import OrderStatus, PaymentMethod, PublishStatus
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.order import Order
from app.models.show import Show
from app.models.ticket import Ticket
from app.models.ticket_type import TicketType


async def test_get_published_event_by_slug_returns_nested_data(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED, name="Christmas Passion")
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    await make_ticket_type(show_id=show.id, name="Adult", price=Decimal("15.00"), quantity_available=50)

    response = await client.get(f"/api/v1/public/events/{event.slug}")
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Christmas Passion"
    assert body["is_preview"] is False
    assert len(body["shows"]) == 1
    assert len(body["shows"][0]["ticket_types"]) == 1
    assert body["shows"][0]["ticket_types"][0]["name"] == "Adult"
    assert body["shows"][0]["ticket_types"][0]["remaining"] == 50


async def test_get_event_by_slug_404s_for_nonexistent_slug(client: AsyncClient) -> None:
    response = await client.get("/api/v1/public/events/no-such-event")
    assert response.status_code == 404
    assert response.json()["detail"] == "Event not found."


async def test_get_event_by_slug_404s_for_draft_event_identically_to_nonexistent(
    client: AsyncClient, make_event: Callable[..., Awaitable[Event]]
) -> None:
    event = await make_event(status=PublishStatus.DRAFT)

    draft_response = await client.get(f"/api/v1/public/events/{event.slug}")
    missing_response = await client.get("/api/v1/public/events/definitely-does-not-exist")

    assert draft_response.status_code == missing_response.status_code == 404
    # A guess against the public slug URL can never distinguish "doesn't
    # exist" from "exists but still draft" — identical response either way.
    assert draft_response.json() == missing_response.json()


async def test_published_route_excludes_draft_shows(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    await make_show(event_id=event.id, status=PublishStatus.DRAFT)

    response = await client.get(f"/api/v1/public/events/{event.slug}")
    assert response.status_code == 200
    assert len(response.json()["shows"]) == 1


async def test_preview_route_200s_with_correct_token_for_draft_event(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
) -> None:
    event = await make_event(status=PublishStatus.DRAFT)
    await make_show(event_id=event.id, status=PublishStatus.DRAFT)

    response = await client.get(f"/api/v1/public/preview/{event.preview_token}")
    assert response.status_code == 200
    body = response.json()
    assert body["is_preview"] is True
    # Preview includes every Show regardless of its own draft status.
    assert len(body["shows"]) == 1


async def test_preview_route_404s_for_wrong_or_random_token(
    client: AsyncClient, make_event: Callable[..., Awaitable[Event]]
) -> None:
    await make_event(status=PublishStatus.DRAFT)

    response = await client.get("/api/v1/public/preview/totally-made-up-token")
    assert response.status_code == 404


async def test_preview_response_never_leaks_another_events_data(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
) -> None:
    event_a = await make_event(status=PublishStatus.DRAFT, name="Event A")
    event_b = await make_event(status=PublishStatus.DRAFT, name="Event B")
    await make_show(event_id=event_a.id, status=PublishStatus.DRAFT, venue_name="Venue A")
    await make_show(event_id=event_b.id, status=PublishStatus.DRAFT, venue_name="Venue B")

    response = await client.get(f"/api/v1/public/preview/{event_a.preview_token}")
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Event A"
    assert all(show["venue_name"] == "Venue A" for show in body["shows"])


async def test_published_events_do_not_leak_across_slugs(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
) -> None:
    event_a = await make_event(status=PublishStatus.PUBLISHED, name="Event A")
    event_b = await make_event(status=PublishStatus.PUBLISHED, name="Event B")
    await make_show(event_id=event_a.id, status=PublishStatus.PUBLISHED, venue_name="Venue A")
    await make_show(event_id=event_b.id, status=PublishStatus.PUBLISHED, venue_name="Venue B")

    response = await client.get(f"/api/v1/public/events/{event_a.slug}")
    body = response.json()
    assert body["name"] == "Event A"
    assert all(show["venue_name"] == "Venue A" for show in body["shows"])


async def test_remaining_stock_drops_as_tickets_are_created(
    client: AsyncClient,
    db_session: AsyncSession,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=10)
    await make_event_config(event_id=event.id, enabled_payment_methods=[PaymentMethod.DOOR])

    before = await client.get(f"/api/v1/public/events/{event.slug}")
    assert before.json()["shows"][0]["ticket_types"][0]["remaining"] == 10

    order = Order(
        event_id=event.id,
        buyer_name="Buyer",
        buyer_email="buyer@example.test",
        buyer_address="1 Test Street",
        status=OrderStatus.PAID,
        payment_method=PaymentMethod.DOOR,
        total=Decimal("30.00"),
        language="en",
    )
    db_session.add(order)
    await db_session.flush()
    for _ in range(3):
        db_session.add(Ticket(order_id=order.id, ticket_type_id=ticket_type.id))
    await db_session.commit()

    after = await client.get(f"/api/v1/public/events/{event.slug}")
    assert after.json()["shows"][0]["ticket_types"][0]["remaining"] == 7
