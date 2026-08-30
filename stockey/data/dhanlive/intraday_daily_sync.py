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


def run_daily_intraday_sync() -> dict[str, object]:
    symbols = get_equity_universe()
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


def main() -> int:
    global STOCKEY_RUN_STATE
    result = run_daily_intraday_sync()
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
