"""Auth dependencies for admin sessions and agent API keys.

Both paths resolve to one :class:`Principal`, so ``require_admin`` makes a
route structurally unreachable by agents rather than by convention.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.rate_limit import agent_auth_rate_limiter
from app.core.security import ADMIN_SESSION_COOKIE_NAME, hash_api_key, verify_session_token
from app.db.session import get_session
from app.models.admin_user import AdminUser
from app.models.agent_account import AgentAccount
from app.models.enums import ActorType, AdminRole

AGENT_API_KEY_HEADER = "X-Agent-Api-Key"


@dataclass(frozen=True)
class Principal:
    """The authenticated caller: a human admin or an AI agent. ``role`` applies
    only to humans; agents are scoped by which dependencies admit them.
    """

    actor_type: ActorType
    id: uuid.UUID
    name: str
    role: AdminRole | None = None


async def _load_admin_principal(request: Request, session: AsyncSession) -> Principal | None:
    """Principal from a valid admin session cookie, if any."""
    token = request.cookies.get(ADMIN_SESSION_COOKIE_NAME)
    if not token:
        return None
    settings = get_settings()
    admin_user_id = verify_session_token(token, max_age_seconds=settings.session_timeout_minutes * 60)
    if admin_user_id is None:
        return None
    try:
        admin_id = uuid.UUID(admin_user_id)
    except ValueError:
        return None
    admin = await session.get(AdminUser, admin_id)
    if admin is None or not admin.is_active:
        return None
    return Principal(actor_type=ActorType.HUMAN, id=admin.id, name=admin.email, role=admin.role)


async def _load_agent_principal(request: Request, session: AsyncSession) -> Principal | None:
    """Principal from a valid agent API key header, if any. Rate-limited per IP;
    updates ``last_used_at``.
    """
    raw_key = request.headers.get(AGENT_API_KEY_HEADER)
    if not raw_key:
        return None
    agent_auth_rate_limiter.check(request.client.host if request.client else "unknown")
    key_hash = hash_api_key(raw_key)
    result = await session.execute(select(AgentAccount).where(AgentAccount.key_hash == key_hash))
    agent = result.scalar_one_or_none()
    if agent is None or not agent.is_active:
        return None
    agent.last_used_at = datetime.now(UTC)
    await session.commit()
    return Principal(actor_type=ActorType.AI_AGENT, id=agent.id, name=agent.name)


async def get_current_principal(
    request: Request, session: AsyncSession = Depends(get_session)
) -> Principal:
    """Session cookie first, then API key; 401 if neither is valid."""
    admin_principal = await _load_admin_principal(request, session)
    if admin_principal is not None:
        return admin_principal
    agent_principal = await _load_agent_principal(request, session)
    if agent_principal is not None:
        return agent_principal
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated.")


async def require_admin(principal: Principal = Depends(get_current_principal)) -> Principal:
    """Admin-role humans only — agents and scanners get 403. Use for anything
    financial, credential-related, or user/agent management.
    """
    if principal.actor_type != ActorType.HUMAN or principal.role != AdminRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required.")
    return principal


async def require_scanner_or_admin(principal: Principal = Depends(get_current_principal)) -> Principal:
    """Admin or scanner humans, never agents. Only for scanning routes: payment
    actions (mark-paid, invoices, orders) must stay ``require_admin`` so a scanner
    can see an unpaid ticket but never settle it.
    """
    if principal.actor_type != ActorType.HUMAN or principal.role not in (AdminRole.ADMIN, AdminRole.SCANNER):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Scanner or admin access required.")
    return principal


async def require_admin_or_agent(principal: Principal = Depends(get_current_principal)) -> Principal:
    """Admin-role humans or agents, never scanners. For content (events, shows,
    ticket types, themes, email templates) — never EventConfig.
    """
    if principal.actor_type == ActorType.AI_AGENT:
        return principal
    if principal.actor_type == ActorType.HUMAN and principal.role == AdminRole.ADMIN:
        return principal
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin or agent access required.")
