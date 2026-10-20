"""Three quick EXPLORATION checks on Nifty, data already in hand (LEDGER rows 61-63, trials=0).

A. Is option selling paid in India? India VIX against the volatility Nifty then
   delivered over the next 21 sessions, and a rough sold-straddle proxy.
B. Trend-following on Nifty / Bank Nifty futures, long and short, daily bars:
   Carver's EWMAC 16/64, 32/128, 64/256 (row 1, adopted a priori), 20% vol target.
C. Intraday momentum (Gao, Han, Li, Zhou 2018): does the first half-hour's return
   (previous close -> 09:45 IST) predict the last half-hour's (15:00 -> 15:30)?
   NIFTYBEES 1-minute bars, the only Nifty intraday series we hold.

No significance claims, no parameters chosen from these numbers: each answers
"is this worth collecting data and pre-registering a real test for".

Run: stockey/.xstockey/bin/python research/scripts/2026-10-20_nifty_quick_checks.py
"""

import os
import sys

import numpy as np
import pandas as pd
import psycopg2

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))


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
    return pd.read_sql(sql, CONN, params=params)


def index_series(name):
    df = q("SELECT date::date AS date, close, div_yield FROM nseindia_indices "
           "WHERE index_name = %(n)s AND close > 0 ORDER BY date", {"n": name})
    df = df.drop_duplicates("date").set_index("date")
    # One-day spikes that fully reverse are bad rows, not sessions: nseindia_indices
    # has junk weekend rows on 2023-06-04 and 2023-11-04 (Nifty 17,599 / 17,722 between
    # ~18,500 and ~19,200 closes), in all three series. Reported to stockey 2026-10-20.
    c = df["close"]
    up, down = c / c.shift(1) - 1, c.shift(-1) / c - 1
    spike = (up.abs() > 0.03) & (down.abs() > 0.03) & (np.sign(up) != np.sign(down)) & \
            ((c.shift(-1) / c.shift(1) - 1).abs() < 0.015) & \
            (pd.to_datetime(df.index).dayofweek >= 5)  # real volatile days reverse too; only weekend rows are junk
    if name != "India VIX":
        bad = set(df.index[spike])
        BAD_DATES.update(bad)
    return df[~df.index.isin(BAD_DATES)]


BAD_DATES = set()


def repo_rate():
    df = q("SELECT date::date AS date, repo_rate FROM rbi_bank_rates WHERE repo_rate IS NOT NULL ORDER BY date")
    return df.drop_duplicates("date").set_index("date")["repo_rate"]


def max_dd(equity):
    peak = np.maximum.accumulate(equity)
    return float((equity / peak - 1).min())


def stats(daily, label):
    daily = daily.dropna()
    n = len(daily)
    if n == 0:
        return f"{label:<34} (no data)"
    ann = daily.mean() * 252
    vol = daily.std() * np.sqrt(252)
    eq = np.cumprod(1 + daily.values)
    cagr = eq[-1] ** (252 / n) - 1
    sr = ann / vol if vol > 0 else float("nan")
    return f"{label:<34} CAGR {cagr*100:6.1f}%  vol {vol*100:5.1f}%  SR {sr:5.2f}  maxDD {max_dd(eq)*100:6.1f}%"


out = []


def p(s=""):
    print(s)
    out.append(s)


