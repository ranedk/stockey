"""Story-and-flaw score, per company per day (docs/FUNDAMENTAL_REEVALUATION_PRD.md 3.1-3.2,
build step 2; 2026-09-29).

Markets pay for one story and punish one flaw; an average across many dimensions hides both.
So each company in the universe gets:
  - READINGS per dimension, each a percentile within its universe group (lenders ranked with
    lenders): a LEVEL and, where the data allows, a CHANGE / ACCELERATION reading -- change
    re-rates stocks more than level;
  - a dimension's STRENGTH = its best reading. The STORY SCORE asks how exceptional the
    company's best reading is GIVEN how many chances it had: with n readings, the chance that
    none exceeds percentile p by luck is p^n, so the score is 100 x p_best^n (plus a small
    bonus, STORY_SECOND_BONUS x the same for the second dimension). A plain "best of n" is
    ~94th percentile for almost everyone -- measured on the first run (median 94.7), it rated
    nearly every company a standout;
  - FLAWS -- deal-breakers that cap the score at FLAW_CAP whatever the stories: cash burn with
    losses, leverage extreme for the group, governance flags, persistent losses.
Stored dated (fundamentals_story_score), with every reading, so the score is point-in-time
and later testable. Which dimensions count as stories and flaws, and how strong a reading must
be, are DEFAULTS here; PRD 3.3 replaces them with what price reactions show the market pays for.

Inputs, all read as of the scoring date: the dated snapshot (C4), the universe run and its
groups, L2 state (ownership, debt trend, valuation vs own history), alerts DETECTED by then,
and the sector cycle.

    python -m fundamentals.screens.story_score                   # today
    python -m fundamentals.screens.story_score --as-of 2026-10-15
"""

from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

from fundamentals.screens import results_reading
from utils.company_master import map_company_master_ids_nse_or_bse
from utils.db import sql_to_df, upsert_to_db

SYNC_SOURCE_NAME = "fundamentals.screens.story_score"
RESULTS_TABLE = "fundamentals_story_score"
# 2 (2026-09-29, PRD step 3): growth change/acceleration and a new margins dimension read from
# the quarterly results table against each company's own trend, one-offs stripped; version 1's
# snapshot-based "quarter YoY minus 5y" acceleration is gone.
SCORE_VERSION = 2
STOCKEY_RUN_STATE: dict[str, object] = {}

STORY_SECOND_BONUS = 0.25
FLAW_CAP = 40.0
EVENT_LOOKBACK_DAYS = 90
EVENT_HALF_LIFE_DAYS = 30
# News (news_tagging.py): only directional tags of materiality >= NEWS_MIN_MATERIALITY count;
# a materiality-3 item weighs like one filing alert, a 5 like 5/3 of one.
NEWS_MIN_MATERIALITY = 3
# A sector's decayed net news at or above this is a tailwind story for its companies.
SECTOR_NEWS_TAILWIND = 1.5
FINANCIAL_GROUPS = frozenset({"lender", "other_financial"})

POSITIVE_TRIGGERS = frozenset({"rating_confirms_deleveraging", "results_confirm_turnaround", "insider_buy",
                               "bulk_deal_buy", "capital_raise", "institutional_first_entry"})
NEGATIVE_TRIGGERS = frozenset({"rating_downgrade", "results_decline", "insider_sell_surprise",
                               "pledge_increase", "bulk_deal_sell", "results_delayed"})

