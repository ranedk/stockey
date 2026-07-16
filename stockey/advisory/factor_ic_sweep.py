"""Factor-IC sweep + edge-drift monitor (descriptive, research/report-only) -- the "confidence instrument".

The scorer selects on a handful of signals (relative strength + a breadth floor). A faithful, FDR-gated
sweep across the UNDERUSED point-in-time data (2026-07 research: fundamentals, deal-flow, and ~30 raw
technical features) found NO robust cross-regime *selection* alpha beyond momentum beta -- so the job of
this module is NOT to discover a new stock-picker but to (a) keep MONITORING the live signals we rely on
and (b) hold a disciplined GRADUATION queue for candidate signals, so drift shows up as a warning light
long before it shows up in P&L.

It reuses the faithful `advisory.subscore_ic` machinery: regenerate the REAL technical features over full
history (`build_technical_features(rebuild=True)`, in-memory, no persist), score each candidate factor's
daily cross-sectional rank-IC vs forward benchmark-EXCESS, aggregate with a block bootstrap, and gate on
the regime split + walk-forward sign. On top of subscore_ic it adds two disciplines the monitor needs:

  * BENJAMINI-HOCHBERG FDR across the whole sweep -- testing ~30 factors at once, an uncorrected "winner"
    is expected by luck; a factor only earns `candidate` if it ALSO clears FDR (spec s7 false-discovery).
  * EDGE DRIFT -- the most recent walk-forward window's IC vs the full-sample IC, mapped to green/amber/red
    for the signals actually IN USE. Drift NEVER auto-changes a weight or threshold (s8.1: ~13 independent
    time blocks can't support prescriptive re-fitting); it only alerts, so a human/LLM can look.

Descriptive/report-only: writes `advisory_factor_ic_sweep` + a per-factor verdict; touches no authority,
weight, or threshold. CLI: `python -m advisory.factor_ic_sweep [--dry-run] [--format text|json]`.
"""
from __future__ import annotations

import argparse
import json
import os
from math import erf, sqrt
from typing import Any

import numpy as np
import pandas as pd

from advisory import subscore_ic as sic
from advisory.archetype_backtest import _load_benchmark
from advisory.technical_features import build_technical_features
from utils.db import upsert_to_db
from utils.schema_migrations import apply_schema_migration

HORIZON = int(os.getenv("FACTOR_IC_SWEEP_HORIZON", "20"))
FDR_Q = float(os.getenv("FACTOR_IC_SWEEP_FDR_Q", "0.10"))
DRIFT_WEAK_FRACTION = 0.5      # recent IC below this fraction of full IC (same sign) -> amber
DRIFT_FLAT_IC = 0.01          # |full IC| below this -> nothing to drift from ("flat")

# (column, family, a-priori sign so a real signal reads +IC, in_use)
# in_use = the system actually leans on it today -> drift is a live warning light, not just research.
FACTORS: list[tuple[str, str, int, bool]] = [
    ("rs_percentile", "momentum", +1, True),
    ("rs_vs_benchmark", "momentum", +1, False),
    ("rs_vs_sector", "momentum", +1, False),
    ("stock_ret_60d", "momentum", +1, False),
    ("stock_ret_120d", "momentum", +1, False),
    ("dist_20d_high", "proximity", +1, False),
    ("dist_50d_high", "proximity", +1, False),
    ("dist_52w_high", "proximity", +1, False),
    ("trend_persistence_20d", "trend", +1, False),
    ("trend_persistence_60d", "trend", +1, False),
    ("trend_persistence_120d", "trend", +1, False),
    ("dma_50_slope_20d_pct", "trend", +1, False),
    ("dma_150_slope_20d_pct", "trend", +1, False),
    ("atr_compression_pct", "volatility", +1, False),
    ("bb_width_rank_252d", "volatility", -1, False),
    ("range_contraction_20d_pct", "volatility", +1, False),
    ("base_depth_60d_pct", "structure", -1, False),
    ("pivot_distance_60d_pct", "structure", -1, False),
    ("higher_high_count_20d", "structure", +1, False),
    ("higher_low_count_20d", "structure", +1, False),
    ("close_location_pct", "structure", +1, False),
    ("up_down_volume_ratio_20d", "volume", +1, False),
    ("accumulation_days_20d", "volume", +1, False),
    ("distribution_days_20d", "volume", -1, False),
    ("breakout_day_volume_vs_20d", "volume", +1, False),
    ("pullback_volume_dryup_ratio_20d", "volume", +1, False),   # 2026-07 graduation candidate (needs_more_data)
    ("support_hold_rate_20d", "structure", +1, False),
    ("gap_frequency_60d", "structure", -1, False),
    ("tight_close_upper_half_60d", "structure", +1, False),
]
# derived factors computed on the panel (not raw columns)
DERIVED_SECTOR_NEUTRAL = "rs_sector_neutral"      # sector-demeaned RS (Task C: tested WORSE than raw RS)
DERIVED_VCP_COMPOSITE = "vcp_composite"           # tested: higher headline IC but degrades RS top-quintile
VCP_PARTS = ("dist_52w_high", "pullback_volume_dryup_ratio_20d", "higher_high_count_20d", "trend_persistence_120d")

