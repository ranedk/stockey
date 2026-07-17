"""Deployment-timing research (report-only) -- can a model beat the simple trend/breadth floor? (It cannot.)

Our factor research shows no robust cross-regime SELECTION alpha; the measured edge is on the DEPLOYMENT/RISK
axis (be less deployed when the tape is bad). This asks whether a low-capacity model that COMBINES a few
point-in-time market-state signals -- trend (NIFTY vs 50/200DMA), volatility (VIX level + 20d change),
foreign flow (FPI equity 20d net), the yield-curve slope, INR -- can time deployment better than the single
trend/breadth floor the system already uses. This is the one axis where we have power: a market-level series
over ~40+ independent 20d blocks (2019-2026 OOS), unlike selection (~13 blocks).

METHOD (point-in-time, walk-forward). Deployable asset = NIFTY daily return. Label bad_t = forward-20d NIFTY
return < BAD_THRESH. An L2-regularized logistic (low capacity by design) is retrained every REFIT_EVERY days
on ONLY rows whose label matured strictly before the test block (no lookahead), predicting P(bad) -> a
continuous exposure clip(1 - P(bad), FLOOR, 1). Compared against always / trend50 / trend200 on the OOS
equity curve (CAGR, ann vol, maxDD, Sharpe, %deployed), with OOS AUC + walk-forward stability.

FINDING (2026-07, decisive). The model FAILS every pre-registered gate: OOS AUC ~= 0.38 (worse than random),
Spearman(P_bad, fwd_ret) ~= 0, worst CAGR, maxDD as deep as always-deployed, and it beats trend50 in only
1 of 3 walk-forward thirds. Adding VIX/FPI/yields DILUTED the one good signal (trend) with noise the model
could not distinguish across ~40 blocks -- the overfitting trap, even on the well-powered axis. But the
SIMPLE trend floor is excellent: trend50/trend200 as a deployment overlay cut max drawdown from ~-38% to
~-12% while PRESERVING CAGR (lifting Sharpe ~0.84 -> ~1.05), OOS across 2019-2026 incl. the COVID crash.
CONCLUSION: the simple trend/breadth floor is the ceiling; do NOT add a model. This validates the
deployment-axis thesis on 5x longer history than breadth alone and argues for leaning on the floor, not
enriching it. Report-only. CLI: `python -m advisory.deployment_timing`.
"""
from __future__ import annotations

import argparse
import json
from typing import Any

import numpy as np
import pandas as pd

from advisory.archetype_backtest import _load_benchmark
from utils.db import sql_to_df

H = 20
BAD_THRESH = -0.03
FLOOR = 0.20
MIN_TRAIN = 500
REFIT_EVERY = 20
TRADING_YEAR = 252
FEATURES = ["trend50", "trend200", "dma50_slope", "vix", "vix_chg20", "fpi20_z", "yield_slope", "inr_chg20"]


