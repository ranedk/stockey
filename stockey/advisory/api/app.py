from __future__ import annotations

import argparse
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

from advisory.action_recommender import TABLE_NAME as ACTION_RECOMMENDATIONS_TABLE
from advisory.decision_trace import load_event_trace, load_symbol_trace
from advisory.decision_trace import ACTION_CONFLICTS_TABLE
from advisory.event_model_artifact_store import build_artifact_manifest
from advisory.event_model_promotion_check import build_promotion_check
from advisory.hypothesis_engine import create_hypothesis, latest_promotion_audit, load_action_plans, load_hypotheses, load_matches, preview_hypothesis_payload, run_hypothesis_scan, run_promotion_audit, update_hypothesis
from advisory.event_policy import TABLE_NAME as EVENT_POLICY_TABLE
from advisory.event_policy_evaluator import SUMMARY_TABLE as EVENT_POLICY_EVAL_SUMMARY_TABLE
from advisory.execution_engine import EXECUTION_TABLE
from advisory.live_dashboard import DEFAULT_OUTPUT_DIR, build_live_dashboard_payload
from advisory.market_context import load_latest_market_context
from advisory.operator_health import build_operator_health
from advisory.operator_snapshot import DEFAULT_MAX_AGE_SECONDS as OPERATOR_SNAPSHOT_MAX_AGE_SECONDS
from advisory.operator_snapshot import load_operator_snapshot
from advisory.performance_slowlog import record_slow_operation
from advisory.performance_slowlog import update_slow_issue_status
from advisory.technical_threshold_calibration import EVALUATIONS_TABLE as TECHNICAL_CALIBRATION_EVALUATIONS_TABLE
from advisory.technical_threshold_calibration import SUMMARY_TABLE as TECHNICAL_CALIBRATION_SUMMARY_TABLE
from advisory.technical_threshold_promotion import generate_promotion_review, load_promotion_reviews, record_manual_decision
from advisory.trace_summary_store import DEFAULT_LIMIT as TRACE_SUMMARY_DEFAULT_LIMIT
from advisory.trace_summary_store import load_summary as load_materialized_trace_summary
from utils.db import db_session, sql_to_df, upsert_to_db


env = Env()
env.read_env()

OPERATOR_API_USE_SNAPSHOT = env.bool("OPERATOR_API_USE_SNAPSHOT", True)
OPERATOR_API_SLOW_REQUEST_MS = env.float("OPERATOR_API_SLOW_REQUEST_MS", 750.0)
OPERATOR_API_PAYLOAD_CACHE_SECONDS = env.float("OPERATOR_API_PAYLOAD_CACHE_SECONDS", 15.0)
OPERATOR_API_LARGE_RESPONSE_BYTES = env.int("OPERATOR_API_LARGE_RESPONSE_BYTES", 250_000)
OPERATOR_API_TRACE_SUMMARY_CACHE_ENABLED = env.bool("OPERATOR_API_TRACE_SUMMARY_CACHE_ENABLED", True)
REPO_ROOT = Path(__file__).resolve().parents[2]
CRON_LOG_DIR = env.path("OPERATOR_CRON_LOG_DIR", REPO_ROOT / "logs" / "cron")
MANUAL_REVIEW_DECISIONS_TABLE = "advisory_manual_review_decisions"
MANUAL_REVIEW_CLOSING_DECISIONS = {"approve_for_manual_config", "ignore", "downgrade_to_no_action", "mark_fixed"}
MANUAL_REVIEW_ALLOWED_DECISIONS = MANUAL_REVIEW_CLOSING_DECISIONS | {"needs_more_data", "watch_for_event", "add_operator_note"}
OPERATOR_COMMAND_RUNS_TABLE = "advisory_operator_command_runs"
OPERATOR_COMMAND_TIMEOUT_SECONDS = env.int("OPERATOR_COMMAND_TIMEOUT_SECONDS", 180)
OPERATOR_COMMAND_OUTPUT_TAIL_CHARS = env.int("OPERATOR_COMMAND_OUTPUT_TAIL_CHARS", 12_000)
OPERATOR_API_ERRORS_TABLE = "advisory_operator_api_errors"
OPERATOR_API_ERROR_TRACE_CHARS = env.int("OPERATOR_API_ERROR_TRACE_CHARS", 4_000)
SLOW_ISSUE_ALLOWED_STATUSES = {"open", "triaged", "fixed", "ignored"}

_PAYLOAD_CACHE: dict[tuple[str, str | None], tuple[float, dict[str, Any]]] = {}


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
    payload = build_live_dashboard_payload(asof_date=parsed_asof, output_dir=DEFAULT_OUTPUT_DIR)
    payload["_snapshot"] = {"source": "live_builder", "reason": "missing_or_stale_snapshot"}
    _PAYLOAD_CACHE[cache_key] = (now, payload)
    return payload


def build_health_payload() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "stockey-operator-api",
        "operator_controlled": True,
        "read_only": False,
        "write_scope": "operator_audit_and_research_controls",
    }


