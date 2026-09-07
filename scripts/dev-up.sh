#!/usr/bin/env bash
# One-command local dev bootstrap: starts app + Postgres + Mailpit,
# app's entrypoint runs migrations automatically. Reseed separately via
# scripts/dev-reseed.sh once backend-builder implements scripts/seed.py.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "[dev-up] .env not found, copying from .env.example"
  cp .env.example .env
fi

docker-compose up --build "$@"
