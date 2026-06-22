from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from advisory.fallback_telemetry import record_local_fallback_event
from advisory.signal_quality_evaluator import EVALUATIONS_TABLE
from advisory.signal_quality_family_report import VARIANT_TO_SOURCE_FAMILY
from advisory.signal_quality_split_report import (
    build_unavailable_report,
    build_split_report,
    load_split_evidence,
)
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration
from utils.sync import parse_datetime_arg


SPLIT_EVALUATIONS_TABLE = "advisory_signal_quality_split_evaluations"
SPLIT_SUMMARY_TABLE = "advisory_signal_quality_split_eval_summary"
SPLIT_REVIEWS_TABLE = "advisory_signal_quality_split_promotion_reviews"
SPLIT_DECISIONS_TABLE = "advisory_signal_quality_split_promotion_decisions"
SPLIT_EVALUATOR_SCHEMA_MIGRATION_ID = "20260620_advisory_signal_quality_split_evaluator"
SPLIT_REVIEW_SCHEMA_MIGRATION_ID = "20260620_advisory_signal_quality_split_promotion_reviews"
DEFAULT_MIN_MATURED_ROWS = 5
DEFAULT_TOP_N = 10
DEFAULT_MIN_STABLE_WINDOWS = 2
DEFAULT_MIN_STABLE_WINDOW_RATE = 0.5

SPLIT_EVALUATOR_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {SPLIT_EVALUATIONS_TABLE} (
        evaluated_at TIMESTAMPTZ NOT NULL,
        source_evaluated_at TIMESTAMPTZ,
        horizon_days BIGINT NOT NULL,
        variant TEXT NOT NULL,
        source_variant TEXT,
        source_family TEXT NOT NULL,
        split_axis TEXT NOT NULL,
        split_value TEXT NOT NULL,
        context_class TEXT,
        direction TEXT,
        asof_date TIMESTAMPTZ NOT NULL,
        setup_id TEXT,
        symbol TEXT NOT NULL,
        selected BOOLEAN,
        matured BOOLEAN,
        forward_return_after_cost DOUBLE PRECISION,
        hit_after_cost BOOLEAN,
        technical_only_selected BOOLEAN,
        technical_only_forward_return_after_cost DOUBLE PRECISION,
        technical_only_hit_after_cost BOOLEAN,
        raw_context_json TEXT,
        authority TEXT,
        broker_execution_allowed BOOLEAN,
        policy_auto_promotion_allowed BOOLEAN,
        load_ts TIMESTAMPTZ,
        UNIQUE (
            evaluated_at,
            horizon_days,
            source_family,
            split_axis,
            split_value,
            asof_date,
            setup_id,
            symbol,
            variant
        )
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {SPLIT_SUMMARY_TABLE} (
        evaluated_at TIMESTAMPTZ NOT NULL,
        source_evaluated_at TIMESTAMPTZ,
        horizon_days BIGINT NOT NULL,
        variant TEXT NOT NULL,
        source_family TEXT NOT NULL,
        split_axis TEXT NOT NULL,
        split_value TEXT NOT NULL,
        context_class TEXT,
        direction TEXT,
        sample_count BIGINT,
        selected_count BIGINT,
        matured_count BIGINT,
        symbol_count BIGINT,
        avg_forward_return_after_cost DOUBLE PRECISION,
        hit_rate_after_cost DOUBLE PRECISION,
        baseline_selected_count BIGINT,
        baseline_avg_forward_return_after_cost DOUBLE PRECISION,
        lift_vs_technical_only DOUBLE PRECISION,
        classification TEXT,
        recommendation TEXT,
        authority TEXT,
        broker_execution_allowed BOOLEAN,
        policy_auto_promotion_allowed BOOLEAN,
        sample_start TIMESTAMPTZ,
        sample_end TIMESTAMPTZ,
        load_ts TIMESTAMPTZ,
        UNIQUE (
            evaluated_at,
            horizon_days,
            source_family,
            split_axis,
            split_value,
            variant
        )
    )
    """,
]

SPLIT_REVIEW_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {SPLIT_REVIEWS_TABLE} (
        reviewed_at TIMESTAMPTZ NOT NULL,
        source_evaluated_at TIMESTAMPTZ,
        horizon_days BIGINT NOT NULL,
        variant TEXT NOT NULL,
        source_family TEXT NOT NULL,
        split_axis TEXT NOT NULL,
        split_value TEXT NOT NULL,
        context_class TEXT,
        direction TEXT,
        stability_evidence_json TEXT,
        llm_review_json TEXT,
        recommendation TEXT,
        confidence DOUBLE PRECISION,
        patch_json TEXT,
        review_model TEXT,
        review_status TEXT,
        review_error TEXT,
        authority TEXT,
        broker_execution_allowed BOOLEAN,
        policy_auto_promotion_allowed BOOLEAN,
        load_ts TIMESTAMPTZ,
        UNIQUE (reviewed_at, horizon_days, source_family, split_axis, split_value, variant)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {SPLIT_DECISIONS_TABLE} (
        decided_at TIMESTAMPTZ NOT NULL,
        reviewed_at TIMESTAMPTZ NOT NULL,
        horizon_days BIGINT NOT NULL,
        variant TEXT NOT NULL,
        source_family TEXT NOT NULL,
        split_axis TEXT NOT NULL,
        split_value TEXT NOT NULL,
        decision TEXT NOT NULL,
        operator_id TEXT,
        decision_reason TEXT,
        final_patch_json TEXT,
        review_snapshot_json TEXT,
        authority TEXT,
        broker_execution_allowed BOOLEAN,
        policy_auto_promotion_allowed BOOLEAN,
        load_ts TIMESTAMPTZ,
        UNIQUE (reviewed_at, horizon_days, source_family, split_axis, split_value, variant, decided_at)
    )
    """,
]

NUMERIC_COLUMNS = [
    "forward_return_after_cost",
    "technical_only_forward_return_after_cost",
    "avg_forward_return_after_cost",
    "hit_rate_after_cost",
    "baseline_avg_forward_return_after_cost",
    "lift_vs_technical_only",
]
INT_COLUMNS = [
    "horizon_days",
    "sample_count",
    "selected_count",
    "matured_count",
    "symbol_count",
    "baseline_selected_count",
]
BOOL_COLUMNS = [
    "selected",
    "matured",
    "hit_after_cost",
    "technical_only_selected",
    "technical_only_hit_after_cost",
    "broker_execution_allowed",
    "policy_auto_promotion_allowed",
]
TS_COLUMNS = ["evaluated_at", "source_evaluated_at", "asof_date", "sample_start", "sample_end", "load_ts"]


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    return str(value)


