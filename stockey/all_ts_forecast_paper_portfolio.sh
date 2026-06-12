#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

exec "${SCRIPT_DIR}/scripts/run_with_markers.sh" "ts_forecast_paper_portfolio" "${PYTHON_BIN}" -m advisory.ts_forecast_paper_portfolio --model-name "${TS_FORECAST_MODEL_NAME:-timesfm_2p5_200m}" --cost-bps "${TS_FORECAST_COST_BPS:-25}" --log-research-ledger "$@"
