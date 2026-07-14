#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

# Research-only: rebuild the split/bonus-adjusted daily close table from price steps (no CA-record dependency).
exec "${SCRIPT_DIR}/scripts/run_with_markers.sh" "price_adjustment" "${PYTHON_BIN}" -m advisory.price_adjustment "$@"
