"""L3 rule-based triggers -- fundamental screener step 7, rule half
(docs/FUNDAMENTAL_SCREENER_PRD.md sec 8 step 7 / sec 4's package layout, which puts
"daily rule-based triggers + LLM triage" in the same l3_triggers.py -- step 7's prose
is titled "LLM triage" but its own text says triage "augments a working alert
surface, it doesn't bootstrap one", meaning the rule-based alert surface has to exist
first. No earlier numbered step built it -- steps 0-6 built detection (fundamentals_
events) and state (fundamentals_l2_state) as two separate tables, but nothing yet
combined them. This module is that combination.

Core rule, straight from fundamental_basic_goal.md's L3 section: "Alert fires only on
trigger x primed L2 state. Never on trigger alone." A rating action or an insider
trade is not automatically alert-worthy -- it's alert-worthy when it lines up with (or
contradicts) what the company's own state vector already suggested. This is a real
design decision, not something the PRD spelled out field-by-field, so the specific
priming rules are recorded here, not assumed:

- rating_action: a downgrade is always alerted (new risk information regardless of
  prior state). An upgrade/reaffirmation is alerted only when L2's own net_debt_yoy_
  delta_rscr is already negative (deleveraging) -- that's an independent rating-agency
  confirmation of the exact turnaround thesis this screener exists to find (source PRD
  sec 1: this system grew out of the deleveraging screen, step 2). A reaffirmation
  with no such confirmation is treated as already-priced-in, not alert-worthy.
- pit_sast: a promoter/KMP buy is always alerted (rarer, higher-signal than a sell).
  A promoter/KMP sell is alerted only when L2's promoter_stake_direction is NOT
  already "decreasing" -- i.e. this transaction is NEW information, not a data point
  that merely confirms a trend L2 had already captured from the quarterly
  shareholding pattern. Rows with no transaction data at all (the common case --
  most BSE-detected pit_sast rows are trading-window-closure procedural notices, not
  actual trades) never alert.
- results (2026-08-13, docs/FUNDAMENTAL_SCREENER_RESULTS_ARC.md): deliberately does
  NOT reconcile against screener.in's own quarter-over-quarter growth fields --
  screener.in lags a fresh filing by days-to-a-week, so by the time it would agree
  or disagree, the point of an early signal is gone. Two independent, intentionally
  un-reconciled streams instead: this evaluator computes growth/margin/timing in
  Python from the filing's own OCR'd numbers (see evaluate_results_trigger);
  screener.in's own eventual figures are logged as a quality cross-check once they
  arrive, not merged into this decision.

Identity bridge: fundamentals_events is keyed by company_master_id ("nse:<ticker>",
stockey's own identity layer); fundamentals_l2_state is keyed by ticker alone
(inherited from fundamentals_l1_universe's screener.in identity) -- joined on
company_master_id = 'nse:' || ticker, confirmed live 2026-08-11 against a real
company (ZFSTEERING) present in both tables under that exact convention.

Idempotent by design: alerts upsert-key on (source, news_id, trigger_type), so
re-running this rule pass (e.g. after L2 refreshes) never duplicates an alert --
matches the append-only-but-idempotent pattern every other fundamentals table in this
codebase already uses.
"""

from __future__ import annotations

import json

import pandas as pd
from psycopg2 import sql as psycopg2_sql

from fundamentals.screens.investor_classification import effective_tier, normalize_investor_key
from fundamentals.screens.l1_universe import RPT_PCT_OF_REVENUE_THRESHOLD, rpt_is_material
from utils.company_master import build_l1_ticker_by_company_master_id
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.fallback_telemetry import record_local_fallback_event

SYNC_SOURCE_NAME = "fundamentals.screens.l3_triggers"
RESULTS_TABLE = "fundamentals_l3_alerts"
STOCKEY_RUN_STATE: dict[str, object] = {}

# Trigger types this rule pass evaluates. capital_raise added 2026-08-12 (see
# evaluate_capital_raise_trigger below). institutional_entry added 2026-08-13
# (synthetic events written by fundamentals/screens/l2_state.py, no exchange filing
# exists for this one). results added 2026-08-13 -- the last of the original four
# L3 triggers, see evaluate_results_trigger and docs/FUNDAMENTAL_SCREENER_RESULTS_ARC.md.
# auditor_change/related_party_transaction added 2026-08-13 (gap fix): both were
# already collected+extracted for fundamentals/screens/l1_universe.py's own
# post-hoc exclusion gate, but never became an alert for an already-watchlisted
# company -- see evaluate_auditor_change_trigger/evaluate_related_party_transaction_
# trigger.
SUPPORTED_FILING_TYPES = (
    "rating_action", "pit_sast", "capital_raise", "institutional_entry", "results",
    "auditor_change", "related_party_transaction",
)

_ALERTS_TABLE_STATEMENT = """
    CREATE TABLE IF NOT EXISTS fundamentals_l3_alerts (
        source TEXT NOT NULL,
        news_id TEXT NOT NULL,
        trigger_type TEXT NOT NULL,
        origin TEXT NOT NULL,
        company_master_id TEXT,
        alert_date DATE,
        reasoning TEXT,
        l2_run_date TEXT,
        l2_state_snapshot_json TEXT,
        status TEXT,
        model TEXT,
        prompt_version INTEGER,
        evidence_bundle_json TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (source, news_id, trigger_type)
    )
"""


