"""Sub-score Information-Coefficient validation (T1, descriptive) -- does each hand-weighted technical
sub-score actually predict forward benchmark-EXCESS return, cross-sectionally, on real data?

The scoring function (advisory/technical_engine.py) is five capped point-accumulators -- trend (25),
structure (30), participation (20), relative-strength (15), tradability (10) -- summed to a 0-100 total.
They have never been measured. This measures each one's rank-IC against forward excess return, the
well-powered cross-sectional way (spec s3 + s8.1). It is DESCRIPTIVE ONLY: it writes a report table and
a per-sub-score verdict; it NEVER re-weights the scorer, changes a threshold, or touches authority
(prescriptive auto-reweighting is deferred -- s8.1: ~13 independent time blocks can't support it).

FAITHFUL method: regenerate the REAL persisted features over full history with the REAL builder
(advisory.technical_features.build_technical_features(rebuild=True); returns in memory, does NOT persist)
and score them with the REAL engine functions -- so the IC is of the ACTUAL sub-scores, not proxies.
For each sampled date, daily cross-sectional rank-IC = Spearman(sub_score, fwd_excess); aggregate with a
BLOCK bootstrap (overlapping forward windows autocorrelate the daily-IC series -> naive t is inflated,
s8.3). Two robustness gates decide the verdict: the REGIME split (IC when NIFTY>50DMA vs weak tape -- a
signal alive only in a rising tape is beta/timing, not selection alpha, s9) and WALK-FORWARD sign
consistency across 3 windows.

Structure/participation are archetype-dependent (base_breakout vs momentum) so both variants are scored;
trend/rs/tradability are archetype-invariant. Review/report-only. CLI: `python -m advisory.subscore_ic`.
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any

import numpy as np
import pandas as pd

from advisory import technical_engine as te
from advisory.archetype_backtest import _load_benchmark
from advisory.technical_features import build_technical_features
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration

MIN_PRICE = float(os.getenv("MISSED_MOVERS_MIN_PRICE", "30.0"))
MIN_TURNOVER = float(os.getenv("MARKET_ACTION_SCAN_MIN_TURNOVER_INR", "50000000"))
SAMPLE_STEP_DAYS = int(os.getenv("MOMENTUM_BACKTEST_SAMPLE_STEP_DAYS", "5"))
DELIST_RETURN = float(os.getenv("BACKTEST_DELIST_RETURN", "-0.5"))
HISTORY_START = os.getenv("SUBSCORE_IC_HISTORY_START", "2025-06-02")
HORIZONS = (5, 10, 20)
BOOTSTRAP_N = int(os.getenv("SUBSCORE_IC_BOOTSTRAP_N", "2000"))
BLOCK_DAYS = 20  # bootstrap block ~ one forward horizon, in calendar days (converted to sampled-date units)
MIN_NAMES_PER_DATE = 20
# a positive edge that is favorable-regime-only is beta; require the weak-tape IC to keep at least this
# fraction of the favorable-tape IC (same sign) before calling it selection alpha rather than beta.
UNFAVORABLE_RETENTION = 0.34

# archetype-invariant sub-scores + the archetype-specific pair (base breakout vs momentum)
SHARED_SUBSCORES = {"trend": te.score_trend_regime, "rs": te.score_relative_strength, "tradability": te.score_tradability}
STRUCTURE_VARIANTS = {"base": te.score_structure_quality, "momentum": te.score_structure_momentum}
PARTICIPATION_VARIANTS = {"base": te.score_participation, "momentum": te.score_participation_momentum}
SUBSCORE_COLUMNS = ["trend", "rs", "tradability", "structure_base", "structure_momentum",
                    "participation_base", "participation_momentum"]

TABLE_NAME = "advisory_subscore_ic"
MIGRATION_ID = "20260714_advisory_subscore_ic"
SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        run_date TIMESTAMPTZ NOT NULL,
        subscore TEXT NOT NULL,
        horizon_days BIGINT NOT NULL,
        n_name_days BIGINT,
        n_dates BIGINT,
        mean_ic DOUBLE PRECISION,
        ci_lo DOUBLE PRECISION,
        ci_hi DOUBLE PRECISION,
        significant BOOLEAN,
        fav_ic DOUBLE PRECISION,
        unf_ic DOUBLE PRECISION,
        fav_dates BIGINT,
        unf_dates BIGINT,
        wf_consistent BOOLEAN,
        wf_signs TEXT,
        verdict TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (run_date, subscore, horizon_days)
    )
    """,
]


def ensure_table() -> None:
    apply_schema_migration(
        migration_id=MIGRATION_ID,
        statements=SCHEMA_STATEMENTS,
        owner="advisory.subscore_ic",
        description="Descriptive sub-score rank-IC vs forward benchmark-excess (regime split + walk-forward verdict).",
        metadata={"module": "advisory.subscore_ic", "tables": [TABLE_NAME]},
    )


