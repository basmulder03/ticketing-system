"""Unit tests for ``app.core.qr_tokens`` (Milestone 4): HMAC-signed QR
ticket tokens. Pure functions, no DB — mirrors ``tests/unit/test_security.py``'s
"pure function, no I/O" coverage style for the same ``itsdangerous``-backed
signing primitive.
"""

import uuid

from app.core.qr_tokens import sign_ticket_token, verify_ticket_token


def test_sign_then_verify_roundtrips_to_the_same_ticket_id() -> None:
    ticket_id = uuid.uuid4()
    token = sign_ticket_token(ticket_id)
    assert verify_ticket_token(token) == ticket_id


def test_token_is_a_string_not_the_raw_uuid() -> None:
    ticket_id = uuid.uuid4()
    token = sign_ticket_token(ticket_id)
    assert isinstance(token, str)
    assert token != str(ticket_id)


def test_signing_the_same_ticket_id_twice_is_deterministic() -> None:
    """``URLSafeSerializer`` (unlike ``itsdangerous``'s ``TimestampSigner``)
    adds no timestamp/nonce beyond the fixed purpose salt — see
    ``sign_ticket_token``'s docstring: "two different-looking but equally
    valid tokens" is describing ``itsdangerous``'s general capability, but
    the actual implementation used here (``URLSafeSerializer``, not
    ``URLSafeTimedSerializer``) produces the exact same token every time for
    the same input. Verified live rather than assumed."""
    ticket_id = uuid.uuid4()
    assert sign_ticket_token(ticket_id) == sign_ticket_token(ticket_id)


def test_tampered_token_flip_a_character_fails_verification() -> None:
    token = sign_ticket_token(uuid.uuid4())
    middle = len(token) // 2
    tampered = token[:middle] + ("x" if token[middle] != "x" else "y") + token[middle + 1 :]
    assert verify_ticket_token(tampered) is None


def test_tampered_token_truncated_fails_verification() -> None:
    token = sign_ticket_token(uuid.uuid4())
    assert verify_ticket_token(token[:-5]) is None


def test_tampered_token_with_appended_characters_fails_verification() -> None:
    token = sign_ticket_token(uuid.uuid4())
    assert verify_ticket_token(token + "abcde") is None


def test_token_signed_for_one_ticket_never_verifies_to_a_different_ticket_id() -> None:
    ticket_a = uuid.uuid4()
    ticket_b = uuid.uuid4()
    token_a = sign_ticket_token(ticket_a)
    assert verify_ticket_token(token_a) != ticket_b


def test_garbage_input_fails_cleanly_without_raising() -> None:
    assert verify_ticket_token("not-a-real-token") is None


def test_empty_string_input_fails_cleanly_without_raising() -> None:
    assert verify_ticket_token("") is None


def test_a_foreign_token_signed_with_a_different_purpose_salt_fails() -> None:
    """A session token (``app.core.security``, distinct salt
    ``beacon-admin-session``) must never verify as a valid ticket token,
    even though both are ultimately keyed off the same
    ``Settings.secret_key`` — see ``qr_tokens`` module docstring."""
    from app.core.security import create_session_token

    session_token = create_session_token("some-admin-id")
    assert verify_ticket_token(session_token) is None


def test_valid_signature_but_non_uuid_payload_fails_cleanly() -> None:
    """A well-formed, correctly-signed token whose payload isn't a UUID
    string at all (fabricated via the same underlying serializer/salt) must
    still fail verification rather than raise."""
    from app.core.qr_tokens import _ticket_serializer

    forged_but_correctly_signed = _ticket_serializer().dumps("not-a-uuid")
    assert verify_ticket_token(forged_but_correctly_signed) is None
