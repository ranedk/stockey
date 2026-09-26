#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

# Daily incremental Dhan 1-min intraday OHLCV sync for the full active NSE
# equity universe -- see data/dhanlive/intraday_daily_sync.py's own
# docstring. Scheduled right after all_price_adjustment.sh so the day's
# price data is settled first.
"${SCRIPT_DIR}/scripts/run_with_markers.sh" "dhan_intraday_sync" "${PYTHON_BIN}" -m data.dhanlive.intraday_daily_sync "$@"
