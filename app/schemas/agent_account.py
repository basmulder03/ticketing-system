"""Pydantic request/response models for agent-account management."""

from datetime import datetime

from pydantic import BaseModel, Field


class AgentAccountCreateRequest(BaseModel):
    """Body of ``POST /api/v1/admin/agent-accounts``."""

    name: str = Field(min_length=1, max_length=255)


class AgentAccountCreatedOut(BaseModel):
    """Response returned only at creation time — the only moment the raw API key is visible."""

    id: str
    name: str
    key_prefix: str
    api_key: str


class AgentAccountOut(BaseModel):
    """Response for listing/viewing agent accounts. Never includes the raw API key."""

    id: str
    name: str
    key_prefix: str
    is_active: bool
    created_at: datetime
    last_used_at: datetime | None
