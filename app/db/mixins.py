"""Shared declarative mixins for ORM models.

``UUIDPrimaryKeyMixin`` and ``TimestampMixin`` are used by every model that
needs them so id generation and created/updated tracking stay consistent
across models instead of being redefined per-model (DRY). Models that must
stay strictly immutable (e.g. ``AuditLogEntry``) intentionally use only the
PK mixin and add their own single ``created_at`` column.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column


def utcnow() -> datetime:
    """Current UTC time. A named function (not a lambda) so it's easy to
    identify in stack traces/mock in tests. Public: reused by models that
    need a single ``created_at``-only timestamp without the full
    ``TimestampMixin`` (e.g. the immutable ``AuditLogEntry``)."""
    return datetime.now(UTC)


class UUIDPrimaryKeyMixin:
    """Adds a UUIDv4 primary key column named ``id``, generated in Python.

    Generated client-side (``default=uuid.uuid4``) rather than via a
    Postgres server default so it works identically against any SQLAlchemy
    dialect (including SQLite, for fast unit tests) without requiring the
    ``pgcrypto`` extension.
    """

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    """Adds ``created_at``/``updated_at`` columns, maintained in Python.

    Uses application-side UTC timestamps (not DB ``now()``) so behavior is
    identical across dialects and doesn't depend on DB server timezone
    configuration.
    """

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
