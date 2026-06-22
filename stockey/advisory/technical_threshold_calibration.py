from __future__ import annotations

import argparse
import itertools
import json
import sys
from typing import Any

import pandas as pd

from advisory.fallback_telemetry import record_local_fallback_event
from advisory.technical_engine import evaluate_pre_entry_state
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration
from utils.sync import parse_datetime_arg


EVALUATIONS_TABLE = "advisory_technical_threshold_evaluations"
SUMMARY_TABLE = "advisory_technical_threshold_eval_summary"
TECHNICAL_THRESHOLD_SCHEMA_MIGRATION_ID = "20260611_advisory_technical_threshold_calibration_base"
TECHNICAL_THRESHOLD_REPAIR_MIGRATION_ID = "20260622_advisory_technical_threshold_timescale_unique_keys_repair"
TECHNICAL_ARCHETYPE_SUMMARY_MIGRATION_ID = "20260621_advisory_technical_threshold_archetype_summary"
DEFAULT_HORIZONS = [5, 10, 20]
DEFAULT_RETURN_THRESHOLD = 0.03
DEFAULT_COST_BPS = 25.0
DEFAULT_MIN_SIGNALS = 10
DEFAULT_PROGRESS_EVERY = 1000
TECHNICAL_DAILY_TABLE = "advisory_technical_daily"


class SourceUnavailableError(RuntimeError):
    """Raised when required calibration inputs cannot be read reliably."""


def _emit(message: str) -> None:
    print(f"[advisory.technical_threshold_calibration] {message}", file=sys.stderr, flush=True)


