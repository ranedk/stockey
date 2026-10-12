#!/usr/bin/env bash
# NSE results for EVERY momentum entry's stock (incl. the "middle" outcomes) -- the results
# confirmation test (LEDGER row 57) must not condition on the outcome. Waits out busy windows.
cd "$(dirname "$0")/.." && source .xstockey/bin/activate
echo "=== results (all events) start $(date -u)"
python -u - <<'PY'
from utils.db import sql_to_df
from fundamentals.trigger_study.nse_results import fetch
syms = sql_to_df("SELECT DISTINCT symbol FROM fundamentals_trigger_events")["symbol"].tolist()
print(fetch(syms), flush=True)
PY
echo "=== results (all events) done $(date -u)"
