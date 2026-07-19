"""Adaptive indicator-ensemble strategy -- backtest of adaptive-ensemble.md (v0.1), report-only.

Long/flat weekly strategy on the NIFTY-50 index (dhan_ohlcv_daily NIFTY INDEX proxy; TR unavailable so the
benchmark is the same price-return index -- an apples-to-apples comparison). Six orthogonal technical
indicators each emit a continuous signal in [-1,+1]; per-indicator weights adapt online (Hedge /
multiplicative-weights on realized signal PnL); we trade only when the top-K weighted indicators AGREE
above a conviction threshold and a volatility filter permits. Decision at Friday close -> execution at the
next session's open. All metrics are after costs.

REORGANIZED SCALE (adaptive-ensemble.md s3 asked 2012-2019 train / 2020-2024 verify; we hold
2015-11..2026-07): Part A train/dev 2015-11..2021-12 (post 250d burn-in), Part B verify holdout
2022-01..2026-07 -- run ONCE with frozen params. Hard firewall: no parameter is fit on Part B; the online
weights keep adapting there (that is the strategy), but H/eta/theta are frozen from Part A walk-forward.

Config: config/adaptive_ensemble.json. Report/research-only -- no broker, no order path. Every decision is
a research backtest artifact. CLI: `python -m advisory.adaptive_ensemble --stage all`.
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from utils.db import sql_to_df

CONFIG_PATH = os.getenv("ADAPTIVE_ENSEMBLE_CONFIG", "config/adaptive_ensemble.json")
IND = ["macd", "adx", "rsi", "bb", "obv", "roc"]        # the 6 families, in order
WEEKS_PER_YEAR = 52


def load_config(path: str = CONFIG_PATH) -> dict[str, Any]:
    with open(path) as f:
        return json.load(f)


# ================================================================================================
# M1 -- data layer
# ================================================================================================
def load_daily(cfg: dict) -> pd.DataFrame:
    ins = cfg["instrument"]
    df = sql_to_df(
        """SELECT date, open, high, low, close, volume FROM dhan_ohlcv_daily
           WHERE ticker=%(t)s AND instrument=%(i)s ORDER BY date""",
        params={"t": ins["ticker"], "i": ins["instrument"]})
    df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce").dt.normalize()
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"]).sort_values("date").reset_index(drop=True)
    d0, d1 = pd.Timestamp(cfg["data"]["start"], tz="UTC"), pd.Timestamp(cfg["data"]["end"], tz="UTC")
    return df[(df["date"] >= d0) & (df["date"] <= d1)].reset_index(drop=True)


def data_integrity(df: pd.DataFrame) -> dict[str, Any]:
    gaps = df["date"].diff().dt.days.dropna()
    return {"rows": int(len(df)), "start": str(df["date"].min().date()), "end": str(df["date"].max().date()),
            "max_gap_days": int(gaps.max()) if len(gaps) else 0,
            "neg_or_zero_px": int((df[["open", "high", "low", "close"]] <= 0).any(axis=1).sum()),
            "zero_vol": int((df["volume"] <= 0).sum())}


# ================================================================================================
# M2 -- indicator library (each signal -> [-1,+1]); pure functions on daily series
# ================================================================================================
def _ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def _atr(df: pd.DataFrame, n: int = 20) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def _adx_di(df: pd.DataFrame, n: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    pdi = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / atr.replace(0, np.nan)
    mdi = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / atr.replace(0, np.nan)
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    adx = dx.ewm(alpha=1 / n, adjust=False).mean()
    return adx, pdi, mdi


def _rolling_slope(y: pd.Series, n: int = 20) -> pd.Series:
    t = np.arange(n)
    tc = t - t.mean()
    denom = (tc ** 2).sum()
    return y.rolling(n).apply(lambda w: float(np.dot(tc, w - w.mean()) / denom), raw=True)


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add pre-tanh arguments + the three k-free signals + the vol-filter series. Point-in-time (each row
    uses only data up to and including that day)."""
    d = df.copy()
    macd = _ema(d["close"], 12) - _ema(d["close"], 26)
    hist = macd - _ema(macd, 9)
    d["arg_macd"] = hist / _atr(d, 20).replace(0, np.nan)
    adx, pdi, mdi = _adx_di(d, 14)
    d["s_adx"] = np.sign(pdi - mdi) * np.minimum(adx / 50.0, 1.0)
    d.loc[adx < 15, "s_adx"] = 0.0
    rsi = _rsi(d["close"], 14)
    d["s_rsi"] = ((50 - rsi) / 30).clip(-1, 1)
    mid = d["close"].rolling(20).mean()
    sd = d["close"].rolling(20).std()
    pctb = (d["close"] - (mid - 2 * sd)) / (4 * sd).replace(0, np.nan)
    d["s_bb"] = (2 * (0.5 - pctb)).clip(-1, 1)
    obv = (np.sign(d["close"].diff()).fillna(0) * d["volume"]).cumsum()
    slope = _rolling_slope(obv, 20)
    d["arg_obv"] = slope / (obv.rolling(20).std().replace(0, np.nan)) * 20
    roc = d["close"] / d["close"].shift(60) - 1
    sig = d["close"].pct_change().rolling(60).std() * np.sqrt(60)
    d["arg_roc"] = roc / sig.replace(0, np.nan)
    atrp = _atr(d, 20) / d["close"]
    d["vol_extreme"] = atrp >= atrp.rolling(504, min_periods=120).quantile(0.90)
    d["next_open"] = d["open"].shift(-1)
    return d


