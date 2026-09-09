"""Integration tests for the backoffice audit-log viewer web surface
(``app/web/routes/audit_log.py``, this branch's audit-log section):
``GET /audit-log``.

Per this task's discipline note, does NOT re-test the underlying JSON API's
own business logic (what gets recorded and when) — that lives in ``tests/
integration/test_audit_log.py``. Focuses on what the WEB layer adds:
rendering real entries with correct actor/action/target attribution, the
``limit`` snapping behavior, and ``require_web_admin`` scoping (this page
has no mutating routes, so no CSRF surface at all).
"""

from collections.abc import Awaitable, Callable

from httpx import AsyncClient

from app.models.enums import AdminRole
from tests.integration.conftest import SeededAdmin


async def _api_login(client: AsyncClient, seeded: SeededAdmin) -> None:
    response = await client.post(
        "/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password}
    )
    assert response.status_code == 200


# --- Real entries render with correct actor/action/target attribution --------


async def test_audit_log_renders_a_real_event_creation_entry_correctly_attributed(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    seeded = await make_admin_user(email="auditor@example.test")
    await _api_login(client, seeded)

    create_response = await client.post(
        "/api/v1/events", json={"name": "Audited Event", "slug": "audited-event"}
    )
    assert create_response.status_code == 201
    event_id = create_response.json()["id"]

    response = await client.get("/audit-log")

    assert response.status_code == 200
    html = response.text
    assert "event.create" in html or "event.created" in html
    assert "auditor@example.test" in html
    assert "human" in html  # actor_type badge
    assert "Event" in html
    assert event_id[:8] in html


async def test_audit_log_renders_agent_actions_with_the_ai_agent_actor_type_not_merged_into_human(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    client_factory: Callable[[str | None], AsyncClient],
) -> None:
    await _api_login(client, await make_admin_user())
    create_agent = await client.post("/api/v1/admin/agent-accounts", json={"name": "audit-test-agent"})
    assert create_agent.status_code == 201
    raw_key = create_agent.json()["api_key"]

    async with client_factory(None) as agent_client:
        agent_event = await agent_client.post(
            "/api/v1/events",
            json={"name": "Agent Created Event", "slug": "agent-created-event"},
            headers={"X-Agent-Api-Key": raw_key},
        )
        assert agent_event.status_code == 201

    response = await client.get("/audit-log")

    assert response.status_code == 200
    assert "audit-test-agent" in response.text
    assert "ai_agent" in response.text


# --- limit control -------------------------------------------------------------


async def test_audit_log_invalid_limit_falls_back_to_the_default(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())

    response = await client.get("/audit-log?limit=not-a-number")

    assert response.status_code == 200
    assert 'value="100" selected' in response.text


async def test_audit_log_valid_limit_is_honored(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())

    response = await client.get("/audit-log?limit=50")

    assert response.status_code == 200
    assert 'value="50" selected' in response.text


# --- require_web_admin scoping --------------------------------------------------


async def test_audit_log_unauthenticated_redirects_to_login(client: AsyncClient) -> None:
    response = await client.get("/audit-log")

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


async def test_audit_log_unreachable_by_scanner_role(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    seeded = await make_admin_user(role=AdminRole.SCANNER)
    await _api_login(client, seeded)

    response = await client.get("/audit-log")

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")
