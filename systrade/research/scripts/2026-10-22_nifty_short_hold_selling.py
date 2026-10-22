"""LEDGER row 73 -- short-hold Nifty option selling, pre-registered
(research/preregistrations/2026-10-22_nifty_short_hold_selling.md, trials=2):

  A  intraday ATM WEEK1 straddle sold 09:20, 25% stop per leg on the bar high, rest bought 15:15
  B  one-day iron fly sold 10:00 (ATM, wings +-400), closed at the next session's bhavcopy close
     (settled at intrinsic when that session is the expiry)

Run: stockey/.xstockey/bin/python research/scripts/2026-10-22_nifty_short_hold_selling.py --period insample
     (--mechanics first: counts and skips only, no P&L)
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
spec = importlib.util.spec_from_file_location("r64", os.path.join(HERE, "2026-10-20_nifty_option_selling.py"))
r64 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r64)
q = r64.q
BURNS = os.path.join(ROOT, "research", "holdout_burns.json")
BURN_NAME = "nifty-short-hold-selling-row73-check-2024-11-to-2026-09"
PERIODS = {"insample": ("2021-10-21", "2024-10-31"), "check": ("2024-11-01", "2026-09-30")}
STEP, WING = 50, 400
A_AT, A_STOP, A_OUT = time(9, 20), 1.25, time(15, 15)
B_AT = time(10, 0)
SLIP = 0.5  # points per leg per transaction (section 3); sensitivity levels below
SLIP_ALT = {"0.25 pt": lambda p: 0.25, "1 pt": lambda p: 1.0, "old rule max(0.25, 1%)": lambda p: max(0.25, 0.01 * p)}


def ts(day, at):
    return pd.Timestamp(datetime.combine(day, at)).tz_localize(r64.IST).tz_convert("UTC")


def fees(p, buy, day, lot):
    """Everything but slippage, points, one leg one transaction (section 3)."""
    exch = (0.0005 if day <= pd.Timestamp("2024-09-30").date() else 0.0003503) * p
    sebi = 0.000001 * p
    brk = 20.0 / lot
    return brk + exch + sebi + 0.18 * (brk + exch + sebi) + (0.00003 * p if buy else r64.stt_sell_rate(day) * p)


def lots():
    df = q("SELECT expiry, max(lot_size) AS lot FROM nseindia_fo_bhavcopy WHERE symbol = 'NIFTY' AND instrument = 'OPTIDX' "
           "AND trade_date >= '2024-07-01' AND expiry >= '2024-07-01' GROUP BY expiry")
    return {r.expiry: float(r.lot) for r in df.itertuples() if pd.notna(r.lot)}


def contract_map(sessions, lo, hi):
    """Label -> the session it actually expired (holiday labels moved to the session before)."""
    out = {}
    for x in r64.expiries(lo, (pd.Timestamp(hi) + pd.Timedelta(days=14)).strftime("%Y-%m-%d")):
        i = pd.Index(sessions).searchsorted(x, side="right") - 1
        out[x] = sessions[i]
    return out


def week1(day, cmap):
    """WEEK1 on `day`: the first contract whose expiry session is on or after it."""
    c = [(xs, x) for x, xs in cmap.items() if xs >= day]
    return min(c) if c else (None, None)


def day_bars(day):
    df = q("SELECT strike_offset, option_type, ts, strike, open, high, close FROM dhan_option_rolling "
           "WHERE underlying = 'NIFTY' AND expiry_flag = 'WEEK' AND expiry_code = 1 AND interval_minutes = 1 "
           "AND ts >= %(s)s AND ts < %(e)s",
           {"s": ts(day, time(9, 0)).to_pydatetime(), "e": ts(day, time(15, 31)).to_pydatetime()})
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df["end"] = df["ts"] + pd.Timedelta(minutes=1)
    return df


def trade_a(day, lot, slip):
    bars = day_bars(day)
    if bars.empty:
        return {"skip": "no bars"}
    t0 = ts(day, A_AT)
    first = bars[(bars["end"] <= t0) & (bars["end"] > t0 - pd.Timedelta(minutes=r64.LOOKBACK_MIN))]
    atm = first[(first.strike_offset == 0) & (first.option_type == "CE")].sort_values("ts")
    if atm.empty:
        return {"skip": "no ATM bar"}
    k = float(atm["strike"].iloc[-1])
    net = gross = prem = 0.0
    stopped, vanished = 0, 0
    for side in ("CE", "PE"):
        b = bars[(bars.option_type == side) & (bars.strike == k)].sort_values("ts").drop_duplicates("ts")
        pre = b[(b["end"] <= t0) & (b["end"] > t0 - pd.Timedelta(minutes=r64.LOOKBACK_MIN))]
        if pre.empty:
            return {"skip": f"no {side} entry"}
        p0 = float(pre["close"].iloc[-1])
        prem += p0
        stop = A_STOP * p0
        life = b[(b["ts"] >= t0) & (b["end"] <= ts(day, A_OUT))]
        hit = life[life["high"] >= stop]
        if not hit.empty:
            exit_ = max(stop, float(hit["open"].iloc[0]))
            stopped += 1
        elif not life.empty:
            exit_ = float(life["close"].iloc[-1])
            if life["end"].iloc[-1] < ts(day, A_OUT) - pd.Timedelta(minutes=5):
                vanished += 1
        else:
            exit_ = p0
            vanished += 1
        gross += p0 - exit_
        net += p0 - exit_ - fees(p0, False, day, lot) - fees(exit_, True, day, lot) - slip(p0) - slip(exit_)
    return {"day": day, "prem": prem, "gross": gross, "pnl": net, "ret": net / prem, "risk": 2.5 * prem,
            "stopped": stopped, "vanished": vanished}


def bhav(day, label, strikes):
    df = q("SELECT expiry, strike, option_type, close FROM nseindia_fo_bhavcopy WHERE trade_date = %(d)s "
           "AND symbol = 'NIFTY' AND instrument = 'OPTIDX' AND expiry BETWEEN %(x4)s AND %(x4p)s",
           {"d": day, "x4": label - pd.Timedelta(days=4), "x4p": label + pd.Timedelta(days=4)})
    if df.empty:
        return {}
    df = df[df["expiry"] == df["expiry"].max()]
    return {(r.option_type, float(r.strike)): float(r.close) for r in df.itertuples() if float(r.strike) in strikes}


def trade_b(day, nxt, xs, label, closes, lot, slip):
    if xs == day:
        return {"skip": "expiry day"}
    chain, atm = r64.chain_at(day, B_AT)
    if atm is None:
        return {"skip": "no ATM bar"}
    legs = [("CE", atm, -1), ("PE", atm, -1), ("CE", atm + WING, +1), ("PE", atm - WING, +1)]
    if any((s, k) not in chain for s, k, _ in legs):
        return {"skip": "missing entry leg"}
    p0 = [chain[(s, k)][0] for s, k, _ in legs]
    settle = nxt == xs
    if settle:
        if nxt not in closes:
            return {"skip": "no settlement close"}
        S = closes[nxt]
        p1 = [max(S - k, 0) if s == "CE" else max(k - S, 0) for s, k, _ in legs]
    else:
        b = bhav(nxt, label, {k for _, k, _ in legs})
        if any((s, k) not in b for s, k, _ in legs):
            return {"skip": "missing exit leg"}
        p1 = [b[(s, k)] for s, k, _ in legs]
    credit = sum(-sg * a for (_, _, sg), a in zip(legs, p0))
    gross = sum(-sg * (a - c) for (_, _, sg), a, c in zip(legs, p0, p1))
    cost = 0.0
    for (_, _, sg), a, c in zip(legs, p0, p1):
        cost += fees(a, sg > 0, day, lot) + slip(a)
        if settle:
            cost += 0.00125 * c if sg > 0 and c > 0 else 0.0
        else:
            cost += fees(c, sg < 0, nxt, lot) + slip(c)
    risk = WING - credit
    if risk <= 0:
        return {"skip": "credit above width"}
    # same-day part (10:00 -> 15:15 from the 1-minute data; survivorship-prone, reported only)
    ch2, _ = r64.chain_at(day, A_OUT)
    day_part = (sum(-sg * (a - ch2[(s, k)][0]) for (s, k, sg), a in zip(legs, p0))
                if all((s, k) in ch2 for s, k, _ in legs) else np.nan)
    return {"day": day, "credit": credit, "gross": gross, "pnl": gross - cost, "risk": risk,
            "ret": (gross - cost) / risk, "day_part": day_part, "settled": settle}


def summarise(name, df, out):
    x = df["ret"].values
    acct = np.cumsum(0.02 * df["pnl"].values / df["risk"].values)
    dd = float((acct - np.maximum.accumulate(np.r_[0, acct])[1:]).min())
    yrs = df.groupby(pd.to_datetime(df["day"]).dt.year)["ret"].mean()
    p = r64.bootstrap_p(x)
    out.append(f"{name}: n={len(df)}  mean net {100 * x.mean():+.2f}% per trade  (gross {df['gross'].mean():+.2f} pts, "
               f"net {df['pnl'].mean():+.2f} pts)  hit {100 * (df['pnl'] > 0).mean():.0f}%  worst {100 * x.min():+.0f}%  p {p:.4f}")
    out.append(f"   account (2% at risk per trade, not compounded): total {100 * acct[-1]:+.1f}%, maxDD {100 * dd:.1f}%")
    out.append("   by year " + "  ".join(f"{y}: {100 * v:+.2f}%" for y, v in yrs.items()))
    full = [y for y in yrs.index if y in (2022, 2023, 2024)]
    return p, int(sum(yrs[y] > 0 for y in full)), dd, x.mean()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mechanics", action="store_true")
    ap.add_argument("--period", choices=list(PERIODS))
    args = ap.parse_args()
    if not args.mechanics and not args.period:
        sys.exit("give --mechanics or --period")
    period = args.period or "insample"
    lo, hi = (pd.Timestamp(v).date() for v in PERIODS[period])
    if period == "check" and not args.mechanics:
        burns = json.load(open(BURNS))
        if any(b["name"] == BURN_NAME for b in burns):
            sys.exit(f"{BURN_NAME} is already burned: the check runs once")
        burns.append({"name": BURN_NAME, "configHash": "preregistrations/2026-10-22_nifty_short_hold_selling.md",
                      "burnedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
                      "note": "LEDGER row 73's once-only check, entries 2024-11-01..2026-09-30."})
        json.dump(burns, open(BURNS, "w"), indent=2)
    sessions, closes = r64.sessions_and_closes()
    cmap = contract_map(sessions, str(lo), str(hi))
    lotmap = lots()
    days = [d for d in sessions if lo <= d <= hi]
    ra, rb = [], []
    for i, d in enumerate(days):
        xs, label = week1(d, cmap)
        if xs is None:
            continue
        lot = lotmap.get(label, 50.0)
        nxt = sessions[sessions.index(d) + 1] if sessions.index(d) + 1 < len(sessions) else None
        ra.append(trade_a(d, lot, lambda p: SLIP))
        if nxt is not None:
            rb.append(trade_b(d, nxt, xs, label, closes, lot, lambda p: SLIP))
    out = [f"LEDGER 73 -- short-hold Nifty option selling, {period} entries {lo}..{hi}"]
    for name, res in (("A intraday stopped straddle", ra), ("B one-day iron fly", rb)):
        skips = pd.Series([r["skip"] for r in res if "skip" in r]).value_counts().to_dict()
        out.append(f"{name}: {sum('ret' in r for r in res)} trades, skipped {skips}")
    if args.mechanics:
        print("\n".join(out))
        return
    A = pd.DataFrame([r for r in ra if "ret" in r])
    B = pd.DataFrame([r for r in rb if "ret" in r])
    out.append("")
    pa = summarise("A intraday stopped straddle", A, out)
    out.append(f"   legs stopped: {A['stopped'].sum()} of {2 * len(A)} ({100 * A['stopped'].sum() / (2 * len(A)):.0f}%), "
               f"days with both stopped {(A['stopped'] == 2).sum()}; legs that left Dhan's window {A['vanished'].sum()}")
    pb = summarise("B one-day iron fly", B, out)
    dp = B["day_part"].dropna()
    out.append(f"   same-day part 10:00->15:15 (n={len(dp)}, survivorship-prone): {dp.mean():+.2f} pts gross of "
               f"{B['gross'].mean():+.2f}; settled at expiry {int(B['settled'].sum())} trades")
    q_ = r64.bh([pa[0], pb[0]])
    out.append("\nVERDICT (pass = mean > 0 with BH q < 0.10, >= 2 of 3 full years positive, maxDD within 35%):")
    for (name, (p, ny, dd, m)), qq in zip((("A", pa), ("B", pb)), q_):
        ok = m > 0 and qq < 0.10 and ny >= 2 and dd >= -0.35
        out.append(f"  {name}: mean {100 * m:+.2f}%  q {qq:.4f}  years positive {ny}/3  maxDD {100 * dd:.1f}%  -> {'PASS' if ok else 'FAIL'}")
    out.append("\nslippage sensitivity (mean net per trade; A % of premium, B % of risk), not gating:")
    for lab, f in SLIP_ALT.items():
        a2 = [trade_a(r["day"], lotmap.get(week1(r["day"], cmap)[1], 50.0), f) for r in ra if "ret" in r]
        out.append(f"  {lab:24s} A {100 * np.mean([x['ret'] for x in a2 if 'ret' in x]):+.2f}%")
    for lab, f in SLIP_ALT.items():
        rb2 = []
        for r in rb:
            if "ret" not in r:
                continue
            d = r["day"]
            xs, label = week1(d, cmap)
            rb2.append(trade_b(d, sessions[sessions.index(d) + 1], xs, label, closes, lotmap.get(label, 50.0), f))
        out.append(f"  {lab:24s} B {100 * np.mean([x['ret'] for x in rb2 if 'ret' in x]):+.2f}%")
    for name, df in (("A", A), ("B", B)):
        df = df.copy()
        df["wd"] = pd.to_datetime(df["day"]).dt.day_name().str[:3]
        out.append(f"  {name} by weekday: " + "  ".join(f"{k} {100 * v:+.2f}%" for k, v in df.groupby("wd")["ret"].mean().items()))
    text = "\n".join(out)
    print(text)
    path = os.path.join(ROOT, "research", "reports", f"2026-10-22_nifty_short_hold_selling_{period}.txt")
    open(path, "w").write(text + "\n")
    print(f"\nwritten {path}")


if __name__ == "__main__":
    main()
