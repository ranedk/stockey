#!/usr/bin/env bash

# Digest of open identity issues (utils/identity_issues.py) and recent fallback
# telemetry (utils/fallback_telemetry.py) -- the reader half of CLAUDE.md's "no
# silent fallback" write paths, previously write-only with no scheduled review
# (confirmed live 2026-08-14; docs/DATA_COVERAGE.md's "Still open" list).
# Non-fatal by default; pass --require to gate.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

"${SCRIPT_DIR}/scripts/run_with_markers.sh" "issue_digest" "${PYTHON_BIN}" -m scripts.issue_digest "$@"
