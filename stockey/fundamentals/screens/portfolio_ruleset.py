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
    out, stale = {}, 0
    for row in payload.get("rows", []):
        if row.get("ticker") is None or row.get("stage") is None:
            continue
        as_of = row.get("as_of")
        if as_of is not None:
            try:
                if pd.Timestamp(as_of).tz_localize(None) < cutoff.tz_localize(None):
                    stale += 1
                    continue
            except (ValueError, TypeError):
                stale += 1
                continue
        out[str(row["ticker"])] = int(row["stage"])
    if stale:
        # Never silent: a stale stage API is an infrastructure outage wearing the costume
        # of a normal quiet day (CLAUDE.md, "no silent fallback").
        print(f"[{SYNC_SOURCE_NAME}] discarded {stale} stage read(s) older than "
              f"{STAGE_MAX_AGE_DAYS} days -- systrader's stage API may be frozen",
              flush=True)
    return out


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
            "basis": (
                f"{STOP_VOL_MULTIPLE}x the 10-day 1-sigma move "
                f"({ten_day_sigma_pct:.1f}%), clamped to {STOP_PCT_MIN:g}-{STOP_PCT_MAX:g}%"
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
           ORDER BY company_master_id, run_date DESC
        )
        SELECT w.company_master_id, w.narrative_text, w.first_seen_price, w.first_seen_at,
               l.confluence_count, l.contradicting_count, l.evaluable_count,
               l.axis_fundamentals_trajectory, l.axis_event_corroboration,
               l.axis_sector_cycle, l.axis_ownership, l.axis_valuation,
               l.run_date AS scored_on
          FROM fundamentals_watchlist w
          JOIN latest l USING (company_master_id)
         WHERE w.status = 'active'
        """
    )
    if rows.empty:
        return {"ruleset_version": RULESET_VERSION, "evaluated": 0, "candidates": [], "stage_reads": 0}

    stages = load_stage_reads()
    candidates = []
    for r in rows.itertuples():
        ticker = str(r.company_master_id).replace("nse:", "")
        stage = stages.get(ticker)
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
        "candidates": candidates,
    }
