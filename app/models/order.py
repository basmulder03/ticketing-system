"""``Order``: one buyer's purchase — buyer/invoicing details, payment
lifecycle status, and the total charged.
"""

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import OrderStatus, PaymentMethod

if TYPE_CHECKING:
    from app.models.event import Event
    from app.models.ticket import Ticket


class Order(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A buyer's purchase, created in ``pending`` status at checkout
    (Milestone 2) and moved to ``paid``/``cancelled``/``expired`` by later
    milestones' payment handling (Mollie webhook: Milestone 3; manual
    mark-as-paid/door reconciliation: Milestone 6).

    ``event_id`` (not ``show_id``): per PROJECT_BRIEF.md's Core entities,
    "Order... belongs to an Event", because a single order's Tickets can
    span multiple TicketType rows as long as they all belong to the *same*
    Show — that "same show" constraint is enforced at checkout time (see
    ``app.services.checkout``), not by the schema; the specific Show(s) an
    Order touches are only reachable indirectly via its Tickets'
    ``TicketType.show`` relationship.

    ``buyer_address`` is free text (not structured fields), matching the
    brief's "address (for invoicing)" and mirroring how
    ``EventConfig.invoice_company_address`` is stored — the Milestone 5
    invoice PDF renders it as a single block.

    ``language`` is the buyer's chosen language code (e.g. ``"en"``/
    ``"nl"``) per the brief's Internationalization section ("their chosen
    language is stored on the Order and used for confirmation email,
    ticket PDF, and invoice PDF"). Validated/normalized to a supported
    locale at the schema layer (see ``app.schemas.order``), not
    constrained by a DB-level enum — adding a third supported language
    later is a schema-layer change only, no migration.

    ``total`` is the sum of its Tickets' TicketType prices at the moment of
    checkout (``Numeric(10, 2)``, never float — this is money). It does
    NOT yet include any Mollie service-fee surcharge amount: the concrete
    fee calculation is Milestone 3 scope (this milestone only has the
    ``TicketType.service_fee_included`` toggle from Milestone 1, not a fee
    percentage/amount anywhere yet) — see ``app.services.checkout`` for the
    current total computation, which is a plain price*quantity sum.
    """

    __tablename__ = "orders"

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    buyer_name: Mapped[str] = mapped_column(String(255), nullable=False)
    buyer_email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    buyer_address: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[OrderStatus] = mapped_column(
        SAEnum(OrderStatus, name="order_status", native_enum=True, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=OrderStatus.PENDING,
    )
    payment_method: Mapped[PaymentMethod] = mapped_column(
        SAEnum(
            PaymentMethod, name="payment_method", native_enum=True, values_callable=lambda e: [m.value for m in e]
        ),
        nullable=False,
    )
    total: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    language: Mapped[str] = mapped_column(String(10), nullable=False)

    event: Mapped["Event"] = relationship()
    tickets: Mapped[list["Ticket"]] = relationship(back_populates="order", cascade="all, delete-orphan")
