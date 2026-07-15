#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

# Regime-sized shadow ledger + data-selected floor config; research-only.
exec "${SCRIPT_DIR}/scripts/run_with_markers.sh" "regime_shadow_ledger" "${PYTHON_BIN}" -m advisory.regime_shadow_ledger "$@"
