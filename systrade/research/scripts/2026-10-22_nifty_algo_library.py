"""LEDGER row 76, step 1 -- build the 36-member Nifty algo library
(research/preregistrations/2026-10-22_nifty_algo_rotation.md, section 1). No member is judged here:
this writes each member's daily NET return (points / Nifty at entry, i.e. per unit of notional) and
the rotation script (2026-10-22_nifty_algo_rotation.py) scales and rotates them.

  S  short ATM straddle, intraday, out 15:15: entry 09:20/10:00/11:00 x stop per leg +25%/+50%/none (9)
  F  iron fly, intraday, out 15:15: entry 09:20/10:00/11:00 x wings 200/400 x managed/unmanaged (12)
  O  overnight: fly 400 sold 10:00 -> next bhavcopy close; same sold 15:15; row 74 D3 (3)
  L  long ATM straddle, intraday, out 15:15: entry 09:20/10:00 x none / TP +50% SL -30% (4)
  T  Nifty futures EWMAC 8/32, 16/64, 32/128, 64/256, long and short, carry as row 62 (4)
  I  index intraday on Dhan's minute spot, traded as futures: ORB 15/30, first-half-hour momentum,
     gap fade (4)

Costs: options as row 73 (fees() + 0.5 pt slippage per leg per transaction); index trades 3 bps of
notional per round trip; T 3 bps per unit traded plus a monthly roll (row 62).
Return dating: a trade's P&L sits on its ENTRY session; O members' results are known one session
later, which the rotation script handles with a one-day availability lag.
Missing-leg rules as rows 73-74: a short leg that leaves Dhan's +-10 window keeps its last close, a
long leg missing at exit is worth 0 (counted, reported).

Run: stockey/.xstockey/bin/python research/scripts/2026-10-22_nifty_algo_library.py --period insample
     (resumable: per-session results are checkpointed in research/reports/.cache_algo_library_<period>.pkl)
"""

import argparse
import importlib.util
import os
import pickle
import sys
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
q = r64.q
PERIODS = {"insample": ("2021-10-21", "2024-10-31")}  # the check is built only by the rotation script's burn
SLIP, IDX_COST = 0.5, 0.0003
OUT, STALE, LB = time(15, 15), pd.Timedelta(minutes=5), pd.Timedelta(minutes=r64.LOOKBACK_MIN)
ENTRIES = {"0920": time(9, 20), "1000": time(10, 0), "1100": time(11, 0)}
CARVER = {(8, 32): 5.3, (16, 64): 3.75, (32, 128): 2.65, (64, 256): 1.87}


def members():
    m = [f"S-{e}-{s}" for e in ENTRIES for s in ("25", "50", "none")]
    m += [f"F-{e}-{w}-{g}" for e in ENTRIES for w in (200, 400) for g in ("managed", "unmanaged")]
    m += ["O-fly-1000-bhav", "O-fly-1515-bhav", "O-D3-managed"]
    m += [f"L-{e}-{x}" for e in ("0920", "1000") for x in ("none", "tp50sl30")]
    m += [f"T-ewmac-{f}-{s}" for f, s in CARVER]
    m += ["I-orb15", "I-orb30", "I-mom-first-half-hour", "I-gap-fade"]
    return m


def day_bars(day):
    """r73.day_bars plus Dhan's spot column (the I members' minute index)."""
    df = q("SELECT strike_offset, option_type, ts, strike, spot, open, high, close FROM dhan_option_rolling "
           "WHERE underlying = 'NIFTY' AND expiry_flag = 'WEEK' AND expiry_code = 1 AND interval_minutes = 1 "
           "AND ts >= %(s)s AND ts < %(e)s",
           {"s": r73.ts(day, time(9, 0)).to_pydatetime(), "e": r73.ts(day, time(15, 31)).to_pydatetime()})
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df["end"] = df["ts"] + pd.Timedelta(minutes=1)
    return df


def leg_series(bars, side, k):
    return bars[(bars.option_type == side) & (bars.strike == k)].drop_duplicates("end").set_index("end").sort_index()


