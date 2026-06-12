#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

exec "${SCRIPT_DIR}/scripts/run_with_markers.sh" "event_policy_evaluator" "${PYTHON_BIN}" -m advisory.event_policy_evaluator --horizons ${EVENT_POLICY_EVAL_HORIZONS:-5 10 20} --cost-bps "${EVENT_POLICY_EVAL_COST_BPS:-25}" --min-matured-rows "${EVENT_POLICY_EVAL_MIN_MATURED_ROWS:-10}" "$@"
