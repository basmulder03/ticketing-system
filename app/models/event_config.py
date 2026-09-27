"""``EventConfig``: one event's operational settings (SMTP, Mollie, invoicing, sales timing, payment methods)."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import ARRAY, DateTime, ForeignKey, Integer, Numeric, String, Text
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
    """Per-event operational config (1:1 with ``Event``).

    Admin-only: agents must never reach it, so every route uses
    ``require_admin``. SMTP password and Mollie keys are encrypted at rest and
    never returned to clients (routes expose only "is set" booleans).
    ``next_invoice_number`` is the next number to assign, allocated under a
    row lock (``app.services.invoicing``).
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
    """Which Mollie key checkout uses — an explicit admin toggle."""

    # --- Invoice / company details ---
    invoice_company_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    invoice_company_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    invoice_company_vat_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    invoice_number_prefix: Mapped[str | None] = mapped_column(String(50), nullable=True)
    next_invoice_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # --- Pricing ---
    service_fee_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal("0.00"))
    """Flat amount added per ticket for ticket types with
    ``TicketType.service_fee_included`` set — see ``app.services.pricing``.
    """

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
