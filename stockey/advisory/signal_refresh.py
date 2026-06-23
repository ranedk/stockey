from __future__ import annotations

import argparse
import json
import uuid
from typing import Any

import pandas as pd

from advisory.action_recommender import TABLE_NAME as ACTIONS_TABLE
from advisory.causal_event_memory import TABLE_NAME as CAUSAL_EVENT_MEMORY_TABLE
from advisory.causal_event_memory_evaluator import SUMMARY_TABLE as CAUSAL_EVENT_MEMORY_SUMMARY_TABLE
from advisory.decision_trace import append_trace, append_trace_step, safe_trace_call
from advisory.event_evidence_store import ANNOUNCEMENT_CONTEXT_OVERLAYS_TABLE, BHAVCOPY_CONTEXT_OVERLAYS_TABLE
from advisory.event_policy import TABLE_NAME as EVENT_POLICY_TABLE
from advisory.exchange_context_overlays import EXCHANGE_CONTEXT_OVERLAYS_TABLE
from advisory.fallback_telemetry import record_local_fallback_event
from advisory.macro_context_overlays import MACRO_CONTEXT_OVERLAYS_TABLE, macro_sector_alias_values_sql
from advisory.market_context import UNIVERSE_TABLE as MARKET_CONTEXT_UNIVERSE_TABLE
from advisory.news_theme_engine import THEME_CONTEXT_OVERLAYS_TABLE, theme_sector_alias_values_sql
from advisory.portfolio_engine import PORTFOLIO_TABLE
from advisory.position_lifecycle import LIFECYCLE_TABLE, REBALANCE_TABLE
from advisory.sync_state import load_sync_state, persist_sync_state, publish_bus_message
from advisory.trace_summary_store import build_event_summary, build_symbol_summary
from advisory.wait_signals import WAIT_SIGNAL_MATCHES_TABLE, match_wait_signals
from advisory.watchlist_builder import TABLE_NAME as WATCHLIST_TABLE
from advisory.watchlist_builder import load_context_family_reliability
from advisory.watchlist_builder import load_context_overlay_watch_candidates
from advisory.watchlist_builder import load_negative_context_overlay_suppression_candidates
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration


TABLE_NAME = "advisory_signal_refresh_actions"
SIGNAL_REFRESH_SCHEMA_MIGRATION_ID = "20260611_advisory_signal_refresh_actions_base"
ROUTER_ACTIONS_TABLE = "advisory_live_router_actions"
STATE_SOURCE_NAME = "advisory:signal_refresh"
CONTEXT_OVERLAY_STATE_SOURCE_NAME = "advisory:signal_refresh:context_overlays"
THEME_CONTEXT_STATE_SOURCE_NAME = CONTEXT_OVERLAY_STATE_SOURCE_NAME
CONTEXT_OVERLAY_SIGNAL_REFRESH_VERSION = "context_overlay_signal_refresh_v2_theme_sector_aliases"
CAUSAL_MEMORY_STATE_SOURCE_NAME = "advisory:signal_refresh:causal_memory"
CAUSAL_MEMORY_MIN_DECAYED_PRESSURE_SCORE = 0.35
CAUSAL_MEMORY_MIN_EXCESS_DIRECTIONAL_HELPFULNESS_SCORE = 0.0
CAUSAL_MEMORY_MIN_EXCESS_DIRECTION_HIT_RATE = 0.55
CAUSAL_MEMORY_LOOKBACK_DAYS = 14
CONTEXT_RELIABILITY_DERISK_SUPPRESS_CLASSES = {
    "hurts_or_no_lift",
    "negative_after_cost",
    "inconsistent_or_horizon_sensitive",
    "needs_benchmark_attribution",
    "benchmark_beta_not_overlay_alpha",
}

CONTEXT_OVERLAY_SIGNAL_SOURCES = [
    {"source": "announcement_context", "table": ANNOUNCEMENT_CONTEXT_OVERLAYS_TABLE, "date_column": "published_on", "required_columns": {"symbol", "published_on", "direction"}},
    {"source": "exchange_context", "table": EXCHANGE_CONTEXT_OVERLAYS_TABLE, "date_column": "asof_date", "required_columns": {"symbol", "asof_date", "direction"}},
    {"source": "bhavcopy_context", "table": BHAVCOPY_CONTEXT_OVERLAYS_TABLE, "date_column": "asof_date", "required_columns": {"symbol", "asof_date", "direction"}},
    {"source": "theme_context", "table": THEME_CONTEXT_OVERLAYS_TABLE, "date_column": "asof_date", "required_columns": {"asof_date", "direction", "sector_name", "sector_code"}},
    {"source": "macro_context", "table": MACRO_CONTEXT_OVERLAYS_TABLE, "date_column": "asof_date", "required_columns": {"asof_date", "direction", "sector_name", "sector_code"}},
]

CONTEXT_SOURCE_TABLES = {
    str(item["source"]): str(item["table"])
    for item in CONTEXT_OVERLAY_SIGNAL_SOURCES
}

SIGNAL_REFRESH_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        refresh_id TEXT PRIMARY KEY,
        refreshed_at TIMESTAMPTZ NOT NULL,
        asof_date TIMESTAMPTZ,
        symbol TEXT NOT NULL,
        unique_id TEXT,
        reason TEXT,
        signal_action TEXT NOT NULL,
        signal_status TEXT,
        signal_source TEXT,
        confidence DOUBLE PRECISION,
        action_reason TEXT,
        effect_type TEXT,
        effect_summary TEXT,
        previous_action TEXT,
        action_changed BOOLEAN,
        authority_scope TEXT,
        portfolio_authority TEXT,
        broker_execution_allowed BOOLEAN,
        full_advisory_required BOOLEAN,
        action_payload_json TEXT,
        trace_id TEXT,
        dry_run BOOLEAN,
        load_ts TIMESTAMPTZ
    )
    """,
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS effect_type TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS effect_summary TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS previous_action TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS action_changed BOOLEAN",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS authority_scope TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS portfolio_authority TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS broker_execution_allowed BOOLEAN",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS full_advisory_required BOOLEAN",
    f"CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_symbol_refreshed ON {TABLE_NAME} (symbol, refreshed_at DESC)",
    f"CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_unique_id ON {TABLE_NAME} (unique_id)",
]

SIGNAL_REFRESH_AUTHORITY_CONTRACT = {
    "authority_scope": "review_input_only",
    "portfolio_authority": "none",
    "broker_execution_allowed": False,
    "full_advisory_required": True,
}

EXIT_ACTIONS = {"SELL", "FULL_EXIT", "EMERGENCY_EXIT", "PARTIAL_SELL", "PARTIAL_EXIT", "REDUCE", "REDUCE_REVIEW", "REDUCE_EXPOSURE_REVIEW", "GO_CASH_REVIEW"}
BUY_ACTIONS = {"BUY", "BUY_MORE", "ADD_ON_PULLBACK", "BUY_TRIGGERED"}
WATCH_ACTIONS = {"WATCH", "WATCHLIST", "NEAR_PIVOT", "READY", "MANUAL_REVIEW"}
WAIT_SIGNAL_NEGATIVE_ACTIONS = {"REDUCE_EXPOSURE_REVIEW", "GO_CASH_REVIEW", "FULL_EXIT", "PARTIAL_EXIT", "SELL", "PARTIAL_SELL", "REDUCE", "REDUCE_REVIEW"}
WAIT_SIGNAL_POSITIVE_ACTIONS = {"BUY", "BUY_MORE", "BUY_TRIGGERED", "ADD_ON_PULLBACK", "BUY_WATCH", "WATCH_SYMBOLS", "ADD_TO_WATCHLIST", "WATCH"}
ROUTER_NEGATIVE_ALERTS = {"POSITION_INVALIDATION_HIT", "STOP_HIT", "INVALIDATION_HIT"}
ROUTER_POSITIVE_ALERTS = {"ENTRY_ZONE_HIT", "BREAKOUT_ABOVE_RANGE"}


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def context_overlay_source_contract(
    *,
    context_source: Any,
    overlay_id: Any,
    symbol: Any = None,
    event_class: Any = None,
    direction: Any = None,
    asof_date: Any = None,
) -> dict[str, Any]:
    source = _text(context_source) or "context_overlay"
    source_table = CONTEXT_SOURCE_TABLES.get(source)
    source_key = _text(overlay_id)
    return {
        key: value
        for key, value in {
            "reference_type": "context_overlay_row",
            "context_source": source,
            "context_overlay_id": source_key,
            "source_table": source_table,
            "source_key": source_key,
            "symbol": _text(symbol),
            "event_class": _text(event_class),
            "direction": _text(direction),
            "asof_date": _text(asof_date),
            "authority_scope": SIGNAL_REFRESH_AUTHORITY_CONTRACT["authority_scope"],
            "portfolio_authority": SIGNAL_REFRESH_AUTHORITY_CONTRACT["portfolio_authority"],
            "broker_execution_allowed": False,
            "full_advisory_required": True,
            "policy_effect": "review_input_only_no_trade_authority",
        }.items()
        if value not in (None, "", [], {})
    }


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
            module="advisory.signal_refresh",
            fallback_type="signal_refresh_missing_check_failed",
            source="parse_jsonish",
            severity="warn",
            reason="Signal refresh could not evaluate missingness for a stored JSON value and continued parsing.",
            error=exc,
            metadata={"value_type": type(value).__name__, "default_type": type(default).__name__},
        )
    try:
        return json.loads(str(value))
    except Exception as exc:
        if isinstance(value, str) and value.strip():
            record_local_fallback_event(
                module="advisory.signal_refresh",
                fallback_type="signal_refresh_json_parse_failed",
                source="json_context",
                severity="warn",
                reason="Signal refresh could not parse stored JSON context and used the provided default.",
                error=exc,
                metadata={"default_type": type(default).__name__, "value_length": len(value), "value_excerpt": value[:240]},
            )
        return default


def _first_watch_reason_payload(item: dict[str, Any]) -> dict[str, Any]:
    raw = item.get("watch_reasons")
    if raw is None:
        raw = item.get("watch_reasons_json")
    reasons = _parse_jsonish(raw, [])
    if isinstance(reasons, dict):
        return reasons
    if isinstance(reasons, list) and reasons and isinstance(reasons[0], dict):
        return reasons[0]
    return {}


def _compact_watch_reason_audit(item: dict[str, Any]) -> dict[str, Any]:
    reason = _first_watch_reason_payload(item)
    return {
        key: reason.get(key)
        for key in [
            "watch_priority_score",
            "breakout_watch_threshold",
            "context_class_blocks_breakout",
            "context_sector_reliability_classification",
            "context_sector_blocks_breakout",
            "watch_breakout_priority_blocked",
            "watch_breakout_reliability_classification",
            "technical_confirmation_plan",
        ]
        if reason.get(key) is not None
    }


def _text(value: Any, default: str | None = None) -> str | None:
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.signal_refresh",
            fallback_type="signal_refresh_text_missing_check_failed",
            source="text",
            severity="warn",
            reason="Signal-refresh normalization could not evaluate missingness while normalizing text and kept string conversion fallback.",
            error=exc,
            metadata={"value_type": type(value).__name__},
        )
    text = str(value).strip()
    if text.lower() in {"", "nan", "none", "null", "<na>"}:
        return default
    return text


def _num(value: Any) -> float | None:
    out = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(out) else float(out)


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "t", "yes", "y", "on"}


def _ts(value: Any) -> pd.Timestamp | None:
    out = pd.to_datetime(value, utc=True, errors="coerce")
    return None if pd.isna(out) else out


def _date(value: Any = None) -> pd.Timestamp:
    out = pd.to_datetime(value or pd.Timestamp.utcnow(), utc=True, errors="coerce")
    if pd.isna(out):
        out = pd.Timestamp.utcnow()
    return out.normalize()


def _point_in_time_cutoff(value: Any = None) -> tuple[pd.Timestamp, str]:
    ts = pd.to_datetime(value or pd.Timestamp.utcnow(), utc=True, errors="coerce")
    if pd.isna(ts):
        ts = pd.Timestamp.utcnow()
    if ts == ts.normalize():
        return ts + pd.Timedelta(days=1), "<"
    return ts, "<="


def _jsonish(value: Any, default: Any = None, *, source: str = "signal_refresh_json_context") -> Any:
    if isinstance(value, (dict, list)):
        return value
    text = _text(value)
    if not text:
        return {} if default is None else default
    try:
        return json.loads(text)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.signal_refresh",
            fallback_type="signal_refresh_json_parse_failed",
            source=source,
            severity="warn",
            reason="Signal refresh could not parse stored JSON context and used the provided default.",
            error=exc,
            metadata={
                "default_type": type(default).__name__,
                "value_length": len(text),
                "value_excerpt": text[:240],
            },
        )
        return {} if default is None else default


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
        return not df.empty
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.signal_refresh",
            fallback_type="signal_refresh_table_exists_check_failed",
            source=table_name,
            severity="warn",
            reason="Signal refresh could not verify whether a source table exists and will treat it as unavailable.",
            error=exc,
            metadata={"table_name": table_name},
        )
        return False


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
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.signal_refresh",
            fallback_type="signal_refresh_table_columns_check_failed",
            source=table_name,
            severity="warn",
            reason="Signal refresh could not inspect source table columns and will treat optional context fields as unavailable.",
            error=exc,
            metadata={"table_name": table_name},
        )
        return set()
    if df.empty or "column_name" not in df.columns:
        return set()
    return {str(value) for value in df["column_name"].dropna().tolist()}


def _first_record(df: pd.DataFrame) -> dict[str, Any]:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return {}
    return df.iloc[0].to_dict()


def _table_count_summary(
    *,
    table_name: str,
    date_column: str,
    asof_date: pd.Timestamp | None = None,
    asof_operator: str = "<=",
) -> dict[str, Any]:
    if not table_exists(table_name):
        return {"table": table_name, "exists": False, "status": "missing_table"}
    columns = table_columns(table_name)
    if date_column not in columns:
        return {
            "table": table_name,
            "exists": True,
            "status": "missing_date_column",
            "missing_columns": [date_column],
            "available_column_count": len(columns),
        }
    cutoff_operator = "<" if asof_operator == "<" else "<="
    asof_filter = f"WHERE {date_column} {cutoff_operator} %(asof_date)s" if asof_date is not None else ""
    params = {"asof_date": asof_date} if asof_date is not None else None
    try:
        row = _first_record(
            sql_to_df(
                f"""
                SELECT COUNT(*) AS row_count, MAX({date_column}) AS latest_date
                FROM {table_name}
                {asof_filter}
                """,
                params=params,
                retries=3,
                statement_timeout_ms=5000,
            )
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.signal_refresh",
            fallback_type="signal_refresh_context_diagnostic_failed",
            source=table_name,
            severity="warn",
            reason="Signal refresh could not build context-overlay source diagnostics.",
            error=exc,
            metadata={"table_name": table_name, "date_column": date_column, "asof_date": str(asof_date) if asof_date is not None else None},
        )
        return {"table": table_name, "exists": True, "status": "diagnostic_failed", "error_type": exc.__class__.__name__}
    return {
        "table": table_name,
        "exists": True,
        "status": "ok",
        "row_count": int(row.get("row_count") or 0),
        "latest_date": row.get("latest_date"),
    }


def inspect_context_overlay_signal_inputs(*, asof_date: Any = None) -> dict[str, Any]:
    effective_asof = _date(asof_date)
    asof_cutoff, asof_operator = _point_in_time_cutoff(asof_date if asof_date is not None else effective_asof)
    sources: list[dict[str, Any]] = []
    for spec in CONTEXT_OVERLAY_SIGNAL_SOURCES:
        table_name = str(spec["table"])
        date_column = str(spec["date_column"])
        summary = _table_count_summary(table_name=table_name, date_column=date_column, asof_date=asof_cutoff, asof_operator=asof_operator)
        summary["source"] = spec["source"]
        if summary.get("exists"):
            columns = table_columns(table_name)
            required = set(spec.get("required_columns") or set())
            missing = sorted(required - columns)
            summary["required_columns_present"] = not missing
            summary["missing_required_columns"] = missing
            if not missing and "direction" in columns:
                active_filter = "TRUE"
                if "production_status" in columns:
                    active_filter += " AND COALESCE(production_status, 'active') = 'active'"
                if "authority_scope" in columns:
                    active_filter += " AND COALESCE(authority_scope, 'watchlist_pressure_only') = 'watchlist_pressure_only'"
                try:
                    direction_row = _first_record(
                        sql_to_df(
                            f"""
                            SELECT
                                SUM(CASE WHEN LOWER(TRIM(COALESCE(direction, ''))) IN ('positive', 'watch') THEN 1 ELSE 0 END) AS positive_watch_rows,
                                SUM(CASE WHEN LOWER(TRIM(COALESCE(direction, ''))) = 'negative' THEN 1 ELSE 0 END) AS negative_rows,
                                SUM(CASE WHEN ({active_filter}) THEN 1 ELSE 0 END) AS active_authorized_rows
                            FROM {table_name}
                            WHERE {date_column} {asof_operator} %(asof_date)s
                            """,
                            params={"asof_date": asof_cutoff},
                            retries=3,
                            statement_timeout_ms=5000,
                        )
                    )
                    summary["positive_watch_rows"] = int(direction_row.get("positive_watch_rows") or 0)
                    summary["negative_rows"] = int(direction_row.get("negative_rows") or 0)
                    summary["active_authorized_rows"] = int(direction_row.get("active_authorized_rows") or 0)
                except Exception as exc:
                    record_local_fallback_event(
                        module="advisory.signal_refresh",
                        fallback_type="signal_refresh_context_direction_diagnostic_failed",
                        source=table_name,
                        severity="warn",
                        reason="Signal refresh could not count context-overlay rows by direction.",
                        error=exc,
                        metadata={"source": spec["source"], "table_name": table_name, "asof_date": str(asof_cutoff), "asof_operator": asof_operator},
                    )
                    summary["direction_diagnostic_status"] = "failed"
        sources.append(summary)

    watchlist = _table_count_summary(table_name=WATCHLIST_TABLE, date_column="asof_date", asof_date=asof_cutoff, asof_operator=asof_operator)
    portfolio = _table_count_summary(table_name=PORTFOLIO_TABLE, date_column="asof_date", asof_date=asof_cutoff, asof_operator=asof_operator)
    available_sources = [source for source in sources if source.get("exists") and source.get("required_columns_present", True)]
    source_rows_total = sum(int(source.get("row_count") or 0) for source in sources)
    active_authorized_total = sum(int(source.get("active_authorized_rows") or 0) for source in sources)
    return {
        "asof_date": effective_asof,
        "asof_cutoff": asof_cutoff,
        "asof_operator": asof_operator,
        "source_count": len(sources),
        "available_source_count": len(available_sources),
        "source_rows_total": int(source_rows_total),
        "active_authorized_rows_total": int(active_authorized_total),
        "sources": sources,
        "watchlist": watchlist,
        "portfolio": portfolio,
        "source_status": "no_context_overlay_rows_available" if source_rows_total <= 0 else "context_overlay_rows_available",
        "empty_reason": "no_context_overlay_rows_available" if source_rows_total <= 0 else None,
    }


def summarize_context_overlay_conversion(
    *,
    diagnostics: dict[str, Any],
    raw_theme_targets: pd.DataFrame,
    raw_direct_targets: pd.DataFrame,
    raw_positive_watch_targets: pd.DataFrame,
    kept_theme_targets: pd.DataFrame,
    kept_direct_targets: pd.DataFrame,
    kept_positive_watch_targets: pd.DataFrame,
    reliability_suppressed: list[dict[str, Any]],
    policy_suppressed: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Explain why context-overlay source evidence did or did not become signal rows.

    This is diagnostic-only. It must not grant portfolio, broker, or full-advisory
    authority to watcher/refresher rows.
    """

    def _safe_len(frame: Any) -> int:
        return int(len(frame)) if isinstance(frame, pd.DataFrame) else 0

    def _source_counts(frame: pd.DataFrame, *, default_source: str | None = None) -> dict[str, int]:
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return {}
        if "context_source" in frame.columns:
            series = frame["context_source"].fillna(default_source or "").astype("string")
        else:
            series = pd.Series([default_source or "unknown"] * len(frame), dtype="string")
        counts: dict[str, int] = {}
        for value in series:
            key = str(value or default_source or "unknown").strip().lower() or "unknown"
            counts[key] = counts.get(key, 0) + 1
        return counts

    raw_target_counts: dict[str, int] = {}
    kept_target_counts: dict[str, int] = {}
    for source, count in _source_counts(raw_theme_targets, default_source="theme_context").items():
        raw_target_counts[source] = raw_target_counts.get(source, 0) + count
    for source, count in _source_counts(raw_direct_targets).items():
        raw_target_counts[source] = raw_target_counts.get(source, 0) + count
    for source, count in _source_counts(raw_positive_watch_targets).items():
        raw_target_counts[source] = raw_target_counts.get(source, 0) + count
    for source, count in _source_counts(kept_theme_targets, default_source="theme_context").items():
        kept_target_counts[source] = kept_target_counts.get(source, 0) + count
    for source, count in _source_counts(kept_direct_targets).items():
        kept_target_counts[source] = kept_target_counts.get(source, 0) + count
    for source, count in _source_counts(kept_positive_watch_targets).items():
        kept_target_counts[source] = kept_target_counts.get(source, 0) + count

    suppressed_counts: dict[str, int] = {}
    for item in reliability_suppressed or []:
        source = str(item.get("context_source") or "unknown").strip().lower() or "unknown"
        suppressed_counts[source] = suppressed_counts.get(source, 0) + 1
    policy_suppressed_counts: dict[str, int] = {}
    policy_suppression_reason_counts: dict[str, dict[str, int]] = {}
    identity_policy_suppressed_counts: dict[str, int] = {}
    for item in policy_suppressed or []:
        source = str(item.get("context_source") or "unknown").strip().lower() or "unknown"
        policy_suppressed_counts[source] = policy_suppressed_counts.get(source, 0) + 1
        reasons = item.get("suppression_reasons") if isinstance(item.get("suppression_reasons"), list) else []
        reason_texts = [str(reason or "").strip() for reason in reasons if str(reason or "").strip()]
        effect = str(item.get("context_policy_effect") or "").strip()
        if effect:
            reason_texts.append(effect)
        if not reason_texts:
            reason_texts.append("unspecified_policy_suppression")
        reason_texts = sorted(set(reason_texts))
        source_reason_counts = policy_suppression_reason_counts.setdefault(source, {})
        for reason in reason_texts:
            source_reason_counts[reason] = source_reason_counts.get(reason, 0) + 1
        combined_reason_text = " ".join(reason_texts).lower()
        if any(token in combined_reason_text for token in ["identity", "unresolved", "company_master", "security_id"]):
            identity_policy_suppressed_counts[source] = identity_policy_suppressed_counts.get(source, 0) + 1

    rows: list[dict[str, Any]] = []
    source_rows = diagnostics.get("sources") if isinstance(diagnostics, dict) else []
    for source_row in source_rows or []:
        source = str(source_row.get("source") or "").strip().lower()
        if not source:
            continue
        source_count = int(source_row.get("row_count") or 0)
        positive_watch_count = int(source_row.get("positive_watch_rows") or 0)
        negative_count = int(source_row.get("negative_rows") or 0)
        raw_targets = int(raw_target_counts.get(source, 0))
        kept_targets = int(kept_target_counts.get(source, 0))
        suppressed = int(suppressed_counts.get(source, 0))
        policy_suppressed_count = int(policy_suppressed_counts.get(source, 0))
        identity_policy_suppressed_count = int(identity_policy_suppressed_counts.get(source, 0))
        policy_reason_counts = policy_suppression_reason_counts.get(source, {})
        reasons: list[str] = []
        if source_count <= 0:
            reasons.append("no_source_rows")
        if source_count > 0 and not source_row.get("required_columns_present", True):
            reasons.append("missing_required_columns")
        if source in {"macro_context"} and negative_count > 0 and raw_targets <= 0:
            reasons.append("negative_macro_context_rows_did_not_match_reviewable_watchlist_or_portfolio_exposure")
        if source in {"theme_context", "macro_context"} and positive_watch_count > 0 and raw_targets <= 0:
            reasons.append("sector_overlay_rows_did_not_match_market_context_universe_or_watch_candidate_filters")
        if source in {"announcement_context", "exchange_context", "bhavcopy_context"} and negative_count > 0 and raw_targets <= 0:
            reasons.append("direct_negative_rows_did_not_match_watchlist_or_portfolio_exposure")
        if raw_targets > 0 and kept_targets <= 0 and suppressed > 0:
            reasons.append("all_targets_suppressed_by_context_reliability_policy")
        if raw_targets > 0 and policy_suppressed_count > 0:
            reasons.append("positive_watch_targets_suppressed_by_watchlist_policy")
        if identity_policy_suppressed_count > 0:
            reasons.append("positive_watch_targets_suppressed_by_identity_source_policy")
        if raw_targets > 0 and kept_targets > 0:
            reasons.append("converted_to_review_only_signal_targets")
        elif source_count > 0 and not reasons:
            reasons.append("source_rows_available_but_no_target_conversion")
        rows.append(
            {
                "source": source,
                "source_rows": source_count,
                "positive_watch_source_rows": positive_watch_count,
                "negative_source_rows": negative_count,
                "raw_target_rows": raw_targets,
                "kept_target_rows": kept_targets,
                "reliability_suppressed_target_rows": suppressed,
                "policy_suppressed_target_rows": policy_suppressed_count,
                "identity_policy_suppressed_target_rows": identity_policy_suppressed_count,
                "policy_suppression_reason_counts": dict(sorted(policy_reason_counts.items(), key=lambda item: (-item[1], item[0]))),
                "conversion_status": "converted" if kept_targets > 0 else "not_converted",
                "conversion_reasons": reasons,
                "authority_scope": SIGNAL_REFRESH_AUTHORITY_CONTRACT["authority_scope"],
                "broker_execution_allowed": False,
            }
        )

    sources_with_rows = [row for row in rows if int(row.get("source_rows") or 0) > 0]
    converted_sources = [row for row in rows if int(row.get("kept_target_rows") or 0) > 0]
    sources_with_conversion_gaps = [
        row
        for row in sources_with_rows
        if int(row.get("kept_target_rows") or 0) <= 0
    ]
    status = (
        "converted"
        if converted_sources
        else "source_rows_available_but_no_signal_targets_matched"
        if sources_with_rows
        else "no_context_overlay_rows_available"
    )
    return {
        "status": status,
        "source_rows_total": int(sum(int(row.get("source_rows") or 0) for row in rows)),
        "raw_target_rows": int(_safe_len(raw_theme_targets) + _safe_len(raw_direct_targets) + _safe_len(raw_positive_watch_targets)),
        "kept_target_rows": int(_safe_len(kept_theme_targets) + _safe_len(kept_direct_targets) + _safe_len(kept_positive_watch_targets)),
        "reliability_suppressed_target_rows": int(len(reliability_suppressed or [])),
        "policy_suppressed_target_rows": int(len(policy_suppressed or [])),
        "identity_policy_suppressed_target_rows": int(sum(identity_policy_suppressed_counts.values())),
        "policy_suppressed_samples": list((policy_suppressed or [])[:10]),
        "sources_with_conversion_gaps": int(len(sources_with_conversion_gaps)),
        "sources": rows,
        "authority_scope": SIGNAL_REFRESH_AUTHORITY_CONTRACT["authority_scope"],
        "portfolio_authority": SIGNAL_REFRESH_AUTHORITY_CONTRACT["portfolio_authority"],
        "broker_execution_allowed": False,
        "full_advisory_required": True,
    }


