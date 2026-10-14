#!/usr/bin/env bash
# Once-only 2023-2024 check of the results confirmation rule (LEDGER row 57, pre-registration
# addendum 2026-10-14): fetch NSE XBRL quarters 2022-01..2024-12 for the check entries' stocks,
# then run the unchanged test ONCE. Refuses to run the test twice.
cd "$(dirname "$0")/.." && source .xstockey/bin/activate
OUT=logs/results_confirmation_check.json
echo "=== check fetch start $(date -u)"
python -u - <<'PY'
from utils.db import sql_to_df
from fundamentals.trigger_study.nse_results import fetch, CHECK_FIRST_PERIOD, CHECK_LAST_PERIOD
syms = sql_to_df("SELECT DISTINCT symbol FROM fundamentals_trigger_events WHERE sample = 'check'")["symbol"].tolist()
print(fetch(syms, first_period=CHECK_FIRST_PERIOD, last_period=CHECK_LAST_PERIOD), flush=True)
PY
echo "=== check fetch done $(date -u)"
if [ -e "$OUT" ]; then echo "check already ran ($OUT exists) -- not running it again"; exit 0; fi
python -u - <<'PY'
import json
from fundamentals.trigger_study import events as ev
from fundamentals.trigger_study.results_test import build_rows, summarise
pn = ev.panel(ev.CHECK_PRICE_FROM, ev.CHECK_PRICE_TO)
df, ex = build_rows(pn, samples=["check"], first_start="2023-01-01", last_start="2025-12-31")
out = summarise(df, ex)
df.to_csv("logs/results_confirmation_check_rows.csv", index=False)
json.dump(out, open("logs/results_confirmation_check.json", "w"), indent=1, default=str)
print(json.dumps(out, indent=1, default=str))
PY
echo "=== check done $(date -u)"
