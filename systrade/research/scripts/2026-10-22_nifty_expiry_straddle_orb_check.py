"""LEDGER row 78 -- the once-only check of E1 (expiry-day stopped straddle basket) and E2 (I-orb30)
(research/preregistrations/2026-10-22_nifty_expiry_straddle_and_orb.md, trials=2), sessions
2024-11-01..2026-09-30, rules exactly row 76's library definitions.

Run: stockey/.xstockey/bin/python research/scripts/2026-10-22_nifty_expiry_straddle_orb_check.py --mechanics
     (counts only, no P&L, no burn), then the same without --mechanics (burns the check)
"""

import argparse
import importlib.util
import json
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
BURNS = os.path.join(ROOT, "research", "holdout_burns.json")
BURN_NAME = "nifty-expiry-straddle-orb30-row78-check-2024-11-to-2026-09"
LO, HI = pd.Timestamp("2024-11-01").date(), pd.Timestamp("2026-09-30").date()
HALF = pd.Timestamp("2025-10-01").date()
E1 = ["S-0920-25", "S-0920-50", "S-1000-25", "S-1000-50", "S-1100-25", "S-1100-50"]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, file))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


lib = load("lib", "2026-10-22_nifty_algo_library.py")
rot = load("rot", "2026-10-22_nifty_algo_rotation.py")
r64, r73 = lib.r64, lib.r73


