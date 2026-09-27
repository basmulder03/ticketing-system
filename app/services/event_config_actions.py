"""EventConfig connection tests: send a test email via the event's SMTP
settings, and verify a Mollie key. Provider failures are expected outcomes,
returned as a ``ConnectionTestResult`` — never raised as a 500.
"""

import ssl
from email.message import EmailMessage

import aiosmtplib
import httpx

from app.models.enums import SmtpEncryptionMode
from app.models.event_config import EventConfig
from app.schemas.event_config import ConnectionTestResult

MOLLIE_API_BASE_URL = "https://api.mollie.com/v2"
"""The one real Mollie endpoint; only the key (test/live) varies per event."""

_HTTP_TIMEOUT_SECONDS = 10.0


async def send_test_email(config: EventConfig, recipient: str) -> ConnectionTestResult:
    """Send a minimal test email using only ``config``'s SMTP settings. In local
    dev this lands in Mailpit (http://localhost:8025).
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
    """Check the key with a cheap authenticated call (``GET /v2/methods``)."""
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
        # 400 = malformed key, 401/403 = invalid or revoked: all "key doesn't work".
        return ConnectionTestResult(success=False, message="Mollie rejected this API key.")
    return ConnectionTestResult(
        success=False, message=f"Unexpected response from Mollie (HTTP {response.status_code})."
    )
