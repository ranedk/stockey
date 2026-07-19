"""Strategy lab -- multi-period, after-cost robustness filter over the registry selectors (report-only).

Runs each selector's top-K book across N non-overlapping holdout folds, after cost, on the dhan-EQUITY
universe, and decides ENABLED vs PARKED by whether it beats the incumbent raw-RS baseline ROBUSTLY (not just
in one lucky window -- the lesson of this whole system: a single-period win is period-luck). Writes the
verdict into config/strategy_registry.json (so the live pipeline picks up only the enabled selector) and
persists advisory_strategy_backtest. Report/research-only; no broker.

Filter (deliberately strict, given ~13-block power): a candidate is ENABLED only if it beats the baseline
on net Sharpe in EVERY fold (or >=2/3 folds and never worse by >0.2 Sharpe in any fold). Otherwise PARKED
with notes='needs_more_data' -- coded, re-tested automatically on the next run when more data has arrived.
"""
from __future__ import annotations

import argparse
import json
from typing import Any

import numpy as np
import pandas as pd

from advisory import strategy_registry as sr
from utils.db import upsert_to_db
from utils.schema_migrations import apply_schema_migration

REBALANCE, K, RT_COST = 21, 30, 0.0045      # monthly rebalance, top-30, ~45bps round trip per replaced slot
RF_D = (1.06) ** (1 / 252) - 1
N_FOLDS = 3
WARMUP_START = "2022-06-01"

