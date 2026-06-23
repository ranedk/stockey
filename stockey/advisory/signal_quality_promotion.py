from __future__ import annotations

import argparse
import json
import os
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, Field

from advisory.context_overlay_reliability_report import RELIABILITY_SUMMARY_TABLE as CONTEXT_OVERLAY_RELIABILITY_SUMMARY_TABLE
from advisory.context_overlay_reliability_report import reliability_runtime_policy_contract
from advisory.context_overlay_reliability_report import load_persisted_reliability_report as load_persisted_context_reliability_report
from advisory.fallback_telemetry import record_local_fallback_event
from advisory.multiple_testing import benjamini_hochberg, benjamini_hochberg_qvalues, binomial_right_tail_p_value
from advisory.signal_quality_family_report import _is_missing_table_error as is_missing_family_report_table_error
from advisory.signal_quality_family_report import build_family_report, build_unavailable_report, load_family_summary
from advisory.signal_quality_family_report import family_report_authority_contract
from advisory.signal_quality_evaluator import EVALUATIONS_TABLE, SUMMARY_TABLE
from advisory.signal_quality_split_evaluator import (
    SPLIT_SUMMARY_TABLE,
    build_split_stability_report,
    load_split_summary_history,
)
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration


REVIEWS_TABLE = "advisory_signal_quality_promotion_reviews"
DECISIONS_TABLE = "advisory_signal_quality_promotion_decisions"
# False-discovery-rate ceiling for the family-candidate batch. Across many candidate
# (source family x horizon) comparisons in one run, Benjamini-Hochberg keeps the expected
# share of false promotions at or below this level. Research-only; no auto-apply.
SIGNAL_QUALITY_PROMOTION_FDR_ALPHA = float(os.getenv("SIGNAL_QUALITY_PROMOTION_FDR_ALPHA", "0.10"))
SIGNAL_QUALITY_PROMOTION_SCHEMA_MIGRATION_ID = "20260611_advisory_signal_quality_promotion_base"

SIGNAL_QUALITY_PROMOTION_SCHEMA_STATEMENTS = [
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
    """,
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
    """,
    f"ALTER TABLE {DECISIONS_TABLE} ADD COLUMN IF NOT EXISTS final_patch_json TEXT",
]

ManualDecision = Literal["approved", "rejected", "needs_more_data"]
CONTEXT_SIGNAL_QUALITY_VARIANTS = {
    "technical_plus_context_overlay",
    "technical_plus_all_context",
    "technical_plus_announcement_context",
    "technical_plus_exchange_context",
    "technical_plus_bhavcopy_context",
    "technical_plus_theme_context",
    "technical_plus_macro_context",
}
CONTEXT_SOURCE_FAMILY_VARIANTS = {
    "technical_plus_announcement_context": "announcement_context",
    "technical_plus_exchange_context": "exchange_context",
    "technical_plus_bhavcopy_context": "bhavcopy_context",
    "technical_plus_theme_context": "theme_context",
    "technical_plus_macro_context": "macro_context",
}
TRUSTED_CONTEXT_RULE_VARIANT = "technical_after_trusted_context_rules"
DEFAULT_MIN_CONTEXT_SELECTED_ROWS = 10
DEFAULT_MIN_CONTEXT_MATURED_ROWS = 10
DEFAULT_MIN_CONTEXT_SYMBOLS = 5
DEFAULT_MIN_CONTEXT_SOURCE_FAMILIES = 2
DEFAULT_SPLIT_NEGATIVE_CONTROL_LIMIT = 500
FAST_RELIABILITY_CONTEXT_CLASS_BLOCKERS = {
    "hurts_or_no_lift",
    "negative_after_cost",
    "inconsistent_or_horizon_sensitive",
    "needs_benchmark_attribution",
    "benchmark_beta_not_overlay_alpha",
}
FAST_RELIABILITY_SECTOR_BLOCKERS = FAST_RELIABILITY_CONTEXT_CLASS_BLOCKERS


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


def parse_jsonish(value: Any, default: Any, *, source: str = "signal_quality_promotion_json") -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        if pd.isna(value):
            return default
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.signal_quality_promotion",
            fallback_type="signal_quality_promotion_json_missing_check_failed",
            source=source,
            severity="warn",
            reason="Signal-quality promotion could not evaluate missingness for stored JSON and continued parsing.",
            error=exc,
            metadata={"value_type": type(value).__name__, "default_type": type(default).__name__},
        )
    try:
        return json.loads(str(value))
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.signal_quality_promotion",
            fallback_type="signal_quality_promotion_json_parse_failed",
            source=source,
            severity="warn",
            reason="Signal-quality promotion could not parse stored JSON; using the existing default fallback.",
            error=exc,
            metadata={"source": source, "payload_length": len(str(value))},
        )
        return default