# ------------------------------------------------------------------------------------------------
# Pure stats helpers (unit-tested without DB/build)
# ------------------------------------------------------------------------------------------------
def daily_ic(df: pd.DataFrame, score_col: str, *, min_names: int = MIN_NAMES_PER_DATE) -> pd.Series:
    """Daily cross-sectional rank-IC = Spearman(score, excess) within each date."""
    def _ic(g: pd.DataFrame) -> float:
        s, e = g[score_col], g["excess"]
        m = s.notna() & e.notna()
        if m.sum() < min_names or s[m].nunique() < 5:
            return np.nan
        return s[m].rank().corr(e[m].rank())
    return df.groupby("date")[[score_col, "excess"]].apply(_ic).dropna()


def block_bootstrap_ci(ic: pd.Series, *, n: int = BOOTSTRAP_N, block: int = 4) -> tuple[float, float, float]:
    """Moving-block bootstrap CI for the mean of an autocorrelated daily-IC series. `block` in units of
    sampled dates. Deterministic RNG seeded from the data (scripts can't use Math.random-style entropy)."""
    vals = ic.to_numpy(dtype="float64")
    k = len(vals)
    if k == 0:
        return (float("nan"), float("nan"), float("nan"))
    if k < block + 1:
        return (float(np.mean(vals)), float("nan"), float("nan"))
    nblocks = int(np.ceil(k / block))
    starts_pool = np.arange(0, k - block + 1)
    rng = np.random.default_rng(abs(hash(tuple(np.round(vals, 6)))) % (2**32))
    means = np.empty(n)
    for i in range(n):
        starts = rng.choice(starts_pool, size=nblocks, replace=True)
        means[i] = np.concatenate([vals[s:s + block] for s in starts])[:k].mean()
    return (float(np.mean(vals)), float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5)))


def regime_split_ic(ic: pd.Series, favorable_dates: set) -> tuple[float, float, int, int]:
    """Mean daily IC on favorable (NIFTY>50DMA) vs unfavorable dates."""
    fav = ic[[d in favorable_dates for d in ic.index]]
    unf = ic[[d not in favorable_dates for d in ic.index]]
    fav_m = float(fav.mean()) if len(fav) else float("nan")
    unf_m = float(unf.mean()) if len(unf) else float("nan")
    return (fav_m, unf_m, len(fav), len(unf))


