"""Shared model mixins. Immutable models (e.g. ``AuditLogEntry``) use only
the PK mixin plus their own ``created_at``, never ``TimestampMixin``."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column


def utcnow() -> datetime:
    """Current UTC time (column default)."""
    return datetime.now(UTC)


class UUIDPrimaryKeyMixin:
    """UUIDv4 ``id`` primary key, generated in Python (no ``pgcrypto`` needed)."""

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    """``created_at``/``updated_at`` in UTC, set by the app rather than DB
    ``now()`` so they don't depend on the server's timezone."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