def build_summary_payload(*, asof_date: str | None = None) -> dict[str, Any]:
    payload = load_operator_payload(asof_date=asof_date)
    return {
        "generated_at": payload.get("generated_at"),
        "asof_date": payload.get("asof_date"),
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
    "pnl_pct",
    "invest_score_pct",
    "allocation_inr",
    "execution_intent",
    "exit_strategy",
    "technical_context",
    "announcement_summary",
    "news_summary",
    "manual_revision_summary",
    "manual_revision_pointers",
    "sort_ts",
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


def _compact_reason_contract(value: Any) -> dict[str, Any] | str | None:
    parsed = _jsonish(value)
    if not isinstance(parsed, dict):
        return _trim_home_value(value, max_text=500, max_depth=1)
    out: dict[str, Any] = {}
    for key in [
        "status",
        "action_code",
        "final_action",
        "new_action",
        "action_source",
        "execution_action",
        "setup_id",
        "primary_reason",
        "reason",
        "reason_detail",
        "missing_fields",
    ]:
        if parsed.get(key) is not None:
            out[key] = _trim_home_value(parsed.get(key), max_text=320, max_list=4, max_depth=2)
    evidence = parsed.get("evidence")
    if isinstance(evidence, dict):
        compact_evidence: dict[str, Any] = {}
        for section_key, section_value in evidence.items():
            if not isinstance(section_value, dict):
                continue
            compact_section: dict[str, Any] = {}
            for item_key, item_value in list(section_value.items())[:5]:
                trimmed = _trim_home_value(item_value, max_text=160, max_list=3, max_depth=1)
                if trimmed is not None:
                    compact_section[str(item_key)] = trimmed
            if compact_section:
                compact_evidence[str(section_key)] = compact_section
        if compact_evidence:
            out["evidence"] = compact_evidence
    return out or None


def _compact_home_field(key: str, value: Any) -> Any:
    if key == "recommendation_reason":
        return _compact_reason_contract(value)
    if key == "manual_revision_pointers":
        return _trim_home_value(value, max_text=220, max_list=3, max_depth=2)
    if key in {"announcement_summary", "news_summary", "reason_detail", "exit_strategy", "manual_revision_summary"}:
        return _trim_home_value(value, max_text=360, max_list=3, max_depth=2)
    return _trim_home_value(value, max_text=500, max_list=4, max_depth=3)


def _compact_home_rows(rows: Any, *, limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(rows, list):
        return out
    for row in rows[: max(0, int(limit))]:
        if not isinstance(row, dict):
            continue
        compact = {
            key: _compact_home_field(key, row.get(key))
            for key in HOME_CARD_FIELDS
            if row.get(key) is not None
        }
        out.append({key: value for key, value in compact.items() if value not in (None, "", [], {})})
    return out


def build_home_payload(*, asof_date: str | None = None) -> dict[str, Any]:
    payload = load_operator_payload(asof_date=asof_date)
    return {
        "generated_at": payload.get("generated_at"),
        "asof_date": payload.get("asof_date"),
        "summary": payload.get("summary") or {},
        "runtime_processes": payload.get("runtime_processes") or [],
        "cron_status": payload.get("cron_status") or [],
        "sync_state": payload.get("sync_state") or [],
        "top_action_recommendations": _compact_home_rows(payload.get("top_action_recommendations"), limit=25),
        "today_recommendations": _compact_home_rows(payload.get("today_recommendations"), limit=25),
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
) -> dict[str, Any]:
    payload = load_operator_payload(asof_date=asof_date)
    top_actions = _filter_rows(payload.get("top_action_recommendations") or [], symbol=symbol, action=action, status=status, search=search)
    action_rows = _filter_rows(payload.get("action_recommendations") or [], symbol=symbol, action=action, status=status, search=search)
    alert_rows = _filter_rows(payload.get("alerts") or [], symbol=symbol, status=status, search=search)
    action_page, action_meta = _page_rows(action_rows, limit=limit, offset=offset)
    return {
        "generated_at": payload.get("generated_at"),
        "asof_date": payload.get("asof_date"),
        "top_action_recommendations": _compact_list_rows(top_actions[: _bounded_limit(limit, default=25)], compact=compact),
        "action_recommendations": _compact_list_rows(action_page, compact=compact),
        "alerts": _compact_list_rows(alert_rows[: _bounded_limit(limit, default=25)], compact=compact),
        "meta": {
            "top_action_recommendations": {"total": len(top_actions), "returned": min(len(top_actions), _bounded_limit(limit, default=25))},
            "action_recommendations": action_meta,
            "alerts": {"total": len(alert_rows), "returned": min(len(alert_rows), _bounded_limit(limit, default=25))},
            "filters": {"symbol": symbol, "action": action, "status": status, "search": search, "compact": compact},
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
) -> dict[str, Any]:
    payload = load_operator_payload(asof_date=asof_date)
    today_rows = _filter_rows(payload.get("today_recommendations") or [], symbol=symbol, status=status, search=search)
    current_rows = _filter_rows(payload.get("current_recommendations") or [], symbol=symbol, status=status, search=search)
    exited_rows = _filter_rows(payload.get("exited_recommendations") or [], symbol=symbol, status=status, search=search)
    portfolio_rows = _filter_rows(payload.get("portfolio") or [], symbol=symbol, status=status, search=search)
    lifecycle_rows = _filter_rows(payload.get("lifecycle") or [], symbol=symbol, status=status, search=search)
    portfolio_page, portfolio_meta = _page_rows(portfolio_rows, limit=limit, offset=offset)
    return {
        "generated_at": payload.get("generated_at"),
        "asof_date": payload.get("asof_date"),
        "today_recommendations": _compact_list_rows(today_rows[: _bounded_limit(limit, default=25)], compact=compact),
        "current_recommendations": _compact_list_rows(current_rows[: _bounded_limit(limit, default=25)], compact=compact),
        "exited_recommendations": _compact_list_rows(exited_rows[: _bounded_limit(limit, default=25)], compact=compact),
        "portfolio": _compact_list_rows(portfolio_page, compact=compact),
        "lifecycle": _compact_list_rows(lifecycle_rows[: _bounded_limit(limit, default=25)], compact=compact),
        "meta": {
            "today_recommendations": {"total": len(today_rows), "returned": min(len(today_rows), _bounded_limit(limit, default=25))},
            "current_recommendations": {"total": len(current_rows), "returned": min(len(current_rows), _bounded_limit(limit, default=25))},
            "exited_recommendations": {"total": len(exited_rows), "returned": min(len(exited_rows), _bounded_limit(limit, default=25))},
            "portfolio": portfolio_meta,
            "lifecycle": {"total": len(lifecycle_rows), "returned": min(len(lifecycle_rows), _bounded_limit(limit, default=25))},
            "filters": {"symbol": symbol, "status": status, "search": search, "compact": compact},
        },
    }


def build_watchlist_payload(*, asof_date: str | None = None) -> dict[str, Any]:
    payload = load_operator_payload(asof_date=asof_date)
    return {
        "generated_at": payload.get("generated_at"),
        "asof_date": payload.get("asof_date"),
        "watch_recommendations": payload.get("watch_recommendations") or [],
        "watchlist": payload.get("watchlist") or [],
        "ts_watch_recommendations": payload.get("ts_watch_recommendations") or [],
        "ts_forecast_watch": payload.get("ts_forecast_watch") or [],
        "ts_forecast_eval_summary": payload.get("ts_forecast_eval_summary") or [],
    }


def build_market_context_payload(*, asof_date: str | None = None, limit: int = 50) -> dict[str, Any]:
    parsed_asof = _parse_asof_date(asof_date)
    payload = load_latest_market_context(parsed_asof, limit=max(0, int(limit)))
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "asof_date": asof_date,
        "summary": payload.get("summary") or {},
        "top_universe": payload.get("top_universe") or [],
    }


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


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df.empty:
        return []
    out = df.copy()
    out = out.astype(object).where(pd.notna(out), None)
    return out.to_dict(orient="records")


def _bounded_limit(value: int | None, *, default: int = 50, maximum: int = 500) -> int:
    try:
        parsed = int(value if value is not None else default)
    except Exception:
        parsed = default
    return max(0, min(parsed, maximum))


def _bounded_offset(value: int | None) -> int:
    try:
        parsed = int(value if value is not None else 0)
    except Exception:
        parsed = 0
    return max(0, parsed)


def _row_text(row: dict[str, Any], keys: list[str]) -> str:
    return " ".join(str(row.get(key) or "") for key in keys).lower()


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
        out = [
            row
            for row in out
            if normalized_status
            in _row_text(
                row,
                ["status", "action_status", "portfolio_status", "reason_contract_status", "event_status", "review_action", "severity"],
            )
        ]
    normalized_action = str(action or "").strip().upper()
    if normalized_action and normalized_action != "ALL":
        out = [
            row
            for row in out
            if normalized_action
            in _row_text(row, ["action_code", "action", "next_action", "action_type", "portfolio_action", "execution_intent"]).upper()
        ]
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
    "portfolio_status",
    "entry_date",
    "entry_price",
    "current_price",
    "pnl_pct",
    "invest_score_pct",
    "allocation_inr",
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
        compacted.append({key: row.get(key) for key in COMPACT_LIST_FIELDS if row.get(key) not in (None, "", [], {})})
    return compacted


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
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "status": "ok" if rows else "not_found",
        "kind": kind,
        "filters": filters,
        "rows": rows,
        "row_count": len(rows),
    }


def ensure_operator_api_errors_table() -> None:
    with db_session() as (_, cur):
        cur.execute(
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
            """
        )


def _safe_json_dumps(value: Any) -> str:
    try:
        return json.dumps(_json_ready(value), ensure_ascii=False, default=str)
    except Exception:
        return json.dumps({"serialization_error": True, "repr": repr(value)}, ensure_ascii=False)


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
        "error_message": str(exc),
        "traceback_tail": _tail_text("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)), max_chars=OPERATOR_API_ERROR_TRACE_CHARS),
        "request_context_json": _safe_json_dumps(request_context or {}),
        "load_ts": occurred_at,
    }
    try:
        ensure_operator_api_errors_table()
        upsert_to_db(pd.DataFrame([row]), OPERATOR_API_ERRORS_TABLE, unique_keys=["error_id"])
    except Exception:
        # Error auditing must not mask the API error being reported to the frontend.
        pass
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
        "error_message": message,
        "traceback_tail": None,
        "request_context_json": _safe_json_dumps(context or {}),
        "load_ts": occurred_at,
    }
    try:
        ensure_operator_api_errors_table()
        upsert_to_db(pd.DataFrame([row]), OPERATOR_API_ERRORS_TABLE, unique_keys=["error_id"])
    except Exception:
        pass


def build_operator_api_errors_payload(*, limit: int = 50) -> dict[str, Any]:
    if not _table_exists(OPERATOR_API_ERRORS_TABLE):
        return {
            "generated_at": pd.Timestamp.utcnow().isoformat(),
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
        row["request_context"] = _jsonish(row.pop("request_context_json", None))
    error_count = sum(1 for row in rows if int(row.get("status_code") or 500) >= 500)
    warn_count = len(rows) - error_count
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
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
        "status": "ok",
        "summary": _records(summary),
        "top_configs": _records(top_configs),
    }


def build_technical_threshold_promotion_review_payload(payload: dict[str, Any]) -> dict[str, Any]:
    setup_id = str(payload.get("setup_id") or "").strip()
    config_id = str(payload.get("config_id") or "").strip()
    if not setup_id:
        raise ValueError("setup_id is required")
    if not config_id:
        raise ValueError("config_id is required")
    return generate_promotion_review(
        setup_id=setup_id,
        config_id=config_id,
        model=str(payload.get("model") or "") or None,
        use_llm=bool(payload.get("use_llm", True)),
        persist=True,
    )


def build_technical_threshold_reviews_payload(*, limit: int = 25) -> dict[str, Any]:
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "status": "ok",
        "reviews": load_promotion_reviews(limit=limit),
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
    return record_manual_decision(
        reviewed_at=reviewed_at,
        setup_id=setup_id,
        config_id=config_id,
        decision=decision,  # type: ignore[arg-type]
        operator_id=str(payload.get("operator_id") or "") or None,
        decision_reason=str(payload.get("decision_reason") or "") or None,
    )


def build_events_payload(
    *,
    asof_date: str | None = None,
    limit: int = 50,
    offset: int = 0,
    symbol: str | None = None,
    status: str | None = None,
    search: str | None = None,
    compact: bool = False,
) -> dict[str, Any]:
    payload = load_operator_payload(asof_date=asof_date)
    events = _filter_rows(payload.get("watch_events") or [], symbol=symbol, status=status, search=search)
    operator_feed = _filter_rows(payload.get("operator_feed") or [], symbol=symbol, status=status, search=search)
    alerts = _filter_rows(payload.get("alerts") or [], symbol=symbol, status=status, search=search)
    event_page, event_meta = _page_rows(events, limit=limit, offset=offset)
    return {
        "generated_at": payload.get("generated_at"),
        "asof_date": payload.get("asof_date"),
        "events": _compact_list_rows(event_page, compact=compact),
        "operator_feed": _compact_list_rows(operator_feed[: _bounded_limit(limit, default=25)], compact=compact),
        "alerts": _compact_list_rows(alerts[: _bounded_limit(limit, default=25)], compact=compact),
        "meta": {
            "events": event_meta,
            "operator_feed": {"total": len(operator_feed), "returned": min(len(operator_feed), _bounded_limit(limit, default=25))},
            "alerts": {"total": len(alerts), "returned": min(len(alerts), _bounded_limit(limit, default=25))},
            "filters": {"symbol": symbol, "status": status, "search": search, "compact": compact},
        },
    }


def build_action_detail_payload(*, symbol: str | None = None, unique_id: str | None = None, setup_id: str | None = None, asof_date: str | None = None) -> dict[str, Any]:
    payload = load_operator_payload(asof_date=asof_date)
    rows = _payload_rows_by_kind(payload, "actions")
    rows = _filter_rows(rows, symbol=symbol, search=unique_id)
    normalized_setup = str(setup_id or "").strip().upper()
    if normalized_setup:
        rows = [row for row in rows if str(row.get("setup_id") or "").strip().upper() == normalized_setup]
    return _detail_payload("actions", rows, filters={"symbol": symbol, "unique_id": unique_id, "setup_id": setup_id, "asof_date": asof_date})


def build_portfolio_detail_payload(*, symbol: str, asof_date: str | None = None) -> dict[str, Any]:
    payload = load_operator_payload(asof_date=asof_date)
    rows = _filter_rows(_payload_rows_by_kind(payload, "portfolio"), symbol=symbol)
    return _detail_payload("portfolio", rows, filters={"symbol": symbol, "asof_date": asof_date})


def build_event_detail_payload(*, unique_id: str, asof_date: str | None = None) -> dict[str, Any]:
    payload = load_operator_payload(asof_date=asof_date)
    rows = [row for row in _payload_rows_by_kind(payload, "events") if str(row.get("unique_id") or "") == str(unique_id)]
    return _detail_payload("events", rows, filters={"unique_id": unique_id, "asof_date": asof_date})


def build_event_policy_payload(*, asof_date: str | None = None, action_type: str | None = None, limit: int = 100) -> dict[str, Any]:
    if not _table_exists(EVENT_POLICY_TABLE):
        return {
            "generated_at": pd.Timestamp.utcnow().isoformat(),
            "status": "missing_table",
            "summary": {"action_counts": {}, "policy_class_counts": {}},
            "rows": [],
        }
    clauses = ["1 = 1"]
    params: dict[str, Any] = {"limit": max(1, int(limit))}
    parsed_asof = _parse_asof_date(asof_date)
    if parsed_asof is not None:
        clauses.append("asof_date = %(asof_date)s")
        params["asof_date"] = parsed_asof
    else:
        clauses.append(f"asof_date = (SELECT MAX(asof_date) FROM {EVENT_POLICY_TABLE})")
    normalized_action = str(action_type or "").strip().upper()
    if normalized_action and normalized_action != "ALL":
        clauses.append("action_type = %(action_type)s")
        params["action_type"] = normalized_action
    where_sql = " AND ".join(clauses)
    rows = sql_to_df(
        f"""
        SELECT *
        FROM {EVENT_POLICY_TABLE}
        WHERE {where_sql}
        ORDER BY
            CASE action_type
                WHEN 'REDUCE_EXPOSURE_REVIEW' THEN 1
                WHEN 'BUY_WATCH' THEN 2
                WHEN 'MANUAL_REVIEW' THEN 3
                ELSE 4
            END,
            published_on DESC NULLS LAST,
            ABS(policy_score) DESC NULLS LAST
        LIMIT %(limit)s
        """,
        params=params,
        retries=3,
    )
    summary_df = sql_to_df(
        f"""
        SELECT action_type, policy_class, count(*) AS row_count
        FROM {EVENT_POLICY_TABLE}
        WHERE {where_sql}
        GROUP BY action_type, policy_class
        """,
        params={key: value for key, value in params.items() if key != "limit"},
        retries=3,
    )
    records = _records(rows)
    for row in records:
        row["checks"] = _jsonish(row.get("checks_json"))
        row["operator_notes"] = _jsonish(row.get("operator_notes_json"))
        row["llm_review"] = _jsonish(row.get("llm_review_json"))
        row["raw_context"] = _jsonish(row.get("raw_context_json"))
    action_counts: dict[str, int] = {}
    policy_class_counts: dict[str, int] = {}
    if not summary_df.empty:
        for item in summary_df.to_dict(orient="records"):
            count = int(item.get("row_count") or 0)
            action_counts[str(item.get("action_type") or "UNKNOWN")] = action_counts.get(str(item.get("action_type") or "UNKNOWN"), 0) + count
            policy_class_counts[str(item.get("policy_class") or "UNKNOWN")] = policy_class_counts.get(str(item.get("policy_class") or "UNKNOWN"), 0) + count
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "status": "ok",
        "asof_date": asof_date,
        "summary": {
            "action_counts": action_counts,
            "policy_class_counts": policy_class_counts,
            "row_count": int(sum(action_counts.values())),
        },
        "rows": records,
    }


def build_event_policy_evaluation_payload(*, limit: int = 100) -> dict[str, Any]:
    if not _table_exists(EVENT_POLICY_EVAL_SUMMARY_TABLE):
        return {
            "generated_at": pd.Timestamp.utcnow().isoformat(),
            "status": "missing_table",
            "summary": [],
        }
    df = sql_to_df(
        f"""
        SELECT *
        FROM {EVENT_POLICY_EVAL_SUMMARY_TABLE}
        WHERE evaluated_at = (
            SELECT MAX(evaluated_at)
            FROM {EVENT_POLICY_EVAL_SUMMARY_TABLE}
        )
        ORDER BY
            horizon_days,
            CASE recommendation
                WHEN 'candidate_policy_tighten_or_downgrade' THEN 1
                WHEN 'candidate_policy_strengthen' THEN 2
                WHEN 'monitor' THEN 3
                ELSE 4
            END,
            matured_count DESC NULLS LAST,
            avg_forward_return_after_cost DESC NULLS LAST
        LIMIT %(limit)s
        """,
        params={"limit": max(1, int(limit))},
        retries=3,
    )
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "status": "ok",
        "summary": _records(df),
    }


def build_event_trace_payload(unique_id: str) -> dict[str, Any]:
    return load_event_trace(unique_id)


def build_symbol_trace_payload(symbol: str, *, limit: int = 100) -> dict[str, Any]:
    return load_symbol_trace(symbol, limit=_bounded_limit(limit, default=100, maximum=500))


def _jsonish(value: Any) -> Any:
    if value is None:
        return {}
    if isinstance(value, (dict, list)):
        return value
    try:
        if pd.isna(value):
            return {}
    except Exception:
        pass
    try:
        return json.loads(str(value))
    except Exception:
        return {}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
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
    except Exception:
        pass
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
            }
        )

    return {
        "symbol": raw.get("symbol"),
        "unique_id": raw.get("unique_id"),
        "processing": processing,
        "decisions": decisions,
        "action_conflicts": conflicts,
        "raw_counts": {
            "processing": len(processing_rows),
            "traces": len(trace_rows),
            "steps": len(step_rows),
            "action_conflicts": len(conflict_rows),
        },
    }


def build_event_trace_summary_payload(unique_id: str) -> dict[str, Any]:
    if OPERATOR_API_TRACE_SUMMARY_CACHE_ENABLED:
        cached = load_materialized_trace_summary("event", unique_id, limit=TRACE_SUMMARY_DEFAULT_LIMIT)
        if cached is not None:
            return cached
        record_operator_api_marker(
            route="/api/events/{unique_id}/trace/summary",
            operation="build_event_trace_summary_payload",
            message="trace_summary_cache_miss:event",
            context={"unique_id": unique_id},
        )
    summary = normalize_trace_payload(load_event_trace(unique_id))
    summary["_trace_summary_cache"] = {"source": "live_fallback", "entity_type": "event", "entity_key": unique_id}
    return summary


def build_symbol_trace_summary_payload(symbol: str, *, limit: int = 100) -> dict[str, Any]:
    bounded_limit = _bounded_limit(limit, default=100, maximum=500)
    if OPERATOR_API_TRACE_SUMMARY_CACHE_ENABLED:
        cached = load_materialized_trace_summary("symbol", symbol, limit=bounded_limit)
        if cached is not None:
            return cached
        record_operator_api_marker(
            route="/api/symbols/{symbol}/trace/summary",
            operation="build_symbol_trace_summary_payload",
            message="trace_summary_cache_miss:symbol",
            context={"symbol": symbol, "limit": bounded_limit},
        )
    summary = normalize_trace_payload(load_symbol_trace(symbol, limit=bounded_limit))
    summary["_trace_summary_cache"] = {"source": "live_fallback", "entity_type": "symbol", "entity_key": str(symbol).upper(), "limit_rows": bounded_limit}
    return summary


def build_data_health_payload(*, asof_date: str | None = None) -> dict[str, Any]:
    payload = load_operator_payload(asof_date=asof_date)
    summary = payload.get("summary") or {}
    return {
        "generated_at": payload.get("generated_at"),
        "asof_date": payload.get("asof_date"),
        "summary": {
            "alert_count": summary.get("alert_count"),
            "action_count": summary.get("action_count"),
            "ts_eval_summary_count": summary.get("ts_eval_summary_count"),
        },
        "sync_state": payload.get("sync_state") or [],
        "runtime_processes": payload.get("runtime_processes") or [],
        "cron_status": payload.get("cron_status") or [],
    }


def build_operator_health_payload() -> dict[str, Any]:
    return build_operator_health()


def build_operations_smoke_payload() -> dict[str, Any]:
    payload = build_operator_health()
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "status": payload.get("status"),
        "operator_health": payload,
        "fix_hints": payload.get("fix_hints") or [],
        "read_only": True,
        "note": "This endpoint runs the same read-only health checks used by the operator CLI; it does not start ingestion, advisory, broker, or trading jobs.",
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


def build_cron_logs_payload(*, limit: int = 20, lines: int = 80) -> dict[str, Any]:
    log_dir = Path(CRON_LOG_DIR)
    if not log_dir.exists():
        return {
            "generated_at": pd.Timestamp.utcnow().isoformat(),
            "status": "missing_log_dir",
            "log_dir": str(log_dir),
            "logs": [],
        }
    files = sorted(
        [path for path in log_dir.glob("*.log") if path.is_file()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[: max(0, int(limit))]
    logs: list[dict[str, Any]] = []
    for path in files:
        stat = path.stat()
        tail = _tail_file(path, line_count=int(lines))
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
        "status": "ok",
        "log_dir": str(log_dir),
        "logs": logs,
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
    return payload


def build_event_model_artifacts_payload() -> dict[str, Any]:
    artifact_dir = Path(".cache/advisory_event_meta_model")
    try:
        manifest = build_artifact_manifest(
            artifact_dir=artifact_dir,
            model_basename="event_meta_model",
            s3_prefix=env.str("EVENT_MODEL_ARTIFACT_S3_PREFIX", "models/advisory_event_meta_model"),
        )
        manifest["status"] = "ok"
    except FileNotFoundError as exc:
        manifest = {
            "status": "missing_artifact",
            "error": str(exc),
            "artifact_dir": str(artifact_dir),
            "files": [],
        }
    latest_heads: list[dict[str, Any]] = []
    if manifest.get("latest_prefix"):
        try:
            from utils.store import AWS_BUCKET_NAME, _get_client

            s3 = _get_client()
            for item in manifest.get("files") or []:
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
                    latest_heads.append({"key": key, "status": "missing_or_error", "error": f"{type(exc).__name__}: {exc}"})
        except Exception as exc:
            latest_heads.append({"status": "s3_unavailable", "error": f"{type(exc).__name__}: {exc}"})
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "status": manifest.get("status"),
        "artifact": manifest,
        "latest_s3_heads": latest_heads,
        "read_only": True,
    }


def _python_cmd(*args: str) -> list[str]:
    return [sys.executable, *args]


OPERATOR_COMMAND_REGISTRY: dict[str, dict[str, Any]] = {
    "operator_health_skip_dhan": {
        "label": "Operator Health",
        "description": "Read-only health check for DB, Redis, cron logs, snapshots, optional dependencies, and frontend dependencies without initiating Dhan login.",
        "args": _python_cmd("-m", "advisory.operator_health", "--skip-dhan"),
        "risk": "safe_read_only",
        "dry_run": True,
        "timeout_seconds": 180,
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
}


def ensure_operator_command_runs_table() -> None:
    with db_session() as (_, cur):
        cur.execute(
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
            """
        )


def _tail_text(value: str, *, max_chars: int | None = None) -> str:
    max_len = max(1, int(max_chars or OPERATOR_COMMAND_OUTPUT_TAIL_CHARS))
    text = value or ""
    return text[-max_len:]


def persist_operator_command_run(row: dict[str, Any]) -> None:
    ensure_operator_command_runs_table()
    frame = pd.DataFrame([row])
    upsert_to_db(frame, OPERATOR_COMMAND_RUNS_TABLE, unique_keys=["run_id"], timescaledb_column="started_at")


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


def _json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    if isinstance(value, pd.Timestamp):
        return _ts(value)
    if hasattr(value, "isoformat") and not isinstance(value, str):
        try:
            return value.isoformat()
        except Exception:
            pass
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return value


def _manual_review_item(
    *,
    item_type: str,
    severity: str,
    status: str,
    title: str,
    reason: Any = None,
    source_table: str,
    source_key: Any = None,
    row: dict[str, Any] | None = None,
    symbol: Any = None,
    unique_id: Any = None,
    setup_id: Any = None,
    asof_date: Any = None,
    updated_at: Any = None,
) -> dict[str, Any]:
    raw = _json_ready(row or {})
    symbol_text = _text(symbol if symbol is not None else raw.get("symbol"))
    unique_id_text = _text(unique_id if unique_id is not None else raw.get("unique_id"))
    setup_id_text = _text(setup_id if setup_id is not None else raw.get("setup_id"))
    source_key_text = _text(source_key) or unique_id_text or symbol_text or setup_id_text or title
    return {
        "item_id": f"{item_type}:{source_table}:{source_key_text}",
        "item_type": item_type,
        "severity": severity,
        "status": status,
        "title": title,
        "reason": _text(reason) or "",
        "source_table": source_table,
        "source_key": source_key_text,
        "symbol": symbol_text,
        "unique_id": unique_id_text,
        "setup_id": setup_id_text,
        "asof_date": _ts(asof_date if asof_date is not None else raw.get("asof_date")),
        "updated_at": _ts(updated_at if updated_at is not None else raw.get("updated_at") or raw.get("load_ts") or raw.get("created_at") or raw.get("reviewed_at")),
        "raw": raw,
    }


def _safe_manual_query(source_name: str, query: str, *, params: dict[str, Any] | None = None, skipped: list[dict[str, str]]) -> pd.DataFrame:
    try:
        return sql_to_df(query, params=params or {}, retries=3)
    except Exception as exc:
        skipped.append({"source": source_name, "error": f"{type(exc).__name__}: {exc}"})
        return pd.DataFrame()


def ensure_manual_review_decisions_table() -> None:
    with db_session() as (_, cur):
        cur.execute(
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
            """
        )


def load_latest_manual_review_decisions(*, limit: int = 1000) -> dict[str, dict[str, Any]]:
    ensure_manual_review_decisions_table()
    df = sql_to_df(
        f"""
        SELECT DISTINCT ON (item_id) *
        FROM {MANUAL_REVIEW_DECISIONS_TABLE}
        ORDER BY item_id, decided_at DESC
        LIMIT %(limit)s
        """,
        params={"limit": max(1, int(limit))},
        retries=3,
    )
    decisions: dict[str, dict[str, Any]] = {}
    for row in _records(df):
        item_id = _text(row.get("item_id"))
        if not item_id:
            continue
        row["note"] = _jsonish(row.get("note_json"))
        row["item_snapshot"] = _jsonish(row.get("item_snapshot_json"))
        decisions[item_id] = row
    return decisions


def _append_latest_action_review_items(items: list[dict[str, Any]], skipped: list[dict[str, str]], *, limit: int) -> None:
    table = ACTION_RECOMMENDATIONS_TABLE
    if not _table_exists(table):
        skipped.append({"source": table, "error": "missing_table"})
        return
    df = _safe_manual_query(
        table,
        f"""
        SELECT *
        FROM {table}
        WHERE asof_date = (SELECT MAX(asof_date) FROM {table})
          AND (
            action_code = 'MANUAL_REVIEW'
            OR reason_contract_status IS DISTINCT FROM 'complete'
            OR execution_mode = 'review_only'
          )
        ORDER BY
            CASE action_code WHEN 'MANUAL_REVIEW' THEN 1 ELSE 2 END,
            updated_at DESC NULLS LAST,
            load_ts DESC NULLS LAST
        LIMIT %(limit)s
        """,
        params={"limit": max(1, int(limit))},
        skipped=skipped,
    )
    for row in _records(df):
        status = _text(row.get("action_code")) or "manual_review"
        reason = row.get("action_reason") or row.get("reason") or row.get("reason_detail") or row.get("recommendation_reason")
        items.append(
            _manual_review_item(
                item_type="action_manual_review",
                severity="review",
                status=status,
                title=f"{row.get('symbol') or 'Symbol'} action needs review",
                reason=reason,
                source_table=table,
                source_key=f"{row.get('asof_date')}:{row.get('symbol')}:{row.get('setup_id')}",
                row=row,
            )
        )


def _append_event_policy_review_items(items: list[dict[str, Any]], skipped: list[dict[str, str]], *, limit: int) -> None:
    table = EVENT_POLICY_TABLE
    if not _table_exists(table):
        skipped.append({"source": table, "error": "missing_table"})
        return
    df = _safe_manual_query(
        table,
        f"""
        SELECT *
        FROM {table}
        WHERE asof_date = (SELECT MAX(asof_date) FROM {table})
          AND (
            action_type = 'MANUAL_REVIEW'
            OR action_status ILIKE '%%review%%'
            OR action_status ILIKE '%%blocked%%'
          )
        ORDER BY published_on DESC NULLS LAST, ABS(policy_score) DESC NULLS LAST
        LIMIT %(limit)s
        """,
        params={"limit": max(1, int(limit))},
        skipped=skipped,
    )
    for row in _records(df):
        checks = _jsonish(row.get("checks_json"))
        notes = _jsonish(row.get("operator_notes_json"))
        reason = row.get("action_reason") or row.get("reason") or (notes.get("summary") if isinstance(notes, dict) else None) or (checks.get("reason") if isinstance(checks, dict) else None)
        items.append(
            _manual_review_item(
                item_type="event_policy_manual_review",
                severity="review",
                status=_text(row.get("action_type")) or "MANUAL_REVIEW",
                title=f"{row.get('symbol') or row.get('entity') or 'Event'} policy review",
                reason=reason,
                source_table=table,
                source_key=row.get("unique_id") or row.get("event_id"),
                row=row,
            )
        )


def _append_action_conflict_items(items: list[dict[str, Any]], skipped: list[dict[str, str]], *, limit: int) -> None:
    table = ACTION_CONFLICTS_TABLE
    if not _table_exists(table):
        skipped.append({"source": table, "error": "missing_table"})
        return
    df = _safe_manual_query(
        table,
        f"""
        SELECT *
        FROM {table}
        WHERE asof_date = (SELECT MAX(asof_date) FROM {table})
        ORDER BY updated_at DESC NULLS LAST, asof_date DESC NULLS LAST
        LIMIT %(limit)s
        """,
        params={"limit": max(1, int(limit))},
        skipped=skipped,
    )
    for row in _records(df):
        reason = row.get("lost_reason") or f"{row.get('winning_action_code')} won over {row.get('losing_action_code')}"
        items.append(
            _manual_review_item(
                item_type="action_conflict",
                severity="warning",
                status="conflict_resolved",
                title=f"{row.get('symbol') or 'Symbol'} had conflicting action signals",
                reason=reason,
                source_table=table,
                source_key=f"{row.get('asof_date')}:{row.get('symbol')}:{row.get('losing_setup_id')}:{row.get('losing_unique_id')}",
                row=row,
            )
        )


def _append_threshold_review_items(items: list[dict[str, Any]], skipped: list[dict[str, str]], *, limit: int) -> None:
    try:
        reviews = load_promotion_reviews(limit=max(1, int(limit)))
    except Exception as exc:
        skipped.append({"source": "technical_threshold_promotion_reviews", "error": f"{type(exc).__name__}: {exc}"})
        return
    for row in reviews:
        if _text(row.get("manual_decision")):
            continue
        reason = row.get("review_status") or row.get("review_error") or "Threshold calibration needs operator decision before promotion."
        items.append(
            _manual_review_item(
                item_type="threshold_review",
                severity="review",
                status=_text(row.get("review_status")) or "pending_operator_decision",
                title=f"{row.get('setup_id') or 'Setup'} threshold review",
                reason=reason,
                source_table="technical_threshold_promotion_reviews",
                source_key=f"{row.get('reviewed_at')}:{row.get('setup_id')}:{row.get('config_id')}",
                row=row,
                updated_at=row.get("reviewed_at"),
            )
        )


def _append_execution_blocker_items(items: list[dict[str, Any]], skipped: list[dict[str, str]], *, limit: int) -> None:
    table = EXECUTION_TABLE
    if not _table_exists(table):
        skipped.append({"source": table, "error": "missing_table"})
        return
    df = _safe_manual_query(
        table,
        f"""
        SELECT *
        FROM {table}
        WHERE asof_date = (SELECT MAX(asof_date) FROM {table})
          AND execution_status IN ('submit_blocked', 'submit_error', 'reconcile_error', 'invalid_order')
        ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
        LIMIT %(limit)s
        """,
        params={"limit": max(1, int(limit))},
        skipped=skipped,
    )
    for row in _records(df):
        items.append(
            _manual_review_item(
                item_type="execution_blocker",
                severity="error",
                status=_text(row.get("execution_status")) or "execution_issue",
                title=f"{row.get('symbol') or 'Order'} execution blocker",
                reason=row.get("execution_reason") or row.get("error") or row.get("status_reason"),
                source_table=table,
                source_key=row.get("execution_id") or f"{row.get('asof_date')}:{row.get('symbol')}:{row.get('action_code')}",
                row=row,
            )
        )


def _append_processing_failure_items(items: list[dict[str, Any]], skipped: list[dict[str, str]], *, limit: int) -> None:
    table = "advisory_event_processing_runs"
    if not _table_exists(table):
        skipped.append({"source": table, "error": "missing_table"})
        return
    df = _safe_manual_query(
        table,
        f"""
        SELECT *
        FROM {table}
        WHERE status IN ('failed', 'error')
           OR error IS NOT NULL
        ORDER BY COALESCE(completed_at, started_at, load_ts) DESC NULLS LAST
        LIMIT %(limit)s
        """,
        params={"limit": max(1, int(limit))},
        skipped=skipped,
    )
    for row in _records(df):
        items.append(
            _manual_review_item(
                item_type="event_processing_failure",
                severity="error",
                status=_text(row.get("status")) or "failed",
                title=f"{row.get('stage') or 'Event processing'} failed",
                reason=row.get("error") or row.get("reason"),
                source_table=table,
                source_key=row.get("run_id") or row.get("unique_id"),
                row=row,
            )
        )


def _append_announcement_failure_items(items: list[dict[str, Any]], skipped: list[dict[str, str]], *, limit: int) -> None:
    table = "announcement_pipeline_documents"
    if not _table_exists(table):
        skipped.append({"source": table, "error": "missing_table"})
        return
    df = _safe_manual_query(
        table,
        f"""
        SELECT *
        FROM {table}
        WHERE ocr_status = 'failed'
           OR parse_status = 'failed'
           OR last_error IS NOT NULL
        ORDER BY COALESCE(published_at, created_at, updated_at) DESC NULLS LAST
        LIMIT %(limit)s
        """,
        params={"limit": max(1, int(limit))},
        skipped=skipped,
    )
    for row in _records(df):
        items.append(
            _manual_review_item(
                item_type="announcement_failure",
                severity="warning",
                status=_text(row.get("parse_status")) or _text(row.get("ocr_status")) or "failed",
                title=f"{row.get('ticker') or row.get('symbol') or 'Announcement'} extraction issue",
                reason=row.get("last_error") or row.get("ocr_error") or row.get("parse_error"),
                source_table=table,
                source_key=row.get("unique_id"),
                row=row,
                symbol=row.get("ticker") or row.get("symbol"),
                updated_at=row.get("updated_at") or row.get("published_at"),
            )
        )


def build_manual_review_payload(*, limit: int = 100) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    per_source_limit = max(1, int(limit))
    _append_execution_blocker_items(items, skipped, limit=per_source_limit)
    _append_latest_action_review_items(items, skipped, limit=per_source_limit)
    _append_event_policy_review_items(items, skipped, limit=per_source_limit)
    _append_action_conflict_items(items, skipped, limit=per_source_limit)
    _append_threshold_review_items(items, skipped, limit=per_source_limit)
    _append_processing_failure_items(items, skipped, limit=per_source_limit)
    _append_announcement_failure_items(items, skipped, limit=per_source_limit)
    latest_decisions: dict[str, dict[str, Any]] = {}
    try:
        latest_decisions = load_latest_manual_review_decisions(limit=max(per_source_limit * 5, 1000))
    except Exception as exc:
        skipped.append({"source": MANUAL_REVIEW_DECISIONS_TABLE, "error": f"{type(exc).__name__}: {exc}"})
    active_items: list[dict[str, Any]] = []
    closed_count = 0
    annotated_count = 0
    for item in items:
        decision = latest_decisions.get(str(item.get("item_id") or ""))
        if decision:
            item["latest_operator_decision"] = _json_ready(decision)
            if str(decision.get("decision") or "").strip().lower() in MANUAL_REVIEW_CLOSING_DECISIONS:
                closed_count += 1
                continue
            annotated_count += 1
        active_items.append(item)
    items = active_items
    items.sort(key=lambda row: row.get("updated_at") or row.get("asof_date") or "", reverse=True)
    trimmed = items[:per_source_limit]
    by_type: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for row in trimmed:
        by_type[row["item_type"]] = by_type.get(row["item_type"], 0) + 1
        by_severity[row["severity"]] = by_severity.get(row["severity"], 0) + 1
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "status": "ok",
        "summary": {
            "total_items": len(trimmed),
            "untrimmed_items": len(items),
            "closed_by_operator": closed_count,
            "annotated_by_operator": annotated_count,
            "by_type": by_type,
            "by_severity": by_severity,
            "skipped_sources": skipped,
        },
        "items": trimmed,
    }


def record_manual_review_decision_payload(payload: dict[str, Any]) -> dict[str, Any]:
    item = payload.get("item")
    if not isinstance(item, dict):
        item = {}
    item_id = _text(payload.get("item_id")) or _text(item.get("item_id"))
    if not item_id:
        raise ValueError("item_id is required")
    decision = str(payload.get("decision") or "").strip().lower()
    if decision not in MANUAL_REVIEW_ALLOWED_DECISIONS:
        raise ValueError(f"decision must be one of: {', '.join(sorted(MANUAL_REVIEW_ALLOWED_DECISIONS))}")
    rationale = _text(payload.get("rationale"))
    if decision != "add_operator_note" and not rationale:
        raise ValueError("rationale is required for this decision")
    decided_at = pd.Timestamp.utcnow()
    note = payload.get("note") if isinstance(payload.get("note"), dict) else {}
    if payload.get("follow_up_event") is not None:
        note["follow_up_event"] = _text(payload.get("follow_up_event"))
    row = pd.DataFrame(
        [
            {
                "decided_at": decided_at,
                "item_id": item_id,
                "item_type": _text(payload.get("item_type")) or _text(item.get("item_type")),
                "source_table": _text(payload.get("source_table")) or _text(item.get("source_table")),
                "source_key": _text(payload.get("source_key")) or _text(item.get("source_key")),
                "symbol": _text(payload.get("symbol")) or _text(item.get("symbol")),
                "unique_id": _text(payload.get("unique_id")) or _text(item.get("unique_id")),
                "setup_id": _text(payload.get("setup_id")) or _text(item.get("setup_id")),
                "decision": decision,
                "operator_id": _text(payload.get("operator_id")),
                "rationale": rationale,
                "follow_up_event": _text(payload.get("follow_up_event")),
                "note_json": json.dumps(_json_ready(note), ensure_ascii=False, sort_keys=True, default=str),
                "item_snapshot_json": json.dumps(_json_ready(item), ensure_ascii=False, sort_keys=True, default=str),
                "load_ts": decided_at,
            }
        ]
    )
    ensure_manual_review_decisions_table()
    upsert_to_db(row, MANUAL_REVIEW_DECISIONS_TABLE, unique_keys=["item_id", "decided_at"], timescaledb_column="decided_at")
    return {
        "status": "ok",
        "decided_at": decided_at.isoformat(),
        "item_id": item_id,
        "decision": decision,
        "closing_decision": decision in MANUAL_REVIEW_CLOSING_DECISIONS,
        "note": "Decision recorded only. No config, strategy, broker, or trading behavior was changed.",
    }


def build_hypotheses_payload(*, limit: int = 100) -> dict[str, Any]:
    hypotheses = load_hypotheses().head(max(0, int(limit)))
    matches = load_matches(limit=max(0, int(limit)))
    action_plans = load_action_plans(limit=max(0, int(limit)))
    promotion_audits = []
    if not hypotheses.empty:
        for hypothesis_id in hypotheses["hypothesis_id"].dropna().astype(str).head(max(0, int(limit))).tolist():
            audit = latest_promotion_audit(hypothesis_id)
            if audit:
                promotion_audits.append(audit)
    return {
        "hypotheses": hypotheses.to_dict(orient="records") if not hypotheses.empty else [],
        "matches": matches.to_dict(orient="records") if not matches.empty else [],
        "action_plans": action_plans.to_dict(orient="records") if not action_plans.empty else [],
        "promotion_audits": promotion_audits,
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
                    except ValueError:
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
            except Exception:
                # Slow logging must never turn a successful operator API response into a failure.
                pass

    def _guard(callable_obj, *, route: str | None = None, **kwargs):
        operation = getattr(callable_obj, "__name__", str(callable_obj))
        try:
            return callable_obj(**kwargs)
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

    @app.get("/api/health")
    def health():
        return build_health_payload()

    @app.get("/api/health/details")
    def health_details():
        return _guard(build_operator_health_payload, route="/api/health/details")

    @app.get("/api/operations/smoke")
    def operations_smoke():
        return _guard(build_operations_smoke_payload, route="/api/operations/smoke")

    @app.get("/api/operations/cron-logs")
    def operations_cron_logs(limit: int = Query(default=20, ge=1, le=100), lines: int = Query(default=80, ge=1, le=300)):
        return _guard(build_cron_logs_payload, route="/api/operations/cron-logs", limit=limit, lines=lines)

    @app.get("/api/operations/commands")
    def operations_commands(limit: int = Query(default=25, ge=1, le=100)):
        return _guard(build_operator_commands_payload, route="/api/operations/commands", limit=limit)

    @app.post("/api/operations/commands/run")
    def operations_command_run(payload: dict[str, Any] = Body(...)):
        return _guard(run_operator_command_payload, route="/api/operations/commands/run", payload=payload)

    @app.get("/api/operations/api-errors")
    def operations_api_errors(limit: int = Query(default=50, ge=1, le=200)):
        return _guard(build_operator_api_errors_payload, route="/api/operations/api-errors", limit=limit)

    @app.post("/api/operations/slow-issues/status")
    def operations_slow_issue_status(payload: dict[str, Any] = Body(...)):
        return _guard(update_slow_issue_payload, route="/api/operations/slow-issues/status", payload=payload)

    @app.get("/api/research/event-model-promotion-check")
    def research_event_model_promotion_check():
        return _guard(build_event_model_promotion_check_payload, route="/api/research/event-model-promotion-check")

    @app.get("/api/research/event-model-artifacts")
    def research_event_model_artifacts():
        return _guard(build_event_model_artifacts_payload, route="/api/research/event-model-artifacts")

    @app.get("/api/manual-review")
    def manual_review(limit: int = Query(default=100, ge=1, le=500)):
        return _guard(build_manual_review_payload, route="/api/manual-review", limit=limit)

    @app.post("/api/manual-review/decision")
    def manual_review_decision(payload: dict[str, Any] = Body(...)):
        return _guard(record_manual_review_decision_payload, route="/api/manual-review/decision", payload=payload)

    @app.get("/api/summary")
    def summary(asof_date: str | None = None):
        return _guard(build_summary_payload, route="/api/summary", asof_date=asof_date)

    @app.get("/api/home")
    def home(asof_date: str | None = None):
        return _guard(build_home_payload, route="/api/home", asof_date=asof_date)

    @app.get("/api/actions")
    def actions(
        asof_date: str | None = None,
        limit: int = Query(default=50, ge=0, le=500),
        offset: int = Query(default=0, ge=0),
        symbol: str | None = None,
        action: str | None = None,
        status: str | None = None,
        search: str | None = None,
        compact: bool = Query(default=False),
    ):
        return _guard(build_actions_payload, route="/api/actions", asof_date=asof_date, limit=limit, offset=offset, symbol=symbol, action=action, status=status, search=search, compact=compact)

    @app.get("/api/actions/detail")
    def action_detail(symbol: str | None = None, unique_id: str | None = None, setup_id: str | None = None, asof_date: str | None = None):
        return _guard(build_action_detail_payload, route="/api/actions/detail", symbol=symbol, unique_id=unique_id, setup_id=setup_id, asof_date=asof_date)

    @app.get("/api/portfolio")
    def portfolio(
        asof_date: str | None = None,
        limit: int = Query(default=50, ge=0, le=500),
        offset: int = Query(default=0, ge=0),
        symbol: str | None = None,
        status: str | None = None,
        search: str | None = None,
        compact: bool = Query(default=False),
    ):
        return _guard(build_portfolio_payload, route="/api/portfolio", asof_date=asof_date, limit=limit, offset=offset, symbol=symbol, status=status, search=search, compact=compact)

    @app.get("/api/portfolio/{symbol}/detail")
    def portfolio_detail(symbol: str, asof_date: str | None = None):
        return _guard(build_portfolio_detail_payload, route="/api/portfolio/{symbol}/detail", symbol=symbol, asof_date=asof_date)

    @app.get("/api/watchlist")
    def watchlist(asof_date: str | None = None):
        return _guard(build_watchlist_payload, route="/api/watchlist", asof_date=asof_date)

    @app.get("/api/market-context")
    def market_context(asof_date: str | None = None, limit: int = Query(default=50, ge=0, le=500)):
        return _guard(build_market_context_payload, route="/api/market-context", asof_date=asof_date, limit=limit)

    @app.get("/api/technical-calibration")
    def technical_calibration(limit: int = Query(default=25, ge=1, le=100)):
        return _guard(build_technical_calibration_payload, route="/api/technical-calibration", limit=limit)

    @app.post("/api/technical-calibration/promotion-review")
    def technical_calibration_promotion_review(payload: dict[str, Any]):
        return _guard(build_technical_threshold_promotion_review_payload, route="/api/technical-calibration/promotion-review", payload=payload)

    @app.get("/api/technical-calibration/promotion-reviews")
    def technical_calibration_promotion_reviews(limit: int = Query(default=25, ge=1, le=100)):
        return _guard(build_technical_threshold_reviews_payload, route="/api/technical-calibration/promotion-reviews", limit=limit)

    @app.post("/api/technical-calibration/promotion-review/decision")
    def technical_calibration_promotion_review_decision(payload: dict[str, Any]):
        return _guard(build_technical_threshold_review_decision_payload, route="/api/technical-calibration/promotion-review/decision", payload=payload)

    @app.get("/api/events")
    def events(
        asof_date: str | None = None,
        limit: int = Query(default=50, ge=0, le=500),
        offset: int = Query(default=0, ge=0),
        symbol: str | None = None,
        status: str | None = None,
        search: str | None = None,
        compact: bool = Query(default=False),
    ):
        return _guard(build_events_payload, route="/api/events", asof_date=asof_date, limit=limit, offset=offset, symbol=symbol, status=status, search=search, compact=compact)

    @app.get("/api/events/{unique_id}/detail")
    def event_detail(unique_id: str, asof_date: str | None = None):
        return _guard(build_event_detail_payload, route="/api/events/{unique_id}/detail", unique_id=unique_id, asof_date=asof_date)

    @app.get("/api/event-policy")
    def event_policy(asof_date: str | None = None, action_type: str | None = None, limit: int = Query(default=100, ge=1, le=500)):
        return _guard(build_event_policy_payload, route="/api/event-policy", asof_date=asof_date, action_type=action_type, limit=limit)

    @app.get("/api/event-policy/evaluation")
    def event_policy_evaluation(limit: int = Query(default=100, ge=1, le=500)):
        return _guard(build_event_policy_evaluation_payload, route="/api/event-policy/evaluation", limit=limit)

    @app.get("/api/events/{unique_id}/trace")
    def event_trace(unique_id: str):
        return _guard(build_event_trace_payload, route="/api/events/{unique_id}/trace", unique_id=unique_id)

    @app.get("/api/events/{unique_id}/trace/summary")
    def event_trace_summary(unique_id: str):
        return _guard(build_event_trace_summary_payload, route="/api/events/{unique_id}/trace/summary", unique_id=unique_id)

    @app.get("/api/symbols/{symbol}/trace")
    def symbol_trace(symbol: str, limit: int = Query(default=100, ge=1, le=500)):
        return _guard(build_symbol_trace_payload, route="/api/symbols/{symbol}/trace", symbol=symbol, limit=limit)

    @app.get("/api/symbols/{symbol}/trace/summary")
    def symbol_trace_summary(symbol: str, limit: int = Query(default=100, ge=1, le=500)):
        return _guard(build_symbol_trace_summary_payload, route="/api/symbols/{symbol}/trace/summary", symbol=symbol, limit=limit)

    @app.get("/api/data-health")
    def data_health(asof_date: str | None = None):
        return _guard(build_data_health_payload, route="/api/data-health", asof_date=asof_date)

    @app.get("/api/hypotheses")
    def hypotheses(limit: int = Query(default=100, ge=0, le=500)):
        return _guard(build_hypotheses_payload, route="/api/hypotheses", limit=limit)

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


try:
    app = create_app()
except RuntimeError:
    app = None


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
