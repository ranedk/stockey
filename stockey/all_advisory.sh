#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

ADVISORY_ARGS=(--skip-downloads --intraday-lookback-days "${ADVISORY_INTRADAY_LOOKBACK_DAYS:-30}")
ADVISORY_LOG_PATH="${ADVISORY_LOG_PATH:-logs/cron/all_advisory.log}"
advisory_failure_context="startup"
PRE_SIGNAL_REFRESH_DATE_ARGS=()
PRE_CONTEXT_WATCHLIST_DATE_ARGS=()
explicit_advisory_date="0"

extract_pre_signal_refresh_date_args() {
  local expect_date_value="0"
  local arg
  for arg in "$@"; do
    if [[ "${expect_date_value}" == "1" ]]; then
      if [[ -n "${arg}" ]]; then
        PRE_SIGNAL_REFRESH_DATE_ARGS=(--asof-date "${arg}")
        PRE_CONTEXT_WATCHLIST_DATE_ARGS=(--date "${arg}")
        explicit_advisory_date="1"
      fi
      return 0
    fi
    case "${arg}" in
      --date=*)
        PRE_SIGNAL_REFRESH_DATE_ARGS=(--asof-date "${arg#--date=}")
        PRE_CONTEXT_WATCHLIST_DATE_ARGS=(--date "${arg#--date=}")
        explicit_advisory_date="1"
        return 0
        ;;
      --date)
        expect_date_value="1"
        ;;
      *)
        expect_date_value="0"
        ;;
    esac
  done
}

extract_pre_signal_refresh_date_args "$@"

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
  elif [[ "${advisory_failure_context}" == "pre_advisory_signal_refresh" ]]; then
    echo "[all_advisory] Review-only pre-advisory signal refresh failed before the long advisory run." >&2
    echo "[all_advisory] Re-run manually with: ${PYTHON_BIN} -m advisory.signal_refresh --from-context-overlays ${PRE_SIGNAL_REFRESH_DATE_ARGS[*]} --limit ${ADVISORY_PRE_SIGNAL_REFRESH_LIMIT:-50} --format text" >&2
    echo "[all_advisory] Or skip only this pre-refresh with: ADVISORY_SKIP_PRE_SIGNAL_REFRESH=true ./all_advisory.sh" >&2
  elif [[ "${advisory_failure_context}" == "pre_advisory_context_watchlist_reconcile" ]]; then
    echo "[all_advisory] Pre-advisory context watchlist reconciliation failed before the long advisory run." >&2
    echo "[all_advisory] Re-run manually with: ${PYTHON_BIN} -m advisory.watchlist_builder ${PRE_CONTEXT_WATCHLIST_DATE_ARGS[*]} --setup CONTEXT_OVERLAY_WATCH --rebuild" >&2
    echo "[all_advisory] Or skip only this watchlist reconcile with: ADVISORY_SKIP_PRE_CONTEXT_WATCHLIST_RECONCILE=true ./all_advisory.sh" >&2
  elif [[ "${advisory_failure_context}" == "resolve_advisory_date" ]]; then
    echo "[all_advisory] Could not resolve the latest trading-day advisory date from dim_trading_days." >&2
    echo "[all_advisory] Refresh trading-day data with complete_data.sh, pass --date YYYY-MM-DD explicitly, or set ADVISORY_AUTO_LATEST_TRADING_DATE=false for targeted debugging." >&2
  elif [[ "${advisory_failure_context}" == "recommendation_diagnostics" ]]; then
    echo "[all_advisory] Advisory pipeline, operator snapshot, and trace-summary refresh completed before this failure." >&2
    echo "[all_advisory] Only the read-only recommendation diagnostics post-run report failed." >&2
    echo "[all_advisory] Re-run diagnostics with: ${PYTHON_BIN} -m advisory.recommendation_diagnostics --format text" >&2
    echo "[all_advisory] To skip only this post-run report, set ADVISORY_SKIP_RECOMMENDATION_DIAGNOSTICS=true." >&2
  fi
  return "${exit_code}"
}
trap report_advisory_failure ERR

if [[ "${explicit_advisory_date}" != "1" && "${ADVISORY_AUTO_LATEST_TRADING_DATE:-1}" != "0" && "${ADVISORY_AUTO_LATEST_TRADING_DATE:-true}" != "false" ]]; then
  advisory_failure_context="resolve_advisory_date"
  resolved_advisory_date="$("${PYTHON_BIN}" -m advisory.advisory_date --format date)"
  if [[ -z "${resolved_advisory_date}" ]]; then
    echo "[all_advisory] failed to resolve latest trading-day advisory date" >&2
    exit 1
  fi
  ADVISORY_ARGS+=(--date "${resolved_advisory_date}")
  PRE_SIGNAL_REFRESH_DATE_ARGS=(--asof-date "${resolved_advisory_date}")
  PRE_CONTEXT_WATCHLIST_DATE_ARGS=(--date "${resolved_advisory_date}")
  echo "[all_advisory] resolved advisory date ${resolved_advisory_date} using latest trading day"
elif [[ "${explicit_advisory_date}" != "1" ]]; then
  echo "[all_advisory] latest trading-day date pinning skipped ADVISORY_AUTO_LATEST_TRADING_DATE=${ADVISORY_AUTO_LATEST_TRADING_DATE:-}"
fi

