"""Integration tests for ``app.services.audit.record_audit_entry`` and its
call sites (login, agent-account create/revoke), plus the
``GET /api/v1/admin/audit-log`` viewer route.

The core invariant under test: every audit entry produced by the app so
far is attributed to a real actor (``human`` + the admin's email, or
``ai_agent`` + the agent's name) — never a generic/unattributed actor, per
PROJECT_BRIEF.md's AI/Agent Access requirement.
"""

from collections.abc import Awaitable, Callable

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLogEntry
from app.models.enums import ActorType
from tests.integration.conftest import SeededAdmin, SeededAgent


async def _login(client: AsyncClient, seeded: SeededAdmin) -> None:
    response = await client.post(
        "/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password}
    )
    assert response.status_code == 200


async def test_human_login_writes_an_audit_entry_attributed_to_the_admin(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    db_session: AsyncSession,
) -> None:
    seeded = await make_admin_user()
    await _login(client, seeded)

    result = await db_session.execute(
        select(AuditLogEntry).where(AuditLogEntry.action == "admin_user.login")
    )
    entries = result.scalars().all()

    assert len(entries) == 1
    entry = entries[0]
    assert entry.actor_type == ActorType.HUMAN
    assert entry.actor_id == seeded.user.id
    assert entry.actor_name == seeded.user.email
    assert entry.target_type == "AdminUser"
    assert entry.target_id == str(seeded.user.id)


async def test_agent_account_create_is_attributed_to_the_creating_admin(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], db_session: AsyncSession
) -> None:
    seeded = await make_admin_user()
    await _login(client, seeded)

    create_response = await client.post("/api/v1/admin/agent-accounts", json={"name": "content-bot"})
    assert create_response.status_code == 201
    agent_id = create_response.json()["id"]

    result = await db_session.execute(
        select(AuditLogEntry).where(AuditLogEntry.action == "agent_account.create")
    )
    entries = result.scalars().all()

    assert len(entries) == 1
    entry = entries[0]
    # Attributed to the admin who created it — NOT to the new AgentAccount
    # itself and NOT to a generic "system" actor, per PROJECT_BRIEF.md: an
    # agent-account create is always a human admin action (agent keys can
    # never reach this route — see test_deps_admin_scoping.py).
    assert entry.actor_type == ActorType.HUMAN
    assert entry.actor_id == seeded.user.id
    assert entry.actor_name == seeded.user.email
    assert entry.target_type == "AgentAccount"
    assert entry.target_id == agent_id
    assert entry.detail == {"name": "content-bot"}


async def test_agent_account_revoke_is_attributed_to_the_revoking_admin(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_agent_account: Callable[..., Awaitable[SeededAgent]],
    db_session: AsyncSession,
) -> None:
    seeded = await make_admin_user()
    await _login(client, seeded)
    agent = await make_agent_account(name="content-bot")

    revoke_response = await client.post(f"/api/v1/admin/agent-accounts/{agent.account.id}/revoke")
    assert revoke_response.status_code == 200

    result = await db_session.execute(
        select(AuditLogEntry).where(AuditLogEntry.action == "agent_account.revoke")
    )
    entries = result.scalars().all()

    assert len(entries) == 1
    entry = entries[0]
    assert entry.actor_type == ActorType.HUMAN
    assert entry.actor_id == seeded.user.id
    assert entry.actor_name == seeded.user.email
    assert entry.target_id == str(agent.account.id)


async def test_double_revoke_does_not_write_a_second_audit_entry(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_agent_account: Callable[..., Awaitable[SeededAgent]],
    db_session: AsyncSession,
) -> None:
    seeded = await make_admin_user()
    await _login(client, seeded)
    agent = await make_agent_account(name="content-bot")

    first = await client.post(f"/api/v1/admin/agent-accounts/{agent.account.id}/revoke")
    second = await client.post(f"/api/v1/admin/agent-accounts/{agent.account.id}/revoke")
    assert first.status_code == 200
    assert second.status_code == 200

    result = await db_session.execute(
        select(AuditLogEntry).where(AuditLogEntry.action == "agent_account.revoke")
    )
    assert len(result.scalars().all()) == 1


async def test_no_audit_entry_ever_has_an_unattributed_generic_actor(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    db_session: AsyncSession,
) -> None:
    """Defensive, systemic check across every write path exercised in this
    suite so far: every persisted entry is either human+identity or
    ai_agent+identity, never a blank/"system"/unattributed actor."""
    seeded = await make_admin_user()
    await _login(client, seeded)
    await client.post("/api/v1/admin/agent-accounts", json={"name": "content-bot"})

    result = await db_session.execute(select(AuditLogEntry))
    entries = result.scalars().all()

    assert len(entries) >= 2  # at least the login + the create
    for entry in entries:
        assert entry.actor_type in (ActorType.HUMAN, ActorType.AI_AGENT)
        assert entry.actor_name  # never blank
        assert entry.actor_name.lower() != "system"
        assert entry.actor_id is not None


async def test_audit_log_route_lists_entries_newest_first_for_an_admin(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]]
) -> None:
    seeded = await make_admin_user()
    await _login(client, seeded)
    await client.post("/api/v1/admin/agent-accounts", json={"name": "content-bot"})

    response = await client.get("/api/v1/admin/audit-log")

    assert response.status_code == 200
    entries = response.json()
    actions = [entry["action"] for entry in entries]
    # Both the login and the agent-account create wrote an entry; the most
    # recent action (the create) comes first.
    assert actions[0] == "agent_account.create"
    assert "admin_user.login" in actions
    created_ats = [entry["created_at"] for entry in entries]
    assert created_ats == sorted(created_ats, reverse=True)
