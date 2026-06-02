#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

"${SCRIPT_DIR}/scripts/run_with_markers.sh" "all_advisory" "${PYTHON_BIN}" -m advisory.master_pipeline --skip-downloads "$@"

"${SCRIPT_DIR}/scripts/run_with_markers.sh" "operator_snapshot" "${PYTHON_BIN}" -m advisory.operator_snapshot

"${SCRIPT_DIR}/scripts/run_with_markers.sh" "trace_summary_store" "${PYTHON_BIN}" -m advisory.trace_summary_store --symbol-limit 150 --event-limit 150 --trace-limit 100 --format text
