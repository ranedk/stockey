"""Regime-sized shadow ledger (T2 evidence) -- does the risk/timing layer actually help?

The T1 finding: entry-selection alpha is weak and REGIME-conditional (works in a rising tape, ~0/inverts
in a weak one). So the leverage is not-losing: vol-target sizing + a pre-committed crash floor that cuts
exposure when NIFTY is below its MA. This ledger MEASURES whether that layer improves outcomes BEFORE it
is ever allowed to bind a real trade -- the whole point of shadowing.

It reads the paper decision loop's already-evaluated trades (`advisory_paper_decision_loop`), which persist
per pick the vol-target `weight_pct`, the `floor_multiplier` SEPARATELY, and the cost-net `net_return` and
NIFTY `benchmark_return`. So the same RS pick stream can be re-booked under three sizing policies purely as
analysis (no recompute, no coupling to the live engine):

  * equal_weight      -- 1/N, fully deployed (naive baseline: no sizing, no regime).
  * voltarget_nofloor -- vol-target weights, always fully deployed (isolates sizing, floor removed).
  * regime_sized      -- vol-target x crash floor (the T2 policy: de-risk when the tape is weak).

Uninvested capital earns 0 (cash) -- so the floor's de-risking actually shows up as muted returns and
drawdown in a weak tape, instead of being normalised away. Reported vs buy-and-hold NIFTY, net of cost.
Overlapping decision dates => distributional stats (mean/vol/excess, §8.3, not a compounded curve); a
separate NON-overlapping (spaced by hold_days) equity curve gives an honest max-drawdown. Research/report-
only: writes one table, no broker authority, changes no sizing that binds. CLI: `python -m advisory.regime_shadow_ledger`.
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

SOURCE_TABLE = "advisory_paper_decision_loop"
TABLE_NAME = "advisory_regime_shadow_ledger"
MIGRATION_ID = "20260714_advisory_regime_shadow_ledger"
SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        run_date TIMESTAMPTZ NOT NULL,
        policy TEXT NOT NULL,
        hold_days BIGINT NOT NULL,
        n_dates BIGINT,
        n_trades BIGINT,
        mean_return_pct DOUBLE PRECISION,
        vol_pct DOUBLE PRECISION,
        return_vol_ratio DOUBLE PRECISION,
        mean_excess_pct DOUBLE PRECISION,
        pct_dates_positive DOUBLE PRECISION,
        worst_date_pct DOUBLE PRECISION,
        avg_deployment_pct DOUBLE PRECISION,
        nonoverlap_points BIGINT,
        nonoverlap_total_return_pct DOUBLE PRECISION,
        nonoverlap_maxdd_pct DOUBLE PRECISION,
        nonoverlap_vol_pct DOUBLE PRECISION,
        verdict TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (run_date, policy, hold_days)
    )
    """,
]

# NIFTY buy-and-hold is booked as its own policy for the north-star comparison. regime_sized_breadth uses
# the market-breadth floor (advisory.market_breadth) instead of the NIFTY-50DMA floor -- measured to
# dominate it (same-or-better drawdown, higher return) because breadth catches narrow rallies NIFTY misses.
POLICIES = ("equal_weight", "voltarget_nofloor", "regime_sized", "regime_sized_breadth", "nifty_buy_hold")


def ensure_table() -> None:
    apply_schema_migration(
        migration_id=MIGRATION_ID,
        statements=SCHEMA_STATEMENTS,
        owner="advisory.regime_shadow_ledger",
        description="Shadow book: RS picks re-sized under equal-weight / vol-target / vol-target+crash-floor vs NIFTY.",
        metadata={"tables": [TABLE_NAME], "source": SOURCE_TABLE, "authority": "research_only"},
    )


# -------------------------------------------------------------------------------------------------
# Objective-scored floor selection: let the DATA pick the floor config so no return/drawdown preference
# is hand-set. Objective (pre-committed): MAXIMISE compounded growth SUBJECT TO a coarse ruin-guard (a
# wide max-drawdown cap, "never risk ruin", not an optimisation knob). On a sample whose one crash
# recovered this favours a LIGHT floor; as worse tails accrue the SAME objective tightens it automatically.
# -------------------------------------------------------------------------------------------------
RUIN_GUARD_MAXDD = float(os.getenv("SHADOW_LEDGER_RUIN_GUARD_MAXDD", "-0.20"))  # coarse survival cap (fraction)
# floor configs to sweep: (signal, breadth_threshold, floor_exposure). 'none' = always fully deployed.
FLOOR_SWEEP = (
    [("none", None, 1.0)]
    + [("nifty", None, e) for e in (0.30, 0.50, 0.70)]
    + [("breadth", t, e) for t in (0.45, 0.50, 0.55) for e in (0.30, 0.50, 0.70)]
)

