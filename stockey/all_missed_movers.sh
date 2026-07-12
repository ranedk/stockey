#!/usr/bin/env bash

# Post-market research analyser: classify the day's big movers by their relationship to our
# funnel (no_signal / blocked_by_gate / watched_not_triggered / captured) to guide signal
# development. Review-only; never trades, never changes thresholds.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

exec "${SCRIPT_DIR}/scripts/run_with_markers.sh" "missed_movers" "${PYTHON_BIN}" -m advisory.missed_movers_analyzer "$@"
