"""Structured extraction -- fundamental screener step 6, second stage
(docs/FUNDAMENTAL_SCREENER_PRD.md sec 6, sec 3.1: "GLM-OCR's own output is the input
to structured extraction, not an alternative to it"). Takes the raw OCR text
fundamentals/collectors/ocr_pipeline.py already produced and turns it into typed
fields via gpt-5.4-mini, per filing_type.

Model choice tested before building anything (explicit user instruction 2026-08-11,
not assumed): gpt-5.4-mini run live against 3 real OCR'd documents already sitting in
S3 from this session's own pipeline runs --
- a BSE quarterly-results filing with a genuine multi-column P&L table (every number
  correctly extracted, including correctly picking the CURRENT-quarter column out of
  four candidate columns);
- a real ICRA rating rationale (company/agency/amounts/ratings all correct, plus a
  well-synthesized rationale summary and separately-categorized positive/risk factor
  lists, not just copy-paste);
- a thin board-meeting-intimation notice with no financial content (correctly nulled
  every field rather than guessing).
Verdict: good enough, verified by hand against source text in all three cases, not
just accepted on the model's own say-so.

One real failure mode found and fixed during that same testing, not from imagination:
asked for an "auditor_qualification_flag" and the model returned "unqualified" with an
invented-sounding justification ("nothing came to attention"), even though the schema
said only to set it if the text discussed the opinion. On inspection the underlying
substance was actually correct -- the document DID contain a real, standard-form
"nothing has come to our attention..." clean review conclusion -- but the model
collapsed that into a categorical label without saying so, which is a genuine
overreach on a field this fragile (it would not have caught a real BSE OCR job's
attention if the label had been wrong instead of merely under-explained). Fixed by
replacing the forced category with an explicit boolean (was an opinion paragraph
present at all) plus a short grounded summary, and by instructing the model that
"null is correct far more often than a guessed value" and "silence is not evidence" --
re-tested, correctly distinguished "opinion present, no qualifying language" from
"opinion absent" afterward. Lesson generalized into every schema below: numbers/named
entities directly stated in the text are reliable; anything requiring inference about
the ABSENCE of something is the fragile case and gets a boolean-plus-quote shape
instead of a forced category, everywhere in this module, not just the audit field.

Every extraction is provenance-stamped (model, prompt/schema version, source news_id)
per fundamental_basic_goal.md sec 4's "version every signal definition" -- a later
review must be able to see exactly what the LLM was shown and which schema version
produced a given row, not just its conclusion.
"""

from __future__ import annotations

import json

import pandas as pd
from environs import Env
from openai import OpenAI
from psycopg2 import sql as psycopg2_sql

from fundamentals.collectors.events_store import RESULTS_TABLE, _ensure_events_schema
from utils.blob_store import get_text_blob
from utils.db import db_session, execute_db_operation, sql_to_df
from utils.fallback_telemetry import record_local_fallback_event

env = Env()
env.read_env()

SYNC_SOURCE_NAME = "fundamentals.collectors.structured_extraction"
STOCKEY_RUN_STATE: dict[str, object] = {}

DEFAULT_MODEL = env("STRUCTURED_EXTRACTION_MODEL", "gpt-5.4-mini")
# Bumped 1 -> 2 (2026-08-13): RESULTS_SCHEMA's shape changed (qoq/yoy split,
# finance_costs/depreciation_amortisation added, period_type broadened past Q1-Q4)
# -- see RESULTS_SCHEMA's own docstring. This is a single global version across
# every filing_type's extraction (not per-schema, matching this module's existing
# design), so it also bumps rating_action/pit_sast/capital_raise/auditor_change/rpt
# rows going forward even though only results actually changed -- per the PRD's own
# "version every signal definition" rule, a later re-read of an old row needs to
# know which schema shape produced it, and a coarser global version is what this
# module already committed to rather than something introduced here.
# Bumped 2 -> 3 (2026-08-17): RATING_ACTION_SCHEMA's shape changed (instrument_
# actions/multiple_instruments_covered added, top-level fields' descriptions now
# pin them to the most-severe instrument) -- see RATING_ACTION_SCHEMA's own
# docstring. Same global-version convention as the 1->2 bump above.
SCHEMA_VERSION = 3
CIRCUIT_BREAKER_THRESHOLD = 3
# 2026-08-15: raised 50 -> 400 (user request: "pace up so we don't have backlog").
# This stage is a plain LLM text-extraction call, not an OCR image-render job --
# confirmed live it's fast and low-variance (~3-4s/item), unlike ocr_pipeline.py's
# per-page cost, so it can absorb a much larger limit safely. It's gated on
# ocr_status='done' (load_pending_extraction_targets), so ocr_pipeline.py's own
# throughput is the real bottleneck this stage can't outrun on its own -- this bump
# just makes sure extraction is never the SECOND bottleneck once OCR speeds up.
DEFAULT_BATCH_LIMIT = 400