TECHNICAL_THRESHOLD_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {EVALUATIONS_TABLE} (
        evaluated_at TIMESTAMPTZ NOT NULL,
        config_id TEXT NOT NULL,
        horizon_days BIGINT NOT NULL,
        threshold_config_json TEXT NOT NULL,
        signal_count BIGINT,
        eligible_count BIGINT,
        trade_rate DOUBLE PRECISION,
        avg_forward_return DOUBLE PRECISION,
        median_forward_return DOUBLE PRECISION,
        hit_rate DOUBLE PRECISION,
        avg_forward_return_after_cost DOUBLE PRECISION,
        hit_rate_after_cost DOUBLE PRECISION,
        avg_rejected_forward_return DOUBLE PRECISION,
        spread_vs_rejected DOUBLE PRECISION,
        objective_score DOUBLE PRECISION,
        sample_start TIMESTAMPTZ,
        sample_end TIMESTAMPTZ,
        load_ts TIMESTAMPTZ,
        UNIQUE (evaluated_at, config_id, horizon_days)
    )
    """,
    f"""
    DO $$
    DECLARE
        rec RECORD;
    BEGIN
        FOR rec IN
            SELECT
                con.conname AS constraint_name,
                idx.relname AS index_name
            FROM pg_index i
            JOIN pg_class tbl ON tbl.oid = i.indrelid
            JOIN pg_namespace ns ON ns.oid = tbl.relnamespace
            JOIN pg_class idx ON idx.oid = i.indexrelid
            LEFT JOIN pg_constraint con ON con.conindid = i.indexrelid
            WHERE ns.nspname = 'public'
              AND tbl.relname = '{EVALUATIONS_TABLE}'
              AND i.indisunique
              AND (
                  SELECT array_agg(att.attname::text ORDER BY keys.ord)
                  FROM unnest(i.indkey) WITH ORDINALITY AS keys(attnum, ord)
                  JOIN pg_attribute att
                    ON att.attrelid = tbl.oid
                   AND att.attnum = keys.attnum
              ) = ARRAY['config_id', 'horizon_days']::text[]
        LOOP
            IF rec.constraint_name IS NOT NULL THEN
                EXECUTE format('ALTER TABLE %I DROP CONSTRAINT IF EXISTS %I', '{EVALUATIONS_TABLE}', rec.constraint_name);
            ELSE
                EXECUTE format('DROP INDEX IF EXISTS %I', rec.index_name);
            END IF;
        END LOOP;
    END $$;
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {SUMMARY_TABLE} (
        evaluated_at TIMESTAMPTZ NOT NULL,
        horizon_days BIGINT NOT NULL,
        best_config_id TEXT,
        best_threshold_config_json TEXT,
        best_objective_score DOUBLE PRECISION,
        best_eligible_count BIGINT,
        best_hit_rate_after_cost DOUBLE PRECISION,
        best_avg_forward_return_after_cost DOUBLE PRECISION,
        baseline_signal_count BIGINT,
        baseline_avg_forward_return_after_cost DOUBLE PRECISION,
        baseline_hit_rate_after_cost DOUBLE PRECISION,
        archetype_breakdown_json TEXT,
        recommendation TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (evaluated_at, horizon_days)
    )
    """,
]

TECHNICAL_THRESHOLD_REPAIR_STATEMENTS = [
    f"""
    DO $$
    DECLARE
        rec RECORD;
    BEGIN
        FOR rec IN
            SELECT
                tbl.relname AS table_name,
                con.conname AS constraint_name,
                idx.relname AS index_name,
                COALESCE(
                    (
                        SELECT array_agg(att.attname::text ORDER BY keys.ord)
                        FROM unnest(i.indkey) WITH ORDINALITY AS keys(attnum, ord)
                        JOIN pg_attribute att
                          ON att.attrelid = tbl.oid
                         AND att.attnum = keys.attnum
                    ),
                    ARRAY[]::text[]
                ) AS columns
            FROM pg_index i
            JOIN pg_class tbl ON tbl.oid = i.indrelid
            JOIN pg_namespace ns ON ns.oid = tbl.relnamespace
            JOIN pg_class idx ON idx.oid = i.indexrelid
            LEFT JOIN pg_constraint con ON con.conindid = i.indexrelid
            WHERE ns.nspname = 'public'
              AND tbl.relname IN ('{EVALUATIONS_TABLE}', '{SUMMARY_TABLE}')
              AND i.indisunique
        LOOP
            IF NOT ('evaluated_at' = ANY(rec.columns)) THEN
                IF rec.constraint_name IS NOT NULL THEN
                    EXECUTE format('ALTER TABLE %I DROP CONSTRAINT IF EXISTS %I', rec.table_name, rec.constraint_name);
                ELSE
                    EXECUTE format('DROP INDEX IF EXISTS %I', rec.index_name);
                END IF;
            END IF;
        END LOOP;
    END $$;
    """,
]

TECHNICAL_ARCHETYPE_SUMMARY_STATEMENTS = [
    f"ALTER TABLE {SUMMARY_TABLE} ADD COLUMN IF NOT EXISTS archetype_breakdown_json TEXT",
]

DEFAULT_GRID = {
    "trend_min": [0.0, 12.0, 15.0, 18.0],
    "structure_min": [0.0, 15.0, 18.0, 21.0],
    "participation_min": [0.0, 8.0, 10.0, 12.0],
    "relative_strength_min": [0.0, 6.0, 8.0, 10.0],
    "tradability_min": [0.0, 5.0, 6.0, 7.0],
    "ready_total_min": [68.0, 70.0, 72.0],
    "buy_total_min": [76.0, 78.0, 80.0, 82.0],
}

SCORE_COLUMNS = {
    "trend_min": "technical_trend_score",
    "structure_min": "technical_structure_score",
    "participation_min": "technical_participation_score",
    "relative_strength_min": "technical_relative_strength_score",
    "tradability_min": "technical_tradability_score",
    "buy_total_min": "technical_total_score",
}

OPTIONAL_SIGNAL_COLUMNS = [
    "candidate_state",
    "technical_state",
    "technical_trigger_type",
    "technical_setup_archetype",
    "technical_setup_quality_json",
    "technical_score",
    "technical_trend_score",
    "technical_structure_score",
    "technical_participation_score",
    "technical_relative_strength_score",
    "technical_tradability_score",
    "technical_total_score",
    "setup_score",
    "load_ts",
]

TECHNICAL_RECONSTRUCTION_COLUMNS = {
    "adj_close": "DOUBLE PRECISION",
    "avg_traded_value_20d": "DOUBLE PRECISION",
    "median_volume_20d": "DOUBLE PRECISION",
    "atr_pct": "DOUBLE PRECISION",
    "gap_frequency_60d": "DOUBLE PRECISION",
    "base_depth_60d_pct": "DOUBLE PRECISION",
    "pass_liquidity_20d": "BOOLEAN",
    "pass_gap_behavior": "BOOLEAN",
    "pass_above_dma_20": "BOOLEAN",
    "pass_above_dma_50": "BOOLEAN",
    "pass_above_dma_150": "BOOLEAN",
    "pass_above_dma_200": "BOOLEAN",
    "pass_trend_alignment": "BOOLEAN",
    "dma_50_slope_20d_pct": "DOUBLE PRECISION",
    "dma_150_slope_20d_pct": "DOUBLE PRECISION",
    "dist_52w_high": "DOUBLE PRECISION",
    "trend_persistence_60d": "DOUBLE PRECISION",
    "trend_persistence_120d": "DOUBLE PRECISION",
    "higher_high_count_20d": "DOUBLE PRECISION",
    "higher_low_count_20d": "DOUBLE PRECISION",
    "pivot_distance_20d_pct": "DOUBLE PRECISION",
    "range_contraction_ratio": "DOUBLE PRECISION",
    "volatility_contraction_flag": "BOOLEAN",
    "tight_close_upper_half_20d": "DOUBLE PRECISION",
    "support_hold_rate_20d": "DOUBLE PRECISION",
    "breakout_extension_pct": "DOUBLE PRECISION",
    "bb_width_rank_252d": "DOUBLE PRECISION",
    "breakout_day_volume_vs_20d": "DOUBLE PRECISION",
    "up_down_volume_ratio_20d": "DOUBLE PRECISION",
    "accumulation_days_20d": "DOUBLE PRECISION",
    "distribution_days_20d": "DOUBLE PRECISION",
    "pullback_volume_dryup_ratio_20d": "DOUBLE PRECISION",
    "rs_vs_benchmark": "DOUBLE PRECISION",
    "rs_vs_sector": "DOUBLE PRECISION",
    "stock_ret_60d": "DOUBLE PRECISION",
    "stock_ret_120d": "DOUBLE PRECISION",
    "support_distance_20d_pct": "DOUBLE PRECISION",
    "close_location_pct": "DOUBLE PRECISION",
    "dist_20d_high": "DOUBLE PRECISION",
}


KNOWN_SETUP_ARCHETYPES = {
    "breakout",
    "breakout_retest",
    "trend_pullback",
    "trend_pullback_watch",
    "reclaim",
    "watch_breakout",
    "watch_pullback",
    "near_pivot",
    "ready_no_trigger",
    "buy_triggered_unclassified",
}


def _record_technical_threshold_calibration_fallback(
    *,
    fallback_type: str,
    source: str,
    reason: str,
    error: Exception,
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="advisory.technical_threshold_calibration",
        fallback_type=fallback_type,
        source=source,
        severity="warn",
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def json_safe_value(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return value


def _json_loads(value: Any, default: Any) -> Any:
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
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if bool(pd.isna(value)):
        return ""
    return str(value).strip().lower()


def infer_calibration_setup_archetype(row: pd.Series | dict[str, Any]) -> str:
    """Infer a research-only setup bucket for older rows without explicit archetype."""
    explicit = _clean_text(row.get("technical_setup_archetype"))
    if explicit and explicit not in {"unknown", "none", "nan", "null"}:
        return explicit

    trigger = _clean_text(row.get("technical_trigger_type"))
    if trigger in {"breakout", "breakout_retest", "trend_pullback", "reclaim"}:
        return trigger

    candidate_state = _clean_text(row.get("candidate_state"))
    technical_state = _clean_text(row.get("technical_state"))
    setup_id = _clean_text(row.get("setup_id"))

    if "pullback" in candidate_state or "pullback" in setup_id or technical_state == "add_on_pullback":
        return "trend_pullback_watch" if candidate_state.startswith("watch") else "trend_pullback"
    if candidate_state in {"near_pivot", "watch_pivot"} or (
        candidate_state in {"watchlist", "watch_event"} and technical_state == "near_pivot"
    ):
        return "near_pivot"
    if "breakout" in candidate_state or "breakout" in setup_id:
        return "watch_breakout" if candidate_state.startswith("watch") else "breakout"
    if technical_state == "ready":
        return "ready_no_trigger"
    if technical_state == "buy_triggered":
        return "buy_triggered_unclassified"
    return "unknown"


def ensure_tables() -> None:
    apply_schema_migration(
        migration_id=TECHNICAL_THRESHOLD_SCHEMA_MIGRATION_ID,
        description="Create technical-threshold calibration output tables.",
        statements=TECHNICAL_THRESHOLD_SCHEMA_STATEMENTS,
        metadata={"tables": [EVALUATIONS_TABLE, SUMMARY_TABLE]},
    )
    apply_schema_migration(
        migration_id=TECHNICAL_THRESHOLD_REPAIR_MIGRATION_ID,
        description="Drop stale technical-threshold unique indexes that omit the Timescale partition column.",
        statements=TECHNICAL_THRESHOLD_REPAIR_STATEMENTS,
        metadata={"tables": [EVALUATIONS_TABLE, SUMMARY_TABLE], "partition_column": "evaluated_at"},
    )
    apply_schema_migration(
        migration_id=TECHNICAL_ARCHETYPE_SUMMARY_MIGRATION_ID,
        description="Add technical setup archetype breakdown to technical-threshold calibration summaries.",
        statements=TECHNICAL_ARCHETYPE_SUMMARY_STATEMENTS,
        metadata={"tables": [SUMMARY_TABLE], "source_table": "advisory_candidates"},
    )


def table_columns(table_name: str, *, raise_on_error: bool = False) -> set[str]:
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
        )
    except Exception as exc:
        _record_technical_threshold_calibration_fallback(
            fallback_type="technical_threshold_calibration_schema_lookup_failed",
            source=table_name,
            reason="Technical-threshold calibration could not inspect source table columns.",
            error=exc,
            metadata={"table_name": table_name},
        )
        if raise_on_error:
            raise SourceUnavailableError(f"Technical-threshold calibration could not inspect {table_name} columns: {exc}") from exc
        return set()
    return set(df["column_name"].astype(str).tolist()) if not df.empty else set()


def _optional_select_alias(alias: str, column_name: str, columns: set[str], *, cast: str = "TEXT", output_name: str | None = None) -> str:
    output = output_name or column_name
    if column_name in columns:
        return f"{alias}.{column_name} AS {output}"
    return f"NULL::{cast} AS {output}"


def _has_blocker_contract(value: Any) -> bool:
    payload = _json_loads(value, {})
    if not isinstance(payload, dict):
        return False
    return any(
        key in payload
        for key in {
            "entry_trigger_status",
            "nearest_entry_trigger_type",
            "entry_trigger_blockers",
            "entry_trigger_diagnostics",
            "buy_readiness",
        }
    )


def _reconstruct_technical_setup_quality(row: pd.Series) -> str | None:
    if not any(
        not pd.isna(pd.to_numeric(row.get(column), errors="coerce"))
        for column in TECHNICAL_RECONSTRUCTION_COLUMNS
    ):
        return None
    try:
        reconstructed = evaluate_pre_entry_state(row)
    except Exception as exc:
        _record_technical_threshold_calibration_fallback(
            fallback_type="technical_threshold_calibration_contract_reconstruction_failed",
            source=TECHNICAL_DAILY_TABLE,
            reason="Technical-threshold calibration could not reconstruct missing blocker contracts from point-in-time technical features.",
            error=exc,
            metadata={
                "symbol": str(row.get("symbol") or "").strip().upper(),
                "setup_id": row.get("setup_id"),
                "asof_date": str(row.get("asof_date")),
            },
        )
        return None
    setup_quality = reconstructed.get("technical_setup_quality") if isinstance(reconstructed, dict) else None
    if not isinstance(setup_quality, dict):
        return None
    setup_quality = dict(setup_quality)
    setup_quality["diagnostic_reconstructed"] = True
    setup_quality["diagnostic_reconstruction_source"] = TECHNICAL_DAILY_TABLE
    setup_quality["diagnostic_reconstruction_asof_date"] = json_safe_value(row.get("technical_feature_asof_date"))
    setup_quality["diagnostic_reconstruction_note"] = (
        "Rebuilt by technical-threshold calibration from point-in-time technical features because the candidate row lacked a native blocker contract."
    )
    return json_dumps(setup_quality)


def reconstruct_missing_blocker_contracts(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    if df.empty or "technical_setup_quality_json" not in df.columns:
        return df, {"candidate_rows": int(len(df)), "reconstructed_rows": 0, "native_contract_rows": 0}
    out = df.copy()
    native_mask = out["technical_setup_quality_json"].map(_has_blocker_contract)
    reconstructed_count = 0
    for idx, row in out.loc[~native_mask].iterrows():
        reconstructed = _reconstruct_technical_setup_quality(row)
        if reconstructed:
            out.at[idx, "technical_setup_quality_json"] = reconstructed
            reconstructed_count += 1
    out["technical_blocker_contract_source"] = "missing"
    out.loc[native_mask, "technical_blocker_contract_source"] = "native"
    if reconstructed_count:
        reconstructed_mask = out["technical_setup_quality_json"].map(_has_blocker_contract) & ~native_mask
        out.loc[reconstructed_mask, "technical_blocker_contract_source"] = "reconstructed"
    return out, {
        "candidate_rows": int(len(out)),
        "native_contract_rows": int(native_mask.sum()),
        "reconstructed_rows": int(reconstructed_count),
        "missing_contract_rows": int(max(0, len(out) - int(native_mask.sum()) - int(reconstructed_count))),
        "source": TECHNICAL_DAILY_TABLE,
        "authority_scope": "research_only",
        "action_policy_effect": "diagnostic_contract_reconstruction_only_no_threshold_change",
        "broker_execution_allowed": False,
    }


def load_point_in_time_technical_features_for_signals(signals: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Attach latest available technical feature row at or before each signal date.

    This intentionally avoids per-candidate lateral DB lookups. The calibration
    path can have many historical candidates, so we do one bounded feature read
    and merge in memory while preserving point-in-time discipline.
    """
    base_meta = {
        "source": TECHNICAL_DAILY_TABLE,
        "candidate_rows": int(len(signals)),
        "technical_feature_rows": 0,
        "technical_feature_coverage_count": 0,
        "missing_technical_feature_count": int(len(signals)),
        "authority_scope": "research_only",
        "action_policy_effect": "feature_context_for_diagnostic_reconstruction_only",
        "broker_execution_allowed": False,
    }
    if signals.empty:
        return signals, {**base_meta, "status": "empty_candidates"}

    technical_columns = table_columns(TECHNICAL_DAILY_TABLE)
    feature_cols = list(TECHNICAL_RECONSTRUCTION_COLUMNS.keys())
    out = signals.copy()
    for column in ["technical_feature_asof_date", "technical_feature_load_ts", *feature_cols]:
        if column not in out.columns:
            out[column] = pd.NA
    if not technical_columns:
        return out, {**base_meta, "status": "technical_table_unavailable_or_empty_schema"}

    candidate_dates = pd.to_datetime(out["asof_date"], utc=True, errors="coerce").dropna()
    symbols = out["symbol"].dropna().astype(str).str.strip().str.upper().drop_duplicates().tolist()
    if candidate_dates.empty or not symbols:
        return out, {**base_meta, "status": "missing_candidate_dates_or_symbols"}

    min_date = candidate_dates.min().normalize() - pd.Timedelta(days=45)
    max_date = candidate_dates.max().normalize()
    select_exprs = [
        "UPPER(TRIM(symbol)) AS symbol",
        "asof_date AS technical_feature_asof_date",
        "load_ts AS technical_feature_load_ts" if "load_ts" in technical_columns else "NULL::TIMESTAMPTZ AS technical_feature_load_ts",
    ]
    select_exprs.extend(
        _optional_select_alias("", column, technical_columns, cast=cast).lstrip(".")
        for column, cast in TECHNICAL_RECONSTRUCTION_COLUMNS.items()
    )
    series_filter = ""
    if "series" in technical_columns:
        series_filter = "AND UPPER(TRIM(COALESCE(series, 'daily'))) IN ('DAILY', 'EQ')"

    try:
        features = sql_to_df(
            f"""
            SELECT
                {', '.join(select_exprs)}
            FROM {TECHNICAL_DAILY_TABLE}
            WHERE asof_date >= %s
              AND asof_date <= %s
              AND UPPER(TRIM(symbol)) = ANY(%s)
              {series_filter}
            ORDER BY symbol, asof_date
            """,
            params=(min_date, max_date, symbols),
            retries=4,
            statement_timeout_ms=0,
            chunksize=100000,
        )
    except Exception as exc:
        _record_technical_threshold_calibration_fallback(
            fallback_type="technical_threshold_calibration_feature_rows_load_failed",
            source=TECHNICAL_DAILY_TABLE,
            reason="Technical-threshold calibration could not load point-in-time technical features for diagnostic reconstruction.",
            error=exc,
            metadata={
                "symbol_count": len(symbols),
                "from_date": str(min_date),
                "to_date": str(max_date),
            },
        )
        return out, {**base_meta, "status": "technical_feature_load_failed", "error": str(exc)}

    if features.empty:
        return out, {
            **base_meta,
            "status": "no_matching_technical_features",
            "from_date": str(min_date),
            "to_date": str(max_date),
            "symbol_count": len(symbols),
        }

    features = features.copy()
    features["symbol"] = features["symbol"].astype("string").str.strip().str.upper()
    features["technical_feature_asof_date"] = pd.to_datetime(
        features["technical_feature_asof_date"], utc=True, errors="coerce"
    ).dt.normalize()
    features["technical_feature_load_ts"] = pd.to_datetime(
        features.get("technical_feature_load_ts"), utc=True, errors="coerce"
    )
    features = features.dropna(subset=["symbol", "technical_feature_asof_date"])
    if features.empty:
        return out, {**base_meta, "status": "technical_features_invalid_dates"}

    features = features.sort_values(["symbol", "technical_feature_asof_date", "technical_feature_load_ts"], kind="stable")
    features = features.drop_duplicates(subset=["symbol", "technical_feature_asof_date"], keep="last")

    out = out.reset_index(drop=True)
    out["_candidate_row_id"] = range(len(out))
    out["asof_date"] = pd.to_datetime(out["asof_date"], utc=True, errors="coerce").dt.normalize()
    merge_cols = ["technical_feature_asof_date", "technical_feature_load_ts", *feature_cols]
    for symbol, candidate_group in out.groupby("symbol", dropna=False):
        symbol_text = str(symbol or "").strip().upper()
        tech_group = features[features["symbol"] == symbol_text]
        if tech_group.empty:
            continue
        candidate_group = candidate_group.sort_values("asof_date", kind="stable")
        tech_group = tech_group.sort_values("technical_feature_asof_date", kind="stable")
        tech_select_cols = list(dict.fromkeys(["technical_feature_asof_date", "technical_feature_load_ts", *feature_cols]))
        merged = pd.merge_asof(
            candidate_group[["_candidate_row_id", "asof_date"]],
            tech_group[tech_select_cols],
            left_on="asof_date",
            right_on="technical_feature_asof_date",
            direction="backward",
        )
        for column in merge_cols:
            out.loc[merged["_candidate_row_id"].to_numpy(), column] = merged[column].to_numpy()

    enriched = out.sort_values("_candidate_row_id", kind="stable").drop(columns=["_candidate_row_id"], errors="ignore").reset_index(drop=True)
    coverage = int(pd.to_datetime(enriched.get("technical_feature_asof_date"), utc=True, errors="coerce").notna().sum())
    return enriched, {
        **base_meta,
        "status": "ok",
        "technical_feature_rows": int(len(features)),
        "technical_feature_coverage_count": coverage,
        "missing_technical_feature_count": int(max(0, len(enriched) - coverage)),
        "from_date": str(min_date),
        "to_date": str(max_date),
        "symbol_count": len(symbols),
    }