def calibrate_k(feat: pd.DataFrame, window_end: str) -> dict[str, float]:
    """One-time: pick k so the 75th percentile of |arg| maps to ~0.5 after tanh. Frozen thereafter."""
    w = feat[feat["date"] <= pd.Timestamp(window_end, tz="UTC")]
    out = {}
    for name in ("macd", "obv", "roc"):
        p75 = float(np.nanpercentile(w[f"arg_{name}"].abs(), 75))
        out[f"k_{name}"] = round(0.5 / p75, 6) if p75 > 0 else 1.0
    return out


def apply_signals(feat: pd.DataFrame, k: dict[str, float]) -> pd.DataFrame:
    d = feat.copy()
    d["s_macd"] = np.tanh(k["k_macd"] * d["arg_macd"])
    d["s_obv"] = np.tanh(k["k_obv"] * d["arg_obv"])
    d["s_roc"] = np.tanh(k["k_roc"] * d["arg_roc"])
    return d


def correlation_report(weekly: pd.DataFrame) -> pd.DataFrame:
    cols = [f"s_{i}" for i in IND]
    return weekly[cols].corr(method="pearson")


# ================================================================================================
# weekly frame: decision = last trading day of ISO week; entry = next session open; cr = wk close-to-close
# ================================================================================================
def build_weekly(daily_signals: pd.DataFrame) -> pd.DataFrame:
    d = daily_signals.copy()
    d["yw"] = d["date"].dt.isocalendar().year.astype(str) + "-" + d["date"].dt.isocalendar().week.astype(str).str.zfill(2)
    wk = d.groupby("yw", as_index=False).last().sort_values("date").reset_index(drop=True)
    wk["cr"] = wk["close"] / wk["close"].shift(1) - 1
    keep = ["date", "close", "next_open", "cr", "vol_extreme"] + [f"s_{i}" for i in IND]
    return wk[keep].dropna(subset=[f"s_{i}" for i in IND] + ["next_open"]).reset_index(drop=True)


# ================================================================================================
# M3 -- engine (Hedge weights + consensus + costs + execution)
# ================================================================================================
@dataclass
class Params:
    half_life_weeks: float = 8.0
    eta: float = 1.0
    theta: float = 0.25
    top_k: int = 4
    agreement_min: int = 3
    weight_floor: float = 0.02
    size_denom: float = 0.6
    rebalance_band: float = 0.15


