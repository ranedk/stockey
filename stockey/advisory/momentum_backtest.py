"""Momentum-archetype backtest: does buying momentum leaders pay, and WHEN?

Uses the months of price history we already have to answer the conditional the spec calls
for (3c) -- not the blanket "does momentum work?" but "does it work when the regime is
favorable vs not?" Pure price: deterministic, point-in-time (only data up to each decision
date drives the pick; forward windows are the outcome, not an input), no LLM.

For each sampled historical date it replicates the momentum scan's admission filter, then
scores each pick's forward 5/10/20-day benchmark-excess-after-cost, tagged by a point-in-time
regime proxy (NIFTY above its 50-DMA on the decision date). Reports hit rate, mean excess,
and expectancy overall and split by regime -- the evidence Phase B (performance-following
allocation) and the critic need, generated from history instead of waiting for live cycles.

Overlapping forward windows are reduced by sampling every SAMPLE_STEP_DAYS trading days.
Review-only research; writes advisory_momentum_backtest, never trades.
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any

import pandas as pd

from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration

TABLE_NAME = "advisory_momentum_backtest"
MIGRATION_ID = "20260712_advisory_momentum_backtest_base"
SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        run_date TIMESTAMPTZ NOT NULL,
        horizon_days BIGINT NOT NULL,
        regime TEXT NOT NULL,
        picks BIGINT,
        hit_rate DOUBLE PRECISION,
        mean_excess_pct DOUBLE PRECISION,
        median_excess_pct DOUBLE PRECISION,
        mean_abs_return_pct DOUBLE PRECISION,
        abs_win_rate DOUBLE PRECISION,
        avg_win_pct DOUBLE PRECISION,
        avg_loss_pct DOUBLE PRECISION,
        expectancy_pct DOUBLE PRECISION,
        load_ts TIMESTAMPTZ,
        UNIQUE (run_date, horizon_days, regime)
    )
    """,
]

MIN_RETURN_PCT = float(os.getenv("MOMENTUM_SCAN_MIN_RETURN_PCT", "15.0"))
LOOKBACK_DAYS = int(os.getenv("MOMENTUM_SCAN_LOOKBACK_DAYS", "10"))
MIN_52W_PROX = float(os.getenv("MOMENTUM_SCAN_MIN_52W_PROXIMITY", "0.90"))
MIN_PRICE = float(os.getenv("MISSED_MOVERS_MIN_PRICE", "30.0"))
MIN_TURNOVER = float(os.getenv("MARKET_ACTION_SCAN_MIN_TURNOVER_INR", "50000000"))
SAMPLE_STEP_DAYS = int(os.getenv("MOMENTUM_BACKTEST_SAMPLE_STEP_DAYS", "5"))
COST_BPS = float(os.getenv("MOMENTUM_BACKTEST_COST_BPS", "25"))
HORIZONS = (5, 10, 20)


def ensure_table() -> None:
    apply_schema_migration(
        migration_id=MIGRATION_ID,
        description="Store momentum-archetype backtest results (forward excess by regime/horizon).",
        statements=SCHEMA_STATEMENTS,
        metadata={"module": "advisory.momentum_backtest", "tables": [TABLE_NAME]},
    )


def _load_momentum_picks() -> pd.DataFrame:
    return sql_to_df(
        f"""
        WITH eq AS (
            SELECT symbol, date, close,
                   LAG(close, {LOOKBACK_DAYS}) OVER w AS close_n_ago,
                   AVG(close)  OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS dma20,
                   MAX(close)  OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 252 PRECEDING AND 1 PRECEDING) AS max252,
                   AVG(volume) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS avgvol20,
                   LEAD(close, 5)  OVER w AS fwd5,
                   LEAD(close, 10) OVER w AS fwd10,
                   LEAD(close, 20) OVER w AS fwd20,
                   COUNT(*) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 252 PRECEDING AND 1 PRECEDING) AS nprev
            FROM nseindia_ohlcv WHERE series = 'EQ'
            WINDOW w AS (PARTITION BY symbol ORDER BY date)
        )
        SELECT symbol, date, close, fwd5, fwd10, fwd20
        FROM eq
        WHERE close_n_ago IS NOT NULL AND dma20 IS NOT NULL AND max252 IS NOT NULL
          AND nprev >= 60
          AND (close / NULLIF(close_n_ago, 0) - 1) * 100 >= %(min_ret)s
          AND close > dma20
          AND close >= max252 * %(min_prox)s
          AND close >= %(min_price)s
          AND avgvol20 * close >= %(min_turnover)s
        ORDER BY date, symbol
        """,
        params={"min_ret": MIN_RETURN_PCT, "min_prox": MIN_52W_PROX,
                "min_price": MIN_PRICE, "min_turnover": MIN_TURNOVER},
    )


