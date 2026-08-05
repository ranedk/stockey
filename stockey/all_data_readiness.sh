#!/usr/bin/env bash

# Nightly data catch-up + readiness fix pass (also runnable ad hoc). Checks
# bhavcopy / Dhan daily coverage / benchmark and runs bounded repairs for failures.
# Pass --require to gate (exit 1 on remaining hard errors).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

"${SCRIPT_DIR}/scripts/run_with_markers.sh" "data_readiness" "${PYTHON_BIN}" -m data.data_readiness --fix "$@"