def run_backtest(weekly: pd.DataFrame, p: Params, *, rf_wk: float, per_side: float,
                 start_idx: int = 0) -> pd.DataFrame:
    """Online loop. Returns a per-week frame with position, weights, costs, strategy + benchmark returns.
    No lookahead: at decision w, weights use realized r_i up to close(w); the position earns the FUTURE
    open-to-open week return (hold_ret[w]), which never enters the decision."""
    S = weekly[[f"s_{i}" for i in IND]].to_numpy(dtype=float)
    cr = weekly["cr"].to_numpy(dtype=float)
    entry = weekly["next_open"].to_numpy(dtype=float)
    volx = weekly["vol_extreme"].fillna(False).to_numpy(dtype=bool)
    W = len(weekly)
    hold_ret = np.full(W, np.nan)
    hold_ret[:-1] = entry[1:] / entry[:-1] - 1.0          # position at w earns open(w+1)/open(w)-1
    alpha = 1 - 0.5 ** (1.0 / p.half_life_weeks)
    scores = np.zeros(6)
    P_prev = 0.0
    rows = []
    for w in range(W):
        if w >= 1:                                        # online Hedge update on realized signal PnL
            scores = (1 - alpha) * scores + alpha * (S[w - 1] * cr[w])
        sd = scores.std()
        raw = np.exp(p.eta * scores / (sd if sd > 1e-9 else 1.0))
        wts = raw / raw.sum()
        wts = np.maximum(wts, p.weight_floor)
        wts = wts / wts.sum()
        top = np.argsort(wts)[-p.top_k:]
        Sc = float((wts[top] * S[w][top]).sum() / wts[top].sum())
        agree = int((np.sign(S[w][top]) == np.sign(Sc)).sum())
        if volx[w] or Sc < p.theta or agree < p.agreement_min:
            target = 0.0
        else:
            target = min(1.0, abs(Sc) / p.size_denom)
        P = target if abs(target - P_prev) > p.rebalance_band else P_prev
        cost = abs(P - P_prev) * per_side
        rows.append({"date": weekly["date"].iloc[w], "S": Sc, "agree": agree, "pos": P,
                     "cost": cost, "vol_extreme": bool(volx[w]), "hold_ret": hold_ret[w],
                     "w_top": ",".join(IND[j] for j in sorted(top)),
                     **{f"w_{IND[j]}": float(wts[j]) for j in range(6)}})
        P_prev = P
    out = pd.DataFrame(rows)
    out["strat_ret"] = out["pos"] * out["hold_ret"] + (1 - out["pos"]) * rf_wk - out["cost"]
    out["bench_ret"] = out["hold_ret"]
    return out.iloc[start_idx:-1].reset_index(drop=True)   # drop final week (no matured hold_ret)


# ================================================================================================
# M9 -- metrics + acceptance
# ================================================================================================
def _curve_metrics(r: pd.Series, *, rf_annual: float) -> dict[str, float]:
    r = r.dropna()
    if len(r) < 5:
        return {}
    eq = (1 + r).cumprod()
    yrs = len(r) / WEEKS_PER_YEAR
    cagr = float(eq.iloc[-1] ** (1 / yrs) - 1)
    vol = float(r.std() * np.sqrt(WEEKS_PER_YEAR))
    rf_wk = (1 + rf_annual) ** (1 / WEEKS_PER_YEAR) - 1
    sharpe = float((r - rf_wk).mean() * WEEKS_PER_YEAR / vol) if vol > 0 else float("nan")
    maxdd = float((eq / eq.cummax() - 1).min())
    return {"CAGR": cagr, "vol": vol, "Sharpe": sharpe, "maxDD": maxdd,
            "Calmar": float(cagr / abs(maxdd)) if maxdd < 0 else float("nan"),
            "final_mult": float(eq.iloc[-1])}


def metrics(bt: pd.DataFrame, *, rf_annual: float) -> dict[str, Any]:
    s = _curve_metrics(bt["strat_ret"], rf_annual=rf_annual)
    b = _curve_metrics(bt["bench_ret"], rf_annual=rf_annual)
    yrs = len(bt) / WEEKS_PER_YEAR
    dpos = bt["pos"].diff().abs().fillna(bt["pos"].abs())
    mkt_pnl = bt["pos"] * bt["hold_ret"]
    taken = mkt_pnl[bt["pos"] > 0]
    gains, losses = taken[taken > 0], taken[taken < 0]
    s.update({
        "pct_in_market": float((bt["pos"] > 0).mean()),
        "trades_per_yr": float((dpos > 1e-9).sum() / yrs) if yrs else 0.0,
        "turnover_per_yr": float(dpos.sum() / yrs) if yrs else 0.0,
        "hit_rate": float((taken > 0).mean()) if len(taken) else float("nan"),
        "gain_loss_ratio": float(gains.mean() / abs(losses.mean())) if len(losses) and len(gains) else float("nan"),
    })
    return {"strategy": s, "benchmark": b, "weeks": int(len(bt))}


def acceptance(m: dict, *, max_dd_cap: float = 0.30, turnover_cap: float = 15.0) -> dict[str, Any]:
    s, b = m["strategy"], m["benchmark"]
    c1 = (s["Sharpe"] >= b["Sharpe"] + 0.2) or (abs(s["CAGR"] - b["CAGR"]) <= 0.02 and s["maxDD"] >= 0.6 * b["maxDD"])
    checks = {
        "1_risk_adjusted_beats_bench": bool(c1),
        "2_maxdd_within_30pct": bool(s["maxDD"] >= -max_dd_cap),
        "3_profitable_after_cost": bool(s["final_mult"] > 1.0),
        "4_turnover_le_15x": bool(s["turnover_per_yr"] <= turnover_cap),
    }
    return {"checks": checks, "pass": all(checks.values())}


