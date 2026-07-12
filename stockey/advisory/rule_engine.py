from __future__ import annotations

import argparse
import json
import os
from datetime import timezone
from typing import Any

import pandas as pd

from advisory.data_sync import ensure_advisory_symbol_inputs
from advisory.fallback_telemetry import record_local_fallback_event
from advisory.fundamental_snapshot import build_fundamental_snapshot, persist_fundamental_snapshot
from advisory.intraday_features import TABLE_NAME as INTRADAY_FEATURES_TABLE
from advisory.intraday_features import build_intraday_features, persist_intraday_features
from advisory.news_theme_engine import load_active_theme_screener_mapping
from advisory.peer_sync import sync_peer_data
from advisory.setup_registry import load_setup_registry
from advisory.technical_engine import evaluate_pre_entry_state as evaluate_technical_pre_entry_state
from advisory.technical_features import build_technical_features, persist_technical_features
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration
from utils.sync import parse_datetime_arg


CANDIDATES_TABLE = "advisory_candidates"
REJECTIONS_TABLE = "advisory_candidate_rejections"
OVERLAY_TABLE = "advisory_market_overlay_daily"
RULE_ENGINE_SCHEMA_MIGRATION_ID = "20260611_advisory_rule_outputs_base"
RULE_ENGINE_CONTEXT_METADATA_MIGRATION_ID = "20260622_advisory_rule_outputs_context_metadata"
RULE_ENGINE_TECHNICAL_ENTRY_MIGRATION_ID = "20260622_advisory_rule_outputs_technical_entry_confirmed"
CANDIDATE_COLUMNS = {
    "screener_date": "TIMESTAMPTZ",
    "setup_name": "TEXT",
    "setup_family": "TEXT",
    "holding_horizon_note": "TEXT",
    "regime_name": "TEXT",
    "base_regime": "TEXT",
    "news_overlay": "TEXT",
    "theme_ids": "TEXT",
    "company_master_id": "TEXT",
    "screener_slug": "TEXT",
    "source_screener_slug": "TEXT",
    "source_screener_list": "TEXT",
    "rank": "BIGINT",
    "candidate_state": "TEXT",
    "watch_reason_detail": "TEXT",
    "soft_failures_json": "TEXT",
    "technical_state": "TEXT",
    "technical_trigger_type": "TEXT",
    "technical_trigger_note": "TEXT",
    "technical_setup_archetype": "TEXT",
    "technical_setup_quality_json": "TEXT",
    "technical_trend_score": "DOUBLE PRECISION",
    "technical_structure_score": "DOUBLE PRECISION",
    "technical_participation_score": "DOUBLE PRECISION",
    "technical_relative_strength_score": "DOUBLE PRECISION",
    "technical_tradability_score": "DOUBLE PRECISION",
    "technical_score": "DOUBLE PRECISION",
    "fundamental_score": "DOUBLE PRECISION",
    "regime_fit_score": "DOUBLE PRECISION",
    "regime_fit_weight_effective": "DOUBLE PRECISION",
    "event_score": "DOUBLE PRECISION",
    "setup_score": "DOUBLE PRECISION",
    "avg_traded_value_20d": "DOUBLE PRECISION",
    "rs_vs_benchmark": "DOUBLE PRECISION",
    "rs_vs_sector": "DOUBLE PRECISION",
    "intraday_close_vs_vwap_pct": "DOUBLE PRECISION",
    "intraday_pct_bars_above_vwap": "DOUBLE PRECISION",
    "intraday_close_location_pct": "DOUBLE PRECISION",
    "intraday_opening_range_breakout_up": "BOOLEAN",
    "intraday_prev_day_breakout_up": "BOOLEAN",
    "intraday_failed_prev_day_breakout": "BOOLEAN",
    "intraday_volume_vs_20d": "DOUBLE PRECISION",
    "intraday_breakout_score": "DOUBLE PRECISION",
    "intraday_pattern_label": "TEXT",
    "intraday_interval_minutes": "INTEGER",
    "total_revenue_qoq_growth_vs_sector": "DOUBLE PRECISION",
    "profit_after_tax_qoq_growth_vs_sector": "DOUBLE PRECISION",
    "debt_to_equity_vs_sector": "DOUBLE PRECISION",
    "entry_style": "TEXT",
    "attractive_price_low": "DOUBLE PRECISION",
    "attractive_price_high": "DOUBLE PRECISION",
    "invalidation_price": "DOUBLE PRECISION",
    "entry_note": "TEXT",
    "near_miss_flag": "BOOLEAN",
    "watch_enabled": "BOOLEAN",
    "watch_reasons": "TEXT",
    "rule_pass": "BOOLEAN",
    "load_ts": "TIMESTAMPTZ",
}
CONTEXT_METADATA_CANDIDATE_COLUMNS = {
    "soft_failures_json",
    "technical_setup_archetype",
    "technical_setup_quality_json",
    "regime_fit_weight_effective",
}
BASE_CANDIDATE_COLUMNS = {
    column: sql_type
    for column, sql_type in CANDIDATE_COLUMNS.items()
    if column not in CONTEXT_METADATA_CANDIDATE_COLUMNS
}
REJECTION_COLUMNS = {
    "screener_date": "TIMESTAMPTZ",
    "setup_name": "TEXT",
    "company_master_id": "TEXT",
    "base_regime": "TEXT",
    "news_overlay": "TEXT",
    "severity": "TEXT",
    "is_near_miss": "BOOLEAN",
    "delta_to_pass": "DOUBLE PRECISION",
    "reason_detail": "TEXT",
    "load_ts": "TIMESTAMPTZ",
}
RULE_ENGINE_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {CANDIDATES_TABLE} (
        asof_date TIMESTAMPTZ NOT NULL,
        screener_date TIMESTAMPTZ,
        setup_id TEXT NOT NULL,
        setup_name TEXT,
        setup_family TEXT,
        holding_horizon_note TEXT,
        regime_name TEXT,
        base_regime TEXT,
        news_overlay TEXT,
        theme_ids TEXT,
        symbol TEXT NOT NULL,
        company_master_id TEXT,
        screener_slug TEXT,
        source_screener_slug TEXT,
        source_screener_list TEXT,
        rank BIGINT,
        candidate_state TEXT,
        watch_reason_detail TEXT,
        technical_state TEXT,
        technical_trigger_type TEXT,
        technical_trigger_note TEXT,
        technical_trend_score DOUBLE PRECISION,
        technical_structure_score DOUBLE PRECISION,
        technical_participation_score DOUBLE PRECISION,
        technical_relative_strength_score DOUBLE PRECISION,
        technical_tradability_score DOUBLE PRECISION,
        technical_score DOUBLE PRECISION,
        fundamental_score DOUBLE PRECISION,
        regime_fit_score DOUBLE PRECISION,
        event_score DOUBLE PRECISION,
        setup_score DOUBLE PRECISION,
        avg_traded_value_20d DOUBLE PRECISION,
        rs_vs_benchmark DOUBLE PRECISION,
        rs_vs_sector DOUBLE PRECISION,
        intraday_close_vs_vwap_pct DOUBLE PRECISION,
        intraday_pct_bars_above_vwap DOUBLE PRECISION,
        intraday_close_location_pct DOUBLE PRECISION,
        intraday_opening_range_breakout_up BOOLEAN,
        intraday_prev_day_breakout_up BOOLEAN,
        intraday_failed_prev_day_breakout BOOLEAN,
        intraday_volume_vs_20d DOUBLE PRECISION,
        intraday_breakout_score DOUBLE PRECISION,
        intraday_pattern_label TEXT,
        intraday_interval_minutes INTEGER,
        total_revenue_qoq_growth_vs_sector DOUBLE PRECISION,
        profit_after_tax_qoq_growth_vs_sector DOUBLE PRECISION,
        debt_to_equity_vs_sector DOUBLE PRECISION,
        entry_style TEXT,
        attractive_price_low DOUBLE PRECISION,
        attractive_price_high DOUBLE PRECISION,
        invalidation_price DOUBLE PRECISION,
        entry_note TEXT,
        near_miss_flag BOOLEAN,
        watch_enabled BOOLEAN,
        watch_reasons TEXT,
        rule_pass BOOLEAN,
        load_ts TIMESTAMPTZ,
        UNIQUE (asof_date, setup_id, symbol)
    )
    """,
    *[
        f"ALTER TABLE {CANDIDATES_TABLE} ADD COLUMN IF NOT EXISTS {column} {sql_type}"
        for column, sql_type in BASE_CANDIDATE_COLUMNS.items()
    ],
    f"""
    CREATE TABLE IF NOT EXISTS {REJECTIONS_TABLE} (
        asof_date TIMESTAMPTZ NOT NULL,
        setup_id TEXT NOT NULL,
        symbol TEXT,
        reason_code TEXT NOT NULL,
        screener_date TIMESTAMPTZ,
        setup_name TEXT,
        company_master_id TEXT,
        base_regime TEXT,
        news_overlay TEXT,
        severity TEXT,
        is_near_miss BOOLEAN,
        delta_to_pass DOUBLE PRECISION,
        reason_detail TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (asof_date, setup_id, symbol, reason_code)
    )
    """,
    *[
        f"ALTER TABLE {REJECTIONS_TABLE} ADD COLUMN IF NOT EXISTS {column} {sql_type}"
        for column, sql_type in REJECTION_COLUMNS.items()
    ],
]
RULE_ENGINE_CONTEXT_METADATA_SCHEMA_STATEMENTS = [
    f"ALTER TABLE {CANDIDATES_TABLE} ADD COLUMN IF NOT EXISTS {column} {CANDIDATE_COLUMNS[column]}"
    for column in [
        "soft_failures_json",
        "technical_setup_archetype",
        "technical_setup_quality_json",
        "regime_fit_weight_effective",
    ]
]
RULE_ENGINE_TS_OVERRIDE_MIGRATION_ID = "20260707_advisory_rule_outputs_ts_forecast_override"
RULE_ENGINE_TS_OVERRIDE_SCHEMA_STATEMENTS = [
    f"ALTER TABLE {CANDIDATES_TABLE} ADD COLUMN IF NOT EXISTS technical_override_source TEXT",
    f"ALTER TABLE {CANDIDATES_TABLE} ADD COLUMN IF NOT EXISTS technical_override_json TEXT",
]

RULE_ENGINE_RS_PERCENTILE_MIGRATION_ID = "20260708_advisory_rule_outputs_rs_percentile"
RULE_ENGINE_RS_PERCENTILE_SCHEMA_STATEMENTS = [
    f"ALTER TABLE {CANDIDATES_TABLE} ADD COLUMN IF NOT EXISTS rs_percentile DOUBLE PRECISION",
]

# TS-forecast rescue (allow-gate): a candidate that fails deterministic technical
# confirmation but carries a strong TimesFM forecast can be rescued to PASS_NOW with the
# override recorded (same principle as recorded LLM soft-gate overrides). Intersecting a
# model with the existing filters can only remove candidates; the rescue path is the only
# way the model can ADD information. Thresholds are deliberately high, the daily rescue
# count is capped, risk sizing applies a tighter cap for override-sourced rows, and the
# rescued cohort matures through the normal outcome labeling so the gate keeps or loses
# its power based on measured benchmark-excess evidence.
TS_RESCUE_ENABLED = os.getenv("TS_FORECAST_RESCUE_ENABLED", "true").strip().lower() not in {"0", "false", "no"}
TS_RESCUE_HORIZON_DAYS = int(os.getenv("TS_FORECAST_RESCUE_HORIZON_DAYS", "10"))
TS_RESCUE_MIN_FORECAST_RETURN = float(os.getenv("TS_FORECAST_RESCUE_MIN_FORECAST_RETURN", "0.05"))
TS_RESCUE_MIN_PROBABILITY = float(os.getenv("TS_FORECAST_RESCUE_MIN_PROBABILITY", "0.65"))
TS_RESCUE_MIN_SIGNAL_QUALITY = float(os.getenv("TS_FORECAST_RESCUE_MIN_SIGNAL_QUALITY", "0.65"))
TS_RESCUE_MAX_DOWNSIDE_P10 = float(os.getenv("TS_FORECAST_RESCUE_MAX_DOWNSIDE_P10", "-0.12"))
TS_RESCUE_MAX_PER_DAY = int(os.getenv("TS_FORECAST_RESCUE_MAX_PER_DAY", "5"))
TS_RESCUE_MAX_FORECAST_AGE_DAYS = int(os.getenv("TS_FORECAST_RESCUE_MAX_FORECAST_AGE_DAYS", "5"))
TS_FORECASTS_TABLE = "advisory_ts_forecasts_daily"

RULE_ENGINE_TECHNICAL_ENTRY_SCHEMA_STATEMENTS = [
    f"ALTER TABLE {CANDIDATES_TABLE} ADD COLUMN IF NOT EXISTS technical_entry_confirmed BOOLEAN",
]

DEFAULT_SCORING_WEIGHTS = {
    "technical": 0.35,
    "fundamental": 0.30,
    "regime_fit": 0.20,
    "event": 0.15,
}
DEFAULT_SCORE_THRESHOLDS = {
    "pass_now": 0.68,
    "watch_breakout": 0.58,
    "watch_pullback": 0.52,
    "watch_event": 0.48,
    "abstain": 0.40,
    "near_miss_gap": 0.05,
}


def normalize_timestamp(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce").dt.normalize()


def _record_rule_engine_fallback(
    *,
    fallback_type: str,
    source: str,
    reason: str,
    error: Exception,
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="advisory.rule_engine",
        fallback_type=fallback_type,
        source=source,
        severity="warn",
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def table_exists(table_name: str) -> bool:
    try:
        df = sql_to_df(
            """
            SELECT 1 AS exists_flag
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = %s
            LIMIT 1
            """,
            params=(table_name,),
        )
    except Exception as exc:
        _record_rule_engine_fallback(
            fallback_type="rule_engine_table_lookup_failed",
            source=table_name,
            reason="Rule engine could not inspect whether a source/output table exists.",
            error=exc,
            metadata={"table_name": table_name},
        )
        return False
    return not df.empty


def safe_float(value: Any) -> float | None:
    out = pd.to_numeric(value, errors="coerce")
    if pd.isna(out):
        return None
    return float(out)


def ensure_rule_output_tables() -> None:
    apply_schema_migration(
        migration_id=RULE_ENGINE_SCHEMA_MIGRATION_ID,
        statements=RULE_ENGINE_SCHEMA_STATEMENTS,
        owner="advisory.rule_engine",
        description="Create advisory rule candidate and rejection output tables.",
        metadata={"tables": [CANDIDATES_TABLE, REJECTIONS_TABLE], "workflow": "rule_engine_outputs"},
    )
    apply_schema_migration(
        migration_id=RULE_ENGINE_CONTEXT_METADATA_MIGRATION_ID,
        statements=RULE_ENGINE_CONTEXT_METADATA_SCHEMA_STATEMENTS,
        owner="advisory.rule_engine",
        description="Add context and technical metadata columns to advisory rule candidate outputs.",
        metadata={
            "tables": [CANDIDATES_TABLE],
            "workflow": "rule_engine_outputs",
            "base_migration_id": RULE_ENGINE_SCHEMA_MIGRATION_ID,
            "columns": list(RULE_ENGINE_CONTEXT_METADATA_SCHEMA_STATEMENTS),
        },
    )
    apply_schema_migration(
        migration_id=RULE_ENGINE_TECHNICAL_ENTRY_MIGRATION_ID,
        statements=RULE_ENGINE_TECHNICAL_ENTRY_SCHEMA_STATEMENTS,
        owner="advisory.rule_engine",
        description="Add explicit technical entry confirmation flag to advisory rule candidate outputs.",
        metadata={
            "tables": [CANDIDATES_TABLE],
            "workflow": "rule_engine_outputs",
            "base_migration_id": RULE_ENGINE_SCHEMA_MIGRATION_ID,
            "columns": ["technical_entry_confirmed"],
        },
    )
    apply_schema_migration(
        migration_id=RULE_ENGINE_TS_OVERRIDE_MIGRATION_ID,
        statements=RULE_ENGINE_TS_OVERRIDE_SCHEMA_STATEMENTS,
        owner="advisory.rule_engine",
        description="Add recorded technical-override columns (TS-forecast rescue allow-gate) to rule candidate outputs.",
        metadata={
            "tables": [CANDIDATES_TABLE],
            "workflow": "rule_engine_outputs",
            "base_migration_id": RULE_ENGINE_SCHEMA_MIGRATION_ID,
            "columns": ["technical_override_source", "technical_override_json"],
        },
    )
    apply_schema_migration(
        migration_id=RULE_ENGINE_RS_PERCENTILE_MIGRATION_ID,
        statements=RULE_ENGINE_RS_PERCENTILE_SCHEMA_STATEMENTS,
        owner="advisory.rule_engine",
        description="Add cross-sectional relative-strength percentile to rule candidate outputs.",
        metadata={
            "tables": [CANDIDATES_TABLE],
            "workflow": "rule_engine_outputs",
            "base_migration_id": RULE_ENGINE_SCHEMA_MIGRATION_ID,
            "columns": ["rs_percentile"],
        },
    )


DEFAULT_FRESHNESS_POLICY = {
    "technical_max_age_days": 15,
    "fundamentals_max_age_days": 150,
    "regime_max_age_days": 15,
    "intraday_max_age_days": 2,
    "fundamentals_required": True,
}
DEFAULT_MAX_SNAPSHOT_REFRESH_AGE_DAYS = 7
DEFAULT_MAX_INTRADAY_PREFETCH_AGE_DAYS = 14
RULE_ENGINE_REGIME_LABEL_HARD_BLOCK_ENABLED_ENV = "RULE_ENGINE_REGIME_LABEL_HARD_BLOCK_ENABLED"
RULE_ENGINE_OVERLAY_LABEL_HARD_BLOCK_ENABLED_ENV = "RULE_ENGINE_OVERLAY_LABEL_HARD_BLOCK_ENABLED"
RULE_ENGINE_REGIME_FIT_WEIGHT_MULTIPLIER_ENV = "RULE_ENGINE_REGIME_FIT_WEIGHT_MULTIPLIER"
RULE_ENGINE_REQUIRE_REGIME_CONTEXT_ENV = "RULE_ENGINE_REQUIRE_REGIME_CONTEXT"
DEFAULT_REGIME_FIT_WEIGHT_MULTIPLIER = 0.35
DIAGNOSTIC_REGIME_NAME = "UNKNOWN"


def get_effective_dates(asof_date: pd.Timestamp | None = None) -> dict[str, pd.Timestamp | None]:
    screener_cutoff = asof_date
    if screener_cutoff is None:
        try:
            screener_df = sql_to_df("SELECT MAX(date) AS screener_date FROM advisory_screener_constituents")
        except Exception as exc:
            _record_rule_engine_fallback(
                fallback_type="rule_engine_screener_date_lookup_failed",
                source="advisory_screener_constituents",
                reason="Rule engine could not resolve the latest screener date.",
                error=exc,
            )
            raise
        if screener_df.empty or pd.isna(pd.to_datetime(screener_df.iloc[0]["screener_date"], utc=True, errors="coerce")):
            return {
                "requested_asof_date": None,
                "screener_date": None,
                "regime_date": None,
                "technical_date": None,
                "fundamentals_date": None,
                "intraday_date": None,
            }
        screener_cutoff = pd.to_datetime(screener_df.iloc[0]["screener_date"], utc=True, errors="coerce").normalize()

    try:
        df = sql_to_df(
            """
            SELECT
                (SELECT MAX(date) FROM advisory_screener_constituents WHERE date <= %(asof_date)s) AS screener_date,
                (SELECT MAX(asof_date) FROM advisory_market_regime WHERE asof_date <= %(asof_date)s) AS regime_date,
                (SELECT MAX(asof_date) FROM advisory_technical_daily WHERE asof_date <= %(asof_date)s) AS technical_date,
                (SELECT MAX(asof_date) FROM advisory_fundamentals_daily WHERE asof_date <= %(asof_date)s) AS fundamentals_date
            """,
            params={"asof_date": screener_cutoff},
        )
    except Exception as exc:
        _record_rule_engine_fallback(
            fallback_type="rule_engine_effective_dates_lookup_failed",
            source="advisory_screener_constituents,advisory_market_regime,advisory_technical_daily,advisory_fundamentals_daily",
            reason="Rule engine could not resolve effective source dates.",
            error=exc,
            metadata={"asof_date": str(screener_cutoff)},
        )
        raise
    if df.empty:
        return {
            "requested_asof_date": screener_cutoff.normalize(),
            "screener_date": None,
            "regime_date": None,
            "technical_date": None,
            "fundamentals_date": None,
            "intraday_date": None,
        }
    row = df.iloc[0]
    screener_date = pd.to_datetime(row.get("screener_date"), utc=True, errors="coerce")
    regime_date = pd.to_datetime(row.get("regime_date"), utc=True, errors="coerce")
    technical_date = pd.to_datetime(row.get("technical_date"), utc=True, errors="coerce")
    fundamentals_date = pd.to_datetime(row.get("fundamentals_date"), utc=True, errors="coerce")
    intraday_date = None
    if table_exists(INTRADAY_FEATURES_TABLE):
        try:
            intraday_df = sql_to_df(
                f"SELECT MAX(asof_date) AS intraday_date FROM {INTRADAY_FEATURES_TABLE} WHERE asof_date <= %s",
                params=(screener_cutoff,),
            )
        except Exception as exc:
            _record_rule_engine_fallback(
                fallback_type="rule_engine_intraday_date_lookup_failed",
                source=INTRADAY_FEATURES_TABLE,
                reason="Rule engine could not resolve latest intraday feature date.",
                error=exc,
                metadata={"asof_date": str(screener_cutoff)},
            )
            raise
        if not intraday_df.empty:
            intraday_date = pd.to_datetime(intraday_df.iloc[0].get("intraday_date"), utc=True, errors="coerce")
    return {
        "requested_asof_date": screener_cutoff.normalize(),
        "screener_date": None if pd.isna(screener_date) else screener_date.normalize(),
        "regime_date": None if pd.isna(regime_date) else regime_date.normalize(),
        "technical_date": None if pd.isna(technical_date) else technical_date.normalize(),
        "fundamentals_date": None if pd.isna(fundamentals_date) else fundamentals_date.normalize(),
        "intraday_date": None if intraday_date is None or pd.isna(intraday_date) else intraday_date.normalize(),
    }


def load_regime(asof_date: pd.Timestamp) -> dict[str, Any] | None:
    try:
        df = sql_to_df(
            """
            SELECT *
            FROM advisory_market_regime
            WHERE asof_date <= %s
            ORDER BY asof_date DESC
            LIMIT 1
            """,
            params=(asof_date,),
        )
    except Exception as exc:
        _record_rule_engine_fallback(
            fallback_type="rule_engine_regime_load_failed",
            source="advisory_market_regime",
            reason="Rule engine could not load market-regime context.",
            error=exc,
            metadata={"asof_date": str(asof_date)},
        )
        raise
    if df.empty:
        return None
    return df.iloc[0].to_dict()


def missing_regime_context(asof_date: pd.Timestamp) -> dict[str, Any]:
    error = RuntimeError("no market-regime row available")
    _record_rule_engine_fallback(
        fallback_type="rule_engine_regime_context_missing",
        source="advisory_market_regime",
        reason="Rule engine continued with UNKNOWN diagnostic regime because no market-regime snapshot was available.",
        error=error,
        metadata={"asof_date": str(asof_date), "regime_name": DIAGNOSTIC_REGIME_NAME},
    )
    return {
        "asof_date": None,
        "regime_name": DIAGNOSTIC_REGIME_NAME,
        "regime_context_status": "missing",
        "regime_context_reason": "no market-regime snapshot available on or before screener date",
    }


def load_overlay(asof_date: pd.Timestamp, regime_name: str | None = None) -> dict[str, Any]:
    if table_exists(OVERLAY_TABLE):
        try:
            df = sql_to_df(
                f"""
                SELECT *
                FROM {OVERLAY_TABLE}
                WHERE asof_date <= %s
                ORDER BY asof_date DESC
                LIMIT 1
                """,
                params=(asof_date,),
            )
        except Exception as exc:
            _record_rule_engine_fallback(
                fallback_type="rule_engine_overlay_load_failed",
                source=OVERLAY_TABLE,
                reason="Rule engine could not load market-overlay context.",
                error=exc,
                metadata={"asof_date": str(asof_date)},
            )
            raise
        if not df.empty:
            row = df.iloc[0].to_dict()
            row["overlay_name"] = str(row.get("overlay_name") or "NONE").upper()
            return row
    return {
        "asof_date": asof_date,
        "base_regime": regime_name,
        "overlay_name": "NONE",
        "overlay_intensity": 0.0,
        "overlay_reason": "overlay table missing or no overlay row for date",
        "source_count": 0,
    }


def resolve_setup_screeners(
    setup: dict[str, Any],
    overlay_name: str | None,
    *,
    theme_screener_mapping: dict[str, Any] | None = None,
    dynamic_source_slugs: dict[str, list[str]] | None = None,
) -> tuple[list[str], str, list[str]]:
    base_screeners = [str(value) for value in (setup.get("screeners") or setup.get("screener_slugs") or []) if value]
    if not base_screeners and setup.get("screener_slug"):
        base_screeners = [str(setup["screener_slug"])]
    theme_ids: list[str] = []
    if str(setup.get("setup_id") or "").upper() == "EVENT_OPPORTUNITY_V1":
        theme_ids = [str(value) for value in ((theme_screener_mapping or {}).get("theme_ids") or []) if value]
        for value in ((theme_screener_mapping or {}).get("screener_slugs") or []):
            if value and str(value) not in base_screeners:
                base_screeners.append(str(value))
    # Dynamic constituents sources (market scan / hypothesis-<id> / theme-<id>) only reach
    # evaluation when a setup declares them via `dynamic_sources` -- their slugs are per-id
    # and cannot be listed statically in the YAML. Without this expansion, dynamically
    # materialized constituents never enter the rule engine at all.
    for source_kind in setup.get("dynamic_sources") or []:
        for slug in (dynamic_source_slugs or {}).get(str(source_kind).lower(), []):
            if slug and str(slug) not in base_screeners:
                base_screeners.append(str(slug))
    overlay_cfg = (setup.get("overlay_screeners") or {}).get(str(overlay_name or "NONE").upper(), {})
    add = [str(value) for value in (overlay_cfg.get("add") or []) if value]
    remove = {str(value) for value in (overlay_cfg.get("remove") or []) if value}
    active = [value for value in base_screeners if value not in remove]
    for value in add:
        if value not in active:
            active.append(value)
    return active, str(setup.get("screener_mode") or "union").lower(), theme_ids


def load_dynamic_source_slugs(asof_date: pd.Timestamp | None) -> dict[str, list[str]]:
    """The dynamic constituents slugs actually present at the screener date, grouped by
    source kind, so setups can opt in via `dynamic_sources`. Best-effort: {} on failure."""
    if asof_date is None:
        return {}
    try:
        frame = sql_to_df(
            """
            SELECT DISTINCT screener_slug FROM advisory_screener_constituents
            WHERE date = %s AND (
                screener_slug = 'market-action-scan-v1'
                OR screener_slug = 'volume-surge-scan-v1'
                OR screener_slug = 'momentum-trend-scan-v1'
                OR screener_slug LIKE 'hypothesis-%%'
                OR screener_slug LIKE 'theme-%%'
            )
            """,
            params=(asof_date,),
        )
        slugs = sorted(str(value) for value in frame["screener_slug"].dropna().tolist()) if not frame.empty else []
        return {
            "market_scan": [slug for slug in slugs if slug == "market-action-scan-v1"],
            "volume_surge": [slug for slug in slugs if slug == "volume-surge-scan-v1"],
            "momentum": [slug for slug in slugs if slug == "momentum-trend-scan-v1"],
            "hypothesis": [slug for slug in slugs if slug.startswith("hypothesis-")],
            "theme": [slug for slug in slugs if slug.startswith("theme-")],
        }
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.rule_engine",
            source="advisory_screener_constituents",
            fallback_type="dynamic_source_slugs_load_failed",
            severity="warn",
            reason="Dynamic source slugs could not be loaded; setups evaluate static screeners only this run.",
            error=exc,
            metadata={"asof_date": str(asof_date)},
        )
        return {}


def load_screener_universe(asof_date: pd.Timestamp, screener_slugs: list[str] | None, *, screener_mode: str = "union") -> pd.DataFrame:
    clauses = ["date = %s"]
    params: list[object] = [asof_date]
    normalized_slugs = [str(value) for value in (screener_slugs or []) if str(value).strip()]
    if normalized_slugs:
        clauses.append("screener_slug = ANY(%s)")
        params.append(normalized_slugs)
    try:
        df = sql_to_df(
            f"""
            SELECT
                date AS screener_date,
                screener_slug,
                screener_name,
                ticker AS symbol,
                exchange,
                company_master_id,
                rank,
                last_price,
                volume,
                market_cap,
                pe_ratio
            FROM advisory_screener_constituents
            WHERE {' AND '.join(clauses)}
            ORDER BY rank, symbol
            """,
            params=tuple(params),
        )
    except Exception as exc:
        _record_rule_engine_fallback(
            fallback_type="rule_engine_screener_universe_load_failed",
            source="advisory_screener_constituents",
            reason="Rule engine could not load screener universe rows.",
            error=exc,
            metadata={
                "asof_date": str(asof_date),
                "screener_slugs": normalized_slugs,
                "screener_mode": screener_mode,
            },
        )
        raise
    if df.empty:
        return df
    df["screener_date"] = normalize_timestamp(df["screener_date"])
    df["symbol"] = df["symbol"].astype("string").str.upper()
    required = sorted(set(normalized_slugs))
    if screener_mode == "intersection" and required:
        counts = df.groupby("symbol")["screener_slug"].nunique()
        eligible = counts[counts >= len(required)].index.astype("string").tolist()
        df = df[df["symbol"].isin(eligible)]
    if df.empty:
        return df
    rows: list[dict[str, Any]] = []
    for symbol, group in df.groupby("symbol", sort=False):
        ranked = group.sort_values(["rank", "screener_slug"], kind="stable")
        first = ranked.iloc[0].to_dict()
        screener_list = sorted(group["screener_slug"].dropna().astype(str).unique().tolist())
        first["source_screener_slug"] = first.get("screener_slug")
        first["source_screener_list"] = json.dumps(screener_list, ensure_ascii=False)
        first["source_screener_count"] = len(screener_list)
        rows.append(first)
    return pd.DataFrame(rows)


def load_technical(asof_date: pd.Timestamp) -> pd.DataFrame:
    try:
        df = sql_to_df(
            """
            SELECT DISTINCT ON (symbol)
                asof_date AS technical_snapshot_date,
                company_master_id,
                symbol,
                series,
                security_id,
                isin,
                benchmark_name,
                sector_code,
                sector_name,
                adj_close,
                adj_high,
                adj_low,
                volume,
                total_value,
                dma_20,
                dma_50,
                dma_150,
                dma_200,
                dma_20_slope_20d_pct,
                dma_50_slope_20d_pct,
                dma_150_slope_20d_pct,
                atr_20,
                atr_pct,
                atr_compression_pct,
                bb_width,
                bb_width_rank_252d,
                dist_20d_high,
                dist_50d_high,
                dist_52w_high,
                base_depth_20d_pct,
                base_depth_60d_pct,
                pivot_distance_20d_pct,
                pivot_distance_60d_pct,
                avg_traded_value_20d,
                avg_traded_value_60d,
                median_volume_20d,
                median_volume_60d,
                rs_vs_benchmark,
                sector_peer_ret_20d,
                sector_peer_count,
                rs_vs_sector,
                stock_ret_60d,
                stock_ret_120d,
                daily_range_pct,
                range_contraction_20d_pct,
                range_contraction_60d_pct,
                range_contraction_ratio,
                close_location_pct,
                tight_close_upper_half_20d,
                tight_close_upper_half_60d,
                higher_high_count_20d,
                higher_low_count_20d,
                trend_persistence_20d,
                trend_persistence_60d,
                trend_persistence_120d,
                breakout_day_volume_vs_20d,
                up_volume_20d,
                down_volume_20d,
                up_down_volume_ratio_20d,
                distribution_days_20d,
                accumulation_days_20d,
                pullback_volume_dryup_ratio_20d,
                gap_pct,
                gap_frequency_60d,
                support_distance_20d_pct,
                support_hold_rate_20d,
                breakout_extension_pct,
                volatility_contraction_flag,
                pass_above_dma_20,
                pass_above_dma_50,
                pass_above_dma_150,
                pass_above_dma_200,
                pass_liquidity_20d,
                pass_near_52w_high,
                pass_breakout_extension,
                pass_gap_behavior,
                pass_trend_alignment,
                load_ts
            FROM advisory_technical_daily
            WHERE asof_date <= %s
            ORDER BY symbol, asof_date DESC
            """,
            params=(asof_date,),
        )
    except Exception as exc:
        _record_rule_engine_fallback(
            fallback_type="rule_engine_technical_load_failed",
            source="advisory_technical_daily",
            reason="Rule engine could not load latest technical feature rows.",
            error=exc,
            metadata={"asof_date": str(asof_date)},
        )
        raise
    if df.empty:
        return df
    df["technical_snapshot_date"] = normalize_timestamp(df["technical_snapshot_date"])
    df["symbol"] = df["symbol"].astype("string").str.upper()
    return df


def technical_entry_confirmed_from_evaluation(evaluation: dict[str, Any]) -> bool:
    state = str(evaluation.get("technical_state") or "").strip().upper()
    trigger = str(evaluation.get("technical_trigger_type") or "").strip()
    return bool(state == "BUY_TRIGGERED" and trigger)


def prefer_technical_feature_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Prefer latest technical features when screener rows carry stale metric names."""
    if frame.empty:
        return frame
    out = frame.copy()
    tech_columns = [column for column in out.columns if str(column).endswith("_tech")]
    for tech_column in tech_columns:
        base_column = str(tech_column)[: -len("_tech")]
        if base_column in out.columns:
            out[base_column] = out[tech_column].combine_first(out[base_column])
    return out.drop(columns=tech_columns, errors="ignore")


