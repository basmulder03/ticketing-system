#!/usr/bin/env bash
# Run the app server natively (hot reload via uvicorn --reload), using
# docker-compose only for Postgres + Mailpit — no app container. Faster
# edit/reload loop than docker-compose's bind-mount reload, at the cost of
# needing the native venv set up first (see CONTRIBUTING.md "Running
# natively").
#
# Usage: scripts/dev-native-up.sh [extra uvicorn args]
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  echo "[dev-native-up] .venv not found — see CONTRIBUTING.md to create it first." >&2
  exit 1
fi

if [ ! -f .env ]; then
  echo "[dev-native-up] .env not found, copying from .env.example"
  cp .env.example .env
fi

docker-compose up -d db mailpit

export DATABASE_URL="${DATABASE_URL:-postgresql+asyncpg://beacon:beacon@localhost:5433/beacon}"
export UPLOADS_DIR="${UPLOADS_DIR:-uploads}"
export SEED_SMTP_HOST="${SEED_SMTP_HOST:-localhost}"
export SEED_SMTP_PORT="${SEED_SMTP_PORT:-1025}"
mkdir -p "$UPLOADS_DIR"

echo "[dev-native-up] waiting for database..."
.venv/bin/python - <<'PY'
import asyncio
import sys

from sqlalchemy import text

from app.db.session import engine


async def wait() -> None:
    for attempt in range(30):
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return
        except Exception as exc:  # noqa: BLE001 - retry loop, any error means "not ready"
            print(f"[dev-native-up] db not ready ({exc}), retrying ({attempt + 1}/30)...", file=sys.stderr)
            await asyncio.sleep(1)
    print("[dev-native-up] database never became ready", file=sys.stderr)
    sys.exit(1)


asyncio.run(wait())
PY

echo "[dev-native-up] running migrations..."
.venv/bin/alembic upgrade head

echo "[dev-native-up] starting app — http://localhost:8000, Mailpit UI http://localhost:8025"
exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload "$@"