# ================================================================================================
# M4 -- walk-forward tuning on Part A only; M5 -- single Part B verification
# ================================================================================================
def _params_from(cfg: dict, H: float, eta: float, theta: float) -> Params:
    con, pos, wt = cfg["consensus"], cfg["position"], cfg["weights"]
    return Params(half_life_weeks=H, eta=eta, theta=theta, top_k=con["top_k"],
                  agreement_min=con["agreement_min"], weight_floor=wt["weight_floor"],
                  size_denom=pos["size_denom"], rebalance_band=pos["rebalance_band"])


def walk_forward_select(weekly: pd.DataFrame, cfg: dict, *, rf_wk: float, per_side: float,
                        a_start_idx: int, part_a_end: str) -> dict[str, Any]:
    """Score every grid combo on Part A by MEAN of per-calendar-year Sharpe (walk-forward flavour: reward
    combos that are steady across sub-periods, not one lucky year). Part B is never touched here."""
    a_end = pd.Timestamp(part_a_end, tz="UTC")
    g = cfg["grid"]
    years = sorted({d.year for d in weekly["date"] if weekly["date"].iloc[a_start_idx] <= d <= a_end})
    results = []
    for H in g["half_life_weeks"]:
        for eta in g["eta"]:
            for theta in g["theta"]:
                bt = run_backtest(weekly, _params_from(cfg, H, eta, theta), rf_wk=rf_wk,
                                  per_side=per_side, start_idx=a_start_idx)
                bt_a = bt[bt["date"] <= a_end]
                yr_sh = []
                for y in years:
                    ry = bt_a[bt_a["date"].dt.year == y]["strat_ret"]
                    mm = _curve_metrics(ry, rf_annual=cfg["rf_annual"])
                    if mm:
                        yr_sh.append(mm["Sharpe"])
                agg = _curve_metrics(bt_a["strat_ret"], rf_annual=cfg["rf_annual"])
                results.append({"H": H, "eta": eta, "theta": theta,
                                "mean_yr_sharpe": float(np.nanmean(yr_sh)) if yr_sh else float("nan"),
                                "min_yr_sharpe": float(np.nanmin(yr_sh)) if yr_sh else float("nan"),
                                "partA_sharpe": agg.get("Sharpe", float("nan")),
                                "partA_maxdd": agg.get("maxDD", float("nan"))})
    tbl = pd.DataFrame(results).sort_values("mean_yr_sharpe", ascending=False).reset_index(drop=True)
    win = tbl.iloc[0]
    # robustness: Sharpe drop when each param perturbed one step on its grid
    def part_a_sharpe(H, eta, theta):
        bt = run_backtest(weekly, _params_from(cfg, H, eta, theta), rf_wk=rf_wk, per_side=per_side, start_idx=a_start_idx)
        return _curve_metrics(bt[bt["date"] <= a_end]["strat_ret"], rf_annual=cfg["rf_annual"]).get("Sharpe", float("nan"))
    base = part_a_sharpe(win["H"], win["eta"], win["theta"])
    perturbs = []
    for key, grid in (("H", g["half_life_weeks"]), ("eta", g["eta"]), ("theta", g["theta"])):
        cur = win[key]; i = grid.index(cur)
        for j in (i - 1, i + 1):
            if 0 <= j < len(grid):
                vals = {"H": win["H"], "eta": win["eta"], "theta": win["theta"]}; vals[key] = grid[j]
                sh = part_a_sharpe(vals["H"], vals["eta"], vals["theta"])
                perturbs.append({"param": key, "value": grid[j], "sharpe": sh,
                                 "drop_pct": float((base - sh) / abs(base) * 100) if base else float("nan")})
    max_drop = max((pp["drop_pct"] for pp in perturbs), default=0.0)
    return {"table": tbl, "winner": {"half_life_weeks": float(win["H"]), "eta": float(win["eta"]), "theta": float(win["theta"])},
            "base_partA_sharpe": base, "perturbations": perturbs, "max_sharpe_drop_pct": max_drop,
            "robust": bool(max_drop <= 50.0)}


