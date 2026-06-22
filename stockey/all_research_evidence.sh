#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

RESEARCH_EVIDENCE_ARGS=()

if [[ "${RESEARCH_EVIDENCE_INCLUDE_SPLIT_REPORTS:-1}" != "0" && "${RESEARCH_EVIDENCE_INCLUDE_SPLIT_REPORTS:-true}" != "false" ]]; then
  RESEARCH_EVIDENCE_ARGS+=(--include-signal-quality-split-reports)
fi

if [[ -n "${RESEARCH_EVIDENCE_EXTRA_ARGS:-}" ]]; then
  # shellcheck disable=SC2206
  EXTRA_ARGS=(${RESEARCH_EVIDENCE_EXTRA_ARGS})
  RESEARCH_EVIDENCE_ARGS+=("${EXTRA_ARGS[@]}")
fi

exec "${SCRIPT_DIR}/scripts/run_with_markers.sh" "research_evidence" "${PYTHON_BIN}" -m advisory.research_evidence_runner "${RESEARCH_EVIDENCE_ARGS[@]}" "$@"
