"""Post-results drift test -- LEDGER row 58, pre-registered in
systrade/research/preregistrations/2026-10-15_results_drift.md. Read it before changing
anything: a change to the rule is a new test.

Every liquid non-financial NSE result with an exact publish time; s0 = the last session whose
close (15:30 IST) is before the publish time; AR = stock vs industry from s0 to s0+2; entry at
the close of s0+2; outcome = stock vs industry over 20 / 40 / 60 (primary) sessions.
Excellent = top tenth by A (profit + sales growth rank), B (AR), or C (top fifth on both),
ranked within the quarter (same period end). Excellent minus the rest, after 50 bps.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sqlalchemy import text

from fundamentals.trigger_study.events import EXCLUDED_SECTORS, _systrader_conn, load_membership
from fundamentals.trigger_study.nse_results import load as load_results

PRICE_FROM, PRICE_TO = "2019-01-01", "2023-09-30"   # in-sample outcomes end ~2023-04; nothing later read
CHECK_PRICE_TO = "2025-09-30"                        # the check's last entries (Mar 2025) + 60 sessions
IN_SAMPLE = (pd.Timestamp("2020-10-01"), pd.Timestamp("2022-12-31"))
CHECK = (pd.Timestamp("2023-01-01"), pd.Timestamp("2025-03-31"))
LIQ_MIN_RS, LIQ_WINDOW = 5e7, 60
DAILY_CAP = 0.20
AR_SESSIONS = 2
HORIZONS = (20, 40, 60)
COST = 0.005
MIN_NET_GAP, Q_MAX = 0.02, 0.10
REPS, SEED = 10_000, 1
MARKET_CLOSE_HOUR, MARKET_CLOSE_MIN = 15, 30


def load_daily(price_from: str = PRICE_FROM, price_to: str = PRICE_TO) -> tuple[pd.DataFrame, pd.DataFrame]:
    sql = """SELECT symbol, date::date AS d, tr_adj_close AS c, adj_close * adj_volume AS tv
               FROM advisory_adjusted_ohlcv_daily
              WHERE date >= :a AND date <= :b AND series = 'EQ' AND tr_adj_close > 0"""
    with _systrader_conn() as conn:
        df = pd.read_sql(text(sql), conn, params={"a": price_from, "b": price_to})
    df["d"] = pd.to_datetime(df["d"])
    close = df.pivot_table(index="d", columns="symbol", values="c", aggfunc="last").sort_index()
    tv = df.pivot_table(index="d", columns="symbol", values="tv", aggfunc="last").sort_index()
    return close, tv


def industry_levels(close: pd.DataFrame, tv: pd.DataFrame, memb: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    """Equal-weight daily index per industry (>= 5 eligible members, else sector, else market),
    daily returns capped at +-20%; eligibility = 60-session median traded value >= Rs 5 cr
    as of the previous session."""
    ret = close.pct_change(fill_method=None).clip(-DAILY_CAP, DAILY_CAP)
    liq = tv.rolling(LIQ_WINDOW, min_periods=40).median()
    elig = liq.shift(1) >= LIQ_MIN_RS
    r = ret.where(elig)
    mkt = r.mean(axis=1).fillna(0.0)

    def grp(members, fb):
        cols = [c for c in members if c in r.columns]
        if not cols:
            return fb
        x = r[cols]
        return x.mean(axis=1).where(x.notna().sum(axis=1) >= 5, fb)

    sector = {s: grp(list(g.index), mkt).fillna(0.0) for s, g in memb.groupby("sector_code")}
    levels = {}
    for ind, g in memb.groupby("industry_code"):
        sec = g["sector_code"].mode().iloc[0] if g["sector_code"].notna().any() else None
        levels[ind] = (1 + grp(list(g.index), sector.get(sec, mkt)).fillna(0.0)).cumprod()
    return levels, liq


def build_rows(close: pd.DataFrame, tv: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    memb = load_membership()
    memb = memb[memb["sector_code"].notna() & ~memb["sector_code"].isin(EXCLUDED_SECTORS)]
    levels, liq = industry_levels(close, tv, memb)
    res = load_results(sorted(memb.index))
    days = close.index
    excluded: dict[str, int] = {}

    def skip(w):
        excluded[w] = excluded.get(w, 0) + 1

    rows = []
    for sym, r in res.groupby("symbol"):
        if sym not in close.columns:
            skip("no_prices")
            continue
        ind = memb.at[sym, "industry_code"]
        lvl = levels.get(ind)
        if lvl is None:
            skip("no_industry")
            continue
        r = r.sort_values("period_end")
        s = close[sym]
        for q in r.itertuples():
            pub = pd.Timestamp(q.public_at)
            if pd.isna(pub):
                skip("no_publish_time")
                continue
            # s0: the last session whose 15:30 close is before the publish time
            cut = pub.normalize() + pd.Timedelta(hours=MARKET_CLOSE_HOUR, minutes=MARKET_CLOSE_MIN)
            i0 = days.searchsorted(pub.normalize(), side="right") - 1 if pub > cut else days.searchsorted(pub.normalize(), side="left") - 1
            if i0 < 0 or i0 + AR_SESSIONS + max(HORIZONS) >= len(days):
                skip("outside_price_window")
                continue
            if not (liq[sym].iat[i0] >= LIQ_MIN_RS if pd.notna(liq[sym].iat[i0]) else False):
                skip("illiquid")
                continue
            ie = i0 + AR_SESSIONS
            if pd.isna(s.iat[i0]) or pd.isna(s.iat[ie]):
                skip("no_price_at_event")
                continue
            prior = r[(r["consolidated"] == q.consolidated) & (r["period_end"] < q.period_end)
                      & ((q.period_end - r["period_end"]).dt.days.sub(365).abs() <= 20)]
            sales_g = profit_g = np.nan
            if len(prior):
                p = prior.iloc[-1]
                if pd.notna(p["sales"]) and p["sales"] > 0 and pd.notna(q.sales):
                    sales_g = q.sales / p["sales"] - 1
                if pd.notna(p["net_profit"]) and pd.notna(q.net_profit):
                    base = max(5.0, 0.05 * (p["sales"] or 0))
                    if p["net_profit"] < 0 < q.net_profit:
                        profit_g = np.inf                          # loss-to-profit swing ranks at the top
                    elif p["net_profit"] >= base:
                        profit_g = q.net_profit / p["net_profit"] - 1
            ar = (s.iat[ie] / s.iat[i0]) / (lvl.iat[ie] / lvl.iat[i0]) - 1
            row = {"symbol": sym, "period_end": q.period_end, "public_at": pub, "s0": days[i0], "entry": days[ie],
                   "sales_g": sales_g, "profit_g": profit_g, "ar": ar}
            for h in HORIZONS:
                j = ie + h
                row[f"out{h}"] = (s.iat[j] / s.iat[ie]) / (lvl.iat[j] / lvl.iat[ie]) - 1 if pd.notna(s.iat[j]) else np.nan
            rows.append(row)
    return pd.DataFrame(rows), excluded


def label(df: pd.DataFrame) -> pd.DataFrame:
    """Ranks within each quarter (period end): A, B, C flags; NaN where a definition's inputs are missing."""
    out = []
    for _, g in df.groupby("period_end"):
        g = g.copy()
        a_ok = g["sales_g"].notna() & g["profit_g"].notna()
        pa = (g.loc[a_ok, "profit_g"].rank(pct=True) + g.loc[a_ok, "sales_g"].rank(pct=True)) / 2
        g["A_score"] = pa
        g["A"] = np.where(a_ok, g["A_score"].rank(pct=True) > 0.9, np.nan)
        g["B"] = g["ar"].rank(pct=True) > 0.9
        both = a_ok
        ra, rb = g.loc[both, "A_score"].rank(pct=True), g.loc[both, "ar"].rank(pct=True)
        g["C"] = pd.Series(np.nan, index=g.index, dtype=object)
        g.loc[both, "C"] = ((ra > 0.8) & (rb > 0.8)).astype(object)
        out.append(g)
    return pd.concat(out)


