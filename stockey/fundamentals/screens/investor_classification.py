"""Investor classification -- fundamental screener step 14 (agreed 2026-08-12):
"company getting money through any means is an important signal... high quality
investor investing in a company is a big confidence boost." Classifies each named
investor from a capital-raise filing (preferential allotment / QIP / rights issue /
warrants -- fundamentals/collectors/structured_extraction.py's CAPITAL_RAISE_SCHEMA)
into one of three tiers by how widely RECOGNIZED the name is, not how skilled or
well-performing.

Deliberately NOT a performance/quality score: we have no AUM, returns, or track-record
data to check an LLM's judgment against, so asking it to rate investing SKILL would be
an unverifiable, hallucination-prone guess -- exactly the "null is correct far more
often than a guessed value" trap this codebase has hit and fixed before (see
structured_extraction.py's own audit_opinion_flag lesson). Recognition/fame is a
groundable question instead: does the LLM's own knowledge actually contain this name.
The third tier ("unknown") is the deliberately-honest default, not a failure mode --
most real investor names in a smallcap capital raise genuinely will not be famous, and
forcing a guess between "marquee" and "recognized" here would be the same mistake
again in a new shape.

Uses the LLM's own training knowledge only, not live web search (2026-08-12 build
decision) -- a browsing-enabled call is a different API surface than the plain chat
completions every other LLM step in this codebase already uses; add it later only if
memory-only recall proves too weak in practice, not preemptively.

Classified ONCE per investor (keyed on a normalized name, fundamentals_investor_
classification), then reused across every filing/company that investor appears in --
avoids re-asking the same question repeatedly and means a single human correction
(override_tier, always wins over llm_tier) fixes it everywhere that investor shows up.
This is a suggestion the human can override, never an authority -- same pattern as
fundamentals/screens/l4_thesis.py's check_structured_prediction(), and the override is
never auto-applied by anything else in this pipeline (nothing here gates an alert or a
trigger on classification tier)."""

from __future__ import annotations

import json
import re

import pandas as pd
from environs import Env
from openai import OpenAI

from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.fallback_telemetry import record_local_fallback_event

env = Env()
env.read_env()

SYNC_SOURCE_NAME = "fundamentals.screens.investor_classification"
RESULTS_TABLE = "fundamentals_investor_classification"
STOCKEY_RUN_STATE: dict[str, object] = {}

DEFAULT_MODEL = env("INVESTOR_CLASSIFICATION_MODEL", "gpt-5.4-mini")
PROMPT_VERSION = 1
CIRCUIT_BREAKER_THRESHOLD = 3
DEFAULT_BATCH_LIMIT = 50

INVESTOR_TIERS = ("marquee", "recognized", "unknown")

_INVESTOR_CLASSIFICATION_TABLE_STATEMENT = """
    CREATE TABLE IF NOT EXISTS fundamentals_investor_classification (
        investor_key TEXT PRIMARY KEY,
        investor_name_display TEXT NOT NULL,
        llm_tier TEXT,
        llm_reasoning TEXT,
        llm_model TEXT,
        llm_prompt_version INTEGER,
        llm_classified_at TIMESTAMPTZ,
        override_tier TEXT,
        override_notes TEXT,
        override_at TIMESTAMPTZ,
        first_seen_source TEXT,
        first_seen_news_id TEXT,
        load_ts TIMESTAMPTZ
    )
"""

INVESTOR_CLASSIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "tier": {"type": "string", "enum": list(INVESTOR_TIERS)},
        "reasoning": {"type": "string", "description": "1-2 sentences grounding the tier -- if unknown, say so plainly rather than guessing"},
    },
    "required": ["tier", "reasoning"],
    "additionalProperties": False,
}

INVESTOR_CLASSIFICATION_SYSTEM_PROMPT = (
    "You are classifying an investor/institution name by how widely RECOGNIZED it is in Indian and "
    "global markets -- NOT by investment skill, performance, or AUM (you have no reliable data on "
    "those, and must not guess or infer them). Three tiers, in order: "
    "'marquee' = a globally or nationally famous fund/investor most market participants would "
    "immediately recognize by name (e.g. a well-known sovereign wealth fund, a top-tier global "
    "PE/VC firm, a famous individual investor). "
    "'recognized' = a real, identifiable institutional name you have genuine knowledge of, but not "
    "a household name. "
    "'unknown' = you do not actually recognize this name, or cannot confirm it is a real "
    "institution -- this is the CORRECT and expected answer for most names, not a failure. Never "
    "guess a tier to sound confident; 'unknown' is right far more often than 'marquee' or "
    "'recognized' for a typical smallcap capital raise."
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


def _ensure_investor_classification_table() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute(_INVESTOR_CLASSIFICATION_TABLE_STATEMENT)

    execute_db_operation(_op, operation_name="fundamentals_investor_classification:ensure_table")


def normalize_investor_key(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def effective_tier(row: dict) -> str | None:
    """override_tier always wins when set -- the one place a human correction is
    read back, everywhere this classification is used."""
    return row.get("override_tier") or row.get("llm_tier")


def load_unclassified_investor_names(limit: int | None = None) -> list[dict]:
    """Every (investor_key, investor_name_display, source, news_id) not yet in
    fundamentals_investor_classification, pulled from structured-extracted
    capital_raise events' investor_names array. A name seen in multiple filings this
    run is only queued once (first occurrence wins first_seen_source/news_id)."""
    events_df = sql_to_df(
        """
        SELECT source, news_id, structured_extraction_json
        FROM fundamentals_events
        WHERE filing_type = 'capital_raise'
          AND structured_extraction_status = 'done'
          AND structured_extraction_json IS NOT NULL
        ORDER BY load_ts ASC NULLS LAST
        """
    )
    if events_df.empty:
        return []

    known_df = sql_to_df("SELECT investor_key FROM fundamentals_investor_classification")
    known_keys = set(known_df["investor_key"]) if not known_df.empty else set()

    candidates: list[dict] = []
    seen_this_batch: set[str] = set()
    for _, row in events_df.iterrows():
        try:
            extracted = json.loads(row["structured_extraction_json"])
        except (TypeError, ValueError):
            continue
        for raw_name in extracted.get("investor_names") or []:
            key = normalize_investor_key(raw_name)
            if not key or key in known_keys or key in seen_this_batch:
                continue
            seen_this_batch.add(key)
            candidates.append(
                {"investor_key": key, "investor_name_display": str(raw_name).strip(), "source": row["source"], "news_id": row["news_id"]}
            )
            if limit and len(candidates) >= limit:
                return candidates
    return candidates


def classify_investor(investor_name: str, *, model: str = DEFAULT_MODEL) -> dict:
    client = OpenAI(api_key=env("OPENAI_API_KEY"))
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": INVESTOR_CLASSIFICATION_SYSTEM_PROMPT},
            {"role": "user", "content": investor_name},
        ],
        response_format={"type": "json_schema", "json_schema": {"name": "investor_classification", "schema": INVESTOR_CLASSIFICATION_SCHEMA, "strict": True}},
    )
    return json.loads(response.choices[0].message.content)