def entry_price(s, t0):
    pre = s[(s.index <= t0) & (s.index > t0 - LB)]
    return None if pre.empty else float(pre["close"].iloc[-1])


def atm_at(bars, t0):
    w = bars[(bars["end"] <= t0) & (bars["end"] > t0 - LB) & (bars.strike_offset == 0) & (bars.option_type == "CE")]
    if w.empty:
        return None, None
    w = w.sort_values("end")
    return float(w["strike"].iloc[-1]), float(w["spot"].iloc[-1])


def short_straddle(day, bars, at, stop, lot):
    """Row 73 A with the entry time and stop as parameters (stop on the bar high, fill max(stop, open))."""
    t0, t1 = r73.ts(day, at), r73.ts(day, OUT)
    k, spot = atm_at(bars, t0)
    if k is None:
        return None
    net, missing = 0.0, 0
    for side in ("CE", "PE"):
        s = leg_series(bars, side, k)
        p0 = entry_price(s, t0)
        if p0 is None:
            return None
        life = s[(s.index > t0) & (s.index <= t1)]
        hit = life[life["high"] >= stop * p0] if stop else life.iloc[0:0]
        if not hit.empty:
            x = max(stop * p0, float(hit["open"].iloc[0]))
        elif not life.empty:
            x = float(life["close"].iloc[-1])
            missing += life.index[-1] < t1 - STALE
        else:
            x, missing = p0, missing + 1
        net += p0 - x - r73.fees(p0, False, day, lot) - r73.fees(x, True, day, lot) - 2 * SLIP
    return {"r": net / spot, "missing": missing}


def fly_path(day, bars, nxt, at, wing, managed, overnight, label, lot):
    """Row 74's run() with entry time and wing as parameters: each side (call spread, put spread) alone,
    TP at value <= 0.5 credit / stop at >= 1.5 credit on minute closes, else out at 15:15 (next session's
    for overnight; bhavcopy close there if a leg is stale)."""
    t0 = r73.ts(day, at)
    k, spot = atm_at(bars, t0)
    if k is None:
        return None
    t_end = r73.ts(nxt if overnight else day, OUT)
    legs = {"Cs": ("CE", k), "Cl": ("CE", k + wing), "Ps": ("PE", k), "Pl": ("PE", k - wing)}
    ser, p0 = {}, {}
    for n, (side, kk) in legs.items():
        s = leg_series(bars, side, kk)["close"]
        p = entry_price(s.to_frame(), t0)
        if p is None:
            return None
        p0[n], ser[n] = p, s[(s.index > t0) & (s.index <= t_end)]
    grid = sorted(set().union(*[set(s.index) for s in ser.values()]))
    if not grid:
        return None
    path = pd.DataFrame({n: s.reindex(grid) for n, s in ser.items()})
    seen = pd.DataFrame({n: pd.Series(np.where(path[n].notna(), grid, pd.NaT), index=grid) for n in path}).ffill()
    path = path.ffill().fillna(pd.Series(p0))
    net, missing = 0.0, 0
    for sp in ("C", "P"):
        s_, l_ = sp + "s", sp + "l"
        c = p0[s_] - p0[l_]
        v = path[s_] - path[l_]
        when, timed = grid[-1], True
        if managed:
            hit = v[(v <= 0.5 * c) | (v >= 1.5 * c)]
            if not hit.empty:
                when, timed = hit.index[0], False
        ps, pl = float(path.at[when, s_]), float(path.at[when, l_])
        st_s = pd.isna(seen.at[when, s_]) or when - seen.at[when, s_] > STALE
        st_l = pd.isna(seen.at[when, l_]) or when - seen.at[when, l_] > STALE
        if overnight and timed and (st_s or st_l):
            b = r73.bhav(nxt, label, {legs[s_][1], legs[l_][1]})
            if legs[s_] in b and legs[l_] in b:
                ps, pl, st_s, st_l = b[legs[s_]], b[legs[l_]], False, False
        if st_l:
            pl, missing = 0.0, missing + 1
        missing += st_s
        xd = when.tz_convert(r64.IST).date()
        net += (p0[s_] - ps) - (p0[l_] - pl) - 4 * SLIP - (
            r73.fees(p0[s_], False, day, lot) + r73.fees(p0[l_], True, day, lot)
            + r73.fees(ps, True, xd, lot) + r73.fees(pl, False, xd, lot))
    return {"r": net / spot, "missing": missing}


