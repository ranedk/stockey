"""Mechanical portfolio ruleset -- the deterministic half of docs/PORTFOLIO_RULESET_PRD.md.

This module DECIDES NOTHING on its own judgement. It evaluates a fixed, versioned rule
over the watchlist and returns candidates; an LLM adjudicator (portfolio_adjudicator.py)
may then REJECT any of them, but can never add one. The ruleset is the upper bound on
what may enter the portfolio.

Everything here is pure and reproducible: same inputs, same RULESET_VERSION, same output.
That is the point. Per the PRD, attribution is already hard with a fixed rule and becomes
impossible if the decision process drifts between decisions -- so the rule is a fixed
object that a small, noisy sample can slowly say something about.

BOUNDARY (CLAUDE.md, revised 2026-09-04): stockey owns fundamental signals; systrader
owns price-derived ones. The stage read comes from systrader's API rather than being
recomputed here. The volatility used for stop sizing is NOT a signal -- it never selects
or rejects a candidate, it only scales a risk parameter for a name already chosen.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import pandas as pd

from fundamentals.screens.portfolio_adjudicator import numeric_l2_metrics
from utils.db import sql_to_df

SYNC_SOURCE_NAME = "fundamentals.screens.portfolio_ruleset"

# Bump on ANY change to the rule below. Positions record it, so a later review can ask
# "how did v1 do" without the answer being contaminated by v2's behaviour.
RULESET_VERSION = 1

SYSTRADER_API = os.getenv("SYSTRADER_API_BASE", "http://127.0.0.1:8090")

# --- stop-loss band (operator-set 2026-09-04) --------------------------------------
# The operator specified "5-15% or so". Held as a BAND rather than a single number,
# because a flat percentage means very different things across this universe: measured
# on the 2026-09-04 candidates, HESTERBIO's ordinary 10-day noise is +/-12.2% (3.87%
# daily vol) while SOFTTECH's is +/-5.8%. A flat 5% stop on the first is hit by noise
# within days; a flat 15% on the second is ~2.6 sigma and would essentially never
# trigger. Same number, opposite meanings.
#
# So the stop is scaled by the name's own realised volatility and then CLAMPED into the
# operator's band. Every position gets roughly the same probability of a noise-triggered
# exit, which is what a stop is for.
STOP_PCT_MIN = float(os.getenv("PORTFOLIO_STOP_PCT_MIN", "5"))
STOP_PCT_MAX = float(os.getenv("PORTFOLIO_STOP_PCT_MAX", "15"))
# Multiple of the 10-day one-sigma move. 1.5 puts the stop outside ordinary noise
# without being so wide it stops being a stop.
STOP_VOL_MULTIPLE = float(os.getenv("PORTFOLIO_STOP_VOL_MULTIPLE", "1.5"))
VOL_LOOKBACK_DAYS = int(os.getenv("PORTFOLIO_STOP_VOL_LOOKBACK_DAYS", "60"))

ENTRY_STAGE = 2  # Weinstein stage 2 = advancing

# How stale systrader's stage read may be before it stops counting as a read at all.
#
# REVIEW FINDING, 2026-09-05. The entry rule was fail-closed on a MISSING stage read but
# had NO protection against a STALE one -- and a stale read is the more dangerous of the
# two precisely because it looks healthy. systrader's cmd/api is kept alive by its own
# cron (`mage api:ensure`); if that dies, /api/stage keeps serving the last stages it
# computed, forever, with no error. This repo has been bitten by exactly this shape more
# than once: go-crond died silently for 5 days, and Dhan collection died for 5 days while
# every freshness check stayed green because one ticker a day was still being written.
#
# 5 calendar days spans a long weekend plus a holiday without tripping.
STAGE_MAX_AGE_DAYS = int(os.getenv("PORTFOLIO_STAGE_MAX_AGE_DAYS", "5"))

# The same guard for the confluence score (2026-09-23 data audit). The candidate query
# takes each company's LATEST score whatever its age, so a confluence step that fails for
# a week lets week-old scores pass silently. 4 calendar days covers a Friday score read
# on Monday plus one holiday. Fail-closed at entry: an unscored day is not a quiet day.
CONFLUENCE_MAX_AGE_DAYS = int(os.getenv("PORTFOLIO_CONFLUENCE_MAX_AGE_DAYS", "4"))


def load_stage_reads() -> dict[str, int]:
    """Weinstein stage per ticker from systrader's API.

    Returns {} on any failure, which makes the stage condition FAIL-CLOSED: no stage
    read, no entry. A missing price read must never be treated as a passing one -- that
    is the same "absence of evidence read as evidence of absence" mistake the
    evaluable_count guard exists to prevent (see the PRD).

    A read STALER than STAGE_MAX_AGE_DAYS is discarded the same way, because a frozen
    stage API serves last week's stages indefinitely and without error -- so trusting it
    would silently turn a daily price read into a constant. Each row carries its own
    `as_of`, so staleness is judged per row rather than trusting the endpoint wholesale.
    """
    try:
        with urllib.request.urlopen(f"{SYSTRADER_API}/api/stage", timeout=120) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return {}

    cutoff = (pd.Timestamp.now(tz="UTC") + pd.Timedelta(hours=5, minutes=30)).normalize() \
        - pd.Timedelta(days=STAGE_MAX_AGE_DAYS)
    out, stale, newest = {}, 0, None
    for row in payload.get("rows", []):
        if row.get("ticker") is None or row.get("stage") is None:
            continue
        as_of = row.get("as_of")
        if as_of is not None:
            try:
                as_of_ts = pd.Timestamp(as_of).tz_localize(None)
                newest = as_of_ts if newest is None or as_of_ts > newest else newest
                if as_of_ts < cutoff.tz_localize(None):
                    stale += 1
                    continue
            except (ValueError, TypeError):
                stale += 1
                continue
        out[str(row["ticker"])] = int(row["stage"])
    if stale:
        # Never silent: a stale stage API is an infrastructure outage wearing the costume
        # of a normal quiet day (CLAUDE.md, "no silent fallback"). But CLASSIFY it: every
        # run since at least 2026-09-15 printed "may be frozen" for ~25 suspended/illiquid
        # tickers while 2,135 rows were current -- an alarm that cries wolf daily is how
        # the real outage gets ignored. Frozen means the NEWEST read is stale.
        if newest is None or newest < cutoff.tz_localize(None):
            print(f"[{SYNC_SOURCE_NAME}] STAGE API FROZEN: newest read {newest} is older than "
                  f"{STAGE_MAX_AGE_DAYS} days; discarded {stale} read(s)", flush=True)
        else:
            print(f"[{SYNC_SOURCE_NAME}] discarded {stale} individually stale stage read(s) "
                  f"(suspended/illiquid tickers; API current as of {newest.date()})", flush=True)
    return out


def load_stage_keys(company_master_ids: list[str]) -> dict[str, list[str]]:
    """company_master_id -> the keys systrader's stage API may list it under, in order.

    2026-09-24: the lookup used `company_master_id.replace("nse:", "")` alone, so a
    renamed company (nse:TATAMOTORS, trading as TMPV) and every BSE-only company
    never found a stage -- and the entry rule fails closed without one (48 of 137
    active watchlist names). Now: the id's own symbol, its current NSE symbol after a
    rename, then "BSE:<scrip_code>" (systrader classifies BSE-only names since the same
    date)."""
    if not company_master_ids:
        return {}
    df = sql_to_df(
        """
        SELECT cm.company_master_id, cm.bse_scrip_code, a.alias_ticker
          FROM company_master cm
          LEFT JOIN company_master_nse_alias a ON a.company_master_id = cm.company_master_id
         WHERE cm.company_master_id = ANY(%s)
        """,
        params=(list(company_master_ids),),
    )
    keys: dict[str, list[str]] = {cmid: [str(cmid).replace("nse:", "")] for cmid in company_master_ids}
    for r in df.itertuples():
        found = keys.setdefault(str(r.company_master_id), [str(r.company_master_id).replace("nse:", "")])
        if isinstance(r.alias_ticker, str) and r.alias_ticker and r.alias_ticker not in found:
            found.append(r.alias_ticker)
        if isinstance(r.bse_scrip_code, str) and r.bse_scrip_code:
            found.append(f"BSE:{r.bse_scrip_code}")
    return keys


def stage_for(company_master_id: str, stages: dict[str, int], stage_keys: dict[str, list[str]]):
    for key in stage_keys.get(str(company_master_id)) or [str(company_master_id).replace("nse:", "")]:
        if key in stages:
            return stages[key]
    return None


def compute_stop_pct(symbols: list[str]) -> dict[str, dict]:
    """Volatility-scaled stop percentage per symbol, clamped to the operator's band.

    Uses adjusted closes stockey already owns (advisory_adjusted_ohlcv_daily). A symbol
    with too little history gets the MIDPOINT of the band rather than the widest end --
    an unknown-volatility name should not silently receive the loosest stop.
    """
    if not symbols:
        return {}
    df = sql_to_df(
        """
        SELECT symbol, stddev(r) AS daily_vol
          FROM (SELECT symbol,
                       adj_close / NULLIF(lag(adj_close) OVER (PARTITION BY symbol ORDER BY date), 0) - 1 AS r
                  FROM advisory_adjusted_ohlcv_daily
                 WHERE symbol = ANY(%s)
                   AND date >= now() - make_interval(days => %s)) x
         WHERE r IS NOT NULL
         GROUP BY symbol
        """,
        params=(list(symbols), VOL_LOOKBACK_DAYS),
    )
    vol_by_symbol = {
        str(r.symbol): float(r.daily_vol)
        for r in df.itertuples()
        if r.daily_vol is not None and pd.notna(r.daily_vol)
    }

    midpoint = (STOP_PCT_MIN + STOP_PCT_MAX) / 2
    out: dict[str, dict] = {}
    for symbol in symbols:
        daily_vol = vol_by_symbol.get(symbol)
        if not daily_vol or daily_vol <= 0:
            out[symbol] = {
                "stop_pct": round(midpoint, 2),
                "daily_vol_pct": None,
                "basis": "band midpoint (insufficient price history to size on volatility)",
            }
            continue
        ten_day_sigma_pct = daily_vol * 100 * (10 ** 0.5)
        raw = STOP_VOL_MULTIPLE * ten_day_sigma_pct
        clamped = min(max(raw, STOP_PCT_MIN), STOP_PCT_MAX)
        out[symbol] = {
            "stop_pct": round(clamped, 2),
            "daily_vol_pct": round(daily_vol * 100, 2),
            # States the raw product and whether the clamp BOUND. "(4.8%), clamped to 5-15%"
            # used to describe a 7.2% stop that no clamp touched (2026-09-23 audit).
            "basis": (
                f"{STOP_VOL_MULTIPLE}x the 10-day 1-sigma move ({ten_day_sigma_pct:.1f}%) = {raw:.1f}%"
                + (f", clamped to {clamped:g}% (band {STOP_PCT_MIN:g}-{STOP_PCT_MAX:g}%)" if clamped != raw
                   else f", inside the {STOP_PCT_MIN:g}-{STOP_PCT_MAX:g}% band")
            ),
        }
    return out


def evaluate_entry_candidates() -> dict[str, object]:
    """Run ruleset v1 over the active watchlist. Pure: no writes, no side effects.

    v1:  evaluable_count >= 1  AND  contradicting_count == 0  AND  stage == 2
    """
    from fundamentals.screens.confluence_score import _ensure_confluence_score_table

    _ensure_confluence_score_table()
    rows = sql_to_df(
        """
        WITH latest AS (
          SELECT DISTINCT ON (company_master_id)
                 company_master_id, run_date, score_version,
                 axis_fundamentals_trajectory, axis_event_corroboration, axis_sector_cycle,
                 axis_ownership, axis_valuation,
                 confluence_count, contradicting_count, evaluable_count
            FROM fundamentals_confluence_score
           ORDER BY company_master_id, run_date DESC, score_version DESC
        )
        SELECT w.company_master_id, w.narrative_text, w.first_seen_price, w.first_seen_at,
               l.confluence_count, l.contradicting_count, l.evaluable_count,
               l.axis_fundamentals_trajectory, l.axis_event_corroboration,
               l.axis_sector_cycle, l.axis_ownership, l.axis_valuation,
               l.run_date AS scored_on, l.score_version
          FROM fundamentals_watchlist w
          JOIN latest l USING (company_master_id)
         WHERE w.status = 'active'
        """
    )
    if rows.empty:
        return {"ruleset_version": RULESET_VERSION, "evaluated": 0, "candidates": [], "stage_reads": 0}

    stages = load_stage_reads()
    stage_keys = load_stage_keys(rows["company_master_id"].astype(str).tolist())
    score_cutoff = pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=CONFLUENCE_MAX_AGE_DAYS)
    stale_scores: list[str] = []
    candidates = []
    for r in rows.itertuples():
        if r.scored_on is None or pd.Timestamp(r.scored_on) < score_cutoff:
            stale_scores.append(str(r.company_master_id))
            continue
        ticker = str(r.company_master_id).replace("nse:", "")
        stage = stage_for(r.company_master_id, stages, stage_keys)
        # Each condition recorded individually so a rejection is explainable, not just
        # a boolean. This is what makes a later "why was X not taken" answerable.
        checks = {
            "has_evaluable_axis": bool(r.evaluable_count and r.evaluable_count >= 1),
            "no_contradicting_axis": r.contradicting_count == 0,
            "stage_is_advancing": stage == ENTRY_STAGE,
        }
        if not all(checks.values()):
            continue
        candidates.append(
            {
                "company_master_id": r.company_master_id,
                "ticker": ticker,
                "stage": stage,
                "confluence_count": int(r.confluence_count),
                "contradicting_count": int(r.contradicting_count),
                "evaluable_count": int(r.evaluable_count),
                "axes": {
                    "fundamentals_trajectory": r.axis_fundamentals_trajectory,
                    "event_corroboration": r.axis_event_corroboration,
                    "sector_cycle": r.axis_sector_cycle,
                    "ownership": r.axis_ownership,
                    "valuation": r.axis_valuation,
                },
                "narrative_text": r.narrative_text,
                "first_seen_price": r.first_seen_price,
                "first_seen_at": r.first_seen_at,
                "scored_on": r.scored_on,
                "score_version": int(r.score_version),
                "checks": checks,
            }
        )

    stops = compute_stop_pct([c["ticker"] for c in candidates])
    # The menu of L2 columns the adjudicator may bind a structured forecast to. Passed in
    # rather than looked up inside the adjudicator so one DB read serves the whole run.
    metrics = numeric_l2_metrics() if candidates else []
    for c in candidates:
        c["stop"] = stops.get(c["ticker"])
        c["available_l2_metrics"] = metrics

    return {
        "ruleset_version": RULESET_VERSION,
        "evaluated": int(len(rows)),
        "stage_reads": len(stages),
        # Surfaced, not swallowed: zero stage reads means systrader's API is down and
        # EVERY candidate was rejected for a reason that has nothing to do with the data.
        "stage_api_available": bool(stages),
        # Named, not silent: these were never evaluated because their latest confluence
        # score is older than CONFLUENCE_MAX_AGE_DAYS.
        "stale_confluence_skipped": stale_scores,
        "candidates": candidates,
    }
