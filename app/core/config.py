"""Application-wide settings, loaded from environment variables / ``.env``.

Only genuinely global, infrastructure-level config lives here (DB
connection, session secret, the key used to encrypt secrets at rest,
default locale). Per-event operational config (SMTP, Mollie keys, invoice
details) belongs on the ``EventConfig`` model per ``PROJECT_BRIEF.md`` and
is NOT read from here at runtime — the ``SEED_*`` values below exist only
so the Milestone 0 seed script has sensible local-dev defaults to write
into the first demo Event's ``EventConfig``.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Global application settings sourced from the environment."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    """One of ``development``, ``staging``, ``production``."""

    database_url: str = "postgresql+asyncpg://beacon:beacon@localhost:5432/beacon"
    """SQLAlchemy async connection string for PostgreSQL."""

    secret_key: str = "dev-insecure-secret-key-change-me"
    """Used for session signing/CSRF once auth lands in a later milestone."""

    encryption_key: str = "dev-insecure-encryption-key-change-me-32b"
    """Key used to encrypt EventConfig secrets (SMTP/Mollie credentials) at
    rest. Must be overridden with a real generated secret in staging/prod —
    see README "Environments" section."""

    default_locale: str = "en"

    session_timeout_minutes: int = 30
    """Admin session timeout. A session cookie whose embedded issue
    timestamp is older than this is rejected even if its signature is
    still valid — see ``app.core.security.verify_session_token``."""

    login_rate_limit_per_minute: int = 10
    """Max ``/api/v1/auth/login`` attempts per client IP per rolling minute."""

    agent_auth_rate_limit_per_minute: int = 30
    """Max agent-API-key-authenticated requests per client IP per rolling minute."""

    # Seed-only defaults for the demo AdminUser (local dev login). Never
    # used for real deployments — change/rotate before going anywhere near
    # production.
    seed_admin_email: str = "admin@beacon.local"
    seed_admin_password: str = "dev-only-change-me-123"

    # Seed-only defaults for the demo Event's EventConfig (local dev SMTP
    # sink + Mollie test key placeholder). Never used for real events.
    seed_smtp_host: str = "mailpit"
    seed_smtp_port: int = 1025
    seed_smtp_username: str = ""
    seed_smtp_password: str = ""
    seed_smtp_use_tls: bool = False
    seed_smtp_sender_name: str = "Beacon Demo Event"
    seed_smtp_sender_email: str = "demo@beacon.local"
    seed_mollie_test_api_key: str = "test_placeholder_replace_with_real_mollie_test_key"


@lru_cache
def get_settings() -> Settings:
    """Return a cached ``Settings`` instance (env is read once per process)."""
    return Settings()
