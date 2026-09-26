#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

# Research-only: rebuild the split/bonus-adjusted daily close table from price steps (no CA-record dependency).
"${SCRIPT_DIR}/scripts/run_with_markers.sh" "price_adjustment" "${PYTHON_BIN}" -m data.nseindia.price_adjustment "$@"
# BSE-only-company twin (2026-08-15, fundamentals screener gap fix) -- see data/bseindia/price_adjustment.py's
# own docstring. Not "exec"'d as the last command the way the NSE step used to be, now that there are two.
"${SCRIPT_DIR}/scripts/run_with_markers.sh" "bse_price_adjustment" "${PYTHON_BIN}" -m data.bseindia.price_adjustment "$@"