# Shared instruction, every filing_type -- the "null is correct far more often than a
# guessed value" lesson from this module's own live testing (see module docstring).
BASE_SYSTEM_PROMPT = (
    "Extract structured fields from this OCR'd exchange/rating-agency filing. Only use "
    "information actually present in the text. Be extremely conservative: null means "
    "'not stated', and null is correct far more often than a guessed value. Never infer "
    "a positive/clean state from the mere absence of negative language -- silence is not "
    "evidence. If a table's column/period headers are ambiguous or inconsistent, say so "
    "explicitly in confidence_notes rather than silently picking one interpretation."
)

# 2026-08-13 (L3 results trigger build, docs/FUNDAMENTAL_SCREENER_RESULTS_ARC.md):
# reshaped after a live audit of 43 real already-collected results filings.
#
# revenue/pat split into explicit _qoq/_yoy fields (was a single generic
# "_comparison" field + a freeform "_comparison_period_label" describing which
# baseline it was) -- confirmed live that a single slot forces an inconsistent
# choice across companies (the old label field's own values kept saying things
# like "the table headers are somewhat inconsistent/ambiguous... best matched to
# ..."). Explicit qoq/yoy fields let the LLM populate whichever the filing actually
# shows and leave the other null, instead of being forced to pick one; genuine
# ambiguity goes in confidence_notes (already the catch-all for exactly this) rather
# than a dedicated label field. "qoq"/"yoy" here mean "immediately preceding
# reporting period" / "same reporting period one year back" -- cadence-relative, not
# literally quarter-vs-quarter, since NOT every company reports quarterly (see
# period_type below).
#
# finance_costs/depreciation_amortisation added: confirmed live (a real Ambika
# Cotton Mills filing) that Indian quarterly results under Ind AS do NOT disclose an
# EBITDA/operating-profit line item at all -- these two lines are what
# fundamentals/screens/l3_triggers.py derives operating margin from (Revenue -
# Expenses excl. these two), not something extracted as a stated figure. Current
# period only for this first cut, not qoq/yoy -- margin TREND is a later increment,
# not blocking the initial trigger.
#
# period_type broadened past Q1-Q4 to include H1/H2/9M: confirmed live that
# SME-platform companies file half-yearly, not quarterly (real headlines: "quarter
# and half year ended..."). Don't assume every company is on a quarterly cadence --
# fundamentals/screens/l3_triggers.py infers each company's own reporting cadence
# from its own period_type history rather than a fixed assumption.
RESULTS_SCHEMA = {
    "type": "object",
    "properties": {
        "company_name": {"type": ["string", "null"]},
        "period_ended": {"type": ["string", "null"]},
        "period_type": {"type": ["string", "null"], "description": "Q1/Q2/Q3/Q4/H1/H2/9M/Annual, inferred from the period label -- H1/H2 for a half-yearly (SME-platform) filer, not every company reports quarterly"},
        "consolidated_or_standalone": {"type": ["string", "null"]},
        "revenue_current_rs_lakh": {"type": ["number", "null"]},
        "revenue_qoq_rs_lakh": {"type": ["number", "null"], "description": "revenue for the immediately preceding reporting period (preceding quarter, or preceding half-year for an H1/H2 filer) -- null if that column isn't shown"},
        "revenue_yoy_rs_lakh": {"type": ["number", "null"], "description": "revenue for the same reporting period one year earlier -- null if that column isn't shown"},
        "pat_current_rs_lakh": {"type": ["number", "null"]},
        "pat_qoq_rs_lakh": {"type": ["number", "null"]},
        "pat_yoy_rs_lakh": {"type": ["number", "null"]},
        "eps_basic_current": {"type": ["number", "null"]},
        "finance_costs_current_rs_lakh": {"type": ["number", "null"], "description": "the 'Finance Costs' P&L line item for the current period, if shown -- used to derive operating margin (no EBITDA/operating-profit line is disclosed directly)"},
        "depreciation_amortisation_current_rs_lakh": {"type": ["number", "null"], "description": "the 'Depreciation and amortisation expense' P&L line item for the current period, if shown -- same purpose as finance_costs_current_rs_lakh"},
        "exceptional_items_rs_lakh": {"type": ["number", "null"], "description": "0 if explicitly stated as zero/nil, null if not disclosed at all -- these are different things"},
        "audit_opinion_present": {"type": "boolean", "description": "true only if the auditor's/reviewer's opinion paragraph literally appears in the text"},
        "audit_opinion_summary": {"type": ["string", "null"], "description": "short, grounded paraphrase of the opinion actually stated -- null if audit_opinion_present is false"},
        "promoter_pledge_status": {"type": ["string", "null"]},
        "promoter_holding_pct": {"type": ["number", "null"]},
        "confidence_notes": {"type": "string"},
    },
    "required": [
        "company_name", "period_ended", "period_type", "consolidated_or_standalone",
        "revenue_current_rs_lakh", "revenue_qoq_rs_lakh", "revenue_yoy_rs_lakh",
        "pat_current_rs_lakh", "pat_qoq_rs_lakh", "pat_yoy_rs_lakh", "eps_basic_current",
        "finance_costs_current_rs_lakh", "depreciation_amortisation_current_rs_lakh",
        "exceptional_items_rs_lakh", "audit_opinion_present", "audit_opinion_summary",
        "promoter_pledge_status", "promoter_holding_pct", "confidence_notes",
    ],
    "additionalProperties": False,
}

