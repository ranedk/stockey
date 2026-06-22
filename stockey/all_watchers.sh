#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"
WATCHER_LOCK_FILE="${STOCKEY_WATCHER_LOCK_FILE:-/tmp/stockey_watchers.lock}"
WATCHER_CONTEXT_WATCHLIST_DATE_ARGS=()

if [[ "${STOCKEY_WATCHER_LOCK_HELD:-0}" != "1" ]]; then
  WATCHER_LOCK_DIR="${WATCHER_LOCK_FILE}.d"
  WATCHER_LOCK_PID_FILE="${WATCHER_LOCK_DIR}/pid"
  WATCHER_LOCK_COMMAND_FILE="${WATCHER_LOCK_DIR}/command"
  if [[ -f "${WATCHER_LOCK_PID_FILE}" ]]; then
    WATCHER_LOCK_PID="$(cat "${WATCHER_LOCK_PID_FILE}" 2>/dev/null || true)"
    if [[ -n "${WATCHER_LOCK_PID}" ]] && kill -0 "${WATCHER_LOCK_PID}" 2>/dev/null; then
      WATCHER_LOCK_IS_CURRENT="0"
      if [[ ! -f "${WATCHER_LOCK_COMMAND_FILE}" ]]; then
        WATCHER_LOCK_IS_CURRENT="1"
      elif ! command -v ps >/dev/null 2>&1; then
        WATCHER_LOCK_IS_CURRENT="1"
      else
        WATCHER_LOCK_EXPECTED_COMMAND="$(cat "${WATCHER_LOCK_COMMAND_FILE}" 2>/dev/null || true)"
        WATCHER_LOCK_PROCESS_COMMAND="$(ps -p "${WATCHER_LOCK_PID}" -o command= 2>/dev/null || true)"
        if [[ -n "${WATCHER_LOCK_EXPECTED_COMMAND}" && "${WATCHER_LOCK_PROCESS_COMMAND}" == *"${WATCHER_LOCK_EXPECTED_COMMAND}"* ]]; then
          WATCHER_LOCK_IS_CURRENT="1"
        fi
      fi
      if [[ "${WATCHER_LOCK_IS_CURRENT}" == "1" ]]; then
        "${PYTHON_BIN}" -m advisory.continuous_watch --publish-lock-skipped --lock-file "${WATCHER_LOCK_FILE}" --lock-pid "${WATCHER_LOCK_PID}" || true
        exit 0
      fi
    fi
  fi
  exec "${SCRIPT_DIR}/scripts/with_lock.sh" "${WATCHER_LOCK_FILE}" env STOCKEY_WATCHER_LOCK_HELD=1 "$0" "$@"
fi

"${SCRIPT_DIR}/scripts/run_with_markers.sh" "all_watchers" "${PYTHON_BIN}" -m advisory.continuous_watch "$@"

"${SCRIPT_DIR}/scripts/run_with_markers.sh" "wait_signals" "${PYTHON_BIN}" -m advisory.wait_signals --match --format text

if [[ "${WATCHER_SKIP_CONTEXT_WATCHLIST_RECONCILE:-0}" == "1" || "${WATCHER_SKIP_CONTEXT_WATCHLIST_RECONCILE:-false}" == "true" ]]; then
  echo "[all_watchers] context watchlist reconciliation skipped WATCHER_SKIP_CONTEXT_WATCHLIST_RECONCILE=${WATCHER_SKIP_CONTEXT_WATCHLIST_RECONCILE}"
else
  if [[ "${WATCHER_AUTO_LATEST_TRADING_DATE:-1}" != "0" && "${WATCHER_AUTO_LATEST_TRADING_DATE:-true}" != "false" ]]; then
    watcher_resolved_date="$("${PYTHON_BIN}" -m advisory.advisory_date --format date)"
    if [[ -n "${watcher_resolved_date}" ]]; then
      WATCHER_CONTEXT_WATCHLIST_DATE_ARGS=(--date "${watcher_resolved_date}")
      echo "[all_watchers] resolved context watchlist date ${watcher_resolved_date} using latest trading day"
    else
      echo "[all_watchers] failed to resolve latest trading-day context watchlist date" >&2
      exit 1
    fi
  fi
  WATCHER_CONTEXT_WATCHLIST_ARGS=("${WATCHER_CONTEXT_WATCHLIST_DATE_ARGS[@]}" --setup CONTEXT_OVERLAY_WATCH --rebuild)
  if [[ "${WATCHER_CONTEXT_WATCHLIST_FORCE:-0}" != "1" && "${WATCHER_CONTEXT_WATCHLIST_FORCE:-false}" != "true" ]]; then
    WATCHER_CONTEXT_WATCHLIST_ARGS+=(--skip-if-current)
  fi
  "${SCRIPT_DIR}/scripts/run_with_markers.sh" "watcher_context_watchlist_reconcile" "${PYTHON_BIN}" -m advisory.watchlist_builder "${WATCHER_CONTEXT_WATCHLIST_ARGS[@]}"
fi

"${SCRIPT_DIR}/scripts/run_with_markers.sh" "operator_snapshot" "${PYTHON_BIN}" -m advisory.operator_snapshot

"${SCRIPT_DIR}/scripts/run_with_markers.sh" "trace_summary_store" "${PYTHON_BIN}" -m advisory.trace_summary_store --symbol-limit 100 --event-limit 100 --trace-limit 100 --format text
