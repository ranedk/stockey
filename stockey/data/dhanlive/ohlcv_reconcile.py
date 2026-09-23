"""Reconcile daily OHLCV coverage for the pure-TA equity universe.

The scheduled Dhan OHLCV module only syncs the small tracked-symbols list, and the
broad equity universe is kept fresh by the intraday watchers -- so whenever the cron
scheduler is down during market hours, universe daily bars silently decay (observed:
106 -> 56 -> 1 tickers current) and the freshness gates then correctly suppress all
buy authority. This module closes that gap: it finds every universe symbol whose
latest daily bar is older than the last *completed* trading day and syncs just those
symbols through the existing incremental `sync_daily_ohlcv` path.

Run automatically as the first step of `start_cron.sh` and on the pre-advisory
schedule; run manually via `./all_ohlcv_reconcile.sh` or
`python -m data.dhanlive.ohlcv_reconcile --dry-run`.

The universe query itself was promoted to `utils/universe.py`'s `get_equity_universe()`
2026-08-14 -- this used to be a local `load_universe_symbols()` unioning
`advisory_screener_constituents`/`advisory_watchlist`/`advisory_operator_holdings`, all
of which lost their writers in the 2026-07-27 pure-TA cut and had silently frozen at a
stale 2026-07-21 snapshot ever since. Promoted (not just fixed in place) so any other
collector that needs "the current tradeable NSE universe" has one shared, always-live
place to get it instead of growing its own local notion of the universe again.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import pandas as pd
from environs import Env

from utils.advisory_date import _market_calendar_date, latest_trading_day_on_or_before
from data.dhanlive.ohlcv import DAILY_TABLE, sync_many_daily
from utils.db import sql_to_df
from utils.universe import get_equity_universe

env = Env()

# Raised 400 -> 1500 on 2026-09-02. The cap is a runtime bound, not a correctness knob,
# and 400 was too small to be one: with ~2,880 universe symbols a single run could close
# at most ~377, so recovering a fully-stale universe took EIGHT nightly runs. That only
# stayed invisible because the 18:45 IST schedule meant the job was reading Dhan before
# it had published anything (every logged run reported current=0), so the backlog was
# being hand-repaired rather than drained by cron.
#
# With the corrected 23:05 IST slot a healthy night has almost nothing to reconcile, so
# this ceiling only binds during recovery -- and two runs a day (23:05 + the 07:40
# morning catch-up) now drain a completely stale universe within a single day instead of
# a working week. At roughly 1.3s per symbol a full 1500 is about 30 minutes, which fits
# the overnight window comfortably.
# Raised 1500 -> 3000 on 2026-09-23. 1500 was sized for "a healthy night reconciles almost
# nothing", but Dhan's daily endpoint fails most requests in the 07:40 IST window (measured
# 2026-09-21: 289/1500 at 07:40 vs 177/200 at 12:00 and ~1410/1500 at 23:15), so the backlog
# outgrew the cap and never drained: coverage sat at 664-910 of ~2,900 symbols. A full pass
# at ~1.3s per symbol is about an hour, which both slots have.
DEFAULT_MAX_SYMBOLS = env.int("OHLCV_RECONCILE_MAX_SYMBOLS", default=3000)
# After this hour (IST) on a trading day, today's EOD bars are expected to exist, so the
# pre-advisory reconcile (18:45) pulls TODAY's bars instead of stopping at yesterday.
TODAY_COMPLETE_AFTER_HOUR_IST = env.int("OHLCV_RECONCILE_TODAY_COMPLETE_AFTER_HOUR_IST", default=18)
MARKET_TIMEZONE = "Asia/Kolkata"


def _ist_now(now: Any | None = None) -> pd.Timestamp:
    if now is None:
        return pd.Timestamp.now(tz=MARKET_TIMEZONE)
    ts = pd.Timestamp(now)
    return ts.tz_convert(MARKET_TIMEZONE) if ts.tzinfo is not None else ts.tz_localize(MARKET_TIMEZONE)


def expected_complete_trading_day(now: Any | None = None) -> pd.Timestamp:
    """The most recent trading day whose EOD bars should already exist.

    Intraday on a trading day, today's bars cannot exist yet, so the expectation is the
    previous trading day. After the post-close publish window (TODAY_COMPLETE_AFTER_HOUR_IST)
    today's bars are expected -- this is what lets the 18:45 pre-advisory reconcile fetch
    today's bars before the 19:10 advisory evaluates today's date.
    """
    latest = latest_trading_day_on_or_before(now)
    market_today = _market_calendar_date(now)
    if latest.date() >= market_today.date():
        if _ist_now(now).hour >= TODAY_COMPLETE_AFTER_HOUR_IST:
            return latest
        previous = market_today - pd.Timedelta(days=1)
        return latest_trading_day_on_or_before(previous)
    return latest


def find_stale_symbols(universe: list[str], expected_date: pd.Timestamp) -> list[str]:
    """Symbols whose latest daily bar predates the expected date (or that have no bars)."""
    if not universe:
        return []
    try:
        frame = sql_to_df(
            f"SELECT UPPER(TRIM(ticker)) AS symbol, MAX(date)::date AS max_date "
            f"FROM {DAILY_TABLE} WHERE UPPER(TRIM(ticker)) = ANY(%s) GROUP BY 1",
            params=(list(universe),),
        )
    except Exception as exc:
        print(f"[ohlcv_reconcile] coverage lookup failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return list(universe)
    expected = pd.Timestamp(expected_date).date()
    current: set[str] = set()
    if not frame.empty:
        max_dates = pd.to_datetime(frame["max_date"], errors="coerce")
        for symbol, max_date in zip(frame["symbol"].tolist(), max_dates):
            if not pd.isna(max_date) and pd.Timestamp(max_date).date() >= expected:
                current.add(str(symbol))
    return [symbol for symbol in universe if symbol not in current]


def run_reconcile(*, max_symbols: int | None = None, dry_run: bool = False, now: Any | None = None) -> dict[str, Any]:
    expected = expected_complete_trading_day(now)
    universe = get_equity_universe()
    stale = find_stale_symbols(universe, expected)
    cap = int(DEFAULT_MAX_SYMBOLS if max_symbols is None else max_symbols)
    skipped = 0
    to_sync = stale
    # BUG FOUND LIVE 2026-08-19 (re-audit): `cap > 0` meant an explicit `--max-symbols 0` disabled
    # the cap entirely (synced everything) instead of syncing zero -- the opposite of what anyone
    # passing 0 would reasonably expect. `cap >= 0` now treats 0 as a literal "sync nothing" cap;
    # a negative value (never the default, only reachable via an explicit --max-symbols) is still
    # treated as "uncapped", preserving that escape hatch for anyone already relying on it.
    if cap >= 0 and len(stale) > cap:
        to_sync = stale[:cap]
        skipped = len(stale) - cap
        print(f"[ohlcv_reconcile] symbol cap applied max={cap} stale={len(stale)} skipped={skipped}", file=sys.stderr)
    summary: dict[str, Any] = {
        "expected_trading_day": expected.date().isoformat(),
        "universe_symbols": len(universe),
        "current_symbols": len(universe) - len(stale),
        "stale_symbols": len(stale),
        "sync_attempted": 0,
        "sync_succeeded": 0,
        "sync_failed": 0,
        "skipped_over_cap": skipped,
        "dry_run": bool(dry_run),
        "broker_execution_allowed": False,
    }
    if dry_run or not to_sync:
        summary["stale_sample"] = to_sync[:20]
        return summary
    results = sync_many_daily(to_sync)
    # Dhan's daily endpoint fails transiently in bulk -- the same symbol that returns
    # DH-905 in one pass returns its bars minutes later (measured 2026-09-21). One retry
    # of just the failures, at the end of the run, costs a fraction of the run and
    # recovers most of them. Only one: a symbol that fails twice is a real failure.
    failed_rows = [row for row in results if row.get("error")]
    retried = [str(row.get("ticker")) for row in failed_rows if row.get("ticker")]
    recovered = 0
    if retried:
        print(f"[ohlcv_reconcile] retrying {len(retried)} failed symbols once", file=sys.stderr)
        retry_results = sync_many_daily(retried)
        by_ticker = {str(row.get("ticker")): row for row in retry_results}
        merged = []
        for row in results:
            ticker = str(row.get("ticker"))
            replacement = by_ticker.get(ticker)
            if row.get("error") and replacement is not None and not replacement.get("error"):
                recovered += 1
                merged.append(replacement)
            else:
                merged.append(replacement if row.get("error") and replacement is not None else row)
        results = merged
    succeeded = sum(1 for row in results if not row.get("error"))
    failure_classes = sorted(
        {str(row.get("classification") or "failed") for row in results if row.get("error")}
    )
    summary["sync_attempted"] = len(results)
    summary["sync_succeeded"] = succeeded
    summary["sync_failed"] = len(results) - succeeded
    summary["sync_retried"] = len(retried)
    summary["sync_recovered_on_retry"] = recovered
    summary["failure_classifications"] = failure_classes
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reconcile daily OHLCV coverage for the pure-TA equity universe.")
    parser.add_argument("--dry-run", action="store_true", help="Report stale coverage without syncing.")
    parser.add_argument("--max-symbols", type=int, default=None, help=f"Cap synced symbols (default {DEFAULT_MAX_SYMBOLS}).")
    parser.add_argument("--format", choices=["json", "text"], default="text")
    args = parser.parse_args(argv)

    summary = run_reconcile(max_symbols=args.max_symbols, dry_run=bool(args.dry_run))
    if args.format == "json":
        print(json.dumps(summary, indent=2, default=str))
    else:
        print(
            f"[ohlcv_reconcile] expected_day={summary['expected_trading_day']} "
            f"universe={summary['universe_symbols']} current={summary['current_symbols']} "
            f"stale={summary['stale_symbols']} attempted={summary['sync_attempted']} "
            f"succeeded={summary['sync_succeeded']} failed={summary['sync_failed']} "
            f"skipped_over_cap={summary['skipped_over_cap']} dry_run={summary['dry_run']}"
        )
    # Systemic failure exits non-zero so cron logs/script markers surface it: either the
    # Dhan token is dead (auth_unavailable) or a broad sweep produced zero successes.
    # A handful of persistently-bad symbols failing (per-symbol 400s) stays exit 0 --
    # they are visible via fallback telemetry and the coverage health check instead.
    if summary["sync_attempted"] > 0 and summary["sync_succeeded"] == 0:
        classes = set(summary.get("failure_classifications") or [])
        if "auth_unavailable" in classes or summary["sync_attempted"] >= 10:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
