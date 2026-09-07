"""``Event``: the top-level container for a single ticketed production."""

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import PublishStatus

if TYPE_CHECKING:
    # Import cycle avoided at runtime (these modules import Event back);
    # only needed so mypy can resolve the string forward-refs below.
    from app.models.event_config import EventConfig
    from app.models.show import Show


class Event(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single event/production (e.g. one theatre run), the top-level
    container PROJECT_BRIEF.md's Core entities describe as owning one Theme
    and one EventConfig.

    ``slug`` is the URL-safe, unique public identifier used in landing-page
    routes from Milestone 2 onward (e.g. ``/events/<slug>``) — validated for
    shape at the schema layer (see ``app.schemas.event``), enforced unique
    here at the DB layer.

    ``status`` is the draft/published flag from PROJECT_BRIEF.md's Draft &
    Preview section; the shareable unguessable-preview-link mechanism that
    makes a draft viewable is Milestone 2 scope and doesn't require any
    additional column on this model (an unguessable link can be derived from
    ``id`` at that point, e.g. via a signed token).

    ``sales_paused`` is the "manual pause/resume sales override" from the
    brief's Show & Ticket Management section. Modeled at the Event level
    (one flag applying across every Show under this event) rather than
    per-Show: the brief doesn't scope the override to a specific show, and a
    single event-wide switch is the simplest correct reading (KISS) — an
    operator pausing sales during an incident almost certainly means "stop
    all sales for this event right now," not "let me remember to toggle
    every show individually."

    Theme (colors, logo, custom CSS) is explicitly deferred to Milestone 1.5
    per the brief's Build order — no ``theme_id`` column exists yet.
    Backfilling a nullable FK to a ``themes`` table in a later migration is a
    one-line addition; adding a placeholder UUID column now with no table to
    reference it would only invite an untyped, unenforced "foreign key in
    name only," so it's left out entirely until the Theme model exists.
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

    config: Mapped["EventConfig | None"] = relationship(
        back_populates="event", uselist=False, cascade="all, delete-orphan"
    )
    shows: Mapped[list["Show"]] = relationship(
        back_populates="event", cascade="all, delete-orphan", order_by="Show.date"
    )