TABLE_NAME = "advisory_factor_ic_sweep"
MIGRATION_ID = "20260716_advisory_factor_ic_sweep"
SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        run_date TIMESTAMPTZ NOT NULL,
        factor TEXT NOT NULL,
        family TEXT,
        horizon_days BIGINT NOT NULL,
        in_use BOOLEAN,
        n_name_days BIGINT,
        n_dates BIGINT,
        mean_ic DOUBLE PRECISION,
        ci_lo DOUBLE PRECISION,
        ci_hi DOUBLE PRECISION,
        ci_excludes_zero BOOLEAN,
        fav_ic DOUBLE PRECISION,
        unf_ic DOUBLE PRECISION,
        wf_consistent BOOLEAN,
        wf_signs TEXT,
        recent_ic DOUBLE PRECISION,
        p_value DOUBLE PRECISION,
        fdr_significant BOOLEAN,
        drift_status TEXT,
        verdict TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (run_date, factor, horizon_days)
    )
    """,
]


def ensure_table() -> None:
    apply_schema_migration(
        migration_id=MIGRATION_ID,
        statements=SCHEMA_STATEMENTS,
        owner="advisory.factor_ic_sweep",
        description="Descriptive factor rank-IC sweep vs forward benchmark-excess with FDR + edge-drift monitor.",
        metadata={"module": "advisory.factor_ic_sweep", "tables": [TABLE_NAME]},
    )


# ------------------------------------------------------------------------------------------------
# Pure helpers (unit-tested without DB/build)
# ------------------------------------------------------------------------------------------------
def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def ic_p_value(mean_ic: float, ic_std: float, n_eff: int) -> float:
    """Two-sided p-value that the mean daily-IC is zero, using effective (block-reduced) sample size."""
    if n_eff < 2 or not np.isfinite(ic_std) or ic_std <= 0 or not np.isfinite(mean_ic):
        return 1.0
    t = mean_ic / (ic_std / sqrt(n_eff))
    return float(2.0 * (1.0 - normal_cdf(abs(t))))


def benjamini_hochberg(pvalues: list[float], q: float = FDR_Q) -> list[bool]:
    """Benjamini-Hochberg step-up. Returns a bool per input p-value (original order); True = FDR-significant
    at level q. Controls the expected false-discovery rate when many factors are tested together."""
    n = len(pvalues)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: (pvalues[i] if np.isfinite(pvalues[i]) else 1.0))
    cutoff_rank = -1
    for rank, idx in enumerate(order):
        p = pvalues[idx] if np.isfinite(pvalues[idx]) else 1.0
        if p <= (rank + 1) / n * q:
            cutoff_rank = rank
    sig = [False] * n
    for rank in range(cutoff_rank + 1):
        sig[order[rank]] = True
    return sig


def drift_status(recent_ic: float, full_ic: float, *, weak: float = DRIFT_WEAK_FRACTION,
                 flat: float = DRIFT_FLAT_IC) -> str:
    """Compare the most-recent walk-forward window IC to the full-sample IC. Warning light only.
    red = sign flipped; amber = same sign but decayed below `weak` of full; green = holding; flat = no edge
    to drift from; unknown = insufficient data."""
    if not np.isfinite(recent_ic) or not np.isfinite(full_ic):
        return "unknown"
    if abs(full_ic) < flat:
        return "flat"
    if recent_ic * full_ic < 0:
        return "red"
    if abs(recent_ic) < weak * abs(full_ic):
        return "amber"
    return "green"


# ------------------------------------------------------------------------------------------------
# Panel build + factor computation
# ------------------------------------------------------------------------------------------------
def build_factor_panel(*, history_start: str | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Faithful in-memory rebuild of the real technical features over history, filtered to sampled liquid
    name-days and joined to the forward-return panel. Returns (panel, benchmark)."""
    bench = _load_benchmark()
    if bench.empty:
        return (pd.DataFrame(), bench)
    bench["date"] = pd.to_datetime(bench["date"], utc=True, errors="coerce").dt.normalize()
    days = sorted(bench["date"].dropna().unique())
    sampled = set(days[:: max(1, sic.SAMPLE_STEP_DAYS)])
    feat = build_technical_features(
        from_date=pd.Timestamp(history_start or sic.HISTORY_START, tz="UTC"),
        to_date=pd.Timestamp(days[-1]) if days else None,
        symbols=sic.load_liquid_symbols(), rebuild=True,
    )
    if feat.empty:
        return (pd.DataFrame(), bench)
    feat["date"] = pd.to_datetime(feat["asof_date"], utc=True, errors="coerce").dt.normalize()
    feat = feat[feat["date"].isin(sampled)]
    feat = feat[(feat["avg_traded_value_20d"] >= sic.MIN_TURNOVER) & feat["dma_50"].notna()].copy()
    fp = sic.load_forward_panel()
    fp["date"] = pd.to_datetime(fp["date"], utc=True, errors="coerce").dt.normalize()
    feat = feat.merge(fp, on=["symbol", "date"], how="inner")
    return (feat, bench)


