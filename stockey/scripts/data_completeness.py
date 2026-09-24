"""Data-completeness checks: is the data actually THERE, across the whole universe?

Written 2026-08-31, after Dhan collection died on 2026-08-26/28 and nothing noticed
for five days. Two failure modes made that invisible, and this script exists for both:

1. FRESHNESS WITHOUT BREADTH. Every existing check asked "how recent is the newest
   row?". On 2026-08-27/28/31 `dhan_ohlcv_intraday` answered "today" -- because the
   Dhan connectivity smoke test (`download_runner._dhan_precheck_symbols`) was still
   writing ONE ticker a day while real collection for the other ~2,590 was dead. A
   max(timestamp) check cannot see that. Every check here that asks "how recent"
   also asks "how many, versus the recent norm".

2. THE MONITOR SHARING THE COLLECTORS' DEPENDENCIES. `data_readiness.py` is the job
   that should have shouted, and it was itself crashing on the same dead-Chrome CDP
   timeout that killed the collectors. So this script imports NOTHING browser- or
   broker-related: postgres and stdlib only, no Playwright, no Dhan client, no CDP.
   It is read-only -- it never repairs. Repair stays in data_readiness.py --fix.

SAFETY: `dhan_ohlcv_intraday` is ~525M rows across 263 COMPRESSED chunks. An
unbounded aggregate over it makes TimescaleDB decompress every chunk at once and
OOM'd this 30 GB box in ~15 seconds on 2026-08-31, taking postgres with it. Every
query below is time-bounded or reads catalog metadata. If you add one, bound it.

    python -m scripts.data_completeness            # human-readable report
    python -m scripts.data_completeness --json     # machine-readable
    python -m scripts.data_completeness --window-days 45

Exit codes:  0 = no hard errors (warnings may be present)
             1 = at least one ERROR finding
             2 = the checks themselves could not run (no DB, bad args)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import pandas as pd

from utils.db import sql_to_df

# NSE trades 09:15-15:30 IST; the last 1-min bar of a complete session is 15:29.
# A session whose MEDIAN last bar lands before this was cut short.
SESSION_CLOSE_IST = os.getenv("COMPLETENESS_SESSION_CLOSE_IST", "15:20")
# Fraction of the recent median breadth below which a feed counts as collapsed.
BREADTH_ERROR_RATIO = float(os.getenv("COMPLETENESS_BREADTH_ERROR_RATIO", "0.50"))
BREADTH_WARN_RATIO = float(os.getenv("COMPLETENESS_BREADTH_WARN_RATIO", "0.90"))
BHAVCOPY_MIN_ROWS = int(os.getenv("DATA_READINESS_BHAVCOPY_MIN_ROWS", "1500"))
TRADING_DAYS_MIN_RUNWAY = int(os.getenv("COMPLETENESS_TRADING_DAYS_RUNWAY", "90"))
# NSE closes 15:30 IST; the bhavcopy lands ~18:00-19:00 IST. Before this hour, "today"
# is not yet an expectable trading day and comparing against it is a false positive.
EOD_PUBLISH_CUTOFF_IST_HOUR = int(os.getenv("COMPLETENESS_EOD_CUTOFF_IST_HOUR", "19"))

OK, WARN, ERROR = "ok", "warn", "error"


class Findings:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(self, level: str, check: str, message: str, **metrics: Any) -> None:
        self.items.append(
            {"level": level, "check": check, "message": message, "metrics": metrics}
        )

    @property
    def errors(self) -> list[dict[str, Any]]:
        return [f for f in self.items if f["level"] == ERROR]

    @property
    def warnings(self) -> list[dict[str, Any]]:
        return [f for f in self.items if f["level"] == WARN]


def _scalar(query: str, params: tuple = ()) -> Any:
    df = sql_to_df(query, params=params)
    if df.empty:
        return None
    return df.iloc[0, 0]


def _naive_utc(val: Any) -> pd.Timestamp | None:
    """Normalize to naive UTC. Date columns in this schema are a mix of DATE and
    TIMESTAMPTZ, and subtracting one kind from the other raises."""
    if val is None:
        return None
    ts = pd.Timestamp(val)
    return ts.tz_convert("UTC").tz_localize(None) if ts.tzinfo is not None else ts


def _last_complete_trading_day() -> pd.Timestamp | None:
    """The most recent trading day whose data should ALREADY be published.

    Not simply max(date) <= now(): during a live session that returns today, and every
    feed then looks "3 days behind" purely because the market has not closed yet. NSE
    closes 15:30 IST and the bhavcopy lands ~18:00-19:00 IST, so today only counts once
    it is past the cutoff. Read from dim_trading_days rather than a collector module,
    so a broken collector cannot take this check down with it."""
    now_ist = pd.Timestamp.utcnow().replace(tzinfo=None) + pd.Timedelta(hours=5, minutes=30)
    candidate = now_ist.normalize()
    if now_ist.hour < EOD_PUBLISH_CUTOFF_IST_HOUR:
        candidate -= pd.Timedelta(days=1)
    return _naive_utc(
        _scalar("SELECT MAX(date) FROM dim_trading_days WHERE date <= %s", (candidate.date(),))
    )


def _breadth_verdict(latest: int, median: float) -> tuple[str, str]:
    """Compare the latest session's breadth against the recent norm."""
    if median <= 0:
        return WARN, "no recent baseline to compare against"
    ratio = latest / median
    if ratio < BREADTH_ERROR_RATIO:
        return ERROR, f"{latest} vs a recent median of {median:.0f} ({ratio:.0%} of normal) -- feed has COLLAPSED"
    if ratio < BREADTH_WARN_RATIO:
        return WARN, f"{latest} vs a recent median of {median:.0f} ({ratio:.0%} of normal)"
    return OK, f"{latest} vs a recent median of {median:.0f} ({ratio:.0%} of normal)"


