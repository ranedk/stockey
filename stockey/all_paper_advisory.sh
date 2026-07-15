#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

# Review-only daily advisory (today's RS picks, regime-floored sizing); NO broker.
exec "${SCRIPT_DIR}/scripts/run_with_markers.sh" "paper_advisory" "${PYTHON_BIN}" -m advisory.paper_advisory "$@"
