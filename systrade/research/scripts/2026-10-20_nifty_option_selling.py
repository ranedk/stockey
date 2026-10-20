"""LEDGER row 64 -- selling Nifty weekly options, defined risk. Implements
research/preregistrations/2026-10-20_nifty_option_selling.md exactly; change nothing here
without a new row.

    --mechanics          data checks only (legs found / missing per period); prints no P&L
    --period insample    expiries 2021-10-21 .. 2024-10-31
    --period check       expiries 2024-11-01 .. 2026-10-13, ONCE: refuses if already burned,
                         and records the burn in research/holdout_burns.json before running

Run: stockey/.xstockey/bin/python research/scripts/2026-10-20_nifty_option_selling.py --mechanics
"""

import argparse
import json
import os
import sys
from datetime import datetime, time, timedelta

import numpy as np
import pandas as pd
import psycopg2

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
BURNS = os.path.join(ROOT, "research", "holdout_burns.json")
BURN_NAME = "nifty-option-selling-row64-check-2024-11-to-2026-10"

PERIODS = {
    "insample": ("2021-10-21", "2024-10-31"),
    "check": ("2024-11-01", "2026-10-13"),
}
STEP = 50  # Nifty strike spacing, points
WING = 10  # strikes
STRUCTURES = {
    # name: (entry: sessions before expiry, entry time IST, short offset, wing offset)
    "A iron fly, 2 sessions out": (2, time(15, 15), 0, WING),
    "B iron condor, 2 sessions out": (2, time(15, 15), 4, WING),
    "C iron fly, expiry day 09:30": (0, time(9, 30), 0, WING),
}
LOOKBACK_MIN = 30
JUNK_INDEX_DATES = {pd.Timestamp("2023-06-04").date(), pd.Timestamp("2023-11-04").date()}  # TODO C11
IST = "Asia/Kolkata"


def env():
    out = {}
    with open(os.path.join(ROOT, ".env")) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k] = v.strip().strip('"').strip("'")
    return out


E = env()
CONN = psycopg2.connect(host=E["POSTGRES_HOST"], port=E["POSTGRES_PORT"], dbname=E["POSTGRES_DB"],
                        user=E["POSTGRES_USER"], password=E["POSTGRES_PASSWORD"])


def q(sql, params=None):
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return pd.read_sql(sql, CONN, params=params)


def sessions_and_closes():
    df = q("SELECT date::date AS d, close FROM nseindia_indices WHERE index_name = 'Nifty 50' "
           "AND date >= '2021-06-01' ORDER BY date")
    df = df[~df["d"].isin(JUNK_INDEX_DATES)].drop_duplicates("d")
    return list(df["d"]), dict(zip(df["d"], df["close"]))


def expiries(lo, hi):
    df = q("SELECT DISTINCT expiry FROM nseindia_fo_bhavcopy WHERE symbol = 'NIFTY' AND instrument = 'OPTIDX' "
           "AND expiry BETWEEN %(lo)s AND %(hi)s AND trade_date BETWEEN %(tlo)s AND %(hi)s ORDER BY expiry",
           {"lo": lo, "hi": hi, "tlo": (pd.Timestamp(lo) - timedelta(days=60)).date()})
    return list(df["expiry"])


def lot_sizes():
    df = q("SELECT expiry, max(lot_size) AS lot FROM nseindia_fo_bhavcopy WHERE symbol = 'NIFTY' "
           "AND instrument = 'OPTIDX' AND lot_size IS NOT NULL AND trade_date >= '2024-07-01' GROUP BY expiry")
    return dict(zip(df["expiry"], df["lot"]))


def chain_at(day, at):
    """Last bar at or before `at` (IST) on `day`, within LOOKBACK_MIN, for every WEEK1 offset and side:
    {(side, strike): (close, offset)} and the ATM strike (offset 0's, CE)."""
    end = pd.Timestamp(datetime.combine(day, at)).tz_localize(IST)
    start = end - pd.Timedelta(minutes=LOOKBACK_MIN)
    df = q("SELECT strike_offset, option_type, ts, strike, close FROM dhan_option_rolling "
           "WHERE underlying = 'NIFTY' AND expiry_flag = 'WEEK' AND expiry_code = 1 AND interval_minutes = 1 "
           "AND ts > %(s)s AND ts <= %(e)s",
           {"s": start.tz_convert("UTC").to_pydatetime(), "e": end.tz_convert("UTC").to_pydatetime()})
    if df.empty:
        return {}, None
    # a bar stamped t covers t..t+1min; its close is the price at t+1min, so it must END by `at`
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df[df["ts"] + pd.Timedelta(minutes=1) <= end.tz_convert("UTC")]
    last = df.sort_values("ts").groupby(["strike_offset", "option_type"]).tail(1)
    chain = {(r.option_type, float(r.strike)): (float(r.close), int(r.strike_offset)) for r in last.itertuples()}
    atm_rows = last[(last["strike_offset"] == 0) & (last["option_type"] == "CE")]
    atm = float(atm_rows["strike"].iloc[0]) if not atm_rows.empty else None
    return chain, atm


def stt_sell_rate(day):
    return 0.000625 if day <= pd.Timestamp("2024-09-30").date() else 0.001


