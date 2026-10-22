"""LEDGER row 72 -- EXPLORATION (trials=0): what Dhan's algo marketplace's Nifty option strategies
actually earned (operator, 2026-10-22: "a good starting point").

Source: algos.dhan.co, the public lists by trading style and each strategy's public trade
history (the site's own open endpoints GetAllAlgoList / GetQuantStats / GetHistoricalTrades),
fetched 2026-10-22 and archived in research/imports/dhan_algos_2026-10-22/trades.tar.gz.
61 Nifty option strategies, all from one vendor (Stratzy); every trade's legs, prices,
open/close time, P&L per lot and margin per lot.

Their P&L is GROSS: (entry - exit) x quantity, no charges, at signal prices. Here:
  costs  statutory (STT on the sell side, NSE 0.03503%, SEBI, stamp on buys, 18% GST), Dhan
         Rs 20 per order at one lot, slippage 0.25 pt per leg per side
  return sum of per-trade P&L / that trade's margin, per year (not compounded)
  live   trades on or after the strategy's liveSince date; earlier ones were not live
Direction: for single-short credit spreads (sell one option, buy one further out), was Nifty's
move from entry to exit their way (spot from dhan_option_rolling), and does the direction
follow the morning, the gap, yesterday or the last five days?

Caveats: one vendor; only strategies still listed (survivorship); about a year of live record,
one market; signal fills, not account fills.
Run: stockey/.xstockey/bin/python research/scripts/2026-10-22_dhan_algos_review.py
"""

import glob
import importlib.util
import json
import os
import statistics as stat
import tarfile
import tempfile
from datetime import datetime

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
ARCHIVE = os.path.join(ROOT, "research", "imports", "dhan_algos_2026-10-22", "trades.tar.gz")
spec = importlib.util.spec_from_file_location("r64", os.path.join(HERE, "2026-10-20_nifty_option_selling.py"))
r64 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r64)
SELLING = ("Credit Spread", "Short Strangle", "Short Straddle")


def load():
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(ARCHIVE) as t:
            t.extractall(tmp, filter="data")
        for f in glob.glob(os.path.join(tmp, "all", "*.json")):
            out.append(json.load(open(f)))
    return out


def trade_cost(x):
    """Rupees per lot for one trade, round trip."""
    dt = x["createdAt"][:10]
    stt_rate = 0.001 if dt >= "2024-10-01" else 0.000625
    c = 0.0
    for leg in x["trades"]:
        q = leg.get("quantity", 1) * leg["multiplier"]
        e, xp = leg["entryPrice"], leg.get("avgExitPrice") or leg["exitPrice"]
        sell, buy = (e, xp) if leg["transactionType"] == "SELL" else (xp, e)
        turn = (e + xp) * q
        exch, sebi = 0.0003503 * turn, 0.000001 * turn
        c += stt_rate * sell * q + exch + sebi + 0.00003 * buy * q + 0.18 * (exch + sebi + 40) + 40 + 2 * 0.25 * q
    return c


def per_year(T, lo=None, hi=None, net=True):
    g, ds = [], []
    for x in T:
        if x.get("signalPnl") is None or not x.get("marginRequired"):
            continue
        dt = x["createdAt"][:10]
        if (lo and dt < lo) or (hi and dt >= hi):
            continue
        g.append((x["signalPnl"] - (trade_cost(x) if net else 0)) / x["marginRequired"])
        ds.append(dt)
    if len(g) < 20:
        return None
    yrs = max((datetime.fromisoformat(max(ds)) - datetime.fromisoformat(min(ds))).days / 365.25, 0.08)
    return 100 * sum(g) / yrs


def direction(data):
    parts = []
    for m in pd.period_range("2025-05", "2026-10", freq="M"):
        a = m.start_time.tz_localize(r64.IST).tz_convert("UTC").to_pydatetime()
        b = (m + 1).start_time.tz_localize(r64.IST).tz_convert("UTC").to_pydatetime()
        parts.append(r64.q("SELECT ts, spot FROM dhan_option_rolling WHERE underlying = 'NIFTY' AND expiry_flag = 'WEEK' "
                           "AND expiry_code = 1 AND interval_minutes = 1 AND strike_offset = 0 AND option_type = 'CE' "
                           "AND ts >= %(a)s AND ts < %(b)s", {"a": a, "b": b}))
    sp = pd.concat(parts)
    sp["ts"] = pd.to_datetime(sp["ts"], utc=True)
    sp = sp.drop_duplicates("ts").set_index("ts")["spot"].sort_index()
    day = sp.groupby(sp.index.tz_convert(r64.IST).date)
    dclose, dopen = day.last(), day.first()
    days = list(dclose.index)

    def at(t):
        i = sp.index.searchsorted(t, side="right") - 1
        return sp.iloc[i] if i >= 0 else np.nan
    rows = []
    for d in data:
        m = d["meta"]
        if m["displayCategory"] != "Credit Spread":
            continue
        for t in d["trades"] or []:
            sells = [leg for leg in t["trades"] if leg["transactionType"] == "SELL"]
            if len(sells) != 1:
                continue
            a, b = pd.Timestamp(t["createdAt"]), pd.Timestamp(t["closedOn"])
            if a < sp.index.min() or b > sp.index.max():
                continue
            dd = a.tz_convert(r64.IST).date()
            prev = [x for x in days if x < dd]
            if len(prev) < 6 or dd not in dopen.index:
                continue
            s0, s1, pc = at(a), at(b), dclose[prev[-1]]
            rows.append(dict(strat=m["STRATEGY_NAME"], d=dd, dir=1 if sells[0]["optionType"] == "PE" else -1,
                             live=str(dd) >= m["liveSince"][:10], fwd=np.log(s1 / s0),
                             morning=np.log(s0 / dopen[dd]), gap=np.log(dopen[dd] / pc),
                             prev1=np.log(pc / dclose[prev[-2]]), prev5=np.log(pc / dclose[prev[-6]]),
                             hold=(b - a).total_seconds() / 86400, hour=a.tz_convert(r64.IST).hour,
                             pnl=t["signalPnl"] / t["marginRequired"]))
    return pd.DataFrame(rows)


