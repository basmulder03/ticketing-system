"""Fernet encryption for secrets at rest (SMTP passwords, Mollie API keys),
keyed off ``Settings.encryption_key``.

Never log a value returned by :func:`decrypt` or passed to :func:`encrypt`.
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
    """Turn an arbitrary-length secret into a valid Fernet key (SHA-256 → 32
    bytes), so operators needn't hand-generate a Fernet-format value."""
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


@lru_cache
def _fernet() -> Fernet:
    settings = get_settings()
    return Fernet(_derive_fernet_key(settings.encryption_key))


def encrypt(plaintext: str) -> str:
    """Encrypt ``plaintext`` into an ASCII token safe for a text column."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    """Decrypt a token from :func:`encrypt`. Raises ``InvalidToken`` if it's
    malformed, tampered with, or encrypted under a different key."""
    return _fernet().decrypt(token.encode("ascii")).decode("utf-8")


class EncryptedString(TypeDecorator[str]):
    """Column type that stores Fernet ciphertext but exposes plaintext to the
    ORM. The backing column is sized generously because ciphertext is longer."""

    impl = String(2048)
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return encrypt(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return decrypt(value)

    def process_literal_param(self, value: Any, dialect: Dialect) -> str:
        """Encrypted values must never appear as SQL literals."""
        raise NotImplementedError("EncryptedString does not support literal binds.")

    @property
    def python_type(self) -> type[str]:
        return str
