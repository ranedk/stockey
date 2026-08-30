"""One-off historical backfill: 5 years of 1-min Dhan intraday OHLCV for the
full active NSE equity universe (utils.universe.get_equity_universe() -- no
static symbol list, per CLAUDE.md's "no static symbol/company registry file"
rule). HF_DATA_PLATFORM_PLAN.md (systrader repo): decided 2026-08-22 to
validate the systrader thesis against Dhan's own 5-year intraday history
directly, rather than wait on a vendor data delivery -- Dhan's docs confirm
5 years of 1/5/15/25/60-min data is genuinely available (90 days max per
request, 5 req/sec, 100,000/day -- see data/dhanlive/ohlcv.py's
INTRADAY_MAX_WINDOW_DAYS, already tuned to the same 90-day limit).

BATCHED UPSERT (2026-08-22, smoke-test finding): dhan_ohlcv_intraday is a
TimescaleDB hypertable chunked every 7 days. sync_intraday_ohlcv's own
per-symbol upsert_to_db call, run once per symbol across a full 5-year span,
touches ~260 chunks in that ONE call -- confirmed live: RELIANCE/TCS each
took ~30s end-to-end for one symbol, of which pure API wait (21 windows at
5 req/sec) is only ~4s. At 2,876 symbols that's a ~24-hour run, not the
~3.4 hours pure rate-limit math suggested. This script fetches per-symbol
(reusing the exact same client/normalize path sync_intraday_ohlcv uses) but
accumulates BATCH_SIZE symbols' frames before a single upsert_to_db call,
amortizing the per-call chunk-touch cost across many symbols instead of
paying it per symbol.

Resumable: skips a symbol whose earliest stored bar already reaches back to
(or past) the target start date, so a re-run after an interruption only
re-fetches symbols that are genuinely incomplete.

Run standalone (not a cron job -- this is a one-time historical fill; the
DAILY keep-current job is all_dhan_intraday_sync.sh):
    python -m scripts.backfill_intraday_5yr [--years 5] [--symbols A,B,C] [--batch-size 25]
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta

import pandas as pd

from data.dhanlive.client import DhanAPIError, DhanHistoricalClient, candles_to_df
from data.dhanlive.dhan_db import resolve_dhan_identity
from data.dhanlive.ohlcv import (
    INTRADAY_TABLE,
    _intraday_windows,
    _ist_now,
    _to_naive_utc_datetime,
    ensure_ohlcv_tables,
    is_no_data_error,
    normalize_intraday_frame,
)
from utils.db import sql_to_df, upsert_to_db
from utils.universe import get_equity_universe


def earliest_intraday_timestamp(security_id, exchange: str, asset_type: str, interval_minutes: int):
    df = sql_to_df(
        f"""
        SELECT MIN("timestamp") AS min_timestamp
        FROM {INTRADAY_TABLE}
        WHERE security_id = %s AND exchange = %s AND asset_type = %s AND interval_minutes = %s
        """,
        params=(security_id, exchange.upper(), asset_type.lower(), int(interval_minutes)),
    )
    if df.empty:
        return None
    # MIN(timestamp) comes back tz-aware (TIMESTAMPTZ column) while target_start/
    # now are naive, matching sync_intraday_ohlcv's own convention elsewhere in
    # this module -- same conversion latest_intraday_timestamp() uses for MAX.
    return _to_naive_utc_datetime(df.iloc[0]["min_timestamp"])


def fetch_symbol_frame(client: DhanHistoricalClient, ticker: str, interval_minutes: int, from_date: datetime, to_date: datetime) -> pd.DataFrame:
    """Fetch + normalize one symbol's full window range WITHOUT upserting --
    same fetch loop as sync_intraday_ohlcv, split out so callers can batch the
    upsert across many symbols instead of one call per symbol."""
    identity = resolve_dhan_identity(ticker, "NSE", asset_type="stock")
    frames: list[pd.DataFrame] = []
    for window_start, window_end in _intraday_windows(from_date, to_date):
        try:
            payload = client.fetch_intraday(
                security_id=identity["security_id"],
                exchange_segment=str(identity["exchange_segment"]),
                instrument=str(identity["instrument"]),
                interval_minutes=interval_minutes,
                from_datetime=window_start,
                to_datetime=window_end,
            )
        except DhanAPIError as exc:
            if not is_no_data_error(exc):
                raise
            continue
        frame = normalize_intraday_frame(candles_to_df(payload), identity, interval_minutes)
        if not frame.empty:
            frames.append(frame)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    return df.drop_duplicates(subset=["exchange", "security_id", "interval_minutes", "timestamp"], keep="last")


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill N years of 1-min Dhan intraday OHLCV for the full active universe.")
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--interval-minutes", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=25, help="Symbols per upsert_to_db call")
    parser.add_argument("--symbols", help="Comma-separated override; default is the full active universe")
    args = parser.parse_args()

    ensure_ohlcv_tables()
    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else get_equity_universe()
    if not symbols:
        print("no symbols in universe, aborting", file=sys.stderr)
        return 1

    # BUG FOUND LIVE 2026-08-30 (adversarial review): datetime.now() on this
    # UTC-clocked host returns naive UTC digits, but Dhan's intraday API and
    # this whole module's own naive-datetime convention both mean IST wall-
    # clock -- see data.dhanlive.ohlcv._ist_now's own docstring for the
    # confirmed real-data damage this caused in the daily incremental sync.
    # `now` here is the historical fetch's actual to_date upper bound (via
    # fetch_symbol_frame), so this script's own most-recent trading days were
    # exposed to the identical truncation risk.
    target_start = _ist_now() - timedelta(days=365 * args.years)
    now = _ist_now()
    client = DhanHistoricalClient()

    print(f"[backfill_intraday_5yr] {len(symbols)} symbols, target_start={target_start:%Y-%m-%d}, "
          f"interval={args.interval_minutes}m, batch_size={args.batch_size}", flush=True)
    started = time.monotonic()
    ok, skipped, failed, rows_written = 0, 0, 0, 0
    batch_frames: list[pd.DataFrame] = []

    def flush_batch():
        nonlocal rows_written
        if not batch_frames:
            return
        combined = pd.concat(batch_frames, ignore_index=True)
        upsert_to_db(combined, INTRADAY_TABLE, unique_keys=["exchange", "security_id", "interval_minutes", "timestamp"], timescaledb_column="timestamp")
        rows_written += len(combined)
        batch_frames.clear()

    for i, ticker in enumerate(symbols, start=1):
        try:
            identity_check = resolve_dhan_identity(ticker, "NSE", asset_type="stock")
            earliest = earliest_intraday_timestamp(identity_check["security_id"], str(identity_check["exchange"]), "stock", args.interval_minutes)
            # BUG FOUND LIVE 2026-08-22 (batched-version smoke test): an exact
            # earliest<=target_start comparison never matched -- Dhan's actual
            # earliest served bar for an already-fully-backfilled symbol landed
            # ONE DAY later than the freshly-recomputed target_start (weekend/
            # holiday boundary, or simply a different "now" between runs),
            # so a symbol with real 5-year coverage still looked incomplete
            # and got wastefully re-fetched in full. A few days' tolerance is
            # immaterial for a 5-year window and avoids that false negative.
            if earliest is not None and earliest <= target_start + timedelta(days=5):
                skipped += 1
                continue
            frame = fetch_symbol_frame(client, ticker, args.interval_minutes, target_start, now)
            if not frame.empty:
                batch_frames.append(frame)
            ok += 1
            if len(batch_frames) >= args.batch_size:
                flush_batch()
            elapsed = time.monotonic() - started
            print(
                f"[backfill_intraday_5yr] {i}/{len(symbols)} {ticker}: fetched "
                f"(ok={ok} skipped={skipped} failed={failed} rows_written={rows_written}, {elapsed/60:.1f}min elapsed)",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001 -- one bad symbol must not abort a multi-hour run
            failed += 1
            print(f"[backfill_intraday_5yr] {i}/{len(symbols)} {ticker}: FAILED {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

    flush_batch()
    elapsed = time.monotonic() - started
    print(f"[backfill_intraday_5yr] done: ok={ok} skipped={skipped} failed={failed} total={len(symbols)} "
          f"rows_written={rows_written} elapsed={elapsed/60:.1f}min", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