# ---------------------------------------------------------------- A. option selling
def check_a():
    p("=" * 90)
    p("A. IS OPTION SELLING PAID? India VIX vs Nifty's next-21-session realised volatility")
    p("=" * 90)
    nifty = index_series("Nifty 50")
    vix = index_series("India VIX")["close"].rename("vix")
    df = nifty.join(vix, how="inner")
    p(f"Dropped as bad rows (one-day spikes that fully reverse): {sorted(str(d) for d in BAD_DATES)}")
    df["r"] = np.log(df["close"]).diff()
    h = 21
    # realised vol over the NEXT h sessions, annualised, in VIX points
    fwd_sq = df["r"].pow(2).rolling(h).sum().shift(-h)
    df["rv"] = np.sqrt(fwd_sq * 252 / h) * 100
    df["move"] = (df["close"].shift(-h) / df["close"] - 1).abs()
    s = df.dropna(subset=["rv", "vix"]).iloc[::h].copy()  # non-overlapping months
    s["spread"] = s["vix"] - s["rv"]
    p(f"Sample: {s.index[0]} .. {s.index[-1]}, {len(s)} non-overlapping 21-session windows")
    p(f"Mean VIX {s['vix'].mean():.1f}, mean realised {s['rv'].mean():.1f}, mean VIX - realised {s['spread'].mean():+.1f} pts")
    p(f"VIX above realised in {100*(s['spread']>0).mean():.0f}% of windows; median spread {s['spread'].median():+.1f}")
    worst = s.nsmallest(5, "spread")
    p("Five worst windows (realised far above VIX):")
    for d, r in worst.iterrows():
        p(f"   {d}  VIX {r['vix']:5.1f}  realised {r['rv']:5.1f}  spread {r['spread']:+6.1f}")
    p("")
    p("By year:  windows  mean VIX  mean realised  spread  % VIX above")
    for y, g in s.groupby(pd.to_datetime(s.index).year):
        p(f"   {y}   {len(g):3d}     {g['vix'].mean():5.1f}      {g['rv'].mean():5.1f}      {g['spread'].mean():+5.1f}     {100*(g['spread']>0).mean():3.0f}%")

    # Sold-straddle proxy: an at-the-money 30-day straddle is worth about
    # 0.798 * sigma * sqrt(T) of the index; priced here at VIX (ATM implied vol
    # usually sits a little BELOW VIX, so this flatters the seller), held to expiry,
    # unhedged. Cost: 3% of the premium (STT on the sale, spreads, brokerage).
    T = 30 / 365
    s["premium"] = 0.798 * (s["vix"] / 100) * np.sqrt(T)
    s["pnl"] = s["premium"] * 0.97 - s["move"]  # fraction of notional
    margin = 0.15  # roughly what a short Nifty straddle blocks, fraction of notional
    p("")
    p("Sold 30-day ATM straddle proxy (priced at VIX, held to expiry, unhedged, 3% of premium as cost):")
    p(f"   mean premium {s['premium'].mean()*100:.2f}% of notional, mean |move| {s['move'].mean()*100:.2f}%")
    p(f"   mean P&L {s['pnl'].mean()*100:+.2f}% of notional per month, win rate {100*(s['pnl']>0).mean():.0f}%")
    p(f"   worst month {s['pnl'].min()*100:+.2f}% of notional = {s['pnl'].min()/margin*100:+.0f}% of a 15% margin")
    eq = 1 + (s["pnl"] / margin).cumsum()  # simple (not compounded) on margin
    p(f"   on 15% margin, non-compounded: {s['pnl'].mean()/margin*12*100:+.0f}%/yr average, deepest fall {(eq/eq.cummax()-1).min()*100:.0f}% of peak")
    p("   by year (% of notional, summed):  " + "  ".join(
        f"{y}:{g['pnl'].sum()*100:+.1f}" for y, g in s.groupby(pd.to_datetime(s.index).year)))
    p("")


# ---------------------------------------------------------------- B. trend
CARVER = {(16, 64): 3.75, (32, 128): 2.65, (64, 256): 1.87}


def ewmac_forecast(price, vol_px):
    fs = []
    for (f, s), scalar in CARVER.items():
        raw = price.ewm(span=f, adjust=False).mean() - price.ewm(span=s, adjust=False).mean()
        fs.append((raw / vol_px * scalar).clip(-20, 20))
    return (sum(fs) / len(fs)).clip(-20, 20)


def check_b():
    p("=" * 90)
    p("B. TREND-FOLLOWING ON INDEX FUTURES, long and short, daily (EWMAC 16/64+32/128+64/256, 20% vol target)")
    p("=" * 90)
    p("Returns are EXCESS over cash (the collateral earns the repo rate on top). Futures carry")
    p("modelled as spot return - (repo - dividend yield)/252. Cost 3 bps per unit notional traded,")
    p("plus a monthly roll (both legs) on the open position. Decided at the close, earned next day.")
    rr = repo_rate()
    for name in ["Nifty 50", "Nifty Bank"]:
        df = index_series(name)
        df = df.join(rr.rename("repo"), how="left")
        df["repo"] = df["repo"].ffill().bfill()
        df["div_yield"] = df["div_yield"].ffill().fillna(1.2)
        px = df["close"]
        ret = px.pct_change()
        carry = (df["repo"] - df["div_yield"]) / 100 / 252
        fut = ret - carry  # long futures' daily excess return
        vol_d = ret.ewm(span=36, adjust=False).std()  # daily return vol
        vol_px = vol_d * px
        fc = ewmac_forecast(px, vol_px)
        target = 0.20
        lev = (target / (vol_d * np.sqrt(252))).clip(upper=4)
        cost = 0.0003
        roll_every = 21

        def run(pos):
            pos = pos.shift(1)  # decided yesterday
            gross = pos * fut
            trade = pos.diff().abs()
            rolls = pd.Series(0.0, index=pos.index)
            rolls.iloc[::roll_every] = 2 * pos.abs().iloc[::roll_every]
            return gross - (trade + rolls) * cost, pos

        warm = 260
        ls, pls = run(fc / 10 * lev)
        lo, plo = run((fc / 10 * lev).clip(lower=0))
        bh, _ = run(lev * 0 + 1.0)
        bhv, _ = run(lev)
        sl = slice(warm, None)
        p("")
        p(f"{name}: {df.index[warm]} .. {df.index[-1]}")
        p("   " + stats(ls.iloc[sl], "trend long+short"))
        p("   " + stats(lo.iloc[sl], "trend long-only"))
        p("   " + stats(bhv.iloc[sl], "always long, same vol target"))
        p("   " + stats(bh.iloc[sl], "always long 1x"))
        p(f"   average |position| {pls.iloc[sl].abs().mean():.2f}x notional, short {100*(pls.iloc[sl]<0).mean():.0f}% of days;"
          f" corr(trend L+S, always long) {ls.iloc[sl].corr(bhv.iloc[sl]):.2f}")
        yr = pd.DataFrame({"ls": ls, "lo": lo, "bh": bhv}).iloc[sl]
        yr.index = pd.to_datetime(yr.index)
        g = (1 + yr).groupby(yr.index.year).prod() - 1
        p("   by year (L+S / long-only / always long):  " + "  ".join(
            f"{y}:{r['ls']*100:+.0f}/{r['lo']*100:+.0f}/{r['bh']*100:+.0f}" for y, r in g.iterrows()))
    p("")


