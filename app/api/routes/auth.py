"""Admin session-login routes.

Only the admin (session) auth path lives here. Agent API-key auth has no
"login" endpoint by design — the key itself, sent on the
``X-Agent-Api-Key`` header of each request, is the credential (see
``app.api.deps``).

Post-launch fix: also the initial-admin-account setup routes
(``setup_required``/``setup``) — per the user's NOTES: "The initial admin
account is currently being created by setting the environment variables.
I don't really like that flow." ``scripts/seed.py``'s env-var-driven
``AdminUser`` creation remains exactly as it was (still explicitly a local
dev convenience — see its own module docstring), but a fresh, real
deployment now has a real in-app way to get its first admin account
instead of needing shell access to set ``SEED_ADMIN_EMAIL``/
``SEED_ADMIN_PASSWORD`` and run a script.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, get_current_principal
from app.core.config import get_settings
from app.core.rate_limit import login_rate_limiter, rate_limit_dependency
from app.core.security import (
    ADMIN_SESSION_COOKIE_NAME,
    create_session_token,
    hash_password,
    verify_password_or_dummy,
)
from app.db.session import get_session
from app.models.admin_user import AdminUser
from app.models.enums import ActorType, AdminRole
from app.schemas.auth import InitialAdminSetupRequest, LoginRequest, PrincipalOut, SetupRequiredOut
from app.services.audit import record_audit_entry

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _to_principal_out(principal: Principal) -> PrincipalOut:
    return PrincipalOut(
        actor_type=principal.actor_type.value,
        id=str(principal.id),
        name=principal.name,
        role=principal.role.value if principal.role else None,
    )


def _set_session_cookie(response: Response, admin_id: str) -> None:
    settings = get_settings()
    token = create_session_token(admin_id)
    response.set_cookie(
        key=ADMIN_SESSION_COOKIE_NAME,
        value=token,
        max_age=settings.session_timeout_minutes * 60,
        httponly=True,
        samesite="lax",
        secure=settings.app_env != "development",
    )


@router.post("/login", dependencies=[Depends(rate_limit_dependency(login_rate_limiter))])
async def login(
    body: LoginRequest, response: Response, session: AsyncSession = Depends(get_session)
) -> PrincipalOut:
    """Authenticate an admin user by email/password and set a signed session cookie.

    Returns a generic 401 for both "no such user" and "wrong password" so
    the response can't be used to enumerate registered admin emails — the
    password hash check runs unconditionally (against a dummy hash when the
    account doesn't exist/isn't active) so the response also can't be
    distinguished by timing.
    Rate-limited per client IP (see ``app.core.rate_limit``).
    """
    result = await session.execute(select(AdminUser).where(AdminUser.email == body.email.lower()))
    admin = result.scalar_one_or_none()
    password_ok = verify_password_or_dummy(
        body.password, admin.hashed_password if admin is not None else None
    )
    if admin is None or not admin.is_active or not password_ok:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password.")

    admin.last_login_at = datetime.now(UTC)
    principal = Principal(actor_type=ActorType.HUMAN, id=admin.id, name=admin.email, role=admin.role)
    await record_audit_entry(
        session, principal, action="admin_user.login", target_type="AdminUser", target_id=str(admin.id)
    )
    await session.commit()
    _set_session_cookie(response, str(admin.id))
    return _to_principal_out(principal)


@router.post("/logout")
async def logout(response: Response) -> dict[str, str]:
    """Clear the admin session cookie. Idempotent — safe to call when not logged in."""
    response.delete_cookie(ADMIN_SESSION_COOKIE_NAME)
    return {"status": "logged_out"}


@router.get("/me")
async def me(principal: Principal = Depends(get_current_principal)) -> PrincipalOut:
    """Return the currently authenticated principal (admin or agent)."""
    return _to_principal_out(principal)


async def _admin_count(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(AdminUser))
    return result.scalar_one()


@router.get("/setup-required")
async def setup_required(session: AsyncSession = Depends(get_session)) -> SetupRequiredOut:
    """Whether this deployment still needs its very first ``AdminUser``
    account — deliberately unauthenticated (there's no admin to
    authenticate as yet) so the login/setup pages can check it before
    anyone has signed in at all. See :func:`setup`."""
    return SetupRequiredOut(setup_required=(await _admin_count(session)) == 0)


@router.post("/setup", status_code=status.HTTP_201_CREATED)
async def setup(
    body: InitialAdminSetupRequest, response: Response, session: AsyncSession = Depends(get_session)
) -> PrincipalOut:
    """Create this deployment's very first ``AdminUser`` account and log
    them in immediately — post-launch fix, see this module's docstring.

    NOT a general "create an admin" endpoint (that's the admin-only
    ``POST /api/v1/admin/admin-users``, see ``app.api.routes.
    admin_users``): only reachable while zero ``AdminUser`` rows exist at
    all. Once the very first one is created, this permanently 409s —
    every account after that goes through the normal admin-only
    admin-user-management flow instead, same "someone who is already an
    admin invites/creates the next one" model most self-hosted apps use.

    Race note: two truly concurrent calls could both observe zero admins
    and both succeed, creating two initial accounts instead of one —
    accepted here rather than adding a Postgres advisory lock, since the
    realistic "attacker" is the deployer's own browser in the few seconds
    right after their own ``docker compose up``, and the worst outcome
    (two legitimate admin accounts instead of one) is not a security
    problem, unlike ``app.api.routes.admin_users.create_admin_user``'s
    email-uniqueness race, which the pre-check + ``commit_or_conflict``
    combination there already tolerates the same way.
    """
    if (await _admin_count(session)) > 0:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Setup has already been completed.")

    email = body.email.lower()
    admin = AdminUser(email=email, hashed_password=hash_password(body.password), role=AdminRole.ADMIN)
    session.add(admin)
    await session.flush()

    principal = Principal(actor_type=ActorType.HUMAN, id=admin.id, name=admin.email, role=admin.role)
    await record_audit_entry(
        session,
        principal,
        action="admin_user.initial_setup",
        target_type="AdminUser",
        target_id=str(admin.id),
        detail={"email": admin.email},
    )
    await session.commit()
    _set_session_cookie(response, str(admin.id))
    return _to_principal_out(principal)
