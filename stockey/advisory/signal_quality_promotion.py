from __future__ import annotations

import argparse
import json
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, Field

from advisory.signal_quality_evaluator import SUMMARY_TABLE
from utils.db import db_session, sql_to_df, upsert_to_db


REVIEWS_TABLE = "advisory_signal_quality_promotion_reviews"
DECISIONS_TABLE = "advisory_signal_quality_promotion_decisions"

ManualDecision = Literal["approved", "rejected", "needs_more_data"]


class SignalQualityPromotionReview(BaseModel):
    recommendation: Literal["promote_overlay_review", "reject", "needs_more_data"]
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str = Field(min_length=10, max_length=900)
    reasons: list[str] = Field(default_factory=list)
    promotion_risks: list[str] = Field(default_factory=list)
    suggested_manual_checks: list[str] = Field(default_factory=list)
    proposed_patch: dict[str, Any] = Field(default_factory=dict)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def parse_jsonish(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        if pd.isna(value):
            return default
    except Exception:
        pass
    try:
        return json.loads(str(value))
    except Exception:
        return default


def ensure_tables() -> None:
    with db_session() as (_, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {REVIEWS_TABLE} (
                reviewed_at TIMESTAMPTZ NOT NULL,
                evaluated_at TIMESTAMPTZ NOT NULL,
                horizon_days BIGINT NOT NULL,
                variant TEXT NOT NULL,
                signal_quality_evidence_json TEXT,
                coverage_json TEXT,
                llm_review_json TEXT,
                recommendation TEXT,
                confidence DOUBLE PRECISION,
                patch_json TEXT,
                review_model TEXT,
                review_status TEXT,
                review_error TEXT,
                load_ts TIMESTAMPTZ,
                UNIQUE (reviewed_at, evaluated_at, horizon_days, variant)
            )
            """
        )
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {DECISIONS_TABLE} (
                decided_at TIMESTAMPTZ NOT NULL,
                reviewed_at TIMESTAMPTZ NOT NULL,
                evaluated_at TIMESTAMPTZ NOT NULL,
                horizon_days BIGINT NOT NULL,
                variant TEXT NOT NULL,
                decision TEXT NOT NULL,
                operator_id TEXT,
                decision_reason TEXT,
                final_patch_json TEXT,
                review_snapshot_json TEXT,
                load_ts TIMESTAMPTZ,
                UNIQUE (reviewed_at, evaluated_at, horizon_days, variant, decided_at)
            )
            """
        )
        cur.execute(f"ALTER TABLE {DECISIONS_TABLE} ADD COLUMN IF NOT EXISTS final_patch_json TEXT")


def load_signal_quality_summary(*, evaluated_at: Any, horizon_days: int, variant: str) -> dict[str, Any]:
    parsed_evaluated_at = pd.to_datetime(evaluated_at, utc=True, errors="coerce")
    if pd.isna(parsed_evaluated_at):
        raise ValueError(f"Invalid evaluated_at: {evaluated_at}")
    normalized_variant = str(variant or "").strip()
    if not normalized_variant or normalized_variant == "technical_only":
        raise ValueError("variant must be a non-technical_only overlay variant")
    df = sql_to_df(
        f"""
        SELECT *
        FROM {SUMMARY_TABLE}
        WHERE evaluated_at = %(evaluated_at)s
          AND horizon_days = %(horizon_days)s
          AND variant = %(variant)s
        LIMIT 1
        """,
        params={"evaluated_at": parsed_evaluated_at, "horizon_days": int(horizon_days), "variant": normalized_variant},
        retries=3,
    )
    if df.empty:
        raise ValueError(f"Unknown signal-quality row: {evaluated_at}/{horizon_days}/{variant}")
    return df.iloc[0].to_dict()


def build_pending_patch(evidence: dict[str, Any]) -> dict[str, Any]:
    variant = str(evidence.get("variant") or "").strip()
    horizon_days = int(evidence.get("horizon_days") or 0)
    return {
        "path": "config/advisory_setups.yaml",
        "mode": "manual_review_only",
        "operation": "add_signal_quality_overlay_review_rule",
        "variant": variant,
        "horizon_days": horizon_days,
        "rule_suggestion": {
            "signal_quality_overlay": variant,
            "minimum_horizon_days": horizon_days,
            "minimum_matured_rows": evidence.get("matured_count"),
            "minimum_lift_vs_technical_only": evidence.get("lift_vs_technical_only"),
            "authority": "review_input_only",
            "broker_execution_allowed": False,
        },
        "note": "Generated from signal-quality evidence. Review manually before changing config or action-consolidation rules.",
    }


def build_manual_patch_text(patch: dict[str, Any]) -> str:
    suggestion = patch.get("rule_suggestion") if isinstance(patch.get("rule_suggestion"), dict) else {}
    lines = [
        "# Manual patch guidance only. Do not apply without reviewing signal-quality evidence across multiple runs.",
        "# Suggested review-only overlay influence rule:",
        "signal_quality_overlay_rules:",
        f"  - overlay: {json.dumps(suggestion.get('signal_quality_overlay'), ensure_ascii=False, default=str)}",
        f"    minimum_horizon_days: {json.dumps(suggestion.get('minimum_horizon_days'), ensure_ascii=False, default=str)}",
        f"    minimum_matured_rows: {json.dumps(suggestion.get('minimum_matured_rows'), ensure_ascii=False, default=str)}",
        f"    minimum_lift_vs_technical_only: {json.dumps(suggestion.get('minimum_lift_vs_technical_only'), ensure_ascii=False, default=str)}",
        "    authority: review_input_only",
        "    broker_execution_allowed: false",
    ]
    return "\n".join(lines)


def deterministic_review(evidence: dict[str, Any], coverage: dict[str, Any] | None = None) -> SignalQualityPromotionReview:
    matured = pd.to_numeric(evidence.get("matured_count"), errors="coerce")
    selected = pd.to_numeric(evidence.get("selected_count"), errors="coerce")
    lift = pd.to_numeric(evidence.get("lift_vs_technical_only"), errors="coerce")
    hit_rate = pd.to_numeric(evidence.get("hit_rate_after_cost"), errors="coerce")
    avg_return = pd.to_numeric(evidence.get("avg_forward_return_after_cost"), errors="coerce")
    matured_count = 0 if pd.isna(matured) else int(matured)
    selected_count = 0 if pd.isna(selected) else int(selected)
    reasons: list[str] = []
    risks: list[str] = []
    recommendation: Literal["promote_overlay_review", "reject", "needs_more_data"] = "needs_more_data"

    if matured_count < 30:
        reasons.append("Matured selected sample is below 30 rows.")
        risks.append("Overlay result may be dominated by a few symbols or one market regime.")
    if selected_count < 10:
        reasons.append("Selected overlay sample is below 10 rows.")
    if pd.notna(lift) and float(lift) <= 0:
        recommendation = "reject"
        reasons.append("Overlay does not improve average return after costs versus technical-only.")
    elif matured_count >= 30 and selected_count >= 10 and pd.notna(lift) and pd.notna(avg_return) and float(lift) >= 0.01 and float(avg_return) > 0:
        recommendation = "promote_overlay_review"
        reasons.append("Overlay has positive average return after costs and at least 1 percentage point lift versus technical-only.")
        if pd.notna(hit_rate) and float(hit_rate) < 0.45:
            risks.append("Hit rate is below 45%; improvement may come from a few large winners.")
    else:
        reasons.append("Evidence is mixed or insufficient for promotion.")

    def safe_int(value: Any) -> int:
        numeric = pd.to_numeric(value, errors="coerce")
        return 0 if pd.isna(numeric) else int(numeric)

    coverage = coverage or {}
    overlay_rows = max(
        safe_int(coverage.get("event_policy_rows")),
        safe_int(coverage.get("bhavcopy_rows")),
        safe_int(coverage.get("company_memory_rows")),
    )
    if overlay_rows < 10:
        risks.append("Overlay source coverage is below 10 rows in the latest run.")

    patch = build_pending_patch(evidence)
    return SignalQualityPromotionReview(
        recommendation=recommendation,
        confidence=0.55 if recommendation == "promote_overlay_review" else 0.45,
        summary=f"Deterministic signal-quality promotion review for {evidence.get('variant')} at horizon {evidence.get('horizon_days')}.",
        reasons=reasons,
        promotion_risks=risks,
        suggested_manual_checks=[
            "Compare this overlay result against at least one longer date range.",
            "Check whether lift is stable across 5/10/20-day horizons.",
            "Confirm the Trust Gate is usable or consciously treat the decision as review-only.",
            "Keep any promoted overlay as review input only until a separate action-consolidation rule is approved.",
        ],
        proposed_patch=patch,
    )


def generate_promotion_review(
    *,
    evaluated_at: Any,
    horizon_days: int,
    variant: str,
    coverage: dict[str, Any] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    evidence = load_signal_quality_summary(evaluated_at=evaluated_at, horizon_days=horizon_days, variant=variant)
    pending_patch = build_pending_patch(evidence)
    review = deterministic_review(evidence, coverage=coverage).model_copy(update={"proposed_patch": pending_patch})
    reviewed_at = pd.Timestamp.utcnow()
    result = {
        "status": "ok",
        "reviewed_at": reviewed_at,
        "evaluated_at": evidence.get("evaluated_at"),
        "horizon_days": int(evidence.get("horizon_days") or horizon_days),
        "variant": evidence.get("variant") or variant,
        "signal_quality_evidence": evidence,
        "coverage": coverage or {},
        "pending_patch": pending_patch,
        "llm_review": review.model_dump(),
        "review_model": "deterministic_signal_quality_v1",
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
                "variant": result["variant"],
                "signal_quality_evidence_json": json_dumps(result["signal_quality_evidence"]),
                "coverage_json": json_dumps(result["coverage"]),
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
    upsert_to_db(row, REVIEWS_TABLE, unique_keys=["reviewed_at", "evaluated_at", "horizon_days", "variant"])


def load_promotion_reviews(limit: int = 25) -> list[dict[str, Any]]:
    ensure_tables()
    df = sql_to_df(
        f"""
        WITH latest_decision AS (
            SELECT DISTINCT ON (reviewed_at, evaluated_at, horizon_days, variant)
                reviewed_at,
                evaluated_at,
                horizon_days,
                variant,
                decision,
                operator_id,
                decision_reason,
                final_patch_json,
                decided_at
            FROM {DECISIONS_TABLE}
            ORDER BY reviewed_at, evaluated_at, horizon_days, variant, decided_at DESC
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
         AND d.variant = r.variant
        ORDER BY r.reviewed_at DESC
        LIMIT %(limit)s
        """,
        params={"limit": max(1, int(limit))},
        retries=3,
    )
    if df.empty:
        return []
    out = df.copy()
    for col in ["signal_quality_evidence_json", "coverage_json", "llm_review_json", "patch_json", "final_patch_json"]:
        if col in out.columns:
            out[col.replace("_json", "")] = out[col].map(lambda value: parse_jsonish(value, {}))
    out["manual_patch_text"] = out.get("patch", pd.Series([{} for _ in range(len(out))])).map(build_manual_patch_text)
    out = out.astype(object).where(pd.notna(out), None)
    return out.to_dict(orient="records")


def find_review(*, reviewed_at: Any, evaluated_at: Any, horizon_days: int, variant: str) -> dict[str, Any]:
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
          AND variant = %(variant)s
        LIMIT 1
        """,
        params={
            "reviewed_at": parsed_reviewed_at,
            "evaluated_at": parsed_evaluated_at,
            "horizon_days": int(horizon_days),
            "variant": str(variant),
        },
        retries=3,
    )
    if df.empty:
        raise ValueError(f"Unknown signal-quality promotion review: {evaluated_at}/{horizon_days}/{variant}/{reviewed_at}")
    row = df.iloc[0].to_dict()
    row["signal_quality_evidence"] = parse_jsonish(row.get("signal_quality_evidence_json"), {})
    row["coverage"] = parse_jsonish(row.get("coverage_json"), {})
    row["llm_review"] = parse_jsonish(row.get("llm_review_json"), {})
    row["patch"] = parse_jsonish(row.get("patch_json"), {})
    return row


def record_manual_decision(
    *,
    reviewed_at: Any,
    evaluated_at: Any,
    horizon_days: int,
    variant: str,
    decision: ManualDecision,
    operator_id: str | None = None,
    decision_reason: str | None = None,
) -> dict[str, Any]:
    normalized_decision = str(decision or "").strip().lower()
    if normalized_decision not in {"approved", "rejected", "needs_more_data"}:
        raise ValueError("decision must be approved, rejected, or needs_more_data")
    review = find_review(reviewed_at=reviewed_at, evaluated_at=evaluated_at, horizon_days=horizon_days, variant=variant)
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
        "variant": review["variant"],
        "decision": normalized_decision,
        "operator_id": operator_id,
        "decision_reason": decision_reason,
        "final_patch": final_patch,
        "review": {
            "recommendation": review.get("recommendation"),
            "confidence": review.get("confidence"),
            "llm_review": review.get("llm_review") or {},
            "signal_quality_evidence": review.get("signal_quality_evidence") or {},
        },
        "applied": False,
        "note": "Decision recorded only. No config file, action rule, or trading behavior was changed.",
    }
    row = pd.DataFrame(
        [
            {
                "decided_at": decided_at,
                "reviewed_at": review["reviewed_at"],
                "evaluated_at": review["evaluated_at"],
                "horizon_days": review["horizon_days"],
                "variant": review["variant"],
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
    upsert_to_db(row, DECISIONS_TABLE, unique_keys=["reviewed_at", "evaluated_at", "horizon_days", "variant", "decided_at"])
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate manual review guidance for signal-quality overlay promotion.")
    parser.add_argument("--evaluated-at", required=True)
    parser.add_argument("--horizon-days", type=int, required=True)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = generate_promotion_review(
        evaluated_at=args.evaluated_at,
        horizon_days=int(args.horizon_days),
        variant=args.variant,
        persist=not bool(args.dry_run),
    )
    result["dry_run"] = bool(args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