# positive_factor_tags/risk_factor_tags are a loose, non-exhaustive controlled
# vocabulary -- listed in the prompt as *examples*, not an enum, since a real rationale
# will name things this list doesn't anticipate; risk_factor_details keeps the actual
# specifics so a tag alone never has to carry all the nuance.
# BUG FOUND LIVE 2026-08-17: a single rated_amount_rs_cr/previous_rating/current_
# rating/rating_action set can't represent a real, common rationale shape -- one
# filing rating SEVERAL distinct instruments/facilities (e.g. "Long-term Bank
# Facilities" downgraded alongside "Short-term Bank Facilities" reaffirmed in the
# same document), each with its own action. Forcing the model to pick just one
# action risked silently masking a real downgrade whenever a filing's OTHER
# instrument happened to be reaffirmed/upgraded -- a genuine problem given
# l3_triggers.py's evaluate_rating_action_trigger() treats "downgraded" as always
# alert-worthy regardless of prior state (fundamentals/screens/l3_triggers.py's own
# module docstring): a missed downgrade here is a silently missed alert, not a
# cosmetic gap.
#
# instrument_actions is the full per-instrument breakdown (one entry per rated
# instrument/facility actually named in the text, even if the filing only covers
# one -- consumers wanting per-instrument detail read this). The top-level
# instrument_description/rated_amount_rs_cr/previous_rating/current_rating/
# rating_action fields stay (existing consumers, notably l3_triggers.py's
# _resolve_rating_action_type, read rating_action alone) but are now explicitly
# defined to describe the MOST SEVERE instrument when actions differ, per the
# severity order in rating_action's own description below -- never an arbitrary or
# first-listed instrument, so the always-alert-on-downgrade rule can't be defeated
# by a mixed-action filing.
RATING_ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "company_name": {"type": ["string", "null"]},
        "rating_agency": {"type": ["string", "null"]},
        "instrument_description": {"type": ["string", "null"], "description": "the instrument/facility for the MOST SEVERE action (see rating_action) -- if only one instrument is covered, describes that one"},
        "rated_amount_rs_cr": {"type": ["number", "null"], "description": "amount for that same most-severe instrument, not a total across instruments"},
        "previous_rating": {"type": ["string", "null"], "description": "that same most-severe instrument's previous rating"},
        "current_rating": {"type": ["string", "null"], "description": "that same most-severe instrument's current rating"},
        "rating_action": {
            "type": ["string", "null"],
            "description": (
                "upgraded / downgraded / reaffirmed / withdrawn / assigned / suspended / placed_on_watch. "
                "If this filing covers multiple instruments/facilities with DIFFERENT actions, set this to "
                "the MOST SEVERE action present, using this order (most to least severe): downgraded > "
                "placed_on_watch > suspended > withdrawn > assigned > reaffirmed > upgraded. Never collapse a "
                "mix of actions into 'reaffirmed' or 'upgraded' when ANY covered instrument was downgraded -- "
                "see instrument_actions for the full per-instrument breakdown."
            ),
        },
        "instrument_actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "instrument_description": {"type": ["string", "null"]},
                    "rated_amount_rs_cr": {"type": ["number", "null"]},
                    "previous_rating": {"type": ["string", "null"]},
                    "current_rating": {"type": ["string", "null"]},
                    "rating_action": {"type": ["string", "null"], "description": "same vocabulary as the top-level rating_action, for this one instrument only"},
                },
                "required": ["instrument_description", "rated_amount_rs_cr", "previous_rating", "current_rating", "rating_action"],
                "additionalProperties": False,
            },
            "description": "one entry per instrument/facility actually named in the text -- exactly one entry for a single-instrument filing, several for a multi-instrument one. Empty array only if the filing gives no instrument-level breakdown at all.",
        },
        "multiple_instruments_covered": {"type": "boolean", "description": "true if instrument_actions has more than one entry"},
        "outlook_previous": {"type": ["string", "null"]},
        "outlook_current": {"type": ["string", "null"]},
        "rationale_summary": {"type": "string", "description": "2-3 sentence summary of why the agency took this action"},
        "positive_factor_tags": {"type": "array", "items": {"type": "string"}, "description": "short tags, e.g. revenue_growth, margin_expansion, deleveraging, liquidity_strength, market_position -- not limited to these examples"},
        "risk_factor_tags": {"type": "array", "items": {"type": "string"}, "description": "short tags, e.g. leverage_increase, litigation_risk, capex_execution_risk, cyclicality_exposure, customer_concentration, regulatory_risk -- not limited to these examples"},
        "risk_factor_details": {"type": "string", "description": "the actual specifics behind the risk tags -- a tag alone loses nuance"},
        "confidence_notes": {"type": "string"},
    },
    "required": [
        "company_name", "rating_agency", "instrument_description", "rated_amount_rs_cr",
        "previous_rating", "current_rating", "rating_action", "instrument_actions",
        "multiple_instruments_covered", "outlook_previous", "outlook_current",
        "rationale_summary", "positive_factor_tags", "risk_factor_tags", "risk_factor_details",
        "confidence_notes",
    ],
    "additionalProperties": False,
}

