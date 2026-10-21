"""LEDGER row 68 -- selling Nifty MONTHLY options at the roll, defined risk, managed exits.
Implements research/preregistrations/2026-10-21_nifty_monthly_option_selling.md exactly;
change nothing here without a new row.

    --mechanics          data checks only (legs found / skipped / wings moved); prints no P&L
    --period insample    monthly expiries 2016-02 .. 2024-10
    --period check       monthly expiries 2024-11 .. 2026-09, ONCE: refuses if already burned,
                         and records the burn in research/holdout_burns.json before running

Run: stockey/.xstockey/bin/python research/scripts/2026-10-21_nifty_monthly_option_selling.py --mechanics
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
spec = importlib.util.spec_from_file_location("r64", os.path.join(HERE, "2026-10-20_nifty_option_selling.py"))
r64 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r64)
q = r64.q
BURNS = os.path.join(ROOT, "research", "holdout_burns.json")
BURN_NAME = "nifty-monthly-option-selling-row68-check-2024-11-to-2026-09"

PERIODS = {"insample": ("2016-02-01", "2024-10-31"), "check": ("2024-11-01", "2026-09-30")}
VERSIONS = ["M1 hold to expiry", "M2 exit 10 sessions out", "M3 M2 + take profit 50%", "M4 M2 + stop 1x credit"]
EXIT_BACK = 10
STEP = 100
ELECTION_RESULTS = [pd.Timestamp(d).date() for d in ("2019-05-23", "2024-06-04")]


def sessions_and_closes():
    df = q("SELECT date::date AS d, close FROM nseindia_indices WHERE index_name = 'Nifty 50' "
           "AND date >= '2015-11-01' ORDER BY date")
    df = df[~df["d"].isin(r64.JUNK_INDEX_DATES)].drop_duplicates("d")
    return list(df["d"]), dict(zip(df["d"], df["close"]))


def monthly_expiries():
    e = q("SELECT DISTINCT expiry FROM nseindia_fo_bhavcopy WHERE symbol = 'NIFTY' AND instrument = 'OPTIDX' "
          "AND trade_date >= '2015-12-01' AND expiry BETWEEN '2016-01-01' AND '2026-10-31'")["expiry"]
    e = pd.Series(sorted(e))
    return list(e.groupby(pd.to_datetime(e).dt.to_period("M").values).max())


def lot_size(expiry):
    df = q("SELECT max(lot_size) AS lot FROM nseindia_fo_bhavcopy WHERE symbol = 'NIFTY' AND instrument = 'OPTIDX' "
           "AND expiry = %(x)s AND trade_date >= '2024-07-01'", {"x": expiry})
    if not df.empty and pd.notna(df["lot"].iloc[0]):
        return float(df["lot"].iloc[0])
    return 75.0 if expiry <= pd.Timestamp("2021-06-30").date() else 50.0


def chain(expiry, lo, hi):
    # A holiday-moved monthly is labelled with the scheduled date on some days and the moved one
    # on others (2018-03-29 / 03-28): take, per day, the latest expiry within 4 days of the label.
    df = q("SELECT trade_date, expiry, strike, option_type, close, contracts FROM nseindia_fo_bhavcopy "
           "WHERE symbol = 'NIFTY' AND instrument = 'OPTIDX' AND expiry BETWEEN %(x4)s AND %(x)s "
           "AND trade_date BETWEEN %(lo)s AND %(hi)s AND option_type IN ('CE', 'PE')",
           {"x": expiry, "x4": expiry - pd.Timedelta(days=4), "lo": lo, "hi": hi})
    df = df[df["expiry"] == df.groupby("trade_date")["expiry"].transform("max")]
    return {(r.trade_date, r.option_type, float(r.strike)): (float(r.close), int(r.contracts or 0))
            for r in df.itertuples()}


def stt_sell(day):
    return r64.stt_sell_rate(day)


def leg_cost(p, buy, day, lot):
    c = max(0.25, 0.01 * p) + 20.0 / lot + 0.0006 * p
    return c + (0.00003 * p if buy else stt_sell(day) * p)


def on_or_before(d, sessions):
    """A bhavcopy expiry that fell on a market holiday (2018-03-29, 2023-03-30, 2023-06-29,
    2026-03-31) expired on the session before it."""
    i = pd.Index(sessions).searchsorted(d, side="right") - 1
    return sessions[i] if i >= 0 else None


def setup(x, prev, sessions, closes):
    """The month's legs, or {'skip': reason}. x is the bhavcopy expiry label; xs its session."""
    xs, ps = on_or_before(x, sessions), on_or_before(prev, sessions)
    if xs is None or ps is None or xs not in closes or sessions.index(xs) - sessions.index(ps) < EXIT_BACK + 2:
        return {"skip": "no session"}
    entry = sessions[sessions.index(ps) + 1]
    if any(entry < d <= x for d in ELECTION_RESULTS):
        return {"skip": "election month"}
    ch = chain(x, entry, x)
    atm = round(closes[entry] / STEP) * STEP

    def traded(side, k):
        v = ch.get((entry, side, float(k)))
        return v if v and v[1] > 0 else None

    ce, pe = traded("CE", atm), traded("PE", atm)
    if not ce or not pe:
        return {"skip": "ATM not traded"}
    prem = ce[0] + pe[0]
    w = max(STEP, round(2 * prem / STEP) * STEP)
    wings, moved = {}, 0
    for side, sign in (("CE", +1), ("PE", -1)):
        k = atm + sign * w
        while traded(side, k) is None and abs(k - atm) > STEP:
            k -= sign * STEP
            moved += 1
        if traded(side, k) is None:
            return {"skip": "no traded wing"}
        wings[side] = k
    legs = [("CE", atm, -1), ("PE", atm, -1), ("CE", wings["CE"], +1), ("PE", wings["PE"], +1)]
    return {"entry": entry, "xs": xs, "atm": atm, "prem": prem, "w": w, "legs": legs, "chain": ch, "moved": moved,
            "width": max(wings["CE"] - atm, atm - wings["PE"])}


