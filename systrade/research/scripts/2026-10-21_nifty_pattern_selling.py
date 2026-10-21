"""LEDGER row 70 -- EXPLORATION (trials=0): do chart patterns tell an option seller when to sell?
(TODO G-goal step 3, the operator's contacts: they sell Nifty options, enter and exit on
patterns with a stop, and stay out when patterns are likely to fail -- elections, global
events, trend reversals.)

Rows 64-68: Nifty options are about fairly priced, so a seller's edge, if any, is in WHEN.
Unit: every session, sell the ATM WEEK1 straddle at 15:15, buy the same strikes back at the
next session's 15:15 (Dhan rolling-option 1-minute closes; no entry on an expiry day, when
the contract dies before the next session). P&L gross, in points and as % of premium.

Each entry day carries labels from Nifty's DAILY bars, all known at the 15:30 close before
(the bars are the day's own -- a 15:15 entry sees all but the last 15 minutes; accepted for
an exploration and stated). The list is fixed here, before any result:

  NR7          today's high-low range is the narrowest of the last 7 sessions
  inside       today's high < yesterday's high and low > yesterday's low
  big move     |today's return| > 1.5x the move VIX implied at yesterday's close
  gap          |open - yesterday's close| > 0.5%
  reversal     the close crossed its 20-day average within the last 3 sessions
  steady trend above a rising 20-day average or below a falling one (10-session slope), no
               cross in the last 3 sessions
  tight range  the last 10 sessions' high-low range below its trailing 250-session median
  event next   an RBI / Budget / election day is the next session (row 65's list)

Reported: mean P&L as % of premium, hit rate, n, and the per-year split, against all days.
No significance claim. In-sample only (entries 2021-10-21..2024-10-30); check period unread.
Run: stockey/.xstockey/bin/python research/scripts/2026-10-21_nifty_pattern_selling.py
"""

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


r64 = load("r64", "2026-10-20_nifty_option_selling.py")
ev = load("ev", "2026-10-21_nifty_event_study.py")
q = r64.q
LO, HI = pd.Timestamp("2021-10-21").date(), pd.Timestamp("2024-10-31").date()  # HI: the check starts after
AT = time(15, 15)
LABELS = ["NR7", "inside", "big move", "gap", "reversal", "steady trend", "tight range", "event next"]


def daily_bars():
    df = q("SELECT date::date AS d, open, high, low, close FROM nseindia_indices WHERE index_name = 'Nifty 50' "
           "AND date BETWEEN '2020-06-01' AND %(e)s ORDER BY date", {"e": HI})
    df = df[~df["d"].isin(r64.JUNK_INDEX_DATES)].drop_duplicates("d").set_index("d")
    vix = q("SELECT date::date AS d, close FROM nseindia_indices WHERE index_name = 'India VIX' "
            "AND date BETWEEN '2020-06-01' AND %(e)s", {"e": HI}).drop_duplicates("d").set_index("d")["close"]
    df["vix"] = vix
    return df


def labels(b, sessions):
    rng = b["high"] - b["low"]
    ret = np.log(b["close"]).diff()
    imp = b["vix"].shift(1) / 100 / np.sqrt(252)
    sma = b["close"].rolling(20).mean()
    side = np.sign(b["close"] - sma)
    crossed = (side != side.shift(1)) & side.shift(1).notna()
    recent_cross = crossed.rolling(3).max().astype(bool)
    slope = sma - sma.shift(10)
    r10 = b["high"].rolling(10).max() / b["low"].rolling(10).min() - 1
    events = ev.to_session(ev.RBI, sessions) | ev.to_session(ev.BUDGET, sessions) | ev.to_session(ev.ELECTION, sessions)
    nxt = pd.Series(list(b.index[1:]) + [None], index=b.index)
    return pd.DataFrame({
        "NR7": rng == rng.rolling(7).min(),
        "inside": (b["high"] < b["high"].shift(1)) & (b["low"] > b["low"].shift(1)),
        "big move": ret.abs() > 1.5 * imp,
        "gap": (b["open"] / b["close"].shift(1) - 1).abs() > 0.005,
        "reversal": recent_cross,
        "steady trend": ~recent_cross & (((side > 0) & (slope > 0)) | ((side < 0) & (slope < 0))),
        "tight range": r10 < r10.rolling(250).median(),
        "event next": nxt.isin(events),
    }, index=b.index)


