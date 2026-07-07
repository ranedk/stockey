#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

"${SCRIPT_DIR}/scripts/run_with_markers.sh" "ohlcv_reconcile" "${PYTHON_BIN}" -m data.dhanlive.ohlcv_reconcile ${OHLCV_RECONCILE_ARGS:-} "$@"

# Cross-sectional relative-strength percentiles ride the same slot: freshly reconciled
# bars in, full-market RS ranks out (the selection layer for the max-positions slots).
# Non-fatal: the pipeline treats missing RS as neutral.
if [[ "${SKIP_RELATIVE_STRENGTH:-0}" == "1" || "${SKIP_RELATIVE_STRENGTH:-false}" == "true" ]]; then
  echo "[all_ohlcv_reconcile] relative strength skipped SKIP_RELATIVE_STRENGTH=${SKIP_RELATIVE_STRENGTH}"
else
  "${SCRIPT_DIR}/scripts/run_with_markers.sh" "relative_strength" "${PYTHON_BIN}" -m advisory.relative_strength || echo "[all_ohlcv_reconcile] relative strength failed (non-fatal)" >&2
fi