def _concat_nonempty(frames: list[pd.DataFrame]) -> pd.DataFrame:
    kept = [frame for frame in frames if isinstance(frame, pd.DataFrame) and not frame.empty]
    if not kept:
        return pd.DataFrame()
    if len(kept) == 1:
        return kept[0].copy()
    return pd.concat(kept, ignore_index=True, sort=False)


def ensure_table() -> None:
    apply_schema_migration(
        migration_id=SIGNAL_REFRESH_SCHEMA_MIGRATION_ID,
        description="Create and normalize incremental signal refresh action table.",
        statements=SIGNAL_REFRESH_SCHEMA_STATEMENTS,
        metadata={"module": "advisory.signal_refresh", "tables": [TABLE_NAME]},
    )


def _latest_row(table_name: str, symbol: str, *, asof_date: pd.Timestamp | None = None, date_column: str = "asof_date") -> dict[str, Any] | None:
    if not table_exists(table_name):
        return None
    clauses = ["UPPER(TRIM(symbol)) = %s"]
    params: list[Any] = [symbol.upper()]
    if asof_date is not None:
        clauses.append(f"{date_column} <= %s")
        params.append(asof_date)
    try:
        df = sql_to_df(
            f"""
            SELECT *
            FROM {table_name}
            WHERE {' AND '.join(clauses)}
            ORDER BY {date_column} DESC NULLS LAST, load_ts DESC NULLS LAST
            LIMIT 1
            """,
            params=tuple(params),
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.signal_refresh",
            source=table_name,
            fallback_type="signal_refresh_latest_row_unavailable",
            severity="warn",
            symbol=symbol,
            reason="Signal refresh continued without latest source row because lookup failed.",
            error=exc,
            metadata={"date_column": date_column, "asof_date": asof_date},
        )
        return None
    if df.empty:
        return None
    return df.iloc[0].to_dict()


def load_latest_action(symbol: str, *, asof_date: pd.Timestamp | None = None) -> dict[str, Any] | None:
    return _latest_row(ACTIONS_TABLE, symbol, asof_date=asof_date, date_column="asof_date")


def load_latest_lifecycle(symbol: str, *, asof_date: pd.Timestamp | None = None) -> dict[str, Any] | None:
    return _latest_row(LIFECYCLE_TABLE, symbol, asof_date=asof_date, date_column="asof_date")


def load_latest_rebalance(symbol: str, *, asof_date: pd.Timestamp | None = None) -> dict[str, Any] | None:
    return _latest_row(REBALANCE_TABLE, symbol, asof_date=asof_date, date_column="asof_date")


def load_event_policy(symbol: str, *, unique_id: str | None = None, asof_date: pd.Timestamp | None = None, limit: int = 5) -> list[dict[str, Any]]:
    if not table_exists(EVENT_POLICY_TABLE):
        return []
    clauses = ["UPPER(TRIM(symbol)) = %s"]
    params: list[Any] = [symbol.upper()]
    if unique_id:
        clauses.append("unique_id = %s")
        params.append(unique_id)
    if asof_date is not None:
        clauses.append("COALESCE(asof_date, published_on) <= %s")
        params.append(asof_date)
    try:
        df = sql_to_df(
            f"""
            SELECT *
            FROM {EVENT_POLICY_TABLE}
            WHERE {' AND '.join(clauses)}
            ORDER BY COALESCE(policy_at, asof_date, published_on) DESC NULLS LAST, load_ts DESC NULLS LAST
            LIMIT %s
            """,
            params=tuple([*params, max(1, min(int(limit), 50))]),
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.signal_refresh",
            source=EVENT_POLICY_TABLE,
            fallback_type="signal_refresh_event_policy_unavailable",
            severity="warn",
            symbol=symbol,
            unique_id=unique_id,
            reason="Signal refresh continued without event-policy rows because lookup failed.",
            error=exc,
            metadata={"asof_date": asof_date, "limit": limit},
        )
        return []
    return df.to_dict(orient="records") if not df.empty else []


def load_recent_router_actions(*, limit: int = 25) -> list[dict[str, Any]]:
    if not table_exists(ROUTER_ACTIONS_TABLE):
        return []
    try:
        df = sql_to_df(
            f"""
            SELECT *
            FROM {ROUTER_ACTIONS_TABLE}
            WHERE symbol IS NOT NULL
            ORDER BY routed_at DESC NULLS LAST, load_ts DESC NULLS LAST
            LIMIT %s
            """,
            params=(max(1, min(int(limit), 250)),),
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.signal_refresh",
            source=ROUTER_ACTIONS_TABLE,
            fallback_type="signal_refresh_router_actions_unavailable",
            severity="warn",
            reason="Signal refresh continued without recent router actions because lookup failed.",
            error=exc,
            metadata={"limit": limit},
        )
        return []
    if df.empty:
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in df.to_dict(orient="records"):
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        rows.append(item)
    return rows


def _normalized_action(value: Any) -> str | None:
    text = _text(value)
    return None if text is None else text.upper()


def _row_confidence(row: dict[str, Any] | None, *columns: str) -> float | None:
    if not row:
        return None
    for column in columns:
        value = _num(row.get(column))
        if value is not None:
            return value / 100.0 if value > 1.0 and column.endswith("_pct") else value
    return None


def _extract_action_payload(*, action: dict[str, Any] | None, lifecycle: dict[str, Any] | None, rebalance: dict[str, Any] | None, events: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "action": action or {},
        "lifecycle": lifecycle or {},
        "rebalance": rebalance or {},
        "event_policy": events,
    }


def wait_signal_escalation(match: dict[str, Any]) -> dict[str, Any]:
    expected = _normalized_action(match.get("expected_action")) or "MANUAL_REVIEW"
    evidence = _jsonish(match.get("evidence_json"), {}, source="wait_signal_evidence_json")
    wait_context = evidence.get("wait_signal") if isinstance(evidence, dict) else {}
    if not isinstance(wait_context, dict):
        wait_context = {}
    condition_type = _text(wait_context.get("condition_type")) or _text(match.get("signal_type")) or "wait_signal"
    wait_question = _text(wait_context.get("wait_question"))
    match_reason = _text(match.get("match_reason")) or "A wait signal matched fresh evidence."
    if expected in WAIT_SIGNAL_NEGATIVE_ACTIONS or "EXIT" in expected or "SELL" in expected or "REDUCE" in expected:
        action = "REDUCE_EXPOSURE_REVIEW" if expected not in {"GO_CASH_REVIEW"} else expected
    elif expected in WAIT_SIGNAL_POSITIVE_ACTIONS or "BUY" in expected or "WATCH" in expected:
        action = "WATCH"
    else:
        action = "MANUAL_REVIEW"
    reason_bits = [f"Matched {condition_type.replace('_', ' ')} wait signal.", match_reason]
    if expected != action:
        reason_bits.append(f"Escalated safely from expected {expected} to review-only {action}.")
    else:
        reason_bits.append(f"Expected follow-up action is {expected}.")
    if wait_question:
        reason_bits.append(f"Original wait: {wait_question}")
    return {
        "action": action,
        "reason": " ".join(reason_bits),
        "confidence": _row_confidence(match, "match_score"),
        "condition_type": condition_type,
        "expected_action": expected,
    }


