#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

# Economic Times RSS news, hourly (fundamentals/collectors/et_news.py, 2026-09-29).
# Deliberately NOT gated on .pause_fundamentals: each feed keeps only its latest 50
# items, so skipped hours lose news permanently. Collection only; no decisions.
exec "${SCRIPT_DIR}/scripts/run_with_markers.sh" "fundamentals_news" "${PYTHON_BIN}" -m fundamentals.collectors.et_news "$@"
