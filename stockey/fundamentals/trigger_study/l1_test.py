"""Lowest-bidder (L1) announcements as a short trade -- LEDGER row 89, pre-registered in
systrade/research/preregistrations/2026-11-07_l1_bids.md. Read it before changing anything: a
change to the rule is a new test.

Events: L1 announcements classified by `l1_bids.classify` with a stated value >= 5% of the
trailing four quarters' sales published before the announcement; the first per company per 15
sessions; liquidity as row 84. s0 = the last session whose close is before the announcement;
entry at the close of s0+1; outcome = stock minus its industry index over 5 / 10 / 15 sessions,
one member per horizon. Mean after 50 bps, one-sided p by a month-cluster bootstrap, BH over
the three horizons.

    python -m fundamentals.trigger_study.l1_test in-sample
    python -m fundamentals.trigger_study.l1_test check      # once, only for passing horizons
"""
from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd
from sqlalchemy import text

from fundamentals.trigger_study.drift_test import COST, LIQ_MIN_RS, MARKET_CLOSE_HOUR, MARKET_CLOSE_MIN, industry_levels, load_daily
from fundamentals.trigger_study.events import EXCLUDED_SECTORS, _systrader_conn, load_membership
from fundamentals.trigger_study.l1_bids import CLASSIFIED_TABLE, CLASSIFY_VERSION
from fundamentals.trigger_study.nse_results import load as load_results
from fundamentals.trigger_study.order_test import CHECK, IN_SAMPLE, PRICE, mean_p, ttm_sales
from fundamentals.trigger_study.orders import CLASSIFIED_TABLE as ORDER_TABLE
from fundamentals.trigger_study.orders import CLASSIFY_VERSION as ORDER_VERSION
from utils.db import sql_to_df

SIZE_MIN = 0.05
DEDUP_SESSIONS = 15
HORIZONS = (5, 10, 15)
MIN_NET_GAP, Q_MAX = 0.02, 0.10
AWARD_DAYS = 90
INTRADAY_FROM = pd.Timestamp("2021-08-20")


def load_l1(window: tuple) -> pd.DataFrame:
    df = sql_to_df(f"""SELECT symbol, announced_at, value_cr FROM {CLASSIFIED_TABLE}
                        WHERE classify_version = %(v)s AND l1 AND value_cr IS NOT NULL
                          AND announced_at >= %(a)s AND announced_at < %(b)s""",  # noqa: S608
                   params={"v": CLASSIFY_VERSION, "a": window[0].to_pydatetime(),
                           "b": (window[1] + pd.Timedelta(days=1)).to_pydatetime()})
    df["announced_at"] = pd.to_datetime(df["announced_at"])
    return df.sort_values(["symbol", "announced_at"])


def load_awards(window: tuple) -> pd.DataFrame:
    """Row 84's order wins in the window (+ AWARD_DAYS), for the descriptive 'award followed' share."""
    df = sql_to_df(f"""SELECT symbol, announced_at FROM {ORDER_TABLE}
                        WHERE classify_version = %(v)s AND order_win
                          AND announced_at >= %(a)s AND announced_at < %(b)s""",  # noqa: S608
                   params={"v": ORDER_VERSION, "a": window[0].to_pydatetime(),
                           "b": (window[1] + pd.Timedelta(days=AWARD_DAYS + 1)).to_pydatetime()})
    df["announced_at"] = pd.to_datetime(df["announced_at"])
    return df


def load_open(price_from: str, price_to: str, index: pd.DatetimeIndex) -> pd.DataFrame:
    sql = """SELECT symbol, date::date AS d, tr_adj_open AS o FROM advisory_adjusted_ohlcv_daily
              WHERE date >= :a AND date <= :b AND series = 'EQ' AND tr_adj_close > 0"""
    with _systrader_conn() as conn:
        df = pd.read_sql(text(sql), conn, params={"a": price_from, "b": price_to})
    df["d"] = pd.to_datetime(df["d"])
    return df.pivot_table(index="d", columns="symbol", values="o", aggfunc="last").reindex(index)


