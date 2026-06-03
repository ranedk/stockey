#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"
WATCHER_LOCK_FILE="${STOCKEY_WATCHER_LOCK_FILE:-/tmp/stockey_watchers.lock}"

if [[ "${STOCKEY_WATCHER_LOCK_HELD:-0}" != "1" ]]; then
  exec "${SCRIPT_DIR}/scripts/with_lock.sh" "${WATCHER_LOCK_FILE}" env STOCKEY_WATCHER_LOCK_HELD=1 "$0" "$@"
fi

"${SCRIPT_DIR}/scripts/run_with_markers.sh" "all_watchers" "${PYTHON_BIN}" -m advisory.continuous_watch "$@"

"${SCRIPT_DIR}/scripts/run_with_markers.sh" "wait_signals" "${PYTHON_BIN}" -m advisory.wait_signals --match --format text

"${SCRIPT_DIR}/scripts/run_with_markers.sh" "operator_snapshot" "${PYTHON_BIN}" -m advisory.operator_snapshot

"${SCRIPT_DIR}/scripts/run_with_markers.sh" "trace_summary_store" "${PYTHON_BIN}" -m advisory.trace_summary_store --symbol-limit 100 --event-limit 100 --trace-limit 100 --format text