def _parse_jsonish(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    text = str(value or "").strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.signal_quality_split_evaluator",
            fallback_type="signal_quality_split_evaluator_json_parse_failed",
            source="signal_quality_split_evaluator_json_fields",
            severity="warn",
            reason="Signal-quality split evaluator could not parse a JSON field and used the default fallback.",
            error=exc,
            metadata={"value_type": type(value).__name__, "value_excerpt": text[:500], "default_type": type(default).__name__},
        )
        return default


def _number(value: Any) -> float | None:
    numeric = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric):
        return None
    return float(numeric)


def _boolish(value: Any) -> bool:
    if value is True:
        return True
    if value is False or value is None:
        return False
    return str(value).strip().lower() in {"true", "t", "1", "yes", "y"}


def _slug(value: Any) -> str:
    text = str(value or "").strip().lower()
    out: list[str] = []
    previous_underscore = False
    for char in text:
        if char.isalnum():
            out.append(char)
            previous_underscore = False
        elif not previous_underscore:
            out.append("_")
            previous_underscore = True
    return "".join(out).strip("_") or "unknown"


def _coerce_bool_series(series: pd.Series) -> pd.Series:
    if pd.api.types.is_bool_dtype(series):
        return series.astype("boolean")
    normalized = series.astype("string").str.strip().str.lower()
    mapped = normalized.map(
        {
            "true": True,
            "t": True,
            "1": True,
            "yes": True,
            "y": True,
            "false": False,
            "f": False,
            "0": False,
            "no": False,
            "n": False,
        }
    )
    return mapped.astype("boolean")


def _normalize_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    for column in NUMERIC_COLUMNS:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce")
    for column in INT_COLUMNS:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce").astype("Int64")
    for column in BOOL_COLUMNS:
        if column in out.columns:
            out[column] = _coerce_bool_series(out[column])
    for column in TS_COLUMNS:
        if column in out.columns:
            out[column] = pd.to_datetime(out[column], utc=True, errors="coerce")
    return out


