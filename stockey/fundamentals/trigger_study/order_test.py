"""Large order wins -- LEDGER row 84, pre-registered in systrade/research/preregistrations/
2026-11-02_order_wins.md. Read it before changing anything: a change to the rule is a new test.

Events: order wins classified by `orders.classify` with a stated value >= 10% of the trailing
four quarters' sales published before the announcement; the first per company per 60 sessions.
s0 = the last session whose close is before the announcement; entry at the close of s0+1;
outcome = stock minus control over 20 / 40 / 60 (primary) sessions.
  A: control = the stock's industry index (row 58's).
  B: Stage 2 at s0 only; control = equal-weight return of the same industry's liquid stocks that
     are also in Stage 2 at s0 (>= 3, else sector, else market), held fixed from entry.
Mean outcome after 50 bps, one-sided p by a month-cluster bootstrap, BH over A and B.

    python -m fundamentals.trigger_study.order_test in-sample
    python -m fundamentals.trigger_study.order_test check      # once, only for a passing member
"""
from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

from fundamentals.trigger_study.drift_test import (COST, DAILY_CAP, LIQ_MIN_RS, LIQ_WINDOW, MARKET_CLOSE_HOUR,
                                                    MARKET_CLOSE_MIN, REPS, SEED, industry_levels, load_daily)
from fundamentals.trigger_study.events import EXCLUDED_SECTORS, load_membership
from fundamentals.trigger_study.nse_results import load as load_results
from fundamentals.trigger_study.orders import CLASSIFIED_TABLE, CLASSIFY_VERSION
from utils.db import sql_to_df

SIZE_MIN = 0.10
DEDUP_SESSIONS = 60
HORIZONS = (20, 40, 60)
MIN_NET_GAP, Q_MAX = 0.02, 0.10
IN_SAMPLE = (pd.Timestamp("2018-07-01"), pd.Timestamp("2022-12-31"))
CHECK = (pd.Timestamp("2023-01-01"), pd.Timestamp("2025-12-31"))
PRICE = {"in-sample": ("2017-01-01", "2023-04-30"), "check": ("2021-06-01", "2026-04-30")}
STAGE_MA_WEEKS, STAGE_SLOPE_WEEKS, STAGE_FLAT_PCT = 30, 4, 1.0
MIN_PEERS = 3


# --- inputs ---------------------------------------------------------------------------------

def load_orders(window: tuple) -> pd.DataFrame:
    df = sql_to_df(f"""SELECT symbol, announced_at, value_cr FROM {CLASSIFIED_TABLE}
                        WHERE classify_version = %(v)s AND order_win AND value_cr IS NOT NULL
                          AND announced_at >= %(a)s AND announced_at < %(b)s""",  # noqa: S608
                   params={"v": CLASSIFY_VERSION, "a": window[0].to_pydatetime(),
                           "b": (window[1] + pd.Timedelta(days=1)).to_pydatetime()})
    df["announced_at"] = pd.to_datetime(df["announced_at"])
    return df.sort_values(["symbol", "announced_at"])


def ttm_sales(res: pd.DataFrame, at: pd.Timestamp) -> float:
    """Sum of the latest four quarters published before `at` (one basis, quarter ends within a
    year of each other); NaN otherwise."""
    known = res[(res["public_at"] < at) & res["sales"].notna()].sort_values("period_end")
    if known.empty:
        return np.nan
    basis = known["consolidated"].iloc[-1]
    q = known[known["consolidated"] == basis].drop_duplicates("period_end", keep="last").tail(4)
    if len(q) < 4 or (q["period_end"].iloc[-1] - q["period_end"].iloc[0]).days > 300:
        return np.nan
    return float(q["sales"].sum())