# ---------------------------------------------------------------- C. intraday momentum
def check_c():
    p("=" * 90)
    p("C. INTRADAY MOMENTUM: first half-hour (prev close -> 09:45 IST) vs last half-hour (15:00 -> 15:30)")
    p("=" * 90)
    df = q("SELECT timestamp, close FROM dhan_ohlcv_intraday WHERE ticker = 'NIFTYBEES' "
           "AND exchange_segment = 'NSE_EQ' AND timestamp >= '2021-08-01' AND timestamp < '2026-10-21' ORDER BY timestamp")
    df["ts"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("Asia/Kolkata")
    df["day"] = df["ts"].dt.date
    df["hm"] = df["ts"].dt.strftime("%H:%M")
    # A bar is stamped at its start: the close of the 09:44 bar is the price at 09:45.
    piv = df.pivot_table(index="day", columns="hm", values="close", aggfunc="last")

    def at(hm):
        return piv[hm] if hm in piv.columns else pd.Series(np.nan, index=piv.index)

    last = df.groupby("day")["close"].last()
    rows = pd.DataFrame({
        "prev": last.shift(1),
        "p0944": at("09:44"),
        "p1429": at("14:29"),
        "p1459": at("14:59"),
        "p1529": at("15:29"),
    }).dropna()
    rows["r1"] = rows["p0944"] / rows["prev"] - 1
    rows["r12"] = rows["p1459"] / rows["p1429"] - 1
    rows["r13"] = rows["p1529"] / rows["p1459"] - 1
    n = len(rows)
    p(f"NIFTYBEES, {rows.index[0]} .. {rows.index[-1]}, {n} complete days")

    def ols(y, X):
        X = np.column_stack([np.ones(len(y))] + X)
        b, *_ = np.linalg.lstsq(X, y, rcond=None)
        e = y - X @ b
        XtX_inv = np.linalg.inv(X.T @ X)
        meat = (X * e[:, None]).T @ (X * e[:, None])
        se = np.sqrt(np.diag(XtX_inv @ meat @ XtX_inv))  # White
        r2 = 1 - (e @ e) / ((y - y.mean()) @ (y - y.mean()))
        return b, b / se, r2

    b, t, r2 = ols(rows["r13"].values, [rows["r1"].values])
    p(f"r_last = a + b*r_first:          b {b[1]:+.4f} (t {t[1]:+.2f}), R2 {r2*100:.2f}%")
    b, t, r2 = ols(rows["r13"].values, [rows["r1"].values, rows["r12"].values])
    p(f"r_last = a + b1*r_first + b2*r_12: b1 {b[1]:+.4f} (t {t[1]:+.2f}), b2 {b[2]:+.4f} (t {t[2]:+.2f}), R2 {r2*100:.2f}%")
    cost = 0.0002  # round trip on Nifty futures: STT, exchange, brokerage, a tick of spread
    for label, mask in [("all days", rows.index == rows.index),
                        ("|r_first| top third", rows["r1"].abs() >= rows["r1"].abs().quantile(2 / 3))]:
        g = rows[mask]
        pnl = np.sign(g["r1"]) * g["r13"]
        net = pnl - cost
        p(f"Trade sign(r_first) 15:00->15:30, {label:<20}: {len(g)} days, mean {pnl.mean()*1e4:+.2f} bps gross,"
          f" {net.mean()*1e4:+.2f} net, hit {100*(pnl>0).mean():.0f}%, net t {net.mean()/net.std()*np.sqrt(len(net)):+.2f}")
    rows.index = pd.to_datetime(rows.index)
    yr = (np.sign(rows["r1"]) * rows["r13"]).groupby(rows.index.year).agg(["count", "mean"])
    p("   by year (days, mean gross bps):  " + "  ".join(f"{y}:{int(r['count'])}/{r['mean']*1e4:+.1f}" for y, r in yr.iterrows()))
    p("")


if __name__ == "__main__":
    check_a()
    check_b()
    check_c()
    path = os.path.join(ROOT, "research", "reports", "2026-10-20_nifty_quick_checks.txt")
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")
    print(f"\nwritten {path}", file=sys.stderr)
