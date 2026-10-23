"""LEDGER row 79 -- EXPLORATION (trials=0): does buying wings make E1 (row 78's expiry-day stopped
straddle basket) cheaper on margin without giving up its edge?

A. Margin, measured: row 72's Dhan marketplace archive (2025-05..2026-10) carries Dhan's
   marginRequired per lot for every trade, with each leg's strike. Classified by structure -- naked
   (short legs only) vs hedged (every short leg covered by a long one further out) -- and by whether
   the trade was entered ON its expiry day (SEBI's extra 2% ELM on short options, from 2024-11-20).
   Naked: margin / notional (short strike x lot). Hedged: margin / maximum loss ((width - credit) x lot).
B. P&L, in-sample only (2021-10..2024-10 expiry days; the 2024-11..2026-09 check is burned): E1's six
   straddles exactly as the library, plus a long call at K+W and a long put at K-W bought at the same
   minute and sold at 15:15 (W = 200 / 300 / 400 / 500), net of the same costs; a wing missing at exit
   is worth 0 (rows 73-74). Reported in bps of notional and per rupee of margin using A's medians.
No claim: exploration, and A's margins are 2025-26's applied to 2021-24 trades.
Run: stockey/.xstockey/bin/python research/scripts/2026-10-22_e1_wings_margin.py
"""

import importlib.util
import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
WINGS = (200, 300, 400, 500)
STOPS = (1.25, 1.5)


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, file))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


lib = load("lib", "2026-10-22_nifty_algo_library.py")
rev = load("rev", "2026-10-22_dhan_algos_review.py")
r64, r73 = lib.r64, lib.r73


def margins():
    rows = []
    for d in rev.load():
        for t in d["trades"] or []:
            legs = [x for x in t.get("trades") or [] if x.get("instrumentType") == "OPT"]
            m, lot = t.get("marginRequired"), None
            if not legs or not m:
                continue
            lot = legs[0].get("multiplier")
            shorts = [x for x in legs if x["transactionType"] == "SELL"]
            longs = [x for x in legs if x["transactionType"] == "BUY"]
            if not shorts or not lot:
                continue
            entry = pd.Timestamp(t["createdAt"]).tz_convert("Asia/Kolkata").date()
            expiry = pd.Timestamp(legs[0]["expiry"]).tz_convert("Asia/Kolkata").date()
            covered = all(any(l["optionType"] == s["optionType"] and
                              (l["strikePrice"] > s["strikePrice"] if s["optionType"] == "CE" else l["strikePrice"] < s["strikePrice"])
                              for l in longs) for s in shorts)
            kind = "naked" if not longs else ("hedged" if covered else "other")
            notional = np.mean([s["strikePrice"] for s in shorts]) * lot
            row = {"kind": kind, "expiry_day": entry == expiry, "margin_pct_notional": m / notional,
                   "n_short": len(shorts), "n_long": len(longs)}
            if kind == "hedged":
                credit = sum(s["entryPrice"] for s in shorts) - sum(l["entryPrice"] for l in longs)
                width = max(abs(l["strikePrice"] - s["strikePrice"]) for s in shorts for l in longs
                            if l["optionType"] == s["optionType"])
                row["margin_per_max_loss"] = m / max((width - credit) * lot, 1.0)
                row["width"] = width
            rows.append(row)
    return pd.DataFrame(rows)


def leg_pnl(day, bars, k, side, at, lot):
    """One long option from `at` to 15:15, net points; missing at exit -> 0 (sold for nothing)."""
    s = lib.leg_series(bars, side, k)
    t0, t1 = r73.ts(day, at), r73.ts(day, lib.OUT)
    p0 = lib.entry_price(s, t0)
    if p0 is None:
        return None
    life = s[(s.index > t0) & (s.index <= t1)]
    x = float(life["close"].iloc[-1]) if not life.empty and life.index[-1] >= t1 - lib.STALE else 0.0
    return x - p0 - r73.fees(p0, True, day, lot) - r73.fees(x, False, day, lot) - 2 * lib.SLIP


def leg_expire(day, bars, k, side, at, lot, spot_close):
    """One long option bought at `at` and left to expire: worth its intrinsic value at the close
    (STT 0.125% on an exercised ITM option), no exit order, no exit slippage."""
    p0 = lib.entry_price(lib.leg_series(bars, side, k), r73.ts(day, at))
    if p0 is None or spot_close is None:
        return None
    iv = max(spot_close - k, 0.0) if side == "CE" else max(k - spot_close, 0.0)
    return iv - p0 - r73.fees(p0, True, day, lot) - lib.SLIP - (0.00125 * iv if iv > 0 else 0.0)


