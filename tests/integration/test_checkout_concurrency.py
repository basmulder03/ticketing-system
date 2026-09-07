"""THE concurrency test PROJECT_BRIEF.md's Testing section calls out by
name: "simulate concurrent checkouts for the last remaining ticket and
assert exactly one succeeds, since this is the single most
consequence-heavy bug class in a system built around a sales-live moment."

This fires genuinely concurrent HTTP requests (via ``asyncio.gather``) at
the real ``POST /api/v1/public/checkout`` route over the ASGI app. Each
request resolves ``app.db.session.get_session`` independently (a fresh
``AsyncSession``/asyncpg connection per request, from the shared pool), so
this exercises real overlapping Postgres transactions and the real
``SELECT ... FOR UPDATE`` row lock in ``app.services.stock.reserve_stock``
— not an in-process mock and not sequential calls dressed up to look
concurrent.

The whole scenario is repeated across several fresh TicketTypes (each with
its own client, to stay under the per-IP checkout rate limit) so a flaky/
false-green pass is unlikely to slip through in CI.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import PaymentMethod, PublishStatus
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.show import Show
from app.models.ticket import Ticket
from app.models.ticket_type import TicketType

_CONCURRENT_REQUESTS = 8
_ITERATIONS = 3


def _checkout_payload(ticket_type_id: uuid.UUID, buyer_suffix: str) -> dict[str, object]:
    return {
        "buyer_name": f"Concurrent Buyer {buyer_suffix}",
        "buyer_email": f"buyer-{buyer_suffix}@example.test",
        "buyer_address": "1 Race Condition Lane",
        "language": "en",
        "payment_method": PaymentMethod.DOOR.value,
        "items": [{"ticket_type_id": str(ticket_type_id), "quantity": 1}],
    }


async def test_concurrent_checkouts_for_the_last_ticket_yield_exactly_one_success(
    db_session: AsyncSession,
    client_factory: Callable[[str | None], AsyncClient],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    await make_event_config(event_id=event.id, enabled_payment_methods=[PaymentMethod.DOOR])

    for iteration in range(_ITERATIONS):
        ticket_type = await make_ticket_type(show_id=show.id, quantity_available=1)

        async with client_factory(f"race-{iteration}-{uuid.uuid4().hex[:8]}") as client:
            responses = await asyncio.gather(
                *[
                    client.post(
                        "/api/v1/public/checkout",
                        json=_checkout_payload(ticket_type.id, f"{iteration}-{i}"),
                    )
                    for i in range(_CONCURRENT_REQUESTS)
                ]
            )

        statuses = sorted(r.status_code for r in responses)
        successes = [r for r in responses if r.status_code == 201]
        failures = [r for r in responses if r.status_code == 409]

        assert len(successes) == 1, (
            f"iteration {iteration}: expected exactly 1 success, got statuses={statuses}"
        )
        assert len(failures) == _CONCURRENT_REQUESTS - 1, (
            f"iteration {iteration}: expected the rest to be clean 409s, got statuses={statuses}"
        )
        for failure in failures:
            assert "remain" in failure.json()["detail"].lower()

        # The successful order must itself only hold exactly 1 ticket.
        order_body = successes[0].json()
        assert len(order_body["tickets"]) == 1
        # Milestone 2 always creates orders as PENDING regardless of chosen
        # payment method — PENDING_DOOR is reserved for Milestone 6 (see
        # app.models.enums.OrderStatus docstring).
        assert order_body["status"] == "pending"

        # And the DB-level truth, checked against a fresh session, must
        # never show more than 1 live ticket for this TicketType — the
        # actual guarantee the row lock exists to provide.
        result = await db_session.execute(select(Ticket).where(Ticket.ticket_type_id == ticket_type.id))
        tickets = result.scalars().all()
        assert len(tickets) == 1, f"iteration {iteration}: expected exactly 1 Ticket row, found {len(tickets)}"