if [[ "${ADVISORY_PARALLEL_LOCAL_STAGES:-1}" != "0" && "${ADVISORY_PARALLEL_LOCAL_STAGES:-true}" != "false" ]]; then
  ADVISORY_ARGS+=(--parallel-local-stages --local-stage-workers "${ADVISORY_LOCAL_STAGE_WORKERS:-3}")
fi

if [[ "${ADVISORY_DISABLE_RULE_REPAIR:-1}" != "0" && "${ADVISORY_DISABLE_RULE_REPAIR:-true}" != "false" ]]; then
  ADVISORY_ARGS+=(--skip-rule-snapshot-refresh --skip-intraday-prefetch)
fi

# Opt-in crash-safe resume: skip stages already completed for this advisory date.
# Off by default so the scheduled EOD run always recomputes fresh; set ADVISORY_RESUME=1
# when restarting an interrupted run. Ignored by the pipeline with --rebuild/--start-at.
if [[ "${ADVISORY_RESUME:-0}" == "1" || "${ADVISORY_RESUME:-false}" == "true" ]]; then
  ADVISORY_ARGS+=(--resume)
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

if [[ "${ADVISORY_SKIP_PRE_SIGNAL_REFRESH:-0}" == "1" || "${ADVISORY_SKIP_PRE_SIGNAL_REFRESH:-false}" == "true" ]]; then
  echo "[all_advisory] pre-advisory review-only signal refresh skipped ADVISORY_SKIP_PRE_SIGNAL_REFRESH=${ADVISORY_SKIP_PRE_SIGNAL_REFRESH}"
else
  advisory_failure_context="pre_advisory_signal_refresh"
  PRE_CONTEXT_SIGNAL_REFRESH_ARGS=(--from-context-overlays "${PRE_SIGNAL_REFRESH_DATE_ARGS[@]}" --limit "${ADVISORY_PRE_SIGNAL_REFRESH_LIMIT:-50}" --format text)
  if [[ "${ADVISORY_PRE_SIGNAL_REFRESH_FORCE:-0}" != "1" && "${ADVISORY_PRE_SIGNAL_REFRESH_FORCE:-false}" != "true" ]]; then
    PRE_CONTEXT_SIGNAL_REFRESH_ARGS+=(--skip-if-current)
  fi
  "${SCRIPT_DIR}/scripts/run_with_markers.sh" "pre_advisory_context_signal_refresh" "${PYTHON_BIN}" -m advisory.signal_refresh "${PRE_CONTEXT_SIGNAL_REFRESH_ARGS[@]}"
  if [[ "${ADVISORY_SKIP_PRE_CONTEXT_WATCHLIST_RECONCILE:-0}" == "1" || "${ADVISORY_SKIP_PRE_CONTEXT_WATCHLIST_RECONCILE:-false}" == "true" ]]; then
    echo "[all_advisory] pre-advisory context watchlist reconciliation skipped ADVISORY_SKIP_PRE_CONTEXT_WATCHLIST_RECONCILE=${ADVISORY_SKIP_PRE_CONTEXT_WATCHLIST_RECONCILE}"
  else
    advisory_failure_context="pre_advisory_context_watchlist_reconcile"
    PRE_CONTEXT_WATCHLIST_ARGS=("${PRE_CONTEXT_WATCHLIST_DATE_ARGS[@]}" --setup CONTEXT_OVERLAY_WATCH --rebuild)
    if [[ "${ADVISORY_PRE_CONTEXT_WATCHLIST_FORCE:-0}" != "1" && "${ADVISORY_PRE_CONTEXT_WATCHLIST_FORCE:-false}" != "true" ]]; then
      PRE_CONTEXT_WATCHLIST_ARGS+=(--skip-if-current)
    fi
    "${SCRIPT_DIR}/scripts/run_with_markers.sh" "pre_advisory_context_watchlist_reconcile" "${PYTHON_BIN}" -m advisory.watchlist_builder "${PRE_CONTEXT_WATCHLIST_ARGS[@]}"
  fi
  if [[ "${ADVISORY_PRE_CAUSAL_MEMORY_REFRESH:-1}" != "0" && "${ADVISORY_PRE_CAUSAL_MEMORY_REFRESH:-true}" != "false" ]]; then
    advisory_failure_context="pre_advisory_signal_refresh"
    "${SCRIPT_DIR}/scripts/run_with_markers.sh" "pre_advisory_causal_memory_signal_refresh" "${PYTHON_BIN}" -m advisory.signal_refresh --from-causal-memory "${PRE_SIGNAL_REFRESH_DATE_ARGS[@]}" --limit "${ADVISORY_PRE_CAUSAL_MEMORY_REFRESH_LIMIT:-25}" --format text
  fi
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

if [[ "${ADVISORY_SKIP_RECOMMENDATION_DIAGNOSTICS:-0}" == "1" || "${ADVISORY_SKIP_RECOMMENDATION_DIAGNOSTICS:-false}" == "true" ]]; then
  echo "[all_advisory] recommendation diagnostics skipped ADVISORY_SKIP_RECOMMENDATION_DIAGNOSTICS=${ADVISORY_SKIP_RECOMMENDATION_DIAGNOSTICS}"
else
  advisory_failure_context="recommendation_diagnostics"
  "${SCRIPT_DIR}/scripts/run_with_markers.sh" "recommendation_diagnostics" "${PYTHON_BIN}" -m advisory.recommendation_diagnostics --format text
fi