def router_context_signal(router_context: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(router_context, dict) or not router_context:
        return None
    source_types = {
        str(item).strip().lower()
        for item in (router_context.get("source_types") or [])
        if str(item or "").strip()
    }
    reasons = [
        str(item).strip().upper()
        for item in (router_context.get("reasons") or [])
        if str(item or "").strip()
    ]
    action_type = _text(router_context.get("action_type")) or "router_update"
    priority_score = _num(router_context.get("priority_score"))
    confidence = None if priority_score is None else max(0.0, min(float(priority_score) / 5.0, 1.0))

    if any(reason in ROUTER_NEGATIVE_ALERTS for reason in reasons):
        matched = [reason for reason in reasons if reason in ROUTER_NEGATIVE_ALERTS]
        return {
            "action": "REDUCE_EXPOSURE_REVIEW",
            "source": "router_price_alert",
            "confidence": confidence,
            "reason": (
                "Live price watcher matched stop/invalidation evidence "
                f"({', '.join(matched)}). Review exposure immediately; daily advisory remains authoritative."
            ),
        }
    if any(reason in ROUTER_POSITIVE_ALERTS for reason in reasons):
        matched = [reason for reason in reasons if reason in ROUTER_POSITIVE_ALERTS]
        return {
            "action": "WATCH",
            "source": "router_price_alert",
            "confidence": confidence,
            "reason": (
                "Live price watcher matched entry/breakout evidence "
                f"({', '.join(matched)}). Treat this as a review-only entry signal until advisory confirms stop, target, and sizing."
            ),
        }
    if "announcement_event" in source_types or "news_event" in source_types:
        readable_sources = ", ".join(sorted(source_types)) or action_type
        return {
            "action": "WATCH",
            "source": "router_event",
            "confidence": confidence,
            "reason": (
                f"Watcher routed fresh {readable_sources} context before a full advisory reconciliation. "
                "Treat this as review-only watch evidence; event-policy/full advisory must confirm any buy, sell, or sizing change."
            ),
        }
    return None


def choose_signal(
    *,
    symbol: str,
    reason: str,
    action: dict[str, Any] | None,
    lifecycle: dict[str, Any] | None,
    rebalance: dict[str, Any] | None,
    events: list[dict[str, Any]],
    wait_matches: list[dict[str, Any]] | None = None,
    router_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lifecycle_action = _normalized_action((lifecycle or {}).get("next_action"))
    rebalance_action = _normalized_action((rebalance or {}).get("suggested_action"))
    action_code = _normalized_action((action or {}).get("action_code"))

    wait_matches = wait_matches or []
    router_signal = router_context_signal(router_context)
    if lifecycle_action in EXIT_ACTIONS or rebalance_action in EXIT_ACTIONS:
        source = "lifecycle"
        signal_action = rebalance_action or lifecycle_action or "SELL"
        action_reason = _text((rebalance or {}).get("action_reason")) or _text((lifecycle or {}).get("next_action_reason")) or _text((lifecycle or {}).get("active_exit_condition"))
        confidence = _row_confidence(lifecycle, "target_confidence") or _row_confidence(action, "invest_score_pct")
    elif wait_matches:
        top_wait = wait_matches[0]
        escalation = wait_signal_escalation(top_wait)
        source = "wait_signal"
        signal_action = escalation["action"]
        action_reason = escalation["reason"]
        confidence = escalation["confidence"]
    elif router_signal and router_signal["action"] in EXIT_ACTIONS:
        source = str(router_signal["source"])
        signal_action = str(router_signal["action"])
        action_reason = str(router_signal["reason"])
        confidence = _num(router_signal.get("confidence"))
    elif events:
        top_event = events[0]
        event_action = _normalized_action(top_event.get("action_type")) or "MANUAL_REVIEW"
        source = "event_policy"
        if event_action == "BUY_WATCH":
            signal_action = "WATCH"
        elif event_action == "REDUCE_EXPOSURE_REVIEW":
            signal_action = "REDUCE_EXPOSURE_REVIEW"
        elif event_action == "MANUAL_REVIEW":
            signal_action = "MANUAL_REVIEW"
        else:
            signal_action = event_action
        action_reason = _text(top_event.get("action_reason")) or _text(top_event.get("action_detail")) or f"Latest event policy action: {event_action}"
        confidence = _row_confidence(top_event, "confidence", "policy_score")
    elif router_signal:
        source = str(router_signal["source"])
        signal_action = str(router_signal["action"])
        action_reason = str(router_signal["reason"])
        confidence = _num(router_signal.get("confidence"))
    elif action_code:
        source = "action_recommendation"
        signal_action = action_code
        action_reason = _text(action.get("action_reason")) or _text(action.get("action_detail")) or f"Latest consolidated advisory action: {action_code}"
        confidence = _row_confidence(action, "invest_score_pct")
    elif lifecycle_action:
        source = "lifecycle"
        signal_action = lifecycle_action
        action_reason = _text((lifecycle or {}).get("next_action_reason")) or _text((lifecycle or {}).get("lifecycle_reason"))
        confidence = _row_confidence(lifecycle, "target_confidence")
    else:
        source = "none"
        signal_action = "NO_CHANGE"
        action_reason = "No latest action, lifecycle, or event-policy row found for symbol."
        confidence = None

    normalized = _normalized_action(signal_action) or "NO_CHANGE"
    if normalized in EXIT_ACTIONS:
        status = "exit_or_reduce"
    elif normalized in BUY_ACTIONS:
        status = "entry_or_add"
    elif normalized in WATCH_ACTIONS:
        status = "watch_or_review"
    elif normalized == "NO_CHANGE":
        status = "no_change"
    else:
        status = "review"

    return {
        "symbol": symbol.upper(),
        "signal_action": normalized,
        "signal_status": status,
        "signal_source": source,
        "confidence": confidence,
        "action_reason": action_reason or f"Signal refresh from {reason}.",
    }


def classify_signal_effect(
    *,
    signal: dict[str, Any],
    action: dict[str, Any] | None,
    wait_matches: list[dict[str, Any]] | None = None,
) -> dict[str, str | None]:
    wait_matches = wait_matches or []
    signal_action = _normalized_action(signal.get("signal_action")) or "NO_CHANGE"
    previous_action = _normalized_action((action or {}).get("action_code"))
    signal_source = _text(signal.get("signal_source")) or "none"

    if wait_matches:
        top_match = wait_matches[0]
        condition_type = _text(top_match.get("signal_type")) or "wait_signal"
        return {
            "effect_type": "wait_match_created",
            "effect_summary": f"Detected a wait-signal match for {condition_type}; fast refresh stayed review-only until operator/advisory reconciliation.",
            "previous_action": previous_action,
        }
    if previous_action and signal_action != previous_action and signal_action != "NO_CHANGE":
        return {
            "effect_type": "action_changed",
            "effect_summary": f"Fast refresh changed the symbol-level action from {previous_action} to {signal_action}; daily advisory remains authoritative.",
            "previous_action": previous_action,
        }
    return {
        "effect_type": "evidence_only",
        "effect_summary": f"Watcher refreshed {signal_source} evidence without changing the latest consolidated action.",
        "previous_action": previous_action,
    }


def make_refresh_id(*, refreshed_at: Any, symbol: str, unique_id: str | None, reason: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, json_dumps({"refreshed_at": str(refreshed_at), "symbol": symbol.upper(), "unique_id": unique_id, "reason": reason})))


def persist_signal_rows(rows: list[dict[str, Any]]) -> None:
    ensure_table()
    if not rows:
        return
    df = pd.DataFrame(rows)
    for column in ["refreshed_at", "asof_date", "load_ts"]:
        if column in df.columns:
            df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    if "confidence" in df.columns:
        df["confidence"] = pd.to_numeric(df["confidence"], errors="coerce")
    if "dry_run" in df.columns:
        df["dry_run"] = df["dry_run"].astype("boolean")
    if "action_changed" in df.columns:
        df["action_changed"] = df["action_changed"].astype("boolean")
    if "broker_execution_allowed" in df.columns:
        df["broker_execution_allowed"] = df["broker_execution_allowed"].astype("boolean")
    if "full_advisory_required" in df.columns:
        df["full_advisory_required"] = df["full_advisory_required"].astype("boolean")
    upsert_to_db(df, TABLE_NAME, unique_keys=["refresh_id"])


def dedupe_signal_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        signal_action = str(row.get("signal_action") or "").strip().upper()
        signal_source = str(row.get("signal_source") or "").strip().lower()
        reason = str(row.get("reason") or "").strip().lower()
        payload = str(row.get("action_payload_json") or "").strip()
        key = (symbol, signal_action, signal_source, reason, payload)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _direct_context_reason_expr(columns: set[str]) -> str:
    if "watch_reason_detail" in columns:
        return "o.watch_reason_detail"
    if "trigger_reason" in columns:
        return "o.trigger_reason"
    if "theme_reason" in columns:
        return "o.theme_reason"
    return "NULL::text"


def _direct_context_class_expr(columns: set[str], preferred_column: str | None) -> str:
    if preferred_column and preferred_column in columns:
        return f"o.{preferred_column}"
    for column in ["event_class", "event_type", "deal_pressure", "macro_signal_id", "theme_id"]:
        if column in columns:
            return f"o.{column}"
    return "NULL::text"


def _optional_text_column_expr(columns: set[str], column: str) -> str:
    return f"o.{column}" if column in columns else "NULL::text"


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
        "\n                  " + "\n                  ".join(f"AND {clause}" for clause in clauses),
        {
            "taxonomy_filter_applied": bool(clauses),
            "archive_only_rows_excluded": "announcement_storage_form" in columns,
            "review_ready_rows_required": "llm_review_ready" in columns,
        },
    )


def _portfolio_exposure_cte(portfolio_exists: bool, *, asof_operator: str = "<=") -> tuple[str, str]:
    if not portfolio_exists:
        return "", ""
    cutoff_operator = "<" if asof_operator == "<" else "<="
    return (
        f"""
            , latest_portfolio_rows AS (
                SELECT *
                FROM (
                    SELECT
                        p.*,
                        ROW_NUMBER() OVER (
                            PARTITION BY UPPER(TRIM(p.symbol))
                            ORDER BY
                                COALESCE(p.asof_date, p.published_on) DESC NULLS LAST,
                                p.published_on DESC NULLS LAST,
                                p.load_ts DESC NULLS LAST,
                                p.unique_id DESC NULLS LAST
                        ) AS portfolio_rn
                    FROM {PORTFOLIO_TABLE} p
                    WHERE COALESCE(p.asof_date, p.published_on) {cutoff_operator} %(asof_date)s
                      AND NULLIF(TRIM(p.symbol), '') IS NOT NULL
                ) ranked_portfolio
                WHERE portfolio_rn = 1
            ),
            portfolio_exposure AS (
                SELECT DISTINCT UPPER(TRIM(symbol)) AS symbol, 'portfolio'::text AS exposure_source
                FROM latest_portfolio_rows
                WHERE COALESCE(LOWER(TRIM(portfolio_status)), 'active') NOT IN ('rejected', 'deferred', 'skipped', 'blocked', 'closed', 'exited')
                  AND COALESCE(approved_allocation_inr, requested_allocation_inr, 0) > 0
            )
        """,
        "UNION ALL SELECT * FROM portfolio_exposure",
    )