def insample_scales():
    """10%-vol scale per unit, fixed from the in-sample library (section 2.3)."""
    raw = pd.read_csv(os.path.join(ROOT, "research", "reports", "2026-10-22_nifty_algo_library_insample.csv"),
                      index_col="day", parse_dates=["day"])
    raw.index = raw.index.date
    sessions, _ = r64.sessions_and_closes()
    xs = set(r73.contract_map(sessions, str(raw.index[0]), str(raw.index[-1])).values())
    e1 = raw.loc[[d for d in raw.index if d in xs], E1].mean(axis=1)
    per_year = len(e1) / ((raw.index[-1] - raw.index[0]).days / 365.25)
    return 0.10 / (e1.std() * np.sqrt(per_year)), 0.10 / (raw["I-orb30"].std() * np.sqrt(252)), per_year


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mechanics", action="store_true")
    args = ap.parse_args()
    sessions, closes = r64.sessions_and_closes()
    days = [d for d in sessions if LO <= d <= HI]
    cmap = r73.contract_map(sessions, str(LO), str(HI))
    xs_all = set(cmap.values())
    lotmap = r73.lots()
    out = [f"LEDGER 78 check, sessions {days[0]}..{days[-1]} ({len(days)}), expiry sessions {sum(d in xs_all for d in days)}; "
           f"last index session in the DB {sessions[-1]}"]
    if not args.mechanics:
        burns = json.load(open(BURNS))
        if any(b["name"] == BURN_NAME for b in burns):
            sys.exit(f"{BURN_NAME} is already burned: the check runs once")
        burns.append({"name": BURN_NAME, "configHash": "preregistrations/2026-10-22_nifty_expiry_straddle_and_orb.md",
                      "burnedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
                      "note": "LEDGER row 78's once-only check, sessions 2024-11-01..2026-09-30."})
        json.dump(burns, open(BURNS, "w"), indent=2)
    e1, e2, nonexp, skips, miss = {}, {}, {}, [], 0
    lots_seen, wd_exp = set(), []
    for d in days:
        xs, label = r73.week1(d, cmap)
        i = sessions.index(d)
        if xs is None:
            skips.append("no contract")
            continue
        b = lib.day_bars(d)
        if b.empty:
            skips.append("no bars")
            continue
        lot = lotmap.get(label)
        if lot is None:
            skips.append("no lot size")
            continue
        lots_seen.add(lot)
        prev = closes.get(sessions[i - 1]) if i > 0 else None
        if args.mechanics:
            spot = lib.spot_series(b)
            if len(spot) < 300:
                skips.append("spot < 300 minutes")
            if d == xs:
                wd_exp.append(pd.Timestamp(d).day_name()[:3])
                if lib.atm_at(b, r73.ts(d, lib.ENTRIES["0920"]))[0] is None:
                    skips.append("expiry: no ATM bar at 09:20")
            continue
        legs = {}
        for e, at in lib.ENTRIES.items():
            for st, f in (("25", 1.25), ("50", 1.5)):
                legs[f"S-{e}-{st}"] = lib.short_straddle(d, b, at, f, lot)
        ok = [v for v in legs.values() if v]
        miss += sum(v["missing"] for v in ok)
        if len(ok) == len(E1):
            (e1 if d == xs else nonexp)[d] = {k: v["r"] for k, v in legs.items()}
        elif d == xs:
            skips.append("expiry: a member did not trade")
        e2[d] = lib.index_trades(d, b, prev).get("I-orb30", np.nan)
    sk = pd.Series(skips).value_counts().to_dict()
    out.append(f"skips {sk}; lot sizes seen {sorted(lots_seen)}")
    if args.mechanics:
        out.append(f"expiry weekdays {pd.Series(wd_exp).value_counts().to_dict()}")
        print("\n".join(out))
        return
    s1, s2, per_year = insample_scales()
    E1df = pd.DataFrame(e1).T.sort_index()
    x1 = E1df.mean(axis=1)
    x2 = pd.Series(e2).sort_index()
    out.append(f"E2 days without a usable spot path (no result): {int(x2.isna().sum())}; legs missing at exit (E1 members, all days): {miss}")
    x2 = x2.dropna()
    p1 = r64.bootstrap_p(x1.values)
    p2 = rot.block_p(x2.values)
    q1, q2 = r64.bh([p1, p2])
    out.append("")
    res = []
    for name, x, p, qq, sc, n_per in (("E1 expiry-day stopped straddle basket", x1, p1, q1, s1, per_year),
                                      ("E2 opening-range breakout, 30 min", x2, p2, q2, s2, 252)):
        h1, h2 = x[[d < HALF for d in x.index]].mean(), x[[d >= HALF for d in x.index]].mean()
        dd = rot.maxdd((sc * x).values)
        ok = x.mean() > 0 and qq < 0.10 and h1 > 0 and h2 > 0 and dd >= -0.35
        out.append(f"{name}: n {len(x)}  mean {1e4 * x.mean():+.2f} bps of notional (t {x.mean() / x.std() * np.sqrt(len(x)):+.2f})  "
                   f"hit {100 * (x > 0).mean():.0f}%  p {p:.4f}  q {qq:.4f}")
        out.append(f"   halves: 2024-11..2025-09 {1e4 * h1:+.2f}  2025-10..2026-09 {1e4 * h2:+.2f};  at 10% vol (scale {sc:.1f}x): "
                   f"{100 * sc * x.mean() * n_per:+.1f}%/yr, maxDD {100 * dd:.1f}%")
        res.append((name, ok, x.mean(), qq, h1, h2, dd))
    out.append("\nVERDICT (pass = mean > 0 with BH q < 0.10, both halves positive, maxDD within 35%):")
    for name, ok, m, qq, h1, h2, dd in res:
        out.append(f"  {name:40s} {'PASS' if ok else 'FAIL'}")
    out.append("\nreported, not gated:")
    out.append("  E1 members on expiry days (bps): " + "  ".join(f"{k} {1e4 * v:+.1f}" for k, v in E1df.mean().items()))
    ne = pd.DataFrame(nonexp).T
    out.append(f"  the same basket on NON-expiry days (in-sample contrast −0.98): {1e4 * ne.mean(axis=1).mean():+.2f} bps (n {len(ne)})")
    out.append("  E1 by expiry weekday: " + "  ".join(f"{k} {1e4 * v:+.2f} (n {c})" for (k, v), c in zip(
        x1.groupby([pd.Timestamp(d).day_name()[:3] for d in x1.index]).mean().items(),
        x1.groupby([pd.Timestamp(d).day_name()[:3] for d in x1.index]).size())))
    out.append("  E2 by weekday: " + "  ".join(f"{k} {1e4 * v:+.2f}" for k, v in
                                            x2.groupby([pd.Timestamp(d).day_name()[:3] for d in x2.index]).mean().items())
               + f";  expiry {1e4 * x2[[d in xs_all for d in x2.index]].mean():+.2f} vs not {1e4 * x2[[d not in xs_all for d in x2.index]].mean():+.2f};"
               f"  breakout days {100 * (x2 != 0).mean():.0f}%")
    for slip in (0.25, 1.0):
        lib.SLIP = slip
        v = [np.mean([lib.short_straddle(d, b, at, f, lotmap[r73.week1(d, cmap)[1]])["r"]
                      for at in lib.ENTRIES.values() for f in (1.25, 1.5)])
             for d in E1df.index for b in [lib.day_bars(d)]]
        out.append(f"  E1 at {slip} pt slippage: {1e4 * np.mean(v):+.2f} bps")
    lib.SLIP = 0.5
    text = "\n".join(out)
    print(text)
    path = os.path.join(ROOT, "research", "reports", "2026-10-22_nifty_expiry_straddle_orb_check.txt")
    open(path, "w").write(text + "\n")
    print(f"\nwritten {path}")


if __name__ == "__main__":
    main()
