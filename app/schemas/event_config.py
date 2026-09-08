"""Pydantic request/response models for EventConfig.

Secrets (SMTP password, Mollie API keys) are write-only at this layer:
request schemas accept plaintext input, but response schemas never echo a
decrypted value back — only an ``*_is_set`` boolean — per PROJECT_BRIEF.md's
Security & Ops requirement that secrets are "never logged or exposed
client-side". The one exception is the connection-test actions, which need
the plaintext internally (server-side only, never returned) to actually
attempt a connection.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models.enums import MollieMode, PaymentMethod, SmtpEncryptionMode

_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
"""A deliberately lightweight email-shape check (not full RFC/deliverability
validation via ``EmailStr``/``email-validator``): this is a self-hosted app
whose own local-dev SMTP sink convention (``demo@beacon.local``, see
``.env.example``) uses a non-public TLD that ``email-validator``'s stricter
"special-use domain" check rejects outright. Operators may also reasonably
run internal mail relays on non-public domains in production. A basic
shape check is enough here — actual deliverability is proven by the
test-email connection-test action itself, not by request validation."""


class EventConfigUpdateRequest(BaseModel):
    """Body of ``PUT /api/v1/events/{event_id}/config``.

    Upserts the event's config (creates it if it doesn't exist yet). Only
    fields explicitly present in the request body are applied
    (``exclude_unset`` semantics) — a field omitted entirely is left
    unchanged (or left at its default on first creation); to explicitly
    clear a secret, pass it as an empty string.
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
    sales_live_at: datetime | None
    enabled_payment_methods: list[PaymentMethod]
    created_at: datetime
    updated_at: datetime


class TestEmailRequest(BaseModel):
    """Body of ``POST /api/v1/events/{event_id}/config/test-email``."""

    recipient: str = Field(max_length=255, pattern=_EMAIL_PATTERN)


class TestMollieKeyRequest(BaseModel):
    """Body of ``POST /api/v1/events/{event_id}/config/test-mollie``.

    ``environment`` selects which of the two configured keys (test/live) to
    verify — the same key is never assumed; the caller must say which one.
    """

    environment: Literal["test", "live"] = "test"


class ConnectionTestResult(BaseModel):
    """Result of a connection-test action (test email / Mollie key verify).

    Always returned with HTTP 200 on a *handled* failure (e.g. SMTP auth
    rejected, Mollie key invalid) — ``success=False`` plus a human-readable
    ``message`` — so a real network/provider failure never crashes the
    request; only unexpected server errors return a 5xx.
    """

    success: bool
    message: str
