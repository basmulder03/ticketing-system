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
    render an order-confirmation page, and (Milestone 3) to know where to
    send the buyer next.

    ``payment_redirect_url`` is only set when this checkout just created a
    real payment needing an interstitial page before order-confirmation —
    Mollie's own hosted checkout, or (post-launch) the in-app demo-payment
    simulator's page (see ``app.services.checkout``'s payment-initiation
    dispatch) — the web layer (``app.web.routes.public_site``) must
    redirect the buyer there instead of straight to the order-confirmation
    page when it's present. It is ``None`` for ``door`` orders and for the
    preview-mode simulated-payment path (see that module's docstring), both
    of which go straight to order-confirmation. ``status`` may already be
    ``paid`` at this point for the simulated-preview path (no real payment
    involved) — it is NOT reliably ``paid`` yet for a real Mollie order or
    a still-pending demo order, since payment confirmation there only
    arrives later (the webhook, or the buyer's own action on the
    demo-payment page).
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
    """One requested ticket type + quantity line, same shape as
    :class:`CheckoutItem`."""

    ticket_type_id: str
    quantity: int = Field(gt=0, le=50)


class ManualOrderCreateRequest(BaseModel):
    """Body of ``POST /api/v1/shows/{show_id}/manual-orders`` — an
    admin-created Order for a buyer who never submitted any checkout
    request at all (see ``app.services.manual_order`` module docstring).

    ``buyer_email``/``buyer_address`` are optional, unlike
    :class:`CheckoutRequest`'s required versions of the same fields — per
    the user's NOTES, this exists specifically FOR "people without a
    computer or phone", who may have no email address to give at all.
    Left blank, ``app.services.manual_order.create_manual_order``
    synthesizes an `.invalid`-domain placeholder to satisfy ``Order.
    buyer_email``'s NOT NULL column, and the route skips confirmation-email
    dispatch entirely rather than trying to send to it.

    ``method_label``/``reason`` are the same free-text fields
    :class:`MarkOrderPaidRequest` already uses, for the same reason (see
    that schema's docstring) — this Order is settled to ``paid``
    immediately on creation (see ``app.services.manual_order.
    create_manual_order``), so the admin records how payment was actually
    collected (cash, comp, etc.) at the same time as creating it, rather
    than in a separate follow-up call.
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
        """Same lenient fallback as :meth:`CheckoutRequest._normalize_language`."""
        normalized = value.lower()
        return normalized if normalized in _SUPPORTED_LANGUAGES else "en"

    @model_validator(mode="after")
    def _check_total_quantity(self) -> "ManualOrderCreateRequest":
        total = sum(item.quantity for item in self.items)
        if total > _MAX_TICKETS_PER_ORDER:
            raise ValueError(f"An order may not request more than {_MAX_TICKETS_PER_ORDER} tickets in total.")
        return self


class MarkOrderPaidRequest(BaseModel):
    """Body of ``POST /api/v1/orders/{order_id}/mark-paid`` (Milestone 6).

    Both fields are free text rather than a fixed enum: PROJECT_BRIEF.md's
    Manual Payment Handling section only says staff must "select a
    reason/method" without prescribing a closed set of options, and the
    realistic set (cash, bank transfer, a separately-operated SumUp card
    terminal, a goodwill correction, etc.) is exactly the kind of
    per-deployment/per-event copy this app already treats as editable
    content elsewhere rather than hardcoded — see
    ``app.services.order_payment.mark_order_paid``'s own ``method_label``/
    ``reason`` parameters, which this schema maps onto directly. Any actual
    fixed choice list for ``method_label`` (e.g. a dropdown) is a
    frontend-theming/content-i18n concern, not enforced here.
    """

    method_label: str = Field(min_length=1, max_length=100)
    reason: str | None = Field(default=None, max_length=1000)


class MarkOrderPaidResponse(BaseModel):
    """Response of the manual mark-as-paid action.

    ``already_paid`` mirrors ``app.services.order_payment.
    MarkOrderPaidResult.already_paid`` directly: ``True`` means this call
    was a safe no-op (the Order was already ``paid`` — no new audit entry,
    no re-triggered ticket/invoice/email dispatch), so the caller can
    distinguish "just settled it" from "it was already settled" without
    treating either as an error — both are HTTP 200 successes, since
    clicking mark-as-paid twice must never surface as a failure per
    PROJECT_BRIEF.md's idempotency requirements.
    """

    already_paid: bool
    order: OrderOut


class ErasePiiRequest(BaseModel):
    """Body of ``POST /api/v1/orders/{order_id}/erase-pii`` (Milestone 9).

    ``confirm`` only matters for an Order that already has an issued
    Invoice: it is the explicit, opt-in acknowledgement PROJECT_BRIEF.md's
    GDPR-conscious requirement calls for when a deletion request runs up
    against invoice-retention law (see ``app.services.gdpr`` for the full
    reasoning). Defaulting to ``False`` means an accidental/careless call
    against an invoiced order is refused (409), not silently erased; an
    Order with no Invoice erases immediately regardless of this field.
    """

    confirm: bool = False


class ErasePiiResponse(BaseModel):
    """Response of a successful buyer-PII erasure."""

    order: OrderOut
    had_invoice: bool
    """``True`` if this Order had an issued Invoice at the time of erasure
    (meaning the caller had to pass ``confirm: true`` to reach this
    success response) — surfaced back to the caller/UI as a reminder that
    the underlying Invoice record itself was intentionally left untouched,
    per this app's accounting-retention stance (see ``app.services.gdpr``).
    """
