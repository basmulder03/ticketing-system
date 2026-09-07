"""Unit tests for ``app.core.security``: password hashing, session tokens,
and agent API-key generation. No DB/network involved — pure functions.
"""

import time

from app.core.security import (
    _session_serializer,
    create_session_token,
    generate_agent_api_key,
    hash_api_key,
    hash_password,
    verify_password,
    verify_password_or_dummy,
    verify_session_token,
)


def test_hash_password_then_verify_password_succeeds() -> None:
    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed) is True


def test_hash_password_produces_a_hash_different_from_the_input() -> None:
    hashed = hash_password("correct horse battery staple")
    assert hashed != "correct horse battery staple"


def test_verify_password_rejects_wrong_password() -> None:
    hashed = hash_password("correct horse battery staple")
    assert verify_password("wrong password", hashed) is False


def test_verify_password_rejects_malformed_hash_without_raising() -> None:
    assert verify_password("anything", "not-a-real-argon2-hash") is False


def test_verify_password_or_dummy_rejects_wrong_password_against_real_hash() -> None:
    hashed = hash_password("correct horse battery staple")
    assert verify_password_or_dummy("wrong password", hashed) is False


def test_verify_password_or_dummy_accepts_correct_password_against_real_hash() -> None:
    hashed = hash_password("correct horse battery staple")
    assert verify_password_or_dummy("correct horse battery staple", hashed) is True


def test_verify_password_or_dummy_returns_false_for_none_hash_without_raising() -> None:
    # None simulates "no such account"/"inactive account" — must fail closed,
    # not raise, and must still pay argon2's cost (see the docstring in
    # app.core.security) so a nonexistent-account login can't be
    # distinguished from a wrong-password one by response time.
    assert verify_password_or_dummy("anything", None) is False


def test_create_and_verify_session_token_roundtrip() -> None:
    token = create_session_token("admin-123")
    assert verify_session_token(token, max_age_seconds=3600) == "admin-123"


def test_verify_session_token_rejects_tampered_signature() -> None:
    token = create_session_token("admin-123")
    # Flip a character in the middle of the token (well inside the
    # base64url payload segment) rather than the very last character: the
    # last character of a base64url group can have unused low bits, so
    # some substitutions there decode to the exact same bytes and
    # wouldn't actually change the signed content.
    middle = len(token) // 2
    tampered = token[:middle] + ("x" if token[middle] != "x" else "y") + token[middle + 1 :]
    assert verify_session_token(tampered, max_age_seconds=3600) is None


def test_verify_session_token_rejects_garbage_input() -> None:
    assert verify_session_token("not-a-real-token", max_age_seconds=3600) is None


def test_verify_session_token_rejects_expired_token() -> None:
    # itsdangerous timestamps to whole seconds, so a token signed right at
    # a second boundary could read as only ~1s old even after sleeping
    # 1.1s (rounding, not a real bug) — sleep past two second boundaries
    # (2.1s) so elapsed age is unambiguously > the 1-second max_age
    # regardless of where in its signing second the token landed.
    token = _session_serializer().dumps({"admin_user_id": "admin-123"})
    time.sleep(2.1)
    assert verify_session_token(token, max_age_seconds=1) is None


def test_verify_session_token_accepts_token_within_max_age() -> None:
    token = _session_serializer().dumps({"admin_user_id": "admin-123"})
    assert verify_session_token(token, max_age_seconds=3600) == "admin-123"


def test_verify_session_token_rejects_token_missing_admin_user_id() -> None:
    # Uses the private serializer directly (same secret/salt as the real
    # one) to fabricate a token shape that create_session_token would never
    # produce, exercising verify_session_token's defensive isinstance check.
    token = _session_serializer().dumps({"something_else": "value"})
    assert verify_session_token(token, max_age_seconds=3600) is None


def test_generate_agent_api_key_produces_unique_keys() -> None:
    raw_key_a, _, _ = generate_agent_api_key()
    raw_key_b, _, _ = generate_agent_api_key()
    assert raw_key_a != raw_key_b


def test_generate_agent_api_key_hash_is_deterministic_and_not_the_raw_key() -> None:
    raw_key, key_hash, key_prefix = generate_agent_api_key()
    assert key_hash == hash_api_key(raw_key)
    assert key_hash != raw_key
    assert key_prefix == raw_key[:12]


def test_hash_api_key_is_deterministic_for_the_same_input() -> None:
    raw_key, _, _ = generate_agent_api_key()
    assert hash_api_key(raw_key) == hash_api_key(raw_key)


def test_hash_api_key_differs_for_different_inputs() -> None:
    raw_key_a, _, _ = generate_agent_api_key()
    raw_key_b, _, _ = generate_agent_api_key()
    assert hash_api_key(raw_key_a) != hash_api_key(raw_key_b)
