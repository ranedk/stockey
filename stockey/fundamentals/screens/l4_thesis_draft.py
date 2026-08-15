"""L4 thesis DRAFTING -- fundamental screener step 11.5, sitting between watch_summary
(step 11, narrative synthesis) and notifications (step 12, digest email). Extends the
same per-company evidence bundle watch_summary.py already assembles (alerts, L2 state,
technicals, sector context, signal pointers) -- plus the narrative text watch_summary
JUST generated this run -- and asks an LLM to draft a CANDIDATE entry for
fundamentals_l4_thesis: a falsifiable, dated, non-price prediction, pre-committed
invalidation criteria, and a calibrated confidence score.

THIS MODULE NEVER WRITES fundamentals_l4_thesis AND NEVER CALLS create_thesis()
(fundamentals/screens/l4_thesis.py). That table's own docstring is explicit:
"creating a thesis is always a deliberate, separate human act... L3 never auto-creates
one" (fundamental_basic_goal.md sec 1's L4 section is the ultimate source for this --
L4 is "the ONE deliberate human act in this whole pipeline," the only gate capital
passes through). Automating the DRAFT is not automating the GATE: everything this
module produces lands in its own table, fundamentals_l4_thesis_draft, is surfaced in
the daily digest email as clearly-labeled candidate material, and requires a human to
read it, edit or discard it, and call create_thesis() themselves before it becomes a
real, committed thesis. If this boundary ever gets blurred -- e.g. a future change
that writes straight to fundamentals_l4_thesis from here -- that violates the PRD's
central rule and should not be made without the user explicitly revisiting it.

confidence_score (0-100) scores the FORECAST, not the stock: "how likely is this
specific prediction_text to resolve true by target_date, given only the evidence
shown" -- not general investment attractiveness or conviction. This mirrors
fundamental_basic_goal.md sec 5's own emphasis on scoring dated fundamental
predictions rather than returns; once real theses accumulate a resolution history,
compare this drafting-time score against actual outcomes as a free calibration check
on the draft LLM itself (not built yet -- there are zero resolved theses to compare
against, see l4_thesis.py's compute_quarterly_scoring).

Same regeneration discipline as watch_summary.py, and for the same reason (module
docstring there): gated on fundamentals_watchlist.last_alert_at moving past this
table's own generated_at, not a fixed daily schedule. A company with no new alert has
no new evidence to re-draft a prediction from -- re-running the LLM against unchanged
evidence would only add sampling noise to confidence_score, not signal. "Runs daily"
means this step executes every pipeline run (matching the user's request 2026-08-15
to make L4-register drafting part of the daily email); it does NOT mean every company
gets a fresh LLM call every day -- see run_pipeline.py step ordering: this module
runs after watch_summary so it can read the narrative text watch_summary just wrote."""

from __future__ import annotations

import json

import pandas as pd
from environs import Env
from openai import OpenAI

from fundamentals.screens.signal_pointers import get_stock_signal_pointers
from fundamentals.screens.watch_summary import (
    build_company_evidence_bundle,
    load_company_alerts,
    load_latest_l2_state_for_company,
    load_latest_technicals_for_company,
    load_sector_context_for_company,
)
from fundamentals.screens.watchlist import _ensure_watchlist_table
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.fallback_telemetry import record_local_fallback_event

env = Env()
env.read_env()

SYNC_SOURCE_NAME = "fundamentals.screens.l4_thesis_draft"
RESULTS_TABLE = "fundamentals_l4_thesis_draft"
STOCKEY_RUN_STATE: dict[str, object] = {}

DEFAULT_MODEL = env("L4_THESIS_DRAFT_MODEL", "gpt-5.4-mini")
PROMPT_VERSION = 1
CIRCUIT_BREAKER_THRESHOLD = 3
DEFAULT_BATCH_LIMIT = 50