def main():
    b = daily_bars()
    sessions = list(b.index)
    lab = labels(b, sessions)
    exp = set(r64.expiries(str(LO), str(HI)))
    rows = []
    for i, d in enumerate(sessions[:-1]):
        if d < LO or d in exp:
            continue
        n = sessions[i + 1]
        c0, atm = r64.chain_at(d, AT)
        if atm is None or ("CE", atm) not in c0 or ("PE", atm) not in c0:
            continue
        c1, _ = r64.chain_at(n, AT)
        if ("CE", atm) not in c1 or ("PE", atm) not in c1:
            continue
        p0 = c0[("CE", atm)][0] + c0[("PE", atm)][0]
        p1 = c1[("CE", atm)][0] + c1[("PE", atm)][0]
        cm, _ = r64.chain_at(n, time(9, 30))  # the same strikes at 09:30 next day: night vs day
        pm = cm[("CE", atm)][0] + cm[("PE", atm)][0] if ("CE", atm) in cm and ("PE", atm) in cm else np.nan
        rows.append({"d": d, "prem": p0, "pnl": p0 - p1, "night": p0 - pm, "day": pm - p1, **lab.loc[d].to_dict()})
    df = pd.DataFrame(rows)
    df["pct"] = df["pnl"] / df["prem"]
    df["year"] = pd.to_datetime(df["d"]).dt.year

    def line(g, name):
        yrs = g.groupby("year")["pct"].mean()
        return (f"  {name:<14} n={len(g):4d}  mean {100 * g['pct'].mean():+6.2f}% of premium  "
                f"({g['pnl'].mean():+5.1f} pts)  hit {100 * (g['pnl'] > 0).mean():3.0f}%  worst {100 * g['pct'].min():+5.0f}%"
                f"   by year " + " ".join(f"{y}:{100 * v:+.1f}" for y, v in yrs.items()))

    out = [f"LEDGER 70 -- one-day ATM straddle selling by pattern, EXPLORATION, entries {LO}..{df['d'].max()} (gross)",
           "  a 1-day sale must clear costs of roughly 1-2% of premium a round trip (two legs, in and out)", ""]
    out.append(line(df, "all days"))
    for k in LABELS:
        out.append(line(df[df[k]], k))
        out.append(line(df[~df[k]], f"  not {k}"))
    # Where the all-days result comes from (added after the first run showed all days at +4%
    # while row 64 held to expiry broke even): calendar days held, and sessions to expiry.
    out.append("\n  where the all-days number comes from:")
    df["wd"] = pd.to_datetime(df["d"]).dt.day_name().str[:3]
    nxt = {d: sessions[i + 1] for i, d in enumerate(sessions[:-1])}
    df["cal"] = [(nxt[d] - d).days for d in df["d"]]
    exps = sorted(exp)
    df["to_exp"] = [sum(1 for s_ in sessions if d < s_ <= min(e for e in exps if e > d)) for d in df["d"]]
    for k, g in df.groupby("wd"):
        out.append(line(g, f"entry {k}"))
    for k, g in df.groupby("cal"):
        out.append(line(g, f"{k} cal. day(s)"))
    for k, g in df.groupby(df["to_exp"].clip(upper=5)):
        out.append(line(g, f"{k}{'+' if k == 5 else ''} sess. to exp"))
    # The 1-day trade split at 09:30 next session, same strikes (Muravyev & Ni 2020: option
    # returns are negative overnight, positive intraday -- sellers are paid at night).
    out.append("\n  overnight (15:15 -> 09:30) vs daytime (09:30 -> 15:15), same strikes, % of entry premium:")
    sp = df.dropna(subset=["night"])
    for k, g in [("all", sp)] + [(f"{w}", g) for w, g in sp.groupby("wd")]:
        out.append(f"  {k:<5} n={len(g):4d}  night {100 * (g['night'] / g['prem']).mean():+6.2f}% "
                   f"(hit {100 * (g['night'] > 0).mean():3.0f}%)   day {100 * (g['day'] / g['prem']).mean():+6.2f}% "
                   f"(hit {100 * (g['day'] > 0).mean():3.0f}%)   by year night "
                   + " ".join(f"{y}:{100 * v:+.1f}" for y, v in (g['night'] / g['prem']).groupby(g['year']).mean().items()))
    text = "\n".join(out)
    print(text)
    path = os.path.join(ROOT, "research", "reports", "2026-10-21_nifty_pattern_selling.txt")
    open(path, "w").write(text + "\n")
    print(f"\nwritten {path}")


if __name__ == "__main__":
    main()
