#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

MAX_CYCLES="${ANALYSIS_AGENT_MAX_CYCLES:-1}"
LOG_DIR="${ANALYSIS_AGENT_LOG_DIR:-${SCRIPT_DIR}/logs/analysis_agents}"
TIMEOUT_SECONDS="${ANALYSIS_AGENT_CODEX_TIMEOUT_SECONDS:-3600}"

mkdir -p "${LOG_DIR}"

exec "${SCRIPT_DIR}/scripts/run_with_markers.sh" "all_analysis_codex" "${PYTHON_BIN}" "${SCRIPT_DIR}/scripts/analysis_agent_loop.py" \
  --max-cycles "${MAX_CYCLES}" \
  --log-dir "${LOG_DIR}" \
  --codex-timeout-seconds "${TIMEOUT_SECONDS}" \
  "$@"
