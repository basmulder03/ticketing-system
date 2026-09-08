"""``EmailTemplate``: per Event + language + email type, admin/agent-editable
subject and body content (Milestone 4), per PROJECT_BRIEF.md's Ticket
Generation & Delivery section: "Email content is editable per event/language
in the backoffice, not hardcoded".
"""

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
    """One event's editable subject/body for one email ``template_type`` in
    one ``language``. At most one row per ``(event_id, language,
    template_type)`` triple (enforced by a unique constraint) — an
    event/type/language combination with no row here falls back to a
    built-in hardcoded default (see
    ``app.services.email_render.DEFAULT_SUBJECT``/``DEFAULT_BODY``) so a
    real payment confirmation never hard-fails just because an admin hasn't
    customized this yet.

    ``language``/``template_type`` are plain strings, not native Postgres
    enums — validated at the schema layer (see ``app.schemas.email_template``)
    against ``app.i18n.SUPPORTED_LOCALES`` / ``app.models.enums.
    EmailTemplateType`` respectively. This mirrors ``Order.language``'s
    documented reasoning: adding a new supported language or a new email
    type (invoice, door-payment-reminder — named in the brief for later
    milestones) is then a Python-only change, no migration.

    ``subject``/``body`` use a small, fixed ``{{placeholder}}`` syntax —
    see ``app.services.email_placeholders.render_placeholders`` for the
    exact, isolated substitution implementation and the security properties
    it guarantees (no template-language features; every substituted value
    is sanitized/HTML-escaped). ``body`` is an HTML *content snippet* (a
    paragraph or two of admin-authored copy), not a full HTML document —
    the outgoing email's table-based shell/branding (logo, theme colors,
    ticket QR images, "time until show" line) is built separately around
    it by ``app.services.email_render``, mirroring how ``Theme.custom_css``
    is scoped to the landing page's content container rather than the whole
    page.

    Content-type data per PROJECT_BRIEF.md's AI/Agent Access section
    ("email template content" is explicitly listed as agent-accessible) —
    routes here use ``require_admin_or_agent``, not ``require_admin``.
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