# Most BSE-sourced pit_sast rows this schema will ever see are trading-window-closure
# procedural notices with zero transaction data (confirmed live: the majority of real
# rows this session collected) -- genuine PIT transaction detail is NSE's structured
# corporates-pit feed's job (fundamentals/collectors/nse_pit.py), not OCR. This schema
# exists for the BSE-only/redundant case, and must null out gracefully on the common
# no-transaction-data path (already confirmed live: no hallucination on a thin notice).
PIT_SAST_SCHEMA = {
    "type": "object",
    "properties": {
        "company_name": {"type": ["string", "null"]},
        "disclosure_type": {"type": ["string", "null"], "description": "trading_window_notice / pit_disclosure / sast_disclosure / other"},
        "insider_name": {"type": ["string", "null"]},
        "insider_category": {"type": ["string", "null"], "description": "promoter / kmp / director / employee / other"},
        "transaction_type": {"type": ["string", "null"], "description": "buy / sell / pledge / release_of_pledge / other"},
        "quantity_shares": {"type": ["number", "null"]},
        "pct_of_holding_before": {"type": ["number", "null"]},
        "pct_of_holding_after": {"type": ["number", "null"]},
        "confidence_notes": {"type": "string"},
    },
    "required": [
        "company_name", "disclosure_type", "insider_name", "insider_category",
        "transaction_type", "quantity_shares", "pct_of_holding_before", "pct_of_holding_after",
        "confidence_notes",
    ],
    "additionalProperties": False,
}

