#!/usr/bin/env bash
# Runs the pre-registered results confirmation test (LEDGER row 57) ONCE, after the NSE results
# fetch for every momentum entry's stock has finished.
cd "$(dirname "$0")/.." && source .xstockey/bin/activate
until grep -q "=== results (all events) done" logs/trigger_study_full.log; do sleep 120; done
python -u - <<'PY'
import json
from fundamentals.trigger_study.results_test import build_rows, summarise
df, ex = build_rows()
out = summarise(df, ex)
df.to_csv("logs/results_confirmation_rows.csv", index=False)
json.dump(out, open("logs/results_confirmation.json", "w"), indent=1, default=str)
print(json.dumps(out, indent=1, default=str))
PY