FLOOR_CONFIG_TABLE = "advisory_regime_floor_config"
FLOOR_CONFIG_MIGRATION_ID = "20260714_advisory_regime_floor_config"
FLOOR_CONFIG_SCHEMA = [
    f"""
    CREATE TABLE IF NOT EXISTS {FLOOR_CONFIG_TABLE} (
        run_date TIMESTAMPTZ NOT NULL,
        hold_days BIGINT NOT NULL,
        objective TEXT,
        ruin_guard_maxdd_pct DOUBLE PRECISION,
        selected_signal TEXT,
        selected_threshold DOUBLE PRECISION,
        selected_exposure DOUBLE PRECISION,
        growth_pct DOUBLE PRECISION,
        maxdd_pct DOUBLE PRECISION,
        deploy_pct DOUBLE PRECISION,
        n_passing BIGINT,
        n_configs BIGINT,
        robust BOOLEAN,
        load_ts TIMESTAMPTZ,
        UNIQUE (run_date, hold_days)
    )
    """,
]


def ensure_floor_config_table() -> None:
    apply_schema_migration(
        migration_id=FLOOR_CONFIG_MIGRATION_ID,
        statements=FLOOR_CONFIG_SCHEMA,
        owner="advisory.regime_shadow_ledger",
        description="Data-selected crash-floor config (max growth s.t. a coarse ruin-guard maxDD cap).",
        metadata={"tables": [FLOOR_CONFIG_TABLE], "authority": "research_only"},
    )


def _load_breadth_series() -> pd.Series | None:
    """Date-indexed % of the liquid universe above its 50DMA (advisory.market_breadth), or None."""
    try:
        from advisory.market_breadth import load_breadth
        br = load_breadth()
        return None if br.empty else br.set_index("date")["pct_above_50"]
    except Exception:
        return None


def _breadth_floor_by_date(breadth_pct: pd.Series | None = None) -> dict:
    """date -> breadth-floor exposure multiplier. Empty when breadth is unavailable -> the breadth policy
    falls back to full exposure (fail-open)."""
    br = breadth_pct if breadth_pct is not None else _load_breadth_series()
    if br is None or br.empty:
        return {}
    from advisory.portfolio_risk import breadth_floor_multiplier
    return dict(zip(br.index, breadth_floor_multiplier(br).to_numpy()))


def load_trades(hold_days: int | None = None, policy_version: str | None = None) -> pd.DataFrame:
    where = ["evaluation_status = 'evaluated'", "net_return IS NOT NULL", "weight_pct IS NOT NULL",
             "floor_multiplier IS NOT NULL AND floor_multiplier > 0"]
    params: dict[str, Any] = {}
    if hold_days is not None:
        where.append("hold_days = %(hold)s"); params["hold"] = int(hold_days)
    if policy_version is not None:
        where.append("policy_version = %(pv)s"); params["pv"] = policy_version
    df = sql_to_df(
        f"""SELECT asof_date, symbol, hold_days, weight_pct, floor_multiplier, net_return, benchmark_return
            FROM {SOURCE_TABLE} WHERE {' AND '.join(where)} ORDER BY asof_date, symbol""",
        params=params,
    )
    if not df.empty:
        df["asof_date"] = pd.to_datetime(df["asof_date"], utc=True, errors="coerce").dt.normalize()
    return df


# ------------------------------------------------------------------------------------------------
# Pure book math (unit-tested without DB)
# ------------------------------------------------------------------------------------------------
def _weights(day: pd.DataFrame, policy: str, breadth_floor: dict | None = None) -> np.ndarray:
    """Per-pick capital FRACTION for a policy (may sum to <1 -> the rest is cash at 0)."""
    n = len(day)
    if policy == "equal_weight":
        return np.full(n, 1.0 / n)                                   # fully deployed, 1/N
    nofloor = (day["weight_pct"].to_numpy() / day["floor_multiplier"].to_numpy()) / 100.0  # un-floor
    if policy == "voltarget_nofloor":
        return nofloor
    if policy == "regime_sized":
        return day["weight_pct"].to_numpy() / 100.0                  # vol-target x NIFTY-50DMA floor
    if policy == "regime_sized_breadth":
        bf = float((breadth_floor or {}).get(day["asof_date"].iloc[0], 1.0))  # vol-target x breadth floor
        return nofloor * bf
    raise ValueError(policy)