def trade(structure, expiry, sessions, closes, lots):
    back, at, short_off, wing_off = STRUCTURES[structure]
    if expiry not in closes:
        return {"skip": "no settlement close"}
    i = sessions.index(expiry)
    if i - back < 0:
        return {"skip": "no entry session"}
    day = sessions[i - back]
    chain, atm = chain_at(day, at)
    if atm is None:
        return {"skip": "no ATM bar"}
    legs = [  # (side, strike, +1 long / -1 short)
        ("CE", atm + short_off * STEP, -1), ("PE", atm - short_off * STEP, -1),
        ("CE", atm + wing_off * STEP, +1), ("PE", atm - wing_off * STEP, +1),
    ]
    prices = []
    for side, k, sign in legs:
        if (side, k) not in chain:
            return {"skip": f"missing {side} {k:.0f}", "day": day}
        prices.append(chain[(side, k)][0])
    lot = lots.get(expiry, 50)
    settle = closes[expiry]
    credit = cost = pnl = 0.0
    for (side, k, sign), p in zip(legs, prices):
        intrinsic = max(settle - k, 0.0) if side == "CE" else max(k - settle, 0.0)
        credit += -sign * p
        pnl += -sign * p + sign * intrinsic
        cost += max(0.25, 0.01 * p) + 20.0 / lot + 0.0006 * p
        cost += 0.00003 * p if sign > 0 else stt_sell_rate(day) * p
        if sign > 0 and intrinsic > 0:
            cost += 0.00125 * intrinsic
    width = (wing_off - short_off) * STEP
    risk = width - credit
    if risk <= 0:
        return {"skip": "credit above width", "day": day}
    net = pnl - cost
    return {"day": day, "expiry": expiry, "atm": atm, "settle": settle, "credit": credit,
            "cost": cost, "pnl": net, "risk": risk, "ret": net / risk}


def bootstrap_p(x, reps=10000, seed=1):
    rng = np.random.default_rng(seed)
    x = np.asarray(x)
    means = rng.choice(x - x.mean(), size=(reps, len(x)), replace=True).mean(axis=1)
    return float((means >= x.mean()).mean())


def bh(pvals):
    p = np.asarray(pvals)
    order = np.argsort(p)
    q = np.empty_like(p)
    prev = 1.0
    for rank, idx in reversed(list(enumerate(order, start=1))):
        prev = min(prev, p[idx] * len(p) / rank)
        q[idx] = prev
    return q


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mechanics", action="store_true")
    ap.add_argument("--period", choices=list(PERIODS))
    args = ap.parse_args()
    if not args.mechanics and not args.period:
        sys.exit("give --mechanics or --period")

    sessions, closes = sessions_and_closes()
    lots = lot_sizes()

    if args.mechanics:
        for name, (lo, hi) in PERIODS.items():
            exps = expiries(lo, hi)
            print(f"{name}: {lo}..{hi}, {len(exps)} expiries in the bhavcopy")
            for s in STRUCTURES:
                res = [trade(s, e, sessions, closes, lots) for e in exps]
                skips = pd.Series([r["skip"].split(" ")[0] + (" " + r["skip"].split(" ")[1] if r["skip"].startswith("missing") else "")
                                   for r in res if "skip" in r]).value_counts().to_dict()
                print(f"   {s:<32} tradable {sum('ret' in r for r in res):3d}, skipped {skips}")
        return

    lo, hi = PERIODS[args.period]
    if args.period == "check":
        burns = json.load(open(BURNS))
        if any(b["name"] == BURN_NAME for b in burns):
            sys.exit(f"{BURN_NAME} is already burned: the check runs once")
        burns.append({"name": BURN_NAME, "configHash": "preregistrations/2026-10-20_nifty_option_selling.md",
                      "burnedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
                      "note": "LEDGER row 64's once-only check, expiries 2024-11-01..2026-10-13."})
        json.dump(burns, open(BURNS, "w"), indent=2)
        open(BURNS, "a").write("\n")

    exps = expiries(lo, hi)
    out, rows = [], {}
    pvals = []
    for s in STRUCTURES:
        res = [trade(s, e, sessions, closes, lots) for e in exps]
        ok = pd.DataFrame([r for r in res if "ret" in r])
        rows[s] = ok
        pvals.append(bootstrap_p(ok["ret"]) if len(ok) else 1.0)
    qs = bh(pvals)
    out.append(f"LEDGER 64, period {args.period}: expiries {lo}..{hi}, {len(exps)} weeks")
    for (s, ok), p, qv in zip(rows.items(), pvals, qs):
        r = ok["ret"]
        cum = r.cumsum()
        dd = (cum - cum.cummax()).min()
        yrs = r.groupby(pd.to_datetime(ok["expiry"]).dt.year).agg(["count", "mean", "sum"])
        out.append(f"\n{s}: {len(ok)} trades of {len(exps)}")
        out.append(f"   mean net return on risk {r.mean()*100:+.2f}%/week (x52 = {r.mean()*52*100:+.0f}%/yr), "
                   f"hit {100*(r>0).mean():.0f}%, worst {r.min()*100:+.0f}%, best {r.max()*100:+.0f}%")
        out.append(f"   account max drawdown {dd*10:.0f}% at 10% of the account at risk a week "
                   f"({dd*100:.0f}% of one week's risk); mean credit {ok['credit'].mean():.1f} pts, "
                   f"mean cost {ok['cost'].mean():.1f} pts, mean risk {ok['risk'].mean():.0f} pts")
        out.append(f"   bootstrap p {p:.4f}, BH q {qv:.4f}")
        out.append("   by year (trades, mean %/week, sum %):  " + "  ".join(
            f"{y}:{int(v['count'])}/{v['mean']*100:+.2f}/{v['sum']*100:+.0f}" for y, v in yrs.iterrows()))
    text = "\n".join(out)
    print(text)
    path = os.path.join(ROOT, "research", "reports", f"2026-10-20_nifty_option_selling_{args.period}.txt")
    open(path, "w").write(text + "\n")
    print(f"\nwritten {path}")


if __name__ == "__main__":
    main()
