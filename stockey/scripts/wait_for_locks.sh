#!/usr/bin/env bash

set -euo pipefail

TIMEOUT_SECONDS=7200
SLEEP_SECONDS=30
LOCKS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --timeout-seconds)
      TIMEOUT_SECONDS="$2"
      shift 2
      ;;
    --sleep-seconds)
      SLEEP_SECONDS="$2"
      shift 2
      ;;
    --)
      shift
      break
      ;;
    -*)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
    *)
      LOCKS+=("$1")
      shift
      ;;
  esac
done

if [[ "${#LOCKS[@]}" -eq 0 ]]; then
  echo "Usage: $0 [--timeout-seconds N] [--sleep-seconds N] <lock-file>..." >&2
  exit 2
fi

started_at="$(date +%s)"

is_lock_active() {
  local lock_file="$1"
  local lock_dir="${lock_file}.d"
  local pid_file="${lock_dir}/pid"

  if [[ ! -d "$lock_dir" ]]; then
    return 1
  fi

  if [[ -f "$pid_file" ]]; then
    local pid
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  fi

  echo "[stockey.lock] removing stale lock=${lock_file}" >&2
  rm -rf "$lock_dir"
  return 1
}

while true; do
  active_locks=()
  for lock_file in "${LOCKS[@]}"; do
    if is_lock_active "$lock_file"; then
      active_locks+=("$lock_file")
    fi
  done

  if [[ "${#active_locks[@]}" -eq 0 ]]; then
    echo "[stockey.lock] all_locks_clear locks=${LOCKS[*]}" >&2
    exit 0
  fi

  now="$(date +%s)"
  elapsed=$((now - started_at))
  if [[ "$elapsed" -ge "$TIMEOUT_SECONDS" ]]; then
    echo "[stockey.lock] timeout waiting_for_locks elapsed=${elapsed}s locks=${active_locks[*]}" >&2
    exit 124
  fi

  echo "[stockey.lock] waiting elapsed=${elapsed}s locks=${active_locks[*]}" >&2
  sleep "$SLEEP_SECONDS"
done
