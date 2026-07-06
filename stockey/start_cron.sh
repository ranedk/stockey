#!/usr/bin/env bash

# Start the Stockey scheduler. Always reconciles daily OHLCV coverage FIRST so a
# scheduler that was down during market hours does not begin the day on stale bars
# (stale bars make the freshness gates suppress all buy authority). go-crond does not
# support @reboot entries, so this wrapper is the supported way to start the cron.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
crontab_path="${1:-${SCRIPT_DIR}/config/stockey.generated.crontab}"

echo "[start_cron] step 1/2: OHLCV reconciliation (universe daily-bar catch-up)"
if ! "${SCRIPT_DIR}/scripts/with_lock.sh" /tmp/stockey_ohlcv_reconcile.lock "${SCRIPT_DIR}/all_ohlcv_reconcile.sh"; then
  # The scheduler starting matters more than the catch-up succeeding: watchers and the
  # scheduled reconcile retry it, and the Operator Health OHLCV coverage check keeps the
  # gap visible. Never silent, never fatal.
  echo "[start_cron] WARNING: OHLCV reconciliation failed; starting scheduler anyway." >&2
  echo "[start_cron] Inspect with: ./all_ohlcv_reconcile.sh --dry-run --format json" >&2
fi

echo "[start_cron] step 2/2: starting go-crond crontab=${crontab_path}"
exec "${SCRIPT_DIR}/go-crond" "${crontab_path}" --allow-unprivileged
