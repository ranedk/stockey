"""Forward records for the fundamental families (TODO C6 quality, C8 value; 2026-09-28).

No backtest is possible -- fundamental history starts with the dated snapshot (TODO C4,
fundamentals/collectors/fundamentals_snapshot.py, 2026-09-27) -- so each family is a
FORWARD record: a frozen rule, applied monthly, whose holdings are written down with the
reference price at the time. In a year the record answers whether the family earns its
keep here; until then it is evidence being collected, not a result. Pre-registered in
systrader research/LEDGER.md (rows 46-47) under docs/RESEARCH_PROTOCOL.md.

Rules (operator, 2026-09-28) -- FROZEN. A change is a new version and a new record, never
an edit (the stored spec is compared on every run and a mismatch fails loudly):

  quality v1: operating companies in the universe, profitable in each of the last 2 years,
    positive 3-year free cash flow; score = mean percentile rank of 5-year average ROCE
    (high), 5-year average ROE (high), debt/equity (low).
  value v1:  the top half of quality v1's eligible set by quality score; score = mean
    percentile rank of earnings yield (high) and EV/EBIT (low, positive only).
  both: top 30 equal weight, rebalanced on the first run of each month; a held name is kept
    until it falls outside the top 60 (fewer trades, less short-term tax -- TODO A1/A2).

Inputs are read point-in-time: the newest snapshot on or before the rebalance date, and the
universe run on or before it. Reference price = latest adjusted close on or before it.

Paradigm tracks (operator, 2026-10-23; systrader LEDGER, the /paradigms page): five more
frozen records, each a rule-based reading of a published investing method from
twitter_handles.md -- marcellus, sageone, maheshwari, kedia, whiteoak. Each spec carries the
method's paradigm in plain words and an "approximations" text naming everything that had to
be guessed or left out; kedia and whiteoak are crude stand-ins and say so. They add three
things to the framework: a rebalance cadence (monthly / quarterly / annual), a monthly
REVIEW for tracks that sell a failing holding between rebalances (its weight waits in cash;
review rows carry event='review' so they do not restart the rebalance clock), and
track-specific selection (sector caps, sector quotas, a sector-first pick).

    python -m fundamentals.screens.forward_tracks            # rebalance any track that is due
    python -m fundamentals.screens.forward_tracks --report   # returns so far vs the universe
    python -m fundamentals.screens.forward_tracks --dry-run [--as-of YYYY-MM-DD]  # what each would hold; writes nothing
"""

from __future__ import annotations

import argparse
import json
from datetime import timedelta

import pandas as pd

from utils.db import db_session, sql_to_df, upsert_to_db

SYNC_SOURCE_NAME = "fundamentals.screens.forward_tracks"
SPEC_TABLE = "fundamentals_forward_track_spec"
HOLDING_TABLE = "fundamentals_forward_track_holding"
STOCKEY_RUN_STATE: dict[str, object] = {}

