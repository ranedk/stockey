"""Strategy registry -- the backtested technical selectors, as pluggable coded strategies (report-only).

Each SELECTOR maps a per-date cross-section of stocks to a score (higher = more preferred); the review-only
recommendation pipeline (advisory.paper_advisory) picks the top-K by whichever selector is ENABLED, then
sizes/stops/floors them with the existing stack. Strategies that do not robustly beat the incumbent are
PARKED (coded, status='parked', notes='needs_more_data') so they are re-tested automatically when more data
arrives -- nothing is thrown away.

Selectors (pure functions of a per-date cross-section DataFrame indexed by symbol):
  raw_rs            Tier0 baseline  -- mean cross-sectional rank of 63/126/252d return (today's pipeline)
  voladj_momentum   Tier1 KEEPER    -- z(ret126/vol) + z(ret252/vol)  (Nifty-momentum methodology; beat raw RS)
  residual_momentum Tier2           -- market-beta-adjusted 12m momentum (Blitz idiosyncratic momentum)
  lowvol            Tier2           -- lowest trailing volatility (India low-vol anomaly)

Enabled/parked STATE lives in config/strategy_registry.json (written by advisory.strategy_lab after the
multi-period after-cost robustness filter). This module holds the CODE + defaults and reads that state.
Authority: review/research-only; selection feeds the review-only advisory, never a broker path.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd

from utils.db import sql_to_df

REGISTRY_CONFIG = os.getenv("STRATEGY_REGISTRY_CONFIG", "config/strategy_registry.json")
MIN_TV, MIN_PX = 5e7, 30.0


# ------------------------------------------------------------------------------------------------
# Pure selector score functions -- cross-section indexed by symbol -> score Series (higher = better)
# ------------------------------------------------------------------------------------------------
def _z(x: pd.Series) -> pd.Series:
    sd = x.std(ddof=0)
    return (x - x.mean()) / sd if sd and np.isfinite(sd) and sd > 0 else x * 0.0


def score_raw_rs(cs: pd.DataFrame) -> pd.Series:
    return cs[["ret63", "ret126", "ret252"]].rank(pct=True).mean(axis=1)


def score_voladj_momentum(cs: pd.DataFrame) -> pd.Series:
    v = cs["vol252"].replace(0, np.nan)
    return _z(cs["ret126"] / v) + _z(cs["ret252"] / v)


def score_residual_momentum(cs: pd.DataFrame) -> pd.Series:
    v = cs["vol252"].replace(0, np.nan)
    return _z(cs["resid_ret252"] / v)


def score_lowvol(cs: pd.DataFrame) -> pd.Series:
    return -cs["vol252"].rank(pct=True)          # lowest vol -> highest score


SELECTOR_FNS: dict[str, Callable[[pd.DataFrame], pd.Series]] = {
    "raw_rs": score_raw_rs,
    "voladj_momentum": score_voladj_momentum,
    "residual_momentum": score_residual_momentum,
    "lowvol": score_lowvol,
}


@dataclass
class Strategy:
    name: str
    tier: str
    kind: str = "selector"
    status: str = "parked"            # 'enabled' | 'parked' | 'baseline'
    notes: str = "needs_more_data"
    knobs: dict[str, Any] = field(default_factory=dict)
    verdict: dict[str, Any] = field(default_factory=dict)

    def score(self, cs: pd.DataFrame) -> pd.Series:
        return SELECTOR_FNS[self.name](cs)


# Code-level defaults (the config overrides status/verdict once the backtest runs).
DEFAULT_STRATEGIES: list[Strategy] = [
    Strategy("raw_rs", tier="0", status="baseline", notes="incumbent selector (current pipeline)",
             knobs={"lookbacks": [63, 126, 252]}),
    Strategy("voladj_momentum", tier="1", knobs={"lookbacks": [126, 252], "vol_adjust": True}),
    Strategy("residual_momentum", tier="2", knobs={"beta_window": 252, "lookback": 252}),
    Strategy("lowvol", tier="2", knobs={"vol_window": 252}),
]


# ------------------------------------------------------------------------------------------------
# Feature panel (dhan EQUITY, split-adjusted) -- everything the selectors need, point-in-time
# ------------------------------------------------------------------------------------------------
def build_panel(*, start: str = "2021-01-01", end: str | None = None) -> pd.DataFrame:
    q = """SELECT ticker AS symbol, date, open, high, low, close, volume FROM dhan_ohlcv_daily
           WHERE instrument='EQUITY' AND exchange_segment='NSE_EQ' AND close>0 AND date >= %(s)s"""
    params = {"s": start}
    if end:
        q += " AND date <= %(e)s"; params["e"] = end
    df = sql_to_df(q + " ORDER BY ticker, date", params=params)
    df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce").dt.normalize()
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["close"]).drop_duplicates(["symbol", "date"]).sort_values(["symbol", "date"])
    g = df.groupby("symbol", group_keys=False)
    df["ret1"] = g["close"].pct_change().clip(-0.25, 0.25)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - g["close"].shift(1)).abs(),
                    (df["low"] - g["close"].shift(1)).abs()], axis=1).max(axis=1)
    df["atr_pct"] = tr.groupby(df["symbol"]).transform(lambda s: s.rolling(14).mean()) / df["close"]
    df["tv"] = g.apply(lambda d: (d["close"] * d["volume"]).rolling(20).mean()).reset_index(level=0, drop=True)
    for n in (63, 126, 252):
        df[f"ret{n}"] = g["close"].transform(lambda s, n=n: s / s.shift(n) - 1)
    df["vol252"] = g["ret1"].transform(lambda s: s.rolling(252).std()) * np.sqrt(252)
    df["liquid"] = (df["tv"] >= MIN_TV) & (df["close"] >= MIN_PX) & df["ret252"].notna() & (df["vol252"] > 0)
    # market (EW of liquid) daily return + cumulative 252d, for residual momentum
    mkt = df[df["liquid"]].groupby("date")["ret1"].mean().rename("mkt_ret1")
    df = df.merge(mkt, on="date", how="left")
    df["mkt_ret252"] = df.groupby("symbol")["mkt_ret1"].transform(lambda s: (1 + s).rolling(252).apply(np.prod, raw=True) - 1)
    beta = df.groupby("symbol", group_keys=False).apply(
        lambda d: d["ret1"].rolling(252).cov(d["mkt_ret1"]) / d["mkt_ret1"].rolling(252).var())
    df["beta252"] = beta.reset_index(level=0, drop=True) if isinstance(beta.index, pd.MultiIndex) else beta.values
    df["resid_ret252"] = df["ret252"] - df["beta252"].clip(-3, 3) * df["mkt_ret252"]
    return df


# ------------------------------------------------------------------------------------------------
# Registry config I/O
# ------------------------------------------------------------------------------------------------
def load_registry(path: str = REGISTRY_CONFIG) -> list[Strategy]:
    if not os.path.exists(path):
        return [Strategy(**{k: getattr(s, k) for k in ("name", "tier", "kind", "status", "notes", "knobs", "verdict")})
                for s in DEFAULT_STRATEGIES]
    raw = json.load(open(path))
    out = []
    for s in raw.get("strategies", []):
        if s["name"] in SELECTOR_FNS:
            out.append(Strategy(name=s["name"], tier=s.get("tier", "?"), kind=s.get("kind", "selector"),
                                status=s.get("status", "parked"), notes=s.get("notes", ""),
                                knobs=s.get("knobs", {}), verdict=s.get("verdict", {})))
    return out or load_registry("__none__")


def save_registry(strategies: list[Strategy], *, path: str = REGISTRY_CONFIG, meta: dict | None = None) -> None:
    payload = {"meta": meta or {}, "strategies": [
        {"name": s.name, "tier": s.tier, "kind": s.kind, "status": s.status, "notes": s.notes,
         "knobs": s.knobs, "verdict": s.verdict} for s in strategies]}
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)


def picks_by_date(panel: pd.DataFrame, *, k: int = 20, path: str = REGISTRY_CONFIG) -> dict:
    """{date -> top-k picks DataFrame} for the ACTIVE selector across every date in the panel. Columns are
    the book's buy-ingestion contract (symbol, close, atr_pct, rs_percentile). Lets advisory.paper_book
    manage exactly the names the advisory recommends, instead of re-deriving raw RS."""
    sel = active_selector(path)
    liq = panel[panel["liquid"]].copy()
    liq = liq[np.isfinite(liq["ret252"]) & np.isfinite(liq["vol252"])]
    out: dict = {}
    for d, cs in liq.groupby("date"):
        c = cs.set_index("symbol")
        c["rs_percentile"] = score_raw_rs(c).rank(pct=True) * 100.0
        c["_score"] = sel.score(c)
        top = c["_score"].dropna().nlargest(k)
        out[d] = c.loc[top.index, ["close", "atr_pct", "rs_percentile"]].reset_index()
    return out


def active_selector(path: str = REGISTRY_CONFIG) -> Strategy:
    """The selector the live pipeline should use: the ENABLED one (highest tier if several), else the
    baseline raw_rs. Never returns a parked strategy -- parked ones are coded but not used."""
    strategies = load_registry(path)
    enabled = [s for s in strategies if s.status == "enabled"]
    if enabled:
        return sorted(enabled, key=lambda s: s.tier, reverse=True)[0]
    return next((s for s in strategies if s.name == "raw_rs"), DEFAULT_STRATEGIES[0])


def recommend(*, asof_date: Any = None, k: int = 20, path: str = REGISTRY_CONFIG,
              panel: pd.DataFrame | None = None) -> pd.DataFrame:
    """Review-only stock recommendations from the ACTIVE (enabled or baseline) selector: the top-k liquid
    names on the latest available date, with the entry price / ATR / RS the sizing stack needs. Pure of any
    authority -- the caller (advisory.paper_advisory) applies sizing, stop, floor, and the review-only stamp.
    Returns columns: rank, symbol, selector, score, entry_price, atr_pct, rs_percentile."""
    sel = active_selector(path)
    if panel is None:
        panel = build_panel(start="2021-01-01")
    panel = panel[panel["liquid"]].copy()
    if asof_date is not None:
        asof = pd.Timestamp(asof_date)
        asof = asof.tz_localize("UTC") if asof.tzinfo is None else asof.tz_convert("UTC")
        panel = panel[panel["date"] <= asof.normalize()]
    if panel.empty:
        return pd.DataFrame(columns=["rank", "symbol", "selector", "score", "entry_price", "atr_pct", "rs_percentile"])
    latest = panel["date"].max()
    cs = panel[panel["date"] == latest].set_index("symbol")
    cs = cs[np.isfinite(cs["ret252"]) & np.isfinite(cs["vol252"])]
    cs["rs_percentile"] = score_raw_rs(cs).rank(pct=True) * 100.0     # display continuity with the current UI
    cs["_score"] = sel.score(cs)
    top = cs["_score"].dropna().nlargest(k)
    # columns match the paper_advisory sizing contract (symbol, close, atr_pct, rs_percentile, avg_turnover_inr)
    out = cs.loc[top.index, ["_score", "close", "atr_pct", "rs_percentile", "tv"]].reset_index()
    out = out.rename(columns={"_score": "score", "tv": "avg_turnover_inr"})
    out.insert(0, "rank", range(1, len(out) + 1))
    out.insert(2, "selector", sel.name)
    return out