def main():
    data = load()
    out = ["Dhan algo marketplace, Nifty option strategies (Stratzy), fetched 2026-10-22 -- EXPLORATION",
           "per year, % of margin per lot, not compounded; net = after statutory costs, Rs 20/order, 0.25 pt slippage/leg/side", ""]
    rows = []
    for d in data:
        m, T = d["meta"], d["trades"] or []
        live = m["liveSince"][:10]
        rows.append((m["displayCategory"] or "-", m["STRATEGY_NAME"][:36], live, len(T),
                     per_year(T, net=False), per_year(T), per_year(T, hi=live), per_year(T, lo=live)))
    rows.sort(key=lambda r: (r[0], -(r[7] if r[7] is not None else -1e9)))
    f = lambda v: f"{v:+8.1f}" if v is not None else "       -"
    out.append(f"{'type':14s} {'strategy':36s} {'live from':10s} {'trades':>6s} {'gross':>8s} {'net':>8s} {'net pre':>8s} {'net live':>8s}")
    for r in rows:
        out.append(f"{r[0]:14s} {r[1]:36s} {r[2]} {r[3]:6d} {f(r[4])} {f(r[5])} {f(r[6])} {f(r[7])}")
    out.append("\nby type, LIVE trades only (strategies with >= 20 live trades):")
    for cat in SELLING + ("Option Buying",):
        v = [r[7] for r in rows if r[0] == cat and r[7] is not None]
        g = [r[4] for r in rows if r[0] == cat and r[4] is not None]
        if v:
            out.append(f"  {cat:15s} n={len(v):2d}  gross (all) median {stat.median(g):+7.1f}   net live median {stat.median(v):+7.1f}, "
                       f"mean {stat.mean(v):+7.1f}, positive {sum(x > 0 for x in v)}/{len(v)}")
    out.append("  (option buying: margin = one lot's premium, so its % is on a tiny base and not comparable)")

    df = direction(data)
    df["right"] = np.sign(df.fwd) == df.dir
    out.append(f"\ncredit spreads, direction (single short leg; {len(df)} trades with Nifty spot at entry and exit):")
    out.append(f"  Nifty moved their way entry->exit: {100 * df.right.mean():.1f}%; trade profitable: {100 * (df.pnl > 0).mean():.1f}%; "
               f"mean move their way {1e4 * (df.fwd * df.dir).mean():+.1f} bp")
    for c in ["morning", "gap", "prev1", "prev5"]:
        out.append(f"  direction agrees with the sign of {c:8s}: {100 * (np.sign(df[c]) == df.dir).mean():5.1f}%  "
                   f"(corr {np.corrcoef(df.dir, df[c])[0, 1]:+.2f})")
    dd = df.groupby("d").dir.mean()
    out.append(f"  days on which every strategy trading took the same side: {100 * (dd.abs() == 1).mean():.0f}% of {len(dd)}")
    out.append(f"  holding time median {df.hold.median():.2f} days; entry hour IST: "
               + ", ".join(f"{h}h {n}" for h, n in df.hour.value_counts().sort_index().items()))
    g = df[df.live].groupby("strat")
    t = pd.DataFrame({"n": g.size(), "right %": 100 * g.right.mean(), "pnl % margin": 100 * g.pnl.sum()}).round(1)
    out.append("\n  live, per strategy (direction-right rate vs result):\n" + t.sort_values("pnl % margin").to_string())
    text = "\n".join(out)
    print(text)
    path = os.path.join(ROOT, "research", "reports", "2026-10-22_dhan_algos_review.txt")
    open(path, "w").write(text + "\n")
    print(f"\nwritten {path}")


if __name__ == "__main__":
    main()
