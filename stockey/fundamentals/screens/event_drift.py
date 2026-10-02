"""Event drift, as a forward record (TODO C7, 2026-09-28).

Every rule-based L3 alert on a filing DETECTED on or after RECORD_START becomes one row:
a hypothetical entry at the close of the first session AFTER the IST date we detected the
filing (conservative -- a filing detected at 18:00 cannot be traded that day), measured over
20 / 40 / 60 sessions against the equal-weight universe over the same window. Pre-registered
in systrader research/LEDGER.md row 48: nine trigger types, judged together as one family
(FDR within the family, docs/RESEARCH_PROTOCOL.md), not one at a time.

Why detection time, not disclosure date: an event-drift study on the date the company filed
assumes we knew at that moment. fundamentals_events.detected_at (C4) is when stockey first
saw the filing; that is the only honest entry clock. Filings loaded as history or through the
one-time catch-up (admission_cohort) are excluded -- detected late by construction.

    python -m fundamentals.screens.event_drift            # record new alerts, fill entry prices
    python -m fundamentals.screens.event_drift --report   # excess returns per trigger type
"""

from __future__ import annotations

import argparse
import json

import pandas as pd

from utils.db import db_session, sql_to_df, upsert_to_db

SYNC_SOURCE_NAME = "fundamentals.screens.event_drift"
RECORD_TABLE = "fundamentals_event_drift"
RECORD_START = "2026-09-28"
HORIZONS = (20, 40, 60)
# Frozen with the pre-registration. +1 = the event should precede outperformance.
DIRECTION = {
    "insider_buy": 1, "bulk_deal_buy": 1, "results_confirm_turnaround": 1,
    "rating_confirms_deleveraging": 1, "capital_raise": 1,
    "bulk_deal_sell": -1, "results_decline": -1, "auditor_change": -1, "insider_sell_surprise": -1,
}
# Second family (systrader LEDGER row 51, pre-registered 2026-09-29): the LLM story read's
# alerts (story_read.py), same entry clock and horizons, judged separately from the nine rule
# triggers. Detection time is the read's own time (the alert's load_ts).
STORY_READ_START = "2026-09-30"
STORY_READ_DIRECTION = {"story_read_positive": 1, "story_read_negative": -1}
# One record per company per direction per window: each new piece of evidence can raise another
# alert on the same company, and counting them all would put overlapping samples of ONE price
# move into the family (review 2026-10-02, before the family's first record).
STORY_READ_DEDUP_DAYS = 90
STOCKEY_RUN_STATE: dict[str, object] = {}

_DDL = f"""
    CREATE TABLE IF NOT EXISTS {RECORD_TABLE} (
        source TEXT NOT NULL, news_id TEXT NOT NULL, trigger_type TEXT NOT NULL,
        direction INTEGER, company_master_id TEXT, symbol TEXT,
        detected_at TIMESTAMPTZ, entry_date DATE, entry_price DOUBLE PRECISION,
        delivery_change_pp DOUBLE PRECISION, upper_band_hits_20d INTEGER, lower_band_hits_20d INTEGER,
        load_ts TIMESTAMPTZ DEFAULT now(),
        PRIMARY KEY (source, news_id, trigger_type)
    )
"""


def ensure_table() -> None:
    with db_session() as (_, cur):
        cur.execute(_DDL)
        for col, typ in (("delivery_change_pp", "DOUBLE PRECISION"), ("upper_band_hits_20d", "INTEGER"),
                         ("lower_band_hits_20d", "INTEGER"), ("family", "TEXT")):
            cur.execute(f"ALTER TABLE {RECORD_TABLE} ADD COLUMN IF NOT EXISTS {col} {typ}")
        cur.execute("ALTER TABLE fundamentals_events ADD COLUMN IF NOT EXISTS admission_cohort TEXT")


