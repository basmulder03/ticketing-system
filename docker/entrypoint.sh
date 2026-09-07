#!/usr/bin/env bash
# Dev container entrypoint: wait for Postgres, run migrations, then exec
# whatever CMD was passed (uvicorn --reload).
set -euo pipefail

echo "[entrypoint] waiting for database..."
python - <<'PY'
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
            print(f"[entrypoint] db not ready ({exc}), retrying ({attempt + 1}/30)...", file=sys.stderr)
            await asyncio.sleep(1)
    print("[entrypoint] database never became ready", file=sys.stderr)
    sys.exit(1)


asyncio.run(wait())
PY

echo "[entrypoint] running migrations..."
alembic upgrade head

echo "[entrypoint] starting app..."
exec "$@"
