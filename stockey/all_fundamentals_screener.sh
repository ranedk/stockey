#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

# Fundamental screener pipeline (docs/FUNDAMENTAL_SCREENER_PRD.md): L1/L2 refresh,
# event collectors, OCR + structured extraction, sector capital-cycle, L3 alerts
# (rule + LLM triage), descriptive technicals, and the watchlist/narrative/email
# pipeline -- run in dependency order by fundamentals/run_pipeline.py. One step
# failing does not abort the run (see that module's docstring); this script's own
# exit code only goes non-zero if every step failed.
exec "${SCRIPT_DIR}/scripts/run_with_markers.sh" "fundamentals_screener" "${PYTHON_BIN}" -m fundamentals.run_pipeline "$@"
