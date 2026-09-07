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

> **Status:** Milestone 0 (Foundations) — project skeleton, local dev
> stack, and CI are in place. No business logic (auth, models, routes)
> exists yet; see `PROJECT_BRIEF.md`'s "Build order" section for what's
> next.

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

Once models/seed data exist (Milestone 1+), the stack will also leave at
least one demo Event/Show/TicketType ready to click through immediately.
Until then, `/healthz` is the only route.

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
secrets (SMTP passwords, Mollie keys) at rest — generate a real one for
staging/production (`openssl rand -hex 32`), never reuse the dev
placeholder outside local dev.

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

## Project layout

```
app/                  FastAPI application package
  core/config.py       Settings (env-driven)
  db/                  SQLAlchemy engine/session, declarative Base
  api/                 Routers (empty — Milestone 1+)
  templates/, static/  Jinja2 templates / static assets (empty — Milestone 2+)
  i18n/                EN/NL key-based translation dictionaries
alembic/               DB migrations (empty initial revision so far)
scripts/
  seed.py               Demo data seed script (stub — Milestone 1+)
  dev-up.sh, dev-down.sh, dev-reseed.sh, dev-reset-db.sh
tests/                 pytest suite
docker/entrypoint.sh   Waits for DB, runs migrations, then execs uvicorn
```

## Contributing

See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for branch/PR workflow, coding
standards, and how to run tests locally. Security issues should go through
[`SECURITY.md`](./SECURITY.md), not a public issue.

## License

MIT — see [`LICENSE`](./LICENSE).
