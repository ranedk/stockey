"""Trigger study report: which triggers separate momentum entries that kept running from
those that failed, whether they came before or after the entry, and how the price moved
around each trigger.

Exploration only (LEDGER row 56, trials=0): with tens of events per group no difference here
is evidence. It decides which triggers go on the frozen list for the 2023+ check.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from utils.db import sql_to_df

from fundamentals.trigger_study import events as ev_mod
from fundamentals.trigger_study.events import EVENTS_TABLE, samples
from fundamentals.trigger_study.filings import FILINGS_TABLE
from fundamentals.trigger_study.llm_runner import TAGS_TABLE

RESULTS_LAG_DAYS = 60          # only when NSE gives no publish time
MIN_IMPORTANCE = 2


def load_events(sample: str) -> pd.DataFrame:
    # a stock NSE returned no filings for would read as "no triggers" -- leave it out instead
    df = sql_to_df(f"""SELECT * FROM {EVENTS_TABLE} e WHERE sample = ANY(%s) AND label IN ('continued','failed')
                          AND EXISTS (SELECT 1 FROM {FILINGS_TABLE} f WHERE f.symbol = e.symbol)""",
                   params=(samples(sample),))
    for c in ("entry_week", "window_start", "window_end"):
        df[c] = pd.to_datetime(df[c])
    return df


def load_tags(symbols: list[str]) -> pd.DataFrame:
    """One row per filing: the document read when there is one, else the subject-line tag."""
    df = sql_to_df(f"""
        SELECT DISTINCT ON (t.filing_id) t.filing_id, t.pass, t.trigger, t.direction, t.value_cr,
               t.importance, t.note, f.symbol, f.announced_at, f.nse_category, f.text
          FROM {TAGS_TABLE} t JOIN {FILINGS_TABLE} f USING (filing_id)
         WHERE f.symbol = ANY(%s) AND t.pass IN ('tag', 'document')
         ORDER BY t.filing_id, (t.pass = 'document') DESC
    """, params=(symbols,))
    df["announced_at"] = pd.to_datetime(df["announced_at"])
    return df


def load_results(symbols: list[str]) -> pd.DataFrame:
    """NSE XBRL quarters (from Sep 2019) with their real publish time; a missing publish time
    falls back to quarter end + RESULTS_LAG_DAYS."""
    from fundamentals.trigger_study import nse_results
    df = nse_results.load(symbols)
    df["public_at"] = df["public_at"].fillna(df["period_end"] + pd.Timedelta(days=RESULTS_LAG_DAYS))
    return df


def _yoy(cur: float, base: float, floor: float) -> float:
    if pd.isna(cur) or pd.isna(base) or abs(base) < floor:
        return np.nan
    return cur / abs(base) - 1 if base > 0 else np.nan


def results_signals(res: pd.DataFrame, symbol: str, as_of: pd.Timestamp, until: pd.Timestamp | None = None) -> dict:
    """The latest quarter public by `as_of` (or, with `until`, the first one made public in
    (as_of, until]) against the same quarter a year earlier."""
    r = res[res["symbol"] == symbol].set_index("period_end")
    public = pd.DatetimeIndex(r["public_at"])
    if until is None:
        cand = r.index[public <= as_of]
        q = cand.max() if len(cand) else None
    else:
        cand = r.index[(public > as_of) & (public <= until)]
        q = cand.min() if len(cand) else None
    if q is None:
        return {}
    prior = [p for p in r.index if abs((q - p).days - 365) <= 20]
    if not prior:
        return {}
    a, b = r.loc[q], r.loc[prior[0]]
    prev_q = [p for p in r.index if 80 <= (q - p).days <= 100]
    prev_prior = [p for p in r.index if prev_q and abs((prev_q[0] - p).days - 365) <= 20]
    sales_yoy = _yoy(a["sales"], b["sales"], 5)
    accel = np.nan
    if prev_q and prev_prior:
        accel = sales_yoy - _yoy(r.loc[prev_q[0], "sales"], r.loc[prev_prior[0], "sales"], 5)
    return {"sales_yoy": sales_yoy, "sales_accel": accel,
            "opm_change_pp": a["opm_pct"] - b["opm_pct"] if pd.notna(a["opm_pct"]) and pd.notna(b["opm_pct"]) else np.nan,
            "profit_yoy": _yoy(a["net_profit"], b["net_profit"], max(5.0, 0.05 * (b["sales"] or 0)))}


def _ttm_sales(res: pd.DataFrame, symbol: str, as_of: pd.Timestamp) -> float:
    r = res[(res["symbol"] == symbol) & (res["public_at"] <= as_of)]
    return float(r.tail(4)["sales"].sum()) if len(r) >= 4 else np.nan


def price_around(pn: dict, symbol: str, industry: str, when: pd.Timestamp) -> dict:
    """Stock vs its industry: 4 weeks before the filing's week, and 13 weeks after it."""
    close, lvl = pn["close"], pn["industry_lvl"].get(industry)
    if symbol not in close.columns or lvl is None:
        return {}
    weeks = close.index
    i = weeks.searchsorted(when.normalize() - pd.Timedelta(days=when.weekday()))
    if i < 4 or i + 13 >= len(weeks):
        return {}
    s, l = close[symbol], lvl

    def rel(a, b):
        return float((s.iat[b] / s.iat[a]) / (l.iat[b] / l.iat[a]) - 1) if pd.notna(s.iat[a]) and pd.notna(s.iat[b]) else np.nan

    return {"move_4w_before": rel(i - 4, i), "move_13w_after": rel(i, i + 13)}