def load_technical_signal_rows(
    *,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
) -> pd.DataFrame:
    available = table_columns("advisory_candidates", raise_on_error=True)
    if not available:
        return pd.DataFrame()
    score_source_col = "technical_total_score" if "technical_total_score" in available else "technical_score" if "technical_score" in available else None
    if score_source_col is None:
        return pd.DataFrame()
    clauses = [f"c.{score_source_col} IS NOT NULL", "NULLIF(TRIM(c.symbol), '') IS NOT NULL"]
    params: list[Any] = []
    if from_date is not None:
        clauses.append("c.asof_date >= %s")
        params.append(from_date)
    if to_date is not None:
        clauses.append("c.asof_date <= %s")
        params.append(to_date)
    if symbols:
        clauses.append("UPPER(TRIM(c.symbol)) = ANY(%s)")
        params.append([str(value).upper() for value in symbols])
    select_exprs = ["c.asof_date", "c.setup_id", "c.symbol"]
    for col in OPTIONAL_SIGNAL_COLUMNS:
        if col in {"technical_total_score", "technical_score"}:
            if col in available:
                select_exprs.append(f"c.{col}")
            continue
        select_exprs.append(f"c.{col}" if col in available else f"NULL AS {col}")
    if "technical_score" not in available:
        select_exprs.append("NULL::double precision AS technical_score")
    if "technical_total_score" not in available:
        if "technical_score" in available:
            select_exprs.append("(c.technical_score * 100.0) AS technical_total_score")
        else:
            select_exprs.append("NULL::double precision AS technical_total_score")
    try:
        df = sql_to_df(
            f"""
            SELECT
                {', '.join(select_exprs)}
            FROM advisory_candidates c
            WHERE {' AND '.join(clauses)}
            ORDER BY c.asof_date, c.symbol, c.setup_id
            """,
            params=tuple(params) if params else None,
            retries=4,
            statement_timeout_ms=0,
            chunksize=50000,
        )
    except Exception as exc:
        _record_technical_threshold_calibration_fallback(
            fallback_type="technical_threshold_calibration_signal_rows_load_failed",
            source="advisory_candidates",
            reason="Technical-threshold calibration could not load technical signal rows.",
            error=exc,
            metadata={
                "from_date": str(from_date) if from_date is not None else None,
                "to_date": str(to_date) if to_date is not None else None,
                "symbol_count": len(symbols or []),
            },
        )
        raise SourceUnavailableError(f"Technical-threshold calibration could not load technical signal rows: {exc}") from exc
    if df.empty:
        return df
    df["asof_date"] = pd.to_datetime(df["asof_date"], utc=True, errors="coerce").dt.normalize()
    df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()
    numeric_cols = [
        "technical_score",
        "technical_trend_score",
        "technical_structure_score",
        "technical_participation_score",
        "technical_relative_strength_score",
        "technical_tradability_score",
        "technical_total_score",
        "setup_score",
    ]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if "technical_setup_archetype" in df.columns:
        df["technical_setup_archetype"] = df["technical_setup_archetype"].astype("string").str.strip().str.lower()
    df["technical_setup_archetype_effective"] = df.apply(infer_calibration_setup_archetype, axis=1)
    df = df.dropna(subset=["asof_date", "symbol", "technical_total_score"])
    df, feature_meta = load_point_in_time_technical_features_for_signals(df)
    df, reconstruction_meta = reconstruct_missing_blocker_contracts(df)
    df.attrs["technical_feature_join"] = feature_meta
    df.attrs["technical_blocker_contract_reconstruction"] = reconstruction_meta
    return df


