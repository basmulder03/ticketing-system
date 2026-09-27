"""``Event``: the top-level container for a single ticketed production."""

import secrets
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import PublishStatus

if TYPE_CHECKING:
    # Type-checking only: these modules import Event back.
    from app.models.event_config import EventConfig
    from app.models.show import Show
    from app.models.theme import Theme


class Event(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single production (e.g. one theatre run); owns one Theme and one
    EventConfig, and its Shows.

    - ``slug``: unique public URL identifier (``/e/<slug>``).
    - ``sales_paused``: event-wide kill switch for all its shows' sales.
    - ``preview_token``: unguessable token that makes a draft event (and all its
      shows) viewable at ``/preview/<token>``. Generated once, never rotated;
      shows have no token of their own.
    - ``is_default_event``: at most one event (partial unique index); ``/``
      redirects to it while it's also published.
    """

    __tablename__ = "events"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[PublishStatus] = mapped_column(
        SAEnum(PublishStatus, name="publish_status", native_enum=True, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=PublishStatus.DRAFT,
    )
    sales_paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    preview_token: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False, default=lambda: secrets.token_urlsafe(32)
    )
    is_default_event: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    config: Mapped["EventConfig | None"] = relationship(
        back_populates="event", uselist=False, cascade="all, delete-orphan"
    )
    theme: Mapped["Theme | None"] = relationship(
        back_populates="event", uselist=False, cascade="all, delete-orphan"
    )
    shows: Mapped[list["Show"]] = relationship(
        back_populates="event", cascade="all, delete-orphan", order_by="Show.date"
    )
