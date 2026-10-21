"""Row 70 follow-up: why did a fresh ATM straddle sold daily earn ~4% of premium a day, while
the straddle held from two sessions out broke even (row 65 sect. 5)? Not a new experiment:
an accounting check on rows 65 and 70's own in-sample numbers.

Per weekly expiry X (entry day E = two sessions before, M = the session before X):
  held:  sell ATM K0 at E 15:15 (P0) -> K0 at M 15:15 (P1) -> K0 at X 15:15 (P2) -> intrinsic S
         P0 - S = (P0 - P1) + (P1 - P2) + (P2 - S)
  daily: day 1 = P0 - P1 (the same trade); day 2 = fresh ATM K1 at M 15:15 (Q1) -> X 15:15 (Q2)
Plus quote quality at every 15:15 lookup: how old the last bar is, strike collisions between
offsets, and put-call parity against the bar's spot.

Run: stockey/.xstockey/bin/python research/scripts/2026-10-21_nifty_daily_vs_hold_reconcile.py
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
AT = time(15, 15)


def raw_at(day, at=AT):
    """The rows chain_at builds its dict from, with timestamps, spot and offsets kept."""
    end = pd.Timestamp(datetime.combine(day, at)).tz_localize(r64.IST)
    start = end - pd.Timedelta(minutes=r64.LOOKBACK_MIN)
    df = q("SELECT strike_offset, option_type, ts, strike, close, spot, volume FROM dhan_option_rolling "
           "WHERE underlying = 'NIFTY' AND expiry_flag = 'WEEK' AND expiry_code = 1 AND interval_minutes = 1 "
           "AND ts > %(s)s AND ts <= %(e)s",
           {"s": start.tz_convert("UTC").to_pydatetime(), "e": end.tz_convert("UTC").to_pydatetime()})
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df[df["ts"] + pd.Timedelta(minutes=1) <= end.tz_convert("UTC")]
    last = df.sort_values("ts").groupby(["strike_offset", "option_type"]).tail(1).copy()
    last["age_min"] = (end.tz_convert("UTC") - (last["ts"] + pd.Timedelta(minutes=1))).dt.total_seconds() / 60
    return last


def main():
    sessions, closes = r64.sessions_and_closes()
    rows, ages, collisions, parity = [], [], 0, []
    for x in r64.expiries("2021-10-21", "2024-10-31"):
        if x not in closes:
            continue
        i = sessions.index(x)
        e, m = sessions[i - 2], sessions[i - 1]
        snaps = {d: raw_at(d) for d in (e, m, x)}
        for d, s in snaps.items():
            ages.extend(s.loc[s["strike_offset"].abs() <= 2, "age_min"].tolist())
            dup = s.groupby(["option_type", "strike"]).size()
            collisions += int((dup > 1).sum())
            ce = s[s["option_type"] == "CE"].set_index("strike")
            pe = s[s["option_type"] == "PE"].set_index("strike")
            for k in ce.index.intersection(pe.index).unique():
                c_, p_ = ce.loc[[k]].iloc[-1], pe.loc[[k]].iloc[-1]
                if abs(k - c_["spot"]) <= 100:
                    parity.append((c_["close"] - p_["close"]) - (c_["spot"] - k))
        ch = {d: r64.chain_at(d, AT) for d in (e, m, x)}
        (c0, k0), (c1, k1), (c2, _) = ch[e], ch[m], ch[x]
        if k0 is None or k1 is None:
            continue

        def strad(c, k):
            return c[("CE", k)][0] + c[("PE", k)][0] if ("CE", k) in c and ("PE", k) in c else np.nan
        p0, p1, p2 = strad(c0, k0), strad(c1, k0), strad(c2, k0)
        q1, q2 = strad(c1, k1), strad(c2, k1)
        rows.append({"x": x, "p0": p0, "p1": p1, "p2": p2, "s": abs(closes[x] - k0),
                     "q1": q1, "q2": q2, "move_em": abs(k1 - k0)})
    d = pd.DataFrame(rows).dropna()
    out = [f"Daily-vs-hold reconciliation, {len(d)} weekly expiries 2021-10..2024-10 (points, gross)"]
    out.append(f"  held from E:   P0 {d.p0.mean():6.1f}  -> P1 {d.p1.mean():6.1f} -> P2 {d.p2.mean():6.1f} -> intrinsic {d.s.mean():6.1f}")
    out.append(f"    seller P&L: day 1 (E->M) {(d.p0 - d.p1).mean():+6.1f}   day 2 (M->X 15:15) {(d.p1 - d.p2).mean():+6.1f}   "
               f"last 15 min {(d.p2 - d.s).mean():+6.1f}   total {(d.p0 - d.s).mean():+6.1f}  ({100 * (d.p0 - d.s).sum() / d.p0.sum():+.1f}% of P0)")
    out.append(f"  daily day 2 (fresh ATM at M): Q1 {d.q1.mean():6.1f} -> Q2 {d.q2.mean():6.1f}   seller {(d.q1 - d.q2).mean():+6.1f}")
    out.append(f"  day 2 held vs fresh, split by how far Nifty moved on day 1 (|K1 - K0|):")
    for lo_, hi_ in [(0, 0), (50, 100), (150, 10000)]:
        g = d[(d.move_em >= lo_) & (d.move_em <= hi_)]
        out.append(f"    moved {lo_}-{hi_ if hi_ < 10000 else '+'} pts: n={len(g):3d}  held day 2 {(g.p1 - g.p2).mean():+6.1f}  "
                   f"fresh day 2 {(g.q1 - g.q2).mean():+6.1f}  held total {(g.p0 - g.s).mean():+6.1f}")
    a = np.array(ages)
    out.append(f"\n  quote age at 15:15, offsets -2..+2 (minutes since the bar closed): median {np.median(a):.0f}, "
               f"95th pct {np.percentile(a, 95):.0f}, max {a.max():.0f}; share over 5 min {100 * (a > 5).mean():.1f}%")
    out.append(f"  strike collisions between offsets (same strike, two bars): {collisions} across {3 * len(rows)} snapshots")
    p = np.array(parity)
    out.append(f"  put-call parity, (C - P) - (spot - K), strikes within 100 pts: median {np.median(p):+.1f} pts, "
               f"5-95% {np.percentile(p, 5):+.1f}..{np.percentile(p, 95):+.1f} (carry to expiry is a few pts; wide = stale or mismatched)")
    # Survivorship check on row 70: a day whose strike left Dhan's +-10 range by the next
    # 15:15 was SKIPPED there -- the biggest moves, the seller's worst days. Price every
    # skipped day from the bhavcopy close (NSE's 15:00-15:30 average) of the same contract.
    out.append("\n  row 70's daily trade, days skipped because the strike was out of Dhan's range next day:")
    exps = sorted(r64.expiries("2021-10-21", "2024-11-30"))
    full, skipped = [], []
    for j, dd in enumerate(sessions[:-1]):
        if dd < pd.Timestamp("2021-10-21").date() or dd > pd.Timestamp("2024-10-30").date() or dd in exps:
            continue
        nn = sessions[j + 1]
        c0, k = r64.chain_at(dd, AT)
        if k is None or ("CE", k) not in c0 or ("PE", k) not in c0:
            continue
        p0 = c0[("CE", k)][0] + c0[("PE", k)][0]
        c1, _ = r64.chain_at(nn, AT)
        if ("CE", k) in c1 and ("PE", k) in c1:
            full.append((p0, p0 - c1[("CE", k)][0] - c1[("PE", k)][0]))
            continue
        xe = min(e for e in exps if e > dd)
        b = q("SELECT option_type, close FROM nseindia_fo_bhavcopy WHERE trade_date = %(n)s AND symbol = 'NIFTY' "
              "AND instrument = 'OPTIDX' AND expiry = %(x)s AND strike = %(k)s", {"n": nn, "x": xe, "k": k})
        if len(b) == 2:
            skipped.append((dd, p0, p0 - b["close"].sum()))
        else:
            skipped.append((dd, p0, np.nan))
    f = np.array(full)
    sk = pd.DataFrame(skipped, columns=["d", "prem", "pnl"])
    out.append(f"    kept {len(f)} days: mean {100 * (f[:, 1] / f[:, 0]).mean():+.2f}% of premium")
    out.append(f"    skipped {len(sk)} days, priced from the bhavcopy: mean {100 * (sk.pnl / sk.prem).mean():+.1f}% of premium, "
               f"worst {100 * (sk.pnl / sk.prem).min():+.0f}%, unpriced {sk.pnl.isna().sum()}")
    for r in sk.itertuples():
        out.append(f"      {r.d}  premium {r.prem:6.1f}  seller {r.pnl:+7.1f} pts")
    allp = np.r_[f[:, 1] / f[:, 0], (sk.pnl / sk.prem).dropna().values]
    out.append(f"    ALL days together: mean {100 * allp.mean():+.2f}% of premium a day, hit {100 * (allp > 0).mean():.0f}%, n={len(allp)}")
    text = "\n".join(out)
    print(text)
    path = os.path.join(ROOT, "research", "reports", "2026-10-21_nifty_daily_vs_hold_reconcile.txt")
    open(path, "w").write(text + "\n")
    print(f"\nwritten {path}")


if __name__ == "__main__":
    main()