def cluster_p(d: pd.DataFrame, flag: str, col: str) -> tuple[float, float]:
    """Net difference (excellent - cost - rest) and one-sided p, bootstrap over entry months."""
    d = d[d[flag].notna() & d[col].notna()].copy()
    d["is"] = d[flag].astype(bool)
    d["m"] = d["entry"].dt.to_period("M")
    g = d.groupby(["m", "is"])[col].agg(["sum", "count"]).unstack(fill_value=0)
    se, ne = g[("sum", True)].to_numpy(), g[("count", True)].to_numpy()
    sr, nr = g[("sum", False)].to_numpy(), g[("count", False)].to_numpy()
    obs = se.sum() / ne.sum() - COST - sr.sum() / nr.sum()
    rng = np.random.default_rng(SEED)
    idx = rng.integers(0, len(g), size=(REPS, len(g)))
    w = np.stack([(idx == k).sum(axis=1) for k in range(len(g))], axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        boot = (w @ se) / (w @ ne) - COST - (w @ sr) / (w @ nr)
    boot = boot[np.isfinite(boot)]
    return float(obs), float((boot <= 0).mean())


def evaluate(df: pd.DataFrame, window: tuple) -> dict:
    d = df[(df["public_at"] >= window[0]) & (df["public_at"] <= window[1] + pd.Timedelta(days=1))]
    out = {"window": [str(window[0].date()), str(window[1].date())], "results": int(len(d)), "definitions": {}}
    for f in ("A", "B", "C"):
        res = {}
        for h in HORIZONS:
            gap, p = cluster_p(d, f, f"out{h}")
            res[f"{h}d"] = {"net_gap": round(gap, 4), "p": round(p, 4)}
        x = d[d[f].notna() & d["out60"].notna()]
        exc, rest = x[x[f].astype(bool)], x[~x[f].astype(bool)]
        res["n_excellent"], res["n_rest"] = int(len(exc)), int(len(rest))
        res["mean60_excellent"], res["mean60_rest"] = round(float(exc["out60"].mean()), 4), round(float(rest["out60"].mean()), 4)
        res["median60_excellent"], res["median60_rest"] = round(float(exc["out60"].median()), 4), round(float(rest["out60"].median()), 4)
        res["by_year_net_gap60"] = {str(y): round(float(g.loc[g[f].astype(bool), "out60"].mean() - COST
                                                        - g.loc[~g[f].astype(bool), "out60"].mean()), 4)
                                    for y, g in x.groupby(x["entry"].dt.year)}
        out["definitions"][f] = res
    # Benjamini-Hochberg over the three definitions, on the primary (60-session) p
    ps = sorted(((out["definitions"][f]["60d"]["p"], f) for f in ("A", "B", "C")))
    prev = 1.0
    for k in range(len(ps), 0, -1):
        p, f = ps[k - 1]
        prev = min(prev, p * len(ps) / k)
        out["definitions"][f]["q60"] = round(prev, 4)
    for f in ("A", "B", "C"):
        r = out["definitions"][f]
        years_pos = sum(v > 0 for v in r["by_year_net_gap60"].values())
        r["pass"] = bool(r["60d"]["net_gap"] >= MIN_NET_GAP and r["q60"] < Q_MAX and years_pos >= 2)
    return out