def _load_negative_direct_context_table_targets(
    *,
    table_name: str,
    source_name: str,
    asof_date: pd.Timestamp,
    date_column: str,
    asof_operator: str = "<=",
    class_column: str | None = None,
    limit: int = 50,
) -> pd.DataFrame:
    required_tables = [table_name, WATCHLIST_TABLE]
    if not all(table_exists(name) for name in required_tables):
        return pd.DataFrame()
    columns = table_columns(table_name)
    if not {"symbol", date_column, "direction"}.issubset(columns):
        return pd.DataFrame()
    portfolio_exists = table_exists(PORTFOLIO_TABLE)
    cutoff_operator = "<" if asof_operator == "<" else "<="
    portfolio_cte, portfolio_union = _portfolio_exposure_cte(portfolio_exists, asof_operator=cutoff_operator)
    score_expr = "o.pressure_score" if "pressure_score" in columns else "NULL::double precision"
    overlay_expr = "o.overlay_id" if "overlay_id" in columns else f"(o.{date_column}::text || ':' || UPPER(TRIM(o.symbol)) || ':{source_name}:negative')"
    class_expr = _direct_context_class_expr(columns, class_column)
    reason_expr = _direct_context_reason_expr(columns)
    sector_name_expr = _optional_text_column_expr(columns, "sector_name")
    sector_code_expr = _optional_text_column_expr(columns, "sector_code")
    load_ts_expr = "o.load_ts" if "load_ts" in columns else "NULL::timestamptz"
    production_filter = "AND COALESCE(o.production_status, 'active') = 'active'" if "production_status" in columns else ""
    authority_filter = "AND COALESCE(o.authority_scope, 'watchlist_pressure_only') = 'watchlist_pressure_only'" if "authority_scope" in columns else ""
    taxonomy_filter, taxonomy_metadata = (
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
            WITH watchlist_exposure AS (
                SELECT DISTINCT UPPER(TRIM(symbol)) AS symbol, 'watchlist'::text AS exposure_source
                FROM {WATCHLIST_TABLE}
                WHERE asof_date = (
                    SELECT MAX(asof_date)
                    FROM {WATCHLIST_TABLE}
                    WHERE asof_date {cutoff_operator} %(asof_date)s
                )
                  AND NULLIF(TRIM(symbol), '') IS NOT NULL
                  AND COALESCE(watch_enabled, TRUE) = TRUE
                  AND COALESCE(LOWER(TRIM(watch_status)), 'active') IN ('active', 'review_manual', 'theme_context')
            )
            {portfolio_cte},
            exposure AS (
                SELECT * FROM watchlist_exposure
                {portfolio_union}
            ),
            ranked AS (
                SELECT
                    UPPER(TRIM(o.symbol)) AS symbol,
                    e.exposure_source,
                    o.{date_column} AS overlay_asof_date,
                    %(source_name)s::text AS context_source,
                    {overlay_expr} AS overlay_id,
                    o.direction,
                    {score_expr} AS pressure_score,
                    {class_expr} AS context_class,
                    {reason_expr} AS context_reason,
                    {sector_name_expr} AS overlay_sector_name,
                    {sector_code_expr} AS overlay_sector_code,
                    {load_ts_expr} AS overlay_load_ts,
                    ROW_NUMBER() OVER (
                        PARTITION BY UPPER(TRIM(o.symbol))
                        ORDER BY
                            CASE WHEN e.exposure_source = 'portfolio' THEN 0 ELSE 1 END,
                            o.{date_column} DESC NULLS LAST,
                            {score_expr} DESC NULLS LAST,
                            {overlay_expr}
                    ) AS rn
                FROM {table_name} o
                JOIN exposure e
                  ON UPPER(TRIM(o.symbol)) = e.symbol
                WHERE o.{date_column} {cutoff_operator} %(asof_date)s
                  AND LOWER(TRIM(COALESCE(o.direction, ''))) = 'negative'
                  {production_filter}
                  {authority_filter}
                  {taxonomy_filter}
            )
            SELECT *
            FROM ranked
            WHERE rn = 1
            ORDER BY pressure_score DESC NULLS LAST, overlay_asof_date DESC NULLS LAST, symbol
            LIMIT %(limit)s
            """,
            params={
                "asof_date": asof_date,
                "source_name": source_name,
                "limit": max(1, min(int(limit), 500)),
            },
            retries=3,
            statement_timeout_ms=10000,
        )
        if not df.empty and source_name == "announcement_context":
            df["compact_evidence_contract"] = json.dumps(
                {
                    "source_table": table_name,
                    "source_family": source_name,
                    **taxonomy_metadata,
                    "raw_announcement_scan_allowed": False,
                    "authority_scope": "watchlist_pressure_only",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        return df
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.signal_refresh",
            fallback_type="signal_refresh_direct_context_targets_failed",
            source=table_name,
            severity="warn",
            reason="Signal refresh could not load direct negative context exposure targets.",
            error=exc,
            metadata={"source_name": source_name, "asof_date": str(asof_date), "asof_operator": cutoff_operator, "limit": int(limit), "portfolio_table_available": bool(portfolio_exists)},
        )
        return pd.DataFrame()


def load_negative_direct_context_exposure_targets(*, asof_date: Any = None, limit: int = 50) -> pd.DataFrame:
    if int(limit) <= 0:
        return pd.DataFrame()
    effective_asof = _date(asof_date)
    asof_cutoff, asof_operator = _point_in_time_cutoff(asof_date if asof_date is not None else effective_asof)
    per_source_limit = max(1, min(int(limit), 500))
    frames = [
        _load_negative_direct_context_table_targets(
            table_name=ANNOUNCEMENT_CONTEXT_OVERLAYS_TABLE,
            source_name="announcement_context",
            asof_date=asof_cutoff,
            asof_operator=asof_operator,
            date_column="published_on",
            class_column="event_class",
            limit=per_source_limit,
        ),
        _load_negative_direct_context_table_targets(
            table_name=EXCHANGE_CONTEXT_OVERLAYS_TABLE,
            source_name="exchange_context",
            asof_date=asof_cutoff,
            asof_operator=asof_operator,
            date_column="asof_date",
            class_column="event_type",
            limit=per_source_limit,
        ),
        _load_negative_direct_context_table_targets(
            table_name=BHAVCOPY_CONTEXT_OVERLAYS_TABLE,
            source_name="bhavcopy_context",
            asof_date=asof_cutoff,
            asof_operator=asof_operator,
            date_column="asof_date",
            class_column="deal_pressure",
            limit=per_source_limit,
        ),
    ]
    frames = [frame for frame in frames if isinstance(frame, pd.DataFrame) and not frame.empty]
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True, sort=False)
    out["symbol"] = out["symbol"].astype("string").str.strip().str.upper()
    out["pressure_score"] = pd.to_numeric(out.get("pressure_score"), errors="coerce")
    out["overlay_asof_date"] = pd.to_datetime(out["overlay_asof_date"], utc=True, errors="coerce")
    out = out.dropna(subset=["symbol", "overlay_asof_date"])
    if out.empty:
        return pd.DataFrame()
    out = out.sort_values(
        ["symbol", "pressure_score", "overlay_asof_date"],
        ascending=[True, False, False],
        na_position="last",
    ).drop_duplicates(subset=["symbol", "context_source"], keep="first")
    return out.sort_values(["pressure_score", "overlay_asof_date"], ascending=[False, False], na_position="last").head(max(1, min(int(limit), 500)))


def load_negative_theme_context_exposure_targets(*, asof_date: Any = None, limit: int = 50) -> pd.DataFrame:
    if int(limit) <= 0:
        return pd.DataFrame()
    required_tables = [THEME_CONTEXT_OVERLAYS_TABLE, MARKET_CONTEXT_UNIVERSE_TABLE, WATCHLIST_TABLE]
    if not all(table_exists(table_name) for table_name in required_tables):
        return pd.DataFrame()
    portfolio_exists = table_exists(PORTFOLIO_TABLE)
    effective_asof = _date(asof_date)
    asof_cutoff, asof_operator = _point_in_time_cutoff(asof_date if asof_date is not None else effective_asof)
    cutoff_operator = "<" if asof_operator == "<" else "<="
    portfolio_cte, portfolio_union = _portfolio_exposure_cte(portfolio_exists, asof_operator=cutoff_operator)
    try:
        return sql_to_df(
            f"""
            WITH theme_sector_alias(overlay_sector_key, universe_sector_code) AS (
                VALUES
                {theme_sector_alias_values_sql()}
            ),
            latest_overlays AS (
                SELECT *
                FROM {THEME_CONTEXT_OVERLAYS_TABLE}
                WHERE asof_date = (
                    SELECT MAX(asof_date)
                    FROM {THEME_CONTEXT_OVERLAYS_TABLE}
                    WHERE asof_date {cutoff_operator} %(asof_date)s
                )
                  AND production_status = 'active'
                  AND authority_scope = 'watchlist_pressure_only'
                  AND direction = 'negative'
                  AND NULLIF(TRIM(COALESCE(sector_name, sector_code, '')), '') IS NOT NULL
            ),
            latest_universe AS (
                SELECT *
                FROM {MARKET_CONTEXT_UNIVERSE_TABLE}
                WHERE asof_date = (
                    SELECT MAX(asof_date)
                    FROM {MARKET_CONTEXT_UNIVERSE_TABLE}
                    WHERE asof_date {cutoff_operator} %(asof_date)s
                )
                  AND NULLIF(TRIM(symbol), '') IS NOT NULL
                  AND NULLIF(TRIM(company_master_id), '') IS NOT NULL
            ),
            watchlist_exposure AS (
                SELECT DISTINCT UPPER(TRIM(symbol)) AS symbol, 'watchlist'::text AS exposure_source
                FROM {WATCHLIST_TABLE}
                WHERE asof_date = (
                    SELECT MAX(asof_date)
                    FROM {WATCHLIST_TABLE}
                    WHERE asof_date {cutoff_operator} %(asof_date)s
                )
                  AND NULLIF(TRIM(symbol), '') IS NOT NULL
                  AND COALESCE(watch_enabled, TRUE) = TRUE
                  AND COALESCE(LOWER(TRIM(watch_status)), 'active') IN ('active', 'review_manual', 'theme_context')
            )
            {portfolio_cte},
            exposure AS (
                SELECT * FROM watchlist_exposure
                {portfolio_union}
            ),
            joined AS (
                SELECT
                    UPPER(TRIM(u.symbol)) AS symbol,
                    u.company_master_id,
                    e.exposure_source,
                    u.asof_date AS universe_asof_date,
                    o.asof_date AS overlay_asof_date,
                    o.overlay_id,
                    o.theme_id,
                    o.theme_name,
                    o.sector_name AS overlay_sector_name,
                    o.sector_code AS overlay_sector_code,
                    u.sector_name AS universe_sector_name,
                    u.sector_code AS universe_sector_code,
                    u.context_rank,
                    u.technical_leadership_score,
                    u.macro_sensitivity_tag,
                    o.pressure_score,
                    o.theme_intensity,
                    o.hit_score,
                    o.holding_profile,
                    o.risk_level,
                    o.ideal_screener_logic,
                    o.theme_reason,
                    o.invalidation_signals_json,
                    o.matched_sources_json,
                    o.suggested_screeners_json,
                    o.load_ts AS overlay_load_ts,
                    u.load_ts AS universe_load_ts,
                    ROW_NUMBER() OVER (
                        PARTITION BY UPPER(TRIM(u.symbol))
                        ORDER BY
                            CASE WHEN e.exposure_source = 'portfolio' THEN 0 ELSE 1 END,
                            o.pressure_score DESC NULLS LAST,
                            u.context_rank ASC NULLS LAST,
                            o.theme_id
                    ) AS rn
                FROM exposure e
                JOIN latest_universe u
                  ON UPPER(TRIM(u.symbol)) = e.symbol
                JOIN latest_overlays o
                  ON TRUE
                LEFT JOIN theme_sector_alias tsa
                  ON tsa.overlay_sector_key = regexp_replace(upper(coalesce(o.sector_name, o.sector_code, '')), '[^A-Z0-9]', '', 'g')
                WHERE (
                    regexp_replace(upper(coalesce(u.sector_name, '')), '[^A-Z0-9]', '', 'g') = regexp_replace(upper(coalesce(o.sector_name, '')), '[^A-Z0-9]', '', 'g')
                    OR regexp_replace(upper(coalesce(u.sector_code, '')), '[^A-Z0-9]', '', 'g') = regexp_replace(upper(coalesce(o.sector_code, '')), '[^A-Z0-9]', '', 'g')
                    OR upper(coalesce(u.sector_code, '')) = tsa.universe_sector_code
                )
            )
            SELECT *
            FROM joined
            WHERE rn = 1
            ORDER BY pressure_score DESC NULLS LAST, context_rank ASC NULLS LAST, symbol
            LIMIT %(limit)s
            """,
            params={"asof_date": asof_cutoff, "limit": max(1, min(int(limit), 500))},
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.signal_refresh",
            fallback_type="signal_refresh_theme_context_targets_failed",
            source=THEME_CONTEXT_OVERLAYS_TABLE,
            severity="warn",
            reason="Signal refresh could not load negative theme-context exposure targets.",
            error=exc,
            metadata={"asof_date": str(asof_cutoff), "asof_operator": cutoff_operator, "limit": int(limit), "portfolio_table_available": bool(portfolio_exists)},
        )
        return pd.DataFrame()


def load_negative_macro_context_exposure_targets(*, asof_date: Any = None, limit: int = 50) -> pd.DataFrame:
    if int(limit) <= 0:
        return pd.DataFrame()
    required_tables = [MACRO_CONTEXT_OVERLAYS_TABLE, MARKET_CONTEXT_UNIVERSE_TABLE, WATCHLIST_TABLE]
    if not all(table_exists(table_name) for table_name in required_tables):
        return pd.DataFrame()
    portfolio_exists = table_exists(PORTFOLIO_TABLE)
    effective_asof = _date(asof_date)
    asof_cutoff, asof_operator = _point_in_time_cutoff(asof_date if asof_date is not None else effective_asof)
    cutoff_operator = "<" if asof_operator == "<" else "<="
    portfolio_cte, portfolio_union = _portfolio_exposure_cte(portfolio_exists, asof_operator=cutoff_operator)
    try:
        return sql_to_df(
            f"""
            WITH macro_sector_alias(overlay_sector_key, universe_sector_code) AS (
                VALUES
                {macro_sector_alias_values_sql()}
            ),
            latest_overlays AS (
                SELECT *
                FROM {MACRO_CONTEXT_OVERLAYS_TABLE}
                WHERE asof_date = (
                    SELECT MAX(asof_date)
                    FROM {MACRO_CONTEXT_OVERLAYS_TABLE}
                    WHERE asof_date {cutoff_operator} %(asof_date)s
                )
                  AND COALESCE(production_status, 'active') = 'active'
                  AND COALESCE(authority_scope, 'watchlist_pressure_only') = 'watchlist_pressure_only'
                  AND LOWER(TRIM(COALESCE(direction, ''))) = 'negative'
                  AND NULLIF(TRIM(COALESCE(sector_name, sector_code, '')), '') IS NOT NULL
            ),
            latest_universe AS (
                SELECT *
                FROM {MARKET_CONTEXT_UNIVERSE_TABLE}
                WHERE asof_date = (
                    SELECT MAX(asof_date)
                    FROM {MARKET_CONTEXT_UNIVERSE_TABLE}
                    WHERE asof_date {cutoff_operator} %(asof_date)s
                )
                  AND NULLIF(TRIM(symbol), '') IS NOT NULL
                  AND NULLIF(TRIM(company_master_id), '') IS NOT NULL
            ),
            watchlist_exposure AS (
                SELECT DISTINCT UPPER(TRIM(symbol)) AS symbol, 'watchlist'::text AS exposure_source
                FROM {WATCHLIST_TABLE}
                WHERE asof_date = (
                    SELECT MAX(asof_date)
                    FROM {WATCHLIST_TABLE}
                    WHERE asof_date {cutoff_operator} %(asof_date)s
                )
                  AND NULLIF(TRIM(symbol), '') IS NOT NULL
                  AND COALESCE(watch_enabled, TRUE) = TRUE
                  AND COALESCE(LOWER(TRIM(watch_status)), 'active') IN ('active', 'review_manual', 'theme_context')
            )
            {portfolio_cte},
            exposure AS (
                SELECT * FROM watchlist_exposure
                {portfolio_union}
            ),
            joined AS (
                SELECT
                    UPPER(TRIM(u.symbol)) AS symbol,
                    u.company_master_id,
                    e.exposure_source,
                    u.asof_date AS universe_asof_date,
                    o.asof_date AS overlay_asof_date,
                    o.overlay_id,
                    o.macro_signal_id AS theme_id,
                    o.macro_signal_name AS theme_name,
                    o.macro_signal_id AS context_class,
                    o.trigger_reason AS context_reason,
                    o.sector_name AS overlay_sector_name,
                    o.sector_code AS overlay_sector_code,
                    u.sector_name AS universe_sector_name,
                    u.sector_code AS universe_sector_code,
                    u.context_rank,
                    u.technical_leadership_score,
                    u.macro_sensitivity_tag,
                    o.pressure_score,
                    o.macro_stress_score,
                    o.macro_risk_state,
                    o.trigger_reason AS theme_reason,
                    o.matched_sources_json,
                    o.load_ts AS overlay_load_ts,
                    u.load_ts AS universe_load_ts,
                    'macro_context'::text AS context_source,
                    'macro_sector_alias_reviewed'::text AS sector_mapping_source,
                    ROW_NUMBER() OVER (
                        PARTITION BY UPPER(TRIM(u.symbol)), o.overlay_id
                        ORDER BY
                            CASE WHEN e.exposure_source = 'portfolio' THEN 0 ELSE 1 END,
                            o.pressure_score DESC NULLS LAST,
                            u.context_rank ASC NULLS LAST
                    ) AS rn
                FROM exposure e
                JOIN latest_universe u
                  ON UPPER(TRIM(u.symbol)) = e.symbol
                JOIN latest_overlays o
                  ON TRUE
                LEFT JOIN macro_sector_alias msa
                  ON msa.overlay_sector_key = regexp_replace(upper(coalesce(o.sector_name, o.sector_code, '')), '[^A-Z0-9]', '', 'g')
                WHERE (
                    regexp_replace(upper(coalesce(u.sector_name, '')), '[^A-Z0-9]', '', 'g') = regexp_replace(upper(coalesce(o.sector_name, '')), '[^A-Z0-9]', '', 'g')
                    OR regexp_replace(upper(coalesce(u.sector_code, '')), '[^A-Z0-9]', '', 'g') = regexp_replace(upper(coalesce(o.sector_code, '')), '[^A-Z0-9]', '', 'g')
                    OR upper(coalesce(u.sector_code, '')) = msa.universe_sector_code
                )
            )
            SELECT *
            FROM joined
            WHERE rn = 1
            ORDER BY pressure_score DESC NULLS LAST, context_rank ASC NULLS LAST, symbol
            LIMIT %(limit)s
            """,
            params={"asof_date": asof_cutoff, "limit": max(1, min(int(limit), 500))},
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.signal_refresh",
            fallback_type="signal_refresh_macro_context_targets_failed",
            source=MACRO_CONTEXT_OVERLAYS_TABLE,
            severity="warn",
            reason="Signal refresh could not load negative macro-context exposure targets.",
            error=exc,
            metadata={"asof_date": str(asof_cutoff), "asof_operator": cutoff_operator, "limit": int(limit), "portfolio_table_available": bool(portfolio_exists)},
        )
        return pd.DataFrame()


def _positive_context_watch_suppression_reason(item: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    watch_enabled = item.get("watch_enabled")
    if watch_enabled is not None and str(watch_enabled).strip().lower() not in {"1", "true", "yes", "y", "on"}:
        reasons.append("watch_disabled_or_inactive")
    candidate_state = str(item.get("candidate_state") or "").strip().upper()
    if candidate_state and candidate_state not in {"WATCH_EVENT", "WATCH_BREAKOUT"}:
        reasons.append("candidate_state_not_watch")
    policy_effect = str(item.get("context_policy_effect") or "").strip().lower()
    if policy_effect.startswith("suppress_"):
        reasons.append(policy_effect)
    return reasons


def _negative_context_suppressed_symbols(frame: pd.DataFrame, *, asof_date: Any) -> dict[str, dict[str, Any]]:
    """Symbols with fresh negative direct context that should withhold a review-only positive WATCH.

    Reuses the watchlist builder's reliability-gated negative-suppression rule so fast signal
    refresh stays consistent with the persisted watchlist instead of emitting a positive WATCH for
    a symbol that just received fresh negative announcement/exchange/bhavcopy context. Suppression
    withholds a review-only WATCH only; it grants no sell, portfolio, or broker authority. Fails
    open (no suppression) with telemetry when the negative-context source is unavailable.
    """
    if not isinstance(frame, pd.DataFrame) or frame.empty or "symbol" not in frame.columns or "candidate_state" not in frame.columns:
        return {}
    positive_mask = frame["candidate_state"].astype("string").str.strip().str.upper().isin(["WATCH_EVENT", "WATCH_BREAKOUT"])
    symbols = sorted({
        str(value).strip().upper()
        for value in frame.loc[positive_mask, "symbol"].tolist()
        if str(value or "").strip()
    })
    if not symbols:
        return {}
    try:
        negative = load_negative_context_overlay_suppression_candidates(asof_date=asof_date, symbols=symbols)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.signal_refresh",
            fallback_type="signal_refresh_negative_context_suppression_failed",
            source="advisory_context_overlays",
            severity="warn",
            reason="Signal refresh could not load fresh negative context-overlay suppression; positive watch targets were not negative-suppressed this run.",
            error=exc,
            metadata={"symbol_count": len(symbols)},
        )
        return {}
    if not isinstance(negative, pd.DataFrame) or negative.empty or "symbol" not in negative.columns:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for item in negative.to_dict(orient="records"):
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol or symbol in out:
            continue
        out[symbol] = {
            "negative_context_source": _text(item.get("context_source")) or "context_overlay",
            "negative_context_overlay_id": _text(item.get("context_overlay_id")),
        }
    return out


def _split_positive_context_overlay_watch_targets(
    raw: pd.DataFrame,
    *,
    limit: int,
    negative_suppressed_symbols: dict[str, dict[str, Any]] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, list[dict[str, Any]]]:
    if not isinstance(raw, pd.DataFrame) or raw.empty:
        return pd.DataFrame(), pd.DataFrame(), []
    out = raw.copy()
    out["symbol"] = out["symbol"].astype("string").str.strip().str.upper()
    out["setup_score"] = pd.to_numeric(out.get("setup_score"), errors="coerce")
    out = out.dropna(subset=["symbol"])
    if out.empty:
        return pd.DataFrame(), pd.DataFrame(), []
    negative_suppressed_symbols = negative_suppressed_symbols or {}
    keep_mask: list[bool] = []
    suppressed: list[dict[str, Any]] = []
    for item in out.to_dict(orient="records"):
        reasons = _positive_context_watch_suppression_reason(item)
        symbol_key = str(item.get("symbol") or "").strip().upper()
        negative_hit = negative_suppressed_symbols.get(symbol_key)
        if negative_hit:
            reasons = list(reasons) + ["suppress_context_fresh_negative_overlay_no_buy_authority"]
        keep_mask.append(not reasons)
        if reasons:
            entry = {
                "symbol": _text(item.get("symbol")),
                "context_source": _text(item.get("context_source")) or "context_overlay",
                "context_overlay_id": _text(item.get("context_overlay_id")),
                "context_policy_effect": _text(item.get("context_policy_effect")),
                "candidate_state": _text(item.get("candidate_state")),
                "watch_enabled": item.get("watch_enabled"),
                "suppression_reasons": reasons,
                "watch_reason_detail": _text(item.get("watch_reason_detail")),
                "authority_scope": SIGNAL_REFRESH_AUTHORITY_CONTRACT["authority_scope"],
                "broker_execution_allowed": False,
            }
            if negative_hit:
                entry["negative_context_source"] = negative_hit.get("negative_context_source")
                entry["negative_context_overlay_id"] = negative_hit.get("negative_context_overlay_id")
            suppressed.append(entry)
    kept = out.loc[keep_mask].copy()
    if kept.empty:
        return out, pd.DataFrame(), suppressed
    sort_cols = [column for column in ["setup_score", "asof_date", "symbol"] if column in kept.columns]
    if sort_cols:
        kept = kept.sort_values(
            sort_cols,
            ascending=[False if column == "setup_score" else True for column in sort_cols],
            na_position="last",
        )
    return out, kept.head(max(1, min(int(limit), 500))), suppressed


def load_positive_context_overlay_watch_target_frames(*, asof_date: Any = None, limit: int = 50) -> tuple[pd.DataFrame, pd.DataFrame, list[dict[str, Any]]]:
    if int(limit) <= 0:
        return pd.DataFrame(), pd.DataFrame(), []
    effective_asof = _date(asof_date)
    try:
        frame = load_context_overlay_watch_candidates(asof_date=asof_date if asof_date is not None else effective_asof, symbols=None)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.signal_refresh",
            fallback_type="signal_refresh_positive_context_watch_targets_failed",
            source="advisory_context_overlays",
            severity="warn",
            reason="Signal refresh could not load positive/watch context-overlay discovery targets.",
            error=exc,
            metadata={"asof_date": str(effective_asof), "limit": int(limit)},
        )
        return pd.DataFrame(), pd.DataFrame(), []
    negative_suppressed = _negative_context_suppressed_symbols(
        frame, asof_date=asof_date if asof_date is not None else effective_asof
    )
    return _split_positive_context_overlay_watch_targets(
        frame, limit=limit, negative_suppressed_symbols=negative_suppressed
    )


def load_positive_context_overlay_watch_targets(*, asof_date: Any = None, limit: int = 50) -> pd.DataFrame:
    _, kept, _ = load_positive_context_overlay_watch_target_frames(asof_date=asof_date, limit=limit)
    return kept


def load_causal_memory_signal_targets(*, asof_date: Any = None, limit: int = 50) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Load fresh symbol-scoped causal-memory rows with candidate-helpful evidence."""

    diagnostics: dict[str, Any] = {
        "memory_table": CAUSAL_EVENT_MEMORY_TABLE,
        "summary_table": CAUSAL_EVENT_MEMORY_SUMMARY_TABLE,
        "authority_scope": SIGNAL_REFRESH_AUTHORITY_CONTRACT["authority_scope"],
        "portfolio_authority": SIGNAL_REFRESH_AUTHORITY_CONTRACT["portfolio_authority"],
        "broker_execution_allowed": False,
        "full_advisory_required": True,
    }
    if int(limit) <= 0:
        diagnostics.update({"status": "disabled_by_limit", "target_rows": 0})
        return pd.DataFrame(), diagnostics
    if not table_exists(CAUSAL_EVENT_MEMORY_TABLE):
        diagnostics.update({"status": "missing_memory_table", "target_rows": 0})
        return pd.DataFrame(), diagnostics
    if not table_exists(CAUSAL_EVENT_MEMORY_SUMMARY_TABLE):
        diagnostics.update({"status": "missing_summary_table", "target_rows": 0})
        return pd.DataFrame(), diagnostics

    effective_asof = _date(asof_date)
    asof_cutoff, asof_operator = _point_in_time_cutoff(asof_date if asof_date is not None else effective_asof)
    cutoff_operator = "<" if asof_operator == "<" else "<="
    from_cutoff = asof_cutoff - pd.Timedelta(days=max(1, int(CAUSAL_MEMORY_LOOKBACK_DAYS)))
    try:
        df = sql_to_df(
            f"""
            WITH latest_summary AS (
                SELECT DISTINCT ON (
                    LOWER(TRIM(context_source)),
                    LOWER(TRIM(context_class)),
                    LOWER(TRIM(event_type)),
                    LOWER(TRIM(direction)),
                    LOWER(TRIM(event_state))
                )
                    context_source,
                    context_class,
                    event_type,
                    direction,
                    event_state,
                    horizon_days,
                    classification,
                    recommendation,
                    matured_count,
                    symbol_count,
                    avg_excess_return_after_cost,
                    avg_excess_directional_helpfulness_score,
                    excess_direction_hit_rate_after_cost,
                    evaluated_at
                FROM {CAUSAL_EVENT_MEMORY_SUMMARY_TABLE}
                WHERE evaluated_at {cutoff_operator} %(asof_date)s
                ORDER BY
                    LOWER(TRIM(context_source)),
                    LOWER(TRIM(context_class)),
                    LOWER(TRIM(event_type)),
                    LOWER(TRIM(direction)),
                    LOWER(TRIM(event_state)),
                    evaluated_at DESC NULLS LAST,
                    matured_count DESC NULLS LAST,
                    horizon_days ASC NULLS LAST
            ),
            memory_candidates AS (
                SELECT
                    m.asof_date AS memory_asof_date,
                    m.memory_id,
                    UPPER(TRIM(m.symbol)) AS symbol,
                    m.context_source,
                    m.event_group,
                    m.event_type,
                    m.context_class,
                    LOWER(TRIM(m.direction)) AS direction,
                    LOWER(TRIM(m.event_state)) AS event_state,
                    m.pressure_score,
                    m.decayed_pressure_score,
                    m.event_count,
                    m.first_seen_at,
                    m.last_seen_at,
                    m.freshness_days,
                    m.contradiction_state,
                    m.source_refs_json,
                    m.source_summary_json,
                    s.horizon_days AS evaluation_horizon_days,
                    s.classification AS memory_reliability_classification,
                    s.recommendation AS memory_reliability_recommendation,
                    s.matured_count AS memory_reliability_matured_count,
                    s.symbol_count AS memory_reliability_symbol_count,
                    s.avg_excess_return_after_cost,
                    s.avg_excess_directional_helpfulness_score,
                    s.excess_direction_hit_rate_after_cost,
                    s.evaluated_at AS memory_reliability_evaluated_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY UPPER(TRIM(m.symbol))
                        ORDER BY
                            CASE WHEN LOWER(TRIM(m.direction)) = 'negative' THEN 0 ELSE 1 END,
                            m.decayed_pressure_score DESC NULLS LAST,
                            m.event_count DESC NULLS LAST,
                            m.last_seen_at DESC NULLS LAST
                    ) AS rn
                FROM {CAUSAL_EVENT_MEMORY_TABLE} m
                JOIN latest_summary s
                  ON LOWER(TRIM(COALESCE(s.context_source, 'unknown'))) = LOWER(TRIM(COALESCE(m.context_source, 'unknown')))
                 AND LOWER(TRIM(COALESCE(s.context_class, 'unknown'))) = LOWER(TRIM(COALESCE(m.context_class, 'unknown')))
                 AND LOWER(TRIM(COALESCE(s.event_type, 'unknown'))) = LOWER(TRIM(COALESCE(m.event_type, 'unknown')))
                 AND LOWER(TRIM(COALESCE(s.direction, 'unknown'))) = LOWER(TRIM(COALESCE(m.direction, 'unknown')))
                 AND LOWER(TRIM(COALESCE(s.event_state, 'unknown'))) = LOWER(TRIM(COALESCE(m.event_state, 'unknown')))
                WHERE m.asof_date {cutoff_operator} %(asof_date)s
                  AND m.asof_date >= %(from_cutoff)s
                  AND NULLIF(TRIM(m.symbol), '') IS NOT NULL
                  AND LOWER(TRIM(COALESCE(m.direction, ''))) IN ('positive', 'negative')
                  AND LOWER(TRIM(COALESCE(m.event_state, ''))) IN ('positive_watch_pressure', 'negative_derisk_pressure')
                  AND COALESCE(LOWER(TRIM(m.contradiction_state)), 'none') = 'none'
                  AND COALESCE(m.decayed_pressure_score, 0) >= %(min_decayed_score)s
                  AND LOWER(TRIM(COALESCE(s.classification, ''))) = 'candidate_helpful'
                  AND COALESCE(s.avg_excess_directional_helpfulness_score, -999.0) > %(min_excess_directional_helpfulness_score)s
                  AND COALESCE(s.excess_direction_hit_rate_after_cost, 0) >= %(min_excess_direction_hit_rate)s
            )
            SELECT *
            FROM memory_candidates
            WHERE rn = 1
            ORDER BY decayed_pressure_score DESC NULLS LAST, symbol
            LIMIT %(limit)s
            """,
            params={
                "asof_date": asof_cutoff,
                "from_cutoff": from_cutoff,
                "min_decayed_score": float(CAUSAL_MEMORY_MIN_DECAYED_PRESSURE_SCORE),
                "min_excess_directional_helpfulness_score": float(CAUSAL_MEMORY_MIN_EXCESS_DIRECTIONAL_HELPFULNESS_SCORE),
                "min_excess_direction_hit_rate": float(CAUSAL_MEMORY_MIN_EXCESS_DIRECTION_HIT_RATE),
                "limit": max(1, min(int(limit), 500)),
            },
            retries=3,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.signal_refresh",
            fallback_type="signal_refresh_causal_memory_targets_failed",
            source=CAUSAL_EVENT_MEMORY_TABLE,
            severity="warn",
            reason="Signal refresh could not load candidate-helpful causal-memory targets.",
            error=exc,
            metadata={"asof_date": str(asof_cutoff), "asof_operator": cutoff_operator, "from_cutoff": str(from_cutoff), "limit": int(limit)},
        )
        diagnostics.update({"status": "load_failed", "target_rows": 0, "error_type": exc.__class__.__name__})
        return pd.DataFrame(), diagnostics

    suppression_df = pd.DataFrame()
    suppression_diagnostics_degraded = False
    try:
        suppression_df = sql_to_df(
            f"""
            WITH latest_summary AS (
                SELECT DISTINCT ON (
                    LOWER(TRIM(context_source)),
                    LOWER(TRIM(context_class)),
                    LOWER(TRIM(event_type)),
                    LOWER(TRIM(direction)),
                    LOWER(TRIM(event_state))
                )
                    context_source,
                    context_class,
                    event_type,
                    direction,
                    event_state,
                    classification,
                    matured_count,
                    evaluated_at
                FROM {CAUSAL_EVENT_MEMORY_SUMMARY_TABLE}
                WHERE evaluated_at {cutoff_operator} %(asof_date)s
                ORDER BY
                    LOWER(TRIM(context_source)),
                    LOWER(TRIM(context_class)),
                    LOWER(TRIM(event_type)),
                    LOWER(TRIM(direction)),
                    LOWER(TRIM(event_state)),
                    evaluated_at DESC NULLS LAST,
                    matured_count DESC NULLS LAST,
                    horizon_days ASC NULLS LAST
            )
            SELECT
                LOWER(TRIM(COALESCE(s.classification, 'unknown'))) AS classification,
                COUNT(*) AS memory_row_count,
                COUNT(DISTINCT UPPER(TRIM(m.symbol))) AS symbol_count,
                MAX(s.evaluated_at) AS latest_evaluated_at
            FROM {CAUSAL_EVENT_MEMORY_TABLE} m
            JOIN latest_summary s
              ON LOWER(TRIM(COALESCE(s.context_source, 'unknown'))) = LOWER(TRIM(COALESCE(m.context_source, 'unknown')))
             AND LOWER(TRIM(COALESCE(s.context_class, 'unknown'))) = LOWER(TRIM(COALESCE(m.context_class, 'unknown')))
             AND LOWER(TRIM(COALESCE(s.event_type, 'unknown'))) = LOWER(TRIM(COALESCE(m.event_type, 'unknown')))
             AND LOWER(TRIM(COALESCE(s.direction, 'unknown'))) = LOWER(TRIM(COALESCE(m.direction, 'unknown')))
             AND LOWER(TRIM(COALESCE(s.event_state, 'unknown'))) = LOWER(TRIM(COALESCE(m.event_state, 'unknown')))
            WHERE m.asof_date {cutoff_operator} %(asof_date)s
              AND m.asof_date >= %(from_cutoff)s
              AND NULLIF(TRIM(m.symbol), '') IS NOT NULL
              AND LOWER(TRIM(COALESCE(m.direction, ''))) IN ('positive', 'negative')
              AND LOWER(TRIM(COALESCE(m.event_state, ''))) IN ('positive_watch_pressure', 'negative_derisk_pressure')
              AND COALESCE(LOWER(TRIM(m.contradiction_state)), 'none') = 'none'
              AND COALESCE(m.decayed_pressure_score, 0) >= %(min_decayed_score)s
            GROUP BY LOWER(TRIM(COALESCE(s.classification, 'unknown')))
            ORDER BY memory_row_count DESC
            """,
            params={
                "asof_date": asof_cutoff,
                "from_cutoff": from_cutoff,
                "min_decayed_score": float(CAUSAL_MEMORY_MIN_DECAYED_PRESSURE_SCORE),
            },
            retries=3,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.signal_refresh",
            fallback_type="signal_refresh_causal_memory_suppression_diagnostics_failed",
            source=CAUSAL_EVENT_MEMORY_TABLE,
            severity="warn",
            reason="Signal refresh could not load causal-memory classification suppression diagnostics.",
            error=exc,
            metadata={"asof_date": str(asof_cutoff), "asof_operator": cutoff_operator, "from_cutoff": str(from_cutoff), "limit": int(limit)},
        )
        suppression_diagnostics_degraded = True

    suppression_counts: dict[str, int] = {}
    suppression_symbol_counts: dict[str, int] = {}
    latest_evaluated_at = None
    if isinstance(suppression_df, pd.DataFrame) and not suppression_df.empty:
        for item in suppression_df.to_dict(orient="records"):
            classification = _text(item.get("classification")) or "unknown"
            suppression_counts[classification] = int(_num(item.get("memory_row_count")) or 0)
            suppression_symbol_counts[classification] = int(_num(item.get("symbol_count")) or 0)
        if "latest_evaluated_at" in suppression_df.columns:
            latest_series = pd.to_datetime(suppression_df.get("latest_evaluated_at"), utc=True, errors="coerce")
            if latest_series.notna().any():
                latest_evaluated_at = latest_series.max()
    candidate_helpful_rows = int(suppression_counts.get("candidate_helpful", 0))
    harmful_or_no_lift_rows = int(suppression_counts.get("hurts_or_no_lift", 0))
    benchmark_beta_only_rows = int(suppression_counts.get("benchmark_beta_not_memory_alpha", 0))
    benchmark_unattributed_rows = int(suppression_counts.get("needs_benchmark_attribution", 0))
    non_candidate_rows = max(sum(suppression_counts.values()) - candidate_helpful_rows, 0)

    diagnostics.update(
        {
            "status": "ok",
            "asof_cutoff": asof_cutoff,
            "asof_operator": cutoff_operator,
            "from_cutoff": from_cutoff,
            "min_decayed_pressure_score": float(CAUSAL_MEMORY_MIN_DECAYED_PRESSURE_SCORE),
            "min_excess_directional_helpfulness_score": float(CAUSAL_MEMORY_MIN_EXCESS_DIRECTIONAL_HELPFULNESS_SCORE),
            "min_excess_direction_hit_rate": float(CAUSAL_MEMORY_MIN_EXCESS_DIRECTION_HIT_RATE),
            "lookback_days": int(CAUSAL_MEMORY_LOOKBACK_DAYS),
            "target_rows": int(len(df)),
            "classification_memory_row_counts": suppression_counts,
            "classification_symbol_counts": suppression_symbol_counts,
            "candidate_helpful_memory_rows": candidate_helpful_rows,
            "non_candidate_suppressed_memory_rows": non_candidate_rows,
            "harmful_or_no_lift_suppressed_memory_rows": harmful_or_no_lift_rows,
            "benchmark_beta_not_memory_alpha_suppressed_memory_rows": benchmark_beta_only_rows,
            "needs_benchmark_attribution_suppressed_memory_rows": benchmark_unattributed_rows,
            "latest_reliability_evaluated_at": latest_evaluated_at,
            "suppression_diagnostics_degraded": suppression_diagnostics_degraded,
            "suppression_policy": "only exact candidate_helpful causal-memory evaluator groups with positive benchmark-excess evidence can create review-only signals",
        }
    )
    return df, diagnostics


def _context_reliability_classification(family: str, reliability: dict[str, Any]) -> str | None:
    families = reliability.get("families") if isinstance(reliability, dict) else {}
    if not isinstance(families, dict):
        return None
    row = families.get(str(family or "").strip().lower())
    if not isinstance(row, dict):
        return None
    classification = str(row.get("classification") or "").strip().lower()
    return classification or None


def _context_reliability_family_row(family: str, reliability: dict[str, Any]) -> dict[str, Any] | None:
    families = reliability.get("families") if isinstance(reliability, dict) else {}
    if not isinstance(families, dict):
        return None
    row = families.get(str(family or "").strip().lower())
    return row if isinstance(row, dict) else None


def _runtime_contract_allows(row: dict[str, Any] | None, use_name: str, *, legacy_classification: str | None = None) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("runtime_policy_contract_synthesized") is True:
        return False
    contract = row.get("runtime_policy_contract")
    if isinstance(contract, dict):
        allowed = contract.get("allowed_runtime_uses")
        if isinstance(allowed, dict) and use_name in allowed:
            return bool(allowed.get(use_name))
    return False


def _context_class_reliability_classification(family: str, context_class: Any, reliability: dict[str, Any]) -> str | None:
    family_row = _context_reliability_family_row(family, reliability)
    if not isinstance(family_row, dict):
        return None
    target_class = str(context_class or "").strip().upper()
    if not target_class or target_class in {"<NA>", "NAN", "NONE"}:
        return None
    for item in family_row.get("context_class_diagnostics") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("context_class") or "").strip().upper() != target_class:
            continue
        classification = str(item.get("classification") or "").strip().lower()
        return classification or None
    return None


