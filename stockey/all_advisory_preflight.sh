#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"
preflight_failure_context="startup"

report_preflight_failure() {
  local exit_code="$?"
  if [[ "${exit_code}" == "0" ]]; then
    return 0
  fi
  echo "[all_advisory_preflight] failed step=${preflight_failure_context} exit_code=${exit_code}" >&2
  if [[ "${preflight_failure_context}" == "advisory_dhan_preflight" ]]; then
    echo "[all_advisory_preflight] Dhan auth preflight failed before advisory smoke ran." >&2
    echo "[all_advisory_preflight] If CDP/Chrome is unavailable, start it first with: ${SCRIPT_DIR}/scripts/start_chrome_cdp.sh" >&2
    echo "[all_advisory_preflight] Then rerun: ${SCRIPT_DIR}/all_advisory_preflight.sh" >&2
  elif [[ "${preflight_failure_context}" == "advisory_preflight_smoke" ]]; then
    echo "[all_advisory_preflight] Operator smoke failed after Dhan auth preflight completed." >&2
    echo "[all_advisory_preflight] Inspect compact smoke output above, then run: ${PYTHON_BIN} -m advisory.operator_health --skip-dhan" >&2
  fi
  return "${exit_code}"
}
trap report_preflight_failure ERR

DHAN_PREFLIGHT_ARGS=(ensure --min-fresh-minutes "${ADVISORY_DHAN_PREFLIGHT_MIN_FRESH_MINUTES:-30}")
if [[ "${ADVISORY_DHAN_PREFLIGHT_AUTO_LOGIN:-1}" != "0" && "${ADVISORY_DHAN_PREFLIGHT_AUTO_LOGIN:-true}" != "false" ]]; then
  DHAN_PREFLIGHT_ARGS+=(--auto-login)
fi
if [[ "${ADVISORY_DHAN_PREFLIGHT_SKIP_VALIDATE:-0}" == "1" || "${ADVISORY_DHAN_PREFLIGHT_SKIP_VALIDATE:-false}" == "true" ]]; then
  DHAN_PREFLIGHT_ARGS+=(--skip-validate)
fi

preflight_failure_context="advisory_dhan_preflight"
"${SCRIPT_DIR}/scripts/run_with_markers.sh" "advisory_dhan_preflight" "${PYTHON_BIN}" -m data.dhanlive.auth_cli "${DHAN_PREFLIGHT_ARGS[@]}"

if [[ "${ADVISORY_PREFLIGHT_SKIP_SMOKE:-0}" == "1" || "${ADVISORY_PREFLIGHT_SKIP_SMOKE:-false}" == "true" ]]; then
  echo "[all_advisory_preflight] smoke skipped ADVISORY_PREFLIGHT_SKIP_SMOKE=${ADVISORY_PREFLIGHT_SKIP_SMOKE}"
  exit 0
fi

preflight_failure_context="advisory_preflight_smoke"
"${SCRIPT_DIR}/scripts/run_with_markers.sh" "advisory_preflight_smoke" "${PYTHON_BIN}" -m advisory.operator_smoke --include-dhan --fix-hint-limit "${ADVISORY_PREFLIGHT_FIX_HINT_LIMIT:-8}"