def minute_after(symbol: str, at: pd.Timestamp) -> tuple[float, float] | None:
    """(close of the first 1-min bar starting a minute after the filing, that day's last close); IST naive `at`."""
    a = at.tz_localize("Asia/Kolkata") + pd.Timedelta(minutes=1)
    b = a.normalize() + pd.Timedelta(hours=15, minutes=30)
    with _systrader_conn() as conn:
        x = pd.read_sql(text("""SELECT close FROM dhan_ohlcv_intraday
                                 WHERE ticker = :s AND exchange = 'NSE' AND interval_minutes = 1
                                   AND timestamp >= :a AND timestamp < :b ORDER BY timestamp"""),
                        conn, params={"s": symbol, "a": a.to_pydatetime(), "b": b.to_pydatetime()})
    return (float(x["close"].iat[0]), float(x["close"].iat[-1])) if len(x) >= 5 else None


def build_events(window: tuple, close: pd.DataFrame, tv: pd.DataFrame, opn: pd.DataFrame | None = None,
                 intraday: bool = True) -> tuple[pd.DataFrame, dict]:
    memb = load_membership()
    memb = memb[memb["sector_code"].notna() & ~memb["sector_code"].isin(EXCLUDED_SECTORS)]
    levels, liq = industry_levels(close, tv, memb)
    l1 = load_l1(window)
    res = load_results(sorted(set(l1["symbol"])))
    res_by = {s: g for s, g in res.groupby("symbol")}
    awards = load_awards(window)
    award_by = {s: g["announced_at"].sort_values().to_numpy() for s, g in awards.groupby("symbol")}
    days = close.index
    excluded: dict[str, int] = {}

    def skip(w):
        excluded[w] = excluded.get(w, 0) + 1

    rows, last_event = [], {}
    for o in l1.itertuples(index=False):
        sym = o.symbol
        if sym not in memb.index:
            skip("excluded_sector_or_no_industry")
            continue
        if sym not in close.columns:
            skip("no_prices")
            continue
        lvl = levels.get(memb.at[sym, "industry_code"])
        if lvl is None:
            skip("no_industry")
            continue
        at = pd.Timestamp(o.announced_at)
        cut = at.normalize() + pd.Timedelta(hours=MARKET_CLOSE_HOUR, minutes=MARKET_CLOSE_MIN)
        i0 = (days.searchsorted(at.normalize(), side="right") - 1 if at > cut
              else days.searchsorted(at.normalize(), side="left") - 1)
        ie = i0 + 1
        if i0 < 20 or ie + max(HORIZONS) + 1 >= len(days):
            skip("outside_price_window")
            continue
        sales = ttm_sales(res_by.get(sym, pd.DataFrame(columns=res.columns)), at)
        if not sales > 0:
            skip("no_trailing_revenue")
            continue
        size = o.value_cr / sales
        if size < SIZE_MIN:
            skip("small_bid")
            continue
        if sym in last_event and i0 - last_event[sym] < DEDUP_SESSIONS:
            skip(f"within_{DEDUP_SESSIONS}_sessions_of_an_earlier_event")
            continue
        if not (pd.notna(liq[sym].iat[i0]) and liq[sym].iat[i0] >= LIQ_MIN_RS):
            skip("illiquid")
            continue
        s = close[sym]
        if pd.isna(s.iat[i0]) or pd.isna(s.iat[ie]):
            skip("no_price_at_event")
            continue
        last_event[sym] = i0
        aw = award_by.get(sym)
        followed = bool(aw is not None and ((aw > np.datetime64(at)) & (aw <= np.datetime64(at + pd.Timedelta(days=AWARD_DAYS)))).any())
        row = {"symbol": sym, "announced_at": at, "s0": days[i0], "entry": days[ie], "value_cr": o.value_cr,
               "ttm_sales_cr": sales, "size": size, "award_followed": followed,
               "day_move": s.iat[ie] / s.iat[i0] - 1,
               "pre20_vs_ind": (s.iat[i0] / s.iat[i0 - 20]) / (lvl.iat[i0] / lvl.iat[i0 - 20]) - 1}
        in_hours = at.normalize() == days[ie] and (at.hour, at.minute) >= (9, 15) and at <= cut
        m = minute_after(sym, at) if intraday and in_hours and days[ie] >= INTRADAY_FROM else None
        for h in HORIZONS:
            j = ie + h
            stock = s.iat[j] / s.iat[ie] - 1 if pd.notna(s.iat[j]) else np.nan
            ind = lvl.iat[j] / lvl.iat[ie] - 1
            row[f"L{h}"] = (1 + stock) / (1 + ind) - 1 if pd.notna(stock) else np.nan
            if opn is not None and pd.notna(opn[sym].iat[ie + 1]) and pd.notna(s.iat[j]):
                row[f"open{h}"] = (s.iat[j] / opn[sym].iat[ie + 1]) / (1 + ind) - 1
            if m and pd.notna(row[f"L{h}"]):
                row[f"minute{h}"] = (m[1] / m[0]) * (1 + row[f"L{h}"]) - 1
        rows.append(row)
    return pd.DataFrame(rows), excluded


