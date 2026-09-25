"""The rebuilt stock universe, Layer 1 (docs/UNIVERSE_PRD.md sections 3 and 7).

Layer 1 is the same for every stock and only EXCLUDES (PRD build steps 1-2):

  1. NSE main board: series EQ on the latest session (SME SM/ST and trade-to-trade BE out)
  2. not under serious NSE surveillance: no GSM stage, no long- or short-term ASM stage >= 2
  3. market cap >= Rs 300 cr (latest nseindia_mcap row)
  4. median daily traded value over the last 63 sessions >= Rs 50 L
  5. listed >= 252 sessions

Rule 2 reads NSE's daily surveillance-indicator file (nseindia_surveillance_indicator).
ASM stage 1 is left in on purpose (operator, 2026-09-25): NSE applies it mechanically
after a large price move, and excluding it removed 87 stocks including WELCORP,
KIRLOSENG and TATACHEM -- the runners this universe exists to keep. Only a positive flag
excludes; a stock absent from the file passes and is counted (`no_surveillance_row`).

Rule 6 (promoter pledge < 50%) moved to Layer 2 (operator, 2026-09-25). NSE's "> 50%
encumbered" flag also counts parent and PE non-disposal undertakings (it removed VEDL,
HINDZINC, OBEROIRLTY, AFFLE, EUREKAFORB), and screener.in's pledge % covers only the
old ~190-name universe. The report still shows how many passing stocks carry the flag.

Nothing reads this module in the nightly pipeline until the operator signs off the
counts (build step 5); until then `python -m fundamentals.screens.universe` reports.

Definitions match systrader research/scripts/2026-09-25_universe_recall.py (LEDGER 44),
which tested these rules on history, except that this runs for today only and uses the
real market cap rather than an estimate.
"""

from __future__ import annotations

import argparse
import json
import re

import pandas as pd

from utils.db import sql_to_df

