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
from utils.db import sql_to_df
from utils.redis_utils import get_redis_client


env = Env()
env.read_env()

DEFAULT_LOG_DIR = Path("logs/cron")
DEFAULT_LOG_TAIL_LINES = 80
DEFAULT_LOG_ANALYSIS_LINES = 5000
DEFAULT_OPERATOR_API_URL = "http://127.0.0.1:8765/api/health"
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
                **analysis,
            )
        )
    return rows or [_status("warn", "Cron log directory has no .log files.", log_dir=str(root))]


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


def build_operator_health(*, log_dir: str | Path = DEFAULT_LOG_DIR, include_dhan: bool = True) -> dict[str, Any]:
    sections: dict[str, Any] = {
        "database": check_database(),
        "operator_api": check_operator_api(),
        "slow_operations": summarize_slow_operations(limit=20),
        "operator_snapshot": check_operator_snapshot(),
        "table_freshness": check_table_freshness(),
        "sync_state_failures": check_sync_state_failures(),
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
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "status": summarize_status(sections),
        "sections": sections,
        "fix_hints": build_fix_hints(sections),
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