def fly_to_bhav(day, nxt, xs, label, closes, lot, at, wing=400):
    """Row 73 B with the entry time as a parameter (no same-day-part diagnostic)."""
    chain, atm = r64.chain_at(day, at)
    if atm is None:
        return None
    legs = [("CE", atm, -1), ("PE", atm, -1), ("CE", atm + wing, +1), ("PE", atm - wing, +1)]
    if any((s, k) not in chain for s, k, _ in legs):
        return None
    p0 = [chain[(s, k)][0] for s, k, _ in legs]
    settle = nxt == xs
    if settle:
        if nxt not in closes:
            return None
        S = closes[nxt]
        p1 = [max(S - k, 0) if s == "CE" else max(k - S, 0) for s, k, _ in legs]
    else:
        b = r73.bhav(nxt, label, {k for _, k, _ in legs})
        if any((s, k) not in b for s, k, _ in legs):
            return None
        p1 = [b[(s, k)] for s, k, _ in legs]
    gross = sum(-sg * (a - c) for (_, _, sg), a, c in zip(legs, p0, p1))
    cost = 0.0
    for (_, _, sg), a, c in zip(legs, p0, p1):
        cost += r73.fees(a, sg > 0, day, lot) + SLIP
        cost += (0.00125 * c if sg > 0 and c > 0 else 0.0) if settle else r73.fees(c, sg < 0, nxt, lot) + SLIP
    return {"r": (gross - cost) / atm, "missing": 0}


def long_straddle(day, bars, at, managed, lot):
    t0, t1 = r73.ts(day, at), r73.ts(day, OUT)
    k, spot = atm_at(bars, t0)
    if k is None:
        return None
    ser, p0 = {}, {}
    for side in ("CE", "PE"):
        s = leg_series(bars, side, k)["close"]
        p = entry_price(s.to_frame(), t0)
        if p is None:
            return None
        p0[side], ser[side] = p, s[(s.index > t0) & (s.index <= t1)]
    grid = sorted(set(ser["CE"].index) | set(ser["PE"].index))
    if not grid:
        return None
    path = pd.DataFrame({n: s.reindex(grid) for n, s in ser.items()})
    seen = pd.DataFrame({n: pd.Series(np.where(path[n].notna(), grid, pd.NaT), index=grid) for n in path}).ffill()
    path = path.ffill().fillna(pd.Series(p0))
    prem, v = p0["CE"] + p0["PE"], path.sum(axis=1)
    when = grid[-1]
    if managed:
        hit = v[(v >= 1.5 * prem) | (v <= 0.7 * prem)]
        if not hit.empty:
            when = hit.index[0]
    net, missing = 0.0, 0
    for side in ("CE", "PE"):
        x = float(path.at[when, side])
        if pd.isna(seen.at[when, side]) or when - seen.at[when, side] > STALE:
            x, missing = 0.0, missing + 1
        net += x - p0[side] - r73.fees(p0[side], True, day, lot) - r73.fees(x, False, day, lot) - 2 * SLIP
    return {"r": net / spot, "missing": missing}


def spot_series(bars):
    s = bars[(bars.strike_offset == 0) & (bars.option_type == "CE")].drop_duplicates("end").set_index("end")["spot"]
    return s.sort_index().dropna()