MCAP_FLOOR_RS = 300e7  # Rs 300 cr
VALUE_FLOOR_RS = 50e5  # Rs 50 lakh a day
VALUE_WINDOW_SESSIONS = 63
VALUE_MIN_SESSIONS = 40  # a stock traded on fewer of the 63 sessions has no honest median
MIN_LISTED_SESSIONS = 252
NOT_FLAGGED = 100  # NSE surveillance file: 100 = not flagged, anything else = stage/flag
SURVEILLANCE_COLUMNS = ("gsm", "lt_asm", "st_asm")
# lowest stage that excludes, per column (GSM: any stage, including stage 0)
SURVEILLANCE_MIN_STAGE = {"gsm": 0, "lt_asm": 2, "st_asm": 2}

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
           percentile_cont(0.5) WITHIN GROUP (ORDER BY o.total_value) AS median_value_rs,
           -- 1-month average volume: l5_sizing's ADV input (was screener.in's, in the v1 screen)
           avg(o.volume) FILTER (WHERE r.k <= 21) AS avg_volume_1m
      FROM nseindia_ohlcv o
      JOIN ranked r ON r.date = o.date AND r.k <= %(value_window)s
     WHERE o.date >= now() - interval '150 days' AND o.series IN ('EQ', 'BE')
     GROUP BY o.symbol
), surveillance AS (
    SELECT DISTINCT ON (symbol) symbol, date AS surveillance_date, gsm, lt_asm, st_asm, esm, irp,
           encumbered_over_50
      FROM nseindia_surveillance_indicator
     WHERE date >= (now() - interval '15 days')::date AND series = 'EQ'
     ORDER BY symbol, date DESC
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
SELECT l.symbol, l.isin, l.company_master_id, s.d AS session_date, l.close,
       m.category, m.market_cap_rs, m.mcap_date,
       v.median_value_rs, v.value_sessions, v.avg_volume_1m,
       greatest(coalesce(ai.n, 0), coalesce(asym.n, 0)) AS listed_sessions,
       sv.surveillance_date, sv.gsm, sv.lt_asm, sv.st_asm, sv.esm, sv.irp, sv.encumbered_over_50
  FROM latest l
  CROSS JOIN latest_session s
  LEFT JOIN mcap m ON m.symbol = l.symbol
  LEFT JOIN traded_value v ON v.symbol = l.symbol
  LEFT JOIN age_by_isin ai ON ai.isin = l.isin
  LEFT JOIN age_by_symbol asym ON asym.symbol = l.symbol
  LEFT JOIN surveillance sv ON sv.symbol = l.symbol
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

    def excluding(col: str) -> pd.Series:
        v = pd.to_numeric(df[col], errors="coerce")
        return v.notna() & (v != NOT_FLAGGED) & (v >= SURVEILLANCE_MIN_STAGE[col])

    df["rule1_main_board"] = True
    df["rule2_surveillance"] = ~pd.concat([excluding(c) for c in SURVEILLANCE_COLUMNS], axis=1).any(axis=1)
    df["rule3_mcap"] = mcap >= MCAP_FLOOR_RS  # NaN (no mcap row: ETFs, bonds) fails
    df["rule4_traded_value"] = (value >= VALUE_FLOOR_RS) & (value_sessions >= VALUE_MIN_SESSIONS)
    df["rule5_listed"] = listed >= MIN_LISTED_SESSIONS
    rules = ["rule1_main_board", "rule2_surveillance", "rule3_mcap", "rule4_traded_value", "rule5_listed"]
    df["layer1_pass"] = df[rules].all(axis=1)

    def reasons(row) -> list[str]:
        out = []
        if not row["rule2_surveillance"]:
            out.append("on surveillance: " + ", ".join(
                f"{c.upper()} stage {int(row[c])}" for c in SURVEILLANCE_COLUMNS
                if pd.notna(row[c]) and row[c] != NOT_FLAGGED and row[c] >= SURVEILLANCE_MIN_STAGE[c]))
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


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").fillna(NOT_FLAGGED)


def layer1_report(df: pd.DataFrame) -> dict[str, object]:
    rules = ["rule2_surveillance", "rule3_mcap", "rule4_traded_value", "rule5_listed"]
    only_fail = {
        r: int((~df[r] & df[[x for x in rules if x != r]].all(axis=1)).sum()) for r in rules
    }
    return {
        "session_date": str(pd.Timestamp(df["session_date"].iloc[0]).date()) if len(df) else None,
        "eq_on_latest_session": len(df),
        "fails": {r: int((~df[r]).sum()) for r in rules},
        "fails_only_this_rule": only_fail,
        "pass_layer1": int(df["layer1_pass"].sum()),
        "no_surveillance_row": int(df["surveillance_date"].isna().sum()),
        # Not Layer 1 rules; shown so the operator can decide whether they should be.
        "passing_but_asm_stage1": int((df["layer1_pass"] & (_num(df["lt_asm"]).eq(1) | _num(df["st_asm"]).eq(1))).sum()),
        "passing_but_encumbered_over_50": int((df["layer1_pass"] & _num(df["encumbered_over_50"]).ne(NOT_FLAGGED)).sum()),
        "passing_but_esm": int((df["layer1_pass"] & pd.to_numeric(df["esm"], errors="coerce").fillna(NOT_FLAGGED).ne(NOT_FLAGGED)).sum()),
        "passing_but_irp": int((df["layer1_pass"] & pd.to_numeric(df["irp"], errors="coerce").fillna(NOT_FLAGGED).ne(NOT_FLAGGED)).sum()),
        "pass_by_category": {str(k): int(v) for k, v in df.loc[df["layer1_pass"], "category"].value_counts().items()},
    }


# --- Groups (docs/UNIVERSE_PRD.md section 4, PRD build step 3) --------------------------
#
# Four groups, by business model, each with its own Layer 2 checks. Operator decision
# 2026-09-25: non-financial companies are ONE group. The planned asset-heavy/light line had
# no natural break on either measure tried (fixed assets / sales: median 0.56, smooth;
# depreciation / sales: median 3.3%, smooth) and each misplaced well-known names (HUL,
# Wipro, Naukri heavy on fixed assets because acquired brands count; Trent heavy on
# depreciation because leases do), while all it changed in Layer 2 was the debt allowance.
#
# Labels come from the exchanges' shared four-level scheme. Every stock has one from
# Sharpely (master_sharpely_equity codes + fundamentals_sector_reference names, one bulk
# request); where BSE's own current label has been collected
# (fundamentals_industry_classification) it wins, because Sharpely's lags reclassification
# (checked 2026-09-25: 19 of 303 differ, e.g. ABREL still "Paper" after its demerger into
# real estate). RBI's NBFC register (fundamentals_rbi_nbfc_
# registry, matched by normalised company name) adds what the label cannot say: a core
# investment company (CIC) is a holding company whatever its label, and a registered
# lender labelled "Other Financial Services" is still a lender.

GROUP_LENDER = "lender"
GROUP_OTHER_FINANCIAL = "other_financial"
GROUP_REALTY_HOLDING = "realty_holding"
GROUP_OPERATING = "operating"
GROUP_UNLABELLED = "unlabelled"  # no industry label collected yet

LENDING_BASIC_INDUSTRIES = frozenset({
    "Public Sector Bank", "Private Sector Bank", "Other Bank",
    "Non Banking Financial Company (NBFC)", "Housing Finance Company",
    "Microfinance Institutions", "Financial Institution",
})
HOLDING_BASIC_INDUSTRIES = frozenset({"Holding Company", "Investment Company"})
RBI_HOLDING_CLASSES = frozenset({"CIC"})
RBI_LENDER_CLASSES = frozenset({"ICC", "HFC", "MFI", "IFC"})
FINANCIAL_SERVICES = "Financial Services"
REALTY = "Realty"


def normalise_company_name(name) -> str:
    s = str(name or "").lower()
    s = re.sub(r"\(formerly.*$", "", s).replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\b(limited|ltd|the|co|company|corporation|corpn|private|pvt|india)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


SHARPELY_LABELS_QUERY = """
WITH names AS (
    SELECT DISTINCT ON (code) code, description FROM fundamentals_sector_reference ORDER BY code, as_of_date DESC
)
SELECT DISTINCT ON (e.isin) e.isin, NULL::text AS macro_sector, s.description AS sector,
       i.description AS industry, b.description AS basic_industry
  FROM master_sharpely_equity e
  LEFT JOIN names s ON s.code = e.sector_code
  LEFT JOIN names i ON i.code = e.industry_code
  LEFT JOIN names b ON b.code = e.nse_basic_ind_code
 WHERE e.isin IS NOT NULL AND e.nse_basic_ind_code IS NOT NULL
 ORDER BY e.isin, e.nse_active DESC NULLS LAST
"""


def load_group_inputs() -> pd.DataFrame:
    """Per company_master_id / isin: latest industry label and RBI classification."""
    from fundamentals.collectors.industry_classification import load_latest_industry
    from fundamentals.collectors.rbi_nbfc_registry import load_latest_registry

    cols = ["isin", "macro_sector", "sector", "industry", "basic_industry"]
    exchange = load_latest_industry()
    exchange = exchange[cols].assign(label_source="bse") if not exchange.empty else pd.DataFrame(columns=[*cols, "label_source"])
    sharpely = sql_to_df(SHARPELY_LABELS_QUERY).assign(label_source="sharpely")
    labels = pd.concat([exchange, sharpely[~sharpely["isin"].isin(exchange["isin"])]], ignore_index=True)
    names = sql_to_df("SELECT company_master_id, company_name FROM company_master WHERE nse_ticker IS NOT NULL")
    rbi = load_latest_registry()
    rbi = rbi[rbi["is_listed"].astype(bool)].copy()
    rbi["_k"] = rbi["nbfc_name"].map(normalise_company_name)
    names["_k"] = names["company_name"].map(normalise_company_name)
    matched = names.merge(rbi[["_k", "classification"]].drop_duplicates("_k"), on="_k", how="inner")
    rbi_by_cmid = matched.drop_duplicates("company_master_id").set_index("company_master_id")["classification"]
    return labels, rbi_by_cmid.rename("rbi_classification")


def assign_groups(df: pd.DataFrame, labels: pd.DataFrame, rbi_by_cmid: pd.Series) -> pd.DataFrame:
    out = df.merge(labels, on="isin", how="left")
    out["rbi_classification"] = out["company_master_id"].map(rbi_by_cmid)

    def group(row) -> str:
        basic, rbi_class = row["basic_industry"], row["rbi_classification"]
        financial = FINANCIAL_SERVICES in (row["macro_sector"], row["sector"])
        if rbi_class in RBI_HOLDING_CLASSES or basic in HOLDING_BASIC_INDUSTRIES:
            return GROUP_REALTY_HOLDING
        if REALTY in (row["macro_sector"], row["sector"]):
            return GROUP_REALTY_HOLDING
        if basic in LENDING_BASIC_INDUSTRIES:
            return GROUP_LENDER
        if financial and rbi_class in RBI_LENDER_CLASSES:
            return GROUP_LENDER
        if financial:
            return GROUP_OTHER_FINANCIAL
        if pd.isna(basic):
            return GROUP_UNLABELLED
        return GROUP_OPERATING

    out["group"] = out.apply(group, axis=1)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Report the Layer 1 universe count (docs/UNIVERSE_PRD.md).")
    parser.add_argument("--csv", help="also write every EQ stock with its rule results to this path")
    args = parser.parse_args()
    df = apply_layer1_rules(load_layer1_inputs())
    labels, rbi_by_cmid = load_group_inputs()
    df = assign_groups(df, labels, rbi_by_cmid)
    if args.csv:
        out = df.copy()
        out["fail_reasons"] = out["fail_reasons"].map("; ".join)
        out.to_csv(args.csv, index=False)
    report = layer1_report(df)
    report["groups"] = {str(k): int(v) for k, v in df.loc[df["layer1_pass"], "group"].value_counts().items()}
    print(json.dumps(report, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
