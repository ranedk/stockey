#!/usr/bin/env bash

set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <lock-file> <command> [args...]" >&2
  exit 2
fi

LOCK_FILE="$1"
shift

mkdir -p "$(dirname "$LOCK_FILE")"

LOCK_DIR="${LOCK_FILE}.d"
PID_FILE="${LOCK_DIR}/pid"
COMMAND_FILE="${LOCK_DIR}/command"

lock_command_label() {
  printf "%s" "$*"
}

process_matches_lock_command() {
  local pid="$1"
  local expected_command="$2"
  local process_command
  if ! command -v ps >/dev/null 2>&1; then
    return 0
  fi
  process_command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  if [[ -z "$process_command" ]]; then
    return 1
  fi
  [[ "$process_command" == *"$expected_command"* ]]
}

acquire_lock() {
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "$$" > "$PID_FILE"
    lock_command_label "$@" > "$COMMAND_FILE"
    return 0
  fi

  if [[ -f "$PID_FILE" ]]; then
    local old_pid
    old_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
      if [[ ! -f "$COMMAND_FILE" ]]; then
        echo "[stockey.lock] skip already_running_legacy lock=${LOCK_FILE} pid=${old_pid}" >&2
        return 1
      else
        local expected_command
        expected_command="$(cat "$COMMAND_FILE" 2>/dev/null || true)"
        if [[ -n "$expected_command" ]] && process_matches_lock_command "$old_pid" "$expected_command"; then
          echo "[stockey.lock] skip already_running lock=${LOCK_FILE} pid=${old_pid}" >&2
          return 1
        fi
        echo "[stockey.lock] removing stale pid_reuse lock=${LOCK_FILE} pid=${old_pid}" >&2
      fi
    fi
  fi

  echo "[stockey.lock] removing stale lock=${LOCK_FILE}" >&2
  rm -rf "$LOCK_DIR"
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "$$" > "$PID_FILE"
    lock_command_label "$@" > "$COMMAND_FILE"
    return 0
  fi

  echo "[stockey.lock] skip lock_race lock=${LOCK_FILE}" >&2
  return 1
}

if ! acquire_lock "$@"; then
  exit 0
fi

cleanup_lock() {
  rm -rf "$LOCK_DIR"
}
trap cleanup_lock EXIT INT TERM

"$@"