def book_returns_by_date(trades: pd.DataFrame, policy: str, breadth_floor: dict | None = None) -> pd.DataFrame:
    """Per decision date: book net return, NIFTY return, and deployed fraction under `policy`."""
    rows = []
    for d, day in trades.groupby("asof_date"):
        if policy == "nifty_buy_hold":
            rows.append({"asof_date": d, "book_ret": float(day["benchmark_return"].mean()),
                         "nifty_ret": float(day["benchmark_return"].mean()), "deployed": 1.0})
            continue
        w = _weights(day, policy, breadth_floor)
        book_ret = float(np.sum(w * day["net_return"].to_numpy()))   # cash (1-sum w) earns 0
        rows.append({"asof_date": d, "book_ret": book_ret,
                     "nifty_ret": float(day["benchmark_return"].mean()), "deployed": float(np.sum(w))})
    return pd.DataFrame(rows).sort_values("asof_date").reset_index(drop=True)


def overlap_stats(book: pd.DataFrame) -> dict[str, float]:
    """Distributional stats across (overlapping) decision dates -- NOT compounded (§8.3)."""
    r = book["book_ret"].to_numpy(dtype="float64")
    excess = (book["book_ret"] - book["nifty_ret"]).to_numpy(dtype="float64")
    vol = float(np.std(r, ddof=1)) if len(r) > 1 else float("nan")
    return {
        "n_dates": int(len(r)),
        "mean_return_pct": round(float(np.mean(r)) * 100, 3),
        "vol_pct": round(vol * 100, 3),
        "return_vol_ratio": round(float(np.mean(r)) / vol, 3) if vol and np.isfinite(vol) and vol > 0 else None,
        "mean_excess_pct": round(float(np.mean(excess)) * 100, 3),
        "pct_dates_positive": round(float(np.mean(r > 0)) * 100, 1),
        "worst_date_pct": round(float(np.min(r)) * 100, 3),
        "avg_deployment_pct": round(float(book["deployed"].mean()) * 100, 1),
    }


def nonoverlap_curve(book: pd.DataFrame, hold_days: int) -> dict[str, float]:
    """Compound a NON-overlapping equity curve (decision dates spaced >= hold_days apart) so max-drawdown
    is honest (overlapping holds would double-count). Thin by construction -- reported with n."""
    if book.empty:
        return {"nonoverlap_points": 0, "nonoverlap_total_return_pct": None,
                "nonoverlap_maxdd_pct": None, "nonoverlap_vol_pct": None}
    dates = list(book["asof_date"])
    picked_idx = [0]
    for i in range(1, len(dates)):
        if (dates[i] - dates[picked_idx[-1]]).days >= hold_days:
            picked_idx.append(i)
    seg = book.iloc[picked_idx]
    rets = seg["book_ret"].to_numpy(dtype="float64")
    equity = np.cumprod(1.0 + rets)
    peak = np.maximum.accumulate(equity)
    maxdd = float(np.min(equity / peak - 1.0)) if len(equity) else float("nan")
    vol = float(np.std(rets, ddof=1)) if len(rets) > 1 else float("nan")
    return {
        "nonoverlap_points": int(len(rets)),
        "nonoverlap_total_return_pct": round((float(equity[-1]) - 1.0) * 100, 3) if len(equity) else None,
        "nonoverlap_maxdd_pct": round(maxdd * 100, 3) if np.isfinite(maxdd) else None,
        "nonoverlap_vol_pct": round(vol * 100, 3) if np.isfinite(vol) else None,
    }


