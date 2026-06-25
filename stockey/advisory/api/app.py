from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

import pandas as pd
from environs import Env
from pydantic import BaseModel, ConfigDict, Field

from advisory.action_recommender import TABLE_NAME as ACTION_RECOMMENDATIONS_TABLE
from advisory.company_memory_review import TABLE_NAME as COMPANY_MEMORY_REVIEWS_TABLE
from advisory.config_change_assistant import build_event_policy_review_rule_preview, build_signal_quality_overlay_preview, build_technical_threshold_preview, build_ts_forecast_review_rule_preview, load_application_decisions, load_previews as load_config_change_previews, record_application_decision
from advisory.cron_status import build_cron_status
from advisory.current_prices import load_current_prices
from advisory.decision_trace import ensure_trace_tables, load_event_trace, load_symbol_trace
from advisory.decision_trace import ACTION_CONFLICTS_TABLE, ACTION_CONFLICT_RULES_TABLE, MANUAL_REVIEW_DECISIONS_TABLE
from advisory.event_model_artifact_store import build_artifact_manifest
from advisory.event_model_promotion_check import build_promotion_check
from advisory.feature_freshness import build_feature_freshness_contract, build_required_feature_freshness_summaries, evaluate_stage_feature_gate
from advisory.hypothesis_engine import create_hypothesis, latest_promotion_audits, load_action_plans, load_hypotheses, load_matches, preview_hypothesis_payload, run_hypothesis_scan, run_promotion_audit, update_hypothesis
from advisory.identity_issues import IDENTITY_ISSUES_TABLE, load_open_identity_issues, resolve_open_identity_issues
from advisory.fallback_telemetry import record_local_fallback_event
from advisory.live_dashboard import DEFAULT_OUTPUT_DIR, build_live_dashboard_payload
from advisory.market_context import load_latest_market_context
from advisory.operator_health import build_operator_health, check_screener_failures
from advisory.operator_snapshot import DEFAULT_MAX_AGE_SECONDS as OPERATOR_SNAPSHOT_MAX_AGE_SECONDS
from advisory.operator_snapshot import load_operator_snapshot
from advisory.operator_snapshot import load_operator_snapshot_sections
from advisory.performance_slowlog import record_slow_operation
from advisory.performance_slowlog import update_slow_issue_status
from advisory.prompt_registry import build_prompt_registry_payload
from advisory.recommendation_diagnostics import build_recommendation_diagnostics
from advisory.regime_overlay import load_regime_overlay_decisions, load_regime_overlay_reviews, record_regime_overlay_decision
from advisory.research_ledger import LEDGER_TABLE as RESEARCH_LEDGER_TABLE
from advisory.research_ledger import list_runs as list_research_ledger_runs
from advisory.screener_coverage import build_screener_coverage_payload
from advisory.signal_refresh import TABLE_NAME as SIGNAL_REFRESH_TABLE
from advisory.signal_quality_evaluator import EVALUATIONS_TABLE as SIGNAL_QUALITY_EVALUATIONS_TABLE
from advisory.signal_quality_evaluator import SUMMARY_TABLE as SIGNAL_QUALITY_SUMMARY_TABLE
from advisory.signal_quality_promotion import generate_promotion_review as generate_signal_quality_promotion_review
from advisory.signal_quality_promotion import load_promotion_reviews as load_signal_quality_promotion_reviews
from advisory.signal_quality_promotion import record_manual_decision as record_signal_quality_manual_decision
from advisory.operator_smoke import build_operator_smoke
from advisory.position_lifecycle import POLICY_CHANGES_TABLE as LIFECYCLE_POLICY_CHANGES_TABLE
from advisory.superseded_failures import cleanup_superseded_failures
from advisory.technical_threshold_calibration import EVALUATIONS_TABLE as TECHNICAL_CALIBRATION_EVALUATIONS_TABLE
from advisory.technical_threshold_calibration import SUMMARY_TABLE as TECHNICAL_CALIBRATION_SUMMARY_TABLE
from advisory.technical_threshold_promotion import generate_promotion_review, load_promotion_reviews, record_manual_decision
from advisory.trace_summary_store import DEFAULT_LIMIT as TRACE_SUMMARY_DEFAULT_LIMIT
from advisory.trace_summary_store import load_summary as load_materialized_trace_summary
from advisory.wait_signals import WAIT_SIGNAL_MATCHES_TABLE, WAIT_SIGNALS_TABLE, load_wait_signal_matches, load_wait_signals, match_wait_signals
from data.screenerin.ad_hoc_query import build_raw_screen_url, companies_preview, fetch_ad_hoc_payload, slugify as screener_slugify
from data.screenerin.query_validation import validate_screener_query
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.ingestion_state import get_state_entries as get_ingestion_state_entries
from utils.ingestion_state import summarize_state_entries as summarize_ingestion_state_entries
from utils.redaction import redact_mapping, redact_text
from utils.schema_migrations import apply_schema_migration


env = Env()
env.read_env()

OPERATOR_API_USE_SNAPSHOT = env.bool("OPERATOR_API_USE_SNAPSHOT", True)
OPERATOR_API_ALLOW_STALE_SNAPSHOT = env.bool("OPERATOR_API_ALLOW_STALE_SNAPSHOT", True)
OPERATOR_API_SLOW_REQUEST_MS = env.float("OPERATOR_API_SLOW_REQUEST_MS", 750.0)
OPERATOR_API_PAYLOAD_CACHE_SECONDS = env.float("OPERATOR_API_PAYLOAD_CACHE_SECONDS", 15.0)
OPERATOR_API_LARGE_RESPONSE_BYTES = env.int("OPERATOR_API_LARGE_RESPONSE_BYTES", 250_000)
OPERATOR_API_TRACE_SUMMARY_CACHE_ENABLED = env.bool("OPERATOR_API_TRACE_SUMMARY_CACHE_ENABLED", True)
OPERATOR_HEALTH_COMPACT_LIST_LIMIT = env.int("OPERATOR_HEALTH_COMPACT_LIST_LIMIT", 25)
OPERATOR_HEALTH_COMPACT_STRING_CHARS = env.int("OPERATOR_HEALTH_COMPACT_STRING_CHARS", 2_000)
REPO_ROOT = Path(__file__).resolve().parents[2]
CRON_LOG_DIR = env.path("OPERATOR_CRON_LOG_DIR", REPO_ROOT / "logs" / "cron")
OPERATOR_COMMAND_RUNS_TABLE = "advisory_operator_command_runs"
OPERATOR_COMMAND_TIMEOUT_SECONDS = env.int("OPERATOR_COMMAND_TIMEOUT_SECONDS", 180)
OPERATOR_COMMAND_OUTPUT_TAIL_CHARS = env.int("OPERATOR_COMMAND_OUTPUT_TAIL_CHARS", 12_000)
OPERATOR_API_ERRORS_TABLE = "advisory_operator_api_errors"
OPERATOR_API_AUDIT_SCHEMA_MIGRATION_ID = "20260611_advisory_operator_api_audit_base"
EXECUTION_APPROVAL_DECISIONS_TABLE = "advisory_execution_approval_decisions"
EXECUTION_APPROVAL_SCHEMA_MIGRATION_ID = "20260612_advisory_execution_approval_decisions"
EXECUTION_EVIDENCE_REVIEWS_TABLE = "advisory_execution_evidence_reviews"
EXECUTION_EVIDENCE_SCHEMA_MIGRATION_ID = "20260612_advisory_execution_evidence_reviews"
EXECUTION_LIVE_ALLOWANCE_REVIEWS_TABLE = "advisory_execution_live_allowance_reviews"
EXECUTION_LIVE_ALLOWANCE_SCHEMA_MIGRATION_ID = "20260612_advisory_execution_live_allowance_reviews"
OPERATOR_API_ERROR_TRACE_CHARS = env.int("OPERATOR_API_ERROR_TRACE_CHARS", 4_000)
SLOW_ISSUE_ALLOWED_STATUSES = {"open", "triaged", "fixed", "ignored"}
EXECUTION_APPROVAL_ALLOWED_DECISIONS = {"approve_dry_run", "reject", "needs_reconciliation", "needs_more_data"}
EXECUTION_MIN_EVIDENCE_SUCCESSFUL_RUNS = env.int("STOCKEY_EXECUTION_MIN_EVIDENCE_SUCCESSFUL_RUNS", 3)
OPERATOR_API_STALE_CODE_GRACE_SECONDS = env.float("OPERATOR_API_STALE_CODE_GRACE_SECONDS", 2.0)
OPERATOR_API_INGESTION_FAILURE_ACTIVE_DAYS = env.int("OPERATOR_HEALTH_INGESTION_FAILURE_ACTIVE_DAYS", 30)
PROCESS_STARTED_AT = time.time()
OPERATOR_API_SCHEMA_VERSION = "2026-06-07.v1"
OPERATOR_API_AUDIT_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {OPERATOR_API_ERRORS_TABLE} (
        error_id TEXT NOT NULL,
        occurred_at TIMESTAMPTZ NOT NULL,
        route TEXT,
        operation TEXT,
        status_code BIGINT,
        error_type TEXT,
        error_message TEXT,
        traceback_tail TEXT,
        request_context_json TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (error_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {OPERATOR_COMMAND_RUNS_TABLE} (
        run_id TEXT NOT NULL,
        command_key TEXT NOT NULL,
        command_label TEXT,
        command_args_json TEXT,
        risk TEXT,
        dry_run BOOLEAN,
        status TEXT NOT NULL,
        returncode BIGINT,
        operator_id TEXT,
        requested_reason TEXT,
        started_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ,
        elapsed_ms DOUBLE PRECISION,
        stdout_tail TEXT,
        stderr_tail TEXT,
        error TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (run_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {MANUAL_REVIEW_DECISIONS_TABLE} (
        decided_at TIMESTAMPTZ NOT NULL,
        item_id TEXT NOT NULL,
        item_type TEXT,
        source_table TEXT,
        source_key TEXT,
        symbol TEXT,
        unique_id TEXT,
        setup_id TEXT,
        decision TEXT NOT NULL,
        operator_id TEXT,
        rationale TEXT,
        follow_up_event TEXT,
        note_json TEXT,
        item_snapshot_json TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (item_id, decided_at)
    )
    """,
]
EXECUTION_APPROVAL_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {EXECUTION_APPROVAL_DECISIONS_TABLE} (
        decision_id TEXT NOT NULL,
        decided_at TIMESTAMPTZ NOT NULL,
        asof_date TIMESTAMPTZ,
        published_on TIMESTAMPTZ,
        setup_id TEXT,
        symbol TEXT,
        unique_id TEXT,
        correlation_id TEXT,
        decision TEXT NOT NULL,
        operator_id TEXT,
        rationale TEXT NOT NULL,
        item_snapshot_json TEXT,
        safety_contract_json TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (decision_id)
    )
    """,
]
EXECUTION_EVIDENCE_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {EXECUTION_EVIDENCE_REVIEWS_TABLE} (
        review_id TEXT NOT NULL,
        reviewed_at TIMESTAMPTZ NOT NULL,
        asof_date TIMESTAMPTZ,
        published_on TIMESTAMPTZ,
        setup_id TEXT,
        symbol TEXT,
        unique_id TEXT,
        transaction_type TEXT,
        mode TEXT NOT NULL,
        decision TEXT NOT NULL,
        operator_id TEXT,
        rationale TEXT,
        successful_runs BIGINT,
        required_runs BIGINT,
        failed_runs BIGINT,
        evidence_rows_json TEXT,
        item_snapshot_json TEXT,
        safety_contract_json TEXT,
        applied BOOLEAN,
        load_ts TIMESTAMPTZ,
        UNIQUE (review_id)
    )
    """,
]
EXECUTION_LIVE_ALLOWANCE_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {EXECUTION_LIVE_ALLOWANCE_REVIEWS_TABLE} (
        allowance_id TEXT NOT NULL,
        allowed_at TIMESTAMPTZ NOT NULL,
        asof_date TIMESTAMPTZ,
        published_on TIMESTAMPTZ,
        setup_id TEXT,
        symbol TEXT,
        unique_id TEXT,
        transaction_type TEXT,
        operator_id TEXT,
        rationale TEXT NOT NULL,
        confirmation_phrase TEXT NOT NULL,
        blocker_json TEXT,
        item_snapshot_json TEXT,
        safety_contract_json TEXT,
        applied BOOLEAN,
        load_ts TIMESTAMPTZ,
        UNIQUE (allowance_id)
    )
    """,
]

_PAYLOAD_CACHE: dict[tuple[str, str | None], tuple[float, dict[str, Any]]] = {}
FEATURE_FRESHNESS_OPERATOR_STAGES = ("rules", "risk", "portfolio", "lifecycle", "actions", "company_memory")


def _record_operator_local_fallback(
    *,
    source: str,
    fallback_type: str,
    reason: str,
    error: Exception | str,
    severity: str = "warn",
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="advisory.api.app",
        source=source,
        fallback_type=fallback_type,
        severity=severity,
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


DISPLAY_REASON_CODE_LABELS = {
    "blocked_by_adversarial_review": "Blocked by adversarial review",
    "positive_action_blocked_by_market_context": "Positive action blocked by market context",
    "risk_off_positive_action_block": "Risk-off market context blocked the positive action",
    "manual_review": "Manual review",
    "review_only": "Review only",
    "incomplete": "Incomplete",
    "incomplete_downgraded": "Incomplete reason contract; downgraded to manual review",
    "no_action": "No action",
    "buy_risk_level": "Buy risk level",
    "sell_exit_trigger": "Sell exit trigger",
    "action_reason": "Action reason",
    "action_source": "Action source",
    "action_code": "Action code",
    "evidence_context": "Evidence context",
    "execution_mode": "Execution mode",
    "playbook_id": "Playbook id",
    "playbook_source_key": "Playbook source key",
    "playbook_review_checks": "Playbook review checks",
    "event_unique_id": "Event unique id",
    "event_review_action": "Event review action",
}


class OperatorApiSchemaModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    version: str
    endpoint: str
    generated_at: str | None = None
    read_only: bool
    broker_execution_enabled: bool


class OperatorApiResponseModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class OperatorRuntimeResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str
    service: str
    process_started_at: str | None = None
    uptime_seconds: int | None = None
    git_rev: str | None = None
    git_branch: str | None = None
    git_dirty: bool | None = None
    latest_source_mtime: str | None = None
    latest_source_path: str | None = None
    stale_code: bool | None = None
    stale_reason: str | None = None
    operator_action: str | None = None
    live_trading_enabled: bool | None = None
    live_trading_disabled: bool | None = None
    live_trading_env_var: str | None = None
    live_trading_operator_note: str | None = None
    read_only: bool


class OperatorHealthResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str
    service: str
    operator_controlled: bool
    read_only: bool
    write_scope: str | None = None


class OperatorActionsResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    asof_date: str | None = None
    snapshot: dict[str, Any] = Field(default_factory=dict)
    snapshot_warning: dict[str, Any] | None = None
    top_action_recommendations: list[dict[str, Any]] = Field(default_factory=list)
    action_recommendations: list[dict[str, Any]] = Field(default_factory=list)
    alerts: list[dict[str, Any]] = Field(default_factory=list)
    pagination: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)


class OperatorHomeResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    asof_date: str | None = None
    snapshot: dict[str, Any] = Field(default_factory=dict)
    snapshot_warning: dict[str, Any] | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    runtime_processes: list[dict[str, Any]] = Field(default_factory=list)
    cron_status: list[dict[str, Any]] = Field(default_factory=list)
    sync_state: list[dict[str, Any]] = Field(default_factory=list)
    top_action_recommendations: list[dict[str, Any]] = Field(default_factory=list)
    today_recommendations: list[dict[str, Any]] = Field(default_factory=list)
    ts_forecast_paper_summary: list[dict[str, Any]] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


class OperatorSummaryResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    asof_date: str | None = None
    snapshot: dict[str, Any] = Field(default_factory=dict)
    snapshot_warning: dict[str, Any] | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    runtime_processes: list[dict[str, Any]] = Field(default_factory=list)
    cron_status: list[dict[str, Any]] = Field(default_factory=list)
    sync_state: list[dict[str, Any]] = Field(default_factory=list)


class OperatorPortfolioResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    asof_date: str | None = None
    snapshot: dict[str, Any] = Field(default_factory=dict)
    snapshot_warning: dict[str, Any] | None = None
    today_recommendations: list[dict[str, Any]] = Field(default_factory=list)
    current_recommendations: list[dict[str, Any]] = Field(default_factory=list)
    exited_recommendations: list[dict[str, Any]] = Field(default_factory=list)
    portfolio: list[dict[str, Any]] = Field(default_factory=list)
    lifecycle: list[dict[str, Any]] = Field(default_factory=list)
    pagination: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)


class OperatorWatchlistResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    asof_date: str | None = None
    snapshot: dict[str, Any] = Field(default_factory=dict)
    snapshot_warning: dict[str, Any] | None = None
    watch_recommendations: list[dict[str, Any]] = Field(default_factory=list)
    watchlist: list[dict[str, Any]] = Field(default_factory=list)
    ts_watch_recommendations: list[dict[str, Any]] = Field(default_factory=list)
    ts_forecast_watch: list[dict[str, Any]] = Field(default_factory=list)
    ts_forecast_eval_summary: list[dict[str, Any]] = Field(default_factory=list)
    ts_forecast_paper_summary: list[dict[str, Any]] = Field(default_factory=list)
    pagination: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)


class OperatorMarketContextResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    asof_date: str | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    top_universe: list[dict[str, Any]] = Field(default_factory=list)


class RegimeOverlaysResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str
    proposals: list[dict[str, Any]] = Field(default_factory=list)
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    pagination: dict[str, Any] = Field(default_factory=dict)
    operator_boundary: dict[str, Any] = Field(default_factory=dict)


class RegimeOverlayDecisionResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str
    decided_at: str | None = None
    decision: str | None = None
    proposal_id: str | None = None
    proposal_status: str | None = None
    decision_effect: dict[str, Any] = Field(default_factory=dict)
    proposal: dict[str, Any] = Field(default_factory=dict)
    operator_boundary: dict[str, Any] = Field(default_factory=dict)
    note: str | None = None




class SignalRefreshResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str
    signals: list[dict[str, Any]] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)




class OperatorHealthDetailsResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str
    sections: dict[str, Any] = Field(default_factory=dict)
    fix_hints: list[dict[str, Any]] = Field(default_factory=list)
    current_blockers: dict[str, Any] | None = None


class DataHealthResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    asof_date: str | None = None
    snapshot: dict[str, Any] = Field(default_factory=dict)
    snapshot_warning: dict[str, Any] | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    sync_state: list[dict[str, Any]] = Field(default_factory=list)
    runtime_processes: list[dict[str, Any]] = Field(default_factory=list)
    cron_status: list[dict[str, Any]] = Field(default_factory=list)


class FeatureFreshnessResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str
    symbol: str | None = None
    asof_date: str | None = None
    counts: dict[str, Any] = Field(default_factory=dict)
    blockers: list[dict[str, Any]] = Field(default_factory=list)
    inputs: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class OperationsSmokeResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str | None = None
    operator_health: dict[str, Any] = Field(default_factory=dict)
    fix_hints: list[dict[str, Any]] = Field(default_factory=list)
    read_only: bool = True
    note: str | None = None


class OperationsCronLogsResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str
    log_dir: str | None = None
    logs: list[dict[str, Any]] = Field(default_factory=list)
    pagination: dict[str, Any] = Field(default_factory=dict)


class OperationsCronStatusResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str
    crontab_path: str | None = None
    log_dir: str | None = None
    counts: dict[str, Any] = Field(default_factory=dict)
    jobs: list[dict[str, Any]] = Field(default_factory=list)
    pagination: dict[str, Any] = Field(default_factory=dict)


class OperationsCommandsResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str
    commands: list[dict[str, Any]] = Field(default_factory=list)
    recent_runs: list[dict[str, Any]] = Field(default_factory=list)


class OperationsApiErrorsResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str
    errors: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)


class OperationsIngestionStateResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str
    filters: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)
    operator_boundary: dict[str, Any] = Field(default_factory=dict)


class OperationsSupersededCleanupResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str
    mode: str
    dry_run: bool = True
    result: dict[str, Any] = Field(default_factory=dict)
    counts: dict[str, Any] = Field(default_factory=dict)
    operator_boundary: dict[str, Any] = Field(default_factory=dict)
    audit_run: dict[str, Any] | None = None
    note: str | None = None


class EventModelPromotionCheckResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str
    decision: str | None = None
    ready_for_operator_review: bool | None = None
    scorecard: dict[str, Any] = Field(default_factory=dict)
    promotion_mode: str | None = None
    artifact: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] | None = None
    coverage: dict[str, Any] = Field(default_factory=dict)
    weekly_runs: dict[str, Any] = Field(default_factory=dict)
    score_freshness: dict[str, Any] = Field(default_factory=dict)
    gates: list[dict[str, Any]] = Field(default_factory=list)
    failed_gates: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)






class RecommendationDiagnosticsResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str
    asof_date: str | None = None
    row_count: int | None = None
    positive_action_count: int | None = None
    no_buy_diagnosis: str | None = None
    primary_no_buy_cause: dict[str, Any] = Field(default_factory=dict)
    layered_underparticipation_attribution: dict[str, Any] = Field(default_factory=dict)
    advisory_rerun_readiness: dict[str, Any] = Field(default_factory=dict)
    context_overlay_refresh_preview: dict[str, Any] = Field(default_factory=dict)
    causal_memory_refresh_preview: dict[str, Any] = Field(default_factory=dict)
    market_participation_context: dict[str, Any] = Field(default_factory=dict)
    diagnostic_trust: dict[str, Any] = Field(default_factory=dict)
    operator_next_steps: list[str] = Field(default_factory=list)
    operator_boundary: dict[str, Any] = Field(default_factory=dict)


class EventModelArtifactsResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str | None = None
    artifact: dict[str, Any] = Field(default_factory=dict)
    latest_s3_heads: list[dict[str, Any]] = Field(default_factory=list)
    pagination: dict[str, Any] = Field(default_factory=dict)
    operator_boundary: dict[str, Any] = Field(default_factory=dict)
    read_only: bool = True


class ResearchLedgerResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str
    runs: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    pagination: dict[str, Any] = Field(default_factory=dict)
    operator_boundary: dict[str, Any] = Field(default_factory=dict)




class IdentityIssuesResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str
    summary: dict[str, Any] = Field(default_factory=dict)
    issues: list[dict[str, Any]] = Field(default_factory=list)
    skipped: list[dict[str, Any]] = Field(default_factory=list)


class IdentityIssueResolutionResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str
    mode: str
    checked_rows: int = 0
    counts: dict[str, Any] = Field(default_factory=dict)
    results: list[dict[str, Any]] = Field(default_factory=list)
    requested_issue_keys: list[str] = Field(default_factory=list)
    operator_boundary: dict[str, Any] = Field(default_factory=dict)
    note: str | None = None




class ActionConflictRulesResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    rules: list[dict[str, Any]] = Field(default_factory=list)
    unresolved_conflicts: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int
    unresolved_count: int = 0


class ActionConflictRuleWriteResponse(OperatorApiResponseModel):
    status: str
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    rule: dict[str, Any] = Field(default_factory=dict)
    condition: dict[str, Any] | None = None
    note: str | None = None
















class WaitSignalsResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str
    summary: dict[str, Any] = Field(default_factory=dict)
    sections: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    signals: list[dict[str, Any]] = Field(default_factory=list)
    matches: list[dict[str, Any]] = Field(default_factory=list)
    match_result: dict[str, Any] | None = None


class WaitSignalMatchResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str
    match_result: dict[str, Any] = Field(default_factory=dict)


class SymbolTraceResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel | None = None
    symbol: str | None = None
    processing: list[dict[str, Any]] = Field(default_factory=list)
    traces: list[dict[str, Any]] = Field(default_factory=list)
    steps: list[dict[str, Any]] = Field(default_factory=list)
    action_conflicts: list[dict[str, Any]] = Field(default_factory=list)
    pagination: dict[str, Any] = Field(default_factory=dict)


class OperatorDetailResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str
    kind: str
    filters: dict[str, Any] = Field(default_factory=dict)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int
    policy_changes: list[dict[str, Any]] = Field(default_factory=list)
    policy_change_count: int = 0






class TraceSummaryResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel | None = None
    symbol: str | None = None
    unique_id: str | None = None
    processing: list[dict[str, Any]] = Field(default_factory=list)
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    action_conflicts: list[dict[str, Any]] = Field(default_factory=list)
    raw_counts: dict[str, Any] = Field(default_factory=dict)
    pagination: dict[str, Any] = Field(default_factory=dict)


class TechnicalCalibrationResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str
    summary: list[dict[str, Any]] = Field(default_factory=list)
    top_configs: list[dict[str, Any]] = Field(default_factory=list)


class SignalQualityResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str
    latest_evaluated_at: str | None = None
    summary: list[dict[str, Any]] = Field(default_factory=list)
    examples: list[dict[str, Any]] = Field(default_factory=list)
    coverage: list[dict[str, Any]] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


class TechnicalPromotionReviewsResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str
    reviews: list[dict[str, Any]] = Field(default_factory=list)
    pagination: dict[str, Any] = Field(default_factory=dict)


class SignalQualityPromotionReviewsResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str
    reviews: list[dict[str, Any]] = Field(default_factory=list)
    pagination: dict[str, Any] = Field(default_factory=dict)






class PromotionReviewResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel | None = None
    status: str
    reviewed_at: str | None = None
    review_status: str | None = None
    review_error: str | None = None
    recommendation: str | None = None
    confidence: float | None = None
    pending_patch: dict[str, Any] = Field(default_factory=dict)
    llm_review: dict[str, Any] = Field(default_factory=dict)
    applied: bool = False


class PromotionDecisionResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel | None = None
    status: str
    decided_at: str | None = None
    reviewed_at: str | None = None
    decision: str | None = None
    final_patch: dict[str, Any] = Field(default_factory=dict)
    review: dict[str, Any] = Field(default_factory=dict)
    applied: bool = False
    note: str | None = None


class ConfigChangePreviewsResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str
    previews: list[dict[str, Any]] = Field(default_factory=list)
    pagination: dict[str, Any] = Field(default_factory=dict)


class ConfigChangePreviewResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str
    preview_id: str | None = None
    source_type: str | None = None
    source_key: str | None = None
    config_path: str | None = None
    review_status: str | None = None
    decision_status: str | None = None
    patch_payload: dict[str, Any] = Field(default_factory=dict)
    unified_diff: str | None = None
    rollback_note: str | None = None
    safety_checks: list[Any] = Field(default_factory=list)
    decision: dict[str, Any] = Field(default_factory=dict)
    applied: bool = False


class ConfigChangeApplicationsResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str
    applications: list[dict[str, Any]] = Field(default_factory=list)
    pagination: dict[str, Any] = Field(default_factory=dict)


class ConfigChangeApplicationResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str
    application_id: str | None = None
    preview_id: str | None = None
    application_decision: str | None = None
    verification_status: str | None = None
    verification: dict[str, Any] = Field(default_factory=dict)
    safety_checks: list[Any] = Field(default_factory=list)
    decision_effect: dict[str, Any] = Field(default_factory=dict)
    operator_boundary: dict[str, Any] = Field(default_factory=dict)
    applied_by_system: bool = False
    applied: bool = False
    note: str | None = None


class PromptRegistryResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str
    contracts: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    pagination: dict[str, Any] = Field(default_factory=dict)


class ScreenerPreviewResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str
    query_name: str | None = None
    query_slug: str | None = None
    query_hash: str | None = None
    screener_url: str | None = None
    validation_issues: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    rows: list[dict[str, Any]] = Field(default_factory=list)
    headers: list[Any] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    operator_boundary: dict[str, Any] = Field(default_factory=dict)


class ScreenerCoverageResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str
    asof_date: str | None = None
    lookback_days: int | None = None
    window: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)
    screeners: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ScreenerFailuresResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str
    message: str | None = None
    window_hours: int | None = None
    active_count: int | None = None
    validation_count: int | None = None
    fetch_count: int | None = None
    parse_count: int | None = None
    counts_by_stage: dict[str, Any] = Field(default_factory=dict)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    operator_boundary: dict[str, Any] = Field(default_factory=dict)






class HypothesesResponse(OperatorApiResponseModel):
    generated_at: str | None = None
    api_schema: OperatorApiSchemaModel
    status: str
    hypotheses: list[dict[str, Any]] = Field(default_factory=list)
    matches: list[dict[str, Any]] = Field(default_factory=list)
    action_plans: list[dict[str, Any]] = Field(default_factory=list)
    wait_signals: list[dict[str, Any]] = Field(default_factory=list)
    wait_signal_matches: list[dict[str, Any]] = Field(default_factory=list)
    promotion_audits: list[dict[str, Any]] = Field(default_factory=list)
    pagination: dict[str, Any] = Field(default_factory=dict)


EVENT_CLASS_LABELS = {
    "REGULATORY_NOTICE": "regulatory or tax notice",
    "POLICY_SECTOR_NEGATIVE": "sector policy risk",
    "POLICY_SECTOR_POSITIVE": "sector policy support",
    "ANALYST_MEET": "analyst or investor meeting",
    "ORDER_WIN": "order win",
    "RESULTS_POSITIVE": "positive results",
    "RESULTS_NEGATIVE": "weak results",
    "RESULTS_MIXED": "mixed results",
    "GROWTH_ACCELERATION": "growth acceleration",
    "MARGIN_EXPANSION": "margin expansion",
    "GUIDANCE_UPGRADE": "guidance upgrade",
    "GUIDANCE_DOWNGRADE": "guidance downgrade",
    "PLEDGE_UP": "promoter pledge increase",
    "PLEDGE_DOWN": "promoter pledge reduction",
    "PROMOTER_BUYING": "promoter buying",
    "PROMOTER_SELLING": "promoter selling",
    "MANAGEMENT_RESIGNATION": "senior management resignation",
    "AUDITOR_GOVERNANCE": "auditor or governance concern",
    "DILUTION": "dilution or fund raise",
    "BUYBACK": "buyback",
    "DIVIDEND": "dividend",
    "CORPORATE_ACTION_NEUTRAL": "routine corporate action",
    "OTHER": "company event",
}


def _operator_api_schema(endpoint: str, *, schema_name: str, version: str = OPERATOR_API_SCHEMA_VERSION, read_only: bool = True) -> dict[str, Any]:
    return {
        "name": schema_name,
        "version": version,
        "endpoint": endpoint,
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "read_only": bool(read_only),
        "broker_execution_enabled": False,
    }


def _with_operator_api_schema(payload: dict[str, Any], *, endpoint: str, schema_name: str) -> dict[str, Any]:
    enriched = dict(payload or {})
    enriched.setdefault("generated_at", pd.Timestamp.utcnow().isoformat())
    enriched.setdefault("api_schema", _operator_api_schema(endpoint, schema_name=schema_name))
    return enriched


def _parse_asof_date(value: str | None) -> pd.Timestamp | None:
    if not value:
        return None
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        raise ValueError(f"Invalid asof_date: {value}")
    return ts.normalize()


def _parse_timestamp(value: str | None) -> pd.Timestamp | None:
    if not value:
        return None
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        raise ValueError(f"Invalid timestamp: {value}")
    return ts


def _snapshot_payload(payload: dict[str, Any]) -> dict[str, Any]:
    snapshot = payload.get("_snapshot")
    if not isinstance(snapshot, dict):
        return {"source": "unknown", "freshness": "unknown", "age_seconds": None}
    out = dict(snapshot)
    generated_at = pd.to_datetime(out.get("generated_at"), utc=True, errors="coerce")
    if not pd.isna(generated_at):
        age_seconds = max(0, int((pd.Timestamp.now(tz="UTC") - generated_at).total_seconds()))
        out["generated_at"] = generated_at.isoformat()
        out["age_seconds"] = age_seconds
    else:
        out["age_seconds"] = None
    out.setdefault("freshness", "fresh")
    return out


def _snapshot_warning_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    snapshot = _snapshot_payload(payload)
    freshness = str(snapshot.get("freshness") or "").lower()
    if freshness != "stale":
        return None
    reason = str(snapshot.get("reason") or "fresh_snapshot_missing")
    return {
        "status": "warn",
        "title": "Stale operator snapshot",
        "message": "The API is serving the latest cached operator snapshot because a fresh snapshot is missing.",
        "source": snapshot.get("source") or "unknown",
        "generated_at": snapshot.get("generated_at"),
        "age_seconds": snapshot.get("age_seconds"),
        "max_age_seconds": snapshot.get("max_age_seconds"),
        "reason": reason,
        "operator_action": "run_operator_snapshot_or_wait_for_advisory",
        "commands": ["python -m advisory.operator_snapshot", "./all_advisory.sh"],
    }


OPERATOR_SOURCE_WARNING_MAX_AGE_SECONDS = int(os.getenv("OPERATOR_SOURCE_WARNING_MAX_AGE_SECONDS", str(36 * 3600)))
OPERATOR_SOURCE_WARNING_TIMESTAMP_FIELDS = (
    "updated_at",
    "last_seen_at",
    "matched_at",
    "created_at",
    "load_ts",
    "observed_at",
    "asof_date",
)


def _source_warning_timestamp(row: dict[str, Any], fields: tuple[str, ...] = OPERATOR_SOURCE_WARNING_TIMESTAMP_FIELDS) -> pd.Timestamp | None:
    latest: pd.Timestamp | None = None
    for field in fields:
        ts = pd.to_datetime(row.get(field), utc=True, errors="coerce")
        if pd.isna(ts):
            continue
        if latest is None or ts > latest:
            latest = ts
    return latest


def _operator_source_warnings(
    rows: list[dict[str, Any]],
    *,
    default_source: str,
    source_field: str | None = None,
    skipped: list[dict[str, Any]] | None = None,
    max_age_seconds: int = OPERATOR_SOURCE_WARNING_MAX_AGE_SECONDS,
) -> list[dict[str, Any]]:
    now = pd.Timestamp.utcnow()
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        source = _text(row.get(source_field)) if source_field else ""
        source = source or default_source
        group = grouped.setdefault(source, {"source": source, "row_count": 0, "latest_at": None, "timestamped_rows": 0})
        group["row_count"] += 1
        ts = _source_warning_timestamp(row)
        if ts is None:
            continue
        group["timestamped_rows"] += 1
        if group["latest_at"] is None or ts > group["latest_at"]:
            group["latest_at"] = ts

    warnings: list[dict[str, Any]] = []
    for source, group in sorted(grouped.items()):
        latest_at = group.get("latest_at")
        if latest_at is None:
            warnings.append(
                {
                    "status": "warn",
                    "title": "Source freshness unknown",
                    "message": "Rows are visible, but none include a usable source timestamp.",
                    "source": source,
                    "reason": "source_timestamp_missing",
                    "affected_rows": group["row_count"],
                    "operator_action": "inspect_source_row_or_rerun_source",
                }
            )
            continue
        age_seconds = max(0, int((now - latest_at).total_seconds()))
        if age_seconds <= max_age_seconds:
            continue
        warnings.append(
            {
                "status": "warn",
                "title": "Stale source rows",
                "message": "Rows on this page come from source timestamps older than the operator freshness window.",
                "source": source,
                "latest_at": latest_at.isoformat(),
                "age_seconds": age_seconds,
                "max_age_seconds": max_age_seconds,
                "affected_rows": group["row_count"],
                "timestamped_rows": group["timestamped_rows"],
                "reason": "source_rows_stale",
                "operator_action": "rerun_source_or_refresh_advisory",
            }
        )

    for row in skipped or []:
        warnings.append(
            {
                "status": "warn",
                "title": "Source unavailable",
                "message": "A source query failed while building this page, so visible rows may be incomplete.",
                "source": _text(row.get("source")) or default_source,
                "error": _text(row.get("error")),
                "reason": "source_query_skipped",
                "operator_action": "inspect_source_error_and_rerun_page",
            }
        )
    return warnings


def load_operator_payload(*, asof_date: str | pd.Timestamp | None = None) -> dict[str, Any]:
    parsed_asof = _parse_asof_date(asof_date) if isinstance(asof_date, str) else asof_date
    cache_key = ("operator_payload", None if parsed_asof is None else pd.to_datetime(parsed_asof, utc=True).normalize().strftime("%Y-%m-%d"))
    now = time.monotonic()
    cached = _PAYLOAD_CACHE.get(cache_key)
    if cached and OPERATOR_API_PAYLOAD_CACHE_SECONDS > 0 and (now - cached[0]) <= OPERATOR_API_PAYLOAD_CACHE_SECONDS:
        return cached[1]
    if OPERATOR_API_USE_SNAPSHOT:
        snapshot = load_operator_snapshot(
            asof_date=parsed_asof,
            max_age_seconds=OPERATOR_SNAPSHOT_MAX_AGE_SECONDS,
        )
        if snapshot is not None:
            _PAYLOAD_CACHE[cache_key] = (now, snapshot)
            return snapshot
        if OPERATOR_API_ALLOW_STALE_SNAPSHOT:
            stale_snapshot = load_operator_snapshot(
                asof_date=parsed_asof,
                max_age_seconds=0,
            )
            if stale_snapshot is not None:
                stale_snapshot["_snapshot"] = {
                    **(stale_snapshot.get("_snapshot") or {}),
                    "freshness": "stale",
                    "reason": "fresh_snapshot_missing",
                    "max_age_seconds": OPERATOR_SNAPSHOT_MAX_AGE_SECONDS,
                }
                _PAYLOAD_CACHE[cache_key] = (now, stale_snapshot)
                return stale_snapshot
    payload = build_live_dashboard_payload(asof_date=parsed_asof, output_dir=DEFAULT_OUTPUT_DIR)
    payload["_snapshot"] = {
        "source": "live_builder",
        "freshness": "live",
        "reason": "missing_or_stale_snapshot",
        "generated_at": pd.Timestamp.utcnow().isoformat(),
    }
    _PAYLOAD_CACHE[cache_key] = (now, payload)
    return payload


