#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

# Fundamentals screener API (fundamentals/api/app.py), serving the screener/ Nuxt
# frontend. Long-running service kept alive the same way the old all_frontend.sh did
# (see the stockey-service-management memory / 2026-07-09 incident): cron respawns
# this every few minutes under scripts/with_lock.sh, and with_lock's own directory
# lock means a second invocation while a real instance holds it just exits without
# starting a rival. This script adds a second, cheaper check on top -- if something is
# already answering /api/health on the target host:port, exit 0 immediately without
# even trying to bind, so a lingering process that outlived its lock can't cause a
# bind-failure crash-loop.

FUNDAMENTALS_API_HOST="${FUNDAMENTALS_API_HOST:-127.0.0.1}"
FUNDAMENTALS_API_PORT="${FUNDAMENTALS_API_PORT:-8000}"

if "${PYTHON_BIN}" -c "
import sys, urllib.request
try:
    urllib.request.urlopen('http://${FUNDAMENTALS_API_HOST}:${FUNDAMENTALS_API_PORT}/api/health', timeout=2)
except Exception:
    sys.exit(1)
"; then
  echo "[fundamentals_api] already healthy on ${FUNDAMENTALS_API_HOST}:${FUNDAMENTALS_API_PORT}, exiting" >&2
  exit 0
fi

exec "${SCRIPT_DIR}/scripts/run_with_markers.sh" "fundamentals_api" "${PYTHON_BIN}" -m uvicorn fundamentals.api.app:app --host "${FUNDAMENTALS_API_HOST}" --port "${FUNDAMENTALS_API_PORT}"
