"""Password hashing, agent API-key generation, and session token signing.

Password hashing uses ``argon2-cffi`` directly (already a pinned
dependency) rather than passlib: passlib's own argon2 backend just wraps
argon2-cffi, and passlib has been effectively unmaintained, so calling
argon2-cffi directly avoids an unnecessary indirection layer. Decision
noted here since the brief left "argon2 or bcrypt" open.

Admin sessions are stateless signed cookies (via ``itsdangerous``), not a
DB-backed session table: the token embeds an issue timestamp that
:func:`verify_session_token` checks against the configured session
timeout. This keeps session timeout enforcement correct without an extra
table/service, appropriate for a small single-instance deployment (KISS).
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
    "verify_session_token",
]

_password_hasher = PasswordHasher()

ADMIN_SESSION_COOKIE_NAME = "beacon_admin_session"
_SESSION_SALT = "beacon-admin-session"
AGENT_API_KEY_PREFIX = "bcag_"


def hash_password(plain_password: str) -> str:
    """Hash a plaintext password with argon2id. Never log the input value."""
    return _password_hasher.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against an argon2 hash.

    Returns ``False`` (rather than raising) both for a wrong password and
    for a malformed/foreign hash, so callers get a single failure branch
    and can't distinguish "user doesn't exist" from "wrong password"
    timing/behavior at this layer.
    """
    try:
        return _password_hasher.verify(hashed_password, plain_password)
    except (VerifyMismatchError, InvalidHash):
        return False


def _session_serializer() -> URLSafeTimedSerializer:
    settings = get_settings()
    return URLSafeTimedSerializer(settings.secret_key, salt=_SESSION_SALT)


def create_session_token(admin_user_id: str) -> str:
    """Sign an opaque, timestamped session token for ``admin_user_id``."""
    return _session_serializer().dumps({"admin_user_id": admin_user_id})


def verify_session_token(token: str, max_age_seconds: int) -> str | None:
    """Return the admin_user_id embedded in ``token`` if still valid, else ``None``.

    "Valid" means: signature checks out AND the embedded timestamp is no
    older than ``max_age_seconds`` — this is the session-timeout check.
    """
    try:
        data = _session_serializer().loads(token, max_age=max_age_seconds)
    except (BadSignature, SignatureExpired):
        return None
    admin_user_id = data.get("admin_user_id") if isinstance(data, dict) else None
    return admin_user_id if isinstance(admin_user_id, str) else None


def generate_agent_api_key() -> tuple[str, str, str]:
    """Generate a new agent API key.

    Returns ``(raw_key, key_hash, key_prefix)``. ``raw_key`` must be shown
    to the caller exactly once (at creation time) and never stored; only
    ``key_hash`` is persisted. API keys are high-entropy random tokens (not
    user-chosen passwords), so a fast cryptographic hash (SHA-256) is
    appropriate here — unlike admin passwords, there's no low-entropy
    guessing risk that argon2's deliberate slowness needs to defend
    against, and a fast hash keeps every agent-authenticated request cheap.
    """
    raw_key = f"{AGENT_API_KEY_PREFIX}{secrets.token_urlsafe(32)}"
    return raw_key, hash_api_key(raw_key), raw_key[:12]


def hash_api_key(raw_key: str) -> str:
    """SHA-256 hex digest of a raw agent API key, used for storage and DB lookup."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