def attach_derived(ex: pd.DataFrame) -> pd.DataFrame:
    """Add the two derived factors (sector-neutral RS, VCP composite) if their inputs are present."""
    if "sector_code" in ex.columns and "rs_percentile" in ex.columns:
        ex[DERIVED_SECTOR_NEUTRAL] = ex.groupby(["date", "sector_code"])["rs_percentile"].transform(
            lambda s: s - s.mean())
    parts = [c for c in VCP_PARTS if c in ex.columns]
    if len(parts) == len(VCP_PARTS):
        ex[DERIVED_VCP_COMPOSITE] = ex.groupby("date", group_keys=False).apply(
            lambda d: sum(d[c].rank(pct=True) for c in VCP_PARTS) / len(VCP_PARTS), include_groups=False)
    return ex


def compute_records(ex: pd.DataFrame, bench: pd.DataFrame, *, run_date: pd.Timestamp,
                    horizon: int = HORIZON) -> list[dict[str, Any]]:
    """One record per factor: IC + bootstrap CI + regime split + walk-forward + p-value + drift + verdict.
    FDR is applied across the whole sweep after per-factor stats are collected."""
    fav_dates = set(ex.loc[ex["bench_close"] > ex["bench_dma50"], "date"].unique())
    block = max(1, sic.BLOCK_DAYS // sic.SAMPLE_STEP_DAYS)
    registry = list(FACTORS)
    if DERIVED_SECTOR_NEUTRAL in ex.columns:
        registry.append((DERIVED_SECTOR_NEUTRAL, "momentum", +1, False))
    if DERIVED_VCP_COMPOSITE in ex.columns:
        registry.append((DERIVED_VCP_COMPOSITE, "composite", +1, False))

    rows: list[dict[str, Any]] = []
    for col, family, sign, in_use in registry:
        if col not in ex.columns:
            continue
        ic = sic.daily_ic(ex, col) * sign
        ic = ic.dropna()
        if ic.empty:
            continue
        mean_ic, lo, hi = sic.block_bootstrap_ci(ic, block=block)
        fav_ic, unf_ic, fav_n, unf_n = sic.regime_split_ic(ic, fav_dates)
        parts, wf_consistent = sic.walkforward_signs(ic)
        recent_ic = parts[-1] if parts else float("nan")
        n_dates = int(ic.notna().sum())
        n_eff = max(1, n_dates // block)
        p = ic_p_value(mean_ic, float(ic.std(ddof=1)) if n_dates > 1 else float("nan"), n_eff)
        ci_excl = bool(np.isfinite(lo) and np.isfinite(hi) and lo * hi > 0)
        rows.append({
            "run_date": run_date, "factor": col, "family": family, "horizon_days": horizon,
            "in_use": in_use, "n_name_days": int(len(ex)), "n_dates": n_dates,
            "mean_ic": round(mean_ic, 6), "ci_lo": round(lo, 6) if np.isfinite(lo) else None,
            "ci_hi": round(hi, 6) if np.isfinite(hi) else None, "ci_excludes_zero": ci_excl,
            "fav_ic": round(fav_ic, 6) if np.isfinite(fav_ic) else None,
            "unf_ic": round(unf_ic, 6) if np.isfinite(unf_ic) else None,
            "wf_consistent": bool(wf_consistent), "wf_signs": " ".join(f"{p_:+.3f}" for p_ in parts),
            "recent_ic": round(recent_ic, 6) if np.isfinite(recent_ic) else None,
            "p_value": round(p, 6), "drift_status": drift_status(recent_ic, mean_ic),
            "_fav_ic": fav_ic, "_unf_ic": unf_ic,   # scratch for verdict
        })

    fdr = benjamini_hochberg([r["p_value"] for r in rows], q=FDR_Q)
    for r, sig in zip(rows, fdr):
        r["fdr_significant"] = bool(sig)
        verdict = sic.classify_verdict(
            significant=r["ci_excludes_zero"], mean_ic=r["mean_ic"], fav_ic=r["_fav_ic"],
            unf_ic=r["_unf_ic"], wf_consistent=r["wf_consistent"], n_dates=r["n_dates"])
        # a `candidate` must also clear FDR across the whole sweep, else it is not yet trustworthy.
        if verdict == "candidate" and not r["fdr_significant"]:
            verdict = "needs_more_data"
        r["verdict"] = verdict
        r["load_ts"] = pd.Timestamp.utcnow()
        del r["_fav_ic"], r["_unf_ic"]
    rows.sort(key=lambda r: (r["mean_ic"] if r["mean_ic"] is not None else -9), reverse=True)
    return rows


def run_factor_ic_sweep(*, dry_run: bool = False, history_start: str | None = None,
                        horizon: int = HORIZON) -> dict[str, Any]:
    ex_panel, bench = build_factor_panel(history_start=history_start)
    if ex_panel.empty:
        return {"records": [], "note": "no panel"}
    ex = sic.attach_excess(ex_panel, bench, horizon)
    ex = attach_derived(ex)
    days = sorted(ex["date"].dropna().unique())
    run_date = pd.Timestamp(days[-1]).normalize() if days else pd.Timestamp.utcnow().normalize()
    records = compute_records(ex, bench, run_date=run_date, horizon=horizon)
    if records and not dry_run:
        ensure_table()
        upsert_to_db(pd.DataFrame(records), TABLE_NAME,
                     unique_keys=["run_date", "factor", "horizon_days"], timescaledb_column="run_date")
    drift_alerts = [r for r in records if r["in_use"] and r["drift_status"] in ("amber", "red")]
    return {"records": records, "run_date": str(run_date.date()), "horizon": horizon,
            "n_factors": len(records), "drift_alerts": drift_alerts,
            "fdr_survivors": [r["factor"] for r in records if r["fdr_significant"]]}


def render_report(result: dict[str, Any]) -> str:
    recs = result.get("records", [])
    if not recs:
        return f"factor-IC sweep: {result.get('note', 'no records')}"
    lines = [f"factor-IC sweep  (run_date={result.get('run_date')}, H={result.get('horizon')}d, "
             f"factors={result.get('n_factors')}, FDR q={FDR_Q})",
             f"{'factor':<32}{'fam':<11}{'IC':>8}{'CI2.5':>8}{'CI97.5':>8}"
             f"{'fav':>7}{'unf':>7}{'drift':>7}{'FDR':>4}  verdict"]
    for r in recs:
        use = "*" if r["in_use"] else " "
        fdr = "yes" if r["fdr_significant"] else " - "
        lines.append(
            f"{use}{r['factor']:<31}{(r['family'] or ''):<11}{_f(r['mean_ic']):>8}{_f(r['ci_lo']):>8}"
            f"{_f(r['ci_hi']):>8}{_f(r['fav_ic']):>7}{_f(r['unf_ic']):>7}{r['drift_status']:>7}{fdr:>4}  {r['verdict']}")
    alerts = result.get("drift_alerts", [])
    lines.append("")
    if alerts:
        lines.append("DRIFT ALERTS (in-use signals off their full-sample edge):")
        for a in alerts:
            lines.append(f"  [{a['drift_status'].upper()}] {a['factor']}: recent_ic={_f(a['recent_ic'])} "
                         f"vs full_ic={_f(a['mean_ic'])} (wf {a['wf_signs']})")
    else:
        lines.append("DRIFT: all in-use signals green (recent edge holding vs full sample).")
    surv = result.get("fdr_survivors", [])
    lines.append(f"FDR survivors (q={FDR_Q}): {surv if surv else 'NONE -- no factor beats multiple-testing luck'}")
    return "\n".join(lines)


def _f(x: Any) -> str:
    return f"{x:+.4f}" if isinstance(x, (int, float)) and x is not None and np.isfinite(x) else "   -"


def main() -> None:
    ap = argparse.ArgumentParser(description="Factor-IC sweep + edge-drift monitor (descriptive, report-only).")
    ap.add_argument("--dry-run", action="store_true", help="compute + print, do not persist")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    ap.add_argument("--history-start", default=None)
    ap.add_argument("--horizon", type=int, default=HORIZON)
    args = ap.parse_args()
    result = run_factor_ic_sweep(dry_run=args.dry_run, history_start=args.history_start, horizon=args.horizon)
    if args.format == "json":
        print(json.dumps(result, default=str, indent=2))
    else:
        print(render_report(result))


if __name__ == "__main__":
    main()
