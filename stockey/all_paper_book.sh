#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

# Review-only always-on paper book: assumes each daily BUY was taken and manages it to EXIT (stop / 20-day
# cap / momentum fade / data-gap). Writes advisory_paper_book + the operator dashboard. No broker.
exec "${SCRIPT_DIR}/scripts/run_with_markers.sh" "paper_book" "${PYTHON_BIN}" -m advisory.paper_book "$@"
