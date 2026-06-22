from __future__ import annotations

import argparse
import json
import os

import pandas as pd

from advisory.event_evidence_store import ANNOUNCEMENT_CONTEXT_OVERLAYS_TABLE, BHAVCOPY_CONTEXT_OVERLAYS_TABLE
from advisory.exchange_context_overlays import EXCHANGE_CONTEXT_OVERLAYS_TABLE
from advisory.fallback_telemetry import record_local_fallback_event
from advisory.macro_context_overlays import MACRO_CONTEXT_OVERLAYS_TABLE, macro_sector_alias_values_sql
from advisory.market_context import UNIVERSE_TABLE as MARKET_CONTEXT_UNIVERSE_TABLE
from advisory.news_theme_engine import THEME_CONTEXT_OVERLAYS_TABLE, theme_sector_alias_values_sql
from advisory.context_overlay_reliability_report import load_persisted_reliability_report as load_persisted_context_reliability_report
from advisory.context_overlay_reliability_report import load_reliability_report as load_fast_context_reliability_report
from advisory.context_overlay_reliability_report import reliability_runtime_policy_contract
from advisory.signal_quality_family_report import SUMMARY_TABLE as SIGNAL_QUALITY_SUMMARY_TABLE
from advisory.signal_quality_family_report import VARIANT_TO_SOURCE_FAMILY, build_family_report
from advisory.identity_issues import IDENTITY_ISSUES_TABLE
from advisory.setup_registry import load_setup_registry
from advisory.sync_state import load_sync_state
from utils.company_master import COMPANY_MASTER_TABLE, map_company_master_ids
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration
from utils.sync import parse_datetime_arg


