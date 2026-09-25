"""The rebuilt stock universe, Layer 1 (docs/UNIVERSE_PRD.md sections 3 and 7).

Layer 1 is the same for every stock and only EXCLUDES. Built so far (PRD build step 1),
all from tables stockey already collects:

  1. NSE main board: series EQ on the latest session (SME SM/ST and trade-to-trade BE out)
  3. market cap >= Rs 300 cr (latest nseindia_mcap row)
  4. median daily traded value over the last 63 sessions >= Rs 50 L
  5. listed >= 252 sessions

Not built yet (build step 2): 2 surveillance list (ASM/GSM), 6 promoter pledge < 50%.
Nothing reads this module in the nightly pipeline until the operator signs off the
counts (build step 5); until then `python -m fundamentals.screens.universe` reports.

Definitions match systrader research/scripts/2026-09-25_universe_recall.py (LEDGER 44),
which tested these rules on history, except that this runs for today only and uses the
real market cap rather than an estimate.
"""

from __future__ import annotations

import argparse
import json

import pandas as pd

from utils.db import sql_to_df

MCAP_FLOOR_RS = 300e7  # Rs 300 cr
VALUE_FLOOR_RS = 50e5  # Rs 50 lakh a day
VALUE_WINDOW_SESSIONS = 63
VALUE_MIN_SESSIONS = 40  # a stock traded on fewer of the 63 sessions has no honest median
MIN_LISTED_SESSIONS = 252

# One row per stock trading as EQ on the latest session, with the inputs every rule reads.
# Every read of nseindia_ohlcv is date-bounded. Listing age counts sessions under the
# stock's current ISIN OR its current symbol, whichever is longer: a face-value split
# issues a new ISIN (NAZARA, ADANIPOWER 2025-09) and a rename changes the symbol, and
# either alone would make a years-old stock look newly listed.
LAYER1_INPUTS_QUERY = """
WITH sessions AS (
    SELECT DISTINCT date FROM nseindia_ohlcv
     WHERE date >= now() - interval '600 days' AND series = 'EQ'
), ranked AS (
    SELECT date, row_number() OVER (ORDER BY date DESC) AS k FROM sessions
), latest_session AS (
    SELECT max(date) AS d FROM sessions
), latest AS (
    SELECT o.symbol, o.isin, o.company_master_id, o.close
      FROM nseindia_ohlcv o, latest_session s
     WHERE o.date >= now() - interval '15 days' AND o.date = s.d AND o.series = 'EQ'
), mcap AS (
    SELECT DISTINCT ON (symbol) symbol, category, market_cap_rs, date AS mcap_date
      FROM nseindia_mcap
     WHERE date >= now() - interval '15 days' AND series = 'EQ'
     ORDER BY symbol, date DESC
), traded_value AS (
    SELECT o.symbol, count(*) AS value_sessions,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY o.total_value) AS median_value_rs
      FROM nseindia_ohlcv o
      JOIN ranked r ON r.date = o.date AND r.k <= %(value_window)s
     WHERE o.date >= now() - interval '150 days' AND o.series IN ('EQ', 'BE')
     GROUP BY o.symbol
), history AS (
    SELECT DISTINCT o.date, o.isin, o.symbol
      FROM nseindia_ohlcv o
     WHERE o.date >= now() - interval '600 days' AND o.series IN ('EQ', 'BE', 'SM', 'ST')
), age_by_isin AS (
    SELECT isin, count(DISTINCT date) AS n FROM history
     WHERE isin IN (SELECT isin FROM latest) GROUP BY isin
), age_by_symbol AS (
    SELECT symbol, count(DISTINCT date) AS n FROM history
     WHERE symbol IN (SELECT symbol FROM latest) GROUP BY symbol
)
SELECT l.symbol, l.isin, l.company_master_id, s.d AS session_date,
       m.category, m.market_cap_rs, m.mcap_date,
       v.median_value_rs, v.value_sessions,
       greatest(coalesce(ai.n, 0), coalesce(asym.n, 0)) AS listed_sessions
  FROM latest l
  CROSS JOIN latest_session s
  LEFT JOIN mcap m ON m.symbol = l.symbol
  LEFT JOIN traded_value v ON v.symbol = l.symbol
  LEFT JOIN age_by_isin ai ON ai.isin = l.isin
  LEFT JOIN age_by_symbol asym ON asym.symbol = l.symbol
 ORDER BY l.symbol
"""


