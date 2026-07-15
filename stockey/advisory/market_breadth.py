"""Market breadth -- the richer market check NIFTY misses (2026-07-14).

NIFTY is cap-weighted: a handful of megacaps can hold it above its 50DMA while most stocks break down
(a narrow top), or drag it down while the median stock is fine. Breadth = % of the liquid universe above
its OWN 50DMA measures the market most stocks actually live in. Measured on this history: NIFTY>50DMA and
breadth>=50% AGREE only 67% of days (54 narrow-rally days where NIFTY says healthy but the market is
narrow), and a breadth-based crash floor DOMINATES the NIFTY-50DMA floor on the paper book across every
threshold 0.40-0.60 (same-or-better drawdown, higher return, best risk-adjusted) -- see todo.md T2/breadth.

This computes and persists the breadth series (point-in-time, on split-adjusted closes) so consumers -- the
crash floor (advisory.portfolio_risk.breadth_floor_multiplier), the regime split, the shadow ledger -- read
one number per day instead of a single cap-weighted index level. Descriptive market measurement, NOT a
fitted knob. Research/report-only. CLI: `python -m advisory.market_breadth`.
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any

import numpy as np
import pandas as pd

from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration

MIN_PRICE = float(os.getenv("MISSED_MOVERS_MIN_PRICE", "30.0"))
MIN_TURNOVER = float(os.getenv("MARKET_ACTION_SCAN_MIN_TURNOVER_INR", "50000000"))

TABLE_NAME = "advisory_market_breadth_daily"
MIGRATION_ID = "20260714_advisory_market_breadth_daily"
SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        date TIMESTAMPTZ NOT NULL,
        n_universe_50 BIGINT,
        n_universe_200 BIGINT,
        pct_above_50 DOUBLE PRECISION,
        pct_above_200 DOUBLE PRECISION,
        load_ts TIMESTAMPTZ,
        UNIQUE (date)
    )
    """,
]


def ensure_table() -> None:
    apply_schema_migration(
        migration_id=MIGRATION_ID,
        statements=SCHEMA_STATEMENTS,
        owner="advisory.market_breadth",
        description="Daily market breadth: % of the liquid universe above its own 50DMA / 200DMA (adj_close).",
        metadata={"tables": [TABLE_NAME], "authority": "research_only"},
    )


def compute_breadth(prices: pd.DataFrame, *, min_price: float = MIN_PRICE,
                    min_turnover: float = MIN_TURNOVER) -> pd.DataFrame:
    """Pure: from [symbol, date, adj_close, raw_close, turnover] (full per-symbol history), return per-date
    % of LIQUID names trading above their own trailing 50DMA / 200DMA. The DMA is the mean of the prior N
    closes (EXCLUDING today -> no lookahead); a name counts on a day only if that DMA is defined AND the
    row is liquid (raw price + turnover). adj_close keeps splits/bonuses from faking a DMA cross."""
    if prices.empty:
        return pd.DataFrame(columns=["date", "n_universe_50", "n_universe_200", "pct_above_50", "pct_above_200"])
    df = prices.sort_values(["symbol", "date"]).copy()
    g = df.groupby("symbol")["adj_close"]
    df["dma50"] = g.transform(lambda s: s.shift(1).rolling(50, min_periods=50).mean())
    df["dma200"] = g.transform(lambda s: s.shift(1).rolling(200, min_periods=200).mean())
    liquid = (df["raw_close"] >= min_price) & (df["turnover"] >= min_turnover)
    df = df[liquid].copy()
    df["above50"] = np.where(df["dma50"].notna(), (df["adj_close"] > df["dma50"]).astype(float), np.nan)
    df["above200"] = np.where(df["dma200"].notna(), (df["adj_close"] > df["dma200"]).astype(float), np.nan)
    out = df.groupby("date").agg(
        n_universe_50=("above50", "count"),
        n_universe_200=("above200", "count"),
        pct_above_50=("above50", "mean"),
        pct_above_200=("above200", "mean"),
    ).reset_index()
    return out.sort_values("date").reset_index(drop=True)


def _load_prices() -> pd.DataFrame:
    return sql_to_df(
        """
        SELECT o.symbol, o.date, a.adj_close, o.close AS raw_close,
               AVG(o.volume) OVER w20 * o.close AS turnover
        FROM nseindia_ohlcv o JOIN advisory_adjusted_ohlcv_daily a ON a.symbol = o.symbol AND a.date = o.date
        WHERE o.series IN ('EQ', 'BE')
        WINDOW w20 AS (PARTITION BY o.symbol ORDER BY o.date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING)
        ORDER BY o.symbol, o.date
        """
    )


def load_breadth() -> pd.DataFrame:
    """Read the persisted breadth series (date, pct_above_50, pct_above_200)."""
    df = sql_to_df(f"SELECT date, pct_above_50, pct_above_200 FROM {TABLE_NAME} ORDER BY date")
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce").dt.normalize()
        for c in ("pct_above_50", "pct_above_200"):
            df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    return df


def build_breadth(*, dry_run: bool = False) -> dict[str, Any]:
    prices = _load_prices()
    if prices.empty:
        return {"rows": 0, "note": "no prices"}
    prices["date"] = pd.to_datetime(prices["date"], utc=True, errors="coerce").dt.normalize()
    for c in ("adj_close", "raw_close", "turnover"):
        prices[c] = pd.to_numeric(prices[c], errors="coerce")
    breadth = compute_breadth(prices)
    breadth["load_ts"] = pd.Timestamp.utcnow()
    if not dry_run:
        ensure_table()
        upsert_to_db(breadth, TABLE_NAME, unique_keys=["date"], timescaledb_column="date")
    latest = breadth.dropna(subset=["pct_above_50"]).tail(1)
    return {
        "rows": int(len(breadth)),
        "dates": [str(breadth["date"].min().date()), str(breadth["date"].max().date())],
        "latest_pct_above_50": round(float(latest["pct_above_50"].iloc[0]), 3) if not latest.empty else None,
        "latest_pct_above_200": round(float(latest["pct_above_200"].iloc[0]), 3) if not latest.empty else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the daily market-breadth series (% of liquid names above 50/200 DMA).")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(build_breadth(dry_run=bool(args.dry_run)), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
