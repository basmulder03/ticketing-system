"""Symmetric encryption for secrets stored at rest.

Uses Fernet (AES-128-CBC + HMAC, via the ``cryptography`` package) keyed
off ``Settings.encryption_key``. This is the primitive ``EventConfig``
(added in a later milestone) will use to encrypt SMTP passwords and Mollie
API keys before they touch the database, per PROJECT_BRIEF.md's
"credentials encrypted at rest" requirement. Built now so that model can
use it directly instead of secrets briefly existing in plaintext columns.

Never log a decrypted value returned by :func:`decrypt` or the plaintext
passed to :func:`encrypt`.
"""

import base64
import hashlib
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import String
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from app.core.config import get_settings

__all__ = ["EncryptedString", "InvalidToken", "decrypt", "encrypt"]


def _derive_fernet_key(secret: str) -> bytes:
    """Derive a valid 32-byte urlsafe-base64 Fernet key from an arbitrary secret.

    ``Settings.encryption_key`` is an operator-supplied string of any
    length (see ``.env.example``), not necessarily already in the exact
    format Fernet requires. SHA-256 always produces exactly 32 bytes, which
    is what Fernet's key format needs, so hashing the configured secret
    yields a valid key deterministically without requiring operators to
    hand-generate a Fernet-specific value.
    """
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


@lru_cache
def _fernet() -> Fernet:
    """Return a process-cached ``Fernet`` instance keyed off the configured encryption key."""
    settings = get_settings()
    return Fernet(_derive_fernet_key(settings.encryption_key))


def encrypt(plaintext: str) -> str:
    """Encrypt ``plaintext`` and return an opaque ASCII token safe to store in a text column."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    """Decrypt a token produced by :func:`encrypt`.

    Raises ``cryptography.fernet.InvalidToken`` if the token is malformed,
    was encrypted with a different key, or has been tampered with.
    """
    return _fernet().decrypt(token.encode("ascii")).decode("utf-8")


class EncryptedString(TypeDecorator[str]):
    """SQLAlchemy column type that transparently encrypts/decrypts a string.

    The ORM layer sees plaintext; the database only ever sees Fernet
    ciphertext. Ciphertext is longer than plaintext and ASCII, hence the
    generous backing column size. Intended for EventConfig's SMTP
    password / Mollie API key columns starting Milestone 1.
    """

    impl = String(2048)
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        """Encrypt ``value`` before it is sent to the database."""
        if value is None:
            return None
        return encrypt(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> str | None:
        """Decrypt a stored value read back from the database."""
        if value is None:
            return None
        return decrypt(value)

    def process_literal_param(self, value: Any, dialect: Dialect) -> str:
        """Not supported: encrypted values must never appear as SQL literals."""
        raise NotImplementedError("EncryptedString does not support literal binds.")

    @property
    def python_type(self) -> type[str]:
        return str
