"""Request/response models for admin login and first-run setup."""

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    """Login body. ``email`` is a plain string, not ``EmailStr``: dev addresses
    like ``admin@beacon.local`` use a reserved TLD that ``email-validator``
    rejects, and a malformed email just fails the lookup with a 401 anyway.
    """

    email: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1)


class PrincipalOut(BaseModel):
    """The authenticated principal (admin or agent), returned by login/me."""

    actor_type: str
    id: str
    name: str
    role: str | None = None


class SetupRequiredOut(BaseModel):
    """Response of ``GET /api/v1/auth/setup-required``."""

    setup_required: bool


class InitialAdminSetupRequest(BaseModel):
    """First-run admin account (see ``app.api.routes.auth.setup``). Same
    constraints as ``AdminUserCreateRequest``; the role is always ``admin``.
    """

    email: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=8)