def stage2(close: pd.DataFrame) -> pd.DataFrame:
    """Stage 2 (systrader internal/stage) per stock per day, judged on the last COMPLETED week:
    weekly close above its 30-week average and the average up more than 1% over 4 weeks."""
    wk = close.resample("W-FRI").last()
    ma = wk.rolling(STAGE_MA_WEEKS, min_periods=STAGE_MA_WEEKS).mean()
    slope = (ma / ma.shift(STAGE_SLOPE_WEEKS) - 1) * 100
    s2w = (wk > ma) & (slope > STAGE_FLAT_PCT)
    # a day sees the stage of the last week that ENDED on or before it
    return s2w.reindex(close.index, method="ffill").fillna(False).astype(bool)


# --- events ---------------------------------------------------------------------------------

def build_events(window: tuple, close: pd.DataFrame, tv: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    memb = load_membership()
    memb = memb[memb["sector_code"].notna() & ~memb["sector_code"].isin(EXCLUDED_SECTORS)]
    levels, liq = industry_levels(close, tv, memb)
    ret = close.pct_change(fill_method=None).clip(-DAILY_CAP, DAILY_CAP)
    s2 = stage2(close)
    orders = load_orders(window)
    res = load_results(sorted(set(orders["symbol"])))
    res_by = {s: g for s, g in res.groupby("symbol")}
    days = close.index
    excluded: dict[str, int] = {}

    def skip(w):
        excluded[w] = excluded.get(w, 0) + 1

    rows, last_event = [], {}
    for o in orders.itertuples(index=False):
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
        if i0 < 0 or ie + max(HORIZONS) >= len(days):
            skip("outside_price_window")
            continue
        sales = ttm_sales(res_by.get(sym, pd.DataFrame(columns=res.columns)), at)
        if not sales > 0:
            skip("no_trailing_revenue")
            continue
        size = o.value_cr / sales
        if size < SIZE_MIN:
            skip("small_order")
            continue
        if sym in last_event and i0 - last_event[sym] < DEDUP_SESSIONS:
            skip("within_60_sessions_of_an_earlier_event")
            continue
        if not (pd.notna(liq[sym].iat[i0]) and liq[sym].iat[i0] >= LIQ_MIN_RS):
            skip("illiquid")
            continue
        s = close[sym]
        if pd.isna(s.iat[i0]) or pd.isna(s.iat[ie]):
            skip("no_price_at_event")
            continue
        last_event[sym] = i0
        in_s2 = bool(s2[sym].iat[i0])
        # B's control: same-industry liquid Stage 2 peers at s0 (>= 3), else sector, else market
        elig = (liq.iloc[i0] >= LIQ_MIN_RS) & s2.iloc[i0]
        peers = []
        for grp in (memb.index[memb["industry_code"] == memb.at[sym, "industry_code"]],
                    memb.index[memb["sector_code"] == memb.at[sym, "sector_code"]], memb.index):
            peers = [p for p in grp if p != sym and p in elig.index and bool(elig.get(p, False))]
            if len(peers) >= MIN_PEERS:
                break
        row = {"symbol": sym, "announced_at": at, "s0": days[i0], "entry": days[ie], "value_cr": o.value_cr,
               "ttm_sales_cr": sales, "size": size, "stage2": in_s2, "n_peers": len(peers)}
        for h in HORIZONS:
            j = ie + h
            stock = s.iat[j] / s.iat[ie] - 1 if pd.notna(s.iat[j]) else np.nan
            ind = lvl.iat[j] / lvl.iat[ie] - 1
            row[f"A{h}"] = (1 + stock) / (1 + ind) - 1 if pd.notna(stock) else np.nan
            if in_s2 and peers:
                pr = (1 + ret[peers].iloc[ie + 1:j + 1].fillna(0.0)).prod() - 1
                row[f"B{h}"] = (1 + stock) / (1 + float(pr.mean())) - 1 if pd.notna(stock) else np.nan
            else:
                row[f"B{h}"] = np.nan
        rows.append(row)
    return pd.DataFrame(rows), excluded


# --- statistics -----------------------------------------------------------------------------

def mean_p(d: pd.DataFrame, col: str) -> tuple[float, float, int]:
    """Net mean outcome (after COST) and one-sided p that it is <= 0, bootstrap over entry months."""
    x = d[d[col].notna()].copy()
    if x.empty:
        return np.nan, np.nan, 0
    x["m"] = x["entry"].dt.to_period("M")
    g = x.groupby("m")[col].agg(["sum", "count"])
    s, n = g["sum"].to_numpy(), g["count"].to_numpy()
    obs = s.sum() / n.sum() - COST
    rng = np.random.default_rng(SEED)
    idx = rng.integers(0, len(g), size=(REPS, len(g)))
    w = np.stack([(idx == k).sum(axis=1) for k in range(len(g))], axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        boot = (w @ s) / (w @ n) - COST
    boot = boot[np.isfinite(boot)]
    return float(obs), float((boot <= 0).mean()), int(len(x))


def evaluate(ev: pd.DataFrame, check: bool = False) -> dict:
    out = {"events": int(len(ev)), "members": {}}
    for m in ("A", "B"):
        r = {}
        for h in HORIZONS:
            gap, p, n = mean_p(ev, f"{m}{h}")
            r[f"{h}d"] = {"net_mean": None if pd.isna(gap) else round(gap, 4), "p": None if pd.isna(p) else round(p, 4), "n": n}
        x = ev[ev[f"{m}60"].notna()]
        r["median60"] = round(float(x[f"{m}60"].median()), 4) if len(x) else None
        r["share_beating60"] = round(float((x[f"{m}60"] > 0).mean()), 3) if len(x) else None
        r["by_year_net60"] = {str(y): round(float(g[f"{m}60"].mean() - COST), 4) for y, g in x.groupby(x["entry"].dt.year)}
        r["by_year_n"] = {str(y): int(len(g)) for y, g in x.groupby(x["entry"].dt.year)}
        out["members"][m] = r
    ps = sorted(((out["members"][m]["60d"]["p"] if out["members"][m]["60d"]["p"] is not None else 1.0), m) for m in ("A", "B"))
    prev = 1.0
    for k in range(len(ps), 0, -1):
        p, m = ps[k - 1]
        prev = min(prev, p * len(ps) / k)
        out["members"][m]["q60"] = round(prev, 4)
    for m in ("A", "B"):
        r = out["members"][m]
        g, p = r["60d"]["net_mean"], r["60d"]["p"]
        pos = sum(v > 0 for v in r["by_year_net60"].values())
        if check:
            r["pass"] = bool(g is not None and g > 0 and p is not None and p < 0.10 and pos >= 2)
        else:
            r["pass"] = bool(g is not None and g >= MIN_NET_GAP and r["q60"] < Q_MAX and pos >= 3)
    return out


def descriptive_sizes(ev: pd.DataFrame) -> dict:
    """Reported, not judged (pre-registration section 4): A's 60-session net mean by size band."""
    out = {}
    for lo, hi in ((0.10, 0.25), (0.25, np.inf)):
        x = ev[(ev["size"] >= lo) & (ev["size"] < hi) & ev["A60"].notna()]
        out[f"{lo:.0%}-{'' if hi == np.inf else f'{hi:.0%}'}"] = {"n": int(len(x)),
                                                               "net_mean60": round(float(x["A60"].mean() - COST), 4) if len(x) else None}
    return out


def run(which: str) -> dict:
    window = IN_SAMPLE if which == "in-sample" else CHECK
    close, tv = load_daily(*PRICE[which])
    ev, excluded = build_events(window, close, tv)
    out = {"run": which, "window": [str(window[0].date()), str(window[1].date())], "excluded": excluded,
           **evaluate(ev, check=(which == "check")), "sizes_descriptive": descriptive_sizes(ev) if len(ev) else {},
           "stage2_events": int(ev["stage2"].sum()) if len(ev) else 0}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LEDGER row 84: large order wins.")
    ap.add_argument("which", choices=["in-sample", "check"])
    a = ap.parse_args(argv)
    print(json.dumps(run(a.which), indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
