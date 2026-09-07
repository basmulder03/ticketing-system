"""Pydantic request/response models for the public checkout flow
(Milestone 2: create a pending Order; payment processing itself is
Milestone 3)."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.enums import OrderStatus, PaymentMethod

_MAX_TICKETS_PER_ORDER = 50
"""Caps the SUM of quantities across every line item, not just each item
individually (``CheckoutItem.quantity`` already caps a single line at 50).
Without this, a single request could ask for up to 20 x 50 = 1000 tickets
in one call — each one an INSERT inside the same row-locked transaction
``app.services.stock.reserve_stock`` opens, needlessly extending lock hold
time on the TicketType row(s) it touches at exactly the sales-live moment
PROJECT_BRIEF.md flags as the highest-contention scenario."""

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
    Event's real ``Event.preview_token`` AND the Event or Show is still
    draft, the checkout bypasses the draft-publish gate (see
    ``app.services.checkout``) so a draft event's buy flow can be
    exercised end-to-end before it's published — per PROJECT_BRIEF.md's
    Draft & Preview section. Once fully published, ``sales_paused``/
    ``sales_live_at`` are unconditionally enforced regardless of any token
    presented — a preview link is not a standing bypass of the manual
    sales kill-switch or embargo once the event is actually live.
    """

    buyer_name: str = Field(min_length=1, max_length=255)
    buyer_email: str = Field(max_length=255, pattern=_EMAIL_PATTERN)
    buyer_address: str = Field(min_length=1, max_length=1000)
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

    @model_validator(mode="after")
    def _check_total_quantity(self) -> "CheckoutRequest":
        """Reject a request whose per-item quantities already fit each
        item's own cap but sum to more than :data:`_MAX_TICKETS_PER_ORDER`
        across the whole order."""
        total = sum(item.quantity for item in self.items)
        if total > _MAX_TICKETS_PER_ORDER:
            raise ValueError(f"An order may not request more than {_MAX_TICKETS_PER_ORDER} tickets in total.")
        return self


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
