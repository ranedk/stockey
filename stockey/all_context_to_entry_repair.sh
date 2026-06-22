#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

REPAIR_DATE_ARGS=()
SIGNAL_REFRESH_DATE_ARGS=()
WATCHLIST_DATE_ARGS=()
DIAGNOSTICS_DATE_ARGS=()
explicit_repair_date="0"

extract_repair_date_args() {
  local expect_date_value="0"
  local arg
  for arg in "$@"; do
    if [[ "${expect_date_value}" == "1" ]]; then
      if [[ -n "${arg}" ]]; then
        REPAIR_DATE_ARGS=(--date "${arg}")
        SIGNAL_REFRESH_DATE_ARGS=(--asof-date "${arg}")
        WATCHLIST_DATE_ARGS=(--date "${arg}")
        DIAGNOSTICS_DATE_ARGS=(--asof-date "${arg}")
        explicit_repair_date="1"
      fi
      return 0
    fi
    case "${arg}" in
      --date=*)
        local date_value="${arg#--date=}"
        REPAIR_DATE_ARGS=(--date "${date_value}")
        SIGNAL_REFRESH_DATE_ARGS=(--asof-date "${date_value}")
        WATCHLIST_DATE_ARGS=(--date "${date_value}")
        DIAGNOSTICS_DATE_ARGS=(--asof-date "${date_value}")
        explicit_repair_date="1"
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

extract_repair_date_args "$@"

if [[ "${explicit_repair_date}" != "1" && "${CONTEXT_REPAIR_AUTO_LATEST_TRADING_DATE:-1}" != "0" && "${CONTEXT_REPAIR_AUTO_LATEST_TRADING_DATE:-true}" != "false" ]]; then
  resolved_repair_date="$("${PYTHON_BIN}" -m advisory.advisory_date --format date)"
  if [[ -z "${resolved_repair_date}" ]]; then
    echo "[all_context_to_entry_repair] failed to resolve latest trading-day repair date" >&2
    exit 1
  fi
  REPAIR_DATE_ARGS=(--date "${resolved_repair_date}")
  SIGNAL_REFRESH_DATE_ARGS=(--asof-date "${resolved_repair_date}")
  WATCHLIST_DATE_ARGS=(--date "${resolved_repair_date}")
  DIAGNOSTICS_DATE_ARGS=(--asof-date "${resolved_repair_date}")
  echo "[all_context_to_entry_repair] resolved repair date ${resolved_repair_date} using latest trading day"
fi

if [[ "${CONTEXT_REPAIR_SKIP_SIGNAL_REFRESH:-0}" != "1" && "${CONTEXT_REPAIR_SKIP_SIGNAL_REFRESH:-false}" != "true" ]]; then
  SIGNAL_REFRESH_ARGS=(--from-context-overlays "${SIGNAL_REFRESH_DATE_ARGS[@]}" --limit "${CONTEXT_REPAIR_SIGNAL_REFRESH_LIMIT:-50}" --format text)
  if [[ "${CONTEXT_REPAIR_SIGNAL_REFRESH_FORCE:-0}" != "1" && "${CONTEXT_REPAIR_SIGNAL_REFRESH_FORCE:-false}" != "true" ]]; then
    SIGNAL_REFRESH_ARGS+=(--skip-if-current)
  fi
  "${SCRIPT_DIR}/scripts/run_with_markers.sh" "context_repair_signal_refresh" "${PYTHON_BIN}" -m advisory.signal_refresh "${SIGNAL_REFRESH_ARGS[@]}"
fi

if [[ "${CONTEXT_REPAIR_SKIP_WATCHLIST_RECONCILE:-0}" != "1" && "${CONTEXT_REPAIR_SKIP_WATCHLIST_RECONCILE:-false}" != "true" ]]; then
  WATCHLIST_ARGS=("${WATCHLIST_DATE_ARGS[@]}" --setup CONTEXT_OVERLAY_WATCH --rebuild)
  if [[ "${CONTEXT_REPAIR_WATCHLIST_FORCE:-0}" != "1" && "${CONTEXT_REPAIR_WATCHLIST_FORCE:-false}" != "true" ]]; then
    WATCHLIST_ARGS+=(--skip-if-current)
  fi
  "${SCRIPT_DIR}/scripts/run_with_markers.sh" "context_repair_watchlist_reconcile" "${PYTHON_BIN}" -m advisory.watchlist_builder "${WATCHLIST_ARGS[@]}"
fi

if [[ "${CONTEXT_REPAIR_SKIP_TECHNICAL_REFRESH:-0}" != "1" && "${CONTEXT_REPAIR_SKIP_TECHNICAL_REFRESH:-false}" != "true" ]]; then
  technical_refresh_command="$("${PYTHON_BIN}" -m advisory.recommendation_diagnostics "${DIAGNOSTICS_DATE_ARGS[@]}" --context-watch-technical-command-only)"
  if [[ -n "${technical_refresh_command}" ]]; then
    technical_refresh_command="${technical_refresh_command/#python /\"${PYTHON_BIN}\" }"
    "${SCRIPT_DIR}/scripts/run_with_markers.sh" "context_repair_technical_refresh" /bin/bash -lc "${technical_refresh_command}"
  else
    echo "[all_context_to_entry_repair] context_repair_technical_refresh skipped: no active refreshable context-watch symbols"
  fi
fi

if [[ "${CONTEXT_REPAIR_SKIP_BOUNDED_ADVISORY:-0}" != "1" && "${CONTEXT_REPAIR_SKIP_BOUNDED_ADVISORY:-false}" != "true" ]]; then
  PIPELINE_ARGS=(
    "${REPAIR_DATE_ARGS[@]}"
    --start-at rules
    --stop-at actions
    --skip-peer-sync
    --skip-intraday
    --skip-rule-snapshot-refresh
    --skip-intraday-prefetch
    --include-lifecycle
  )
  if [[ "${CONTEXT_REPAIR_PARALLEL_LOCAL_STAGES:-1}" != "0" && "${CONTEXT_REPAIR_PARALLEL_LOCAL_STAGES:-true}" != "false" ]]; then
    PIPELINE_ARGS+=(--parallel-local-stages --local-stage-workers "${CONTEXT_REPAIR_LOCAL_STAGE_WORKERS:-3}")
  fi
  "${SCRIPT_DIR}/scripts/run_with_markers.sh" "context_repair_bounded_advisory" "${PYTHON_BIN}" -m advisory.pipeline "${PIPELINE_ARGS[@]}"
fi

if [[ "${CONTEXT_REPAIR_SKIP_ACTION_REFRESH:-0}" != "1" && "${CONTEXT_REPAIR_SKIP_ACTION_REFRESH:-false}" != "true" ]]; then
  "${SCRIPT_DIR}/scripts/run_with_markers.sh" "context_repair_action_recommender" "${PYTHON_BIN}" -m advisory.action_recommender "${REPAIR_DATE_ARGS[@]}" --format text
fi

if [[ "${CONTEXT_REPAIR_SKIP_DIAGNOSTICS:-0}" != "1" && "${CONTEXT_REPAIR_SKIP_DIAGNOSTICS:-false}" != "true" ]]; then
  "${SCRIPT_DIR}/scripts/run_with_markers.sh" "context_repair_recommendation_diagnostics" "${PYTHON_BIN}" -m advisory.recommendation_diagnostics "${DIAGNOSTICS_DATE_ARGS[@]}" --format text
fi
