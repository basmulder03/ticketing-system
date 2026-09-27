"""The single audit-log writer. Always go through :func:`record_audit_entry`
so agent writes are attributed as ``AI_AGENT`` + agent name, never as a human
or a generic system actor.
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
    """Add one audit entry attributing ``action`` to ``principal`` (taken from
    auth, so routes can't spoof it). Flushes but doesn't commit.
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