def load_intraday(asof_date: pd.Timestamp) -> pd.DataFrame:
    if not table_exists(INTRADAY_FEATURES_TABLE):
        return pd.DataFrame()
    try:
        df = sql_to_df(
            f"""
            SELECT DISTINCT ON (symbol)
                asof_date AS intraday_snapshot_date,
                symbol,
                company_master_id,
                interval_minutes,
                bar_count,
                session_open,
                session_high,
                session_low,
                session_close,
                session_volume,
                intraday_vwap,
                intraday_range_pct,
                intraday_open_to_close_pct,
                intraday_close_vs_vwap_pct,
                intraday_pct_bars_above_vwap,
                intraday_close_location_pct,
                intraday_opening_range_high,
                intraday_opening_range_low,
                intraday_opening_range_breakout_up,
                intraday_opening_range_breakout_down,
                intraday_prev_day_high,
                intraday_prev_day_low,
                intraday_prev_day_breakout_up,
                intraday_failed_prev_day_breakout,
                intraday_first_30m_return_pct,
                intraday_last_60m_return_pct,
                intraday_volume_vs_20d,
                intraday_breakout_score,
                intraday_pattern_label,
                model_name,
                model_score,
                load_ts
            FROM {INTRADAY_FEATURES_TABLE}
            WHERE asof_date <= %s
              AND interval_minutes = 1
            ORDER BY symbol, asof_date DESC
            """,
            params=(asof_date,),
        )
    except Exception as exc:
        _record_rule_engine_fallback(
            fallback_type="rule_engine_intraday_load_failed",
            source=INTRADAY_FEATURES_TABLE,
            reason="Rule engine could not load latest intraday feature rows.",
            error=exc,
            metadata={"asof_date": str(asof_date)},
        )
        raise
    if df.empty:
        return df
    df["intraday_snapshot_date"] = normalize_timestamp(df["intraday_snapshot_date"])
    df["symbol"] = df["symbol"].astype("string").str.upper()
    return df


