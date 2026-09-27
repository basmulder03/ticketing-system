"""Password hashing (argon2id), agent API keys, and signed session tokens.

Admin sessions are stateless signed cookies: the token carries its issue
time, which :func:`verify_session_token` checks against the timeout — no
session table needed.
"""

import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerifyMismatchError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.config import get_settings

__all__ = [
    "ADMIN_SESSION_COOKIE_NAME",
    "AGENT_API_KEY_PREFIX",
    "create_session_token",
    "generate_agent_api_key",
    "hash_api_key",
    "hash_password",
    "verify_password",
    "verify_password_or_dummy",
    "verify_session_token",
]

_password_hasher = PasswordHasher()

ADMIN_SESSION_COOKIE_NAME = "beacon_admin_session"
_SESSION_SALT = "beacon-admin-session"
AGENT_API_KEY_PREFIX = "bcag_"

# Hash of an unguessable value, checked when no account exists so that
# "no such user" costs the same argon2 time as "wrong password" — otherwise
# response timing would reveal which emails are registered.
_DUMMY_PASSWORD_HASH = _password_hasher.hash(secrets.token_urlsafe(32))


def hash_password(plain_password: str) -> str:
    """Hash with argon2id. Never log the input."""
    return _password_hasher.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """``False`` for a wrong password or a malformed hash — never raises."""
    try:
        return _password_hasher.verify(hashed_password, plain_password)
    except (VerifyMismatchError, InvalidHash):
        return False


def verify_password_or_dummy(plain_password: str, hashed_password: str | None) -> bool:
    """Like :func:`verify_password`, but pays argon2's cost even when there's
    no account (``None``). Use this for any auth check whose timing must not
    reveal whether the account exists."""
    return verify_password(plain_password, hashed_password if hashed_password is not None else _DUMMY_PASSWORD_HASH)


def _session_serializer() -> URLSafeTimedSerializer:
    settings = get_settings()
    return URLSafeTimedSerializer(settings.secret_key, salt=_SESSION_SALT)


def create_session_token(admin_user_id: str) -> str:
    """Sign a timestamped session token for ``admin_user_id``."""
    return _session_serializer().dumps({"admin_user_id": admin_user_id})


def verify_session_token(token: str, max_age_seconds: int) -> str | None:
    """The admin id in ``token`` if the signature is valid and it's younger
    than ``max_age_seconds`` (the session timeout); else ``None``."""
    try:
        data = _session_serializer().loads(token, max_age=max_age_seconds)
    except (BadSignature, SignatureExpired):
        return None
    admin_user_id = data.get("admin_user_id") if isinstance(data, dict) else None
    return admin_user_id if isinstance(admin_user_id, str) else None


def generate_agent_api_key() -> tuple[str, str, str]:
    """Return ``(raw_key, key_hash, key_prefix)``.

    Show ``raw_key`` once and store only ``key_hash``. Keys are high-entropy
    random tokens, so fast SHA-256 is enough — argon2's slowness only matters
    for guessable passwords, and would slow every agent request.
    """
    raw_key = f"{AGENT_API_KEY_PREFIX}{secrets.token_urlsafe(32)}"
    return raw_key, hash_api_key(raw_key), raw_key[:12]


def hash_api_key(raw_key: str) -> str:
    """SHA-256 hex digest of a raw API key, used for storage and lookup."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
