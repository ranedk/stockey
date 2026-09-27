"""Daily, dated, market-wide snapshot of screener.in fundamentals (TODO C4, 2026-09-27).

Point-in-time history for everything a quality / value / Layer 2 rule reads: every company
above Rs 250 cr, every night, stored under that day's date and never overwritten. With it,
"what did we know about company X on date D" is the newest row on or before D
(fundamentals/pit.py). Without it, quality and value can never be backtested -- the
figures screener.in shows today are restated, and yesterday's are gone.

Layer 2 (fundamentals/screens/universe_layer2.py) reads its general inputs from today's
snapshot instead of querying screener.in a second time; its loss-maker-only queries stay
there. screener.in reveals only ~3 queried columns per query, in query order, so each
query below asks for three fields behind always-true bounds. Default columns (price, P/E,
market cap, quarterly figures, ROCE) come with every query and are kept from one of them.

    python -m fundamentals.collectors.fundamentals_snapshot          # today's, if missing
    python -m fundamentals.collectors.fundamentals_snapshot --force
"""

from __future__ import annotations

import argparse
import json

import pandas as pd

from utils.company_master import map_company_master_ids_nse_or_bse
from utils.db import sql_to_df, upsert_to_db

SYNC_SOURCE_NAME = "fundamentals.collectors.fundamentals_snapshot"
SNAPSHOT_TABLE = "fundamentals_snapshot_daily"
_MCAP = "Market Capitalization > 250"
STOCKEY_RUN_STATE: dict[str, object] = {}

# name -> (query, {screener metric key: our column})
QUERIES: dict[str, tuple[str, dict[str, str]]] = {
    "ocf": ("Cash from operations last year > -10000000 AND Cash from operations preceding year > -10000000 AND "
            f"Operating cash flow 3years > -10000000 AND {_MCAP}",
            {"cf_operations_rscr": "ocf_y1", "cf_operations_py_rscr": "ocf_y2", "cf_opr_3yrs_rscr": "ocf_3y_total"}),
    "profit": ("Net Profit last year > -10000000 AND Net Profit preceding year > -10000000 AND "
               f"Net worth > -10000000 AND {_MCAP}",
               {"np_ann_rscr": "profit_y1", "np_prev_ann_rscr": "profit_y2", "net_worth_rscr": "net_worth"}),
    "debt": (f"Debt to equity > -10000 AND Interest Coverage Ratio > -1000000 AND {_MCAP}",
             {"debt___eq": "debt_to_equity", "int_coverage": "interest_cover"}),
    "misc": (f"Return on assets > -1000 AND Pledged percentage > -1 AND {_MCAP}",
             {"roa_12m_pct": "return_on_assets_pct", "pledged_pct": "promoter_pledged_pct"}),
    "quality": ("Return on equity > -100000 AND Average return on equity 5Years > -100000 AND "
                f"Average return on capital employed 5Years > -100000 AND {_MCAP}",
                {"roe_pct": "roe_pct", "roe_5yr_pct": "roe_5y_avg_pct", "roce_5yr_pct": "roce_5y_avg_pct"}),
    "value": (f"Earnings yield > -100000 AND Enterprise Value to EBIT > -100000 AND Price to book value > -100000 AND {_MCAP}",
              {"earnings_yield_pct": "earnings_yield_pct", "ev___ebit": "ev_to_ebit", "cmp___bv": "price_to_book"}),
    "growth": ("Sales growth 5Years > -1000 AND Profit growth 5Years > -100000 AND "
               f"Free cash flow 3years > -10000000 AND {_MCAP}",
               {"sales_var_5yrs_pct": "sales_growth_5y_pct", "profit_var_5yrs_pct": "profit_growth_5y_pct",
                "free_cash_flow_3yrs_rscr": "fcf_3y_total"}),
}
# Default columns every query returns; kept once, from the first query.
DEFAULT_FIELDS = {"cmp_rs": "price", "p_e": "pe", "mar_cap_rscr": "market_cap_cr", "roce_pct": "roce_pct",
                  "np_qtr_rscr": "profit_q", "qtr_profit_var_pct": "profit_growth_q_pct",
                  "sales_qtr_rscr": "sales_q", "qtr_sales_var_pct": "sales_growth_q_pct"}
FIELDS = {k: v for _, fields in QUERIES.values() for k, v in fields.items()}


def companies_to_frame(companies: list[dict], fields: dict[str, str], *, keep_identity: bool = False) -> pd.DataFrame:
    """One row per screener.in company with the given metric keys renamed. keep_identity
    also keeps name, URL, the full default-metrics dict and the default fields."""
    rows = []
    for c in companies:
        metrics = c.get("metrics", {}) or {}
        row = {"screener_company_id": c.get("company_id"), "screener_ticker": c.get("ticker")}
        if keep_identity:
            row.update({"screener_name": c.get("name"), "screener_url": c.get("url"), "screener_metrics": metrics})
            for key, name in DEFAULT_FIELDS.items():
                if key in metrics:
                    row[name] = metrics[key]
        for key, name in fields.items():
            if key in metrics:
                row[name] = metrics[key]
        rows.append(row)
    return pd.DataFrame(rows).drop_duplicates("screener_company_id") if rows else pd.DataFrame(
        columns=["screener_company_id", "screener_ticker"])


