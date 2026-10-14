"""Results confirmation test -- LEDGER row 57, pre-registered in
systrade/research/preregistrations/2026-10-12_results_confirmation.md (read it before
changing anything here; a change to the rule is a new test).

Every momentum entry; the first NSE result published within 120 days after it; confirmed =
EBITDA margin and net profit both above the year-ago quarter; outcome = stock vs its industry
over 13 (primary) and 26 weeks from the first weekly close after publication.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from utils.db import sql_to_df

from fundamentals.trigger_study import events as ev_mod
from fundamentals.trigger_study import nse_results
from fundamentals.trigger_study.events import EVENTS_TABLE

RESULT_WINDOW_DAYS = 120
HORIZONS = (13, 26)
MIN_GAP = 0.03
ALPHA = 0.05
REPS = 10_000
SEED = 1


def build_rows(pn: dict | None = None, *, last_start: str = "2022-12-31", first_start: str | None = None,
               samples: list[str] | None = None) -> tuple[pd.DataFrame, dict]:
    """`samples`: which events (None = every event computed for discovery, i.e. not 'check')."""
    pn = pn or ev_mod.panel()
    close, ilvl = pn["close"], pn["industry_lvl"]
    weeks = close.index
    if samples:
        events = sql_to_df(f"SELECT event_id, symbol, industry_code, entry_week, label FROM {EVENTS_TABLE} "
                           "WHERE sample = ANY(%s)", params=(samples,))
    else:
        events = sql_to_df(f"SELECT event_id, symbol, industry_code, entry_week, label FROM {EVENTS_TABLE} "
                           "WHERE sample IS DISTINCT FROM 'check'")
    events["entry_week"] = pd.to_datetime(events["entry_week"])
    res = nse_results.load(sorted(events["symbol"].unique()))
    excluded: dict[str, int] = {}

    def skip(why):
        excluded[why] = excluded.get(why, 0) + 1

    rows = []
    for e in events.itertuples():
        r = res[res["symbol"] == e.symbol]
        decided = e.entry_week + pd.Timedelta(days=4)           # the entry week's last session
        after = r[(r["public_at"] > decided + pd.Timedelta(days=1))
                  & (r["public_at"] <= e.entry_week + pd.Timedelta(days=RESULT_WINDOW_DAYS))].sort_values("public_at")
        if after.empty:
            skip("no_result_within_120d")
            continue
        q = after.iloc[0]
        prior = r[(r["period_end"] - q["period_end"]).dt.days.abs().sub(365).abs() <= 20]
        prior = prior[(prior["period_end"] < q["period_end"]) & (prior["consolidated"] == q["consolidated"])]
        if prior.empty:
            skip("no_year_ago_quarter")
            continue
        p = prior.iloc[-1]
        if any(pd.isna(v) for v in (q["opm_pct"], p["opm_pct"], q["net_profit"], p["net_profit"])):
            skip("missing_fields")
            continue
        confirmed = bool(q["opm_pct"] > p["opm_pct"] and q["net_profit"] > p["net_profit"])
        pub = pd.Timestamp(q["public_at"]).normalize()
        wk = pub - pd.Timedelta(days=pub.weekday())
        if pub.weekday() > 3:                                   # Friday-Sunday: next week's close
            wk += pd.Timedelta(days=7)
        i = weeks.searchsorted(wk)
        if i >= len(weeks) or weeks[i] != wk:
            skip("start_week_not_in_prices")
            continue
        if e.symbol not in close.columns or e.industry_code not in ilvl:
            skip("no_price_or_industry")
            continue
        s, lv = close[e.symbol], ilvl[e.industry_code]
        out = {"event_id": e.event_id, "symbol": e.symbol, "label": e.label, "start": weeks[i],
               "period_end": q["period_end"], "confirmed": confirmed,
               "opm_change_pp": q["opm_pct"] - p["opm_pct"],
               "profit_up": bool(q["net_profit"] > p["net_profit"])}
        for h in HORIZONS:
            if i + h < len(weeks) and pd.notna(s.iat[i]) and pd.notna(s.iat[i + h]):
                out[f"ex{h}"] = float((s.iat[i + h] / s.iat[i]) / (lv.iat[i + h] / lv.iat[i]) - 1)
            else:
                out[f"ex{h}"] = np.nan
        rows.append(out)
    df = pd.DataFrame(rows)
    if len(df):
        df = df[df["start"] <= pd.Timestamp(last_start)]
        if first_start:
            df = df[df["start"] >= pd.Timestamp(first_start)]
    return df, excluded


def cluster_bootstrap_p(df: pd.DataFrame, col: str, reps: int = REPS, seed: int = SEED) -> tuple[float, float]:
    """Observed confirmed-minus-not mean difference and one-sided p (share of month-cluster
    bootstrap draws with a difference <= 0)."""
    d = df.dropna(subset=[col]).copy()
    d["month"] = d["start"].dt.to_period("M")
    g = d.groupby(["month", "confirmed"])[col].agg(["sum", "count"]).unstack(fill_value=0)
    months = g.index.to_numpy()
    sc, nc = g[("sum", True)].to_numpy(), g[("count", True)].to_numpy()
    sn, nn = g[("sum", False)].to_numpy(), g[("count", False)].to_numpy()
    obs = sc.sum() / nc.sum() - sn.sum() / nn.sum()
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(months), size=(reps, len(months)))
    w = np.zeros((reps, len(months)))
    for k in range(len(months)):
        w[:, k] = (idx == k).sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        boot = (w @ sc) / (w @ nc) - (w @ sn) / (w @ nn)
    boot = boot[np.isfinite(boot)]
    return float(obs), float((boot <= 0).mean())


def summarise(df: pd.DataFrame, excluded: dict) -> dict:
    out = {"entries": int(len(df)), "confirmed": int(df["confirmed"].sum()), "excluded": excluded, "horizons": {}}
    for h in HORIZONS:
        col = f"ex{h}"
        d = df.dropna(subset=[col])
        obs, p = cluster_bootstrap_p(d, col)
        grp = {k: {"n": int(len(x)), "mean": round(float(x[col].mean()), 4), "median": round(float(x[col].median()), 4),
                   "beat_industry": round(float((x[col] > 0).mean()), 3)}
               for k, x in d.groupby(d["confirmed"].map({True: "confirmed", False: "not_confirmed"}))}
        out["horizons"][f"{h}w"] = {"difference": round(obs, 4), "p_one_sided": round(p, 4), **grp,
                                    "all_entries_mean": round(float(d[col].mean()), 4)}
    h = out["horizons"]["13w"]
    out["pass"] = bool(h["difference"] >= MIN_GAP and h["p_one_sided"] < ALPHA)
    d = df.dropna(subset=["ex13"])
    out["by_year"] = {str(y): {"n": int(len(x)), "diff_13w": round(float(x.loc[x.confirmed, "ex13"].mean()
                                                                       - x.loc[~x.confirmed, "ex13"].mean()), 4)}
                      for y, x in d.groupby(d["start"].dt.year)}
    return out
