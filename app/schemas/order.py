"""Pydantic request/response models for the public checkout flow
(Milestone 2: create a pending Order; payment processing itself is
Milestone 3)."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

from app.models.enums import OrderStatus, PaymentMethod

_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
"""Mirrors ``app.schemas.event_config``'s lightweight email-shape check
(not ``EmailStr``/``email-validator``) for the same reason documented
there: a self-hosted deployment may reasonably see non-public-TLD or
internal addresses, and this is a shape check, not a deliverability
guarantee."""

_SUPPORTED_LANGUAGES = {"en", "nl"}
"""Per PROJECT_BRIEF.md's Internationalization section: "English and Dutch
supported from the first milestone"."""


class CheckoutItem(BaseModel):
    """One requested ticket type + quantity line in a checkout request."""

    ticket_type_id: str
    quantity: int = Field(gt=0, le=50)


class CheckoutRequest(BaseModel):
    """Body of ``POST /api/v1/public/checkout``.

    ``preview_token`` is optional: when present and it matches the target
    Event's real ``Event.preview_token``, the checkout bypasses the
    draft/sales-live/sales-paused gates (see ``app.services.checkout``) so
    a draft event's buy flow can be exercised end-to-end before it's
    published — per PROJECT_BRIEF.md's Draft & Preview section. A
    mismatched or absent token means the normal published-only gates
    apply.
    """

    buyer_name: str = Field(min_length=1, max_length=255)
    buyer_email: str = Field(max_length=255, pattern=_EMAIL_PATTERN)
    buyer_address: str = Field(min_length=1)
    language: str = Field(min_length=2, max_length=10)
    payment_method: PaymentMethod
    items: list[CheckoutItem] = Field(min_length=1, max_length=20)
    preview_token: str | None = None

    @field_validator("language")
    @classmethod
    def _normalize_language(cls, value: str) -> str:
        """Lowercase and fall back to English for any value that isn't
        currently a supported locale, rather than hard-rejecting the whole
        checkout over a garbled/unsupported language code."""
        normalized = value.lower()
        return normalized if normalized in _SUPPORTED_LANGUAGES else "en"


class TicketOut(BaseModel):
    """One issued Ticket in an order confirmation response.

    ``qr_token`` is always ``None`` at this milestone — populated by
    Milestone 4's QR-signing work (see ``app.models.ticket.Ticket``
    docstring for why the row already exists now, ahead of that).
    """

    id: str
    ticket_type_id: str
    ticket_type_name: str
    price: Decimal
    qr_token: str | None


class OrderOut(BaseModel):
    """Response of a successful checkout — enough for `frontend-theming` to
    render an order-confirmation page (Milestone 2 scope; payment status is
    always ``pending``/``pending_door`` at this point, since Milestone 3
    adds actual payment processing)."""

    id: str
    event_id: str
    status: OrderStatus
    payment_method: PaymentMethod
    buyer_name: str
    buyer_email: str
    buyer_address: str
    language: str
    total: Decimal
    tickets: list[TicketOut]
    created_at: datetime
