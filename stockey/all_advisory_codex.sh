#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

MAX_ATTEMPTS="${CODEX_SUPERVISOR_MAX_ATTEMPTS:-10}"
TAIL_LINES="${CODEX_SUPERVISOR_TAIL_LINES:-200}"

exec "${PYTHON_BIN}" "${SCRIPT_DIR}/scripts/codex_supervised_runner.py" \
  --max-attempts "${MAX_ATTEMPTS}" \
  --tail-lines "${TAIL_LINES}" \
  -- "${SCRIPT_DIR}/all_advisory.sh" "$@"