TRACKS: dict[str, dict] = {
    "quality": {"version": 1, "start": "2026-10-01", "top_n": 30, "keep_within": 60, "rebalance": "monthly",
                "universe": "l1_universe latest run on/before date, group=operating",
                "eligible": "profit_y1>0, profit_y2>0, fcf_3y_total>0, roce_5y/roe_5y/debt_to_equity present",
                "score": "mean pct rank: roce_5y_avg_pct high, roe_5y_avg_pct high, debt_to_equity low"},
    "value": {"version": 1, "start": "2026-10-01", "top_n": 30, "keep_within": 60, "rebalance": "monthly",
              "universe": "quality v1 eligible set, quality score >= its median",
              "eligible": "earnings_yield_pct present, ev_to_ebit > 0",
              "score": "mean pct rank: earnings_yield_pct high, ev_to_ebit low"},
    # --- paradigm tracks (2026-10-23). Rules below are frozen like quality/value. ---
    "marcellus": {
        "version": 1, "start": "2026-10-26", "top_n": 15, "keep_within": 30, "rebalance": "annual",
        "review": "monthly: a holding that no longer passes 'eligible' is sold; its weight waits in cash "
                  "until the next annual rebalance",
        "universe": "l1_universe latest run on/before date, group=operating (lenders excluded)",
        "eligible": "profit_y1>0, profit_y2>0, fcf_3y_total>0, ocf_3y_total>0, roce_5y_avg_pct>=15, "
                    "debt_to_equity<=0.5, promoter_pledged_pct<=1 (missing = 0), and trailing-4-quarter sales up "
                    ">=10% year on year at each of the last 5 consecutive fiscal year-ends (March) known on the "
                    "date, the latest no older than the previous calendar year",
        "score": "mean pct rank: roce_5y_avg_pct high, roe_5y_avg_pct high",
        "paradigm": "Saurabh Mukherjea / Marcellus: ten-year share-price returns follow earnings growth, not the "
                    "starting valuation. Throw out companies with accounting red flags, keep the few that grew "
                    "revenue by double digits AND earned more than their cost of capital every single year, and "
                    "hold them for a decade with almost no trading.",
        "approximations": "Marcellus' 10-12 forensic ratios are not in our data: they are stood in for by positive "
                          "3-year free cash flow and operating cash flow, low debt and no promoter pledge. 'Ten "
                          "consecutive years' is cut to the last 5 fiscal years we can measure (quarterly results "
                          "from 2017, two sources), and 'return on capital above its cost every year' to a 5-year "
                          "average ROCE of 15%+. No moat research (their step 3). Lenders (their Kings of Capital) "
                          "are outside this universe. Equal weight, reset at each annual rebalance.",
    },
    "sageone": {
        "version": 1, "start": "2026-10-26", "top_n": 20, "keep_within": 40, "max_per_industry": 5,
        "rebalance": "quarterly",
        "universe": "l1_universe latest run on/before date, group=operating; market-cap rank 101-1000 among all "
                    "companies in that day's snapshot",
        "eligible": "profit_growth_5y_pct>=20, roe_5y_avg_pct>=20, roce_pct>=20, profit_y1>0, profit_y2>0, "
                    "(ocf_y1+ocf_y2)/(profit_y1+profit_y2)>=1, debt_to_equity<=0.5, promoter_pledged_pct<=1 "
                    "(missing = 0)",
        "score": "mean pct rank: profit_growth_5y_pct high, roce_pct high; at most 5 per NSE industry",
        "paradigm": "Samit Vartak / SageOne: buy businesses whose earnings can double in 3-4 years (20-25% a "
                    "year), with high returns on capital, profits that turn into cash and no governance red "
                    "flags -- ideally before they enter the top 100 -- and hold them 3-4 years, selling when the "
                    "earnings-doubling story breaks.",
        "approximations": "'Earnings that can double' is read from the past: 5-year profit growth of 20%+. ROE "
                          "uses the 5-year average (steadier than one year). Cash conversion (CFO/PAT >= 1) uses "
                          "the last two years. His forensic model, channel checks, fair-value price and conviction "
                          "weights are private: equal weight here. No incremental-ROCE test (no capex history). "
                          "His 25% sector cap becomes at most 5 of 20 names per NSE industry. Lenders are out.",
    },
    "maheshwari": {
        "version": 1, "start": "2026-10-26", "top_n": 15, "per_industry": 4, "buy_industries": 4,
        "keep_industries": 8, "near_high_pct": 5, "min_members": 5, "min_industry_sales_growth_5y": 15,
        "rebalance": "monthly",
        "universe": "l1_universe latest run on/before date, group=operating; NSE industry from "
                    "fundamentals_industry_classification",
        "industry_breadth": "share of an industry's members (at least 5 with prices) whose latest adjusted close is "
                            "within 5% of their highest close of the past 365 days; industries whose median "
                            "sales_growth_5y_pct is below 15 are left out",
        "eligible": "in a top-4 industry by breadth; sales_growth_5y_pct>=20 and roe_pct>=20",
        "score": "market_cap_cr: the largest passing companies are the leaders, taken leader-first across the "
                 "industries, at most 4 per industry, 15 in all",
        "exit": "a holding is sold when its industry falls out of the top 8 by breadth or it fails the 20/20 screen",
        "paradigm": "Basant Maheshwari: the big money is made in the leader of a secular growth sector, bought at a "
                    "premium and held while its profits grow. Find the sector first -- a pack of its stocks "
                    "making new highs, the sector itself growing fast -- buy its leaders by growth and return on "
                    "equity, and sell when the theme fades and its stocks stop holding their highs.",
        "approximations": "'Sector growing ~30%' becomes a floor of 15% median 5-year sales growth across the "
                          "industry (few whole industries grow at 30%). 'Leader' = the largest company passing his "
                          "BM Focused 20/20 screen (5-year sales growth and ROE both 20%+); his cash-flow, "
                          "dividend, promoter-holding and no-dilution checks are not applied. 'Theme fades' = the "
                          "industry drops out of the top 8 by share of stocks near their highs. Equal weight. "
                          "Lenders are out.",
    },
    "kedia": {
        "version": 1, "start": "2026-10-26", "top_n": 15, "rebalance": "annual",
        "universe": "l1_universe latest run on/before date, group=operating",
        "eligible": "market_cap_cr 500-5000, first traded on NSE at least 10 years before the date (dim_security), "
                    "sales_growth_5y_pct>=15, profit_growth_5y_pct>=15, debt_to_equity<=1, "
                    "promoter_pledged_pct<=1 (missing = 0)",
        "score": "pct rank: sales_growth_5y_pct high",
        "exit": "at the annual review a holding is kept whatever its rank or size, unless it lost money in both of "
                "the last 2 years (profit_y1<0 and profit_y2<0); no stop loss; free slots go to the best new names",
        "paradigm": "Vijay Kedia -- SMILE: Small in size, Medium in experience, Large in aspiration, Extra-large "
                    "market potential. A small company in a very large market, run by a management that has been "
                    "through a full cycle and still wants to grow, bought at a fair price and held for 10-15 years "
                    "with no exit decided at entry.",
        "approximations": "CRUDE STAND-IN. SMILE is a judgement of the promoter, made in person; none of it is in "
                          "data. Small = market cap 500-5,000 cr. Medium experience = listed at least 10 years. "
                          "Large aspiration and extra-large market = 5-year sales and profit growth both 15%+. "
                          "There is no measure of management at all. Read this track as 'small, seasoned, growing "
                          "companies held for years', not as Kedia's picks.",
    },
    "whiteoak": {
        "version": 1, "start": "2026-10-26", "top_n": 80, "keep_multiple": 2, "assumed_cost_of_capital_pct": 12,
        "rebalance": "quarterly",
        "universe": "l1_universe latest run on/before date, group=operating; NSE sector from "
                    "fundamentals_industry_classification",
        "eligible": "promoter_pledged_pct<=1 (missing = 0), no stockey story flaw on the latest "
                    "fundamentals_story_filter_daily row on/before the date (missing = no flaw), roce_5y_avg_pct>=15, "
                    "roce_pct present, ev_to_ebit>0",
        "score": "(roce_pct - 12) / ev_to_ebit: return on capital above an assumed 12% cost of capital, per unit of "
                 "price",
        "construction": "80 names; each NSE sector gets slots in proportion to its share of the eligible set (at "
                        "least 1, largest remainders); within a sector the best scores, a held name kept while it "
                        "ranks within 2x its sector's slots",
        "paradigm": "Prashant Khemka / WhiteOak: great businesses at attractive valuations, where 'great' means a "
                    "high return on the capital it adds. OpcoFinco splits a company into a capital-light operating "
                    "business and a finance company that earns only the cost of capital, and prices the operating "
                    "part. Alpha should come from stock picking alone, so the book is balanced across sectors and "
                    "styles.",
        "approximations": "CRUDE STAND-IN. The real OpcoFinco multiple needs invested capital, a cost of capital "
                          "and excess cash-flow forecasts; here it is ROCE above an assumed 12% cost of capital "
                          "divided by EV/EBIT. The governance screen is reduced to no promoter pledge and no "
                          "stockey story flaw. Factor balancing is reduced to sector balancing. Lenders are out.",
    },
}
PARADIGM_TRACKS = ("marcellus", "sageone", "maheshwari", "kedia", "whiteoak")
CADENCE_MONTHS = {"monthly": 1, "quarterly": 3, "annual": 12}

_SPEC_DDL = f"""
    CREATE TABLE IF NOT EXISTS {SPEC_TABLE} (
        track TEXT NOT NULL, version INTEGER NOT NULL, spec_json TEXT NOT NULL,
        frozen_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (track, version)
    )
"""
_HOLDING_DDL = f"""
    CREATE TABLE IF NOT EXISTS {HOLDING_TABLE} (
        track TEXT NOT NULL, version INTEGER NOT NULL, rebalance_date DATE NOT NULL,
        company_master_id TEXT NOT NULL, symbol TEXT, rank INTEGER, score DOUBLE PRECISION,
        weight DOUBLE PRECISION, action TEXT, ref_price DOUBLE PRECISION, ref_price_date DATE,
        snapshot_date DATE, load_ts TIMESTAMPTZ DEFAULT now(),
        PRIMARY KEY (track, version, rebalance_date, company_master_id)
    )
"""


