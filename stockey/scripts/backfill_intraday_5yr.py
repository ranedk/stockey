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

Resumable via a persisted per-symbol completion table
(dhan_intraday_backfill_state): once a symbol's window has been fetched as
completely as Dhan will serve it, it is recorded and skipped on every later run.
The earlier "earliest stored bar <= target_start" heuristic could not express
that -- target_start is recomputed as now()-5y each launch, so any symbol whose
history genuinely starts later (a recent IPO, or a name Dhan simply does not
hold 5 years of) never matched and was re-fetched in full on every restart.
Pass --ignore-state to re-evaluate everything from scratch.

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


# Per-symbol completion state. WITHOUT this, the only resume signal is
# "earliest stored bar <= target_start + 5d", and target_start is recomputed as
# now()-5y on EVERY launch. A symbol whose history genuinely begins inside that
# window -- an IPO from last year, or simply a name Dhan does not hold 5 years of --
# can never satisfy it, so each restart re-fetched and re-upserted its ENTIRE window.
# That is the "never_skippable" population the audit's --deep mode counts, and the
# reason a restart cost hundreds of millions of rows of rewrite.
BACKFILL_STATE_TABLE = "dhan_intraday_backfill_state"


def ensure_backfill_state_table() -> None:
    from utils.db import db_session

    with db_session() as (_conn, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {BACKFILL_STATE_TABLE} (
                ticker            text        NOT NULL,
                interval_minutes  integer     NOT NULL,
                target_start      date        NOT NULL,
                earliest_bar      timestamptz,
                completed_at      timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (ticker, interval_minutes)
            )
            """
        )


def load_completed(interval_minutes: int) -> dict[str, dict]:
    df = sql_to_df(
        f"SELECT ticker, target_start, earliest_bar FROM {BACKFILL_STATE_TABLE} "
        f"WHERE interval_minutes = %s",
        params=(int(interval_minutes),),
    )
    if df.empty:
        return {}
    return {r.ticker: {"target_start": r.target_start, "earliest_bar": r.earliest_bar} for r in df.itertuples()}


def mark_completed(ticker: str, interval_minutes: int, target_start: datetime, earliest_bar) -> None:
    from utils.db import db_session

    with db_session() as (_conn, cur):
        cur.execute(
            f"""
            INSERT INTO {BACKFILL_STATE_TABLE}
                   (ticker, interval_minutes, target_start, earliest_bar, completed_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (ticker, interval_minutes) DO UPDATE
               SET target_start = EXCLUDED.target_start,
                   earliest_bar = EXCLUDED.earliest_bar,
                   completed_at = EXCLUDED.completed_at
            """,
            (ticker, int(interval_minutes), target_start.date(), earliest_bar),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill N years of 1-min Dhan intraday OHLCV for the full active universe.")
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--interval-minutes", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=25, help="Symbols per upsert_to_db call")
    parser.add_argument("--max-batch-rows", type=int, default=500_000,
                        help="Flush once this many rows are buffered, whichever cap trips first. "
                             "A ROW cap is the real memory bound; a symbol count is not.")
    parser.add_argument("--ignore-state", action="store_true",
                        help="Ignore the persisted per-symbol completion state and re-evaluate every symbol.")
    parser.add_argument("--symbols", help="Comma-separated override; default is the full active universe")
    args = parser.parse_args()

    ensure_ohlcv_tables()
    ensure_backfill_state_table()
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

    completed = {} if args.ignore_state else load_completed(args.interval_minutes)

    print(f"[backfill_intraday_5yr] {len(symbols)} symbols, target_start={target_start:%Y-%m-%d}, "
          f"interval={args.interval_minutes}m, batch_size={args.batch_size}, "
          f"max_batch_rows={args.max_batch_rows}, already_complete={len(completed)}", flush=True)
    started = time.monotonic()
    ok, skipped, failed, rows_written, empty = 0, 0, 0, 0, 0
    batch_frames: list[pd.DataFrame] = []
    batch_rows = 0

    def flush_batch():
        """Upsert whatever is buffered and ALWAYS drop the buffer.

        The clear() used to be the last statement of the try-body, so any upsert
        failure unwound to the per-symbol handler below and left the frames in the
        list -- which then grew for the remainder of a multi-hour run at ~59 MB per
        retained symbol. ~430 retained symbols is the 25.6 GB process that OOM'd the
        box. A failed flush must lose its batch, not accumulate it: the symbols are
        recorded as failed and a later re-run refetches them.
        """
        nonlocal rows_written, batch_rows
        if not batch_frames:
            return
        try:
            combined = pd.concat(batch_frames, ignore_index=True)
            # on_conflict="nothing": see data/dhanlive/ohlcv.py -- a DO UPDATE here
            # rewrites compressed chunks and is what made this script cost ~1.6 TB
            # of WAL per run. Re-fetched windows carry identical values; only
            # genuinely new bars are written.
            upsert_to_db(
                combined,
                INTRADAY_TABLE,
                unique_keys=["exchange", "security_id", "interval_minutes", "timestamp"],
                timescaledb_column="timestamp",
                on_conflict="nothing",
            )
            rows_written += len(combined)
        finally:
            batch_frames.clear()
            batch_rows = 0

    for i, ticker in enumerate(symbols, start=1):
        try:
            # Cheapest check first: a symbol recorded complete for a target_start at or
            # before this run's is done, no matter where its history actually begins.
            # This is what makes "never_skippable" symbols skippable.
            state = completed.get(ticker)
            if state is not None and state["target_start"] <= target_start.date():
                skipped += 1
                continue

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
                # Already covers the window -- record it so the next run skips it
                # without re-deriving anything.
                mark_completed(ticker, args.interval_minutes, target_start, earliest)
                completed[ticker] = {"target_start": target_start.date(), "earliest_bar": earliest}
                skipped += 1
                continue
            frame = fetch_symbol_frame(client, ticker, args.interval_minutes, target_start, now)
            if not frame.empty:
                batch_frames.append(frame)
                batch_rows += len(frame)
            ok += 1
            # Record completion ONLY when rows actually came back. A symbol whose
            # window fetched successfully is done even if its earliest bar never
            # reaches target_start -- that is exactly the case the old
            # earliest<=target_start check could not express, and why those symbols
            # were re-fetched AND re-upserted on every restart.
            #
            # A zero-row result is deliberately NOT recorded. fetch_symbol_frame
            # swallows per-window "no data" responses, so a Dhan-side outage looks
            # identical to a symbol Dhan genuinely holds nothing for; marking those
            # complete would blacklist them permanently on a transient failure.
            # Re-fetching them is cheap -- no rows means no upsert, which is the
            # expensive half. Whether the data is actually present is the
            # completeness checker's question, not this table's.
            if not frame.empty:
                mark_completed(ticker, args.interval_minutes, target_start,
                               frame["timestamp"].min())
                completed[ticker] = {"target_start": target_start.date(),
                                     "earliest_bar": frame["timestamp"].min()}
            else:
                empty += 1
            # Cap on retained ROWS, not retained frames: frame sizes vary by orders of
            # magnitude (a 5-year fill vs a one-day top-up), so a frame count is not a
            # memory bound. ~500k rows is roughly 60-70 MB of DataFrame.
            if batch_rows >= args.max_batch_rows or len(batch_frames) >= args.batch_size:
                flush_batch()
            elapsed = time.monotonic() - started
            print(
                f"[backfill_intraday_5yr] {i}/{len(symbols)} {ticker}: fetched "
                f"(ok={ok} skipped={skipped} empty={empty} failed={failed} rows_written={rows_written}, {elapsed/60:.1f}min elapsed)",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001 -- one bad symbol must not abort a multi-hour run
            failed += 1
            print(f"[backfill_intraday_5yr] {i}/{len(symbols)} {ticker}: FAILED {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

    flush_batch()
    elapsed = time.monotonic() - started
    print(f"[backfill_intraday_5yr] done: ok={ok} skipped={skipped} empty={empty} failed={failed} total={len(symbols)} "
          f"rows_written={rows_written} elapsed={elapsed/60:.1f}min", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
