#!/usr/bin/env bash
# Re-run the (idempotent) seed script against the running dev stack.
set -euo pipefail
cd "$(dirname "$0")/.."
docker-compose exec -T app python scripts/seed.py
