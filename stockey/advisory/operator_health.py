from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pandas as pd
import requests
from environs import Env

from advisory.performance_slowlog import summarize_slow_operations
from advisory.operator_snapshot import DEFAULT_MAX_AGE_SECONDS as OPERATOR_SNAPSHOT_MAX_AGE_SECONDS
from advisory.operator_snapshot import SNAPSHOT_NAME, TABLE_NAME as OPERATOR_SNAPSHOT_TABLE
from advisory.event_data_quality import build_event_data_quality_report
from advisory.fallback_telemetry import record_local_fallback_event, summarize_fallback_events
from advisory.feature_freshness import STAGE_FEATURE_DEPENDENCIES_BY_STAGE, evaluate_stage_feature_gate
from advisory.identity_issues import IDENTITY_ISSUES_TABLE
from advisory.signal_quality_evaluator import EVALUATIONS_TABLE as SIGNAL_QUALITY_EVALUATIONS_TABLE
from advisory.signal_quality_evaluator import SUMMARY_TABLE as SIGNAL_QUALITY_SUMMARY_TABLE
from advisory.superseded_failures import cleanup_superseded_failures
from scripts.advisory_stage_report import build_stage_report
from scripts.api_performance_report import build_api_performance_report
from data.screenerin.failure_log import FAILURES_TABLE as SCREENER_FAILURES_TABLE
from utils.db import sql_to_df
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
REBALANCE_TABLE = "advisory_rebalance_actions"
LIFECYCLE_POLICY_CHANGES_TABLE = "advisory_lifecycle_policy_changes"
COMPANY_MASTER_TABLE = "company_master"
BROKER_CAPABLE_ACTION_CODES = ("BUY", "BUY_MORE", "SELL", "PARTIAL_SELL")
DHAN_TOKEN_EXPIRY_WARN_SECONDS = 6 * 60 * 60
OPERATOR_HEALTH_FAST_WORKERS = env.int("OPERATOR_HEALTH_FAST_WORKERS", default=3)
OPERATOR_HEALTH_INGESTION_FAILURE_ACTIVE_DAYS = env.int("OPERATOR_HEALTH_INGESTION_FAILURE_ACTIVE_DAYS", default=30)
API_LATENCY_PROBE_OUTPUT_FILE = Path(env.str("API_LATENCY_PROBE_OUTPUT_FILE", "logs/performance/latest_api_latency_probe.json"))
API_LATENCY_PROBE_MAX_AGE_SECONDS = env.float("API_LATENCY_PROBE_MAX_AGE_SECONDS", default=24 * 60 * 60)
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
    {"name": "sync_state", "table": "advisory_sync_state", "column": "updated_at", "max_age_days": 1},
    {"name": "operator_snapshot", "table": "advisory_operator_snapshots", "column": "generated_at", "max_age_days": 1},
    {"name": "signal_quality", "table": "advisory_signal_quality_eval_summary", "column": "evaluated_at", "max_age_days": 14},
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

    return _status(
        status,
        message,
        **report_payload,
        command=command,
        operations_command="advisory_stage_report",
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
            if has_error:
                row_status = "error"
                error_count += 1
            elif is_stale:
                row_status = "warn"
                stale_count += 1
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
                    error=row.get("error_text"),
                    counters=counters,
                )
            )
        status = "error" if error_count else "warn" if (stale_count or empty_count or missing_sources) else "ok"
        return _status(
            status,
            "Latest OHLCV/news/announcement watcher counters loaded.",
            table="advisory_sync_state",
            rows=rows,
            returned_count=len(rows),
            missing_sources=missing_sources,
            stale_count=stale_count,
            error_count=error_count,
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

    now = pd.Timestamp.utcnow()
    age_days = float((now - latest).total_seconds() / 86400.0)
    matured = pd.to_numeric(summary.get("matured_count", pd.Series(dtype=float)), errors="coerce")
    max_matured = int(matured.max()) if not matured.dropna().empty else 0
    overlay_rows = 0
    if not coverage.empty:
        for column in ["event_policy_rows", "bhavcopy_rows", "company_memory_rows"]:
            if column in coverage.columns:
                overlay_rows += int(pd.to_numeric(coverage[column], errors="coerce").fillna(0).max())
    reasons: list[str] = []
    if age_days > SIGNAL_QUALITY_MAX_AGE_DAYS:
        reasons.append("stale_signal_quality_run")
    if max_matured < SIGNAL_QUALITY_MIN_MATURED_ROWS:
        reasons.append("insufficient_matured_rows")
    if overlay_rows < SIGNAL_QUALITY_MIN_OVERLAY_ROWS:
        reasons.append("insufficient_overlay_coverage")
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
        coverage=_records(coverage),
        summary=_records(summary.head(20)),
        command="python -m advisory.signal_quality_evaluator --from-date YYYY-MM-DD --to-date YYYY-MM-DD --horizons 5 10 20",
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
    stages = ["rules", "risk", "portfolio", "lifecycle", "actions"]
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
                    "gate_effect": dependency.gate_effect,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        blocked_symbols = [str(value).upper() for value in gate.get("blocked_symbols") or []]
        rows.append(
            {
                "stage": stage,
                "status": gate.get("status"),
                "symbols_checked": gate.get("symbols_checked"),
                "blocked_count": gate.get("blocked_count", len(blocked_symbols)),
                "blocked_symbols": blocked_symbols[:10],
                "required_input_keys": gate.get("required_input_keys"),
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
                "empty_counter_count": watcher_counters.get("empty_counter_count"),
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
                "operations_command": advisory_stage_report.get("operations_command"),
                "operator_action": advisory_stage_report.get("operator_action"),
                "ranked_stages": advisory_stage_report.get("ranked_stages"),
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

    signal_quality = sections.get("signal_quality") if isinstance(sections.get("signal_quality"), dict) else {}
    if signal_quality.get("status") in {"warn", "error"}:
        add(
            status=str(signal_quality.get("status") or "warn"),
            title="Signal-quality evidence is not usable for promotion",
            reason=str(signal_quality.get("error") or signal_quality.get("message") or "Signal-quality evaluator needs a fresher or better-covered run."),
            commands=[str(signal_quality.get("command") or "python -m advisory.signal_quality_evaluator --from-date YYYY-MM-DD --to-date YYYY-MM-DD --horizons 5 10 20")],
            details={
                "section": "signal_quality",
                "latest_evaluated_at": signal_quality.get("latest_evaluated_at"),
                "reasons": signal_quality.get("reasons"),
                "max_matured_rows": signal_quality.get("max_matured_rows"),
                "overlay_rows": signal_quality.get("overlay_rows"),
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
            "sync_state": "./complete_data.sh",
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
    "signal_quality": "Signal-quality evidence readiness",
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
    if section in {"dhan", "dhan_cache"} or kind.startswith("dhan") or "dhan" in title:
        return "broker_data"
    if section == "identity_issues" or "identity" in title:
        return "broker_data"
    if section == "signal_quality" or "signal-quality" in title:
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
        "operations": 6,
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


def build_operator_health(*, log_dir: str | Path = DEFAULT_LOG_DIR, include_dhan: bool = True, detail_level: str = "fast") -> dict[str, Any]:
    mode = "full" if str(detail_level or "").strip().lower() == "full" else "fast"
    full_mode = mode == "full"
    if full_mode:
        sections: dict[str, Any] = {
            "database": check_database(),
            "operator_api": check_operator_api(),
            "operator_api_runtime": check_operator_api_runtime(),
            "trace_summaries": check_trace_summaries(),
            "slow_operations": check_slow_operations(limit=20),
            "api_latency_probe": check_api_latency_probe(),
            "advisory_stage_report": check_advisory_stage_report(log_dir),
            "operator_snapshot": check_operator_snapshot(),
            "event_data_quality": build_event_data_quality_report(limit=20),
            "identity_issues": check_identity_issues(limit=10),
            "screener_failures": check_screener_failures(limit=10),
            "signal_quality": check_signal_quality(),
            "feature_stage_gates": check_feature_stage_gates(),
            "lifecycle_policy_audit": check_lifecycle_policy_change_audit(),
            "fallback_telemetry": summarize_fallback_events(hours=24, limit=25),
            "table_freshness": check_table_freshness(include_counts=True),
            "sync_state_failures": check_sync_state_failures(),
            "watcher_source_counters": check_watcher_source_counters(),
            "downloader_run_state": check_downloader_run_state(),
            "ingestion_file_state": check_ingestion_file_state_failures(),
            "schema_migrations": check_schema_migrations(),
            "operator_api_errors": check_operator_api_errors(),
            "redis": check_redis(),
            "cron_logs": check_cron_logs(log_dir),
            "optional_dependencies": check_optional_dependencies(),
            "frontend": check_frontend_dependencies(),
            "frontend_runtime": check_frontend_runtime(),
            "dhan_cache": check_dhan_cache(),
        }
    else:
        fast_checks = {
            "database": check_database,
            "operator_api": check_operator_api,
            "operator_api_runtime": check_operator_api_runtime,
            "slow_operations": lambda: check_slow_operations(limit=20),
            "api_latency_probe": check_api_latency_probe,
            "advisory_stage_report": lambda: check_advisory_stage_report(log_dir),
            "operator_snapshot": check_operator_snapshot,
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
    payload = build_operator_health(log_dir=args.log_dir, include_dhan=not bool(args.skip_dhan), detail_level="full" if bool(args.full) else "fast")
    if args.format == "text":
        print(format_text(payload))
    else:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return 0 if payload["status"] in {"ok", "warn"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
