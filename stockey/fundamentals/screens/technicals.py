"""Long-term descriptive technicals -- feeds the fundamentals screener's watch-summary
narrative (fundamentals/screens/watchlist.py) and the sectors view, per the boundary
worked out in conversation 2026-08-11: momentum and mean-reversion are DESCRIPTIVE
facts here (observable, unparametrized, no entry/exit signal), never a scored or
trading-actionable signal -- that stays exclusively in systrader's LEDGER-governed
research process (see stockey/CLAUDE.md's boundary section and
docs/FUNDAMENTAL_SCREENER_PRD.md sec 1: "the two must never be blended into one
decision pipeline"). This module answers "is this stock currently up a lot / down a
lot / stretched from its own average", nothing more -- no buy/sell/hold verdict, no
threshold-based classification, no backtest.

Uses advisory_adjusted_ohlcv_daily (stockey's own split/bonus-adjusted PRIMARY equity
series, keyed by bare `symbol`, not company_master_id -- confirmed live 2026-08-11),
not raw nseindia_ohlcv: momentum windows here (3/6/12 months) are long enough to
routinely cross a corporate action, unlike llm_triage.py's short 20-session price
context where raw close is fine. Coverage confirmed live: 138/188 L1 tickers have an
adjusted series (73%) -- the remainder (recent listings, series gaps) get a row with
all stats None plus a fallback event, not silently skipped.

BSE-only-company fallback (2026-08-15): a company with no NSE listing (confirmed
live -- 43 of the active L1 universe) has nothing in advisory_adjusted_ohlcv_daily
at all, since that view is entirely NSE/Dhan-sourced. load_adjusted_price_history()
falls back to bse_advisory_adjusted_ohlcv_daily (data/bseindia/price_adjustment.py)
keyed by scrip_code -- fundamentals_l1_universe.ticker for a BSE-only company IS
already its BSE scrip code (confirmed live: company_master's identity resolution
stores the scrip code in that slot for a company with no NSE ticker), so no extra
identity lookup is needed here, the same `ticker` value is used against both views.
This is a cross-package READ of a data/ table (fundamentals building on top of the
pure-TA platform, not the reverse -- see CLAUDE.md's boundary section); this module
still writes only its own fundamentals_technicals table.

Windows are in trading days (63/126/252 for 3/6/12 months, standard convention), not
calendar days -- a company needs that many rows of history for a given window to be
computed at all; shorter-history companies get partial rows (some windows None), never
padded or estimated.
"""

from __future__ import annotations

import json

import pandas as pd

from data.bseindia.price_adjustment import ensure_view as ensure_bse_view
from utils.db import sql_to_df, upsert_to_db
from utils.fallback_telemetry import record_local_fallback_event

SYNC_SOURCE_NAME = "fundamentals.screens.technicals"
RESULTS_TABLE = "fundamentals_technicals"
STOCKEY_RUN_STATE: dict[str, object] = {}

# Trading-day approximations for calendar months -- standard convention, not tuned.
RETURN_WINDOWS_TRADING_DAYS = {"3m": 63, "6m": 126, "12m": 252}
DMA_WINDOWS = (50, 200)
# Below this many rows of adjusted-close history, no stats are computed at all (an
# empty row is still written, with a fallback event, so the gap is visible rather than
# the company just silently missing from the table).
MIN_HISTORY_ROWS = 20


