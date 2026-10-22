"""LEDGER row 74 -- Nifty credit spreads with Dhan-style exits, and plain intraday selling, pre-registered
(research/preregistrations/2026-10-22_nifty_managed_credit_spreads.md, trials=3):

  at 10:00 sell the ATM call spread and put spread (wings 400), each managed on its own
  D1 intraday: take profit at value <= 0.5 x credit, stop at value >= 1.5 x credit, else 15:15
  D2 intraday unmanaged, 10:00 -> 15:15
  D3 as D1, held to the next session's 15:15 (no entry on an expiry day)

Entries 2021-10-21..2024-10-31 only; no check period (the rules came from 2025-26 trades).
Run: stockey/.xstockey/bin/python research/scripts/2026-10-22_nifty_managed_credit_spreads.py [--mechanics]
"""

import argparse
import importlib.util
import os
from datetime import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, file))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


r73 = load("r73", "2026-10-22_nifty_short_hold_selling.py")
r64 = r73.r64
LO, HI = pd.Timestamp("2021-10-21").date(), pd.Timestamp("2024-10-31").date()
AT, OUT, WING, TP, SL = time(10, 0), time(15, 15), 400, 0.5, 1.5
STALE = pd.Timedelta(minutes=5)
SLIPS = {"0.5 pt (verdict)": lambda p: 0.5, "0.25 pt": lambda p: 0.25, "1 pt": lambda p: 1.0,
         "old rule max(0.25, 1%)": lambda p: max(0.25, 0.01 * p)}
VERSIONS = {"D1 intraday, managed": (True, False), "D2 intraday, unmanaged": (False, False),
            "D3 overnight, managed": (True, True)}


def closes(bars, side, k):
    b = bars[(bars.option_type == side) & (bars.strike == k)].drop_duplicates("end").set_index("end")["close"]
    return b.sort_index()


def run(day, bars_today, nxt, bars_next, xs, label, lot, managed, overnight):
    t0 = r73.ts(day, AT)
    win = bars_today[(bars_today["end"] <= t0) & (bars_today["end"] > t0 - pd.Timedelta(minutes=r64.LOOKBACK_MIN))]
    atm = win[(win.strike_offset == 0) & (win.option_type == "CE")].sort_values("end")
    if atm.empty:
        return {"skip": "no ATM bar"}
    k = float(atm["strike"].iloc[-1])
    bars = pd.concat([bars_today, bars_next]) if overnight else bars_today
    t_end = r73.ts(nxt if overnight else day, OUT)
    legs = {"Cs": ("CE", k), "Cl": ("CE", k + WING), "Ps": ("PE", k), "Pl": ("PE", k - WING)}
    ser, p0 = {}, {}
    for name, (side, kk) in legs.items():
        s = closes(bars, side, kk)
        pre = s[(s.index <= t0) & (s.index > t0 - pd.Timedelta(minutes=r64.LOOKBACK_MIN))]
        if pre.empty:
            return {"skip": "missing entry leg"}
        p0[name] = float(pre.iloc[-1])
        ser[name] = s[(s.index > t0) & (s.index <= t_end)]
    grid = sorted(set().union(*[set(s.index) for s in ser.values()]))
    if not grid:
        return {"skip": "no bars after entry"}
    path = pd.DataFrame({n: s.reindex(grid) for n, s in ser.items()})
    last_seen = pd.DataFrame({n: pd.Series(np.where(path[n].notna(), grid, pd.NaT), index=grid) for n in path})
    last_seen = last_seen.ffill()
    path = path.ffill().fillna(pd.Series(p0))
    gross, txns, fees, exits, missing = 0.0, [], 0.0, [], 0
    credit = {"C": p0["Cs"] - p0["Cl"], "P": p0["Ps"] - p0["Pl"]}
    for sp in ("C", "P"):
        s_, l_ = sp + "s", sp + "l"
        c = credit[sp]
        v = path[s_] - path[l_]
        when, how = grid[-1], "time"
        if managed:
            hit = v[(v <= TP * c) | (v >= SL * c)]
            if not hit.empty:
                when = hit.index[0]
                how = "take profit" if v[when] <= TP * c else "stop"
        p_s, p_l = float(path.at[when, s_]), float(path.at[when, l_])
        stale_s = pd.isna(last_seen.at[when, s_]) or when - last_seen.at[when, s_] > STALE
        stale_l = pd.isna(last_seen.at[when, l_]) or when - last_seen.at[when, l_] > STALE
        if overnight and how == "time" and (stale_s or stale_l):
            b = r73.bhav(nxt, label, {legs[s_][1], legs[l_][1]})
            if (legs[s_][0], legs[s_][1]) in b and (legs[l_][0], legs[l_][1]) in b:
                p_s, p_l = b[legs[s_]], b[legs[l_]]
                how, stale_s, stale_l = "time (bhavcopy)", False, False
        if stale_l:
            p_l, missing = 0.0, missing + 1
        if stale_s:
            missing += 1
        gross += (p0[s_] - p_s) - (p0[l_] - p_l)
        exit_day = when.tz_convert(r64.IST).date()
        fees += (r73.fees(p0[s_], False, day, lot) + r73.fees(p0[l_], True, day, lot)
                 + r73.fees(p_s, True, exit_day, lot) + r73.fees(p_l, False, exit_day, lot))
        txns += [p0[s_], p0[l_], p_s, p_l]
        exits.append(how)
    risk = WING - credit["C"] - credit["P"]
    if risk <= 0:
        return {"skip": "credit above width"}
    return {"day": day, "credit": credit["C"] + credit["P"], "gross": gross, "fees": fees, "txns": txns,
            "risk": risk, "exits": exits, "missing": missing, "expiry_day": xs == day}