def classify_verdict(regime: dict[str, Any], nofloor: dict[str, Any]) -> str:
    """Does the crash floor help? Compare regime_sized (vol-target+floor) to voltarget_nofloor (same picks,
    floor removed). Descriptive -- the floor is capital protection (sold as risk, never alpha, §10)."""
    r_ratio, n_ratio = regime.get("return_vol_ratio"), nofloor.get("return_vol_ratio")
    r_dd, n_dd = regime.get("nonoverlap_maxdd_pct"), nofloor.get("nonoverlap_maxdd_pct")
    if regime.get("n_dates", 0) < 12:
        return "needs_more_data"
    dd_better = (r_dd is not None and n_dd is not None and r_dd > n_dd)  # less negative = shallower DD
    riskadj_better = (r_ratio is not None and n_ratio is not None and r_ratio > n_ratio)
    ret_lower = regime.get("mean_return_pct", 0) < nofloor.get("mean_return_pct", 0)
    if riskadj_better and dd_better:
        return "floor_improves_riskadj"
    if dd_better and ret_lower:
        return "floor_reduces_drawdown_costs_return"   # the §10 insurance trade-off
    if not dd_better and ret_lower:
        return "floor_not_helpful"
    return "manual_review_required"


def _floor_map_for(signal: str, threshold: float | None, exposure: float,
                   trades: pd.DataFrame, breadth_pct: pd.Series | None) -> dict:
    """date -> floor multiplier for a candidate config. 'nifty' recovers below-50DMA per date from the
    source's baked-in floor_multiplier (<1 == below); 'breadth' uses the point-in-time breadth floor."""
    if signal == "none":
        return {}
    if signal == "nifty":
        return {d: (exposure if float(day["floor_multiplier"].iloc[0]) < 1.0 else 1.0)
                for d, day in trades.groupby("asof_date")}
    if signal == "breadth":
        if breadth_pct is None or breadth_pct.empty:
            return {}
        from advisory.portfolio_risk import breadth_floor_multiplier
        fl = breadth_floor_multiplier(breadth_pct, healthy_pct=threshold, floor_exposure=exposure)
        return dict(zip(breadth_pct.index, fl.to_numpy()))
    raise ValueError(signal)


def _book_from_floor_map(trades: pd.DataFrame, floor_map: dict) -> pd.DataFrame:
    """Re-book the (un-floored) vol-target weights under an arbitrary date->floor map."""
    rows = []
    for d, day in trades.groupby("asof_date"):
        fm = float(floor_map.get(d, 1.0))
        w = (day["weight_pct"].to_numpy() / day["floor_multiplier"].to_numpy()) / 100.0 * fm
        rows.append({"asof_date": d, "book_ret": float(np.sum(w * day["net_return"].to_numpy())),
                     "nifty_ret": float(day["benchmark_return"].mean()), "deployed": float(np.sum(w))})
    return pd.DataFrame(rows).sort_values("asof_date").reset_index(drop=True)


def choose_config(scored: list[dict[str, Any]], ruin_guard_pct: float) -> tuple[dict[str, Any], str, int]:
    """Pure objective: among configs whose max-drawdown clears the ruin guard, pick the highest compounded
    growth. If NONE clear the guard, the guard binds -> pick the shallowest-drawdown (most protective)
    config instead. Returns (best, objective_label, n_passing)."""
    passing = [c for c in scored if c.get("maxdd_pct") is not None and c["maxdd_pct"] >= ruin_guard_pct]
    if passing:
        best = max(passing, key=lambda c: c["growth_pct"] if c.get("growth_pct") is not None else -1e18)
        return best, "max_growth_within_ruin_guard", len(passing)
    best = max(scored, key=lambda c: c["maxdd_pct"] if c.get("maxdd_pct") is not None else -1e18)
    return best, "ruin_guard_binds_min_drawdown", 0


