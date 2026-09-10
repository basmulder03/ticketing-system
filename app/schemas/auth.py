"""Pydantic request/response models for admin session login."""

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    """Body of ``POST /api/v1/auth/login``.

    ``email`` is deliberately plain ``str`` rather than Pydantic's
    ``EmailStr``: strict RFC/deliverability validation belongs on account
    *creation* (not built in this milestone — admin accounts are
    provisioned via ``scripts/seed.py`` for now), and would otherwise
    reject legitimate-looking local-dev addresses like the seeded
    ``admin@beacon.local`` (``.local`` is a reserved TLD that
    ``email-validator`` rejects outright). A malformed email at login
    simply fails the subsequent user lookup and returns 401, same as any
    other wrong credential.
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
    """Body of ``POST /api/v1/auth/setup`` — post-launch fix, per the
    user's NOTES: "The initial admin account is currently being created by
    setting the environment variables. I don't really like that flow."

    Same field constraints as ``app.schemas.admin_user.
    AdminUserCreateRequest`` (no ``role`` field here — the very first
    account is always ``AdminRole.ADMIN``, never ``scanner``)."""

    email: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=8)
