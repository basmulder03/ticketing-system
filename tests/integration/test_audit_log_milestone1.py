"""Extends ``test_audit_log.py``'s Milestone-0 pattern to Milestone 1's
Event/Show/TicketType/EventConfig create/update/delete routes: every write
must produce exactly one audit entry, correctly attributed to whichever
principal made it (human admin vs. AI agent — never a generic actor), per
PROJECT_BRIEF.md's AI/Agent Access requirement.

Also regression-covers a bug found while writing these tests: ``Show.date``/
``doors_time``/``start_time`` (and ``EventConfig.sales_live_at``) are
``date``/``time``/``datetime`` values that Python's default JSON encoder
can't serialize — passing them straight into the audit log's ``detail``
JSON column crashed the request with a 500 the first time either field was
patched. Fixed in ``app/api/routes/shows.py`` and
``app/api/routes/event_configs.py`` by stringifying every value first, the
same convention ``app/api/routes/ticket_types.py`` already used.
"""

from collections.abc import Awaitable, Callable

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLogEntry
from app.models.enums import ActorType
from tests.integration.conftest import SeededAdmin, SeededAgent

_SHOW_BODY = {
    "date": "2026-12-18",
    "doors_time": "19:30:00",
    "start_time": "20:00:00",
    "venue_name": "Venue",
    "venue_address": "1 Street",
    "capacity": 100,
}


async def _login(client: AsyncClient, seeded: SeededAdmin) -> None:
    response = await client.post("/api/v1/auth/login", json={"email": seeded.user.email, "password": seeded.password})
    assert response.status_code == 200


async def _entries_for(db_session: AsyncSession, action: str) -> list[AuditLogEntry]:
    result = await db_session.execute(select(AuditLogEntry).where(AuditLogEntry.action == action))
    return list(result.scalars().all())


# --- Event ---


async def test_event_create_update_delete_are_each_audited_for_an_admin(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], db_session: AsyncSession
) -> None:
    seeded = await make_admin_user()
    await _login(client, seeded)

    created = await client.post("/api/v1/events", json={"name": "Audit Event", "slug": "audit-event"})
    event_id = created.json()["id"]
    await client.patch(f"/api/v1/events/{event_id}", json={"sales_paused": True})
    await client.delete(f"/api/v1/events/{event_id}")

    for action in ("event.create", "event.update", "event.delete"):
        entries = await _entries_for(db_session, action)
        assert len(entries) == 1, f"expected exactly one {action} entry"
        entry = entries[0]
        assert entry.actor_type == ActorType.HUMAN
        assert entry.actor_id == seeded.user.id
        assert entry.actor_name == seeded.user.email
        assert entry.target_type == "Event"
        assert entry.target_id == event_id


async def test_event_create_is_attributed_to_the_agent_that_made_it(
    client: AsyncClient, make_agent_account: Callable[..., Awaitable[SeededAgent]], db_session: AsyncSession
) -> None:
    seeded = await make_agent_account(name="content-bot")
    client.headers["X-Agent-Api-Key"] = seeded.raw_key

    response = await client.post("/api/v1/events", json={"name": "Agent Event", "slug": "agent-event"})
    assert response.status_code == 201

    entries = await _entries_for(db_session, "event.create")
    assert len(entries) == 1
    entry = entries[0]
    assert entry.actor_type == ActorType.AI_AGENT
    assert entry.actor_id == seeded.account.id
    assert entry.actor_name == "content-bot"


# --- Show (regression coverage for the date/time JSON bug) ---


