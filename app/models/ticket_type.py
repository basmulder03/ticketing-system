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

    # Lets `_sold_count` below be a plain (non-`Mapped[...]`) annotated
    # attribute without SQLAlchemy's declarative mapper trying to interpret
    # it as a mapped column — see that attribute's comment. `ClassVar[...]`
    # would achieve the same for SQLAlchemy but mypy then forbids assigning
    # to it via `self.` (as `attach_sold_count` below does), so this is the
    # documented SQLAlchemy escape hatch instead.
    __allow_unmapped__ = True

    show_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    service_fee_included: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    quantity_available: Mapped[int] = mapped_column(Integer, nullable=False)

    show: Mapped["Show"] = relationship(back_populates="ticket_types")

    # Transient (non-persisted) per-instance state — deliberately a plain
    # class attribute, NOT a `Mapped[...]`/`mapped_column`, so SQLAlchemy's
    # declarative mapper never treats it as a DB column. Holds the live
    # count of non-cancelled/non-expired Ticket rows for this TicketType,
    # attached by `attach_sold_count` (see `app.services.stock`) after a
    # fetch — see `remaining`'s docstring for why this two-step shape
    # exists instead of a synchronous DB query inside the property.
    _sold_count: int | None = None

    @property
    def remaining(self) -> int:
        """Tickets still available for purchase: ``quantity_available``
        minus the live count of Ticket rows (across all non-cancelled,
        non-expired Orders) referencing this TicketType — see
        ``app.services.stock`` for the real query and
        ``app.models.enums.OrderStatus`` for which statuses "release"
        stock.

        This is a plain synchronous property, so it CANNOT itself run the
        async DB query needed to count sold tickets — callers must call
        ``attach_sold_count`` (typically via
        ``app.services.stock.attach_remaining``) on this instance first.
        If never attached, this conservatively falls back to
        ``quantity_available`` (as if nothing has sold yet) rather than
        raising, so a caller that forgets to attach a count fails safe
        (shows full stock) rather than crashing — every read path that
        needs a *correct* live number (backoffice TicketType routes, the
        public landing-page routes) calls ``attach_remaining`` before
        building its response; the checkout flow's actual stock check
        does its own row-locked count directly (see
        ``app.services.stock.reserve_stock``) and never relies on this
        property at all.
        """
        sold = self._sold_count if self._sold_count is not None else 0
        return max(self.quantity_available - sold, 0)

    def attach_sold_count(self, sold_count: int) -> None:
        """Attach the live sold count so subsequent reads of ``remaining``
        reflect it. See ``remaining``'s docstring."""
        self._sold_count = sold_count
