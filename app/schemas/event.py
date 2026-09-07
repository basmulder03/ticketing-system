"""Pydantic request/response models for Event CRUD."""

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import PublishStatus

_SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"


class EventCreateRequest(BaseModel):
    """Body of ``POST /api/v1/events``."""

    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=255, pattern=_SLUG_PATTERN)
    description: str | None = None
    status: PublishStatus = PublishStatus.DRAFT
    sales_paused: bool = False


class EventUpdateRequest(BaseModel):
    """Body of ``PATCH /api/v1/events/{event_id}``.

    All fields optional; only fields explicitly present in the request body
    are applied (``exclude_unset``), so omitting a field never clobbers it.
    """

    name: str | None = Field(default=None, min_length=1, max_length=255)
    slug: str | None = Field(default=None, min_length=1, max_length=255, pattern=_SLUG_PATTERN)
    description: str | None = None
    status: PublishStatus | None = None
    sales_paused: bool | None = None


class EventOut(BaseModel):
    """Response shape for a single Event."""

    id: str
    name: str
    slug: str
    description: str | None
    status: PublishStatus
    sales_paused: bool
    created_at: datetime
    updated_at: datetime
