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

_INSECURE_SECRET_KEY = "dev-insecure-secret-key-change-me"
_INSECURE_ENCRYPTION_KEY = "dev-insecure-encryption-key-change-me-32b"


class InsecureDefaultSecretError(RuntimeError):
    """Raised when a non-development environment boots with a dev-default secret.

    Booting staging/production with ``secret_key`` or ``encryption_key`` left at
    their hardcoded dev defaults would let anyone who has read this public repo
    forge session cookies or decrypt EventConfig credentials (SMTP passwords,
    Mollie keys) at rest.
    """


class Settings(BaseSettings):
    """Global application settings sourced from the environment."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    """One of ``development``, ``staging``, ``production``."""

    database_url: str = "postgresql+asyncpg://beacon:beacon@localhost:5432/beacon"
    """SQLAlchemy async connection string for PostgreSQL."""

    secret_key: str = _INSECURE_SECRET_KEY
    """Used for session signing/CSRF. Must be overridden outside development —
    see ``model_post_init`` below, which refuses to boot on the dev default."""

    encryption_key: str = _INSECURE_ENCRYPTION_KEY
    """Key used to encrypt EventConfig secrets (SMTP/Mollie credentials) at
    rest. Must be overridden with a real generated secret in staging/prod —
    see README "Environments" section. Must be overridden outside development —
    see ``model_post_init`` below, which refuses to boot on the dev default."""

    default_locale: str = "en"

    session_timeout_minutes: int = 30
    """Admin session timeout. A session cookie whose embedded issue
    timestamp is older than this is rejected even if its signature is
    still valid — see ``app.core.security.verify_session_token``."""

    login_rate_limit_per_minute: int = 10
    """Max ``/api/v1/auth/login`` attempts per client IP per rolling minute."""

    agent_auth_rate_limit_per_minute: int = 30
    """Max agent-API-key-authenticated requests per client IP per rolling minute."""

    checkout_rate_limit_per_minute: int = 10
    """Max ``POST /api/v1/public/checkout`` attempts per client IP per rolling
    minute (Milestone 2) — see ``app.core.rate_limit.checkout_rate_limiter``."""

    mollie_webhook_rate_limit_per_minute: int = 120
    """Max ``POST /api/v1/public/mollie-webhook`` requests per client IP per
    rolling minute (Milestone 3). Deliberately much more generous than the
    checkout limit: this endpoint's real caller is Mollie's own
    infrastructure retrying undelivered webhooks (potentially from a small
    number of shared source IPs across many merchants), not individual
    buyers — a tight per-IP limit here risks silently dropping Mollie's own
    legitimate retries, which would leave a paid order stuck ``pending``.
    120/minute is generous enough for that while still bounding the worst
    case if this public, unauthenticated endpoint is ever hit by something
    other than Mollie (each request only causes one lookup plus, if it
    matches a real Order, one outbound Mollie GET call — see
    ``app.api.routes.public.mollie_webhook``)."""

    scan_rate_limit_per_minute: int = 60
    """Max ``POST /api/v1/shows/{show_id}/scan`` attempts per client IP per
    rolling minute (Milestone 7) — per PROJECT_BRIEF.md's Security & Ops
    section ("rate limiting on checkout and scan endpoints"). Set well above
    a single scanner's realistic worst-case entry-rush rate: this endpoint's
    caller is a door-staff mobile browser, not a buyer, but a shared venue
    Wi-Fi network can put several scanning devices behind one NATed IP, so
    the limit is looser than ``checkout_rate_limit_per_minute`` while still
    bounding abuse (e.g. someone scripting signature-guessing attempts
    against ``verify_ticket_token``) — see ``app.core.rate_limit.scan_rate_limiter``."""

    pending_order_ttl_hours: int = 6
    """Max age (hours) a Mollie-method ``pending`` Order is allowed to sit
    unresolved before ``app.services.order_expiry`` sweeps it to ``expired``,
    releasing its reserved stock. Exists because a buyer who opens the
    Mollie checkout page and abandons the browser tab produces no webhook
    delivery at all — Mollie's OWN payment session eventually expires on
    Mollie's side, but this app is never told, so without a sweep the Order
    (and the stock its Tickets hold) would sit ``pending`` forever, which
    for a limited-capacity show shows up as phantom "sold out" state from
    checkouts nobody ever completed.

    6 hours is deliberately generous relative to how long a real Mollie
    checkout session is actually open for (Mollie's own iDEAL/card sessions
    typically expire in well under an hour), so this can never race a
    buyer who is genuinely still mid-checkout, even accounting for someone
    stepping away and coming back — while still being short enough that a
    single popular/limited-capacity on-sale moment (the scenario this exists
    to protect) doesn't stay artificially "sold out" for long. A named,
    tunable setting rather than a hardcoded constant so an operator can
    shorten it for a high-demand on-sale without a code change; see
    ``app.services.order_expiry`` for the full sweep logic."""

    pending_door_order_sweep_interval_minutes: int = 20
    """How often the in-process background sweep
    (``app.services.order_expiry.run_order_expiry_background_loop``) runs,
    in minutes. Applies to BOTH the ``pending_order_ttl_hours`` sweep and the
    ``pending_door`` show-has-passed sweep — one shared interval for one
    shared loop. 20 minutes is frequent enough that a stale reservation
    doesn't hold phantom stock for long, without being aggressive enough to
    put meaningful load on the DB from a single-process background poll;
    see that module's docstring for the "why a background loop AND a
    standalone script" reasoning."""

    public_base_url: str = "http://localhost:8000"
    """Canonical public base URL used to build absolute links in
    ``sitemap.xml``/``robots.txt`` (Milestone 2) and, in later milestones,
    email/PDF content. A genuinely global, infra-level setting (not
    per-event) since this app is deployed on one domain per
    PROJECT_BRIEF.md's single-VPS deployment target — deliberately NOT
    scoped in ``EventConfig``."""

    uploads_dir: str = "/app/uploads"
    """Local filesystem directory theme images (logo/background) are written
    to and served from. No object storage per PROJECT_BRIEF.md's "avoid
    adding services unless a requirement actually needs one" — this is a
    small self-hosted VPS deployment. Mounted as a dedicated docker volume
    (see ``docker-compose.yml``) so uploads survive container
    rebuilds/redeploys independently of the app image."""

    theme_upload_max_bytes: int = 5 * 1024 * 1024
    """Max accepted size (bytes) for a single theme logo/background image
    upload — see ``app.services.theme_images``. 5 MB comfortably covers a
    web-optimized logo/hero image without risking memory pressure on the
    small Hetzner VPS target."""

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

    def model_post_init(self, __context: object, /) -> None:
        """Refuse to boot outside development with a hardcoded dev-default secret."""
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
    """Return a cached ``Settings`` instance (env is read once per process)."""
    return Settings()
