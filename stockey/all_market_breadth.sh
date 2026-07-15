#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

# Daily market breadth (%% above 50DMA); read-only research table.
exec "${SCRIPT_DIR}/scripts/run_with_markers.sh" "market_breadth" "${PYTHON_BIN}" -m advisory.market_breadth "$@"