def ensure_tables() -> None:
    apply_schema_migration(
        migration_id=SIGNAL_QUALITY_PROMOTION_SCHEMA_MIGRATION_ID,
        description="Create advisory signal-quality promotion review and decision tables.",
        statements=SIGNAL_QUALITY_PROMOTION_SCHEMA_STATEMENTS,
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
        module="advisory.signal_quality_promotion",
        fallback_type=fallback_type,
        source=source,
        severity="warn",
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def load_signal_quality_summary(*, evaluated_at: Any, horizon_days: int, variant: str) -> dict[str, Any]:
    parsed_evaluated_at = pd.to_datetime(evaluated_at, utc=True, errors="coerce")
    if pd.isna(parsed_evaluated_at):
        raise ValueError(f"Invalid evaluated_at: {evaluated_at}")
    normalized_variant = str(variant or "").strip()
    if not normalized_variant or normalized_variant == "technical_only":
        raise ValueError("variant must be a non-technical_only overlay variant")
    try:
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
    except Exception as exc:
        _record_promotion_source_failure(
            fallback_type="signal_quality_promotion_summary_load_failed",
            source=SUMMARY_TABLE,
            reason="Signal-quality promotion could not load overlay summary evidence for review.",
            error=exc,
            metadata={
                "evaluated_at": str(parsed_evaluated_at),
                "horizon_days": int(horizon_days),
                "variant": normalized_variant,
            },
        )
        raise
    if df.empty:
        raise ValueError(f"Unknown signal-quality row: {evaluated_at}/{horizon_days}/{variant}")
    return df.iloc[0].to_dict()


def is_context_variant(variant: Any) -> bool:
    return str(variant or "").strip() in CONTEXT_SIGNAL_QUALITY_VARIANTS


def is_trusted_context_rule_variant(variant: Any) -> bool:
    return str(variant or "").strip() == TRUSTED_CONTEXT_RULE_VARIANT


def context_variant_source_family(variant: Any) -> str | None:
    return CONTEXT_SOURCE_FAMILY_VARIANTS.get(str(variant or "").strip())


def _safe_int(value: Any) -> int:
    numeric = pd.to_numeric(value, errors="coerce")
    return 0 if pd.isna(numeric) else int(numeric)


def _runtime_contract_allowed(contract: Any, use_name: str) -> bool:
    if not isinstance(contract, dict):
        return False
    allowed = contract.get("allowed_runtime_uses")
    if not isinstance(allowed, dict):
        return False
    return bool(allowed.get(use_name))


def _context_source_family_counts(rows: pd.DataFrame) -> dict[str, int]:
    counts: dict[str, int] = {}
    if rows.empty or "context_sources_json" not in rows.columns:
        return counts
    for value in rows["context_sources_json"].tolist():
        parsed = parse_jsonish(value, [], source="context_sources_json")
        if isinstance(parsed, str):
            parsed = parse_jsonish(parsed, [], source="context_sources_json_nested")
        if isinstance(parsed, dict):
            parsed = [parsed]
        if not isinstance(parsed, list):
            continue
        seen_for_row: set[str] = set()
        for item in parsed:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source") or item.get("context_source") or "").strip().lower()
            if not source:
                continue
            seen_for_row.add(source)
        for source in seen_for_row:
            counts[source] = counts.get(source, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _context_source_family_coverage(rows: pd.DataFrame, family: str | None) -> dict[str, Any]:
    if not family or rows.empty or "context_sources_json" not in rows.columns:
        return {
            "context_variant_source_family": family,
            "context_family_selected_rows": 0,
            "context_family_matured_rows": 0,
            "context_family_symbols": 0,
        }
    normalized_family = str(family).strip().lower()
    selected_rows = 0
    matured_rows = 0
    symbols: set[str] = set()
    for _, row in rows.iterrows():
        parsed = parse_jsonish(row.get("context_sources_json"), [], source="context_sources_json")
        if isinstance(parsed, str):
            parsed = parse_jsonish(parsed, [], source="context_sources_json_nested")
        if isinstance(parsed, dict):
            parsed = [parsed]
        if not isinstance(parsed, list):
            continue
        sources = {
            str(item.get("source") or item.get("context_source") or "").strip().lower()
            for item in parsed
            if isinstance(item, dict)
        }
        if normalized_family not in sources:
            continue
        selected_rows += 1
        if str(row.get("matured")).strip().lower() in {"true", "1", "yes", "y", "t"}:
            matured_rows += 1
        symbol = str(row.get("symbol") or "").strip().upper()
        if symbol:
            symbols.add(symbol)
    return {
        "context_variant_source_family": normalized_family,
        "context_family_selected_rows": selected_rows,
        "context_family_matured_rows": matured_rows,
        "context_family_symbols": len(symbols),
    }


def load_context_variant_coverage(*, evaluated_at: Any, horizon_days: int, variant: str) -> dict[str, Any]:
    parsed_evaluated_at = pd.to_datetime(evaluated_at, utc=True, errors="coerce")
    if pd.isna(parsed_evaluated_at):
        raise ValueError(f"Invalid evaluated_at: {evaluated_at}")
    normalized_variant = str(variant or "").strip()
    if not is_context_variant(normalized_variant):
        return {"context_variant": False}
    try:
        df = sql_to_df(
            f"""
            SELECT
                COUNT(*) FILTER (WHERE selected IS TRUE) AS selected_rows,
                COUNT(*) FILTER (WHERE selected IS TRUE AND matured IS TRUE) AS matured_selected_rows,
                COUNT(*) FILTER (
                    WHERE selected IS TRUE
                      AND COALESCE(context_overlay_count, 0) > 0
                ) AS context_selected_rows,
                COUNT(*) FILTER (
                    WHERE selected IS TRUE
                      AND matured IS TRUE
                      AND COALESCE(context_overlay_count, 0) > 0
                ) AS context_matured_rows,
                COUNT(DISTINCT symbol) FILTER (
                    WHERE selected IS TRUE
                      AND COALESCE(context_overlay_count, 0) > 0
                ) AS context_symbols,
                AVG(context_overlay_count) FILTER (WHERE selected IS TRUE) AS avg_selected_context_overlay_count,
                MAX(context_overlay_count) FILTER (WHERE selected IS TRUE) AS max_selected_context_overlay_count
            FROM {EVALUATIONS_TABLE}
            WHERE evaluated_at = %(evaluated_at)s
              AND horizon_days = %(horizon_days)s
              AND variant = %(variant)s
            """,
            params={"evaluated_at": parsed_evaluated_at, "horizon_days": int(horizon_days), "variant": normalized_variant},
            retries=3,
        )
    except Exception as exc:
        _record_promotion_source_failure(
            fallback_type="signal_quality_promotion_context_coverage_load_failed",
            source=EVALUATIONS_TABLE,
            reason="Signal-quality promotion could not load context-overlay coverage for the candidate variant.",
            error=exc,
            metadata={
                "evaluated_at": str(parsed_evaluated_at),
                "horizon_days": int(horizon_days),
                "variant": normalized_variant,
            },
        )
        raise
    if df.empty:
        return {
            "context_variant": True,
            "selected_rows": 0,
            "matured_selected_rows": 0,
            "context_selected_rows": 0,
            "context_matured_rows": 0,
            "context_symbols": 0,
            "context_source_families": 0,
            "context_source_family_counts": {},
            "context_variant_source_family": context_variant_source_family(normalized_variant),
            "context_family_selected_rows": 0,
            "context_family_matured_rows": 0,
            "context_family_symbols": 0,
            "avg_selected_context_overlay_count": None,
            "max_selected_context_overlay_count": None,
        }
    row = df.iloc[0].to_dict()
    try:
        source_rows = sql_to_df(
            f"""
            SELECT symbol, matured, context_sources_json
            FROM {EVALUATIONS_TABLE}
            WHERE evaluated_at = %(evaluated_at)s
              AND horizon_days = %(horizon_days)s
              AND variant = %(variant)s
              AND selected IS TRUE
              AND COALESCE(context_overlay_count, 0) > 0
            LIMIT 50000
            """,
            params={"evaluated_at": parsed_evaluated_at, "horizon_days": int(horizon_days), "variant": normalized_variant},
            retries=3,
        )
    except Exception as exc:
        _record_promotion_source_failure(
            fallback_type="signal_quality_promotion_context_source_family_load_failed",
            source=EVALUATIONS_TABLE,
            reason="Signal-quality promotion could not load context-overlay source families for the candidate variant.",
            error=exc,
            metadata={
                "evaluated_at": str(parsed_evaluated_at),
                "horizon_days": int(horizon_days),
                "variant": normalized_variant,
            },
        )
        raise
    source_counts = _context_source_family_counts(source_rows)
    family_coverage = _context_source_family_coverage(source_rows, context_variant_source_family(normalized_variant))
    return {
        "context_variant": True,
        "selected_rows": _safe_int(row.get("selected_rows")),
        "matured_selected_rows": _safe_int(row.get("matured_selected_rows")),
        "context_selected_rows": _safe_int(row.get("context_selected_rows")),
        "context_matured_rows": _safe_int(row.get("context_matured_rows")),
        "context_symbols": _safe_int(row.get("context_symbols")),
        "context_source_families": len(source_counts),
        "context_source_family_counts": source_counts,
        **family_coverage,
        "avg_selected_context_overlay_count": row.get("avg_selected_context_overlay_count"),
        "max_selected_context_overlay_count": row.get("max_selected_context_overlay_count"),
    }


def load_source_family_report_gate(*, evaluated_at: Any, horizon_days: int, variant: str) -> dict[str, Any]:
    source_family = context_variant_source_family(variant)
    if not source_family:
        return {"family_report_required": False}
    parsed_evaluated_at = pd.to_datetime(evaluated_at, utc=True, errors="coerce")
    if pd.isna(parsed_evaluated_at):
        raise ValueError(f"Invalid evaluated_at: {evaluated_at}")
    try:
        rows = load_family_summary(evaluated_at=parsed_evaluated_at, horizons=[int(horizon_days)])
        report = build_family_report(rows)
    except Exception as exc:
        _record_promotion_source_failure(
            fallback_type="signal_quality_promotion_family_report_load_failed",
            source=SUMMARY_TABLE,
            reason="Signal-quality promotion could not load source-family report evidence for the candidate variant.",
            error=exc,
            metadata={
                "evaluated_at": str(parsed_evaluated_at),
                "horizon_days": int(horizon_days),
                "variant": str(variant),
                "source_family": source_family,
            },
        )
        raise
    family_rows = [row for row in report.get("families", []) if row.get("source_family") == source_family]
    family_row = family_rows[0] if family_rows else None
    classification = None if family_row is None else family_row.get("classification")
    authority_contract = (
        family_row.get("authority_contract")
        if isinstance(family_row, dict) and isinstance(family_row.get("authority_contract"), dict)
        else family_report_authority_contract(source_family)
    )
    return {
        "family_report_required": True,
        "family_report_status": report.get("status"),
        "family_report_source_family": source_family,
        "family_report_authority_contract": authority_contract,
        "family_report_classification": classification,
        "family_report_candidate_helpful": classification == "candidate_helpful",
        "family_report_total_selected_count": None if family_row is None else family_row.get("total_selected_count"),
        "family_report_total_matured_count": None if family_row is None else family_row.get("total_matured_count"),
        "family_report_avg_lift_vs_technical_only": None if family_row is None else family_row.get("avg_lift_vs_technical_only"),
        "family_report_horizon_count": None if family_row is None else family_row.get("horizon_count"),
    }


def load_split_negative_control_gate(*, source_family: str | None, horizon_days: int | None) -> dict[str, Any]:
    normalized_family = str(source_family or "").strip().lower()
    if not normalized_family or not horizon_days:
        return {
            "split_negative_control_gate_required": False,
            "split_negative_control_blocks_broad_promotion": False,
            "split_negative_control_block_reason": None,
            "split_negative_control_status": "not_required",
            "split_negative_control_splits": [],
            "split_negative_control_baseline_unavailable_splits": [],
        }
    try:
        history = load_split_summary_history(
            source_family=normalized_family,
            horizons=[int(horizon_days)],
            limit=DEFAULT_SPLIT_NEGATIVE_CONTROL_LIMIT,
        )
        report = build_split_stability_report(history)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.signal_quality_promotion",
            fallback_type="signal_quality_promotion_split_negative_control_load_failed",
            source=SPLIT_SUMMARY_TABLE,
            severity="warn",
            reason="Signal-quality promotion could not load narrowed split negative-control evidence; broad family promotion is held for more data.",
            error=exc,
            metadata={"source_family": normalized_family, "horizon_days": int(horizon_days)},
        )
        return {
            "split_negative_control_gate_required": True,
            "split_negative_control_blocks_broad_promotion": True,
            "split_negative_control_block_reason": "unavailable_due_to_error",
            "split_negative_control_status": "unavailable_due_to_error",
            "split_negative_control_splits": [],
            "split_negative_control_baseline_unavailable_splits": [],
            "split_negative_control_error": exc.__class__.__name__,
        }
    harmful = [
        split
        for split in report.get("splits", [])
        if split.get("classification") == "harmful_negative_control"
    ]

    def _split_count(value: Any) -> int:
        numeric = pd.to_numeric(value, errors="coerce")
        if pd.isna(numeric):
            return 0
        return int(numeric)

    baseline_unavailable = [
        split
        for split in report.get("splits", [])
        if _split_count(split.get("baseline_unavailable_window_count")) > 0
        or split.get("classification") == "technical_baseline_unavailable"
    ]
    block_reason = None
    if harmful:
        block_reason = "harmful_narrowed_split_negative_control"
    elif baseline_unavailable:
        block_reason = "technical_baseline_unavailable"
    return {
        "split_negative_control_gate_required": True,
        "split_negative_control_blocks_broad_promotion": bool(harmful or baseline_unavailable),
        "split_negative_control_block_reason": block_reason,
        "split_negative_control_status": report.get("status"),
        "split_negative_control_harmful_count": len(harmful),
        "split_negative_control_baseline_unavailable_count": len(baseline_unavailable),
        "split_negative_control_split_count": report.get("split_count", 0),
        "split_negative_control_splits": harmful[:10],
        "split_negative_control_baseline_unavailable_splits": baseline_unavailable[:10],
        "split_negative_control_headline": report.get("headline"),
    }


def load_persisted_fast_reliability_gate(*, evaluated_at: Any | None, source_family: str | None) -> dict[str, Any]:
    normalized_family = str(source_family or "").strip().lower()
    if not normalized_family:
        return {
            "fast_reliability_gate_required": False,
            "fast_reliability_blocks_broad_promotion": False,
            "fast_reliability_status": "not_required",
        }
    try:
        report = load_persisted_context_reliability_report(evaluated_at=evaluated_at)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.signal_quality_promotion",
            fallback_type="signal_quality_promotion_fast_reliability_load_failed",
            source=CONTEXT_OVERLAY_RELIABILITY_SUMMARY_TABLE,
            severity="warn",
            reason="Signal-quality promotion could not load persisted fast context-overlay reliability; broad family promotion is held for more data.",
            error=exc,
            metadata={"evaluated_at": str(evaluated_at) if evaluated_at is not None else None, "source_family": normalized_family},
        )
        return {
            "fast_reliability_gate_required": True,
            "fast_reliability_blocks_broad_promotion": True,
            "fast_reliability_status": "unavailable_due_to_error",
            "fast_reliability_source_family": normalized_family,
            "fast_reliability_error": exc.__class__.__name__,
        }
    if report.get("status") != "ok":
        return {
            "fast_reliability_gate_required": False,
            "fast_reliability_blocks_broad_promotion": False,
            "fast_reliability_status": report.get("status") or "no_data",
            "fast_reliability_source_family": normalized_family,
            "fast_reliability_evaluated_at": report.get("evaluated_at"),
        }
    family = next(
        (
            row
            for row in report.get("families") or []
            if str(row.get("source_family") or "").strip().lower() == normalized_family
        ),
        None,
    )
    if not family:
        return {
            "fast_reliability_gate_required": False,
            "fast_reliability_blocks_broad_promotion": False,
            "fast_reliability_status": "family_not_present",
            "fast_reliability_source_family": normalized_family,
            "fast_reliability_evaluated_at": report.get("evaluated_at"),
        }
    classification = str(family.get("classification") or "").strip()
    runtime_contract = family.get("runtime_policy_contract")
    if not isinstance(runtime_contract, dict):
        runtime_contract = reliability_runtime_policy_contract(classification)
    watch_priority_allowed = _runtime_contract_allowed(runtime_contract, "watch_priority")
    harmful_context_classes = [
        item
        for item in family.get("context_class_diagnostics") or []
        if str(item.get("classification") or "").strip() in FAST_RELIABILITY_CONTEXT_CLASS_BLOCKERS
    ]
    harmful_context_class_count = len(harmful_context_classes)
    harmful_sector_diagnostics = [
        item
        for item in family.get("sector_diagnostics") or []
        if str(item.get("classification") or "").strip() in FAST_RELIABILITY_SECTOR_BLOCKERS
    ]
    harmful_sector_count = len(harmful_sector_diagnostics)
    blocks_broad_promotion = not watch_priority_allowed or harmful_context_class_count > 0 or harmful_sector_count > 0
    return {
        "fast_reliability_gate_required": True,
        "fast_reliability_blocks_broad_promotion": blocks_broad_promotion,
        "fast_reliability_status": "ok",
        "fast_reliability_source_family": normalized_family,
        "fast_reliability_classification": classification,
        "fast_reliability_runtime_policy_contract": runtime_contract,
        "fast_reliability_watch_priority_allowed": watch_priority_allowed,
        "fast_reliability_evaluated_at": report.get("evaluated_at"),
        "fast_reliability_harmful_context_class_count": harmful_context_class_count,
        "fast_reliability_harmful_context_classes": harmful_context_classes[:10],
        "fast_reliability_harmful_sector_count": harmful_sector_count,
        "fast_reliability_harmful_sectors": harmful_sector_diagnostics[:10],
        "fast_reliability_family": family,
    }


