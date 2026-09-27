"""Request/response models for the ticket-scanning API (outcomes decided in ``app.services.scan``)."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.enums import OrderStatus, ScanOutcome


class ScanRequest(BaseModel):
    """``token`` is the string decoded from the QR code by the scanning frontend."""

    token: str = Field(min_length=1, max_length=2048)


class ScanResponse(BaseModel):
    """One flat shape for every outcome; the frontend switches on ``outcome``.
    Fields not meaningful for an outcome are ``None``.
    """

    outcome: ScanOutcome
    message: str
    """Door-displayable summary; never reveals *why* a code is invalid."""

    ticket_id: str | None = None
    """Every outcome except ``invalid``."""

    ticket_type_name: str | None = None
    """``pass``, ``already_scanned``, ``unpaid``."""

    buyer_name: str | None = None
    """``pass``, ``already_scanned``, ``unpaid``. Deliberately no email/address:
    door staff only need to recognize the person.
    """

    order_id: str | None = None
    """``unpaid`` (and ``pass``), so an admin can go straight to mark-as-paid."""

    order_status: OrderStatus | None = None
    """``unpaid`` (any non-paid status) and ``pass`` (always ``paid``)."""

    amount_due: Decimal | None = None
    """``unpaid`` only: the whole order's total, to collect at the door."""

    scanned_at: datetime | None = None
    """``pass`` (now) and ``already_scanned`` (the original scan)."""

    scanned_by_name: str | None = None
    """Scanner's email, or ``None`` if that account was deleted."""

    actual_show_id: str | None = None
    """``wrong_show`` only: the show this ticket is really for."""

    actual_show_label: str | None = None
    """``wrong_show`` only: "Event name — date" for :attr:`actual_show_id`."""