# --------------------------------------------------------------------- checks


def check_bhavcopy(f: Findings, last_td: pd.Timestamp | None) -> None:
    latest = _scalar("SELECT MAX(date) FROM nseindia_ohlcv WHERE series = 'EQ'")
    if latest is None:
        f.add(ERROR, "bhavcopy", "nseindia_ohlcv has no EQ rows at all")
        return
    latest = _naive_utc(latest)
    rows = int(
        _scalar(
            "SELECT count(*) FROM nseindia_ohlcv WHERE series = 'EQ' AND date = %s",
            (latest.date(),),
        )
        or 0
    )
    stale_days = (last_td - latest).days if last_td is not None else 0
    if stale_days > 0:
        f.add(
            ERROR,
            "bhavcopy",
            f"latest EQ bar is {latest.date()}, but the last trading day is {last_td.date()} "
            f"({stale_days} day(s) behind)",
            latest=str(latest.date()), last_trading_day=str(last_td.date()), rows=rows,
        )
    elif rows < BHAVCOPY_MIN_ROWS:
        f.add(ERROR, "bhavcopy",
              f"{latest.date()} has only {rows} EQ rows (floor is {BHAVCOPY_MIN_ROWS})",
              latest=str(latest.date()), rows=rows)
    else:
        f.add(OK, "bhavcopy", f"{latest.date()} has {rows} EQ rows", rows=rows)


