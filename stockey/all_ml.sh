#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

if [[ "${ML_SKIP_DATA_READINESS:-0}" != "1" && "${ML_SKIP_DATA_READINESS:-false}" != "true" ]]; then
  "${SCRIPT_DIR}/scripts/run_with_markers.sh" "ml_data_readiness" "${PYTHON_BIN}" -m advisory.data_readiness --require
fi
exec "${SCRIPT_DIR}/scripts/run_with_markers.sh" "all_ml" "${PYTHON_BIN}" -m advisory.model_training_runner "$@"