def fetch_snapshot(session=None) -> pd.DataFrame:
    """Run every query and join them into one wide frame keyed by screener company id."""
    from fundamentals.collectors.screenerin import build_authenticated_session, run_query

    session = session or build_authenticated_session()
    frames = []
    for i, (name, (query, fields)) in enumerate(QUERIES.items()):
        _, companies = run_query(session, query)
        revealed = set().union(*(c.get("metrics", {}).keys() for c in companies)) if companies else set()
        missing = [k for k in fields if k not in revealed]
        if companies and missing:
            # Column-reveal changed under us: fail loudly rather than store a silent gap.
            raise RuntimeError(f"screener.in did not reveal {missing} for the '{name}' snapshot query")
        frames.append(companies_to_frame(companies, fields, keep_identity=(i == 0 or name == "profit")))
    out = frames[0]
    for f in frames[1:]:
        dup = [c for c in f.columns if c in out.columns and c not in ("screener_company_id", "screener_ticker")]
        out = out.merge(f.drop(columns=dup), on=["screener_company_id", "screener_ticker"], how="outer")
    tickers = out["screener_ticker"].astype("string")
    out["company_master_id"] = map_company_master_ids_nse_or_bse(tickers).to_numpy()
    return out


def store_snapshot(df: pd.DataFrame, as_of_date) -> int:
    if df.empty:
        return 0
    rows = df.copy()
    rows["as_of_date"] = as_of_date
    rows["screener_metrics"] = rows["screener_metrics"].map(
        lambda m: json.dumps(m, ensure_ascii=False, default=str) if isinstance(m, dict) else None)
    rows["load_ts"] = pd.Timestamp.now(tz="UTC")
    rows = rows.dropna(subset=["screener_company_id"])
    rows["screener_company_id"] = rows["screener_company_id"].astype(int)
    upsert_to_db(rows, SNAPSHOT_TABLE, unique_keys=["as_of_date", "screener_company_id"])
    return len(rows)


def load_snapshot(as_of_date) -> pd.DataFrame:
    """The snapshot stored for exactly this date (empty if none)."""
    try:
        df = sql_to_df(f"SELECT * FROM {SNAPSHOT_TABLE} WHERE as_of_date = %s", params=(as_of_date,))
    except Exception:  # noqa: BLE001 -- table absent before the first snapshot
        return pd.DataFrame()
    if not df.empty and "screener_metrics" in df:
        df["screener_metrics"] = df["screener_metrics"].map(lambda s: json.loads(s) if isinstance(s, str) else None)
    return df


def ist_today():
    return pd.Timestamp.now(tz="Asia/Kolkata").date()


def snapshot_for_today(session=None) -> pd.DataFrame:
    """Today's snapshot, fetching and storing it first if it is not there yet."""
    today = ist_today()
    df = load_snapshot(today)
    if df.empty:
        store_snapshot(fetch_snapshot(session), today)
        df = load_snapshot(today)
    return df


def missing_weekdays(today) -> list[str]:
    """Weekdays from the first stored snapshot up to yesterday with no snapshot. A missed
    day is a permanent hole in the point-in-time history, so it must be visible."""
    try:
        have = sql_to_df(f"SELECT DISTINCT as_of_date FROM {SNAPSHOT_TABLE}")
    except Exception:  # noqa: BLE001 -- no table yet, nothing can be missing
        return []
    if have.empty:
        return []
    dates = {pd.Timestamp(d).date() for d in have["as_of_date"]}
    days = pd.bdate_range(min(dates), pd.Timestamp(today) - pd.Timedelta(days=1))
    return [str(d.date()) for d in days if d.date() not in dates]


def main(argv: list[str] | None = None) -> int:
    global STOCKEY_RUN_STATE
    parser = argparse.ArgumentParser(description="Store today's market-wide fundamentals snapshot.")
    parser.add_argument("--force", action="store_true", help="re-fetch even if today's snapshot exists")
    args = parser.parse_args(argv)
    today = ist_today()
    existing = load_snapshot(today)
    if args.force or existing.empty:
        rows = store_snapshot(fetch_snapshot(), today)
    else:
        rows = 0
    gaps = missing_weekdays(today)
    if gaps:
        from utils.fallback_telemetry import record_local_fallback_event

        record_local_fallback_event(
            module=SYNC_SOURCE_NAME, source="screenerin", fallback_type="fundamentals_snapshot_gap", severity="warn",
            reason=f"{len(gaps)} weekday(s) have no fundamentals snapshot -- permanent holes in the point-in-time history",
            error="missing snapshot days", metadata={"missing": gaps[-30:]},
        )
    STOCKEY_RUN_STATE = {"source": SYNC_SOURCE_NAME, "as_of_date": str(today), "rows": rows, "rows_written": rows,
                         "already_present": int(len(existing)) if rows == 0 else 0, "missing_weekdays": gaps[-30:],
                         "fallback_used": bool(gaps), "state_advanced": rows > 0}
    print(json.dumps({"status": "ok", **STOCKEY_RUN_STATE}, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
