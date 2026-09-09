# Contributing to Beacon

Thanks for your interest in contributing. This project is described in full
in [`PROJECT_BRIEF.md`](./PROJECT_BRIEF.md) — read that first for context on
scope, architecture, and the build-order roadmap.

## Branch & PR workflow

- `main` is protected — all work happens on a feature branch and lands via
  pull request, never a direct commit to `main`.
- Branch naming: `milestone-<n>/<short-description>` (e.g.
  `milestone-1/event-crud`) for roadmap work, or `fix/<short-description>`
  / `docs/<short-description>` for smaller ad hoc changes.
- Keep commits small and atomic — one coherent change per commit. Use
  [Conventional Commits](https://www.conventionalcommits.org/) prefixes:
  `feat:`, `fix:`, `docs:`, `test:`, `chore:`, `refactor:`.
- Each PR description should state what was built/changed, why, and which
  milestone/requirement from `PROJECT_BRIEF.md` it addresses.
- CI (tests + type-check) must pass before merge.

## Coding standards

- **KISS and DRY** — prefer the simplest implementation that satisfies a
  requirement over a clever or speculatively "flexible" one. Extract shared
  logic instead of duplicating it, but don't abstract prematurely for
  hypothetical needs outside the brief.
- **Full type hints** on all Python code — function signatures, class
  attributes, return types. This is enforced in CI via `mypy`.
- **Docstrings** on all public functions/classes/modules explaining purpose
  and any non-obvious behavior. Inline comments only where the "why" isn't
  obvious from the code itself.
- **Tests are part of the definition of done** — write tests alongside the
  feature, not after. See the Testing section of `PROJECT_BRIEF.md` for
  what's expected at unit/integration/e2e level for each area of the app.
- Parameterized queries / ORM only — never raw string-built SQL.
- Keep the README and `PROJECT_BRIEF.md` in sync with what's actually been
  built; if scope shifts during implementation, update the brief rather than
  letting it go stale.

## Running the app and test suite locally

**Inside docker-compose** (simplest, no local setup — see README "Quickstart"):

```bash
docker-compose up --build            # app + Postgres + Mailpit, hot reload via the bind mount
docker-compose exec app pytest       # or: docker-compose run --rm app pytest
docker-compose exec app mypy app tests
docker-compose exec app ruff check app tests
```

**Natively, in a venv** (faster iteration — no per-command container-start
cost, and uvicorn's own `--reload` file-watcher is snappier than the bind-
mount version; this is also exactly what CI does, so it's a good way to
reproduce a CI failure locally). Only Postgres and Mailpit run in Docker
here — the app/test process itself runs on the host:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/playwright install chromium   # needed for the accessibility test suite
```

weasyprint needs a few system libraries pip can't install — on
Debian/Ubuntu:

```bash
sudo apt-get install -y --no-install-recommends \
  libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 \
  libcairo2 libffi8 shared-mime-info fonts-dejavu-core
```

(exact list also in `.github/workflows/ci.yml`; on macOS, `brew install
pango cairo gdk-pixbuf` covers the same libraries).

Two wrapper scripts bring up just DB + Mailpit (not the app container)
and set the environment overrides a native run needs (`DATABASE_URL`
pointed at the compose Postgres's host port, `SEED_SMTP_HOST=localhost`
instead of the docker-network `mailpit` hostname, a repo-relative
`UPLOADS_DIR`):

```bash
./scripts/dev-native-up.sh                   # runs the app itself: http://localhost:8000, --reload

./scripts/dev-native-test.sh                 # pytest
./scripts/dev-native-test.sh mypy app tests
./scripts/dev-native-test.sh ruff check app tests
./scripts/dev-native-test.sh pytest -k checkout -v
```

(equivalent to `docker-compose up -d db mailpit`, then running
`.venv/bin/uvicorn app.main:app --reload` / `.venv/bin/<cmd>` with those env
vars set yourself, if you'd rather not use the wrappers.)

Whichever way you seed data, `scripts/seed.py` itself needs the same env
vars if run natively: `SEED_SMTP_HOST=localhost .venv/bin/python scripts/seed.py`.

**A gotcha to know about, not worry about:** the app container runs as
root, and `docker-compose exec/run app <cmd>` can leave root-owned files
in the repo working tree (`.mypy_cache/`, `.ruff_cache/`, `beacon.egg-info/`,
stray build artifacts) via the bind mount — these then block a native
`pip install -e` or `mypy`/`ruff` cache write with a permission error. If
you hit `PermissionError`/`readonly database`/`Cannot update time stamp`
switching between docker and native runs, clear the offending directory
via a container (which owns it) rather than fighting `sudo`:

```bash
docker-compose run --rm app rm -rf /app/.mypy_cache /app/.ruff_cache /app/*.egg-info
```

Theme image uploads (`UPLOADS_DIR`) don't have this problem inside
docker-compose — they're a named Docker volume (`beacon_uploads`), not a
bind-mounted path, specifically so they never land in the git working
tree at all.

## Load-testing the sales-live moment

Separate from the automated test suite: `scripts/loadtest/` has a
Locust-based load test for `POST /api/v1/public/checkout` against a real
running instance, for a human operator to run before a real event's
sales-live moment (Milestone 9). Not wired into CI — it's a manual,
occasional operational exercise, not a correctness test. See
[`scripts/loadtest/README.md`](./scripts/loadtest/README.md) for setup and
how to interpret results.

## Getting started

See the [README](./README.md#quickstart) for how to bring up the full local
dev stack (app + Postgres + Mailpit) with one command.

## Reporting bugs / requesting features

Use the issue templates under `.github/ISSUE_TEMPLATE/`. Security
vulnerabilities should **not** be filed as public issues — see
[`SECURITY.md`](./SECURITY.md).