TABLE_NAME = "advisory_strategy_backtest"
MIGRATION_ID = "20260719_advisory_strategy_backtest"
SCHEMA = [f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        run_date TIMESTAMPTZ NOT NULL, strategy TEXT NOT NULL, tier TEXT, fold TEXT NOT NULL,
        cagr DOUBLE PRECISION, vol DOUBLE PRECISION, sharpe DOUBLE PRECISION, maxdd DOUBLE PRECISION,
        baseline_sharpe DOUBLE PRECISION, delta_sharpe DOUBLE PRECISION, status TEXT, verdict TEXT,
        load_ts TIMESTAMPTZ, UNIQUE (run_date, strategy, fold)
    )"""]


def ensure_table() -> None:
    apply_schema_migration(migration_id=MIGRATION_ID, statements=SCHEMA, owner="advisory.strategy_lab",
                           description="Multi-period after-cost robustness filter over registry selectors.",
                           metadata={"module": "advisory.strategy_lab", "tables": [TABLE_NAME]})


# ------------------------------------------------------------------------------------------------
# pure backtest + metric + decision helpers (unit-tested)
# ------------------------------------------------------------------------------------------------
def book_returns(ret_wide: pd.DataFrame, score_wide: pd.DataFrame, *, rebalance: int = REBALANCE,
                 k: int = K, rt_cost: float = RT_COST) -> pd.Series:
    dates = list(ret_wide.index)
    reb = set(range(0, len(dates), rebalance))
    prev: set = set()
    out = []
    for i, d in enumerate(dates):
        if i in reb:
            row = score_wide.loc[d].dropna()
            pick = set(row.nlargest(k).index) if len(row) >= k else set(row.index)
            cost = (len(pick - prev) / max(1, k)) * rt_cost if prev else rt_cost
            prev = pick
        else:
            cost = 0.0
        r = ret_wide.loc[d, list(prev)].mean() if prev else 0.0
        out.append((0.0 if not np.isfinite(r) else float(r)) - cost)
    return pd.Series(out, index=dates)


def curve_stats(r: pd.Series) -> dict[str, float]:
    r = r.dropna()
    if len(r) < 40:
        return {}
    eq = (1 + r).cumprod(); yrs = len(r) / 252
    cagr = float(eq.iloc[-1] ** (1 / yrs) - 1)
    vol = float(r.std() * np.sqrt(252))
    return {"cagr": cagr, "vol": vol, "sharpe": float((r.mean() * 252 - 0.06) / vol) if vol > 0 else float("nan"),
            "maxdd": float((eq / eq.cummax() - 1).min())}


def decide_status(deltas: list[float]) -> tuple[str, str]:
    """ENABLED only if it beats the baseline robustly across folds; else PARKED (needs_more_data)."""
    d = [x for x in deltas if np.isfinite(x)]
    if len(d) < 2:
        return "parked", "needs_more_data: too few folds"
    beats = sum(x > 0 for x in d)
    if all(x >= 0 for x in d):
        return "enabled", f"beats baseline in all {len(d)} folds (mean +{np.mean(d):.2f} Sharpe)"
    if beats >= 2 and min(d) >= -0.2 and np.mean(d) > 0:
        return "enabled", f"beats baseline in {beats}/{len(d)} folds, never worse by >0.2 (mean +{np.mean(d):.2f})"
    return "parked", f"needs_more_data: not robust (folds {['%+.2f' % x for x in d]})"


# ------------------------------------------------------------------------------------------------
# run
# ------------------------------------------------------------------------------------------------
def _score_wide(panel: pd.DataFrame, strat: sr.Strategy) -> pd.DataFrame:
    liq = panel[panel["liquid"]]
    s = liq.groupby("date").apply(lambda cs: strat.score(cs.set_index("symbol")), include_groups=False)
    return s.unstack()          # (date, symbol) -> date x symbol wide


def run_lab(*, dry_run: bool = False) -> dict[str, Any]:
    panel = sr.build_panel(start="2021-01-01")
    panel = panel.dropna(subset=["ret252", "vol252"])
    ret_wide = panel.pivot_table(index="date", columns="symbol", values="ret1")
    start = ret_wide.index[ret_wide.index >= pd.Timestamp(WARMUP_START, tz="UTC")][0]
    ret_wide = ret_wide.loc[start:]
    fold_edges = np.array_split(np.array(ret_wide.index), N_FOLDS)
    fold_ranges = [(pd.Timestamp(f[0]), pd.Timestamp(f[-1])) for f in fold_edges]

    strategies = sr.load_registry()
    scores = {s.name: _score_wide(panel, s).reindex(ret_wide.index) for s in strategies}
    books = {name: book_returns(ret_wide, sw) for name, sw in scores.items()}
    base_fold_sharpe = [curve_stats(books["raw_rs"].loc[a:b]).get("sharpe", float("nan")) for a, b in fold_ranges]

    records, run_date = [], pd.Timestamp(ret_wide.index[-1]).normalize()
    for s in strategies:
        deltas, fold_rows = [], []
        for (a, b), bsh in zip(fold_ranges, base_fold_sharpe):
            st = curve_stats(books[s.name].loc[a:b])
            dsh = (st.get("sharpe", float("nan")) - bsh) if s.name != "raw_rs" else 0.0
            deltas.append(dsh)
            fold_rows.append((f"{a.date()}..{b.date()}", st, bsh, dsh))
        if s.name == "raw_rs":
            s.status, verdict = "baseline", "incumbent"
        else:
            s.status, verdict = decide_status(deltas)
            s.notes = verdict
        s.verdict = {"fold_delta_sharpe": [round(x, 3) for x in deltas], "decision": verdict,
                     "overall": curve_stats(books[s.name])}
        for fname, st, bsh, dsh in fold_rows:
            records.append({"run_date": run_date, "strategy": s.name, "tier": s.tier, "fold": fname,
                            "cagr": st.get("cagr"), "vol": st.get("vol"), "sharpe": st.get("sharpe"),
                            "maxdd": st.get("maxdd"), "baseline_sharpe": bsh, "delta_sharpe": dsh,
                            "status": s.status, "verdict": verdict, "load_ts": pd.Timestamp.utcnow()})

    if not dry_run:
        sr.save_registry(strategies, meta={"run_date": str(run_date.date()), "rebalance": REBALANCE, "top_k": K,
                                           "folds": [f"{a.date()}..{b.date()}" for a, b in fold_ranges],
                                           "filter": "beats raw_rs baseline Sharpe robustly across folds"})
        ensure_table()
        upsert_to_db(pd.DataFrame(records), TABLE_NAME, unique_keys=["run_date", "strategy", "fold"],
                     timescaledb_column="run_date")
    return {"strategies": strategies, "records": records, "fold_ranges": fold_ranges,
            "enabled": [s.name for s in strategies if s.status == "enabled"],
            "parked": [s.name for s in strategies if s.status == "parked"]}


def format_report(r: dict[str, Any]) -> str:
    L = ["=" * 88, "STRATEGY LAB -- multi-period after-cost robustness filter (report-only)", "=" * 88,
         f"folds: {[f'{a.date()}..{b.date()}' for a,b in r['fold_ranges']]}   (top-{K}, monthly, ~45bps)"]
    L.append(f"{'strategy':<20}{'tier':>5}{'status':>10}  {'fold Sharpe vs baseline':<34}overall Sh/DD")
    for s in r["strategies"]:
        v = s.verdict
        fold_d = " ".join(f"{x:+.2f}" for x in v.get("fold_delta_sharpe", []))
        ov = v.get("overall", {})
        tag = "BASELINE" if s.status == "baseline" else s.status.upper()
        L.append(f"{s.name:<20}{s.tier:>5}{tag:>10}  {fold_d:<34}{ov.get('sharpe',float('nan')):>5.2f}/{ov.get('maxdd',float('nan'))*100:>5.0f}%")
    L.append("")
    L.append(f"ENABLED (wired into the recommendation pipeline): {r['enabled'] or 'NONE (pipeline keeps raw_rs baseline)'}")
    L.append(f"PARKED  (coded, re-tested when more data arrives): {r['parked'] or 'none'}")
    for s in r["strategies"]:
        if s.status != "baseline":
            L.append(f"  - {s.name}: {s.notes}")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description="Strategy lab robustness filter (report-only).")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args()
    r = run_lab(dry_run=args.dry_run)
    if args.format == "json":
        print(json.dumps({"enabled": r["enabled"], "parked": r["parked"],
                          "strategies": [{"name": s.name, "status": s.status, "verdict": s.verdict} for s in r["strategies"]]},
                         default=str, indent=2))
    else:
        print(format_report(r))


if __name__ == "__main__":
    main()