def _record_fallback(fallback_type: str, *, reason: str, error, severity: str = "warn", metadata=None) -> None:
    record_local_fallback_event(
        module=SYNC_SOURCE_NAME,
        source="advisory_adjusted_ohlcv_daily",
        fallback_type=fallback_type,
        severity=severity,
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def load_l1_tickers() -> pd.DataFrame:
    return sql_to_df(
        """
        SELECT ticker, company_name
        FROM fundamentals_l1_universe
        WHERE run_date = (SELECT MAX(run_date) FROM fundamentals_l1_universe)
        """
    )


def load_adjusted_price_history(ticker: str, *, lookback_days: int = 300) -> pd.DataFrame:
    nse_history = sql_to_df(
        """
        SELECT date, adj_close
        FROM advisory_adjusted_ohlcv_daily
        WHERE symbol = %s AND series = 'EQ'
        ORDER BY date DESC
        LIMIT %s
        """,
        params=(ticker, lookback_days),
    )
    if not nse_history.empty:
        return nse_history.sort_values("date").reset_index(drop=True)

    # BSE-only-company fallback -- see module docstring. `ticker` here is already the
    # BSE scrip code for these companies, matching bse_advisory_adjusted_ohlcv_daily's
    # own scrip_code key directly.
    bse_history = sql_to_df(
        """
        SELECT date, adj_close
        FROM bse_advisory_adjusted_ohlcv_daily
        WHERE scrip_code = %s
        ORDER BY date DESC
        LIMIT %s
        """,
        params=(ticker, lookback_days),
    )
    return bse_history.sort_values("date").reset_index(drop=True)


def compute_technicals(history: pd.DataFrame) -> dict:
    """Pure function over an ascending-by-date (date, adj_close) frame -- returns
    descriptive stats only, None wherever there isn't enough history for a given
    window (never estimated or padded)."""
    if history.empty:
        return {"as_of_date": None, "close": None, "data_points_available": 0}

    closes = history["adj_close"]
    latest_close = float(closes.iloc[-1])
    n = len(closes)
    result: dict[str, object] = {
        "as_of_date": history["date"].iloc[-1],
        "close": latest_close,
        "data_points_available": int(n),
    }

    for label, window in RETURN_WINDOWS_TRADING_DAYS.items():
        if n > window:
            past_close = float(closes.iloc[-1 - window])
            result[f"return_{label}_pct"] = round((latest_close - past_close) / past_close * 100, 2) if past_close else None
        else:
            result[f"return_{label}_pct"] = None

    for window in DMA_WINDOWS:
        if n >= window:
            dma = float(closes.tail(window).mean())
            result[f"dma_{window}"] = round(dma, 2)
            result[f"pct_vs_dma_{window}"] = round((latest_close - dma) / dma * 100, 2) if dma else None
        else:
            result[f"dma_{window}"] = None
            result[f"pct_vs_dma_{window}"] = None

    # Mean-reversion descriptive stat: how many standard deviations the current close
    # sits from its own trailing-200-day mean -- a fact about dispersion, not a signal
    # threshold (no "buy at -2 sigma" rule lives here).
    if n >= 200:
        window_closes = closes.tail(200)
        mean_200 = float(window_closes.mean())
        std_200 = float(window_closes.std())
        result["z_score_vs_200d_mean"] = round((latest_close - mean_200) / std_200, 2) if std_200 else None
    else:
        result["z_score_vs_200d_mean"] = None

    return result


def run_technicals_refresh() -> dict[str, object]:
    ensure_bse_view()  # self-heals bseindia_ohlcv/bseindia_adjustment_factors/the view -- see module docstring
    tickers = load_l1_tickers()
    if tickers.empty:
        _record_fallback(
            "technicals_no_l1_universe",
            reason="No fundamentals_l1_universe rows to compute technicals for.",
            error="empty L1 universe",
        )
        return {"companies": 0, "no_history": 0}

    run_date = pd.Timestamp.now(tz="UTC").normalize()
    rows = []
    no_history = 0
    for _, row in tickers.iterrows():
        ticker = row["ticker"]
        history = load_adjusted_price_history(ticker)
        stats = compute_technicals(history)
        if stats["data_points_available"] < MIN_HISTORY_ROWS:
            no_history += 1
            _record_fallback(
                "technicals_insufficient_history",
                reason="Fewer than MIN_HISTORY_ROWS of advisory_adjusted_ohlcv_daily history for this ticker; row written with stats=None rather than skipped.",
                error="insufficient history",
                metadata={"ticker": ticker, "rows_available": stats["data_points_available"]},
            )

        rows.append(
            {
                "company_master_id": f"nse:{ticker}",
                "ticker": ticker,
                "run_date": run_date,
                **{k: v for k, v in stats.items() if k != "data_points_available"},
                "data_points_available": stats["data_points_available"],
                "load_ts": pd.Timestamp.now(tz="UTC"),
            }
        )

    result_df = pd.DataFrame(rows)
    upsert_to_db(result_df, RESULTS_TABLE, unique_keys=["company_master_id", "run_date"])
    return {"companies": int(len(result_df)), "no_history": no_history}


def main() -> int:
    global STOCKEY_RUN_STATE
    result = run_technicals_refresh()
    STOCKEY_RUN_STATE = {
        "source": SYNC_SOURCE_NAME,
        "rows": result["companies"],
        "rows_written": result["companies"],
        "no_history": result["no_history"],
        "fallback_used": result["no_history"] > 0,
        "state_advanced": result["companies"] > 0,
        "status": "ok",
    }
    print(json.dumps(STOCKEY_RUN_STATE, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
