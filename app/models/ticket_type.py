"""``TicketType``: a purchasable ticket category under a ``Show``."""

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.show import Show


class TicketType(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A purchasable ticket category under a ``Show``. ``price`` is fixed-point.

    ``service_fee_included`` is stored and editable, but no service fee is
    calculated anywhere yet — totals are always price x quantity.
    """

    __tablename__ = "ticket_types"

    # Lets `_sold_count` be a plain annotated attribute rather than a mapped
    # column (ClassVar would upset mypy on `self._sold_count = ...`).
    __allow_unmapped__ = True

    show_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    service_fee_included: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    quantity_available: Mapped[int] = mapped_column(Integer, nullable=False)

    show: Mapped["Show"] = relationship(back_populates="ticket_types")

    # Transient, not persisted: set by attach_sold_count after a fetch.
    _sold_count: int | None = None

    @property
    def remaining(self) -> int:
        """``quantity_available`` minus the attached sold count.

        Callers must call ``attach_sold_count`` (via
        ``app.services.stock.attach_remaining``) first; without it this falls back
        to full stock rather than raising. Checkout never relies on this — it does
        its own row-locked count.
        """
        sold = self._sold_count if self._sold_count is not None else 0
        return max(self.quantity_available - sold, 0)

    def attach_sold_count(self, sold_count: int) -> None:
        """Set the live sold count used by :attr:`remaining`."""
        self._sold_count = sold_count
