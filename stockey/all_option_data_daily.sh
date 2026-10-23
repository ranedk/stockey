#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

# Keep the Nifty options data current (2026-10-22): until now both tables were one-off backfills.
# systrader's forward paper track for LEDGER row 78 (expiry-day straddle) reads them every day.
# Morning run (10:00 IST: at 09:00, before the open, Dhan answered every call with 400 on
# 2026-10-23; the same calls worked at 13:00): option_rolling stops at yesterday (today's
# session is not finished), and the F&O bhavcopy is published ~18:00 IST, so both are final.
# The last 10 IST days are asked again each run; the window start moves daily, so the state table
# never skips it, and rows already stored are DO NOTHING upserts (reads, not WAL). The token is read
# from the cache only -- this job never logs in (consent rationing).
FROM_OPT="$(TZ=Asia/Kolkata date -d '-10 days' +%F)"
FROM_FO="$(TZ=Asia/Kolkata date -d '-7 days' +%F)"
TO="$(TZ=Asia/Kolkata date +%F)"
status=0
"${SCRIPT_DIR}/scripts/run_with_markers.sh" "fo_bhavcopy_daily" "${PYTHON_BIN}" -m data.nseindia.fo_bhavcopy \
  --from "${FROM_FO}" --to "${TO}" || status=$?
"${SCRIPT_DIR}/scripts/run_with_markers.sh" "option_rolling_daily" "${PYTHON_BIN}" -m data.dhanlive.option_rolling \
  --underlying NIFTY --from-date "${FROM_OPT}" "$@" || status=$?
exit "${status}"
