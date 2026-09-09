"""Request/response models for the door-scanning API (Milestone 7).

See ``app.services.scan`` for the outcome semantics ``ScanResponse`` mirrors
— this module only shapes the JSON contract, it never itself decides an
outcome.
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.enums import OrderStatus, ScanOutcome


class ScanRequest(BaseModel):
    """Body of ``POST /api/v1/shows/{show_id}/scan``.

    ``token`` is the raw string encoded in the scanned QR code (see
    ``app.core.qr_tokens``) — the frontend's camera-scanning code is
    responsible for extracting it from the decoded QR payload before
    posting here; this API never parses QR image data itself.
    """

    token: str = Field(min_length=1, max_length=2048)


class ScanResponse(BaseModel):
    """Result of one scan attempt — a single flat, discriminated shape the
    frontend switches on via ``outcome`` rather than five different
    response bodies, so it can render its low-light-usable pass/fail/unpaid
    UI states off one predictable field.

    Every field below ``outcome``/``message`` is optional and only
    populated for the outcomes where it's meaningful (documented per
    field) — a frontend should treat an absent field as "not applicable to
    this outcome", not as missing data.
    """

    outcome: ScanOutcome
    message: str
    """A short, human-readable summary of the outcome, generic enough to
    display directly at the door (e.g. on ``invalid`` it never distinguishes
    a tampered signature from an unknown ticket id — see
    ``ScanOutcome.INVALID``)."""

    ticket_id: str | None = None
    """Set for every outcome except ``invalid`` (an invalid/unrecognized
    code never resolves to a real ticket worth naming)."""

    ticket_type_name: str | None = None
    """Set for ``pass``, ``already_scanned``, and ``unpaid`` — the ticket
    category name (e.g. "Adult"), useful context for door staff."""

    buyer_name: str | None = None
    """The Order's buyer name — set for ``pass``, ``already_scanned``, and
    ``unpaid``. Deliberately excludes ``buyer_email``/``buyer_address``:
    door staff need enough to recognize who's in front of them, not the
    full financial/PII record (see ``app.api.routes.orders`` for that)."""

    order_id: str | None = None
    """Set for ``unpaid`` (and, informationally, ``pass``) so a frontend
    can deep-link an ADMIN-role user straight to the existing
    ``POST /api/v1/orders/{order_id}/mark-paid`` route without a second
    lookup."""

    order_status: OrderStatus | None = None
    """The Order's status at scan time — set for ``unpaid`` (always some
    non-``paid`` value; most commonly ``pending_door`` per the brief's own
    framing, but any non-``paid`` status is treated identically) and for
    ``pass`` (always ``paid``)."""

    amount_due: Decimal | None = None
    """Set for ``unpaid`` only — the Order's total (all of its tickets, not
    just this one), so a frontend can show door staff how much to collect
    before triggering the admin mark-as-paid action."""

    scanned_at: datetime | None = None
    """Set for ``pass`` (the scan that just happened) and
    ``already_scanned`` (when the ORIGINAL scan happened, so staff can
    judge how suspicious a repeat attempt looks)."""

    scanned_by_name: str | None = None
    """Who performed the (original, for ``already_scanned``; current, for
    ``pass``) scan — an ``AdminUser.email``, or ``None`` if that account has
    since been deleted (``Ticket.scanned_by`` is ``ON DELETE SET NULL``, see
    ``app.models.ticket.Ticket``)."""

    actual_show_id: str | None = None
    """Set for ``wrong_show`` only — the id of the Show this ticket
    actually belongs to, so a frontend can offer to redirect a confused
    attendee (or staff) rather than just saying "no"."""

    actual_show_label: str | None = None
    """Set for ``wrong_show`` only — a human-readable "Event name — date"
    label for :attr:`actual_show_id`, so the frontend doesn't need a
    separate lookup just to display which show the ticket is really for."""
