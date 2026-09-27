"""Shared enum types."""

import enum


class AdminRole(str, enum.Enum):
    """Roles for session-login ``AdminUser`` accounts.

    Agents are deliberately not a role: they use a separate API-key path
    (``AgentAccount``) and must never gain session-login capability.
    """

    ADMIN = "admin"
    SCANNER = "scanner"


class ActorType(str, enum.Enum):
    """Who performed an audited action.

    Agent actions must never be recorded as human or as ``SYSTEM``; ``SYSTEM`` is
    only for the app's own automated processes (e.g. Mollie webhook
    reconciliation).
    """

    HUMAN = "human"
    AI_AGENT = "ai_agent"
    SYSTEM = "system"


class PublishStatus(str, enum.Enum):
    """Draft/published status for Event, Show and Theme. Drafts are reachable
    only via the unguessable preview link.
    """

    DRAFT = "draft"
    PUBLISHED = "published"


class PaymentMethod(str, enum.Enum):
    """How an order is paid.

    ``MOLLIE``, ``DOOR`` and ``DEMO`` are buyer-selectable per event via
    ``EventConfig.enabled_payment_methods``. ``DEMO`` simulates a provider's
    pending-then-settled flow entirely in-process (no credentials, no outbound
    calls). ``MANUAL`` is never buyer-selectable: it marks an order an admin
    created directly (``app.services.manual_order``).
    """

    MOLLIE = "mollie"
    DOOR = "door"
    DEMO = "demo"
    MANUAL = "manual"


class OrderStatus(str, enum.Enum):
    """Order lifecycle.

    - ``PENDING``: created, payment not settled (Mollie or demo).
    - ``PENDING_DOOR``: pay-at-the-door reservation.
    - ``PAID``: settled; tickets and invoice may be issued.
    - ``CANCELLED`` / ``EXPIRED``: abandoned; stock released.

    Every status except ``CANCELLED``/``EXPIRED`` holds its tickets' stock.
    """

    PENDING = "pending"
    PAID = "paid"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    PENDING_DOOR = "pending_door"


class MollieMode(str, enum.Enum):
    """Which Mollie key (test/live) an event uses. An explicit admin choice, not
    derived from publish status; defaults to ``TEST`` so nothing is charged
    until an admin opts in to ``LIVE``.
    """

    TEST = "test"
    LIVE = "live"


class SmtpEncryptionMode(str, enum.Enum):
    """SMTP transport encryption: implicit TLS (``ssl``, e.g. port 465),
    ``starttls`` (e.g. 587), or ``none`` (local Mailpit only).
    """

    NONE = "none"
    SSL = "ssl"
    STARTTLS = "starttls"


class EmailTemplateType(str, enum.Enum):
    """Kinds of outgoing email with editable subject/body. Stored as a plain
    string column, so adding a type needs no migration.
    """

    ORDER_CONFIRMATION_TICKET = "order_confirmation_ticket"
    DOOR_PAYMENT_CONFIRMATION = "door_payment_confirmation"


class ScanOutcome(str, enum.Enum):
    """Result of one door scan. Not a DB column; also written to the audit log's
    ``detail`` and returned by the scan API.

    - ``PASS``: valid, right show, paid, first scan — ticket is now marked scanned.
    - ``ALREADY_SCANNED``: otherwise valid, but scanned before.
    - ``INVALID``: bad signature or unknown ticket (indistinguishable on purpose).
    - ``WRONG_SHOW``: valid ticket for a different show.
    - ``UNPAID``: valid but the order isn't paid. Not marked scanned; rescan
      after an admin marks the order paid.
    """

    PASS = "pass"
    ALREADY_SCANNED = "already_scanned"
    INVALID = "invalid"
    WRONG_SHOW = "wrong_show"
    UNPAID = "unpaid"


class ThemeFont(str, enum.Enum):
    """Fixed font choices (never free text, so no arbitrary external font URLs).

    The non-system fonts are self-hosted static WOFF2 files (``app/static/
    fonts/``, declared in ``app/static/fonts.css``) — never a third-party CDN,
    which would leak visitor IPs to it. See ``app.services.theme_preview
    .FONT_STACKS`` for the fallback stack each one degrades to before its font
    file loads.
    """

    SYSTEM_SANS = "system-sans"
    SYSTEM_SERIF = "system-serif"
    INTER = "inter"
    ROBOTO = "roboto"
    OPEN_SANS = "open-sans"
    LORA = "lora"
    MERRIWEATHER = "merriweather"
    PLAYFAIR_DISPLAY = "playfair-display"
