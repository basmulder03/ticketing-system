"""``AgentAccount``: a named AI-agent integration authenticated via API key."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class AgentAccount(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A named AI-agent/skill integration, authenticated via API key.

    The raw API key is generated once at creation time (see
    ``app.core.security.generate_agent_api_key``) and shown to the caller
    exactly once — only its SHA-256 hash (``key_hash``) is persisted, so a
    stolen database dump alone cannot be used to authenticate as the
    agent. ``key_prefix`` is stored purely for display in the backoffice
    (so an admin can tell accounts apart without the full secret).

    Keys are individually revocable via ``revoked_at`` without affecting
    any other agent or human admin. Every AgentAccount is a distinct,
    named integration — never a shared/anonymous key — per
    PROJECT_BRIEF.md's AI/Agent Access requirement. Scope enforcement
    (agents cannot touch payment/SMTP credentials, admin user management,
    financial data, or the audit log) is implemented in
    ``app.api.deps.require_admin``, not on this model — this table only
    records identity and revocation state.
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
