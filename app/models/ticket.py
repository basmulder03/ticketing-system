"""``Ticket``: one physical/scannable ticket, created at checkout time and
reserving stock against its ``TicketType`` until its ``Order`` is
cancelled/expired.
"""

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
    """One physical ticket: belongs to an ``Order`` and a ``TicketType``.

    Design decision (Milestone 2, affects Milestones 3-4): PROJECT_BRIEF.md
    lists Ticket as having a "unique HMAC-signed QR token, scanned_at,
    scanned_by", but QR signing is explicitly Milestone 4 scope and
    scanning is Milestone 7 scope. Rather than deferring Ticket row
    *creation* until Milestone 4 (which would need a separate
    reservation/line-item concept to track "how many tickets does this
    pending order hold" in the meantime), Ticket rows are created here, at
    checkout time (Milestone 2) — one row per physical ticket purchased,
    created immediately inside the same row-locked transaction that
    verifies and decrements stock (see ``app.services.stock`` /
    ``app.services.checkout``). This is what actually reserves stock: a
    TicketType's live remaining count is simply "quantity_available minus
    the count of Ticket rows whose Order isn't cancelled/expired" (see
    ``app.services.stock.sold_counts_for_ticket_types``), with no separate
    reservation table needed. It also turns:
      - Milestone 4 into "sign a QR token for each already-existing Ticket
        row and render/email it" (populating the nullable ``qr_token``
        below), rather than "create the Ticket rows a second time"; and
      - Milestone 7 into "populate ``scanned_at``/``scanned_by`` on an
        existing row" during a scan.

    ``qr_token`` is nullable (populated by Milestone 4) but unique once
    set — Postgres unique indexes already treat multiple NULLs as distinct
    from one another, so many not-yet-signed tickets coexisting with a
    ``NULL`` token is not a constraint violation.

    ``scanned_at``/``scanned_by`` are both nullable (populated by
    Milestone 7's scanning flow). ``scanned_by`` is a nullable FK to
    ``AdminUser`` (the Scanner-role account that performed the scan) with
    ``ON DELETE SET NULL`` so deleting a scanner account later doesn't
    cascade-delete historical ticket/scan records.

    ``ticket_type_id`` uses ``ON DELETE RESTRICT`` (not CASCADE): once a
    TicketType has sold tickets, deleting it out from under real Orders
    would silently orphan/corrupt purchased tickets — the backoffice
    TicketType-delete route must handle the resulting integrity error as a
    clear conflict response, not let it surface as an unhandled 500 (see
    ``app.api.routes._utils.commit_or_conflict``).
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
