"""Pydantic request/response models for TicketType CRUD."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class TicketTypeCreateRequest(BaseModel):
    """Body of ``POST /api/v1/shows/{show_id}/ticket-types``."""

    name: str = Field(min_length=1, max_length=255)
    price: Decimal = Field(ge=0, decimal_places=2)
    service_fee_included: bool = True
    quantity_available: int = Field(ge=0)


class TicketTypeUpdateRequest(BaseModel):
    """PATCH body; only fields present are changed."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    price: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    service_fee_included: bool | None = None
    quantity_available: int | None = Field(default=None, ge=0)


class TicketTypeOut(BaseModel):
    """One ticket type; ``remaining`` is live stock."""

    id: str
    show_id: str
    name: str
    price: Decimal
    service_fee_included: bool
    quantity_available: int
    remaining: int
    created_at: datetime
    updated_at: datetime