def check_adjustment_join(f: Findings) -> None:
    """advisory_adjusted_ohlcv_daily -- systrader's PRIMARY equity series -- is an INNER
    JOIN of nseindia_ohlcv against nseindia_adjustment_factors. An EQ/BE bar with no
    factor row does not error; it silently VANISHES from the series. On 2026-08-31 that
    was three whole trading days wide, with every cron job still exiting 0."""
    row = sql_to_df(
        """
        SELECT (SELECT count(*) FROM nseindia_ohlcv WHERE series IN ('EQ','BE')) AS price_bars,
               (SELECT count(*) FROM advisory_adjusted_ohlcv_daily)              AS view_rows,
               (SELECT max(date) FROM nseindia_ohlcv WHERE series IN ('EQ','BE')) AS ohlcv_max,
               (SELECT max(date) FROM nseindia_adjustment_factors)                AS factors_max
        """
    )
    if row.empty:
        f.add(ERROR, "adjustment_join", "could not read the adjustment join")
        return
    r = row.iloc[0]
    orphans = int(r["price_bars"]) - int(r["view_rows"])
    ohlcv_max, factors_max = _naive_utc(r["ohlcv_max"]), _naive_utc(r["factors_max"])
    if orphans > 0:
        lag = (ohlcv_max - factors_max).days
        f.add(
            ERROR,
            "adjustment_join",
            f"{orphans} EQ/BE price bars have no adjustment-factor row and are silently "
            f"missing from advisory_adjusted_ohlcv_daily "
            f"(factors reach {factors_max.date()}, prices reach {ohlcv_max.date()}, {lag} day(s) behind). "
            f"Fix: ./all_price_adjustment.sh",
            orphan_bars=orphans, factors_max=str(factors_max.date()), ohlcv_max=str(ohlcv_max.date()),
        )
    else:
        f.add(OK, "adjustment_join",
              f"all {int(r['price_bars'])} EQ/BE bars have a factor row (through {factors_max.date()})",
              price_bars=int(r["price_bars"]))


def check_dhan_daily(f: Findings, window_days: int, last_td: pd.Timestamp | None) -> None:
    """Breadth AND freshness -- see this module's docstring for why breadth alone is
    not optional. Dhan daily decayed 1350 -> 375 -> 373 -> 3 tickers/day over
    2026-08-26..28 while every freshness-only check stayed green."""
    df = sql_to_df(
        """
        SELECT date::date AS session, count(DISTINCT ticker) AS tickers
          FROM dhan_ohlcv_daily
         WHERE date >= now() - make_interval(days => %s)
         GROUP BY 1 ORDER BY 1
        """,
        params=(int(window_days),),
    )
    if df.empty:
        f.add(ERROR, "dhan_daily", f"no dhan_ohlcv_daily rows at all in the last {window_days} days")
        return

    # Same guard as check_intraday: a session still in progress is legitimately partial.
    if last_td is not None:
        complete = df[pd.to_datetime(df["session"]) <= last_td]
        df = complete if not complete.empty else df

    latest_session = _naive_utc(df.iloc[-1]["session"])
    latest_tickers = int(df.iloc[-1]["tickers"])
    baseline = df.iloc[:-1]["tickers"].median() if len(df) > 1 else 0
    level, detail = _breadth_verdict(latest_tickers, float(baseline))
    f.add(level, "dhan_daily", f"latest session {latest_session.date()}: {detail}",
          session=str(latest_session.date()), tickers=latest_tickers, median=float(baseline))

    if last_td is not None and latest_session.date() < last_td.date():
        f.add(ERROR, "dhan_daily_freshness",
              f"latest Dhan daily bar is {latest_session.date()}, last trading day is {last_td.date()}",
              latest=str(latest_session.date()), last_trading_day=str(last_td.date()))


