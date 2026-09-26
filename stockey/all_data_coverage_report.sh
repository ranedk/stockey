#!/usr/bin/env bash

# Daily coverage/staleness report across every KEEP table (docs/DATA_COVERAGE.md).
# Non-fatal: reports "error" status in the log/DB when a table is stale, but does
# not gate the pipeline the way all_data_readiness.sh's --require does.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

"${SCRIPT_DIR}/scripts/run_with_markers.sh" "data_coverage_report" "${PYTHON_BIN}" -m scripts.data_coverage_report "$@"