def ensure_tables() -> None:
    with db_session() as (_, cur):
        cur.execute(_SPEC_DDL)
        cur.execute(_HOLDING_DDL)
        # 'rebalance' (or NULL, every row before 2026-10-23) | 'review': a monthly check that sold a
        # failing holding without a rebalance -- it must not restart an annual/quarterly clock
        cur.execute(f"ALTER TABLE {HOLDING_TABLE} ADD COLUMN IF NOT EXISTS event TEXT")


def freeze_specs() -> None:
    """Store each spec on first sight; fail if a stored spec for the same version differs."""
    stored = sql_to_df(f"SELECT track, version, spec_json FROM {SPEC_TABLE}")
    have = {(r.track, int(r.version)): r.spec_json for r in stored.itertuples()} if not stored.empty else {}
    for track, spec in TRACKS.items():
        text = json.dumps(spec, sort_keys=True)
        key = (track, spec["version"])
        if key not in have:
            with db_session() as (_, cur):
                cur.execute(f"INSERT INTO {SPEC_TABLE} (track, version, spec_json) VALUES (%s, %s, %s)",
                            (track, spec["version"], text))
        elif have[key] != text:
            raise RuntimeError(f"forward track {track} v{spec['version']} changed after it was frozen; "
                               "a change is a new version (and a new record), never an edit")


# --- Inputs, point-in-time ---------------------------------------------------------------

def load_inputs(as_of) -> pd.DataFrame:
    """Operating companies in the universe run on/before `as_of`, joined to the newest
    fundamentals snapshot on/before `as_of` (within 10 days)."""
    d = str(pd.Timestamp(as_of).date())
    universe = sql_to_df(
        """
        SELECT company_id AS screener_company_id, ticker, metrics_json
          FROM fundamentals_l1_universe
         WHERE run_date = (SELECT max(run_date) FROM fundamentals_l1_universe WHERE run_date::date <= %s)
        """,
        params=(d,),
    )
    if universe.empty:
        return universe
    universe["group"] = universe["metrics_json"].map(lambda m: (json.loads(m) if m else {}).get("universe_group"))
    universe = universe[universe["group"].eq("operating")].drop(columns=["metrics_json"])
    snap = sql_to_df(
        """
        SELECT DISTINCT ON (screener_company_id) *
          FROM fundamentals_snapshot_daily
         WHERE as_of_date <= %s AND as_of_date >= %s::date - 10
         ORDER BY screener_company_id, as_of_date DESC
        """,
        params=(d, d),
    )
    if snap.empty:
        return snap
    return universe.merge(snap, on="screener_company_id", how="inner")


def _pct(s: pd.Series, *, high_is_good: bool) -> pd.Series:
    r = s.rank(pct=True, method="average")
    return r if high_is_good else 1 - r + (1 / len(s) if len(s) else 0)


def quality_eligible(df: pd.DataFrame) -> pd.DataFrame:
    need = ["profit_y1", "profit_y2", "fcf_3y_total", "roce_5y_avg_pct", "roe_5y_avg_pct", "debt_to_equity"]
    x = df.dropna(subset=[c for c in need if c in df.columns]).copy()
    if x.empty or any(c not in x.columns for c in need):
        return x.iloc[0:0]
    x = x[(x["profit_y1"] > 0) & (x["profit_y2"] > 0) & (x["fcf_3y_total"] > 0)].copy()
    x["score"] = (_pct(x["roce_5y_avg_pct"], high_is_good=True) + _pct(x["roe_5y_avg_pct"], high_is_good=True)
                  + _pct(x["debt_to_equity"], high_is_good=False)) / 3
    return x


def value_eligible(df: pd.DataFrame) -> pd.DataFrame:
    q = quality_eligible(df)
    if q.empty:
        return q
    x = q[q["score"] >= q["score"].median()].copy()
    x = x.dropna(subset=["earnings_yield_pct", "ev_to_ebit"])
    x = x[x["ev_to_ebit"] > 0].copy()
    if x.empty:
        return x
    x["score"] = (_pct(x["earnings_yield_pct"], high_is_good=True) + _pct(x["ev_to_ebit"], high_is_good=False)) / 2
    return x


# --- Paradigm inputs ---------------------------------------------------------------------

def _symbol(company_master_id) -> str | None:
    return company_master_id.split(":", 1)[-1] if isinstance(company_master_id, str) else None


def _pledge_ok(df: pd.DataFrame) -> pd.Series:
    return df["promoter_pledged_pct"].fillna(0) <= 1


def load_paradigm_inputs(as_of) -> pd.DataFrame:
    """load_inputs plus what the paradigm rules read, all on/before `as_of`: market-cap rank among
    every company in the snapshot, NSE sector/industry, first NSE trade date, and the story flaw."""
    df = load_inputs(as_of)
    if df.empty:
        return df
    d = str(pd.Timestamp(as_of).date())
    df = df.copy()
    df["symbol"] = df["company_master_id"].map(_symbol)
    caps = sql_to_df(
        """
        SELECT DISTINCT ON (screener_company_id) screener_company_id, market_cap_cr AS cap
          FROM fundamentals_snapshot_daily
         WHERE as_of_date <= %s AND as_of_date >= %s::date - 10
         ORDER BY screener_company_id, as_of_date DESC
        """,
        params=(d, d),
    )
    caps["mcap_rank"] = caps["cap"].rank(ascending=False, method="first")
    df = df.merge(caps[["screener_company_id", "mcap_rank"]], on="screener_company_id", how="left")
    # classification rarely changes: the newest row on/before the date, else the nearest after it
    ic = sql_to_df(
        """
        SELECT DISTINCT ON (symbol) symbol, sector, industry
          FROM fundamentals_industry_classification
         WHERE symbol IS NOT NULL
         ORDER BY symbol, (fetch_date > %s::date), abs(fetch_date - %s::date)
        """,
        params=(d, d),
    )
    df = df.merge(ic, on="symbol", how="left")
    first = sql_to_df("SELECT symbol, min(first_trade_date)::date AS first_trade FROM dim_security "
                      "WHERE symbol IS NOT NULL AND first_trade_date IS NOT NULL GROUP BY symbol")
    df = df.merge(first, on="symbol", how="left")
    flaw = sql_to_df(
        """
        SELECT DISTINCT ON (company_master_id) company_master_id, has_flaw
          FROM fundamentals_story_filter_daily
         WHERE date <= %s AND date >= %s::date - 10
         ORDER BY company_master_id, date DESC
        """,
        params=(d, d),
    )
    df = df.merge(flaw, on="company_master_id", how="left")
    df["has_flaw"] = df["has_flaw"].astype("boolean").fillna(False).astype(bool)
    return df


