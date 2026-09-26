#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

# Read-only completeness gate. Deliberately browser-free and broker-free (see the
# module docstring): the monitor must not share the collectors' dependencies, which
# is how the 2026-08-26..31 outage stayed invisible -- data_readiness was crashing on
# the same dead-Chrome CDP timeout as the collectors it was supposed to be watching.
# Exits non-zero on a hard error, which run_with_markers surfaces as status=failed.
exec "${SCRIPT_DIR}/scripts/run_with_markers.sh" "data_completeness" \
  "${PYTHON_BIN}" -m scripts.data_completeness "$@"
