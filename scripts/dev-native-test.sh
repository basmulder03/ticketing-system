#!/usr/bin/env bash
# Run the test suite (or mypy/ruff) natively against a local .venv,
# instead of inside the app docker-compose container — see CONTRIBUTING.md
# "Running natively (faster local iteration)". Only Postgres and Mailpit
# run in Docker; the app/test process itself runs on the host, so there's
# no per-command container-start cost.
#
# Usage:
#   scripts/dev-native-test.sh                 # pytest (default)
#   scripts/dev-native-test.sh mypy app tests
#   scripts/dev-native-test.sh ruff check app tests
#   scripts/dev-native-test.sh pytest -k checkout -v
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  echo "[dev-native-test] .venv not found — see CONTRIBUTING.md to create it first." >&2
  exit 1
fi

docker-compose up -d db mailpit

export DATABASE_URL="${DATABASE_URL:-postgresql+asyncpg://beacon:beacon@localhost:5433/beacon}"
export UPLOADS_DIR="${UPLOADS_DIR:-uploads}"
export SEED_SMTP_HOST="${SEED_SMTP_HOST:-localhost}"
export SEED_SMTP_PORT="${SEED_SMTP_PORT:-1025}"
mkdir -p "$UPLOADS_DIR"

cmd=("${@:-pytest}")
if [ "$#" -eq 0 ]; then
  cmd=(pytest -q)
fi

.venv/bin/alembic upgrade head
exec .venv/bin/"${cmd[@]}"