def _quarter_back(q, k: int):
    return (pd.Timestamp(q) - pd.offsets.QuarterEnd(k)).date()


def fiscal_year_growth(sales: pd.Series) -> dict:
    """{fiscal year-end (31 March): trailing-4-quarter sales / the year before - 1}, from one
    source's quarterly sales indexed by quarter-end date. Only complete, consecutive quarters count."""
    have = {pd.Timestamp(k).date(): float(v) for k, v in sales.items() if pd.notna(v)}
    out = {}
    for q in have:
        if q.month != 3:
            continue
        cur = [_quarter_back(q, k) for k in range(4)]
        prev = [_quarter_back(q, k) for k in range(4, 8)]
        if all(x in have for x in cur + prev):
            base = sum(have[x] for x in prev)
            if base > 0:
                out[q] = sum(have[x] for x in cur) / base - 1
    return out


def revenue_growth_years(as_of) -> tuple[dict, dict]:
    """Fiscal-year sales growth known on `as_of`, from both results sources, each used on its own
    (screener's are standalone and XBRL's prefer consolidated, so they are never mixed in one ratio):
    ({screener_company_id: {fy_end: growth}}, {symbol: {fy_end: growth}})."""
    d = str(pd.Timestamp(as_of).date())
    qr = sql_to_df(
        """
        SELECT company_id, period_end, sales FROM fundamentals_quarterly_results
         WHERE layout = 'operating' AND sales IS NOT NULL AND first_seen_at::date <= %s
        """,
        params=(d,),
    )
    tr = sql_to_df(
        """
        SELECT symbol, period_end, consolidated, sales FROM fundamentals_trigger_results
         WHERE status = 'ok' AND sales IS NOT NULL
           AND coalesce(public_at::date, period_end + 60) <= %s
        """,
        params=(d,),
    )
    by_id = {int(k): fiscal_year_growth(g.set_index("period_end")["sales"]) for k, g in qr.groupby("company_id")} \
        if not qr.empty else {}
    by_sym = {}
    for sym, g in (tr.groupby("symbol") if not tr.empty else []):
        g = g[g["consolidated"]] if g["consolidated"].any() else g
        by_sym[sym] = fiscal_year_growth(g.drop_duplicates("period_end").set_index("period_end")["sales"])
    return by_id, by_sym


def consistent_growth(years: dict, as_of, *, n: int = 5, floor: float = 0.10) -> bool:
    """The last n consecutive fiscal year-ends all grew at least `floor`, the latest no older than
    the previous calendar year (a company whose results stopped is not consistent)."""
    if not years:
        return False
    last = max(years)
    if last.year < pd.Timestamp(as_of).year - 1:
        return False
    want = [last.replace(year=last.year - k) for k in range(n)]
    return all(w in years and years[w] >= floor for w in want)


def near_high_share(symbols: list[str], as_of, *, near_pct: float) -> dict[str, bool]:
    """Per symbol: latest adjusted close within `near_pct`% of its highest close of the past 365
    days (bounded read; a symbol without a close in the last 10 days is left out)."""
    if not symbols:
        return {}
    d = str(pd.Timestamp(as_of).date())
    px = sql_to_df(
        """
        SELECT symbol, max(adj_close) AS hi, (array_agg(adj_close ORDER BY date DESC))[1] AS last,
               max(date)::date AS last_date
          FROM advisory_adjusted_ohlcv_daily
         WHERE symbol = ANY(%s) AND date > %s::date - interval '365 days' AND date < %s::date + interval '1 day'
         GROUP BY symbol
        """,
        params=(symbols, d, d),
    )
    cut = pd.Timestamp(as_of).date() - timedelta(days=10)
    px = px[pd.to_datetime(px["last_date"]).dt.date >= cut]
    return {r.symbol: bool(r.last >= (1 - near_pct / 100) * r.hi) for r in px.itertuples() if r.hi and r.hi > 0}


# --- Paradigm scorers: eligible rows with a `score` (higher = better) ---------------------

def marcellus_eligible(df: pd.DataFrame, as_of=None) -> pd.DataFrame:
    need = ["profit_y1", "profit_y2", "fcf_3y_total", "ocf_3y_total", "roce_5y_avg_pct", "roe_5y_avg_pct",
            "debt_to_equity"]
    if df.empty or any(c not in df.columns for c in need):
        return df.iloc[0:0]
    x = df.dropna(subset=need)
    x = x[(x["profit_y1"] > 0) & (x["profit_y2"] > 0) & (x["fcf_3y_total"] > 0) & (x["ocf_3y_total"] > 0)
          & (x["roce_5y_avg_pct"] >= 15) & (x["debt_to_equity"] <= 0.5) & _pledge_ok(x)].copy()
    if x.empty:
        return x
    if "growth_ok" not in x.columns:
        by_id, by_sym = revenue_growth_years(as_of)
        x["growth_ok"] = [consistent_growth({**by_sym.get(sym, {}), **by_id.get(int(cid), {})}, as_of)
                          for cid, sym in zip(x["screener_company_id"], x["symbol"])]
    x = x[x["growth_ok"].astype(bool)].copy()
    if x.empty:
        return x
    x["score"] = (_pct(x["roce_5y_avg_pct"], high_is_good=True) + _pct(x["roe_5y_avg_pct"], high_is_good=True)) / 2
    return x


def sageone_eligible(df: pd.DataFrame, as_of=None) -> pd.DataFrame:
    need = ["mcap_rank", "profit_growth_5y_pct", "roe_5y_avg_pct", "roce_pct", "profit_y1", "profit_y2",
            "ocf_y1", "ocf_y2", "debt_to_equity"]
    if df.empty or any(c not in df.columns for c in need):
        return df.iloc[0:0]
    x = df.dropna(subset=need)
    profit = x["profit_y1"] + x["profit_y2"]
    x = x[x["mcap_rank"].between(101, 1000) & (x["profit_growth_5y_pct"] >= 20) & (x["roe_5y_avg_pct"] >= 20)
          & (x["roce_pct"] >= 20) & (x["profit_y1"] > 0) & (x["profit_y2"] > 0)
          & ((x["ocf_y1"] + x["ocf_y2"]) >= profit) & (x["debt_to_equity"] <= 0.5) & _pledge_ok(x)].copy()
    if x.empty:
        return x
    x["score"] = (_pct(x["profit_growth_5y_pct"], high_is_good=True) + _pct(x["roce_pct"], high_is_good=True)) / 2
    return x