def load_trusted_context_rule_coverage(*, evaluated_at: Any, horizon_days: int, variant: str) -> dict[str, Any]:
    parsed_evaluated_at = pd.to_datetime(evaluated_at, utc=True, errors="coerce")
    if pd.isna(parsed_evaluated_at):
        raise ValueError(f"Invalid evaluated_at: {evaluated_at}")
    normalized_variant = str(variant or "").strip()
    if not is_trusted_context_rule_variant(normalized_variant):
        return {"trusted_context_rule_variant": False}
    try:
        df = sql_to_df(
            f"""
            SELECT
                COUNT(*) FILTER (WHERE selected IS TRUE) AS selected_rows,
                COUNT(*) FILTER (WHERE selected IS TRUE AND matured IS TRUE) AS matured_selected_rows,
                COUNT(*) FILTER (
                    WHERE selected IS TRUE
                      AND (
                        trusted_context_adjustment IS NOT NULL
                        OR trusted_context_negative_block IS TRUE
                        OR trusted_context_positive_boost IS TRUE
                      )
                ) AS trusted_adjusted_selected_rows,
                COUNT(*) FILTER (
                    WHERE selected IS TRUE
                      AND matured IS TRUE
                      AND (
                        trusted_context_adjustment IS NOT NULL
                        OR trusted_context_negative_block IS TRUE
                        OR trusted_context_positive_boost IS TRUE
                      )
                ) AS trusted_adjusted_matured_rows,
                COUNT(DISTINCT symbol) FILTER (
                    WHERE selected IS TRUE
                      AND (
                        trusted_context_adjustment IS NOT NULL
                        OR trusted_context_negative_block IS TRUE
                        OR trusted_context_positive_boost IS TRUE
                      )
                ) AS trusted_adjusted_symbols,
                COUNT(*) FILTER (WHERE trusted_context_negative_block IS TRUE) AS trusted_negative_block_rows,
                COUNT(*) FILTER (WHERE trusted_context_positive_boost IS TRUE) AS trusted_positive_boost_rows
            FROM {EVALUATIONS_TABLE}
            WHERE evaluated_at = %(evaluated_at)s
              AND horizon_days = %(horizon_days)s
              AND variant = %(variant)s
            """,
            params={"evaluated_at": parsed_evaluated_at, "horizon_days": int(horizon_days), "variant": normalized_variant},
            retries=3,
        )
    except Exception as exc:
        _record_promotion_source_failure(
            fallback_type="signal_quality_promotion_trusted_context_rule_coverage_load_failed",
            source=EVALUATIONS_TABLE,
            reason="Signal-quality promotion could not load trusted context-rule adjustment coverage for the candidate variant.",
            error=exc,
            metadata={
                "evaluated_at": str(parsed_evaluated_at),
                "horizon_days": int(horizon_days),
                "variant": normalized_variant,
            },
        )
        raise
    row = {} if df.empty else df.iloc[0].to_dict()
    return {
        "trusted_context_rule_variant": True,
        "selected_rows": _safe_int(row.get("selected_rows")),
        "matured_selected_rows": _safe_int(row.get("matured_selected_rows")),
        "trusted_adjusted_selected_rows": _safe_int(row.get("trusted_adjusted_selected_rows")),
        "trusted_adjusted_matured_rows": _safe_int(row.get("trusted_adjusted_matured_rows")),
        "trusted_adjusted_symbols": _safe_int(row.get("trusted_adjusted_symbols")),
        "trusted_negative_block_rows": _safe_int(row.get("trusted_negative_block_rows")),
        "trusted_positive_boost_rows": _safe_int(row.get("trusted_positive_boost_rows")),
    }


