"""``AdminUser``: a human backoffice user authenticated via session login."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import AdminRole


class AdminUser(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A human backoffice user.

    Passwords are hashed with argon2 (see ``app.core.security``) — the
    ``hashed_password`` column never holds plaintext, and plaintext
    passwords must never be logged. ``role`` gates access: ``scanner``
    accounts are limited to the door-scanning endpoint (added in a later
    milestone), ``admin`` accounts get full backoffice access, including
    routes gated by ``app.api.deps.require_admin`` (agent-account
    management, the audit log, and — in later milestones — payment/SMTP
    credential configuration).
    """

    __tablename__ = "admin_users"

    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[AdminRole] = mapped_column(
        SAEnum(AdminRole, name="admin_role", native_enum=True, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=AdminRole.ADMIN,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