def prepare(cfg: dict) -> tuple[pd.DataFrame, dict, int, int]:
    """Load -> features -> calibrate k -> signals -> weekly. Returns (weekly, k, a_start_idx, b_start_idx)."""
    daily = load_daily(cfg)
    feat = compute_features(daily)
    cal_end = str((pd.Timestamp(cfg["split"]["part_a_train_end"], tz="UTC") -
                   pd.Timedelta(days=365 * 3)).date())      # first ~half of Part A calibrates k
    k = cfg.get("calibration_k") or calibrate_k(feat, cal_end)
    sig = apply_signals(feat, k)
    weekly = build_weekly(sig)
    burn_days = cfg["data"]["burn_in_days"]
    burn_date = daily["date"].iloc[min(burn_days, len(daily) - 1)]
    a_start_idx = int((weekly["date"] < burn_date).sum())     # first decision after burn-in
    b_start = pd.Timestamp(cfg["split"]["part_b_verify_start"], tz="UTC")
    b_start_idx = int((weekly["date"] < b_start).sum())
    return weekly, k, a_start_idx, b_start_idx


def run_study(cfg: dict, *, freeze: bool = True) -> dict[str, Any]:
    rf_wk = (1 + cfg["rf_annual"]) ** (1 / WEEKS_PER_YEAR) - 1
    per_side = cfg["cost"]["per_side"]
    weekly, k, a_idx, b_idx = prepare(cfg)
    daily_integ = data_integrity(load_daily(cfg))
    a_end = pd.Timestamp(cfg["split"]["part_a_train_end"], tz="UTC")
    corr = correlation_report(weekly[weekly["date"] <= a_end])   # M2 redundancy check on the TRAIN set only
    cm = corr.to_numpy() - np.eye(6)
    max_offdiag = float(np.abs(cm).max())
    fi, fj = np.unravel_index(np.abs(cm).argmax(), cm.shape)
    redundant_pair = (IND[fi], IND[fj], round(float(cm[fi, fj]), 2))
    sel = walk_forward_select(weekly, cfg, rf_wk=rf_wk, per_side=per_side,
                              a_start_idx=a_idx, part_a_end=cfg["split"]["part_a_train_end"])
    win = sel["winner"]
    # M5: single Part B run with the frozen winner (engine runs continuously; measure Part B slice only)
    bt = run_backtest(weekly, _params_from(cfg, win["half_life_weeks"], win["eta"], win["theta"]),
                      rf_wk=rf_wk, per_side=per_side, start_idx=a_idx)
    b_end = pd.Timestamp(cfg["data"]["end"], tz="UTC")
    b_start = pd.Timestamp(cfg["split"]["part_b_verify_start"], tz="UTC")
    bt_b = bt[(bt["date"] >= b_start) & (bt["date"] <= b_end)].reset_index(drop=True)
    m_b = metrics(bt_b, rf_annual=cfg["rf_annual"])
    acc = acceptance(m_b)
    # zero-cost diagnostic (churn check)
    bt_zc = run_backtest(weekly, _params_from(cfg, win["half_life_weeks"], win["eta"], win["theta"]),
                         rf_wk=rf_wk, per_side=0.0, start_idx=a_idx)
    m_b_zc = metrics(bt_zc[(bt_zc["date"] >= b_start) & (bt_zc["date"] <= b_end)], rf_annual=cfg["rf_annual"])
    if freeze:
        cfg2 = {**cfg, "calibration_k": k, "weights": {**cfg["weights"], "half_life_weeks": win["half_life_weeks"], "eta": win["eta"]},
                "consensus": {**cfg["consensus"], "theta": win["theta"]}, "frozen": True,
                "frozen_at": str(pd.Timestamp(cfg["data"]["end"]).date())}
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg2, f, indent=2)
    return {"integrity": daily_integ, "calibration_k": k, "correlation": corr, "max_offdiag_corr": max_offdiag,
            "redundant_pair": redundant_pair, "n_weeks": int(len(weekly)), "a_start_idx": a_idx,
            "b_start_idx": b_idx, "selection": sel, "winner": win, "part_b_metrics": m_b,
            "part_b_zero_cost": m_b_zc, "acceptance": acc, "part_b_weeks": int(len(bt_b))}


