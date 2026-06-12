from __future__ import annotations

import argparse
import json
from typing import Any, Literal

import pandas as pd
from environs import Env
from pydantic import BaseModel, Field

from advisory.fallback_telemetry import record_fallback_event, record_local_fallback_event
from advisory.prompt_registry import prompt_version as registry_prompt_version
from advisory.prompt_registry import response_schema_version
from advisory.setup_registry import load_setup_registry
from advisory.technical_threshold_calibration import EVALUATIONS_TABLE, SUMMARY_TABLE
from utils.codex_cli import run_codex_structured
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration


env = Env()
env.read_env()

REVIEWS_TABLE = "advisory_technical_threshold_promotion_reviews"
DECISIONS_TABLE = "advisory_technical_threshold_promotion_decisions"
TECHNICAL_THRESHOLD_PROMOTION_SCHEMA_MIGRATION_ID = "20260611_advisory_technical_threshold_promotion_base"
DEFAULT_PROMOTION_REVIEW_MODEL = env("TECHNICAL_THRESHOLD_PROMOTION_REVIEW_MODEL", default="codex")
PROMPT_ID = "technical_threshold_promotion_review"
PROMPT_VERSION = registry_prompt_version(PROMPT_ID)
PROMPT_SCHEMA_VERSION = response_schema_version(PROMPT_ID)
TECHNICAL_THRESHOLD_PROMOTION_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {REVIEWS_TABLE} (
        reviewed_at TIMESTAMPTZ NOT NULL,
        setup_id TEXT NOT NULL,
        config_id TEXT NOT NULL,
        horizon_days BIGINT,
        evaluated_at TIMESTAMPTZ,
        current_thresholds_json TEXT,
        candidate_thresholds_json TEXT,
        calibration_evidence_json TEXT,
        llm_review_json TEXT,
        recommendation TEXT,
        confidence DOUBLE PRECISION,
        patch_json TEXT,
        prompt_id TEXT,
        prompt_version TEXT,
        prompt_schema_version TEXT,
        review_model TEXT,
        review_status TEXT,
        review_error TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (setup_id, config_id, reviewed_at)
    )
    """,
    f"ALTER TABLE {REVIEWS_TABLE} ADD COLUMN IF NOT EXISTS prompt_id TEXT",
    f"ALTER TABLE {REVIEWS_TABLE} ADD COLUMN IF NOT EXISTS prompt_version TEXT",
    f"ALTER TABLE {REVIEWS_TABLE} ADD COLUMN IF NOT EXISTS prompt_schema_version TEXT",
    f"""
    CREATE TABLE IF NOT EXISTS {DECISIONS_TABLE} (
        decided_at TIMESTAMPTZ NOT NULL,
        reviewed_at TIMESTAMPTZ NOT NULL,
        setup_id TEXT NOT NULL,
        config_id TEXT NOT NULL,
        decision TEXT NOT NULL,
        operator_id TEXT,
        decision_reason TEXT,
        final_patch_json TEXT,
        review_snapshot_json TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (reviewed_at, setup_id, config_id, decided_at)
    )
    """,
]


class TechnicalThresholdPromotionReview(BaseModel):
    recommendation: Literal["promote", "promote_partially", "reject", "needs_more_data"]
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str = Field(min_length=10, max_length=800)
    reasons: list[str] = Field(default_factory=list)
    overfit_risks: list[str] = Field(default_factory=list)
    threshold_changes: list[str] = Field(default_factory=list)
    suggested_manual_checks: list[str] = Field(default_factory=list)
    proposed_patch: dict[str, Any] = Field(default_factory=dict)


ManualDecision = Literal["approved", "rejected", "needs_more_data"]


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def ensure_tables() -> None:
    apply_schema_migration(
        migration_id=TECHNICAL_THRESHOLD_PROMOTION_SCHEMA_MIGRATION_ID,
        description="Create advisory technical-threshold promotion review and decision tables.",
        statements=TECHNICAL_THRESHOLD_PROMOTION_SCHEMA_STATEMENTS,
        metadata={"tables": [REVIEWS_TABLE, DECISIONS_TABLE], "authority_scope": "manual_config_review_only"},
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
        module="advisory.technical_threshold_promotion",
        fallback_type=fallback_type,
        source=source,
        severity="warn",
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def parse_jsonish(value: Any, default: Any, *, source: str = "technical_threshold_promotion_json") -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        if pd.isna(value):
            return default
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.technical_threshold_promotion",
            fallback_type="technical_threshold_promotion_json_missing_check_failed",
            source=source,
            severity="warn",
            reason="Technical-threshold promotion could not evaluate missingness for stored JSON and continued parsing.",
            error=exc,
            metadata={"source": source, "value_type": type(value).__name__, "default_type": type(default).__name__},
        )
    try:
        return json.loads(str(value))
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.technical_threshold_promotion",
            fallback_type="technical_threshold_promotion_json_parse_failed",
            source=source,
            severity="warn",
            reason="Technical-threshold promotion could not parse stored JSON; using the existing default fallback.",
            error=exc,
            metadata={"source": source, "payload_length": len(str(value))},
        )
        return default


def load_setup(setup_id: str) -> dict[str, Any]:
    normalized = str(setup_id or "").strip().upper()
    for setup in load_setup_registry():
        if str(setup.get("setup_id") or "").strip().upper() == normalized:
            return setup
    raise ValueError(f"Unknown setup_id: {setup_id}")


def load_calibration_config(config_id: str) -> dict[str, Any]:
    try:
        df = sql_to_df(
            f"""
            SELECT *
            FROM {EVALUATIONS_TABLE}
            WHERE config_id = %s
            ORDER BY evaluated_at DESC
            LIMIT 1
            """,
            params=(str(config_id),),
            retries=3,
        )
    except Exception as exc:
        _record_promotion_source_failure(
            fallback_type="technical_threshold_promotion_calibration_load_failed",
            source=EVALUATIONS_TABLE,
            reason="Technical-threshold promotion could not load calibration evidence for review.",
            error=exc,
            metadata={"config_id": str(config_id)},
        )
        raise
    if df.empty:
        raise ValueError(f"Unknown calibration config_id: {config_id}")
    return df.iloc[0].to_dict()


def load_horizon_summary(horizon_days: int, evaluated_at: Any) -> dict[str, Any]:
    try:
        df = sql_to_df(
            f"""
            SELECT *
            FROM {SUMMARY_TABLE}
            WHERE horizon_days = %s
              AND evaluated_at = %s
            LIMIT 1
            """,
            params=(int(horizon_days), evaluated_at),
            retries=3,
        )
    except Exception as exc:
        _record_promotion_source_failure(
            fallback_type="technical_threshold_promotion_summary_load_failed",
            source=SUMMARY_TABLE,
            reason="Technical-threshold promotion could not load horizon summary context for review.",
            error=exc,
            metadata={"horizon_days": int(horizon_days), "evaluated_at": str(evaluated_at)},
        )
        raise
    return df.iloc[0].to_dict() if not df.empty else {}


def current_thresholds_for_setup(setup: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(setup.get("technical_thresholds"), dict):
        out.update(setup["technical_thresholds"])
    if isinstance(setup.get("score_thresholds"), dict):
        out["score_thresholds"] = setup["score_thresholds"]
    return out


def build_pending_patch(setup_id: str, candidate_thresholds: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": "config/advisory_setups.yaml",
        "mode": "manual_review_only",
        "operation": "update_setup_technical_thresholds",
        "setup_id": str(setup_id).upper(),
        "technical_thresholds": candidate_thresholds,
        "note": "Generated from calibration evidence. Review manually before editing config/advisory_setups.yaml.",
    }


def build_manual_patch_text(patch: dict[str, Any]) -> str:
    setup_id = str(patch.get("setup_id") or "").strip()
    thresholds = patch.get("technical_thresholds") if isinstance(patch.get("technical_thresholds"), dict) else {}
    lines = [
        "# Manual patch guidance only. Review config/advisory_setups.yaml before editing.",
        f"# setup_id: {setup_id}",
        "technical_thresholds:",
    ]
    for key, value in thresholds.items():
        lines.append(f"  {key}: {json.dumps(value, ensure_ascii=False, default=str)}")
    return "\n".join(lines)


def build_review_prompt(payload: dict[str, Any]) -> str:
    return (
        "Review this proposed swing-technical threshold promotion. "
        "You are not allowed to apply the change. Decide whether a human should promote, partially promote, reject, or wait for more data. "
        "Focus on sample size, hit rate after costs, average return after costs, spread versus rejected rows, and whether the threshold changes are too loose or overfit.\n\n"
        + json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    )


def deterministic_review(payload: dict[str, Any], *, status: str = "disabled", error: str | None = None) -> TechnicalThresholdPromotionReview:
    evidence = payload.get("calibration_evidence") or {}
    eligible_raw = pd.to_numeric(evidence.get("eligible_count"), errors="coerce")
    eligible = 0 if pd.isna(eligible_raw) else int(eligible_raw)
    hit_rate = pd.to_numeric(evidence.get("hit_rate_after_cost"), errors="coerce")
    avg_return = pd.to_numeric(evidence.get("avg_forward_return_after_cost"), errors="coerce")
    recommendation: Literal["promote", "promote_partially", "reject", "needs_more_data"] = "needs_more_data"
    reasons = []
    risks = []
    if eligible < 30:
        reasons.append("Eligible sample size is below 30 rows.")
        risks.append("Small sample can be dominated by one market regime or a few stocks.")
    elif pd.notna(hit_rate) and pd.notna(avg_return) and float(hit_rate) >= 0.45 and float(avg_return) > 0:
        recommendation = "promote_partially"
        reasons.append("Calibration has adequate sample size and positive average return after costs.")
    elif pd.notna(avg_return) and float(avg_return) <= 0:
        recommendation = "reject"
        reasons.append("Average return after costs is not positive.")
    else:
        reasons.append("Evidence is mixed and should be reviewed manually.")
    if error:
        risks.append(f"LLM review unavailable: {error}")
    return TechnicalThresholdPromotionReview(
        recommendation=recommendation,
        confidence=0.45 if recommendation == "needs_more_data" else 0.55,
        summary=f"Deterministic review status={status}. Human review required before any threshold change.",
        reasons=reasons,
        overfit_risks=risks,
        threshold_changes=[],
        suggested_manual_checks=[
            "Compare the candidate thresholds against current setup behavior.",
            "Check whether evidence spans multiple market regimes.",
            "Run calibration on a longer period and multiple horizons before promotion.",
        ],
        proposed_patch=payload.get("pending_patch") or {},
    )


def generate_promotion_review(
    *,
    setup_id: str,
    config_id: str,
    use_llm: bool = True,
    model: str | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    setup = load_setup(setup_id)
    calibration = load_calibration_config(config_id)
    candidate_thresholds = parse_jsonish(calibration.get("threshold_config_json"), {})
    if not isinstance(candidate_thresholds, dict):
        candidate_thresholds = {}
    evidence = {
        key: calibration.get(key)
        for key in [
            "config_id",
            "horizon_days",
            "evaluated_at",
            "signal_count",
            "eligible_count",
            "trade_rate",
            "avg_forward_return_after_cost",
            "hit_rate_after_cost",
            "avg_rejected_forward_return",
            "spread_vs_rejected",
            "objective_score",
            "sample_start",
            "sample_end",
        ]
    }
    summary = load_horizon_summary(int(calibration.get("horizon_days") or 0), calibration.get("evaluated_at"))
    pending_patch = build_pending_patch(str(setup["setup_id"]), candidate_thresholds)
    payload = {
        "setup_id": setup["setup_id"],
        "setup_name": setup.get("setup_name"),
        "current_thresholds": current_thresholds_for_setup(setup),
        "candidate_thresholds": candidate_thresholds,
        "calibration_evidence": evidence,
        "horizon_summary": summary,
        "pending_patch": pending_patch,
    }
    effective_model = model or DEFAULT_PROMOTION_REVIEW_MODEL
    review_status = "disabled"
    review_error = None
    try:
        if use_llm and str(effective_model).strip().lower() not in {"", "off", "none", "disabled", "false"}:
            codex_model = effective_model.split(":", 1)[1] if effective_model.startswith("codex:") else None if effective_model == "codex" else effective_model
            review = run_codex_structured(
                build_review_prompt(payload),
                response_model=TechnicalThresholdPromotionReview,
                model=codex_model,
                system_prompt=(
                    "You are a cautious quant/research reviewer for Indian-equity swing technical thresholds. "
                    "You can advise and generate a pending patch, but you must not claim the change is production-safe without manual approval."
                ),
                max_attempts=2,
            )
            proposed_patch = dict(review.proposed_patch or {})
            proposed_patch.update(
                {
                    "path": pending_patch["path"],
                    "mode": "manual_review_only",
                    "operation": pending_patch["operation"],
                    "setup_id": pending_patch["setup_id"],
                    "technical_thresholds": pending_patch["technical_thresholds"],
                    "note": pending_patch["note"],
                }
            )
            review = review.model_copy(update={"proposed_patch": proposed_patch})
            review_status = "ok"
        else:
            review = deterministic_review(payload, status="disabled")
    except Exception as exc:
        review_error = f"{type(exc).__name__}: {exc}"
        record_fallback_event(
            module="advisory.technical_threshold_promotion",
            source="technical_threshold_promotion_review",
            fallback_type="llm_deterministic_fallback",
            severity="warn",
            reason="Technical-threshold promotion LLM review failed; deterministic review was used.",
            deterministic_fallback=True,
            error=exc,
            metadata={"model": effective_model, "setup_id": setup.get("setup_id"), "config_id": calibration.get("config_id")},
        )
        review = deterministic_review(payload, status="fallback_after_error", error=review_error)
        review_status = "fallback_after_error"
    reviewed_at = pd.Timestamp.utcnow()
    result = {
        "status": "ok",
        "reviewed_at": reviewed_at,
        "setup_id": setup["setup_id"],
        "config_id": calibration.get("config_id"),
        "horizon_days": calibration.get("horizon_days"),
        "evaluated_at": calibration.get("evaluated_at"),
        "current_thresholds": payload["current_thresholds"],
        "candidate_thresholds": candidate_thresholds,
        "calibration_evidence": evidence,
        "horizon_summary": summary,
        "pending_patch": pending_patch,
        "llm_review": review.model_dump(),
        "prompt_id": PROMPT_ID,
        "prompt_version": PROMPT_VERSION,
        "prompt_schema_version": PROMPT_SCHEMA_VERSION,
        "review_model": effective_model,
        "review_status": review_status,
        "review_error": review_error,
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
                "setup_id": result["setup_id"],
                "config_id": result["config_id"],
                "horizon_days": result["horizon_days"],
                "evaluated_at": result["evaluated_at"],
                "current_thresholds_json": json_dumps(result["current_thresholds"]),
                "candidate_thresholds_json": json_dumps(result["candidate_thresholds"]),
                "calibration_evidence_json": json_dumps(result["calibration_evidence"]),
                "llm_review_json": json_dumps(result["llm_review"]),
                "recommendation": result["llm_review"].get("recommendation"),
                "confidence": result["llm_review"].get("confidence"),
                "patch_json": json_dumps(result["pending_patch"]),
                "prompt_id": result.get("prompt_id") or PROMPT_ID,
                "prompt_version": result.get("prompt_version") or PROMPT_VERSION,
                "prompt_schema_version": result.get("prompt_schema_version") or PROMPT_SCHEMA_VERSION,
                "review_model": result["review_model"],
                "review_status": result["review_status"],
                "review_error": result["review_error"],
                "load_ts": pd.Timestamp.utcnow(),
            }
        ]
    )
    upsert_to_db(row, REVIEWS_TABLE, unique_keys=["setup_id", "config_id", "reviewed_at"], timescaledb_column="reviewed_at")


def load_promotion_reviews(limit: int = 25) -> list[dict[str, Any]]:
    ensure_tables()
    df = sql_to_df(
        f"""
        WITH latest_decision AS (
            SELECT DISTINCT ON (reviewed_at, setup_id, config_id)
                reviewed_at,
                setup_id,
                config_id,
                decision,
                operator_id,
                decision_reason,
                decided_at
            FROM {DECISIONS_TABLE}
            ORDER BY reviewed_at, setup_id, config_id, decided_at DESC
        )
        SELECT
            r.*,
            d.decision AS manual_decision,
            d.operator_id AS manual_operator_id,
            d.decision_reason AS manual_decision_reason,
            d.decided_at AS manual_decided_at
        FROM {REVIEWS_TABLE} r
        LEFT JOIN latest_decision d
          ON d.reviewed_at = r.reviewed_at
         AND d.setup_id = r.setup_id
         AND d.config_id = r.config_id
        ORDER BY r.reviewed_at DESC
        LIMIT %(limit)s
        """,
        params={"limit": max(1, int(limit))},
        retries=3,
    )
    if df.empty:
        return []
    out = df.copy()
    for col in ["current_thresholds_json", "candidate_thresholds_json", "calibration_evidence_json", "llm_review_json", "patch_json"]:
        if col in out.columns:
            out[col.replace("_json", "")] = out[col].map(lambda value: parse_jsonish(value, {}))
    out["manual_patch_text"] = out.get("patch", pd.Series([{} for _ in range(len(out))])).map(build_manual_patch_text)
    out = out.astype(object).where(pd.notna(out), None)
    return out.to_dict(orient="records")


def find_review(*, reviewed_at: Any, setup_id: str, config_id: str) -> dict[str, Any]:
    ensure_tables()
    parsed_reviewed_at = pd.to_datetime(reviewed_at, utc=True, errors="coerce")
    if pd.isna(parsed_reviewed_at):
        raise ValueError(f"Invalid reviewed_at: {reviewed_at}")
    df = sql_to_df(
        f"""
        SELECT *
        FROM {REVIEWS_TABLE}
        WHERE reviewed_at = %(reviewed_at)s
          AND setup_id = %(setup_id)s
          AND config_id = %(config_id)s
        LIMIT 1
        """,
        params={"reviewed_at": parsed_reviewed_at, "setup_id": str(setup_id), "config_id": str(config_id)},
        retries=3,
    )
    if df.empty:
        raise ValueError(f"Unknown threshold promotion review: {setup_id}/{config_id}/{reviewed_at}")
    row = df.iloc[0].to_dict()
    row["current_thresholds"] = parse_jsonish(row.get("current_thresholds_json"), {})
    row["candidate_thresholds"] = parse_jsonish(row.get("candidate_thresholds_json"), {})
    row["calibration_evidence"] = parse_jsonish(row.get("calibration_evidence_json"), {})
    row["llm_review"] = parse_jsonish(row.get("llm_review_json"), {})
    row["patch"] = parse_jsonish(row.get("patch_json"), {})
    return row


def record_manual_decision(
    *,
    reviewed_at: Any,
    setup_id: str,
    config_id: str,
    decision: ManualDecision,
    operator_id: str | None = None,
    decision_reason: str | None = None,
) -> dict[str, Any]:
    normalized_decision = str(decision or "").strip().lower()
    if normalized_decision not in {"approved", "rejected", "needs_more_data"}:
        raise ValueError("decision must be approved, rejected, or needs_more_data")
    review = find_review(reviewed_at=reviewed_at, setup_id=setup_id, config_id=config_id)
    decided_at = pd.Timestamp.utcnow()
    final_patch = dict(review.get("patch") or {})
    final_patch["mode"] = "manual_apply_required"
    final_patch["manual_decision"] = normalized_decision
    final_patch["manual_patch_text"] = build_manual_patch_text(final_patch)
    result = {
        "status": "ok",
        "decided_at": decided_at,
        "reviewed_at": review["reviewed_at"],
        "setup_id": review["setup_id"],
        "config_id": review["config_id"],
        "decision": normalized_decision,
        "operator_id": operator_id,
        "decision_reason": decision_reason,
        "final_patch": final_patch,
        "review": {
            "recommendation": review.get("recommendation"),
            "confidence": review.get("confidence"),
            "llm_review": review.get("llm_review") or {},
            "calibration_evidence": review.get("calibration_evidence") or {},
        },
        "applied": False,
        "note": "Decision recorded only. No config file or trading behavior was changed.",
    }
    row = pd.DataFrame(
        [
            {
                "decided_at": decided_at,
                "reviewed_at": review["reviewed_at"],
                "setup_id": review["setup_id"],
                "config_id": review["config_id"],
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
    upsert_to_db(row, DECISIONS_TABLE, unique_keys=["reviewed_at", "setup_id", "config_id", "decided_at"], timescaledb_column="decided_at")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate an LLM-assisted manual review for calibrated technical threshold promotion.")
    parser.add_argument("--setup-id", required=True)
    parser.add_argument("--config-id", required=True)
    parser.add_argument("--model", default=None)
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = generate_promotion_review(
        setup_id=args.setup_id,
        config_id=args.config_id,
        model=args.model,
        use_llm=not bool(args.no_llm),
        persist=not bool(args.dry_run),
    )
    result["dry_run"] = bool(args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
