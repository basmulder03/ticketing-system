"""``AdminUser``: a human backoffice user authenticated via session login."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import AdminRole


class AdminUser(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A human backoffice user. ``scanner`` accounts can only scan tickets;
    ``admin`` accounts get everything, including ``require_admin`` routes.
    Passwords are argon2-hashed and must never be logged.
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
