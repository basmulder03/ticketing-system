"""HMAC-signed QR ticket tokens (Milestone 4).

Provides the signing/verification primitive for the payload encoded in each
``Ticket``'s printed/emailed QR code. Reuses the same secret-key +
``itsdangerous`` infrastructure as admin session tokens (see
``app.core.security``) but under a distinct salt/purpose
(``beacon-ticket-qr`` vs. ``beacon-admin-session``) so a ticket token and a
session token are never interchangeable, even though both are ultimately
keyed off the same ``Settings.secret_key``. ``itsdangerous``'s default
signer is HMAC-based (HMAC-SHA1 over a URL-safe-base64 payload) — this only
needs to *prove authenticity* (issued by us, not tampered with), never to
be decrypted back to anything, so plain HMAC signing is the correct
primitive here per PROJECT_BRIEF.md's "unique HMAC-signed QR token"
requirement — unlike ``EventConfig``'s secrets, which use reversible
``EncryptedString`` (Fernet) because those genuinely need to be read back.

Each token encodes nothing but the ``Ticket.id`` (already a random
``uuid.uuid4``, per ``app.db.mixins.UUIDPrimaryKeyMixin``) — so the token is
"not sequential/guessable" for two independent reasons: the id itself is
random, and the signature prevents constructing a valid token for a guessed
id without the server's secret key.

This is the exact primitive Milestone 7's scanning UI will call
:func:`verify_ticket_token` against — built correctly now even though no
scanning UI exists yet.

Never log a signed token's secret key material (``Settings.secret_key``).
The signed tokens themselves are not secret (they are printed on a physical
ticket / embedded in an emailed QR code and PDF) but must remain unforgeable
without the key.
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
    """Produce an HMAC-signed, URL-safe token encoding ``ticket_id``.

    The returned string is what gets encoded into the ticket's QR code (see
    ``app.services.ticket_pdf``) and stored on ``Ticket.qr_token``. Calling
    this twice for the same ``ticket_id`` yields two different-looking but
    equally valid tokens (``itsdangerous`` does not add a nonce/salt beyond
    the fixed purpose salt above) — callers should sign a given Ticket
    exactly once and persist the result, rather than re-signing on every
    read, so ``Ticket.qr_token`` (and whatever's printed/emailed) stays
    stable — see ``app.services.ticket_delivery.sign_order_tickets``.
    """
    return str(_ticket_serializer().dumps(str(ticket_id)))


def verify_ticket_token(token: str) -> uuid.UUID | None:
    """Verify ``token`` and return the ``Ticket.id`` it encodes if the
    signature is valid, else ``None``.

    Returns ``None`` (never raises) for a tampered, malformed, or foreign
    token — Milestone 7's scanner should treat ``None`` as "invalid /
    unrecognized code", never as an authenticity check to bypass.
    """
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
