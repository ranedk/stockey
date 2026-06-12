from __future__ import annotations

import argparse
import json
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, Field

from advisory.event_policy_evaluator import SUMMARY_TABLE
from advisory.fallback_telemetry import record_local_fallback_event
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration


REVIEWS_TABLE = "advisory_event_policy_promotion_reviews"
DECISIONS_TABLE = "advisory_event_policy_promotion_decisions"
EVENT_POLICY_PROMOTION_SCHEMA_MIGRATION_ID = "20260611_advisory_event_policy_promotion_base"

EVENT_POLICY_PROMOTION_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {REVIEWS_TABLE} (
        reviewed_at TIMESTAMPTZ NOT NULL,
        evaluated_at TIMESTAMPTZ NOT NULL,
        horizon_days BIGINT NOT NULL,
        group_type TEXT NOT NULL,
        group_value TEXT NOT NULL,
        event_policy_evidence_json TEXT,
        llm_review_json TEXT,
        recommendation TEXT,
        confidence DOUBLE PRECISION,
        patch_json TEXT,
        review_model TEXT,
        review_status TEXT,
        review_error TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (reviewed_at, evaluated_at, horizon_days, group_type, group_value)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {DECISIONS_TABLE} (
        decided_at TIMESTAMPTZ NOT NULL,
        reviewed_at TIMESTAMPTZ NOT NULL,
        evaluated_at TIMESTAMPTZ NOT NULL,
        horizon_days BIGINT NOT NULL,
        group_type TEXT NOT NULL,
        group_value TEXT NOT NULL,
        decision TEXT NOT NULL,
        operator_id TEXT,
        decision_reason TEXT,
        final_patch_json TEXT,
        review_snapshot_json TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (reviewed_at, evaluated_at, horizon_days, group_type, group_value, decided_at)
    )
    """,
]

ManualDecision = Literal["approved", "rejected", "needs_more_data"]


class EventPolicyPromotionReview(BaseModel):
    recommendation: Literal["promote_review_rule", "tighten_or_downgrade", "reject", "needs_more_data"]
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str = Field(min_length=10, max_length=900)
    reasons: list[str] = Field(default_factory=list)
    promotion_risks: list[str] = Field(default_factory=list)
    suggested_manual_checks: list[str] = Field(default_factory=list)
    proposed_patch: dict[str, Any] = Field(default_factory=dict)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def parse_jsonish(value: Any, default: Any, *, source: str = "event_policy_promotion_json") -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        if pd.isna(value):
            return default
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.event_policy_promotion",
            fallback_type="event_policy_promotion_json_missing_check_failed",
            source=source,
            severity="warn",
            reason="Event-policy promotion could not evaluate missingness for stored JSON and continued parsing.",
            error=exc,
            metadata={"value_type": type(value).__name__, "default_type": type(default).__name__},
        )
    try:
        return json.loads(str(value))
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.event_policy_promotion",
            fallback_type="event_policy_promotion_json_parse_failed",
            source=source,
            severity="warn",
            reason="Event-policy promotion could not parse stored JSON; using the existing default fallback.",
            error=exc,
            metadata={"source": source, "payload_length": len(str(value))},
        )
        return default


def ensure_tables() -> None:
    apply_schema_migration(
        migration_id=EVENT_POLICY_PROMOTION_SCHEMA_MIGRATION_ID,
        description="Create advisory event-policy promotion review and decision tables.",
        statements=EVENT_POLICY_PROMOTION_SCHEMA_STATEMENTS,
        metadata={
            "tables": [REVIEWS_TABLE, DECISIONS_TABLE],
            "authority_scope": "manual_config_review_only",
        },
    )


def _record_promotion_source_failure(
    *,
    fallback_type: str,
    source: str,
    reason: str,
    error: Exception,
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="advisory.event_policy_promotion",
        fallback_type=fallback_type,
        source=source,
        severity="warn",
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def load_event_policy_summary(*, evaluated_at: Any, horizon_days: int, group_type: str, group_value: str) -> dict[str, Any]:
    parsed_evaluated_at = pd.to_datetime(evaluated_at, utc=True, errors="coerce")
    if pd.isna(parsed_evaluated_at):
        raise ValueError(f"Invalid evaluated_at: {evaluated_at}")
    normalized_group_type = str(group_type or "").strip()
    normalized_group_value = str(group_value or "").strip()
    if not normalized_group_type or not normalized_group_value:
        raise ValueError("group_type and group_value are required")
    params = {
        "evaluated_at": parsed_evaluated_at,
        "horizon_days": int(horizon_days),
        "group_type": normalized_group_type,
        "group_value": normalized_group_value,
    }
    try:
        df = sql_to_df(
            f"""
            SELECT *
            FROM {SUMMARY_TABLE}
            WHERE evaluated_at = %(evaluated_at)s
              AND horizon_days = %(horizon_days)s
              AND group_type = %(group_type)s
              AND group_value = %(group_value)s
            LIMIT 1
            """,
            params=params,
            retries=3,
        )
    except Exception as exc:
        _record_promotion_source_failure(
            fallback_type="event_policy_promotion_summary_load_failed",
            source=SUMMARY_TABLE,
            reason="Event-policy promotion could not load summary evidence for review.",
            error=exc,
            metadata={key: str(value) for key, value in params.items()},
        )
        raise
    if df.empty:
        raise ValueError(f"Unknown event-policy summary row: {evaluated_at}/{horizon_days}/{group_type}/{group_value}")
    return df.iloc[0].to_dict()


def build_pending_patch(evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": "config/advisory_setups.yaml",
        "mode": "manual_review_only",
        "operation": "add_event_policy_review_rule",
        "group_type": evidence.get("group_type"),
        "group_value": evidence.get("group_value"),
        "horizon_days": evidence.get("horizon_days"),
        "rule_suggestion": {
            "event_policy_group_type": evidence.get("group_type"),
            "event_policy_group_value": evidence.get("group_value"),
            "minimum_horizon_days": evidence.get("horizon_days"),
            "minimum_matured_rows": evidence.get("matured_count"),
            "minimum_avg_forward_return_after_cost": evidence.get("avg_forward_return_after_cost"),
            "minimum_hit_rate_after_cost": evidence.get("hit_rate_after_cost"),
            "authority": "review_input_only",
            "broker_execution_allowed": False,
        },
        "note": "Generated from event-policy evaluation evidence. Review manually before changing config or action-consolidation rules.",
    }


def build_manual_patch_text(patch: dict[str, Any]) -> str:
    suggestion = patch.get("rule_suggestion") if isinstance(patch.get("rule_suggestion"), dict) else {}
    lines = [
        "# Manual patch guidance only. Do not apply without reviewing event-policy evidence across multiple runs.",
        "# Suggested review-only event-policy influence rule:",
        "event_policy_review_rules:",
        f"  - group_type: {json.dumps(suggestion.get('event_policy_group_type'), ensure_ascii=False, default=str)}",
        f"    group_value: {json.dumps(suggestion.get('event_policy_group_value'), ensure_ascii=False, default=str)}",
        f"    minimum_horizon_days: {json.dumps(suggestion.get('minimum_horizon_days'), ensure_ascii=False, default=str)}",
        f"    minimum_matured_rows: {json.dumps(suggestion.get('minimum_matured_rows'), ensure_ascii=False, default=str)}",
        f"    minimum_avg_forward_return_after_cost: {json.dumps(suggestion.get('minimum_avg_forward_return_after_cost'), ensure_ascii=False, default=str)}",
        f"    minimum_hit_rate_after_cost: {json.dumps(suggestion.get('minimum_hit_rate_after_cost'), ensure_ascii=False, default=str)}",
        "    authority: review_input_only",
        "    broker_execution_allowed: false",
    ]
    return "\n".join(lines)


def deterministic_review(evidence: dict[str, Any]) -> EventPolicyPromotionReview:
    matured = pd.to_numeric(evidence.get("matured_count"), errors="coerce")
    hit_rate = pd.to_numeric(evidence.get("hit_rate_after_cost"), errors="coerce")
    avg_after_cost = pd.to_numeric(evidence.get("avg_forward_return_after_cost"), errors="coerce")
    group_type = str(evidence.get("group_type") or "")
    group_value = str(evidence.get("group_value") or "")
    recommendation: Literal["promote_review_rule", "tighten_or_downgrade", "reject", "needs_more_data"] = "needs_more_data"
    reasons: list[str] = []
    risks: list[str] = []
    matured_count = 0 if pd.isna(matured) else int(matured)

    if matured_count < 30:
        reasons.append("Matured sample is below 30 rows.")
        risks.append("Event-policy result may be dominated by one symbol, event type, or market regime.")
    if pd.notna(avg_after_cost) and pd.notna(hit_rate):
        if matured_count >= 30 and float(avg_after_cost) > 0.0 and float(hit_rate) >= 0.50:
            recommendation = "promote_review_rule"
            reasons.append("Group has positive average forward return after costs and hit rate at or above 50%.")
        elif matured_count >= 30 and float(avg_after_cost) < 0.0 and float(hit_rate) < 0.40:
            recommendation = "tighten_or_downgrade"
            reasons.append("Group has negative average forward return after costs and hit rate below 40%.")
        elif matured_count >= 30:
            recommendation = "reject"
            reasons.append("Evidence is not strong enough to justify a review-rule change.")
    else:
        reasons.append("Forward-return evidence is incomplete.")
    if group_type in {"source_quality", "source_family", "source_authority", "source_confirmation_required", "market_scope"}:
        risks.append("This is an actionability-context rule; keep it review-only until operator feedback confirms usefulness.")

    patch = build_pending_patch(evidence)
    return EventPolicyPromotionReview(
        recommendation=recommendation,
        confidence=0.6 if recommendation in {"promote_review_rule", "tighten_or_downgrade"} else 0.45,
        summary=f"Deterministic event-policy promotion review for {group_type}={group_value} at horizon {evidence.get('horizon_days')}.",
        reasons=reasons,
        promotion_risks=risks,
        suggested_manual_checks=[
            "Compare this group across 5/10/20-day horizons.",
            "Confirm sample is not concentrated in one symbol, sector, or date cluster.",
            "Check recent operator Manual Review decisions for agreement or contradiction.",
            "Keep any promoted rule as review input only until a separate action-consolidation rule is approved.",
        ],
        proposed_patch=patch,
    )


def generate_promotion_review(
    *,
    evaluated_at: Any,
    horizon_days: int,
    group_type: str,
    group_value: str,
    persist: bool = True,
) -> dict[str, Any]:
    evidence = load_event_policy_summary(
        evaluated_at=evaluated_at,
        horizon_days=horizon_days,
        group_type=group_type,
        group_value=group_value,
    )
    pending_patch = build_pending_patch(evidence)
    review = deterministic_review(evidence).model_copy(update={"proposed_patch": pending_patch})
    reviewed_at = pd.Timestamp.utcnow()
    result = {
        "status": "ok",
        "reviewed_at": reviewed_at,
        "evaluated_at": evidence.get("evaluated_at"),
        "horizon_days": int(evidence.get("horizon_days") or horizon_days),
        "group_type": evidence.get("group_type") or group_type,
        "group_value": evidence.get("group_value") or group_value,
        "event_policy_evidence": evidence,
        "pending_patch": pending_patch,
        "llm_review": review.model_dump(),
        "review_model": "deterministic_event_policy_v1",
        "review_status": "ok",
        "review_error": None,
    }
    if persist:
        persist_review(result)
    return result


def persist_review(result: dict[str, Any]) -> None:
    ensure_tables()
    row = pd.DataFrame(
        [
            {
                "reviewed_at": result["reviewed_at"],
                "evaluated_at": result["evaluated_at"],
                "horizon_days": result["horizon_days"],
                "group_type": result["group_type"],
                "group_value": result["group_value"],
                "event_policy_evidence_json": json_dumps(result["event_policy_evidence"]),
                "llm_review_json": json_dumps(result["llm_review"]),
                "recommendation": result["llm_review"].get("recommendation"),
                "confidence": result["llm_review"].get("confidence"),
                "patch_json": json_dumps(result["pending_patch"]),
                "review_model": result["review_model"],
                "review_status": result["review_status"],
                "review_error": result["review_error"],
                "load_ts": pd.Timestamp.utcnow(),
            }
        ]
    )
    upsert_to_db(row, REVIEWS_TABLE, unique_keys=["reviewed_at", "evaluated_at", "horizon_days", "group_type", "group_value"])


def load_promotion_reviews(limit: int = 25) -> list[dict[str, Any]]:
    ensure_tables()
    df = sql_to_df(
        f"""
        WITH latest_decision AS (
            SELECT DISTINCT ON (reviewed_at, evaluated_at, horizon_days, group_type, group_value)
                reviewed_at,
                evaluated_at,
                horizon_days,
                group_type,
                group_value,
                decision,
                operator_id,
                decision_reason,
                final_patch_json,
                decided_at
            FROM {DECISIONS_TABLE}
            ORDER BY reviewed_at, evaluated_at, horizon_days, group_type, group_value, decided_at DESC
        )
        SELECT
            r.*,
            d.decision AS manual_decision,
            d.operator_id AS manual_operator_id,
            d.decision_reason AS manual_decision_reason,
            d.final_patch_json AS final_patch_json,
            d.decided_at AS manual_decided_at
        FROM {REVIEWS_TABLE} r
        LEFT JOIN latest_decision d
          ON d.reviewed_at = r.reviewed_at
         AND d.evaluated_at = r.evaluated_at
         AND d.horizon_days = r.horizon_days
         AND d.group_type = r.group_type
         AND d.group_value = r.group_value
        ORDER BY r.reviewed_at DESC
        LIMIT %(limit)s
        """,
        params={"limit": max(1, int(limit))},
        retries=3,
    )
    if df.empty:
        return []
    out = df.copy()
    for col in ["event_policy_evidence_json", "llm_review_json", "patch_json", "final_patch_json"]:
        if col in out.columns:
            out[col.replace("_json", "")] = out[col].map(lambda value: parse_jsonish(value, {}))
    out["manual_patch_text"] = out.get("patch", pd.Series([{} for _ in range(len(out))])).map(build_manual_patch_text)
    out = out.astype(object).where(pd.notna(out), None)
    return out.to_dict(orient="records")


def find_review(*, reviewed_at: Any, evaluated_at: Any, horizon_days: int, group_type: str, group_value: str) -> dict[str, Any]:
    ensure_tables()
    parsed_reviewed_at = pd.to_datetime(reviewed_at, utc=True, errors="coerce")
    parsed_evaluated_at = pd.to_datetime(evaluated_at, utc=True, errors="coerce")
    if pd.isna(parsed_reviewed_at):
        raise ValueError(f"Invalid reviewed_at: {reviewed_at}")
    if pd.isna(parsed_evaluated_at):
        raise ValueError(f"Invalid evaluated_at: {evaluated_at}")
    df = sql_to_df(
        f"""
        SELECT *
        FROM {REVIEWS_TABLE}
        WHERE reviewed_at = %(reviewed_at)s
          AND evaluated_at = %(evaluated_at)s
          AND horizon_days = %(horizon_days)s
          AND group_type = %(group_type)s
          AND group_value = %(group_value)s
        LIMIT 1
        """,
        params={
            "reviewed_at": parsed_reviewed_at,
            "evaluated_at": parsed_evaluated_at,
            "horizon_days": int(horizon_days),
            "group_type": str(group_type),
            "group_value": str(group_value),
        },
        retries=3,
    )
    if df.empty:
        raise ValueError(f"Unknown event-policy promotion review: {evaluated_at}/{horizon_days}/{group_type}/{group_value}/{reviewed_at}")
    row = df.iloc[0].to_dict()
    row["event_policy_evidence"] = parse_jsonish(row.get("event_policy_evidence_json"), {})
    row["llm_review"] = parse_jsonish(row.get("llm_review_json"), {})
    row["patch"] = parse_jsonish(row.get("patch_json"), {})
    return row


def record_manual_decision(
    *,
    reviewed_at: Any,
    evaluated_at: Any,
    horizon_days: int,
    group_type: str,
    group_value: str,
    decision: ManualDecision,
    operator_id: str | None = None,
    decision_reason: str | None = None,
) -> dict[str, Any]:
    normalized_decision = str(decision or "").strip().lower()
    if normalized_decision not in {"approved", "rejected", "needs_more_data"}:
        raise ValueError("decision must be approved, rejected, or needs_more_data")
    review = find_review(
        reviewed_at=reviewed_at,
        evaluated_at=evaluated_at,
        horizon_days=horizon_days,
        group_type=group_type,
        group_value=group_value,
    )
    decided_at = pd.Timestamp.utcnow()
    final_patch = dict(review.get("patch") or {})
    final_patch["mode"] = "manual_apply_required"
    final_patch["manual_decision"] = normalized_decision
    final_patch["manual_patch_text"] = build_manual_patch_text(final_patch)
    result = {
        "status": "ok",
        "decided_at": decided_at,
        "reviewed_at": review["reviewed_at"],
        "evaluated_at": review["evaluated_at"],
        "horizon_days": review["horizon_days"],
        "group_type": review["group_type"],
        "group_value": review["group_value"],
        "decision": normalized_decision,
        "operator_id": operator_id,
        "decision_reason": decision_reason,
        "final_patch": final_patch,
        "review": {
            "recommendation": review.get("recommendation"),
            "confidence": review.get("confidence"),
            "llm_review": review.get("llm_review") or {},
            "event_policy_evidence": review.get("event_policy_evidence") or {},
        },
        "applied": False,
        "note": "Decision recorded only. No config file, action rule, portfolio row, or trading behavior was changed.",
    }
    row = pd.DataFrame(
        [
            {
                "decided_at": decided_at,
                "reviewed_at": review["reviewed_at"],
                "evaluated_at": review["evaluated_at"],
                "horizon_days": review["horizon_days"],
                "group_type": review["group_type"],
                "group_value": review["group_value"],
                "decision": normalized_decision,
                "operator_id": operator_id,
                "decision_reason": decision_reason,
                "final_patch_json": json_dumps(final_patch),
                "review_snapshot_json": json_dumps(result["review"]),
                "load_ts": pd.Timestamp.utcnow(),
            }
        ]
    )
    ensure_tables()
    upsert_to_db(row, DECISIONS_TABLE, unique_keys=["reviewed_at", "evaluated_at", "horizon_days", "group_type", "group_value", "decided_at"])
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate manual review guidance for event-policy evaluation group promotion.")
    parser.add_argument("--evaluated-at", required=True)
    parser.add_argument("--horizon-days", type=int, required=True)
    parser.add_argument("--group-type", required=True)
    parser.add_argument("--group-value", required=True)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = generate_promotion_review(
        evaluated_at=args.evaluated_at,
        horizon_days=int(args.horizon_days),
        group_type=args.group_type,
        group_value=args.group_value,
        persist=not bool(args.dry_run),
    )
    result["dry_run"] = bool(args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