def _load_benchmark() -> pd.DataFrame:
    return sql_to_df(
        """
        SELECT date, close AS bench_close,
               AVG(close) OVER (ORDER BY date ROWS BETWEEN 50 PRECEDING AND 1 PRECEDING) AS bench_dma50,
               LEAD(close, 5)  OVER (ORDER BY date) AS bench_fwd5,
               LEAD(close, 10) OVER (ORDER BY date) AS bench_fwd10,
               LEAD(close, 20) OVER (ORDER BY date) AS bench_fwd20
        FROM nseindia_indices WHERE index_name ILIKE 'nifty 50' ORDER BY date
        """
    )


def run_backtest(*, dry_run: bool = False) -> dict[str, Any]:
    ensure_table()
    picks = _load_momentum_picks()
    bench = _load_benchmark()
    if picks.empty or bench.empty:
        return {"picks": 0, "results": []}
    picks["date"] = pd.to_datetime(picks["date"], utc=True, errors="coerce").dt.normalize()
    bench["date"] = pd.to_datetime(bench["date"], utc=True, errors="coerce").dt.normalize()
    # sample every SAMPLE_STEP_DAYS trading days to cut overlapping-window autocorrelation
    trading_days = sorted(bench["date"].dropna().unique())
    sampled = set(trading_days[::max(1, SAMPLE_STEP_DAYS)])
    picks = picks[picks["date"].isin(sampled)]
    df = picks.merge(bench, on="date", how="inner")
    if df.empty:
        return {"picks": 0, "results": []}
    df["regime"] = df.apply(
        lambda r: "favorable" if pd.notna(r["bench_dma50"]) and r["bench_close"] > r["bench_dma50"] else "unfavorable",
        axis=1,
    )
    cost = COST_BPS / 10000.0
    now = pd.Timestamp.utcnow()
    rows: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for h in HORIZONS:
        sub = df.dropna(subset=[f"fwd{h}", f"bench_fwd{h}"]).copy()
        if sub.empty:
            continue
        sub["abs_ret"] = sub[f"fwd{h}"] / sub["close"] - 1.0 - cost
        sub["excess"] = sub["abs_ret"] - (sub[f"bench_fwd{h}"] / sub["bench_close"] - 1.0)
        for regime in ("all", "favorable", "unfavorable"):
            seg = sub if regime == "all" else sub[sub["regime"] == regime]
            if seg.empty:
                continue
            wins = seg[seg["excess"] > 0]["excess"]
            losses = seg[seg["excess"] <= 0]["excess"]
            hit = round(float(len(wins)) / len(seg), 4)
            avg_win = round(float(wins.mean()) * 100, 3) if len(wins) else 0.0
            avg_loss = round(float(losses.mean()) * 100, 3) if len(losses) else 0.0
            expectancy = round((hit * avg_win) + ((1 - hit) * avg_loss), 3)
            rec = {
                "run_date": now.normalize(), "horizon_days": h, "regime": regime,
                "picks": int(len(seg)), "hit_rate": hit,
                "mean_excess_pct": round(float(seg["excess"].mean()) * 100, 3),
                "median_excess_pct": round(float(seg["excess"].median()) * 100, 3),
                "mean_abs_return_pct": round(float(seg["abs_ret"].mean()) * 100, 3),
                "abs_win_rate": round(float((seg["abs_ret"] > 0).mean()), 4),
                "avg_win_pct": avg_win, "avg_loss_pct": avg_loss,
                "expectancy_pct": expectancy, "load_ts": now,
            }
            rows.append(rec)
            results.append({k: rec[k] for k in ("horizon_days", "regime", "picks", "hit_rate", "mean_excess_pct", "mean_abs_return_pct", "abs_win_rate", "expectancy_pct")})
    if rows and not dry_run:
        upsert_to_db(pd.DataFrame(rows), TABLE_NAME, unique_keys=["run_date", "horizon_days", "regime"], timescaledb_column="run_date")
    return {"picks": int(len(df)), "sampled_dates": len(sampled), "results": results, "dry_run": dry_run}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest the momentum archetype's forward excess by regime.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)
    out = run_backtest(dry_run=bool(args.dry_run))
    if args.format == "json":
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"[momentum_backtest] pick-rows={out['picks']} sampled_dates={out.get('sampled_dates')}")
        print(f"  {'horizon':>7} {'regime':<12} {'picks':>6} {'excess_hit':>10} {'mean_excess':>12} {'abs_return':>11} {'abs_win':>8}")
        for r in out["results"]:
            print(f"  {r['horizon_days']:>6}d {r['regime']:<12} {r['picks']:>6} {r['hit_rate']*100:>9.1f}% "
                  f"{r['mean_excess_pct']:>11.2f}% {r['mean_abs_return_pct']:>10.2f}% {r['abs_win_rate']*100:>7.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