def build_pending_patch(evidence: dict[str, Any]) -> dict[str, Any]:
    variant = str(evidence.get("variant") or "").strip()
    horizon_days = int(evidence.get("horizon_days") or 0)
    if is_trusted_context_rule_variant(variant):
        return {
            "path": "config/advisory_setups.yaml",
            "mode": "review_existing_trusted_runtime_rules_only",
            "operation": "review_trusted_context_rule_runtime_effects",
            "variant": variant,
            "horizon_days": horizon_days,
            "rule_suggestion": {
                "signal_quality_overlay": variant,
                "minimum_horizon_days": horizon_days,
                "minimum_matured_rows": evidence.get("matured_count"),
                "minimum_lift_vs_technical_only": evidence.get("lift_vs_technical_only"),
                "minimum_avg_excess_forward_return_after_cost": evidence.get("avg_excess_forward_return_after_cost"),
                "minimum_excess_hit_rate_after_cost": evidence.get("excess_hit_rate_after_cost"),
                "action_policy_effect": "review_existing_trusted_context_rules_no_config_patch",
                "authority": "review_input_only",
                "broker_execution_allowed": False,
                "policy_auto_promotion_allowed": False,
            },
            "note": (
                "This variant evaluates existing trusted context-rule runtime adjustments. "
                "Do not add a generic signal_quality_overlay_rules config entry; inspect the existing per-source-family "
                "trusted rules and keep any change review-only until separately approved."
            ),
        }
    source_family = context_variant_source_family(variant)
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
            "minimum_avg_excess_forward_return_after_cost": evidence.get("avg_excess_forward_return_after_cost"),
            "minimum_excess_hit_rate_after_cost": evidence.get("excess_hit_rate_after_cost"),
            "context_source_family": source_family,
            "minimum_context_source_families": (
                DEFAULT_MIN_CONTEXT_SOURCE_FAMILIES
                if is_context_variant(variant) and source_family is None
                else None
            ),
            "minimum_context_family_selected_rows": DEFAULT_MIN_CONTEXT_SELECTED_ROWS if source_family else None,
            "minimum_context_family_matured_rows": DEFAULT_MIN_CONTEXT_MATURED_ROWS if source_family else None,
            "minimum_context_family_symbols": DEFAULT_MIN_CONTEXT_SYMBOLS if source_family else None,
            "authority": "review_input_only",
            "broker_execution_allowed": False,
        },
        "note": "Generated from signal-quality evidence. Review manually before changing config or action-consolidation rules.",
    }