def build(sample: str = "pilot") -> dict:
    events = load_events(sample)
    syms = sorted(events["symbol"].unique())
    tags = load_tags(syms)
    res = load_results(syms)
    pn = ev_mod.panel()

    ev_rows, trig_rows = [], []
    for e in events.itertuples():
        t = tags[(tags["symbol"] == e.symbol) & (tags["announced_at"] >= e.window_start)
                 & (tags["announced_at"] <= e.window_end + pd.Timedelta(days=1))]
        row = {"event_id": e.event_id, "symbol": e.symbol, "sector": e.sector_code, "label": e.label,
               "excess26": e.excess26}
        for k, v in results_signals(res, e.symbol, e.entry_week).items():
            row[f"before_{k}"] = v
        for k, v in results_signals(res, e.symbol, e.entry_week, until=e.window_end).items():
            row[f"after_{k}"] = v
        ttm = _ttm_sales(res, e.symbol, e.entry_week)
        for f in t.itertuples():
            side = "before" if f.announced_at < e.entry_week else "after"
            trig_rows.append({
                "event_id": e.event_id, "symbol": e.symbol, "label": e.label, "side": side,
                "trigger": f.trigger, "direction": f.direction, "importance": f.importance,
                "value_cr": f.value_cr, "value_vs_sales": (f.value_cr / ttm) if f.value_cr and ttm and ttm > 0 else np.nan,
                "days_from_entry": (f.announced_at - e.entry_week).days, "note": f.note,
                **price_around(pn, e.symbol, e.industry_code, f.announced_at)})
        ev_rows.append(row)
    evdf, trdf = pd.DataFrame(ev_rows), pd.DataFrame(trig_rows)
    return {"events": evdf, "triggers": trdf, "summary": summarise(evdf, trdf)}


def summarise(evdf: pd.DataFrame, trdf: pd.DataFrame) -> dict:
    n = evdf["label"].value_counts().to_dict()
    out = {"events": n, "triggers": [], "results": {}}
    if len(trdf):
        material = trdf[(trdf["importance"] >= MIN_IMPORTANCE)
                        & ~trdf["trigger"].isin(["routine", "results_filed"])]
        for (trig, side), g in material.groupby(["trigger", "side"]):
            has = g.groupby("label")["event_id"].nunique()
            c, f = has.get("continued", 0) / max(n.get("continued", 1), 1), has.get("failed", 0) / max(n.get("failed", 1), 1)
            out["triggers"].append({
                "trigger": trig, "side": side, "continued_share": round(c, 2), "failed_share": round(f, 2),
                "gap": round(c - f, 2), "filings": len(g),
                "median_move_4w_before": _med(g, "move_4w_before"), "median_move_13w_after": _med(g, "move_13w_after"),
                "median_value_vs_sales": _med(g, "value_vs_sales"),
            })
        out["triggers"].sort(key=lambda r: -abs(r["gap"]))
    for col in [c for c in evdf.columns if c.startswith(("before_", "after_"))]:
        out["results"][col] = {lab: _med(g, col) for lab, g in evdf.groupby("label")}
    return out


def _med(df: pd.DataFrame, col: str):
    v = pd.to_numeric(df[col], errors="coerce").dropna() if col in df else pd.Series(dtype=float)
    return round(float(v.median()), 3) if len(v) else None


def write(sample: str = "pilot", path: str | None = None) -> str:
    out = build(sample)
    path = path or f"logs/trigger_study_{sample}.json"
    with open(path, "w") as fh:
        json.dump(out["summary"], fh, indent=1, default=str)
    out["triggers"].to_csv(path.replace(".json", "_triggers.csv"), index=False)
    out["events"].to_csv(path.replace(".json", "_events.csv"), index=False)
    return path
