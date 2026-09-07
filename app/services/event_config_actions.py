"""Connection-test actions for ``EventConfig``: send a test email over the
event's own SMTP settings, and verify a Mollie API key against Mollie's API.

Both are admin-only actions (EventConfig is excluded from agent access) and
are called directly from EventConfig routes. Neither ever raises on a
network/provider failure — a real SMTP rejection or an invalid Mollie key is
a normal, expected outcome here, not a server error, so both functions
always return a :class:`~app.schemas.event_config.ConnectionTestResult`
rather than letting an exception propagate into a 500.
"""

import ssl
from email.message import EmailMessage

import aiosmtplib
import httpx

from app.models.enums import SmtpEncryptionMode
from app.models.event_config import EventConfig
from app.schemas.event_config import ConnectionTestResult

MOLLIE_API_BASE_URL = "https://api.mollie.com/v2"
"""Mollie's API base URL. Not configurable per event — this is the single
real Mollie endpoint; what *is* per-event is which API key (test/live) is
used against it."""

_HTTP_TIMEOUT_SECONDS = 10.0


async def send_test_email(config: EventConfig, recipient: str) -> ConnectionTestResult:
    """Send a minimal test email using ``config``'s own SMTP settings.

    Reads host/port/encryption/credentials/sender identity entirely from
    ``config`` — never a shared global default, per PROJECT_BRIEF.md's
    Per-Event Configuration requirement. In local dev, ``config.smtp_host``
    naturally points at the Mailpit sink (see ``docker-compose.yml``), so a
    successful test here is immediately visible at ``http://localhost:8025``.
    """
    if not config.smtp_host or not config.smtp_port or not config.sender_email:
        return ConnectionTestResult(
            success=False,
            message="SMTP host, port, and sender email must be set before testing.",
        )

    message = EmailMessage()
    message["From"] = f"{config.sender_name} <{config.sender_email}>" if config.sender_name else config.sender_email
    message["To"] = recipient
    message["Subject"] = "Beacon SMTP connection test"
    message.set_content(
        "This is a test email sent from the Beacon backoffice to verify this "
        "event's SMTP configuration is working."
    )

    use_tls = config.smtp_encryption == SmtpEncryptionMode.SSL
    start_tls = config.smtp_encryption == SmtpEncryptionMode.STARTTLS

    try:
        await aiosmtplib.send(
            message,
            hostname=config.smtp_host,
            port=config.smtp_port,
            username=config.smtp_username or None,
            password=config.smtp_password or None,
            use_tls=use_tls,
            start_tls=start_tls,
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
    except (aiosmtplib.SMTPException, ssl.SSLError, OSError, TimeoutError) as exc:
        return ConnectionTestResult(success=False, message=f"SMTP test failed: {exc}")
    return ConnectionTestResult(success=True, message=f"Test email sent to {recipient}.")


async def verify_mollie_api_key(api_key: str) -> ConnectionTestResult:
    """Verify ``api_key`` works by calling Mollie's low-cost ``GET /v2/methods``.

    A lightweight authenticated call rather than a full Mollie SDK
    integration — full payment-flow integration (payments, webhooks) is
    Milestone 3 scope; this only confirms the key is accepted.
    """
    if not api_key:
        return ConnectionTestResult(success=False, message="No Mollie API key configured.")

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(
                f"{MOLLIE_API_BASE_URL}/methods", headers={"Authorization": f"Bearer {api_key}"}
            )
    except httpx.HTTPError as exc:
        return ConnectionTestResult(success=False, message=f"Could not reach Mollie: {exc}")

    if response.status_code == httpx.codes.OK:
        return ConnectionTestResult(success=True, message="Mollie API key is valid.")
    if response.status_code in (httpx.codes.BAD_REQUEST, httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
        # Mollie returns 400 for a malformed key (e.g. wrong prefix/shape) and
        # 401/403 for a well-formed but invalid/revoked key — both mean "this
        # key doesn't work," not an unexpected server condition.
        return ConnectionTestResult(success=False, message="Mollie rejected this API key.")
    return ConnectionTestResult(
        success=False, message=f"Unexpected response from Mollie (HTTP {response.status_code})."
    )