def walkforward_signs(ic: pd.Series, *, nw: int = 3) -> tuple[list[float], bool]:
    """Mean IC in nw sequential non-overlapping date windows; consistent = all same sign."""
    if len(ic) < nw * 3:
        return ([], False)
    dates = list(ic.index)
    edges = [dates[i * len(dates) // nw] for i in range(nw)] + [dates[-1] + pd.Timedelta(days=1)]
    parts = [float(ic[(ic.index >= edges[i]) & (ic.index < edges[i + 1])].mean()) for i in range(nw)]
    consistent = all(p > 0 for p in parts) or all(p < 0 for p in parts)
    return (parts, bool(consistent))


def classify_verdict(*, significant: bool, mean_ic: float, fav_ic: float, unf_ic: float,
                     wf_consistent: bool, n_dates: int) -> str:
    """Map the evidence to the roadmap's review vocabulary (candidate / benchmark_beta_not_alpha /
    do_not_relax / needs_more_data / manual_review_required). Descriptive -- no action attached."""
    if n_dates < 12 or np.isnan(mean_ic):
        return "needs_more_data"
    # robustly inverted: a significant negative edge that holds (same sign) in BOTH regimes -> the
    # sub-score ranks the WRONG way; do not lean on it (candidate for neutralizing, research-only).
    if significant and mean_ic < 0 and fav_ic < 0 and unf_ic < 0:
        return "do_not_relax"
    if not significant:
        return "needs_more_data"
    # significant positive: is it selection alpha, or just a rising-tape (beta) effect?
    if mean_ic > 0:
        holds_weak_tape = (unf_ic > 0) and (unf_ic >= UNFAVORABLE_RETENTION * fav_ic)
        if holds_weak_tape and wf_consistent:
            return "candidate"           # robust cross-regime descriptive edge
        return "benchmark_beta_not_alpha"  # edge lives in the favorable regime only
    return "manual_review_required"


# ------------------------------------------------------------------------------------------------
# Data load + faithful scoring
# ------------------------------------------------------------------------------------------------
def load_liquid_symbols() -> list[str]:
    df = sql_to_df(
        """
        WITH t AS (
          SELECT o.symbol, o.close, AVG(o.volume) OVER w20 * o.close AS turnover
          FROM nseindia_ohlcv o WHERE o.series IN ('EQ','BE')
          WINDOW w20 AS (PARTITION BY o.symbol ORDER BY o.date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING)
        )
        SELECT symbol FROM t
        GROUP BY symbol HAVING MAX(turnover) >= %(mt)s AND MAX(close) >= %(mp)s
        """,
        params={"mt": MIN_TURNOVER, "mp": MIN_PRICE},
    )
    return df["symbol"].tolist()


def load_forward_panel() -> pd.DataFrame:
    """symbol, date, adj close (return denominator), fwd5/10/20 (adj), ambiguous_ahead -- on adj_close,
    EQ+BE. Splits/bonuses neutralised (adj_close); an `ambiguous` CA flag ahead marks a row to drop."""
    return sql_to_df(
        """
        SELECT symbol, date, adj_close AS close, fwd5, fwd10, fwd20, ambiguous_ahead FROM (
          SELECT o.symbol, o.date, a.adj_close,
                 LEAD(a.adj_close, 5)  OVER w AS fwd5,
                 LEAD(a.adj_close, 10) OVER w AS fwd10,
                 LEAD(a.adj_close, 20) OVER w AS fwd20,
                 MAX(CASE WHEN a.ca_flag='ambiguous' THEN 1 ELSE 0 END)
                   OVER (PARTITION BY o.symbol ORDER BY o.date ROWS BETWEEN 1 FOLLOWING AND 20 FOLLOWING) AS ambiguous_ahead
          FROM nseindia_ohlcv o JOIN advisory_adjusted_ohlcv_daily a ON a.symbol=o.symbol AND a.date=o.date
          WHERE o.series IN ('EQ','BE')
          WINDOW w AS (PARTITION BY o.symbol ORDER BY o.date)
        ) b ORDER BY date, symbol
        """
    )


def attach_subscores(feat: pd.DataFrame) -> pd.DataFrame:
    """Attach the real engine sub-scores. Trend/RS/tradability once; structure/participation per archetype."""
    out = feat.copy()
    for name, fn in SHARED_SUBSCORES.items():
        out[name] = out.apply(fn, axis=1)
    for arch, fn in STRUCTURE_VARIANTS.items():
        out[f"structure_{arch}"] = out.apply(fn, axis=1)
    for arch, fn in PARTICIPATION_VARIANTS.items():
        out[f"participation_{arch}"] = out.apply(fn, axis=1)
    return out


def attach_excess(df: pd.DataFrame, bench: pd.DataFrame, h: int) -> pd.DataFrame:
    """Forward h-day benchmark-excess on adj_close, delist-honest, ambiguous-dropped, over the stock's window."""
    b = bench[["date", "bench_close", "bench_dma50", f"bench_fwd{h}"]].rename(columns={f"bench_fwd{h}": "bf"})
    out = df.merge(b, on="date", how="inner")
    out = out[out["bf"].notna()].copy()
    out = out[~out["ambiguous_ahead"].fillna(0).astype(bool)]
    delisted = out[f"fwd{h}"].isna()
    stock_ret = (out[f"fwd{h}"] / out["close"] - 1.0).where(~delisted, DELIST_RETURN)
    out["excess"] = stock_ret - (out["bf"] / out["bench_close"] - 1.0)
    return out


# ------------------------------------------------------------------------------------------------
# Orchestration
# ------------------------------------------------------------------------------------------------
def compute_ic_records(scored: pd.DataFrame, bench: pd.DataFrame, *, run_date: pd.Timestamp) -> list[dict[str, Any]]:
    """Pure over already-scored data: per (sub-score, horizon) IC + robustness + verdict."""
    block = max(1, BLOCK_DAYS // SAMPLE_STEP_DAYS)
    records: list[dict[str, Any]] = []
    for h in HORIZONS:
        ex = attach_excess(scored, bench, h)
        if ex.empty:
            continue
        fav_dates = set(ex.loc[ex["bench_close"] > ex["bench_dma50"], "date"].unique())
        for col in SUBSCORE_COLUMNS:
            ic = daily_ic(ex, col)
            mean_ic, lo, hi = block_bootstrap_ci(ic, block=block)
            significant = bool(not np.isnan(lo) and not np.isnan(hi) and lo * hi > 0)
            fav_ic, unf_ic, fav_n, unf_n = regime_split_ic(ic, fav_dates)
            wf, wf_consistent = walkforward_signs(ic)
            verdict = classify_verdict(significant=significant, mean_ic=mean_ic, fav_ic=fav_ic,
                                       unf_ic=unf_ic, wf_consistent=wf_consistent, n_dates=len(ic))
            records.append({
                "run_date": run_date, "subscore": col, "horizon_days": h,
                "n_name_days": int(len(ex[[col, "excess"]].dropna())), "n_dates": int(len(ic)),
                "mean_ic": round(mean_ic, 6), "ci_lo": round(lo, 6) if not np.isnan(lo) else None,
                "ci_hi": round(hi, 6) if not np.isnan(hi) else None, "significant": significant,
                "fav_ic": round(fav_ic, 6) if not np.isnan(fav_ic) else None,
                "unf_ic": round(unf_ic, 6) if not np.isnan(unf_ic) else None,
                "fav_dates": fav_n, "unf_dates": unf_n,
                "wf_consistent": wf_consistent, "wf_signs": " ".join(f"{p:+.3f}" for p in wf),
                "verdict": verdict, "load_ts": run_date,
            })
    return records


def run_subscore_ic(*, dry_run: bool = False, history_start: str | None = None) -> dict[str, Any]:
    """Build features over history, score with the real engine, measure IC, persist the report table."""
    bench = _load_benchmark()
    if bench.empty:
        return {"records": [], "note": "no benchmark"}
    bench["date"] = pd.to_datetime(bench["date"], utc=True, errors="coerce").dt.normalize()
    trading_days = sorted(bench["date"].dropna().unique())
    sampled = set(trading_days[::max(1, SAMPLE_STEP_DAYS)])

    symbols = load_liquid_symbols()
    feat = build_technical_features(
        from_date=pd.Timestamp(history_start or HISTORY_START, tz="UTC"),
        to_date=pd.Timestamp(trading_days[-1]) if trading_days else None,
        symbols=symbols, rebuild=True,
    )
    if feat.empty:
        return {"records": [], "note": "no features"}
    feat["date"] = pd.to_datetime(feat["asof_date"], utc=True, errors="coerce").dt.normalize()
    feat = feat[feat["date"].isin(sampled)].copy()
    feat = feat[(feat["avg_traded_value_20d"] >= MIN_TURNOVER) & feat["dma_50"].notna()].copy()
    scored = attach_subscores(feat)

    fp = load_forward_panel()
    fp["date"] = pd.to_datetime(fp["date"], utc=True, errors="coerce").dt.normalize()
    scored = scored.merge(fp, on=["symbol", "date"], how="inner")

    run_date = pd.Timestamp(trading_days[-1]).normalize()
    records = compute_ic_records(scored, bench, run_date=run_date)
    if records and not dry_run:
        ensure_table()
        upsert_to_db(pd.DataFrame(records), TABLE_NAME,
                     unique_keys=["run_date", "subscore", "horizon_days"], timescaledb_column="run_date")
    return {"records": records, "symbols": len(symbols), "scored_rows": int(len(scored)),
            "dates": int(scored["date"].nunique()) if not scored.empty else 0}


def format_text_report(result: dict[str, Any]) -> str:
    recs = result.get("records", [])
    lines = [f"sub-score IC  (liquid names={result.get('symbols')}, scored_rows={result.get('scored_rows')}, "
             f"dates={result.get('dates')})",
             "verdicts: candidate=robust cross-regime edge | benchmark_beta_not_alpha=favorable-tape only | "
             "do_not_relax=robustly INVERTED | needs_more_data=no reliable signal"]
    for h in HORIZONS:
        hs = [r for r in recs if r["horizon_days"] == h]
        if not hs:
            continue
        lines.append(f"\n== horizon {h}d ==")
        lines.append(f"{'subscore':<22}{'meanIC':>9}{'CI2.5':>9}{'CI97.5':>9}{'fav':>9}{'unf':>9}  {'verdict'}")
        for r in hs:
            lines.append(f"{r['subscore']:<22}{r['mean_ic']:>9.4f}"
                         f"{(r['ci_lo'] if r['ci_lo'] is not None else float('nan')):>9.4f}"
                         f"{(r['ci_hi'] if r['ci_hi'] is not None else float('nan')):>9.4f}"
                         f"{(r['fav_ic'] if r['fav_ic'] is not None else float('nan')):>9.4f}"
                         f"{(r['unf_ic'] if r['unf_ic'] is not None else float('nan')):>9.4f}  {r['verdict']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Descriptive sub-score rank-IC vs forward benchmark-excess (report-only).")
    parser.add_argument("--dry-run", action="store_true", help="Compute and print, but do not persist.")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--history-start", default=None, help="YYYY-MM-DD; default SUBSCORE_IC_HISTORY_START.")
    args = parser.parse_args(argv)
    result = run_subscore_ic(dry_run=bool(args.dry_run), history_start=args.history_start)
    if args.format == "json":
        print(json.dumps(result, indent=2, default=str))
    else:
        print(format_text_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