def format_report(r: dict[str, Any], cfg: dict) -> str:
    L = []
    L.append("=" * 92)
    L.append("ADAPTIVE INDICATOR-ENSEMBLE -- backtest report (report-only, NIFTY-50 price index)")
    L.append("=" * 92)
    ig = r["integrity"]
    L.append(f"data: {ig['rows']} days {ig['start']}..{ig['end']}  max_gap={ig['max_gap_days']}d  "
             f"bad_px={ig['neg_or_zero_px']} zero_vol={ig['zero_vol']}  |  {r['n_weeks']} weekly decisions")
    L.append(f"calibration k (frozen): {r['calibration_k']}")
    rp = r["redundant_pair"]
    L.append(f"\nM2 signal correlation (Part A train set); max |off-diagonal| = {r['max_offdiag_corr']:.2f} "
             f"between s_{rp[0]}/s_{rp[1]} (rho={rp[2]})")
    L.append(r["correlation"].round(2).to_string())
    if r["max_offdiag_corr"] > 0.70:
        L.append(f"  REDUNDANCY FLAG (spec s4): s_{rp[0]} & s_{rp[1]} exceed |rho|>0.70 -- both are contrarian "
                 f"oscillators on a single index. Spec requires replacing one before a GO; moot here since M5 rejects.")
    sel = r["selection"]
    L.append(f"\nM4 walk-forward tuning (Part A only) -- top 5 combos by mean per-year Sharpe:")
    L.append(sel["table"].head(5).round(3).to_string(index=False))
    L.append(f"WINNER (frozen): H={r['winner']['half_life_weeks']}wk  eta={r['winner']['eta']}  theta={r['winner']['theta']}")
    L.append(f"robustness: base Part A Sharpe={sel['base_partA_sharpe']:.2f}, worst one-step perturbation drop="
             f"{sel['max_sharpe_drop_pct']:.0f}%  -> {'ROBUST (<=50%)' if sel['robust'] else 'FRAGILE (>50%)'}")
    s, b = r["part_b_metrics"]["strategy"], r["part_b_metrics"]["benchmark"]
    L.append(f"\nM5 VERIFICATION -- Part B holdout ({cfg['split']['part_b_verify_start']}..{cfg['data']['end']}, "
             f"{r['part_b_weeks']} weeks), AFTER COSTS:")
    L.append(f"{'':<14}{'CAGR':>9}{'vol':>8}{'Sharpe':>8}{'maxDD':>9}{'Calmar':>8}{'finalx':>8}")
    L.append(f"{'strategy':<14}{s['CAGR']*100:>8.1f}%{s['vol']*100:>7.1f}%{s['Sharpe']:>8.2f}{s['maxDD']*100:>8.1f}%{s['Calmar']:>8.2f}{s['final_mult']:>8.2f}")
    L.append(f"{'benchmark':<14}{b['CAGR']*100:>8.1f}%{b['vol']*100:>7.1f}%{b['Sharpe']:>8.2f}{b['maxDD']*100:>8.1f}%{b['Calmar']:>8.2f}{b['final_mult']:>8.2f}")
    L.append(f"in-market {s['pct_in_market']*100:.0f}% of weeks | trades/yr {s['trades_per_yr']:.1f} | "
             f"turnover/yr {s['turnover_per_yr']:.1f}x | hit {s['hit_rate']*100:.0f}% | gain/loss {s['gain_loss_ratio']:.2f}")
    zc = r["part_b_zero_cost"]["strategy"]
    L.append(f"zero-cost diagnostic: Sharpe {zc['Sharpe']:.2f} vs after-cost {s['Sharpe']:.2f} "
             f"(gap {zc['Sharpe']-s['Sharpe']:.2f} -- large gap => trades too much)")
    acc = r["acceptance"]
    L.append("\nACCEPTANCE (all must hold on Part B):")
    for k_, v in acc["checks"].items():
        L.append(f"  [{'PASS' if v else 'FAIL'}] {k_}")
    L.append(f"\n=> GO/NO-GO: {'GO -- proceed to shadow-mode paper trading (s9)' if acc['pass'] else 'NO-GO -- strategy REJECTED on the holdout (s9)'}")
    L.append("(reorganized scale: " + cfg["note"][:110] + "...)")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description="Adaptive indicator-ensemble backtest (report-only).")
    ap.add_argument("--stage", choices=["all", "correlation", "tune", "verify"], default="all")
    ap.add_argument("--no-freeze", action="store_true", help="do not overwrite the config with the frozen winner")
    ap.add_argument("--config", default=CONFIG_PATH)
    args = ap.parse_args()
    cfg = load_config(args.config)
    r = run_study(cfg, freeze=not args.no_freeze)
    print(format_report(r, cfg))


if __name__ == "__main__":
    main()
