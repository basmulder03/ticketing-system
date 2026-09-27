"""Pydantic response models for the audit-log viewer route."""

from datetime import datetime

from pydantic import BaseModel


class AuditLogEntryOut(BaseModel):
    """A single audit-log entry as returned by ``GET /api/v1/admin/audit-log``."""

    id: str
    actor_type: str
    actor_id: str | None
    actor_name: str
    action: str
    target_type: str | None
    target_id: str | None
    detail: dict[str, object] | None
    created_at: datetime


class AuditLogPageOut(BaseModel):
    """One page of entries, newest first, plus whether an older page exists.

    ``has_more`` lets the viewer show/hide a "load older" control without a
    separate count query — the route fetches one extra row internally and
    never returns it.
    """

    entries: list[AuditLogEntryOut]
    has_more: bool