def load_operator_sections_payload(section_names: list[str], *, asof_date: str | pd.Timestamp | None = None, allow_missing: bool = False) -> dict[str, Any] | None:
    parsed_asof = _parse_asof_date(asof_date) if isinstance(asof_date, str) else asof_date
    cache_key = (
        "operator_sections",
        ",".join(sorted({str(name) for name in section_names})),
        None if parsed_asof is None else pd.to_datetime(parsed_asof, utc=True).normalize().strftime("%Y-%m-%d"),
        bool(allow_missing),
    )
    now = time.monotonic()
    cached = _PAYLOAD_CACHE.get(cache_key)
    if cached and OPERATOR_API_PAYLOAD_CACHE_SECONDS > 0 and (now - cached[0]) <= OPERATOR_API_PAYLOAD_CACHE_SECONDS:
        return cached[1]
    if not OPERATOR_API_USE_SNAPSHOT:
        return None
    payload = load_operator_snapshot_sections(
        section_names,
        asof_date=parsed_asof,
        max_age_seconds=OPERATOR_SNAPSHOT_MAX_AGE_SECONDS,
        allow_missing=bool(allow_missing),
    )
    if payload is None and OPERATOR_API_ALLOW_STALE_SNAPSHOT:
        payload = load_operator_snapshot_sections(
            section_names,
            asof_date=parsed_asof,
            max_age_seconds=0,
            allow_missing=bool(allow_missing),
        )
        if payload is not None:
            payload["_snapshot"] = {
                **(payload.get("_snapshot") or {}),
                "freshness": "stale",
                "reason": "fresh_section_snapshot_missing",
                "max_age_seconds": OPERATOR_SNAPSHOT_MAX_AGE_SECONDS,
            }
    if payload is not None:
        _PAYLOAD_CACHE[cache_key] = (now, payload)
    return payload


def build_health_payload() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "stockey-operator-api",
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema("/api/health", schema_name="operator_health"),
        "operator_controlled": True,
        "read_only": False,
        "write_scope": "operator_audit_and_research_controls",
    }


def _git_output(args: list[str]) -> str | None:
    try:
        return subprocess.check_output(
            ["git", *args],
            cwd=REPO_ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.5,
        ).strip()
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.api.app",
            fallback_type="operator_api_runtime_git_metadata_unavailable",
            source="operator_runtime",
            severity="warn",
            reason="Operator API runtime metadata could not read git state; stale-code and version display may be incomplete.",
            error=exc,
            metadata={"git_args": list(args)},
        )
        return None


def _latest_source_mtime(root: Path = REPO_ROOT) -> tuple[float | None, str | None]:
    ignored_dirs = {
        ".git",
        ".mypy_cache",
        ".nuxt",
        ".output",
        ".pytest_cache",
        "__pycache__",
        "data",
        "logs",
        "node_modules",
    }
    source_suffixes = {".css", ".html", ".js", ".json", ".py", ".sh", ".ts", ".vue", ".yaml", ".yml"}
    latest_mtime: float | None = None
    latest_path: str | None = None
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in ignored_dirs and not name.startswith(".venv")]
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.suffix not in source_suffixes:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError as exc:
                record_local_fallback_event(
                    module="advisory.api.app",
                    fallback_type="operator_api_runtime_source_mtime_unavailable",
                    source="operator_runtime",
                    severity="warn",
                    reason="Operator API runtime metadata could not stat a source file; stale-code detection may be incomplete.",
                    error=exc,
                    metadata={"path": str(path)},
                )
                continue
            if latest_mtime is None or mtime > latest_mtime:
                latest_mtime = mtime
                try:
                    latest_path = str(path.relative_to(root))
                except ValueError as exc:
                    record_local_fallback_event(
                        module="advisory.api.app",
                        fallback_type="operator_api_runtime_source_path_relative_failed",
                        source="operator_runtime",
                        severity="warn",
                        reason="Operator API runtime metadata could not compute a repository-relative source path.",
                        error=exc,
                        metadata={"path": str(path), "root": str(root)},
                    )
                    latest_path = str(path)
    return latest_mtime, latest_path


