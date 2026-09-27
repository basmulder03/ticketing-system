# Beacon

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![CI](https://github.com/basmulder03/ticketing-system/actions/workflows/ci.yml/badge.svg)](https://github.com/basmulder03/ticketing-system/actions/workflows/ci.yml)

Beacon is a lightweight, self-hosted, open-source ticketing web app for live
theater/music events — a single application covering the public ticket
site, a backoffice, and a mobile door-scanning app. It's built as a
**generic, reusable platform**: any Event can be configured in it, each
with its own theme, SMTP settings, and Mollie payment credentials.

The full product spec — entities, required functionality, security/ops
requirements, build order, and contributor-agent responsibilities — lives
in [`PROJECT_BRIEF.md`](./PROJECT_BRIEF.md). Read that first for context on
what's in scope and why.

> **Status:** Through Milestone 1.5 (Event Theming). Foundations
> (Milestone 0: project skeleton, local dev stack, CI, encrypted-secrets
> primitive, admin session auth, agent API-key auth with scope
> enforcement, audit log), Backoffice core (Milestone 1: Event/EventConfig/
> Show/TicketType CRUD, connection-test actions), and Event Theming
> (Milestone 1.5: Theme CRUD, sanitized custom-CSS override, AA contrast
> checking, logo/background image uploads, live-preview endpoint) are in
> place, all API-only so far — no backoffice UI or public-site templates
> exist yet (`frontend-theming`'s next scope). See `PROJECT_BRIEF.md`'s
> "Build order" section for what's next.

## Stack

- **Backend:** FastAPI (Python 3.12), SQLAlchemy 2.0 (async) + Alembic
- **DB:** PostgreSQL
- **Frontend:** Server-rendered Jinja2 + HTMX (no SPA framework)
- **QR codes:** `qrcode`
- **PDFs (tickets/invoices):** `weasyprint`
- **Email:** `aiosmtplib`, per-Event SMTP configuration
- **Payments:** Mollie Payments API
- **Dependency/packaging:** `pyproject.toml` (PEP 621 + setuptools), no
  Poetry/pipenv — kept to stdlib packaging tooling for simplicity

## Quickstart (local dev)

Requires Docker and `docker-compose`. No real SMTP/Mollie credentials
needed — everything works out of the box against local defaults.

```bash
git clone https://github.com/basmulder03/ticketing-system.git
cd ticketing-system
./scripts/dev-up.sh
```

This copies `.env.example` to `.env` (if not already present), then builds
and starts three containers:

| Service   | Purpose                                   | URL                          |
| --------- | ------------------------------------------ | ----------------------------- |
| `app`     | FastAPI app (hot reload via `uvicorn --reload`) | http://localhost:8000/healthz |
| `db`      | PostgreSQL 16                              | `localhost:5433` (mapped from container's 5432, to avoid clashing with a local Postgres) |
| `mailpit` | SMTP sink — catches every email the app sends | Web UI: http://localhost:8025 |

On startup, the `app` container automatically waits for Postgres and runs
`alembic upgrade head` before starting the server — no separate migration
step needed.

`scripts/seed.py` runs automatically as part of `dev-up.sh`/`dev-reseed.sh`
and creates one demo `AdminUser` (see "Auth" below). Event/Show/TicketType
demo seed data is added in Milestone 1+.

### Auth

- **Admin login:** `POST /api/v1/auth/login` with `{"email", "password"}`
  sets a signed, `httponly` session cookie (`beacon_admin_session`),
  timed out after `SESSION_TIMEOUT_MINUTES` (default 30). `POST
  /api/v1/auth/logout` clears it; `GET /api/v1/auth/me` returns the
  current principal (admin or agent).
- **Initial admin account:** a fresh deployment with no `AdminUser` at
  all yet is set up in the browser — visiting `/login` (or `/setup`
  directly) redirects to a one-time "create your admin account" form
  (`POST /api/v1/auth/setup`), which also logs you straight in. Once any
  admin account exists, that route permanently 409s; every account after
  that is created from inside the backoffice itself
  (`POST /api/v1/admin/admin-users`, admin-only). Local dev still also
  seeds a demo admin from `SEED_ADMIN_EMAIL`/`SEED_ADMIN_PASSWORD`
  (defaults: `admin@beacon.local` / `dev-only-change-me-123`) via
  `scripts/seed.py` for a faster edit/reload loop — that script is a dev
  convenience only, not how a real deployment gets its first admin.
- **Agent (AI) auth:** a separate API-key path — send the raw key on the
  `X-Agent-Api-Key` header. Keys are created via
  `POST /api/v1/admin/agent-accounts` (admin-only; the raw key is shown
  exactly once) and revoked via
  `POST /api/v1/admin/agent-accounts/{id}/revoke`. Agent keys can never
  reach admin-only routes (agent-account management, the audit log, and —
  in later milestones — payment/SMTP credentials, admin user management,
  financial data) — enforced by the `require_admin` dependency in
  `app/api/deps.py`, not just documented.
- **Audit log:** every login and every agent-account create/revoke is
  recorded with an explicit actor type (`human` or `ai_agent`) and actor
  name — never a generic "system" actor. View recent entries via
  `GET /api/v1/admin/audit-log` (admin-only).
- Both auth endpoints are rate-limited per client IP (in-memory, see
  `app/core/rate_limit.py` — single-process only, adequate for this app's
  single-small-VPS target; not shared across multiple workers/replicas).

### Theming

Each Event has one Theme (`/api/v1/events/{event_id}/theme`, both admin and
agent principals can read/write it — same access as Event/Show/TicketType):

- Fixed fields: `primary_color`/`secondary_color`/`accent_color` (hex),
  `font_choice` (a curated enum, not free text — see
  `app.models.enums.ThemeFont`), `status` (draft/published).
- Logo/background images: `PUT .../theme/logo` / `PUT .../theme/background`
  (multipart upload, PNG/JPEG/WEBP only, size-capped by
  `THEME_UPLOAD_MAX_BYTES`) — stored on local disk under `UPLOADS_DIR`
  (its own docker volume, see `docker-compose.yml`) and served back out at
  `/uploads/...`; `DELETE` clears either slot.
- Optional advanced `custom_css`: sanitized server-side before storage —
  see `app.core.css_sanitizer.sanitize_custom_css` for exactly what's
  stripped (`@import`, external `url()` references, `position: fixed`/
  `sticky`, anything not scoped under the `.event-content` container) and
  why. Every Theme response includes `is_custom_css_active` (custom CSS
  can't be reliably auto-audited for contrast, so the backoffice UI should
  show a warning banner whenever this is true) and a computed
  `contrast_report` (WCAG 2.1 AA ratios for the three fixed colors).
- `POST .../theme/copy-from/{source_event_id}` duplicates another event's
  theme (fixed fields + custom CSS; images are not copied — re-upload per
  event).
- `POST .../theme/preview` renders arbitrary, not-yet-saved theme values
  (including custom CSS, sanitized through the exact same code path as the
  real save) into a small sample HTML block + CSS, for a live preview pane
  — nothing is persisted.

### Where to view sent emails

The app's local dev SMTP config points at **Mailpit** — open
**http://localhost:8025** to see every email the app sends (tickets,
invoices, confirmations) without any real inbox involved.

### Resetting / reseeding the database

```bash
# Re-run the seed script against the current DB (idempotent once implemented)
./scripts/dev-reseed.sh

# Full reset: drops the DB volume, re-runs migrations from scratch, reseeds
./scripts/dev-reset-db.sh
```

### Stopping the stack

```bash
./scripts/dev-down.sh          # stops containers, keeps the DB volume
docker-compose down -v         # stops containers and deletes the DB volume
```

### Running tests / type-checking manually

```bash
docker-compose exec app pytest
docker-compose exec app mypy app
```

The same commands run in CI on every push/PR (see
[`.github/workflows/ci.yml`](./.github/workflows/ci.yml)).

## Environments & credentials

**Nothing in this repo is a real secret.** `.env.example` contains only
placeholder values safe to commit to a public repo; copy it to `.env` for
local dev (`dev-up.sh` does this automatically).

Per `PROJECT_BRIEF.md`, SMTP and Mollie credentials are **not** global
application settings — they live on each Event's `EventConfig`, entered
through the backoffice once it exists (Milestone 1+). This means different
events can use entirely different email accounts or Mollie merchant
accounts without any code or redeploy.

| Environment | SMTP                          | Mollie                  |
| ----------- | ------------------------------ | ------------------------ |
| Local dev   | Mailpit (`mailpit:1025`, no auth) | Mollie **test**-mode key |
| Staging     | Mailpit or a real provider in sandbox mode | Mollie **test**-mode key |
| Production  | Real provider (e.g. one.com `send.one.com`) | Mollie **live** key      |

The `SEED_*` variables in `.env`/`.env.example` are only used by
`scripts/seed.py` to populate the *demo* Event's `EventConfig` with
Mailpit/test-key defaults — they are not read by the running app for real
events.

`ENCRYPTION_KEY` (in `.env`) is the key used to encrypt `EventConfig`
secrets (SMTP passwords, Mollie keys) at rest via `app/core/crypto.py`'s
`EncryptedString` column type (Fernet, keyed off a SHA-256 derivation of
this value) — generate a real one for staging/production
(`openssl rand -hex 32`), never reuse the dev placeholder outside local
dev. `SECRET_KEY` similarly signs admin session cookies — rotate it for
staging/production too (rotating either key invalidates existing sessions
/ encrypted values, so treat both as real secrets even though this repo's
defaults are dev placeholders).

## Deployment (production)

Production target is an **existing** small Hetzner VPS that already runs
its own `docker-compose` setup (other services included). This app is
meant to be **added** to that setup, not deployed as if the VPS were
blank — see `PROJECT_BRIEF.md`'s "Deployment" section for the constraints
(lean images, no assuming ports 80/443 are free, no adding services beyond
what's needed).

**Not yet done:** the production compose extension itself is follow-up
work once the existing VPS compose file has been inspected (which reverse
proxy is already routing 80/443, which ports are already taken by other
services). This repo's `docker-compose.yml` is local-dev only — Postgres
config is intentionally unauthenticated/insecure defaults and Mailpit
replaces real SMTP, neither of which are safe for prod as-is.

## Load-testing the sales-live moment

Before a real event's sales-live moment, `scripts/loadtest/` has a
Locust-based load test that fires many concurrent real HTTP checkout
requests at a shared pool of scarce tickets against a real running
instance — measuring capacity/latency/error-rate, not just correctness
(the concurrency correctness test, `tests/integration/test_checkout_concurrency.py`,
already runs in CI on every push and is separate from this). See
[`scripts/loadtest/README.md`](./scripts/loadtest/README.md).

## Project layout

```
app/                  FastAPI application package
  core/config.py       Settings (env-driven)
  core/crypto.py       Fernet encryption primitive + EncryptedString column type
  core/security.py     Password hashing, agent API-key gen, session token signing
  core/rate_limit.py   In-memory per-IP rate limiter
  core/css_sanitizer.py Sanitizer for Theme custom CSS (default-deny, tinycss2-based)
  db/                  SQLAlchemy engine/session, declarative Base, shared mixins
  models/               AdminUser, AgentAccount, AuditLogEntry, Event, EventConfig,
                         Show, TicketType, Theme
  schemas/               Pydantic request/response models
  services/audit.py     Reusable audit-log writer
  services/contrast.py  WCAG 2.1 AA contrast-ratio checker (Theme fixed colors)
  services/theme_images.py   Local-filesystem storage for logo/background uploads
  services/theme_preview.py  Builds the Theme live-preview response
  api/deps.py           Auth dependencies + agent-scoping enforcement (require_admin)
  api/routes/            auth, agent_accounts, audit_log, events, event_configs,
                          shows, ticket_types, themes routers
  templates/, static/  Jinja2 templates / static assets (empty — Milestone 2+)
  i18n/                EN/NL key-based translation dictionaries
alembic/               DB migrations
scripts/
  seed.py               Demo data seed script (AdminUser + demo Event/Show/TicketType)
  dev-up.sh, dev-down.sh, dev-reseed.sh, dev-reset-db.sh, dev-native-up.sh, dev-native-test.sh
  loadtest/              Locust load test for the sales-live checkout moment (Milestone 9,
                          manual/occasional — not run in CI, see scripts/loadtest/README.md)
tests/                 pytest suite
docker/entrypoint.sh   Waits for DB, runs migrations, then execs uvicorn
```

## Contributing

See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for branch/PR workflow, coding
standards, and how to run tests locally. Security issues should go through
[`SECURITY.md`](./SECURITY.md), not a public issue.

## License

MIT — see [`LICENSE`](./LICENSE).
