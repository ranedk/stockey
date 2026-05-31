#!/usr/bin/env bash

set -uo pipefail

if [[ "$#" -lt 2 ]]; then
  echo "usage: $0 <script-name> <command> [args...]" >&2
  exit 2
fi

SCRIPT_NAME="$1"
shift

emit_marker() {
  local status="$1"
  local exit_code="${2:-}"
  local timestamp
  timestamp="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  if [[ -n "${exit_code}" ]]; then
    echo "[stockey.script] name=${SCRIPT_NAME} status=${status} exit_code=${exit_code} timestamp=${timestamp}"
  else
    echo "[stockey.script] name=${SCRIPT_NAME} status=${status} timestamp=${timestamp}"
  fi
}

emit_marker "start"
"$@"
status="$?"

if [[ "${status}" == "130" || "${status}" == "143" ]]; then
  emit_marker "interrupted" "${status}"
elif [[ "${status}" == "0" ]]; then
  emit_marker "done" "${status}"
else
  emit_marker "failed" "${status}"
fi

exit "${status}"
