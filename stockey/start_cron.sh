#!/usr/bin/env bash

# Start the Stockey scheduler. Always reconciles daily OHLCV coverage FIRST so a
# scheduler that was down during market hours does not begin the day on stale bars
# (stale bars make the freshness gates suppress all buy authority). go-crond does not
# support @reboot entries, so this wrapper is the supported way to start the cron.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
crontab_path="${1:-${SCRIPT_DIR}/config/stockey.generated.crontab}"
GO_CROND_BIN="${SCRIPT_DIR}/go-crond"
CRON_START_LOCK="/tmp/stockey_cron_start.lock"
CRON_MAINTENANCE_SENTINEL="/tmp/stockey_cron_maintenance"

# Starting the scheduler is an explicit statement that maintenance is over, so clear the
# sentinel that tells the watchdog to stand down. Leaving it set would mean a later crash
# is never auto-recovered -- the exact silent-death the watchdog exists to prevent.
if [ -e "${CRON_MAINTENANCE_SENTINEL}" ]; then
  rm -f "${CRON_MAINTENANCE_SENTINEL}"
  echo "[start_cron] cleared ${CRON_MAINTENANCE_SENTINEL} -- watchdog auto-restart is active again."
fi

# BUG FOUND LIVE 2026-08-20 (re-audit, LOW): no check here for an already-running go-crond
# before the final `exec` below replaces this process with a NEW go-crond instance -- a
# second manual invocation (or a manual restart racing scripts/ensure_go_crond_alive.sh's
# own watchdog-triggered restart, which already protects only ITS OWN restart call via
# with_lock.sh, not a direct/manual call to this script) would end up with two go-crond
# processes both reading and firing the same crontab, double-running every scheduled job.
# Same `pgrep -f` pattern the watchdog already uses, for one consistent definition of
# "is go-crond alive" across both scripts. Exit 0 (not an error) -- matches this codebase's
# existing idiom for "the thing you wanted is already true" (with_lock.sh's own
# already_running skip; all_fundamentals_api.sh's own health-check no-op, per CLAUDE.md).
if pgrep -x go-crond >/dev/null 2>&1; then
  echo "[start_cron] go-crond is already running (pid $(pgrep -x go-crond | head -1)) -- not starting a second instance. Stop it first if you intend to restart with a different crontab." >&2
  exit 0
fi

# HOLD THE START LOCK FOR THE WHOLE RUN, not just the exec at the end.
#
# BUG REPRODUCED LIVE 2026-09-02: the go-crond check below is necessary but not
# sufficient. Steps 1 and 2 (data_readiness --fix, then a full ohlcv_reconcile) run for
# MINUTES before go-crond is exec'd -- ~30 with the reconcile cap raised to 1500 -- and
# for that entire window go-crond is legitimately not running. scripts/
# ensure_go_crond_alive.sh ticked inside that window, saw no go-crond, correctly
# concluded "the scheduler is dead", and launched a SECOND start_cron.sh: two concurrent
# data_readiness/reconcile passes competing for the same Dhan rate limit. The watchdog
# took a lock for that restart, but this script took none, so they never contended.
#
# Both sides now take CRON_START_LOCK, so whichever starts first wins and the other exits
# immediately. flock is released when this process exits (including on exec).
if command -v flock >/dev/null 2>&1; then
  exec 9>"${CRON_START_LOCK}"
  if ! flock -n 9; then
    echo "[start_cron] another start is already in progress (holds ${CRON_START_LOCK}) -- not starting a second one." >&2
    exit 0
  fi
fi

echo "[start_cron] step 1/2: data readiness (bhavcopy/Dhan/benchmark check + fix)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"
if ! (cd "${SCRIPT_DIR}" && "${PYTHON_BIN}" -m data.data_readiness --fix); then
  echo "[start_cron] WARNING: data readiness fix pass failed; continuing (visible in checks)." >&2
fi
if ! "${SCRIPT_DIR}/scripts/with_lock.sh" /tmp/stockey_ohlcv_reconcile.lock "${SCRIPT_DIR}/all_ohlcv_reconcile.sh"; then
  # The scheduler starting matters more than the catch-up succeeding: watchers and the
  # scheduled reconcile retry it, and the Operator Health OHLCV coverage check keeps the
  # gap visible. Never silent, never fatal.
  echo "[start_cron] WARNING: OHLCV reconciliation failed; starting scheduler anyway." >&2
  echo "[start_cron] Inspect with: ./all_ohlcv_reconcile.sh --dry-run --format json" >&2
fi

echo "[start_cron] step 2/2: starting go-crond crontab=${crontab_path}"

# RELEASE THE START LOCK BEFORE exec'ing.
#
# `exec` replaces this process but does NOT close inherited descriptors, so go-crond
# would inherit fd 9 and hold CRON_START_LOCK for its entire lifetime -- which would
# make scripts/ensure_go_crond_alive.sh unable to EVER restart it, permanently
# disabling the watchdog. That is strictly worse than the double-start this lock was
# added to prevent, and it is the same descriptor-inheritance trap that left
# systrade's API holding its own cron flock indefinitely (found 2026-08-31).
# The lock's job ends here: from this point the cheap `pgrep -x go-crond` liveness
# check is what stops a second instance.
exec 9>&- 2>/dev/null || true

exec "${SCRIPT_DIR}/go-crond" "${crontab_path}" --allow-unprivileged
