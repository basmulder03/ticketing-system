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

## Running the test suite locally

```bash
# with the dev stack running (see README "Quickstart")
docker-compose exec app pytest

# or, outside docker, with dependencies installed locally
pip install -e ".[dev]"
pytest
```

Type checking:

```bash
docker-compose exec app mypy app tests
# or locally
mypy app tests
```

## Getting started

See the [README](./README.md#quickstart) for how to bring up the full local
dev stack (app + Postgres + Mailpit) with one command.

## Reporting bugs / requesting features

Use the issue templates under `.github/ISSUE_TEMPLATE/`. Security
vulnerabilities should **not** be filed as public issues — see
[`SECURITY.md`](./SECURITY.md).
