"""Results reading (reevaluation PRD step 3, sec 3.5): each company's latest quarter read
against ITS OWN recent trend, with one-offs stripped, feeding the story score's Growth and
Margins dimensions.

Source: screener.in's Quarterly Results table (13 quarters), which the L2 crawl already
fetches on every company-page visit and used to throw away. The crawl stores it here
(`fundamentals_quarterly_results`), keyed by (company_id, period_end), first-seen values
kept: a later restatement does not rewrite what was known then, and `first_seen_at` is
the point-in-time clock. A results filing pulls the company's crawl forward
(bse_announcements -> l2_state.pull_crawl_forward), so a new quarter lands within a night
or two of the filing. The structured extraction of the filing itself (RESULTS_SCHEMA) is
kept for the filing-day read; it covers one filing per company, not a trend.

Per company, from the latest quarter t (all YoY, so seasonality cancels):
  - sales_yoy, and sales_vs_trend = sales_yoy minus the median YoY of the 4 quarters before t
    ("accelerating" / "slowing" against the company's own recent pace, not the market's);
  - underlying profit = PBT with this quarter's other income replaced by its median over the
    4 prior quarters. Screener nets exceptional items into Other Income (TCS Mar 2026:
    -Rs 1,168 cr), so a one-off shows up there as a jump from the company's normal level;
    profit_yoy / profit_vs_trend read the underlying profit, and one_off_share records how
    much of reported PBT was the jump;
  - margin_change = operating margin now minus a year ago (pp), margin_vs_trend = that change
    minus the median of the 4 prior quarters' changes.
Lenders' tables carry Revenue / Financing Profit / Financing Margin %; they map onto the same
fields, and their other income (fees) is normal income, which the median treatment keeps.

    python -m fundamentals.screens.results_reading [--as-of YYYY-MM-DD] [--backfill-quarters]
"""

from __future__ import annotations

import argparse
import json
import time

import numpy as np
import pandas as pd

from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db

SYNC_SOURCE_NAME = "fundamentals.screens.results_reading"
QUARTERS_TABLE = "fundamentals_quarterly_results"
RESULTS_TABLE = "fundamentals_results_reading"
# 2 (2026-10-02): profit base guard, trend window by date, quarters filled not frozen blank
READING_VERSION = 2
STOCKEY_RUN_STATE: dict[str, object] = {}

# screener.in row label -> field. Lenders' layout uses the second label of each pair.
LABELS = {
    "Sales": "sales", "Revenue": "sales",
    "Expenses": "expenses",
    "Operating Profit": "operating_profit", "Financing Profit": "operating_profit",
    "OPM %": "opm_pct", "Financing Margin %": "opm_pct",
    "Other Income": "other_income",
    "Interest": "interest",
    "Depreciation": "depreciation",
    "Profit before tax": "pbt",
    "Tax %": "tax_pct",
    "Net Profit": "net_profit",
    "EPS in Rs": "eps",
}
FIELDS = sorted(set(LABELS.values()))
# the trend window: the quarters in the 12 months before the latest one, chosen by date (prior_window)
# a latest quarter older than this is not "the latest results" any more
MAX_QUARTER_AGE_DAYS = 200
# a turn in pace smaller than this (pp of YoY growth) is noise, not a change of direction
DIRECTION_BAND_PP = 5.0
# one-off share of reported PBT worth naming
ONE_OFF_NOTABLE_SHARE = 0.25
# Profit growth is read only off a real base: the year-ago underlying profit must be at least
# this many Rs cr AND this share of year-ago sales. Below it, +36,600% (PPLPHARMA, Rs 0.5 cr ->
# 183 cr, review 2026-10-02) is a turnaround, not growth, and topped every group's ranking.
PROFIT_BASE_MIN_CR = 5.0
PROFIT_BASE_MIN_SHARE_OF_SALES = 0.05

