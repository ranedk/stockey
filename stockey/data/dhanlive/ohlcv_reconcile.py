"""Reconcile daily OHLCV coverage for the advisory symbol universe.

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

env = Env()

DEFAULT_MAX_SYMBOLS = env.int("OHLCV_RECONCILE_MAX_SYMBOLS", default=400)
# Union constituents over a snapshot window instead of only the newest snapshot: a
# partial refresh (one screener updating on a weekend) must not collapse the universe
# and silently exclude symbols from reconciliation.
CONSTITUENTS_LOOKBACK_DAYS = env.int("OHLCV_RECONCILE_CONSTITUENTS_LOOKBACK_DAYS", default=7)
# After this hour (IST) on a trading day, today's EOD bars are expected to exist, so the
# pre-advisory reconcile (18:45) pulls TODAY's bars instead of stopping at yesterday.
TODAY_COMPLETE_AFTER_HOUR_IST = env.int("OHLCV_RECONCILE_TODAY_COMPLETE_AFTER_HOUR_IST", default=18)
MARKET_TIMEZONE = "Asia/Kolkata"
CONSTITUENTS_TABLE = "advisory_screener_constituents"
WATCHLIST_TABLE = "advisory_watchlist"
HOLDINGS_TABLE = "advisory_operator_holdings"


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


def load_universe_symbols() -> list[str]:
    """Advisory symbol universe: latest screener constituents + active watchlist + open holdings."""
    symbols: set[str] = set()
    queries = (
        (
            "constituents",
            f"SELECT DISTINCT ticker AS symbol FROM {CONSTITUENTS_TABLE} "
            f"WHERE date >= (SELECT MAX(date) FROM {CONSTITUENTS_TABLE}) - interval '{int(CONSTITUENTS_LOOKBACK_DAYS)} days'",
        ),
        (
            "watchlist",
            f"SELECT DISTINCT symbol FROM {WATCHLIST_TABLE} "
            f"WHERE asof_date = (SELECT MAX(asof_date) FROM {WATCHLIST_TABLE}) "
            "AND COALESCE(watch_enabled, TRUE) = TRUE",
        ),
        (
            "holdings",
            f"SELECT DISTINCT symbol FROM {HOLDINGS_TABLE} WHERE COALESCE(status, 'open') = 'open'",
        ),
    )
    for source, query in queries:
        try:
            frame = sql_to_df(query)
        except Exception as exc:
            print(f"[ohlcv_reconcile] universe source {source} unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        if frame.empty or "symbol" not in frame.columns:
            continue
        for value in frame["symbol"].tolist():
            text = str(value or "").strip().upper()
            if text:
                symbols.add(text)
    return sorted(symbols)


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
    universe = load_universe_symbols()
    stale = find_stale_symbols(universe, expected)
    cap = int(DEFAULT_MAX_SYMBOLS if max_symbols is None else max_symbols)
    skipped = 0
    to_sync = stale
    if cap > 0 and len(stale) > cap:
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
    succeeded = sum(1 for row in results if not row.get("error"))
    failure_classes = sorted(
        {str(row.get("classification") or "failed") for row in results if row.get("error")}
    )
    summary["sync_attempted"] = len(results)
    summary["sync_succeeded"] = succeeded
    summary["sync_failed"] = len(results) - succeeded
    summary["failure_classifications"] = failure_classes
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reconcile daily OHLCV coverage for the advisory universe.")
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
