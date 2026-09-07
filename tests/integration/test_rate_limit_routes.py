"""Integration tests proving the in-memory rate limiter is actually wired
onto ``/api/v1/auth/login``, the agent-API-key auth path, and the public
checkout endpoint (Milestone 2), keyed per client IP (see
``app.core.rate_limit`` and ``app.api.deps``).

Each test builds its own client(s) with a fresh pseudo-IP (see
``tests/integration/conftest.py``) so hammering one client's bucket here
can never leak into another test's login/agent-auth/checkout attempts.
"""

from collections.abc import Awaitable, Callable

from httpx import AsyncClient

from app.core.config import get_settings
from app.models.enums import PaymentMethod, PublishStatus
from app.models.event import Event
from app.models.event_config import EventConfig
from app.models.show import Show
from app.models.ticket_type import TicketType
from tests.integration.conftest import SeededAgent

_settings = get_settings()


async def test_login_returns_429_once_the_per_ip_limit_is_exceeded(client: AsyncClient) -> None:
    limit = _settings.login_rate_limit_per_minute

    statuses = [
        (
            await client.post(
                "/api/v1/auth/login", json={"email": "nobody@example.test", "password": "wrong"}
            )
        ).status_code
        for _ in range(limit)
    ]
    assert all(status == 401 for status in statuses)

    blocked = await client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.test", "password": "wrong"}
    )
    assert blocked.status_code == 429


async def test_login_rate_limit_is_not_shared_across_different_client_ips(
    client_factory: Callable[[str | None], AsyncClient],
) -> None:
    limit = _settings.login_rate_limit_per_minute

    async with client_factory("client-ip-a") as client_a, client_factory("client-ip-b") as client_b:
        for _ in range(limit):
            response = await client_a.post(
                "/api/v1/auth/login", json={"email": "nobody@example.test", "password": "wrong"}
            )
            assert response.status_code == 401

        exhausted = await client_a.post(
            "/api/v1/auth/login", json={"email": "nobody@example.test", "password": "wrong"}
        )
        assert exhausted.status_code == 429

        # A different client IP has its own, still-fresh bucket.
        still_allowed = await client_b.post(
            "/api/v1/auth/login", json={"email": "nobody@example.test", "password": "wrong"}
        )
        assert still_allowed.status_code == 401


async def test_agent_key_auth_returns_429_once_the_per_ip_limit_is_exceeded(
    client: AsyncClient, make_agent_account: Callable[..., Awaitable[SeededAgent]]
) -> None:
    limit = _settings.agent_auth_rate_limit_per_minute
    seeded = await make_agent_account()
    headers = {"X-Agent-Api-Key": seeded.raw_key}

    statuses = [
        (await client.get("/api/v1/auth/me", headers=headers)).status_code for _ in range(limit)
    ]
    assert all(status == 200 for status in statuses)

    blocked = await client.get("/api/v1/auth/me", headers=headers)
    assert blocked.status_code == 429


async def test_checkout_returns_429_once_the_per_ip_limit_is_exceeded(
    client: AsyncClient,
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    limit = _settings.checkout_rate_limit_per_minute
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    # Plenty of stock so every one of `limit` attempts succeeds on its own
    # merits — the goal here is isolating the rate limiter, not stock.
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=limit + 10)
    await make_event_config(event_id=event.id, enabled_payment_methods=[PaymentMethod.DOOR])

    def _payload() -> dict[str, object]:
        return {
            "buyer_name": "Buyer",
            "buyer_email": "buyer@example.test",
            "buyer_address": "1 Test Street",
            "language": "en",
            "payment_method": PaymentMethod.DOOR.value,
            "items": [{"ticket_type_id": str(ticket_type.id), "quantity": 1}],
        }

    statuses = [
        (await client.post("/api/v1/public/checkout", json=_payload())).status_code for _ in range(limit)
    ]
    assert all(status == 201 for status in statuses)

    blocked = await client.post("/api/v1/public/checkout", json=_payload())
    assert blocked.status_code == 429


async def test_checkout_rate_limit_is_not_shared_across_different_client_ips(
    client_factory: Callable[[str | None], AsyncClient],
    make_event: Callable[..., Awaitable[Event]],
    make_show: Callable[..., Awaitable[Show]],
    make_ticket_type: Callable[..., Awaitable[TicketType]],
    make_event_config: Callable[..., Awaitable[EventConfig]],
) -> None:
    limit = _settings.checkout_rate_limit_per_minute
    event = await make_event(status=PublishStatus.PUBLISHED)
    show = await make_show(event_id=event.id, status=PublishStatus.PUBLISHED)
    ticket_type = await make_ticket_type(show_id=show.id, quantity_available=(limit + 10) * 2)
    await make_event_config(event_id=event.id, enabled_payment_methods=[PaymentMethod.DOOR])

    def _payload() -> dict[str, object]:
        return {
            "buyer_name": "Buyer",
            "buyer_email": "buyer@example.test",
            "buyer_address": "1 Test Street",
            "language": "en",
            "payment_method": PaymentMethod.DOOR.value,
            "items": [{"ticket_type_id": str(ticket_type.id), "quantity": 1}],
        }

    async with client_factory("checkout-ip-a") as client_a, client_factory("checkout-ip-b") as client_b:
        for _ in range(limit):
            response = await client_a.post("/api/v1/public/checkout", json=_payload())
            assert response.status_code == 201

        exhausted = await client_a.post("/api/v1/public/checkout", json=_payload())
        assert exhausted.status_code == 429

        # A different client IP has its own, still-fresh bucket.
        still_allowed = await client_b.post("/api/v1/public/checkout", json=_payload())
        assert still_allowed.status_code == 201
