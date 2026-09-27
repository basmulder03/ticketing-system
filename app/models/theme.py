"""``Theme``: an event's branding — colors, logo, background, font and optional custom CSS."""

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
    """Per-event branding (1:1 with ``Event``).

    Images are stored as paths under the uploads directory, never as bytes.
    ``custom_css`` holds only already-sanitized CSS and applies to the public
    event page only — ticket and invoice PDFs always use the fixed fields.
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
