"""``Order``: one buyer's purchase — buyer details, payment status and total."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import MollieMode, OrderStatus, PaymentMethod

if TYPE_CHECKING:
    from app.models.event import Event
    from app.models.invoice import Invoice
    from app.models.ticket import Ticket


class Order(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A buyer's purchase. Created ``pending``/``pending_door`` at checkout (or
    ``paid`` for admin-issued manual orders) and settled or released later.

    Belongs to an Event, not a Show: all its Tickets must be for one Show, but
    that's enforced at checkout, not in the schema. ``language`` is the buyer's
    locale for emails and PDFs (validated in the schema, not a DB enum).
    ``total`` is the price x quantity sum at checkout — no service fee is ever
    added (see ``TicketType.service_fee_included``).
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
    mollie_payment_id: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True, index=True)
    """Mollie payment id (``tr_...``); the webhook's lookup key. Only set when a
    real Mollie payment was created.
    """
    mollie_mode: Mapped[MollieMode | None] = mapped_column(
        SAEnum(MollieMode, name="mollie_mode", native_enum=True, values_callable=lambda e: [m.value for m in e]),
        nullable=True,
    )
    """The Mollie mode at payment creation. The webhook reconciles with this, not
    the live ``EventConfig`` value, so an admin switching test/live mid-payment
    can't strand the order.
    """

    confirmation_email_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """Last *successful* confirmation-email send (failures are only in the
    audit log). Informational — the double-send guard is
    ``MarkOrderPaidResult.already_paid``.
    """

    event: Mapped["Event"] = relationship()
    tickets: Mapped[list["Ticket"]] = relationship(back_populates="order", cascade="all, delete-orphan")
    invoice: Mapped["Invoice | None"] = relationship(
        back_populates="order", uselist=False, cascade="all, delete-orphan"
    )
    """The Order's Invoice, once issued on payment."""
