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
    """A purchasable ticket category under a ``Show`` (e.g. "Adult", "Child").

    ``service_fee_included`` is the "price includes Mollie service fee vs.
    price excludes it (fee added at checkout)" toggle from the brief's Show
    & Ticket Management section. ``True`` means ``price`` already includes
    the fee (nothing added at checkout); ``False`` means the fee is added on
    top at checkout. The actual fee *calculation* (what the fee amount is,
    how it's split out on the order) is Milestone 3 payments scope — this
    milestone only stores the toggle.

    ``price`` uses ``Numeric(10, 2)`` (fixed-point), never ``Float``, since
    this is money.

    Content-type data per the brief's AI/Agent Access section — routes here
    use ``require_admin_or_agent``, not ``require_admin``.
    """

    __tablename__ = "ticket_types"

    show_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    service_fee_included: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    quantity_available: Mapped[int] = mapped_column(Integer, nullable=False)

    show: Mapped["Show"] = relationship(back_populates="ticket_types")

    @property
    def remaining(self) -> int:
        """Tickets still available for purchase.

        Currently always equals ``quantity_available``: the ``Order``/
        ``Ticket`` models that would let us subtract sold/reserved stock
        don't exist until Milestone 3+. Written as a real computed property
        (not a TODO stub) specifically so wiring in the real stock math
        later is a one-line change to this property's body, not a new
        call-site to hunt down across the codebase — every caller (API
        responses, the public stock display in Milestone 2) should already
        be reading ``remaining``, never ``quantity_available``, wherever
        "how many can still be bought" is the actual question.
        """
        return self.quantity_available
