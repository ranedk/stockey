"""Canonical answer to "what does the pipeline consider the current NSE equity
universe" -- distinct EQ/BE symbols observed in the daily bhavcopy (`nseindia_ohlcv`)
over a recent lookback window. Union over a lookback window rather than just the
newest snapshot: a single incomplete/delayed bhavcopy day must not collapse the
universe and silently exclude symbols downstream.

Promoted here 2026-08-14 (moved out of `data/dhanlive/ohlcv_reconcile.py`) so there is
one shared, always-live source instead of each collector maintaining its own notion of
"the universe". This replaces two prior footguns, both found and fixed the same day:

- `config/tracked_symbols.txt` + `utils/sync.py`'s `load_tracked_symbols()` file
  fallback -- a static 2-symbol placeholder file that multiple collectors silently
  fell back to whenever no explicit --symbols/STOCKEY_SYMBOLS was given (see
  `docs/DATA_INVENTORY.md` for exactly what it contained). The file fallback is
  gone; `load_tracked_symbols()` now only honors an
  explicit --symbols arg or the STOCKEY_SYMBOLS env var.
- `ohlcv_reconcile.py`'s own `load_universe_symbols()`, which used to union
  `advisory_screener_constituents`/`advisory_watchlist`/`advisory_operator_holdings`,
  all of which lost their writers in the 2026-07-27 pure-TA cut and had silently
  frozen at a stale snapshot ever since.

Any collector that needs "the current tradeable NSE universe" should call
`get_equity_universe()` -- not read a config file, not maintain its own list. A
collector that deliberately needs only a small, fixed set of symbols (e.g. a
connectivity/auth smoke test) should pass its own explicit `--symbols`, not rely on
any shared fallback -- see `data/download_runner.py`'s `dhan_ohlcv_precheck` step.
"""
from __future__ import annotations

import sys

from environs import Env

from utils.db import sql_to_df

env = Env()

DEFAULT_LOOKBACK_DAYS = env.int("EQUITY_UNIVERSE_LOOKBACK_DAYS", default=7)
UNIVERSE_SOURCE_TABLE = "nseindia_ohlcv"
# SM/ST (NSE SME) added 2026-09-24: Dhan serves daily and intraday for SME stocks (under
# the id of the series they trade in -- company_master picks it), but they were outside
# this universe, so no Dhan data was ever collected for ~565 listed companies, several of
# them on stockey's own fundamentals watchlist.
UNIVERSE_SERIES = ("EQ", "BE", "SM", "ST")


def get_equity_universe(lookback_days: int | None = None) -> list[str]:
    window = int(DEFAULT_LOOKBACK_DAYS if lookback_days is None else lookback_days)
    query = (
        f"SELECT DISTINCT symbol FROM {UNIVERSE_SOURCE_TABLE} "
        f"WHERE series = ANY(%s) "
        f"AND date >= (SELECT MAX(date) FROM {UNIVERSE_SOURCE_TABLE}) - interval '{window} days'"
    )
    try:
        frame = sql_to_df(query, params=(list(UNIVERSE_SERIES),))
    except Exception as exc:
        print(f"[universe] {UNIVERSE_SOURCE_TABLE} unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
        return []
    if frame.empty or "symbol" not in frame.columns:
        return []
    symbols = {str(value or "").strip().upper() for value in frame["symbol"].tolist() if str(value or "").strip()}
    return sorted(symbols)
