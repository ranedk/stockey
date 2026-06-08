from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from environs import Env

from advisory.performance_slowlog import summarize_slow_operations
from advisory.operator_snapshot import DEFAULT_MAX_AGE_SECONDS as OPERATOR_SNAPSHOT_MAX_AGE_SECONDS
from advisory.operator_snapshot import SNAPSHOT_NAME, TABLE_NAME as OPERATOR_SNAPSHOT_TABLE
from advisory.superseded_failures import cleanup_superseded_failures
from utils.db import sql_to_df
from utils.redis_utils import get_redis_client


env = Env()
env.read_env()

DEFAULT_LOG_DIR = Path("logs/cron")
DEFAULT_LOG_TAIL_LINES = 80
DEFAULT_LOG_ANALYSIS_LINES = 5000
DEFAULT_OPERATOR_API_URL = "http://127.0.0.1:8765/api/health"
OPERATOR_API_ERRORS_TABLE = "advisory_operator_api_errors"
TRACE_SUMMARIES_TABLE = "advisory_trace_summaries"
DHAN_TOKEN_EXPIRY_WARN_SECONDS = 6 * 60 * 60
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
    except Exception:
        pass
    return value


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df.empty:
        return []
    clean = df.copy().astype(object).where(pd.notna(df), None)
    return [{key: _json_ready(value) for key, value in row.items()} for row in clean.to_dict(orient="records")]


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
        return _status("error", "Operator API health endpoint is not reachable.", url=url, latency_ms=latency_ms, error=f"{type(exc).__name__}: {exc}")


def check_table_freshness(now: pd.Timestamp | None = None) -> list[dict[str, Any]]:
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
            df = sql_to_df(
                f'SELECT MAX("{effective_column}") AS latest_at, COUNT(*) AS row_count FROM "{table}"',
                retries=2,
                statement_timeout_ms=10000,
            )
            latest_at = pd.to_datetime(df.iloc[0]["latest_at"], utc=True, errors="coerce") if not df.empty else pd.NaT
            row_count = int(df.iloc[0]["row_count"] or 0) if not df.empty else 0
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
                    "A watcher/router sync cycle has a non-ok status.",
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
        return [_status("error", "Sync-state failure check failed.", table="advisory_sync_state", error=f"{type(exc).__name__}: {exc}")]


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
            return _status("warn", "Could not inspect cached Dhan token.", error=f"{type(exc).__name__}: {exc}")
        token = direct_token or cached_token
        if not token:
            return _status("warn", "No non-interactive Dhan token available. Health check will not trigger login.", has_cached_payload=bool(cached_payload))
        from data.dhanlive.client import DhanHistoricalClient

        client = DhanHistoricalClient(access_token=token, timeout=10, auth_attempts=1)
        payload = client.validate_access_token()
        return _status("ok", "Dhan profile call succeeded.", token_source="env" if direct_token else "cache", profile_keys=sorted(payload.keys()) if isinstance(payload, dict) else [])
    except Exception as exc:
        return _status("error", "Dhan token validation failed.", error=f"{type(exc).__name__}: {exc}")


def check_dhan_cache() -> dict[str, Any]:
    try:
        from data.dhanlive.auth import DEFAULT_TOKEN_CACHE, load_cached_access_token_payload
        from data.dhanlive.auth_cli import mask_token, parse_expiry

        direct_token = env("DHAN_ACCESS_TOKEN", default=None)
        payload = load_cached_access_token_payload()
        cache_exists = DEFAULT_TOKEN_CACHE.exists()
        file_mtime = pd.to_datetime(DEFAULT_TOKEN_CACHE.stat().st_mtime, unit="s", utc=True) if cache_exists else pd.NaT
        now = pd.Timestamp.utcnow()
        expiry_time = payload.get("expiryTime") if payload else None
        expires_at_raw = parse_expiry(expiry_time) if expiry_time else None
        expires_at = pd.to_datetime(expires_at_raw, utc=True, errors="coerce") if expires_at_raw else pd.NaT
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
            has_cached_access_token=bool(payload and payload.get("accessToken")),
            cached_access_token=mask_token(payload.get("accessToken") if payload else None),
            expiry_time=expiry_time,
            expires_at=_json_ready(expires_at),
            seconds_to_expiry=seconds_to_expiry,
            warn_seconds=DHAN_TOKEN_EXPIRY_WARN_SECONDS,
        )
    except Exception as exc:
        return _status("warn", "Could not inspect Dhan token cache.", error=f"{type(exc).__name__}: {exc}")


