#!/usr/bin/env bash
set -euo pipefail

cd /app
mkdir -p "${DATA_DIR:-/data}"
alembic upgrade head

# UVICORN_EXTRA_ARGS is intentionally unquoted so it can hold several flags (e.g. --reload)
# shellcheck disable=SC2086
exec uvicorn roomfinder.main:app --host 0.0.0.0 --port 8000 --no-server-header ${UVICORN_EXTRA_ARGS:-}