def _env_bool_runtime(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def build_runtime_payload() -> dict[str, Any]:
    git_rev = _git_output(["rev-parse", "--short=12", "HEAD"])
    git_branch = _git_output(["rev-parse", "--abbrev-ref", "HEAD"])
    dirty_text = _git_output(["status", "--porcelain"])
    latest_mtime, latest_path = _latest_source_mtime()
    process_started_at = pd.Timestamp.utcfromtimestamp(PROCESS_STARTED_AT).isoformat()
    latest_source_mtime = pd.Timestamp.utcfromtimestamp(latest_mtime).isoformat() if latest_mtime else None
    stale_code = bool(latest_mtime and latest_mtime > PROCESS_STARTED_AT + OPERATOR_API_STALE_CODE_GRACE_SECONDS)
    live_trading_enabled = _env_bool_runtime("STOCKEY_LIVE_TRADING_ENABLED", False)
    return {
        "status": "ok",
        "service": "stockey-operator-api",
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema("/api/runtime", schema_name="operator_runtime"),
        "process_started_at": process_started_at,
        "uptime_seconds": max(0, int(time.time() - PROCESS_STARTED_AT)),
        "git_rev": git_rev,
        "git_branch": git_branch,
        "git_dirty": bool(dirty_text),
        "latest_source_mtime": latest_source_mtime,
        "latest_source_path": latest_path,
        "stale_code": stale_code,
        "stale_reason": "source_newer_than_api_process" if stale_code else None,
        "operator_action": "restart_operator_api" if stale_code else None,
        "live_trading_enabled": live_trading_enabled,
        "live_trading_disabled": not live_trading_enabled,
        "live_trading_env_var": "STOCKEY_LIVE_TRADING_ENABLED",
        "live_trading_operator_note": (
            "Live broker submission is enabled by environment; execution still requires approval, reconciliation, and safety gates."
            if live_trading_enabled
            else "Live broker submission is disabled by default; planning and reconciliation stay read-only/dry-run."
        ),
        "read_only": True,
    }


def build_summary_payload(*, asof_date: str | None = None) -> dict[str, Any]:
    payload = load_operator_sections_payload(
        ["summary", "runtime_processes", "cron_status", "sync_state"],
        asof_date=asof_date,
    ) or load_operator_payload(asof_date=asof_date)
    return {
        "generated_at": payload.get("generated_at"),
        "api_schema": _operator_api_schema("/api/summary", schema_name="operator_summary"),
        "asof_date": payload.get("asof_date"),
        "snapshot": _snapshot_payload(payload),
        "snapshot_warning": _snapshot_warning_payload(payload),
        "summary": payload.get("summary") or {},
        "runtime_processes": payload.get("runtime_processes") or [],
        "cron_status": payload.get("cron_status") or [],
        "sync_state": payload.get("sync_state") or [],
    }


HOME_CARD_FIELDS = [
    "symbol",
    "kind",
    "status",
    "action_summary",
    "reason",
    "reason_detail",
    "recommendation_reason",
    "reason_contract_status",
    "setup_id",
    "setup_name",
    "setup_family",
    "entry_date",
    "entry_price",
    "current_price",
    "last_price",
    "reference_price",
    "pnl_pct",
    "invest_score_pct",
    "allocation_inr",
    "execution_intent",
    "action_queue_contract",
    "execution_safety_contract",
    "exit_strategy",
    "technical_context",
    "announcement_summary",
    "news_summary",
    "manual_revision_summary",
    "manual_revision_pointers",
    "company_memory_review",
    "sort_ts",
    "final_state_trust",
]


def _trim_home_value(value: Any, *, max_text: int = 700, max_list: int = 4, max_depth: int = 3) -> Any:
    if max_depth <= 0:
        if isinstance(value, (dict, list)):
            return None
        return _text(value) if not isinstance(value, (int, float, bool)) else value
    if isinstance(value, str):
        text = value.strip()
        return text if len(text) <= max_text else f"{text[:max_text].rstrip()}..."
    if isinstance(value, list):
        return [_trim_home_value(item, max_text=max_text, max_list=max_list, max_depth=max_depth - 1) for item in value[:max_list]]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            trimmed = _trim_home_value(item, max_text=max_text, max_list=max_list, max_depth=max_depth - 1)
            if trimmed is not None:
                out[str(key)] = trimmed
        return out
    return value


def _display_reason_text(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    mapped = DISPLAY_REASON_CODE_LABELS.get(text.lower())
    if mapped:
        return mapped
    if "_" in text and "\n" not in text and len(text.split()) <= 3:
        return text.replace("_", " ").capitalize()
    return text


def _compact_reason_value(key: str, value: Any, *, max_text: int = 320, max_list: int = 4, max_depth: int = 2) -> Any:
    trimmed = _trim_home_value(value, max_text=max_text, max_list=max_list, max_depth=max_depth)
    if isinstance(trimmed, str) and any(token in key for token in ("reason", "status", "adjustment")):
        return _display_reason_text(trimmed) or trimmed
    if isinstance(trimmed, list) and key in {"missing_fields"}:
        return [_display_reason_text(item) or item for item in trimmed]
    return trimmed


def _compact_reason_contract(value: Any) -> dict[str, Any] | str | None:
    parsed = _jsonish(value)
    if not isinstance(parsed, dict):
        trimmed = _trim_home_value(value, max_text=500, max_depth=1)
        if isinstance(trimmed, str):
            return _display_reason_text(trimmed) or trimmed
        return trimmed
    out: dict[str, Any] = {}
    for key in [
        "status",
        "action_code",
        "final_action",
        "new_action",
        "original_action_code",
        "action_source",
        "execution_action",
        "setup_id",
        "primary_reason",
        "reason",
        "reason_detail",
        "missing_fields",
    ]:
        if parsed.get(key) is not None:
            out[key] = _compact_reason_value(key, parsed.get(key), max_text=320, max_list=4, max_depth=2)
    evidence = parsed.get("evidence")
    if isinstance(evidence, dict):
        compact_evidence: dict[str, Any] = {}
        for section_key, section_value in evidence.items():
            if not isinstance(section_value, dict):
                continue
            compact_section: dict[str, Any] = {}
            if str(section_key) == "conflict_resolution":
                section_items = [
                    (key, section_value.get(key))
                    for key in [
                        "same_symbol_candidate_count",
                        "same_symbol_conflict_count",
                        "conflict_precedence_rule_id",
                        "conflict_precedence_reason",
                        "winning_action_code",
                        "winning_action_source",
                        "source_precedence_reason",
                        "losing_candidates",
                    ]
                    if section_value.get(key) is not None
                ]
            elif str(section_key) == "event":
                section_items = [
                    (key, section_value.get(key))
                    for key in [
                        "event_class",
                        "verdict",
                        "review_action",
                        "veto",
                        "review_reason",
                        "action_status",
                        "state_transition_hint",
                        "score_impact",
                    ]
                    if section_value.get(key) is not None
                ]
            elif str(section_key) == "macro_regime":
                section_items = [
                    (key, section_value.get(key))
                    for key in [
                        "market_context_adjustment",
                        "market_context_adjustment_reason",
                        "regime_name",
                        "macro_risk_state",
                        "breadth_trend_alignment_pct",
                        "risk_off_score",
                        "top_context_rank_pct",
                        "top_context_sector",
                    ]
                    if section_value.get(key) is not None
                ]
            elif str(section_key) == "action_transition":
                section_items = [
                    (key, section_value.get(key))
                    for key in [
                        "action_code",
                        "state_effect",
                        "broker_order_candidate",
                        "broker_execution_allowed",
                        "broker_boundary",
                        "next_required_stage",
                        "allowed_after_preview",
                        "required_preconditions",
                        "missing_preconditions",
                        "precondition_status",
                        "entry_evidence_required",
                        "exit_evidence_required",
                        "policy_audit_required",
                    ]
                    if section_value.get(key) is not None
                ]
            else:
                section_items = list(section_value.items())[:5]
            for item_key, item_value in section_items:
                trim_depth = 2 if str(section_key) == "conflict_resolution" and str(item_key) == "losing_candidates" else 1
                trimmed = _compact_reason_value(str(item_key), item_value, max_text=160, max_list=3, max_depth=trim_depth)
                if trimmed is not None:
                    compact_section[str(item_key)] = trimmed
            if compact_section:
                compact_evidence[str(section_key)] = compact_section
        if compact_evidence:
            out["evidence"] = compact_evidence
    return out or None


def _compact_company_memory_review(value: Any) -> dict[str, Any] | None:
    parsed = _jsonish(value)
    if not isinstance(parsed, dict) or not parsed:
        return None
    out: dict[str, Any] = {}
    for key in [
        "review_date",
        "recommended_signal",
        "confidence",
        "conviction_score",
        "summary",
        "thesis",
        "risk_flags",
        "evidence_used",
        "wait_for",
        "authority_scope",
        "review_status",
        "fallback_used",
        "model_name",
        "evidence_source_contract",
    ]:
        if parsed.get(key) is not None:
            out[key] = _trim_home_value(parsed.get(key), max_text=280, max_list=4, max_depth=2)
    return out or None


def _compact_home_field(key: str, value: Any) -> Any:
    if key == "recommendation_reason":
        return _compact_action_reason_contract(value)
    if key == "company_memory_review":
        return _compact_company_memory_review(value)
    if key == "manual_revision_pointers":
        return _compact_manual_revision_pointers(value)
    if key == "action_queue_contract":
        return _trim_home_value(value, max_text=220, max_list=5, max_depth=2)
    if key == "execution_safety_contract":
        return _trim_home_value(value, max_text=220, max_list=5, max_depth=3)
    if key == "feature_gate_effects":
        return _trim_home_value(value, max_text=360, max_list=5, max_depth=5)
    if key == "final_state_trust":
        return _compact_action_final_state_trust(value)
    if key in {"reason", "reason_detail", "action_reason"}:
        return _compact_reason_value(key, value, max_text=360, max_list=3, max_depth=2)
    if key in {"announcement_summary", "news_summary", "exit_strategy", "manual_revision_summary"}:
        return _trim_home_value(value, max_text=360, max_list=3, max_depth=2)
    return _trim_home_value(value, max_text=500, max_list=4, max_depth=3)


def _compact_home_rows(rows: Any, *, limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(rows, list):
        return out
    prepared = _with_action_queue_contracts(_with_execution_safety_contracts(rows[: max(0, int(limit))]))
    _attach_action_final_state_trust(prepared)
    for row in prepared:
        if not isinstance(row, dict):
            continue
        compact = {
            key: _compact_home_field(key, row.get(key))
            for key in HOME_CARD_FIELDS
            if row.get(key) is not None
        }
        out.append({key: value for key, value in compact.items() if value not in (None, "", [], {})})
    return out


def _execution_safety_contract_from_row(row: dict[str, Any]) -> dict[str, Any] | None:
    existing = row.get("execution_safety_contract")
    if isinstance(existing, dict) and existing:
        return existing
    safety = _jsonish(row.get("safety_checks_json"))
    if not isinstance(safety, dict) or not safety:
        raw_broker = _jsonish(row.get("raw_broker_json"))
        candidate = raw_broker.get("execution_safety_contract") if isinstance(raw_broker, dict) else None
        safety = candidate if isinstance(candidate, dict) else {}
    if not safety:
        return None
    issues = safety.get("issues")
    portfolio_transition_contract = safety.get("portfolio_transition_contract")
    if not isinstance(portfolio_transition_contract, dict):
        portfolio_transition_contract = {}
    return {
        "operator_approval_required": bool(safety.get("operator_approval_required")),
        "operator_approval_status": str(safety.get("operator_approval_status") or "missing"),
        "broker_reconciliation_required": bool(safety.get("broker_reconciliation_required")),
        "broker_reconciliation_status": str(safety.get("broker_reconciliation_status") or "not_run"),
        "broker_identity_required": bool(safety.get("broker_identity_required", True)),
        "broker_identity_status": str(safety.get("broker_identity_status") or "not_checked"),
        "broker_identity_resolved": bool(safety.get("broker_identity_resolved")),
        "broker_identity_error": _json_ready(safety.get("broker_identity_error")),
        "live_evidence_required": bool(safety.get("live_evidence_required", True)),
        "live_evidence_status": str(safety.get("live_evidence_status") or "missing"),
        "live_evidence_successful_runs": _json_ready(safety.get("live_evidence_successful_runs") or 0),
        "live_evidence_min_successful_runs": _json_ready(safety.get("live_evidence_min_successful_runs") or 3),
        "live_submission_allowed": bool(safety.get("live_submission_allowed")),
        "live_allowance_allowed_at": _json_ready(safety.get("live_allowance_allowed_at")),
        "live_allowance_review_id": _json_ready(safety.get("live_allowance_review_id")),
        "post_live_allowance_approval_required": bool(safety.get("post_live_allowance_approval_required", bool(safety.get("live_allowance_allowed_at")))),
        "post_live_allowance_approval_status": str(safety.get("post_live_allowance_approval_status") or ("missing" if safety.get("live_allowance_allowed_at") else "not_required")),
        "post_live_allowance_approval_decision_id": _json_ready(safety.get("post_live_allowance_approval_decision_id")),
        "post_live_allowance_approval_decided_at": _json_ready(safety.get("post_live_allowance_approval_decided_at")),
        "source": safety.get("source"),
        "portfolio_transition_contract": _json_ready(portfolio_transition_contract),
        "issues": [str(item) for item in issues if str(item or "").strip()] if isinstance(issues, list) else [],
    }


def _with_execution_safety_contracts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        contract = _execution_safety_contract_from_row(item)
        if contract:
            item["execution_safety_contract"] = contract
        out.append(item)
    return out


def _execution_approval_blockers(row: dict[str, Any], contract: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    status = str(row.get("execution_status") or "").strip().lower()
    if status and status != "planned":
        blockers.append(f"Execution status is {status}.")
    if contract.get("operator_approval_required") and str(contract.get("operator_approval_status") or "").strip().lower() not in {"approved"}:
        blockers.append(f"Operator approval is {contract.get('operator_approval_status') or 'missing'}.")
    if contract.get("broker_reconciliation_required") and str(contract.get("broker_reconciliation_status") or "").strip().lower() not in {"passed", "ok", "reconciled"}:
        blockers.append(f"Broker reconciliation is {contract.get('broker_reconciliation_status') or 'not_run'}.")
    evidence_runs = pd.to_numeric(contract.get("live_evidence_successful_runs"), errors="coerce")
    if pd.isna(evidence_runs):
        evidence_runs = 0
    evidence_min = pd.to_numeric(contract.get("live_evidence_min_successful_runs"), errors="coerce")
    if pd.isna(evidence_min):
        evidence_min = 3
    if contract.get("live_evidence_required", True) and str(contract.get("live_evidence_status") or "").strip().lower() not in {"passed", "ready", "approved"}:
        blockers.append(f"Live evidence checklist is {contract.get('live_evidence_status') or 'missing'}.")
    if contract.get("live_evidence_required", True) and int(evidence_runs) < int(evidence_min):
        blockers.append(f"Live evidence has {int(evidence_runs)} successful run(s); requires {int(evidence_min)}.")
    if not bool(contract.get("live_submission_allowed")):
        blockers.append("Live submission is not allowed by the safety contract.")
    if bool(contract.get("live_submission_allowed")) and bool(contract.get("post_live_allowance_approval_required", bool(contract.get("live_allowance_allowed_at")))):
        post_allowance_status = str(contract.get("post_live_allowance_approval_status") or "").strip().lower()
        if post_allowance_status not in {"approved", "operator_approved"}:
            allowed_at = contract.get("live_allowance_allowed_at")
            blockers.append(
                "Fresh operator approval after live allowance is required before live submission"
                + (f"; live allowance was recorded at {allowed_at}." if allowed_at else ".")
            )
    for issue in contract.get("issues") or []:
        text = _text(issue)
        if text:
            blockers.append(text)
    return list(dict.fromkeys(blockers))
























































def _load_latest_company_memory_reviews(symbols: list[str]) -> dict[str, dict[str, Any]]:
    normalized = sorted({str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()})
    if not normalized:
        return {}
    try:
        if not _table_exists(COMPANY_MEMORY_REVIEWS_TABLE):
            return {}
    except Exception as exc:
        _record_operator_local_fallback(
            source=COMPANY_MEMORY_REVIEWS_TABLE,
            fallback_type="operator_api_company_memory_table_lookup_failed",
            severity="warn",
            reason="Operator API could not check company-memory review table availability and skipped memory-review enrichment.",
            error=exc,
            metadata={"symbol_count": len(normalized)},
        )
        return {}
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
                review_status,
                fallback_used,
                error,
                payload_json,
                load_ts
            FROM {COMPANY_MEMORY_REVIEWS_TABLE}
            WHERE UPPER(TRIM(symbol)) = ANY(%s)
            ORDER BY UPPER(TRIM(symbol)), review_date DESC, load_ts DESC NULLS LAST
            """,
            params=(normalized,),
            retries=2,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        _record_operator_local_fallback(
            source=COMPANY_MEMORY_REVIEWS_TABLE,
            fallback_type="operator_api_company_memory_reviews_load_failed",
            severity="warn",
            reason="Operator API could not load latest company-memory reviews and returned rows without memory-review enrichment.",
            error=exc,
            metadata={"symbol_count": len(normalized)},
        )
        return {}
    out: dict[str, dict[str, Any]] = {}
    if df.empty:
        return out
    for row in df.to_dict(orient="records"):
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        payload = _jsonish(row.get("payload_json"))
        evidence_source_contract = payload.get("evidence_source_contract") if isinstance(payload, dict) else None
        review = {
            "review_date": _ts(row.get("review_date")),
            "recommended_signal": _text(row.get("recommended_signal")),
            "confidence": row.get("confidence"),
            "conviction_score": row.get("conviction_score"),
            "summary": _text(row.get("summary")),
            "thesis": _text(row.get("thesis")),
            "risk_flags": _jsonish(row.get("risk_flags_json")) or [],
            "evidence_used": _jsonish(row.get("evidence_used_json")) or [],
            "wait_for": _jsonish(row.get("wait_for_json")) or [],
            "deterministic_boundary": _text(row.get("deterministic_boundary")),
            "authority_scope": _text(row.get("authority_scope")) or "review_input_only",
            "model_name": _text(row.get("model_name")),
            "review_status": _text(row.get("review_status")),
            "fallback_used": _boolish(row.get("fallback_used")),
            "error": _text(row.get("error")),
            "evidence_source_contract": evidence_source_contract,
            "load_ts": _ts(row.get("load_ts")),
        }
        out[symbol] = {key: value for key, value in review.items() if value not in (None, "", [], {})}
    return out


def _with_company_memory_reviews(rows: list[dict[str, Any]], *, reviews: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    symbols = [str(row.get("symbol") or row.get("ticker") or "") for row in rows if isinstance(row, dict)]
    reviews = reviews if reviews is not None else _load_latest_company_memory_reviews(symbols)
    if not reviews:
        return rows
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        symbol = str(item.get("symbol") or item.get("ticker") or "").strip().upper()
        review = reviews.get(symbol)
        if review:
            item["company_memory_review"] = review
            reason = _jsonish(item.get("recommendation_reason"))
            if isinstance(reason, dict):
                evidence = reason.get("evidence") if isinstance(reason.get("evidence"), dict) else {}
                reason["evidence"] = {**evidence, "company_memory": review}
                item["recommendation_reason"] = reason
        out.append(item)
    return out


def _feature_freshness_summary_from_contract(contract: Any) -> dict[str, Any] | None:
    payload = _jsonish(contract)
    if not isinstance(payload, dict) or not payload:
        return None
    blockers = payload.get("blockers") if isinstance(payload.get("blockers"), list) else []
    inputs = payload.get("inputs") if isinstance(payload.get("inputs"), list) else []
    required_inputs = [
        {
            "input_key": row.get("input_key"),
            "label": row.get("label"),
            "status": row.get("status"),
            "reason": row.get("reason"),
            "latest_at": row.get("latest_at"),
            "age_days": row.get("age_days"),
            "required": True,
        }
        for row in inputs
        if isinstance(row, dict) and row.get("required")
    ]
    return {
        "symbol": payload.get("symbol"),
        "status": payload.get("status") or ("blocked" if blockers else "unknown"),
        "counts": payload.get("counts") if isinstance(payload.get("counts"), dict) else {},
        "blockers": blockers,
        "required_inputs": required_inputs,
        "asof_date": payload.get("asof_date"),
        "captured_at": payload.get("captured_at"),
        "source": "decision_time_snapshot",
    }


def _attach_feature_freshness_summaries(rows: list[dict[str, Any]], *, live_summaries: dict[str, dict[str, Any]] | None = None) -> None:
    live_summaries = live_summaries or {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        persisted = _feature_freshness_summary_from_contract(row.get("feature_freshness"))
        if persisted:
            row["feature_freshness_summary"] = persisted
            continue
        symbol_key = str(row.get("symbol") or row.get("ticker") or "").strip().upper()
        if symbol_key and symbol_key in live_summaries:
            row["feature_freshness_summary"] = {**live_summaries[symbol_key], "source": "current_live_check"}


FEATURE_GATE_EFFECT_LABELS = {
    "rules": "Rules gate",
    "risk": "Risk gate",
    "portfolio": "Portfolio gate",
    "lifecycle": "Lifecycle gate",
    "actions": "Final action gate",
}


FEATURE_GATE_EFFECT_SUMMARIES = {
    "rules": "Immediate PASS_NOW candidate was kept on watch until required inputs refresh.",
    "risk": "Automatic allocation was moved to manual review and suggested allocation was zeroed.",
    "portfolio": "Approved or trimmed capital was deferred until required inputs refresh.",
    "lifecycle": "Lifecycle output was preserved, but price confidence warning was attached.",
    "actions": "Positive broker-capable action was downgraded to Manual Review with no broker execution.",
}


def _normalise_blocked_feature_inputs(value: Any) -> list[dict[str, Any]]:
    rows = value if isinstance(value, list) else []
    out: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            input_key = _text(row.get("input_key")) or _text(row.get("key")) or _text(row.get("name"))
            label = _text(row.get("label")) or input_key
            status = _text(row.get("status"))
            reason = _text(row.get("reason"))
            if input_key or label:
                out.append({key: item for key, item in {"input_key": input_key, "label": label, "status": status, "reason": reason}.items() if item not in (None, "")})
        else:
            text = _text(row)
            if text:
                out.append({"input_key": text, "label": text})
    return out


def _feature_gate_contexts_from_row(row: dict[str, Any]) -> list[dict[str, Any]]:
    contexts: list[dict[str, Any]] = []
    for key in ["raw_context_json", "raw_context", "context_snapshot_json", "context_snapshot"]:
        value = _jsonish(row.get(key))
        if isinstance(value, dict) and value:
            contexts.append(value)
    reason = _jsonish(row.get("recommendation_reason"))
    if isinstance(reason, dict):
        evidence = reason.get("evidence") if isinstance(reason.get("evidence"), dict) else {}
        freshness = evidence.get("feature_freshness") if isinstance(evidence.get("feature_freshness"), dict) else {}
        if freshness:
            contexts.append(freshness)
    return contexts


def _build_feature_gate_effects(row: dict[str, Any]) -> list[dict[str, Any]]:
    effects: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    contexts = _feature_gate_contexts_from_row(row)
    for context in contexts:
        gate = _text(context.get("feature_freshness_gate"))
        if not gate:
            continue
        stage = (_text(context.get("feature_freshness_stage")) or ("actions" if gate == "blocked_positive_broker_action" else "")).lower()
        stage = stage if stage in FEATURE_GATE_EFFECT_LABELS else "actions"
        blockers = _normalise_blocked_feature_inputs(context.get("feature_freshness_blockers") or context.get("feature_freshness_blocked_inputs"))
        key = (stage, gate, _text(context.get("blocked_original_action_code")) or "")
        if key in seen:
            continue
        seen.add(key)
        effects.append(
            {
                "stage": stage,
                "label": FEATURE_GATE_EFFECT_LABELS.get(stage, "Feature gate"),
                "gate": gate,
                "status": _text(context.get("feature_freshness_status")) or "blocked",
                "gate_effect": _text(context.get("feature_freshness_gate_effect")) or None,
                "summary": FEATURE_GATE_EFFECT_SUMMARIES.get(stage, "Required feature inputs were blocked for this stage."),
                "blocked_inputs": blockers,
                "original_action": _text(context.get("blocked_original_action_code") or context.get("blocked_original_action")),
                "broker_execution_allowed": _boolish(context.get("broker_execution_allowed")),
                "source": "decision_context",
            }
        )
    if effects:
        return effects
    freshness_summary = _feature_freshness_summary_from_contract(row.get("feature_freshness")) or _jsonish(row.get("feature_freshness_summary"))
    action = (_text(row.get("action_code") or row.get("action") or row.get("next_action")) or "").upper()
    if isinstance(freshness_summary, dict) and str(freshness_summary.get("status") or "").lower() == "blocked" and action in {"MANUAL_REVIEW", "REVIEW"}:
        blockers = _normalise_blocked_feature_inputs(freshness_summary.get("blockers"))
        if blockers:
            effects.append(
                {
                    "stage": "actions",
                    "label": FEATURE_GATE_EFFECT_LABELS["actions"],
                    "gate": "blocked_positive_broker_action",
                    "status": "blocked",
                    "summary": FEATURE_GATE_EFFECT_SUMMARIES["actions"],
                    "blocked_inputs": blockers,
                    "original_action": None,
                    "broker_execution_allowed": False,
                    "source": freshness_summary.get("source") or "feature_freshness_summary",
                }
            )
    return effects


def _attach_feature_gate_effects(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        if not isinstance(row, dict):
            continue
        effects = _build_feature_gate_effects(row)
        if effects:
            row["feature_gate_effects"] = effects


def build_home_payload(*, asof_date: str | None = None, include_action_cards: bool = False) -> dict[str, Any]:
    required_section_names = ["summary", "runtime_processes", "cron_status", "sync_state"]
    section_names = [*required_section_names, "ts_forecast_paper_summary"]
    if include_action_cards:
        section_names.extend(["top_action_recommendations", "today_recommendations"])
    section_payload = load_operator_sections_payload(section_names, asof_date=asof_date, allow_missing=True)
    if section_payload is not None and all(name in section_payload for name in required_section_names):
        payload = section_payload
    else:
        payload = load_operator_payload(asof_date=asof_date)
    missing_sections = (
        payload.get("_snapshot", {}).get("missing_sections")
        if isinstance(payload.get("_snapshot"), dict)
        else []
    )
    ts_forecast_paper_summary = payload.get("ts_forecast_paper_summary") or []
    return {
        "generated_at": payload.get("generated_at"),
        "api_schema": _operator_api_schema("/api/home", schema_name="operator_home"),
        "asof_date": payload.get("asof_date"),
        "snapshot": _snapshot_payload(payload),
        "snapshot_warning": _snapshot_warning_payload(payload),
        "summary": payload.get("summary") or {},
        "runtime_processes": payload.get("runtime_processes") or [],
        "cron_status": payload.get("cron_status") or [],
        "sync_state": payload.get("sync_state") or [],
        "top_action_recommendations": _compact_home_rows(payload.get("top_action_recommendations"), limit=25) if include_action_cards else [],
        "today_recommendations": _compact_home_rows(payload.get("today_recommendations"), limit=25) if include_action_cards else [],
        "ts_forecast_paper_summary": ts_forecast_paper_summary[:10],
        "meta": {
            "include_action_cards": bool(include_action_cards),
            "action_card_contract": {
                "default": "omitted",
                "operator_note": "Home omits duplicated action/today cards by default. Use /api/actions and /api/portfolio for paged cards, or include_action_cards=true for legacy/debug reads.",
            },
            "optional_sections": {
                "ts_forecast_paper_summary": "missing_or_unavailable" if "ts_forecast_paper_summary" in (missing_sections or []) else "loaded",
            },
        },
    }


def build_actions_payload(
    *,
    asof_date: str | None = None,
    limit: int = 50,
    offset: int = 0,
    symbol: str | None = None,
    action: str | None = None,
    status: str | None = None,
    search: str | None = None,
    compact: bool = False,
    include_feature_freshness: bool = False,
    refresh_feature_freshness: bool = False,
) -> dict[str, Any]:
    payload = load_operator_sections_payload(
        ["top_action_recommendations", "action_recommendations", "alerts"],
        asof_date=asof_date,
    ) or load_operator_payload(asof_date=asof_date)
    top_actions = _filter_rows(payload.get("top_action_recommendations") or [], symbol=symbol, action=action, status=status, search=search)
    action_rows = _filter_rows(payload.get("action_recommendations") or [], symbol=symbol, action=action, status=status, search=search)
    alert_rows = _filter_rows(payload.get("alerts") or [], symbol=symbol, status=status, search=search)
    action_page, action_meta = _page_rows(action_rows, limit=limit, offset=offset)
    top_action_raw = top_actions[: _bounded_limit(limit, default=25)]
    alert_raw = alert_rows[: _bounded_limit(limit, default=25)]
    top_action_pagination = _limited_pagination_contract(top_actions, limit=limit, default=25)
    alert_pagination = _limited_pagination_contract(alert_rows, limit=limit, default=25)
    latest_prices = _latest_ohlcv_prices([
        str(row.get("symbol") or row.get("ticker") or "")
        for row in [*top_action_raw, *action_page, *alert_raw]
        if isinstance(row, dict)
    ])
    memory_reviews = _load_latest_company_memory_reviews([
        str(row.get("symbol") or row.get("ticker") or "")
        for row in [*top_action_raw, *action_page]
        if isinstance(row, dict)
    ])
    top_action_page = _with_company_memory_reviews(
        _with_action_queue_contracts(_with_execution_safety_contracts(_with_latest_prices(top_action_raw, price_field="current_price", prices=latest_prices))),
        reviews=memory_reviews,
    )
    action_page = _with_company_memory_reviews(
        _with_action_queue_contracts(_with_execution_safety_contracts(_with_latest_prices(action_page, price_field="current_price", prices=latest_prices))),
        reviews=memory_reviews,
    )
    if include_feature_freshness:
        rows_for_freshness = [*top_action_page, *action_page]
        needs_live = [
            str(row.get("symbol") or row.get("ticker") or "")
            for row in rows_for_freshness
            if refresh_feature_freshness and isinstance(row, dict) and not _feature_freshness_summary_from_contract(row.get("feature_freshness"))
        ]
        live_summaries = (
            build_required_feature_freshness_summaries(
                needs_live,
                asof_date=asof_date or payload.get("asof_date"),
            )
            if needs_live
            else {}
        )
        _attach_feature_freshness_summaries(rows_for_freshness, live_summaries=live_summaries)
        _attach_feature_gate_effects(rows_for_freshness)
    _attach_action_final_state_trust(top_action_page)
    _attach_action_final_state_trust(action_page)
    alert_page = _with_latest_prices(alert_raw, price_field="last_price", prices=latest_prices)
    top_action_response = _compact_action_rows(top_action_page, compact=compact)
    action_response = _compact_action_rows(action_page, compact=compact)
    alert_response = _compact_list_rows(alert_page, compact=compact)
    return {
        "generated_at": payload.get("generated_at"),
        "api_schema": _operator_api_schema("/api/actions", schema_name="operator_actions"),
        "asof_date": payload.get("asof_date"),
        "snapshot": _snapshot_payload(payload),
        "snapshot_warning": _snapshot_warning_payload(payload),
        "top_action_recommendations": top_action_response,
        "action_recommendations": action_response,
        "alerts": alert_response,
        "pagination": {
            "primary": "action_recommendations",
            "top_action_recommendations": top_action_pagination,
            "action_recommendations": _pagination_contract(action_meta),
            "alerts": alert_pagination,
        },
        "meta": {
            "top_action_recommendations": {"total": len(top_actions), "returned": top_action_pagination["returned_count"], **_payload_size_meta(top_action_response)},
            "action_recommendations": {**action_meta, **_payload_size_meta(action_response)},
            "alerts": {"total": len(alert_rows), "returned": alert_pagination["returned_count"], **_payload_size_meta(alert_response)},
            "filters": {
                "symbol": symbol,
                "action": action,
                "status": status,
                "search": search,
                "compact": compact,
                "include_feature_freshness": include_feature_freshness,
                "refresh_feature_freshness": refresh_feature_freshness,
            },
            "feature_freshness_contract": {
                "include_feature_freshness": include_feature_freshness,
                "refresh_feature_freshness": refresh_feature_freshness,
                "default_source": "decision_time_snapshot",
                "live_refresh_source": "current_live_check",
                "operator_note": "Action list responses use persisted decision-time freshness unless refresh_feature_freshness=true is explicitly requested.",
            },
        },
    }


def build_action_conflict_rules_payload() -> dict[str, Any]:
    if not _table_exists(ACTION_CONFLICT_RULES_TABLE):
        return {
            "generated_at": pd.Timestamp.utcnow().isoformat(),
            "api_schema": _operator_api_schema("/api/action-conflict-rules", schema_name="action_conflict_rules"),
            "rules": [],
            "unresolved_conflicts": [],
            "row_count": 0,
        }
    df = sql_to_df(
        f"""
        SELECT *
        FROM {ACTION_CONFLICT_RULES_TABLE}
        ORDER BY enabled DESC, priority DESC, rule_id
        """,
        retries=2,
    )
    unresolved = pd.DataFrame()
    if _table_exists(ACTION_CONFLICTS_TABLE):
        unresolved = sql_to_df(
            f"""
            SELECT *
            FROM {ACTION_CONFLICTS_TABLE}
            WHERE asof_date = (SELECT MAX(asof_date) FROM {ACTION_CONFLICTS_TABLE})
              AND (
                COALESCE(requires_manual_resolution, FALSE) = TRUE
                OR COALESCE(resolution_status, '') IN ('unresolved', 'manual_required')
              )
            ORDER BY load_ts DESC NULLS LAST, symbol
            LIMIT 25
            """,
            retries=2,
        )
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema("/api/action-conflict-rules", schema_name="action_conflict_rules"),
        "rules": _json_ready(df.to_dict(orient="records")),
        "unresolved_conflicts": _json_ready(unresolved.to_dict(orient="records")),
        "row_count": int(len(df)),
        "unresolved_count": int(len(unresolved)),
    }


def _promoted_conflict_key(conflict: dict[str, Any]) -> str:
    parts = [
        conflict.get("asof_date"),
        conflict.get("symbol"),
        conflict.get("winning_action_code"),
        conflict.get("losing_action_code"),
        conflict.get("winning_source"),
        conflict.get("losing_source"),
        conflict.get("losing_setup_id"),
        conflict.get("losing_unique_id"),
    ]
    return ":".join("" if part is None else str(part) for part in parts)


def _normalize_action_pair_condition(value: Any) -> dict[str, str]:
    condition = value
    if isinstance(value, str):
        try:
            condition = json.loads(value)
        except Exception as exc:
            raise ValueError("condition_json must be valid JSON") from exc
    if not isinstance(condition, dict):
        raise ValueError("condition_json must be an object")
    condition_type = str(condition.get("condition_type") or "action_pair_exact").strip().lower()
    if condition_type not in {"action_pair", "action_pair_exact"}:
        raise ValueError("only action_pair and action_pair_exact conflict rule conditions are supported")
    out = {
        "condition_type": condition_type,
        "winning_action_code": str(condition.get("winning_action_code") or "").strip().upper(),
        "losing_action_code": str(condition.get("losing_action_code") or "").strip().upper(),
    }
    if condition_type == "action_pair_exact":
        out["winning_source"] = str(condition.get("winning_source") or "").strip().lower()
        out["losing_source"] = str(condition.get("losing_source") or "").strip().lower()
    if not out["winning_action_code"] or not out["losing_action_code"]:
        raise ValueError("condition_json requires winning_action_code and losing_action_code")
    return out


def promote_action_conflict_rule_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    conflict = payload.get("conflict")
    if not isinstance(conflict, dict):
        raise ValueError("conflict must be an object")
    condition_payload = payload.get("condition_json", payload.get("condition"))
    if condition_payload is None:
        condition_payload = {
            "condition_type": "action_pair_exact",
            "winning_action_code": conflict.get("winning_action_code"),
            "losing_action_code": conflict.get("losing_action_code"),
            "winning_source": conflict.get("winning_source"),
            "losing_source": conflict.get("losing_source"),
        }
    condition = _normalize_action_pair_condition(condition_payload)
    reason = str(payload.get("resolution_reason") or conflict.get("resolution_reason") or conflict.get("lost_reason") or "").strip()
    if not reason:
        raise ValueError("resolution_reason is required")
    if len(reason) > 2_000:
        raise ValueError("resolution_reason must be 2000 characters or fewer")
    resolution_action = str(payload.get("resolution_action") or "keep_winner").strip()
    if resolution_action not in {"keep_winner", "collapse_duplicate", "manual_resolution_required"}:
        raise ValueError("resolution_action must be keep_winner, collapse_duplicate, or manual_resolution_required")
    enabled = payload.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")
    conflict_key = _promoted_conflict_key(conflict)
    generated_rule_id = "MANUAL_" + str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(condition, sort_keys=True)))[:8].upper()
    rule_id = str(payload.get("rule_id") or generated_rule_id).strip().upper().replace(" ", "_")
    if not rule_id:
        raise ValueError("rule_id is required")
    rule_name = str(payload.get("rule_name") or f"{condition['winning_action_code']} beats {condition['losing_action_code']}").strip()
    promotion_note = str(payload.get("promotion_note") or "Promoted from unresolved operator action-conflict review.").strip()

    ensure_trace_tables()
    now = pd.Timestamp.utcnow()
    row: tuple[Any, ...] | None = None
    columns: list[str] = []

    def _promote_conflict_rule() -> None:
        nonlocal row, columns
        with db_session() as (_, cur):
            cur.execute(
                f"""
                INSERT INTO {ACTION_CONFLICT_RULES_TABLE}
                    (rule_id, rule_name, rule_scope, resolution_action, resolution_reason, enabled, priority,
                     condition_json, promoted_from_conflict_key, promoted_by, promotion_note, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (rule_id) DO UPDATE SET
                    rule_name = EXCLUDED.rule_name,
                    rule_scope = EXCLUDED.rule_scope,
                    resolution_action = EXCLUDED.resolution_action,
                    resolution_reason = EXCLUDED.resolution_reason,
                    enabled = EXCLUDED.enabled,
                    priority = EXCLUDED.priority,
                    condition_json = EXCLUDED.condition_json,
                    promoted_from_conflict_key = EXCLUDED.promoted_from_conflict_key,
                    promoted_by = EXCLUDED.promoted_by,
                    promotion_note = EXCLUDED.promotion_note,
                    updated_at = EXCLUDED.updated_at
                RETURNING *
                """,
                (
                    rule_id,
                    rule_name,
                    "manual_resolution",
                    resolution_action,
                    reason,
                    enabled,
                    int(payload.get("priority") or 25),
                    json.dumps(condition, ensure_ascii=False, sort_keys=True),
                    conflict_key,
                    str(payload.get("promoted_by") or "operator"),
                    promotion_note,
                    now,
                    now,
                ),
            )
            row = cur.fetchone()
            columns = [desc[0] for desc in cur.description]

    execute_db_operation(
        _promote_conflict_rule,
        operation_name="operator_api:promote_action_conflict_rule",
    )
    if row is None:
        raise RuntimeError("conflict rule promotion did not return a row")
    return {
        "status": "promoted",
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema("/api/action-conflict-rules/promote", schema_name="action_conflict_rule_promotion"),
        "rule": _json_ready(dict(zip(columns, row))),
        "condition": condition,
        "note": "Promoted conflict rules support exact action/source matches or broader action-only pairs. They are disabled by default unless enabled is explicitly true; historical action rows are not rewritten.",
    }


def update_action_conflict_rule_payload(rule_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    normalized_rule_id = str(rule_id or "").strip()
    if not normalized_rule_id:
        raise ValueError("rule_id is required")
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    allowed_fields = {"enabled", "resolution_reason", "reason", "condition", "condition_json"}
    unknown_fields = sorted(set(payload) - allowed_fields)
    if unknown_fields:
        raise ValueError(f"unsupported conflict rule field(s): {', '.join(unknown_fields)}")

    updates: list[str] = []
    params: list[Any] = []
    if "enabled" in payload:
        if not isinstance(payload.get("enabled"), bool):
            raise ValueError("enabled must be a boolean")
        updates.append("enabled = %s")
        params.append(bool(payload["enabled"]))
    if "resolution_reason" in payload or "reason" in payload:
        reason_value = payload.get("resolution_reason", payload.get("reason"))
        reason = str(reason_value or "").strip()
        if not reason:
            raise ValueError("resolution_reason is required when editing rule text")
        if len(reason) > 2_000:
            raise ValueError("resolution_reason must be 2000 characters or fewer")
        updates.append("resolution_reason = %s")
        params.append(reason)
    if "condition_json" in payload or "condition" in payload:
        condition_value = payload.get("condition_json", payload.get("condition"))
        condition = _normalize_action_pair_condition(condition_value)
        updates.append("condition_json = %s")
        params.append(json.dumps(condition, ensure_ascii=False, sort_keys=True))
    if not updates:
        raise ValueError("at least one editable field is required")

    ensure_trace_tables()
    now = pd.Timestamp.utcnow()
    updates.append("updated_at = %s")
    params.append(now)
    params.append(normalized_rule_id)
    row: tuple[Any, ...] | None = None
    columns: list[str] = []

    def _update_conflict_rule() -> None:
        nonlocal row, columns
        with db_session() as (_, cur):
            cur.execute(
                f"""
                UPDATE {ACTION_CONFLICT_RULES_TABLE}
                SET {", ".join(updates)}
                WHERE rule_id = %s
                RETURNING *
                """,
                tuple(params),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"Unknown conflict rule: {normalized_rule_id}")
            columns = [desc[0] for desc in cur.description]

    execute_db_operation(
        _update_conflict_rule,
        operation_name="operator_api:update_action_conflict_rule",
    )
    return {
        "status": "updated",
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema("/api/action-conflict-rules/{rule_id}", schema_name="action_conflict_rule_update"),
        "rule": _json_ready(dict(zip(columns, row))),
        "note": "Rule edits affect future action-candidate ranking/conflict-rule reads where the edited field is consumed; historical action rows are not rewritten.",
    }


def build_signal_refresh_payload(
    *,
    limit: int = 50,
    offset: int = 0,
    symbol: str | None = None,
    status: str | None = None,
    search: str | None = None,
    compact: bool = False,
) -> dict[str, Any]:
    if not _table_exists(SIGNAL_REFRESH_TABLE):
        return {
            "generated_at": pd.Timestamp.utcnow().isoformat(),
            "api_schema": _operator_api_schema("/api/signal-refresh", schema_name="signal_refresh"),
            "status": "ok",
            "signals": [],
            "meta": {"signals": {"total": 0, "returned": 0}, "filters": {"symbol": symbol, "status": status, "search": search, "compact": compact}},
        }
    clauses = ["1 = 1"]
    params: list[Any] = []
    if symbol:
        clauses.append("UPPER(TRIM(symbol)) = %s")
        params.append(str(symbol).strip().upper())
    if status and str(status).lower() not in {"all", "*"}:
        clauses.append("LOWER(COALESCE(signal_status, signal_action, '')) LIKE %s")
        params.append(f"%{str(status).strip().lower()}%")
    if search:
        clauses.append("(symbol ILIKE %s OR COALESCE(signal_action, '') ILIKE %s OR COALESCE(action_reason, '') ILIKE %s OR COALESCE(reason, '') ILIKE %s)")
        needle = f"%{str(search).strip()}%"
        params.extend([needle, needle, needle, needle])
    where_sql = " AND ".join(clauses)
    total_df = sql_to_df(f"SELECT COUNT(*) AS total FROM {SIGNAL_REFRESH_TABLE} WHERE {where_sql}", params=tuple(params) if params else None)
    total = int(total_df["total"].iloc[0]) if not total_df.empty else 0
    row_limit = _bounded_limit(limit, default=50)
    row_offset = max(0, int(offset))
    signal_columns = _table_columns(SIGNAL_REFRESH_TABLE)
    effect_type_expr = "effect_type" if "effect_type" in signal_columns else "NULL::TEXT AS effect_type"
    effect_summary_expr = "effect_summary" if "effect_summary" in signal_columns else "NULL::TEXT AS effect_summary"
    previous_action_expr = "previous_action" if "previous_action" in signal_columns else "NULL::TEXT AS previous_action"
    action_changed_expr = "action_changed" if "action_changed" in signal_columns else "NULL::BOOLEAN AS action_changed"
    authority_scope_expr = "authority_scope" if "authority_scope" in signal_columns else "'review_input_only'::TEXT AS authority_scope"
    portfolio_authority_expr = "portfolio_authority" if "portfolio_authority" in signal_columns else "'none'::TEXT AS portfolio_authority"
    broker_execution_allowed_expr = "broker_execution_allowed" if "broker_execution_allowed" in signal_columns else "FALSE::BOOLEAN AS broker_execution_allowed"
    full_advisory_required_expr = "full_advisory_required" if "full_advisory_required" in signal_columns else "TRUE::BOOLEAN AS full_advisory_required"
    action_payload_expr = "action_payload_json" if "action_payload_json" in signal_columns else "NULL::TEXT AS action_payload_json"
    rows = sql_to_df(
        f"""
        SELECT
            refresh_id,
            refreshed_at,
            asof_date,
            symbol,
            unique_id,
            reason,
            signal_action,
            signal_status,
            signal_source,
            confidence,
            action_reason,
            {effect_type_expr},
            {effect_summary_expr},
            {previous_action_expr},
            {action_changed_expr},
            {authority_scope_expr},
            {portfolio_authority_expr},
            {broker_execution_allowed_expr},
            {full_advisory_required_expr},
            {action_payload_expr},
            trace_id,
            dry_run,
            load_ts
        FROM {SIGNAL_REFRESH_TABLE}
        WHERE {where_sql}
        ORDER BY refreshed_at DESC NULLS LAST, load_ts DESC NULLS LAST
        LIMIT %s OFFSET %s
        """,
        params=tuple([*params, row_limit, row_offset]),
    )
    page_rows = rows.to_dict(orient="records") if not rows.empty else []
    for row in page_rows:
        row["router_context"] = _compact_signal_router_context(row.get("action_payload_json"))
        next_step = _signal_refresh_next_step(row)
        row["operator_next_step_kind"] = next_step["kind"]
        row["operator_next_step"] = next_step["text"]
        if compact:
            row.pop("action_payload_json", None)
    next_offset = row_offset + row_limit if row_offset + row_limit < total else None
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema("/api/signal-refresh", schema_name="signal_refresh"),
        "status": "ok",
        "signals": _compact_list_rows(page_rows, compact=compact),
        "meta": {
            "signals": {"total": total, "returned": len(page_rows), "offset": row_offset, "limit": row_limit, "next_offset": next_offset},
            "filters": {"symbol": symbol, "status": status, "search": search, "compact": compact},
        },
    }










def build_portfolio_payload(
    *,
    asof_date: str | None = None,
    limit: int = 50,
    offset: int = 0,
    symbol: str | None = None,
    status: str | None = None,
    search: str | None = None,
    compact: bool = False,
    bucket: str | None = None,
) -> dict[str, Any]:
    valid_buckets = {"today_recommendations", "current_recommendations", "exited_recommendations", "portfolio", "lifecycle"}
    requested_bucket = str(bucket or "").strip()
    if requested_bucket and requested_bucket not in valid_buckets:
        requested_bucket = ""
    payload = load_operator_payload(asof_date=asof_date)
    today_rows = _filter_rows(payload.get("today_recommendations") or [], symbol=symbol, status=status, search=search)
    current_rows = _filter_rows(payload.get("current_recommendations") or [], symbol=symbol, status=status, search=search)
    exited_rows = _filter_rows(payload.get("exited_recommendations") or [], symbol=symbol, status=status, search=search)
    portfolio_rows = _filter_rows(payload.get("portfolio") or [], symbol=symbol, status=status, search=search)
    lifecycle_rows = _filter_rows(payload.get("lifecycle") or [], symbol=symbol, status=status, search=search)
    portfolio_page, portfolio_meta = _page_rows(portfolio_rows, limit=limit, offset=offset)
    today_pagination = _limited_pagination_contract(today_rows, limit=limit, default=25)
    current_pagination = _limited_pagination_contract(current_rows, limit=limit, default=25)
    exited_pagination = _limited_pagination_contract(exited_rows, limit=limit, default=25)
    lifecycle_pagination = _limited_pagination_contract(lifecycle_rows, limit=limit, default=25)
    include_all = not requested_bucket
    include_today = include_all or requested_bucket == "today_recommendations"
    include_current = include_all or requested_bucket == "current_recommendations"
    include_exited = include_all or requested_bucket == "exited_recommendations"
    include_portfolio = include_all or requested_bucket == "portfolio"
    include_lifecycle = include_all or requested_bucket == "lifecycle"
    today_response = _compact_action_rows(today_rows[: _bounded_limit(limit, default=25)], compact=compact) if include_today else []
    current_response = _compact_action_rows(current_rows[: _bounded_limit(limit, default=25)], compact=compact) if include_current else []
    exited_response = _compact_action_rows(exited_rows[: _bounded_limit(limit, default=25)], compact=compact) if include_exited else []
    portfolio_response = _compact_list_rows(portfolio_page, compact=compact) if include_portfolio else []
    lifecycle_response = _compact_list_rows(lifecycle_rows[: _bounded_limit(limit, default=25)], compact=compact) if include_lifecycle else []
    return {
        "generated_at": payload.get("generated_at"),
        "api_schema": _operator_api_schema("/api/portfolio", schema_name="operator_portfolio"),
        "asof_date": payload.get("asof_date"),
        "snapshot": _snapshot_payload(payload),
        "snapshot_warning": _snapshot_warning_payload(payload),
        "today_recommendations": today_response,
        "current_recommendations": current_response,
        "exited_recommendations": exited_response,
        "portfolio": portfolio_response,
        "lifecycle": lifecycle_response,
        "pagination": {
            "primary": "portfolio",
            "today_recommendations": today_pagination,
            "current_recommendations": current_pagination,
            "exited_recommendations": exited_pagination,
            "portfolio": _pagination_contract(portfolio_meta),
            "lifecycle": lifecycle_pagination,
        },
        "meta": {
            "today_recommendations": {"total": len(today_rows), "returned": today_pagination["returned_count"], **_payload_size_meta(today_response)},
            "current_recommendations": {"total": len(current_rows), "returned": current_pagination["returned_count"], **_payload_size_meta(current_response)},
            "exited_recommendations": {"total": len(exited_rows), "returned": exited_pagination["returned_count"], **_payload_size_meta(exited_response)},
            "portfolio": {**portfolio_meta, **_payload_size_meta(portfolio_response)},
            "lifecycle": {"total": len(lifecycle_rows), "returned": lifecycle_pagination["returned_count"], **_payload_size_meta(lifecycle_response)},
            "filters": {"symbol": symbol, "status": status, "search": search, "compact": compact, "bucket": requested_bucket or None},
            "bucket_contract": {
                "requested_bucket": requested_bucket or None,
                "included_sections": [
                    section
                    for section, include in {
                        "today_recommendations": include_today,
                        "current_recommendations": include_current,
                        "exited_recommendations": include_exited,
                        "portfolio": include_portfolio,
                        "lifecycle": include_lifecycle,
                    }.items()
                    if include
                ],
                "operator_note": "Use bucket=<section> for normal paged UI reads. Omit bucket only when the caller needs all portfolio sections at once.",
            },
        },
    }


TS_FORECAST_WATCH_COMPACT_FIELDS = {
    "symbol",
    "asof_date",
    "model_name",
    "horizon_days",
    "forecast_price",
    "forecast_return_pct",
    "probability_positive_pct",
    "signal_quality_pct",
    "blended_score_pct",
    "upside_return_p90_pct",
    "downside_return_p10_pct",
    "watch_status",
    "combined_state",
    "action_hint",
    "research_only",
    "source_name",
    "source_slug",
    "sort_score",
    "load_ts",
    "watch_reason",
    "window_summary",
    "history_summary",
    "is_active_ts_watch",
}


def _compact_ts_forecast_watch_rows(rows: list[dict[str, Any]], *, compact: bool = True) -> list[dict[str, Any]]:
    if not compact:
        return rows
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        compact_row = {
            key: _trim_home_value(row.get(key), max_text=260, max_list=4, max_depth=2)
            for key in TS_FORECAST_WATCH_COMPACT_FIELDS
            if row.get(key) not in (None, "", [], {})
        }
        out.append({key: value for key, value in compact_row.items() if value not in (None, "", [], {})})
    return out


def _compact_ts_forecast_summary_rows(rows: list[dict[str, Any]], *, compact: bool = True) -> list[dict[str, Any]]:
    if not compact:
        return rows
    return [
        _trim_home_value(row, max_text=260, max_list=6, max_depth=2)
        for row in rows
        if isinstance(row, dict)
    ]


def build_watchlist_payload(
    *,
    asof_date: str | None = None,
    section: str | None = None,
    limit: int = 25,
    compact: bool = True,
) -> dict[str, Any]:
    valid_sections = {
        "watch_recommendations",
        "watchlist",
        "ts_watch_recommendations",
        "ts_forecast_watch",
        "ts_forecast_eval_summary",
        "ts_forecast_paper_summary",
    }
    requested_section = str(section or "").strip()
    if requested_section and requested_section not in valid_sections:
        requested_section = ""
    include_all = not requested_section
    bounded_limit = _bounded_limit(limit, default=25)
    payload = load_operator_payload(asof_date=asof_date)
    raw_sections = {
        "watch_recommendations": payload.get("watch_recommendations") or [],
        "watchlist": payload.get("watchlist") or [],
        "ts_watch_recommendations": payload.get("ts_watch_recommendations") or [],
        "ts_forecast_watch": payload.get("ts_forecast_watch") or [],
        "ts_forecast_eval_summary": payload.get("ts_forecast_eval_summary") or [],
        "ts_forecast_paper_summary": payload.get("ts_forecast_paper_summary") or [],
    }
    included = {
        key: (include_all or requested_section == key)
        for key in raw_sections
    }
    watch_recommendations = _compact_action_rows(raw_sections["watch_recommendations"][:bounded_limit], compact=compact) if included["watch_recommendations"] else []
    watchlist_rows = _compact_list_rows(raw_sections["watchlist"][:bounded_limit], compact=compact) if included["watchlist"] else []
    ts_watch_recommendations = _compact_list_rows(raw_sections["ts_watch_recommendations"][:bounded_limit], compact=compact) if included["ts_watch_recommendations"] else []
    ts_forecast_watch = _compact_ts_forecast_watch_rows(raw_sections["ts_forecast_watch"][:bounded_limit], compact=compact) if included["ts_forecast_watch"] else []
    ts_forecast_eval_summary = _compact_list_rows(raw_sections["ts_forecast_eval_summary"][:bounded_limit], compact=compact) if included["ts_forecast_eval_summary"] else []
    ts_forecast_paper_summary = _compact_ts_forecast_summary_rows(raw_sections["ts_forecast_paper_summary"][:bounded_limit], compact=compact) if included["ts_forecast_paper_summary"] else []
    return {
        "generated_at": payload.get("generated_at"),
        "api_schema": _operator_api_schema("/api/watchlist", schema_name="operator_watchlist"),
        "asof_date": payload.get("asof_date"),
        "snapshot": _snapshot_payload(payload),
        "snapshot_warning": _snapshot_warning_payload(payload),
        "watch_recommendations": watch_recommendations,
        "watchlist": watchlist_rows,
        "ts_watch_recommendations": ts_watch_recommendations,
        "ts_forecast_watch": ts_forecast_watch,
        "ts_forecast_eval_summary": ts_forecast_eval_summary,
        "ts_forecast_paper_summary": ts_forecast_paper_summary,
        "pagination": {
            key: _limited_pagination_contract(rows, limit=bounded_limit, default=25)
            for key, rows in raw_sections.items()
        },
        "meta": {
            "watch_recommendations": {"total": len(raw_sections["watch_recommendations"]), "returned": len(watch_recommendations), **_payload_size_meta(watch_recommendations)},
            "watchlist": {"total": len(raw_sections["watchlist"]), "returned": len(watchlist_rows), **_payload_size_meta(watchlist_rows)},
            "ts_watch_recommendations": {"total": len(raw_sections["ts_watch_recommendations"]), "returned": len(ts_watch_recommendations), **_payload_size_meta(ts_watch_recommendations)},
            "ts_forecast_watch": {"total": len(raw_sections["ts_forecast_watch"]), "returned": len(ts_forecast_watch), **_payload_size_meta(ts_forecast_watch)},
            "ts_forecast_eval_summary": {"total": len(raw_sections["ts_forecast_eval_summary"]), "returned": len(ts_forecast_eval_summary), **_payload_size_meta(ts_forecast_eval_summary)},
            "ts_forecast_paper_summary": {"total": len(raw_sections["ts_forecast_paper_summary"]), "returned": len(ts_forecast_paper_summary), **_payload_size_meta(ts_forecast_paper_summary)},
            "filters": {"section": requested_section or None, "limit": bounded_limit, "compact": compact},
            "section_contract": {
                "requested_section": requested_section or None,
                "included_sections": [key for key, include in included.items() if include],
                "operator_note": "Use section=<name> for normal watchlist UI reads. Omit section only for all-section debug reads.",
            },
        },
    }


LLM_DECISIONS_TABLE = "advisory_llm_decisions"


def build_llm_decisions_payload(
    *, limit: int = 50, offset: int = 0, symbol: str | None = None, asof_date: str | None = None,
) -> dict[str, Any]:
    """Daily review-only LLM decisions (table `advisory_llm_decisions`).

    Deliberately LEAN: bounded page, indexed `decided_at DESC`, and a projection that EXCLUDES the
    large evidence/contract/sizing JSON blobs (fetch those per-row via a detail call), so the list
    view loads fast. Every row is review-only (`broker_execution_allowed` is always False).
    """
    row_limit = _bounded_limit(limit, default=50, maximum=200)
    row_offset = max(int(offset or 0), 0)
    if not _table_exists(LLM_DECISIONS_TABLE):
        return {"decisions": [], "summary": {"total": 0, "by_action": {}, "grounded_for_live": 0},
                "page": {"total": 0, "returned": 0, "offset": row_offset, "limit": row_limit, "next_offset": None},
                "review_only": True, "skipped": [{"source": LLM_DECISIONS_TABLE, "error": "missing_table"}]}
    clauses: list[str] = []
    filters: dict[str, Any] = {}
    sym = _text(symbol)
    if sym:
        clauses.append("symbol = %(symbol)s")
        filters["symbol"] = sym.upper()
    day = _text(asof_date)
    if day:
        clauses.append("asof_date::date = %(asof_day)s")
        filters["asof_day"] = day
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    rows = _records(sql_to_df(
        f"""SELECT decided_at, asof_date, symbol, proposed_action, decision_mode, conviction,
                   meets_data_grounding_for_live, sufficiency_path, broker_execution_allowed,
                   llm_status, prompt_version, llm_model
            FROM {LLM_DECISIONS_TABLE} {where}
            ORDER BY decided_at DESC NULLS LAST LIMIT %(limit)s OFFSET %(offset)s""",
        params={**filters, "limit": row_limit, "offset": row_offset}, retries=3,
    ))
    summary_df = sql_to_df(
        f"""SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE meets_data_grounding_for_live) AS grounded
            FROM {LLM_DECISIONS_TABLE} {where}""",
        params=filters, retries=3,
    )
    by_action_df = sql_to_df(
        f"SELECT proposed_action, COUNT(*) AS n FROM {LLM_DECISIONS_TABLE} {where} GROUP BY 1",
        params=filters, retries=3,
    )
    total = int(summary_df.iloc[0]["total"]) if not summary_df.empty else 0
    grounded = int(summary_df.iloc[0]["grounded"] or 0) if not summary_df.empty else 0
    by_action = {str(r["proposed_action"]): int(r["n"]) for r in _records(by_action_df)}
    next_offset = row_offset + row_limit if (row_offset + row_limit) < total else None
    return {
        "decisions": [_json_ready(row) for row in rows],
        "summary": {"total": total, "by_action": by_action, "grounded_for_live": grounded},
        "page": {"total": total, "returned": len(rows), "offset": row_offset, "limit": row_limit, "next_offset": next_offset},
        "review_only": True,
        "broker_execution_allowed_count": 0,
    }


# Unified per-symbol state resolver (UI redesign WI-2). A symbol is in EXACTLY one state so it never
# appears in two lists. Precedence: an open tracked holding wins (HOLDING); otherwise a fresh actionable
# signal (deterministic action and/or LLM decision) is a RECOMMENDATION; a closed holding with no fresh
# signal is EXITED; everything else is NONE.
_ACTION_DIRECTION = {
    "BUY": 1, "BUY_WATCH": 1, "ACCUMULATE": 1, "ADD": 1, "ADD_EXPOSURE": 1,
    "SELL": -1, "PARTIAL_SELL": -1, "REDUCE_EXPOSURE_REVIEW": -1, "TIGHTEN_STOP": -1, "EXIT": -1,
}
# Codes that do NOT by themselves make a symbol a fresh recommendation needing operator attention.
_NON_ACTIONABLE_CODES = {"HOLD", "NO_ACTION", "NONE", ""}


def _action_direction(code: Any) -> int:
    """Map an action/decision verb to a long(+1)/de-risk(-1)/neutral(0) sign for agree-vs-conflict."""
    return _ACTION_DIRECTION.get(str(code or "").strip().upper(), 0)


def resolve_symbol_states(
    recommendations: list[dict[str, Any]] | None,
    llm_decisions: list[dict[str, Any]] | None,
    holdings: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Collapse the three per-symbol sources into one row per symbol with a single `state`. Pure.

    Inputs are already reduced to the latest row per symbol by the caller. Each output row carries the
    deterministic action, the LLM action/mode/conviction, an agree/conflict flag, and any entry fields.
    """
    rec_by_symbol: dict[str, dict[str, Any]] = {}
    for row in (recommendations or []):
        sym = str(row.get("symbol") or "").strip().upper()
        if sym:
            rec_by_symbol[sym] = row
    llm_by_symbol: dict[str, dict[str, Any]] = {}
    for row in (llm_decisions or []):
        sym = str(row.get("symbol") or "").strip().upper()
        if sym:
            llm_by_symbol[sym] = row
    open_holding: dict[str, dict[str, Any]] = {}
    exited_holding: dict[str, dict[str, Any]] = {}
    for row in (holdings or []):
        sym = str(row.get("symbol") or "").strip().upper()
        if not sym:
            continue
        if str(row.get("status") or "").strip().lower() == "open":
            open_holding[sym] = row
        else:
            exited_holding.setdefault(sym, row)

    symbols = set(rec_by_symbol) | set(llm_by_symbol) | set(open_holding) | set(exited_holding)
    out: list[dict[str, Any]] = []
    for sym in sorted(symbols):
        rec = rec_by_symbol.get(sym) or {}
        llm = llm_by_symbol.get(sym) or {}
        det_action = rec.get("action_code")
        llm_action = llm.get("proposed_action")
        det_dir = _action_direction(det_action)
        llm_dir = _action_direction(llm_action)
        agree = bool(det_dir and llm_dir and det_dir == llm_dir)
        conflict = bool(det_dir and llm_dir and det_dir != llm_dir)
        det_actionable = bool(det_action) and str(det_action).strip().upper() not in _NON_ACTIONABLE_CODES
        # The LLM-only path requires a DIRECTIONAL call (BUY/SELL family): a neutral LLM WATCH -- e.g.
        # the deterministic fallback when LLM authority is off -- must not promote an otherwise
        # non-actionable symbol into the queue. The deterministic action already covers WATCH.
        llm_actionable = _action_direction(llm_action) != 0
        has_signal = det_actionable or llm_actionable

        if sym in open_holding:
            state = "HOLDING"
        elif has_signal:
            state = "RECOMMENDATION"
        elif sym in exited_holding:
            state = "EXITED"
        else:
            state = "NONE"

        hold = open_holding.get(sym) or exited_holding.get(sym) or {}
        out.append({
            "symbol": sym,
            "state": state,
            "action": det_action,
            "action_priority": rec.get("action_priority"),
            "reference_price": rec.get("reference_price"),
            "reason_contract_status": rec.get("reason_contract_status"),
            "asof_date": rec.get("asof_date") or llm.get("asof_date"),
            "llm_action": llm_action,
            "llm_mode": llm.get("decision_mode"),
            "llm_conviction": llm.get("conviction"),
            "llm_grounded": llm.get("meets_data_grounding_for_live"),
            "agree": agree,
            "conflict": conflict,
            "entry_price": hold.get("entry_price"),
            "entry_date": hold.get("entry_date"),
            "holding_status": hold.get("status"),
            "source": hold.get("source"),
        })
    return out


def _load_latest_recommendations_by_symbol() -> list[dict[str, Any]]:
    """Latest action-recommendation row per symbol (most recent asof_date, then highest priority). Lean."""
    if not _table_exists(ACTION_RECOMMENDATIONS_TABLE):
        return []
    return _records(sql_to_df(
        f"""
        SELECT DISTINCT ON (symbol)
               symbol, action_code, action_priority, reference_price,
               reason_contract_status, asof_date
        FROM {ACTION_RECOMMENDATIONS_TABLE}
        ORDER BY symbol, asof_date DESC NULLS LAST, action_priority DESC NULLS LAST
        """,
        retries=3,
    ))


def _load_latest_llm_decisions_by_symbol() -> list[dict[str, Any]]:
    """Latest LLM decision row per symbol (most recent decided_at). Lean (no JSON blobs)."""
    if not _table_exists(LLM_DECISIONS_TABLE):
        return []
    return _records(sql_to_df(
        f"""
        SELECT DISTINCT ON (symbol)
               symbol, proposed_action, decision_mode, conviction,
               meets_data_grounding_for_live, asof_date, decided_at
        FROM {LLM_DECISIONS_TABLE}
        ORDER BY symbol, decided_at DESC NULLS LAST
        """,
        retries=3,
    ))


def load_symbol_states() -> list[dict[str, Any]]:
    """DB-backed wrapper around `resolve_symbol_states` (latest row per source). Review-only read."""
    from advisory.operator_holdings import load_holdings

    try:
        holdings = load_holdings()
    except Exception:
        holdings = []
    return resolve_symbol_states(
        _load_latest_recommendations_by_symbol(),
        _load_latest_llm_decisions_by_symbol(),
        holdings,
    )


def build_recommendations_unified_payload(
    *, limit: int = 50, offset: int = 0, action: str | None = None, only_conflicts: bool = False,
) -> dict[str, Any]:
    """Unified per-symbol RECOMMENDATION queue (WI-3): deterministic action + LLM decision in one row.

    Lean: built from `resolve_symbol_states` (no evidence/contract blobs); the heavy "why" is the
    on-demand `/api/symbol/{symbol}/why`. Review-only -- nothing here submits a broker order.
    """
    row_limit = _bounded_limit(limit, default=50, maximum=200)
    row_offset = max(int(offset or 0), 0)
    all_recs = [row for row in load_symbol_states() if row.get("state") == "RECOMMENDATION"]

    # Summary is computed over the FULL recommendation set (not the filtered view) so the by_action
    # chips stay a stable navigation aid -- clicking one filters the list without hiding the others.
    by_action: dict[str, int] = {}
    agree_count = 0
    conflict_count = 0
    for row in all_recs:
        key = str(row.get("action") or "UNKNOWN")
        by_action[key] = by_action.get(key, 0) + 1
        if row.get("agree"):
            agree_count += 1
        if row.get("conflict"):
            conflict_count += 1

    states = all_recs
    action_filter = _text(action)
    if action_filter:
        wanted = action_filter.strip().upper()
        states = [row for row in states if str(row.get("action") or "").upper() == wanted]
    if only_conflicts:
        states = [row for row in states if row.get("conflict")]
    # Most actionable first: directional (BUY/SELL) ahead of neutral (WATCH), BUY-side ahead of
    # de-risk, then by deterministic priority.
    states.sort(key=lambda r: (
        -abs(_action_direction(r.get("action"))),
        -_action_direction(r.get("action")),
        -float(r.get("action_priority") or 0),
        str(r.get("symbol") or ""),
    ))
    total = len(states)
    page = states[row_offset:row_offset + row_limit]
    next_offset = row_offset + row_limit if (row_offset + row_limit) < total else None
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema("/api/recommendations-unified", schema_name="recommendations_unified"),
        "recommendations": [_json_ready(row) for row in page],
        "summary": {"total": len(all_recs), "filtered": total, "by_action": by_action, "agree": agree_count, "conflict": conflict_count},
        "page": {"total": total, "returned": len(page), "offset": row_offset, "limit": row_limit, "next_offset": next_offset},
        "review_only": True,
        "broker_execution_allowed_count": 0,
    }


def build_positions_payload(*, status: str | None = "open", limit: int = 200) -> dict[str, Any]:
    """Tracked holdings (WI-4): lean list, default open, enriched with the latest price. Review-only."""
    from advisory.operator_holdings import load_holdings

    row_limit = _bounded_limit(limit, default=200, maximum=500)
    status_filter = _text(status)
    holdings = load_holdings(status=(status_filter or None), limit=row_limit)
    prices = _latest_ohlcv_prices([str(row.get("symbol") or "") for row in holdings])
    out: list[dict[str, Any]] = []
    open_count = 0
    exited_count = 0
    for row in holdings:
        sym = str(row.get("symbol") or "").upper()
        price_row = prices.get(sym) or {}
        current_price = price_row.get("price")
        entry_price = row.get("entry_price")
        change_pct = None
        if current_price is not None and entry_price not in (None, 0):
            try:
                change_pct = round((float(current_price) - float(entry_price)) / float(entry_price) * 100.0, 2)
            except (TypeError, ValueError, ZeroDivisionError):
                change_pct = None
        if str(row.get("status") or "").lower() == "open":
            open_count += 1
        else:
            exited_count += 1
        out.append(_json_ready({
            **row,
            "current_price": current_price,
            "price_asof": price_row.get("price_asof"),
            "change_pct_since_entry": change_pct,
        }))
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema("/api/positions", schema_name="operator_positions"),
        "positions": out,
        "summary": {"total": len(out), "open": open_count, "exited": exited_count},
        "review_only": True,
        "operator_note": "Holdings are operator-marked tracking for monitoring, not broker orders; no simulated P&L.",
    }


def build_position_take_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """POST /api/positions/take -- mark a recommendation as taken -> tracked holding (WI-4)."""
    from advisory.operator_holdings import record_take

    symbol = str(payload.get("symbol") or "").strip()
    if not symbol:
        raise ValueError("symbol is required")
    entry_date = payload.get("entry_date") or pd.Timestamp.utcnow().date().isoformat()
    row = record_take(
        symbol=symbol,
        entry_price=payload.get("entry_price"),
        entry_date=entry_date,
        action=payload.get("action"),
        source=payload.get("source"),
        note=payload.get("note"),
    )
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema("/api/positions/take", schema_name="operator_position_take"),
        "status": "ok",
        "holding": _json_ready(row),
        "review_only": True,
        "broker_execution_allowed": False,
    }


def build_position_exit_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """POST /api/positions/exit -- close a tracked holding (WI-4)."""
    from advisory.operator_holdings import record_exit

    symbol = str(payload.get("symbol") or "").strip()
    if not symbol:
        raise ValueError("symbol is required")
    entry_date = payload.get("entry_date")
    if not entry_date:
        raise ValueError("entry_date is required to identify the holding")
    updated = record_exit(
        symbol=symbol,
        entry_date=entry_date,
        exit_price=payload.get("exit_price"),
        exit_date=payload.get("exit_date") or pd.Timestamp.utcnow().date().isoformat(),
        note=payload.get("note"),
    )
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema("/api/positions/exit", schema_name="operator_position_exit"),
        "status": "ok" if updated else "no_open_holding",
        "updated": int(updated),
        "review_only": True,
    }