def _tail_lines(path: Path, limit: int) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
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
                        "line": line,
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
            "latest_success_line": latest_marker["line"] if marker_status == "done" else None,
            "latest_error_line": latest_marker["line"] if marker_status in {"failed", "interrupted"} else (current_errors[-1] if current_errors else None),
            "latest_script_marker": latest_marker,
            "current_error_count": len(current_errors),
            "historical_error_count": len(historical_errors),
            "recent_error_count": len(current_errors),
            "recent_errors": current_errors[-8:],
            "historical_errors": historical_errors[-8:],
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
        "latest_success_line": latest_success[1] if latest_success else None,
        "latest_error_line": latest_error[1] if latest_error else None,
        "current_error_count": len(current_errors),
        "historical_error_count": len(historical_errors),
        "recent_error_count": len(current_errors),
        "recent_errors": current_errors[-8:],
        "historical_errors": historical_errors[-8:],
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


def check_cron_logs(log_dir: str | Path = DEFAULT_LOG_DIR, *, tail_lines: int = DEFAULT_LOG_TAIL_LINES) -> list[dict[str, Any]]:
    root = Path(log_dir)
    if not root.exists():
        return [_status("warn", "Cron log directory does not exist.", log_dir=str(root))]
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.log")):
        stat = path.stat()
        lines = _tail_lines(path, tail_lines)
        analysis_lines = _tail_lines(path, max(tail_lines, DEFAULT_LOG_ANALYSIS_LINES))
        analysis = analyze_cron_log_lines(analysis_lines)
        modified_at = pd.to_datetime(stat.st_mtime, unit="s", utc=True)
        analysis = recover_interrupted_cron_status(path, modified_at, analysis)
        status = str(analysis.pop("status"))
        message = str(analysis.pop("message"))
        degradation_markers = [
            line
            for line in analysis_lines
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
                analyzed_lines=len(analysis_lines),
                recent_tail_errors=[line for line in lines if any(pattern.search(line) for pattern in FAILURE_PATTERNS)][-8:],
                degradation_markers=degradation_markers,
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
        if existing is None or str(row.get("observed_at") or "") >= str(existing.get("observed_at") or ""):
            deduped[key] = row
    out = list(deduped.values())
    out.sort(key=lambda row: (str(row.get("status") or ""), str(row.get("observed_at") or "")), reverse=True)
    return out[: max(1, int(limit))]


def build_degradation_lifecycle_groups(rows: list[dict[str, Any]], *, limit: int = 100) -> dict[str, Any]:
    active_count = sum(1 for row in rows if not row.get("recovered"))
    recovered_count = sum(1 for row in rows if row.get("recovered"))
    superseded_preview: dict[str, Any] = {"status": "unavailable", "error": None}
    superseded_count = 0
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
        superseded_preview = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

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


def build_degradation_feed(sections: dict[str, Any], *, log_dir: str | Path = DEFAULT_LOG_DIR, limit: int = 100) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
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
        "lifecycle": build_degradation_lifecycle_groups(rows, limit=limit),
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


def check_operator_api_errors(limit: int = 25) -> dict[str, Any]:
    if not table_exists(OPERATOR_API_ERRORS_TABLE):
        return _status("ok", "No operator API errors have been recorded yet.", rows=[], returned_count=0)
    try:
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
        return _status("error", "Could not inspect operator API errors.", error=f"{type(exc).__name__}: {exc}", rows=[])
    rows = _records(df)
    for row in rows:
        try:
            row["request_context"] = json.loads(str(row.pop("request_context_json") or "{}"))
        except Exception:
            row["request_context"] = {}
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
    if not table_exists(TRACE_SUMMARIES_TABLE):
        return _status("warn", "Trace summary cache table is missing.", row_count=0)
    try:
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
            reason=f"{slow.get('returned_count', 0)} open slow-operation issue(s) returned from {slow.get('state_file')}.",
            commands=["python -m advisory.performance_slowlog report --limit 20"],
            details={"issue_count": slow.get("issue_count"), "returned_count": slow.get("returned_count"), "state_file": slow.get("state_file")},
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
        add(
            status=str(dhan_cache.get("status") or "warn"),
            title="Dhan cached token needs attention",
            reason=str(dhan_cache.get("message") or "Dhan cached token health check did not pass."),
            commands=["python -m data.dhanlive.auth_cli status", "python -m data.dhanlive.auth_cli refresh --clear-cache-first --auto-login"],
            details={
                "cache_path": dhan_cache.get("cache_path"),
                "expires_at": dhan_cache.get("expires_at"),
                "seconds_to_expiry": dhan_cache.get("seconds_to_expiry"),
                "cache_age_seconds": dhan_cache.get("cache_age_seconds"),
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
        recent_errors = row.get("recent_errors") if isinstance(row.get("recent_errors"), list) else []
        recovered = row.get("latest_run_status") in {"ok_after_historical_errors", "recovered_after_manual_interrupt"} and not recent_errors
        add(
            status=str(row.get("status") or "warn"),
            title=f"Historical cron errors recovered in {Path(log_file).name}" if recovered else f"Recent cron errors in {Path(log_file).name}",
            reason=str(row.get("message") if recovered else recent_errors[-1] if recent_errors else row.get("message")),
            commands=[f"tail -100 {log_file}", "python -m advisory.operator_health --skip-dhan"],
            details={
                "log_file": log_file,
                "latest_run_status": row.get("latest_run_status"),
                "recent_error_count": row.get("recent_error_count"),
                "historical_error_count": row.get("historical_error_count"),
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
    "redis": "Redis runtime state",
    "dhan": "Dhan token validation",
    "dhan_cache": "Dhan token cache",
    "frontend": "Operator frontend dependencies",
    "degradation_feed": "Runtime degradation feed",
}


def _blocker_category(row: dict[str, Any]) -> str:
    details = row.get("details") if isinstance(row.get("details"), dict) else {}
    section = str(details.get("section") or "").lower()
    kind = str(details.get("kind") or "").lower()
    title = str(row.get("title") or "").lower()
    if section in {"database", "operator_api"} or "postgres" in title or "operator api" in title:
        return "runtime"
    if section in {"dhan", "dhan_cache"} or kind.startswith("dhan") or "dhan" in title:
        return "broker_data"
    if section in {"operator_snapshot", "trace_summaries"} or "snapshot" in title or "trace" in title:
        return "operator_visibility"
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
    if status == "error":
        return "A required health check is failing."
    return "This warning should be triaged before relying on fresh advisory output."


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

    deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in candidates:
        details = row.get("details") if isinstance(row.get("details"), dict) else {}
        dedupe_name = str(details.get("section") or details.get("table") or details.get("kind") or row.get("title") or "")
        key = (str(row.get("status") or ""), str(row.get("category") or ""), dedupe_name)
        existing = deduped.get(key)
        if existing is None or (existing.get("source") != "fix_hint" and row.get("source") == "fix_hint"):
            deduped[key] = row
    rows = list(deduped.values())
    severity_rank = {"error": 0, "warn": 1}
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


def build_operator_health(*, log_dir: str | Path = DEFAULT_LOG_DIR, include_dhan: bool = True) -> dict[str, Any]:
    sections: dict[str, Any] = {
        "database": check_database(),
        "operator_api": check_operator_api(),
        "trace_summaries": check_trace_summaries(),
        "slow_operations": summarize_slow_operations(limit=20),
        "operator_snapshot": check_operator_snapshot(),
        "table_freshness": check_table_freshness(),
        "sync_state_failures": check_sync_state_failures(),
        "operator_api_errors": check_operator_api_errors(),
        "redis": check_redis(),
        "cron_logs": check_cron_logs(log_dir),
        "optional_dependencies": check_optional_dependencies(),
        "frontend": check_frontend_dependencies(),
        "dhan_cache": check_dhan_cache(),
    }
    if include_dhan:
        sections["dhan"] = check_dhan_token()
    else:
        sections["dhan"] = _status("warn", "Dhan token validation skipped by request.")
    sections["degradation_feed"] = build_degradation_feed(sections, log_dir=log_dir)
    fix_hints = build_fix_hints(sections)
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "status": summarize_status(sections),
        "sections": sections,
        "fix_hints": fix_hints,
        "current_blockers": build_current_blockers(sections, fix_hints),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only Stockey operator health check.")
    parser.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    parser.add_argument("--skip-dhan", action="store_true", help="Skip Dhan profile token validation.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = build_operator_health(log_dir=args.log_dir, include_dhan=not bool(args.skip_dhan))
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return 0 if payload["status"] in {"ok", "warn"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
