#!/usr/bin/env bash

# Stop the running go-crond scheduler (started via ./start_cron.sh). Only ever targets the
# exact go-crond process this repo would itself start -- matched via the same anchored
# `pgrep -f "^${GO_CROND_BIN} "` pattern start_cron.sh and scripts/ensure_go_crond_alive.sh
# already use, so this never touches an unrelated process that happens to share the name.
#
# Stopping go-crond does not kill any job it already launched -- an in-flight cron job
# (complete_data.sh, a queue worker, ...) is an independent forked process and keeps running
# to completion; only the scheduler that would fire the NEXT job is stopped.
#
# NOTE: if scripts/ensure_go_crond_alive.sh is registered in the OS-level user crontab
# (`crontab -l`), it will detect go-crond as not-running and auto-restart it within its own
# schedule (every 15 min per CLAUDE.md). This script does not touch that OS crontab entry --
# it's host state, not tracked by git or builder.py (see CLAUDE.md's go-crond section). If
# you need go-crond to STAY stopped, comment out or remove that entry yourself first
# (`crontab -e`).
#
# Env:
#   STOCKEY_CRON_STOP_TIMEOUT_SECONDS  seconds to wait after SIGTERM before escalating to
#                                      SIGKILL (default 15)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GO_CROND_BIN="${SCRIPT_DIR}/go-crond"
stop_timeout="${STOCKEY_CRON_STOP_TIMEOUT_SECONDS:-15}"

pids="$(pgrep -f "^${GO_CROND_BIN} " || true)"
if [[ -z "${pids}" ]]; then
  echo "[stop_cron] go-crond is not running (pgrep -f \"^${GO_CROND_BIN} \" found nothing) -- nothing to stop." >&2
  exit 0
fi

echo "[stop_cron] stopping go-crond (pid(s): ${pids//$'\n'/, }) -- sending SIGTERM" >&2
# shellcheck disable=SC2086 -- intentional word-splitting: pids may be more than one PID
kill -TERM ${pids} 2>/dev/null || true

deadline=$((SECONDS + stop_timeout))
while pgrep -f "^${GO_CROND_BIN} " >/dev/null 2>&1; do
  if [[ ${SECONDS} -ge ${deadline} ]]; then
    remaining="$(pgrep -f "^${GO_CROND_BIN} " || true)"
    if [[ -n "${remaining}" ]]; then
      echo "[stop_cron] go-crond still running after ${stop_timeout}s -- sending SIGKILL to pid(s): ${remaining//$'\n'/, }" >&2
      # shellcheck disable=SC2086
      kill -KILL ${remaining} 2>/dev/null || true
    fi
    break
  fi
  sleep 0.5
done

if pgrep -f "^${GO_CROND_BIN} " >/dev/null 2>&1; then
  echo "[stop_cron] ERROR: go-crond is still running after SIGKILL -- inspect manually (pgrep -af go-crond)." >&2
  exit 1
fi

echo "[stop_cron] go-crond stopped." >&2
echo "[stop_cron] NOTE: if scripts/ensure_go_crond_alive.sh is in the OS crontab (crontab -l), it will auto-restart go-crond within its own schedule -- disable that entry first if you need the scheduler to stay stopped." >&2
