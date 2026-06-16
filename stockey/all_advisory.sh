#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

ADVISORY_ARGS=(--skip-downloads --intraday-lookback-days "${ADVISORY_INTRADAY_LOOKBACK_DAYS:-30}")
ADVISORY_LOG_PATH="${ADVISORY_LOG_PATH:-logs/cron/all_advisory.log}"
advisory_failure_context="startup"

report_advisory_failure() {
  local exit_code="$?"
  if [[ "${exit_code}" == "0" ]]; then
    return 0
  fi
  echo "[all_advisory] failed step=${advisory_failure_context} exit_code=${exit_code}" >&2
  if [[ "${advisory_failure_context}" == "all_advisory" ]]; then
    echo "[all_advisory] latest stage report follows" >&2
    "${PYTHON_BIN}" "${SCRIPT_DIR}/scripts/advisory_stage_report.py" --log-path "${ADVISORY_LOG_PATH}" --limit 8 --format text >&2 || true
  elif [[ "${advisory_failure_context}" == "dhan_auth_preflight" ]]; then
    echo "[all_advisory] Dhan auth preflight failed before advisory stages started." >&2
    echo "[all_advisory] If CDP/Chrome is unavailable, start it first with: ${SCRIPT_DIR}/scripts/start_chrome_cdp.sh" >&2
    echo "[all_advisory] Fix token state with: ${PYTHON_BIN} -m data.dhanlive.auth_cli ensure --auto-login" >&2
  fi
  return "${exit_code}"
}
trap report_advisory_failure ERR

if [[ "${ADVISORY_PARALLEL_LOCAL_STAGES:-1}" != "0" && "${ADVISORY_PARALLEL_LOCAL_STAGES:-true}" != "false" ]]; then
  ADVISORY_ARGS+=(--parallel-local-stages --local-stage-workers "${ADVISORY_LOCAL_STAGE_WORKERS:-3}")
fi

if [[ "${ADVISORY_DISABLE_RULE_REPAIR:-1}" != "0" && "${ADVISORY_DISABLE_RULE_REPAIR:-true}" != "false" ]]; then
  ADVISORY_ARGS+=(--skip-rule-snapshot-refresh --skip-intraday-prefetch)
fi

if [[ "${ADVISORY_DHAN_PREFLIGHT:-1}" != "0" && "${ADVISORY_DHAN_PREFLIGHT:-true}" != "false" ]]; then
  advisory_failure_context="dhan_auth_preflight"
  DHAN_PREFLIGHT_ARGS=(ensure --min-fresh-minutes "${ADVISORY_DHAN_PREFLIGHT_MIN_FRESH_MINUTES:-30}")
  if [[ "${ADVISORY_DHAN_PREFLIGHT_AUTO_LOGIN:-1}" != "0" && "${ADVISORY_DHAN_PREFLIGHT_AUTO_LOGIN:-true}" != "false" ]]; then
    DHAN_PREFLIGHT_ARGS+=(--auto-login)
  fi
  if [[ "${ADVISORY_DHAN_PREFLIGHT_SKIP_VALIDATE:-0}" == "1" || "${ADVISORY_DHAN_PREFLIGHT_SKIP_VALIDATE:-false}" == "true" ]]; then
    DHAN_PREFLIGHT_ARGS+=(--skip-validate)
  fi
  "${SCRIPT_DIR}/scripts/run_with_markers.sh" "dhan_auth_preflight" "${PYTHON_BIN}" -m data.dhanlive.auth_cli "${DHAN_PREFLIGHT_ARGS[@]}"
fi

advisory_failure_context="all_advisory"
"${SCRIPT_DIR}/scripts/run_with_markers.sh" "all_advisory" "${PYTHON_BIN}" -m advisory.master_pipeline "${ADVISORY_ARGS[@]}" "$@"

if [[ "${ADVISORY_SKIP_POST_REFRESH:-0}" == "1" || "${ADVISORY_SKIP_POST_REFRESH:-false}" == "true" ]]; then
  echo "[all_advisory] post-refresh skipped ADVISORY_SKIP_POST_REFRESH=${ADVISORY_SKIP_POST_REFRESH}"
  exit 0
fi

advisory_failure_context="operator_snapshot"
"${SCRIPT_DIR}/scripts/run_with_markers.sh" "operator_snapshot" "${PYTHON_BIN}" -m advisory.operator_snapshot
advisory_failure_context="trace_summary_store"
"${SCRIPT_DIR}/scripts/run_with_markers.sh" "trace_summary_store" "${PYTHON_BIN}" -m advisory.trace_summary_store --symbol-limit 150 --event-limit 150 --trace-limit 100 --format text
