#!/usr/bin/env bash
# Full DB reset: drops the Postgres volume, brings the stack back up
# (entrypoint re-runs migrations from scratch), then reseeds.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "[dev-reset-db] stopping stack and removing DB volume..."
docker-compose down -v

echo "[dev-reset-db] starting stack (migrations run automatically)..."
docker-compose up --build -d

echo "[dev-reset-db] waiting for app to become healthy..."
for _ in $(seq 1 60); do
  if curl -sf http://localhost:8000/healthz > /dev/null 2>&1; then
    break
  fi
  sleep 1
done

echo "[dev-reset-db] reseeding..."
docker-compose exec -T app python scripts/seed.py

echo "[dev-reset-db] done."
