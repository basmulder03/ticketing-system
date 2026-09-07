# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
once it reaches a tagged release.

## [Unreleased]

### Added

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