async def test_show_update_with_date_and_time_fields_is_audited_without_error(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], db_session: AsyncSession
) -> None:
    seeded = await make_admin_user()
    await _login(client, seeded)
    event = await client.post("/api/v1/events", json={"name": "Event", "slug": "show-audit-event"})
    event_id = event.json()["id"]
    show = await client.post(f"/api/v1/events/{event_id}/shows", json=_SHOW_BODY)
    show_id = show.json()["id"]

    # Patches every field that previously triggered the JSON-serialization
    # crash (date, doors_time, start_time all changed at once).
    response = await client.patch(
        f"/api/v1/events/{event_id}/shows/{show_id}",
        json={"date": "2026-12-25", "doors_time": "18:00:00", "start_time": "18:30:00"},
    )

    assert response.status_code == 200

    entries = await _entries_for(db_session, "show.update")
    assert len(entries) == 1
    entry = entries[0]
    assert entry.actor_type == ActorType.HUMAN
    assert entry.target_type == "Show"
    assert entry.target_id == show_id
    assert entry.detail is not None
    assert entry.detail["date"] == "2026-12-25"


async def test_show_create_delete_are_each_audited(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], db_session: AsyncSession
) -> None:
    seeded = await make_admin_user()
    await _login(client, seeded)
    event = await client.post("/api/v1/events", json={"name": "Event", "slug": "show-audit-crud"})
    event_id = event.json()["id"]

    show = await client.post(f"/api/v1/events/{event_id}/shows", json=_SHOW_BODY)
    show_id = show.json()["id"]
    await client.delete(f"/api/v1/events/{event_id}/shows/{show_id}")

    for action in ("show.create", "show.delete"):
        entries = await _entries_for(db_session, action)
        assert len(entries) == 1
        assert entries[0].actor_type == ActorType.HUMAN
        assert entries[0].actor_name == seeded.user.email
        assert entries[0].target_id == show_id


# --- TicketType ---


async def test_ticket_type_create_update_delete_are_each_audited(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], db_session: AsyncSession
) -> None:
    seeded = await make_admin_user()
    await _login(client, seeded)
    event = await client.post("/api/v1/events", json={"name": "Event", "slug": "tt-audit-event"})
    event_id = event.json()["id"]
    show = await client.post(f"/api/v1/events/{event_id}/shows", json=_SHOW_BODY)
    show_id = show.json()["id"]

    created = await client.post(
        f"/api/v1/shows/{show_id}/ticket-types",
        json={"name": "Adult", "price": "15.00", "quantity_available": 100},
    )
    ticket_type_id = created.json()["id"]
    await client.patch(f"/api/v1/shows/{show_id}/ticket-types/{ticket_type_id}", json={"price": "17.50"})
    await client.delete(f"/api/v1/shows/{show_id}/ticket-types/{ticket_type_id}")

    for action in ("ticket_type.create", "ticket_type.update", "ticket_type.delete"):
        entries = await _entries_for(db_session, action)
        assert len(entries) == 1
        assert entries[0].actor_type == ActorType.HUMAN
        assert entries[0].target_type == "TicketType"
        assert entries[0].target_id == ticket_type_id


async def test_ticket_type_create_is_attributed_to_the_agent_that_made_it(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_agent_account: Callable[..., Awaitable[SeededAgent]],
    client_factory: Callable[[str | None], AsyncClient],
    db_session: AsyncSession,
) -> None:
    await _login(client, await make_admin_user())
    event = await client.post("/api/v1/events", json={"name": "Event", "slug": "tt-agent-event"})
    event_id = event.json()["id"]
    show = await client.post(f"/api/v1/events/{event_id}/shows", json=_SHOW_BODY)
    show_id = show.json()["id"]

    seeded_agent = await make_agent_account(name="ticket-bot")
    async with client_factory(None) as agent_client:
        agent_client.headers["X-Agent-Api-Key"] = seeded_agent.raw_key
        response = await agent_client.post(
            f"/api/v1/shows/{show_id}/ticket-types",
            json={"name": "Agent Ticket", "price": "5.00", "quantity_available": 10},
        )
        assert response.status_code == 201

    entries = await _entries_for(db_session, "ticket_type.create")
    assert len(entries) == 1
    entry = entries[0]
    assert entry.actor_type == ActorType.AI_AGENT
    assert entry.actor_id == seeded_agent.account.id
    assert entry.actor_name == "ticket-bot"