def load_price_history_for_returns(
    *,
    symbols: list[str],
    from_date: pd.Timestamp,
    to_date: pd.Timestamp,
) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()
    normalized_symbols = [str(value).upper() for value in symbols]
    try:
        df = sql_to_df(
            """
            SELECT ticker AS symbol, date, close
            FROM dhan_ohlcv_daily
            WHERE asset_type = 'stock'
              AND exchange = 'NSE'
              AND ticker = ANY(%s)
              AND date >= %s
              AND date <= %s
            ORDER BY ticker, date
            """,
            params=(normalized_symbols, from_date, to_date),
            retries=4,
            statement_timeout_ms=0,
            chunksize=100000,
        )
    except Exception as exc:
        _record_technical_threshold_calibration_fallback(
            fallback_type="technical_threshold_calibration_price_history_load_failed",
            source="dhan_ohlcv_daily",
            reason="Technical-threshold calibration could not load Dhan OHLCV history for realized returns.",
            error=exc,
            metadata={
                "symbol_count": len(normalized_symbols),
                "from_date": str(from_date),
                "to_date": str(to_date),
            },
        )
        raise SourceUnavailableError(f"Technical-threshold calibration could not load Dhan OHLCV history for realized returns: {exc}") from exc
    if df.empty:
        return df
    df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()
    df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce").dt.normalize()
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df.dropna(subset=["symbol", "date", "close"])


def attach_forward_returns(signals: pd.DataFrame, prices: pd.DataFrame, *, horizons: list[int]) -> pd.DataFrame:
    if signals.empty or prices.empty:
        return signals.copy()
    out = signals.copy().reset_index(drop=True)
    out["point_in_time_return_contract_json"] = json.dumps(
        {
            "entry_rule": "Use first available close strictly after signal asof_date.",
            "exit_rule": "Use the horizon-th available close from the same strictly-after-asof price window.",
            "same_day_price_allowed": False,
            "forward_returns_are_labels_only": True,
        },
        sort_keys=True,
    )
    price_map = {
        symbol: group.sort_values("date").reset_index(drop=True)
        for symbol, group in prices.groupby("symbol", dropna=False)
    }
    for horizon in horizons:
        out[f"entry_date_h{horizon}"] = pd.Series([None] * len(out), dtype="object")
        out[f"exit_date_h{horizon}"] = pd.Series([None] * len(out), dtype="object")
        out[f"entry_close_h{horizon}"] = pd.NA
        out[f"exit_close_h{horizon}"] = pd.NA
        out[f"forward_return_h{horizon}"] = pd.NA

    for idx, row in out.iterrows():
        history = price_map.get(str(row.get("symbol") or "").upper())
        if history is None or history.empty:
            continue
        asof_date = pd.to_datetime(row.get("asof_date"), utc=True, errors="coerce")
        if pd.isna(asof_date):
            continue
        eligible = history[history["date"] > asof_date.normalize()].reset_index(drop=True)
        if eligible.empty:
            continue
        for horizon in horizons:
            exit_idx = int(horizon) - 1
            if len(eligible) <= exit_idx:
                continue
            entry = eligible.iloc[0]
            exit_row = eligible.iloc[exit_idx]
            entry_close = pd.to_numeric(entry.get("close"), errors="coerce")
            exit_close = pd.to_numeric(exit_row.get("close"), errors="coerce")
            if pd.isna(entry_close) or pd.isna(exit_close) or float(entry_close) <= 0:
                continue
            out.at[idx, f"entry_date_h{horizon}"] = entry["date"]
            out.at[idx, f"exit_date_h{horizon}"] = exit_row["date"]
            out.at[idx, f"entry_close_h{horizon}"] = float(entry_close)
            out.at[idx, f"exit_close_h{horizon}"] = float(exit_close)
            out.at[idx, f"forward_return_h{horizon}"] = (float(exit_close) / float(entry_close)) - 1.0
    return out


def build_threshold_grid(grid: dict[str, list[float]] | None = None) -> list[dict[str, float]]:
    raw = grid or DEFAULT_GRID
    keys = list(raw.keys())
    configs: list[dict[str, float]] = []
    for values in itertools.product(*(raw[key] for key in keys)):
        cfg = {key: float(value) for key, value in zip(keys, values, strict=True)}
        if cfg["buy_total_min"] < cfg["ready_total_min"]:
            continue
        configs.append(cfg)
    return configs


def _eligible_mask(dataset: pd.DataFrame, config: dict[str, float]) -> pd.Series:
    mask = pd.Series(True, index=dataset.index)
    for threshold_key, column in SCORE_COLUMNS.items():
        if column not in dataset.columns:
            continue
        values = pd.to_numeric(dataset[column], errors="coerce")
        if values.notna().sum() == 0:
            continue
        mask &= values.ge(float(config[threshold_key]))
    trigger = dataset.get("technical_trigger_type", pd.Series("", index=dataset.index)).astype("string").fillna("")
    state = dataset.get("technical_state", pd.Series("", index=dataset.index)).astype("string").fillna("").str.upper()
    has_trigger_context = bool(trigger.ne("").any() or state.eq("BUY_TRIGGERED").any())
    if has_trigger_context:
        mask &= trigger.ne("") | state.eq("BUY_TRIGGERED")
    return mask.fillna(False)


def evaluate_threshold_config(
    dataset: pd.DataFrame,
    config: dict[str, float],
    *,
    horizon_days: int,
    return_threshold: float = DEFAULT_RETURN_THRESHOLD,
    cost_bps: float = DEFAULT_COST_BPS,
    min_signals: int = DEFAULT_MIN_SIGNALS,
    evaluated_at: pd.Timestamp | None = None,
) -> dict[str, Any]:
    return_col = f"forward_return_h{int(horizon_days)}"
    matured = dataset[return_col].notna() if return_col in dataset.columns else pd.Series(False, index=dataset.index)
    sample = dataset[matured].copy()
    signal_count = int(len(sample))
    eligible = _eligible_mask(sample, config) if not sample.empty else pd.Series(False, index=sample.index)
    selected = sample[eligible]
    rejected = sample[~eligible]
    cost = float(cost_bps) / 10000.0
    selected_returns = pd.to_numeric(selected.get(return_col), errors="coerce").dropna()
    rejected_returns = pd.to_numeric(rejected.get(return_col), errors="coerce").dropna()
    after_cost = selected_returns - cost
    hit_after_cost = after_cost.ge(float(return_threshold))
    eligible_count = int(len(selected_returns))
    avg_after_cost = float(after_cost.mean()) if not after_cost.empty else None
    hit_rate_after_cost = float(hit_after_cost.mean()) if not hit_after_cost.empty else None
    avg_rejected = float((rejected_returns - cost).mean()) if not rejected_returns.empty else None
    spread = None if avg_after_cost is None or avg_rejected is None else float(avg_after_cost - avg_rejected)
    sample_start = sample["asof_date"].min() if not sample.empty else pd.NaT
    sample_end = sample["asof_date"].max() if not sample.empty else pd.NaT
    objective = None
    if eligible_count >= int(min_signals) and avg_after_cost is not None and hit_rate_after_cost is not None:
        objective = float((avg_after_cost * 100.0) + (hit_rate_after_cost * 0.50) + min(eligible_count, 200) / 1000.0)
        if spread is not None:
            objective += float(spread * 25.0)
    config_id = (
        f"h{int(horizon_days)}_t{config['trend_min']:.0f}_s{config['structure_min']:.0f}_p{config['participation_min']:.0f}_"
        f"rs{config['relative_strength_min']:.0f}_tr{config['tradability_min']:.0f}_r{config['ready_total_min']:.0f}_b{config['buy_total_min']:.0f}"
    )
    return {
        "evaluated_at": pd.to_datetime(evaluated_at or pd.Timestamp.utcnow(), utc=True, errors="coerce"),
        "config_id": config_id,
        "horizon_days": int(horizon_days),
        "threshold_config_json": json_dumps(config),
        "signal_count": signal_count,
        "eligible_count": eligible_count,
        "trade_rate": None if signal_count == 0 else eligible_count / signal_count,
        "avg_forward_return": float(selected_returns.mean()) if not selected_returns.empty else None,
        "median_forward_return": float(selected_returns.median()) if not selected_returns.empty else None,
        "hit_rate": float(selected_returns.ge(float(return_threshold)).mean()) if not selected_returns.empty else None,
        "avg_forward_return_after_cost": avg_after_cost,
        "hit_rate_after_cost": hit_rate_after_cost,
        "avg_rejected_forward_return": avg_rejected,
        "spread_vs_rejected": spread,
        "objective_score": objective,
        "sample_start": sample_start,
        "sample_end": sample_end,
        "load_ts": pd.Timestamp.utcnow(),
    }


