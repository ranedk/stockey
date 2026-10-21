"""LEDGER row 67 -- EXPLORATION (trials=0): is the premium in MONTHLY Nifty options?

Row 61: India VIX sat above the volatility Nifty delivered over the next 21 sessions.
Row 65: WEEKLY straddles two sessions out are priced ~15% under VIX and paid out 0.98 of
their premium -- the gap is not there. Is it in the monthly contract instead?

For each monthly expiry (the last Nifty expiry of its calendar month), the ATM straddle
(Dhan MONTH1, offset 0, CE + PE, 15:15 bar) on the first session after the previous monthly
expiry ("roll", a full month, 18-23 sessions) and at 10 and 5 sessions before expiry, held
to the official close: payout/premium (below 1 = sellers were paid, gross of costs), the
straddle's price against VIX's, and by year. No significance claim; no parameter picked.

In-sample only: expiries 2021-10-21..2024-10-31. The check period is not read.
Run: stockey/.xstockey/bin/python research/scripts/2026-10-21_nifty_monthly_premium.py
"""

import importlib.util
import os
from datetime import datetime, time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
spec = importlib.util.spec_from_file_location("r64", os.path.join(HERE, "2026-10-20_nifty_option_selling.py"))
r64 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r64)
q = r64.q
LO, HI = "2021-10-21", "2024-10-31"  # do not move HI: the check period starts after it
BACKS = ["roll", 10, 5]  # not a fixed 20: MONTH1 is still LAST month's contract until it expires
AT = time(15, 15)


def monthly_expiries():
    """The last expiry of each calendar month among NIFTY option expiries."""
    e = pd.Series(sorted(r64.expiries(LO, HI)))
    m = pd.to_datetime(e).dt.to_period("M")
    return list(e.groupby(m.values).max())


def straddle(day):
    end = pd.Timestamp(datetime.combine(day, AT)).tz_localize(r64.IST)
    start = end - pd.Timedelta(minutes=r64.LOOKBACK_MIN)
    df = q("SELECT option_type, ts, strike, close FROM dhan_option_rolling "
           "WHERE underlying = 'NIFTY' AND expiry_flag = 'MONTH' AND expiry_code = 1 AND interval_minutes = 1 "
           "AND strike_offset = 0 AND ts > %(s)s AND ts <= %(e)s",
           {"s": start.tz_convert("UTC").to_pydatetime(), "e": end.tz_convert("UTC").to_pydatetime()})
    if df.empty:
        return None
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df[df["ts"] + pd.Timedelta(minutes=1) <= end.tz_convert("UTC")]
    last = df.sort_values("ts").groupby("option_type").tail(1).set_index("option_type")
    if set(last.index) != {"CE", "PE"} or last.at["CE", "strike"] != last.at["PE", "strike"]:
        return None
    return float(last.at["CE", "strike"]), float(last["close"].sum())


def main():
    sessions, closes = r64.sessions_and_closes()
    vix = q("SELECT date::date AS d, close FROM nseindia_indices WHERE index_name = 'India VIX' "
            "AND date BETWEEN '2021-06-01' AND %(e)s", {"e": HI}).drop_duplicates("d").set_index("d")["close"]
    exps = [x for x in monthly_expiries() if x in closes]
    out = [f"LEDGER 67 -- monthly Nifty straddles, EXPLORATION, {len(exps)} monthly expiries {LO}..{HI} (gross)"]
    for spec_ in BACKS:
        rows = []
        for x in exps:
            i = sessions.index(x)
            if spec_ == "roll":
                prev = [e for e in monthly_expiries() if e < x]
                if not prev or prev[-1] not in sessions:
                    continue
                back = i - sessions.index(prev[-1]) - 1
            else:
                back = spec_
            day = sessions[i - back]
            s = straddle(day)
            if s is None or day not in vix.index:
                continue
            atm, prem = s
            n_left = back + (15.5 - 15.25) / 6.25  # sessions of movement left after 15:15
            at_vix = 0.8 * vix[day] / 100 * np.sqrt(n_left / 252) * atm
            rows.append({"expiry": x, "prem": prem, "pay": abs(closes[x] - atm), "vixratio": prem / at_vix,
                         "pnl_pct": (prem - abs(closes[x] - atm)) / atm})
        df = pd.DataFrame(rows)
        yrs = df.groupby(pd.to_datetime(df["expiry"]).dt.year).apply(lambda h: h["pay"].sum() / h["prem"].sum())
        label = "first session after the previous monthly expiry" if spec_ == "roll" else f"{spec_} sessions out"
        out.append(f"\n{label}: {len(df)} months")
        out.append(f"   payout/premium {df['pay'].sum() / df['prem'].sum():.2f}  (below 1 = sellers paid)  "
                   f"months sellers won {100 * (df['pay'] < df['prem']).mean():.0f}%")
        out.append(f"   straddle priced at {df['vixratio'].mean():.2f}x VIX's  mean premium {df['prem'].mean():.0f} pts")
        out.append(f"   seller P&L per month as % of Nifty: mean {100 * df['pnl_pct'].mean():+.2f}%, "
                   f"worst {100 * df['pnl_pct'].min():+.2f}%, best {100 * df['pnl_pct'].max():+.2f}%")
        out.append("   by year payout/premium: " + "  ".join(f"{y}:{v:.2f}" for y, v in yrs.items()))
    text = "\n".join(out)
    print(text)
    path = os.path.join(ROOT, "research", "reports", "2026-10-21_nifty_monthly_premium.txt")
    open(path, "w").write(text + "\n")
    print(f"\nwritten {path}")


if __name__ == "__main__":
    main()
