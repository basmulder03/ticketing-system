"""Management of human backoffice accounts (admin/scanner roles). Admin-only.

An admin can't deactivate their own account. That also guarantees at least
one active admin always remains: the caller is an active admin and stays
one, so no separate "last admin" check is needed.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, require_admin
from app.api.routes._utils import commit_or_conflict, parse_uuid_or_404
from app.core.security import hash_password, verify_password
from app.db.session import get_session
from app.models.admin_user import AdminUser
from app.schemas.admin_user import (
    AdminUserCreateRequest,
    AdminUserOut,
    AdminUserResetPasswordRequest,
)
from app.services.audit import record_audit_entry

router = APIRouter(prefix="/api/v1/admin/admin-users", tags=["admin", "admin-users"])


def _to_out(admin: AdminUser) -> AdminUserOut:
    return AdminUserOut(
        id=str(admin.id),
        email=admin.email,
        role=admin.role,
        is_active=admin.is_active,
        created_at=admin.created_at,
        last_login_at=admin.last_login_at,
    )


async def _get_admin_user_or_404(session: AsyncSession, admin_user_id: str) -> AdminUser:
    """Resolve ``admin_user_id`` or 404."""
    parsed_id = parse_uuid_or_404(admin_user_id, detail="Admin user not found.")
    admin = await session.get(AdminUser, parsed_id)
    if admin is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admin user not found.")
    return admin


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_admin_user(
    body: AdminUserCreateRequest,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> AdminUserOut:
    """Create an account. Email is lowercased to match the login lookup.

    The explicit uniqueness pre-check is load-bearing: the audit entry needs
    the id, so we flush — and an IntegrityError at flush would escape
    ``commit_or_conflict`` as a 500. The final ``commit_or_conflict`` still covers
    a genuine race. The password is hashed immediately and never logged.
    """
    email = body.email.lower()
    existing = await session.execute(select(AdminUser).where(AdminUser.email == email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="An admin user with this email already exists."
        )

    admin = AdminUser(
        email=email,
        hashed_password=hash_password(body.password),
        role=body.role,
    )
    session.add(admin)
    await session.flush()
    await record_audit_entry(
        session,
        principal,
        action="admin_user.created",
        target_type="AdminUser",
        target_id=str(admin.id),
        detail={"email": admin.email, "role": admin.role.value},
    )
    await commit_or_conflict(session, detail="An admin user with this email already exists.")
    return _to_out(admin)


@router.get("")
async def list_admin_users(
    principal: Principal = Depends(require_admin), session: AsyncSession = Depends(get_session)
) -> list[AdminUserOut]:
    """All accounts (never password hashes)."""
    result = await session.execute(select(AdminUser).order_by(AdminUser.created_at))
    return [_to_out(admin) for admin in result.scalars().all()]


@router.post("/{admin_user_id}/deactivate")
async def deactivate_admin_user(
    admin_user_id: str,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> AdminUserOut:
    """Deactivate an account. Takes effect immediately: ``is_active`` is checked
    at login and on every session request. Idempotent.

    Refuses the caller's own account. Compare parsed ``UUID`` objects, never the
    raw path string: a string comparison let an uppercased id bypass this guard.
    """
    admin = await _get_admin_user_or_404(session, admin_user_id)
    if admin.id == principal.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You cannot deactivate your own admin account.",
        )

    if admin.is_active:
        admin.is_active = False
        await record_audit_entry(
            session,
            principal,
            action="admin_user.deactivated",
            target_type="AdminUser",
            target_id=str(admin.id),
            detail={"email": admin.email},
        )
        await session.commit()
    return _to_out(admin)


@router.post("/{admin_user_id}/reactivate")
async def reactivate_admin_user(
    admin_user_id: str,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> AdminUserOut:
    """Reactivate an account. No self-restriction (it only grants access). Idempotent."""
    admin = await _get_admin_user_or_404(session, admin_user_id)
    if not admin.is_active:
        admin.is_active = True
        await record_audit_entry(
            session,
            principal,
            action="admin_user.reactivated",
            target_type="AdminUser",
            target_id=str(admin.id),
            detail={"email": admin.email},
        )
        await session.commit()
    return _to_out(admin)


@router.post("/{admin_user_id}/reset-password")
async def reset_admin_user_password(
    admin_user_id: str,
    body: AdminUserResetPasswordRequest,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> AdminUserOut:
    """Set a new password.

    Resetting someone else's needs no current password (that's the point of an
    assisted reset). Resetting your *own* requires ``current_password``, so a
    stolen session can't take over the account. As in
    :func:`deactivate_admin_user`, "own" is decided by comparing parsed UUIDs — a
    raw string comparison once let an uppercased id skip the check.
    """
    admin = await _get_admin_user_or_404(session, admin_user_id)
    is_self_reset = admin.id == principal.id

    if is_self_reset and (
        body.current_password is None or not verify_password(body.current_password, admin.hashed_password)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is required and must be correct to reset your own password.",
        )

    admin.hashed_password = hash_password(body.new_password)
    await record_audit_entry(
        session,
        principal,
        action="admin_user.password_reset",
        target_type="AdminUser",
        target_id=str(admin.id),
        detail={"email": admin.email, "self_service": is_self_reset},
    )
    await session.commit()
    return _to_out(admin)