def main():
    out = ["LEDGER 79 -- E1 with wings: margin and P&L (EXPLORATION, trials=0)", ""]
    mg = margins()
    out.append(f"A. Dhan marketplace margins per lot, {len(mg)} trades (2025-05..2026-10):")
    for (k, e), g in mg.groupby(["kind", "expiry_day"]):
        line = f"   {k:7s} {'expiry day' if e else 'other days':10s} n {len(g):5d}  margin/notional median {100 * g.margin_pct_notional.median():5.1f}%"
        if k == "hedged":
            line += f"  margin/max loss median {g.margin_per_max_loss.median():.2f}x  (width median {g.width.median():.0f})"
        out.append(line)
    naked_e = mg[(mg.kind == "naked") & mg.expiry_day].margin_pct_notional.median()
    naked_o = mg[(mg.kind == "naked") & ~mg.expiry_day].margin_pct_notional.median()
    hedged_m = mg[(mg.kind == "hedged") & mg.expiry_day].margin_per_max_loss.median()
    if np.isnan(hedged_m):
        hedged_m = mg[mg.kind == "hedged"].margin_per_max_loss.median()
    naked = naked_e if not np.isnan(naked_e) else naked_o

    lo, hi = (pd.Timestamp(v).date() for v in lib.PERIODS["insample"])
    sessions, _ = r64.sessions_and_closes()
    cmap = r73.contract_map(sessions, str(lo), str(hi))
    _, closes = r64.sessions_and_closes()
    lots = r73.lots()
    rows = []
    for label, d in sorted(cmap.items()):
        if not (lo <= d <= hi):
            continue
        b = lib.day_bars(d)
        if b.empty:
            continue
        lot = lots.get(label, 50.0)
        for e, at in lib.ENTRIES.items():
            k, spot = lib.atm_at(b, r73.ts(d, at))
            if k is None:
                continue
            credit = None
            for st in STOPS:
                s = lib.short_straddle(d, b, at, st, lot)
                if s is None:
                    continue
                if credit is None:
                    credit = sum(lib.entry_price(lib.leg_series(b, side, k), r73.ts(d, at)) or np.nan for side in ("CE", "PE"))
                row = {"day": d, "entry": e, "stop": st, "spot": spot, "credit": credit, "W0": s["r"]}
                for w in WINGS:
                    c, p = leg_pnl(d, b, k + w, "CE", at, lot), leg_pnl(d, b, k - w, "PE", at, lot)
                    row[f"W{w}"] = np.nan if c is None or p is None else s["r"] + (c + p) / spot
                    c, p = (leg_expire(d, b, k + w, "CE", at, lot, closes.get(d)),
                            leg_expire(d, b, k - w, "PE", at, lot, closes.get(d)))
                    row[f"X{w}"] = np.nan if c is None or p is None else s["r"] + (c + p) / spot
                rows.append(row)
    df = pd.DataFrame(rows)
    basket = df.groupby("day")[["W0"] + [f"W{w}" for w in WINGS] + [f"X{w}" for w in WINGS]].mean()
    cr = df.groupby("day")["credit"].mean()
    sp = df.groupby("day")["spot"].mean()
    out.append(f"\nB. In-sample expiry days {basket.index.min()}..{basket.index.max()}, n {len(basket)} (E1 basket, six members each):")
    out.append(f"   margin model (from A): naked {100 * naked:.1f}% of notional (expiry-day trades), hedged {hedged_m:.2f}x max loss")
    out.append(f"   {'version':17s} {'bps notional':>12s} {'t':>6s} {'by year 2022/2023/2024':>24s} {'margin %notional':>16s} {'return/margin per yr':>21s}")
    yrs = pd.Series([d.year for d in basket.index], index=basket.index)
    per_year = len(basket) / ((basket.index.max() - basket.index.min()).days / 365.25)
    for c in basket:
        x = basket[c].dropna()
        if c == "W0":
            m_pct = naked
            name = "straddle (E1)"
        else:
            w = int(c[1:])
            m_pct = hedged_m * float(((w - cr) / sp).median())  # max loss per unit = wing - credit
            name = f"wings {w}" + (" expire" if c[0] == "X" else " sold")
        by = "/".join(f"{1e4 * x[yrs[x.index] == y].mean():+.1f}" for y in (2022, 2023, 2024))
        out.append(f"   {name:17s} {1e4 * x.mean():+12.2f} {x.mean() / x.std() * np.sqrt(len(x)):+6.2f} {by:>24s} "
                   f"{100 * m_pct:15.1f}% {100 * x.mean() * per_year / m_pct:+20.1f}%")
    out.append("   (return/margin: mean per expiry x expiries per year / margin, uncompounded, one position at a time;"
               " margin idle on other days)")
    for pre, lab in (("W", "sold at 15:15"), ("X", "left to expire")):
        out.append(f"   cost of the wings {lab}, bps of notional per expiry: " + "  ".join(
            f"{w}: {1e4 * (basket[f'{pre}{w}'] - basket['W0']).mean():+.2f}" for w in WINGS))
    out.append("   worst expiry, bps: " + "  ".join(f"{c}: {1e4 * basket[c].min():+.1f}" for c in basket))
    text = "\n".join(out)
    print(text)
    path = os.path.join(ROOT, "research", "reports", "2026-10-22_e1_wings_margin.txt")
    open(path, "w").write(text + "\n")
    print(f"\nwritten {path}")


if __name__ == "__main__":
    main()
