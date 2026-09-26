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
  # BUG FOUND LIVE 2026-08-20 (re-audit, same HIGH finding as the TOCTOU race above --
  # a second real way this same "steal an active lock" failure could happen): plain
  # `ps -o command=` truncates its output to a terminal-derived width in some environments
  # (confirmed live: procps truncated a long guarded-command line to 81 chars here even
  # though ps's own stdout was a pipe, not a tty -- it still consults the process's
  # controlling terminal). A truncated process_command can never contain the full (longer)
  # expected_command as a substring, so a genuinely still-running holder was misclassified
  # as "stale pid_reuse" and its lock silently stolen out from under it -- same double-run
  # outcome as the mkdir/write race, via a different path. `ww` (procps: two or more -w
  # flags fully disables output-width truncation) makes this check reliable regardless of
  # environment.
  process_command="$(ps -p "$pid" -o command= ww 2>/dev/null || true)"
  if [[ -z "$process_command" ]]; then
    return 1
  fi
  [[ "$process_command" == *"$expected_command"* ]]
}

# BUG FOUND LIVE 2026-08-20 (re-audit, HIGH): the old acquire path did `mkdir "$LOCK_DIR"`
# (the real mutual-exclusion primitive -- POSIX mkdir is atomic) and only THEN wrote
# PID_FILE/COMMAND_FILE as two separate, non-atomic writes after it. In the window between
# the mkdir succeeding and those writes landing, a second process's mkdir correctly failed
# (dir already exists) -- but it then found `[[ -f "$PID_FILE" ]]` false (not written yet)
# and fell straight through to the "stale lock" branch: `rm -rf "$LOCK_DIR"`, deleting the
# FIRST process's still-forming lock out from under it, then re-`mkdir`'d and took the lock
# for itself. Two guarded commands then ran concurrently under the same lock file --
# exactly what with_lock.sh exists to prevent -- and the first process's later
# `trap cleanup_lock EXIT` would go on to `rm -rf` the SECOND process's lock dir too,
# potentially cascading to a third acquirer.
#
# Fixed by writing PID_FILE/COMMAND_FILE into a staging directory FIRST, then bringing the
# real lock into existence with a single atomic `mv -T` (rename(2)) into place. A second
# process can now only ever observe $LOCK_DIR in one of two states: absent, or present with
# both files already inside -- the half-initialized state this bug exploited is no longer
# reachable. `mv -T` also gives the mutual-exclusion check itself for free: if $LOCK_DIR
# already exists (non-empty, since a real holder's rename always populates it atomically),
# `mv -T` fails cleanly with no destructive side effect, rather than silently merging into it.
_stage_and_rename_lock() {
  local pending_dir
  pending_dir="$(mktemp -d "${LOCK_DIR}.pending.XXXXXX" 2>/dev/null)" || return 1
  echo "$$" > "$pending_dir/pid"
  lock_command_label "$@" > "$pending_dir/command"
  if mv -T "$pending_dir" "$LOCK_DIR" 2>/dev/null; then
    return 0
  fi
  rm -rf "$pending_dir"
  return 1
}

acquire_lock() {
  if _stage_and_rename_lock "$@"; then
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
  if _stage_and_rename_lock "$@"; then
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
