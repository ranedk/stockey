"""Daily incremental Dhan 1-min intraday OHLCV sync for the FULL active NSE
equity universe (utils.universe.get_equity_universe() -- no static symbol
list, per CLAUDE.md's "no static symbol/company registry file" rule).

Companion to scripts/backfill_intraday_5yr.py (the one-time 5-year
historical fill, 2026-08-22): this is the ongoing keep-current job. It uses
sync_many_intraday's own default incremental behavior (from_date=None ->
pulls from each symbol's own last stored bar forward, with a small overlap
window, not a fixed lookback) so a normal day's run is one small per-symbol
window, not a bulk re-pull -- unlike the 5-year backfill, this should take
minutes, not hours.

Scheduled daily after EOD (all_dhan_intraday_sync.sh, right after
all_price_adjustment.sh so the day's price data is settled) -- see
CLAUDE.md's Current Architecture job list. Feeds systrader's own daily
scripts/sync_intraday_from_stockey.sh, scheduled to run after this."""
from __future__ import annotations

import argparse
import json

from data.dhanlive.ohlcv import sync_many_intraday
from utils.fallback_telemetry import record_local_fallback_event
from utils.universe import get_equity_universe

STOCKEY_RUN_STATE: dict[str, object] = {}

# A handful of individual symbol failures in a 2,800+-symbol daily sweep is
# normal (delisted-but-still-in-bhavcopy edge cases, a transient Dhan gap for
# one name); only escalate to "degraded" once failures look like a systemic
# problem rather than routine noise.
DEGRADED_FAILURE_FRACTION = 0.10


def _stale_symbols(symbols: list[str]) -> list[str]:
    """Universe symbols with no intraday bar on the most recent stored session.

    Bounded on purpose: the subquery is restricted to the newest session only, never
    a whole-table scan. dhan_ohlcv_intraday is ~525M rows across 263 compressed
    chunks, and an unbounded aggregate over it OOM'd this box on 2026-08-31."""
    from utils.db import sql_to_df

    df = sql_to_df(
        """
        WITH latest AS (
          SELECT max(timestamp)::date AS d
            FROM dhan_ohlcv_intraday
           WHERE timestamp >= now() - interval '30 days'
        )
        SELECT DISTINCT ticker FROM dhan_ohlcv_intraday, latest
         WHERE timestamp >= latest.d AND timestamp < latest.d + 1
        """
    )
    current = set(df["ticker"]) if not df.empty else set()
    return [s for s in symbols if s not in current]


def run_daily_intraday_sync(
    *, max_symbols: int | None = None, only_stale: bool = False
) -> dict[str, object]:
    """Sync the active universe forward from each symbol's own last stored bar.

    max_symbols / only_stale exist for RECOVERY, not for the nightly run (whose
    defaults are unchanged: whole universe, every symbol). After the 2026-08-26..31
    outage the backlog was ~2,590 symbols x 3 sessions in one process; bounding it
    lets the repair run in batches with a memory check between them. The job is
    naturally resumable either way -- from_date=None means each symbol resumes from
    its own last stored bar -- so an interrupted batch simply re-fetches less next
    time, never the whole window."""
    symbols = get_equity_universe()
    if symbols and only_stale:
        symbols = _stale_symbols(symbols)
    if symbols and max_symbols is not None:
        symbols = symbols[: max(0, int(max_symbols))]
    if not symbols:
        record_local_fallback_event(
            module="data.dhanlive.intraday_daily_sync",
            source="utils.universe.get_equity_universe",
            fallback_type="intraday_daily_sync_empty_universe",
            severity="error",
            reason="get_equity_universe() returned no symbols; the daily intraday sync ran against an empty universe.",
            error=None,
        )
        return {"symbols": 0, "succeeded": 0, "failed": 0, "failed_symbols": [], "total_rows": 0}

    results = sync_many_intraday(symbols, exchange="NSE", asset_type="stock", interval_minutes=1)
    failed = [r for r in results if r.get("error")]
    total_rows = sum(int(r.get("rows") or 0) for r in results)
    if failed:
        record_local_fallback_event(
            module="data.dhanlive.intraday_daily_sync",
            source="dhan_ohlcv_intraday",
            fallback_type="intraday_daily_sync_symbol_failures",
            severity="warn" if len(failed) < len(symbols) * DEGRADED_FAILURE_FRACTION else "error",
            reason=f"{len(failed)}/{len(symbols)} symbols failed in the daily intraday sync.",
            error=None,
            metadata={"failed_count": len(failed), "sample_failed_symbols": [r["ticker"] for r in failed][:20]},
        )
    return {
        "symbols": len(symbols),
        "succeeded": len(results) - len(failed),
        "failed": len(failed),
        "failed_symbols": [r["ticker"] for r in failed],
        "total_rows": total_rows,
    }


def main(argv: list[str] | None = None) -> int:
    global STOCKEY_RUN_STATE
    parser = argparse.ArgumentParser(description="Daily incremental Dhan 1-min intraday OHLCV sync.")
    parser.add_argument("--max-symbols", type=int, default=None,
                        help="Recovery only: cap symbols this run (default: the whole universe).")
    parser.add_argument("--only-stale", action="store_true",
                        help="Recovery only: sync symbols missing from the most recent STORED session. "
                             "Note this is a keep-current measure, not a historical-gap measure: run it "
                             "during market hours and 'the most recent session' is today's PARTIAL one, so "
                             "every symbol that has not yet reported today counts as stale and the number "
                             "will not converge. To close a historical gap, compare against a window "
                             "instead (see scripts/data_completeness.py's universe_coverage check).")
    args = parser.parse_args(argv)
    result = run_daily_intraday_sync(max_symbols=args.max_symbols, only_stale=args.only_stale)
    degraded = result["symbols"] > 0 and result["failed"] >= result["symbols"] * DEGRADED_FAILURE_FRACTION
    STOCKEY_RUN_STATE = {
        "source": "data.dhanlive.intraday_daily_sync",
        "rows": result["total_rows"],
        "rows_written": result["total_rows"],
        "symbols": result["symbols"],
        "succeeded": result["succeeded"],
        "failed": result["failed"],
        "fallback_used": result["failed"] > 0,
        "state_advanced": result["total_rows"] > 0,
        "status": "degraded" if degraded else "ok",
    }
    print(
        json.dumps({**STOCKEY_RUN_STATE, "failed_symbols_sample": result["failed_symbols"][:20]}, ensure_ascii=False, default=str),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