def _sector_key_from_values(*values: Any) -> str | None:
    for value in values:
        if pd.api.types.is_scalar(value) and pd.isna(value):
            continue
        text = str(value or "").strip()
        if not text or text.upper() in {"<NA>", "NAN", "NONE"}:
            continue
        return "".join(ch for ch in text.upper() if ch.isalnum())
    return None


def _context_sector_reliability_classification(
    family: str,
    item: dict[str, Any],
    reliability: dict[str, Any],
) -> str | None:
    family_row = _context_reliability_family_row(family, reliability)
    if not isinstance(family_row, dict):
        return None
    target_keys = {
        key
        for key in [
            _sector_key_from_values(item.get("universe_sector_code"), item.get("overlay_sector_code"), item.get("sector_code")),
            _sector_key_from_values(item.get("universe_sector_name"), item.get("overlay_sector_name"), item.get("sector_name")),
        ]
        if key
    }
    if not target_keys:
        return None
    for row in family_row.get("sector_diagnostics") or []:
        if not isinstance(row, dict):
            continue
        row_keys = {
            key
            for key in [
                _sector_key_from_values(row.get("sector_code")),
                _sector_key_from_values(row.get("sector_name")),
                _sector_key_from_values(row.get("sector_key")),
            ]
            if key
        }
        if not target_keys & row_keys:
            continue
        classification = str(row.get("classification") or "").strip().lower()
        return classification or None
    return None