def load_fundamentals(asof_date: pd.Timestamp) -> pd.DataFrame:
    try:
        df = sql_to_df(
            """
            SELECT DISTINCT ON (symbol)
                asof_date AS fundamentals_snapshot_date,
                *
            FROM advisory_fundamentals_daily
            WHERE asof_date <= %s
            ORDER BY symbol, asof_date DESC
            """,
            params=(asof_date,),
        )
    except Exception as exc:
        _record_rule_engine_fallback(
            fallback_type="rule_engine_fundamentals_load_failed",
            source="advisory_fundamentals_daily",
            reason="Rule engine could not load latest fundamental snapshot rows.",
            error=exc,
            metadata={"asof_date": str(asof_date)},
        )
        raise
    if df.empty:
        return df
    df["fundamentals_snapshot_date"] = normalize_timestamp(df["fundamentals_snapshot_date"])
    df["symbol"] = df["symbol"].astype("string").str.upper()
    return df


def refresh_missing_snapshots(
    symbols: list[str],
    effective_date: pd.Timestamp,
    *,
    include_intraday: bool = True,
) -> dict[str, object]:
    sync_result = ensure_advisory_symbol_inputs(symbols, to_date=effective_date)
    peer_sync_result = sync_peer_data(symbols=symbols, to_date=effective_date)

    technical_df = build_technical_features(symbols=symbols, from_date=effective_date, to_date=effective_date, rebuild=False)
    persist_technical_features(technical_df, rebuild=False, symbols=symbols)

    intraday_df = pd.DataFrame()
    intraday_meta: dict[str, object] = {
        "status": "skipped",
        "reason": "intraday_refresh_disabled",
    }
    if include_intraday:
        intraday_df, intraday_meta = build_intraday_features(symbols=symbols, asof_date=effective_date)
        persist_intraday_features(intraday_df, rebuild=False, asof_date=effective_date)

    fundamentals_df = build_fundamental_snapshot(symbols=symbols, from_date=effective_date, to_date=effective_date, rebuild=False)
    persist_fundamental_snapshot(fundamentals_df)

    return {
        "data_sync": sync_result,
        "peer_sync": peer_sync_result,
        "technical_rows": int(len(technical_df)),
        "intraday_rows": int(len(intraday_df)),
        "intraday_meta": intraday_meta,
        "fundamental_rows": int(len(fundamentals_df)),
    }


def _days_stale_from_today(value: pd.Timestamp | None) -> int | None:
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        return None
    today = pd.Timestamp.now(tz=timezone.utc).normalize()
    return int((today - ts.normalize()).days)


def compare(value: Any, operator: str, threshold: Any) -> bool:
    if pd.isna(value):
        return False
    if operator == "eq":
        return value == threshold
    if operator == "gte":
        return value >= threshold
    if operator == "gt":
        return value > threshold
    if operator == "lte":
        return value <= threshold
    if operator == "lt":
        return value < threshold
    raise ValueError(f"Unsupported operator: {operator}")


def score_rule(value: Any, operator: str, threshold: Any) -> float:
    if pd.isna(value):
        return 0.0
    if operator == "eq":
        return 1.0 if value == threshold else 0.0
    numeric_value = safe_float(value)
    numeric_threshold = safe_float(threshold)
    if numeric_value is None or numeric_threshold is None:
        return 0.0
    if operator in {"gte", "gt"}:
        if numeric_value >= numeric_threshold:
            return 1.0
        gap = abs(numeric_threshold - numeric_value)
        scale = max(abs(numeric_threshold), 1.0)
        return max(0.0, 1.0 - (gap / scale))
    if operator in {"lte", "lt"}:
        if numeric_value <= numeric_threshold:
            return 1.0
        gap = abs(numeric_value - numeric_threshold)
        scale = max(abs(numeric_threshold), 1.0)
        return max(0.0, 1.0 - (gap / scale))
    return 0.0


def average_score(values: list[float], default: float = 0.5) -> float:
    usable = [float(value) for value in values if value is not None]
    if not usable:
        return default
    return round(sum(usable) / len(usable), 6)


def build_rejection(reason_code: str, reason_detail: str, *, severity: str = "hard", is_near_miss: bool = False, delta_to_pass: float | None = None) -> dict[str, Any]:
    return {
        "reason_code": reason_code,
        "reason_detail": reason_detail,
        "severity": severity,
        "is_near_miss": bool(is_near_miss),
        "delta_to_pass": None if delta_to_pass is None else round(float(delta_to_pass), 6),
    }


def compute_regime_fit_score(regime_name: str, setup: dict[str, Any]) -> float:
    policy = setup.get("regime_policy") or {}
    if regime_name in set(policy.get("preferred") or []):
        return 1.0
    if regime_name in set(policy.get("acceptable") or []):
        return 0.7
    if regime_name in set(policy.get("avoid") or []):
        return 0.25
    if regime_name in set(setup.get("allowed_regimes") or []):
        return 0.6
    return 0.0


