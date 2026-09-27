#!/usr/bin/env bash
# One-command local dev bootstrap: starts app + Postgres + Mailpit,
# app's entrypoint runs migrations automatically. Seed demo data with
# scripts/dev-reseed.sh.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "[dev-up] .env not found, copying from .env.example"
  cp .env.example .env
fi

docker-compose up --build "$@"
