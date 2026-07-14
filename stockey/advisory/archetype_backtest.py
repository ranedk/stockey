"""Walk-forward, multi-archetype backtest -- the evidence engine for conditional allocation.

The momentum backtest found a striking regime-conditional edge in ONE 13-month sample; acting
on that would be the overfitting the spec forbids. This harness makes the finding trustworthy:

1. **Walk-forward windows** -- split the timeline into sequential, non-overlapping windows and
   measure the regime-conditional edge in each. A directional finding is only trustworthy if it
   repeats across independent windows; a one-window artifact is caught here.
2. **Both archetypes on equal footing** -- momentum-continuation AND base-breakout, same
   regime tag, same horizons, same metrics -- so the conditional-allocation layer can compare
   which sleeve to lean on in which regime from like-for-like evidence.

Pure price, point-in-time (only pre-decision data picks; forward windows are the outcome), no
LLM. Sampled every SAMPLE_STEP_DAYS to cut overlapping-window autocorrelation. Review-only;
writes advisory_archetype_backtest, never trades or changes config.
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any

import pandas as pd

from advisory.cost_model import describe as describe_cost
from advisory.cost_model import round_trip_cost_fraction
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration

TABLE_NAME = "advisory_archetype_backtest"
MIGRATION_ID = "20260712_advisory_archetype_backtest_v2"
SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        run_date TIMESTAMPTZ NOT NULL,
        archetype TEXT NOT NULL,
        regime_definition TEXT NOT NULL,
        window_label TEXT NOT NULL,
        horizon_days BIGINT NOT NULL,
        regime TEXT NOT NULL,
        picks BIGINT,
        hit_rate DOUBLE PRECISION,
        mean_excess_pct DOUBLE PRECISION,
        mean_abs_return_pct DOUBLE PRECISION,
        abs_win_rate DOUBLE PRECISION,
        expectancy_pct DOUBLE PRECISION,
        load_ts TIMESTAMPTZ,
        UNIQUE (run_date, archetype, regime_definition, window_label, horizon_days, regime)
    )
    """,
]

# Alternative index-regime definitions -- if "deploy strength when weak" only holds under the
# 50DMA level cut it is fragile; if it holds across level, longer-term level, and trend
# direction, it is a robust deployment-timing signal.
REGIME_DEFINITIONS = {
    "above_50dma": lambda r: "favorable" if pd.notna(r["bench_dma50"]) and r["bench_close"] > r["bench_dma50"] else "unfavorable",
    "above_200dma": lambda r: "favorable" if pd.notna(r["bench_dma200"]) and r["bench_close"] > r["bench_dma200"] else "unfavorable",
    "dma50_rising": lambda r: "favorable" if pd.notna(r["bench_dma50_slope"]) and r["bench_dma50_slope"] > 0 else "unfavorable",
}

MIN_PRICE = float(os.getenv("MISSED_MOVERS_MIN_PRICE", "30.0"))
MIN_TURNOVER = float(os.getenv("MARKET_ACTION_SCAN_MIN_TURNOVER_INR", "50000000"))
SAMPLE_STEP_DAYS = int(os.getenv("MOMENTUM_BACKTEST_SAMPLE_STEP_DAYS", "5"))
N_WINDOWS = int(os.getenv("ARCHETYPE_BACKTEST_WINDOWS", "3"))
# A pick whose forward label SHOULD have matured (its horizon date exists in the calendar) but has no
# forward close = the name stopped trading (delist/halt). Silently dropping these biases the edge UP
# (they are disproportionately losers). Include them at a conservative assumed return instead, and
# count them. Not a wipeout prior (many are mergers/relistings); env-tunable.
DELIST_RETURN = float(os.getenv("BACKTEST_DELIST_RETURN", "-0.5"))
# NB: splits/bonuses handled by reading adj_close (advisory.price_adjustment); only `ambiguous` CA flags
# (unsnappable steps = probable data errors) are excluded. No single-day-step guard needed.
HORIZONS = (5, 10, 20)

# Each archetype's admission filter (the WHERE fragment over the `eq` CTE columns below).
ARCHETYPE_FILTERS = {
    # multi-day grind to new highs, still trending
    "momentum": "(close / NULLIF(close_10ago, 0) - 1) * 100 >= 15 AND close > dma20 AND close >= max252 * 0.90",
    # single-day breakout of the 20-day high on volume (the base-breakout population)
    "breakout": "close > max20 AND volume >= 2.0 * avgvol20 AND close >= max252 * 0.80",
}


