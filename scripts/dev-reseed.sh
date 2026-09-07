#!/usr/bin/env bash
# Re-run the seed script against the running dev stack (idempotent once
# backend-builder implements scripts/seed.py for real).
set -euo pipefail
cd "$(dirname "$0")/.."
docker-compose exec -T app python scripts/seed.py