def new_alerts() -> pd.DataFrame:
    """Rule alerts on fresh filings (detected since RECORD_START, not history, not the
    catch-up cohort) that are not recorded yet."""
    return sql_to_df(
        f"""
        SELECT a.source, a.news_id, a.trigger_type, a.company_master_id, e.detected_at
          FROM fundamentals_l3_alerts a
          JOIN fundamentals_events e ON e.source = a.source AND e.news_id = a.news_id
         WHERE a.origin = 'rule' AND a.trigger_type = ANY(%s)
           AND e.detected_at >= %s::timestamptz
           AND e.admission_cohort IS NULL
           AND NOT EXISTS (SELECT 1 FROM {RECORD_TABLE} r
                            WHERE r.source = a.source AND r.news_id = a.news_id AND r.trigger_type = a.trigger_type)
        """,
        params=(list(DIRECTION), f"{RECORD_START} 00:00:00+05:30"),
    )


def new_story_reads() -> pd.DataFrame:
    df = sql_to_df(
        f"""
        SELECT a.source, a.news_id, a.trigger_type, a.company_master_id, a.load_ts AS detected_at
          FROM fundamentals_l3_alerts a
         WHERE a.source = 'story_read' AND a.trigger_type = ANY(%s)
           AND a.load_ts >= %s::timestamptz
           AND NOT EXISTS (SELECT 1 FROM {RECORD_TABLE} r
                            WHERE r.source = a.source AND r.news_id = a.news_id AND r.trigger_type = a.trigger_type)
           AND NOT EXISTS (SELECT 1 FROM {RECORD_TABLE} r
                            WHERE r.family = 'story_read' AND r.company_master_id = a.company_master_id
                              AND r.trigger_type = a.trigger_type
                              AND r.detected_at > a.load_ts - make_interval(days => %s))
         ORDER BY a.load_ts
        """,
        params=(list(STORY_READ_DIRECTION), f"{STORY_READ_START} 00:00:00+05:30", STORY_READ_DEDUP_DAYS),
    )
    return dedupe_story_reads(df)


def dedupe_story_reads(df: pd.DataFrame) -> pd.DataFrame:
    """Within one batch too: keep the first alert per company and direction per window."""
    if df.empty:
        return df
    keep, last = [], {}
    for r in df.sort_values("detected_at").itertuples():
        key = (r.company_master_id, r.trigger_type)
        t = pd.Timestamp(r.detected_at)
        if key in last and t - last[key] < pd.Timedelta(days=STORY_READ_DEDUP_DAYS):
            continue
        last[key] = t
        keep.append(r.Index)
    return df.loc[keep]


def record_new() -> int:
    frames = [f for f in (new_alerts().assign(family="rule_triggers"), new_story_reads().assign(family="story_read"))
              if not f.empty]
    if not frames:
        return 0
    df = pd.concat(frames, ignore_index=True)
    df["direction"] = df["trigger_type"].map({**DIRECTION, **STORY_READ_DIRECTION})
    df["symbol"] = df["company_master_id"].str.replace("nse:", "", regex=False)
    df["entry_date"] = None
    df["entry_price"] = None
    # Market state when the alert is recorded (TODO C5) -- descriptive attributes the record
    # can later be sliced on (RESEARCH_PROTOCOL: slicing on named attributes), never a filter.
    from fundamentals.screens.technicals import load_market_state

    state = load_market_state(df["symbol"].dropna().unique().tolist())
    for field in ("delivery_change_pp", "upper_band_hits_20d", "lower_band_hits_20d"):
        df[field] = df["symbol"].map(lambda sym: state.get(sym, {}).get(field))
    upsert_to_db(df, RECORD_TABLE, unique_keys=["source", "news_id", "trigger_type"])
    return len(df)


def fill_entries() -> int:
    """Entry = close of the first session after the IST detection date, once it exists."""
    pending = sql_to_df(f"SELECT source, news_id, trigger_type, symbol, detected_at FROM {RECORD_TABLE} WHERE entry_price IS NULL")
    filled = 0
    for r in pending.itertuples():
        day = pd.Timestamp(r.detected_at).tz_convert("Asia/Kolkata").date()
        px = sql_to_df(
            "SELECT date::date AS d, adj_close FROM advisory_adjusted_ohlcv_daily "
            "WHERE symbol = %s AND date::date > %s::date "
            "  AND date <= %s::date + interval '15 days' ORDER BY date LIMIT 1",
            params=(r.symbol, str(day), str(day)),
        )
        if px.empty:
            continue
        with db_session() as (_, cur):
            cur.execute(f"UPDATE {RECORD_TABLE} SET entry_date = %s, entry_price = %s "
                        "WHERE source = %s AND news_id = %s AND trigger_type = %s",
                        (px.iloc[0]["d"], float(px.iloc[0]["adj_close"]), r.source, r.news_id, r.trigger_type))
        filled += 1
    return filled


