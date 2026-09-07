"""``Theme``: an Event's branding — fixed colors/logo/background/font plus
an optional sandboxed custom-CSS override (Milestone 1.5, per
PROJECT_BRIEF.md's Event & Theming section).
"""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import PublishStatus, ThemeFont

if TYPE_CHECKING:
    from app.models.event import Event


class Theme(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Per-event branding (1:1 with ``Event``, mirrors ``EventConfig``'s
    cascade/uniqueness pattern: one row per event, deleted along with it).

    ``primary_color``/``secondary_color``/``accent_color`` are hex strings
    (``#rrggbb``, validated at the schema layer — see ``app.schemas.theme``)
    — the brief's "fixed fields by default". ``logo_path``/
    ``background_image_path`` store only a relative filesystem path under
    the configured uploads directory (see ``app.services.theme_images``),
    never raw image bytes in the database — images are served back out via
    a mounted static-files route (see ``app.main``).

    ``custom_css`` stores ONLY the already-sanitized CSS (see
    ``app.core.css_sanitizer.sanitize_custom_css``) — the raw, pre-sanitize
    input a caller submitted is never persisted anywhere. Per the brief,
    custom CSS is scoped to the public landing page only; it must never be
    applied to ticket/invoice PDF rendering (those always use the fixed
    fields for guaranteed AA compliance) — enforced by whichever
    PDF-rendering code lands in later milestones simply never reading this
    column, not by anything here.

    ``status`` reuses the shared ``PublishStatus`` enum (see its docstring)
    for the same draft/published semantics as ``Event``/``Show``.

    Content-type data per the brief's AI/Agent Access section ("theme
    fields" are explicitly agent-accessible) — routes use
    ``require_admin_or_agent``, not ``require_admin``.
    """

    __tablename__ = "themes"

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE"), unique=True, nullable=False
    )

    primary_color: Mapped[str] = mapped_column(String(7), nullable=False, default="#1a1a1a")
    secondary_color: Mapped[str] = mapped_column(String(7), nullable=False, default="#ffffff")
    accent_color: Mapped[str] = mapped_column(String(7), nullable=False, default="#c9a227")

    logo_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    background_image_path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    font_choice: Mapped[ThemeFont] = mapped_column(
        SAEnum(ThemeFont, name="theme_font", native_enum=True, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=ThemeFont.SYSTEM_SANS,
    )

    custom_css: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[PublishStatus] = mapped_column(
        SAEnum(PublishStatus, name="publish_status", native_enum=True, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=PublishStatus.DRAFT,
    )

    event: Mapped["Event"] = relationship(back_populates="theme")