def _record_fallback(fallback_type: str, *, reason: str, error, severity: str = "warn", metadata=None) -> None:
    record_local_fallback_event(
        module=SYNC_SOURCE_NAME,
        source="l3_triggers",
        fallback_type=fallback_type,
        severity=severity,
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def _ensure_alerts_table() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute(_ALERTS_TABLE_STATEMENT)

    execute_db_operation(_op, operation_name="fundamentals_l3_alerts:ensure_table")


def load_candidate_events(limit: int | None = None) -> pd.DataFrame:
    """rating_action/pit_sast events not yet evaluated by this rule pass (an event
    already alerted, or already evaluated and found not alert-worthy, is tracked via
    fundamentals_events.rule_trigger_status so re-runs don't redo work every day).
    structured_extraction_json is included for _resolve_rating_action_type()'s
    fallback -- see that function's docstring. rating_agency is included so
    evaluate_rating_action_trigger()'s reasoning can name which agency acted (2026-
    08-13 fix -- previously loaded but never selected here, so every rating alert's
    reasoning read identically regardless of ICRA/CRISIL/CARE/India Ratings).

    BUG FOUND LIVE 2026-08-18: this used to select every candidate the moment it
    was detected, regardless of whether OCR/structured extraction had actually run
    yet -- 6 of 7 evaluators return None (no evidence) when structured_extraction_
    json is still NULL, and that None gets written as a TERMINAL rule_trigger_
    status='not_alert_worthy' with nothing ever resetting it once real extraction
    data lands weeks later (the OCR backlog only drains ~18-37 items/day).
    Confirmed live: 1,037 of the 1,042-row OCR backlog already had a terminal
    rule_trigger_status while ocr_status was still NULL -- including all 18
    auditor_change events, a trigger this module's own docstring calls "always
    alert-worthy". Now only selects a candidate once its own enrichment path has
    genuinely concluded (rating_action/pit_sast rows with real flat data from
    their OWN dedicated feeds are unaffected -- those never needed the generic OCR
    pipeline in the first place; capital_raise/institutional_entry are unaffected
    too, per this module's own existing "alerts unconditionally" design)."""
    placeholders = ",".join(f"'{ft}'" for ft in SUPPORTED_FILING_TYPES)
    query = f"""
        SELECT source, news_id, company_master_id, filing_type, headline,
               rating_action_type, rating_agency, transaction_type, insider_name, quantity,
               disclosure_date, structured_extraction_json
        FROM fundamentals_events
        WHERE filing_type IN ({placeholders})
          AND (rule_trigger_status IS NULL OR rule_trigger_status = 'pending')
          AND (
                (filing_type = 'rating_action' AND rating_action_type IS NOT NULL)
             OR (filing_type = 'pit_sast' AND transaction_type IS NOT NULL AND transaction_type != '')
             OR filing_type IN ('capital_raise', 'institutional_entry')
             OR (attachment_name IS NULL AND rationale_pdf_url IS NULL)
             OR (ocr_status IN ('done', 'failed', 'no_document')
                 AND (structured_extraction_status IS NULL OR structured_extraction_status != 'pending'))
          )
        ORDER BY load_ts ASC NULLS LAST
    """  # noqa: S608 -- placeholders built from SUPPORTED_FILING_TYPES, a fixed internal constant, never user input
    if limit:
        query += f" LIMIT {int(limit)}"
    return sql_to_df(query)


def load_latest_l2_state() -> pd.DataFrame:
    return sql_to_df(
        """
        SELECT DISTINCT ON (ticker) ticker, company_name, net_debt_rscr, net_debt_yoy_delta_rscr,
               net_debt_consecutive_declining_years, net_debt_trend_direction,
               interest_coverage, debt_to_ebitda, cwip_ratio, cwip_ratio_yoy_delta,
               cwip_ratio_consecutive_declining_years, cwip_ratio_trend_direction,
               pledge_pct, promoter_pct, promoter_stake_direction, run_date
        FROM fundamentals_l2_state
        ORDER BY ticker, run_date DESC
        """
    )


# Most-to-least severe, matching RATING_ACTION_SCHEMA's own rating_action field
# description in structured_extraction.py exactly.
RATING_ACTION_SEVERITY_ORDER = ["downgraded", "placed_on_watch", "suspended", "withdrawn", "assigned", "reaffirmed", "upgraded"]


def _most_severe_rating_action(actions: list[str | None]) -> str | None:
    ranked = [a for a in actions if a in RATING_ACTION_SEVERITY_ORDER]
    if not ranked:
        return None
    return min(ranked, key=RATING_ACTION_SEVERITY_ORDER.index)


def _resolve_rating_action_type(event: dict) -> str | None:
    """rating_action_type is only ever populated by the ICRA-specific enrichment
    path (fundamentals/collectors/rating_agencies.py) -- for other agencies, the
    same fact (upgraded/downgraded/reaffirmed/...) is already sitting in
    structured_extraction_json's own `rating_action` field, extracted from the
    BSE-filed PDF by the generic OCR+extraction pipeline. Confirmed live 2026-08-12:
    a company's own BSE filing states the actual rating change explicitly (previous
    rating, new rating, action taken) -- it just doesn't carry the agency's
    rationale, which genuinely does require agency-site enrichment (see
    rating_agencies.py's own docstring).

    BUG FOUND LIVE 2026-08-18 (re-audit): this used to check the flat
    rating_action_type column FIRST, unconditionally -- bypassing structured_
    extraction_json's own per-instrument instrument_actions breakdown entirely
    whenever the flat column happened to be populated. And RATING_ACTION_SCHEMA's
    "never collapse a mix of actions into reaffirmed/upgraded when ANY covered
    instrument was downgraded" guarantee was enforced by PROMPT INSTRUCTION ALONE --
    nothing here cross-checked the top-level rating_action against instrument_
    actions' own per-instrument list, so a model that didn't follow the instruction
    could silently mask a real downgrade. Now takes the MOST SEVERE action across
    all three sources (flat column, JSON top-level, and every instrument_actions
    entry) whenever at least one is a recognized severity value -- a genuine
    downgrade on any one instrument can no longer be outvoted by a same-filing
    reaffirmation on another, or by a simpler, non-multi-instrument-aware ICRA-site
    scrape. Falls back to the old simple flat-then-JSON precedence only when none of
    the candidates are a recognized vocabulary value (preserves prior behavior for
    an unexpected/unrecognized action string)."""
    flat_action = event.get("rating_action_type")
    flat_action = str(flat_action).lower() if flat_action else None
    raw_json = event.get("structured_extraction_json")
    extracted = None
    if raw_json:
        try:
            extracted = json.loads(raw_json)
        except (TypeError, ValueError):
            extracted = None
    json_top_level = extracted.get("rating_action") if extracted else None
    json_top_level = str(json_top_level).lower() if json_top_level else None
    instrument_actions = (extracted.get("instrument_actions") if extracted else None) or []
    per_instrument = [str(a.get("rating_action")).lower() for a in instrument_actions if a.get("rating_action")]

    recognized = [a for a in (flat_action, json_top_level, *per_instrument) if a in RATING_ACTION_SEVERITY_ORDER]
    if recognized:
        return _most_severe_rating_action(recognized)
    return flat_action or json_top_level


def evaluate_rating_action_trigger(event: dict, l2_row: dict | None) -> dict | None:
    action = (_resolve_rating_action_type(event) or "").lower()
    # 2026-08-13: name the agency when known -- load_candidate_events() now selects
    # rating_agency, but a non-ICRA row's enrichment can still be pending, so this
    # stays optional rather than required.
    agency = event.get("rating_agency")
    agency_note = f" ({agency})" if agency else ""
    if action == "downgraded":
        return {
            "trigger_type": "rating_downgrade",
            "reasoning": f"Rating downgrade{agency_note} is alert-worthy regardless of prior L2 state -- new risk information.",
        }
    if action in ("upgraded", "reaffirmed"):
        net_debt_delta = l2_row.get("net_debt_yoy_delta_rscr") if l2_row else None
        if net_debt_delta is not None and pd.notna(net_debt_delta) and net_debt_delta < 0:
            return {
                "trigger_type": "rating_confirms_deleveraging",
                "reasoning": (
                    f"Rating {action}{agency_note} lines up with L2's own net_debt_yoy_delta_rscr={net_debt_delta} "
                    "(declining) -- independent rating-agency confirmation of a deleveraging trend already "
                    "visible in state."
                ),
            }
        return None  # reaffirmed/upgraded with no independent confirmation -- already priced in
    return None


def _resolve_pit_transaction_fields(event: dict) -> tuple[str | None, str | None]:
    """(transaction_type, insider_name), preferring the flat columns but falling back
    to structured_extraction_json -- same pattern as _resolve_rating_action_type,
    for the same reason. BUG FOUND LIVE 2026-08-15: the flat transaction_type/
    insider_name columns are ONLY ever populated by NSE's own structured
    corporates-pit feed (fundamentals/collectors/nse_pit.py); bse_announcements.py
    always writes them NULL for BSE-detected pit_sast filings (real trade detail
    lives only in the OCR'd PDF, extracted separately by structured_extraction.py's
    PIT_SAST_SCHEMA into structured_extraction_json). Confirmed live: 100% of the
    620 pit_sast events in fundamentals_events today are BSE-sourced with NULL flat
    columns, so this trigger -- including the "insider buy always alerts" rule this
    module's own docstring calls out as unconditional -- had never actually fired
    for any of them; at least 2 real promoter BUY transactions were confirmed sitting
    in structured_extraction_json, silently marked not_alert_worthy."""
    transaction_type = event.get("transaction_type")
    insider_name = event.get("insider_name")
    if transaction_type:
        return str(transaction_type), insider_name
    raw_json = event.get("structured_extraction_json")
    if not raw_json:
        return None, insider_name
    try:
        extracted = json.loads(raw_json)
    except (TypeError, ValueError):
        return None, insider_name
    return extracted.get("transaction_type"), insider_name or extracted.get("insider_name")


# insider_category ("promoter"/"kmp"/"director"/"employee"/"other", per PIT_SAST_
# SCHEMA) -> the reasoning label evaluate_pit_sast_trigger uses, instead of a
# hardcoded "Promoter/insider" regardless of who the filing actually named. "other"
# and an unrecognized/missing category both fall back to a generic "Insider" label
# -- still PIT-regulated, just not confidently one of the four named categories.
PIT_INSIDER_CATEGORY_LABELS = {"promoter": "Promoter", "kmp": "KMP", "director": "Director", "employee": "Employee"}


def _resolve_pit_disclosure_fields(event: dict) -> tuple[str | None, str | None]:
    """(disclosure_type, insider_category) from structured_extraction_json -- both
    only ever populated for BSE-sourced pit_sast rows (NSE's own rows are already
    fully structured with no OCR/extraction step, see nse_pit.py's build_pit_rows
    docstring, so this always returns (None, None) for them today; 0 NSE-sourced
    pit_sast rows exist in production as of this fix)."""
    raw_json = event.get("structured_extraction_json")
    if not raw_json:
        return None, None
    try:
        extracted = json.loads(raw_json)
    except (TypeError, ValueError):
        return None, None
    return extracted.get("disclosure_type"), extracted.get("insider_category")


def evaluate_pit_sast_trigger(event: dict, l2_row: dict | None) -> dict | None:
    # BUG FOUND LIVE 2026-08-18 (re-audit): this used to ignore insider_category and
    # disclosure_type entirely, both already extracted by PIT_SAST_SCHEMA -- alerting
    # on any transaction_type buy/sell match and hardcoding "Promoter/insider" in the
    # reasoning regardless of who the filing actually named. Live: a real resolved
    # SAST disclosure named a corporate acquirer under Regulation 10(6), not a
    # promoter/KMP buy -- would have been mislabeled had its transaction_type been a
    # plain "buy" instead of "other" (an inter-se transfer). disclosure_type is now
    # also an explicit gate: a "trading_window_notice" (the common case -- most
    # BSE-detected pit_sast rows are this, a routine procedural notice) never alerts
    # even if transaction_type came back non-empty, rather than trusting a possibly
    # spurious LLM-extracted value from a filing that structurally isn't a
    # transaction disclosure.
    disclosure_type, insider_category = _resolve_pit_disclosure_fields(event)
    if disclosure_type == "trading_window_notice":
        return None
    transaction_type_raw, insider_name = _resolve_pit_transaction_fields(event)
    transaction_type = (transaction_type_raw or "").lower()
    if not transaction_type:
        return None  # the common case -- a procedural notice with no actual trade
    category_label = PIT_INSIDER_CATEGORY_LABELS.get(insider_category, "Insider")
    if "buy" in transaction_type:
        return {
            "trigger_type": "insider_buy",
            "reasoning": f"{category_label} buy ({insider_name}) -- rarer and higher-signal than a sell, alerted regardless of prior L2 state.",
        }
    if "sell" in transaction_type:
        direction = (l2_row.get("promoter_stake_direction") if l2_row else None) or ""
        if direction != "decreasing":
            return {
                "trigger_type": "insider_sell_surprise",
                "reasoning": (
                    f"{category_label} sell ({insider_name}) while L2's own promoter_stake_direction "
                    f"was '{direction or 'unknown'}', not already 'decreasing' -- this is new information, not "
                    "confirmation of a trend L2 had already captured."
                ),
            }
        return None  # sell confirms an already-known declining trend -- not new information
    return None


def _summarize_investor_tiers(investor_tiers: list[dict] | None) -> str | None:
    """Short reasoning suffix naming the best-classified named investor, if any --
    marquee beats recognized beats unknown/unclassified when several are named. None
    when the filing named no investors at all (distinct from named-but-unclassified,
    which still gets a sentence -- see below)."""
    if not investor_tiers:
        return None
    tier_rank = {"marquee": 2, "recognized": 1}
    best = max(investor_tiers, key=lambda inv: tier_rank.get(inv.get("tier"), 0))
    if best.get("tier") in ("marquee", "recognized"):
        return f"Named investor {best['name']} classified as {best['tier']}."
    return f"Named investor(s) not yet classified (e.g. {best['name']})."


# L3 results trigger build (2026-08-13, docs/FUNDAMENTAL_SCREENER_RESULTS_ARC.md) --
# pure computation helpers, deliberately Python arithmetic, not an LLM judgment call.
# structured_extraction.py's RESULTS_SCHEMA already extracted the raw numbers (qoq/
# yoy revenue+PAT, finance costs, D&A); this only does deterministic math on them,
# per the PRD's own "version every signal definition" rule -- an LLM asked to
# compute/eyeball growth would drift silently between runs, arithmetic on
# already-extracted numbers won't.


def compute_growth_pct(current: float | None, baseline: float | None) -> float | None:
    """(current - baseline) / abs(baseline) * 100. Divides by abs(baseline), not
    baseline itself -- confirmed live this matters: a company swinging from a real
    loss (e.g. pat_yoy=-209.4) to a profit (pat_current=27.32) must read as a large
    POSITIVE change (it is one), but a naive current/baseline-signed denominator
    would produce a large NEGATIVE number instead (looks like "loss deepened",
    exactly backwards). None if either input is missing or baseline is exactly
    zero -- never a guessed/infinite growth rate."""
    if not isinstance(current, (int, float)) or not isinstance(baseline, (int, float)):
        return None
    if baseline == 0:
        return None
    return round((current - baseline) / abs(baseline) * 100, 2)


def compute_approx_operating_margin_pct(
    *, revenue: float | None, pat: float | None, finance_costs: float | None, depreciation_amortisation: float | None
) -> float | None:
    """Documented approximation, not a precise EBITDA margin: (PAT + Finance Costs +
    D&A) / Revenue. Indian quarterly filings don't disclose Profit Before Tax or Tax
    as their own extracted fields here, so a textbook EBITDA back-calculation (PBT +
    Exceptional Items + Finance Costs + D&A) isn't available without adding more
    schema fields to chase precision this screener doesn't need (fundamental_basic_
    goal.md sec 7: "judge it on recall and falsifiability", not precision). Skipping
    the tax addback biases the absolute number, but that bias is roughly stable
    period-to-period for the same company (a roughly stable tax rate), which is
    what a QoQ/YoY margin TREND comparison actually needs -- still directionally
    useful even though it isn't a textbook EBITDA margin. None if any input is
    missing or revenue is zero."""
    if not all(isinstance(v, (int, float)) for v in (revenue, pat, finance_costs, depreciation_amortisation)):
        return None
    if revenue == 0:
        return None
    return round((pat + finance_costs + depreciation_amortisation) / revenue * 100, 2)


RESULTS_PRIOR_PERIOD_LOOKBACK_DAYS = 550  # ~18 months -- see load_prior_same_period_results_event's own docstring


def load_prior_same_period_results_event(company_master_id: str, *, period_type: str | None, before_disclosure_date) -> dict | None:
    """Most recent PRIOR results event for this company with the SAME period_type
    (e.g. last year's Q1), strictly before before_disclosure_date -- the
    self-referential timing baseline compute_timing_delay_days below prefers over
    BSE's own forward calendar. Matched by period_type, not just "closest to 1 year
    back", so a Q1 filing is never compared against a Q2 one. None when period_type
    is unknown, or this is the first filing ever seen for that period_type (nothing
    to compare against, not a guessed baseline).

    BUG FOUND LIVE 2026-08-18 (re-audit): no upper bound on how far back this could
    reach -- if a company had a genuine multi-year gap for this period_type (e.g. a
    filing cadence change), the query would walk arbitrarily far into history and
    silently pick up a 2+-year-old filing as "last year's" baseline.
    compute_timing_delay_days adds exactly 365 days to whatever this returns, so a
    2-year-old match would report results_delayed as ~365 days late -- a full extra
    year of false lateness, not a real signal. Bounded to
    RESULTS_PRIOR_PERIOD_LOOKBACK_DAYS (18 months -- generous slack past the
    expected ~12 months for real-world filing-date variance, without accepting a
    multi-year-old match); no candidate in that window now correctly returns None
    (an unknown baseline) rather than a wrong one."""
    if not period_type:
        return None
    lookback_start = pd.Timestamp(before_disclosure_date) - pd.Timedelta(days=RESULTS_PRIOR_PERIOD_LOOKBACK_DAYS)
    df = sql_to_df(
        """
        SELECT disclosure_date, structured_extraction_json
        FROM fundamentals_events
        WHERE company_master_id = %s AND filing_type = 'results'
          AND structured_extraction_status = 'done' AND structured_extraction_json IS NOT NULL
          AND disclosure_date < %s AND disclosure_date >= %s
        ORDER BY disclosure_date DESC
        """,
        params=(company_master_id, str(before_disclosure_date), str(lookback_start.date())),
    )
    if df.empty:
        return None
    for _, row in df.iterrows():
        try:
            extracted = json.loads(row["structured_extraction_json"])
        except (TypeError, ValueError):
            continue
        if extracted.get("period_type") == period_type:
            return {"disclosure_date": row["disclosure_date"]}
    return None


def load_latest_results_calendar_event(company_master_id: str) -> dict | None:
    """Most recent results_calendar (BSE's own forward results-calendar, already
    collected by fundamentals/collectors/bse_announcements.py's fetch_result_
    calendar) event for this company -- corroborating-only baseline, see
    compute_timing_delay_days' own docstring for why own-history is preferred."""
    df = sql_to_df(
        """
        SELECT disclosure_date
        FROM fundamentals_events
        WHERE company_master_id = %s AND filing_type = 'results_calendar'
        ORDER BY load_ts DESC
        LIMIT 1
        """,
        params=(company_master_id,),
    )
    return df.iloc[0].to_dict() if not df.empty else None


def compute_timing_delay_days(
    disclosure_date, *, prior_same_period_disclosure_date=None, calendar_expected_date=None
) -> dict[str, int | None]:
    """Days late (positive) or early (negative) vs two baselines:
    - vs_own_history_days: PRIMARY -- baseline is this company's own prior-year
      same-period filing date + 365 days ("expected around the same time as last
      year"). Preferred over the calendar: a company's own cadence is a more
      reliable per-company signal than a market-wide calendar prediction (BSE's own
      calendar is often a rough/revised window, not a hard commitment -- see
      docs/FUNDAMENTAL_SCREENER_RESULTS_ARC.md).
    - vs_calendar_days: corroborating only.
    None for either key when its baseline input is missing/unparseable -- never a
    guessed delay."""
    current = pd.to_datetime(disclosure_date, errors="coerce")
    result: dict[str, int | None] = {"vs_own_history_days": None, "vs_calendar_days": None}
    if pd.isna(current):
        return result
    if prior_same_period_disclosure_date is not None:
        prior = pd.to_datetime(prior_same_period_disclosure_date, errors="coerce")
        if pd.notna(prior):
            expected = prior + pd.Timedelta(days=365)
            result["vs_own_history_days"] = int((current - expected).days)
    if calendar_expected_date is not None:
        expected_cal = pd.to_datetime(calendar_expected_date, errors="coerce")
        if pd.notna(expected_cal):
            result["vs_calendar_days"] = int((current - expected_cal).days)
    return result


# First-cut, undocumented-in-the-source-PRD thresholds -- same "documented
# placeholder, easy to tune once reviewed" treatment L1's liquidity floor and
# sector_cycle.py's PHASE_THRESHOLD_POINTS already got.
RESULTS_DECLINE_THRESHOLD_PCT = -10.0
RESULTS_GROWTH_THRESHOLD_PCT = 15.0
RESULTS_DELAY_THRESHOLD_DAYS = 15


def _exceptional_items_caveat(extracted: dict, metric: str) -> str:
    """One-line caveat when a PAT-driven growth conclusion might be distorted by a
    non-trivial one-off item -- exceptional_items_rs_lakh was extracted on every
    results filing since RESULTS_SCHEMA's own build but never read anywhere until
    this (2026-08-13 gap fix). Only applies to PAT -- exceptional items sit below
    the operating line, they never touch revenue -- and only when the value is a
    real non-zero number: 0 means "explicitly disclosed as nil" and None means "not
    disclosed at all", and RESULTS_SCHEMA's own docstring already distinguishes
    those two, so neither should read as noteworthy here."""
    if metric != "PAT":
        return ""
    exceptional = extracted.get("exceptional_items_rs_lakh")
    if not isinstance(exceptional, (int, float)) or exceptional == 0:
        return ""
    return f" Includes a Rs.{exceptional} lakh exceptional item -- PAT growth may not reflect the underlying operating trend."


def evaluate_results_trigger(event: dict, l2_row: dict | None) -> dict | None:
    """The last of fundamental_basic_goal.md's original four L3 triggers
    (docs/FUNDAMENTAL_SCREENER_RESULTS_ARC.md). Three independent checks, evaluated
    in this order, first match wins:

    1. results_delayed -- filed later than the company's own same-period-last-year
       baseline (see compute_timing_delay_days). Always alert-worthy on its own,
       unconditional on L2 state or the numbers themselves -- a late filing is a
       red flag in isolation, same "new risk information" reasoning rating_downgrade
       uses.
    2. results_decline -- revenue OR PAT declined YoY past threshold. Always
       alert-worthy, same reasoning as (1) and as rating_downgrade.
    3. results_confirm_turnaround -- revenue OR PAT grew YoY past threshold AND
       L2's own net_debt_yoy_delta_rscr is already improving (declining) --
       independent confirmation of the deleveraging/turnaround thesis this whole
       screener is built around, not just "a good quarter" (same corroboration gate
       evaluate_rating_action_trigger's rating_confirms_deleveraging branch uses). A
       strong quarter with no independent confirmation is treated as already priced
       in, not alert-worthy -- same as an upgrade/reaffirmation with no L2
       corroboration.

    Growth/margin are Python arithmetic on already-extracted numbers (compute_
    growth_pct/compute_approx_operating_margin_pct above), never LLM-judged -- see
    those functions' own docstrings. A PAT-driven decline/confirm reasoning also
    gets a one-line exceptional-item caveat when relevant -- see
    _exceptional_items_caveat."""
    raw_json = event.get("structured_extraction_json")
    if not raw_json:
        return None
    try:
        extracted = json.loads(raw_json)
    except (TypeError, ValueError):
        return None

    company_master_id = event.get("company_master_id")
    period_type = extracted.get("period_type")
    disclosure_date = event.get("disclosure_date")

    prior_event = (
        load_prior_same_period_results_event(company_master_id, period_type=period_type, before_disclosure_date=disclosure_date)
        if company_master_id and disclosure_date
        else None
    )
    calendar_event = load_latest_results_calendar_event(company_master_id) if company_master_id else None
    delay = compute_timing_delay_days(
        disclosure_date,
        prior_same_period_disclosure_date=prior_event.get("disclosure_date") if prior_event else None,
        calendar_expected_date=calendar_event.get("disclosure_date") if calendar_event else None,
    )
    if delay["vs_own_history_days"] is not None and delay["vs_own_history_days"] > RESULTS_DELAY_THRESHOLD_DAYS:
        return {
            "trigger_type": "results_delayed",
            "reasoning": (
                f"Results filed {delay['vs_own_history_days']} days later than the same period last year "
                "-- alert-worthy regardless of prior L2 state or the numbers themselves."
            ),
        }
    # BUG FOUND LIVE 2026-08-18 (re-audit): vs_calendar_days was computed by
    # compute_timing_delay_days above but never actually read anywhere -- a real
    # per-event DB query (load_latest_results_calendar_event) with no observable
    # effect. Used here as a genuine fallback (not a replacement) only when own-
    # history has no baseline at all (e.g. this company's first-ever filing of this
    # period_type) -- own-history stays the preferred, primary baseline per this
    # function's own docstring, calendar is corroborating-only.
    if (
        delay["vs_own_history_days"] is None
        and delay["vs_calendar_days"] is not None
        and delay["vs_calendar_days"] > RESULTS_DELAY_THRESHOLD_DAYS
    ):
        return {
            "trigger_type": "results_delayed",
            "reasoning": (
                f"Results filed {delay['vs_calendar_days']} days later than BSE's own forward results "
                "calendar expected (no own-history baseline available yet for this period type) -- "
                "alert-worthy regardless of prior L2 state or the numbers themselves."
            ),
        }

    revenue_growth_yoy = compute_growth_pct(extracted.get("revenue_current_rs_lakh"), extracted.get("revenue_yoy_rs_lakh"))
    pat_growth_yoy = compute_growth_pct(extracted.get("pat_current_rs_lakh"), extracted.get("pat_yoy_rs_lakh"))
    growth_candidates = [(name, g) for name, g in (("Revenue", revenue_growth_yoy), ("PAT", pat_growth_yoy)) if g is not None]
    if not growth_candidates:
        return None

    worst_metric, worst_growth = min(growth_candidates, key=lambda pair: pair[1])
    if worst_growth <= RESULTS_DECLINE_THRESHOLD_PCT:
        return {
            "trigger_type": "results_decline",
            "reasoning": (
                f"{worst_metric} declined {worst_growth}% YoY -- alert-worthy regardless of prior L2 state."
                f"{_exceptional_items_caveat(extracted, worst_metric)}"
            ),
        }

    best_metric, best_growth = max(growth_candidates, key=lambda pair: pair[1])
    if best_growth >= RESULTS_GROWTH_THRESHOLD_PCT:
        net_debt_delta = l2_row.get("net_debt_yoy_delta_rscr") if l2_row else None
        if net_debt_delta is not None and pd.notna(net_debt_delta) and net_debt_delta < 0:
            margin = compute_approx_operating_margin_pct(
                revenue=extracted.get("revenue_current_rs_lakh"),
                pat=extracted.get("pat_current_rs_lakh"),
                finance_costs=extracted.get("finance_costs_current_rs_lakh"),
                depreciation_amortisation=extracted.get("depreciation_amortisation_current_rs_lakh"),
            )
            margin_note = f" Approx operating margin {margin}%." if margin is not None else ""
            return {
                "trigger_type": "results_confirm_turnaround",
                "reasoning": (
                    f"{best_metric} grew {best_growth}% YoY, corroborating L2's own net_debt_yoy_delta_rscr="
                    f"{net_debt_delta} (declining) -- independent confirmation of a deleveraging/turnaround "
                    f"trend already visible in state.{margin_note}{_exceptional_items_caveat(extracted, best_metric)}"
                ),
            }
        return None  # strong quarter with no independent confirmation -- already priced in

    return None


def evaluate_auditor_change_trigger(event: dict, l2_row: dict | None) -> dict | None:
    """Always alert-worthy, unconditional on L2 state -- same "new risk information"
    reasoning insider buys/rating downgrades use. 2026-08-13 gap fix: auditor_change
    events were being collected and fully extracted (who resigned, who replaced
    them, effective date) since the L1 universe filter's own post-hoc exclusion
    build, but consumed ONLY by that one-time gate for candidates entering L1 --
    never became an alert for a company already on the watchlist, so an auditor
    resignation on an already-watched name went completely unnoticed. Reuses the
    exact disclosure_type=='confirmed_change' gate fundamentals/screens/
    l1_universe.py's own _auditor_change_excludes already established (see
    AUDITOR_CHANGE_SCHEMA's docstring for why disclosure_type, not the classifier,
    is the real gate against incidental mentions) -- proposed_change_agenda/
    incidental_mention/other never alert here either."""
    raw_json = event.get("structured_extraction_json")
    if not raw_json:
        return None
    try:
        extracted = json.loads(raw_json)
    except (TypeError, ValueError):
        return None
    if extracted.get("disclosure_type") != "confirmed_change":
        return None
    direction = extracted.get("change_direction")
    previous_auditor = extracted.get("previous_auditor")
    new_auditor = extracted.get("new_auditor")
    detail = ""
    if previous_auditor and new_auditor:
        detail = f" ({previous_auditor} -> {new_auditor})"
    elif new_auditor:
        detail = f" (new: {new_auditor})"
    elif previous_auditor:
        detail = f" (outgoing: {previous_auditor})"
    return {
        "trigger_type": "auditor_change",
        "reasoning": f"Statutory auditor {direction or 'change'}{detail} -- alert-worthy regardless of prior L2 state.",
    }


def evaluate_related_party_transaction_trigger(event: dict, l2_row: dict | None) -> dict | None:
    """Always alert-worthy, unconditional on L2 state, same reasoning as
    evaluate_auditor_change_trigger above and the same gap it closes. Reuses
    l1_universe.py's own rpt_is_material() -- SEBI LODR Regulation 23's own
    materiality threshold, not an invented number, and the SAME materiality
    decision fundamentals/screens/l1_universe.py's own _rpt_excludes makes (a
    non-applicable/below-threshold RPT never alerts here either) -- see
    rpt_is_material's own docstring for why this now also falls back to
    rpt_amount_rs_cr when the filing states an amount but no percentage."""
    raw_json = event.get("structured_extraction_json")
    if not raw_json:
        return None
    try:
        extracted = json.loads(raw_json)
    except (TypeError, ValueError):
        return None
    material, basis = rpt_is_material(extracted)
    if not material:
        return None
    pct = extracted.get("pct_of_revenue")
    related_party_name = extracted.get("related_party_name")
    amount = extracted.get("rpt_amount_rs_cr")
    detail = f" with {related_party_name}" if related_party_name else ""
    amount_note = f", Rs.{amount} cr" if isinstance(amount, (int, float)) else ""
    if basis == "pct":
        threshold_note = f" at {pct}% of revenue (>= {RPT_PCT_OF_REVENUE_THRESHOLD}% materiality threshold)"
    else:
        threshold_note = " -- no percentage-of-revenue stated in the filing, alerting on the stated amount alone"
    return {
        "trigger_type": "related_party_transaction",
        "reasoning": (
            f"Related-party transaction{detail}{amount_note}{threshold_note} -- alert-worthy regardless of prior L2 state."
        ),
    }


def evaluate_capital_raise_trigger(event: dict, l2_row: dict | None) -> dict | None:
    """Always alert-worthy, unconditional on L2 state -- "company getting money
    through any means is an important signal" (user, 2026-08-12), same reasoning as
    insider buys: new capital in the door is new information regardless of what the
    state vector already showed. Investor-level tier IS now folded into reasoning
    (2026-08-13 fix -- this docstring used to say it was deliberately left out; that
    was a real gap, not a design choice, once the pipeline audit found the L4/
    narrative layer never saw it either) via event['investor_tiers'], a list of
    {name, tier} attached by run_l3_rule_triggers() before evaluation -- see
    _resolve_investor_tiers_for_event() and _summarize_investor_tiers()."""
    reasoning = "Capital raise detected (preferential allotment / QIP / rights issue / warrants) -- new money in the door, alert-worthy regardless of prior L2 state."
    tier_note = _summarize_investor_tiers(event.get("investor_tiers"))
    if tier_note:
        reasoning = f"{reasoning} {tier_note}"
    return {"trigger_type": "capital_raise", "reasoning": reasoning}


def evaluate_institutional_entry_trigger(event: dict, l2_row: dict | None) -> dict | None:
    """Always alert-worthy, unconditional on L2 state -- the original
    fundamental_basic_goal.md L3 trigger #3 ("first institutional entry"), built
    2026-08-13. Not corroborated against l2_row on purpose: this event's own source
    IS L2's own shareholding table (fundamentals/screens/l2_state.py's
    compute_institutional_stake), so checking l2_row here would be circular, not
    independent confirmation -- same reasoning insider buys and capital_raise use for
    skipping corroboration."""
    return {
        "trigger_type": "institutional_first_entry",
        # Fallback text (headline is always populated in practice by l2_state.py --
        # this only matters for a hand-built event dict, e.g. in tests) carries the
        # same ~3yr-lookback caveat the real headline does, see l2_state.py's
        # _build_institutional_entry_event_row.
        "reasoning": event.get("headline")
        or (
            "First institutional (FII+DII) stake detected in screener.in's quarterly shareholding pattern "
            "(visible in ~3yr history -- may be a re-entry if earlier history isn't captured) -- alert-worthy "
            "regardless of prior L2 state."
        ),
    }


TRIGGER_EVALUATORS = {
    "rating_action": evaluate_rating_action_trigger,
    "pit_sast": evaluate_pit_sast_trigger,
    "capital_raise": evaluate_capital_raise_trigger,
    "institutional_entry": evaluate_institutional_entry_trigger,
    "results": evaluate_results_trigger,
    "auditor_change": evaluate_auditor_change_trigger,
    "related_party_transaction": evaluate_related_party_transaction_trigger,
}


def _set_rule_trigger_status(*, source: str, news_id: str, status: str) -> None:
    def _update() -> None:
        with db_session() as (_, cur):
            cur.execute(
                "UPDATE fundamentals_events SET rule_trigger_status = %s WHERE source = %s AND news_id = %s",
                (status, source, news_id),
            )

    execute_db_operation(_update, operation_name="fundamentals_events:rule_trigger_status_update")


def _bootstrap_rule_trigger_column() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'fundamentals_events'")
            if cur.fetchone() is None:
                return
            cur.execute(
                psycopg2_sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS rule_trigger_status TEXT").format(
                    psycopg2_sql.Identifier("fundamentals_events")
                )
            )

    execute_db_operation(_op, operation_name="fundamentals_events:ensure_rule_trigger_column")