def index_trades(day, bars, prev_close):
    """The four I members on Dhan's minute spot (value at each bar's end)."""
    s = spot_series(bars)
    out = {}
    if s.empty:
        return out
    t1 = r73.ts(day, OUT)
    open_ = float(s.iloc[0])
    for n, mins in (("I-orb15", 15), ("I-orb30", 30)):
        w_end = r73.ts(day, time(9, 15)) + pd.Timedelta(minutes=mins)
        rng = s[s.index <= w_end]
        after = s[(s.index > w_end) & (s.index <= t1)]
        if len(rng) < mins // 2 or after.empty:
            continue
        hi, lo = rng.max(), rng.min()
        brk = after[(after > hi) | (after < lo)]
        if brk.empty:
            out[n] = 0.0
            continue
        t_in, p_in = brk.index[0], float(brk.iloc[0])
        d = 1 if p_in > hi else -1
        rest = after[after.index > t_in]
        stop = rest[(rest < lo) if d > 0 else (rest > hi)]
        p_out = float(stop.iloc[0]) if not stop.empty else (float(rest.iloc[-1]) if not rest.empty else p_in)
        out[n] = d * (p_out / p_in - 1) - IDX_COST

    def at(hm):
        w = s[s.index <= r73.ts(day, hm)]
        return float(w.iloc[-1]) if not w.empty and w.index[-1] > r73.ts(day, hm) - STALE else None
    p945, p1500, p1530 = at(time(9, 45)), at(time(15, 0)), at(time(15, 30))
    if p945 and p1500 and p1530:
        out["I-mom-first-half-hour"] = np.sign(p945 - open_) * (p1530 / p1500 - 1) - IDX_COST * (p945 != open_)
    p920, p1515 = at(time(9, 20)), at(OUT)
    if prev_close and p920 and p1515:
        gap = open_ / prev_close - 1
        out["I-gap-fade"] = (-np.sign(gap) * (p1515 / p920 - 1) - IDX_COST) if abs(gap) > 0.003 else 0.0
    return out


