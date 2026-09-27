"""Pydantic request/response models for Show CRUD."""

from datetime import date as date_type
from datetime import datetime
from datetime import time as time_type

from pydantic import BaseModel, Field, model_validator

from app.models.enums import PublishStatus

DOORS_AFTER_START_MESSAGE = "Doors time must be before the start time."


class ShowCreateRequest(BaseModel):
    """Body of ``POST /api/v1/events/{event_id}/shows``."""

    date: date_type
    doors_time: time_type
    start_time: time_type
    venue_name: str = Field(min_length=1, max_length=255)
    venue_address: str = Field(min_length=1)
    capacity: int = Field(gt=0)
    status: PublishStatus = PublishStatus.DRAFT

    @model_validator(mode="after")
    def _check_doors_before_start(self) -> "ShowCreateRequest":
        """Doors and start are always same-day (see ``app.models.show.Show``), so
        this is a plain time comparison.
        """
        if self.doors_time >= self.start_time:
            raise ValueError(DOORS_AFTER_START_MESSAGE)
        return self


class ShowUpdateRequest(BaseModel):
    """PATCH body; only fields present are changed."""

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