def _latest_recommendation_row(symbol: str) -> dict[str, Any]:
    """Full latest deterministic recommendation row for one symbol (for the why detail)."""
    if not _table_exists(ACTION_RECOMMENDATIONS_TABLE):
        return {}
    rows = _records(sql_to_df(
        f"""
        SELECT symbol, action_code, action_reason, action_detail, reason_contract_status,
               recommendation_reason_json, reference_price, recommended_stop_price,
               recommended_target_price, expected_horizon_days, feature_freshness_json, asof_date
        FROM {ACTION_RECOMMENDATIONS_TABLE}
        WHERE symbol = %(symbol)s
        ORDER BY asof_date DESC NULLS LAST, action_priority DESC NULLS LAST
        LIMIT 1
        """,
        params={"symbol": symbol.strip().upper()}, retries=2,
    ))
    return rows[0] if rows else {}


def _latest_llm_decision_row(symbol: str) -> dict[str, Any]:
    """Full latest LLM decision row for one symbol (for the why detail)."""
    if not _table_exists(LLM_DECISIONS_TABLE):
        return {}
    rows = _records(sql_to_df(
        f"""
        SELECT symbol, asof_date, decided_at, proposed_action, decision_mode, conviction,
               meets_data_grounding_for_live, sufficiency_path, rationale, decision_contract_json,
               llm_status, llm_model, prompt_version
        FROM {LLM_DECISIONS_TABLE}
        WHERE symbol = %(symbol)s
        ORDER BY decided_at DESC NULLS LAST
        LIMIT 1
        """,
        params={"symbol": symbol.strip().upper()}, retries=2,
    ))
    return rows[0] if rows else {}


def build_symbol_why_payload(symbol: str, *, asof_date: str | None = None) -> dict[str, Any]:
    """The single heavy "why" detail for one symbol (WI-5): evidence verdicts + grounding + reasons.

    This is the ONLY endpoint that loads the full evidence tree, fetched on demand when an operator
    drills into a row. List endpoints stay lean. Review-only.
    """
    from advisory.llm_decision_contract import validate_decision_grounding
    from advisory.llm_evidence_packet import load_evidence_packet

    sym = str(symbol or "").strip().upper()
    if not sym:
        raise ValueError("symbol is required")
    rec_row = _latest_recommendation_row(sym)
    llm_row = _latest_llm_decision_row(sym)
    asof = _text(asof_date) or str(rec_row.get("asof_date") or llm_row.get("asof_date") or "")[:10]

    packet: dict[str, Any] = {}
    packet_error: str | None = None
    grounding: dict[str, Any] = {}
    if asof:
        try:
            packet = load_evidence_packet(sym, asof)
        except Exception as exc:  # surface, never silently swallow
            packet_error = f"{type(exc).__name__}: {exc}"
        if packet:
            action = str(llm_row.get("proposed_action") or rec_row.get("action_code") or "").strip().upper()
            try:
                grounding = validate_decision_grounding(
                    action=action, cited_dimensions=None, packet=packet, matched_hypothesis=None,
                )
            except Exception as exc:
                grounding = {"error": f"{type(exc).__name__}: {exc}"}

    reason_contract = None
    raw_reason = rec_row.get("recommendation_reason_json")
    if raw_reason:
        try:
            reason_contract = json.loads(raw_reason) if isinstance(raw_reason, str) else raw_reason
        except (ValueError, TypeError):
            reason_contract = None
    decision_contract = None
    raw_contract = llm_row.get("decision_contract_json")
    if raw_contract:
        try:
            decision_contract = json.loads(raw_contract) if isinstance(raw_contract, str) else raw_contract
        except (ValueError, TypeError):
            decision_contract = None

    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema("/api/symbol/{symbol}/why", schema_name="operator_symbol_why"),
        "symbol": sym,
        "asof_date": asof or None,
        "evidence_packet": _json_ready(packet) if packet else None,
        "evidence_packet_error": packet_error,
        "grounding": _json_ready(grounding) if grounding else None,
        "deterministic": {
            "action_code": rec_row.get("action_code"),
            "action_reason": rec_row.get("action_reason"),
            "action_detail": rec_row.get("action_detail"),
            "reason_contract_status": rec_row.get("reason_contract_status"),
            "recommendation_reason": reason_contract,
            "reference_price": rec_row.get("reference_price"),
            "recommended_stop_price": rec_row.get("recommended_stop_price"),
            "recommended_target_price": rec_row.get("recommended_target_price"),
            "expected_horizon_days": rec_row.get("expected_horizon_days"),
        },
        "llm_decision": _json_ready({**llm_row, "decision_contract": decision_contract}) if llm_row else None,
        "review_only": True,
    }


def _load_decision_monitor_records(*, limit: int = 500) -> list[dict[str, Any]]:
    """Minimal rows for the .3 systematic-error monitor (WI-7). Bounded; no JSON blobs."""
    if not _table_exists(LLM_DECISIONS_TABLE):
        return []
    return _records(sql_to_df(
        f"""
        SELECT symbol, decided_at, proposed_action, event_class, sufficiency_path
        FROM {LLM_DECISIONS_TABLE}
        ORDER BY decided_at DESC NULLS LAST
        LIMIT %(limit)s
        """,
        params={"limit": max(1, int(limit))}, retries=2,
    ))


def build_health_hub_payload(*, asof_date: str | None = None, error_limit: int = 25) -> dict[str, Any]:
    """One consolidated diagnostics hub (WI-7): data freshness + cron + API errors + LLM monitor.

    Each source is independently bounded (no unbounded scans); a degraded source is surfaced under
    `skipped`, never silently dropped. Read-only, so the result is briefly cached (WI-15) to keep the
    multi-source fan-out off the hot path.
    """
    cache_key = ("health_hub_payload", str(asof_date or ""), int(error_limit or 0))
    now = time.monotonic()
    cached = _PAYLOAD_CACHE.get(cache_key)
    if cached and OPERATOR_API_PAYLOAD_CACHE_SECONDS > 0 and (now - cached[0]) <= OPERATOR_API_PAYLOAD_CACHE_SECONDS:
        return cached[1]
    skipped: list[dict[str, Any]] = []
    try:
        data_health = build_data_health_payload(asof_date=asof_date)
    except Exception as exc:
        data_health = {}
        skipped.append({"source": "data_health", "error": f"{type(exc).__name__}: {exc}"})
    try:
        api_errors = build_operator_api_errors_payload(limit=_bounded_limit(error_limit, default=25, maximum=100))
    except Exception as exc:
        api_errors = {"errors": [], "summary": {"total": 0}}
        skipped.append({"source": "api_errors", "error": f"{type(exc).__name__}: {exc}"})

    monitor: dict[str, Any] = {}
    try:
        from advisory.llm_decision_store import build_decision_monitor_report

        monitor = build_decision_monitor_report(_load_decision_monitor_records())
    except Exception as exc:
        skipped.append({"source": "llm_decision_monitor", "error": f"{type(exc).__name__}: {exc}"})

    sync_state = data_health.get("sync_state") or []
    blocked = [
        row for row in sync_state
        if isinstance(row, dict) and str(row.get("status") or row.get("freshness") or "").lower() in {"stale", "error", "degraded", "blocked"}
    ]
    payload = {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema("/api/health-hub", schema_name="operator_health_hub"),
        "data_health": {
            "summary": data_health.get("summary") or {},
            "snapshot": data_health.get("snapshot"),
            "snapshot_warning": data_health.get("snapshot_warning"),
            "sync_state": sync_state,
            "cron_status": data_health.get("cron_status") or [],
            "runtime_processes": data_health.get("runtime_processes") or [],
        },
        "blocked_on_data": {
            "count": len(blocked),
            "sources": blocked,
            "note": "Sources whose sync-state is stale/degraded; symbols depending on them may be blocked.",
        },
        "api_errors": {"errors": api_errors.get("errors") or [], "summary": api_errors.get("summary") or {}},
        "llm_monitor": monitor,
        "skipped": skipped,
        "read_only": True,
    }
    _PAYLOAD_CACHE[cache_key] = (now, payload)
    return payload


def build_workbench_payload(*, top_n: int = 8) -> dict[str, Any]:
    """The home screen (WI-6): one bounded call -> what needs me now.

    Three sections from already-built pieces: the top unified recommendations, open holdings (with a
    rough since-entry move), and the top health alerts. No full-snapshot fallback. Review-only.
    """
    n = _bounded_limit(top_n, default=8, maximum=25)
    recommendations = build_recommendations_unified_payload(limit=n, offset=0)
    positions = build_positions_payload(status="open", limit=n)
    try:
        hub = build_health_hub_payload(error_limit=5)
        health_alerts = {
            "blocked_on_data": hub.get("blocked_on_data", {}).get("count", 0),
            "api_errors": (hub.get("api_errors", {}).get("summary", {}) or {}).get("total", 0),
            "llm_monitor_alerts": hub.get("llm_monitor", {}).get("alerts") or hub.get("llm_monitor", {}).get("findings") or [],
        }
    except Exception as exc:
        health_alerts = {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema("/api/workbench", schema_name="operator_workbench"),
        "recommendations": {
            "items": recommendations.get("recommendations") or [],
            "summary": recommendations.get("summary") or {},
        },
        "holdings": {
            "items": positions.get("positions") or [],
            "summary": positions.get("summary") or {},
        },
        "health": health_alerts,
        "review_only": True,
    }


def build_market_context_payload(*, asof_date: str | None = None, limit: int = 50) -> dict[str, Any]:
    parsed_asof = _parse_asof_date(asof_date)
    payload = load_latest_market_context(parsed_asof, limit=max(0, int(limit)))
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema("/api/market-context", schema_name="operator_market_context"),
        "asof_date": asof_date,
        "summary": payload.get("summary") or {},
        "top_universe": payload.get("top_universe") or [],
    }


def build_regime_overlays_payload(*, limit: int = 25, status: str | None = None) -> dict[str, Any]:
    bounded = _bounded_limit(limit, default=25, maximum=100)
    proposals = load_regime_overlay_reviews(limit=bounded, status=status)
    decisions = load_regime_overlay_decisions(limit=bounded)
    counts: dict[str, int] = {}
    for row in proposals:
        key = str(row.get("production_status") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema("/api/regime-overlays", schema_name="regime_overlays"),
        "status": "ok",
        "proposals": proposals,
        "decisions": decisions,
        "summary": {
            "proposal_count": len(proposals),
            "decision_count": len(decisions),
            "by_status": counts,
            "review_only": True,
            "production_policy_changed": False,
        },
        "pagination": {
            "proposals": _bounded_list_contract(proposals, limit=bounded, default=25, maximum=100),
            "decisions": _bounded_list_contract(decisions, limit=bounded, default=25, maximum=100),
        },
        "operator_boundary": {
            "authority_scope": "review_input_only",
            "changes_action_policy": False,
            "changes_portfolio": False,
            "submits_broker_order": False,
            "operator_note": "Accept/reject decisions are audit/review state only. A separate reviewed config/rule implementation is required before action policy can consume a regime overlay.",
        },
    }


def build_regime_overlay_decision_payload(payload: dict[str, Any]) -> dict[str, Any]:
    asof_date = payload.get("asof_date")
    proposal_id = str(payload.get("proposal_id") or "").strip()
    decision = str(payload.get("decision") or "").strip().lower()
    if not asof_date:
        raise ValueError("asof_date is required")
    if not proposal_id:
        raise ValueError("proposal_id is required")
    result = record_regime_overlay_decision(
        asof_date=asof_date,
        proposal_id=proposal_id,
        decision=decision,
        decision_reason=str(payload.get("decision_reason") or "") or None,
        operator_id=str(payload.get("operator_id") or "") or None,
    )
    result["generated_at"] = pd.Timestamp.utcnow().isoformat()
    result["api_schema"] = _operator_api_schema("/api/regime-overlays/decision", schema_name="regime_overlay_decision")
    return result


def _table_exists(table_name: str) -> bool:
    df = sql_to_df(
        """
        SELECT 1 AS exists_flag
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = %s
        LIMIT 1
        """,
        params=(table_name,),
        retries=2,
    )
    return not df.empty


def _table_columns(table_name: str) -> set[str]:
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
        _record_operator_local_fallback(
            source=table_name,
            fallback_type="operator_api_table_columns_lookup_failed",
            reason="Operator API could not inspect table columns and will continue with an empty column set.",
            error=exc,
            metadata={"table_name": table_name},
        )
        return set()
    if df.empty:
        return set()
    return {str(value) for value in df["column_name"].dropna().tolist()}


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df.empty:
        return []
    out = df.copy()
    out = out.astype(object).where(pd.notna(out), None)
    return out.to_dict(orient="records")


def _bounded_limit(value: int | None, *, default: int = 50, maximum: int = 500) -> int:
    try:
        parsed = int(value if value is not None else default)
    except Exception as exc:
        _record_operator_local_fallback(
            source="operator_api_pagination",
            fallback_type="operator_api_bounded_limit_parse_failed",
            reason="Operator API could not parse a limit value and used the configured default.",
            error=exc,
            metadata={"value": str(value), "default": int(default), "maximum": int(maximum)},
        )
        parsed = default
    return max(0, min(parsed, maximum))


def _bounded_offset(value: int | None) -> int:
    try:
        parsed = int(value if value is not None else 0)
    except Exception as exc:
        _record_operator_local_fallback(
            source="operator_api_pagination",
            fallback_type="operator_api_bounded_offset_parse_failed",
            reason="Operator API could not parse an offset value and used zero.",
            error=exc,
            metadata={"value": str(value), "default": 0},
        )
        parsed = 0
    return max(0, parsed)


def _row_text(row: dict[str, Any], keys: list[str]) -> str:
    return " ".join(str(row.get(key) or "") for key in keys).lower()


def _action_label(row: dict[str, Any]) -> str:
    label = (
        row.get("action_code")
        or row.get("action")
        or row.get("next_action")
        or row.get("reason")
        or row.get("status")
        or row.get("action_status")
        or row.get("alert_type")
        or row.get("source_type")
    )
    if not label and any(row.get(key) not in (None, "", [], {}) for key in ["last_price", "attractive_price_low", "invalidation_price"]):
        label = "ALERT"
    return str(label or "").strip().upper()


BROKER_CANDIDATE_ACTIONS = {"BUY", "BUY_MORE", "ADD_ON_PULLBACK", "SELL", "PARTIAL_SELL", "EXIT", "FULL_EXIT", "EMERGENCY_EXIT"}


def _action_transition_contract(row: dict[str, Any]) -> dict[str, Any]:
    reason = _jsonish(row.get("recommendation_reason"))
    if not isinstance(reason, dict):
        return {}
    evidence = reason.get("evidence") if isinstance(reason.get("evidence"), dict) else {}
    transition = evidence.get("action_transition") if isinstance(evidence.get("action_transition"), dict) else {}
    return transition if isinstance(transition, dict) else {}


def _action_queue_contract(row: dict[str, Any]) -> dict[str, Any]:
    final_action = _action_label(row)
    final_action_upper = final_action.upper()
    transition = _action_transition_contract(row)
    transition_effect = str(transition.get("state_effect") or "").strip().lower()
    transition_broker_candidate = transition.get("broker_order_candidate")
    transition_broker_allowed = transition.get("broker_execution_allowed")
    transition_precondition_status = str(transition.get("precondition_status") or "").strip().lower()
    row_text = _row_text(
        row,
        [
            "status",
            "action_status",
            "portfolio_status",
            "reason_contract_status",
            "event_status",
            "review_action",
            "severity",
            "execution_mode",
            "exit_strategy",
            "reason_detail",
        ],
    )
    is_manual = (
        "MANUAL" in final_action_upper
        or "REVIEW" in final_action_upper
        or transition_effect == "review_only"
        or "manual" in row_text
        or "review" in row_text
    )
    is_blocked = "blocked" in row_text or "risk-off" in row_text or "risk_off" in row_text
    explicit_broker_candidate = (
        bool(transition_broker_candidate)
        if transition_broker_candidate is not None
        else final_action_upper in BROKER_CANDIDATE_ACTIONS
    )
    broker_candidate = final_action_upper in BROKER_CANDIDATE_ACTIONS and explicit_broker_candidate and not is_manual
    approved_filter_match = bool(broker_candidate)
    if transition_precondition_status and transition_precondition_status not in {"complete", "passed", "ready", "approved"}:
        approved_filter_match = False
        is_blocked = True
    if is_manual:
        display_status = "review_only"
    elif approved_filter_match:
        display_status = "broker_candidate"
    elif is_blocked:
        display_status = "blocked"
    elif final_action_upper in {"WATCH", "HOLD", "NO_ACTION"}:
        display_status = "watch_hold"
    else:
        display_status = "other"
    return {
        "schema_version": 1,
        "final_action": final_action_upper or "NO_ACTION",
        "display_status": display_status,
        "approved_filter_match": approved_filter_match,
        "manual_filter_match": bool(is_manual),
        "blocked_filter_match": bool(is_blocked),
        "broker_order_candidate": bool(broker_candidate),
        "broker_execution_allowed_at_action_layer": bool(transition_broker_allowed),
        "precondition_status": transition_precondition_status or None,
        "portfolio_ready": False,
        "broker_ready": False,
        "operator_note": (
            "Approved filter means broker-candidate final action only. Review-only/manual rows remain active operator work "
            "even if an upstream portfolio/source row was approved."
        ),
    }


def _trust_check(key: str, label: str, status: str, detail: str, *, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "key": key,
        "label": label,
        "status": status,
        "detail": detail,
    }
    if evidence:
        out["evidence"] = _json_ready(evidence)
    return out


def _action_final_state_trust(row: dict[str, Any]) -> dict[str, Any]:
    queue = row.get("action_queue_contract") if isinstance(row.get("action_queue_contract"), dict) else _action_queue_contract(row)
    reason = _jsonish(row.get("recommendation_reason"))
    reason_status = str(row.get("reason_contract_status") or (reason.get("status") if isinstance(reason, dict) else "") or "").strip()
    reason_status_lc = reason_status.lower()
    transition = _action_transition_contract(row)
    safety = _execution_safety_contract_from_row(row) or {}
    freshness = _feature_freshness_summary_from_contract(row.get("feature_freshness")) or _jsonish(row.get("feature_freshness_summary"))
    if not isinstance(freshness, dict):
        freshness = {}
    final_action = str(queue.get("final_action") or _action_label(row) or "NO_ACTION").upper()
    display_status = str(queue.get("display_status") or "").lower()
    broker_candidate = bool(queue.get("broker_order_candidate"))
    checks: list[dict[str, Any]] = []

    if reason_status:
        if any(token in reason_status_lc for token in ("incomplete", "blocked", "downgrade", "invalid")):
            status = "blocked"
        elif "review" in reason_status_lc:
            status = "warning"
        elif "complete" in reason_status_lc:
            status = "passed"
        else:
            status = "warning"
        checks.append(_trust_check(
            "reason_contract",
            "Reason contract",
            status,
            f"Reason contract status is {reason_status}. Complete explanations are required before an action can be trusted.",
            evidence={"status": reason_status, "missing_fields": reason.get("missing_fields") if isinstance(reason, dict) else None},
        ))
    else:
        checks.append(_trust_check(
            "reason_contract",
            "Reason contract",
            "blocked",
            "No reason contract status was found, so the operator cannot verify why this action exists.",
        ))

    queue_detail = str(queue.get("operator_note") or "Final action has been classified for operator display.")
    if display_status == "broker_candidate":
        queue_status = "passed"
    elif display_status == "blocked":
        queue_status = "blocked"
    elif display_status in {"review_only", "watch_hold"}:
        queue_status = "warning"
    else:
        queue_status = "warning"
    checks.append(_trust_check(
        "action_queue",
        "Queue classification",
        queue_status,
        f"Final action is {final_action}; queue status is {display_status or 'unknown'}. {queue_detail}",
        evidence={
            "display_status": display_status or None,
            "approved_filter_match": queue.get("approved_filter_match"),
            "manual_filter_match": queue.get("manual_filter_match"),
            "broker_order_candidate": queue.get("broker_order_candidate"),
        },
    ))

    missing_preconditions = transition.get("missing_preconditions") if isinstance(transition.get("missing_preconditions"), list) else []
    precondition_status = str(transition.get("precondition_status") or "").strip().lower()
    if not transition:
        transition_status = "warning"
        transition_detail = "No action-transition contract was found. Treat this row as operator information until the transition is regenerated."
    elif missing_preconditions or (precondition_status and precondition_status not in {"complete", "passed", "ready", "approved"}):
        transition_status = "blocked"
        transition_detail = f"Transition preconditions are {precondition_status or 'incomplete'}; missing: {', '.join(map(str, missing_preconditions)) or 'not listed'}."
    elif broker_candidate:
        transition_status = "passed"
        transition_detail = "Action transition is broker-candidate only after downstream preview, approval, and reconciliation gates."
    else:
        transition_status = "warning" if display_status in {"review_only", "watch_hold"} else "passed"
        transition_detail = str(transition.get("broker_boundary") or "Transition is review/monitoring context and does not create a broker order.")
    checks.append(_trust_check(
        "action_transition",
        "Action transition",
        transition_status,
        transition_detail,
        evidence={
            "state_effect": transition.get("state_effect"),
            "next_required_stage": transition.get("next_required_stage"),
            "precondition_status": precondition_status or None,
            "missing_preconditions": missing_preconditions,
            "broker_execution_allowed": transition.get("broker_execution_allowed"),
        },
    ))

    freshness_status = str(freshness.get("status") or "").strip().lower()
    if freshness_status in {"ok", "fresh", "ready"}:
        freshness_check_status = "passed"
        freshness_detail = "Required price/technical inputs were fresh when the action was evaluated."
    elif freshness_status == "blocked":
        freshness_check_status = "blocked"
        freshness_detail = "Required price/technical inputs were blocked, stale, or missing when this action was evaluated."
    elif freshness:
        freshness_check_status = "warning"
        freshness_detail = f"Feature freshness status is {freshness_status or 'unknown'}."
    else:
        freshness_check_status = "warning"
        freshness_detail = "No feature freshness snapshot is attached to this action response. Use symbol freshness before execution."
    checks.append(_trust_check(
        "feature_freshness",
        "Data freshness",
        freshness_check_status,
        freshness_detail,
        evidence={
            "status": freshness_status or None,
            "counts": freshness.get("counts"),
            "blockers": freshness.get("blockers"),
            "source": freshness.get("source"),
        },
    ))

    if broker_candidate:
        approval_status = str(safety.get("operator_approval_status") or "missing").lower()
        reconciliation_status = str(safety.get("broker_reconciliation_status") or "not_run").lower()
        identity_status = str(safety.get("broker_identity_status") or "not_checked").lower()
        live_allowed = bool(safety.get("live_submission_allowed"))
        if not safety:
            execution_status = "blocked"
            execution_detail = "Broker-candidate action has no execution safety contract attached."
        elif live_allowed:
            execution_status = "passed"
            execution_detail = "Execution safety contract permits live submission, subject to the dedicated execution approval workflow."
        else:
            execution_status = "blocked"
            execution_detail = "Broker-candidate action is still blocked for live submission until approval, identity, reconciliation, and live-evidence gates pass."
        checks.append(_trust_check(
            "execution_boundary",
            "Execution boundary",
            execution_status,
            execution_detail,
            evidence={
                "operator_approval_status": approval_status,
                "broker_reconciliation_status": reconciliation_status,
                "broker_identity_status": identity_status,
                "live_submission_allowed": live_allowed,
                "issues": safety.get("issues"),
            },
        ))
    else:
        checks.append(_trust_check(
            "execution_boundary",
            "Execution boundary",
            "passed",
            "This final action is not broker-candidate, so it remains read-only operator work unless a later pipeline run changes the final action.",
            evidence={"broker_order_candidate": False},
        ))

    blocked = [check["detail"] for check in checks if check.get("status") == "blocked"]
    warnings = [check["detail"] for check in checks if check.get("status") == "warning"]
    if blocked:
        trust_status = "blocked"
    elif broker_candidate:
        trust_status = "execution_preview_required"
    elif display_status == "review_only":
        trust_status = "manual_review"
    elif display_status == "watch_hold":
        trust_status = "monitoring"
    else:
        trust_status = "informational"
    return {
        "schema_version": 1,
        "status": trust_status,
        "final_action": final_action,
        "display_status": display_status or None,
        "broker_order_candidate": broker_candidate,
        "approved_filter_match": bool(queue.get("approved_filter_match")),
        "live_submission_allowed": bool(safety.get("live_submission_allowed")) if safety else False,
        "checks": checks,
        "blockers": blocked[:6],
        "warnings": warnings[:6],
        "operator_note": (
            "Final State Trust explains why the row is visible and what must happen before execution. "
            "It is read-only and does not submit broker orders."
        ),
    }