def overlay_is_allowed(overlay_name: str, setup: dict[str, Any]) -> bool:
    allowed = {str(value).upper() for value in (setup.get("allowed_overlays") or []) if value}
    blocked = {str(value).upper() for value in (setup.get("blocked_overlays") or []) if value}
    overlay_upper = str(overlay_name or "NONE").upper()
    if overlay_upper in blocked:
        return False
    if allowed and overlay_upper not in allowed:
        return False
    return True


def overlay_is_explicitly_blocked(overlay_name: str, setup: dict[str, Any]) -> bool:
    blocked = {str(value).upper() for value in (setup.get("blocked_overlays") or []) if value}
    return str(overlay_name or "NONE").upper() in blocked


def regime_is_explicitly_blocked(regime_name: str, setup: dict[str, Any]) -> bool:
    blocked = {str(value).upper() for value in (setup.get("blocked_regimes") or []) if value}
    return str(regime_name or "").upper() in blocked


def is_context_only_soft_failure(value: Any) -> bool:
    text = str(value or "").strip().lower()
    if text.startswith("regime:") and text.endswith("_not_preferred"):
        return True
    return text.endswith("_blocked_context_only") and (text.startswith("regime:") or text.startswith("overlay:"))


def regime_label_hard_block_enabled() -> bool:
    value = str(os.getenv(RULE_ENGINE_REGIME_LABEL_HARD_BLOCK_ENABLED_ENV, "") or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def overlay_label_hard_block_enabled() -> bool:
    value = str(os.getenv(RULE_ENGINE_OVERLAY_LABEL_HARD_BLOCK_ENABLED_ENV, "") or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def require_regime_context() -> bool:
    value = str(os.getenv(RULE_ENGINE_REQUIRE_REGIME_CONTEXT_ENV, "") or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def regime_fit_weight_multiplier() -> float:
    raw = str(os.getenv(RULE_ENGINE_REGIME_FIT_WEIGHT_MULTIPLIER_ENV, str(DEFAULT_REGIME_FIT_WEIGHT_MULTIPLIER)) or "").strip()
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        record_local_fallback_event(
            module="advisory.rule_engine",
            fallback_type="rule_engine_regime_fit_weight_multiplier_parse_failed",
            source=RULE_ENGINE_REGIME_FIT_WEIGHT_MULTIPLIER_ENV,
            severity="warn",
            reason="Rule engine could not parse regime-fit weight multiplier and used the default bounded multiplier.",
            error=exc,
            metadata={"raw_value": raw, "default_multiplier": DEFAULT_REGIME_FIT_WEIGHT_MULTIPLIER},
        )
        return DEFAULT_REGIME_FIT_WEIGHT_MULTIPLIER
    if value < 0:
        return 0.0
    if value > 1:
        return 1.0
    return value


def component_age_days(candidate_asof_date: pd.Timestamp | None, source_asof_date: Any) -> int | None:
    if candidate_asof_date is None:
        return None
    source_ts = pd.to_datetime(source_asof_date, utc=True, errors="coerce")
    if pd.isna(source_ts):
        return None
    return int((candidate_asof_date.normalize() - source_ts.normalize()).days)


def evaluate_freshness(
    row: pd.Series,
    *,
    candidate_asof_date: pd.Timestamp,
    setup: dict[str, Any],
) -> list[dict[str, Any]]:
    policy = {**DEFAULT_FRESHNESS_POLICY, **(setup.get("freshness_policy") or {})}
    rejections: list[dict[str, Any]] = []

    technical_age = component_age_days(candidate_asof_date, row.get("technical_asof_date"))
    if technical_age is None:
        rejections.append(build_rejection("missing_technical_snapshot", "technical snapshot missing", severity="hard"))
    elif technical_age > int(policy.get("technical_max_age_days", DEFAULT_FRESHNESS_POLICY["technical_max_age_days"])):
        rejections.append(build_rejection("technical_snapshot_stale", f"technical_age_days={technical_age}", severity="hard"))

    fundamentals_age = component_age_days(candidate_asof_date, row.get("fundamentals_asof_date"))
    fundamentals_required = bool(policy.get("fundamentals_required", DEFAULT_FRESHNESS_POLICY["fundamentals_required"]))
    if fundamentals_age is None:
        if fundamentals_required:
            rejections.append(build_rejection("missing_fundamental_snapshot", "fundamental snapshot missing", severity="hard"))
    elif fundamentals_age > int(policy.get("fundamentals_max_age_days", DEFAULT_FRESHNESS_POLICY["fundamentals_max_age_days"])):
        severity = "hard" if fundamentals_required else "soft"
        rejections.append(build_rejection("fundamental_snapshot_stale", f"fundamentals_age_days={fundamentals_age}", severity=severity))

    regime_age = component_age_days(candidate_asof_date, row.get("regime_asof_date"))
    if regime_age is None:
        if require_regime_context():
            rejections.append(build_rejection("missing_regime_snapshot", "regime snapshot missing", severity="hard"))
        else:
            rejections.append(
                build_rejection(
                    "missing_regime_snapshot_context_only",
                    f"regime snapshot missing; hard block disabled by {RULE_ENGINE_REQUIRE_REGIME_CONTEXT_ENV}=false",
                    severity="soft",
                )
            )
    elif regime_age > int(policy.get("regime_max_age_days", DEFAULT_FRESHNESS_POLICY["regime_max_age_days"])):
        if require_regime_context():
            rejections.append(build_rejection("regime_snapshot_stale", f"regime_age_days={regime_age}", severity="hard"))
        else:
            rejections.append(
                build_rejection(
                    "regime_snapshot_stale_context_only",
                    f"regime_age_days={regime_age}; hard block disabled by {RULE_ENGINE_REQUIRE_REGIME_CONTEXT_ENV}=false",
                    severity="soft",
                )
            )

    intraday_mode = str(setup.get("intraday_usage_mode") or "confirm_only").lower()
    intraday_age = component_age_days(candidate_asof_date, row.get("intraday_asof_date"))
    intraday_limit = int(policy.get("intraday_max_age_days", DEFAULT_FRESHNESS_POLICY["intraday_max_age_days"]))
    if intraday_mode == "tactical_primary":
        if intraday_age is None:
            rejections.append(build_rejection("missing_intraday_snapshot", "intraday confirmation snapshot missing", severity="hard"))
        elif intraday_age > intraday_limit:
            rejections.append(build_rejection("intraday_snapshot_stale", f"intraday_age_days={intraday_age}", severity="hard"))
    elif intraday_age is not None and intraday_age > intraday_limit:
        rejections.append(build_rejection("intraday_snapshot_stale", f"intraday_age_days={intraday_age}", severity="soft"))

    return rejections


def intraday_positive_signal(row: pd.Series) -> bool:
    breakout_score = safe_float(row.get("intraday_breakout_score"))
    close_vs_vwap_pct = safe_float(row.get("intraday_close_vs_vwap_pct"))
    close_location_pct = safe_float(row.get("intraday_close_location_pct"))
    failed_breakout = bool(row.get("intraday_failed_prev_day_breakout"))
    return bool(
        not failed_breakout
        and breakout_score is not None
        and breakout_score >= 0.55
        and close_vs_vwap_pct is not None
        and close_vs_vwap_pct >= 0.0
        and close_location_pct is not None
        and close_location_pct >= 0.55
    )


def intraday_negative_signal(row: pd.Series) -> bool:
    breakout_score = safe_float(row.get("intraday_breakout_score"))
    close_vs_vwap_pct = safe_float(row.get("intraday_close_vs_vwap_pct"))
    failed_breakout = bool(row.get("intraday_failed_prev_day_breakout"))
    return bool(failed_breakout or (breakout_score is not None and breakout_score < 0.30) or (close_vs_vwap_pct is not None and close_vs_vwap_pct < -0.25))


def get_freshness_policy(setup: dict[str, Any]) -> dict[str, Any]:
    policy = dict(DEFAULT_FRESHNESS_POLICY)
    policy.update(dict(setup.get("freshness_policy") or {}))
    return policy


def get_intraday_usage_mode(setup: dict[str, Any]) -> str:
    return str(setup.get("intraday_usage_mode") or "confirm_only").lower()


def snapshot_age_days(snapshot_date: Any, asof_date: Any) -> int | None:
    snapshot_ts = pd.to_datetime(snapshot_date, utc=True, errors="coerce")
    asof_ts = pd.to_datetime(asof_date, utc=True, errors="coerce")
    if pd.isna(snapshot_ts) or pd.isna(asof_ts):
        return None
    return int((asof_ts.normalize() - snapshot_ts.normalize()).days)


def compute_component_scores(row: pd.Series, *, regime_name: str, setup: dict[str, Any]) -> dict[str, Any]:
    technical_engine_eval = evaluate_technical_pre_entry_state(
        row,
        thresholds=(setup.get("technical_thresholds") or setup.get("score_thresholds") or {}),
        archetype=str(setup.get("entry_archetype") or "base_breakout"),
    )
    intraday_usage_mode = get_intraday_usage_mode(setup)
    technical_rule_defs = list(setup.get("technical_rules", []))
    if intraday_usage_mode != "none":
        technical_rule_defs += list(setup.get("intraday_rules", []))
    technical_scores = [score_rule(row.get(rule["column"]), rule["operator"], rule["value"]) for rule in technical_rule_defs]
    if technical_scores:
        technical_score = average_score(
            technical_scores + [(float(technical_engine_eval["technical_total_score"]) / 100.0)],
            default=0.0,
        )
    else:
        technical_score = float(technical_engine_eval["technical_total_score"]) / 100.0
    intraday_available = any(
        not pd.isna(row.get(column))
        for column in [
            "intraday_close_vs_vwap_pct",
            "intraday_pct_bars_above_vwap",
            "intraday_close_location_pct",
            "intraday_volume_vs_20d",
            "intraday_breakout_score",
        ]
    )
    if intraday_available or any(
        bool(row.get(column))
        for column in [
            "intraday_opening_range_breakout_up",
            "intraday_prev_day_breakout_up",
            "intraday_failed_prev_day_breakout",
        ]
    ):
        intraday_overlay_score = average_score(
            [
                score_rule(row.get("intraday_close_vs_vwap_pct"), "gt", 0.0),
                score_rule(row.get("intraday_pct_bars_above_vwap"), "gte", 0.55),
                score_rule(row.get("intraday_close_location_pct"), "gte", 0.65),
                1.0 if bool(row.get("intraday_opening_range_breakout_up")) else 0.25,
                1.0 if bool(row.get("intraday_prev_day_breakout_up")) else 0.25,
                0.0 if bool(row.get("intraday_failed_prev_day_breakout")) else 1.0,
                score_rule(row.get("intraday_volume_vs_20d"), "gte", 0.8),
                score_rule(row.get("intraday_breakout_score"), "gte", 0.55),
            ]
        )
        technical_score = average_score([technical_score, intraday_overlay_score], default=technical_score)

    fundamental_rules = setup.get("fundamental_rules", [])
    if fundamental_rules:
        fundamental_score = average_score([score_rule(row.get(rule["column"]), rule["operator"], rule["value"]) for rule in fundamental_rules], default=0.0)
    else:
        fundamental_score = average_score(
            [
                score_rule(row.get("total_revenue_qoq_growth_vs_sector"), "gte", 0.0),
                score_rule(row.get("profit_after_tax_qoq_growth_vs_sector"), "gte", 0.0),
                score_rule(row.get("debt_to_equity_vs_sector"), "lte", 0.25),
                score_rule(row.get("promoter_total_vs_sector"), "gte", 0.0),
                score_rule(row.get("fii_vs_sector"), "gte", 0.0),
            ]
        )

    regime_fit_score = compute_regime_fit_score(regime_name, setup)
    event_score = 0.5
    weights = {**DEFAULT_SCORING_WEIGHTS, **(setup.get("scoring_weights") or {})}
    weights["regime_fit"] = float(weights.get("regime_fit", 0.0)) * regime_fit_weight_multiplier()
    total_weight = sum(float(value) for value in weights.values()) or 1.0
    setup_score = (
        (technical_score * float(weights["technical"]))
        + (fundamental_score * float(weights["fundamental"]))
        + (regime_fit_score * float(weights["regime_fit"]))
        + (event_score * float(weights["event"]))
    ) / total_weight
    return {
        "technical_state": str(technical_engine_eval["technical_state"]),
        "technical_trigger_type": technical_engine_eval.get("entry_trigger_type"),
        "technical_trigger_note": technical_engine_eval.get("entry_trigger_note"),
        "technical_setup_archetype": technical_engine_eval.get("technical_setup_archetype"),
        "technical_setup_quality_json": json.dumps(
            technical_engine_eval.get("technical_setup_quality") or {},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ),
        "technical_trend_score": round(float(technical_engine_eval["trend_score"]), 6),
        "technical_structure_score": round(float(technical_engine_eval["structure_score"]), 6),
        "technical_participation_score": round(float(technical_engine_eval["participation_score"]), 6),
        "technical_relative_strength_score": round(float(technical_engine_eval["relative_strength_score"]), 6),
        "technical_tradability_score": round(float(technical_engine_eval["tradability_score"]), 6),
        "technical_score": round(technical_score, 6),
        "fundamental_score": round(fundamental_score, 6),
        "regime_fit_score": round(regime_fit_score, 6),
        "regime_fit_weight_effective": round(float(weights["regime_fit"]), 6),
        "event_score": round(event_score, 6),
        "setup_score": round(setup_score, 6),
    }


def compute_invalidation_price(row: pd.Series) -> float | None:
    adj_close = safe_float(row.get("adj_close"))
    atr_20 = safe_float(row.get("atr_20"))
    dma_20 = safe_float(row.get("dma_20"))
    dma_50 = safe_float(row.get("dma_50"))
    anchors = [value for value in [dma_20, dma_50] if value is not None]
    if adj_close is not None and atr_20 is not None:
        anchors.append(adj_close - (2.0 * atr_20))
    if not anchors:
        return None
    return round(max(anchors), 2)


def build_entry_plan(row: pd.Series, candidate_state: str, technical_trigger_type: str | None = None) -> dict[str, Any]:
    adj_close = safe_float(row.get("adj_close"))
    atr_20 = safe_float(row.get("atr_20")) or 0.0
    dma_20 = safe_float(row.get("dma_20"))
    dma_50 = safe_float(row.get("dma_50"))
    invalidation_price = compute_invalidation_price(row)
    trigger = str(technical_trigger_type or "").lower()

    if candidate_state == "WATCH_PULLBACK":
        anchor = dma_20 if dma_20 is not None and adj_close is not None and adj_close >= dma_20 else dma_50
        style = "PULLBACK_TO_20DMA" if anchor == dma_20 else "PULLBACK_TO_50DMA"
        if anchor is None:
            return {
                "entry_style": style,
                "attractive_price_low": None,
                "attractive_price_high": None,
                "invalidation_price": invalidation_price,
                "entry_note": "Wait for a pullback into the moving-average support zone.",
            }
        return {
            "entry_style": style,
            "attractive_price_low": round(anchor - (0.5 * atr_20), 2),
            "attractive_price_high": round(anchor + (0.25 * atr_20), 2),
            "invalidation_price": invalidation_price,
            "entry_note": "Good name but extended; wait for a controlled pullback into support.",
        }

    if candidate_state == "WATCH_BREAKOUT":
        if trigger == "trend_pullback":
            anchor = dma_20 if dma_20 is not None and adj_close is not None and adj_close >= dma_20 else dma_50
            style = "PULLBACK_TO_20DMA" if anchor == dma_20 else "PULLBACK_TO_50DMA"
            if anchor is None:
                return {
                    "entry_style": style,
                    "attractive_price_low": None,
                    "attractive_price_high": None,
                    "invalidation_price": invalidation_price,
                    "entry_note": "Trend pullback is forming; wait for a clean hold near moving-average support.",
                }
            return {
                "entry_style": style,
                "attractive_price_low": round(anchor - (0.4 * atr_20), 2),
                "attractive_price_high": round(anchor + (0.2 * atr_20), 2),
                "invalidation_price": invalidation_price,
                "entry_note": "Trend pullback setup is constructive; buy only if support holds and price turns back up.",
            }
        if trigger == "breakout_retest":
            pivot_anchor = adj_close if adj_close is not None else dma_20
            low = None if pivot_anchor is None else round(max(pivot_anchor - (0.5 * atr_20), 0.0), 2)
            high = None if pivot_anchor is None else round(pivot_anchor + (0.15 * atr_20), 2)
            return {
                "entry_style": "BREAKOUT_RETEST",
                "attractive_price_low": low,
                "attractive_price_high": high,
                "invalidation_price": invalidation_price,
                "entry_note": "Breakout-retest setup is forming; wait for a confirmed hold around the pivot zone.",
            }
        if trigger == "reclaim":
            if adj_close is None:
                return {
                    "entry_style": "RECLAIM_ENTRY",
                    "attractive_price_low": None,
                    "attractive_price_high": None,
                    "invalidation_price": invalidation_price,
                    "entry_note": "Reclaim setup forming; wait for strong follow-through above the reclaimed level.",
                }
            return {
                "entry_style": "RECLAIM_ENTRY",
                "attractive_price_low": round(max(adj_close - (0.2 * atr_20), 0.0), 2),
                "attractive_price_high": round(adj_close + (0.35 * atr_20), 2),
                "invalidation_price": invalidation_price,
                "entry_note": "Price is reclaiming a key level; buy only if the reclaim holds and confirms.",
            }
        if adj_close is None:
            return {
                "entry_style": "BREAKOUT_PIVOT",
                "attractive_price_low": None,
                "attractive_price_high": None,
                "invalidation_price": invalidation_price,
                "entry_note": "Setup forming; wait for a clean breakout trigger.",
            }
        return {
            "entry_style": "BREAKOUT_PIVOT",
            "attractive_price_low": round(adj_close + (0.1 * atr_20), 2),
            "attractive_price_high": round(adj_close + (0.6 * atr_20), 2),
            "invalidation_price": invalidation_price,
            "entry_note": "Quality setup forming; buy only on confirmation through the pivot zone.",
        }

    if candidate_state == "WATCH_EVENT":
        return {
            "entry_style": "RETEST_OF_PRIOR_BREAKOUT",
            "attractive_price_low": dma_20,
            "attractive_price_high": adj_close,
            "invalidation_price": invalidation_price,
            "entry_note": "Event-sensitive setup; wait for announcement, results, or order-flow confirmation.",
        }

    if candidate_state == "PASS_NOW":
        if trigger == "breakout_retest":
            if adj_close is None:
                return {
                    "entry_style": "BREAKOUT_RETEST",
                    "attractive_price_low": None,
                    "attractive_price_high": None,
                    "invalidation_price": invalidation_price,
                    "entry_note": "Entry acceptable now on a confirmed breakout-retest hold.",
                }
            return {
                "entry_style": "BREAKOUT_RETEST",
                "attractive_price_low": round(max(adj_close - (0.2 * atr_20), 0.0), 2),
                "attractive_price_high": round(adj_close + (0.25 * atr_20), 2),
                "invalidation_price": invalidation_price,
                "entry_note": "Entry acceptable now on the retest hold; size around the pivot band.",
            }
        if trigger == "trend_pullback":
            anchor = dma_20 if dma_20 is not None and adj_close is not None and adj_close >= dma_20 else dma_50
            style = "PULLBACK_TO_20DMA" if anchor == dma_20 else "PULLBACK_TO_50DMA"
            if anchor is None:
                return {
                    "entry_style": style,
                    "attractive_price_low": None,
                    "attractive_price_high": None,
                    "invalidation_price": invalidation_price,
                    "entry_note": "Entry acceptable now if the pullback continues to respect support.",
                }
            return {
                "entry_style": style,
                "attractive_price_low": round(max(anchor - (0.35 * atr_20), 0.0), 2),
                "attractive_price_high": round(anchor + (0.2 * atr_20), 2),
                "invalidation_price": invalidation_price,
                "entry_note": "Entry acceptable now on a constructive trend pullback into support.",
            }
        if trigger == "reclaim":
            if adj_close is None:
                return {
                    "entry_style": "RECLAIM_ENTRY",
                    "attractive_price_low": None,
                    "attractive_price_high": None,
                    "invalidation_price": invalidation_price,
                    "entry_note": "Entry acceptable now on a strong reclaim setup.",
                }
            return {
                "entry_style": "RECLAIM_ENTRY",
                "attractive_price_low": round(max(adj_close - (0.2 * atr_20), 0.0), 2),
                "attractive_price_high": round(adj_close + (0.4 * atr_20), 2),
                "invalidation_price": invalidation_price,
                "entry_note": "Entry acceptable now on reclaim follow-through.",
            }
        if adj_close is None:
            return {
                "entry_style": "BREAKOUT_PIVOT",
                "attractive_price_low": None,
                "attractive_price_high": None,
                "invalidation_price": invalidation_price,
                "entry_note": "Entry acceptable now subject to liquidity and execution review.",
            }
        return {
            "entry_style": "BREAKOUT_PLUS_EXTENSION_BAND",
            "attractive_price_low": round(max(adj_close - (0.25 * atr_20), 0.0), 2),
            "attractive_price_high": round(adj_close + (0.5 * atr_20), 2),
            "invalidation_price": invalidation_price,
            "entry_note": "Entry acceptable now within the current breakout band.",
        }

    if candidate_state == "ABSTAIN":
        return {
            "entry_style": None,
            "attractive_price_low": None,
            "attractive_price_high": None,
            "invalidation_price": invalidation_price,
            "entry_note": "Do nothing for now; edge is too weak or mixed to justify monitoring or allocation.",
        }

    return {
        "entry_style": None,
        "attractive_price_low": None,
        "attractive_price_high": None,
        "invalidation_price": invalidation_price,
        "entry_note": None,
    }


def map_technical_state_to_candidate_state(technical_state: str | None) -> str:
    state = str(technical_state or "").upper()
    if state == "BUY_TRIGGERED":
        return "PASS_NOW"
    if state in {"READY", "NEAR_PIVOT"}:
        return "WATCH_BREAKOUT"
    if state == "WATCHLIST":
        return "WATCH_EVENT"
    if state == "IGNORE":
        return "ABSTAIN"
    return "REJECT"


def evaluate_setup_row(row: pd.Series, *, regime_name: str, overlay_name: str, setup: dict[str, Any], admission: dict[str, Any] | None = None) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    thresholds = {**DEFAULT_SCORE_THRESHOLDS, **(setup.get("score_thresholds") or {})}
    freshness_policy = get_freshness_policy(setup)
    intraday_usage_mode = get_intraday_usage_mode(setup)
    rejections: list[dict[str, Any]] = []
    soft_failures: list[str] = []

    if regime_is_explicitly_blocked(regime_name, setup):
        if regime_label_hard_block_enabled():
            rejections.append(build_rejection("regime_blocked", f"regime={regime_name}", severity="hard"))
        else:
            soft_failures.append(f"regime:{str(regime_name or '').lower()}_blocked_context_only")
            rejections.append(
                build_rejection(
                    "regime_blocked_context_only",
                    f"regime={regime_name}; hard block disabled by {RULE_ENGINE_REGIME_LABEL_HARD_BLOCK_ENABLED_ENV}=false",
                    severity="soft",
                )
            )
    elif regime_name not in set(setup.get("allowed_regimes") or []):
        soft_failures.append(f"regime:{regime_name.lower()}_not_preferred")
    if overlay_is_explicitly_blocked(overlay_name, setup):
        if overlay_label_hard_block_enabled():
            rejections.append(build_rejection("overlay_not_allowed", f"overlay={overlay_name}", severity="hard"))
        else:
            soft_failures.append(f"overlay:{str(overlay_name or 'NONE').lower()}_blocked_context_only")
            rejections.append(
                build_rejection(
                    "overlay_blocked_context_only",
                    f"overlay={overlay_name}; hard block disabled by {RULE_ENGINE_OVERLAY_LABEL_HARD_BLOCK_ENABLED_ENV}=false",
                    severity="soft",
                )
            )
    elif not overlay_is_allowed(overlay_name, setup):
        soft_failures.append(f"overlay:{str(overlay_name or 'NONE').lower()}_not_preferred")

    if pd.isna(row.get("company_master_id")):
        rejections.append(build_rejection("missing_company_master_id", "company_master_id is null", severity="hard"))

    if pd.isna(row.get("adj_close")):
        rejections.append(build_rejection("missing_technical_snapshot", "technical snapshot missing for date", severity="hard"))

    technical_age_days = snapshot_age_days(row.get("technical_snapshot_date"), row.get("asof_date"))
    fundamentals_age_days = snapshot_age_days(row.get("fundamentals_snapshot_date"), row.get("asof_date"))
    intraday_age_days = snapshot_age_days(row.get("intraday_snapshot_date"), row.get("asof_date"))
    regime_age_days = snapshot_age_days(row.get("regime_snapshot_date"), row.get("asof_date"))

    if technical_age_days is not None and technical_age_days > int(freshness_policy["technical_max_age_days"]):
        soft_failures.append(f"technical:stale_{technical_age_days}d")
    if regime_age_days is not None and regime_age_days > int(freshness_policy["regime_max_age_days"]):
        soft_failures.append(f"regime:stale_{regime_age_days}d")

    fundamentals_required = bool(freshness_policy.get("fundamentals_required", True))
    if pd.isna(row.get("fundamentals_freshness_status")) and fundamentals_required:
        rejections.append(build_rejection("missing_fundamental_snapshot", "fundamental snapshot missing for date", severity="hard"))

    # Admission tolerances flex with the recorded regime admission state (top-of-funnel width
    # only; entry confirmation and risk gates never flex). Neutral defaults reproduce the
    # previous hardcoded literals exactly. The applied tolerance + state is recorded in the
    # rejection detail so the regret ledger's gate rows carry the policy that produced them.
    admission = admission or {}
    admission_state = str(admission.get("state") or "neutral")
    mcap_below_tolerance = float(admission.get("market_cap_below_tolerance") or 0.70)
    mcap_above_tolerance = float(admission.get("market_cap_above_tolerance") or 1.30)
    liquidity_floor_multiplier = float(admission.get("liquidity_floor_multiplier") or 0.50)

    # Unknown size data is NOT the same as failing the band: whole-market admissions
    # (scan/hypothesis/theme) often lack a snapshot market cap, and hard-rejecting on
    # absence gates by missing data rather than by evidence. Known-bad still hard-rejects;
    # unknown becomes a visible soft failure (env-gated back to the old behavior).
    unknown_size_hard = os.getenv("RULE_ENGINE_UNKNOWN_SIZE_HARD_REJECT", "false").strip().lower() in {"1", "true", "yes"}

    market_cap = safe_float(row.get("market_cap"))
    market_cap_min = safe_float(setup.get("market_cap_min"))
    market_cap_max = safe_float(setup.get("market_cap_max"))
    if (market_cap_min is not None or market_cap_max is not None) and market_cap is None:
        if unknown_size_hard:
            rejections.append(build_rejection("market_cap_below_min", f"market_cap=None admission_state={admission_state}", severity="hard"))
        else:
            soft_failures.append("market_cap:unknown")
    if market_cap is not None and market_cap_min is not None:
        if market_cap < (market_cap_min * mcap_below_tolerance):
            rejections.append(build_rejection("market_cap_below_min", f"market_cap={market_cap} tolerance={mcap_below_tolerance} admission_state={admission_state}", severity="hard"))
        elif market_cap < market_cap_min:
            soft_failures.append("market_cap:below_target")
    if market_cap is not None and market_cap_max is not None:
        if market_cap > (market_cap_max * mcap_above_tolerance):
            rejections.append(build_rejection("market_cap_above_max", f"market_cap={market_cap} tolerance={mcap_above_tolerance} admission_state={admission_state}", severity="hard"))
        elif market_cap > market_cap_max:
            soft_failures.append("market_cap:above_target")

    traded_value = safe_float(row.get("avg_traded_value_20d"))
    min_liquidity = safe_float(setup.get("min_avg_traded_value_20d"))
    if min_liquidity is not None:
        if traded_value is None:
            if unknown_size_hard:
                rejections.append(build_rejection("liquidity_far_below_min", f"avg_traded_value_20d=None admission_state={admission_state}", severity="hard"))
            else:
                soft_failures.append("liquidity:unknown")
        elif traded_value < (min_liquidity * liquidity_floor_multiplier):
            rejections.append(build_rejection("liquidity_far_below_min", f"avg_traded_value_20d={traded_value} floor_multiplier={liquidity_floor_multiplier} admission_state={admission_state}", severity="hard"))

    extension = safe_float(row.get("breakout_extension_pct"))
    max_extension = safe_float(setup.get("max_breakout_extension_pct"))
    if max_extension is not None and (extension is None or extension > (max_extension + 5.0)):
        rejections.append(build_rejection("overextended_breakout", f"breakout_extension_pct={extension}", severity="hard"))

    scores = compute_component_scores(row, regime_name=regime_name, setup=setup)
    technical_state = str(scores.get("technical_state") or "")
    technical_trigger_type = scores.get("technical_trigger_type")
    technical_trigger_note = scores.get("technical_trigger_note")
    score_gap = max(0.0, float(thresholds["pass_now"]) - float(scores["setup_score"]))
    near_miss_flag = score_gap > 0.0 and score_gap <= float(thresholds["near_miss_gap"])

    dist_52w_high = safe_float(row.get("dist_52w_high"))
    watch_pullback_extension_pct = safe_float(setup.get("watch_pullback_extension_pct"))
    if fundamentals_age_days is not None and fundamentals_age_days > int(freshness_policy["fundamentals_max_age_days"]):
        soft_failures.append(f"fundamental:stale_{fundamentals_age_days}d")
    intraday_missing = pd.isna(row.get("intraday_snapshot_date"))
    intraday_stale = intraday_age_days is not None and intraday_age_days > int(freshness_policy["intraday_max_age_days"])
    for rule in setup.get("technical_rules", []):
        if not compare(row.get(rule["column"]), rule["operator"], rule["value"]):
            soft_failures.append(f"technical:{rule['column']}")
    if intraday_usage_mode != "none":
        if intraday_missing:
            if intraday_usage_mode == "tactical_primary":
                soft_failures.append("intraday:missing")
        elif intraday_stale:
            if intraday_usage_mode == "tactical_primary":
                soft_failures.append(f"intraday:stale_{intraday_age_days}d")
        for rule in setup.get("intraday_rules", []):
            if not compare(row.get(rule["column"]), rule["operator"], rule["value"]):
                soft_failures.append(f"intraday:{rule['column']}")
    for rule in setup.get("fundamental_rules", []):
        if not compare(row.get(rule["column"]), rule["operator"], rule["value"]):
            soft_failures.append(f"fundamental:{rule['column']}")
    if min_liquidity is not None and traded_value is not None and traded_value < min_liquidity:
        soft_failures.append("liquidity:below_target")
    if setup.get("min_dist_52w_high") is not None and (dist_52w_high is None or dist_52w_high < float(setup["min_dist_52w_high"])):
        soft_failures.append("technical:too_far_from_high")

    hard_rejections = [item for item in rejections if item["severity"] == "hard"]
    if hard_rejections:
        return "REJECT", {**scores, "near_miss_flag": near_miss_flag, "soft_failures": soft_failures}, hard_rejections

    technical_hard_reject = not bool(row.get("pass_liquidity_20d", True)) or technical_state == "REJECT"
    if technical_hard_reject:
        rejections.append(
            build_rejection(
                "technical_engine_reject",
                f"technical_state={technical_state} trigger={technical_trigger_type}",
                severity="soft",
                is_near_miss=False,
                delta_to_pass=score_gap,
            )
        )
        return "REJECT", {**scores, "near_miss_flag": near_miss_flag, "soft_failures": soft_failures}, rejections

    intraday_rule_failures = [value for value in soft_failures if value.startswith("intraday:")]
    non_intraday_soft_failures = [value for value in soft_failures if not value.startswith("intraday:")]
    threshold_soft_failures = [value for value in non_intraday_soft_failures if not is_context_only_soft_failure(value)]
    candidate_state = map_technical_state_to_candidate_state(technical_state)
    watch_reason_detail = technical_trigger_note
    if candidate_state == "ABSTAIN":
        if float(scores["setup_score"]) >= float(thresholds["pass_now"]) and len(threshold_soft_failures) <= 2:
            candidate_state = "WATCH_BREAKOUT"
            watch_reason_detail = "aggregate setup score is strong, but technical trigger is not confirmed"
            soft_failures.append("technical:trigger_not_confirmed")
            rejections.append(
                build_rejection(
                    "technical_trigger_not_confirmed",
                    f"technical_state={technical_state or 'UNKNOWN'} trigger={technical_trigger_type or 'NONE'}; aggregate score cannot create PASS_NOW without technical confirmation",
                    severity="soft",
                    is_near_miss=near_miss_flag,
                    delta_to_pass=score_gap,
                )
            )
        elif float(scores["setup_score"]) >= float(thresholds["watch_breakout"]):
            candidate_state = "WATCH_BREAKOUT"
            watch_reason_detail = "aggregate setup score is strong enough to watch for a clean trigger"
        elif float(scores["setup_score"]) >= float(thresholds["watch_event"]):
            candidate_state = "WATCH_EVENT"
            watch_reason_detail = "aggregate setup score is watchable but needs confirmation"
    if max_extension is not None and extension is not None and extension > max_extension:
        candidate_state = "WATCH_PULLBACK"
        watch_reason_detail = f"extended now at {extension:.2f}% above breakout reference"
    elif watch_pullback_extension_pct is not None and extension is not None and extension > watch_pullback_extension_pct:
        candidate_state = "WATCH_PULLBACK"
        watch_reason_detail = f"extended enough to wait for pullback at {extension:.2f}%"
    elif candidate_state == "PASS_NOW" and float(scores["setup_score"]) >= float(thresholds["pass_now"]) and len(threshold_soft_failures) <= 2:
        candidate_state = "PASS_NOW"
        watch_reason_detail = technical_trigger_note or "qualifies now with acceptable score and entry condition"
    elif candidate_state == "WATCH_BREAKOUT" and float(scores["setup_score"]) >= float(thresholds["watch_breakout"]):
        candidate_state = "WATCH_BREAKOUT"
        watch_reason_detail = technical_trigger_note or watch_reason_detail or "quality setup forming but not fully triggered"
    elif candidate_state == "WATCH_EVENT" and float(scores["setup_score"]) >= float(thresholds["watch_event"]) and len(threshold_soft_failures) <= 2:
        candidate_state = "WATCH_EVENT"
        watch_reason_detail = technical_trigger_note or "candidate needs event confirmation before entry"
    elif near_miss_flag:
        candidate_state = "WATCH_EVENT"
        watch_reason_detail = "near miss on score; keep on watch for improvement"
    elif float(scores["setup_score"]) >= float(thresholds.get("abstain", DEFAULT_SCORE_THRESHOLDS["abstain"])):
        candidate_state = "ABSTAIN"
        watch_reason_detail = "explicit abstain: setup is not broken, but edge is too weak or mixed to monitor actively"

    severe_intraday_miss = len(intraday_rule_failures) >= 2 or intraday_negative_signal(row)
    if intraday_usage_mode == "timing_only" and candidate_state == "PASS_NOW" and intraday_rule_failures:
        if severe_intraday_miss:
            candidate_state = "WATCH_BREAKOUT"
            watch_reason_detail = "daily setup qualifies but intraday timing confirmation is not ready"
        else:
            watch_reason_detail = "qualifies now; intraday timing is mixed but still acceptable"
    elif intraday_usage_mode == "confirm_only" and candidate_state == "PASS_NOW" and intraday_rule_failures:
        if severe_intraday_miss:
            candidate_state = "WATCH_BREAKOUT"
            watch_reason_detail = "daily setup is valid but intraday confirmation is still weak"
        else:
            watch_reason_detail = "qualifies now; intraday confirmation is mixed but not broken"
    elif intraday_usage_mode == "tactical_primary":
        if intraday_rule_failures:
            if float(scores["setup_score"]) >= float(thresholds["watch_breakout"]):
                candidate_state = "WATCH_BREAKOUT"
                watch_reason_detail = "intraday tactical trigger not fully confirmed yet"
            elif float(scores["setup_score"]) >= float(thresholds.get("abstain", DEFAULT_SCORE_THRESHOLDS["abstain"])):
                candidate_state = "ABSTAIN"
                watch_reason_detail = "explicit abstain: tactical trigger quality is too weak to monitor actively"
            else:
                candidate_state = "REJECT"

    if candidate_state == "ABSTAIN":
        if not watch_reason_detail:
            watch_reason_detail = "explicit abstain: setup is not broken, but edge is too weak or mixed to monitor actively"
        rejections.append(
            build_rejection(
                "abstain_low_edge",
                f"setup_score={scores['setup_score']:.4f} watch_event={float(thresholds['watch_event']):.4f} soft_failures={len(soft_failures)}",
                severity="soft",
                is_near_miss=False,
                delta_to_pass=score_gap,
            )
        )
        return "ABSTAIN", {**scores, "near_miss_flag": near_miss_flag, "watch_reason_detail": watch_reason_detail, "soft_failures": soft_failures}, rejections

    if candidate_state == "REJECT":
        rejections.append(
            build_rejection(
                "setup_score_below_threshold",
                f"setup_score={scores['setup_score']:.4f} pass_now={float(thresholds['pass_now']):.4f}",
                severity="soft",
                is_near_miss=near_miss_flag,
                delta_to_pass=score_gap,
            )
        )
        if len(soft_failures) == 1:
            rejections.append(build_rejection("single_rule_near_miss", soft_failures[0], severity="soft", is_near_miss=True, delta_to_pass=score_gap))
        return "REJECT", {**scores, "near_miss_flag": near_miss_flag, "soft_failures": soft_failures}, rejections

    if candidate_state.startswith("WATCH_") and len(soft_failures) == 1 and watch_reason_detail:
        watch_reason_detail = f"{watch_reason_detail}; near miss on {soft_failures[0]}"

    return candidate_state, {**scores, "near_miss_flag": near_miss_flag, "watch_reason_detail": watch_reason_detail, "soft_failures": soft_failures}, rejections


def run_rule_engine(
    *,
    asof_date: pd.Timestamp | None = None,
    setup_ids: list[str] | None = None,
    config_path: str | None = None,
    skip_snapshot_refresh: bool = False,
    skip_intraday_prefetch: bool = False,
    max_snapshot_refresh_age_days: int = DEFAULT_MAX_SNAPSHOT_REFRESH_AGE_DAYS,
    max_intraday_prefetch_age_days: int = DEFAULT_MAX_INTRADAY_PREFETCH_AGE_DAYS,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    effective_dates = get_effective_dates(asof_date)
    screener_date = effective_dates.get("screener_date")
    if screener_date is None:
        return pd.DataFrame(), pd.DataFrame(), {"effective_date": None, "screener_date": None, "regime_name": None}

    regime = load_regime(screener_date)
    if regime is None:
        if require_regime_context():
            return pd.DataFrame(), pd.DataFrame(), {
                "effective_date": str(screener_date),
                "screener_date": str(screener_date),
                "regime_name": None,
                "regime_context_status": "missing",
                "regime_context_reason": f"required by {RULE_ENGINE_REQUIRE_REGIME_CONTEXT_ENV}=true",
            }
        regime = missing_regime_context(screener_date)

    setups = load_setup_registry(config_path)
    if setup_ids:
        selected = {value.upper() for value in setup_ids}
        setups = [setup for setup in setups if setup["setup_id"].upper() in selected]
    else:
        setups = [setup for setup in setups if not bool(setup.get("research_only"))]

    technical = load_technical(screener_date)
    intraday = load_intraday(screener_date)
    fundamentals = load_fundamentals(screener_date)

    candidate_rows: list[dict[str, Any]] = []
    rejection_rows: list[dict[str, Any]] = []
    meta = {
        "effective_date": str(screener_date),
        "screener_date": str(screener_date),
        "regime_name": regime.get("regime_name"),
        "regime_context_status": regime.get("regime_context_status") or "available",
        "regime_context_reason": regime.get("regime_context_reason"),
        "regime_snapshot_date": str(regime.get("asof_date")) if regime.get("asof_date") is not None else None,
        "technical_snapshot_date": str(technical["technical_snapshot_date"].max()) if not technical.empty and "technical_snapshot_date" in technical.columns else None,
        "intraday_snapshot_date": str(intraday["intraday_snapshot_date"].max()) if not intraday.empty and "intraday_snapshot_date" in intraday.columns else None,
        "fundamentals_snapshot_date": str(fundamentals["fundamentals_snapshot_date"].max()) if not fundamentals.empty and "fundamentals_snapshot_date" in fundamentals.columns else None,
    }
    overlay = load_overlay(pd.to_datetime(regime.get("asof_date"), utc=True, errors="coerce") if regime.get("asof_date") is not None else screener_date, regime_name=str(regime.get("regime_name") or ""))
    overlay_name = str(overlay.get("overlay_name") or "NONE").upper()
    theme_screener_mapping = load_active_theme_screener_mapping(asof_date=screener_date)
    meta["overlay_name"] = overlay_name
    meta["overlay_reason"] = overlay.get("overlay_reason")
    meta["overlay_intensity"] = overlay.get("overlay_intensity")
    meta["overlay_snapshot_date"] = str(overlay.get("asof_date")) if overlay.get("asof_date") is not None else None
    meta["active_theme_ids"] = theme_screener_mapping.get("theme_ids") or []
    meta["theme_screeners"] = theme_screener_mapping.get("screener_slugs") or []
    meta["theme_error"] = theme_screener_mapping.get("error")
    days_stale = _days_stale_from_today(screener_date)
    meta["days_stale_from_today"] = days_stale

    # Regime admission policy: resolve ONCE per run (recorded daily row; fail-open to neutral =
    # previous hardcoded tolerances). Flexes top-of-funnel admission width only.
    from advisory.regime_admission_policy import resolve_active_policy as resolve_admission_policy

    admission = resolve_admission_policy(screener_date)
    meta["admission_state"] = admission.get("state")
    meta["admission_parameters"] = dict(admission)

    # Cross-sectional RS percentiles: one batched point-in-time lookup per run; missing map
    # (table empty / lookup failure) is neutral -- candidates simply carry no rank.
    from advisory.relative_strength import load_rs_percentiles

    rs_percentiles = load_rs_percentiles(asof_date=screener_date)
    meta["rs_percentile_symbols"] = len(rs_percentiles)

    dynamic_source_slugs = load_dynamic_source_slugs(screener_date)
    meta["dynamic_source_slugs"] = {kind: len(slugs) for kind, slugs in dynamic_source_slugs.items()}

    setup_screeners: dict[str, list[str]] = {}
    setup_screener_modes: dict[str, str] = {}
    screener_frames = []
    for setup in setups:
        active_screeners, screener_mode, active_theme_ids = resolve_setup_screeners(
            setup,
            overlay_name,
            theme_screener_mapping=theme_screener_mapping,
            dynamic_source_slugs=dynamic_source_slugs,
        )
        setup_screeners[setup["setup_id"].upper()] = active_screeners
        setup_screener_modes[setup["setup_id"].upper()] = screener_mode
        meta.setdefault("active_screeners_by_setup", {})[setup["setup_id"]] = active_screeners
        meta.setdefault("active_theme_ids_by_setup", {})[setup["setup_id"]] = active_theme_ids
        frame = load_screener_universe(screener_date, active_screeners, screener_mode=screener_mode)
        if not frame.empty:
            screener_frames.append(frame)
    screener_frames = [frame for frame in screener_frames if not frame.empty]
    if screener_frames:
        for frame in screener_frames:
            frame["asof_date"] = screener_date
        symbols_to_refresh = sorted(pd.concat(screener_frames, ignore_index=True)["symbol"].dropna().astype("string").str.upper().drop_duplicates().tolist())
        technical_symbols = technical["symbol"].dropna().astype("string").str.upper().drop_duplicates().tolist() if not technical.empty else []
        intraday_symbols = intraday["symbol"].dropna().astype("string").str.upper().drop_duplicates().tolist() if not intraday.empty else []
        fundamental_symbols = fundamentals["symbol"].dropna().astype("string").str.upper().drop_duplicates().tolist() if not fundamentals.empty else []
        available_symbols = set(technical_symbols).intersection(fundamental_symbols)
        missing_symbols = [symbol for symbol in symbols_to_refresh if symbol not in available_symbols]
        missing_intraday_symbols = [symbol for symbol in symbols_to_refresh if symbol not in set(intraday_symbols)]
        meta["missing_snapshot_symbols"] = missing_symbols
        meta["missing_intraday_symbols"] = missing_intraday_symbols
        allow_snapshot_refresh = (
            not skip_snapshot_refresh
            and (
                days_stale is None
                or int(max_snapshot_refresh_age_days) < 0
                or days_stale <= int(max_snapshot_refresh_age_days)
            )
        )
        allow_intraday_prefetch = (
            not skip_intraday_prefetch
            and (
                days_stale is None
                or int(max_intraday_prefetch_age_days) < 0
                or days_stale <= int(max_intraday_prefetch_age_days)
            )
        )
        if missing_symbols and allow_snapshot_refresh:
            meta["preflight"] = refresh_missing_snapshots(
                missing_symbols,
                screener_date,
                include_intraday=allow_intraday_prefetch,
            )
            technical = load_technical(screener_date)
            fundamentals = load_fundamentals(screener_date)
            intraday = load_intraday(screener_date)
        elif missing_symbols and not allow_snapshot_refresh:
            meta["preflight"] = {
                "status": "skipped",
                "reason": "skip_snapshot_refresh" if skip_snapshot_refresh else "historical_snapshot_refresh_disabled",
                "missing_snapshot_symbols": missing_symbols,
                "days_stale_from_today": days_stale,
                "max_snapshot_refresh_age_days": int(max_snapshot_refresh_age_days),
            }
        elif missing_intraday_symbols and allow_intraday_prefetch:
            intraday_df, intraday_meta = build_intraday_features(symbols=missing_intraday_symbols, asof_date=screener_date)
            persist_intraday_features(intraday_df, rebuild=False, asof_date=screener_date)
            intraday = load_intraday(screener_date)
            meta["intraday_preflight"] = intraday_meta
        elif missing_intraday_symbols and not allow_intraday_prefetch:
            meta["intraday_preflight"] = {
                "status": "skipped",
                "reason": "historical_intraday_prefetch_disabled" if not skip_intraday_prefetch else "skip_intraday_prefetch",
                "missing_intraday_symbols": missing_intraday_symbols,
                "days_stale_from_today": days_stale,
                "max_intraday_prefetch_age_days": int(max_intraday_prefetch_age_days),
            }

    regime_name = str(regime["regime_name"])
    for setup in setups:
        screener_slugs = setup_screeners.get(setup["setup_id"].upper(), [])
        universe = load_screener_universe(
            screener_date,
            screener_slugs,
            screener_mode=setup_screener_modes.get(setup["setup_id"].upper(), "union"),
        )
        if universe.empty:
            rejection_rows.append(
                {
                    "asof_date": screener_date,
                    "screener_date": screener_date,
                    "setup_id": setup["setup_id"],
                    "setup_name": setup["setup_name"],
                    "symbol": None,
                    "company_master_id": None,
                    "base_regime": regime_name,
                    "news_overlay": overlay_name,
                    "theme_ids": json.dumps(meta.get("active_theme_ids_by_setup", {}).get(setup["setup_id"], [])),
                    "reason_code": "missing_screener_universe",
                    "severity": "hard",
                    "is_near_miss": False,
                    "delta_to_pass": None,
                    "reason_detail": f"screener_slugs={screener_slugs}",
                    "load_ts": pd.Timestamp.utcnow(),
                }
            )
            continue
        universe = universe.copy()
        universe["asof_date"] = screener_date

        merged = universe.merge(
            technical.drop_duplicates(subset=["symbol"], keep="last"),
            on=["symbol", "company_master_id"],
            how="left",
            suffixes=("", "_tech"),
        )
        merged = prefer_technical_feature_columns(merged)
        merged = merged.merge(
            fundamentals.drop_duplicates(subset=["symbol"], keep="last"),
            on=["symbol", "company_master_id"],
            how="left",
            suffixes=("", "_fund"),
        )
        if not intraday.empty:
            merged = merged.merge(
                intraday.drop_duplicates(subset=["symbol"], keep="last"),
                on=["symbol", "company_master_id"],
                how="left",
                suffixes=("", "_intraday"),
            )
        merged["regime_snapshot_date"] = pd.to_datetime(regime.get("asof_date"), utc=True, errors="coerce")

        for _, row in merged.iterrows():
            candidate_state, evaluation, rejections = evaluate_setup_row(row, regime_name=regime_name, overlay_name=overlay_name, setup=setup, admission=admission)
            if candidate_state != "REJECT":
                entry_plan = build_entry_plan(row, candidate_state, evaluation.get("technical_trigger_type"))
                candidate_rows.append(
                    {
                        "asof_date": screener_date,
                        "screener_date": screener_date,
                        "setup_id": setup["setup_id"],
                        "setup_name": setup["setup_name"],
                        "setup_family": setup.get("setup_family"),
                        "holding_horizon_note": setup.get("holding_horizon_note"),
                        "regime_name": regime_name,
                        "base_regime": regime_name,
                        "news_overlay": overlay_name,
                        "theme_ids": json.dumps(meta.get("active_theme_ids_by_setup", {}).get(setup["setup_id"], [])),
                        "symbol": row["symbol"],
                        "company_master_id": row["company_master_id"],
                        "rs_percentile": rs_percentiles.get(str(row["symbol"] or "").strip().upper()),
                        "screener_slug": row.get("screener_slug"),
                        "source_screener_slug": row.get("source_screener_slug") or row.get("screener_slug"),
                        "source_screener_list": row.get("source_screener_list"),
                        "rank": row.get("rank"),
                        "candidate_state": candidate_state,
                        "watch_reason_detail": evaluation.get("watch_reason_detail"),
                        "soft_failures_json": json.dumps(evaluation.get("soft_failures") or []),
                        "technical_state": evaluation.get("technical_state"),
                        "technical_trigger_type": evaluation.get("technical_trigger_type"),
                        "technical_trigger_note": evaluation.get("technical_trigger_note"),
                        "technical_entry_confirmed": technical_entry_confirmed_from_evaluation(evaluation),
                        "technical_setup_archetype": evaluation.get("technical_setup_archetype"),
                        "technical_setup_quality_json": evaluation.get("technical_setup_quality_json"),
                        "technical_trend_score": evaluation.get("technical_trend_score"),
                        "technical_structure_score": evaluation.get("technical_structure_score"),
                        "technical_participation_score": evaluation.get("technical_participation_score"),
                        "technical_relative_strength_score": evaluation.get("technical_relative_strength_score"),
                        "technical_tradability_score": evaluation.get("technical_tradability_score"),
                        "technical_score": evaluation["technical_score"],
                        "fundamental_score": evaluation["fundamental_score"],
                        "regime_fit_score": evaluation["regime_fit_score"],
                        "regime_fit_weight_effective": evaluation.get("regime_fit_weight_effective"),
                        "event_score": evaluation["event_score"],
                        "setup_score": evaluation["setup_score"],
                        "avg_traded_value_20d": row.get("avg_traded_value_20d"),
                        "rs_vs_benchmark": row.get("rs_vs_benchmark"),
                        "rs_vs_sector": row.get("rs_vs_sector"),
                        "intraday_close_vs_vwap_pct": row.get("intraday_close_vs_vwap_pct"),
                        "intraday_pct_bars_above_vwap": row.get("intraday_pct_bars_above_vwap"),
                        "intraday_close_location_pct": row.get("intraday_close_location_pct"),
                        "intraday_opening_range_breakout_up": row.get("intraday_opening_range_breakout_up"),
                        "intraday_prev_day_breakout_up": row.get("intraday_prev_day_breakout_up"),
                        "intraday_failed_prev_day_breakout": row.get("intraday_failed_prev_day_breakout"),
                        "intraday_volume_vs_20d": row.get("intraday_volume_vs_20d"),
                        "intraday_breakout_score": row.get("intraday_breakout_score"),
                        "intraday_pattern_label": row.get("intraday_pattern_label"),
                        "intraday_interval_minutes": row.get("interval_minutes"),
                        "total_revenue_qoq_growth_vs_sector": row.get("total_revenue_qoq_growth_vs_sector"),
                        "profit_after_tax_qoq_growth_vs_sector": row.get("profit_after_tax_qoq_growth_vs_sector"),
                        "debt_to_equity_vs_sector": row.get("debt_to_equity_vs_sector"),
                        "entry_style": entry_plan["entry_style"],
                        "attractive_price_low": entry_plan["attractive_price_low"],
                        "attractive_price_high": entry_plan["attractive_price_high"],
                        "invalidation_price": entry_plan["invalidation_price"],
                        "entry_note": entry_plan["entry_note"],
                        "near_miss_flag": evaluation["near_miss_flag"],
                        "watch_enabled": True,
                        "watch_reasons": json.dumps(setup.get("watch_reasons", [])),
                        "rule_pass": candidate_state == "PASS_NOW",
                        "load_ts": pd.Timestamp.utcnow(),
                    }
                )
            for rejection in rejections:
                rejection_rows.append(
                    {
                        "asof_date": screener_date,
                        "screener_date": screener_date,
                        "setup_id": setup["setup_id"],
                        "setup_name": setup["setup_name"],
                        "symbol": row["symbol"],
                        "company_master_id": row["company_master_id"],
                        "base_regime": regime_name,
                        "news_overlay": overlay_name,
                        "reason_code": rejection["reason_code"],
                        "severity": rejection.get("severity"),
                        "is_near_miss": rejection.get("is_near_miss"),
                        "delta_to_pass": rejection.get("delta_to_pass"),
                        "reason_detail": rejection["reason_detail"],
                        "load_ts": pd.Timestamp.utcnow(),
                    }
                )

    candidates = pd.DataFrame(candidate_rows)
    if not candidates.empty:
        candidates = candidates.sort_values(["setup_id", "setup_score", "technical_score", "fundamental_score", "symbol"], ascending=[True, False, False, False, True]).copy()
        candidates["rank"] = candidates.groupby("setup_id").cumcount() + 1
    rejections_df = pd.DataFrame(rejection_rows)
    candidates, rescue_meta = apply_ts_forecast_rescue(candidates, asof_date=meta.get("effective_date"))
    meta["ts_forecast_rescue"] = rescue_meta
    return candidates, rejections_df, meta


def _load_rescue_forecasts(symbols: list[str], asof_date: Any | None) -> pd.DataFrame:
    """Latest point-in-time TS forecast per symbol at the rescue horizon (asof <= rule date,
    bounded age so a stale forecast can never rescue)."""
    effective_asof = pd.to_datetime(asof_date, utc=True, errors="coerce")
    if pd.isna(effective_asof):
        effective_asof = pd.Timestamp.utcnow()
    try:
        return sql_to_df(
            f"""
            SELECT DISTINCT ON (symbol)
                symbol, asof_date, model_name, model_version, forecast_horizon_days,
                forecast_return, probability_positive, signal_quality,
                downside_return_p10, upside_return_p90
            FROM {TS_FORECASTS_TABLE}
            WHERE forecast_horizon_days = %s
              AND UPPER(TRIM(symbol)) = ANY(%s)
              AND asof_date <= %s
              AND asof_date > %s - interval '{int(TS_RESCUE_MAX_FORECAST_AGE_DAYS)} days'
            ORDER BY symbol, asof_date DESC
            """,
            params=(int(TS_RESCUE_HORIZON_DAYS), symbols, effective_asof, effective_asof),
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.rule_engine",
            source=TS_FORECASTS_TABLE,
            fallback_type="ts_forecast_rescue_load_failed",
            severity="warn",
            reason="TS-forecast rescue lookup failed; rule outputs continue without the allow-gate.",
            error=exc,
            metadata={"horizon_days": TS_RESCUE_HORIZON_DAYS, "symbols": len(symbols)},
        )
        return pd.DataFrame()


def apply_ts_forecast_rescue(candidates: pd.DataFrame, *, asof_date: Any | None = None) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Allow-gate: rescue technically-unconfirmed candidates with a strong TS forecast.

    Rescued rows become PASS_NOW with `technical_override_source='ts_forecast'` and full
    provenance recorded; `technical_entry_confirmed` stays False (honest). Downstream risk
    sizing applies a tighter cap to override-sourced rows, and outcomes mature through the
    normal labeling so the rescued cohort's benchmark-excess record decides the gate's fate.
    """
    meta: dict[str, Any] = {
        "enabled": bool(TS_RESCUE_ENABLED),
        "horizon_days": int(TS_RESCUE_HORIZON_DAYS),
        "eligible": 0,
        "rescued": 0,
        "rescued_symbols": [],
        "thresholds": {
            "min_forecast_return": TS_RESCUE_MIN_FORECAST_RETURN,
            "min_probability": TS_RESCUE_MIN_PROBABILITY,
            "min_signal_quality": TS_RESCUE_MIN_SIGNAL_QUALITY,
            "max_downside_p10": TS_RESCUE_MAX_DOWNSIDE_P10,
            "max_per_day": TS_RESCUE_MAX_PER_DAY,
        },
    }
    if not TS_RESCUE_ENABLED or candidates.empty or "candidate_state" not in candidates.columns:
        return candidates, meta
    eligible_states = {"WATCH_BREAKOUT", "WATCH_EVENT", "WATCH_PULLBACK", "ABSTAIN"}
    eligible_mask = candidates["candidate_state"].astype("string").str.upper().isin(eligible_states)
    meta["eligible"] = int(eligible_mask.sum())
    if not eligible_mask.any():
        return candidates, meta
    symbols = sorted(
        {str(value).strip().upper() for value in candidates.loc[eligible_mask, "symbol"].tolist() if str(value or "").strip()}
    )
    forecasts = _load_rescue_forecasts(symbols, asof_date)
    if forecasts.empty:
        return candidates, meta
    strong = forecasts[
        (pd.to_numeric(forecasts["forecast_return"], errors="coerce") >= TS_RESCUE_MIN_FORECAST_RETURN)
        & (pd.to_numeric(forecasts["probability_positive"], errors="coerce") >= TS_RESCUE_MIN_PROBABILITY)
        & (pd.to_numeric(forecasts["signal_quality"], errors="coerce") >= TS_RESCUE_MIN_SIGNAL_QUALITY)
        & (pd.to_numeric(forecasts["downside_return_p10"], errors="coerce") >= TS_RESCUE_MAX_DOWNSIDE_P10)
    ].copy()
    if strong.empty:
        return candidates, meta
    strong = strong.sort_values("forecast_return", ascending=False)
    out = candidates.copy()
    if "technical_override_source" not in out.columns:
        out["technical_override_source"] = None
    if "technical_override_json" not in out.columns:
        out["technical_override_json"] = None
    rescued_symbols: list[str] = []
    for forecast in strong.itertuples(index=False):
        if len(rescued_symbols) >= max(0, int(TS_RESCUE_MAX_PER_DAY)):
            break
        symbol = str(forecast.symbol).strip().upper()
        mask = eligible_mask & out["symbol"].astype("string").str.upper().eq(symbol)
        if not mask.any():
            continue
        provenance = json.dumps(
            {
                "override_source": "ts_forecast",
                "model_name": forecast.model_name,
                "model_version": forecast.model_version,
                "forecast_asof": str(forecast.asof_date),
                "forecast_horizon_days": int(forecast.forecast_horizon_days),
                "forecast_return": float(forecast.forecast_return),
                "probability_positive": float(forecast.probability_positive),
                "signal_quality": float(forecast.signal_quality),
                "downside_return_p10": float(forecast.downside_return_p10),
                "upside_return_p90": float(forecast.upside_return_p90),
                "thresholds": meta["thresholds"],
                "note": "Technical confirmation overridden by TS forecast; override recorded. Risk sizing applies the override allocation factor.",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        reason = (
            f"TS-forecast rescue: {forecast.model_name} expects {float(forecast.forecast_return):+.1%} over "
            f"{int(forecast.forecast_horizon_days)}d (p_pos={float(forecast.probability_positive):.2f}, "
            f"quality={float(forecast.signal_quality):.2f}); technical trigger not confirmed -- override recorded"
        )
        out.loc[mask, "candidate_state"] = "PASS_NOW"
        if "rule_pass" in out.columns:
            out.loc[mask, "rule_pass"] = True
        out.loc[mask, "technical_override_source"] = "ts_forecast"
        out.loc[mask, "technical_override_json"] = provenance
        if "watch_reason_detail" in out.columns:
            out.loc[mask, "watch_reason_detail"] = reason
        rescued_symbols.append(symbol)
        print(f"[advisory.rule_engine] ts_forecast_rescue symbol={symbol} {reason}", flush=True)
    meta["rescued"] = len(rescued_symbols)
    meta["rescued_symbols"] = rescued_symbols
    return out, meta


def persist_rule_outputs(candidates: pd.DataFrame, rejections: pd.DataFrame, *, asof_date: pd.Timestamp | None, rebuild: bool = False) -> None:
    ensure_rule_output_tables()
    if rebuild and asof_date is not None:
        def _delete_existing_rule_outputs() -> None:
            with db_session() as (_, cur):
                cur.execute(f"DELETE FROM {CANDIDATES_TABLE} WHERE asof_date = %s", (asof_date,))
                cur.execute(f"DELETE FROM {REJECTIONS_TABLE} WHERE asof_date = %s", (asof_date,))

        execute_db_operation(
            _delete_existing_rule_outputs,
            operation_name="rule_engine:delete_rebuild_outputs",
        )
    if not candidates.empty:
        candidates = candidates.copy()
        for column in ["asof_date", "screener_date", "load_ts"]:
            if column in candidates.columns:
                candidates[column] = pd.to_datetime(candidates[column], utc=True, errors="coerce")
        for column in [
            "rank",
            "intraday_interval_minutes",
        ]:
            if column in candidates.columns:
                candidates[column] = pd.to_numeric(candidates[column], errors="coerce").astype("Int64")
        for column in [
            "technical_score",
            "technical_trend_score",
            "technical_structure_score",
            "technical_participation_score",
            "technical_relative_strength_score",
            "technical_tradability_score",
            "fundamental_score",
            "regime_fit_score",
            "regime_fit_weight_effective",
            "event_score",
            "setup_score",
            "avg_traded_value_20d",
            "rs_vs_benchmark",
            "rs_vs_sector",
            "intraday_close_vs_vwap_pct",
            "intraday_pct_bars_above_vwap",
            "intraday_close_location_pct",
            "intraday_volume_vs_20d",
            "intraday_breakout_score",
            "total_revenue_qoq_growth_vs_sector",
            "profit_after_tax_qoq_growth_vs_sector",
            "debt_to_equity_vs_sector",
            "attractive_price_low",
            "attractive_price_high",
            "invalidation_price",
        ]:
            if column in candidates.columns:
                candidates[column] = pd.to_numeric(candidates[column], errors="coerce")
        for column in [
            "intraday_opening_range_breakout_up",
            "intraday_prev_day_breakout_up",
            "intraday_failed_prev_day_breakout",
            "technical_entry_confirmed",
            "near_miss_flag",
            "watch_enabled",
            "rule_pass",
        ]:
            if column in candidates.columns:
                candidates[column] = candidates[column].map(
                    lambda value: None if pd.isna(value) else bool(value)
                ).astype("boolean")
        for column in [
            "setup_id",
            "setup_name",
            "setup_family",
            "holding_horizon_note",
            "regime_name",
            "base_regime",
            "news_overlay",
            "theme_ids",
            "symbol",
            "company_master_id",
            "screener_slug",
            "source_screener_slug",
            "source_screener_list",
            "candidate_state",
            "watch_reason_detail",
            "soft_failures_json",
            "technical_state",
            "technical_trigger_type",
            "technical_trigger_note",
            "technical_setup_archetype",
            "technical_setup_quality_json",
            "intraday_pattern_label",
            "entry_style",
            "entry_note",
            "watch_reasons",
        ]:
            if column in candidates.columns:
                candidates[column] = candidates[column].astype("string")
        upsert_to_db(candidates, CANDIDATES_TABLE, unique_keys=["asof_date", "setup_id", "symbol"], timescaledb_column="asof_date")
    if not rejections.empty:
        rejections = rejections.copy()
        if "is_near_miss" in rejections.columns:
            rejections["is_near_miss"] = rejections["is_near_miss"].map(
                lambda value: None if pd.isna(value) else str(value).strip().lower() in {"1", "true", "t", "yes", "y"}
            ).astype("boolean")
        if "delta_to_pass" in rejections.columns:
            rejections["delta_to_pass"] = pd.to_numeric(rejections["delta_to_pass"], errors="coerce")
        if "severity" in rejections.columns:
            rejections["severity"] = rejections["severity"].astype("string")
        if "reason_code" in rejections.columns:
            rejections["reason_code"] = rejections["reason_code"].astype("string")
        if "reason_detail" in rejections.columns:
            rejections["reason_detail"] = rejections["reason_detail"].astype("string")
        upsert_to_db(rejections, REJECTIONS_TABLE, unique_keys=["asof_date", "setup_id", "symbol", "reason_code"], timescaledb_column="asof_date")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply setup rules to advisory snapshots.")
    parser.add_argument("--date", type=parse_datetime_arg, help="Asof date in YYYY-MM-DD")
    parser.add_argument("--setup", dest="setup_ids", nargs="*", help="Setup ids to evaluate")
    parser.add_argument("--config", help="Override setup registry YAML path")
    parser.add_argument("--skip-snapshot-refresh", action="store_true", help="Skip on-demand daily/fundamental snapshot repair inside the rule engine")
    parser.add_argument("--skip-intraday-prefetch", action="store_true", help="Skip on-demand intraday feature backfill inside the rule engine")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def summarize(candidates: pd.DataFrame, rejections: pd.DataFrame, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "ok",
        "candidates_table": CANDIDATES_TABLE,
        "rejections_table": REJECTIONS_TABLE,
        "effective_date": meta.get("effective_date"),
        "screener_date": meta.get("screener_date"),
        "snapshot_dates": {
            "regime": meta.get("regime_snapshot_date"),
            "overlay": meta.get("overlay_snapshot_date"),
            "technicals": meta.get("technical_snapshot_date"),
            "intraday": meta.get("intraday_snapshot_date"),
            "fundamentals": meta.get("fundamentals_snapshot_date"),
        },
        "regime_name": meta.get("regime_name"),
        "overlay_name": meta.get("overlay_name"),
        "overlay_reason": meta.get("overlay_reason"),
        "candidate_count": int(len(candidates)),
        "rejection_count": int(len(rejections)),
        "candidate_state_counts": candidates["candidate_state"].value_counts().to_dict() if not candidates.empty and "candidate_state" in candidates.columns else {},
        "avg_setup_score": None if candidates.empty else round(float(candidates["setup_score"].mean()), 6),
        "candidate_sample": candidates.head(10).to_dict(orient="records") if not candidates.empty else [],
        "top_rejections": rejections["reason_code"].value_counts().head(10).to_dict() if not rejections.empty else {},
    }


def main() -> int:
    args = parse_args()
    asof_date = pd.Timestamp(args.date, tz="UTC") if args.date else None
    candidates, rejections, meta = run_rule_engine(
        asof_date=asof_date,
        setup_ids=args.setup_ids,
        config_path=args.config,
        skip_snapshot_refresh=bool(args.skip_snapshot_refresh),
        skip_intraday_prefetch=bool(args.skip_intraday_prefetch),
    )
    effective_date = pd.to_datetime(meta.get("effective_date"), utc=True, errors="coerce")
    if not args.dry_run:
        persist_rule_outputs(candidates, rejections, asof_date=None if pd.isna(effective_date) else effective_date, rebuild=args.rebuild)
    result = summarize(candidates, rejections, meta)
    result["dry_run"] = bool(args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