def _filter_negative_context_targets_by_reliability(
    targets: pd.DataFrame,
    *,
    reliability: dict[str, Any],
    default_family: str | None = None,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    if targets.empty:
        return targets.copy(), []
    out = targets.copy()
    if "context_source" not in out.columns:
        out["context_source"] = default_family
    elif default_family is not None:
        out["context_source"] = out["context_source"].fillna(default_family)
    kept_rows: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    for item in out.to_dict(orient="records"):
        family = str(item.get("context_source") or default_family or "").strip().lower()
        family_row = _context_reliability_family_row(family, reliability)
        classification = _context_reliability_classification(family, reliability)
        class_classification = _context_class_reliability_classification(family, item.get("context_class"), reliability)
        sector_classification = _context_sector_reliability_classification(family, item, reliability)
        derisk_allowed = bool(
            _runtime_contract_allows(family_row, "de_risk_review", legacy_classification=classification)
            or class_classification == "protective_candidate"
        )
        item["context_reliability_classification"] = classification
        item["context_class_reliability_classification"] = class_classification
        item["context_sector_reliability_classification"] = sector_classification
        item["context_reliability_runtime_policy_contract"] = family_row.get("runtime_policy_contract") if isinstance(family_row, dict) else None
        item["de_risk_review_allowed_by_runtime_contract"] = bool(derisk_allowed)
        item["context_reliability_effect"] = (
            "suppressed_derisk_runtime_contract_disallows"
            if family_row is not None and not derisk_allowed
            else
            "suppressed_derisk_false_positive_risk"
            if classification in CONTEXT_RELIABILITY_DERISK_SUPPRESS_CLASSES
            else "suppressed_derisk_context_class_false_positive_risk"
            if class_classification in CONTEXT_RELIABILITY_DERISK_SUPPRESS_CLASSES
            else "suppressed_derisk_sector_false_positive_risk"
            if sector_classification in CONTEXT_RELIABILITY_DERISK_SUPPRESS_CLASSES
            else "allowed_review_only_derisk"
        )
        if (
            (family_row is not None and not derisk_allowed)
            or classification in CONTEXT_RELIABILITY_DERISK_SUPPRESS_CLASSES
            or class_classification in CONTEXT_RELIABILITY_DERISK_SUPPRESS_CLASSES
            or sector_classification in CONTEXT_RELIABILITY_DERISK_SUPPRESS_CLASSES
        ):
            suppressed.append(
                {
                    "symbol": str(item.get("symbol") or "").strip().upper(),
                    "context_source": family,
                    "classification": classification,
                    "context_class": item.get("context_class"),
                    "context_class_classification": class_classification,
                    "context_sector_classification": sector_classification,
                    "runtime_policy_contract": family_row.get("runtime_policy_contract") if isinstance(family_row, dict) else None,
                    "de_risk_review_allowed_by_runtime_contract": bool(derisk_allowed),
                    "overlay_id": item.get("overlay_id"),
                    "reason": (
                        "Exact context-class reliability says this negative context is a false-positive or horizon-inconsistent de-risk signal."
                        if class_classification in CONTEXT_RELIABILITY_DERISK_SUPPRESS_CLASSES
                        else
                        "Sector reliability says this negative context is a false-positive or horizon-inconsistent de-risk signal for the matched sector."
                        if sector_classification in CONTEXT_RELIABILITY_DERISK_SUPPRESS_CLASSES
                        else
                        "Source-family runtime policy contract does not allow this context family to create de-risk review pressure."
                        if family_row is not None and not derisk_allowed
                        else "Source-family reliability says this negative context is harmful, negative after cost, or horizon-inconsistent."
                    ),
                    "authority_scope": "review_input_only",
                    "portfolio_authority": "none",
                    "broker_execution_allowed": False,
                }
            )
            continue
        kept_rows.append(item)
    return pd.DataFrame(kept_rows), suppressed


def build_causal_memory_signal_rows(targets: pd.DataFrame, *, dry_run: bool = False) -> list[dict[str, Any]]:
    if not isinstance(targets, pd.DataFrame) or targets.empty:
        return []
    rows: list[dict[str, Any]] = []
    refreshed_at = pd.Timestamp.utcnow()
    for item in targets.to_dict(orient="records"):
        symbol = str(item.get("symbol") or "").strip().upper()
        memory_id = _text(item.get("memory_id"))
        if not symbol or not memory_id:
            continue
        effective_asof = _date(item.get("memory_asof_date"))
        previous_action_row = load_latest_action(symbol, asof_date=effective_asof)
        previous_action = _normalized_action((previous_action_row or {}).get("action_code"))
        direction = str(item.get("direction") or "").strip().lower()
        signal_action = "REDUCE_EXPOSURE_REVIEW" if direction == "negative" else "WATCH"
        signal_status = "causal_memory_derisk" if signal_action == "REDUCE_EXPOSURE_REVIEW" else "causal_memory_watch"
        context_source = _text(item.get("context_source")) or "causal_event_memory"
        context_class = _text(item.get("context_class")) or "memory_context"
        event_type = _text(item.get("event_type")) or context_class
        decayed_score = _num(item.get("decayed_pressure_score")) or _num(item.get("pressure_score")) or 0.0
        reliability_classification = _text(item.get("memory_reliability_classification")) or "candidate_helpful"
        action_reason = (
            f"Causal event memory found candidate-helpful {direction or 'watch'} pressure for {symbol}: "
            f"{context_source}/{context_class}/{event_type}. "
            f"Decayed pressure={round(float(decayed_score), 4)}; reliability={reliability_classification}. "
            "This is review-input only; full advisory must confirm technical, liquidity, risk, lifecycle, and execution gates."
        )
        payload = {
            "causal_event_memory": item,
            "previous_action": previous_action_row or {},
            "authority_contract": SIGNAL_REFRESH_AUTHORITY_CONTRACT,
            "memory_policy_effect": (
                "review_only_de_risk_pressure_no_sell_authority"
                if signal_action == "REDUCE_EXPOSURE_REVIEW"
                else "review_only_watch_pressure_no_buy_authority"
            ),
            "broker_execution_allowed": False,
            "portfolio_authority": "none",
            "full_advisory_required": True,
        }
        refresh_id = make_refresh_id(
            refreshed_at=refreshed_at,
            symbol=symbol,
            unique_id=memory_id,
            reason=f"causal_memory:{direction or 'watch'}",
        )
        effect = {
            "effect_type": "causal_memory_derisk_pressure" if signal_action == "REDUCE_EXPOSURE_REVIEW" else "causal_memory_watch_pressure",
            "effect_summary": (
                "Candidate-helpful causal event memory created review-only de-risk pressure."
                if signal_action == "REDUCE_EXPOSURE_REVIEW"
                else "Candidate-helpful causal event memory created review-only watch pressure."
            ),
            "previous_action": previous_action,
        }
        action_changed = bool(previous_action and signal_action != previous_action)
        trace_id = None
        if not dry_run:
            trace_id = safe_trace_call(
                append_trace,
                asof_date=effective_asof,
                symbol=symbol,
                unique_id=memory_id,
                trigger_type="signal_refresh:causal_event_memory",
                previous_action=previous_action,
                new_action=signal_action,
                final_action=signal_action,
                final_reason=action_reason,
                source_table=TABLE_NAME,
                source_key=refresh_id,
                payload={"signal": {"signal_action": signal_action, "signal_status": signal_status}, "context": payload},
            )
            if trace_id:
                safe_trace_call(
                    append_trace_step,
                    trace_id=trace_id,
                    step_idx=1,
                    stage="causal_event_memory",
                    status=signal_status,
                    reason=action_reason,
                    input_payload={
                        "symbol": symbol,
                        "memory_id": memory_id,
                        "direction": direction,
                        "context_source": context_source,
                        "context_class": context_class,
                        "event_type": event_type,
                        "decayed_pressure_score": decayed_score,
                    },
                    output_payload={"signal_action": signal_action, "previous_action": previous_action, "action_changed": action_changed},
                    payload={"source_tables": [CAUSAL_EVENT_MEMORY_TABLE, CAUSAL_EVENT_MEMORY_SUMMARY_TABLE]},
                )
        rows.append(
            {
                "refresh_id": refresh_id,
                "refreshed_at": refreshed_at,
                "asof_date": effective_asof,
                "symbol": symbol,
                "unique_id": memory_id,
                "reason": f"causal_memory:{direction or 'watch'}",
                "signal_action": signal_action,
                "signal_status": signal_status,
                "signal_source": "causal_event_memory",
                "confidence": decayed_score,
                "action_reason": action_reason,
                **effect,
                "action_changed": action_changed,
                **SIGNAL_REFRESH_AUTHORITY_CONTRACT,
                "action_payload_json": json_dumps(payload),
                "trace_id": trace_id,
                "dry_run": bool(dry_run),
                "load_ts": pd.Timestamp.utcnow(),
            }
        )
    return rows


def build_theme_context_signal_rows(targets: pd.DataFrame, *, dry_run: bool = False) -> list[dict[str, Any]]:
    if targets.empty:
        return []
    rows: list[dict[str, Any]] = []
    refreshed_at = pd.Timestamp.utcnow()
    for item in targets.to_dict(orient="records"):
        symbol = str(item.get("symbol") or "").strip().upper()
        overlay_id = _text(item.get("overlay_id"))
        if not symbol or not overlay_id:
            continue
        effective_asof = _date(item.get("overlay_asof_date") or item.get("universe_asof_date"))
        previous_action_row = load_latest_action(symbol, asof_date=effective_asof)
        previous_action = _normalized_action((previous_action_row or {}).get("action_code"))
        pressure = _num(item.get("pressure_score"))
        context_source = _text(item.get("context_source")) or "theme_context"
        context_label = "macro-context" if context_source == "macro_context" else "theme-context"
        theme_name = _text(item.get("theme_name")) or _text(item.get("theme_id")) or "theme"
        exposure_source = _text(item.get("exposure_source")) or "watchlist"
        sector_name = _text(item.get("universe_sector_name")) or _text(item.get("overlay_sector_name")) or "sector"
        reliability = _text(item.get("context_reliability_classification"))
        class_reliability = _text(item.get("context_class_reliability_classification"))
        reliability_effect = _text(item.get("context_reliability_effect")) or "allowed_review_only_derisk"
        source_contract = context_overlay_source_contract(
            context_source=context_source,
            overlay_id=overlay_id,
            symbol=symbol,
            event_class=item.get("theme_id") or item.get("context_class"),
            direction="negative",
            asof_date=item.get("overlay_asof_date") or item.get("universe_asof_date"),
        )
        action_reason = (
            f"Negative {context_label} pressure for {sector_name}: {theme_name}. "
            f"{symbol} is already in {exposure_source}; review exposure, thesis freshness, and exit/watch conditions. "
            "This is review-input only; full advisory remains authoritative."
        )
        payload = {
            context_source: item,
            "previous_action": previous_action_row or {},
            "authority_contract": SIGNAL_REFRESH_AUTHORITY_CONTRACT,
            "source_table": MACRO_CONTEXT_OVERLAYS_TABLE if context_source == "macro_context" else THEME_CONTEXT_OVERLAYS_TABLE,
            "context_reliability_classification": reliability,
            "context_class_reliability_classification": class_reliability,
            "context_sector_reliability_classification": _text(item.get("context_sector_reliability_classification")),
            "context_reliability_effect": reliability_effect,
            "source_contract": source_contract,
        }
        refresh_id = make_refresh_id(
            refreshed_at=refreshed_at,
            symbol=symbol,
            unique_id=overlay_id,
            reason=f"{context_source}_negative:{exposure_source}",
        )
        signal = {
            "symbol": symbol,
            "signal_action": "REDUCE_EXPOSURE_REVIEW",
            "signal_status": "exit_or_reduce",
            "signal_source": f"{context_source}_negative",
            "confidence": pressure,
            "action_reason": action_reason,
        }
        effect = {
            "effect_type": f"{context_source}_derisk_pressure",
            "effect_summary": f"Negative sector/{context_source} context matched an existing watched or portfolio symbol; fast refresh stayed review-only.",
            "previous_action": previous_action,
        }
        action_changed = bool(previous_action and signal["signal_action"] != previous_action)
        trace_id = None
        if not dry_run:
            trace_id = safe_trace_call(
                append_trace,
                asof_date=effective_asof,
                symbol=symbol,
                unique_id=overlay_id,
                trigger_type=f"signal_refresh:{context_source}_negative",
                previous_action=previous_action,
                new_action=signal["signal_action"],
                final_action=signal["signal_action"],
                final_reason=action_reason,
                source_table=TABLE_NAME,
                source_key=refresh_id,
                payload={"signal": signal, "context": payload},
            )
            if trace_id:
                safe_trace_call(
                    append_trace_step,
                    trace_id=trace_id,
                    step_idx=1,
                    stage=f"{context_source}_negative",
                    status=signal["signal_status"],
                    reason=action_reason,
                    input_payload={"symbol": symbol, "overlay_id": overlay_id, "exposure_source": exposure_source},
                    output_payload={**signal, "previous_action": previous_action, "action_changed": action_changed},
                    payload={
                        "source_tables": [
                            MACRO_CONTEXT_OVERLAYS_TABLE if context_source == "macro_context" else THEME_CONTEXT_OVERLAYS_TABLE,
                            MARKET_CONTEXT_UNIVERSE_TABLE,
                            WATCHLIST_TABLE,
                            PORTFOLIO_TABLE,
                        ]
                    },
                )
        rows.append(
            {
                "refresh_id": refresh_id,
                "refreshed_at": refreshed_at,
                "asof_date": effective_asof,
                "symbol": symbol,
                "unique_id": overlay_id,
                "reason": f"{context_source}_negative:{exposure_source}",
                **signal,
                **effect,
                "action_changed": action_changed,
                **SIGNAL_REFRESH_AUTHORITY_CONTRACT,
                "action_payload_json": json_dumps(payload),
                "trace_id": trace_id,
                "dry_run": bool(dry_run),
                "load_ts": pd.Timestamp.utcnow(),
            }
        )
    return rows


def build_positive_context_watch_signal_rows(targets: pd.DataFrame, *, dry_run: bool = False) -> list[dict[str, Any]]:
    if targets.empty:
        return []
    rows: list[dict[str, Any]] = []
    refreshed_at = pd.Timestamp.utcnow()
    for item in targets.to_dict(orient="records"):
        symbol = str(item.get("symbol") or "").strip().upper()
        overlay_id = _text(item.get("context_overlay_id")) or _text(item.get("unique_id"))
        context_source = _text(item.get("context_source")) or _text(item.get("watch_source")) or "context_overlay"
        if not symbol or not overlay_id:
            continue
        effective_asof = _date(item.get("asof_date") or item.get("context_asof_date"))
        previous_action_row = load_latest_action(symbol, asof_date=effective_asof)
        previous_action = _normalized_action((previous_action_row or {}).get("action_code"))
        score = _num(item.get("setup_score"))
        reason_detail = _text(item.get("watch_reason_detail")) or "Positive/watch context-overlay pressure added the stock to the discovery queue."
        reliability = _text(item.get("context_reliability_classification"))
        class_reliability = _text(item.get("context_class_reliability_classification"))
        candidate_state = _text(item.get("candidate_state") or item.get("current_state")) or "WATCH_EVENT"
        context_policy_effect = _text(item.get("context_policy_effect")) or "watch_only_no_buy_authority"
        watch_reason_audit = _compact_watch_reason_audit(item)
        signal_status = "watch_breakout_context" if candidate_state == "WATCH_BREAKOUT" else "watch_context"
        source_contract = context_overlay_source_contract(
            context_source=context_source,
            overlay_id=overlay_id,
            symbol=symbol,
            event_class=item.get("context_class") or item.get("candidate_state") or item.get("current_state"),
            direction=item.get("direction") or "watch",
            asof_date=item.get("asof_date") or item.get("context_asof_date"),
        )
        action_reason = (
            f"{context_source} watch pressure for {symbol}. {reason_detail} "
            "This is discovery/watch-input only; technical confirmation and full advisory are required before any buy or portfolio action."
        )
        payload = {
            "context_watch": item,
            "previous_action": previous_action_row or {},
            "authority_contract": SIGNAL_REFRESH_AUTHORITY_CONTRACT,
            "source_context": context_source,
            "context_reliability_classification": reliability,
            "context_class_reliability_classification": class_reliability,
            "context_candidate_state": candidate_state,
            "context_policy_effect": context_policy_effect,
            "watch_reason_audit": watch_reason_audit,
            "source_contract": source_contract,
        }
        refresh_id = make_refresh_id(
            refreshed_at=refreshed_at,
            symbol=symbol,
            unique_id=overlay_id,
            reason=f"{context_source}_watch",
        )
        signal = {
            "symbol": symbol,
            "signal_action": "WATCH",
            "signal_status": signal_status,
            "signal_source": f"{context_source}_watch",
            "confidence": score,
            "action_reason": action_reason,
        }
        effect = {
            "effect_type": "context_overlay_watch_pressure",
            "effect_summary": (
                f"Positive/watch context overlay created a fast {candidate_state} discovery row only; "
                "buy, portfolio, and broker authority remain blocked until full advisory."
            ),
            "previous_action": previous_action,
        }
        action_changed = bool(previous_action and signal["signal_action"] != previous_action)
        trace_id = None
        if not dry_run:
            trace_id = safe_trace_call(
                append_trace,
                asof_date=effective_asof,
                symbol=symbol,
                unique_id=overlay_id,
                trigger_type=f"signal_refresh:{context_source}_watch",
                previous_action=previous_action,
                new_action=signal["signal_action"],
                final_action=signal["signal_action"],
                final_reason=action_reason,
                source_table=TABLE_NAME,
                source_key=refresh_id,
                payload={"signal": signal, "context": payload},
            )
            if trace_id:
                safe_trace_call(
                    append_trace_step,
                    trace_id=trace_id,
                    step_idx=1,
                    stage=f"{context_source}_watch",
                    status=signal["signal_status"],
                    reason=action_reason,
                    input_payload={
                        "symbol": symbol,
                        "overlay_id": overlay_id,
                        "context_source": context_source,
                        "candidate_state": candidate_state,
                        "context_policy_effect": context_policy_effect,
                        **watch_reason_audit,
                    },
                    output_payload={**signal, "previous_action": previous_action, "action_changed": action_changed},
                    payload={"source_tables": ["advisory_context_overlays", WATCHLIST_TABLE]},
                )
        rows.append(
            {
                "refresh_id": refresh_id,
                "refreshed_at": refreshed_at,
                "asof_date": effective_asof,
                "symbol": symbol,
                "unique_id": overlay_id,
                "reason": f"{context_source}_watch",
                **signal,
                **effect,
                "action_changed": action_changed,
                **SIGNAL_REFRESH_AUTHORITY_CONTRACT,
                "action_payload_json": json_dumps(payload),
                "trace_id": trace_id,
                "dry_run": bool(dry_run),
                "load_ts": pd.Timestamp.utcnow(),
            }
        )
    return rows


def build_direct_context_signal_rows(targets: pd.DataFrame, *, dry_run: bool = False) -> list[dict[str, Any]]:
    if targets.empty:
        return []
    rows: list[dict[str, Any]] = []
    refreshed_at = pd.Timestamp.utcnow()
    for item in targets.to_dict(orient="records"):
        symbol = str(item.get("symbol") or "").strip().upper()
        overlay_id = _text(item.get("overlay_id"))
        context_source = _text(item.get("context_source")) or "direct_context"
        if not symbol or not overlay_id:
            continue
        effective_asof = _date(item.get("overlay_asof_date"))
        previous_action_row = load_latest_action(symbol, asof_date=effective_asof)
        previous_action = _normalized_action((previous_action_row or {}).get("action_code"))
        pressure = _num(item.get("pressure_score"))
        context_class = _text(item.get("context_class")) or "negative_context"
        exposure_source = _text(item.get("exposure_source")) or "watchlist"
        context_reason = _text(item.get("context_reason")) or "negative context pressure"
        reliability = _text(item.get("context_reliability_classification"))
        class_reliability = _text(item.get("context_class_reliability_classification"))
        reliability_effect = _text(item.get("context_reliability_effect")) or "allowed_review_only_derisk"
        source_contract = context_overlay_source_contract(
            context_source=context_source,
            overlay_id=overlay_id,
            symbol=symbol,
            event_class=context_class,
            direction=item.get("direction") or "negative",
            asof_date=item.get("overlay_asof_date"),
        )
        action_reason = (
            f"Negative {context_source} pressure for {symbol}: {context_class}. "
            f"{context_reason}. {symbol} is already in {exposure_source}; review thesis freshness, watchlist membership, and exit/reduce conditions. "
            "This is review-input only; full advisory remains authoritative."
        )
        payload = {
            "direct_context": item,
            "previous_action": previous_action_row or {},
            "authority_contract": SIGNAL_REFRESH_AUTHORITY_CONTRACT,
            "source_context": context_source,
            "context_reliability_classification": reliability,
            "context_class_reliability_classification": class_reliability,
            "context_sector_reliability_classification": _text(item.get("context_sector_reliability_classification")),
            "context_reliability_effect": reliability_effect,
            "source_contract": source_contract,
        }
        refresh_id = make_refresh_id(
            refreshed_at=refreshed_at,
            symbol=symbol,
            unique_id=overlay_id,
            reason=f"{context_source}_negative:{exposure_source}",
        )
        signal = {
            "symbol": symbol,
            "signal_action": "REDUCE_EXPOSURE_REVIEW",
            "signal_status": "exit_or_reduce",
            "signal_source": f"{context_source}_negative",
            "confidence": pressure,
            "action_reason": action_reason,
        }
        effect = {
            "effect_type": "direct_context_derisk_pressure",
            "effect_summary": "Negative symbol-level context matched an existing watched or portfolio symbol; fast refresh stayed review-only.",
            "previous_action": previous_action,
        }
        action_changed = bool(previous_action and signal["signal_action"] != previous_action)
        trace_id = None
        if not dry_run:
            trace_id = safe_trace_call(
                append_trace,
                asof_date=effective_asof,
                symbol=symbol,
                unique_id=overlay_id,
                trigger_type=f"signal_refresh:{context_source}_negative",
                previous_action=previous_action,
                new_action=signal["signal_action"],
                final_action=signal["signal_action"],
                final_reason=action_reason,
                source_table=TABLE_NAME,
                source_key=refresh_id,
                payload={"signal": signal, "context": payload},
            )
            if trace_id:
                safe_trace_call(
                    append_trace_step,
                    trace_id=trace_id,
                    step_idx=1,
                    stage=f"{context_source}_negative",
                    status=signal["signal_status"],
                    reason=action_reason,
                    input_payload={"symbol": symbol, "overlay_id": overlay_id, "exposure_source": exposure_source},
                    output_payload={**signal, "previous_action": previous_action, "action_changed": action_changed},
                    payload={"source_tables": [WATCHLIST_TABLE, PORTFOLIO_TABLE]},
                )
        rows.append(
            {
                "refresh_id": refresh_id,
                "refreshed_at": refreshed_at,
                "asof_date": effective_asof,
                "symbol": symbol,
                "unique_id": overlay_id,
                "reason": f"{context_source}_negative:{exposure_source}",
                **signal,
                **effect,
                "action_changed": action_changed,
                **SIGNAL_REFRESH_AUTHORITY_CONTRACT,
                "action_payload_json": json_dumps(payload),
                "trace_id": trace_id,
                "dry_run": bool(dry_run),
                "load_ts": pd.Timestamp.utcnow(),
            }
        )
    return rows


def inspect_context_overlay_refresh_current_state(
    *,
    asof_date: Any = None,
    signal_rows: int = 0,
    target_rows: int = 0,
) -> dict[str, Any]:
    expected_asof = pd.to_datetime(asof_date, utc=True, errors="coerce")
    expected_asof = None if pd.isna(expected_asof) else expected_asof.normalize()
    expected_asof_iso = None if expected_asof is None else expected_asof.isoformat()
    try:
        row = load_sync_state(CONTEXT_OVERLAY_STATE_SOURCE_NAME) or {}
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.signal_refresh",
            fallback_type="context_overlay_refresh_current_state_load_failed",
            source=CONTEXT_OVERLAY_STATE_SOURCE_NAME,
            severity="warn",
            reason="Could not inspect context-overlay signal-refresh sync state before skip-if-current guard.",
            error=exc,
            metadata={"asof_date": expected_asof_iso, "signal_rows": int(signal_rows), "target_rows": int(target_rows)},
        )
        return {
            "status": "sync_state_load_failed",
            "current_for_preview": False,
            "expected_asof_date": expected_asof_iso,
            "signal_refresh_recommended": True,
            "error_type": exc.__class__.__name__,
        }
    if not row:
        return {
            "status": "missing_sync_state",
            "current_for_preview": False,
            "expected_asof_date": expected_asof_iso,
            "state_asof_date": None,
            "signal_refresh_recommended": int(signal_rows) > 0,
        }
    state = row.get("state") if isinstance(row.get("state"), dict) else {}
    diagnostics = state.get("diagnostics") if isinstance(state.get("diagnostics"), dict) else {}
    state_version = str(state.get("context_overlay_signal_refresh_version") or "").strip()
    state_asof = pd.to_datetime(state.get("asof_date") or diagnostics.get("asof_date"), utc=True, errors="coerce")
    state_asof = None if pd.isna(state_asof) else state_asof.normalize()
    state_signal_rows = int(state.get("signal_rows") or 0)
    state_target_rows = int(state.get("target_rows") or 0)
    authority_contract = state.get("authority_contract") if isinstance(state.get("authority_contract"), dict) else {}
    broker_allowed = _boolish(
        state.get("broker_execution_allowed")
        if "broker_execution_allowed" in state
        else authority_contract.get("broker_execution_allowed")
    )
    portfolio_authority = str(state.get("portfolio_authority") or authority_contract.get("portfolio_authority") or "").strip().lower()
    status_text = str(row.get("status") or "").strip().lower()
    asof_matches = expected_asof is None or (state_asof is not None and state_asof == expected_asof)
    row_counts_cover_preview = state_signal_rows >= int(signal_rows) and state_target_rows >= int(target_rows)
    authority_safe = not broker_allowed and portfolio_authority in {"", "none"}
    version_matches = state_version == CONTEXT_OVERLAY_SIGNAL_REFRESH_VERSION
    current = bool(status_text == "ok" and asof_matches and row_counts_cover_preview and authority_safe and version_matches)
    return {
        "status": "current" if current else "stale_or_mismatched",
        "source_name": CONTEXT_OVERLAY_STATE_SOURCE_NAME,
        "expected_asof_date": expected_asof_iso,
        "state_asof_date": None if state_asof is None else state_asof.isoformat(),
        "asof_matches": bool(asof_matches),
        "state_status": status_text,
        "state_signal_rows": state_signal_rows,
        "state_target_rows": state_target_rows,
        "preview_signal_rows": int(signal_rows),
        "preview_target_rows": int(target_rows),
        "row_counts_cover_preview": bool(row_counts_cover_preview),
        "authority_safe": bool(authority_safe),
        "version_matches": bool(version_matches),
        "state_context_overlay_signal_refresh_version": state_version or None,
        "context_overlay_signal_refresh_version": CONTEXT_OVERLAY_SIGNAL_REFRESH_VERSION,
        "current_for_preview": bool(current),
        "signal_refresh_recommended": bool(int(signal_rows) > 0 and not current),
    }