def _attach_action_final_state_trust(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        if isinstance(row, dict):
            row["final_state_trust"] = _action_final_state_trust(row)


def _with_action_queue_contracts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        item["action_queue_contract"] = _action_queue_contract(item)
        out.append(item)
    return out


def _row_matches_status(row: dict[str, Any], normalized_status: str) -> bool:
    if not normalized_status or normalized_status == "all":
        return True
    queue_contract = _action_queue_contract(row)
    label = _action_label(row).lower()
    row_text = _row_text(
        row,
        [
            "status",
            "action_status",
            "portfolio_status",
            "reason_contract_status",
            "event_status",
            "review_action",
            "severity",
            "execution_mode",
            "exit_strategy",
            "reason_detail",
        ],
    )
    if normalized_status == "approved":
        return bool(queue_contract.get("approved_filter_match"))
    if normalized_status == "manual":
        return bool(queue_contract.get("manual_filter_match"))
    if normalized_status == "blocked":
        return bool(queue_contract.get("blocked_filter_match"))
    return normalized_status in row_text or normalized_status in label.lower()


def _row_matches_action(row: dict[str, Any], normalized_action: str) -> bool:
    if not normalized_action or normalized_action == "ALL":
        return True
    label = _action_label(row)
    row_text = " ".join(
        [
            label,
            _row_text(row, ["action_code", "action", "next_action", "action_type", "portfolio_action", "execution_intent", "reason", "status"]),
        ]
    ).upper()
    if normalized_action == "BUY":
        return any(token in row_text for token in ["BUY", "BUY_MORE", "ADD_ON_PULLBACK"])
    if normalized_action == "EXIT":
        return any(token in row_text for token in ["EXIT", "SELL", "PARTIAL_SELL", "TRIM_WINNER", "REDUCE"])
    if normalized_action == "MANUAL":
        return "MANUAL" in row_text or "REVIEW" in row_text
    return normalized_action in row_text


def _filter_rows(
    rows: list[dict[str, Any]],
    *,
    symbol: str | None = None,
    status: str | None = None,
    action: str | None = None,
    search: str | None = None,
) -> list[dict[str, Any]]:
    out = [row for row in rows if isinstance(row, dict)]
    normalized_symbol = str(symbol or "").strip().upper()
    if normalized_symbol:
        out = [row for row in out if str(row.get("symbol") or row.get("ticker") or "").strip().upper() == normalized_symbol]
    normalized_status = str(status or "").strip().lower()
    if normalized_status and normalized_status != "all":
        out = [row for row in out if _row_matches_status(row, normalized_status)]
    normalized_action = str(action or "").strip().upper()
    if normalized_action and normalized_action != "ALL":
        out = [row for row in out if _row_matches_action(row, normalized_action)]
    normalized_search = str(search or "").strip().lower()
    if normalized_search:
        search_keys = [
            "symbol",
            "ticker",
            "unique_id",
            "setup_id",
            "action_code",
            "action_type",
            "policy_class",
            "event_class",
            "subject",
            "title",
            "reason",
            "action_reason",
            "reason_detail",
            "summary",
            "concise_summary_text",
        ]
        out = [row for row in out if normalized_search in _row_text(row, search_keys)]
    return out


def _page_rows(rows: list[dict[str, Any]], *, limit: int, offset: int = 0) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    total = len(rows)
    start = _bounded_offset(offset)
    end = start + _bounded_limit(limit, default=50)
    page = rows[start:end]
    return page, {
        "total": total,
        "limit": _bounded_limit(limit, default=50),
        "offset": start,
        "returned": len(page),
        "has_more": end < total,
        "next_offset": end if end < total else None,
    }


def _pagination_contract(meta: dict[str, Any]) -> dict[str, Any]:
    total = int(meta.get("total") or 0)
    returned = int(meta.get("returned") or 0)
    limit = int(meta.get("limit") or returned or 0)
    offset = int(meta.get("offset") or 0)
    next_offset = meta.get("next_offset")
    return {
        "total_count": total,
        "returned_count": returned,
        "limit": limit,
        "offset": offset,
        "has_more": bool(meta.get("has_more")),
        "next_offset": next_offset,
    }


def _limited_pagination_contract(rows: list[dict[str, Any]], *, limit: int, default: int = 25) -> dict[str, Any]:
    row_limit = _bounded_limit(limit, default=default)
    total = len(rows)
    returned = min(total, row_limit)
    return {
        "total_count": total,
        "returned_count": returned,
        "limit": row_limit,
        "offset": 0,
        "has_more": returned < total,
        "next_offset": returned if returned < total else None,
    }


def _page_any_rows(rows: list[Any], *, limit: int, offset: int = 0, default: int = 50, maximum: int = 500) -> tuple[list[Any], dict[str, Any]]:
    row_limit = _bounded_limit(limit, default=default, maximum=maximum)
    row_offset = _bounded_offset(offset)
    total = len(rows)
    page = rows[row_offset: row_offset + row_limit]
    end = row_offset + row_limit
    return page, {
        "total_count": total,
        "returned_count": len(page),
        "limit": row_limit,
        "offset": row_offset,
        "has_more": end < total,
        "next_offset": end if end < total else None,
    }


def _bounded_list_contract(rows: list[Any], *, limit: int, offset: int = 0, default: int = 25, maximum: int = 500, total_count: int | None = None) -> dict[str, Any]:
    row_limit = _bounded_limit(limit, default=default, maximum=maximum)
    row_offset = _bounded_offset(offset)
    returned = len(rows)
    total = int(total_count) if total_count is not None else returned
    next_offset = row_offset + returned if row_offset + returned < total else None
    return {
        "total_count": total,
        "returned_count": returned,
        "limit": row_limit,
        "offset": row_offset,
        "has_more": next_offset is not None,
        "next_offset": next_offset,
    }


def _latest_ohlcv_prices(symbols: list[str]) -> dict[str, dict[str, Any]]:
    normalized = sorted({str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()})
    if not normalized:
        return {}
    cache_key = ("latest_ohlcv_prices", json.dumps(normalized, sort_keys=True))
    now = time.monotonic()
    cached = _PAYLOAD_CACHE.get(cache_key)
    if cached and OPERATOR_API_PAYLOAD_CACHE_SECONDS > 0 and (now - cached[0]) <= OPERATOR_API_PAYLOAD_CACHE_SECONDS:
        return cached[1]
    prices: dict[str, dict[str, Any]] = load_current_prices(normalized)
    missing = [symbol for symbol in normalized if symbol not in prices]
    if not missing:
        _PAYLOAD_CACHE[cache_key] = (now, prices)
        return prices
    if _table_exists("dhan_ohlcv_daily"):
        try:
            daily = sql_to_df(
                """
                SELECT DISTINCT ON (UPPER(ticker))
                    UPPER(ticker) AS symbol,
                    close AS price,
                    date AS price_asof
                FROM dhan_ohlcv_daily
                WHERE UPPER(ticker) = ANY(%(symbols)s)
                  AND close IS NOT NULL
                ORDER BY UPPER(ticker), date DESC, load_ts DESC
                """,
                params={"symbols": missing},
                retries=2,
            )
            for row in daily.to_dict(orient="records"):
                symbol = str(row.get("symbol") or "").upper()
                if symbol:
                    prices[symbol] = {"price": row.get("price"), "price_asof": row.get("price_asof"), "price_source": "dhan_ohlcv_daily"}
        except Exception as exc:
            _record_operator_local_fallback(
                source="dhan_ohlcv_daily",
                fallback_type="operator_api_latest_daily_ohlcv_prices_load_failed",
                severity="warn",
                reason="Operator API could not load latest daily OHLCV prices and returned rows with partial or missing latest-price enrichment.",
                error=exc,
                metadata={"symbol_count": len(missing), "symbols": missing[:50]},
            )
            print(f"[advisory.api] latest daily price enrichment failed error={type(exc).__name__}: {exc}", flush=True)
    use_intraday = env.bool("OPERATOR_API_INTRADAY_PRICE_FALLBACK", False)
    if use_intraday and _table_exists("dhan_ohlcv_intraday"):
        try:
            intraday = sql_to_df(
                """
                SELECT DISTINCT ON (UPPER(ticker))
                    UPPER(ticker) AS symbol,
                    close AS price,
                    timestamp AS price_asof
                FROM dhan_ohlcv_intraday
                WHERE UPPER(ticker) = ANY(%(symbols)s)
                  AND close IS NOT NULL
                ORDER BY UPPER(ticker), timestamp DESC, load_ts DESC
                """,
                params={"symbols": normalized},
                retries=2,
            )
            for row in intraday.to_dict(orient="records"):
                symbol = str(row.get("symbol") or "").upper()
                if symbol:
                    prices[symbol] = {"price": row.get("price"), "price_asof": row.get("price_asof"), "price_source": "dhan_ohlcv_intraday"}
        except Exception as exc:
            _record_operator_local_fallback(
                source="dhan_ohlcv_intraday",
                fallback_type="operator_api_latest_intraday_ohlcv_prices_load_failed",
                severity="warn",
                reason="Operator API could not load latest intraday OHLCV prices and returned rows with partial or missing latest-price enrichment.",
                error=exc,
                metadata={"symbol_count": len(normalized), "symbols": normalized[:50]},
            )
            print(f"[advisory.api] latest intraday price enrichment failed error={type(exc).__name__}: {exc}", flush=True)
    _PAYLOAD_CACHE[cache_key] = (now, prices)
    return prices


def _with_latest_prices(rows: list[dict[str, Any]], *, price_field: str = "current_price", prices: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    if not rows:
        return rows
    prices = prices if prices is not None else _latest_ohlcv_prices([str(row.get("symbol") or row.get("ticker") or "") for row in rows if isinstance(row, dict)])
    if not prices:
        return rows
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        symbol = str(item.get("symbol") or item.get("ticker") or "").strip().upper()
        price = prices.get(symbol)
        if price and item.get(price_field) in (None, "") and item.get("current_price") in (None, "") and item.get("last_price") in (None, ""):
            item[price_field] = price.get("price")
            item.setdefault("price_source", price.get("price_source"))
            item.setdefault("price_asof", price.get("price_asof"))
        out.append(item)
    return out


COMPACT_LIST_FIELDS = {
    "symbol",
    "ticker",
    "unique_id",
    "setup_id",
    "setup_name",
    "source_type",
    "event_type",
    "subject",
    "title",
    "kind",
    "status",
    "severity",
    "action",
    "action_code",
    "action_type",
    "action_status",
    "action_summary",
    "action_reason",
    "next_action",
    "reason",
    "reason_detail",
    "recommendation_reason",
    "reason_contract_status",
    "feature_freshness",
    "feature_freshness_summary",
    "feature_gate_effects",
    "manual_revision_summary",
    "manual_revision_pointers",
    "manual_revision_status",
    "company_memory_review",
    "portfolio_status",
    "entry_date",
    "entry_price",
    "current_price",
    "last_price",
    "reference_price",
    "price_source",
    "price_asof",
    "pnl_pct",
    "invest_score_pct",
    "allocation_inr",
    "execution_intent",
    "action_queue_contract",
    "execution_safety_contract",
    "technical_context",
    "technical_state",
    "technical_trigger_type",
    "technical_trigger_note",
    "technical_score",
    "setup_score",
    "technical_trend_score",
    "technical_structure_score",
    "technical_participation_score",
    "technical_relative_strength_score",
    "technical_tradability_score",
    "pivot_price",
    "trigger_price",
    "attractive_price_low",
    "attractive_price_high",
    "stop_price",
    "recommended_stop_price",
    "invalidation_price",
    "invalidation_rule",
    "target_price",
    "recommended_target_price",
    "exit_strategy",
    "active_exit_condition",
    "partial_exit_plan",
    "exit_condition_status",
    "published_at",
    "published_on",
    "observed_at",
    "load_ts",
    "event_status",
    "signal_action",
    "signal_status",
    "signal_source",
    "effect_type",
    "effect_summary",
    "previous_action",
    "action_changed",
    "authority_scope",
    "portfolio_authority",
    "broker_execution_allowed",
    "full_advisory_required",
    "router_context",
    "operator_next_step_kind",
    "operator_next_step",
    "final_state_trust",
    "confidence",
    "refreshed_at",
    "dry_run",
    "trace_id",
    "parse_status",
    "concise_summary_text",
    "summary",
}


def _compact_list_rows(rows: list[dict[str, Any]], *, compact: bool = False) -> list[dict[str, Any]]:
    if not compact:
        return rows
    compacted: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        compacted.append({
            key: _compact_home_field(key, row.get(key))
            for key in COMPACT_LIST_FIELDS
            if row.get(key) not in (None, "", [], {})
        })
    return compacted


def _compact_action_final_state_trust(value: Any) -> dict[str, Any] | None:
    trust = _jsonish(value)
    if not isinstance(trust, dict) or not trust:
        return None
    severity_rank = {"blocked": 0, "warning": 1, "execution_preview_required": 2, "manual_review": 3, "passed": 4}
    raw_checks = [check for check in trust.get("checks") or [] if isinstance(check, dict)]
    def check_sort_key(check: dict[str, Any]) -> tuple[int, int, str]:
        key = str(check.get("key") or check.get("label") or "")
        boundary_rank = -1 if key == "execution_boundary" else 0
        return (boundary_rank, severity_rank.get(str(check.get("status") or "").strip().lower(), 3), key)

    raw_checks.sort(key=check_sort_key)
    checks = []
    for check in raw_checks[:3]:
        item = {
            "key": _text(check.get("key")),
            "label": _text(check.get("label")),
            "status": _text(check.get("status")),
            "detail": _trim_home_value(check.get("detail"), max_text=90, max_list=0, max_depth=1),
        }
        checks.append({key: val for key, val in item.items() if val not in (None, "", [], {})})
    out = {
        "schema_version": trust.get("schema_version"),
        "status": _text(trust.get("status")),
        "final_action": _text(trust.get("final_action")),
        "display_status": _text(trust.get("display_status")),
        "broker_order_candidate": _json_ready(trust.get("broker_order_candidate")),
        "approved_filter_match": _json_ready(trust.get("approved_filter_match")),
        "live_submission_allowed": _json_ready(trust.get("live_submission_allowed")),
        "checks": checks,
        "blockers": _trim_home_value(trust.get("blockers"), max_text=110, max_list=2, max_depth=1),
        "warnings": _trim_home_value(trust.get("warnings"), max_text=110, max_list=2, max_depth=1),
        "operator_note": _trim_home_value(trust.get("operator_note"), max_text=120, max_list=0, max_depth=1),
    }
    return {key: val for key, val in out.items() if val not in (None, "", [], {})} or None


def _compact_action_reason_contract(value: Any) -> dict[str, Any] | None:
    reason = _jsonish(value)
    if not isinstance(reason, dict) or not reason:
        return None
    evidence = reason.get("evidence") if isinstance(reason.get("evidence"), dict) else {}
    compact_evidence: dict[str, Any] = {}
    for key in ["technical", "risk", "screener", "action_transition", "conflict_resolution", "company_memory", "event", "macro_regime"]:
        section = evidence.get(key)
        if isinstance(section, dict) and section:
            section_depth = 3 if key in {"conflict_resolution", "event", "macro_regime"} else 2
            compact_evidence[key] = _trim_home_value(section, max_text=220, max_list=4, max_depth=section_depth)
    event_section = compact_evidence.get("event")
    if isinstance(event_section, dict) and event_section.get("action_status") is not None:
        event_section["action_status"] = _display_reason_text(event_section.get("action_status")) or event_section.get("action_status")
    macro_section = compact_evidence.get("macro_regime")
    if isinstance(macro_section, dict) and macro_section.get("market_context_adjustment") is not None:
        macro_section["market_context_adjustment"] = (
            _display_reason_text(macro_section.get("market_context_adjustment")) or macro_section.get("market_context_adjustment")
        )
    missing_fields_raw = reason.get("missing_fields")
    if isinstance(missing_fields_raw, list):
        missing_fields = [_display_reason_text(item) or _text(item) for item in missing_fields_raw if _text(item)]
    else:
        missing_fields = _trim_home_value(missing_fields_raw, max_text=120, max_list=5, max_depth=1)
    out = {
        "status": _display_reason_text(reason.get("status")) or _text(reason.get("status")),
        "action_code": _text(reason.get("action_code")),
        "original_action_code": _text(reason.get("original_action_code") or reason.get("original_action")),
        "action_source": _text(reason.get("action_source")),
        "source_action": _text(reason.get("source_action")),
        "setup_id": _text(reason.get("setup_id")),
        "primary_reason": _trim_home_value(reason.get("primary_reason"), max_text=260, max_list=0, max_depth=1),
        "reason_detail": _trim_home_value(reason.get("reason_detail"), max_text=300, max_list=2, max_depth=1),
        "missing_fields": missing_fields,
        "evidence": compact_evidence,
    }
    return {key: val for key, val in out.items() if val not in (None, "", [], {})} or None


def _compact_manual_revision_pointers(value: Any) -> dict[str, Any] | None:
    pointers = _jsonish(value)
    if not isinstance(pointers, dict) or not pointers:
        return None
    risk_flags = []
    seen: set[str] = set()
    for item in pointers.get("risk_flags") or []:
        text = _text(item)
        if text and text not in seen:
            seen.add(text)
            risk_flags.append(text)
        if len(risk_flags) >= 4:
            break
    out = {
        "status": _text(pointers.get("status")),
        "revision_summary": _trim_home_value(pointers.get("revision_summary"), max_text=220, max_list=0, max_depth=1),
        "key_reasons": _trim_home_value(pointers.get("key_reasons"), max_text=140, max_list=3, max_depth=1),
        "manual_checks": _trim_home_value(pointers.get("manual_checks"), max_text=120, max_list=2, max_depth=1),
        "missing_data": _trim_home_value(pointers.get("missing_data"), max_text=100, max_list=2, max_depth=1),
        "operator_questions": _trim_home_value(pointers.get("operator_questions"), max_text=120, max_list=2, max_depth=1),
        "risk_flags": risk_flags,
    }
    return {key: val for key, val in out.items() if val not in (None, "", [], {})} or None


def _compact_action_list_field(key: str, value: Any) -> Any:
    if key == "final_state_trust":
        return _compact_action_final_state_trust(value)
    if key == "recommendation_reason":
        return _compact_action_reason_contract(value)
    if key == "manual_revision_pointers":
        return _compact_manual_revision_pointers(value)
    if key == "action_queue_contract":
        return _trim_home_value(value, max_text=220, max_list=5, max_depth=2)
    if key == "execution_safety_contract":
        return _trim_home_value(value, max_text=220, max_list=5, max_depth=3)
    return _compact_home_field(key, value)


def _compact_action_rows(rows: list[dict[str, Any]], *, compact: bool = False) -> list[dict[str, Any]]:
    if not compact:
        return rows
    compacted: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        compacted.append({
            key: _compact_action_list_field(key, row.get(key))
            for key in COMPACT_LIST_FIELDS
            if row.get(key) not in (None, "", [], {})
        })
    return compacted


def _compact_signal_router_context(payload_json: Any) -> dict[str, Any]:
    payload = _jsonish(payload_json, source="signal_refresh_action_payload_json")
    if not isinstance(payload, dict):
        return {}
    context = payload.get("router_context")
    if not isinstance(context, dict) or not context:
        return {}

    def _bounded_text_list(value: Any, *, limit: int = 6) -> list[str]:
        if not isinstance(value, list):
            return []
        out: list[str] = []
        for item in value:
            text = _text(item)
            if text:
                out.append(text[:120])
            if len(out) >= limit:
                break
        return out

    compact = {
        "source_types": _bounded_text_list(context.get("source_types")),
        "action_type": _text(context.get("action_type")),
        "reasons": _bounded_text_list(context.get("reasons")),
        "setup_ids": _bounded_text_list(context.get("setup_ids")),
        "priority_score": _json_ready(context.get("priority_score")),
        "best_rank": _json_ready(context.get("best_rank")),
        "start_at": _text(context.get("start_at")),
        "stop_at": _text(context.get("stop_at")),
        "include_watch": _json_ready(context.get("include_watch")),
        "include_lifecycle": _json_ready(context.get("include_lifecycle")),
        "routed_at": _text(context.get("routed_at")),
    }
    return {key: value for key, value in compact.items() if value not in (None, "", [], {})}


def _signal_refresh_next_step(row: dict[str, Any]) -> dict[str, str]:
    action = str(row.get("signal_action") or "").strip().upper()
    status = str(row.get("signal_status") or "").strip().lower()
    source = str(row.get("signal_source") or "").strip().lower()
    effect = str(row.get("effect_type") or "").strip().lower()
    reason = str(row.get("reason") or "").strip().lower()

    if any(token in action for token in ("SELL", "EXIT", "REDUCE")) or "exit" in status or "reduce" in status:
        return {
            "kind": "review_exposure",
            "text": "Review position exposure and evidence now. Run full advisory before changing authoritative portfolio, sizing, or broker execution state.",
        }
    if action in {"WATCH", "WATCHLIST", "NEAR_PIVOT", "READY"} or "watch" in status:
        return {
            "kind": "watch_for_confirmation",
            "text": "Inspect the trigger evidence and keep watching for confirmation. Run full advisory for authoritative entry, stop, target, and sizing.",
        }
    if action in {"MANUAL_REVIEW", "REVIEW", "REDUCE_EXPOSURE_REVIEW", "GO_CASH_REVIEW"} or "review" in status:
        return {
            "kind": "manual_review",
            "text": "Open the symbol, event evidence, or Manual Review queue. This row is review input only; full advisory is required before portfolio mutation.",
        }
    if effect == "wait_match_created" or source == "wait_signal":
        return {
            "kind": "inspect_wait_match",
            "text": "Inspect the matched wait signal and source evidence. Treat the match as evidence, not approval.",
        }
    if "router" in reason or source.startswith("router_"):
        return {
            "kind": "inspect_router_trigger",
            "text": "Inspect the watcher router trigger. If it is material, run symbol refresh or full advisory after source data catches up.",
        }
    return {
        "kind": "no_immediate_action",
        "text": "No immediate operator action is implied by this refresh. It is evidence visibility unless later advisory output changes.",
    }


def _json_byte_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception as exc:
        _record_operator_local_fallback(
            source="operator_api_payload_size",
            fallback_type="operator_api_json_byte_size_failed",
            reason="Operator API could not JSON-serialize a payload row for byte-size telemetry and used repr/string byte length.",
            error=exc,
            metadata={"value_type": type(value).__name__, "value_excerpt": redact_text(str(value)[:240])},
        )
        return len(str(value).encode("utf-8"))


def _payload_size_meta(rows: list[dict[str, Any]]) -> dict[str, Any]:
    row_sizes = [_json_byte_size(row) for row in rows if isinstance(row, dict)]
    total = sum(row_sizes)
    return {
        "payload_bytes": total,
        "avg_row_bytes": round(total / len(row_sizes), 2) if row_sizes else 0,
        "max_row_bytes": max(row_sizes) if row_sizes else 0,
    }


def _payload_rows_by_kind(payload: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    if kind == "actions":
        keys = ["top_action_recommendations", "action_recommendations", "alerts"]
    elif kind == "portfolio":
        keys = ["today_recommendations", "current_recommendations", "exited_recommendations", "portfolio", "lifecycle"]
    elif kind == "events":
        keys = ["watch_events", "operator_feed", "alerts"]
    else:
        keys = []
    rows: list[dict[str, Any]] = []
    for key in keys:
        for row in payload.get(key) or []:
            if isinstance(row, dict):
                item = dict(row)
                item.setdefault("_source_section", key)
                rows.append(item)
    return rows


def _detail_payload(kind: str, rows: list[dict[str, Any]], *, filters: dict[str, Any]) -> dict[str, Any]:
    schema_by_kind = {
        "actions": ("/api/actions/detail", "operator_action_detail"),
        "portfolio": ("/api/portfolio/{symbol}/detail", "operator_portfolio_detail"),
        "events": ("/api/events/{unique_id}/detail", "operator_event_detail"),
    }
    endpoint, schema_name = schema_by_kind.get(kind, (f"/api/{kind}/detail", f"operator_{kind}_detail"))
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema(endpoint, schema_name=schema_name),
        "status": "ok" if rows else "not_found",
        "kind": kind,
        "filters": filters,
        "rows": rows,
        "row_count": len(rows),
    }


def _build_feature_freshness_stage_gates(*, symbol: str, asof_date: str | None = None) -> list[dict[str, Any]]:
    gates: list[dict[str, Any]] = []
    normalized_symbol = str(symbol or "").strip().upper()
    for stage in FEATURE_FRESHNESS_OPERATOR_STAGES:
        try:
            gate = evaluate_stage_feature_gate(stage, [normalized_symbol] if normalized_symbol else [], asof_date=asof_date)
        except Exception as exc:
            _record_operator_local_fallback(
                source="operator_api_feature_freshness_stage_gate",
                fallback_type="operator_api_feature_freshness_stage_gate_failed",
                reason="Operator API could not evaluate a current stage feature gate for the symbol detail data-input view.",
                error=exc,
                metadata={"symbol": normalized_symbol, "stage": stage, "asof_date": asof_date},
            )
            gate = {
                "stage": stage,
                "status": "error",
                "symbols_checked": 1 if normalized_symbol else 0,
                "blocked_symbols": [normalized_symbol] if normalized_symbol else [],
                "blocked_count": 1 if normalized_symbol else 0,
                "required_input_keys": [],
                "context_input_keys": [],
                "gate_effect": "Stage feature gate evaluation failed; treat this stage as blocked until the error is fixed.",
                "block_positive_actions": True,
                "symbols": {
                    normalized_symbol: {
                        "symbol": normalized_symbol,
                        "status": "blocked",
                        "counts": {"error": 1},
                        "blockers": [
                            {
                                "input_key": "stage_gate_evaluation",
                                "label": "Stage gate evaluation",
                                "status": "error",
                                "reason": f"{type(exc).__name__}: {exc}",
                                "required": True,
                            }
                        ],
                        "required_inputs": [],
                        "context_inputs": [],
                    }
                }
                if normalized_symbol
                else {},
            }
        gates.append(_json_ready(gate))
    return gates


def _feature_freshness_stage_gate_summary(stage_gates: list[dict[str, Any]]) -> dict[str, Any]:
    blocked_stages: list[str] = []
    error_stages: list[str] = []
    blocked_inputs: list[dict[str, Any]] = []
    context_inputs: list[dict[str, Any]] = []
    seen_inputs: set[tuple[str, str, str]] = set()
    seen_context_inputs: set[tuple[str, str, str]] = set()
    for gate in stage_gates:
        stage = str(gate.get("stage") or "").strip()
        status = str(gate.get("status") or "").strip().lower()
        if status == "blocked":
            blocked_stages.append(stage)
        if status == "error":
            error_stages.append(stage)
        symbols = gate.get("symbols") if isinstance(gate.get("symbols"), dict) else {}
        for symbol_summary in symbols.values():
            if not isinstance(symbol_summary, dict):
                continue
            for blocker in symbol_summary.get("blockers") or []:
                if not isinstance(blocker, dict):
                    continue
                key = (
                    str(stage),
                    str(blocker.get("input_key") or blocker.get("label") or ""),
                    str(blocker.get("status") or ""),
                )
                if key in seen_inputs:
                    continue
                seen_inputs.add(key)
                item = dict(blocker)
                item.setdefault("stage", stage)
                blocked_inputs.append(item)
            for context_input in symbol_summary.get("context_inputs") or []:
                if not isinstance(context_input, dict):
                    continue
                key = (
                    str(stage),
                    str(context_input.get("input_key") or context_input.get("label") or ""),
                    str(context_input.get("status") or ""),
                )
                if key in seen_context_inputs:
                    continue
                seen_context_inputs.add(key)
                item = dict(context_input)
                item.setdefault("stage", stage)
                item.setdefault("blocking", False)
                context_inputs.append(item)
    return {
        "stage_count": len(stage_gates),
        "blocked_stage_count": len(blocked_stages),
        "error_stage_count": len(error_stages),
        "blocked_stages": blocked_stages,
        "error_stages": error_stages,
        "blocked_inputs": blocked_inputs,
        "context_input_count": len(context_inputs),
        "context_warning_count": sum(
            1
            for item in context_inputs
            if str(item.get("status") or "").strip().lower() in {"missing", "stale", "error", "intentionally_skipped"}
        ),
        "context_inputs": context_inputs,
        "operator_boundary": {
            "read_only": True,
            "mutates_recommendations": False,
            "mutates_portfolio": False,
            "broker_execution_allowed": False,
            "note": "Current feature stage-gate checks explain blockers only; advisory/actions must rerun before decisions change.",
        },
    }


def build_feature_freshness_payload(*, symbol: str, asof_date: str | None = None) -> dict[str, Any]:
    payload = build_feature_freshness_contract(symbol, asof_date=asof_date)
    stage_gates = _build_feature_freshness_stage_gates(symbol=symbol, asof_date=asof_date)
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema("/api/symbols/{symbol}/feature-freshness", schema_name="feature_freshness"),
        "stage_gates": stage_gates,
        "stage_gate_summary": _feature_freshness_stage_gate_summary(stage_gates),
        **payload,
    }


def ensure_operator_api_errors_table() -> None:
    ensure_operator_api_audit_tables()


def ensure_operator_api_audit_tables() -> None:
    apply_schema_migration(
        migration_id=OPERATOR_API_AUDIT_SCHEMA_MIGRATION_ID,
        description="Create operator API audit, command-run, and manual-review decision tables.",
        statements=OPERATOR_API_AUDIT_SCHEMA_STATEMENTS,
        metadata={"tables": [OPERATOR_API_ERRORS_TABLE, OPERATOR_COMMAND_RUNS_TABLE, MANUAL_REVIEW_DECISIONS_TABLE]},
    )






def ensure_execution_live_allowance_reviews_table() -> None:
    apply_schema_migration(
        migration_id=EXECUTION_LIVE_ALLOWANCE_SCHEMA_MIGRATION_ID,
        description="Create execution live-allowance review audit table.",
        statements=EXECUTION_LIVE_ALLOWANCE_SCHEMA_STATEMENTS,
        metadata={"tables": [EXECUTION_LIVE_ALLOWANCE_REVIEWS_TABLE]},
    )


def _safe_json_dumps(value: Any) -> str:
    try:
        return json.dumps(_json_ready(redact_mapping(value) if isinstance(value, dict) else value), ensure_ascii=False, default=str)
    except Exception as exc:
        _record_operator_local_fallback(
            source="operator_api_safe_json_dumps",
            fallback_type="operator_api_safe_json_dumps_failed",
            reason="Operator API could not serialize an audit/request context payload and used the serialization-error fallback.",
            error=exc,
            metadata={"value_type": type(value).__name__, "value_excerpt": redact_text(repr(value)[:240])},
        )
        return json.dumps({"serialization_error": True, "repr": redact_text(repr(value))}, ensure_ascii=False)


def record_operator_api_error(
    *,
    exc: Exception,
    operation: str,
    route: str | None = None,
    status_code: int = 500,
    request_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    occurred_at = pd.Timestamp.utcnow()
    row = {
        "error_id": f"api:{occurred_at.strftime('%Y%m%dT%H%M%S%fZ')}:{uuid.uuid4().hex[:10]}",
        "occurred_at": occurred_at,
        "route": _text(route),
        "operation": _text(operation),
        "status_code": int(status_code),
        "error_type": type(exc).__name__,
        "error_message": redact_text(str(exc)),
        "traceback_tail": redact_text(_tail_text("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)), max_chars=OPERATOR_API_ERROR_TRACE_CHARS)),
        "request_context_json": _safe_json_dumps(request_context or {}),
        "load_ts": occurred_at,
    }
    try:
        ensure_operator_api_errors_table()
        upsert_to_db(pd.DataFrame([row]), OPERATOR_API_ERRORS_TABLE, unique_keys=["error_id"])
    except Exception as audit_exc:
        # Error auditing must not mask the API error being reported to the frontend.
        record_local_fallback_event(
            module="advisory.api.app",
            source=OPERATOR_API_ERRORS_TABLE,
            fallback_type="operator_api_error_audit_write_failed",
            severity="error",
            reason="Operator API could not persist an API error audit row; the original API response continues.",
            error=audit_exc,
            metadata={
                "error_id": row["error_id"],
                "operation": row["operation"],
                "route": row["route"],
                "status_code": row["status_code"],
                "error_type": row["error_type"],
            },
        )
    return row


def record_operator_api_marker(
    *,
    route: str,
    operation: str,
    message: str,
    status_code: int = 299,
    context: dict[str, Any] | None = None,
) -> None:
    occurred_at = pd.Timestamp.utcnow()
    row = {
        "error_id": f"api-marker:{occurred_at.strftime('%Y%m%dT%H%M%S%fZ')}:{uuid.uuid4().hex[:10]}",
        "occurred_at": occurred_at,
        "route": _text(route),
        "operation": _text(operation),
        "status_code": int(status_code),
        "error_type": "FallbackUsed",
        "error_message": redact_text(message),
        "traceback_tail": None,
        "request_context_json": _safe_json_dumps(context or {}),
        "load_ts": occurred_at,
    }
    try:
        ensure_operator_api_errors_table()
        upsert_to_db(pd.DataFrame([row]), OPERATOR_API_ERRORS_TABLE, unique_keys=["error_id"])
    except Exception as audit_exc:
        record_local_fallback_event(
            module="advisory.api.app",
            source=OPERATOR_API_ERRORS_TABLE,
            fallback_type="operator_api_marker_audit_write_failed",
            severity="warn",
            reason="Operator API could not persist a marker audit row; the original API flow continues.",
            error=audit_exc,
            metadata={
                "error_id": row["error_id"],
                "operation": row["operation"],
                "route": row["route"],
                "status_code": row["status_code"],
                "error_type": row["error_type"],
            },
        )


def build_operator_api_errors_payload(*, limit: int = 50) -> dict[str, Any]:
    if not _table_exists(OPERATOR_API_ERRORS_TABLE):
        return {
            "generated_at": pd.Timestamp.utcnow().isoformat(),
            "api_schema": _operator_api_schema("/api/operations/api-errors", schema_name="operations_api_errors"),
            "status": "ok",
            "errors": [],
            "summary": {"total": 0, "error": 0, "warn": 0},
        }
    df = sql_to_df(
        f"""
        SELECT *
        FROM {OPERATOR_API_ERRORS_TABLE}
        ORDER BY occurred_at DESC
        LIMIT %(limit)s
        """,
        params={"limit": max(1, int(limit))},
        retries=3,
    )
    rows = _records(df)
    for row in rows:
        row["error_message"] = redact_text(row.get("error_message"))
        row["traceback_tail"] = redact_text(row.get("traceback_tail"))
        row["request_context"] = _jsonish(row.pop("request_context_json", None))
        if isinstance(row["request_context"], dict):
            row["request_context"] = redact_mapping(row["request_context"])
    error_count = sum(1 for row in rows if int(row.get("status_code") or 500) >= 500)
    warn_count = len(rows) - error_count
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema("/api/operations/api-errors", schema_name="operations_api_errors"),
        "status": "error" if error_count else "warn" if warn_count else "ok",
        "errors": rows,
        "summary": {"total": len(rows), "error": error_count, "warn": warn_count},
    }


def update_slow_issue_payload(payload: dict[str, Any]) -> dict[str, Any]:
    fingerprint = str(payload.get("fingerprint") or "").strip()
    status = str(payload.get("status") or "").strip().lower()
    note = str(payload.get("note") or "").strip()
    operator_id = str(payload.get("operator_id") or "").strip() or "operator"
    if not fingerprint:
        raise ValueError("fingerprint is required")
    if status not in SLOW_ISSUE_ALLOWED_STATUSES:
        raise ValueError(f"status must be one of: {', '.join(sorted(SLOW_ISSUE_ALLOWED_STATUSES))}")
    if status in {"fixed", "ignored"} and not note:
        raise ValueError("note is required when marking a slow issue fixed or ignored")
    full_note = f"operator={operator_id}"
    if note:
        full_note = f"{full_note}; {note}"
    issue = update_slow_issue_status(fingerprint, status=status, note=full_note)
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "status": "ok",
        "fingerprint": fingerprint,
        "new_status": status,
        "issue": _json_ready(issue),
    }


def build_technical_calibration_payload(*, limit: int = 25) -> dict[str, Any]:
    if not _table_exists(TECHNICAL_CALIBRATION_SUMMARY_TABLE):
        return {
            "generated_at": pd.Timestamp.utcnow().isoformat(),
            "api_schema": _operator_api_schema("/api/technical-calibration", schema_name="technical_calibration"),
            "status": "missing_table",
            "summary": [],
            "top_configs": [],
        }
    summary = sql_to_df(
        f"""
        SELECT *
        FROM {TECHNICAL_CALIBRATION_SUMMARY_TABLE}
        WHERE evaluated_at = (
            SELECT MAX(evaluated_at)
            FROM {TECHNICAL_CALIBRATION_SUMMARY_TABLE}
        )
        ORDER BY horizon_days
        """,
        retries=3,
    )
    top_configs = pd.DataFrame()
    if _table_exists(TECHNICAL_CALIBRATION_EVALUATIONS_TABLE):
        top_configs = sql_to_df(
            f"""
            WITH latest AS (
                SELECT MAX(evaluated_at) AS evaluated_at
                FROM {TECHNICAL_CALIBRATION_EVALUATIONS_TABLE}
            ),
            ranked AS (
                SELECT
                    e.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY e.horizon_days
                        ORDER BY e.objective_score DESC NULLS LAST, e.eligible_count DESC NULLS LAST, e.hit_rate_after_cost DESC NULLS LAST
                    ) AS rn
                FROM {TECHNICAL_CALIBRATION_EVALUATIONS_TABLE} e
                JOIN latest ON latest.evaluated_at = e.evaluated_at
            )
            SELECT *
            FROM ranked
            WHERE rn <= %(limit)s
            ORDER BY horizon_days, rn
            """,
            params={"limit": max(1, int(limit))},
            retries=3,
        )
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema("/api/technical-calibration", schema_name="technical_calibration"),
        "status": "ok",
        "summary": _records(summary),
        "top_configs": _records(top_configs),
    }


def build_signal_quality_payload(*, limit: int = 25) -> dict[str, Any]:
    if not _table_exists(SIGNAL_QUALITY_SUMMARY_TABLE):
        return {
            "generated_at": pd.Timestamp.utcnow().isoformat(),
            "api_schema": _operator_api_schema("/api/signal-quality", schema_name="signal_quality"),
            "status": "missing_table",
            "latest_evaluated_at": None,
            "summary": [],
            "examples": [],
            "coverage": [],
            "meta": {
                "note": "Run python -m advisory.signal_quality_evaluator --from-date YYYY-MM-DD --to-date YYYY-MM-DD --horizons 5 10 20",
                "research_only": True,
            },
        }

    latest_df = sql_to_df(
        f"""
        SELECT MAX(evaluated_at) AS latest_evaluated_at
        FROM {SIGNAL_QUALITY_SUMMARY_TABLE}
        """,
        retries=3,
    )
    latest = None if latest_df.empty else pd.to_datetime(latest_df.iloc[0].get("latest_evaluated_at"), utc=True, errors="coerce")
    if latest is None or pd.isna(latest):
        return {
            "generated_at": pd.Timestamp.utcnow().isoformat(),
            "api_schema": _operator_api_schema("/api/signal-quality", schema_name="signal_quality"),
            "status": "empty",
            "latest_evaluated_at": None,
            "summary": [],
            "examples": [],
            "coverage": [],
            "meta": {"research_only": True},
        }

    summary = sql_to_df(
        f"""
        SELECT *
        FROM {SIGNAL_QUALITY_SUMMARY_TABLE}
        WHERE evaluated_at = %(latest)s
        ORDER BY horizon_days, variant
        """,
        params={"latest": latest},
        retries=3,
    )

    examples = pd.DataFrame()
    coverage = pd.DataFrame()
    if _table_exists(SIGNAL_QUALITY_EVALUATIONS_TABLE):
        examples = sql_to_df(
            f"""
            WITH ranked AS (
                SELECT
                    e.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY e.horizon_days, e.variant
                        ORDER BY e.forward_return_after_cost DESC NULLS LAST, e.technical_total_score DESC NULLS LAST, e.symbol
                    ) AS rn
                FROM {SIGNAL_QUALITY_EVALUATIONS_TABLE} e
                WHERE e.evaluated_at = %(latest)s
                  AND e.selected IS TRUE
                  AND e.matured IS TRUE
            )
            SELECT *
            FROM ranked
            WHERE rn <= %(limit)s
            ORDER BY horizon_days, variant, rn
            """,
            params={"latest": latest, "limit": max(1, int(limit))},
            retries=3,
        )
        coverage = sql_to_df(
            f"""
            SELECT
                horizon_days,
                COUNT(*) AS candidate_rows,
                SUM(CASE WHEN event_action_type IS NOT NULL THEN 1 ELSE 0 END) AS event_policy_rows,
                SUM(CASE WHEN bhavcopy_deal_pressure IS NOT NULL THEN 1 ELSE 0 END) AS bhavcopy_rows,
                SUM(CASE WHEN company_memory_signal IS NOT NULL THEN 1 ELSE 0 END) AS company_memory_rows,
                SUM(CASE WHEN event_positive IS TRUE THEN 1 ELSE 0 END) AS event_positive_rows,
                SUM(CASE WHEN event_negative IS TRUE THEN 1 ELSE 0 END) AS event_negative_rows,
                SUM(CASE WHEN bhavcopy_positive IS TRUE THEN 1 ELSE 0 END) AS bhavcopy_positive_rows,
                SUM(CASE WHEN bhavcopy_negative IS TRUE THEN 1 ELSE 0 END) AS bhavcopy_negative_rows,
                SUM(CASE WHEN company_memory_positive IS TRUE THEN 1 ELSE 0 END) AS company_memory_positive_rows,
                SUM(CASE WHEN company_memory_negative IS TRUE THEN 1 ELSE 0 END) AS company_memory_negative_rows
            FROM {SIGNAL_QUALITY_EVALUATIONS_TABLE}
            WHERE evaluated_at = %(latest)s
              AND variant = 'technical_only'
            GROUP BY horizon_days
            ORDER BY horizon_days
            """,
            params={"latest": latest},
            retries=3,
        )

    summary_records = _records(summary)
    best_by_horizon: dict[str, dict[str, Any]] = {}
    for row in summary_records:
        horizon_key = str(row.get("horizon_days") or "")
        if not horizon_key:
            continue
        if str(row.get("variant") or "") == "technical_only":
            continue
        current = best_by_horizon.get(horizon_key)
        lift = pd.to_numeric(row.get("lift_vs_technical_only"), errors="coerce")
        current_lift = pd.to_numeric(current.get("lift_vs_technical_only"), errors="coerce") if current else pd.NA
        if current is None or (pd.notna(lift) and (pd.isna(current_lift) or float(lift) > float(current_lift))):
            best_by_horizon[horizon_key] = row

    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema("/api/signal-quality", schema_name="signal_quality"),
        "status": "ok",
        "latest_evaluated_at": latest.isoformat(),
        "summary": summary_records,
        "examples": _records(examples),
        "coverage": _records(coverage),
        "meta": {
            "research_only": True,
            "best_overlay_by_horizon": best_by_horizon,
            "example_limit_per_variant": max(1, int(limit)),
            "production_policy_changed": False,
        },
    }


def build_technical_threshold_promotion_review_payload(payload: dict[str, Any]) -> dict[str, Any]:
    setup_id = str(payload.get("setup_id") or "").strip()
    config_id = str(payload.get("config_id") or "").strip()
    if not setup_id:
        raise ValueError("setup_id is required")
    if not config_id:
        raise ValueError("config_id is required")
    result = generate_promotion_review(
        setup_id=setup_id,
        config_id=config_id,
        model=str(payload.get("model") or "") or None,
        use_llm=bool(payload.get("use_llm", True)),
        persist=True,
    )
    result["api_schema"] = _operator_api_schema("/api/technical-calibration/promotion-review", schema_name="technical_promotion_review")
    return result


def build_technical_threshold_reviews_payload(*, limit: int = 25) -> dict[str, Any]:
    reviews = load_promotion_reviews(limit=limit)
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema("/api/technical-calibration/promotion-reviews", schema_name="technical_promotion_reviews"),
        "status": "ok",
        "reviews": reviews,
        "pagination": {"reviews": _bounded_list_contract(reviews, limit=limit, default=25, maximum=100)},
    }


def build_technical_threshold_review_decision_payload(payload: dict[str, Any]) -> dict[str, Any]:
    reviewed_at = payload.get("reviewed_at")
    setup_id = str(payload.get("setup_id") or "").strip()
    config_id = str(payload.get("config_id") or "").strip()
    decision = str(payload.get("decision") or "").strip().lower()
    if not reviewed_at:
        raise ValueError("reviewed_at is required")
    if not setup_id:
        raise ValueError("setup_id is required")
    if not config_id:
        raise ValueError("config_id is required")
    if decision not in {"approved", "rejected", "needs_more_data"}:
        raise ValueError("decision must be approved, rejected, or needs_more_data")
    result = record_manual_decision(
        reviewed_at=reviewed_at,
        setup_id=setup_id,
        config_id=config_id,
        decision=decision,  # type: ignore[arg-type]
        operator_id=str(payload.get("operator_id") or "") or None,
        decision_reason=str(payload.get("decision_reason") or "") or None,
    )
    result["api_schema"] = _operator_api_schema(
        "/api/technical-calibration/promotion-review/decision",
        schema_name="technical_promotion_decision",
    )
    return result


def build_signal_quality_promotion_review_payload(payload: dict[str, Any]) -> dict[str, Any]:
    evaluated_at = payload.get("evaluated_at")
    horizon_days = payload.get("horizon_days")
    variant = str(payload.get("variant") or "").strip()
    if not evaluated_at:
        raise ValueError("evaluated_at is required")
    if horizon_days is None or str(horizon_days).strip() == "":
        raise ValueError("horizon_days is required")
    if not variant:
        raise ValueError("variant is required")
    result = generate_signal_quality_promotion_review(
        evaluated_at=evaluated_at,
        horizon_days=int(horizon_days),
        variant=variant,
        persist=True,
    )
    result["api_schema"] = _operator_api_schema("/api/signal-quality/promotion-review", schema_name="signal_quality_promotion_review")
    return result


def build_signal_quality_promotion_reviews_payload(*, limit: int = 25) -> dict[str, Any]:
    reviews = load_signal_quality_promotion_reviews(limit=limit)
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema("/api/signal-quality/promotion-reviews", schema_name="signal_quality_promotion_reviews"),
        "status": "ok",
        "reviews": reviews,
        "pagination": {"reviews": _bounded_list_contract(reviews, limit=limit, default=25, maximum=100)},
    }


def build_signal_quality_promotion_review_decision_payload(payload: dict[str, Any]) -> dict[str, Any]:
    reviewed_at = payload.get("reviewed_at")
    evaluated_at = payload.get("evaluated_at")
    horizon_days = payload.get("horizon_days")
    variant = str(payload.get("variant") or "").strip()
    decision = str(payload.get("decision") or "").strip().lower()
    if not reviewed_at:
        raise ValueError("reviewed_at is required")
    if not evaluated_at:
        raise ValueError("evaluated_at is required")
    if horizon_days is None or str(horizon_days).strip() == "":
        raise ValueError("horizon_days is required")
    if not variant:
        raise ValueError("variant is required")
    if decision not in {"approved", "rejected", "needs_more_data"}:
        raise ValueError("decision must be approved, rejected, or needs_more_data")
    result = record_signal_quality_manual_decision(
        reviewed_at=reviewed_at,
        evaluated_at=evaluated_at,
        horizon_days=int(horizon_days),
        variant=variant,
        decision=decision,  # type: ignore[arg-type]
        operator_id=str(payload.get("operator_id") or "") or None,
        decision_reason=str(payload.get("decision_reason") or "") or None,
    )
    result["api_schema"] = _operator_api_schema(
        "/api/signal-quality/promotion-review/decision",
        schema_name="signal_quality_promotion_decision",
    )
    return result














def build_config_change_previews_payload(*, limit: int = 25) -> dict[str, Any]:
    previews = load_config_change_previews(limit=limit)
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema("/api/config-change/previews", schema_name="config_change_previews"),
        "status": "ok",
        "previews": previews,
        "pagination": {"previews": _bounded_list_contract(previews, limit=limit, default=25, maximum=100)},
    }


def build_config_change_applications_payload(*, limit: int = 25) -> dict[str, Any]:
    applications = load_application_decisions(limit=limit)
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema("/api/config-change/applications", schema_name="config_change_applications"),
        "status": "ok",
        "applications": applications,
        "pagination": {"applications": _bounded_list_contract(applications, limit=limit, default=25, maximum=100)},
    }


def build_config_change_application_decision_payload(payload: dict[str, Any]) -> dict[str, Any]:
    preview_id = str(payload.get("preview_id") or "").strip()
    application_decision = str(payload.get("application_decision") or payload.get("decision") or "").strip().lower()
    if not preview_id:
        raise ValueError("preview_id is required")
    if application_decision not in {"approved_to_apply", "marked_applied", "rejected", "needs_more_data"}:
        raise ValueError("application_decision must be approved_to_apply, marked_applied, rejected, or needs_more_data")
    result = record_application_decision(
        preview_id=preview_id,
        application_decision=application_decision,  # type: ignore[arg-type]
        operator_id=str(payload.get("operator_id") or "") or None,
        operator_note=str(payload.get("operator_note") or payload.get("note") or "") or None,
        verify_config=bool(payload.get("verify_config", True)),
    )
    result["generated_at"] = pd.Timestamp.utcnow().isoformat()
    result["api_schema"] = _operator_api_schema("/api/config-change/application-decision", schema_name="config_change_application_decision")
    return result


def build_technical_config_change_preview_payload(payload: dict[str, Any]) -> dict[str, Any]:
    setup_id = str(payload.get("setup_id") or "").strip()
    config_id = str(payload.get("config_id") or "").strip()
    if not setup_id:
        raise ValueError("setup_id is required")
    if not config_id:
        raise ValueError("config_id is required")
    result = build_technical_threshold_preview(
        setup_id=setup_id,
        config_id=config_id,
        reviewed_at=payload.get("reviewed_at"),
        persist=bool(payload.get("persist", True)),
    )
    result["api_schema"] = _operator_api_schema("/api/config-change/technical-threshold-preview", schema_name="technical_threshold_config_change_preview")
    return result


