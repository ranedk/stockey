#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

ADVISORY_ARGS=(--skip-downloads)

if [[ "${ADVISORY_PARALLEL_LOCAL_STAGES:-1}" != "0" && "${ADVISORY_PARALLEL_LOCAL_STAGES:-true}" != "false" ]]; then
  ADVISORY_ARGS+=(--parallel-local-stages --local-stage-workers "${ADVISORY_LOCAL_STAGE_WORKERS:-3}")
fi

if [[ "${ADVISORY_DISABLE_RULE_REPAIR:-1}" != "0" && "${ADVISORY_DISABLE_RULE_REPAIR:-true}" != "false" ]]; then
  ADVISORY_ARGS+=(--skip-rule-snapshot-refresh --skip-intraday-prefetch)
fi

"${SCRIPT_DIR}/scripts/run_with_markers.sh" "all_advisory" "${PYTHON_BIN}" -m advisory.master_pipeline "${ADVISORY_ARGS[@]}" "$@"

"${SCRIPT_DIR}/scripts/run_with_markers.sh" "operator_snapshot" "${PYTHON_BIN}" -m advisory.operator_snapshot

"${SCRIPT_DIR}/scripts/run_with_markers.sh" "trace_summary_store" "${PYTHON_BIN}" -m advisory.trace_summary_store --symbol-limit 150 --event-limit 150 --trace-limit 100 --format text