def ensure_table() -> None:
    apply_schema_migration(
        migration_id=MIGRATION_ID,
        description="Walk-forward multi-archetype backtest results (regime-conditional forward returns).",
        statements=SCHEMA_STATEMENTS,
        metadata={"module": "advisory.archetype_backtest", "tables": [TABLE_NAME]},
    )


def _load_picks(archetype: str) -> pd.DataFrame:
    where = ARCHETYPE_FILTERS[archetype]
    return sql_to_df(
        f"""
        WITH base AS (
            -- adj_close (advisory.price_adjustment): splits/bonuses neutralised in BOTH the trailing
            -- archetype filter (max252/dma20/close_10ago) AND the forward return. EQ+BE continuity;
            -- turnover/min-price on raw price (split-invariant); `ambiguous` CA flag = probable data error.
            SELECT o.symbol, o.date, a.adj_close AS close, o.close AS raw_close, o.volume,
                   LAG(a.adj_close, 10) OVER w AS close_10ago,
                   MAX(a.adj_close) OVER w20  AS max20,
                   AVG(a.adj_close) OVER w20  AS dma20,
                   MAX(a.adj_close) OVER w252 AS max252,
                   AVG(o.volume)    OVER w20  AS avgvol20,
                   LEAD(a.adj_close, 5)  OVER w AS fwd5,
                   LEAD(a.adj_close, 10) OVER w AS fwd10,
                   LEAD(a.adj_close, 20) OVER w AS fwd20,
                   COUNT(*) OVER w252 AS nprev,
                   MAX(CASE WHEN a.ca_flag = 'ambiguous' THEN 1 ELSE 0 END) OVER wfwd AS ambiguous_ahead
            FROM nseindia_ohlcv o
            JOIN advisory_adjusted_ohlcv_daily a ON a.symbol = o.symbol AND a.date = o.date
            WHERE o.series IN ('EQ', 'BE')
            WINDOW w AS (PARTITION BY o.symbol ORDER BY o.date),
                   w20 AS (PARTITION BY o.symbol ORDER BY o.date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING),
                   w252 AS (PARTITION BY o.symbol ORDER BY o.date ROWS BETWEEN 252 PRECEDING AND 1 PRECEDING),
                   wfwd AS (PARTITION BY o.symbol ORDER BY o.date ROWS BETWEEN 1 FOLLOWING AND 20 FOLLOWING)
        )
        SELECT symbol, date, close, avgvol20 * raw_close AS avg_turnover_inr, fwd5, fwd10, fwd20, ambiguous_ahead
        FROM base
        WHERE close_10ago IS NOT NULL AND dma20 IS NOT NULL AND max20 IS NOT NULL AND max252 IS NOT NULL
          AND nprev >= 60 AND raw_close >= %(min_price)s AND avgvol20 * raw_close >= %(min_turnover)s
          AND {where}
        ORDER BY date, symbol
        """,
        params={"min_price": MIN_PRICE, "min_turnover": MIN_TURNOVER},
    )


def _load_benchmark() -> pd.DataFrame:
    return sql_to_df(
        """
        -- NIFTY 50, priority-coalesced across sources by date (they agree exactly on overlaps): primary
        -- nseindia_indices, then dhan's NIFTY INDEX daily, then dhan NIFTY intraday last-bar. nseindia is
        -- missing some real trading days (the Mar-2026 crash week); the dhan sources fill some. Any day
        -- still missing in ALL of them is a genuine data hole (needs an NSE `ind_close` re-fetch/parse).
        WITH nse AS (
            SELECT date::date AS d, close FROM nseindia_indices WHERE index_name ILIKE 'nifty 50'
        ),
        dhan_d AS (
            SELECT date::date AS d, close FROM dhan_ohlcv_daily WHERE ticker = 'NIFTY' AND instrument = 'INDEX'
        ),
        dhan_i AS (
            SELECT timestamp::date AS d, (array_agg(close ORDER BY timestamp DESC))[1] AS close
            FROM dhan_ohlcv_intraday WHERE ticker = 'NIFTY' AND exchange_segment = 'IDX_I' GROUP BY 1
        ),
        combined AS (
            SELECT d, close FROM nse
            UNION ALL
            SELECT d, close FROM dhan_d WHERE d NOT IN (SELECT d FROM nse)
            UNION ALL
            SELECT d, close FROM dhan_i WHERE d NOT IN (SELECT d FROM nse) AND d NOT IN (SELECT d FROM dhan_d)
        )
        SELECT d AS date, close AS bench_close,
               AVG(close) OVER (ORDER BY d ROWS BETWEEN 50 PRECEDING AND 1 PRECEDING) AS bench_dma50,
               AVG(close) OVER (ORDER BY d ROWS BETWEEN 200 PRECEDING AND 1 PRECEDING) AS bench_dma200,
               LEAD(close, 5)  OVER (ORDER BY d) AS bench_fwd5,
               LEAD(close, 10) OVER (ORDER BY d) AS bench_fwd10,
               LEAD(close, 20) OVER (ORDER BY d) AS bench_fwd20
        FROM combined ORDER BY d
        """
    )