def build_signal_quality_config_change_preview_payload(payload: dict[str, Any]) -> dict[str, Any]:
    evaluated_at = payload.get("evaluated_at")
    horizon_days = payload.get("horizon_days")
    variant = str(payload.get("variant") or "").strip()
    if not evaluated_at:
        raise ValueError("evaluated_at is required")
    if horizon_days is None or str(horizon_days).strip() == "":
        raise ValueError("horizon_days is required")
    if not variant:
        raise ValueError("variant is required")
    result = build_signal_quality_overlay_preview(
        evaluated_at=evaluated_at,
        horizon_days=int(horizon_days),
        variant=variant,
        reviewed_at=payload.get("reviewed_at"),
        persist=bool(payload.get("persist", True)),
    )
    result["api_schema"] = _operator_api_schema("/api/config-change/signal-quality-preview", schema_name="signal_quality_config_change_preview")
    return result






def build_prompt_registry_api_payload(*, owner_area: str | None = None, authority_scope: str | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    payload = build_prompt_registry_payload(owner_area=owner_area, authority_scope=authority_scope)
    contracts = [row for row in payload.get("contracts") or [] if isinstance(row, dict)]
    contract_page, contract_meta = _page_any_rows(contracts, limit=limit, offset=offset, default=100, maximum=500)
    payload["contracts"] = contract_page
    payload["pagination"] = {"contracts": contract_meta}
    payload["api_schema"] = _operator_api_schema("/api/research/prompt-registry", schema_name="prompt_registry")
    return payload


def build_screener_preview_payload(payload: dict[str, Any]) -> dict[str, Any]:
    query_text = str(payload.get("query_text") or payload.get("query") or "").strip()
    if not query_text:
        raise ValueError("query_text is required")
    query_name = str(payload.get("query_name") or payload.get("name") or "Operator Screener Preview").strip() or "Operator Screener Preview"
    fetch_rows = bool(payload.get("fetch_rows") or payload.get("run_query"))
    row_limit = min(max(int(payload.get("row_limit") or 25), 1), 100)
    query_hash = hashlib.sha256(query_text.encode("utf-8")).hexdigest()
    validation_issues = [
        {"code": issue.code, "text": issue.text, "suggestion": issue.suggestion}
        for issue in validate_screener_query(query_text)
    ]
    api_schema = _operator_api_schema("/api/screeners/preview", schema_name="screener_preview")
    api_schema["read_only"] = False
    api_schema["write_scope"] = "failure_audit_only_when_authenticated_fetch_fails"
    api_schema["investment_state_mutation_enabled"] = False
    base = {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": api_schema,
        "query_name": query_name,
        "query_slug": screener_slugify(query_name),
        "query_hash": query_hash,
        "screener_url": build_raw_screen_url(query_text),
        "validation_issues": validation_issues,
        "row_count": 0,
        "rows": [],
        "headers": [],
        "meta": {
            "fetch_rows_requested": fetch_rows,
            "persisted": False,
            "row_limit": row_limit,
            "query_length": len(query_text),
        },
        "operator_boundary": {
            "read_only": False,
            "investment_state_read_only": True,
            "failure_audit_write_possible": fetch_rows,
            "write_scope": "failure_audit_only_when_authenticated_fetch_fails",
            "broker_execution_enabled": False,
            "persists_query_results": False,
            "registers_production_screener": False,
            "side_effects": "Validation is local. Fetch preview may access Screener.in and records failure audit rows if the authenticated fetch/parse fails, but it does not persist query results or register a screener.",
            "next_step_if_good": "Register the query explicitly through the screener registry only after reviewing preview rows and validation outcome.",
        },
    }
    if validation_issues:
        return {
            **base,
            "status": "invalid",
            "meta": {**base["meta"], "can_fetch": False},
        }
    if not fetch_rows:
        return {
            **base,
            "status": "valid",
            "meta": {**base["meta"], "can_fetch": True, "validation_only": True},
        }

    result = fetch_ad_hoc_payload(query_text=query_text, query_name=query_name, persist=False)
    companies = result.get("companies") or []
    return {
        **base,
        "status": "ok",
        "screener_url": result.get("screener_url") or base["screener_url"],
        "row_count": int(len(companies)),
        "rows": companies_preview(companies, limit=row_limit),
        "headers": result.get("headers") or [],
        "meta": {
            **base["meta"],
            "can_fetch": True,
            "validation_only": False,
            "query_run_id": result.get("query_run_id"),
            "returned_rows": min(len(companies), row_limit),
            "omitted_rows": max(0, len(companies) - row_limit),
        },
    }


def build_screener_coverage_api_payload(*, asof_date: str | None = None, lookback_days: int = 30, limit: int = 50) -> dict[str, Any]:
    payload = build_screener_coverage_payload(asof_date=asof_date, lookback_days=lookback_days, limit=limit)
    payload["api_schema"] = _operator_api_schema("/api/screeners/coverage", schema_name="screener_coverage")
    return payload


def build_screener_failures_api_payload(*, hours: int = 24, limit: int = 25) -> dict[str, Any]:
    payload = check_screener_failures(hours=max(1, int(hours)), limit=max(1, int(limit)))
    out = {
        **payload,
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema("/api/screeners/failures", schema_name="screener_failures"),
        "operator_boundary": {
            "read_only": True,
            "manual_review_item_source": False,
            "broker_execution_enabled": False,
            "registers_production_screener": False,
            "clears_failure_rows": False,
            "side_effects": "Read-only failure audit view. It does not clear failures, retry Screener.in, register screeners, change recommendations, or submit broker orders.",
            "operator_action": "Fix query syntax, login/session, or parser issue; then rerun the affected Screener workflow and refresh Health/Screeners.",
        },
    }
    out.setdefault("rows", [])
    out.setdefault("counts_by_stage", {})
    return out




def build_action_detail_payload(*, symbol: str | None = None, unique_id: str | None = None, setup_id: str | None = None, asof_date: str | None = None) -> dict[str, Any]:
    payload = load_operator_payload(asof_date=asof_date)
    rows = _payload_rows_by_kind(payload, "actions")
    rows = _filter_rows(rows, symbol=symbol, search=unique_id)
    normalized_setup = str(setup_id or "").strip().upper()
    if normalized_setup:
        rows = [row for row in rows if str(row.get("setup_id") or "").strip().upper() == normalized_setup]
    out = _detail_payload("actions", rows, filters={"symbol": symbol, "unique_id": unique_id, "setup_id": setup_id, "asof_date": asof_date})
    normalized_symbol = str(symbol or (rows[0].get("symbol") if rows else "") or "").strip().upper()
    if normalized_symbol:
        out["feature_freshness"] = build_feature_freshness_payload(symbol=normalized_symbol, asof_date=asof_date)
    return out


def load_lifecycle_policy_changes_for_symbol(symbol: str, *, limit: int = 25) -> list[dict[str, Any]]:
    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_symbol:
        return []
    if not _table_exists(LIFECYCLE_POLICY_CHANGES_TABLE):
        return []
    try:
        df = sql_to_df(
            f"""
            SELECT *
            FROM {LIFECYCLE_POLICY_CHANGES_TABLE}
            WHERE UPPER(symbol) = %(symbol)s
            ORDER BY changed_at DESC NULLS LAST, load_ts DESC NULLS LAST
            LIMIT %(limit)s
            """,
            params={"symbol": normalized_symbol, "limit": max(1, int(limit))},
            retries=2,
        )
    except Exception as exc:
        _record_operator_local_fallback(
            source=LIFECYCLE_POLICY_CHANGES_TABLE,
            fallback_type="operator_portfolio_detail_policy_changes_load_failed",
            reason="Operator portfolio detail could not load lifecycle policy-change audit rows.",
            error=exc,
            metadata={"symbol": normalized_symbol, "limit": int(limit)},
        )
        return []
    return _json_ready(df.to_dict(orient="records")) if not df.empty else []


def build_portfolio_detail_payload(*, symbol: str, asof_date: str | None = None) -> dict[str, Any]:
    payload = load_operator_payload(asof_date=asof_date)
    rows = _filter_rows(_payload_rows_by_kind(payload, "portfolio"), symbol=symbol)
    out = _detail_payload("portfolio", rows, filters={"symbol": symbol, "asof_date": asof_date})
    policy_changes = load_lifecycle_policy_changes_for_symbol(symbol)
    out["policy_changes"] = policy_changes
    out["policy_change_count"] = len(policy_changes)
    return out





_EVENT_POLICY_COMPACT_OMIT_KEYS = {
    "checks_json",
    "operator_notes_json",
    "llm_review_json",
    "actionability_json",
    "raw_context_json",
    "recommendation_reason_json",
}










def build_symbol_trace_payload(symbol: str, *, limit: int = 100) -> dict[str, Any]:
    bounded_limit = _bounded_limit(limit, default=100, maximum=500)
    payload = load_symbol_trace(symbol, limit=bounded_limit)
    payload.setdefault("pagination", _trace_list_pagination(payload, limit=bounded_limit))
    return payload


def _jsonish(value: Any, *, source: str = "operator_api_jsonish") -> Any:
    if value is None:
        return {}
    if isinstance(value, (dict, list)):
        return value
    try:
        if pd.isna(value):
            return {}
    except Exception as exc:
        _record_operator_local_fallback(
            source=source,
            fallback_type="operator_api_jsonish_missing_check_failed",
            reason="Operator API could not evaluate whether a JSON-like value is missing; it will attempt JSON parsing next.",
            error=exc,
            metadata={"value_type": type(value).__name__, "value_excerpt": redact_text(str(value)[:240])},
        )
    try:
        return json.loads(str(value))
    except Exception as exc:
        _record_operator_local_fallback(
            source=source,
            fallback_type="operator_api_jsonish_parse_failed",
            reason="Operator API could not parse a stored JSON-like value and used the empty-object fallback.",
            error=exc,
            metadata={"value_type": type(value).__name__, "value_excerpt": redact_text(str(value)[:240])},
        )
        return {}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception as exc:
        _record_operator_local_fallback(
            source="operator_api_text",
            fallback_type="operator_api_text_missing_check_failed",
            reason="Operator API could not evaluate missingness while normalizing text and kept string conversion fallback.",
            error=exc,
            metadata={"value_type": type(value).__name__},
        )
    text = str(value).strip()
    return text or None


def _ts(value: Any) -> str | None:
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.isoformat()


def _boolish(value: Any) -> bool | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception as exc:
        _record_operator_local_fallback(
            source="operator_api_boolish",
            fallback_type="operator_api_boolish_missing_check_failed",
            reason="Operator API could not evaluate missingness while normalizing a boolean value and kept boolean conversion fallback.",
            error=exc,
            metadata={"value_type": type(value).__name__},
        )
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "t", "1", "yes"}:
            return True
        if normalized in {"false", "f", "0", "no"}:
            return False
    return bool(value)


def _compact_payload(payload: Any, keys: list[str]) -> dict[str, Any]:
    parsed = _jsonish(payload)
    if not isinstance(parsed, dict):
        return {}
    return {key: parsed.get(key) for key in keys if parsed.get(key) is not None}


def _domain_for_stage(stage: Any) -> str:
    text = str(stage or "").strip().lower()
    if "event_evaluation" in text or "event_tensor" in text:
        return "event"
    if "adversarial" in text or "review" in text:
        return "review"
    if "playbook" in text or "hypothesis" in text:
        return "playbook"
    if "technical" in text:
        return "technical"
    if "lifecycle" in text:
        return "lifecycle"
    if "rebalance" in text:
        return "lifecycle"
    if "risk" in text or "sizing" in text:
        return "risk"
    if "portfolio" in text or "allocation" in text:
        return "portfolio"
    if "macro" in text or "regime" in text:
        return "macro"
    if "exchange" in text or "insider" in text or "short_selling" in text:
        return "exchange"
    if "execution" in text:
        return "execution"
    if "action_consolidation" in text or "action" in text:
        return "action"
    if "ocr" in text or "summary" in text or "categor" in text or "parse" in text or "download" in text:
        return "processing"
    return "generic"


def _domain_payload(stage: Any, payload: Any) -> dict[str, Any]:
    parsed = _jsonish(payload)
    if not isinstance(parsed, dict):
        return {}
    domain = _domain_for_stage(stage)
    if domain == "event":
        event_tensor = parsed.get("event_tensor") if isinstance(parsed.get("event_tensor"), dict) else parsed
        return {
            key: event_tensor.get(key)
            for key in [
                "event_class",
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
                "verdict",
                "what_happened",
                "rationale",
            ]
            if event_tensor.get(key) is not None
        }
    if domain == "review":
        return {
            key: parsed.get(key)
            for key in ["review_action", "review_score", "veto", "review_reason", "flags"]
            if parsed.get(key) is not None
        }
    if domain == "playbook":
        return {
            key: parsed.get(key)
            for key in [
                "playbook_id",
                "source_key",
                "source_action",
                "mapped_action",
                "production_allowed",
                "execution_mode",
                "confidence",
                "operator_summary",
                "review_boundary",
                "manual_revision_summary",
                "manual_revision_pointers",
            ]
            if parsed.get(key) is not None
        }
    if domain == "technical":
        return {
            key: parsed.get(key)
            for key in [
                "technical_state",
                "technical_score",
                "technical_total_score",
                "technical_trend_score",
                "technical_structure_score",
                "technical_participation_score",
                "technical_relative_strength_score",
                "technical_tradability_score",
                "technical_trigger_type",
                "technical_trigger_note",
                "pivot_price",
                "support_price",
                "stop_price",
                "invalidation_price",
            ]
            if parsed.get(key) is not None
        }
    if domain == "lifecycle":
        return {
            key: parsed.get(key)
            for key in [
                "position_status",
                "next_action",
                "suggested_action",
                "pnl_pct",
                "days_held",
                "bucket_status_note",
                "execution_mode",
                "action_fraction",
                "reference_price",
                "recommended_stop_price",
                "recommended_target_price",
                "stop_price",
                "invalidation_price",
                "expected_horizon_days",
                "active_exit_condition",
                "exit_condition_status",
            ]
            if parsed.get(key) is not None
        }
    if domain == "risk":
        return {
            key: parsed.get(key)
            for key in [
                "allocation_status",
                "risk_bucket",
                "conviction_bucket",
                "suggested_allocation_inr",
                "allocation_pct_of_adv20d",
                "stop_price",
                "invalidation_price",
                "invalidation_rule",
                "confidence",
                "score_impact",
                "review_action",
                "review_score",
            ]
            if parsed.get(key) is not None
        }
    if domain == "portfolio":
        return {
            key: parsed.get(key)
            for key in [
                "portfolio_status",
                "portfolio_reason",
                "plan_rank",
                "priority_score",
                "invest_score_pct",
                "requested_allocation_inr",
                "approved_allocation_inr",
                "remaining_capital_after_inr",
                "overlap_group",
                "overlap_reason",
                "thesis_bucket",
                "bucket_reason",
                "expected_horizon_days",
                "target_price",
                "stop_price",
                "invalidation_price",
            ]
            if parsed.get(key) is not None
        }
    if domain == "macro":
        return {
            key: parsed.get(key)
            for key in ["macro_asof_date", "macro_stress_score", "macro_risk_state", "macro_sizing_multiplier"]
            if parsed.get(key) is not None
        }
    if domain == "exchange":
        return {
            key: parsed.get(key)
            for key in [
                "exchange_asof_date",
                "deal_net_value_20d",
                "deal_cluster_count_20d",
                "insider_net_value_90d",
                "insider_event_count_90d",
                "short_selling_quantity_20d",
                "short_selling_event_count_20d",
                "upcoming_earnings_14d",
                "days_to_earnings",
                "corporate_action_count_30d",
                "exchange_accumulation_score",
                "exchange_distribution_score",
                "exchange_event_score",
            ]
            if parsed.get(key) is not None
        }
    if domain == "action":
        return {
            key: parsed.get(key)
            for key in [
                "winner_action",
                "winner_source",
                "action_source",
                "action_priority",
                "candidate_count",
                "conflict_count",
                "execution_mode",
                "transaction_type",
                "action_fraction",
                "reason_contract_status",
                "recommendation_reason",
                "manual_revision_summary",
                "manual_revision_pointers",
                "manual_revision_status",
            ]
            if parsed.get(key) is not None
        }
    if domain == "execution":
        return {
            key: parsed.get(key)
            for key in [
                "execution_status",
                "execution_reason",
                "transaction_type",
                "quantity",
                "filled_quantity",
                "estimated_order_value_inr",
                "order_value_inr",
                "reference_price",
                "reference_price_source",
                "reference_price_asof",
                "product_type",
                "order_type",
                "validity",
                "execution_mode",
                "security_id",
                "exchange_segment",
                "broker_order_id",
                "exchange_order_id",
                "broker_order_status",
                "submitted_at",
                "broker_update_time",
                "live_mode",
                "safety_checks",
                "raw_broker_action",
                "raw_broker_source",
                "raw_broker_execution_mode",
            ]
            if parsed.get(key) is not None
        }
    return _compact_payload(
        parsed,
        ["status", "source_type", "event_status", "parse_status", "summary", "error", "model", "prompt_version"],
    )


def normalize_trace_payload(raw: dict[str, Any]) -> dict[str, Any]:
    processing_rows = list(raw.get("processing") or [])
    trace_rows = list(raw.get("traces") or [])
    step_rows = list(raw.get("steps") or [])
    conflict_rows = list(raw.get("action_conflicts") or [])
    manual_wait_rows = list(raw.get("manual_review_wait_signal_links") or [])

    processing = []
    for row in processing_rows:
        payload = _domain_payload(row.get("stage"), row.get("payload_json"))
        processing.append(
            {
                "domain": _domain_for_stage(row.get("stage")),
                "stage": _text(row.get("stage")) or "processing",
                "status": _text(row.get("status")) or "unknown",
                "symbol": _text(row.get("symbol")),
                "source_type": _text(row.get("source_type")),
                "started_at": _ts(row.get("started_at")),
                "completed_at": _ts(row.get("completed_at")),
                "error": _text(row.get("error")),
                "input_hash": _text(row.get("input_hash")),
                "output_hash": _text(row.get("output_hash")),
                "payload": payload,
            }
        )

    steps_by_trace: dict[str, list[dict[str, Any]]] = {}
    for row in step_rows:
        trace_id = _text(row.get("trace_id"))
        if not trace_id:
            continue
        payload = _domain_payload(row.get("stage"), row.get("payload_json"))
        steps_by_trace.setdefault(trace_id, []).append(
            {
                "domain": _domain_for_stage(row.get("stage")),
                "step_idx": row.get("step_idx"),
                "stage": _text(row.get("stage")) or "stage",
                "status": _text(row.get("status")) or "unknown",
                "reason": _text(row.get("reason")),
                "started_at": _ts(row.get("started_at")),
                "completed_at": _ts(row.get("completed_at")),
                "input_hash": _text(row.get("input_hash")),
                "output_hash": _text(row.get("output_hash")),
                "payload": payload,
            }
        )

    decisions = []
    for row in trace_rows:
        trace_id = _text(row.get("trace_id")) or ""
        payload = _domain_payload(row.get("trigger_type"), row.get("payload_json"))
        decisions.append(
            {
                "domain": _domain_for_stage(row.get("trigger_type")),
                "trace_id": trace_id,
                "asof_date": _ts(row.get("asof_date")),
                "updated_at": _ts(row.get("updated_at")),
                "symbol": _text(row.get("symbol")),
                "unique_id": _text(row.get("unique_id")),
                "setup_id": _text(row.get("setup_id")),
                "trigger_type": _text(row.get("trigger_type")) or "decision",
                "previous_action": _text(row.get("previous_action")),
                "new_action": _text(row.get("new_action")),
                "action_changed": _boolish(row.get("action_changed")),
                "final_action": _text(row.get("final_action")),
                "final_reason": _text(row.get("final_reason")),
                "source_table": _text(row.get("source_table")),
                "source_key": _text(row.get("source_key")),
                "payload": payload,
                "steps": steps_by_trace.get(trace_id, []),
            }
        )

    conflicts = []
    for row in conflict_rows:
        conflicts.append(
            {
                "asof_date": _ts(row.get("asof_date")),
                "symbol": _text(row.get("symbol")),
                "winning_action_code": _text(row.get("winning_action_code")),
                "losing_action_code": _text(row.get("losing_action_code")),
                "winning_source": _text(row.get("winning_source")),
                "losing_source": _text(row.get("losing_source")),
                "losing_setup_id": _text(row.get("losing_setup_id")),
                "losing_unique_id": _text(row.get("losing_unique_id")),
                "lost_reason": _text(row.get("lost_reason")),
                "resolution_status": _text(row.get("resolution_status")),
                "resolution_rule_id": _text(row.get("resolution_rule_id")),
                "resolution_action": _text(row.get("resolution_action")),
                "resolution_reason": _text(row.get("resolution_reason")),
                "requires_manual_resolution": _boolish(row.get("requires_manual_resolution")),
            }
        )

    manual_wait_links = []
    for row in manual_wait_rows:
        manual_wait_links.append(
            {
                "decided_at": _ts(row.get("decided_at")),
                "manual_review_item_id": _text(row.get("manual_review_item_id")),
                "manual_review_item_type": _text(row.get("manual_review_item_type")),
                "manual_review_source_table": _text(row.get("manual_review_source_table")),
                "manual_review_source_key": _text(row.get("manual_review_source_key")),
                "symbol": _text(row.get("manual_review_symbol")) or _text(row.get("symbol")),
                "unique_id": _text(row.get("manual_review_unique_id")),
                "setup_id": _text(row.get("manual_review_setup_id")),
                "decision": _text(row.get("decision")),
                "rationale": _text(row.get("rationale")),
                "follow_up_event": _text(row.get("follow_up_event")),
                "operator_id": _text(row.get("operator_id")),
                "wait_signal_created_at": _ts(row.get("wait_signal_created_at")),
                "signal_id": _text(row.get("signal_id")),
                "wait_signal_status": _text(row.get("wait_signal_status")),
                "signal_type": _text(row.get("signal_type")),
                "expected_action": _text(row.get("expected_action")),
                "operator_summary": _text(row.get("operator_summary")),
                "wait_question": _text(row.get("wait_question")),
                "condition": _jsonish(row.get("condition_json")),
                "valid_until": _ts(row.get("valid_until")),
                "generated_by": _text(row.get("generated_by")),
                "matched_at": _ts(row.get("matched_at")),
                "match_status": _text(row.get("match_status")),
                "match_score": row.get("match_score"),
                "match_source_table": _text(row.get("match_source_table")),
                "match_source_key": _text(row.get("match_source_key")),
                "observed_at": _ts(row.get("observed_at")),
                "match_reason": _text(row.get("match_reason")),
                "evidence": _jsonish(row.get("evidence_json")),
            }
        )

    out = {
        "symbol": raw.get("symbol"),
        "unique_id": raw.get("unique_id"),
        "processing": processing,
        "decisions": decisions,
        "action_conflicts": conflicts,
        "manual_review_wait_signal_links": manual_wait_links,
        "raw_counts": {
            "processing": len(processing_rows),
            "traces": len(trace_rows),
            "steps": len(step_rows),
            "action_conflicts": len(conflict_rows),
            "manual_review_wait_signal_links": len(manual_wait_rows),
        },
    }
    out["pagination"] = _trace_summary_pagination(out)
    return out


def _trace_list_pagination(payload: dict[str, Any], *, limit: int | None = None) -> dict[str, Any]:
    processing = list(payload.get("processing") or [])
    traces = list(payload.get("traces") or [])
    steps = list(payload.get("steps") or [])
    conflicts = list(payload.get("action_conflicts") or [])
    row_limit = _bounded_limit(limit, default=len(traces) or 100, maximum=500) if limit is not None else len(traces)
    return {
        "primary": "traces",
        "processing": {"returned_count": len(processing), "total_count": len(processing), "limit": len(processing), "offset": 0, "has_more": False, "next_offset": None},
        "traces": {"returned_count": len(traces), "total_count": len(traces), "limit": row_limit, "offset": 0, "has_more": len(traces) >= row_limit if row_limit else False, "next_offset": row_limit if row_limit and len(traces) >= row_limit else None},
        "steps": {"returned_count": len(steps), "total_count": len(steps), "limit": len(steps), "offset": 0, "has_more": False, "next_offset": None},
        "action_conflicts": {"returned_count": len(conflicts), "total_count": len(conflicts), "limit": len(conflicts), "offset": 0, "has_more": False, "next_offset": None},
        "bounded": limit is not None,
    }


def _count_value(value: Any, fallback: int = 0, *, source: str = "operator_api_count") -> int:
    try:
        return int(value)
    except Exception as exc:
        _record_operator_local_fallback(
            source=source,
            fallback_type="operator_api_count_value_parse_failed",
            reason="Operator API could not parse a count value and used the supplied fallback count.",
            error=exc,
            metadata={"fallback": int(fallback), "value_type": type(value).__name__, "value_excerpt": redact_text(str(value)[:240])},
        )
        return int(fallback)


def _trace_summary_pagination(payload: dict[str, Any], *, limit: int | None = None) -> dict[str, Any]:
    raw_counts = payload.get("raw_counts") if isinstance(payload.get("raw_counts"), dict) else {}
    decisions = list(payload.get("decisions") or [])
    processing = list(payload.get("processing") or [])
    conflicts = list(payload.get("action_conflicts") or [])
    manual_waits = list(payload.get("manual_review_wait_signal_links") or [])
    decision_limit = _bounded_limit(limit, default=len(decisions) or TRACE_SUMMARY_DEFAULT_LIMIT, maximum=500) if limit is not None else len(decisions)
    processing_total = _count_value(raw_counts.get("processing"), len(processing))
    decisions_total = _count_value(raw_counts.get("traces"), len(decisions))
    conflicts_total = _count_value(raw_counts.get("action_conflicts"), len(conflicts))
    manual_waits_total = _count_value(raw_counts.get("manual_review_wait_signal_links"), len(manual_waits))
    return {
        "primary": "decisions",
        "processing": {"returned_count": len(processing), "total_count": processing_total, "limit": len(processing), "offset": 0, "has_more": False, "next_offset": None},
        "decisions": {"returned_count": len(decisions), "total_count": decisions_total, "limit": decision_limit, "offset": 0, "has_more": bool(decision_limit and decisions_total > len(decisions)), "next_offset": len(decisions) if decision_limit and decisions_total > len(decisions) else None},
        "action_conflicts": {"returned_count": len(conflicts), "total_count": conflicts_total, "limit": len(conflicts), "offset": 0, "has_more": False, "next_offset": None},
        "manual_review_wait_signal_links": {"returned_count": len(manual_waits), "total_count": manual_waits_total, "limit": len(manual_waits), "offset": 0, "has_more": False, "next_offset": None},
        "bounded": limit is not None,
    }


def _with_trace_summary_pagination(payload: dict[str, Any], *, limit: int | None = None) -> dict[str, Any]:
    out = dict(payload)
    out.setdefault("pagination", _trace_summary_pagination(out, limit=limit))
    return out




def build_symbol_trace_summary_payload(symbol: str, *, limit: int = 100) -> dict[str, Any]:
    bounded_limit = _bounded_limit(limit, default=100, maximum=500)
    if OPERATOR_API_TRACE_SUMMARY_CACHE_ENABLED:
        cached = load_materialized_trace_summary("symbol", symbol, limit=bounded_limit)
        if cached is not None:
            return _with_trace_summary_pagination(cached, limit=bounded_limit)
        record_operator_api_marker(
            route="/api/symbols/{symbol}/trace/summary",
            operation="build_symbol_trace_summary_payload",
            message="trace_summary_cache_miss:symbol",
            context={"symbol": symbol, "limit": bounded_limit},
        )
    summary = normalize_trace_payload(load_symbol_trace(symbol, limit=bounded_limit))
    summary["_trace_summary_cache"] = {"source": "live_fallback", "entity_type": "symbol", "entity_key": str(symbol).upper(), "limit_rows": bounded_limit}
    return _with_trace_summary_pagination(summary, limit=bounded_limit)


def build_data_health_payload(*, asof_date: str | None = None) -> dict[str, Any]:
    payload = load_operator_payload(asof_date=asof_date)
    summary = payload.get("summary") or {}
    return {
        "generated_at": payload.get("generated_at"),
        "api_schema": _operator_api_schema("/api/data-health", schema_name="data_health"),
        "asof_date": payload.get("asof_date"),
        "snapshot": _snapshot_payload(payload),
        "snapshot_warning": _snapshot_warning_payload(payload),
        "summary": {
            "alert_count": summary.get("alert_count"),
            "action_count": summary.get("action_count"),
            "ts_eval_summary_count": summary.get("ts_eval_summary_count"),
        },
        "sync_state": payload.get("sync_state") or [],
        "runtime_processes": payload.get("runtime_processes") or [],
        "cron_status": payload.get("cron_status") or [],
    }


def _compact_health_value(value: Any, *, list_limit: int, string_chars: int, stats: dict[str, int]) -> Any:
    if isinstance(value, str):
        if len(value) > string_chars:
            stats["truncated_strings"] = int(stats.get("truncated_strings") or 0) + 1
            return value[:string_chars] + "...[truncated]"
        return value
    if isinstance(value, list):
        compacted = [_compact_health_value(item, list_limit=list_limit, string_chars=string_chars, stats=stats) for item in value[:list_limit]]
        omitted = max(0, len(value) - list_limit)
        if omitted:
            stats["truncated_lists"] = int(stats.get("truncated_lists") or 0) + 1
            stats["omitted_list_items"] = int(stats.get("omitted_list_items") or 0) + omitted
        return compacted
    if isinstance(value, dict):
        return {str(key): _compact_health_value(val, list_limit=list_limit, string_chars=string_chars, stats=stats) for key, val in value.items()}
    return value


def _compact_operator_health_payload(payload: dict[str, Any], *, compact: bool) -> dict[str, Any]:
    if not compact:
        out = dict(payload)
        out["compact"] = False
        return out
    stats = {"truncated_lists": 0, "omitted_list_items": 0, "truncated_strings": 0}
    out = _compact_health_value(
        payload,
        list_limit=max(1, int(OPERATOR_HEALTH_COMPACT_LIST_LIMIT)),
        string_chars=max(200, int(OPERATOR_HEALTH_COMPACT_STRING_CHARS)),
        stats=stats,
    )
    if not isinstance(out, dict):
        return payload
    out["compact"] = True
    out["compact_meta"] = {
        "list_limit": max(1, int(OPERATOR_HEALTH_COMPACT_LIST_LIMIT)),
        "string_chars": max(200, int(OPERATOR_HEALTH_COMPACT_STRING_CHARS)),
        **stats,
        "full_payload_hint": "/api/health/details?mode=full&compact=false",
    }
    return out


def build_operator_health_payload(*, mode: str = "fast", compact: bool = True) -> dict[str, Any]:
    normalized_mode = "full" if str(mode or "").strip().lower() == "full" else "fast"
    cache_key = ("operator_health_payload", normalized_mode, bool(compact), str(id(build_operator_health)))
    now = time.monotonic()
    cached = _PAYLOAD_CACHE.get(cache_key)
    if cached and OPERATOR_API_PAYLOAD_CACHE_SECONDS > 0 and (now - cached[0]) <= OPERATOR_API_PAYLOAD_CACHE_SECONDS:
        return cached[1]
    payload = build_operator_health(detail_level=normalized_mode)
    if isinstance(payload, dict):
        out = {
            **payload,
            "api_schema": _operator_api_schema("/api/health/details", schema_name="operator_health_details"),
        }
        out = _compact_operator_health_payload(out, compact=bool(compact))
        _PAYLOAD_CACHE[cache_key] = (now, out)
        return out
    return payload


def build_operations_smoke_payload() -> dict[str, Any]:
    payload = build_operator_smoke(include_dhan=False)
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema("/api/operations/smoke", schema_name="operations_smoke"),
        "status": payload.get("status"),
        "operator_smoke": payload,
        "fix_hints": payload.get("fix_hints") or [],
        "trust_level": payload.get("trust_level"),
        "trust_status": payload.get("trust_status"),
        "recommendation": payload.get("recommendation"),
        "next_commands": payload.get("next_commands") or [],
        "read_only": True,
        "note": "This endpoint runs the compact read-only operator smoke checks. It does not start ingestion, advisory, broker, or trading jobs.",
    }


def build_ingestion_state_payload(
    *,
    source: str | None = None,
    status: str | None = None,
    limit: int = 1000,
    sample_limit: int = 20,
) -> dict[str, Any]:
    normalized_source = str(source).strip() if source else None
    normalized_status = str(status).strip() if status else None
    bounded_limit = max(1, min(int(limit), 5000))
    bounded_sample_limit = max(0, min(int(sample_limit), 100))
    rows = get_ingestion_state_entries(
        source_prefix=normalized_source or None,
        status=normalized_status or None,
        limit=bounded_limit,
    )
    summary = summarize_ingestion_state_entries(
        rows,
        sample_limit=bounded_sample_limit,
        active_failure_days=OPERATOR_API_INGESTION_FAILURE_ACTIVE_DAYS,
    )
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema("/api/operations/ingestion-state", schema_name="operations_ingestion_state"),
        "status": "ok",
        "filters": {
            "source": normalized_source,
            "status": normalized_status,
            "limit": bounded_limit,
            "sample_limit": bounded_sample_limit,
        },
        "summary": summary,
        "operator_boundary": {
            "read_only": True,
            "mutates_state": False,
            "active_failure_days": OPERATOR_API_INGESTION_FAILURE_ACTIVE_DAYS,
            "stale_historical_failures_block_trust": False,
            "clear_command": "python scripts/ingestion_state_runner.py clear --source <source> --key <object_key>",
            "note": "This endpoint summarizes file-level ingestion state only. It does not clear failed rows or retry ingestion. Failed files outside the active window are visible as stale historical failures and should not pollute current Manual Review/Health blockers unless the operator decides that historical file still matters.",
        },
    }


def _tail_file(path: Path, *, line_count: int, max_bytes: int = 200_000) -> list[str]:
    if not path.exists() or not path.is_file():
        return []
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > max_bytes:
            handle.seek(max(0, size - max_bytes))
        raw = handle.read()
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return lines[-max(0, int(line_count)) :]


def _latest_script_marker(lines: list[str]) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    for line in lines:
        if "[stockey.script]" not in line:
            continue
        parts: dict[str, str] = {}
        for token in line.split():
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            parts[key] = value
        if parts.get("status"):
            latest = parts
    return latest


def build_cron_logs_payload(*, limit: int = 20, lines: int = 80, offset: int = 0) -> dict[str, Any]:
    log_dir = Path(CRON_LOG_DIR)
    if not log_dir.exists():
        return {
            "generated_at": pd.Timestamp.utcnow().isoformat(),
            "api_schema": _operator_api_schema("/api/operations/cron-logs", schema_name="operations_cron_logs"),
            "status": "missing_log_dir",
            "log_dir": str(log_dir),
            "logs": [],
            "pagination": {"logs": _bounded_list_contract([], limit=limit, offset=offset, default=20, maximum=100, total_count=0)},
        }
    all_files = sorted(
        [path for path in log_dir.glob("*.log") if path.is_file()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    files, file_meta = _page_any_rows(all_files, limit=limit, offset=offset, default=20, maximum=100)
    logs: list[dict[str, Any]] = []
    for path in files:
        stat = path.stat()
        tail = [redact_text(line) or "" for line in _tail_file(path, line_count=int(lines))]
        lower_tail = "\n".join(tail).lower()
        latest_marker = _latest_script_marker(tail)
        if latest_marker.get("status") == "failed" or "traceback" in lower_tail or "error" in lower_tail:
            status = "error"
        elif latest_marker.get("status") == "interrupted":
            status = "warning"
        else:
            status = "ok"
        logs.append(
            {
                "name": path.name,
                "path": str(path),
                "status": status,
                "bytes": int(stat.st_size),
                "modified_at": pd.Timestamp(stat.st_mtime, unit="s", tz="UTC").isoformat(),
                "latest_marker": latest_marker,
                "tail": tail,
            }
        )
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema("/api/operations/cron-logs", schema_name="operations_cron_logs"),
        "status": "ok",
        "log_dir": str(log_dir),
        "logs": logs,
        "pagination": {"logs": file_meta},
    }


def build_cron_status_payload(*, limit: int = 50, lines: int = 12, offset: int = 0) -> dict[str, Any]:
    payload = build_cron_status(
        crontab_path=REPO_ROOT / "config" / "stockey.generated.crontab",
        log_dir=CRON_LOG_DIR,
        tail_lines=max(0, min(int(lines), 80)),
    )
    jobs = payload.get("jobs") if isinstance(payload.get("jobs"), list) else []
    page, meta = _page_any_rows(jobs, limit=limit, offset=offset, default=50, maximum=200)
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema("/api/operations/cron-status", schema_name="operations_cron_status"),
        "status": str(payload.get("status") or "ok"),
        "crontab_path": payload.get("crontab_path"),
        "log_dir": payload.get("log_dir"),
        "counts": payload.get("counts") if isinstance(payload.get("counts"), dict) else {},
        "jobs": page,
        "pagination": {"jobs": meta},
    }


def build_event_model_promotion_check_payload() -> dict[str, Any]:
    args = argparse.Namespace(
        artifact_dir=".cache/advisory_event_meta_model",
        model_basename="event_meta_model",
        horizon_days=10,
        return_threshold=0.02,
        log_path=str(CRON_LOG_DIR / "all_ml.log"),
        run_window_days=35,
        max_score_age_days=14,
        min_successful_runs=3,
        min_train_rows=80,
        min_test_rows=20,
        min_labeled_rows=100,
        min_dates=20,
        min_symbols=25,
        min_event_classes=4,
        min_score_rows=10,
        min_precision=0.55,
        min_precision_lift=0.10,
        min_roc_auc=0.55,
    )
    payload = build_promotion_check(args)
    payload["generated_at"] = pd.Timestamp.utcnow().isoformat()
    payload["api_schema"] = _operator_api_schema(
        "/api/research/event-model-promotion-check",
        schema_name="event_model_promotion_check",
    )
    return payload