def set_investor_override(investor_key: str, *, tier: str, notes: str | None = None) -> None:
    if tier not in INVESTOR_TIERS:
        raise ValueError(f"tier must be one of {INVESTOR_TIERS}, got {tier!r}")

    def _update() -> None:
        with db_session() as (_, cur):
            cur.execute(
                """
                UPDATE fundamentals_investor_classification
                   SET override_tier = %s, override_notes = %s, override_at = %s
                 WHERE investor_key = %s
                """,
                (tier, notes, pd.Timestamp.now(tz="UTC"), investor_key),
            )

    execute_db_operation(_update, operation_name="fundamentals_investor_classification:set_override")


def get_all_investor_classifications() -> pd.DataFrame:
    return sql_to_df("SELECT * FROM fundamentals_investor_classification ORDER BY load_ts DESC")


def run_investor_classification(*, limit: int | None = None, model: str = DEFAULT_MODEL) -> dict[str, object]:
    _ensure_investor_classification_table()

    candidates = load_unclassified_investor_names(limit or DEFAULT_BATCH_LIMIT)
    if not candidates:
        return {"classified": 0, "failed": 0, "blocked": False}

    counts = {"classified": 0, "failed": 0}
    consecutive_failures = 0
    blocked = False
    rows: list[dict] = []
    now = pd.Timestamp.now(tz="UTC")

    for candidate in candidates:
        if blocked:
            continue
        try:
            judgment = classify_investor(candidate["investor_name_display"], model=model)
            # 2026-08-15 bug found live: judgment["tier"]/["reasoning"] used to be indexed OUTSIDE
            # this try block -- a malformed/non-conforming LLM response (schema drift, a strict-mode
            # violation) would raise an uncaught KeyError there and abort the ENTIRE remaining batch
            # with no fallback-telemetry record, instead of being handled as this one name's failure
            # like every other error from this same call already is.
            llm_tier = judgment["tier"]
            llm_reasoning = judgment["reasoning"]
        except Exception as exc:  # noqa: BLE001 -- classified as a failure either way
            consecutive_failures += 1
            counts["failed"] += 1
            _record_fallback(
                "investor_classification_failed",
                reason="Investor classification LLM call failed for this name; it stays unclassified and can be retried next run.",
                error=exc,
                metadata={"investor_key": candidate["investor_key"]},
            )
            if consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
                blocked = True
                _record_fallback(
                    "investor_classification_circuit_breaker_tripped",
                    reason=f"{consecutive_failures} consecutive investor classification failures -- stopping this run immediately.",
                    error="circuit breaker",
                    severity="error",
                )
            continue

        consecutive_failures = 0
        counts["classified"] += 1
        rows.append(
            {
                "investor_key": candidate["investor_key"],
                "investor_name_display": candidate["investor_name_display"],
                "llm_tier": llm_tier,
                "llm_reasoning": llm_reasoning,
                "llm_model": model,
                "llm_prompt_version": PROMPT_VERSION,
                "llm_classified_at": now,
                "override_tier": None,
                "override_notes": None,
                "override_at": None,
                "first_seen_source": candidate["source"],
                "first_seen_news_id": candidate["news_id"],
                "load_ts": now,
            }
        )

    if rows:
        upsert_to_db(pd.DataFrame(rows), RESULTS_TABLE, unique_keys=["investor_key"])

    return {**counts, "blocked": blocked}


def main() -> int:
    global STOCKEY_RUN_STATE
    result = run_investor_classification()
    STOCKEY_RUN_STATE = {
        "source": SYNC_SOURCE_NAME,
        "rows": result["classified"],
        "rows_written": result["classified"],
        "failed": result["failed"],
        "blocked": result["blocked"],
        "fallback_used": bool(result["failed"] or result["blocked"]),
        "state_advanced": result["classified"] > 0,
        "status": "blocked" if result["blocked"] else "ok",
    }
    print(json.dumps(STOCKEY_RUN_STATE, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