def evaluate(ev: pd.DataFrame, check: bool = False) -> dict:
    out = {"events": int(len(ev)), "members": {}}
    for h in HORIZONS:
        col = f"L{h}"
        gap, p, n = mean_p(ev, col) if len(ev) else (np.nan, np.nan, 0)
        x = ev[ev[col].notna()] if len(ev) else ev
        out["members"][f"L1-{h}"] = {
            "net_mean": None if pd.isna(gap) else round(gap, 4), "p": None if pd.isna(p) else round(p, 4), "n": n,
            "median": round(float(x[col].median()), 4) if len(x) else None,
            "share_beating": round(float((x[col] > 0).mean()), 3) if len(x) else None,
            "by_year_net": {str(y): round(float(g[col].mean() - COST), 4) for y, g in x.groupby(x["entry"].dt.year)} if len(x) else {},
            "by_year_n": {str(y): int(len(g)) for y, g in x.groupby(x["entry"].dt.year)} if len(x) else {}}
    ms = list(out["members"])
    ps = sorted(((out["members"][m]["p"] if out["members"][m]["p"] is not None else 1.0), m) for m in ms)
    prev = 1.0
    for k in range(len(ps), 0, -1):
        p, m = ps[k - 1]
        prev = min(prev, p * len(ps) / k)
        out["members"][m]["q"] = round(prev, 4)
    for m in ms:
        r = out["members"][m]
        g, p = r["net_mean"], r["p"]
        pos = sum(v > 0 for v in r["by_year_net"].values())
        r["pass"] = bool(g is not None and g > 0 and p is not None and p < 0.10 and pos >= 2) if check \
            else bool(g is not None and g >= MIN_NET_GAP and r["q"] < Q_MAX and pos >= 3)
    return out


def descriptive(ev: pd.DataFrame) -> dict:
    """Reported, not judged (pre-registration section 4)."""
    if ev.empty:
        return {}
    def net(col, d=ev):
        x = d[col].dropna() if col in d else pd.Series(dtype=float)
        return {"n": int(len(x)), "net_mean": round(float(x.mean() - COST), 4) if len(x) else None}
    return {"day_move_mean": round(float(ev["day_move"].mean()), 4), "pre20_vs_ind_mean": round(float(ev["pre20_vs_ind"].mean()), 4),
            "award_followed_share": round(float(ev["award_followed"].mean()), 3),
            "next_open_entry": {h: net(f"open{h}") for h in HORIZONS},
            "minute_after_filing_entry": {h: net(f"minute{h}") for h in HORIZONS},
            "size>=10%": {h: net(f"L{h}", ev[ev["size"] >= 0.10]) for h in HORIZONS}}


def run(which: str) -> dict:
    window = IN_SAMPLE if which == "in-sample" else CHECK
    close, tv = load_daily(*PRICE[which])
    opn = load_open(*PRICE[which], close.index)
    ev, excluded = build_events(window, close, tv, opn)
    return {"run": which, "window": [str(window[0].date()), str(window[1].date())], "excluded": excluded,
            **evaluate(ev, check=(which == "check")), "descriptive": descriptive(ev)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LEDGER row 89: lowest-bidder announcements.")
    ap.add_argument("which", choices=["in-sample", "check"])
    a = ap.parse_args(argv)
    print(json.dumps(run(a.which), indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