# "Company getting money through any means" (user, 2026-08-12) -- preferential
# allotment, QIP, rights issue, warrant conversion, FCCB. investor_names is an array,
# not a single field: a preferential allotment/QIP routinely allots to several
# entities in one filing, and each named investor feeds
# fundamentals/screens/investor_classification.py independently. Numeric/named-entity
# fields here (amount, price, share count, names) are the reliable case per this
# module's own docstring -- no boolean-plus-quote hedging needed, unlike audit_opinion.
CAPITAL_RAISE_SCHEMA = {
    "type": "object",
    "properties": {
        "company_name": {"type": ["string", "null"]},
        "instrument_type": {"type": ["string", "null"], "description": "preferential_allotment / qip / rights_issue / warrants / fccb / other"},
        "total_amount_rs_cr": {"type": ["number", "null"]},
        "price_per_share_rs": {"type": ["number", "null"]},
        "number_of_shares": {"type": ["number", "null"]},
        "investor_names": {"type": "array", "items": {"type": "string"}, "description": "every named allottee/investor actually stated in the text -- empty array if none named, never invented"},
        "purpose_summary": {"type": ["string", "null"], "description": "stated use of proceeds, if disclosed"},
        "confidence_notes": {"type": "string"},
    },
    "required": [
        "company_name", "instrument_type", "total_amount_rs_cr", "price_per_share_rs",
        "number_of_shares", "investor_names", "purpose_summary", "confidence_notes",
    ],
    "additionalProperties": False,
}

# L1 universe filter's auditor-change check (2026-08-13, fundamentals/screens/
# l1_universe.py's own DEFERRED_CHECKS) -- disclosure_type is the load-bearing field
# here, same "classify broadly, let extraction judge precisely" split PIT_SAST_
# SCHEMA's disclosure_type already established: bse_announcements.py's classifier
# deliberately over-matches "statutory auditor" (confirmed live: catches genuine
# appointment/resignation filings AND routine results filings that merely mention the
# incumbent auditor's report, e.g. under a generic "Outcome of Board Meeting"
# subcategory where SUBCATNAME gives no signal) -- this field is what keeps an
# incidental mention from becoming a false L1 exclusion, not the classifier.
AUDITOR_CHANGE_SCHEMA = {
    "type": "object",
    "properties": {
        "company_name": {"type": ["string", "null"]},
        "disclosure_type": {
            "type": "string",
            "description": (
                "confirmed_change: the filing states an auditor was actually appointed or resigned/removed. "
                "proposed_change_agenda: an EGM/AGM notice proposing a change not yet approved. "
                "incidental_mention: the auditor is named only in passing (e.g. citing their report on results), "
                "no change stated. other: none of the above."
            ),
        },
        "change_direction": {"type": ["string", "null"], "description": "appointment / resignation / null if disclosure_type is not confirmed_change"},
        "previous_auditor": {"type": ["string", "null"]},
        "new_auditor": {"type": ["string", "null"]},
        "effective_date": {"type": ["string", "null"], "description": "ISO date if stated, else null -- never inferred from the filing date"},
        "confidence_notes": {"type": "string"},
    },
    "required": [
        "company_name", "disclosure_type", "change_direction", "previous_auditor",
        "new_auditor", "effective_date", "confidence_notes",
    ],
    "additionalProperties": False,
}