def build_archetype_breakdown(
    dataset: pd.DataFrame,
    config: dict[str, float],
    *,
    horizon_days: int,
    return_threshold: float = DEFAULT_RETURN_THRESHOLD,
    cost_bps: float = DEFAULT_COST_BPS,
) -> list[dict[str, Any]]:
    return_col = f"forward_return_h{int(horizon_days)}"
    if dataset.empty or return_col not in dataset.columns:
        return []
    sample = dataset[dataset[return_col].notna()].copy()
    if sample.empty:
        return []
    if "technical_setup_archetype" not in sample.columns:
        sample["technical_setup_archetype"] = "unknown"
    if "technical_setup_archetype_effective" not in sample.columns:
        sample["technical_setup_archetype_effective"] = sample.apply(infer_calibration_setup_archetype, axis=1)
    sample["technical_setup_archetype"] = sample["technical_setup_archetype"].astype("string").fillna("unknown").str.strip().str.lower()
    sample.loc[sample["technical_setup_archetype"].eq(""), "technical_setup_archetype"] = "unknown"
    sample["technical_setup_archetype_effective"] = (
        sample["technical_setup_archetype_effective"].astype("string").fillna("unknown").str.strip().str.lower()
    )
    sample.loc[sample["technical_setup_archetype_effective"].eq(""), "technical_setup_archetype_effective"] = "unknown"
    eligible = _eligible_mask(sample, config)
    sample["_selected"] = eligible.reindex(sample.index, fill_value=False).astype(bool)
    sample["_return_after_cost"] = pd.to_numeric(sample[return_col], errors="coerce") - (float(cost_bps) / 10000.0)
    rows: list[dict[str, Any]] = []
    for archetype, group in sample.groupby("technical_setup_archetype_effective", dropna=False):
        selected = group[group["_selected"]]
        selected_returns = pd.to_numeric(selected["_return_after_cost"], errors="coerce").dropna()
        all_returns = pd.to_numeric(group["_return_after_cost"], errors="coerce").dropna()
        explicit_values = {
            value
            for value in group["technical_setup_archetype"].astype("string").fillna("unknown").str.strip().str.lower().tolist()
            if value and value not in {"unknown", "none", "nan", "null"}
        }
        rows.append(
            {
                "technical_setup_archetype": str(archetype or "unknown"),
                "archetype_source": "explicit" if explicit_values else "inferred",
                "explicit_archetype_values": sorted(explicit_values)[:8],
                "signal_count": int(len(group)),
                "selected_count": int(len(selected_returns)),
                "rejected_count": int(max(0, len(group) - len(selected))),
                "selected_rate": None if len(group) == 0 else round(float(len(selected_returns) / len(group)), 6),
                "avg_selected_return_after_cost": None if selected_returns.empty else round(float(selected_returns.mean()), 6),
                "hit_rate_after_cost": None if selected_returns.empty else round(float(selected_returns.ge(float(return_threshold)).mean()), 6),
                "avg_all_return_after_cost": None if all_returns.empty else round(float(all_returns.mean()), 6),
            }
        )
    rows.sort(
        key=lambda row: (
            int(row.get("selected_count") or 0),
            float(row.get("avg_selected_return_after_cost") or -999.0),
            int(row.get("signal_count") or 0),
        ),
        reverse=True,
    )
    return rows