def check_intraday(f: Findings, window_days: int, last_td: pd.Timestamp | None) -> None:
    """Bounded by construction: the window predicate keeps this to a handful of chunks."""
    df = sql_to_df(
        """
        SELECT timestamp::date AS session,
               count(DISTINCT ticker) AS tickers,
               count(*) AS bars
          FROM dhan_ohlcv_intraday
         WHERE timestamp >= now() - make_interval(days => %s)
         GROUP BY 1 ORDER BY 1
        """,
        params=(int(window_days),),
    )
    if df.empty:
        f.add(ERROR, "intraday", f"no dhan_ohlcv_intraday rows at all in the last {window_days} days")
        return

    # Drop a still-running session before judging breadth: mid-session the row count is
    # legitimately partial, and comparing it to a median of complete days is noise.
    if last_td is not None:
        complete = df[pd.to_datetime(df["session"]) <= last_td]
        df = complete if not complete.empty else df

    latest_session = _naive_utc(df.iloc[-1]["session"])
    latest_tickers = int(df.iloc[-1]["tickers"])
    baseline = df.iloc[:-1]["tickers"].median() if len(df) > 1 else 0
    level, detail = _breadth_verdict(latest_tickers, float(baseline))
    f.add(level, "intraday_breadth", f"latest session {latest_session.date()}: {detail}",
          session=str(latest_session.date()), tickers=latest_tickers, median=float(baseline))

    if last_td is not None and latest_session.date() < last_td.date():
        f.add(ERROR, "intraday_freshness",
              f"latest intraday bar is {latest_session.date()}, last trading day is {last_td.date()}",
              latest=str(latest_session.date()), last_trading_day=str(last_td.date()))

    # Truncation is an early session END, measured per SESSION -- deliberately NOT
    # "ticker-days with fewer than 375 bars". A 1-min bar only exists where a trade
    # happened, so 20-33% of this universe legitimately has far fewer than a full
    # session's 375 bars on any normal day (observed minimum: 2). Counting those flags
    # illiquidity, not damage, and buries a real outage in thousands of false
    # positives. The `_ist_now` bug (data/dhanlive/ohlcv.py, fixed 2026-08-30) cut each
    # day's CLOSE off, which shows up as the whole session's median last bar landing
    # early -- robust to thin names, and the actual failure signature.
    sessions = sql_to_df(
        """
        WITH per_ticker AS (
          SELECT timestamp::date AS session, ticker, max(timestamp) AS last_bar
            FROM dhan_ohlcv_intraday
           WHERE timestamp >= now() - make_interval(days => %s)
           GROUP BY 1, 2
        )
        SELECT session,
               count(*) AS tickers,
               (percentile_disc(0.5) WITHIN GROUP (ORDER BY last_bar)
                  AT TIME ZONE 'Asia/Kolkata')::time AS median_last_ist
          FROM per_ticker GROUP BY 1 ORDER BY 1
        """,
        params=(int(window_days),),
    )
    if last_td is not None and not sessions.empty:
        # A session still in progress is legitimately "early"; judge complete ones only.
        sessions = sessions[pd.to_datetime(sessions["session"]) <= last_td]

    cutoff = pd.Timestamp(SESSION_CLOSE_IST).time()
    short = sessions[sessions["median_last_ist"] < cutoff] if not sessions.empty else sessions
    if not short.empty:
        worst = ", ".join(
            f"{r.session} (ended {r.median_last_ist:%H:%M} IST)" for r in short.head(5).itertuples()
        )
        f.add(WARN, "intraday_truncated",
              f"{len(short)} session(s) in the last {window_days} days ended before "
              f"{cutoff:%H:%M} IST -- close truncated (see data/dhanlive/ohlcv.py:_ist_now): {worst}",
              sessions=len(short), window_days=window_days)
    else:
        f.add(OK, "intraday_truncated",
              f"all {len(sessions)} complete session(s) in the last {window_days} days ran to the close")