L4_THESIS_DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "prediction_text": {
            "type": "string",
            "description": (
                "Exactly ONE falsifiable prediction about the company's FUNDAMENTALS (a debt, coverage, "
                "earnings, or ownership threshold checkable against a specific future filing) -- never a price "
                "target, never 'the stock will go up'. Name a concrete number/threshold and tie it to a real "
                "reporting event (e.g. 'Net debt/RSCR falls below 45 by the FY27 Q2 results')."
            ),
        },
        "target_date": {
            "type": "string",
            "description": "ISO date (YYYY-MM-DD) this prediction should be judged against -- a real date tied to an expected disclosure/results window, not an arbitrary horizon.",
        },
        "invalidation_criteria": {
            "type": "string",
            "description": "What evidence, from the same sources, would prove this prediction wrong -- pre-committed now, not decided after the fact.",
        },
        "confidence_score": {
            "type": "integer",
            "description": (
                "0-100: your own calibrated probability that prediction_text resolves true by target_date, "
                "using ONLY the evidence shown. This scores the FORECAST, not the company -- a genuinely "
                "uncertain turnaround deserves a middling score even if you find the story compelling."
            ),
        },
        "rationale": {
            "type": "string",
            "description": "1-3 sentences: why this specific prediction and confidence, grounded in the evidence shown.",
        },
    },
    "required": ["prediction_text", "target_date", "invalidation_criteria", "confidence_score", "rationale"],
    "additionalProperties": False,
}

L4_THESIS_DRAFT_SYSTEM_PROMPT = (
    "You are drafting a CANDIDATE entry for a human investor's fundamental-thesis register (L4), covering an "
    "Indian smallcap/microcap turnaround/deleveraging watchlist company. You will be shown the same evidence a "
    "human reviewer would see: the synthesized watch narrative, every alert accumulated for this company, its "
    "current state vector, descriptive price technicals, sector capital-cycle context, and structured signal "
    "pointers. Draft exactly one falsifiable, dated, non-price prediction about the company's fundamentals, "
    "pre-commit invalidation criteria (what would prove it wrong), and score your own calibrated confidence "
    "(0-100) that THIS SPECIFIC PREDICTION resolves true by its target date -- a forecast-calibration score, not "
    "a buy/sell conviction score. This output is a DRAFT for a human to edit or discard before it becomes a real "
    "thesis; it is never used on its own to place a trade, size a position, or take any market action."
)


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


_DRAFT_TABLE_STATEMENT = """
    CREATE TABLE IF NOT EXISTS fundamentals_l4_thesis_draft (
        company_master_id TEXT PRIMARY KEY,
        prediction_text TEXT,
        target_date DATE,
        invalidation_criteria TEXT,
        confidence_score INTEGER,
        rationale TEXT,
        source_alert_trigger_type TEXT,
        model TEXT,
        prompt_version INTEGER,
        generated_at TIMESTAMPTZ,
        load_ts TIMESTAMPTZ
    )
"""


def _ensure_draft_table() -> None:
    _ensure_watchlist_table()

    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute(_DRAFT_TABLE_STATEMENT)

    execute_db_operation(_op, operation_name="fundamentals_l4_thesis_draft:ensure_table")


def load_companies_needing_draft_refresh(limit: int | None = None) -> pd.DataFrame:
    """Only ACTIVE watchlist companies whose alert history moved since their last
    draft -- same staleness gate as watch_summary.load_companies_needing_narrative_
    refresh, see module docstring."""
    query = """
        SELECT w.company_master_id, w.last_alert_at, w.narrative_text
        FROM fundamentals_watchlist w
        LEFT JOIN fundamentals_l4_thesis_draft d ON d.company_master_id = w.company_master_id
        WHERE w.status = 'active'
          AND (d.generated_at IS NULL OR w.last_alert_at > d.generated_at::date)
        ORDER BY w.last_alert_at ASC NULLS LAST
    """
    if limit:
        query += f" LIMIT {int(limit)}"
    return sql_to_df(query)


def build_draft_evidence_bundle(company_master_id: str, alerts: pd.DataFrame, narrative_text: str | None) -> dict:
    l2_state = load_latest_l2_state_for_company(company_master_id)
    technicals = load_latest_technicals_for_company(company_master_id)
    sector_context = load_sector_context_for_company(company_master_id)
    signal_pointers = get_stock_signal_pointers(company_master_id)
    bundle = build_company_evidence_bundle(company_master_id, alerts, l2_state, technicals, sector_context, signal_pointers)
    bundle["narrative_text"] = narrative_text
    return bundle


def generate_l4_thesis_draft(evidence_bundle: dict, *, model: str = DEFAULT_MODEL) -> dict:
    client = OpenAI(api_key=env("OPENAI_API_KEY"))
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": L4_THESIS_DRAFT_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(evidence_bundle, ensure_ascii=False, default=str)},
        ],
        response_format={"type": "json_schema", "json_schema": {"name": "l4_thesis_draft", "schema": L4_THESIS_DRAFT_SCHEMA, "strict": True}},
    )
    return json.loads(response.choices[0].message.content)


