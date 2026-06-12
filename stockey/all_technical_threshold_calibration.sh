#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

exec "${SCRIPT_DIR}/scripts/run_with_markers.sh" "technical_threshold_calibration" "${PYTHON_BIN}" -m advisory.technical_threshold_calibration --horizons ${TECHNICAL_CALIBRATION_HORIZONS:-5 10 20} --cost-bps "${TECHNICAL_CALIBRATION_COST_BPS:-25}" --min-signals "${TECHNICAL_CALIBRATION_MIN_SIGNALS:-10}" "$@"
