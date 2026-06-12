#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

ARGS=("$@")
if [[ "${#ARGS[@]}" -eq 0 ]]; then
  ARGS=(--skip-dhan)
fi

exec "${SCRIPT_DIR}/scripts/run_with_markers.sh" "operator_health" "${PYTHON_BIN}" -m advisory.operator_health "${ARGS[@]}"
