"""Shared enum types for auth/audit models."""

import enum


class AdminRole(str, enum.Enum):
    """Roles for session-authenticated human ``AdminUser`` accounts.

    Only ``admin`` and ``scanner`` are represented here, even though
    PROJECT_BRIEF.md lists three conceptual roles (Admin/Scanner/Agent).
    "Agent" is deliberately NOT an ``AdminRole`` value: agents authenticate
    via a separate API-key path (``AgentAccount``, see
    ``app/models/agent_account.py``), not password + session login, so
    modeling it as an ``AdminUser`` row with a role flag would blur two
    structurally different auth mechanisms and make it easy to accidentally
    grant an agent session-login capability it should never have.
    """

    ADMIN = "admin"
    SCANNER = "scanner"


class ActorType(str, enum.Enum):
    """Distinguishes human admins from AI agents in the audit log.

    Every audit entry MUST use one of these two values — never a generic
    "system" actor — per PROJECT_BRIEF.md's AI/Agent Access requirement.
    """

    HUMAN = "human"
    AI_AGENT = "ai_agent"


class PublishStatus(str, enum.Enum):
    """Shared draft/published status, per PROJECT_BRIEF.md's Draft & Preview
    section: "Every Event, Show, and Theme has a draft/published status".

    One enum reused by ``Event`` and ``Show`` (and, in Milestone 1.5,
    ``Theme``) rather than a separate near-identical enum per model (DRY) —
    the semantics are identical: a draft is only reachable via an unguessable
    preview link, a published record is publicly listed/indexed. The
    shareable-preview-link mechanics themselves are Milestone 2 scope; this
    milestone only builds the status field they'll key off.
    """

    DRAFT = "draft"
    PUBLISHED = "published"


class PaymentMethod(str, enum.Enum):
    """A payment method an Event can enable for its shows' checkout flow.

    Stored as a list on ``EventConfig.enabled_payment_methods`` (an event may
    enable one or both). Full payment-flow integration lands in later
    milestones (Mollie: Milestone 3, door: Milestone 6) — this milestone only
    builds the per-event on/off configuration.
    """

    MOLLIE = "mollie"
    DOOR = "door"


class OrderStatus(str, enum.Enum):
    """Lifecycle status of an ``Order`` (Milestone 2).

    - ``PENDING``: created at checkout, no payment settled yet.
    - ``PAID``: payment confirmed (Mollie webhook success — Milestone 3 —
      or manual mark-as-paid — Milestone 6). Tickets/invoice may be issued.
    - ``CANCELLED``: buyer/staff cancelled before payment; releases its
      reserved stock (see ``app.services.stock``).
    - ``EXPIRED``: the payment window lapsed without completion (e.g. a
      Mollie payment expired) — also releases its reserved stock.
    - ``PENDING_DOOR``: reserved now for Milestone 6 ("Pay at the Door" —
      an order whose buyer chose to pay in person at the door). Modeled
      here already (rather than as a later ``ALTER TYPE ... ADD VALUE``
      migration) so this milestone's schema doesn't need to churn again
      when Milestone 6 lands; no code path sets this status yet.

    Stock accounting (see ``app.services.stock``) treats every status
    except ``CANCELLED``/``EXPIRED`` as "still holding its ticket stock" —
    i.e. a ``Ticket`` row belonging to a ``PENDING``, ``PENDING_DOOR``, or
    ``PAID`` order counts against its ``TicketType``'s remaining stock.
    """

    PENDING = "pending"
    PAID = "paid"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    PENDING_DOOR = "pending_door"


class MollieMode(str, enum.Enum):
    """Explicit admin-controlled toggle for which of ``EventConfig``'s two
    Mollie API keys (``mollie_test_api_key`` / ``mollie_live_api_key``) is
    used when actually creating a Mollie payment (Milestone 3).

    Deliberately NOT inferred from ``Event.status`` (draft vs. published):
    PROJECT_BRIEF.md's Milestone 9 line "switch to live Mollie keys" reads
    as a deliberate admin action, not something that flips automatically
    the moment an event is published — an event can be published while
    still exercising Mollie's test mode (e.g. a soft-launch review window),
    and conversely a still-draft/preview event could in principle be
    switched to live early. Defaults to ``TEST`` so a newly created
    EventConfig can never accidentally process a real charge before an
    admin explicitly opts in to ``LIVE``.
    """

    TEST = "test"
    LIVE = "live"


class SmtpEncryptionMode(str, enum.Enum):
    """Transport encryption mode for an EventConfig's SMTP settings.

    Mirrors the options ``aiosmtplib`` actually needs to distinguish:
    implicit TLS on connect (``ssl``, e.g. one.com port 465), STARTTLS after
    a plaintext connect (``starttls``, e.g. one.com port 587), or no
    encryption at all (``none`` — used for the local Mailpit dev sink, which
    doesn't speak TLS).
    """

    NONE = "none"
    SSL = "ssl"
    STARTTLS = "starttls"


class ThemeFont(str, enum.Enum):
    """A curated, fixed list of font choices for a Theme — deliberately NOT
    free-text (the brief calls Theme's font choice "a fixed field"), so
    every value here is guaranteed renderable without loading arbitrary
    third-party font URLs (which would also reintroduce the exact
    exfiltration/tracking-via-external-request risk the custom-CSS
    sanitizer's ``url()`` rule exists to prevent).

    Two system-stack options (no network request at all, instant render,
    best privacy/perf) plus six well-known Google-Fonts-style faces chosen
    for broad legibility across a sans/serif split. Actually self-hosting
    the non-system font files (so the public site never calls out to
    Google Fonts' CDN, keeping the same no-external-request posture as the
    rest of this theming feature) is a `frontend-theming`/Milestone 2
    concern — this enum only fixes the *choice list* and, via
    ``app.services.theme_preview.FONT_STACKS``, the CSS ``font-family``
    fallback stack for each choice.
    """

    SYSTEM_SANS = "system-sans"
    SYSTEM_SERIF = "system-serif"
    INTER = "inter"
    ROBOTO = "roboto"
    OPEN_SANS = "open-sans"
    LORA = "lora"
    MERRIWEATHER = "merriweather"
    PLAYFAIR_DISPLAY = "playfair-display"