def check_universe_coverage(f: Findings, window_days: int) -> None:
    """Which active-universe symbols are not being collected at all right now.
    Bounded: the intraday side carries the window predicate."""
    n = _scalar(
        """
        SELECT count(*) FROM (
          -- A renamed NSE symbol (TMPV) is collected under its company's canonical ticker
          -- (TATAMOTORS); without this mapping ~50 fully-collected names read as missing
          -- (2026-09-24).
          SELECT DISTINCT COALESCE(a.canonical_nse_ticker, o.symbol) FROM nseindia_ohlcv o
            LEFT JOIN company_master_nse_alias a ON a.alias_ticker = o.symbol
           WHERE o.series IN ('EQ','BE')
             AND o.date >= (SELECT max(date) FROM nseindia_ohlcv) - interval '7 days'
          EXCEPT
          SELECT DISTINCT ticker FROM dhan_ohlcv_intraday
           WHERE timestamp >= now() - make_interval(days => %s)
        ) x
        """,
        (int(window_days),),
    )
    universe = _scalar(
        """
        SELECT count(DISTINCT symbol) FROM nseindia_ohlcv
         WHERE series IN ('EQ','BE')
           AND date >= (SELECT max(date) FROM nseindia_ohlcv) - interval '7 days'
        """
    )
    n, universe = int(n or 0), int(universe or 0)
    if universe and n / universe > 0.25:
        level = ERROR
    elif n:
        level = WARN
    else:
        level = OK
    f.add(level, "universe_coverage",
          f"{n} of {universe} active symbols have no intraday data in the last {window_days} days",
          missing=n, universe=universe, window_days=window_days)


def check_trading_days_runway(f: Findings) -> None:
    latest = _scalar("SELECT MAX(date) FROM dim_trading_days")
    if latest is None:
        f.add(ERROR, "trading_days", "dim_trading_days is empty -- every trading-day-aware check is blind")
        return
    latest = _naive_utc(latest)
    runway = (latest - pd.Timestamp.utcnow().replace(tzinfo=None)).days
    if runway < TRADING_DAYS_MIN_RUNWAY:
        f.add(WARN, "trading_days",
              f"dim_trading_days runs out on {latest.date()} ({runway} days of runway) "
              f"and has no producer in the codebase",
              latest=str(latest.date()), runway_days=runway)
    else:
        f.add(OK, "trading_days", f"populated through {latest.date()} ({runway} days of runway)",
              latest=str(latest.date()), runway_days=runway)


def run_all_checks(*, window_days: int = 30) -> Findings:
    """Run every check and return the findings.

    Shared by the CLI below and by the fundamentals API's /api/data-health route, so
    the dashboard and the nightly cron gate can never disagree about what "complete"
    means -- there is one implementation, not two.
    """
    f = Findings()
    last_td = _last_complete_trading_day()
    check_bhavcopy(f, last_td)
    check_adjustment_join(f)
    check_dhan_daily(f, window_days, last_td)
    check_intraday(f, window_days, last_td)
    check_universe_coverage(f, window_days)
    check_trading_days_runway(f)
    return f


# ----------------------------------------------------------------------- main

_ICON = {OK: "OK  ", WARN: "WARN", ERROR: "FAIL"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--window-days", type=int, default=30,
                        help="Bounded lookback for the intraday/daily breadth checks (default 30)")
    parser.add_argument("--json", action="store_true", help="Emit findings as JSON")
    args = parser.parse_args(argv)

    if args.window_days < 1:
        print("--window-days must be >= 1", file=sys.stderr)
        return 2

    try:
        f = run_all_checks(window_days=args.window_days)
    except Exception as exc:  # noqa: BLE001 -- a monitor that dies silently is the bug it exists to catch
        print(f"[data_completeness] checks could not run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({
            "findings": f.items,
            "error_count": len(f.errors),
            "warn_count": len(f.warnings),
        }, indent=2, default=str))
    else:
        print(f"data completeness -- {pd.Timestamp.utcnow():%Y-%m-%dT%H:%M:%SZ}")
        for item in f.items:
            print(f"  {_ICON[item['level']]} {item['check']}: {item['message']}")
        print()
        if f.errors:
            print(f"  {len(f.errors)} ERROR(S), {len(f.warnings)} warning(s)")
        else:
            print(f"  no errors, {len(f.warnings)} warning(s)")

    return 1 if f.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
