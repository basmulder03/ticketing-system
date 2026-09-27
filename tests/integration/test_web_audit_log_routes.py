"""Integration tests for the backoffice audit-log viewer web surface
(``app/web/routes/audit_log.py``): ``GET /audit-log``.

Per this task's discipline note, does NOT re-test the underlying JSON API's
own business logic (what gets recorded and when, or the filter/cursor
mechanics themselves) — that lives in ``tests/integration/test_audit_log.py``.
Focuses on what the WEB layer adds: rendering real entries with correct
actor/action/target attribution, the ``limit`` snapping behavior, translating
query params into the API's filter/cursor params (and building the "load
older" link from a real page's last row), and ``require_web_admin`` scoping
(this page has no mutating routes, so no CSRF surface at all).
"""

import html
import re
from collections.abc import Awaitable, Callable

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLogEntry
from app.models.enums import ActorType, AdminRole
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


# --- Filtering -------------------------------------------------------------


async def test_audit_log_action_filter_excludes_non_matching_entries(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())
    await client.post("/api/v1/admin/agent-accounts", json={"name": "content-bot"})

    response = await client.get("/audit-log?action=agent_account")

    assert response.status_code == 200
    assert "agent_account.create" in response.text
    assert "admin_user.login" not in response.text
    assert "No entries match these filters." not in response.text


async def test_audit_log_actor_type_filter_with_no_matches_shows_the_empty_state(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())

    response = await client.get("/audit-log?actor_type=ai_agent")

    assert response.status_code == 200
    assert "No entries match these filters." in response.text


async def test_audit_log_unknown_actor_type_is_ignored_rather_than_erroring(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    await _api_login(client, await make_admin_user())

    response = await client.get("/audit-log?actor_type=not-a-real-type")

    assert response.status_code == 200
    assert "No entries match these filters." not in response.text


# --- Keyset pagination ("load older") ---------------------------------------


async def _insert_dummy_entries(db_session: AsyncSession, *, count: int, action_prefix: str) -> None:
    """Bulk-insert plain audit rows directly, bypassing ``record_audit_entry``
    — this file tests the WEB layer's pagination/link-building, not what
    gets written or when (see the module docstring), so real HTTP round
    trips through every write path would only make this slower.
    """
    for i in range(count):
        db_session.add(
            AuditLogEntry(
                actor_type=ActorType.SYSTEM,
                actor_name="test-fixture",
                action=f"{action_prefix}.{i}",
            )
        )
    await db_session.commit()


_SMALLEST_ALLOWED_LIMIT = 50
"""Matches app.web.routes.audit_log._ALLOWED_LIMITS[0] -- any other value
snaps to the default (100), so these pagination tests need at least this
many entries on top of the login to force a real "load older" page."""


async def test_audit_log_shows_a_load_older_link_when_more_entries_exist(
    client: AsyncClient,
    db_session: AsyncSession,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
) -> None:
    await _api_login(client, await make_admin_user())
    await _insert_dummy_entries(db_session, count=_SMALLEST_ALLOWED_LIMIT, action_prefix="dummy.load_more")

    response = await client.get(f"/audit-log?limit={_SMALLEST_ALLOWED_LIMIT}")

    assert response.status_code == 200
    assert "Load older entries" in response.text
    assert "before=" in response.text


async def test_audit_log_following_the_load_older_link_shows_different_entries(
    client: AsyncClient,
    db_session: AsyncSession,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
) -> None:
    await _api_login(client, await make_admin_user())
    await _insert_dummy_entries(db_session, count=_SMALLEST_ALLOWED_LIMIT, action_prefix="dummy.page")
    newest_action = f"dummy.page.{_SMALLEST_ALLOWED_LIMIT - 1}"

    first_page = await client.get(f"/audit-log?limit={_SMALLEST_ALLOWED_LIMIT}")
    assert newest_action in first_page.text
    assert "admin_user.login" not in first_page.text  # the oldest entry, pushed off page 1

    older_link_match = re.search(r'href="(/audit-log\?[^"]+)"[^>]*>Load older entries', first_page.text)
    assert older_link_match, "expected a 'Load older entries' link on the page"
    older_href = html.unescape(older_link_match.group(1))  # the template renders "&" as "&amp;"
    second_page = await client.get(older_href)

    assert second_page.status_code == 200
    assert "admin_user.login" in second_page.text
    assert newest_action not in second_page.text  # only shown on the first (newer) page


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