def summarise(name, res, slip, out):
    df = pd.DataFrame(res)
    df["pnl"] = df["gross"] - df["fees"] - df["txns"].apply(lambda t: sum(slip(p) for p in t))
    df["ret"] = df["pnl"] / df["risk"]
    x = df["ret"].values
    acct = np.cumsum(0.02 * x)
    dd = float((acct - np.maximum.accumulate(np.r_[0, acct])[1:]).min())
    yrs = df.groupby(pd.to_datetime(df["day"]).dt.year)["ret"].mean()
    p = r64.bootstrap_p(x)
    ex = pd.Series([e for es in df["exits"] for e in es]).value_counts(normalize=True)
    out.append(f"{name}: n={len(df)}  mean net {100 * x.mean():+.2f}% of risk  (gross {df['gross'].mean():+.2f} pts, "
               f"net {df['pnl'].mean():+.2f} pts, risk {df['risk'].mean():.0f})  hit {100 * (df['pnl'] > 0).mean():.0f}%  "
               f"worst {100 * x.min():+.0f}%  p {p:.4f}")
    out.append(f"   account (2% at risk, not compounded): total {100 * acct[-1]:+.1f}%, maxDD {100 * dd:.1f}%;  by year "
               + "  ".join(f"{y}: {100 * v:+.2f}%" for y, v in yrs.items()))
    out.append("   exits " + ", ".join(f"{k} {100 * v:.0f}%" for k, v in ex.items()) + f";  legs missing at exit {df['missing'].sum()}")
    e = df.groupby("expiry_day")["ret"].mean()
    df["wd"] = pd.to_datetime(df["day"]).dt.day_name().str[:3]
    out.append("   expiry day " + "  ".join(f"{'yes' if k else 'no'} {100 * v:+.2f}%" for k, v in e.items())
               + ";  by weekday " + "  ".join(f"{k} {100 * v:+.2f}%" for k, v in df.groupby("wd")["ret"].mean().items()))
    return p, int(sum(yrs.get(y, -1) > 0 for y in (2022, 2023, 2024))), dd, x.mean(), df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mechanics", action="store_true")
    args = ap.parse_args()
    sessions, closes_ = r64.sessions_and_closes()
    cmap = r73.contract_map(sessions, str(LO), str(HI))
    lotmap = r73.lots()
    days = [d for d in sessions if LO <= d <= HI]
    res = {v: [] for v in VERSIONS}
    cache = {}

    def bars(d):
        if d not in cache:
            cache.clear() if len(cache) > 2 else None
            cache[d] = r73.day_bars(d)
        return cache[d]
    for d in days:
        xs, label = r73.week1(d, cmap)
        i = sessions.index(d)
        if xs is None or i + 1 >= len(sessions):
            continue
        nxt, lot = sessions[i + 1], lotmap.get(label, 50.0)
        today = bars(d)
        if today.empty:
            for v in VERSIONS:
                res[v].append({"skip": "no bars"})
            continue
        for v, (managed, overnight) in VERSIONS.items():
            if overnight and xs == d:
                res[v].append({"skip": "expiry day"})
                continue
            res[v].append(run(d, today, nxt, bars(nxt) if overnight else None, xs, label, lot, managed, overnight))
    out = [f"LEDGER 74 -- Nifty credit spreads with Dhan-style exits, entries {LO}..{HI} (one ATM call spread + put spread, wings {WING})"]
    for v, r in res.items():
        out.append(f"{v}: {sum('gross' in x for x in r)} trades, skipped "
                   f"{pd.Series([x['skip'] for x in r if 'skip' in x]).value_counts().to_dict()}")
    if args.mechanics:
        print("\n".join(out))
        return
    out.append("")
    stats = {}
    for v, r in res.items():
        stats[v] = summarise(v, [x for x in r if "gross" in x], SLIPS["0.5 pt (verdict)"], out)
    qs = r64.bh([stats[v][0] for v in VERSIONS])
    out.append("\nVERDICT (pass = mean > 0 with BH q < 0.10, >= 2 of 3 full years positive, maxDD within 35%):")
    for v, qq in zip(VERSIONS, qs):
        p, ny, dd, m, _ = stats[v]
        ok = m > 0 and qq < 0.10 and ny >= 2 and dd >= -0.35
        out.append(f"  {v:24s} mean {100 * m:+.2f}%  q {qq:.4f}  years positive {ny}/3  maxDD {100 * dd:.1f}%  -> {'PASS' if ok else 'FAIL'}")
    out.append("\nslippage sensitivity, mean net % of risk (not gating):")
    for lab, f in SLIPS.items():
        row = []
        for v, r in res.items():
            df = pd.DataFrame([x for x in r if "gross" in x])
            pnl = df["gross"] - df["fees"] - df["txns"].apply(lambda t: sum(f(p) for p in t))
            row.append(f"{v.split()[0]} {100 * (pnl / df['risk']).mean():+.2f}%")
        out.append(f"  {lab:24s} " + "  ".join(row))
    text = "\n".join(out)
    print(text)
    path = os.path.join(ROOT, "research", "reports", "2026-10-22_nifty_managed_credit_spreads_insample.txt")
    open(path, "w").write(text + "\n")
    print(f"\nwritten {path}")


if __name__ == "__main__":
    main()
