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
    """Distinguishes human admins, AI agents, and the app's own automated
    processes in the audit log.

    PROJECT_BRIEF.md's AI/Agent Access section requires that an Agent
    key's actions are "never recorded as if a human admin made it, and
    never silently merged into a generic 'system' actor" — that rule is
    about not mislabeling AGENT actions, not a ban on a real, correctly-
    named system actor existing at all. ``SYSTEM`` (added Milestone 3) is
    for genuinely non-human, non-agent-key automated backend processes —
    today, Mollie webhook payment reconciliation and the preview-mode
    simulated-payment path (``app.services.order_payment.SYSTEM_PRINCIPAL``)
    — so those entries are never misclassified as ``HUMAN`` in any future
    audit-log view that segments "actions by staff" from everything else.
    """

    HUMAN = "human"
    AI_AGENT = "ai_agent"
    SYSTEM = "system"


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
    enable any combination). Full payment-flow integration lands across
    several milestones (Mollie: Milestone 3, door: Milestone 6, demo:
    post-launch fix).

    ``DEMO`` is a real, selectable payment method (not a dev/test-only
    backdoor) any event can enable, per the user's NOTES: "create a custom
    one, that behaves something like mollie for a test environment/demo
    purposes without having to do stuff with external applications" — it
    walks the buyer through the same pending-then-settled shape a real
    provider would (an interstitial page, not an instant auto-pay), but
    entirely in-process, no credentials or outbound calls of any kind. See
    ``app.services.checkout``'s payment-initiation dispatch and
    ``app.web.routes.demo_payment`` for the full flow — kept structurally
    parallel to Mollie's own initiation/webhook shape specifically so a
    THIRD real provider can be added later by implementing the same
    initiate-then-settle shape, not by special-casing checkout.py again.

    ``MANUAL`` is NOT one of these buyer-selectable checkout methods —
    it never appears in ``enabled_payment_methods`` and a buyer can never
    choose it at checkout. It marks an Order an admin created directly,
    from nothing, for a buyer who never submitted any checkout request at
    all — per the user's NOTES: "for people without a computer or phone,
    allow for an admin to create/do things with tickets... without having
    the payment process." See ``app.services.manual_order.create_manual_order``.
    """

    MOLLIE = "mollie"
    DOOR = "door"
    DEMO = "demo"
    MANUAL = "manual"


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


class EmailTemplateType(str, enum.Enum):
    """The kind of outgoing email an ``EmailTemplate`` row's ``subject``/
    ``body`` apply to, per PROJECT_BRIEF.md's Ticket Generation & Delivery
    section ("subject and body for each email type (order confirmation/
    ticket, invoice, door-payment reminder, etc.)").

    ``ORDER_CONFIRMATION_TICKET`` (Milestone 4) and
    ``DOOR_PAYMENT_CONFIRMATION`` (post-launch fix, see
    ``app.services.door_reservation_email``) are wired to an actual send
    path. A future invoice-specific email type is deliberately NOT
    pre-declared here — add it when it's actually needed, per KISS ("don't
    build unused machinery now").

    ``EmailTemplate.template_type`` stores this enum's value as a plain
    string column (not a native Postgres enum), the same choice
    ``Order.language`` makes and for the same reason (see that column's
    docstring): adding a new email type later is then a Python-only change,
    no migration.
    """

    ORDER_CONFIRMATION_TICKET = "order_confirmation_ticket"
    DOOR_PAYMENT_CONFIRMATION = "door_payment_confirmation"


class ScanOutcome(str, enum.Enum):
    """The discriminated result of one door-scan attempt (Milestone 7).

    NOT a mapped/persisted column type — unlike every other enum in this
    module, ``ScanOutcome`` never backs a database column. It lives here
    anyway (rather than in ``app.schemas.scan``) to keep every shared,
    string-valued app enum in one place, and because its value is written
    into ``AuditLogEntry.detail`` (a JSON column) as the ``result`` field
    by ``app.services.scan`` — see that module for the full outcome
    semantics. ``app.schemas.scan.ScanResponse`` reuses this exact enum as
    its ``outcome`` field so the JSON API and the audit log always agree on
    the same five spellings.

    - ``PASS``: signature valid, correct show, order paid, not previously
      scanned — entry is complete, ``Ticket.scanned_at``/``scanned_by`` are
      now set.
    - ``ALREADY_SCANNED``: everything else checks out but the ticket was
      already scanned before this attempt — a genuine fail, not a pass.
    - ``INVALID``: the QR token's signature didn't verify, or verified but
      names a ``Ticket.id`` that doesn't exist. Both are surfaced with the
      same generic message so a caller can't distinguish "tampered
      signature" from "well-formed but unknown ticket" (see
      ``app.core.qr_tokens.verify_ticket_token``).
    - ``WRONG_SHOW``: the ticket is real and unscanned, but its
      ``TicketType.show_id`` doesn't match the show being scanned for.
    - ``UNPAID``: the ticket is real, for the right show, and unscanned,
      but its ``Order.status`` isn't ``paid`` — the brief's distinct third
      UI state. Does NOT mark the ticket scanned; entry is only completed
      by a later, separate scan of the same QR code once an admin has used
      the existing ``POST /api/v1/orders/{order_id}/mark-paid`` route to
      settle payment.
    """

    PASS = "pass"
    ALREADY_SCANNED = "already_scanned"
    INVALID = "invalid"
    WRONG_SHOW = "wrong_show"
    UNPAID = "unpaid"


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
