"""Reusable audit-log writer.

Every route that performs a write worth auditing should call
:func:`record_audit_entry` rather than constructing ``AuditLogEntry`` rows
directly — keeping the actor-type/actor-name attribution logic in one
place is what guarantees agent-driven writes are always recorded as
``ActorType.AI_AGENT`` plus the agent's name, never as a human or a
generic "system" actor.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal
from app.models.audit_log import AuditLogEntry


async def record_audit_entry(
    session: AsyncSession,
    principal: Principal,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> AuditLogEntry:
    """Write one immutable audit entry attributing ``action`` to ``principal``.

    ``principal.actor_type``/``principal.name`` come directly from the
    already-authenticated caller (see ``app.api.deps``) — route code
    cannot override or spoof the actor. Adds the entry to ``session`` and
    flushes (so ``entry.id`` is populated) but does not commit; the caller
    controls the transaction boundary alongside whatever else it's
    persisting in the same request.
    """
    entry = AuditLogEntry(
        actor_type=principal.actor_type,
        actor_id=principal.id,
        actor_name=principal.name,
        action=action,
        target_type=target_type,
        target_id=target_id,
        detail=detail,
    )
    session.add(entry)
    await session.flush()
    return entry
