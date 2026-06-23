"""Persistence for LLM-direct trade decisions ([P-LLM-AUTH].4c).

Stores each `advisory/llm_decision_policy.decide()` result in `advisory_llm_decisions` (append-only
migration; never edit an applied migration) with its full provenance stamp, and adapts persisted
rows into the `advisory/llm_decision_monitor` (.3) record shape so the systematic-error detector can
consume them. Decisions are review-only: `broker_execution_allowed` is always False here.

Realized-outcome attachment (the matured benchmark-excess a decision is judged on) is intentionally
deferred to the live/labeling path (.5): until an outcome is attached, a decision maps to a
`matured=False` monitor record, which the .3 monitor correctly treats as not-yet-trustworthy.
"""

from __future__ import annotations

import json
from typing import Any

from utils.schema_migrations import apply_schema_migration

DECISIONS_TABLE = "advisory_llm_decisions"
SCHEMA_MIGRATION_ID = "20260623_advisory_llm_decisions_base"
SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {DECISIONS_TABLE} (
        decided_at TIMESTAMPTZ NOT NULL,
        asof_date TIMESTAMPTZ NOT NULL,
        symbol TEXT NOT NULL,
        proposed_action TEXT,
        conviction DOUBLE PRECISION,
        meets_data_grounding_for_live BOOLEAN,
        sufficiency_path TEXT,
        event_class TEXT,
        rationale TEXT,
        proposal_json TEXT,
        evidence_packet_json TEXT,
        decision_contract_json TEXT,
        sizing_plan_json TEXT,
        llm_status TEXT,
        llm_error TEXT,
        prompt_id TEXT,
        prompt_version TEXT,
        prompt_schema_version TEXT,
        llm_model TEXT,
        broker_execution_allowed BOOLEAN,
        authority_scope TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (asof_date, symbol, decided_at)
    )
    """,
]


def ensure_tables() -> None:
    apply_schema_migration(
        migration_id=SCHEMA_MIGRATION_ID,
        description="Create the LLM-direct trade decision persistence table (review-only, provenance-stamped).",
        statements=SCHEMA_STATEMENTS,
        metadata={"tables": [DECISIONS_TABLE], "authority_scope": "llm_decision_review_only"},
    )


def _event_class_of(result: dict[str, Any]) -> Any:
    packet = result.get("evidence_packet")
    if isinstance(packet, dict):
        event = packet.get("event_provenance")
        if isinstance(event, dict):
            return event.get("event_class")
    return None


def _decision_row(result: dict[str, Any], *, decided_at: Any) -> dict[str, Any]:
    """Map a decide() result (+ its evidence packet) to a persistable row. Pure."""
    contract = result.get("contract") or {}
    return {
        "decided_at": decided_at,
        "asof_date": result.get("asof_date"),
        "symbol": result.get("symbol"),
        "proposed_action": result.get("proposal", {}).get("action"),
        "conviction": result.get("proposal", {}).get("conviction"),
        "meets_data_grounding_for_live": bool(result.get("meets_data_grounding_for_live")),
        "sufficiency_path": result.get("sufficiency_path"),
        "event_class": _event_class_of(result),
        "rationale": result.get("proposal", {}).get("rationale"),
        "proposal_json": json.dumps(result.get("proposal"), default=str),
        "evidence_packet_json": json.dumps(result.get("evidence_packet"), default=str),
        "decision_contract_json": json.dumps(contract, default=str),
        "sizing_plan_json": json.dumps(result.get("sizing"), default=str),
        "llm_status": result.get("llm_status"),
        "llm_error": result.get("llm_error"),
        "prompt_id": result.get("prompt_id"),
        "prompt_version": result.get("prompt_version"),
        "prompt_schema_version": result.get("prompt_schema_version"),
        "llm_model": result.get("llm_model"),
        "broker_execution_allowed": False,  # review-only; the live bridge (.5) is separate
        "authority_scope": result.get("authority_scope") or "llm_decision_policy_review_only",
    }


def build_decision_rows(results: list[dict[str, Any]], *, decided_at: Any) -> list[dict[str, Any]]:
    return [_decision_row(result, decided_at=decided_at) for result in (results or []) if isinstance(result, dict)]


def persist_decisions(results: list[dict[str, Any]], *, decided_at: Any) -> int:
    """Persist decide() results to `advisory_llm_decisions`. Returns rows written."""
    import pandas as pd

    from utils.db import upsert_to_db

    rows = build_decision_rows(results, decided_at=decided_at)
    if not rows:
        return 0
    ensure_tables()
    frame = pd.DataFrame(rows)
    frame["load_ts"] = pd.Timestamp(decided_at)
    for column in ("decided_at", "asof_date", "load_ts"):
        frame[column] = pd.to_datetime(frame[column], utc=True, errors="coerce")
    upsert_to_db(frame, DECISIONS_TABLE, unique_keys=["asof_date", "symbol", "decided_at"], timescaledb_column="decided_at")
    return len(rows)


def decisions_to_monitor_records(
    decision_rows: list[dict[str, Any]],
    *,
    outcomes_by_key: dict[tuple[Any, Any], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Adapt persisted decision rows into `advisory/llm_decision_monitor` record dicts.

    An outcome (matured benchmark-excess) is attached by (symbol, decided_at) when available; absent
    that, the record is `matured=False` and the monitor treats it as not-yet-trustworthy. Pure.
    """
    outcomes_by_key = outcomes_by_key or {}
    records: list[dict[str, Any]] = []
    for row in (decision_rows or []):
        if not isinstance(row, dict):
            continue
        key = (row.get("symbol"), row.get("decided_at"))
        outcome = outcomes_by_key.get(key, {})
        records.append({
            "symbol": row.get("symbol"),
            "decided_at": row.get("decided_at"),
            "matured": bool(outcome.get("matured", False)),
            "proposed_action": row.get("proposed_action"),
            "event_class": row.get("event_class"),
            "sufficiency_path": row.get("sufficiency_path"),
            "realized_excess_after_cost": outcome.get("realized_excess_after_cost"),
            "resolved_beta_only": outcome.get("resolved_beta_only"),
        })
    return records


def build_decision_monitor_report(
    decision_rows: list[dict[str, Any]],
    *,
    outcomes_by_key: dict[tuple[Any, Any], dict[str, Any]] | None = None,
    **thresholds: Any,
) -> dict[str, Any]:
    """Run the .3 systematic-error monitor over persisted decisions (+ any attached outcomes)."""
    from advisory.llm_decision_monitor import build_llm_decision_monitor_report

    records = decisions_to_monitor_records(decision_rows, outcomes_by_key=outcomes_by_key)
    return build_llm_decision_monitor_report(records, **thresholds)