def _record_split_eval_fallback(
    *,
    fallback_type: str,
    source: str,
    reason: str,
    error: Exception,
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="advisory.signal_quality_split_evaluator",
        fallback_type=fallback_type,
        source=source,
        severity="warn",
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def _is_missing_table_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return exc.__class__.__name__ in {"UndefinedTable", "ProgrammingError"} and (
        "does not exist" in text or "undefinedtable" in text
    )


def ensure_tables() -> None:
    apply_schema_migration(
        migration_id=SPLIT_EVALUATOR_SCHEMA_MIGRATION_ID,
        description="Create research-only signal-quality split evaluator output tables.",
        statements=SPLIT_EVALUATOR_SCHEMA_STATEMENTS,
        metadata={
            "tables": [SPLIT_EVALUATIONS_TABLE, SPLIT_SUMMARY_TABLE],
            "workflow": "signal_quality_split_evaluator",
            "authority": "research_only",
        },
    )
    apply_schema_migration(
        migration_id=SPLIT_REVIEW_SCHEMA_MIGRATION_ID,
        description="Create research-only signal-quality split promotion review and decision tables.",
        statements=SPLIT_REVIEW_SCHEMA_STATEMENTS,
        metadata={
            "tables": [SPLIT_REVIEWS_TABLE, SPLIT_DECISIONS_TABLE],
            "workflow": "signal_quality_split_promotion_reviews",
            "authority": "manual_config_review_only",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
        },
    )


def _variants_for_specs(specs: list[dict[str, Any]]) -> list[str]:
    families = {str(item.get("source_family") or "").strip() for item in specs if item.get("source_family")}
    variants = [variant for variant, family in VARIANT_TO_SOURCE_FAMILY.items() if family in families]
    return sorted(set(variants))


def _load_latest_source_evaluated_at(variants: list[str]) -> pd.Timestamp | None:
    if not variants:
        return None
    latest = sql_to_df(
        f"""
        SELECT MAX(evaluated_at) AS evaluated_at
        FROM {EVALUATIONS_TABLE}
        WHERE variant = ANY(%(variants)s)
        """,
        params={"variants": variants},
        retries=3,
    )
    if latest.empty or pd.isna(latest.iloc[0].get("evaluated_at")):
        return None
    ts = pd.to_datetime(latest.iloc[0]["evaluated_at"], utc=True, errors="coerce")
    return None if pd.isna(ts) else ts


def load_source_rows(
    *,
    specs: list[dict[str, Any]],
    source_evaluated_at: Any | None = None,
    horizons: list[int] | None = None,
) -> pd.DataFrame:
    variants = _variants_for_specs(specs)
    if not variants:
        return pd.DataFrame()
    parsed_source_evaluated_at = pd.to_datetime(source_evaluated_at, utc=True, errors="coerce") if source_evaluated_at is not None else None
    if parsed_source_evaluated_at is not None and pd.isna(parsed_source_evaluated_at):
        raise ValueError(f"Invalid source_evaluated_at: {source_evaluated_at}")
    if parsed_source_evaluated_at is None:
        try:
            parsed_source_evaluated_at = _load_latest_source_evaluated_at(variants)
        except Exception as exc:
            if _is_missing_table_error(exc):
                return pd.DataFrame()
            raise
    if parsed_source_evaluated_at is None:
        return pd.DataFrame()
    params: dict[str, Any] = {"variants": variants, "source_evaluated_at": parsed_source_evaluated_at}
    clauses = ["evaluated_at = %(source_evaluated_at)s", "(variant = ANY(%(variants)s) OR variant = 'technical_only')"]
    if horizons:
        params["horizons"] = [int(item) for item in horizons]
        clauses.append("horizon_days = ANY(%(horizons)s)")
    try:
        return sql_to_df(
            f"""
            SELECT
                evaluated_at,
                horizon_days,
                variant,
                asof_date,
                setup_id,
                symbol,
                selected,
                matured,
                forward_return_after_cost,
                hit_after_cost,
                context_sources_json
            FROM {EVALUATIONS_TABLE}
            WHERE {' AND '.join(clauses)}
            ORDER BY horizon_days, asof_date, setup_id, symbol, variant
            """,
            params=params,
            retries=3,
        )
    except Exception as exc:
        if _is_missing_table_error(exc):
            return pd.DataFrame()
        _record_split_eval_fallback(
            fallback_type="signal_quality_split_evaluator_source_load_failed",
            source=EVALUATIONS_TABLE,
            reason="Signal-quality split evaluator could not load base evaluation rows.",
            error=exc,
            metadata={"source_evaluated_at": str(source_evaluated_at), "horizons": horizons, "variants": variants},
        )
        raise


def _context_matches_split(row: pd.Series, spec: dict[str, Any]) -> bool:
    source_family = str(spec.get("source_family") or "").strip()
    context_class = str(spec.get("context_class") or "").strip()
    direction = str(spec.get("direction") or "").strip().lower()
    split_value = str(spec.get("split_value") or "").strip()
    payload = _parse_jsonish(row.get("context_sources_json"), [])
    if not isinstance(payload, list):
        return False
    for item in payload:
        if not isinstance(item, dict):
            continue
        item_source = str(item.get("source") or item.get("context_source") or "").strip()
        if item_source != source_family:
            continue
        item_class = str(item.get("class") or item.get("context_class") or "unknown").strip() or "unknown"
        item_direction = str(item.get("direction") or item.get("context_direction") or "unknown").strip().lower() or "unknown"
        if context_class and item_class != context_class:
            continue
        if direction and item_direction != direction:
            continue
        if not context_class and not direction and f"{item_class}|{item_direction}" != split_value:
            continue
        return True
    return False


def _variant_for_spec(spec: dict[str, Any]) -> str:
    return "technical_plus_{family}_split_{split}".format(
        family=_slug(spec.get("source_family")),
        split=_slug(spec.get("split_value")),
    )[:180]


def _source_variant_for_family(source_family: str) -> str | None:
    for variant, family in VARIANT_TO_SOURCE_FAMILY.items():
        if family == source_family:
            return variant
    return None


def build_split_evaluation_rows(
    source_rows: pd.DataFrame,
    specs: list[dict[str, Any]],
    *,
    evaluated_at: pd.Timestamp | None = None,
) -> pd.DataFrame:
    if source_rows.empty or not specs:
        return pd.DataFrame()
    evaluated_at = evaluated_at or pd.Timestamp.utcnow()
    frame = source_rows.copy()
    frame["horizon_days"] = pd.to_numeric(frame.get("horizon_days"), errors="coerce")
    frame["asof_date"] = pd.to_datetime(frame.get("asof_date"), utc=True, errors="coerce")
    frame["symbol"] = frame.get("symbol", pd.Series(dtype=str)).astype("string").str.strip().str.upper()
    frame["setup_id"] = frame.get("setup_id", pd.Series(dtype=str)).astype("string")
    key_cols = ["horizon_days", "asof_date", "setup_id", "symbol"]
    technical = frame[frame["variant"].astype(str).eq("technical_only")].copy()
    technical = technical.drop_duplicates(subset=key_cols, keep="last")
    technical_lookup = {
        tuple(row[col] for col in key_cols): row
        for _, row in technical.iterrows()
    }
    rows: list[dict[str, Any]] = []
    for spec in specs:
        source_family = str(spec.get("source_family") or "").strip()
        source_variant = _source_variant_for_family(source_family)
        if not source_family or not source_variant:
            continue
        source_frame = frame[frame["variant"].astype(str).eq(source_variant)].copy()
        if source_frame.empty:
            continue
        variant = _variant_for_spec(spec)
        for _, row in source_frame.iterrows():
            key = tuple(row[col] for col in key_cols)
            tech_row = technical_lookup.get(key)
            split_matches = _context_matches_split(row, spec)
            selected = bool(split_matches and _boolish(row.get("selected")))
            rows.append(
                {
                    "evaluated_at": evaluated_at,
                    "source_evaluated_at": row.get("evaluated_at"),
                    "horizon_days": row.get("horizon_days"),
                    "variant": variant,
                    "source_variant": source_variant,
                    "source_family": source_family,
                    "split_axis": spec.get("split_axis") or "context_class_direction",
                    "split_value": spec.get("split_value"),
                    "context_class": spec.get("context_class"),
                    "direction": spec.get("direction"),
                    "asof_date": row.get("asof_date"),
                    "setup_id": row.get("setup_id"),
                    "symbol": row.get("symbol"),
                    "selected": selected,
                    "matured": bool(_boolish(row.get("matured")) and selected),
                    "forward_return_after_cost": row.get("forward_return_after_cost") if selected else None,
                    "hit_after_cost": row.get("hit_after_cost") if selected else None,
                    "technical_only_selected": None if tech_row is None else _boolish(tech_row.get("selected")),
                    "technical_only_forward_return_after_cost": None if tech_row is None else tech_row.get("forward_return_after_cost"),
                    "technical_only_hit_after_cost": None if tech_row is None else tech_row.get("hit_after_cost"),
                    "raw_context_json": json.dumps(
                        {
                            "split_spec": spec,
                            "source_context_sources": _parse_jsonish(row.get("context_sources_json"), []),
                            "selection_rule": "source_family_variant_selected_and_context_split_matches",
                            "authority": "research_only",
                            "broker_execution_allowed": False,
                            "policy_auto_promotion_allowed": False,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        default=_json_default,
                    ),
                    "authority": "research_only",
                    "broker_execution_allowed": False,
                    "policy_auto_promotion_allowed": False,
                    "load_ts": pd.Timestamp.utcnow(),
                }
            )
    return _normalize_frame(pd.DataFrame(rows))


def classify_summary_row(row: dict[str, Any], *, min_matured_rows: int = DEFAULT_MIN_MATURED_ROWS) -> str:
    matured = int(_number(row.get("matured_count")) or 0)
    baseline_selected = int(_number(row.get("baseline_selected_count")) or 0)
    lift = _number(row.get("lift_vs_technical_only"))
    avg_return = _number(row.get("avg_forward_return_after_cost"))
    if matured < int(min_matured_rows):
        return "needs_more_data"
    if baseline_selected < matured or lift is None:
        return "technical_baseline_unavailable"
    if avg_return is not None and avg_return <= 0:
        return "negative_after_cost"
    if lift <= 0:
        return "no_lift_vs_technical_only"
    return "candidate_split_helpful"


def summarize_split_evaluations(
    evaluations: pd.DataFrame,
    *,
    min_matured_rows: int = DEFAULT_MIN_MATURED_ROWS,
) -> pd.DataFrame:
    if evaluations.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for keys, group in evaluations.groupby(
        ["evaluated_at", "source_evaluated_at", "horizon_days", "variant", "source_family", "split_axis", "split_value", "context_class", "direction"],
        dropna=False,
        sort=True,
    ):
        (
            evaluated_at,
            source_evaluated_at,
            horizon_days,
            variant,
            source_family,
            split_axis,
            split_value,
            context_class,
            direction,
        ) = keys
        selected = group[group["selected"].fillna(False).astype(bool)]
        matured = selected[selected["matured"].fillna(False).astype(bool)]
        returns = pd.to_numeric(matured.get("forward_return_after_cost"), errors="coerce").dropna()
        hits = matured.get("hit_after_cost", pd.Series(dtype=bool)).dropna().astype(bool)
        baseline = group[
            group["selected"].fillna(False).astype(bool)
            & group["matured"].fillna(False).astype(bool)
            & group["technical_only_selected"].fillna(False).astype(bool)
            & group["technical_only_forward_return_after_cost"].notna()
        ]
        baseline_returns = pd.to_numeric(baseline.get("technical_only_forward_return_after_cost"), errors="coerce").dropna()
        avg_return = None if returns.empty else float(returns.mean())
        baseline_avg = None if baseline_returns.empty else float(baseline_returns.mean())
        summary_row = {
            "evaluated_at": evaluated_at,
            "source_evaluated_at": source_evaluated_at,
            "horizon_days": int(horizon_days),
            "variant": str(variant),
            "source_family": str(source_family),
            "split_axis": str(split_axis),
            "split_value": str(split_value),
            "context_class": None if pd.isna(context_class) else str(context_class),
            "direction": None if pd.isna(direction) else str(direction),
            "sample_count": int(len(group)),
            "selected_count": int(len(selected)),
            "matured_count": int(len(matured)),
            "symbol_count": int(matured["symbol"].nunique()) if not matured.empty else 0,
            "avg_forward_return_after_cost": avg_return,
            "hit_rate_after_cost": None if hits.empty else float(hits.mean()),
            "baseline_selected_count": int(len(baseline)),
            "baseline_avg_forward_return_after_cost": baseline_avg,
            "lift_vs_technical_only": None if avg_return is None or baseline_avg is None else float(avg_return - baseline_avg),
            "authority": "research_only",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
            "sample_start": pd.NaT if matured.empty else matured["asof_date"].min(),
            "sample_end": pd.NaT if matured.empty else matured["asof_date"].max(),
            "load_ts": pd.Timestamp.utcnow(),
        }
        summary_row["classification"] = classify_summary_row(summary_row, min_matured_rows=min_matured_rows)
        summary_row["recommendation"] = _recommendation_for_classification(summary_row["classification"])
        rows.append(summary_row)
    return _normalize_frame(pd.DataFrame(rows))


def _recommendation_for_classification(classification: str) -> str:
    if classification == "candidate_split_helpful":
        return "run_rolling_window_validation_before_any_policy_influence"
    if classification in {"negative_after_cost", "no_lift_vs_technical_only"}:
        return "track_as_negative_control_and_block_broad_family_promotion"
    if classification == "technical_baseline_unavailable":
        return "collect_matching_technical_only_baseline_before_split_review"
    return "collect_more_matured_labels"


def persist_outputs(evaluations: pd.DataFrame, summary: pd.DataFrame) -> None:
    ensure_tables()
    evaluations = _normalize_frame(evaluations)
    summary = _normalize_frame(summary)
    if not evaluations.empty:
        upsert_to_db(
            evaluations,
            SPLIT_EVALUATIONS_TABLE,
            unique_keys=[
                "evaluated_at",
                "horizon_days",
                "source_family",
                "split_axis",
                "split_value",
                "asof_date",
                "setup_id",
                "symbol",
                "variant",
            ],
        )
    if not summary.empty:
        upsert_to_db(
            summary,
            SPLIT_SUMMARY_TABLE,
            unique_keys=["evaluated_at", "horizon_days", "source_family", "split_axis", "split_value", "variant"],
        )


def load_split_summary_history(
    *,
    source_family: str | None = None,
    horizons: list[int] | None = None,
    limit: int = 500,
) -> pd.DataFrame:
    clauses: list[str] = []
    params: dict[str, Any] = {"limit": max(1, int(limit))}
    if source_family:
        clauses.append("source_family = %(source_family)s")
        params["source_family"] = str(source_family).strip()
    if horizons:
        clauses.append("horizon_days = ANY(%(horizons)s)")
        params["horizons"] = [int(item) for item in horizons]
    where_sql = "WHERE " + " AND ".join(clauses) if clauses else ""
    try:
        df = sql_to_df(
            f"""
            SELECT
                evaluated_at,
                source_evaluated_at,
                horizon_days,
                variant,
                source_family,
                split_axis,
                split_value,
                context_class,
                direction,
                sample_count,
                selected_count,
                matured_count,
                symbol_count,
                avg_forward_return_after_cost,
                hit_rate_after_cost,
                baseline_selected_count,
                baseline_avg_forward_return_after_cost,
                lift_vs_technical_only,
                classification,
                recommendation,
                sample_start,
                sample_end,
                load_ts
            FROM {SPLIT_SUMMARY_TABLE}
            {where_sql}
            ORDER BY evaluated_at DESC, horizon_days, source_family, split_value
            LIMIT %(limit)s
            """,
            params=params,
            retries=3,
        )
    except Exception as exc:
        if _is_missing_table_error(exc):
            return pd.DataFrame()
        _record_split_eval_fallback(
            fallback_type="signal_quality_split_evaluator_summary_history_load_failed",
            source=SPLIT_SUMMARY_TABLE,
            reason="Signal-quality split evaluator could not load persisted split-summary history.",
            error=exc,
            metadata={"source_family": source_family, "horizons": horizons, "limit": limit},
        )
        raise
    return _normalize_frame(df)


def _is_helpful_classification(value: Any) -> bool:
    return str(value or "").strip() == "candidate_split_helpful"


def _is_harmful_classification(value: Any) -> bool:
    return str(value or "").strip() in {"negative_after_cost", "no_lift_vs_technical_only"}


def _is_baseline_unavailable_classification(value: Any) -> bool:
    return str(value or "").strip() == "technical_baseline_unavailable"


def _split_stability_classification(
    *,
    window_count: int,
    helpful_count: int,
    harmful_count: int,
    baseline_unavailable_count: int,
    min_stable_windows: int,
    min_stable_window_rate: float,
) -> str:
    if window_count < int(min_stable_windows):
        return "needs_more_data"
    if baseline_unavailable_count > 0:
        return "needs_more_data"
    helpful_rate = 0.0 if window_count <= 0 else float(helpful_count / window_count)
    if helpful_count > 0 and harmful_count > 0:
        return "unstable_or_horizon_sensitive"
    if helpful_count >= int(min_stable_windows) and helpful_rate >= float(min_stable_window_rate):
        return "stable_candidate"
    if harmful_count >= int(min_stable_windows) and helpful_count == 0:
        return "harmful_negative_control"
    return "needs_more_data"


def _stability_recommendation(classification: str) -> str:
    if classification == "stable_candidate":
        return "eligible_for_manual_reviewed_overlay_rule_after_false_discovery_checks"
    if classification == "unstable_or_horizon_sensitive":
        return "keep_split_watch_only_and_split_further_before_policy_influence"
    if classification == "harmful_negative_control":
        return "use_as_negative_control_to_block_broad_family_promotion"
    return "collect_more_matured_split_windows"


def build_split_stability_report(
    summary: pd.DataFrame,
    *,
    min_stable_windows: int = DEFAULT_MIN_STABLE_WINDOWS,
    min_stable_window_rate: float = DEFAULT_MIN_STABLE_WINDOW_RATE,
) -> dict[str, Any]:
    if summary.empty:
        return {
            "status": "no_data",
            "authority": "research_only",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
            "headline": "No persisted narrowed split summaries are available for stability classification.",
            "split_count": 0,
            "stable_candidate_count": 0,
            "harmful_negative_control_count": 0,
            "unstable_or_horizon_sensitive_count": 0,
            "splits": [],
            "next_actions": [
                "Run advisory.signal_quality_split_evaluator after signal-quality split queues have enough matured evidence.",
                "Keep context overlays watch-only until narrowed split stability is proven across windows.",
            ],
        }
    frame = _normalize_frame(summary)
    frame["classification"] = frame.get("classification", pd.Series(dtype=str)).astype("string").str.strip()
    rows: list[dict[str, Any]] = []
    group_cols = ["source_family", "horizon_days", "split_axis", "split_value", "context_class", "direction", "variant"]
    for keys, group in frame.groupby(group_cols, dropna=False, sort=True):
        source_family, horizon_days, split_axis, split_value, context_class, direction, variant = keys
        windows = pd.to_datetime(group.get("evaluated_at"), utc=True, errors="coerce").dropna().nunique()
        helpful = int(group["classification"].map(_is_helpful_classification).sum())
        harmful = int(group["classification"].map(_is_harmful_classification).sum())
        baseline_unavailable = int(group["classification"].map(_is_baseline_unavailable_classification).sum())
        window_count = int(windows)
        classification = _split_stability_classification(
            window_count=window_count,
            helpful_count=helpful,
            harmful_count=harmful,
            baseline_unavailable_count=baseline_unavailable,
            min_stable_windows=int(min_stable_windows),
            min_stable_window_rate=float(min_stable_window_rate),
        )
        avg_lift = pd.to_numeric(group.get("lift_vs_technical_only"), errors="coerce").dropna()
        avg_return = pd.to_numeric(group.get("avg_forward_return_after_cost"), errors="coerce").dropna()
        rows.append(
            {
                "source_family": str(source_family),
                "horizon_days": int(horizon_days),
                "split_axis": str(split_axis),
                "split_value": str(split_value),
                "context_class": None if pd.isna(context_class) else str(context_class),
                "direction": None if pd.isna(direction) else str(direction),
                "variant": str(variant),
                "window_count": window_count,
                "helpful_window_count": helpful,
                "harmful_window_count": harmful,
                "baseline_unavailable_window_count": baseline_unavailable,
                "helpful_window_rate": None if window_count == 0 else float(helpful / window_count),
                "avg_lift_vs_technical_only": None if avg_lift.empty else float(avg_lift.mean()),
                "avg_forward_return_after_cost": None if avg_return.empty else float(avg_return.mean()),
                "total_matured_count": int(pd.to_numeric(group.get("matured_count"), errors="coerce").fillna(0).sum()),
                "max_symbol_count": int(pd.to_numeric(group.get("symbol_count"), errors="coerce").fillna(0).max()),
                "classification": classification,
                "recommendation": _stability_recommendation(classification),
                "authority": "research_only",
                "broker_execution_allowed": False,
                "policy_auto_promotion_allowed": False,
            }
        )
    rows.sort(
        key=lambda item: (
            item["classification"] == "stable_candidate",
            item["classification"] == "harmful_negative_control",
            item.get("avg_lift_vs_technical_only") if item.get("avg_lift_vs_technical_only") is not None else -999.0,
            item.get("window_count") or 0,
        ),
        reverse=True,
    )
    stable = [row for row in rows if row["classification"] == "stable_candidate"]
    harmful = [row for row in rows if row["classification"] == "harmful_negative_control"]
    unstable = [row for row in rows if row["classification"] == "unstable_or_horizon_sensitive"]
    return {
        "status": "ok",
        "authority": "research_only",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        "headline": (
            "At least one narrowed split is stable across persisted windows."
            if stable
            else "No narrowed split has stable helpful evidence across persisted windows yet."
        ),
        "min_stable_windows": int(min_stable_windows),
        "min_stable_window_rate": float(min_stable_window_rate),
        "split_count": len(rows),
        "stable_candidate_count": len(stable),
        "harmful_negative_control_count": len(harmful),
        "unstable_or_horizon_sensitive_count": len(unstable),
        "splits": rows,
        "next_actions": [
            "Stable candidates can only become reviewed overlay-rule proposals after false-discovery and leakage checks.",
            "Harmful negative controls should suppress broad source-family promotion attempts.",
            "Unstable or low-data splits remain watch/context evidence only.",
        ],
    }


def build_split_review_patch(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation": "signal_quality_narrowed_split_overlay_rule",
        "mode": "manual_apply_required",
        "status": "disabled_review_candidate",
        "authority": "review_input_only",
        "source_family": row.get("source_family"),
        "horizon_days": row.get("horizon_days"),
        "split_axis": row.get("split_axis"),
        "split_value": row.get("split_value"),
        "context_class": row.get("context_class"),
        "direction": row.get("direction"),
        "variant": row.get("variant"),
        "classification": row.get("classification"),
        "minimum_lift_vs_technical_only": row.get("avg_lift_vs_technical_only"),
        "minimum_windows": row.get("window_count"),
        "minimum_matured_rows": row.get("total_matured_count"),
        "supported_effects": [
            "watchlist_priority_boost",
            "reduce_exposure_review",
            "manual_review_question",
        ],
        "forbidden_effects": [
            "create_buy_authority",
            "bypass_technical_confirmation",
            "bypass_liquidity_or_risk_checks",
            "submit_broker_order",
            "change_portfolio_state",
        ],
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        "notes": [
            "This proposal can only become a reviewed context-overlay rule after manual config review.",
            "Even if manually applied, it must remain bounded to watch/review pressure unless a later validated policy explicitly permits more.",
        ],
    }


def build_split_promotion_review(row: dict[str, Any], *, reviewed_at: pd.Timestamp | None = None) -> dict[str, Any]:
    reviewed_at = reviewed_at or pd.Timestamp.utcnow()
    patch = build_split_review_patch(row)
    llm_review = {
        "recommendation": "promote_narrowed_split_review",
        "confidence": 0.72,
        "summary": (
            f"Stable narrowed split {row.get('source_family')} / {row.get('split_value')} "
            f"has repeated positive evidence versus technical-only and is ready for manual rule review."
        ),
        "reasons": [
            f"stable_window_count={row.get('window_count')}",
            f"helpful_window_count={row.get('helpful_window_count')}",
            f"avg_lift_vs_technical_only={row.get('avg_lift_vs_technical_only')}",
            f"total_matured_count={row.get('total_matured_count')}",
        ],
        "promotion_risks": [
            "False discovery risk remains if this split is concentrated in a small sector/date cluster.",
            "The proposal must not create direct BUY authority or broker execution.",
            "A later config review must verify point-in-time evidence and leakage controls before runtime use.",
        ],
        "suggested_manual_checks": [
            "Check whether sample symbols are concentrated in one sector or event cluster.",
            "Compare against broad source-family and technical-only results over the same window.",
            "Confirm the proposed effect is watch/review-only, not direct action authority.",
        ],
        "proposed_patch": patch,
    }
    return {
        "status": "ok",
        "reviewed_at": reviewed_at,
        "source_evaluated_at": row.get("source_evaluated_at"),
        "horizon_days": int(row.get("horizon_days") or 0),
        "variant": row.get("variant"),
        "source_family": row.get("source_family"),
        "split_axis": row.get("split_axis"),
        "split_value": row.get("split_value"),
        "context_class": row.get("context_class"),
        "direction": row.get("direction"),
        "stability_evidence": row,
        "pending_patch": patch,
        "llm_review": llm_review,
        "review_model": "deterministic_signal_quality_split_v1",
        "review_status": "ok",
        "review_error": None,
        "authority": "manual_config_review_only",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
    }


def persist_split_promotion_review(result: dict[str, Any]) -> None:
    ensure_tables()
    row = pd.DataFrame(
        [
            {
                "reviewed_at": result["reviewed_at"],
                "source_evaluated_at": result.get("source_evaluated_at"),
                "horizon_days": result["horizon_days"],
                "variant": result["variant"],
                "source_family": result["source_family"],
                "split_axis": result["split_axis"],
                "split_value": result["split_value"],
                "context_class": result.get("context_class"),
                "direction": result.get("direction"),
                "stability_evidence_json": json.dumps(result["stability_evidence"], ensure_ascii=False, sort_keys=True, default=_json_default),
                "llm_review_json": json.dumps(result["llm_review"], ensure_ascii=False, sort_keys=True, default=_json_default),
                "recommendation": result["llm_review"].get("recommendation"),
                "confidence": result["llm_review"].get("confidence"),
                "patch_json": json.dumps(result["pending_patch"], ensure_ascii=False, sort_keys=True, default=_json_default),
                "review_model": result["review_model"],
                "review_status": result["review_status"],
                "review_error": result["review_error"],
                "authority": result["authority"],
                "broker_execution_allowed": False,
                "policy_auto_promotion_allowed": False,
                "load_ts": pd.Timestamp.utcnow(),
            }
        ]
    )
    upsert_to_db(
        row,
        SPLIT_REVIEWS_TABLE,
        unique_keys=["reviewed_at", "horizon_days", "source_family", "split_axis", "split_value", "variant"],
    )


def generate_stability_promotion_reviews(
    *,
    stability_report: dict[str, Any] | None = None,
    source_family: str | None = None,
    horizons: list[int] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    if stability_report is None:
        history = load_split_summary_history(source_family=source_family, horizons=horizons)
        stability_report = build_split_stability_report(history)
    reviews: list[dict[str, Any]] = []
    reviewed_at = pd.Timestamp.utcnow()
    for row in stability_report.get("splits") or []:
        if not isinstance(row, dict) or row.get("classification") != "stable_candidate":
            continue
        review = build_split_promotion_review(row, reviewed_at=reviewed_at)
        if persist:
            persist_split_promotion_review(review)
        reviews.append(review)
    return {
        "status": "ok",
        "mode": "stable_split_candidates",
        "persisted": bool(persist),
        "authority": "manual_config_review_only",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        "review_count": len(reviews),
        "reviews": reviews,
        "stability_report_status": stability_report.get("status"),
        "note": "Generated review rows only for stable narrowed split candidates. No config, action, portfolio, or broker behavior was changed.",
    }


def load_split_promotion_reviews(limit: int = 25) -> list[dict[str, Any]]:
    ensure_tables()
    df = sql_to_df(
        f"""
        WITH latest_decision AS (
            SELECT DISTINCT ON (reviewed_at, horizon_days, source_family, split_axis, split_value, variant)
                reviewed_at,
                horizon_days,
                source_family,
                split_axis,
                split_value,
                variant,
                decision,
                operator_id,
                decision_reason,
                final_patch_json,
                decided_at
            FROM {SPLIT_DECISIONS_TABLE}
            ORDER BY reviewed_at, horizon_days, source_family, split_axis, split_value, variant, decided_at DESC
        )
        SELECT
            r.*,
            d.decision AS manual_decision,
            d.operator_id AS manual_operator_id,
            d.decision_reason AS manual_decision_reason,
            d.final_patch_json AS final_patch_json,
            d.decided_at AS manual_decided_at
        FROM {SPLIT_REVIEWS_TABLE} r
        LEFT JOIN latest_decision d
          ON d.reviewed_at = r.reviewed_at
         AND d.horizon_days = r.horizon_days
         AND d.source_family = r.source_family
         AND d.split_axis = r.split_axis
         AND d.split_value = r.split_value
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
    for col in ["stability_evidence_json", "llm_review_json", "patch_json", "final_patch_json"]:
        if col in out.columns:
            out[col.replace("_json", "")] = out[col].map(lambda value: _parse_jsonish(value, {}))
    out = out.astype(object).where(pd.notna(out), None)
    return out.to_dict(orient="records")


def find_split_promotion_review(
    *,
    reviewed_at: Any,
    horizon_days: int,
    source_family: str,
    split_axis: str,
    split_value: str,
    variant: str,
) -> dict[str, Any]:
    ensure_tables()
    parsed_reviewed_at = pd.to_datetime(reviewed_at, utc=True, errors="coerce")
    if pd.isna(parsed_reviewed_at):
        raise ValueError(f"Invalid reviewed_at: {reviewed_at}")
    df = sql_to_df(
        f"""
        SELECT *
        FROM {SPLIT_REVIEWS_TABLE}
        WHERE reviewed_at = %(reviewed_at)s
          AND horizon_days = %(horizon_days)s
          AND source_family = %(source_family)s
          AND split_axis = %(split_axis)s
          AND split_value = %(split_value)s
          AND variant = %(variant)s
        LIMIT 1
        """,
        params={
            "reviewed_at": parsed_reviewed_at,
            "horizon_days": int(horizon_days),
            "source_family": str(source_family),
            "split_axis": str(split_axis),
            "split_value": str(split_value),
            "variant": str(variant),
        },
        retries=3,
    )
    if df.empty:
        raise ValueError(f"Unknown split promotion review: {reviewed_at}/{horizon_days}/{source_family}/{split_axis}/{split_value}/{variant}")
    row = df.iloc[0].to_dict()
    row["stability_evidence"] = _parse_jsonish(row.get("stability_evidence_json"), {})
    row["llm_review"] = _parse_jsonish(row.get("llm_review_json"), {})
    row["patch"] = _parse_jsonish(row.get("patch_json"), {})
    return row


def record_split_manual_decision(
    *,
    reviewed_at: Any,
    horizon_days: int,
    source_family: str,
    split_axis: str,
    split_value: str,
    variant: str,
    decision: str,
    operator_id: str | None = None,
    decision_reason: str | None = None,
) -> dict[str, Any]:
    normalized_decision = str(decision or "").strip().lower()
    if normalized_decision not in {"approved", "rejected", "needs_more_data"}:
        raise ValueError("decision must be approved, rejected, or needs_more_data")
    review = find_split_promotion_review(
        reviewed_at=reviewed_at,
        horizon_days=horizon_days,
        source_family=source_family,
        split_axis=split_axis,
        split_value=split_value,
        variant=variant,
    )
    decided_at = pd.Timestamp.utcnow()
    final_patch = dict(review.get("patch") or {})
    final_patch["mode"] = "manual_apply_required"
    final_patch["manual_decision"] = normalized_decision
    result = {
        "status": "ok",
        "decided_at": decided_at,
        "reviewed_at": review["reviewed_at"],
        "horizon_days": int(review["horizon_days"]),
        "variant": review["variant"],
        "source_family": review["source_family"],
        "split_axis": review["split_axis"],
        "split_value": review["split_value"],
        "decision": normalized_decision,
        "operator_id": operator_id,
        "decision_reason": decision_reason,
        "final_patch": final_patch,
        "review": {
            "recommendation": review.get("recommendation"),
            "confidence": review.get("confidence"),
            "llm_review": review.get("llm_review") or {},
            "stability_evidence": review.get("stability_evidence") or {},
        },
        "applied": False,
        "note": "Decision recorded only. No config file, action rule, portfolio row, or broker behavior was changed.",
        "authority": "manual_config_review_only",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
    }
    row = pd.DataFrame(
        [
            {
                "decided_at": decided_at,
                "reviewed_at": review["reviewed_at"],
                "horizon_days": int(review["horizon_days"]),
                "variant": review["variant"],
                "source_family": review["source_family"],
                "split_axis": review["split_axis"],
                "split_value": review["split_value"],
                "decision": normalized_decision,
                "operator_id": operator_id,
                "decision_reason": decision_reason,
                "final_patch_json": json.dumps(final_patch, ensure_ascii=False, sort_keys=True, default=_json_default),
                "review_snapshot_json": json.dumps(result["review"], ensure_ascii=False, sort_keys=True, default=_json_default),
                "authority": "manual_config_review_only",
                "broker_execution_allowed": False,
                "policy_auto_promotion_allowed": False,
                "load_ts": pd.Timestamp.utcnow(),
            }
        ]
    )
    ensure_tables()
    upsert_to_db(
        row,
        SPLIT_DECISIONS_TABLE,
        unique_keys=["reviewed_at", "horizon_days", "source_family", "split_axis", "split_value", "variant", "decided_at"],
    )
    return result


def specs_from_split_queue(queue: dict[str, Any], *, include_negative_controls: bool = True, top_n: int = DEFAULT_TOP_N) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for bucket in ["build_narrower_evaluator_variant", "track_as_negative_control" if include_negative_controls else ""]:
        if not bucket:
            continue
        for item in queue.get(bucket) or []:
            if not isinstance(item, dict):
                continue
            specs.append(item)
    return specs[: max(0, int(top_n))]


def load_specs_from_latest_split_report(
    *,
    source_family: str | None = None,
    horizons: list[int] | None = None,
    min_matured_rows: int = DEFAULT_MIN_MATURED_ROWS,
    top_n: int = DEFAULT_TOP_N,
    include_negative_controls: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        rows = load_split_evidence(horizons=horizons, source_family=source_family)
        report = build_split_report(rows, min_matured_rows=min_matured_rows, top_n=top_n)
    except Exception as exc:
        if not _is_missing_table_error(exc):
            raise
        report = build_unavailable_report(exc)
    queue = report.get("research_queue") if isinstance(report.get("research_queue"), dict) else {}
    specs = specs_from_split_queue(queue, include_negative_controls=include_negative_controls, top_n=top_n)
    return specs, report


def evaluate_split_quality(
    *,
    specs: list[dict[str, Any]] | None = None,
    source_family: str | None = None,
    source_evaluated_at: Any | None = None,
    horizons: list[int] | None = None,
    min_matured_rows: int = DEFAULT_MIN_MATURED_ROWS,
    top_n: int = DEFAULT_TOP_N,
    include_negative_controls: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    evaluated_at = pd.Timestamp.utcnow()
    split_report: dict[str, Any] | None = None
    if specs is None:
        specs, split_report = load_specs_from_latest_split_report(
            source_family=source_family,
            horizons=horizons,
            min_matured_rows=min_matured_rows,
            top_n=top_n,
            include_negative_controls=include_negative_controls,
        )
    source_rows = load_source_rows(specs=specs, source_evaluated_at=source_evaluated_at, horizons=horizons)
    evaluations = build_split_evaluation_rows(source_rows, specs, evaluated_at=evaluated_at)
    summary = summarize_split_evaluations(evaluations, min_matured_rows=min_matured_rows)
    return evaluations, summary, {
        "status": "ok",
        "authority": "research_only",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        "spec_count": len(specs or []),
        "evaluation_rows": int(len(evaluations)),
        "summary_rows": int(len(summary)),
        "source_evaluated_at": None if source_rows.empty else pd.to_datetime(source_rows["evaluated_at"], utc=True, errors="coerce").max(),
        "split_report_status": None if split_report is None else split_report.get("status"),
        "split_report_headline": None if split_report is None else split_report.get("headline"),
        "next_actions": [
            "Use candidate_split_helpful summaries only as research hypotheses.",
            "Run rolling-window validation before any source-family split can influence action policy.",
            "Use negative/no-lift summaries to suppress broad source-family promotion attempts.",
        ],
    }


def _load_specs_file(path: str | None) -> list[dict[str, Any]] | None:
    if not path:
        return None
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, dict) and isinstance(payload.get("research_queue"), dict):
        return specs_from_split_queue(payload["research_queue"])
    if isinstance(payload, dict) and isinstance(payload.get("specs"), list):
        return [item for item in payload["specs"] if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    raise ValueError("spec file must contain a list, {'specs': [...]}, or {'research_queue': {...}}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate research-only source-family split candidates against technical-only evidence.")
    parser.add_argument("--source-family")
    parser.add_argument("--source-evaluated-at", type=parse_datetime_arg)
    parser.add_argument("--horizons", nargs="*", type=int)
    parser.add_argument("--min-matured-rows", type=int, default=DEFAULT_MIN_MATURED_ROWS)
    parser.add_argument("--min-stable-windows", type=int, default=DEFAULT_MIN_STABLE_WINDOWS)
    parser.add_argument("--min-stable-window-rate", type=float, default=DEFAULT_MIN_STABLE_WINDOW_RATE)
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    parser.add_argument("--specs-file")
    parser.add_argument("--exclude-negative-controls", action="store_true")
    parser.add_argument(
        "--stability-report",
        action="store_true",
        help="Read persisted narrowed split summaries and classify stability without running a new split evaluation.",
    )
    parser.add_argument(
        "--generate-stability-reviews",
        action="store_true",
        help="Generate manual review rows for stable narrowed split candidates. Dry-run prints rows without persisting.",
    )
    parser.add_argument(
        "--ensure-schema-only",
        action="store_true",
        help="Apply split evaluator/review schema migrations and exit without loading data or writing evaluation/review rows.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--format", choices=["json", "text"], default="json")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.ensure_schema_only:
        ensure_tables()
        payload = {
            "status": "ok",
            "mode": "ensure_schema_only",
            "evaluations_table": SPLIT_EVALUATIONS_TABLE,
            "summary_table": SPLIT_SUMMARY_TABLE,
            "reviews_table": SPLIT_REVIEWS_TABLE,
            "decisions_table": SPLIT_DECISIONS_TABLE,
            "authority": "research_only_schema_maintenance",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
        }
        if args.format == "text":
            print(
                "status={status} mode={mode} evaluations_table={evaluations_table} "
                "summary_table={summary_table} authority={authority}".format(**payload)
            )
        else:
            print(json.dumps(payload, indent=2, default=_json_default))
        return 0
    if args.stability_report:
        history = load_split_summary_history(source_family=args.source_family, horizons=args.horizons)
        stability_report = build_split_stability_report(
            history,
            min_stable_windows=max(1, int(args.min_stable_windows)),
            min_stable_window_rate=float(args.min_stable_window_rate),
        )
        review_result = None
        if args.generate_stability_reviews:
            review_result = generate_stability_promotion_reviews(
                stability_report=stability_report,
                persist=not bool(args.dry_run),
            )
        if args.format == "text":
            print(f"status: {stability_report['status']}")
            print(f"headline: {stability_report.get('headline')}")
            print("authority: research_only; no policy or broker behavior changed")
            if review_result is not None:
                print(f"reviews: generated={review_result.get('review_count')} persisted={review_result.get('persisted')}")
            for row in stability_report.get("splits") or []:
                print(
                    " - {family} h{horizon} {split}: {classification}, windows={windows}, lift={lift}".format(
                        family=row.get("source_family"),
                        horizon=row.get("horizon_days"),
                        split=row.get("split_value"),
                        classification=row.get("classification"),
                        windows=row.get("window_count"),
                        lift=row.get("avg_lift_vs_technical_only"),
                    )
                )
        else:
            payload = dict(stability_report)
            if review_result is not None:
                payload["promotion_reviews"] = review_result
            print(json.dumps(payload, indent=2, default=_json_default))
        return 0

    specs = _load_specs_file(args.specs_file)
    evaluations, summary, meta = evaluate_split_quality(
        specs=specs,
        source_family=args.source_family,
        source_evaluated_at=args.source_evaluated_at,
        horizons=args.horizons,
        min_matured_rows=max(1, int(args.min_matured_rows)),
        top_n=max(1, int(args.top_n)),
        include_negative_controls=not bool(args.exclude_negative_controls),
    )
    if not args.dry_run:
        persist_outputs(evaluations, summary)
    history = load_split_summary_history(source_family=args.source_family, horizons=args.horizons)
    stability_input = pd.concat([history, summary], ignore_index=True, sort=False) if not summary.empty else history
    stability_report = build_split_stability_report(
        stability_input,
        min_stable_windows=max(1, int(args.min_stable_windows)),
        min_stable_window_rate=float(args.min_stable_window_rate),
    )
    review_result = None
    if args.generate_stability_reviews:
        review_result = generate_stability_promotion_reviews(
            stability_report=stability_report,
            persist=not bool(args.dry_run),
        )
    payload = {
        "status": "ok",
        "evaluations_table": SPLIT_EVALUATIONS_TABLE,
        "summary_table": SPLIT_SUMMARY_TABLE,
        "meta": meta,
        "stability_report": stability_report,
        "promotion_reviews": review_result,
        "summary_sample": summary.head(20).to_dict(orient="records") if not summary.empty else [],
        "dry_run": bool(args.dry_run),
    }
    if args.format == "text":
        print(f"status: {payload['status']}")
        print(f"authority: research_only; no policy or broker behavior changed")
        print(f"specs={meta['spec_count']} evaluations={meta['evaluation_rows']} summaries={meta['summary_rows']}")
        print(
            "stability: status={status} stable={stable} harmful={harmful} unstable={unstable}".format(
                status=stability_report.get("status"),
                stable=stability_report.get("stable_candidate_count"),
                harmful=stability_report.get("harmful_negative_control_count"),
                unstable=stability_report.get("unstable_or_horizon_sensitive_count"),
            )
        )
        if review_result is not None:
            print(f"reviews: generated={review_result.get('review_count')} persisted={review_result.get('persisted')}")
        for row in payload["summary_sample"]:
            print(
                " - {family} h{horizon} {split}: {classification}, lift={lift}, matured={matured}".format(
                    family=row.get("source_family"),
                    horizon=row.get("horizon_days"),
                    split=row.get("split_value"),
                    classification=row.get("classification"),
                    lift=row.get("lift_vs_technical_only"),
                    matured=row.get("matured_count"),
                )
            )
    else:
        print(json.dumps(payload, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
