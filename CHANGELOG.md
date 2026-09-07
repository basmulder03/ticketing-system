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
