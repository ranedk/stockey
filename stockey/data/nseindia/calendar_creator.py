"""Producer for `dim_trading_days` -- the NSE trading calendar.

RESTORED 2026-09-02. This module was `features/calendar_creator.py` and was deleted in
`fb84396` ("Phase 4 cut -- delete advisory/ and REMOVE-scope dirs"). That was collateral
damage, not a decision: `dim_trading_days` is a KEEP table (docs/DATA_INVENTORY.md, NSE
calendar), but its only writer happened to live inside a REMOVE-scope directory and went
out with it. The table has had no producer since, and nothing noticed for five weeks
because it was already populated through 2026-12-31 -- a dated fuse rather than a
visible break.

Why it matters more than its size suggests: this table is what every trading-day-aware
check in the repo resolves against. When it runs out,
`scripts/data_completeness.py::_last_complete_trading_day` returns None and the nightly
gate quietly stops comparing freshness at all; `ohlcv_reconcile`'s expected-day
calculation goes wrong; `data/dhanlive/ohlcv.py::clamp_to_last_trading_day` degrades to
a bare weekday+holiday heuristic -- which is precisely the bug 277774a fixed for real
weekend NSE sessions. The monitoring layer loses its footing before any feed does.

How the calendar is built (unchanged from the original -- the live table's schema and
contents were verified to match this output exactly before restoring):
  1. Historical trading days come from `nseindia_ohlcv` -- days the market provably
     traded, which beats deriving the past from a holiday list.
  2. Current and future years are business days (Mon-Fri) minus `nseindia_holidays`.
     This is the only part that extends the runway, so the table can never reach
     further than NSE's published holiday calendar (today: through 2026).
  3. Diwali Muhurat sessions are added BACK. NSE lists them as holidays and still runs a
     one-hour evening session, and at least one falls on a weekend -- 2026-11-08 is a
     Sunday. Dropping them would make the calendar disagree with reality on exactly the
     dates hardest to reason about.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from utils.db import sql_to_df, upsert_to_db

SYNC_SOURCE_NAME = "data.nseindia.calendar_creator"
STOCKEY_RUN_STATE: dict[str, object] = {}

TABLE_NAME = "dim_trading_days"
MIN_DATE = "2014-01-01"


def _fy_year(d: pd.Timestamp) -> int:
    """Indian fiscal year: April-March, labelled by the starting calendar year."""
    return d.year if d.month >= 4 else d.year - 1


def _fy_quarter(d: pd.Timestamp) -> int:
    return ((d.month - 4) % 12) // 3 + 1  # Q1=Apr-Jun ... Q4=Jan-Mar


def compute_trading_days() -> pd.DataFrame:
    """Historical sessions from OHLCV, unioned with holiday-derived future business days."""
    hist = sql_to_df(
        "SELECT DISTINCT date FROM nseindia_ohlcv WHERE date::date >= %s",
        params=(MIN_DATE,),
    )

    # Equity cash-market holidays only: the feed lists every segment, and a currency- or
    # debt-only holiday (2026-08-26 Id-E-Milad: CD/IRD, not CM) is a normal equity session.
    hol = sql_to_df("SELECT date FROM nseindia_holidays WHERE type = 'CM'")

    cal = []
    if not hol.empty:
        # tz-NAIVE dates: the column is timestamptz, so pandas reads it tz-aware (UTC), and a
        # tz-aware index never equals the tz-naive business days -- `difference` removed
        # nothing, and every 2026 weekday holiday (Republic Day ... Gandhi Jayanti, Diwali,
        # Christmas) sat in dim_trading_days as a trading day. Found 2026-10-03 when the
        # completeness gate failed on Gandhi Jayanti "missing" bars.
        hol["date"] = pd.to_datetime(hol["date"], utc=True).dt.tz_convert(None)
        current_year = pd.Timestamp.today().year
        for y in sorted(hol["date"].dt.year.unique()):
            if y < current_year:  # the past comes from OHLCV, which is authoritative
                continue
            bdays = pd.date_range(f"{y}-01-01", f"{y}-12-31", freq="B", normalize=True)
            hol_y = hol.loc[hol["date"].dt.year == y, "date"].dt.normalize()
            d = pd.Index(bdays).difference(pd.Index(hol_y))
            if len(d):
                cal.append(pd.DataFrame({"date": d}))

    cal = pd.concat(cal, ignore_index=True) if cal else pd.DataFrame(columns=["date"])
    cal["date"] = pd.to_datetime(cal["date"], utc=True)

    # Muhurat: listed as a holiday, still a real (one-hour) session. 2026-11-08 is a
    # Sunday, so this is also what keeps weekend sessions in the calendar.
    ov = sql_to_df(
        "SELECT date FROM public.nseindia_holidays "
        # Laxmi Pujan only: the Muhurat session is that evening. "%diwali%" also matched
        # Diwali-Balipratipada (2026-11-10), a full holiday, and put it back as a session.
        "WHERE type = 'CM' AND holiday ILIKE '%%laxmi%%pujan%%'",
    )
    if not ov.empty:
        cal = pd.concat([cal, ov], ignore_index=True)

    all_days = pd.concat([hist, cal]).drop_duplicates(subset=["date"])
    if all_days.empty:
        return all_days
    s = pd.to_datetime(all_days["date"]).sort_values().reset_index(drop=True)

    next_dt = s.shift(-1)
    prev_dt = s.shift(1)

    df = pd.DataFrame({"date": s.dt.date})
    df["date"] = pd.to_datetime(df["date"])
    df["is_next_day_working"] = next_dt - s == pd.Timedelta(days=1)
    df["is_previous_day_working"] = s - prev_dt == pd.Timedelta(days=1)
    df["is_month_end"] = next_dt.isna() | (next_dt.dt.month != s.dt.month)

    q = s.map(_fy_quarter)
    q_next = next_dt.map(lambda x: np.nan if pd.isna(x) else _fy_quarter(x))
    df["is_quarter_end"] = next_dt.isna() | (q != q_next)

    fy = s.map(_fy_year)
    fy_next = next_dt.map(lambda x: np.nan if pd.isna(x) else _fy_year(x))
    df["is_year_end"] = next_dt.isna() | (fy != fy_next)

    df["week_number"] = s.dt.isocalendar().week.astype(int)
    return df



def _prune_stale_days(df: pd.DataFrame, min_date, max_date, *, dry_run: bool) -> int:
    """Delete calendar rows this rebuild no longer considers trading days.

    An upsert alone cannot fix a WRONG row, only a missing or changed one, and the live
    table had 13 of them when this producer was restored: 2025 NSE holidays recorded as
    trading days (2025-08-15 Independence Day, 2025-12-25 Christmas, ...), almost
    certainly written during a window when nseindia_holidays had not yet been populated
    for that year, so the business-day filter had nothing to subtract.

    SAFETY: a date is never pruned if nseindia_ohlcv shows the market actually traded on
    it. The holiday feed is an input we do not control and this table is load-bearing for
    every trading-day-aware check in the repo, so a bug in the holiday logic must not be
    able to delete a real session. Traded days are evidence; the holiday list is only a
    prediction. That guard is what makes this safe to run unattended -- without it, a day
    when NSE published a bad holiday file could silently erase real trading days.
    """
    from utils.db import db_session

    keep = pd.to_datetime(df["date"]).dt.date.unique().tolist()
    with db_session() as (_conn, cur):
        cur.execute(
            """
            SELECT date::date FROM dim_trading_days
             WHERE date::date BETWEEN %s AND %s
               AND date::date <> ALL(%s)
               AND NOT EXISTS (
                     SELECT 1 FROM nseindia_ohlcv o
                      WHERE o.date::date = dim_trading_days.date::date
                   )
            """,
            (min_date.date(), max_date.date(), keep),
        )
        stale = [r[0] for r in cur.fetchall()]
        if stale and not dry_run:
            cur.execute(
                "DELETE FROM dim_trading_days WHERE date::date = ANY(%s)", (stale,)
            )
    if stale:
        print(
            f"[calendar_creator] {'would prune' if dry_run else 'pruned'} "
            f"{len(stale)} non-trading day(s) with no OHLCV evidence: "
            f"{', '.join(str(d) for d in stale[:8])}"
            f"{' ...' if len(stale) > 8 else ''}",
            flush=True,
        )
    return len(stale)


def run_calendar_build(*, dry_run: bool = False) -> dict[str, object]:
    df = compute_trading_days()
    if df.empty:
        return {
            "source": SYNC_SOURCE_NAME,
            "rows": 0,
            "rows_written": 0,
            "status": "no_data",
            "state_advanced": False,
        }

    max_date = pd.Timestamp(df["date"].max())
    min_date = pd.Timestamp(df["date"].min())
    runway_days = int((max_date - pd.Timestamp.utcnow().tz_localize(None)).days)
    if not dry_run:
        # update_if_changed, not a blanket DO UPDATE: this rebuilds the whole calendar
        # every run and the overwhelming majority of rows never move, so a plain upsert
        # would rewrite ~3,200 rows nightly for nothing (same reasoning as
        # nseindia/price_adjustment.py).
        upsert_to_db(
            df,
            TABLE_NAME,
            unique_keys=["date"],
            timescaledb_column="date",
            on_conflict="update_if_changed",
        )
    pruned = _prune_stale_days(df, min_date, max_date, dry_run=dry_run)

    return {
        "source": SYNC_SOURCE_NAME,
        "rows": int(len(df)),
        "rows_written": 0 if dry_run else int(len(df)),
        "pruned_rows": pruned,
        "min_date": str(pd.Timestamp(df["date"].min()).date()),
        "max_date": str(max_date.date()),
        "runway_days": runway_days,
        # The calendar can only reach as far as NSE's published holiday list. A short
        # runway is not a failure here -- it means next year's calendar is not out yet --
        # but it IS the thing worth seeing, and scripts/data_completeness.py's
        # trading_days check warns on it independently.
        "status": "ok",
        "state_advanced": not dry_run,
        "dry_run": bool(dry_run),
    }


def main(argv: list[str] | None = None) -> int:
    global STOCKEY_RUN_STATE
    import argparse

    parser = argparse.ArgumentParser(description="Rebuild dim_trading_days from OHLCV history + NSE holidays.")
    parser.add_argument("--dry-run", action="store_true", help="Compute and report without writing.")
    args = parser.parse_args(argv)

    STOCKEY_RUN_STATE = run_calendar_build(dry_run=args.dry_run)
    print(json.dumps(STOCKEY_RUN_STATE, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
