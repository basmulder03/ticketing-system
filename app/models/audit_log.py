"""``AuditLogEntry``: an immutable record of a backoffice/agent-driven change."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import UUIDPrimaryKeyMixin, utcnow
from app.models.enums import ActorType


class AuditLogEntry(UUIDPrimaryKeyMixin, Base):
    """Append-only record of who did what, when.

    ``actor_id``/``actor_name`` are a snapshot, not foreign keys, so entries
    stay readable after an account is deleted. Only ``app.services.audit``
    writes rows; agent actions are always recorded as ``AI_AGENT``.
    """

    __tablename__ = "audit_log_entries"

    actor_type: Mapped[ActorType] = mapped_column(
        SAEnum(ActorType, name="actor_type", native_enum=True, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    actor_name: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    target_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    detail: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
