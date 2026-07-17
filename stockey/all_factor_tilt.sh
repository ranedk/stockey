#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

# Selection-shadow measurement: pure-RS top-N vs the PROPOSED graduated tilt, forward-excess delta. Cheap
# SQL short-circuit skips the heavy rebuild until a factor is actually `active`. Research/report-only.
exec "${SCRIPT_DIR}/scripts/run_with_markers.sh" "factor_tilt" "${PYTHON_BIN}" -m advisory.factor_tilt --measure "$@"