def maheshwari_eligible(df: pd.DataFrame, as_of=None, *, near_high: dict | None = None) -> pd.DataFrame:
    """Every 20/20 name in an industry ranked within keep_industries by breadth, with its industry's
    rank (`ind_rank`): the selector buys only from the top buy_industries and keeps down to keep_industries."""
    spec = TRACKS["maheshwari"]
    need = ["industry", "sales_growth_5y_pct", "roe_pct", "market_cap_cr"]
    if df.empty or any(c not in df.columns for c in need):
        return df.iloc[0:0]
    x = df.dropna(subset=["industry"]).copy()
    if near_high is None:
        near_high = near_high_share(x["symbol"].dropna().tolist(), as_of, near_pct=spec["near_high_pct"])
    x["near_high"] = x["symbol"].map(near_high)
    priced = x.dropna(subset=["near_high"])
    ind = priced.groupby("industry").agg(members=("near_high", "size"), breadth=("near_high", "mean"))
    growth = x.groupby("industry")["sales_growth_5y_pct"].median()
    ind = ind[(ind["members"] >= spec["min_members"])
              & (growth.reindex(ind.index) >= spec["min_industry_sales_growth_5y"])]
    ind = ind.sort_values(["breadth", "members"], ascending=False)
    ind["ind_rank"] = range(1, len(ind) + 1)
    ind = ind[ind["ind_rank"] <= spec["keep_industries"]]
    x = x.merge(ind[["ind_rank", "breadth"]], left_on="industry", right_index=True, how="inner")
    x = x[(x["sales_growth_5y_pct"] >= 20) & (x["roe_pct"] >= 20)].dropna(subset=["market_cap_cr"]).copy()
    x["score"] = x["market_cap_cr"].astype(float)
    return x


def kedia_eligible(df: pd.DataFrame, as_of=None) -> pd.DataFrame:
    need = ["market_cap_cr", "first_trade", "sales_growth_5y_pct", "profit_growth_5y_pct", "debt_to_equity"]
    if df.empty or any(c not in df.columns for c in need):
        return df.iloc[0:0]
    x = df.dropna(subset=need)
    seasoned = pd.to_datetime(x["first_trade"]) <= pd.Timestamp(as_of) - pd.DateOffset(years=10)
    x = x[x["market_cap_cr"].between(500, 5000) & seasoned & (x["sales_growth_5y_pct"] >= 15)
          & (x["profit_growth_5y_pct"] >= 15) & (x["debt_to_equity"] <= 1) & _pledge_ok(x)].copy()
    if x.empty:
        return x
    x["score"] = _pct(x["sales_growth_5y_pct"], high_is_good=True)
    return x


def whiteoak_eligible(df: pd.DataFrame, as_of=None) -> pd.DataFrame:
    need = ["roce_5y_avg_pct", "roce_pct", "ev_to_ebit"]
    if df.empty or any(c not in df.columns for c in need):
        return df.iloc[0:0]
    x = df.dropna(subset=need)
    x = x[_pledge_ok(x) & ~x["has_flaw"].astype(bool) & (x["roce_5y_avg_pct"] >= 15) & (x["ev_to_ebit"] > 0)].copy()
    if x.empty:
        return x
    coc = TRACKS["whiteoak"]["assumed_cost_of_capital_pct"]
    x["score"] = (x["roce_pct"] - coc) / x["ev_to_ebit"]
    x["sector"] = x["sector"].fillna("unclassified")
    return x


SCORERS = {"quality": quality_eligible, "value": value_eligible, "marcellus": marcellus_eligible,
           "sageone": sageone_eligible, "maheshwari": maheshwari_eligible, "kedia": kedia_eligible,
           "whiteoak": whiteoak_eligible}


def score(track: str, inputs: pd.DataFrame, as_of) -> pd.DataFrame:
    if inputs.empty:
        return inputs
    if track in PARADIGM_TRACKS:
        return SCORERS[track](inputs, as_of)
    return SCORERS[track](inputs)


def select_holdings(ranked: pd.DataFrame, held: set[str], *, top_n: int, keep_within: int) -> pd.DataFrame:
    """Keep held names still ranked within `keep_within`, then fill to `top_n` with the
    best-ranked new names. `ranked` needs company_master_id and score."""
    r = ranked.sort_values("score", ascending=False).reset_index(drop=True)
    r["rank"] = r.index + 1
    keep = r[r["company_master_id"].isin(held) & (r["rank"] <= keep_within)]
    fresh = r[~r["company_master_id"].isin(held)]
    chosen = pd.concat([keep, fresh.head(max(top_n - len(keep), 0))])
    chosen = chosen.sort_values("rank").head(max(top_n, len(keep)))
    chosen["action"] = chosen["company_master_id"].isin(held).map({True: "hold", False: "enter"})
    chosen["weight"] = 1.0 / len(chosen) if len(chosen) else 0.0
    return chosen


def _finish(chosen: pd.DataFrame, held: set[str]) -> pd.DataFrame:
    chosen = chosen.copy()
    chosen["action"] = chosen["company_master_id"].isin(held).map({True: "hold", False: "enter"})
    chosen["weight"] = 1.0 / len(chosen) if len(chosen) else 0.0
    return chosen


def select_capped(ranked: pd.DataFrame, held: set[str], *, top_n: int, keep_within: int, max_per_group: int,
                  group_col: str) -> pd.DataFrame:
    """select_holdings with at most `max_per_group` names per `group_col` (missing = one group)."""
    r = ranked.sort_values("score", ascending=False).reset_index(drop=True)
    r["rank"] = r.index + 1
    groups = r[group_col].fillna("unclassified")
    count, picked = {}, []
    for take_held in (True, False):
        for i in r.index:
            cid = r.at[i, "company_master_id"]
            if len(picked) >= top_n or i in picked:
                continue
            if take_held and not (cid in held and r.at[i, "rank"] <= keep_within):
                continue
            if count.get(groups[i], 0) >= max_per_group:
                continue
            picked.append(i)
            count[groups[i]] = count.get(groups[i], 0) + 1
    return _finish(r.loc[sorted(picked)], held)