def build_manual_patch_text(patch: dict[str, Any]) -> str:
    if patch.get("operation") == "review_trusted_context_rule_runtime_effects":
        suggestion = patch.get("rule_suggestion") if isinstance(patch.get("rule_suggestion"), dict) else {}
        return "\n".join(
            [
                "# Manual review guidance only. This is not a config patch.",
                "# The trusted context-rule variant evaluates existing runtime rules that already adjusted actions.",
                "# Do not add a generic signal_quality_overlay_rules entry for this variant.",
                f"# variant: {json.dumps(patch.get('variant'), ensure_ascii=False, default=str)}",
                f"# horizon_days: {json.dumps(patch.get('horizon_days'), ensure_ascii=False, default=str)}",
                f"# action_policy_effect: {json.dumps(suggestion.get('action_policy_effect'), ensure_ascii=False, default=str)}",
                "# Review the matched trusted per-source-family rules and only edit those specific rules after manual approval.",
            ]
        )
    suggestion = patch.get("rule_suggestion") if isinstance(patch.get("rule_suggestion"), dict) else {}
    lines = [
        "# Manual patch guidance only. Do not apply without reviewing signal-quality evidence across multiple runs.",
        "# Suggested review-only overlay influence rule:",
        "signal_quality_overlay_rules:",
        f"  - overlay: {json.dumps(suggestion.get('signal_quality_overlay'), ensure_ascii=False, default=str)}",
        f"    minimum_horizon_days: {json.dumps(suggestion.get('minimum_horizon_days'), ensure_ascii=False, default=str)}",
        f"    minimum_matured_rows: {json.dumps(suggestion.get('minimum_matured_rows'), ensure_ascii=False, default=str)}",
        f"    minimum_lift_vs_technical_only: {json.dumps(suggestion.get('minimum_lift_vs_technical_only'), ensure_ascii=False, default=str)}",
        f"    minimum_avg_excess_forward_return_after_cost: {json.dumps(suggestion.get('minimum_avg_excess_forward_return_after_cost'), ensure_ascii=False, default=str)}",
        f"    minimum_excess_hit_rate_after_cost: {json.dumps(suggestion.get('minimum_excess_hit_rate_after_cost'), ensure_ascii=False, default=str)}",
        f"    context_source_family: {json.dumps(suggestion.get('context_source_family'), ensure_ascii=False, default=str)}",
        f"    minimum_context_source_families: {json.dumps(suggestion.get('minimum_context_source_families'), ensure_ascii=False, default=str)}",
        f"    minimum_context_family_selected_rows: {json.dumps(suggestion.get('minimum_context_family_selected_rows'), ensure_ascii=False, default=str)}",
        f"    minimum_context_family_matured_rows: {json.dumps(suggestion.get('minimum_context_family_matured_rows'), ensure_ascii=False, default=str)}",
        f"    minimum_context_family_symbols: {json.dumps(suggestion.get('minimum_context_family_symbols'), ensure_ascii=False, default=str)}",
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
    avg_excess_return = pd.to_numeric(evidence.get("avg_excess_forward_return_after_cost"), errors="coerce")
    excess_hit_rate = pd.to_numeric(evidence.get("excess_hit_rate_after_cost"), errors="coerce")
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
    elif pd.isna(avg_excess_return) or pd.isna(excess_hit_rate):
        reasons.append("Benchmark-excess overlay evidence is missing; raw up-market returns are not enough for promotion review.")
        risks.append("Promotion is blocked until signal-quality evaluator records benchmark-excess returns and excess hit rate.")
    elif matured_count >= 30 and selected_count >= 10 and pd.notna(lift) and pd.notna(avg_return) and float(lift) >= 0.01 and float(avg_return) > 0 and float(avg_excess_return) > 0 and float(excess_hit_rate) >= 0.50:
        recommendation = "promote_overlay_review"
        reasons.append("Overlay has positive average return after costs, positive benchmark-excess return, and at least 1 percentage point lift versus technical-only.")
        if pd.notna(hit_rate) and float(hit_rate) < 0.45:
            risks.append("Hit rate is below 45%; improvement may come from a few large winners.")
    elif pd.notna(avg_return) and float(avg_return) > 0 and pd.notna(avg_excess_return) and float(avg_excess_return) <= 0:
        recommendation = "needs_more_data"
        reasons.append("Overlay return is positive but does not beat the benchmark after costs.")
        risks.append("The apparent overlay value may be broad market beta rather than source-family alpha.")
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
        safe_int(coverage.get("context_selected_rows")),
        safe_int(coverage.get("trusted_adjusted_selected_rows")),
    )
    if overlay_rows < 10:
        risks.append("Overlay source coverage is below 10 rows in the latest run.")

    if is_trusted_context_rule_variant(evidence.get("variant")):
        trusted_selected_rows = safe_int(coverage.get("trusted_adjusted_selected_rows"))
        trusted_matured_rows = safe_int(coverage.get("trusted_adjusted_matured_rows"))
        trusted_symbols = safe_int(coverage.get("trusted_adjusted_symbols"))
        if trusted_selected_rows < DEFAULT_MIN_CONTEXT_SELECTED_ROWS:
            reasons.append(
                f"Trusted context-rule adjusted selected coverage is below {DEFAULT_MIN_CONTEXT_SELECTED_ROWS} rows."
            )
            risks.append("Trusted runtime-rule outcome lift may be an artefact of sparse adjusted rows.")
            if recommendation == "promote_overlay_review":
                recommendation = "needs_more_data"
        if trusted_matured_rows < DEFAULT_MIN_CONTEXT_MATURED_ROWS:
            reasons.append(
                f"Trusted context-rule adjusted matured coverage is below {DEFAULT_MIN_CONTEXT_MATURED_ROWS} rows."
            )
            if recommendation == "promote_overlay_review":
                recommendation = "needs_more_data"
        if trusted_symbols < DEFAULT_MIN_CONTEXT_SYMBOLS:
            reasons.append(
                f"Trusted context-rule adjusted breadth is below {DEFAULT_MIN_CONTEXT_SYMBOLS} distinct symbols."
            )
            if recommendation == "promote_overlay_review":
                recommendation = "needs_more_data"

    if is_context_variant(evidence.get("variant")):
        source_family = context_variant_source_family(evidence.get("variant"))
        context_selected_rows = safe_int(coverage.get("context_selected_rows"))
        context_matured_rows = safe_int(coverage.get("context_matured_rows"))
        context_symbols = safe_int(coverage.get("context_symbols"))
        context_source_families = safe_int(coverage.get("context_source_families"))
        if source_family:
            family_selected_rows = safe_int(coverage.get("context_family_selected_rows"))
            family_matured_rows = safe_int(coverage.get("context_family_matured_rows"))
            family_symbols = safe_int(coverage.get("context_family_symbols"))
            if coverage.get("family_report_required") and not coverage.get("family_report_candidate_helpful"):
                classification = coverage.get("family_report_classification") or coverage.get("family_report_status") or "unknown"
                reasons.append(
                    f"{source_family} family report classification is {classification}, not candidate_helpful."
                )
                risks.append("Single-family context evidence has not passed the source-family outcome report gate.")
                if recommendation == "promote_overlay_review":
                    recommendation = "needs_more_data"
            if coverage.get("fast_reliability_blocks_broad_promotion"):
                classification = coverage.get("fast_reliability_classification") or coverage.get("fast_reliability_status") or "unknown"
                harmful_count = safe_int(coverage.get("fast_reliability_harmful_context_class_count"))
                harmful_sector_count = safe_int(coverage.get("fast_reliability_harmful_sector_count"))
                if str(coverage.get("fast_reliability_status") or "") == "unavailable_due_to_error":
                    reasons.append(
                        f"{source_family} fast context-overlay reliability evidence could not be loaded; broad family promotion is held for more data."
                    )
                    risks.append("Fast context-overlay reliability gate failed closed because evidence was unavailable.")
                elif harmful_count > 0:
                    reasons.append(
                        f"{source_family} fast context-overlay reliability contains {harmful_count} harmful context-class diagnostic rows."
                    )
                    risks.append(
                        "Fast context-overlay reliability shows harmful, negative-after-cost, or horizon-inconsistent "
                        "event classes; broad source-family promotion may hide those class-level failures."
                    )
                elif harmful_sector_count > 0:
                    reasons.append(
                        f"{source_family} fast context-overlay reliability contains {harmful_sector_count} harmful sector diagnostic rows."
                    )
                    risks.append(
                        "Fast context-overlay reliability shows harmful, negative-after-cost, benchmark-unattributed, "
                        "or horizon-inconsistent sector splits; broad source-family promotion may hide sector-level failures."
                    )
                else:
                    runtime_effect = (
                        (coverage.get("fast_reliability_runtime_policy_contract") or {}).get("runtime_policy_effect")
                        if isinstance(coverage.get("fast_reliability_runtime_policy_contract"), dict)
                        else None
                    )
                    reasons.append(
                        f"{source_family} fast context-overlay reliability runtime contract does not allow watch-priority use"
                        + (f" ({runtime_effect})." if runtime_effect else ".")
                    )
                    risks.append("Fast context-overlay reliability has not passed the source-family gate.")
                if recommendation == "promote_overlay_review":
                    recommendation = "needs_more_data"
            if coverage.get("split_negative_control_blocks_broad_promotion"):
                status = coverage.get("split_negative_control_status") or "unknown"
                block_reason = coverage.get("split_negative_control_block_reason") or "unknown"
                harmful_count = safe_int(coverage.get("split_negative_control_harmful_count"))
                baseline_unavailable_count = safe_int(coverage.get("split_negative_control_baseline_unavailable_count"))
                if status == "unavailable_due_to_error":
                    reasons.append(
                        f"{source_family} narrowed split negative-control evidence could not be loaded; broad family promotion is held for more data."
                    )
                    risks.append("Split-negative-control gate failed closed because the underlying evidence was unavailable.")
                elif block_reason == "technical_baseline_unavailable":
                    reasons.append(
                        f"{source_family} has {baseline_unavailable_count} narrowed split rows with incomplete matching technical-only baselines, so broad family promotion is held for more data."
                    )
                    risks.append(
                        "Broad source-family lift cannot be trusted until narrowed event-class or direction splits have complete technical-only baselines."
                    )
                else:
                    reasons.append(
                        f"{source_family} has {harmful_count} stable harmful narrowed split negative-control rows, so broad family promotion is blocked."
                    )
                    risks.append("Broad source-family lift may hide harmful event classes or directions.")
                if recommendation == "promote_overlay_review":
                    recommendation = "needs_more_data"
            if family_selected_rows < DEFAULT_MIN_CONTEXT_SELECTED_ROWS:
                reasons.append(
                    f"{source_family} selected coverage is below {DEFAULT_MIN_CONTEXT_SELECTED_ROWS} rows."
                )
                risks.append("Single-family context lift may be an artefact of sparse family-specific rows.")
                if recommendation == "promote_overlay_review":
                    recommendation = "needs_more_data"
            if family_matured_rows < DEFAULT_MIN_CONTEXT_MATURED_ROWS:
                reasons.append(
                    f"{source_family} matured coverage is below {DEFAULT_MIN_CONTEXT_MATURED_ROWS} rows."
                )
                if recommendation == "promote_overlay_review":
                    recommendation = "needs_more_data"
            if family_symbols < DEFAULT_MIN_CONTEXT_SYMBOLS:
                reasons.append(f"{source_family} breadth is below {DEFAULT_MIN_CONTEXT_SYMBOLS} distinct symbols.")
                if recommendation == "promote_overlay_review":
                    recommendation = "needs_more_data"
        else:
            if context_selected_rows < DEFAULT_MIN_CONTEXT_SELECTED_ROWS:
                reasons.append(
                    f"Context-overlay selected coverage is below {DEFAULT_MIN_CONTEXT_SELECTED_ROWS} rows."
                )
                risks.append("Context-overlay lift may be an artefact of sparse or missing context rows.")
                if recommendation == "promote_overlay_review":
                    recommendation = "needs_more_data"
            if context_matured_rows < DEFAULT_MIN_CONTEXT_MATURED_ROWS:
                reasons.append(
                    f"Context-overlay matured coverage is below {DEFAULT_MIN_CONTEXT_MATURED_ROWS} rows."
                )
                if recommendation == "promote_overlay_review":
                    recommendation = "needs_more_data"
            if context_symbols < DEFAULT_MIN_CONTEXT_SYMBOLS:
                reasons.append(f"Context-overlay breadth is below {DEFAULT_MIN_CONTEXT_SYMBOLS} distinct symbols.")
                if recommendation == "promote_overlay_review":
                    recommendation = "needs_more_data"
            if context_source_families < DEFAULT_MIN_CONTEXT_SOURCE_FAMILIES:
                reasons.append(
                    f"Context-overlay source diversity is below {DEFAULT_MIN_CONTEXT_SOURCE_FAMILIES} source families."
                )
                risks.append("Context-overlay lift may be driven by one noisy source family rather than independent evidence.")
                if recommendation == "promote_overlay_review":
                    recommendation = "needs_more_data"

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
    resolved_variant = str(evidence.get("variant") or variant)
    if coverage is None and is_trusted_context_rule_variant(resolved_variant):
        coverage = load_trusted_context_rule_coverage(
            evaluated_at=evidence.get("evaluated_at") or evaluated_at,
            horizon_days=int(evidence.get("horizon_days") or horizon_days),
            variant=resolved_variant,
        )
    elif coverage is None and is_context_variant(resolved_variant):
        coverage = load_context_variant_coverage(
            evaluated_at=evidence.get("evaluated_at") or evaluated_at,
            horizon_days=int(evidence.get("horizon_days") or horizon_days),
            variant=resolved_variant,
        )
    if coverage is not None and context_variant_source_family(resolved_variant):
        family_gate = load_source_family_report_gate(
            evaluated_at=evidence.get("evaluated_at") or evaluated_at,
            horizon_days=int(evidence.get("horizon_days") or horizon_days),
            variant=resolved_variant,
        )
        split_gate = load_split_negative_control_gate(
            source_family=context_variant_source_family(resolved_variant),
            horizon_days=int(evidence.get("horizon_days") or horizon_days),
        )
        fast_reliability_gate = load_persisted_fast_reliability_gate(
            evaluated_at=evidence.get("evaluated_at") or evaluated_at,
            source_family=context_variant_source_family(resolved_variant),
        )
        coverage = {**coverage, **family_gate, **fast_reliability_gate, **split_gate}
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


