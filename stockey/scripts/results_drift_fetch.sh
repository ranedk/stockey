#!/usr/bin/env bash
# NSE XBRL results 2019-07..2024-12 for every liquid non-financial NSE stock -- data for the
# post-results drift test (LEDGER row 58). Resumable: completion is tracked per stock under
# range_key 'drift_2019Q3_2024Q4'; already-stored quarters are skipped. Waits out the NSE
# busy windows (production jobs first).
cd "$(dirname "$0")/.." && source .xstockey/bin/activate
echo "=== drift fetch start $(date -u)"
python -u - <<'PY'
import pandas as pd
from sqlalchemy import text
from fundamentals.trigger_study.events import _systrader_conn
from fundamentals.trigger_study.nse_results import fetch
sql = """
WITH d AS (SELECT symbol, date_trunc('month', date) m,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY adj_close*adj_volume) tv
             FROM advisory_adjusted_ohlcv_daily
            WHERE date >= '2020-06-01' AND date < '2025-01-01' AND series = 'EQ' GROUP BY 1, 2)
SELECT DISTINCT d.symbol FROM d JOIN master_sharpely_equity s ON s.symbol = d.symbol
 WHERE d.tv >= 5e7 AND s.sector_code IS NOT NULL AND s.sector_code NOT IN ('IN0501', 'IN1201')
 ORDER BY 1"""
with _systrader_conn() as conn:
    syms = pd.read_sql(text(sql), conn)["symbol"].tolist()
print("universe", len(syms), flush=True)
print(fetch(syms, first_period=pd.Timestamp("2019-07-01"), last_period=pd.Timestamp("2024-12-31"),
            range_key="drift_2019Q3_2024Q4"), flush=True)
PY
echo "=== drift fetch done $(date -u)"