def select_maheshwari(ranked: pd.DataFrame, held: set[str], spec: dict) -> pd.DataFrame:
    """Held names still in a top-keep_industries industry and passing 20/20 stay; free slots go
    leader-first across the top buy_industries industries (largest passing company of each, then
    the second largest, ...), at most per_industry per industry."""
    r = ranked.copy()
    r["in_ind"] = r.groupby("industry")["score"].rank(ascending=False, method="first")
    keep = r[r["company_master_id"].isin(held)]
    count = keep.groupby("industry").size().to_dict()
    fresh = r[~r["company_master_id"].isin(held) & (r["ind_rank"] <= spec["buy_industries"])]
    fresh = fresh.sort_values(["in_ind", "ind_rank"])
    picked = list(keep.index)
    for i in fresh.index:
        if len(picked) >= spec["top_n"]:
            break
        ind = fresh.at[i, "industry"]
        if count.get(ind, 0) >= spec["per_industry"]:
            continue
        picked.append(i)
        count[ind] = count.get(ind, 0) + 1
    out = r.loc[picked].sort_values(["ind_rank", "in_ind"]).reset_index(drop=True)
    out["rank"] = out.index + 1
    return _finish(out, held)


def select_kedia(ranked: pd.DataFrame, held: set[str], spec: dict, inputs: pd.DataFrame) -> pd.DataFrame:
    """Held names stay whatever their rank or size unless they lost money in both of the last two
    years; free slots go to the best-ranked new names."""
    cur = inputs[inputs["company_master_id"].isin(held)].copy()
    cur = cur[~((cur["profit_y1"] < 0) & (cur["profit_y2"] < 0))]
    r = ranked.sort_values("score", ascending=False)
    fresh = r[~r["company_master_id"].isin(held)].head(max(spec["top_n"] - len(cur), 0))
    cur["score"] = cur["company_master_id"].map(ranked.set_index("company_master_id")["score"]) \
        if not ranked.empty else float("nan")
    out = pd.concat([cur, fresh]).reset_index(drop=True)
    out["rank"] = out.index + 1
    return _finish(out, held)


def sector_quotas(sizes: pd.Series, total: int) -> dict:
    """Slots per sector in proportion to its size, at least 1 each, largest remainders, summing
    to `total` (or to the number of sectors when that is larger)."""
    sizes = sizes[sizes > 0]
    if sizes.empty:
        return {}
    exact = sizes / sizes.sum() * total
    q = exact.astype(int).clip(lower=1)
    left = total - int(q.sum())
    order = (exact - exact.astype(int)).sort_values(ascending=False).index
    for s in order:
        if left <= 0:
            break
        if q[s] < sizes[s]:
            q[s] += 1
            left -= 1
    while left < 0:  # the at-least-1 floor overshot: take back from the largest quotas
        s = q.idxmax()
        q[s] -= 1
        left += 1
    return {k: int(min(v, sizes[k])) for k, v in q.items()}


def select_whiteoak(ranked: pd.DataFrame, held: set[str], spec: dict) -> pd.DataFrame:
    quotas = sector_quotas(ranked["sector"].value_counts(), spec["top_n"])
    parts = []
    for sec, n in quotas.items():
        g = ranked[ranked["sector"] == sec].sort_values("score", ascending=False).reset_index(drop=True)
        g["in_sec"] = g.index + 1
        keep = g[g["company_master_id"].isin(held) & (g["in_sec"] <= spec["keep_multiple"] * n)].head(n)
        fresh = g[~g["company_master_id"].isin(keep["company_master_id"])].head(n - len(keep))
        parts.append(pd.concat([keep, fresh]))
    out = pd.concat(parts).sort_values("score", ascending=False).reset_index(drop=True) if parts else ranked.iloc[0:0]
    out["rank"] = out.index + 1
    return _finish(out, held)


def select(track: str, ranked: pd.DataFrame, held: set[str], inputs: pd.DataFrame) -> pd.DataFrame:
    spec = TRACKS[track]
    if track == "sageone":
        return select_capped(ranked, held, top_n=spec["top_n"], keep_within=spec["keep_within"],
                             max_per_group=spec["max_per_industry"], group_col="industry")
    if track == "maheshwari":
        return select_maheshwari(ranked, held, spec)
    if track == "kedia":
        return select_kedia(ranked, held, spec, inputs)
    if track == "whiteoak":
        return select_whiteoak(ranked, held, spec)
    return select_holdings(ranked, held, top_n=spec["top_n"], keep_within=spec["keep_within"])


# --- Rebalance ---------------------------------------------------------------------------

def last_rebalance(track: str, version: int, *, include_reviews: bool = False):
    where = "" if include_reviews else " AND coalesce(event, 'rebalance') = 'rebalance'"
    df = sql_to_df(f"SELECT max(rebalance_date) AS d FROM {HOLDING_TABLE} WHERE track = %s AND version = %s{where}",
                   params=(track, version))
    d = df.iloc[0]["d"] if not df.empty else None
    return None if d is None or pd.isna(d) else pd.Timestamp(d).date()


def is_due(last, today, cadence: str = "monthly") -> bool:
    if last is None:
        return True
    return (today.year - last.year) * 12 + (today.month - last.month) >= CADENCE_MONTHS[cadence]


def _ref_prices(symbols: list[str], as_of) -> dict[str, tuple[float, object]]:
    if not symbols:
        return {}
    df = sql_to_df(
        """
        SELECT DISTINCT ON (symbol) symbol, adj_close, date::date AS d
          FROM advisory_adjusted_ohlcv_daily
         WHERE symbol = ANY(%s) AND date <= %s::date + interval '1 day' AND date >= %s::date - interval '10 days'
         ORDER BY symbol, date DESC
        """,
        params=(symbols, str(as_of), str(as_of)),
    )
    return {r.symbol: (float(r.adj_close), r.d) for r in df.itertuples()}


def _held(track: str, version: int, on) -> pd.DataFrame:
    if on is None:
        return pd.DataFrame(columns=["company_master_id", "symbol", "weight", "ref_price"])
    return sql_to_df(f"SELECT company_master_id, symbol, weight, ref_price FROM {HOLDING_TABLE} "
                     "WHERE track=%s AND version=%s AND rebalance_date=%s", params=(track, version, on))


def _rows(track: str, chosen: pd.DataFrame, as_of, event: str) -> pd.DataFrame:
    spec = TRACKS[track]
    prices = _ref_prices(chosen["symbol"].tolist(), pd.Timestamp(as_of).date())
    return pd.DataFrame({
        "track": track, "version": spec["version"], "rebalance_date": pd.Timestamp(as_of).date(),
        "company_master_id": chosen["company_master_id"], "symbol": chosen["symbol"],
        "rank": chosen["rank"].astype(int), "score": chosen["score"].astype(float), "weight": chosen["weight"],
        "action": chosen["action"], "ref_price": chosen["symbol"].map(lambda s: prices.get(s, (None, None))[0]),
        "ref_price_date": chosen["symbol"].map(lambda s: prices.get(s, (None, None))[1]),
        "snapshot_date": chosen["as_of_date"], "event": event,
    })


