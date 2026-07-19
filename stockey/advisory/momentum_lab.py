"""Momentum lab -- Tier-1 knob ablation from docs/research/strategy_backlog.md (report-only).

Does a vol-adjusted (6+12mo) momentum score + semi-annual rebalance + constant-12%-vol scaling beat the
current 20-day RS book, net of cost? Isolates each knob so we see which one carries the improvement.
Universe = liquid dhan EQUITY (2021-2026, split-adjusted; ~494 names/day). Benchmark = equal-weight
buy-and-hold of the same universe (the NIFTY index has a 2021-07..2025-06 data hole). CLI: `python -m
advisory.momentum_lab`.

FINDING (2026-07, holdout 2022-06..2026-07 -- a STRONG momentum bull, one regime):
  * The literature priors HURT on this window. Semi-annual rebalance (29.6% CAGR) and constant-vol scaling
    (17.1%, but maxDD -33%->-16%) both gave up return vs the current monthly raw-RS book (40.1%) -- because
    it was a persistent bull with NO momentum crash and a liquid (cheap) universe, so defensive/low-turnover
    knobs only cost return. Same regime-dependence found everywhere in this system.
  * The one robust, cost-free win: the VOL-ADJUSTED SCORE (Sharpe 1.23 vs raw-RS 1.20, maxDD -29.6 vs -32.8
    at equal frequency). Small but free -- worth adopting/graduating.
  * Rebalance frequency did NOT follow the India 'low-turnover wins' prior: weekly won at EVERY cost level
    (45/90/150bps: 53.7/46.1/36.5% vs semi-annual 29.6/28.4/26.8%). Caveat -- one bull regime, no momentum
    crash (the scenario that punishes high turnover never occurred), and a liquid universe; do NOT commit the
    frequency knob on this alone.
  * Nothing beat the equal-weight universe buy&hold on Sharpe (1.38): in this bull the market did the work;
    momentum added CAGR at more risk. Consistent with 'no easy selection alpha'.
CONCLUSION: adopt the vol-adjusted score (route through advisory.factor_graduation); treat vol-scaling as
crash insurance only (deploy via the existing breadth/deployment floor, not always-on); the frequency knob is
unsettled (regime-/universe-dependent). Report/research-only; no broker.
"""
from __future__ import annotations
import numpy as np, pandas as pd
from utils.db import sql_to_df

MIN_TV, MIN_PX, K = 5e7, 30.0, 30
RT_COST = 0.0045                 # ~45bps round trip per replaced slot
RF_D = (1.06) ** (1 / 252) - 1
TGT_VOL = 0.12


