"""Auth dependencies: admin session cookies, agent API keys, and the
scoping mechanism that keeps agent principals out of sensitive routes.

This module is the single place both auth paths converge, which is what
makes the scoping guarantee in :func:`require_admin` real rather than a
convention each route has to remember: any route depending on
``require_admin`` is structurally unreachable with an agent API key.
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
    """The authenticated caller of a request: a human admin or an AI agent.

    ``role`` is only meaningful for human principals. Agents don't carry a
    role at all — their access surface is defined entirely by which routes
    accept ``ActorType.AI_AGENT`` (i.e. all of them, by default) versus
    which routes require :func:`require_admin` (which agents can never
    satisfy). That's the actual enforcement mechanism, not a flag checked
    ad hoc per route.
    """

    actor_type: ActorType
    id: uuid.UUID
    name: str
    role: AdminRole | None = None


async def _load_admin_principal(request: Request, session: AsyncSession) -> Principal | None:
    """Resolve a ``Principal`` from the admin session cookie, if present and valid."""
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
    """Resolve a ``Principal`` from the agent API-key header, if present and valid.

    Rate-limited per client IP (see ``app.core.rate_limit``) to slow down
    key-guessing, and updates ``last_used_at`` as a side effect so
    revocation decisions in the backoffice can be informed by recency.
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
    """Resolve the authenticated principal from either auth path.

    Tries the admin session cookie first, then the agent API-key header.
    Raises 401 if neither yields a valid principal.
    """
    admin_principal = await _load_admin_principal(request, session)
    if admin_principal is not None:
        return admin_principal
    agent_principal = await _load_agent_principal(request, session)
    if agent_principal is not None:
        return agent_principal
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated.")


async def require_admin(principal: Principal = Depends(get_current_principal)) -> Principal:
    """Dependency for routes that must NEVER be reachable by an agent key.

    This is the concrete enforcement mechanism for PROJECT_BRIEF.md's
    agent-scoping requirement: an agent principal's ``actor_type`` is
    always ``ai_agent``, so it can never satisfy this check. Any route
    depending on this — admin user management, agent-account management,
    the audit log, and (in later milestones) payment/SMTP credential
    routes and financial data — is therefore unreachable with an agent API
    key as a matter of code structure, not documentation.
    """
    if principal.actor_type != ActorType.HUMAN or principal.role != AdminRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required.")
    return principal
