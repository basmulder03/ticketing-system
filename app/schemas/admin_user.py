"""Request/response models for backoffice account management. Passwords only
ever arrive as plaintext input and are never echoed back.
"""

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import AdminRole


class AdminUserCreateRequest(BaseModel):
    """Body of ``POST /api/v1/admin/admin-users``."""

    email: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=8)
    role: AdminRole = AdminRole.ADMIN


class AdminUserOut(BaseModel):
    """Response for listing/viewing admin-user accounts. Never includes the password hash."""

    id: str
    email: str
    role: AdminRole
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None


class AdminUserResetPasswordRequest(BaseModel):
    """``current_password`` is required only when an admin resets their own
    password (checked in the route), so it's optional here.
    """

    new_password: str = Field(min_length=8)
    current_password: str | None = Field(default=None, min_length=1)