# L1 universe filter's related-party-transaction check (2026-08-13, same
# DEFERRED_CHECKS entry) -- is_applicable mirrors RESULTS_SCHEMA's audit_opinion_
# present boolean-hedge pattern: both real matches found live were "non-applicability"
# declarations under SEBI LODR Regulation 23(9), stating RPT does NOT apply to the
# company at all, not an amount disclosure -- l1_universe.py's post-hoc exclusion
# reads pct_of_revenue only, so a non-applicability filing correctly contributes
# nothing to exclude on (is_applicable=false, pct_of_revenue=null), not a guessed 0%.
RPT_SCHEMA = {
    "type": "object",
    "properties": {
        "company_name": {"type": ["string", "null"]},
        "is_applicable": {"type": "boolean", "description": "true only if the filing discloses an actual related-party transaction, false for a non-applicability/below-threshold declaration"},
        "rpt_amount_rs_cr": {"type": ["number", "null"], "description": "null if is_applicable is false or no amount is stated"},
        "pct_of_revenue": {"type": ["number", "null"], "description": "only if the filing itself states this percentage -- never computed/inferred here"},
        "related_party_name": {"type": ["string", "null"]},
        "confidence_notes": {"type": "string"},
    },
    "required": [
        "company_name", "is_applicable", "rpt_amount_rs_cr", "pct_of_revenue",
        "related_party_name", "confidence_notes",
    ],
    "additionalProperties": False,
}

SCHEMAS_BY_FILING_TYPE = {
    "results": ("results_extraction", RESULTS_SCHEMA),
    "results_calendar": ("results_extraction", RESULTS_SCHEMA),
    "rating_action": ("rating_action_extraction", RATING_ACTION_SCHEMA),
    "pit_sast": ("pit_sast_extraction", PIT_SAST_SCHEMA),
    "capital_raise": ("capital_raise_extraction", CAPITAL_RAISE_SCHEMA),
    "auditor_change": ("auditor_change_extraction", AUDITOR_CHANGE_SCHEMA),
    "related_party_transaction": ("rpt_extraction", RPT_SCHEMA),
}

EXTRACTION_COLUMN_TYPES = {
    "structured_extraction_status": "TEXT",
    "structured_extraction_json": "TEXT",
    "structured_extraction_model": "TEXT",
    "structured_extraction_schema_version": "INTEGER",
}


def _record_fallback(fallback_type: str, *, reason: str, error, severity: str = "warn", metadata=None) -> None:
    record_local_fallback_event(
        module=SYNC_SOURCE_NAME,
        source="openai",
        fallback_type=fallback_type,
        severity=severity,
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def _bootstrap_extraction_columns() -> None:
    """Same idempotent-ALTER pattern as events_store/rating_agencies/ocr_pipeline's own
    schema bootstraps, for this module's columns."""

    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = %s", (RESULTS_TABLE,))
            if cur.fetchone() is None:
                return
            for column, pg_type in EXTRACTION_COLUMN_TYPES.items():
                cur.execute(
                    psycopg2_sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} {}").format(
                        psycopg2_sql.Identifier(RESULTS_TABLE), psycopg2_sql.Identifier(column), psycopg2_sql.SQL(pg_type)
                    )
                )

    execute_db_operation(_op, operation_name="fundamentals_events:ensure_extraction_columns")


class UnsupportedFilingTypeError(RuntimeError):
    """Raised when a row's filing_type has no extraction schema -- caught by the
    caller and recorded as skipped, not a failure (this is an expected, visible gap,
    not an error condition)."""


def extract_structured_fields(ocr_text: str, filing_type: str, *, model: str = DEFAULT_MODEL) -> dict:
    if filing_type not in SCHEMAS_BY_FILING_TYPE:
        raise UnsupportedFilingTypeError(f"no extraction schema for filing_type={filing_type!r}")
    schema_name, schema = SCHEMAS_BY_FILING_TYPE[filing_type]
    client = OpenAI(api_key=env("OPENAI_API_KEY"))
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": BASE_SYSTEM_PROMPT},
            {"role": "user", "content": ocr_text},
        ],
        response_format={"type": "json_schema", "json_schema": {"name": schema_name, "schema": schema, "strict": True}},
    )
    return json.loads(response.choices[0].message.content)


