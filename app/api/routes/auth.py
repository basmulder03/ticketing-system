"""Admin session login/logout and first-run admin setup.

Agents have no login: their API key header is the credential. The setup
routes replace needing env vars and a script to create the first admin;
``scripts/seed.py`` remains a local-dev convenience only.
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
    """Check email/password and set a signed session cookie.

    "No such user" and "wrong password" get the same 401 and take the same time
    (dummy hash), so emails can't be enumerated. Rate-limited per IP.
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
    """Clear the session cookie. Idempotent."""
    response.delete_cookie(ADMIN_SESSION_COOKIE_NAME)
    return {"status": "logged_out"}


@router.get("/me")
async def me(principal: Principal = Depends(get_current_principal)) -> PrincipalOut:
    """The current principal (admin or agent)."""
    return _to_principal_out(principal)


async def _admin_count(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(AdminUser))
    return result.scalar_one()


@router.get("/setup-required")
async def setup_required(session: AsyncSession = Depends(get_session)) -> SetupRequiredOut:
    """Whether no admin exists yet. Unauthenticated on purpose — nobody can log in yet."""
    return SetupRequiredOut(setup_required=(await _admin_count(session)) == 0)


@router.post("/setup", status_code=status.HTTP_201_CREATED)
async def setup(
    body: InitialAdminSetupRequest, response: Response, session: AsyncSession = Depends(get_session)
) -> PrincipalOut:
    """Create the very first admin and log them in. 409 forever once any admin
    exists; later accounts come from the admin-only user management.

    Two truly simultaneous calls could both succeed and create two admins.
    Accepted: the only realistic caller is the deployer right after first
    boot, and two legitimate admins isn't a security problem.
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
