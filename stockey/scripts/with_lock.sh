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

acquire_lock() {
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "$$" > "$PID_FILE"
    return 0
  fi

  if [[ -f "$PID_FILE" ]]; then
    local old_pid
    old_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
      echo "[stockey.lock] skip already_running lock=${LOCK_FILE} pid=${old_pid}" >&2
      return 1
    fi
  fi

  echo "[stockey.lock] removing stale lock=${LOCK_FILE}" >&2
  rm -rf "$LOCK_DIR"
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "$$" > "$PID_FILE"
    return 0
  fi

  echo "[stockey.lock] skip lock_race lock=${LOCK_FILE}" >&2
  return 1
}

if ! acquire_lock; then
  exit 0
fi

cleanup_lock() {
  rm -rf "$LOCK_DIR"
}
trap cleanup_lock EXIT INT TERM

"$@"
