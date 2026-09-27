"""Global settings from the environment / ``.env``.

Only infrastructure-level config lives here. Per-event config (SMTP, Mollie,
invoicing) lives on ``EventConfig``; the ``seed_*`` values below are only
local-dev defaults for ``scripts/seed.py``.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

_INSECURE_SECRET_KEY = "dev-insecure-secret-key-change-me"
_INSECURE_ENCRYPTION_KEY = "dev-insecure-encryption-key-change-me-32b"


class InsecureDefaultSecretError(RuntimeError):
    """A non-development environment booted with a dev-default secret.

    The defaults are public (they're in this repo), so booting with them
    would let anyone forge session cookies or decrypt stored credentials.
    """


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    """``development``, ``staging`` or ``production``."""

    database_url: str = "postgresql+asyncpg://beacon:beacon@localhost:5432/beacon"

    secret_key: str = _INSECURE_SECRET_KEY
    """Signs sessions, CSRF tokens and ticket QR codes. Must be overridden
    outside development (enforced in ``model_post_init``)."""

    encryption_key: str = _INSECURE_ENCRYPTION_KEY
    """Encrypts EventConfig secrets at rest. Must be overridden outside
    development (enforced in ``model_post_init``)."""

    default_locale: str = "en"

    session_timeout_minutes: int = 30

    login_rate_limit_per_minute: int = 10
    agent_auth_rate_limit_per_minute: int = 30
    checkout_rate_limit_per_minute: int = 10

    mollie_webhook_rate_limit_per_minute: int = 120
    """Generous: the caller is Mollie's retrying infrastructure, often from
    few shared IPs; dropping a retry would leave a paid order ``pending``."""

    scan_rate_limit_per_minute: int = 60
    """Looser than checkout: several scanners can share one venue NAT IP."""

    pending_order_ttl_hours: int = 6
    """How long an unresolved Mollie ``pending`` order may hold stock before
    the expiry sweep releases it. Mollie sends no webhook for an abandoned
    checkout, so without this the stock would be held forever. Kept well
    above Mollie's own session lifetime so it never races a real buyer."""

    pending_door_order_sweep_interval_minutes: int = 20
    """Interval of the in-process expiry sweep (both pending and pending_door)."""

    public_base_url: str = "http://localhost:8000"
    """Base for absolute links (sitemap, emails, PDFs, payment redirects)."""

    uploads_dir: str = "/app/uploads"
    """Where theme images are stored; a dedicated docker volume in compose."""

    theme_upload_max_bytes: int = 5 * 1024 * 1024

    # Local-dev seed data only (scripts/seed.py) — never used by a real deployment.
    seed_admin_email: str = "admin@beacon.local"
    seed_admin_password: str = "dev-only-change-me-123"
    seed_smtp_host: str = "mailpit"
    seed_smtp_port: int = 1025
    seed_smtp_username: str = ""
    seed_smtp_password: str = ""
    seed_smtp_use_tls: bool = False
    seed_smtp_sender_name: str = "Beacon Demo Event"
    seed_smtp_sender_email: str = "demo@beacon.local"
    seed_mollie_test_api_key: str = "test_placeholder_replace_with_real_mollie_test_key"

    def model_post_init(self, __context: object, /) -> None:
        """Refuse to boot outside development with a dev-default secret."""
        if self.app_env == "development":
            return
        if self.secret_key == _INSECURE_SECRET_KEY:
            raise InsecureDefaultSecretError(
                f"SECRET_KEY is still the insecure development default while "
                f"APP_ENV={self.app_env!r}. Set a real generated secret."
            )
        if self.encryption_key == _INSECURE_ENCRYPTION_KEY:
            raise InsecureDefaultSecretError(
                f"ENCRYPTION_KEY is still the insecure development default while "
                f"APP_ENV={self.app_env!r}. Set a real generated secret."
            )


@lru_cache
def get_settings() -> Settings:
    """Cached settings (env is read once per process)."""
    return Settings()