def _parse_setup_quality(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    if pd.api.types.is_scalar(value) and pd.isna(value):
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _iter_blocker_rows(row: pd.Series) -> list[dict[str, Any]]:
    quality = _parse_setup_quality(row.get("technical_setup_quality_json"))
    out: list[dict[str, Any]] = []
    trigger_blockers = quality.get("entry_trigger_blockers")
    if not isinstance(trigger_blockers, list):
        trigger_diagnostics = quality.get("entry_trigger_diagnostics")
        if isinstance(trigger_diagnostics, dict):
            trigger_blockers = trigger_diagnostics.get("nearest_trigger_blockers")
    if isinstance(trigger_blockers, list):
        for blocker in trigger_blockers:
            if not isinstance(blocker, dict):
                continue
            out.append(
                {
                    "blocker_family": "entry_trigger",
                    "blocker_code": _clean_text(blocker.get("code")) or "missing",
                    "blocker_category": _clean_text(blocker.get("archetype")) or "missing",
                    "blocker_metric": _clean_text(blocker.get("metric")) or "missing",
                }
            )
    buy_readiness = quality.get("buy_readiness")
    buy_blockers = buy_readiness.get("blockers") if isinstance(buy_readiness, dict) else None
    if isinstance(buy_blockers, list):
        for blocker in buy_blockers:
            if not isinstance(blocker, dict):
                continue
            out.append(
                {
                    "blocker_family": "buy_readiness",
                    "blocker_code": _clean_text(blocker.get("code")) or "missing",
                    "blocker_category": _clean_text(blocker.get("category")) or "missing",
                    "blocker_metric": _clean_text(blocker.get("metric")) or "missing",
                }
            )
    return out


def _numeric_near_miss(blocker: dict[str, Any], *, relative_tolerance: float = 0.25, absolute_tolerance: float = 0.75) -> tuple[bool, float | None]:
    current = pd.to_numeric(blocker.get("current"), errors="coerce")
    threshold = pd.to_numeric(blocker.get("threshold"), errors="coerce")
    if pd.isna(current) or pd.isna(threshold):
        return False, None
    current_f = float(current)
    threshold_f = float(threshold)
    operator = str(blocker.get("operator") or "").strip().lower()
    scale = max(abs(threshold_f), 1.0)
    if operator == "gte":
        gap = threshold_f - current_f
        if gap <= 0:
            return True, 0.0
        return bool(gap <= max(float(absolute_tolerance), scale * float(relative_tolerance))), round(float(gap), 6)
    if operator == "lte":
        gap = current_f - threshold_f
        if gap <= 0:
            return True, 0.0
        return bool(gap <= max(float(absolute_tolerance), scale * float(relative_tolerance))), round(float(gap), 6)
    if operator == "abs_lte":
        gap = abs(current_f) - threshold_f
        if gap <= 0:
            return True, 0.0
        return bool(gap <= max(float(absolute_tolerance), scale * float(relative_tolerance))), round(float(gap), 6)
    return False, None


def _iter_near_miss_trigger_rows(row: pd.Series) -> list[dict[str, Any]]:
    quality = _parse_setup_quality(row.get("technical_setup_quality_json"))
    trigger_diagnostics = quality.get("entry_trigger_diagnostics")
    if not isinstance(trigger_diagnostics, dict):
        return []
    all_blockers = trigger_diagnostics.get("all_trigger_blockers")
    if not isinstance(all_blockers, dict):
        blockers = trigger_diagnostics.get("nearest_trigger_blockers")
        all_blockers = {trigger_diagnostics.get("nearest_trigger_type") or "unknown": blockers}
    out: list[dict[str, Any]] = []
    for archetype, blockers in all_blockers.items():
        if not isinstance(blockers, list) or not blockers:
            continue
        normalized: list[dict[str, Any]] = [item for item in blockers if isinstance(item, dict)]
        if not normalized or len(normalized) > 2:
            continue
        near_miss_items: list[dict[str, Any]] = []
        all_near = True
        for blocker in normalized:
            is_near, gap = _numeric_near_miss(blocker)
            if not is_near:
                all_near = False
                break
            near_miss_items.append(
                {
                    "blocker_code": _clean_text(blocker.get("code")) or "missing",
                    "blocker_metric": _clean_text(blocker.get("metric")) or "missing",
                    "current": blocker.get("current"),
                    "threshold": blocker.get("threshold"),
                    "operator": blocker.get("operator"),
                    "gap_to_threshold": gap,
                }
            )
        if not all_near:
            continue
        blocker_codes = sorted({str(item.get("blocker_code") or "missing") for item in near_miss_items})
        out.append(
            {
                "near_miss_archetype": str(archetype or "unknown"),
                "near_miss_blocker_count": len(near_miss_items),
                "near_miss_blocker_codes": blocker_codes,
                "near_miss_blocker_key": "|".join(blocker_codes) if blocker_codes else "none",
                "near_miss_blockers": near_miss_items,
            }
        )
    return out


def build_trigger_near_miss_breakdown(
    dataset: pd.DataFrame,
    config: dict[str, float],
    *,
    horizon_days: int,
    return_threshold: float = DEFAULT_RETURN_THRESHOLD,
    cost_bps: float = DEFAULT_COST_BPS,
) -> list[dict[str, Any]]:
    """Outcome summary for rows that narrowly missed an entry-trigger archetype.

    This is research-only. It identifies candidates with one or two numeric
    trigger blockers that were close to threshold, then measures realised
    after-cost outcomes. It does not change production trigger confirmation.
    """
    return_col = f"forward_return_h{int(horizon_days)}"
    if dataset.empty or return_col not in dataset.columns or "technical_setup_quality_json" not in dataset.columns:
        return []
    sample = dataset[dataset[return_col].notna()].copy()
    if sample.empty:
        return []
    eligible = _eligible_mask(sample, config)
    sample["_selected"] = eligible.reindex(sample.index, fill_value=False).astype(bool)
    sample["_return_after_cost"] = pd.to_numeric(sample[return_col], errors="coerce") - (float(cost_bps) / 10000.0)
    rows: list[dict[str, Any]] = []
    for idx, row in sample.iterrows():
        if bool(sample.at[idx, "_selected"]):
            continue
        for near_miss in _iter_near_miss_trigger_rows(row):
            rows.append(
                {
                    **near_miss,
                    "_return_after_cost": sample.at[idx, "_return_after_cost"],
                }
            )
    if not rows:
        return []
    frame = pd.DataFrame(rows)
    out: list[dict[str, Any]] = []
    for key, group in frame.groupby(["near_miss_archetype", "near_miss_blocker_key"], dropna=False):
        returns = pd.to_numeric(group["_return_after_cost"], errors="coerce").dropna()
        sample_blockers = group.iloc[0].get("near_miss_blockers")
        out.append(
            {
                "near_miss_archetype": str(key[0] or "unknown"),
                "near_miss_blocker_key": str(key[1] or "none"),
                "near_miss_blocker_codes": str(key[1] or "").split("|") if str(key[1] or "") else [],
                "signal_count": int(len(group)),
                "avg_return_after_cost": None if returns.empty else round(float(returns.mean()), 6),
                "hit_rate_after_cost": None if returns.empty else round(float(returns.ge(float(return_threshold)).mean()), 6),
                "sample_blockers": sample_blockers if isinstance(sample_blockers, list) else [],
                "authority_scope": "research_only",
                "action_policy_effect": "near_miss_diagnostic_only_no_live_trigger_change",
                "broker_execution_allowed": False,
            }
        )
    out.sort(
        key=lambda row: (
            int(row.get("signal_count") or 0),
            float(row.get("avg_return_after_cost") or -999.0),
        ),
        reverse=True,
    )
    return out


def build_blocker_breakdown(
    dataset: pd.DataFrame,
    config: dict[str, float],
    *,
    horizon_days: int,
    return_threshold: float = DEFAULT_RETURN_THRESHOLD,
    cost_bps: float = DEFAULT_COST_BPS,
) -> list[dict[str, Any]]:
    """Outcome summary by diagnostic blocker, under the candidate threshold config.

    Blockers are explanatory metadata from the production technical engine. They do
    not change calibration eligibility here; this report answers whether rows
    currently blocked by a specific reason would have helped after costs.
    """
    return_col = f"forward_return_h{int(horizon_days)}"
    if dataset.empty or return_col not in dataset.columns or "technical_setup_quality_json" not in dataset.columns:
        return []
    sample = dataset[dataset[return_col].notna()].copy()
    if sample.empty:
        return []
    eligible = _eligible_mask(sample, config)
    sample["_selected"] = eligible.reindex(sample.index, fill_value=False).astype(bool)
    sample["_return_after_cost"] = pd.to_numeric(sample[return_col], errors="coerce") - (float(cost_bps) / 10000.0)
    rows: list[dict[str, Any]] = []
    for idx, row in sample.iterrows():
        for blocker in _iter_blocker_rows(row):
            rows.append(
                {
                    **blocker,
                    "_selected": bool(sample.at[idx, "_selected"]),
                    "_return_after_cost": sample.at[idx, "_return_after_cost"],
                }
            )
    if not rows:
        return []
    blockers = pd.DataFrame(rows)
    out: list[dict[str, Any]] = []
    for key, group in blockers.groupby(["blocker_family", "blocker_code", "blocker_category", "blocker_metric"], dropna=False):
        selected = group[group["_selected"]]
        selected_returns = pd.to_numeric(selected["_return_after_cost"], errors="coerce").dropna()
        all_returns = pd.to_numeric(group["_return_after_cost"], errors="coerce").dropna()
        out.append(
            {
                "blocker_family": str(key[0] or "missing"),
                "blocker_code": str(key[1] or "missing"),
                "blocker_category": str(key[2] or "missing"),
                "blocker_metric": str(key[3] or "missing"),
                "signal_count": int(len(group)),
                "selected_count": int(len(selected_returns)),
                "selected_rate": None if len(group) == 0 else round(float(len(selected_returns) / len(group)), 6),
                "avg_selected_return_after_cost": None if selected_returns.empty else round(float(selected_returns.mean()), 6),
                "hit_rate_selected_after_cost": None if selected_returns.empty else round(float(selected_returns.ge(float(return_threshold)).mean()), 6),
                "avg_all_return_after_cost": None if all_returns.empty else round(float(all_returns.mean()), 6),
                "hit_rate_all_after_cost": None if all_returns.empty else round(float(all_returns.ge(float(return_threshold)).mean()), 6),
            }
        )
    out.sort(
        key=lambda row: (
            int(row.get("selected_count") or 0),
            int(row.get("signal_count") or 0),
            float(row.get("avg_selected_return_after_cost") or -999.0),
            float(row.get("avg_all_return_after_cost") or -999.0),
        ),
        reverse=True,
    )
    return out


def build_summary(
    evaluations: pd.DataFrame,
    *,
    evaluated_at: pd.Timestamp,
    dataset: pd.DataFrame | None = None,
    return_threshold: float = DEFAULT_RETURN_THRESHOLD,
    cost_bps: float = DEFAULT_COST_BPS,
) -> pd.DataFrame:
    if evaluations.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for horizon, group in evaluations.groupby("horizon_days", dropna=False):
        ranked = group.dropna(subset=["objective_score"]).sort_values(
            ["objective_score", "eligible_count", "hit_rate_after_cost"],
            ascending=[False, False, False],
            kind="stable",
        )
        best = ranked.iloc[0] if not ranked.empty else group.sort_values("eligible_count", ascending=False).iloc[0]
        baseline = group.sort_values("eligible_count", ascending=False).iloc[0]
        recommendation = (
            "review_for_promotion"
            if pd.notna(best.get("objective_score")) and int(best.get("eligible_count") or 0) >= DEFAULT_MIN_SIGNALS
            else "insufficient_matured_signals"
        )
        archetype_breakdown = []
        blocker_breakdown = []
        trigger_near_miss_breakdown = []
        if isinstance(dataset, pd.DataFrame) and not dataset.empty:
            try:
                best_config = json.loads(str(best.get("threshold_config_json") or "{}"))
                archetype_breakdown = build_archetype_breakdown(
                    dataset,
                    best_config,
                    horizon_days=int(horizon),
                    return_threshold=return_threshold,
                    cost_bps=cost_bps,
                )
                blocker_breakdown = build_blocker_breakdown(
                    dataset,
                    best_config,
                    horizon_days=int(horizon),
                    return_threshold=return_threshold,
                    cost_bps=cost_bps,
                )
                trigger_near_miss_breakdown = build_trigger_near_miss_breakdown(
                    dataset,
                    best_config,
                    horizon_days=int(horizon),
                    return_threshold=return_threshold,
                    cost_bps=cost_bps,
                )
            except Exception as exc:
                _record_technical_threshold_calibration_fallback(
                    fallback_type="technical_threshold_calibration_archetype_breakdown_failed",
                    source="advisory_candidates",
                    reason="Technical-threshold calibration could not build archetype/blocker/near-miss breakdown for summary.",
                    error=exc,
                    metadata={"horizon_days": int(horizon), "best_config_id": str(best.get("config_id") or "")},
                )
        rows.append(
            {
                "evaluated_at": evaluated_at,
                "horizon_days": int(horizon),
                "best_config_id": best.get("config_id"),
                "best_threshold_config_json": best.get("threshold_config_json"),
                "best_objective_score": best.get("objective_score"),
                "best_eligible_count": best.get("eligible_count"),
                "best_hit_rate_after_cost": best.get("hit_rate_after_cost"),
                "best_avg_forward_return_after_cost": best.get("avg_forward_return_after_cost"),
                "baseline_signal_count": baseline.get("eligible_count"),
                "baseline_avg_forward_return_after_cost": baseline.get("avg_forward_return_after_cost"),
                "baseline_hit_rate_after_cost": baseline.get("hit_rate_after_cost"),
                "archetype_breakdown_json": json_dumps(
                    {
                        "schema_version": 1,
                        "horizon_days": int(horizon),
                        "best_config_id": best.get("config_id"),
                        "authority_scope": "research_only",
                        "action_policy_effect": "no_live_policy_change",
                        "broker_execution_allowed": False,
                        "breakdown": archetype_breakdown,
                        "blocker_breakdown": blocker_breakdown,
                        "trigger_near_miss_breakdown": trigger_near_miss_breakdown,
                    }
                ),
                "recommendation": recommendation,
                "load_ts": pd.Timestamp.utcnow(),
            }
        )
    return pd.DataFrame(rows)


def summarize_blocker_research(
    summary: pd.DataFrame,
    *,
    min_signals: int = DEFAULT_MIN_SIGNALS,
    return_threshold: float = DEFAULT_RETURN_THRESHOLD,
    min_hit_rate: float = 0.5,
) -> dict[str, Any]:
    """Compact cross-horizon readout of blocker outcome evidence.

    This is intentionally research-only. It does not recommend applying a
    threshold change; it classifies which blocker families deserve deeper
    calibration review and which should not be relaxed based on realised
    after-cost outcomes.
    """

    authority = {
        "authority_scope": "research_only",
        "action_policy_effect": "summary_only_no_live_threshold_change",
        "portfolio_authority": "none",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
    }
    if summary.empty or "archetype_breakdown_json" not in summary.columns:
        return {
            "status": "no_summary_rows",
            **authority,
            "blocker_rows": 0,
            "top_relaxation_candidates": [],
            "top_do_not_relax": [],
            "needs_more_label_blockers": [],
        }

    buckets: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for _, item in summary.iterrows():
        payload = _json_loads(item.get("archetype_breakdown_json"), {})
        if not isinstance(payload, dict):
            continue
        horizon = int(item.get("horizon_days") or payload.get("horizon_days") or 0)
        for row in payload.get("blocker_breakdown") or []:
            if not isinstance(row, dict):
                continue
            key = (
                str(row.get("blocker_family") or "missing"),
                str(row.get("blocker_code") or "missing"),
                str(row.get("blocker_category") or "missing"),
                str(row.get("blocker_metric") or "missing"),
            )
            bucket = buckets.setdefault(
                key,
                {
                    "blocker_family": key[0],
                    "blocker_code": key[1],
                    "blocker_category": key[2],
                    "blocker_metric": key[3],
                    "horizons": [],
                    "signal_count": 0,
                    "selected_count": 0,
                    "_all_return_weighted_sum": 0.0,
                    "_all_return_weight": 0,
                    "_all_hit_weighted_sum": 0.0,
                    "_all_hit_weight": 0,
                    "_selected_return_weighted_sum": 0.0,
                    "_selected_return_weight": 0,
                    "_selected_hit_weighted_sum": 0.0,
                    "_selected_hit_weight": 0,
                },
            )
            if horizon and horizon not in bucket["horizons"]:
                bucket["horizons"].append(horizon)
            signal_count = int(row.get("signal_count") or 0)
            selected_count = int(row.get("selected_count") or 0)
            bucket["signal_count"] += signal_count
            bucket["selected_count"] += selected_count
            avg_all = pd.to_numeric(row.get("avg_all_return_after_cost"), errors="coerce")
            if not pd.isna(avg_all) and signal_count > 0:
                bucket["_all_return_weighted_sum"] += float(avg_all) * signal_count
                bucket["_all_return_weight"] += signal_count
            hit_all = pd.to_numeric(row.get("hit_rate_all_after_cost"), errors="coerce")
            if not pd.isna(hit_all) and signal_count > 0:
                bucket["_all_hit_weighted_sum"] += float(hit_all) * signal_count
                bucket["_all_hit_weight"] += signal_count
            avg_selected = pd.to_numeric(row.get("avg_selected_return_after_cost"), errors="coerce")
            if not pd.isna(avg_selected) and selected_count > 0:
                bucket["_selected_return_weighted_sum"] += float(avg_selected) * selected_count
                bucket["_selected_return_weight"] += selected_count
            hit_selected = pd.to_numeric(row.get("hit_rate_selected_after_cost"), errors="coerce")
            if not pd.isna(hit_selected) and selected_count > 0:
                bucket["_selected_hit_weighted_sum"] += float(hit_selected) * selected_count
                bucket["_selected_hit_weight"] += selected_count

    rows: list[dict[str, Any]] = []
    for bucket in buckets.values():
        avg_all = (
            None
            if int(bucket["_all_return_weight"]) == 0
            else round(float(bucket["_all_return_weighted_sum"] / bucket["_all_return_weight"]), 6)
        )
        hit_all = (
            None
            if int(bucket["_all_hit_weight"]) == 0
            else round(float(bucket["_all_hit_weighted_sum"] / bucket["_all_hit_weight"]), 6)
        )
        avg_selected = (
            None
            if int(bucket["_selected_return_weight"]) == 0
            else round(float(bucket["_selected_return_weighted_sum"] / bucket["_selected_return_weight"]), 6)
        )
        hit_selected = (
            None
            if int(bucket["_selected_hit_weight"]) == 0
            else round(float(bucket["_selected_hit_weighted_sum"] / bucket["_selected_hit_weight"]), 6)
        )
        signal_count = int(bucket["signal_count"])
        if signal_count < int(min_signals) or avg_all is None or hit_all is None:
            classification = "needs_more_matured_labels"
        elif avg_all >= float(return_threshold) and hit_all >= float(min_hit_rate):
            classification = "potential_relaxation_candidate"
        elif avg_all <= 0.0 or hit_all < 0.4:
            classification = "do_not_relax_negative_or_weak"
        else:
            classification = "mixed_or_marginal_requires_review"
        rows.append(
            {
                "blocker_family": bucket["blocker_family"],
                "blocker_code": bucket["blocker_code"],
                "blocker_category": bucket["blocker_category"],
                "blocker_metric": bucket["blocker_metric"],
                "horizons": sorted(bucket["horizons"]),
                "signal_count": signal_count,
                "selected_count": int(bucket["selected_count"]),
                "avg_all_return_after_cost": avg_all,
                "hit_rate_all_after_cost": hit_all,
                "avg_selected_return_after_cost": avg_selected,
                "hit_rate_selected_after_cost": hit_selected,
                "classification": classification,
            }
        )

    rows.sort(
        key=lambda row: (
            row["classification"] == "potential_relaxation_candidate",
            int(row.get("signal_count") or 0),
            float(row.get("avg_all_return_after_cost") or -999.0),
        ),
        reverse=True,
    )
    classification_counts = dict(pd.Series([row["classification"] for row in rows]).value_counts().to_dict()) if rows else {}
    return {
        "status": "ok" if rows else "no_blocker_rows",
        **authority,
        "min_signals": int(min_signals),
        "return_threshold": float(return_threshold),
        "min_hit_rate": float(min_hit_rate),
        "blocker_rows": len(rows),
        "classification_counts": classification_counts,
        "top_relaxation_candidates": [row for row in rows if row["classification"] == "potential_relaxation_candidate"][:10],
        "top_do_not_relax": [row for row in rows if row["classification"] == "do_not_relax_negative_or_weak"][:10],
        "needs_more_label_blockers": [row for row in rows if row["classification"] == "needs_more_matured_labels"][:10],
        "mixed_or_marginal_blockers": [row for row in rows if row["classification"] == "mixed_or_marginal_requires_review"][:10],
    }


def summarize_trigger_near_miss_research(
    summary: pd.DataFrame,
    *,
    min_signals: int = DEFAULT_MIN_SIGNALS,
    return_threshold: float = DEFAULT_RETURN_THRESHOLD,
    min_hit_rate: float = 0.5,
) -> dict[str, Any]:
    """Compact cross-horizon readout for near-miss trigger relaxation research."""

    authority = {
        "authority_scope": "research_only",
        "action_policy_effect": "summary_only_no_live_trigger_change",
        "portfolio_authority": "none",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
    }
    if summary.empty or "archetype_breakdown_json" not in summary.columns:
        return {
            "status": "no_summary_rows",
            **authority,
            "near_miss_rows": 0,
            "top_relaxation_candidates": [],
            "top_do_not_relax": [],
            "needs_more_label_near_misses": [],
        }

    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for _, item in summary.iterrows():
        payload = _json_loads(item.get("archetype_breakdown_json"), {})
        if not isinstance(payload, dict):
            continue
        horizon = int(item.get("horizon_days") or payload.get("horizon_days") or 0)
        for row in payload.get("trigger_near_miss_breakdown") or []:
            if not isinstance(row, dict):
                continue
            key = (
                str(row.get("near_miss_archetype") or "unknown"),
                str(row.get("near_miss_blocker_key") or "none"),
            )
            bucket = buckets.setdefault(
                key,
                {
                    "near_miss_archetype": key[0],
                    "near_miss_blocker_key": key[1],
                    "near_miss_blocker_codes": row.get("near_miss_blocker_codes") if isinstance(row.get("near_miss_blocker_codes"), list) else [],
                    "horizons": [],
                    "signal_count": 0,
                    "_return_weighted_sum": 0.0,
                    "_return_weight": 0,
                    "_hit_weighted_sum": 0.0,
                    "_hit_weight": 0,
                    "sample_blockers": row.get("sample_blockers") if isinstance(row.get("sample_blockers"), list) else [],
                },
            )
            if horizon and horizon not in bucket["horizons"]:
                bucket["horizons"].append(horizon)
            signal_count = int(row.get("signal_count") or 0)
            bucket["signal_count"] += signal_count
            avg_return = pd.to_numeric(row.get("avg_return_after_cost"), errors="coerce")
            if not pd.isna(avg_return) and signal_count > 0:
                bucket["_return_weighted_sum"] += float(avg_return) * signal_count
                bucket["_return_weight"] += signal_count
            hit_rate = pd.to_numeric(row.get("hit_rate_after_cost"), errors="coerce")
            if not pd.isna(hit_rate) and signal_count > 0:
                bucket["_hit_weighted_sum"] += float(hit_rate) * signal_count
                bucket["_hit_weight"] += signal_count

    rows: list[dict[str, Any]] = []
    for bucket in buckets.values():
        avg_return = (
            None
            if int(bucket["_return_weight"]) == 0
            else round(float(bucket["_return_weighted_sum"] / bucket["_return_weight"]), 6)
        )
        hit_rate = (
            None
            if int(bucket["_hit_weight"]) == 0
            else round(float(bucket["_hit_weighted_sum"] / bucket["_hit_weight"]), 6)
        )
        signal_count = int(bucket["signal_count"])
        if signal_count < int(min_signals) or avg_return is None or hit_rate is None:
            classification = "needs_more_matured_labels"
        elif avg_return >= float(return_threshold) and hit_rate >= float(min_hit_rate):
            classification = "potential_trigger_relaxation_candidate"
        elif avg_return <= 0.0 or hit_rate < 0.4:
            classification = "do_not_relax_negative_or_weak"
        else:
            classification = "mixed_or_marginal_requires_review"
        rows.append(
            {
                "near_miss_archetype": bucket["near_miss_archetype"],
                "near_miss_blocker_key": bucket["near_miss_blocker_key"],
                "near_miss_blocker_codes": bucket["near_miss_blocker_codes"],
                "horizons": sorted(bucket["horizons"]),
                "signal_count": signal_count,
                "avg_return_after_cost": avg_return,
                "hit_rate_after_cost": hit_rate,
                "classification": classification,
                "sample_blockers": bucket["sample_blockers"],
            }
        )

    rows.sort(
        key=lambda row: (
            row["classification"] == "potential_trigger_relaxation_candidate",
            int(row.get("signal_count") or 0),
            float(row.get("avg_return_after_cost") or -999.0),
        ),
        reverse=True,
    )
    classification_counts = dict(pd.Series([row["classification"] for row in rows]).value_counts().to_dict()) if rows else {}
    return {
        "status": "ok" if rows else "no_near_miss_rows",
        **authority,
        "min_signals": int(min_signals),
        "return_threshold": float(return_threshold),
        "min_hit_rate": float(min_hit_rate),
        "near_miss_rows": len(rows),
        "classification_counts": classification_counts,
        "top_relaxation_candidates": [row for row in rows if row["classification"] == "potential_trigger_relaxation_candidate"][:10],
        "top_do_not_relax": [row for row in rows if row["classification"] == "do_not_relax_negative_or_weak"][:10],
        "needs_more_label_near_misses": [row for row in rows if row["classification"] == "needs_more_matured_labels"][:10],
        "mixed_or_marginal_near_misses": [row for row in rows if row["classification"] == "mixed_or_marginal_requires_review"][:10],
    }


def calibrate_thresholds(
    *,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    horizons: list[int] | None = None,
    return_threshold: float = DEFAULT_RETURN_THRESHOLD,
    cost_bps: float = DEFAULT_COST_BPS,
    min_signals: int = DEFAULT_MIN_SIGNALS,
    max_configs: int | None = None,
    progress_every: int = DEFAULT_PROGRESS_EVERY,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    effective_horizons = sorted({int(value) for value in (horizons or DEFAULT_HORIZONS) if int(value) > 0})
    _emit(
        "load_signals start "
        f"from={from_date.isoformat() if from_date is not None else 'all'} "
        f"to={to_date.isoformat() if to_date is not None else 'all'} "
        f"symbols={len(symbols or []) or 'all'} horizons={effective_horizons}"
    )
    signals = load_technical_signal_rows(from_date=from_date, to_date=to_date, symbols=symbols)
    _emit(f"load_signals done rows={len(signals)}")
    if signals.empty or not effective_horizons:
        return pd.DataFrame(), pd.DataFrame(), {"signal_rows": int(len(signals)), "matured_rows": 0}
    price_start = signals["asof_date"].min() - pd.Timedelta(days=5)
    price_end = signals["asof_date"].max() + pd.Timedelta(days=max(effective_horizons) * 3 + 15)
    signal_symbols = signals["symbol"].dropna().astype(str).str.upper().drop_duplicates().tolist()
    _emit(
        "load_prices start "
        f"symbols={len(signal_symbols)} from={price_start.isoformat()} to={price_end.isoformat()}"
    )
    prices = load_price_history_for_returns(
        symbols=signal_symbols,
        from_date=price_start,
        to_date=price_end,
    )
    _emit(f"load_prices done rows={len(prices)}")
    _emit("attach_forward_returns start")
    dataset = attach_forward_returns(signals, prices, horizons=effective_horizons)
    _emit(f"attach_forward_returns done rows={len(dataset)}")
    evaluated_at = pd.Timestamp.utcnow()
    full_grid = build_threshold_grid()
    if max_configs is not None and int(max_configs) > 0:
        grid = full_grid[: int(max_configs)]
    else:
        grid = full_grid
    total_evaluations = len(grid) * len(effective_horizons)
    _emit(
        "evaluate_grid start "
        f"horizons={len(effective_horizons)} configs={len(grid)} full_configs={len(full_grid)} total={total_evaluations}"
    )
    rows: list[dict[str, Any]] = []
    evaluated_count = 0
    for horizon in effective_horizons:
        _emit(f"evaluate_horizon start horizon={horizon}")
        for config in grid:
            rows.append(
                evaluate_threshold_config(
                    dataset,
                    config,
                    horizon_days=horizon,
                    return_threshold=return_threshold,
                    cost_bps=cost_bps,
                    min_signals=min_signals,
                    evaluated_at=evaluated_at,
                )
            )
            evaluated_count += 1
            if progress_every and int(progress_every) > 0 and evaluated_count % int(progress_every) == 0:
                _emit(f"evaluate_grid progress evaluated={evaluated_count}/{total_evaluations}")
        _emit(f"evaluate_horizon done horizon={horizon} evaluated={evaluated_count}/{total_evaluations}")
    evaluations = pd.DataFrame(rows)
    _emit(f"evaluate_grid done rows={len(evaluations)}")
    _emit("build_summary start")
    summary = build_summary(
        evaluations,
        evaluated_at=evaluated_at,
        dataset=dataset,
        return_threshold=return_threshold,
        cost_bps=cost_bps,
    )
    grid_truncated = len(grid) != len(full_grid)
    if grid_truncated and not summary.empty:
        summary = summary.copy()
        summary["recommendation"] = "bounded_first_pass_only"
    _emit(f"build_summary done rows={len(summary)}")
    meta = {
        "signal_rows": int(len(signals)),
        "price_rows": int(len(prices)),
        "technical_feature_join": signals.attrs.get("technical_feature_join", {}),
        "technical_blocker_contract_reconstruction": signals.attrs.get("technical_blocker_contract_reconstruction", {}),
        "matured_rows_by_horizon": {
            str(horizon): int(dataset[f"forward_return_h{horizon}"].notna().sum())
            for horizon in effective_horizons
            if f"forward_return_h{horizon}" in dataset.columns
        },
        "grid_count": len(grid),
        "full_grid_count": len(full_grid),
        "grid_truncated": grid_truncated,
        "max_configs": int(max_configs) if max_configs is not None and int(max_configs) > 0 else None,
        "promotion_eligible": not grid_truncated,
        "policy_effect": "diagnostic_only_no_promotion" if grid_truncated else "full_grid_research_evidence",
        "horizons": effective_horizons,
        "return_threshold": float(return_threshold),
        "cost_bps": float(cost_bps),
        "min_signals": int(min_signals),
    }
    return evaluations, summary, meta


def persist_outputs(evaluations: pd.DataFrame, summary: pd.DataFrame) -> None:
    ensure_tables()
    if not evaluations.empty:
        upsert_to_db(evaluations, EVALUATIONS_TABLE, unique_keys=["evaluated_at", "config_id", "horizon_days"], timescaledb_column="evaluated_at")
    if not summary.empty:
        upsert_to_db(summary, SUMMARY_TABLE, unique_keys=["evaluated_at", "horizon_days"], timescaledb_column="evaluated_at")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Calibrate swing technical thresholds against realized forward returns.")
    parser.add_argument("--from-date", type=parse_datetime_arg)
    parser.add_argument("--to-date", type=parse_datetime_arg)
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument("--horizons", nargs="*", type=int, default=DEFAULT_HORIZONS)
    parser.add_argument("--return-threshold", type=float, default=DEFAULT_RETURN_THRESHOLD)
    parser.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    parser.add_argument("--min-signals", type=int, default=DEFAULT_MIN_SIGNALS)
    parser.add_argument(
        "--max-configs",
        type=int,
        default=None,
        help="Evaluate only the first N threshold configs for a bounded first-pass diagnostic. Omit for the full grid.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=DEFAULT_PROGRESS_EVERY,
        help="Emit stderr progress every N evaluated configs. Use 0 to disable progress logs.",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        evaluations, summary, meta = calibrate_thresholds(
            from_date=pd.Timestamp(args.from_date, tz="UTC") if args.from_date else None,
            to_date=pd.Timestamp(args.to_date, tz="UTC") if args.to_date else None,
            symbols=args.symbols,
            horizons=args.horizons,
            return_threshold=float(args.return_threshold),
            cost_bps=float(args.cost_bps),
            min_signals=int(args.min_signals),
            max_configs=args.max_configs,
            progress_every=int(args.progress_every),
        )
    except SourceUnavailableError as exc:
        record_local_fallback_event(
            module="advisory.technical_threshold_calibration",
            source="threshold_calibration",
            fallback_type="source_unavailable",
            severity="error",
            reason="Technical threshold calibration could not run because required source data was unavailable.",
            error=exc,
            metadata={
                "from_date": args.from_date,
                "to_date": args.to_date,
                "symbols": list(args.symbols or []),
                "horizons": list(args.horizons or []),
            },
        )
        print(
            json.dumps(
                {
                    "status": "source_unavailable",
                    "evaluations_table": EVALUATIONS_TABLE,
                    "summary_table": SUMMARY_TABLE,
                    "evaluation_rows": 0,
                    "summary_rows": 0,
                    "meta": {
                        "signal_rows": None,
                        "matured_rows": None,
                        "source_unavailable": True,
                    },
                    "error": str(exc),
                    "dry_run": bool(args.dry_run),
                },
                indent=2,
                ensure_ascii=False,
                default=str,
            )
        )
        return 2
    if not args.dry_run:
        persist_outputs(evaluations, summary)
    blocker_research_summary = summarize_blocker_research(
        summary,
        min_signals=int(args.min_signals),
        return_threshold=float(args.return_threshold),
    )
    trigger_near_miss_research_summary = summarize_trigger_near_miss_research(
        summary,
        min_signals=int(args.min_signals),
        return_threshold=float(args.return_threshold),
    )
    print(
        json.dumps(
            {
                "status": "ok",
                "evaluations_table": EVALUATIONS_TABLE,
                "summary_table": SUMMARY_TABLE,
                "evaluation_rows": int(len(evaluations)),
                "summary_rows": int(len(summary)),
                "meta": meta,
                "blocker_research_summary": blocker_research_summary,
                "trigger_near_miss_research_summary": trigger_near_miss_research_summary,
                "summary_sample": summary.head(10).to_dict(orient="records") if not summary.empty else [],
                "dry_run": bool(args.dry_run),
            },
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