def _latest_trigger_type(alerts: pd.DataFrame) -> str | None:
    if alerts.empty:
        return None
    return alerts.iloc[-1].get("trigger_type")


def run_l4_thesis_drafting(*, limit: int | None = None, model: str = DEFAULT_MODEL) -> dict[str, object]:
    _ensure_draft_table()

    candidates = load_companies_needing_draft_refresh(limit or DEFAULT_BATCH_LIMIT)
    if candidates.empty:
        return {"drafted": 0, "failed": 0, "blocked": False}

    counts = {"drafted": 0, "failed": 0}
    consecutive_failures = 0
    blocked = False
    now = pd.Timestamp.now(tz="UTC")

    for _, candidate in candidates.iterrows():
        if blocked:
            continue
        company_master_id = candidate["company_master_id"]

        try:
            # Evidence-gathering (including a DB read of fundamentals_l2_state) lives
            # INSIDE this try, not before it -- watch_summary.py's own sibling loop
            # had this exact bug (an unguarded call ahead of its try block let one
            # bad company's exception abort the entire remaining batch with no
            # fallback-telemetry record) and was fixed live 2026-08-15; that fix
            # wasn't carried over here when this file was written the same day, so
            # this repeats the mistake it should have avoided. Fixed here the same way.
            alerts = load_company_alerts(company_master_id)
            evidence_bundle = build_draft_evidence_bundle(company_master_id, alerts, candidate.get("narrative_text"))
            draft = generate_l4_thesis_draft(evidence_bundle, model=model)
            prediction_text = draft["prediction_text"]
            target_date = pd.Timestamp(draft["target_date"]).date()
            invalidation_criteria = draft["invalidation_criteria"]
            confidence_score = int(draft["confidence_score"])
            rationale = draft["rationale"]
        except Exception as exc:  # noqa: BLE001 -- classified as a failure either way
            consecutive_failures += 1
            counts["failed"] += 1
            _record_fallback(
                "l4_thesis_draft_generation_failed",
                reason="L4 thesis-draft LLM call failed for this company; it stays eligible for retry next run.",
                error=exc,
                metadata={"company_master_id": company_master_id},
            )
            if consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
                blocked = True
                _record_fallback(
                    "l4_thesis_draft_circuit_breaker_tripped",
                    reason=f"{consecutive_failures} consecutive L4 thesis-draft failures -- stopping this run immediately.",
                    error="circuit breaker",
                    severity="error",
                )
            continue

        consecutive_failures = 0
        row = {
            "company_master_id": company_master_id,
            "prediction_text": prediction_text.strip(),
            "target_date": target_date,
            "invalidation_criteria": invalidation_criteria.strip(),
            "confidence_score": confidence_score,
            "rationale": rationale.strip(),
            "source_alert_trigger_type": _latest_trigger_type(alerts),
            "model": model,
            "prompt_version": PROMPT_VERSION,
            "generated_at": now,
            "load_ts": now,
        }
        upsert_to_db(pd.DataFrame([row]), RESULTS_TABLE, unique_keys=["company_master_id"])
        counts["drafted"] += 1

    return {**counts, "blocked": blocked}


def load_current_drafts_by_company() -> dict[str, dict]:
    """For notifications.py's digest -- current draft (if any) per active watchlist
    company, keyed by company_master_id. Ensures the table exists first: this is
    called from notifications.py, which in the normal pipeline runs after
    run_l4_thesis_drafting() already created it, but callable standalone too (e.g.
    tests, a fresh DB) without erroring on a missing relation."""
    _ensure_draft_table()
    df = sql_to_df(
        """
        SELECT d.* FROM fundamentals_l4_thesis_draft d
        JOIN fundamentals_watchlist w ON w.company_master_id = d.company_master_id
        WHERE w.status = 'active'
        """
    )
    if df.empty:
        return {}
    return {row["company_master_id"]: row for row in df.to_dict("records")}


def main() -> int:
    global STOCKEY_RUN_STATE
    result = run_l4_thesis_drafting()
    STOCKEY_RUN_STATE = {
        "source": SYNC_SOURCE_NAME,
        "rows": result["drafted"],
        "rows_written": result["drafted"],
        "failed": result["failed"],
        "blocked": result["blocked"],
        "fallback_used": bool(result["failed"] or result["blocked"]),
        "state_advanced": result["drafted"] > 0,
        "status": "blocked" if result["blocked"] else "ok",
    }
    print(json.dumps(STOCKEY_RUN_STATE, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