def load_investor_tiers() -> pd.DataFrame:
    return sql_to_df("SELECT investor_key, llm_tier, override_tier FROM fundamentals_investor_classification")


def _resolve_investor_tiers_for_event(event: dict, tiers_by_key: dict[str, dict]) -> list[dict]:
    """Every named investor in a capital_raise event's structured_extraction_json,
    with its classification tier where known -- see evaluate_capital_raise_trigger's
    docstring for why this now feeds reasoning. tiers_by_key is bulk-loaded once per
    run_l3_rule_triggers() call, not per event (same "bulk-load once, dict-lookup per
    row" pattern l2_by_ticker below already uses)."""
    raw_json = event.get("structured_extraction_json")
    if not raw_json:
        return []
    try:
        extracted = json.loads(raw_json)
    except (TypeError, ValueError):
        return []
    tiers = []
    for raw_name in extracted.get("investor_names") or []:
        key = normalize_investor_key(raw_name)
        if not key:
            continue
        row = tiers_by_key.get(key)
        tiers.append({"name": str(raw_name).strip(), "tier": effective_tier(row) if row else None})
    return tiers


def run_l3_rule_triggers(*, limit: int | None = None) -> dict[str, object]:
    _bootstrap_rule_trigger_column()
    _ensure_alerts_table()

    events = load_candidate_events(limit)
    if events.empty:
        return {"alerted": 0, "not_alert_worthy": 0, "no_l2_state": 0}

    l2_state = load_latest_l2_state()
    l2_by_ticker = {row["ticker"]: row for row in l2_state.to_dict("records")}
    # BUG FOUND LIVE 2026-08-18: naive removeprefix("nse:") below only recovers
    # the correct fundamentals_l2_state.ticker when the company IS its own NSE
    # symbol -- wrong for the ~22% BSE-only cohort. Confirmed live: 44/196 (22%)
    # of L2 tickers resolve to a different canonical company_master_id, so every
    # corroboration-gated trigger (deleveraging confirmation, results-confirms-
    # turnaround, insider-sell-surprise) could never evaluate its L2 gate for
    # them. Built once per run (not per event) since it's the same reverse map
    # for every event in this batch.
    l1_ticker_by_cmid = build_l1_ticker_by_company_master_id()

    tiers_df = load_investor_tiers()
    tiers_by_key = {row["investor_key"]: row for row in tiers_df.to_dict("records")} if not tiers_df.empty else {}

    counts = {"alerted": 0, "not_alert_worthy": 0, "no_l2_state": 0}
    alert_rows: list[dict] = []

    for _, event in events.iterrows():
        event_dict = event.to_dict()
        ticker = l1_ticker_by_cmid.get(event_dict.get("company_master_id"))
        l2_row = l2_by_ticker.get(ticker) if ticker is not None else None
        if l2_row is None:
            counts["no_l2_state"] += 1
            _record_fallback(
                "l3_trigger_no_l2_state",
                reason="No L2 state found for this event's company; evaluated with L2 context unavailable (treated as None, may under-alert).",
                error="missing L2 state",
                metadata={"company_master_id": event_dict.get("company_master_id")},
            )

        if event_dict["filing_type"] == "capital_raise":
            event_dict["investor_tiers"] = _resolve_investor_tiers_for_event(event_dict, tiers_by_key)

        evaluator = TRIGGER_EVALUATORS[event_dict["filing_type"]]
        result = evaluator(event_dict, l2_row)

        if result is None:
            counts["not_alert_worthy"] += 1
            _set_rule_trigger_status(source=event_dict["source"], news_id=event_dict["news_id"], status="not_alert_worthy")
            continue

        counts["alerted"] += 1
        alert_rows.append(
            {
                "source": event_dict["source"],
                "news_id": event_dict["news_id"],
                "trigger_type": result["trigger_type"],
                "origin": "rule",
                "company_master_id": event_dict.get("company_master_id"),
                "alert_date": event_dict.get("disclosure_date"),
                "reasoning": result["reasoning"],
                "l2_run_date": str(l2_row.get("run_date")) if l2_row is not None else None,
                "l2_state_snapshot_json": json.dumps(l2_row, ensure_ascii=False, default=str) if l2_row is not None else None,
                # LOW FINDING (re-audit 2026-08-18): "status" is write-only -- always
                # "new", no reader or updater anywhere in this repo, shipped to the
                # frontend where it will always read "new" regardless of real review
                # state. Left as-is (a human-review-status workflow is real new
                # feature scope, not a bug fix) but documented so a future reader
                # doesn't assume this reflects live review state.
                "status": "new",
                "load_ts": pd.Timestamp.now(tz="UTC"),
            }
        )
        # rule_trigger_status is NOT set to "alerted" here -- see below, after the
        # batched upsert actually succeeds.

    if alert_rows:
        upsert_to_db(pd.DataFrame(alert_rows), RESULTS_TABLE, unique_keys=["source", "news_id", "trigger_type"])
        # BUG FOUND LIVE 2026-08-15, fixed here: this used to set rule_trigger_status
        # = "alerted" per-event INSIDE the loop above, each its own committed
        # transaction, well before this single batched upsert ran. A transient
        # failure on THIS call would have left every already-processed event in the
        # batch marked "alerted" with no corresponding fundamentals_l3_alerts row
        # ever written -- load_candidate_events() only re-selects NULL/pending, so
        # they'd be permanently suppressed with no retry. Only mark an event
        # "alerted" once its alert row is confirmed durably written.
        for row in alert_rows:
            _set_rule_trigger_status(source=row["source"], news_id=row["news_id"], status="alerted")

    return counts


def main() -> int:
    global STOCKEY_RUN_STATE
    result = run_l3_rule_triggers()
    STOCKEY_RUN_STATE = {
        "source": SYNC_SOURCE_NAME,
        "alerted": result["alerted"],
        "rows_written": result["alerted"],
        "not_alert_worthy": result["not_alert_worthy"],
        "no_l2_state": result["no_l2_state"],
        "fallback_used": result["no_l2_state"] > 0,
        "state_advanced": result["alerted"] > 0,
        "status": "ok",
    }
    print(json.dumps(STOCKEY_RUN_STATE, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
