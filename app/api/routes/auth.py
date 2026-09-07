"""Admin session-login routes.

Only the admin (session) auth path lives here. Agent API-key auth has no
"login" endpoint by design — the key itself, sent on the
``X-Agent-Api-Key`` header of each request, is the credential (see
``app.api.deps``).
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, get_current_principal
from app.core.config import get_settings
from app.core.rate_limit import login_rate_limiter, rate_limit_dependency
from app.core.security import ADMIN_SESSION_COOKIE_NAME, create_session_token, verify_password
from app.db.session import get_session
from app.models.admin_user import AdminUser
from app.models.enums import ActorType
from app.schemas.auth import LoginRequest, PrincipalOut
from app.services.audit import record_audit_entry

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _to_principal_out(principal: Principal) -> PrincipalOut:
    return PrincipalOut(
        actor_type=principal.actor_type.value,
        id=str(principal.id),
        name=principal.name,
        role=principal.role.value if principal.role else None,
    )


@router.post("/login", dependencies=[Depends(rate_limit_dependency(login_rate_limiter))])
async def login(
    body: LoginRequest, response: Response, session: AsyncSession = Depends(get_session)
) -> PrincipalOut:
    """Authenticate an admin user by email/password and set a signed session cookie.

    Returns a generic 401 for both "no such user" and "wrong password" so
    the response can't be used to enumerate registered admin emails.
    Rate-limited per client IP (see ``app.core.rate_limit``).
    """
    result = await session.execute(select(AdminUser).where(AdminUser.email == body.email.lower()))
    admin = result.scalar_one_or_none()
    if admin is None or not admin.is_active or not verify_password(body.password, admin.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password.")

    admin.last_login_at = datetime.now(UTC)
    principal = Principal(actor_type=ActorType.HUMAN, id=admin.id, name=admin.email, role=admin.role)
    await record_audit_entry(
        session, principal, action="admin_user.login", target_type="AdminUser", target_id=str(admin.id)
    )
    await session.commit()

    settings = get_settings()
    token = create_session_token(str(admin.id))
    response.set_cookie(
        key=ADMIN_SESSION_COOKIE_NAME,
        value=token,
        max_age=settings.session_timeout_minutes * 60,
        httponly=True,
        samesite="lax",
        secure=settings.app_env != "development",
    )
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
