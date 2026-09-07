"""Read-only audit-log viewer route.

Admin-only per PROJECT_BRIEF.md: agent keys must never be able to touch
the audit log itself, which ``require_admin`` enforces structurally (see
``app.api.deps``).
"""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, require_admin
from app.db.session import get_session
from app.models.audit_log import AuditLogEntry
from app.schemas.audit import AuditLogEntryOut

router = APIRouter(prefix="/api/v1/admin/audit-log", tags=["admin", "audit-log"])


@router.get("")
async def list_audit_log(
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    limit: int = 100,
) -> list[AuditLogEntryOut]:
    """Return the most recent audit entries, newest first. Admin-only.

    ``limit`` is clamped to ``[1, 500]`` to prevent an accidentally huge
    unbounded query.
    """
    clamped_limit = max(1, min(limit, 500))
    result = await session.execute(
        select(AuditLogEntry).order_by(AuditLogEntry.created_at.desc()).limit(clamped_limit)
    )
    return [
        AuditLogEntryOut(
            id=str(entry.id),
            actor_type=entry.actor_type.value,
            actor_id=str(entry.actor_id) if entry.actor_id else None,
            actor_name=entry.actor_name,
            action=entry.action,
            target_type=entry.target_type,
            target_id=entry.target_id,
            detail=entry.detail,
            created_at=entry.created_at,
        )
        for entry in result.scalars().all()
    ]
