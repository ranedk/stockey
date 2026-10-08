"""Momentum entries and their 26-week outcomes (the events the trigger study explains).

Point-in-time throughout: liquidity is judged on data up to the previous week, the momentum
rank on that week's close. Only the OUTCOME looks forward, and it is used to label events,
never to choose which filings are read (the tagger never sees it).

Prices are read from the local systrader database (CLAUDE.md: heavy reads go there), weekly =
last total-return close of the week.
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd
from environs import Env
from sqlalchemy import create_engine, text

from utils.db import db_session, upsert_to_db

EVENTS_TABLE = "fundamentals_trigger_events"

PRICE_FROM = "2014-01-01"
PRICE_TO = "2023-09-30"            # 26 weeks past the last discovery entry, no further
DISCOVERY_FROM = pd.Timestamp("2016-07-01")
DISCOVERY_TO = pd.Timestamp("2022-12-31")

LIQUIDITY_MIN_RS = 5e7             # Rs 5 cr average daily traded value, previous 12 weeks
TOP_SHARE = 0.10                   # momentum = top tenth by RS26
QUIET_WEEKS = 8                    # an entry needs 8 weeks outside the top tenth before it
MIN_HISTORY_WEEKS = 104            # listed two years
ROCKET_52W = 3.0                   # +300% in a year before entry: pump-and-dump risk, excluded
HORIZON = 26
CONTINUED_EXCESS = 0.20            # beat its industry by 20%+ over 26 weeks ...
CONTINUED_MAX_DD = -0.30           # ... without falling 30% on the way
FAILED_EXCESS = -0.10              # lagged its industry by 10%+
EXCLUDED_SECTORS = {"IN0501", "IN1201"}   # financial services (different statements), diversified
WINDOW_BEFORE_DAYS = 365
WINDOW_AFTER_DAYS = 91


def _systrader_conn():
    env = Env()
    env.read_env(os.path.join(os.path.dirname(__file__), "../../../systrade/.env"), override=False)
    url = "postgresql+psycopg2://{u}:{p}@{h}:{port}/{db}".format(
        u=env("SYSTRADER_POSTGRES_USER", "systrader"), p=env("SYSTRADER_POSTGRES_PASSWORD", "systrader"),
        h=env("SYSTRADER_POSTGRES_HOST", "localhost"), port=env.int("SYSTRADER_POSTGRES_PORT", 5432),
        db=env("SYSTRADER_POSTGRES_DB", "systrader"))
    return create_engine(url).connect()


def load_weekly() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Weekly total-return close and average daily traded value, symbols as columns."""
    sql = """
        SELECT symbol, date_trunc('week', date)::date AS wk,
               (array_agg(tr_adj_close ORDER BY date DESC))[1] AS close,
               avg(adj_close * adj_volume) AS tv
          FROM advisory_adjusted_ohlcv_daily
         WHERE date >= :a AND date <= :b AND series = 'EQ' AND tr_adj_close > 0
         GROUP BY 1, 2
    """
    with _systrader_conn() as conn:
        df = pd.read_sql(text(sql), conn, params={"a": PRICE_FROM, "b": PRICE_TO})
    df["wk"] = pd.to_datetime(df["wk"])
    close = df.pivot(index="wk", columns="symbol", values="close").sort_index()
    tv = df.pivot(index="wk", columns="symbol", values="tv").sort_index()
    return close, tv


def load_membership() -> pd.DataFrame:
    sql = """
        SELECT DISTINCT ON (symbol) symbol, sector_code, industry_code
          FROM master_sharpely_equity
         WHERE symbol IS NOT NULL
         ORDER BY symbol, nse_active DESC NULLS LAST
    """
    with _systrader_conn() as conn:
        return pd.read_sql(text(sql), conn).set_index("symbol")


def _group_index(capped: pd.DataFrame, elig: pd.DataFrame, members: list[str], fallback: pd.Series) -> pd.Series:
    """Equal-weight weekly return of a group's eligible members; the fallback where < 5."""
    cols = [c for c in members if c in capped.columns]
    if not cols:
        return fallback
    r = capped[cols].where(elig[cols])
    n = r.notna().sum(axis=1)
    return r.mean(axis=1).where(n >= 5, fallback)


def panel() -> dict:
    """Weekly closes, eligibility, momentum rank and the market / industry index levels."""
    close, tv = load_weekly()
    memb = load_membership()
    ret = close.pct_change(fill_method=None)
    capped = ret.clip(-0.5, 0.5)
    liq = tv.rolling(12, min_periods=8).median().shift(1)
    elig = (liq >= LIQUIDITY_MIN_RS) & close.notna() & close.shift(MIN_HISTORY_WEEKS).notna()

    mkt_r = capped.where(elig).mean(axis=1).fillna(0.0)
    mkt_lvl = (1 + mkt_r).cumprod()

    sector_r, industry_lvl = {}, {}
    for sec, grp in memb.groupby("sector_code"):
        sector_r[sec] = _group_index(capped, elig, list(grp.index), mkt_r).fillna(0.0)
    for ind, grp in memb.groupby("industry_code"):
        sec = grp["sector_code"].mode().iloc[0] if grp["sector_code"].notna().any() else None
        fb = sector_r.get(sec, mkt_r)
        industry_lvl[ind] = (1 + _group_index(capped, elig, list(grp.index), fb).fillna(0.0)).cumprod()

    rs26 = close / close.shift(HORIZON) - (mkt_lvl / mkt_lvl.shift(HORIZON)).values[:, None]
    rank = rs26.where(elig).rank(axis=1, pct=True)
    mom = rank >= 1 - TOP_SHARE
    was_quiet = ~mom.shift(1).rolling(QUIET_WEEKS, min_periods=QUIET_WEEKS).max().fillna(1).astype(bool)
    entry = mom & was_quiet
    r52 = close / close.shift(52) - 1
    return {"close": close, "elig": elig, "rank": rank, "entry": entry, "r52": r52, "mkt_lvl": mkt_lvl,
            "industry_lvl": industry_lvl, "memb": memb}