def load_pending_extraction_targets(limit: int | None = None) -> pd.DataFrame:
    query = """
        SELECT source, news_id, filing_type, ocr_text_s3_key
        FROM fundamentals_events
        WHERE ocr_status = 'done'
          AND (structured_extraction_status IS NULL OR structured_extraction_status = 'pending')
        ORDER BY load_ts ASC NULLS LAST
    """
    if limit:
        query += f" LIMIT {int(limit)}"
    return sql_to_df(query)


def _set_extraction_result(*, source: str, news_id: str, status: str, fields: dict | None = None) -> None:
    fields = fields or {}
    set_columns = ["structured_extraction_status = %s"] + [f"{col} = %s" for col in fields]
    params = [status, *fields.values(), source, news_id]

    def _update() -> None:
        with db_session() as (_, cur):
            cur.execute(
                f"UPDATE fundamentals_events SET {', '.join(set_columns)} WHERE source = %s AND news_id = %s",  # noqa: S608 -- column names are our own fixed constants, never user input
                params,
            )

    execute_db_operation(_update, operation_name="fundamentals_events:extraction_update")


def run_structured_extraction(*, limit: int | None = None, model: str = DEFAULT_MODEL) -> dict[str, object]:
    _ensure_events_schema()
    _bootstrap_extraction_columns()

    pending = load_pending_extraction_targets(limit or DEFAULT_BATCH_LIMIT)
    if pending.empty:
        return {"extracted": 0, "failed": 0, "unsupported_filing_type": 0, "blocked": False}

    counts = {"extracted": 0, "failed": 0, "unsupported_filing_type": 0}
    consecutive_failures = 0
    blocked = False

    for _, row in pending.iterrows():
        if row["filing_type"] not in SCHEMAS_BY_FILING_TYPE:
            counts["unsupported_filing_type"] += 1
            _set_extraction_result(source=row["source"], news_id=row["news_id"], status="unsupported_filing_type")
            continue

        if blocked:
            continue

        try:
            ocr_text = get_text_blob(row["ocr_text_s3_key"])
            extracted = extract_structured_fields(ocr_text, row["filing_type"], model=model)
        except Exception as exc:  # noqa: BLE001 -- classified as a failure either way
            consecutive_failures += 1
            counts["failed"] += 1
            _set_extraction_result(source=row["source"], news_id=row["news_id"], status="failed")
            _record_fallback(
                "structured_extraction_failed",
                reason="Structured extraction failed for this row; it stays structured_extraction_status=failed permanently -- load_pending_extraction_targets only re-selects NULL/pending, so this needs a manual UPDATE to retry, not an automatic one.",
                error=exc,
                metadata={"news_id": row["news_id"], "filing_type": row["filing_type"]},
            )
            if consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
                blocked = True
                _record_fallback(
                    "structured_extraction_circuit_breaker_tripped",
                    reason=f"{consecutive_failures} consecutive extraction failures -- stopping this run immediately.",
                    error="circuit breaker",
                    severity="error",
                )
            continue

        consecutive_failures = 0
        counts["extracted"] += 1
        _set_extraction_result(
            source=row["source"],
            news_id=row["news_id"],
            status="done",
            fields={
                "structured_extraction_json": json.dumps(extracted, ensure_ascii=False, default=str),
                "structured_extraction_model": model,
                "structured_extraction_schema_version": SCHEMA_VERSION,
            },
        )

    return {**counts, "blocked": blocked}


def main() -> int:
    global STOCKEY_RUN_STATE
    result = run_structured_extraction()
    STOCKEY_RUN_STATE = {
        "source": SYNC_SOURCE_NAME,
        "extracted": result["extracted"],
        "rows_written": result["extracted"],
        "failed": result["failed"],
        "unsupported_filing_type": result["unsupported_filing_type"],
        "blocked": result["blocked"],
        "fallback_used": bool(result["failed"] or result["blocked"]),
        "state_advanced": result["extracted"] > 0,
        "status": "blocked" if result["blocked"] else "ok",
    }
    print(json.dumps(STOCKEY_RUN_STATE, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