def build_panel() -> pd.DataFrame:
    b = _load_benchmark().copy()
    b["date"] = pd.to_datetime(b["date"], utc=True, errors="coerce").dt.normalize()
    b = b.sort_values("date").reset_index(drop=True)
    c = b["bench_close"]
    b["ret1"] = c.pct_change()
    b["fwd20_ret"] = c.shift(-H) / c - 1
    b["trend50"] = c / b["bench_dma50"] - 1
    b["trend200"] = c / b["bench_dma200"] - 1
    b["dma50_slope"] = b["bench_dma50"].pct_change(H)

    m = sql_to_df("SELECT asof_date AS date, vix_close, gsec_10y_yield, gsec_2y_yield, inr_usd_spot "
                  "FROM advisory_macro_daily")
    m["date"] = pd.to_datetime(m["date"], utc=True, errors="coerce").dt.normalize()
    m = m.sort_values("date")
    f = sql_to_df("SELECT date, net_investment_inr_crore AS fpi FROM fii_investments "
                  "WHERE instrument='equity_sub_total'")
    f["date"] = pd.to_datetime(f["date"], utc=True, errors="coerce").dt.normalize()
    f = f.sort_values("date")

    df = pd.merge_asof(b, m, on="date")
    df = pd.merge_asof(df, f, on="date")
    for col in ["vix_close", "gsec_10y_yield", "gsec_2y_yield", "inr_usd_spot", "fpi"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").ffill()
    df["vix"] = df["vix_close"]
    df["vix_chg20"] = df["vix_close"].pct_change(H)
    fpi20 = df["fpi"].rolling(H).sum()
    df["fpi20_z"] = (fpi20 - fpi20.rolling(TRADING_YEAR).mean()) / fpi20.rolling(TRADING_YEAR).std()
    df["yield_slope"] = df["gsec_10y_yield"] - df["gsec_2y_yield"]
    df["inr_chg20"] = df["inr_usd_spot"].pct_change(H)
    df["bad"] = (df["fwd20_ret"] < BAD_THRESH).astype(float)
    return df


def walkforward_proba(df: pd.DataFrame) -> pd.Series:
    """Expanding-window L2 logistic P(bad), trained only on labels matured before each test block."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    proba = pd.Series(np.nan, index=df.index)
    for start in range(MIN_TRAIN, len(df), REFIT_EVERY):
        tr = df.iloc[: max(0, start - H)].dropna(subset=FEATURES + ["bad"])
        if len(tr) < 200 or tr["bad"].nunique() < 2:
            continue
        sc = StandardScaler().fit(tr[FEATURES])
        clf = LogisticRegression(C=0.5, max_iter=1000).fit(sc.transform(tr[FEATURES]), tr["bad"])
        te = df.iloc[start: start + REFIT_EVERY].dropna(subset=FEATURES)
        if not te.empty:
            proba.loc[te.index] = clf.predict_proba(sc.transform(te[FEATURES]))[:, 1]
    return proba


def curve_stats(ret: pd.Series, expo: pd.Series) -> dict[str, float]:
    """Equity-curve stats for a daily-return series scaled by an exposure set the prior day."""
    strat = (expo.shift(1) * ret).dropna()
    if strat.empty:
        return {}
    eq = (1 + strat).cumprod()
    yrs = len(strat) / TRADING_YEAR
    cagr = eq.iloc[-1] ** (1 / yrs) - 1 if yrs > 0 else float("nan")
    vol = strat.std() * np.sqrt(TRADING_YEAR)
    maxdd = float((eq / eq.cummax() - 1).min())
    return {"CAGR": float(cagr), "vol": float(vol), "maxDD": maxdd,
            "Sharpe": float(cagr / vol) if vol else float("nan"),
            "deployed": float(expo.reindex(strat.index).mean())}


def run_experiment() -> dict[str, Any]:
    from sklearn.metrics import roc_auc_score
    df = build_panel()
    df["proba"] = walkforward_proba(df)
    oos = df[df["proba"].notna()].copy()
    if oos.empty:
        return {"note": "no OOS predictions"}
    idx = oos.index
    ret = df["ret1"].loc[idx.min():]
    schemes = {
        "always": pd.Series(1.0, index=df.index),
        "trend50 (floor)": pd.Series(np.where(df["bench_close"] > df["bench_dma50"], 1.0, FLOOR), index=df.index),
        "trend200 (floor)": pd.Series(np.where(df["bench_close"] > df["bench_dma200"], 1.0, FLOOR), index=df.index),
        "MODEL logit(1-P_bad)": (1 - df["proba"]).clip(FLOOR, 1.0),
    }
    stats = {name: curve_stats(ret, ex.loc[idx.min():]) for name, ex in schemes.items()}
    auc = float(roc_auc_score(oos["bad"], oos["proba"]))
    corr = float(oos[["proba", "fwd20_ret"]].corr(method="spearman").iloc[0, 1])
    thirds = []
    for i, seg in enumerate(np.array_split(idx, 3)):
        seg = pd.Index(seg)
        r = df["ret1"].loc[seg]
        m_dd = curve_stats(r, (1 - df["proba"]).clip(FLOOR, 1.0).loc[seg]).get("maxDD", float("nan"))
        t_dd = curve_stats(r, schemes["trend50 (floor)"].loc[seg]).get("maxDD", float("nan"))
        thirds.append({"third": i + 1, "model_maxDD": m_dd, "trend50_maxDD": t_dd,
                       "winner": "MODEL" if m_dd > t_dd else "trend50"})
    model_wins = sum(1 for t in thirds if t["winner"] == "MODEL")
    verdict = ("model_beats_floor" if (auc > 0.55 and model_wins == 3
               and stats["MODEL logit(1-P_bad)"]["maxDD"] > stats["trend50 (floor)"]["maxDD"])
               else "simple_floor_is_ceiling")
    return {"oos_start": str(oos["date"].min().date()), "oos_end": str(oos["date"].max().date()),
            "oos_days": int(len(oos)), "oos_blocks": int(len(oos) // H), "bad_rate": round(float(oos["bad"].mean()), 3),
            "stats": stats, "auc": round(auc, 3), "spearman_pbad_fwd": round(corr, 3),
            "thirds": thirds, "model_thirds_won": model_wins, "verdict": verdict}


def format_report(r: dict[str, Any]) -> str:
    if "stats" not in r:
        return f"deployment timing: {r.get('note', 'no result')}"
    lines = [f"DEPLOYMENT TIMING (report-only)  OOS {r['oos_start']}..{r['oos_end']}  "
             f"days={r['oos_days']} blocks~{r['oos_blocks']} bad_rate={r['bad_rate']}",
             f"{'scheme':<24}{'CAGR':>8}{'vol':>8}{'maxDD':>8}{'Sharpe':>8}{'deployed':>10}"]
    for name, s in r["stats"].items():
        if s:
            lines.append(f"{name:<24}{s['CAGR']*100:>7.1f}%{s['vol']*100:>7.1f}%{s['maxDD']*100:>7.1f}%"
                         f"{s['Sharpe']:>8.2f}{s['deployed']*100:>9.0f}%")
    lines.append(f"\nOOS AUC(P_bad)={r['auc']} (want>0.55)  Spearman(P_bad,fwd_ret)={r['spearman_pbad_fwd']} (want<0)  "
                 f"model won {r['model_thirds_won']}/3 walk-forward thirds")
    for t in r["thirds"]:
        lines.append(f"  third {t['third']}: model maxDD={t['model_maxDD']*100:>6.1f}%  "
                     f"trend50 maxDD={t['trend50_maxDD']*100:>6.1f}%  -> {t['winner']}")
    lines.append(f"\nVERDICT: {r['verdict']}")
    if r["verdict"] == "simple_floor_is_ceiling":
        lines.append("  The simple trend/breadth floor cannot be beaten by the model here -- do NOT add it. "
                     "The floor itself is the well-powered deployment edge (maxDD cut ~3x, CAGR preserved).")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Deployment-timing research (report-only).")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args()
    result = run_experiment()
    print(json.dumps(result, default=str, indent=2) if args.format == "json" else format_report(result))


if __name__ == "__main__":
    main()