def compute_events(pn: dict | None = None) -> pd.DataFrame:
    pn = pn or panel()
    close, rank, entry, r52 = pn["close"], pn["rank"], pn["entry"], pn["r52"]
    mkt_lvl, industry_lvl, memb = pn["mkt_lvl"], pn["industry_lvl"], pn["memb"]
    rows = []
    weeks = close.index
    for sym in entry.columns:
        if sym not in memb.index:
            continue
        sec, ind = memb.at[sym, "sector_code"], memb.at[sym, "industry_code"]
        if not sec or sec in EXCLUDED_SECTORS or ind not in industry_lvl:
            continue
        ilvl = industry_lvl[ind]
        for t in np.flatnonzero(entry[sym].to_numpy()):
            wk = weeks[t]
            if wk < DISCOVERY_FROM or wk > DISCOVERY_TO or t + HORIZON >= len(weeks):
                continue
            if r52[sym].iat[t] > ROCKET_52W:
                continue
            path = close[sym].iloc[t:t + HORIZON + 1]
            if path.isna().any():
                continue
            stock26 = path.iat[-1] / path.iat[0]
            ind26 = ilvl.iat[t + HORIZON] / ilvl.iat[t]
            mkt26 = mkt_lvl.iat[t + HORIZON] / mkt_lvl.iat[t]
            excess = stock26 / ind26 - 1
            dd = float((path / path.cummax() - 1).min())
            label = ("continued" if excess >= CONTINUED_EXCESS and dd > CONTINUED_MAX_DD
                     else "failed" if excess <= FAILED_EXCESS else "middle")
            rows.append({
                "event_id": f"{sym}:{wk.date()}", "symbol": sym, "sector_code": sec,
                "industry_code": ind, "entry_week": wk.date(), "rs_rank_pct": float(rank[sym].iat[t]),
                "ret52_before": float(r52[sym].iat[t]), "excess26": float(excess), "maxdd26": dd,
                "stock26": float(stock26 - 1), "industry_vs_market26": float(ind26 / mkt26 - 1),
                "label": label,
                "window_start": (wk - pd.Timedelta(days=WINDOW_BEFORE_DAYS)).date(),
                "window_end": (wk + pd.Timedelta(days=WINDOW_AFTER_DAYS)).date(),
            })
    return pd.DataFrame(rows)


def pick_pilot(events: pd.DataFrame, per_group: int = 20, seed: int = 1) -> pd.DataFrame:
    """per_group continued + per_group failed events, one per stock, no stock in both groups,
    spread across sectors round-robin, restricted to stocks with screener.in quarterly results
    (the free results signals need them)."""
    from utils.db import sql_to_df
    have_results = set(sql_to_df("SELECT DISTINCT ticker FROM fundamentals_quarterly_results")["ticker"])
    ev = events[events["symbol"].isin(have_results)].sample(frac=1.0, random_state=seed)
    chosen, used = [], set()
    for label in ("continued", "failed"):
        pool = ev[(ev["label"] == label) & ~ev["symbol"].isin(used)].drop_duplicates("symbol")
        by_sector = {s: g for s, g in pool.groupby("sector_code")}
        picked = []
        while len(picked) < per_group and any(len(g) for g in by_sector.values()):
            for s in sorted(by_sector):
                g = by_sector[s]
                g = g[~g["symbol"].isin(used)]
                if len(g):
                    row = g.iloc[0]
                    picked.append(row)
                    used.add(row["symbol"])
                    by_sector[s] = g.iloc[1:]
                else:
                    by_sector[s] = g
                if len(picked) >= per_group:
                    break
        chosen.extend(picked)
    return pd.DataFrame(chosen)


def ensure_table() -> None:
    with db_session() as (_, cur):
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {EVENTS_TABLE} (
                event_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, sector_code TEXT, industry_code TEXT,
                entry_week DATE NOT NULL, rs_rank_pct DOUBLE PRECISION, ret52_before DOUBLE PRECISION,
                excess26 DOUBLE PRECISION, maxdd26 DOUBLE PRECISION, stock26 DOUBLE PRECISION,
                industry_vs_market26 DOUBLE PRECISION, label TEXT NOT NULL,
                window_start DATE NOT NULL, window_end DATE NOT NULL,
                sample TEXT, computed_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )""")


def build(per_group: int = 20) -> dict:
    ensure_table()
    events = compute_events()
    pilot = pick_pilot(events, per_group=per_group)
    events["sample"] = np.where(events["event_id"].isin(pilot["event_id"]), "pilot", None)
    upsert_to_db(events, EVENTS_TABLE, ["event_id"])
    counts = events["label"].value_counts().to_dict()
    return {"events": len(events), "labels": counts, "pilot": len(pilot),
            "pilot_symbols": sorted(pilot["symbol"])}


def samples(sample: str) -> list[str]:
    """'full' is every labelled discovery event, the pilot's included."""
    return ["pilot", "full"] if sample == "full" else [sample]


def mark_full() -> dict:
    """Tags every continued/failed event not in the pilot as 'full'. Does NOT recompute events:
    rebuilding would re-pick the pilot."""
    with db_session() as (_, cur):
        cur.execute(f"""UPDATE {EVENTS_TABLE} SET sample = 'full'
                         WHERE sample IS NULL AND label IN ('continued', 'failed')""")
        n = cur.rowcount
    return {"marked_full": n}
