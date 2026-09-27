"""``Invoice``: the sequentially numbered invoice for a paid ``Order``.

Company/VAT details and line items are *snapshotted* at issuance, because
both are editable later (EventConfig, TicketType prices) and an issued
invoice must never change what it said. Buyer name/address and total are
read live from the Order: they're only ever changed by GDPR erasure, after
which the invoice deliberately shows the erased values. The PDF isn't
stored; it's re-rendered on request.
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
"""Zero-padding for the numeric part, e.g. ``"CP-00007"``."""


class Invoice(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One invoice per paid Order (``order_id`` is unique).

    ``event_id`` is denormalized so ``number`` can be unique per event at the DB
    level. ``number`` is allocated under an EventConfig row lock;
    ``number_prefix`` snapshots the prefix at issuance.
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
    """``[{"name", "quantity", "unit_price", "line_total"}]`` per ticket type,
    money as decimal strings. Built once at issuance, never recomputed.
    """

    order: Mapped["Order"] = relationship(back_populates="invoice")

    @property
    def formatted_number(self) -> str:
        """Printed number: prefix + zero-padded number, e.g. ``"CP-00007"``."""
        return f"{self.number_prefix}{self.number:0{_NUMBER_WIDTH}d}"
