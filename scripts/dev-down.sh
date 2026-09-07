#!/usr/bin/env bash
# Stop the local dev stack (keeps DB volume — use dev-reset-db.sh to wipe it).
set -euo pipefail
cd "$(dirname "$0")/.."
docker-compose down
