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
# routed through with_lock.sh so a watchdog tick racing a human's own manual restart (this
# happened live on 2026-08-19 -- see start_cron.sh's own docstring) can't double-start it.
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

if pgrep -f "^${GO_CROND_BIN} " >/dev/null 2>&1; then
  exit 0  # alive -- stay quiet, this may run every few minutes
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

"${SCRIPT_DIR}/scripts/with_lock.sh" /tmp/stockey_go_crond_watchdog_restart.lock "${SCRIPT_DIR}/start_cron.sh" >> "${LOG_FILE}" 2>&1 &
disown
echo "[go_crond_watchdog] ${TIMESTAMP} restart attempted in the background (with_lock-protected, so a concurrent manual restart wins cleanly) -- see ${LOG_FILE} for start_cron.sh's own output" | tee -a "${LOG_FILE}" >&2
