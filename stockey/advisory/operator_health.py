from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pandas as pd
import requests
from environs import Env

from advisory.action_evidence_provenance import build_action_evidence_provenance
from advisory.action_transition_evaluator import DEFAULT_MIN_MATURED_ROWS as ACTION_TRANSITION_MIN_MATURED_ROWS
from advisory.action_transition_evaluator import EVALUATIONS_TABLE as ACTION_TRANSITION_EVALUATIONS_TABLE
from advisory.action_transition_evaluator import SUMMARY_TABLE as ACTION_TRANSITION_SUMMARY_TABLE
from advisory.adversarial_review_evaluator import DEFAULT_MIN_MATURED_ROWS as ADVERSARIAL_REVIEW_MIN_MATURED_ROWS
from advisory.adversarial_review_evaluator import EVALUATIONS_TABLE as ADVERSARIAL_REVIEW_EVALUATIONS_TABLE
from advisory.adversarial_review_evaluator import SUMMARY_TABLE as ADVERSARIAL_REVIEW_SUMMARY_TABLE
from advisory.causal_event_memory_evaluator import DEFAULT_MIN_MATURED_ROWS as CAUSAL_MEMORY_MIN_MATURED_ROWS
from advisory.causal_event_memory_evaluator import EVALUATIONS_TABLE as CAUSAL_MEMORY_EVALUATIONS_TABLE
from advisory.causal_event_memory_evaluator import SUMMARY_TABLE as CAUSAL_MEMORY_SUMMARY_TABLE
from advisory.causal_event_provenance import build_causal_event_provenance
from advisory.context_watch_evaluator import DEFAULT_MIN_MATURED_ROWS as CONTEXT_WATCH_MIN_MATURED_ROWS
from advisory.context_watch_evaluator import EVALUATIONS_TABLE as CONTEXT_WATCH_EVALUATIONS_TABLE
from advisory.context_watch_evaluator import SUMMARY_TABLE as CONTEXT_WATCH_SUMMARY_TABLE
from advisory.performance_slowlog import summarize_slow_operations
from advisory.operator_snapshot import DEFAULT_MAX_AGE_SECONDS as OPERATOR_SNAPSHOT_MAX_AGE_SECONDS
from advisory.operator_snapshot import SNAPSHOT_NAME, TABLE_NAME as OPERATOR_SNAPSHOT_TABLE
from advisory.event_data_quality import build_event_data_quality_health_summary, build_event_data_quality_report
from advisory.event_policy_evaluator import DEFAULT_MIN_MATURED_ROWS as EVENT_POLICY_MIN_MATURED_ROWS
from advisory.event_policy_evaluator import EVALUATIONS_TABLE as EVENT_POLICY_EVALUATIONS_TABLE
from advisory.event_policy_evaluator import SUMMARY_TABLE as EVENT_POLICY_SUMMARY_TABLE
from advisory.fallback_telemetry import read_local_fallback_events, record_local_fallback_event, summarize_fallback_events
from advisory.feature_freshness import STAGE_FEATURE_DEPENDENCIES_BY_STAGE, evaluate_stage_feature_gate
from advisory.identity_issues import IDENTITY_ISSUES_TABLE
from advisory.llm_provenance_audit import build_llm_provenance_audit
from advisory.macro_context_overlays import build_macro_context_overlays
from advisory.news_theme_engine import build_theme_context_overlays
from advisory.negative_pressure_evaluator import DEFAULT_MIN_MATURED_ROWS as NEGATIVE_PRESSURE_MIN_MATURED_ROWS
from advisory.negative_pressure_evaluator import EVALUATIONS_TABLE as NEGATIVE_PRESSURE_EVALUATIONS_TABLE
from advisory.negative_pressure_evaluator import SUMMARY_TABLE as NEGATIVE_PRESSURE_SUMMARY_TABLE
from advisory.context_overlay_reliability_report import RELIABILITY_SUMMARY_TABLE as CONTEXT_OVERLAY_RELIABILITY_SUMMARY_TABLE
from advisory.context_overlay_reliability_report import reliability_runtime_policy_contract
from advisory.recommendation_diagnostics import current_context_gate_policy_snapshot
from advisory.signal_quality_evaluator import EVALUATIONS_TABLE as SIGNAL_QUALITY_EVALUATIONS_TABLE
from advisory.signal_quality_evaluator import SUMMARY_TABLE as SIGNAL_QUALITY_SUMMARY_TABLE
from advisory.signal_quality_family_report import build_family_report as build_signal_quality_family_report
from advisory.signal_quality_promotion import REVIEWS_TABLE as SIGNAL_QUALITY_PROMOTION_REVIEWS_TABLE
from advisory.signal_quality_split_evaluator import DEFAULT_MIN_STABLE_WINDOWS as SIGNAL_QUALITY_SPLIT_MIN_STABLE_WINDOWS
from advisory.signal_quality_split_evaluator import SPLIT_EVALUATIONS_TABLE as SIGNAL_QUALITY_SPLIT_EVALUATIONS_TABLE
from advisory.signal_quality_split_evaluator import SPLIT_SUMMARY_TABLE as SIGNAL_QUALITY_SPLIT_SUMMARY_TABLE
from advisory.signal_quality_split_evaluator import build_split_stability_report
from advisory.setup_registry import load_signal_quality_overlay_rules
from advisory.superseded_failures import cleanup_superseded_failures
from advisory.technical_threshold_calibration import DEFAULT_MIN_SIGNALS as TECHNICAL_THRESHOLD_MIN_SIGNALS
from advisory.technical_threshold_calibration import EVALUATIONS_TABLE as TECHNICAL_THRESHOLD_EVALUATIONS_TABLE
from advisory.technical_threshold_calibration import SUMMARY_TABLE as TECHNICAL_THRESHOLD_SUMMARY_TABLE
from advisory.ts_forecast_paper_portfolio import PAPER_TABLE as TS_FORECAST_PAPER_TABLE
from scripts.advisory_stage_report import build_stage_report
from scripts.api_performance_report import build_api_performance_report
from data.screenerin.failure_log import FAILURES_TABLE as SCREENER_FAILURES_TABLE
from utils.db import read_db_retry_telemetry_events, sql_to_df
from utils.ingestion_state import classification_metadata as ingestion_classification_metadata
from utils.ingestion_state import get_state_entries as get_ingestion_state_entries
from utils.ingestion_state import summarize_state_entries as summarize_ingestion_state_entries
from utils.redaction import redact_mapping, redact_text
from utils.redis_utils import get_redis_client
from utils.schema_migrations import TABLE_NAME as SCHEMA_MIGRATIONS_TABLE


env = Env()
env.read_env()

DEFAULT_LOG_DIR = Path("logs/cron")
DEFAULT_LOG_TAIL_LINES = 80
DEFAULT_LOG_ANALYSIS_LINES = 5000
DEFAULT_OPERATOR_API_URL = "http://127.0.0.1:8765/api/health"
DEFAULT_OPERATOR_API_RUNTIME_URL = "http://127.0.0.1:8765/api/runtime"
DEFAULT_OPERATOR_WEB_URL = "http://127.0.0.1:3000/"
OPERATOR_API_ERRORS_TABLE = "advisory_operator_api_errors"
TRACE_SUMMARIES_TABLE = "advisory_trace_summaries"
ACTION_RECOMMENDATIONS_TABLE = "advisory_action_recommendations"
SIGNAL_REFRESH_ACTIONS_TABLE = "advisory_signal_refresh_actions"
REBALANCE_TABLE = "advisory_rebalance_actions"
LIFECYCLE_POLICY_CHANGES_TABLE = "advisory_lifecycle_policy_changes"
COMPANY_MASTER_TABLE = "company_master"
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
CONTEXT_SIGNAL_REFRESH_SOURCE_PREFIXES = (
    "announcement_context",
    "bhavcopy_context",
    "exchange_context",
    "macro_context",
    "theme_context",
)
BROKER_CAPABLE_ACTION_CODES = ("BUY", "BUY_MORE", "SELL", "PARTIAL_SELL")
DHAN_TOKEN_EXPIRY_WARN_SECONDS = 6 * 60 * 60
OPERATOR_HEALTH_FAST_WORKERS = env.int("OPERATOR_HEALTH_FAST_WORKERS", default=3)
OPERATOR_HEALTH_FULL_WORKERS = env.int("OPERATOR_HEALTH_FULL_WORKERS", default=4)
OPERATOR_HEALTH_FULL_PROCESS_ISOLATED = env.bool("OPERATOR_HEALTH_FULL_PROCESS_ISOLATED", default=True)
OPERATOR_HEALTH_FULL_SECTION_TIMEOUT_SECONDS = env.float("OPERATOR_HEALTH_FULL_SECTION_TIMEOUT_SECONDS", default=30.0)
OPERATOR_HEALTH_INGESTION_FAILURE_ACTIVE_DAYS = env.int("OPERATOR_HEALTH_INGESTION_FAILURE_ACTIVE_DAYS", default=30)
OPERATOR_HEALTH_FEATURE_STAGE_GATES_TIMEOUT_SECONDS = env.float("OPERATOR_HEALTH_FEATURE_STAGE_GATES_TIMEOUT_SECONDS", default=20.0)
OPERATOR_HEALTH_FEATURE_STAGE_GATE_SYMBOL_LIMIT = env.int("OPERATOR_HEALTH_FEATURE_STAGE_GATE_SYMBOL_LIMIT", default=5)
API_LATENCY_PROBE_OUTPUT_FILE = Path(env.str("API_LATENCY_PROBE_OUTPUT_FILE", "logs/performance/latest_api_latency_probe.json"))
API_LATENCY_PROBE_MAX_AGE_SECONDS = env.float("API_LATENCY_PROBE_MAX_AGE_SECONDS", default=24 * 60 * 60)
RESEARCH_EVIDENCE_LOG_MAX_BYTES = env.int("RESEARCH_EVIDENCE_LOG_MAX_BYTES", default=2_000_000)
SIGNAL_QUALITY_MAX_AGE_DAYS = env.float("SIGNAL_QUALITY_MAX_AGE_DAYS", default=14.0)
SIGNAL_QUALITY_MIN_MATURED_ROWS = env.int("SIGNAL_QUALITY_MIN_MATURED_ROWS", default=30)
SIGNAL_QUALITY_MIN_OVERLAY_ROWS = env.int("SIGNAL_QUALITY_MIN_OVERLAY_ROWS", default=10)
FEATURE_STAGE_GATE_SYMBOL_LIMIT = env.int("FEATURE_STAGE_GATE_SYMBOL_LIMIT", default=25)
ERROR_PATTERNS = [
    re.compile(r"traceback", re.IGNORECASE),
    re.compile(r"\bERROR\b"),
    re.compile(r"\bfailed\b(?!\s*=\s*0)", re.IGNORECASE),
    re.compile(r"\bexception\b", re.IGNORECASE),
    re.compile(r"connection refused", re.IGNORECASE),
    re.compile(r"\btimeout\b|timed out|statement timeout", re.IGNORECASE),
]
SUCCESS_PATTERNS = [
    re.compile(r'"status"\s*:\s*"ok"', re.IGNORECASE),
    re.compile(r"\bdone elapsed=", re.IGNORECASE),
    re.compile(r"\bpipeline done\b", re.IGNORECASE),
    re.compile(r"\bserver built\b", re.IGNORECASE),
    re.compile(r"\bstarted server process\b", re.IGNORECASE),
]
FAILURE_PATTERNS = [
    *ERROR_PATTERNS,
    re.compile(r'"status"\s*:\s*"failed"', re.IGNORECASE),
    re.compile(r"\bKeyboardInterrupt\b", re.IGNORECASE),
]
SCRIPT_MARKER_PATTERN = re.compile(
    r"\[stockey\.script\]\s+name=(?P<name>\S+)\s+status=(?P<status>\S+)(?:\s+exit_code=(?P<exit_code>\d+))?(?:\s+timestamp=(?P<timestamp>\S+))?",
    re.IGNORECASE,
)
DEGRADATION_PATTERNS = [
    {
        "kind": "dhan_master_miss",
        "severity": "error",
        "pattern": re.compile(r"No Dhan security id mapped for (?P<exchange>[A-Z]+):(?P<symbol>[A-Z0-9&\-.]+)", re.IGNORECASE),
        "title": "Symbol missing from Dhan master",
        "suggested_fix": "Refresh Dhan scrip master and verify NSE/BSE fallback mapping for the symbol.",
    },
    {
        "kind": "dhan_no_data",
        "severity": "warn",
        "pattern": re.compile(r"dhan_no_data_(?P<mode>retry|skip).*?'ticker': '?(?P<symbol>[A-Z0-9&\-.]+)'?", re.IGNORECASE),
        "title": "Dhan returned no OHLCV rows",
        "suggested_fix": "Check whether the symbol is suspended/newly listed, retry with BSE fallback, or reduce requested date window.",
    },
    {
        "kind": "dhan_auth_preflight_failed",
        "severity": "error",
        "pattern": re.compile(
            r"Dhan auth preflight failed|advisory_dhan_preflight|dhan_auth_preflight|connect_over_cdp|access token is invalid or expired|Client ID or user generated access token is invalid or expired",
            re.IGNORECASE,
        ),
        "title": "Dhan auth/CDP preflight failed",
        "suggested_fix": "Start Chrome CDP if needed, then run ./all_advisory_preflight.sh before retrying all_advisory.sh.",
    },
    {
        "kind": "llm_fallback",
        "severity": "warn",
        "pattern": re.compile(r"fallback_after_error|Deterministic fallback", re.IGNORECASE),
        "title": "LLM fallback was used",
        "suggested_fix": "Inspect the source row and model/provider logs; output may be deterministic fallback rather than LLM-reviewed.",
    },
    {
        "kind": "nse_retry",
        "severity": "warn",
        "pattern": re.compile(r"NSE .*retrying|NSE .*bootstrap failed|nseindia.*timed out", re.IGNORECASE),
        "title": "NSE request retry or bootstrap issue",
        "suggested_fix": "Let the retry loop continue; if persistent, clear NSE session/cookies and run complete_data again.",
    },
    {
        "kind": "db_reconnect",
        "severity": "warn",
        "pattern": re.compile(r"transient postgres error|server closed the connection|statement timeout", re.IGNORECASE),
        "title": "Postgres reconnect or timeout",
        "suggested_fix": "Check slow-operation log, DB connection limits, and whether the query should be paginated/materialized.",
    },
    {
        "kind": "redis_unavailable",
        "severity": "warn",
        "pattern": re.compile(r"redis unavailable|connection refused.*6379", re.IGNORECASE),
        "title": "Redis unavailable",
        "suggested_fix": "Start Redis or verify fail-soft mode; watcher pub/sub and state updates may be degraded.",
    },
    {
        "kind": "ocr_parse_failure",
        "severity": "warn",
        "pattern": re.compile(r"OCR/transcription failed|parse_status.*failed|poppler", re.IGNORECASE),
        "title": "Announcement OCR/parse failure",
        "suggested_fix": "Install Poppler or inspect the attachment; affected event summaries may be incomplete.",
    },
]


def _record_health_local_fallback(
    *,
    source: str,
    fallback_type: str,
    reason: str,
    error: Exception | str,
    severity: str = "error",
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="advisory.operator_health",
        source=source,
        fallback_type=fallback_type,
        severity=severity,
        reason=reason,
        error=error,
        metadata=metadata or {},
    )

TABLE_FRESHNESS_CHECKS = [
    {"name": "dhan_daily", "table": "dhan_ohlcv_daily", "column": "date", "max_age_days": 3},
    {"name": "actions", "table": "advisory_action_recommendations", "column": "asof_date", "max_age_days": 3},
    {"name": "portfolio", "table": "advisory_portfolio_orders", "column": "asof_date", "max_age_days": 7},
    {"name": "watch_alerts", "table": "advisory_live_watch_alerts", "column": "observed_at", "max_age_days": 3},
    {"name": "event_policy", "table": "advisory_event_policy_actions", "column": "policy_at", "fallback_column": "load_ts", "max_age_days": 7},
    {"name": "market_context", "table": "advisory_market_context_summary_daily", "column": "asof_date", "max_age_days": 7},
    {"name": "ts_forecasts", "table": "advisory_ts_forecasts_daily", "column": "asof_date", "max_age_days": 7},
    {"name": "ts_forecast_paper", "table": "advisory_ts_forecast_paper_portfolio", "column": "load_ts", "fallback_column": "asof_date", "max_age_days": 7},
    {"name": "sync_state", "table": "advisory_sync_state", "column": "updated_at", "max_age_days": 1},
    {"name": "operator_snapshot", "table": "advisory_operator_snapshots", "column": "generated_at", "max_age_days": 1},
    {"name": "signal_quality", "table": "advisory_signal_quality_eval_summary", "column": "evaluated_at", "max_age_days": 14},
    {"name": "context_overlay_reliability", "table": "advisory_context_overlay_reliability_summary", "column": "evaluated_at", "max_age_days": 14},
]
CRON_RECOVERY_OUTPUTS = {
    "all_advisory.log": [
        {"name": "actions", "table": "advisory_action_recommendations", "column": "load_ts", "fallback_column": "asof_date"},
        {"name": "portfolio", "table": "advisory_portfolio_orders", "column": "load_ts", "fallback_column": "asof_date"},
    ],
    "all_watchers.log": [
        {"name": "watch_alerts", "table": "advisory_live_watch_alerts", "column": "load_ts", "fallback_column": "observed_at"},
        {"name": "sync_state", "table": "advisory_sync_state", "column": "updated_at"},
    ],
    "complete_data.log": [
        {"name": "dhan_daily", "table": "dhan_ohlcv_daily", "column": "load_ts", "fallback_column": "date"},
        {"name": "sync_state", "table": "advisory_sync_state", "column": "updated_at"},
    ],
    "live_dashboard.log": [
        {"name": "actions", "table": "advisory_action_recommendations", "column": "load_ts", "fallback_column": "asof_date"},
    ],
}


def _status(severity: str, message: str, **extra: Any) -> dict[str, Any]:
    return {
        "status": severity,
        "message": message,
        **extra,
    }


def _json_ready(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    try:
        if pd.isna(value):
            return None
    except Exception as exc:
        _record_health_local_fallback(
            source="operator_health_payload",
            fallback_type="operator_health_json_ready_missing_check_failed",
            severity="warn",
            reason="Operator Health could not evaluate a value for missingness while preparing JSON output and kept the original value.",
            error=exc,
            metadata={"value_type": type(value).__name__},
        )
    return value


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df.empty:
        return []
    clean = df.copy().astype(object).where(pd.notna(df), None)
    return [{key: _json_ready(value) for key, value in row.items()} for row in clean.to_dict(orient="records")]


def _json_dict(
    value: Any,
    *,
    source: str | None = None,
    fallback_type: str = "operator_health_json_parse_failed",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            if source:
                _record_health_local_fallback(
                    source=source,
                    fallback_type=fallback_type,
                    severity="warn",
                    reason="Operator Health could not parse a stored JSON payload and used an empty object.",
                    error=exc,
                    metadata={
                        "value_length": len(value),
                        "value_excerpt": value[:240],
                        **(metadata or {}),
                    },
                )
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


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
        retries=2,
        statement_timeout_ms=5000,
    )
    return not df.empty


def table_columns(table_name: str) -> set[str]:
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
    return set(df["column_name"].astype(str).tolist()) if not df.empty else set()


def check_database() -> dict[str, Any]:
    try:
        df = sql_to_df("SELECT now() AS db_now", retries=2, statement_timeout_ms=5000)
        db_now = df.iloc[0]["db_now"] if not df.empty else None
        return _status("ok", "Postgres query succeeded.", db_now=_json_ready(pd.to_datetime(db_now, utc=True, errors="coerce")))
    except Exception as exc:
        _record_health_local_fallback(
            source="postgres",
            fallback_type="operator_health_database_check_failed",
            reason="Operator Health could not run the basic Postgres connectivity check.",
            error=exc,
        )
        return _status("error", "Postgres query failed.", error=f"{type(exc).__name__}: {exc}")


def check_operator_api() -> dict[str, Any]:
    url = env("OPERATOR_API_HEALTH_URL", default=DEFAULT_OPERATOR_API_URL)
    timeout_seconds = env.float("OPERATOR_API_HEALTH_TIMEOUT_SECONDS", default=3.0)
    started = time.monotonic()
    try:
        response = requests.get(url, timeout=timeout_seconds)
        latency_ms = round((time.monotonic() - started) * 1000.0, 2)
        payload = response.json() if response.headers.get("content-type", "").lower().startswith("application/json") else {}
        if response.ok and isinstance(payload, dict) and payload.get("status") == "ok":
            return _status("ok", "Operator API health endpoint responded.", url=url, latency_ms=latency_ms, status_code=response.status_code)
        return _status(
            "error",
            "Operator API health endpoint returned an unhealthy response.",
            url=url,
            latency_ms=latency_ms,
            status_code=response.status_code,
            response_text=response.text[:500],
        )
    except Exception as exc:
        latency_ms = round((time.monotonic() - started) * 1000.0, 2)
        _record_health_local_fallback(
            source=url,
            fallback_type="operator_health_api_check_failed",
            reason="Operator Health could not reach the operator API health endpoint.",
            error=exc,
            metadata={"url": url, "latency_ms": latency_ms},
        )
        return _status("error", "Operator API health endpoint is not reachable.", url=url, latency_ms=latency_ms, error=f"{type(exc).__name__}: {exc}")


def check_operator_api_runtime() -> dict[str, Any]:
    url = env("OPERATOR_API_RUNTIME_URL", default=DEFAULT_OPERATOR_API_RUNTIME_URL)
    timeout_seconds = env.float("OPERATOR_API_HEALTH_TIMEOUT_SECONDS", default=3.0)
    started = time.monotonic()
    try:
        response = requests.get(url, timeout=timeout_seconds)
        latency_ms = round((time.monotonic() - started) * 1000.0, 2)
        payload = response.json() if response.headers.get("content-type", "").lower().startswith("application/json") else {}
        if not response.ok or not isinstance(payload, dict):
            return _status(
                "error",
                "Operator API runtime endpoint returned an unhealthy response.",
                url=url,
                latency_ms=latency_ms,
                status_code=response.status_code,
                response_text=response.text[:500],
            )
        if payload.get("stale_code"):
            return _status(
                "error",
                "Operator API is serving stale code.",
                url=url,
                latency_ms=latency_ms,
                status_code=response.status_code,
                stale_code=True,
                stale_reason=payload.get("stale_reason"),
                operator_action=payload.get("operator_action") or "restart_operator_api",
                process_started_at=payload.get("process_started_at"),
                latest_source_mtime=payload.get("latest_source_mtime"),
                latest_source_path=payload.get("latest_source_path"),
                git_rev=payload.get("git_rev"),
                git_dirty=payload.get("git_dirty"),
            )
        return _status(
            "ok",
            "Operator API runtime is current.",
            url=url,
            latency_ms=latency_ms,
            status_code=response.status_code,
            stale_code=False,
            process_started_at=payload.get("process_started_at"),
            latest_source_mtime=payload.get("latest_source_mtime"),
            latest_source_path=payload.get("latest_source_path"),
            git_rev=payload.get("git_rev"),
            git_dirty=payload.get("git_dirty"),
        )
    except Exception as exc:
        latency_ms = round((time.monotonic() - started) * 1000.0, 2)
        _record_health_local_fallback(
            source=url,
            fallback_type="operator_health_api_runtime_check_failed",
            reason="Operator Health could not inspect the Operator API runtime metadata.",
            error=exc,
            metadata={"url": url, "latency_ms": latency_ms},
        )
        return _status("error", "Operator API runtime endpoint is not reachable.", url=url, latency_ms=latency_ms, error=f"{type(exc).__name__}: {exc}")


def check_api_latency_probe(path: str | Path = API_LATENCY_PROBE_OUTPUT_FILE, *, max_age_seconds: float = API_LATENCY_PROBE_MAX_AGE_SECONDS) -> dict[str, Any]:
    probe_path = Path(path)
    probe_command = f"python scripts/api_latency_probe.py --output-path {probe_path}"
    performance_report_command = "python scripts/api_performance_report.py --limit 20"
    if not probe_path.exists():
        return _status(
            "warn",
            "API latency probe has not produced a latest summary yet.",
            path=str(probe_path),
            command=probe_command,
            performance_report_command=performance_report_command,
        )
    try:
        payload = json.loads(probe_path.read_text(encoding="utf-8"))
    except Exception as exc:
        _record_health_local_fallback(
            source=str(probe_path),
            fallback_type="operator_health_api_latency_probe_unreadable",
            reason="Operator Health could not read the latest API latency probe summary.",
            error=exc,
            severity="warn",
            metadata={"path": str(probe_path)},
        )
        return _status("error", "API latency probe summary is unreadable.", path=str(probe_path), error=f"{type(exc).__name__}: {exc}")
    generated_at = pd.to_datetime(payload.get("generated_at"), utc=True, errors="coerce")
    age_seconds = None
    if generated_at is not None and not pd.isna(generated_at):
        age_seconds = max(0.0, (pd.Timestamp.now(tz="UTC") - generated_at).total_seconds())
    stale = age_seconds is None or age_seconds > float(max_age_seconds)
    slow_count = int(payload.get("slow_count") or 0)
    error_count = int(payload.get("error_count") or 0)
    if error_count > 0:
        status = "error"
        message = "API latency probe found endpoint errors."
    elif slow_count > 0 or stale:
        status = "warn"
        message = "API latency probe found slow endpoints or stale probe data."
    else:
        status = "ok"
        message = "API latency probe is current and healthy."
    return _status(
        status,
        message,
        path=str(probe_path),
        generated_at=None if pd.isna(generated_at) else generated_at.isoformat(),
        age_seconds=None if age_seconds is None else round(age_seconds, 2),
        max_age_seconds=float(max_age_seconds),
        slow_count=slow_count,
        error_count=error_count,
        endpoint_count=int(payload.get("endpoint_count") or 0),
        rows=payload.get("rows") or [],
        command=probe_command,
        performance_report_command=performance_report_command,
        operator_action=(
            f"Run `{probe_command}`, then `{performance_report_command}` to rank current API fixes."
            if status in {"warn", "error"}
            else f"Run `{performance_report_command}` after normal UI traffic to rank any current API fixes."
        ),
    )


def check_slow_operations(
    *,
    limit: int = 20,
    state_file: str | Path | None = None,
    probe_path: str | Path = API_LATENCY_PROBE_OUTPUT_FILE,
    max_probe_age_seconds: float = API_LATENCY_PROBE_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    slow_kwargs: dict[str, Any] = {"limit": limit}
    if state_file is not None:
        slow_kwargs["state_file"] = state_file
    slow = summarize_slow_operations(**slow_kwargs)
    try:
        report_kwargs: dict[str, Any] = {
            "probe_path": probe_path,
            "limit": limit,
            "max_probe_age_hours": max(float(max_probe_age_seconds), 0.0) / 3600.0,
        }
        if state_file is not None:
            report_kwargs["state_file"] = state_file
        report = build_api_performance_report(**report_kwargs)
    except Exception as exc:
        _record_health_local_fallback(
            source=str(probe_path),
            fallback_type="operator_health_slow_operations_probe_context_failed",
            reason="Operator Health could not enrich slow-operation state with API probe context.",
            error=exc,
            severity="warn",
            metadata={"probe_path": str(probe_path), "limit": int(limit)},
        )
        slow["message"] = "Open slow-operation issues exist; API probe context could not be loaded."
        slow["active_issue_count"] = int(slow.get("returned_count") or 0)
        slow["historical_issue_count"] = 0
        slow["probe_status"] = "unknown"
        return slow

    evidence = report.get("evidence_freshness") if isinstance(report.get("evidence_freshness"), dict) else {}
    probe_status = str(evidence.get("probe_status") or "unknown").lower()
    report_rows = [row for row in report.get("rows") or [] if isinstance(row, dict)]
    active_routes = {
        str(row.get("route") or "").split("?", 1)[0]
        for row in report_rows
        if str(row.get("ranking_basis") or "") == "fresh_probe"
        and (
            bool(row.get("latest_probe_slow_logged"))
            or bool(row.get("slow_logged"))
            or bool(row.get("latest_probe_error"))
            or float(row.get("latest_probe_elapsed_ms") or 0.0) >= 1000.0
            or int(row.get("latest_probe_error_count") or 0) > 0
            or int(row.get("error_count") or 0) > 0
        )
    }
    if probe_status == "fresh":
        active_issues = []
        historical_issues = []
        for issue in slow.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            operation = str(issue.get("operation") or "")
            route = operation.split(" ", 1)[1].split("?", 1)[0] if operation.upper().startswith("GET ") else operation.split("?", 1)[0]
            if route in active_routes:
                active_issues.append(issue)
            else:
                historical_issues.append({**issue, "historical_context": True})
        slow["issues"] = active_issues
        slow["historical_issues"] = historical_issues[: max(int(limit), 0)]
        slow["active_issue_count"] = len(active_issues)
        slow["historical_issue_count"] = len(historical_issues)
        slow["probe_status"] = probe_status
        slow["probe_generated_at"] = report.get("probe_generated_at")
        slow["performance_report_command"] = "python scripts/api_performance_report.py --limit 20"
        if active_issues:
            slow["status"] = "warn"
            slow["message"] = "Fresh API probe still reproduces slow or failing routes."
        else:
            slow["status"] = "ok"
            slow["message"] = "Historical slow-operation rows exist, but the latest API probe did not reproduce active slow routes."
        return slow

    slow["probe_status"] = probe_status
    slow["probe_generated_at"] = report.get("probe_generated_at")
    slow["performance_report_command"] = "python scripts/api_performance_report.py --limit 20"
    slow["active_issue_count"] = int(slow.get("returned_count") or 0)
    slow["historical_issue_count"] = 0
    slow["message"] = str(evidence.get("warning") or "Open slow-operation issues exist and probe freshness is not current enough to suppress them.")
    return slow


def check_advisory_stage_report(log_dir: str | Path = DEFAULT_LOG_DIR) -> dict[str, Any]:
    log_path = Path(log_dir) / "all_advisory.log"
    command = f"python scripts/advisory_stage_report.py --log-path {log_path} --limit 20"
    try:
        report = build_stage_report(log_path=log_path, limit=20)
    except Exception as exc:
        _record_health_local_fallback(
            source=str(log_path),
            fallback_type="operator_health_advisory_stage_report_failed",
            reason="Operator Health could not build the advisory stage timing report.",
            error=exc,
            severity="warn",
            metadata={"log_path": str(log_path)},
        )
        return _status(
            "error",
            "Could not build advisory stage timing report.",
            error=f"{type(exc).__name__}: {exc}",
            log_path=str(log_path),
            command=command,
            operations_command="advisory_stage_report",
        )

    stage_count = int(report.get("stage_count") or 0)
    slow_stage_count = int(report.get("slow_stage_count") or 0)
    degraded_stage_count = int(report.get("degraded_stage_count") or 0)
    report_status = str(report.get("status") or "missing")
    report_payload = dict(report)
    report_payload["report_status"] = report_payload.pop("status", report_status)
    status = "ok"
    message = "Latest advisory stage timing report is available and no stage is over budget."
    failed_stage = str(report.get("failed_stage") or "").strip()
    if report_status == "failed" and stage_count:
        status = "warn"
        message = (
            f"Latest advisory run failed in or near stage {failed_stage}."
            if failed_stage
            else "Latest advisory run failed after emitting stage markers."
        )
    elif report_status == "running" and stage_count:
        status = "warn"
        message = (
            f"Latest advisory run is still running or incomplete near stage {failed_stage}."
            if failed_stage
            else "Latest advisory run is still running or incomplete after emitting stage markers."
        )
    elif report_status != "ok" or stage_count == 0:
        status = "warn"
        message = "No usable advisory stage timing summary was found in the latest all_advisory log."
    elif slow_stage_count:
        status = "warn"
        message = "Latest advisory run has one or more over-budget stages."
    elif degraded_stage_count:
        status = "warn"
        message = "Latest advisory run completed with degraded stage evidence."

    return _status(
        status,
        message,
        **report_payload,
        command=command,
        operations_command="advisory_stage_report",
    )


def _read_log_tail(path: Path, *, max_bytes: int) -> str:
    if not path.exists():
        return ""
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - max(int(max_bytes), 0)))
            data = handle.read()
    except OSError as exc:
        _record_health_local_fallback(
            source=str(path),
            fallback_type="operator_health_log_tail_read_failed",
            reason="Operator Health could not read a cron log tail.",
            error=exc,
            severity="warn",
            metadata={"path": str(path), "max_bytes": max_bytes},
        )
        return ""
    return data.decode("utf-8", errors="replace")


def _candidate_json_objects_from_mixed_text(text: str, *, source: str, max_failures: int = 3) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    failures = 0
    for idx, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError as exc:
            if failures < max_failures:
                _record_health_local_fallback(
                    source=source,
                    fallback_type="operator_health_candidate_json_parse_failed",
                    reason="Operator Health skipped a non-JSON brace while scanning mixed cron log text.",
                    error=exc,
                    severity="warn",
                    metadata={"offset": idx, "source": source},
                )
                failures += 1
            continue
        if isinstance(value, dict):
            objects.append(value)
    return objects


def _extract_latest_research_evidence_payload(text: str, *, source: str) -> dict[str, Any]:
    candidates = []
    for payload in _candidate_json_objects_from_mixed_text(text, source=source):
        evidence = payload.get("research_evidence")
        if isinstance(evidence, dict) and isinstance(evidence.get("readiness_summary"), dict):
            candidates.append(payload)
    return candidates[-1] if candidates else {}


def check_research_evidence_run_summary(log_dir: str | Path = DEFAULT_LOG_DIR) -> dict[str, Any]:
    log_path = Path(log_dir) / "research_evidence.log"
    command = "./all_research_evidence.sh"
    health_command = "python -m advisory.operator_health --full --skip-dhan"
    common = {
        "log_path": str(log_path),
        "command": command,
        "health_command": health_command,
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        "portfolio_mutation_allowed": False,
        "authority": "research_only_no_policy_or_broker_authority",
        "operations_command": "run_research_evidence",
    }
    if not log_path.exists():
        return _status(
            "warn",
            "Research evidence refresh log is missing.",
            usable=False,
            ready_for_research_review=False,
            reasons=["missing_research_evidence_log"],
            **common,
        )

    mtime = pd.Timestamp.fromtimestamp(log_path.stat().st_mtime, tz="UTC")
    text = _read_log_tail(log_path, max_bytes=RESEARCH_EVIDENCE_LOG_MAX_BYTES)
    if not text.strip():
        return _status(
            "warn",
            "Research evidence refresh log is empty.",
            usable=False,
            ready_for_research_review=False,
            reasons=["empty_research_evidence_log"],
            latest_log_mtime=_json_ready(mtime),
            **common,
        )

    payload = _extract_latest_research_evidence_payload(text, source=str(log_path))
    if not payload:
        return _status(
            "warn",
            "Research evidence refresh log does not contain a parseable readiness summary.",
            usable=False,
            ready_for_research_review=False,
            reasons=["missing_research_evidence_readiness_summary"],
            latest_log_mtime=_json_ready(mtime),
            **common,
        )

    evidence = payload.get("research_evidence") if isinstance(payload.get("research_evidence"), dict) else {}
    readiness = evidence.get("readiness_summary") if isinstance(evidence.get("readiness_summary"), dict) else {}
    readiness_status = str(readiness.get("status") or "unknown")
    payload_status = str(payload.get("status") or "unknown")
    component_status = evidence.get("component_status") if isinstance(evidence.get("component_status"), list) else []
    blocker_components = readiness.get("blocker_components") if isinstance(readiness.get("blocker_components"), list) else []
    candidate_components = readiness.get("candidate_components") if isinstance(readiness.get("candidate_components"), list) else []
    authority_violation_count = int(readiness.get("authority_violation_count") or 0)
    authority_violation_components = (
        readiness.get("authority_violation_components")
        if isinstance(readiness.get("authority_violation_components"), list)
        else []
    )
    active_component_count = int(readiness.get("active_component_count") or 0)
    skipped_component_count = int(readiness.get("skipped_component_count") or 0)
    candidate_component_count = int(readiness.get("candidate_component_count") or 0)
    blocker_count = int(readiness.get("blocker_count") or len(blocker_components or []))
    reasons: list[str] = []

    if payload_status not in {"ok", "success", "unknown"}:
        status = "error"
        reasons.append("research_evidence_runner_failed")
        message = "Research evidence refresh failed."
        usable = False
        ready_for_research_review = False
    elif readiness_status == "candidate_evidence_available":
        status = "ok"
        message = "Research evidence refresh is available for research review."
        usable = True
        ready_for_research_review = True
    elif readiness_status == "all_skipped":
        status = "warn"
        reasons.append("research_evidence_all_components_skipped")
        message = "Research evidence refresh skipped every evidence component."
        usable = False
        ready_for_research_review = False
    elif readiness_status == "not_ready":
        status = "warn"
        reasons.append("research_evidence_not_ready")
        message = "Research evidence refresh ran, but evidence is not ready for review."
        usable = False
        ready_for_research_review = False
    elif readiness_status == "monitor":
        status = "warn"
        reasons.append("research_evidence_monitor_only")
        message = "Research evidence refresh produced monitor-only evidence."
        usable = False
        ready_for_research_review = False
    elif readiness_status == "error":
        status = "error"
        reasons.append("research_evidence_readiness_error")
        message = "Research evidence readiness summary reports an error."
        usable = False
        ready_for_research_review = False
    else:
        status = "warn"
        reasons.append("unknown_research_evidence_readiness_status")
        message = "Research evidence refresh has an unknown readiness status."
        usable = False
        ready_for_research_review = False

    if (
        bool(readiness.get("broker_execution_allowed"))
        or bool(readiness.get("policy_auto_promotion_allowed"))
        or bool(readiness.get("portfolio_mutation_allowed"))
        or authority_violation_count > 0
    ):
        status = "error"
        reasons.append("research_evidence_authority_boundary_violation")
        message = "Research evidence readiness claims policy, portfolio, or broker authority; this violates the research-only contract."
        usable = False
        ready_for_research_review = False

    return _status(
        status,
        message,
        usable=usable,
        ready_for_research_review=ready_for_research_review,
        reasons=list(dict.fromkeys(reasons)),
        latest_log_mtime=_json_ready(mtime),
        payload_status=payload_status,
        readiness_status=readiness_status,
        active_component_count=active_component_count,
        skipped_component_count=skipped_component_count,
        candidate_component_count=candidate_component_count,
        blocker_count=blocker_count,
        candidate_components=candidate_components,
        blocker_components=blocker_components[:10],
        authority_violation_count=authority_violation_count,
        authority_violation_components=authority_violation_components[:10],
        component_count=len(component_status),
        manual_review_rows_may_be_created=bool(evidence.get("manual_review_rows_may_be_created")),
        manual_review_row_reason=evidence.get("manual_review_row_reason"),
        **common,
    )


def check_table_freshness(now: pd.Timestamp | None = None, *, include_counts: bool = True) -> list[dict[str, Any]]:
    effective_now = pd.to_datetime(now or pd.Timestamp.utcnow(), utc=True, errors="coerce")
    rows: list[dict[str, Any]] = []
    for spec in TABLE_FRESHNESS_CHECKS:
        table = str(spec["table"])
        column = str(spec["column"])
        fallback_column = str(spec.get("fallback_column") or column)
        max_age_days = float(spec["max_age_days"])
        try:
            if not table_exists(table):
                rows.append(_status("warn", "Table does not exist yet.", name=spec["name"], table=table))
                continue
            columns = table_columns(table)
            effective_column = column if column in columns else fallback_column if fallback_column in columns else None
            if effective_column is None:
                rows.append(_status("warn", "Freshness column is missing.", name=spec["name"], table=table, column=column))
                continue
            count_sql = ', COUNT(*) AS row_count' if include_counts else ''
            df = sql_to_df(
                f'SELECT MAX("{effective_column}") AS latest_at{count_sql} FROM "{table}"',
                retries=2,
                statement_timeout_ms=10000,
            )
            latest_at = pd.to_datetime(df.iloc[0]["latest_at"], utc=True, errors="coerce") if not df.empty else pd.NaT
            row_count = int(df.iloc[0]["row_count"] or 0) if include_counts and not df.empty else None
            if pd.isna(latest_at):
                rows.append(_status("warn", "Table has no timestamped rows.", name=spec["name"], table=table, row_count=row_count))
                continue
            age_hours = max((effective_now - latest_at).total_seconds() / 3600.0, 0.0)
            severity = "ok" if age_hours <= max_age_days * 24.0 else "warn"
            rows.append(
                _status(
                    severity,
                    "Fresh enough." if severity == "ok" else "Stale relative to configured threshold.",
                    name=spec["name"],
                    table=table,
                    column=effective_column,
                    latest_at=_json_ready(latest_at),
                    age_hours=round(age_hours, 2),
                    max_age_hours=round(max_age_days * 24.0, 2),
                    row_count=row_count,
                )
            )
        except Exception as exc:
            _record_health_local_fallback(
                source=table,
                fallback_type="operator_health_table_freshness_check_failed",
                reason="Operator Health could not inspect table freshness.",
                error=exc,
                metadata={
                    "name": spec["name"],
                    "table": table,
                    "column": column,
                    "fallback_column": fallback_column,
                    "include_counts": bool(include_counts),
                },
            )
            rows.append(_status("error", "Freshness check failed.", name=spec["name"], table=table, error=f"{type(exc).__name__}: {exc}"))
    return rows


def get_table_latest_timestamp(table: str, column: str, fallback_column: str | None = None) -> dict[str, Any]:
    if not table_exists(table):
        return _status("warn", "Table does not exist yet.", table=table, column=column)
    columns = table_columns(table)
    effective_column = column if column in columns else fallback_column if fallback_column and fallback_column in columns else None
    if effective_column is None:
        return _status("warn", "Freshness column is missing.", table=table, column=column, fallback_column=fallback_column)
    df = sql_to_df(
        f'SELECT MAX("{effective_column}") AS latest_at, COUNT(*) AS row_count FROM "{table}"',
        retries=2,
        statement_timeout_ms=10000,
    )
    latest_at = pd.to_datetime(df.iloc[0]["latest_at"], utc=True, errors="coerce") if not df.empty else pd.NaT
    return _status(
        "ok" if pd.notna(latest_at) else "warn",
        "Latest timestamp loaded." if pd.notna(latest_at) else "Table has no timestamped rows.",
        table=table,
        column=effective_column,
        latest_at=_json_ready(latest_at),
        row_count=int(df.iloc[0]["row_count"] or 0) if not df.empty else 0,
    )


def check_redis() -> dict[str, Any]:
    host = env("REDIS_HOST", default="")
    port = env("REDIS_PORT", default="")
    if not host or not port:
        return _status("warn", "Redis host/port not configured.", host=host, port=port)
    try:
        client = get_redis_client(host=host, port=port, decode_responses=True, fail_soft=False)
        pong = client.ping()
        client.close()
        return _status("ok", "Redis ping succeeded.", host=host, port=port, pong=bool(pong))
    except Exception as exc:
        _record_health_local_fallback(
            source="redis",
            fallback_type="operator_health_redis_check_failed",
            reason="Operator Health could not ping Redis; runtime may continue only if Redis fail-soft is enabled.",
            error=exc,
            severity="warn",
            metadata={"host": host, "port": port},
        )
        return _status("warn", "Redis ping failed. Runtime can continue if Redis fail-soft is enabled.", host=host, port=port, error=f"{type(exc).__name__}: {exc}")


def check_operator_snapshot() -> dict[str, Any]:
    try:
        if not table_exists(OPERATOR_SNAPSHOT_TABLE):
            return _status("warn", "Operator snapshot table does not exist yet.", table=OPERATOR_SNAPSHOT_TABLE)
        df = sql_to_df(
            f"""
            SELECT snapshot_key, generated_at, payload_bytes, section_counts_json
            FROM {OPERATOR_SNAPSHOT_TABLE}
            WHERE snapshot_name = %s
            ORDER BY generated_at DESC
            LIMIT 1
            """,
            params=(SNAPSHOT_NAME,),
            retries=2,
            statement_timeout_ms=10000,
        )
        if df.empty:
            return _status("warn", "No operator snapshot rows exist yet.", table=OPERATOR_SNAPSHOT_TABLE)
        row = df.iloc[0]
        generated_at = pd.to_datetime(row.get("generated_at"), utc=True, errors="coerce")
        age_seconds = None if pd.isna(generated_at) else max((pd.Timestamp.utcnow() - generated_at).total_seconds(), 0.0)
        max_age_seconds = int(OPERATOR_SNAPSHOT_MAX_AGE_SECONDS)
        status = "ok" if age_seconds is not None and (max_age_seconds <= 0 or age_seconds <= max_age_seconds) else "warn"
        return _status(
            status,
            "Operator snapshot is fresh enough." if status == "ok" else "Operator snapshot is missing or stale; API will fall back to the live builder if enabled.",
            table=OPERATOR_SNAPSHOT_TABLE,
            snapshot_key=str(row.get("snapshot_key") or ""),
            generated_at=_json_ready(generated_at),
            age_seconds=None if age_seconds is None else round(age_seconds, 2),
            max_age_seconds=max_age_seconds,
            payload_bytes=int(row.get("payload_bytes") or 0),
            section_counts_json=str(row.get("section_counts_json") or "{}")[:1000],
        )
    except Exception as exc:
        _record_health_local_fallback(
            source=OPERATOR_SNAPSHOT_TABLE,
            fallback_type="operator_health_snapshot_check_failed",
            reason="Operator Health could not inspect the operator snapshot table.",
            error=exc,
            metadata={"snapshot_name": SNAPSHOT_NAME},
        )
        return _status("error", "Operator snapshot health check failed.", table=OPERATOR_SNAPSHOT_TABLE, error=f"{type(exc).__name__}: {exc}")


def check_sync_state_failures(limit: int = 25) -> list[dict[str, Any]]:
    try:
        if not table_exists("advisory_sync_state"):
            return [_status("warn", "Sync-state table does not exist yet.", table="advisory_sync_state")]
        df = sql_to_df(
            """
            SELECT source_name, scope_key, status, error_text, updated_at, last_success_at, state_json
            FROM advisory_sync_state
            WHERE COALESCE(status, '') NOT IN ('', 'ok', 'skipped')
               OR error_text IS NOT NULL
            ORDER BY updated_at DESC NULLS LAST
            LIMIT %s
            """,
            params=(int(limit),),
            retries=2,
            statement_timeout_ms=10000,
        )
        if df.empty:
            return [_status("ok", "No failed sync-state rows found.", table="advisory_sync_state")]
        rows = []
        for _, row in df.iterrows():
            rows.append(
                _status(
                    "error" if str(row.get("status") or "").lower() == "error" else "warn",
                    "A sync/download/parser cycle has a non-ok status.",
                    source_name=row.get("source_name"),
                    scope_key=row.get("scope_key"),
                    sync_status=row.get("status"),
                    error=row.get("error_text"),
                    updated_at=_json_ready(pd.to_datetime(row.get("updated_at"), utc=True, errors="coerce")),
                    last_success_at=_json_ready(pd.to_datetime(row.get("last_success_at"), utc=True, errors="coerce")),
                    state_json=str(row.get("state_json") or "{}")[:1000],
                )
            )
        return rows
    except Exception as exc:
        _record_health_local_fallback(
            source="advisory_sync_state",
            fallback_type="operator_health_sync_state_check_failed",
            reason="Operator Health could not inspect sync-state failures.",
            error=exc,
            metadata={"limit": int(limit)},
        )
        return [_status("error", "Sync-state failure check failed.", table="advisory_sync_state", error=f"{type(exc).__name__}: {exc}")]


def check_watcher_source_counters(max_age_minutes: int = 90) -> dict[str, Any]:
    expected_sources = {
        "continuous_watch:ohlcv": "ohlcv",
        "continuous_watch:news": "news",
        "continuous_watch:announcements": "announcements",
    }
    try:
        if not table_exists("advisory_sync_state"):
            return _status(
                "warn",
                "Sync-state table does not exist yet; watcher source counters are unavailable.",
                table="advisory_sync_state",
                rows=[],
                missing_sources=sorted(expected_sources),
            )
        df = sql_to_df(
            """
            SELECT source_name, scope_key, status, error_text, updated_at, last_success_at, state_json
            FROM advisory_sync_state
            WHERE source_name = ANY(%s)
            ORDER BY source_name, updated_at DESC NULLS LAST
            """,
            params=(list(expected_sources.keys()),),
            retries=2,
            statement_timeout_ms=10000,
        )
        latest: dict[str, dict[str, Any]] = {}
        for row in _records(df):
            source_name = str(row.get("source_name") or "")
            if source_name and source_name not in latest:
                latest[source_name] = row

        now = pd.Timestamp.utcnow()
        rows: list[dict[str, Any]] = []
        missing_sources: list[str] = []
        stale_count = 0
        error_count = 0
        empty_count = 0
        degraded_count = 0
        for source_name, short_name in expected_sources.items():
            row = latest.get(source_name)
            if not row:
                missing_sources.append(source_name)
                rows.append(
                    _status(
                        "warn",
                        f"No watcher sync-state row found for {short_name}.",
                        source_name=source_name,
                        watcher_source=short_name,
                        counters={},
                    )
                )
                continue
            state = _json_dict(
                row.get("state_json"),
                source=f"{source_name}:state_json",
                metadata={"source_name": source_name},
            )
            counters = state.get("source_counters") if isinstance(state.get("source_counters"), dict) else {}
            updated_at = pd.to_datetime(row.get("updated_at"), utc=True, errors="coerce")
            last_success_at = pd.to_datetime(row.get("last_success_at"), utc=True, errors="coerce")
            age_minutes = None if pd.isna(updated_at) else max((now - updated_at).total_seconds() / 60.0, 0.0)
            status = str(row.get("status") or "").lower()
            is_stale = age_minutes is not None and age_minutes > float(max_age_minutes)
            has_error = status == "error" or bool(row.get("error_text"))
            produced = any(
                int(counters.get(key) or 0) > 0
                for key in (
                    "latest_price_count",
                    "alert_persisted_count",
                    "matched_event_count",
                    "match_count",
                    "persisted_event_count",
                    "discovered_count",
                    "parsed_count",
                )
            )
            degraded_evidence_count = sum(
                int(counters.get(key) or 0)
                for key in (
                    "ingest_issue_count",
                    "unresolved_ingest_target_count",
                    "unresolved_watch_row_count",
                    "managed_ingest_failed_count",
                    "document_lookup_failed_count",
                )
            ) if isinstance(counters, dict) else 0
            if has_error:
                row_status = "error"
                error_count += 1
            elif is_stale:
                row_status = "warn"
                stale_count += 1
            elif degraded_evidence_count > 0:
                row_status = "warn"
                degraded_count += 1
            elif not counters:
                row_status = "warn"
                empty_count += 1
            else:
                row_status = "ok"
            rows.append(
                _status(
                    row_status,
                    "Latest OHLCV/news/announcement watcher counters loaded."
                    if counters
                    else "Watcher row has no source counters yet.",
                    source_name=source_name,
                    watcher_source=short_name,
                    sync_status=row.get("status"),
                    updated_at=_json_ready(updated_at),
                    last_success_at=_json_ready(last_success_at),
                    age_minutes=None if age_minutes is None else round(age_minutes, 2),
                    stale=is_stale,
                    produced_data=bool(produced),
                    degraded_evidence_count=degraded_evidence_count,
                    empty_reason=counters.get("empty_reason") if isinstance(counters, dict) else None,
                    substeps=counters.get("substeps") if isinstance(counters, dict) else [],
                    error=row.get("error_text"),
                    counters=counters,
                )
            )
        status = "error" if error_count else "warn" if (stale_count or degraded_count or empty_count or missing_sources) else "ok"
        return _status(
            status,
            "Latest OHLCV/news/announcement watcher counters loaded.",
            table="advisory_sync_state",
            rows=rows,
            returned_count=len(rows),
            missing_sources=missing_sources,
            stale_count=stale_count,
            error_count=error_count,
            degraded_counter_count=degraded_count,
            empty_counter_count=empty_count,
            max_age_minutes=int(max_age_minutes),
        )
    except Exception as exc:
        _record_health_local_fallback(
            source="advisory_sync_state",
            fallback_type="operator_health_watcher_source_counters_failed",
            reason="Operator Health could not inspect watcher source counters.",
            error=exc,
            metadata={"max_age_minutes": int(max_age_minutes)},
        )
        return _status(
            "error",
            "Watcher source counter check failed.",
            table="advisory_sync_state",
            error=f"{type(exc).__name__}: {exc}",
            rows=[],
            returned_count=0,
        )


def check_signal_refresh_source_state(max_age_minutes: int = 90) -> dict[str, Any]:
    from advisory.signal_refresh import CONTEXT_OVERLAY_SIGNAL_REFRESH_VERSION

    expected_sources = {
        "advisory:signal_refresh:context_overlays": "context_overlays",
        "advisory:signal_refresh:causal_memory": "causal_memory",
        "continuous_watch:action_refresh": "action_refresh",
    }
    try:
        if not table_exists("advisory_sync_state"):
            return _status(
                "warn",
                "Sync-state table does not exist yet; signal-refresh source state is unavailable.",
                table="advisory_sync_state",
                rows=[],
                missing_sources=sorted(expected_sources),
                stale_sources=[],
                error_sources=[],
            )
        df = sql_to_df(
            """
            SELECT source_name, scope_key, status, error_text, updated_at, last_success_at, state_json
            FROM advisory_sync_state
            WHERE source_name = ANY(%s)
            ORDER BY source_name, updated_at DESC NULLS LAST
            """,
            params=(list(expected_sources.keys()),),
            retries=2,
            statement_timeout_ms=10000,
        )
        latest: dict[str, dict[str, Any]] = {}
        for row in _records(df):
            source_name = str(row.get("source_name") or "")
            if source_name and source_name not in latest:
                latest[source_name] = row

        source_contract_audit = _audit_context_signal_refresh_source_contracts(max_age_minutes=max_age_minutes)
        source_contract_issue_sources = (
            ["advisory:signal_refresh:context_overlays"]
            if int(source_contract_audit.get("malformed_source_contract_count") or 0) > 0
            else []
        )
        now = pd.Timestamp.utcnow()
        rows: list[dict[str, Any]] = []
        missing_sources: list[str] = []
        stale_sources: list[str] = []
        error_sources: list[str] = []
        unsafe_authority_sources: list[str] = []
        algorithm_version_mismatch_sources: list[str] = []
        stale_count = 0
        error_count = 0
        unsafe_authority_count = 0
        algorithm_version_mismatch_count = 0
        signal_row_total = 0
        identity_policy_suppressed_target_rows_total = 0
        identity_policy_suppressed_sources: dict[str, int] = {}
        for source_name, short_name in expected_sources.items():
            row = latest.get(source_name)
            if not row:
                missing_sources.append(source_name)
                rows.append(
                    _status(
                        "warn",
                        f"No signal-refresh sync-state row found for {short_name}.",
                        source_name=source_name,
                        signal_refresh_source=short_name,
                        state={},
                    )
                )
                continue
            state = _json_dict(
                row.get("state_json"),
                source=f"{source_name}:state_json",
                metadata={"source_name": source_name},
            )
            updated_at = pd.to_datetime(row.get("updated_at"), utc=True, errors="coerce")
            last_success_at = pd.to_datetime(row.get("last_success_at"), utc=True, errors="coerce")
            age_minutes = None if pd.isna(updated_at) else max((now - updated_at).total_seconds() / 60.0, 0.0)
            status = str(row.get("status") or "").lower()
            is_stale = age_minutes is not None and age_minutes > float(max_age_minutes)
            has_error = status == "error" or bool(row.get("error_text"))
            signal_rows = int(state.get("signal_rows") or state.get("candidate_count") or 0)
            signal_row_total += signal_rows
            authority_contract = state.get("authority_contract") if isinstance(state.get("authority_contract"), dict) else {}
            broker_raw = state.get("broker_execution_allowed")
            if broker_raw is None:
                broker_raw = authority_contract.get("broker_execution_allowed")
            portfolio_authority = str(state.get("portfolio_authority") or authority_contract.get("portfolio_authority") or "none").strip().lower()
            broker_allowed = str(broker_raw).strip().lower() in {"1", "true", "yes", "y", "on"}
            full_advisory_required = state.get("full_advisory_required")
            if full_advisory_required is None:
                full_advisory_required = authority_contract.get("full_advisory_required")
            full_advisory_required_bool = str(full_advisory_required).strip().lower() in {"1", "true", "yes", "y", "on"}
            diagnostics = state.get("diagnostics") if isinstance(state.get("diagnostics"), dict) else {}
            conversion_diagnostics = state.get("conversion_diagnostics") if isinstance(state.get("conversion_diagnostics"), dict) else {}
            expected_algorithm_version = CONTEXT_OVERLAY_SIGNAL_REFRESH_VERSION if short_name == "context_overlays" else None
            state_algorithm_version = (
                str(state.get("context_overlay_signal_refresh_version") or "").strip()
                if short_name == "context_overlays"
                else ""
            )
            algorithm_version_matches = (
                True
                if short_name != "context_overlays"
                else state_algorithm_version == CONTEXT_OVERLAY_SIGNAL_REFRESH_VERSION
            )
            identity_policy_suppressed_target_rows = 0
            identity_policy_suppressed_source_counts: dict[str, int] = {}
            if short_name == "context_overlays":
                identity_policy_suppressed_target_rows = int(
                    conversion_diagnostics.get("identity_policy_suppressed_target_rows") or 0
                )
                for source_row in conversion_diagnostics.get("sources") or []:
                    if not isinstance(source_row, dict):
                        continue
                    count = int(source_row.get("identity_policy_suppressed_target_rows") or 0)
                    if count <= 0:
                        continue
                    source = str(source_row.get("source") or "unknown").strip().lower() or "unknown"
                    identity_policy_suppressed_source_counts[source] = identity_policy_suppressed_source_counts.get(source, 0) + count
                    identity_policy_suppressed_sources[source] = identity_policy_suppressed_sources.get(source, 0) + count
                identity_policy_suppressed_target_rows_total += identity_policy_suppressed_target_rows
            unsafe_authority = bool(broker_allowed or portfolio_authority != "none" or not full_advisory_required_bool)
            has_source_contract_issue = source_name in source_contract_issue_sources
            if has_error:
                row_status = "error"
                error_count += 1
                error_sources.append(source_name)
            elif unsafe_authority:
                row_status = "error"
                unsafe_authority_count += 1
                unsafe_authority_sources.append(source_name)
            elif is_stale or has_source_contract_issue or not algorithm_version_matches:
                row_status = "warn"
                if is_stale:
                    stale_count += 1
                    stale_sources.append(source_name)
                if not algorithm_version_matches:
                    algorithm_version_mismatch_count += 1
                    algorithm_version_mismatch_sources.append(source_name)
            else:
                row_status = "ok"
            rows.append(
                _status(
                    row_status,
                    "Latest review-only signal-refresh source state loaded.",
                    source_name=source_name,
                    signal_refresh_source=short_name,
                    sync_status=row.get("status"),
                    updated_at=_json_ready(updated_at),
                    last_success_at=_json_ready(last_success_at),
                    age_minutes=None if age_minutes is None else round(age_minutes, 2),
                    stale=is_stale,
                    target_rows=int(state.get("target_rows") or 0),
                    signal_rows=signal_rows,
                    affected_symbol_count=int(state.get("affected_symbol_count") or len(state.get("symbols") or [])),
                    expected_algorithm_version=expected_algorithm_version,
                    state_algorithm_version=state_algorithm_version or None,
                    algorithm_version_matches=bool(algorithm_version_matches),
                    identity_policy_suppressed_target_rows=(
                        identity_policy_suppressed_target_rows
                        if short_name == "context_overlays"
                        else None
                    ),
                    identity_policy_suppressed_sources=(
                        identity_policy_suppressed_source_counts
                        if short_name == "context_overlays"
                        else None
                    ),
                    source_contract_audit=(
                        source_contract_audit
                        if source_name == "advisory:signal_refresh:context_overlays"
                        else None
                    ),
                    source_contract_audit_ok=(
                        not has_source_contract_issue
                        if source_name == "advisory:signal_refresh:context_overlays"
                        else None
                    ),
                    authority_contract=authority_contract,
                    authority_boundary_ok=not unsafe_authority,
                    broker_execution_allowed=bool(broker_allowed),
                    portfolio_authority=portfolio_authority,
                    full_advisory_required=bool(full_advisory_required_bool),
                    candidate_helpful_memory_rows=(
                        int(diagnostics.get("candidate_helpful_memory_rows") or 0)
                        if short_name == "causal_memory"
                        else None
                    ),
                    harmful_or_no_lift_suppressed_memory_rows=(
                        int(diagnostics.get("harmful_or_no_lift_suppressed_memory_rows") or 0)
                        if short_name == "causal_memory"
                        else None
                    ),
                    benchmark_beta_not_memory_alpha_suppressed_memory_rows=(
                        int(diagnostics.get("benchmark_beta_not_memory_alpha_suppressed_memory_rows") or 0)
                        if short_name == "causal_memory"
                        else None
                    ),
                    needs_benchmark_attribution_suppressed_memory_rows=(
                        int(diagnostics.get("needs_benchmark_attribution_suppressed_memory_rows") or 0)
                        if short_name == "causal_memory"
                        else None
                    ),
                    non_candidate_suppressed_memory_rows=(
                        int(diagnostics.get("non_candidate_suppressed_memory_rows") or 0)
                        if short_name == "causal_memory"
                        else None
                    ),
                    causal_memory_suppression_policy=(
                        diagnostics.get("suppression_policy")
                        if short_name == "causal_memory"
                        else None
                    ),
                    causal_memory_suppression_diagnostics_degraded=(
                        bool(diagnostics.get("suppression_diagnostics_degraded"))
                        if short_name == "causal_memory"
                        else None
                    ),
                    error=row.get("error_text"),
                    state=state,
                )
            )
        status = (
            "error"
            if (error_count or unsafe_authority_count)
            else "warn"
            if (stale_count or missing_sources or source_contract_issue_sources or algorithm_version_mismatch_count)
            else "ok"
        )
        return _status(
            status,
            "Latest review-only signal-refresh source state loaded.",
            table="advisory_sync_state",
            rows=rows,
            returned_count=len(rows),
            missing_sources=missing_sources,
            stale_sources=stale_sources,
            error_sources=error_sources,
            unsafe_authority_sources=unsafe_authority_sources,
            algorithm_version_mismatch_sources=algorithm_version_mismatch_sources,
            source_contract_issue_sources=source_contract_issue_sources,
            source_contract_audit=source_contract_audit,
            stale_count=stale_count,
            error_count=error_count,
            unsafe_authority_count=unsafe_authority_count,
            algorithm_version_mismatch_count=algorithm_version_mismatch_count,
            malformed_source_contract_count=int(source_contract_audit.get("malformed_source_contract_count") or 0),
            signal_row_total=signal_row_total,
            identity_policy_suppressed_target_rows=identity_policy_suppressed_target_rows_total,
            identity_policy_suppressed_sources=dict(sorted(identity_policy_suppressed_sources.items(), key=lambda item: (-item[1], item[0]))),
            max_age_minutes=int(max_age_minutes),
            broker_execution_allowed=False,
            portfolio_authority="none",
            full_advisory_required=True,
        )
    except Exception as exc:
        _record_health_local_fallback(
            source="advisory_sync_state",
            fallback_type="operator_health_signal_refresh_source_state_failed",
            reason="Operator Health could not inspect signal-refresh source state.",
            error=exc,
            metadata={"max_age_minutes": int(max_age_minutes)},
        )
        return _status(
            "error",
            "Signal-refresh source-state check failed.",
            table="advisory_sync_state",
            error=f"{type(exc).__name__}: {exc}",
            rows=[],
            returned_count=0,
        )


def _audit_context_signal_refresh_source_contracts(max_age_minutes: int = 90, *, limit: int = 100) -> dict[str, Any]:
    try:
        if not table_exists(SIGNAL_REFRESH_ACTIONS_TABLE):
            return {
                "status": "warn",
                "message": "Signal-refresh actions table does not exist yet; source-contract audit is unavailable.",
                "table": SIGNAL_REFRESH_ACTIONS_TABLE,
                "checked_rows": 0,
                "malformed_source_contract_count": 0,
                "samples": [],
            }
        cutoff = pd.Timestamp.utcnow() - pd.Timedelta(minutes=max(1, int(max_age_minutes)))
        prefixes = list(CONTEXT_SIGNAL_REFRESH_SOURCE_PREFIXES)
        df = sql_to_df(
            f"""
            SELECT refreshed_at, symbol, unique_id, signal_source, signal_action, action_payload_json
            FROM {SIGNAL_REFRESH_ACTIONS_TABLE}
            WHERE refreshed_at >= %s
              AND (
                LOWER(COALESCE(signal_source, '')) LIKE ANY(%s)
              )
            ORDER BY refreshed_at DESC
            LIMIT %s
            """,
            params=(cutoff, [f"{prefix}%" for prefix in prefixes], max(1, int(limit))),
            retries=2,
            statement_timeout_ms=10000,
        )
        malformed: list[dict[str, Any]] = []
        checked = 0
        for row in _records(df):
            checked += 1
            payload = _json_dict(
                row.get("action_payload_json"),
                source="signal_refresh_action_payload_json",
                metadata={"symbol": row.get("symbol"), "signal_source": row.get("signal_source")},
            )
            source_contract = payload.get("source_contract") if isinstance(payload.get("source_contract"), dict) else {}
            source_table = str(source_contract.get("source_table") or "").strip()
            source_key = str(source_contract.get("source_key") or source_contract.get("context_overlay_id") or "").strip()
            broker_allowed = str(source_contract.get("broker_execution_allowed")).strip().lower() in {"1", "true", "yes", "y", "on"}
            issue_reasons: list[str] = []
            if not source_contract:
                issue_reasons.append("missing_source_contract")
            if not source_table:
                issue_reasons.append("missing_source_table")
            if not source_key:
                issue_reasons.append("missing_source_key")
            if broker_allowed:
                issue_reasons.append("source_contract_broker_authority_not_allowed")
            if issue_reasons:
                malformed.append(
                    {
                        "symbol": row.get("symbol"),
                        "unique_id": row.get("unique_id"),
                        "signal_source": row.get("signal_source"),
                        "signal_action": row.get("signal_action"),
                        "refreshed_at": _json_ready(pd.to_datetime(row.get("refreshed_at"), utc=True, errors="coerce")),
                        "issue_reasons": issue_reasons,
                    }
                )
        status = "warn" if malformed else "ok"
        return {
            "status": status,
            "message": (
                "Recent context-overlay signal-refresh rows have source contracts."
                if not malformed
                else "Recent context-overlay signal-refresh rows include malformed or missing source contracts."
            ),
            "table": SIGNAL_REFRESH_ACTIONS_TABLE,
            "checked_rows": int(checked),
            "malformed_source_contract_count": int(len(malformed)),
            "samples": malformed[:10],
        }
    except Exception as exc:
        _record_health_local_fallback(
            source=SIGNAL_REFRESH_ACTIONS_TABLE,
            fallback_type="operator_health_signal_refresh_source_contract_audit_failed",
            reason="Operator Health could not audit signal-refresh context source contracts.",
            error=exc,
            metadata={"max_age_minutes": int(max_age_minutes), "limit": int(limit)},
        )
        return {
            "status": "warn",
            "message": "Signal-refresh source-contract audit failed.",
            "table": SIGNAL_REFRESH_ACTIONS_TABLE,
            "checked_rows": 0,
            "malformed_source_contract_count": 0,
            "samples": [],
            "error": f"{type(exc).__name__}: {exc}",
        }


def _download_runner_severity(row_status: str, classification: str) -> str:
    status = str(row_status or "").lower()
    klass = str(classification or "").lower()
    if klass in {"source_unavailable", "sync_state_persist_failed", "partial_failed"}:
        return "warn"
    if status == "error" or klass in {"failed", "parse_failed", "auth_unavailable", "reference_mapping_missing"}:
        return "error"
    return "ok"


def check_downloader_run_state(limit: int = 30) -> dict[str, Any]:
    try:
        if not table_exists("advisory_sync_state"):
            return _status(
                "warn",
                "Sync-state table does not exist yet; downloader/parser run-state is unavailable.",
                table="advisory_sync_state",
                rows=[],
                returned_count=0,
            )
        df = sql_to_df(
            """
            SELECT source_name, scope_key, status, error_text, updated_at, last_success_at, state_json
            FROM advisory_sync_state
            WHERE source_name LIKE 'download_runner:%'
            ORDER BY updated_at DESC NULLS LAST
            LIMIT %s
            """,
            params=(int(limit),),
            retries=2,
            statement_timeout_ms=10000,
        )
        if df.empty:
            return _status(
                "warn",
                "No standardized downloader/parser run-state rows found yet.",
                table="advisory_sync_state",
                rows=[],
                returned_count=0,
                command="./complete_data.sh",
            )

        rows: list[dict[str, Any]] = []
        counts_by_classification: dict[str, int] = {}
        counts_by_phase: dict[str, int] = {}
        for _, row in df.iterrows():
            state = _json_dict(row.get("state_json"))
            source_name = str(row.get("source_name") or "")
            module = str(state.get("module") or source_name.removeprefix("download_runner:") or "unknown")
            classification = str(state.get("classification") or state.get("status") or row.get("status") or "unknown")
            classification_meta = ingestion_classification_metadata(classification)
            phase = str(state.get("phase") or "unknown")
            severity = _download_runner_severity(str(row.get("status") or ""), classification)
            counts_by_classification[classification] = counts_by_classification.get(classification, 0) + 1
            counts_by_phase[phase] = counts_by_phase.get(phase, 0) + 1
            updated_at = pd.to_datetime(row.get("updated_at"), utc=True, errors="coerce")
            last_success_at = pd.to_datetime(row.get("last_success_at"), utc=True, errors="coerce")
            rows.append(
                {
                    "status": severity,
                    "message": "Latest standardized downloader/parser run-state.",
                    "source_name": source_name,
                    "module": module,
                    "purpose": state.get("purpose"),
                    "phase": phase,
                    "classification": classification,
                    "classification_label": classification_meta["label"],
                    "classification_meaning": classification_meta["meaning"],
                    "classification_operator_action": classification_meta["operator_action"],
                    "trust_impact": classification_meta["trust_impact"],
                    "run_status": state.get("status"),
                    "sync_status": row.get("status"),
                    "rows": state.get("rows"),
                    "rows_read": state.get("rows_read"),
                    "rows_written": state.get("rows_written"),
                    "symbol_count": state.get("symbol_count"),
                    "retry_count": state.get("retry_count") if state.get("retry_count") is not None else state.get("retries"),
                    "attempt_count": state.get("attempt_count"),
                    "failed_attempt_count": state.get("failed_attempt_count"),
                    "fallback_count": state.get("fallback_count"),
                    "source_unavailable_count": state.get("source_unavailable_count"),
                    "no_data_count": state.get("no_data_count"),
                    "auth_unavailable_count": state.get("auth_unavailable_count"),
                    "reference_mapping_missing_count": state.get("reference_mapping_missing_count"),
                    "classification_counts": state.get("classification_counts"),
                    "failed_symbols": state.get("failed_symbols"),
                    "from": state.get("from"),
                    "to": state.get("to"),
                    "updated_at": _json_ready(updated_at),
                    "last_success_at": _json_ready(last_success_at),
                    "state_advanced": bool(state.get("state_advanced")) if state.get("state_advanced") is not None else None,
                    "fallback_used": bool(state.get("fallback_used")) if state.get("fallback_used") is not None else None,
                    "error": row.get("error_text") or state.get("error"),
                    "state": state,
                }
            )

        error_count = sum(1 for row in rows if row.get("status") == "error")
        warn_count = sum(1 for row in rows if row.get("status") == "warn")
        ok_count = sum(1 for row in rows if row.get("status") == "ok")
        advanced_count = sum(1 for row in rows if row.get("state_advanced") is True)
        stalled_count = sum(1 for row in rows if row.get("state_advanced") is False and row.get("status") != "error")
        retry_count = sum(int(row.get("retry_count") or 0) for row in rows)
        fallback_count = sum(int(row.get("fallback_count") or 0) for row in rows)
        status = "error" if error_count else "warn" if warn_count else "ok"
        return _status(
            status,
            "Latest standardized downloader/parser run-state loaded.",
            table="advisory_sync_state",
            returned_count=len(rows),
            ok_count=ok_count,
            warn_count=warn_count,
            error_count=error_count,
            advanced_count=advanced_count,
            stalled_count=stalled_count,
            retry_count=retry_count,
            fallback_count=fallback_count,
            counts_by_classification=counts_by_classification,
            counts_by_phase=counts_by_phase,
            rows=rows,
        )
    except Exception as exc:
        _record_health_local_fallback(
            source="advisory_sync_state",
            fallback_type="operator_health_downloader_run_state_check_failed",
            reason="Operator Health could not inspect standardized downloader/parser run-state rows.",
            error=exc,
            metadata={"limit": int(limit)},
        )
        return _status(
            "error",
            "Downloader/parser run-state check failed.",
            table="advisory_sync_state",
            error=f"{type(exc).__name__}: {exc}",
            rows=[],
            returned_count=0,
        )


def check_ingestion_file_state_failures(limit: int = 100) -> dict[str, Any]:
    try:
        rows = get_ingestion_state_entries(status="failed", limit=max(1, int(limit)))
        summary = summarize_ingestion_state_entries(
            rows,
            sample_limit=10,
            active_failure_days=OPERATOR_HEALTH_INGESTION_FAILURE_ACTIVE_DAYS,
        )
        active_count = int(summary.get("active_failure_count") or 0)
        stale_count = int(summary.get("stale_historical_failure_count") or 0)
        classifications = summary.get("classification_counts") if isinstance(summary.get("classification_counts"), dict) else {}
        active_rows = summary.get("active_failure_sample_rows") if isinstance(summary.get("active_failure_sample_rows"), list) else []
        active_error_classes = {
            str(row.get("classification") or "unknown")
            for row in active_rows
            if isinstance(row, dict) and str(row.get("classification") or "unknown") in {"parser_bug", "schema_changed"}
        }
        status = "error" if active_error_classes else "warn" if active_count else "ok"
        return _status(
            status,
            (
                "Recent actionable file-level parser failures found."
                if active_count
                else "No recent actionable file-level parser failures found."
            ),
            table="ingestion_file_state",
            active_failure_days=OPERATOR_HEALTH_INGESTION_FAILURE_ACTIVE_DAYS,
            returned_count=len(rows),
            active_failure_count=active_count,
            stale_historical_failure_count=stale_count,
            classification_counts=classifications,
            failure_lifecycle_counts=summary.get("failure_lifecycle_counts") or {},
            active_failure_sample_rows=active_rows,
            stale_historical_failure_sample_rows=summary.get("stale_historical_failure_sample_rows") or [],
            operator_boundary={
                "read_only": True,
                "mutates_ingestion_state": False,
                "stale_historical_failures_block_trust": False,
                "note": "Old file-level parser failures stay visible for audit, but only recent failures inside the active window are treated as current blockers.",
            },
        )
    except Exception as exc:
        _record_health_local_fallback(
            source="ingestion_file_state",
            fallback_type="operator_health_ingestion_file_state_failed",
            reason="Operator Health could not inspect file-level ingestion parser failures.",
            error=exc,
            metadata={"limit": int(limit), "active_days": OPERATOR_HEALTH_INGESTION_FAILURE_ACTIVE_DAYS},
        )
        return _status(
            "error",
            "File-level ingestion-state failure check failed.",
            table="ingestion_file_state",
            error=f"{type(exc).__name__}: {exc}",
            rows=[],
            returned_count=0,
        )


def check_schema_migrations(limit: int = 20) -> dict[str, Any]:
    try:
        if not table_exists(SCHEMA_MIGRATIONS_TABLE):
            return _status(
                "warn",
                "Schema migration registry table does not exist yet.",
                table=SCHEMA_MIGRATIONS_TABLE,
                rows=[],
                returned_count=0,
                failed_count=0,
                running_count=0,
                command="python -m utils.schema_migrations --ensure-table",
            )
        df = sql_to_df(
            f"""
            SELECT migration_id, status, checksum, description, started_at, applied_at, finished_at, error_text, metadata_json
            FROM {SCHEMA_MIGRATIONS_TABLE}
            ORDER BY COALESCE(finished_at, started_at) DESC NULLS LAST, migration_id DESC
            LIMIT %s
            """,
            params=(int(limit),),
            retries=2,
            statement_timeout_ms=10000,
        )
        rows = _records(df)
        failed_count = sum(1 for row in rows if str(row.get("status") or "").lower() == "failed")
        running_count = sum(1 for row in rows if str(row.get("status") or "").lower() == "running")
        applied_count = sum(1 for row in rows if str(row.get("status") or "").lower() == "applied")
        status = "error" if failed_count else "warn" if running_count else "ok"
        message = (
            "Schema migration registry has failed migration rows."
            if failed_count
            else "Schema migration registry has running migration rows."
            if running_count
            else "Schema migration registry is ready."
        )
        if not rows:
            message = "Schema migration registry exists; no migrations recorded yet."
        return _status(
            status,
            message,
            table=SCHEMA_MIGRATIONS_TABLE,
            returned_count=len(rows),
            failed_count=failed_count,
            running_count=running_count,
            applied_count=applied_count,
            rows=rows,
        )
    except Exception as exc:
        _record_health_local_fallback(
            source=SCHEMA_MIGRATIONS_TABLE,
            fallback_type="operator_health_schema_migrations_check_failed",
            reason="Operator Health could not inspect the schema migration registry.",
            error=exc,
        )
        return _status(
            "error",
            "Schema migration registry check failed.",
            table=SCHEMA_MIGRATIONS_TABLE,
            error=f"{type(exc).__name__}: {exc}",
            rows=[],
            returned_count=0,
            failed_count=0,
            running_count=0,
        )


def check_dhan_token() -> dict[str, Any]:
    try:
        direct_token = env("DHAN_ACCESS_TOKEN", default=None)
        cached_token = None
        cached_payload = None
        try:
            from data.dhanlive.auth import load_cached_access_token, load_cached_access_token_payload

            cached_token = load_cached_access_token()
            cached_payload = load_cached_access_token_payload()
        except Exception as exc:
            _record_health_local_fallback(
                source="data.dhanlive.auth",
                fallback_type="operator_health_dhan_cached_token_inspect_failed",
                reason="Operator Health could not inspect cached Dhan token state.",
                error=exc,
                severity="warn",
            )
            return _status("warn", "Could not inspect cached Dhan token.", error=f"{type(exc).__name__}: {exc}")
        token = direct_token or cached_token
        if not token:
            return _status("warn", "No non-interactive Dhan token available. Health check will not trigger login.", has_cached_payload=bool(cached_payload))
        from data.dhanlive.client import DhanHistoricalClient

        client = DhanHistoricalClient(access_token=token, timeout=10, auth_attempts=1)
        payload = client.validate_access_token()
        return _status("ok", "Dhan profile call succeeded.", token_source="env" if direct_token else "cache", profile_keys=sorted(payload.keys()) if isinstance(payload, dict) else [])
    except Exception as exc:
        _record_health_local_fallback(
            source="data.dhanlive.client",
            fallback_type="operator_health_dhan_token_validation_failed",
            reason="Operator Health could not validate the configured Dhan token.",
            error=exc,
        )
        return _status("error", "Dhan token validation failed.", error=f"{type(exc).__name__}: {exc}")


def _cdp_version_url(cdp_endpoint: str) -> str | None:
    text = str(cdp_endpoint or "").strip()
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.scheme in {"ws", "wss"}:
        scheme = "https" if parsed.scheme == "wss" else "http"
        return f"{scheme}://{parsed.netloc}/json/version"
    if parsed.scheme in {"http", "https"}:
        base = text.rstrip("/")
        if base.endswith("/json/version"):
            return base
        return f"{base}/json/version"
    return f"http://{text.rstrip('/')}/json/version"


def check_dhan_cdp_endpoint(timeout_seconds: float = 1.0) -> dict[str, Any]:
    cdp_endpoint = env("CDP_ENDPOINT", default="")
    url = _cdp_version_url(cdp_endpoint)
    if not url:
        return _status("warn", "CDP_ENDPOINT is not configured.", configured=False, cdp_endpoint=cdp_endpoint)
    started = time.monotonic()
    try:
        response = requests.get(url, timeout=timeout_seconds)
        latency_ms = round((time.monotonic() - started) * 1000.0, 2)
        payload = response.json() if response.headers.get("content-type", "").lower().startswith("application/json") else {}
        if response.ok:
            return _status(
                "ok",
                "Chrome CDP endpoint responded.",
                configured=True,
                cdp_endpoint=cdp_endpoint,
                url=url,
                latency_ms=latency_ms,
                browser=payload.get("Browser") if isinstance(payload, dict) else None,
            )
        return _status(
            "warn",
            "Chrome CDP endpoint returned an unhealthy response.",
            configured=True,
            cdp_endpoint=cdp_endpoint,
            url=url,
            latency_ms=latency_ms,
            status_code=response.status_code,
            response_text=response.text[:300],
        )
    except Exception as exc:
        latency_ms = round((time.monotonic() - started) * 1000.0, 2)
        _record_health_local_fallback(
            source=str(url),
            fallback_type="operator_health_dhan_cdp_check_failed",
            reason="Operator Health could not reach the configured Chrome CDP endpoint used for Dhan automated login.",
            error=exc,
            severity="warn",
            metadata={"cdp_endpoint": cdp_endpoint, "url": url, "latency_ms": latency_ms},
        )
        return _status(
            "warn",
            "Chrome CDP endpoint is not reachable.",
            configured=True,
            cdp_endpoint=cdp_endpoint,
            url=url,
            latency_ms=latency_ms,
            error=f"{type(exc).__name__}: {exc}",
        )


def check_dhan_cache() -> dict[str, Any]:
    try:
        from data.dhanlive.auth import DEFAULT_TOKEN_CACHE, is_auto_login_configured, load_cached_access_token_payload
        from data.dhanlive.auth_cli import mask_token

        direct_token = env("DHAN_ACCESS_TOKEN", default=None)
        env_token_id = env("DHAN_TOKEN_ID", default=None)
        auto_login_configured = is_auto_login_configured()
        cdp_status = check_dhan_cdp_endpoint() if auto_login_configured else _status("warn", "Dhan automated login is not fully configured.", configured=False)
        auth_refresh_ready = bool(direct_token or env_token_id or (auto_login_configured and cdp_status.get("status") == "ok"))
        payload = load_cached_access_token_payload()
        cache_exists = DEFAULT_TOKEN_CACHE.exists()
        file_mtime = pd.to_datetime(DEFAULT_TOKEN_CACHE.stat().st_mtime, unit="s", utc=True) if cache_exists else pd.NaT
        now = pd.Timestamp.utcnow()
        expiry_time = payload.get("expiryTime") if payload else None
        expires_at = pd.to_datetime(expiry_time, utc=True, errors="coerce") if expiry_time else pd.NaT
        seconds_to_expiry = None if pd.isna(expires_at) else round((expires_at - now).total_seconds(), 2)
        cache_age_seconds = None if pd.isna(file_mtime) else round((now - file_mtime).total_seconds(), 2)
        if direct_token:
            severity = "ok"
            message = "Dhan direct env token is configured; cache expiry is informational."
        elif not cache_exists:
            severity = "warn"
            message = "Dhan token cache file does not exist."
        elif not payload or not payload.get("accessToken"):
            severity = "warn"
            message = "Dhan token cache has no access token."
        elif seconds_to_expiry is None:
            severity = "warn"
            message = "Dhan cached token expiry could not be parsed."
        elif seconds_to_expiry <= 0:
            severity = "error"
            message = "Dhan cached token is expired."
            if not auth_refresh_ready:
                message = "Dhan cached token is expired and no ready non-interactive refresh path was detected."
        elif seconds_to_expiry <= DHAN_TOKEN_EXPIRY_WARN_SECONDS:
            severity = "warn"
            message = "Dhan cached token expires soon."
        else:
            severity = "ok"
            message = "Dhan cached token expiry looks healthy."
        return _status(
            severity,
            message,
            cache_path=str(DEFAULT_TOKEN_CACHE),
            cache_exists=bool(cache_exists),
            cache_mtime=_json_ready(file_mtime),
            cache_age_seconds=cache_age_seconds,
            has_direct_env_token=bool(direct_token),
            has_env_token_id=bool(env_token_id),
            auto_login_configured=bool(auto_login_configured),
            auth_refresh_ready=bool(auth_refresh_ready),
            cdp_status=cdp_status,
            has_cached_access_token=bool(payload and payload.get("accessToken")),
            cached_access_token=mask_token(payload.get("accessToken") if payload else None),
            expiry_time=expiry_time,
            expires_at=_json_ready(expires_at),
            seconds_to_expiry=seconds_to_expiry,
            warn_seconds=DHAN_TOKEN_EXPIRY_WARN_SECONDS,
        )
    except Exception as exc:
        _record_health_local_fallback(
            source="data.dhanlive.auth",
            fallback_type="operator_health_dhan_cache_check_failed",
            reason="Operator Health could not inspect the Dhan token cache file.",
            error=exc,
            severity="warn",
        )
        return _status("warn", "Could not inspect Dhan token cache.", error=f"{type(exc).__name__}: {exc}")


def _tail_lines(path: Path, limit: int) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        _record_health_local_fallback(
            source=str(path),
            fallback_type="operator_health_log_tail_failed",
            reason="Operator Health could not read a log tail; diagnostics may be incomplete.",
            error=exc,
            severity="warn",
            metadata={"path": str(path), "limit": int(limit)},
        )
        return []
    return lines[-max(1, int(limit)) :]


def analyze_cron_log_lines(lines: list[str]) -> dict[str, Any]:
    error_hits: list[tuple[int, str]] = []
    success_hits: list[tuple[int, str]] = []
    marker_hits: list[tuple[int, dict[str, Any]]] = []
    for idx, line in enumerate(lines):
        if any(pattern.search(line) for pattern in FAILURE_PATTERNS):
            error_hits.append((idx, line))
        if any(pattern.search(line) for pattern in SUCCESS_PATTERNS):
            success_hits.append((idx, line))
        marker = SCRIPT_MARKER_PATTERN.search(line)
        if marker:
            marker_hits.append(
                (
                    idx,
                    {
                        "line": redact_text(line),
                        "name": marker.group("name"),
                        "status": str(marker.group("status") or "").lower(),
                        "exit_code": marker.group("exit_code"),
                        "timestamp": marker.group("timestamp"),
                    },
                )
            )

    if marker_hits:
        latest_marker_idx, latest_marker = marker_hits[-1]
        marker_status = str(latest_marker.get("status") or "")
        historical_errors = [line for idx, line in error_hits if idx <= latest_marker_idx]
        current_errors = [line for idx, line in error_hits if idx > latest_marker_idx]
        if marker_status == "done":
            severity = "warn" if historical_errors else "ok"
            latest_run_status = "ok_after_historical_errors" if historical_errors else "ok"
            message = "Latest script marker completed successfully." if not historical_errors else "Latest script marker completed after earlier error markers."
        elif marker_status == "interrupted":
            severity = "warn"
            latest_run_status = "interrupted_by_operator"
            message = "Latest script marker shows operator interruption."
        elif marker_status == "restart_requested":
            severity = "warn"
            latest_run_status = "restart_requested"
            message = "Latest script marker requested a supervised restart, usually after source-code changes."
        elif marker_status == "start":
            severity = "warn"
            latest_run_status = "running_or_started"
            message = "Latest script marker is start; no terminal marker is present yet."
        else:
            severity = "error"
            latest_run_status = "failed"
            message = "Latest script marker reports failure."
        if current_errors and marker_status != "interrupted":
            severity = "error"
            latest_run_status = "failed"
            message = "Error markers appear after latest script marker."
        return {
            "status": severity,
            "message": message,
            "latest_run_status": latest_run_status,
            "latest_success_line": redact_text(latest_marker["line"]) if marker_status == "done" else None,
            "latest_error_line": redact_text(latest_marker["line"]) if marker_status in {"failed", "interrupted"} else (redact_text(current_errors[-1]) if current_errors else None),
            "latest_script_marker": latest_marker,
            "current_error_count": len(current_errors),
            "historical_error_count": len(historical_errors),
            "recent_error_count": len(current_errors),
            "recent_errors": [redact_text(line) or "" for line in current_errors[-8:]],
            "historical_errors": [redact_text(line) or "" for line in historical_errors[-8:]],
        }

    latest_error = error_hits[-1] if error_hits else None
    latest_success = success_hits[-1] if success_hits else None
    latest_error_idx = latest_error[0] if latest_error else -1
    latest_success_idx = latest_success[0] if latest_success else -1

    if latest_error_idx < 0:
        severity = "ok"
        latest_run_status = "ok"
        message = "No error markers found in analyzed log window."
    elif latest_success_idx > latest_error_idx:
        severity = "warn"
        latest_run_status = "ok_after_historical_errors"
        message = "Latest success marker appears after earlier error markers."
    else:
        severity = "error"
        latest_run_status = "failed_or_interrupted"
        message = "Latest terminal marker is an error or interruption."

    current_errors = [line for idx, line in error_hits if idx > latest_success_idx]
    historical_errors = [line for idx, line in error_hits if idx <= latest_success_idx]
    return {
        "status": severity,
        "message": message,
        "latest_run_status": latest_run_status,
        "latest_success_line": redact_text(latest_success[1]) if latest_success else None,
        "latest_error_line": redact_text(latest_error[1]) if latest_error else None,
        "current_error_count": len(current_errors),
        "historical_error_count": len(historical_errors),
        "recent_error_count": len(current_errors),
        "recent_errors": [redact_text(line) or "" for line in current_errors[-8:]],
        "historical_errors": [redact_text(line) or "" for line in historical_errors[-8:]],
    }


def recover_interrupted_cron_status(path: Path, modified_at: pd.Timestamp, analysis: dict[str, Any]) -> dict[str, Any]:
    latest_error_line = str(analysis.get("latest_error_line") or "")
    recent_errors = [str(value) for value in analysis.get("recent_errors") or []]
    if "KeyboardInterrupt" not in latest_error_line and not any("KeyboardInterrupt" in value for value in recent_errors):
        return analysis
    specs = CRON_RECOVERY_OUTPUTS.get(path.name)
    if not specs:
        return {
            **analysis,
            "latest_run_status": "interrupted_by_operator",
            "message": "Latest log marker is a manual interruption and no output recovery mapping exists.",
            "status": "warn",
        }

    outputs: list[dict[str, Any]] = []
    recovered = False
    for spec in specs:
        try:
            output = get_table_latest_timestamp(
                str(spec["table"]),
                str(spec["column"]),
                fallback_column=str(spec.get("fallback_column")) if spec.get("fallback_column") else None,
            )
            output["name"] = spec["name"]
            latest_at = pd.to_datetime(output.get("latest_at"), utc=True, errors="coerce")
            output["is_newer_than_log"] = bool(pd.notna(latest_at) and latest_at > modified_at)
            recovered = recovered or bool(output["is_newer_than_log"])
            outputs.append(output)
        except Exception as exc:
            _record_health_local_fallback(
                source=str(spec["table"]),
                fallback_type="operator_health_cron_recovery_check_failed",
                reason="Operator Health could not inspect recovery output for an interrupted cron log.",
                error=exc,
                metadata={
                    "log_file": path.name,
                    "output_name": spec["name"],
                    "table": spec["table"],
                    "column": spec["column"],
                },
            )
            outputs.append(
                _status(
                    "error",
                    "Could not check output recovery table.",
                    name=spec["name"],
                    table=spec["table"],
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    if not recovered:
        return {**analysis, "recovery_outputs": outputs}
    historical_errors = list(analysis.get("historical_errors") or [])
    historical_errors.extend(str(value) for value in analysis.get("recent_errors") or [])
    return {
        **analysis,
        "status": "warn",
        "message": "Latest log marker is a manual interruption, but newer output rows show a later successful run.",
        "latest_run_status": "recovered_after_manual_interrupt",
        "current_error_count": 0,
        "recent_error_count": 0,
        "recent_errors": [],
        "historical_error_count": int(analysis.get("historical_error_count") or 0) + int(analysis.get("recent_error_count") or 0),
        "historical_errors": historical_errors[-8:],
        "recovery_outputs": outputs,
    }


def check_cron_logs(log_dir: str | Path = DEFAULT_LOG_DIR, *, tail_lines: int = DEFAULT_LOG_TAIL_LINES, analysis_lines: int = DEFAULT_LOG_ANALYSIS_LINES) -> list[dict[str, Any]]:
    root = Path(log_dir)
    if not root.exists():
        return [_status("warn", "Cron log directory does not exist.", log_dir=str(root))]
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.log")):
        stat = path.stat()
        lines = _tail_lines(path, tail_lines)
        analyzed_lines = _tail_lines(path, max(tail_lines, int(analysis_lines)))
        analysis = analyze_cron_log_lines(analyzed_lines)
        modified_at = pd.to_datetime(stat.st_mtime, unit="s", utc=True)
        analysis = recover_interrupted_cron_status(path, modified_at, analysis)
        status = str(analysis.pop("status"))
        message = str(analysis.pop("message"))
        degradation_markers = [
            line
            for line in analyzed_lines
            if any(spec["pattern"].search(line) for spec in DEGRADATION_PATTERNS)
        ][-12:]
        rows.append(
            _status(
                status,
                message,
                log_file=str(path),
                size_bytes=stat.st_size,
                modified_at=_json_ready(modified_at),
                tail_lines=len(lines),
                analyzed_lines=len(analyzed_lines),
                recent_tail_errors=[redact_text(line) or "" for line in lines if any(pattern.search(line) for pattern in FAILURE_PATTERNS)][-8:],
                degradation_markers=[redact_text(line) or "" for line in degradation_markers],
                **analysis,
            )
        )
    return rows or [_status("warn", "Cron log directory has no .log files.", log_dir=str(root))]


def _extract_degradation_from_line(line: str, *, source: str, observed_at: Any = None, recovered: bool = False) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for spec in DEGRADATION_PATTERNS:
        match = spec["pattern"].search(line)
        if not match:
            continue
        symbol = str(match.groupdict().get("symbol") or "").upper() or None
        row = {
            "status": "recovered" if recovered else spec["severity"],
            "severity": spec["severity"],
            "kind": spec["kind"],
            "title": spec["title"],
            "message": line.strip()[:700],
            "source": source,
            "symbol": symbol,
            "exchange": match.groupdict().get("exchange"),
            "observed_at": _json_ready(pd.to_datetime(observed_at, utc=True, errors="coerce")) if observed_at is not None else None,
            "suggested_fix": spec["suggested_fix"],
            "recovered": bool(recovered),
        }
        rows.append(row)
    return rows


def _dedupe_degradations(rows: list[dict[str, Any]], *, limit: int = 100) -> list[dict[str, Any]]:
    deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row.get("kind") or ""), str(row.get("symbol") or ""), str(row.get("source") or ""))
        existing = deduped.get(key)
        if existing is None:
            deduped[key] = row
            continue
        row_recovered = bool(row.get("recovered"))
        existing_recovered = bool(existing.get("recovered"))
        if existing_recovered and not row_recovered:
            deduped[key] = row
            continue
        if not existing_recovered and row_recovered:
            continue
        if str(row.get("observed_at") or "") >= str(existing.get("observed_at") or ""):
            deduped[key] = row
    out = list(deduped.values())
    out.sort(key=lambda row: (str(row.get("status") or ""), str(row.get("observed_at") or "")), reverse=True)
    return out[: max(1, int(limit))]


def build_degradation_lifecycle_groups(rows: list[dict[str, Any]], *, limit: int = 100, include_superseded_preview: bool = True) -> dict[str, Any]:
    active_count = sum(1 for row in rows if not row.get("recovered"))
    recovered_count = sum(1 for row in rows if row.get("recovered"))
    superseded_preview: dict[str, Any] = {"status": "unavailable", "error": None}
    superseded_count = 0
    if include_superseded_preview:
        try:
            preview = cleanup_superseded_failures(apply=False, limit=max(1, int(limit)))
            event_processing = preview.get("event_processing") if isinstance(preview.get("event_processing"), dict) else {}
            announcement_documents = preview.get("announcement_documents") if isinstance(preview.get("announcement_documents"), dict) else {}
            superseded_count = int(event_processing.get("candidates") or 0) + int(announcement_documents.get("candidates") or 0)
            superseded_preview = {
                "status": preview.get("status") or "dry_run",
                "event_processing_candidates": int(event_processing.get("candidates") or 0),
                "announcement_document_candidates": int(announcement_documents.get("candidates") or 0),
                "sample_count": len(event_processing.get("sample") or []) + len(announcement_documents.get("sample") or []),
                "event_processing_sample": event_processing.get("sample") or [],
                "announcement_document_sample": announcement_documents.get("sample") or [],
                "dry_run_command": "python -m advisory.superseded_failures --limit 500",
                "apply_command": "python -m advisory.superseded_failures --apply --limit 500",
                "apply_requires_operator_intent": True,
            }
        except Exception as exc:
            _record_health_local_fallback(
                source="advisory.superseded_failures",
                fallback_type="operator_health_superseded_preview_failed",
                reason="Operator Health could not build superseded failure preview.",
                error=exc,
                metadata={"limit": max(1, int(limit))},
            )
            superseded_preview = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    else:
        superseded_preview = {
            "status": "deferred",
            "dry_run_command": "python -m advisory.superseded_failures --limit 500",
            "apply_requires_operator_intent": True,
        }

    groups = [
        {
            "key": "active",
            "label": "Active",
            "status": "error" if any(row.get("status") == "error" and not row.get("recovered") for row in rows) else "warn" if active_count else "ok",
            "count": active_count,
            "description": "Current degradation rows that still need investigation before trusting advisory output.",
            "next_action": "Fix source issue, rerun the relevant pipeline, then refresh Health.",
        },
        {
            "key": "recovered",
            "label": "Recovered",
            "status": "warn" if recovered_count else "ok",
            "count": recovered_count,
            "description": "Historical errors followed by a successful marker or newer recovered output.",
            "next_action": "Keep for audit unless they continue to obscure current issues.",
        },
        {
            "key": "superseded",
            "label": "Superseded Ready",
            "status": "error" if superseded_preview.get("status") == "error" else "warn" if superseded_count else "ok",
            "count": superseded_count,
            "description": "Recovered processing/document failures that can be marked superseded after operator review.",
            "next_action": "Review the Health sample, run the dry-run command, and apply only with explicit operator intent.",
            "details": superseded_preview,
        },
    ]
    status = "error" if groups[0]["status"] == "error" or superseded_preview.get("status") == "error" else "warn" if any(group["status"] == "warn" for group in groups) else "ok"
    return {
        "status": status,
        "counts": {
            "active": active_count,
            "recovered": recovered_count,
            "superseded": superseded_count,
        },
        "groups": groups,
        "superseded_preview": superseded_preview,
    }


_RECOVERED_DOCUMENT_STATUSES = {"completed", "success", "ok", "processed", "skipped", "unavailable"}


def _announcement_failure_is_active(row: dict[str, Any]) -> bool:
    ocr_status = str(row.get("ocr_status") or "").strip().lower()
    parse_status = str(row.get("parse_status") or "").strip().lower()
    if ocr_status == "failed" or parse_status == "failed":
        return True
    if row.get("last_error") is None:
        return False
    return not (ocr_status in _RECOVERED_DOCUMENT_STATUSES and parse_status in _RECOVERED_DOCUMENT_STATUSES)


def check_announcement_document_failures(limit: int = 25) -> list[dict[str, Any]]:
    table = "announcement_pipeline_documents"
    try:
        if not table_exists(table):
            return []
        columns = table_columns(table)
        if not {"ocr_status", "parse_status"}.intersection(columns):
            return []
        order_column = "updated_at" if "updated_at" in columns else "published_at" if "published_at" in columns else "load_ts" if "load_ts" in columns else None
        select_columns = [col for col in ["unique_id", "ticker", "symbol", "ocr_status", "parse_status", "last_error", "published_at", "updated_at", "load_ts"] if col in columns]
        order_sql = f'ORDER BY "{order_column}" DESC NULLS LAST' if order_column else ""
        df = sql_to_df(
            f"""
            SELECT {", ".join(f'"{col}"' for col in select_columns)}
            FROM {table}
            WHERE COALESCE(ocr_status, '') = 'failed'
               OR COALESCE(parse_status, '') = 'failed'
               OR (
                    last_error IS NOT NULL
                    AND NOT (
                        LOWER(COALESCE(ocr_status, '')) IN ('completed', 'success', 'ok', 'processed', 'skipped', 'unavailable')
                        AND LOWER(COALESCE(parse_status, '')) IN ('completed', 'success', 'ok', 'processed', 'skipped', 'unavailable')
                    )
               )
            {order_sql}
            LIMIT %s
            """,
            params=(int(limit),),
            retries=2,
            statement_timeout_ms=10000,
        )
        rows = []
        for item in _records(df):
            if not _announcement_failure_is_active(item):
                continue
            symbol = str(item.get("ticker") or item.get("symbol") or "").strip().upper() or None
            rows.append(
                {
                    "status": "warn",
                    "severity": "warn",
                    "kind": "announcement_document_failure",
                    "title": "Announcement document OCR/parse issue",
                    "message": str(item.get("last_error") or f"ocr={item.get('ocr_status')} parse={item.get('parse_status')}"),
                    "source": table,
                    "symbol": symbol,
                    "unique_id": item.get("unique_id"),
                    "observed_at": item.get("updated_at") or item.get("published_at") or item.get("load_ts"),
                    "suggested_fix": "Install Poppler if needed, rerun announcement ingest, or inspect the attachment manually.",
                    "recovered": False,
                }
            )
        return rows
    except Exception as exc:
        _record_health_local_fallback(
            source=table,
            fallback_type="operator_health_announcement_document_failures_check_failed",
            reason="Operator Health could not inspect announcement document OCR/parse failures.",
            error=exc,
            metadata={"limit": int(limit)},
        )
        return [
            {
                "status": "error",
                "severity": "error",
                "kind": "announcement_failure_check_error",
                "title": "Could not inspect announcement document failures",
                "message": f"{type(exc).__name__}: {exc}",
                "source": table,
                "suggested_fix": "Run python -m advisory.operator_health --skip-dhan and inspect DB connectivity.",
                "recovered": False,
            }
        ]


def build_degradation_feed(sections: dict[str, Any], *, log_dir: str | Path = DEFAULT_LOG_DIR, limit: int = 100, include_deep_checks: bool = True) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    downloader_state = sections.get("downloader_run_state") if isinstance(sections.get("downloader_run_state"), dict) else {}
    for row in downloader_state.get("rows") or []:
        if not isinstance(row, dict) or row.get("status") == "ok":
            continue
        classification = str(row.get("classification") or "unknown")
        classification_meta = ingestion_classification_metadata(classification)
        rows.append(
            {
                "status": row.get("status") or "warn",
                "severity": row.get("status") or "warn",
                "kind": "download_runner_state",
                "title": f"Downloader/parser run issue: {row.get('module') or row.get('source_name') or 'unknown'}",
                "message": str(row.get("error") or f"classification={classification}, phase={row.get('phase') or 'unknown'}"),
                "source": row.get("source_name") or "download_runner",
                "observed_at": row.get("updated_at"),
                "suggested_fix": classification_meta["operator_action"],
                "recovered": False,
                "details": {
                    "module": row.get("module"),
                    "purpose": row.get("purpose"),
                    "classification": classification,
                    "classification_label": classification_meta["label"],
                    "classification_meaning": classification_meta["meaning"],
                    "trust_impact": classification_meta["trust_impact"],
                    "phase": row.get("phase"),
                    "rows": row.get("rows"),
                    "rows_written": row.get("rows_written"),
                    "state_advanced": row.get("state_advanced"),
                    "fallback_used": row.get("fallback_used"),
                },
            }
        )

    ingestion_state = sections.get("ingestion_file_state") if isinstance(sections.get("ingestion_file_state"), dict) else {}
    for row in ingestion_state.get("active_failure_sample_rows") or []:
        if not isinstance(row, dict):
            continue
        classification = str(row.get("classification") or "unknown")
        classification_meta = ingestion_classification_metadata(classification)
        rows.append(
            {
                "status": "error" if classification in {"parser_bug", "schema_changed"} else "warn",
                "severity": "error" if classification in {"parser_bug", "schema_changed"} else "warn",
                "kind": "ingestion_file_failure",
                "title": f"Recent ingestion failure: {row.get('source_prefix') or 'unknown'}",
                "message": str(row.get("error_message") or f"classification={classification}"),
                "source": row.get("source_prefix") or "ingestion_file_state",
                "observed_at": row.get("processed_at"),
                "suggested_fix": classification_meta["operator_action"],
                "recovered": False,
                "details": {
                    "object_key": row.get("object_key"),
                    "classification": classification,
                    "classification_label": classification_meta["label"],
                    "classification_meaning": classification_meta["meaning"],
                    "trust_impact": classification_meta["trust_impact"],
                    "failure_lifecycle": row.get("failure_lifecycle"),
                    "active_failure_days": ingestion_state.get("active_failure_days"),
                },
            }
        )
    for row in ingestion_state.get("stale_historical_failure_sample_rows") or []:
        if not isinstance(row, dict):
            continue
        classification = str(row.get("classification") or "unknown")
        classification_meta = ingestion_classification_metadata(classification)
        rows.append(
            {
                "status": "warn",
                "severity": "warn",
                "kind": "stale_historical_ingestion_failure",
                "title": f"Suppressed stale parser/file-state failure: {row.get('source_prefix') or 'unknown'}",
                "message": str(row.get("error_message") or f"classification={classification}"),
                "source": row.get("source_prefix") or "ingestion_file_state",
                "observed_at": row.get("processed_at"),
                "suggested_fix": f"Suppressed from active blockers. {classification_meta['operator_action']}",
                "recovered": True,
                "details": {
                    "object_key": row.get("object_key"),
                    "classification": classification,
                    "classification_label": classification_meta["label"],
                    "classification_meaning": classification_meta["meaning"],
                    "trust_impact": classification_meta["trust_impact"],
                    "failure_lifecycle": row.get("failure_lifecycle"),
                    "active_failure_days": ingestion_state.get("active_failure_days"),
                    "suppressed_from_active_blockers": True,
                },
            }
        )

    for row in sections.get("sync_state_failures") or []:
        if not isinstance(row, dict) or row.get("status") == "ok":
            continue
        message = str(row.get("error") or row.get("message") or row.get("state_json") or "")
        extracted = _extract_degradation_from_line(message, source=str(row.get("source_name") or "advisory_sync_state"), observed_at=row.get("updated_at"))
        if extracted:
            rows.extend(extracted)
        else:
            rows.append(
                {
                    "status": row.get("status") or "warn",
                    "severity": row.get("status") or "warn",
                    "kind": "sync_state_issue",
                    "title": f"Sync-state issue: {row.get('source_name') or 'unknown'}",
                    "message": message or "Sync-state row is non-ok.",
                    "source": row.get("source_name") or "advisory_sync_state",
                    "observed_at": row.get("updated_at"),
                    "suggested_fix": "Run the relevant watcher/data script and then python -m advisory.operator_health --skip-dhan.",
                    "recovered": False,
                }
            )

    for row in sections.get("cron_logs") or []:
        if not isinstance(row, dict):
            continue
        source = str(row.get("log_file") or "cron")
        observed_at = row.get("modified_at")
        for line in list(row.get("recent_errors") or []) + list(row.get("recent_tail_errors") or []) + list(row.get("degradation_markers") or []):
            rows.extend(_extract_degradation_from_line(str(line), source=source, observed_at=observed_at, recovered=False))
        for line in list(row.get("historical_errors") or []):
            rows.extend(_extract_degradation_from_line(str(line), source=source, observed_at=observed_at, recovered=True))

    api_errors = sections.get("operator_api_errors") if isinstance(sections.get("operator_api_errors"), dict) else {}
    for row in api_errors.get("rows") or []:
        if not isinstance(row, dict):
            continue
        status_code = int(row.get("status_code") or 500)
        message_text = str(row.get("error_message") or "")
        is_trace_cache_miss = "trace_summary_cache_miss" in message_text
        rows.append(
            {
                "status": "error" if status_code >= 500 else "warn",
                "severity": "error" if status_code >= 500 else "warn",
                "kind": "trace_summary_cache_miss" if is_trace_cache_miss else "operator_api_error",
                "title": "Trace summary cache miss" if is_trace_cache_miss else "Operator API endpoint failed",
                "message": f"{row.get('route') or row.get('operation')}: {row.get('error_type')}: {row.get('error_message')}",
                "source": row.get("route") or "operator_api",
                "observed_at": row.get("occurred_at"),
                "suggested_fix": "Run the trace summary rebuild command from Operations." if is_trace_cache_miss else "Open Operations or Health, inspect the API error traceback tail, then rerun the failed endpoint after fixing the source issue.",
                "recovered": False,
                "details": {
                    "error_id": row.get("error_id"),
                    "operation": row.get("operation"),
                    "status_code": status_code,
                    "traceback_tail": row.get("traceback_tail"),
                },
            }
        )

    screener_failures = sections.get("screener_failures") if isinstance(sections.get("screener_failures"), dict) else {}
    for row in screener_failures.get("rows") or []:
        if not isinstance(row, dict):
            continue
        stage = str(row.get("failure_stage") or "unknown")
        rows.append(
            {
                "status": "warn",
                "severity": "warn",
                "kind": f"screener_{stage}",
                "title": "Screener.in query/fetch/parse failure",
                "message": str(row.get("error_message") or row.get("body_excerpt") or stage),
                "source": row.get("screener_url") or "screenerin_parse_failures",
                "observed_at": row.get("observed_at"),
                "suggested_fix": "Fix the Screener.in query syntax or login/session issue, then rerun the affected screener workflow.",
                "recovered": False,
                "details": {
                    "failure_id": row.get("failure_id"),
                    "failure_stage": stage,
                    "query_name": row.get("query_name"),
                    "query_hash": row.get("query_hash"),
                    "has_login_form": row.get("has_login_form"),
                    "has_page_results_container": row.get("has_page_results_container"),
                    "error_type": row.get("error_type"),
                },
            }
        )

    slow = sections.get("slow_operations") if isinstance(sections.get("slow_operations"), dict) else {}
    for issue in slow.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        rows.append(
            {
                "status": "warn",
                "severity": "warn",
                "kind": "slow_operation",
                "title": "Slow operation open",
                "message": str(issue.get("operation") or issue.get("kind") or "Slow operation"),
                "source": "slow_operation_state",
                "observed_at": issue.get("last_seen_at"),
                "suggested_fix": "Inspect slow-operation report and materialize/paginate expensive endpoints or queries.",
                "recovered": False,
                "details": issue,
            }
        )
    for issue in slow.get("historical_issues") or []:
        if not isinstance(issue, dict):
            continue
        rows.append(
            {
                "status": "ok",
                "severity": "info",
                "kind": "slow_operation_historical",
                "title": "Historical slow operation not reproduced",
                "message": str(issue.get("operation") or issue.get("kind") or "Slow operation"),
                "source": "slow_operation_state",
                "observed_at": issue.get("last_seen_at"),
                "suggested_fix": "Keep as historical context. Reprobe or mark fixed/triaged only if this route becomes slow again.",
                "recovered": True,
                "details": issue,
            }
        )

    api_latency = sections.get("api_latency_probe") if isinstance(sections.get("api_latency_probe"), dict) else {}
    if api_latency.get("status") in {"warn", "error"}:
        rows.append(
            {
                "status": api_latency.get("status"),
                "severity": api_latency.get("status"),
                "kind": "api_latency_probe",
                "title": "Operator API latency probe needs attention",
                "message": str(api_latency.get("message") or "API latency probe is stale, slow, or failed."),
                "source": api_latency.get("path") or "api_latency_probe",
                "observed_at": api_latency.get("generated_at"),
                "suggested_fix": "Run API latency probe and inspect slow-operation report.",
                "recovered": False,
                "details": api_latency,
            }
        )

    fallback_telemetry = sections.get("fallback_telemetry") if isinstance(sections.get("fallback_telemetry"), dict) else {}
    for row in fallback_telemetry.get("rows") or []:
        if not isinstance(row, dict):
            continue
        rows.append(
            {
                "status": row.get("severity") or row.get("status") or "warn",
                "severity": row.get("severity") or "warn",
                "kind": row.get("fallback_type") or "fallback_event",
                "title": "Fallback telemetry event",
                "message": row.get("reason") or row.get("error_message") or "A fallback/degraded path was used.",
                "source": row.get("source") or row.get("module") or "fallback_telemetry",
                "symbol": row.get("symbol"),
                "unique_id": row.get("unique_id"),
                "observed_at": row.get("observed_at"),
                "suggested_fix": "Inspect the source module and rerun the affected pipeline after the fallback cause is fixed.",
                "recovered": False,
                "details": {
                    "event_id": row.get("event_id"),
                    "module": row.get("module"),
                    "fallback_type": row.get("fallback_type"),
                    "error_type": row.get("error_type"),
                    "error_message": row.get("error_message"),
                    "metadata_json": row.get("metadata_json"),
                },
            }
        )

    if include_deep_checks:
        rows.extend(check_announcement_document_failures())
    rows = _dedupe_degradations(rows, limit=limit)
    active = [row for row in rows if not row.get("recovered")]
    recovered = [row for row in rows if row.get("recovered")]
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get("kind") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    status = "error" if any(row.get("status") == "error" for row in active) else "warn" if active else "ok"
    return {
        "status": status,
        "active_count": len(active),
        "recovered_count": len(recovered),
        "counts_by_kind": counts,
        "lifecycle": build_degradation_lifecycle_groups(rows, limit=limit, include_superseded_preview=include_deep_checks),
        "rows": rows,
    }


def check_optional_dependencies() -> list[dict[str, Any]]:
    poppler_path = env("POPPLER_PATH", default="")
    path_prefixes = [poppler_path] if poppler_path else []
    pdfinfo = next((str(Path(prefix) / "pdfinfo") for prefix in path_prefixes if (Path(prefix) / "pdfinfo").exists()), None) or shutil.which("pdfinfo")
    pdftoppm = next((str(Path(prefix) / "pdftoppm") for prefix in path_prefixes if (Path(prefix) / "pdftoppm").exists()), None) or shutil.which("pdftoppm")
    node = shutil.which("node")
    npm = shutil.which("npm")
    codex = shutil.which(env("CODEX_CLI_BIN", default="codex"))
    deps = [
        _status("ok" if pdfinfo and pdftoppm else "warn", "Poppler utilities detected." if pdfinfo and pdftoppm else "Poppler utilities missing; announcement PDF OCR may fail.", pdfinfo=pdfinfo, pdftoppm=pdftoppm),
        _status("ok" if node and npm else "warn", "Node/npm detected." if node and npm else "Node/npm missing; operator frontend may fail.", node=node, npm=npm),
        _status("ok" if codex else "warn", "Codex CLI detected." if codex else "Codex CLI missing; LLM/Codex flows may fail.", codex=codex),
    ]
    for module_name in ["torch", "timesfm"]:
        found = importlib.util.find_spec(module_name) is not None
        deps.append(_status("ok" if found else "warn", f"Python module {module_name} detected." if found else f"Python module {module_name} missing.", module=module_name))
    return deps


def check_frontend_dependencies() -> dict[str, Any]:
    web_dir = Path("apps/operator-web")
    node_modules = web_dir / "node_modules"
    package_lock = web_dir / "package-lock.json"
    if not web_dir.exists():
        return _status("warn", "Operator frontend directory is missing.", path=str(web_dir))
    try:
        node_version = subprocess.run(["node", "--version"], check=False, capture_output=True, text=True, timeout=5)
        npm_version = subprocess.run(["npm", "--version"], check=False, capture_output=True, text=True, timeout=5)
    except Exception as exc:
        _record_health_local_fallback(
            source="apps/operator-web",
            fallback_type="operator_health_frontend_dependencies_check_failed",
            reason="Operator Health could not run node/npm frontend dependency checks.",
            error=exc,
            severity="warn",
            metadata={"web_dir": str(web_dir)},
        )
        return _status("warn", "Could not run node/npm.", error=f"{type(exc).__name__}: {exc}")
    severity = "ok" if node_modules.exists() and node_version.returncode == 0 and npm_version.returncode == 0 else "warn"
    return _status(
        severity,
        "Frontend dependencies look installed." if severity == "ok" else "Frontend dependency check found missing pieces.",
        node_modules=node_modules.exists(),
        package_lock=package_lock.exists(),
        node_version=node_version.stdout.strip(),
        npm_version=npm_version.stdout.strip(),
    )


def check_frontend_runtime() -> dict[str, Any]:
    url = env("OPERATOR_WEB_HEALTH_URL", default=DEFAULT_OPERATOR_WEB_URL)
    timeout_seconds = env.float("OPERATOR_WEB_HEALTH_TIMEOUT_SECONDS", default=3.0)
    started = time.monotonic()
    try:
        response = requests.get(url, timeout=timeout_seconds)
        latency_ms = round((time.monotonic() - started) * 1000.0, 2)
        if response.ok:
            return _status("ok", "Operator web frontend responded.", url=url, latency_ms=latency_ms, status_code=response.status_code)
        return _status(
            "error",
            "Operator web frontend returned an unhealthy response.",
            url=url,
            latency_ms=latency_ms,
            status_code=response.status_code,
            response_text=response.text[:500],
        )
    except Exception as exc:
        latency_ms = round((time.monotonic() - started) * 1000.0, 2)
        _record_health_local_fallback(
            source=url,
            fallback_type="operator_health_frontend_runtime_check_failed",
            reason="Operator Health could not reach the operator web frontend.",
            error=exc,
            metadata={"url": url, "latency_ms": latency_ms},
        )
        return _status("error", "Operator web frontend is not reachable.", url=url, latency_ms=latency_ms, error=f"{type(exc).__name__}: {exc}")


def check_operator_api_errors(limit: int = 25) -> dict[str, Any]:
    try:
        if not table_exists(OPERATOR_API_ERRORS_TABLE):
            return _status("ok", "No operator API errors have been recorded yet.", rows=[], returned_count=0)
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
    except Exception as exc:
        _record_health_local_fallback(
            source=OPERATOR_API_ERRORS_TABLE,
            fallback_type="operator_health_api_errors_check_failed",
            reason="Operator Health could not inspect recent operator API errors.",
            error=exc,
            metadata={"limit": max(1, int(limit))},
        )
        return _status("error", "Could not inspect operator API errors.", error=f"{type(exc).__name__}: {exc}", rows=[])
    rows = _records(df)
    for row in rows:
        row["error_message"] = redact_text(row.get("error_message"))
        row["traceback_tail"] = redact_text(row.get("traceback_tail"))
        request_context_json = row.pop("request_context_json", None)
        row["request_context"] = _json_dict(
            request_context_json,
            source=OPERATOR_API_ERRORS_TABLE,
            fallback_type="operator_health_api_error_context_parse_failed",
            metadata={"occurred_at": row.get("occurred_at"), "route": row.get("route")},
        )
        if isinstance(row["request_context"], dict):
            row["request_context"] = redact_mapping(row["request_context"])
    active_errors = [row for row in rows if int(row.get("status_code") or 500) >= 500]
    active_warnings = [row for row in rows if int(row.get("status_code") or 500) < 500]
    status = "error" if active_errors else "warn" if active_warnings else "ok"
    return _status(
        status,
        "Recent operator API errors found." if rows else "No recent operator API errors.",
        rows=rows,
        returned_count=len(rows),
        error_count=len(active_errors),
        warning_count=len(active_warnings),
    )


def check_trace_summaries() -> dict[str, Any]:
    try:
        if not table_exists(TRACE_SUMMARIES_TABLE):
            return _status("warn", "Trace summary cache table is missing.", row_count=0)
        df = sql_to_df(
            f"""
            SELECT
                entity_type,
                count(*) AS row_count,
                max(generated_at) AS latest_generated_at,
                max(source_max_ts) AS latest_source_max_ts
            FROM {TRACE_SUMMARIES_TABLE}
            GROUP BY entity_type
            ORDER BY entity_type
            """,
            retries=3,
        )
    except Exception as exc:
        _record_health_local_fallback(
            source=TRACE_SUMMARIES_TABLE,
            fallback_type="operator_health_trace_summaries_check_failed",
            reason="Operator Health could not inspect the trace summary cache.",
            error=exc,
        )
        return _status("error", "Could not inspect trace summary cache.", error=f"{type(exc).__name__}: {exc}")
    rows = _records(df)
    total = sum(int(row.get("row_count") or 0) for row in rows)
    latest_values = pd.to_datetime([row.get("latest_generated_at") for row in rows], utc=True, errors="coerce")
    latest_values = latest_values[~pd.isna(latest_values)]
    latest_at = latest_values.max() if len(latest_values) else None
    age_hours = None if latest_at is None else (pd.Timestamp.utcnow() - latest_at).total_seconds() / 3600.0
    status = "warn" if total == 0 or (age_hours is not None and age_hours > 24) else "ok"
    return _status(
        status,
        "Trace summary cache is warm." if status == "ok" else "Trace summary cache is missing, empty, or stale.",
        row_count=total,
        latest_generated_at=None if latest_at is None else latest_at.isoformat(),
        age_hours=age_hours,
        rows=rows,
    )


def check_identity_issues(limit: int = 10) -> dict[str, Any]:
    active_action_coverage = check_active_action_identity_coverage(limit=limit)
    try:
        if not table_exists(IDENTITY_ISSUES_TABLE):
            status = "ok" if active_action_coverage.get("status") == "ok" else str(active_action_coverage.get("status") or "warn")
            return _status(
                status,
                "No identity issue table exists yet." if status == "ok" else "Active action identity coverage needs attention.",
                open_count=0,
                rows=[],
                active_action_identity_coverage=active_action_coverage,
            )
        df = sql_to_df(
            f"""
            SELECT *
            FROM {IDENTITY_ISSUES_TABLE}
            WHERE COALESCE(status, 'open') IN ('open', 'active')
            ORDER BY last_seen_at DESC NULLS LAST, first_seen_at DESC NULLS LAST
            LIMIT %(limit)s
            """,
            params={"limit": max(1, int(limit))},
            retries=3,
            statement_timeout_ms=10000,
        )
        count_df = sql_to_df(
            f"""
            SELECT COUNT(*) AS open_count
            FROM {IDENTITY_ISSUES_TABLE}
            WHERE COALESCE(status, 'open') IN ('open', 'active')
            """,
            retries=3,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        _record_health_local_fallback(
            source=IDENTITY_ISSUES_TABLE,
            fallback_type="operator_health_identity_issues_check_failed",
            reason="Operator Health could not inspect open identity issues.",
            error=exc,
            metadata={"limit": max(1, int(limit))},
        )
        return _status(
            "error",
            "Could not inspect identity issues.",
            error=f"{type(exc).__name__}: {exc}",
            open_count=None,
            rows=[],
            active_action_identity_coverage=active_action_coverage,
        )
    open_count = int(count_df.iloc[0].get("open_count") or 0) if not count_df.empty else len(df)
    coverage_status = str(active_action_coverage.get("status") or "ok")
    status = "error" if coverage_status == "error" else "warn" if open_count or coverage_status == "warn" else "ok"
    message = "No open identity issues."
    if open_count and coverage_status == "warn":
        message = "Open identity issues and active action identity gaps exist."
    elif open_count:
        message = "Open Dhan/company identity issues exist."
    elif coverage_status == "warn":
        message = "Active broker-capable actions have missing company/security identity."
    elif coverage_status == "error":
        message = "Could not verify active action identity coverage."
    return _status(
        status,
        message,
        open_count=open_count,
        rows=_records(df),
        active_action_identity_coverage=active_action_coverage,
    )


def check_active_action_identity_coverage(limit: int = 10) -> dict[str, Any]:
    try:
        if not table_exists(ACTION_RECOMMENDATIONS_TABLE):
            return _status("ok", "No action recommendations table exists yet.", missing_count=0, rows=[])
        if not table_exists(COMPANY_MASTER_TABLE):
            return _status("warn", "Company master table is missing; active action identity coverage cannot be verified.", missing_count=None, rows=[])
        df = sql_to_df(
            f"""
            WITH latest_actions AS (
                SELECT *
                FROM {ACTION_RECOMMENDATIONS_TABLE}
                WHERE asof_date = (SELECT MAX(asof_date) FROM {ACTION_RECOMMENDATIONS_TABLE})
                  AND UPPER(COALESCE(action_code, '')) = ANY(%(action_codes)s)
                  AND COALESCE(execution_mode, '') <> 'no_broker_execution'
            ),
            mapped AS (
                SELECT
                    a.asof_date,
                    a.symbol,
                    a.action_code,
                    a.action_source,
                    a.execution_mode,
                    a.reason_contract_status,
                    a.action_reason,
                    cm.company_master_id,
                    cm.nse_ticker,
                    cm.bse_ticker,
                    cm.dhan_nse_id,
                    cm.dhan_bse_id,
                    CASE
                        WHEN cm.company_master_id IS NULL THEN 'missing_company_master'
                        WHEN cm.dhan_nse_id IS NULL AND cm.dhan_bse_id IS NULL THEN 'missing_dhan_security_id'
                        ELSE NULL
                    END AS identity_gap
                FROM latest_actions a
                LEFT JOIN company_master cm
                  ON UPPER(cm.nse_ticker) = UPPER(a.symbol)
                  OR UPPER(cm.bse_ticker) = UPPER(a.symbol)
            )
            SELECT *
            FROM mapped
            WHERE identity_gap IS NOT NULL
            ORDER BY asof_date DESC NULLS LAST, symbol
            LIMIT %(limit)s
            """,
            params={"action_codes": list(BROKER_CAPABLE_ACTION_CODES), "limit": max(1, int(limit))},
            retries=3,
            statement_timeout_ms=10000,
        )
        count_df = sql_to_df(
            f"""
            WITH latest_actions AS (
                SELECT *
                FROM {ACTION_RECOMMENDATIONS_TABLE}
                WHERE asof_date = (SELECT MAX(asof_date) FROM {ACTION_RECOMMENDATIONS_TABLE})
                  AND UPPER(COALESCE(action_code, '')) = ANY(%(action_codes)s)
                  AND COALESCE(execution_mode, '') <> 'no_broker_execution'
            ),
            mapped AS (
                SELECT
                    a.symbol,
                    CASE
                        WHEN cm.company_master_id IS NULL THEN 'missing_company_master'
                        WHEN cm.dhan_nse_id IS NULL AND cm.dhan_bse_id IS NULL THEN 'missing_dhan_security_id'
                        ELSE NULL
                    END AS identity_gap
                FROM latest_actions a
                LEFT JOIN company_master cm
                  ON UPPER(cm.nse_ticker) = UPPER(a.symbol)
                  OR UPPER(cm.bse_ticker) = UPPER(a.symbol)
            )
            SELECT COUNT(*) AS missing_count
            FROM mapped
            WHERE identity_gap IS NOT NULL
            """,
            params={"action_codes": list(BROKER_CAPABLE_ACTION_CODES)},
            retries=3,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        _record_health_local_fallback(
            source=ACTION_RECOMMENDATIONS_TABLE,
            fallback_type="operator_health_active_action_identity_coverage_failed",
            reason="Operator Health could not verify active broker-capable action identity coverage.",
            error=exc,
            metadata={"limit": max(1, int(limit)), "action_codes": list(BROKER_CAPABLE_ACTION_CODES)},
        )
        return _status("error", "Could not verify active action identity coverage.", error=f"{type(exc).__name__}: {exc}", missing_count=None, rows=[])
    missing_count = int(count_df.iloc[0].get("missing_count") or 0) if not count_df.empty else len(df)
    return _status(
        "warn" if missing_count else "ok",
        "Active broker-capable actions have missing company/security identity." if missing_count else "Active broker-capable actions have company/security identity coverage.",
        missing_count=missing_count,
        broker_capable_action_codes=list(BROKER_CAPABLE_ACTION_CODES),
        rows=_records(df),
    )


def check_screener_failures(*, hours: int = 24, limit: int = 10) -> dict[str, Any]:
    window_hours = max(1, int(hours))
    try:
        if not table_exists(SCREENER_FAILURES_TABLE):
            return _status("ok", "No Screener.in failure table exists yet.", active_count=0, rows=[])
        rows_df = sql_to_df(
            f"""
            SELECT *
            FROM {SCREENER_FAILURES_TABLE}
            WHERE observed_at >= now() - (%(hours)s || ' hours')::interval
            ORDER BY observed_at DESC
            LIMIT %(limit)s
            """,
            params={"hours": window_hours, "limit": max(1, int(limit))},
            retries=3,
            statement_timeout_ms=10000,
        )
        counts_df = sql_to_df(
            f"""
            SELECT
                COUNT(*) AS active_count,
                SUM(CASE WHEN failure_stage = 'ad_hoc_validation' THEN 1 ELSE 0 END) AS validation_count,
                SUM(CASE WHEN failure_stage LIKE '%fetch' THEN 1 ELSE 0 END) AS fetch_count,
                SUM(CASE WHEN failure_stage LIKE '%parse' THEN 1 ELSE 0 END) AS parse_count
            FROM {SCREENER_FAILURES_TABLE}
            WHERE observed_at >= now() - (%(hours)s || ' hours')::interval
            """,
            params={"hours": window_hours},
            retries=3,
            statement_timeout_ms=10000,
        )
        by_stage_df = sql_to_df(
            f"""
            SELECT failure_stage, COUNT(*) AS count
            FROM {SCREENER_FAILURES_TABLE}
            WHERE observed_at >= now() - (%(hours)s || ' hours')::interval
            GROUP BY failure_stage
            ORDER BY count DESC, failure_stage
            """,
            params={"hours": window_hours},
            retries=3,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        _record_health_local_fallback(
            source=SCREENER_FAILURES_TABLE,
            fallback_type="operator_health_screener_failures_check_failed",
            reason="Operator Health could not inspect recent Screener.in failures.",
            error=exc,
            severity="warn",
            metadata={"hours": window_hours, "limit": max(1, int(limit))},
        )
        return _status("error", "Could not inspect Screener.in failures.", error=f"{type(exc).__name__}: {exc}", active_count=None, rows=[])
    count_row = counts_df.iloc[0].to_dict() if not counts_df.empty else {}
    active_count = int(count_row.get("active_count") or 0)
    validation_count = int(count_row.get("validation_count") or 0)
    fetch_count = int(count_row.get("fetch_count") or 0)
    parse_count = int(count_row.get("parse_count") or 0)
    status = "warn" if active_count else "ok"
    return _status(
        status,
        "Recent Screener.in query/fetch/parse failures found." if active_count else "No recent Screener.in failures.",
        window_hours=window_hours,
        active_count=active_count,
        validation_count=validation_count,
        fetch_count=fetch_count,
        parse_count=parse_count,
        counts_by_stage={str(row["failure_stage"]): int(row["count"] or 0) for row in by_stage_df.to_dict(orient="records")} if not by_stage_df.empty else {},
        rows=_records(rows_df),
    )


def check_context_gate_policy() -> dict[str, Any]:
    try:
        snapshot = current_context_gate_policy_snapshot()
    except Exception as exc:
        _record_health_local_fallback(
            source="advisory.recommendation_diagnostics",
            fallback_type="operator_health_context_gate_policy_check_failed",
            reason="Operator Health could not inspect the current context/regime gate policy.",
            error=exc,
        )
        return _status(
            "error",
            "Could not inspect context/regime gate policy.",
            error=f"{type(exc).__name__}: {exc}",
            broker_execution_allowed=False,
            portfolio_authority="none",
        )
    exposure = snapshot.get("single_regime_hard_gate_exposure") if isinstance(snapshot.get("single_regime_hard_gate_exposure"), dict) else {}
    active_single = list(exposure.get("active_single_regime_flags") or [])
    active_context = list(exposure.get("active_context_hard_flags") or [])
    if active_single:
        status = "warn"
        message = "A broad single-regime hard gate is enabled."
        reasons = ["active_single_regime_hard_gate"]
    else:
        status = "ok"
        message = "Broad regime labels are diagnostic/context-only by current env policy."
        reasons = []
    return _status(
        status,
        message,
        reasons=reasons,
        active_single_regime_flags=active_single,
        active_context_hard_flags=active_context,
        global_regime_label_blocks_buy=bool(exposure.get("global_regime_label_blocks_buy")),
        policy_summary=snapshot.get("policy_summary") if isinstance(snapshot.get("policy_summary"), dict) else {},
        env=snapshot.get("env") if isinstance(snapshot.get("env"), dict) else {},
        operator_action=exposure.get("operator_action"),
        command="python scripts/context_gate_policy_audit.py --fail-on-single-regime",
        authority="read_only_env_policy_audit",
        portfolio_authority="none",
        broker_execution_allowed=False,
        policy_auto_promotion_allowed=False,
    )


def check_technical_threshold_evidence() -> dict[str, Any]:
    command = "python -m advisory.technical_threshold_calibration --horizons 5 10 20"
    promotion_command = (
        "python -m advisory.technical_threshold_promotion "
        "--setup-id EVENT_OPPORTUNITY_V1 --config-id <config_id> --dry-run"
    )
    if not table_exists(TECHNICAL_THRESHOLD_EVALUATIONS_TABLE) or not table_exists(TECHNICAL_THRESHOLD_SUMMARY_TABLE):
        missing = [
            table
            for table in [TECHNICAL_THRESHOLD_EVALUATIONS_TABLE, TECHNICAL_THRESHOLD_SUMMARY_TABLE]
            if not table_exists(table)
        ]
        return _status(
            "warn",
            "Technical-threshold calibration tables do not exist yet.",
            usable=False,
            ready_for_policy_review=False,
            reasons=["missing_technical_threshold_evidence_tables"],
            missing_tables=missing,
            command=command,
            promotion_review_command=promotion_command,
            authority="research_only",
            broker_execution_allowed=False,
            policy_auto_promotion_allowed=False,
        )

    summary_columns = table_columns(TECHNICAL_THRESHOLD_SUMMARY_TABLE)
    required_columns = {
        "best_config_id",
        "best_eligible_count",
        "best_avg_forward_return_after_cost",
        "baseline_avg_forward_return_after_cost",
        "recommendation",
    }
    missing_summary_columns = sorted(required_columns - summary_columns)
    best_config_expr = "best_config_id" if "best_config_id" in summary_columns else "NULL::TEXT AS best_config_id"
    best_eligible_expr = (
        "best_eligible_count" if "best_eligible_count" in summary_columns else "NULL::BIGINT AS best_eligible_count"
    )
    best_return_expr = (
        "best_avg_forward_return_after_cost"
        if "best_avg_forward_return_after_cost" in summary_columns
        else "NULL::DOUBLE PRECISION AS best_avg_forward_return_after_cost"
    )
    baseline_return_expr = (
        "baseline_avg_forward_return_after_cost"
        if "baseline_avg_forward_return_after_cost" in summary_columns
        else "NULL::DOUBLE PRECISION AS baseline_avg_forward_return_after_cost"
    )
    best_hit_expr = (
        "best_hit_rate_after_cost"
        if "best_hit_rate_after_cost" in summary_columns
        else "NULL::DOUBLE PRECISION AS best_hit_rate_after_cost"
    )
    baseline_hit_expr = (
        "baseline_hit_rate_after_cost"
        if "baseline_hit_rate_after_cost" in summary_columns
        else "NULL::DOUBLE PRECISION AS baseline_hit_rate_after_cost"
    )
    recommendation_expr = "recommendation" if "recommendation" in summary_columns else "NULL::TEXT AS recommendation"
    review_candidate_expr = (
        "SUM(CASE WHEN recommendation = 'review_for_promotion' THEN 1 ELSE 0 END)"
        if "recommendation" in summary_columns
        else "0"
    )
    bounded_expr = (
        "SUM(CASE WHEN recommendation = 'bounded_first_pass_only' THEN 1 ELSE 0 END)"
        if "recommendation" in summary_columns
        else "0"
    )
    enough_signals_expr = (
        f"SUM(CASE WHEN best_eligible_count >= {int(TECHNICAL_THRESHOLD_MIN_SIGNALS)} THEN 1 ELSE 0 END)"
        if "best_eligible_count" in summary_columns
        else "0"
    )
    lift_expr = (
        "SUM(CASE WHEN best_avg_forward_return_after_cost > baseline_avg_forward_return_after_cost THEN 1 ELSE 0 END)"
        if {"best_avg_forward_return_after_cost", "baseline_avg_forward_return_after_cost"}.issubset(summary_columns)
        else "0"
    )
    positive_return_expr = (
        "SUM(CASE WHEN best_avg_forward_return_after_cost > 0 THEN 1 ELSE 0 END)"
        if "best_avg_forward_return_after_cost" in summary_columns
        else "0"
    )
    non_positive_return_expr = (
        "SUM(CASE WHEN best_avg_forward_return_after_cost <= 0 THEN 1 ELSE 0 END)"
        if "best_avg_forward_return_after_cost" in summary_columns
        else "0"
    )
    no_lift_expr = (
        "SUM(CASE WHEN best_avg_forward_return_after_cost <= baseline_avg_forward_return_after_cost THEN 1 ELSE 0 END)"
        if {"best_avg_forward_return_after_cost", "baseline_avg_forward_return_after_cost"}.issubset(summary_columns)
        else "0"
    )
    try:
        eval_df = sql_to_df(
            f"""
            SELECT
                MAX(evaluated_at) AS latest_evaluated_at,
                MAX(load_ts) AS latest_load_ts,
                COUNT(*) AS evaluation_rows,
                MAX(signal_count) AS max_matured_signal_count,
                MAX(eligible_count) AS max_eligible_count,
                SUM(CASE WHEN objective_score IS NOT NULL THEN 1 ELSE 0 END) AS objective_scored_rows,
                COUNT(DISTINCT horizon_days) AS horizon_count
            FROM {TECHNICAL_THRESHOLD_EVALUATIONS_TABLE}
            """,
            retries=3,
            statement_timeout_ms=10000,
        )
        summary_df = sql_to_df(
            f"""
            SELECT
                MAX(evaluated_at) AS latest_evaluated_at,
                COUNT(*) AS summary_rows,
                {review_candidate_expr} AS review_candidate_count,
                {bounded_expr} AS bounded_first_pass_count,
                {enough_signals_expr} AS sufficiently_matured_group_count,
                {lift_expr} AS lift_over_baseline_count,
                {positive_return_expr} AS positive_after_cost_count,
                {non_positive_return_expr} AS non_positive_after_cost_count,
                {no_lift_expr} AS no_lift_over_baseline_count
            FROM {TECHNICAL_THRESHOLD_SUMMARY_TABLE}
            """,
            retries=3,
            statement_timeout_ms=10000,
        )
        groups = sql_to_df(
            f"""
            SELECT
                evaluated_at,
                horizon_days,
                {best_config_expr},
                {best_eligible_expr},
                {best_return_expr},
                {baseline_return_expr},
                {best_hit_expr},
                {baseline_hit_expr},
                {recommendation_expr}
            FROM {TECHNICAL_THRESHOLD_SUMMARY_TABLE}
            ORDER BY evaluated_at DESC, horizon_days
            LIMIT 10
            """,
            retries=3,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        _record_health_local_fallback(
            source=TECHNICAL_THRESHOLD_SUMMARY_TABLE,
            fallback_type="operator_health_technical_threshold_evidence_failed",
            reason="Operator Health could not inspect technical-threshold calibration evidence.",
            error=exc,
        )
        return _status(
            "error",
            "Could not inspect technical-threshold calibration evidence.",
            error=f"{type(exc).__name__}: {exc}",
            usable=False,
            ready_for_policy_review=False,
            command=command,
            promotion_review_command=promotion_command,
            authority="research_only",
            broker_execution_allowed=False,
            policy_auto_promotion_allowed=False,
        )

    eval_row = eval_df.iloc[0] if not eval_df.empty else {}
    summary_row = summary_df.iloc[0] if not summary_df.empty else {}
    latest_eval = pd.to_datetime(eval_row.get("latest_evaluated_at"), utc=True, errors="coerce") if len(eval_row) else pd.NaT
    latest_load = pd.to_datetime(eval_row.get("latest_load_ts"), utc=True, errors="coerce") if len(eval_row) else pd.NaT
    evaluation_rows = int(eval_row.get("evaluation_rows") or 0) if len(eval_row) else 0
    max_matured_signal_count = int(eval_row.get("max_matured_signal_count") or 0) if len(eval_row) else 0
    max_eligible_count = int(eval_row.get("max_eligible_count") or 0) if len(eval_row) else 0
    objective_scored_rows = int(eval_row.get("objective_scored_rows") or 0) if len(eval_row) else 0
    horizon_count = int(eval_row.get("horizon_count") or 0) if len(eval_row) else 0
    summary_rows = int(summary_row.get("summary_rows") or 0) if len(summary_row) else 0
    review_candidate_count = int(summary_row.get("review_candidate_count") or 0) if len(summary_row) else 0
    bounded_first_pass_count = int(summary_row.get("bounded_first_pass_count") or 0) if len(summary_row) else 0
    sufficiently_matured_group_count = int(summary_row.get("sufficiently_matured_group_count") or 0) if len(summary_row) else 0
    lift_over_baseline_count = int(summary_row.get("lift_over_baseline_count") or 0) if len(summary_row) else 0
    positive_after_cost_count = int(summary_row.get("positive_after_cost_count") or 0) if len(summary_row) else 0
    non_positive_after_cost_count = int(summary_row.get("non_positive_after_cost_count") or 0) if len(summary_row) else 0
    no_lift_over_baseline_count = int(summary_row.get("no_lift_over_baseline_count") or 0) if len(summary_row) else 0
    reasons: list[str] = []
    if evaluation_rows <= 0 or summary_rows <= 0:
        reasons.append("empty_technical_threshold_evidence")
    if max_matured_signal_count < int(TECHNICAL_THRESHOLD_MIN_SIGNALS):
        reasons.append("insufficient_matured_technical_threshold_signals")
    if missing_summary_columns:
        reasons.append("technical_threshold_evidence_schema_missing_summary_columns")
    if bounded_first_pass_count > 0:
        reasons.append("technical_threshold_evidence_bounded_first_pass_only")
    if review_candidate_count <= 0:
        reasons.append("no_technical_threshold_review_candidate")
    if lift_over_baseline_count <= 0:
        reasons.append("no_technical_threshold_lift_over_baseline")
    if positive_after_cost_count <= 0:
        reasons.append("no_positive_technical_threshold_after_cost_return")
    if pd.isna(latest_eval):
        reasons.append("missing_latest_technical_threshold_evaluated_at")
        age_days = None
    else:
        age_days = max((pd.Timestamp.utcnow() - latest_eval).total_seconds() / 86400.0, 0.0)
        if age_days > 14:
            reasons.append("stale_technical_threshold_evidence")
    trigger_near_miss_research = _load_technical_threshold_near_miss_health(
        latest_evaluated_at=latest_eval,
        summary_columns=summary_columns,
    )
    status = "ok" if not reasons else "warn"
    ready_for_policy_review = bool(
        status == "ok"
        and review_candidate_count > 0
        and lift_over_baseline_count > 0
        and positive_after_cost_count > 0
        and max_matured_signal_count >= int(TECHNICAL_THRESHOLD_MIN_SIGNALS)
    )
    if status == "ok" and ready_for_policy_review:
        message = "Technical-threshold calibration has after-cost lift over baseline and is ready for offline review."
    elif missing_summary_columns:
        message = "Technical-threshold calibration summary is missing required columns; rerun the calibration migration/output."
    elif bounded_first_pass_count > 0:
        message = "Technical-threshold calibration is only a bounded first pass; do not use it for threshold review."
    elif lift_over_baseline_count <= 0:
        message = "Technical-threshold calibration has not shown after-cost lift over the baseline threshold set."
    elif positive_after_cost_count <= 0:
        message = "Technical-threshold calibration has no positive after-cost candidate; do not review thresholds for promotion."
    else:
        message = "Technical-threshold calibration is missing, stale, or lacks enough matured after-cost evidence."
    return _status(
        status,
        message,
        usable=status == "ok",
        ready_for_policy_review=ready_for_policy_review,
        reasons=reasons,
        evaluations_table=TECHNICAL_THRESHOLD_EVALUATIONS_TABLE,
        summary_table=TECHNICAL_THRESHOLD_SUMMARY_TABLE,
        latest_evaluated_at=_json_ready(latest_eval),
        latest_load_ts=_json_ready(latest_load),
        age_days=None if age_days is None else round(age_days, 3),
        evaluation_rows=evaluation_rows,
        summary_rows=summary_rows,
        max_matured_signal_count=max_matured_signal_count,
        max_eligible_count=max_eligible_count,
        objective_scored_rows=objective_scored_rows,
        horizon_count=horizon_count,
        review_candidate_count=review_candidate_count,
        bounded_first_pass_count=bounded_first_pass_count,
        sufficiently_matured_group_count=sufficiently_matured_group_count,
        lift_over_baseline_count=lift_over_baseline_count,
        positive_after_cost_count=positive_after_cost_count,
        non_positive_after_cost_count=non_positive_after_cost_count,
        no_lift_over_baseline_count=no_lift_over_baseline_count,
        missing_summary_columns=missing_summary_columns,
        trigger_near_miss_research=trigger_near_miss_research,
        trigger_near_miss_candidate_count=len(trigger_near_miss_research.get("top_relaxation_candidates") or []),
        trigger_near_miss_do_not_relax_count=len(trigger_near_miss_research.get("top_do_not_relax") or []),
        trigger_near_miss_needs_more_label_count=len(trigger_near_miss_research.get("needs_more_label_near_misses") or []),
        min_matured_signals=int(TECHNICAL_THRESHOLD_MIN_SIGNALS),
        groups=_records(groups),
        command=command,
        promotion_review_command=promotion_command,
        authority="research_only_manual_review",
        broker_execution_allowed=False,
        policy_auto_promotion_allowed=False,
    )


def _load_technical_threshold_near_miss_health(
    *,
    latest_evaluated_at: pd.Timestamp,
    summary_columns: set[str],
    min_signals: int = TECHNICAL_THRESHOLD_MIN_SIGNALS,
    return_threshold: float = 0.03,
    min_hit_rate: float = 0.5,
) -> dict[str, Any]:
    authority = {
        "authority": "research_only_manual_review",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
    }
    if "archetype_breakdown_json" not in summary_columns:
        return {
            "status": "missing_column",
            **authority,
            "near_miss_rows": 0,
            "classification_counts": {},
            "top_relaxation_candidates": [],
            "top_do_not_relax": [],
            "needs_more_label_near_misses": [],
        }
    if pd.isna(latest_evaluated_at):
        return {
            "status": "missing_latest_evaluated_at",
            **authority,
            "near_miss_rows": 0,
            "classification_counts": {},
            "top_relaxation_candidates": [],
            "top_do_not_relax": [],
            "needs_more_label_near_misses": [],
        }
    try:
        df = sql_to_df(
            f"""
            SELECT evaluated_at, horizon_days, archetype_breakdown_json
            FROM {TECHNICAL_THRESHOLD_SUMMARY_TABLE}
            WHERE evaluated_at = %(latest)s
            ORDER BY horizon_days
            """,
            params={"latest": latest_evaluated_at},
            retries=2,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        _record_health_local_fallback(
            source=TECHNICAL_THRESHOLD_SUMMARY_TABLE,
            fallback_type="operator_health_technical_threshold_near_miss_load_failed",
            reason="Operator Health could not load technical-threshold trigger near-miss evidence.",
            error=exc,
            metadata={"latest_evaluated_at": latest_evaluated_at.isoformat()},
        )
        return {
            "status": "error",
            **authority,
            "error": f"{type(exc).__name__}: {exc}",
            "near_miss_rows": 0,
            "classification_counts": {},
            "top_relaxation_candidates": [],
            "top_do_not_relax": [],
            "needs_more_label_near_misses": [],
        }

    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for row in _records(df):
        payload = _json_dict(
            row.get("archetype_breakdown_json"),
            source=TECHNICAL_THRESHOLD_SUMMARY_TABLE,
            fallback_type="operator_health_technical_threshold_near_miss_json_parse_failed",
            metadata={"horizon_days": row.get("horizon_days"), "evaluated_at": row.get("evaluated_at")},
        )
        horizon = int(row.get("horizon_days") or payload.get("horizon_days") or 0)
        for item in payload.get("trigger_near_miss_breakdown") or []:
            if not isinstance(item, dict):
                continue
            key = (
                str(item.get("near_miss_archetype") or "unknown"),
                str(item.get("near_miss_blocker_key") or "none"),
            )
            bucket = buckets.setdefault(
                key,
                {
                    "near_miss_archetype": key[0],
                    "near_miss_blocker_key": key[1],
                    "near_miss_blocker_codes": item.get("near_miss_blocker_codes")
                    if isinstance(item.get("near_miss_blocker_codes"), list)
                    else [],
                    "horizons": [],
                    "signal_count": 0,
                    "_return_weighted_sum": 0.0,
                    "_return_weight": 0,
                    "_hit_weighted_sum": 0.0,
                    "_hit_weight": 0,
                    "sample_blockers": item.get("sample_blockers") if isinstance(item.get("sample_blockers"), list) else [],
                },
            )
            if horizon and horizon not in bucket["horizons"]:
                bucket["horizons"].append(horizon)
            signal_count = int(item.get("signal_count") or 0)
            bucket["signal_count"] += signal_count
            avg_return = pd.to_numeric(item.get("avg_return_after_cost"), errors="coerce")
            if not pd.isna(avg_return) and signal_count > 0:
                bucket["_return_weighted_sum"] += float(avg_return) * signal_count
                bucket["_return_weight"] += signal_count
            hit_rate = pd.to_numeric(item.get("hit_rate_after_cost"), errors="coerce")
            if not pd.isna(hit_rate) and signal_count > 0:
                bucket["_hit_weighted_sum"] += float(hit_rate) * signal_count
                bucket["_hit_weight"] += signal_count

    rows: list[dict[str, Any]] = []
    for bucket in buckets.values():
        avg_return = None if int(bucket["_return_weight"]) == 0 else round(float(bucket["_return_weighted_sum"] / bucket["_return_weight"]), 6)
        hit_rate = None if int(bucket["_hit_weight"]) == 0 else round(float(bucket["_hit_weighted_sum"] / bucket["_hit_weight"]), 6)
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
                **authority,
            }
        )
    rows.sort(
        key=lambda item: (
            item["classification"] == "potential_trigger_relaxation_candidate",
            int(item.get("signal_count") or 0),
            float(item.get("avg_return_after_cost") or -999.0),
        ),
        reverse=True,
    )
    classification_counts = dict(pd.Series([item["classification"] for item in rows]).value_counts().to_dict()) if rows else {}
    return {
        "status": "ok" if rows else "no_near_miss_rows",
        **authority,
        "min_signals": int(min_signals),
        "return_threshold": float(return_threshold),
        "min_hit_rate": float(min_hit_rate),
        "near_miss_rows": len(rows),
        "classification_counts": classification_counts,
        "top_relaxation_candidates": [item for item in rows if item["classification"] == "potential_trigger_relaxation_candidate"][:10],
        "top_do_not_relax": [item for item in rows if item["classification"] == "do_not_relax_negative_or_weak"][:10],
        "needs_more_label_near_misses": [item for item in rows if item["classification"] == "needs_more_matured_labels"][:10],
        "mixed_or_marginal_near_misses": [item for item in rows if item["classification"] == "mixed_or_marginal_requires_review"][:10],
    }


def check_signal_quality() -> dict[str, Any]:
    if not table_exists(SIGNAL_QUALITY_SUMMARY_TABLE):
        return _status(
            "warn",
            "Signal-quality evaluator has not been run yet.",
            usable=False,
            reasons=["missing_summary_table"],
            command="python -m advisory.signal_quality_evaluator --from-date YYYY-MM-DD --to-date YYYY-MM-DD --horizons 5 10 20",
        )
    try:
        latest_df = sql_to_df(
            f"SELECT MAX(evaluated_at) AS latest_evaluated_at FROM {SIGNAL_QUALITY_SUMMARY_TABLE}",
            retries=3,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        _record_health_local_fallback(
            source=SIGNAL_QUALITY_SUMMARY_TABLE,
            fallback_type="operator_health_signal_quality_summary_check_failed",
            reason="Operator Health could not inspect signal-quality summary freshness.",
            error=exc,
        )
        return _status("error", "Could not inspect signal-quality summary.", error=f"{type(exc).__name__}: {exc}", usable=False)
    latest = pd.to_datetime(latest_df.iloc[0].get("latest_evaluated_at"), utc=True, errors="coerce") if not latest_df.empty else pd.NaT
    if pd.isna(latest):
        return _status("warn", "Signal-quality summary table has no evaluated rows.", usable=False, reasons=["empty_summary_table"])

    try:
        summary = sql_to_df(
            f"""
            SELECT *
            FROM {SIGNAL_QUALITY_SUMMARY_TABLE}
            WHERE evaluated_at = %(latest)s
            ORDER BY horizon_days, variant
            """,
            params={"latest": latest},
            retries=3,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        _record_health_local_fallback(
            source=SIGNAL_QUALITY_SUMMARY_TABLE,
            fallback_type="operator_health_signal_quality_rows_load_failed",
            reason="Operator Health could not load latest signal-quality summary rows.",
            error=exc,
            metadata={"latest_evaluated_at": latest.isoformat()},
        )
        return _status("error", "Could not read latest signal-quality rows.", error=f"{type(exc).__name__}: {exc}", usable=False)

    coverage = pd.DataFrame()
    if table_exists(SIGNAL_QUALITY_EVALUATIONS_TABLE):
        try:
            coverage = sql_to_df(
                f"""
                SELECT
                    horizon_days,
                    COUNT(*) AS candidate_rows,
                    SUM(CASE WHEN event_action_type IS NOT NULL THEN 1 ELSE 0 END) AS event_policy_rows,
                    SUM(CASE WHEN bhavcopy_deal_pressure IS NOT NULL THEN 1 ELSE 0 END) AS bhavcopy_rows,
                    SUM(CASE WHEN company_memory_signal IS NOT NULL THEN 1 ELSE 0 END) AS company_memory_rows
                FROM {SIGNAL_QUALITY_EVALUATIONS_TABLE}
                WHERE evaluated_at = %(latest)s
                  AND variant = 'technical_only'
                GROUP BY horizon_days
                ORDER BY horizon_days
                """,
                params={"latest": latest},
                retries=3,
                statement_timeout_ms=10000,
            )
        except Exception as exc:
            _record_health_local_fallback(
                source=SIGNAL_QUALITY_EVALUATIONS_TABLE,
                fallback_type="operator_health_signal_quality_coverage_load_failed",
                reason="Operator Health could not load signal-quality overlay coverage; continuing with empty coverage.",
                error=exc,
                severity="warn",
                metadata={"latest_evaluated_at": latest.isoformat()},
            )
            coverage = pd.DataFrame()

    promotion_reviews = _load_signal_quality_promotion_review_health(latest)
    now = pd.Timestamp.utcnow()
    age_days = float((now - latest).total_seconds() / 86400.0)
    matured = pd.to_numeric(summary.get("matured_count", pd.Series(dtype=float)), errors="coerce")
    max_matured = int(matured.max()) if not matured.dropna().empty else 0
    empty_label_series = pd.Series([""] * len(summary), index=summary.index, dtype=object)
    recommendations = (
        summary.get("recommendation", empty_label_series)
        .fillna("")
        .astype(str)
        .str.strip()
    )
    classifications = (
        summary.get("classification", empty_label_series)
        .fillna("")
        .astype(str)
        .str.strip()
    )
    benchmark_beta_rows = int(
        (recommendations.eq("benchmark_beta_not_overlay_alpha") | classifications.eq("benchmark_beta_not_overlay_alpha")).sum()
    )
    needs_benchmark_rows = int(
        (recommendations.eq("needs_benchmark_attribution") | classifications.eq("needs_benchmark_attribution")).sum()
    )
    benchmark_or_attribution_blocked_rows = benchmark_beta_rows + needs_benchmark_rows
    overlay_rows = 0
    if not coverage.empty:
        for column in ["event_policy_rows", "bhavcopy_rows", "company_memory_rows"]:
            if column in coverage.columns:
                overlay_rows += int(pd.to_numeric(coverage[column], errors="coerce").fillna(0).max())
    family_report = build_signal_quality_family_report(summary)
    promotion_readiness = (
        family_report.get("promotion_readiness")
        if isinstance(family_report.get("promotion_readiness"), dict)
        else {}
    )
    promotion_readiness_status = str(promotion_readiness.get("status") or "").strip()
    candidate_helpful_families = promotion_readiness.get("candidate_helpful_families")
    blocked_families = promotion_readiness.get("benchmark_or_attribution_blocked_families")
    candidate_helpful_families = candidate_helpful_families if isinstance(candidate_helpful_families, list) else []
    blocked_families = blocked_families if isinstance(blocked_families, list) else []
    reasons: list[str] = []
    if age_days > SIGNAL_QUALITY_MAX_AGE_DAYS:
        reasons.append("stale_signal_quality_run")
    if max_matured < SIGNAL_QUALITY_MIN_MATURED_ROWS:
        reasons.append("insufficient_matured_rows")
    if overlay_rows < SIGNAL_QUALITY_MIN_OVERLAY_ROWS:
        reasons.append("insufficient_overlay_coverage")
    if benchmark_beta_rows > 0:
        reasons.append("benchmark_beta_not_overlay_alpha")
    if needs_benchmark_rows > 0:
        reasons.append("needs_benchmark_attribution")
    if promotion_readiness_status in {"blocked_by_benchmark_or_attribution", "blocked_by_horizon_instability"}:
        reasons.append(promotion_readiness_status)
    elif promotion_readiness_status in {"no_family_ready_for_review", "needs_more_matured_data", "no_family_evidence"}:
        reasons.append("no_context_family_ready_for_review")
    status = "ok" if not reasons else "warn"
    return _status(
        status,
        "Latest signal-quality run is usable for manual review." if status == "ok" else "Latest signal-quality run is not strong enough for promotion decisions.",
        usable=status == "ok",
        reasons=reasons,
        latest_evaluated_at=_json_ready(latest),
        age_days=round(age_days, 3),
        max_age_days=SIGNAL_QUALITY_MAX_AGE_DAYS,
        summary_rows=int(len(summary)),
        max_matured_rows=max_matured,
        min_matured_rows=SIGNAL_QUALITY_MIN_MATURED_ROWS,
        overlay_rows=overlay_rows,
        min_overlay_rows=SIGNAL_QUALITY_MIN_OVERLAY_ROWS,
        benchmark_beta_not_overlay_alpha_count=benchmark_beta_rows,
        needs_benchmark_attribution_count=needs_benchmark_rows,
        benchmark_or_attribution_blocked_count=benchmark_or_attribution_blocked_rows,
        promotion_readiness=promotion_readiness,
        promotion_readiness_status=promotion_readiness_status,
        candidate_helpful_family_count=int(len(candidate_helpful_families)),
        benchmark_or_attribution_blocked_family_count=int(len(blocked_families)),
        promotion_reviews=promotion_reviews,
        promotion_review_sector_block_count=promotion_reviews.get("sector_block_count"),
        promotion_review_runtime_block_count=promotion_reviews.get("runtime_block_count"),
        promotion_review_harmful_class_block_count=promotion_reviews.get("harmful_class_block_count"),
        coverage=_records(coverage),
        summary=_records(summary.head(20)),
        command="python -m advisory.signal_quality_evaluator --from-date YYYY-MM-DD --to-date YYYY-MM-DD --horizons 5 10 20",
        window_runner_command="python -m advisory.signal_quality_window_runner --horizons 5 10 20 --include-split-reports",
        authority="research_only",
        broker_execution_allowed=False,
        policy_auto_promotion_allowed=False,
    )


def _load_signal_quality_promotion_review_health(latest_evaluated_at: pd.Timestamp) -> dict[str, Any]:
    if not table_exists(SIGNAL_QUALITY_PROMOTION_REVIEWS_TABLE):
        return {
            "status": "missing_table",
            "review_count": 0,
            "sector_block_count": 0,
            "runtime_block_count": 0,
            "harmful_class_block_count": 0,
            "sample_blocked_reviews": [],
        }
    try:
        df = sql_to_df(
            f"""
            SELECT reviewed_at, evaluated_at, horizon_days, variant, recommendation, coverage_json, llm_review_json
            FROM {SIGNAL_QUALITY_PROMOTION_REVIEWS_TABLE}
            WHERE evaluated_at = %(latest)s
            ORDER BY reviewed_at DESC NULLS LAST
            LIMIT 50
            """,
            params={"latest": latest_evaluated_at},
            retries=2,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        _record_health_local_fallback(
            source=SIGNAL_QUALITY_PROMOTION_REVIEWS_TABLE,
            fallback_type="operator_health_signal_quality_promotion_reviews_load_failed",
            reason="Operator Health could not inspect persisted signal-quality promotion review blockers.",
            error=exc,
            metadata={"latest_evaluated_at": latest_evaluated_at.isoformat()},
        )
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "review_count": 0,
            "sector_block_count": 0,
            "runtime_block_count": 0,
            "harmful_class_block_count": 0,
            "sample_blocked_reviews": [],
        }
    rows = _records(df)
    sector_blocks: list[dict[str, Any]] = []
    runtime_blocks: list[dict[str, Any]] = []
    harmful_class_blocks: list[dict[str, Any]] = []
    for row in rows:
        coverage = _json_dict(
            row.get("coverage_json"),
            source=SIGNAL_QUALITY_PROMOTION_REVIEWS_TABLE,
            metadata={"variant": row.get("variant"), "horizon_days": row.get("horizon_days")},
        )
        llm_review = _json_dict(
            row.get("llm_review_json"),
            source=SIGNAL_QUALITY_PROMOTION_REVIEWS_TABLE,
            metadata={"variant": row.get("variant"), "horizon_days": row.get("horizon_days")},
        )
        record = {
            "reviewed_at": row.get("reviewed_at"),
            "evaluated_at": row.get("evaluated_at"),
            "horizon_days": row.get("horizon_days"),
            "variant": row.get("variant"),
            "recommendation": row.get("recommendation"),
            "reasons": llm_review.get("reasons") if isinstance(llm_review.get("reasons"), list) else [],
            "source_family": coverage.get("fast_reliability_source_family") or coverage.get("context_variant_source_family"),
            "authority": "research_only_manual_review",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
        }
        if int(coverage.get("fast_reliability_harmful_sector_count") or 0) > 0:
            sector_blocks.append(
                {
                    **record,
                    "harmful_sector_count": int(coverage.get("fast_reliability_harmful_sector_count") or 0),
                    "harmful_sectors": coverage.get("fast_reliability_harmful_sectors") or [],
                }
            )
        if coverage.get("fast_reliability_watch_priority_allowed") is False:
            runtime_blocks.append(
                {
                    **record,
                    "runtime_policy_contract": coverage.get("fast_reliability_runtime_policy_contract"),
                }
            )
        if int(coverage.get("fast_reliability_harmful_context_class_count") or 0) > 0:
            harmful_class_blocks.append(
                {
                    **record,
                    "harmful_context_class_count": int(coverage.get("fast_reliability_harmful_context_class_count") or 0),
                    "harmful_context_classes": coverage.get("fast_reliability_harmful_context_classes") or [],
                }
            )
    return {
        "status": "ok",
        "review_count": len(rows),
        "sector_block_count": len(sector_blocks),
        "runtime_block_count": len(runtime_blocks),
        "harmful_class_block_count": len(harmful_class_blocks),
        "sample_blocked_reviews": (sector_blocks + runtime_blocks + harmful_class_blocks)[:10],
        "authority": "research_only_manual_review",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
    }


def check_ts_forecast_paper_portfolio() -> dict[str, Any]:
    command = "python -m advisory.ts_forecast_paper_portfolio --log-research-ledger"
    if not table_exists(TS_FORECAST_PAPER_TABLE):
        return _status(
            "warn",
            "TS forecast paper-portfolio evidence table does not exist yet.",
            usable=False,
            reasons=["missing_ts_forecast_paper_table"],
            table=TS_FORECAST_PAPER_TABLE,
            command=command,
            promotion_check_endpoint="/api/research/ts-forecast-promotion-check",
            authority="research_only",
            broker_execution_allowed=False,
            policy_auto_promotion_allowed=False,
        )
    try:
        df = sql_to_df(
            f"""
            SELECT
                MAX(load_ts) AS latest_load_ts,
                MAX(asof_date) AS latest_asof_date,
                COUNT(*) AS row_count,
                SUM(CASE WHEN evaluation_status = 'matured' THEN 1 ELSE 0 END) AS matured_count,
                SUM(CASE WHEN paper_decision = 'PAPER_BUY' THEN 1 ELSE 0 END) AS paper_buy_count,
                COUNT(DISTINCT symbol) AS symbol_count,
                COUNT(DISTINCT asof_date) AS asof_date_count
            FROM {TS_FORECAST_PAPER_TABLE}
            """,
            retries=3,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        _record_health_local_fallback(
            source=TS_FORECAST_PAPER_TABLE,
            fallback_type="operator_health_ts_forecast_paper_check_failed",
            reason="Operator Health could not inspect TS forecast paper-portfolio evidence.",
            error=exc,
        )
        return _status(
            "error",
            "Could not inspect TS forecast paper-portfolio evidence.",
            error=f"{type(exc).__name__}: {exc}",
            usable=False,
            table=TS_FORECAST_PAPER_TABLE,
            command=command,
        )

    row = df.iloc[0] if not df.empty else {}
    latest_load_ts = pd.to_datetime(row.get("latest_load_ts"), utc=True, errors="coerce") if len(row) else pd.NaT
    latest_asof_date = pd.to_datetime(row.get("latest_asof_date"), utc=True, errors="coerce") if len(row) else pd.NaT
    row_count = int(row.get("row_count") or 0) if len(row) else 0
    matured_count = int(row.get("matured_count") or 0) if len(row) else 0
    paper_buy_count = int(row.get("paper_buy_count") or 0) if len(row) else 0
    symbol_count = int(row.get("symbol_count") or 0) if len(row) else 0
    asof_date_count = int(row.get("asof_date_count") or 0) if len(row) else 0
    reasons: list[str] = []
    if row_count <= 0:
        reasons.append("empty_ts_forecast_paper_table")
    if matured_count <= 0:
        reasons.append("no_matured_paper_outcomes")
    if pd.isna(latest_load_ts):
        reasons.append("missing_latest_load_ts")
    else:
        age_days = max((pd.Timestamp.utcnow() - latest_load_ts).total_seconds() / 86400.0, 0.0)
        if age_days > 7:
            reasons.append("stale_ts_forecast_paper_evidence")
    status = "ok" if not reasons else "warn"
    return _status(
        status,
        "TS forecast paper-portfolio evidence is available."
        if status == "ok"
        else "TS forecast paper-portfolio evidence is missing, stale, or not matured enough for promotion review.",
        usable=status == "ok",
        reasons=reasons,
        table=TS_FORECAST_PAPER_TABLE,
        latest_load_ts=_json_ready(latest_load_ts),
        latest_asof_date=_json_ready(latest_asof_date),
        age_days=None if pd.isna(latest_load_ts) else round(max((pd.Timestamp.utcnow() - latest_load_ts).total_seconds() / 86400.0, 0.0), 3),
        row_count=row_count,
        matured_count=matured_count,
        paper_buy_count=paper_buy_count,
        symbol_count=symbol_count,
        asof_date_count=asof_date_count,
        command=command,
        promotion_check_command="python -m advisory.ts_forecast_promotion_check --format json",
        promotion_check_endpoint="/api/research/ts-forecast-promotion-check",
        authority="research_only",
        broker_execution_allowed=False,
        policy_auto_promotion_allowed=False,
    )


def check_llm_provenance_audit() -> dict[str, Any]:
    command = "python -m advisory.llm_provenance_audit --lookback-days 30 --limit-per-table 100 --format text"
    try:
        payload = build_llm_provenance_audit(lookback_days=30, limit_per_table=100)
    except Exception as exc:
        _record_health_local_fallback(
            source="advisory.llm_provenance_audit",
            fallback_type="operator_health_llm_provenance_audit_failed",
            reason="Operator Health could not run the LLM provenance audit.",
            error=exc,
        )
        return _status(
            "error",
            "Could not run LLM provenance audit.",
            error=f"{type(exc).__name__}: {exc}",
            usable=False,
            command=command,
            authority="audit_only",
            broker_execution_allowed=False,
            policy_auto_promotion_allowed=False,
            repairs_metadata=False,
        )

    issue_count = int(payload.get("issue_table_count") or 0)
    table_count = int(payload.get("table_count") or 0)
    issue_rows = [
        row
        for row in (payload.get("tables") if isinstance(payload.get("tables"), list) else [])
        if isinstance(row, dict) and str(row.get("status") or "ok") not in {"ok", "no_recent_rows"}
    ]
    status = "warn" if issue_count else "ok"
    return _status(
        status,
        "LLM/Codex provenance audit is clean."
        if status == "ok"
        else "LLM/Codex provenance audit found missing prompt/schema/evidence/authority metadata.",
        usable=status == "ok",
        reasons=["llm_provenance_issues_found"] if issue_count else [],
        issue_table_count=issue_count,
        table_count=table_count,
        lookback_days=int(payload.get("lookback_days") or 30),
        limit_per_table=int(payload.get("limit_per_table") or 100),
        issue_rows=issue_rows[:10],
        command=command,
        authority="audit_only",
        broker_execution_allowed=False,
        policy_auto_promotion_allowed=False,
        repairs_metadata=False,
    )


def check_provenance_graph_dry_runs() -> dict[str, Any]:
    commands = {
        "action_evidence": "python -m advisory.action_evidence_provenance --dry-run --limit 250 --format text",
        "causal_event": "python -m advisory.causal_event_provenance --dry-run --limit 250 --format text",
    }
    rows: list[dict[str, Any]] = []
    reasons: list[str] = []
    builders = [
        ("action_evidence", build_action_evidence_provenance, "provenance_rows", {"limit": 250}),
        ("causal_event", build_causal_event_provenance, "provenance_rows", {"limit": 250}),
    ]
    for key, builder, row_count_key, kwargs in builders:
        try:
            _frame, meta = builder(**kwargs)
        except Exception as exc:
            _record_health_local_fallback(
                source=f"advisory.{key}_provenance",
                fallback_type="operator_health_provenance_graph_dry_run_failed",
                reason="Operator Health could not run a provenance graph dry-run builder.",
                error=exc,
                metadata={"graph": key, "command": commands[key]},
            )
            rows.append(
                {
                    "graph": key,
                    "status": "error",
                    "message": "Provenance dry run failed.",
                    "error": f"{type(exc).__name__}: {exc}",
                    "command": commands[key],
                    "broker_execution_allowed": False,
                    "policy_auto_promotion_allowed": False,
                    "persisted": False,
                }
            )
            reasons.append(f"{key}_dry_run_failed")
            continue
        provenance_rows = int(meta.get(row_count_key) or 0)
        graph_status = "ok" if provenance_rows > 0 else "warn"
        if graph_status != "ok":
            reasons.append(f"{key}_no_provenance_rows")
        rows.append(
            {
                "graph": key,
                "status": graph_status,
                "message": "Provenance dry run produced audit rows." if graph_status == "ok" else "Provenance dry run produced no audit rows.",
                "command": commands[key],
                "from_date": meta.get("from_date"),
                "to_date": meta.get("to_date"),
                "source_row_count": int(meta.get("action_rows") or meta.get("memory_rows") or 0),
                "evaluation_rows": int(meta.get("evaluation_rows") or 0),
                "provenance_rows": provenance_rows,
                "table": meta.get("table"),
                "authority_scope": meta.get("authority_scope") or "research_only",
                "broker_execution_allowed": bool(meta.get("broker_execution_allowed")),
                "policy_auto_promotion_allowed": bool(meta.get("policy_auto_promotion_allowed")),
                "persisted": False,
            }
        )
    error_count = sum(1 for row in rows if row.get("status") == "error")
    warn_count = sum(1 for row in rows if row.get("status") == "warn")
    status = "error" if error_count else "warn" if warn_count else "ok"
    return _status(
        status,
        "Provenance graph dry runs are producing audit lineage."
        if status == "ok"
        else "One or more provenance graph dry runs are missing lineage rows or failed.",
        usable=status == "ok",
        reasons=reasons,
        rows=rows,
        row_count=len(rows),
        error_count=error_count,
        warn_count=warn_count,
        commands=list(commands.values()),
        authority="audit_only",
        broker_execution_allowed=False,
        policy_auto_promotion_allowed=False,
        persists_rows=False,
    )


def check_causal_event_memory_evidence() -> dict[str, Any]:
    command = "python -m advisory.causal_event_memory_evaluator --horizons 5 10 20 --format json"
    if not table_exists(CAUSAL_MEMORY_EVALUATIONS_TABLE) or not table_exists(CAUSAL_MEMORY_SUMMARY_TABLE):
        missing = [
            table
            for table in [CAUSAL_MEMORY_EVALUATIONS_TABLE, CAUSAL_MEMORY_SUMMARY_TABLE]
            if not table_exists(table)
        ]
        return _status(
            "warn",
            "Causal event-memory evaluation tables do not exist yet.",
            usable=False,
            reasons=["missing_causal_event_memory_evidence_tables"],
            missing_tables=missing,
            command=command,
            authority="research_only",
            broker_execution_allowed=False,
            policy_auto_promotion_allowed=False,
        )
    try:
        eval_df = sql_to_df(
            f"""
            SELECT
                MAX(evaluated_at) AS latest_evaluated_at,
                MAX(load_ts) AS latest_load_ts,
                COUNT(*) AS evaluation_rows,
                SUM(CASE WHEN matured THEN 1 ELSE 0 END) AS matured_rows,
                COUNT(DISTINCT symbol) AS symbol_count,
                COUNT(DISTINCT horizon_days) AS horizon_count
            FROM {CAUSAL_MEMORY_EVALUATIONS_TABLE}
            """,
            retries=3,
            statement_timeout_ms=10000,
        )
        summary_df = sql_to_df(
            f"""
            SELECT
                MAX(evaluated_at) AS latest_evaluated_at,
                COUNT(*) AS summary_rows,
                SUM(CASE WHEN classification = 'candidate_helpful' THEN 1 ELSE 0 END) AS candidate_group_count,
                SUM(CASE WHEN classification = 'hurts_or_no_lift' THEN 1 ELSE 0 END) AS harmful_group_count,
                SUM(CASE WHEN matured_count >= %s THEN 1 ELSE 0 END) AS sufficiently_matured_group_count
            FROM {CAUSAL_MEMORY_SUMMARY_TABLE}
            """,
            params=(int(CAUSAL_MEMORY_MIN_MATURED_ROWS),),
            retries=3,
            statement_timeout_ms=10000,
        )
        groups = sql_to_df(
            f"""
            SELECT
                evaluated_at,
                horizon_days,
                context_source,
                context_class,
                event_type,
                direction,
                event_state,
                sample_count,
                matured_count,
                symbol_count,
                avg_forward_return_after_cost,
                avg_excess_return_after_cost,
                direction_hit_rate_after_cost,
                excess_direction_hit_rate_after_cost,
                classification,
                recommendation,
                broker_execution_allowed,
                policy_auto_promotion_allowed
            FROM {CAUSAL_MEMORY_SUMMARY_TABLE}
            WHERE classification IN ('candidate_helpful', 'hurts_or_no_lift')
            ORDER BY evaluated_at DESC, matured_count DESC, horizon_days, context_source, context_class
            LIMIT 10
            """,
            retries=3,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        _record_health_local_fallback(
            source=CAUSAL_MEMORY_SUMMARY_TABLE,
            fallback_type="operator_health_causal_event_memory_evidence_failed",
            reason="Operator Health could not inspect causal event-memory evaluation evidence.",
            error=exc,
        )
        return _status(
            "error",
            "Could not inspect causal event-memory evaluation evidence.",
            error=f"{type(exc).__name__}: {exc}",
            usable=False,
            command=command,
            authority="research_only",
            broker_execution_allowed=False,
            policy_auto_promotion_allowed=False,
        )

    eval_row = eval_df.iloc[0] if not eval_df.empty else {}
    summary_row = summary_df.iloc[0] if not summary_df.empty else {}
    latest_eval = pd.to_datetime(eval_row.get("latest_evaluated_at"), utc=True, errors="coerce") if len(eval_row) else pd.NaT
    latest_load = pd.to_datetime(eval_row.get("latest_load_ts"), utc=True, errors="coerce") if len(eval_row) else pd.NaT
    evaluation_rows = int(eval_row.get("evaluation_rows") or 0) if len(eval_row) else 0
    matured_rows = int(eval_row.get("matured_rows") or 0) if len(eval_row) else 0
    summary_rows = int(summary_row.get("summary_rows") or 0) if len(summary_row) else 0
    candidate_group_count = int(summary_row.get("candidate_group_count") or 0) if len(summary_row) else 0
    harmful_group_count = int(summary_row.get("harmful_group_count") or 0) if len(summary_row) else 0
    sufficiently_matured_group_count = int(summary_row.get("sufficiently_matured_group_count") or 0) if len(summary_row) else 0
    reasons: list[str] = []
    if evaluation_rows <= 0 or summary_rows <= 0:
        reasons.append("empty_causal_event_memory_evidence")
    if matured_rows <= 0:
        reasons.append("no_matured_causal_memory_labels")
    if pd.isna(latest_eval):
        reasons.append("missing_latest_causal_memory_evaluated_at")
        age_days = None
    else:
        age_days = max((pd.Timestamp.utcnow() - latest_eval).total_seconds() / 86400.0, 0.0)
        if age_days > 7:
            reasons.append("stale_causal_event_memory_evidence")
    status = "ok" if not reasons else "warn"
    ready_for_policy_review = bool(candidate_group_count > 0 and matured_rows >= int(CAUSAL_MEMORY_MIN_MATURED_ROWS))
    if status == "ok" and ready_for_policy_review:
        message = "Causal event-memory evaluation evidence has matured candidate groups for offline review."
    elif status == "ok":
        message = "Causal event-memory evaluation evidence is available, but no helpful candidate group is ready."
    else:
        message = "Causal event-memory evaluation evidence is missing, stale, or has no matured labels."
    return _status(
        status,
        message,
        usable=status == "ok",
        ready_for_policy_review=ready_for_policy_review,
        reasons=reasons,
        evaluations_table=CAUSAL_MEMORY_EVALUATIONS_TABLE,
        summary_table=CAUSAL_MEMORY_SUMMARY_TABLE,
        latest_evaluated_at=_json_ready(latest_eval),
        latest_load_ts=_json_ready(latest_load),
        age_days=None if age_days is None else round(age_days, 3),
        evaluation_rows=evaluation_rows,
        matured_rows=matured_rows,
        summary_rows=summary_rows,
        candidate_group_count=candidate_group_count,
        harmful_group_count=harmful_group_count,
        sufficiently_matured_group_count=sufficiently_matured_group_count,
        min_matured_rows=int(CAUSAL_MEMORY_MIN_MATURED_ROWS),
        groups=_records(groups),
        command=command,
        config_preview_command=(
            "python -m advisory.causal_event_memory_evaluator --horizons 5 10 20 "
            "--generate-config-previews --include-suppression-config-previews --dry-run --format text"
        ),
        authority="research_only",
        broker_execution_allowed=False,
        policy_auto_promotion_allowed=False,
    )


def check_action_transition_evidence() -> dict[str, Any]:
    command = "python -m advisory.action_transition_evaluator --horizons 5 10 20 --format json"
    schema_command = "python -m advisory.action_transition_evaluator --ensure-schema-only"
    if not table_exists(ACTION_TRANSITION_EVALUATIONS_TABLE) or not table_exists(ACTION_TRANSITION_SUMMARY_TABLE):
        missing = [
            table
            for table in [ACTION_TRANSITION_EVALUATIONS_TABLE, ACTION_TRANSITION_SUMMARY_TABLE]
            if not table_exists(table)
        ]
        return _status(
            "warn",
            "Action-transition evaluation tables do not exist yet.",
            usable=False,
            ready_for_policy_review=False,
            reasons=["missing_action_transition_evidence_tables"],
            missing_tables=missing,
            command=command,
            schema_command=schema_command,
            authority="research_only",
            broker_execution_allowed=False,
            policy_auto_promotion_allowed=False,
        )
    try:
        eval_df = sql_to_df(
            f"""
            SELECT
                MAX(evaluated_at) AS latest_evaluated_at,
                MAX(load_ts) AS latest_load_ts,
                COUNT(*) AS evaluation_rows,
                SUM(CASE WHEN matured THEN 1 ELSE 0 END) AS matured_rows,
                COUNT(DISTINCT symbol) AS symbol_count,
                COUNT(DISTINCT horizon_days) AS horizon_count
            FROM {ACTION_TRANSITION_EVALUATIONS_TABLE}
            """,
            retries=3,
            statement_timeout_ms=10000,
        )
        summary_df = sql_to_df(
            f"""
            SELECT
                MAX(evaluated_at) AS latest_evaluated_at,
                COUNT(*) AS summary_rows,
                SUM(CASE WHEN classification = 'candidate_stable_policy_signal' THEN 1 ELSE 0 END) AS candidate_group_count,
                SUM(CASE WHEN classification = 'benchmark_beta_not_transition_alpha' THEN 1 ELSE 0 END) AS benchmark_beta_group_count,
                SUM(CASE WHEN classification = 'needs_benchmark_attribution' THEN 1 ELSE 0 END) AS needs_benchmark_attribution_count,
                SUM(CASE WHEN matured_count >= %s THEN 1 ELSE 0 END) AS sufficiently_matured_group_count
            FROM {ACTION_TRANSITION_SUMMARY_TABLE}
            """,
            params=(int(ACTION_TRANSITION_MIN_MATURED_ROWS),),
            retries=3,
            statement_timeout_ms=10000,
        )
        groups = sql_to_df(
            f"""
            SELECT
                evaluated_at,
                horizon_days,
                group_type,
                group_value,
                sample_count,
                matured_count,
                direction_hit_rate_after_cost,
                excess_direction_hit_rate_after_cost,
                avg_directional_helpfulness_score,
                avg_excess_directional_helpfulness_score,
                avg_forward_return_after_cost,
                avg_benchmark_forward_return,
                avg_excess_forward_return_after_cost,
                classification,
                recommendation,
                broker_execution_allowed,
                policy_auto_promotion_allowed
            FROM {ACTION_TRANSITION_SUMMARY_TABLE}
            WHERE classification IN (
                'candidate_stable_policy_signal',
                'benchmark_beta_not_transition_alpha',
                'needs_benchmark_attribution'
            )
            ORDER BY evaluated_at DESC, matured_count DESC, horizon_days, group_type, group_value
            LIMIT 10
            """,
            retries=3,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        _record_health_local_fallback(
            source=ACTION_TRANSITION_SUMMARY_TABLE,
            fallback_type="operator_health_action_transition_evidence_failed",
            reason="Operator Health could not inspect action-transition evaluation evidence.",
            error=exc,
        )
        return _status(
            "error",
            "Could not inspect action-transition evaluation evidence.",
            error=f"{type(exc).__name__}: {exc}",
            usable=False,
            ready_for_policy_review=False,
            command=command,
            schema_command=schema_command,
            authority="research_only",
            broker_execution_allowed=False,
            policy_auto_promotion_allowed=False,
        )

    eval_row = eval_df.iloc[0] if not eval_df.empty else {}
    summary_row = summary_df.iloc[0] if not summary_df.empty else {}
    latest_eval = pd.to_datetime(eval_row.get("latest_evaluated_at"), utc=True, errors="coerce") if len(eval_row) else pd.NaT
    latest_load = pd.to_datetime(eval_row.get("latest_load_ts"), utc=True, errors="coerce") if len(eval_row) else pd.NaT
    evaluation_rows = int(eval_row.get("evaluation_rows") or 0) if len(eval_row) else 0
    matured_rows = int(eval_row.get("matured_rows") or 0) if len(eval_row) else 0
    summary_rows = int(summary_row.get("summary_rows") or 0) if len(summary_row) else 0
    candidate_group_count = int(summary_row.get("candidate_group_count") or 0) if len(summary_row) else 0
    benchmark_beta_group_count = int(summary_row.get("benchmark_beta_group_count") or 0) if len(summary_row) else 0
    needs_benchmark_attribution_count = int(summary_row.get("needs_benchmark_attribution_count") or 0) if len(summary_row) else 0
    sufficiently_matured_group_count = int(summary_row.get("sufficiently_matured_group_count") or 0) if len(summary_row) else 0
    reasons: list[str] = []
    if evaluation_rows <= 0 or summary_rows <= 0:
        reasons.append("empty_action_transition_evidence")
    if matured_rows <= 0:
        reasons.append("no_matured_action_transition_labels")
    if benchmark_beta_group_count > 0:
        reasons.append("transition_evidence_benchmark_beta_only")
    if needs_benchmark_attribution_count > 0:
        reasons.append("transition_evidence_needs_benchmark_attribution")
    if pd.isna(latest_eval):
        reasons.append("missing_latest_action_transition_evaluated_at")
        age_days = None
    else:
        age_days = max((pd.Timestamp.utcnow() - latest_eval).total_seconds() / 86400.0, 0.0)
        if age_days > 14:
            reasons.append("stale_action_transition_evidence")
    status = "ok" if not reasons else "warn"
    ready_for_policy_review = bool(candidate_group_count > 0 and matured_rows >= int(ACTION_TRANSITION_MIN_MATURED_ROWS))
    if status == "ok" and ready_for_policy_review:
        message = "Action-transition evidence has candidate stable-policy groups for offline review."
    elif benchmark_beta_group_count > 0:
        message = "Action-transition evidence is raw-helpful but benchmark-beta-only; keep it research-only."
    elif needs_benchmark_attribution_count > 0:
        message = "Action-transition evidence needs benchmark attribution before policy review."
    elif status == "ok":
        message = "Action-transition evidence is available, but no stable helpful group is ready."
    else:
        message = "Action-transition evidence is missing, stale, or has no matured labels."
    return _status(
        status,
        message,
        usable=status == "ok",
        ready_for_policy_review=ready_for_policy_review,
        reasons=reasons,
        evaluations_table=ACTION_TRANSITION_EVALUATIONS_TABLE,
        summary_table=ACTION_TRANSITION_SUMMARY_TABLE,
        latest_evaluated_at=_json_ready(latest_eval),
        latest_load_ts=_json_ready(latest_load),
        age_days=None if age_days is None else round(age_days, 3),
        evaluation_rows=evaluation_rows,
        matured_rows=matured_rows,
        summary_rows=summary_rows,
        candidate_group_count=candidate_group_count,
        benchmark_beta_not_transition_alpha_count=benchmark_beta_group_count,
        needs_benchmark_attribution_count=needs_benchmark_attribution_count,
        sufficiently_matured_group_count=sufficiently_matured_group_count,
        min_matured_rows=int(ACTION_TRANSITION_MIN_MATURED_ROWS),
        groups=_records(groups),
        command=command,
        schema_command=schema_command,
        authority="research_only",
        broker_execution_allowed=False,
        policy_auto_promotion_allowed=False,
    )


def check_event_policy_evidence() -> dict[str, Any]:
    command = "python -m advisory.event_policy_evaluator --horizons 5 10 20"
    schema_command = "python -m advisory.event_policy_evaluator --ensure-schema-only"
    if not table_exists(EVENT_POLICY_EVALUATIONS_TABLE) or not table_exists(EVENT_POLICY_SUMMARY_TABLE):
        missing = [
            table
            for table in [EVENT_POLICY_EVALUATIONS_TABLE, EVENT_POLICY_SUMMARY_TABLE]
            if not table_exists(table)
        ]
        return _status(
            "warn",
            "Event-policy evaluation tables do not exist yet.",
            usable=False,
            ready_for_policy_review=False,
            reasons=["missing_event_policy_evidence_tables"],
            missing_tables=missing,
            command=command,
            schema_command=schema_command,
            authority="research_only",
            broker_execution_allowed=False,
            policy_auto_promotion_allowed=False,
        )
    summary_columns = table_columns(EVENT_POLICY_SUMMARY_TABLE)
    required_benchmark_columns = {
        "avg_benchmark_forward_return",
        "avg_excess_forward_return_after_cost",
        "excess_hit_rate_after_cost",
    }
    missing_benchmark_columns = sorted(required_benchmark_columns - summary_columns)
    avg_benchmark_expr = (
        "avg_benchmark_forward_return"
        if "avg_benchmark_forward_return" in summary_columns
        else "NULL::DOUBLE PRECISION AS avg_benchmark_forward_return"
    )
    avg_excess_expr = (
        "avg_excess_forward_return_after_cost"
        if "avg_excess_forward_return_after_cost" in summary_columns
        else "NULL::DOUBLE PRECISION AS avg_excess_forward_return_after_cost"
    )
    excess_hit_expr = (
        "excess_hit_rate_after_cost"
        if "excess_hit_rate_after_cost" in summary_columns
        else "NULL::DOUBLE PRECISION AS excess_hit_rate_after_cost"
    )
    try:
        eval_df = sql_to_df(
            f"""
            SELECT
                MAX(evaluated_at) AS latest_evaluated_at,
                MAX(load_ts) AS latest_load_ts,
                COUNT(*) AS evaluation_rows,
                SUM(CASE WHEN matured THEN 1 ELSE 0 END) AS matured_rows,
                COUNT(DISTINCT symbol) AS symbol_count,
                COUNT(DISTINCT horizon_days) AS horizon_count
            FROM {EVENT_POLICY_EVALUATIONS_TABLE}
            """,
            retries=3,
            statement_timeout_ms=10000,
        )
        summary_df = sql_to_df(
            f"""
            SELECT
                MAX(evaluated_at) AS latest_evaluated_at,
                COUNT(*) AS summary_rows,
                SUM(CASE WHEN recommendation = 'candidate_policy_strengthen' THEN 1 ELSE 0 END) AS candidate_strengthen_count,
                SUM(CASE WHEN recommendation = 'candidate_policy_tighten_or_downgrade' THEN 1 ELSE 0 END) AS candidate_tighten_or_downgrade_count,
                SUM(CASE WHEN recommendation = 'benchmark_beta_not_policy_alpha' THEN 1 ELSE 0 END) AS benchmark_beta_group_count,
                SUM(CASE WHEN recommendation = 'needs_benchmark_attribution' THEN 1 ELSE 0 END) AS needs_benchmark_attribution_count,
                SUM(CASE WHEN matured_count >= %s THEN 1 ELSE 0 END) AS sufficiently_matured_group_count
            FROM {EVENT_POLICY_SUMMARY_TABLE}
            """,
            params=(int(EVENT_POLICY_MIN_MATURED_ROWS),),
            retries=3,
            statement_timeout_ms=10000,
        )
        groups = sql_to_df(
            f"""
            SELECT
                evaluated_at,
                horizon_days,
                group_type,
                group_value,
                sample_count,
                matured_count,
                avg_forward_return_after_cost,
                {avg_benchmark_expr},
                {avg_excess_expr},
                hit_rate_after_cost,
                {excess_hit_expr},
                recommendation
            FROM {EVENT_POLICY_SUMMARY_TABLE}
            WHERE recommendation IN (
                'candidate_policy_strengthen',
                'candidate_policy_tighten_or_downgrade',
                'benchmark_beta_not_policy_alpha',
                'needs_benchmark_attribution'
            )
            ORDER BY evaluated_at DESC, matured_count DESC, horizon_days, group_type, group_value
            LIMIT 10
            """,
            retries=3,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        _record_health_local_fallback(
            source=EVENT_POLICY_SUMMARY_TABLE,
            fallback_type="operator_health_event_policy_evidence_failed",
            reason="Operator Health could not inspect event-policy evaluation evidence.",
            error=exc,
        )
        return _status(
            "error",
            "Could not inspect event-policy evaluation evidence.",
            error=f"{type(exc).__name__}: {exc}",
            usable=False,
            ready_for_policy_review=False,
            command=command,
            authority="research_only",
            broker_execution_allowed=False,
            policy_auto_promotion_allowed=False,
        )

    eval_row = eval_df.iloc[0] if not eval_df.empty else {}
    summary_row = summary_df.iloc[0] if not summary_df.empty else {}
    latest_eval = pd.to_datetime(eval_row.get("latest_evaluated_at"), utc=True, errors="coerce") if len(eval_row) else pd.NaT
    latest_load = pd.to_datetime(eval_row.get("latest_load_ts"), utc=True, errors="coerce") if len(eval_row) else pd.NaT
    evaluation_rows = int(eval_row.get("evaluation_rows") or 0) if len(eval_row) else 0
    matured_rows = int(eval_row.get("matured_rows") or 0) if len(eval_row) else 0
    summary_rows = int(summary_row.get("summary_rows") or 0) if len(summary_row) else 0
    candidate_strengthen_count = int(summary_row.get("candidate_strengthen_count") or 0) if len(summary_row) else 0
    candidate_tighten_or_downgrade_count = int(summary_row.get("candidate_tighten_or_downgrade_count") or 0) if len(summary_row) else 0
    benchmark_beta_group_count = int(summary_row.get("benchmark_beta_group_count") or 0) if len(summary_row) else 0
    needs_benchmark_attribution_count = int(summary_row.get("needs_benchmark_attribution_count") or 0) if len(summary_row) else 0
    sufficiently_matured_group_count = int(summary_row.get("sufficiently_matured_group_count") or 0) if len(summary_row) else 0
    reasons: list[str] = []
    if evaluation_rows <= 0 or summary_rows <= 0:
        reasons.append("empty_event_policy_evidence")
    if matured_rows <= 0:
        reasons.append("no_matured_event_policy_labels")
    if missing_benchmark_columns:
        reasons.append("event_policy_evidence_schema_missing_benchmark_columns")
    if benchmark_beta_group_count > 0:
        reasons.append("event_policy_evidence_benchmark_beta_only")
    if needs_benchmark_attribution_count > 0:
        reasons.append("event_policy_evidence_needs_benchmark_attribution")
    if pd.isna(latest_eval):
        reasons.append("missing_latest_event_policy_evaluated_at")
        age_days = None
    else:
        age_days = max((pd.Timestamp.utcnow() - latest_eval).total_seconds() / 86400.0, 0.0)
        if age_days > 14:
            reasons.append("stale_event_policy_evidence")
    status = "ok" if not reasons else "warn"
    ready_for_policy_review = bool(
        status == "ok"
        and (candidate_strengthen_count + candidate_tighten_or_downgrade_count) > 0
        and matured_rows >= int(EVENT_POLICY_MIN_MATURED_ROWS)
    )
    if status == "ok" and ready_for_policy_review:
        message = "Event-policy evidence has benchmark-attributed candidate groups for offline review."
    elif missing_benchmark_columns:
        message = "Event-policy evidence summary is missing benchmark-attribution columns; rerun the evaluator migration/output."
    elif benchmark_beta_group_count > 0:
        message = "Event-policy evidence is raw-positive but benchmark-beta-only; keep policy influence research-only."
    elif needs_benchmark_attribution_count > 0:
        message = "Event-policy evidence needs benchmark attribution before policy review."
    elif status == "ok":
        message = "Event-policy evidence is available, but no benchmark-attributed candidate group is ready."
    else:
        message = "Event-policy evidence is missing, stale, or has no matured labels."
    return _status(
        status,
        message,
        usable=status == "ok",
        ready_for_policy_review=ready_for_policy_review,
        reasons=reasons,
        evaluations_table=EVENT_POLICY_EVALUATIONS_TABLE,
        summary_table=EVENT_POLICY_SUMMARY_TABLE,
        latest_evaluated_at=_json_ready(latest_eval),
        latest_load_ts=_json_ready(latest_load),
        age_days=None if age_days is None else round(age_days, 3),
        evaluation_rows=evaluation_rows,
        matured_rows=matured_rows,
        summary_rows=summary_rows,
        candidate_strengthen_count=candidate_strengthen_count,
        candidate_tighten_or_downgrade_count=candidate_tighten_or_downgrade_count,
        benchmark_beta_not_policy_alpha_count=benchmark_beta_group_count,
        needs_benchmark_attribution_count=needs_benchmark_attribution_count,
        missing_benchmark_columns=missing_benchmark_columns,
        sufficiently_matured_group_count=sufficiently_matured_group_count,
        min_matured_rows=int(EVENT_POLICY_MIN_MATURED_ROWS),
        groups=_records(groups),
        command=command,
        schema_command=schema_command,
        promotion_review_command=(
            "python -m advisory.event_policy_promotion --evaluated-at <timestamp> "
            "--horizon-days <days> --group-type <group_type> --group-value <group_value> --dry-run"
        ),
        authority="research_only",
        broker_execution_allowed=False,
        policy_auto_promotion_allowed=False,
    )


def check_context_watch_evidence() -> dict[str, Any]:
    command = "python -m advisory.context_watch_evaluator --horizons 5 10 20"
    if not table_exists(CONTEXT_WATCH_EVALUATIONS_TABLE) or not table_exists(CONTEXT_WATCH_SUMMARY_TABLE):
        missing = [
            table
            for table in [CONTEXT_WATCH_EVALUATIONS_TABLE, CONTEXT_WATCH_SUMMARY_TABLE]
            if not table_exists(table)
        ]
        return _status(
            "warn",
            "Context-watch evaluation tables do not exist yet.",
            usable=False,
            ready_for_policy_review=False,
            reasons=["missing_context_watch_evidence_tables"],
            missing_tables=missing,
            command=command,
            authority="research_only",
            broker_execution_allowed=False,
            policy_auto_promotion_allowed=False,
        )
    summary_columns = table_columns(CONTEXT_WATCH_SUMMARY_TABLE)
    required_benchmark_columns = {
        "avg_benchmark_forward_return",
        "avg_excess_watch_return_after_cost",
        "excess_opportunity_hit_rate_after_cost",
        "negative_excess_after_cost_rate",
    }
    missing_benchmark_columns = sorted(required_benchmark_columns - summary_columns)
    avg_benchmark_expr = (
        "avg_benchmark_forward_return"
        if "avg_benchmark_forward_return" in summary_columns
        else "NULL::DOUBLE PRECISION AS avg_benchmark_forward_return"
    )
    avg_excess_expr = (
        "avg_excess_watch_return_after_cost"
        if "avg_excess_watch_return_after_cost" in summary_columns
        else "NULL::DOUBLE PRECISION AS avg_excess_watch_return_after_cost"
    )
    excess_hit_expr = (
        "excess_opportunity_hit_rate_after_cost"
        if "excess_opportunity_hit_rate_after_cost" in summary_columns
        else "NULL::DOUBLE PRECISION AS excess_opportunity_hit_rate_after_cost"
    )
    negative_excess_expr = (
        "negative_excess_after_cost_rate"
        if "negative_excess_after_cost_rate" in summary_columns
        else "NULL::DOUBLE PRECISION AS negative_excess_after_cost_rate"
    )
    beta_only_expr = (
        "SUM(CASE WHEN avg_watch_return_after_cost > 0 AND avg_excess_watch_return_after_cost <= 0 THEN 1 ELSE 0 END)"
        if "avg_excess_watch_return_after_cost" in summary_columns
        else "0"
    )
    try:
        eval_df = sql_to_df(
            f"""
            SELECT
                MAX(evaluated_at) AS latest_evaluated_at,
                MAX(load_ts) AS latest_load_ts,
                COUNT(*) AS evaluation_rows,
                SUM(CASE WHEN matured THEN 1 ELSE 0 END) AS matured_rows,
                COUNT(DISTINCT symbol) AS symbol_count,
                COUNT(DISTINCT horizon_days) AS horizon_count
            FROM {CONTEXT_WATCH_EVALUATIONS_TABLE}
            """,
            retries=3,
            statement_timeout_ms=10000,
        )
        summary_df = sql_to_df(
            f"""
            SELECT
                MAX(evaluated_at) AS latest_evaluated_at,
                COUNT(*) AS summary_rows,
                SUM(CASE WHEN classification = 'opportunity_candidate' THEN 1 ELSE 0 END) AS opportunity_candidate_count,
                SUM(CASE WHEN classification = 'harmful_watch_noise' THEN 1 ELSE 0 END) AS harmful_watch_noise_count,
                SUM(CASE WHEN classification = 'mixed_or_weak' THEN 1 ELSE 0 END) AS mixed_or_weak_count,
                SUM(CASE WHEN classification = 'needs_more_data' THEN 1 ELSE 0 END) AS needs_more_data_count,
                {beta_only_expr} AS benchmark_beta_group_count,
                SUM(CASE WHEN matured_count >= %s THEN 1 ELSE 0 END) AS sufficiently_matured_group_count
            FROM {CONTEXT_WATCH_SUMMARY_TABLE}
            """,
            params=(int(CONTEXT_WATCH_MIN_MATURED_ROWS),),
            retries=3,
            statement_timeout_ms=10000,
        )
        groups = sql_to_df(
            f"""
            SELECT
                evaluated_at,
                horizon_days,
                signal_source,
                effect_type,
                source_context,
                context_class,
                context_candidate_state,
                context_policy_effect,
                watch_breakout_blocker,
                sample_count,
                matured_count,
                avg_watch_return_after_cost,
                {avg_benchmark_expr},
                {avg_excess_expr},
                opportunity_hit_rate_after_cost,
                {excess_hit_expr},
                negative_after_cost_rate,
                {negative_excess_expr},
                classification,
                recommendation,
                broker_execution_allowed,
                policy_auto_promotion_allowed
            FROM {CONTEXT_WATCH_SUMMARY_TABLE}
            WHERE classification IN ('opportunity_candidate', 'harmful_watch_noise', 'mixed_or_weak', 'needs_more_data')
            ORDER BY evaluated_at DESC, matured_count DESC, horizon_days, signal_source, source_context, context_class
            LIMIT 10
            """,
            retries=3,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        _record_health_local_fallback(
            source=CONTEXT_WATCH_SUMMARY_TABLE,
            fallback_type="operator_health_context_watch_evidence_failed",
            reason="Operator Health could not inspect context-watch evaluation evidence.",
            error=exc,
        )
        return _status(
            "error",
            "Could not inspect context-watch evaluation evidence.",
            error=f"{type(exc).__name__}: {exc}",
            usable=False,
            ready_for_policy_review=False,
            command=command,
            authority="research_only",
            broker_execution_allowed=False,
            policy_auto_promotion_allowed=False,
        )

    eval_row = eval_df.iloc[0] if not eval_df.empty else {}
    summary_row = summary_df.iloc[0] if not summary_df.empty else {}
    latest_eval = pd.to_datetime(eval_row.get("latest_evaluated_at"), utc=True, errors="coerce") if len(eval_row) else pd.NaT
    latest_load = pd.to_datetime(eval_row.get("latest_load_ts"), utc=True, errors="coerce") if len(eval_row) else pd.NaT
    evaluation_rows = int(eval_row.get("evaluation_rows") or 0) if len(eval_row) else 0
    matured_rows = int(eval_row.get("matured_rows") or 0) if len(eval_row) else 0
    summary_rows = int(summary_row.get("summary_rows") or 0) if len(summary_row) else 0
    opportunity_candidate_count = int(summary_row.get("opportunity_candidate_count") or 0) if len(summary_row) else 0
    harmful_watch_noise_count = int(summary_row.get("harmful_watch_noise_count") or 0) if len(summary_row) else 0
    mixed_or_weak_count = int(summary_row.get("mixed_or_weak_count") or 0) if len(summary_row) else 0
    needs_more_data_count = int(summary_row.get("needs_more_data_count") or 0) if len(summary_row) else 0
    benchmark_beta_group_count = int(summary_row.get("benchmark_beta_group_count") or 0) if len(summary_row) else 0
    sufficiently_matured_group_count = int(summary_row.get("sufficiently_matured_group_count") or 0) if len(summary_row) else 0
    reasons: list[str] = []
    if evaluation_rows <= 0 or summary_rows <= 0:
        reasons.append("empty_context_watch_evidence")
    if matured_rows <= 0:
        reasons.append("no_matured_context_watch_labels")
    if missing_benchmark_columns:
        reasons.append("context_watch_evidence_schema_missing_benchmark_columns")
    if benchmark_beta_group_count > 0:
        reasons.append("context_watch_evidence_benchmark_beta_only")
    if pd.isna(latest_eval):
        reasons.append("missing_latest_context_watch_evaluated_at")
        age_days = None
    else:
        age_days = max((pd.Timestamp.utcnow() - latest_eval).total_seconds() / 86400.0, 0.0)
        if age_days > 14:
            reasons.append("stale_context_watch_evidence")
    status = "ok" if not reasons else "warn"
    ready_for_policy_review = bool(status == "ok" and opportunity_candidate_count > 0 and matured_rows >= int(CONTEXT_WATCH_MIN_MATURED_ROWS))
    if status == "ok" and ready_for_policy_review:
        message = "Context-watch evidence has benchmark-attributed watch opportunity candidates for offline review."
    elif missing_benchmark_columns:
        message = "Context-watch evidence summary is missing benchmark-attribution columns; rerun the evaluator migration/output."
    elif benchmark_beta_group_count > 0:
        message = "Context-watch evidence has raw-positive but benchmark-beta-only groups; keep watch-priority influence research-only."
    elif status == "ok":
        message = "Context-watch evidence is available, but no benchmark-attributed watch opportunity is ready."
    else:
        message = "Context-watch evidence is missing, stale, or has no matured labels."
    return _status(
        status,
        message,
        usable=status == "ok",
        ready_for_policy_review=ready_for_policy_review,
        reasons=reasons,
        evaluations_table=CONTEXT_WATCH_EVALUATIONS_TABLE,
        summary_table=CONTEXT_WATCH_SUMMARY_TABLE,
        latest_evaluated_at=_json_ready(latest_eval),
        latest_load_ts=_json_ready(latest_load),
        age_days=None if age_days is None else round(age_days, 3),
        evaluation_rows=evaluation_rows,
        matured_rows=matured_rows,
        summary_rows=summary_rows,
        opportunity_candidate_count=opportunity_candidate_count,
        harmful_watch_noise_count=harmful_watch_noise_count,
        mixed_or_weak_count=mixed_or_weak_count,
        needs_more_data_count=needs_more_data_count,
        benchmark_beta_not_watch_alpha_count=benchmark_beta_group_count,
        missing_benchmark_columns=missing_benchmark_columns,
        sufficiently_matured_group_count=sufficiently_matured_group_count,
        min_matured_rows=int(CONTEXT_WATCH_MIN_MATURED_ROWS),
        groups=_records(groups),
        command=command,
        authority="research_only",
        broker_execution_allowed=False,
        policy_auto_promotion_allowed=False,
    )


def check_negative_pressure_evidence() -> dict[str, Any]:
    command = "python -m advisory.negative_pressure_evaluator --horizons 5 10 20"
    if not table_exists(NEGATIVE_PRESSURE_EVALUATIONS_TABLE) or not table_exists(NEGATIVE_PRESSURE_SUMMARY_TABLE):
        missing = [
            table
            for table in [NEGATIVE_PRESSURE_EVALUATIONS_TABLE, NEGATIVE_PRESSURE_SUMMARY_TABLE]
            if not table_exists(table)
        ]
        return _status(
            "warn",
            "Negative-pressure evaluation tables do not exist yet.",
            usable=False,
            ready_for_policy_review=False,
            reasons=["missing_negative_pressure_evidence_tables"],
            missing_tables=missing,
            command=command,
            authority="research_only",
            broker_execution_allowed=False,
            policy_auto_promotion_allowed=False,
        )
    summary_columns = table_columns(NEGATIVE_PRESSURE_SUMMARY_TABLE)
    required_benchmark_columns = {
        "avg_benchmark_forward_return",
        "avg_excess_avoided_return_after_cost",
        "excess_protective_hit_rate_after_cost",
        "false_positive_excess_rate_after_cost",
    }
    missing_benchmark_columns = sorted(required_benchmark_columns - summary_columns)
    avg_benchmark_expr = (
        "avg_benchmark_forward_return"
        if "avg_benchmark_forward_return" in summary_columns
        else "NULL::DOUBLE PRECISION AS avg_benchmark_forward_return"
    )
    avg_excess_expr = (
        "avg_excess_avoided_return_after_cost"
        if "avg_excess_avoided_return_after_cost" in summary_columns
        else "NULL::DOUBLE PRECISION AS avg_excess_avoided_return_after_cost"
    )
    excess_hit_expr = (
        "excess_protective_hit_rate_after_cost"
        if "excess_protective_hit_rate_after_cost" in summary_columns
        else "NULL::DOUBLE PRECISION AS excess_protective_hit_rate_after_cost"
    )
    false_positive_excess_expr = (
        "false_positive_excess_rate_after_cost"
        if "false_positive_excess_rate_after_cost" in summary_columns
        else "NULL::DOUBLE PRECISION AS false_positive_excess_rate_after_cost"
    )
    try:
        eval_df = sql_to_df(
            f"""
            SELECT
                MAX(evaluated_at) AS latest_evaluated_at,
                MAX(load_ts) AS latest_load_ts,
                COUNT(*) AS evaluation_rows,
                SUM(CASE WHEN matured THEN 1 ELSE 0 END) AS matured_rows,
                COUNT(DISTINCT symbol) AS symbol_count,
                COUNT(DISTINCT horizon_days) AS horizon_count
            FROM {NEGATIVE_PRESSURE_EVALUATIONS_TABLE}
            """,
            retries=3,
            statement_timeout_ms=10000,
        )
        summary_df = sql_to_df(
            f"""
            SELECT
                MAX(evaluated_at) AS latest_evaluated_at,
                COUNT(*) AS summary_rows,
                SUM(CASE WHEN classification = 'protective_candidate' THEN 1 ELSE 0 END) AS protective_candidate_count,
                SUM(CASE WHEN classification = 'benchmark_beta_not_derisk_alpha' THEN 1 ELSE 0 END) AS benchmark_beta_group_count,
                SUM(CASE WHEN classification = 'harmful_false_positive_pressure' THEN 1 ELSE 0 END) AS harmful_false_positive_count,
                SUM(CASE WHEN classification = 'mixed_or_weak' THEN 1 ELSE 0 END) AS mixed_or_weak_count,
                SUM(CASE WHEN classification = 'needs_more_data' THEN 1 ELSE 0 END) AS needs_more_data_count,
                SUM(CASE WHEN matured_count >= %s THEN 1 ELSE 0 END) AS sufficiently_matured_group_count
            FROM {NEGATIVE_PRESSURE_SUMMARY_TABLE}
            """,
            params=(int(NEGATIVE_PRESSURE_MIN_MATURED_ROWS),),
            retries=3,
            statement_timeout_ms=10000,
        )
        groups = sql_to_df(
            f"""
            SELECT
                evaluated_at,
                horizon_days,
                signal_source,
                effect_type,
                source_context,
                context_class,
                sample_count,
                matured_count,
                avg_avoided_return_after_cost,
                {avg_benchmark_expr},
                {avg_excess_expr},
                protective_hit_rate_after_cost,
                {excess_hit_expr},
                false_positive_rate_after_cost,
                {false_positive_excess_expr},
                classification,
                recommendation,
                broker_execution_allowed,
                policy_auto_promotion_allowed
            FROM {NEGATIVE_PRESSURE_SUMMARY_TABLE}
            WHERE classification IN (
                'protective_candidate',
                'benchmark_beta_not_derisk_alpha',
                'harmful_false_positive_pressure',
                'mixed_or_weak',
                'needs_more_data'
            )
            ORDER BY evaluated_at DESC, matured_count DESC, horizon_days, signal_source, source_context, context_class
            LIMIT 10
            """,
            retries=3,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        _record_health_local_fallback(
            source=NEGATIVE_PRESSURE_SUMMARY_TABLE,
            fallback_type="operator_health_negative_pressure_evidence_failed",
            reason="Operator Health could not inspect negative-pressure evaluation evidence.",
            error=exc,
        )
        return _status(
            "error",
            "Could not inspect negative-pressure evaluation evidence.",
            error=f"{type(exc).__name__}: {exc}",
            usable=False,
            ready_for_policy_review=False,
            command=command,
            authority="research_only",
            broker_execution_allowed=False,
            policy_auto_promotion_allowed=False,
        )

    eval_row = eval_df.iloc[0] if not eval_df.empty else {}
    summary_row = summary_df.iloc[0] if not summary_df.empty else {}
    latest_eval = pd.to_datetime(eval_row.get("latest_evaluated_at"), utc=True, errors="coerce") if len(eval_row) else pd.NaT
    latest_load = pd.to_datetime(eval_row.get("latest_load_ts"), utc=True, errors="coerce") if len(eval_row) else pd.NaT
    evaluation_rows = int(eval_row.get("evaluation_rows") or 0) if len(eval_row) else 0
    matured_rows = int(eval_row.get("matured_rows") or 0) if len(eval_row) else 0
    summary_rows = int(summary_row.get("summary_rows") or 0) if len(summary_row) else 0
    protective_candidate_count = int(summary_row.get("protective_candidate_count") or 0) if len(summary_row) else 0
    benchmark_beta_group_count = int(summary_row.get("benchmark_beta_group_count") or 0) if len(summary_row) else 0
    harmful_false_positive_count = int(summary_row.get("harmful_false_positive_count") or 0) if len(summary_row) else 0
    mixed_or_weak_count = int(summary_row.get("mixed_or_weak_count") or 0) if len(summary_row) else 0
    needs_more_data_count = int(summary_row.get("needs_more_data_count") or 0) if len(summary_row) else 0
    sufficiently_matured_group_count = int(summary_row.get("sufficiently_matured_group_count") or 0) if len(summary_row) else 0
    reasons: list[str] = []
    if evaluation_rows <= 0 or summary_rows <= 0:
        reasons.append("empty_negative_pressure_evidence")
    if matured_rows <= 0:
        reasons.append("no_matured_negative_pressure_labels")
    if missing_benchmark_columns:
        reasons.append("negative_pressure_evidence_schema_missing_benchmark_columns")
    if benchmark_beta_group_count > 0:
        reasons.append("negative_pressure_evidence_benchmark_beta_only")
    if pd.isna(latest_eval):
        reasons.append("missing_latest_negative_pressure_evaluated_at")
        age_days = None
    else:
        age_days = max((pd.Timestamp.utcnow() - latest_eval).total_seconds() / 86400.0, 0.0)
        if age_days > 14:
            reasons.append("stale_negative_pressure_evidence")
    status = "ok" if not reasons else "warn"
    ready_for_policy_review = bool(status == "ok" and protective_candidate_count > 0 and matured_rows >= int(NEGATIVE_PRESSURE_MIN_MATURED_ROWS))
    if status == "ok" and ready_for_policy_review:
        message = "Negative-pressure evidence has benchmark-attributed de-risk candidates for offline review."
    elif missing_benchmark_columns:
        message = "Negative-pressure evidence summary is missing benchmark-attribution columns; rerun the evaluator migration/output."
    elif benchmark_beta_group_count > 0:
        message = "Negative-pressure evidence is raw-protective but benchmark-beta-only; keep de-risk influence research-only."
    elif status == "ok":
        message = "Negative-pressure evidence is available, but no benchmark-attributed de-risk candidate is ready."
    else:
        message = "Negative-pressure evidence is missing, stale, or has no matured labels."
    return _status(
        status,
        message,
        usable=status == "ok",
        ready_for_policy_review=ready_for_policy_review,
        reasons=reasons,
        evaluations_table=NEGATIVE_PRESSURE_EVALUATIONS_TABLE,
        summary_table=NEGATIVE_PRESSURE_SUMMARY_TABLE,
        latest_evaluated_at=_json_ready(latest_eval),
        latest_load_ts=_json_ready(latest_load),
        age_days=None if age_days is None else round(age_days, 3),
        evaluation_rows=evaluation_rows,
        matured_rows=matured_rows,
        summary_rows=summary_rows,
        protective_candidate_count=protective_candidate_count,
        benchmark_beta_not_derisk_alpha_count=benchmark_beta_group_count,
        harmful_false_positive_count=harmful_false_positive_count,
        mixed_or_weak_count=mixed_or_weak_count,
        needs_more_data_count=needs_more_data_count,
        missing_benchmark_columns=missing_benchmark_columns,
        sufficiently_matured_group_count=sufficiently_matured_group_count,
        min_matured_rows=int(NEGATIVE_PRESSURE_MIN_MATURED_ROWS),
        groups=_records(groups),
        command=command,
        authority="research_only",
        broker_execution_allowed=False,
        policy_auto_promotion_allowed=False,
    )


def check_adversarial_review_evidence() -> dict[str, Any]:
    command = "python -m advisory.adversarial_review_evaluator --horizons 5 10 20"
    schema_command = "python -m advisory.adversarial_review_evaluator --ensure-schema-only"
    if not table_exists(ADVERSARIAL_REVIEW_EVALUATIONS_TABLE) or not table_exists(ADVERSARIAL_REVIEW_SUMMARY_TABLE):
        missing = [
            table
            for table in [ADVERSARIAL_REVIEW_EVALUATIONS_TABLE, ADVERSARIAL_REVIEW_SUMMARY_TABLE]
            if not table_exists(table)
        ]
        return _status(
            "warn",
            "Adversarial-review evaluation tables do not exist yet.",
            usable=False,
            ready_for_policy_review=False,
            reasons=["missing_adversarial_review_evidence_tables"],
            missing_tables=missing,
            command=command,
            schema_command=schema_command,
            authority="research_only",
            broker_execution_allowed=False,
            policy_auto_promotion_allowed=False,
        )
    try:
        eval_df = sql_to_df(
            f"""
            SELECT
                MAX(evaluated_at) AS latest_evaluated_at,
                MAX(load_ts) AS latest_load_ts,
                COUNT(*) AS evaluation_rows,
                SUM(CASE WHEN matured THEN 1 ELSE 0 END) AS matured_rows,
                COUNT(DISTINCT symbol) AS symbol_count,
                COUNT(DISTINCT horizon_days) AS horizon_count
            FROM {ADVERSARIAL_REVIEW_EVALUATIONS_TABLE}
            """,
            retries=3,
            statement_timeout_ms=10000,
        )
        summary_df = sql_to_df(
            f"""
            SELECT
                MAX(evaluated_at) AS latest_evaluated_at,
                COUNT(*) AS summary_rows,
                SUM(CASE WHEN recommendation = 'candidate_veto_policy_keep_or_tighten' THEN 1 ELSE 0 END) AS candidate_keep_or_tighten_count,
                SUM(CASE WHEN recommendation = 'benchmark_beta_not_veto_alpha' THEN 1 ELSE 0 END) AS benchmark_beta_group_count,
                SUM(CASE WHEN recommendation = 'needs_benchmark_attribution' THEN 1 ELSE 0 END) AS needs_benchmark_attribution_count,
                SUM(CASE WHEN recommendation = 'candidate_veto_policy_relax_or_review_false_positives' THEN 1 ELSE 0 END) AS relax_or_false_positive_count,
                SUM(CASE WHEN matured_count >= %s THEN 1 ELSE 0 END) AS sufficiently_matured_group_count
            FROM {ADVERSARIAL_REVIEW_SUMMARY_TABLE}
            """,
            params=(int(ADVERSARIAL_REVIEW_MIN_MATURED_ROWS),),
            retries=3,
            statement_timeout_ms=10000,
        )
        groups = sql_to_df(
            f"""
            SELECT
                evaluated_at,
                horizon_days,
                group_type,
                group_value,
                sample_count,
                matured_count,
                avg_forward_return_after_cost,
                avg_benchmark_forward_return,
                avg_avoided_loss_return_after_cost,
                avg_excess_avoided_loss_return_after_cost,
                false_positive_rate,
                excess_false_positive_rate,
                recommendation
            FROM {ADVERSARIAL_REVIEW_SUMMARY_TABLE}
            WHERE recommendation IN (
                'candidate_veto_policy_keep_or_tighten',
                'benchmark_beta_not_veto_alpha',
                'needs_benchmark_attribution',
                'candidate_veto_policy_relax_or_review_false_positives'
            )
            ORDER BY evaluated_at DESC, matured_count DESC, horizon_days, group_type, group_value
            LIMIT 10
            """,
            retries=3,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        _record_health_local_fallback(
            source=ADVERSARIAL_REVIEW_SUMMARY_TABLE,
            fallback_type="operator_health_adversarial_review_evidence_failed",
            reason="Operator Health could not inspect adversarial-review evaluation evidence.",
            error=exc,
        )
        return _status(
            "error",
            "Could not inspect adversarial-review evaluation evidence.",
            error=f"{type(exc).__name__}: {exc}",
            usable=False,
            ready_for_policy_review=False,
            command=command,
            schema_command=schema_command,
            authority="research_only",
            broker_execution_allowed=False,
            policy_auto_promotion_allowed=False,
        )

    eval_row = eval_df.iloc[0] if not eval_df.empty else {}
    summary_row = summary_df.iloc[0] if not summary_df.empty else {}
    latest_eval = pd.to_datetime(eval_row.get("latest_evaluated_at"), utc=True, errors="coerce") if len(eval_row) else pd.NaT
    latest_load = pd.to_datetime(eval_row.get("latest_load_ts"), utc=True, errors="coerce") if len(eval_row) else pd.NaT
    evaluation_rows = int(eval_row.get("evaluation_rows") or 0) if len(eval_row) else 0
    matured_rows = int(eval_row.get("matured_rows") or 0) if len(eval_row) else 0
    summary_rows = int(summary_row.get("summary_rows") or 0) if len(summary_row) else 0
    candidate_keep_or_tighten_count = int(summary_row.get("candidate_keep_or_tighten_count") or 0) if len(summary_row) else 0
    benchmark_beta_group_count = int(summary_row.get("benchmark_beta_group_count") or 0) if len(summary_row) else 0
    needs_benchmark_attribution_count = int(summary_row.get("needs_benchmark_attribution_count") or 0) if len(summary_row) else 0
    relax_or_false_positive_count = int(summary_row.get("relax_or_false_positive_count") or 0) if len(summary_row) else 0
    sufficiently_matured_group_count = int(summary_row.get("sufficiently_matured_group_count") or 0) if len(summary_row) else 0
    reasons: list[str] = []
    if evaluation_rows <= 0 or summary_rows <= 0:
        reasons.append("empty_adversarial_review_evidence")
    if matured_rows <= 0:
        reasons.append("no_matured_adversarial_review_labels")
    if benchmark_beta_group_count > 0:
        reasons.append("adversarial_review_evidence_benchmark_beta_only")
    if needs_benchmark_attribution_count > 0:
        reasons.append("adversarial_review_evidence_needs_benchmark_attribution")
    if pd.isna(latest_eval):
        reasons.append("missing_latest_adversarial_review_evaluated_at")
        age_days = None
    else:
        age_days = max((pd.Timestamp.utcnow() - latest_eval).total_seconds() / 86400.0, 0.0)
        if age_days > 14:
            reasons.append("stale_adversarial_review_evidence")
    status = "ok" if not reasons else "warn"
    ready_for_policy_review = bool(candidate_keep_or_tighten_count > 0 and matured_rows >= int(ADVERSARIAL_REVIEW_MIN_MATURED_ROWS))
    if status == "ok" and ready_for_policy_review:
        message = "Adversarial-review evidence has keep/tighten candidates for offline review."
    elif benchmark_beta_group_count > 0:
        message = "Adversarial-review evidence is raw-helpful but benchmark-beta-only; keep veto policy research-only."
    elif needs_benchmark_attribution_count > 0:
        message = "Adversarial-review evidence needs benchmark attribution before policy review."
    elif status == "ok":
        message = "Adversarial-review evidence is available, but no keep/tighten candidate is ready."
    else:
        message = "Adversarial-review evidence is missing, stale, or has no matured labels."
    return _status(
        status,
        message,
        usable=status == "ok",
        ready_for_policy_review=ready_for_policy_review,
        reasons=reasons,
        evaluations_table=ADVERSARIAL_REVIEW_EVALUATIONS_TABLE,
        summary_table=ADVERSARIAL_REVIEW_SUMMARY_TABLE,
        latest_evaluated_at=_json_ready(latest_eval),
        latest_load_ts=_json_ready(latest_load),
        age_days=None if age_days is None else round(age_days, 3),
        evaluation_rows=evaluation_rows,
        matured_rows=matured_rows,
        summary_rows=summary_rows,
        candidate_keep_or_tighten_count=candidate_keep_or_tighten_count,
        benchmark_beta_not_veto_alpha_count=benchmark_beta_group_count,
        needs_benchmark_attribution_count=needs_benchmark_attribution_count,
        relax_or_false_positive_count=relax_or_false_positive_count,
        sufficiently_matured_group_count=sufficiently_matured_group_count,
        min_matured_rows=int(ADVERSARIAL_REVIEW_MIN_MATURED_ROWS),
        groups=_records(groups),
        command=command,
        schema_command=schema_command,
        authority="research_only",
        broker_execution_allowed=False,
        policy_auto_promotion_allowed=False,
    )


def check_signal_quality_split_evidence() -> dict[str, Any]:
    command = "python -m advisory.signal_quality_split_evaluator --stability-report --horizons 5 10 20 --format json"
    schema_command = "python -m advisory.signal_quality_split_evaluator --ensure-schema-only"
    if not table_exists(SIGNAL_QUALITY_SPLIT_EVALUATIONS_TABLE) or not table_exists(SIGNAL_QUALITY_SPLIT_SUMMARY_TABLE):
        missing = [
            table
            for table in [SIGNAL_QUALITY_SPLIT_EVALUATIONS_TABLE, SIGNAL_QUALITY_SPLIT_SUMMARY_TABLE]
            if not table_exists(table)
        ]
        return _status(
            "warn",
            "Signal-quality narrowed split evaluation tables do not exist yet.",
            usable=False,
            ready_for_policy_review=False,
            reasons=["missing_signal_quality_split_evidence_tables"],
            missing_tables=missing,
            command=command,
            schema_command=schema_command,
            authority="research_only",
            broker_execution_allowed=False,
            policy_auto_promotion_allowed=False,
        )
    try:
        eval_df = sql_to_df(
            f"""
            SELECT
                MAX(evaluated_at) AS latest_evaluated_at,
                MAX(load_ts) AS latest_load_ts,
                COUNT(*) AS evaluation_rows,
                SUM(CASE WHEN matured THEN 1 ELSE 0 END) AS matured_rows,
                COUNT(DISTINCT symbol) AS symbol_count,
                COUNT(DISTINCT horizon_days) AS horizon_count
            FROM {SIGNAL_QUALITY_SPLIT_EVALUATIONS_TABLE}
            """,
            retries=3,
            statement_timeout_ms=10000,
        )
        summary = sql_to_df(
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
                baseline_selected_count,
                baseline_avg_forward_return_after_cost,
                lift_vs_technical_only,
                classification,
                recommendation,
                authority,
                broker_execution_allowed,
                policy_auto_promotion_allowed,
                load_ts
            FROM {SIGNAL_QUALITY_SPLIT_SUMMARY_TABLE}
            ORDER BY evaluated_at DESC, horizon_days, source_family, split_axis, split_value
            LIMIT 1000
            """,
            retries=3,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        _record_health_local_fallback(
            source=SIGNAL_QUALITY_SPLIT_SUMMARY_TABLE,
            fallback_type="operator_health_signal_quality_split_evidence_failed",
            reason="Operator Health could not inspect signal-quality narrowed split evidence.",
            error=exc,
        )
        return _status(
            "error",
            "Could not inspect signal-quality narrowed split evidence.",
            error=f"{type(exc).__name__}: {exc}",
            usable=False,
            ready_for_policy_review=False,
            command=command,
            schema_command=schema_command,
            authority="research_only",
            broker_execution_allowed=False,
            policy_auto_promotion_allowed=False,
        )

    eval_row = eval_df.iloc[0] if not eval_df.empty else {}
    latest_eval = pd.to_datetime(eval_row.get("latest_evaluated_at"), utc=True, errors="coerce") if len(eval_row) else pd.NaT
    latest_load = pd.to_datetime(eval_row.get("latest_load_ts"), utc=True, errors="coerce") if len(eval_row) else pd.NaT
    evaluation_rows = int(eval_row.get("evaluation_rows") or 0) if len(eval_row) else 0
    matured_rows = int(eval_row.get("matured_rows") or 0) if len(eval_row) else 0
    summary_rows = int(len(summary))
    stability_report = build_split_stability_report(summary) if not summary.empty else build_split_stability_report(pd.DataFrame())
    splits = stability_report.get("splits") if isinstance(stability_report.get("splits"), list) else []
    baseline_unavailable_count = sum(int(row.get("baseline_unavailable_window_count") or 0) for row in splits if isinstance(row, dict))
    stable_candidate_count = int(stability_report.get("stable_candidate_count") or 0)
    harmful_negative_control_count = int(stability_report.get("harmful_negative_control_count") or 0)
    unstable_or_horizon_sensitive_count = int(stability_report.get("unstable_or_horizon_sensitive_count") or 0)
    reasons: list[str] = []
    if evaluation_rows <= 0 or summary_rows <= 0:
        reasons.append("empty_signal_quality_split_evidence")
    if matured_rows <= 0:
        reasons.append("no_matured_signal_quality_split_labels")
    if baseline_unavailable_count > 0:
        reasons.append("signal_quality_split_technical_baseline_unavailable")
    if pd.isna(latest_eval):
        reasons.append("missing_latest_signal_quality_split_evaluated_at")
        age_days = None
    else:
        age_days = max((pd.Timestamp.utcnow() - latest_eval).total_seconds() / 86400.0, 0.0)
        if age_days > 14:
            reasons.append("stale_signal_quality_split_evidence")
    status = "ok" if not reasons else "warn"
    ready_for_policy_review = bool(stable_candidate_count > 0 and status == "ok")
    if status == "ok" and ready_for_policy_review:
        message = "Signal-quality narrowed split evidence has stable candidates for offline reviewed-rule work."
    elif baseline_unavailable_count > 0:
        message = "Signal-quality narrowed split evidence has incomplete technical-only baselines; keep splits research-only."
    elif status == "ok":
        message = "Signal-quality narrowed split evidence is available, but no stable candidate is ready."
    else:
        message = "Signal-quality narrowed split evidence is missing, stale, or has no matured labels."
    return _status(
        status,
        message,
        usable=status == "ok",
        ready_for_policy_review=ready_for_policy_review,
        reasons=reasons,
        evaluations_table=SIGNAL_QUALITY_SPLIT_EVALUATIONS_TABLE,
        summary_table=SIGNAL_QUALITY_SPLIT_SUMMARY_TABLE,
        latest_evaluated_at=_json_ready(latest_eval),
        latest_load_ts=_json_ready(latest_load),
        age_days=None if age_days is None else round(age_days, 3),
        evaluation_rows=evaluation_rows,
        matured_rows=matured_rows,
        summary_rows=summary_rows,
        split_count=int(stability_report.get("split_count") or 0),
        stable_candidate_count=stable_candidate_count,
        harmful_negative_control_count=harmful_negative_control_count,
        unstable_or_horizon_sensitive_count=unstable_or_horizon_sensitive_count,
        baseline_unavailable_window_count=baseline_unavailable_count,
        min_stable_windows=int(SIGNAL_QUALITY_SPLIT_MIN_STABLE_WINDOWS),
        splits=splits[:10],
        command=command,
        schema_command=schema_command,
        authority="research_only",
        broker_execution_allowed=False,
        policy_auto_promotion_allowed=False,
    )


def check_context_overlay_reliability() -> dict[str, Any]:
    command = "python -m advisory.context_overlay_reliability_report --horizons 5 10 20 --format json"
    if not table_exists(CONTEXT_OVERLAY_RELIABILITY_SUMMARY_TABLE):
        return _status(
            "warn",
            "Context-overlay reliability report has not been persisted yet.",
            usable=False,
            reasons=["missing_reliability_table"],
            command=command,
        )
    try:
        latest_df = sql_to_df(
            f"SELECT MAX(evaluated_at) AS latest_evaluated_at FROM {CONTEXT_OVERLAY_RELIABILITY_SUMMARY_TABLE}",
            retries=3,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        _record_health_local_fallback(
            source=CONTEXT_OVERLAY_RELIABILITY_SUMMARY_TABLE,
            fallback_type="operator_health_context_overlay_reliability_summary_check_failed",
            reason="Operator Health could not inspect context-overlay reliability freshness.",
            error=exc,
        )
        return _status("error", "Could not inspect context-overlay reliability summary.", error=f"{type(exc).__name__}: {exc}", usable=False)

    latest = pd.to_datetime(latest_df.iloc[0].get("latest_evaluated_at"), utc=True, errors="coerce") if not latest_df.empty else pd.NaT
    if pd.isna(latest):
        return _status(
            "warn",
            "Context-overlay reliability summary table has no evaluated rows.",
            usable=False,
            reasons=["empty_reliability_table"],
            command=command,
        )

    try:
        rows = sql_to_df(
            f"""
            SELECT *
            FROM {CONTEXT_OVERLAY_RELIABILITY_SUMMARY_TABLE}
            WHERE evaluated_at = %(latest)s
            ORDER BY source_family
            """,
            params={"latest": latest},
            retries=3,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        _record_health_local_fallback(
            source=CONTEXT_OVERLAY_RELIABILITY_SUMMARY_TABLE,
            fallback_type="operator_health_context_overlay_reliability_rows_load_failed",
            reason="Operator Health could not load latest context-overlay reliability rows.",
            error=exc,
            metadata={"latest_evaluated_at": latest.isoformat()},
        )
        return _status("error", "Could not read latest context-overlay reliability rows.", error=f"{type(exc).__name__}: {exc}", usable=False)

    now = pd.Timestamp.utcnow()
    age_days = float((now - latest).total_seconds() / 86400.0)
    matured = pd.to_numeric(rows.get("total_matured_count", pd.Series(dtype=float)), errors="coerce")
    max_matured = int(matured.max()) if not matured.dropna().empty else 0
    classifications = rows.get("classification", pd.Series(dtype=object)).astype("string").str.strip().str.lower() if not rows.empty else pd.Series(dtype="string")
    reasons: list[str] = []
    if age_days > SIGNAL_QUALITY_MAX_AGE_DAYS:
        reasons.append("stale_context_overlay_reliability")
    if max_matured < SIGNAL_QUALITY_MIN_MATURED_ROWS:
        reasons.append("insufficient_matured_rows")
    status = "ok" if not reasons else "warn"
    return _status(
        status,
        "Latest context-overlay reliability report is usable for review-only context intake."
        if status == "ok"
        else "Latest context-overlay reliability report is missing, stale, or too sparse.",
        usable=status == "ok",
        reasons=reasons,
        latest_evaluated_at=_json_ready(latest),
        age_days=round(age_days, 3),
        max_age_days=SIGNAL_QUALITY_MAX_AGE_DAYS,
        family_count=int(len(rows)),
        max_matured_rows=max_matured,
        min_matured_rows=SIGNAL_QUALITY_MIN_MATURED_ROWS,
        candidate_helpful_count=int(classifications.eq("candidate_helpful").sum()),
        protective_candidate_count=int(classifications.eq("protective_candidate").sum()),
        hurts_or_no_lift_count=int(classifications.isin(["hurts_or_no_lift", "negative_after_cost", "benchmark_beta_not_overlay_alpha"]).sum()),
        inconsistent_or_horizon_sensitive_count=int(classifications.eq("inconsistent_or_horizon_sensitive").sum()),
        needs_benchmark_attribution_count=int(classifications.eq("needs_benchmark_attribution").sum()),
        benchmark_beta_not_overlay_alpha_count=int(classifications.eq("benchmark_beta_not_overlay_alpha").sum()),
        suppressed_reliability_count=int(classifications.isin(SIGNAL_QUALITY_OVERLAY_SUPPRESSED_CONTEXT_CLASS_RELIABILITY).sum()),
        suppressed_reliability_classes=sorted(SIGNAL_QUALITY_OVERLAY_SUPPRESSED_CONTEXT_CLASS_RELIABILITY),
        authority="research_only",
        broker_execution_allowed=False,
        policy_auto_promotion_allowed=False,
        rows=_records(rows.head(20)),
        command=command,
    )


def check_macro_sector_alias_coverage() -> dict[str, Any]:
    command = "python -m advisory.macro_context_overlays --dry-run --format json"
    try:
        overlays, meta = build_macro_context_overlays()
    except Exception as exc:
        _record_health_local_fallback(
            source="advisory.macro_context_overlays",
            fallback_type="operator_health_macro_sector_alias_coverage_failed",
            reason="Operator Health could not build macro context overlays for sector-alias coverage diagnostics.",
            error=exc,
        )
        return _status(
            "error",
            "Macro sector-alias coverage check failed.",
            usable=False,
            error=f"{type(exc).__name__}: {exc}",
            command=command,
        )

    coverage = meta.get("sector_alias_coverage") if isinstance(meta, dict) else {}
    if not isinstance(coverage, dict):
        coverage = {}
    unmapped = coverage.get("unmapped_sectors") if isinstance(coverage.get("unmapped_sectors"), list) else []
    intentionally_broad = (
        coverage.get("intentionally_broad_sectors")
        if isinstance(coverage.get("intentionally_broad_sectors"), list)
        else []
    )
    unmapped_count = int(coverage.get("unmapped_sector_count") or len(unmapped))
    status = "ok" if unmapped_count <= 0 else "warn"
    return _status(
        status,
        "Macro sector aliases cover the generated macro overlay sectors."
        if status == "ok"
        else "Some generated macro overlay sectors are not mapped to market-context universe sector codes.",
        usable=status == "ok",
        overlay_count=int(len(overlays)),
        sector_count=int(coverage.get("sector_count") or 0),
        mapped_sector_count=int(coverage.get("mapped_sector_count") or 0),
        intentionally_broad_sector_count=int(coverage.get("intentionally_broad_sector_count") or len(intentionally_broad)),
        unmapped_sector_count=unmapped_count,
        unmapped_sectors=unmapped,
        intentionally_broad_sectors=intentionally_broad,
        authority_scope="diagnostic_only",
        broker_execution_allowed=False,
        command=command,
        coverage=coverage,
    )


def check_theme_sector_alias_coverage() -> dict[str, Any]:
    command = "python -m advisory.news_theme_engine build-overlays --dry-run --format json"
    try:
        overlays, meta = build_theme_context_overlays()
    except Exception as exc:
        _record_health_local_fallback(
            source="advisory.news_theme_engine",
            fallback_type="operator_health_theme_sector_alias_coverage_failed",
            reason="Operator Health could not build news-theme context overlays for sector-alias coverage diagnostics.",
            error=exc,
        )
        return _status(
            "error",
            "Theme sector-alias coverage check failed.",
            usable=False,
            error=f"{type(exc).__name__}: {exc}",
            command=command,
        )

    coverage = meta.get("sector_alias_coverage") if isinstance(meta, dict) else {}
    if not isinstance(coverage, dict):
        coverage = {}
    unmapped = coverage.get("unmapped_sectors") if isinstance(coverage.get("unmapped_sectors"), list) else []
    intentionally_broad = (
        coverage.get("intentionally_broad_sectors")
        if isinstance(coverage.get("intentionally_broad_sectors"), list)
        else []
    )
    unmapped_count = int(coverage.get("unmapped_sector_count") or len(unmapped))
    status = "ok" if unmapped_count <= 0 else "warn"
    return _status(
        status,
        "Theme sector aliases cover the generated news-theme overlay sectors."
        if status == "ok"
        else "Some generated news-theme overlay sectors are not mapped to market-context universe sector codes.",
        usable=status == "ok",
        overlay_count=int(len(overlays)),
        sector_count=int(coverage.get("sector_count") or 0),
        mapped_sector_count=int(coverage.get("mapped_sector_count") or 0),
        intentionally_broad_sector_count=int(coverage.get("intentionally_broad_sector_count") or len(intentionally_broad)),
        unmapped_sector_count=unmapped_count,
        unmapped_sectors=unmapped,
        intentionally_broad_sectors=intentionally_broad,
        authority_scope="diagnostic_only",
        broker_execution_allowed=False,
        command=command,
        coverage=coverage,
    )


def _signal_quality_rule_text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.operator_health",
            fallback_type="operator_health_signal_quality_rule_text_missing_check_failed",
            source="signal_quality_overlay_rules",
            severity="warn",
            reason="Operator Health could not evaluate a signal-quality rule field for missingness and used string fallback.",
            error=exc,
            metadata={"value_type": type(value).__name__, "value_excerpt": str(value)[:200]},
        )
        pass
    return str(value).strip().lower()


def _signal_quality_split_value_tokens(value: Any) -> list[str]:
    text = _signal_quality_rule_text(value)
    if not text:
        return []
    return [part.strip() for part in text.split("|")]


def _signal_quality_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            record_local_fallback_event(
                module="advisory.operator_health",
                fallback_type="operator_health_signal_quality_json_list_parse_failed",
                source="signal_quality_overlay_rules",
                severity="warn",
                reason="Operator Health could not parse a signal-quality JSON list and used an empty list fallback.",
                error=exc,
                metadata={"value_excerpt": value[:500], "value_length": len(value)},
            )
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _signal_quality_rule_context_class(rule: dict[str, Any]) -> str | None:
    split_axis = _signal_quality_rule_text(rule.get("split_axis"))
    split_tokens = _signal_quality_split_value_tokens(rule.get("split_value"))
    rule_class = _signal_quality_rule_text(rule.get("context_class"))
    if split_axis == "context_class_direction":
        return split_tokens[0] if split_tokens else rule_class or None
    if split_axis in {"context_class", "event_class", "pressure_class", "macro_signal", "theme", "rule_id"}:
        return split_tokens[0] if split_tokens else rule_class or None
    if not split_axis and rule_class:
        return rule_class
    return None


def _signal_quality_context_class_reliability(
    reliability: dict[str, Any] | None,
    context_class: str | None,
) -> dict[str, Any] | None:
    target = _signal_quality_rule_text(context_class).upper()
    if not reliability or not target:
        return None
    report = _signal_quality_reliability_report(reliability)
    diagnostics = []
    if isinstance(report, dict):
        diagnostics = _signal_quality_json_list(report.get("context_class_diagnostics"))
    if not diagnostics:
        diagnostics = _signal_quality_json_list(reliability.get("context_class_diagnostics"))
    for item in diagnostics:
        if not isinstance(item, dict):
            continue
        if _signal_quality_rule_text(item.get("context_class")).upper() != target:
            continue
        return {
            **item,
            "context_class": item.get("context_class") or target,
            "source_family": reliability.get("source_family"),
            "authority_scope": "research_only",
            "action_policy_effect": "annotation_only_no_ranking_change",
            "broker_execution_allowed": False,
        }
    return None


def _signal_quality_reliability_report(reliability: dict[str, Any] | None) -> dict[str, Any]:
    if not reliability:
        return {}
    report = _json_dict(
        reliability.get("report_json"),
        source=CONTEXT_OVERLAY_RELIABILITY_SUMMARY_TABLE,
        fallback_type="operator_health_context_reliability_report_json_parse_failed",
        metadata={"source_family": reliability.get("source_family")},
    )
    if report:
        return report
    return reliability


def _signal_quality_runtime_policy_contract(reliability: dict[str, Any] | None) -> dict[str, Any]:
    report = _signal_quality_reliability_report(reliability)
    contract = report.get("runtime_policy_contract") if isinstance(report, dict) else None
    if isinstance(contract, dict):
        return contract
    classification = report.get("classification") if isinstance(report, dict) else None
    if classification is None and reliability:
        classification = reliability.get("classification")
    return reliability_runtime_policy_contract(classification)


def _signal_quality_runtime_contract_allows(reliability: dict[str, Any] | None, use_name: str) -> bool:
    contract = _signal_quality_runtime_policy_contract(reliability)
    allowed = contract.get("allowed_runtime_uses")
    return isinstance(allowed, dict) and bool(allowed.get(use_name))


def _signal_quality_rule_supported_effect(rule: dict[str, Any]) -> bool:
    effect = _signal_quality_rule_text(rule.get("action_policy_effect"))
    return effect in SIGNAL_QUALITY_OVERLAY_NEGATIVE_BLOCK_EFFECTS | SIGNAL_QUALITY_OVERLAY_POSITIVE_WATCH_EFFECTS


def _signal_quality_rule_supported_split_scope(rule: dict[str, Any]) -> tuple[bool, str | None]:
    split_axis = _signal_quality_rule_text(rule.get("split_axis"))
    split_value = _signal_quality_rule_text(rule.get("split_value"))
    if not split_axis:
        return True, None
    if split_axis not in SIGNAL_QUALITY_OVERLAY_SUPPORTED_SPLIT_AXES:
        return False, "unsupported_split_axis"
    if split_axis == "context_class_direction" and split_value and len(_signal_quality_split_value_tokens(split_value)) < 2:
        return False, "invalid_context_class_direction_split_value"
    return True, None


def _signal_quality_rule_is_split_scoped(rule: dict[str, Any]) -> bool:
    return bool(_signal_quality_rule_text(rule.get("split_axis")) or _signal_quality_rule_text(rule.get("split_value")))


def _signal_quality_split_key_from_rule(rule: dict[str, Any]) -> tuple[str, int, str, str] | None:
    source_family = _signal_quality_rule_text(rule.get("context_source_family"))
    horizon = pd.to_numeric(rule.get("minimum_horizon_days") or rule.get("horizon_days"), errors="coerce")
    split_axis = _signal_quality_rule_text(rule.get("split_axis"))
    split_value = _signal_quality_rule_text(rule.get("split_value"))
    if not source_family or pd.isna(horizon) or not split_axis or not split_value:
        return None
    return source_family, int(horizon), split_axis, split_value


def _signal_quality_split_key_from_stability_row(row: dict[str, Any]) -> tuple[str, int, str, str] | None:
    source_family = _signal_quality_rule_text(row.get("source_family"))
    horizon = pd.to_numeric(row.get("horizon_days"), errors="coerce")
    split_axis = _signal_quality_rule_text(row.get("split_axis"))
    split_value = _signal_quality_rule_text(row.get("split_value"))
    if not source_family or pd.isna(horizon) or not split_axis or not split_value:
        return None
    return source_family, int(horizon), split_axis, split_value


def _load_signal_quality_split_stability_for_rules() -> tuple[str, dict[tuple[str, int, str, str], dict[str, Any]], str | None]:
    if not table_exists(SIGNAL_QUALITY_SPLIT_SUMMARY_TABLE):
        return "missing", {}, "split_summary_table_missing"
    try:
        summary = sql_to_df(
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
                baseline_selected_count,
                baseline_avg_forward_return_after_cost,
                lift_vs_technical_only,
                classification,
                recommendation,
                authority,
                broker_execution_allowed,
                policy_auto_promotion_allowed,
                load_ts
            FROM {SIGNAL_QUALITY_SPLIT_SUMMARY_TABLE}
            ORDER BY evaluated_at DESC, horizon_days, source_family, split_axis, split_value
            LIMIT 1000
            """,
            retries=3,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        _record_health_local_fallback(
            source=SIGNAL_QUALITY_SPLIT_SUMMARY_TABLE,
            fallback_type="operator_health_signal_quality_overlay_rules_split_stability_load_failed",
            reason="Operator Health could not load persisted narrowed split stability for trusted overlay-rule eligibility.",
            error=exc,
        )
        return "error", {}, "split_stability_load_failed"
    if summary.empty:
        return "empty", {}, "split_summary_empty"
    report = build_split_stability_report(summary)
    split_rows = report.get("splits") if isinstance(report.get("splits"), list) else []
    by_key: dict[tuple[str, int, str, str], dict[str, Any]] = {}
    for row in split_rows:
        if not isinstance(row, dict):
            continue
        key = _signal_quality_split_key_from_stability_row(row)
        if key is not None:
            by_key[key] = row
    return "ok", by_key, None


def check_signal_quality_overlay_rules() -> dict[str, Any]:
    try:
        rules_payload = load_signal_quality_overlay_rules()
    except Exception as exc:
        _record_health_local_fallback(
            source="config/advisory_setups.yaml",
            fallback_type="operator_health_signal_quality_overlay_rules_load_failed",
            reason="Operator Health could not load signal-quality overlay rules.",
            error=exc,
        )
        return _status(
            "error",
            "Could not load signal-quality overlay rules.",
            error=f"{type(exc).__name__}: {exc}",
            command="python -m advisory.operator_health --full --skip-dhan",
        )

    rules = rules_payload.get("rules") if isinstance(rules_payload, dict) else []
    if not isinstance(rules, list):
        rules = []
    trusted_rules = [
        rule
        for rule in rules
        if isinstance(rule, dict)
        and rule.get("valid")
        and str(rule.get("status") or "").strip().lower() == "trusted_overlay"
    ]
    split_scoped_trusted_rules = [rule for rule in trusted_rules if _signal_quality_rule_is_split_scoped(rule)]
    reliability_by_family: dict[str, dict[str, Any]] = {}
    reliability_status = "unavailable"
    reliability_latest = None
    if table_exists(CONTEXT_OVERLAY_RELIABILITY_SUMMARY_TABLE):
        try:
            latest_df = sql_to_df(
                f"SELECT MAX(evaluated_at) AS latest_evaluated_at FROM {CONTEXT_OVERLAY_RELIABILITY_SUMMARY_TABLE}",
                retries=3,
                statement_timeout_ms=10000,
            )
            latest = pd.to_datetime(latest_df.iloc[0].get("latest_evaluated_at"), utc=True, errors="coerce") if not latest_df.empty else pd.NaT
            if pd.notna(latest):
                reliability_latest = latest
                rows = sql_to_df(
                    f"""
                    SELECT *
                    FROM {CONTEXT_OVERLAY_RELIABILITY_SUMMARY_TABLE}
                    WHERE evaluated_at = %(latest)s
                    """,
                    params={"latest": latest},
                    retries=3,
                    statement_timeout_ms=10000,
                )
                reliability_by_family = {
                    str(row.get("source_family") or "").strip().lower(): row
                    for row in _records(rows)
                    if str(row.get("source_family") or "").strip()
                }
                reliability_status = "ok" if reliability_by_family else "empty"
            else:
                reliability_status = "empty"
        except Exception as exc:
            _record_health_local_fallback(
                source=CONTEXT_OVERLAY_RELIABILITY_SUMMARY_TABLE,
                fallback_type="operator_health_signal_quality_overlay_rules_reliability_load_failed",
                reason="Operator Health could not load context-overlay reliability for trusted rule eligibility.",
                error=exc,
            )
            reliability_status = "error"
    split_stability_status = "not_required"
    split_stability_block_reason = None
    split_stability_by_key: dict[tuple[str, int, str, str], dict[str, Any]] = {}
    if split_scoped_trusted_rules:
        split_stability_status, split_stability_by_key, split_stability_block_reason = _load_signal_quality_split_stability_for_rules()
    rows: list[dict[str, Any]] = []
    eligible_count = 0
    blocked_count = 0
    for rule in trusted_rules:
        source_family = str(rule.get("context_source_family") or "").strip().lower()
        reliability = reliability_by_family.get(source_family) if source_family else None
        reliability_classification = str((reliability or {}).get("classification") or "").strip().lower() or None
        runtime_policy_contract = _signal_quality_runtime_policy_contract(reliability)
        watch_priority_allowed = _signal_quality_runtime_contract_allows(reliability, "watch_priority")
        stability_classification = str(rule.get("stability_classification") or "").strip() or None
        supported_effect = _signal_quality_rule_supported_effect(rule)
        supported_split_scope, split_scope_block_reason = _signal_quality_rule_supported_split_scope(rule)
        split_key = _signal_quality_split_key_from_rule(rule)
        matching_split_stability = split_stability_by_key.get(split_key) if split_key is not None else None
        split_stability_supported = True
        split_stability_reason = None
        if _signal_quality_rule_is_split_scoped(rule):
            split_stability_supported = False
            if split_stability_status != "ok":
                split_stability_reason = split_stability_block_reason or f"split_stability_{split_stability_status}"
            elif split_key is None:
                split_stability_reason = "split_rule_missing_match_fields"
            elif not matching_split_stability:
                split_stability_reason = "current_split_stability_missing"
            elif int(matching_split_stability.get("baseline_unavailable_window_count") or 0) > 0:
                split_stability_reason = "current_split_technical_baseline_unavailable"
            elif str(matching_split_stability.get("classification") or "").strip() != "stable_candidate":
                split_stability_reason = "current_split_stability_not_stable_candidate"
            else:
                split_stability_supported = True
        rule_context_class = _signal_quality_rule_context_class(rule)
        class_reliability = _signal_quality_context_class_reliability(reliability, rule_context_class)
        class_reliability_classification = (
            str((class_reliability or {}).get("classification") or "").strip().lower() or None
        )
        class_reliability_supported = class_reliability_classification not in SIGNAL_QUALITY_OVERLAY_SUPPRESSED_CONTEXT_CLASS_RELIABILITY
        reasons: list[str] = []
        if not watch_priority_allowed:
            reasons.append("current_reliability_runtime_contract_disallows_watch_priority")
        if stability_classification != "stable_candidate":
            reasons.append("missing_stable_candidate_cross_window_evidence")
        if not supported_effect:
            reasons.append("unsupported_action_policy_effect")
        if not supported_split_scope:
            reasons.append(split_scope_block_reason or "unsupported_split_scope")
        if not split_stability_supported:
            reasons.append(split_stability_reason or "current_split_stability_not_supported")
        if not class_reliability_supported:
            reasons.append("context_class_reliability_suppressed")
        eligible = not reasons
        eligible_count += int(eligible)
        blocked_count += int(not eligible)
        rows.append(
            {
                "rule_id": rule.get("rule_id"),
                "overlay": rule.get("overlay"),
                "context_source_family": source_family or None,
                "status": rule.get("status"),
                "runtime_eligible_for_signal_quality_overlay_consumer": eligible,
                "runtime_reliability_gate": "runtime_policy_contract_watch_priority_required",
                "runtime_reliability_classification": reliability_classification,
                "runtime_policy_contract": runtime_policy_contract,
                "runtime_watch_priority_allowed": watch_priority_allowed,
                "runtime_stability_gate": "stable_candidate_required",
                "runtime_stability_classification": stability_classification,
                "runtime_supported_effect_gate": "supported_review_only_effect_required",
                "runtime_supported_effect": supported_effect,
                "runtime_supported_split_scope_gate": "known_split_axis_or_broad_rule_required",
                "runtime_supported_split_scope": supported_split_scope,
                "runtime_split_scope_block_reason": split_scope_block_reason,
                "runtime_current_split_stability_gate": "current_persisted_split_stability_required",
                "runtime_current_split_stability_status": split_stability_status,
                "runtime_current_split_stability_supported": split_stability_supported,
                "runtime_current_split_stability_block_reason": split_stability_reason,
                "runtime_current_split_stability": matching_split_stability,
                "runtime_context_class_reliability_gate": "exact_context_class_not_harmful_required",
                "runtime_context_class": rule_context_class,
                "runtime_context_class_reliability": class_reliability,
                "runtime_context_class_reliability_classification": class_reliability_classification,
                "runtime_context_class_reliability_supported": class_reliability_supported,
                "runtime_context_class_reliability_block_reason": (
                    "context_class_reliability_suppressed" if not class_reliability_supported else None
                ),
                "blocked_reasons": reasons,
                "action_policy_effect": rule.get("action_policy_effect"),
                "broker_execution_allowed": False,
                "policy_auto_promotion_allowed": False,
            }
        )

    status = "ok"
    reasons: list[str] = []
    if (rules_payload.get("summary") or {}).get("issue_count"):
        status = "warn"
        reasons.append("config_rule_issues")
    if trusted_rules and blocked_count:
        status = "warn"
        reasons.append("trusted_rules_not_runtime_eligible")
    if trusted_rules and reliability_status in {"unavailable", "empty", "error"}:
        status = "warn" if reliability_status != "error" else "error"
        reasons.append(f"reliability_{reliability_status}")
    if split_scoped_trusted_rules and split_stability_status in {"missing", "empty", "error"}:
        status = "warn" if split_stability_status != "error" else "error"
        reasons.append(f"split_stability_{split_stability_status}")
    return _status(
        status,
        "Trusted signal-quality overlay rules are runtime-eligible under current evidence gates."
        if status == "ok"
        else "Some trusted signal-quality overlay rules are not runtime-eligible under current reliability/stability gates.",
        reasons=reasons,
        rule_count=int(len(rules)),
        trusted_rule_count=int(len(trusted_rules)),
        runtime_eligible_rule_count=int(eligible_count),
        runtime_blocked_rule_count=int(blocked_count),
        reliability_status=reliability_status,
        reliability_latest_evaluated_at=_json_ready(reliability_latest),
        split_scoped_trusted_rule_count=int(len(split_scoped_trusted_rules)),
        split_stability_status=split_stability_status,
        split_stability_block_reason=split_stability_block_reason,
        operator_boundary={
            "read_only": True,
            "broker_execution_enabled": False,
            "policy_auto_promotion_allowed": False,
        },
        rows=rows[:20],
        command="python -m advisory.context_overlay_reliability_report --horizons 5 10 20 --format json",
    )


def _count_fallback_rows(rows: list[dict[str, Any]]) -> tuple[int, int, int, dict[str, int], dict[str, int]]:
    active_count = len(rows)
    error_count = sum(1 for row in rows if str(row.get("severity") or "").strip().lower() == "error")
    warn_count = sum(1 for row in rows if str(row.get("severity") or "").strip().lower() == "warn")
    counts_by_type: dict[str, int] = {}
    counts_by_module: dict[str, int] = {}
    for row in rows:
        fallback_type = str(row.get("fallback_type") or "unknown")
        module = str(row.get("module") or "unknown")
        counts_by_type[fallback_type] = counts_by_type.get(fallback_type, 0) + 1
        counts_by_module[module] = counts_by_module.get(module, 0) + 1
    return active_count, error_count, warn_count, counts_by_type, counts_by_module


def check_fallback_telemetry_compact(*, hours: int = 24, limit: int = 25) -> dict[str, Any]:
    """Fast Health fallback telemetry from local spools only.

    The DB-backed fallback table can be large and grouped scans are not suitable
    for frequent Health polling on a small Postgres instance. This compact path
    keeps urgent local fallback and DB-retry telemetry visible while pointing
    operators to the deep command for the full persisted table summary.
    """
    window_hours = max(1, int(hours))
    row_limit = max(1, int(limit))
    errors: list[str] = []
    try:
        db_retry_rows = read_db_retry_telemetry_events(hours=window_hours, limit=row_limit)
    except Exception as exc:
        _record_health_local_fallback(
            source="utils.db",
            fallback_type="operator_health_db_retry_spool_summary_failed",
            reason="Operator Health could not read DB retry telemetry spool.",
            error=exc,
            metadata={"hours": window_hours, "limit": row_limit},
        )
        db_retry_rows = []
        errors.append(f"{type(exc).__name__}: {exc}")
    try:
        local_rows = read_local_fallback_events(hours=window_hours, limit=row_limit)
    except Exception as exc:
        _record_health_local_fallback(
            source="local_fallback_telemetry",
            fallback_type="operator_health_local_fallback_spool_summary_failed",
            reason="Operator Health could not read local fallback telemetry spool.",
            error=exc,
            metadata={"hours": window_hours, "limit": row_limit},
        )
        local_rows = []
        errors.append(f"{type(exc).__name__}: {exc}")

    rows = (db_retry_rows + local_rows)[:row_limit]
    active_count, error_count, warn_count, counts_by_type, counts_by_module = _count_fallback_rows(rows)
    nse_session_reset_count = int(counts_by_type.get("nse_session_reset") or 0)
    nse_retry_count = int(counts_by_type.get("nse_retry") or 0)
    status = "error" if errors or error_count else "warn" if active_count else "ok"
    return _status(
        status,
        "Recent local fallback/degraded-path events found."
        if active_count
        else "No recent local fallback/degraded-path events; DB fallback table scan skipped for routine Health.",
        compact_source="local_and_db_retry_spools",
        db_table_scan_skipped=True,
        deep_diagnostic_command="python -m advisory.operator_health --full --skip-dhan --format json",
        window_hours=window_hours,
        active_count=active_count,
        error_count=error_count,
        warn_count=warn_count,
        counts_by_type=counts_by_type,
        counts_by_module=counts_by_module,
        db_retry_count=len(db_retry_rows),
        db_retry_error_count=sum(1 for row in db_retry_rows if str(row.get("severity") or "").strip().lower() == "error"),
        local_fallback_count=len(local_rows),
        local_fallback_error_count=sum(1 for row in local_rows if str(row.get("severity") or "").strip().lower() == "error"),
        nse_session_reset_count=nse_session_reset_count,
        nse_retry_count=nse_retry_count,
        nse_http_count=nse_session_reset_count + nse_retry_count,
        errors=errors,
        rows=rows,
    )


def load_feature_stage_gate_symbols(*, limit: int = FEATURE_STAGE_GATE_SYMBOL_LIMIT) -> list[str]:
    table = "advisory_action_recommendations"
    if not table_exists(table):
        return []
    df = sql_to_df(
        f"""
        SELECT DISTINCT UPPER(TRIM(symbol)) AS symbol
        FROM {table}
        WHERE asof_date = (SELECT MAX(asof_date) FROM {table})
          AND NULLIF(TRIM(symbol), '') IS NOT NULL
        ORDER BY symbol
        LIMIT %s
        """,
        params=(max(1, int(limit)),),
        retries=2,
        statement_timeout_ms=5000,
    )
    return [str(value).strip().upper() for value in df.get("symbol", pd.Series(dtype=object)).dropna().tolist() if str(value).strip()]


def check_feature_stage_gates(*, limit: int = FEATURE_STAGE_GATE_SYMBOL_LIMIT) -> dict[str, Any]:
    try:
        symbols = load_feature_stage_gate_symbols(limit=limit)
    except Exception as exc:
        _record_health_local_fallback(
            source=ACTION_RECOMMENDATIONS_TABLE,
            fallback_type="operator_health_feature_stage_gate_symbols_load_failed",
            reason="Operator Health could not load action symbols for feature stage-gate checks.",
            error=exc,
            metadata={"limit": int(limit)},
        )
        return _status(
            "error",
            "Feature stage-gate health check failed while loading action symbols.",
            error=f"{type(exc).__name__}: {exc}",
            symbol_limit=int(limit),
        )
    stages = [stage for stage in ("rules", "risk", "portfolio", "lifecycle", "actions", "company_memory") if stage in STAGE_FEATURE_DEPENDENCIES_BY_STAGE]
    if not symbols:
        return _status(
            "warn",
            "No recent action symbols available for feature stage-gate health.",
            symbol_limit=int(limit),
            symbols_checked=0,
            rows=[],
            blocked_stage_count=0,
            blocked_symbol_count=0,
            command="./all_advisory.sh",
        )
    rows: list[dict[str, Any]] = []
    for stage in stages:
        dependency = STAGE_FEATURE_DEPENDENCIES_BY_STAGE.get(stage)
        if dependency is None:
            continue
        try:
            gate = evaluate_stage_feature_gate(stage, symbols, asof_date=None)
        except Exception as exc:
            _record_health_local_fallback(
                source="advisory.feature_freshness",
                fallback_type="operator_health_feature_stage_gate_eval_failed",
                reason="Operator Health could not evaluate one feature stage gate.",
                error=exc,
                severity="warn",
                metadata={"stage": stage, "symbol_count": len(symbols)},
            )
            rows.append(
                {
                    "stage": stage,
                    "status": "error",
                    "symbols_checked": len(symbols),
                    "blocked_count": len(symbols),
                    "blocked_symbols": symbols[:10],
                    "required_input_keys": list(dependency.input_keys),
                    "context_input_keys": list(dependency.context_input_keys),
                    "context_input_status_counts": {},
                    "context_warning_count": 0,
                    "gate_effect": dependency.gate_effect,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        blocked_symbols = [str(value).upper() for value in gate.get("blocked_symbols") or []]
        context_counts: dict[str, int] = {}
        for symbol_summary in (gate.get("symbols") or {}).values():
            if not isinstance(symbol_summary, dict):
                continue
            for context_input in symbol_summary.get("context_inputs") or []:
                if not isinstance(context_input, dict):
                    continue
                status = str(context_input.get("status") or "unknown").strip().lower() or "unknown"
                context_counts[status] = context_counts.get(status, 0) + 1
        context_warning_count = sum(
            count
            for status, count in context_counts.items()
            if status in {"missing", "stale", "error", "intentionally_skipped"}
        )
        rows.append(
            {
                "stage": stage,
                "status": gate.get("status"),
                "symbols_checked": gate.get("symbols_checked"),
                "blocked_count": gate.get("blocked_count", len(blocked_symbols)),
                "blocked_symbols": blocked_symbols[:10],
                "required_input_keys": gate.get("required_input_keys"),
                "context_input_keys": gate.get("context_input_keys"),
                "context_input_status_counts": context_counts,
                "context_warning_count": context_warning_count,
                "gate_effect": gate.get("gate_effect"),
            }
        )
    blocked_rows = [row for row in rows if row.get("status") in {"blocked", "error"}]
    blocked_symbols = sorted({symbol for row in blocked_rows for symbol in row.get("blocked_symbols") or []})
    status = "warn" if blocked_rows else "ok"
    return _status(
        status,
        "Feature stage gates are clear." if status == "ok" else "One or more advisory stages have blocked required feature inputs.",
        symbol_limit=int(limit),
        symbols_checked=len(symbols),
        sampled_symbols=symbols[:10],
        blocked_stage_count=len(blocked_rows),
        blocked_symbol_count=len(blocked_symbols),
        rows=rows,
        command="./complete_data.sh && ./all_advisory.sh",
    )


def check_feature_stage_gates_snapshot(*, limit: int = OPERATOR_HEALTH_FEATURE_STAGE_GATE_SYMBOL_LIMIT) -> dict[str, Any]:
    """Cheap Health summary from persisted decision-time freshness snapshots.

    The deep `check_feature_stage_gates` path re-evaluates feature inputs against
    source tables and can be expensive on small Postgres instances. Full Health
    uses this snapshot path so the UI surfaces known blockers without scanning
    OHLCV/technical tables on every health poll.
    """
    row_limit = max(1, int(limit))
    try:
        if not table_exists(ACTION_RECOMMENDATIONS_TABLE):
            return _status(
                "warn",
                "No action recommendations table exists yet; feature stage-gate snapshots are unavailable.",
                degraded=True,
                compact_source="decision_time_snapshot",
                live_recomputed=False,
                symbol_limit=row_limit,
                symbols_checked=0,
                blocked_stage_count=0,
                blocked_symbol_count=0,
                rows=[],
                command="./all_advisory.sh",
            )
        columns = table_columns(ACTION_RECOMMENDATIONS_TABLE)
        if "feature_freshness_json" not in columns:
            return _status(
                "warn",
                "Action recommendations do not contain persisted feature-freshness snapshots.",
                degraded=True,
                compact_source="decision_time_snapshot",
                live_recomputed=False,
                symbol_limit=row_limit,
                symbols_checked=0,
                blocked_stage_count=0,
                blocked_symbol_count=0,
                rows=[],
                command="./all_advisory.sh",
            )
        df = sql_to_df(
            f"""
            SELECT
                asof_date,
                symbol,
                action_code,
                feature_freshness_json
            FROM {ACTION_RECOMMENDATIONS_TABLE}
            WHERE asof_date = (SELECT MAX(asof_date) FROM {ACTION_RECOMMENDATIONS_TABLE})
              AND NULLIF(TRIM(symbol), '') IS NOT NULL
            ORDER BY asof_date DESC NULLS LAST, symbol
            LIMIT %s
            """,
            params=(row_limit,),
            retries=2,
            statement_timeout_ms=5000,
        )
    except Exception as exc:
        _record_health_local_fallback(
            source=ACTION_RECOMMENDATIONS_TABLE,
            fallback_type="operator_health_feature_stage_gate_snapshot_load_failed",
            reason="Operator Health could not load persisted feature stage-gate snapshots.",
            error=exc,
            metadata={"limit": row_limit},
        )
        return _status(
            "error",
            "Could not load persisted feature stage-gate snapshots.",
            error=f"{type(exc).__name__}: {exc}",
            compact_source="decision_time_snapshot",
            live_recomputed=False,
            symbol_limit=row_limit,
            blocked_stage_count=None,
            blocked_symbol_count=None,
            rows=[],
            command="./all_advisory.sh",
        )

    if df.empty:
        return _status(
            "warn",
            "No latest action rows are available for feature stage-gate snapshots.",
            compact_source="decision_time_snapshot",
            live_recomputed=False,
            symbol_limit=row_limit,
            symbols_checked=0,
            blocked_stage_count=0,
            blocked_symbol_count=0,
            rows=[],
            command="./all_advisory.sh",
        )

    rows: list[dict[str, Any]] = []
    blocked_symbols: set[str] = set()
    malformed_count = 0
    for row in _records(df):
        symbol = str(row.get("symbol") or "").strip().upper()
        contract = _json_dict(
            row.get("feature_freshness_json"),
            source=ACTION_RECOMMENDATIONS_TABLE,
            fallback_type="operator_health_feature_stage_gate_snapshot_json_parse_failed",
            metadata={"symbol": symbol, "asof_date": str(row.get("asof_date"))},
        )
        if not contract:
            malformed_count += 1
        status = str(contract.get("status") or "unknown").strip().lower()
        blockers = contract.get("blockers") if isinstance(contract.get("blockers"), list) else []
        required_inputs = contract.get("required_inputs") if isinstance(contract.get("required_inputs"), list) else []
        if status == "blocked" or blockers:
            blocked_symbols.add(symbol)
        rows.append(
            {
                "stage": "actions",
                "status": "blocked" if status == "blocked" or blockers else "ok" if status in {"ok", "fresh"} else status,
                "symbol": symbol,
                "action_code": row.get("action_code"),
                "asof_date": row.get("asof_date"),
                "blocked_count": 1 if status == "blocked" or blockers else 0,
                "blocked_symbols": [symbol] if status == "blocked" or blockers else [],
                "required_input_keys": [
                    str(item.get("input_key"))
                    for item in required_inputs
                    if isinstance(item, dict) and item.get("input_key") is not None
                ],
                "blockers": blockers[:5],
                "captured_at": contract.get("captured_at"),
                "gate_effect": "Persisted decision-time feature freshness snapshot; run direct feature freshness diagnostics for live source-table detail.",
            }
        )

    blocked_count = len(blocked_symbols)
    status = "warn" if blocked_count or malformed_count else "ok"
    message = "Persisted feature-freshness snapshots are clear."
    if blocked_count:
        message = "Persisted feature-freshness snapshots contain blocked action inputs."
    elif malformed_count:
        message = "Some persisted feature-freshness snapshots were missing or malformed."
    return _status(
        status,
        message,
        compact_source="decision_time_snapshot",
        live_recomputed=False,
        degraded=bool(malformed_count),
        symbol_limit=row_limit,
        symbols_checked=int(len(df)),
        blocked_stage_count=1 if blocked_count else 0,
        blocked_symbol_count=blocked_count,
        malformed_snapshot_count=malformed_count,
        rows=rows,
        command="./complete_data.sh && ./all_advisory.sh",
    )


def check_feature_stage_gates_bounded(
    *,
    limit: int = FEATURE_STAGE_GATE_SYMBOL_LIMIT,
    timeout_seconds: float = OPERATOR_HEALTH_FEATURE_STAGE_GATES_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    timeout = max(1.0, float(timeout_seconds))
    command = [
        sys.executable,
        "-c",
        (
            "import json, sys; "
            "from advisory.operator_health import check_feature_stage_gates; "
            "print(json.dumps(check_feature_stage_gates(limit=int(sys.argv[1])), default=str))"
        ),
        str(int(limit)),
    ]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        _record_health_local_fallback(
            source="advisory.feature_freshness",
            fallback_type="operator_health_feature_stage_gate_timeout",
            reason="Operator Health feature stage-gate diagnostics exceeded the bounded full-health timeout.",
            error=exc,
            severity="warn",
            metadata={"limit": int(limit), "timeout_seconds": timeout},
        )
        return _status(
            "warn",
            "Feature stage-gate diagnostics timed out and were skipped for this health run.",
            timed_out=True,
            degraded=True,
            symbol_limit=int(limit),
            timeout_seconds=timeout,
            blocked_stage_count=None,
            blocked_symbol_count=None,
            rows=[],
            command=f"FEATURE_STAGE_GATE_SYMBOL_LIMIT={max(1, min(5, int(limit)))} python -m advisory.operator_health --full --skip-dhan",
        )
    if proc.returncode:
        error_text = (proc.stderr or proc.stdout or "").strip()
        error = RuntimeError(error_text[:1000] or f"feature stage-gate subprocess failed with returncode={proc.returncode}")
        _record_health_local_fallback(
            source="advisory.feature_freshness",
            fallback_type="operator_health_feature_stage_gate_subprocess_failed",
            reason="Operator Health feature stage-gate subprocess failed.",
            error=error,
            severity="warn",
            metadata={"limit": int(limit), "timeout_seconds": timeout, "returncode": proc.returncode},
        )
        return _status(
            "error",
            "Feature stage-gate diagnostics failed in subprocess.",
            error=f"{type(error).__name__}: {error}",
            symbol_limit=int(limit),
            blocked_stage_count=None,
            blocked_symbol_count=None,
            rows=[],
            command="./complete_data.sh && ./all_advisory.sh",
        )
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        _record_health_local_fallback(
            source="advisory.feature_freshness",
            fallback_type="operator_health_feature_stage_gate_json_parse_failed",
            reason="Operator Health could not parse feature stage-gate subprocess output.",
            error=exc,
            severity="warn",
            metadata={"limit": int(limit), "stdout_length": len(proc.stdout or "")},
        )
        return _status(
            "error",
            "Feature stage-gate diagnostics returned invalid JSON.",
            error=f"{type(exc).__name__}: {exc}",
            symbol_limit=int(limit),
            blocked_stage_count=None,
            blocked_symbol_count=None,
            rows=[],
            command="./complete_data.sh && ./all_advisory.sh",
        )
    return payload if isinstance(payload, dict) else _status("error", "Feature stage-gate diagnostics returned a non-object payload.", symbol_limit=int(limit), rows=[])


def _is_improving_stop_change(row: dict[str, Any]) -> bool:
    recommended = pd.to_numeric(pd.Series([row.get("recommended_stop_price")]), errors="coerce").iloc[0]
    current = pd.to_numeric(pd.Series([row.get("stop_price")]), errors="coerce").iloc[0]
    if pd.isna(recommended):
        return False
    if pd.isna(current):
        return True
    return float(recommended) > float(current)


def check_lifecycle_policy_change_audit(*, limit: int = 50) -> dict[str, Any]:
    try:
        rebalance_exists = table_exists(REBALANCE_TABLE)
        policy_change_exists = table_exists(LIFECYCLE_POLICY_CHANGES_TABLE)
        if not rebalance_exists or not policy_change_exists:
            return _status(
                "warn",
                "Lifecycle policy-change audit tables are not fully available.",
                rebalance_table_exists=rebalance_exists,
                policy_change_table_exists=policy_change_exists,
                tighten_stop_rows=0,
                auditable_rows=0,
                missing_audit_count=0,
                sample_missing=[],
                command="./all_advisory.sh",
            )
        df = sql_to_df(
            f"""
            WITH tighten AS (
                SELECT
                    published_on,
                    setup_id,
                    UPPER(TRIM(symbol)) AS symbol,
                    unique_id,
                    MAX(load_ts) AS action_load_ts,
                    MAX(recommended_stop_price) AS recommended_stop_price,
                    MAX(stop_price) AS stop_price,
                    MAX(action_reason) AS action_reason
                FROM {REBALANCE_TABLE}
                WHERE LOWER(TRIM(suggested_action)) = 'tighten_stop'
                  AND published_on >= NOW() - INTERVAL '30 days'
                GROUP BY published_on, setup_id, UPPER(TRIM(symbol)), unique_id
                ORDER BY MAX(load_ts) DESC NULLS LAST
                LIMIT %s
            )
            SELECT
                tighten.published_on,
                tighten.setup_id,
                tighten.symbol,
                tighten.unique_id,
                tighten.action_load_ts,
                tighten.recommended_stop_price,
                tighten.stop_price,
                tighten.action_reason,
                policy_changes.change_id,
                policy_changes.changed_at,
                policy_changes.old_value,
                policy_changes.new_value
            FROM tighten
            LEFT JOIN {LIFECYCLE_POLICY_CHANGES_TABLE} AS policy_changes
              ON policy_changes.published_on = tighten.published_on
             AND policy_changes.setup_id = tighten.setup_id
             AND UPPER(TRIM(policy_changes.symbol)) = tighten.symbol
             AND policy_changes.unique_id = tighten.unique_id
             AND policy_changes.change_type = 'stop_tightened'
             AND ABS(policy_changes.new_value - tighten.recommended_stop_price) < 0.0001
            ORDER BY tighten.action_load_ts DESC NULLS LAST
            """,
            params=(max(1, int(limit)),),
            retries=2,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        _record_health_local_fallback(
            source=LIFECYCLE_POLICY_CHANGES_TABLE,
            fallback_type="operator_health_lifecycle_policy_audit_check_failed",
            reason="Operator Health could not verify lifecycle policy-change audit coverage.",
            error=exc,
            metadata={"limit": int(limit)},
        )
        return _status(
            "error",
            "Lifecycle policy-change audit health check failed.",
            error=f"{type(exc).__name__}: {exc}",
            limit=int(limit),
            command="python -m advisory.operator_health --full --skip-dhan",
        )

    rows = _records(df)
    auditable = [row for row in rows if _is_improving_stop_change(row)]
    missing = [row for row in auditable if not row.get("change_id")]
    status = "warn" if missing else "ok"
    return _status(
        status,
        "Lifecycle tighten-stop policy changes have matching audit rows."
        if status == "ok"
        else "Some recent tighten-stop actions are missing lifecycle policy-change audit rows.",
        tighten_stop_rows=len(rows),
        auditable_rows=len(auditable),
        missing_audit_count=len(missing),
        sample_missing=missing[:10],
        command="./all_advisory.sh && python -m advisory.operator_health --skip-dhan",
    )


def summarize_status(sections: dict[str, Any]) -> str:
    statuses: list[str] = []
    for value in sections.values():
        if isinstance(value, dict):
            statuses.append(str(value.get("status") or "ok"))
        elif isinstance(value, list):
            statuses.extend(str(item.get("status") or "ok") for item in value if isinstance(item, dict))
    if "error" in statuses:
        return "error"
    if "warn" in statuses:
        return "warn"
    return "ok"


def build_fix_hints(sections: dict[str, Any]) -> list[dict[str, Any]]:
    hints: list[dict[str, Any]] = []

    def add(
        *,
        status: str,
        title: str,
        reason: str,
        commands: list[str] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        hints.append(
            {
                "status": status,
                "title": title,
                "reason": reason,
                "commands": commands or [],
                "details": details or {},
            }
        )

    database = sections.get("database") if isinstance(sections.get("database"), dict) else {}
    if database.get("status") == "error":
        add(
            status="error",
            title="Postgres is not reachable",
            reason=str(database.get("error") or database.get("message") or "Database health check failed."),
            commands=["python -m advisory.operator_health --skip-dhan", "python -m advisory.api.app"],
            details={"section": "database"},
        )

    operator_api = sections.get("operator_api") if isinstance(sections.get("operator_api"), dict) else {}
    if operator_api.get("status") == "error":
        add(
            status="error",
            title="Operator API is not reachable",
            reason=str(operator_api.get("error") or operator_api.get("message") or "Operator API health check failed."),
            commands=["./all_frontend.sh", "python -m advisory.operator_health --skip-dhan"],
            details={"url": operator_api.get("url"), "latency_ms": operator_api.get("latency_ms"), "status_code": operator_api.get("status_code")},
        )

    operator_api_runtime = sections.get("operator_api_runtime") if isinstance(sections.get("operator_api_runtime"), dict) else {}
    if operator_api_runtime.get("status") in {"warn", "error"}:
        add(
            status=str(operator_api_runtime.get("status") or "error"),
            title="Operator API runtime is stale or unavailable",
            reason=str(operator_api_runtime.get("error") or operator_api_runtime.get("message") or "Operator API runtime metadata check failed."),
            commands=["./all_frontend.sh", "python -m advisory.operator_health --skip-dhan"],
            details={
                "url": operator_api_runtime.get("url"),
                "latency_ms": operator_api_runtime.get("latency_ms"),
                "status_code": operator_api_runtime.get("status_code"),
                "stale_code": operator_api_runtime.get("stale_code"),
                "stale_reason": operator_api_runtime.get("stale_reason"),
                "operator_action": operator_api_runtime.get("operator_action"),
                "process_started_at": operator_api_runtime.get("process_started_at"),
                "latest_source_mtime": operator_api_runtime.get("latest_source_mtime"),
                "latest_source_path": operator_api_runtime.get("latest_source_path"),
            },
        )

    watcher_counters = sections.get("watcher_source_counters") if isinstance(sections.get("watcher_source_counters"), dict) else {}
    if watcher_counters.get("status") in {"warn", "error"}:
        add(
            status=str(watcher_counters.get("status") or "warn"),
            title="Watcher source counters are stale, missing, or failing",
            reason=str(
                watcher_counters.get("message")
                or "Latest watcher runs do not have healthy OHLCV/news/announcement source counters."
            ),
            commands=["./all_watchers.sh", "python -m advisory.operator_health --skip-dhan"],
            details={
                "section": "watcher_source_counters",
                "missing_sources": watcher_counters.get("missing_sources"),
                "stale_count": watcher_counters.get("stale_count"),
                "error_count": watcher_counters.get("error_count"),
                "degraded_counter_count": watcher_counters.get("degraded_counter_count"),
                "empty_counter_count": watcher_counters.get("empty_counter_count"),
            },
        )

    signal_refresh_state = sections.get("signal_refresh_source_state") if isinstance(sections.get("signal_refresh_source_state"), dict) else {}
    if int(signal_refresh_state.get("identity_policy_suppressed_target_rows") or 0) > 0:
        add(
            status="warn",
            title="Context-overlay identity/source mapping is suppressing targets",
            reason=(
                "Some review-only context-overlay watch/de-risk targets are suppressed because company-master, Dhan, or security identity mapping is unresolved. "
                "Repair identity mapping before changing regime/context thresholds."
            ),
            commands=[
                "python -m advisory.operator_health --check identity_issues --skip-dhan",
                "python -m data.dhanlive.scrip_master",
                "python -m advisory.identity_issues --format text",
                "python -m advisory.signal_refresh --from-context-overlays --limit 50 --format text",
                "python -m advisory.operator_health --check signal_refresh_source_state --skip-dhan",
            ],
            details={
                "section": "signal_refresh_source_state",
                "identity_policy_suppressed_target_rows": signal_refresh_state.get("identity_policy_suppressed_target_rows"),
                "identity_policy_suppressed_sources": signal_refresh_state.get("identity_policy_suppressed_sources"),
            },
        )

    if signal_refresh_state.get("status") in {"warn", "error"}:
        unhealthy_signal_sources = {
            str(source)
            for key in (
                "missing_sources",
                "stale_sources",
                "error_sources",
                "unsafe_authority_sources",
                "source_contract_issue_sources",
                "algorithm_version_mismatch_sources",
            )
            for source in (signal_refresh_state.get(key) if isinstance(signal_refresh_state.get(key), list) else [])
            if str(source or "").strip()
        }
        signal_refresh_commands: list[str] = []
        if "advisory:signal_refresh:context_overlays" in unhealthy_signal_sources:
            signal_refresh_commands.append("python -m advisory.signal_refresh --from-context-overlays --limit 50 --format text")
        if "advisory:signal_refresh:causal_memory" in unhealthy_signal_sources:
            signal_refresh_commands.append("python -m advisory.signal_refresh --from-causal-memory --limit 25 --format text")
        if "continuous_watch:action_refresh" in unhealthy_signal_sources:
            signal_refresh_commands.append("python -m advisory.recommendation_diagnostics --format text")
        signal_refresh_commands.extend(["./all_watchers.sh", "python -m advisory.operator_health --full --skip-dhan"])
        signal_refresh_commands = list(dict.fromkeys(signal_refresh_commands))
        add(
            status=str(signal_refresh_state.get("status") or "warn"),
            title="Review-only signal-refresh source state is stale or incomplete",
            reason=str(
                signal_refresh_state.get("message")
                or "Latest context-overlay, causal-memory, or bounded action-refresh sync-state rows are stale, missing, or failing."
            ),
            commands=signal_refresh_commands,
            details={
                "section": "signal_refresh_source_state",
                "missing_sources": signal_refresh_state.get("missing_sources"),
                "stale_sources": signal_refresh_state.get("stale_sources"),
                "error_sources": signal_refresh_state.get("error_sources"),
                "unsafe_authority_sources": signal_refresh_state.get("unsafe_authority_sources"),
                "algorithm_version_mismatch_sources": signal_refresh_state.get("algorithm_version_mismatch_sources"),
                "source_contract_issue_sources": signal_refresh_state.get("source_contract_issue_sources"),
                "stale_count": signal_refresh_state.get("stale_count"),
                "error_count": signal_refresh_state.get("error_count"),
                "unsafe_authority_count": signal_refresh_state.get("unsafe_authority_count"),
                "algorithm_version_mismatch_count": signal_refresh_state.get("algorithm_version_mismatch_count"),
                "malformed_source_contract_count": signal_refresh_state.get("malformed_source_contract_count"),
                "source_contract_audit": signal_refresh_state.get("source_contract_audit"),
                "signal_row_total": signal_refresh_state.get("signal_row_total"),
                "identity_policy_suppressed_target_rows": signal_refresh_state.get("identity_policy_suppressed_target_rows"),
                "identity_policy_suppressed_sources": signal_refresh_state.get("identity_policy_suppressed_sources"),
                "broker_execution_allowed": bool(signal_refresh_state.get("broker_execution_allowed")),
                "portfolio_authority": signal_refresh_state.get("portfolio_authority") or "none",
                "full_advisory_required": signal_refresh_state.get("full_advisory_required"),
            },
        )

    trace_summaries = sections.get("trace_summaries") if isinstance(sections.get("trace_summaries"), dict) else {}
    if trace_summaries.get("status") in {"warn", "error"}:
        add(
            status=str(trace_summaries.get("status") or "warn"),
            title="Trace summary cache is stale or unavailable",
            reason=str(trace_summaries.get("error") or trace_summaries.get("message") or "Trace summary materialization did not pass."),
            commands=["python -m advisory.trace_summary_store --symbol-limit 100 --event-limit 100", "python -m advisory.operator_health --skip-dhan"],
            details={"row_count": trace_summaries.get("row_count"), "latest_generated_at": trace_summaries.get("latest_generated_at"), "age_hours": trace_summaries.get("age_hours")},
        )

    snapshot = sections.get("operator_snapshot") if isinstance(sections.get("operator_snapshot"), dict) else {}
    if snapshot.get("status") in {"warn", "error"}:
        add(
            status=str(snapshot.get("status") or "warn"),
            title="Operator snapshot is stale or unavailable",
            reason=str(snapshot.get("error") or snapshot.get("message") or "Snapshot health check did not pass."),
            commands=["python -m advisory.operator_snapshot", "./all_frontend.sh", "python -m advisory.operator_health --skip-dhan"],
            details={"generated_at": snapshot.get("generated_at"), "age_seconds": snapshot.get("age_seconds"), "max_age_seconds": snapshot.get("max_age_seconds")},
        )

    slow = sections.get("slow_operations") if isinstance(sections.get("slow_operations"), dict) else {}
    if slow.get("status") in {"warn", "error"}:
        add(
            status=str(slow.get("status") or "warn"),
            title="Open slow-operation issues exist",
            reason=str(slow.get("message") or f"{slow.get('returned_count', 0)} open slow-operation issue(s) returned from {slow.get('state_file')}."),
            commands=["python -m advisory.performance_slowlog report --limit 20", str(slow.get("performance_report_command") or "python scripts/api_performance_report.py --limit 20")],
            details={
                "issue_count": slow.get("issue_count"),
                "returned_count": slow.get("returned_count"),
                "active_issue_count": slow.get("active_issue_count"),
                "historical_issue_count": slow.get("historical_issue_count"),
                "probe_status": slow.get("probe_status"),
                "probe_generated_at": slow.get("probe_generated_at"),
                "state_file": slow.get("state_file"),
            },
        )

    api_latency = sections.get("api_latency_probe") if isinstance(sections.get("api_latency_probe"), dict) else {}
    if api_latency.get("status") in {"warn", "error"}:
        add(
            status=str(api_latency.get("status") or "warn"),
            title="Operator API latency probe is stale or unhealthy",
            reason=str(api_latency.get("message") or "Latest API latency probe is stale, slow, or failed."),
            commands=[
                str(api_latency.get("command") or "python scripts/api_latency_probe.py"),
                str(api_latency.get("performance_report_command") or "python scripts/api_performance_report.py --limit 20"),
                "python -m advisory.performance_slowlog report --limit 20",
            ],
            details={
                "path": api_latency.get("path"),
                "age_seconds": api_latency.get("age_seconds"),
                "max_age_seconds": api_latency.get("max_age_seconds"),
                "slow_count": api_latency.get("slow_count"),
                "error_count": api_latency.get("error_count"),
                "operator_action": api_latency.get("operator_action"),
            },
        )

    advisory_stage_report = sections.get("advisory_stage_report") if isinstance(sections.get("advisory_stage_report"), dict) else {}
    if advisory_stage_report.get("status") in {"warn", "error"}:
        add(
            status=str(advisory_stage_report.get("status") or "warn"),
            title="Advisory stage timing report needs attention",
            reason=str(advisory_stage_report.get("error") or advisory_stage_report.get("message") or advisory_stage_report.get("operator_action") or "Advisory stage timing report is missing or has over-budget stages."),
            commands=[
                str(advisory_stage_report.get("command") or "python scripts/advisory_stage_report.py --log-path logs/cron/all_advisory.log --limit 20"),
                "Run the Operations command: advisory_stage_report",
            ],
            details={
                "section": "advisory_stage_report",
                "log_path": advisory_stage_report.get("log_path"),
                "stage_count": advisory_stage_report.get("stage_count"),
                "slow_stage_count": advisory_stage_report.get("slow_stage_count"),
                "degraded_stage_count": advisory_stage_report.get("degraded_stage_count"),
                "operations_command": advisory_stage_report.get("operations_command"),
                "operator_action": advisory_stage_report.get("operator_action"),
                "ranked_stages": advisory_stage_report.get("ranked_stages"),
                "stage_degradations": advisory_stage_report.get("stage_degradations"),
            },
        )

    event_quality = sections.get("event_data_quality") if isinstance(sections.get("event_data_quality"), dict) else {}
    if event_quality.get("status") in {"warn", "error"}:
        summary = event_quality.get("summary") if isinstance(event_quality.get("summary"), dict) else {}
        add(
            status=str(event_quality.get("status") or "warn"),
            title="Announcement and bhavcopy evidence need attention",
            reason=str(event_quality.get("message") or "Event evidence quality gate found stale, missing, or incomplete inputs."),
            commands=["python -m advisory.event_data_quality --format json", "./complete_data.sh", "./all_advisory.sh"],
            details={
                "section": "event_data_quality",
                "issue_count": summary.get("issue_count"),
                "error_count": summary.get("error_count"),
                "warn_count": summary.get("warn_count"),
                "llm_signal_authority": summary.get("llm_signal_authority"),
            },
        )

    identity = sections.get("identity_issues") if isinstance(sections.get("identity_issues"), dict) else {}
    if identity.get("status") in {"warn", "error"}:
        add(
            status=str(identity.get("status") or "warn"),
            title="Open Dhan/security identity issues exist",
            reason=str(identity.get("error") or identity.get("message") or "Some active symbols do not resolve cleanly to broker/security identity."),
            commands=[
                "python -m data.dhanlive.scrip_master",
                "python -m advisory.identity_issues --limit 100",
                "python -m advisory.identity_issues --apply --limit 100",
                "python -m advisory.operator_health --skip-dhan",
            ],
            details={
                "section": "identity_issues",
                "open_count": identity.get("open_count"),
                "active_action_identity_coverage": identity.get("active_action_identity_coverage"),
            },
        )

    screener_failures = sections.get("screener_failures") if isinstance(sections.get("screener_failures"), dict) else {}
    if screener_failures.get("status") in {"warn", "error"}:
        add(
            status=str(screener_failures.get("status") or "warn"),
            title="Recent Screener.in query/fetch/parse failures exist",
            reason=str(screener_failures.get("error") or screener_failures.get("message") or "Screener.in failures were recorded recently."),
            commands=[
                "python -m data.screenerin.auth --check",
                "python -m data.screenerin.screener_registry query --name 'Test query' --query 'Current price > DMA 50'",
                "python -m advisory.operator_health --full --skip-dhan",
            ],
            details={
                "section": "screener_failures",
                "active_count": screener_failures.get("active_count"),
                "validation_count": screener_failures.get("validation_count"),
                "fetch_count": screener_failures.get("fetch_count"),
                "parse_count": screener_failures.get("parse_count"),
                "counts_by_stage": screener_failures.get("counts_by_stage"),
            },
        )

    context_gate_policy = sections.get("context_gate_policy") if isinstance(sections.get("context_gate_policy"), dict) else {}
    if context_gate_policy.get("status") in {"warn", "error"}:
        add(
            status=str(context_gate_policy.get("status") or "warn"),
            title="Broad regime hard gate is active",
            reason=str(
                context_gate_policy.get("error")
                or context_gate_policy.get("operator_action")
                or context_gate_policy.get("message")
                or "A broad single-regime hard gate is active."
            ),
            commands=[
                str(context_gate_policy.get("command") or "python scripts/context_gate_policy_audit.py --fail-on-single-regime"),
                "python -m advisory.recommendation_diagnostics --format text",
                "python -m advisory.operator_health --skip-dhan",
            ],
            details={
                "section": "context_gate_policy",
                "reasons": context_gate_policy.get("reasons"),
                "active_single_regime_flags": context_gate_policy.get("active_single_regime_flags"),
                "active_context_hard_flags": context_gate_policy.get("active_context_hard_flags"),
                "global_regime_label_blocks_buy": bool(context_gate_policy.get("global_regime_label_blocks_buy")),
                "policy_summary": context_gate_policy.get("policy_summary"),
                "broker_execution_allowed": bool(context_gate_policy.get("broker_execution_allowed")),
                "portfolio_authority": context_gate_policy.get("portfolio_authority") or "none",
            },
        )

    signal_quality = sections.get("signal_quality") if isinstance(sections.get("signal_quality"), dict) else {}
    if signal_quality.get("status") in {"warn", "error"}:
        add(
            status=str(signal_quality.get("status") or "warn"),
            title="Signal-quality evidence is not usable for promotion",
            reason=str(signal_quality.get("error") or signal_quality.get("message") or "Signal-quality evaluator needs a fresher or better-covered run."),
            commands=[
                str(signal_quality.get("command") or "python -m advisory.signal_quality_evaluator --from-date YYYY-MM-DD --to-date YYYY-MM-DD --horizons 5 10 20"),
                str(signal_quality.get("window_runner_command") or "python -m advisory.signal_quality_window_runner --horizons 5 10 20 --include-split-reports"),
            ],
            details={
                "section": "signal_quality",
                "latest_evaluated_at": signal_quality.get("latest_evaluated_at"),
                "reasons": signal_quality.get("reasons"),
                "max_matured_rows": signal_quality.get("max_matured_rows"),
                "overlay_rows": signal_quality.get("overlay_rows"),
                "benchmark_beta_not_overlay_alpha_count": signal_quality.get("benchmark_beta_not_overlay_alpha_count"),
                "needs_benchmark_attribution_count": signal_quality.get("needs_benchmark_attribution_count"),
                "benchmark_or_attribution_blocked_count": signal_quality.get("benchmark_or_attribution_blocked_count"),
                "promotion_readiness_status": signal_quality.get("promotion_readiness_status"),
                "candidate_helpful_family_count": signal_quality.get("candidate_helpful_family_count"),
                "benchmark_or_attribution_blocked_family_count": signal_quality.get("benchmark_or_attribution_blocked_family_count"),
                "promotion_review_sector_block_count": signal_quality.get("promotion_review_sector_block_count"),
                "promotion_review_runtime_block_count": signal_quality.get("promotion_review_runtime_block_count"),
                "promotion_review_harmful_class_block_count": signal_quality.get("promotion_review_harmful_class_block_count"),
                "promotion_reviews": signal_quality.get("promotion_reviews"),
            },
        )

    technical_threshold_evidence = sections.get("technical_threshold_evidence") if isinstance(sections.get("technical_threshold_evidence"), dict) else {}
    if technical_threshold_evidence.get("status") in {"warn", "error"}:
        add(
            status=str(technical_threshold_evidence.get("status") or "warn"),
            title="Technical-threshold calibration evidence is not policy-ready",
            reason=str(
                technical_threshold_evidence.get("error")
                or technical_threshold_evidence.get("message")
                or "Technical threshold calibration needs fresh, matured, after-cost evidence with lift over baseline."
            ),
            commands=[
                str(technical_threshold_evidence.get("command") or "python -m advisory.technical_threshold_calibration --horizons 5 10 20"),
                str(technical_threshold_evidence.get("promotion_review_command") or "python -m advisory.technical_threshold_promotion --setup-id EVENT_OPPORTUNITY_V1 --config-id <config_id> --dry-run"),
                "python -m advisory.operator_health --full --skip-dhan",
            ],
            details={
                "section": "technical_threshold_evidence",
                "reasons": technical_threshold_evidence.get("reasons"),
                "evaluation_rows": technical_threshold_evidence.get("evaluation_rows"),
                "summary_rows": technical_threshold_evidence.get("summary_rows"),
                "max_matured_signal_count": technical_threshold_evidence.get("max_matured_signal_count"),
                "review_candidate_count": technical_threshold_evidence.get("review_candidate_count"),
                "lift_over_baseline_count": technical_threshold_evidence.get("lift_over_baseline_count"),
                "positive_after_cost_count": technical_threshold_evidence.get("positive_after_cost_count"),
                "bounded_first_pass_count": technical_threshold_evidence.get("bounded_first_pass_count"),
                "trigger_near_miss_candidate_count": technical_threshold_evidence.get("trigger_near_miss_candidate_count"),
                "trigger_near_miss_do_not_relax_count": technical_threshold_evidence.get("trigger_near_miss_do_not_relax_count"),
                "trigger_near_miss_needs_more_label_count": technical_threshold_evidence.get("trigger_near_miss_needs_more_label_count"),
                "trigger_near_miss_research": technical_threshold_evidence.get("trigger_near_miss_research"),
                "missing_summary_columns": technical_threshold_evidence.get("missing_summary_columns"),
                "latest_evaluated_at": technical_threshold_evidence.get("latest_evaluated_at"),
                "age_days": technical_threshold_evidence.get("age_days"),
                "broker_execution_allowed": bool(technical_threshold_evidence.get("broker_execution_allowed")),
                "policy_auto_promotion_allowed": bool(technical_threshold_evidence.get("policy_auto_promotion_allowed")),
            },
        )

    research_evidence_summary = sections.get("research_evidence_run_summary") if isinstance(sections.get("research_evidence_run_summary"), dict) else {}
    if research_evidence_summary.get("status") in {"warn", "error"}:
        add(
            status=str(research_evidence_summary.get("status") or "warn"),
            title="Research evidence refresh is missing, failed, or not ready",
            reason=str(
                research_evidence_summary.get("error")
                or research_evidence_summary.get("message")
                or "The latest daily research-evidence wrapper did not produce a usable readiness summary."
            ),
            commands=[
                str(research_evidence_summary.get("command") or "./all_research_evidence.sh"),
                str(research_evidence_summary.get("health_command") or "python -m advisory.operator_health --full --skip-dhan"),
            ],
            details={
                "section": "research_evidence_run_summary",
                "reasons": research_evidence_summary.get("reasons"),
                "log_path": research_evidence_summary.get("log_path"),
                "readiness_status": research_evidence_summary.get("readiness_status"),
                "payload_status": research_evidence_summary.get("payload_status"),
                "active_component_count": research_evidence_summary.get("active_component_count"),
                "candidate_component_count": research_evidence_summary.get("candidate_component_count"),
                "blocker_count": research_evidence_summary.get("blocker_count"),
                "blocker_components": research_evidence_summary.get("blocker_components"),
                "authority_violation_count": research_evidence_summary.get("authority_violation_count"),
                "authority_violation_components": research_evidence_summary.get("authority_violation_components"),
                "broker_execution_allowed": bool(research_evidence_summary.get("broker_execution_allowed")),
                "policy_auto_promotion_allowed": bool(research_evidence_summary.get("policy_auto_promotion_allowed")),
                "portfolio_mutation_allowed": bool(research_evidence_summary.get("portfolio_mutation_allowed")),
            },
        )

    ts_forecast_paper = sections.get("ts_forecast_paper") if isinstance(sections.get("ts_forecast_paper"), dict) else {}
    if ts_forecast_paper.get("status") in {"warn", "error"}:
        add(
            status=str(ts_forecast_paper.get("status") or "warn"),
            title="TS forecast paper-portfolio evidence is missing or not matured",
            reason=str(
                ts_forecast_paper.get("error")
                or ts_forecast_paper.get("message")
                or "TS forecast promotion checks need research-only paper-portfolio evidence before they can be reviewed."
            ),
            commands=[
                str(ts_forecast_paper.get("command") or "python -m advisory.ts_forecast_paper_portfolio --log-research-ledger"),
                str(ts_forecast_paper.get("promotion_check_command") or "python -m advisory.ts_forecast_promotion_check --format json"),
                "python -m advisory.operator_health --full --skip-dhan",
            ],
            details={
                "section": "ts_forecast_paper",
                "table": ts_forecast_paper.get("table"),
                "reasons": ts_forecast_paper.get("reasons"),
                "row_count": ts_forecast_paper.get("row_count"),
                "matured_count": ts_forecast_paper.get("matured_count"),
                "paper_buy_count": ts_forecast_paper.get("paper_buy_count"),
                "symbol_count": ts_forecast_paper.get("symbol_count"),
                "latest_load_ts": ts_forecast_paper.get("latest_load_ts"),
                "latest_asof_date": ts_forecast_paper.get("latest_asof_date"),
                "promotion_check_endpoint": ts_forecast_paper.get("promotion_check_endpoint"),
                "broker_execution_allowed": bool(ts_forecast_paper.get("broker_execution_allowed")),
                "policy_auto_promotion_allowed": bool(ts_forecast_paper.get("policy_auto_promotion_allowed")),
            },
        )

    llm_provenance = sections.get("llm_provenance_audit") if isinstance(sections.get("llm_provenance_audit"), dict) else {}
    if llm_provenance.get("status") in {"warn", "error"}:
        add(
            status=str(llm_provenance.get("status") or "warn"),
            title="LLM provenance audit has open issues",
            reason=str(
                llm_provenance.get("error")
                or llm_provenance.get("message")
                or "Persisted LLM/Codex-derived rows are missing prompt/schema/evidence/authority metadata."
            ),
            commands=[
                str(llm_provenance.get("command") or "python -m advisory.llm_provenance_audit --lookback-days 30 --limit-per-table 100 --format text"),
                "python -m advisory.operator_health --full --skip-dhan",
            ],
            details={
                "section": "llm_provenance_audit",
                "reasons": llm_provenance.get("reasons"),
                "issue_table_count": llm_provenance.get("issue_table_count"),
                "table_count": llm_provenance.get("table_count"),
                "lookback_days": llm_provenance.get("lookback_days"),
                "limit_per_table": llm_provenance.get("limit_per_table"),
                "issue_rows": llm_provenance.get("issue_rows"),
                "broker_execution_allowed": bool(llm_provenance.get("broker_execution_allowed")),
                "policy_auto_promotion_allowed": bool(llm_provenance.get("policy_auto_promotion_allowed")),
                "repairs_metadata": bool(llm_provenance.get("repairs_metadata")),
            },
        )

    provenance_graphs = sections.get("provenance_graph_dry_runs") if isinstance(sections.get("provenance_graph_dry_runs"), dict) else {}
    if provenance_graphs.get("status") in {"warn", "error"}:
        add(
            status=str(provenance_graphs.get("status") or "warn"),
            title="Provenance graph dry runs are missing lineage",
            reason=str(
                provenance_graphs.get("error")
                or provenance_graphs.get("message")
                or "Action-evidence or causal-event provenance dry runs did not produce auditable lineage rows."
            ),
            commands=[
                "python -m advisory.action_evidence_provenance --dry-run --limit 250 --format text",
                "python -m advisory.causal_event_provenance --dry-run --limit 250 --format text",
                "python -m advisory.operator_health --full --skip-dhan",
            ],
            details={
                "section": "provenance_graph_dry_runs",
                "reasons": provenance_graphs.get("reasons"),
                "row_count": provenance_graphs.get("row_count"),
                "error_count": provenance_graphs.get("error_count"),
                "warn_count": provenance_graphs.get("warn_count"),
                "rows": provenance_graphs.get("rows"),
                "broker_execution_allowed": bool(provenance_graphs.get("broker_execution_allowed")),
                "policy_auto_promotion_allowed": bool(provenance_graphs.get("policy_auto_promotion_allowed")),
                "persists_rows": bool(provenance_graphs.get("persists_rows")),
            },
        )

    causal_memory = sections.get("causal_event_memory_evidence") if isinstance(sections.get("causal_event_memory_evidence"), dict) else {}
    if causal_memory.get("status") in {"warn", "error"}:
        add(
            status=str(causal_memory.get("status") or "warn"),
            title="Causal event-memory evidence is stale or unavailable",
            reason=str(
                causal_memory.get("error")
                or causal_memory.get("message")
                or "Persisted causal event-memory labels are missing, stale, or not mature enough for offline review."
            ),
            commands=[
                str(causal_memory.get("command") or "python -m advisory.causal_event_memory_evaluator --horizons 5 10 20 --format json"),
                str(
                    causal_memory.get("config_preview_command")
                    or "python -m advisory.causal_event_memory_evaluator --horizons 5 10 20 --generate-config-previews --include-suppression-config-previews --dry-run --format text"
                ),
                "python -m advisory.operator_health --full --skip-dhan",
            ],
            details={
                "section": "causal_event_memory_evidence",
                "reasons": causal_memory.get("reasons"),
                "evaluation_rows": causal_memory.get("evaluation_rows"),
                "matured_rows": causal_memory.get("matured_rows"),
                "summary_rows": causal_memory.get("summary_rows"),
                "candidate_group_count": causal_memory.get("candidate_group_count"),
                "harmful_group_count": causal_memory.get("harmful_group_count"),
                "latest_evaluated_at": causal_memory.get("latest_evaluated_at"),
                "age_days": causal_memory.get("age_days"),
                "broker_execution_allowed": bool(causal_memory.get("broker_execution_allowed")),
                "policy_auto_promotion_allowed": bool(causal_memory.get("policy_auto_promotion_allowed")),
            },
        )

    action_transition = sections.get("action_transition_evidence") if isinstance(sections.get("action_transition_evidence"), dict) else {}
    if action_transition.get("status") in {"warn", "error"}:
        add(
            status=str(action_transition.get("status") or "warn"),
            title="Action-transition evidence is not policy-ready",
            reason=str(
                action_transition.get("error")
                or action_transition.get("message")
                or "Persisted action-transition labels are missing, stale, benchmark-beta-only, or not mature enough for offline review."
            ),
            commands=[
                *(
                    [str(action_transition.get("schema_command"))]
                    if action_transition.get("schema_command")
                    and "missing_action_transition_evidence_tables" in (action_transition.get("reasons") or [])
                    else []
                ),
                str(action_transition.get("command") or "python -m advisory.action_transition_evaluator --horizons 5 10 20 --format json"),
                "python -m advisory.operator_health --full --skip-dhan",
            ],
            details={
                "section": "action_transition_evidence",
                "reasons": action_transition.get("reasons"),
                "schema_command": action_transition.get("schema_command"),
                "evaluation_rows": action_transition.get("evaluation_rows"),
                "matured_rows": action_transition.get("matured_rows"),
                "summary_rows": action_transition.get("summary_rows"),
                "candidate_group_count": action_transition.get("candidate_group_count"),
                "benchmark_beta_not_transition_alpha_count": action_transition.get("benchmark_beta_not_transition_alpha_count"),
                "needs_benchmark_attribution_count": action_transition.get("needs_benchmark_attribution_count"),
                "latest_evaluated_at": action_transition.get("latest_evaluated_at"),
                "age_days": action_transition.get("age_days"),
                "broker_execution_allowed": bool(action_transition.get("broker_execution_allowed")),
                "policy_auto_promotion_allowed": bool(action_transition.get("policy_auto_promotion_allowed")),
            },
        )

    event_policy_evidence = sections.get("event_policy_evidence") if isinstance(sections.get("event_policy_evidence"), dict) else {}
    if event_policy_evidence.get("status") in {"warn", "error"}:
        add(
            status=str(event_policy_evidence.get("status") or "warn"),
            title="Event-policy evidence is not policy-ready",
            reason=str(
                event_policy_evidence.get("error")
                or event_policy_evidence.get("message")
                or "Persisted event-policy labels are missing, stale, benchmark-beta-only, or not mature enough for offline review."
            ),
            commands=[
                *(
                    [str(event_policy_evidence.get("schema_command"))]
                    if event_policy_evidence.get("schema_command")
                    and (
                        "event_policy_evidence_schema_missing_benchmark_columns" in (event_policy_evidence.get("reasons") or [])
                        or "missing_event_policy_evidence_tables" in (event_policy_evidence.get("reasons") or [])
                    )
                    else []
                ),
                str(event_policy_evidence.get("command") or "python -m advisory.event_policy_evaluator --horizons 5 10 20"),
                str(event_policy_evidence.get("promotion_review_command") or "python -m advisory.event_policy_promotion --format json"),
                "python -m advisory.operator_health --full --skip-dhan",
            ],
            details={
                "section": "event_policy_evidence",
                "reasons": event_policy_evidence.get("reasons"),
                "schema_command": event_policy_evidence.get("schema_command"),
                "evaluation_rows": event_policy_evidence.get("evaluation_rows"),
                "matured_rows": event_policy_evidence.get("matured_rows"),
                "summary_rows": event_policy_evidence.get("summary_rows"),
                "candidate_strengthen_count": event_policy_evidence.get("candidate_strengthen_count"),
                "candidate_tighten_or_downgrade_count": event_policy_evidence.get("candidate_tighten_or_downgrade_count"),
                "benchmark_beta_not_policy_alpha_count": event_policy_evidence.get("benchmark_beta_not_policy_alpha_count"),
                "needs_benchmark_attribution_count": event_policy_evidence.get("needs_benchmark_attribution_count"),
                "missing_benchmark_columns": event_policy_evidence.get("missing_benchmark_columns"),
                "latest_evaluated_at": event_policy_evidence.get("latest_evaluated_at"),
                "age_days": event_policy_evidence.get("age_days"),
                "broker_execution_allowed": bool(event_policy_evidence.get("broker_execution_allowed")),
                "policy_auto_promotion_allowed": bool(event_policy_evidence.get("policy_auto_promotion_allowed")),
            },
        )

    context_watch_evidence = sections.get("context_watch_evidence") if isinstance(sections.get("context_watch_evidence"), dict) else {}
    if context_watch_evidence.get("status") in {"warn", "error"}:
        add(
            status=str(context_watch_evidence.get("status") or "warn"),
            title="Context-watch evidence is not policy-ready",
            reason=str(
                context_watch_evidence.get("error")
                or context_watch_evidence.get("message")
                or "Persisted context-watch labels are missing, stale, benchmark-beta-only, or not mature enough for offline review."
            ),
            commands=[
                str(context_watch_evidence.get("command") or "python -m advisory.context_watch_evaluator --horizons 5 10 20"),
                "python -m advisory.operator_health --full --skip-dhan",
            ],
            details={
                "section": "context_watch_evidence",
                "reasons": context_watch_evidence.get("reasons"),
                "evaluation_rows": context_watch_evidence.get("evaluation_rows"),
                "matured_rows": context_watch_evidence.get("matured_rows"),
                "summary_rows": context_watch_evidence.get("summary_rows"),
                "opportunity_candidate_count": context_watch_evidence.get("opportunity_candidate_count"),
                "harmful_watch_noise_count": context_watch_evidence.get("harmful_watch_noise_count"),
                "benchmark_beta_not_watch_alpha_count": context_watch_evidence.get("benchmark_beta_not_watch_alpha_count"),
                "missing_benchmark_columns": context_watch_evidence.get("missing_benchmark_columns"),
                "latest_evaluated_at": context_watch_evidence.get("latest_evaluated_at"),
                "age_days": context_watch_evidence.get("age_days"),
                "broker_execution_allowed": bool(context_watch_evidence.get("broker_execution_allowed")),
                "policy_auto_promotion_allowed": bool(context_watch_evidence.get("policy_auto_promotion_allowed")),
            },
        )

    negative_pressure_evidence = sections.get("negative_pressure_evidence") if isinstance(sections.get("negative_pressure_evidence"), dict) else {}
    if negative_pressure_evidence.get("status") in {"warn", "error"}:
        add(
            status=str(negative_pressure_evidence.get("status") or "warn"),
            title="Negative-pressure evidence is not policy-ready",
            reason=str(
                negative_pressure_evidence.get("error")
                or negative_pressure_evidence.get("message")
                or "Persisted negative-pressure labels are missing, stale, benchmark-beta-only, or not mature enough for offline review."
            ),
            commands=[
                str(negative_pressure_evidence.get("command") or "python -m advisory.negative_pressure_evaluator --horizons 5 10 20"),
                "python -m advisory.operator_health --full --skip-dhan",
            ],
            details={
                "section": "negative_pressure_evidence",
                "reasons": negative_pressure_evidence.get("reasons"),
                "evaluation_rows": negative_pressure_evidence.get("evaluation_rows"),
                "matured_rows": negative_pressure_evidence.get("matured_rows"),
                "summary_rows": negative_pressure_evidence.get("summary_rows"),
                "protective_candidate_count": negative_pressure_evidence.get("protective_candidate_count"),
                "benchmark_beta_not_derisk_alpha_count": negative_pressure_evidence.get("benchmark_beta_not_derisk_alpha_count"),
                "harmful_false_positive_count": negative_pressure_evidence.get("harmful_false_positive_count"),
                "missing_benchmark_columns": negative_pressure_evidence.get("missing_benchmark_columns"),
                "latest_evaluated_at": negative_pressure_evidence.get("latest_evaluated_at"),
                "age_days": negative_pressure_evidence.get("age_days"),
                "broker_execution_allowed": bool(negative_pressure_evidence.get("broker_execution_allowed")),
                "policy_auto_promotion_allowed": bool(negative_pressure_evidence.get("policy_auto_promotion_allowed")),
            },
        )

    adversarial_review_evidence = sections.get("adversarial_review_evidence") if isinstance(sections.get("adversarial_review_evidence"), dict) else {}
    if adversarial_review_evidence.get("status") in {"warn", "error"}:
        add(
            status=str(adversarial_review_evidence.get("status") or "warn"),
            title="Adversarial-review evidence is not policy-ready",
            reason=str(
                adversarial_review_evidence.get("error")
                or adversarial_review_evidence.get("message")
                or "Persisted adversarial-review labels are missing, stale, benchmark-beta-only, or not mature enough for offline review."
            ),
            commands=[
                *(
                    [str(adversarial_review_evidence.get("schema_command"))]
                    if adversarial_review_evidence.get("schema_command")
                    and "missing_adversarial_review_evidence_tables" in (adversarial_review_evidence.get("reasons") or [])
                    else []
                ),
                str(adversarial_review_evidence.get("command") or "python -m advisory.adversarial_review_evaluator --horizons 5 10 20"),
                "python -m advisory.operator_health --full --skip-dhan",
            ],
            details={
                "section": "adversarial_review_evidence",
                "reasons": adversarial_review_evidence.get("reasons"),
                "schema_command": adversarial_review_evidence.get("schema_command"),
                "evaluation_rows": adversarial_review_evidence.get("evaluation_rows"),
                "matured_rows": adversarial_review_evidence.get("matured_rows"),
                "summary_rows": adversarial_review_evidence.get("summary_rows"),
                "candidate_keep_or_tighten_count": adversarial_review_evidence.get("candidate_keep_or_tighten_count"),
                "benchmark_beta_not_veto_alpha_count": adversarial_review_evidence.get("benchmark_beta_not_veto_alpha_count"),
                "needs_benchmark_attribution_count": adversarial_review_evidence.get("needs_benchmark_attribution_count"),
                "relax_or_false_positive_count": adversarial_review_evidence.get("relax_or_false_positive_count"),
                "latest_evaluated_at": adversarial_review_evidence.get("latest_evaluated_at"),
                "age_days": adversarial_review_evidence.get("age_days"),
                "broker_execution_allowed": bool(adversarial_review_evidence.get("broker_execution_allowed")),
                "policy_auto_promotion_allowed": bool(adversarial_review_evidence.get("policy_auto_promotion_allowed")),
            },
        )

    signal_quality_split_evidence = sections.get("signal_quality_split_evidence") if isinstance(sections.get("signal_quality_split_evidence"), dict) else {}
    if signal_quality_split_evidence.get("status") in {"warn", "error"}:
        add(
            status=str(signal_quality_split_evidence.get("status") or "warn"),
            title="Signal-quality narrowed split evidence is not policy-ready",
            reason=str(
                signal_quality_split_evidence.get("error")
                or signal_quality_split_evidence.get("message")
                or "Persisted narrowed split evidence is missing, stale, under-baselined, or not mature enough for offline review."
            ),
            commands=[
                *(
                    [str(signal_quality_split_evidence.get("schema_command"))]
                    if signal_quality_split_evidence.get("schema_command")
                    and "missing_signal_quality_split_evidence_tables" in (signal_quality_split_evidence.get("reasons") or [])
                    else []
                ),
                str(signal_quality_split_evidence.get("command") or "python -m advisory.signal_quality_split_evaluator --stability-report --horizons 5 10 20 --format json"),
                "python -m advisory.model_training_runner --run-signal-quality-window-runner --include-signal-quality-split-reports --skip-s3-upload",
                "python -m advisory.operator_health --full --skip-dhan",
            ],
            details={
                "section": "signal_quality_split_evidence",
                "reasons": signal_quality_split_evidence.get("reasons"),
                "schema_command": signal_quality_split_evidence.get("schema_command"),
                "evaluation_rows": signal_quality_split_evidence.get("evaluation_rows"),
                "matured_rows": signal_quality_split_evidence.get("matured_rows"),
                "summary_rows": signal_quality_split_evidence.get("summary_rows"),
                "split_count": signal_quality_split_evidence.get("split_count"),
                "stable_candidate_count": signal_quality_split_evidence.get("stable_candidate_count"),
                "harmful_negative_control_count": signal_quality_split_evidence.get("harmful_negative_control_count"),
                "unstable_or_horizon_sensitive_count": signal_quality_split_evidence.get("unstable_or_horizon_sensitive_count"),
                "baseline_unavailable_window_count": signal_quality_split_evidence.get("baseline_unavailable_window_count"),
                "latest_evaluated_at": signal_quality_split_evidence.get("latest_evaluated_at"),
                "age_days": signal_quality_split_evidence.get("age_days"),
                "broker_execution_allowed": bool(signal_quality_split_evidence.get("broker_execution_allowed")),
                "policy_auto_promotion_allowed": bool(signal_quality_split_evidence.get("policy_auto_promotion_allowed")),
            },
        )

    context_overlay_reliability = sections.get("context_overlay_reliability") if isinstance(sections.get("context_overlay_reliability"), dict) else {}
    if context_overlay_reliability.get("status") in {"warn", "error"}:
        add(
            status=str(context_overlay_reliability.get("status") or "warn"),
            title="Context-overlay reliability evidence is stale or unavailable",
            reason=str(
                context_overlay_reliability.get("error")
                or context_overlay_reliability.get("message")
                or "Persisted context-overlay reliability evidence needs a fresher or better-covered run."
            ),
            commands=[
                str(context_overlay_reliability.get("command") or "python -m advisory.context_overlay_reliability_report --horizons 5 10 20 --format json"),
                "python -m advisory.operator_health --full --skip-dhan",
            ],
            details={
                "section": "context_overlay_reliability",
                "latest_evaluated_at": context_overlay_reliability.get("latest_evaluated_at"),
                "reasons": context_overlay_reliability.get("reasons"),
                "family_count": context_overlay_reliability.get("family_count"),
                "max_matured_rows": context_overlay_reliability.get("max_matured_rows"),
                "candidate_helpful_count": context_overlay_reliability.get("candidate_helpful_count"),
                "protective_candidate_count": context_overlay_reliability.get("protective_candidate_count"),
                "needs_benchmark_attribution_count": context_overlay_reliability.get("needs_benchmark_attribution_count"),
                "benchmark_beta_not_overlay_alpha_count": context_overlay_reliability.get("benchmark_beta_not_overlay_alpha_count"),
                "suppressed_reliability_count": context_overlay_reliability.get("suppressed_reliability_count"),
            },
        )

    macro_sector_alias_coverage = sections.get("macro_sector_alias_coverage") if isinstance(sections.get("macro_sector_alias_coverage"), dict) else {}
    if macro_sector_alias_coverage.get("status") in {"warn", "error"}:
        add(
            status=str(macro_sector_alias_coverage.get("status") or "warn"),
            title="Macro sector aliases need review",
            reason=str(
                macro_sector_alias_coverage.get("error")
                or macro_sector_alias_coverage.get("message")
                or "Generated macro overlay sectors are not fully mapped to market-context universe sector codes."
            ),
            commands=[
                str(macro_sector_alias_coverage.get("command") or "python -m advisory.macro_context_overlays --dry-run --format json"),
                "Update MACRO_SECTOR_CODE_ALIASES in advisory/macro_context_overlays.py after reviewing the sector mapping.",
                "python -m advisory.operator_health --full --skip-dhan",
            ],
            details={
                "section": "macro_sector_alias_coverage",
                "unmapped_sector_count": macro_sector_alias_coverage.get("unmapped_sector_count"),
                "unmapped_sectors": macro_sector_alias_coverage.get("unmapped_sectors"),
                "intentionally_broad_sector_count": macro_sector_alias_coverage.get("intentionally_broad_sector_count"),
                "mapped_sector_count": macro_sector_alias_coverage.get("mapped_sector_count"),
            },
        )

    theme_sector_alias_coverage = sections.get("theme_sector_alias_coverage") if isinstance(sections.get("theme_sector_alias_coverage"), dict) else {}
    if theme_sector_alias_coverage.get("status") in {"warn", "error"}:
        add(
            status=str(theme_sector_alias_coverage.get("status") or "warn"),
            title="Theme sector aliases need review",
            reason=str(
                theme_sector_alias_coverage.get("error")
                or theme_sector_alias_coverage.get("message")
                or "Generated news-theme overlay sectors are not fully mapped to market-context universe sector codes."
            ),
            commands=[
                str(theme_sector_alias_coverage.get("command") or "python -m advisory.news_theme_engine build-overlays --dry-run --format json"),
                "Update THEME_SECTOR_CODE_ALIASES in advisory/news_theme_engine.py after reviewing the sector mapping.",
                "python -m advisory.operator_health --full --skip-dhan",
            ],
            details={
                "section": "theme_sector_alias_coverage",
                "unmapped_sector_count": theme_sector_alias_coverage.get("unmapped_sector_count"),
                "unmapped_sectors": theme_sector_alias_coverage.get("unmapped_sectors"),
                "intentionally_broad_sector_count": theme_sector_alias_coverage.get("intentionally_broad_sector_count"),
                "mapped_sector_count": theme_sector_alias_coverage.get("mapped_sector_count"),
            },
        )

    signal_quality_overlay_rules = sections.get("signal_quality_overlay_rules") if isinstance(sections.get("signal_quality_overlay_rules"), dict) else {}
    if signal_quality_overlay_rules.get("status") in {"warn", "error"}:
        add(
            status=str(signal_quality_overlay_rules.get("status") or "warn"),
            title="Trusted signal-quality overlay rules are blocked by evidence gates",
            reason=str(
                signal_quality_overlay_rules.get("error")
                or signal_quality_overlay_rules.get("message")
                or "Trusted overlay rules are configured but not runtime-eligible under current reliability/stability gates."
            ),
            commands=[
                "python -m advisory.context_overlay_reliability_report --horizons 5 10 20 --format json",
                "python -m advisory.model_training_runner --run-signal-quality-window-runner --include-signal-quality-split-reports --skip-s3-upload",
                "python -m advisory.operator_health --full --skip-dhan",
            ],
            details={
                "section": "signal_quality_overlay_rules",
                "trusted_rule_count": signal_quality_overlay_rules.get("trusted_rule_count"),
                "runtime_eligible_rule_count": signal_quality_overlay_rules.get("runtime_eligible_rule_count"),
                "runtime_blocked_rule_count": signal_quality_overlay_rules.get("runtime_blocked_rule_count"),
                "reasons": signal_quality_overlay_rules.get("reasons"),
                "reliability_status": signal_quality_overlay_rules.get("reliability_status"),
                "reliability_latest_evaluated_at": signal_quality_overlay_rules.get("reliability_latest_evaluated_at"),
            },
        )

    feature_stage_gates = sections.get("feature_stage_gates") if isinstance(sections.get("feature_stage_gates"), dict) else {}
    if feature_stage_gates.get("status") in {"warn", "error"}:
        add(
            status=str(feature_stage_gates.get("status") or "warn"),
            title="Feature freshness gates are blocking advisory stages",
            reason=str(feature_stage_gates.get("error") or feature_stage_gates.get("message") or "One or more stage-level feature gates are blocked."),
            commands=[str(feature_stage_gates.get("command") or "./complete_data.sh && ./all_advisory.sh"), "python -m advisory.operator_health --skip-dhan"],
            details={
                "section": "feature_stage_gates",
                "blocked_stage_count": feature_stage_gates.get("blocked_stage_count"),
                "blocked_symbol_count": feature_stage_gates.get("blocked_symbol_count"),
                "symbols_checked": feature_stage_gates.get("symbols_checked"),
                "rows": feature_stage_gates.get("rows"),
            },
        )

    lifecycle_policy_audit = sections.get("lifecycle_policy_audit") if isinstance(sections.get("lifecycle_policy_audit"), dict) else {}
    if lifecycle_policy_audit.get("status") in {"warn", "error"}:
        add(
            status=str(lifecycle_policy_audit.get("status") or "warn"),
            title="Lifecycle stop-policy audit rows are missing",
            reason=str(
                lifecycle_policy_audit.get("error")
                or lifecycle_policy_audit.get("message")
                or "Recent tighten-stop actions should have matching lifecycle policy-change audit rows."
            ),
            commands=[
                str(lifecycle_policy_audit.get("command") or "./all_advisory.sh && python -m advisory.operator_health --skip-dhan"),
                "python -m advisory.position_lifecycle --format json",
            ],
            details={
                "section": "lifecycle_policy_audit",
                "tighten_stop_rows": lifecycle_policy_audit.get("tighten_stop_rows"),
                "auditable_rows": lifecycle_policy_audit.get("auditable_rows"),
                "missing_audit_count": lifecycle_policy_audit.get("missing_audit_count"),
                "sample_missing": lifecycle_policy_audit.get("sample_missing"),
            },
        )

    fallback_telemetry = sections.get("fallback_telemetry") if isinstance(sections.get("fallback_telemetry"), dict) else {}
    if fallback_telemetry.get("status") in {"warn", "error"}:
        nse_session_reset_count = int(fallback_telemetry.get("nse_session_reset_count") or 0)
        nse_retry_count = int(fallback_telemetry.get("nse_retry_count") or 0)
        db_retry_count = int(fallback_telemetry.get("db_retry_count") or 0)
        db_retry_error_count = int(fallback_telemetry.get("db_retry_error_count") or 0)
        counts_by_type = fallback_telemetry.get("counts_by_type") if isinstance(fallback_telemetry.get("counts_by_type"), dict) else {}
        current_price_cache_failures = int(counts_by_type.get("current_price_cache_unavailable") or 0)
        operator_snapshot_fallbacks = (
            int(counts_by_type.get("operator_snapshot_price_cache_refresh_failed") or 0)
            + int(counts_by_type.get("operator_snapshot_load_failed") or 0)
            + int(counts_by_type.get("operator_snapshot_section_load_failed") or 0)
        )
        risk_liquidity_fallbacks = int(counts_by_type.get("risk_liquidity_cap_adv20_missing") or 0)
        risk_context_fallbacks = int(counts_by_type.get("risk_macro_context_unavailable") or 0) + int(counts_by_type.get("risk_exchange_context_unavailable") or 0)
        signal_refresh_fallbacks = (
            int(counts_by_type.get("signal_refresh_latest_row_unavailable") or 0)
            + int(counts_by_type.get("signal_refresh_event_policy_unavailable") or 0)
            + int(counts_by_type.get("signal_refresh_router_actions_unavailable") or 0)
        )
        event_router_fallbacks = (
            int(counts_by_type.get("event_router_source_rows_load_failed") or 0)
            + int(counts_by_type.get("event_router_watchlist_priority_load_failed") or 0)
            + int(counts_by_type.get("event_router_symbol_refresh_failed") or 0)
        )
        external_task_queue_fallbacks = (
            int(counts_by_type.get("external_task_queue_status_load_failed") or 0)
            + int(counts_by_type.get("external_task_queue_task_failed") or 0)
        )
        config_change_fallbacks = (
            int(counts_by_type.get("config_change_technical_decision_load_failed") or 0)
            + int(counts_by_type.get("config_change_signal_quality_decision_load_failed") or 0)
            + int(counts_by_type.get("config_change_event_policy_decision_load_failed") or 0)
            + int(counts_by_type.get("config_change_previews_load_failed") or 0)
        )
        research_ledger_fallbacks = (
            int(counts_by_type.get("research_ledger_start_write_failed") or 0)
            + int(counts_by_type.get("research_ledger_finish_write_failed") or 0)
            + int(counts_by_type.get("research_ledger_list_runs_failed") or 0)
        )
        action_conflict_rule_fallbacks = (
            int(counts_by_type.get("action_conflict_rule_lookup_failed_default_rules") or 0)
            + int(counts_by_type.get("action_conflict_rule_load_failed_default_rules") or 0)
            + int(counts_by_type.get("action_dynamic_conflict_rule_lookup_failed") or 0)
            + int(counts_by_type.get("action_dynamic_conflict_rule_load_failed") or 0)
        )
        wait_signal_source_fallbacks = (
            int(counts_by_type.get("wait_signal_source_table_lookup_failed") or 0)
            + int(counts_by_type.get("wait_signal_source_column_check_failed") or 0)
            + int(counts_by_type.get("wait_signal_source_table_missing_columns") or 0)
            + int(counts_by_type.get("wait_signal_source_event_load_failed") or 0)
        )
        feature_freshness_fallbacks = int(counts_by_type.get("feature_freshness_input_lookup_failed") or 0)
        company_memory_source_fallbacks = (
            int(counts_by_type.get("company_memory_source_table_lookup_failed") or 0)
            + int(counts_by_type.get("company_memory_source_rows_load_failed") or 0)
            + int(counts_by_type.get("company_memory_wait_signals_load_failed") or 0)
        )
        news_theme_fallbacks = (
            int(counts_by_type.get("news_theme_asof_resolve_failed") or 0)
            + int(counts_by_type.get("news_theme_market_news_load_failed") or 0)
        )
        event_policy_evaluator_fallbacks = (
            int(counts_by_type.get("event_policy_evaluator_schema_lookup_failed") or 0)
            + int(counts_by_type.get("event_policy_evaluator_required_columns_missing") or 0)
            + int(counts_by_type.get("event_policy_evaluator_policy_rows_load_failed") or 0)
        )
        event_data_quality_fallbacks = (
            int(counts_by_type.get("event_data_quality_table_lookup_failed") or 0)
            + int(counts_by_type.get("event_data_quality_schema_lookup_failed") or 0)
            + int(counts_by_type.get("event_data_quality_row_count_failed") or 0)
            + int(counts_by_type.get("event_data_quality_sync_state_load_failed") or 0)
            + int(counts_by_type.get("event_data_quality_source_freshness_query_failed") or 0)
            + int(counts_by_type.get("event_data_quality_announcement_readiness_query_failed") or 0)
            + int(counts_by_type.get("event_data_quality_exchange_event_readiness_query_failed") or 0)
            + int(counts_by_type.get("event_data_quality_exchange_feature_readiness_query_failed") or 0)
        )
        trace_summary_fallbacks = (
            int(counts_by_type.get("trace_summary_cache_load_failed") or 0)
            + int(counts_by_type.get("trace_summary_recent_symbols_load_failed") or 0)
            + int(counts_by_type.get("trace_summary_recent_events_load_failed") or 0)
            + int(counts_by_type.get("trace_summary_symbol_build_failed") or 0)
            + int(counts_by_type.get("trace_summary_event_build_failed") or 0)
        )
        event_evidence_fallbacks = (
            int(counts_by_type.get("event_evidence_table_lookup_failed") or 0)
            + int(counts_by_type.get("event_evidence_schema_lookup_failed") or 0)
            + int(counts_by_type.get("event_evidence_max_date_lookup_failed") or 0)
            + int(counts_by_type.get("event_evidence_bhavcopy_source_load_failed") or 0)
            + int(counts_by_type.get("event_evidence_announcement_source_load_failed") or 0)
        )
        announcement_fetch_fallbacks = int(counts_by_type.get("announcement_managed_fetch_failed") or 0)
        announcement_mapping_fallbacks = int(counts_by_type.get("announcement_company_master_mapping_missing") or 0)
        announcement_document_lookup_fallbacks = int(counts_by_type.get("announcement_watch_document_lookup_failed") or 0)
        announcement_watch_ingest_fallbacks = int(counts_by_type.get("announcement_watch_managed_ingest_target_failed") or 0)
        event_meta_model_fallbacks = (
            int(counts_by_type.get("event_meta_model_event_table_lookup_failed") or 0)
            + int(counts_by_type.get("event_meta_model_event_rows_load_failed") or 0)
            + int(counts_by_type.get("event_meta_model_price_history_load_failed") or 0)
            + int(counts_by_type.get("event_meta_model_intraday_context_load_failed") or 0)
            + int(counts_by_type.get("event_meta_model_macro_context_load_failed") or 0)
            + int(counts_by_type.get("event_meta_model_exchange_context_load_failed") or 0)
        )
        exchange_features_fallbacks = (
            int(counts_by_type.get("exchange_features_table_lookup_failed") or 0)
            + int(counts_by_type.get("exchange_features_max_date_lookup_failed") or 0)
            + int(counts_by_type.get("exchange_features_trading_days_load_failed") or 0)
            + int(counts_by_type.get("exchange_features_events_load_failed") or 0)
        )
        market_context_fallbacks = (
            int(counts_by_type.get("market_context_table_lookup_failed") or 0)
            + int(counts_by_type.get("market_context_technical_load_failed") or 0)
            + int(counts_by_type.get("market_context_market_cap_load_failed") or 0)
            + int(counts_by_type.get("market_context_exchange_context_load_failed") or 0)
            + int(counts_by_type.get("market_context_regime_load_failed") or 0)
            + int(counts_by_type.get("market_context_event_count_load_failed") or 0)
            + int(counts_by_type.get("market_context_summary_cache_load_failed") or 0)
            + int(counts_by_type.get("market_context_universe_cache_load_failed") or 0)
        )
        fundamental_snapshot_fallbacks = (
            int(counts_by_type.get("fundamental_snapshot_universe_load_failed") or 0)
            + int(counts_by_type.get("fundamental_snapshot_company_master_mapping_failed") or 0)
            + int(counts_by_type.get("fundamental_snapshot_peer_membership_load_failed") or 0)
            + int(counts_by_type.get("fundamental_snapshot_max_date_lookup_failed") or 0)
            + int(counts_by_type.get("fundamental_snapshot_trading_days_load_failed") or 0)
            + int(counts_by_type.get("fundamental_snapshot_release_calendar_load_failed") or 0)
            + int(counts_by_type.get("fundamental_snapshot_income_load_failed") or 0)
            + int(counts_by_type.get("fundamental_snapshot_balance_load_failed") or 0)
            + int(counts_by_type.get("fundamental_snapshot_cashflow_load_failed") or 0)
            + int(counts_by_type.get("fundamental_snapshot_shareholding_load_failed") or 0)
        )
        ts_forecast_fallbacks = (
            int(counts_by_type.get("ts_forecast_features_universe_load_failed") or 0)
            + int(counts_by_type.get("ts_forecast_features_ohlcv_load_failed") or 0)
            + int(counts_by_type.get("ts_forecast_features_ohlcv_refresh_failed") or 0)
            + int(counts_by_type.get("ts_forecast_evaluator_forecast_load_failed") or 0)
            + int(counts_by_type.get("ts_forecast_evaluator_price_window_load_failed") or 0)
        )
        technical_threshold_calibration_fallbacks = (
            int(counts_by_type.get("technical_threshold_calibration_schema_lookup_failed") or 0)
            + int(counts_by_type.get("technical_threshold_calibration_signal_rows_load_failed") or 0)
            + int(counts_by_type.get("technical_threshold_calibration_price_history_load_failed") or 0)
        )
        signal_quality_evaluator_fallbacks = (
            int(counts_by_type.get("signal_quality_evaluator_schema_lookup_failed") or 0)
            + int(counts_by_type.get("signal_quality_evaluator_event_policy_load_failed") or 0)
            + int(counts_by_type.get("signal_quality_evaluator_bhavcopy_load_failed") or 0)
            + int(counts_by_type.get("signal_quality_evaluator_company_memory_load_failed") or 0)
        )
        promotion_review_fallbacks = (
            int(counts_by_type.get("technical_threshold_promotion_calibration_load_failed") or 0)
            + int(counts_by_type.get("technical_threshold_promotion_summary_load_failed") or 0)
            + int(counts_by_type.get("signal_quality_promotion_summary_load_failed") or 0)
            + int(counts_by_type.get("event_policy_promotion_summary_load_failed") or 0)
        )
        event_model_promotion_fallbacks = (
            int(counts_by_type.get("event_model_promotion_label_coverage_load_failed") or 0)
            + int(counts_by_type.get("event_model_promotion_score_freshness_load_failed") or 0)
        )
        macro_features_fallbacks = (
            int(counts_by_type.get("macro_features_table_lookup_failed") or 0)
            + int(counts_by_type.get("macro_features_source_load_failed") or 0)
        )
        regime_engine_fallbacks = (
            int(counts_by_type.get("regime_engine_table_lookup_failed") or 0)
            + int(counts_by_type.get("regime_engine_nse_benchmark_load_failed") or 0)
            + int(counts_by_type.get("regime_engine_dhan_benchmark_load_failed") or 0)
            + int(counts_by_type.get("regime_engine_macro_features_load_failed") or 0)
            + int(counts_by_type.get("regime_engine_macro_daily_load_failed") or 0)
        )
        technical_features_fallbacks = (
            int(counts_by_type.get("technical_features_universe_load_failed") or 0)
            + int(counts_by_type.get("technical_features_nse_benchmark_load_failed") or 0)
            + int(counts_by_type.get("technical_features_dhan_benchmark_load_failed") or 0)
            + int(counts_by_type.get("technical_features_price_history_load_failed") or 0)
            + int(counts_by_type.get("technical_features_sector_mapping_load_failed") or 0)
            + int(counts_by_type.get("technical_features_peer_membership_load_failed") or 0)
            + int(counts_by_type.get("technical_features_peer_ohlcv_load_failed") or 0)
        )
        intraday_features_fallbacks = (
            int(counts_by_type.get("intraday_features_table_lookup_failed") or 0)
            + int(counts_by_type.get("intraday_features_universe_load_failed") or 0)
            + int(counts_by_type.get("intraday_features_coverage_load_failed") or 0)
            + int(counts_by_type.get("intraday_features_dhan_sync_failed") or 0)
            + int(counts_by_type.get("intraday_features_history_load_failed") or 0)
            + int(counts_by_type.get("intraday_features_daily_reference_load_failed") or 0)
        )
        rule_engine_fallbacks = (
            int(counts_by_type.get("rule_engine_table_lookup_failed") or 0)
            + int(counts_by_type.get("rule_engine_screener_date_lookup_failed") or 0)
            + int(counts_by_type.get("rule_engine_effective_dates_lookup_failed") or 0)
            + int(counts_by_type.get("rule_engine_intraday_date_lookup_failed") or 0)
            + int(counts_by_type.get("rule_engine_regime_load_failed") or 0)
            + int(counts_by_type.get("rule_engine_overlay_load_failed") or 0)
            + int(counts_by_type.get("rule_engine_screener_universe_load_failed") or 0)
            + int(counts_by_type.get("rule_engine_technical_load_failed") or 0)
            + int(counts_by_type.get("rule_engine_intraday_load_failed") or 0)
            + int(counts_by_type.get("rule_engine_fundamentals_load_failed") or 0)
        )
        portfolio_engine_fallbacks = (
            int(counts_by_type.get("portfolio_engine_table_lookup_failed") or 0)
            + int(counts_by_type.get("portfolio_engine_allocations_load_failed") or 0)
            + int(counts_by_type.get("portfolio_engine_symbol_metadata_load_failed") or 0)
        )
        position_lifecycle_fallbacks = (
            int(counts_by_type.get("position_lifecycle_table_lookup_failed") or 0)
            + int(counts_by_type.get("position_lifecycle_open_orders_load_failed") or 0)
            + int(counts_by_type.get("position_lifecycle_price_identity_failed") or 0)
            + int(counts_by_type.get("position_lifecycle_price_history_load_failed") or 0)
            + int(counts_by_type.get("position_lifecycle_technical_context_load_failed") or 0)
        )
        execution_engine_fallbacks = (
            int(counts_by_type.get("execution_broker_fund_limits_failed") or 0)
            + int(counts_by_type.get("execution_broker_inventory_failed") or 0)
            + int(counts_by_type.get("execution_recon_table_lookup_failed") or 0)
            + int(counts_by_type.get("execution_recon_targets_load_failed") or 0)
            + int(counts_by_type.get("execution_live_submit_failed") or 0)
            + int(counts_by_type.get("execution_broker_reconcile_failed") or 0)
        )
        watchlist_builder_fallbacks = (
            int(counts_by_type.get("watchlist_builder_event_transition_load_failed") or 0)
            + int(counts_by_type.get("watchlist_builder_existing_state_load_failed") or 0)
        )
        symbol_trace_fallbacks = (
            int(counts_by_type.get("symbol_trace_table_lookup_failed") or 0)
            + int(counts_by_type.get("symbol_trace_stage_rows_load_failed") or 0)
            + int(counts_by_type.get("symbol_trace_screener_rows_load_failed") or 0)
            + int(counts_by_type.get("symbol_trace_rejections_load_failed") or 0)
            + int(counts_by_type.get("symbol_trace_aggregated_event_load_failed") or 0)
        )
        setup_trace_fallbacks = (
            int(counts_by_type.get("setup_trace_table_lookup_failed") or 0)
            + int(counts_by_type.get("setup_trace_asof_lookup_failed") or 0)
            + int(counts_by_type.get("setup_trace_regime_load_failed") or 0)
            + int(counts_by_type.get("setup_trace_overlay_load_failed") or 0)
            + int(counts_by_type.get("setup_trace_screener_rows_load_failed") or 0)
            + int(counts_by_type.get("setup_trace_column_lookup_failed") or 0)
            + int(counts_by_type.get("setup_trace_stage_rows_load_failed") or 0)
        )
        if db_retry_count:
            add(
                status="error" if db_retry_error_count else "warn",
                title="Postgres retry or reconnect telemetry was recorded",
                reason=(
                    f"DB retry telemetry appeared in the last {fallback_telemetry.get('window_hours') or 24} hours "
                    f"(retry_events={db_retry_count}, exhausted={db_retry_error_count})."
                ),
                commands=["python -m advisory.operator_health --full --skip-dhan", "python scripts/api_performance_report.py --limit 20"],
                details={
                    "section": "fallback_telemetry",
                    "db_retry_count": db_retry_count,
                    "db_retry_error_count": db_retry_error_count,
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if current_price_cache_failures:
            add(
                status="warn",
                title="Operator current-price cache fallback was used",
                reason=(
                    f"The current-price cache was unavailable {current_price_cache_failures} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours, so the API may have fallen back to OHLCV history."
                ),
                commands=["python -m advisory.current_prices", "python -m advisory.operator_health --full --skip-dhan"],
                details={
                    "section": "fallback_telemetry",
                    "current_price_cache_unavailable": current_price_cache_failures,
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if operator_snapshot_fallbacks:
            add(
                status="warn",
                title="Operator snapshot fallback used",
                reason=(
                    f"Operator snapshot load or price-cache refresh failed {operator_snapshot_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. Frontend pages may fall back to live payloads, stale snapshots, or degraded current-price context."
                ),
                commands=["python -m advisory.operator_snapshot", "python -m advisory.operator_health --full --skip-dhan"],
                details={
                    "section": "fallback_telemetry",
                    "operator_snapshot_price_cache_refresh_failed": counts_by_type.get("operator_snapshot_price_cache_refresh_failed"),
                    "operator_snapshot_load_failed": counts_by_type.get("operator_snapshot_load_failed"),
                    "operator_snapshot_section_load_failed": counts_by_type.get("operator_snapshot_section_load_failed"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if risk_liquidity_fallbacks:
            add(
                status="warn",
                title="Risk sizing used liquidity fallback because ADV20 was missing",
                reason=(
                    f"Risk allocation used max-allocation liquidity fallback {risk_liquidity_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. Position sizes may be conservative but less liquidity-aware."
                ),
                commands=["./complete_data.sh", "./all_advisory.sh", "python -m advisory.operator_health --full --skip-dhan"],
                details={
                    "section": "fallback_telemetry",
                    "risk_liquidity_cap_adv20_missing": risk_liquidity_fallbacks,
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if risk_context_fallbacks:
            add(
                status="warn",
                title="Risk sizing continued with missing context fallback",
                reason=(
                    f"Risk allocation skipped macro or exchange-event context {risk_context_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours after source lookup failures."
                ),
                commands=["./complete_data.sh", "./all_advisory.sh", "python -m advisory.operator_health --full --skip-dhan"],
                details={
                    "section": "fallback_telemetry",
                    "risk_macro_context_unavailable": counts_by_type.get("risk_macro_context_unavailable"),
                    "risk_exchange_context_unavailable": counts_by_type.get("risk_exchange_context_unavailable"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if signal_refresh_fallbacks:
            add(
                status="warn",
                title="Signal refresh continued with missing source context",
                reason=(
                    f"Signal refresh skipped one or more source-context lookups {signal_refresh_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours after lookup failures."
                ),
                commands=["./all_watchers.sh", "python -m advisory.operator_health --full --skip-dhan"],
                details={
                    "section": "fallback_telemetry",
                    "signal_refresh_latest_row_unavailable": counts_by_type.get("signal_refresh_latest_row_unavailable"),
                    "signal_refresh_event_policy_unavailable": counts_by_type.get("signal_refresh_event_policy_unavailable"),
                    "signal_refresh_router_actions_unavailable": counts_by_type.get("signal_refresh_router_actions_unavailable"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if event_router_fallbacks:
            add(
                status="error" if int(counts_by_type.get("event_router_symbol_refresh_failed") or 0) else "warn",
                title="Event router source or refresh path failed",
                reason=(
                    f"Event router source loading or symbol refresh failed {event_router_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. Watcher-triggered fast refreshes may be missing, delayed, or lower priority."
                ),
                commands=["python -m advisory.event_router --dry-run", "./all_watchers.sh", "python -m advisory.operator_health --full --skip-dhan"],
                details={
                    "section": "fallback_telemetry",
                    "event_router_source_rows_load_failed": counts_by_type.get("event_router_source_rows_load_failed"),
                    "event_router_watchlist_priority_load_failed": counts_by_type.get("event_router_watchlist_priority_load_failed"),
                    "event_router_symbol_refresh_failed": counts_by_type.get("event_router_symbol_refresh_failed"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if external_task_queue_fallbacks:
            add(
                status="error" if int(counts_by_type.get("external_task_queue_task_failed") or 0) else "warn",
                title="External task queue degraded",
                reason=(
                    f"Serialized external task queue status or worker tasks failed {external_task_queue_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. NSE/Dhan/Screener queued work may be delayed, retried, or invisible to the operator."
                ),
                commands=["python -m advisory.external_task_queue --status", "./all_external_workers.sh", "python -m advisory.operator_health --full --skip-dhan"],
                details={
                    "section": "fallback_telemetry",
                    "external_task_queue_status_load_failed": counts_by_type.get("external_task_queue_status_load_failed"),
                    "external_task_queue_task_failed": counts_by_type.get("external_task_queue_task_failed"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if config_change_fallbacks:
            add(
                status="warn",
                title="Config-change preview source failed",
                reason=(
                    f"Config-change preview source or history lookups failed {config_change_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. Reviewed config diffs may be unavailable; no config is applied automatically."
                ),
                commands=["python -m advisory.config_change_assistant --help", "python -m advisory.operator_health --full --skip-dhan"],
                details={
                    "section": "fallback_telemetry",
                    "config_change_technical_decision_load_failed": counts_by_type.get("config_change_technical_decision_load_failed"),
                    "config_change_signal_quality_decision_load_failed": counts_by_type.get("config_change_signal_quality_decision_load_failed"),
                    "config_change_event_policy_decision_load_failed": counts_by_type.get("config_change_event_policy_decision_load_failed"),
                    "config_change_previews_load_failed": counts_by_type.get("config_change_previews_load_failed"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if research_ledger_fallbacks:
            add(
                status="warn",
                title="Research ledger degraded",
                reason=(
                    f"Research ledger writes or reads failed {research_ledger_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. Research validation, false-discovery audit, or model-evidence visibility may be incomplete."
                ),
                commands=["python -m advisory.research_ledger --limit 20", "python -m advisory.operator_health --full --skip-dhan"],
                details={
                    "section": "fallback_telemetry",
                    "research_ledger_start_write_failed": counts_by_type.get("research_ledger_start_write_failed"),
                    "research_ledger_finish_write_failed": counts_by_type.get("research_ledger_finish_write_failed"),
                    "research_ledger_list_runs_failed": counts_by_type.get("research_ledger_list_runs_failed"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if action_conflict_rule_fallbacks:
            add(
                status="warn",
                title="Action consolidation used fallback conflict-rule behavior",
                reason=(
                    f"Action consolidation could not read conflict-rule state {action_conflict_rule_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. Default deterministic rules may have been used, "
                    "and promoted dynamic rules may have been ignored for affected runs."
                ),
                commands=["./all_advisory.sh", "python -m advisory.operator_health --full --skip-dhan"],
                details={
                    "section": "fallback_telemetry",
                    "action_conflict_rule_lookup_failed_default_rules": counts_by_type.get("action_conflict_rule_lookup_failed_default_rules"),
                    "action_conflict_rule_load_failed_default_rules": counts_by_type.get("action_conflict_rule_load_failed_default_rules"),
                    "action_dynamic_conflict_rule_lookup_failed": counts_by_type.get("action_dynamic_conflict_rule_lookup_failed"),
                    "action_dynamic_conflict_rule_load_failed": counts_by_type.get("action_dynamic_conflict_rule_load_failed"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if wait_signal_source_fallbacks:
            add(
                status="warn",
                title="Wait-signal matching skipped source evidence",
                reason=(
                    f"Wait-signal matching skipped one or more source evidence lookups {wait_signal_source_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. Price wait-signals may still work, but event/news/announcement waits may miss matches."
                ),
                commands=["./all_watchers.sh", "python -m advisory.operator_health --full --skip-dhan"],
                details={
                    "section": "fallback_telemetry",
                    "wait_signal_source_table_lookup_failed": counts_by_type.get("wait_signal_source_table_lookup_failed"),
                    "wait_signal_source_column_check_failed": counts_by_type.get("wait_signal_source_column_check_failed"),
                    "wait_signal_source_table_missing_columns": counts_by_type.get("wait_signal_source_table_missing_columns"),
                    "wait_signal_source_event_load_failed": counts_by_type.get("wait_signal_source_event_load_failed"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if feature_freshness_fallbacks:
            add(
                status="warn",
                title="Feature freshness lookup fallback was used",
                reason=(
                    f"Feature freshness marked inputs as errored {feature_freshness_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours after source lookup failures. Positive actions may be downgraded to Manual Review."
                ),
                commands=["./complete_data.sh", "./all_advisory.sh", "python -m advisory.operator_health --full --skip-dhan"],
                details={
                    "section": "fallback_telemetry",
                    "feature_freshness_input_lookup_failed": feature_freshness_fallbacks,
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if company_memory_source_fallbacks:
            add(
                status="warn",
                title="Company-memory review skipped source context",
                reason=(
                    f"Company-memory review skipped one or more source-context lookups {company_memory_source_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. Review-only memory summaries may be based on partial evidence."
                ),
                commands=["./all_advisory.sh", "python -m advisory.operator_health --full --skip-dhan"],
                details={
                    "section": "fallback_telemetry",
                    "company_memory_source_table_lookup_failed": counts_by_type.get("company_memory_source_table_lookup_failed"),
                    "company_memory_source_rows_load_failed": counts_by_type.get("company_memory_source_rows_load_failed"),
                    "company_memory_wait_signals_load_failed": counts_by_type.get("company_memory_wait_signals_load_failed"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if news_theme_fallbacks:
            add(
                status="warn",
                title="News-theme recommendations returned empty fallback output",
                reason=(
                    f"News-theme recommendations could not resolve as-of date or load recent market news {news_theme_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. Theme-to-screener routing may be stale or absent."
                ),
                commands=["./complete_data.sh", "python -m advisory.operator_health --full --skip-dhan"],
                details={
                    "section": "fallback_telemetry",
                    "news_theme_asof_resolve_failed": counts_by_type.get("news_theme_asof_resolve_failed"),
                    "news_theme_market_news_load_failed": counts_by_type.get("news_theme_market_news_load_failed"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if event_policy_evaluator_fallbacks:
            add(
                status="warn",
                title="Event-policy evaluator returned empty fallback output",
                reason=(
                    f"Event-policy evaluator skipped source rows {event_policy_evaluator_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours after schema or source-load failures. Research calibration may be stale or incomplete."
                ),
                commands=["./all_event_policy_evaluator.sh", "python -m advisory.operator_health --full --skip-dhan"],
                details={
                    "section": "fallback_telemetry",
                    "event_policy_evaluator_schema_lookup_failed": counts_by_type.get("event_policy_evaluator_schema_lookup_failed"),
                    "event_policy_evaluator_required_columns_missing": counts_by_type.get("event_policy_evaluator_required_columns_missing"),
                    "event_policy_evaluator_policy_rows_load_failed": counts_by_type.get("event_policy_evaluator_policy_rows_load_failed"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if event_data_quality_fallbacks:
            add(
                status="warn",
                title="Event data-quality checks used fallback source handling",
                reason=(
                    f"Event data-quality checks hit source lookup/count/readiness failures {event_data_quality_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. Health may show explicit source errors, but the quality check itself was partially degraded."
                ),
                commands=["python -m advisory.event_data_quality --format json", "python -m advisory.operator_health --full --skip-dhan"],
                details={
                    "section": "fallback_telemetry",
                    "event_data_quality_table_lookup_failed": counts_by_type.get("event_data_quality_table_lookup_failed"),
                    "event_data_quality_schema_lookup_failed": counts_by_type.get("event_data_quality_schema_lookup_failed"),
                    "event_data_quality_row_count_failed": counts_by_type.get("event_data_quality_row_count_failed"),
                    "event_data_quality_sync_state_load_failed": counts_by_type.get("event_data_quality_sync_state_load_failed"),
                    "event_data_quality_source_freshness_query_failed": counts_by_type.get("event_data_quality_source_freshness_query_failed"),
                    "event_data_quality_announcement_readiness_query_failed": counts_by_type.get("event_data_quality_announcement_readiness_query_failed"),
                    "event_data_quality_exchange_event_readiness_query_failed": counts_by_type.get("event_data_quality_exchange_event_readiness_query_failed"),
                    "event_data_quality_exchange_feature_readiness_query_failed": counts_by_type.get("event_data_quality_exchange_feature_readiness_query_failed"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if trace_summary_fallbacks:
            add(
                status="warn",
                title="Trace summary cache used fallback behavior",
                reason=(
                    f"Trace summary cache lookups or rebuilds failed {trace_summary_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. Symbol/event detail pages may fall back to live or partial trace data."
                ),
                commands=["python -m advisory.trace_summary_store --dry-run", "python -m advisory.operator_health --full --skip-dhan"],
                details={
                    "section": "fallback_telemetry",
                    "trace_summary_cache_load_failed": counts_by_type.get("trace_summary_cache_load_failed"),
                    "trace_summary_recent_symbols_load_failed": counts_by_type.get("trace_summary_recent_symbols_load_failed"),
                    "trace_summary_recent_events_load_failed": counts_by_type.get("trace_summary_recent_events_load_failed"),
                    "trace_summary_symbol_build_failed": counts_by_type.get("trace_summary_symbol_build_failed"),
                    "trace_summary_event_build_failed": counts_by_type.get("trace_summary_event_build_failed"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if event_evidence_fallbacks:
            add(
                status="warn",
                title="Compact event evidence refresh used fallback behavior",
                reason=(
                    f"Compact bhavcopy or announcement evidence refresh hit lookup/source-query failures {event_evidence_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. LLM/event paths may use stale compact evidence or fail until sources are repaired."
                ),
                commands=["python -m advisory.event_evidence_store --dry-run", "python -m advisory.event_data_quality --format json", "python -m advisory.operator_health --full --skip-dhan"],
                details={
                    "section": "fallback_telemetry",
                    "event_evidence_table_lookup_failed": counts_by_type.get("event_evidence_table_lookup_failed"),
                    "event_evidence_schema_lookup_failed": counts_by_type.get("event_evidence_schema_lookup_failed"),
                    "event_evidence_max_date_lookup_failed": counts_by_type.get("event_evidence_max_date_lookup_failed"),
                    "event_evidence_bhavcopy_source_load_failed": counts_by_type.get("event_evidence_bhavcopy_source_load_failed"),
                    "event_evidence_announcement_source_load_failed": counts_by_type.get("event_evidence_announcement_source_load_failed"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if announcement_mapping_fallbacks:
            add(
                status="warn",
                title="Announcement ingest skipped symbols with missing company-master mappings",
                reason=(
                    f"Managed announcement ingestion skipped {announcement_mapping_fallbacks} ticker(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours because no company-master mapping was found. "
                    "Those symbols will not receive announcement evidence until identity/company-master mappings are repaired."
                ),
                commands=[
                    "python -m data.dhanlive.scrip_master",
                    "python -m advisory.identity_issues --limit 100",
                    "python -m advisory.operator_health --full --skip-dhan",
                    "./all_advisory.sh",
                ],
                details={
                    "section": "fallback_telemetry",
                    "announcement_company_master_mapping_missing": announcement_mapping_fallbacks,
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if announcement_fetch_fallbacks:
            add(
                status="warn",
                title="Announcement ingest source fetch failed for some symbols",
                reason=(
                    f"Managed announcement ingestion could not fetch source announcements {announcement_fetch_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. Advisory continues, but affected symbols may use stale or missing announcement evidence."
                ),
                commands=[
                    "./all_watchers.sh",
                    "./all_advisory.sh",
                    "python -m advisory.event_data_quality --format json",
                    "python -m advisory.operator_health --full --skip-dhan",
                ],
                details={
                    "section": "fallback_telemetry",
                    "announcement_managed_fetch_failed": announcement_fetch_fallbacks,
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if announcement_document_lookup_fallbacks:
            add(
                status="warn",
                title="Announcement watch document lookup failed for some symbols",
                reason=(
                    f"Announcement watcher could not load persisted document history {announcement_document_lookup_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. Advisory continues, but affected symbols may miss fresh announcement-event matching."
                ),
                commands=[
                    "python -m advisory.event_data_quality --format json",
                    "python -m advisory.operator_health --full --skip-dhan",
                    "./all_advisory.sh",
                ],
                details={
                    "section": "fallback_telemetry",
                    "announcement_watch_document_lookup_failed": announcement_document_lookup_fallbacks,
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if announcement_watch_ingest_fallbacks:
            add(
                status="warn",
                title="Announcement watch managed ingest failed for some symbols",
                reason=(
                    f"Announcement watcher managed ingest failed before returning a summary {announcement_watch_ingest_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. Advisory continues, but affected symbols may have stale or missing announcement evidence."
                ),
                commands=[
                    "python -m advisory.event_data_quality --format json",
                    "python -m advisory.operator_health --full --skip-dhan",
                    "./all_advisory.sh",
                ],
                details={
                    "section": "fallback_telemetry",
                    "announcement_watch_managed_ingest_target_failed": announcement_watch_ingest_fallbacks,
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if event_meta_model_fallbacks:
            add(
                status="warn",
                title="Event meta-model used degraded source context",
                reason=(
                    f"Event meta-model training/scoring hit required-source or optional-context failures {event_meta_model_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. Labels may fail to build, or model scores may omit intraday/macro/exchange context."
                ),
                commands=["./all_ml.sh", "python -m advisory.operator_health --full --skip-dhan"],
                details={
                    "section": "fallback_telemetry",
                    "event_meta_model_event_table_lookup_failed": counts_by_type.get("event_meta_model_event_table_lookup_failed"),
                    "event_meta_model_event_rows_load_failed": counts_by_type.get("event_meta_model_event_rows_load_failed"),
                    "event_meta_model_price_history_load_failed": counts_by_type.get("event_meta_model_price_history_load_failed"),
                    "event_meta_model_intraday_context_load_failed": counts_by_type.get("event_meta_model_intraday_context_load_failed"),
                    "event_meta_model_macro_context_load_failed": counts_by_type.get("event_meta_model_macro_context_load_failed"),
                    "event_meta_model_exchange_context_load_failed": counts_by_type.get("event_meta_model_exchange_context_load_failed"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if exchange_features_fallbacks:
            add(
                status="warn",
                title="Exchange-event feature build used fallback behavior",
                reason=(
                    f"Exchange feature generation hit table/date/event source failures {exchange_features_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. Risk, model, and event paths may miss deal/insider/short/corporate-action context."
                ),
                commands=["python -m advisory.exchange_features --dry-run", "python -m advisory.event_data_quality --format json", "python -m advisory.operator_health --full --skip-dhan"],
                details={
                    "section": "fallback_telemetry",
                    "exchange_features_table_lookup_failed": counts_by_type.get("exchange_features_table_lookup_failed"),
                    "exchange_features_max_date_lookup_failed": counts_by_type.get("exchange_features_max_date_lookup_failed"),
                    "exchange_features_trading_days_load_failed": counts_by_type.get("exchange_features_trading_days_load_failed"),
                    "exchange_features_events_load_failed": counts_by_type.get("exchange_features_events_load_failed"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if market_context_fallbacks:
            add(
                status="warn",
                title="Market context used partial or fallback source data",
                reason=(
                    f"Market-context generation or cache reads hit source failures {market_context_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. Action market-gating and broad-context explanations may be stale or incomplete."
                ),
                commands=["python -m advisory.market_context --dry-run", "python -m advisory.operator_health --full --skip-dhan"],
                details={
                    "section": "fallback_telemetry",
                    "market_context_table_lookup_failed": counts_by_type.get("market_context_table_lookup_failed"),
                    "market_context_technical_load_failed": counts_by_type.get("market_context_technical_load_failed"),
                    "market_context_market_cap_load_failed": counts_by_type.get("market_context_market_cap_load_failed"),
                    "market_context_exchange_context_load_failed": counts_by_type.get("market_context_exchange_context_load_failed"),
                    "market_context_regime_load_failed": counts_by_type.get("market_context_regime_load_failed"),
                    "market_context_event_count_load_failed": counts_by_type.get("market_context_event_count_load_failed"),
                    "market_context_summary_cache_load_failed": counts_by_type.get("market_context_summary_cache_load_failed"),
                    "market_context_universe_cache_load_failed": counts_by_type.get("market_context_universe_cache_load_failed"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if fundamental_snapshot_fallbacks:
            add(
                status="warn",
                title="Fundamental snapshot source context failed",
                reason=(
                    f"Fundamental snapshot generation hit source lookup failures {fundamental_snapshot_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. Peer-relative fundamentals may be missing, stale, or unavailable for affected advisory runs."
                ),
                commands=["python -m advisory.fundamental_snapshot --dry-run", "python -m advisory.operator_health --full --skip-dhan"],
                details={
                    "section": "fallback_telemetry",
                    "fundamental_snapshot_universe_load_failed": counts_by_type.get("fundamental_snapshot_universe_load_failed"),
                    "fundamental_snapshot_company_master_mapping_failed": counts_by_type.get("fundamental_snapshot_company_master_mapping_failed"),
                    "fundamental_snapshot_peer_membership_load_failed": counts_by_type.get("fundamental_snapshot_peer_membership_load_failed"),
                    "fundamental_snapshot_max_date_lookup_failed": counts_by_type.get("fundamental_snapshot_max_date_lookup_failed"),
                    "fundamental_snapshot_trading_days_load_failed": counts_by_type.get("fundamental_snapshot_trading_days_load_failed"),
                    "fundamental_snapshot_release_calendar_load_failed": counts_by_type.get("fundamental_snapshot_release_calendar_load_failed"),
                    "fundamental_snapshot_income_load_failed": counts_by_type.get("fundamental_snapshot_income_load_failed"),
                    "fundamental_snapshot_balance_load_failed": counts_by_type.get("fundamental_snapshot_balance_load_failed"),
                    "fundamental_snapshot_cashflow_load_failed": counts_by_type.get("fundamental_snapshot_cashflow_load_failed"),
                    "fundamental_snapshot_shareholding_load_failed": counts_by_type.get("fundamental_snapshot_shareholding_load_failed"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if ts_forecast_fallbacks:
            add(
                status="warn",
                title="TS forecast research pipeline source data failed",
                reason=(
                    f"Experimental TS forecast generation/evaluation hit source failures {ts_forecast_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. TS Watch rows or validation summaries may be missing, stale, or incomplete."
                ),
                commands=["./all_ts_forecast_workflow.sh", "./all_ts_forecast_evaluator.sh", "python -m advisory.operator_health --full --skip-dhan"],
                details={
                    "section": "fallback_telemetry",
                    "ts_forecast_features_universe_load_failed": counts_by_type.get("ts_forecast_features_universe_load_failed"),
                    "ts_forecast_features_ohlcv_load_failed": counts_by_type.get("ts_forecast_features_ohlcv_load_failed"),
                    "ts_forecast_features_ohlcv_refresh_failed": counts_by_type.get("ts_forecast_features_ohlcv_refresh_failed"),
                    "ts_forecast_evaluator_forecast_load_failed": counts_by_type.get("ts_forecast_evaluator_forecast_load_failed"),
                    "ts_forecast_evaluator_price_window_load_failed": counts_by_type.get("ts_forecast_evaluator_price_window_load_failed"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if technical_threshold_calibration_fallbacks:
            add(
                status="warn",
                title="Technical-threshold calibration source data failed",
                reason=(
                    f"Technical-threshold calibration hit source lookup/load failures {technical_threshold_calibration_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. Threshold review output may be missing, stale, or based on incomplete realized-return evidence."
                ),
                commands=["./all_technical_threshold_calibration.sh", "python -m advisory.operator_health --full --skip-dhan"],
                details={
                    "section": "fallback_telemetry",
                    "technical_threshold_calibration_schema_lookup_failed": counts_by_type.get("technical_threshold_calibration_schema_lookup_failed"),
                    "technical_threshold_calibration_signal_rows_load_failed": counts_by_type.get("technical_threshold_calibration_signal_rows_load_failed"),
                    "technical_threshold_calibration_price_history_load_failed": counts_by_type.get("technical_threshold_calibration_price_history_load_failed"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if signal_quality_evaluator_fallbacks:
            add(
                status="warn",
                title="Signal-quality evaluator source data failed",
                reason=(
                    f"Signal-quality overlay evaluation hit source lookup/load failures {signal_quality_evaluator_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. Overlay promotion evidence may be missing, stale, or based on incomplete event/bhavcopy/company-memory context."
                ),
                commands=["python -m advisory.signal_quality_evaluator --from-date YYYY-MM-DD --to-date YYYY-MM-DD --horizons 5 10 20", "python -m advisory.operator_health --full --skip-dhan"],
                details={
                    "section": "fallback_telemetry",
                    "signal_quality_evaluator_schema_lookup_failed": counts_by_type.get("signal_quality_evaluator_schema_lookup_failed"),
                    "signal_quality_evaluator_event_policy_load_failed": counts_by_type.get("signal_quality_evaluator_event_policy_load_failed"),
                    "signal_quality_evaluator_bhavcopy_load_failed": counts_by_type.get("signal_quality_evaluator_bhavcopy_load_failed"),
                    "signal_quality_evaluator_company_memory_load_failed": counts_by_type.get("signal_quality_evaluator_company_memory_load_failed"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if promotion_review_fallbacks:
            add(
                status="warn",
                title="Promotion-review evidence source data failed",
                reason=(
                    f"Manual promotion-review evidence lookup failed {promotion_review_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. Operator review rows may be missing until evaluator summary tables are repaired or rerun."
                ),
                commands=[
                    "./all_technical_threshold_calibration.sh",
                    "python -m advisory.signal_quality_evaluator --from-date YYYY-MM-DD --to-date YYYY-MM-DD --horizons 5 10 20",
                    "python -m advisory.event_policy_evaluator --horizons 5 10 20",
                    "python -m advisory.operator_health --full --skip-dhan",
                ],
                details={
                    "section": "fallback_telemetry",
                    "technical_threshold_promotion_calibration_load_failed": counts_by_type.get("technical_threshold_promotion_calibration_load_failed"),
                    "technical_threshold_promotion_summary_load_failed": counts_by_type.get("technical_threshold_promotion_summary_load_failed"),
                    "signal_quality_promotion_summary_load_failed": counts_by_type.get("signal_quality_promotion_summary_load_failed"),
                    "event_policy_promotion_summary_load_failed": counts_by_type.get("event_policy_promotion_summary_load_failed"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if event_model_promotion_fallbacks:
            add(
                status="warn",
                title="Event-model promotion check source evidence failed",
                reason=(
                    f"Event-model promotion readiness evidence failed to load {event_model_promotion_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. Keep event-model promotion research-only until label coverage and score freshness can be verified."
                ),
                commands=[
                    "./all_ml.sh",
                    "python -m advisory.event_model_promotion_check --format text",
                    "python -m advisory.operator_health --full --skip-dhan",
                ],
                details={
                    "section": "fallback_telemetry",
                    "event_model_promotion_label_coverage_load_failed": counts_by_type.get("event_model_promotion_label_coverage_load_failed"),
                    "event_model_promotion_score_freshness_load_failed": counts_by_type.get("event_model_promotion_score_freshness_load_failed"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if macro_features_fallbacks:
            add(
                status="warn",
                title="Macro feature source data failed",
                reason=(
                    f"Macro feature generation hit source lookup/load failures {macro_features_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. Macro/regime context may be stale or unavailable for advisory and model paths."
                ),
                commands=["./complete_data.sh", "./all_advisory.sh", "python -m advisory.operator_health --full --skip-dhan"],
                details={
                    "section": "fallback_telemetry",
                    "macro_features_table_lookup_failed": counts_by_type.get("macro_features_table_lookup_failed"),
                    "macro_features_source_load_failed": counts_by_type.get("macro_features_source_load_failed"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if regime_engine_fallbacks:
            add(
                status="warn",
                title="Regime snapshot source data failed",
                reason=(
                    f"Regime generation hit benchmark or macro source lookup/load failures {regime_engine_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. Market-regime gating may be stale, partial, or unavailable."
                ),
                commands=[
                    "./complete_data.sh",
                    "python -m advisory.regime_engine --dry-run",
                    "python -m advisory.operator_health --full --skip-dhan",
                ],
                details={
                    "section": "fallback_telemetry",
                    "regime_engine_table_lookup_failed": counts_by_type.get("regime_engine_table_lookup_failed"),
                    "regime_engine_nse_benchmark_load_failed": counts_by_type.get("regime_engine_nse_benchmark_load_failed"),
                    "regime_engine_dhan_benchmark_load_failed": counts_by_type.get("regime_engine_dhan_benchmark_load_failed"),
                    "regime_engine_macro_features_load_failed": counts_by_type.get("regime_engine_macro_features_load_failed"),
                    "regime_engine_macro_daily_load_failed": counts_by_type.get("regime_engine_macro_daily_load_failed"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if technical_features_fallbacks:
            add(
                status="warn",
                title="Technical feature source data failed",
                reason=(
                    f"Technical feature generation hit universe, OHLCV, benchmark, sector, or peer source failures {technical_features_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. Rule scoring and technical explanations may be stale or unavailable."
                ),
                commands=[
                    "./complete_data.sh",
                    "python -m advisory.technical_features --dry-run --skip-peer-sync",
                    "python -m advisory.operator_health --full --skip-dhan",
                ],
                details={
                    "section": "fallback_telemetry",
                    "technical_features_universe_load_failed": counts_by_type.get("technical_features_universe_load_failed"),
                    "technical_features_nse_benchmark_load_failed": counts_by_type.get("technical_features_nse_benchmark_load_failed"),
                    "technical_features_dhan_benchmark_load_failed": counts_by_type.get("technical_features_dhan_benchmark_load_failed"),
                    "technical_features_price_history_load_failed": counts_by_type.get("technical_features_price_history_load_failed"),
                    "technical_features_sector_mapping_load_failed": counts_by_type.get("technical_features_sector_mapping_load_failed"),
                    "technical_features_peer_membership_load_failed": counts_by_type.get("technical_features_peer_membership_load_failed"),
                    "technical_features_peer_ohlcv_load_failed": counts_by_type.get("technical_features_peer_ohlcv_load_failed"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if intraday_features_fallbacks:
            add(
                status="warn",
                title="Intraday feature source data failed",
                reason=(
                    f"Intraday feature generation hit universe, coverage, Dhan sync, intraday history, or daily-reference failures {intraday_features_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. Intraday breakout/context fields may be stale or unavailable."
                ),
                commands=[
                    "./complete_data.sh",
                    "python -m advisory.intraday_features --skip-sync",
                    "python -m advisory.operator_health --full --skip-dhan",
                ],
                details={
                    "section": "fallback_telemetry",
                    "intraday_features_table_lookup_failed": counts_by_type.get("intraday_features_table_lookup_failed"),
                    "intraday_features_universe_load_failed": counts_by_type.get("intraday_features_universe_load_failed"),
                    "intraday_features_coverage_load_failed": counts_by_type.get("intraday_features_coverage_load_failed"),
                    "intraday_features_dhan_sync_failed": counts_by_type.get("intraday_features_dhan_sync_failed"),
                    "intraday_features_history_load_failed": counts_by_type.get("intraday_features_history_load_failed"),
                    "intraday_features_daily_reference_load_failed": counts_by_type.get("intraday_features_daily_reference_load_failed"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if rule_engine_fallbacks:
            add(
                status="warn",
                title="Rule engine source data failed",
                reason=(
                    f"Rule engine source lookups failed {rule_engine_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. Candidate pass/watch/rejection output may be stale, incomplete, or unavailable."
                ),
                commands=["./complete_data.sh", "./all_advisory.sh", "python -m advisory.operator_health --full --skip-dhan"],
                details={
                    "section": "fallback_telemetry",
                    "rule_engine_table_lookup_failed": counts_by_type.get("rule_engine_table_lookup_failed"),
                    "rule_engine_screener_date_lookup_failed": counts_by_type.get("rule_engine_screener_date_lookup_failed"),
                    "rule_engine_effective_dates_lookup_failed": counts_by_type.get("rule_engine_effective_dates_lookup_failed"),
                    "rule_engine_intraday_date_lookup_failed": counts_by_type.get("rule_engine_intraday_date_lookup_failed"),
                    "rule_engine_regime_load_failed": counts_by_type.get("rule_engine_regime_load_failed"),
                    "rule_engine_overlay_load_failed": counts_by_type.get("rule_engine_overlay_load_failed"),
                    "rule_engine_screener_universe_load_failed": counts_by_type.get("rule_engine_screener_universe_load_failed"),
                    "rule_engine_technical_load_failed": counts_by_type.get("rule_engine_technical_load_failed"),
                    "rule_engine_intraday_load_failed": counts_by_type.get("rule_engine_intraday_load_failed"),
                    "rule_engine_fundamentals_load_failed": counts_by_type.get("rule_engine_fundamentals_load_failed"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if portfolio_engine_fallbacks:
            add(
                status="warn",
                title="Portfolio engine source data failed",
                reason=(
                    f"Portfolio planning source lookups failed {portfolio_engine_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. Portfolio approval/defer/overlap output may be stale, incomplete, or unavailable."
                ),
                commands=["./all_advisory.sh", "python -m advisory.portfolio_engine --format json", "python -m advisory.operator_health --full --skip-dhan"],
                details={
                    "section": "fallback_telemetry",
                    "portfolio_engine_table_lookup_failed": counts_by_type.get("portfolio_engine_table_lookup_failed"),
                    "portfolio_engine_allocations_load_failed": counts_by_type.get("portfolio_engine_allocations_load_failed"),
                    "portfolio_engine_symbol_metadata_load_failed": counts_by_type.get("portfolio_engine_symbol_metadata_load_failed"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if position_lifecycle_fallbacks:
            add(
                status="warn",
                title="Position lifecycle source data failed",
                reason=(
                    f"Position lifecycle source lookups failed {position_lifecycle_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. Hold/exit/partial-exit recommendations may be stale, incomplete, or unavailable."
                ),
                commands=["./all_advisory.sh", "python -m advisory.position_lifecycle --format json", "python -m advisory.operator_health --full --skip-dhan"],
                details={
                    "section": "fallback_telemetry",
                    "position_lifecycle_table_lookup_failed": counts_by_type.get("position_lifecycle_table_lookup_failed"),
                    "position_lifecycle_open_orders_load_failed": counts_by_type.get("position_lifecycle_open_orders_load_failed"),
                    "position_lifecycle_price_identity_failed": counts_by_type.get("position_lifecycle_price_identity_failed"),
                    "position_lifecycle_price_history_load_failed": counts_by_type.get("position_lifecycle_price_history_load_failed"),
                    "position_lifecycle_technical_context_load_failed": counts_by_type.get("position_lifecycle_technical_context_load_failed"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if execution_engine_fallbacks:
            add(
                status="error" if int(counts_by_type.get("execution_live_submit_failed") or 0) or int(counts_by_type.get("execution_broker_reconcile_failed") or 0) else "warn",
                title="Execution engine broker/reconciliation path failed",
                reason=(
                    f"Execution planning, broker account, submission, or reconciliation paths failed {execution_engine_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. Broker handoff and reconciliation state may be stale, incomplete, or blocked."
                ),
                commands=["python -m advisory.execution_engine --reconcile-only --dry-run", "python -m advisory.operator_health --full --skip-dhan"],
                details={
                    "section": "fallback_telemetry",
                    "execution_broker_fund_limits_failed": counts_by_type.get("execution_broker_fund_limits_failed"),
                    "execution_broker_inventory_failed": counts_by_type.get("execution_broker_inventory_failed"),
                    "execution_recon_table_lookup_failed": counts_by_type.get("execution_recon_table_lookup_failed"),
                    "execution_recon_targets_load_failed": counts_by_type.get("execution_recon_targets_load_failed"),
                    "execution_live_submit_failed": counts_by_type.get("execution_live_submit_failed"),
                    "execution_broker_reconcile_failed": counts_by_type.get("execution_broker_reconcile_failed"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if watchlist_builder_fallbacks:
            add(
                status="warn",
                title="Watchlist builder used degraded source context",
                reason=(
                    f"Watchlist generation skipped event-transition or existing-state source context {watchlist_builder_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. Watch states may miss fresh event upgrades/downgrades or reset prior watch metadata."
                ),
                commands=["./all_advisory.sh", "python -m advisory.operator_health --full --skip-dhan"],
                details={
                    "section": "fallback_telemetry",
                    "watchlist_builder_event_transition_load_failed": counts_by_type.get("watchlist_builder_event_transition_load_failed"),
                    "watchlist_builder_existing_state_load_failed": counts_by_type.get("watchlist_builder_existing_state_load_failed"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if symbol_trace_fallbacks:
            add(
                status="warn",
                title="Symbol trace detail pages hit source read failures",
                reason=(
                    f"Symbol trace generation hit source lookup/load failures {symbol_trace_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. Symbol detail pages may be incomplete or fail until source tables recover."
                ),
                commands=["python -m advisory.symbol_trace RELIANCE --format text", "python -m advisory.operator_health --full --skip-dhan"],
                details={
                    "section": "fallback_telemetry",
                    "symbol_trace_table_lookup_failed": counts_by_type.get("symbol_trace_table_lookup_failed"),
                    "symbol_trace_stage_rows_load_failed": counts_by_type.get("symbol_trace_stage_rows_load_failed"),
                    "symbol_trace_screener_rows_load_failed": counts_by_type.get("symbol_trace_screener_rows_load_failed"),
                    "symbol_trace_rejections_load_failed": counts_by_type.get("symbol_trace_rejections_load_failed"),
                    "symbol_trace_aggregated_event_load_failed": counts_by_type.get("symbol_trace_aggregated_event_load_failed"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if setup_trace_fallbacks:
            add(
                status="warn",
                title="Setup trace funnel pages hit source read failures",
                reason=(
                    f"Setup trace generation hit source lookup/load failures {setup_trace_fallbacks} time(s) in the last "
                    f"{fallback_telemetry.get('window_hours') or 24} hours. Setup funnel pages may be incomplete or fail until source tables recover."
                ),
                commands=["python -m advisory.setup_trace LARGECAP_BREAKOUT_POSITION_V1 --format text", "python -m advisory.operator_health --full --skip-dhan"],
                details={
                    "section": "fallback_telemetry",
                    "setup_trace_table_lookup_failed": counts_by_type.get("setup_trace_table_lookup_failed"),
                    "setup_trace_asof_lookup_failed": counts_by_type.get("setup_trace_asof_lookup_failed"),
                    "setup_trace_regime_load_failed": counts_by_type.get("setup_trace_regime_load_failed"),
                    "setup_trace_overlay_load_failed": counts_by_type.get("setup_trace_overlay_load_failed"),
                    "setup_trace_screener_rows_load_failed": counts_by_type.get("setup_trace_screener_rows_load_failed"),
                    "setup_trace_column_lookup_failed": counts_by_type.get("setup_trace_column_lookup_failed"),
                    "setup_trace_stage_rows_load_failed": counts_by_type.get("setup_trace_stage_rows_load_failed"),
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        if nse_session_reset_count or nse_retry_count:
            add(
                status="warn",
                title="NSE HTTP session retries or cookie resets were recorded",
                reason=(
                    f"NSE HTTP retry/reset telemetry appeared in the last {fallback_telemetry.get('window_hours') or 24} hours "
                    f"(retries={nse_retry_count}, session_resets={nse_session_reset_count})."
                ),
                commands=["python -m advisory.operator_health --full --skip-dhan", "./complete_data.sh"],
                details={
                    "section": "fallback_telemetry",
                    "nse_retry_count": nse_retry_count,
                    "nse_session_reset_count": nse_session_reset_count,
                    "counts_by_type": fallback_telemetry.get("counts_by_type"),
                    "counts_by_module": fallback_telemetry.get("counts_by_module"),
                },
            )
        add(
            status=str(fallback_telemetry.get("status") or "warn"),
            title="Recent fallback/degraded-path events were recorded",
            reason=str(fallback_telemetry.get("message") or "Some modules used deterministic fallback, fail-soft, retry, or degraded source handling."),
            commands=["python -m advisory.operator_health --skip-dhan"],
            details={
                "window_hours": fallback_telemetry.get("window_hours"),
                "active_count": fallback_telemetry.get("active_count"),
                "error_count": fallback_telemetry.get("error_count"),
                "warn_count": fallback_telemetry.get("warn_count"),
                "counts_by_type": fallback_telemetry.get("counts_by_type"),
                "counts_by_module": fallback_telemetry.get("counts_by_module"),
            },
        )

    downloader_run_state = sections.get("downloader_run_state") if isinstance(sections.get("downloader_run_state"), dict) else {}
    if downloader_run_state.get("status") in {"warn", "error"}:
        add(
            status=str(downloader_run_state.get("status") or "warn"),
            title="Downloader/parser run-state needs attention",
            reason=str(downloader_run_state.get("error") or downloader_run_state.get("message") or "At least one standardized downloader/parser run is degraded."),
            commands=["./complete_data.sh", "python -m advisory.operator_health --skip-dhan"],
            details={
                "returned_count": downloader_run_state.get("returned_count"),
                "error_count": downloader_run_state.get("error_count"),
                "warn_count": downloader_run_state.get("warn_count"),
                "advanced_count": downloader_run_state.get("advanced_count"),
                "counts_by_classification": downloader_run_state.get("counts_by_classification"),
            },
        )

    schema_migrations = sections.get("schema_migrations") if isinstance(sections.get("schema_migrations"), dict) else {}
    if schema_migrations.get("status") in {"warn", "error"}:
        add(
            status=str(schema_migrations.get("status") or "warn"),
            title="Schema migration registry needs attention",
            reason=str(schema_migrations.get("error") or schema_migrations.get("message") or "Schema migration tracking is missing or has failed rows."),
            commands=[
                "python -m utils.schema_migrations --ensure-table",
                "python -m utils.schema_migrations --list --limit 20",
                "python -m advisory.operator_health --skip-dhan",
            ],
            details={
                "section": "schema_migrations",
                "table": schema_migrations.get("table"),
                "returned_count": schema_migrations.get("returned_count"),
                "failed_count": schema_migrations.get("failed_count"),
                "running_count": schema_migrations.get("running_count"),
            },
        )

    redis = sections.get("redis") if isinstance(sections.get("redis"), dict) else {}
    if redis.get("status") in {"warn", "error"}:
        add(
            status=str(redis.get("status") or "warn"),
            title="Redis is unavailable or not configured",
            reason=str(redis.get("error") or redis.get("message") or "Redis health check did not pass."),
            commands=["redis-server", "python -m advisory.operator_health --skip-dhan"],
            details={"host": redis.get("host"), "port": redis.get("port")},
        )

    dhan = sections.get("dhan") if isinstance(sections.get("dhan"), dict) else {}
    if dhan.get("status") == "error":
        add(
            status="error",
            title="Dhan token validation failed",
            reason=str(dhan.get("error") or dhan.get("message") or "Dhan profile call failed."),
            commands=["python -m data.dhanlive.auth", "python -m advisory.operator_health"],
            details={"section": "dhan"},
        )
    elif dhan.get("status") == "warn":
        add(
            status="warn",
            title="Dhan check is skipped or token is missing",
            reason=str(dhan.get("message") or "Dhan health check is not validating the broker token."),
            commands=["python -m advisory.operator_health", "python -m data.dhanlive.auth"],
            details={"section": "dhan"},
        )

    dhan_cache = sections.get("dhan_cache") if isinstance(sections.get("dhan_cache"), dict) else {}
    if dhan_cache.get("status") in {"warn", "error"}:
        auto_login_configured = bool(dhan_cache.get("auto_login_configured"))
        auth_refresh_ready = bool(dhan_cache.get("auth_refresh_ready"))
        cdp_status = dhan_cache.get("cdp_status") if isinstance(dhan_cache.get("cdp_status"), dict) else {}
        cdp_unready = auto_login_configured and not auth_refresh_ready and cdp_status.get("status") != "ok"
        commands = ["python -m data.dhanlive.auth_cli status"]
        if cdp_unready:
            commands.append("scripts/start_chrome_cdp.sh")
        commands.extend(
            [
                "python -m data.dhanlive.auth_cli ensure --auto-login",
                "python -m advisory.operator_health --skip-dhan",
            ]
        )
        add(
            status=str(dhan_cache.get("status") or "warn"),
            title="Dhan cached token needs attention",
            reason=str(dhan_cache.get("message") or "Dhan cached token health check did not pass."),
            commands=commands,
            details={
                "cache_path": dhan_cache.get("cache_path"),
                "expires_at": dhan_cache.get("expires_at"),
                "seconds_to_expiry": dhan_cache.get("seconds_to_expiry"),
                "cache_age_seconds": dhan_cache.get("cache_age_seconds"),
                "auth_refresh_ready": auth_refresh_ready,
                "auto_login_configured": auto_login_configured,
                "cdp_status": cdp_status or dhan_cache.get("cdp_status"),
                "cdp_recovery_required": cdp_unready,
            },
        )

    for row in sections.get("table_freshness") or []:
        if not isinstance(row, dict) or row.get("status") == "ok":
            continue
        table = str(row.get("table") or row.get("name") or "unknown")
        name = str(row.get("name") or table)
        command_map = {
            "dhan_daily": "./complete_data.sh",
            "actions": "./all_advisory.sh",
            "portfolio": "./all_advisory.sh",
            "watch_alerts": "./all_watchers.sh",
            "event_policy": "python -m advisory.event_policy_evaluator --horizons 5 10 20",
            "market_context": "./all_advisory.sh",
            "ts_forecasts": "python -m advisory.ts_forecast_workflow",
            "ts_forecast_paper": "python -m advisory.ts_forecast_paper_portfolio --log-research-ledger",
            "sync_state": "./complete_data.sh",
            "signal_quality": "python -m advisory.signal_quality_evaluator --from-date YYYY-MM-DD --to-date YYYY-MM-DD --horizons 5 10 20",
            "context_overlay_reliability": "python -m advisory.context_overlay_reliability_report --horizons 5 10 20 --format json",
        }
        add(
            status=str(row.get("status") or "warn"),
            title=f"{name} data is stale or missing",
            reason=str(row.get("error") or row.get("message") or "Freshness check failed."),
            commands=[command_map.get(name, "./complete_data.sh"), "python -m advisory.operator_health --skip-dhan"],
            details={"table": table, "latest_at": row.get("latest_at"), "age_hours": row.get("age_hours"), "row_count": row.get("row_count")},
        )

    for row in sections.get("cron_logs") or []:
        if not isinstance(row, dict) or row.get("status") == "ok":
            continue
        log_file = str(row.get("log_file") or "logs/cron")
        log_name = Path(log_file).name
        recent_errors = row.get("recent_errors") if isinstance(row.get("recent_errors"), list) else []
        recovered = row.get("latest_run_status") in {"ok_after_historical_errors", "recovered_after_manual_interrupt"} and not recent_errors
        commands = [f"tail -100 {log_file}", "python -m advisory.operator_health --skip-dhan"]
        if log_name == "all_advisory_preflight.log" and not recovered:
            commands = [
                f"tail -100 {log_file}",
                "scripts/start_chrome_cdp.sh",
                "./all_advisory_preflight.sh",
                "python -m advisory.operator_health --skip-dhan",
            ]
        add(
            status=str(row.get("status") or "warn"),
            title=f"Historical cron errors recovered in {log_name}" if recovered else f"Recent cron errors in {log_name}",
            reason=str(row.get("message") if recovered else recent_errors[-1] if recent_errors else row.get("message")),
            commands=commands,
            details={
                "log_file": log_file,
                "latest_run_status": row.get("latest_run_status"),
                "recent_error_count": row.get("recent_error_count"),
                "historical_error_count": row.get("historical_error_count"),
                "preflight_recovery": log_name == "all_advisory_preflight.log" and not recovered,
            },
        )

    for row in sections.get("sync_state_failures") or []:
        if not isinstance(row, dict) or row.get("status") == "ok":
            continue
        add(
            status=str(row.get("status") or "warn"),
            title=f"Sync cycle issue: {row.get('source_name') or 'unknown'}",
            reason=str(row.get("error") or row.get("message") or "Sync-state row is not OK."),
            commands=["./all_watchers.sh", "python -m advisory.operator_health --skip-dhan"],
            details={"source_name": row.get("source_name"), "sync_status": row.get("sync_status"), "updated_at": row.get("updated_at")},
        )

    for row in sections.get("optional_dependencies") or []:
        if not isinstance(row, dict) or row.get("status") == "ok":
            continue
        message = str(row.get("message") or "")
        module = str(row.get("module") or "")
        if "Poppler" in message:
            commands = ["brew install poppler", "python -m advisory.operator_health --skip-dhan"]
        elif module == "timesfm":
            commands = ["python builder.py", "python -m advisory.operator_health --skip-dhan"]
        elif module == "torch":
            commands = ["python -m pip install torch", "python -m advisory.operator_health --skip-dhan"]
        elif "Node/npm" in message:
            commands = ["nvm use default", "cd apps/operator-web && npm install"]
        elif "Codex" in message:
            commands = ["which codex", "export CODEX_CLI_BIN=/path/to/codex"]
        else:
            commands = ["python -m advisory.operator_health --skip-dhan"]
        add(
            status=str(row.get("status") or "warn"),
            title=message or f"{module} dependency needs attention",
            reason=message or "Optional dependency check did not pass.",
            commands=commands,
            details={"module": module},
        )

    degradation_feed = sections.get("degradation_feed") if isinstance(sections.get("degradation_feed"), dict) else {}
    for row in degradation_feed.get("rows") or []:
        if not isinstance(row, dict) or row.get("recovered"):
            continue
        kind = str(row.get("kind") or "")
        symbol = str(row.get("symbol") or "").strip().upper()
        if kind == "dhan_master_miss":
            commands = ["python -m data.dhanlive.scrip_master", f"python -m data.dhanlive.ohlcv --symbol {symbol}" if symbol else "python -m data.dhanlive.ohlcv", "python -m advisory.operator_health --skip-dhan"]
        elif kind == "announcement_document_failure":
            commands = ["brew install poppler", "python -m data.announcements.cli", "python -m advisory.operator_health --skip-dhan"]
        elif kind == "slow_operation":
            commands = ["python -m advisory.performance_slowlog report --limit 20"]
        elif kind == "trace_summary_cache_miss":
            commands = ["python -m advisory.trace_summary_store --symbol-limit 150 --event-limit 150 --trace-limit 100", "python -m advisory.operator_health --skip-dhan"]
        else:
            commands = ["python -m advisory.operator_health --skip-dhan"]
        add(
            status=str(row.get("status") or row.get("severity") or "warn"),
            title=str(row.get("title") or "Runtime degradation detected"),
            reason=str(row.get("message") or row.get("suggested_fix") or "A fallback/error marker was found."),
            commands=commands,
            details={key: row.get(key) for key in ["kind", "source", "symbol", "unique_id", "observed_at", "suggested_fix"] if row.get(key) is not None},
        )

    frontend = sections.get("frontend") if isinstance(sections.get("frontend"), dict) else {}
    if frontend.get("status") in {"warn", "error"}:
        add(
            status=str(frontend.get("status") or "warn"),
            title="Operator frontend dependencies need attention",
            reason=str(frontend.get("error") or frontend.get("message") or "Frontend dependency check did not pass."),
            commands=["nvm use default", "cd apps/operator-web && npm install", "./all_frontend.sh"],
            details={"node_modules": frontend.get("node_modules"), "node_version": frontend.get("node_version"), "npm_version": frontend.get("npm_version")},
        )

    frontend_runtime = sections.get("frontend_runtime") if isinstance(sections.get("frontend_runtime"), dict) else {}
    if frontend_runtime.get("status") in {"warn", "error"}:
        add(
            status=str(frontend_runtime.get("status") or "error"),
            title="Operator frontend is not reachable",
            reason=str(frontend_runtime.get("error") or frontend_runtime.get("message") or "Frontend runtime check did not pass."),
            commands=["./all_frontend.sh", "python -m advisory.operator_health --skip-dhan"],
            details={
                "url": frontend_runtime.get("url"),
                "latency_ms": frontend_runtime.get("latency_ms"),
                "status_code": frontend_runtime.get("status_code"),
            },
        )

    if not hints:
        add(
            status="ok",
            title="No active fix hints",
            reason="All operator health sections are currently OK.",
            commands=["python -m advisory.operator_health --skip-dhan"],
        )
    return hints


TRUST_BLOCKER_SECTION_TITLES = {
    "database": "Postgres health",
    "operator_api": "Operator API health",
    "trace_summaries": "Trace summary cache",
    "operator_snapshot": "Operator snapshot freshness",
    "slow_operations": "Slow operator paths",
    "event_data_quality": "Announcement/bhavcopy evidence readiness",
    "identity_issues": "Security identity readiness",
    "screener_failures": "Screener.in failure readiness",
    "context_gate_policy": "Context/regime gate policy",
    "signal_quality": "Signal-quality evidence readiness",
    "technical_threshold_evidence": "Technical-threshold calibration evidence readiness",
    "research_evidence_run_summary": "Research evidence refresh readiness",
    "ts_forecast_paper": "TS forecast paper evidence readiness",
    "llm_provenance_audit": "LLM provenance audit readiness",
    "provenance_graph_dry_runs": "Typed provenance graph readiness",
    "causal_event_memory_evidence": "Causal event-memory evidence readiness",
    "action_transition_evidence": "Action-transition evidence readiness",
    "event_policy_evidence": "Event-policy evidence readiness",
    "context_watch_evidence": "Context-watch evidence readiness",
    "negative_pressure_evidence": "Negative-pressure evidence readiness",
    "adversarial_review_evidence": "Adversarial-review evidence readiness",
    "signal_quality_split_evidence": "Signal-quality narrowed split evidence readiness",
    "context_overlay_reliability": "Context-overlay reliability readiness",
    "signal_quality_overlay_rules": "Trusted signal-quality overlay rule eligibility",
    "feature_stage_gates": "Feature freshness stage gates",
    "fallback_telemetry": "Fallback telemetry",
    "trust_gate": "Advisory trust gate",
    "redis": "Redis runtime state",
    "dhan": "Dhan token validation",
    "dhan_cache": "Dhan token cache",
    "frontend": "Operator frontend dependencies",
    "frontend_runtime": "Operator frontend runtime",
    "degradation_feed": "Runtime degradation feed",
}


def _blocker_category(row: dict[str, Any]) -> str:
    details = row.get("details") if isinstance(row.get("details"), dict) else {}
    section = str(details.get("section") or "").lower()
    kind = str(details.get("kind") or "").lower()
    title = str(row.get("title") or "").lower()
    if section in {"database", "operator_api", "frontend_runtime"} or "postgres" in title or "operator api" in title or "operator frontend" in title:
        return "runtime"
    if section == "context_gate_policy" or "broad regime hard gate" in title:
        return "advisory_trust"
    if section in {"dhan", "dhan_cache"} or kind.startswith("dhan") or "dhan" in title:
        return "broker_data"
    if section == "identity_issues" or "identity" in title:
        return "broker_data"
    if section in {
        "signal_quality",
        "technical_threshold_evidence",
        "research_evidence_run_summary",
        "ts_forecast_paper",
        "llm_provenance_audit",
        "provenance_graph_dry_runs",
        "causal_event_memory_evidence",
        "action_transition_evidence",
        "event_policy_evidence",
        "context_watch_evidence",
        "negative_pressure_evidence",
        "adversarial_review_evidence",
        "signal_quality_split_evidence",
        "context_overlay_reliability",
        "signal_quality_overlay_rules",
    }:
        return "research_evidence"
    if (
        "signal-quality" in title
        or "technical-threshold" in title
        or "research evidence refresh" in title
        or "ts forecast paper" in title
        or "llm provenance" in title
        or "provenance graph" in title
        or "causal event-memory" in title
        or "action-transition" in title
        or "adversarial-review" in title
        or "narrowed split" in title
        or "context-overlay reliability" in title
    ):
        return "research_evidence"
    if section == "trust_gate":
        return "advisory_trust"
    if section in {"operator_snapshot", "trace_summaries"} or "snapshot" in title or "trace" in title:
        return "operator_visibility"
    if section == "feature_stage_gates" or "stage gate" in title:
        return "data_freshness"
    if "stale" in title or "freshness" in title or "data is stale" in title:
        return "data_freshness"
    if "cron" in title or "sync" in title:
        return "pipeline"
    if kind or "fallback" in title or "degradation" in title or "ocr" in title:
        return "data_quality"
    return "operations"


def _trust_impact(row: dict[str, Any]) -> str:
    category = _blocker_category(row)
    status = str(row.get("status") or "").lower()
    if category in {"runtime", "operator_visibility"}:
        return "Operator cannot trust the advisory UI until this is cleared."
    if category == "broker_data":
        return "Broker/security identity or token state may block validation and execution planning."
    if category == "data_freshness":
        return "Advisory decisions may be using stale or missing inputs."
    if category == "pipeline":
        return "Recent pipeline runs may have failed or skipped required updates."
    if category == "data_quality":
        return "Some evidence may be fallback, partial, or unresolved."
    if category == "research_evidence":
        return "Research evidence is not strong enough to promote overlays or thresholds."
    if category == "advisory_trust":
        return "Use this to decide whether today’s recommendations are usable, review-only, or blocked."
    if status == "error":
        return "A required health check is failing."
    return "This warning should be triaged before relying on fresh advisory output."


def _blocker_dedupe_key(row: dict[str, Any]) -> tuple[str, str]:
    details = row.get("details") if isinstance(row.get("details"), dict) else {}
    category = str(row.get("category") or _blocker_category(row) or "unknown")
    section = str(details.get("section") or "").strip().lower()
    kind = str(details.get("kind") or "").strip().lower()
    title = str(row.get("title") or "").strip().lower()
    source = str(row.get("source") or "").strip().lower()
    path = str(details.get("path") or "").strip().lower()

    if section == "dhan_cache" or "dhan cached token" in title or title == "dhan token cache":
        return ("health_source", "dhan_cache")
    if section == "dhan" or "dhan check is skipped" in title or title == "dhan token validation":
        return ("health_source", "dhan_token_validation")
    if section == "api_latency_probe" or kind == "api_latency_probe" or "api latency probe" in title or "api_latency_probe" in path:
        return ("health_source", "api_latency_probe")
    if section == "slow_operations" or kind == "slow_operation" or "slow-operation" in title or title == "slow operator paths":
        return ("health_source", "slow_operations")
    if section == "advisory_stage_report" or "advisory stage timing" in title:
        return ("health_source", "advisory_stage_report")
    if section == "research_evidence_run_summary" or "research evidence refresh" in title:
        return ("health_source", "research_evidence_run_summary")
    if section:
        return (category, section)
    if kind:
        return (category, kind)
    if source and source != "fix_hint":
        return (category, source)
    return (category, title)


def build_trust_gate(sections: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add_check(
        key: str,
        status: str,
        title: str,
        reason: str,
        *,
        impact: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        checks.append(
            {
                "key": key,
                "status": status,
                "title": title,
                "reason": reason,
                "impact": impact,
                "details": details or {},
            }
        )

    def section(name: str) -> dict[str, Any]:
        value = sections.get(name)
        return value if isinstance(value, dict) else {}

    for name, title, impact in [
        ("database", "Postgres is reachable", "Without DB access, advisory state cannot be trusted."),
        ("operator_api", "Operator API is reachable", "Without API access, UI state may be stale or incomplete."),
        ("operator_api_runtime", "Operator API runtime is current", "If the API process is stale, the UI may serve fixed bugs as active failures."),
        ("frontend_runtime", "Operator frontend is reachable", "Without frontend access, manual review and operator controls may be unavailable."),
        ("dhan", "Dhan token validates", "Without broker data access, prices/identity/execution planning may be stale."),
    ]:
        row = section(name)
        if row.get("status") == "error":
            add_check(name, "error", title, str(row.get("error") or row.get("message") or "Check failed."), impact=impact, details=row)

    snapshot = section("operator_snapshot")
    if snapshot.get("status") in {"warn", "error"}:
        add_check(
            "operator_snapshot",
            str(snapshot.get("status") or "warn"),
            "Operator snapshot freshness",
            str(snapshot.get("error") or snapshot.get("message") or "Operator snapshot is stale or unavailable."),
            impact="The UI may be serving stale advisory actions, portfolio rows, prices, targets, or reasons.",
            details=snapshot,
        )

    freshness_rows = sections.get("table_freshness") if isinstance(sections.get("table_freshness"), list) else []
    required_freshness = {"dhan_daily", "actions", "event_policy", "operator_snapshot"}
    for row in freshness_rows:
        if not isinstance(row, dict) or row.get("status") == "ok":
            continue
        name = str(row.get("name") or "")
        if name in required_freshness:
            add_check(
                f"freshness:{name}",
                str(row.get("status") or "warn"),
                f"{name.replace('_', ' ').title()} freshness",
                str(row.get("message") or "Input is stale or missing."),
                impact="Advisory may be using stale or missing core inputs.",
                details=row,
            )

    event_quality = section("event_data_quality")
    if event_quality.get("status") in {"warn", "error"}:
        event_summary = event_quality.get("summary") if isinstance(event_quality.get("summary"), dict) else {}
        add_check(
            "event_data_quality",
            str(event_quality.get("status") or "warn"),
            "Announcement/bhavcopy evidence readiness",
            str(event_quality.get("message") or "Event evidence quality gate found issues."),
            impact="Event-driven decisions may miss or misinterpret evidence.",
            details={
                "summary": event_summary,
                "issue_count": event_summary.get("issue_count"),
                "error_count": event_summary.get("error_count"),
                "warn_count": event_summary.get("warn_count"),
                "llm_signal_authority": event_summary.get("llm_signal_authority"),
            },
        )

    identity = section("identity_issues")
    active_action_coverage = identity.get("active_action_identity_coverage") if isinstance(identity.get("active_action_identity_coverage"), dict) else {}
    if int(identity.get("open_count") or 0) > 0 or int(active_action_coverage.get("missing_count") or 0) > 0 or identity.get("status") == "error":
        add_check(
            "identity_issues",
            "warn" if identity.get("status") != "error" else "error",
            "Open security identity issues",
            str(identity.get("message") or "Some symbols cannot be mapped cleanly."),
            impact="Affected symbols may be skipped from Dhan prices or broker execution planning.",
            details={
                "open_count": identity.get("open_count"),
                "rows": (identity.get("rows") or [])[:5],
                "active_action_identity_coverage": active_action_coverage,
            },
        )

    screener_failures = section("screener_failures")
    if int(screener_failures.get("active_count") or 0) > 0 or screener_failures.get("status") == "error":
        add_check(
            "screener_failures",
            "warn" if screener_failures.get("status") != "error" else "error",
            "Recent Screener.in failures",
            str(screener_failures.get("message") or "Screener.in query/fetch/parse failures were recorded."),
            impact="Candidate universe generation may be incomplete until the failing Screener query or login/session issue is fixed.",
            details={
                "active_count": screener_failures.get("active_count"),
                "validation_count": screener_failures.get("validation_count"),
                "fetch_count": screener_failures.get("fetch_count"),
                "parse_count": screener_failures.get("parse_count"),
                "rows": (screener_failures.get("rows") or [])[:5],
            },
        )

    context_gate_policy = section("context_gate_policy")
    if context_gate_policy.get("status") in {"warn", "error"}:
        add_check(
            "context_gate_policy",
            str(context_gate_policy.get("status") or "warn"),
            "Context/regime gate policy",
            str(context_gate_policy.get("operator_action") or context_gate_policy.get("message") or "A broad single-regime hard gate is active."),
            impact=(
                "Missing BUY recommendations may be caused by a broad regime-label hard gate; verify env policy before tuning "
                "technical thresholds or context-overlay rules."
            ),
            details={
                "reasons": context_gate_policy.get("reasons"),
                "active_single_regime_flags": context_gate_policy.get("active_single_regime_flags"),
                "active_context_hard_flags": context_gate_policy.get("active_context_hard_flags"),
                "global_regime_label_blocks_buy": bool(context_gate_policy.get("global_regime_label_blocks_buy")),
                "policy_summary": context_gate_policy.get("policy_summary"),
                "broker_execution_allowed": bool(context_gate_policy.get("broker_execution_allowed")),
                "portfolio_authority": context_gate_policy.get("portfolio_authority") or "none",
            },
        )

    signal_quality = section("signal_quality")
    if signal_quality.get("status") in {"warn", "error"}:
        add_check(
            "signal_quality",
            str(signal_quality.get("status") or "warn"),
            "Signal-quality evidence readiness",
            str(signal_quality.get("message") or "Signal-quality run is not usable for promotion decisions."),
            impact="Do not promote overlays/thresholds from this evidence yet.",
            details={
                "reasons": signal_quality.get("reasons"),
                "latest_evaluated_at": signal_quality.get("latest_evaluated_at"),
                "max_matured_rows": signal_quality.get("max_matured_rows"),
                "overlay_rows": signal_quality.get("overlay_rows"),
                "benchmark_beta_not_overlay_alpha_count": signal_quality.get("benchmark_beta_not_overlay_alpha_count"),
                "needs_benchmark_attribution_count": signal_quality.get("needs_benchmark_attribution_count"),
                "benchmark_or_attribution_blocked_count": signal_quality.get("benchmark_or_attribution_blocked_count"),
                "promotion_readiness_status": signal_quality.get("promotion_readiness_status"),
                "promotion_review_sector_block_count": signal_quality.get("promotion_review_sector_block_count"),
                "promotion_review_runtime_block_count": signal_quality.get("promotion_review_runtime_block_count"),
                "promotion_review_harmful_class_block_count": signal_quality.get("promotion_review_harmful_class_block_count"),
            },
        )

    technical_threshold_evidence = section("technical_threshold_evidence")
    if technical_threshold_evidence.get("status") in {"warn", "error"}:
        add_check(
            "technical_threshold_evidence",
            str(technical_threshold_evidence.get("status") or "warn"),
            "Technical-threshold calibration evidence readiness",
            str(technical_threshold_evidence.get("message") or "Technical-threshold calibration is missing, stale, bounded, or lacks after-cost lift over baseline."),
            impact=(
                "Do not loosen or promote technical thresholds until calibration shows fresh matured after-cost evidence with lift over baseline."
            ),
            details={
                "reasons": technical_threshold_evidence.get("reasons"),
                "evaluation_rows": technical_threshold_evidence.get("evaluation_rows"),
                "summary_rows": technical_threshold_evidence.get("summary_rows"),
                "max_matured_signal_count": technical_threshold_evidence.get("max_matured_signal_count"),
                "review_candidate_count": technical_threshold_evidence.get("review_candidate_count"),
                "lift_over_baseline_count": technical_threshold_evidence.get("lift_over_baseline_count"),
                "positive_after_cost_count": technical_threshold_evidence.get("positive_after_cost_count"),
                "bounded_first_pass_count": technical_threshold_evidence.get("bounded_first_pass_count"),
                "trigger_near_miss_candidate_count": technical_threshold_evidence.get("trigger_near_miss_candidate_count"),
                "trigger_near_miss_do_not_relax_count": technical_threshold_evidence.get("trigger_near_miss_do_not_relax_count"),
                "trigger_near_miss_needs_more_label_count": technical_threshold_evidence.get("trigger_near_miss_needs_more_label_count"),
                "missing_summary_columns": technical_threshold_evidence.get("missing_summary_columns"),
                "latest_evaluated_at": technical_threshold_evidence.get("latest_evaluated_at"),
                "age_days": technical_threshold_evidence.get("age_days"),
                "broker_execution_allowed": bool(technical_threshold_evidence.get("broker_execution_allowed")),
                "policy_auto_promotion_allowed": bool(technical_threshold_evidence.get("policy_auto_promotion_allowed")),
            },
        )

    research_evidence_summary = section("research_evidence_run_summary")
    if research_evidence_summary.get("status") in {"warn", "error"}:
        add_check(
            "research_evidence_run_summary",
            str(research_evidence_summary.get("status") or "warn"),
            "Research evidence refresh readiness",
            str(research_evidence_summary.get("message") or "The daily research-evidence wrapper is missing, failed, skipped, or not ready."),
            impact=(
                "Do not promote context, event, transition, or LLM-derived overlays until the consolidated research refresh is healthy."
            ),
            details={
                "reasons": research_evidence_summary.get("reasons"),
                "log_path": research_evidence_summary.get("log_path"),
                "readiness_status": research_evidence_summary.get("readiness_status"),
                "payload_status": research_evidence_summary.get("payload_status"),
                "active_component_count": research_evidence_summary.get("active_component_count"),
                "candidate_component_count": research_evidence_summary.get("candidate_component_count"),
                "blocker_count": research_evidence_summary.get("blocker_count"),
                "authority_violation_count": research_evidence_summary.get("authority_violation_count"),
                "authority_violation_components": research_evidence_summary.get("authority_violation_components"),
                "broker_execution_allowed": bool(research_evidence_summary.get("broker_execution_allowed")),
                "policy_auto_promotion_allowed": bool(research_evidence_summary.get("policy_auto_promotion_allowed")),
                "portfolio_mutation_allowed": bool(research_evidence_summary.get("portfolio_mutation_allowed")),
            },
        )

    ts_forecast_paper = section("ts_forecast_paper")
    if ts_forecast_paper.get("status") in {"warn", "error"}:
        add_check(
            "ts_forecast_paper",
            str(ts_forecast_paper.get("status") or "warn"),
            "TS forecast paper evidence readiness",
            str(ts_forecast_paper.get("message") or "TS forecast paper-portfolio evidence is not usable for promotion review."),
            impact="Do not promote TS forecast model rules or rely on TS forecast promotion checks until paper outcomes are available and matured.",
            details={
                "reasons": ts_forecast_paper.get("reasons"),
                "latest_load_ts": ts_forecast_paper.get("latest_load_ts"),
                "latest_asof_date": ts_forecast_paper.get("latest_asof_date"),
                "row_count": ts_forecast_paper.get("row_count"),
                "matured_count": ts_forecast_paper.get("matured_count"),
                "paper_buy_count": ts_forecast_paper.get("paper_buy_count"),
                "symbol_count": ts_forecast_paper.get("symbol_count"),
                "broker_execution_allowed": bool(ts_forecast_paper.get("broker_execution_allowed")),
                "policy_auto_promotion_allowed": bool(ts_forecast_paper.get("policy_auto_promotion_allowed")),
            },
        )

    llm_provenance = section("llm_provenance_audit")
    if llm_provenance.get("status") in {"warn", "error"}:
        add_check(
            "llm_provenance_audit",
            str(llm_provenance.get("status") or "warn"),
            "LLM provenance audit readiness",
            str(llm_provenance.get("message") or "LLM/Codex provenance audit found prompt/schema/evidence/authority issues."),
            impact="Treat affected LLM-derived signals as not production-auditable until prompt, schema, evidence, and authority metadata are repaired.",
            details={
                "reasons": llm_provenance.get("reasons"),
                "issue_table_count": llm_provenance.get("issue_table_count"),
                "table_count": llm_provenance.get("table_count"),
                "lookback_days": llm_provenance.get("lookback_days"),
                "issue_rows": llm_provenance.get("issue_rows"),
                "broker_execution_allowed": bool(llm_provenance.get("broker_execution_allowed")),
                "policy_auto_promotion_allowed": bool(llm_provenance.get("policy_auto_promotion_allowed")),
                "repairs_metadata": bool(llm_provenance.get("repairs_metadata")),
            },
        )

    provenance_graphs = section("provenance_graph_dry_runs")
    if provenance_graphs.get("status") in {"warn", "error"}:
        add_check(
            "provenance_graph_dry_runs",
            str(provenance_graphs.get("status") or "warn"),
            "Typed provenance graph readiness",
            str(provenance_graphs.get("message") or "Action-evidence or causal-event provenance dry runs are not producing complete audit lineage."),
            impact="Treat affected action/context evidence as harder to audit until the provenance dry runs produce lineage rows.",
            details={
                "reasons": provenance_graphs.get("reasons"),
                "row_count": provenance_graphs.get("row_count"),
                "error_count": provenance_graphs.get("error_count"),
                "warn_count": provenance_graphs.get("warn_count"),
                "rows": provenance_graphs.get("rows"),
                "broker_execution_allowed": bool(provenance_graphs.get("broker_execution_allowed")),
                "policy_auto_promotion_allowed": bool(provenance_graphs.get("policy_auto_promotion_allowed")),
                "persists_rows": bool(provenance_graphs.get("persists_rows")),
            },
        )

    causal_memory = section("causal_event_memory_evidence")
    if causal_memory.get("status") in {"warn", "error"}:
        add_check(
            "causal_event_memory_evidence",
            str(causal_memory.get("status") or "warn"),
            "Causal event-memory evidence readiness",
            str(causal_memory.get("message") or "Causal event-memory evidence is missing, stale, or lacks matured labels."),
            impact="Keep causal memory as explanation-only until point-in-time realized outcome labels are fresh and sufficiently matured.",
            details={
                "reasons": causal_memory.get("reasons"),
                "evaluation_rows": causal_memory.get("evaluation_rows"),
                "matured_rows": causal_memory.get("matured_rows"),
                "summary_rows": causal_memory.get("summary_rows"),
                "candidate_group_count": causal_memory.get("candidate_group_count"),
                "harmful_group_count": causal_memory.get("harmful_group_count"),
                "latest_evaluated_at": causal_memory.get("latest_evaluated_at"),
                "age_days": causal_memory.get("age_days"),
                "broker_execution_allowed": bool(causal_memory.get("broker_execution_allowed")),
                "policy_auto_promotion_allowed": bool(causal_memory.get("policy_auto_promotion_allowed")),
            },
        )

    action_transition = section("action_transition_evidence")
    if action_transition.get("status") in {"warn", "error"}:
        add_check(
            "action_transition_evidence",
            str(action_transition.get("status") or "warn"),
            "Action-transition evidence readiness",
            str(action_transition.get("message") or "Action-transition evidence is missing, stale, beta-only, or lacks matured labels."),
            impact=(
                "Keep transition-stability evidence research-only until point-in-time labels show both absolute and benchmark-excess usefulness."
            ),
            details={
                "reasons": action_transition.get("reasons"),
                "evaluation_rows": action_transition.get("evaluation_rows"),
                "matured_rows": action_transition.get("matured_rows"),
                "summary_rows": action_transition.get("summary_rows"),
                "candidate_group_count": action_transition.get("candidate_group_count"),
                "benchmark_beta_not_transition_alpha_count": action_transition.get("benchmark_beta_not_transition_alpha_count"),
                "needs_benchmark_attribution_count": action_transition.get("needs_benchmark_attribution_count"),
                "latest_evaluated_at": action_transition.get("latest_evaluated_at"),
                "age_days": action_transition.get("age_days"),
                "broker_execution_allowed": bool(action_transition.get("broker_execution_allowed")),
                "policy_auto_promotion_allowed": bool(action_transition.get("policy_auto_promotion_allowed")),
            },
        )

    event_policy_evidence = section("event_policy_evidence")
    if event_policy_evidence.get("status") in {"warn", "error"}:
        add_check(
            "event_policy_evidence",
            str(event_policy_evidence.get("status") or "warn"),
            "Event-policy evidence readiness",
            str(event_policy_evidence.get("message") or "Event-policy evidence is missing, stale, beta-only, or lacks matured labels."),
            impact=(
                "Keep event-policy LLM/classification influence research-only until point-in-time labels show both absolute and benchmark-excess usefulness."
            ),
            details={
                "reasons": event_policy_evidence.get("reasons"),
                "evaluation_rows": event_policy_evidence.get("evaluation_rows"),
                "matured_rows": event_policy_evidence.get("matured_rows"),
                "summary_rows": event_policy_evidence.get("summary_rows"),
                "candidate_strengthen_count": event_policy_evidence.get("candidate_strengthen_count"),
                "candidate_tighten_or_downgrade_count": event_policy_evidence.get("candidate_tighten_or_downgrade_count"),
                "benchmark_beta_not_policy_alpha_count": event_policy_evidence.get("benchmark_beta_not_policy_alpha_count"),
                "needs_benchmark_attribution_count": event_policy_evidence.get("needs_benchmark_attribution_count"),
                "missing_benchmark_columns": event_policy_evidence.get("missing_benchmark_columns"),
                "latest_evaluated_at": event_policy_evidence.get("latest_evaluated_at"),
                "age_days": event_policy_evidence.get("age_days"),
                "broker_execution_allowed": bool(event_policy_evidence.get("broker_execution_allowed")),
                "policy_auto_promotion_allowed": bool(event_policy_evidence.get("policy_auto_promotion_allowed")),
            },
        )

    context_watch_evidence = section("context_watch_evidence")
    if context_watch_evidence.get("status") in {"warn", "error"}:
        add_check(
            "context_watch_evidence",
            str(context_watch_evidence.get("status") or "warn"),
            "Context-watch evidence readiness",
            str(context_watch_evidence.get("message") or "Context-watch evidence is missing, stale, beta-only, or lacks matured labels."),
            impact=(
                "Keep context-overlay watch priority research-only until point-in-time labels show benchmark-excess opportunity value."
            ),
            details={
                "reasons": context_watch_evidence.get("reasons"),
                "evaluation_rows": context_watch_evidence.get("evaluation_rows"),
                "matured_rows": context_watch_evidence.get("matured_rows"),
                "summary_rows": context_watch_evidence.get("summary_rows"),
                "opportunity_candidate_count": context_watch_evidence.get("opportunity_candidate_count"),
                "harmful_watch_noise_count": context_watch_evidence.get("harmful_watch_noise_count"),
                "benchmark_beta_not_watch_alpha_count": context_watch_evidence.get("benchmark_beta_not_watch_alpha_count"),
                "missing_benchmark_columns": context_watch_evidence.get("missing_benchmark_columns"),
                "latest_evaluated_at": context_watch_evidence.get("latest_evaluated_at"),
                "age_days": context_watch_evidence.get("age_days"),
                "broker_execution_allowed": bool(context_watch_evidence.get("broker_execution_allowed")),
                "policy_auto_promotion_allowed": bool(context_watch_evidence.get("policy_auto_promotion_allowed")),
            },
        )

    negative_pressure_evidence = section("negative_pressure_evidence")
    if negative_pressure_evidence.get("status") in {"warn", "error"}:
        add_check(
            "negative_pressure_evidence",
            str(negative_pressure_evidence.get("status") or "warn"),
            "Negative-pressure evidence readiness",
            str(negative_pressure_evidence.get("message") or "Negative-pressure evidence is missing, stale, beta-only, or lacks matured labels."),
            impact=(
                "Keep context-overlay de-risk pressure research-only until point-in-time labels show benchmark-excess protection."
            ),
            details={
                "reasons": negative_pressure_evidence.get("reasons"),
                "evaluation_rows": negative_pressure_evidence.get("evaluation_rows"),
                "matured_rows": negative_pressure_evidence.get("matured_rows"),
                "summary_rows": negative_pressure_evidence.get("summary_rows"),
                "protective_candidate_count": negative_pressure_evidence.get("protective_candidate_count"),
                "benchmark_beta_not_derisk_alpha_count": negative_pressure_evidence.get("benchmark_beta_not_derisk_alpha_count"),
                "harmful_false_positive_count": negative_pressure_evidence.get("harmful_false_positive_count"),
                "missing_benchmark_columns": negative_pressure_evidence.get("missing_benchmark_columns"),
                "latest_evaluated_at": negative_pressure_evidence.get("latest_evaluated_at"),
                "age_days": negative_pressure_evidence.get("age_days"),
                "broker_execution_allowed": bool(negative_pressure_evidence.get("broker_execution_allowed")),
                "policy_auto_promotion_allowed": bool(negative_pressure_evidence.get("policy_auto_promotion_allowed")),
            },
        )

    adversarial_review_evidence = section("adversarial_review_evidence")
    if adversarial_review_evidence.get("status") in {"warn", "error"}:
        add_check(
            "adversarial_review_evidence",
            str(adversarial_review_evidence.get("status") or "warn"),
            "Adversarial-review evidence readiness",
            str(adversarial_review_evidence.get("message") or "Adversarial-review evidence is missing, stale, beta-only, or lacks matured labels."),
            impact=(
                "Keep adversarial veto/penalty policy research-only until point-in-time labels show benchmark-excess protection, not just broad-market beta."
            ),
            details={
                "reasons": adversarial_review_evidence.get("reasons"),
                "evaluation_rows": adversarial_review_evidence.get("evaluation_rows"),
                "matured_rows": adversarial_review_evidence.get("matured_rows"),
                "summary_rows": adversarial_review_evidence.get("summary_rows"),
                "candidate_keep_or_tighten_count": adversarial_review_evidence.get("candidate_keep_or_tighten_count"),
                "benchmark_beta_not_veto_alpha_count": adversarial_review_evidence.get("benchmark_beta_not_veto_alpha_count"),
                "needs_benchmark_attribution_count": adversarial_review_evidence.get("needs_benchmark_attribution_count"),
                "relax_or_false_positive_count": adversarial_review_evidence.get("relax_or_false_positive_count"),
                "latest_evaluated_at": adversarial_review_evidence.get("latest_evaluated_at"),
                "age_days": adversarial_review_evidence.get("age_days"),
                "broker_execution_allowed": bool(adversarial_review_evidence.get("broker_execution_allowed")),
                "policy_auto_promotion_allowed": bool(adversarial_review_evidence.get("policy_auto_promotion_allowed")),
            },
        )

    signal_quality_split_evidence = section("signal_quality_split_evidence")
    if signal_quality_split_evidence.get("status") in {"warn", "error"}:
        add_check(
            "signal_quality_split_evidence",
            str(signal_quality_split_evidence.get("status") or "warn"),
            "Signal-quality narrowed split evidence readiness",
            str(signal_quality_split_evidence.get("message") or "Narrowed split evidence is missing, stale, under-baselined, or lacks matured labels."),
            impact=(
                "Do not promote broad source-family context rules until narrowed splits have complete technical-only baselines and stable evidence."
            ),
            details={
                "reasons": signal_quality_split_evidence.get("reasons"),
                "evaluation_rows": signal_quality_split_evidence.get("evaluation_rows"),
                "matured_rows": signal_quality_split_evidence.get("matured_rows"),
                "summary_rows": signal_quality_split_evidence.get("summary_rows"),
                "split_count": signal_quality_split_evidence.get("split_count"),
                "stable_candidate_count": signal_quality_split_evidence.get("stable_candidate_count"),
                "harmful_negative_control_count": signal_quality_split_evidence.get("harmful_negative_control_count"),
                "unstable_or_horizon_sensitive_count": signal_quality_split_evidence.get("unstable_or_horizon_sensitive_count"),
                "baseline_unavailable_window_count": signal_quality_split_evidence.get("baseline_unavailable_window_count"),
                "latest_evaluated_at": signal_quality_split_evidence.get("latest_evaluated_at"),
                "age_days": signal_quality_split_evidence.get("age_days"),
                "broker_execution_allowed": bool(signal_quality_split_evidence.get("broker_execution_allowed")),
                "policy_auto_promotion_allowed": bool(signal_quality_split_evidence.get("policy_auto_promotion_allowed")),
            },
        )

    signal_quality_overlay_rules = section("signal_quality_overlay_rules")
    if signal_quality_overlay_rules.get("status") in {"warn", "error"}:
        add_check(
            "signal_quality_overlay_rules",
            str(signal_quality_overlay_rules.get("status") or "warn"),
            "Trusted signal-quality overlay rule eligibility",
            str(signal_quality_overlay_rules.get("message") or "Trusted overlay rules are blocked by reliability/stability gates."),
            impact="Configured trusted context rules may be annotation-only and should not be assumed to affect action policy.",
            details={
                "trusted_rule_count": signal_quality_overlay_rules.get("trusted_rule_count"),
                "runtime_eligible_rule_count": signal_quality_overlay_rules.get("runtime_eligible_rule_count"),
                "runtime_blocked_rule_count": signal_quality_overlay_rules.get("runtime_blocked_rule_count"),
                "reasons": signal_quality_overlay_rules.get("reasons"),
                "rows": signal_quality_overlay_rules.get("rows"),
            },
        )

    feature_stage_gates = section("feature_stage_gates")
    if feature_stage_gates.get("status") in {"warn", "error"}:
        add_check(
            "feature_stage_gates",
            str(feature_stage_gates.get("status") or "warn"),
            "Feature freshness stage gates",
            str(feature_stage_gates.get("error") or feature_stage_gates.get("message") or "Stage-level freshness gates found blocked inputs."),
            impact="Some stages may have downgraded positive signals to watch/review/deferred states due to stale or missing inputs.",
            details={
                "blocked_stage_count": feature_stage_gates.get("blocked_stage_count"),
                "blocked_symbol_count": feature_stage_gates.get("blocked_symbol_count"),
                "rows": feature_stage_gates.get("rows"),
            },
        )

    degradation = section("degradation_feed")
    active_count = int(degradation.get("active_count") or 0)
    if active_count:
        add_check(
            "degradation_feed",
            str(degradation.get("status") or "warn"),
            "Active degradation rows",
            f"{active_count} active degradation row(s) are visible.",
            impact="Some data may be fallback, partial, missing, or slow.",
            details={"active_count": active_count, "counts_by_kind": degradation.get("counts_by_kind")},
        )

    fallback_telemetry = section("fallback_telemetry")
    fallback_count = int(fallback_telemetry.get("active_count") or 0)
    if fallback_count:
        add_check(
            "fallback_telemetry",
            str(fallback_telemetry.get("status") or "warn"),
            "Fallback telemetry",
            f"{fallback_count} fallback/degraded-path event(s) were recorded in the recent window.",
            impact="Treat affected recommendations as review-only until the fallback source is understood.",
            details={
                "window_hours": fallback_telemetry.get("window_hours"),
                "error_count": fallback_telemetry.get("error_count"),
                "nse_retry_count": fallback_telemetry.get("nse_retry_count"),
                "nse_session_reset_count": fallback_telemetry.get("nse_session_reset_count"),
                "counts_by_type": fallback_telemetry.get("counts_by_type"),
                "counts_by_module": fallback_telemetry.get("counts_by_module"),
            },
        )

    error_count = sum(1 for row in checks if row["status"] == "error")
    warn_count = sum(1 for row in checks if row["status"] == "warn")
    if error_count:
        status = "error"
        trust_level = "blocked"
        recommendation = "Do not rely on today’s advisory output until error blockers are fixed."
    elif warn_count:
        status = "warn"
        trust_level = "review_required"
        recommendation = "Use recommendations as review-only; do not promote new rules or submit broker actions from this state."
    else:
        status = "ok"
        trust_level = "usable"
        recommendation = "No active trust blockers detected. Normal operator review still applies."
    return {
        "status": status,
        "trust_level": trust_level,
        "recommendation": recommendation,
        "error_count": error_count,
        "warn_count": warn_count,
        "count": len(checks),
        "checks": checks,
    }


def build_current_blockers(sections: dict[str, Any], fix_hints: list[dict[str, Any]], *, limit: int = 8) -> dict[str, Any]:
    """Summarize the smallest active set that blocks operator trust."""
    candidates: list[dict[str, Any]] = []
    for hint in fix_hints:
        if not isinstance(hint, dict):
            continue
        status = str(hint.get("status") or "ok").lower()
        if status == "ok" or "historical cron errors recovered" in str(hint.get("title") or "").lower():
            continue
        candidates.append(
            {
                "status": status if status in {"error", "warn"} else "warn",
                "title": str(hint.get("title") or "Health blocker"),
                "reason": str(hint.get("reason") or "Health check needs attention."),
                "category": _blocker_category(hint),
                "trust_impact": _trust_impact(hint),
                "commands": hint.get("commands") if isinstance(hint.get("commands"), list) else [],
                "details": hint.get("details") if isinstance(hint.get("details"), dict) else {},
                "source": "fix_hint",
            }
        )

    for key, value in sections.items():
        if key in {"trust_gate", "deferred_diagnostics"}:
            continue
        rows = value if isinstance(value, list) else [value] if isinstance(value, dict) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or "ok").lower()
            if status not in {"error", "warn"}:
                continue
            if row.get("recovered") or str(row.get("latest_run_status") or "") in {"ok_after_historical_errors", "recovered_after_manual_interrupt"}:
                continue
            title = TRUST_BLOCKER_SECTION_TITLES.get(key, str(row.get("name") or row.get("title") or key))
            candidates.append(
                {
                    "status": status,
                    "title": title,
                    "reason": str(row.get("error") or row.get("message") or row.get("title") or "Health row needs attention."),
                    "category": _blocker_category({"title": title, "details": {"section": key}}),
                    "trust_impact": _trust_impact({"status": status, "title": title, "details": {"section": key}}),
                    "commands": ["python -m advisory.operator_health --skip-dhan"],
                    "details": {
                        "section": key,
                        **{field: row.get(field) for field in ["name", "table", "latest_at", "age_hours", "row_count", "log_file", "source_name", "kind", "symbol", "observed_at"] if row.get(field) is not None},
                    },
                    "source": key,
                }
            )

    severity_rank = {"error": 0, "warn": 1}
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in candidates:
        key = _blocker_dedupe_key(row)
        existing = deduped.get(key)
        if existing is None:
            deduped[key] = row
            continue
        existing_rank = severity_rank.get(str(existing.get("status") or ""), 9)
        row_rank = severity_rank.get(str(row.get("status") or ""), 9)
        if row_rank < existing_rank or (row_rank == existing_rank and existing.get("source") != "fix_hint" and row.get("source") == "fix_hint"):
            deduped[key] = row
    rows = list(deduped.values())
    category_rank = {
        "runtime": 0,
        "operator_visibility": 1,
        "data_freshness": 2,
        "pipeline": 3,
        "broker_data": 4,
        "data_quality": 5,
        "research_evidence": 6,
        "advisory_trust": 7,
        "operations": 8,
    }
    rows.sort(key=lambda row: (severity_rank.get(str(row.get("status")), 9), category_rank.get(str(row.get("category")), 9), str(row.get("title") or "")))
    rows = rows[: max(1, int(limit))]
    status = "error" if any(row.get("status") == "error" for row in rows) else "warn" if rows else "ok"
    counts_by_category: dict[str, int] = {}
    for row in rows:
        category = str(row.get("category") or "unknown")
        counts_by_category[category] = counts_by_category.get(category, 0) + 1
    return {
        "status": status,
        "count": len(rows),
        "error_count": sum(1 for row in rows if row.get("status") == "error"),
        "warn_count": sum(1 for row in rows if row.get("status") == "warn"),
        "counts_by_category": counts_by_category,
        "rows": rows,
    }


def _deferred_section(name: str, *, command: str, reason: str) -> dict[str, Any]:
    return _status(
        "ok",
        f"{name} deferred in fast health mode.",
        deferred=True,
        reason=reason,
        command=command,
        suggested_fix=f"Run `{command}` when you need the full diagnostic output.",
    )


def build_deferred_diagnostics(sections: dict[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for section, value in sections.items():
        items = value if isinstance(value, list) else [value] if isinstance(value, dict) else []
        for item in items:
            if not isinstance(item, dict) or not item.get("deferred"):
                continue
            rows.append(
                {
                    "section": section,
                    "status": item.get("status") or "ok",
                    "message": item.get("message") or f"{section} deferred in fast health mode.",
                    "reason": item.get("reason") or "",
                    "command": item.get("command") or "python -m advisory.operator_health --full --skip-dhan",
                    "suggested_fix": item.get("suggested_fix") or "Run full Operator Health when you need this diagnostic evidence.",
                }
            )
    rows.sort(key=lambda row: str(row.get("section") or ""))
    return {
        "status": "warn" if rows else "ok",
        "count": len(rows),
        "rows": rows,
        "operator_action": (
            "Fast Health intentionally skipped deep diagnostics. Run full Health only when you need the listed evidence."
            if rows
            else "No diagnostics were deferred in this Health payload."
        ),
        "full_diagnostics_command": "python -m advisory.operator_health --full --skip-dhan",
        "api_full_payload_hint": "/api/health/details?mode=full&compact=false",
    }


def _run_health_checks(checks: dict[str, Any], *, workers: int) -> dict[str, Any]:
    if not checks:
        return {}
    max_workers = max(1, min(int(workers), len(checks)))
    if max_workers <= 1:
        out: dict[str, Any] = {}
        for name, func in checks.items():
            try:
                out[name] = func()
            except Exception as exc:
                _record_health_local_fallback(
                    source=str(name),
                    fallback_type="operator_health_check_failed",
                    reason="Operator Health check function failed.",
                    error=exc,
                    metadata={"check_name": str(name), "workers": max_workers},
                )
                out[name] = _status("error", "Health check failed.", error=f"{type(exc).__name__}: {exc}")
        return out
    out: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="operator-health") as executor:
        future_to_name = {executor.submit(func): name for name, func in checks.items()}
        for future in as_completed(future_to_name):
            name = future_to_name[future]
            try:
                out[name] = future.result()
            except Exception as exc:
                _record_health_local_fallback(
                    source=str(name),
                    fallback_type="operator_health_check_failed",
                    reason="Operator Health check function failed.",
                    error=exc,
                    metadata={"check_name": str(name), "workers": max_workers},
                )
                out[name] = _status("error", "Health check failed.", error=f"{type(exc).__name__}: {exc}")
    return {name: out[name] for name in checks if name in out}


def run_named_health_check(name: str, *, log_dir: str | Path = DEFAULT_LOG_DIR) -> Any:
    normalized = str(name or "").strip()
    if normalized == "database":
        return check_database()
    if normalized == "operator_api":
        return check_operator_api()
    if normalized == "operator_api_runtime":
        return check_operator_api_runtime()
    if normalized == "trace_summaries":
        return check_trace_summaries()
    if normalized == "slow_operations":
        return check_slow_operations(limit=20)
    if normalized == "api_latency_probe":
        return check_api_latency_probe()
    if normalized == "advisory_stage_report":
        return check_advisory_stage_report(log_dir)
    if normalized == "research_evidence_run_summary":
        return check_research_evidence_run_summary(log_dir)
    if normalized == "operator_snapshot":
        return check_operator_snapshot()
    if normalized == "event_data_quality":
        return build_event_data_quality_health_summary(limit=20)
    if normalized == "identity_issues":
        return check_identity_issues(limit=10)
    if normalized == "screener_failures":
        return check_screener_failures(limit=10)
    if normalized == "context_gate_policy":
        return check_context_gate_policy()
    if normalized == "signal_quality":
        return check_signal_quality()
    if normalized == "technical_threshold_evidence":
        return check_technical_threshold_evidence()
    if normalized == "ts_forecast_paper":
        return check_ts_forecast_paper_portfolio()
    if normalized == "llm_provenance_audit":
        return check_llm_provenance_audit()
    if normalized == "provenance_graph_dry_runs":
        return check_provenance_graph_dry_runs()
    if normalized == "causal_event_memory_evidence":
        return check_causal_event_memory_evidence()
    if normalized == "action_transition_evidence":
        return check_action_transition_evidence()
    if normalized == "event_policy_evidence":
        return check_event_policy_evidence()
    if normalized == "context_watch_evidence":
        return check_context_watch_evidence()
    if normalized == "negative_pressure_evidence":
        return check_negative_pressure_evidence()
    if normalized == "adversarial_review_evidence":
        return check_adversarial_review_evidence()
    if normalized == "signal_quality_split_evidence":
        return check_signal_quality_split_evidence()
    if normalized == "context_overlay_reliability":
        return check_context_overlay_reliability()
    if normalized == "macro_sector_alias_coverage":
        return check_macro_sector_alias_coverage()
    if normalized == "theme_sector_alias_coverage":
        return check_theme_sector_alias_coverage()
    if normalized == "signal_quality_overlay_rules":
        return check_signal_quality_overlay_rules()
    if normalized == "feature_stage_gates":
        return check_feature_stage_gates_snapshot(limit=OPERATOR_HEALTH_FEATURE_STAGE_GATE_SYMBOL_LIMIT)
    if normalized == "lifecycle_policy_audit":
        return check_lifecycle_policy_change_audit()
    if normalized == "fallback_telemetry":
        return check_fallback_telemetry_compact(hours=24, limit=25)
    if normalized == "table_freshness":
        return check_table_freshness(include_counts=False)
    if normalized == "sync_state_failures":
        return check_sync_state_failures()
    if normalized == "watcher_source_counters":
        return check_watcher_source_counters()
    if normalized == "signal_refresh_source_state":
        return check_signal_refresh_source_state()
    if normalized == "downloader_run_state":
        return check_downloader_run_state()
    if normalized == "ingestion_file_state":
        return check_ingestion_file_state_failures()
    if normalized == "schema_migrations":
        return check_schema_migrations()
    if normalized == "operator_api_errors":
        return check_operator_api_errors()
    if normalized == "redis":
        return check_redis()
    if normalized == "cron_logs":
        return check_cron_logs(log_dir)
    if normalized == "optional_dependencies":
        return check_optional_dependencies()
    if normalized == "frontend":
        return check_frontend_dependencies()
    if normalized == "frontend_runtime":
        return check_frontend_runtime()
    if normalized == "dhan_cache":
        return check_dhan_cache()
    raise ValueError(f"Unknown health check: {name}")


def run_named_health_check_subprocess(
    name: str,
    *,
    log_dir: str | Path = DEFAULT_LOG_DIR,
    timeout_seconds: float = OPERATOR_HEALTH_FULL_SECTION_TIMEOUT_SECONDS,
) -> Any:
    timeout = max(1.0, float(timeout_seconds))
    command = [
        sys.executable,
        "-c",
        (
            "import json, sys; "
            "from advisory.operator_health import run_named_health_check; "
            "payload = run_named_health_check(sys.argv[1], log_dir=sys.argv[2]); "
            "print(json.dumps(payload, default=str))"
        ),
        str(name),
        str(log_dir),
    ]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        _record_health_local_fallback(
            source=str(name),
            fallback_type="operator_health_section_timeout",
            reason="Full Operator Health section exceeded its process-isolated timeout.",
            error=exc,
            severity="warn",
            metadata={"check_name": str(name), "timeout_seconds": timeout},
        )
        return _status(
            "warn",
            "Full Health section timed out and was skipped for this payload.",
            timed_out=True,
            degraded=True,
            timeout_seconds=timeout,
            command=f"python -m advisory.operator_health --full --skip-dhan",
        )
    if proc.returncode:
        error_text = (proc.stderr or proc.stdout or "").strip()
        error = RuntimeError(error_text[:1000] or f"health section subprocess failed with returncode={proc.returncode}")
        _record_health_local_fallback(
            source=str(name),
            fallback_type="operator_health_section_subprocess_failed",
            reason="Full Operator Health section subprocess failed.",
            error=error,
            severity="warn",
            metadata={"check_name": str(name), "timeout_seconds": timeout, "returncode": proc.returncode},
        )
        return _status("error", "Full Health section subprocess failed.", error=f"{type(error).__name__}: {error}", degraded=True)
    output = (proc.stdout or "").strip()
    if not output:
        return _status("warn", "Full Health section returned no output.", degraded=True)
    json_text = output.splitlines()[-1]
    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError as exc:
        _record_health_local_fallback(
            source=str(name),
            fallback_type="operator_health_section_json_parse_failed",
            reason="Full Operator Health could not parse section subprocess output.",
            error=exc,
            severity="warn",
            metadata={"check_name": str(name), "stdout_length": len(output), "timeout_seconds": timeout},
        )
        return _status("error", "Full Health section returned invalid JSON.", error=f"{type(exc).__name__}: {exc}", degraded=True)
    return payload


def _full_health_checks(log_dir: str | Path) -> dict[str, Any]:
    names = [
        "database",
        "operator_api",
        "operator_api_runtime",
        "trace_summaries",
        "slow_operations",
        "api_latency_probe",
        "advisory_stage_report",
        "research_evidence_run_summary",
        "operator_snapshot",
        "event_data_quality",
        "identity_issues",
        "screener_failures",
        "context_gate_policy",
        "signal_quality",
        "technical_threshold_evidence",
        "ts_forecast_paper",
        "llm_provenance_audit",
        "provenance_graph_dry_runs",
        "causal_event_memory_evidence",
        "action_transition_evidence",
        "event_policy_evidence",
        "context_watch_evidence",
        "negative_pressure_evidence",
        "adversarial_review_evidence",
        "signal_quality_split_evidence",
        "context_overlay_reliability",
        "macro_sector_alias_coverage",
        "theme_sector_alias_coverage",
        "signal_quality_overlay_rules",
        "feature_stage_gates",
        "lifecycle_policy_audit",
        "fallback_telemetry",
        "table_freshness",
        "sync_state_failures",
        "watcher_source_counters",
        "signal_refresh_source_state",
        "downloader_run_state",
        "ingestion_file_state",
        "schema_migrations",
        "operator_api_errors",
        "redis",
        "cron_logs",
        "optional_dependencies",
        "frontend",
        "frontend_runtime",
        "dhan_cache",
    ]
    if OPERATOR_HEALTH_FULL_PROCESS_ISOLATED:
        return {
            name: (lambda _name=name: run_named_health_check_subprocess(_name, log_dir=log_dir))
            for name in names
        }
    return {name: (lambda _name=name: run_named_health_check(_name, log_dir=log_dir)) for name in names}


def build_operator_health(*, log_dir: str | Path = DEFAULT_LOG_DIR, include_dhan: bool = True, detail_level: str = "fast") -> dict[str, Any]:
    mode = "full" if str(detail_level or "").strip().lower() == "full" else "fast"
    full_mode = mode == "full"
    if full_mode:
        full_checks = _full_health_checks(log_dir)
        sections: dict[str, Any] = _run_health_checks(full_checks, workers=OPERATOR_HEALTH_FULL_WORKERS)
    else:
        fast_checks = {
            "database": check_database,
            "operator_api": check_operator_api,
            "operator_api_runtime": check_operator_api_runtime,
            "slow_operations": lambda: check_slow_operations(limit=20),
            "api_latency_probe": check_api_latency_probe,
            "advisory_stage_report": lambda: check_advisory_stage_report(log_dir),
            "research_evidence_run_summary": lambda: check_research_evidence_run_summary(log_dir),
            "operator_snapshot": check_operator_snapshot,
            "context_gate_policy": check_context_gate_policy,
            "redis": check_redis,
            "optional_dependencies": check_optional_dependencies,
            "frontend": check_frontend_dependencies,
            "frontend_runtime": check_frontend_runtime,
            "dhan_cache": check_dhan_cache,
        }
        sections = _run_health_checks(fast_checks, workers=OPERATOR_HEALTH_FAST_WORKERS)
        sections["trace_summaries"] = _deferred_section(
            "Trace summary cache",
            command="python -m advisory.operator_health --full --skip-dhan",
            reason="Trace-summary cache checks query materialized trace tables and are available in full health mode.",
        )
        sections["identity_issues"] = _deferred_section(
            "Dhan/security identity issues",
            command="python -m advisory.operator_health --full --skip-dhan",
            reason="Identity coverage checks join action rows, company master, and broker ids; use full health for source-blocker details.",
        )
        sections["signal_quality"] = _deferred_section(
            "Signal-quality evidence",
            command="python -m advisory.operator_health --full --skip-dhan",
            reason="Signal-quality evidence checks inspect evaluation summary tables and are available in full health mode.",
        )
        sections["technical_threshold_evidence"] = _deferred_section(
            "Technical-threshold calibration evidence",
            command="python -m advisory.operator_health --full --skip-dhan",
            reason="Technical-threshold calibration checks inspect realized after-cost threshold evidence and are available in full health mode.",
        )
        sections["ts_forecast_paper"] = _deferred_section(
            "TS forecast paper-portfolio evidence",
            command="python -m advisory.operator_health --full --skip-dhan",
            reason="TS forecast paper-portfolio checks inspect research-only forecast paper outcomes and are available in full health mode.",
        )
        sections["llm_provenance_audit"] = _deferred_section(
            "LLM provenance audit",
            command="python -m advisory.operator_health --full --skip-dhan",
            reason="LLM provenance checks inspect persisted LLM/Codex-derived rows for prompt/schema/evidence/authority metadata and are available in full health mode.",
        )
        sections["provenance_graph_dry_runs"] = _deferred_section(
            "Typed provenance graph dry runs",
            command="python -m advisory.operator_health --full --skip-dhan",
            reason="Typed provenance checks run action-evidence and causal-event provenance builders in dry-run mode and are available in full health mode.",
        )
        sections["causal_event_memory_evidence"] = _deferred_section(
            "Causal event-memory evaluation evidence",
            command="python -m advisory.operator_health --full --skip-dhan",
            reason="Causal event-memory evidence checks inspect persisted point-in-time outcome labels and are available in full health mode.",
        )
        sections["action_transition_evidence"] = _deferred_section(
            "Action-transition evaluation evidence",
            command="python -m advisory.operator_health --full --skip-dhan",
            reason="Action-transition evidence checks inspect persisted point-in-time transition labels and are available in full health mode.",
        )
        sections["event_policy_evidence"] = _deferred_section(
            "Event-policy evaluation evidence",
            command="python -m advisory.operator_health --full --skip-dhan",
            reason="Event-policy evidence checks inspect persisted point-in-time LLM/classification policy labels and are available in full health mode.",
        )
        sections["context_watch_evidence"] = _deferred_section(
            "Context-watch evaluation evidence",
            command="python -m advisory.operator_health --full --skip-dhan",
            reason="Context-watch evidence checks inspect persisted point-in-time watch-priority labels and are available in full health mode.",
        )
        sections["negative_pressure_evidence"] = _deferred_section(
            "Negative-pressure evaluation evidence",
            command="python -m advisory.operator_health --full --skip-dhan",
            reason="Negative-pressure evidence checks inspect persisted point-in-time de-risk labels and are available in full health mode.",
        )
        sections["adversarial_review_evidence"] = _deferred_section(
            "Adversarial-review evaluation evidence",
            command="python -m advisory.operator_health --full --skip-dhan",
            reason="Adversarial-review evidence checks inspect persisted point-in-time veto/penalty labels and are available in full health mode.",
        )
        sections["signal_quality_split_evidence"] = _deferred_section(
            "Signal-quality narrowed split evidence",
            command="python -m advisory.operator_health --full --skip-dhan",
            reason="Narrowed split evidence checks inspect persisted split labels, technical-only baselines, and stability classifications in full health mode.",
        )
        sections["signal_quality_overlay_rules"] = _deferred_section(
            "Signal-quality overlay rule eligibility",
            command="python -m advisory.operator_health --full --skip-dhan",
            reason="Trusted overlay-rule eligibility checks read config plus persisted reliability evidence and are available in full health mode.",
        )
        sections["macro_sector_alias_coverage"] = _deferred_section(
            "Macro sector alias coverage",
            command="python -m advisory.operator_health --full --skip-dhan",
            reason="Macro sector alias coverage builds current macro overlays and is available in full health mode.",
        )
        sections["theme_sector_alias_coverage"] = _deferred_section(
            "Theme sector alias coverage",
            command="python -m advisory.operator_health --full --skip-dhan",
            reason="Theme sector alias coverage builds current news-theme overlays and is available in full health mode.",
        )
        sections["feature_stage_gates"] = _deferred_section(
            "Feature freshness stage gates",
            command="python -m advisory.operator_health --full --skip-dhan",
            reason="Stage-gate checks evaluate required feature inputs for recent symbols and are available in full health mode.",
        )
        sections["lifecycle_policy_audit"] = _deferred_section(
            "Lifecycle policy-change audit",
            command="python -m advisory.operator_health --full --skip-dhan",
            reason="Lifecycle audit checks compare rebalance and policy-change rows and are available in full health mode.",
        )
        sections["sync_state_failures"] = [
            _deferred_section(
                "Sync-state failures",
                command="python -m advisory.operator_health --full --skip-dhan",
                reason="Sync-state failure scans read durable downloader/watcher state and are available in full health mode.",
            )
        ]
        sections["watcher_source_counters"] = _deferred_section(
            "Watcher source counters",
            command="python -m advisory.operator_health --full --skip-dhan",
            reason="Watcher source-counter checks read latest OHLCV/news/announcement sync-state rows and are available in full health mode.",
        )
        sections["signal_refresh_source_state"] = _deferred_section(
            "Review-only signal-refresh source state",
            command="python -m advisory.operator_health --full --skip-dhan",
            reason="Signal-refresh state checks read context-overlay, causal-memory, and bounded action-refresh sync-state rows and are available in full health mode.",
        )
        sections["downloader_run_state"] = _deferred_section(
            "Downloader run state",
            command="python -m advisory.operator_health --full --skip-dhan",
            reason="Downloader classification checks read latest run-state rows and failed-symbol samples; use full health for details.",
        )
        sections["ingestion_file_state"] = _deferred_section(
            "Ingestion file-state failures",
            command="python -m advisory.operator_health --full --skip-dhan",
            reason="File-state failure scans inspect parser state rows and are available in full health mode.",
        )
        sections["schema_migrations"] = _deferred_section(
            "Schema migration registry",
            command="python -m advisory.operator_health --full --skip-dhan",
            reason="Schema migration registry inspection is available in full health mode.",
        )
        sections["event_data_quality"] = _deferred_section(
            "Announcement/bhavcopy evidence quality",
            command="python -m advisory.event_data_quality --format json",
            reason="The full check scans/counts large NSE, announcement, and bhavcopy source tables and can take tens of seconds.",
        )
        sections["fallback_telemetry"] = _deferred_section(
            "Fallback/degraded-path telemetry",
            command="python -m advisory.operator_health --full --skip-dhan",
            reason="Fallback telemetry is queried from durable event rows and is available from full health or the dedicated diagnostics flow.",
        )
        sections["operator_api_errors"] = _deferred_section(
            "Operator API error history",
            command="Open Operations API errors or run python -m advisory.operator_health --full --skip-dhan",
            reason="Recent API-error rows can include tracebacks and are available through the dedicated Operations view.",
        )
        sections["screener_failures"] = _deferred_section(
            "Screener.in failure history",
            command="python -m advisory.operator_health --full --skip-dhan",
            reason="Recent Screener.in query/fetch/parse failure rows are inspected in full health mode.",
        )
        sections["table_freshness"] = [
            _deferred_section(
                "Table freshness",
                command="python -m advisory.operator_health --full --skip-dhan",
                reason="Freshness scans touch many large tables; fast health only checks runtime readiness and cached health indicators.",
            )
        ]
        sections["cron_logs"] = [
            _deferred_section(
                "Cron log deep scan",
                command="python -m advisory.operator_health --full --skip-dhan",
                reason="Cron log analysis can read thousands of lines from many logs. Use /api/operations/cron-logs for paged log inspection.",
            )
        ]
    if include_dhan:
        sections["dhan"] = check_dhan_token()
    else:
        sections["dhan"] = _status("warn", "Dhan token validation skipped by request.")
    sections["degradation_feed"] = build_degradation_feed(sections, log_dir=log_dir, include_deep_checks=full_mode)
    sections["trust_gate"] = build_trust_gate(sections)
    deferred_diagnostics = build_deferred_diagnostics(sections)
    fix_hints = build_fix_hints(sections)
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "detail_level": mode,
        "full_diagnostics_command": "python -m advisory.operator_health --full --skip-dhan",
        "status": summarize_status(sections),
        "sections": sections,
        "deferred_diagnostics": deferred_diagnostics,
        "fix_hints": fix_hints,
        "current_blockers": build_current_blockers(sections, fix_hints),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only Stockey operator health check.")
    parser.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    parser.add_argument("--skip-dhan", action="store_true", help="Skip Dhan profile token validation.")
    parser.add_argument("--full", action="store_true", help="Run expensive deep diagnostics against source data tables and full cron logs.")
    parser.add_argument("--check", help="Run one named health check, for example theme_sector_alias_coverage.")
    parser.add_argument("--format", choices=["json", "text"], default="json", help="Output format. Defaults to json for cron/API tooling.")
    return parser.parse_args()


def format_text(payload: dict[str, Any]) -> str:
    trust_gate = payload.get("sections", {}).get("trust_gate", {}) if isinstance(payload.get("sections"), dict) else {}
    blockers = payload.get("current_blockers", {}) if isinstance(payload.get("current_blockers"), dict) else {}
    lines = [
        "Stockey Operator Health",
        f"Status: {payload.get('status')}",
        f"Detail level: {payload.get('detail_level')}",
        f"Trust level: {trust_gate.get('trust_level') or trust_gate.get('status')}",
        f"Current blockers: {blockers.get('count', 0)}",
        "",
        "Top fix hints:",
    ]
    hints = payload.get("fix_hints") if isinstance(payload.get("fix_hints"), list) else []
    for hint in hints[:8]:
        commands = hint.get("commands") if isinstance(hint.get("commands"), list) else []
        lines.append(f"- [{hint.get('status')}] {hint.get('title')}: {hint.get('reason')}")
        if commands:
            lines.append(f"  first command: {commands[0]}")
    if not hints:
        lines.append("- none")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    if getattr(args, "check", None):
        payload = run_named_health_check(str(args.check), log_dir=args.log_dir)
        if args.format == "text":
            print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        else:
            print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        status = payload.get("status") if isinstance(payload, dict) else "ok"
        return 0 if status in {"ok", "warn"} else 1

    payload = build_operator_health(log_dir=args.log_dir, include_dhan=not bool(args.skip_dhan), detail_level="full" if bool(args.full) else "fast")
    if args.format == "text":
        print(format_text(payload))
    else:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return 0 if payload["status"] in {"ok", "warn"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