# (dimension, reading, source column, higher_is_better, kind). Percentile within group.
PERCENTILE_READINGS = [
    ("growth", "sales_growth_5y", "sales_growth_5y_pct", True, "level"),
    ("growth", "profit_growth_5y", "profit_growth_5y_pct", True, "level"),
    # results reading (results_reading.py): the latest quarter against the company's own trend
    ("growth", "sales_yoy", "rr_sales_yoy_pct", True, "change"),
    ("growth", "sales_vs_trend", "rr_sales_vs_trend_pp", True, "acceleration"),
    ("growth", "profit_yoy", "rr_profit_yoy_pct", True, "change"),
    ("growth", "profit_vs_trend", "rr_profit_vs_trend_pp", True, "acceleration"),
    ("margins", "margin_change", "rr_margin_change_pp", True, "change"),
    ("margins", "margin_vs_trend", "rr_margin_vs_trend_pp", True, "acceleration"),
    # Levels on the 5-year averages: a single freak year (a demerger or asset-sale gain) must
    # not be a company's whole story. The latest year counts only as improvement (change).
    ("profitability", "roce_5y", "roce_5y_avg_pct", True, "level"),
    ("profitability", "roe_5y", "roe_5y_avg_pct", True, "level"),
    ("profitability", "roce_improvement", "roce_improvement", True, "change"),
    ("profitability", "roe_improvement", "roe_improvement", True, "change"),
    ("balance_sheet", "low_debt", "debt_to_equity", False, "level"),
    ("balance_sheet", "interest_cover", "interest_cover", True, "level"),
    ("balance_sheet", "fcf_yield", "fcf_yield", True, "level"),
    ("value", "earnings_yield", "earnings_yield_pct", True, "level"),
    ("value", "low_ev_ebit", "ev_to_ebit_pos", False, "level"),
    ("value", "low_price_to_book", "price_to_book", False, "level"),
    ("value", "cheap_vs_own_history", "valuation_vs_own_history_ratio", False, "change"),
    ("events", "net_events", "event_net", True, "change"),
]
# Readings that do not describe a lender's business.
NOT_FOR_FINANCIALS = frozenset({"low_debt", "interest_cover", "fcf_yield", "low_ev_ebit"})
# Readings that only describe a lender's business: for everyone else a low price-to-book
# mostly marks distress, not a bargain.
ONLY_FOR_FINANCIALS = frozenset({"low_price_to_book"})
# Returns above this are almost always one-off gains or a near-zero base, not a business.
PLAUSIBLE_RETURN_PCT = 100.0
# A quarter's YoY reading off less revenue than this (Rs cr, about the universe's 5th pct) is
# lumpy licence/order income, not acceleration (SPARC: +314% on Rs 40 cr).
MIN_ACCELERATION_SALES_Q_CR = 50.0


# --- inputs ------------------------------------------------------------------------------

def load_inputs(as_of) -> pd.DataFrame:
    d = str(pd.Timestamp(as_of).date())
    universe = sql_to_df(
        """
        SELECT company_id AS screener_company_id, ticker, metrics_json
          FROM fundamentals_l1_universe
         WHERE run_date = (SELECT max(run_date) FROM fundamentals_l1_universe WHERE run_date::date <= %s)
        """, params=(d,))
    if universe.empty:
        return universe
    meta = universe["metrics_json"].map(lambda m: json.loads(m) if m else {})
    universe["group"] = meta.map(lambda m: m.get("universe_group"))
    universe["layer2_allowed_by"] = meta.map(lambda m: m.get("layer2_allowed_by"))
    universe = universe.drop(columns=["metrics_json"])
    universe["company_master_id"] = map_company_master_ids_nse_or_bse(universe["ticker"].astype("string")).to_numpy()
    snap = sql_to_df(
        """
        SELECT DISTINCT ON (screener_company_id) *
          FROM fundamentals_snapshot_daily
         WHERE as_of_date <= %s AND as_of_date >= %s::date - 10
         ORDER BY screener_company_id, as_of_date DESC
        """, params=(d, d))
    df = universe.merge(snap.drop(columns=["company_master_id"], errors="ignore"), on="screener_company_id", how="left")
    l2 = sql_to_df(
        """
        SELECT DISTINCT ON (company_id) company_id AS screener_company_id, promoter_stake_direction,
               institutional_stake_direction, institutional_first_entry, pledge_pct_trend_direction,
               net_debt_trend_direction, valuation_vs_own_history_ratio
          FROM fundamentals_l2_state WHERE run_date::date <= %s
         ORDER BY company_id, run_date DESC, state_vector_version DESC
        """, params=(d,))
    df = df.merge(l2, on="screener_company_id", how="left")
    alerts = sql_to_df(
        """
        SELECT a.company_master_id, a.trigger_type, e.detected_at
          FROM fundamentals_l3_alerts a
          JOIN fundamentals_events e ON e.source = a.source AND e.news_id = a.news_id
         WHERE e.detected_at < %s::date + 1 AND e.detected_at >= %s::date - %s
        """, params=(d, d, 400))
    df = df.merge(event_features(alerts, as_of), on="company_master_id", how="left")
    news = sql_to_df(
        """
        SELECT target_type, target_id, direction, materiality, first_seen_at
          FROM fundamentals_news_tag
         WHERE first_seen_at < %s::date + 1 AND first_seen_at >= %s::date - %s
           AND materiality >= %s AND direction IN ('positive', 'negative')
        """, params=(d, d, EVENT_LOOKBACK_DAYS, NEWS_MIN_MATERIALITY)) if _table_exists("fundamentals_news_tag") else pd.DataFrame()
    company_news, sector_news = news_features(news, as_of)
    df = df.merge(company_news, on="company_master_id", how="left")
    df["event_net"] = df[["event_net", "news_net"]].sum(axis=1, min_count=1)
    sectors = sql_to_df(
        """
        SELECT cs.company_master_id, cs.sector_code, sc.phase
          FROM fundamentals_company_sector cs
          LEFT JOIN LATERAL (SELECT phase FROM fundamentals_sector_cycle s
                              WHERE s.sector_code = cs.sector_code AND s.run_date::date <= %s
                              ORDER BY s.run_date DESC LIMIT 1) sc ON true
        """, params=(d,))
    sectors = sectors.merge(sector_news, on="sector_code", how="left") if "sector_code" in sectors else sectors
    df = df.merge(sectors.drop(columns=["sector_code"], errors="ignore"), on="company_master_id", how="left")
    rr = results_reading.readings(as_of)
    if not rr.empty:
        rr = rr.drop(columns=["ticker"]).rename(columns=lambda c: c if c == "company_id" else f"rr_{c}")
        df["screener_company_id"] = pd.to_numeric(df["screener_company_id"], errors="coerce")
        df = df.merge(rr.rename(columns={"company_id": "screener_company_id"}), on="screener_company_id", how="left")
    return df


