"""Admin-only management of ``AdminUser`` accounts (the human backoffice
accounts, ``role`` = ``admin`` or ``scanner``).

Until now the only way to create/deactivate/reset one of these accounts was
to hand-edit the database via ``scripts/seed.py`` — this module is the
first real API surface for it, closely mirroring
``app.api.routes.agent_accounts``'s shape (create/list/revoke) since that's
this codebase's closest precedent for "admin manages a category of
accounts". Every route here depends on :func:`app.api.deps.require_admin`,
so — like agent-account management — it is structurally unreachable with
an agent API key or a ``scanner``-role session.

Two safety properties are enforced here that are easy to get wrong:

* **Self-deactivation guard** (:func:`deactivate_admin_user`): an admin can
  never deactivate their own account through this route. Without this, a
  solo admin (or an admin acting alone) could lock themselves out of the
  backoffice entirely via a single misclick, with no way back in short of
  ``scripts/seed.py``/direct DB access.

* **Last-admin safety is a *consequence* of the self-deactivation guard,
  not a separate check.** Every route in this module requires an
  ``admin``-role, ``is_active`` principal (that's what ``require_admin``
  means). Deactivation only ever targets a *different* account than the
  caller (enforced by the guard above), so the calling admin themselves
  always remains active immediately after the operation completes — the
  active-admin count can never drop to zero via this route. A dedicated
  "don't deactivate the last admin" check would therefore be redundant
  with the self-deactivation guard; it's deliberately not implemented
  separately, but this reasoning is spelled out here because it's a real
  judgment call, not an oversight.
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
    """Look up an ``AdminUser`` by path-segment id, or raise a 404.

    Shared by every route below that takes ``admin_user_id`` in the path,
    so the "malformed UUID looks like 404" behavior (see
    ``parse_uuid_or_404``) and the not-found message stay consistent.
    """
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
    """Create a new backoffice ``AdminUser`` account.

    Email is lowercased before storage/comparison, matching the login
    route's lookup (``AdminUser.email == body.email.lower()`` in
    ``app.api.routes.auth``) and ``scripts/seed.py``'s existing
    convention — otherwise an account created here with mixed-case email
    could silently never be able to log in.

    Uniqueness is checked explicitly with a pre-insert ``SELECT`` (matching
    ``app.api.routes.events.create_event``'s established convention) rather
    than relying solely on the DB's unique constraint on ``email`` —
    load-bearing, not a stylistic choice: the audit entry below needs
    ``admin.id``, which only exists after a ``flush()``, and a flush sends
    the INSERT to Postgres immediately rather than deferring it to
    ``commit()`` — so a bare insert-then-flush would raise
    ``IntegrityError`` right there at ``flush()`` time, ESCAPING
    :func:`commit_or_conflict`'s try/except entirely (that only wraps the
    later ``commit()`` call, never reached in that failure path) and
    surfacing as an unhandled 500 instead of a clean 409. Reproduced and
    confirmed directly before this fix. ``commit_or_conflict`` is still
    kept on the final commit below as defense in depth against a genuine
    race between this check and the insert (two concurrent creates for the
    same email) — the explicit pre-check is what makes the common,
    non-racing case return a clean 409, not a crash. The plaintext
    password is hashed immediately and never stored, logged, returned, or
    included in the audit detail.
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
    """List every ``AdminUser`` account (never includes the password hash)."""
    result = await session.execute(select(AdminUser).order_by(AdminUser.created_at))
    return [_to_out(admin) for admin in result.scalars().all()]


@router.post("/{admin_user_id}/deactivate")
async def deactivate_admin_user(
    admin_user_id: str,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> AdminUserOut:
    """Deactivate an ``AdminUser`` account, immediately revoking its access.

    ``is_active`` is checked both at login (``app.api.routes.auth.login``)
    and on every subsequent request that resolves a session cookie
    (``app.api.deps._load_admin_principal``) — both already existed before
    this route was added, so deactivating an account here takes effect
    immediately: it blocks new logins AND invalidates any session the
    account is already holding, with no separate revocation step needed.

    Refuses to deactivate the caller's own account (see module docstring
    for the self-deactivation/last-admin reasoning). Idempotent: deactivating
    an already-inactive account is a no-op (no duplicate audit entry) rather
    than an error, matching ``agent_accounts.revoke_agent_account``'s
    idempotency.
    """
    if admin_user_id == str(principal.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You cannot deactivate your own admin account.",
        )

    admin = await _get_admin_user_or_404(session, admin_user_id)
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
    """Reactivate a previously-deactivated ``AdminUser`` account.

    No self/last-admin restriction applies here — reactivation only ever
    grants access back, it can't be used to lock anyone out. Idempotent
    like :func:`deactivate_admin_user`.
    """
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
    """Set a new password for an ``AdminUser`` account.

    Two legitimate cases, deliberately handled differently:

    * **Resetting someone ELSE's password** (``admin_user_id != principal.id``):
      no current-password check — that's the entire point of an
      admin-assisted reset (e.g. the target forgot their password and can't
      supply it). Any active admin can do this to any other account.

    * **Resetting your OWN password** (``admin_user_id == principal.id``):
      ``body.current_password`` is required and must verify against the
      account's existing hash. Without this, a hijacked/stolen session
      cookie could silently rotate the password to something only the
      attacker knows — a quiet full account takeover — with no additional
      proof the caller is the legitimate account holder. Requiring the
      current password here closes that gap the same way most account
      systems do for a self-service password change.

    Never logs the old or new password; the audit entry records only which
    account was affected.
    """
    admin = await _get_admin_user_or_404(session, admin_user_id)
    is_self_reset = admin_user_id == str(principal.id)

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
