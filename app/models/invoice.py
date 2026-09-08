"""``Invoice``: the sequentially-numbered financial document issued for an
``Order`` once it's paid (Milestone 5), per PROJECT_BRIEF.md's Invoicing
section and Core entities ("Invoice (belongs to an Order: sequential
number, PDF)").

Design decision — snapshot, not live-reference (read this before touching
any field below): PROJECT_BRIEF.md doesn't say explicitly whether an
Invoice should store its own copy of the company/VAT/line-item data or
always re-derive it from the current ``EventConfig``/``TicketType`` rows at
render time. This module snapshots everything needed to reconstruct the
invoice EXACTLY as issued, for two concrete reasons:

1. ``EventConfig.invoice_company_name``/``invoice_company_address``/
   ``invoice_company_vat_number`` are plain editable admin fields (see
   ``app.models.event_config.EventConfig``) with no history of their own.
   An admin correcting a VAT number next month must not silently rewrite
   the VAT number on every invoice already issued and (likely) already
   sent to a buyer's accountant — that would make a previously-issued
   legal/accounting document lie about what it said when it was issued.
2. ``TicketType.price``/``name`` are editable via
   ``PATCH /api/v1/ticket-types/{id}`` (see ``app.api.routes.ticket_types``)
   at any time, including after tickets against that TicketType have
   already been sold and invoiced. Re-deriving line items from the live
   TicketType row at render time would silently change a past invoice's
   unit prices if an admin edits pricing later — the same problem as (1).

``Order.buyer_name``/``buyer_address``/``total`` are NOT snapshotted here
(read straight off the related ``Order`` at render time instead): unlike
EventConfig/TicketType, there is no route anywhere in this app that edits
an existing Order's buyer details or total after checkout, so those values
are already immutable in practice — duplicating them here would be pure
redundancy (DRY) with no snapshot benefit. If a future milestone ever adds
order editing, that decision should be revisited alongside this one.

The rendered PDF itself is never persisted as a blob — mirroring
``app.services.ticket_pdf``'s existing pattern of rendering fresh on every
request from stored row data. This is safe specifically because nothing
this Invoice snapshots (or reads live off ``Order``) can change after
issuance, so re-rendering is always byte-for-byte reproducible.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin, utcnow

if TYPE_CHECKING:
    from app.models.order import Order

_NUMBER_WIDTH = 5
"""Zero-padding width for the numeric portion of a formatted invoice number
(e.g. prefix ``"CP-"`` + number ``7`` -> ``"CP-00007"``). Five digits
comfortably covers any realistic single-event ticket volume for this app's
target scale (a self-hosted small/medium venue) without ever needing to
widen the format; see :attr:`Invoice.formatted_number`."""


class Invoice(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One issued invoice, 1:1 with a paid ``Order`` (``order_id`` is
    unique — an Order gets exactly one Invoice, ever; see
    ``app.services.invoicing.issue_invoice_for_order`` for the idempotent
    issuance logic that enforces this in practice).

    ``event_id`` is denormalized off ``Order.event_id`` (not reached only
    via ``order.event_id``) purely so the per-event uniqueness constraint
    on ``number`` (``uq_invoice_event_number``) can be expressed directly
    at the DB layer without a join — the same denormalization tradeoff
    ``Order.event_id`` itself already makes relative to ``Show``/
    ``TicketType`` (see that column's docstring).

    ``number`` is the raw per-event sequential integer (starts at 1,
    allocated by :func:`app.services.invoicing._allocate_invoice_number`
    under an ``EventConfig`` row lock — see that function's docstring for
    the concurrency discipline). ``number_prefix`` snapshots
    ``EventConfig.invoice_number_prefix`` *as it was at issuance* (empty
    string if the event had no prefix configured), so a later prefix change
    in the backoffice never rewrites how an already-issued invoice number
    displays. :attr:`formatted_number` combines the two into the actual
    displayed/printed invoice number.
    """

    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("event_id", "number", name="uq_invoice_event_number"),
    )

    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    number_prefix: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    # --- Snapshot of EventConfig's company/VAT block at issue time ---
    company_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    company_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    company_vat_number: Mapped[str | None] = mapped_column(String(50), nullable=True)

    line_items: Mapped[list[dict[str, object]]] = mapped_column(JSON, nullable=False)
    """One entry per distinct ``TicketType`` purchased on the Order, each
    shaped ``{"name": str, "quantity": int, "unit_price": str, "line_total":
    str}`` — ``unit_price``/``line_total`` stored as decimal-string (never
    float, matching this project's money-handling rule; JSON has no
    ``Decimal`` type) so re-rendering is exact. Built once, at issuance, by
    ``app.services.invoicing._build_line_items``; never recomputed from
    live ``TicketType`` rows afterward (see this module's docstring)."""

    order: Mapped["Order"] = relationship(back_populates="invoice")

    @property
    def formatted_number(self) -> str:
        """The displayed/printed invoice number, e.g. ``"CP-00007"`` for
        ``number_prefix="CP-"``, ``number=7``. Zero-padded to
        :data:`_NUMBER_WIDTH` digits; a prefix of ``""`` yields a bare
        zero-padded number (e.g. ``"00007"``)."""
        return f"{self.number_prefix}{self.number:0{_NUMBER_WIDTH}d}"
