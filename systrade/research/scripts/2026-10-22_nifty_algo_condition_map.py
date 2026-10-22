"""LEDGER row 77 -- EXPLORATION (trials=0): does each of row 76's 36 Nifty algos earn in some market
condition we can read BEFORE the trade, and lose in the others? (operator, 2026-10-22: "identify when an
algo is working and enter, exit when it stops"; row 76 showed the algo's own slow scorecard cannot.)

Conditions, each split into named cells, all known before the session's first entry:
  vix          India VIX at the previous close, terciles over the period
  vix_5d       VIX change over the last 5 sessions, terciles (falling / flat / rising)
  trend_20d    Nifty return over the last 20 sessions, terciles (down / flat / up)
  rv_vs_vix    10-session realised vol (close to close, annualised) / previous VIX, terciles (moved less
               than priced / about / more)
  prev_range   previous session's (high - low) / close, terciles (volatility clustering)
  gap          |open / previous close - 1|, terciles (known at 09:15; not for T, decided at the close)
  expiry       weekly expiry session or not
  own_10d,     the member's own known results over the last 10 / 20 sessions: positive or not
  own_20d      (the fast scorecard; O members lag a session, as row 76)
Per member x condition: mean net bps per day in each cell (raw, per unit notional, traded days only),
the best cell's t, and a permutation p (2,000 shuffles of the cell labels, seed 1) for the spread
best - worst cell. BH across all member x condition tests is printed as a guide only.
In-sample library only (2021-10..2024-10); the 2024-11..2026-09 check stays unseen.
Run: stockey/.xstockey/bin/python research/scripts/2026-10-22_nifty_algo_condition_map.py
"""

import importlib.util
import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
REPS = 2000


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, file))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


lib = load("lib", "2026-10-22_nifty_algo_library.py")
rot = load("rot", "2026-10-22_nifty_algo_rotation.py")
r64, r73 = lib.r64, lib.r73


def terciles(x, labels):
    return pd.qcut(x, 3, labels=labels).astype(object)


def conditions(days):
    ix = r64.q("SELECT date::date AS d, index_name, open, high, low, close FROM nseindia_indices "
               "WHERE index_name IN ('Nifty 50', 'India VIX') AND date >= '2021-06-01' AND date <= '2024-10-31' "
               "ORDER BY date")
    ix = ix[~ix["d"].isin(r64.JUNK_INDEX_DATES)].drop_duplicates(["d", "index_name"])
    n = ix[ix.index_name == "Nifty 50"].set_index("d")
    v = ix[ix.index_name == "India VIX"].set_index("d")["close"].reindex(n.index).ffill()
    ret = n["close"].pct_change()
    c = pd.DataFrame(index=n.index)
    c["vix"] = v.shift(1)
    c["vix_5d"] = (v / v.shift(5) - 1).shift(1)
    c["trend_20d"] = (n["close"] / n["close"].shift(20) - 1).shift(1)
    c["rv_vs_vix"] = (ret.rolling(10).std() * np.sqrt(252) * 100 / v).shift(1)
    c["prev_range"] = ((n["high"] - n["low"]) / n["close"]).shift(1)
    c["gap"] = (n["open"] / n["close"].shift(1) - 1).abs()
    c = c.reindex(days)
    out = pd.DataFrame(index=days)
    out["vix"] = terciles(c["vix"], ["low", "mid", "high"])
    out["vix_5d"] = terciles(c["vix_5d"], ["falling", "flat", "rising"])
    out["trend_20d"] = terciles(c["trend_20d"], ["down", "flat", "up"])
    out["rv_vs_vix"] = terciles(c["rv_vs_vix"], ["moved less", "about", "moved more"])
    out["prev_range"] = terciles(c["prev_range"], ["narrow", "mid", "wide"])
    out["gap"] = terciles(c["gap"], ["small", "mid", "big"])
    sessions, _ = r64.sessions_and_closes()
    cmap = r73.contract_map(sessions, str(days[0]), str(days[-1]))
    xs = set(cmap.values())
    out["expiry"] = ["yes" if d in xs else "no" for d in days]
    return out


def perm_spread(x, lab, rng):
    cells = sorted(set(lab))
    codes = np.array([cells.index(v) for v in lab])

    def spread(cd):
        m = np.array([x[cd == k].mean() for k in range(len(cells))])
        return m.max() - m.min()
    obs = spread(codes)
    sims = np.array([spread(rng.permutation(codes)) for _ in range(REPS)])
    return obs, float((sims >= obs).mean())


