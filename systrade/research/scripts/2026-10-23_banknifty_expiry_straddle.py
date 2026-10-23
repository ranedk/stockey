"""LEDGER row 80 -- E1 (row 78's expiry-day stopped straddle basket) on Bank Nifty, once
(research/preregistrations/2026-10-23_banknifty_expiry_straddle.md, trials=1), expiry sessions
2021-10-21..2026-09-30, rules exactly the library's short_straddle() with 1 point slippage.

Run: stockey/.xstockey/bin/python research/scripts/2026-10-23_banknifty_expiry_straddle.py --mechanics
     (counts only, no P&L, no burn), then the same without --mechanics (burns the test)
"""

import argparse
import importlib.util
import json
import os
import sys
from datetime import datetime, time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
BURNS = os.path.join(ROOT, "research", "holdout_burns.json")
BURN_NAME = "banknifty-expiry-straddle-row80-2021-10-to-2026-09"
LO, HI = pd.Timestamp("2021-10-21").date(), pd.Timestamp("2026-09-30").date()
LAST_WEEKLY = pd.Timestamp("2024-11-13").date()
SLIP = 1.0
MEMBERS = [("0920", 1.25), ("0920", 1.5), ("1000", 1.25), ("1000", 1.5), ("1100", 1.25), ("1100", 1.5)]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, file))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


lib = load("lib", "2026-10-22_nifty_algo_library.py")
rot = load("rot", "2026-10-22_nifty_algo_rotation.py")
r64, r73 = lib.r64, lib.r73
q = r64.q


def expiry_sessions(sessions):
    """Bank Nifty label -> session it expired, keeping only labels that traded on that session."""
    df = q("SELECT expiry, trade_date FROM nseindia_fo_bhavcopy WHERE symbol = 'BANKNIFTY' AND instrument = 'OPTIDX' "
           "AND expiry BETWEEN %(lo)s AND %(hi)s AND trade_date BETWEEN %(tlo)s AND %(hi)s GROUP BY 1, 2",
           {"lo": LO, "hi": pd.Timestamp(HI) + pd.Timedelta(days=14), "tlo": pd.Timestamp(LO) - pd.Timedelta(days=60)})
    traded = set(df.itertuples(index=False, name=None))
    idx = pd.Index(sessions)
    out, phantom = {}, []
    for x in sorted(set(df["expiry"])):
        s = sessions[idx.searchsorted(x, side="right") - 1]
        if (x, s) in traded:
            out[x] = s
        else:
            phantom.append(x)
    return out, phantom


def lot_for(label, bh):
    if label in bh:
        return bh[label]
    return 25.0 if label <= pd.Timestamp("2023-06-30").date() else 15.0


def day_bars(day, flag):
    df = q("SELECT strike_offset, option_type, ts, strike, spot, open, high, close FROM dhan_option_rolling "
           "WHERE underlying = 'BANKNIFTY' AND expiry_flag = %(f)s AND expiry_code = 1 AND interval_minutes = 1 "
           "AND ts >= %(s)s AND ts < %(e)s",
           {"f": flag, "s": r73.ts(day, time(9, 0)).to_pydatetime(), "e": r73.ts(day, time(15, 31)).to_pydatetime()})
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df["end"] = df["ts"] + pd.Timedelta(minutes=1)
    return df


