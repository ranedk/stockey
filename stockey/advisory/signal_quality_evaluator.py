from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from advisory.company_memory_review import TABLE_NAME as COMPANY_MEMORY_TABLE
from advisory.event_evidence_store import ANNOUNCEMENT_CONTEXT_OVERLAYS_TABLE, BHAVCOPY_CONTEXT_OVERLAYS_TABLE, BHAVCOPY_EVIDENCE_TABLE
from advisory.exchange_context_overlays import EXCHANGE_CONTEXT_OVERLAYS_TABLE
from advisory.fallback_telemetry import record_local_fallback_event
from advisory.event_policy import TABLE_NAME as EVENT_POLICY_TABLE
from advisory.macro_context_overlays import MACRO_CONTEXT_OVERLAYS_TABLE
from advisory.market_context import UNIVERSE_TABLE as MARKET_CONTEXT_UNIVERSE_TABLE
from advisory.news_theme_engine import THEME_CONTEXT_OVERLAYS_TABLE
from advisory.return_attribution import attach_benchmark_forward_returns, load_benchmark_history_for_attribution
from advisory.technical_threshold_calibration import (
    attach_forward_returns,
    load_price_history_for_returns,
    load_technical_signal_rows,
)
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration
from utils.sync import parse_datetime_arg


EVALUATIONS_TABLE = "advisory_signal_quality_evaluations"
SUMMARY_TABLE = "advisory_signal_quality_eval_summary"
ACTION_RECOMMENDATIONS_TABLE = "advisory_action_recommendations"
SIGNAL_QUALITY_SCHEMA_MIGRATION_ID = "20260611_advisory_signal_quality_evaluator_base"
SIGNAL_QUALITY_CONTEXT_SCHEMA_MIGRATION_ID = "20260620_advisory_signal_quality_context_overlays"
SIGNAL_QUALITY_TRUSTED_ADJUSTMENT_SCHEMA_MIGRATION_ID = "20260620_advisory_signal_quality_trusted_context_adjustments"
SIGNAL_QUALITY_BENCHMARK_SCHEMA_MIGRATION_ID = "20260622_advisory_signal_quality_evaluator_benchmark_attribution"
DEFAULT_HORIZONS = [5, 10, 20]
DEFAULT_RETURN_THRESHOLD = 0.03
DEFAULT_COST_BPS = 25.0
DEFAULT_MIN_MATURED_ROWS = 10
DEFAULT_TECHNICAL_MIN = 70.0
DEFAULT_NEAR_TECHNICAL_MIN = 65.0
SIGNAL_QUALITY_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {EVALUATIONS_TABLE} (
        evaluated_at TIMESTAMPTZ NOT NULL,
        horizon_days BIGINT NOT NULL,
        asof_date TIMESTAMPTZ NOT NULL,
        setup_id TEXT,
        symbol TEXT NOT NULL,
        variant TEXT NOT NULL,
        technical_pass BOOLEAN,
        near_technical_pass BOOLEAN,
        event_positive BOOLEAN,
        event_negative BOOLEAN,
        bhavcopy_positive BOOLEAN,
        bhavcopy_negative BOOLEAN,
        company_memory_positive BOOLEAN,
        company_memory_negative BOOLEAN,
        selected BOOLEAN,
        technical_total_score DOUBLE PRECISION,
        technical_state TEXT,
        candidate_state TEXT,
        technical_trigger_type TEXT,
        event_action_type TEXT,
        event_policy_class TEXT,
        event_policy_score DOUBLE PRECISION,
        event_confidence DOUBLE PRECISION,
        bhavcopy_deal_pressure TEXT,
        bhavcopy_evidence_score DOUBLE PRECISION,
        bhavcopy_deal_net_value_inr DOUBLE PRECISION,
        company_memory_signal TEXT,
        company_memory_confidence DOUBLE PRECISION,
        company_memory_conviction_score DOUBLE PRECISION,
        entry_date TIMESTAMPTZ,
        exit_date TIMESTAMPTZ,
        entry_close DOUBLE PRECISION,
        exit_close DOUBLE PRECISION,
        forward_return DOUBLE PRECISION,
        forward_return_after_cost DOUBLE PRECISION,
        hit_after_cost BOOLEAN,
        matured BOOLEAN,
        raw_context_json TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (evaluated_at, horizon_days, asof_date, setup_id, symbol, variant)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {SUMMARY_TABLE} (
        evaluated_at TIMESTAMPTZ NOT NULL,
        horizon_days BIGINT NOT NULL,
        variant TEXT NOT NULL,
        sample_count BIGINT,
        selected_count BIGINT,
        matured_count BIGINT,
        selection_rate DOUBLE PRECISION,
        avg_forward_return DOUBLE PRECISION,
        median_forward_return DOUBLE PRECISION,
        avg_forward_return_after_cost DOUBLE PRECISION,
        hit_rate_after_cost DOUBLE PRECISION,
        positive_return_rate DOUBLE PRECISION,
        baseline_avg_forward_return_after_cost DOUBLE PRECISION,
        lift_vs_technical_only DOUBLE PRECISION,
        avg_selected_technical_score DOUBLE PRECISION,
        sample_start TIMESTAMPTZ,
        sample_end TIMESTAMPTZ,
        recommendation TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (evaluated_at, horizon_days, variant)
    )
    """,
]
SIGNAL_QUALITY_CONTEXT_SCHEMA_STATEMENTS = [
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS context_positive BOOLEAN",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS context_negative BOOLEAN",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS context_watch BOOLEAN",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS context_overlay_count BIGINT",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS context_pressure_score DOUBLE PRECISION",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS context_sources_json TEXT",
]
SIGNAL_QUALITY_TRUSTED_ADJUSTMENT_SCHEMA_STATEMENTS = [
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS trusted_context_adjustment TEXT",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS trusted_context_original_action TEXT",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS trusted_context_adjusted_action TEXT",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS trusted_context_matched_rules_json TEXT",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS trusted_context_negative_block BOOLEAN",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS trusted_context_positive_boost BOOLEAN",
]
SIGNAL_QUALITY_BENCHMARK_SCHEMA_STATEMENTS = [
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS benchmark_name TEXT",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS benchmark_entry_date TIMESTAMPTZ",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS benchmark_exit_date TIMESTAMPTZ",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS benchmark_entry_close DOUBLE PRECISION",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS benchmark_exit_close DOUBLE PRECISION",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS benchmark_forward_return DOUBLE PRECISION",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS excess_forward_return_after_cost DOUBLE PRECISION",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS excess_hit_after_cost BOOLEAN",
    f"ALTER TABLE {SUMMARY_TABLE} ADD COLUMN IF NOT EXISTS avg_benchmark_forward_return DOUBLE PRECISION",
    f"ALTER TABLE {SUMMARY_TABLE} ADD COLUMN IF NOT EXISTS avg_excess_forward_return_after_cost DOUBLE PRECISION",
    f"ALTER TABLE {SUMMARY_TABLE} ADD COLUMN IF NOT EXISTS excess_hit_rate_after_cost DOUBLE PRECISION",
    f"ALTER TABLE {SUMMARY_TABLE} ADD COLUMN IF NOT EXISTS positive_excess_return_rate DOUBLE PRECISION",
]

VARIANTS = [
    "technical_only",
    "technical_plus_event",
    "technical_plus_bhavcopy",
    "technical_plus_company_memory",
    "technical_plus_all",
    "technical_plus_context_overlay",
    "technical_plus_all_context",
    "technical_plus_announcement_context",
    "technical_plus_exchange_context",
    "technical_plus_bhavcopy_context",
    "technical_plus_theme_context",
    "technical_plus_macro_context",
    "technical_after_trusted_context_rules",
]
CONTEXT_SOURCE_FAMILY_VARIANTS = {
    "technical_plus_announcement_context": "announcement_context",
    "technical_plus_exchange_context": "exchange_context",
    "technical_plus_bhavcopy_context": "bhavcopy_context",
    "technical_plus_theme_context": "theme_context",
    "technical_plus_macro_context": "macro_context",
}

EVALUATION_NUMERIC_COLUMNS = [
    "technical_total_score",
    "event_policy_score",
    "event_confidence",
    "bhavcopy_evidence_score",
    "bhavcopy_deal_net_value_inr",
    "company_memory_confidence",
    "company_memory_conviction_score",
    "context_pressure_score",
    "entry_close",
    "exit_close",
    "benchmark_entry_close",
    "benchmark_exit_close",
    "benchmark_forward_return",
    "forward_return",
    "forward_return_after_cost",
    "excess_forward_return_after_cost",
]
EVALUATION_INT_COLUMNS = ["horizon_days", "context_overlay_count"]
EVALUATION_BOOL_COLUMNS = [
    "technical_pass",
    "near_technical_pass",
    "event_positive",
    "event_negative",
    "bhavcopy_positive",
    "bhavcopy_negative",
    "company_memory_positive",
    "company_memory_negative",
    "context_positive",
    "context_negative",
    "context_watch",
    "trusted_context_negative_block",
    "trusted_context_positive_boost",
    "selected",
    "matured",
    "hit_after_cost",
    "excess_hit_after_cost",
]
EVALUATION_TS_COLUMNS = ["evaluated_at", "asof_date", "entry_date", "exit_date", "benchmark_entry_date", "benchmark_exit_date", "load_ts"]

SUMMARY_NUMERIC_COLUMNS = [
    "selection_rate",
    "avg_forward_return",
    "median_forward_return",
    "avg_forward_return_after_cost",
    "hit_rate_after_cost",
    "positive_return_rate",
    "avg_benchmark_forward_return",
    "avg_excess_forward_return_after_cost",
    "excess_hit_rate_after_cost",
    "positive_excess_return_rate",
    "baseline_avg_forward_return_after_cost",
    "lift_vs_technical_only",
    "avg_selected_technical_score",
]
SUMMARY_INT_COLUMNS = ["horizon_days", "sample_count", "selected_count", "matured_count"]
SUMMARY_TS_COLUMNS = ["evaluated_at", "sample_start", "sample_end", "load_ts"]


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _json_ready(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    try:
        if pd.isna(value):
            return None
    except Exception as exc:
        _record_signal_quality_fallback(
            fallback_type="signal_quality_json_ready_missing_check_failed",
            source="json_ready",
            reason="Signal-quality evaluator could not evaluate missingness while preparing JSON and kept the original value.",
            error=exc,
            metadata={"value_type": type(value).__name__},
        )
    return value


def _record_signal_quality_fallback(
    *,
    fallback_type: str,
    source: str,
    reason: str,
    error: Exception,
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="advisory.signal_quality_evaluator",
        fallback_type=fallback_type,
        source=source,
        severity="warn",
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def table_columns(table_name: str) -> set[str]:
    try:
        df = sql_to_df(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = %s
            """,
            params=(table_name,),
            retries=2,
            statement_timeout_ms=5000,
        )
    except Exception as exc:
        _record_signal_quality_fallback(
            fallback_type="signal_quality_evaluator_schema_lookup_failed",
            source=table_name,
            reason="Signal-quality evaluator could not inspect source table columns.",
            error=exc,
            metadata={"table_name": table_name},
        )
        return set()
    return set(df["column_name"].astype(str).tolist()) if not df.empty else set()


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