def main():
    raw = pd.read_csv(os.path.join(ROOT, "research", "reports", "2026-10-22_nifty_algo_library_insample.csv"),
                      index_col="day", parse_dates=["day"])
    raw.index = raw.index.date
    raw = raw[lib.members()]
    days = list(raw.index)
    cond = conditions(days)
    book = rot.Book(raw, {})
    for w in (10, 20):
        known = pd.DataFrame(book.K, index=days, columns=raw.columns).rolling(w).sum()
        cond = cond.join(known.gt(0).replace({True: "pos", False: "neg"}).where(known.notna()).add_prefix(f"own_{w}d|"))
    rng = np.random.default_rng(1)
    rows = []
    for m in raw.columns:
        traded = raw[m] != 0  # a member's non-trading day is 0 in the library; cells use traded days only
        for cname in ("vix", "vix_5d", "trend_20d", "rv_vs_vix", "prev_range", "gap", "expiry", "own_10d", "own_20d"):
            if cname == "gap" and m.startswith("T-"):
                continue
            col = cond[f"{cname}|{m}"] if cname.startswith("own") else cond[cname]
            ok = traded & col.notna()
            x, lab = raw.loc[ok, m].values, col[ok].values
            cells = {k: x[lab == k] for k in sorted(set(lab))}
            if min(len(v) for v in cells.values()) < 30:
                continue
            obs, p = perm_spread(x, lab, rng)
            best = max(cells, key=lambda k: cells[k].mean())
            b = cells[best]
            rows.append({"member": m, "cond": cname, "cells": "  ".join(f"{k} {1e4 * v.mean():+.1f}" for k, v in cells.items()),
                         "all": 1e4 * x.mean(), "best": best, "best_bps": 1e4 * b.mean(), "best_n": len(b),
                         "best_t": b.mean() / (b.std(ddof=1) / np.sqrt(len(b))), "spread": 1e4 * obs, "p": p})
    df = pd.DataFrame(rows)
    df["q"] = r64.bh(df["p"].values)
    out = ["LEDGER 77 -- condition map of row 76's 36 Nifty algos (EXPLORATION, trials=0), 2021-10..2024-10",
           "net bps of notional per traded day by cell; p = permutation p of (best cell - worst cell); q = BH over all "
           f"{len(df)} member x condition tests (a guide, not a claim)", ""]
    out.append("A. Cells where a member EARNS after costs: best cell mean > 0 with t > 2 (sorted by t)")
    hits = df[(df.best_bps > 0) & (df.best_t > 2)].sort_values("best_t", ascending=False)
    for r in hits.itertuples():
        out.append(f"  {r.member:24s} {r.cond:10s} best '{r.best}' {r.best_bps:+6.1f} bps (n {r.best_n}, t {r.best_t:+.2f}); "
                   f"all days {r.all:+.1f};  cells: {r.cells};  spread p {r.p:.3f} q {r.q:.2f}")
    if hits.empty:
        out.append("  none")
    out.append("\nB. Strongest sorts (spread between cells), q < 0.10, whatever the sign")
    for r in df[df.q < 0.10].sort_values("p").itertuples():
        out.append(f"  {r.member:24s} {r.cond:10s} cells: {r.cells};  p {r.p:.4f} q {r.q:.3f}")
    out.append("\nC. By condition: how many members it sorts (p < 0.05) and how many get a cell earning with t > 2")
    for cname, g in df.groupby("cond", sort=False):
        out.append(f"  {cname:10s} {int((g.p < 0.05).sum()):2d}/{len(g)} sorted (5% expected by chance: {0.05 * len(g):.1f});"
                   f"  {int(((g.best_bps > 0) & (g.best_t > 2)).sum())} earning cells")
    out.append("\nD. Every member x condition (bps per traded day by cell)")
    for r in df.itertuples():
        out.append(f"  {r.member:24s} {r.cond:10s} {r.cells}   p {r.p:.3f}")
    text = "\n".join(out)
    path = os.path.join(ROOT, "research", "reports", "2026-10-22_nifty_algo_condition_map.txt")
    open(path, "w").write(text + "\n")
    print(text.split("\nD. Every member")[0])
    print(f"\nwritten {path}")


if __name__ == "__main__":
    main()
