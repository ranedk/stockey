#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

"${SCRIPT_DIR}/scripts/run_with_markers.sh" "ohlcv_reconcile" "${PYTHON_BIN}" -m data.dhanlive.ohlcv_reconcile ${OHLCV_RECONCILE_ARGS:-} "$@"