# --- EventConfig ---


async def test_event_config_create_and_update_are_each_audited_and_attributed_to_the_admin(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], db_session: AsyncSession
) -> None:
    seeded = await make_admin_user()
    await _login(client, seeded)
    event = await client.post("/api/v1/events", json={"name": "Event", "slug": "config-audit-event"})
    event_id = event.json()["id"]

    await client.put(f"/api/v1/events/{event_id}/config", json={"smtp_host": "mailpit"})
    await client.put(f"/api/v1/events/{event_id}/config", json={"smtp_port": 1025})

    create_entries = await _entries_for(db_session, "event_config.create")
    update_entries = await _entries_for(db_session, "event_config.update")
    assert len(create_entries) == 1
    assert len(update_entries) == 1
    for entry in (*create_entries, *update_entries):
        assert entry.actor_type == ActorType.HUMAN
        assert entry.actor_name == seeded.user.email
        assert entry.target_type == "EventConfig"


async def test_event_config_copy_from_is_audited(
    client: AsyncClient, make_admin_user: Callable[..., Awaitable[SeededAdmin]], db_session: AsyncSession
) -> None:
    seeded = await make_admin_user()
    await _login(client, seeded)
    source = await client.post("/api/v1/events", json={"name": "Source", "slug": "copy-audit-source"})
    target = await client.post("/api/v1/events", json={"name": "Target", "slug": "copy-audit-target"})
    source_id = source.json()["id"]
    target_id = target.json()["id"]
    await client.put(f"/api/v1/events/{source_id}/config", json={"smtp_host": "mailpit"})

    response = await client.post(f"/api/v1/events/{target_id}/config/copy-from/{source_id}")
    assert response.status_code == 200

    entries = await _entries_for(db_session, "event_config.copy_from")
    assert len(entries) == 1
    entry = entries[0]
    assert entry.actor_type == ActorType.HUMAN
    assert entry.actor_name == seeded.user.email
    assert entry.detail is not None
    assert entry.detail["source_event_id"] == source_id
    assert entry.detail["target_event_id"] == target_id


async def test_no_milestone1_audit_entry_ever_has_an_unattributed_generic_actor(
    client: AsyncClient,
    make_admin_user: Callable[..., Awaitable[SeededAdmin]],
    make_agent_account: Callable[..., Awaitable[SeededAgent]],
    client_factory: Callable[[str | None], AsyncClient],
    db_session: AsyncSession,
) -> None:
    """Same systemic check as ``test_audit_log.py``'s Milestone-0 version,
    extended across a mixed human+agent Milestone-1 write sequence."""
    await _login(client, await make_admin_user())
    event = await client.post("/api/v1/events", json={"name": "Event", "slug": "mixed-audit-event"})
    event_id = event.json()["id"]
    show = await client.post(f"/api/v1/events/{event_id}/shows", json=_SHOW_BODY)
    show_id = show.json()["id"]
    await client.put(f"/api/v1/events/{event_id}/config", json={"smtp_host": "mailpit"})

    seeded_agent = await make_agent_account(name="mixed-bot")
    async with client_factory(None) as agent_client:
        agent_client.headers["X-Agent-Api-Key"] = seeded_agent.raw_key
        await agent_client.post(
            f"/api/v1/shows/{show_id}/ticket-types",
            json={"name": "Agent Ticket", "price": "5.00", "quantity_available": 10},
        )

    result = await db_session.execute(select(AuditLogEntry))
    entries = result.scalars().all()

    assert len(entries) >= 4  # event.create, show.create, event_config.create, ticket_type.create
    for entry in entries:
        assert entry.actor_type in (ActorType.HUMAN, ActorType.AI_AGENT)
        assert entry.actor_name
        assert entry.actor_name.lower() != "system"
        assert entry.actor_id is not None