def _diagnostic_row_count(diagnostics: dict[str, Any], key: str) -> int | None:
    value = diagnostics.get(key)
    numeric = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(numeric) else int(numeric)


def _nested_row_count(diagnostics: dict[str, Any], section: str) -> int | None:
    nested = diagnostics.get(section) if isinstance(diagnostics.get(section), dict) else {}
    return _diagnostic_row_count(nested, "row_count")


def inspect_context_overlay_refresh_precheck_state(
    *,
    asof_date: Any = None,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    expected_asof = pd.to_datetime(asof_date or diagnostics.get("asof_date"), utc=True, errors="coerce")
    expected_asof = None if pd.isna(expected_asof) else expected_asof.normalize()
    expected_asof_iso = None if expected_asof is None else expected_asof.isoformat()
    try:
        row = load_sync_state(CONTEXT_OVERLAY_STATE_SOURCE_NAME) or {}
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.signal_refresh",
            fallback_type="context_overlay_refresh_precheck_state_load_failed",
            source=CONTEXT_OVERLAY_STATE_SOURCE_NAME,
            severity="warn",
            reason="Could not inspect context-overlay signal-refresh sync state before cheap skip precheck.",
            error=exc,
            metadata={"asof_date": expected_asof_iso},
        )
        return {
            "status": "sync_state_load_failed",
            "current_for_precheck": False,
            "expected_asof_date": expected_asof_iso,
            "error_type": exc.__class__.__name__,
        }
    if not row:
        return {
            "status": "missing_sync_state",
            "current_for_precheck": False,
            "expected_asof_date": expected_asof_iso,
        }
    state = row.get("state") if isinstance(row.get("state"), dict) else {}
    prior_diagnostics = state.get("diagnostics") if isinstance(state.get("diagnostics"), dict) else {}
    state_version = str(state.get("context_overlay_signal_refresh_version") or "").strip()
    state_asof = pd.to_datetime(state.get("asof_date") or prior_diagnostics.get("asof_date"), utc=True, errors="coerce")
    state_asof = None if pd.isna(state_asof) else state_asof.normalize()
    authority_contract = state.get("authority_contract") if isinstance(state.get("authority_contract"), dict) else {}
    broker_allowed = _boolish(
        state.get("broker_execution_allowed")
        if "broker_execution_allowed" in state
        else authority_contract.get("broker_execution_allowed")
    )
    portfolio_authority = str(state.get("portfolio_authority") or authority_contract.get("portfolio_authority") or "").strip().lower()
    status_text = str(row.get("status") or "").strip().lower()
    asof_matches = expected_asof is None or (state_asof is not None and state_asof == expected_asof)
    authority_safe = not broker_allowed and portfolio_authority in {"", "none"}
    compared_fields = {
        "source_rows_total": (
            _diagnostic_row_count(prior_diagnostics, "source_rows_total"),
            _diagnostic_row_count(diagnostics, "source_rows_total"),
        ),
        "active_authorized_rows_total": (
            _diagnostic_row_count(prior_diagnostics, "active_authorized_rows_total"),
            _diagnostic_row_count(diagnostics, "active_authorized_rows_total"),
        ),
        "watchlist_row_count": (
            _nested_row_count(prior_diagnostics, "watchlist"),
            _nested_row_count(diagnostics, "watchlist"),
        ),
        "portfolio_row_count": (
            _nested_row_count(prior_diagnostics, "portfolio"),
            _nested_row_count(diagnostics, "portfolio"),
        ),
    }
    diagnostics_comparable = all(old is not None and new is not None for old, new in compared_fields.values())
    diagnostics_match = diagnostics_comparable and all(old == new for old, new in compared_fields.values())
    version_matches = state_version == CONTEXT_OVERLAY_SIGNAL_REFRESH_VERSION
    current = bool(status_text == "ok" and asof_matches and authority_safe and diagnostics_match and version_matches)
    return {
        "status": "current" if current else "stale_or_mismatched",
        "source_name": CONTEXT_OVERLAY_STATE_SOURCE_NAME,
        "expected_asof_date": expected_asof_iso,
        "state_asof_date": None if state_asof is None else state_asof.isoformat(),
        "state_status": status_text,
        "asof_matches": bool(asof_matches),
        "authority_safe": bool(authority_safe),
        "diagnostics_comparable": bool(diagnostics_comparable),
        "diagnostics_match": bool(diagnostics_match),
        "version_matches": bool(version_matches),
        "state_context_overlay_signal_refresh_version": state_version or None,
        "context_overlay_signal_refresh_version": CONTEXT_OVERLAY_SIGNAL_REFRESH_VERSION,
        "compared_fields": compared_fields,
        "state_signal_rows": int(state.get("signal_rows") or 0),
        "state_target_rows": int(state.get("target_rows") or 0),
        "current_for_precheck": bool(current),
    }


def _build_context_overlay_refresh_result(
    *,
    asof_date: Any = None,
    limit: int = 50,
    dry_run: bool = False,
    persist: bool = True,
    skip_if_current: bool = False,
) -> dict[str, Any]:
    if persist:
        ensure_table()
    diagnostics = inspect_context_overlay_signal_inputs(asof_date=asof_date)
    precheck_state = inspect_context_overlay_refresh_precheck_state(
        asof_date=asof_date,
        diagnostics=diagnostics,
    ) if skip_if_current and persist and not dry_run else None
    if precheck_state and precheck_state.get("current_for_precheck"):
        return {
            "status": "skipped_current",
            "context_overlay_signal_refresh_version": CONTEXT_OVERLAY_SIGNAL_REFRESH_VERSION,
            "target_rows": int(precheck_state.get("state_target_rows") or 0),
            "signal_rows": int(precheck_state.get("state_signal_rows") or 0),
            "affected_symbols": [],
            "affected_symbol_count": 0,
            "empty_reason": None,
            "diagnostics": diagnostics,
            "conversion_diagnostics": {},
            "rows": [],
            "dry_run": bool(dry_run),
            "preview_only": False,
            "skip_if_current": True,
            "current_state": precheck_state,
            "operator_action": "Context-overlay signal refresh is already current for this as-of date; skipped duplicate target loading and persistence.",
            "authority_contract": SIGNAL_REFRESH_AUTHORITY_CONTRACT,
            **SIGNAL_REFRESH_AUTHORITY_CONTRACT,
        }
    reliability = load_context_family_reliability(asof_date=_date(asof_date))
    theme_targets = load_negative_theme_context_exposure_targets(asof_date=asof_date, limit=limit)
    macro_targets = load_negative_macro_context_exposure_targets(asof_date=asof_date, limit=limit)
    direct_targets = load_negative_direct_context_exposure_targets(asof_date=asof_date, limit=limit)
    raw_theme_target_rows = int(len(theme_targets))
    raw_macro_target_rows = int(len(macro_targets))
    raw_direct_target_rows = int(len(direct_targets))
    raw_theme_targets = theme_targets.copy()
    raw_macro_targets = macro_targets.copy()
    raw_direct_targets = direct_targets.copy()
    theme_targets, theme_suppressed = _filter_negative_context_targets_by_reliability(
        theme_targets,
        reliability=reliability,
        default_family="theme_context",
    )
    macro_targets, macro_suppressed = _filter_negative_context_targets_by_reliability(
        macro_targets,
        reliability=reliability,
        default_family="macro_context",
    )
    direct_targets, direct_suppressed = _filter_negative_context_targets_by_reliability(
        direct_targets,
        reliability=reliability,
        default_family=None,
    )
    reliability_suppressed = [*theme_suppressed, *macro_suppressed, *direct_suppressed]
    raw_positive_watch_targets, positive_watch_targets, positive_watch_suppressed = load_positive_context_overlay_watch_target_frames(
        asof_date=asof_date,
        limit=limit,
    )
    causal_memory_targets, causal_memory_diagnostics = load_causal_memory_signal_targets(asof_date=asof_date, limit=limit)
    conversion_diagnostics = summarize_context_overlay_conversion(
        diagnostics=diagnostics,
        raw_theme_targets=_concat_nonempty([raw_theme_targets, raw_macro_targets]),
        raw_direct_targets=raw_direct_targets,
        raw_positive_watch_targets=raw_positive_watch_targets,
        kept_theme_targets=_concat_nonempty([theme_targets, macro_targets]),
        kept_direct_targets=direct_targets,
        kept_positive_watch_targets=positive_watch_targets,
        reliability_suppressed=reliability_suppressed,
        policy_suppressed=positive_watch_suppressed,
    )
    rows = [
        *build_theme_context_signal_rows(theme_targets, dry_run=bool(dry_run)),
        *build_theme_context_signal_rows(macro_targets, dry_run=bool(dry_run)),
        *build_direct_context_signal_rows(direct_targets, dry_run=bool(dry_run)),
        *build_positive_context_watch_signal_rows(positive_watch_targets, dry_run=bool(dry_run)),
        *build_causal_memory_signal_rows(causal_memory_targets, dry_run=bool(dry_run)),
    ]
    raw_signal_rows = int(len(rows))
    rows = dedupe_signal_rows(rows)
    deduped_signal_rows = raw_signal_rows - int(len(rows))
    affected_symbols = sorted(
        {
            str(row.get("symbol") or "").strip().upper()
            for row in rows
            if str(row.get("symbol") or "").strip()
        }
    )
    target_rows = int(len(theme_targets) + len(macro_targets) + len(direct_targets) + len(positive_watch_targets) + len(causal_memory_targets))
    empty_reason = None if target_rows > 0 else diagnostics.get("empty_reason") or "context_rows_available_but_no_signal_targets_matched"
    current_state = inspect_context_overlay_refresh_current_state(
        asof_date=asof_date,
        signal_rows=len(rows),
        target_rows=target_rows,
    ) if skip_if_current and persist and not dry_run else None
    if current_state and current_state.get("current_for_preview"):
        return {
            "status": "skipped_current",
            "context_overlay_signal_refresh_version": CONTEXT_OVERLAY_SIGNAL_REFRESH_VERSION,
            "target_rows": target_rows,
            "signal_rows": int(len(rows)),
            "affected_symbols": affected_symbols,
            "affected_symbol_count": int(len(affected_symbols)),
            "empty_reason": empty_reason,
            "diagnostics": diagnostics,
            "conversion_diagnostics": conversion_diagnostics,
            "rows": [],
            "dry_run": bool(dry_run),
            "preview_only": False,
            "skip_if_current": True,
            "current_state": current_state,
            "operator_action": "Context-overlay signal refresh is already current for this as-of date; skipped duplicate persistence.",
            "authority_contract": SIGNAL_REFRESH_AUTHORITY_CONTRACT,
            **SIGNAL_REFRESH_AUTHORITY_CONTRACT,
        }
    if persist and not dry_run:
        persist_signal_rows(rows)
        now = pd.Timestamp.utcnow()
        persist_sync_state(
            source_name=THEME_CONTEXT_STATE_SOURCE_NAME,
            last_success_at=now,
            last_item_ts=now,
            cursor_value=now.isoformat(),
            state={
                "context_overlay_signal_refresh_version": CONTEXT_OVERLAY_SIGNAL_REFRESH_VERSION,
                "target_rows": target_rows,
                "raw_theme_target_rows": raw_theme_target_rows,
                "raw_macro_context_target_rows": raw_macro_target_rows,
                "raw_direct_context_target_rows": raw_direct_target_rows,
                "theme_target_rows": int(len(theme_targets)),
                "macro_context_target_rows": int(len(macro_targets)),
                "direct_context_target_rows": int(len(direct_targets)),
                "positive_watch_target_rows": int(len(positive_watch_targets)),
                "raw_positive_watch_target_rows": int(len(raw_positive_watch_targets)),
                "positive_watch_policy_suppressed_target_rows": int(len(positive_watch_suppressed)),
                "positive_watch_policy_suppressed_samples": positive_watch_suppressed[:10],
                "causal_memory_target_rows": int(len(causal_memory_targets)),
                "causal_memory_diagnostics": causal_memory_diagnostics,
                "reliability_suppressed_target_rows": int(len(reliability_suppressed)),
                "context_reliability_evidence_source": reliability.get("evidence_source") if isinstance(reliability, dict) else None,
                "raw_signal_rows": raw_signal_rows,
                "deduped_signal_rows": deduped_signal_rows,
                "signal_rows": int(len(rows)),
                "affected_symbols": affected_symbols,
                "affected_symbol_count": int(len(affected_symbols)),
                "empty_reason": empty_reason,
                "authority_contract": SIGNAL_REFRESH_AUTHORITY_CONTRACT,
                "authority_scope": SIGNAL_REFRESH_AUTHORITY_CONTRACT["authority_scope"],
                "portfolio_authority": SIGNAL_REFRESH_AUTHORITY_CONTRACT["portfolio_authority"],
                "broker_execution_allowed": False,
                "full_advisory_required": SIGNAL_REFRESH_AUTHORITY_CONTRACT["full_advisory_required"],
                "diagnostics": diagnostics,
                "conversion_diagnostics": conversion_diagnostics,
            },
            status="ok",
        )
        publish_bus_message(
            "stockey:signal_refresh:context_overlays",
            {
                "published_at": now,
                "context_overlay_signal_refresh_version": CONTEXT_OVERLAY_SIGNAL_REFRESH_VERSION,
                "target_rows": target_rows,
                "raw_theme_target_rows": raw_theme_target_rows,
                "raw_macro_context_target_rows": raw_macro_target_rows,
                "raw_direct_context_target_rows": raw_direct_target_rows,
                "theme_target_rows": len(theme_targets),
                "macro_context_target_rows": len(macro_targets),
                "direct_context_target_rows": len(direct_targets),
                "positive_watch_target_rows": len(positive_watch_targets),
                "raw_positive_watch_target_rows": len(raw_positive_watch_targets),
                "positive_watch_policy_suppressed_target_rows": len(positive_watch_suppressed),
                "positive_watch_policy_suppressed_samples": positive_watch_suppressed[:10],
                "causal_memory_target_rows": len(causal_memory_targets),
                "causal_memory_diagnostics": causal_memory_diagnostics,
                "reliability_suppressed_target_rows": len(reliability_suppressed),
                "reliability_suppressed_samples": reliability_suppressed[:10],
                "context_reliability_evidence_source": reliability.get("evidence_source") if isinstance(reliability, dict) else None,
                "raw_signal_rows": raw_signal_rows,
                "deduped_signal_rows": deduped_signal_rows,
                "signal_rows": len(rows),
                "affected_symbols": affected_symbols,
                "affected_symbol_count": len(affected_symbols),
                "empty_reason": empty_reason,
                "authority_contract": SIGNAL_REFRESH_AUTHORITY_CONTRACT,
                "authority_scope": SIGNAL_REFRESH_AUTHORITY_CONTRACT["authority_scope"],
                "portfolio_authority": SIGNAL_REFRESH_AUTHORITY_CONTRACT["portfolio_authority"],
                "broker_execution_allowed": False,
                "full_advisory_required": SIGNAL_REFRESH_AUTHORITY_CONTRACT["full_advisory_required"],
                "diagnostics": diagnostics,
                "conversion_diagnostics": conversion_diagnostics,
                "sample": rows[:10],
            },
        )
    return {
        "status": "ok",
        "context_overlay_signal_refresh_version": CONTEXT_OVERLAY_SIGNAL_REFRESH_VERSION,
        "target_rows": target_rows,
        "raw_theme_target_rows": raw_theme_target_rows,
        "raw_macro_context_target_rows": raw_macro_target_rows,
        "raw_direct_context_target_rows": raw_direct_target_rows,
        "theme_target_rows": int(len(theme_targets)),
        "macro_context_target_rows": int(len(macro_targets)),
        "direct_context_target_rows": int(len(direct_targets)),
        "positive_watch_target_rows": int(len(positive_watch_targets)),
        "raw_positive_watch_target_rows": int(len(raw_positive_watch_targets)),
        "positive_watch_policy_suppressed_target_rows": int(len(positive_watch_suppressed)),
        "positive_watch_policy_suppressed_samples": positive_watch_suppressed[:10],
        "causal_memory_target_rows": int(len(causal_memory_targets)),
        "causal_memory_diagnostics": causal_memory_diagnostics,
        "reliability_suppressed_target_rows": int(len(reliability_suppressed)),
        "reliability_suppressed_samples": reliability_suppressed[:10],
        "context_reliability_evidence_source": reliability.get("evidence_source") if isinstance(reliability, dict) else None,
        "raw_signal_rows": raw_signal_rows,
        "deduped_signal_rows": deduped_signal_rows,
        "signal_rows": int(len(rows)),
        "affected_symbols": affected_symbols,
        "affected_symbol_count": int(len(affected_symbols)),
        "empty_reason": empty_reason,
        "diagnostics": diagnostics,
        "conversion_diagnostics": conversion_diagnostics,
        "rows": rows[:50],
        "dry_run": bool(dry_run),
        "preview_only": not bool(persist),
        "authority_contract": SIGNAL_REFRESH_AUTHORITY_CONTRACT,
        **SIGNAL_REFRESH_AUTHORITY_CONTRACT,
    }