def trend_members(lo, hi):
    """T: one EWMAC speed each, long and short, position = forecast/10 at 10% vol, row 62's carry and costs."""
    ix = q("SELECT date::date AS date, close, div_yield FROM nseindia_indices WHERE index_name = 'Nifty 50' "
           "AND close > 0 AND date >= '2019-01-01' AND date <= %(hi)s ORDER BY date", {"hi": hi})
    ix = ix[~ix["date"].isin(r64.JUNK_INDEX_DATES)].drop_duplicates("date").set_index("date")
    rr = q("SELECT date::date AS date, repo_rate FROM rbi_bank_rates WHERE repo_rate IS NOT NULL ORDER BY date")
    ix = ix.join(rr.drop_duplicates("date").set_index("date")["repo_rate"].rename("repo"), how="left")
    ix["repo"] = ix["repo"].ffill().bfill()
    ix["div_yield"] = ix["div_yield"].ffill().fillna(1.2)
    px, ret = ix["close"], ix["close"].pct_change()
    fut = ret - (ix["repo"] - ix["div_yield"]) / 100 / 252
    vol_d = ret.ewm(span=36, adjust=False).std()
    out, held = {}, {}
    for (f, s_), scalar in CARVER.items():
        raw = px.ewm(span=f, adjust=False).mean() - px.ewm(span=s_, adjust=False).mean()
        fc = (raw / (vol_d * px) * scalar).clip(-20, 20)
        pos = (fc / 10 * 0.10 / (vol_d * np.sqrt(252))).shift(1)
        rolls = pd.Series(0.0, index=pos.index)
        rolls.iloc[::21] = 2 * pos.abs().iloc[::21]
        r = pos * fut - (pos.diff().abs() + rolls) * 0.0003
        keep = (r.index >= pd.Timestamp(lo).date()) & (r.index <= pd.Timestamp(hi).date())
        out[f"T-ewmac-{f}-{s_}"], held[f"T-ewmac-{f}-{s_}"] = r[keep], pos[keep]
    return pd.DataFrame(out), pd.DataFrame(held)  # daily returns; position held that day (x notional)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", choices=list(PERIODS), required=True)
    ap.add_argument("--limit", type=int, default=0, help="first N sessions only (smoke test, no output file)")
    args = ap.parse_args()
    lo, hi = (pd.Timestamp(v).date() for v in PERIODS[args.period])
    sessions, closes = r64.sessions_and_closes()
    cmap = r73.contract_map(sessions, str(lo), str(hi))
    lotmap = r73.lots()
    days = [d for d in sessions if lo <= d <= hi]
    if args.limit:
        days = days[:args.limit]
    ckpt = os.path.join(ROOT, "research", "reports", f".cache_algo_library_{args.period}.pkl")
    done = {} if args.limit or not os.path.exists(ckpt) else pickle.load(open(ckpt, "rb"))
    cache = {}

    def bars(d):
        if d not in cache:
            if len(cache) > 2:
                cache.clear()
            cache[d] = day_bars(d)
        return cache[d]
    for n, d in enumerate(days):
        if d in done:
            continue
        xs, label = r73.week1(d, cmap)
        i = sessions.index(d)
        row = {}
        if xs is not None and i + 1 < len(sessions):
            nxt, lot, b = sessions[i + 1], lotmap.get(label, 50.0), bars(d)
            prev = closes.get(sessions[i - 1]) if i > 0 else None
            if not b.empty:
                for e, at in ENTRIES.items():
                    for st, f in (("25", 1.25), ("50", 1.5), ("none", None)):
                        row[f"S-{e}-{st}"] = short_straddle(d, b, at, f, lot)
                    for w in (200, 400):
                        for g in ("managed", "unmanaged"):
                            row[f"F-{e}-{w}-{g}"] = fly_path(d, b, nxt, at, w, g == "managed", False, label, lot)
                for e in ("0920", "1000"):
                    for x in ("none", "tp50sl30"):
                        row[f"L-{e}-{x}"] = long_straddle(d, b, ENTRIES[e], x != "none", lot)
                for k, v in index_trades(d, b, prev).items():
                    row[k] = {"r": v, "missing": 0}
                if xs != d:
                    row["O-fly-1000-bhav"] = fly_to_bhav(d, nxt, xs, label, closes, lot, time(10, 0))
                    row["O-fly-1515-bhav"] = fly_to_bhav(d, nxt, xs, label, closes, lot, OUT)
                    row["O-D3-managed"] = fly_path(d, pd.concat([b, bars(nxt)]), nxt, time(10, 0), 400, True, True, label, lot)
        done[d] = row
        if not args.limit and n % 25 == 0:
            pickle.dump(done, open(ckpt, "wb"))
            print(f"{d}  {n + 1}/{len(days)}", flush=True)
    if not args.limit:
        pickle.dump(done, open(ckpt, "wb"))
    names = [m for m in members() if not m.startswith("T-")]
    lib = pd.DataFrame({m: {d: (r[m]["r"] if r.get(m) else np.nan) for d, r in done.items()} for m in names})
    miss = pd.Series({m: sum((r.get(m) or {}).get("missing", 0) for r in done.values()) for m in names})
    traded = lib.notna().sum()
    lib = lib.sort_index().reindex(days).fillna(0.0)  # a day a member does not trade is 0
    T, _ = trend_members(lo, hi)
    lib = lib.join(T.reindex(days).fillna(0.0))[members()]
    out = [f"LEDGER 76 library, {args.period} {days[0]}..{days[-1]}, {len(days)} sessions; daily net return per unit of "
           "notional (pre-scaling). Inputs, not trials: printed so the members are on record, not judged.",
           f"{'member':26s} {'days':>5s} {'missing legs':>12s} {'mean bps/day':>12s} {'ann. vol':>8s} {'Sharpe':>7s}"]
    for m in members():
        x = lib[m]
        sh = x.mean() / x.std() * np.sqrt(252) if x.std() > 0 else np.nan
        out.append(f"{m:26s} {int(traded.get(m, len(days))):5d} {int(miss.get(m, 0)):12d} {1e4 * x.mean():+12.2f} "
                   f"{100 * x.std() * np.sqrt(252):7.1f}% {sh:+7.2f}")
    print("\n".join(out))
    if args.limit:
        return
    lib.index.name = "day"
    lib.to_csv(os.path.join(ROOT, "research", "reports", f"2026-10-22_nifty_algo_library_{args.period}.csv"))
    open(os.path.join(ROOT, "research", "reports", f"2026-10-22_nifty_algo_library_{args.period}.txt"), "w").write("\n".join(out) + "\n")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.exit("give --period insample")
    main()
