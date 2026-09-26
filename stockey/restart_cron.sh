#!/usr/bin/env bash

# Stop the running go-crond scheduler (if any) and start it again -- the supported way to
# safely restart the cron stack. "Safely" specifically means:
#   1. Actually waits for the old go-crond to exit (via stop_cron.sh) before starting a new
#      one -- never fires a new instance while the old one might still be alive.
#   2. Reuses start_cron.sh unmodified, so this also gets its already-running guard (added
#      2026-08-20 after a real double-start bug) and its OHLCV-reconcile-before-scheduling
#      step for free, instead of re-implementing a weaker version of either here.
#   3. Runs in the foreground and hands off to start_cron.sh/go-crond via `exec`, exactly
#      like running ./start_cron.sh directly -- Ctrl-C or backgrounding it is the operator's
#      choice, same as today, not a new behavior to learn.
#
# Usage: ./restart_cron.sh [crontab-path]   (crontab-path forwarded to start_cron.sh; both
#                                             default to config/stockey.generated.crontab)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[restart_cron] step 1/2: stopping go-crond (if running)" >&2
"${SCRIPT_DIR}/stop_cron.sh"

echo "[restart_cron] step 2/2: starting go-crond" >&2
exec "${SCRIPT_DIR}/start_cron.sh" "$@"
