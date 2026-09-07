"""Unit tests for ``app.core.crypto``: the Fernet-backed encrypt/decrypt
helpers and the ``EncryptedString`` SQLAlchemy type. No DB involved — the
type's ``process_bind_param``/``process_result_value`` are plain functions
that don't require a real connection/dialect to exercise.
"""

from typing import cast

import pytest
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.engine import Dialect

from app.core.crypto import EncryptedString, _derive_fernet_key, decrypt, encrypt

# EncryptedString never actually branches on the dialect (see app/core/crypto.py),
# so a typed placeholder is enough to exercise it without a real DB connection.
_DIALECT = cast(Dialect, None)


def test_encrypt_decrypt_roundtrip() -> None:
    plaintext = "smtp-password-hunter2"
    ciphertext = encrypt(plaintext)
    assert ciphertext != plaintext
    assert decrypt(ciphertext) == plaintext


def test_decrypt_with_a_different_key_raises_invalid_token() -> None:
    ciphertext = encrypt("mollie-live-api-key-xyz")
    other_fernet = Fernet(_derive_fernet_key("a-completely-different-secret"))
    with pytest.raises(InvalidToken):
        other_fernet.decrypt(ciphertext.encode("ascii"))


def test_decrypt_rejects_tampered_ciphertext() -> None:
    ciphertext = encrypt("mollie-live-api-key-xyz")
    tampered = ciphertext[:-1] + ("a" if ciphertext[-1] != "a" else "b")
    with pytest.raises(InvalidToken):
        decrypt(tampered)


def test_derive_fernet_key_is_deterministic_for_the_same_secret() -> None:
    assert _derive_fernet_key("some-secret") == _derive_fernet_key("some-secret")


def test_derive_fernet_key_differs_for_different_secrets() -> None:
    assert _derive_fernet_key("secret-a") != _derive_fernet_key("secret-b")


def test_encrypted_string_process_bind_param_encrypts_non_none_values() -> None:
    column_type = EncryptedString()
    bound = column_type.process_bind_param("smtp-password", dialect=_DIALECT)
    assert bound is not None
    assert bound != "smtp-password"
    assert decrypt(bound) == "smtp-password"


def test_encrypted_string_process_bind_param_passes_through_none() -> None:
    column_type = EncryptedString()
    assert column_type.process_bind_param(None, dialect=_DIALECT) is None


def test_encrypted_string_process_result_value_decrypts_stored_ciphertext() -> None:
    column_type = EncryptedString()
    stored = encrypt("mollie-test-api-key")
    assert column_type.process_result_value(stored, dialect=_DIALECT) == "mollie-test-api-key"


def test_encrypted_string_process_result_value_passes_through_none() -> None:
    column_type = EncryptedString()
    assert column_type.process_result_value(None, dialect=_DIALECT) is None


def test_encrypted_string_roundtrip_via_bind_and_result() -> None:
    column_type = EncryptedString()
    bound = column_type.process_bind_param("round-trip-me", dialect=_DIALECT)
    assert column_type.process_result_value(bound, dialect=_DIALECT) == "round-trip-me"


def test_encrypted_string_rejects_literal_binds() -> None:
    column_type = EncryptedString()
    with pytest.raises(NotImplementedError):
        column_type.process_literal_param("anything", dialect=_DIALECT)


def test_encrypted_string_python_type_is_str() -> None:
    assert EncryptedString().python_type is str
