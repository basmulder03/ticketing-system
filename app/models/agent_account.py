"""``AgentAccount``: a named AI-agent integration authenticated via API key."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class AgentAccount(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A named AI-agent integration authenticated by API key.

    Only the key's SHA-256 hash is stored (the raw key is shown once), so a DB
    dump can't be used to authenticate. ``key_prefix`` is for display only.
    Scope limits are enforced in ``app.api.deps``, not here.
    """

    __tablename__ = "agent_accounts"

    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_active(self) -> bool:
        """``True`` unless the key has been revoked."""
        return self.revoked_at is None