TABLE_NAME = "advisory_watchlist"
TECHNICAL_DAILY_TABLE = "advisory_technical_daily"
TECHNICAL_REFRESH_STATUS_TABLE = "advisory_technical_feature_refresh_status"
WATCHLIST_SCHEMA_MIGRATION_ID = "20260611_advisory_watchlist_base"
WATCHLIST_CONTEXT_OVERLAY_SCHEMA_MIGRATION_ID = "20260622_advisory_watchlist_context_overlay_columns"
CONTEXT_OVERLAY_SETUP_ID = "CONTEXT_OVERLAY_WATCH"
CONTEXT_OVERLAY_SETUP_NAME = "Layered Context Overlay Watch"
CONTEXT_OVERLAY_SIGNAL_REFRESH_SYNC_SOURCE = "advisory:signal_refresh:context_overlays"
CONTEXT_OVERLAY_CONFIRMATION_PLAN_KEY = "technical_confirmation_plan"
CONTEXT_OVERLAY_WATCHLIST_BUILDER_VERSION = "context_overlay_watchlist_v4_theme_sector_aliases"
WATCHLIST_CONTEXT_OVERLAY_ENABLED = os.getenv("WATCHLIST_CONTEXT_OVERLAY_ENABLED", "true").strip().lower() not in {"0", "false", "no"}
WATCHLIST_CONTEXT_OVERLAY_LOOKBACK_DAYS = int(os.getenv("WATCHLIST_CONTEXT_OVERLAY_LOOKBACK_DAYS", "14"))
WATCHLIST_CONTEXT_OVERLAY_LIMIT = int(os.getenv("WATCHLIST_CONTEXT_OVERLAY_LIMIT", "50"))
WATCHLIST_CONTEXT_OVERLAY_RELIABILITY_PRIORITY_ENABLED = os.getenv(
    "WATCHLIST_CONTEXT_OVERLAY_RELIABILITY_PRIORITY_ENABLED",
    "true",
).strip().lower() not in {"0", "false", "no"}
WATCHLIST_CONTEXT_OVERLAY_HELPFUL_SCORE_MULTIPLIER = float(os.getenv("WATCHLIST_CONTEXT_OVERLAY_HELPFUL_SCORE_MULTIPLIER", "1.25"))
WATCHLIST_CONTEXT_OVERLAY_BREAKOUT_SCORE_THRESHOLD = float(os.getenv("WATCHLIST_CONTEXT_OVERLAY_BREAKOUT_SCORE_THRESHOLD", "0.75"))
WATCHLIST_CONTEXT_OVERLAY_NEGATIVE_SUPPRESSION_ENABLED = os.getenv(
    "WATCHLIST_CONTEXT_OVERLAY_NEGATIVE_SUPPRESSION_ENABLED",
    "true",
).strip().lower() not in {"0", "false", "no"}
WATCHLIST_CONTEXT_OVERLAY_TECHNICAL_PRIORITY_ENABLED = os.getenv(
    "WATCHLIST_CONTEXT_OVERLAY_TECHNICAL_PRIORITY_ENABLED",
    "true",
).strip().lower() not in {"0", "false", "no"}
WATCHLIST_CONTEXT_OVERLAY_TECHNICAL_PRIORITY_MAX_BONUS = float(
    os.getenv("WATCHLIST_CONTEXT_OVERLAY_TECHNICAL_PRIORITY_MAX_BONUS", "0.15")
)
WATCHLIST_CONTEXT_OVERLAY_SUPPRESS_HARD_OHLCV_BLOCKERS = os.getenv(
    "WATCHLIST_CONTEXT_OVERLAY_SUPPRESS_HARD_OHLCV_BLOCKERS",
    "true",
).strip().lower() not in {"0", "false", "no"}
WATCHLIST_CONTEXT_OVERLAY_OHLCV_ISSUE_EXCLUSION_ATTEMPTS = int(
    os.getenv("WATCHLIST_CONTEXT_OVERLAY_OHLCV_ISSUE_EXCLUSION_ATTEMPTS", "2")
)
WATCHLIST_CONTEXT_OVERLAY_EXCLUDED_RELIABILITY_CLASSES = {
    value.strip()
    for value in os.getenv(
        "WATCHLIST_CONTEXT_OVERLAY_EXCLUDED_RELIABILITY_CLASSES",
        "hurts_or_no_lift,negative_after_cost,inconsistent_or_horizon_sensitive,needs_benchmark_attribution,benchmark_beta_not_overlay_alpha",
    ).split(",")
    if value.strip()
}
DEFAULT_SCORE_THRESHOLDS = {
    "pass_now": 0.68,
    "watch_breakout": 0.58,
    "watch_event": 0.48,
    "abstain": 0.40,
}
_TRANSITION_CUTOFF = 0.12
WATCHLIST_BASE_SCHEMA_COLUMNS = {
    "setup_name": "TEXT",
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
    "current_state": "TEXT",
    "watch_reason_detail": "TEXT",
    "entry_style": "TEXT",
    "attractive_price_low": "DOUBLE PRECISION",
    "attractive_price_high": "DOUBLE PRECISION",
    "invalidation_price": "DOUBLE PRECISION",
    "entry_note": "TEXT",
    "near_miss_flag": "BOOLEAN",
    "last_event_class": "TEXT",
    "last_state_transition_hint": "TEXT",
    "last_event_score_impact": "DOUBLE PRECISION",
    "watch_enabled": "BOOLEAN",
    "watch_reasons_json": "TEXT",
    "watch_status": "TEXT",
    "state_updated_at": "TIMESTAMPTZ",
    "watch_started_at": "TIMESTAMPTZ",
    "last_checked_at": "TIMESTAMPTZ",
    "last_document_published_on": "TIMESTAMPTZ",
    "load_ts": "TIMESTAMPTZ",
}
WATCHLIST_CONTEXT_OVERLAY_SCHEMA_COLUMNS = {
    "watch_source": "TEXT",
    "context_source": "TEXT",
    "context_overlay_id": "TEXT",
    "context_authority_scope": "TEXT",
    "context_policy_effect": "TEXT",
    "context_reliability_classification": "TEXT",
    "context_class_reliability_classification": "TEXT",
    "context_reliability_evaluated_at": "TIMESTAMPTZ",
}
WATCHLIST_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        asof_date TIMESTAMPTZ NOT NULL,
        setup_id TEXT NOT NULL,
        setup_name TEXT,
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
        current_state TEXT,
        watch_reason_detail TEXT,
        entry_style TEXT,
        attractive_price_low DOUBLE PRECISION,
        attractive_price_high DOUBLE PRECISION,
        invalidation_price DOUBLE PRECISION,
        entry_note TEXT,
        near_miss_flag BOOLEAN,
        last_event_class TEXT,
        last_state_transition_hint TEXT,
        last_event_score_impact DOUBLE PRECISION,
        watch_enabled BOOLEAN,
        watch_reasons_json TEXT,
        watch_status TEXT,
        state_updated_at TIMESTAMPTZ,
        watch_started_at TIMESTAMPTZ,
        last_checked_at TIMESTAMPTZ,
        last_document_published_on TIMESTAMPTZ,
        load_ts TIMESTAMPTZ,
        UNIQUE (asof_date, setup_id, symbol)
    )
    """,
    *[
        f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS {column} {sql_type}"
        for column, sql_type in WATCHLIST_BASE_SCHEMA_COLUMNS.items()
    ],
]
WATCHLIST_CONTEXT_OVERLAY_SCHEMA_STATEMENTS = [
    *[
        f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS {column} {sql_type}"
        for column, sql_type in WATCHLIST_CONTEXT_OVERLAY_SCHEMA_COLUMNS.items()
    ],
]


def _record_watchlist_builder_fallback(
    *,
    fallback_type: str,
    source: str,
    reason: str,
    error: Exception,
    metadata: dict[str, object] | None = None,
) -> None:
    record_local_fallback_event(
        module="advisory.watchlist_builder",
        fallback_type=fallback_type,
        source=source,
        severity="warn",
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def normalize_timestamp(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce").dt.normalize()


def ensure_watchlist_table() -> None:
    apply_schema_migration(
        migration_id=WATCHLIST_SCHEMA_MIGRATION_ID,
        description="Create advisory watchlist table with full builder-owned columns.",
        statements=WATCHLIST_SCHEMA_STATEMENTS,
        metadata={"tables": [TABLE_NAME]},
    )
    apply_schema_migration(
        migration_id=WATCHLIST_CONTEXT_OVERLAY_SCHEMA_MIGRATION_ID,
        description="Add context-overlay provenance columns to advisory watchlist.",
        statements=WATCHLIST_CONTEXT_OVERLAY_SCHEMA_STATEMENTS,
        metadata={"tables": [TABLE_NAME], "depends_on": WATCHLIST_SCHEMA_MIGRATION_ID},
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
        _record_watchlist_builder_fallback(
            fallback_type="watchlist_builder_table_check_failed",
            source=table_name,
            reason="Watchlist builder could not check whether an optional input table exists.",
            error=exc,
            metadata={},
        )
        return False
    return not df.empty


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
        _record_watchlist_builder_fallback(
            fallback_type="watchlist_builder_column_check_failed",
            source=table_name,
            reason="Watchlist builder could not inspect optional input table columns.",
            error=exc,
            metadata={},
        )
        return set()
    if df.empty or "column_name" not in df.columns:
        return set()
    return {str(value) for value in df["column_name"].dropna().tolist()}


def _boolish(value: object) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "t", "yes", "y"}


def _load_context_technical_actionability(
    *,
    symbols: list[str],
    asof_date: pd.Timestamp,
) -> dict[str, dict[str, object]]:
    if not WATCHLIST_CONTEXT_OVERLAY_TECHNICAL_PRIORITY_ENABLED:
        return {}
    clean_symbols = sorted({str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()})
    if not clean_symbols:
        return {}
    if not table_exists(TECHNICAL_DAILY_TABLE):
        return {}
    try:
        df = sql_to_df(
            f"""
            SELECT DISTINCT ON (UPPER(TRIM(symbol)))
                UPPER(TRIM(symbol)) AS symbol,
                asof_date,
                adj_close,
                avg_traded_value_20d,
                rs_vs_benchmark,
                rs_vs_sector,
                pivot_distance_20d_pct,
                pass_liquidity_20d,
                pass_trend_alignment,
                pass_breakout_extension,
                pass_gap_behavior,
                load_ts
            FROM {TECHNICAL_DAILY_TABLE}
            WHERE asof_date <= %s
              AND UPPER(TRIM(symbol)) = ANY(%s)
              AND UPPER(TRIM(COALESCE(series, 'daily'))) IN ('DAILY', 'EQ')
            ORDER BY UPPER(TRIM(symbol)), asof_date DESC, load_ts DESC NULLS LAST
            """,
            params=(asof_date, clean_symbols),
            retries=3,
            statement_timeout_ms=15000,
        )
    except Exception as exc:
        _record_watchlist_builder_fallback(
            fallback_type="watchlist_builder_context_technical_priority_load_failed",
            source=TECHNICAL_DAILY_TABLE,
            reason="Watchlist builder could not load technical actionability hints for context-overlay ranking; context rows remain ranked by context pressure only.",
            error=exc,
            metadata={"asof_date": asof_date.isoformat(), "symbol_count": len(clean_symbols)},
        )
        return {}
    if df.empty or "symbol" not in df.columns:
        return {}
    frame = df.copy()
    frame["symbol"] = frame["symbol"].astype("string").str.strip().str.upper()
    frame["asof_date"] = pd.to_datetime(frame.get("asof_date"), utc=True, errors="coerce")
    for column in ["adj_close", "avg_traded_value_20d", "rs_vs_benchmark", "rs_vs_sector", "pivot_distance_20d_pct"]:
        if column not in frame.columns:
            frame[column] = pd.NA
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    for column in ["pass_liquidity_20d", "pass_trend_alignment", "pass_breakout_extension", "pass_gap_behavior"]:
        if column not in frame.columns:
            frame[column] = False
        frame[column] = frame[column].map(_boolish)
    frame["_near_pivot_5pct"] = frame["pivot_distance_20d_pct"].abs().le(5.0).fillna(False)
    frame["_rs_positive"] = frame["rs_vs_benchmark"].gt(0).fillna(False)
    out: dict[str, dict[str, object]] = {}
    for item in frame.to_dict(orient="records"):
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        liquidity = bool(item.get("pass_liquidity_20d"))
        trend = bool(item.get("pass_trend_alignment"))
        extension = bool(item.get("pass_breakout_extension"))
        gap = bool(item.get("pass_gap_behavior"))
        near_pivot = bool(item.get("_near_pivot_5pct"))
        rs_positive = bool(item.get("_rs_positive"))
        score = 0.25
        score += 0.20 if liquidity else 0.0
        score += 0.20 if trend else 0.0
        score += 0.15 if near_pivot else 0.0
        score += 0.10 if rs_positive else 0.0
        score += 0.05 if extension else 0.0
        score += 0.05 if gap else 0.0
        asof = pd.to_datetime(item.get("asof_date"), utc=True, errors="coerce")
        out[symbol] = {
            "technical_covered": True,
            "technical_actionability_score": round(max(0.0, min(1.0, float(score))), 4),
            "technical_asof_date": None if pd.isna(asof) else asof.normalize().isoformat(),
            "pass_liquidity_20d": liquidity,
            "pass_trend_alignment": trend,
            "near_pivot_5pct": near_pivot,
            "rs_positive": rs_positive,
            "pass_breakout_extension": extension,
            "pass_gap_behavior": gap,
        }
    return out


def _load_context_hard_ohlcv_blockers(
    *,
    symbols: list[str],
    asof_date: pd.Timestamp,
) -> dict[str, dict[str, object]]:
    if not WATCHLIST_CONTEXT_OVERLAY_SUPPRESS_HARD_OHLCV_BLOCKERS:
        return {}
    clean_symbols = sorted({str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()})
    if not clean_symbols:
        return {}
    out: dict[str, dict[str, object]] = {}
    hard_ohlcv_reasons = ["dhan_daily_sync_failed_no_history", "ohlcv_sync_zero_rows", "ohlcv_no_new_data"]
    hard_technical_reasons = ["technical_rows_missing_after_build"]
    if table_exists(TECHNICAL_REFRESH_STATUS_TABLE):
        try:
            df = sql_to_df(
                f"""
                SELECT DISTINCT ON (UPPER(TRIM(symbol)))
                    UPPER(TRIM(symbol)) AS symbol,
                    asof_date,
                    stage,
                    status,
                    action,
                    reason,
                    rows,
                    error_type,
                    error_text,
                    load_ts
                FROM {TECHNICAL_REFRESH_STATUS_TABLE}
                WHERE asof_date <= %s
                  AND UPPER(TRIM(symbol)) = ANY(%s)
                  AND (
                    (stage = 'ohlcv' AND reason = ANY(%s))
                    OR (stage = 'technical_build' AND reason = ANY(%s))
                  )
                ORDER BY UPPER(TRIM(symbol)), asof_date DESC, load_ts DESC NULLS LAST
                """,
                params=(asof_date, clean_symbols, hard_ohlcv_reasons, hard_technical_reasons),
                retries=3,
                statement_timeout_ms=10000,
            )
        except Exception as exc:
            _record_watchlist_builder_fallback(
                fallback_type="watchlist_builder_context_hard_ohlcv_blocker_load_failed",
                source=TECHNICAL_REFRESH_STATUS_TABLE,
                reason="Watchlist builder could not load hard OHLCV blockers for context-overlay suppression; rows remain governed by identity/reliability/technical confirmation gates.",
                error=exc,
                metadata={"asof_date": asof_date.isoformat(), "symbol_count": len(clean_symbols)},
            )
            df = pd.DataFrame()
        if not df.empty and "symbol" in df.columns:
            for item in df.to_dict(orient="records"):
                symbol = str(item.get("symbol") or "").strip().upper()
                if not symbol:
                    continue
                asof = pd.to_datetime(item.get("asof_date"), utc=True, errors="coerce")
                out[symbol] = {
                    "symbol": symbol,
                    "asof_date": None if pd.isna(asof) else asof.isoformat(),
                    "reason": str(item.get("reason") or "").strip() or None,
                    "status": str(item.get("status") or "").strip() or None,
                    "action": str(item.get("action") or "").strip() or None,
                    "rows": None if pd.isna(item.get("rows")) else float(item.get("rows")),
                    "error_type": str(item.get("error_type") or "").strip() or None,
                    "error_text": str(item.get("error_text") or "").strip()[:300] or None,
                    "blocker_source": TECHNICAL_REFRESH_STATUS_TABLE,
                }
    if table_exists(IDENTITY_ISSUES_TABLE):
        try:
            issue_df = sql_to_df(
                f"""
                SELECT DISTINCT ON (UPPER(TRIM(symbol)))
                    UPPER(TRIM(symbol)) AS symbol,
                    issue_key,
                    issue_type,
                    status,
                    source,
                    error_text,
                    suggested_action,
                    attempt_count,
                    last_seen_at,
                    context_json
                FROM {IDENTITY_ISSUES_TABLE}
                WHERE COALESCE(status, 'open') IN ('open', 'active')
                  AND issue_type = 'dhan_ohlcv_history_unavailable'
                  AND UPPER(TRIM(symbol)) = ANY(%s)
                  AND COALESCE(attempt_count, 0) >= %s
                ORDER BY UPPER(TRIM(symbol)), last_seen_at DESC NULLS LAST
                """,
                params=(clean_symbols, max(1, int(WATCHLIST_CONTEXT_OVERLAY_OHLCV_ISSUE_EXCLUSION_ATTEMPTS))),
                retries=3,
                statement_timeout_ms=10000,
            )
        except Exception as exc:
            _record_watchlist_builder_fallback(
                fallback_type="watchlist_builder_context_ohlcv_issue_exclusion_load_failed",
                source=IDENTITY_ISSUES_TABLE,
                reason="Watchlist builder could not load durable Dhan OHLCV-history issue exclusions; rows remain governed by refresh-status, identity, reliability, and technical confirmation gates.",
                error=exc,
                metadata={"asof_date": asof_date.isoformat(), "symbol_count": len(clean_symbols)},
            )
            issue_df = pd.DataFrame()
        if not issue_df.empty and "symbol" in issue_df.columns:
            for item in issue_df.to_dict(orient="records"):
                symbol = str(item.get("symbol") or "").strip().upper()
                if not symbol or symbol in out:
                    continue
                last_seen = pd.to_datetime(item.get("last_seen_at"), utc=True, errors="coerce")
                out[symbol] = {
                    "symbol": symbol,
                    "asof_date": None if pd.isna(last_seen) else last_seen.isoformat(),
                    "reason": "dhan_ohlcv_history_unavailable",
                    "status": str(item.get("status") or "").strip() or None,
                    "action": "exclude_context_watch_intake_until_ohlcv_history_exists",
                    "issue_key": str(item.get("issue_key") or "").strip() or None,
                    "issue_type": str(item.get("issue_type") or "").strip() or None,
                    "attempt_count": None if pd.isna(item.get("attempt_count")) else int(item.get("attempt_count")),
                    "error_text": str(item.get("error_text") or "").strip()[:300] or None,
                    "suggested_action": str(item.get("suggested_action") or "").strip()[:500] or None,
                    "blocker_source": IDENTITY_ISSUES_TABLE,
                }
    return out


def _load_context_stale_ohlcv_blockers(
    *,
    symbols: list[str],
    asof_date: pd.Timestamp,
) -> dict[str, dict[str, object]]:
    clean_symbols = sorted({str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()})
    if not clean_symbols or not table_exists(TECHNICAL_REFRESH_STATUS_TABLE):
        return {}
    try:
        df = sql_to_df(
            f"""
            SELECT DISTINCT ON (UPPER(TRIM(symbol)))
                UPPER(TRIM(symbol)) AS symbol,
                asof_date,
                stage,
                status,
                action,
                reason,
                rows,
                error_type,
                error_text,
                raw_json,
                load_ts
            FROM {TECHNICAL_REFRESH_STATUS_TABLE}
            WHERE asof_date <= %s
              AND UPPER(TRIM(symbol)) = ANY(%s)
              AND stage = 'ohlcv'
              AND reason = 'dhan_daily_sync_failed_with_stale_history'
            ORDER BY UPPER(TRIM(symbol)), asof_date DESC, load_ts DESC NULLS LAST
            """,
            params=(asof_date, clean_symbols),
            retries=3,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        _record_watchlist_builder_fallback(
            fallback_type="watchlist_builder_context_stale_ohlcv_blocker_load_failed",
            source=TECHNICAL_REFRESH_STATUS_TABLE,
            reason="Watchlist builder could not load stale-history OHLCV blockers for context-overlay ranking; technical priority may not include stale-data warnings.",
            error=exc,
            metadata={"asof_date": asof_date.isoformat(), "symbol_count": len(clean_symbols)},
        )
        return {}
    if df.empty or "symbol" not in df.columns:
        return {}
    out: dict[str, dict[str, object]] = {}
    for item in df.to_dict(orient="records"):
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        asof = pd.to_datetime(item.get("asof_date"), utc=True, errors="coerce")
        raw = item.get("raw_json")
        latest_date = None
        if isinstance(raw, str) and raw.strip():
            try:
                payload = json.loads(raw)
                latest_date = payload.get("latest_date") if isinstance(payload, dict) else None
            except Exception as exc:
                _record_watchlist_builder_fallback(
                    fallback_type="watchlist_builder_context_stale_ohlcv_raw_json_parse_failed",
                    source=TECHNICAL_REFRESH_STATUS_TABLE,
                    reason="Watchlist builder could not parse stale-history OHLCV blocker raw_json; continuing without latest_date metadata.",
                    error=exc,
                    metadata={
                        "symbol": symbol,
                        "payload_length": len(raw),
                        "asof_date": None if pd.isna(asof) else asof.isoformat(),
                    },
                )
                latest_date = None
        out[symbol] = {
            "symbol": symbol,
            "asof_date": None if pd.isna(asof) else asof.isoformat(),
            "latest_date": latest_date,
            "reason": str(item.get("reason") or "").strip() or None,
            "status": str(item.get("status") or "").strip() or None,
            "action": str(item.get("action") or "").strip() or None,
            "error_type": str(item.get("error_type") or "").strip() or None,
            "error_text": str(item.get("error_text") or "").strip()[:300] or None,
        }
    return out


def load_existing_watch_symbols() -> list[str]:
    if not table_exists(TABLE_NAME):
        return []
    try:
        df = sql_to_df(
            f"""
            SELECT DISTINCT UPPER(TRIM(symbol)) AS symbol
            FROM {TABLE_NAME}
            WHERE symbol IS NOT NULL
              AND (
                COALESCE(watch_enabled, false) IS TRUE
                OR LOWER(TRIM(COALESCE(watch_status, ''))) IN ('active', 'review_manual')
              )
            """,
            retries=3,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        _record_watchlist_builder_fallback(
            fallback_type="watchlist_builder_existing_symbols_load_failed",
            source=TABLE_NAME,
            reason="Watchlist builder could not load active existing symbols for targeted negative context-overlay suppression.",
            error=exc,
            metadata={},
        )
        return []
    if df.empty or "symbol" not in df.columns:
        return []
    return sorted({str(value).strip().upper() for value in df["symbol"].dropna().tolist() if str(value or "").strip()})


def load_candidate_rows(
    *,
    asof_date: pd.Timestamp | None = None,
    setup_ids: list[str] | None = None,
    symbols: list[str] | None = None,
) -> pd.DataFrame:
    clauses = []
    params: list[object] = []
    if asof_date is not None:
        clauses.append("asof_date = %s")
        params.append(asof_date)
    else:
        clauses.append("asof_date = (SELECT MAX(asof_date) FROM advisory_candidates)")
    if setup_ids:
        clauses.append("setup_id = ANY(%s)")
        params.append([value.upper() for value in setup_ids])
    if symbols:
        clauses.append("symbol = ANY(%s)")
        params.append([value.upper() for value in symbols])
    df = sql_to_df(
        f"""
        SELECT *
        FROM advisory_candidates
        WHERE {' AND '.join(clauses)}
        ORDER BY setup_id, symbol
        """,
        params=tuple(params) if params else None,
    )
    if df.empty:
        return df
    df["asof_date"] = normalize_timestamp(df["asof_date"])
    df["symbol"] = df["symbol"].astype("string").str.upper()
    if "watch_enabled" not in df.columns:
        df["watch_enabled"] = True
    else:
        df["watch_enabled"] = df["watch_enabled"].fillna(True)
    if "watch_reasons" not in df.columns:
        df["watch_reasons"] = "[]"
    if "candidate_state" not in df.columns:
        df["candidate_state"] = "PASS_NOW"
    df = df[df["watch_enabled"] == True].reset_index(drop=True)
    return df


def _normalize_asof_date(asof_date: pd.Timestamp | None) -> pd.Timestamp:
    ts = pd.to_datetime(asof_date or pd.Timestamp.utcnow(), utc=True, errors="coerce")
    if pd.isna(ts):
        ts = pd.Timestamp.utcnow()
    return ts.normalize()


def _context_overlay_asof_window(asof_date: pd.Timestamp | None) -> tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]:
    ts = pd.to_datetime(asof_date or pd.Timestamp.utcnow(), utc=True, errors="coerce")
    if pd.isna(ts):
        ts = pd.Timestamp.utcnow()
    effective_asof = ts.normalize()
    cutoff = effective_asof + pd.Timedelta(days=1) if ts == effective_asof else ts
    from_date = cutoff - pd.Timedelta(days=max(1, int(WATCHLIST_CONTEXT_OVERLAY_LOOKBACK_DAYS)))
    return effective_asof, from_date, cutoff


def _context_reason_expr(columns: set[str]) -> str:
    if "watch_reason_detail" in columns:
        return "watch_reason_detail"
    if "trigger_reason" in columns:
        return "trigger_reason"
    if "theme_reason" in columns:
        return "theme_reason"
    return "NULL::text"


def _json_safe(value: object) -> object:
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    try:
        if pd.isna(value):
            return None
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.watchlist_builder",
            fallback_type="watchlist_builder_json_safe_missing_check_failed",
            source="json_safe",
            severity="warn",
            reason="Watchlist builder could not evaluate missingness for a JSON context value and kept the original value.",
            error=exc,
            metadata={"value_type": type(value).__name__},
        )
    return value


def _runtime_policy_contract_for_reliability(row: dict[str, object] | None) -> dict[str, object]:
    if not isinstance(row, dict):
        return reliability_runtime_policy_contract(None)
    contract = row.get("runtime_policy_contract")
    if isinstance(contract, dict) and contract:
        return contract
    return reliability_runtime_policy_contract(row.get("classification"))


def _runtime_contract_allows(row: dict[str, object] | None, use_name: str, *, legacy_classification: str | None = None) -> bool:
    contract = _runtime_policy_contract_for_reliability(row)
    allowed = contract.get("allowed_runtime_uses")
    if isinstance(allowed, dict) and use_name in allowed:
        return bool(allowed.get(use_name))
    classification = str((row or {}).get("classification") or legacy_classification or "").strip()
    if use_name == "watch_priority":
        return classification == "candidate_helpful"
    if use_name == "de_risk_review":
        return classification == "protective_candidate"
    return False


def _context_reliability_priority_multiplier(family: str, reliability_by_family: dict[str, object]) -> tuple[str | None, float, str]:
    if not WATCHLIST_CONTEXT_OVERLAY_RELIABILITY_PRIORITY_ENABLED:
        return None, 1.0, "disabled"
    row = reliability_by_family.get(str(family)) if isinstance(reliability_by_family, dict) else None
    if not isinstance(row, dict):
        return None, 1.0, "no_evidence_neutral"
    classification = str(row.get("classification") or "").strip()
    if _runtime_contract_allows(row, "watch_priority", legacy_classification=classification):
        return classification, max(1.0, float(WATCHLIST_CONTEXT_OVERLAY_HELPFUL_SCORE_MULTIPLIER)), "candidate_helpful_priority_boost"
    return classification or None, 1.0, "classification_neutral"


def _watch_state_reliability_classification(
    reliability_row: dict[str, object] | None,
    candidate_state: str,
    *,
    watch_breakout_blocker: str = "none",
) -> str | None:
    if not isinstance(reliability_row, dict):
        return None
    target_state = str(candidate_state or "").strip().upper()
    target_blocker = str(watch_breakout_blocker or "none").strip().lower()
    for item in reliability_row.get("watch_state_diagnostics") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("context_candidate_state") or "").strip().upper() != target_state:
            continue
        blocker = str(item.get("watch_breakout_blocker") or "none").strip().lower()
        if blocker != target_blocker:
            continue
        classification = str(item.get("classification") or "").strip()
        return classification or None
    return None


def _watch_breakout_reliability_blocks_promotion(reliability_row: dict[str, object] | None) -> tuple[bool, str | None]:
    classification = _watch_state_reliability_classification(
        reliability_row,
        "WATCH_BREAKOUT",
        watch_breakout_blocker="none",
    )
    if classification in WATCHLIST_CONTEXT_OVERLAY_EXCLUDED_RELIABILITY_CLASSES:
        return True, classification
    return False, classification


def _context_class_reliability_classification(reliability_row: dict[str, object] | None, context_class: object) -> str | None:
    if not isinstance(reliability_row, dict):
        return None
    target_class = str(context_class or "").strip().upper()
    if not target_class or target_class in {"<NA>", "NAN", "NONE"}:
        return None
    for item in reliability_row.get("context_class_diagnostics") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("context_class") or "").strip().upper() != target_class:
            continue
        classification = str(item.get("classification") or "").strip()
        return classification or None
    return None


def _context_class_reliability_blocks_watch(reliability_row: dict[str, object] | None, context_class: object) -> tuple[bool, str | None]:
    classification = _context_class_reliability_classification(reliability_row, context_class)
    if classification in WATCHLIST_CONTEXT_OVERLAY_EXCLUDED_RELIABILITY_CLASSES:
        return True, classification
    return False, classification


def _sector_key_from_values(*values: object) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if not text or text.upper() in {"<NA>", "NAN", "NONE"}:
            continue
        return "".join(ch for ch in text.upper() if ch.isalnum())
    return None


def _first_nonempty_text(*values: object) -> str | None:
    for value in values:
        if pd.api.types.is_scalar(value) and pd.isna(value):
            continue
        text = str(value or "").strip()
        if text and text.upper() not in {"<NA>", "NAN", "NONE"}:
            return text
    return None


def _technical_confirmation_plan_for_context_watch(
    *,
    candidate_state: str,
    policy_effect: str,
    context_source: str,
    context_class: str | None = None,
) -> dict[str, object]:
    state = str(candidate_state or "WATCH_EVENT").strip().upper()
    source = str(context_source or "context_overlay").strip() or "context_overlay"
    context_class_text = str(context_class or "").strip()
    base_plan: dict[str, object] = {
        "plan_type": "context_watch_requires_technical_confirmation",
        "context_source": source,
        "context_class": context_class_text or None,
        "context_candidate_state": state,
        "context_policy_effect": str(policy_effect or "watch_only_no_buy_authority"),
        "required_next_stage": "full_advisory_technical_risk_lifecycle_confirmation",
        "broker_execution_allowed": False,
        "portfolio_authority": "none",
        "full_advisory_required": True,
        "cannot_do": [
            "cannot_create_buy_without_confirmed_technical_entry",
            "cannot_create_portfolio_allocation_without_risk_lifecycle_confirmation",
            "cannot_create_broker_order",
        ],
    }
    if state == "WATCH_BREAKOUT":
        base_plan.update(
            {
                "operator_summary": "Context evidence is strong enough for breakout watch priority; wait for a valid pivot/breakout or equivalent technical entry trigger before any buy.",
                "wait_for": [
                    "valid_pivot_or_breakout_level",
                    "close_above_trigger_or_retest_hold",
                    "volume_or_relative_strength_confirmation",
                    "risk_lifecycle_and_position_state_pass",
                ],
                "entry_gate": "BUY_TRIGGERED_or_READY_after_full_advisory",
                "watch_action": "prioritize_for_breakout_monitoring",
            }
        )
    else:
        base_plan.update(
            {
                "operator_summary": "Context evidence adds the stock to watch only; wait for constructive technical setup and full advisory confirmation.",
                "wait_for": [
                    "constructive_base_or_pullback_setup",
                    "technical_state_NEAR_PIVOT_READY_or_BUY_TRIGGERED",
                    "liquidity_and_feature_freshness_pass",
                    "risk_lifecycle_and_position_state_pass",
                ],
                "entry_gate": "NEAR_PIVOT_READY_or_BUY_TRIGGERED_after_full_advisory",
                "watch_action": "keep_on_context_watchlist",
            }
        )
    return base_plan


def _context_sector_reliability_classification(
    reliability_row: dict[str, object] | None,
    *,
    sector_name: object = None,
    sector_code: object = None,
) -> str | None:
    if not isinstance(reliability_row, dict):
        return None
    target_keys = {
        key
        for key in [
            _sector_key_from_values(sector_code),
            _sector_key_from_values(sector_name),
        ]
        if key
    }
    if not target_keys:
        return None
    for item in reliability_row.get("sector_diagnostics") or []:
        if not isinstance(item, dict):
            continue
        item_keys = {
            key
            for key in [
                _sector_key_from_values(item.get("sector_code")),
                _sector_key_from_values(item.get("sector_name")),
                _sector_key_from_values(item.get("sector_key")),
            ]
            if key
        }
        if not target_keys & item_keys:
            continue
        classification = str(item.get("classification") or "").strip()
        return classification or None
    return None


def _sector_reliability_blocks_watch_priority(classification: str | None) -> bool:
    if not classification:
        return False
    return str(classification).strip() != "candidate_helpful"


def _negative_context_reliability_blocks_suppression(
    reliability_row: dict[str, object] | None,
    context_class: object,
) -> tuple[bool, str | None, str | None]:
    family_classification = str((reliability_row or {}).get("classification") or "").strip() if isinstance(reliability_row, dict) else ""
    if family_classification in WATCHLIST_CONTEXT_OVERLAY_EXCLUDED_RELIABILITY_CLASSES:
        return True, family_classification, None
    class_blocked, class_classification = _context_class_reliability_blocks_watch(reliability_row, context_class)
    if class_blocked:
        return True, family_classification or None, class_classification
    return False, family_classification or None, class_classification


def load_context_family_reliability(asof_date: pd.Timestamp | None = None) -> dict[str, object]:
    effective_asof = _normalize_asof_date(asof_date)
    try:
        persisted_report = load_persisted_context_reliability_report(asof_date=effective_asof)
    except Exception as exc:
        _record_watchlist_builder_fallback(
            fallback_type="watchlist_builder_persisted_context_reliability_load_failed",
            source="advisory_context_overlay_reliability_summary",
            reason="Watchlist builder could not load persisted fast context-overlay reliability evidence; recomputing from evaluator summaries.",
            error=exc,
            metadata={"asof_date": str(effective_asof)},
        )
        persisted_report = {}
    persisted_families = persisted_report.get("families") if isinstance(persisted_report, dict) else None
    if persisted_families:
        families = {
            str(row.get("source_family")): {
                "classification": _json_safe(row.get("classification")),
                "total_selected_count": _json_safe(row.get("total_selected_count")),
                "total_matured_count": _json_safe(row.get("total_matured_count")),
                "avg_lift_vs_technical_only": None,
                "avg_forward_return_after_cost": _json_safe(row.get("avg_forward_return_after_cost")),
                "avg_hit_rate_after_cost": _json_safe(row.get("avg_hit_rate_after_cost")),
                "evidence_source": "persisted_context_overlay_reliability",
                "watch_matured_count": _json_safe(row.get("watch_matured_count")),
                "negative_pressure_matured_count": _json_safe(row.get("negative_pressure_matured_count")),
                "context_class_diagnostics": _json_safe(row.get("context_class_diagnostics") or []),
                "context_class_policy_effect": _json_safe(row.get("context_class_policy_effect")),
                "watch_state_diagnostics": _json_safe(row.get("watch_state_diagnostics") or []),
                "watch_state_policy_effect": _json_safe(row.get("watch_state_policy_effect")),
                "runtime_policy_contract": _json_safe(_runtime_policy_contract_for_reliability(row)),
            }
            for row in persisted_families
            if row.get("source_family")
        }
        return {
            "status": _json_safe(persisted_report.get("status")),
            "evaluated_at": pd.to_datetime(persisted_report.get("evaluated_at"), utc=True, errors="coerce"),
            "families": families,
            "evidence_source": "persisted_context_overlay_reliability",
        }
    try:
        fast_report = load_fast_context_reliability_report(asof_date=effective_asof)
    except Exception as exc:
        _record_watchlist_builder_fallback(
            fallback_type="watchlist_builder_fast_context_reliability_load_failed",
            source="advisory_context_overlay_reliability_report",
            reason="Watchlist builder could not load fast context-overlay reliability evidence; falling back to full signal-quality family evidence.",
            error=exc,
            metadata={"asof_date": str(effective_asof)},
        )
        fast_report = {}
    fast_families = fast_report.get("families") if isinstance(fast_report, dict) else None
    if fast_families:
        families = {
            str(row.get("source_family")): {
                "classification": _json_safe(row.get("classification")),
                "total_selected_count": _json_safe(row.get("total_selected_count")),
                "total_matured_count": _json_safe(row.get("total_matured_count")),
                "avg_lift_vs_technical_only": None,
                "avg_forward_return_after_cost": _json_safe(row.get("avg_forward_return_after_cost")),
                "avg_hit_rate_after_cost": _json_safe(row.get("avg_hit_rate_after_cost")),
                "evidence_source": "fast_context_overlay_reliability",
                "watch_matured_count": _json_safe(row.get("watch_matured_count")),
                "negative_pressure_matured_count": _json_safe(row.get("negative_pressure_matured_count")),
                "context_class_diagnostics": _json_safe(row.get("context_class_diagnostics") or []),
                "context_class_policy_effect": _json_safe(row.get("context_class_policy_effect")),
                "watch_state_diagnostics": _json_safe(row.get("watch_state_diagnostics") or []),
                "watch_state_policy_effect": _json_safe(row.get("watch_state_policy_effect")),
                "runtime_policy_contract": _json_safe(_runtime_policy_contract_for_reliability(row)),
            }
            for row in fast_families
            if row.get("source_family")
        }
        return {
            "status": _json_safe(fast_report.get("status")),
            "evaluated_at": pd.to_datetime(fast_report.get("evaluated_at"), utc=True, errors="coerce"),
            "families": families,
            "evidence_source": "fast_context_overlay_reliability",
        }
    if not table_exists(SIGNAL_QUALITY_SUMMARY_TABLE):
        return {}
    cutoff = effective_asof + pd.Timedelta(days=1)
    variants = sorted(VARIANT_TO_SOURCE_FAMILY)
    try:
        latest = sql_to_df(
            f"""
            SELECT MAX(evaluated_at) AS evaluated_at
            FROM {SIGNAL_QUALITY_SUMMARY_TABLE}
            WHERE variant = ANY(%(variants)s)
              AND evaluated_at < %(cutoff)s
            """,
            params={"variants": variants, "cutoff": cutoff},
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
        _record_watchlist_builder_fallback(
            fallback_type="watchlist_builder_context_family_reliability_load_failed",
            source=SIGNAL_QUALITY_SUMMARY_TABLE,
            reason="Watchlist builder could not load source-family reliability evidence; context-overlay watchlist intake remained ungated.",
            error=exc,
            metadata={"asof_date": str(effective_asof)},
        )
        return {}
    families = {
        str(row.get("source_family")): {
            "classification": _json_safe(row.get("classification")),
            "total_selected_count": _json_safe(row.get("total_selected_count")),
            "total_matured_count": _json_safe(row.get("total_matured_count")),
            "avg_lift_vs_technical_only": _json_safe(row.get("avg_lift_vs_technical_only")),
            "avg_forward_return_after_cost": _json_safe(row.get("avg_forward_return_after_cost")),
            "avg_benchmark_forward_return": _json_safe(row.get("avg_benchmark_forward_return")),
            "avg_excess_forward_return_after_cost": _json_safe(row.get("avg_excess_forward_return_after_cost")),
            "avg_hit_rate_after_cost": _json_safe(row.get("avg_hit_rate_after_cost")),
            "avg_excess_hit_rate_after_cost": _json_safe(row.get("avg_excess_hit_rate_after_cost")),
            "runtime_policy_contract": _json_safe(reliability_runtime_policy_contract(row.get("classification"))),
        }
        for row in report.get("families", [])
        if row.get("source_family")
    }
    readiness = report.get("promotion_readiness") if isinstance(report.get("promotion_readiness"), dict) else {}
    return {
        "status": _json_safe(report.get("status")),
        "evaluated_at": pd.to_datetime(report.get("evaluated_at"), utc=True, errors="coerce"),
        "evidence_source": "signal_quality_family_report",
        "promotion_readiness": _json_safe(readiness),
        "candidate_helpful_families": _json_safe(readiness.get("candidate_helpful_families") or []),
        "benchmark_or_attribution_blocked_families": _json_safe(readiness.get("benchmark_or_attribution_blocked_families") or []),
        "families": families,
    }


def _load_direct_context_overlay_watch_rows(
    *,
    table_name: str,
    source_name: str,
    asof_date: pd.Timestamp,
    symbols: list[str] | None,
    date_column: str,
    class_column: str | None = None,
    directions: tuple[str, ...] = ("positive", "watch"),
) -> pd.DataFrame:
    if not WATCHLIST_CONTEXT_OVERLAY_ENABLED or not table_exists(table_name):
        return pd.DataFrame()
    columns = table_columns(table_name)
    if not {"symbol", date_column, "direction"}.issubset(columns):
        return pd.DataFrame()
    clean_symbols = sorted({str(value).strip().upper() for value in symbols or [] if str(value or "").strip()})
    symbol_clause = "AND UPPER(TRIM(symbol)) = ANY(%(symbols)s)" if clean_symbols else ""
    clean_directions = sorted({str(value or "").strip().lower() for value in directions if str(value or "").strip()})
    if not clean_directions:
        return pd.DataFrame()
    effective_asof, from_date, cutoff = _context_overlay_asof_window(asof_date)
    score_expr = "pressure_score" if "pressure_score" in columns else "NULL::double precision"
    class_expr = class_column if class_column and class_column in columns else "NULL::text"
    overlay_expr = "overlay_id" if "overlay_id" in columns else "NULL::text"
    sector_name_expr = "sector_name" if "sector_name" in columns else "NULL::text"
    sector_code_expr = "sector_code" if "sector_code" in columns else "NULL::text"
    reason_expr = _context_reason_expr(columns)
    try:
        return sql_to_df(
            f"""
            SELECT
                %(asof_date)s::timestamptz AS asof_date,
                UPPER(TRIM(symbol)) AS symbol,
                {date_column} AS context_asof_date,
                %(source_name)s::text AS context_source,
                {overlay_expr} AS context_overlay_id,
                direction,
                {score_expr} AS pressure_score,
                {class_expr} AS context_class,
                {sector_name_expr} AS overlay_sector_name,
                {sector_code_expr} AS overlay_sector_code,
                {sector_name_expr} AS universe_sector_name,
                {sector_code_expr} AS universe_sector_code,
                {reason_expr} AS context_reason
            FROM {table_name}
            WHERE {date_column} >= %(from_date)s
              AND {date_column} < %(cutoff)s
              AND LOWER(TRIM(COALESCE(direction, ''))) = ANY(%(directions)s)
              AND COALESCE(production_status, 'active') = 'active'
              AND COALESCE(authority_scope, 'watchlist_pressure_only') = 'watchlist_pressure_only'
              {symbol_clause}
            """,
            params={
                "asof_date": effective_asof,
                "from_date": from_date,
                "cutoff": cutoff,
                "symbols": clean_symbols,
                "source_name": source_name,
                "directions": clean_directions,
            },
            retries=3,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        _record_watchlist_builder_fallback(
            fallback_type="watchlist_builder_context_overlay_load_failed",
            source=table_name,
            reason="Watchlist builder could not load direct context-overlay watch candidates.",
            error=exc,
            metadata={"source_name": source_name, "lookback_days": WATCHLIST_CONTEXT_OVERLAY_LOOKBACK_DAYS},
        )
        return pd.DataFrame()


def _load_negative_direct_context_overlay_rows(
    *,
    asof_date: pd.Timestamp,
    symbols: list[str] | None,
) -> pd.DataFrame:
    if not WATCHLIST_CONTEXT_OVERLAY_NEGATIVE_SUPPRESSION_ENABLED:
        return pd.DataFrame()
    frames = [
        _load_direct_context_overlay_watch_rows(
            table_name=ANNOUNCEMENT_CONTEXT_OVERLAYS_TABLE,
            source_name="announcement_context",
            asof_date=asof_date,
            symbols=symbols,
            date_column="published_on",
            class_column="event_class",
            directions=("negative",),
        ),
        _load_direct_context_overlay_watch_rows(
            table_name=EXCHANGE_CONTEXT_OVERLAYS_TABLE,
            source_name="exchange_context",
            asof_date=asof_date,
            symbols=symbols,
            date_column="asof_date",
            class_column="event_type",
            directions=("negative",),
        ),
        _load_direct_context_overlay_watch_rows(
            table_name=BHAVCOPY_CONTEXT_OVERLAYS_TABLE,
            source_name="bhavcopy_context",
            asof_date=asof_date,
            symbols=symbols,
            date_column="asof_date",
            class_column="deal_pressure",
            directions=("negative",),
        ),
    ]
    frames = [frame for frame in frames if isinstance(frame, pd.DataFrame) and not frame.empty]
    if not frames:
        return pd.DataFrame()
    frame = pd.concat(frames, ignore_index=True, sort=False)
    frame["symbol"] = frame["symbol"].astype("string").str.strip().str.upper()
    frame["context_asof_date"] = pd.to_datetime(frame["context_asof_date"], utc=True, errors="coerce")
    frame["pressure_score"] = pd.to_numeric(frame.get("pressure_score"), errors="coerce").fillna(0.0)
    frame["direction"] = frame["direction"].astype("string").str.strip().str.lower()
    frame = frame[frame["direction"].eq("negative")].copy()
    frame = frame.dropna(subset=["symbol", "context_asof_date"])
    if frame.empty:
        return frame
    return frame.sort_values(["symbol", "pressure_score", "context_asof_date"], ascending=[True, False, False]).drop_duplicates(
        subset=["symbol"],
        keep="first",
    )


def _negative_context_suppression_rows(
    negative_frame: pd.DataFrame,
    *,
    effective_asof: pd.Timestamp,
    reliability_by_family: dict[str, object],
    reliability_evaluated_at: pd.Timestamp,
    suppressed_positive_by_symbol: dict[str, dict[str, object]] | None = None,
    reason_prefix: str = "Fresh negative context-overlay pressure",
) -> list[dict[str, object]]:
    if negative_frame.empty:
        return []
    rows: list[dict[str, object]] = []
    suppressed_positive_by_symbol = suppressed_positive_by_symbol or {}
    for negative in negative_frame.to_dict(orient="records"):
        symbol = str(negative.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        negative_ts = pd.to_datetime(negative.get("context_asof_date"), utc=True, errors="coerce")
        negative_source = str(negative.get("context_source") or "context_overlay")
        negative_class = str(negative.get("context_class") or "").strip()
        negative_reason = str(negative.get("context_reason") or "").strip()
        negative_overlay_id = str(negative.get("context_overlay_id") or f"{negative_source}:{symbol}:{negative_ts}")
        negative_score = max(0.0, min(1.0, float(negative.get("pressure_score") or 0.0)))
        reliability_row = reliability_by_family.get(negative_source) if isinstance(reliability_by_family, dict) else None
        blocked_by_reliability, reliability_label, context_class_reliability = _negative_context_reliability_blocks_suppression(
            reliability_row if isinstance(reliability_row, dict) else None,
            negative_class,
        )
        if blocked_by_reliability:
            continue
        reliability_label = reliability_label or ""
        suppressed_positive = suppressed_positive_by_symbol.get(symbol) or {}
        suppressed_positive_source = str(suppressed_positive.get("context_source") or "").strip()
        suppressed_positive_overlay_id = suppressed_positive.get("context_overlay_id")
        suppression_reason = (
            f"{reason_prefix} from {negative_source}"
            + (f" ({negative_class})" if negative_class else "")
            + (f": {negative_reason}" if negative_reason else "")
            + (f" Reliability: {reliability_label}." if reliability_label else "")
            + ". This removes context-watch intake only; it has no sell, portfolio, or broker authority."
        )
        rows.append(
            {
                "asof_date": effective_asof,
                "setup_id": CONTEXT_OVERLAY_SETUP_ID,
                "setup_name": CONTEXT_OVERLAY_SETUP_NAME,
                "regime_name": "LAYERED_CONTEXT",
                "base_regime": "LAYERED_CONTEXT",
                "news_overlay": negative_source,
                "theme_ids": pd.NA,
                "symbol": symbol,
                "company_master_id": pd.NA,
                "screener_slug": "context-overlay-watch",
                "source_screener_slug": "context-overlay-watch",
                "source_screener_list": negative_source,
                "rank": pd.NA,
                "candidate_state": "REJECT",
                "setup_score": 0.0,
                "watch_reason_detail": suppression_reason,
                "entry_style": "context_watch_suppressed",
                "attractive_price_low": pd.NA,
                "attractive_price_high": pd.NA,
                "invalidation_price": pd.NA,
                "entry_note": "Do not add or keep from context overlay; wait for fresh positive evidence and full advisory confirmation.",
                "near_miss_flag": False,
                "watch_enabled": False,
                "watch_reasons": json.dumps(
                    [
                        {
                            "source": negative_source,
                            "direction": "negative",
                            "context_class": negative_class or None,
                            "context_overlay_id": negative_overlay_id,
                            "context_asof_date": None if pd.isna(negative_ts) else negative_ts.isoformat(),
                            "pressure_score": negative_score,
                            "suppressed_positive_source": suppressed_positive_source or None,
                            "suppressed_positive_overlay_id": suppressed_positive_overlay_id,
                            "authority_scope": "watchlist_pressure_only",
                            "policy_effect": "suppress_context_watch_only_no_sell_authority",
                            "reliability_classification": reliability_label or None,
                            "context_class_reliability_classification": context_class_reliability,
                            "reliability_evaluated_at": None if pd.isna(reliability_evaluated_at) else reliability_evaluated_at.isoformat(),
                        }
                    ],
                    ensure_ascii=False,
                    default=str,
                ),
                "watch_source": "context_overlay",
                "context_source": negative_source,
                "context_overlay_id": negative_overlay_id,
                "context_authority_scope": "watchlist_pressure_only",
                "context_policy_effect": "suppress_context_watch_only_no_sell_authority",
                "context_reliability_classification": reliability_label or pd.NA,
                "context_class_reliability_classification": context_class_reliability or pd.NA,
                "context_reliability_evaluated_at": reliability_evaluated_at,
            }
        )
    return rows


def _map_context_company_master_ids(symbols: list[str]) -> dict[str, object] | None:
    clean_symbols = sorted({str(value or "").strip().upper() for value in symbols if str(value or "").strip()})
    if not clean_symbols:
        return {}
    if not table_exists(COMPANY_MASTER_TABLE):
        return None
    try:
        mapped = map_company_master_ids(clean_symbols, exchange="NSE")
    except Exception as exc:
        _record_watchlist_builder_fallback(
            fallback_type="watchlist_builder_context_identity_validation_failed",
            source=COMPANY_MASTER_TABLE,
            reason="Watchlist builder could not validate context-overlay symbols against company master; context watch rows are suppressed until identity validation is available.",
            error=exc,
            metadata={"symbol_count": len(clean_symbols), "symbols_sample": clean_symbols[:20]},
        )
        return None
    return {symbol: mapped.iloc[idx] for idx, symbol in enumerate(clean_symbols)}


def _is_non_empty_identity(value: object) -> bool:
    if value is None or pd.isna(value):
        return False
    text = str(value).strip()
    return bool(text and text.lower() not in {"nan", "none", "<na>"})


def _map_context_dhan_master_ids(symbols: list[str]) -> dict[str, object]:
    clean_symbols = sorted({str(value or "").strip().upper() for value in symbols if str(value or "").strip()})
    if not clean_symbols or not table_exists("master_dhan_instruments"):
        return {}
    try:
        df = sql_to_df(
            """
            WITH ranked AS (
                SELECT
                    UPPER(TRIM(underlying_symbol)) AS symbol,
                    exch_id,
                    security_id,
                    underlying_symbol,
                    ROW_NUMBER() OVER (
                        PARTITION BY UPPER(TRIM(underlying_symbol))
                        ORDER BY
                            CASE WHEN exch_id = 'NSE' THEN 0 WHEN exch_id = 'BSE' THEN 1 ELSE 2 END,
                            load_ts DESC NULLS LAST,
                            valid_from DESC NULLS LAST,
                            security_id DESC
                    ) AS rn
                FROM master_dhan_instruments
                WHERE valid_to IS NULL
                  AND instrument = 'EQUITY'
                  AND instrument_type = 'ES'
                  AND underlying_symbol IS NOT NULL
                  AND UPPER(TRIM(underlying_symbol)) = ANY(%s)
            )
            SELECT symbol, exch_id, security_id, underlying_symbol
            FROM ranked
            WHERE rn = 1
            """,
            params=(clean_symbols,),
            retries=3,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        _record_watchlist_builder_fallback(
            fallback_type="watchlist_builder_context_dhan_identity_fallback_failed",
            source="master_dhan_instruments",
            reason="Watchlist builder could not use current Dhan master as a fallback for context-overlay identity validation.",
            error=exc,
            metadata={"symbol_count": len(clean_symbols), "symbols_sample": clean_symbols[:20]},
        )
        return {}
    if df.empty:
        return {}
    mapping: dict[str, object] = {}
    for row in df.to_dict(orient="records"):
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        exch = str(row.get("exch_id") or "").strip().upper()
        underlying = str(row.get("underlying_symbol") or symbol).strip().upper()
        security_id = row.get("security_id")
        if exch == "NSE" and underlying:
            mapping[symbol] = f"nse:{underlying}"
        elif exch == "BSE" and _is_non_empty_identity(security_id):
            mapping[symbol] = f"bse:{str(security_id).strip()}"
    return mapping


def _map_context_company_identities(symbols: list[str]) -> tuple[dict[str, object], dict[str, str]] | None:
    company_mapping = _map_context_company_master_ids(symbols)
    if company_mapping is None:
        return None

    resolved: dict[str, object] = {}
    statuses: dict[str, str] = {}
    unresolved: list[str] = []
    for symbol, company_master_id in company_mapping.items():
        if _is_non_empty_identity(company_master_id):
            resolved[symbol] = company_master_id
            statuses[symbol] = "resolved_company_master"
        else:
            unresolved.append(symbol)

    dhan_mapping = _map_context_dhan_master_ids(unresolved)
    for symbol, dhan_identity in dhan_mapping.items():
        if _is_non_empty_identity(dhan_identity):
            resolved[symbol] = dhan_identity
            statuses[symbol] = "resolved_dhan_master_current_symbol"

    return resolved, statuses


def _build_context_identity_suppression_rows(
    frame: pd.DataFrame,
    *,
    effective_asof: pd.Timestamp,
    status: str,
    detail: str,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for item in frame.to_dict(orient="records"):
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        source = str(item.get("context_source") or "context_overlay").strip() or "context_overlay"
        context_class = str(item.get("context_class") or "").strip()
        context_asof = pd.to_datetime(item.get("context_asof_date"), utc=True, errors="coerce")
        overlay_id = str(item.get("context_overlay_id") or f"{source}:{symbol}:{context_asof}")
        reason = (
            f"Suppressed {source} context-watch intake"
            + (f" for {context_class}" if context_class else "")
            + f" because {detail}. "
            + "This is an identity/source blocker only; it has no buy, sell, portfolio, or broker authority."
        )
        rows.append(
            {
                "asof_date": effective_asof,
                "setup_id": CONTEXT_OVERLAY_SETUP_ID,
                "setup_name": CONTEXT_OVERLAY_SETUP_NAME,
                "regime_name": "LAYERED_CONTEXT",
                "base_regime": "LAYERED_CONTEXT",
                "news_overlay": source,
                "theme_ids": pd.NA,
                "symbol": symbol,
                "company_master_id": pd.NA,
                "screener_slug": "context-overlay-watch",
                "source_screener_slug": "context-overlay-watch",
                "source_screener_list": source,
                "rank": pd.NA,
                "candidate_state": "REJECT",
                "setup_score": 0.0,
                "watch_reason_detail": reason,
                "entry_style": "context_watch_identity_blocked",
                "attractive_price_low": pd.NA,
                "attractive_price_high": pd.NA,
                "invalidation_price": pd.NA,
                "entry_note": "Repair or restore company/security identity validation before this context can add watch pressure.",
                "near_miss_flag": False,
                "watch_enabled": False,
                "watch_reasons": json.dumps(
                    [
                        {
                            "source": source,
                            "direction": str(item.get("direction") or "watch"),
                            "context_class": context_class or None,
                            "context_overlay_id": overlay_id,
                            "context_asof_date": None if pd.isna(context_asof) else context_asof.isoformat(),
                            "pressure_score": max(0.0, min(1.0, float(item.get("pressure_score") or 0.0))),
                            "authority_scope": "watchlist_pressure_only",
                            "policy_effect": "suppress_context_identity_unresolved_no_trade_authority",
                            "identity_validation_status": status,
                            "identity_validation_source": COMPANY_MASTER_TABLE,
                        }
                    ],
                    ensure_ascii=False,
                    default=str,
                ),
                "watch_source": "context_overlay",
                "context_source": source,
                "context_overlay_id": overlay_id,
                "context_authority_scope": "watchlist_pressure_only",
                "context_policy_effect": "suppress_context_identity_unresolved_no_trade_authority",
                "context_reliability_classification": pd.NA,
                "context_class_reliability_classification": pd.NA,
                "context_reliability_evaluated_at": pd.NaT,
            }
        )
    return rows


def _identity_unresolved_context_rows(frame: pd.DataFrame, *, effective_asof: pd.Timestamp) -> tuple[pd.DataFrame, list[dict[str, object]]]:
    if frame.empty or "symbol" not in frame.columns:
        return frame, []
    symbols = sorted({str(value or "").strip().upper() for value in frame["symbol"].tolist() if str(value or "").strip()})
    identity_mapping = _map_context_company_identities(symbols)
    if identity_mapping is None:
        return (
            pd.DataFrame(),
            _build_context_identity_suppression_rows(
                frame,
                effective_asof=effective_asof,
                status="company_master_validation_unavailable",
                detail="company-master validation is unavailable",
            ),
        )
    mapping, statuses = identity_mapping
    out = frame.copy()
    out["company_master_id"] = out["symbol"].astype("string").str.strip().str.upper().map(mapping)
    out["context_identity_validation_status"] = (
        out["symbol"].astype("string").str.strip().str.upper().map(statuses).fillna("unresolved_company_master")
    )
    unresolved_mask = out["company_master_id"].isna() | out["company_master_id"].astype("string").str.strip().isin(["", "nan", "None", "<NA>"])
    if not bool(unresolved_mask.any()):
        return out, []
    unresolved_rows = _build_context_identity_suppression_rows(
        out.loc[unresolved_mask],
        effective_asof=effective_asof,
        status="unresolved_company_master",
        detail="the symbol did not resolve to a current NSE company-master identity",
    )
    kept = out.loc[~unresolved_mask].copy()
    return kept, unresolved_rows


def _build_context_hard_ohlcv_suppression_rows(
    frame: pd.DataFrame,
    *,
    effective_asof: pd.Timestamp,
    blockers_by_symbol: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    if frame.empty or not blockers_by_symbol:
        return []
    rows: list[dict[str, object]] = []
    for item in frame.to_dict(orient="records"):
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol or symbol not in blockers_by_symbol:
            continue
        source = str(item.get("context_source") or "context_overlay").strip() or "context_overlay"
        context_class = str(item.get("context_class") or "").strip()
        context_asof = pd.to_datetime(item.get("context_asof_date"), utc=True, errors="coerce")
        overlay_id = str(item.get("context_overlay_id") or f"{source}:{symbol}:{context_asof}")
        blocker = blockers_by_symbol.get(symbol) or {}
        reason = str(blocker.get("reason") or "hard_ohlcv_input_blocker").strip()
        blocker_source = str(blocker.get("blocker_source") or TECHNICAL_REFRESH_STATUS_TABLE).strip()
        watch_reason = (
            f"Suppressed {source} context-watch intake"
            + (f" for {context_class}" if context_class else "")
            + f" because the symbol has a hard technical/OHLCV input blocker: {reason.replace('_', ' ')}. "
            + "This removes context-watch entry pressure only; it has no buy, sell, portfolio, or broker authority."
        )
        rows.append(
            {
                "asof_date": effective_asof,
                "setup_id": CONTEXT_OVERLAY_SETUP_ID,
                "setup_name": CONTEXT_OVERLAY_SETUP_NAME,
                "regime_name": "LAYERED_CONTEXT",
                "base_regime": "LAYERED_CONTEXT",
                "news_overlay": source,
                "theme_ids": pd.NA,
                "symbol": symbol,
                "company_master_id": item.get("company_master_id") if not pd.isna(item.get("company_master_id")) else pd.NA,
                "screener_slug": "context-overlay-watch",
                "source_screener_slug": "context-overlay-watch",
                "source_screener_list": source,
                "rank": pd.NA,
                "candidate_state": "REJECT",
                "setup_score": 0.0,
                "watch_reason_detail": watch_reason,
                "entry_style": "context_watch_ohlcv_blocked",
                "attractive_price_low": pd.NA,
                "attractive_price_high": pd.NA,
                "invalidation_price": pd.NA,
                "entry_note": "Repair OHLCV/technical feature inputs or explicitly exclude this symbol before context can add watch pressure.",
                "near_miss_flag": False,
                "watch_enabled": False,
                "watch_reasons": json.dumps(
                    [
                        {
                            "source": source,
                            "direction": str(item.get("direction") or "watch"),
                            "context_class": context_class or None,
                            "context_overlay_id": overlay_id,
                            "context_asof_date": None if pd.isna(context_asof) else context_asof.isoformat(),
                            "pressure_score": max(0.0, min(1.0, float(item.get("pressure_score") or 0.0))),
                            "authority_scope": "watchlist_pressure_only",
                            "policy_effect": "suppress_context_hard_ohlcv_blocker_no_trade_authority",
                            "technical_input_blocker": blocker,
                            "technical_input_blocker_source": blocker_source,
                            "broker_execution_allowed": False,
                        }
                    ],
                    ensure_ascii=False,
                    default=str,
                ),
                "watch_source": "context_overlay",
                "context_source": source,
                "context_overlay_id": overlay_id,
                "context_authority_scope": "watchlist_pressure_only",
                "context_policy_effect": "suppress_context_hard_ohlcv_blocker_no_trade_authority",
                "context_reliability_classification": pd.NA,
                "context_class_reliability_classification": pd.NA,
                "context_reliability_evaluated_at": pd.NaT,
            }
        )
    return rows


def _split_context_hard_ohlcv_blocked_rows(
    frame: pd.DataFrame,
    *,
    effective_asof: pd.Timestamp,
) -> tuple[pd.DataFrame, list[dict[str, object]]]:
    if frame.empty or "symbol" not in frame.columns:
        return frame, []
    symbols = frame["symbol"].astype("string").dropna().str.strip().str.upper().drop_duplicates().tolist()
    blockers = _load_context_hard_ohlcv_blockers(symbols=symbols, asof_date=effective_asof)
    if not blockers:
        return frame, []
    normalized = frame["symbol"].astype("string").str.strip().str.upper()
    blocked_mask = normalized.isin(set(blockers))
    suppressed = _build_context_hard_ohlcv_suppression_rows(
        frame.loc[blocked_mask],
        effective_asof=effective_asof,
        blockers_by_symbol=blockers,
    )
    return frame.loc[~blocked_mask].copy(), suppressed


def load_negative_context_overlay_suppression_candidates(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
) -> pd.DataFrame:
    clean_symbols = sorted({str(value).strip().upper() for value in symbols or [] if str(value or "").strip()})
    if not clean_symbols or not WATCHLIST_CONTEXT_OVERLAY_NEGATIVE_SUPPRESSION_ENABLED:
        return pd.DataFrame()
    effective_asof = _normalize_asof_date(asof_date)
    negative_frame = _load_negative_direct_context_overlay_rows(asof_date=asof_date, symbols=clean_symbols)
    if negative_frame.empty:
        return pd.DataFrame()
    reliability = load_context_family_reliability(asof_date=effective_asof)
    reliability_by_family = reliability.get("families", {}) if isinstance(reliability.get("families"), dict) else {}
    reliability_evaluated_at = pd.to_datetime(reliability.get("evaluated_at"), utc=True, errors="coerce") if reliability else pd.NaT
    rows = _negative_context_suppression_rows(
        negative_frame,
        effective_asof=effective_asof,
        reliability_by_family=reliability_by_family,
        reliability_evaluated_at=reliability_evaluated_at,
        reason_prefix="Fresh negative context-overlay pressure for an existing watch symbol",
    )
    return pd.DataFrame(rows)


def _load_sector_context_overlay_watch_rows(
    *,
    table_name: str,
    source_name: str,
    asof_date: pd.Timestamp,
    symbols: list[str] | None,
    class_expr: str,
    reason_expr: str,
) -> pd.DataFrame:
    if not WATCHLIST_CONTEXT_OVERLAY_ENABLED or not table_exists(table_name) or not table_exists(MARKET_CONTEXT_UNIVERSE_TABLE):
        return pd.DataFrame()
    overlay_columns = table_columns(table_name)
    universe_columns = table_columns(MARKET_CONTEXT_UNIVERSE_TABLE)
    if not {"asof_date", "direction", "sector_name", "sector_code"}.issubset(overlay_columns):
        return pd.DataFrame()
    if not {"asof_date", "symbol", "sector_name", "sector_code"}.issubset(universe_columns):
        return pd.DataFrame()
    clean_symbols = sorted({str(value).strip().upper() for value in symbols or [] if str(value or "").strip()})
    symbol_clause = "AND UPPER(TRIM(u.symbol)) = ANY(%(symbols)s)" if clean_symbols else ""
    effective_asof, from_date, cutoff = _context_overlay_asof_window(asof_date)
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
        alias_match = "upper(coalesce(u.sector_code, '')) = msa.universe_sector_code"
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
        alias_match = "upper(coalesce(u.sector_code, '')) = tsa.universe_sector_code"
    try:
        return sql_to_df(
            f"""
            WITH
            {alias_cte}
            source_rows AS (
                SELECT o.*
                FROM {table_name} o
            ),
            selected_universe AS MATERIALIZED (
                SELECT DISTINCT ON (UPPER(TRIM(symbol)))
                    UPPER(TRIM(symbol)) AS symbol,
                    asof_date,
                    sector_name,
                    sector_code
                FROM {MARKET_CONTEXT_UNIVERSE_TABLE}
                WHERE asof_date < %(cutoff)s
                  AND NULLIF(TRIM(symbol), '') IS NOT NULL
                  AND NULLIF(TRIM(company_master_id), '') IS NOT NULL
                ORDER BY UPPER(TRIM(symbol)), asof_date DESC
            )
            SELECT
                %(asof_date)s::timestamptz AS asof_date,
                UPPER(TRIM(u.symbol)) AS symbol,
                o.asof_date AS context_asof_date,
                %(source_name)s::text AS context_source,
                o.overlay_id AS context_overlay_id,
                o.direction,
                o.pressure_score,
                {class_expr} AS context_class,
                o.sector_name AS overlay_sector_name,
                o.sector_code AS overlay_sector_code,
                u.sector_name AS universe_sector_name,
                u.sector_code AS universe_sector_code,
                {reason_expr} AS context_reason
            FROM source_rows o
            {alias_join}
            JOIN selected_universe u
              ON (
                regexp_replace(upper(coalesce(u.sector_name, '')), '[^A-Z0-9]', '', 'g') = regexp_replace(upper(coalesce(o.sector_name, '')), '[^A-Z0-9]', '', 'g')
                OR regexp_replace(upper(coalesce(u.sector_code, '')), '[^A-Z0-9]', '', 'g') = regexp_replace(upper(coalesce(o.sector_code, '')), '[^A-Z0-9]', '', 'g')
                OR {alias_match}
             )
            WHERE o.asof_date >= %(from_date)s
              AND o.asof_date < %(cutoff)s
              AND LOWER(TRIM(COALESCE(o.direction, ''))) IN ('positive', 'watch')
              AND COALESCE(o.production_status, 'active') = 'active'
              AND COALESCE(o.authority_scope, 'watchlist_pressure_only') = 'watchlist_pressure_only'
              {symbol_clause}
            """,
            params={
                "asof_date": effective_asof,
                "from_date": from_date,
                "cutoff": cutoff,
                "symbols": clean_symbols,
                "source_name": source_name,
            },
            retries=3,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        _record_watchlist_builder_fallback(
            fallback_type="watchlist_builder_context_overlay_load_failed",
            source=table_name,
            reason="Watchlist builder could not load sector context-overlay watch candidates.",
            error=exc,
            metadata={"source_name": source_name, "lookback_days": WATCHLIST_CONTEXT_OVERLAY_LOOKBACK_DAYS},
        )
        return pd.DataFrame()


def load_context_overlay_watch_candidates(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
) -> pd.DataFrame:
    if not WATCHLIST_CONTEXT_OVERLAY_ENABLED:
        return pd.DataFrame()
    effective_asof = _normalize_asof_date(asof_date)
    frames = [
        _load_direct_context_overlay_watch_rows(
            table_name=ANNOUNCEMENT_CONTEXT_OVERLAYS_TABLE,
            source_name="announcement_context",
            asof_date=asof_date,
            symbols=symbols,
            date_column="published_on",
            class_column="event_class",
        ),
        _load_direct_context_overlay_watch_rows(
            table_name=EXCHANGE_CONTEXT_OVERLAYS_TABLE,
            source_name="exchange_context",
            asof_date=asof_date,
            symbols=symbols,
            date_column="asof_date",
            class_column="event_type",
        ),
        _load_direct_context_overlay_watch_rows(
            table_name=BHAVCOPY_CONTEXT_OVERLAYS_TABLE,
            source_name="bhavcopy_context",
            asof_date=asof_date,
            symbols=symbols,
            date_column="asof_date",
            class_column="deal_pressure",
        ),
        _load_sector_context_overlay_watch_rows(
            table_name=THEME_CONTEXT_OVERLAYS_TABLE,
            source_name="theme_context",
            asof_date=asof_date,
            symbols=symbols,
            class_expr="o.theme_id",
            reason_expr="o.theme_reason",
        ),
        _load_sector_context_overlay_watch_rows(
            table_name=MACRO_CONTEXT_OVERLAYS_TABLE,
            source_name="macro_context",
            asof_date=asof_date,
            symbols=symbols,
            class_expr="o.macro_signal_id",
            reason_expr="o.trigger_reason",
        ),
    ]
    frames = [frame for frame in frames if isinstance(frame, pd.DataFrame) and not frame.empty]
    if not frames:
        return pd.DataFrame()
    frame = pd.concat(frames, ignore_index=True, sort=False)
    frame["symbol"] = frame["symbol"].astype("string").str.strip().str.upper()
    frame["context_asof_date"] = pd.to_datetime(frame["context_asof_date"], utc=True, errors="coerce")
    frame["pressure_score"] = pd.to_numeric(frame.get("pressure_score"), errors="coerce").fillna(0.0)
    frame["direction"] = frame["direction"].astype("string").str.strip().str.lower()
    frame = frame.dropna(subset=["symbol", "context_asof_date"])
    if frame.empty:
        return pd.DataFrame()
    suppressed_rows: list[dict[str, object]] = []
    frame, identity_suppressed_rows = _identity_unresolved_context_rows(frame, effective_asof=effective_asof)
    suppressed_rows.extend(identity_suppressed_rows)
    if frame.empty:
        return pd.DataFrame(suppressed_rows) if suppressed_rows else pd.DataFrame()
    frame, ohlcv_suppressed_rows = _split_context_hard_ohlcv_blocked_rows(frame, effective_asof=effective_asof)
    suppressed_rows.extend(ohlcv_suppressed_rows)
    if frame.empty:
        return pd.DataFrame(suppressed_rows) if suppressed_rows else pd.DataFrame()
    reliability = load_context_family_reliability(asof_date=effective_asof)
    reliability_by_family = reliability.get("families", {}) if isinstance(reliability.get("families"), dict) else {}
    reliability_evaluated_at = pd.to_datetime(reliability.get("evaluated_at"), utc=True, errors="coerce") if reliability else pd.NaT
    negative_frame = _load_negative_direct_context_overlay_rows(asof_date=asof_date, symbols=symbols)
    if not negative_frame.empty:
        negative_by_symbol = {str(row.get("symbol")): row for row in negative_frame.to_dict(orient="records") if str(row.get("symbol") or "").strip()}
        keep_indexes: list[int] = []
        for idx, item in frame.iterrows():
            symbol = str(item.get("symbol") or "").strip().upper()
            negative = negative_by_symbol.get(symbol)
            if not negative:
                keep_indexes.append(idx)
                continue
            positive_ts = pd.to_datetime(item.get("context_asof_date"), utc=True, errors="coerce")
            negative_ts = pd.to_datetime(negative.get("context_asof_date"), utc=True, errors="coerce")
            if pd.isna(positive_ts) or pd.isna(negative_ts) or negative_ts < positive_ts:
                keep_indexes.append(idx)
                continue
            suppression = _negative_context_suppression_rows(
                pd.DataFrame([negative]),
                effective_asof=effective_asof,
                reliability_by_family=reliability_by_family,
                reliability_evaluated_at=reliability_evaluated_at,
                suppressed_positive_by_symbol={symbol: item.to_dict()},
                reason_prefix=f"Suppressed {item.get('context_source') or 'context_overlay'} {item.get('direction')} watch pressure because newer negative context-overlay pressure",
            )
            if suppression:
                suppressed_rows.extend(suppression)
            else:
                keep_indexes.append(idx)
        frame = frame.loc[keep_indexes].copy()
        if frame.empty and suppressed_rows:
            return pd.DataFrame(suppressed_rows)
    if reliability_by_family:
        frame["_reliability_classification"] = frame["context_source"].map(
            lambda value: (reliability_by_family.get(str(value)) or {}).get("classification")
        )
        frame = frame[
            ~frame["_reliability_classification"].astype("string").isin(WATCHLIST_CONTEXT_OVERLAY_EXCLUDED_RELIABILITY_CLASSES)
        ].copy()
        if frame.empty:
            if suppressed_rows:
                return pd.DataFrame(suppressed_rows)
            return pd.DataFrame()
    else:
        frame["_reliability_classification"] = pd.NA
    if reliability_by_family and not frame.empty:
        keep_indexes: list[int] = []
        for idx, item in frame.iterrows():
            source = str(item.get("context_source") or "context_overlay")
            reliability_row = reliability_by_family.get(source) if isinstance(reliability_by_family, dict) else None
            blocked, context_class_reliability = _context_class_reliability_blocks_watch(
                reliability_row if isinstance(reliability_row, dict) else None,
                item.get("context_class"),
            )
            if not blocked:
                keep_indexes.append(idx)
                continue
            symbol = str(item.get("symbol") or "").strip().upper()
            context_class = str(item.get("context_class") or "").strip()
            context_asof = pd.to_datetime(item.get("context_asof_date"), utc=True, errors="coerce")
            overlay_id = str(item.get("context_overlay_id") or f"{source}:{symbol}:{context_asof}")
            score = max(0.0, min(1.0, float(item.get("pressure_score") or 0.0)))
            suppression_reason = (
                f"Suppressed {source} context-watch intake"
                + (f" for {context_class}" if context_class else "")
                + f" because the exact context class reliability is {context_class_reliability}."
                + " This removes context-watch intake only; it has no sell, portfolio, or broker authority."
            )
            suppressed_rows.append(
                {
                    "asof_date": effective_asof,
                    "setup_id": CONTEXT_OVERLAY_SETUP_ID,
                    "setup_name": CONTEXT_OVERLAY_SETUP_NAME,
                    "regime_name": "LAYERED_CONTEXT",
                    "base_regime": "LAYERED_CONTEXT",
                    "news_overlay": source,
                    "theme_ids": pd.NA,
                    "symbol": symbol,
                    "company_master_id": pd.NA,
                    "screener_slug": "context-overlay-watch",
                    "source_screener_slug": "context-overlay-watch",
                    "source_screener_list": source,
                    "rank": pd.NA,
                    "candidate_state": "REJECT",
                    "setup_score": 0.0,
                    "watch_reason_detail": suppression_reason,
                    "entry_style": "context_watch_suppressed",
                    "attractive_price_low": pd.NA,
                    "attractive_price_high": pd.NA,
                    "invalidation_price": pd.NA,
                    "entry_note": "Do not add from this context class; wait for fresh evidence and full advisory confirmation.",
                    "near_miss_flag": False,
                    "watch_enabled": False,
                    "watch_reasons": json.dumps(
                        [
                            {
                                "source": source,
                                "direction": str(item.get("direction") or "watch"),
                                "context_class": context_class or None,
                                "context_overlay_id": overlay_id,
                                "context_asof_date": None if pd.isna(context_asof) else context_asof.isoformat(),
                                "pressure_score": score,
                                "authority_scope": "watchlist_pressure_only",
                                "policy_effect": "suppress_context_class_watch_only_no_sell_authority",
                                "context_class_reliability_classification": context_class_reliability,
                                "reliability_classification": item.get("_reliability_classification") if not pd.isna(item.get("_reliability_classification")) else None,
                                "reliability_evaluated_at": None if pd.isna(reliability_evaluated_at) else reliability_evaluated_at.isoformat(),
                            }
                        ],
                        ensure_ascii=False,
                        default=str,
                    ),
                    "watch_source": "context_overlay",
                    "context_source": source,
                    "context_overlay_id": overlay_id,
                    "context_authority_scope": "watchlist_pressure_only",
                    "context_policy_effect": "suppress_context_class_watch_only_no_sell_authority",
                    "context_reliability_classification": item.get("_reliability_classification") if not pd.isna(item.get("_reliability_classification")) else pd.NA,
                    "context_class_reliability_classification": context_class_reliability or pd.NA,
                    "context_reliability_evaluated_at": reliability_evaluated_at,
                }
            )
        frame = frame.loc[keep_indexes].copy()
        if frame.empty:
            if suppressed_rows:
                return pd.DataFrame(suppressed_rows)
            return pd.DataFrame()
    priority_details = frame["context_source"].map(lambda value: _context_reliability_priority_multiplier(str(value), reliability_by_family))
    frame["_reliability_score_multiplier"] = priority_details.map(lambda value: float(value[1]) if isinstance(value, tuple) else 1.0)
    frame["_reliability_priority_reason"] = priority_details.map(lambda value: value[2] if isinstance(value, tuple) else "no_evidence_neutral")
    stale_ohlcv_blockers = _load_context_stale_ohlcv_blockers(
        symbols=frame["symbol"].astype("string").dropna().str.strip().str.upper().drop_duplicates().tolist(),
        asof_date=effective_asof,
    )
    technical_actionability = _load_context_technical_actionability(
        symbols=frame["symbol"].astype("string").dropna().str.strip().str.upper().drop_duplicates().tolist(),
        asof_date=effective_asof,
    )
    if reliability_by_family:
        sector_details = frame.apply(
            lambda row: _context_sector_reliability_classification(
                reliability_by_family.get(str(row.get("context_source") or "")) if isinstance(reliability_by_family, dict) else None,
                sector_name=_first_nonempty_text(row.get("universe_sector_name"), row.get("overlay_sector_name"), row.get("sector_name")),
                sector_code=_first_nonempty_text(row.get("universe_sector_code"), row.get("overlay_sector_code"), row.get("sector_code")),
            ),
            axis=1,
        )
        frame["_context_sector_reliability_classification"] = sector_details
        frame["_context_sector_blocks_watch_priority"] = frame["_context_sector_reliability_classification"].map(
            _sector_reliability_blocks_watch_priority
        )
        blocked_mask = frame["_context_sector_blocks_watch_priority"].fillna(False).astype(bool)
        frame.loc[blocked_mask, "_reliability_score_multiplier"] = 1.0
        frame.loc[blocked_mask, "_reliability_priority_reason"] = "sector_reliability_blocks_watch_priority"
    else:
        frame["_context_sector_reliability_classification"] = pd.NA
        frame["_context_sector_blocks_watch_priority"] = False
    frame["_raw_pressure_score"] = frame["pressure_score"].astype(float)
    frame["_technical_actionability"] = frame["symbol"].map(lambda value: technical_actionability.get(str(value or "").strip().upper(), {}))
    frame["_stale_ohlcv_blocker"] = frame["symbol"].map(lambda value: stale_ohlcv_blockers.get(str(value or "").strip().upper(), {}))
    frame["_stale_ohlcv_partial_blocker"] = frame["_stale_ohlcv_blocker"].map(lambda value: isinstance(value, dict) and bool(value))
    frame["_technical_actionability_score"] = frame["_technical_actionability"].map(
        lambda value: float(value.get("technical_actionability_score") or 0.0) if isinstance(value, dict) else 0.0
    )
    frame.loc[frame["_stale_ohlcv_partial_blocker"], "_technical_actionability_score"] = 0.0
    frame["_technical_priority_bonus"] = (
        frame["_technical_actionability_score"] * max(0.0, float(WATCHLIST_CONTEXT_OVERLAY_TECHNICAL_PRIORITY_MAX_BONUS))
    )
    frame["_watch_priority_score"] = (
        (frame["_raw_pressure_score"] * frame["_reliability_score_multiplier"]) + frame["_technical_priority_bonus"]
    ).clip(lower=0.0, upper=1.0)
    frame["_direction_rank"] = frame["direction"].map({"positive": 0, "watch": 1}).fillna(2)
    frame = frame.sort_values(
        ["symbol", "_direction_rank", "_watch_priority_score", "context_asof_date"],
        ascending=[True, True, False, False],
        kind="mergesort",
    )
    frame = frame.drop_duplicates(subset=["symbol"], keep="first")
    frame = frame.sort_values(
        ["_watch_priority_score", "_technical_actionability_score", "_direction_rank", "context_asof_date", "_raw_pressure_score", "symbol"],
        ascending=[False, False, True, False, False, True],
        kind="mergesort",
    ).head(max(1, int(WATCHLIST_CONTEXT_OVERLAY_LIMIT)))
    rows: list[dict[str, object]] = []
    for rank, item in enumerate(frame.to_dict(orient="records"), start=1):
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        source = str(item.get("context_source") or "context_overlay")
        direction = str(item.get("direction") or "watch")
        context_class = str(item.get("context_class") or "").strip()
        reason = str(item.get("context_reason") or "").strip()
        overlay_id = str(item.get("context_overlay_id") or f"{source}:{symbol}:{item.get('context_asof_date')}")
        raw_score = max(0.0, min(1.0, float(item.get("_raw_pressure_score") or item.get("pressure_score") or 0.0)))
        score = max(0.0, min(1.0, float(item.get("_watch_priority_score") or raw_score)))
        technical_hint = item.get("_technical_actionability") if isinstance(item.get("_technical_actionability"), dict) else {}
        technical_score = float(technical_hint.get("technical_actionability_score") or 0.0) if isinstance(technical_hint, dict) else 0.0
        technical_bonus = float(item.get("_technical_priority_bonus") or 0.0)
        stale_ohlcv_blocker = item.get("_stale_ohlcv_blocker") if isinstance(item.get("_stale_ohlcv_blocker"), dict) else {}
        has_stale_ohlcv_blocker = bool(stale_ohlcv_blocker)
        if has_stale_ohlcv_blocker:
            technical_score = 0.0
        reliability_multiplier = float(item.get("_reliability_score_multiplier") or 1.0)
        reliability_priority_reason = str(item.get("_reliability_priority_reason") or "no_evidence_neutral")
        reliability_classification = item.get("_reliability_classification")
        reliability_label = "" if pd.isna(reliability_classification) else str(reliability_classification)
        reliability_row = reliability_by_family.get(source) if isinstance(reliability_by_family, dict) else None
        context_class_reliability = _context_class_reliability_classification(
            reliability_row if isinstance(reliability_row, dict) else None,
            context_class,
        )
        context_class_blocks_breakout = bool(context_class_reliability and context_class_reliability != "candidate_helpful")
        context_sector_reliability = item.get("_context_sector_reliability_classification")
        context_sector_reliability = "" if pd.isna(context_sector_reliability) else str(context_sector_reliability)
        context_sector_blocks_breakout = _sector_reliability_blocks_watch_priority(context_sector_reliability or None)
        breakout_blocked, breakout_state_classification = _watch_breakout_reliability_blocks_promotion(
            reliability_row if isinstance(reliability_row, dict) else None
        )
        watch_priority_allowed = _runtime_contract_allows(
            reliability_row if isinstance(reliability_row, dict) else None,
            "watch_priority",
            legacy_classification=reliability_label,
        )
        promoted_to_breakout_watch = bool(
            direction == "positive"
            and watch_priority_allowed
            and not context_class_blocks_breakout
            and not context_sector_blocks_breakout
            and not breakout_blocked
            and score >= float(WATCHLIST_CONTEXT_OVERLAY_BREAKOUT_SCORE_THRESHOLD)
        )
        candidate_state = "WATCH_BREAKOUT" if promoted_to_breakout_watch else "WATCH_EVENT"
        policy_effect = (
            "watch_breakout_priority_no_buy_authority"
            if promoted_to_breakout_watch
            else "watch_only_no_buy_authority"
        )
        technical_confirmation_plan = _technical_confirmation_plan_for_context_watch(
            candidate_state=candidate_state,
            policy_effect=policy_effect,
            context_source=source,
            context_class=context_class or None,
        )
        watch_reason = (
            f"{source} {direction} pressure"
            + (f" ({context_class})" if context_class else "")
            + (f": {reason}" if reason else "")
            + (f" Reliability: {reliability_label}." if reliability_label else "")
            + (
                f" Watch priority score {round(score, 4)} from raw pressure {round(raw_score, 4)} "
                f"and reliability multiplier {round(reliability_multiplier, 4)}."
            )
            + (
                f" Technical actionability score {round(technical_score, 4)} added ranking bonus {round(technical_bonus, 4)}."
                if technical_score > 0
                else ""
            )
            + (
                " Technical priority bonus suppressed because Dhan daily OHLCV has stale-history sync failures; keep visible as watch-only until data is fresh."
                if has_stale_ohlcv_blocker
                else ""
            )
            + (
                f" Classified as WATCH_BREAKOUT because candidate-helpful reliability and score >= "
                f"{round(float(WATCHLIST_CONTEXT_OVERLAY_BREAKOUT_SCORE_THRESHOLD), 4)}."
                if promoted_to_breakout_watch
                else ""
            )
            + (
                f" WATCH_BREAKOUT promotion blocked because exact context class reliability is {context_class_reliability}."
                if context_class_blocks_breakout
                else ""
            )
            + (
                f" WATCH_BREAKOUT promotion blocked because sector reliability is {context_sector_reliability}."
                if context_sector_blocks_breakout
                else ""
            )
            + (
                f" WATCH_BREAKOUT promotion blocked because its own reliability split is {breakout_state_classification}."
                if breakout_blocked
                else ""
            )
            + f" Confirmation plan: {technical_confirmation_plan['operator_summary']}"
            + " This row can add the stock to watch, but it has no buy or broker authority."
        )
        rows.append(
            {
                "asof_date": effective_asof,
                "setup_id": CONTEXT_OVERLAY_SETUP_ID,
                "setup_name": CONTEXT_OVERLAY_SETUP_NAME,
                "regime_name": "LAYERED_CONTEXT",
                "base_regime": "LAYERED_CONTEXT",
                "news_overlay": source,
                "theme_ids": pd.NA,
                "symbol": symbol,
                "company_master_id": item.get("company_master_id") if not pd.isna(item.get("company_master_id")) else pd.NA,
                "screener_slug": "context-overlay-watch",
                "source_screener_slug": "context-overlay-watch",
                "source_screener_list": source,
                "rank": rank,
                "candidate_state": candidate_state,
                "setup_score": score,
                "watch_reason_detail": watch_reason,
                "entry_style": "context_watch_only",
                "attractive_price_low": pd.NA,
                "attractive_price_high": pd.NA,
                "invalidation_price": pd.NA,
                "entry_note": str(technical_confirmation_plan["operator_summary"]),
                "near_miss_flag": False,
                "watch_enabled": True,
                "watch_reasons": json.dumps(
                    [
                        {
                            "source": source,
                            "direction": direction,
                            "context_class": context_class or None,
                            "context_overlay_id": overlay_id,
                            "context_asof_date": str(item.get("context_asof_date")),
                            "pressure_score": raw_score,
                            "watch_priority_score": score,
                            "reliability_score_multiplier": reliability_multiplier,
                            "reliability_priority_reason": reliability_priority_reason,
                            "technical_actionability_score": technical_score,
                            "technical_priority_bonus": technical_bonus,
                            "technical_actionability": _json_safe(technical_hint),
                            "technical_input_blocker": _json_safe(stale_ohlcv_blocker) if has_stale_ohlcv_blocker else None,
                            "technical_input_blocker_policy_effect": "stale_history_watch_only_no_fresh_entry_authority" if has_stale_ohlcv_blocker else None,
                            "technical_fresh_entry_allowed": not has_stale_ohlcv_blocker,
                            "context_watchlist_builder_version": CONTEXT_OVERLAY_WATCHLIST_BUILDER_VERSION,
                            "identity_validation_status": item.get("context_identity_validation_status") or "resolved_company_master",
                            "company_master_id": item.get("company_master_id") if not pd.isna(item.get("company_master_id")) else None,
                            "authority_scope": "watchlist_pressure_only",
                            "policy_effect": policy_effect,
                            "candidate_state": candidate_state,
                            "breakout_watch_threshold": float(WATCHLIST_CONTEXT_OVERLAY_BREAKOUT_SCORE_THRESHOLD),
                            "reliability_classification": reliability_label or None,
                            "runtime_policy_contract": _runtime_policy_contract_for_reliability(
                                reliability_row if isinstance(reliability_row, dict) else None
                            ),
                            "watch_priority_allowed_by_runtime_contract": bool(watch_priority_allowed),
                            "context_class_reliability_classification": context_class_reliability,
                            "context_class_blocks_breakout": bool(context_class_blocks_breakout),
                            "context_sector_reliability_classification": context_sector_reliability or None,
                            "context_sector_blocks_breakout": bool(context_sector_blocks_breakout),
                            "watch_breakout_reliability_classification": breakout_state_classification,
                            "watch_breakout_priority_blocked": breakout_blocked,
                            "technical_confirmation_plan": technical_confirmation_plan,
                            "reliability_evaluated_at": None if pd.isna(reliability_evaluated_at) else reliability_evaluated_at.isoformat(),
                        }
                    ],
                    ensure_ascii=False,
                    default=str,
                ),
                "watch_source": "context_overlay",
                "context_source": source,
                "context_overlay_id": overlay_id,
                "context_authority_scope": "watchlist_pressure_only",
                "context_policy_effect": policy_effect,
                "context_reliability_classification": reliability_label or pd.NA,
                "context_class_reliability_classification": context_class_reliability or pd.NA,
                "context_reliability_evaluated_at": reliability_evaluated_at,
            }
        )
    if suppressed_rows:
        rows.extend(suppressed_rows)
    return pd.DataFrame(rows)


def load_latest_event_transitions(
    *,
    asof_date: pd.Timestamp | None = None,
    setup_ids: list[str] | None = None,
    symbols: list[str] | None = None,
) -> pd.DataFrame:
    try:
        clauses = ["1 = 1"]
        params: list[object] = []
        if asof_date is not None:
            clauses.append("asof_date = %s")
            params.append(asof_date)
        else:
            clauses.append("asof_date = (SELECT MAX(asof_date) FROM advisory_event_evaluations)")
        if setup_ids:
            clauses.append("setup_id = ANY(%s)")
            params.append([value.upper() for value in setup_ids])
        if symbols:
            clauses.append("symbol = ANY(%s)")
            params.append([value.upper() for value in symbols])
        df = sql_to_df(
            f"""
            SELECT
                asof_date,
                setup_id,
                symbol,
                published_on,
                event_class,
                state_transition_hint,
                score_impact
            FROM advisory_event_evaluations
            WHERE {' AND '.join(clauses)}
            ORDER BY setup_id, symbol, published_on, load_ts NULLS LAST
            """,
            params=tuple(params) if params else None,
        )
    except Exception as exc:
        _record_watchlist_builder_fallback(
            fallback_type="watchlist_builder_event_transition_load_failed",
            source="advisory_event_evaluations",
            reason="Watchlist builder could not load event transition context; watch states may ignore fresh event upgrades/downgrades.",
            error=exc,
            metadata={
                "asof_date": str(asof_date) if asof_date is not None else None,
                "setup_count": len(setup_ids or []),
                "symbol_count": len(symbols or []),
            },
        )
        return pd.DataFrame()
    if df.empty:
        return df
    df["asof_date"] = normalize_timestamp(df["asof_date"])
    df["published_on"] = pd.to_datetime(df["published_on"], utc=True, errors="coerce")
    df["symbol"] = df["symbol"].astype("string").str.upper()
    df["score_impact"] = pd.to_numeric(df["score_impact"], errors="coerce").fillna(0.0)

    rows: list[dict[str, object]] = []
    for (asof_key, setup_id, symbol), group in df.groupby(["asof_date", "setup_id", "symbol"], dropna=False, sort=False):
        group = group.sort_values(["published_on"], ascending=[True], kind="stable")
        latest = group.iloc[-1]
        hints = {str(value).upper() for value in group["state_transition_hint"].dropna().astype(str)}
        total_score_impact = round(max(-0.35, min(0.35, float(group["score_impact"].sum()))), 4)
        if "DOWNGRADE_TO_REJECT" in hints:
            transition_hint = "DOWNGRADE_TO_REJECT"
        elif "UPGRADE_TO_PASS_NOW" in hints:
            transition_hint = "UPGRADE_TO_PASS_NOW"
        elif total_score_impact >= _TRANSITION_CUTOFF:
            transition_hint = "RAISE_SCORE_ONLY"
        elif total_score_impact <= -_TRANSITION_CUTOFF:
            transition_hint = "CUT_SCORE_ONLY"
        elif "REVIEW_MANUAL" in hints:
            transition_hint = "REVIEW_MANUAL"
        else:
            transition_hint = "NO_CHANGE"
        rows.append(
            {
                "asof_date": asof_key,
                "setup_id": setup_id,
                "symbol": symbol,
                "published_on": latest["published_on"],
                "event_class": latest.get("event_class"),
                "state_transition_hint": transition_hint,
                "score_impact": total_score_impact,
                "has_review_manual": "REVIEW_MANUAL" in hints,
            }
        )
    return pd.DataFrame(rows)


def load_setup_thresholds() -> dict[str, dict[str, float]]:
    thresholds: dict[str, dict[str, float]] = {}
    for setup in load_setup_registry():
        setup_id = str(setup.get("setup_id") or "").upper()
        if not setup_id:
            continue
        raw = {**DEFAULT_SCORE_THRESHOLDS, **(setup.get("score_thresholds") or {})}
        thresholds[setup_id] = {key: float(value) for key, value in raw.items() if value is not None}
    return thresholds


def derive_current_state(
    candidate_state: str,
    transition_hint: str | None,
    *,
    setup_score: float | None = None,
    score_impact: float | None = None,
    thresholds: dict[str, float] | None = None,
) -> str:
    state = str(candidate_state or "WATCH_EVENT").upper()
    hint = "" if pd.isna(transition_hint) else str(transition_hint).upper()
    if state == "ABSTAIN":
        return "ABSTAIN"
    if hint == "UPGRADE_TO_PASS_NOW":
        return "PASS_NOW"
    if hint == "DOWNGRADE_TO_REJECT":
        return "REJECT"
    if hint == "RAISE_SCORE_ONLY" and setup_score is not None and score_impact is not None and thresholds:
        adjusted_score = float(setup_score) + float(score_impact)
        if adjusted_score >= float(thresholds.get("pass_now", DEFAULT_SCORE_THRESHOLDS["pass_now"])):
            return "PASS_NOW"
        if adjusted_score >= float(thresholds.get("watch_breakout", DEFAULT_SCORE_THRESHOLDS["watch_breakout"])):
            return "WATCH_BREAKOUT"
    if hint == "CUT_SCORE_ONLY" and setup_score is not None and score_impact is not None and thresholds:
        adjusted_score = float(setup_score) + float(score_impact)
        if adjusted_score < float(thresholds.get("watch_event", DEFAULT_SCORE_THRESHOLDS["watch_event"])):
            return "REJECT"
        if adjusted_score < float(thresholds.get("watch_breakout", DEFAULT_SCORE_THRESHOLDS["watch_breakout"])) and state == "PASS_NOW":
            return "WATCH_EVENT"
    return state


def build_watchlist(
    *,
    asof_date: pd.Timestamp | None = None,
    setup_ids: list[str] | None = None,
    symbols: list[str] | None = None,
) -> pd.DataFrame:
    candidates = load_candidate_rows(asof_date=asof_date, setup_ids=setup_ids, symbols=symbols)
    include_context_candidates = not setup_ids or CONTEXT_OVERLAY_SETUP_ID in {str(value or "").strip().upper() for value in setup_ids}
    existing_context_symbols = load_existing_watch_symbols() if include_context_candidates and not symbols else []
    context_candidates = (
        load_context_overlay_watch_candidates(asof_date=asof_date, symbols=symbols)
        if include_context_candidates
        else pd.DataFrame()
    )
    negative_context_candidates = (
        load_negative_context_overlay_suppression_candidates(asof_date=asof_date, symbols=symbols or existing_context_symbols)
        if include_context_candidates
        else pd.DataFrame()
    )
    if not negative_context_candidates.empty:
        if not context_candidates.empty and "symbol" in context_candidates.columns:
            context_symbols = {
                str(value).strip().upper()
                for value in context_candidates["symbol"].dropna().tolist()
                if str(value or "").strip()
            }
            negative_context_candidates = negative_context_candidates[
                ~negative_context_candidates["symbol"].astype("string").str.strip().str.upper().isin(context_symbols)
            ].copy()
        if not negative_context_candidates.empty:
            context_candidates = (
                negative_context_candidates
                if context_candidates.empty
                else pd.concat([context_candidates, negative_context_candidates], ignore_index=True, sort=False)
            )
    if candidates.empty and context_candidates.empty:
        return pd.DataFrame()
    if candidates.empty:
        candidates = context_candidates
    elif not context_candidates.empty:
        candidates = pd.concat([candidates, context_candidates], ignore_index=True, sort=False)
    candidates = candidates.drop_duplicates(subset=["asof_date", "setup_id", "symbol"], keep="last").reset_index(drop=True)
    thresholds_by_setup = load_setup_thresholds()
    event_transitions = load_latest_event_transitions(asof_date=asof_date, setup_ids=setup_ids, symbols=symbols)

    try:
        existing = sql_to_df(
            """
            SELECT asof_date, setup_id, symbol, last_checked_at, last_document_published_on, watch_status, current_state
            FROM advisory_watchlist
            """
        )
    except Exception as exc:
        _record_watchlist_builder_fallback(
            fallback_type="watchlist_builder_existing_state_load_failed",
            source=TABLE_NAME,
            reason="Watchlist builder could not load existing watch state; last-checked/document timestamps may reset for this build.",
            error=exc,
            metadata={
                "asof_date": str(asof_date) if asof_date is not None else None,
                "candidate_count": int(len(candidates)),
            },
        )
        existing = pd.DataFrame()
    if not existing.empty:
        existing["asof_date"] = normalize_timestamp(existing["asof_date"])
        existing["symbol"] = existing["symbol"].astype("string").str.upper()
        existing = existing[
            existing.set_index(["asof_date", "setup_id", "symbol"]).index.isin(
                candidates.set_index(["asof_date", "setup_id", "symbol"]).index
            )
        ]

    out = candidates.copy()
    if not event_transitions.empty:
        if "has_review_manual" not in event_transitions.columns:
            event_transitions = event_transitions.copy()
            event_transitions["has_review_manual"] = False
        out = out.merge(
            event_transitions[["setup_id", "symbol", "event_class", "state_transition_hint", "score_impact", "has_review_manual"]],
            on=["setup_id", "symbol"],
            how="left",
        )
    else:
        out["event_class"] = pd.NA
        out["state_transition_hint"] = pd.NA
        out["score_impact"] = pd.NA
        out["has_review_manual"] = False
    out["current_state"] = out.apply(
        lambda row: derive_current_state(
            str(row.get("candidate_state") or ""),
            row.get("state_transition_hint"),
            setup_score=pd.to_numeric(row.get("setup_score"), errors="coerce"),
            score_impact=pd.to_numeric(row.get("score_impact"), errors="coerce"),
            thresholds=thresholds_by_setup.get(str(row.get("setup_id") or "").upper(), DEFAULT_SCORE_THRESHOLDS),
        ),
        axis=1,
    )
    out["watch_reason_detail"] = out.apply(
        lambda row: (
            f"{str(row.get('watch_reason_detail') or '').strip()}; event {row.get('event_class')} -> {row.get('state_transition_hint')}"
            if pd.notna(row.get("event_class")) and pd.notna(row.get("state_transition_hint")) and str(row.get("watch_reason_detail") or "").strip()
            else (
                f"event {row.get('event_class')} -> {row.get('state_transition_hint')}"
                if pd.notna(row.get("event_class")) and pd.notna(row.get("state_transition_hint"))
                else row.get("watch_reason_detail")
            )
        ),
        axis=1,
    )
    out["watch_enabled"] = ~out["current_state"].isin(["REJECT", "ABSTAIN"])
    out["watch_reasons_json"] = out["watch_reasons"].astype("string")
    out["watch_status"] = out["current_state"].map(
        lambda value: "rejected" if value == "REJECT" else ("abstained" if value == "ABSTAIN" else "active")
    )
    out["has_review_manual"] = out["has_review_manual"].fillna(False).astype(bool)
    out.loc[out["has_review_manual"], "watch_status"] = "review_manual"
    out["last_event_class"] = out["event_class"]
    out["last_state_transition_hint"] = out["state_transition_hint"]
    out["last_event_score_impact"] = out["score_impact"]
    out["state_updated_at"] = pd.Timestamp.utcnow()
    out["watch_started_at"] = pd.Timestamp.utcnow()
    out["last_checked_at"] = pd.NaT
    out["last_document_published_on"] = pd.NaT
    out["load_ts"] = pd.Timestamp.utcnow()
    for column in ["base_regime", "news_overlay", "theme_ids", "source_screener_slug", "source_screener_list"]:
        if column not in out.columns:
            out[column] = pd.NA
    for column in [
        "watch_source",
        "context_source",
        "context_overlay_id",
        "context_authority_scope",
        "context_policy_effect",
        "context_reliability_classification",
        "context_class_reliability_classification",
        "context_reliability_evaluated_at",
    ]:
        if column not in out.columns:
            out[column] = pd.NA
    out["watch_source"] = out["watch_source"].fillna("candidate")
    out["context_authority_scope"] = out["context_authority_scope"].where(out["watch_source"].eq("context_overlay"), pd.NA)
    out["context_policy_effect"] = out["context_policy_effect"].where(out["watch_source"].eq("context_overlay"), pd.NA)
    out["context_class_reliability_classification"] = out["context_class_reliability_classification"].where(
        out["watch_source"].eq("context_overlay"),
        pd.NA,
    )
    out["context_reliability_evaluated_at"] = pd.to_datetime(out["context_reliability_evaluated_at"], utc=True, errors="coerce")

    if not existing.empty:
        out = out.merge(
            existing,
            on=["asof_date", "setup_id", "symbol"],
            how="left",
            suffixes=("", "_existing"),
        )
        existing_watch_status = out["watch_status_existing"]
        preserve_existing_status = existing_watch_status.notna() & ~out["current_state"].isin(["REJECT", "ABSTAIN"])
        out.loc[preserve_existing_status, "watch_status"] = existing_watch_status.loc[preserve_existing_status]
        out.loc[out["current_state"].eq("REJECT"), "watch_status"] = "rejected"
        out.loc[out["current_state"].eq("ABSTAIN"), "watch_status"] = "abstained"
        out["current_state"] = out["current_state"].fillna(out["current_state_existing"])
        out["last_checked_at"] = pd.to_datetime(out["last_checked_at_existing"], utc=True, errors="coerce")
        out["last_document_published_on"] = pd.to_datetime(
            out["last_document_published_on_existing"], utc=True, errors="coerce"
        )
        out = out.drop(
            columns=[
                "watch_status_existing",
                "current_state_existing",
                "last_checked_at_existing",
                "last_document_published_on_existing",
            ],
            errors="ignore",
        )

    out["last_event_score_impact"] = pd.to_numeric(out["last_event_score_impact"], errors="coerce")
    for column in ["attractive_price_low", "attractive_price_high", "invalidation_price"]:
        out[column] = pd.to_numeric(out[column], errors="coerce")
    for column in ["state_updated_at", "watch_started_at", "last_checked_at", "last_document_published_on", "load_ts"]:
        out[column] = pd.to_datetime(out[column], utc=True, errors="coerce")

    ordered_cols = [
        "asof_date",
        "setup_id",
        "setup_name",
        "regime_name",
        "base_regime",
        "news_overlay",
        "theme_ids",
        "symbol",
        "company_master_id",
        "screener_slug",
        "source_screener_slug",
        "source_screener_list",
        "rank",
        "candidate_state",
        "current_state",
        "watch_reason_detail",
        "entry_style",
        "attractive_price_low",
        "attractive_price_high",
        "invalidation_price",
        "entry_note",
        "near_miss_flag",
        "last_event_class",
        "last_state_transition_hint",
        "last_event_score_impact",
        "watch_enabled",
        "watch_reasons_json",
        "watch_status",
        "state_updated_at",
        "watch_started_at",
        "last_checked_at",
        "last_document_published_on",
        "watch_source",
        "context_source",
        "context_overlay_id",
        "context_authority_scope",
        "context_policy_effect",
        "context_reliability_classification",
        "context_class_reliability_classification",
        "context_reliability_evaluated_at",
        "load_ts",
    ]
    return out[ordered_cols].drop_duplicates(subset=["asof_date", "setup_id", "symbol"], keep="last")


def persist_watchlist(df: pd.DataFrame, *, rebuild: bool = False, asof_date: pd.Timestamp | None = None) -> None:
    ensure_watchlist_table()
    if df.empty:
        return
    if rebuild and asof_date is not None:
        def _delete_existing_watchlist_rows() -> None:
            pairs = (
                df[["asof_date", "setup_id"]]
                .dropna()
                .drop_duplicates()
                .to_dict(orient="records")
            )
            with db_session() as (_, cur):
                for item in pairs:
                    cur.execute(
                        f"DELETE FROM {TABLE_NAME} WHERE asof_date = %s AND setup_id = %s",
                        (
                            pd.to_datetime(item["asof_date"], utc=True, errors="coerce").to_pydatetime(),
                            str(item["setup_id"]),
                        ),
                    )

        execute_db_operation(
            _delete_existing_watchlist_rows,
            operation_name="watchlist_builder:delete_rebuild_rows",
        )
    upsert_to_db(
        df,
        TABLE_NAME,
        unique_keys=["asof_date", "setup_id", "symbol"],
        timescaledb_column="asof_date",
    )


def inspect_context_overlay_watchlist_current_state(asof_date: pd.Timestamp | None = None) -> dict[str, object]:
    effective_asof = _normalize_asof_date(asof_date)
    if not table_exists(TABLE_NAME):
        return {
            "status": "missing_watchlist_table",
            "current_for_reconcile": False,
            "asof_date": effective_asof.isoformat(),
        }
    columns = table_columns(TABLE_NAME)
    has_watch_reasons = "watch_reasons_json" in columns
    confirmation_plan_expr = (
        f"COALESCE(watch_reasons_json, '') LIKE '%%\"{CONTEXT_OVERLAY_CONFIRMATION_PLAN_KEY}\"%%'"
        if has_watch_reasons
        else "FALSE"
    )
    builder_version_expr = (
        f"COALESCE(watch_reasons_json, '') LIKE '%%\"context_watchlist_builder_version\": \"{CONTEXT_OVERLAY_WATCHLIST_BUILDER_VERSION}\"%%'"
        if has_watch_reasons
        else "FALSE"
    )
    try:
        row = sql_to_df(
            f"""
            SELECT
                COUNT(*) AS row_count,
                SUM(
                    CASE
                        WHEN COALESCE(watch_enabled, false) IS TRUE
                         AND UPPER(TRIM(COALESCE(current_state, ''))) NOT IN ('REJECT', 'ABSTAIN')
                        THEN 1 ELSE 0
                    END
                ) AS active_watch_count,
                SUM(
                    CASE
                        WHEN COALESCE(watch_enabled, false) IS TRUE
                         AND UPPER(TRIM(COALESCE(current_state, ''))) NOT IN ('REJECT', 'ABSTAIN')
                         AND {confirmation_plan_expr}
                        THEN 1 ELSE 0
                    END
                ) AS active_confirmation_plan_count,
                SUM(
                    CASE
                        WHEN COALESCE(watch_enabled, false) IS TRUE
                         AND UPPER(TRIM(COALESCE(current_state, ''))) NOT IN ('REJECT', 'ABSTAIN')
                         AND {builder_version_expr}
                        THEN 1 ELSE 0
                    END
                ) AS active_builder_version_count,
                MAX(load_ts) AS latest_load_ts
            FROM {TABLE_NAME}
            WHERE asof_date = %(asof_date)s
              AND setup_id = %(setup_id)s
            """,
            params={"asof_date": effective_asof, "setup_id": CONTEXT_OVERLAY_SETUP_ID},
            retries=3,
            statement_timeout_ms=5000,
        )
    except Exception as exc:
        _record_watchlist_builder_fallback(
            fallback_type="watchlist_builder_context_current_state_load_failed",
            source=TABLE_NAME,
            reason="Watchlist builder could not inspect current CONTEXT_OVERLAY_WATCH rows before skip-if-current guard.",
            error=exc,
            metadata={"asof_date": str(effective_asof), "setup_id": CONTEXT_OVERLAY_SETUP_ID},
        )
        return {
            "status": "current_state_load_failed",
            "current_for_reconcile": False,
            "asof_date": effective_asof.isoformat(),
            "error_type": exc.__class__.__name__,
        }
    record = row.iloc[0].to_dict() if not row.empty else {}
    latest_load_ts = pd.to_datetime(record.get("latest_load_ts"), utc=True, errors="coerce")
    latest_load_ts = None if pd.isna(latest_load_ts) else latest_load_ts
    try:
        sync_row = load_sync_state(CONTEXT_OVERLAY_SIGNAL_REFRESH_SYNC_SOURCE) or {}
    except Exception as exc:
        _record_watchlist_builder_fallback(
            fallback_type="watchlist_builder_context_signal_sync_state_load_failed",
            source=CONTEXT_OVERLAY_SIGNAL_REFRESH_SYNC_SOURCE,
            reason="Watchlist builder could not inspect context signal-refresh sync state before skip-if-current guard.",
            error=exc,
            metadata={"asof_date": str(effective_asof)},
        )
        sync_row = {}
    sync_state = sync_row.get("state") if isinstance(sync_row.get("state"), dict) else {}
    sync_diagnostics = sync_state.get("diagnostics") if isinstance(sync_state.get("diagnostics"), dict) else {}
    sync_status = str(sync_row.get("status") or "").strip().lower() if sync_row else ""
    sync_asof = pd.to_datetime(sync_state.get("asof_date") or sync_diagnostics.get("asof_date"), utc=True, errors="coerce")
    sync_asof = None if pd.isna(sync_asof) else sync_asof.normalize()
    sync_last_success_at = pd.to_datetime(sync_row.get("last_success_at"), utc=True, errors="coerce") if sync_row else pd.NaT
    sync_last_success_at = None if pd.isna(sync_last_success_at) else sync_last_success_at
    row_count = int(record.get("row_count") or 0)
    active_watch_count = int(record.get("active_watch_count") or 0)
    active_confirmation_plan_count = int(record.get("active_confirmation_plan_count") or 0)
    active_builder_version_count = int(record.get("active_builder_version_count") or 0)
    active_missing_confirmation_plan_count = max(0, active_watch_count - active_confirmation_plan_count)
    active_stale_builder_version_count = max(0, active_watch_count - active_builder_version_count)
    confirmation_plan_coverage_current = bool(
        has_watch_reasons
        and active_watch_count > 0
        and active_missing_confirmation_plan_count == 0
    )
    builder_version_current = bool(
        has_watch_reasons
        and active_watch_count > 0
        and active_stale_builder_version_count == 0
    )
    sync_state_available = bool(sync_row) and sync_status == "ok"
    sync_asof_matches = sync_asof is not None and sync_asof == effective_asof
    watchlist_fresh_for_signal_refresh = (
        latest_load_ts is not None
        and sync_last_success_at is not None
        and latest_load_ts >= sync_last_success_at
    )
    technical_hard_blocker_count = 0
    technical_hard_blocker_sample: list[str] = []
    technical_hard_blocker_latest_ts = None
    technical_status_after_watchlist_count = 0
    technical_status_latest_ts = None
    if latest_load_ts is not None and table_exists(TECHNICAL_REFRESH_STATUS_TABLE):
        try:
            blocker_df = sql_to_df(
                f"""
                WITH active_watch AS (
                    SELECT DISTINCT UPPER(TRIM(symbol)) AS symbol
                    FROM {TABLE_NAME}
                    WHERE asof_date = %(asof_date)s
                      AND setup_id = %(setup_id)s
                      AND COALESCE(watch_enabled, false) IS TRUE
                      AND UPPER(TRIM(COALESCE(current_state, ''))) NOT IN ('REJECT', 'ABSTAIN')
                      AND symbol IS NOT NULL
                ),
                latest_status AS (
                    SELECT DISTINCT ON (UPPER(TRIM(s.symbol)), LOWER(TRIM(COALESCE(s.stage, ''))))
                        UPPER(TRIM(s.symbol)) AS symbol,
                        LOWER(TRIM(COALESCE(s.stage, ''))) AS stage,
                        COALESCE(s.reason, '') AS reason,
                        s.load_ts
                    FROM {TECHNICAL_REFRESH_STATUS_TABLE} s
                    JOIN active_watch a
                      ON UPPER(TRIM(s.symbol)) = a.symbol
                    WHERE s.asof_date <= %(asof_date)s
                    ORDER BY UPPER(TRIM(s.symbol)), LOWER(TRIM(COALESCE(s.stage, ''))), s.load_ts DESC NULLS LAST
                )
                SELECT
                    COUNT(DISTINCT symbol) AS hard_blocker_count,
                    MAX(load_ts) AS latest_blocker_load_ts,
                    ARRAY_AGG(DISTINCT symbol ORDER BY symbol) AS hard_blocker_symbols
                FROM latest_status
                WHERE load_ts > %(latest_load_ts)s
                  AND (
                    (
                      stage = 'ohlcv'
                      AND reason IN ('dhan_daily_sync_failed_no_history', 'ohlcv_sync_zero_rows', 'ohlcv_no_new_data')
                    )
                    OR (
                      stage = 'technical_build'
                      AND reason = 'technical_rows_missing_after_build'
                    )
                  )
                """,
                params={"asof_date": effective_asof, "setup_id": CONTEXT_OVERLAY_SETUP_ID, "latest_load_ts": latest_load_ts},
                retries=3,
                statement_timeout_ms=5000,
            )
            if not blocker_df.empty:
                first = blocker_df.iloc[0]
                technical_hard_blocker_count = int(first.get("hard_blocker_count") or 0)
                symbols_value = first.get("hard_blocker_symbols")
                if isinstance(symbols_value, (list, tuple)):
                    technical_hard_blocker_sample = [str(symbol).strip().upper() for symbol in symbols_value[:10] if str(symbol).strip()]
                blocker_ts = pd.to_datetime(first.get("latest_blocker_load_ts"), utc=True, errors="coerce")
                technical_hard_blocker_latest_ts = None if pd.isna(blocker_ts) else blocker_ts
            status_df = sql_to_df(
                f"""
                WITH active_watch AS (
                    SELECT DISTINCT UPPER(TRIM(symbol)) AS symbol
                    FROM {TABLE_NAME}
                    WHERE asof_date = %(asof_date)s
                      AND setup_id = %(setup_id)s
                      AND COALESCE(watch_enabled, false) IS TRUE
                      AND UPPER(TRIM(COALESCE(current_state, ''))) NOT IN ('REJECT', 'ABSTAIN')
                      AND symbol IS NOT NULL
                )
                SELECT
                    COUNT(*) AS newer_status_count,
                    MAX(s.load_ts) AS latest_status_load_ts
                FROM {TECHNICAL_REFRESH_STATUS_TABLE} s
                JOIN active_watch a
                  ON UPPER(TRIM(s.symbol)) = a.symbol
                WHERE s.asof_date <= %(asof_date)s
                  AND s.load_ts > %(latest_load_ts)s
                """,
                params={"asof_date": effective_asof, "setup_id": CONTEXT_OVERLAY_SETUP_ID, "latest_load_ts": latest_load_ts},
                retries=3,
                statement_timeout_ms=5000,
            )
            if not status_df.empty:
                status_first = status_df.iloc[0]
                technical_status_after_watchlist_count = int(status_first.get("newer_status_count") or 0)
                status_ts = pd.to_datetime(status_first.get("latest_status_load_ts"), utc=True, errors="coerce")
                technical_status_latest_ts = None if pd.isna(status_ts) else status_ts
        except Exception as exc:
            _record_watchlist_builder_fallback(
                fallback_type="watchlist_builder_context_current_state_technical_blocker_check_failed",
                source=TECHNICAL_REFRESH_STATUS_TABLE,
                reason="Watchlist builder could not inspect newer technical refresh status before skip-if-current guard.",
                error=exc,
                metadata={"asof_date": str(effective_asof), "setup_id": CONTEXT_OVERLAY_SETUP_ID},
            )
    technical_hard_blockers_current = technical_hard_blocker_count <= 0
    technical_status_current = technical_status_after_watchlist_count <= 0
    current = bool(
        row_count > 0
        and active_watch_count > 0
        and sync_state_available
        and sync_asof_matches
        and watchlist_fresh_for_signal_refresh
        and confirmation_plan_coverage_current
        and builder_version_current
        and technical_hard_blockers_current
        and technical_status_current
    )
    if current:
        status = "current"
    elif not has_watch_reasons:
        status = "missing_watch_reasons_json_column"
    elif active_watch_count > 0 and not confirmation_plan_coverage_current:
        status = "missing_technical_confirmation_plan"
    elif active_watch_count > 0 and not builder_version_current:
        status = "stale_context_watchlist_builder_version"
    elif not technical_hard_blockers_current:
        status = "stale_due_to_new_hard_ohlcv_blockers"
    elif not technical_status_current:
        status = "stale_due_to_new_technical_refresh_status"
    else:
        status = "stale_or_missing"
    return {
        "status": status,
        "current_for_reconcile": bool(current),
        "asof_date": effective_asof.isoformat(),
        "row_count": row_count,
        "active_watch_count": active_watch_count,
        "active_confirmation_plan_count": active_confirmation_plan_count,
        "active_missing_confirmation_plan_count": active_missing_confirmation_plan_count,
        "confirmation_plan_coverage_current": bool(confirmation_plan_coverage_current),
        "confirmation_plan_key": CONTEXT_OVERLAY_CONFIRMATION_PLAN_KEY,
        "active_builder_version_count": active_builder_version_count,
        "active_stale_builder_version_count": active_stale_builder_version_count,
        "builder_version_current": bool(builder_version_current),
        "context_watchlist_builder_version": CONTEXT_OVERLAY_WATCHLIST_BUILDER_VERSION,
        "latest_load_ts": None if latest_load_ts is None else latest_load_ts.isoformat(),
        "context_signal_refresh_sync_state_available": bool(sync_state_available),
        "context_signal_refresh_status": sync_status or None,
        "context_signal_refresh_asof_date": None if sync_asof is None else sync_asof.isoformat(),
        "context_signal_refresh_last_success_at": None if sync_last_success_at is None else sync_last_success_at.isoformat(),
        "sync_asof_matches": bool(sync_asof_matches),
        "watchlist_fresh_for_signal_refresh": bool(watchlist_fresh_for_signal_refresh),
        "technical_hard_blockers_current": bool(technical_hard_blockers_current),
        "technical_hard_blocker_after_watchlist_count": int(technical_hard_blocker_count),
        "technical_hard_blocker_after_watchlist_sample": technical_hard_blocker_sample,
        "technical_hard_blocker_latest_ts": None if technical_hard_blocker_latest_ts is None else technical_hard_blocker_latest_ts.isoformat(),
        "technical_status_current": bool(technical_status_current),
        "technical_status_after_watchlist_count": int(technical_status_after_watchlist_count),
        "technical_status_latest_ts": None if technical_status_latest_ts is None else technical_status_latest_ts.isoformat(),
        "operator_action": (
            "CONTEXT_OVERLAY_WATCH rows are current for the latest context signal-refresh sync state; skip duplicate rebuild."
            if current
            else (
                "Rebuild CONTEXT_OVERLAY_WATCH rows because active context watch rows are missing technical confirmation-plan metadata."
                if active_watch_count > 0 and not confirmation_plan_coverage_current
                else (
                    "Rebuild CONTEXT_OVERLAY_WATCH rows because active rows were created by an older context-watch selection algorithm."
                    if active_watch_count > 0 and not builder_version_current
                    else (
                        "Rebuild CONTEXT_OVERLAY_WATCH rows because newer technical-refresh status found hard OHLCV blockers for active context-watch symbols."
                        if not technical_hard_blockers_current
                        else (
                            "Rebuild CONTEXT_OVERLAY_WATCH rows because newer technical-refresh status may change technical priority, stale-history handling, or blocker suppression."
                            if not technical_status_current
                            else "Rebuild CONTEXT_OVERLAY_WATCH rows before full advisory if context watch pressure should be durable."
                        )
                    )
                )
            )
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build advisory announcement watchlist from candidates.")
    parser.add_argument("--date", type=parse_datetime_arg, help="Asof date in YYYY-MM-DD")
    parser.add_argument("--setup", dest="setup_ids", nargs="*", help="Optional setup ids")
    parser.add_argument("--symbols", nargs="*", help="Optional symbols")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument(
        "--skip-if-current",
        action="store_true",
        help="For CONTEXT_OVERLAY_WATCH-only runs, skip rebuild when same-date active rows are at least as fresh as context signal-refresh sync state.",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def summarize(df: pd.DataFrame) -> dict[str, object]:
    if df.empty:
        return {
            "status": "ok",
            "table": TABLE_NAME,
            "row_count": 0,
            "setup_count": 0,
            "symbol_count": 0,
            "sample": [],
        }
    return {
        "status": "ok",
        "table": TABLE_NAME,
        "row_count": int(len(df)),
        "setup_count": int(df["setup_id"].nunique()),
        "symbol_count": int(df["symbol"].nunique()),
        "sample": _json_safe(df.head(10).to_dict(orient="records")),
    }


def main() -> int:
    args = parse_args()
    asof_date = pd.Timestamp(args.date, tz="UTC") if args.date else None
    setup_ids = args.setup_ids
    setup_set = {str(value).strip() for value in setup_ids or [] if str(value).strip()}
    if (
        args.skip_if_current
        and not args.dry_run
        and not args.symbols
        and setup_set == {CONTEXT_OVERLAY_SETUP_ID}
    ):
        current_state = inspect_context_overlay_watchlist_current_state(asof_date=asof_date)
        if current_state.get("current_for_reconcile"):
            result = {
                "status": "skipped_current",
                "table": TABLE_NAME,
                "row_count": int(current_state.get("row_count") or 0),
                "setup_count": 1,
                "symbol_count": None,
                "sample": [],
                "dry_run": False,
                "skip_if_current": True,
                "current_state": current_state,
            }
            print(json.dumps(_json_safe(result), indent=2, ensure_ascii=False, default=str, allow_nan=False))
            return 0
    df = build_watchlist(
        asof_date=asof_date,
        setup_ids=setup_ids,
        symbols=args.symbols,
    )
    if not args.dry_run:
        persist_watchlist(df, rebuild=args.rebuild, asof_date=asof_date or (df["asof_date"].max() if not df.empty else None))
    result = summarize(df)
    result["dry_run"] = bool(args.dry_run)
    print(json.dumps(_json_safe(result), indent=2, ensure_ascii=False, default=str, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
