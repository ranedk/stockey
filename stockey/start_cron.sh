#!/usr/bin/env bash

# Start the Stockey scheduler. Always reconciles daily OHLCV coverage FIRST so a
# scheduler that was down during market hours does not begin the day on stale bars
# (stale bars make the freshness gates suppress all buy authority). go-crond does not
# support @reboot entries, so this wrapper is the supported way to start the cron.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
crontab_path="${1:-${SCRIPT_DIR}/config/stockey.generated.crontab}"
GO_CROND_BIN="${SCRIPT_DIR}/go-crond"

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
if pgrep -f "^${GO_CROND_BIN} " >/dev/null 2>&1; then
  echo "[start_cron] go-crond is already running (pgrep -f \"^${GO_CROND_BIN} \" matched) -- not starting a second instance. Stop the existing process first if you intend to restart with a different crontab." >&2
  exit 0
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
exec "${SCRIPT_DIR}/go-crond" "${crontab_path}" --allow-unprivileged