def select_floor_config(trades: pd.DataFrame, breadth_pct: pd.Series | None, *, hold_days: int,
                        ruin_guard: float = RUIN_GUARD_MAXDD) -> dict[str, Any]:
    """Score every floor config on the paper book and let the objective pick. Descriptive: reports the
    data-preferred config + the frontier; changes no live sizing."""
    scored = []
    for signal, threshold, exposure in FLOOR_SWEEP:
        book = _book_from_floor_map(trades, _floor_map_for(signal, threshold, exposure, trades, breadth_pct))
        nc = nonoverlap_curve(book, hold_days)
        st = overlap_stats(book)
        scored.append({"signal": signal, "threshold": threshold, "exposure": exposure,
                       "growth_pct": nc["nonoverlap_total_return_pct"], "maxdd_pct": nc["nonoverlap_maxdd_pct"],
                       "mean_pct": st["mean_return_pct"], "deploy_pct": st.get("avg_deployment_pct")})
    guard_pct = ruin_guard * 100.0
    best, objective, n_passing = choose_config(scored, guard_pct)
    robust = n_passing >= max(3, len(scored) // 2)  # winner sits on a broad plateau, not a knife-edge
    frontier = sorted([c for c in scored if c.get("growth_pct") is not None],
                      key=lambda c: -c["growth_pct"])[:4]
    return {"objective": objective, "ruin_guard_maxdd_pct": round(guard_pct, 1), "selected": best,
            "n_passing": n_passing, "n_configs": len(scored), "robust": robust, "frontier": frontier}


def compute_ledger(trades: pd.DataFrame, *, run_date: pd.Timestamp, hold_days: int,
                   breadth_floor: dict | None = None) -> list[dict[str, Any]]:
    per_policy: dict[str, dict[str, Any]] = {}
    for policy in POLICIES:
        book = book_returns_by_date(trades, policy, breadth_floor)
        stats = overlap_stats(book)
        stats.update(nonoverlap_curve(book, hold_days))
        stats["n_trades"] = int(len(trades)) if policy != "nifty_buy_hold" else int(trades["asof_date"].nunique())
        per_policy[policy] = stats
    verdict = classify_verdict(per_policy["regime_sized"], per_policy["voltarget_nofloor"])
    # is the breadth floor a better regime lever than the NIFTY-50DMA floor? (both de-risk vs no-floor;
    # breadth wins if it earns more at same-or-better drawdown -- the measured result on this sample.)
    br, nf = per_policy.get("regime_sized_breadth"), per_policy["regime_sized"]
    if br and br.get("n_dates", 0) >= 12:
        br_better = (br.get("mean_return_pct", 0) > nf.get("mean_return_pct", 0)
                     and (br.get("nonoverlap_maxdd_pct") or -99) >= (nf.get("nonoverlap_maxdd_pct") or -99))
        per_policy["regime_sized_breadth"]["_verdict"] = "breadth_floor_dominates_nifty" if br_better else "breadth_floor_not_better"
    now = pd.Timestamp.utcnow()  # wall-clock run time (run_date is the data/maturity frontier, not "when it ran")
    records = []
    for policy, s in per_policy.items():
        records.append({
            "run_date": run_date, "policy": policy, "hold_days": hold_days,
            "n_dates": s["n_dates"], "n_trades": s["n_trades"],
            "mean_return_pct": s["mean_return_pct"], "vol_pct": s["vol_pct"],
            "return_vol_ratio": s["return_vol_ratio"], "mean_excess_pct": s["mean_excess_pct"],
            "pct_dates_positive": s["pct_dates_positive"], "worst_date_pct": s["worst_date_pct"],
            "avg_deployment_pct": s.get("avg_deployment_pct"),
            "nonoverlap_points": s["nonoverlap_points"],
            "nonoverlap_total_return_pct": s["nonoverlap_total_return_pct"],
            "nonoverlap_maxdd_pct": s["nonoverlap_maxdd_pct"], "nonoverlap_vol_pct": s["nonoverlap_vol_pct"],
            "verdict": (verdict if policy == "regime_sized" else s.get("_verdict", "")), "load_ts": now,
        })
    return records


def run_shadow_ledger(*, hold_days: int | None = None, dry_run: bool = False) -> dict[str, Any]:
    trades = load_trades(hold_days=hold_days)
    if trades.empty:
        return {"records": [], "note": "no evaluated trades in source"}
    hold = int(hold_days if hold_days is not None else trades["hold_days"].mode().iloc[0])
    trades = trades[trades["hold_days"] == hold].copy()
    run_date = pd.Timestamp(trades["asof_date"].max()).normalize()
    breadth_pct = _load_breadth_series()
    breadth_floor = _breadth_floor_by_date(breadth_pct)
    records = compute_ledger(trades, run_date=run_date, hold_days=hold, breadth_floor=breadth_floor)
    floor_selection = select_floor_config(trades, breadth_pct, hold_days=hold)
    if records and not dry_run:
        ensure_table()
        upsert_to_db(pd.DataFrame(records), TABLE_NAME,
                     unique_keys=["run_date", "policy", "hold_days"], timescaledb_column="run_date")
        sel = floor_selection["selected"]
        ensure_floor_config_table()
        upsert_to_db(pd.DataFrame([{
            "run_date": run_date, "hold_days": hold, "objective": floor_selection["objective"],
            "ruin_guard_maxdd_pct": floor_selection["ruin_guard_maxdd_pct"],
            "selected_signal": sel["signal"], "selected_threshold": sel["threshold"],
            "selected_exposure": sel["exposure"], "growth_pct": sel["growth_pct"], "maxdd_pct": sel["maxdd_pct"],
            "deploy_pct": sel["deploy_pct"], "n_passing": floor_selection["n_passing"],
            "n_configs": floor_selection["n_configs"], "robust": floor_selection["robust"],
            "load_ts": pd.Timestamp.utcnow(),
        }]), FLOOR_CONFIG_TABLE, unique_keys=["run_date", "hold_days"], timescaledb_column="run_date")
    return {"records": records, "floor_selection": floor_selection, "hold_days": hold, "n_trades": int(len(trades))}


def format_text_report(result: dict[str, Any]) -> str:
    recs = result.get("records", [])
    if not recs:
        return f"regime shadow ledger: {result.get('note', 'no data')}"
    verdict = next((r["verdict"] for r in recs if r["policy"] == "regime_sized" and r["verdict"]), "")
    breadth_verdict = next((r["verdict"] for r in recs if r["policy"] == "regime_sized_breadth" and r["verdict"]), "")
    lines = [f"regime-sized shadow ledger (hold={result.get('hold_days')}d, trades={result.get('n_trades')}, "
             f"source={SOURCE_TABLE})",
             "policies: equal_weight=1/N always-on | voltarget_nofloor=sized, no regime | "
             "regime_sized=sized x NIFTY-50DMA floor | regime_sized_breadth=sized x breadth floor | nifty_buy_hold=north star",
             f"VERDICT (NIFTY floor vs no-floor): {verdict}",
             f"VERDICT (breadth floor vs NIFTY floor): {breadth_verdict}",
             f"\n{'policy':<20}{'meanRet%':>9}{'vol%':>8}{'ret/vol':>8}{'excess%':>8}{'deploy%':>8}"
             f"{'noMaxDD%':>9}{'noTotRet%':>10}"]
    for r in recs:
        def g(k, d="-"):
            v = r.get(k); return f"{v:>.3f}" if isinstance(v, (int, float)) and v is not None else d
        lines.append(f"{r['policy']:<20}{g('mean_return_pct'):>9}{g('vol_pct'):>8}{g('return_vol_ratio'):>8}"
                     f"{g('mean_excess_pct'):>8}{g('avg_deployment_pct'):>8}"
                     f"{g('nonoverlap_maxdd_pct'):>9}{g('nonoverlap_total_return_pct'):>10}")
    lines.append(f"\nnon-overlapping equity points: {recs[0].get('nonoverlap_points')} (thin -- maxDD indicative)")
    sel = result.get("floor_selection")
    if sel:
        s = sel["selected"]
        cfg = ("no floor" if s["signal"] == "none"
               else f"{s['signal']} floor" + (f" @ >={s['threshold']:.2f}" if s["threshold"] is not None else "")
               + f", exposure {s['exposure']:.2f}")
        lines.append(f"\n-- DATA-SELECTED FLOOR (objective: max compounded growth s.t. ruin-guard maxDD "
                     f">= {sel['ruin_guard_maxdd_pct']}%) --")
        lines.append(f"  chosen: {cfg}   growth {s['growth_pct']}%  maxDD {s['maxdd_pct']}%  "
                     f"deploy {s['deploy_pct']}%   [{sel['objective']}, {sel['n_passing']}/{sel['n_configs']} pass, "
                     f"{'robust' if sel['robust'] else 'FRAGILE'}]")
        for c in sel["frontier"]:
            tag = ("no floor" if c["signal"] == "none" else
                   f"{c['signal']}" + (f">={c['threshold']:.2f}" if c["threshold"] is not None else "") + f"x{c['exposure']:.2f}")
            lines.append(f"    {tag:<22} growth {c['growth_pct']:>7}%  maxDD {c['maxdd_pct']:>7}%")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Regime-sized shadow ledger vs NIFTY (research/report-only).")
    parser.add_argument("--dry-run", action="store_true", help="Compute and print, but do not persist.")
    parser.add_argument("--hold-days", type=int, default=None, help="Hold horizon to book (default = source mode).")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)
    result = run_shadow_ledger(hold_days=args.hold_days, dry_run=bool(args.dry_run))
    print(json.dumps(result, indent=2, default=str) if args.format == "json" else format_text_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
