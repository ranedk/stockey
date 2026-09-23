#!/usr/bin/env bash

set -euo pipefail

# Out-of-band watchdog: checks whether go-crond itself is running, and if not, restarts it via
# the supported start_cron.sh entrypoint. MUST be registered in the OS-level user crontab
# (`crontab -e`), NEVER in go-crond's own generated crontab -- go-crond dying is exactly the
# failure mode this exists to catch, so it cannot depend on go-crond being alive to run it.
#
# BUG FOUND LIVE 2026-08-19 (re-audit): go-crond died silently for 5 days (2026-08-14 to
# 2026-08-19, discovered only because the user noticed a missing email) with zero alerting
# anywhere in this codebase. Every mechanism that would have caught it -- cron_preflight.py,
# data_readiness.py, data_coverage_report.py -- is itself a go-crond job, so none of them ran
# either. This is the first check in this repo that does not depend on go-crond being alive.
#
# Follows the same idiom already established for other long-running services in this repo
# (see all_fundamentals_api.sh / the stockey-service-management convention): a cheap liveness
# check first (exit 0 immediately if healthy, so this can run frequently and quietly), restart
# routed through a lock shared with start_cron.sh itself.
#
# CORRECTION 2026-09-02: this block used to claim the with_lock call meant "a concurrent
# manual restart wins cleanly". It did not, and the gap was reproduced live. The lock was
# only ever held by OTHER watchdog ticks -- a human running ./start_cron.sh took no lock at
# all -- and because start_cron.sh runs data_readiness --fix and a full ohlcv_reconcile
# BEFORE it execs go-crond, there is a long window (minutes, now ~30 with the raised
# reconcile cap) where go-crond is legitimately not yet running. A watchdog tick landing in
# that window saw "not running", declared the scheduler dead, and launched a SECOND
# start_cron.sh -- two concurrent Dhan reconciles competing for the same rate limit.
# Both sides now take CRON_START_LOCK, so whichever starts first wins and the other exits.
#
# MAINTENANCE MODE: a watchdog that always restarts makes a deliberate stop impossible --
# ./stop_cron.sh would be undone within the tick interval, which cost real time during the
# 2026-08-31 repair when cron had to be held down while data was being rebuilt. If
# CRON_MAINTENANCE_SENTINEL exists, this exits without restarting. It lives in /tmp on
# purpose: maintenance must not survive a reboot, or a forgotten sentinel would silently
# keep the scheduler off forever -- the exact failure this watchdog exists to prevent.
#
# No push-alert channel (email/Slack) is wired up here -- this repo has no general-purpose ops
# alerting config, only the fundamentals screener's own scoped SES pipeline (WATCHLIST_ALERT_
# EMAIL_*), and no mail transport is installed on this host (confirmed live: no sendmail/mail,
# no postfix). What this DOES guarantee: a dead scheduler self-heals within one watchdog
# interval instead of sitting silent for days, and every detection is recorded via fallback
# telemetry (record_local_fallback_event) plus a dedicated, always-checkable log file --
# visible to a human or a future Claude session reading logs/cron/, not silent.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${SCRIPT_DIR}"
LOG_FILE="${SCRIPT_DIR}/logs/cron/go_crond_watchdog.log"
mkdir -p "$(dirname "${LOG_FILE}")"

GO_CROND_BIN="${SCRIPT_DIR}/go-crond"
CRON_START_LOCK="/tmp/stockey_cron_start.lock"
CRON_MAINTENANCE_SENTINEL="/tmp/stockey_cron_maintenance"

if pgrep -x go-crond >/dev/null 2>&1; then
  exit 0  # alive -- stay quiet, this may run every few minutes
fi

if [ -e "${CRON_MAINTENANCE_SENTINEL}" ]; then
  echo "[go_crond_watchdog] $(date -u +%Y-%m-%dT%H:%M:%SZ) go-crond is down but ${CRON_MAINTENANCE_SENTINEL} exists -- maintenance mode, NOT restarting. Remove the file (or run ./start_cron.sh) to resume." | tee -a "${LOG_FILE}" >&2
  exit 0
fi

TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "[go_crond_watchdog] ${TIMESTAMP} go-crond is NOT running -- every scheduled job has been silently not firing. Recording telemetry and attempting an automatic restart." | tee -a "${LOG_FILE}" >&2

PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"
"${PYTHON_BIN}" -c "
from utils.fallback_telemetry import record_local_fallback_event
record_local_fallback_event(
    module='scripts.ensure_go_crond_alive',
    source='go-crond',
    fallback_type='go_crond_not_running',
    severity='error',
    reason=(
        'go-crond was found not running by the out-of-band watchdog -- every scheduled job '
        '(downloads, adjustment, readiness checks, the fundamentals screener) has been '
        'silently not firing since it died. An automatic restart via start_cron.sh was '
        'attempted; check logs/cron/go_crond_watchdog.log for the outcome.'
    ),
    error=None,
)
" >> "${LOG_FILE}" 2>&1 || echo "[go_crond_watchdog] ${TIMESTAMP} WARNING: fallback telemetry write itself failed -- see above" | tee -a "${LOG_FILE}" >&2

"${SCRIPT_DIR}/scripts/with_lock.sh" "${CRON_START_LOCK}" "${SCRIPT_DIR}/start_cron.sh" >> "${LOG_FILE}" 2>&1 &
disown
echo "[go_crond_watchdog] ${TIMESTAMP} restart attempted in the background (holding ${CRON_START_LOCK}, which start_cron.sh also takes -- so a manual start already in progress wins and this exits) -- see ${LOG_FILE} for start_cron.sh's own output" | tee -a "${LOG_FILE}" >&2
