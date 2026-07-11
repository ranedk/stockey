#!/usr/bin/env bash

# Nightly data catch-up + readiness fix pass (also runnable ad hoc). Checks
# bhavcopy / Dhan daily coverage / RS panel and runs bounded repairs for failures.
# Pass --require to gate (exit 1 on remaining hard errors).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

"${SCRIPT_DIR}/scripts/run_with_markers.sh" "data_readiness" "${PYTHON_BIN}" -m advisory.data_readiness --fix "$@"

# Event-driven fundamentals refresh: companies whose announcements were classified as
# results/dividend in the last few days get a targeted Sharpely statements pull.
# Non-fatal: sync failures are classified and recorded, never block the readiness pass.
"${SCRIPT_DIR}/scripts/run_with_markers.sh" "fundamentals_refresh" "${PYTHON_BIN}" -m advisory.fundamentals_refresh || echo "[all_data_readiness] fundamentals refresh failed (non-fatal)" >&2