_QUARTERS_DDL = f"""
    CREATE TABLE IF NOT EXISTS {QUARTERS_TABLE} (
        company_id BIGINT NOT NULL,
        period_end DATE NOT NULL,
        period_label TEXT,
        ticker TEXT,
        layout TEXT,
        {", ".join(f"{f} DOUBLE PRECISION" for f in FIELDS)},
        first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (company_id, period_end)
    )
"""
_RESULTS_DDL = f"""
    CREATE TABLE IF NOT EXISTS {RESULTS_TABLE} (
        as_of_date DATE NOT NULL,
        company_id BIGINT NOT NULL,
        reading_version INTEGER NOT NULL,
        ticker TEXT,
        latest_period_end DATE,
        quarters_seen INTEGER,
        sales_q_cr DOUBLE PRECISION,
        sales_yoy_pct DOUBLE PRECISION,
        sales_trend_yoy_pct DOUBLE PRECISION,
        sales_vs_trend_pp DOUBLE PRECISION,
        sales_direction TEXT,
        underlying_pbt_cr DOUBLE PRECISION,
        profit_yoy_pct DOUBLE PRECISION,
        profit_trend_yoy_pct DOUBLE PRECISION,
        profit_vs_trend_pp DOUBLE PRECISION,
        profit_direction TEXT,
        one_off_cr DOUBLE PRECISION,
        one_off_share DOUBLE PRECISION,
        opm_pct DOUBLE PRECISION,
        margin_change_pp DOUBLE PRECISION,
        margin_vs_trend_pp DOUBLE PRECISION,
        load_ts TIMESTAMPTZ,
        PRIMARY KEY (as_of_date, company_id, reading_version)
    )
"""


def ensure_tables() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute(_QUARTERS_DDL)
            cur.execute(_RESULTS_DDL)

    execute_db_operation(_op, operation_name=f"{QUARTERS_TABLE}:ensure_tables")


# --- parsing and storing the quarterly table ------------------------------------------------

def period_end(label: str | None):
    """'Jun 2026' -> 2026-06-30; anything else -> None."""
    try:
        ts = pd.to_datetime(label, format="%b %Y")
    except (TypeError, ValueError):
        return None
    if pd.isna(ts):  # None / "" parse to NaT, which would break the NOT NULL key
        return None
    return (pd.Timestamp(ts) + pd.offsets.MonthEnd(0)).date()


def quarter_rows(company_id, ticker, table: dict) -> list[dict]:
    """One row per period column of a parsed #quarters table (l2_state._parse_period_table)."""
    periods, rows = table.get("periods") or [], table.get("rows") or {}
    layout = "lender" if "Financing Profit" in rows else "operating"
    out = []
    for i, label in enumerate(periods):
        end = period_end(label)
        if end is None:
            continue
        rec = {"company_id": int(company_id), "period_end": end, "period_label": label, "ticker": ticker, "layout": layout}
        for src, field in LABELS.items():
            values = rows.get(src)
            if values is not None and i < len(values) and isinstance(values[i], (int, float)):
                rec[field] = float(values[i])
        out.append(rec)
    return out


def store_quarters(rows: list[dict]) -> int:
    """Insert new (company, quarter) rows; on an existing row only FILL fields still NULL
    (a quarter first seen with blank cells is completed later) -- a value once seen is never
    rewritten, so restatements do not change history. Returns rows inserted or filled."""
    if not rows:
        return 0
    ensure_tables()
    cols = ["company_id", "period_end", "period_label", "ticker", "layout", *FIELDS]
    values = [tuple(None if (isinstance(r.get(c), float) and np.isnan(r.get(c))) else r.get(c) for c in cols) for r in rows]
    fill = ", ".join(f"{f} = COALESCE({QUARTERS_TABLE}.{f}, EXCLUDED.{f})" for f in FIELDS)
    changed_if = " OR ".join(f"({QUARTERS_TABLE}.{f} IS NULL AND EXCLUDED.{f} IS NOT NULL)" for f in FIELDS)
    sql = (f"INSERT INTO {QUARTERS_TABLE} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "  # noqa: S608
           f"ON CONFLICT (company_id, period_end) DO UPDATE SET {fill} WHERE {changed_if}")

    def _op() -> int:
        n = 0
        with db_session() as (_, cur):
            for v in values:
                cur.execute(sql, v)
                n += cur.rowcount
        return n

    return int(execute_db_operation(_op, operation_name=f"{QUARTERS_TABLE}:store") or 0)


# --- the reading --------------------------------------------------------------------------

def _yoy(now, then):
    if now is None or then is None or pd.isna(now) or pd.isna(then) or then <= 0:
        return np.nan
    return (now / then - 1.0) * 100.0