def load_layer1_inputs() -> pd.DataFrame:
    return sql_to_df(LAYER1_INPUTS_QUERY, params={"value_window": VALUE_WINDOW_SESSIONS})


def apply_layer1_rules(inputs: pd.DataFrame) -> pd.DataFrame:
    """Adds one boolean column per rule, `layer1_pass`, and `fail_reasons` (every rule a
    stock fails, not just the first). Rule 1 holds by construction: the inputs are the
    stocks trading as EQ on the latest session."""
    df = inputs.copy()
    mcap = pd.to_numeric(df["market_cap_rs"], errors="coerce")
    value = pd.to_numeric(df["median_value_rs"], errors="coerce")
    value_sessions = pd.to_numeric(df["value_sessions"], errors="coerce").fillna(0)
    listed = pd.to_numeric(df["listed_sessions"], errors="coerce").fillna(0)

    df["rule1_main_board"] = True
    df["rule3_mcap"] = mcap >= MCAP_FLOOR_RS  # NaN (no mcap row: ETFs, bonds) fails
    df["rule4_traded_value"] = (value >= VALUE_FLOOR_RS) & (value_sessions >= VALUE_MIN_SESSIONS)
    df["rule5_listed"] = listed >= MIN_LISTED_SESSIONS
    rules = ["rule1_main_board", "rule3_mcap", "rule4_traded_value", "rule5_listed"]
    df["layer1_pass"] = df[rules].all(axis=1)

    def reasons(row) -> list[str]:
        out = []
        if not row["rule3_mcap"]:
            out.append("no market cap" if pd.isna(row["market_cap_rs"]) else "market cap < Rs 300 cr")
        if not row["rule4_traded_value"]:
            if row["value_sessions"] is None or pd.isna(row["value_sessions"]) or row["value_sessions"] < VALUE_MIN_SESSIONS:
                out.append("traded on < 40 of 63 sessions")
            else:
                out.append("traded value < Rs 50 L")
        if not row["rule5_listed"]:
            out.append("listed < 252 sessions")
        return out

    df["fail_reasons"] = df.apply(reasons, axis=1)
    return df


def layer1_report(df: pd.DataFrame) -> dict[str, object]:
    rules = ["rule3_mcap", "rule4_traded_value", "rule5_listed"]
    only_fail = {
        r: int((~df[r] & df[[x for x in rules if x != r]].all(axis=1)).sum()) for r in rules
    }
    return {
        "session_date": str(pd.Timestamp(df["session_date"].iloc[0]).date()) if len(df) else None,
        "eq_on_latest_session": len(df),
        "fails": {r: int((~df[r]).sum()) for r in rules},
        "fails_only_this_rule": only_fail,
        "pass_rules_1_3_4_5": int(df["layer1_pass"].sum()),
        "pass_by_category": {str(k): int(v) for k, v in df.loc[df["layer1_pass"], "category"].value_counts().items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Report the Layer 1 universe count (docs/UNIVERSE_PRD.md).")
    parser.add_argument("--csv", help="also write every EQ stock with its rule results to this path")
    args = parser.parse_args()
    df = apply_layer1_rules(load_layer1_inputs())
    if args.csv:
        out = df.copy()
        out["fail_reasons"] = out["fail_reasons"].map("; ".join)
        out.to_csv(args.csv, index=False)
    print(json.dumps(layer1_report(df), indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