def build_recommendation_diagnostics_payload(*, asof_date: str | None = None, limit: int = 500) -> dict[str, Any]:
    parsed_asof = _parse_asof_date(asof_date) if asof_date else None
    payload = build_recommendation_diagnostics(
        asof_date=parsed_asof,
        limit=max(1, min(int(limit), 2_000)),
    )
    payload["generated_at"] = pd.Timestamp.utcnow().isoformat()
    payload["api_schema"] = _operator_api_schema(
        "/api/research/recommendation-diagnostics",
        schema_name="recommendation_diagnostics",
    )
    payload["operator_boundary"] = {
        "read_only": True,
        "portfolio_authority": "none",
        "broker_execution_allowed": False,
        "decision_use": (
            "diagnose missing BUY/BUY_MORE rows, stale artifacts, context-overlay conversion, "
            "technical confirmation, and downstream consolidation before tuning regime or policy"
        ),
    }
    return payload


def build_event_model_artifacts_payload(*, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    artifact_dir = Path(".cache/advisory_event_meta_model")
    try:
        manifest = build_artifact_manifest(
            artifact_dir=artifact_dir,
            model_basename="event_meta_model",
            s3_prefix=env.str("EVENT_MODEL_ARTIFACT_S3_PREFIX", "models/advisory_event_meta_model"),
        )
        manifest["status"] = "ok"
    except FileNotFoundError as exc:
        _record_operator_local_fallback(
            source="event_model_artifacts",
            fallback_type="operator_api_event_model_artifact_manifest_missing",
            reason="event model artifact manifest is not available",
            error=exc,
            severity="warn",
            metadata={"artifact_dir": str(artifact_dir), "model_basename": "event_meta_model"},
        )
        manifest = {
            "status": "missing_artifact",
            "error": str(exc),
            "artifact_dir": str(artifact_dir),
            "files": [],
        }
    manifest = dict(manifest)
    files = [item for item in manifest.get("files") or [] if isinstance(item, dict)]
    file_page, file_meta = _page_any_rows(files, limit=limit, offset=offset, default=50, maximum=200)
    manifest["files"] = file_page
    latest_heads: list[dict[str, Any]] = []
    if manifest.get("latest_prefix"):
        try:
            from utils.store import AWS_BUCKET_NAME, _get_client

            s3 = _get_client()
            for item in file_page:
                key = item.get("latest_key")
                if not key:
                    continue
                try:
                    head = s3.head_object(Bucket=AWS_BUCKET_NAME, Key=key)
                    latest_heads.append(
                        {
                            "key": key,
                            "status": "ok",
                            "content_length": head.get("ContentLength"),
                            "last_modified": _ts(head.get("LastModified")),
                            "etag": head.get("ETag"),
                        }
                    )
                except Exception as exc:
                    _record_operator_local_fallback(
                        source="event_model_artifacts",
                        fallback_type="operator_api_event_model_artifact_head_failed",
                        reason="event model artifact latest S3 object could not be inspected",
                        error=exc,
                        severity="warn",
                        metadata={"bucket": AWS_BUCKET_NAME, "key": str(key)},
                    )
                    latest_heads.append({"key": key, "status": "missing_or_error", "error": f"{type(exc).__name__}: {exc}"})
        except Exception as exc:
            _record_operator_local_fallback(
                source="event_model_artifacts",
                fallback_type="operator_api_event_model_artifact_s3_unavailable",
                reason="event model artifact S3 client or store configuration is unavailable",
                error=exc,
                severity="warn",
                metadata={"latest_prefix": str(manifest.get("latest_prefix") or "")},
            )
            latest_heads.append({"status": "s3_unavailable", "error": f"{type(exc).__name__}: {exc}"})
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema(
            "/api/research/event-model-artifacts",
            schema_name="event_model_artifacts",
        ),
        "status": manifest.get("status"),
        "artifact": manifest,
        "latest_s3_heads": latest_heads,
        "pagination": {
            "artifact_files": file_meta,
            "latest_s3_heads": _bounded_list_contract(latest_heads, limit=limit, offset=offset, default=50, maximum=200, total_count=file_meta["total_count"]),
        },
        "operator_boundary": {
            "authority_scope": "read_only_artifact_inspection",
            "system_applies_model": False,
            "policy_auto_promotion_allowed": False,
            "portfolio_mutation_allowed": False,
            "broker_execution_allowed": False,
            "s3_write_allowed": False,
            "local_file_write_allowed": False,
            "operator_action": "Use this page to verify local artifact hashes and S3 HEAD status only. Uploads still happen through the explicit training/artifact-store scripts.",
        },
        "read_only": True,
    }


def _research_ledger_operator_boundary() -> dict[str, Any]:
    return {
        "authority_scope": "read_only_research_audit",
        "system_starts_research_runs": False,
        "system_finishes_research_runs": False,
        "mutates_research_ledger": False,
        "policy_auto_promotion_allowed": False,
        "portfolio_mutation_allowed": False,
        "broker_execution_allowed": False,
        "operator_action": "Use this view to audit experiment provenance, validation protocol, results, and missing ledger evidence. Start/finish writes still happen only inside explicit research scripts.",
    }


def _research_ledger_row(row: dict[str, Any]) -> dict[str, Any]:
    out = _json_ready(row)
    for source_col, target_col in [
        ("config_json", "config"),
        ("validation_protocol_json", "validation_protocol"),
        ("data_snapshot_json", "data_snapshot"),
        ("result_metrics_json", "result_metrics"),
        ("notes_json", "notes"),
    ]:
        out[target_col] = _jsonish(out.get(source_col), source=f"research_ledger:{source_col}")
    return out


def build_research_ledger_payload(*, limit: int = 25, offset: int = 0) -> dict[str, Any]:
    endpoint = "/api/research/ledger"
    row_limit = _bounded_limit(limit, default=25, maximum=100)
    row_offset = _bounded_offset(offset)
    if not _table_exists(RESEARCH_LEDGER_TABLE):
        return {
            "generated_at": pd.Timestamp.utcnow().isoformat(),
            "api_schema": _operator_api_schema(endpoint, schema_name="research_ledger"),
            "status": "missing_table",
            "runs": [],
            "summary": {
                "total_count": 0,
                "status_counts": {},
                "run_type_counts": {},
                "operator_action": "Run a research script with --log-research-ledger to create audit rows.",
            },
            "pagination": {
                "runs": _bounded_list_contract([], limit=row_limit, offset=row_offset, default=25, maximum=100, total_count=0),
            },
            "operator_boundary": _research_ledger_operator_boundary(),
        }

    df = list_research_ledger_runs(limit=row_limit, offset=row_offset, include_details=True)
    rows = [_research_ledger_row(row) for row in df.to_dict(orient="records")] if not df.empty else []
    total_count = row_offset + len(rows)
    try:
        count_df = sql_to_df(f"SELECT COUNT(*) AS total_count FROM {RESEARCH_LEDGER_TABLE}", retries=2)
        if not count_df.empty:
            total_count = int(count_df["total_count"].iloc[0] or total_count)
    except Exception as exc:
        _record_operator_local_fallback(
            source=RESEARCH_LEDGER_TABLE,
            fallback_type="operator_api_research_ledger_count_failed",
            reason="Operator API could not count research ledger rows and used the current page size as pagination fallback.",
            error=exc,
            severity="warn",
            metadata={"limit": row_limit, "offset": row_offset},
        )

    status_counts: dict[str, int] = {}
    run_type_counts: dict[str, int] = {}
    missing_validation_count = 0
    failed_count = 0
    running_count = 0
    for row in rows:
        status = str(row.get("status") or "unknown")
        run_type = str(row.get("run_type") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        run_type_counts[run_type] = run_type_counts.get(run_type, 0) + 1
        if not row.get("validation_protocol"):
            missing_validation_count += 1
        if status.lower() in {"failed", "error"}:
            failed_count += 1
        if status.lower() == "running":
            running_count += 1

    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema(endpoint, schema_name="research_ledger"),
        "status": "ok" if rows else "empty",
        "runs": rows,
        "summary": {
            "total_count": total_count,
            "returned_count": len(rows),
            "status_counts": status_counts,
            "run_type_counts": run_type_counts,
            "missing_validation_protocol_count": missing_validation_count,
            "failed_count": failed_count,
            "running_count": running_count,
            "operator_action": "Review failed/running rows and rows missing validation protocol before trusting research promotion evidence.",
        },
        "pagination": {
            "runs": _bounded_list_contract(rows, limit=row_limit, offset=row_offset, default=25, maximum=100, total_count=total_count),
        },
        "operator_boundary": _research_ledger_operator_boundary(),
    }


def _python_cmd(*args: str) -> list[str]:
    return [sys.executable, *args]


OPERATOR_COMMAND_REGISTRY: dict[str, dict[str, Any]] = {
    "operator_smoke": {
        "label": "Operator Smoke Check",
        "description": "Single compact read-only preflight for API, DB, frontend, freshness, identity, signal-quality, cron logs, and trust gate.",
        "args": _python_cmd("-m", "advisory.operator_smoke"),
        "risk": "safe_read_only",
        "dry_run": True,
        "timeout_seconds": 180,
    },
    "operator_health_skip_dhan": {
        "label": "Operator Health",
        "description": "Read-only health check for DB, Redis, cron logs, snapshots, optional dependencies, and frontend dependencies without initiating Dhan login.",
        "args": _python_cmd("-m", "advisory.operator_health", "--skip-dhan"),
        "risk": "safe_read_only",
        "dry_run": True,
        "timeout_seconds": 180,
    },
    "operator_snapshot_refresh": {
        "label": "Refresh Operator Snapshot",
        "description": "Rebuilds the DB-backed operator frontend snapshot/cache so stale Action Queue, Portfolio, Events, and Health views can recover without rerunning full advisory. Does not mutate recommendations, portfolio rows, config, or broker orders.",
        "args": _python_cmd("-m", "advisory.operator_snapshot"),
        "risk": "cache_write",
        "dry_run": False,
        "timeout_seconds": 180,
    },
    "cron_preflight": {
        "label": "Cron Preflight",
        "description": "Read-only go-crond setup check for generated crontab, environment, referenced scripts, stale locks, Python resolution, and operator ports.",
        "args": _python_cmd("scripts/cron_preflight.py", "--format", "json"),
        "risk": "safe_read_only",
        "dry_run": True,
        "timeout_seconds": 60,
    },
    "advisory_stage_report": {
        "label": "Advisory Stage Report",
        "description": "Read-only report ranking the slowest stages from the latest all_advisory cron log JSON summary.",
        "args": _python_cmd("scripts/advisory_stage_report.py", "--log-path", "logs/cron/all_advisory.log", "--format", "json", "--limit", "20"),
        "risk": "safe_read_only",
        "dry_run": True,
        "timeout_seconds": 60,
    },
    "recommendation_diagnostics": {
        "label": "Recommendation Diagnostics",
        "description": "Read-only no-BUY and underparticipation diagnosis over persisted advisory/action rows. Does not mutate recommendations, portfolio rows, config, or broker orders.",
        "args": _python_cmd("-m", "advisory.recommendation_diagnostics", "--format", "text"),
        "risk": "safe_read_only",
        "dry_run": True,
        "timeout_seconds": 180,
    },
    "context_overlay_reliability_report": {
        "label": "Context Overlay Reliability",
        "description": "Read-only benchmark-attributed reliability report for announcement, exchange, bhavcopy, theme, and macro context-overlay watch/de-risk evidence. Does not change live policy or broker behavior.",
        "args": _python_cmd("-m", "advisory.context_overlay_reliability_report", "--horizons", "5", "10", "20", "--format", "text"),
        "risk": "safe_read_only",
        "dry_run": True,
        "timeout_seconds": 180,
    },
    "signal_quality_family_report": {
        "label": "Signal Quality Family Report",
        "description": "Read-only source-family outcome report comparing technical-only against technical plus context families after costs. Does not create promotion rows or live policy changes.",
        "args": _python_cmd("-m", "advisory.signal_quality_family_report", "--horizons", "5", "10", "20", "--format", "text"),
        "risk": "safe_read_only",
        "dry_run": True,
        "timeout_seconds": 180,
    },
    "run_research_evidence": {
        "label": "Run Research Evidence Refresh",
        "description": "Runs the daily research-only evidence refresh for context, event, adversarial-review, action-transition, causal-memory, provenance, and reliability evidence. It does not run event-model training, mutate live policy/config, change portfolio rows, or call the broker.",
        "args": ["./all_research_evidence.sh"],
        "risk": "research_evidence_write",
        "dry_run": False,
        "timeout_seconds": 1800,
    },
    "llm_provenance_audit": {
        "label": "LLM Provenance Audit",
        "description": "Read-only audit for persisted LLM/Codex-derived signal rows covering prompt id/version, response schema, model, evidence, and authority metadata. Does not repair rows or change policy.",
        "args": _python_cmd("-m", "advisory.llm_provenance_audit", "--lookback-days", "30", "--limit-per-table", "100", "--format", "text"),
        "risk": "safe_read_only",
        "dry_run": True,
        "timeout_seconds": 180,
    },
    "action_evidence_provenance_dry_run": {
        "label": "Action Evidence Provenance Dry Run",
        "description": "Read-only dry run that builds final-action to evidence-section provenance rows for audit review. Does not persist provenance, change recommendations, or call the broker.",
        "args": _python_cmd("-m", "advisory.action_evidence_provenance", "--dry-run", "--limit", "250", "--format", "text"),
        "risk": "safe_read_only",
        "dry_run": True,
        "timeout_seconds": 180,
    },
    "causal_event_provenance_dry_run": {
        "label": "Causal Event Provenance Dry Run",
        "description": "Read-only dry run that builds source-to-memory-to-outcome provenance for causal event memory review. Does not persist provenance, change policy, or call the broker.",
        "args": _python_cmd("-m", "advisory.causal_event_provenance", "--dry-run", "--limit", "250", "--format", "text"),
        "risk": "safe_read_only",
        "dry_run": True,
        "timeout_seconds": 180,
    },
    "advisory_preflight": {
        "label": "Advisory Preflight",
        "description": "Runs Dhan auth refresh/validation and compact operator smoke before starting the expensive all_advisory pipeline. Does not run advisory, rebuild portfolios, or submit broker orders.",
        "args": ["./all_advisory_preflight.sh"],
        "risk": "auth_cache_write",
        "dry_run": False,
        "timeout_seconds": 240,
    },
    "event_model_promotion_check": {
        "label": "Event Model Promotion Check",
        "description": "Read-only gate showing whether the event model has enough evidence for manual low-weight review.",
        "args": _python_cmd("-m", "advisory.event_model_promotion_check", "--format", "json"),
        "risk": "safe_read_only",
        "dry_run": True,
        "timeout_seconds": 180,
    },
    "event_model_artifact_manifest": {
        "label": "Event Model Artifact Manifest",
        "description": "Read-only local/S3 artifact manifest check for the latest trained event model files.",
        "args": _python_cmd("-m", "advisory.event_model_artifact_store", "--dry-run"),
        "risk": "safe_read_only",
        "dry_run": True,
        "timeout_seconds": 180,
    },
    "hypothesis_scan_dry_run": {
        "label": "Playbook Scan Dry Run",
        "description": "Runs investor playbook matching without persisting matches or action plans.",
        "args": _python_cmd("-m", "advisory.hypothesis_engine", "--run-scan", "--dry-run"),
        "risk": "dry_run",
        "dry_run": True,
        "timeout_seconds": 180,
    },
    "event_policy_evaluator_dry_run": {
        "label": "Event Policy Evaluator Dry Run",
        "description": "Evaluates event-policy outcome summaries without writing updated evaluation rows.",
        "args": _python_cmd("-m", "advisory.event_policy_evaluator", "--horizons", "5", "10", "20", "--dry-run"),
        "risk": "dry_run",
        "dry_run": True,
        "timeout_seconds": 180,
    },
    "trace_summary_rebuild": {
        "label": "Rebuild Trace Summaries",
        "description": "Materializes compact symbol/event trace summaries for faster operator trace pages. Does not touch broker/trading state.",
        "args": _python_cmd("-m", "advisory.trace_summary_store", "--symbol-limit", "150", "--event-limit", "150", "--trace-limit", "100", "--format", "json"),
        "risk": "cache_write",
        "dry_run": False,
        "timeout_seconds": 180,
    },
    "trace_summary_cleanup_dry_run": {
        "label": "Trace Cache Cleanup Dry Run",
        "description": "Reports old duplicate trace-summary cache rows that can be pruned. Does not delete unless run manually with cleanup outside this dry-run command.",
        "args": _python_cmd("-m", "advisory.trace_summary_store", "--cleanup", "--keep-latest-per-entity", "1", "--older-than-days", "14", "--dry-run", "--format", "json"),
        "risk": "dry_run",
        "dry_run": True,
        "timeout_seconds": 180,
    },
    "superseded_failure_cleanup_dry_run": {
        "label": "Superseded Failure Cleanup Dry Run",
        "description": "Audits recovered event-processing and announcement-document failures that can be marked superseded later. This command is preview-only and never writes cleanup markers.",
        "args": _python_cmd("-m", "advisory.superseded_failures", "--limit", "500"),
        "risk": "safe_read_only",
        "dry_run": True,
        "timeout_seconds": 180,
    },
}


def ensure_operator_command_runs_table() -> None:
    ensure_operator_api_audit_tables()


def _tail_text(value: str, *, max_chars: int | None = None) -> str:
    max_len = max(1, int(max_chars or OPERATOR_COMMAND_OUTPUT_TAIL_CHARS))
    text = value or ""
    return text[-max_len:]


def persist_operator_command_run(row: dict[str, Any]) -> None:
    ensure_operator_command_runs_table()
    frame = pd.DataFrame([row])
    upsert_to_db(frame, OPERATOR_COMMAND_RUNS_TABLE, unique_keys=["run_id"])


def build_operator_commands_payload(*, limit: int = 25) -> dict[str, Any]:
    ensure_operator_command_runs_table()
    runs = sql_to_df(
        f"""
        SELECT *
        FROM {OPERATOR_COMMAND_RUNS_TABLE}
        ORDER BY started_at DESC NULLS LAST
        LIMIT %(limit)s
        """,
        params={"limit": max(1, int(limit))},
        retries=3,
    )
    rows = _records(runs)
    for row in rows:
        row["command_args"] = _jsonish(row.get("command_args_json"))
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema("/api/operations/commands", schema_name="operations_commands"),
        "status": "ok",
        "commands": [
            {
                "key": key,
                "label": value["label"],
                "description": value["description"],
                "args": value["args"],
                "risk": value["risk"],
                "dry_run": value["dry_run"],
                "timeout_seconds": value["timeout_seconds"],
            }
            for key, value in OPERATOR_COMMAND_REGISTRY.items()
        ],
        "recent_runs": rows,
    }


def run_operator_command_payload(payload: dict[str, Any]) -> dict[str, Any]:
    command_key = str(payload.get("command_key") or "").strip()
    if command_key not in OPERATOR_COMMAND_REGISTRY:
        raise ValueError(f"Unknown command_key: {command_key}")
    command = OPERATOR_COMMAND_REGISTRY[command_key]
    require_confirm = bool(payload.get("confirm"))
    if not require_confirm:
        raise ValueError("confirm=true is required before running an operator command")
    timeout_seconds = min(max(int(command.get("timeout_seconds") or OPERATOR_COMMAND_TIMEOUT_SECONDS), 1), OPERATOR_COMMAND_TIMEOUT_SECONDS)
    run_id = f"{command_key}:{pd.Timestamp.utcnow().strftime('%Y%m%dT%H%M%S%fZ')}"
    started_at = pd.Timestamp.utcnow()
    status = "running"
    base_row = {
        "run_id": run_id,
        "command_key": command_key,
        "command_label": command["label"],
        "command_args_json": json.dumps(command["args"], ensure_ascii=False),
        "risk": command["risk"],
        "dry_run": bool(command["dry_run"]),
        "status": status,
        "returncode": None,
        "operator_id": _text(payload.get("operator_id")),
        "requested_reason": _text(payload.get("requested_reason")),
        "started_at": started_at,
        "completed_at": None,
        "elapsed_ms": None,
        "stdout_tail": None,
        "stderr_tail": None,
        "error": None,
        "load_ts": started_at,
    }
    persist_operator_command_run(base_row)
    try:
        completed = subprocess.run(
            list(command["args"]),
            cwd=REPO_ROOT,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        completed_at = pd.Timestamp.utcnow()
        elapsed_ms = (completed_at - started_at).total_seconds() * 1000.0
        status = "ok" if int(completed.returncode) == 0 else "failed"
        final_row = {
            **base_row,
            "status": status,
            "returncode": int(completed.returncode),
            "completed_at": completed_at,
            "elapsed_ms": elapsed_ms,
            "stdout_tail": _tail_text(completed.stdout),
            "stderr_tail": _tail_text(completed.stderr),
            "error": None if status == "ok" else f"command exited with {completed.returncode}",
            "load_ts": completed_at,
        }
    except subprocess.TimeoutExpired as exc:
        completed_at = pd.Timestamp.utcnow()
        elapsed_ms = (completed_at - started_at).total_seconds() * 1000.0
        _record_operator_local_fallback(
            source="operator_command",
            fallback_type="operator_command_timeout",
            reason="whitelisted operator command timed out before completion",
            error=exc,
            severity="warn",
            metadata={
                "command_key": command_key,
                "timeout_seconds": timeout_seconds,
                "risk": str(command.get("risk") or ""),
                "dry_run": bool(command.get("dry_run")),
            },
        )
        final_row = {
            **base_row,
            "status": "timeout",
            "returncode": None,
            "completed_at": completed_at,
            "elapsed_ms": elapsed_ms,
            "stdout_tail": _tail_text(exc.stdout if isinstance(exc.stdout, str) else ""),
            "stderr_tail": _tail_text(exc.stderr if isinstance(exc.stderr, str) else ""),
            "error": f"timeout after {timeout_seconds}s",
            "load_ts": completed_at,
        }
    persist_operator_command_run(final_row)
    result = dict(final_row)
    result["command_args"] = list(command["args"])
    result.pop("command_args_json", None)
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "status": result["status"],
        "run": _json_ready(result),
        "note": "Command run is whitelisted and audited. No broker/trading command is available from this endpoint.",
    }


def _superseded_cleanup_counts(result: dict[str, Any]) -> dict[str, Any]:
    event_processing = result.get("event_processing") if isinstance(result.get("event_processing"), dict) else {}
    announcement_documents = result.get("announcement_documents") if isinstance(result.get("announcement_documents"), dict) else {}
    return {
        "event_processing_candidates": int(event_processing.get("candidates") or 0),
        "event_processing_updated": int(event_processing.get("updated") or 0),
        "announcement_document_candidates": int(announcement_documents.get("candidates") or 0),
        "announcement_document_updated": int(announcement_documents.get("updated") or 0),
        "total_candidates": int(event_processing.get("candidates") or 0) + int(announcement_documents.get("candidates") or 0),
        "total_updated": int(event_processing.get("updated") or 0) + int(announcement_documents.get("updated") or 0),
    }


def _superseded_cleanup_boundary(*, apply: bool) -> dict[str, Any]:
    return {
        "mode": "apply" if apply else "preview",
        "writes_superseded_metadata": bool(apply),
        "mutates_portfolio": False,
        "mutates_action_recommendation": False,
        "mutates_config": False,
        "submits_order": False,
        "broker_execution_enabled": False,
        "requires_confirm": bool(apply),
        "requires_requested_reason": bool(apply),
        "description": (
            "Marks recovered event-processing/document failure rows as superseded so they stop polluting operator review."
            if apply
            else "Previews recovered event-processing/document failure rows that are safe candidates for superseded metadata."
        ),
    }


def _persist_superseded_cleanup_audit(
    *,
    run_id: str,
    status: str,
    started_at: pd.Timestamp,
    completed_at: pd.Timestamp,
    operator_id: str,
    requested_reason: str,
    limit: int,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    elapsed_ms = (completed_at - started_at).total_seconds() * 1000.0
    row = {
        "run_id": run_id,
        "command_key": "superseded_failure_cleanup_apply",
        "command_label": "Apply Superseded Failure Cleanup",
        "command_args_json": json.dumps(["internal", "advisory.superseded_failures", "--apply", "--limit", str(limit)], ensure_ascii=False),
        "risk": "metadata_cleanup",
        "dry_run": False,
        "status": status,
        "returncode": 0 if status == "ok" else 1,
        "operator_id": operator_id,
        "requested_reason": requested_reason,
        "started_at": started_at,
        "completed_at": completed_at,
        "elapsed_ms": elapsed_ms,
        "stdout_tail": _tail_text(json.dumps(_json_ready(result or {}), ensure_ascii=False, default=str)),
        "stderr_tail": None,
        "error": error,
        "load_ts": completed_at,
    }
    persist_operator_command_run(row)
    output = dict(row)
    output["command_args"] = _jsonish(output.pop("command_args_json", None))
    return output


def build_superseded_cleanup_payload(*, limit: int = 500) -> dict[str, Any]:
    bounded_limit = max(1, min(int(limit), 1000))
    result = cleanup_superseded_failures(apply=False, limit=bounded_limit)
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema("/api/operations/superseded-cleanup", schema_name="operations_superseded_cleanup"),
        "status": "ok",
        "mode": "preview",
        "dry_run": True,
        "result": _json_ready(result),
        "counts": _superseded_cleanup_counts(result),
        "operator_boundary": _superseded_cleanup_boundary(apply=False),
        "audit_run": None,
        "note": "Preview only. Use apply with confirm=true and a requested_reason to mark recovered failures superseded.",
    }


def apply_superseded_cleanup_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not bool(payload.get("confirm")):
        raise ValueError("confirm=true is required before applying superseded cleanup")
    requested_reason = (_text(payload.get("requested_reason")) or "").strip()
    if not requested_reason:
        raise ValueError("requested_reason is required before applying superseded cleanup")
    operator_id = (_text(payload.get("operator_id")) or "").strip() or "operator"
    limit = max(1, min(int(payload.get("limit") or 500), 1000))
    started_at = pd.Timestamp.utcnow()
    run_id = f"superseded_failure_cleanup_apply:{started_at.strftime('%Y%m%dT%H%M%S%fZ')}"
    try:
        result = cleanup_superseded_failures(apply=True, limit=limit)
    except Exception as exc:
        completed_at = pd.Timestamp.utcnow()
        audit_run = _persist_superseded_cleanup_audit(
            run_id=run_id,
            status="failed",
            started_at=started_at,
            completed_at=completed_at,
            operator_id=operator_id,
            requested_reason=requested_reason,
            limit=limit,
            result=None,
            error=f"{type(exc).__name__}: {exc}",
        )
        # Surface the original error through the API guard while keeping the audit row.
        raise RuntimeError(f"superseded cleanup failed; audit_run={audit_run.get('run_id')}: {type(exc).__name__}: {exc}") from exc
    completed_at = pd.Timestamp.utcnow()
    audit_run = _persist_superseded_cleanup_audit(
        run_id=run_id,
        status="ok",
        started_at=started_at,
        completed_at=completed_at,
        operator_id=operator_id,
        requested_reason=requested_reason,
        limit=limit,
        result=result,
    )
    return {
        "generated_at": completed_at.isoformat(),
        "api_schema": _operator_api_schema("/api/operations/superseded-cleanup/apply", schema_name="operations_superseded_cleanup_apply", read_only=False),
        "status": "ok",
        "mode": "apply",
        "dry_run": False,
        "result": _json_ready(result),
        "counts": _superseded_cleanup_counts(result),
        "operator_boundary": _superseded_cleanup_boundary(apply=True),
        "audit_run": _json_ready(audit_run),
        "note": "Applied only superseded metadata markers for recovered failures. Portfolio, actions, config, and broker state were not changed.",
    }


def _json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    if isinstance(value, pd.Timestamp):
        return _ts(value)
    if hasattr(value, "item") and not isinstance(value, (str, bytes, bytearray)):
        try:
            return _json_ready(value.item())
        except Exception as exc:
            _record_operator_local_fallback(
                source="operator_api_json_ready",
                fallback_type="operator_api_json_ready_item_failed",
                reason="Operator API could not unwrap a scalar value with item() and kept the original value for later serialization.",
                error=exc,
                metadata={"value_type": type(value).__name__},
            )
    if hasattr(value, "isoformat") and not isinstance(value, str):
        try:
            return value.isoformat()
        except Exception as exc:
            _record_operator_local_fallback(
                source="operator_api_json_ready",
                fallback_type="operator_api_json_ready_isoformat_failed",
                reason="Operator API could not serialize an object with isoformat() and kept the original value for later serialization.",
                error=exc,
                metadata={"value_type": type(value).__name__},
            )
    try:
        if pd.isna(value):
            return None
    except Exception as exc:
        _record_operator_local_fallback(
            source="operator_api_json_ready",
            fallback_type="operator_api_json_ready_missing_check_failed",
            reason="Operator API could not evaluate missingness while preparing JSON and kept the original value.",
            error=exc,
            metadata={"value_type": type(value).__name__},
        )
    return value
















EVENT_POLICY_LLM_RESOLVED_ACTIONS = {"NO_ACTION", "BUY_WATCH", "REDUCE_EXPOSURE_REVIEW"}






















_SUCCESS_PROCESSING_STATUSES = {"ok", "success", "completed", "processed"}
_RECOVERED_DOCUMENT_STATUSES = {"completed", "success", "ok", "processed", "skipped", "unavailable"}












def ensure_manual_review_decisions_table() -> None:
    ensure_operator_api_audit_tables()




MANUAL_REVIEW_ACTION_CODES = {"BUY", "BUY_MORE", "SELL", "PARTIAL_SELL", "TIGHTEN_STOP"}




















def _identity_issue_row(row: dict[str, Any]) -> dict[str, Any]:
    exchanges_tried = _jsonish(row.get("exchanges_tried_json"))
    fallback_tried = _jsonish(row.get("fallback_tried_json"))
    context = _jsonish(row.get("context_json"))
    resolution_context = _jsonish(row.get("resolution_context_json"))
    if not isinstance(exchanges_tried, list):
        exchanges_tried = []
    if not isinstance(fallback_tried, list):
        fallback_tried = []
    if not isinstance(context, dict):
        context = {}
    if not isinstance(resolution_context, dict):
        resolution_context = {}
    symbol = _text(row.get("symbol"))
    exchange = _text(row.get("requested_exchange"))
    issue_type = _text(row.get("issue_type")) or "identity_issue"
    return {
        "issue_key": _text(row.get("issue_key")),
        "issue_type": issue_type,
        "status": _text(row.get("status")) or "open",
        "symbol": symbol,
        "requested_exchange": exchange,
        "asset_type": _text(row.get("asset_type")),
        "company_master_id": _text(row.get("company_master_id")),
        "source": _text(row.get("source")),
        "error_text": _text(row.get("error_text")),
        "resolution_error_text": _text(row.get("resolution_error_text")),
        "suggested_action": _text(row.get("suggested_action")),
        "attempt_count": _count_value(row.get("attempt_count"), fallback=0),
        "first_seen_at": _ts(row.get("first_seen_at")),
        "last_seen_at": _ts(row.get("last_seen_at")),
        "resolved_at": _ts(row.get("resolved_at")),
        "load_ts": _ts(row.get("load_ts")),
        "exchanges_tried": exchanges_tried,
        "fallback_tried": fallback_tried,
        "context": context,
        "resolution_context": resolution_context,
        "manual_review_item_id": f"identity_issue:{IDENTITY_ISSUES_TABLE}:{row.get('issue_key')}",
        "operator_boundary": {
            "read_only": True,
            "mutates_identity_mapping": False,
            "mutates_broker_execution": False,
            "next_review_surface": "Manual Review",
        },
        "repair_hint": (
            _text(row.get("suggested_action"))
            or f"Fix the {exchange or 'exchange'}:{symbol or 'symbol'} security mapping, then rerun the failed source."
        ),
    }


def build_identity_issues_payload(*, limit: int = 100, symbol: str | None = None) -> dict[str, Any]:
    skipped: list[dict[str, Any]] = []
    if not _table_exists(IDENTITY_ISSUES_TABLE):
        skipped.append({"source": IDENTITY_ISSUES_TABLE, "error": "missing_table"})
        issues: list[dict[str, Any]] = []
    else:
        df = sql_to_df(
            f"""
            SELECT *
            FROM {IDENTITY_ISSUES_TABLE}
            WHERE COALESCE(status, 'open') IN ('open', 'active')
            ORDER BY last_seen_at DESC NULLS LAST, first_seen_at DESC NULLS LAST
            LIMIT %s
            """,
            params=(max(1, int(limit)),),
            retries=3,
        )
        rows = [_identity_issue_row(row) for row in _records(df)]
        symbol_filter = _text(symbol)
        if symbol_filter:
            symbol_filter = symbol_filter.upper()
            rows = [row for row in rows if str(row.get("symbol") or "").upper() == symbol_filter]
        issues = rows[: max(1, int(limit))]
    by_type: dict[str, int] = {}
    by_exchange: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for row in issues:
        by_type[str(row.get("issue_type") or "identity_issue")] = by_type.get(str(row.get("issue_type") or "identity_issue"), 0) + 1
        by_exchange[str(row.get("requested_exchange") or "unknown")] = by_exchange.get(str(row.get("requested_exchange") or "unknown"), 0) + 1
        by_source[str(row.get("source") or "unknown")] = by_source.get(str(row.get("source") or "unknown"), 0) + 1
    source_warnings = _operator_source_warnings(issues, default_source=IDENTITY_ISSUES_TABLE, source_field="source", skipped=skipped)
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema("/api/identity-issues", schema_name="identity_issues"),
        "status": "ok",
        "source_warnings": source_warnings,
        "summary": {
            "total_open": len(issues),
            "by_type": by_type,
            "by_exchange": by_exchange,
            "by_source": by_source,
            "read_only": True,
            "broker_execution_enabled": False,
            "source_warnings": source_warnings,
        },
        "issues": issues,
        "skipped": skipped,
    }


def resolve_identity_issues_payload(*, payload: dict[str, Any] | None = None, apply: bool = False) -> dict[str, Any]:
    request = payload if isinstance(payload, dict) else {}
    limit = _bounded_limit(request.get("limit"), default=100, maximum=500)
    raw_keys = request.get("issue_keys")
    issue_keys = [str(key).strip() for key in raw_keys if str(key or "").strip()] if isinstance(raw_keys, list) else []
    endpoint = "/api/identity-issues/resolve-apply" if apply else "/api/identity-issues/resolve-preview"
    schema = _operator_api_schema(endpoint, schema_name="identity_issue_resolution")
    schema["read_only"] = not bool(apply)
    schema["broker_execution_enabled"] = False
    if apply and not issue_keys:
        return {
            "generated_at": pd.Timestamp.utcnow().isoformat(),
            "api_schema": schema,
            "status": "error",
            "mode": "apply",
            "checked_rows": 0,
            "counts": {},
            "results": [],
            "requested_issue_keys": [],
            "operator_boundary": {
                "mutates_identity_issue_status": True,
                "mutates_identity_mapping": False,
                "mutates_broker_execution": False,
                "requires_preview_issue_keys": True,
            },
            "note": "Apply requires explicit issue_keys from a previous preview. No rows were changed.",
        }
    summary = resolve_open_identity_issues(limit=limit, apply=bool(apply), issue_keys=issue_keys or None)
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": schema,
        "status": str(summary.get("status") or "ok"),
        "mode": str(summary.get("mode") or ("apply" if apply else "dry_run")),
        "checked_rows": int(summary.get("checked_rows") or 0),
        "counts": summary.get("counts") if isinstance(summary.get("counts"), dict) else {},
        "results": summary.get("results") if isinstance(summary.get("results"), list) else [],
        "requested_issue_keys": summary.get("requested_issue_keys") if isinstance(summary.get("requested_issue_keys"), list) else [],
        "operator_boundary": {
            "mutates_identity_issue_status": bool(apply),
            "mutates_identity_mapping": False,
            "mutates_broker_execution": False,
            "requires_preview_issue_keys": bool(apply),
            "safe_to_run_during_market": True,
        },
        "note": (
            "Resolved identity issue rows were closed. Re-run Health and advisory/data source if needed."
            if apply
            else "Preview only. Use issue_keys from would_resolve rows if you want to close them."
        ),
    }




























_MANUAL_REVIEW_COMPACT_RAW_KEYS = {
    "action_code",
    "action_reason",
    "action_source",
    "action_status",
    "action_type",
    "asof_date",
    "asset_type",
    "company_master_id",
    "completed_at",
    "confidence",
    "created_at",
    "error",
    "error_text",
    "event_class",
    "event_id",
    "execution_id",
    "execution_reason",
    "execution_status",
    "issue_key",
    "issue_type",
    "last_error",
    "last_seen_at",
    "load_ts",
    "losing_action_code",
    "match_reason",
    "match_score",
    "match_status",
    "materiality",
    "observed_at",
    "observed_value",
    "ocr_status",
    "parse_status",
    "policy_class",
    "policy_score",
    "published_on",
    "reason",
    "reason_contract_status",
    "reason_detail",
    "requested_exchange",
    "resolution_status",
    "review_status",
    "reviewed_at",
    "run_id",
    "setup_id",
    "signal_id",
    "signal_type",
    "source_action",
    "source_key",
    "source_table",
    "stage",
    "status",
    "suggested_action",
    "symbol",
    "threshold_value",
    "ticker",
    "unique_id",
    "updated_at",
    "wait_question",
    "wait_signal_followup",
    "winning_action_code",
}
















MANUAL_REVIEW_LANES = {"all", "investment_review", "technical_issue", "research_config"}






