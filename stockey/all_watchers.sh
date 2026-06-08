#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"
WATCHER_LOCK_FILE="${STOCKEY_WATCHER_LOCK_FILE:-/tmp/stockey_watchers.lock}"

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

"${SCRIPT_DIR}/scripts/run_with_markers.sh" "operator_snapshot" "${PYTHON_BIN}" -m advisory.operator_snapshot

"${SCRIPT_DIR}/scripts/run_with_markers.sh" "trace_summary_store" "${PYTHON_BIN}" -m advisory.trace_summary_store --symbol-limit 100 --event-limit 100 --trace-limit 100 --format text
