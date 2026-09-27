"""``Ticket``: one scannable ticket; its existence reserves stock."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.order import Order
    from app.models.ticket_type import TicketType


class Ticket(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One physical ticket, created at checkout inside the stock-locking
    transaction. The rows *are* the reservation: remaining stock is
    ``quantity_available`` minus tickets on non-cancelled/expired orders.

    ``qr_token`` is set when the order is paid (unique; many NULLs are fine).
    ``scanned_by`` uses ``SET NULL`` so deleting a scanner account keeps scan
    history. ``ticket_type_id`` uses ``RESTRICT``: a ticket type with sold
    tickets can't be deleted.
    """

    __tablename__ = "tickets"

    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ticket_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ticket_types.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    qr_token: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scanned_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )

    order: Mapped["Order"] = relationship(back_populates="tickets")
    ticket_type: Mapped["TicketType"] = relationship()
