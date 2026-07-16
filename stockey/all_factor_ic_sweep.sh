#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

# Factor-IC sweep + edge-drift monitor (the confidence instrument); FDR-gated, descriptive/report-only.
# Heavy faithful rebuild -- scheduled weekly, not daily.
exec "${SCRIPT_DIR}/scripts/run_with_markers.sh" "factor_ic_sweep" "${PYTHON_BIN}" -m advisory.factor_ic_sweep "$@"