def choose(track: str, as_of, held: set[str] | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(eligible ranked set, chosen holdings) on `as_of` -- no writes."""
    inputs = load_paradigm_inputs(as_of) if track in PARADIGM_TRACKS else load_inputs(as_of)
    ranked = score(track, inputs, as_of)
    if ranked.empty:
        return ranked, ranked
    chosen = select(track, ranked, held or set(), inputs)
    chosen["symbol"] = (chosen["company_master_id"].map(_symbol) if track in PARADIGM_TRACKS
                        else chosen["company_master_id"].str.replace("nse:", "", regex=False))
    return ranked, chosen


def rebalance(track: str, as_of) -> dict[str, object]:
    spec = TRACKS[track]
    last = last_rebalance(track, spec["version"], include_reviews=True)
    held = set(_held(track, spec["version"], last)["company_master_id"])
    ranked, chosen = choose(track, as_of, held)
    if ranked.empty or chosen.empty:
        return {"track": track, "rebalanced": False, "reason": "no eligible companies (snapshot or universe missing)"}
    rows = _rows(track, chosen, as_of, "rebalance")
    upsert_to_db(rows, HOLDING_TABLE, unique_keys=["track", "version", "rebalance_date", "company_master_id"])
    return {"track": track, "rebalanced": True, "holdings": len(rows), "entered": int((rows["action"] == "enter").sum()),
            "kept": int((rows["action"] == "hold").sum()), "eligible": int(len(ranked)),
            "exited": sorted(held - set(rows["company_master_id"])), "missing_price": int(rows["ref_price"].isna().sum())}


def drifted_weights(prev: pd.DataFrame, prices: dict) -> pd.Series:
    """Each holding's share of the book now, from its weight and reference price at the last
    snapshot; cash (1 - sum of weights) earns nothing. A name without a price keeps its value."""
    grown = {r.company_master_id: r.weight * (prices[r.symbol][0] / r.ref_price
                                              if r.symbol in prices and r.ref_price else 1.0)
             for r in prev.itertuples()}
    total = sum(grown.values()) + max(0.0, 1 - float(prev["weight"].sum()))
    return pd.Series({k: v / total for k, v in grown.items()}) if total > 0 else pd.Series(dtype=float)


def review(track: str, as_of) -> dict[str, object]:
    """The monthly check between rebalances: holdings that no longer pass the rule are sold, their
    weight goes to cash, the rest keep their drifted weights. Written every month (event='review')
    so the record carries a monthly mark even when nothing is sold."""
    spec = TRACKS[track]
    last = last_rebalance(track, spec["version"], include_reviews=True)
    prev = _held(track, spec["version"], last)
    if prev.empty:
        return {"track": track, "reviewed": False, "reason": "nothing held"}
    inputs = load_paradigm_inputs(as_of)
    ranked = score(track, inputs, as_of)
    if inputs.empty:
        return {"track": track, "reviewed": False, "reason": "snapshot or universe missing"}
    passing = set(ranked["company_master_id"]) if not ranked.empty else set()
    prices = _ref_prices(prev["symbol"].tolist(), pd.Timestamp(as_of).date())
    w = drifted_weights(prev, prices)
    kept = prev[prev["company_master_id"].isin(passing)].copy()
    sold = sorted(set(prev["company_master_id"]) - passing)
    if kept.empty:
        return {"track": track, "reviewed": False, "reason": "every holding failed; left for the rebalance", "sold": sold}
    snap = inputs.set_index("company_master_id")["as_of_date"]
    sc = ranked.set_index("company_master_id")["score"] if not ranked.empty else pd.Series(dtype=float)
    kept["score"] = kept["company_master_id"].map(sc)
    kept = kept.sort_values("score", ascending=False).reset_index(drop=True)
    kept["rank"] = kept.index + 1
    kept["weight"] = kept["company_master_id"].map(w)
    kept["action"] = "hold"
    kept["as_of_date"] = kept["company_master_id"].map(snap)
    rows = _rows(track, kept, as_of, "review")
    upsert_to_db(rows, HOLDING_TABLE, unique_keys=["track", "version", "rebalance_date", "company_master_id"])
    return {"track": track, "reviewed": True, "holdings": len(rows), "sold": sold,
            "cash_weight": round(1 - float(rows["weight"].sum()), 4)}


def run_due(today=None) -> list[dict]:
    ensure_tables()
    freeze_specs()
    today = today or pd.Timestamp.now(tz="Asia/Kolkata").date()
    results = []
    for t, s in TRACKS.items():
        cadence = s.get("rebalance", "monthly")
        if today < pd.Timestamp(s["start"]).date():
            results.append({"track": t, "rebalanced": False, "reason": f"record starts {s['start']}"})
        elif is_due(last_rebalance(t, s["version"]), today, cadence):
            results.append(rebalance(t, today))
        elif s.get("review") and is_due(last_rebalance(t, s["version"], include_reviews=True), today, "monthly"):
            results.append(review(t, today))
        else:
            results.append({"track": t, "rebalanced": False, "reason": f"not due ({cadence})"})
    return results


# --- Report ------------------------------------------------------------------------------

def _today():
    return pd.Timestamp.now(tz="Asia/Kolkata").date()


def _period_return(held: pd.DataFrame, end) -> tuple[float | None, int]:
    """Buy-and-hold return of one snapshot to `end`: sum of weight x price change; cash earns
    nothing. Names without a price at `end` are left out and the rest scaled to the same invested
    share (for quality/value -- weights summing to 1 -- this is the plain equal-weight mean)."""
    held = held.dropna(subset=["ref_price"])
    if held.empty:
        return None, 0
    now = _ref_prices(held["symbol"].tolist(), end)
    priced = held[held["symbol"].isin(now)]
    if priced.empty:
        return None, 0
    w = priced["weight"].astype(float)
    r = priced.apply(lambda x: now[x["symbol"]][0] / x["ref_price"] - 1, axis=1)
    invested = float(held["weight"].astype(float).sum())
    return float((w * r).sum() / w.sum() * invested), len(priced)


def report(track: str) -> dict[str, object]:
    """Return of each holding period, from its reference prices to the next snapshot (or today)."""
    spec = TRACKS[track]
    h = sql_to_df(f"SELECT * FROM {HOLDING_TABLE} WHERE track=%s AND version=%s ORDER BY rebalance_date",
                  params=(track, spec["version"]))
    if h.empty:
        return {"track": track, "periods": []}
    dates = sorted(h["rebalance_date"].unique())
    periods = []
    for i, start in enumerate(dates):
        end = dates[i + 1] if i + 1 < len(dates) else _today()
        ret, n = _period_return(h[h["rebalance_date"] == start], end)
        periods.append({"start": str(start), "end": str(end), "names": n,
                        "return_pct": round(100 * ret, 2) if ret is not None else None})
    return {"track": track, "version": spec["version"], "periods": periods}


_BENCH_CACHE: dict = {}


def universe_return(start, end) -> tuple[float | None, int]:
    """Equal-weight return of the operating universe as it stood on `start`, to `end`."""
    key = (str(start), str(end))
    if key not in _BENCH_CACHE:
        u = load_inputs(start)
        syms = sorted({_symbol(c) for c in u["company_master_id"].dropna()}) if not u.empty else []
        p0, p1 = _ref_prices(syms, start), _ref_prices(syms, end)
        r = [p1[s][0] / p0[s][0] - 1 for s in syms if s in p0 and s in p1 and p0[s][0]]
        _BENCH_CACHE[key] = (sum(r) / len(r) if r else None, len(r))
    return _BENCH_CACHE[key]


def track_summary(track: str, today=None) -> dict[str, object]:
    """Everything the /paradigms page shows for one track: the frozen spec (paradigm,
    approximations), the latest holdings with return since entry, the chained return since the
    start, and the equal-weight universe over the same days."""
    spec = TRACKS[track]
    today = today or _today()
    start = pd.Timestamp(spec["start"]).date()
    out = {"track": track, "version": spec["version"], "start": spec["start"], "rebalance": spec.get("rebalance"),
           "paradigm": spec.get("paradigm"), "approximations": spec.get("approximations"), "spec": spec,
           "as_of": str(today), "status": "waiting" if today < start else "live", "holdings_as_of": None,
           "holdings": [], "cash_weight": None, "periods": [], "return_since_start_pct": None,
           "benchmark": {"name": "equal-weight operating universe at the start", "return_since_start_pct": None,
                         "names": 0}}
    h = sql_to_df(f"SELECT * FROM {HOLDING_TABLE} WHERE track=%s AND version=%s ORDER BY rebalance_date",
                  params=(track, spec["version"]))
    if h.empty:
        return out
    rep = report(track)
    growth = 1.0
    for p in rep["periods"]:
        if p["return_pct"] is not None:
            growth *= 1 + p["return_pct"] / 100
    out["periods"] = rep["periods"]
    out["return_since_start_pct"] = round(100 * (growth - 1), 2)
    first = min(h["rebalance_date"])
    bench, n = universe_return(first, today)
    out["benchmark"].update({"return_since_start_pct": round(100 * bench, 2) if bench is not None else None,
                             "names": n, "from": str(first)})
    dates = sorted(h["rebalance_date"].unique())
    last = h[h["rebalance_date"] == dates[-1]]
    now = _ref_prices(last["symbol"].dropna().tolist(), today)
    present = {d: set(h.loc[h["rebalance_date"] == d, "company_master_id"]) for d in dates}
    holdings = []
    for r in last.sort_values("rank").itertuples():
        k = len(dates) - 1
        while k > 0 and r.company_master_id in present[dates[k - 1]]:
            k -= 1
        entry = h[(h["rebalance_date"] == dates[k]) & (h["company_master_id"] == r.company_master_id)].iloc[0]
        latest = now.get(r.symbol)
        ep = entry["ref_price"]
        holdings.append({
            "symbol": r.symbol, "company_master_id": r.company_master_id, "rank": int(r.rank) if pd.notna(r.rank) else None,
            "score": None if pd.isna(r.score) else float(r.score), "weight": float(r.weight), "action": r.action,
            "entry_date": str(dates[k]), "entry_price": None if pd.isna(ep) else float(ep),
            "ref_price": None if pd.isna(r.ref_price) else float(r.ref_price),
            "ref_price_date": None if pd.isna(r.ref_price_date) else str(r.ref_price_date),
            "latest_price": latest[0] if latest else None, "latest_price_date": str(latest[1]) if latest else None,
            "return_since_entry_pct": round(100 * (latest[0] / ep - 1), 2) if latest and pd.notna(ep) and ep else None,
        })
    out.update({"holdings_as_of": str(dates[-1]), "holdings": holdings,
                "cash_weight": round(max(0.0, 1 - float(last["weight"].sum())), 4)})
    return out


def dry_run(as_of=None) -> list[dict]:
    """What each paradigm track would hold on `as_of` from scratch -- reads only, writes nothing."""
    as_of = as_of or _today()
    out = []
    for t in PARADIGM_TRACKS:
        ranked, chosen = choose(t, as_of)
        out.append({"track": t, "as_of": str(as_of), "eligible": int(len(ranked)), "would_hold": int(len(chosen)),
                    "symbols": chosen["symbol"].tolist() if not chosen.empty else []})
    return out


def main(argv: list[str] | None = None) -> int:
    global STOCKEY_RUN_STATE
    parser = argparse.ArgumentParser(description="Fundamental forward records (quality, value, paradigm tracks).")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="paradigm tracks' would-be holdings; writes nothing")
    parser.add_argument("--as-of", help="YYYY-MM-DD for --dry-run (default: today, IST)")
    args = parser.parse_args(argv)
    if args.report:
        print(json.dumps([report(t) for t in TRACKS], default=str, indent=2), flush=True)
        return 0
    if args.dry_run:
        print(json.dumps(dry_run(pd.Timestamp(args.as_of).date() if args.as_of else None), default=str, indent=2),
              flush=True)
        return 0
    results = run_due()
    rebalanced = [r for r in results if r.get("rebalanced") or r.get("reviewed")]
    STOCKEY_RUN_STATE = {"source": SYNC_SOURCE_NAME, "rows": sum(r.get("holdings", 0) for r in rebalanced),
                         "rows_written": sum(r.get("holdings", 0) for r in rebalanced), "tracks": results,
                         "fallback_used": any(not r.get("rebalanced") and "no eligible" in str(r.get("reason")) for r in results),
                         "state_advanced": bool(rebalanced)}
    print(json.dumps({"status": "ok", **STOCKEY_RUN_STATE}, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
