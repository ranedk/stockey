#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

# Research-only: RS-selected, vol-sized, crash-floored paper decisions committed and scored vs NIFTY.
# --persist writes advisory_paper_decision_loop only; never the action queue or broker.
exec "${SCRIPT_DIR}/scripts/run_with_markers.sh" "paper_decision_loop" "${PYTHON_BIN}" -m advisory.paper_decision_loop --persist "$@"
