from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from functools import lru_cache
from typing import Any

import pandas as pd
from environs import Env
from pydantic import BaseModel, Field

from advisory.decision_trace import ACTION_CONFLICT_RULES_TABLE
from advisory.score_scales import to_100
from advisory.decision_trace import append_trace, append_trace_step, build_action_conflicts, persist_action_conflicts, safe_trace_call
from advisory.event_evidence_store import ANNOUNCEMENT_CONTEXT_OVERLAYS_TABLE, ANNOUNCEMENT_EVIDENCE_TABLE, BHAVCOPY_CONTEXT_OVERLAYS_TABLE
from advisory.exchange_context_overlays import EXCHANGE_CONTEXT_OVERLAYS_TABLE
from advisory.fallback_telemetry import record_fallback_event, record_local_fallback_event
from advisory.feature_freshness import build_feature_freshness_contract
from advisory.company_memory_review import TABLE_NAME as COMPANY_MEMORY_REVIEWS_TABLE
from advisory.causal_event_memory import TABLE_NAME as CAUSAL_EVENT_MEMORY_TABLE
from advisory.hypothesis_engine import ACTION_PLANS_TABLE, HYPOTHESES_TABLE, ensure_tables as ensure_hypothesis_tables
from advisory.macro_context_overlays import MACRO_CONTEXT_OVERLAYS_TABLE, macro_sector_alias_values_sql
from advisory.market_context import load_latest_market_context
from advisory.market_context import UNIVERSE_TABLE as MARKET_CONTEXT_UNIVERSE_TABLE
from advisory.news_theme_engine import THEME_CONTEXT_OVERLAYS_TABLE, theme_sector_alias_values_sql
from advisory.portfolio_engine import PORTFOLIO_TABLE
from advisory.position_lifecycle import LIFECYCLE_TABLE, REBALANCE_TABLE
from advisory.prompt_registry import prompt_version as registry_prompt_version
from advisory.prompt_registry import response_schema_version
from advisory.context_overlay_reliability_report import load_persisted_reliability_report as load_persisted_context_reliability_report
from advisory.context_overlay_reliability_report import load_reliability_report as load_fast_context_reliability_report
from advisory.context_overlay_reliability_report import reliability_runtime_policy_contract
from advisory.signal_quality_family_report import SUMMARY_TABLE as SIGNAL_QUALITY_SUMMARY_TABLE
from advisory.signal_quality_family_report import VARIANT_TO_SOURCE_FAMILY, build_family_report
from advisory.setup_registry import load_signal_quality_overlay_rules
from advisory.sync_state import persist_sync_state, publish_bus_message
from advisory.wait_signals import WAIT_SIGNAL_MATCHES_TABLE, WAIT_SIGNALS_TABLE
from advisory.watchlist_builder import TABLE_NAME as WATCHLIST_TABLE
from utils.codex_cli import run_codex_structured
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.display_time import to_display_value
from utils.schema_migrations import apply_schema_migration
from utils.sync import parse_datetime_arg


env = Env()
env.read_env()
logger = logging.getLogger(__name__)

ACTION_REFRESH_SYNC_SOURCE = "continuous_watch:action_refresh"
ACTION_RECOMMENDER_PROGRESS_LOG_ENABLED = env.bool("ACTION_RECOMMENDER_PROGRESS_LOG_ENABLED", True)
ACTION_RECOMMENDER_ENSURE_CONTEXT_INDEXES = env.bool("ACTION_RECOMMENDER_ENSURE_CONTEXT_INDEXES", True)
ACTION_RECOMMENDER_PERSIST_LIVE_FEATURE_FRESHNESS_ENABLED = env.bool(
    "ACTION_RECOMMENDER_PERSIST_LIVE_FEATURE_FRESHNESS_ENABLED",
    False,
)
ACTION_RECOMMENDER_PERSIST_TRACE_ENABLED = env.bool("ACTION_RECOMMENDER_PERSIST_TRACE_ENABLED", True)
ACTION_RECOMMENDER_CLI_TRACE_ENABLED = env.bool("ACTION_RECOMMENDER_CLI_TRACE_ENABLED", False)
ACTION_TRANSITION_STABILITY_POLICY_GATE_ENABLED = env.bool("ACTION_TRANSITION_STABILITY_POLICY_GATE_ENABLED", True)


def _progress_log(stage: str, status: str, *, rows: int | None = None, extra: str | None = None, started_at: float | None = None) -> None:
    if not ACTION_RECOMMENDER_PROGRESS_LOG_ENABLED:
        return
    parts = [f"[advisory.action_recommender] stage={stage}", status]
    if started_at is not None:
        parts.append(f"elapsed={time.monotonic() - started_at:.2f}s")
    if rows is not None:
        parts.append(f"rows={int(rows)}")
    if extra:
        parts.append(str(extra))
    print(" ".join(parts), file=sys.stderr, flush=True)


TABLE_NAME = "advisory_action_recommendations"
ACTIONS_SCHEMA_MIGRATION_ID = "20260611_advisory_action_recommendations_base"
ACTION_AUTHORITY_SCHEMA_MIGRATION_ID = "20260621_action_recommendations_authority_columns"
ACTION_CONTEXT_ENRICHMENT_INDEX_MIGRATION_ID = "20260621_action_context_enrichment_indexes"
MANUAL_REVISION_PROMPT_ID = "manual_revision_pointers"
MANUAL_REVISION_PROMPT_VERSION = registry_prompt_version(MANUAL_REVISION_PROMPT_ID)
MANUAL_REVISION_PROMPT_SCHEMA_VERSION = response_schema_version(MANUAL_REVISION_PROMPT_ID)
CANDIDATES_TABLE = "advisory_candidates"
REGIME_TABLE = "advisory_market_regime"
EVENT_EVALUATIONS_TABLE = "advisory_event_evaluations"
EVENT_REVIEWS_TABLE = "advisory_event_reviews"
EVENT_POLICY_TABLE = "advisory_event_policy_actions"
SIGNAL_REFRESH_TABLE = "advisory_signal_refresh_actions"

ACTIONS_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        asof_date TIMESTAMPTZ NOT NULL,
        published_on TIMESTAMPTZ,
        symbol TEXT NOT NULL,
        setup_id TEXT,
        unique_id TEXT,
        action_code TEXT NOT NULL,
        action_priority BIGINT,
        action_source TEXT,
        source_action TEXT,
        transaction_type TEXT,
        execution_mode TEXT,
        action_fraction DOUBLE PRECISION,
        approved_allocation_inr DOUBLE PRECISION,
        reference_price DOUBLE PRECISION,
        stop_price DOUBLE PRECISION,
        invalidation_price DOUBLE PRECISION,
        recommended_stop_price DOUBLE PRECISION,
        recommended_target_price DOUBLE PRECISION,
        expected_horizon_days BIGINT,
        invest_score_pct DOUBLE PRECISION,
        action_reason TEXT,
        action_detail TEXT,
        recommendation_reason_json TEXT,
        reason_contract_status TEXT,
        feature_freshness_json TEXT,
        manual_revision_summary TEXT,
        manual_revision_pointers_json TEXT,
        manual_revision_prompt_id TEXT,
        manual_revision_prompt_version TEXT,
        manual_revision_prompt_schema_version TEXT,
        manual_revision_model TEXT,
        manual_revision_status TEXT,
        raw_context_json TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (asof_date, symbol)
    )
    """,
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS recommended_target_price DOUBLE PRECISION",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS expected_horizon_days BIGINT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS recommendation_reason_json TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS reason_contract_status TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS feature_freshness_json TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS manual_revision_summary TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS manual_revision_pointers_json TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS manual_revision_prompt_id TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS manual_revision_prompt_version TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS manual_revision_prompt_schema_version TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS manual_revision_model TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS manual_revision_status TEXT",
]

ACTION_AUTHORITY_SCHEMA_STATEMENTS = [
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS authority_scope TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS portfolio_authority TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS broker_execution_allowed BOOLEAN",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS full_advisory_required BOOLEAN",
]

ACTION_CONTEXT_ENRICHMENT_INDEX_STATEMENTS = [
    f"CREATE INDEX IF NOT EXISTS idx_action_ctx_announcement_symbol_published ON {ANNOUNCEMENT_CONTEXT_OVERLAYS_TABLE} (UPPER(TRIM(symbol)), published_on DESC)",
    f"CREATE INDEX IF NOT EXISTS idx_action_ctx_announcement_published ON {ANNOUNCEMENT_CONTEXT_OVERLAYS_TABLE} (published_on DESC)",
    f"CREATE INDEX IF NOT EXISTS idx_action_ctx_exchange_symbol_asof ON {EXCHANGE_CONTEXT_OVERLAYS_TABLE} (UPPER(TRIM(symbol)), asof_date DESC)",
    f"CREATE INDEX IF NOT EXISTS idx_action_ctx_exchange_asof ON {EXCHANGE_CONTEXT_OVERLAYS_TABLE} (asof_date DESC)",
    f"CREATE INDEX IF NOT EXISTS idx_action_ctx_bhavcopy_symbol_asof ON {BHAVCOPY_CONTEXT_OVERLAYS_TABLE} (UPPER(TRIM(symbol)), asof_date DESC)",
    f"CREATE INDEX IF NOT EXISTS idx_action_ctx_bhavcopy_asof ON {BHAVCOPY_CONTEXT_OVERLAYS_TABLE} (asof_date DESC)",
    f"CREATE INDEX IF NOT EXISTS idx_action_ctx_theme_asof_sector ON {THEME_CONTEXT_OVERLAYS_TABLE} (asof_date DESC, sector_name, sector_code)",
    f"CREATE INDEX IF NOT EXISTS idx_action_ctx_macro_asof_sector ON {MACRO_CONTEXT_OVERLAYS_TABLE} (asof_date DESC, sector_name, sector_code)",
    f"CREATE INDEX IF NOT EXISTS idx_action_ctx_market_universe_symbol_asof ON {MARKET_CONTEXT_UNIVERSE_TABLE} (UPPER(TRIM(symbol)), asof_date DESC)",
    f"CREATE INDEX IF NOT EXISTS idx_action_ctx_market_universe_asof_sector ON {MARKET_CONTEXT_UNIVERSE_TABLE} (asof_date DESC, sector_name, sector_code)",
]

ACTION_PRIORITY = {
    "SELL": 100,
    "PARTIAL_SELL": 90,
    "MANUAL_REVIEW": 80,
    "TIGHTEN_STOP": 70,
    "REDUCE_EXPOSURE_REVIEW": 70,
    "BUY_MORE": 60,
    "BUY": 50,
    "HOLD": 20,
    "WATCH": 10,
}
DEFAULT_CONFLICT_RULE_IDS = {
    "EXIT_BEATS_ENTRY_OR_WATCH",
    "ADVERSARIAL_VETO_MANUAL_BEATS_POSITIVE_OR_WATCH",
    "EVENT_POLICY_REVIEW_BEATS_POSITIVE_ENTRY",
    "MARKET_GATE_MANUAL_BEATS_POSITIVE",
    "SAME_ACTION_DUPLICATE_COLLAPSE",
    "WATCH_LOSES_TO_HIGHER_PRIORITY",
}

PLAYBOOK_LOOKBACK_DAYS = 14
ACTION_MANUAL_REVISION_POINTERS_ENABLED = env.bool("ACTION_MANUAL_REVISION_POINTERS_ENABLED", default=False)
ACTION_MANUAL_REVISION_POINTERS_MODEL = env("ACTION_MANUAL_REVISION_POINTERS_MODEL", default="codex")
ACTION_MANUAL_REVISION_POINTERS_TIMEOUT_SECONDS = env.int("ACTION_MANUAL_REVISION_POINTERS_TIMEOUT_SECONDS", default=180)
ACTION_MANUAL_REVISION_POINTERS_MAX_CANDIDATES = env.int("ACTION_MANUAL_REVISION_POINTERS_MAX_CANDIDATES", default=8)
ACTION_EVENT_POLICY_BUY_WATCH_AS_WATCH = env.bool("ACTION_EVENT_POLICY_BUY_WATCH_AS_WATCH", default=True)
ACTION_EVENT_POLICY_REDUCE_EXPOSURE_AS_REVIEW_ACTION = env.bool(
    "ACTION_EVENT_POLICY_REDUCE_EXPOSURE_AS_REVIEW_ACTION",
    default=True,
)
ACTION_PLAYBOOK_BUY_WATCH_AS_WATCH = env.bool("ACTION_PLAYBOOK_BUY_WATCH_AS_WATCH", default=True)
ACTION_PLAYBOOK_REDUCE_EXPOSURE_AS_REVIEW_ACTION = env.bool(
    "ACTION_PLAYBOOK_REDUCE_EXPOSURE_AS_REVIEW_ACTION",
    default=True,
)
ACTION_COMPANY_MEMORY_BRIDGE_ENABLED = env.bool("ACTION_COMPANY_MEMORY_BRIDGE_ENABLED", default=True)
ACTION_COMPANY_MEMORY_LOOKBACK_DAYS = env.int("ACTION_COMPANY_MEMORY_LOOKBACK_DAYS", default=14)
ACTION_COMPANY_MEMORY_MIN_CONFIDENCE = env.float("ACTION_COMPANY_MEMORY_MIN_CONFIDENCE", default=0.65)
ACTION_COMPANY_MEMORY_MIN_CONVICTION = env.float("ACTION_COMPANY_MEMORY_MIN_CONVICTION", default=60.0)
ACTION_SIGNAL_REFRESH_LOOKBACK_DAYS = env.int("ACTION_SIGNAL_REFRESH_LOOKBACK_DAYS", default=3)
ACTION_SIGNAL_REFRESH_MAX_ROWS = env.int("ACTION_SIGNAL_REFRESH_MAX_ROWS", default=500)
ACTION_CONTEXT_OVERLAY_LOOKBACK_DAYS = env.int("ACTION_CONTEXT_OVERLAY_LOOKBACK_DAYS", default=30)
ACTION_CONTEXT_OVERLAY_MAX_ROWS_PER_SYMBOL = env.int("ACTION_CONTEXT_OVERLAY_MAX_ROWS_PER_SYMBOL", default=10)
ACTION_CAUSAL_EVENT_MEMORY_LOOKBACK_DAYS = env.int("ACTION_CAUSAL_EVENT_MEMORY_LOOKBACK_DAYS", default=30)
ACTION_CAUSAL_EVENT_MEMORY_MAX_ROWS_PER_SYMBOL = env.int("ACTION_CAUSAL_EVENT_MEMORY_MAX_ROWS_PER_SYMBOL", default=8)
ACTION_SIGNAL_QUALITY_OVERLAY_RULE_CONSUMER_ENABLED = env.bool(
    "ACTION_SIGNAL_QUALITY_OVERLAY_RULE_CONSUMER_ENABLED",
    default=False,
)
SIGNAL_QUALITY_OVERLAY_NEGATIVE_BLOCK_EFFECTS = {
    "negative_context_blocks_positive_to_watch",
    "trusted_negative_context_blocks_positive_to_watch",
}
SIGNAL_QUALITY_OVERLAY_POSITIVE_WATCH_EFFECTS = {
    "positive_context_boosts_watch_priority",
    "trusted_positive_context_boosts_watch_priority",
}
SIGNAL_QUALITY_OVERLAY_SUPPORTED_SPLIT_AXES = {
    "context_class_direction",
    "context_class",
    "event_class",
    "pressure_class",
    "macro_signal",
    "theme",
    "rule_id",
    "direction",
}
SIGNAL_QUALITY_OVERLAY_SUPPRESSED_CONTEXT_CLASS_RELIABILITY = {
    "hurts_or_no_lift",
    "negative_after_cost",
    "inconsistent_or_horizon_sensitive",
    "needs_benchmark_attribution",
    "benchmark_beta_not_overlay_alpha",
}

PLAYBOOK_ACTION_MAP = {
    "GO_CASH_REVIEW": ("MANUAL_REVIEW", None),
    "REDUCE_EXPOSURE_REVIEW": ("MANUAL_REVIEW", None),
    "SECTOR_REVIEW": ("MANUAL_REVIEW", None),
    "MANUAL_REVIEW": ("MANUAL_REVIEW", None),
    "BUY_WATCH": ("MANUAL_REVIEW", None),
    "WATCH_SYMBOLS": ("WATCH", None),
    "ADD_TO_WATCHLIST": ("WATCH", None),
    "NO_ACTION": ("HOLD", None),
}
EVENT_POLICY_ACTION_MAP = {
    "BUY_WATCH": ("MANUAL_REVIEW", None),
    "REDUCE_EXPOSURE_REVIEW": ("MANUAL_REVIEW", None),
    "MANUAL_REVIEW": ("MANUAL_REVIEW", None),
}
EVENT_POLICY_POSITIVE_CLASSES = {
    "ORDER_WIN",
    "RESULTS_POSITIVE",
    "GROWTH_ACCELERATION",
    "MARGIN_EXPANSION",
    "GUIDANCE_UPGRADE",
    "PLEDGE_DOWN",
    "PROMOTER_BUYING",
    "BUYBACK",
    "CAPEX_EXPANSION",
    "POLICY_SECTOR_POSITIVE",
}
EVENT_POLICY_NEGATIVE_CLASSES = {
    "RESULTS_NEGATIVE",
    "GUIDANCE_DOWNGRADE",
    "PLEDGE_UP",
    "PROMOTER_SELLING",
    "MANAGEMENT_RESIGNATION",
    "REGULATORY_NOTICE",
    "AUDITOR_GOVERNANCE",
    "POLICY_SECTOR_NEGATIVE",
}
EVENT_POLICY_LOW_ACTION_CLASSES = {"DIVIDEND", "ANALYST_MEET", "CORPORATE_ACTION_NEUTRAL", "OTHER"}
MANUAL_REVIEW_WAIT_SIGNAL_SETUP_ID = "MANUAL_REVIEW_WAIT_SIGNAL"
SIGNAL_REFRESH_SETUP_ID = "SIGNAL_REFRESH_REVIEW"
CLI_COMPACT_SAMPLE_COLUMNS = [
    "asof_date",
    "published_on",
    "symbol",
    "setup_id",
    "action_code",
    "action_source",
    "source_action",
    "transaction_type",
    "execution_mode",
    "authority_scope",
    "portfolio_authority",
    "broker_execution_allowed",
    "full_advisory_required",
    "invest_score_pct",
    "reference_price",
    "stop_price",
    "recommended_target_price",
    "expected_horizon_days",
    "reason_contract_status",
    "manual_revision_status",
    "action_reason",
]

RISK_OFF_STATES = {"RISK_OFF", "HIGH", "STRESS", "CRASH", "HOSTILE"}
SYSTEMIC_MACRO_HARD_RISK_STATES = {"STRESS", "CRASH", "HOSTILE", "SHOCK"}
POSITIVE_BROKER_ACTIONS = {"BUY", "BUY_MORE"}
BROKER_CAPABLE_ACTIONS = {"BUY", "BUY_MORE", "SELL", "PARTIAL_SELL"}
REVIEW_ONLY_SIGNAL_ACTION_SOURCES = {
    "company_memory",
    "event_policy",
    "manual_review_wait_signal",
    "signal_refresh",
}
REVIEW_ONLY_SIGNAL_ACTION_SOURCE_PREFIXES = ("playbook",)
FEATURE_FRESHNESS_BLOCKED_POSITIVE_ACTIONS = POSITIVE_BROKER_ACTIONS
MARKET_CONTEXT_RISK_OFF_SCORE_THRESHOLD = env.float("ACTION_MARKET_CONTEXT_RISK_OFF_SCORE_THRESHOLD", default=0.60)
MARKET_CONTEXT_WEAK_BREADTH_THRESHOLD = env.float("ACTION_MARKET_CONTEXT_WEAK_BREADTH_THRESHOLD", default=40.0)
MARKET_CONTEXT_CAUTION_RISK_OFF_SCORE_THRESHOLD = env.float("ACTION_MARKET_CONTEXT_CAUTION_RISK_OFF_SCORE_THRESHOLD", default=0.45)
MARKET_CONTEXT_CAUTION_BREADTH_THRESHOLD = env.float("ACTION_MARKET_CONTEXT_CAUTION_BREADTH_THRESHOLD", default=50.0)
MARKET_CONTEXT_CAUTION_SIZE_MULTIPLIER = env.float("ACTION_MARKET_CONTEXT_CAUTION_SIZE_MULTIPLIER", default=0.75)
MARKET_CONTEXT_REGIME_LABEL_HARD_BLOCK_ENABLED = env.bool("ACTION_MARKET_CONTEXT_REGIME_LABEL_HARD_BLOCK_ENABLED", default=False)
MARKET_CONTEXT_WEAK_BREADTH_HARD_BLOCK_ENABLED = env.bool("ACTION_MARKET_CONTEXT_WEAK_BREADTH_HARD_BLOCK_ENABLED", default=False)
MARKET_CONTEXT_SCORE_RISK_OFF_HARD_BLOCK_ENABLED = env.bool("ACTION_MARKET_CONTEXT_SCORE_RISK_OFF_HARD_BLOCK_ENABLED", default=False)
MARKET_CONTEXT_HIGH_MACRO_RISK_HARD_BLOCK_ENABLED = env.bool("ACTION_MARKET_CONTEXT_HIGH_MACRO_RISK_HARD_BLOCK_ENABLED", default=False)
MARKET_CONTEXT_SYMBOL_LEADERSHIP_OVERRIDE_ENABLED = env.bool("ACTION_MARKET_CONTEXT_SYMBOL_LEADERSHIP_OVERRIDE_ENABLED", default=True)
MARKET_CONTEXT_LEADER_SOFTENS_SCORE_RISK_OFF_ENABLED = env.bool("ACTION_MARKET_CONTEXT_LEADER_SOFTENS_SCORE_RISK_OFF_ENABLED", default=True)
MARKET_CONTEXT_CONTEXT_OVERLAY_OVERRIDE_ENABLED = env.bool("ACTION_MARKET_CONTEXT_CONTEXT_OVERLAY_OVERRIDE_ENABLED", default=True)
MARKET_CONTEXT_CONTEXT_OVERLAY_SOFTENS_ANY_SYMBOL_ENABLED = env.bool(
    "ACTION_MARKET_CONTEXT_CONTEXT_OVERLAY_SOFTENS_ANY_SYMBOL_ENABLED",
    default=True,
)


class ManualRevisionPointers(BaseModel):
    revision_summary: str = Field(min_length=10, description="One concise operator-facing summary of the final decision.")
    key_reasons: list[str] = Field(default_factory=list, description="Most important reasons supporting the chosen action.")
    manual_checks: list[str] = Field(default_factory=list, description="Specific checks the operator should do before acting.")
    risk_flags: list[str] = Field(default_factory=list, description="Risks, contradictions, stale evidence, or execution issues to verify.")
    missing_data: list[str] = Field(default_factory=list, description="Data that would improve confidence if available.")
    operator_questions: list[str] = Field(default_factory=list, description="Questions to answer during manual review.")


def table_exists(table_name: str) -> bool:
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
    return not df.empty


def load_action_identity_gaps(symbols: list[str]) -> pd.DataFrame:
    clean_symbols = sorted({str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()})
    if not clean_symbols:
        return pd.DataFrame()
    if not table_exists("company_master"):
        return pd.DataFrame(
            [
                {
                    "symbol": symbol,
                    "identity_gap": "company_master_table_missing",
                    "company_master_id": None,
                    "nse_ticker": None,
                    "bse_ticker": None,
                    "dhan_nse_id": None,
                    "dhan_bse_id": None,
                }
                for symbol in clean_symbols
            ]
        )
    return sql_to_df(
        """
        WITH requested(symbol) AS (
            SELECT UPPER(UNNEST(%(symbols)s::text[]))
        ),
        matches AS (
            SELECT
                r.symbol,
                cm.company_master_id,
                cm.nse_ticker,
                cm.bse_ticker,
                cm.dhan_nse_id,
                cm.dhan_bse_id
            FROM requested r
            LEFT JOIN company_master cm
              ON UPPER(cm.nse_ticker) = r.symbol
              OR UPPER(cm.bse_ticker) = r.symbol
        ),
        mapped AS (
            SELECT
                symbol,
                MAX(company_master_id::text) FILTER (WHERE company_master_id IS NOT NULL) AS company_master_id,
                MAX(nse_ticker) FILTER (WHERE nse_ticker IS NOT NULL) AS nse_ticker,
                MAX(bse_ticker) FILTER (WHERE bse_ticker IS NOT NULL) AS bse_ticker,
                MAX(dhan_nse_id::text) FILTER (WHERE dhan_nse_id IS NOT NULL) AS dhan_nse_id,
                MAX(dhan_bse_id::text) FILTER (WHERE dhan_bse_id IS NOT NULL) AS dhan_bse_id,
                CASE
                    WHEN NOT BOOL_OR(company_master_id IS NOT NULL) THEN 'missing_company_master'
                    WHEN NOT BOOL_OR(dhan_nse_id IS NOT NULL OR dhan_bse_id IS NOT NULL) THEN 'missing_dhan_security_id'
                    ELSE NULL
                END AS identity_gap
            FROM matches
            GROUP BY symbol
        )
        SELECT *
        FROM mapped
        WHERE identity_gap IS NOT NULL
        ORDER BY symbol
        """,
        params={"symbols": clean_symbols},
    )


@lru_cache(maxsize=256)
def table_columns(table_name: str) -> set[str]:
    df = sql_to_df(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
        """,
        params=(table_name,),
    )
    if df.empty or "column_name" not in df.columns:
        return set()
    return {str(value) for value in df["column_name"].dropna().tolist()}


def ensure_actions_table() -> None:
    apply_schema_migration(
        migration_id=ACTIONS_SCHEMA_MIGRATION_ID,
        description="Create and normalize consolidated advisory action recommendations table.",
        statements=ACTIONS_SCHEMA_STATEMENTS,
        metadata={"module": "advisory.action_recommender", "tables": [TABLE_NAME]},
    )
    apply_schema_migration(
        migration_id=ACTION_AUTHORITY_SCHEMA_MIGRATION_ID,
        description="Add explicit authority boundary columns to consolidated advisory action recommendations.",
        statements=ACTION_AUTHORITY_SCHEMA_STATEMENTS,
        metadata={
            "module": "advisory.action_recommender",
            "tables": [TABLE_NAME],
            "authority_scope": "action_recommendation_boundary",
        },
    )
    if ACTION_RECOMMENDER_ENSURE_CONTEXT_INDEXES:
        ensure_action_context_enrichment_indexes()


def ensure_action_context_enrichment_indexes() -> None:
    required_tables = [
        ANNOUNCEMENT_CONTEXT_OVERLAYS_TABLE,
        EXCHANGE_CONTEXT_OVERLAYS_TABLE,
        BHAVCOPY_CONTEXT_OVERLAYS_TABLE,
        THEME_CONTEXT_OVERLAYS_TABLE,
        MACRO_CONTEXT_OVERLAYS_TABLE,
        MARKET_CONTEXT_UNIVERSE_TABLE,
    ]
    if not all(table_exists(table_name) for table_name in required_tables):
        return
    apply_schema_migration(
        migration_id=ACTION_CONTEXT_ENRICHMENT_INDEX_MIGRATION_ID,
        description="Create indexes used by action-recommender context enrichment reads.",
        statements=ACTION_CONTEXT_ENRICHMENT_INDEX_STATEMENTS,
        owner="advisory.action_recommender",
        metadata={
            "module": "advisory.action_recommender",
            "tables": required_tables,
            "workflow": "action_context_enrichment",
        },
    )


def _normalize_asof_date(asof_date: pd.Timestamp | None) -> pd.Timestamp:
    ts = pd.to_datetime(asof_date or pd.Timestamp.utcnow(), utc=True, errors="coerce")
    return ts.normalize()


def _point_in_time_cutoff(asof_date: Any) -> tuple[pd.Timestamp, str]:
    ts = pd.to_datetime(asof_date or pd.Timestamp.utcnow(), utc=True, errors="coerce")
    if pd.isna(ts):
        ts = pd.Timestamp.utcnow()
    # Date-only advisory runs should see the whole trading date. Intraday/watch
    # refreshes must not see later same-day rows.
    if ts == ts.normalize():
        return ts + pd.Timedelta(days=1), "<"
    return ts, "<="


def _coerce_ts(value: Any) -> pd.Timestamp | None:
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    return None if pd.isna(ts) else ts


def _text(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _num(value: Any) -> float | None:
    out = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(out) else float(out)


def _boolish_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    if pd.api.types.is_scalar(value) and pd.isna(value):
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None


# Serialized-context keys that themselves carry an entire prior context. Several raw_context
# builders spread **row.to_dict() from source tables that hold these columns, so embedding them
# re-serializes a full context into the new one -- the compounding that grew raw_context_json to
# >1 GiB and OOM-killed Postgres. Strip them (recursively) before persisting; the structured
# fields and any lean parsed payload are kept.
_HEAVY_RECURSIVE_CONTEXT_KEYS = ("raw_context_json", "action_payload_json")


def _strip_heavy_context_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_heavy_context_keys(item)
            for key, item in value.items()
            if key not in _HEAVY_RECURSIVE_CONTEXT_KEYS
        }
    if isinstance(value, list):
        return [_strip_heavy_context_keys(item) for item in value]
    return value


def _build_record(
    *,
    asof_date: pd.Timestamp,
    symbol: Any,
    setup_id: Any = None,
    unique_id: Any = None,
    published_on: Any = None,
    action_code: str,
    action_source: str,
    source_action: Any = None,
    transaction_type: Any = None,
    execution_mode: Any = None,
    action_fraction: Any = None,
    approved_allocation_inr: Any = None,
    reference_price: Any = None,
    stop_price: Any = None,
    invalidation_price: Any = None,
    recommended_stop_price: Any = None,
    recommended_target_price: Any = None,
    expected_horizon_days: Any = None,
    invest_score_pct: Any = None,
    action_reason: Any = None,
    action_detail: Any = None,
    raw_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_action = str(action_code).upper()
    return {
        "asof_date": asof_date,
        "published_on": _coerce_ts(published_on),
        "symbol": str(symbol).upper(),
        "setup_id": _text(setup_id),
        "unique_id": _text(unique_id),
        "action_code": normalized_action,
        "action_priority": int(ACTION_PRIORITY.get(normalized_action, 0)),
        "action_source": action_source,
        "source_action": _text(source_action),
        "transaction_type": _text(transaction_type),
        "execution_mode": _text(execution_mode),
        "action_fraction": _num(action_fraction),
        "approved_allocation_inr": _num(approved_allocation_inr),
        "reference_price": _num(reference_price),
        "stop_price": _num(stop_price),
        "invalidation_price": _num(invalidation_price),
        "recommended_stop_price": _num(recommended_stop_price),
        "recommended_target_price": _num(recommended_target_price),
        "expected_horizon_days": None if pd.isna(pd.to_numeric(expected_horizon_days, errors="coerce")) else int(pd.to_numeric(expected_horizon_days, errors="coerce")),
        "invest_score_pct": _num(invest_score_pct),
        "action_reason": _text(action_reason),
        "action_detail": _text(action_detail),
        "raw_context_json": json.dumps(_strip_heavy_context_keys(raw_context or {}), ensure_ascii=False, default=str, sort_keys=True),
        "load_ts": pd.Timestamp.utcnow(),
    }


def _is_review_only_signal_source(value: Any) -> bool:
    source = str(value or "").strip().lower()
    return source in REVIEW_ONLY_SIGNAL_ACTION_SOURCES or any(
        source.startswith(prefix) for prefix in REVIEW_ONLY_SIGNAL_ACTION_SOURCE_PREFIXES
    )


def enforce_review_only_signal_boundaries(df: pd.DataFrame) -> pd.DataFrame:
    """Fail closed if research/LLM-derived sources accidentally emit broker-capable actions."""
    if df.empty or "action_source" not in df.columns:
        return df
    out = df.copy()
    sources = out["action_source"].astype("string").str.strip().str.lower()
    review_only_mask = sources.apply(_is_review_only_signal_source)
    if not bool(review_only_mask.any()):
        return out
    for idx, row in out.loc[review_only_mask].iterrows():
        original_action = str(row.get("action_code") or "").strip().upper()
        original_transaction = _text(row.get("transaction_type"))
        original_execution_mode = _text(row.get("execution_mode"))
        needs_sanitize = (
            original_action in BROKER_CAPABLE_ACTIONS
            or original_transaction in {"BUY", "SELL"}
            or original_execution_mode == "broker_order"
        )
        if not needs_sanitize:
            continue
        if original_action in {"BUY", "BUY_MORE"}:
            safe_action = "WATCH"
            safe_reason = "review_only_source_positive_downgraded_to_watch"
            safe_detail = "Research/LLM-derived positive signal requires deterministic technical/risk confirmation before any entry."
        elif original_action in {"SELL", "PARTIAL_SELL"}:
            safe_action = "REDUCE_EXPOSURE_REVIEW"
            safe_reason = "review_only_source_exit_downgraded_to_derisk_review"
            safe_detail = "Research/LLM-derived negative signal requires lifecycle/risk confirmation before any exit."
        else:
            safe_action = "MANUAL_REVIEW"
            safe_reason = "review_only_source_broker_fields_removed"
            safe_detail = "Review-only source attempted to carry broker execution fields."
        raw_context = _parse_jsonish(row.get("raw_context_json"), {})
        if not isinstance(raw_context, dict):
            raw_context = {}
        raw_context.update(
            {
                "review_only_source_sanitized": True,
                "review_only_source_sanitizer_version": 1,
                "blocked_original_action_code": original_action,
                "blocked_original_transaction_type": original_transaction,
                "blocked_original_execution_mode": original_execution_mode,
                "sanitized_action_code": safe_action,
                "sanitizer_reason": safe_reason,
                "authority_scope": raw_context.get("authority_scope") or "review_input_only",
                "portfolio_authority": "none",
                "broker_execution_allowed": False,
                "full_advisory_required": True,
            }
        )
        existing_reason = _text(row.get("action_reason"))
        out.at[idx, "action_code"] = safe_action
        out.at[idx, "action_priority"] = int(ACTION_PRIORITY.get(safe_action, 0))
        out.at[idx, "transaction_type"] = None
        out.at[idx, "execution_mode"] = "review_only"
        out.at[idx, "action_reason"] = (
            f"{safe_detail} Original reason: {existing_reason}"
            if existing_reason
            else safe_detail
        )
        out.at[idx, "action_detail"] = safe_reason
        out.at[idx, "raw_context_json"] = json.dumps(raw_context, ensure_ascii=False, default=str, sort_keys=True)
    return out


def load_portfolio_actions(*, asof_date: pd.Timestamp, symbols: list[str] | None = None, setup_ids: list[str] | None = None) -> pd.DataFrame:
    if not table_exists(PORTFOLIO_TABLE):
        return pd.DataFrame()
    columns = table_columns(PORTFOLIO_TABLE)

    def optional_column(column: str) -> str:
        return column if column in columns else f"NULL AS {column}"

    clauses = ["asof_date = %s", "portfolio_status IN ('approved', 'trimmed')"]
    params: list[object] = [asof_date]
    if symbols:
        clauses.append("symbol = ANY(%s)")
        params.append([str(value).upper() for value in symbols])
    if setup_ids:
        clauses.append("setup_id = ANY(%s)")
        params.append([str(value).upper() for value in setup_ids])
    return sql_to_df(
        f"""
        SELECT
            asof_date,
            published_on,
            setup_id,
            symbol,
            unique_id,
            portfolio_status,
            portfolio_reason,
            approved_allocation_inr,
            {optional_column("position_state")},
            {optional_column("state_transition_contract_json")},
            stop_price,
            invalidation_price,
            {optional_column("target_price")},
            {optional_column("expected_horizon_days")},
            invest_score_pct,
            execution_notes
        FROM {PORTFOLIO_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY published_on DESC, symbol
        """,
        params=tuple(params),
    )


def load_lifecycle_actions(*, asof_date: pd.Timestamp, symbols: list[str] | None = None, setup_ids: list[str] | None = None) -> pd.DataFrame:
    if not table_exists(LIFECYCLE_TABLE):
        return pd.DataFrame()
    clauses = ["asof_date = %s"]
    params: list[object] = [asof_date]
    if symbols:
        clauses.append("symbol = ANY(%s)")
        params.append([str(value).upper() for value in symbols])
    if setup_ids:
        clauses.append("setup_id = ANY(%s)")
        params.append([str(value).upper() for value in setup_ids])
    return sql_to_df(
        f"""
        SELECT
            asof_date,
            published_on,
            setup_id,
            symbol,
            unique_id,
            position_status,
            lifecycle_reason,
            next_action,
            next_action_reason,
            current_price,
            stop_price,
            invalidation_price
        FROM {LIFECYCLE_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY published_on DESC, symbol
        """,
        params=tuple(params),
    )


def load_rebalance_actions(*, asof_date: pd.Timestamp, symbols: list[str] | None = None, setup_ids: list[str] | None = None) -> pd.DataFrame:
    if not table_exists(REBALANCE_TABLE):
        return pd.DataFrame()
    clauses = ["asof_date = %s"]
    params: list[object] = [asof_date]
    if symbols:
        clauses.append("symbol = ANY(%s)")
        params.append([str(value).upper() for value in symbols])
    if setup_ids:
        clauses.append("setup_id = ANY(%s)")
        params.append([str(value).upper() for value in setup_ids])
    return sql_to_df(
        f"""
        SELECT
            asof_date,
            published_on,
            setup_id,
            symbol,
            unique_id,
            suggested_action,
            action_reason,
            reference_price,
            stop_price,
            invalidation_price,
            recommended_stop_price,
            action_fraction,
            execution_mode
        FROM {REBALANCE_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY published_on DESC, symbol
        """,
        params=tuple(params),
    )


def load_watch_actions(*, asof_date: pd.Timestamp, symbols: list[str] | None = None, setup_ids: list[str] | None = None) -> pd.DataFrame:
    if not table_exists(WATCHLIST_TABLE):
        return pd.DataFrame()
    available = table_columns(WATCHLIST_TABLE)
    select_items = [
        "asof_date",
        "state_updated_at AS published_on",
        "setup_id",
        "symbol",
        "watch_status",
        "current_state",
        "candidate_state",
        "watch_reason_detail",
        "attractive_price_low",
        "invalidation_price",
    ]
    optional_columns = [
        "watch_reasons_json",
        "watch_source",
        "context_source",
        "context_overlay_id",
        "context_authority_scope",
        "context_policy_effect",
        "context_reliability_classification",
        "context_class_reliability_classification",
        "context_reliability_evaluated_at",
    ]
    select_items.extend(column for column in optional_columns if column in available)
    clauses = ["asof_date = %s", "COALESCE(current_state, watch_status, '') NOT ILIKE 'abstain%%'"]
    if "watch_enabled" in available:
        clauses.append("COALESCE(watch_enabled, false) IS TRUE")
    if "candidate_state" in available:
        clauses.append("UPPER(TRIM(COALESCE(candidate_state, ''))) <> 'REJECT'")
    if "current_state" in available:
        clauses.append("UPPER(TRIM(COALESCE(current_state, ''))) <> 'REJECT'")
    if "watch_status" in available:
        clauses.append(
            "LOWER(TRIM(COALESCE(watch_status, ''))) NOT IN "
            "('rejected', 'inactive', 'removed', 'suppressed', 'disabled', 'abstained')"
        )
    params: list[object] = [asof_date]
    if symbols:
        clauses.append("symbol = ANY(%s)")
        params.append([str(value).upper() for value in symbols])
    if setup_ids:
        clauses.append("setup_id = ANY(%s)")
        params.append([str(value).upper() for value in setup_ids])
    return sql_to_df(
        f"""
        SELECT
            {", ".join(select_items)}
        FROM {WATCHLIST_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY state_updated_at DESC, symbol
        """,
        params=tuple(params),
    )


def load_signal_refresh_actions(*, asof_date: pd.Timestamp, symbols: list[str] | None = None) -> pd.DataFrame:
    if not table_exists(SIGNAL_REFRESH_TABLE):
        return pd.DataFrame()
    available = table_columns(SIGNAL_REFRESH_TABLE)
    required = {
        "refreshed_at",
        "symbol",
        "signal_action",
        "signal_source",
        "action_payload_json",
    }
    if not required.issubset(available):
        return pd.DataFrame()
    end_ts, end_operator = _point_in_time_cutoff(asof_date)
    start_ts = asof_date - pd.Timedelta(days=max(1, int(ACTION_SIGNAL_REFRESH_LOOKBACK_DAYS)))
    select_items = [
        "refreshed_at",
        "asof_date",
        "symbol",
        "unique_id",
        "reason",
        "signal_action",
        "signal_status",
        "signal_source",
        "confidence",
        "action_reason",
        "effect_type",
        "effect_summary",
        "previous_action",
        "action_changed",
        "authority_scope",
        "portfolio_authority",
        "broker_execution_allowed",
        "full_advisory_required",
        "action_payload_json",
        "trace_id",
    ]
    selected = [column for column in select_items if column in available]
    clauses = [
        "refreshed_at >= %s",
        f"refreshed_at {end_operator} %s",
        "UPPER(COALESCE(signal_action, '')) IN ('WATCH', 'BUY', 'BUY_MORE', 'BUY_TRIGGERED', 'ADD_ON_PULLBACK', 'MANUAL_REVIEW', 'REVIEW_MANUAL', 'REDUCE_EXPOSURE_REVIEW', 'REDUCE_REVIEW', 'TIGHTEN_STOP')",
        "LOWER(COALESCE(signal_source, '')) <> 'action_recommendation'",
    ]
    params: list[object] = [start_ts, end_ts]
    if "dry_run" in available:
        clauses.append("COALESCE(dry_run, FALSE) = FALSE")
    if "full_advisory_required" in available:
        clauses.append("COALESCE(full_advisory_required, TRUE) = TRUE")
    if symbols:
        clauses.append("UPPER(symbol) = ANY(%s)")
        params.append([str(value).upper() for value in symbols])
    params.append(max(1, int(ACTION_SIGNAL_REFRESH_MAX_ROWS)))
    return sql_to_df(
        f"""
        SELECT DISTINCT ON (UPPER(symbol), UPPER(signal_action), LOWER(COALESCE(signal_source, '')))
            {", ".join(selected)}
        FROM {SIGNAL_REFRESH_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY UPPER(symbol), UPPER(signal_action), LOWER(COALESCE(signal_source, '')), refreshed_at DESC
        LIMIT %s
        """,
        params=tuple(params),
    )


def load_playbook_action_plans(*, asof_date: pd.Timestamp, symbols: list[str] | None = None) -> pd.DataFrame:
    ensure_hypothesis_tables()
    if not table_exists(ACTION_PLANS_TABLE):
        return pd.DataFrame()
    end_ts, end_operator = _point_in_time_cutoff(asof_date)
    start_ts = asof_date - pd.Timedelta(days=PLAYBOOK_LOOKBACK_DAYS)
    clauses = [
        "COALESCE(p.production_allowed, FALSE) = TRUE",
        "COALESCE(h.status, '') IN ('trusted_overlay', 'production')",
        "p.planned_at >= %s",
        f"p.planned_at {end_operator} %s",
    ]
    params: list[object] = [start_ts, end_ts]
    if symbols:
        clauses.append("(p.symbol IS NULL OR p.symbol = '' OR UPPER(p.symbol) = ANY(%s))")
        params.append([str(value).upper() for value in symbols])
    return sql_to_df(
        f"""
        SELECT p.*
        FROM {ACTION_PLANS_TABLE} p
        JOIN {HYPOTHESES_TABLE} h
          ON h.hypothesis_id = p.hypothesis_id
        WHERE {' AND '.join(clauses)}
        ORDER BY planned_at DESC, confidence DESC NULLS LAST
        """,
        params=tuple(params),
    )


def load_event_policy_actions(*, asof_date: pd.Timestamp, symbols: list[str] | None = None, setup_ids: list[str] | None = None) -> pd.DataFrame:
    if not table_exists(EVENT_POLICY_TABLE):
        return pd.DataFrame()
    clauses = [
        "asof_date = %s",
        "action_type IN ('BUY_WATCH', 'REDUCE_EXPOSURE_REVIEW', 'MANUAL_REVIEW')",
    ]
    params: list[object] = [asof_date]
    if symbols:
        clauses.append("UPPER(TRIM(symbol)) = ANY(%s)")
        params.append([str(value).upper() for value in symbols])
    if setup_ids:
        clauses.append("UPPER(TRIM(setup_id)) = ANY(%s)")
        params.append([str(value).upper() for value in setup_ids])
    return sql_to_df(
        f"""
        SELECT *
        FROM {EVENT_POLICY_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY published_on DESC NULLS LAST, policy_score DESC NULLS LAST
        """,
        params=tuple(params),
    )


def load_company_memory_reviews(
    *,
    asof_date: pd.Timestamp,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
) -> pd.DataFrame:
    if setup_ids:
        allowed_setup_ids = {str(value).strip().upper() for value in setup_ids if str(value or "").strip()}
        if "COMPANY_MEMORY_REVIEW" not in allowed_setup_ids and "COMPANY_MEMORY" not in allowed_setup_ids:
            return pd.DataFrame()
    if not ACTION_COMPANY_MEMORY_BRIDGE_ENABLED:
        return pd.DataFrame()
    try:
        exists = table_exists(COMPANY_MEMORY_REVIEWS_TABLE)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.action_recommender",
            fallback_type="action_recommender_company_memory_reviews_table_lookup_failed",
            source=COMPANY_MEMORY_REVIEWS_TABLE,
            severity="warn",
            reason="Action recommender could not check whether company-memory review candidates are available.",
            error=exc,
            metadata={
                "asof_date": None if pd.isna(asof_date) else asof_date.isoformat(),
                "symbol_count": len(symbols or []),
                "setup_id_count": len(setup_ids or []),
            },
        )
        return pd.DataFrame()
    if not exists:
        return pd.DataFrame()
    start_date = asof_date - pd.Timedelta(days=max(1, int(ACTION_COMPANY_MEMORY_LOOKBACK_DAYS)))
    clauses = [
        "review_date <= %(asof_date)s",
        "review_date >= %(start_date)s",
        "recommended_signal IN ('BUY', 'BUY_MORE', 'WATCH', 'SELL_PARTIAL', 'SELL')",
        "COALESCE(authority_scope, 'review_input_only') = 'review_input_only'",
        "COALESCE(review_status, '') IN ('completed', 'fallback_completed')",
    ]
    params: dict[str, Any] = {"asof_date": asof_date, "start_date": start_date}
    if symbols:
        normalized_symbols = [str(value).strip().upper() for value in symbols if str(value or "").strip()]
        if normalized_symbols:
            clauses.append("UPPER(TRIM(symbol)) = ANY(%(symbols)s)")
            params["symbols"] = normalized_symbols
    try:
        return sql_to_df(
            f"""
            SELECT DISTINCT ON (UPPER(TRIM(symbol)))
                review_date,
                symbol,
                recommended_signal,
                confidence,
                conviction_score,
                summary,
                thesis,
                risk_flags_json,
                evidence_used_json,
                wait_for_json,
                deterministic_boundary,
                authority_scope,
                model_name,
                prompt_id,
                prompt_version,
                prompt_schema_version,
                review_status,
                fallback_used,
                payload_json
            FROM {COMPANY_MEMORY_REVIEWS_TABLE}
            WHERE {' AND '.join(clauses)}
            ORDER BY UPPER(TRIM(symbol)), review_date DESC, confidence DESC NULLS LAST
            """,
            params=params,
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.action_recommender",
            fallback_type="action_recommender_company_memory_reviews_load_failed",
            source=COMPANY_MEMORY_REVIEWS_TABLE,
            severity="warn",
            reason="Action recommender could not load company-memory reviews and skipped review-only memory candidates.",
            error=exc,
            metadata={
                "asof_date": None if pd.isna(asof_date) else asof_date.isoformat(),
                "lookback_days": int(ACTION_COMPANY_MEMORY_LOOKBACK_DAYS),
                "symbol_count": len(symbols or []),
            },
        )
        return pd.DataFrame()


def _compact_action_company_memory_review(row: pd.Series) -> dict[str, Any]:
    payload = _parse_jsonish(row.get("payload_json"), {})
    if not isinstance(payload, dict):
        payload = {}
    risk_flags = _parse_jsonish(row.get("risk_flags_json"), [])
    evidence_used = _parse_jsonish(row.get("evidence_used_json"), [])
    wait_for = _parse_jsonish(row.get("wait_for_json"), [])
    return {
        "review_date": row.get("review_date"),
        "recommended_signal": row.get("recommended_signal"),
        "confidence": _num(row.get("confidence")),
        "conviction_score": _num(row.get("conviction_score")),
        "summary": row.get("summary"),
        "thesis": row.get("thesis"),
        "risk_flags": risk_flags if isinstance(risk_flags, list) else [],
        "evidence_used": evidence_used if isinstance(evidence_used, list) else [],
        "wait_for": wait_for if isinstance(wait_for, list) else [],
        "deterministic_boundary": row.get("deterministic_boundary"),
        "authority_scope": row.get("authority_scope") or "review_input_only",
        "action_policy_effect": "explain_only_no_ranking_change",
        "portfolio_authority": "none",
        "broker_execution_allowed": False,
        "full_advisory_required": True,
        "model_name": row.get("model_name"),
        "prompt_id": row.get("prompt_id"),
        "prompt_version": row.get("prompt_version"),
        "prompt_schema_version": row.get("prompt_schema_version"),
        "review_status": row.get("review_status"),
        "fallback_used": bool(row.get("fallback_used")) if row.get("fallback_used") is not None else False,
        "evidence_source_contract": payload.get("evidence_source_contract"),
        "context_overlay_summary": payload.get("context_overlay_summary"),
    }


def _load_latest_action_company_memory_reviews(asof_date: pd.Timestamp, symbols: list[str]) -> dict[str, dict[str, Any]]:
    normalized_symbols = sorted({str(value).strip().upper() for value in symbols if str(value or "").strip()})
    if not normalized_symbols:
        return {}
    try:
        exists = table_exists(COMPANY_MEMORY_REVIEWS_TABLE)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.action_recommender",
            fallback_type="action_recommender_company_memory_context_table_lookup_failed",
            source=COMPANY_MEMORY_REVIEWS_TABLE,
            severity="warn",
            reason="Action recommender could not check whether company-memory review context is available.",
            error=exc,
            metadata={
                "asof_date": None if pd.isna(asof_date) else asof_date.isoformat(),
                "symbol_count": len(normalized_symbols),
            },
        )
        return {}
    if not exists:
        return {}
    start_date = asof_date - pd.Timedelta(days=max(1, int(ACTION_COMPANY_MEMORY_LOOKBACK_DAYS)))
    clauses = [
        "review_date <= %(asof_date)s",
        "review_date >= %(start_date)s",
        "COALESCE(authority_scope, 'review_input_only') = 'review_input_only'",
        "COALESCE(review_status, '') IN ('completed', 'fallback_completed')",
        "UPPER(TRIM(symbol)) = ANY(%(symbols)s)",
    ]
    params: dict[str, Any] = {
        "asof_date": asof_date,
        "start_date": start_date,
        "symbols": normalized_symbols,
    }
    try:
        df = sql_to_df(
            f"""
            SELECT DISTINCT ON (UPPER(TRIM(symbol)))
                review_date,
                symbol,
                recommended_signal,
                confidence,
                conviction_score,
                summary,
                thesis,
                risk_flags_json,
                evidence_used_json,
                wait_for_json,
                deterministic_boundary,
                authority_scope,
                model_name,
                prompt_id,
                prompt_version,
                prompt_schema_version,
                review_status,
                fallback_used,
                payload_json
            FROM {COMPANY_MEMORY_REVIEWS_TABLE}
            WHERE {' AND '.join(clauses)}
            ORDER BY UPPER(TRIM(symbol)), review_date DESC, confidence DESC NULLS LAST
            """,
            params=params,
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.action_recommender",
            fallback_type="action_recommender_company_memory_context_load_failed",
            source=COMPANY_MEMORY_REVIEWS_TABLE,
            severity="warn",
            reason="Action recommender could not load latest company-memory review context for recommendation explanations.",
            error=exc,
            metadata={
                "asof_date": None if pd.isna(asof_date) else asof_date.isoformat(),
                "lookback_days": int(ACTION_COMPANY_MEMORY_LOOKBACK_DAYS),
                "symbol_count": len(normalized_symbols),
            },
        )
        return {}
    if df.empty:
        return {}
    output: dict[str, dict[str, Any]] = {}
    for _, row in df.iterrows():
        symbol = str(row.get("symbol") or "").strip().upper()
        if symbol:
            output[symbol] = _compact_action_company_memory_review(row)
    return output


def load_matched_manual_review_wait_signals(*, asof_date: pd.Timestamp, symbols: list[str] | None = None, setup_ids: list[str] | None = None) -> pd.DataFrame:
    if not table_exists(WAIT_SIGNALS_TABLE) or not table_exists(WAIT_SIGNAL_MATCHES_TABLE):
        return pd.DataFrame()
    if setup_ids:
        allowed_setup_ids = {str(value).strip().upper() for value in setup_ids if str(value or "").strip()}
        if MANUAL_REVIEW_WAIT_SIGNAL_SETUP_ID not in allowed_setup_ids and "MANUAL_REVIEW" not in allowed_setup_ids:
            return pd.DataFrame()
    end_ts, end_operator = _point_in_time_cutoff(asof_date)
    clauses = [
        "s.generated_by = 'manual_review_decision'",
        "COALESCE(m.match_status, 'matched') = 'matched'",
        f"m.matched_at {end_operator} %(end_ts)s",
    ]
    params: dict[str, Any] = {"end_ts": end_ts}
    if symbols:
        normalized_symbols = [str(value).strip().upper() for value in symbols if str(value or "").strip()]
        if normalized_symbols:
            clauses.append("UPPER(TRIM(COALESCE(s.symbol, m.symbol, ''))) = ANY(%(symbols)s)")
            params["symbols"] = normalized_symbols
    return sql_to_df(
        f"""
        SELECT DISTINCT ON (m.signal_id)
            m.matched_at,
            m.signal_id,
            m.hypothesis_id,
            m.symbol AS match_symbol,
            m.signal_type,
            m.expected_action,
            m.match_status,
            m.match_score,
            m.source_table AS match_source_table,
            m.source_key AS match_source_key,
            m.observed_at,
            m.observed_value,
            m.threshold_value,
            m.match_reason,
            m.evidence_json,
            s.created_at AS signal_created_at,
            s.hypothesis_title,
            s.source_table AS signal_source_table,
            s.source_key AS signal_source_key,
            s.symbol AS signal_symbol,
            s.status AS signal_status,
            s.priority,
            s.expected_action AS signal_expected_action,
            s.operator_summary,
            s.wait_question,
            s.condition_json,
            s.valid_until,
            s.generated_by
        FROM {WAIT_SIGNAL_MATCHES_TABLE} m
        JOIN {WAIT_SIGNALS_TABLE} s
          ON s.signal_id = m.signal_id
        WHERE {' AND '.join(clauses)}
        ORDER BY m.signal_id, m.matched_at DESC NULLS LAST
        """,
        params=params,
    )


def _parse_jsonish(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        if pd.isna(value):
            return default
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.action_recommender",
            fallback_type="action_recommender_missing_check_failed",
            source="parse_jsonish",
            severity="warn",
            reason="Action recommender could not evaluate missingness for a stored JSON context value and continued parsing.",
            error=exc,
            metadata={
                "value_type": type(value).__name__,
                "default_type": type(default).__name__,
            },
        )
    try:
        return json.loads(str(value))
    except Exception as exc:
        if isinstance(value, str) and value.strip():
            record_local_fallback_event(
                module="advisory.action_recommender",
                fallback_type="action_recommender_json_parse_failed",
                source="json_context",
                severity="warn",
                reason="Action recommender could not parse stored JSON context and used the provided default.",
                error=exc,
                metadata={
                    "default_type": type(default).__name__,
                    "value_length": len(value),
                    "value_excerpt": value[:240],
                },
            )
        return default


def _json_context_value(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    try:
        if pd.api.types.is_scalar(value) and pd.isna(value):
            return None
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.action_recommender",
            fallback_type="action_recommender_context_missing_check_failed",
            source="json_context_value",
            severity="warn",
            reason="Action recommender could not evaluate missingness for a context value and kept the original value.",
            error=exc,
            metadata={"value_type": type(value).__name__},
        )
    return value


def _merge_context(base: Any, *extras: dict[str, Any]) -> dict[str, Any]:
    context = _parse_jsonish(base, {})
    if not isinstance(context, dict):
        context = {}
    for extra in extras:
        for key, value in (extra or {}).items():
            normalized = _json_context_value(value)
            if normalized is None:
                continue
            existing = context.get(key)
            if key not in context or existing is None or existing == "":
                context[key] = normalized
    return context


def _load_latest_candidate_context(asof_date: pd.Timestamp, symbols: list[str]) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, dict[str, Any]]]:
    normalized_symbols = sorted({str(value).strip().upper() for value in symbols if str(value or "").strip()})
    if not normalized_symbols or not table_exists(CANDIDATES_TABLE):
        return {}, {}
    available = table_columns(CANDIDATES_TABLE)
    wanted = [
        "asof_date",
        "screener_date",
        "setup_id",
        "setup_name",
        "setup_family",
        "holding_horizon_note",
        "regime_name",
        "base_regime",
        "news_overlay",
        "theme_ids",
        "symbol",
        "screener_slug",
        "source_screener_slug",
        "source_screener_list",
        "rank",
        "candidate_state",
        "watch_reason_detail",
        "soft_failures_json",
        "technical_state",
        "technical_trigger_type",
        "technical_trigger_note",
        "technical_setup_archetype",
        "technical_setup_quality_json",
        "technical_trend_score",
        "technical_structure_score",
        "technical_participation_score",
        "technical_relative_strength_score",
        "technical_tradability_score",
        "technical_score",
        "fundamental_score",
        "regime_fit_score",
        "regime_fit_weight_effective",
        "event_score",
        "setup_score",
        "avg_traded_value_20d",
        "rs_vs_benchmark",
        "rs_vs_sector",
        "intraday_breakout_score",
        "intraday_pattern_label",
        "entry_style",
        "attractive_price_low",
        "attractive_price_high",
        "invalidation_price",
        "entry_note",
        "near_miss_flag",
        "watch_reasons",
        "rule_pass",
        "load_ts",
    ]
    selected = [column for column in wanted if column in available]
    if not {"asof_date", "symbol", "setup_id"}.issubset(set(selected)):
        return {}, {}
    order_parts = ["asof_date DESC"]
    if "setup_score" in available:
        order_parts.append("setup_score DESC NULLS LAST")
    if "load_ts" in available:
        order_parts.append("load_ts DESC NULLS LAST")
    df = sql_to_df(
        f"""
        WITH ranked AS (
            SELECT
                {", ".join(selected)},
                ROW_NUMBER() OVER (
                    PARTITION BY UPPER(TRIM(symbol)), COALESCE(UPPER(TRIM(setup_id)), '')
                    ORDER BY {", ".join(order_parts)}
                ) AS rn
            FROM {CANDIDATES_TABLE}
            WHERE asof_date <= %(asof_date)s
              AND UPPER(TRIM(symbol)) = ANY(%(symbols)s)
        )
        SELECT {", ".join(selected)}
        FROM ranked
        WHERE rn = 1
        """,
        params={"asof_date": asof_date, "symbols": normalized_symbols},
    )
    if df.empty:
        return {}, {}
    for column in ["asof_date", "screener_date", "load_ts"]:
        if column in df.columns:
            df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()
    df["setup_id"] = df["setup_id"].astype("string").str.strip().str.upper()
    exact: dict[tuple[str, str], dict[str, Any]] = {}
    by_symbol: dict[str, dict[str, Any]] = {}
    for _, row in df.iterrows():
        payload = {key: _json_context_value(value) for key, value in row.to_dict().items() if _json_context_value(value) is not None}
        symbol = str(payload.get("symbol") or "").upper()
        setup_id = str(payload.get("setup_id") or "").upper()
        if symbol and setup_id:
            exact[(setup_id, symbol)] = payload
        if symbol and symbol not in by_symbol:
            by_symbol[symbol] = payload
        elif symbol and "setup_score" in df.columns:
            current_score = pd.to_numeric(by_symbol[symbol].get("setup_score"), errors="coerce")
            next_score = pd.to_numeric(payload.get("setup_score"), errors="coerce")
            if pd.notna(next_score) and (pd.isna(current_score) or float(next_score) > float(current_score)):
                by_symbol[symbol] = payload
    return exact, by_symbol


def _load_regime_context(asof_date: pd.Timestamp) -> dict[str, Any]:
    if not table_exists(REGIME_TABLE):
        return {}
    available = table_columns(REGIME_TABLE)
    wanted = [
        "asof_date",
        "regime_name",
        "regime_notes",
        "shock_flag",
        "risk_off_flag",
        "macro_stress_score",
        "macro_risk_state",
        "macro_sizing_multiplier",
    ]
    selected = [column for column in wanted if column in available]
    if "asof_date" not in selected:
        return {}
    df = sql_to_df(
        f"""
        SELECT {", ".join(selected)}
        FROM {REGIME_TABLE}
        WHERE asof_date <= %s
        ORDER BY asof_date DESC
        LIMIT 1
        """,
        params=(asof_date,),
    )
    if df.empty:
        return {}
    row = df.iloc[0].to_dict()
    return {
        ("regime_asof_date" if key == "asof_date" else key): _json_context_value(value)
        for key, value in row.items()
        if _json_context_value(value) is not None
    }


def _load_latest_event_context(asof_date: pd.Timestamp, symbols: list[str]) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, dict[str, Any]]]:
    normalized_symbols = sorted({str(value).strip().upper() for value in symbols if str(value or "").strip()})
    if not normalized_symbols or not table_exists(EVENT_EVALUATIONS_TABLE):
        return {}, {}
    available = table_columns(EVENT_EVALUATIONS_TABLE)
    wanted = [
        "published_on",
        "asof_date",
        "evaluated_at",
        "setup_id",
        "setup_name",
        "symbol",
        "unique_id",
        "event_source",
        "subject",
        "evaluation_status",
        "sentiment",
        "materiality",
        "setup_effect",
        "direction",
        "surprise",
        "novelty",
        "contradiction",
        "expected_decay_days",
        "source_reliability",
        "investable_now",
        "verdict",
        "event_class",
        "state_transition_hint",
        "score_impact",
        "confidence",
        "what_happened",
        "rationale",
        "event_tensor_json",
        "source_trace_json",
    ]
    selected = [column for column in wanted if column in available]
    if not {"published_on", "symbol", "setup_id", "unique_id"}.issubset(set(selected)):
        return {}, {}
    evidence_select = ""
    evidence_join = ""
    if table_exists(ANNOUNCEMENT_EVIDENCE_TABLE):
        evidence_available = table_columns(ANNOUNCEMENT_EVIDENCE_TABLE)
        evidence_cols = [
            "evidence_id",
            "unique_id",
            "raw_s3_key",
            "pdf_s3_key",
            "ocr_s3_key",
            "full_ocr_s3_key",
            "audio_transcript_s3_key",
            "concise_summary_s3_key",
            "attachment_url",
            "attachment_name",
            "announcement_storage_form",
            "announcement_storage_reason",
            "llm_evidence_mode",
            "llm_review_ready",
            "raw_archive_required",
            "has_s3_evidence",
            "has_text_evidence",
            "source_reliability",
        ]
        selected_evidence = [column for column in evidence_cols if column in evidence_available]
        if selected_evidence and {"published_on", "unique_id"}.issubset(evidence_available):
            evidence_select = ", " + ", ".join(f"ae.{column} AS event_evidence_{column}" for column in selected_evidence)
            evidence_join = f"""
            LEFT JOIN {ANNOUNCEMENT_EVIDENCE_TABLE} ae
              ON ae.published_on = e.published_on
             AND ae.unique_id = e.unique_id
            """
    review_select = ""
    review_join = ""
    if table_exists(EVENT_REVIEWS_TABLE):
        review_available = table_columns(EVENT_REVIEWS_TABLE)
        review_cols = [
            "review_status",
            "review_action",
            "review_score",
            "veto",
            "review_reason",
            "review_flags_json",
        ]
        selected_review = [column for column in review_cols if column in review_available]
        if selected_review:
            review_select = ", " + ", ".join(f"r.{column} AS {column}" for column in selected_review)
            review_join = f"""
            LEFT JOIN {EVENT_REVIEWS_TABLE} r
              ON r.published_on = e.published_on
             AND r.setup_id = e.setup_id
             AND r.symbol = e.symbol
             AND r.unique_id = e.unique_id
            """
    asof_cutoff, asof_operator = _point_in_time_cutoff(asof_date)
    df = sql_to_df(
        f"""
        WITH ranked AS (
            SELECT
                {", ".join(f"e.{column}" for column in selected)}
                {evidence_select}
                {review_select},
                ROW_NUMBER() OVER (
                    PARTITION BY UPPER(TRIM(e.symbol)), COALESCE(UPPER(TRIM(e.setup_id)), '')
                    ORDER BY e.published_on DESC NULLS LAST, e.evaluated_at DESC NULLS LAST
                ) AS rn_setup,
                ROW_NUMBER() OVER (
                    PARTITION BY UPPER(TRIM(e.symbol))
                    ORDER BY e.published_on DESC NULLS LAST, e.evaluated_at DESC NULLS LAST
                ) AS rn_symbol
            FROM {EVENT_EVALUATIONS_TABLE} e
            {evidence_join}
            {review_join}
            WHERE COALESCE(e.asof_date, e.published_on) {asof_operator} %(asof_date)s
              AND UPPER(TRIM(e.symbol)) = ANY(%(symbols)s)
        )
        SELECT *
        FROM ranked
        WHERE rn_setup = 1 OR rn_symbol = 1
        """,
        params={"asof_date": asof_cutoff, "symbols": normalized_symbols},
    )
    if df.empty:
        return {}, {}
    for column in ["published_on", "asof_date", "evaluated_at"]:
        if column in df.columns:
            df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()
    df["setup_id"] = df["setup_id"].astype("string").str.strip().str.upper()
    exact: dict[tuple[str, str], dict[str, Any]] = {}
    by_symbol: dict[str, dict[str, Any]] = {}
    for _, row in df.iterrows():
        payload = {
            f"event_{key}" if key in {"asof_date", "published_on", "setup_id", "unique_id", "source_trace_json"} else key: _json_context_value(value)
            for key, value in row.to_dict().items()
            if key not in {"rn_setup", "rn_symbol"} and _json_context_value(value) is not None
        }
        symbol = str(row.get("symbol") or "").upper()
        setup_id = str(row.get("setup_id") or "").upper()
        if symbol and setup_id and int(row.get("rn_setup") or 0) == 1:
            exact[(setup_id, symbol)] = payload
        if symbol and int(row.get("rn_symbol") or 0) == 1:
            by_symbol[symbol] = payload
    return exact, by_symbol


def _load_latest_playbook_context(asof_date: pd.Timestamp, symbols: list[str]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    normalized_symbols = sorted({str(value).strip().upper() for value in symbols if str(value or "").strip()})
    if not table_exists(ACTION_PLANS_TABLE):
        return {}, {}
    available = table_columns(ACTION_PLANS_TABLE)
    wanted = [
        "planned_at",
        "hypothesis_id",
        "source_table",
        "source_key",
        "symbol",
        "trigger_scope",
        "suggested_action",
        "action_type",
        "urgency",
        "confidence",
        "production_allowed",
        "operator_summary",
        "decision_reason",
        "checks_json",
        "risk_controls_json",
        "action_plan_json",
        "llm_status",
    ]
    selected = [column for column in wanted if column in available]
    if not {"planned_at", "hypothesis_id", "source_key"}.issubset(set(selected)):
        return {}, {}
    asof_cutoff, asof_operator = _point_in_time_cutoff(asof_date)
    clauses = [f"planned_at {asof_operator} %(asof_date)s"]
    params: dict[str, Any] = {"asof_date": asof_cutoff}
    if "symbol" in selected and normalized_symbols:
        clauses.append("(symbol IS NULL OR symbol = '' OR UPPER(TRIM(symbol)) = ANY(%(symbols)s))")
        params["symbols"] = normalized_symbols
    df = sql_to_df(
        f"""
        WITH ranked AS (
            SELECT
                {", ".join(selected)},
                ROW_NUMBER() OVER (
                    PARTITION BY NULLIF(UPPER(TRIM(COALESCE(symbol, ''))), '')
                    ORDER BY planned_at DESC NULLS LAST, confidence DESC NULLS LAST
                ) AS rn_symbol,
                ROW_NUMBER() OVER (
                    PARTITION BY source_key
                    ORDER BY planned_at DESC NULLS LAST, confidence DESC NULLS LAST
                ) AS rn_source
            FROM {ACTION_PLANS_TABLE}
            WHERE {' AND '.join(clauses)}
        )
        SELECT *
        FROM ranked
        WHERE rn_symbol = 1 OR rn_source = 1
        """,
        params=params,
    )
    if df.empty:
        return {}, {}
    if "planned_at" in df.columns:
        df["planned_at"] = pd.to_datetime(df["planned_at"], utc=True, errors="coerce")
    by_symbol: dict[str, dict[str, Any]] = {}
    by_source: dict[str, dict[str, Any]] = {}
    for _, row in df.iterrows():
        payload = {
            ("playbook_id" if key == "hypothesis_id" else f"playbook_{key}" if key in {"source_table", "source_key", "symbol", "planned_at"} else key): _json_context_value(value)
            for key, value in row.to_dict().items()
            if key not in {"rn_symbol", "rn_source"} and _json_context_value(value) is not None
        }
        symbol = str(row.get("symbol") or "").upper()
        source_key = str(row.get("source_key") or "")
        if symbol and int(row.get("rn_symbol") or 0) == 1:
            by_symbol[symbol] = payload
        if source_key and int(row.get("rn_source") or 0) == 1:
            by_source[source_key] = payload
    return by_symbol, by_source


def _action_context_overlay_reason_expr(columns: set[str], *, table_alias: str = "") -> str:
    prefix = f"{table_alias}." if table_alias else ""
    if "watch_reason_detail" in columns:
        return f"{prefix}watch_reason_detail"
    if "trigger_reason" in columns:
        return f"{prefix}trigger_reason"
    if "theme_reason" in columns:
        return f"{prefix}theme_reason"
    return "NULL::text"


def _announcement_taxonomy_filter(columns: set[str], *, table_alias: str = "o") -> tuple[str, dict[str, Any]]:
    if not {"announcement_storage_form", "llm_review_ready"}.intersection(columns):
        return "", {
            "taxonomy_filter_applied": False,
            "archive_only_rows_excluded": False,
            "review_ready_rows_required": False,
        }
    prefix = f"{table_alias}." if table_alias else ""
    clauses: list[str] = []
    if "announcement_storage_form" in columns:
        clauses.append(f"COALESCE(NULLIF(TRIM({prefix}announcement_storage_form), ''), 'compact_structured_event') <> 'archived_raw_reference_only'")
    if "llm_review_ready" in columns:
        clauses.append(f"COALESCE({prefix}llm_review_ready, TRUE) IS TRUE")
    return (
        "\n              " + "\n              ".join(f"AND {clause}" for clause in clauses),
        {
            "taxonomy_filter_applied": bool(clauses),
            "archive_only_rows_excluded": "announcement_storage_form" in columns,
            "review_ready_rows_required": "llm_review_ready" in columns,
        },
    )


def _load_direct_action_context_overlay_rows(
    *,
    table_name: str,
    source_name: str,
    asof_date: pd.Timestamp,
    symbols: list[str],
    date_column: str,
    class_column: str | None = None,
) -> pd.DataFrame:
    normalized_symbols = sorted({str(value).strip().upper() for value in symbols if str(value or "").strip()})
    if not normalized_symbols or not table_exists(table_name):
        return pd.DataFrame()
    columns = table_columns(table_name)
    if not {"symbol", date_column, "direction"}.issubset(columns):
        return pd.DataFrame()
    asof_cutoff, asof_operator = _point_in_time_cutoff(asof_date)
    from_date = asof_cutoff - pd.Timedelta(days=max(0, int(ACTION_CONTEXT_OVERLAY_LOOKBACK_DAYS)))
    score_expr = "o.pressure_score" if "pressure_score" in columns else "NULL::double precision"
    class_expr = f"o.{class_column}" if class_column and class_column in columns else "NULL::text"
    overlay_expr = "o.overlay_id" if "overlay_id" in columns else "NULL::text"
    reason_expr = _action_context_overlay_reason_expr(columns, table_alias="o")
    sector_name_expr = "o.sector_name" if "sector_name" in columns else "NULL::text"
    sector_code_expr = "o.sector_code" if "sector_code" in columns else "NULL::text"
    production_filter = "AND COALESCE(o.production_status, 'active') = 'active'" if "production_status" in columns else ""
    authority_filter = (
        "AND COALESCE(o.authority_scope, 'watchlist_pressure_only') = 'watchlist_pressure_only'"
        if "authority_scope" in columns
        else ""
    )
    taxonomy_filter, taxonomy_contract = (
        _announcement_taxonomy_filter(columns, table_alias="o")
        if source_name == "announcement_context"
        else (
            "",
            {
                "taxonomy_filter_applied": False,
                "archive_only_rows_excluded": False,
                "review_ready_rows_required": False,
            },
        )
    )
    try:
        df = sql_to_df(
            f"""
            SELECT
                UPPER(TRIM(o.symbol)) AS symbol,
                o.{date_column} AS context_asof_date,
                %(source_name)s::text AS context_source,
                {overlay_expr} AS overlay_id,
                o.direction,
                {score_expr} AS pressure_score,
                {class_expr} AS context_class,
                {reason_expr} AS reason,
                {sector_name_expr} AS sector_name,
                {sector_code_expr} AS sector_code
            FROM {table_name} o
            WHERE o.{date_column} >= %(from_date)s
              AND o.{date_column} {asof_operator} %(asof_date)s
              AND UPPER(TRIM(o.symbol)) = ANY(%(symbols)s)
              {production_filter}
              {authority_filter}
              {taxonomy_filter}
            ORDER BY symbol, o.{date_column} DESC NULLS LAST, pressure_score DESC NULLS LAST
            """,
            params={
                "from_date": from_date,
                "asof_date": asof_cutoff,
                "symbols": normalized_symbols,
                "source_name": source_name,
            },
            retries=3,
            statement_timeout_ms=10000,
        )
        if not df.empty and source_name == "announcement_context":
            df["compact_evidence_contract"] = json.dumps(
                {
                    "source_table": table_name,
                    "source_family": source_name,
                    **taxonomy_contract,
                    "raw_announcement_scan_allowed": False,
                    "authority_scope": "watchlist_pressure_only",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        return df
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.action_recommender",
            fallback_type="action_recommender_context_overlay_load_failed",
            source=table_name,
            severity="warn",
            reason="Action recommender could not load direct context-overlay rows for read-only action reasoning.",
            error=exc,
            metadata={"source_name": source_name, "symbol_count": len(normalized_symbols), "lookback_days": ACTION_CONTEXT_OVERLAY_LOOKBACK_DAYS},
        )
        return pd.DataFrame()


def _load_sector_action_context_overlay_rows(
    *,
    table_name: str,
    source_name: str,
    asof_date: pd.Timestamp,
    symbols: list[str],
    class_column: str | None,
    reason_column: str | None,
) -> pd.DataFrame:
    normalized_symbols = sorted({str(value).strip().upper() for value in symbols if str(value or "").strip()})
    if not normalized_symbols or not table_exists(table_name) or not table_exists(MARKET_CONTEXT_UNIVERSE_TABLE):
        return pd.DataFrame()
    overlay_columns = table_columns(table_name)
    universe_columns = table_columns(MARKET_CONTEXT_UNIVERSE_TABLE)
    if not {"asof_date", "direction", "sector_name", "sector_code"}.issubset(overlay_columns):
        return pd.DataFrame()
    if not {"asof_date", "symbol", "sector_name", "sector_code"}.issubset(universe_columns):
        return pd.DataFrame()
    asof_cutoff, asof_operator = _point_in_time_cutoff(asof_date)
    from_date = asof_cutoff - pd.Timedelta(days=max(0, int(ACTION_CONTEXT_OVERLAY_LOOKBACK_DAYS)))
    score_expr = "o.pressure_score" if "pressure_score" in overlay_columns else "NULL::double precision"
    overlay_expr = "o.overlay_id" if "overlay_id" in overlay_columns else "NULL::text"
    class_expr = f"o.{class_column}" if class_column and class_column in overlay_columns else "NULL::text"
    reason_expr = f"o.{reason_column}" if reason_column and reason_column in overlay_columns else "NULL::text"
    production_filter = "AND COALESCE(o.production_status, 'active') = 'active'" if "production_status" in overlay_columns else ""
    authority_filter = (
        "AND COALESCE(o.authority_scope, 'watchlist_pressure_only') = 'watchlist_pressure_only'"
        if "authority_scope" in overlay_columns
        else ""
    )
    alias_cte = ""
    alias_join = ""
    alias_match = "FALSE"
    if table_name == MACRO_CONTEXT_OVERLAYS_TABLE:
        alias_cte = f"""
            macro_sector_alias(overlay_sector_key, universe_sector_code) AS (
                VALUES
                {macro_sector_alias_values_sql()}
            ),
        """
        alias_join = """
            LEFT JOIN macro_sector_alias msa
              ON msa.overlay_sector_key = regexp_replace(upper(coalesce(o.sector_name, o.sector_code, '')), '[^A-Z0-9]', '', 'g')
        """
        alias_match = "u.sector_code_key = msa.universe_sector_code"
    elif table_name == THEME_CONTEXT_OVERLAYS_TABLE:
        alias_cte = f"""
            theme_sector_alias(overlay_sector_key, universe_sector_code) AS (
                VALUES
                {theme_sector_alias_values_sql()}
            ),
        """
        alias_join = """
            LEFT JOIN theme_sector_alias tsa
              ON tsa.overlay_sector_key = regexp_replace(upper(coalesce(o.sector_name, o.sector_code, '')), '[^A-Z0-9]', '', 'g')
        """
        alias_match = "u.sector_code_key = tsa.universe_sector_code"
    try:
        return sql_to_df(
            f"""
            WITH
            {alias_cte}
            selected_universe AS MATERIALIZED (
                SELECT DISTINCT ON (UPPER(TRIM(symbol)))
                    UPPER(TRIM(symbol)) AS symbol,
                    asof_date,
                    regexp_replace(upper(coalesce(sector_name, '')), '[^A-Z0-9]', '', 'g') AS sector_name_key,
                    regexp_replace(upper(coalesce(sector_code, '')), '[^A-Z0-9]', '', 'g') AS sector_code_key
                FROM {MARKET_CONTEXT_UNIVERSE_TABLE}
                WHERE asof_date >= %(from_date)s
                  AND asof_date {asof_operator} %(asof_date)s
                  AND UPPER(TRIM(symbol)) = ANY(%(symbols)s)
                ORDER BY UPPER(TRIM(symbol)), asof_date DESC
            )
            SELECT
                UPPER(TRIM(u.symbol)) AS symbol,
                o.asof_date AS context_asof_date,
                %(source_name)s::text AS context_source,
                {overlay_expr} AS overlay_id,
                o.direction,
                {score_expr} AS pressure_score,
                {class_expr} AS context_class,
                {reason_expr} AS reason,
                o.sector_name,
                o.sector_code
            FROM {table_name} o
            {alias_join}
            JOIN selected_universe u
              ON (
                u.sector_name_key = regexp_replace(upper(coalesce(o.sector_name, '')), '[^A-Z0-9]', '', 'g')
                OR u.sector_code_key = regexp_replace(upper(coalesce(o.sector_code, '')), '[^A-Z0-9]', '', 'g')
                OR {alias_match}
             )
            WHERE o.asof_date >= %(from_date)s
              AND o.asof_date {asof_operator} %(asof_date)s
              {production_filter}
              {authority_filter}
            ORDER BY u.symbol, o.asof_date DESC NULLS LAST, pressure_score DESC NULLS LAST
            """,
            params={
                "from_date": from_date,
                "asof_date": asof_cutoff,
                "symbols": normalized_symbols,
                "source_name": source_name,
            },
            retries=3,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.action_recommender",
            fallback_type="action_recommender_context_overlay_load_failed",
            source=table_name,
            severity="warn",
            reason="Action recommender could not load sector context-overlay rows for read-only action reasoning.",
            error=exc,
            metadata={"source_name": source_name, "symbol_count": len(normalized_symbols), "lookback_days": ACTION_CONTEXT_OVERLAY_LOOKBACK_DAYS},
        )
        return pd.DataFrame()


def _context_overlay_interpretation(directions: pd.Series) -> str:
    values = directions.dropna().astype(str).str.strip().str.lower()
    positive = int(values.eq("positive").sum())
    negative = int(values.eq("negative").sum())
    watch = int(values.eq("watch").sum())
    if positive and negative:
        return "mixed_context_pressure"
    if negative:
        return "risk_or_derisk_pressure"
    if positive:
        return "supportive_watch_pressure"
    if watch:
        return "watch_only_context"
    return "no_directional_context"


def _context_overlay_reliability_suppressed(item: dict[str, Any]) -> bool:
    family_reliability = item.get("source_family_reliability") if isinstance(item.get("source_family_reliability"), dict) else {}
    class_reliability = item.get("context_class_reliability") if isinstance(item.get("context_class_reliability"), dict) else {}
    sector_reliability = item.get("context_sector_reliability") if isinstance(item.get("context_sector_reliability"), dict) else {}
    family_classification = str(family_reliability.get("classification") or "").strip()
    class_classification = str(class_reliability.get("classification") or "").strip()
    sector_classification = str(sector_reliability.get("classification") or "").strip()
    return (
        family_classification in SIGNAL_QUALITY_OVERLAY_SUPPRESSED_CONTEXT_CLASS_RELIABILITY
        or class_classification in SIGNAL_QUALITY_OVERLAY_SUPPRESSED_CONTEXT_CLASS_RELIABILITY
        or sector_classification in SIGNAL_QUALITY_OVERLAY_SUPPRESSED_CONTEXT_CLASS_RELIABILITY
    )


def _context_overlay_action_alignment(action: str, context_overlay_summary: Any) -> dict[str, Any]:
    if not isinstance(context_overlay_summary, dict) or not context_overlay_summary:
        return {}
    action_code = str(action or "").strip().upper()
    positive_count = int(context_overlay_summary.get("positive_count") or 0)
    negative_count = int(context_overlay_summary.get("negative_count") or 0)
    watch_count = int(context_overlay_summary.get("watch_count") or 0)
    summary_effective_positive = context_overlay_summary.get("effective_positive_count")
    summary_effective_negative = context_overlay_summary.get("effective_negative_count")
    summary_effective_watch = context_overlay_summary.get("effective_watch_count")
    overlay_count = int(context_overlay_summary.get("overlay_count") or 0)
    source_counts = context_overlay_summary.get("source_family_counts")
    if not isinstance(source_counts, dict):
        source_counts = {}

    entry_actions = {"BUY", "BUY_MORE", "WATCH", "BUY_WATCH"}
    de_risk_actions = {"SELL", "PARTIAL_SELL", "FULL_EXIT", "TIGHTEN_STOP", "REDUCE_EXPOSURE_REVIEW"}
    hold_actions = {"HOLD", "NO_ACTION", "IGNORE"}

    top_overlays = [item for item in (context_overlay_summary.get("top_overlays") or []) if isinstance(item, dict)]
    suppressed_overlays: list[dict[str, Any]] = [
        item for item in (context_overlay_summary.get("top_suppressed_overlays") or []) if isinstance(item, dict)
    ]
    effective_direction_counts = {
        "positive": int(summary_effective_positive) if summary_effective_positive is not None else positive_count,
        "negative": int(summary_effective_negative) if summary_effective_negative is not None else negative_count,
        "watch": int(summary_effective_watch) if summary_effective_watch is not None else watch_count,
    }
    if top_overlays:
        effective_direction_counts = {"positive": 0, "negative": 0, "watch": 0}
        for item in top_overlays:
            direction = str(item.get("direction") or "").strip().lower()
            if direction not in effective_direction_counts:
                continue
            if _context_overlay_reliability_suppressed(item):
                suppressed_overlays.append(item)
                continue
            effective_direction_counts[direction] += 1
    effective_positive_count = int(effective_direction_counts["positive"])
    effective_negative_count = int(effective_direction_counts["negative"])
    effective_watch_count = int(effective_direction_counts["watch"])

    supportive_count = 0
    conflicting_count = 0
    if action_code in entry_actions:
        supportive_count = effective_positive_count + effective_watch_count
        conflicting_count = effective_negative_count
    elif action_code in de_risk_actions:
        supportive_count = effective_negative_count
        conflicting_count = effective_positive_count + effective_watch_count
    elif action_code in hold_actions:
        supportive_count = effective_watch_count
        conflicting_count = effective_positive_count + effective_negative_count
    else:
        conflicting_count = effective_positive_count + effective_negative_count

    if not overlay_count:
        alignment = "no_context_overlay_evidence"
    elif supportive_count and conflicting_count:
        alignment = "mixed_or_conflicting_context"
    elif supportive_count:
        alignment = "context_supports_selected_action"
    elif conflicting_count:
        alignment = "context_conflicts_with_selected_action"
    elif effective_watch_count:
        alignment = "watch_context_only"
    elif suppressed_overlays:
        alignment = "context_observed_reliability_suppressed"
    else:
        alignment = "context_observed_no_direction"

    top_supporting_overlays: list[dict[str, Any]] = []
    top_conflicting_overlays: list[dict[str, Any]] = []
    top_suppressed_overlays: list[dict[str, Any]] = []
    for item in top_overlays:
        direction = str(item.get("direction") or "").strip().lower()
        reliability_suppressed = _context_overlay_reliability_suppressed(item)
        compact = {
            "source": item.get("source"),
            "context_source": item.get("source"),
            "context_overlay_id": item.get("overlay_id"),
            "overlay_id": item.get("overlay_id"),
            "direction": item.get("direction"),
            "class": item.get("class"),
            "reason": item.get("reason"),
            "asof_date": item.get("asof_date"),
            "source_family_reliability": item.get("source_family_reliability"),
            "context_class_reliability": item.get("context_class_reliability"),
            "context_sector_reliability": item.get("context_sector_reliability"),
            "reliability_suppressed": reliability_suppressed,
        }
        if reliability_suppressed:
            top_suppressed_overlays.append(compact)
            continue
        if action_code in entry_actions:
            if direction == "negative":
                top_conflicting_overlays.append(compact)
            elif direction in {"positive", "watch"}:
                top_supporting_overlays.append(compact)
        elif action_code in de_risk_actions:
            if direction == "negative":
                top_supporting_overlays.append(compact)
            elif direction in {"positive", "watch"}:
                top_conflicting_overlays.append(compact)
        elif action_code in hold_actions:
            if direction == "watch":
                top_supporting_overlays.append(compact)
            elif direction in {"positive", "negative"}:
                top_conflicting_overlays.append(compact)
        elif direction in {"positive", "negative"}:
            top_conflicting_overlays.append(compact)

    return {
        "schema_version": 1,
        "action_code": action_code,
        "alignment": alignment,
        "supportive_context_count": int(supportive_count),
        "conflicting_context_count": int(conflicting_count),
        "positive_count": int(positive_count),
        "negative_count": int(negative_count),
        "watch_count": int(watch_count),
        "raw_supportive_context_count": int(
            positive_count + watch_count
            if action_code in entry_actions
            else negative_count
            if action_code in de_risk_actions
            else watch_count
            if action_code in hold_actions
            else 0
        ),
        "raw_conflicting_context_count": int(
            negative_count
            if action_code in entry_actions
            else positive_count + watch_count
            if action_code in de_risk_actions
            else positive_count + negative_count
            if action_code in hold_actions
            else positive_count + negative_count
        ),
        "reliability_suppressed_context_count": int(len(suppressed_overlays)),
        "reliability_filter_policy_effect": "annotation_only_no_ranking_change",
        "source_family_counts": source_counts,
        "interpretation": context_overlay_summary.get("interpretation"),
        "authority_scope": "explain_only",
        "action_policy_effect": "explain_only_no_ranking_change",
        "broker_execution_allowed": False,
        "portfolio_authority": "none",
        "top_supporting_overlays": top_supporting_overlays[:3],
        "top_conflicting_overlays": top_conflicting_overlays[:3],
        "top_suppressed_overlays": top_suppressed_overlays[:3],
    }


def _causal_event_memory_action_alignment(action: str, memory_summary: Any) -> dict[str, Any]:
    if not isinstance(memory_summary, dict) or not memory_summary:
        return {}
    action_code = str(action or "").strip().upper()
    positive_count = int(memory_summary.get("positive_count") or 0)
    negative_count = int(memory_summary.get("negative_count") or 0)
    mixed_count = int(memory_summary.get("mixed_count") or 0)
    contradiction_count = int(memory_summary.get("contradiction_count") or 0)
    memory_count = int(memory_summary.get("memory_count") or memory_summary.get("row_count") or 0)
    entry_actions = {"BUY", "BUY_MORE", "WATCH", "BUY_WATCH"}
    de_risk_actions = {"SELL", "PARTIAL_SELL", "FULL_EXIT", "TIGHTEN_STOP", "REDUCE_EXPOSURE_REVIEW"}
    hold_actions = {"HOLD", "NO_ACTION", "IGNORE"}

    supportive_count = 0
    conflicting_count = 0
    if action_code in entry_actions:
        supportive_count = positive_count
        conflicting_count = negative_count + mixed_count + contradiction_count
    elif action_code in de_risk_actions:
        supportive_count = negative_count
        conflicting_count = positive_count
    elif action_code in hold_actions:
        supportive_count = mixed_count + contradiction_count
        conflicting_count = positive_count + negative_count
    else:
        conflicting_count = positive_count + negative_count + mixed_count + contradiction_count

    if not memory_count:
        alignment = "no_causal_event_memory"
    elif mixed_count or contradiction_count:
        alignment = "mixed_or_contradictory_memory"
    elif supportive_count and conflicting_count:
        alignment = "mixed_or_conflicting_memory"
    elif supportive_count:
        alignment = "memory_supports_selected_action"
    elif conflicting_count:
        alignment = "memory_conflicts_with_selected_action"
    else:
        alignment = "memory_observed_no_direction"

    top_supporting: list[dict[str, Any]] = []
    top_conflicting: list[dict[str, Any]] = []
    top_mixed: list[dict[str, Any]] = []
    for item in memory_summary.get("top_memories") or []:
        if not isinstance(item, dict):
            continue
        direction = str(item.get("direction") or "").strip().lower()
        event_state = str(item.get("event_state") or "").strip().lower()
        contradiction = str(item.get("contradiction_state") or "").strip().lower()
        compact = {
            "memory_id": item.get("memory_id"),
            "source": item.get("source"),
            "event_type": item.get("event_type"),
            "context_class": item.get("context_class"),
            "direction": item.get("direction"),
            "event_state": item.get("event_state"),
            "decayed_pressure_score": item.get("decayed_pressure_score"),
            "freshness_days": item.get("freshness_days"),
            "contradiction_state": item.get("contradiction_state"),
            "policy_effect": item.get("policy_effect") or "memory_only_no_trade_authority",
        }
        if direction == "mixed" or event_state == "mixed_context" or contradiction not in {"", "none", "nan", "<na>"}:
            top_mixed.append(compact)
            continue
        if action_code in entry_actions:
            if direction == "positive" or event_state == "positive_watch_pressure":
                top_supporting.append(compact)
            elif direction == "negative" or event_state == "negative_derisk_pressure":
                top_conflicting.append(compact)
        elif action_code in de_risk_actions:
            if direction == "negative" or event_state == "negative_derisk_pressure":
                top_supporting.append(compact)
            elif direction == "positive" or event_state == "positive_watch_pressure":
                top_conflicting.append(compact)
        elif action_code in hold_actions:
            if direction in {"positive", "negative"}:
                top_conflicting.append(compact)

    return {
        "schema_version": 1,
        "action_code": action_code,
        "alignment": alignment,
        "supportive_memory_count": int(supportive_count),
        "conflicting_memory_count": int(conflicting_count),
        "positive_count": int(positive_count),
        "negative_count": int(negative_count),
        "mixed_count": int(mixed_count),
        "contradiction_count": int(contradiction_count),
        "source_family_counts": memory_summary.get("source_family_counts") if isinstance(memory_summary.get("source_family_counts"), dict) else {},
        "authority_scope": "explain_only",
        "action_policy_effect": "explain_only_no_ranking_change",
        "memory_policy_effect": "memory_context_only_no_trade_authority",
        "broker_execution_allowed": False,
        "portfolio_authority": "none",
        "top_supporting_memories": top_supporting[:3],
        "top_conflicting_memories": top_conflicting[:3],
        "top_mixed_memories": top_mixed[:3],
    }


def _compact_source_family_reliability(row: dict[str, Any]) -> dict[str, Any]:
    contract = row.get("runtime_policy_contract") if isinstance(row.get("runtime_policy_contract"), dict) else None
    compact = {
        "classification": _json_context_value(row.get("classification")),
        "total_selected_count": _json_context_value(row.get("total_selected_count")),
        "total_matured_count": _json_context_value(row.get("total_matured_count")),
        "avg_lift_vs_technical_only": _json_context_value(row.get("avg_lift_vs_technical_only")),
        "avg_forward_return_after_cost": _json_context_value(row.get("avg_forward_return_after_cost")),
        "avg_benchmark_forward_return": _json_context_value(row.get("avg_benchmark_forward_return")),
        "avg_excess_forward_return_after_cost": _json_context_value(row.get("avg_excess_forward_return_after_cost")),
        "avg_hit_rate_after_cost": _json_context_value(row.get("avg_hit_rate_after_cost")),
        "avg_excess_hit_rate_after_cost": _json_context_value(row.get("avg_excess_hit_rate_after_cost")),
        "context_class_diagnostics": _json_context_value(row.get("context_class_diagnostics") or []),
        "context_class_policy_effect": _json_context_value(row.get("context_class_policy_effect")),
        "sector_diagnostics": _json_context_value(row.get("sector_diagnostics") or []),
        "sector_policy_effect": _json_context_value(row.get("sector_policy_effect")),
        "watch_state_diagnostics": _json_context_value(row.get("watch_state_diagnostics") or []),
        "watch_state_policy_effect": _json_context_value(row.get("watch_state_policy_effect")),
        "runtime_policy_contract": _json_context_value(contract or reliability_runtime_policy_contract(row.get("classification"))),
    }
    if contract is None:
        compact["runtime_policy_contract_synthesized"] = True
    return compact


def _compact_fast_context_reliability_report(report: dict[str, Any], *, evidence_source: str) -> dict[str, Any]:
    families = {}
    for row in report.get("families", []):
        if not row.get("source_family"):
            continue
        contract = row.get("runtime_policy_contract") if isinstance(row.get("runtime_policy_contract"), dict) else None
        compact = {
            "classification": _json_context_value(row.get("classification")),
            "total_selected_count": _json_context_value(row.get("total_selected_count")),
            "total_matured_count": _json_context_value(row.get("total_matured_count")),
            "avg_lift_vs_technical_only": None,
            "avg_forward_return_after_cost": _json_context_value(row.get("avg_forward_return_after_cost")),
            "avg_hit_rate_after_cost": _json_context_value(row.get("avg_hit_rate_after_cost")),
            "watch_matured_count": _json_context_value(row.get("watch_matured_count")),
            "negative_pressure_matured_count": _json_context_value(row.get("negative_pressure_matured_count")),
            "context_class_diagnostics": _json_context_value(row.get("context_class_diagnostics") or []),
            "context_class_policy_effect": _json_context_value(row.get("context_class_policy_effect")),
            "sector_diagnostics": _json_context_value(row.get("sector_diagnostics") or []),
            "sector_policy_effect": _json_context_value(row.get("sector_policy_effect")),
            "watch_state_diagnostics": _json_context_value(row.get("watch_state_diagnostics") or []),
            "watch_state_policy_effect": _json_context_value(row.get("watch_state_policy_effect")),
            "runtime_policy_contract": _json_context_value(contract or reliability_runtime_policy_contract(row.get("classification"))),
            "evidence_source": evidence_source,
        }
        if contract is None or row.get("runtime_policy_contract_synthesized") is True:
            compact["runtime_policy_contract_synthesized"] = True
        families[str(row.get("source_family"))] = compact
    return {
        "schema_version": 1,
        "authority_scope": "research_only",
        "action_policy_effect": "annotation_only_no_ranking_change",
        "broker_execution_allowed": False,
        "status": _json_context_value(report.get("status")),
        "evaluated_at": _json_context_value(report.get("evaluated_at")),
        "evidence_source": evidence_source,
        "candidate_helpful_families": [
            str(row.get("source_family"))
            for row in report.get("families", [])
            if row.get("source_family") and row.get("classification") == "candidate_helpful"
        ],
        "families": families,
    }


def _load_full_signal_quality_context_family_reliability(asof_date: pd.Timestamp) -> dict[str, Any]:
    try:
        if not table_exists(SIGNAL_QUALITY_SUMMARY_TABLE):
            return {}
        asof_cutoff, asof_operator = _point_in_time_cutoff(asof_date)
        variants = sorted(VARIANT_TO_SOURCE_FAMILY)
        latest = sql_to_df(
            f"""
            SELECT MAX(evaluated_at) AS evaluated_at
            FROM {SIGNAL_QUALITY_SUMMARY_TABLE}
            WHERE variant = ANY(%(variants)s)
              AND evaluated_at {asof_operator} %(asof_date)s
            """,
            params={"variants": variants, "asof_date": asof_cutoff},
            retries=3,
            statement_timeout_ms=10000,
        )
        if latest.empty or pd.isna(latest.iloc[0].get("evaluated_at")):
            return {}
        evaluated_at = pd.to_datetime(latest.iloc[0]["evaluated_at"], utc=True, errors="coerce")
        if pd.isna(evaluated_at):
            return {}
        rows = sql_to_df(
            f"""
            SELECT
                evaluated_at,
                horizon_days,
                variant,
                sample_count,
                selected_count,
                matured_count,
                selection_rate,
                avg_forward_return_after_cost,
                hit_rate_after_cost,
                positive_return_rate,
                avg_benchmark_forward_return,
                avg_excess_forward_return_after_cost,
                excess_hit_rate_after_cost,
                positive_excess_return_rate,
                baseline_avg_forward_return_after_cost,
                lift_vs_technical_only,
                recommendation,
                sample_start,
                sample_end,
                load_ts
            FROM {SIGNAL_QUALITY_SUMMARY_TABLE}
            WHERE evaluated_at = %(evaluated_at)s
              AND variant = ANY(%(variants)s)
            ORDER BY horizon_days, variant
            """,
            params={"evaluated_at": evaluated_at, "variants": variants},
            retries=3,
            statement_timeout_ms=10000,
        )
        report = build_family_report(rows)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.action_recommender",
            fallback_type="action_recommender_context_family_reliability_load_failed",
            source=SIGNAL_QUALITY_SUMMARY_TABLE,
            severity="warn",
            reason="Action recommender could not load source-family signal-quality evidence for read-only context annotations.",
            error=exc,
            metadata={"asof_date": str(asof_date)},
        )
        return {}
    families = {
        str(row.get("source_family")): _compact_source_family_reliability(row)
        for row in report.get("families", [])
        if row.get("source_family")
    }
    readiness = report.get("promotion_readiness") if isinstance(report.get("promotion_readiness"), dict) else {}
    return {
        "schema_version": 1,
        "authority_scope": "research_only",
        "action_policy_effect": "annotation_only_no_ranking_change",
        "broker_execution_allowed": False,
        "status": _json_context_value(report.get("status")),
        "evaluated_at": _json_context_value(report.get("evaluated_at")),
        "evidence_source": "signal_quality_family_report",
        "promotion_readiness": _json_context_value(readiness),
        "candidate_helpful_families": _json_context_value(readiness.get("candidate_helpful_families") or []),
        "benchmark_or_attribution_blocked_families": _json_context_value(readiness.get("benchmark_or_attribution_blocked_families") or []),
        "families": families,
    }


def _load_context_family_reliability(asof_date: pd.Timestamp) -> dict[str, Any]:
    try:
        persisted = load_persisted_context_reliability_report(asof_date=asof_date)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.action_recommender",
            fallback_type="action_recommender_persisted_context_reliability_load_failed",
            source="advisory_context_overlay_reliability_summary",
            severity="warn",
            reason="Action recommender could not load persisted fast context-overlay reliability evidence; recomputing from evaluator summaries.",
            error=exc,
            metadata={"asof_date": str(asof_date)},
        )
        persisted = {}
    if isinstance(persisted, dict) and persisted.get("families"):
        return _compact_fast_context_reliability_report(
            persisted,
            evidence_source="persisted_context_overlay_reliability",
        )
    try:
        fast = load_fast_context_reliability_report(asof_date=asof_date)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.action_recommender",
            fallback_type="action_recommender_fast_context_reliability_load_failed",
            source="advisory.context_overlay_reliability_report",
            severity="warn",
            reason="Action recommender could not recompute fast context-overlay reliability evidence; falling back to full signal-quality family evidence.",
            error=exc,
            metadata={"asof_date": str(asof_date)},
        )
        fast = {}
    if isinstance(fast, dict) and fast.get("families"):
        return _compact_fast_context_reliability_report(
            fast,
            evidence_source="fast_context_overlay_reliability",
        )
    return _load_full_signal_quality_context_family_reliability(asof_date)


def _compact_signal_quality_overlay_rule(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "overlay": _json_context_value(row.get("overlay")),
        "context_source_family": _json_context_value(row.get("context_source_family")),
        "minimum_horizon_days": _json_context_value(row.get("minimum_horizon_days")),
        "minimum_matured_rows": _json_context_value(row.get("minimum_matured_rows")),
        "minimum_lift_vs_technical_only": _json_context_value(row.get("minimum_lift_vs_technical_only")),
        "minimum_context_source_families": _json_context_value(row.get("minimum_context_source_families")),
        "minimum_context_family_selected_rows": _json_context_value(row.get("minimum_context_family_selected_rows")),
        "minimum_context_family_matured_rows": _json_context_value(row.get("minimum_context_family_matured_rows")),
        "minimum_context_family_symbols": _json_context_value(row.get("minimum_context_family_symbols")),
        "split_axis": _json_context_value(row.get("split_axis")),
        "split_value": _json_context_value(row.get("split_value")),
        "context_class": _json_context_value(row.get("context_class")),
        "direction": _json_context_value(row.get("direction")),
        "stability_classification": _json_context_value(row.get("stability_classification")),
        "minimum_windows": _json_context_value(row.get("minimum_windows")),
        "helpful_window_count": _json_context_value(row.get("helpful_window_count")),
        "harmful_window_count": _json_context_value(row.get("harmful_window_count")),
        "avg_lift_vs_technical_only": _json_context_value(row.get("avg_lift_vs_technical_only")),
        "status": _json_context_value(row.get("status")),
        "valid": bool(row.get("valid")),
        "readable_as_review_input": bool(row.get("readable_as_review_input")),
        "action_policy_effect": _json_context_value(row.get("action_policy_effect")) or "review_input_only_no_live_policy",
        "authority": "review_input_only",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        "usable_for_live_policy": False,
    }


def _context_reliability_classification_for_family(
    reliability_by_family: dict[str, Any],
    source_family: str,
) -> str | None:
    if not isinstance(reliability_by_family, dict):
        return None
    row = reliability_by_family.get(source_family)
    if isinstance(row, dict):
        return _json_context_value(row.get("classification"))
    return None


def _runtime_contract_for_reliability(row: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(row, dict):
        return reliability_runtime_policy_contract(None)
    contract = row.get("runtime_policy_contract")
    if isinstance(contract, dict) and contract:
        return contract
    return reliability_runtime_policy_contract(row.get("classification"))


def _runtime_contract_allows(row: dict[str, Any] | None, use_name: str, *, legacy_classification: str | None = None) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("runtime_policy_contract_synthesized") is True:
        return False
    raw_contract = row.get("runtime_policy_contract")
    if not isinstance(raw_contract, dict) or not raw_contract:
        return False
    contract = _runtime_contract_for_reliability(row)
    allowed = contract.get("allowed_runtime_uses")
    if isinstance(allowed, dict) and use_name in allowed:
        return bool(allowed.get(use_name))
    return False


def _context_class_reliability_for_overlay(
    reliability_by_family: dict[str, Any],
    source_family: Any,
    context_class: Any,
) -> dict[str, Any] | None:
    if not isinstance(reliability_by_family, dict):
        return None
    family = str(source_family or "").strip()
    target_class = str(context_class or "").strip().upper()
    if not family or not target_class or target_class in {"<NA>", "NAN", "NONE"}:
        return None
    reliability_row = reliability_by_family.get(family)
    if not isinstance(reliability_row, dict):
        return None
    for item in reliability_row.get("context_class_diagnostics") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("context_class") or "").strip().upper() != target_class:
            continue
        compact = {
            key: _json_context_value(value)
            for key, value in item.items()
            if _json_context_value(value) is not None
        }
        compact.setdefault("context_class", target_class)
        compact["source_family"] = family
        compact["authority_scope"] = "research_only"
        compact["action_policy_effect"] = "annotation_only_no_ranking_change"
        compact["broker_execution_allowed"] = False
        return compact
    return None


def _context_sector_key(value: Any) -> str:
    if pd.api.types.is_scalar(value) and pd.isna(value):
        return ""
    text = str(value or "").strip()
    if not text or text.upper() in {"<NA>", "NAN", "NONE"}:
        return ""
    return "".join(ch for ch in text.upper() if ch.isalnum())


def _context_sector_reliability_for_overlay(
    reliability_by_family: dict[str, Any],
    source_family: Any,
    sector_name: Any,
    sector_code: Any,
) -> dict[str, Any] | None:
    if not isinstance(reliability_by_family, dict):
        return None
    family = str(source_family or "").strip()
    if not family:
        return None
    target_keys = {
        key
        for key in [
            _context_sector_key(sector_code),
            _context_sector_key(sector_name),
        ]
        if key
    }
    if not target_keys:
        return None
    reliability_row = reliability_by_family.get(family)
    if not isinstance(reliability_row, dict):
        return None
    for item in reliability_row.get("sector_diagnostics") or []:
        if not isinstance(item, dict):
            continue
        row_keys = {
            key
            for key in [
                _context_sector_key(item.get("sector_code")),
                _context_sector_key(item.get("sector_name")),
                _context_sector_key(item.get("sector_key")),
            ]
            if key
        }
        if not target_keys & row_keys:
            continue
        compact = {
            key: _json_context_value(value)
            for key, value in item.items()
            if _json_context_value(value) is not None
        }
        compact["source_family"] = family
        compact["authority_scope"] = "research_only"
        compact["action_policy_effect"] = "annotation_only_no_ranking_change"
        compact["broker_execution_allowed"] = False
        return compact
    return None


def _signal_quality_overlay_rule_context_class(rule: dict[str, Any]) -> str | None:
    split_axis = _context_match_token(rule.get("split_axis"))
    split_tokens = _split_value_tokens(rule.get("split_value"))
    rule_class = _context_match_token(rule.get("context_class"))
    if split_axis == "context_class_direction":
        return split_tokens[0] if split_tokens else rule_class or None
    if split_axis in {"context_class", "event_class", "pressure_class", "macro_signal", "theme", "rule_id"}:
        return split_tokens[0] if split_tokens else rule_class or None
    if not split_axis and rule_class:
        return rule_class
    return None


def _signal_quality_overlay_rule_context_class_reliability(
    reliability_by_family: dict[str, Any],
    source_family: str,
    rule: dict[str, Any],
) -> dict[str, Any] | None:
    context_class = _signal_quality_overlay_rule_context_class(rule)
    if not context_class:
        return None
    return _context_class_reliability_for_overlay(reliability_by_family, source_family, context_class)


def _signal_quality_overlay_rule_supported_effect(rule: dict[str, Any]) -> bool:
    effect = str(rule.get("action_policy_effect") or "").strip().lower()
    return effect in SIGNAL_QUALITY_OVERLAY_NEGATIVE_BLOCK_EFFECTS | SIGNAL_QUALITY_OVERLAY_POSITIVE_WATCH_EFFECTS


def _signal_quality_overlay_rule_supported_split_scope(rule: dict[str, Any]) -> tuple[bool, str | None]:
    split_axis = _context_match_token(rule.get("split_axis"))
    split_value = _context_match_token(rule.get("split_value"))
    if not split_axis:
        return True, None
    if split_axis not in SIGNAL_QUALITY_OVERLAY_SUPPORTED_SPLIT_AXES:
        return False, "unsupported_split_axis"
    if split_axis == "context_class_direction" and split_value and len(_split_value_tokens(split_value)) < 2:
        return False, "invalid_context_class_direction_split_value"
    return True, None


def _annotate_signal_quality_overlay_rules_with_runtime_gate(
    source_family: str,
    rules: list[dict[str, Any]],
    reliability_by_family: dict[str, Any],
) -> list[dict[str, Any]]:
    classification = _context_reliability_classification_for_family(reliability_by_family, source_family)
    family_row = reliability_by_family.get(source_family) if isinstance(reliability_by_family, dict) else None
    runtime_eligible_reliability = (
        classification == "candidate_helpful"
        and _runtime_contract_allows(
            family_row if isinstance(family_row, dict) else None,
            "watch_priority",
            legacy_classification=classification,
        )
    )
    out: list[dict[str, Any]] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        status = str(rule.get("status") or "").strip().lower()
        stability_classification = str(rule.get("stability_classification") or "").strip()
        runtime_eligible_stability = stability_classification == "stable_candidate"
        runtime_eligible_status = status == "trusted_overlay"
        runtime_eligible_effect = _signal_quality_overlay_rule_supported_effect(rule)
        runtime_eligible_scope, scope_reason = _signal_quality_overlay_rule_supported_split_scope(rule)
        class_reliability = _signal_quality_overlay_rule_context_class_reliability(
            reliability_by_family,
            source_family,
            rule,
        )
        class_reliability_classification = (
            str(class_reliability.get("classification") or "").strip()
            if isinstance(class_reliability, dict)
            else None
        )
        runtime_eligible_class_reliability = (
            class_reliability_classification not in SIGNAL_QUALITY_OVERLAY_SUPPRESSED_CONTEXT_CLASS_RELIABILITY
        )
        runtime_eligible = bool(
            runtime_eligible_reliability
            and runtime_eligible_stability
            and runtime_eligible_status
            and runtime_eligible_effect
            and runtime_eligible_scope
            and runtime_eligible_class_reliability
        )
        enriched = dict(rule)
        enriched["runtime_status_gate"] = "trusted_overlay_required"
        enriched["runtime_status"] = status or None
        enriched["runtime_reliability_gate"] = "candidate_helpful_required"
        enriched["runtime_reliability_classification"] = classification
        enriched["runtime_policy_contract_required"] = True
        enriched["runtime_policy_contract_synthesized"] = bool(isinstance(family_row, dict) and family_row.get("runtime_policy_contract_synthesized") is True)
        enriched["runtime_stability_gate"] = "stable_candidate_required"
        enriched["runtime_stability_classification"] = stability_classification or None
        enriched["runtime_supported_effect_gate"] = "supported_review_only_effect_required"
        enriched["runtime_supported_effect"] = bool(runtime_eligible_effect)
        enriched["runtime_supported_split_scope_gate"] = "known_split_axis_or_broad_rule_required"
        enriched["runtime_supported_split_scope"] = bool(runtime_eligible_scope)
        enriched["runtime_context_class_reliability_gate"] = "exact_context_class_not_harmful_required"
        enriched["runtime_context_class_reliability"] = class_reliability
        enriched["runtime_context_class_reliability_classification"] = class_reliability_classification
        enriched["runtime_context_class_reliability_supported"] = bool(runtime_eligible_class_reliability)
        if scope_reason:
            enriched["runtime_split_scope_block_reason"] = scope_reason
        if not runtime_eligible_class_reliability:
            enriched["runtime_context_class_reliability_block_reason"] = "context_class_reliability_suppressed"
        enriched["runtime_eligible_for_signal_quality_overlay_consumer"] = bool(runtime_eligible)
        if not runtime_eligible:
            reasons = []
            if not runtime_eligible_status:
                reasons.append("rule status is not trusted_overlay")
            if not runtime_eligible_reliability:
                reasons.append("current source-family reliability lacks candidate_helpful with explicit runtime watch-priority contract")
            if not runtime_eligible_stability:
                reasons.append("reviewed rule lacks stable_candidate cross-window evidence")
            if not runtime_eligible_effect:
                reasons.append("rule action_policy_effect is not supported by the runtime consumer")
            if not runtime_eligible_scope:
                reasons.append("rule split scope is not supported by the runtime consumer")
            if not runtime_eligible_class_reliability:
                reasons.append("exact context-class reliability is suppressed")
            enriched["runtime_ineligible_reason"] = f"{'; '.join(reasons)}; reviewed config remains annotation/review-only."
        out.append(enriched)
    return out


def _load_signal_quality_overlay_rule_context() -> dict[str, Any]:
    try:
        rules_payload = load_signal_quality_overlay_rules()
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.action_recommender",
            fallback_type="action_recommender_signal_quality_overlay_rules_load_failed",
            source="config/advisory_setups.yaml",
            severity="warn",
            reason="Action recommender could not load signal-quality overlay config rules for read-only context annotations.",
            error=exc,
            metadata={},
        )
        return {}
    rules = rules_payload.get("rules") if isinstance(rules_payload, dict) else []
    if not isinstance(rules, list):
        return {}
    valid_rules = [rule for rule in rules if isinstance(rule, dict) and rule.get("valid")]
    by_family: dict[str, list[dict[str, Any]]] = {}
    aggregate_rules: list[dict[str, Any]] = []
    for rule in valid_rules:
        compact = _compact_signal_quality_overlay_rule(rule)
        source_family = str(rule.get("context_source_family") or "").strip().lower()
        if source_family:
            by_family.setdefault(source_family, []).append(compact)
        else:
            aggregate_rules.append(compact)
    return {
        "schema_version": 1,
        "status": _json_context_value(rules_payload.get("status")) if isinstance(rules_payload, dict) else None,
        "authority_scope": "review_input_only",
        "action_policy_effect": "review_only_annotation_no_ranking_change",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        "usable_for_live_policy": False,
        "valid_rule_count": int(len(valid_rules)),
        "issue_count": int((rules_payload.get("summary") or {}).get("issue_count") or 0) if isinstance(rules_payload, dict) else 0,
        "by_family": by_family,
        "aggregate_rules": aggregate_rules,
    }


def _summarize_action_context_overlays(
    rows: pd.DataFrame,
    *,
    source_family_reliability: dict[str, Any] | None = None,
    signal_quality_overlay_rules: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    if rows.empty:
        return {}
    reliability = source_family_reliability or {}
    rule_context = signal_quality_overlay_rules or {}
    reliability_by_family = reliability.get("families", {}) if isinstance(reliability.get("families"), dict) else {}
    candidate_helpful = set(reliability.get("candidate_helpful_families") or [])
    rules_by_family = rule_context.get("by_family", {}) if isinstance(rule_context.get("by_family"), dict) else {}
    aggregate_rules = rule_context.get("aggregate_rules", []) if isinstance(rule_context.get("aggregate_rules"), list) else []
    frame = rows.copy()
    frame["symbol"] = frame["symbol"].astype("string").str.strip().str.upper()
    frame["context_asof_date"] = pd.to_datetime(frame["context_asof_date"], utc=True, errors="coerce")
    frame["pressure_score"] = pd.to_numeric(frame.get("pressure_score"), errors="coerce")
    frame["direction"] = frame["direction"].astype("string").str.strip().str.lower()
    frame = frame.dropna(subset=["symbol", "context_asof_date"])
    if frame.empty:
        return {}
    out: dict[str, dict[str, Any]] = {}
    max_rows = max(1, int(ACTION_CONTEXT_OVERLAY_MAX_ROWS_PER_SYMBOL))
    for symbol, group in frame.groupby("symbol", sort=True, dropna=False):
        ranked = group.sort_values(["context_asof_date", "pressure_score"], ascending=[False, False], na_position="last")
        directions = ranked["direction"]
        source_counts = ranked["context_source"].dropna().astype(str).value_counts().to_dict()
        used_reliability = {
            source: reliability_by_family[source]
            for source in source_counts
            if source in reliability_by_family
        }
        used_rules = {
            source: _annotate_signal_quality_overlay_rules_with_runtime_gate(
                source,
                rules_by_family[source],
                reliability_by_family,
            )
            for source in source_counts
            if source in rules_by_family
        }
        top_rows = []
        top_suppressed_rows = []
        class_reliability_rows: list[dict[str, Any]] = []
        sector_reliability_rows: list[dict[str, Any]] = []
        effective_counts = {"positive": 0, "negative": 0, "watch": 0}
        for item in ranked.head(max_rows).to_dict(orient="records"):
            source = _json_context_value(item.get("context_source"))
            class_reliability = _context_class_reliability_for_overlay(
                reliability_by_family,
                source,
                item.get("context_class"),
            )
            if class_reliability:
                class_reliability_rows.append(class_reliability)
            sector_reliability = _context_sector_reliability_for_overlay(
                reliability_by_family,
                source,
                item.get("sector_name"),
                item.get("sector_code"),
            )
            if sector_reliability:
                sector_reliability_rows.append(sector_reliability)
            compact = {
                "source": source,
                "overlay_id": _json_context_value(item.get("overlay_id")),
                "direction": _json_context_value(item.get("direction")),
                "pressure_score": _json_context_value(item.get("pressure_score")),
                "class": _json_context_value(item.get("context_class")),
                "sector_name": _json_context_value(item.get("sector_name")),
                "sector_code": _json_context_value(item.get("sector_code")),
                "reason": _json_context_value(item.get("reason")),
                "asof_date": _json_context_value(item.get("context_asof_date")),
                "compact_evidence_contract": _parse_jsonish(item.get("compact_evidence_contract"), None),
                "source_family_reliability": used_reliability.get(str(source)) if source else None,
                "context_class_reliability": class_reliability,
                "context_sector_reliability": sector_reliability,
                "reviewed_config_rules": used_rules.get(str(source)) if source else None,
                "broker_execution_allowed": False,
            }
            if _context_overlay_reliability_suppressed(compact):
                compact["reliability_suppressed"] = True
                top_suppressed_rows.append(compact)
                continue
            direction = str(item.get("direction") or "").strip().lower()
            if direction in effective_counts:
                effective_counts[direction] += 1
            top_rows.append(compact)
        class_reliability_counts: dict[str, int] = {}
        for item in class_reliability_rows:
            classification = str(item.get("classification") or "").strip()
            if classification:
                class_reliability_counts[classification] = class_reliability_counts.get(classification, 0) + 1
        sector_reliability_counts: dict[str, int] = {}
        for item in sector_reliability_rows:
            classification = str(item.get("classification") or "").strip()
            if classification:
                sector_reliability_counts[classification] = sector_reliability_counts.get(classification, 0) + 1
        effective_directions = (
            ["positive"] * int(effective_counts["positive"])
            + ["negative"] * int(effective_counts["negative"])
            + ["watch"] * int(effective_counts["watch"])
        )
        out[str(symbol)] = {
            "schema_version": 1,
            "authority_scope": "watchlist_pressure_only",
            "action_policy_effect": "explain_only_no_ranking_change",
            "source_family_reliability_policy_effect": "annotation_only_no_ranking_change",
            "context_class_reliability_policy_effect": "annotation_only_no_ranking_change",
            "broker_execution_allowed": False,
            "lookback_days": int(ACTION_CONTEXT_OVERLAY_LOOKBACK_DAYS),
            "overlay_count": int(len(ranked)),
            "positive_count": int(directions.eq("positive").sum()),
            "negative_count": int(directions.eq("negative").sum()),
            "watch_count": int(directions.eq("watch").sum()),
            "effective_positive_count": int(effective_counts["positive"]),
            "effective_negative_count": int(effective_counts["negative"]),
            "effective_watch_count": int(effective_counts["watch"]),
            "reliability_suppressed_count": int(len(top_suppressed_rows)),
            "source_family_count": int(len(source_counts)),
            "source_family_counts": source_counts,
            "source_family_reliability_status": _json_context_value(reliability.get("status")) if reliability else None,
            "source_family_reliability_evaluated_at": _json_context_value(reliability.get("evaluated_at")) if reliability else None,
            "source_family_reliability": used_reliability,
            "context_class_reliability": class_reliability_rows,
            "context_class_reliability_counts": class_reliability_counts,
            "context_sector_reliability": sector_reliability_rows,
            "context_sector_reliability_counts": sector_reliability_counts,
            "candidate_helpful_source_families": sorted(source for source in source_counts if source in candidate_helpful),
            "reviewed_config_rule_policy_effect": "review_only_annotation_no_ranking_change",
            "reviewed_config_rule_status": _json_context_value(rule_context.get("status")) if rule_context else None,
            "reviewed_config_rule_count": int(sum(len(value) for value in used_rules.values()) + len(aggregate_rules)),
            "reviewed_config_rules_by_family": used_rules,
            "reviewed_config_aggregate_rules": aggregate_rules[:5],
            "max_pressure_score": None if ranked["pressure_score"].dropna().empty else float(ranked["pressure_score"].abs().max()),
            "interpretation": _context_overlay_interpretation(pd.Series(effective_directions, dtype="object")),
            "top_overlays": top_rows,
            "top_suppressed_overlays": top_suppressed_rows,
        }
    return out


def _load_latest_action_context_overlays(
    asof_date: pd.Timestamp,
    symbols: list[str],
    *,
    progress: Any | None = None,
) -> dict[str, dict[str, Any]]:
    def _progress(stage: str, status: str, *, rows: int | None = None, extra: str | None = None) -> None:
        if callable(progress):
            progress(f"context_enrichment.context_overlays.{stage}", status, rows=rows, extra=extra)

    frames = []
    loaders = [
        (
            "announcement",
            lambda: _load_direct_action_context_overlay_rows(
                table_name=ANNOUNCEMENT_CONTEXT_OVERLAYS_TABLE,
                source_name="announcement_context",
                asof_date=asof_date,
                symbols=symbols,
                date_column="published_on",
                class_column="event_class",
            ),
        ),
        (
            "exchange",
            lambda: _load_direct_action_context_overlay_rows(
                table_name=EXCHANGE_CONTEXT_OVERLAYS_TABLE,
                source_name="exchange_context",
                asof_date=asof_date,
                symbols=symbols,
                date_column="asof_date",
                class_column="event_type",
            ),
        ),
        (
            "bhavcopy",
            lambda: _load_direct_action_context_overlay_rows(
                table_name=BHAVCOPY_CONTEXT_OVERLAYS_TABLE,
                source_name="bhavcopy_context",
                asof_date=asof_date,
                symbols=symbols,
                date_column="asof_date",
                class_column="deal_pressure",
            ),
        ),
        (
            "theme",
            lambda: _load_sector_action_context_overlay_rows(
                table_name=THEME_CONTEXT_OVERLAYS_TABLE,
                source_name="theme_context",
                asof_date=asof_date,
                symbols=symbols,
                class_column="theme_id",
                reason_column="theme_reason",
            ),
        ),
        (
            "macro",
            lambda: _load_sector_action_context_overlay_rows(
                table_name=MACRO_CONTEXT_OVERLAYS_TABLE,
                source_name="macro_context",
                asof_date=asof_date,
                symbols=symbols,
                class_column="macro_signal_id",
                reason_column="trigger_reason",
            ),
        ),
    ]
    for stage, loader in loaders:
        _progress(stage, "start", extra=f"symbols={len(symbols)}")
        frame = loader()
        _progress(stage, "done", rows=len(frame) if isinstance(frame, pd.DataFrame) else 0)
        frames.append(frame)
    frames = [frame for frame in frames if isinstance(frame, pd.DataFrame) and not frame.empty]
    if not frames:
        return {}
    _progress("reliability", "start")
    reliability = _load_context_family_reliability(asof_date)
    _progress("reliability", "done", rows=len(reliability.get("families", {})) if isinstance(reliability, dict) else 0)
    _progress("reviewed_rules", "start")
    signal_quality_overlay_rules = _load_signal_quality_overlay_rule_context()
    _progress("reviewed_rules", "done", rows=int(signal_quality_overlay_rules.get("valid_rule_count") or 0) if isinstance(signal_quality_overlay_rules, dict) else 0)
    _progress("summarize", "start", rows=sum(len(frame) for frame in frames))
    return _summarize_action_context_overlays(
        pd.concat(frames, ignore_index=True, sort=False),
        source_family_reliability=reliability,
        signal_quality_overlay_rules=signal_quality_overlay_rules,
    )


def _summarize_action_causal_event_memory(rows: pd.DataFrame) -> dict[str, dict[str, Any]]:
    if rows.empty:
        return {}
    frame = rows.copy()
    frame["symbol"] = frame["symbol"].astype("string").str.strip().str.upper()
    frame["asof_date"] = pd.to_datetime(frame.get("asof_date"), utc=True, errors="coerce")
    frame["decayed_pressure_score"] = pd.to_numeric(frame.get("decayed_pressure_score"), errors="coerce")
    frame["pressure_score"] = pd.to_numeric(frame.get("pressure_score"), errors="coerce")
    frame["direction"] = frame.get("direction", pd.Series(dtype="object")).astype("string").str.strip().str.lower()
    frame["event_state"] = frame.get("event_state", pd.Series(dtype="object")).astype("string").str.strip().str.lower()
    frame["contradiction_state"] = frame.get("contradiction_state", pd.Series(dtype="object")).astype("string").str.strip().str.lower()
    frame = frame.dropna(subset=["symbol", "asof_date"])
    if frame.empty:
        return {}
    out: dict[str, dict[str, Any]] = {}
    max_rows = max(1, int(ACTION_CAUSAL_EVENT_MEMORY_MAX_ROWS_PER_SYMBOL))
    for symbol, group in frame.groupby("symbol", sort=True, dropna=False):
        ranked = group.sort_values(
            ["asof_date", "decayed_pressure_score", "pressure_score"],
            ascending=[False, False, False],
            na_position="last",
        )
        directions = ranked["direction"]
        states = ranked["event_state"]
        contradictions = ranked["contradiction_state"]
        positive_mask = directions.eq("positive") | states.eq("positive_watch_pressure")
        negative_mask = directions.eq("negative") | states.eq("negative_derisk_pressure")
        mixed_mask = directions.eq("mixed") | states.eq("mixed_context")
        top_memories: list[dict[str, Any]] = []
        for item in ranked.head(max_rows).to_dict(orient="records"):
            top_memories.append(
                {
                    "asof_date": _json_context_value(item.get("asof_date")),
                    "memory_id": _json_context_value(item.get("memory_id")),
                    "source": _json_context_value(item.get("context_source")),
                    "event_group": _json_context_value(item.get("event_group")),
                    "event_type": _json_context_value(item.get("event_type")),
                    "context_class": _json_context_value(item.get("context_class")),
                    "direction": _json_context_value(item.get("direction")),
                    "event_state": _json_context_value(item.get("event_state")),
                    "sector_code": _json_context_value(item.get("sector_code")),
                    "sector_name": _json_context_value(item.get("sector_name")),
                    "decayed_pressure_score": _json_context_value(item.get("decayed_pressure_score")),
                    "pressure_score": _json_context_value(item.get("pressure_score")),
                    "freshness_days": _json_context_value(item.get("freshness_days")),
                    "expected_decay_days": _json_context_value(item.get("expected_decay_days")),
                    "contradiction_state": _json_context_value(item.get("contradiction_state")),
                    "event_count": _json_context_value(item.get("event_count")),
                    "policy_effect": _json_context_value(item.get("policy_effect")) or "memory_only_no_trade_authority",
                }
            )
        source_counts = ranked.get("context_source", pd.Series(dtype="object")).dropna().astype(str).value_counts().to_dict()
        out[str(symbol)] = {
            "schema_version": 1,
            "authority_scope": "explain_only",
            "action_policy_effect": "explain_only_no_ranking_change",
            "memory_policy_effect": "memory_context_only_no_trade_authority",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "lookback_days": int(ACTION_CAUSAL_EVENT_MEMORY_LOOKBACK_DAYS),
            "memory_count": int(len(ranked)),
            "positive_count": int(positive_mask.sum()),
            "negative_count": int(negative_mask.sum()),
            "mixed_count": int(mixed_mask.sum()),
            "contradiction_count": int(contradictions.ne("none").sum()),
            "source_family_counts": source_counts,
            "max_decayed_pressure_score": (
                None
                if ranked["decayed_pressure_score"].dropna().empty
                else float(ranked["decayed_pressure_score"].abs().max())
            ),
            "top_memories": top_memories,
        }
    return out


def _load_latest_action_causal_event_memory(asof_date: pd.Timestamp, symbols: list[str]) -> dict[str, dict[str, Any]]:
    normalized_symbols = sorted({str(value).strip().upper() for value in symbols if str(value or "").strip()})
    if not normalized_symbols or not table_exists(CAUSAL_EVENT_MEMORY_TABLE):
        return {}
    memory_columns = table_columns(CAUSAL_EVENT_MEMORY_TABLE)
    required = {"asof_date", "memory_id", "context_source", "direction", "event_state"}
    if not required.issubset(memory_columns):
        return {}
    has_symbol = "symbol" in memory_columns
    has_sector_code = "sector_code" in memory_columns
    if not has_symbol and not has_sector_code:
        return {}
    asof_cutoff, asof_operator = _point_in_time_cutoff(asof_date)
    from_date = asof_cutoff - pd.Timedelta(days=max(0, int(ACTION_CAUSAL_EVENT_MEMORY_LOOKBACK_DAYS)))
    direct_clause = "UPPER(TRIM(m.symbol)) = ANY(%(symbols)s)" if has_symbol else "FALSE"
    sector_union = ""
    if has_sector_code and table_exists(MARKET_CONTEXT_UNIVERSE_TABLE):
        universe_columns = table_columns(MARKET_CONTEXT_UNIVERSE_TABLE)
        if {"asof_date", "symbol", "sector_code"}.issubset(universe_columns):
            sector_union = f"""
            UNION ALL
            SELECT
                UPPER(TRIM(u.symbol)) AS symbol,
                m.asof_date,
                m.memory_id,
                m.sector_code,
                m.sector_name,
                m.context_source,
                m.event_group,
                m.event_type,
                m.context_class,
                m.direction,
                m.event_state,
                m.pressure_score,
                m.decayed_pressure_score,
                m.event_count,
                m.expected_decay_days,
                m.freshness_days,
                m.contradiction_state,
                m.policy_effect
            FROM {CAUSAL_EVENT_MEMORY_TABLE} m
            JOIN {MARKET_CONTEXT_UNIVERSE_TABLE} u
              ON u.asof_date = m.asof_date
             AND regexp_replace(upper(coalesce(u.sector_code, '')), '[^A-Z0-9]', '', 'g') =
                 regexp_replace(upper(coalesce(m.sector_code, '')), '[^A-Z0-9]', '', 'g')
            WHERE m.asof_date >= %(from_date)s
              AND m.asof_date {asof_operator} %(asof_date)s
              AND m.symbol IS NULL
              AND UPPER(TRIM(u.symbol)) = ANY(%(symbols)s)
              AND COALESCE(m.authority_scope, 'review_input_only') = 'review_input_only'
              AND COALESCE(m.portfolio_authority, 'none') = 'none'
              AND COALESCE(m.broker_execution_allowed, false) = false
            """
    try:
        df = sql_to_df(
            f"""
            WITH memory_rows AS (
                SELECT
                    UPPER(TRIM(m.symbol)) AS symbol,
                    m.asof_date,
                    m.memory_id,
                    m.sector_code,
                    m.sector_name,
                    m.context_source,
                    m.event_group,
                    m.event_type,
                    m.context_class,
                    m.direction,
                    m.event_state,
                    m.pressure_score,
                    m.decayed_pressure_score,
                    m.event_count,
                    m.expected_decay_days,
                    m.freshness_days,
                    m.contradiction_state,
                    m.policy_effect
                FROM {CAUSAL_EVENT_MEMORY_TABLE} m
                WHERE m.asof_date >= %(from_date)s
                  AND m.asof_date {asof_operator} %(asof_date)s
                  AND {direct_clause}
                  AND COALESCE(m.authority_scope, 'review_input_only') = 'review_input_only'
                  AND COALESCE(m.portfolio_authority, 'none') = 'none'
                  AND COALESCE(m.broker_execution_allowed, false) = false
                {sector_union}
            )
            SELECT *
            FROM memory_rows
            ORDER BY symbol, asof_date DESC, decayed_pressure_score DESC NULLS LAST, pressure_score DESC NULLS LAST
            """,
            params={"from_date": from_date, "asof_date": asof_cutoff, "symbols": normalized_symbols},
            retries=3,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.action_recommender",
            fallback_type="action_recommender_causal_event_memory_load_failed",
            source=CAUSAL_EVENT_MEMORY_TABLE,
            severity="warn",
            reason="Action recommender could not load causal event memory for explanation-only action reasoning.",
            error=exc,
            metadata={
                "symbol_count": len(normalized_symbols),
                "lookback_days": int(ACTION_CAUSAL_EVENT_MEMORY_LOOKBACK_DAYS),
            },
        )
        return {}
    if df.empty:
        return {}
    return _summarize_action_causal_event_memory(df)


def enrich_action_candidate_context(df: pd.DataFrame, *, asof_date: pd.Timestamp, progress: Any | None = None) -> pd.DataFrame:
    if df.empty or "symbol" not in df.columns:
        return df
    out = df.copy()
    symbols = out["symbol"].dropna().astype(str).str.upper().drop_duplicates().tolist()
    def _progress(stage: str, status: str, *, rows: int | None = None, extra: str | None = None) -> None:
        if not callable(progress):
            return
        progress(stage, status, rows=rows, extra=extra)

    _progress("context_enrichment.candidate_context", "start", extra=f"symbols={len(symbols)}")
    exact_candidates, symbol_candidates = _load_latest_candidate_context(asof_date, symbols)
    _progress(
        "context_enrichment.candidate_context",
        "done",
        rows=len(exact_candidates) + len(symbol_candidates),
        extra=f"exact={len(exact_candidates)} symbol={len(symbol_candidates)}",
    )
    _progress("context_enrichment.regime_context", "start")
    regime_context = _load_regime_context(asof_date)
    _progress("context_enrichment.regime_context", "done", rows=1 if regime_context else 0)
    _progress("context_enrichment.event_context", "start", extra=f"symbols={len(symbols)}")
    exact_events, symbol_events = _load_latest_event_context(asof_date, symbols)
    _progress(
        "context_enrichment.event_context",
        "done",
        rows=len(exact_events) + len(symbol_events),
        extra=f"exact={len(exact_events)} symbol={len(symbol_events)}",
    )
    _progress("context_enrichment.playbook_context", "start", extra=f"symbols={len(symbols)}")
    symbol_playbooks, source_playbooks = _load_latest_playbook_context(asof_date, symbols)
    _progress(
        "context_enrichment.playbook_context",
        "done",
        rows=len(symbol_playbooks) + len(source_playbooks),
        extra=f"symbol={len(symbol_playbooks)} source={len(source_playbooks)}",
    )
    _progress("context_enrichment.context_overlays", "start", extra=f"symbols={len(symbols)}")
    context_overlays = _load_latest_action_context_overlays(asof_date, symbols, progress=progress)
    _progress("context_enrichment.context_overlays", "done", rows=len(context_overlays))
    _progress("context_enrichment.company_memory", "start", extra=f"symbols={len(symbols)}")
    company_memory_reviews = _load_latest_action_company_memory_reviews(asof_date, symbols)
    _progress("context_enrichment.company_memory", "done", rows=len(company_memory_reviews))
    _progress("context_enrichment.causal_event_memory", "start", extra=f"symbols={len(symbols)}")
    causal_event_memory = _load_latest_action_causal_event_memory(asof_date, symbols)
    _progress("context_enrichment.causal_event_memory", "done", rows=len(causal_event_memory))
    enriched_contexts: list[str] = []
    for _, row in out.iterrows():
        symbol = str(row.get("symbol") or "").upper()
        setup_id = str(row.get("setup_id") or "").upper()
        unique_id = str(row.get("unique_id") or "")
        candidate_context = symbol_candidates.get(symbol, {})
        exact_context = exact_candidates.get((setup_id, symbol), {})
        event_context = symbol_events.get(symbol, {})
        exact_event_context = exact_events.get((setup_id, symbol), {})
        playbook_context = symbol_playbooks.get(symbol, {})
        source_playbook_context = source_playbooks.get(unique_id, {})
        context = _merge_context(
            row.get("raw_context_json"),
            regime_context,
            candidate_context,
            exact_context,
            event_context,
            exact_event_context,
            playbook_context,
            source_playbook_context,
            {"context_overlay_summary": context_overlays.get(symbol, {})},
            {"company_memory_review": company_memory_reviews.get(symbol, {})},
            {"causal_event_memory_summary": causal_event_memory.get(symbol, {})},
            {
                "final_action_source": row.get("action_source"),
                "final_source_action": row.get("source_action"),
            },
        )
        parsed_sources = _parse_jsonish(context.get("context_sources"), [])
        context_sources = set(parsed_sources) if isinstance(parsed_sources, list) else set()
        if regime_context:
            context_sources.add("market_regime")
        if candidate_context or exact_context:
            context_sources.add("advisory_candidates")
        if event_context or exact_event_context:
            context_sources.add("advisory_event_evaluations")
        if playbook_context or source_playbook_context:
            context_sources.add("advisory_playbook_action_plans")
        if context_overlays.get(symbol):
            context_sources.add("advisory_context_overlays")
        if company_memory_reviews.get(symbol):
            context_sources.add("advisory_company_memory_reviews")
        if causal_event_memory.get(symbol):
            context_sources.add("advisory_causal_event_memory")
        if context_sources:
            context["context_sources"] = sorted(context_sources)
        enriched_contexts.append(json.dumps(context, ensure_ascii=False, default=str, sort_keys=True))
    out["raw_context_json"] = enriched_contexts
    return out


def _market_symbol_context(market_context: dict[str, Any], symbol: str) -> dict[str, Any]:
    rows = market_context.get("top_universe") if isinstance(market_context, dict) else []
    if not isinstance(rows, list):
        return {}
    normalized = str(symbol or "").strip().upper()
    for row in rows:
        if isinstance(row, dict) and str(row.get("symbol") or "").strip().upper() == normalized:
            return row
    return {}


def _market_symbol_has_leadership(symbol_context: dict[str, Any]) -> bool:
    if not isinstance(symbol_context, dict) or not symbol_context:
        return False
    trend_leader = str(symbol_context.get("trend_leader_flag") or "").strip().lower() in {"1", "true", "yes", "on"}
    leadership_score = pd.to_numeric(symbol_context.get("technical_leadership_score"), errors="coerce")
    rank_pct = pd.to_numeric(symbol_context.get("rank_pct"), errors="coerce")
    context_rank_score = pd.to_numeric(symbol_context.get("context_rank_score"), errors="coerce")
    rs_benchmark = pd.to_numeric(symbol_context.get("rs_vs_benchmark"), errors="coerce")
    rs_sector = pd.to_numeric(symbol_context.get("rs_vs_sector"), errors="coerce")
    return bool(
        trend_leader
        or (not pd.isna(leadership_score) and float(leadership_score) >= 0.70)
        or (not pd.isna(context_rank_score) and float(context_rank_score) >= 0.80)
        or (not pd.isna(rank_pct) and float(rank_pct) <= 10.0)
        or (
            not pd.isna(rs_benchmark)
            and float(rs_benchmark) > 0.0
            and not pd.isna(rs_sector)
            and float(rs_sector) > 0.0
        )
    )


def _bucket_percent(value: Any, *, low: float, caution: float, high_label: str, caution_label: str, low_label: str) -> str:
    numeric = _num(value)
    if numeric is None:
        return "unknown"
    if numeric < low:
        return low_label
    if numeric < caution:
        return caution_label
    return high_label


def _bucket_score(value: Any, *, caution: float, high: float, low_label: str, caution_label: str, high_label: str) -> str:
    numeric = _num(value)
    if numeric is None:
        return "unknown"
    if numeric >= high:
        return high_label
    if numeric >= caution:
        return caution_label
    return low_label


def _drop_empty_json_values(value: Any) -> Any:
    if isinstance(value, dict):
        out = {
            key: _drop_empty_json_values(item)
            for key, item in value.items()
            if item not in (None, "", [], {})
        }
        return {key: item for key, item in out.items() if item not in (None, "", [], {})}
    if isinstance(value, list):
        return [_drop_empty_json_values(item) for item in value if item not in (None, "", [], {})]
    return value


def _build_multi_context_diagnostics(
    *,
    market_summary: dict[str, Any],
    symbol_context: dict[str, Any],
    context_overlay_alignment: dict[str, Any] | None = None,
    technical_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    market_summary = market_summary if isinstance(market_summary, dict) else {}
    symbol_context = symbol_context if isinstance(symbol_context, dict) else {}
    technical_evidence = technical_evidence if isinstance(technical_evidence, dict) else {}
    context_overlay_alignment = context_overlay_alignment if isinstance(context_overlay_alignment, dict) else {}

    breadth_value = market_summary.get("breadth_trend_alignment_pct")
    risk_off_score = market_summary.get("risk_off_score")
    macro_state = str(market_summary.get("macro_risk_state") or "").strip().upper() or None
    regime_name = str(market_summary.get("regime_name") or "").strip().upper() or None
    leadership = _market_symbol_has_leadership(symbol_context)
    technical_state = str(
        technical_evidence.get("technical_state")
        or technical_evidence.get("candidate_state")
        or technical_evidence.get("current_state")
        or ""
    ).strip().upper()
    # technical_total_score is 0-100; the candidate technical_score column is 0-1 (rule_engine normalizes
    # total/100). Scale the fallback to 0-100 so the >= 70 comparison below is scale-correct. _num may
    # return None (absent) -> keep None so the "constructive vs weak" branch is skipped, not miscalled.
    _total = technical_evidence.get("technical_total_score")
    if _total is not None:
        technical_score = _num(_total)
    else:
        _frac = _num(technical_evidence.get("technical_score"))
        technical_score = to_100(_frac) if _frac is not None else None
    technical_context = "unknown"
    if technical_state in {"BUY_TRIGGERED", "READY", "NEAR_PIVOT", "HOLD", "ADD_ON_PULLBACK"}:
        technical_context = "constructive"
    elif technical_state in {"REJECT", "FULL_EXIT", "EMERGENCY_EXIT", "PARTIAL_EXIT"}:
        technical_context = "adverse_or_exit"
    elif technical_score is not None:
        technical_context = "constructive" if technical_score >= 70 else "weak_or_unconfirmed"

    overlay_alignment = str(context_overlay_alignment.get("alignment") or "no_context_overlay_evidence").strip()
    diagnostics = {
        "policy_effect": "diagnostic_only_no_direct_trade_authority",
        "decision_use": "layered_context_explanation_not_single_regime_gate",
        "global_regime_label": regime_name,
        "breadth_context": {
            "label": _bucket_percent(
                breadth_value,
                low=MARKET_CONTEXT_WEAK_BREADTH_THRESHOLD,
                caution=MARKET_CONTEXT_CAUTION_BREADTH_THRESHOLD,
                high_label="broad_participation_ok",
                caution_label="narrow_or_cautious_breadth",
                low_label="weak_breadth",
            ),
            "breadth_trend_alignment_pct": _num(breadth_value),
        },
        "macro_stress_context": {
            "label": (
                "hard_macro_risk"
                if macro_state in SYSTEMIC_MACRO_HARD_RISK_STATES or regime_name == "SHOCK"
                else _bucket_score(
                    risk_off_score,
                    caution=MARKET_CONTEXT_CAUTION_RISK_OFF_SCORE_THRESHOLD,
                    high=MARKET_CONTEXT_RISK_OFF_SCORE_THRESHOLD,
                    low_label="normal",
                    caution_label="elevated",
                    high_label="risk_off_score_high",
                )
            ),
            "macro_risk_state": macro_state,
            "risk_off_score": _num(risk_off_score),
            "macro_sizing_multiplier": _num(market_summary.get("macro_sizing_multiplier")),
        },
        "sector_symbol_context": {
            "label": "leader_or_resilient" if leadership else ("ranked_no_leadership" if symbol_context else "not_in_top_context_universe"),
            "sector_name": symbol_context.get("sector_name") or symbol_context.get("sector_code"),
            "rank_pct": _num(symbol_context.get("rank_pct")),
            "technical_leadership_score": _num(symbol_context.get("technical_leadership_score")),
            "rs_vs_benchmark": _num(symbol_context.get("rs_vs_benchmark")),
            "rs_vs_sector": _num(symbol_context.get("rs_vs_sector")),
            "trend_leader_flag": bool(str(symbol_context.get("trend_leader_flag") or "").strip().lower() in {"1", "true", "yes", "on"}),
        },
        "event_context_overlay": {
            "label": overlay_alignment,
            "positive_count": context_overlay_alignment.get("positive_count"),
            "negative_count": context_overlay_alignment.get("negative_count"),
            "watch_count": context_overlay_alignment.get("watch_count"),
            "broker_execution_allowed": False,
        },
        "technical_confirmation_context": {
            "label": technical_context,
            "technical_state": technical_state or None,
            "technical_score": technical_score,
            "trigger_type": technical_evidence.get("technical_trigger_type"),
            "setup_archetype": technical_evidence.get("technical_setup_archetype"),
        },
    }
    return _drop_empty_json_values(diagnostics)


def _context_overlay_supports_positive_action(context_overlay_summary: Any) -> bool:
    if not isinstance(context_overlay_summary, dict) or not context_overlay_summary:
        return False
    positive_count = int(context_overlay_summary.get("effective_positive_count", context_overlay_summary.get("positive_count") or 0) or 0)
    negative_count = int(context_overlay_summary.get("effective_negative_count", context_overlay_summary.get("negative_count") or 0) or 0)
    max_pressure = pd.to_numeric(context_overlay_summary.get("max_pressure_score"), errors="coerce")
    if positive_count <= 0 or negative_count > 0:
        return False
    if pd.notna(max_pressure) and float(max_pressure) < 0.35:
        return False
    inspected_positive = False
    for item in context_overlay_summary.get("top_overlays") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("direction") or "").strip().lower() != "positive":
            continue
        inspected_positive = True
        family_reliability = item.get("source_family_reliability") if isinstance(item.get("source_family_reliability"), dict) else {}
        class_reliability = item.get("context_class_reliability") if isinstance(item.get("context_class_reliability"), dict) else {}
        sector_reliability = item.get("context_sector_reliability") if isinstance(item.get("context_sector_reliability"), dict) else {}
        family_classification = str(family_reliability.get("classification") or "").strip()
        class_classification = str(class_reliability.get("classification") or "").strip()
        sector_classification = str(sector_reliability.get("classification") or "").strip()
        if family_reliability and not _runtime_contract_allows(
            family_reliability,
            "watch_priority",
            legacy_classification=family_classification,
        ):
            continue
        if family_classification in SIGNAL_QUALITY_OVERLAY_SUPPRESSED_CONTEXT_CLASS_RELIABILITY:
            continue
        if class_classification in SIGNAL_QUALITY_OVERLAY_SUPPRESSED_CONTEXT_CLASS_RELIABILITY:
            continue
        if sector_classification in SIGNAL_QUALITY_OVERLAY_SUPPRESSED_CONTEXT_CLASS_RELIABILITY:
            continue
        return True
    return positive_count > 0 and not inspected_positive


def _market_context_adjustment(
    action_code: str,
    market_context: dict[str, Any],
    *,
    symbol: str | None = None,
    context_overlay_summary: Any = None,
    technical_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary = market_context.get("summary") if isinstance(market_context, dict) else {}
    if not isinstance(summary, dict):
        summary = {}
    action = str(action_code or "").strip().upper()
    regime_name = str(summary.get("regime_name") or "").strip().upper()
    macro_risk_state = str(summary.get("macro_risk_state") or "").strip().upper()
    breadth = pd.to_numeric(summary.get("breadth_trend_alignment_pct"), errors="coerce")
    risk_off_score = pd.to_numeric(summary.get("risk_off_score"), errors="coerce")
    macro_multiplier = pd.to_numeric(summary.get("macro_sizing_multiplier"), errors="coerce")
    symbol_context = _market_symbol_context(market_context, symbol or "")
    symbol_leadership_override = bool(
        MARKET_CONTEXT_SYMBOL_LEADERSHIP_OVERRIDE_ENABLED
        and _market_symbol_has_leadership(symbol_context)
    )
    context_overlay_positive_override = bool(
        MARKET_CONTEXT_CONTEXT_OVERLAY_OVERRIDE_ENABLED
        and (symbol_leadership_override or MARKET_CONTEXT_CONTEXT_OVERLAY_SOFTENS_ANY_SYMBOL_ENABLED)
        and _context_overlay_supports_positive_action(context_overlay_summary)
    )
    leader_score_risk_off_softening = bool(
        MARKET_CONTEXT_LEADER_SOFTENS_SCORE_RISK_OFF_ENABLED
        and symbol_leadership_override
        and not context_overlay_positive_override
    )
    weak_breadth = bool(not pd.isna(breadth) and float(breadth) < MARKET_CONTEXT_WEAK_BREADTH_THRESHOLD)
    caution_breadth = bool(not pd.isna(breadth) and float(breadth) < MARKET_CONTEXT_CAUTION_BREADTH_THRESHOLD)
    label_risk_off = bool(regime_name in RISK_OFF_STATES or macro_risk_state in RISK_OFF_STATES)
    score_risk_off = bool(not pd.isna(risk_off_score) and float(risk_off_score) >= MARKET_CONTEXT_RISK_OFF_SCORE_THRESHOLD)
    systemic_macro_hard_risk = bool(macro_risk_state in SYSTEMIC_MACRO_HARD_RISK_STATES or regime_name == "SHOCK")
    high_macro_risk_hard_block = bool(
        MARKET_CONTEXT_HIGH_MACRO_RISK_HARD_BLOCK_ENABLED
        and macro_risk_state in RISK_OFF_STATES
        and not systemic_macro_hard_risk
    )
    macro_hard_risk = bool(systemic_macro_hard_risk or high_macro_risk_hard_block)
    score_risk_off_hard_block = bool(
        MARKET_CONTEXT_SCORE_RISK_OFF_HARD_BLOCK_ENABLED
        and score_risk_off
        and not context_overlay_positive_override
        and not leader_score_risk_off_softening
    )
    risk_off = bool(
        score_risk_off_hard_block
        or macro_hard_risk
        or (MARKET_CONTEXT_REGIME_LABEL_HARD_BLOCK_ENABLED and label_risk_off)
    )
    risk_off_block_reason = None
    if macro_hard_risk:
        risk_off_block_reason = "hard_macro_risk"
    elif score_risk_off_hard_block:
        risk_off_block_reason = "score_risk_off_hard_block_enabled"
    elif MARKET_CONTEXT_REGIME_LABEL_HARD_BLOCK_ENABLED and label_risk_off:
        risk_off_block_reason = "regime_label_hard_block_enabled"
    elif MARKET_CONTEXT_WEAK_BREADTH_HARD_BLOCK_ENABLED and weak_breadth:
        risk_off_block_reason = "weak_breadth_hard_block_enabled"
    caution = (
        not risk_off
        and (
            label_risk_off
            or caution_breadth
            or bool(not pd.isna(risk_off_score) and float(risk_off_score) >= MARKET_CONTEXT_CAUTION_RISK_OFF_SCORE_THRESHOLD)
            or bool(not pd.isna(macro_multiplier) and float(macro_multiplier) < 1.0)
        )
    )
    adjusted_action = action
    adjustment = "none"
    reason = "Market context did not change the action boundary."
    size_multiplier = 1.0
    weak_breadth_block = bool(MARKET_CONTEXT_WEAK_BREADTH_HARD_BLOCK_ENABLED and weak_breadth and not symbol_leadership_override)
    context_overlay_alignment = _context_overlay_action_alignment(action, context_overlay_summary)
    multi_context_diagnostics = _build_multi_context_diagnostics(
        market_summary=summary,
        symbol_context=symbol_context,
        context_overlay_alignment=context_overlay_alignment,
        technical_evidence=technical_evidence,
    )
    if action in POSITIVE_BROKER_ACTIONS and (risk_off or weak_breadth_block):
        adjusted_action = "MANUAL_REVIEW"
        adjustment = "positive_action_blocked_by_market_context"
        size_multiplier = 0.0
        reason = (
            "Positive broker action was blocked because broad market context is weak or risk-off "
            f"(regime={regime_name or 'n/a'}, macro_risk={macro_risk_state or 'n/a'}, "
            f"trend_breadth={None if pd.isna(breadth) else round(float(breadth), 2)}%, "
            f"risk_off_score={None if pd.isna(risk_off_score) else round(float(risk_off_score), 3)})."
        )
    elif action in POSITIVE_BROKER_ACTIONS and (caution or (weak_breadth and symbol_leadership_override)):
        adjustment = "positive_action_size_reduced_by_market_context"
        multiplier_candidates = [MARKET_CONTEXT_CAUTION_SIZE_MULTIPLIER]
        if not pd.isna(macro_multiplier):
            multiplier_candidates.append(float(macro_multiplier))
        size_multiplier = max(0.0, min(1.0, min(multiplier_candidates)))
        reason = (
            "Positive broker action size was reduced because broad market context is cautious "
            f"(regime={regime_name or 'n/a'}, macro_risk={macro_risk_state or 'n/a'}, "
            f"trend_breadth={None if pd.isna(breadth) else round(float(breadth), 2)}%, "
            f"risk_off_score={None if pd.isna(risk_off_score) else round(float(risk_off_score), 3)}, "
            f"symbol_leadership_override={symbol_leadership_override}, "
            f"context_overlay_positive_override={context_overlay_positive_override}, "
            f"leader_score_risk_off_softening={leader_score_risk_off_softening}, "
            f"size_multiplier={round(size_multiplier, 3)})."
        )
    return {
        "adjustment": adjustment,
        "original_action_code": action,
        "adjusted_action_code": adjusted_action,
        "reason": reason,
        "size_multiplier": size_multiplier,
        "risk_off_block_reason": risk_off_block_reason,
        "regime_name": regime_name or None,
        "macro_risk_state": macro_risk_state or None,
        "label_risk_off": label_risk_off,
        "score_risk_off": score_risk_off,
        "macro_hard_risk": macro_hard_risk,
        "systemic_macro_hard_risk": systemic_macro_hard_risk,
        "high_macro_risk_hard_block": high_macro_risk_hard_block,
        "high_macro_risk_hard_block_enabled": MARKET_CONTEXT_HIGH_MACRO_RISK_HARD_BLOCK_ENABLED,
        "score_risk_off_hard_block": score_risk_off_hard_block,
        "score_risk_off_hard_block_enabled": MARKET_CONTEXT_SCORE_RISK_OFF_HARD_BLOCK_ENABLED,
        "regime_label_hard_block_enabled": MARKET_CONTEXT_REGIME_LABEL_HARD_BLOCK_ENABLED,
        "weak_breadth_hard_block_enabled": MARKET_CONTEXT_WEAK_BREADTH_HARD_BLOCK_ENABLED,
        "breadth_trend_alignment_pct": None if pd.isna(breadth) else float(breadth),
        "risk_off_score": None if pd.isna(risk_off_score) else float(risk_off_score),
        "macro_sizing_multiplier": None if pd.isna(macro_multiplier) else float(macro_multiplier),
        "weak_breadth": weak_breadth,
        "weak_breadth_block": weak_breadth_block,
        "symbol_leadership_override": symbol_leadership_override,
        "symbol_leadership_override_enabled": MARKET_CONTEXT_SYMBOL_LEADERSHIP_OVERRIDE_ENABLED,
        "leader_score_risk_off_softening": leader_score_risk_off_softening,
        "leader_score_risk_off_softening_enabled": MARKET_CONTEXT_LEADER_SOFTENS_SCORE_RISK_OFF_ENABLED,
        "symbol_context": symbol_context,
        "context_overlay_positive_override": context_overlay_positive_override,
        "context_overlay_override_enabled": MARKET_CONTEXT_CONTEXT_OVERLAY_OVERRIDE_ENABLED,
        "context_overlay_softens_any_symbol_enabled": MARKET_CONTEXT_CONTEXT_OVERLAY_SOFTENS_ANY_SYMBOL_ENABLED,
        "context_overlay_support": context_overlay_alignment,
        "multi_context_diagnostics": multi_context_diagnostics,
    }


def apply_market_context_adjustments(
    df: pd.DataFrame,
    *,
    asof_date: pd.Timestamp,
    annotate_only: bool = False,
) -> pd.DataFrame:
    if df.empty or "action_code" not in df.columns:
        return df
    market_context = load_latest_market_context(asof_date, limit=500)
    summary = market_context.get("summary") if isinstance(market_context, dict) else {}
    if not isinstance(summary, dict) or not summary:
        return df
    out = df.copy()
    for idx, row in out.iterrows():
        action = str(row.get("action_code") or "").strip().upper()
        row_context = _parse_jsonish(row.get("raw_context_json"), {})
        if not isinstance(row_context, dict):
            row_context = {}
        adjustment = _market_context_adjustment(
            action,
            market_context,
            symbol=str(row.get("symbol") or ""),
            context_overlay_summary=row_context.get("context_overlay_summary"),
            technical_evidence=row_context,
        )
        raw_context = _merge_context(
            row.get("raw_context_json"),
            {
                "market_context_adjustment": adjustment["adjustment"],
                "market_context_adjustment_json": adjustment,
                "market_context_json": {
                    "summary": summary,
                    "symbol_context": adjustment.get("symbol_context") or {},
                },
            },
        )
        out.at[idx, "raw_context_json"] = json.dumps(raw_context, ensure_ascii=False, default=str, sort_keys=True)
        if annotate_only or action not in POSITIVE_BROKER_ACTIONS or adjustment["adjustment"] == "none":
            continue
        original_reason = _text(row.get("action_reason")) or "No original action reason supplied."
        out.at[idx, "action_detail"] = (
            f"{_text(row.get('action_detail')) or ''} Market context adjustment: {adjustment['reason']}"
        ).strip()
        if adjustment["adjustment"] == "positive_action_blocked_by_market_context":
            adjusted_action = str(adjustment["adjusted_action_code"])
            out.at[idx, "action_code"] = adjusted_action
            out.at[idx, "action_priority"] = int(ACTION_PRIORITY.get(adjusted_action, ACTION_PRIORITY["MANUAL_REVIEW"]))
            out.at[idx, "transaction_type"] = None
            out.at[idx, "execution_mode"] = "review_only"
            out.at[idx, "approved_allocation_inr"] = 0.0
            out.at[idx, "action_fraction"] = None
            out.at[idx, "action_reason"] = f"{adjustment['reason']} Original reason: {original_reason}"
        else:
            size_multiplier = float(adjustment.get("size_multiplier") or 1.0)
            for column in ["approved_allocation_inr", "action_fraction", "invest_score_pct"]:
                value = pd.to_numeric(out.at[idx, column], errors="coerce") if column in out.columns else pd.NA
                if pd.notna(value):
                    out.at[idx, column] = float(value) * size_multiplier
            out.at[idx, "action_reason"] = f"{original_reason} Market context reduced sizing: {adjustment['reason']}"
    return out


def _trusted_signal_quality_overlay_rules_by_family(context_overlay_summary: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(context_overlay_summary, dict) or not context_overlay_summary:
        return {}
    rules_by_family = context_overlay_summary.get("reviewed_config_rules_by_family")
    if not isinstance(rules_by_family, dict):
        return {}
    reliability_by_family = context_overlay_summary.get("source_family_reliability")
    if not isinstance(reliability_by_family, dict):
        reliability_by_family = {}
    trusted: dict[str, list[dict[str, Any]]] = {}
    for family, raw_rules in rules_by_family.items():
        source_family = str(family or "").strip().lower()
        if not source_family or not isinstance(raw_rules, list):
            continue
        classification = _context_reliability_classification_for_family(reliability_by_family, source_family)
        if classification != "candidate_helpful":
            continue
        family_row = reliability_by_family.get(source_family) if isinstance(reliability_by_family, dict) else None
        if isinstance(family_row, dict) and not _runtime_contract_allows(
            family_row,
            "watch_priority",
            legacy_classification=classification,
        ):
            continue
        for raw_rule in raw_rules:
            if not isinstance(raw_rule, dict):
                continue
            stability_classification = str(raw_rule.get("stability_classification") or raw_rule.get("runtime_stability_classification") or "").strip()
            if stability_classification != "stable_candidate":
                continue
            if raw_rule.get("runtime_eligible_for_signal_quality_overlay_consumer") is False:
                continue
            if raw_rule.get("runtime_context_class_reliability_supported") is False:
                continue
            class_reliability = _signal_quality_overlay_rule_context_class_reliability(
                reliability_by_family,
                source_family,
                raw_rule,
            )
            class_reliability_classification = (
                str(class_reliability.get("classification") or "").strip()
                if isinstance(class_reliability, dict)
                else None
            )
            if class_reliability_classification in SIGNAL_QUALITY_OVERLAY_SUPPRESSED_CONTEXT_CLASS_RELIABILITY:
                continue
            status = str(raw_rule.get("status") or "").strip().lower()
            effect = str(raw_rule.get("action_policy_effect") or "").strip().lower()
            if status != "trusted_overlay":
                continue
            if raw_rule.get("readable_as_review_input") is False:
                continue
            if raw_rule.get("broker_execution_allowed") is True or raw_rule.get("policy_auto_promotion_allowed") is True:
                continue
            if raw_rule.get("usable_for_live_policy") is True:
                continue
            if effect not in SIGNAL_QUALITY_OVERLAY_NEGATIVE_BLOCK_EFFECTS | SIGNAL_QUALITY_OVERLAY_POSITIVE_WATCH_EFFECTS:
                continue
            trusted.setdefault(source_family, []).append(raw_rule)
    return trusted


def _context_match_token(value: Any) -> str:
    text = _text(value)
    return text.strip().lower() if text else ""


def _split_value_tokens(value: Any) -> list[str]:
    text = _context_match_token(value)
    if not text:
        return []
    return [part.strip() for part in text.split("|")]


def _signal_quality_overlay_rule_matches_overlay(rule: dict[str, Any], overlay: dict[str, Any]) -> bool:
    """Keep reviewed split rules scoped to the exact context row they were validated on."""

    overlay_direction = _context_match_token(overlay.get("direction"))
    overlay_class = _context_match_token(overlay.get("class") or overlay.get("context_class"))
    rule_direction = _context_match_token(rule.get("direction"))
    rule_class = _context_match_token(rule.get("context_class"))
    split_axis = _context_match_token(rule.get("split_axis"))
    split_tokens = _split_value_tokens(rule.get("split_value"))

    if rule_direction and rule_direction != overlay_direction:
        return False
    if rule_class and rule_class != overlay_class:
        return False

    if split_axis == "context_class_direction":
        if len(split_tokens) >= 1 and split_tokens[0] and split_tokens[0] != overlay_class:
            return False
        if len(split_tokens) >= 2 and split_tokens[1] and split_tokens[1] != overlay_direction:
            return False
        if not overlay_class or not overlay_direction:
            return False
    elif split_axis in {"context_class", "event_class", "pressure_class", "macro_signal", "theme", "rule_id"}:
        expected = split_tokens[0] if split_tokens else rule_class
        if expected and expected != overlay_class:
            return False
        if expected and not overlay_class:
            return False
    elif split_axis == "direction":
        expected = split_tokens[0] if split_tokens else rule_direction
        if expected and expected != overlay_direction:
            return False
        if expected and not overlay_direction:
            return False
    elif split_axis:
        # Unknown split axes are not safe to apply at runtime because we cannot prove row-level scope.
        return False
    elif split_tokens:
        if len(split_tokens) >= 2:
            if split_tokens[0] and split_tokens[0] != overlay_class:
                return False
            if split_tokens[1] and split_tokens[1] != overlay_direction:
                return False
            if not overlay_class or not overlay_direction:
                return False
        elif split_tokens[0] not in {overlay_class, overlay_direction}:
            return False

    return True


def _trusted_signal_quality_overlay_adjustment(action_code: str, raw_context: dict[str, Any]) -> dict[str, Any]:
    action = str(action_code or "").strip().upper()
    summary = raw_context.get("context_overlay_summary") if isinstance(raw_context, dict) else {}
    if not isinstance(summary, dict) or not summary:
        return {"adjustment": "none", "reason": "No context-overlay summary is available."}
    trusted_rules = _trusted_signal_quality_overlay_rules_by_family(summary)
    if not trusted_rules:
        return {"adjustment": "none", "reason": "No trusted signal-quality overlay rules matched the action context."}
    top_overlays = summary.get("top_overlays") if isinstance(summary.get("top_overlays"), list) else []
    negative_matches: list[dict[str, Any]] = []
    positive_matches: list[dict[str, Any]] = []
    for overlay in top_overlays:
        if not isinstance(overlay, dict):
            continue
        source_family = str(overlay.get("source") or "").strip().lower()
        direction = str(overlay.get("direction") or "").strip().lower()
        rules = trusted_rules.get(source_family) or []
        if not rules:
            continue
        for rule in rules:
            if not _signal_quality_overlay_rule_matches_overlay(rule, overlay):
                continue
            effect = str(rule.get("action_policy_effect") or "").strip().lower()
            match = {
                "source_family": source_family,
                "direction": direction,
                "context_class": overlay.get("class") or overlay.get("context_class"),
                "split_axis": rule.get("split_axis"),
                "split_value": rule.get("split_value"),
                "effect": effect,
                "rule_id": rule.get("rule_id"),
                "overlay": rule.get("overlay"),
                "reason": overlay.get("reason"),
            }
            if direction == "negative" and effect in SIGNAL_QUALITY_OVERLAY_NEGATIVE_BLOCK_EFFECTS:
                negative_matches.append(match)
            elif direction == "positive" and effect in SIGNAL_QUALITY_OVERLAY_POSITIVE_WATCH_EFFECTS:
                positive_matches.append(match)
    if action in POSITIVE_BROKER_ACTIONS and negative_matches:
        return {
            "adjustment": "trusted_negative_context_blocks_positive_to_watch",
            "original_action_code": action,
            "adjusted_action_code": "WATCH",
            "size_multiplier": 0.0,
            "broker_execution_allowed": False,
            "portfolio_authority": "none",
            "authority_scope": "review_input_only",
            "matched_rules": negative_matches[:5],
            "reason": (
                "A trusted, review-only signal-quality overlay rule found negative context pressure for this symbol; "
                "the positive broker-capable action was reduced to WATCH instead of being executed."
            ),
        }
    if action == "WATCH" and positive_matches:
        return {
            "adjustment": "trusted_positive_context_boosts_watch_priority",
            "original_action_code": action,
            "adjusted_action_code": "WATCH",
            "priority_boost": 5,
            "broker_execution_allowed": False,
            "portfolio_authority": "none",
            "authority_scope": "review_input_only",
            "matched_rules": positive_matches[:5],
            "reason": (
                "A trusted, review-only signal-quality overlay rule found positive context pressure; "
                "the row remains WATCH but receives a small ranking boost among non-broker watch rows."
            ),
        }
    return {"adjustment": "none", "reason": "Trusted signal-quality overlay rules did not apply to this action."}


def apply_signal_quality_overlay_rule_adjustments(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "action_code" not in df.columns:
        return df
    if not ACTION_SIGNAL_QUALITY_OVERLAY_RULE_CONSUMER_ENABLED:
        return df
    out = df.copy()
    changed_count = 0
    for idx, row in out.iterrows():
        raw_context = _parse_jsonish(row.get("raw_context_json"), {})
        if not isinstance(raw_context, dict):
            raw_context = {}
        action = str(row.get("action_code") or "").strip().upper()
        adjustment = _trusted_signal_quality_overlay_adjustment(action, raw_context)
        if adjustment.get("adjustment") == "none":
            continue
        raw_context = _merge_context(
            raw_context,
            {
                "signal_quality_overlay_rule_adjustment": adjustment.get("adjustment"),
                "signal_quality_overlay_rule_adjustment_json": adjustment,
            },
        )
        out.at[idx, "raw_context_json"] = json.dumps(raw_context, ensure_ascii=False, default=str, sort_keys=True)
        original_reason = _text(row.get("action_reason")) or "No original action reason supplied."
        out.at[idx, "action_detail"] = (
            f"{_text(row.get('action_detail')) or ''} Signal-quality overlay rule adjustment: {adjustment['reason']}"
        ).strip()
        if adjustment["adjustment"] == "trusted_negative_context_blocks_positive_to_watch":
            out.at[idx, "action_code"] = "WATCH"
            out.at[idx, "action_priority"] = int(ACTION_PRIORITY["WATCH"])
            out.at[idx, "transaction_type"] = None
            out.at[idx, "execution_mode"] = "review_only"
            out.at[idx, "approved_allocation_inr"] = 0.0
            out.at[idx, "action_fraction"] = None
            out.at[idx, "action_reason"] = f"{adjustment['reason']} Original reason: {original_reason}"
            changed_count += 1
        elif adjustment["adjustment"] == "trusted_positive_context_boosts_watch_priority":
            current_priority = pd.to_numeric(row.get("action_priority"), errors="coerce")
            base_priority = int(ACTION_PRIORITY["WATCH"] if pd.isna(current_priority) else current_priority)
            out.at[idx, "action_priority"] = base_priority + int(adjustment.get("priority_boost") or 0)
            out.at[idx, "execution_mode"] = "review_only"
            out.at[idx, "action_reason"] = f"{original_reason} {adjustment['reason']}"
            changed_count += 1
    out.attrs["signal_quality_overlay_rule_changed_count"] = changed_count
    return out


def load_enabled_conflict_rule_ids() -> set[str]:
    try:
        exists = table_exists(ACTION_CONFLICT_RULES_TABLE)
    except Exception as exc:
        logger.warning(
            "conflict rule table lookup failed; using default deterministic rules table=%s error=%s: %s",
            ACTION_CONFLICT_RULES_TABLE,
            type(exc).__name__,
            exc,
        )
        record_local_fallback_event(
            module="advisory.action_recommender",
            fallback_type="action_conflict_rule_lookup_failed_default_rules",
            source=ACTION_CONFLICT_RULES_TABLE,
            severity="warn",
            reason="Action consolidation used default deterministic conflict rules because conflict-rule table lookup failed.",
            error=exc,
            metadata={"fallback_rule_count": len(DEFAULT_CONFLICT_RULE_IDS)},
        )
        return set(DEFAULT_CONFLICT_RULE_IDS)
    if not exists:
        logger.warning(
            "conflict rule table missing; using default deterministic rules table=%s",
            ACTION_CONFLICT_RULES_TABLE,
        )
        return set(DEFAULT_CONFLICT_RULE_IDS)
    try:
        df = sql_to_df(
            f"""
            SELECT rule_id
            FROM {ACTION_CONFLICT_RULES_TABLE}
            WHERE COALESCE(enabled, TRUE)
            """,
            retries=2,
        )
    except Exception as exc:
        logger.warning(
            "conflict rule load failed; using default deterministic rules table=%s error=%s: %s",
            ACTION_CONFLICT_RULES_TABLE,
            type(exc).__name__,
            exc,
        )
        record_local_fallback_event(
            module="advisory.action_recommender",
            fallback_type="action_conflict_rule_load_failed_default_rules",
            source=ACTION_CONFLICT_RULES_TABLE,
            severity="warn",
            reason="Action consolidation used default deterministic conflict rules because enabled conflict-rule load failed.",
            error=exc,
            metadata={"fallback_rule_count": len(DEFAULT_CONFLICT_RULE_IDS)},
        )
        return set(DEFAULT_CONFLICT_RULE_IDS)
    return {str(value) for value in df["rule_id"].dropna().tolist()} if not df.empty else set()


def _norm_conflict_value(value: Any, *, upper: bool = True) -> str:
    text = "" if value is None else str(value).strip()
    return text.upper() if upper else text.lower()


def load_enabled_dynamic_conflict_rules_for_ranking() -> list[dict[str, Any]]:
    try:
        exists = table_exists(ACTION_CONFLICT_RULES_TABLE)
    except Exception as exc:
        logger.warning(
            "dynamic conflict rule table lookup failed; promoted conflict rules will not affect ranking table=%s error=%s: %s",
            ACTION_CONFLICT_RULES_TABLE,
            type(exc).__name__,
            exc,
        )
        record_local_fallback_event(
            module="advisory.action_recommender",
            fallback_type="action_dynamic_conflict_rule_lookup_failed",
            source=ACTION_CONFLICT_RULES_TABLE,
            severity="warn",
            reason="Action consolidation ignored promoted dynamic conflict rules because conflict-rule table lookup failed.",
            error=exc,
        )
        return []
    if not exists:
        return []
    try:
        df = sql_to_df(
            f"""
            SELECT *
            FROM {ACTION_CONFLICT_RULES_TABLE}
            WHERE COALESCE(enabled, TRUE)
              AND condition_json IS NOT NULL
            ORDER BY priority DESC, rule_id
            """,
            retries=2,
        )
    except Exception as exc:
        logger.warning(
            "dynamic conflict rule load failed; promoted conflict rules will not affect ranking table=%s error=%s: %s",
            ACTION_CONFLICT_RULES_TABLE,
            type(exc).__name__,
            exc,
        )
        record_local_fallback_event(
            module="advisory.action_recommender",
            fallback_type="action_dynamic_conflict_rule_load_failed",
            source=ACTION_CONFLICT_RULES_TABLE,
            severity="warn",
            reason="Action consolidation ignored promoted dynamic conflict rules because dynamic conflict-rule load failed.",
            error=exc,
        )
        return []
    return df.to_dict(orient="records") if not df.empty else []


def _dynamic_conflict_rule_matches_candidate(row: pd.Series, peer: pd.Series, rule: dict[str, Any]) -> bool:
    condition = _parse_jsonish(rule.get("condition_json"), {})
    if not isinstance(condition, dict):
        return False
    condition_type = str(condition.get("condition_type") or "action_pair_exact").strip().lower()
    if condition_type not in {"action_pair", "action_pair_exact"}:
        return False
    if str(rule.get("resolution_action") or "keep_winner").strip() != "keep_winner":
        return False
    checks = [
        ("winning_action_code", row.get("action_code"), True),
        ("losing_action_code", peer.get("action_code"), True),
    ]
    if condition_type == "action_pair_exact":
        checks.extend(
            [
                ("winning_source", row.get("action_source"), False),
                ("losing_source", peer.get("action_source"), False),
            ]
        )
    for field, actual, upper in checks:
        expected = condition.get(field)
        if expected in (None, ""):
            continue
        if _norm_conflict_value(actual, upper=upper) != _norm_conflict_value(expected, upper=upper):
            return False
    return True


def dynamic_conflict_precedence_for_row(row: pd.Series, peers: pd.DataFrame, dynamic_rules: list[dict[str, Any]]) -> dict[str, Any]:
    for rule in dynamic_rules:
        for _, peer in peers.iterrows():
            if peer.name == row.name:
                continue
            if not _dynamic_conflict_rule_matches_candidate(row, peer, rule):
                continue
            priority = pd.to_numeric(rule.get("priority"), errors="coerce")
            priority_score = 0 if pd.isna(priority) else int(priority)
            return {
                "score": 200 + priority_score,
                "rule_id": str(rule.get("rule_id") or ""),
                "reason": str(rule.get("resolution_reason") or "Matched operator-promoted exact conflict rule."),
            }
    return {"score": 0, "rule_id": None, "reason": None}


def conflict_precedence_for_row(
    row: pd.Series,
    enabled_rules: set[str],
    *,
    peers: pd.DataFrame | None = None,
    dynamic_rules: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    action = str(row.get("action_code") or "").strip().upper()
    raw_context = _parse_jsonish(row.get("raw_context_json"), {})
    if not isinstance(raw_context, dict):
        raw_context = {}
    context_text = json.dumps(raw_context, ensure_ascii=False, default=str).lower()
    dynamic_result = {"score": 0, "rule_id": None, "reason": None}
    if dynamic_rules and peers is not None and not peers.empty:
        dynamic_result = dynamic_conflict_precedence_for_row(row, peers, dynamic_rules)
    if "EXIT_BEATS_ENTRY_OR_WATCH" in enabled_rules and action in {"SELL", "PARTIAL_SELL", "TIGHTEN_STOP"}:
        return {
            "score": 400,
            "rule_id": "EXIT_BEATS_ENTRY_OR_WATCH",
            "reason": "Risk-management actions are selected before entry/watch/manual signals.",
        }
    if "EXIT_BEATS_ENTRY_OR_WATCH" in enabled_rules and action == "REDUCE_EXPOSURE_REVIEW":
        return {
            "score": 380,
            "rule_id": "EXIT_BEATS_ENTRY_OR_WATCH",
            "reason": "Review-only de-risk pressure is selected before entry/watch signals but remains non-executable.",
        }
    if (
        "ADVERSARIAL_VETO_MANUAL_BEATS_POSITIVE_OR_WATCH" in enabled_rules
        and action == "MANUAL_REVIEW"
        and (
            raw_context.get("veto") is True
            or str(raw_context.get("review_action") or "").strip().lower() == "veto"
            or str(raw_context.get("action_status") or "").strip().lower() == "blocked_by_adversarial_review"
            or "adversarial review veto" in context_text
            or "blocked_by_adversarial_review" in context_text
        )
    ):
        return {
            "score": 350,
            "rule_id": "ADVERSARIAL_VETO_MANUAL_BEATS_POSITIVE_OR_WATCH",
            "reason": "Adversarial-review veto keeps the symbol in manual review ahead of buy/watch candidates.",
        }
    if (
        "MARKET_GATE_MANUAL_BEATS_POSITIVE" in enabled_rules
        and action == "MANUAL_REVIEW"
        and ("market_context_adjustment" in context_text or "risk-off" in context_text or "risk_off" in context_text)
    ):
        return {
            "score": 300,
            "rule_id": "MARKET_GATE_MANUAL_BEATS_POSITIVE",
            "reason": "Risk-off market context keeps positive actions in manual review.",
        }
    if (
        "EVENT_POLICY_REVIEW_BEATS_POSITIVE_ENTRY" in enabled_rules
        and action == "MANUAL_REVIEW"
        and str(row.get("action_source") or "").strip().lower() == "event_policy"
        and isinstance(peers, pd.DataFrame)
        and not peers.empty
        and any(str(peer.get("action_code") or "").strip().upper() in POSITIVE_BROKER_ACTIONS for _, peer in peers.iterrows() if peer.name != row.name)
    ):
        return {
            "score": 250,
            "rule_id": "EVENT_POLICY_REVIEW_BEATS_POSITIVE_ENTRY",
            "reason": "Event-policy review blocks BUY/BUY_MORE candidates until the operator resolves the event evidence.",
        }
    if int(dynamic_result.get("score") or 0) > 0:
        return dynamic_result
    if "WATCH_LOSES_TO_HIGHER_PRIORITY" in enabled_rules and action == "WATCH":
        return {
            "score": -100,
            "rule_id": "WATCH_LOSES_TO_HIGHER_PRIORITY",
            "reason": "Watch rows are informational unless no stronger action exists.",
        }
    return {"score": 0, "rule_id": None, "reason": None}


def apply_conflict_rule_precedence(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    enabled_rules = load_enabled_conflict_rule_ids()
    dynamic_rules = load_enabled_dynamic_conflict_rules_for_ranking()
    if not enabled_rules and not dynamic_rules:
        return df
    out = df.copy()
    out["_conflict_group_symbol"] = out["symbol"].astype("string").str.upper() if "symbol" in out.columns else ""
    out["_conflict_group_asof"] = pd.to_datetime(out["asof_date"], utc=True, errors="coerce") if "asof_date" in out.columns else pd.NaT
    scores: list[int] = []
    rule_ids: list[str | None] = []
    reasons: list[str | None] = []
    for _, row in out.iterrows():
        peers = out[
            (out["_conflict_group_symbol"] == row.get("_conflict_group_symbol"))
            & (out["_conflict_group_asof"] == row.get("_conflict_group_asof"))
        ]
        result = conflict_precedence_for_row(row, enabled_rules, peers=peers, dynamic_rules=dynamic_rules)
        scores.append(int(result.get("score") or 0))
        rule_ids.append(_text(result.get("rule_id")))
        reasons.append(_text(result.get("reason")))
    out["_conflict_rule_precedence"] = scores
    out["_conflict_precedence_rule_id"] = rule_ids
    out["_conflict_precedence_reason"] = reasons
    for idx, row in out.iterrows():
        rule_id = _text(row.get("_conflict_precedence_rule_id"))
        reason = _text(row.get("_conflict_precedence_reason"))
        if not rule_id:
            continue
        out.at[idx, "raw_context_json"] = json.dumps(
            _merge_context(
                row.get("raw_context_json"),
                {
                    "conflict_precedence_rule_id": rule_id,
                    "conflict_precedence_reason": reason,
                    "conflict_precedence_score": int(row.get("_conflict_rule_precedence") or 0),
                },
            ),
            ensure_ascii=False,
            default=str,
            sort_keys=True,
        )
    return out.drop(columns=["_conflict_group_symbol", "_conflict_group_asof"], errors="ignore")


def _json_ready_record(row: pd.Series | dict[str, Any]) -> dict[str, Any]:
    data = row.to_dict() if isinstance(row, pd.Series) else dict(row)
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, str) and key.endswith("_json"):
            out[key] = _parse_jsonish(value, value)
            continue
        if isinstance(value, pd.Timestamp):
            out[key] = value.isoformat()
            continue
        try:
            if pd.api.types.is_scalar(value) and pd.isna(value):
                out[key] = None
                continue
        except Exception as exc:
            record_local_fallback_event(
                module="advisory.action_recommender",
                fallback_type="action_recommender_record_missing_check_failed",
                source="json_ready_record",
                severity="warn",
                reason="Action recommender could not evaluate missingness for a record value and kept the original value.",
                error=exc,
                metadata={"key": str(key), "value_type": type(value).__name__},
            )
        out[key] = value
    return out


def _contract_section_from_context(context: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    return {key: context.get(key) for key in keys if context.get(key) is not None}


def _signal_refresh_contract_section(raw_context: dict[str, Any]) -> dict[str, Any]:
    signal = raw_context.get("signal_refresh")
    if not isinstance(signal, dict) or not signal:
        return {}
    payload = signal.get("action_payload")
    if not isinstance(payload, dict):
        payload = {}
    mapping = raw_context.get("signal_refresh_action_mapping")
    if not isinstance(mapping, dict):
        mapping = {}
    source_contract = payload.get("source_contract")
    if not isinstance(source_contract, dict):
        source_contract = {}
    return _drop_empty_json_values(
        {
            "schema_version": 1,
            "signal_action": signal.get("signal_action"),
            "signal_status": signal.get("signal_status"),
            "signal_source": signal.get("signal_source"),
            "effect_type": signal.get("effect_type"),
            "effect_summary": signal.get("effect_summary"),
            "previous_action": signal.get("previous_action"),
            "action_changed": signal.get("action_changed"),
            "authority_scope": signal.get("authority_scope") or mapping.get("authority_scope") or "review_input_only",
            "portfolio_authority": signal.get("portfolio_authority") or mapping.get("portfolio_authority") or "none",
            "broker_execution_allowed": False,
            "full_advisory_required": True,
            "action_policy_effect": mapping.get("action_policy_effect"),
            "mapping_reason": mapping.get("mapping_reason"),
            "mapped_action": mapping.get("mapped_action"),
            "source_contract": source_contract,
            "source_table": signal.get("source_table") or source_contract.get("source_table"),
            "source_key": signal.get("unique_id") or source_contract.get("source_key"),
            "signal_refresh_bridge_note": raw_context.get("signal_refresh_bridge_note"),
        }
    )


def _compact_event_policy_identity_section(actionability: dict[str, Any]) -> dict[str, Any]:
    market_scope = actionability.get("market_scope")
    if not isinstance(market_scope, dict):
        return {}
    identity = market_scope.get("identity_validation")
    if not isinstance(identity, dict):
        identity = {}
    validated_peers = market_scope.get("validated_affected_peers")
    unresolved_peers = market_scope.get("unresolved_affected_peers")
    if not isinstance(validated_peers, list):
        validated_peers = []
    if not isinstance(unresolved_peers, list):
        unresolved_peers = []
    compact = {
        "status": identity.get("status"),
        "validated_affected_peers": [str(item) for item in validated_peers[:8] if item is not None],
        "unresolved_affected_peers": [str(item) for item in unresolved_peers[:8] if item is not None],
        "requires_company_master_resolution": _boolish_or_none(identity.get("requires_company_master_resolution")),
        "policy_effect": identity.get("policy_effect"),
        "portfolio_authority": identity.get("portfolio_authority") or "none",
        "broker_execution_allowed": False,
        "source": "event_policy_actionability.market_scope.identity_validation",
    }
    if not (compact["status"] or compact["validated_affected_peers"] or compact["unresolved_affected_peers"]):
        return {}
    return {key: value for key, value in compact.items() if value not in (None, "", [], {})}


def _first_watch_reason_contract_section(raw_context: dict[str, Any]) -> dict[str, Any]:
    reasons = _parse_jsonish(raw_context.get("watch_reasons_json"), [])
    if isinstance(reasons, dict):
        reason = reasons
    elif isinstance(reasons, list) and reasons and isinstance(reasons[0], dict):
        reason = reasons[0]
    else:
        reason = {}
    return _contract_section_from_context(
        reason,
        [
            "watch_priority_score",
            "breakout_watch_threshold",
            "context_class_blocks_breakout",
            "watch_breakout_priority_blocked",
            "watch_breakout_reliability_classification",
        ],
    )


def _rule_engine_contract_section(raw_context: dict[str, Any]) -> dict[str, Any]:
    section = _contract_section_from_context(
        raw_context,
        [
            "candidate_state",
            "watch_reason_detail",
            "setup_score",
            "regime_fit_score",
            "regime_fit_weight_effective",
        ],
    )
    soft_failures = _parse_jsonish(raw_context.get("soft_failures_json"), [])
    if not isinstance(soft_failures, list):
        soft_failures = []
    if soft_failures:
        section["soft_failures"] = [str(item) for item in soft_failures if item is not None]
        section["soft_failure_count"] = len(section["soft_failures"])
    return section


def _event_policy_contract_section(raw_context: dict[str, Any]) -> dict[str, Any]:
    mapping = raw_context.get("event_policy_action_mapping")
    if not isinstance(mapping, dict):
        mapping = {}
    actionability = raw_context.get("actionability")
    if not isinstance(actionability, dict):
        actionability = _parse_jsonish(raw_context.get("actionability_json"), {})
    if not isinstance(actionability, dict):
        actionability = {}
    notes = raw_context.get("operator_notes")
    if not isinstance(notes, dict):
        notes = _parse_jsonish(raw_context.get("operator_notes_json"), {})
    if not isinstance(notes, dict):
        notes = _parse_jsonish(raw_context.get("llm_review_json"), {})
    if not isinstance(notes, dict):
        notes = {}
    policy_raw = raw_context.get("policy_raw_context")
    if not isinstance(policy_raw, dict):
        policy_raw = {}
    authority = notes.get("authority_contract")
    if not isinstance(authority, dict):
        authority = policy_raw.get("authority_contract")
    if not isinstance(authority, dict):
        authority = mapping
    has_event_policy_evidence = any(
        value not in (None, "", [], {})
        for value in [
            raw_context.get("policy_class") or raw_context.get("event_class"),
            raw_context.get("event_policy_action_mapping"),
            raw_context.get("actionability_json"),
            raw_context.get("operator_notes_json"),
            raw_context.get("llm_review_json"),
            raw_context.get("llm_review_status"),
            mapping.get("mapped_action"),
            notes.get("final_action_type"),
        ]
    )
    if not has_event_policy_evidence:
        return {}
    policy_source_key = raw_context.get("source_key") or raw_context.get("event_unique_id") or raw_context.get("unique_id")
    event_source_key = (
        policy_raw.get("unique_id")
        or raw_context.get("event_unique_id")
        or raw_context.get("unique_id")
        or policy_source_key
    )
    def _empty_context_value(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, str):
            return not value.strip()
        if isinstance(value, (list, dict, tuple, set)):
            return not bool(value)
        return bool(pd.isna(value)) if pd.api.types.is_scalar(value) else False

    source_trace = raw_context.get("source_trace")
    if _empty_context_value(source_trace):
        source_trace = policy_raw.get("source_trace")
    source_trace = _parse_jsonish(source_trace, source_trace)
    if isinstance(source_trace, list):
        source_trace = [str(item)[:500] for item in source_trace if str(item or "").strip()][:8]
    elif isinstance(source_trace, dict):
        source_trace = {
            key: value
            for key, value in source_trace.items()
            if key
            in {
                "source_table",
                "source_key",
                "object_key",
                "s3_key",
                "storage_key",
                "storage_uri",
                "document_url",
                "raw_text_s3_key",
                "text_s3_key",
                "announcement_storage_form",
                "announcement_storage_reason",
                "llm_evidence_mode",
                "llm_review_ready",
            }
            and value not in (None, "", [], {})
        }
    else:
        source_trace = str(source_trace)[:500] if str(source_trace or "").strip() else None
    event_tensor = raw_context.get("event_tensor")
    if _empty_context_value(event_tensor):
        event_tensor = policy_raw.get("event_tensor")
    event_tensor = _parse_jsonish(event_tensor, {})
    if isinstance(event_tensor, dict):
        event_tensor = {
            key: event_tensor.get(key)
            for key in [
                "event_type",
                "direction",
                "materiality",
                "surprise",
                "novelty",
                "contradiction",
                "confidence",
                "expected_decay_days",
                "source_reliability",
                "state_transition_hint",
                "score_impact",
            ]
            if event_tensor.get(key) not in (None, "", [], {})
        }
    else:
        event_tensor = {}
    evidence_source_key = raw_context.get("event_evidence_evidence_id") or raw_context.get("event_evidence_unique_id")
    evidence_object_pointers = []
    for artifact_type, key_name in [
        ("raw", "event_evidence_raw_s3_key"),
        ("pdf", "event_evidence_pdf_s3_key"),
        ("ocr", "event_evidence_ocr_s3_key"),
        ("full_ocr", "event_evidence_full_ocr_s3_key"),
        ("audio_transcript", "event_evidence_audio_transcript_s3_key"),
        ("concise_summary", "event_evidence_concise_summary_s3_key"),
    ]:
        value = raw_context.get(key_name)
        if not _empty_context_value(value):
            evidence_object_pointers.append(
                {
                    "artifact_type": artifact_type,
                    "s3_key": value,
                    "source_table": ANNOUNCEMENT_EVIDENCE_TABLE,
                    "source_key": evidence_source_key or event_source_key,
                    "storage_form": raw_context.get("event_evidence_announcement_storage_form"),
                }
            )
    attachment_url = raw_context.get("event_evidence_attachment_url")
    if not _empty_context_value(attachment_url):
        evidence_object_pointers.append(
            {
                "artifact_type": "attachment_url",
                "document_url": attachment_url,
                "source_table": ANNOUNCEMENT_EVIDENCE_TABLE,
                "source_key": evidence_source_key or event_source_key,
                "storage_form": raw_context.get("event_evidence_announcement_storage_form"),
            }
        )
    announcement_evidence_source = {
        "source_table": ANNOUNCEMENT_EVIDENCE_TABLE,
        "source_key": evidence_source_key,
        "reference_type": "announcement_evidence",
        "unique_id": raw_context.get("event_evidence_unique_id"),
        "announcement_storage_form": raw_context.get("event_evidence_announcement_storage_form"),
        "announcement_storage_reason": raw_context.get("event_evidence_announcement_storage_reason"),
        "llm_evidence_mode": raw_context.get("event_evidence_llm_evidence_mode"),
        "llm_review_ready": raw_context.get("event_evidence_llm_review_ready"),
        "raw_archive_required": raw_context.get("event_evidence_raw_archive_required"),
        "has_s3_evidence": raw_context.get("event_evidence_has_s3_evidence"),
        "has_text_evidence": raw_context.get("event_evidence_has_text_evidence"),
        "source_reliability": raw_context.get("event_evidence_source_reliability"),
        "attachment_name": raw_context.get("event_evidence_attachment_name"),
        "object_pointers": evidence_object_pointers,
    }
    announcement_evidence_source = {
        key: value
        for key, value in announcement_evidence_source.items()
        if not _empty_context_value(value)
    }
    underlying_sources = [
        {
            "source_table": EVENT_POLICY_TABLE,
            "source_key": policy_source_key,
            "reference_type": "event_policy_action",
        },
        {
            "source_table": EVENT_EVALUATIONS_TABLE,
            "source_key": event_source_key,
            "reference_type": "event_evaluation",
        },
    ]
    if announcement_evidence_source:
        underlying_sources.append(announcement_evidence_source)
    underlying_sources = [
        item
        for item in underlying_sources
        if item.get("source_table") and item.get("source_key")
    ]
    compact = {
        "policy_class": raw_context.get("policy_class") or raw_context.get("event_class"),
        "source_table": raw_context.get("source_table"),
        "source_key": policy_source_key,
        "published_on": raw_context.get("published_on"),
        "setup_id": raw_context.get("setup_id"),
        "unique_id": raw_context.get("unique_id"),
        "source_action": raw_context.get("action_type"),
        "mapped_action": mapping.get("mapped_action") or raw_context.get("action_code"),
        "mapping_changed": mapping.get("mapping_changed"),
        "mapping_reason": mapping.get("mapping_reason"),
        "action_status": raw_context.get("action_status"),
        "llm_review_status": raw_context.get("llm_review_status"),
        "llm_prompt_id": raw_context.get("llm_prompt_id"),
        "llm_prompt_version": raw_context.get("llm_prompt_version"),
        "final_action_type": notes.get("final_action_type"),
        "operator_summary": notes.get("operator_summary"),
        "possible_action": notes.get("possible_action"),
        "wait_for_events": notes.get("wait_for_events"),
        "operator_questions": notes.get("operator_questions"),
        "review_priority": actionability.get("review_priority"),
        "materiality": actionability.get("materiality"),
        "affected_peer_identity": _compact_event_policy_identity_section(actionability),
        "authority_contract": authority,
        "underlying_sources": underlying_sources,
        "source_trace": source_trace,
        "event_tensor": event_tensor,
    }
    return {key: value for key, value in compact.items() if value not in (None, "", [], {})}


def _playbook_contract_section(raw_context: dict[str, Any]) -> dict[str, Any]:
    has_playbook_evidence = any(
        raw_context.get(key) not in (None, "", [], {})
        for key in [
            "playbook_id",
            "hypothesis_id",
            "playbook_source_table",
            "playbook_source_key",
            "playbook_action_mapping",
        ]
    )
    if not has_playbook_evidence:
        return {}
    return _contract_section_from_context(
        raw_context,
        [
            "playbook_id",
            "hypothesis_id",
            "source_table",
            "source_key",
            "playbook_source_table",
            "playbook_source_key",
            "playbook_planned_at",
            "action_type",
            "operator_summary",
            "decision_reason",
        ],
    )


def _manual_review_contract_section(row: pd.Series, raw_context: dict[str, Any], action: str) -> dict[str, Any]:
    existing = _contract_section_from_context(
        raw_context,
        [
            "manual_review_boundary",
            "manual_review_effect",
            "operator_question",
            "review_reason",
            "blocked_original_action_code",
            "blocked_reason_contract_missing_fields",
            "broker_execution_allowed",
        ],
    )
    if action != "MANUAL_REVIEW":
        return existing

    action_source = _text(row.get("action_source"))
    source_action = _text(row.get("source_action"))
    execution_mode = _text(row.get("execution_mode"))
    transaction_type = _text(row.get("transaction_type"))
    review_only = execution_mode == "review_only" or not transaction_type
    inferred = {
        "manual_review_boundary": existing.get("manual_review_boundary") or "action_consolidation_manual_review",
        "manual_review_effect": existing.get("manual_review_effect") or "review_only_no_broker_execution",
        "operator_question": existing.get("operator_question")
        or raw_context.get("operator_question")
        or row.get("action_detail")
        or "Review the candidate evidence before any downstream portfolio, config, or execution change.",
        "review_reason": existing.get("review_reason") or row.get("action_reason"),
        "broker_execution_allowed": existing.get("broker_execution_allowed") if "broker_execution_allowed" in existing else (False if review_only else None),
        "manual_review_action_source": action_source or None,
        "manual_review_source_action": source_action or None,
    }
    rebalance_review_boundaries = {
        "review_manual": "lifecycle_rebalance_manual_review_required",
        "review_stale": "lifecycle_rebalance_stale_review_required",
        "review_horizon": "lifecycle_rebalance_horizon_review_required",
        "review_target": "lifecycle_rebalance_target_review_required",
    }
    if action_source == "rebalance" and source_action.lower() in rebalance_review_boundaries:
        inferred["manual_review_boundary"] = existing.get("manual_review_boundary") or rebalance_review_boundaries[source_action.lower()]
    elif action_source == "event_policy":
        inferred["manual_review_boundary"] = existing.get("manual_review_boundary") or "event_policy_review_required"
    elif action_source.startswith("playbook"):
        inferred["manual_review_boundary"] = existing.get("manual_review_boundary") or "playbook_review_required"
    elif action_source == "manual_review_wait_signal":
        inferred["manual_review_boundary"] = existing.get("manual_review_boundary") or "manual_review_wait_signal_matched"
        inferred["operator_question"] = (
            existing.get("operator_question")
            or raw_context.get("operator_question")
            or "A previously watched Manual Review condition matched. Decide whether the fresh evidence changes the action state or should be closed as noise."
        )
        inferred["review_reason"] = (
            existing.get("review_reason")
            or row.get("action_reason")
            or "Manual Review wait signal matched fresh evidence."
        )
    market_adjustment = _parse_jsonish(raw_context.get("market_context_adjustment_json"), {})
    if not isinstance(market_adjustment, dict):
        market_adjustment = {}
    if raw_context.get("market_context_adjustment") == "positive_action_blocked_by_market_context":
        inferred["manual_review_boundary"] = (
            existing.get("manual_review_boundary") or "market_context_positive_action_review_required"
        )
        inferred["blocked_original_action_code"] = (
            existing.get("blocked_original_action_code") or market_adjustment.get("original_action_code") or None
        )
    if not existing.get("boundary") and inferred.get("manual_review_boundary"):
        inferred["boundary"] = inferred["manual_review_boundary"]
    if not existing.get("effect") and inferred.get("manual_review_effect"):
        inferred["effect"] = inferred["manual_review_effect"]
    if not existing.get("blocked_original_action"):
        inferred["blocked_original_action"] = inferred.get("blocked_original_action_code") or action
    return {key: value for key, value in {**existing, **inferred}.items() if value is not None}


def _same_action_candidate(row: pd.Series, item: dict[str, Any]) -> bool:
    for key in ["action_code", "action_source", "setup_id", "unique_id"]:
        left = _text(row.get(key))
        right = _text(item.get(key))
        if left != right:
            return False
    return True


def _same_symbol_conflict_section(row: pd.Series, candidates: pd.DataFrame | None) -> dict[str, Any]:
    if not isinstance(candidates, pd.DataFrame) or candidates.empty:
        return {}
    losing_candidates: list[dict[str, Any]] = []
    for item in candidates.to_dict(orient="records"):
        if _same_action_candidate(row, item):
            continue
        raw_context = _parse_jsonish(item.get("raw_context_json"), {})
        if not isinstance(raw_context, dict):
            raw_context = {}
        market_adjustment = _parse_jsonish(raw_context.get("market_context_adjustment_json"), {})
        if not isinstance(market_adjustment, dict):
            market_adjustment = {}
        losing_candidate = {
            "action_code": item.get("action_code"),
            "action_source": item.get("action_source"),
            "setup_id": item.get("setup_id"),
            "source_action": item.get("source_action"),
            "action_reason": item.get("action_reason"),
            "action_priority": item.get("action_priority"),
            "published_on": _json_context_value(item.get("published_on")),
        }
        if raw_context.get("market_context_adjustment"):
            losing_candidate.update(
                {
                    "market_context_adjustment": raw_context.get("market_context_adjustment"),
                    "original_action_code": market_adjustment.get("original_action_code"),
                    "market_context_adjustment_reason": market_adjustment.get("reason"),
                    "size_multiplier": market_adjustment.get("size_multiplier"),
                }
            )
        losing_candidates.append({key: value for key, value in losing_candidate.items() if value is not None})
    if not losing_candidates:
        return {}
    return {
        "same_symbol_candidate_count": int(len(candidates)),
        "same_symbol_conflict_count": int(len(losing_candidates)),
        "winning_action_code": row.get("action_code"),
        "winning_action_source": row.get("action_source"),
        "winning_action_priority": row.get("action_priority"),
        "source_precedence_reason": (
            f"Selected {row.get('action_code')} from {row.get('action_source')} over "
            f"{len(losing_candidates)} same-symbol candidate(s) by deterministic action priority, "
            "conflict-rule precedence, and freshness tie-breaks."
        ),
        "losing_candidates": losing_candidates[:5],
    }


def _portfolio_transition_section(raw_context: dict[str, Any]) -> dict[str, Any]:
    contract = _parse_jsonish(raw_context.get("state_transition_contract_json"), {})
    if not isinstance(contract, dict):
        contract = {}
    section = {
        "position_state": raw_context.get("position_state") or contract.get("current_state"),
        "portfolio_status": raw_context.get("portfolio_status") or contract.get("portfolio_status"),
        "portfolio_reason": raw_context.get("portfolio_reason") or contract.get("portfolio_reason"),
        "current_state": contract.get("current_state"),
        "entry_evidence_required": contract.get("entry_evidence_required"),
        "entry_evidence_rule": contract.get("entry_evidence_rule"),
        "broker_execution_allowed": contract.get("broker_execution_allowed"),
        "broker_boundary": contract.get("broker_boundary"),
        "next_required_stage": contract.get("next_required_stage"),
        "can_transition_to": contract.get("can_transition_to"),
    }
    return {key: value for key, value in section.items() if value not in (None, "", [], {})}


def _timestamp_value(*values: Any) -> pd.Timestamp | None:
    for value in values:
        ts = pd.to_datetime(value, utc=True, errors="coerce")
        if not pd.isna(ts):
            return ts
    return None


def _has_price_evidence_after_publication(row: pd.Series, raw_context: dict[str, Any]) -> bool:
    published_on = _timestamp_value(row.get("published_on"), raw_context.get("published_on"))
    evidence_at = _timestamp_value(
        row.get("reference_price_asof"),
        raw_context.get("reference_price_asof"),
        raw_context.get("price_asof"),
        raw_context.get("entry_date"),
        row.get("asof_date"),
        raw_context.get("asof_date"),
    )
    evidence_price = (
        _num(row.get("reference_price"))
        or _num(raw_context.get("reference_price"))
        or _num(raw_context.get("entry_price"))
        or _num(raw_context.get("price"))
    )
    return bool(published_on is not None and evidence_at is not None and evidence_at >= published_on and evidence_price is not None)


ENTRY_ACTIONS = {"BUY", "BUY_MORE"}
EXIT_ACTIONS = {"SELL", "PARTIAL_SELL"}
DE_RISK_ACTIONS = {"SELL", "PARTIAL_SELL", "TIGHTEN_STOP", "REDUCE_EXPOSURE_REVIEW"}
PASSIVE_ACTIONS = {"HOLD", "WATCH", "MANUAL_REVIEW"}


def _transition_action_family(action: Any) -> str:
    normalized = str(action or "").strip().upper()
    if normalized in ENTRY_ACTIONS:
        return "entry"
    if normalized in EXIT_ACTIONS:
        return "exit"
    if normalized in {"TIGHTEN_STOP", "REDUCE_EXPOSURE_REVIEW"}:
        return "de_risk"
    if normalized == "WATCH":
        return "watch"
    if normalized == "HOLD":
        return "hold"
    if normalized == "MANUAL_REVIEW":
        return "manual_review"
    return "unknown"


def _previous_action_from_context(row: pd.Series, raw_context: dict[str, Any]) -> str | None:
    candidates = [
        row.get("previous_action"),
        raw_context.get("previous_action"),
        raw_context.get("latest_action_code"),
        raw_context.get("prior_action_code"),
        raw_context.get("previous_recommendation_action"),
    ]
    for value in candidates:
        text = _text(value)
        if text:
            return text.upper()
    latest_action = raw_context.get("latest_action")
    if isinstance(latest_action, dict):
        text = _text(latest_action.get("action_code") or latest_action.get("signal_action"))
        if text:
            return text.upper()
    return None


def _technical_confirmation_state(raw_context: dict[str, Any]) -> dict[str, Any]:
    technical_state = _text(raw_context.get("technical_state") or raw_context.get("candidate_state") or raw_context.get("current_state"))
    trigger_type = _text(raw_context.get("technical_trigger_type") or raw_context.get("trigger_type"))
    confirmed_states = {"BUY_TRIGGERED", "ADD_ON_PULLBACK", "READY", "HOLD", "PARTIAL_EXIT", "FULL_EXIT", "EMERGENCY_EXIT"}
    entry_confirmed = bool(technical_state and technical_state.upper() in {"BUY_TRIGGERED", "ADD_ON_PULLBACK", "READY"})
    exit_confirmed = bool(technical_state and technical_state.upper() in {"PARTIAL_EXIT", "FULL_EXIT", "EMERGENCY_EXIT"})
    return {
        "technical_state": technical_state,
        "technical_trigger_type": trigger_type,
        "technical_state_known": bool(technical_state),
        "technical_entry_confirmed": entry_confirmed,
        "technical_exit_confirmed": exit_confirmed,
        "technical_confirmation_class": "confirmed" if technical_state and technical_state.upper() in confirmed_states else ("missing" if not technical_state else "not_confirmed"),
    }


def _action_transition_stability_section(
    row: pd.Series,
    raw_context: dict[str, Any],
    action: str,
    *,
    missing_preconditions: list[str],
) -> dict[str, Any]:
    previous_action = _previous_action_from_context(row, raw_context)
    current_family = _transition_action_family(action)
    previous_family = _transition_action_family(previous_action)
    source = _text(row.get("action_source"))
    position_status = _text(raw_context.get("position_status") or raw_context.get("portfolio_status"))
    action_changed = _boolish_or_none(row.get("action_changed"))
    if action_changed is None:
        action_changed = _boolish_or_none(raw_context.get("action_changed"))
    if action_changed is None and previous_action:
        action_changed = previous_action != action
    technical = _technical_confirmation_state(raw_context)
    blockers: list[str] = []
    warnings: list[str] = []

    def add_blocker(value: str) -> None:
        if value not in blockers:
            blockers.append(value)

    def add_warning(value: str) -> None:
        if value not in warnings:
            warnings.append(value)

    review_only_source = _is_review_only_signal_source(source)
    if review_only_source and action in BROKER_CAPABLE_ACTIONS:
        add_blocker("review_only_source_attempted_broker_capable_action")
    if action in {"BUY_MORE", "SELL", "PARTIAL_SELL", "TIGHTEN_STOP"} and not position_status:
        add_blocker("position_state_missing_for_position_transition")
    if previous_family == "exit" and current_family == "entry" and not technical["technical_entry_confirmed"]:
        add_blocker("entry_after_exit_without_fresh_technical_confirmation")
    if previous_family == "entry" and current_family in {"exit", "de_risk"} and review_only_source:
        add_blocker("review_only_source_cannot_directly_reverse_entry")
    if previous_action and previous_action != action and action in ENTRY_ACTIONS and not technical["technical_entry_confirmed"]:
        add_warning("changed_to_entry_without_confirmed_entry_state")
    if previous_action and previous_action != action and action in EXIT_ACTIONS and not (technical["technical_exit_confirmed"] or raw_context.get("next_action") or raw_context.get("suggested_action")):
        add_warning("changed_to_exit_without_explicit_exit_state")
    if missing_preconditions:
        add_warning("base_action_preconditions_incomplete")

    if blockers:
        stability_status = "unstable_requires_deterministic_confirmation"
    elif previous_action is None:
        stability_status = "insufficient_prior_action_history"
    elif previous_action == action:
        stability_status = "stable_repeat_action"
    elif current_family in {"watch", "hold", "manual_review"}:
        stability_status = "stable_non_executable_transition"
    elif warnings:
        stability_status = "stable_with_warnings"
    else:
        stability_status = "stable_changed_action"

    return {
        "schema_version": 1,
        "previous_action": previous_action,
        "current_action": action,
        "previous_action_family": previous_family,
        "current_action_family": current_family,
        "action_changed": bool(action_changed) if action_changed is not None else None,
        "stability_status": stability_status,
        "stable_for_ranking": not blockers,
        "transition_blockers": blockers,
        "transition_warnings": warnings,
        "position_status": position_status,
        "source_stage": source,
        "review_only_source": bool(review_only_source),
        "technical_confirmation": technical,
        "policy_effect": "transition_audit_only_no_ranking_change",
        "portfolio_authority": "none",
        "broker_execution_allowed": False,
    }


def _action_transition_section(row: pd.Series, raw_context: dict[str, Any], action: str) -> dict[str, Any]:
    source = _text(row.get("action_source"))
    source_action = _text(row.get("source_action"))
    transaction = _text(row.get("transaction_type"))
    execution_mode = _text(row.get("execution_mode"))
    broker_order_candidate = action in BROKER_CAPABLE_ACTIONS and execution_mode == "broker_order" and transaction in {"BUY", "SELL"}
    position_status = raw_context.get("position_status")
    reference_price = _num(row.get("reference_price"))
    stop_price = _num(row.get("stop_price"))
    action_fraction = _num(row.get("action_fraction"))
    state_effect_by_action = {
        "BUY": "planned_entry_candidate",
        "BUY_MORE": "planned_add_candidate",
        "SELL": "planned_full_exit_candidate",
        "PARTIAL_SELL": "planned_partial_exit_candidate",
        "TIGHTEN_STOP": "policy_update_review",
        "REDUCE_EXPOSURE_REVIEW": "review_only_de_risk_pressure",
        "HOLD": "no_position_change",
        "WATCH": "watch_only",
        "MANUAL_REVIEW": "review_only",
    }
    required_stage_by_action = {
        "BUY": "execution_engine_order_preview",
        "BUY_MORE": "execution_engine_order_preview",
        "SELL": "execution_engine_order_preview",
        "PARTIAL_SELL": "execution_engine_order_preview",
        "TIGHTEN_STOP": "position_lifecycle_policy_audit",
        "REDUCE_EXPOSURE_REVIEW": "full_advisory_or_lifecycle_risk_review",
        "HOLD": "continue_monitoring",
        "WATCH": "watchers_or_next_advisory",
        "MANUAL_REVIEW": "operator_manual_review",
    }
    required_preconditions_by_action = {
        "BUY": [
            "broker_order_execution_mode",
            "buy_transaction_type",
            "risk_level_present",
            "entry_evidence_after_publication",
        ],
        "BUY_MORE": [
            "open_position_context",
            "broker_order_execution_mode",
            "buy_transaction_type",
            "risk_level_present",
            "add_on_evidence_present",
        ],
        "SELL": [
            "open_position_context",
            "broker_order_execution_mode",
            "sell_transaction_type",
            "exit_trigger_present",
            "full_exit_implied",
        ],
        "PARTIAL_SELL": [
            "open_position_context",
            "broker_order_execution_mode",
            "sell_transaction_type",
            "partial_exit_trigger_present",
            "partial_exit_fraction_resolved",
        ],
        "TIGHTEN_STOP": ["open_position_context", "recommended_stop_present", "policy_audit_only"],
        "REDUCE_EXPOSURE_REVIEW": ["negative_event_or_context_evidence", "no_broker_order", "full_advisory_required"],
        "HOLD": ["open_position_context", "no_exit_trigger_present"],
        "WATCH": ["watch_evidence_present", "no_broker_order"],
        "MANUAL_REVIEW": ["operator_question_present", "no_broker_order"],
    }
    missing_preconditions: list[str] = []

    def add_missing(precondition: str) -> None:
        if precondition not in missing_preconditions:
            missing_preconditions.append(precondition)

    if action in BROKER_CAPABLE_ACTIONS and execution_mode != "broker_order":
        add_missing("broker_order_execution_mode")
    if action in {"BUY", "BUY_MORE"} and transaction != "BUY":
        add_missing("buy_transaction_type")
    if action in {"SELL", "PARTIAL_SELL"} and transaction != "SELL":
        add_missing("sell_transaction_type")
    if action in {"BUY_MORE", "SELL", "PARTIAL_SELL", "TIGHTEN_STOP", "HOLD"} and not position_status:
        add_missing("open_position_context")
    if action in {"BUY", "BUY_MORE"} and stop_price is None and _num(row.get("invalidation_price")) is None and _num(row.get("recommended_stop_price")) is None:
        add_missing("risk_level_present")
    has_post_publication_price_evidence = _has_price_evidence_after_publication(row, raw_context)
    if action == "BUY" and not has_post_publication_price_evidence:
        add_missing("entry_evidence_after_publication")
    if action == "BUY_MORE" and not has_post_publication_price_evidence:
        add_missing("add_on_evidence_present")
    if action in {"SELL", "PARTIAL_SELL"} and not (
        row.get("action_detail") or raw_context.get("suggested_action") or raw_context.get("next_action")
    ):
        add_missing("exit_trigger_present")
    if action in {"SELL", "PARTIAL_SELL"} and not has_post_publication_price_evidence:
        add_missing("exit_trigger_present")
    if action == "PARTIAL_SELL" and action_fraction is None:
        add_missing("partial_exit_fraction_resolved")
    if action == "TIGHTEN_STOP" and _num(row.get("recommended_stop_price")) is None:
        add_missing("recommended_stop_present")
    stability = _action_transition_stability_section(
        row,
        raw_context,
        action,
        missing_preconditions=missing_preconditions,
    )
    broker_boundary = (
        "Action row is not a broker order. Execution engine must create a separate safety-gated preview before any order."
        if broker_order_candidate
        else "This action is review-only/monitoring/policy context and does not create a broker order."
    )
    section = {
        "schema_version": 1,
        "action_code": action,
        "source_stage": source,
        "source_action": source_action,
        "state_effect": state_effect_by_action.get(action, "unknown"),
        "transaction_type": transaction,
        "execution_mode": execution_mode,
        "broker_order_candidate": bool(broker_order_candidate),
        "broker_execution_allowed": False,
        "broker_boundary": broker_boundary,
        "next_required_stage": required_stage_by_action.get(action, "operator_review"),
        "allowed_after_preview": action in BROKER_CAPABLE_ACTIONS,
        "required_preconditions": required_preconditions_by_action.get(action, []),
        "missing_preconditions": missing_preconditions,
        "precondition_status": "complete" if not missing_preconditions else "incomplete",
        "position_status": position_status,
        "lifecycle_next_action": raw_context.get("next_action") or raw_context.get("suggested_action"),
        "entry_evidence_required": action in {"BUY", "BUY_MORE"},
        "exit_evidence_required": action in {"SELL", "PARTIAL_SELL"},
        "policy_audit_required": action == "TIGHTEN_STOP",
        "transition_stability": stability,
        "transition_stability_status": stability.get("stability_status"),
        "transition_stable_for_ranking": stability.get("stable_for_ranking"),
    }
    if action == "WATCH":
        watch_reason_contract = _first_watch_reason_contract_section(raw_context)
        section.update(
            {
                "context_candidate_state": raw_context.get("candidate_state") or raw_context.get("current_state"),
                "context_policy_effect": raw_context.get("context_policy_effect"),
                "context_authority_scope": raw_context.get("context_authority_scope"),
                "context_class_reliability_classification": raw_context.get("context_class_reliability_classification"),
                **watch_reason_contract,
            }
        )
    return {key: value for key, value in section.items() if value not in (None, "", [], {})}


def _transition_stability_policy_target(action: str, contract: dict[str, Any]) -> str | None:
    if not ACTION_TRANSITION_STABILITY_POLICY_GATE_ENABLED:
        return None
    normalized = str(action or "").strip().upper()
    if normalized not in BROKER_CAPABLE_ACTIONS:
        return None
    transition = ((contract.get("evidence") or {}).get("action_transition") or {}) if isinstance(contract, dict) else {}
    if not bool(transition.get("broker_order_candidate")):
        return None
    stability = transition.get("transition_stability") if isinstance(transition.get("transition_stability"), dict) else {}
    if stability.get("stability_status") != "unstable_requires_deterministic_confirmation":
        return None
    blockers = stability.get("transition_blockers") if isinstance(stability.get("transition_blockers"), list) else []
    if not blockers:
        return None
    if normalized in ENTRY_ACTIONS:
        return "WATCH"
    if normalized in EXIT_ACTIONS:
        return "REDUCE_EXPOSURE_REVIEW"
    return "MANUAL_REVIEW"


def _apply_transition_stability_downgrade(
    out: pd.DataFrame,
    *,
    idx: Any,
    row: pd.Series,
    contract: dict[str, Any],
    target_action: str,
) -> dict[str, Any]:
    original_action = str(row.get("action_code") or "").upper()
    transition = ((contract.get("evidence") or {}).get("action_transition") or {}) if isinstance(contract, dict) else {}
    stability = transition.get("transition_stability") if isinstance(transition.get("transition_stability"), dict) else {}
    blockers = [str(value) for value in stability.get("transition_blockers") or [] if str(value)]
    context = _parse_jsonish(row.get("raw_context_json"), {})
    if not isinstance(context, dict):
        context = {}
    context.update(
        {
            "action_transition_gate": "unstable_transition_downgraded",
            "action_transition_policy_effect": "downgrade_to_review_only_signal_no_broker_execution",
            "action_transition_stability_status": stability.get("stability_status"),
            "action_transition_blockers": blockers,
            "blocked_original_action_code": original_action,
            "manual_review_boundary": "action_transition_unstable_requires_fresh_confirmation",
            "manual_review_effect": "review_only_no_broker_execution",
            "operator_question": "Wait for fresh technical, lifecycle, and risk confirmation before this transition can become broker-executable.",
            "review_reason": "Broker-capable action was downgraded because the state transition was unstable.",
            "broker_execution_allowed": False,
            "portfolio_authority": "none",
            "full_advisory_required": True,
        }
    )
    out.at[idx, "action_code"] = target_action
    out.at[idx, "action_priority"] = int(ACTION_PRIORITY.get(target_action, ACTION_PRIORITY["MANUAL_REVIEW"]))
    out.at[idx, "transaction_type"] = None
    out.at[idx, "execution_mode"] = "review_only"
    out.at[idx, "raw_context_json"] = json.dumps(context, ensure_ascii=False, default=str, sort_keys=True)
    existing_reason = _text(row.get("action_reason"))
    blocker_text = ", ".join(blockers) if blockers else "unstable transition"
    prefix = f"{target_action}: broker-capable {original_action} downgraded because transition stability failed ({blocker_text})."
    out.at[idx, "action_reason"] = prefix if not existing_reason else f"{prefix} Original reason: {existing_reason}"
    out.at[idx, "action_detail"] = (
        "Transition-stability policy gate requires a fresh full-advisory confirmation before broker execution."
    )
    updated = build_recommendation_reason_contract(out.loc[idx])
    updated["status"] = "transition_downgraded"
    updated["original_action_code"] = original_action
    updated["transition_policy_gate"] = {
        "gate": "action_transition_stability",
        "enabled": bool(ACTION_TRANSITION_STABILITY_POLICY_GATE_ENABLED),
        "original_action_code": original_action,
        "downgraded_action_code": target_action,
        "stability_status": stability.get("stability_status"),
        "transition_blockers": blockers,
        "policy_effect": "downgrade_to_review_only_signal_no_broker_execution",
        "broker_execution_allowed": False,
        "portfolio_authority": "none",
        "full_advisory_required": True,
    }
    return updated


def build_recommendation_reason_contract(row: pd.Series, candidates: pd.DataFrame | None = None) -> dict[str, Any]:
    raw_context = _parse_jsonish(row.get("raw_context_json"), {})
    if not isinstance(raw_context, dict):
        raw_context = {}
    action = str(row.get("action_code") or "").upper()
    execution_mode = _text(row.get("execution_mode"))
    action_reason = _text(row.get("action_reason"))
    action_detail = _text(row.get("action_detail"))
    risk_fields = {
        "reference_price": _num(row.get("reference_price")),
        "stop_price": _num(row.get("stop_price")),
        "invalidation_price": _num(row.get("invalidation_price")),
        "recommended_stop_price": _num(row.get("recommended_stop_price")),
        "recommended_target_price": _num(row.get("recommended_target_price")),
        "expected_horizon_days": None if pd.isna(pd.to_numeric(row.get("expected_horizon_days"), errors="coerce")) else int(pd.to_numeric(row.get("expected_horizon_days"), errors="coerce")),
        "action_fraction": _num(row.get("action_fraction")),
        "approved_allocation_inr": _num(row.get("approved_allocation_inr")),
    }
    risk_fields = {key: value for key, value in risk_fields.items() if value is not None}
    competing: list[dict[str, Any]] = []
    if isinstance(candidates, pd.DataFrame) and not candidates.empty:
        for item in candidates.head(max(1, ACTION_MANUAL_REVISION_POINTERS_MAX_CANDIDATES)).to_dict(orient="records"):
            item_context = _parse_jsonish(item.get("raw_context_json"), {})
            if not isinstance(item_context, dict):
                item_context = {}
            item_event_policy = _event_policy_contract_section(item_context)
            compact_item = {
                    "action_code": item.get("action_code"),
                    "action_source": item.get("action_source"),
                    "setup_id": item.get("setup_id"),
                    "source_action": item.get("source_action"),
                    "action_reason": item.get("action_reason"),
                    "action_priority": item.get("action_priority"),
                    "event_policy": item_event_policy or None,
            }
            competing.append({key: value for key, value in compact_item.items() if value not in (None, "", [], {})})
    conflict_resolution = _contract_section_from_context(
        raw_context,
        ["conflict_precedence_rule_id", "conflict_precedence_reason", "conflict_precedence_score"],
    )
    conflict_resolution = {**conflict_resolution, **_same_symbol_conflict_section(row, candidates)}
    context_overlay_summary = raw_context.get("context_overlay_summary")
    context_overlay_alignment = _context_overlay_action_alignment(action, context_overlay_summary)
    causal_event_memory_summary = raw_context.get("causal_event_memory_summary")
    causal_event_memory_alignment = _causal_event_memory_action_alignment(action, causal_event_memory_summary)
    evidence_sections = {
        "screener": _contract_section_from_context(raw_context, ["source_screener_slug", "source_screener_list", "screener_name"]),
        "technical": _contract_section_from_context(
            raw_context,
            [
                "technical_state",
                "technical_total_score",
                "technical_score",
                "technical_trigger_type",
                "technical_setup_archetype",
                "technical_setup_quality_json",
                "pivot_price",
                "support_price",
                "setup_score",
            ],
        ),
        "event": _contract_section_from_context(
            raw_context,
            ["event_class", "verdict", "state_transition_hint", "score_impact", "review_action", "veto", "review_reason", "action_status"],
        ),
        "event_policy": _event_policy_contract_section(raw_context),
        "playbook": _playbook_contract_section(raw_context),
        "wait_signal": _contract_section_from_context(
            raw_context,
            ["wait_signal_followup", "signal_id", "match_reason", "condition_json", "wait_question"],
        ),
        "feature_freshness": _contract_section_from_context(
            raw_context,
            [
                "feature_freshness_status",
                "feature_freshness_gate",
                "feature_freshness_blockers",
                "blocked_original_action_code",
            ],
        ),
        "rule_engine": _rule_engine_contract_section(raw_context),
        "watchlist_context": {
            **_contract_section_from_context(
                raw_context,
                [
                    "watch_source",
                    "watch_status",
                    "current_state",
                    "candidate_state",
                    "context_source",
                    "context_overlay_id",
                    "context_authority_scope",
                    "context_policy_effect",
                    "context_reliability_classification",
                    "context_class_reliability_classification",
                    "context_reliability_evaluated_at",
                ],
            ),
            **_first_watch_reason_contract_section(raw_context),
        },
        "manual_review": _manual_review_contract_section(row, raw_context, action),
        "macro_regime": _contract_section_from_context(
            raw_context,
            [
                "macro_risk_state",
                "macro_stress_score",
                "regime_state",
                "market_regime",
                "regime_fit_score",
                "regime_fit_weight_effective",
            ],
        ),
        "context_overlays": {
            **_contract_section_from_context(raw_context, ["context_overlay_summary"]),
            **({"context_overlay_action_alignment": context_overlay_alignment} if context_overlay_alignment else {}),
        },
        "causal_event_memory": {
            **_contract_section_from_context(raw_context, ["causal_event_memory_summary"]),
            **({"causal_event_memory_action_alignment": causal_event_memory_alignment} if causal_event_memory_alignment else {}),
        },
        "company_memory": _contract_section_from_context(
            raw_context,
            [
                "company_memory_review",
                "company_memory_action_mapping",
                "company_memory_bridge_note",
            ],
        ),
        "signal_quality_overlay_rule_adjustment": _contract_section_from_context(
            raw_context,
            [
                "signal_quality_overlay_rule_adjustment",
                "signal_quality_overlay_rule_adjustment_json",
            ],
        ),
        "signal_refresh": _signal_refresh_contract_section(raw_context),
        "conflict_resolution": conflict_resolution,
        "risk": risk_fields,
        "lifecycle": _contract_section_from_context(raw_context, ["position_status", "next_action", "suggested_action", "lifecycle_reason", "next_action_reason"]),
        "portfolio_transition": _portfolio_transition_section(raw_context),
        "action_transition": _action_transition_section(row, raw_context, action),
    }
    market_context = _parse_jsonish(raw_context.get("market_context_json"), {})
    if not isinstance(market_context, dict):
        market_context = {}
    market_summary = market_context.get("summary") if isinstance(market_context.get("summary"), dict) else {}
    market_symbol_context = market_context.get("symbol_context") if isinstance(market_context.get("symbol_context"), dict) else {}
    market_adjustment = _parse_jsonish(raw_context.get("market_context_adjustment_json"), {})
    if not isinstance(market_adjustment, dict):
        market_adjustment = {}
    market_context_section = {
        "market_context_adjustment": raw_context.get("market_context_adjustment"),
        "market_context_adjustment_reason": market_adjustment.get("reason"),
        "market_context_block_reason": market_adjustment.get("risk_off_block_reason"),
        "multi_context_diagnostics": market_adjustment.get("multi_context_diagnostics"),
        "regime_name": market_adjustment.get("regime_name") or market_summary.get("regime_name"),
        "macro_risk_state": market_adjustment.get("macro_risk_state") or market_summary.get("macro_risk_state"),
        "macro_hard_risk": market_adjustment.get("macro_hard_risk"),
        "systemic_macro_hard_risk": market_adjustment.get("systemic_macro_hard_risk"),
        "high_macro_risk_hard_block": market_adjustment.get("high_macro_risk_hard_block"),
        "high_macro_risk_hard_block_enabled": market_adjustment.get("high_macro_risk_hard_block_enabled"),
        "score_risk_off_hard_block": market_adjustment.get("score_risk_off_hard_block"),
        "weak_breadth_block": market_adjustment.get("weak_breadth_block"),
        "breadth_trend_alignment_pct": market_adjustment.get("breadth_trend_alignment_pct") or market_summary.get("breadth_trend_alignment_pct"),
        "risk_off_score": market_adjustment.get("risk_off_score") or market_summary.get("risk_off_score"),
        "size_multiplier": market_adjustment.get("size_multiplier"),
        "macro_sizing_multiplier": market_adjustment.get("macro_sizing_multiplier") or market_summary.get("macro_sizing_multiplier"),
        "top_context_rank_pct": market_symbol_context.get("rank_pct"),
        "top_context_sector": market_symbol_context.get("sector_name") or market_symbol_context.get("sector_code"),
    }
    market_context_section = {key: value for key, value in market_context_section.items() if value is not None}
    if market_context_section:
        evidence_sections["macro_regime"] = {**evidence_sections.get("macro_regime", {}), **market_context_section}
    technical_quality = _parse_jsonish(evidence_sections.get("technical", {}).get("technical_setup_quality_json"), {})
    if isinstance(technical_quality, dict) and technical_quality:
        evidence_sections["technical"]["technical_setup_quality"] = technical_quality
    present_sections = [key for key, value in evidence_sections.items() if value]
    missing: list[str] = []
    if not action:
        missing.append("action_code")
    if not action_reason:
        missing.append("action_reason")
    if not row.get("action_source"):
        missing.append("action_source")
    if not present_sections:
        missing.append("evidence_context")
    if action in {"BUY", "BUY_MORE", "SELL", "PARTIAL_SELL"}:
        if not execution_mode:
            missing.append("execution_mode")
        transaction_type = _text(row.get("transaction_type"))
        if action in {"BUY", "BUY_MORE"} and transaction_type != "BUY":
            missing.append("buy_transaction_type")
        if action in {"SELL", "PARTIAL_SELL"} and transaction_type != "SELL":
            missing.append("sell_transaction_type")
        if action in {"BUY", "BUY_MORE"} and not any(risk_fields.get(key) is not None for key in ["stop_price", "invalidation_price", "recommended_stop_price"]):
            missing.append("buy_risk_level")
        if action in {"SELL", "PARTIAL_SELL"} and not (action_detail or raw_context.get("suggested_action") or raw_context.get("next_action")):
            missing.append("sell_exit_trigger")
    if str(row.get("action_source") or "").startswith("playbook"):
        if not (raw_context.get("playbook_id") or raw_context.get("hypothesis_id")):
            missing.append("playbook_id")
        if not (row.get("unique_id") or raw_context.get("playbook_source_key") or raw_context.get("event_unique_id")):
            missing.append("playbook_source_key")
        if not (raw_context.get("checks_json") or raw_context.get("checks")):
            missing.append("playbook_review_checks")
    if action in {"BUY", "BUY_MORE"} and raw_context.get("event_class"):
        if not raw_context.get("event_unique_id"):
            missing.append("event_unique_id")
        if raw_context.get("review_action") is None:
            missing.append("event_review_action")
    status = "complete" if not missing else "incomplete"
    return {
        "schema_version": 1,
        "status": status,
        "missing_fields": missing,
        "symbol": str(row.get("symbol") or "").upper(),
        "action_code": action,
        "original_action_code": str(row.get("original_action_code") or market_adjustment.get("original_action_code") or action).upper(),
        "action_source": _text(row.get("action_source")),
        "source_action": _text(row.get("source_action")),
        "setup_id": _text(row.get("setup_id")),
        "unique_id": _text(row.get("unique_id")),
        "primary_reason": action_reason,
        "reason_detail": action_detail,
        "execution_mode": execution_mode,
        "transaction_type": _text(row.get("transaction_type")),
        "evidence_sections_present": present_sections,
        "evidence": evidence_sections,
        "competing_candidates": competing,
    }


def add_recommendation_reason_contracts(df: pd.DataFrame, all_candidates: pd.DataFrame | None = None) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    candidates = all_candidates if isinstance(all_candidates, pd.DataFrame) and not all_candidates.empty else out
    tmp = candidates.copy()
    tmp["asof_date"] = pd.to_datetime(tmp["asof_date"], utc=True, errors="coerce")
    candidate_groups: dict[tuple[Any, Any], pd.DataFrame] = {}
    for key, group in tmp.groupby(["asof_date", "symbol"], dropna=False):
        candidate_groups[key] = group.sort_values(["action_priority", "published_on"], ascending=[False, False], kind="stable")
    contracts: list[str] = []
    statuses: list[str] = []
    for idx, row in out.iterrows():
        asof_date = pd.to_datetime(row.get("asof_date"), utc=True, errors="coerce")
        group = candidate_groups.get((asof_date, row.get("symbol")), pd.DataFrame())
        contract = build_recommendation_reason_contract(row, group)
        if contract["status"] != "complete":
            original_action = str(row.get("action_code") or "").upper()
            original_context = _parse_jsonish(row.get("raw_context_json"), {})
            if not isinstance(original_context, dict):
                original_context = {}
            raw_missing = [str(value) for value in contract.get("missing_fields") or []]
            original_context.update(
                {
                    "manual_review_boundary": "incomplete_reason_contract",
                    "manual_review_effect": "review_only_no_broker_execution",
                    "operator_question": "Resolve the missing reason-contract fields before this recommendation can become broker-executable.",
                    "review_reason": "Recommendation was downgraded because required action evidence or risk controls were missing.",
                    "blocked_original_action_code": original_action,
                    "blocked_reason_contract_missing_fields": raw_missing,
                    "broker_execution_allowed": False,
                }
            )
            out.at[idx, "action_code"] = "MANUAL_REVIEW"
            out.at[idx, "action_priority"] = int(ACTION_PRIORITY["MANUAL_REVIEW"])
            out.at[idx, "transaction_type"] = None
            out.at[idx, "execution_mode"] = "review_only"
            out.at[idx, "raw_context_json"] = json.dumps(original_context, ensure_ascii=False, default=str, sort_keys=True)
            missing_text = ", ".join(contract.get("missing_fields") or [])
            existing_reason = _text(row.get("action_reason"))
            out.at[idx, "action_reason"] = f"Manual review required: incomplete reason contract ({missing_text})." if not existing_reason else f"Manual review required: incomplete reason contract ({missing_text}). Original reason: {existing_reason}"
            contract = build_recommendation_reason_contract(out.loc[idx], group)
            contract["status"] = "incomplete_downgraded"
            contract["original_action_code"] = original_action
            contract["missing_fields"] = contract.get("missing_fields") or []
            if missing_text and missing_text not in contract["missing_fields"]:
                contract["missing_fields"] = [*contract["missing_fields"], missing_text]
        else:
            target_action = _transition_stability_policy_target(str(row.get("action_code") or "").upper(), contract)
            if target_action:
                contract = _apply_transition_stability_downgrade(
                    out,
                    idx=idx,
                    row=row,
                    contract=contract,
                    target_action=target_action,
                )
        contracts.append(json.dumps(contract, ensure_ascii=False, default=str, sort_keys=True))
        statuses.append(str(contract["status"]))
    out["recommendation_reason_json"] = contracts
    out["reason_contract_status"] = statuses
    return out


def add_feature_freshness_contracts(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    snapshots: list[str] = []
    for _, row in out.iterrows():
        symbol = str(row.get("symbol") or "").strip().upper()
        asof_date = row.get("asof_date")
        if not symbol:
            snapshots.append(json.dumps({"status": "error", "reason": "symbol_missing"}, sort_keys=True))
            continue
        contract = build_feature_freshness_contract(symbol, asof_date=asof_date)
        contract["captured_at"] = pd.Timestamp.utcnow().isoformat()
        contract["captured_for"] = "advisory_action_recommendation"
        snapshots.append(json.dumps(contract, ensure_ascii=False, default=str, sort_keys=True))
    out["feature_freshness_json"] = snapshots
    return out


def add_deferred_feature_freshness_contracts(df: pd.DataFrame, *, reason: str = "action_refresh_persist_skipped_live_feature_rebuild") -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    snapshots: list[str] = []
    now = pd.Timestamp.utcnow().isoformat()
    for _, row in out.iterrows():
        action = str(row.get("action_code") or "").upper()
        is_positive_broker_action = action in FEATURE_FRESHNESS_BLOCKED_POSITIVE_ACTIONS
        contract = {
            "status": "blocked" if is_positive_broker_action else "deferred",
            "reason": reason,
            "captured_at": now,
            "captured_for": "advisory_action_recommendation",
            "policy_effect": "full_advisory_required_before_broker_authority",
            "broker_execution_allowed": False,
            "full_advisory_required": True,
            "blockers": (
                [
                    {
                        "input_key": "feature_freshness_contract",
                        "label": "Decision-time feature freshness",
                        "status": "missing",
                        "required": True,
                        "reason": "Fast action refresh did not rebuild live feature freshness during persistence.",
                    }
                ]
                if is_positive_broker_action
                else []
            ),
        }
        snapshots.append(json.dumps(contract, ensure_ascii=False, default=str, sort_keys=True))
    out["feature_freshness_json"] = snapshots
    return out


def _has_missing_text_values(df: pd.DataFrame, column: str) -> bool:
    if df.empty or column not in df.columns:
        return True
    return bool(df[column].map(_text).isna().any())


def _carry_forward_symbol_column(target: pd.DataFrame, source: pd.DataFrame, column: str) -> pd.DataFrame:
    if target.empty or source.empty or column not in source.columns or "symbol" not in target.columns or "symbol" not in source.columns:
        return target
    source_subset = source[["symbol", column]].copy()
    source_subset["symbol"] = source_subset["symbol"].astype("string").str.upper()
    source_subset = source_subset[source_subset[column].map(_text).notna()]
    if source_subset.empty:
        return target
    lookup = source_subset.drop_duplicates(subset=["symbol"], keep="last").set_index("symbol")[column].to_dict()
    out = target.copy()
    if column not in out.columns:
        out[column] = None
    for idx, symbol in out["symbol"].astype("string").str.upper().items():
        if _text(out.at[idx, column]) is None and symbol in lookup:
            out.at[idx, column] = lookup[symbol]
    return out


def _feature_freshness_blockers(contract: Any) -> list[dict[str, Any]]:
    payload = _parse_jsonish(contract, {})
    if not isinstance(payload, dict):
        return []
    blockers = payload.get("blockers")
    if not isinstance(blockers, list):
        return []
    return [
        row
        for row in blockers
        if isinstance(row, dict)
        and bool(row.get("required", True))
        and str(row.get("status") or "").lower() in {"missing", "stale", "error"}
    ]


def apply_feature_freshness_gates(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    if _has_missing_text_values(out, "feature_freshness_json"):
        out = add_feature_freshness_contracts(out)
    changed = 0
    for idx, row in out.iterrows():
        action = str(row.get("action_code") or "").upper()
        if action not in FEATURE_FRESHNESS_BLOCKED_POSITIVE_ACTIONS:
            continue
        contract = _parse_jsonish(row.get("feature_freshness_json"), {})
        if not isinstance(contract, dict) or str(contract.get("status") or "").lower() != "blocked":
            continue
        blockers = _feature_freshness_blockers(contract)
        if not blockers:
            continue
        context = _parse_jsonish(row.get("raw_context_json"), {})
        if not isinstance(context, dict):
            context = {}
        blocker_labels = [
            str(item.get("label") or item.get("input_key") or "required input")
            for item in blockers
        ]
        existing_reason = _text(row.get("action_reason"))
        context.update(
            {
                "manual_review_boundary": "feature_freshness_required_input_blocked",
                "manual_review_effect": "review_only_no_broker_execution",
                "operator_question": "Repair or refresh the stale/missing required inputs before allowing this positive action to become broker-executable.",
                "review_reason": "Required decision inputs were stale, missing, or errored when this action was created.",
                "blocked_original_action_code": action,
                "feature_freshness_gate": "blocked_positive_broker_action",
                "feature_freshness_status": contract.get("status"),
                "feature_freshness_blockers": blockers,
                "broker_execution_allowed": False,
            }
        )
        out.at[idx, "action_code"] = "MANUAL_REVIEW"
        out.at[idx, "action_priority"] = int(ACTION_PRIORITY["MANUAL_REVIEW"])
        out.at[idx, "transaction_type"] = None
        out.at[idx, "execution_mode"] = "review_only"
        out.at[idx, "raw_context_json"] = json.dumps(context, ensure_ascii=False, default=str, sort_keys=True)
        blocker_text = ", ".join(blocker_labels)
        prefix = f"Manual review required: required data inputs are blocked ({blocker_text})."
        out.at[idx, "action_reason"] = prefix if not existing_reason else f"{prefix} Original reason: {existing_reason}"
        changed += 1
    out.attrs["feature_freshness_gate_changed_count"] = changed
    return out


def apply_identity_resolution_gates(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    if "action_code" not in out.columns or "symbol" not in out.columns:
        out.attrs["identity_gate_changed_count"] = 0
        return out
    action_series = out["action_code"].astype("string").str.upper()
    execution_mode = out["execution_mode"].astype("string").str.lower() if "execution_mode" in out.columns else pd.Series("", index=out.index)
    broker_mask = action_series.isin(BROKER_CAPABLE_ACTIONS) & ~execution_mode.isin({"review_only", "no_broker_execution"})
    symbols = out.loc[broker_mask, "symbol"].dropna().astype(str).str.upper().drop_duplicates().tolist()
    if not symbols:
        out.attrs["identity_gate_changed_count"] = 0
        return out
    gaps = load_action_identity_gaps(symbols)
    if gaps.empty or "symbol" not in gaps.columns:
        out.attrs["identity_gate_changed_count"] = 0
        return out
    gap_by_symbol = {
        str(row.get("symbol") or "").strip().upper(): row
        for row in gaps.to_dict(orient="records")
        if str(row.get("symbol") or "").strip()
    }
    changed = 0
    for idx, row in out.loc[broker_mask].iterrows():
        symbol = str(row.get("symbol") or "").strip().upper()
        gap = gap_by_symbol.get(symbol)
        if not gap:
            continue
        original_action = str(row.get("action_code") or "").upper()
        identity_gap = str(gap.get("identity_gap") or "identity_unresolved")
        context = _parse_jsonish(row.get("raw_context_json"), {})
        if not isinstance(context, dict):
            context = {}
        context.update(
            {
                "manual_review_boundary": "broker_identity_unresolved",
                "manual_review_effect": "review_only_no_broker_execution",
                "operator_question": "Repair company/Dhan identity before this action can become broker-executable.",
                "review_reason": "Broker-capable action was downgraded because company/security identity is missing.",
                "blocked_original_action_code": original_action,
                "identity_gap": identity_gap,
                "company_master_id": _json_context_value(gap.get("company_master_id")),
                "nse_ticker": _json_context_value(gap.get("nse_ticker")),
                "bse_ticker": _json_context_value(gap.get("bse_ticker")),
                "dhan_nse_id": _json_context_value(gap.get("dhan_nse_id")),
                "dhan_bse_id": _json_context_value(gap.get("dhan_bse_id")),
                "broker_execution_allowed": False,
            }
        )
        out.at[idx, "action_code"] = "MANUAL_REVIEW"
        out.at[idx, "action_priority"] = int(ACTION_PRIORITY["MANUAL_REVIEW"])
        out.at[idx, "transaction_type"] = None
        out.at[idx, "execution_mode"] = "review_only"
        out.at[idx, "raw_context_json"] = json.dumps(context, ensure_ascii=False, default=str, sort_keys=True)
        prefix = f"Manual review required: broker identity unresolved ({identity_gap})."
        existing_reason = _text(row.get("action_reason"))
        out.at[idx, "action_reason"] = prefix if not existing_reason else f"{prefix} Original reason: {existing_reason}"
        changed += 1
    out.attrs["identity_gate_changed_count"] = changed
    return out


def _deterministic_manual_revision_pointers(row: pd.Series, candidates: pd.DataFrame, *, status: str = "deterministic") -> dict[str, Any]:
    action = str(row.get("action_code") or "ACTION").upper()
    symbol = str(row.get("symbol") or "").upper()
    source = str(row.get("action_source") or "unknown")
    reason = _text(row.get("action_reason")) or "No explicit reason was provided by the winning candidate."
    losing_actions = []
    if not candidates.empty:
        losing_actions = [
            f"{item.get('action_code')} from {item.get('action_source')}"
            for item in candidates.to_dict(orient="records")
            if str(item.get("action_code") or "").upper() != action or str(item.get("action_source") or "") != source
        ][:5]
    pointers = {
        "revision_summary": f"{symbol}: final action is {action} from {source}. {reason}",
        "key_reasons": [reason, f"Winning source: {source}"],
        "manual_checks": [
            "Confirm latest OHLCV/current price before acting.",
            "Check whether newer news, announcements, or exchange events contradict the decision.",
            "Verify stop/invalidation and position sizing before broker execution.",
        ],
        "risk_flags": losing_actions or ["No conflicting lower-priority candidate was available in the consolidation set."],
        "missing_data": [],
        "operator_questions": [
            "Is the evidence still fresh enough to act on?",
            "Does the current market/regime context still support this action?",
            "Would this action violate exposure, liquidity, or sector concentration limits?",
        ],
        "status": status,
    }
    return pointers


def _manual_revision_prompt(row: pd.Series, candidates: pd.DataFrame) -> str:
    payload = {
        "instruction": (
            "Create manual revision pointers for an investment operator. "
            "Do not change the final action. Do not recommend broker execution. "
            "Summarize what the operator should manually verify before accepting, rejecting, or modifying the decision."
        ),
        "final_decision": _json_ready_record(row),
        "competing_candidates": [
            _json_ready_record(item)
            for item in candidates.head(max(1, ACTION_MANUAL_REVISION_POINTERS_MAX_CANDIDATES)).to_dict(orient="records")
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


def build_manual_revision_pointers(row: pd.Series, candidates: pd.DataFrame, *, use_llm: bool | None = None) -> tuple[dict[str, Any], str, str]:
    enabled = ACTION_MANUAL_REVISION_POINTERS_ENABLED if use_llm is None else bool(use_llm)
    model = ACTION_MANUAL_REVISION_POINTERS_MODEL
    if not enabled or str(model).strip().lower() in {"", "off", "none", "disabled", "false"}:
        return _deterministic_manual_revision_pointers(row, candidates, status="disabled"), model, "disabled"
    try:
        codex_model = model.split(":", 1)[1] if model.startswith("codex:") else None if model == "codex" else model
        result = run_codex_structured(
            _manual_revision_prompt(row, candidates),
            response_model=ManualRevisionPointers,
            model=codex_model,
            system_prompt=(
                "You are a cautious investment-operations reviewer. "
                "You produce concise manual review pointers for a human operator. "
                "You never override the decision, submit trades, or invent missing evidence."
            ),
            max_attempts=2,
            timeout_seconds=ACTION_MANUAL_REVISION_POINTERS_TIMEOUT_SECONDS,
        )
        pointers = result.model_dump()
        pointers["status"] = "ok"
        return pointers, model, "ok"
    except Exception as exc:
        pointers = _deterministic_manual_revision_pointers(row, candidates, status="fallback_after_error")
        pointers["llm_error"] = f"{type(exc).__name__}: {exc}"
        record_fallback_event(
            module="advisory.action_recommender",
            source="manual_revision_pointers",
            fallback_type="llm_deterministic_fallback",
            severity="warn",
            symbol=_text(row.get("symbol")),
            reason="Manual revision pointer LLM failed; deterministic pointers were used.",
            deterministic_fallback=True,
            error=exc,
            metadata={"model": model, "action_code": row.get("action_code"), "action_source": row.get("action_source")},
        )
        return pointers, model, "fallback_after_error"


def add_manual_revision_pointers(df: pd.DataFrame, all_candidates: pd.DataFrame | None = None, *, use_llm: bool | None = None) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    candidates = all_candidates if isinstance(all_candidates, pd.DataFrame) and not all_candidates.empty else out
    candidate_groups: dict[tuple[Any, Any], pd.DataFrame] = {}
    tmp = candidates.copy()
    tmp["asof_date"] = pd.to_datetime(tmp["asof_date"], utc=True, errors="coerce")
    for key, group in tmp.groupby(["asof_date", "symbol"], dropna=False):
        candidate_groups[key] = group.sort_values(["action_priority", "published_on"], ascending=[False, False], kind="stable")
    summaries: list[str | None] = []
    payloads: list[str | None] = []
    prompt_ids: list[str | None] = []
    prompt_versions: list[str | None] = []
    prompt_schema_versions: list[str | None] = []
    models: list[str | None] = []
    statuses: list[str | None] = []
    for _, row in out.iterrows():
        asof_date = pd.to_datetime(row.get("asof_date"), utc=True, errors="coerce")
        key = (asof_date, row.get("symbol"))
        group = candidate_groups.get(key, pd.DataFrame())
        pointers, model, status = build_manual_revision_pointers(row, group, use_llm=use_llm)
        summaries.append(_text(pointers.get("revision_summary")))
        payloads.append(json.dumps(pointers, ensure_ascii=False, default=str, sort_keys=True))
        prompt_ids.append(MANUAL_REVISION_PROMPT_ID)
        prompt_versions.append(MANUAL_REVISION_PROMPT_VERSION)
        prompt_schema_versions.append(MANUAL_REVISION_PROMPT_SCHEMA_VERSION)
        models.append(model)
        statuses.append(status)
    out["manual_revision_summary"] = summaries
    out["manual_revision_pointers_json"] = payloads
    out["manual_revision_prompt_id"] = prompt_ids
    out["manual_revision_prompt_version"] = prompt_versions
    out["manual_revision_prompt_schema_version"] = prompt_schema_versions
    out["manual_revision_model"] = models
    out["manual_revision_status"] = statuses
    return out


def _follow_up_window_days(row: pd.Series) -> int:
    plan = _parse_jsonish(row.get("action_plan_json"), {})
    out = pd.to_numeric(plan.get("follow_up_window_days") if isinstance(plan, dict) else None, errors="coerce")
    if pd.isna(out):
        out = 3
    return max(0, int(out))


def _playbook_plan_is_fresh(row: pd.Series, *, asof_date: pd.Timestamp) -> bool:
    planned_at = pd.to_datetime(row.get("planned_at"), utc=True, errors="coerce")
    if pd.isna(planned_at):
        return False
    expiry = planned_at.normalize() + pd.Timedelta(days=_follow_up_window_days(row) + 1)
    return asof_date < expiry


def _playbook_target_symbols(row: pd.Series, base_symbols: list[str], requested_symbols: list[str] | None) -> list[str]:
    symbol = _text(row.get("symbol"))
    if symbol:
        targets = [symbol.upper()]
    else:
        targets = [str(value).upper() for value in base_symbols if _text(value)]
    if requested_symbols:
        allowed = {str(value).upper() for value in requested_symbols}
        targets = [value for value in targets if value in allowed]
    return sorted(set(targets))


def build_playbook_action_candidates(
    *,
    asof_date: pd.Timestamp,
    base_symbols: list[str],
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    plans = load_playbook_action_plans(asof_date=asof_date, symbols=symbols)
    if plans.empty:
        return []
    if setup_ids:
        allowed_setup_ids = {str(value).upper() for value in setup_ids}
        plans = plans[plans["hypothesis_id"].astype("string").str.upper().isin(allowed_setup_ids)]
        if plans.empty:
            return []
    rows: list[dict[str, Any]] = []
    for _, row in plans.iterrows():
        if not _playbook_plan_is_fresh(row, asof_date=asof_date):
            continue
        action_type = str(row.get("action_type") or row.get("suggested_action") or "MANUAL_REVIEW").strip().upper()
        mapped_action, transaction_type = PLAYBOOK_ACTION_MAP.get(action_type, ("MANUAL_REVIEW", None))
        mapping_context: dict[str, Any] = {
            "playbook_action_mapping_version": 2,
            "original_mapped_action": mapped_action,
            "mapping_changed": False,
            "mapping_reason": None,
        }
        if action_type == "BUY_WATCH" and ACTION_PLAYBOOK_BUY_WATCH_AS_WATCH:
            mapped_action = "WATCH"
            transaction_type = None
            mapping_context.update(
                {
                    "mapping_changed": True,
                    "mapping_reason": (
                        "BUY_WATCH playbook rows are positive watchlist pressure only; "
                        "they should not outrank broker-capable technical BUY/BUY_MORE rows once confirmation exists."
                    ),
                    "authority_scope": "watchlist_pressure_only",
                    "broker_execution_allowed": False,
                }
            )
        elif action_type == "REDUCE_EXPOSURE_REVIEW" and ACTION_PLAYBOOK_REDUCE_EXPOSURE_AS_REVIEW_ACTION:
            mapped_action = "REDUCE_EXPOSURE_REVIEW"
            transaction_type = None
            mapping_context.update(
                {
                    "mapping_changed": True,
                    "mapping_reason": (
                        "REDUCE_EXPOSURE_REVIEW playbook rows are explicit de-risk pressure. "
                        "They should appear as review-only risk actions instead of generic Manual Review."
                    ),
                    "authority_scope": "review_input_only",
                    "portfolio_authority": "none",
                    "broker_execution_allowed": False,
                    "full_advisory_required": True,
                }
            )
        targets = _playbook_target_symbols(row, base_symbols, symbols)
        if not targets:
            continue
        risk_controls = _parse_jsonish(row.get("risk_controls_json"), {})
        checks = _parse_jsonish(row.get("checks_json"), [])
        for symbol in targets:
            rows.append(
                _build_record(
                    asof_date=asof_date,
                    published_on=row.get("planned_at"),
                    setup_id=row.get("hypothesis_id"),
                    symbol=symbol,
                    unique_id=row.get("source_key"),
                    action_code=mapped_action,
                    action_source="playbook_symbol" if _text(row.get("symbol")) else "playbook_market",
                    source_action=action_type,
                    transaction_type=transaction_type,
                    execution_mode="review_only",
                    action_fraction=risk_controls.get("max_fraction") if isinstance(risk_controls, dict) else None,
                    invest_score_pct=float(row.get("confidence") or 0.0) * 100.0,
                    expected_horizon_days=_follow_up_window_days(row),
                    action_reason=row.get("decision_reason"),
                    action_detail=row.get("operator_summary"),
                    raw_context={
                        **row.to_dict(),
                        "source_table": ACTION_PLANS_TABLE,
                        "source_key": row.get("source_key"),
                        "playbook_source_table": row.get("source_table"),
                        "playbook_source_key": row.get("source_key"),
                        "playbook_planned_at": row.get("planned_at"),
                        "checks": checks,
                        "risk_controls": risk_controls,
                        "playbook_action_mapping": mapping_context,
                        "bridge_note": "Production playbook action plans are risk/review overlays only; they do not create broker-executable trades.",
                    },
                )
            )
    return rows


def _event_policy_actionability_context(row: pd.Series) -> dict[str, Any]:
    context = _parse_jsonish(row.get("actionability_json"), {})
    return context if isinstance(context, dict) else {}


def _event_policy_operator_notes(row: pd.Series) -> dict[str, Any]:
    notes = _parse_jsonish(row.get("operator_notes_json"), {})
    if not isinstance(notes, dict):
        notes = _parse_jsonish(row.get("llm_review_json"), {})
    return notes if isinstance(notes, dict) else {}


def _map_event_policy_action(row: pd.Series) -> tuple[str, str | None, dict[str, Any]]:
    action_type = str(row.get("action_type") or "MANUAL_REVIEW").strip().upper()
    mapped_action, transaction_type = EVENT_POLICY_ACTION_MAP.get(action_type, ("MANUAL_REVIEW", None))
    mapping_context: dict[str, Any] = {
        "event_policy_action_mapping_version": 2,
        "original_mapped_action": mapped_action,
        "mapping_changed": False,
        "mapping_reason": None,
    }
    if action_type == "BUY_WATCH" and ACTION_EVENT_POLICY_BUY_WATCH_AS_WATCH:
        mapped_action = "WATCH"
        transaction_type = None
        mapping_context.update(
            {
                "mapping_changed": True,
                "mapping_reason": (
                    "BUY_WATCH event-policy rows are positive watchlist pressure only; "
                    "they should not outrank broker-capable technical BUY/BUY_MORE rows once confirmation exists."
                ),
                "authority_scope": "watchlist_pressure_only",
                "broker_execution_allowed": False,
            }
        )
    elif action_type == "REDUCE_EXPOSURE_REVIEW" and ACTION_EVENT_POLICY_REDUCE_EXPOSURE_AS_REVIEW_ACTION:
        mapped_action = "REDUCE_EXPOSURE_REVIEW"
        transaction_type = None
        mapping_context.update(
            {
                "mapping_changed": True,
                "mapping_reason": (
                    "REDUCE_EXPOSURE_REVIEW event-policy rows are explicit de-risk pressure. "
                    "They should appear as review-only risk actions instead of generic Manual Review."
                ),
                "authority_scope": "review_input_only",
                "portfolio_authority": "none",
                "broker_execution_allowed": False,
                "full_advisory_required": True,
            }
        )
    elif action_type == "MANUAL_REVIEW":
        notes = _event_policy_operator_notes(row)
        final_action = str(notes.get("final_action_type") or "").strip().upper()
        actionability = _event_policy_actionability_context(row)
        review_priority = str(actionability.get("review_priority") or "").strip().lower()
        policy_class = str(row.get("policy_class") or row.get("event_class") or "").strip().upper()
        materiality = str(actionability.get("materiality") or row.get("materiality") or "").strip().lower()
        action_status = str(row.get("action_status") or "").strip().lower()
        source_verdict = str(row.get("source_verdict") or "").strip().lower()
        source_setup_effect = str(row.get("source_setup_effect") or "").strip().lower()
        low_action_class = policy_class in {"DIVIDEND", "ANALYST_MEET", "CORPORATE_ACTION_NEUTRAL", "OTHER"}
        low_actionability = (
            final_action == "NO_ACTION"
            or (
                low_action_class
                and review_priority in {"", "low"}
                and materiality in {"", "low"}
                and not any(token in action_status for token in ["risk", "blocked", "veto", "capital_structure"])
            )
        )
        negative_review_only = (
            policy_class in EVENT_POLICY_NEGATIVE_CLASSES
            and action_status in {"risk_overlay", "blocked_by_adversarial_review"}
            and "capital_structure" not in action_status
        )
        positive_watch_only = (
            policy_class in EVENT_POLICY_POSITIVE_CLASSES
            and action_status in {"positive_but_incomplete", "manual_review_required", "downgraded_by_adversarial_review"}
            and final_action not in {"REDUCE_EXPOSURE_REVIEW", "NO_ACTION"}
            and source_verdict in {"", "review_manual", "continue"}
            and source_setup_effect in {"", "neutral", "strengthens", "mixed"}
        )
        if negative_review_only and ACTION_EVENT_POLICY_REDUCE_EXPOSURE_AS_REVIEW_ACTION:
            mapped_action = "REDUCE_EXPOSURE_REVIEW"
            transaction_type = None
            mapping_context.update(
                {
                    "mapping_changed": True,
                    "mapping_reason": (
                        "Negative event-policy Manual Review with risk_overlay status was converted to explicit review-only "
                        "de-risk pressure. It should not consume generic Manual Review or create broker execution authority."
                    ),
                    "authority_scope": "review_input_only",
                    "portfolio_authority": "none",
                    "broker_execution_allowed": False,
                    "full_advisory_required": True,
                    "event_policy_auto_resolution": "negative_manual_review_to_reduce_exposure_review",
                    "event_policy_final_action_type": final_action or action_type,
                    "event_policy_review_priority": review_priority or None,
                }
            )
        elif positive_watch_only and ACTION_EVENT_POLICY_BUY_WATCH_AS_WATCH:
            mapped_action = "WATCH"
            transaction_type = None
            mapping_context.update(
                {
                    "mapping_changed": True,
                    "mapping_reason": (
                        "Positive-but-incomplete event-policy Manual Review was converted to review-only WATCH pressure. "
                        "The event can keep the symbol visible, but entry still requires technical, risk, lifecycle, and price confirmation."
                    ),
                    "authority_scope": "watchlist_pressure_only",
                    "portfolio_authority": "none",
                    "broker_execution_allowed": False,
                    "full_advisory_required": True,
                    "event_policy_auto_resolution": "positive_manual_review_to_watch",
                    "event_policy_final_action_type": final_action or action_type,
                    "event_policy_review_priority": review_priority or None,
                }
            )
        elif low_actionability:
            mapped_action = "WATCH"
            transaction_type = None
            mapping_context.update(
                {
                    "mapping_changed": True,
                    "mapping_reason": (
                        "Low-actionability event-policy Manual Review was downgraded to WATCH so it remains visible "
                        "without blocking stronger technical or portfolio actions."
                    ),
                    "authority_scope": "watchlist_pressure_only",
                    "broker_execution_allowed": False,
                    "event_policy_final_action_type": final_action or None,
                    "event_policy_review_priority": review_priority or None,
                }
            )
    mapping_context["mapped_action"] = mapped_action
    return mapped_action, transaction_type, mapping_context


def build_event_policy_action_candidates(
    *,
    asof_date: pd.Timestamp,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    policies = load_event_policy_actions(asof_date=asof_date, symbols=symbols, setup_ids=setup_ids)
    if policies.empty:
        return []
    rows: list[dict[str, Any]] = []
    for _, row in policies.iterrows():
        action_type = str(row.get("action_type") or "MANUAL_REVIEW").strip().upper()
        mapped_action, transaction_type, mapping_context = _map_event_policy_action(row)
        confidence = _num(row.get("confidence")) or 0.0
        invest_score_pct = max(0.0, min(100.0, confidence * 100.0 if confidence <= 1.0 else confidence))
        rows.append(
            _build_record(
                asof_date=asof_date,
                published_on=row.get("published_on"),
                setup_id=row.get("setup_id"),
                symbol=row.get("symbol"),
                unique_id=row.get("unique_id"),
                action_code=mapped_action,
                action_source="event_policy",
                source_action=action_type,
                transaction_type=transaction_type,
                execution_mode="review_only",
                invest_score_pct=invest_score_pct,
                expected_horizon_days=None,
                action_reason=row.get("action_reason"),
                action_detail=row.get("action_detail"),
                raw_context={
                    **row.to_dict(),
                    "source_table": EVENT_POLICY_TABLE,
                    "source_key": row.get("unique_id"),
                    "event_policy_action_mapping": mapping_context,
                    "event_policy_bridge_note": "Deterministic event policies are review/risk overlays only; they do not create broker-executable trades.",
                    "policy_checks": _parse_jsonish(row.get("checks_json"), []),
                    "policy_raw_context": _parse_jsonish(row.get("raw_context_json"), {}),
                },
            )
        )
    return rows


def _map_company_memory_review_action(row: pd.Series) -> tuple[str | None, dict[str, Any]]:
    signal = str(row.get("recommended_signal") or "").strip().upper()
    confidence = _num(row.get("confidence")) or 0.0
    conviction = _num(row.get("conviction_score")) or 0.0
    mapping_context: dict[str, Any] = {
        "company_memory_action_mapping_version": 1,
        "recommended_signal": signal,
        "confidence": confidence,
        "conviction_score": conviction,
        "authority_scope": "review_input_only",
        "portfolio_authority": "none",
        "broker_execution_allowed": False,
        "full_advisory_required": True,
        "min_confidence": ACTION_COMPANY_MEMORY_MIN_CONFIDENCE,
        "min_conviction": ACTION_COMPANY_MEMORY_MIN_CONVICTION,
    }
    if confidence < ACTION_COMPANY_MEMORY_MIN_CONFIDENCE:
        mapping_context["mapped_action"] = None
        mapping_context["mapping_reason"] = "Company-memory confidence is below the review-only action bridge threshold."
        return None, mapping_context
    if signal in {"BUY", "BUY_MORE", "WATCH"}:
        if conviction < ACTION_COMPANY_MEMORY_MIN_CONVICTION:
            mapping_context["mapped_action"] = None
            mapping_context["mapping_reason"] = "Positive company-memory conviction is below the watch bridge threshold."
            return None, mapping_context
        mapping_context["mapped_action"] = "WATCH"
        mapping_context["mapping_reason"] = (
            "Company-memory positive/constructive signal becomes WATCH pressure only. "
            "It cannot create BUY/BUY_MORE or broker authority."
        )
        mapping_context["action_policy_effect"] = "watch_only_no_buy_authority"
        return "WATCH", mapping_context
    if signal in {"SELL", "SELL_PARTIAL"}:
        mapping_context["mapped_action"] = "REDUCE_EXPOSURE_REVIEW"
        mapping_context["mapping_reason"] = (
            "Company-memory negative/de-risk signal becomes review-only de-risk pressure. "
            "It cannot create SELL/PARTIAL_SELL or broker authority."
        )
        mapping_context["action_policy_effect"] = "review_only_de_risk_no_sell_authority"
        return "REDUCE_EXPOSURE_REVIEW", mapping_context
    mapping_context["mapped_action"] = None
    mapping_context["mapping_reason"] = "Company-memory signal is not actionable for the review-only action bridge."
    return None, mapping_context


def build_company_memory_action_candidates(
    *,
    asof_date: pd.Timestamp,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    reviews = load_company_memory_reviews(asof_date=asof_date, symbols=symbols, setup_ids=setup_ids)
    if reviews.empty:
        return []
    rows: list[dict[str, Any]] = []
    for _, row in reviews.iterrows():
        mapped_action, mapping_context = _map_company_memory_review_action(row)
        if not mapped_action:
            continue
        confidence = _num(row.get("confidence")) or 0.0
        conviction = _num(row.get("conviction_score")) or 0.0
        payload = _parse_jsonish(row.get("payload_json"), {})
        if not isinstance(payload, dict):
            payload = {}
        risk_flags = _parse_jsonish(row.get("risk_flags_json"), [])
        evidence_used = _parse_jsonish(row.get("evidence_used_json"), [])
        wait_for = _parse_jsonish(row.get("wait_for_json"), [])
        rows.append(
            _build_record(
                asof_date=asof_date,
                published_on=row.get("review_date"),
                setup_id="COMPANY_MEMORY_REVIEW",
                symbol=row.get("symbol"),
                unique_id=None,
                action_code=mapped_action,
                action_source="company_memory",
                source_action=row.get("recommended_signal"),
                transaction_type=None,
                execution_mode="review_only",
                invest_score_pct=max(0.0, min(100.0, confidence * 100.0 if confidence <= 1.0 else confidence)),
                action_reason=row.get("summary"),
                action_detail=row.get("thesis"),
                raw_context={
                    "company_memory_review": {
                        "review_date": row.get("review_date"),
                        "recommended_signal": row.get("recommended_signal"),
                        "confidence": confidence,
                        "conviction_score": conviction,
                        "summary": row.get("summary"),
                        "thesis": row.get("thesis"),
                        "risk_flags": risk_flags if isinstance(risk_flags, list) else [],
                        "evidence_used": evidence_used if isinstance(evidence_used, list) else [],
                        "wait_for": wait_for if isinstance(wait_for, list) else [],
                        "deterministic_boundary": row.get("deterministic_boundary"),
                        "authority_scope": row.get("authority_scope") or "review_input_only",
                        "model_name": row.get("model_name"),
                        "prompt_id": row.get("prompt_id"),
                        "prompt_version": row.get("prompt_version"),
                        "prompt_schema_version": row.get("prompt_schema_version"),
                        "review_status": row.get("review_status"),
                        "fallback_used": bool(row.get("fallback_used")) if row.get("fallback_used") is not None else False,
                        "evidence_source_contract": payload.get("evidence_source_contract") if isinstance(payload, dict) else None,
                        "context_overlay_summary": payload.get("context_overlay_summary") if isinstance(payload, dict) else None,
                    },
                    "company_memory_action_mapping": mapping_context,
                    "company_memory_bridge_note": (
                        "Company-memory review is review input only. Positive memory can add WATCH pressure; "
                        "negative memory can add de-risk review pressure. It never creates broker-executable trades."
                    ),
                },
            )
        )
    return rows


def _matched_wait_signal_context(row: pd.Series) -> dict[str, Any]:
    evidence = _parse_jsonish(row.get("evidence_json"), {})
    if not isinstance(evidence, dict):
        evidence = {}
    wait_signal_context = evidence.get("wait_signal") if isinstance(evidence.get("wait_signal"), dict) else {}
    condition = _parse_jsonish(row.get("condition_json"), {})
    if not isinstance(condition, dict):
        condition = {}
    return {
        "signal_id": row.get("signal_id"),
        "match_source_table": row.get("match_source_table"),
        "match_source_key": row.get("match_source_key"),
        "match_reason": row.get("match_reason"),
        "manual_review_item_id": wait_signal_context.get("manual_review_item_id") or row.get("signal_source_key"),
        "manual_review_source_table": wait_signal_context.get("manual_review_source_table") or row.get("signal_source_table"),
        "manual_review_source_key": wait_signal_context.get("manual_review_source_key") or row.get("signal_source_key"),
        "wait_question": wait_signal_context.get("wait_question") or row.get("wait_question"),
        "condition_type": condition.get("condition_type") or row.get("signal_type"),
        "condition": condition,
        "evidence": evidence,
        "bridge_note": "Matched Manual Review wait signals become review-only action candidates; they do not create broker-executable trades.",
    }


def build_matched_wait_signal_action_candidates(
    *,
    asof_date: pd.Timestamp,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    matches = load_matched_manual_review_wait_signals(asof_date=asof_date, symbols=symbols, setup_ids=setup_ids)
    if matches.empty:
        return []
    rows: list[dict[str, Any]] = []
    for _, row in matches.iterrows():
        symbol = _text(row.get("signal_symbol")) or _text(row.get("match_symbol"))
        if not symbol:
            continue
        context = _matched_wait_signal_context(row)
        confidence = _num(row.get("match_score"))
        invest_score_pct = None if confidence is None else max(0.0, min(100.0, confidence * 100.0 if confidence <= 1.0 else confidence))
        wait_question = _text(context.get("wait_question"))
        reason_bits = [
            "Manual Review wait signal matched fresh evidence.",
            _text(row.get("match_reason")),
        ]
        if wait_question:
            reason_bits.append(f"Original wait: {wait_question}")
        rows.append(
            _build_record(
                asof_date=asof_date,
                published_on=row.get("matched_at") or row.get("observed_at"),
                setup_id=MANUAL_REVIEW_WAIT_SIGNAL_SETUP_ID,
                symbol=symbol,
                unique_id=f"{row.get('signal_id')}:{row.get('match_source_table')}:{row.get('match_source_key')}",
                action_code="MANUAL_REVIEW",
                action_source="manual_review_wait_signal",
                source_action=row.get("signal_expected_action") or row.get("expected_action") or "MANUAL_REVIEW",
                transaction_type=None,
                execution_mode="review_only",
                invest_score_pct=invest_score_pct,
                action_reason=" ".join(part for part in reason_bits if part),
                action_detail=row.get("operator_summary"),
                raw_context={**row.to_dict(), "wait_signal_followup": context},
            )
        )
    return rows


def _map_signal_refresh_action(row: pd.Series) -> tuple[str | None, dict[str, Any]]:
    raw_action = str(row.get("signal_action") or "").strip().upper()
    payload = _parse_jsonish(row.get("action_payload_json"), {})
    if not isinstance(payload, dict):
        payload = {}
    authority_contract = payload.get("authority_contract") if isinstance(payload.get("authority_contract"), dict) else {}
    mapping_context = {
        "signal_refresh_action_mapping_version": 1,
        "source_signal_action": raw_action,
        "signal_source": row.get("signal_source"),
        "signal_status": row.get("signal_status"),
        "effect_type": row.get("effect_type"),
        "effect_summary": row.get("effect_summary"),
        "previous_action": row.get("previous_action"),
        "action_changed": _boolish_or_none(row.get("action_changed")),
        "full_advisory_required": True,
        "authority_scope": row.get("authority_scope") or authority_contract.get("authority_scope") or "review_input_only",
        "portfolio_authority": row.get("portfolio_authority") or authority_contract.get("portfolio_authority") or "none",
        "broker_execution_allowed": False,
        "source_authority_contract": authority_contract,
    }
    if raw_action in {"BUY", "BUY_MORE", "BUY_TRIGGERED", "ADD_ON_PULLBACK", "WATCH"}:
        mapping_context["mapped_action"] = "WATCH"
        mapping_context["mapping_reason"] = (
            "Fast signal-refresh positive evidence becomes WATCH pressure only. "
            "It cannot create BUY/BUY_MORE or broker authority before full advisory confirmation."
        )
        mapping_context["action_policy_effect"] = "watch_only_no_buy_authority"
        return "WATCH", mapping_context
    if raw_action in {"REDUCE_EXPOSURE_REVIEW", "REDUCE_REVIEW"}:
        mapping_context["mapped_action"] = "REDUCE_EXPOSURE_REVIEW"
        mapping_context["mapping_reason"] = (
            "Fast signal-refresh negative evidence becomes review-only de-risk pressure. "
            "It cannot create SELL/PARTIAL_SELL or broker authority before lifecycle/full advisory confirmation."
        )
        mapping_context["action_policy_effect"] = "review_only_de_risk_no_sell_authority"
        return "REDUCE_EXPOSURE_REVIEW", mapping_context
    if raw_action == "TIGHTEN_STOP":
        mapping_context["mapped_action"] = "TIGHTEN_STOP"
        mapping_context["mapping_reason"] = "Fast signal-refresh stop evidence can request review-only stop tightening."
        mapping_context["action_policy_effect"] = "review_only_stop_policy_no_broker_authority"
        return "TIGHTEN_STOP", mapping_context
    if raw_action in {"MANUAL_REVIEW", "REVIEW_MANUAL"}:
        recovered_action, recovered_context = _recover_signal_refresh_manual_review_action(row=row, payload=payload)
        mapping_context.update(recovered_context)
        if recovered_action:
            mapping_context["mapped_action"] = recovered_action
            return recovered_action, mapping_context
        if recovered_context.get("drop_manual_review_candidate"):
            mapping_context["mapped_action"] = None
            return None, mapping_context
        mapping_context["mapped_action"] = "MANUAL_REVIEW"
        mapping_context["mapping_reason"] = "Fast signal-refresh evidence requires manual/full advisory review."
        mapping_context["action_policy_effect"] = "manual_review_only_no_broker_authority"
        return "MANUAL_REVIEW", mapping_context
    mapping_context["mapped_action"] = None
    mapping_context["mapping_reason"] = "Signal-refresh action is not eligible for action consolidation."
    return None, mapping_context


def _first_payload_event_policy(payload: dict[str, Any]) -> dict[str, Any]:
    event_policy_rows = payload.get("event_policy")
    if isinstance(event_policy_rows, list):
        for item in event_policy_rows:
            if isinstance(item, dict):
                return item
    if isinstance(event_policy_rows, dict):
        return event_policy_rows
    return {}


def _recover_signal_refresh_manual_review_action(*, row: pd.Series, payload: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    signal_source = str(row.get("signal_source") or "").strip().lower()
    effect_type = str(row.get("effect_type") or "").strip().lower()
    event_policy_row = _first_payload_event_policy(payload)
    context: dict[str, Any] = {
        "manual_review_auto_resolution_source": None,
    }
    if signal_source == "router_event":
        context.update(
            {
                "manual_review_auto_resolution_source": "router_event",
                "mapping_reason": (
                    "Raw watcher news/announcement router context is review-only WATCH evidence. "
                    "It should not create generic Manual Review unless a wait signal or event-policy row explicitly requires it."
                ),
                "action_policy_effect": "watch_only_no_buy_authority",
                "authority_scope": "watchlist_pressure_only",
                "portfolio_authority": "none",
                "broker_execution_allowed": False,
                "full_advisory_required": True,
            }
        )
        return "WATCH", context
    if not event_policy_row:
        return None, context

    action_type = str(event_policy_row.get("action_type") or "").strip().upper()
    action_status = str(event_policy_row.get("action_status") or "").strip().lower()
    policy_class = str(event_policy_row.get("policy_class") or event_policy_row.get("event_class") or "").strip().upper()
    source_verdict = str(event_policy_row.get("source_verdict") or "").strip().lower()
    source_setup_effect = str(event_policy_row.get("source_setup_effect") or "").strip().lower()
    common = {
        "manual_review_auto_resolution_source": "embedded_event_policy",
        "embedded_event_policy_action_type": action_type or None,
        "embedded_event_policy_action_status": action_status or None,
        "embedded_event_policy_class": policy_class or None,
        "portfolio_authority": "none",
        "broker_execution_allowed": False,
        "full_advisory_required": True,
    }
    if action_type == "REDUCE_EXPOSURE_REVIEW" or (
        policy_class in EVENT_POLICY_NEGATIVE_CLASSES
        and action_status in {"risk_overlay", "blocked_by_adversarial_review"}
    ):
        return (
            "REDUCE_EXPOSURE_REVIEW",
            {
                **common,
                "mapping_reason": (
                    "Signal-refresh Manual Review wraps negative event-policy risk evidence; recover it as explicit review-only de-risk pressure."
                ),
                "action_policy_effect": "review_only_de_risk_no_sell_authority",
                "authority_scope": "review_input_only",
            },
        )
    if action_type == "BUY_WATCH" or (
        policy_class in EVENT_POLICY_POSITIVE_CLASSES
        and action_status in {"positive_but_incomplete", "manual_review_required", "downgraded_by_adversarial_review"}
        and source_verdict in {"", "review_manual", "continue"}
        and source_setup_effect in {"", "neutral", "strengthens", "mixed"}
    ):
        return (
            "WATCH",
            {
                **common,
                "mapping_reason": (
                    "Signal-refresh Manual Review wraps positive or incomplete event-policy evidence; recover it as review-only WATCH pressure."
                ),
                "action_policy_effect": "watch_only_no_buy_authority",
                "authority_scope": "watchlist_pressure_only",
            },
        )
    if policy_class in EVENT_POLICY_LOW_ACTION_CLASSES and action_status in {"manual_review_required", "low_information_downgraded", ""}:
        return (
            None,
            {
                **common,
                "drop_manual_review_candidate": True,
                "mapping_reason": (
                    "Low-actionability event-policy evidence is kept in signal-refresh/source traces but does not create an Action Queue Manual Review candidate."
                ),
                "action_policy_effect": "drop_low_actionability_review_candidate_no_broker_authority",
                "authority_scope": "review_input_only",
            },
        )
    if effect_type == "evidence_only" and action_status in {"manual_review_required", ""}:
        return (
            None,
            {
                **common,
                "drop_manual_review_candidate": True,
                "mapping_reason": (
                    "Evidence-only event-policy refresh is not actionable enough to create a Manual Review candidate."
                ),
                "action_policy_effect": "drop_evidence_only_review_candidate_no_broker_authority",
                "authority_scope": "review_input_only",
            },
        )
    return None, common


def build_signal_refresh_action_candidates(
    *,
    asof_date: pd.Timestamp,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    if setup_ids and SIGNAL_REFRESH_SETUP_ID not in {str(value).strip().upper() for value in setup_ids}:
        return []
    signals = load_signal_refresh_actions(asof_date=asof_date, symbols=symbols)
    if signals.empty:
        return []
    rows: list[dict[str, Any]] = []
    for _, row in signals.iterrows():
        mapped_action, mapping_context = _map_signal_refresh_action(row)
        if not mapped_action:
            continue
        confidence = _num(row.get("confidence"))
        invest_score_pct = None if confidence is None else max(0.0, min(100.0, confidence * 100.0 if confidence <= 1.0 else confidence))
        payload = _parse_jsonish(row.get("action_payload_json"), {})
        if not isinstance(payload, dict):
            payload = {}
        rows.append(
            _build_record(
                asof_date=asof_date,
                published_on=row.get("refreshed_at") or row.get("asof_date"),
                setup_id=SIGNAL_REFRESH_SETUP_ID,
                symbol=row.get("symbol"),
                unique_id=row.get("unique_id") or row.get("trace_id"),
                action_code=mapped_action,
                action_source="signal_refresh",
                source_action=row.get("signal_action"),
                transaction_type=None,
                execution_mode="review_only",
                invest_score_pct=invest_score_pct,
                action_reason=row.get("action_reason") or row.get("effect_summary"),
                action_detail=row.get("effect_type") or row.get("reason"),
                raw_context={
                    "signal_refresh": {
                        **row.to_dict(),
                        "action_payload": payload,
                    },
                    "signal_refresh_action_mapping": mapping_context,
                    "signal_refresh_bridge_note": (
                        "Fast watcher/signal-refresh rows are review input only. They can surface WATCH, "
                        "de-risk review, stop-review, or manual-review candidates, but never broker-executable trades."
                    ),
                },
            )
        )
    return rows


def build_action_recommendations(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
    progress: bool | None = None,
) -> pd.DataFrame:
    monitor_date = _normalize_asof_date(asof_date)
    candidates: list[dict[str, Any]] = []
    progress_enabled = ACTION_RECOMMENDER_PROGRESS_LOG_ENABLED if progress is None else bool(progress)
    run_started = time.monotonic()

    def _progress(stage: str, status: str, *, rows: int | None = None, extra: str | None = None) -> None:
        if not progress_enabled:
            return
        elapsed = time.monotonic() - run_started
        parts = [f"[advisory.action_recommender] stage={stage}", status, f"elapsed={elapsed:.2f}s"]
        if rows is not None:
            parts.append(f"rows={int(rows)}")
        if extra:
            parts.append(str(extra))
        print(" ".join(parts), file=sys.stderr, flush=True)

    def _count_rows(value: Any) -> int:
        if isinstance(value, pd.DataFrame):
            return int(len(value))
        if isinstance(value, list):
            return int(len(value))
        return 0

    _progress("start", "running", extra=f"asof={monitor_date.date()} symbols={0 if not symbols else len(symbols)} setups={0 if not setup_ids else len(setup_ids)}")
    _progress("portfolio", "start")
    portfolio_df = load_portfolio_actions(asof_date=monitor_date, symbols=symbols, setup_ids=setup_ids)
    _progress("portfolio", "done", rows=len(portfolio_df), extra=f"candidates={len(candidates)}")
    for _, row in portfolio_df.iterrows():
        candidates.append(
            _build_record(
                asof_date=monitor_date,
                published_on=row.get("published_on"),
                setup_id=row.get("setup_id"),
                symbol=row.get("symbol"),
                unique_id=row.get("unique_id"),
                action_code="BUY",
                action_source="portfolio",
                source_action=row.get("portfolio_status"),
                transaction_type="BUY",
                execution_mode="broker_order",
                approved_allocation_inr=row.get("approved_allocation_inr"),
                stop_price=row.get("stop_price"),
                invalidation_price=row.get("invalidation_price"),
                recommended_target_price=row.get("target_price"),
                expected_horizon_days=row.get("expected_horizon_days"),
                invest_score_pct=row.get("invest_score_pct"),
                action_reason=row.get("portfolio_reason"),
                action_detail=row.get("execution_notes"),
                raw_context=row.to_dict(),
            )
        )

    _progress("lifecycle", "start")
    lifecycle_df = load_lifecycle_actions(asof_date=monitor_date, symbols=symbols, setup_ids=setup_ids)
    _progress("lifecycle", "done", rows=len(lifecycle_df), extra=f"candidates={len(candidates)}")
    for _, row in lifecycle_df.iterrows():
        if str(row.get("position_status") or "").lower() != "open":
            continue
        candidates.append(
            _build_record(
                asof_date=monitor_date,
                published_on=row.get("published_on"),
                setup_id=row.get("setup_id"),
                symbol=row.get("symbol"),
                unique_id=row.get("unique_id"),
                action_code="HOLD",
                action_source="lifecycle",
                source_action=row.get("next_action"),
                transaction_type=None,
                execution_mode="review_only",
                reference_price=row.get("current_price"),
                stop_price=row.get("stop_price"),
                invalidation_price=row.get("invalidation_price"),
                action_reason=row.get("lifecycle_reason"),
                action_detail=row.get("next_action_reason"),
                raw_context=row.to_dict(),
            )
        )

    _progress("rebalance", "start")
    rebalance_df = load_rebalance_actions(asof_date=monitor_date, symbols=symbols, setup_ids=setup_ids)
    _progress("rebalance", "done", rows=len(rebalance_df), extra=f"candidates={len(candidates)}")
    action_map = {
        "exit_invalidation": ("SELL", "SELL"),
        "exit_stop": ("SELL", "SELL"),
        "exit_emergency": ("SELL", "SELL"),
        "exit_technical_failure": ("SELL", "SELL"),
        "exit_time_stop": ("SELL", "SELL"),
        "trim_winner": ("PARTIAL_SELL", "SELL"),
        "add_on_pullback": ("BUY_MORE", "BUY"),
        "tighten_stop": ("TIGHTEN_STOP", None),
        "review_manual": ("MANUAL_REVIEW", None),
        "review_stale": ("MANUAL_REVIEW", None),
        "review_horizon": ("MANUAL_REVIEW", None),
        "review_target": ("MANUAL_REVIEW", None),
    }
    for _, row in rebalance_df.iterrows():
        source_action = str(row.get("suggested_action") or "").strip().lower()
        mapped = action_map.get(source_action, ("MANUAL_REVIEW", None))
        candidates.append(
            _build_record(
                asof_date=monitor_date,
                published_on=row.get("published_on"),
                setup_id=row.get("setup_id"),
                symbol=row.get("symbol"),
                unique_id=row.get("unique_id"),
                action_code=mapped[0],
                action_source="rebalance",
                source_action=row.get("suggested_action"),
                transaction_type=mapped[1],
                execution_mode=row.get("execution_mode") or ("broker_order" if mapped[1] else "review_only"),
                action_fraction=row.get("action_fraction"),
                reference_price=row.get("reference_price"),
                stop_price=row.get("stop_price"),
                invalidation_price=row.get("invalidation_price"),
                recommended_stop_price=row.get("recommended_stop_price"),
                recommended_target_price=row.get("recommended_target_price"),
                expected_horizon_days=row.get("expected_horizon_days"),
                action_reason=row.get("action_reason"),
                action_detail=row.get("suggested_action"),
                raw_context=row.to_dict(),
            )
        )

    _progress("watchlist", "start")
    watch_df = load_watch_actions(asof_date=monitor_date, symbols=symbols, setup_ids=setup_ids)
    _progress("watchlist", "done", rows=len(watch_df), extra=f"candidates={len(candidates)}")
    for _, row in watch_df.iterrows():
        candidates.append(
            _build_record(
                asof_date=monitor_date,
                published_on=row.get("published_on"),
                setup_id=row.get("setup_id"),
                symbol=row.get("symbol"),
                unique_id=None,
                action_code="WATCH",
                action_source="watchlist",
                source_action=row.get("current_state") or row.get("watch_status"),
                transaction_type=None,
                execution_mode="review_only",
                reference_price=row.get("attractive_price_low"),
                invalidation_price=row.get("invalidation_price"),
                action_reason=row.get("watch_reason_detail"),
                action_detail=row.get("candidate_state"),
                raw_context=row.to_dict(),
            )
        )

    _progress("event_policy", "start")
    event_policy_candidates = build_event_policy_action_candidates(
        asof_date=monitor_date,
        symbols=symbols,
        setup_ids=setup_ids,
    )
    candidates.extend(event_policy_candidates)
    _progress("event_policy", "done", rows=_count_rows(event_policy_candidates), extra=f"candidates={len(candidates)}")
    _progress("wait_signals", "start")
    wait_signal_candidates = build_matched_wait_signal_action_candidates(
        asof_date=monitor_date,
        symbols=symbols,
        setup_ids=setup_ids,
    )
    candidates.extend(wait_signal_candidates)
    _progress("wait_signals", "done", rows=_count_rows(wait_signal_candidates), extra=f"candidates={len(candidates)}")
    _progress("company_memory", "start")
    company_memory_candidates = build_company_memory_action_candidates(
        asof_date=monitor_date,
        symbols=symbols,
        setup_ids=setup_ids,
    )
    candidates.extend(company_memory_candidates)
    _progress("company_memory", "done", rows=_count_rows(company_memory_candidates), extra=f"candidates={len(candidates)}")
    _progress("signal_refresh", "start")
    signal_refresh_candidates = build_signal_refresh_action_candidates(
        asof_date=monitor_date,
        symbols=symbols,
        setup_ids=setup_ids,
    )
    candidates.extend(signal_refresh_candidates)
    _progress("signal_refresh", "done", rows=_count_rows(signal_refresh_candidates), extra=f"candidates={len(candidates)}")

    base_symbols = sorted(
        {
            str(row.get("symbol")).upper()
            for row in candidates
            if _text(row.get("symbol"))
        }
    )
    _progress("playbook", "start", extra=f"base_symbols={len(base_symbols)}")
    playbook_candidates = build_playbook_action_candidates(
        asof_date=monitor_date,
        base_symbols=base_symbols,
        symbols=symbols,
        setup_ids=setup_ids,
    )
    candidates.extend(playbook_candidates)
    _progress("playbook", "done", rows=_count_rows(playbook_candidates), extra=f"candidates={len(candidates)}")

    if not candidates:
        _progress("finish", "done", rows=0)
        empty = pd.DataFrame()
        empty.attrs["action_refresh_scope"] = {
            "full_date_refresh": not symbols and not setup_ids,
            "symbols": [str(value).strip().upper() for value in symbols or [] if str(value or "").strip()],
            "setup_ids": [str(value).strip().upper() for value in setup_ids or [] if str(value or "").strip()],
        }
        return empty

    _progress("boundary_enforcement", "start", rows=len(candidates))
    candidate_df = enforce_review_only_signal_boundaries(pd.DataFrame(candidates))
    _progress("boundary_enforcement", "done", rows=len(candidate_df))
    _progress("context_enrichment", "start", rows=len(candidate_df))
    candidate_df = enrich_action_candidate_context(candidate_df, asof_date=monitor_date, progress=_progress)
    _progress("context_enrichment", "done", rows=len(candidate_df))
    _progress("signal_quality_overlay_rules", "start", rows=len(candidate_df))
    candidate_df = apply_signal_quality_overlay_rule_adjustments(candidate_df)
    _progress("signal_quality_overlay_rules", "done", rows=len(candidate_df))
    _progress("market_context", "start", rows=len(candidate_df))
    candidate_df = apply_market_context_adjustments(candidate_df, asof_date=monitor_date)
    _progress("market_context", "done", rows=len(candidate_df))
    _progress("ranking", "start", rows=len(candidate_df))
    winners = rank_action_candidates(candidate_df)
    _progress("ranking", "done", rows=len(winners))
    _progress("reason_contracts", "start", rows=len(winners))
    winners = add_recommendation_reason_contracts(winners, candidate_df)
    _progress("reason_contracts", "done", rows=len(winners))
    _progress("identity_gates", "start", rows=len(winners))
    winners = apply_identity_resolution_gates(winners)
    _progress("identity_gates", "done", rows=len(winners), extra=f"changed={int(winners.attrs.get('identity_gate_changed_count') or 0)}")
    if int(winners.attrs.get("identity_gate_changed_count") or 0) > 0:
        _progress("reason_contracts_after_identity", "start", rows=len(winners))
        winners = add_recommendation_reason_contracts(winners, candidate_df)
        _progress("reason_contracts_after_identity", "done", rows=len(winners))
    _progress("manual_revision_pointers", "start", rows=len(winners))
    winners = add_manual_revision_pointers(winners, candidate_df)
    _progress("manual_revision_pointers", "done", rows=len(winners))
    winners = normalize_cli_authority_frame(winners)
    winners.attrs["all_action_candidates"] = candidate_df
    winners.attrs["action_refresh_scope"] = {
        "full_date_refresh": not symbols and not setup_ids,
        "symbols": [str(value).strip().upper() for value in symbols or [] if str(value or "").strip()],
        "setup_ids": [str(value).strip().upper() for value in setup_ids or [] if str(value or "").strip()],
    }
    _progress("finish", "done", rows=len(winners), extra=f"candidates={len(candidate_df)}")
    return winners


def rank_action_candidates(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    ranked = enforce_review_only_signal_boundaries(df)
    ranked = apply_conflict_rule_precedence(ranked)
    ranked["symbol"] = ranked["symbol"].astype("string").str.upper()
    ranked["published_on"] = pd.to_datetime(ranked["published_on"], utc=True, errors="coerce")
    ranked["action_priority"] = pd.to_numeric(ranked["action_priority"], errors="coerce").fillna(0).astype(int)
    if "_conflict_rule_precedence" not in ranked.columns:
        ranked["_conflict_rule_precedence"] = 0
    ranked["_conflict_rule_precedence"] = pd.to_numeric(ranked["_conflict_rule_precedence"], errors="coerce").fillna(0).astype(int)
    if "invest_score_pct" not in ranked.columns:
        ranked["invest_score_pct"] = pd.NA
    ranked["invest_score_pct"] = pd.to_numeric(ranked["invest_score_pct"], errors="coerce")
    ranked = ranked.sort_values(
        ["symbol", "_conflict_rule_precedence", "action_priority", "published_on", "invest_score_pct", "setup_id"],
        ascending=[True, False, False, False, False, True],
        kind="stable",
    )
    winners = ranked.drop_duplicates(subset=["asof_date", "symbol"], keep="first").reset_index(drop=True)
    return winners.drop(columns=[column for column in ["_conflict_rule_precedence", "_conflict_precedence_rule_id", "_conflict_precedence_reason"] if column in winners.columns])


def persist_action_recommendations(df: pd.DataFrame, *, trace_enabled: bool | None = None) -> None:
    started_at = time.monotonic()
    should_trace = ACTION_RECOMMENDER_PERSIST_TRACE_ENABLED if trace_enabled is None else bool(trace_enabled)
    _progress_log("persist", "start", rows=len(df), started_at=started_at)
    _progress_log("persist.ensure_table", "start", started_at=started_at)
    ensure_actions_table()
    _progress_log("persist.ensure_table", "done", started_at=started_at)
    if df.empty:
        _progress_log("persist", "done", rows=0, extra="empty=true", started_at=started_at)
        return
    action_refresh_scope = df.attrs.get("action_refresh_scope") if isinstance(df.attrs.get("action_refresh_scope"), dict) else {}
    full_date_refresh = bool(action_refresh_scope.get("full_date_refresh"))
    all_candidates = df.attrs.get("all_action_candidates")
    if not isinstance(all_candidates, pd.DataFrame) or all_candidates.empty:
        all_candidates = df
    asof_series = all_candidates["asof_date"] if "asof_date" in all_candidates.columns else pd.Series([], dtype=object)
    asof_values = pd.to_datetime(asof_series, utc=True, errors="coerce").dropna()
    enrich_asof = asof_values.max().normalize() if not asof_values.empty else _normalize_asof_date(None)
    _progress_log("persist.rebuild_candidates", "start", rows=len(all_candidates), started_at=started_at)
    all_candidates = enforce_review_only_signal_boundaries(all_candidates)
    all_candidates = enrich_action_candidate_context(all_candidates, asof_date=enrich_asof)
    all_candidates = apply_signal_quality_overlay_rule_adjustments(all_candidates)
    all_candidates = apply_market_context_adjustments(all_candidates, asof_date=enrich_asof)
    all_candidates = apply_conflict_rule_precedence(all_candidates)
    winners = rank_action_candidates(all_candidates)
    winners = _carry_forward_symbol_column(winners, df, "feature_freshness_json")
    _progress_log("persist.rebuild_candidates", "done", rows=len(winners), extra=f"candidates={len(all_candidates)}", started_at=started_at)
    existing_reason_columns = {"recommendation_reason_json", "reason_contract_status"}
    if not existing_reason_columns.issubset(set(winners.columns)) or winners["recommendation_reason_json"].isna().any():
        _progress_log("persist.reason_contracts", "start", rows=len(winners), started_at=started_at)
        winners = add_recommendation_reason_contracts(winners, all_candidates)
        _progress_log("persist.reason_contracts", "done", rows=len(winners), started_at=started_at)
    existing_pointer_columns = {
        "manual_revision_summary",
        "manual_revision_pointers_json",
        "manual_revision_prompt_id",
        "manual_revision_prompt_version",
        "manual_revision_prompt_schema_version",
        "manual_revision_model",
        "manual_revision_status",
    }
    if not existing_pointer_columns.issubset(set(winners.columns)) or winners["manual_revision_pointers_json"].isna().any():
        _progress_log("persist.manual_revision_pointers", "start", rows=len(winners), started_at=started_at)
        winners = add_manual_revision_pointers(winners, all_candidates)
        _progress_log("persist.manual_revision_pointers", "done", rows=len(winners), started_at=started_at)
    if _has_missing_text_values(winners, "feature_freshness_json"):
        if ACTION_RECOMMENDER_PERSIST_LIVE_FEATURE_FRESHNESS_ENABLED:
            _progress_log("persist.feature_freshness", "start", rows=len(winners), started_at=started_at)
            winners = add_feature_freshness_contracts(winners)
            _progress_log("persist.feature_freshness", "done", rows=len(winners), started_at=started_at)
        else:
            _progress_log("persist.feature_freshness", "deferred", rows=len(winners), started_at=started_at)
            winners = add_deferred_feature_freshness_contracts(winners)
    winners = apply_feature_freshness_gates(winners)
    feature_gate_changed_count = int(winners.attrs.get("feature_freshness_gate_changed_count") or 0)
    winners = apply_identity_resolution_gates(winners)
    identity_gate_changed_count = int(winners.attrs.get("identity_gate_changed_count") or 0)
    if feature_gate_changed_count > 0 or identity_gate_changed_count > 0:
        _progress_log(
            "persist.post_gate_reasons",
            "start",
            rows=len(winners),
            extra=f"feature_changed={feature_gate_changed_count} identity_changed={identity_gate_changed_count}",
            started_at=started_at,
        )
        winners = add_recommendation_reason_contracts(winners, all_candidates)
        winners = add_manual_revision_pointers(winners, all_candidates)
        _progress_log("persist.post_gate_reasons", "done", rows=len(winners), started_at=started_at)
    _progress_log("persist.conflicts", "start", rows=len(winners), started_at=started_at)
    conflicts = build_action_conflicts(all_candidates, winners)
    _progress_log("persist.conflicts", "done", rows=len(conflicts), started_at=started_at)
    out = winners.copy()
    out = out.drop(columns=[column for column in ["_conflict_rule_precedence", "_conflict_precedence_rule_id", "_conflict_precedence_reason"] if column in out.columns])
    out = normalize_cli_authority_frame(out)
    for column in [
        "action_fraction",
        "approved_allocation_inr",
        "reference_price",
        "stop_price",
        "invalidation_price",
        "recommended_stop_price",
        "recommended_target_price",
        "invest_score_pct",
    ]:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce")
    if "expected_horizon_days" in out.columns:
        out["expected_horizon_days"] = pd.to_numeric(out["expected_horizon_days"], errors="coerce").astype("Int64")
    for column in ["broker_execution_allowed", "full_advisory_required"]:
        if column in out.columns:
            out[column] = out[column].map(_boolish_or_none)
    for column in ["asof_date", "published_on", "load_ts"]:
        if column in out.columns:
            out[column] = pd.to_datetime(out[column], utc=True, errors="coerce")
    pairs = (
        out[["asof_date", "symbol"]]
        .dropna()
        .drop_duplicates()
        .to_dict(orient="records")
    )
    delete_asof_values = sorted(
        {
            pd.to_datetime(value, utc=True, errors="coerce").normalize()
            for value in out["asof_date"].dropna().tolist()
            if not pd.isna(pd.to_datetime(value, utc=True, errors="coerce"))
        }
    )

    def _delete_existing_action_recommendations() -> None:
        with db_session() as (_, cur):
            if full_date_refresh and delete_asof_values:
                for asof_value in delete_asof_values:
                    cur.execute(
                        f"DELETE FROM {TABLE_NAME} WHERE asof_date = %s",
                        (asof_value.to_pydatetime(),),
                    )
                return
            for item in pairs:
                cur.execute(
                    f"DELETE FROM {TABLE_NAME} WHERE asof_date = %s AND symbol = %s",
                    (
                        pd.to_datetime(item["asof_date"], utc=True, errors="coerce").to_pydatetime(),
                        str(item["symbol"]).upper(),
                    ),
                )

    execute_db_operation(
        _delete_existing_action_recommendations,
        operation_name="action_recommender:delete_existing_recommendations",
    )
    _progress_log("persist.delete_existing", "done", rows=len(pairs), started_at=started_at)
    _progress_log("persist.upsert", "start", rows=len(out), started_at=started_at)
    upsert_to_db(
        out,
        TABLE_NAME,
        unique_keys=["asof_date", "symbol"],
        timescaledb_column="asof_date",
    )
    _progress_log("persist.upsert", "done", rows=len(out), started_at=started_at)
    _progress_log("persist.action_conflicts", "start", rows=len(conflicts), started_at=started_at)
    persist_action_conflicts(conflicts)
    _progress_log("persist.action_conflicts", "done", rows=len(conflicts), started_at=started_at)
    if should_trace:
        _progress_log("persist.trace", "start", rows=len(winners), started_at=started_at)
        _trace_action_consolidation(all_candidates, winners, conflicts)
        _progress_log("persist.trace", "done", rows=len(winners), started_at=started_at)
    else:
        _progress_log("persist.trace", "skipped", rows=len(winners), started_at=started_at)
    _progress_log("persist", "done", rows=len(out), started_at=started_at)


def _augment_raw_context_source_handles(row: pd.Series) -> str:
    raw_context = _parse_jsonish(row.get("raw_context_json"), {})
    if not isinstance(raw_context, dict):
        raw_context = {}
    action_source = str(row.get("action_source") or "").strip().lower()
    unique_id = _text(row.get("unique_id"))
    if action_source == "event_policy":
        raw_context.setdefault("source_table", EVENT_POLICY_TABLE)
        if unique_id:
            raw_context.setdefault("source_key", unique_id)
    elif action_source.startswith("playbook"):
        existing_source_table = raw_context.get("source_table")
        if existing_source_table and not raw_context.get("playbook_source_table") and existing_source_table != ACTION_PLANS_TABLE:
            raw_context["playbook_source_table"] = existing_source_table
        existing_source_key = raw_context.get("source_key")
        if existing_source_key and not raw_context.get("playbook_source_key") and existing_source_key != unique_id:
            raw_context["playbook_source_key"] = existing_source_key
        raw_context["source_table"] = ACTION_PLANS_TABLE
        if unique_id:
            raw_context.setdefault("source_key", unique_id)
            raw_context.setdefault("playbook_source_key", unique_id)
    return json.dumps(raw_context, ensure_ascii=False, default=str, sort_keys=True)


def _load_action_recommendations_for_reason_repair(
    *,
    from_date: pd.Timestamp | None,
    to_date: pd.Timestamp | None,
    symbols: list[str] | None = None,
    limit: int = 1000,
) -> pd.DataFrame:
    if not table_exists(TABLE_NAME):
        return pd.DataFrame()
    available = table_columns(TABLE_NAME)
    required = {"asof_date", "symbol", "action_code", "action_source", "raw_context_json"}
    if not required.issubset(available):
        missing = sorted(required - available)
        record_local_fallback_event(
            module="advisory.action_recommender",
            fallback_type="action_reason_contract_repair_required_columns_missing",
            source=TABLE_NAME,
            severity="warn",
            reason="Action reason-contract repair could not load persisted rows because required columns are missing.",
            metadata={"missing_columns": missing},
        )
        return pd.DataFrame()
    wanted = [
        "asof_date",
        "published_on",
        "symbol",
        "setup_id",
        "unique_id",
        "action_code",
        "action_priority",
        "action_source",
        "source_action",
        "transaction_type",
        "execution_mode",
        "action_fraction",
        "approved_allocation_inr",
        "reference_price",
        "stop_price",
        "invalidation_price",
        "recommended_stop_price",
        "recommended_target_price",
        "expected_horizon_days",
        "invest_score_pct",
        "action_reason",
        "action_detail",
        "authority_scope",
        "portfolio_authority",
        "broker_execution_allowed",
        "full_advisory_required",
        "raw_context_json",
        "recommendation_reason_json",
        "reason_contract_status",
        "feature_freshness_json",
        "manual_revision_summary",
        "manual_revision_pointers_json",
        "load_ts",
    ]
    selected = [column for column in wanted if column in available]
    clauses = ["raw_context_json IS NOT NULL", "NULLIF(TRIM(symbol), '') IS NOT NULL"]
    params: dict[str, Any] = {"limit": max(1, int(limit))}
    if from_date is not None:
        clauses.append("asof_date >= %(from_date)s")
        params["from_date"] = from_date
    if to_date is not None:
        clauses.append("asof_date <= %(to_date)s")
        params["to_date"] = to_date
    normalized_symbols = [str(value).strip().upper() for value in symbols or [] if str(value or "").strip()]
    if normalized_symbols:
        clauses.append("UPPER(TRIM(symbol)) = ANY(%(symbols)s)")
        params["symbols"] = normalized_symbols
    return sql_to_df(
        f"""
        SELECT {", ".join(selected)}
        FROM {TABLE_NAME}
        WHERE {' AND '.join(clauses)}
        ORDER BY asof_date DESC, symbol
        LIMIT %(limit)s
        """,
        params=params,
        retries=4,
        statement_timeout_ms=0,
        chunksize=50000,
    )


def _build_reason_contract_repair_frame(rows: pd.DataFrame) -> pd.DataFrame:
    if rows.empty:
        return rows.copy()
    out = rows.copy()
    for column in ["asof_date", "published_on", "load_ts"]:
        if column in out.columns:
            out[column] = pd.to_datetime(out[column], utc=True, errors="coerce")
    if "action_priority" not in out.columns:
        out["action_priority"] = out["action_code"].astype("string").str.upper().map(ACTION_PRIORITY).fillna(0).astype(int)
    out["raw_context_json"] = out.apply(_augment_raw_context_source_handles, axis=1)
    asof_values = pd.to_datetime(out["asof_date"], utc=True, errors="coerce").dropna()
    enrich_asof = asof_values.max().normalize() if not asof_values.empty else _normalize_asof_date(None)
    enriched = enrich_action_candidate_context(out, asof_date=enrich_asof)
    enriched = apply_market_context_adjustments(enriched, asof_date=enrich_asof, annotate_only=True)
    contracts: list[str] = []
    statuses: list[str] = []
    for _, row in enriched.iterrows():
        contract = build_recommendation_reason_contract(row, enriched)
        contracts.append(json.dumps(contract, ensure_ascii=False, default=str, sort_keys=True))
        statuses.append(str(contract.get("status") or "unknown"))
    enriched["recommendation_reason_json"] = contracts
    enriched["reason_contract_status"] = statuses
    return enriched


def _multi_context_label_coverage(rows: pd.DataFrame) -> dict[str, Any]:
    dimensions = {
        "breadth": ("breadth_context",),
        "macro_stress": ("macro_stress_context",),
        "sector_symbol": ("sector_symbol_context",),
        "technical": ("technical_confirmation_context",),
        "context_overlay": ("event_context_overlay",),
    }
    label_counts: dict[str, dict[str, int]] = {key: {} for key in dimensions}
    missing_counts: dict[str, int] = {key: 0 for key in dimensions}
    row_count = int(len(rows))
    complete_row_count = 0
    for _, row in rows.iterrows():
        contract = _parse_jsonish(row.get("recommendation_reason_json"), {})
        if not isinstance(contract, dict):
            contract = {}
        raw_context = _parse_jsonish(row.get("raw_context_json"), {})
        if not isinstance(raw_context, dict):
            raw_context = {}
        evidence = contract.get("evidence") if isinstance(contract.get("evidence"), dict) else {}
        macro = evidence.get("macro_regime") if isinstance(evidence.get("macro_regime"), dict) else {}
        diagnostics = macro.get("multi_context_diagnostics") if isinstance(macro.get("multi_context_diagnostics"), dict) else {}
        if not diagnostics:
            adjustment = _parse_jsonish(raw_context.get("market_context_adjustment_json"), {})
            if isinstance(adjustment, dict):
                diagnostics = adjustment.get("multi_context_diagnostics") if isinstance(adjustment.get("multi_context_diagnostics"), dict) else {}
        row_missing = False
        for dimension, path in dimensions.items():
            node = diagnostics
            for key in path:
                node = node.get(key) if isinstance(node, dict) else None
            label = str(node.get("label") if isinstance(node, dict) else "").strip() or "missing"
            label_counts[dimension][label] = label_counts[dimension].get(label, 0) + 1
            if label == "missing":
                missing_counts[dimension] += 1
                row_missing = True
        if not row_missing:
            complete_row_count += 1
    return {
        "row_count": row_count,
        "label_counts": label_counts,
        "missing_label_counts": {key: value for key, value in missing_counts.items() if value},
        "missing_label_total": int(sum(missing_counts.values())),
        "complete_row_count": int(complete_row_count),
    }


def repair_action_reason_contracts(
    *,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    limit: int = 1000,
    dry_run: bool = True,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    rows = _load_action_recommendations_for_reason_repair(
        from_date=from_date,
        to_date=to_date,
        symbols=symbols,
        limit=max(1, int(limit)),
    )
    before_coverage = _multi_context_label_coverage(rows)
    repaired = _build_reason_contract_repair_frame(rows)
    after_coverage = _multi_context_label_coverage(repaired)
    if not dry_run and not repaired.empty:
        update_rows = repaired[
            [
                "asof_date",
                "symbol",
                "raw_context_json",
                "recommendation_reason_json",
                "reason_contract_status",
            ]
        ].copy()

        def _update_reason_contracts() -> None:
            with db_session() as (_, cur):
                for _, item in update_rows.iterrows():
                    cur.execute(
                        f"""
                        UPDATE {TABLE_NAME}
                        SET raw_context_json = %s,
                            recommendation_reason_json = %s,
                            reason_contract_status = %s
                        WHERE asof_date = %s
                          AND UPPER(TRIM(symbol)) = %s
                        """,
                        (
                            item.get("raw_context_json"),
                            item.get("recommendation_reason_json"),
                            item.get("reason_contract_status"),
                            pd.to_datetime(item.get("asof_date"), utc=True, errors="coerce").to_pydatetime(),
                            str(item.get("symbol") or "").strip().upper(),
                        ),
                    )

        execute_db_operation(
            _update_reason_contracts,
            operation_name="action_recommender:repair_reason_contracts",
        )
    source_handle_counts: dict[str, int] = {}
    for value in repaired.get("recommendation_reason_json", []):
        contract = _parse_jsonish(value, {})
        if not isinstance(contract, dict):
            continue
        evidence = contract.get("evidence")
        if not isinstance(evidence, dict):
            continue
        for section in ["event_policy", "playbook", "company_memory"]:
            node = evidence.get(section)
            if isinstance(node, dict) and (node.get("source_table") or node.get("source_key") or node.get("playbook_source_key")):
                source_handle_counts[section] = source_handle_counts.get(section, 0) + 1
    meta = {
        "status": "ok",
        "table": TABLE_NAME,
        "loaded_rows": int(len(rows)),
        "repaired_rows": int(len(repaired)),
        "persisted": not bool(dry_run),
        "source_handle_counts": source_handle_counts,
        "multi_context_label_coverage_before": before_coverage,
        "multi_context_label_coverage_after": after_coverage,
        "multi_context_missing_label_delta": int(before_coverage.get("missing_label_total") or 0)
        - int(after_coverage.get("missing_label_total") or 0),
        "policy_effect": "reason_contract_metadata_repair_only_no_action_or_ranking_change",
        "broker_execution_allowed": False,
    }
    return repaired, meta


def _trace_action_consolidation(all_candidates: pd.DataFrame, winners: pd.DataFrame, conflicts: pd.DataFrame) -> None:
    if winners.empty:
        return
    candidate_groups: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    if not all_candidates.empty:
        tmp = all_candidates.copy()
        tmp["asof_date"] = pd.to_datetime(tmp["asof_date"], utc=True, errors="coerce")
        for key, group in tmp.groupby(["asof_date", "symbol"], dropna=False):
            candidate_groups[key] = group.sort_values("action_priority", ascending=False).head(10).to_dict(orient="records")
    conflict_counts: dict[tuple[Any, Any], int] = {}
    if not conflicts.empty:
        tmp_conflicts = conflicts.copy()
        tmp_conflicts["asof_date"] = pd.to_datetime(tmp_conflicts["asof_date"], utc=True, errors="coerce")
        conflict_counts = tmp_conflicts.groupby(["asof_date", "symbol"], dropna=False).size().to_dict()
    for _, row in winners.iterrows():
        asof_date = pd.to_datetime(row.get("asof_date"), utc=True, errors="coerce")
        key = (asof_date, row.get("symbol"))
        candidate_rows = candidate_groups.get(key, [])
        trace_id = safe_trace_call(
            append_trace,
            asof_date=row.get("asof_date"),
            symbol=row.get("symbol"),
            unique_id=row.get("unique_id"),
            setup_id=row.get("setup_id"),
            trigger_type="action_consolidation",
            final_action=row.get("action_code"),
            final_reason=row.get("action_reason"),
            source_table=TABLE_NAME,
            source_key=f"{row.get('asof_date')}:{row.get('symbol')}",
            payload={
                "action_source": row.get("action_source"),
                "action_priority": row.get("action_priority"),
                "conflict_count": int(conflict_counts.get(key, 0)),
                "candidate_count": len(candidate_rows),
                "reason_contract_status": row.get("reason_contract_status"),
                "recommendation_reason": _parse_jsonish(row.get("recommendation_reason_json"), {}),
                "feature_freshness": _parse_jsonish(row.get("feature_freshness_json"), {}),
                "manual_revision_summary": row.get("manual_revision_summary"),
                "manual_revision_pointers": _parse_jsonish(row.get("manual_revision_pointers_json"), {}),
                "manual_revision_status": row.get("manual_revision_status"),
            },
        )
        if trace_id:
            if str(row.get("action_source") or "").startswith("playbook"):
                safe_trace_call(
                    append_trace_step,
                    trace_id=trace_id,
                    step_idx=45,
                    stage="playbook_action_plan",
                    status="review_overlay",
                    reason=row.get("action_reason"),
                    input_payload=row.get("raw_context_json"),
                    output_payload=row.to_dict(),
                    payload={
                        "playbook_id": row.get("setup_id"),
                        "source_key": row.get("unique_id"),
                        "source_action": row.get("source_action"),
                        "mapped_action": row.get("action_code"),
                        "production_allowed": True,
                        "execution_mode": row.get("execution_mode"),
                        "confidence": row.get("invest_score_pct"),
                        "operator_summary": row.get("action_detail"),
                        "review_boundary": "Playbook candidates are review/risk overlays only, not direct broker orders.",
                        "reason_contract_status": row.get("reason_contract_status"),
                        "recommendation_reason": _parse_jsonish(row.get("recommendation_reason_json"), {}),
                        "feature_freshness": _parse_jsonish(row.get("feature_freshness_json"), {}),
                        "manual_revision_summary": row.get("manual_revision_summary"),
                        "manual_revision_pointers": _parse_jsonish(row.get("manual_revision_pointers_json"), {}),
                    },
                )
            safe_trace_call(
                append_trace_step,
                trace_id=trace_id,
                step_idx=50,
                stage="action_consolidation",
                status="completed",
                reason=row.get("action_reason"),
                input_payload=candidate_rows,
                output_payload=row.to_dict(),
                payload={
                    "winner_action": row.get("action_code"),
                    "winner_source": row.get("action_source"),
                    "candidate_count": len(candidate_rows),
                    "conflict_count": int(conflict_counts.get(key, 0)),
                    "reason_contract_status": row.get("reason_contract_status"),
                    "recommendation_reason": _parse_jsonish(row.get("recommendation_reason_json"), {}),
                    "feature_freshness": _parse_jsonish(row.get("feature_freshness_json"), {}),
                    "manual_revision_summary": row.get("manual_revision_summary"),
                    "manual_revision_pointers": _parse_jsonish(row.get("manual_revision_pointers_json"), {}),
                    "manual_revision_model": row.get("manual_revision_model"),
                    "manual_revision_status": row.get("manual_revision_status"),
                },
            )


def _truncate_text(value: Any, *, max_chars: int = 240) -> Any:
    text = _text(value)
    if text is None:
        return None
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3]}..."


def _cli_sample(df: pd.DataFrame, *, sample_limit: int = 10, full_sample: bool = False) -> list[dict[str, Any]]:
    if df.empty or int(sample_limit) <= 0:
        return []
    sample = df.head(max(1, int(sample_limit))).copy()
    if not full_sample:
        sample = sample[[column for column in CLI_COMPACT_SAMPLE_COLUMNS if column in sample.columns]].copy()
        for column in ["action_reason", "action_detail", "manual_revision_summary"]:
            if column in sample.columns:
                sample[column] = sample[column].map(_truncate_text)
    return to_display_value(sample)


def _series_value_counts(df: pd.DataFrame, column: str) -> dict[str, int]:
    if df.empty or column not in df.columns:
        return {}
    series = df[column].astype("string").fillna("missing").str.strip()
    series = series.mask(series.eq(""), "missing")
    return {str(key): int(value) for key, value in series.value_counts(dropna=False).to_dict().items()}


def _authority_defaults_for_cli_row(row: pd.Series) -> dict[str, Any]:
    raw_context = _parse_jsonish(row.get("raw_context_json"), {})
    if not isinstance(raw_context, dict):
        raw_context = {}
    authority_contract = raw_context.get("authority_contract")
    if not isinstance(authority_contract, dict):
        authority_contract = raw_context.get("event_policy_action_mapping")
    if not isinstance(authority_contract, dict):
        authority_contract = raw_context.get("playbook_action_mapping")
    if not isinstance(authority_contract, dict):
        authority_contract = raw_context.get("company_memory_action_mapping")
    if not isinstance(authority_contract, dict):
        authority_contract = {}

    source = str(row.get("action_source") or "").strip().lower()
    if source in {"signal_refresh", "manual_review_wait_signal"}:
        inferred = {
            "authority_scope": "review_input_only",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "full_advisory_required": True,
        }
    elif source in {"event_policy", "company_memory"} or source.startswith("playbook"):
        inferred = {
            "authority_scope": authority_contract.get("authority_scope") or "review_input_only",
            "portfolio_authority": authority_contract.get("portfolio_authority") or "none",
            "broker_execution_allowed": False,
            "full_advisory_required": True,
        }
    elif source == "watchlist":
        inferred = {
            "authority_scope": "watchlist_pressure_only",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "full_advisory_required": True,
        }
    elif source in {"portfolio", "rebalance", "lifecycle"}:
        inferred = {
            "authority_scope": "advisory_reconciliation_output",
            "portfolio_authority": "advisory_reconciliation",
            "broker_execution_allowed": False,
            "full_advisory_required": False,
        }
    else:
        inferred = {
            "authority_scope": authority_contract.get("authority_scope") or None,
            "portfolio_authority": authority_contract.get("portfolio_authority") or None,
            "broker_execution_allowed": _boolish_or_none(authority_contract.get("broker_execution_allowed")),
            "full_advisory_required": _boolish_or_none(authority_contract.get("full_advisory_required")),
        }

    broker_value = _boolish_or_none(row.get("broker_execution_allowed")) if "broker_execution_allowed" in row.index else None
    full_advisory_value = _boolish_or_none(row.get("full_advisory_required")) if "full_advisory_required" in row.index else None
    return {
        "authority_scope": _text(row.get("authority_scope")) or inferred.get("authority_scope"),
        "portfolio_authority": _text(row.get("portfolio_authority")) or inferred.get("portfolio_authority"),
        "broker_execution_allowed": broker_value if broker_value is not None else inferred.get("broker_execution_allowed"),
        "full_advisory_required": full_advisory_value if full_advisory_value is not None else inferred.get("full_advisory_required"),
    }


def normalize_cli_authority_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Fill reporting-only authority columns without changing action ranking or persistence."""
    if df.empty:
        return df.copy()
    out = df.copy()
    for column in ["authority_scope", "portfolio_authority", "broker_execution_allowed", "full_advisory_required"]:
        if column not in out.columns:
            out[column] = None
    for idx, row in out.iterrows():
        defaults = _authority_defaults_for_cli_row(row)
        for column, value in defaults.items():
            if value is None:
                continue
            existing = out.at[idx, column]
            if column in {"broker_execution_allowed", "full_advisory_required"}:
                if _boolish_or_none(existing) is None:
                    out.at[idx, column] = bool(value)
            elif _text(existing) is None:
                out.at[idx, column] = value
    return out


def summarize_cli_authority(df: pd.DataFrame) -> dict[str, Any]:
    """Summarize execution authority so fast-refresh output is not mistaken for broker intent."""
    row_count = int(len(df))
    if row_count <= 0:
        return {
            "row_count": 0,
            "review_only_rows": 0,
            "broker_order_candidate_rows": 0,
            "broker_capable_action_rows": 0,
            "broker_capable_review_only_rows": 0,
            "broker_execution_allowed_rows": 0,
            "broker_execution_allowed_unknown_rows": 0,
            "full_advisory_required_rows": 0,
            "full_advisory_required_unknown_rows": 0,
            "authority_scope_counts": {},
            "portfolio_authority_counts": {},
            "execution_mode_counts": {},
            "policy_boundary": "no_rows",
        }

    action_series = df["action_code"].astype("string").str.upper() if "action_code" in df.columns else pd.Series("", index=df.index)
    execution_series = df["execution_mode"].astype("string").str.lower() if "execution_mode" in df.columns else pd.Series("", index=df.index)
    if "broker_execution_allowed" in df.columns:
        broker_allowed_raw = df["broker_execution_allowed"].map(_boolish_or_none)
        broker_allowed = broker_allowed_raw.fillna(False)
        broker_allowed_unknown_rows = int(broker_allowed_raw.isna().sum())
    else:
        broker_allowed = pd.Series(False, index=df.index)
        broker_allowed_unknown_rows = row_count
    if "full_advisory_required" in df.columns:
        full_advisory_required_raw = df["full_advisory_required"].map(_boolish_or_none)
        full_advisory_required = full_advisory_required_raw.fillna(False)
        full_advisory_required_unknown_rows = int(full_advisory_required_raw.isna().sum())
    else:
        full_advisory_required = pd.Series(False, index=df.index)
        full_advisory_required_unknown_rows = row_count
    broker_capable = action_series.isin(BROKER_CAPABLE_ACTIONS)
    review_only = execution_series.isin({"review_only", "no_broker_execution"})
    broker_order = execution_series.eq("broker_order")
    broker_allowed_count = int(broker_allowed.sum())
    broker_order_count = int(broker_order.sum())
    return {
        "row_count": row_count,
        "review_only_rows": int(review_only.sum()),
        "broker_order_candidate_rows": broker_order_count,
        "broker_capable_action_rows": int(broker_capable.sum()),
        "broker_capable_review_only_rows": int((broker_capable & review_only).sum()),
        "broker_execution_allowed_rows": broker_allowed_count,
        "broker_execution_allowed_unknown_rows": broker_allowed_unknown_rows,
        "full_advisory_required_rows": int(full_advisory_required.sum()),
        "full_advisory_required_unknown_rows": full_advisory_required_unknown_rows,
        "authority_scope_counts": _series_value_counts(df, "authority_scope"),
        "portfolio_authority_counts": _series_value_counts(df, "portfolio_authority"),
        "execution_mode_counts": _series_value_counts(df, "execution_mode"),
        "policy_boundary": (
            "contains_broker_execution_candidates"
            if broker_allowed_count > 0
            else "cli_no_broker_execution_contains_broker_order_candidates"
            if broker_order_count > 0
            else "review_only_no_broker"
            if broker_allowed_unknown_rows == 0
            else "cli_no_broker_execution_unknown_broker_authority_fields"
        ),
    }


def _authority_inference_audit(df: pd.DataFrame) -> dict[str, Any]:
    """Audit which reporting authority fields were inferred (filled) vs explicit on the source rows.

    Mirrors normalize_cli_authority_frame's gap-fill: a field counts as inferred when the row
    lacked an explicit value and a non-None default was applied. Audit-only; it changes no
    ranking, portfolio, or broker state and only makes silent authority gap-filling visible.
    """
    fields = ["authority_scope", "portfolio_authority", "broker_execution_allowed", "full_advisory_required"]
    row_count = int(len(df))
    audit: dict[str, Any] = {
        "row_count": row_count,
        "inferred_authority_rows": 0,
        "explicit_authority_rows": 0,
        "inferred_field_counts": {field: 0 for field in fields},
        "inferred_rows_by_source": {},
    }
    if row_count <= 0:
        return audit
    bool_fields = {"broker_execution_allowed", "full_advisory_required"}
    for _, row in df.iterrows():
        defaults = _authority_defaults_for_cli_row(row)
        any_inferred = False
        all_explicit = True
        for field in fields:
            if field in bool_fields:
                explicit = field in df.columns and _boolish_or_none(row.get(field)) is not None
            else:
                explicit = field in df.columns and _text(row.get(field)) is not None
            if explicit:
                continue
            all_explicit = False
            if defaults.get(field) is not None:
                any_inferred = True
                audit["inferred_field_counts"][field] += 1
        if any_inferred:
            audit["inferred_authority_rows"] += 1
            source = _text(row.get("action_source")) or "unknown"
            audit["inferred_rows_by_source"][source] = audit["inferred_rows_by_source"].get(source, 0) + 1
        if all_explicit:
            audit["explicit_authority_rows"] += 1
    return audit


def build_cli_result(
    df: pd.DataFrame,
    *,
    dry_run: bool,
    sample_limit: int = 10,
    full_sample: bool = False,
) -> dict[str, Any]:
    display_df = normalize_cli_authority_frame(df)
    authority_summary = summarize_cli_authority(display_df)
    authority_summary["authority_inference"] = _authority_inference_audit(df)
    return {
        "status": "ok",
        "table": TABLE_NAME,
        "row_count": int(len(display_df)),
        "action_counts": display_df["action_code"].value_counts(dropna=False).to_dict() if not display_df.empty and "action_code" in display_df.columns else {},
        "action_source_counts": display_df["action_source"].value_counts(dropna=False).to_dict() if not display_df.empty and "action_source" in display_df.columns else {},
        "authority_summary": authority_summary,
        "sample": _cli_sample(display_df, sample_limit=sample_limit, full_sample=full_sample),
        "sample_limit": int(sample_limit),
        "sample_mode": "full" if full_sample else "compact",
        "omitted_sample_fields": [] if full_sample else ["raw_context_json", "recommendation_reason_json", "feature_freshness_json", "manual_revision_pointers_json"],
        "dry_run": bool(dry_run),
        "policy_boundary": {
            "portfolio_authority": "none_from_cli_output",
            "broker_execution_allowed": False,
            "notes": "This CLI builds/persists consolidated advisory rows only. Broker execution remains controlled by execution safety gates.",
        },
    }


def record_action_refresh_sync_state(df: pd.DataFrame, *, asof_date: pd.Timestamp | None, source: str = "action_recommender_cli") -> dict[str, Any]:
    started_at = time.monotonic()
    _progress_log("action_refresh_sync", "start", rows=len(df), started_at=started_at)
    now = pd.Timestamp.utcnow()
    normalized_asof = pd.to_datetime(asof_date, utc=True, errors="coerce") if asof_date is not None else pd.NaT
    result = {
        "status": "ok",
        "source": source,
        "asof_date": None if pd.isna(normalized_asof) else normalized_asof.normalize().isoformat(),
        "recommendation_rows": int(len(df)),
        "symbol_count": int(df["symbol"].nunique()) if not df.empty and "symbol" in df.columns else 0,
        "action_counts": df["action_code"].value_counts().to_dict() if not df.empty and "action_code" in df.columns else {},
        "full_advisory_required": True,
        "broker_execution_allowed": False,
        "portfolio_authority": "none",
        "authority_scope": "review_input_only",
    }
    try:
        _progress_log("action_refresh_sync.persist_state", "start", started_at=started_at)
        persist_sync_state(
            source_name=ACTION_REFRESH_SYNC_SOURCE,
            last_success_at=now,
            last_item_ts=now,
            cursor_value=now.isoformat(),
            state=result,
            status="ok",
        )
        _progress_log("action_refresh_sync.persist_state", "done", started_at=started_at)
        _progress_log("action_refresh_sync.publish_bus", "start", started_at=started_at)
        publish_bus_message("stockey:continuous_watch:action_refresh", {"published_at": now, **result})
        _progress_log("action_refresh_sync.publish_bus", "done", started_at=started_at)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.action_recommender",
            fallback_type="action_recommender_action_refresh_sync_state_failed",
            source=ACTION_REFRESH_SYNC_SOURCE,
            severity="warn",
            reason="Action recommender persisted recommendations, but could not update bounded Action Queue refresh sync state.",
            error=exc,
            metadata={"source": source, "recommendation_rows": int(len(df))},
        )
        result.update({"status": "sync_state_failed", "error_type": exc.__class__.__name__, "error": str(exc)[:300]})
    _progress_log("action_refresh_sync", "done", rows=len(df), extra=f"status={result.get('status')}", started_at=started_at)
    return result


def format_cli_text(result: dict[str, Any]) -> str:
    lines = [
        "Action Recommender",
        f"Status: {result.get('status')}",
        f"Table: {result.get('table')}",
        f"Rows: {result.get('row_count', 0)}",
        f"Dry run: {bool(result.get('dry_run'))}",
        f"Sample mode: {result.get('sample_mode')}",
        f"Action counts: {result.get('action_counts', {})}",
        f"Action source counts: {result.get('action_source_counts', {})}",
    ]
    authority = result.get("authority_summary") if isinstance(result.get("authority_summary"), dict) else {}
    if authority:
        lines.append(
            "Authority: "
            f"boundary={authority.get('policy_boundary')} "
            f"review_only={authority.get('review_only_rows', 0)} "
            f"broker_order_candidates={authority.get('broker_order_candidate_rows', 0)} "
            f"broker_allowed={authority.get('broker_execution_allowed_rows', 0)} "
            f"broker_allowed_unknown={authority.get('broker_execution_allowed_unknown_rows', 0)} "
            f"broker_capable_review_only={authority.get('broker_capable_review_only_rows', 0)} "
            f"full_advisory_required={authority.get('full_advisory_required_rows', 0)} "
            f"full_advisory_unknown={authority.get('full_advisory_required_unknown_rows', 0)}"
        )
        if authority.get("authority_scope_counts"):
            lines.append(f"Authority scopes: {authority.get('authority_scope_counts')}")
        if authority.get("portfolio_authority_counts"):
            lines.append(f"Portfolio authority: {authority.get('portfolio_authority_counts')}")
        if authority.get("execution_mode_counts"):
            lines.append(f"Execution modes: {authority.get('execution_mode_counts')}")
        inference = authority.get("authority_inference") if isinstance(authority.get("authority_inference"), dict) else {}
        if inference:
            lines.append(
                "Authority inference: "
                f"inferred_rows={inference.get('inferred_authority_rows', 0)} "
                f"explicit_rows={inference.get('explicit_authority_rows', 0)} "
                f"inferred_fields={inference.get('inferred_field_counts', {})}"
            )
    sync_state = result.get("action_refresh_sync") if isinstance(result.get("action_refresh_sync"), dict) else {}
    if sync_state:
        lines.append(
            "Action refresh sync: "
            f"status={sync_state.get('status')} "
            f"source={sync_state.get('source')} "
            f"rows={sync_state.get('recommendation_rows')} "
            f"symbols={sync_state.get('symbol_count')}"
        )
    sample = result.get("sample") if isinstance(result.get("sample"), list) else []
    if sample:
        lines.extend(["", "Sample:"])
        for row in sample[: int(result.get("sample_limit") or 10)]:
            if not isinstance(row, dict):
                continue
            lines.append(
                "- "
                f"{row.get('symbol')}: {row.get('action_code')} "
                f"from {row.get('action_source')} "
                f"setup={row.get('setup_id')} "
                f"reason={row.get('action_reason')}"
            )
    if result.get("omitted_sample_fields"):
        lines.append(f"Omitted sample fields: {', '.join(result.get('omitted_sample_fields') or [])}")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build one consolidated action recommendation per symbol for operator and execution flows.")
    parser.add_argument("--date", type=parse_datetime_arg, help="Asof date in YYYY-MM-DD")
    parser.add_argument("--from-date", type=parse_datetime_arg, help="Start date for repair modes.")
    parser.add_argument("--to-date", type=parse_datetime_arg, help="End date for repair modes.")
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument("--setup", dest="setup_ids", nargs="*")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--repair-reason-contracts", action="store_true", help="Repair persisted recommendation reason contracts without changing actions, ranking, or broker behavior.")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--format", choices=["json", "text"], default="json")
    parser.add_argument("--sample-limit", type=int, default=10)
    parser.add_argument("--full-sample", action="store_true", help="Include full raw JSON fields in the CLI sample. Default output is compact/log-safe.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    asof_date = pd.Timestamp(args.date, tz="UTC") if args.date else None
    if args.repair_reason_contracts:
        repaired, meta = repair_action_reason_contracts(
            from_date=args.from_date or asof_date,
            to_date=args.to_date or asof_date,
            symbols=args.symbols,
            limit=max(1, int(args.limit)),
            dry_run=bool(args.dry_run),
        )
        result = {
            **meta,
            "sample": _cli_sample(repaired, sample_limit=max(0, int(args.sample_limit)), full_sample=bool(args.full_sample)),
            "sample_limit": int(args.sample_limit),
            "sample_mode": "full" if args.full_sample else "compact",
        }
        if args.format == "text":
            print(
                f"status={result['status']} loaded={result['loaded_rows']} repaired={result['repaired_rows']} "
                f"persisted={result['persisted']} source_handles={result['source_handle_counts']} "
                f"multi_context_missing_before={result['multi_context_label_coverage_before'].get('missing_label_total')} "
                f"multi_context_missing_after={result['multi_context_label_coverage_after'].get('missing_label_total')} "
                f"multi_context_missing_delta={result['multi_context_missing_label_delta']}"
            )
        else:
            print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0
    df = build_action_recommendations(asof_date=asof_date, symbols=args.symbols, setup_ids=args.setup_ids, progress=True)
    action_refresh_sync = None
    if not args.dry_run:
        _progress_log("cli.persist_action_recommendations", "start", rows=len(df))
        persist_action_recommendations(df, trace_enabled=ACTION_RECOMMENDER_CLI_TRACE_ENABLED)
        _progress_log("cli.persist_action_recommendations", "done", rows=len(df))
        _progress_log("cli.record_action_refresh_sync_state", "start", rows=len(df))
        action_refresh_sync = record_action_refresh_sync_state(df, asof_date=asof_date)
        _progress_log("cli.record_action_refresh_sync_state", "done", rows=len(df), extra=f"status={action_refresh_sync.get('status') if isinstance(action_refresh_sync, dict) else None}")
    result = build_cli_result(
        df,
        dry_run=bool(args.dry_run),
        sample_limit=max(0, int(args.sample_limit)),
        full_sample=bool(args.full_sample),
    )
    if action_refresh_sync is not None:
        result["action_refresh_sync"] = action_refresh_sync
    if args.format == "text":
        print(format_cli_text(result))
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