def _forward_return(symbol: str, entry_date, sessions: int) -> float | None:
    df = sql_to_df(
        "SELECT adj_close FROM advisory_adjusted_ohlcv_daily WHERE symbol = %s AND date >= %s "
        "AND date <= %s::date + interval '120 days' ORDER BY date LIMIT %s",
        params=(symbol, str(entry_date), str(entry_date), sessions + 1),
    )
    if len(df) < sessions + 1:
        return None  # horizon not reached yet
    return float(df.iloc[-1, 0]) / float(df.iloc[0, 0]) - 1


def _universe_return(entry_date, sessions: int, cache: dict) -> float | None:
    key = (str(entry_date), sessions)
    if key not in cache:
        df = sql_to_df(
            """
            WITH u AS (SELECT DISTINCT replace(ticker, 'nse:', '') AS symbol FROM fundamentals_l1_universe
                        WHERE run_date = (SELECT max(run_date) FROM fundamentals_l1_universe WHERE run_date::date <= %s)),
                 p AS (SELECT o.symbol, o.date, o.adj_close,
                              row_number() OVER (PARTITION BY o.symbol ORDER BY o.date) AS k
                         FROM advisory_adjusted_ohlcv_daily o JOIN u ON u.symbol = o.symbol
                        WHERE o.date >= %s AND o.date <= %s::date + interval '120 days')
            SELECT avg(b.adj_close / a.adj_close - 1) AS r, count(*) AS n
              FROM p a JOIN p b ON a.symbol = b.symbol AND a.k = 1 AND b.k = %s
            """,
            params=(str(entry_date), str(entry_date), str(entry_date), sessions + 1),
        )
        cache[key] = None if df.empty or pd.isna(df.iloc[0]["r"]) else float(df.iloc[0]["r"])
    return cache[key]


def report() -> dict[str, object]:
    rows = sql_to_df(f"SELECT * FROM {RECORD_TABLE} WHERE entry_price IS NOT NULL")
    cache: dict = {}
    out = {}
    for trigger, g in rows.groupby("trigger_type") if not rows.empty else []:
        entry = {"events": int(len(g)), "direction": int({**DIRECTION, **STORY_READ_DIRECTION}.get(trigger, 0)),
                 "family": "story_read" if trigger in STORY_READ_DIRECTION else "rule_triggers"}
        for h in HORIZONS:
            excess = []
            for r in g.itertuples():
                own = _forward_return(r.symbol, r.entry_date, h)
                bench = _universe_return(r.entry_date, h, cache) if own is not None else None
                if own is not None and bench is not None:
                    excess.append(own - bench)
            entry[f"{h}d"] = {"n": len(excess),
                              "mean_excess_pct": round(100 * sum(excess) / len(excess), 2) if excess else None}
        out[trigger] = entry
    return {"record_start": RECORD_START, "story_read_start": STORY_READ_START, "by_trigger": out}


def main(argv: list[str] | None = None) -> int:
    global STOCKEY_RUN_STATE
    parser = argparse.ArgumentParser(description="Event-drift forward record (TODO C7).")
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args(argv)
    ensure_table()
    if args.report:
        print(json.dumps(report(), default=str, indent=2), flush=True)
        return 0
    recorded, filled = record_new(), fill_entries()
    STOCKEY_RUN_STATE = {"source": SYNC_SOURCE_NAME, "rows": recorded, "rows_written": recorded + filled,
                         "recorded": recorded, "entries_filled": filled, "fallback_used": False,
                         "state_advanced": bool(recorded or filled)}
    print(json.dumps({"status": "ok", **STOCKEY_RUN_STATE}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