def normalize_evaluation_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    for column in EVALUATION_NUMERIC_COLUMNS:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce")
    for column in EVALUATION_INT_COLUMNS:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce").astype("Int64")
    for column in EVALUATION_BOOL_COLUMNS:
        if column in out.columns:
            out[column] = _coerce_bool_series(out[column])
    for column in EVALUATION_TS_COLUMNS:
        if column in out.columns:
            out[column] = pd.to_datetime(out[column], utc=True, errors="coerce")
    return out


def normalize_summary_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    for column in SUMMARY_NUMERIC_COLUMNS:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce")
    for column in SUMMARY_INT_COLUMNS:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce").astype("Int64")
    for column in SUMMARY_TS_COLUMNS:
        if column in out.columns:
            out[column] = pd.to_datetime(out[column], utc=True, errors="coerce")
    return out


def ensure_tables() -> None:
    apply_schema_migration(
        migration_id=SIGNAL_QUALITY_SCHEMA_MIGRATION_ID,
        description="Create signal-quality evaluator output tables.",
        statements=SIGNAL_QUALITY_SCHEMA_STATEMENTS,
        metadata={"tables": [EVALUATIONS_TABLE, SUMMARY_TABLE]},
    )
    apply_schema_migration(
        migration_id=SIGNAL_QUALITY_CONTEXT_SCHEMA_MIGRATION_ID,
        description="Add review-only context-overlay flags to signal-quality evaluator output.",
        statements=SIGNAL_QUALITY_CONTEXT_SCHEMA_STATEMENTS,
        metadata={"tables": [EVALUATIONS_TABLE], "workflow": "signal_quality_context_overlays"},
    )
    apply_schema_migration(
        migration_id=SIGNAL_QUALITY_TRUSTED_ADJUSTMENT_SCHEMA_MIGRATION_ID,
        description="Add trusted context-rule adjustment fields to signal-quality evaluator output.",
        statements=SIGNAL_QUALITY_TRUSTED_ADJUSTMENT_SCHEMA_STATEMENTS,
        metadata={"tables": [EVALUATIONS_TABLE], "workflow": "signal_quality_trusted_context_adjustments"},
    )
    apply_schema_migration(
        migration_id=SIGNAL_QUALITY_BENCHMARK_SCHEMA_MIGRATION_ID,
        description="Add benchmark-excess attribution to signal-quality evaluator outputs.",
        statements=SIGNAL_QUALITY_BENCHMARK_SCHEMA_STATEMENTS,
        metadata={
            "tables": [EVALUATIONS_TABLE, SUMMARY_TABLE],
            "workflow": "signal_quality_benchmark_attribution",
            "authority_scope": "research_only_benchmark_attribution",
        },
    )