def load():
    df = sql_to_df("""SELECT ticker AS symbol, date, close, volume FROM dhan_ohlcv_daily
                      WHERE instrument='EQUITY' AND close>0 ORDER BY ticker, date""")
    df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce").dt.normalize()
    for c in ("close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values(["symbol", "date"])
    g = df.groupby("symbol", group_keys=False)
    df["ret"] = g["close"].pct_change().clip(-0.25, 0.25)          # winsorize CA/delist jumps
    df["tv"] = (df["close"] * df["volume"]).groupby(df["symbol"]).transform(lambda s: s.rolling(20).mean())
    df["ret126"] = g["close"].transform(lambda s: s / s.shift(126) - 1)
    df["ret252"] = g["close"].transform(lambda s: s / s.shift(252) - 1)
    df["ret63"] = g["close"].transform(lambda s: s / s.shift(63) - 1)
    df["vol252"] = g["ret"].transform(lambda s: s.rolling(252).std()) * np.sqrt(252)
    df["liquid"] = (df["tv"] >= MIN_TV) & (df["close"] >= MIN_PX) & df["ret252"].notna() & (df["vol252"] > 0)
    return df


def wide(df):
    ret = df.pivot_table(index="date", columns="symbol", values="ret")
    liq = df.pivot_table(index="date", columns="symbol", values="liquid").fillna(0).astype(bool)
    # scores (masked to liquid)
    z = lambda x: (x - x.mean()) / x.std(ddof=0)
    df["_va"] = df["ret126"] / df["vol252"]
    df["_vb"] = df["ret252"] / df["vol252"]
    voladj = df.assign(s=df.groupby("date")["_va"].transform(z).add(df.groupby("date")["_vb"].transform(z)))
    va = voladj.pivot_table(index="date", columns="symbol", values="s")
    # raw RS (current-style): mean cross-sectional rank of 63/126/252d return
    for c in ("ret63", "ret126", "ret252"):
        df["_r_" + c] = df.groupby("date")[c].rank(pct=True)
    df["_rs"] = df[["_r_ret63", "_r_ret126", "_r_ret252"]].mean(axis=1)
    rs = df.pivot_table(index="date", columns="symbol", values="_rs")
    return ret, liq, va.where(liq), rs.where(liq)


def run(ret, score, rebalance, *, vol_scale, rt_cost=RT_COST):
    dates = list(ret.index)
    reb_idx = set(range(0, len(dates), rebalance))
    held, book, cost_series = [], [], []
    prev = set()
    for i, d in enumerate(dates):
        if i in reb_idx:
            row = score.loc[d].dropna()
            pick = set(row.nlargest(K).index) if len(row) >= K else set(row.index)
            cost = (len(pick - prev) / max(1, K)) * rt_cost if prev else rt_cost
            prev = pick
        else:
            cost = 0.0
        r = ret.loc[d, list(prev)].mean() if prev else 0.0
        book.append(0.0 if not np.isfinite(r) else float(r))
        cost_series.append(cost)
    s = pd.Series(book, index=dates) - pd.Series(cost_series, index=dates)
    if vol_scale:
        rv = pd.Series(book, index=dates).rolling(126).std() * np.sqrt(252)
        expo = (TGT_VOL / rv).clip(0, 1.0).shift(1).fillna(0.5)
        s = expo * (pd.Series(book, index=dates) - pd.Series(cost_series, index=dates)) + (1 - expo) * RF_D
    return s


def stats(r, label):
    r = r.dropna()
    if len(r) < 60:
        return None
    eq = (1 + r).cumprod(); yrs = len(r) / 252
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    vol = r.std() * np.sqrt(252)
    sharpe = (r.mean() * 252 - 0.06) / vol if vol > 0 else np.nan
    maxdd = (eq / eq.cummax() - 1).min()
    return dict(label=label, cagr=cagr, vol=vol, sharpe=sharpe, maxdd=maxdd, final=eq.iloc[-1])


def main():
    df = load()
    ret, liq, va, rs = wide(df)
    start = ret.index[ret.index >= pd.Timestamp("2022-06-01", tz="UTC")][0]
    ret, va, rs = ret.loc[start:], va.loc[start:], rs.loc[start:]
    n_uni = int(liq.loc[start:].sum(axis=1).mean())
    print(f"universe ~{n_uni} liquid names/day, {len(ret)} days {ret.index.min().date()}..{ret.index.max().date()}, top-K={K}\n")
    bench = ret.where(liq.loc[start:]).mean(axis=1)   # equal-weight buy&hold the liquid universe

    configs = [
        ("A current: raw RS, 21d, no volscale", rs, 21, False),
        ("B +vol-adj score, 21d",               va, 21, False),
        ("C +semi-annual (126d)",               va, 126, False),
        ("D +const-vol scale (12%)  [FULL]",    va, 126, True),
        ("  raw RS, 126d (freq only)",          rs, 126, False),
    ]
    rows = [stats(bench, "NIFTY-proxy: EW universe buy&hold")]
    for lbl, sc, rb, vs in configs:
        rows.append(stats(run(ret, sc, rb, vol_scale=vs), lbl))
    print(f"{'config':<38}{'CAGR':>8}{'vol':>8}{'Sharpe':>8}{'maxDD':>9}{'x':>7}")
    for r in rows:
        if r:
            print(f"{r['label']:<38}{r['cagr']*100:>7.1f}%{r['vol']*100:>7.1f}%{r['sharpe']:>8.2f}{r['maxdd']*100:>8.1f}%{r['final']:>7.2f}")

    print("\nrebalance-frequency x COST sensitivity (vol-adj score, no vol-scale) -- CAGR net of cost:")
    print("  the India 'low-turnover wins' prior only holds if costs are high enough; does the ranking flip?")
    costs = [("45bps", 0.0045), ("90bps", 0.0090), ("150bps", 0.0150)]
    print(f"{'rebalance':<14}" + "".join(f"{c[0]:>10}" for c in costs) + f"{'  best@150bps':>0}")
    freqs = [(5, "weekly"), (21, "monthly"), (63, "quarterly"), (126, "semi-annual")]
    grid = {}
    for rb, name in freqs:
        cells = []
        for _, c in costs:
            r = stats(run(ret, va, rb, vol_scale=False, rt_cost=c), name)
            grid[(name, c)] = r["cagr"] if r else float("nan")
            cells.append(grid[(name, c)])
        print(f"{name:<14}" + "".join(f"{v*100:>9.1f}%" for v in cells))
    for _, chi in [costs[-1]]:
        best = max(freqs, key=lambda f: grid[(f[1], chi)])[1]
        print(f"  -> at {costs[-1][0]}, best frequency = {best}")


if __name__ == "__main__":
    main()
