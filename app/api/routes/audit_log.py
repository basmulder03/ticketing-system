"""Read-only audit-log route. Admin-only: agents must never read or touch the audit log."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, require_admin
from app.db.session import get_session
from app.models.audit_log import AuditLogEntry
from app.models.enums import ActorType
from app.schemas.audit import AuditLogEntryOut, AuditLogPageOut

router = APIRouter(prefix="/api/v1/admin/audit-log", tags=["admin", "audit-log"])


def _to_out(entry: AuditLogEntry) -> AuditLogEntryOut:
    return AuditLogEntryOut(
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


@router.get("")
async def list_audit_log(
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    limit: int = 100,
    before: str | None = None,
    actor_type: ActorType | None = None,
    action: str | None = None,
    target_type: str | None = None,
) -> AuditLogPageOut:
    """One page, newest first. ``limit`` is clamped to 1-500.

    ``before`` is an entry id from a previous page's last row — pass it to
    keep paging backward in time. Keyset (not offset) pagination: a page
    boundary is anchored to a real row's ``(created_at, id)``, so entries
    written between two page loads (this is an append-only, ever-growing
    log) can never shift already-fetched rows onto the wrong page or
    duplicate/skip one, the way an offset would.

    ``action`` matches as a case-insensitive substring (actions are
    dot-namespaced, e.g. ``order.mark_paid`` — typing ``order`` filters to
    every order action at once). ``actor_type``/``target_type`` match exactly.
    """
    clamped_limit = max(1, min(limit, 500))

    query = select(AuditLogEntry)
    if actor_type is not None:
        query = query.where(AuditLogEntry.actor_type == actor_type)
    if action:
        query = query.where(AuditLogEntry.action.ilike(f"%{action}%"))
    if target_type:
        query = query.where(AuditLogEntry.target_type == target_type)

    if before is not None:
        try:
            cursor_id = uuid.UUID(before)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown cursor.") from None
        cursor = await session.get(AuditLogEntry, cursor_id)
        if cursor is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown cursor.")
        query = query.where(
            (AuditLogEntry.created_at < cursor.created_at)
            | ((AuditLogEntry.created_at == cursor.created_at) & (AuditLogEntry.id < cursor.id))
        )

    query = query.order_by(AuditLogEntry.created_at.desc(), AuditLogEntry.id.desc()).limit(clamped_limit + 1)
    result = await session.execute(query)
    rows = list(result.scalars().all())

    has_more = len(rows) > clamped_limit
    return AuditLogPageOut(entries=[_to_out(entry) for entry in rows[:clamped_limit]], has_more=has_more)
