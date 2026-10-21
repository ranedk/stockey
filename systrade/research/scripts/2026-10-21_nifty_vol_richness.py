"""LEDGER row 66 -- EXPLORATION (trials=0): trade volatility both ways? (TODO G-goal)

Row 65: weekly Nifty straddles are priced about at what Nifty then delivers, on AVERAGE.
A volatility trader does not trade the average: sell when options are rich against
how much Nifty has actually been moving, buy when they are cheap, stay out in between.
Does that sorting exist? For each weekly expiry, the ATM straddle at two windows:

  A  15:15 two sessions before expiry -> expiry close   (row 64's A entry)
  C  09:30 on expiry day -> expiry close                 (row 64's C entry)

richness = straddle premium / the straddle a recent-movement forecast would charge
         = premium / (0.8 x RV x sqrt(window / 252) x spot)
with two forecasts of RV: 10-session close-to-close, and 5-session realised from 5-minute
returns (plus the overnight gaps). Then payout/premium by richness fifth: if the cheap
fifth pays out well above 1 and the rich fifth well below, a two-way rule exists to
pre-register. Gross of costs; no significance claim; no parameter is picked here.

In-sample only: expiries 2021-10-21..2024-10-31. The check period is not read.
Run: stockey/.xstockey/bin/python research/scripts/2026-10-21_nifty_vol_richness.py
"""

import importlib.util
import os
from datetime import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
spec = importlib.util.spec_from_file_location("r64", os.path.join(HERE, "2026-10-20_nifty_option_selling.py"))
r64 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r64)
q = r64.q
LO, HI = "2021-10-21", "2024-10-31"  # do not move HI: the check period starts after it
WINDOWS = {"A 15:15, 2 sessions out": (2, time(15, 15), 2.0 + 0.25 / 6.25),
           "C 09:30, expiry day": (0, time(9, 30), 6.0 / 6.25)}  # sessions of movement left


def spot_bars():
    sp = q("SELECT ts, spot FROM dhan_option_rolling WHERE underlying = 'NIFTY' AND expiry_flag = 'WEEK' "
           "AND expiry_code = 1 AND interval_minutes = 1 AND strike_offset = 0 AND option_type = 'CE' "
           "AND ts >= '2021-09-01' AND ts < '2024-11-01' AND spot > 0")
    sp["ts"] = pd.to_datetime(sp["ts"], utc=True).dt.tz_convert("Asia/Kolkata")
    return sp.sort_values("ts").set_index("ts")["spot"]


def intraday_rv(sp):
    """Per session: sum of squared 5-minute log returns + the squared overnight gap (daily variance)."""
    five = sp.resample("5min").last().dropna()
    r = np.log(five).diff()
    # a day's first 5-minute return runs from the previous close, so it carries the overnight gap
    return (r**2).groupby(r.index.date).sum().rename("var")


def main():
    sessions, closes = r64.sessions_and_closes()
    sp = spot_bars()
    dvar = intraday_rv(sp)
    cl = pd.Series(closes)
    cl.index = pd.Index(cl.index)
    lr = np.log(cl).diff()
    exps = r64.expiries(LO, HI)
    out = [f"LEDGER 66 -- volatility richness, EXPLORATION, expiries {LO}..{HI} (gross of costs)"]
    for name, (back, at, horizon) in WINDOWS.items():
        rows = []
        for x in exps:
            if x not in closes:
                continue
            i = sessions.index(x)
            day = sessions[i - back]
            chain, atm = r64.chain_at(day, at)
            if atm is None or ("CE", atm) not in chain or ("PE", atm) not in chain:
                continue
            prem = chain[("CE", atm)][0] + chain[("PE", atm)][0]
            j = sessions.index(day)  # forecasts use sessions strictly before the entry day
            rv10 = lr.reindex(sessions[j - 10:j]).std() * np.sqrt(252)
            v5 = dvar.reindex(sessions[j - 5:j]).dropna()
            rv5 = np.sqrt(v5.mean() * 252) if len(v5) >= 4 else np.nan
            k = 0.8 * np.sqrt(horizon / 252) * atm  # the ATM strike stands in for spot
            rows.append({"expiry": x, "prem": prem, "pay": abs(closes[x] - atm),
                         "rich10": prem / (k * rv10), "rich5": prem / (k * rv5)})
        df = pd.DataFrame(rows).dropna()
        out.append(f"\n{name}: {len(df)} weeks, payout/premium overall {df['pay'].sum() / df['prem'].sum():.2f}")
        for col, label in [("rich10", "vs 10-session close-to-close"), ("rich5", "vs 5-session intraday")]:
            df["q"] = pd.qcut(df[col], 5, labels=["cheapest", "2", "3", "4", "richest"])
            out.append(f"  richness {label}:  (median richness {df[col].median():.2f})")
            for k_, g in df.groupby("q", observed=True):
                yrs = g.groupby(pd.to_datetime(g["expiry"]).dt.year).apply(
                    lambda h: h["pay"].sum() / h["prem"].sum())
                out.append(f"    {k_:<9} n={len(g):3d}  richness {g[col].min():4.2f}-{g[col].max():4.2f}  "
                           f"payout/premium {g['pay'].sum() / g['prem'].sum():4.2f}  hit(pay<prem) "
                           f"{100*(g['pay'] < g['prem']).mean():3.0f}%   by year "
                           + " ".join(f"{y}:{v:.2f}" for y, v in yrs.items()))
            rho = df[[col, "pay"]].assign(r=df["pay"] / df["prem"])[[col, "r"]].corr("spearman").iloc[0, 1]
            out.append(f"    rank correlation richness vs payout/premium {rho:+.2f}  (a usable sort is clearly negative)")
    text = "\n".join(out)
    print(text)
    path = os.path.join(ROOT, "research", "reports", "2026-10-21_nifty_vol_richness.txt")
    open(path, "w").write(text + "\n")
    print(f"\nwritten {path}")


if __name__ == "__main__":
    main()
