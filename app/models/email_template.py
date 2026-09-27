"""``EmailTemplate``: editable email subject/body per event, language and email type."""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.event import Event


class EmailTemplate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Editable subject/body for one ``(event, language, template_type)``.

    Without a row, ``app.services.email_render`` falls back to built-in
    defaults, so a send never fails for lack of customization. ``language`` and
    ``template_type`` are plain strings (validated in the schema) so new values
    need no migration. ``body`` is an HTML snippet with ``{{placeholder}}``
    substitution only (``app.services.email_placeholders``); the email's
    shell and branding are built around it.
    """

    __tablename__ = "email_templates"
    __table_args__ = (
        UniqueConstraint("event_id", "language", "template_type", name="uq_email_template_event_lang_type"),
    )

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    language: Mapped[str] = mapped_column(String(10), nullable=False)
    template_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    event: Mapped["Event"] = relationship()
