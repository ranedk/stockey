from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from advisory.fallback_telemetry import record_local_fallback_event
from advisory.signal_quality_family_report import _is_missing_table_error
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration
from utils.sync import parse_datetime_arg


CONTEXT_WATCH_SUMMARY_TABLE = "advisory_context_watch_eval_summary"
NEGATIVE_PRESSURE_SUMMARY_TABLE = "advisory_negative_pressure_eval_summary"
RELIABILITY_SUMMARY_TABLE = "advisory_context_overlay_reliability_summary"
SCHEMA_MIGRATION_ID = "20260621_advisory_context_overlay_reliability_summary"
DEFAULT_MIN_MATURED_ROWS = 10
DEFAULT_MIN_OPPORTUNITY_HIT_RATE = 0.45
DEFAULT_MIN_AVG_WATCH_RETURN = 0.0
DEFAULT_MAX_NEGATIVE_RATE = 0.60
CONTEXT_FAMILIES = {
    "announcement_context",
    "exchange_context",
    "bhavcopy_context",
    "theme_context",
    "macro_context",
}
SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {RELIABILITY_SUMMARY_TABLE} (
        evaluated_at TIMESTAMPTZ NOT NULL,
        source_family TEXT NOT NULL,
        classification TEXT NOT NULL,
        total_selected_count BIGINT,
        total_matured_count BIGINT,
        watch_matured_count BIGINT,
        negative_pressure_matured_count BIGINT,
        avg_forward_return_after_cost DOUBLE PRECISION,
        avg_hit_rate_after_cost DOUBLE PRECISION,
        watch_horizons_json TEXT,
        negative_pressure_horizons_json TEXT,
        authority TEXT,
        broker_execution_allowed BOOLEAN,
        policy_auto_promotion_allowed BOOLEAN,
        report_json TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (evaluated_at, source_family)
    )
    """,
]


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    return str(value)


def _number(value: Any) -> float | None:
    numeric = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(numeric) else float(numeric)


def _int(value: Any) -> int:
    numeric = pd.to_numeric(value, errors="coerce")
    return 0 if pd.isna(numeric) else int(numeric)


def _context_class(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text or text in {"<NA>", "NAN", "NONE"}:
        return "UNSPECIFIED"
    return text


def _context_candidate_state(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text or text in {"<NA>", "NAN", "NONE"}:
        return "WATCH_EVENT"
    return text


def _context_policy_effect(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text or text in {"<na>", "nan", "none"}:
        return "watch_only_no_buy_authority"
    return text


def _sector_key(value: dict[str, Any]) -> str:
    sector_code = str(value.get("sector_code") or "").strip().upper()
    if sector_code and sector_code not in {"<NA>", "NAN", "NONE"}:
        return sector_code
    sector_name = str(value.get("sector_name") or value.get("sector_key") or "").strip()
    if sector_name and sector_name.upper() not in {"<NA>", "NAN", "NONE"}:
        return sector_name.upper()
    return "UNSPECIFIED"


def _summary_input_coverage(frame: pd.DataFrame, *, table_name: str) -> dict[str, Any]:
    if frame is None or frame.empty:
        return {
            "table": table_name,
            "row_count": 0,
            "latest_evaluated_at": None,
            "source_family_count": 0,
            "matured_count": 0,
            "benchmark_attributed_rows": 0,
            "benchmark_missing_rows": 0,
            "benchmark_attributed_matured_count": 0,
            "benchmark_missing_matured_count": 0,
            "benchmark_attribution_status": "no_matured_labels",
            "horizon_count": 0,
        }
    evaluated_at = None
    if "evaluated_at" in frame.columns:
        parsed = pd.to_datetime(frame["evaluated_at"], utc=True, errors="coerce").dropna()
        if not parsed.empty:
            evaluated_at = parsed.max().isoformat()
    source_family_count = 0
    if "source_context" in frame.columns:
        source_family_count = int(frame["source_context"].dropna().astype(str).str.strip().replace("", pd.NA).dropna().nunique())
    matured_count = 0
    if "matured_count" in frame.columns:
        matured_count = int(pd.to_numeric(frame["matured_count"], errors="coerce").fillna(0).sum())
    benchmark_attributed_rows = 0
    benchmark_missing_rows = 0
    benchmark_attributed_matured_count = 0
    benchmark_missing_matured_count = 0
    if "avg_benchmark_forward_return" in frame.columns:
        benchmark_available = pd.to_numeric(frame["avg_benchmark_forward_return"], errors="coerce").notna()
        benchmark_attributed_rows = int(benchmark_available.sum())
        benchmark_missing_rows = int((~benchmark_available).sum())
        if "matured_count" in frame.columns:
            matured_series = pd.to_numeric(frame["matured_count"], errors="coerce").fillna(0)
            benchmark_attributed_matured_count = int(matured_series[benchmark_available].sum())
            benchmark_missing_matured_count = int(matured_series[~benchmark_available].sum())
    else:
        benchmark_missing_rows = int(len(frame))
        benchmark_missing_matured_count = matured_count
    horizon_count = 0
    if "horizon_days" in frame.columns:
        horizon_count = int(pd.to_numeric(frame["horizon_days"], errors="coerce").dropna().nunique())
    return {
        "table": table_name,
        "row_count": int(len(frame)),
        "latest_evaluated_at": evaluated_at,
        "source_family_count": source_family_count,
        "matured_count": matured_count,
        "benchmark_attributed_rows": benchmark_attributed_rows,
        "benchmark_missing_rows": benchmark_missing_rows,
        "benchmark_attributed_matured_count": benchmark_attributed_matured_count,
        "benchmark_missing_matured_count": benchmark_missing_matured_count,
        "benchmark_attribution_status": (
            "no_matured_labels"
            if matured_count <= 0
            else "available"
            if benchmark_attributed_matured_count >= matured_count
            else "missing"
            if benchmark_attributed_matured_count <= 0
            else "partial"
        ),
        "horizon_count": horizon_count,
    }


def _empty_reason(watch_summary: pd.DataFrame, negative_summary: pd.DataFrame) -> str | None:
    watch_empty = watch_summary is None or watch_summary.empty
    negative_empty = negative_summary is None or negative_summary.empty
    if watch_empty and negative_empty:
        return "no_context_watch_or_negative_pressure_summary_rows"
    if watch_empty:
        return "no_context_watch_summary_rows"
    if negative_empty:
        return "no_negative_pressure_summary_rows"
    return "summary_rows_loaded_but_no_source_family_rows"


def _recommended_commands(status: str) -> list[dict[str, str]]:
    if status in {"ok", "candidate_helpful", "protective_candidate"}:
        return []
    return [
        {
            "command": "python -m advisory.context_watch_evaluator --horizons 5 10 20",
            "purpose": "Evaluate review-only positive/watch context-overlay rows against future OHLCV returns after costs.",
        },
        {
            "command": "python -m advisory.negative_pressure_evaluator --horizons 5 10 20",
            "purpose": "Evaluate review-only negative/de-risk context-overlay rows against future OHLCV returns and benchmark-relative protection.",
        },
        {
            "command": "python -m advisory.context_overlay_reliability_report --horizons 5 10 20 --format text",
            "purpose": "Rebuild and inspect source-family reliability after evaluator summaries exist.",
        },
    ]


def _report_status_for_families(families: list[dict[str, Any]]) -> str:
    if not families:
        return "no_data"
    total_matured = sum(_int(row.get("total_matured_count")) for row in families)
    classifications = {str(row.get("classification") or "").strip().lower() for row in families}
    if total_matured <= 0 or classifications <= {"needs_more_data", ""}:
        return "needs_more_data"
    if classifications & {"candidate_helpful", "protective_candidate"}:
        return "ok"
    if classifications & {"needs_benchmark_attribution"}:
        return "needs_benchmark_attribution"
    if classifications & {"benchmark_beta_not_overlay_alpha"}:
        return "benchmark_beta_not_overlay_alpha"
    if classifications & {"inconsistent_or_horizon_sensitive"}:
        return "inconsistent_or_horizon_sensitive"
    if classifications & {"hurts_or_no_lift", "negative_after_cost", "benchmark_beta_not_overlay_alpha"}:
        return "suppressed_or_harmful"
    return "monitor"


def _watch_breakout_blocker(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text or text in {"<na>", "nan"}:
        return "none"
    return text


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def reliability_runtime_policy_contract(classification: Any) -> dict[str, Any]:
    label = str(classification or "").strip().lower()
    if label == "candidate_helpful":
        runtime_effect = "watch_priority_allowed_no_buy_authority"
        operator_action = "Allow this source family only as review-only watchlist priority evidence after matching exact-class gates."
        allowed = {"watch_priority": True, "de_risk_review": False, "ranking_change": False}
    elif label == "protective_candidate":
        runtime_effect = "de_risk_review_allowed_no_sell_authority"
        operator_action = "Allow this source family only as review-only de-risk evidence for exposed/watchlisted symbols."
        allowed = {"watch_priority": False, "de_risk_review": True, "ranking_change": False}
    elif label == "inconsistent_or_horizon_sensitive":
        runtime_effect = "split_required_annotation_only"
        operator_action = "Split by context class, direction, sector, or horizon before any runtime influence is considered."
        allowed = {"watch_priority": False, "de_risk_review": False, "ranking_change": False}
    elif label in {"hurts_or_no_lift", "negative_after_cost", "needs_benchmark_attribution", "benchmark_beta_not_overlay_alpha"}:
        runtime_effect = "suppressed_annotation_only"
        operator_action = "Keep visible for explanation and diagnostics only; suppress watch/de-risk runtime influence."
        allowed = {"watch_priority": False, "de_risk_review": False, "ranking_change": False}
    else:
        runtime_effect = "collect_more_data_annotation_only"
        operator_action = "Collect more matured, benchmark-attributed evidence before runtime influence."
        allowed = {"watch_priority": False, "de_risk_review": False, "ranking_change": False}
    return {
        "classification": label or None,
        "runtime_policy_effect": runtime_effect,
        "allowed_runtime_uses": allowed,
        "authority_scope": "research_only_runtime_boundary",
        "portfolio_authority": "none",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        "technical_confirmation_required": True,
        "liquidity_risk_lifecycle_required": True,
        "operator_action": operator_action,
    }


def _record_reliability_fallback(
    *,
    fallback_type: str,
    source: str,
    reason: str,
    error: Exception,
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="advisory.context_overlay_reliability_report",
        fallback_type=fallback_type,
        source=source,
        severity="warn",
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def ensure_tables() -> None:
    apply_schema_migration(
        migration_id=SCHEMA_MIGRATION_ID,
        description="Create research-only context-overlay source-family reliability summary table.",
        statements=SCHEMA_STATEMENTS,
        metadata={
            "tables": [RELIABILITY_SUMMARY_TABLE],
            "workflow": "context_overlay_reliability_report",
            "authority": "research_only",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
        },
    )


def _load_latest_summary(
    *,
    table_name: str,
    evaluated_at: Any | None = None,
    asof_date: Any | None = None,
    horizons: list[int] | None = None,
) -> pd.DataFrame:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    parsed_evaluated_at = None
    if evaluated_at is not None:
        parsed_evaluated_at = pd.to_datetime(evaluated_at, utc=True, errors="coerce")
        if pd.isna(parsed_evaluated_at):
            raise ValueError(f"Invalid evaluated_at: {evaluated_at}")
        clauses.append("evaluated_at = %(evaluated_at)s")
        params["evaluated_at"] = parsed_evaluated_at
    if horizons:
        clauses.append("horizon_days = ANY(%(horizons)s)")
        params["horizons"] = [int(value) for value in horizons]
    if parsed_evaluated_at is None:
        cutoff = pd.to_datetime(asof_date or pd.Timestamp.utcnow(), utc=True, errors="coerce")
        if pd.isna(cutoff):
            cutoff = pd.Timestamp.utcnow()
        cutoff = cutoff.normalize() + pd.Timedelta(days=1)
        latest = sql_to_df(
            f"""
            SELECT MAX(evaluated_at) AS evaluated_at
            FROM {table_name}
            WHERE evaluated_at < %(cutoff)s
            """,
            params={"cutoff": cutoff},
            retries=3,
            statement_timeout_ms=10000,
        )
        if latest.empty or pd.isna(latest.iloc[0].get("evaluated_at")):
            return pd.DataFrame()
        parsed_evaluated_at = pd.to_datetime(latest.iloc[0]["evaluated_at"], utc=True, errors="coerce")
        if pd.isna(parsed_evaluated_at):
            return pd.DataFrame()
        clauses.append("evaluated_at = %(evaluated_at)s")
        params["evaluated_at"] = parsed_evaluated_at
    return sql_to_df(
        f"""
        SELECT *
        FROM {table_name}
        WHERE {' AND '.join(clauses)}
        ORDER BY horizon_days, signal_source, source_context
        """,
        params=params,
        retries=3,
        statement_timeout_ms=10000,
    )


def load_context_watch_summary(
    *,
    evaluated_at: Any | None = None,
    asof_date: Any | None = None,
    horizons: list[int] | None = None,
) -> pd.DataFrame:
    try:
        return _load_latest_summary(
            table_name=CONTEXT_WATCH_SUMMARY_TABLE,
            evaluated_at=evaluated_at,
            asof_date=asof_date,
            horizons=horizons,
        )
    except Exception as exc:
        _record_reliability_fallback(
            fallback_type="context_overlay_reliability_watch_summary_load_failed",
            source=CONTEXT_WATCH_SUMMARY_TABLE,
            reason="Context-overlay reliability report could not load positive/watch evaluator summary rows.",
            error=exc,
            metadata={"evaluated_at": str(evaluated_at) if evaluated_at is not None else None, "asof_date": str(asof_date) if asof_date is not None else None, "horizons": horizons},
        )
        if _is_missing_table_error(exc):
            return pd.DataFrame()
        raise


def load_negative_pressure_summary(
    *,
    evaluated_at: Any | None = None,
    asof_date: Any | None = None,
    horizons: list[int] | None = None,
) -> pd.DataFrame:
    try:
        return _load_latest_summary(
            table_name=NEGATIVE_PRESSURE_SUMMARY_TABLE,
            evaluated_at=evaluated_at,
            asof_date=asof_date,
            horizons=horizons,
        )
    except Exception as exc:
        _record_reliability_fallback(
            fallback_type="context_overlay_reliability_negative_summary_load_failed",
            source=NEGATIVE_PRESSURE_SUMMARY_TABLE,
            reason="Context-overlay reliability report could not load negative-pressure evaluator summary rows.",
            error=exc,
            metadata={"evaluated_at": str(evaluated_at) if evaluated_at is not None else None, "asof_date": str(asof_date) if asof_date is not None else None, "horizons": horizons},
        )
        if _is_missing_table_error(exc):
            return pd.DataFrame()
        raise


def _watch_horizon_row(row: dict[str, Any], *, min_matured_rows: int) -> dict[str, Any]:
    matured = _int(row.get("matured_count"))
    avg_return = _number(row.get("avg_watch_return_after_cost"))
    avg_excess_return = _number(row.get("avg_excess_watch_return_after_cost"))
    hit_rate = _number(row.get("opportunity_hit_rate_after_cost"))
    excess_hit_rate = _number(row.get("excess_opportunity_hit_rate_after_cost"))
    negative_rate = _number(row.get("negative_after_cost_rate"))
    negative_excess_rate = _number(row.get("negative_excess_after_cost_rate"))
    attribution_status = "available" if avg_excess_return is not None and excess_hit_rate is not None else "missing"
    if matured < int(min_matured_rows):
        reliability = "needs_more_data"
    elif attribution_status != "available":
        reliability = "needs_benchmark_attribution"
    elif avg_excess_return is not None and avg_excess_return <= 0:
        reliability = "negative_after_cost"
    elif negative_excess_rate is not None and negative_excess_rate >= DEFAULT_MAX_NEGATIVE_RATE:
        reliability = "hurts_or_no_lift"
    elif (
        avg_return is not None
        and avg_return > DEFAULT_MIN_AVG_WATCH_RETURN
        and avg_excess_return is not None
        and avg_excess_return > DEFAULT_MIN_AVG_WATCH_RETURN
        and hit_rate is not None
        and hit_rate >= DEFAULT_MIN_OPPORTUNITY_HIT_RATE
        and excess_hit_rate is not None
        and excess_hit_rate >= DEFAULT_MIN_OPPORTUNITY_HIT_RATE
    ):
        reliability = "candidate_helpful"
    else:
        reliability = "monitor"
    return {
        "horizon_days": _int(row.get("horizon_days")),
        "source_context": str(row.get("source_context") or "").strip().lower(),
        "context_class": _context_class(row.get("context_class")),
        "context_candidate_state": _context_candidate_state(row.get("context_candidate_state")),
        "context_policy_effect": _context_policy_effect(row.get("context_policy_effect")),
        "watch_breakout_blocker": _watch_breakout_blocker(row.get("watch_breakout_blocker")),
        "matured_count": matured,
        "symbol_count": _int(row.get("symbol_count")),
        "avg_forward_return": _number(row.get("avg_forward_return")),
        "avg_benchmark_forward_return": _number(row.get("avg_benchmark_forward_return")),
        "avg_watch_return_after_cost": avg_return,
        "avg_excess_watch_return_after_cost": avg_excess_return,
        "opportunity_hit_rate_after_cost": hit_rate,
        "excess_opportunity_hit_rate_after_cost": excess_hit_rate,
        "negative_after_cost_rate": negative_rate,
        "negative_excess_after_cost_rate": negative_excess_rate,
        "classification": row.get("classification"),
        "reliability_classification": reliability,
        "benchmark_attribution_status": attribution_status,
        "sector_diagnostics": _parse_jsonish(row.get("sector_diagnostics_json"), []),
        "evidence_type": "positive_context_watch",
    }


def _negative_horizon_row(row: dict[str, Any], *, min_matured_rows: int) -> dict[str, Any]:
    matured = _int(row.get("matured_count"))
    avg_avoided = _number(row.get("avg_avoided_return_after_cost"))
    avg_excess_avoided = _number(row.get("avg_excess_avoided_return_after_cost"))
    hit_rate = _number(row.get("protective_hit_rate_after_cost"))
    excess_hit_rate = _number(row.get("excess_protective_hit_rate_after_cost"))
    false_positive_rate = _number(row.get("false_positive_rate_after_cost"))
    false_positive_excess_rate = _number(row.get("false_positive_excess_rate_after_cost"))
    attribution_status = "available" if avg_excess_avoided is not None and excess_hit_rate is not None else "missing"
    if matured < int(min_matured_rows):
        reliability = "needs_more_data"
    elif attribution_status != "available":
        reliability = "needs_benchmark_attribution"
    elif str(row.get("classification") or "").strip().lower() == "benchmark_beta_not_derisk_alpha":
        reliability = "benchmark_beta_not_overlay_alpha"
    elif false_positive_excess_rate is not None and false_positive_excess_rate >= DEFAULT_MAX_NEGATIVE_RATE:
        reliability = "hurts_or_no_lift"
    elif (
        avg_avoided is not None
        and avg_avoided > 0
        and avg_excess_avoided is not None
        and avg_excess_avoided > 0
        and hit_rate is not None
        and hit_rate >= DEFAULT_MIN_OPPORTUNITY_HIT_RATE
        and excess_hit_rate is not None
        and excess_hit_rate >= DEFAULT_MIN_OPPORTUNITY_HIT_RATE
    ):
        reliability = "protective_candidate"
    else:
        reliability = "monitor"
    return {
        "horizon_days": _int(row.get("horizon_days")),
        "source_context": str(row.get("source_context") or "").strip().lower(),
        "context_class": _context_class(row.get("context_class")),
        "matured_count": matured,
        "symbol_count": _int(row.get("symbol_count")),
        "avg_forward_return": _number(row.get("avg_forward_return")),
        "avg_benchmark_forward_return": _number(row.get("avg_benchmark_forward_return")),
        "avg_avoided_return_after_cost": avg_avoided,
        "avg_excess_avoided_return_after_cost": avg_excess_avoided,
        "protective_hit_rate_after_cost": hit_rate,
        "excess_protective_hit_rate_after_cost": excess_hit_rate,
        "false_positive_rate_after_cost": false_positive_rate,
        "false_positive_excess_rate_after_cost": false_positive_excess_rate,
        "classification": row.get("classification"),
        "reliability_classification": reliability,
        "benchmark_attribution_status": attribution_status,
        "sector_diagnostics": _parse_jsonish(row.get("sector_diagnostics_json"), []),
        "evidence_type": "negative_context_pressure",
    }


def _aggregate_context_class(
    *,
    source_family: str,
    context_class: str,
    watch_rows: list[dict[str, Any]],
    negative_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    row = _aggregate_family_core(source_family, watch_rows, negative_rows)
    row["context_class"] = context_class
    row["split_axis"] = "context_class"
    row["policy_effect"] = "diagnostic_only_no_policy_change"
    return row


def _aggregate_watch_state(
    *,
    source_family: str,
    context_candidate_state: str,
    context_policy_effect: str,
    watch_breakout_blocker: str,
    watch_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    row = _aggregate_family_core(source_family, watch_rows, [], include_watch_state_diagnostics=False)
    row["context_candidate_state"] = context_candidate_state
    row["context_policy_effect"] = context_policy_effect
    row["watch_breakout_blocker"] = watch_breakout_blocker
    row["split_axis"] = "context_candidate_state_and_breakout_blocker"
    row["policy_effect"] = "diagnostic_only_no_policy_change"
    return row


def _sector_child_rows(rows: list[dict[str, Any]], *, evidence_type: str, min_matured_rows: int) -> list[dict[str, Any]]:
    children: list[dict[str, Any]] = []
    for row in rows:
        for item in row.get("sector_diagnostics") or []:
            if not isinstance(item, dict):
                continue
            child = {
                **item,
                "source_context": row.get("source_context"),
                "context_class": row.get("context_class"),
                "horizon_days": row.get("horizon_days"),
                "evidence_type": evidence_type,
                "reliability_classification": (
                    _watch_horizon_row(item, min_matured_rows=min_matured_rows).get("reliability_classification")
                    if evidence_type == "positive_context_watch"
                    else _negative_horizon_row(item, min_matured_rows=min_matured_rows).get("reliability_classification")
                ),
                "benchmark_attribution_status": (
                    "available"
                    if _number(item.get("avg_excess_watch_return_after_cost") if evidence_type == "positive_context_watch" else item.get("avg_excess_avoided_return_after_cost")) is not None
                    else "missing"
                ),
            }
            children.append(child)
    return children


def _aggregate_sector(
    *,
    source_family: str,
    sector_key: str,
    watch_rows: list[dict[str, Any]],
    negative_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    row = _aggregate_family_core(source_family, watch_rows, negative_rows, include_watch_state_diagnostics=False)
    row["sector_key"] = sector_key
    row["sector_name"] = next((item.get("sector_name") for item in [*watch_rows, *negative_rows] if item.get("sector_name")), None)
    row["sector_code"] = next((item.get("sector_code") for item in [*watch_rows, *negative_rows] if item.get("sector_code")), None)
    row["split_axis"] = "sector"
    row["policy_effect"] = "diagnostic_only_no_policy_change"
    return row


def _aggregate_family_core(
    source_family: str,
    watch_rows: list[dict[str, Any]],
    negative_rows: list[dict[str, Any]],
    *,
    include_watch_state_diagnostics: bool = True,
) -> dict[str, Any]:
    watch_classes = {str(row.get("reliability_classification")) for row in watch_rows}
    negative_classes = {str(row.get("reliability_classification")) for row in negative_rows}
    all_classes = watch_classes | negative_classes
    helpful_classes = {"candidate_helpful", "protective_candidate"}
    harmful_classes = {"negative_after_cost", "hurts_or_no_lift", "benchmark_beta_not_overlay_alpha"}
    total_matured = sum(_int(row.get("matured_count")) for row in [*watch_rows, *negative_rows])
    watch_matured = sum(_int(row.get("matured_count")) for row in watch_rows)
    negative_matured = sum(_int(row.get("matured_count")) for row in negative_rows)
    if all_classes & helpful_classes and all_classes & harmful_classes:
        classification = "inconsistent_or_horizon_sensitive"
    elif "needs_benchmark_attribution" in all_classes:
        classification = "needs_benchmark_attribution"
    elif "candidate_helpful" in watch_classes:
        classification = "candidate_helpful"
    elif "negative_after_cost" in watch_classes or "hurts_or_no_lift" in watch_classes:
        classification = "hurts_or_no_lift"
    elif "benchmark_beta_not_overlay_alpha" in all_classes:
        classification = "benchmark_beta_not_overlay_alpha"
    elif "protective_candidate" in negative_classes:
        classification = "protective_candidate"
    elif total_matured <= 0 or ("needs_more_data" in all_classes and len(all_classes) == 1):
        classification = "needs_more_data"
    else:
        classification = "monitor"
    row = {
        "source_family": source_family,
        "classification": classification,
        "total_selected_count": total_matured,
        "total_matured_count": total_matured,
        "watch_matured_count": watch_matured,
        "negative_pressure_matured_count": negative_matured,
        "avg_forward_return_after_cost": _number(pd.Series([row.get("avg_watch_return_after_cost") for row in watch_rows]).dropna().mean()) if watch_rows else None,
        "avg_excess_return_after_cost": _number(pd.Series([row.get("avg_excess_watch_return_after_cost") for row in watch_rows]).dropna().mean()) if watch_rows else None,
        "avg_hit_rate_after_cost": _number(pd.Series([row.get("opportunity_hit_rate_after_cost") for row in watch_rows]).dropna().mean()) if watch_rows else None,
        "avg_excess_hit_rate_after_cost": _number(pd.Series([row.get("excess_opportunity_hit_rate_after_cost") for row in watch_rows]).dropna().mean()) if watch_rows else None,
        "benchmark_attribution_status": (
            "available"
            if all(str(row.get("benchmark_attribution_status") or "") == "available" for row in [*watch_rows, *negative_rows])
            else "missing"
        ),
        "watch_horizons": watch_rows,
        "negative_pressure_horizons": negative_rows,
        "authority": "research_only",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
    }
    row["runtime_policy_contract"] = reliability_runtime_policy_contract(classification)
    if not include_watch_state_diagnostics:
        return row
    watch_state_keys = sorted(
        {
            (
                _context_candidate_state(item.get("context_candidate_state")),
                _context_policy_effect(item.get("context_policy_effect")),
                _watch_breakout_blocker(item.get("watch_breakout_blocker")),
            )
            for item in watch_rows
        }
    )
    watch_state_diagnostics = [
        _aggregate_watch_state(
            source_family=source_family,
            context_candidate_state=context_candidate_state,
            context_policy_effect=context_policy_effect,
            watch_breakout_blocker=watch_breakout_blocker,
            watch_rows=[
                item
                for item in watch_rows
                if _context_candidate_state(item.get("context_candidate_state")) == context_candidate_state
                and _context_policy_effect(item.get("context_policy_effect")) == context_policy_effect
                and _watch_breakout_blocker(item.get("watch_breakout_blocker")) == watch_breakout_blocker
            ],
        )
        for context_candidate_state, context_policy_effect, watch_breakout_blocker in watch_state_keys
    ]
    watch_state_diagnostics.sort(
        key=lambda item: (
            item.get("classification") == "candidate_helpful",
            item.get("classification") in {"hurts_or_no_lift", "negative_after_cost", "benchmark_beta_not_overlay_alpha"},
            _int(item.get("total_matured_count")),
        ),
        reverse=True,
    )
    row["watch_state_diagnostics"] = watch_state_diagnostics
    row["watch_state_count"] = len(watch_state_diagnostics)
    row["helpful_watch_state_count"] = sum(1 for item in watch_state_diagnostics if item.get("classification") == "candidate_helpful")
    row["harmful_watch_state_count"] = sum(
        1 for item in watch_state_diagnostics if item.get("classification") in {"hurts_or_no_lift", "negative_after_cost", "benchmark_beta_not_overlay_alpha"}
    )
    row["watch_state_policy_effect"] = "diagnostic_only_no_policy_change"
    return row


def _aggregate_family(
    source_family: str,
    watch_rows: list[dict[str, Any]],
    negative_rows: list[dict[str, Any]],
    *,
    min_matured_rows: int = DEFAULT_MIN_MATURED_ROWS,
) -> dict[str, Any]:
    row = _aggregate_family_core(source_family, watch_rows, negative_rows)
    class_names = sorted(
        {
            _context_class(item.get("context_class"))
            for item in [*watch_rows, *negative_rows]
            if _context_class(item.get("context_class"))
        }
    )
    context_class_diagnostics = [
        _aggregate_context_class(
            source_family=source_family,
            context_class=context_class,
            watch_rows=[item for item in watch_rows if _context_class(item.get("context_class")) == context_class],
            negative_rows=[item for item in negative_rows if _context_class(item.get("context_class")) == context_class],
        )
        for context_class in class_names
    ]
    context_class_diagnostics.sort(
        key=lambda item: (
            item.get("classification") in {"candidate_helpful", "protective_candidate"},
            item.get("classification") in {"hurts_or_no_lift", "negative_after_cost", "benchmark_beta_not_overlay_alpha"},
            _int(item.get("total_matured_count")),
        ),
        reverse=True,
    )
    row["context_class_diagnostics"] = context_class_diagnostics
    row["context_class_count"] = len(context_class_diagnostics)
    row["helpful_context_class_count"] = sum(
        1 for item in context_class_diagnostics if item.get("classification") in {"candidate_helpful", "protective_candidate"}
    )
    row["harmful_context_class_count"] = sum(
        1 for item in context_class_diagnostics if item.get("classification") in {"hurts_or_no_lift", "negative_after_cost", "benchmark_beta_not_overlay_alpha"}
    )
    row["context_class_policy_effect"] = "diagnostic_only_no_policy_change"
    watch_sector_rows = _sector_child_rows(watch_rows, evidence_type="positive_context_watch", min_matured_rows=min_matured_rows)
    negative_sector_rows = _sector_child_rows(negative_rows, evidence_type="negative_context_pressure", min_matured_rows=min_matured_rows)
    sector_names = sorted({_sector_key(item) for item in [*watch_sector_rows, *negative_sector_rows]})
    sector_diagnostics = [
        _aggregate_sector(
            source_family=source_family,
            sector_key=sector_key,
            watch_rows=[item for item in watch_sector_rows if _sector_key(item) == sector_key],
            negative_rows=[item for item in negative_sector_rows if _sector_key(item) == sector_key],
        )
        for sector_key in sector_names
    ]
    sector_diagnostics.sort(
        key=lambda item: (
            item.get("classification") in {"candidate_helpful", "protective_candidate"},
            item.get("classification") in {"hurts_or_no_lift", "negative_after_cost", "benchmark_beta_not_overlay_alpha"},
            _int(item.get("total_matured_count")),
        ),
        reverse=True,
    )
    row["sector_diagnostics"] = sector_diagnostics
    row["sector_count"] = len(sector_diagnostics)
    row["helpful_sector_count"] = sum(1 for item in sector_diagnostics if item.get("classification") in {"candidate_helpful", "protective_candidate"})
    row["harmful_sector_count"] = sum(
        1 for item in sector_diagnostics if item.get("classification") in {"hurts_or_no_lift", "negative_after_cost", "benchmark_beta_not_overlay_alpha"}
    )
    row["sector_policy_effect"] = "diagnostic_only_no_policy_change"
    return row


def build_reliability_report(
    *,
    watch_summary: pd.DataFrame,
    negative_summary: pd.DataFrame,
    min_matured_rows: int = DEFAULT_MIN_MATURED_ROWS,
) -> dict[str, Any]:
    family_names = set(CONTEXT_FAMILIES)
    watch_by_family: dict[str, list[dict[str, Any]]] = {}
    negative_by_family: dict[str, list[dict[str, Any]]] = {}
    if not watch_summary.empty:
        for row in watch_summary.to_dict(orient="records"):
            family = str(row.get("source_context") or "").strip().lower()
            if not family:
                continue
            family_names.add(family)
            watch_by_family.setdefault(family, []).append(_watch_horizon_row(row, min_matured_rows=min_matured_rows))
    if not negative_summary.empty:
        for row in negative_summary.to_dict(orient="records"):
            family = str(row.get("source_context") or "").strip().lower()
            if not family:
                continue
            family_names.add(family)
            negative_by_family.setdefault(family, []).append(_negative_horizon_row(row, min_matured_rows=min_matured_rows))
    families = [
        _aggregate_family(
            family,
            watch_by_family.get(family, []),
            negative_by_family.get(family, []),
            min_matured_rows=min_matured_rows,
        )
        for family in sorted(family_names)
        if watch_by_family.get(family) or negative_by_family.get(family)
    ]
    families.sort(
        key=lambda row: (
            row.get("classification") == "candidate_helpful",
            row.get("classification") == "protective_candidate",
            _int(row.get("total_matured_count")),
        ),
        reverse=True,
    )
    status = _report_status_for_families(families)
    return {
        "status": status,
        "authority": "research_only",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        "headline": (
            "Fast context-overlay reliability evidence is available."
            if status == "ok"
            else "Fast context-overlay reliability evidence exists but needs more matured labels."
            if status == "needs_more_data"
            else "No fast context-overlay reliability evidence is available yet."
        ),
        "family_count": len(families),
        "candidate_helpful_count": sum(1 for row in families if row.get("classification") == "candidate_helpful"),
        "protective_candidate_count": sum(1 for row in families if row.get("classification") == "protective_candidate"),
        "benchmark_beta_not_overlay_alpha_count": sum(1 for row in families if row.get("classification") == "benchmark_beta_not_overlay_alpha"),
        "input_coverage": {
            "context_watch": _summary_input_coverage(watch_summary, table_name=CONTEXT_WATCH_SUMMARY_TABLE),
            "negative_pressure": _summary_input_coverage(negative_summary, table_name=NEGATIVE_PRESSURE_SUMMARY_TABLE),
        },
        "empty_reason": "summary_rows_exist_but_no_matured_labels" if status == "needs_more_data" else None if families else _empty_reason(watch_summary, negative_summary),
        "recommended_commands": _recommended_commands(status),
        "families": families,
        "best_family": families[0] if families else None,
        "next_actions": [
            "Use candidate_helpful positive watch families as review-only watchlist intake evidence.",
            "Keep hurts_or_no_lift and negative_after_cost families out of context-overlay watch intake.",
            "Keep benchmark_beta_not_overlay_alpha families suppressed; raw positive returns without benchmark excess are not overlay alpha.",
            "Keep protective_candidate negative-pressure families research-only until reviewed de-risk policy gates pass.",
            "Do not promote families marked needs_benchmark_attribution; rerun context evaluators after benchmark rows are available.",
        ],
    }


def load_reliability_report(
    *,
    evaluated_at: Any | None = None,
    asof_date: Any | None = None,
    horizons: list[int] | None = None,
    min_matured_rows: int = DEFAULT_MIN_MATURED_ROWS,
) -> dict[str, Any]:
    watch = load_context_watch_summary(evaluated_at=evaluated_at, asof_date=asof_date, horizons=horizons)
    negative = load_negative_pressure_summary(evaluated_at=evaluated_at, asof_date=asof_date, horizons=horizons)
    report = build_reliability_report(watch_summary=watch, negative_summary=negative, min_matured_rows=min_matured_rows)
    evaluated_candidates = []
    for frame in [watch, negative]:
        if not frame.empty and "evaluated_at" in frame.columns:
            evaluated_candidates.extend(pd.to_datetime(frame["evaluated_at"], utc=True, errors="coerce").dropna().tolist())
    report["evaluated_at"] = max(evaluated_candidates) if evaluated_candidates else None
    report["evidence_tables"] = {
        "context_watch": CONTEXT_WATCH_SUMMARY_TABLE,
        "negative_pressure": NEGATIVE_PRESSURE_SUMMARY_TABLE,
    }
    return report


def report_to_rows(report: dict[str, Any]) -> pd.DataFrame:
    evaluated_at = pd.to_datetime(report.get("evaluated_at"), utc=True, errors="coerce")
    if pd.isna(evaluated_at):
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for item in report.get("families") or []:
        source_family = str(item.get("source_family") or "").strip().lower()
        classification = str(item.get("classification") or "").strip()
        if not source_family or not classification:
            continue
        serialized_item = dict(item)
        if "runtime_policy_contract" not in serialized_item:
            serialized_item["runtime_policy_contract"] = reliability_runtime_policy_contract(classification)
            serialized_item["runtime_policy_contract_synthesized"] = True
        rows.append(
            {
                "evaluated_at": evaluated_at,
                "source_family": source_family,
                "classification": classification,
                "total_selected_count": _int(item.get("total_selected_count")),
                "total_matured_count": _int(item.get("total_matured_count")),
                "watch_matured_count": _int(item.get("watch_matured_count")),
                "negative_pressure_matured_count": _int(item.get("negative_pressure_matured_count")),
                "avg_forward_return_after_cost": _number(item.get("avg_forward_return_after_cost")),
                "avg_hit_rate_after_cost": _number(item.get("avg_hit_rate_after_cost")),
                "watch_horizons_json": json_dumps(item.get("watch_horizons") or []),
                "negative_pressure_horizons_json": json_dumps(item.get("negative_pressure_horizons") or []),
                "authority": "research_only",
                "broker_execution_allowed": False,
                "policy_auto_promotion_allowed": False,
                "report_json": json_dumps(serialized_item),
                "load_ts": pd.Timestamp.utcnow(),
            }
        )
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    for column in [
        "total_selected_count",
        "total_matured_count",
        "watch_matured_count",
        "negative_pressure_matured_count",
        "avg_forward_return_after_cost",
        "avg_hit_rate_after_cost",
    ]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    for column in ["broker_execution_allowed", "policy_auto_promotion_allowed"]:
        frame[column] = frame[column].astype("boolean")
    frame["evaluated_at"] = pd.to_datetime(frame["evaluated_at"], utc=True, errors="coerce")
    frame["load_ts"] = pd.to_datetime(frame["load_ts"], utc=True, errors="coerce")
    return frame


def persist_reliability_report(report: dict[str, Any]) -> int:
    ensure_tables()
    rows = report_to_rows(report)
    if rows.empty:
        return 0
    upsert_to_db(rows, RELIABILITY_SUMMARY_TABLE, unique_keys=["evaluated_at", "source_family"])
    return int(len(rows))


def _parse_jsonish(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if pd.api.types.is_scalar(value) and pd.isna(value):
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        record_local_fallback_event(
            module="advisory.context_overlay_reliability_report",
            fallback_type="context_overlay_reliability_json_parse_failed",
            source="persisted_reliability_json",
            severity="warn",
            reason="Context-overlay reliability report could not parse persisted JSON and used a conservative default.",
            error=exc,
            metadata={"value_type": value.__class__.__name__},
        )
        return default


def load_persisted_reliability_report(
    *,
    evaluated_at: Any | None = None,
    asof_date: Any | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    clauses: list[str] = []
    parsed_evaluated_at = None
    if evaluated_at is not None:
        parsed_evaluated_at = pd.to_datetime(evaluated_at, utc=True, errors="coerce")
        if pd.isna(parsed_evaluated_at):
            raise ValueError(f"Invalid evaluated_at: {evaluated_at}")
        clauses.append("evaluated_at = %(evaluated_at)s")
        params["evaluated_at"] = parsed_evaluated_at
    try:
        if parsed_evaluated_at is None:
            cutoff = pd.to_datetime(asof_date or pd.Timestamp.utcnow(), utc=True, errors="coerce")
            if pd.isna(cutoff):
                cutoff = pd.Timestamp.utcnow()
            cutoff = cutoff.normalize() + pd.Timedelta(days=1)
            latest = sql_to_df(
                f"""
                SELECT MAX(evaluated_at) AS evaluated_at
                FROM {RELIABILITY_SUMMARY_TABLE}
                WHERE evaluated_at < %(cutoff)s
                """,
                params={"cutoff": cutoff},
                retries=3,
                statement_timeout_ms=10000,
            )
            if latest.empty or pd.isna(latest.iloc[0].get("evaluated_at")):
                return {"status": "no_data", "families": [], "evaluated_at": None}
            parsed_evaluated_at = pd.to_datetime(latest.iloc[0]["evaluated_at"], utc=True, errors="coerce")
            if pd.isna(parsed_evaluated_at):
                return {"status": "no_data", "families": [], "evaluated_at": None}
            clauses.append("evaluated_at = %(evaluated_at)s")
            params["evaluated_at"] = parsed_evaluated_at
        rows = sql_to_df(
            f"""
            SELECT *
            FROM {RELIABILITY_SUMMARY_TABLE}
            WHERE {' AND '.join(clauses)}
            ORDER BY source_family
            """,
            params=params,
            retries=3,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        _record_reliability_fallback(
            fallback_type="context_overlay_reliability_persisted_load_failed",
            source=RELIABILITY_SUMMARY_TABLE,
            reason="Context-overlay reliability report could not load persisted source-family reliability rows.",
            error=exc,
            metadata={"evaluated_at": str(evaluated_at) if evaluated_at is not None else None, "asof_date": str(asof_date) if asof_date is not None else None},
        )
        if _is_missing_table_error(exc):
            return {"status": "evidence_unavailable", "families": [], "evaluated_at": None}
        raise
    if rows.empty:
        return {"status": "no_data", "families": [], "evaluated_at": parsed_evaluated_at}
    families = []
    for row in rows.to_dict(orient="records"):
        parsed = _parse_jsonish(row.get("report_json"), {})
        if not isinstance(parsed, dict) or not parsed:
            parsed = {
                "source_family": row.get("source_family"),
                "classification": row.get("classification"),
                "total_selected_count": _int(row.get("total_selected_count")),
                "total_matured_count": _int(row.get("total_matured_count")),
                "watch_matured_count": _int(row.get("watch_matured_count")),
                "negative_pressure_matured_count": _int(row.get("negative_pressure_matured_count")),
                "avg_forward_return_after_cost": _number(row.get("avg_forward_return_after_cost")),
                "avg_hit_rate_after_cost": _number(row.get("avg_hit_rate_after_cost")),
                "watch_horizons": _parse_jsonish(row.get("watch_horizons_json"), []),
                "negative_pressure_horizons": _parse_jsonish(row.get("negative_pressure_horizons_json"), []),
                "authority": "research_only",
                "broker_execution_allowed": False,
                "policy_auto_promotion_allowed": False,
            }
        if "runtime_policy_contract" not in parsed:
            parsed["runtime_policy_contract"] = reliability_runtime_policy_contract(parsed.get("classification"))
            parsed["runtime_policy_contract_synthesized"] = True
        families.append(parsed)
    status = _report_status_for_families(families)
    return {
        "status": status,
        "authority": "research_only",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        "headline": (
            "Persisted fast context-overlay reliability evidence is available."
            if status == "ok"
            else "Persisted fast context-overlay reliability evidence exists but needs more matured labels."
            if status == "needs_more_data"
            else "Persisted fast context-overlay reliability evidence is not usable yet."
        ),
        "evaluated_at": parsed_evaluated_at,
        "family_count": len(families),
        "candidate_helpful_count": sum(1 for row in families if row.get("classification") == "candidate_helpful"),
        "protective_candidate_count": sum(1 for row in families if row.get("classification") == "protective_candidate"),
        "benchmark_beta_not_overlay_alpha_count": sum(1 for row in families if row.get("classification") == "benchmark_beta_not_overlay_alpha"),
        "empty_reason": "persisted_families_exist_but_no_matured_labels" if status == "needs_more_data" else None,
        "recommended_commands": _recommended_commands(status),
        "families": families,
        "best_family": families[0] if families else None,
        "evidence_source": "persisted_context_overlay_reliability",
    }


def format_text_report(report: dict[str, Any]) -> str:
    coverage = report.get("input_coverage") if isinstance(report.get("input_coverage"), dict) else {}
    watch_coverage = coverage.get("context_watch") if isinstance(coverage.get("context_watch"), dict) else {}
    negative_coverage = coverage.get("negative_pressure") if isinstance(coverage.get("negative_pressure"), dict) else {}
    lines = [
        f"status: {report.get('status')}",
        f"headline: {report.get('headline')}",
        f"evaluated_at: {report.get('evaluated_at')}",
        f"empty_reason: {report.get('empty_reason')}",
        (
            "input_coverage: "
            f"context_watch_rows={watch_coverage.get('row_count', 0)} "
            f"context_watch_matured={watch_coverage.get('matured_count', 0)} "
            f"context_watch_benchmark={watch_coverage.get('benchmark_attribution_status')} "
            f"negative_rows={negative_coverage.get('row_count', 0)} "
            f"negative_matured={negative_coverage.get('matured_count', 0)} "
            f"negative_benchmark={negative_coverage.get('benchmark_attribution_status')}"
        ),
        "authority: research_only; no policy, portfolio, or broker behavior changed",
    ]
    commands = report.get("recommended_commands") if isinstance(report.get("recommended_commands"), list) else []
    if commands:
        lines.append("recommended_commands:")
        for item in commands:
            if isinstance(item, dict):
                lines.append(f" - {item.get('command')}: {item.get('purpose')}")
            else:
                lines.append(f" - {item}")
    for row in report.get("families") or []:
        lines.append(
            " - {family}: {classification}, matured={matured}, watch_matured={watch}, negative_matured={negative}".format(
                family=row.get("source_family"),
                classification=row.get("classification"),
                matured=row.get("total_matured_count"),
                watch=row.get("watch_matured_count"),
                negative=row.get("negative_pressure_matured_count"),
            )
        )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize fast context-overlay reliability from watch and de-risk signal outcomes.")
    parser.add_argument("--evaluated-at")
    parser.add_argument("--asof-date", type=parse_datetime_arg)
    parser.add_argument("--horizons", type=int, nargs="*")
    parser.add_argument("--min-matured-rows", type=int, default=DEFAULT_MIN_MATURED_ROWS)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--format", choices=["json", "text"], default="json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = load_reliability_report(
        evaluated_at=args.evaluated_at,
        asof_date=args.asof_date,
        horizons=args.horizons,
        min_matured_rows=int(args.min_matured_rows),
    )
    persisted_rows = 0
    if not args.dry_run:
        persisted_rows = persist_reliability_report(report)
    report["persisted_rows"] = int(persisted_rows)
    report["dry_run"] = bool(args.dry_run)
    if args.format == "text":
        print(format_text_report(report))
    else:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