def run(s, x, sessions, closes, version, lot):
    entry, legs, ch = s["entry"], s["legs"], s["chain"]
    px = [ch[(entry, side, float(k))][0] for side, k, _ in legs]
    credit = sum(-sign * p for (_, _, sign), p in zip(legs, px))
    cost = sum(leg_cost(p, sign > 0, entry, lot) for (_, _, sign), p in zip(legs, px))
    risk = s["width"] - credit
    if risk <= 0:
        return {"skip": "credit above width"}
    xs = s["xs"]
    i_x = sessions.index(xs)
    exit_day = sessions[i_x - EXIT_BACK]

    def value(day):  # cost to close the position at that day's closes (short legs positive)
        v = 0.0
        for side, k, sign in legs:
            r = ch.get((day, side, float(k)))
            if r is None:
                return None
            v += -sign * r[0]
        return v

    def close_out(day):
        prices = [ch[(day, side, float(k))][0] for side, k, _ in legs]
        c = sum(leg_cost(p, sign < 0, day, lot) for (_, _, sign), p in zip(legs, prices))
        return value(day), c

    if version == 0:
        settle = closes[xs]
        pay = 0.0
        for side, k, sign in legs:
            intr = max(settle - k, 0.0) if side == "CE" else max(k - settle, 0.0)
            pay += -sign * intr
            if sign > 0 and intr > 0:
                cost += 0.00125 * intr
        pnl, how = credit - pay - cost, "expiry"
    else:
        days = [d for d in sessions[sessions.index(entry) + 1: sessions.index(exit_day) + 1]]
        hit = None
        for d in days:
            v = value(d)
            if v is None:
                continue
            if (version == 2 and v <= 0.5 * credit) or (version == 3 and v - credit >= credit):
                hit = d
                break
        d = hit or exit_day
        while value(d) is None and d < xs:  # a missing mark on the exit day: next session with all legs
            d = sessions[sessions.index(d) + 1]
        v, c = close_out(d)
        pnl, how = credit - v - cost - c, ("trigger" if hit else "exit day")
    return {"expiry": x, "entry": entry, "credit": credit, "risk": risk, "pnl": pnl, "ret": pnl / risk, "how": how}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mechanics", action="store_true")
    ap.add_argument("--period", choices=list(PERIODS))
    args = ap.parse_args()
    if not args.mechanics and not args.period:
        sys.exit("give --mechanics or --period")
    sessions, closes = sessions_and_closes()
    allm = monthly_expiries()

    def months(lo, hi):
        return [(x, allm[i - 1]) for i, x in enumerate(allm)
                if i > 0 and pd.Timestamp(lo).date() <= x <= pd.Timestamp(hi).date()]

    if args.mechanics:
        for name, (lo, hi) in PERIODS.items():
            ms = months(lo, hi)
            st = [setup(x, p, sessions, closes) for x, p in ms]
            ok = [s for s in st if "skip" not in s]
            skips = pd.Series([s["skip"] for s in st if "skip" in s]).value_counts().to_dict()
            print(f"{name}: {lo}..{hi}, {len(ms)} monthly expiries, {len(ok)} set up, skipped {skips}")
            if ok:
                print(f"   wing distance W: median {np.median([s['w'] for s in ok]):.0f} pts "
                      f"(range {min(s['w'] for s in ok):.0f}-{max(s['w'] for s in ok):.0f}); "
                      f"months with a wing moved inward {sum(s['moved'] > 0 for s in ok)}")
                gaps = 0
                for (x, _), s in zip(ms, st):
                    if "skip" in s:
                        continue
                    span = sessions[sessions.index(s["entry"]) + 1: sessions.index(s["xs"]) - EXIT_BACK + 1]
                    gaps += sum(any((d, side, float(k)) not in s["chain"] for side, k, _ in s["legs"]) for d in span)
                print(f"   session-days with a missing leg mark before the exit day: {gaps}")
        return

    lo, hi = PERIODS[args.period]
    if args.period == "check":
        burns = json.load(open(BURNS))
        if any(b["name"] == BURN_NAME for b in burns):
            sys.exit(f"{BURN_NAME} is already burned: the check runs once")
        burns.append({"name": BURN_NAME, "configHash": "preregistrations/2026-10-21_nifty_monthly_option_selling.md",
                      "burnedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
                      "note": "LEDGER row 68's once-only check, monthly expiries 2024-11..2026-09."})
        json.dump(burns, open(BURNS, "w"), indent=2)
        open(BURNS, "a").write("\n")

    ms = months(lo, hi)
    st = [(x, setup(x, p, sessions, closes)) for x, p in ms]
    rows = {v: [] for v in VERSIONS}
    for x, s in st:
        if "skip" in s:
            continue
        lot = lot_size(x)
        for vi, v in enumerate(VERSIONS):
            r = run(s, x, sessions, closes, vi, lot)
            if "ret" in r:
                rows[v].append(r)
    frames = {v: pd.DataFrame(r) for v, r in rows.items()}
    pvals = [r64.bootstrap_p(f["ret"]) if len(f) else 1.0 for f in frames.values()]
    qs = r64.bh(pvals)
    out = [f"LEDGER 68, period {args.period}: monthly expiries {lo}..{hi}, {len(ms)} months "
           f"({sum('skip' in s for _, s in st)} skipped)"]
    for (v, f), p, qv in zip(frames.items(), pvals, qs):
        r = f["ret"]
        cum = (r * 0.10).cumsum()
        dd = (cum - np.maximum.accumulate(np.r_[0.0, cum.values])[1:]).min()
        yrs = f.groupby(pd.to_datetime(f["expiry"]).dt.year)["ret"].agg(["count", "mean", "sum"])
        out.append(f"\n{v}: {len(f)} months")
        out.append(f"   mean net return on risk {100*r.mean():+.2f}%/month, hit {100*(r>0).mean():.0f}%, "
                   f"worst {100*r.min():+.0f}%, best {100*r.max():+.0f}%; exits by trigger {(f['how']=='trigger').sum()}")
        out.append(f"   account at 10% risk a month: {100*cum.iloc[-1]/len(r)*12:+.1f}%/yr, max drawdown {100*dd:.0f}%; "
                   f"mean credit {f['credit'].mean():.0f} pts, mean risk {f['risk'].mean():.0f} pts")
        out.append(f"   bootstrap p {p:.4f}, BH q {qv:.4f}; years positive {(yrs['mean'] > 0).sum()} of {len(yrs)}")
        out.append("   by year (months / mean %): " + "  ".join(
            f"{y}:{int(t['count'])}/{100*t['mean']:+.1f}" for y, t in yrs.iterrows()))
    m1, m2 = frames[VERSIONS[0]].set_index("expiry")["ret"], frames[VERSIONS[1]].set_index("expiry")["ret"]
    d = (m2 - m1).dropna()
    out.append(f"\nM2 minus M1 (does exiting early help?): mean {100*d.mean():+.2f}%/month over {len(d)} months, "
               f"M2 better in {100*(d>0).mean():.0f}%, bootstrap p {r64.bootstrap_p(d):.4f}")
    text = "\n".join(out)
    print(text)
    path = os.path.join(ROOT, "research", "reports", f"2026-10-21_nifty_monthly_option_selling_{args.period}.txt")
    open(path, "w").write(text + "\n")
    print(f"\nwritten {path}")


if __name__ == "__main__":
    main()