def _load_latest_event_policy(
    *,
    from_date: pd.Timestamp,
    to_date: pd.Timestamp,
    symbols: list[str],
    lookback_days: int,
) -> pd.DataFrame:
    columns = table_columns(EVENT_POLICY_TABLE)
    if not columns or not symbols:
        return pd.DataFrame(columns=["symbol", "asof_date"])
    select_exprs = [
        "UPPER(TRIM(symbol)) AS symbol",
        "asof_date",
        "action_type AS event_action_type" if "action_type" in columns else "NULL::text AS event_action_type",
        "policy_class AS event_policy_class" if "policy_class" in columns else "NULL::text AS event_policy_class",
        "policy_score AS event_policy_score" if "policy_score" in columns else "NULL::double precision AS event_policy_score",
        "confidence AS event_confidence" if "confidence" in columns else "NULL::double precision AS event_confidence",
        "event_class AS event_class" if "event_class" in columns else "NULL::text AS event_class",
    ]
    query_params = {
        "from_date": from_date - pd.Timedelta(days=max(0, int(lookback_days))),
        "to_date": to_date,
        "symbols": [str(symbol).upper() for symbol in symbols],
    }
    try:
        df = sql_to_df(
            f"""
            SELECT {', '.join(select_exprs)}
            FROM {EVENT_POLICY_TABLE}
            WHERE asof_date >= %(from_date)s
              AND asof_date <= %(to_date)s
              AND UPPER(TRIM(symbol)) = ANY(%(symbols)s)
            ORDER BY symbol, asof_date
            """,
            params=query_params,
            retries=4,
            statement_timeout_ms=0,
            chunksize=50000,
        )
    except Exception as exc:
        _record_signal_quality_fallback(
            fallback_type="signal_quality_evaluator_event_policy_load_failed",
            source=EVENT_POLICY_TABLE,
            reason="Signal-quality evaluator could not load point-in-time event-policy overlay rows.",
            error=exc,
            metadata={
                "from_date": str(query_params["from_date"]),
                "to_date": str(query_params["to_date"]),
                "symbol_count": len(query_params["symbols"]),
                "lookback_days": int(lookback_days),
            },
        )
        raise
    if df.empty:
        return df
    df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()
    df["asof_date"] = pd.to_datetime(df["asof_date"], utc=True, errors="coerce").dt.normalize()
    for column in ["event_policy_score", "event_confidence"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    return df.dropna(subset=["symbol", "asof_date"])


def _load_latest_bhavcopy(
    *,
    from_date: pd.Timestamp,
    to_date: pd.Timestamp,
    symbols: list[str],
    lookback_days: int,
) -> pd.DataFrame:
    columns = table_columns(BHAVCOPY_EVIDENCE_TABLE)
    if not columns or not symbols:
        return pd.DataFrame(columns=["symbol", "asof_date"])
    select_exprs = [
        "UPPER(TRIM(symbol)) AS symbol",
        "asof_date",
        "deal_pressure AS bhavcopy_deal_pressure" if "deal_pressure" in columns else "NULL::text AS bhavcopy_deal_pressure",
        "evidence_score AS bhavcopy_evidence_score" if "evidence_score" in columns else "NULL::double precision AS bhavcopy_evidence_score",
        "deal_net_value_inr AS bhavcopy_deal_net_value_inr" if "deal_net_value_inr" in columns else "NULL::double precision AS bhavcopy_deal_net_value_inr",
        "evidence_summary AS bhavcopy_evidence_summary" if "evidence_summary" in columns else "NULL::text AS bhavcopy_evidence_summary",
    ]
    query_params = {
        "from_date": from_date - pd.Timedelta(days=max(0, int(lookback_days))),
        "to_date": to_date,
        "symbols": [str(symbol).upper() for symbol in symbols],
    }
    try:
        df = sql_to_df(
            f"""
            SELECT {', '.join(select_exprs)}
            FROM {BHAVCOPY_EVIDENCE_TABLE}
            WHERE asof_date >= %(from_date)s
              AND asof_date <= %(to_date)s
              AND UPPER(TRIM(symbol)) = ANY(%(symbols)s)
            ORDER BY symbol, asof_date
            """,
            params=query_params,
            retries=4,
            statement_timeout_ms=0,
            chunksize=50000,
        )
    except Exception as exc:
        _record_signal_quality_fallback(
            fallback_type="signal_quality_evaluator_bhavcopy_load_failed",
            source=BHAVCOPY_EVIDENCE_TABLE,
            reason="Signal-quality evaluator could not load point-in-time bhavcopy evidence overlay rows.",
            error=exc,
            metadata={
                "from_date": str(query_params["from_date"]),
                "to_date": str(query_params["to_date"]),
                "symbol_count": len(query_params["symbols"]),
                "lookback_days": int(lookback_days),
            },
        )
        raise
    if df.empty:
        return df
    df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()
    df["asof_date"] = pd.to_datetime(df["asof_date"], utc=True, errors="coerce").dt.normalize()
    for column in ["bhavcopy_evidence_score", "bhavcopy_deal_net_value_inr"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    return df.dropna(subset=["symbol", "asof_date"])


def _load_latest_company_memory(
    *,
    from_date: pd.Timestamp,
    to_date: pd.Timestamp,
    symbols: list[str],
    lookback_days: int,
) -> pd.DataFrame:
    columns = table_columns(COMPANY_MEMORY_TABLE)
    if not columns or not symbols:
        return pd.DataFrame(columns=["symbol", "asof_date"])
    select_exprs = [
        "UPPER(TRIM(symbol)) AS symbol",
        "review_date AS asof_date",
        "recommended_signal AS company_memory_signal" if "recommended_signal" in columns else "NULL::text AS company_memory_signal",
        "confidence AS company_memory_confidence" if "confidence" in columns else "NULL::double precision AS company_memory_confidence",
        "conviction_score AS company_memory_conviction_score" if "conviction_score" in columns else "NULL::double precision AS company_memory_conviction_score",
        "summary AS company_memory_summary" if "summary" in columns else "NULL::text AS company_memory_summary",
    ]
    query_params = {
        "from_date": from_date - pd.Timedelta(days=max(0, int(lookback_days))),
        "to_date": to_date,
        "symbols": [str(symbol).upper() for symbol in symbols],
    }
    try:
        df = sql_to_df(
            f"""
            SELECT {', '.join(select_exprs)}
            FROM {COMPANY_MEMORY_TABLE}
            WHERE review_date >= %(from_date)s
              AND review_date <= %(to_date)s
              AND UPPER(TRIM(symbol)) = ANY(%(symbols)s)
            ORDER BY symbol, review_date
            """,
            params=query_params,
            retries=4,
            statement_timeout_ms=0,
            chunksize=50000,
        )
    except Exception as exc:
        _record_signal_quality_fallback(
            fallback_type="signal_quality_evaluator_company_memory_load_failed",
            source=COMPANY_MEMORY_TABLE,
            reason="Signal-quality evaluator could not load point-in-time company-memory overlay rows.",
            error=exc,
            metadata={
                "from_date": str(query_params["from_date"]),
                "to_date": str(query_params["to_date"]),
                "symbol_count": len(query_params["symbols"]),
                "lookback_days": int(lookback_days),
            },
        )
        raise
    if df.empty:
        return df
    df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()
    df["asof_date"] = pd.to_datetime(df["asof_date"], utc=True, errors="coerce").dt.normalize()
    for column in ["company_memory_confidence", "company_memory_conviction_score"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    return df.dropna(subset=["symbol", "asof_date"])


def _announcement_taxonomy_filter(columns: set[str]) -> tuple[str, dict[str, Any]]:
    contract = {
        "taxonomy_filter_applied": False,
        "archive_only_rows_excluded": False,
        "review_ready_rows_required": False,
        "raw_announcement_scan_allowed": False,
    }
    clauses: list[str] = []
    if "announcement_storage_form" in columns:
        clauses.append(
            "COALESCE(NULLIF(TRIM(announcement_storage_form), ''), 'compact_structured_event') <> "
            "'archived_raw_reference_only'"
        )
        contract["archive_only_rows_excluded"] = True
    if "llm_review_ready" in columns:
        clauses.append("COALESCE(llm_review_ready, TRUE) IS TRUE")
        contract["review_ready_rows_required"] = True
    if not clauses:
        return "", contract
    contract["taxonomy_filter_applied"] = True
    return "".join(f"\n              AND {clause}" for clause in clauses), contract


def _load_direct_context_table(
    *,
    table_name: str,
    source_name: str,
    from_date: pd.Timestamp,
    to_date: pd.Timestamp,
    symbols: list[str],
    lookback_days: int,
    date_column: str = "asof_date",
    score_column: str = "pressure_score",
    class_column: str | None = None,
) -> pd.DataFrame:
    columns = table_columns(table_name)
    required = {"symbol", "direction", date_column}
    if not symbols or not required.issubset(columns):
        return pd.DataFrame()
    taxonomy_filter, taxonomy_contract = (
        _announcement_taxonomy_filter(columns)
        if source_name == "announcement_context"
        else (
            "",
            {
                "taxonomy_filter_applied": False,
                "archive_only_rows_excluded": False,
                "review_ready_rows_required": False,
                "raw_announcement_scan_allowed": False,
            },
        )
    )
    query_params = {
        "from_date": from_date - pd.Timedelta(days=max(0, int(lookback_days))),
        "to_date": to_date + pd.Timedelta(days=1),
        "symbols": [str(symbol).upper() for symbol in symbols],
    }
    asof_expr = f"{date_column}::timestamptz"
    select_exprs = [
        "UPPER(TRIM(symbol)) AS symbol",
        f"{asof_expr} AS asof_date",
        "%(source_name)s::text AS context_source",
        "overlay_id AS context_overlay_id" if "overlay_id" in columns else "NULL::text AS context_overlay_id",
        "direction AS context_direction",
        f"{score_column} AS context_pressure_score" if score_column in columns else "NULL::double precision AS context_pressure_score",
        f"{class_column} AS context_class" if class_column and class_column in columns else "NULL::text AS context_class",
        "watch_reason_detail AS context_reason" if "watch_reason_detail" in columns else "trigger_reason AS context_reason" if "trigger_reason" in columns else "theme_reason AS context_reason" if "theme_reason" in columns else "NULL::text AS context_reason",
        "matched_sources_json AS context_sources_json" if "matched_sources_json" in columns else "NULL::text AS context_sources_json",
    ]
    try:
        df = sql_to_df(
            f"""
            SELECT {', '.join(select_exprs)}
            FROM {table_name}
            WHERE {date_column} >= %(from_date)s
              AND {date_column} < %(to_date)s
              AND UPPER(TRIM(symbol)) = ANY(%(symbols)s)
              AND COALESCE(production_status, 'active') = 'active'
              AND COALESCE(authority_scope, 'watchlist_pressure_only') = 'watchlist_pressure_only'
              {taxonomy_filter}
            ORDER BY symbol, {date_column}
            """,
            params={**query_params, "source_name": source_name},
            retries=4,
            statement_timeout_ms=0,
            chunksize=50000,
        )
    except Exception as exc:
        _record_signal_quality_fallback(
            fallback_type="signal_quality_evaluator_context_overlay_load_failed",
            source=table_name,
            reason="Signal-quality evaluator could not load point-in-time symbol context overlay rows.",
            error=exc,
            metadata={
                "from_date": str(query_params["from_date"]),
                "to_date": str(query_params["to_date"]),
                "symbol_count": len(query_params["symbols"]),
                "lookback_days": int(lookback_days),
                "source_name": source_name,
            },
        )
        raise
    if not df.empty and source_name == "announcement_context":
        df["compact_evidence_contract"] = json_dumps(
            {
                "source_table": table_name,
                "source_family": source_name,
                **taxonomy_contract,
                "authority_scope": "watchlist_pressure_only",
                "research_evaluator": "signal_quality_evaluator",
            }
        )
    return df


def _load_sector_context_table(
    *,
    table_name: str,
    source_name: str,
    from_date: pd.Timestamp,
    to_date: pd.Timestamp,
    symbols: list[str],
    lookback_days: int,
    id_column: str,
    reason_column: str,
) -> pd.DataFrame:
    overlay_columns = table_columns(table_name)
    universe_columns = table_columns(MARKET_CONTEXT_UNIVERSE_TABLE)
    required_overlay = {"asof_date", "direction", "sector_name", "sector_code"}
    required_universe = {"asof_date", "symbol", "sector_name", "sector_code"}
    if not symbols or not required_overlay.issubset(overlay_columns) or not required_universe.issubset(universe_columns):
        return pd.DataFrame()
    query_params = {
        "from_date": from_date - pd.Timedelta(days=max(0, int(lookback_days))),
        "to_date": to_date + pd.Timedelta(days=1),
        "symbols": [str(symbol).upper() for symbol in symbols],
    }
    try:
        df = sql_to_df(
            f"""
            SELECT
                UPPER(TRIM(u.symbol)) AS symbol,
                o.asof_date,
                %(source_name)s::text AS context_source,
                o.overlay_id AS context_overlay_id,
                o.direction AS context_direction,
                o.pressure_score AS context_pressure_score,
                {id_column} AS context_class,
                {reason_column} AS context_reason,
                o.matched_sources_json AS context_sources_json
            FROM {table_name} o
            JOIN {MARKET_CONTEXT_UNIVERSE_TABLE} u
              ON u.asof_date = o.asof_date
             AND (
                regexp_replace(upper(coalesce(u.sector_name, '')), '[^A-Z0-9]', '', 'g') = regexp_replace(upper(coalesce(o.sector_name, '')), '[^A-Z0-9]', '', 'g')
                OR regexp_replace(upper(coalesce(u.sector_code, '')), '[^A-Z0-9]', '', 'g') = regexp_replace(upper(coalesce(o.sector_code, '')), '[^A-Z0-9]', '', 'g')
             )
            WHERE o.asof_date >= %(from_date)s
              AND o.asof_date < %(to_date)s
              AND UPPER(TRIM(u.symbol)) = ANY(%(symbols)s)
              AND COALESCE(o.production_status, 'active') = 'active'
              AND COALESCE(o.authority_scope, 'watchlist_pressure_only') = 'watchlist_pressure_only'
            ORDER BY u.symbol, o.asof_date
            """,
            params={**query_params, "source_name": source_name},
            retries=4,
            statement_timeout_ms=0,
            chunksize=50000,
        )
    except Exception as exc:
        _record_signal_quality_fallback(
            fallback_type="signal_quality_evaluator_context_overlay_load_failed",
            source=table_name,
            reason="Signal-quality evaluator could not load point-in-time sector context overlay rows.",
            error=exc,
            metadata={
                "from_date": str(query_params["from_date"]),
                "to_date": str(query_params["to_date"]),
                "symbol_count": len(query_params["symbols"]),
                "lookback_days": int(lookback_days),
                "source_name": source_name,
                "market_context_table": MARKET_CONTEXT_UNIVERSE_TABLE,
            },
        )
        raise
    return df


def _aggregate_context_overlays(rows: pd.DataFrame) -> pd.DataFrame:
    if rows.empty:
        return pd.DataFrame(columns=["symbol", "asof_date"])
    frame = rows.copy()
    frame["symbol"] = frame["symbol"].astype("string").str.strip().str.upper()
    frame["asof_date"] = pd.to_datetime(frame["asof_date"], utc=True, errors="coerce").dt.normalize()
    frame["context_direction"] = frame["context_direction"].astype("string").str.strip().str.lower()
    frame["context_pressure_score"] = pd.to_numeric(frame["context_pressure_score"], errors="coerce")
    frame = frame.dropna(subset=["symbol", "asof_date"])
    if frame.empty:
        return pd.DataFrame(columns=["symbol", "asof_date"])
    out_rows: list[dict[str, Any]] = []
    for (symbol, asof_date), group in frame.groupby(["symbol", "asof_date"], dropna=False, sort=True):
        sources = []
        for _, item in group.sort_values("context_pressure_score", ascending=False, na_position="last").iterrows():
            sources.append(
                {
                    "source": _json_ready(item.get("context_source")),
                    "overlay_id": _json_ready(item.get("context_overlay_id")),
                    "direction": _json_ready(item.get("context_direction")),
                    "pressure_score": _json_ready(item.get("context_pressure_score")),
                    "class": _json_ready(item.get("context_class")),
                    "reason": _json_ready(item.get("context_reason")),
                    "compact_evidence_contract": _parse_jsonish(
                        item.get("compact_evidence_contract"),
                        {},
                        source="context_overlay.compact_evidence_contract",
                    ),
                }
            )
        directions = group["context_direction"].dropna().astype(str).str.lower()
        out_rows.append(
            {
                "symbol": symbol,
                "asof_date": asof_date,
                "context_positive": bool(directions.eq("positive").any()),
                "context_negative": bool(directions.eq("negative").any()),
                "context_watch": bool(directions.eq("watch").any()),
                "context_overlay_count": int(len(group)),
                "context_pressure_score": (
                    None
                    if group["context_pressure_score"].dropna().empty
                    else float(group["context_pressure_score"].abs().max())
                ),
                "context_sources_json": json_dumps(sources[:10]),
            }
        )
    return pd.DataFrame(out_rows)


def _load_latest_context_overlays(
    *,
    from_date: pd.Timestamp,
    to_date: pd.Timestamp,
    symbols: list[str],
    lookback_days: int,
) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame(columns=["symbol", "asof_date"])
    frames = [
        _load_direct_context_table(
            table_name=ANNOUNCEMENT_CONTEXT_OVERLAYS_TABLE,
            source_name="announcement_context",
            from_date=from_date,
            to_date=to_date,
            symbols=symbols,
            lookback_days=lookback_days,
            date_column="published_on",
            class_column="event_class",
        ),
        _load_direct_context_table(
            table_name=EXCHANGE_CONTEXT_OVERLAYS_TABLE,
            source_name="exchange_context",
            from_date=from_date,
            to_date=to_date,
            symbols=symbols,
            lookback_days=lookback_days,
            date_column="asof_date",
            class_column="event_type",
        ),
        _load_direct_context_table(
            table_name=BHAVCOPY_CONTEXT_OVERLAYS_TABLE,
            source_name="bhavcopy_context",
            from_date=from_date,
            to_date=to_date,
            symbols=symbols,
            lookback_days=lookback_days,
            date_column="asof_date",
            class_column="deal_pressure",
        ),
        _load_sector_context_table(
            table_name=THEME_CONTEXT_OVERLAYS_TABLE,
            source_name="theme_context",
            from_date=from_date,
            to_date=to_date,
            symbols=symbols,
            lookback_days=lookback_days,
            id_column="o.theme_id",
            reason_column="o.theme_reason",
        ),
        _load_sector_context_table(
            table_name=MACRO_CONTEXT_OVERLAYS_TABLE,
            source_name="macro_context",
            from_date=from_date,
            to_date=to_date,
            symbols=symbols,
            lookback_days=lookback_days,
            id_column="o.macro_signal_id",
            reason_column="o.trigger_reason",
        ),
    ]
    frames = [frame for frame in frames if isinstance(frame, pd.DataFrame) and not frame.empty]
    if not frames:
        return pd.DataFrame(columns=["symbol", "asof_date"])
    return _aggregate_context_overlays(pd.concat(frames, ignore_index=True, sort=False))


def _extract_trusted_context_adjustment(row: pd.Series) -> dict[str, Any] | None:
    raw_context = _parse_jsonish(row.get("raw_context_json"), {}, source="action_recommendations.raw_context_json")
    reason_context = _parse_jsonish(row.get("recommendation_reason_json"), {}, source="action_recommendations.recommendation_reason_json")
    if not isinstance(raw_context, dict):
        raw_context = {}
    if not isinstance(reason_context, dict):
        reason_context = {}
    adjustment = raw_context.get("signal_quality_overlay_rule_adjustment_json")
    if not isinstance(adjustment, dict):
        evidence = reason_context.get("evidence") if isinstance(reason_context.get("evidence"), dict) else {}
        section = evidence.get("signal_quality_overlay_rule_adjustment") if isinstance(evidence, dict) else {}
        if isinstance(section, dict):
            adjustment = section.get("signal_quality_overlay_rule_adjustment_json")
    if not isinstance(adjustment, dict):
        return None
    effect = str(adjustment.get("adjustment") or raw_context.get("signal_quality_overlay_rule_adjustment") or "").strip()
    if not effect:
        return None
    matched_rules = adjustment.get("matched_rules") if isinstance(adjustment.get("matched_rules"), list) else []
    return {
        "trusted_context_adjustment": effect,
        "trusted_context_original_action": str(adjustment.get("original_action_code") or "").strip().upper() or None,
        "trusted_context_adjusted_action": str(adjustment.get("adjusted_action_code") or row.get("action_code") or "").strip().upper() or None,
        "trusted_context_matched_rules_json": json_dumps(matched_rules[:10]),
        "trusted_context_negative_block": effect == "trusted_negative_context_blocks_positive_to_watch",
        "trusted_context_positive_boost": effect == "trusted_positive_context_boosts_watch_priority",
    }


def _load_latest_trusted_context_adjustments(
    *,
    from_date: pd.Timestamp,
    to_date: pd.Timestamp,
    symbols: list[str],
    lookback_days: int,
) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame(columns=["symbol", "asof_date"])
    columns = table_columns(ACTION_RECOMMENDATIONS_TABLE)
    required = {"asof_date", "symbol", "action_code", "raw_context_json"}
    if not required.issubset(columns):
        return pd.DataFrame(columns=["symbol", "asof_date"])
    query_params = {
        "from_date": from_date - pd.Timedelta(days=max(0, int(lookback_days))),
        "to_date": to_date + pd.Timedelta(days=1),
        "symbols": [str(symbol).upper() for symbol in symbols],
    }
    select_exprs = [
        "asof_date",
        "UPPER(TRIM(symbol)) AS symbol",
        "action_code",
        "raw_context_json",
        "recommendation_reason_json" if "recommendation_reason_json" in columns else "NULL::text AS recommendation_reason_json",
    ]
    try:
        df = sql_to_df(
            f"""
            SELECT {', '.join(select_exprs)}
            FROM {ACTION_RECOMMENDATIONS_TABLE}
            WHERE asof_date >= %(from_date)s
              AND asof_date < %(to_date)s
              AND UPPER(TRIM(symbol)) = ANY(%(symbols)s)
              AND raw_context_json LIKE '%%signal_quality_overlay_rule_adjustment%%'
            ORDER BY symbol, asof_date
            """,
            params=query_params,
            retries=4,
            statement_timeout_ms=0,
            chunksize=50000,
        )
    except Exception as exc:
        _record_signal_quality_fallback(
            fallback_type="signal_quality_evaluator_action_adjustment_load_failed",
            source=ACTION_RECOMMENDATIONS_TABLE,
            reason="Signal-quality evaluator could not load trusted context-rule adjustment rows from final action recommendations.",
            error=exc,
            metadata={
                "from_date": str(query_params["from_date"]),
                "to_date": str(query_params["to_date"]),
                "symbol_count": len(query_params["symbols"]),
                "lookback_days": int(lookback_days),
            },
        )
        raise
    if df.empty:
        return pd.DataFrame(columns=["symbol", "asof_date"])
    rows: list[dict[str, Any]] = []
    df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()
    df["asof_date"] = pd.to_datetime(df["asof_date"], utc=True, errors="coerce").dt.normalize()
    for _, row in df.dropna(subset=["symbol", "asof_date"]).iterrows():
        adjustment = _extract_trusted_context_adjustment(row)
        if not adjustment:
            continue
        rows.append(
            {
                "symbol": row.get("symbol"),
                "asof_date": row.get("asof_date"),
                **adjustment,
            }
        )
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=["symbol", "asof_date"])


def _merge_latest_asof(base: pd.DataFrame, overlay: pd.DataFrame, *, suffix: str) -> pd.DataFrame:
    if base.empty or overlay.empty:
        return base.copy()
    frames: list[pd.DataFrame] = []
    overlay_cols = [column for column in overlay.columns if column not in {"symbol", "asof_date"}]
    for symbol, group in base.groupby("symbol", dropna=False):
        left = group.sort_values("asof_date").copy()
        right = overlay[overlay["symbol"] == symbol].sort_values("asof_date").copy()
        if right.empty:
            frames.append(left)
            continue
        merged = pd.merge_asof(
            left,
            right[["asof_date", *overlay_cols]],
            on="asof_date",
            direction="backward",
            suffixes=("", f"_{suffix}"),
        )
        frames.append(merged)
    return pd.concat(frames, ignore_index=True, sort=False) if frames else base.copy()


def enrich_technical_signals(
    signals: pd.DataFrame,
    *,
    event_lookback_days: int = 30,
    bhavcopy_lookback_days: int = 30,
    company_memory_lookback_days: int = 180,
    context_lookback_days: int = 30,
    action_adjustment_lookback_days: int = 30,
) -> pd.DataFrame:
    if signals.empty:
        return signals.copy()
    base = signals.copy()
    base["symbol"] = base["symbol"].astype("string").str.strip().str.upper()
    base["asof_date"] = pd.to_datetime(base["asof_date"], utc=True, errors="coerce").dt.normalize()
    base = base.dropna(subset=["symbol", "asof_date"])
    if base.empty:
        return base

    symbols = base["symbol"].dropna().astype(str).drop_duplicates().tolist()
    from_date = base["asof_date"].min()
    to_date = base["asof_date"].max()
    event_rows = _load_latest_event_policy(from_date=from_date, to_date=to_date, symbols=symbols, lookback_days=event_lookback_days)
    bhavcopy_rows = _load_latest_bhavcopy(from_date=from_date, to_date=to_date, symbols=symbols, lookback_days=bhavcopy_lookback_days)
    memory_rows = _load_latest_company_memory(from_date=from_date, to_date=to_date, symbols=symbols, lookback_days=company_memory_lookback_days)
    context_rows = _load_latest_context_overlays(from_date=from_date, to_date=to_date, symbols=symbols, lookback_days=context_lookback_days)
    action_adjustment_rows = _load_latest_trusted_context_adjustments(
        from_date=from_date,
        to_date=to_date,
        symbols=symbols,
        lookback_days=action_adjustment_lookback_days,
    )

    out = _merge_latest_asof(base, event_rows, suffix="event")
    out = _merge_latest_asof(out, bhavcopy_rows, suffix="bhavcopy")
    out = _merge_latest_asof(out, memory_rows, suffix="memory")
    out = _merge_latest_asof(out, context_rows, suffix="context")
    out = _merge_latest_asof(out, action_adjustment_rows, suffix="action_adjustment")
    for column in [
        "event_policy_score",
        "event_confidence",
        "bhavcopy_evidence_score",
        "bhavcopy_deal_net_value_inr",
        "company_memory_confidence",
        "company_memory_conviction_score",
        "context_pressure_score",
    ]:
        if column not in out.columns:
            out[column] = pd.NA
        out[column] = pd.to_numeric(out[column], errors="coerce")
    for column in ["context_overlay_count"]:
        if column not in out.columns:
            out[column] = 0
        out[column] = pd.to_numeric(out[column], errors="coerce").fillna(0).astype("Int64")
    for column in ["context_positive", "context_negative", "context_watch"]:
        if column not in out.columns:
            out[column] = False
        out[column] = _coerce_bool_series(out[column]).fillna(False)
    for column in ["trusted_context_negative_block", "trusted_context_positive_boost"]:
        if column not in out.columns:
            out[column] = False
        out[column] = _coerce_bool_series(out[column]).fillna(False)
    for column in [
        "event_action_type",
        "event_policy_class",
        "event_class",
        "bhavcopy_deal_pressure",
        "bhavcopy_evidence_summary",
        "company_memory_signal",
        "company_memory_summary",
        "context_sources_json",
        "trusted_context_adjustment",
        "trusted_context_original_action",
        "trusted_context_adjusted_action",
        "trusted_context_matched_rules_json",
    ]:
        if column not in out.columns:
            out[column] = None
    return out


def _clean_text(value: Any) -> str:
    try:
        if pd.isna(value):
            return ""
    except Exception as exc:
        _record_signal_quality_fallback(
            fallback_type="signal_quality_clean_text_missing_check_failed",
            source="clean_text",
            reason="Signal-quality evaluator could not evaluate missingness while normalizing text and kept string conversion fallback.",
            error=exc,
            metadata={"value_type": type(value).__name__},
        )
    return str(value or "").strip().upper()


def _parse_jsonish(value: Any, default: Any, *, source: str) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        if pd.isna(value):
            return default
    except Exception as exc:
        _record_signal_quality_fallback(
            fallback_type="signal_quality_json_missing_check_failed",
            source=source,
            reason="Signal-quality evaluator could not evaluate missingness while parsing JSON payload.",
            error=exc,
            metadata={"value_type": type(value).__name__, "default_type": type(default).__name__},
        )
    try:
        return json.loads(str(value))
    except Exception as exc:
        if str(value or "").strip():
            _record_signal_quality_fallback(
                fallback_type="signal_quality_json_parse_failed",
                source=source,
                reason="Signal-quality evaluator could not parse JSON payload; using default.",
                error=exc,
                metadata={"payload_length": len(str(value))},
            )
        return default


def _technical_flags(row: pd.Series, *, technical_min: float, near_technical_min: float) -> tuple[bool, bool]:
    total = pd.to_numeric(row.get("technical_total_score"), errors="coerce")
    state = _clean_text(row.get("technical_state"))
    candidate_state = _clean_text(row.get("candidate_state"))
    trigger = _clean_text(row.get("technical_trigger_type"))
    score = 0.0 if pd.isna(total) else float(total)
    technical_pass = score >= float(technical_min) or state == "BUY_TRIGGERED" or candidate_state in {"READY", "BUY_TRIGGERED", "PASS_NOW"}
    near_pass = score >= float(near_technical_min) or technical_pass or bool(trigger)
    return bool(technical_pass), bool(near_pass)


def _context_source_family_flags(row: pd.Series) -> dict[str, dict[str, bool]]:
    payload = _parse_jsonish(row.get("context_sources_json"), [], source="context_sources_json")
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return {}
    out: dict[str, dict[str, bool]] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or item.get("context_source") or "").strip().lower()
        direction = str(item.get("direction") or item.get("context_direction") or "").strip().lower()
        if not source or direction not in {"positive", "negative", "watch"}:
            continue
        bucket = out.setdefault(source, {"positive": False, "negative": False, "watch": False})
        bucket[direction] = True
    return out


def _overlay_flags(row: pd.Series) -> dict[str, bool]:
    action = _clean_text(row.get("event_action_type"))
    policy_class = _clean_text(row.get("event_policy_class"))
    event_score = pd.to_numeric(row.get("event_policy_score"), errors="coerce")
    event_score_value = 0.0 if pd.isna(event_score) else float(event_score)
    event_positive = action in {"BUY_WATCH", "WATCH", "BUY", "BUY_MORE"} or policy_class in {"POSITIVE", "BUY_WATCH"} or event_score_value >= 0.25
    event_negative = action in {"REDUCE_EXPOSURE_REVIEW", "SELL", "SELL_PARTIAL"} or policy_class in {"NEGATIVE", "RISK"} or event_score_value <= -0.25

    pressure = _clean_text(row.get("bhavcopy_deal_pressure"))
    bhav_score = pd.to_numeric(row.get("bhavcopy_evidence_score"), errors="coerce")
    bhav_score_value = 0.0 if pd.isna(bhav_score) else float(bhav_score)
    bhav_positive = pressure == "ACCUMULATION" or bhav_score_value >= 0.15
    bhav_negative = pressure in {"DISTRIBUTION_OR_PRESSURE", "CIRCUIT_RISK"} or bhav_score_value <= -0.15

    memory_signal = _clean_text(row.get("company_memory_signal"))
    memory_conf = pd.to_numeric(row.get("company_memory_confidence"), errors="coerce")
    memory_conf_value = 0.0 if pd.isna(memory_conf) else float(memory_conf)
    memory_positive = memory_signal in {"BUY", "BUY_MORE", "WATCH", "HOLD"} and memory_conf_value >= 0.50
    memory_negative = memory_signal in {"SELL", "SELL_PARTIAL", "NO_ACTION"} and memory_conf_value >= 0.50
    context_positive = bool(row.get("context_positive") is True or str(row.get("context_positive")).strip().lower() in {"true", "1", "yes"})
    context_negative = bool(row.get("context_negative") is True or str(row.get("context_negative")).strip().lower() in {"true", "1", "yes"})
    context_watch = bool(row.get("context_watch") is True or str(row.get("context_watch")).strip().lower() in {"true", "1", "yes"})
    trusted_context_negative_block = bool(
        row.get("trusted_context_negative_block") is True
        or str(row.get("trusted_context_negative_block")).strip().lower() in {"true", "1", "yes"}
    )
    trusted_context_positive_boost = bool(
        row.get("trusted_context_positive_boost") is True
        or str(row.get("trusted_context_positive_boost")).strip().lower() in {"true", "1", "yes"}
    )
    return {
        "event_positive": bool(event_positive),
        "event_negative": bool(event_negative),
        "bhavcopy_positive": bool(bhav_positive),
        "bhavcopy_negative": bool(bhav_negative),
        "company_memory_positive": bool(memory_positive),
        "company_memory_negative": bool(memory_negative),
        "context_positive": bool(context_positive),
        "context_negative": bool(context_negative),
        "context_watch": bool(context_watch),
        "trusted_context_negative_block": bool(trusted_context_negative_block),
        "trusted_context_positive_boost": bool(trusted_context_positive_boost),
    }


def _variant_selected(
    variant: str,
    *,
    technical_pass: bool,
    near_pass: bool,
    flags: dict[str, bool],
    context_source_flags: dict[str, dict[str, bool]] | None = None,
) -> bool:
    if variant == "technical_only":
        return bool(technical_pass)
    if variant == "technical_plus_event":
        return bool((technical_pass and not flags["event_negative"]) or (near_pass and flags["event_positive"]))
    if variant == "technical_plus_bhavcopy":
        return bool((technical_pass and not flags["bhavcopy_negative"]) or (near_pass and flags["bhavcopy_positive"]))
    if variant == "technical_plus_company_memory":
        return bool((technical_pass and not flags["company_memory_negative"]) or (near_pass and flags["company_memory_positive"]))
    if variant == "technical_plus_all":
        positives = int(flags["event_positive"]) + int(flags["bhavcopy_positive"]) + int(flags["company_memory_positive"])
        negatives = flags["event_negative"] or flags["bhavcopy_negative"] or flags["company_memory_negative"]
        return bool((technical_pass and not negatives) or (near_pass and positives >= 2 and not negatives))
    if variant == "technical_plus_context_overlay":
        return bool((technical_pass and not flags["context_negative"]) or (near_pass and flags["context_positive"] and not flags["context_negative"]))
    if variant == "technical_plus_all_context":
        positives = int(flags["event_positive"]) + int(flags["bhavcopy_positive"]) + int(flags["company_memory_positive"]) + int(flags["context_positive"])
        negatives = flags["event_negative"] or flags["bhavcopy_negative"] or flags["company_memory_negative"] or flags["context_negative"]
        return bool((technical_pass and not negatives) or (near_pass and positives >= 2 and not negatives))
    if variant in CONTEXT_SOURCE_FAMILY_VARIANTS:
        source = CONTEXT_SOURCE_FAMILY_VARIANTS[variant]
        source_flags = (context_source_flags or {}).get(source, {})
        positive = bool(source_flags.get("positive"))
        negative = bool(source_flags.get("negative"))
        return bool((technical_pass and not negative) or (near_pass and positive and not negative))
    if variant == "technical_after_trusted_context_rules":
        return bool((technical_pass and not flags["trusted_context_negative_block"]) or (near_pass and flags["trusted_context_positive_boost"]))
    return False


def _signal_quality_contract_for_variant(variant: str) -> dict[str, Any]:
    source_family = CONTEXT_SOURCE_FAMILY_VARIANTS.get(variant)
    return {
        "authority_scope": "research_only",
        "action_policy_effect": "outcome_evaluation_only_no_live_policy_change",
        "portfolio_authority": "none",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        "requires_after_cost_validation": True,
        "baseline_variant": "technical_only",
        "variant": variant,
        "source_family": source_family,
    }


def _variant_selection_context(
    variant: str,
    *,
    selected: bool,
    technical_pass: bool,
    near_pass: bool,
    flags: dict[str, bool],
    context_source_flags: dict[str, dict[str, bool]],
) -> dict[str, Any]:
    source_family = CONTEXT_SOURCE_FAMILY_VARIANTS.get(variant)
    reason = "not_selected"
    source_flags: dict[str, bool] = {}
    if variant == "technical_only":
        reason = "technical_pass" if selected else "technical_threshold_not_met"
    elif variant in CONTEXT_SOURCE_FAMILY_VARIANTS:
        source_flags = dict(context_source_flags.get(source_family or "", {}))
        if selected and technical_pass and not source_flags.get("negative"):
            reason = "technical_pass_without_negative_source_family_context"
        elif selected and near_pass and source_flags.get("positive") and not source_flags.get("negative"):
            reason = "near_technical_plus_positive_source_family_context"
        elif source_flags.get("negative"):
            reason = "source_family_negative_context_blocks_selection"
        elif not source_flags:
            reason = "source_family_context_absent"
        else:
            reason = "source_family_context_not_sufficient"
    elif variant == "technical_plus_context_overlay":
        if selected and technical_pass and not flags.get("context_negative"):
            reason = "technical_pass_without_negative_context_overlay"
        elif selected and near_pass and flags.get("context_positive") and not flags.get("context_negative"):
            reason = "near_technical_plus_positive_context_overlay"
        elif flags.get("context_negative"):
            reason = "negative_context_overlay_blocks_selection"
        else:
            reason = "context_overlay_not_sufficient"
    elif variant == "technical_plus_bhavcopy":
        if selected and technical_pass and not flags.get("bhavcopy_negative"):
            reason = "technical_pass_without_negative_bhavcopy"
        elif selected and near_pass and flags.get("bhavcopy_positive"):
            reason = "near_technical_plus_positive_bhavcopy"
        elif flags.get("bhavcopy_negative"):
            reason = "negative_bhavcopy_blocks_selection"
        else:
            reason = "bhavcopy_not_sufficient"
    elif variant == "technical_after_trusted_context_rules":
        if selected and technical_pass and not flags.get("trusted_context_negative_block"):
            reason = "technical_pass_after_trusted_context_rules"
        elif selected and near_pass and flags.get("trusted_context_positive_boost"):
            reason = "near_technical_plus_trusted_positive_context_boost"
        elif flags.get("trusted_context_negative_block"):
            reason = "trusted_negative_context_blocks_selection"
        else:
            reason = "trusted_context_rules_not_sufficient"
    elif selected:
        reason = "variant_rule_selected"
    return {
        "variant": variant,
        "selected": bool(selected),
        "selection_reason": reason,
        "technical_pass": bool(technical_pass),
        "near_technical_pass": bool(near_pass),
        "source_family": source_family,
        "source_family_flags": source_flags,
        "context_policy_effect": "research_only_no_policy_change",
    }


def build_evaluation_rows(
    dataset: pd.DataFrame,
    *,
    horizons: list[int],
    cost_bps: float = DEFAULT_COST_BPS,
    return_threshold: float = DEFAULT_RETURN_THRESHOLD,
    technical_min: float = DEFAULT_TECHNICAL_MIN,
    near_technical_min: float = DEFAULT_NEAR_TECHNICAL_MIN,
    evaluated_at: pd.Timestamp | None = None,
) -> pd.DataFrame:
    if dataset.empty:
        return pd.DataFrame()
    effective_evaluated_at = pd.to_datetime(evaluated_at or pd.Timestamp.utcnow(), utc=True, errors="coerce")
    cost = float(cost_bps) / 10000.0
    rows: list[dict[str, Any]] = []
    for _, row in dataset.iterrows():
        technical_pass, near_pass = _technical_flags(row, technical_min=technical_min, near_technical_min=near_technical_min)
        flags = _overlay_flags(row)
        context_source_flags = _context_source_family_flags(row)
        context = {
            "event": {
                "action_type": _json_ready(row.get("event_action_type")),
                "policy_class": _json_ready(row.get("event_policy_class")),
                "event_class": _json_ready(row.get("event_class")),
                "policy_score": _json_ready(row.get("event_policy_score")),
            },
            "bhavcopy": {
                "deal_pressure": _json_ready(row.get("bhavcopy_deal_pressure")),
                "evidence_score": _json_ready(row.get("bhavcopy_evidence_score")),
                "summary": _json_ready(row.get("bhavcopy_evidence_summary")),
            },
            "company_memory": {
                "signal": _json_ready(row.get("company_memory_signal")),
                "confidence": _json_ready(row.get("company_memory_confidence")),
                "summary": _json_ready(row.get("company_memory_summary")),
            },
            "context_overlays": {
                "positive": _json_ready(row.get("context_positive")),
                "negative": _json_ready(row.get("context_negative")),
                "watch": _json_ready(row.get("context_watch")),
                "overlay_count": _json_ready(row.get("context_overlay_count")),
                "pressure_score": _json_ready(row.get("context_pressure_score")),
                "sources": _json_ready(row.get("context_sources_json")),
            },
            "trusted_context_rule_adjustment": {
                "adjustment": _json_ready(row.get("trusted_context_adjustment")),
                "original_action": _json_ready(row.get("trusted_context_original_action")),
                "adjusted_action": _json_ready(row.get("trusted_context_adjusted_action")),
                "negative_block": _json_ready(row.get("trusted_context_negative_block")),
                "positive_boost": _json_ready(row.get("trusted_context_positive_boost")),
                "matched_rules": _json_ready(row.get("trusted_context_matched_rules_json")),
            },
        }
        for horizon in horizons:
            return_col = f"forward_return_h{int(horizon)}"
            forward_return = pd.to_numeric(row.get(return_col), errors="coerce")
            matured = bool(pd.notna(forward_return))
            after_cost = None if not matured else float(forward_return) - cost
            benchmark_forward_return = pd.to_numeric(row.get(f"benchmark_forward_return_h{int(horizon)}"), errors="coerce")
            has_benchmark = bool(pd.notna(benchmark_forward_return))
            excess_after_cost = (
                None
                if not matured or not has_benchmark
                else float(forward_return) - float(benchmark_forward_return) - cost
            )
            benchmark_entry_date = pd.to_datetime(row.get(f"benchmark_entry_date_h{int(horizon)}"), utc=True, errors="coerce")
            benchmark_exit_date = pd.to_datetime(row.get(f"benchmark_exit_date_h{int(horizon)}"), utc=True, errors="coerce")
            for variant in VARIANTS:
                selected = _variant_selected(
                    variant,
                    technical_pass=technical_pass,
                    near_pass=near_pass,
                    flags=flags,
                    context_source_flags=context_source_flags,
                )
                row_context = dict(context)
                row_context["signal_quality_contract"] = _signal_quality_contract_for_variant(variant)
                row_context["variant_selection_context"] = _variant_selection_context(
                    variant,
                    selected=selected,
                    technical_pass=technical_pass,
                    near_pass=near_pass,
                    flags=flags,
                    context_source_flags=context_source_flags,
                )
                rows.append(
                    {
                        "evaluated_at": effective_evaluated_at,
                        "horizon_days": int(horizon),
                        "asof_date": row.get("asof_date"),
                        "setup_id": row.get("setup_id"),
                        "symbol": row.get("symbol"),
                        "variant": variant,
                        "technical_pass": technical_pass,
                        "near_technical_pass": near_pass,
                        **flags,
                        "selected": selected,
                        "technical_total_score": row.get("technical_total_score"),
                        "technical_state": row.get("technical_state"),
                        "candidate_state": row.get("candidate_state"),
                        "technical_trigger_type": row.get("technical_trigger_type"),
                        "event_action_type": row.get("event_action_type"),
                        "event_policy_class": row.get("event_policy_class"),
                        "event_policy_score": row.get("event_policy_score"),
                        "event_confidence": row.get("event_confidence"),
                        "bhavcopy_deal_pressure": row.get("bhavcopy_deal_pressure"),
                        "bhavcopy_evidence_score": row.get("bhavcopy_evidence_score"),
                        "bhavcopy_deal_net_value_inr": row.get("bhavcopy_deal_net_value_inr"),
                        "company_memory_signal": row.get("company_memory_signal"),
                        "company_memory_confidence": row.get("company_memory_confidence"),
                        "company_memory_conviction_score": row.get("company_memory_conviction_score"),
                        "context_overlay_count": row.get("context_overlay_count"),
                        "context_pressure_score": row.get("context_pressure_score"),
                        "context_sources_json": row.get("context_sources_json"),
                        "trusted_context_adjustment": row.get("trusted_context_adjustment"),
                        "trusted_context_original_action": row.get("trusted_context_original_action"),
                        "trusted_context_adjusted_action": row.get("trusted_context_adjusted_action"),
                        "trusted_context_matched_rules_json": row.get("trusted_context_matched_rules_json"),
                        "entry_date": row.get(f"entry_date_h{int(horizon)}"),
                        "exit_date": row.get(f"exit_date_h{int(horizon)}"),
                        "entry_close": row.get(f"entry_close_h{int(horizon)}"),
                        "exit_close": row.get(f"exit_close_h{int(horizon)}"),
                        "benchmark_name": row.get("benchmark_name"),
                        "benchmark_entry_date": None if pd.isna(benchmark_entry_date) else benchmark_entry_date,
                        "benchmark_exit_date": None if pd.isna(benchmark_exit_date) else benchmark_exit_date,
                        "benchmark_entry_close": row.get(f"benchmark_entry_close_h{int(horizon)}"),
                        "benchmark_exit_close": row.get(f"benchmark_exit_close_h{int(horizon)}"),
                        "benchmark_forward_return": None if not has_benchmark else float(benchmark_forward_return),
                        "forward_return": None if not matured else float(forward_return),
                        "forward_return_after_cost": after_cost,
                        "excess_forward_return_after_cost": excess_after_cost,
                        "hit_after_cost": None if after_cost is None else bool(after_cost >= float(return_threshold)),
                        "excess_hit_after_cost": None if excess_after_cost is None else bool(excess_after_cost >= float(return_threshold)),
                        "matured": matured,
                        "raw_context_json": json_dumps(row_context),
                        "load_ts": pd.Timestamp.utcnow(),
                    }
                )
    return normalize_evaluation_frame(pd.DataFrame(rows))


def _summary_recommendation(
    *,
    variant: str,
    matured_count: int,
    avg_after_cost: float | None,
    hit_rate: float | None,
    lift: float | None,
    avg_excess_after_cost: float | None,
    excess_hit_rate: float | None,
    min_rows: int,
) -> str:
    if matured_count < int(min_rows):
        return "insufficient_matured_rows"
    if variant == "technical_only":
        return "baseline"
    if avg_excess_after_cost is None or excess_hit_rate is None:
        return "needs_benchmark_attribution"
    if (
        lift is not None
        and avg_after_cost is not None
        and hit_rate is not None
        and lift > 0.01
        and avg_after_cost > 0
        and hit_rate >= 0.50
        and avg_excess_after_cost > 0
        and excess_hit_rate >= 0.50
    ):
        return "candidate_overlay_improves"
    if lift is not None and avg_after_cost is not None and lift > 0.01 and avg_after_cost > 0 and avg_excess_after_cost <= 0:
        return "benchmark_beta_not_overlay_alpha"
    if lift is not None and lift < -0.01:
        return "candidate_overlay_worse"
    if avg_after_cost is not None and avg_after_cost <= 0 and avg_excess_after_cost <= 0:
        return "candidate_overlay_worse"
    return "monitor"


def summarize_evaluations(
    evaluations: pd.DataFrame,
    *,
    evaluated_at: pd.Timestamp,
    min_matured_rows: int = DEFAULT_MIN_MATURED_ROWS,
) -> pd.DataFrame:
    if evaluations.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for horizon, horizon_group in evaluations.groupby("horizon_days", dropna=False):
        baseline_selected = horizon_group[
            (horizon_group["variant"] == "technical_only")
            & horizon_group["selected"].fillna(False).astype(bool)
            & horizon_group["matured"].fillna(False).astype(bool)
        ]
        baseline_after_cost = pd.to_numeric(baseline_selected.get("forward_return_after_cost"), errors="coerce").dropna()
        baseline_avg = float(baseline_after_cost.mean()) if not baseline_after_cost.empty else None
        for variant, group in horizon_group.groupby("variant", dropna=False):
            selected = group[group["selected"].fillna(False).astype(bool)]
            matured = selected[selected["matured"].fillna(False).astype(bool)]
            returns = pd.to_numeric(matured.get("forward_return"), errors="coerce").dropna()
            after_cost = pd.to_numeric(matured.get("forward_return_after_cost"), errors="coerce").dropna()
            benchmark_returns = pd.to_numeric(matured.get("benchmark_forward_return"), errors="coerce").dropna()
            excess_after_cost = pd.to_numeric(matured.get("excess_forward_return_after_cost"), errors="coerce").dropna()
            hit = matured["hit_after_cost"].dropna().astype(bool) if "hit_after_cost" in matured.columns else pd.Series(dtype=bool)
            excess_hit = matured["excess_hit_after_cost"].dropna().astype(bool) if "excess_hit_after_cost" in matured.columns else pd.Series(dtype=bool)
            avg_after_cost = float(after_cost.mean()) if not after_cost.empty else None
            avg_excess_after_cost = float(excess_after_cost.mean()) if not excess_after_cost.empty else None
            lift = None if baseline_avg is None or avg_after_cost is None else float(avg_after_cost - baseline_avg)
            hit_rate = float(hit.mean()) if not hit.empty else None
            excess_hit_rate = float(excess_hit.mean()) if not excess_hit.empty else None
            rows.append(
                {
                    "evaluated_at": evaluated_at,
                    "horizon_days": int(horizon),
                    "variant": str(variant),
                    "sample_count": int(len(group)),
                    "selected_count": int(len(selected)),
                    "matured_count": int(len(matured)),
                    "selection_rate": None if len(group) == 0 else float(len(selected) / len(group)),
                    "avg_forward_return": float(returns.mean()) if not returns.empty else None,
                    "median_forward_return": float(returns.median()) if not returns.empty else None,
                    "avg_forward_return_after_cost": avg_after_cost,
                    "hit_rate_after_cost": hit_rate,
                    "positive_return_rate": float(returns.gt(0).mean()) if not returns.empty else None,
                    "avg_benchmark_forward_return": float(benchmark_returns.mean()) if not benchmark_returns.empty else None,
                    "avg_excess_forward_return_after_cost": avg_excess_after_cost,
                    "excess_hit_rate_after_cost": excess_hit_rate,
                    "positive_excess_return_rate": float(excess_after_cost.gt(0).mean()) if not excess_after_cost.empty else None,
                    "baseline_avg_forward_return_after_cost": baseline_avg,
                    "lift_vs_technical_only": lift,
                    "avg_selected_technical_score": (
                        None
                        if selected["technical_total_score"].dropna().empty
                        else float(pd.to_numeric(selected["technical_total_score"], errors="coerce").mean())
                    ),
                    "sample_start": matured["asof_date"].min() if not matured.empty else pd.NaT,
                    "sample_end": matured["asof_date"].max() if not matured.empty else pd.NaT,
                    "recommendation": _summary_recommendation(
                        variant=str(variant),
                        matured_count=int(len(matured)),
                        avg_after_cost=avg_after_cost,
                        hit_rate=hit_rate,
                        lift=lift,
                        avg_excess_after_cost=avg_excess_after_cost,
                        excess_hit_rate=excess_hit_rate,
                        min_rows=min_matured_rows,
                    ),
                    "load_ts": pd.Timestamp.utcnow(),
                }
            )
    return normalize_summary_frame(pd.DataFrame(rows))


def evaluate_signal_quality(
    *,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    horizons: list[int] | None = None,
    return_threshold: float = DEFAULT_RETURN_THRESHOLD,
    cost_bps: float = DEFAULT_COST_BPS,
    min_matured_rows: int = DEFAULT_MIN_MATURED_ROWS,
    technical_min: float = DEFAULT_TECHNICAL_MIN,
    near_technical_min: float = DEFAULT_NEAR_TECHNICAL_MIN,
    event_lookback_days: int = 30,
    bhavcopy_lookback_days: int = 30,
    company_memory_lookback_days: int = 180,
    context_lookback_days: int = 30,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    effective_horizons = sorted({int(value) for value in (horizons or DEFAULT_HORIZONS) if int(value) > 0})
    signals = load_technical_signal_rows(from_date=from_date, to_date=to_date, symbols=symbols)
    if signals.empty or not effective_horizons:
        return pd.DataFrame(), pd.DataFrame(), {"signal_rows": int(len(signals)), "matured_rows_by_horizon": {}}
    enriched = enrich_technical_signals(
        signals,
        event_lookback_days=event_lookback_days,
        bhavcopy_lookback_days=bhavcopy_lookback_days,
        company_memory_lookback_days=company_memory_lookback_days,
        context_lookback_days=context_lookback_days,
    )
    price_start = enriched["asof_date"].min() - pd.Timedelta(days=5)
    price_end = enriched["asof_date"].max() + pd.Timedelta(days=max(effective_horizons) * 3 + 15)
    prices = load_price_history_for_returns(
        symbols=enriched["symbol"].dropna().astype(str).str.upper().drop_duplicates().tolist(),
        from_date=price_start,
        to_date=price_end,
    )
    dataset = attach_forward_returns(enriched, prices, horizons=effective_horizons)
    benchmark = load_benchmark_history_for_attribution(from_date=price_start, to_date=price_end)
    dataset = attach_benchmark_forward_returns(dataset, benchmark, horizons=effective_horizons)
    evaluated_at = pd.Timestamp.utcnow()
    evaluations = build_evaluation_rows(
        dataset,
        horizons=effective_horizons,
        cost_bps=cost_bps,
        return_threshold=return_threshold,
        technical_min=technical_min,
        near_technical_min=near_technical_min,
        evaluated_at=evaluated_at,
    )
    summary = summarize_evaluations(evaluations, evaluated_at=evaluated_at, min_matured_rows=min_matured_rows)
    meta = {
        "signal_rows": int(len(signals)),
        "enriched_rows": int(len(enriched)),
        "price_rows": int(len(prices)),
        "benchmark_rows": int(len(benchmark)),
        "evaluation_rows": int(len(evaluations)),
        "summary_rows": int(len(summary)),
        "overlay_coverage": {
            "event_policy_rows": int(enriched["event_action_type"].notna().sum()) if "event_action_type" in enriched.columns else 0,
            "bhavcopy_rows": int(enriched["bhavcopy_deal_pressure"].notna().sum()) if "bhavcopy_deal_pressure" in enriched.columns else 0,
            "company_memory_rows": int(enriched["company_memory_signal"].notna().sum()) if "company_memory_signal" in enriched.columns else 0,
            "context_overlay_rows": int(pd.to_numeric(enriched.get("context_overlay_count", pd.Series(dtype=float)), errors="coerce").fillna(0).gt(0).sum()),
        },
        "matured_rows_by_horizon": {
            str(horizon): int(evaluations[(evaluations["horizon_days"] == int(horizon)) & (evaluations["matured"] == True)].shape[0])
            for horizon in effective_horizons
        },
        "selected_rows_by_variant": evaluations.groupby("variant")["selected"].sum().to_dict() if not evaluations.empty else {},
        "horizons": effective_horizons,
        "return_threshold": float(return_threshold),
        "cost_bps": float(cost_bps),
        "technical_min": float(technical_min),
        "near_technical_min": float(near_technical_min),
        "event_lookback_days": int(event_lookback_days),
        "bhavcopy_lookback_days": int(bhavcopy_lookback_days),
        "company_memory_lookback_days": int(company_memory_lookback_days),
        "context_lookback_days": int(context_lookback_days),
        "point_in_time_return_contract": (
            json.loads(dataset["point_in_time_return_contract_json"].dropna().iloc[0])
            if "point_in_time_return_contract_json" in dataset.columns and dataset["point_in_time_return_contract_json"].notna().any()
            else {}
        ),
        "benchmark_return_contract": (
            json.loads(dataset["benchmark_return_contract_json"].dropna().iloc[0])
            if "benchmark_return_contract_json" in dataset.columns and dataset["benchmark_return_contract_json"].notna().any()
            else {}
        ),
    }
    return evaluations, summary, meta


def persist_outputs(evaluations: pd.DataFrame, summary: pd.DataFrame) -> None:
    ensure_tables()
    evaluations = normalize_evaluation_frame(evaluations)
    summary = normalize_summary_frame(summary)
    if not evaluations.empty:
        upsert_to_db(
            evaluations,
            EVALUATIONS_TABLE,
            unique_keys=["evaluated_at", "horizon_days", "asof_date", "setup_id", "symbol", "variant"],
        )
    if not summary.empty:
        upsert_to_db(
            summary,
            SUMMARY_TABLE,
            unique_keys=["evaluated_at", "horizon_days", "variant"],
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare realized outcomes for technical-only signals versus technical signals enriched with event, bhavcopy, company-memory, and context-overlay evidence."
    )
    parser.add_argument("--from-date", type=parse_datetime_arg)
    parser.add_argument("--to-date", type=parse_datetime_arg)
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument("--horizons", nargs="*", type=int, default=DEFAULT_HORIZONS)
    parser.add_argument("--return-threshold", type=float, default=DEFAULT_RETURN_THRESHOLD)
    parser.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    parser.add_argument("--min-matured-rows", type=int, default=DEFAULT_MIN_MATURED_ROWS)
    parser.add_argument("--technical-min", type=float, default=DEFAULT_TECHNICAL_MIN)
    parser.add_argument("--near-technical-min", type=float, default=DEFAULT_NEAR_TECHNICAL_MIN)
    parser.add_argument("--event-lookback-days", type=int, default=30)
    parser.add_argument("--bhavcopy-lookback-days", type=int, default=30)
    parser.add_argument("--company-memory-lookback-days", type=int, default=180)
    parser.add_argument("--context-lookback-days", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def _arg_timestamp(value: Any) -> pd.Timestamp | None:
    if value is None:
        return None
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.normalize()


def main() -> int:
    args = parse_args()
    evaluations, summary, meta = evaluate_signal_quality(
        from_date=_arg_timestamp(args.from_date),
        to_date=_arg_timestamp(args.to_date),
        symbols=args.symbols,
        horizons=args.horizons,
        return_threshold=float(args.return_threshold),
        cost_bps=float(args.cost_bps),
        min_matured_rows=int(args.min_matured_rows),
        technical_min=float(args.technical_min),
        near_technical_min=float(args.near_technical_min),
        event_lookback_days=max(0, int(args.event_lookback_days)),
        bhavcopy_lookback_days=max(0, int(args.bhavcopy_lookback_days)),
        company_memory_lookback_days=max(0, int(args.company_memory_lookback_days)),
        context_lookback_days=max(0, int(args.context_lookback_days)),
    )
    if not args.dry_run:
        persist_outputs(evaluations, summary)
    print(
        json.dumps(
            {
                "status": "ok",
                "evaluations_table": EVALUATIONS_TABLE,
                "summary_table": SUMMARY_TABLE,
                "evaluation_rows": int(len(evaluations)),
                "summary_rows": int(len(summary)),
                "meta": meta,
                "summary_sample": summary.head(10).to_dict(orient="records") if not summary.empty else [],
                "dry_run": bool(args.dry_run),
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