def _direction(pp) -> str | None:
    if pd.isna(pp):
        return None
    return "accelerating" if pp > DIRECTION_BAND_PP else "slowing" if pp < -DIRECTION_BAND_PP else "steady"


def read_company(q: pd.DataFrame, as_of) -> dict | None:
    """q: one company's quarters (period_end, fields), any order. None when too little history."""
    q = q.dropna(subset=["period_end"]).sort_values("period_end").reset_index(drop=True)
    if q.empty:
        return None
    ends = [pd.Timestamp(e) for e in q["period_end"]]
    latest = len(q) - 1
    if (pd.Timestamp(as_of) - ends[latest]).days > MAX_QUARTER_AGE_DAYS:
        return None
    idx = {e: i for i, e in enumerate(ends)}

    def year_ago(i):
        return idx.get(ends[i] - pd.offsets.MonthEnd(12))

    def prior_window(i):
        """Quarters in the 12 months before quarter i, by DATE (a missing quarter must not
        stretch the window back in time)."""
        start = ends[i] - pd.offsets.MonthEnd(12)
        return [k for k in range(i) if ends[k] >= start]

    # underlying PBT: this quarter's other income swapped for its recent median
    oi = q.get("other_income", pd.Series(np.nan, index=q.index))
    pbt = q.get("pbt", pd.Series(np.nan, index=q.index))

    def normal_other_income(i):
        prior = oi.iloc[prior_window(i)].dropna()
        return float(prior.median()) if len(prior) >= 2 else np.nan

    def underlying(i):
        n = normal_other_income(i)
        if pd.isna(pbt.iloc[i]) or pd.isna(oi.iloc[i]) or pd.isna(n):
            return np.nan
        return float(pbt.iloc[i] - oi.iloc[i] + n)

    def series_yoy(value_at):
        out = {}
        for i in range(len(q)):
            j = year_ago(i)
            if j is not None:
                out[i] = _yoy(value_at(i), value_at(j))
        return out

    sales = q["sales"] if "sales" in q else pd.Series(np.nan, index=q.index)
    sales_yoy = series_yoy(lambda i: sales.iloc[i])

    def profit_yoy_at(i):
        j = year_ago(i)
        base, then_sales = underlying(j), sales.iloc[j]
        if pd.isna(base) or base < PROFIT_BASE_MIN_CR or pd.isna(then_sales) or then_sales <= 0 \
                or base < PROFIT_BASE_MIN_SHARE_OF_SALES * then_sales:
            return np.nan
        return _yoy(underlying(i), base)

    profit_yoy = {i: profit_yoy_at(i) for i in range(len(q)) if year_ago(i) is not None}
    opm = q.get("opm_pct", pd.Series(np.nan, index=q.index))
    margin_chg = {i: (opm.iloc[i] - opm.iloc[j]) for i in range(len(q)) if (j := year_ago(i)) is not None
                  and pd.notna(opm.iloc[i]) and pd.notna(opm.iloc[j])}

    def now_and_trend(series: dict):
        now = series.get(latest, np.nan)
        prior = [series[i] for i in prior_window(latest) if i in series and pd.notna(series[i])]
        trend = float(np.median(prior)) if len(prior) >= 2 else np.nan
        return now, trend, (now - trend) if pd.notna(now) and pd.notna(trend) else np.nan

    s_now, s_trend, s_vs = now_and_trend(sales_yoy)
    p_now, p_trend, p_vs = now_and_trend(profit_yoy)
    m_now, _, m_vs = now_and_trend(margin_chg)
    n_oi = normal_other_income(latest)
    one_off = float(oi.iloc[latest] - n_oi) if pd.notna(oi.iloc[latest]) and pd.notna(n_oi) else np.nan
    reported = pbt.iloc[latest]
    share = abs(one_off) / abs(reported) if pd.notna(one_off) and pd.notna(reported) and reported != 0 else np.nan
    sales_q = sales.iloc[latest]
    return {
        "latest_period_end": ends[latest].date(), "quarters_seen": len(q),
        "sales_q_cr": sales_q, "sales_yoy_pct": s_now, "sales_trend_yoy_pct": s_trend,
        "sales_vs_trend_pp": s_vs, "sales_direction": _direction(s_vs),
        "underlying_pbt_cr": underlying(latest), "profit_yoy_pct": p_now, "profit_trend_yoy_pct": p_trend,
        "profit_vs_trend_pp": p_vs, "profit_direction": _direction(p_vs),
        "one_off_cr": one_off, "one_off_share": share,
        "opm_pct": opm.iloc[latest], "margin_change_pp": m_now, "margin_vs_trend_pp": m_vs,
    }


