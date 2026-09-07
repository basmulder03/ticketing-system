# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
once it reaches a tagged release.

## [Unreleased]

### Added

- Milestone 1.5 (Event Theming): `Theme` model (1:1 with `Event`, fixed
  `primary_color`/`secondary_color`/`accent_color`/`font_choice` fields,
  optional sanitized `custom_css`, draft/published `status`) and CRUD
  routes under `/api/v1/events/{event_id}/theme` (agent-accessible, same
  as Event/Show/TicketType).
- Default-deny custom-CSS sanitizer (`app/core/css_sanitizer.py`, built on
  `tinycss2`): strips `@import`, external `url()` references,
  `position: fixed`/`sticky`, and any selector not scoped under
  `.event-content`; used identically by the real save path and the new
  preview endpoint.
- WCAG 2.1 AA contrast checker (`app/services/contrast.py`) run against a
  theme's three fixed colors, returned as a structured
  pass/fail-per-pair report on every Theme response.
- Local-filesystem logo/background image uploads
  (`app/services/theme_images.py`): content-type + magic-byte validation,
  size cap (`THEME_UPLOAD_MAX_BYTES`), served back out via a new
  `/uploads` static mount; stored under `UPLOADS_DIR`, now its own docker
  volume (`beacon_uploads`) so uploads survive container rebuilds.
- "Duplicate theme from previous event" action
  (`POST .../theme/copy-from/{source_event_id}`) and a live-preview
  endpoint (`POST .../theme/preview`) that sanitizes arbitrary draft
  custom CSS and returns a ready-to-render sample block, without
  persisting anything.
- Alembic revision `0004` for the `themes` table.
- Milestone 1 (Backoffice core): `Event` (name, slug, description,
  draft/published status, event-wide `sales_paused` override),
  `EventConfig` (1:1 with Event — SMTP settings, Mollie test/live keys via
  `EncryptedString`, invoice/company details + numbering prefix,
  sales-live datetime, enabled payment methods; secrets are write-only
  over the API, exposed only as `*_is_set` booleans), `Show` (belongs to
  Event: date, doors/start time, venue, capacity, draft/published status),
  and `TicketType` (belongs to Show: name, price, service-fee toggle,
  `quantity_available`, a `remaining` property currently equal to
  `quantity_available` pending Milestone 3's stock-decrement logic).
- `require_admin_or_agent` dependency (`app/api/deps.py`): admits admin
  humans and agent principals (never scanner) to Event/Show/TicketType
  content routes, per the brief's agent content-management scope.
  `EventConfig` routes stay strictly `require_admin`-only.
- EventConfig connection-test actions (`POST .../event-config/test-email`
  via `aiosmtplib`, `POST .../event-config/test-mollie` via `httpx`) and a
  "copy configuration from previous event" action.
- Alembic revision `0003` for `events`, `event_configs`, `shows`, and
  `ticket_types`.
- Project skeleton: FastAPI app factory with a `/healthz` route, package
  layout under `app/`, Alembic migration scaffolding.
- Local dev stack: `docker-compose.yml` with app (hot reload), PostgreSQL,
  and Mailpit (SMTP sink); one-command bootstrap via
  `./scripts/dev-up.sh`.
- Seed script stub (`scripts/seed.py`) — entrypoint reserved for
  `backend-builder` to populate a demo Event/Show/TicketType once models
  exist.
- CI pipeline (GitHub Actions): pytest job and mypy type-check job, run on
  every push/PR.
- Open-source repo hygiene docs: README, LICENSE (MIT), CONTRIBUTING,
  CODE_OF_CONDUCT, SECURITY, issue templates, PR template.
- `.env.example` with local-dev-safe placeholder values (Mailpit SMTP,
  Mollie test-key placeholder, compose Postgres DSN).
- Encrypted-secrets primitive: `app/core/crypto.py` (Fernet, keyed off
  `ENCRYPTION_KEY`) and a reusable `EncryptedString` SQLAlchemy column
  type, ready for `EventConfig`'s SMTP/Mollie credentials in a later
  milestone.
- Admin session auth: `AdminUser` model (argon2-hashed passwords, `admin`/
  `scanner` roles), signed/timed-out session cookies
  (`SESSION_TIMEOUT_MINUTES`), `POST /api/v1/auth/login`,
  `POST /api/v1/auth/logout`, `GET /api/v1/auth/me`.
- Agent API-key auth: `AgentAccount` model (named, individually revocable,
  SHA-256-hashed keys), authenticated via the `X-Agent-Api-Key` header.
  Admin-only management routes: `POST`/`GET /api/v1/admin/agent-accounts`,
  `POST /api/v1/admin/agent-accounts/{id}/revoke`.
- Agent-scoping enforcement: `app/api/deps.py`'s `require_admin`
  dependency, which agent principals can never satisfy — used by every
  admin-only route (agent-account management, the audit log, and future
  payment/SMTP/admin-user/financial routes).
- Audit log: `AuditLogEntry` model + `app/services/audit.py` writer,
  recording actor type (`human`/`ai_agent`) and actor name on every login
  and agent-account create/revoke; viewable (admin-only) via
  `GET /api/v1/admin/audit-log`.
- In-memory per-IP rate limiting (`app/core/rate_limit.py`), applied to
  the login endpoint and every agent-API-key-authenticated request.
- Alembic revision `0002` for the above tables.
