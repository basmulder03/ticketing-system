"""HMAC-signed QR ticket tokens.

Tokens only need to prove authenticity (issued by us, untampered), so they're
signed, not encrypted. They share ``Settings.secret_key`` with session tokens
but use a distinct salt, so the two can never be swapped for each other. The
payload is just the random ``Ticket.id``; the signature stops anyone forging a
token for a guessed id. Tokens themselves aren't secret — they're printed on
tickets.
"""

import uuid

from itsdangerous import BadSignature, URLSafeSerializer

from app.core.config import get_settings

__all__ = ["sign_ticket_token", "verify_ticket_token"]

_TICKET_QR_SALT = "beacon-ticket-qr"


def _ticket_serializer() -> URLSafeSerializer:
    settings = get_settings()
    return URLSafeSerializer(settings.secret_key, salt=_TICKET_QR_SALT)


def sign_ticket_token(ticket_id: uuid.UUID) -> str:
    """Sign ``ticket_id`` into a URL-safe token.

    Sign each Ticket once and persist the result on ``Ticket.qr_token`` so
    the printed/emailed code stays stable.
    """
    return str(_ticket_serializer().dumps(str(ticket_id)))


def verify_ticket_token(token: str) -> uuid.UUID | None:
    """Return the Ticket id in ``token``, or ``None`` if it's tampered,
    malformed, or foreign. Never raises."""
    try:
        raw = _ticket_serializer().loads(token)
    except BadSignature:
        return None
    if not isinstance(raw, str):
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None
