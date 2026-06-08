#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

LIMIT="${SUPERSEDED_CLEANUP_AUDIT_LIMIT:-500}"

"${SCRIPT_DIR}/scripts/run_with_markers.sh" "superseded_cleanup_audit" "${PYTHON_BIN}" -m advisory.superseded_failures --limit "${LIMIT}" "$@"
