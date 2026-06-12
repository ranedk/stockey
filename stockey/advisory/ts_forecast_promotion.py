from __future__ import annotations

import argparse
import json
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, Field

from advisory.fallback_telemetry import record_local_fallback_event
from advisory.ts_forecast_promotion_check import (
    DEFAULT_MAX_EXIT_CONFLICT_RATE,
    DEFAULT_MIN_AVG_COST_ADJUSTED_RETURN,
    DEFAULT_MIN_DISTINCT_DATES,
    DEFAULT_MIN_EVALUATED_TRADES,
    DEFAULT_MIN_LIFT_VS_MOMENTUM,
    DEFAULT_MIN_SYMBOLS,
    DEFAULT_MIN_WIN_RATE,
    build_promotion_check,
)
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration


REVIEWS_TABLE = "advisory_ts_forecast_promotion_reviews"
DECISIONS_TABLE = "advisory_ts_forecast_promotion_decisions"
TS_FORECAST_PROMOTION_SCHEMA_MIGRATION_ID = "20260612_advisory_ts_forecast_promotion_base"

TS_FORECAST_PROMOTION_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {REVIEWS_TABLE} (
        reviewed_at TIMESTAMPTZ NOT NULL,
        model_name TEXT NOT NULL,
        horizon_days BIGINT NOT NULL,
        evidence_from_date TIMESTAMPTZ,
        evidence_to_date TIMESTAMPTZ,
        promotion_check_json TEXT,
        group_evidence_json TEXT,
        llm_review_json TEXT,
        recommendation TEXT,
        confidence DOUBLE PRECISION,
        patch_json TEXT,
        review_model TEXT,
        review_status TEXT,
        review_error TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (reviewed_at, model_name, horizon_days)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {DECISIONS_TABLE} (
        decided_at TIMESTAMPTZ NOT NULL,
        reviewed_at TIMESTAMPTZ NOT NULL,
        model_name TEXT NOT NULL,
        horizon_days BIGINT NOT NULL,
        decision TEXT NOT NULL,
        operator_id TEXT,
        decision_reason TEXT,
        final_patch_json TEXT,
        review_snapshot_json TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (reviewed_at, model_name, horizon_days, decided_at)
    )
    """,
]

ManualDecision = Literal["approved", "rejected", "needs_more_data"]


class TsForecastPromotionReview(BaseModel):
    recommendation: Literal["promote_low_weight_review", "needs_more_data"]
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str = Field(min_length=10, max_length=900)
    reasons: list[str] = Field(default_factory=list)
    promotion_risks: list[str] = Field(default_factory=list)
    suggested_manual_checks: list[str] = Field(default_factory=list)
    proposed_patch: dict[str, Any] = Field(default_factory=dict)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def parse_jsonish(value: Any, default: Any, *, source: str = "ts_forecast_promotion_json") -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        if pd.isna(value):
            return default
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.ts_forecast_promotion",
            fallback_type="ts_forecast_promotion_json_missing_check_failed",
            source=source,
            severity="warn",
            reason="TS forecast promotion could not evaluate missingness for stored JSON and continued parsing.",
            error=exc,
            metadata={"value_type": type(value).__name__, "default_type": type(default).__name__},
        )
    try:
        return json.loads(str(value))
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.ts_forecast_promotion",
            fallback_type="ts_forecast_promotion_json_parse_failed",
            source=source,
            severity="warn",
            reason="TS forecast promotion could not parse stored JSON; using the existing default fallback.",
            error=exc,
            metadata={"source": source, "payload_length": len(str(value))},
        )
        return default


def ensure_tables() -> None:
    apply_schema_migration(
        migration_id=TS_FORECAST_PROMOTION_SCHEMA_MIGRATION_ID,
        description="Create advisory TS forecast promotion review and decision tables.",
        statements=TS_FORECAST_PROMOTION_SCHEMA_STATEMENTS,
        metadata={
            "tables": [REVIEWS_TABLE, DECISIONS_TABLE],
            "authority_scope": "manual_config_review_only",
        },
    )


def _timestamp_or_none(value: Any) -> pd.Timestamp | None:
    if value is None:
        return None
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.normalize()


def _number(value: Any) -> float | None:
    numeric = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric):
        return None
    return float(numeric)


def build_pending_patch(group: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": "config/advisory_setups.yaml",
        "mode": "manual_review_only",
        "operation": "add_ts_forecast_low_weight_review_rule",
        "model_name": group.get("model_name"),
        "horizon_days": group.get("horizon_days"),
        "rule_suggestion": {
            "ts_forecast_model_name": group.get("model_name"),
            "ts_forecast_horizon_days": group.get("horizon_days"),
            "minimum_evaluated_trades": group.get("evaluated_trades"),
            "minimum_win_rate": group.get("win_rate"),
            "minimum_avg_cost_adjusted_return": group.get("avg_cost_adjusted_return"),
            "minimum_lift_vs_momentum": group.get("lift_vs_momentum"),
            "maximum_exit_conflict_rate": group.get("exit_conflict_rate"),
            "authority": "review_input_only",
            "broker_execution_allowed": False,
        },
        "note": "Generated from TS forecast paper evidence. Review manually before allowing forecasts as any low-weight policy input.",
    }


def build_manual_patch_text(patch: dict[str, Any]) -> str:
    suggestion = patch.get("rule_suggestion") if isinstance(patch.get("rule_suggestion"), dict) else {}
    lines = [
        "# Manual patch guidance only. Do not apply without reviewing paper evidence across multiple runs.",
        "# Suggested review-only TS forecast input rule:",
        "ts_forecast_review_rules:",
        f"  - model_name: {json.dumps(suggestion.get('ts_forecast_model_name'), ensure_ascii=False, default=str)}",
        f"    horizon_days: {json.dumps(suggestion.get('ts_forecast_horizon_days'), ensure_ascii=False, default=str)}",
        f"    minimum_evaluated_trades: {json.dumps(suggestion.get('minimum_evaluated_trades'), ensure_ascii=False, default=str)}",
        f"    minimum_win_rate: {json.dumps(suggestion.get('minimum_win_rate'), ensure_ascii=False, default=str)}",
        f"    minimum_avg_cost_adjusted_return: {json.dumps(suggestion.get('minimum_avg_cost_adjusted_return'), ensure_ascii=False, default=str)}",
        f"    minimum_lift_vs_momentum: {json.dumps(suggestion.get('minimum_lift_vs_momentum'), ensure_ascii=False, default=str)}",
        f"    maximum_exit_conflict_rate: {json.dumps(suggestion.get('maximum_exit_conflict_rate'), ensure_ascii=False, default=str)}",
        "    authority: review_input_only",
        "    broker_execution_allowed: false",
    ]
    return "\n".join(lines)


def deterministic_review(group: dict[str, Any]) -> TsForecastPromotionReview:
    failed = list(group.get("failed_gates") or [])
    reasons: list[str] = []
    risks: list[str] = []
    if failed:
        reasons.append(f"Promotion gates failed: {', '.join(str(item) for item in failed)}.")
    else:
        reasons.append("Paper evidence passed all TS forecast promotion gates.")
    if (_number(group.get("exit_conflict_rate")) or 0.0) > 0:
        risks.append("Some forecast paper buys conflicted with advisory exit posture; inspect those cases before applying any rule.")
    if (_number(group.get("lift_vs_momentum")) or 0.0) < 0.02:
        risks.append("Lift versus naive momentum is positive but still modest; verify that it survives another evaluation window.")
    patch = build_pending_patch(group)
    recommendation: Literal["promote_low_weight_review", "needs_more_data"] = "needs_more_data" if failed else "promote_low_weight_review"
    return TsForecastPromotionReview(
        recommendation=recommendation,
        confidence=0.6 if recommendation == "promote_low_weight_review" else 0.4,
        summary=f"Deterministic TS forecast promotion review for {group.get('model_name')} at {group.get('horizon_days')} days.",
        reasons=reasons,
        promotion_risks=risks,
        suggested_manual_checks=[
            "Review the top winning and losing paper buys for data leakage or stale prices.",
            "Compare this model/horizon across at least two independent evaluation windows.",
            "Keep any approved TS forecast influence low-weight and review-only until it proves incremental lift in live decisions.",
            "Do not allow TS forecasts to create broker orders or override exit signals.",
        ],
        proposed_patch=patch,
    )


def _promotion_args(
    *,
    from_date: Any = None,
    to_date: Any = None,
    model_name: str | None = None,
    horizon_days: int | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        from_date=_timestamp_or_none(from_date),
        to_date=_timestamp_or_none(to_date),
        model_name=model_name,
        horizon_days=horizon_days,
        min_evaluated_trades=DEFAULT_MIN_EVALUATED_TRADES,
        min_win_rate=DEFAULT_MIN_WIN_RATE,
        min_avg_cost_adjusted_return=DEFAULT_MIN_AVG_COST_ADJUSTED_RETURN,
        min_lift_vs_momentum=DEFAULT_MIN_LIFT_VS_MOMENTUM,
        max_exit_conflict_rate=DEFAULT_MAX_EXIT_CONFLICT_RATE,
        min_distinct_dates=DEFAULT_MIN_DISTINCT_DATES,
        min_symbols=DEFAULT_MIN_SYMBOLS,
    )


def generate_promotion_review(
    *,
    model_name: str | None = None,
    horizon_days: int | None = None,
    from_date: Any = None,
    to_date: Any = None,
    persist: bool = True,
    allow_not_ready: bool = False,
) -> dict[str, Any]:
    check = build_promotion_check(_promotion_args(from_date=from_date, to_date=to_date, model_name=model_name, horizon_days=horizon_days))
    scorecard = check.get("scorecard") or {}
    group = scorecard.get("best_group") or {}
    if not group:
        raise ValueError("No TS forecast paper group is available for promotion review.")
    if not bool(group.get("ready_for_operator_review")) and not allow_not_ready:
        raise ValueError("TS forecast paper evidence has not passed promotion gates; no review row was created.")
    pending_patch = build_pending_patch(group)
    review = deterministic_review(group).model_copy(update={"proposed_patch": pending_patch})
    reviewed_at = pd.Timestamp.utcnow()
    result = {
        "status": "ok",
        "reviewed_at": reviewed_at,
        "model_name": str(group.get("model_name") or model_name or "").strip().lower(),
        "horizon_days": int(group.get("horizon_days") or horizon_days or 0),
        "evidence_from_date": group.get("from_date"),
        "evidence_to_date": group.get("to_date"),
        "promotion_check": check,
        "group_evidence": group,
        "pending_patch": pending_patch,
        "llm_review": review.model_dump(),
        "review_model": "deterministic_ts_forecast_v1",
        "review_status": "ok",
        "review_error": None,
        "recommendation": review.recommendation,
        "confidence": review.confidence,
        "applied": False,
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
                "model_name": result["model_name"],
                "horizon_days": result["horizon_days"],
                "evidence_from_date": result.get("evidence_from_date"),
                "evidence_to_date": result.get("evidence_to_date"),
                "promotion_check_json": json_dumps(result.get("promotion_check") or {}),
                "group_evidence_json": json_dumps(result.get("group_evidence") or {}),
                "llm_review_json": json_dumps(result.get("llm_review") or {}),
                "recommendation": result.get("recommendation") or (result.get("llm_review") or {}).get("recommendation"),
                "confidence": _number(result.get("confidence") or (result.get("llm_review") or {}).get("confidence")),
                "patch_json": json_dumps(result.get("pending_patch") or {}),
                "review_model": result["review_model"],
                "review_status": result["review_status"],
                "review_error": result["review_error"],
                "load_ts": pd.Timestamp.utcnow(),
            }
        ]
    )
    upsert_to_db(row, REVIEWS_TABLE, unique_keys=["reviewed_at", "model_name", "horizon_days"])


def load_promotion_reviews(limit: int = 25) -> list[dict[str, Any]]:
    ensure_tables()
    df = sql_to_df(
        f"""
        WITH latest_decision AS (
            SELECT DISTINCT ON (reviewed_at, model_name, horizon_days)
                reviewed_at,
                model_name,
                horizon_days,
                decision,
                operator_id,
                decision_reason,
                final_patch_json,
                decided_at
            FROM {DECISIONS_TABLE}
            ORDER BY reviewed_at, model_name, horizon_days, decided_at DESC
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
         AND d.model_name = r.model_name
         AND d.horizon_days = r.horizon_days
        ORDER BY r.reviewed_at DESC
        LIMIT %(limit)s
        """,
        params={"limit": max(1, int(limit))},
        retries=3,
    )
    if df.empty:
        return []
    out = df.copy()
    for col in ["promotion_check_json", "group_evidence_json", "llm_review_json", "patch_json", "final_patch_json"]:
        if col in out.columns:
            out[col.replace("_json", "")] = out[col].map(lambda value: parse_jsonish(value, {}))
    out["manual_patch_text"] = out.get("patch", pd.Series([{} for _ in range(len(out))])).map(build_manual_patch_text)
    out = out.astype(object).where(pd.notna(out), None)
    return out.to_dict(orient="records")


def find_review(*, reviewed_at: Any, model_name: str, horizon_days: int) -> dict[str, Any]:
    ensure_tables()
    parsed_reviewed_at = pd.to_datetime(reviewed_at, utc=True, errors="coerce")
    if pd.isna(parsed_reviewed_at):
        raise ValueError(f"Invalid reviewed_at: {reviewed_at}")
    normalized_model = str(model_name or "").strip().lower()
    if not normalized_model:
        raise ValueError("model_name is required")
    df = sql_to_df(
        f"""
        SELECT *
        FROM {REVIEWS_TABLE}
        WHERE reviewed_at = %(reviewed_at)s
          AND model_name = %(model_name)s
          AND horizon_days = %(horizon_days)s
        LIMIT 1
        """,
        params={
            "reviewed_at": parsed_reviewed_at,
            "model_name": normalized_model,
            "horizon_days": int(horizon_days),
        },
        retries=3,
    )
    if df.empty:
        raise ValueError(f"Unknown TS forecast promotion review: {reviewed_at}/{normalized_model}/{horizon_days}")
    row = df.iloc[0].to_dict()
    row["promotion_check"] = parse_jsonish(row.get("promotion_check_json"), {})
    row["group_evidence"] = parse_jsonish(row.get("group_evidence_json"), {})
    row["llm_review"] = parse_jsonish(row.get("llm_review_json"), {})
    row["patch"] = parse_jsonish(row.get("patch_json"), {})
    return row


def record_manual_decision(
    *,
    reviewed_at: Any,
    model_name: str,
    horizon_days: int,
    decision: ManualDecision,
    operator_id: str | None = None,
    decision_reason: str | None = None,
) -> dict[str, Any]:
    normalized_decision = str(decision or "").strip().lower()
    if normalized_decision not in {"approved", "rejected", "needs_more_data"}:
        raise ValueError("decision must be approved, rejected, or needs_more_data")
    review = find_review(reviewed_at=reviewed_at, model_name=model_name, horizon_days=horizon_days)
    decided_at = pd.Timestamp.utcnow()
    final_patch = dict(review.get("patch") or {})
    final_patch["mode"] = "manual_apply_required"
    final_patch["manual_decision"] = normalized_decision
    final_patch["manual_patch_text"] = build_manual_patch_text(final_patch)
    result = {
        "status": "ok",
        "decided_at": decided_at,
        "reviewed_at": review["reviewed_at"],
        "model_name": review["model_name"],
        "horizon_days": review["horizon_days"],
        "decision": normalized_decision,
        "operator_id": operator_id,
        "decision_reason": decision_reason,
        "final_patch": final_patch,
        "review": {
            "recommendation": review.get("recommendation"),
            "confidence": review.get("confidence"),
            "llm_review": review.get("llm_review") or {},
            "group_evidence": review.get("group_evidence") or {},
        },
        "applied": False,
        "note": "Decision recorded only. No config file, action rule, portfolio row, or broker behavior was changed.",
    }
    row = pd.DataFrame(
        [
            {
                "decided_at": decided_at,
                "reviewed_at": review["reviewed_at"],
                "model_name": review["model_name"],
                "horizon_days": review["horizon_days"],
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
    upsert_to_db(row, DECISIONS_TABLE, unique_keys=["reviewed_at", "model_name", "horizon_days", "decided_at"])
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate manual review guidance for TS forecast paper-evidence promotion.")
    parser.add_argument("--model-name")
    parser.add_argument("--horizon-days", type=int)
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-not-ready", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = generate_promotion_review(
        model_name=args.model_name,
        horizon_days=args.horizon_days,
        from_date=args.from_date,
        to_date=args.to_date,
        persist=not bool(args.dry_run),
        allow_not_ready=bool(args.allow_not_ready),
    )
    result["dry_run"] = bool(args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
