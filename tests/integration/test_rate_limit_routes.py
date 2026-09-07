"""Integration tests proving the in-memory rate limiter is actually wired
onto ``/api/v1/auth/login`` and the agent-API-key auth path, keyed per
client IP (see ``app.core.rate_limit`` and ``app.api.deps``).

Each test builds its own client(s) with a fresh pseudo-IP (see
``tests/integration/conftest.py``) so hammering one client's bucket here
can never leak into another test's login/agent-auth attempts.
"""

from collections.abc import Awaitable, Callable

from httpx import AsyncClient

from app.core.config import get_settings
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
