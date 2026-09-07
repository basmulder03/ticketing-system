"""Admin-only agent-account management: create/list/revoke API keys.

This is a minimal Milestone-0 slice of what PROJECT_BRIEF.md calls "Agent
account management (create/revoke API keys, scope content-management
access)" — the full CRUD/UI surface lands in Milestone 1 alongside Event
CRUD. It's built now specifically to give the audit-log mechanism a real,
demonstrable write path (per this milestone's scope), and because
agent-key auth (``app.api.deps``) needs at least one way to create the
accounts it authenticates.

Every route here depends on ``require_admin``, so it is one of the routes
an agent API key can never reach — content management, not admin user
management, is what agents get in later milestones.
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, require_admin
from app.core.security import generate_agent_api_key
from app.db.session import get_session
from app.models.agent_account import AgentAccount
from app.schemas.agent_account import (
    AgentAccountCreatedOut,
    AgentAccountCreateRequest,
    AgentAccountOut,
)
from app.services.audit import record_audit_entry

router = APIRouter(prefix="/api/v1/admin/agent-accounts", tags=["admin", "agent-accounts"])


def _to_out(agent: AgentAccount) -> AgentAccountOut:
    return AgentAccountOut(
        id=str(agent.id),
        name=agent.name,
        key_prefix=agent.key_prefix,
        is_active=agent.is_active,
        created_at=agent.created_at,
        last_used_at=agent.last_used_at,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_agent_account(
    body: AgentAccountCreateRequest,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> AgentAccountCreatedOut:
    """Create a new named agent account and return its API key.

    The raw API key is only ever present in this one response — it is
    never stored and cannot be retrieved again; a lost key must be revoked
    and replaced with a new account.
    """
    existing = await session.execute(select(AgentAccount).where(AgentAccount.name == body.name))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="An agent account with this name already exists."
        )

    raw_key, key_hash, key_prefix = generate_agent_api_key()
    agent = AgentAccount(
        name=body.name, key_hash=key_hash, key_prefix=key_prefix, created_by_admin_id=principal.id
    )
    session.add(agent)
    await session.flush()
    await record_audit_entry(
        session,
        principal,
        action="agent_account.create",
        target_type="AgentAccount",
        target_id=str(agent.id),
        detail={"name": agent.name},
    )
    await session.commit()
    return AgentAccountCreatedOut(id=str(agent.id), name=agent.name, key_prefix=agent.key_prefix, api_key=raw_key)


@router.get("")
async def list_agent_accounts(
    principal: Principal = Depends(require_admin), session: AsyncSession = Depends(get_session)
) -> list[AgentAccountOut]:
    """List all agent accounts (never includes the raw API key)."""
    result = await session.execute(select(AgentAccount).order_by(AgentAccount.created_at))
    return [_to_out(agent) for agent in result.scalars().all()]


@router.post("/{agent_id}/revoke")
async def revoke_agent_account(
    agent_id: str,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> AgentAccountOut:
    """Revoke a single agent's API key without affecting any other account.

    Idempotent: revoking an already-revoked account is a no-op (no
    duplicate audit entry) rather than an error.
    """
    try:
        parsed_id = uuid.UUID(agent_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent account not found.") from None

    agent = await session.get(AgentAccount, parsed_id)
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent account not found.")

    if agent.is_active:
        agent.revoked_at = datetime.now(UTC)
        await record_audit_entry(
            session,
            principal,
            action="agent_account.revoke",
            target_type="AgentAccount",
            target_id=str(agent.id),
            detail={"name": agent.name},
        )
        await session.commit()
    return _to_out(agent)
