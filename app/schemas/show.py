"""Pydantic request/response models for Show CRUD."""

from datetime import date as date_type
from datetime import datetime
from datetime import time as time_type

from pydantic import BaseModel, Field

from app.models.enums import PublishStatus


class ShowCreateRequest(BaseModel):
    """Body of ``POST /api/v1/events/{event_id}/shows``."""

    date: date_type
    doors_time: time_type
    start_time: time_type
    venue_name: str = Field(min_length=1, max_length=255)
    venue_address: str = Field(min_length=1)
    capacity: int = Field(gt=0)
    status: PublishStatus = PublishStatus.DRAFT


class ShowUpdateRequest(BaseModel):
    """Body of ``PATCH /api/v1/events/{event_id}/shows/{show_id}``.

    All fields optional; only fields explicitly present are applied.
    """

    date: date_type | None = None
    doors_time: time_type | None = None
    start_time: time_type | None = None
    venue_name: str | None = Field(default=None, min_length=1, max_length=255)
    venue_address: str | None = Field(default=None, min_length=1)
    capacity: int | None = Field(default=None, gt=0)
    status: PublishStatus | None = None


class ShowOut(BaseModel):
    """Response shape for a single Show."""

    id: str
    event_id: str
    date: date_type
    doors_time: time_type
    start_time: time_type
    venue_name: str
    venue_address: str
    capacity: int
    status: PublishStatus
    created_at: datetime
    updated_at: datetime