def build_hypotheses_payload(*, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    row_limit = _bounded_limit(limit, default=100, maximum=500)
    row_offset = _bounded_offset(offset)
    cache_key = (
        "hypotheses_payload",
        json.dumps(
            {
                "limit": row_limit,
                "offset": row_offset,
                "load_hypotheses_id": id(load_hypotheses),
                "load_matches_id": id(load_matches),
                "load_action_plans_id": id(load_action_plans),
                "load_wait_signals_id": id(load_wait_signals),
                "load_wait_signal_matches_id": id(load_wait_signal_matches),
                "latest_promotion_audits_id": id(latest_promotion_audits),
            },
            sort_keys=True,
        ),
    )
    now = time.monotonic()
    cached = _PAYLOAD_CACHE.get(cache_key)
    if cached and OPERATOR_API_PAYLOAD_CACHE_SECONDS > 0 and (now - cached[0]) <= OPERATOR_API_PAYLOAD_CACHE_SECONDS:
        return cached[1]
    all_hypotheses = load_hypotheses()
    hypothesis_rows = all_hypotheses.to_dict(orient="records") if not all_hypotheses.empty else []
    hypothesis_page, hypothesis_meta = _page_any_rows(hypothesis_rows, limit=row_limit, offset=row_offset, default=100, maximum=500)
    if hypothesis_page:
        matches = load_matches(limit=row_limit)
        action_plans = load_action_plans(limit=row_limit)
        wait_signals = load_wait_signals(limit=row_limit)
        wait_signal_matches = load_wait_signal_matches(limit=row_limit)
        promotion_audits = latest_promotion_audits([
            str(row.get("hypothesis_id") or "")
            for row in hypothesis_page
            if str(row.get("hypothesis_id") or "").strip()
        ])
    else:
        matches = pd.DataFrame()
        action_plans = pd.DataFrame()
        wait_signals = pd.DataFrame()
        wait_signal_matches = pd.DataFrame()
        promotion_audits = []
    matches_rows = matches.to_dict(orient="records") if not matches.empty else []
    action_plan_rows = action_plans.to_dict(orient="records") if not action_plans.empty else []
    wait_signal_rows = wait_signals.to_dict(orient="records") if not wait_signals.empty else []
    wait_signal_match_rows = wait_signal_matches.to_dict(orient="records") if not wait_signal_matches.empty else []
    payload = {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema("/api/hypotheses", schema_name="hypotheses"),
        "status": "ok",
        "hypotheses": hypothesis_page,
        "matches": matches_rows,
        "action_plans": action_plan_rows,
        "wait_signals": wait_signal_rows,
        "wait_signal_matches": wait_signal_match_rows,
        "promotion_audits": promotion_audits,
        "pagination": {
            "hypotheses": hypothesis_meta,
            "matches": _bounded_list_contract(matches_rows, limit=row_limit, default=100, maximum=500),
            "action_plans": _bounded_list_contract(action_plan_rows, limit=row_limit, default=100, maximum=500),
            "wait_signals": _bounded_list_contract(wait_signal_rows, limit=row_limit, default=100, maximum=500),
            "wait_signal_matches": _bounded_list_contract(wait_signal_match_rows, limit=row_limit, default=100, maximum=500),
            "promotion_audits": _bounded_list_contract(promotion_audits, limit=row_limit, default=100, maximum=500),
        },
    }
    _PAYLOAD_CACHE[cache_key] = (now, payload)
    return payload


def build_wait_signals_payload(*, limit: int = 100, status: str | None = None, symbol: str | None = None) -> dict[str, Any]:
    signals = load_wait_signals(status=status, symbol=symbol, limit=limit)
    signal_rows = [_wait_signal_view(row) for row in (signals.to_dict(orient="records") if not signals.empty else [])]
    signal_ids = [_text(row.get("signal_id")) for row in signal_rows]
    signal_matches = load_wait_signal_matches(signal_ids=[item for item in signal_ids if item], symbol=symbol, limit=limit) if signal_ids else pd.DataFrame()
    match_rows = [_wait_signal_match_view(row) for row in (signal_matches.to_dict(orient="records") if not signal_matches.empty else [])]
    latest_match_by_signal: dict[str, dict[str, Any]] = {}
    for match in match_rows:
        signal_id = _text(match.get("signal_id"))
        if signal_id and signal_id not in latest_match_by_signal:
            latest_match_by_signal[signal_id] = match
    now = pd.Timestamp.utcnow()
    for row in signal_rows:
        signal_id = _text(row.get("signal_id"))
        row["latest_match"] = latest_match_by_signal.get(signal_id) if signal_id else None
        row["state_bucket"] = _wait_signal_bucket(row, now=now)
    sections = {
        "active": [row for row in signal_rows if row.get("state_bucket") == "active"],
        "matched": [row for row in signal_rows if row.get("state_bucket") == "matched"],
        "closed": [row for row in signal_rows if row.get("state_bucket") == "closed"],
        "expired": [row for row in signal_rows if row.get("state_bucket") == "expired"],
    }
    source_warnings = _operator_source_warnings(signal_rows + match_rows, default_source=WAIT_SIGNALS_TABLE, source_field="source_table")
    summary = _wait_signal_summary(signal_rows, match_rows)
    summary["source_warnings"] = source_warnings
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema("/api/wait-signals", schema_name="wait_signals"),
        "status": "ok",
        "source_warnings": source_warnings,
        "summary": summary,
        "sections": sections,
        "signals": signal_rows,
        "matches": match_rows,
        "match_result": None,
    }


def run_wait_signal_match_payload(payload: dict[str, Any]) -> dict[str, Any]:
    symbol = _text(payload.get("symbol"))
    raw_symbols = payload.get("symbols")
    symbols = [symbol] if symbol else []
    if isinstance(raw_symbols, list):
        symbols.extend(str(item).strip().upper() for item in raw_symbols if str(item or "").strip())
    symbols = sorted(set(symbols)) or None
    limit = max(1, min(int(payload.get("limit") or 250), 1000))
    result = match_wait_signals(symbols=symbols, limit=limit, persist=True)
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "api_schema": _operator_api_schema("/api/wait-signals/match", schema_name="wait_signal_match"),
        "status": "ok",
        "match_result": _json_ready(result),
    }


def _wait_signal_bucket(row: dict[str, Any], *, now: pd.Timestamp) -> str:
    status = str(row.get("status") or "active").strip().lower()
    if status == "matched":
        return "matched"
    if status not in {"active", "open", ""}:
        return "closed"
    valid_until = pd.to_datetime(row.get("valid_until"), utc=True, errors="coerce")
    if not pd.isna(valid_until) and valid_until < now:
        return "expired"
    return "active"


def _wait_signal_source_label(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text == "manual_review_decision":
        return "Manual review"
    if text == "hypothesis_action_plan":
        return "Playbook action plan"
    return text.replace("_", " ").title() if text else "System"


def _wait_signal_condition_summary(row: dict[str, Any]) -> str:
    condition = _jsonish(row.get("condition_json"))
    signal_type = str(row.get("signal_type") or "").strip().lower()
    condition_type = str(condition.get("condition_type") or signal_type).strip().lower() if isinstance(condition, dict) else signal_type
    issue_reason = condition.get("issue_reason") if isinstance(condition, dict) else None
    if issue_reason:
        return f"Condition issue: {issue_reason}"
    if condition_type in {"price_close", "price_level"}:
        operator = str(condition.get("operator") or "").strip().lower()
        operator_label = {
            "gte": "above or equal to",
            "above": "above",
            "close_above": "above",
            "lte": "below or equal to",
            "below": "below",
            "close_below": "below",
        }.get(operator, operator.replace("_", " "))
        threshold = condition.get("threshold")
        if operator_label and threshold is not None:
            observed_field = str(condition.get("observed_field") or "close").replace("_", " ")
            return f"Wait for latest {observed_field} to be {operator_label} {threshold}."
    keywords = condition.get("keywords") if isinstance(condition, dict) else None
    if isinstance(keywords, list) and keywords:
        label = condition_type.replace("_", " ") if condition_type else "evidence"
        return f"Wait for {label} evidence containing: {', '.join(str(item) for item in keywords[:8])}."
    question = _text(row.get("wait_question"))
    if question:
        return question
    return _text(row.get("operator_summary")) or "Wait condition was recorded without a readable summary."


def _wait_signal_evidence_summary(row: dict[str, Any]) -> str:
    evidence = _jsonish(row.get("evidence_json"))
    reason = _text(row.get("match_reason"))
    if reason:
        return reason
    if isinstance(evidence, dict):
        subject = _text(evidence.get("subject"))
        summary = _text(evidence.get("concise_summary_text"))
        if subject and summary:
            return f"{subject}: {summary}"
        if subject:
            return subject
        if summary:
            return summary
    return "Matched evidence is available in raw details."


def _wait_signal_view(row: dict[str, Any]) -> dict[str, Any]:
    out = _json_ready(dict(row))
    out["source_label"] = _wait_signal_source_label(out.get("generated_by"))
    out["condition"] = _jsonish(out.get("condition_json"))
    out["condition_type"] = out["condition"].get("condition_type") if isinstance(out["condition"], dict) else out.get("signal_type")
    out["condition_issue"] = out["condition"].get("issue_reason") if isinstance(out["condition"], dict) else None
    out["condition_summary"] = _wait_signal_condition_summary(out)
    out["is_manual_review_signal"] = str(out.get("generated_by") or "").strip().lower() == "manual_review_decision"
    out["is_playbook_signal"] = str(out.get("generated_by") or "").strip().lower() == "hypothesis_action_plan"
    condition = out["condition"] if isinstance(out.get("condition"), dict) else {}
    out["manual_review_item_id"] = condition.get("manual_review_item_id") or (out.get("source_key") if out["is_manual_review_signal"] else None)
    out["manual_review_source_table"] = out.get("source_table") if out["is_manual_review_signal"] else None
    out["manual_review_source_key"] = out.get("source_key") if out["is_manual_review_signal"] else None
    return out


def _wait_signal_match_view(row: dict[str, Any]) -> dict[str, Any]:
    out = _json_ready(dict(row))
    out["evidence"] = _jsonish(out.get("evidence_json"))
    out["evidence_summary"] = _wait_signal_evidence_summary(out)
    wait_signal = out["evidence"].get("wait_signal") if isinstance(out.get("evidence"), dict) and isinstance(out["evidence"].get("wait_signal"), dict) else {}
    out["manual_review_item_id"] = wait_signal.get("manual_review_item_id")
    out["manual_review_source_table"] = wait_signal.get("manual_review_source_table")
    out["manual_review_source_key"] = wait_signal.get("manual_review_source_key")
    out["wait_question"] = wait_signal.get("wait_question")
    out["wait_generated_by"] = wait_signal.get("generated_by")
    return out


def _wait_signal_summary(signals: list[dict[str, Any]], matches: list[dict[str, Any]]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    by_source: dict[str, int] = {}
    by_signal_type: dict[str, int] = {}
    by_bucket: dict[str, int] = {}
    for row in signals:
        status = str(row.get("status") or "active").strip().lower()
        source = str(row.get("source_label") or "System")
        signal_type = str(row.get("signal_type") or "unknown")
        bucket = str(row.get("state_bucket") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
        by_source[source] = by_source.get(source, 0) + 1
        by_signal_type[signal_type] = by_signal_type.get(signal_type, 0) + 1
        by_bucket[bucket] = by_bucket.get(bucket, 0) + 1
    return {
        "total_signals": len(signals),
        "total_matches": len(matches),
        "active": by_bucket.get("active", 0),
        "matched": by_bucket.get("matched", 0),
        "expired": by_bucket.get("expired", 0),
        "closed": by_bucket.get("closed", 0),
        "manual_review": sum(1 for row in signals if row.get("is_manual_review_signal")),
        "playbook": sum(1 for row in signals if row.get("is_playbook_signal")),
        "by_status": by_status,
        "by_source": by_source,
        "by_signal_type": by_signal_type,
    }


def create_hypothesis_payload(payload: dict[str, Any]) -> dict[str, Any]:
    row = create_hypothesis(payload)
    return {"status": "ok", "hypothesis": row}


def preview_hypothesis_create_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return preview_hypothesis_payload(payload)


def update_hypothesis_payload(hypothesis_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    row = update_hypothesis(hypothesis_id, payload)
    return {"status": "ok", "hypothesis": row}


def run_promotion_audit_payload(hypothesis_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return run_promotion_audit(
        hypothesis_id,
        lookback_days=int(payload.get("lookback_days") or 365),
        min_matches=int(payload.get("min_matches") or 3),
        operator_notes=payload.get("operator_notes"),
        approved_by=payload.get("approved_by"),
        persist=bool(payload.get("persist", True)),
    )


def run_hypothesis_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return run_hypothesis_scan(
        hypothesis_id=payload.get("hypothesis_id"),
        from_date=_parse_timestamp(payload.get("from_date")) if payload.get("from_date") else None,
        to_date=_parse_timestamp(payload.get("to_date")) if payload.get("to_date") else None,
        sources=payload.get("sources") or ["news", "announcements", "announcement_documents"],
        persist=bool(payload.get("persist", True)),
        build_actions=bool(payload.get("build_actions", True)),
        use_llm=bool(payload.get("use_llm", True)),
        model=payload.get("model"),
    )


def create_app():
    try:
        from fastapi import Body, FastAPI, HTTPException, Query
        from fastapi.middleware.cors import CORSMiddleware
    except ImportError as exc:
        raise RuntimeError("FastAPI is required for the operator API. Install requirements.txt first.") from exc

    app = FastAPI(title="Stockey Operator API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def slow_request_logger(request, call_next):
        started = time.perf_counter()
        status_code = 500
        response = None
        try:
            response = await call_next(request)
            status_code = int(response.status_code)
            return response
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            response_bytes = None
            if response is not None:
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        response_bytes = int(content_length)
                    except ValueError as exc:
                        record_local_fallback_event(
                            module="advisory.api.app",
                            fallback_type="operator_api_response_size_parse_failed",
                            source="slow_request_logger",
                            severity="warn",
                            reason="Operator API middleware could not parse response content-length for slow/large response telemetry.",
                            error=exc,
                            metadata={
                                "method": str(request.method),
                                "route": str(request.url.path),
                                "content_length": str(content_length),
                                "status_code": int(status_code),
                            },
                        )
                        response_bytes = None
            try:
                record_slow_operation(
                    kind="operator_api_request",
                    operation=f"{request.method} {request.url.path}",
                    elapsed_ms=elapsed_ms,
                    threshold_ms=OPERATOR_API_SLOW_REQUEST_MS,
                    details={
                        "method": request.method,
                        "route": request.url.path,
                        "query": str(request.url.query or ""),
                        "status_code": status_code,
                        "response_bytes": response_bytes,
                    },
                )
                if response_bytes is not None:
                    record_slow_operation(
                        kind="operator_api_large_response",
                        operation=f"{request.method} {request.url.path}",
                        elapsed_ms=float(response_bytes),
                        threshold_ms=float(OPERATOR_API_LARGE_RESPONSE_BYTES),
                        details={
                            "method": request.method,
                            "route": request.url.path,
                            "query": str(request.url.query or ""),
                            "status_code": status_code,
                            "response_bytes": response_bytes,
                        },
                    )
            except Exception as exc:
                # Slow logging must never turn a successful operator API response into a failure.
                record_local_fallback_event(
                    module="advisory.api.app",
                    fallback_type="operator_api_slow_request_logging_failed",
                    source="slow_request_logger",
                    severity="warn",
                    reason="Operator API slow-request telemetry failed; the API response was still returned.",
                    error=exc,
                    metadata={
                        "method": str(request.method),
                        "route": str(request.url.path),
                        "query": str(request.url.query or ""),
                        "status_code": int(status_code),
                        "elapsed_ms": float(elapsed_ms),
                        "response_bytes": response_bytes,
                    },
                )

    def _guard(callable_obj, *, route: str | None = None, **kwargs):
        operation = getattr(callable_obj, "__name__", str(callable_obj))
        try:
            return _json_ready(callable_obj(**kwargs))
        except ValueError as exc:
            record_operator_api_error(
                exc=exc,
                operation=operation,
                route=route,
                status_code=400,
                request_context={"kwargs": kwargs},
            )
            raise HTTPException(
                status_code=400,
                detail={"message": str(exc), "error_type": type(exc).__name__, "operation": operation, "route": route},
            ) from exc
        except Exception as exc:
            record_operator_api_error(
                exc=exc,
                operation=operation,
                route=route,
                status_code=500,
                request_context={"kwargs": kwargs},
            )
            raise HTTPException(
                status_code=500,
                detail={"message": str(exc), "error_type": type(exc).__name__, "operation": operation, "route": route},
            ) from exc

    @app.get("/api/health", response_model=OperatorHealthResponse)
    def health():
        return _guard(build_health_payload, route="/api/health")

    @app.get("/api/runtime", response_model=OperatorRuntimeResponse)
    def runtime():
        return _guard(build_runtime_payload, route="/api/runtime")

    @app.get("/api/health/details", response_model=OperatorHealthDetailsResponse)
    def health_details(mode: str = Query(default="fast", pattern="^(fast|full)$"), compact: bool = Query(default=True)):
        return _guard(lambda: build_operator_health_payload(mode=mode, compact=compact), route="/api/health/details")

    @app.get("/api/operations/smoke", response_model=OperationsSmokeResponse)
    def operations_smoke():
        return _guard(build_operations_smoke_payload, route="/api/operations/smoke")

    @app.get("/api/operations/cron-logs", response_model=OperationsCronLogsResponse)
    def operations_cron_logs(limit: int = Query(default=20, ge=1, le=100), lines: int = Query(default=80, ge=1, le=300), offset: int = Query(default=0, ge=0)):
        return _guard(build_cron_logs_payload, route="/api/operations/cron-logs", limit=limit, lines=lines, offset=offset)

    @app.get("/api/operations/cron-status", response_model=OperationsCronStatusResponse)
    def operations_cron_status(limit: int = Query(default=50, ge=1, le=200), lines: int = Query(default=12, ge=0, le=80), offset: int = Query(default=0, ge=0)):
        return _guard(build_cron_status_payload, route="/api/operations/cron-status", limit=limit, lines=lines, offset=offset)

    @app.get("/api/operations/commands", response_model=OperationsCommandsResponse)
    def operations_commands(limit: int = Query(default=25, ge=1, le=100)):
        return _guard(build_operator_commands_payload, route="/api/operations/commands", limit=limit)

    @app.post("/api/operations/commands/run")
    def operations_command_run(payload: dict[str, Any] = Body(...)):
        return _guard(run_operator_command_payload, route="/api/operations/commands/run", payload=payload)

    @app.get("/api/operations/api-errors", response_model=OperationsApiErrorsResponse)
    def operations_api_errors(limit: int = Query(default=50, ge=1, le=200)):
        return _guard(build_operator_api_errors_payload, route="/api/operations/api-errors", limit=limit)

    @app.get("/api/operations/ingestion-state", response_model=OperationsIngestionStateResponse)
    def operations_ingestion_state(
        source: str | None = None,
        status: str | None = None,
        limit: int = Query(default=1000, ge=1, le=5000),
        sample_limit: int = Query(default=20, ge=0, le=100),
    ):
        return _guard(build_ingestion_state_payload, route="/api/operations/ingestion-state", source=source, status=status, limit=limit, sample_limit=sample_limit)

    @app.get("/api/operations/superseded-cleanup", response_model=OperationsSupersededCleanupResponse)
    def operations_superseded_cleanup(limit: int = Query(default=500, ge=1, le=1000)):
        return _guard(build_superseded_cleanup_payload, route="/api/operations/superseded-cleanup", limit=limit)

    @app.post("/api/operations/superseded-cleanup/apply", response_model=OperationsSupersededCleanupResponse)
    def operations_superseded_cleanup_apply(payload: dict[str, Any] = Body(...)):
        return _guard(apply_superseded_cleanup_payload, route="/api/operations/superseded-cleanup/apply", payload=payload)

    @app.post("/api/operations/slow-issues/status")
    def operations_slow_issue_status(payload: dict[str, Any] = Body(...)):
        return _guard(update_slow_issue_payload, route="/api/operations/slow-issues/status", payload=payload)

    @app.get("/api/research/event-model-promotion-check", response_model=EventModelPromotionCheckResponse)
    def research_event_model_promotion_check():
        return _guard(build_event_model_promotion_check_payload, route="/api/research/event-model-promotion-check")






    @app.get("/api/research/recommendation-diagnostics", response_model=RecommendationDiagnosticsResponse)
    def research_recommendation_diagnostics(
        asof_date: str | None = None,
        limit: int = Query(default=500, ge=1, le=2000),
    ):
        return _guard(
            build_recommendation_diagnostics_payload,
            route="/api/research/recommendation-diagnostics",
            asof_date=asof_date,
            limit=limit,
        )

    @app.get("/api/research/event-model-artifacts", response_model=EventModelArtifactsResponse)
    def research_event_model_artifacts(limit: int = Query(default=50, ge=0, le=200), offset: int = Query(default=0, ge=0)):
        return _guard(build_event_model_artifacts_payload, route="/api/research/event-model-artifacts", limit=limit, offset=offset)

    @app.get("/api/research/ledger", response_model=ResearchLedgerResponse)
    def research_ledger(limit: int = Query(default=25, ge=1, le=100), offset: int = Query(default=0, ge=0)):
        return _guard(build_research_ledger_payload, route="/api/research/ledger", limit=limit, offset=offset)

    @app.get("/api/research/prompt-registry", response_model=PromptRegistryResponse)
    def research_prompt_registry(owner_area: str | None = None, authority_scope: str | None = None, limit: int = Query(default=100, ge=0, le=500), offset: int = Query(default=0, ge=0)):
        return _guard(build_prompt_registry_api_payload, route="/api/research/prompt-registry", owner_area=owner_area, authority_scope=authority_scope, limit=limit, offset=offset)

    @app.post("/api/screeners/preview", response_model=ScreenerPreviewResponse)
    def screener_preview(payload: dict[str, Any] = Body(...)):
        return _guard(build_screener_preview_payload, route="/api/screeners/preview", payload=payload)

    @app.get("/api/screeners/coverage", response_model=ScreenerCoverageResponse)
    def screener_coverage(asof_date: str | None = None, lookback_days: int = Query(default=30, ge=0, le=365), limit: int = Query(default=50, ge=1, le=200)):
        return _guard(build_screener_coverage_api_payload, route="/api/screeners/coverage", asof_date=asof_date, lookback_days=lookback_days, limit=limit)

    @app.get("/api/screeners/failures", response_model=ScreenerFailuresResponse)
    def screener_failures(hours: int = Query(default=24, ge=1, le=168), limit: int = Query(default=25, ge=1, le=100)):
        return _guard(build_screener_failures_api_payload, route="/api/screeners/failures", hours=hours, limit=limit)


    @app.get("/api/identity-issues", response_model=IdentityIssuesResponse)
    def identity_issues(limit: int = Query(default=100, ge=1, le=500), symbol: str | None = None):
        return _guard(build_identity_issues_payload, route="/api/identity-issues", limit=limit, symbol=symbol)

    @app.post("/api/identity-issues/resolve-preview", response_model=IdentityIssueResolutionResponse)
    def identity_issues_resolve_preview(payload: dict[str, Any] = Body(default_factory=dict)):
        return _guard(resolve_identity_issues_payload, route="/api/identity-issues/resolve-preview", payload=payload, apply=False)

    @app.post("/api/identity-issues/resolve-apply", response_model=IdentityIssueResolutionResponse)
    def identity_issues_resolve_apply(payload: dict[str, Any] = Body(...)):
        return _guard(resolve_identity_issues_payload, route="/api/identity-issues/resolve-apply", payload=payload, apply=True)


    @app.get("/api/summary", response_model=OperatorSummaryResponse)
    def summary(asof_date: str | None = None):
        return _guard(build_summary_payload, route="/api/summary", asof_date=asof_date)

    @app.get("/api/home", response_model=OperatorHomeResponse)
    def home(asof_date: str | None = None, include_action_cards: bool = Query(default=False)):
        return _guard(build_home_payload, route="/api/home", asof_date=asof_date, include_action_cards=include_action_cards)

    @app.get("/api/actions", response_model=OperatorActionsResponse)
    def actions(
        asof_date: str | None = None,
        limit: int = Query(default=50, ge=0, le=500),
        offset: int = Query(default=0, ge=0),
        symbol: str | None = None,
        action: str | None = None,
        status: str | None = None,
        search: str | None = None,
        compact: bool = Query(default=True),
        include_feature_freshness: bool = Query(default=False),
        refresh_feature_freshness: bool = Query(default=False),
    ):
        return _guard(
            build_actions_payload,
            route="/api/actions",
            asof_date=asof_date,
            limit=limit,
            offset=offset,
            symbol=symbol,
            action=action,
            status=status,
            search=search,
            compact=compact,
            include_feature_freshness=include_feature_freshness,
            refresh_feature_freshness=refresh_feature_freshness,
        )

    @app.get("/api/actions/detail", response_model=OperatorDetailResponse)
    def action_detail(symbol: str | None = None, unique_id: str | None = None, setup_id: str | None = None, asof_date: str | None = None):
        return _guard(build_action_detail_payload, route="/api/actions/detail", symbol=symbol, unique_id=unique_id, setup_id=setup_id, asof_date=asof_date)

    @app.get("/api/action-conflict-rules", response_model=ActionConflictRulesResponse)
    def action_conflict_rules():
        return _guard(build_action_conflict_rules_payload, route="/api/action-conflict-rules")

    @app.post("/api/action-conflict-rules/promote", response_model=ActionConflictRuleWriteResponse)
    def action_conflict_rule_promote(payload: dict[str, Any] = Body(...)):
        return _guard(promote_action_conflict_rule_payload, route="/api/action-conflict-rules/promote", payload=payload)

    @app.post("/api/action-conflict-rules/{rule_id}", response_model=ActionConflictRuleWriteResponse)
    def action_conflict_rule_update(rule_id: str, payload: dict[str, Any] = Body(...)):
        return _guard(update_action_conflict_rule_payload, route="/api/action-conflict-rules/{rule_id}", rule_id=rule_id, payload=payload)

    @app.get("/api/signal-refresh", response_model=SignalRefreshResponse)
    def signal_refresh(
        limit: int = Query(default=50, ge=0, le=500),
        offset: int = Query(default=0, ge=0),
        symbol: str | None = None,
        status: str | None = None,
        search: str | None = None,
        compact: bool = Query(default=False),
    ):
        return _guard(build_signal_refresh_payload, route="/api/signal-refresh", limit=limit, offset=offset, symbol=symbol, status=status, search=search, compact=compact)









    @app.get("/api/portfolio", response_model=OperatorPortfolioResponse)
    def portfolio(
        asof_date: str | None = None,
        limit: int = Query(default=50, ge=0, le=500),
        offset: int = Query(default=0, ge=0),
        symbol: str | None = None,
        status: str | None = None,
        search: str | None = None,
        compact: bool = Query(default=True),
        bucket: str | None = None,
    ):
        return _guard(build_portfolio_payload, route="/api/portfolio", asof_date=asof_date, limit=limit, offset=offset, symbol=symbol, status=status, search=search, compact=compact, bucket=bucket)

    @app.get("/api/portfolio/{symbol}/detail", response_model=OperatorDetailResponse)
    def portfolio_detail(symbol: str, asof_date: str | None = None):
        return _guard(build_portfolio_detail_payload, route="/api/portfolio/{symbol}/detail", symbol=symbol, asof_date=asof_date)

    @app.get("/api/watchlist", response_model=OperatorWatchlistResponse)
    def watchlist(
        asof_date: str | None = None,
        section: str | None = None,
        limit: int = Query(default=25, ge=0, le=500),
        compact: bool = Query(default=True),
    ):
        return _guard(build_watchlist_payload, route="/api/watchlist", asof_date=asof_date, section=section, limit=limit, compact=compact)

    @app.get("/api/market-context", response_model=OperatorMarketContextResponse)
    def market_context(asof_date: str | None = None, limit: int = Query(default=50, ge=0, le=500)):
        return _guard(build_market_context_payload, route="/api/market-context", asof_date=asof_date, limit=limit)

    @app.get("/api/regime-overlays", response_model=RegimeOverlaysResponse)
    def regime_overlays(limit: int = Query(default=25, ge=1, le=100), status: str | None = None):
        return _guard(build_regime_overlays_payload, route="/api/regime-overlays", limit=limit, status=status)

    @app.post("/api/regime-overlays/decision", response_model=RegimeOverlayDecisionResponse)
    def regime_overlay_decision(payload: dict[str, Any] = Body(...)):
        return _guard(build_regime_overlay_decision_payload, route="/api/regime-overlays/decision", payload=payload)

    @app.get("/api/technical-calibration", response_model=TechnicalCalibrationResponse)
    def technical_calibration(limit: int = Query(default=25, ge=1, le=100)):
        return _guard(build_technical_calibration_payload, route="/api/technical-calibration", limit=limit)

    @app.get("/api/signal-quality", response_model=SignalQualityResponse)
    def signal_quality(limit: int = Query(default=10, ge=1, le=100)):
        return _guard(build_signal_quality_payload, route="/api/signal-quality", limit=limit)

    @app.post("/api/signal-quality/promotion-review", response_model=PromotionReviewResponse)
    def signal_quality_promotion_review(payload: dict[str, Any]):
        return _guard(build_signal_quality_promotion_review_payload, route="/api/signal-quality/promotion-review", payload=payload)

    @app.get("/api/signal-quality/promotion-reviews", response_model=SignalQualityPromotionReviewsResponse)
    def signal_quality_promotion_reviews(limit: int = Query(default=25, ge=1, le=100)):
        return _guard(build_signal_quality_promotion_reviews_payload, route="/api/signal-quality/promotion-reviews", limit=limit)

    @app.post("/api/signal-quality/promotion-review/decision", response_model=PromotionDecisionResponse)
    def signal_quality_promotion_review_decision(payload: dict[str, Any]):
        return _guard(build_signal_quality_promotion_review_decision_payload, route="/api/signal-quality/promotion-review/decision", payload=payload)




    @app.get("/api/config-change/previews", response_model=ConfigChangePreviewsResponse)
    def config_change_previews(limit: int = Query(default=25, ge=1, le=100)):
        return _guard(build_config_change_previews_payload, route="/api/config-change/previews", limit=limit)

    @app.get("/api/config-change/applications", response_model=ConfigChangeApplicationsResponse)
    def config_change_applications(limit: int = Query(default=25, ge=1, le=100)):
        return _guard(build_config_change_applications_payload, route="/api/config-change/applications", limit=limit)

    @app.post("/api/config-change/application-decision", response_model=ConfigChangeApplicationResponse)
    def config_change_application_decision(payload: dict[str, Any]):
        return _guard(build_config_change_application_decision_payload, route="/api/config-change/application-decision", payload=payload)

    @app.post("/api/config-change/technical-threshold-preview", response_model=ConfigChangePreviewResponse)
    def technical_threshold_config_change_preview(payload: dict[str, Any]):
        return _guard(build_technical_config_change_preview_payload, route="/api/config-change/technical-threshold-preview", payload=payload)

    @app.post("/api/config-change/signal-quality-preview", response_model=ConfigChangePreviewResponse)
    def signal_quality_config_change_preview(payload: dict[str, Any]):
        return _guard(build_signal_quality_config_change_preview_payload, route="/api/config-change/signal-quality-preview", payload=payload)



    @app.post("/api/technical-calibration/promotion-review", response_model=PromotionReviewResponse)
    def technical_calibration_promotion_review(payload: dict[str, Any]):
        return _guard(build_technical_threshold_promotion_review_payload, route="/api/technical-calibration/promotion-review", payload=payload)

    @app.get("/api/technical-calibration/promotion-reviews", response_model=TechnicalPromotionReviewsResponse)
    def technical_calibration_promotion_reviews(limit: int = Query(default=25, ge=1, le=100)):
        return _guard(build_technical_threshold_reviews_payload, route="/api/technical-calibration/promotion-reviews", limit=limit)

    @app.post("/api/technical-calibration/promotion-review/decision", response_model=PromotionDecisionResponse)
    def technical_calibration_promotion_review_decision(payload: dict[str, Any]):
        return _guard(build_technical_threshold_review_decision_payload, route="/api/technical-calibration/promotion-review/decision", payload=payload)







    @app.get("/api/symbols/{symbol}/trace", response_model=SymbolTraceResponse)
    def symbol_trace(symbol: str, limit: int = Query(default=100, ge=1, le=500)):
        payload = _guard(build_symbol_trace_payload, route="/api/symbols/{symbol}/trace", symbol=symbol, limit=limit)
        return _with_operator_api_schema(
            payload,
            endpoint="/api/symbols/{symbol}/trace",
            schema_name="operator_symbol_trace",
        )

    @app.get("/api/symbols/{symbol}/trace/summary", response_model=TraceSummaryResponse)
    def symbol_trace_summary(symbol: str, limit: int = Query(default=100, ge=1, le=500)):
        payload = _guard(build_symbol_trace_summary_payload, route="/api/symbols/{symbol}/trace/summary", symbol=symbol, limit=limit)
        return _with_operator_api_schema(
            payload,
            endpoint="/api/symbols/{symbol}/trace/summary",
            schema_name="operator_symbol_trace_summary",
        )

    @app.get("/api/symbols/{symbol}/feature-freshness", response_model=FeatureFreshnessResponse)
    def symbol_feature_freshness(symbol: str, asof_date: str | None = None):
        return _guard(build_feature_freshness_payload, route="/api/symbols/{symbol}/feature-freshness", symbol=symbol, asof_date=asof_date)

    @app.get("/api/data-health", response_model=DataHealthResponse)
    def data_health(asof_date: str | None = None):
        return _guard(build_data_health_payload, route="/api/data-health", asof_date=asof_date)

    @app.get("/api/hypotheses", response_model=HypothesesResponse)
    def hypotheses(limit: int = Query(default=100, ge=0, le=500), offset: int = Query(default=0, ge=0)):
        return _guard(build_hypotheses_payload, route="/api/hypotheses", limit=limit, offset=offset)

    @app.get("/api/llm-decisions")
    def llm_decisions(
        symbol: str | None = None,
        asof_date: str | None = None,
        limit: int = Query(default=50, ge=0, le=200),
        offset: int = Query(default=0, ge=0),
    ):
        return _guard(build_llm_decisions_payload, route="/api/llm-decisions",
                      symbol=symbol, asof_date=asof_date, limit=limit, offset=offset)

    @app.get("/api/recommendations-unified")
    def recommendations_unified(
        action: str | None = None,
        only_conflicts: bool = False,
        limit: int = Query(default=50, ge=0, le=200),
        offset: int = Query(default=0, ge=0),
    ):
        return _guard(build_recommendations_unified_payload, route="/api/recommendations-unified",
                      action=action, only_conflicts=only_conflicts, limit=limit, offset=offset)

    @app.get("/api/positions")
    def positions(status: str | None = "open", limit: int = Query(default=200, ge=0, le=500)):
        return _guard(build_positions_payload, route="/api/positions", status=status, limit=limit)

    @app.post("/api/positions/take")
    def positions_take(payload: dict[str, Any] = Body(...)):
        return _guard(build_position_take_payload, route="/api/positions/take", payload=payload)

    @app.post("/api/positions/exit")
    def positions_exit(payload: dict[str, Any] = Body(...)):
        return _guard(build_position_exit_payload, route="/api/positions/exit", payload=payload)

    @app.get("/api/symbol/{symbol}/why")
    def symbol_why(symbol: str, asof_date: str | None = None):
        return _guard(build_symbol_why_payload, route="/api/symbol/{symbol}/why", symbol=symbol, asof_date=asof_date)

    @app.get("/api/workbench")
    def workbench(top_n: int = Query(default=8, ge=1, le=25)):
        return _guard(build_workbench_payload, route="/api/workbench", top_n=top_n)

    @app.get("/api/health-hub")
    def health_hub(asof_date: str | None = None, error_limit: int = Query(default=25, ge=0, le=100)):
        return _guard(build_health_hub_payload, route="/api/health-hub", asof_date=asof_date, error_limit=error_limit)

    @app.get("/api/wait-signals", response_model=WaitSignalsResponse)
    def wait_signals(limit: int = Query(default=100, ge=1, le=500), status: str | None = None, symbol: str | None = None):
        return _guard(build_wait_signals_payload, route="/api/wait-signals", limit=limit, status=status, symbol=symbol)

    @app.post("/api/wait-signals/match", response_model=WaitSignalMatchResponse)
    def wait_signal_match(payload: dict[str, Any] = Body(default_factory=dict)):
        return _guard(run_wait_signal_match_payload, route="/api/wait-signals/match", payload=payload)

    @app.post("/api/hypotheses")
    def hypothesis_create(payload: dict[str, Any] = Body(...)):
        return _guard(create_hypothesis_payload, route="/api/hypotheses", payload=payload)

    @app.post("/api/hypotheses/preview")
    def hypothesis_preview(payload: dict[str, Any] = Body(...)):
        return _guard(preview_hypothesis_create_payload, route="/api/hypotheses/preview", payload=payload)

    @app.post("/api/hypotheses/{hypothesis_id}")
    def hypothesis_update(hypothesis_id: str, payload: dict[str, Any] = Body(...)):
        return _guard(update_hypothesis_payload, route="/api/hypotheses/{hypothesis_id}", hypothesis_id=hypothesis_id, payload=payload)

    @app.post("/api/hypotheses/{hypothesis_id}/promotion-audit")
    def hypothesis_promotion_audit(hypothesis_id: str, payload: dict[str, Any] = Body(default={})):
        return _guard(run_promotion_audit_payload, route="/api/hypotheses/{hypothesis_id}/promotion-audit", hypothesis_id=hypothesis_id, payload=payload)

    @app.post("/api/hypotheses/run")
    def hypothesis_run(payload: dict[str, Any] = Body(default={})):
        return _guard(run_hypothesis_payload, route="/api/hypotheses/run", payload=payload)

    return app


def _create_module_app():
    try:
        return create_app()
    except RuntimeError as exc:
        record_local_fallback_event(
            module="advisory.api.app",
            source="operator_api_create_app",
            fallback_type="operator_api_create_app_failed",
            severity="error",
            reason="Operator API app initialization failed; module-level app is unavailable until the configuration/runtime issue is fixed.",
            error=exc,
            metadata={},
        )
        return None


app = _create_module_app()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the read-only Stockey operator API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if app is None:
        raise SystemExit("FastAPI is required for the operator API. Install requirements.txt first.")
    import uvicorn

    uvicorn.run("advisory.api.app:app", host=args.host, port=args.port, reload=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