def _table_exists(name: str) -> bool:
    return not sql_to_df("SELECT 1 FROM information_schema.tables WHERE table_name = %s", params=(name,)).empty


def news_features(news: pd.DataFrame, as_of) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Decayed net of material, directional news: per company (joins the events dimension)
    and per sector (a tailwind story for the sector's companies)."""
    empty = (pd.DataFrame(columns=["company_master_id", "news_net"]), pd.DataFrame(columns=["sector_code", "sector_news_net"]))
    if news.empty:
        return empty
    n = news.copy()
    now = pd.Timestamp(str(pd.Timestamp(as_of).date()), tz="Asia/Kolkata") + pd.Timedelta(days=1)
    age = (now - pd.to_datetime(n["first_seen_at"], utc=True)).dt.total_seconds() / 86400
    sign = np.where(n["direction"] == "positive", 1.0, -1.0)
    n["w"] = sign * (n["materiality"].astype(float) / NEWS_MIN_MATERIALITY) * 0.5 ** (age / EVENT_HALF_LIFE_DAYS)
    comp = n[n["target_type"] == "company"].groupby("target_id")["w"].sum()
    sect = n[n["target_type"] == "sector"].groupby("target_id")["w"].sum()
    return (pd.DataFrame({"company_master_id": comp.index, "news_net": comp.to_numpy()}),
            pd.DataFrame({"sector_code": sect.index, "sector_news_net": sect.to_numpy()}))


def event_features(alerts: pd.DataFrame, as_of) -> pd.DataFrame:
    """Per company: a decayed net event score over EVENT_LOOKBACK_DAYS, and governance flags."""
    if alerts.empty:
        return pd.DataFrame(columns=["company_master_id", "event_net", "gov_auditor", "gov_rpt", "gov_pledge_rise"])
    a = alerts.copy()
    now = pd.Timestamp(str(pd.Timestamp(as_of).date()), tz="Asia/Kolkata") + pd.Timedelta(days=1)
    a["age"] = (now - pd.to_datetime(a["detected_at"], utc=True)).dt.total_seconds() / 86400
    sign = a["trigger_type"].map(lambda t: 1 if t in POSITIVE_TRIGGERS else (-1 if t in NEGATIVE_TRIGGERS else 0))
    recent = a["age"] <= EVENT_LOOKBACK_DAYS
    a["w"] = np.where(recent, sign * 0.5 ** (a["age"] / EVENT_HALF_LIFE_DAYS), 0.0)
    g = a.groupby("company_master_id")
    out = pd.DataFrame({
        "event_net": g["w"].sum(),
        "gov_auditor": g.apply(lambda x: bool(((x.trigger_type == "auditor_change") & (x.age <= 365)).any()), include_groups=False),
        "gov_rpt": g.apply(lambda x: bool(((x.trigger_type == "related_party_transaction") & (x.age <= 365)).any()), include_groups=False),
        "gov_pledge_rise": g.apply(lambda x: bool(((x.trigger_type == "pledge_increase") & (x.age <= 180)).any()), include_groups=False),
    }).reset_index()
    out.loc[out["event_net"] == 0, "event_net"] = np.nan  # no recent events: unknown, not neutral
    return out


# --- scoring -----------------------------------------------------------------------------

def derive(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    num = lambda c: pd.to_numeric(x.get(c), errors="coerce")
    base_ok = num("rr_sales_q_cr") >= MIN_ACCELERATION_SALES_Q_CR
    # margins too: an operating margin on near-zero sales swings by hundreds of points
    # (SPARC -400% -> +57% on Rs 40 cr; RPOWER -3,205 pp on Rs 0.2 cr)
    for c in ("rr_sales_yoy_pct", "rr_sales_vs_trend_pp", "rr_profit_yoy_pct", "rr_profit_vs_trend_pp",
              "rr_margin_change_pp", "rr_margin_vs_trend_pp"):
        x[c] = num(c).where(base_ok)
    plausible = lambda c: num(c).abs() <= PLAUSIBLE_RETURN_PCT
    for now, avg in (("roce_pct", "roce_5y_avg_pct"), ("roe_pct", "roe_5y_avg_pct")):
        # the year that makes today's return implausible also sits inside the 5-year average
        # (RAYMOND: 168% ROE from a one-off sale lifts its 5y average to 42%), so drop both
        ok_now = plausible(now) | num(now).isna()
        x[avg] = num(avg).where(plausible(avg) & ok_now)
        x[now] = num(now).where(plausible(now))
    x["roce_improvement"] = x["roce_pct"] - x["roce_5y_avg_pct"]
    x["roe_improvement"] = x["roe_pct"] - x["roe_5y_avg_pct"]
    mcap = num("market_cap_cr")
    x["fcf_yield"] = num("fcf_3y_total") / 3 / mcap.where(mcap > 0)
    ev = num("ev_to_ebit")
    x["ev_to_ebit_pos"] = ev.where(ev > 0)
    return x


def _categorical(row) -> dict[str, float]:
    """Fixed strengths (0-100) for signals that have no cross-sectional scale."""
    out = {}
    promoter, inst = row.get("promoter_stake_direction"), row.get("institutional_stake_direction")
    if isinstance(inst, str) and "increas" in inst:
        out["institutional_buying"] = 85.0
    if row.get("institutional_first_entry") is True:
        out["institutional_first_entry"] = 95.0
    if isinstance(promoter, str) and "increas" in promoter:
        out["promoter_buying"] = 85.0
    pledge = row.get("pledge_pct_trend_direction")
    if isinstance(pledge, str) and ("decreas" in pledge or "decline" in pledge):
        out["pledge_falling"] = 70.0
    ownership = {k: v for k, v in out.items()}
    sector = {}
    if row.get("phase") == "capacity_discipline":
        sector["capacity_discipline"] = 80.0
    sector_news = row.get("sector_news_net")
    if pd.notna(sector_news) and sector_news >= SECTOR_NEWS_TAILWIND:
        sector["sector_news_tailwind"] = 75.0
    debt = row.get("net_debt_trend_direction")
    balance = {}
    if isinstance(debt, str) and "decline" in debt and row.get("group") not in FINANCIAL_GROUPS:
        balance["deleveraging"] = 80.0
    return {"ownership": ownership, "sector": sector, "balance_sheet": balance}


def flaws_for(row, leverage_p90: float | None) -> list[str]:
    f = []
    ocf3, p1, p2 = row.get("ocf_3y_total"), row.get("profit_y1"), row.get("profit_y2")
    # A company Layer 2 let in through its scaling-growth or turnaround allow-rule (operator,
    # 2026-09-25) already had its losses judged and accepted; the score must not overrule that.
    losses_accepted = row.get("layer2_allowed_by") in ("scaling_growth", "turnaround")
    if not losses_accepted and pd.notna(ocf3) and pd.notna(p1) and ocf3 < 0 and p1 < 0:
        f.append("cash burn with losses")
    if not losses_accepted and pd.notna(p1) and pd.notna(p2) and p1 < 0 and p2 < 0:
        f.append("loss-making both of the last 2 years")
    de, ic = row.get("debt_to_equity"), row.get("interest_cover")
    if (row.get("group") not in FINANCIAL_GROUPS and leverage_p90 is not None and pd.notna(de) and de >= leverage_p90
            and pd.notna(ic) and ic < 2):
        f.append("leverage extreme for its group with thin interest cover")
    if row.get("gov_auditor") is True:
        f.append("auditor change in the last year")
    if row.get("gov_rpt") is True:
        f.append("material related-party transaction in the last year")
    pledge = row.get("promoter_pledged_pct")
    if pd.notna(pledge) and pledge >= 50:
        f.append("promoter pledge >= 50%")
    if row.get("gov_pledge_rise") is True:
        f.append("pledge increased in the last 6 months")
    return f


def standout(pct: float, n: int) -> float:
    """100 x (pct/100)^n: how exceptional a best-of-n reading is, net of luck."""
    return 100.0 * (max(min(pct, 100.0), 0.0) / 100.0) ** max(n, 1)


def score(inputs: pd.DataFrame) -> pd.DataFrame:
    df = derive(inputs)
    rows = []
    for group, g in df.groupby(df["group"].fillna("unlabelled")):
        pct = {}
        for dim, name, col, hib, kind in PERCENTILE_READINGS:
            if group in FINANCIAL_GROUPS and name in NOT_FOR_FINANCIALS:
                continue
            if group not in FINANCIAL_GROUPS and name in ONLY_FOR_FINANCIALS:
                continue
            v = pd.to_numeric(g.get(col), errors="coerce")
            if v is None or v.notna().sum() < 5:
                continue
            # Midpoint percentile, (rank - 1/2) / N: the best of N reads 100 x (1 - 1/2N), never
            # exactly 100 -- topping a 90-name lender group is less surprising than topping 1,000
            # operating names, and a flat 100 let every reading's leader score 100 (103 names).
            n_ok = v.notna().sum()
            rank = v.rank() if hib else v.rank(ascending=False)
            pct[(dim, name, kind)] = (rank - 0.5) / n_ok * 100
        de = pd.to_numeric(g.get("debt_to_equity"), errors="coerce")
        lev_p90 = float(de.quantile(0.9)) if de.notna().sum() >= 5 else None
        for idx, row in g.iterrows():
            readings: dict[str, dict] = {}
            for (dim, name, kind), series in pct.items():
                val = series.get(idx)
                if pd.notna(val):
                    readings.setdefault(dim, {})[name] = {"pct": round(float(val), 1), "kind": kind}
            for dim, items in _categorical(row).items():
                for name, strength in items.items():
                    readings.setdefault(dim, {})[name] = {"pct": strength, "kind": "change"}
            strengths = {dim: max(r.values(), key=lambda z: z["pct"]) | {"reading": max(r, key=lambda k: r[k]["pct"])}
                         for dim, r in readings.items() if r}
            ranked = sorted(strengths.items(), key=lambda kv: -kv[1]["pct"])
            story, primary, second = None, None, None
            n = sum(len(r) for r in readings.values())
            if ranked:
                primary = ranked[0]
                story = standout(primary[1]["pct"], n)
                if len(ranked) > 1:
                    second = ranked[1]
                    # a second story closes a share of the REMAINING gap: adding it on top pushed
                    # 86 names into a clamp at 100 and erased the ordering among the best
                    story += (100.0 - story) * STORY_SECOND_BONUS * standout(second[1]["pct"], n) / 100.0
            flaws = flaws_for(row, lev_p90)
            capped = story is not None and bool(flaws) and story > FLAW_CAP
            rows.append({
                "company_master_id": row.get("company_master_id"), "symbol": row.get("ticker"), "group": group,
                "story_score": None if story is None else round(min(story, FLAW_CAP) if flaws else story, 1),
                "story_score_uncapped": None if story is None else round(story, 1),
                "primary_dimension": primary[0] if primary else None,
                "primary_reading": primary[1]["reading"] if primary else None,
                "primary_kind": primary[1]["kind"] if primary else None,
                "primary_pct": primary[1]["pct"] if primary else None,
                "second_dimension": second[0] if second else None,
                "second_reading": second[1]["reading"] if second else None,
                "second_pct": second[1]["pct"] if second else None,
                "readings_count": n,
                "flaws": "; ".join(flaws) if flaws else None, "flaw_capped": capped,
                "readings_json": json.dumps(readings, sort_keys=True),
            })
    return pd.DataFrame(rows)


# Slim, dated read of the daily score for systrader's fundamentals-filtered paper track
# (systrade docs/strategies/2026-10-01_stage2_rs_leaders_clean.md). A VIEW with a `date`
# column so systrader's incremental sync can copy it; stockey owns it, systrader only reads.
FILTER_VIEW = "fundamentals_story_filter_daily"
_FILTER_VIEW_SQL = f"""
    CREATE OR REPLACE VIEW {FILTER_VIEW} AS
    SELECT s.as_of_date AS date, s.company_master_id, cm.nse_ticker AS symbol, s.story_score,
           (s.flaws IS NOT NULL AND btrim(s.flaws) <> '') AS has_flaw, s.score_version
      FROM {RESULTS_TABLE} s
      LEFT JOIN company_master cm ON cm.company_master_id = s.company_master_id
"""


def ensure_filter_view() -> None:
    from utils.db import db_session

    with db_session() as (_, cur):
        cur.execute(_FILTER_VIEW_SQL)


def run(as_of=None, *, store: bool = True) -> pd.DataFrame:
    as_of = pd.Timestamp(as_of).date() if as_of else pd.Timestamp.now(tz="Asia/Kolkata").date()
    inputs = load_inputs(as_of)
    if inputs.empty:
        return pd.DataFrame()
    out = score(inputs).dropna(subset=["company_master_id"])
    out["as_of_date"] = as_of
    out["score_version"] = SCORE_VERSION
    out["load_ts"] = pd.Timestamp.now(tz="UTC")
    if store and not out.empty:
        upsert_to_db(out, RESULTS_TABLE, unique_keys=["as_of_date", "company_master_id", "score_version"])
        # the nightly full run refreshes every company's live score and records what moved
        from fundamentals.screens import reeval
        reeval.apply_scores(out, causes="daily", score_version=SCORE_VERSION)
        ensure_filter_view()
        from utils.schema_migrations import apply_schema_migration
        apply_schema_migration(
            migration_id="20260929_fundamentals_story_score_as_of_date_date",
            description="fundamentals_story_score.as_of_date TEXT -> DATE.", owner=SYNC_SOURCE_NAME,
            metadata={"tables": [RESULTS_TABLE]},
            statements=[f"ALTER TABLE {RESULTS_TABLE} ALTER COLUMN as_of_date TYPE DATE USING as_of_date::date"],
        )
    return out


def main(argv: list[str] | None = None) -> int:
    global STOCKEY_RUN_STATE
    parser = argparse.ArgumentParser(description="Story-and-flaw score (reevaluation PRD step 2).")
    parser.add_argument("--as-of", help="score as of this date (default: today, IST)")
    args = parser.parse_args(argv)
    out = run(args.as_of)
    STOCKEY_RUN_STATE = {"source": SYNC_SOURCE_NAME, "rows": len(out), "rows_written": len(out),
                         "scored": int(out["story_score"].notna().sum()) if not out.empty else 0,
                         "flawed": int(out["flaws"].notna().sum()) if not out.empty else 0,
                         "fallback_used": out.empty, "state_advanced": not out.empty}
    print(json.dumps({"status": "ok", **STOCKEY_RUN_STATE}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
