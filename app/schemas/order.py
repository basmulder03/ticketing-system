"""Request/response models for checkout, manual orders and order actions."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.enums import OrderStatus, PaymentMethod

_MAX_TICKETS_PER_ORDER = 50
"""Cap on the *sum* of quantities per order. Each ticket is an INSERT inside
the stock-locking transaction, so huge orders would hold the lock longer
at exactly the busiest moment (sales going live).
"""

_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
"""Shape check only — see ``app.schemas.event_config._EMAIL_PATTERN``."""

_SUPPORTED_LANGUAGES = {"en", "nl"}


class CheckoutItem(BaseModel):
    """One ticket type + quantity line."""

    ticket_type_id: str
    quantity: int = Field(gt=0, le=50)


class CheckoutRequest(BaseModel):
    """Public checkout body.

    A valid ``preview_token`` lets a *draft* event's checkout run end to end.
    It never bypasses ``sales_paused``/``sales_live_at`` once the event is
    published.
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
        """Fall back to English rather than rejecting an unsupported language."""
        normalized = value.lower()
        return normalized if normalized in _SUPPORTED_LANGUAGES else "en"

    @model_validator(mode="after")
    def _check_total_quantity(self) -> "CheckoutRequest":
        """Enforce :data:`_MAX_TICKETS_PER_ORDER` across all lines."""
        total = sum(item.quantity for item in self.items)
        if total > _MAX_TICKETS_PER_ORDER:
            raise ValueError(f"An order may not request more than {_MAX_TICKETS_PER_ORDER} tickets in total.")
        return self


class TicketOut(BaseModel):
    """One ticket. ``qr_token`` is ``None`` until the order is paid."""

    id: str
    ticket_type_id: str
    ticket_type_name: str
    price: Decimal
    qr_token: str | None


class OrderOut(BaseModel):
    """One order.

    ``payment_redirect_url`` is set when the buyer must visit a payment page
    first (Mollie, or the demo-payment page); otherwise go straight to order
    confirmation. ``status`` is only reliably ``paid`` here for the preview
    sandbox path — real payments settle later.
    """

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
    payment_redirect_url: str | None = None


class ManualOrderItem(BaseModel):
    """Same shape as :class:`CheckoutItem`."""

    ticket_type_id: str
    quantity: int = Field(gt=0, le=50)


class ManualOrderCreateRequest(BaseModel):
    """Admin-issued order for a buyer who never checked out
    (``app.services.manual_order``).

    Email and address are optional — the buyer may have neither. Without an
    email, a ``.invalid`` placeholder is stored and no confirmation is sent.
    ``method_label``/``reason`` record how payment was collected, since the
    order is settled immediately.
    """

    buyer_name: str = Field(min_length=1, max_length=255)
    buyer_email: str | None = Field(default=None, max_length=255, pattern=_EMAIL_PATTERN)
    buyer_address: str | None = Field(default=None, max_length=1000)
    language: str = Field(default="en")
    items: list[ManualOrderItem] = Field(min_length=1, max_length=20)
    method_label: str = Field(min_length=1, max_length=100)
    reason: str | None = Field(default=None, max_length=1000)

    @field_validator("language")
    @classmethod
    def _normalize_language(cls, value: str) -> str:
        """Same fallback as :meth:`CheckoutRequest._normalize_language`."""
        normalized = value.lower()
        return normalized if normalized in _SUPPORTED_LANGUAGES else "en"

    @model_validator(mode="after")
    def _check_total_quantity(self) -> "ManualOrderCreateRequest":
        total = sum(item.quantity for item in self.items)
        if total > _MAX_TICKETS_PER_ORDER:
            raise ValueError(f"An order may not request more than {_MAX_TICKETS_PER_ORDER} tickets in total.")
        return self


class MarkOrderPaidRequest(BaseModel):
    """Mark-as-paid body. Free text rather than an enum: real methods vary by
    venue (cash, bank transfer, card terminal, correction...).
    """

    method_label: str = Field(min_length=1, max_length=100)
    reason: str | None = Field(default=None, max_length=1000)


class MarkOrderPaidResponse(BaseModel):
    """``already_paid=True`` means the call was a no-op (no new audit entry, email
    or invoice). Both outcomes are 200 so a double click never errors.
    """

    already_paid: bool
    order: OrderOut


class ErasePiiRequest(BaseModel):
    """``confirm`` is required only for an invoiced order (invoice retention);
    without it that order is refused with 409. Un-invoiced orders erase
    immediately.
    """

    confirm: bool = False


class ErasePiiResponse(BaseModel):
    """Response of a successful buyer-PII erasure."""

    order: OrderOut
    had_invoice: bool
    """``True`` if the order had an invoice, which is deliberately kept intact."""
