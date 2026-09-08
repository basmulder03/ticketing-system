"""``EventConfig``: the single place every operational setting for one
Event lives — SMTP, Mollie, invoice/company details, sales-live timing, and
enabled payment methods. Nothing here is ever a shared global default; see
``app.core.config.Settings`` docstring for the split between infrastructure
config (there) and per-event operational config (here).
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import ARRAY, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.crypto import EncryptedString
from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import MollieMode, PaymentMethod, SmtpEncryptionMode

if TYPE_CHECKING:
    from app.models.event import Event


class EventConfig(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Per-event operational configuration (1:1 with ``Event``).

    Excluded entirely from agent-key access per PROJECT_BRIEF.md's AI/Agent
    Access section ("cannot touch payment credentials, SMTP credentials...
    or financial data") — every route touching this model must depend on
    ``app.api.deps.require_admin``, never ``require_admin_or_agent``.

    ``smtp_password``, ``mollie_test_api_key``, and ``mollie_live_api_key``
    use ``EncryptedString`` (Fernet, see ``app.core.crypto``) so they are
    never stored in plaintext at rest, per the brief's "All credentials in
    EventConfig are encrypted at rest" requirement. They must also never be
    round-tripped back to a client in plaintext over the API — routes return
    only "is this secret set" booleans (see ``app.schemas.event_config``).

    ``enabled_payment_methods`` is a Postgres array of ``PaymentMethod``
    values rather than two separate boolean columns: it reads directly as
    "the set of methods enabled for this event" (matches the brief's
    phrasing) and adding a third method later (if ever) is a value addition
    to the enum, not a schema change.

    ``next_invoice_number`` (Milestone 5) is the atomic per-event running
    counter backing sequential invoice numbering — see
    ``app.services.invoicing._allocate_invoice_number`` for the row-locked
    (``SELECT ... FOR UPDATE``) allocation discipline, mirroring
    ``app.services.stock.reserve_stock``'s locking pattern. It holds the
    NEXT number to be assigned (starts at 1, so the first invoice issued for
    an event is number 1), not the last one used — this ordering avoids an
    off-by-one at the very first allocation and matches how a fresh
    EventConfig with no invoices yet still has a well-defined "next" value.
    """

    __tablename__ = "event_configs"

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE"), unique=True, nullable=False
    )

    # --- SMTP ---
    smtp_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    smtp_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    smtp_encryption: Mapped[SmtpEncryptionMode] = mapped_column(
        SAEnum(
            SmtpEncryptionMode,
            name="smtp_encryption_mode",
            native_enum=True,
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=SmtpEncryptionMode.NONE,
    )
    smtp_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    smtp_password: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    sender_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sender_email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # --- Mollie ---
    mollie_test_api_key: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    mollie_live_api_key: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    mollie_mode: Mapped[MollieMode] = mapped_column(
        SAEnum(MollieMode, name="mollie_mode", native_enum=True, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=MollieMode.TEST,
    )
    """Which of the two Mollie keys above is actually used at checkout
    (Milestone 3, see ``app.services.mollie.resolve_mollie_api_key``) — an
    explicit per-event admin toggle, never inferred from publish status."""

    # --- Invoice / company details ---
    invoice_company_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    invoice_company_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    invoice_company_vat_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    invoice_number_prefix: Mapped[str | None] = mapped_column(String(50), nullable=True)
    next_invoice_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # --- Sales timing & payment methods ---
    sales_live_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    enabled_payment_methods: Mapped[list[PaymentMethod]] = mapped_column(
        ARRAY(
            SAEnum(
                PaymentMethod,
                name="payment_method",
                native_enum=True,
                values_callable=lambda e: [m.value for m in e],
            )
        ),
        nullable=False,
        default=list,
    )

    event: Mapped["Event"] = relationship(back_populates="config")
