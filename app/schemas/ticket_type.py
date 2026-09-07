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
    """Body of ``PATCH /api/v1/ticket-types/{ticket_type_id}``.

    All fields optional; only fields explicitly present are applied.
    """

    name: str | None = Field(default=None, min_length=1, max_length=255)
    price: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    service_fee_included: bool | None = None
    quantity_available: int | None = Field(default=None, ge=0)


class TicketTypeOut(BaseModel):
    """Response shape for a single TicketType.

    ``remaining`` mirrors ``TicketType.remaining`` — see that property's
    docstring for why it currently always equals ``quantity_available``.
    """

    id: str
    show_id: str
    name: str
    price: Decimal
    service_fee_included: bool
    quantity_available: int
    remaining: int
    created_at: datetime
    updated_at: datetime
