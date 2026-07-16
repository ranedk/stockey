#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

# Factor graduation harness -- disciplined state machine over the monitor snapshot; proposal-only by
# default (FACTOR_GRADUATION_APPLY_ENABLED defaults OFF). Light (reads the persisted sweep table).
exec "${SCRIPT_DIR}/scripts/run_with_markers.sh" "factor_graduation" "${PYTHON_BIN}" -m advisory.factor_graduation "$@"