def basket(day, b, lot):
    v = [lib.short_straddle(day, b, lib.ENTRIES[e], st, lot) for e, st in MEMBERS]
    return None if any(x is None for x in v) else v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mechanics", action="store_true")
    args = ap.parse_args()
    lib.SLIP = SLIP
    sessions, _ = r64.sessions_and_closes()
    cmap, phantom = expiry_sessions(sessions)
    bh = {r.expiry: float(r.lot) for r in q(
        "SELECT expiry, max(lot_size) AS lot FROM nseindia_fo_bhavcopy WHERE symbol = 'BANKNIFTY' AND instrument = 'OPTIDX' "
        "AND trade_date >= '2024-07-01' AND expiry >= '2024-07-01' GROUP BY expiry").itertuples() if pd.notna(r.lot)}
    xdays = {s: x for x, s in cmap.items() if LO <= s <= HI}
    out = [f"LEDGER 80, Bank Nifty expiry sessions {min(xdays)}..{max(xdays)}: {len(xdays)} "
           f"(weekly era {sum(d <= LAST_WEEKLY for d in xdays)}, monthly era {sum(d > LAST_WEEKLY for d in xdays)}); "
           f"phantom labels dropped {[str(p) for p in phantom if LO <= p <= HI]}"]
    if not args.mechanics:
        burns = json.load(open(BURNS))
        if any(b["name"] == BURN_NAME for b in burns):
            sys.exit(f"{BURN_NAME} is already burned: the test runs once")
        burns.append({"name": BURN_NAME, "configHash": "preregistrations/2026-10-23_banknifty_expiry_straddle.md",
                      "burnedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
                      "note": "LEDGER row 80's once-only run, Bank Nifty expiry sessions 2021-10-21..2026-09-30."})
        json.dump(burns, open(BURNS, "w"), indent=2)
    res, skips, miss, wd, chk = {}, [], 0, [], []
    for d in sorted(xdays):
        label = xdays[d]
        flag = "WEEK" if d <= LAST_WEEKLY else "MONTH"
        b = day_bars(d, flag)
        if b.empty:
            skips.append(f"no {flag}1 bars")
            continue
        wd.append(pd.Timestamp(d).day_name()[:3])
        if args.mechanics:
            # the series must have an ATM bar at the first entry and run to the exit time
            k, spot = lib.atm_at(b, r73.ts(d, lib.ENTRIES["0920"]))
            if k is None:
                skips.append("no ATM bar at 09:20")
            if b["end"].max() < r73.ts(d, lib.OUT):
                skips.append("bars end before 15:15")
            if d > LAST_WEEKLY and len(chk) < 400:
                w = day_bars(d, "WEEK")
                chk.append(len(w))
            continue
        v = basket(d, b, lot_for(label, bh))
        if v is None:
            skips.append("a member did not trade")
            continue
        miss += sum(x["missing"] for x in v)
        res[d] = [x["r"] for x in v]
    out.append(f"skips {pd.Series(skips).value_counts().to_dict()}; weekdays {pd.Series(wd).value_counts().to_dict()}")
    if args.mechanics:
        out.append(f"monthly era: WEEK1 bars on those days (rows per day) min {min(chk) if chk else '-'} "
                   f"max {max(chk) if chk else '-'} -- WEEK1 and MONTH1 should then be the same contract")
        print("\n".join(out))
        return
    df = pd.DataFrame(res, index=[f"S-{e}-{int(round((s - 1) * 100))}" for e, s in MEMBERS]).T.sort_index()
    x = df.mean(axis=1)
    n = len(x)
    p = r64.bootstrap_p(x.values)
    h1, h2 = x.iloc[: n // 2].mean(), x.iloc[n // 2:].mean()
    mon = x[[d > LAST_WEEKLY for d in x.index]]
    per_year = n / ((x.index[-1] - x.index[0]).days / 365.25)
    sc = 0.10 / (x.std() * np.sqrt(per_year))
    dd = rot.maxdd((sc * x).values)
    checks = [("mean > 0, p < 0.10", x.mean() > 0 and p < 0.10),
              ("both halves positive", h1 > 0 and h2 > 0),
              ("maxDD within 35%", dd >= -0.35),
              ("monthly era positive", mon.mean() > 0)]
    out.append(f"legs missing at exit: {miss}")
    out.append("")
    out.append(f"E1 on Bank Nifty: n {n}  mean {1e4 * x.mean():+.2f} bps of notional per expiry "
               f"(t {x.mean() / x.std() * np.sqrt(n):+.2f})  hit {100 * (x > 0).mean():.0f}%  p {p:.4f}")
    out.append(f"   halves by count: {x.index[0]}..{x.index[n // 2 - 1]} {1e4 * h1:+.2f}   "
               f"{x.index[n // 2]}..{x.index[-1]} {1e4 * h2:+.2f}")
    out.append(f"   monthly-only era (after {LAST_WEEKLY}): n {len(mon)} mean {1e4 * mon.mean():+.2f}  "
               f"(weekly era {1e4 * x[[d <= LAST_WEEKLY for d in x.index]].mean():+.2f})")
    out.append(f"   at 10% vol (scale {sc:.1f}x): {100 * sc * x.mean() * per_year:+.1f}%/yr, maxDD {100 * dd:.1f}%")
    out.append("\nVERDICT: " + ("PASS" if all(ok for _, ok in checks) else "FAIL") + "  (" +
               "; ".join(f"{k}: {'yes' if ok else 'NO'}" for k, ok in checks) + ")")
    out.append("\nreported, not gated:")
    out.append("  members (bps): " + "  ".join(f"{k} {1e4 * v:+.1f}" for k, v in df.mean().items()))
    out.append("  by year: " + "  ".join(f"{y} {1e4 * g.mean():+.2f} (n {len(g)})" for y, g in
                                         x.groupby([d.year for d in x.index])))
    for slip in (0.5, 2.0):
        lib.SLIP = slip
        v = []
        for d in x.index:
            b = day_bars(d, "WEEK" if d <= LAST_WEEKLY else "MONTH")
            v.append(np.mean([r["r"] for r in basket(d, b, lot_for(xdays[d], bh))]))
        out.append(f"  at {slip} pt slippage: {1e4 * np.mean(v):+.2f} bps")
    lib.SLIP = SLIP
    # the Nifty contrast: same basket on Bank Nifty's non-expiry sessions of the weekly era
    ne = []
    for d in [d for d in sessions if LO <= d <= LAST_WEEKLY and d not in xdays]:
        nxt = [s for s in xdays if s > d]
        if not nxt:
            continue
        b = day_bars(d, "WEEK")
        if b.empty:
            continue
        v = basket(d, b, lot_for(xdays[min(nxt)], bh))
        if v is not None:
            ne.append(np.mean([r["r"] for r in v]))
    out.append(f"  non-expiry sessions, weekly era (Nifty contrast about −1): {1e4 * np.mean(ne):+.2f} bps (n {len(ne)})")
    # correlation with Nifty E1 where both expired the same session
    lib_SLIP = lib.SLIP
    lib.SLIP = 0.5
    nmap = r73.contract_map(sessions, str(LO), str(HI))
    nlots = r73.lots()
    both = sorted(set(x.index) & set(nmap.values()))
    pair = []
    for d in both:
        label = r73.week1(d, nmap)[1]
        b = lib.day_bars(d)
        if b.empty:
            continue
        v = [lib.short_straddle(d, b, lib.ENTRIES[e], st, nlots.get(label, 50.0)) for e, st in MEMBERS]
        if all(v):
            pair.append((x[d], np.mean([r["r"] for r in v])))
    lib.SLIP = lib_SLIP
    if len(pair) > 2:
        a = np.array(pair)
        out.append(f"  correlation with Nifty E1 on {len(a)} shared expiry sessions: {np.corrcoef(a.T)[0, 1]:+.2f}")
    text = "\n".join(out)
    print(text)
    path = os.path.join(ROOT, "research", "reports", "2026-10-23_banknifty_expiry_straddle.txt")
    open(path, "w").write(text + "\n")
    print(f"\nwritten {path}")


if __name__ == "__main__":
    np.seterr(all="ignore")
    main()
