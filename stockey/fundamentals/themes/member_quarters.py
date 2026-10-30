"""Quarterly results for theme members the nightly screener crawl does not reach
(docs/THEMATIC_WATCHLIST_PRD.md section 4.3, build step 4).

The L2 crawl stores screener.in's 13-quarter table for the L1 universe only, so about a third
of the theme members (large caps and BSE-only names outside L1's band) have no quarters, and
theme strength would read their themes from the smaller names alone. This job fetches those
members' company pages and stores their quarters in the same table
(`results_reading.store_quarters`, first-seen values kept), so one table serves both.

A member is due when it has no quarters stored, or its latest stored quarter ended more than
DUE_AFTER_DAYS ago (a new one should be out), and it was not fetched in the last
RETRY_AFTER_DAYS. Requests go through exchange_request_gate(domain="screenerin") inside
l2_state.fetch_company_detail.

    python -m fundamentals.themes.member_quarters [--limit N]
"""
from __future__ import annotations

import argparse
import json
import time

import pandas as pd

from fundamentals.screens import results_reading
from fundamentals.themes.strength import load_members
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.fallback_telemetry import record_local_fallback_event

FETCH_TABLE = "fundamentals_theme_quarters_fetch"
DUE_AFTER_DAYS = 100        # quarter end + ~45 days to file + screener's lag, with room
RETRY_AFTER_DAYS = 7

_DDL = f"""
    CREATE TABLE IF NOT EXISTS {FETCH_TABLE} (
        company_id BIGINT PRIMARY KEY,      -- screener.in company id, as in the quarters table
        company_master_id TEXT NOT NULL,
        slug TEXT NOT NULL,
        fetched_at TIMESTAMPTZ NOT NULL,
        quarters_stored INT,
        error TEXT
    )"""


def ensure_table() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute(_DDL)

    execute_db_operation(_op, operation_name=f"{FETCH_TABLE}:ensure_table")


def due(today: pd.Timestamp, limit: int | None = None) -> pd.DataFrame:
    from fundamentals.screens.l1_universe import load_l1_universe_tickers

    ensure_table()
    m = load_members(as_of=today.date())
    m = m.dropna(subset=["screener_company_id"]).drop_duplicates("company_master_id")
    in_l1 = set(load_l1_universe_tickers()["company_id"].astype(int))
    m = m[~m["screener_company_id"].isin(in_l1)]
    latest = sql_to_df(f"SELECT company_id, max(period_end) AS last_end FROM {results_reading.QUARTERS_TABLE}"
                       f" WHERE company_id = ANY(%(ids)s) GROUP BY 1",  # noqa: S608 -- constant
                       params={"ids": [int(x) for x in m["screener_company_id"]]})
    fetched = sql_to_df(f"SELECT company_id, fetched_at FROM {FETCH_TABLE}")  # noqa: S608 -- constant
    m = (m.merge(latest, left_on="screener_company_id", right_on="company_id", how="left").drop(columns="company_id")
          .merge(fetched, left_on="screener_company_id", right_on="company_id", how="left").drop(columns="company_id"))
    stale = m["last_end"].isna() | ((today.tz_localize(None) - pd.to_datetime(m["last_end"])).dt.days > DUE_AFTER_DAYS)
    fresh_fetch = pd.to_datetime(m["fetched_at"], utc=True) > today.tz_convert("UTC") - pd.Timedelta(days=RETRY_AFTER_DAYS)
    out = m[stale & ~fresh_fetch.fillna(False)]
    return out.head(limit) if limit else out


def run(limit: int | None = None) -> dict:
    from fundamentals.collectors.screenerin import build_authenticated_session
    from fundamentals.screens.l2_state import fetch_company_detail

    results_reading.ensure_tables()
    today = pd.Timestamp.now(tz="Asia/Kolkata").normalize()
    todo = due(today, limit)
    if todo.empty:
        return {"due": 0}
    session = build_authenticated_session()
    stored, failed = 0, 0
    for r in todo.itertuples(index=False):
        cid = int(r.screener_company_id)
        n, err = 0, None
        try:
            detail = fetch_company_detail(session, r.slug)
            n = results_reading.store_quarters(results_reading.quarter_rows(cid, r.ticker, detail.get("quarters") or {}))
        except Exception as exc:  # noqa: BLE001 -- one company must not stop the run
            err = repr(exc)[:300]
            failed += 1
            record_local_fallback_event(module="fundamentals.themes.member_quarters", fallback_type="company_page_failed",
                                        source="screenerin", symbol=r.ticker, severity="warn", reason=err)
            time.sleep(10)
        stored += n
        upsert_to_db(pd.DataFrame([{"company_id": cid, "company_master_id": r.company_master_id, "slug": r.slug,
                                    "fetched_at": pd.Timestamp.now(tz="UTC"), "quarters_stored": n, "error": err}]),
                     FETCH_TABLE, ["company_id"])
    return {"due": len(todo), "rows": stored, "failed": failed}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch quarterly results for theme members outside the L1 crawl.")
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args(argv)
    print(json.dumps(run(limit=a.limit), default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