def preview_context_overlay_signal_refresh(*, asof_date: Any = None, limit: int = 50) -> dict[str, Any]:
    """Build a read-only context-overlay refresh preview for operator diagnostics.

    Unlike ``refresh_from_context_overlays(..., dry_run=True)``, this helper does
    not create/migrate the signal-refresh table, persist sync state, or publish
    Redis events. It exists so recommendation diagnostics can show whether a
    bounded context-overlay refresh would currently produce review-only Action
    Queue signals without changing runtime state.
    """

    return _build_context_overlay_refresh_result(
        asof_date=asof_date,
        limit=limit,
        dry_run=True,
        persist=False,
    )


def refresh_from_context_overlays(
    *,
    asof_date: Any = None,
    limit: int = 50,
    dry_run: bool = False,
    skip_if_current: bool = False,
) -> dict[str, Any]:
    return _build_context_overlay_refresh_result(
        asof_date=asof_date,
        limit=limit,
        dry_run=dry_run,
        persist=True,
        skip_if_current=bool(skip_if_current),
    )


def refresh_from_theme_context(*, asof_date: Any = None, limit: int = 50, dry_run: bool = False) -> dict[str, Any]:
    """Backward-compatible alias; includes direct context overlays too."""
    return refresh_from_context_overlays(asof_date=asof_date, limit=limit, dry_run=dry_run)


def refresh_from_causal_memory(*, asof_date: Any = None, limit: int = 50, dry_run: bool = False) -> dict[str, Any]:
    if not dry_run:
        ensure_table()
    targets, diagnostics = load_causal_memory_signal_targets(asof_date=asof_date, limit=limit)
    rows = build_causal_memory_signal_rows(targets, dry_run=bool(dry_run))
    affected_symbols = sorted({str(row.get("symbol") or "").strip().upper() for row in rows if str(row.get("symbol") or "").strip()})
    if not dry_run:
        persist_signal_rows(rows)
        now = pd.Timestamp.utcnow()
        persist_sync_state(
            source_name=CAUSAL_MEMORY_STATE_SOURCE_NAME,
            last_success_at=now,
            last_item_ts=now,
            cursor_value=now.isoformat(),
            state={
                "target_rows": int(len(targets)),
                "signal_rows": int(len(rows)),
                "affected_symbols": affected_symbols,
                "affected_symbol_count": int(len(affected_symbols)),
                "diagnostics": diagnostics,
                "authority_scope": SIGNAL_REFRESH_AUTHORITY_CONTRACT["authority_scope"],
                "broker_execution_allowed": False,
                "portfolio_authority": SIGNAL_REFRESH_AUTHORITY_CONTRACT["portfolio_authority"],
                "full_advisory_required": SIGNAL_REFRESH_AUTHORITY_CONTRACT["full_advisory_required"],
            },
            status="ok",
        )
        publish_bus_message(
            "stockey:signal_refresh:causal_memory",
            {
                "published_at": now,
                "target_rows": int(len(targets)),
                "signal_rows": int(len(rows)),
                "affected_symbols": affected_symbols,
                "affected_symbol_count": int(len(affected_symbols)),
                "diagnostics": diagnostics,
                "sample": rows[:10],
            },
        )
    return {
        "status": "ok",
        "target_rows": int(len(targets)),
        "signal_rows": int(len(rows)),
        "affected_symbols": affected_symbols,
        "affected_symbol_count": int(len(affected_symbols)),
        "diagnostics": diagnostics,
        "rows": rows[:50],
        "dry_run": bool(dry_run),
        **SIGNAL_REFRESH_AUTHORITY_CONTRACT,
    }


def refresh_symbol(
    *,
    symbol: str,
    reason: str = "manual",
    unique_id: str | None = None,
    asof_date: Any = None,
    dry_run: bool = False,
    refresh_trace_summary: bool = True,
    router_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_table()
    normalized_symbol = str(symbol).strip().upper()
    if not normalized_symbol:
        raise ValueError("symbol is required")
    effective_asof = _date(asof_date)
    action = load_latest_action(normalized_symbol, asof_date=effective_asof)
    lifecycle = load_latest_lifecycle(normalized_symbol, asof_date=effective_asof)
    rebalance = load_latest_rebalance(normalized_symbol, asof_date=effective_asof)
    events = load_event_policy(normalized_symbol, unique_id=unique_id, asof_date=effective_asof)
    wait_result = match_wait_signals(symbols=[normalized_symbol], limit=100, persist=not bool(dry_run))
    wait_matches = wait_result.get("matches") or []
    signal = choose_signal(
        symbol=normalized_symbol,
        reason=reason,
        action=action,
        lifecycle=lifecycle,
        rebalance=rebalance,
        events=events,
        wait_matches=wait_matches,
        router_context=router_context,
    )
    effect = classify_signal_effect(signal=signal, action=action, wait_matches=wait_matches)
    refreshed_at = pd.Timestamp.utcnow()
    refresh_id = make_refresh_id(refreshed_at=refreshed_at, symbol=normalized_symbol, unique_id=unique_id, reason=reason)
    payload = _extract_action_payload(action=action, lifecycle=lifecycle, rebalance=rebalance, events=events)
    payload["wait_signal_matches"] = wait_matches
    payload["wait_signal_match_table"] = WAIT_SIGNAL_MATCHES_TABLE
    payload["watcher_effect"] = effect
    payload["authority_contract"] = SIGNAL_REFRESH_AUTHORITY_CONTRACT
    payload["router_context"] = router_context or {}
    previous_action = effect.get("previous_action")
    action_changed = bool(previous_action and signal["signal_action"] != previous_action and signal["signal_action"] != "NO_CHANGE")
    trace_id = None
    if not dry_run:
        trace_id = safe_trace_call(
            append_trace,
            asof_date=effective_asof,
            symbol=normalized_symbol,
            unique_id=unique_id,
            trigger_type=f"signal_refresh:{reason}",
            previous_action=previous_action,
            new_action=signal["signal_action"],
            final_action=signal["signal_action"],
            final_reason=signal["action_reason"],
            source_table=TABLE_NAME,
            source_key=refresh_id,
            payload={"signal": signal, "context": payload},
        )
        if trace_id:
            safe_trace_call(
                append_trace_step,
                trace_id=trace_id,
                step_idx=1,
                stage="signal_refresh",
                status=signal["signal_status"],
                reason=signal["action_reason"],
                input_payload={"symbol": normalized_symbol, "reason": reason, "unique_id": unique_id, "asof_date": str(effective_asof)},
                output_payload={**signal, "previous_action": previous_action, "action_changed": action_changed},
                payload={"source_tables": [ACTIONS_TABLE, LIFECYCLE_TABLE, REBALANCE_TABLE, EVENT_POLICY_TABLE]},
            )

    row = {
        "refresh_id": refresh_id,
        "refreshed_at": refreshed_at,
        "asof_date": effective_asof,
        "symbol": normalized_symbol,
        "unique_id": unique_id,
        "reason": reason,
        "signal_action": signal["signal_action"],
        "signal_status": signal["signal_status"],
        "signal_source": signal["signal_source"],
        "confidence": signal["confidence"],
        "action_reason": signal["action_reason"],
        "effect_type": effect["effect_type"],
        "effect_summary": effect["effect_summary"],
        "previous_action": previous_action,
        "action_changed": action_changed,
        **SIGNAL_REFRESH_AUTHORITY_CONTRACT,
        "action_payload_json": json_dumps(payload),
        "trace_id": trace_id,
        "dry_run": bool(dry_run),
        "load_ts": pd.Timestamp.utcnow(),
    }
    if not dry_run:
        persist_signal_rows([row])
        if refresh_trace_summary:
            try:
                build_symbol_summary(normalized_symbol, persist=True)
                if unique_id:
                    build_event_summary(str(unique_id), persist=True)
            except Exception as exc:
                record_local_fallback_event(
                    module="advisory.signal_refresh",
                    fallback_type="signal_refresh_trace_summary_refresh_failed",
                    source="advisory.trace_summary_store",
                    severity="warn",
                    symbol=normalized_symbol,
                    unique_id=unique_id,
                    reason="Signal refresh row was persisted but trace-summary cache refresh failed.",
                    error=exc,
                    metadata={"refresh_id": refresh_id, "reason": reason},
                )
    return row


def refresh_from_router(*, limit: int = 25, dry_run: bool = False) -> dict[str, Any]:
    router_rows = load_recent_router_actions(limit=limit)
    output_rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for item in router_rows:
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        reason_bits = [_text(item.get("source_type")), _text(item.get("action_type"))]
        reason = "router:" + ":".join([bit for bit in reason_bits if bit])
        try:
            output_rows.append(
                refresh_symbol(
                    symbol=symbol,
                    reason=reason,
                    asof_date=_ts(item.get("asof_date")),
                    dry_run=dry_run,
                    refresh_trace_summary=False,
                    router_context={
                        "source_types": [part for part in str(item.get("source_type") or "").split(",") if part],
                        "action_type": _text(item.get("action_type")),
                        "reasons": [part for part in str(item.get("action_reason") or "").split(",") if part],
                        "setup_ids": _jsonish(item.get("setup_ids_json"), [], source="signal_refresh_router_setup_ids_json"),
                        "routed_at": _text(item.get("routed_at")),
                    },
                )
            )
        except Exception as exc:  # pragma: no cover - runtime guard
            record_local_fallback_event(
                module="advisory.signal_refresh",
                fallback_type="signal_refresh_router_item_failed",
                source=ROUTER_ACTIONS_TABLE,
                severity="warn",
                symbol=symbol or None,
                reason="Router-triggered signal refresh failed for one symbol while the batch continued.",
                error=exc,
                metadata={
                    "router_reason": reason,
                    "unique_id": _text(item.get("unique_id")),
                    "asof_date": str(item.get("asof_date")) if item.get("asof_date") is not None else None,
                },
            )
            errors.append({"symbol": symbol, "error": f"{exc.__class__.__name__}: {exc}"})
    if not dry_run:
        persist_sync_state(
            source_name=STATE_SOURCE_NAME,
            last_success_at=pd.Timestamp.utcnow(),
            last_item_ts=max([pd.to_datetime(row.get("refreshed_at"), utc=True, errors="coerce") for row in output_rows], default=pd.Timestamp.utcnow()),
            state={"router_rows": len(router_rows), "signal_rows": len(output_rows), "errors": errors[:20]},
            status="ok" if not errors else "partial",
            error_text=json_dumps(errors[:20]) if errors else None,
        )
        publish_bus_message(
            "stockey:signal_refresh",
            {"published_at": pd.Timestamp.utcnow(), "signal_rows": len(output_rows), "errors": errors[:20], "sample": output_rows[:10]},
        )
    return {
        "status": "ok" if not errors else "partial",
        "router_rows": len(router_rows),
        "signal_rows": len(output_rows),
        "errors": errors,
        "sample": output_rows[:10],
        "dry_run": bool(dry_run),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fast symbol-scoped signal refresh for watcher/router events.")
    parser.add_argument("--symbol", action="append", default=[], help="Symbol to refresh. Can be passed multiple times.")
    parser.add_argument("--unique-id", default=None, help="Optional event unique id for event-scoped refresh.")
    parser.add_argument("--reason", default="manual", help="Refresh reason, for example ohlcv, announcement, news, router.")
    parser.add_argument("--asof-date", default=None, help="Advisory date. Defaults to today UTC-normalized.")
    parser.add_argument("--from-router", action="store_true", help="Refresh latest symbols from advisory_live_router_actions.")
    parser.add_argument(
        "--from-context-overlays",
        action="store_true",
        help="Create review-only de-risk signals from active negative theme and direct symbol context overlays.",
    )
    parser.add_argument(
        "--from-theme-context",
        action="store_true",
        help="Backward-compatible alias for --from-context-overlays; also includes direct announcement/exchange/bhavcopy overlays.",
    )
    parser.add_argument(
        "--from-causal-memory",
        action="store_true",
        help="Create review-only signals from fresh candidate-helpful causal event memory rows.",
    )
    parser.add_argument("--limit", type=int, default=25, help="Router symbol limit for --from-router.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--skip-if-current",
        action="store_true",
        help="For --from-context-overlays, skip persistence when the persisted sync state already covers the preview with review-only/no-broker authority.",
    )
    parser.add_argument("--format", choices=["json", "text"], default="json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.from_router:
        result = refresh_from_router(limit=args.limit, dry_run=bool(args.dry_run))
    elif args.from_causal_memory:
        result = refresh_from_causal_memory(asof_date=args.asof_date, limit=args.limit, dry_run=bool(args.dry_run))
    elif args.from_context_overlays or args.from_theme_context:
        result = refresh_from_context_overlays(
            asof_date=args.asof_date,
            limit=args.limit,
            dry_run=bool(args.dry_run),
            skip_if_current=bool(args.skip_if_current),
        )
    else:
        symbols = [str(value).strip().upper() for value in args.symbol if str(value).strip()]
        if not symbols:
            raise SystemExit("--symbol is required unless --from-router, --from-context-overlays, or --from-causal-memory is used")
        rows = [
            refresh_symbol(
                symbol=symbol,
                reason=args.reason,
                unique_id=args.unique_id,
                asof_date=args.asof_date,
                dry_run=bool(args.dry_run),
            )
            for symbol in symbols
        ]
        result = {"status": "ok", "signal_rows": len(rows), "rows": rows, "dry_run": bool(args.dry_run)}
    if args.format == "text":
        print(f"status={result.get('status')} signal_rows={result.get('signal_rows', 0)} dry_run={result.get('dry_run')}")
        for row in result.get("rows") or result.get("sample") or []:
            print(f"{row.get('symbol')} {row.get('signal_action')} {row.get('signal_status')} source={row.get('signal_source')} reason={row.get('action_reason')}")
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