def _segment_metrics(seg: pd.DataFrame, h: int) -> dict[str, Any]:
    wins = seg[seg["excess"] > 0]["excess"]
    losses = seg[seg["excess"] <= 0]["excess"]
    hit = round(float(len(wins)) / len(seg), 4)
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(losses.mean()) if len(losses) else 0.0
    return {
        "picks": int(len(seg)), "hit_rate": hit,
        "mean_excess_pct": round(float(seg["excess"].mean()) * 100, 3),
        "mean_abs_return_pct": round(float(seg["abs_ret"].mean()) * 100, 3),
        "abs_win_rate": round(float((seg["abs_ret"] > 0).mean()), 4),
        "expectancy_pct": round((hit * avg_win + (1 - hit) * avg_loss) * 100, 3),
        "delisted": int(seg["_delisted"].sum()) if "_delisted" in seg else 0,
        "mean_cost_pct": round(float(seg["cost"].mean()) * 100, 3) if "cost" in seg else 0.0,
    }


def run_backtest(*, n_windows: int | None = None, dry_run: bool = False) -> dict[str, Any]:
    ensure_table()
    n_windows = int(n_windows if n_windows is not None else N_WINDOWS)
    bench = _load_benchmark()
    if bench.empty:
        return {"results": []}
    bench["date"] = pd.to_datetime(bench["date"], utc=True, errors="coerce").dt.normalize()
    bench = bench.sort_values("date").reset_index(drop=True)
    bench["bench_dma50_slope"] = bench["bench_dma50"] - bench["bench_dma50"].shift(20)  # 20d trend of the 50DMA
    trading_days = sorted(bench["date"].dropna().unique())
    sampled = trading_days[::max(1, SAMPLE_STEP_DAYS)]
    # sequential, non-overlapping walk-forward windows over the sampled decision dates
    win_edges = [sampled[i * len(sampled) // n_windows] for i in range(n_windows)] + [sampled[-1] + pd.Timedelta(days=1)]
    # maturity map: decision date -> the h-th trading day after it (NaT if it runs past our data). A
    # missing forward close is a DELISTING only when the label should have matured; otherwise it is
    # merely immature (correctly excluded, not a survivorship drop).
    pos = {d: i for i, d in enumerate(trading_days)}
    mat_map = {h: {d: (trading_days[i + h] if i + h < len(trading_days) else pd.NaT) for d, i in pos.items()} for h in HORIZONS}
    now = pd.Timestamp.utcnow()
    rows: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for archetype in ARCHETYPE_FILTERS:
        picks = _load_picks(archetype)
        if picks.empty:
            continue
        picks["date"] = pd.to_datetime(picks["date"], utc=True, errors="coerce").dt.normalize()
        picks = picks[picks["date"].isin(set(sampled))]
        df = picks.merge(bench, on="date", how="inner")
        if df.empty:
            continue
        for defn, fn in REGIME_DEFINITIONS.items():
            df["regime"] = df.apply(fn, axis=1)
            for wi in range(n_windows):
                lo, hi = win_edges[wi], win_edges[wi + 1]
                wdf = df[(df["date"] >= lo) & (df["date"] < hi)]
                label = f"W{wi + 1}:{pd.Timestamp(lo).date()}"
                for h in HORIZONS:
                    sub = wdf[wdf[f"bench_fwd{h}"].notna()].copy()  # benchmark forward must be mature
                    if sub.empty:
                        continue
                    mat = sub["date"].map(mat_map[h])
                    missing = sub[f"fwd{h}"].isna()
                    # immature: no forward close AND the horizon runs past our data -> correctly excluded
                    sub = sub[~(missing & mat.isna())].copy()
                    if sub.empty:
                        continue
                    # splits/bonuses are already neutralised (returns on adj_close); drop only an
                    # `ambiguous` CA flag in the forward window (unsnappable step = probable data error).
                    if "ambiguous_ahead" in sub.columns:
                        sub = sub[~sub["ambiguous_ahead"].fillna(0).astype(bool)].copy()
                        if sub.empty:
                            continue
                    # any remaining missing forward close = matured-but-gone = delisting/halt
                    sub["_delisted"] = sub[f"fwd{h}"].isna()
                    sub["cost"] = round_trip_cost_fraction(sub["avg_turnover_inr"].to_numpy(dtype="float64"))
                    raw_ret = (sub[f"fwd{h}"] / sub["close"] - 1.0).where(~sub["_delisted"], DELIST_RETURN)
                    sub["abs_ret"] = raw_ret - sub["cost"]
                    sub["excess"] = sub["abs_ret"] - (sub[f"bench_fwd{h}"] / sub["bench_close"] - 1.0)
                    for regime in ("favorable", "unfavorable"):
                        seg = sub[sub["regime"] == regime]
                        if len(seg) < 10:  # too thin for a window/regime cell to be meaningful
                            continue
                        m = _segment_metrics(seg, h)
                        rec = {"run_date": now.normalize(), "archetype": archetype, "regime_definition": defn,
                               "window_label": label, "horizon_days": h, "regime": regime,
                               **{k: v for k, v in m.items() if k not in ("delisted", "mean_cost_pct")}, "load_ts": now}
                        rows.append(rec)
                        results.append({**{k: rec[k] for k in ("archetype", "regime_definition", "window_label", "horizon_days", "regime", "picks", "mean_abs_return_pct", "abs_win_rate")},
                                        "delisted": m["delisted"], "mean_cost_pct": m["mean_cost_pct"]})
    if rows and not dry_run:
        upsert_to_db(pd.DataFrame(rows), TABLE_NAME, unique_keys=["run_date", "archetype", "regime_definition", "window_label", "horizon_days", "regime"], timescaledb_column="run_date")
    return {"n_windows": n_windows, "results": results, "cost_model": describe_cost(),
            "delisted_total": sum(r.get("delisted", 0) for r in results)}


def _consistency(results: list[dict[str, Any]], archetype: str, horizon: int, regime_definition: str) -> dict[str, Any]:
    """Is the favorable-minus-unfavorable abs-return edge the same SIGN across all windows?"""
    by_win: dict[str, dict[str, float]] = {}
    for r in results:
        if r["archetype"] == archetype and r["horizon_days"] == horizon and r.get("regime_definition") == regime_definition:
            by_win.setdefault(r["window_label"], {})[r["regime"]] = r["mean_abs_return_pct"]
    edges = {w: v.get("favorable", 0.0) - v.get("unfavorable", 0.0) for w, v in by_win.items() if "favorable" in v and "unfavorable" in v}
    signs = {(-1 if e < 0 else 1) for e in edges.values()}
    return {"windows": len(edges), "edges": edges, "consistent": len(signs) == 1 and len(edges) >= 2,
            "mean_edge": round(sum(edges.values()) / len(edges), 2) if edges else None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Walk-forward multi-archetype regime-conditional backtest.")
    parser.add_argument("--windows", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)
    out = run_backtest(n_windows=args.windows, dry_run=bool(args.dry_run))
    if args.format == "json":
        print(json.dumps(out, indent=2, default=str))
        return 0
    cm = out.get("cost_model", {})
    print(f"[archetype_backtest] windows={out['n_windows']}  robustness of the 'buy strength when weak' edge")
    print(f"  cost=name-specific (statutory {cm.get('statutory_bps')}bps + turnover-spread + size-impact @ "
          f"pos={cm.get('position_size_inr')}); delisted picks filled at {DELIST_RETURN:+.0%}, "
          f"total={out.get('delisted_total', 0)}")
    print("  (fav-minus-unfav abs-return edge; NEGATIVE = strength does WORSE in favorable/strong markets)")
    for archetype in ARCHETYPE_FILTERS:
        print(f"\n  == {archetype} ==")
        print(f"    {'regime_definition':<16} {'horizon':>7} {'mean_edge':>10} {'verdict':>14}")
        for defn in REGIME_DEFINITIONS:
            for h in HORIZONS:
                c = _consistency(out["results"], archetype, h, defn)
                if c["windows"] >= 2:
                    verdict = "CONSISTENT" if c["consistent"] else "inconsistent"
                    print(f"    {defn:<16} {h:>6}d {c['mean_edge']:>+9.2f}% {verdict:>14}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
