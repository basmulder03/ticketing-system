"""``Show``: a single performance/date under an ``Event``."""

import uuid
from datetime import date as date_type
from datetime import time as time_type
from typing import TYPE_CHECKING

from sqlalchemy import Date, ForeignKey, Integer, String, Text, Time
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import PublishStatus

if TYPE_CHECKING:
    from app.models.event import Event
    from app.models.ticket_type import TicketType


class Show(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One performance of an ``Event``.

    ``date``/``doors_time``/``start_time`` are separate because doors and start
    are always on the same day. Drafts are previewed via the parent event's
    ``preview_token``, which shows every show regardless of its own status.
    """

    __tablename__ = "shows"

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    date: Mapped[date_type] = mapped_column(Date, nullable=False)
    doors_time: Mapped[time_type] = mapped_column(Time, nullable=False)
    start_time: Mapped[time_type] = mapped_column(Time, nullable=False)
    venue_name: Mapped[str] = mapped_column(String(255), nullable=False)
    venue_address: Mapped[str] = mapped_column(Text, nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[PublishStatus] = mapped_column(
        SAEnum(PublishStatus, name="publish_status", native_enum=True, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=PublishStatus.DRAFT,
    )

    event: Mapped["Event"] = relationship(back_populates="shows")
    ticket_types: Mapped[list["TicketType"]] = relationship(back_populates="show", cascade="all, delete-orphan")
