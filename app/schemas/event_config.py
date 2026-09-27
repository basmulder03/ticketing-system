"""Request/response models for EventConfig. Secrets are write-only: responses
expose only ``*_is_set`` booleans, never decrypted values.
"""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from app.models.enums import MollieMode, PaymentMethod, SmtpEncryptionMode

_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
"""Shape check only, not ``EmailStr``: ``email-validator`` rejects non-public
TLDs (e.g. ``demo@beacon.local``, internal relays). The test-email action
proves actual deliverability.
"""


class EventConfigUpdateRequest(BaseModel):
    """Upsert body. Omitted fields stay unchanged; clear a secret by sending an
    empty string.
    """

    smtp_host: str | None = Field(default=None, max_length=255)
    smtp_port: int | None = Field(default=None, gt=0, le=65535)
    smtp_encryption: SmtpEncryptionMode | None = None
    smtp_username: str | None = Field(default=None, max_length=255)
    smtp_password: str | None = None
    sender_name: str | None = Field(default=None, max_length=255)
    sender_email: str | None = Field(default=None, max_length=255, pattern=_EMAIL_PATTERN)
    mollie_test_api_key: str | None = None
    mollie_live_api_key: str | None = None
    mollie_mode: MollieMode | None = None
    invoice_company_name: str | None = Field(default=None, max_length=255)
    invoice_company_address: str | None = None
    invoice_company_vat_number: str | None = Field(default=None, max_length=50)
    invoice_number_prefix: str | None = Field(default=None, max_length=50)
    service_fee_amount: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    sales_live_at: datetime | None = None
    enabled_payment_methods: list[PaymentMethod] | None = None


class EventConfigOut(BaseModel):
    """Response shape for an EventConfig. Never includes decrypted secrets."""

    id: str
    event_id: str
    smtp_host: str | None
    smtp_port: int | None
    smtp_encryption: SmtpEncryptionMode
    smtp_username: str | None
    smtp_password_is_set: bool
    sender_name: str | None
    sender_email: str | None
    mollie_test_api_key_is_set: bool
    mollie_live_api_key_is_set: bool
    mollie_mode: MollieMode
    invoice_company_name: str | None
    invoice_company_address: str | None
    invoice_company_vat_number: str | None
    invoice_number_prefix: str | None
    service_fee_amount: Decimal
    sales_live_at: datetime | None
    enabled_payment_methods: list[PaymentMethod]
    created_at: datetime
    updated_at: datetime


class TestEmailRequest(BaseModel):
    """Body of ``POST /api/v1/events/{event_id}/config/test-email``."""

    recipient: str = Field(max_length=255, pattern=_EMAIL_PATTERN)


class TestMollieKeyRequest(BaseModel):
    """``environment`` picks which configured key (test/live) to verify."""

    environment: Literal["test", "live"] = "test"


class ConnectionTestResult(BaseModel):
    """Connection-test outcome. A handled failure (bad SMTP auth, invalid key)
    is a 200 with ``success=False`` and a readable message.
    """

    success: bool
    message: str