def load_quarters(as_of) -> pd.DataFrame:
    """Every stored quarter as it was known at the end of as_of (IST)."""
    ensure_tables()
    cutoff = pd.Timestamp(as_of).tz_localize("Asia/Kolkata") + pd.Timedelta(days=1)
    return sql_to_df(
        f"SELECT * FROM {QUARTERS_TABLE} WHERE first_seen_at < %s",  # noqa: S608 -- constant
        (cutoff.to_pydatetime(),),
    )


def readings(as_of, quarters: pd.DataFrame | None = None) -> pd.DataFrame:
    quarters = load_quarters(as_of) if quarters is None else quarters
    rows = []
    for company_id, q in quarters.groupby("company_id"):
        r = read_company(q, as_of)
        if r:
            rows.append({"company_id": int(company_id), "ticker": q["ticker"].dropna().iloc[-1] if q["ticker"].notna().any() else None, **r})
    return pd.DataFrame(rows)


def run(as_of=None, *, store: bool = True) -> pd.DataFrame:
    as_of = pd.Timestamp(as_of).date() if as_of else pd.Timestamp.now(tz="Asia/Kolkata").date()
    out = readings(as_of)
    if out.empty:
        return out
    out["as_of_date"] = as_of
    out["reading_version"] = READING_VERSION
    out["load_ts"] = pd.Timestamp.now(tz="UTC")
    if store:
        upsert_to_db(out, RESULTS_TABLE, unique_keys=["as_of_date", "company_id", "reading_version"])
    return out


# --- one-time backfill of the quarterly table ------------------------------------------------

def backfill_quarters(limit: int | None = None) -> dict[str, object]:
    """Fetch every universe company's page once and store its quarters. Does NOT write L2
    state or touch crawl cooldowns -- it only fills the history the nightly crawl will extend.
    Companies that already have quarters stored are skipped, so it resumes after a stop."""
    from fundamentals.collectors.screenerin import build_authenticated_session
    from fundamentals.screens.l1_universe import load_l1_universe_tickers
    from fundamentals.screens.l2_state import fetch_company_detail

    ensure_tables()
    have = set(sql_to_df(f"SELECT DISTINCT company_id FROM {QUARTERS_TABLE}")["company_id"].astype(int))  # noqa: S608
    todo = [r for r in load_l1_universe_tickers().itertuples() if r.ticker and int(r.company_id) not in have]
    if limit:
        todo = todo[:limit]
    session = build_authenticated_session()
    stored, failed = 0, []
    for n, r in enumerate(todo, 1):
        try:
            detail = fetch_company_detail(session, r.ticker)
            stored += store_quarters(quarter_rows(r.company_id, r.ticker, detail.get("quarters") or {}))
        except Exception as exc:  # noqa: BLE001 -- one company must not stop the backfill
            failed.append(r.ticker)
            print(json.dumps({"ticker": r.ticker, "error": repr(exc)[:200]}), flush=True)
            time.sleep(30)
        if n % 50 == 0:
            print(json.dumps({"done": n, "of": len(todo), "rows": stored, "failed": len(failed)}), flush=True)
    return {"companies": len(todo), "rows": stored, "failed": failed}


def main(argv: list[str] | None = None) -> int:
    global STOCKEY_RUN_STATE
    parser = argparse.ArgumentParser(description="Results reading (reevaluation PRD step 3).")
    parser.add_argument("--as-of", help="read as of this date (default: today, IST)")
    parser.add_argument("--backfill-quarters", action="store_true", help="one-time: fetch every universe company's quarterly table")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    if args.backfill_quarters:
        print(json.dumps({"status": "ok", **backfill_quarters(args.limit)}), flush=True)
        return 0
    out = run(args.as_of)
    STOCKEY_RUN_STATE = {"source": SYNC_SOURCE_NAME, "rows": len(out), "rows_written": len(out),
                         "fallback_used": out.empty, "state_advanced": not out.empty}
    print(json.dumps({"status": "ok", **STOCKEY_RUN_STATE}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
