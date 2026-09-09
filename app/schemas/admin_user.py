"""Pydantic request/response models for admin-user (backoffice account) management.

Mirrors ``app.schemas.agent_account``'s shape: a create-request schema, an
``*Out`` schema for list/detail responses, and a couple of narrow
single-field request schemas for the password-reset/self-reset actions.
None of these schemas ever carry ``hashed_password`` — password material
only ever flows in as plaintext on a request body, is hashed immediately by
the route (see ``app.core.security.hash_password``), and is never echoed
back.
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
    """Body of ``POST /api/v1/admin/admin-users/{admin_user_id}/reset-password``.

    ``current_password`` is only required (and only checked) when the
    calling admin is resetting their OWN password — see the route
    docstring for why that distinction exists. It is optional here at the
    schema level because a different-account reset legitimately omits it.
    """

    new_password: str = Field(min_length=8)
    current_password: str | None = Field(default=None, min_length=1)