def generate_family_candidate_reviews(
    *,
    evaluated_at: Any | None = None,
    horizons: list[int] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    try:
        rows = load_family_summary(evaluated_at=evaluated_at, horizons=horizons)
        report = build_family_report(rows)
    except Exception as exc:
        if not is_missing_family_report_table_error(exc):
            raise
        report = build_unavailable_report(exc)
        return {
            "status": "evidence_unavailable",
            "mode": "auto_family_candidates",
            "persisted": bool(persist),
            "authority": "manual_config_review_only",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
            "family_report": report,
            "review_count": 0,
            "reviews": [],
            "note": "Signal-quality family evidence is unavailable. Run advisory.signal_quality_evaluator before generating family promotion reviews.",
        }
    reviews: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for family in report.get("families") or []:
        if family.get("classification") != "candidate_helpful":
            continue
        source_family = str(family.get("source_family") or "").strip().lower()
        for horizon in family.get("horizons") or []:
            if not isinstance(horizon, dict):
                continue
            if horizon.get("classification") != "candidate_helpful":
                continue
            variant = str(horizon.get("variant") or "").strip()
            horizon_days = int(horizon.get("horizon_days") or 0)
            if not variant or horizon_days <= 0:
                continue
            fast_reliability_gate = load_persisted_fast_reliability_gate(
                evaluated_at=report.get("evaluated_at") or evaluated_at,
                source_family=source_family,
            )
            if fast_reliability_gate.get("fast_reliability_blocks_broad_promotion"):
                reason = "blocked_by_fast_context_reliability"
                if fast_reliability_gate.get("fast_reliability_watch_priority_allowed") is False:
                    reason = "blocked_by_fast_context_runtime_contract"
                elif int(fast_reliability_gate.get("fast_reliability_harmful_sector_count") or 0) > 0:
                    reason = "blocked_by_fast_context_sector_reliability"
                skipped.append(
                    {
                        "source_family": source_family,
                        "horizon_days": horizon_days,
                        "variant": variant,
                        "reason": reason,
                        "fast_reliability_gate": fast_reliability_gate,
                    }
                )
                continue
            split_gate = load_split_negative_control_gate(source_family=source_family, horizon_days=horizon_days)
            if split_gate.get("split_negative_control_blocks_broad_promotion"):
                skip_reason = "blocked_by_harmful_narrowed_split_negative_control"
                if split_gate.get("split_negative_control_block_reason") == "technical_baseline_unavailable":
                    skip_reason = "blocked_by_incomplete_narrowed_split_technical_baseline"
                elif split_gate.get("split_negative_control_block_reason") == "unavailable_due_to_error":
                    skip_reason = "blocked_by_unavailable_narrowed_split_evidence"
                skipped.append(
                    {
                        "source_family": source_family,
                        "horizon_days": horizon_days,
                        "variant": variant,
                        "reason": skip_reason,
                        "split_negative_control_gate": split_gate,
                    }
                )
                continue
            # Passed all deterministic gates; record as an FDR candidate before promoting.
            matured_count = _safe_int(horizon.get("matured_count"))
            excess_hit_rate = horizon.get("excess_hit_rate_after_cost")
            excess_hits = (
                None
                if excess_hit_rate is None or matured_count <= 0
                else int(round(float(excess_hit_rate) * matured_count))
            )
            candidates.append(
                {
                    "source_family": source_family,
                    "horizon_days": horizon_days,
                    "variant": variant,
                    "matured_count": matured_count,
                    "excess_hit_rate_after_cost": excess_hit_rate,
                    "p_value": binomial_right_tail_p_value(excess_hits, matured_count) if excess_hits is not None else None,
                }
            )
    # Benjamini-Hochberg FDR control across the whole candidate batch: when many
    # family x horizon combos clear the deterministic gates together, a few can do so by chance.
    # The null per candidate is "beating the benchmark after costs is a coin flip" (excess hit p=0.5).
    p_values = [candidate["p_value"] for candidate in candidates]
    rejected = benjamini_hochberg(p_values, alpha=SIGNAL_QUALITY_PROMOTION_FDR_ALPHA)
    q_values = benjamini_hochberg_qvalues(p_values)
    for index, candidate in enumerate(candidates):
        if rejected[index]:
            reviews.append(
                generate_promotion_review(
                    evaluated_at=report.get("evaluated_at") or evaluated_at,
                    horizon_days=candidate["horizon_days"],
                    variant=candidate["variant"],
                    persist=persist,
                )
            )
        else:
            skipped.append(
                {
                    "source_family": candidate["source_family"],
                    "horizon_days": candidate["horizon_days"],
                    "variant": candidate["variant"],
                    "reason": (
                        "blocked_by_multiple_testing_fdr_control"
                        if candidate["p_value"] is not None
                        else "blocked_by_missing_benchmark_excess_stats_for_fdr"
                    ),
                    "fdr_p_value": candidate["p_value"],
                    "fdr_q_value": q_values[index],
                    "fdr_alpha": float(SIGNAL_QUALITY_PROMOTION_FDR_ALPHA),
                    "matured_count": candidate["matured_count"],
                    "excess_hit_rate_after_cost": candidate["excess_hit_rate_after_cost"],
                }
            )
    return {
        "status": "ok",
        "mode": "auto_family_candidates",
        "persisted": bool(persist),
        "authority": "manual_config_review_only",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        "family_report": report,
        "review_count": len(reviews),
        "skipped_count": len(skipped),
        "skipped": skipped,
        "reviews": reviews,
        "fdr_alpha": float(SIGNAL_QUALITY_PROMOTION_FDR_ALPHA),
        "fdr_candidate_count": len(candidates),
        "fdr_survived_count": int(sum(1 for flag in rejected if flag)),
        "note": "Generated promotion review rows only for source-family horizons classified as candidate_helpful, not blocked by harmful narrowed split negative controls, and surviving Benjamini-Hochberg FDR control across the candidate batch. No config, action, portfolio, or broker behavior was changed.",
    }


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
    parser.add_argument("--evaluated-at")
    parser.add_argument("--horizon-days", type=int)
    parser.add_argument("--variant")
    parser.add_argument("--auto-family-candidates", action="store_true")
    parser.add_argument("--horizons", type=int, nargs="*")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.auto_family_candidates:
        result = generate_family_candidate_reviews(
            evaluated_at=args.evaluated_at,
            horizons=args.horizons,
            persist=not bool(args.dry_run),
        )
    else:
        if not args.evaluated_at or not args.horizon_days or not args.variant:
            raise SystemExit("--evaluated-at, --horizon-days, and --variant are required unless --auto-family-candidates is used")
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
