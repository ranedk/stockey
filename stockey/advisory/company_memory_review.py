from __future__ import annotations

import argparse
import json
from typing import Any, Literal

import pandas as pd
from environs import Env
from pydantic import BaseModel, Field

from advisory.fallback_telemetry import record_fallback_event, record_local_fallback_event
from advisory.event_evidence_store import ANNOUNCEMENT_CONTEXT_OVERLAYS_TABLE, BHAVCOPY_CONTEXT_OVERLAYS_TABLE
from advisory.exchange_context_overlays import EXCHANGE_CONTEXT_OVERLAYS_TABLE
from advisory.macro_context_overlays import MACRO_CONTEXT_OVERLAYS_TABLE
from advisory.news_theme_engine import THEME_CONTEXT_OVERLAYS_TABLE
from advisory.causal_event_memory import TABLE_NAME as CAUSAL_EVENT_MEMORY_TABLE
from advisory.score_scales import to_100
from advisory.prompt_registry import prompt_version as registry_prompt_version
from advisory.prompt_registry import response_schema_version
from utils.codex_cli import run_codex_structured
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration
from utils.sync import parse_datetime_arg


env = Env()
env.read_env()

TABLE_NAME = "advisory_company_memory_reviews"
COMPANY_MEMORY_SCHEMA_MIGRATION_ID = "20260611_advisory_company_memory_reviews_base"
ANNOUNCEMENT_EVIDENCE_TABLE = "advisory_announcement_evidence"
BHAVCOPY_EVIDENCE_TABLE = "advisory_bhavcopy_evidence_daily"
TECHNICAL_TABLE = "advisory_technical_daily"
CANDIDATES_TABLE = "advisory_candidates"
WATCHLIST_TABLE = "advisory_watchlist"
EVENT_EVALUATIONS_TABLE = "advisory_event_evaluations"
EVENT_POLICY_TABLE = "advisory_event_policy_actions"
ACTIONS_TABLE = "advisory_action_recommendations"
WAIT_SIGNALS_TABLE = "advisory_wait_signals"
PROMPT_ID = "company_memory_review"
PROMPT_VERSION = registry_prompt_version(PROMPT_ID)
PROMPT_SCHEMA_VERSION = response_schema_version(PROMPT_ID)

DEFAULT_MODEL = env("COMPANY_MEMORY_REVIEW_MODEL", default=env("CODEX_CLI_MODEL", default="gpt-5.4-mini"))
DEFAULT_MAX_SYMBOLS = env.int("COMPANY_MEMORY_REVIEW_MAX_SYMBOLS", default=12)
DEFAULT_LOOKBACK_DAYS = env.int("COMPANY_MEMORY_REVIEW_LOOKBACK_DAYS", default=180)
DEFAULT_CONTEXT_OVERLAY_LOOKBACK_DAYS = env.int("COMPANY_MEMORY_CONTEXT_OVERLAY_LOOKBACK_DAYS", default=90)
DEFAULT_CAUSAL_EVENT_MEMORY_LOOKBACK_DAYS = env.int("COMPANY_MEMORY_CAUSAL_EVENT_MEMORY_LOOKBACK_DAYS", default=90)
LLM_ENABLED = env.bool("COMPANY_MEMORY_REVIEW_LLM_ENABLED", default=False)

SIGNALS = {"BUY", "BUY_MORE", "HOLD", "WATCH", "SELL_PARTIAL", "SELL", "NO_ACTION"}
CONTEXT_RELIABILITY_SUPPRESS_CLASSES = {
    "hurts_or_no_lift",
    "negative_after_cost",
    "inconsistent_or_horizon_sensitive",
    "needs_benchmark_attribution",
    "benchmark_beta_not_overlay_alpha",
}
COMPANY_MEMORY_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        review_date TIMESTAMPTZ NOT NULL,
        symbol TEXT NOT NULL,
        recommended_signal TEXT NOT NULL,
        confidence DOUBLE PRECISION,
        conviction_score DOUBLE PRECISION,
        summary TEXT,
        thesis TEXT,
        risk_flags_json TEXT,
        evidence_used_json TEXT,
        wait_for_json TEXT,
        deterministic_boundary TEXT,
        authority_scope TEXT,
        model_name TEXT,
        prompt_id TEXT,
        prompt_version TEXT,
        prompt_schema_version TEXT,
        review_status TEXT,
        fallback_used BOOLEAN,
        error TEXT,
        payload_json TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (review_date, symbol)
    )
    """,
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS risk_flags_json TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS evidence_used_json TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS wait_for_json TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS deterministic_boundary TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS authority_scope TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS model_name TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS prompt_id TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS prompt_version TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS prompt_schema_version TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS review_status TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS fallback_used BOOLEAN",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS error TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS payload_json TEXT",
]


class CompanyMemoryReview(BaseModel):
    recommended_signal: Literal["BUY", "BUY_MORE", "HOLD", "WATCH", "SELL_PARTIAL", "SELL", "NO_ACTION"]
    confidence: float = Field(ge=0.0, le=1.0)
    conviction_score: float = Field(ge=0.0, le=100.0)
    summary: str = Field(min_length=10, max_length=900)
    thesis: str = Field(min_length=10, max_length=1400)
    risk_flags: list[str] = Field(default_factory=list, max_length=8)
    evidence_used: list[str] = Field(default_factory=list, max_length=12)
    wait_for: list[str] = Field(default_factory=list, max_length=8)
    deterministic_boundary: str = Field(
        default="Review input only. Deterministic action consolidation and execution safety checks remain authoritative.",
        max_length=500,
    )


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _json_ready(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    try:
        if pd.isna(value):
            return None
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.company_memory_review",
            fallback_type="company_memory_json_ready_missing_check_failed",
            source="json_ready",
            severity="warn",
            reason="Company-memory review could not evaluate missingness while preparing JSON and kept the original value.",
            error=exc,
            metadata={"value_type": type(value).__name__},
        )
    return value


def _records(df: pd.DataFrame, *, limit: int = 10) -> list[dict[str, Any]]:
    if df.empty:
        return []
    sample = df.head(max(0, int(limit))).copy()
    sample = sample.astype(object).where(pd.notna(sample), None)
    return [{key: _json_ready(value) for key, value in row.items()} for row in sample.to_dict(orient="records")]


def _text(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.company_memory_review",
            fallback_type="company_memory_text_missing_check_failed",
            source="text",
            severity="warn",
            reason="Company-memory review could not evaluate missingness while normalizing text and kept string conversion fallback.",
            error=exc,
            metadata={"value_type": type(value).__name__},
        )
    text = str(value).strip()
    return text or None


def _num(value: Any, default: float = 0.0) -> float:
    out = pd.to_numeric(value, errors="coerce")
    return default if pd.isna(out) else float(out)


def _asof(value: Any = None) -> pd.Timestamp:
    ts = pd.to_datetime(value or pd.Timestamp.utcnow(), utc=True, errors="coerce")
    if pd.isna(ts):
        ts = pd.Timestamp.utcnow()
    return ts.normalize()


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
            retries=2,
            statement_timeout_ms=5000,
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.company_memory_review",
            fallback_type="company_memory_source_table_lookup_failed",
            source=table_name,
            severity="warn",
            reason="Company-memory context skipped a source because table existence lookup failed.",
            error=exc,
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
            retries=2,
            statement_timeout_ms=5000,
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.company_memory_review",
            fallback_type="company_memory_source_columns_lookup_failed",
            source=table_name,
            severity="warn",
            reason="Company-memory context could not inspect optional source columns and will use required columns only.",
            error=exc,
        )
        return set()
    return set(df["column_name"].astype(str).tolist()) if not df.empty and "column_name" in df.columns else set()


def ensure_table() -> None:
    apply_schema_migration(
        migration_id=COMPANY_MEMORY_SCHEMA_MIGRATION_ID,
        description="Create review-only company memory signal table.",
        statements=COMPANY_MEMORY_SCHEMA_STATEMENTS,
        metadata={"tables": [TABLE_NAME], "authority_scope": "review_input_only"},
    )


def load_review_symbols(*, asof_date: pd.Timestamp, symbols: list[str] | None = None, limit: int = DEFAULT_MAX_SYMBOLS) -> list[str]:
    explicit = sorted({str(symbol).strip().upper() for symbol in (symbols or []) if str(symbol or "").strip()})
    if explicit:
        return explicit[: max(1, int(limit))]

    frames: list[pd.DataFrame] = []
    sources = [
        (ACTIONS_TABLE, "asof_date"),
        (EVENT_POLICY_TABLE, "asof_date"),
        (CANDIDATES_TABLE, "asof_date"),
        (WATCHLIST_TABLE, "asof_date"),
    ]
    for table_name, date_col in sources:
        if not table_exists(table_name):
            continue
        try:
            frames.append(
                sql_to_df(
                    f"""
                    SELECT DISTINCT UPPER(TRIM(symbol)) AS symbol
                    FROM {table_name}
                    WHERE symbol IS NOT NULL
                      AND {date_col} <= %(asof_date)s
                    ORDER BY symbol
                    LIMIT %(limit)s
                    """,
                    params={"asof_date": asof_date, "limit": max(1, int(limit) * 4)},
                    retries=2,
                    statement_timeout_ms=10000,
                )
            )
        except Exception as exc:
            record_local_fallback_event(
                module="advisory.company_memory_review",
                fallback_type="company_memory_review_symbols_load_failed",
                source=table_name,
                severity="warn",
                reason="Company-memory review skipped a candidate-symbol source because symbol discovery failed.",
                error=exc,
                metadata={
                    "date_column": date_col,
                    "asof_date": None if pd.isna(asof_date) else pd.Timestamp(asof_date).isoformat(),
                    "limit": max(1, int(limit) * 4),
                },
            )
            continue
    if not frames:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for frame in frames:
        if frame.empty:
            continue
        for symbol in frame["symbol"].dropna().astype(str).tolist():
            clean = symbol.strip().upper()
            if not clean or clean in seen:
                continue
            seen.add(clean)
            out.append(clean)
            if len(out) >= max(1, int(limit)):
                return out
    return out


def _load_table_rows(
    table_name: str,
    *,
    symbol: str,
    asof_date: pd.Timestamp,
    date_column: str,
    columns: list[str],
    optional_columns: list[str] | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    limit: int = 8,
) -> pd.DataFrame:
    if not table_exists(table_name):
        return pd.DataFrame()
    start_date = asof_date - pd.Timedelta(days=max(1, int(lookback_days)))
    selected_columns = list(columns)
    if optional_columns:
        available = table_columns(table_name)
        selected_columns.extend([column for column in optional_columns if column in available and column not in selected_columns])
    column_sql = ", ".join(selected_columns)
    try:
        return sql_to_df(
            f"""
            SELECT {column_sql}
            FROM {table_name}
            WHERE UPPER(TRIM(symbol)) = %(symbol)s
              AND {date_column} <= %(asof_date)s
              AND {date_column} >= %(start_date)s
            ORDER BY {date_column} DESC
            LIMIT %(limit)s
            """,
            params={"symbol": symbol.upper(), "asof_date": asof_date, "start_date": start_date, "limit": max(1, int(limit))},
            retries=2,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.company_memory_review",
            fallback_type="company_memory_source_rows_load_failed",
            source=table_name,
            severity="warn",
            symbol=symbol.upper(),
            reason="Company-memory context skipped source rows because source loading failed.",
            error=exc,
            metadata={
                "date_column": date_column,
                "lookback_days": int(lookback_days),
                "limit": max(1, int(limit)),
            },
        )
        return pd.DataFrame()


def _load_wait_signal_rows(
    *,
    symbol: str,
    asof_date: pd.Timestamp,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    limit: int = 8,
) -> pd.DataFrame:
    if not table_exists(WAIT_SIGNALS_TABLE):
        return pd.DataFrame()
    start_date = asof_date - pd.Timedelta(days=max(1, int(lookback_days)))
    try:
        return sql_to_df(
            f"""
            SELECT
                created_at,
                signal_id,
                status,
                signal_type,
                condition_summary,
                operator_summary,
                wait_question,
                valid_until
            FROM {WAIT_SIGNALS_TABLE}
            WHERE UPPER(TRIM(symbol)) = %(symbol)s
              AND created_at <= %(asof_date)s
              AND created_at >= %(start_date)s
              AND (valid_until IS NULL OR valid_until >= %(asof_date)s)
            ORDER BY created_at DESC
            LIMIT %(limit)s
            """,
            params={"symbol": symbol.upper(), "asof_date": asof_date, "start_date": start_date, "limit": max(1, int(limit))},
            retries=2,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.company_memory_review",
            fallback_type="company_memory_wait_signals_load_failed",
            source=WAIT_SIGNALS_TABLE,
            severity="warn",
            symbol=symbol.upper(),
            reason="Company-memory context skipped wait-signal rows because source loading failed.",
            error=exc,
            metadata={
                "lookback_days": int(lookback_days),
                "limit": max(1, int(limit)),
            },
        )
        return pd.DataFrame()


def _load_company_context_overlay_rows(
    table_name: str,
    *,
    source_name: str,
    symbol: str,
    asof_date: pd.Timestamp,
    date_column: str,
    class_column: str | None = None,
    reason_column: str = "watch_reason_detail",
    lookback_days: int = DEFAULT_CONTEXT_OVERLAY_LOOKBACK_DAYS,
    limit: int = 8,
) -> pd.DataFrame:
    if not table_exists(table_name):
        return pd.DataFrame()
    available = table_columns(table_name)
    required = {"symbol", date_column, "direction"}
    if not required.issubset(available):
        return pd.DataFrame()
    selected = [
        f"{date_column} AS observed_at",
        "%(source_name)s::TEXT AS context_source",
        "overlay_id",
        "symbol",
        "direction",
    ]
    if "pressure_score" in available:
        selected.append("pressure_score")
    if class_column and class_column in available:
        selected.append(f"{class_column} AS context_class")
    else:
        selected.append("NULL::TEXT AS context_class")
    if reason_column in available:
        selected.append(f"{reason_column} AS context_reason")
    else:
        selected.append("NULL::TEXT AS context_reason")
    for column in [
        "authority_scope",
        "production_status",
        "matched_sources_json",
        "source_reliability",
        "confidence",
        "materiality",
        "evidence_score",
        "deal_pressure",
        "event_type",
        "event_side",
    ]:
        if column in available and column not in selected:
            selected.append(column)
    start_date = asof_date - pd.Timedelta(days=max(1, int(lookback_days)))
    production_filter = "AND LOWER(COALESCE(production_status, 'active')) = 'active'" if "production_status" in available else ""
    pressure_order = "ABS(COALESCE(pressure_score, 0.0))" if "pressure_score" in available else "0.0"
    try:
        df = sql_to_df(
            f"""
            SELECT {", ".join(selected)}
            FROM {table_name}
            WHERE UPPER(TRIM(symbol)) = %(symbol)s
              AND {date_column} <= %(asof_date)s
              AND {date_column} >= %(start_date)s
              {production_filter}
            ORDER BY {date_column} DESC, {pressure_order} DESC
            LIMIT %(limit)s
            """,
            params={
                "source_name": source_name,
                "symbol": symbol.upper(),
                "asof_date": asof_date,
                "start_date": start_date,
                "limit": max(1, int(limit)),
            },
            retries=2,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.company_memory_review",
            fallback_type="company_memory_context_overlay_rows_load_failed",
            source=table_name,
            severity="warn",
            symbol=symbol.upper(),
            reason="Company-memory context skipped context-overlay rows because source loading failed.",
            error=exc,
            metadata={
                "context_source": source_name,
                "date_column": date_column,
                "lookback_days": int(lookback_days),
                "limit": max(1, int(limit)),
            },
        )
        return pd.DataFrame()
    if df.empty:
        return df
    df["observed_at"] = pd.to_datetime(df["observed_at"], utc=True, errors="coerce")
    if "pressure_score" in df.columns:
        df["pressure_score"] = pd.to_numeric(df["pressure_score"], errors="coerce")
    return df


def _sector_code_from_context(symbol_context: dict[str, Any] | None) -> str | None:
    context = symbol_context if isinstance(symbol_context, dict) else {}
    for row in context.get("technical") or []:
        if not isinstance(row, dict):
            continue
        text = _text(row.get("sector_code"))
        if text:
            return text.upper()
    return None


def _load_company_sector_context_overlay_rows(
    table_name: str,
    *,
    source_name: str,
    symbol: str,
    sector_code: str | None,
    asof_date: pd.Timestamp,
    date_column: str = "asof_date",
    class_column: str | None = None,
    reason_column: str = "theme_reason",
    lookback_days: int = DEFAULT_CONTEXT_OVERLAY_LOOKBACK_DAYS,
    limit: int = 8,
) -> pd.DataFrame:
    if not sector_code or not table_exists(table_name):
        return pd.DataFrame()
    available = table_columns(table_name)
    required = {"sector_code", date_column, "direction"}
    if not required.issubset(available):
        return pd.DataFrame()
    selected = [
        f"{date_column} AS observed_at",
        "%(source_name)s::TEXT AS context_source",
        "overlay_id",
        "%(symbol)s::TEXT AS symbol",
        "sector_code",
        "sector_name" if "sector_name" in available else "NULL::TEXT AS sector_name",
        "direction",
    ]
    if "pressure_score" in available:
        selected.append("pressure_score")
    if class_column and class_column in available:
        selected.append(f"{class_column} AS context_class")
    else:
        selected.append("NULL::TEXT AS context_class")
    if reason_column in available:
        selected.append(f"{reason_column} AS context_reason")
    elif "trigger_reason" in available:
        selected.append("trigger_reason AS context_reason")
    else:
        selected.append("NULL::TEXT AS context_reason")
    for column in [
        "authority_scope",
        "production_status",
        "matched_sources_json",
        "theme_intensity",
        "macro_stress_score",
        "macro_risk_state",
        "risk_level",
    ]:
        if column in available and column not in selected:
            selected.append(column)
    start_date = asof_date - pd.Timedelta(days=max(1, int(lookback_days)))
    production_filter = "AND LOWER(COALESCE(production_status, 'active')) = 'active'" if "production_status" in available else ""
    pressure_order = "ABS(COALESCE(pressure_score, 0.0))" if "pressure_score" in available else "0.0"
    try:
        df = sql_to_df(
            f"""
            SELECT {", ".join(selected)}
            FROM {table_name}
            WHERE UPPER(TRIM(sector_code)) = %(sector_code)s
              AND {date_column} <= %(asof_date)s
              AND {date_column} >= %(start_date)s
              {production_filter}
            ORDER BY {date_column} DESC, {pressure_order} DESC
            LIMIT %(limit)s
            """,
            params={
                "source_name": source_name,
                "symbol": symbol.upper(),
                "sector_code": sector_code.upper(),
                "asof_date": asof_date,
                "start_date": start_date,
                "limit": max(1, int(limit)),
            },
            retries=2,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.company_memory_review",
            fallback_type="company_memory_sector_context_overlay_rows_load_failed",
            source=table_name,
            severity="warn",
            symbol=symbol.upper(),
            reason="Company-memory context skipped sector-mapped context-overlay rows because source loading failed.",
            error=exc,
            metadata={
                "context_source": source_name,
                "sector_code": sector_code,
                "date_column": date_column,
                "lookback_days": int(lookback_days),
                "limit": max(1, int(limit)),
            },
        )
        return pd.DataFrame()
    if df.empty:
        return df
    df["observed_at"] = pd.to_datetime(df["observed_at"], utc=True, errors="coerce")
    if "pressure_score" in df.columns:
        df["pressure_score"] = pd.to_numeric(df["pressure_score"], errors="coerce")
    return df


def load_company_context_overlays(
    *,
    symbol: str,
    asof_date: pd.Timestamp,
    symbol_context: dict[str, Any] | None = None,
    lookback_days: int = DEFAULT_CONTEXT_OVERLAY_LOOKBACK_DAYS,
    limit_per_source: int = 8,
) -> list[dict[str, Any]]:
    symbol = symbol.strip().upper()
    sector_code = _sector_code_from_context(symbol_context)
    frames = [
        _load_company_context_overlay_rows(
            ANNOUNCEMENT_CONTEXT_OVERLAYS_TABLE,
            source_name="announcement_context",
            symbol=symbol,
            asof_date=asof_date + pd.Timedelta(days=1),
            date_column="published_on",
            class_column="event_class",
            lookback_days=lookback_days,
            limit=limit_per_source,
        ),
        _load_company_context_overlay_rows(
            EXCHANGE_CONTEXT_OVERLAYS_TABLE,
            source_name="exchange_context",
            symbol=symbol,
            asof_date=asof_date,
            date_column="asof_date",
            class_column="event_type",
            lookback_days=lookback_days,
            limit=limit_per_source,
        ),
        _load_company_context_overlay_rows(
            BHAVCOPY_CONTEXT_OVERLAYS_TABLE,
            source_name="bhavcopy_context",
            symbol=symbol,
            asof_date=asof_date,
            date_column="asof_date",
            class_column="deal_pressure",
            lookback_days=lookback_days,
            limit=limit_per_source,
        ),
        _load_company_sector_context_overlay_rows(
            THEME_CONTEXT_OVERLAYS_TABLE,
            source_name="theme_context",
            symbol=symbol,
            sector_code=sector_code,
            asof_date=asof_date,
            class_column="theme_id",
            reason_column="theme_reason",
            lookback_days=lookback_days,
            limit=limit_per_source,
        ),
        _load_company_sector_context_overlay_rows(
            MACRO_CONTEXT_OVERLAYS_TABLE,
            source_name="macro_context",
            symbol=symbol,
            sector_code=sector_code,
            asof_date=asof_date,
            class_column="macro_signal_id",
            reason_column="trigger_reason",
            lookback_days=lookback_days,
            limit=limit_per_source,
        ),
    ]
    frames = [frame for frame in frames if isinstance(frame, pd.DataFrame) and not frame.empty]
    if not frames:
        return []
    overlays = pd.concat(frames, ignore_index=True, sort=False)
    if "pressure_score" in overlays.columns:
        overlays = overlays.assign(_rank_pressure=overlays["pressure_score"].abs().fillna(0.0))
    else:
        overlays = overlays.assign(_rank_pressure=0.0)
    overlays = overlays.sort_values(["observed_at", "_rank_pressure"], ascending=[False, False]).drop(columns=["_rank_pressure"], errors="ignore")
    return _records(overlays, limit=max(1, int(limit_per_source) * 3))


def _context_class_reliability(
    source_family_reliability: dict[str, Any] | None,
    source_family: Any,
    context_class: Any,
) -> dict[str, Any] | None:
    families = _reliability_families(source_family_reliability)
    family = str(source_family or "").strip()
    target_class = str(context_class or "").strip().upper()
    if not family or not target_class or target_class in {"<NA>", "NAN", "NONE"}:
        return None
    family_row = families.get(family)
    if not isinstance(family_row, dict):
        return None
    for item in family_row.get("context_class_diagnostics") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("context_class") or "").strip().upper() != target_class:
            continue
        return {
            **item,
            "context_class": item.get("context_class") or target_class,
            "source_family": family,
            "authority_scope": "research_only",
            "action_policy_effect": "annotation_only_no_trade_authority",
            "broker_execution_allowed": False,
        }
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


def _context_sector_reliability(
    source_family_reliability: dict[str, Any] | None,
    source_family: Any,
    sector_name: Any,
    sector_code: Any,
) -> dict[str, Any] | None:
    families = _reliability_families(source_family_reliability)
    family = str(source_family or "").strip()
    if not family:
        return None
    family_row = families.get(family)
    if not isinstance(family_row, dict):
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
    for item in family_row.get("sector_diagnostics") or []:
        if not isinstance(item, dict):
            continue
        row_keys = {
            key
            for key in [
                _sector_key_from_values(item.get("sector_code")),
                _sector_key_from_values(item.get("sector_name")),
                _sector_key_from_values(item.get("sector_key")),
            ]
            if key
        }
        if not target_keys & row_keys:
            continue
        return {
            **item,
            "source_family": family,
            "authority_scope": "research_only",
            "action_policy_effect": "annotation_only_no_trade_authority",
            "broker_execution_allowed": False,
        }
    return None


def _reliability_families(source_family_reliability: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(source_family_reliability, dict):
        return {}
    families = source_family_reliability.get("families")
    if isinstance(families, dict):
        return families
    if isinstance(families, list):
        return {
            str(item.get("source_family")): item
            for item in families
            if isinstance(item, dict) and item.get("source_family")
        }
    return source_family_reliability


def _runtime_contract_for_context_reliability(row: dict[str, Any] | None) -> dict[str, Any]:
    from advisory.context_overlay_reliability_report import reliability_runtime_policy_contract

    if not isinstance(row, dict):
        return reliability_runtime_policy_contract(None)
    contract = row.get("runtime_policy_contract")
    if isinstance(contract, dict) and contract:
        return contract
    return reliability_runtime_policy_contract(row.get("classification"))


def _runtime_contract_allows_context(
    row: dict[str, Any] | None,
    use_name: str,
    *,
    legacy_classification: str | None = None,
) -> bool:
    contract = _runtime_contract_for_context_reliability(row)
    allowed = contract.get("allowed_runtime_uses")
    if isinstance(allowed, dict) and use_name in allowed:
        return bool(allowed.get(use_name))
    classification = str((row or {}).get("classification") or legacy_classification or "").strip()
    if use_name == "watch_priority":
        return classification == "candidate_helpful"
    if use_name == "de_risk_review":
        return classification == "protective_candidate"
    return False


def _context_overlay_reliability_suppressed(item: dict[str, Any]) -> bool:
    family_reliability = item.get("source_family_reliability") if isinstance(item.get("source_family_reliability"), dict) else {}
    class_reliability = item.get("context_class_reliability") if isinstance(item.get("context_class_reliability"), dict) else {}
    sector_reliability = item.get("context_sector_reliability") if isinstance(item.get("context_sector_reliability"), dict) else {}
    family_classification = str(family_reliability.get("classification") or "").strip()
    class_classification = str(class_reliability.get("classification") or "").strip()
    sector_classification = str(sector_reliability.get("classification") or "").strip()
    return (
        family_classification in CONTEXT_RELIABILITY_SUPPRESS_CLASSES
        or class_classification in CONTEXT_RELIABILITY_SUPPRESS_CLASSES
        or sector_classification in CONTEXT_RELIABILITY_SUPPRESS_CLASSES
    )


def _context_overlay_runtime_effect_allowed(item: dict[str, Any], direction: str) -> bool:
    family_reliability = item.get("source_family_reliability") if isinstance(item.get("source_family_reliability"), dict) else {}
    class_reliability = item.get("context_class_reliability") if isinstance(item.get("context_class_reliability"), dict) else {}
    if not family_reliability:
        return True
    if direction in {"positive", "watch"}:
        return _runtime_contract_allows_context(
            family_reliability,
            "watch_priority",
            legacy_classification=str(family_reliability.get("classification") or "").strip(),
        )
    if direction == "negative":
        family_allows = _runtime_contract_allows_context(
            family_reliability,
            "de_risk_review",
            legacy_classification=str(family_reliability.get("classification") or "").strip(),
        )
        class_allows = bool(
            class_reliability
            and _runtime_contract_allows_context(
                class_reliability,
                "de_risk_review",
                legacy_classification=str(class_reliability.get("classification") or "").strip(),
            )
        )
        return bool(family_allows or class_allows)
    return True


def load_company_context_overlay_reliability(asof_date: pd.Timestamp | None = None) -> dict[str, Any]:
    try:
        from advisory.context_overlay_reliability_report import load_persisted_reliability_report as load_persisted_context_reliability_report

        reliability = load_persisted_context_reliability_report(asof_date=asof_date)
        return reliability if isinstance(reliability, dict) else {}
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.company_memory_review",
            fallback_type="company_memory_context_overlay_reliability_load_failed",
            source="advisory_context_overlay_reliability_summary",
            severity="warn",
            reason="Company-memory review could not load context-overlay reliability and treated missing reliability as neutral.",
            error=exc,
            metadata={"asof_date": None if asof_date is None else str(asof_date)},
        )
        return {}


def summarize_company_context_overlays(
    rows: list[dict[str, Any]],
    *,
    source_family_reliability: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not rows:
        return {
            "schema_version": 1,
            "authority_scope": "watchlist_pressure_only",
            "action_policy_effect": "review_input_only_no_trade_authority",
            "broker_execution_allowed": False,
            "row_count": 0,
            "positive_count": 0,
            "negative_count": 0,
            "watch_count": 0,
            "effective_positive_count": 0,
            "effective_negative_count": 0,
            "effective_watch_count": 0,
            "reliability_suppressed_count": 0,
            "source_family_counts": {},
            "top_overlays": [],
            "top_suppressed_overlays": [],
        }
    frame = pd.DataFrame(rows)
    directions = frame.get("direction", pd.Series(dtype="object")).astype("string").str.lower()
    pressure = pd.to_numeric(frame.get("pressure_score"), errors="coerce").abs() if "pressure_score" in frame.columns else pd.Series([0.0] * len(frame))
    ranked = frame.assign(_rank_pressure=pressure.fillna(0.0))
    if "observed_at" in ranked.columns:
        ranked["_rank_time"] = pd.to_datetime(ranked["observed_at"], utc=True, errors="coerce")
        ranked = ranked.sort_values(["_rank_time", "_rank_pressure"], ascending=[False, False])
    else:
        ranked = ranked.sort_values("_rank_pressure", ascending=False)
    top_overlays: list[dict[str, Any]] = []
    top_suppressed_overlays: list[dict[str, Any]] = []
    effective_counts = {"positive": 0, "negative": 0, "watch": 0}
    for item in _records(ranked.drop(columns=["_rank_pressure", "_rank_time"], errors="ignore"), limit=8):
        source = item.get("context_source")
        family_reliability = None
        if isinstance(source_family_reliability, dict):
            families = _reliability_families(source_family_reliability)
            family_reliability = families.get(str(source)) if isinstance(families, dict) and source is not None else None
            if isinstance(family_reliability, dict) and "runtime_policy_contract" not in family_reliability:
                from advisory.context_overlay_reliability_report import reliability_runtime_policy_contract

                family_reliability = {
                    **family_reliability,
                    "runtime_policy_contract": reliability_runtime_policy_contract(family_reliability.get("classification")),
                }
        class_reliability = _context_class_reliability(source_family_reliability, source, item.get("context_class"))
        sector_reliability = _context_sector_reliability(
            source_family_reliability,
            source,
            item.get("sector_name"),
            item.get("sector_code"),
        )
        compact = {
            "observed_at": item.get("observed_at"),
            "source": source,
            "direction": item.get("direction"),
            "class": item.get("context_class"),
            "sector_code": item.get("sector_code"),
            "sector_name": item.get("sector_name"),
            "pressure_score": item.get("pressure_score"),
            "reason": item.get("context_reason"),
            "authority_scope": item.get("authority_scope") or "watchlist_pressure_only",
            "broker_execution_allowed": False,
            "source_family_reliability": family_reliability,
            "context_class_reliability": class_reliability,
            "context_sector_reliability": sector_reliability,
        }
        if _context_overlay_reliability_suppressed(compact):
            compact["reliability_suppressed"] = True
            top_suppressed_overlays.append(compact)
            continue
        direction = str(item.get("direction") or "").strip().lower()
        if not _context_overlay_runtime_effect_allowed(compact, direction):
            compact["reliability_suppressed"] = True
            compact["runtime_policy_contract_suppressed"] = True
            compact["runtime_policy_contract_suppression_reason"] = (
                "watch_priority_not_allowed"
                if direction in {"positive", "watch"}
                else "de_risk_review_not_allowed"
                if direction == "negative"
                else "runtime_use_not_allowed"
            )
            top_suppressed_overlays.append(compact)
            continue
        if direction in effective_counts:
            effective_counts[direction] += 1
        top_overlays.append(compact)
    return {
        "schema_version": 1,
        "authority_scope": "watchlist_pressure_only",
        "action_policy_effect": "review_input_only_no_trade_authority",
        "reliability_policy_effect": "annotation_only_no_trade_authority",
        "portfolio_authority": "none",
        "broker_execution_allowed": False,
        "row_count": int(len(frame)),
        "positive_count": int(directions.eq("positive").sum()),
        "negative_count": int(directions.eq("negative").sum()),
        "watch_count": int(directions.eq("watch").sum()),
        "effective_positive_count": int(effective_counts["positive"]),
        "effective_negative_count": int(effective_counts["negative"]),
        "effective_watch_count": int(effective_counts["watch"]),
        "reliability_suppressed_count": int(len(top_suppressed_overlays)),
        "source_family_counts": frame.get("context_source", pd.Series(dtype="object")).dropna().astype(str).value_counts().to_dict(),
        "max_pressure_score": None if pressure.dropna().empty else float(pressure.max()),
        "top_overlays": top_overlays,
        "top_suppressed_overlays": top_suppressed_overlays,
    }


def _filter_review_ready_announcement_evidence(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    if "announcement_storage_form" in out.columns:
        out = out[out["announcement_storage_form"].astype("string").str.lower().ne("archived_raw_reference_only")].copy()
    if "llm_review_ready" in out.columns:
        out = out[out["llm_review_ready"].map(lambda value: str(value).strip().lower() not in {"0", "false", "f", "no", "n"})].copy()
    return out


def _latest_sector_code_from_context(context: dict[str, Any]) -> str | None:
    for section in ("technical", "context_overlays"):
        rows = context.get(section) or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            sector_code = _text(row.get("sector_code"))
            if sector_code:
                return sector_code.strip().upper()
    return None


def load_company_causal_event_memory(
    *,
    symbol: str,
    asof_date: pd.Timestamp,
    symbol_context: dict[str, Any] | None = None,
    lookback_days: int = DEFAULT_CAUSAL_EVENT_MEMORY_LOOKBACK_DAYS,
    limit: int = 8,
) -> list[dict[str, Any]]:
    symbol = symbol.strip().upper()
    if not table_exists(CAUSAL_EVENT_MEMORY_TABLE):
        return []
    start_date = asof_date - pd.Timedelta(days=max(1, int(lookback_days)))
    context = symbol_context or {}
    sector_code = _latest_sector_code_from_context(context)
    sector_clause = ""
    params: dict[str, Any] = {
        "symbol": symbol,
        "asof_date": asof_date,
        "start_date": start_date,
        "limit": max(1, int(limit)),
    }
    if sector_code:
        sector_clause = "OR (symbol IS NULL AND UPPER(TRIM(sector_code)) = %(sector_code)s)"
        params["sector_code"] = sector_code
    try:
        df = sql_to_df(
            f"""
            SELECT
                asof_date,
                memory_id,
                symbol,
                sector_code,
                sector_name,
                context_source,
                event_group,
                event_type,
                context_class,
                direction,
                event_state,
                pressure_score,
                decayed_pressure_score,
                event_count,
                first_seen_at,
                last_seen_at,
                expected_decay_days,
                freshness_days,
                contradiction_state,
                source_refs_json,
                source_summary_json,
                authority_scope,
                portfolio_authority,
                broker_execution_allowed,
                policy_effect
            FROM {CAUSAL_EVENT_MEMORY_TABLE}
            WHERE asof_date <= %(asof_date)s
              AND asof_date >= %(start_date)s
              AND (
                  UPPER(TRIM(symbol)) = %(symbol)s
                  {sector_clause}
              )
            ORDER BY asof_date DESC, decayed_pressure_score DESC NULLS LAST, pressure_score DESC NULLS LAST
            LIMIT %(limit)s
            """,
            params=params,
            retries=2,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.company_memory_review",
            fallback_type="company_memory_causal_event_memory_load_failed",
            source=CAUSAL_EVENT_MEMORY_TABLE,
            severity="warn",
            symbol=symbol,
            reason="Company-memory context skipped causal event memory rows because source loading failed.",
            error=exc,
            metadata={"lookback_days": int(lookback_days), "limit": max(1, int(limit)), "sector_code": sector_code},
        )
        return []
    return _records(df, limit=limit)


def summarize_company_causal_event_memory(rows: list[dict[str, Any]]) -> dict[str, Any]:
    frame = pd.DataFrame(rows or [])
    if frame.empty:
        return {
            "schema_version": 1,
            "authority_scope": "review_input_only",
            "action_policy_effect": "memory_context_only_no_trade_authority",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "row_count": 0,
            "positive_count": 0,
            "negative_count": 0,
            "mixed_count": 0,
            "contradiction_count": 0,
            "top_memories": [],
        }
    directions = frame.get("direction", pd.Series(dtype="object")).astype("string").str.lower()
    states = frame.get("event_state", pd.Series(dtype="object")).astype("string").str.lower()
    contradictions = frame.get("contradiction_state", pd.Series(dtype="object")).astype("string").str.lower()
    pressure = pd.to_numeric(frame.get("decayed_pressure_score", frame.get("pressure_score")), errors="coerce")
    if "asof_date" not in frame.columns:
        frame["asof_date"] = pd.NaT
    sorted_frame = frame.assign(_sort_pressure=pressure.fillna(0.0)).sort_values(
        ["asof_date", "_sort_pressure"],
        ascending=[False, False],
    )
    top_memories: list[dict[str, Any]] = []
    for row in sorted_frame.head(5).to_dict(orient="records"):
        top_memories.append(
            {
                "asof_date": _json_ready(row.get("asof_date")),
                "memory_id": _text(row.get("memory_id")),
                "symbol": _text(row.get("symbol")),
                "sector_code": _text(row.get("sector_code")),
                "sector_name": _text(row.get("sector_name")),
                "source": _text(row.get("context_source")),
                "event_type": _text(row.get("event_type")),
                "context_class": _text(row.get("context_class")),
                "direction": _text(row.get("direction")),
                "event_state": _text(row.get("event_state")),
                "decayed_pressure_score": _num(row.get("decayed_pressure_score"), default=0.0),
                "freshness_days": _num(row.get("freshness_days"), default=0.0),
                "contradiction_state": _text(row.get("contradiction_state")),
                "policy_effect": _text(row.get("policy_effect")) or "memory_only_no_trade_authority",
            }
        )
    positive_mask = directions.eq("positive") | states.eq("positive_watch_pressure")
    negative_mask = directions.eq("negative") | states.eq("negative_derisk_pressure")
    mixed_mask = directions.eq("mixed") | states.eq("mixed_context")
    return {
        "schema_version": 1,
        "authority_scope": "review_input_only",
        "action_policy_effect": "memory_context_only_no_trade_authority",
        "portfolio_authority": "none",
        "broker_execution_allowed": False,
        "row_count": int(len(frame)),
        "positive_count": int(positive_mask.sum()),
        "negative_count": int(negative_mask.sum()),
        "mixed_count": int(mixed_mask.sum()),
        "contradiction_count": int(contradictions.ne("none").sum()),
        "source_family_counts": frame.get("context_source", pd.Series(dtype="object")).dropna().astype(str).value_counts().to_dict(),
        "max_decayed_pressure_score": None if pressure.dropna().empty else float(pressure.max()),
        "top_memories": top_memories,
    }


def load_company_memory_context(
    symbol: str,
    *,
    asof_date: pd.Timestamp,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict[str, Any]:
    symbol = symbol.strip().upper()
    context: dict[str, Any] = {"symbol": symbol, "asof_date": asof_date.isoformat(), "lookback_days": int(lookback_days)}

    context["technical"] = _records(
        _load_table_rows(
            TECHNICAL_TABLE,
            symbol=symbol,
            asof_date=asof_date,
            date_column="asof_date",
            columns=[
                "asof_date",
                "adj_close",
                "dma_20",
                "dma_50",
                "dma_200",
                "avg_traded_value_20d",
                "rs_vs_benchmark",
                "rs_vs_sector",
                "dist_52w_high",
                "breakout_extension_pct",
                "sector_code",
            ],
            lookback_days=lookback_days,
            limit=1,
        )
    )
    context["candidates"] = _records(
        _load_table_rows(
            CANDIDATES_TABLE,
            symbol=symbol,
            asof_date=asof_date,
            date_column="asof_date",
            columns=[
                "asof_date",
                "setup_id",
                "setup_name",
                "candidate_state",
                "technical_state",
                "technical_trigger_type",
                "technical_trigger_note",
                "technical_score",
                "setup_score",
                "watch_reason_detail",
                "invalidation_price",
                "entry_note",
            ],
            lookback_days=lookback_days,
            limit=5,
        )
    )
    announcement_rows = _filter_review_ready_announcement_evidence(
        _load_table_rows(
            ANNOUNCEMENT_EVIDENCE_TABLE,
            symbol=symbol,
            asof_date=asof_date + pd.Timedelta(days=1),
            date_column="published_on",
            columns=[
                "published_on",
                "unique_id",
                "subject",
                "filed_under_category",
                "source_reliability",
                "event_class",
                "direction",
                "materiality",
                "confidence",
                "verdict",
                "evidence_summary",
                "what_happened",
                "rationale",
            ],
            optional_columns=[
                "announcement_storage_form",
                "announcement_storage_reason",
                "llm_evidence_mode",
                "llm_review_ready",
                "raw_archive_required",
            ],
            lookback_days=lookback_days,
            limit=8,
        )
    )
    context["announcement_evidence"] = _records(announcement_rows)
    context["bhavcopy_evidence"] = _records(
        _load_table_rows(
            BHAVCOPY_EVIDENCE_TABLE,
            symbol=symbol,
            asof_date=asof_date,
            date_column="asof_date",
            columns=[
                "asof_date",
                "close",
                "daily_return",
                "turnover_value_inr",
                "avg_turnover_value_20d",
                "deal_net_value_inr",
                "short_selling_quantity",
                "circuit_hit_count",
                "deal_pressure",
                "evidence_score",
                "evidence_summary",
            ],
            lookback_days=lookback_days,
            limit=10,
        )
    )
    context["event_policy"] = _records(
        _load_table_rows(
            EVENT_POLICY_TABLE,
            symbol=symbol,
            asof_date=asof_date + pd.Timedelta(days=1),
            date_column="asof_date",
            columns=[
                "asof_date",
                "published_on",
                "unique_id",
                "action_type",
                "policy_class",
                "event_class",
                "policy_score",
                "confidence",
                "action_reason",
                "llm_operator_summary",
                "llm_possible_action",
                "llm_event_to_wait_for",
            ],
            lookback_days=lookback_days,
            limit=8,
        )
    )
    context["actions"] = _records(
        _load_table_rows(
            ACTIONS_TABLE,
            symbol=symbol,
            asof_date=asof_date,
            date_column="asof_date",
            columns=[
                "asof_date",
                "action_code",
                "action_source",
                "action_reason",
                "recommended_stop_price",
                "recommended_target_price",
                "expected_horizon_days",
                "invest_score_pct",
                "reason_contract_status",
            ],
            lookback_days=lookback_days,
            limit=5,
        )
    )
    context["wait_signals"] = _records(
        _load_wait_signal_rows(
            symbol=symbol,
            asof_date=asof_date + pd.Timedelta(days=1),
            lookback_days=lookback_days,
            limit=8,
        )
    )
    context["context_overlays"] = load_company_context_overlays(
        symbol=symbol,
        asof_date=asof_date,
        symbol_context=context,
        lookback_days=min(int(lookback_days), int(DEFAULT_CONTEXT_OVERLAY_LOOKBACK_DAYS)),
        limit_per_source=8,
    )
    context["context_overlay_reliability"] = load_company_context_overlay_reliability(asof_date=asof_date)
    context["context_overlay_summary"] = summarize_company_context_overlays(
        context["context_overlays"],
        source_family_reliability=context["context_overlay_reliability"],
    )
    context["causal_event_memory"] = load_company_causal_event_memory(
        symbol=symbol,
        asof_date=asof_date,
        symbol_context=context,
        lookback_days=min(int(lookback_days), int(DEFAULT_CAUSAL_EVENT_MEMORY_LOOKBACK_DAYS)),
        limit=8,
    )
    context["causal_event_memory_summary"] = summarize_company_causal_event_memory(context["causal_event_memory"])
    context["evidence_source_contract"] = build_evidence_source_contract(context)
    return context


def build_evidence_source_contract(context: dict[str, Any]) -> dict[str, Any]:
    sources = {
        "announcement_evidence": {
            "source_table": ANNOUNCEMENT_EVIDENCE_TABLE,
            "purpose": "compact_event_context",
            "required_for_confident_upgrade": True,
        },
        "bhavcopy_evidence": {
            "source_table": BHAVCOPY_EVIDENCE_TABLE,
            "purpose": "compact_market_participation_context",
            "required_for_confident_upgrade": True,
        },
        "event_policy": {
            "source_table": EVENT_POLICY_TABLE,
            "purpose": "deterministic_event_policy_context",
            "required_for_confident_upgrade": False,
        },
        "technical": {
            "source_table": TECHNICAL_TABLE,
            "purpose": "technical_state_context",
            "required_for_confident_upgrade": True,
        },
        "actions": {
            "source_table": ACTIONS_TABLE,
            "purpose": "latest_consolidated_action_context",
            "required_for_confident_upgrade": False,
        },
        "wait_signals": {
            "source_table": WAIT_SIGNALS_TABLE,
            "purpose": "operator_wait_condition_context",
            "required_for_confident_upgrade": False,
        },
        "context_overlays": {
            "source_table": ",".join(
                [
                    ANNOUNCEMENT_CONTEXT_OVERLAYS_TABLE,
                    EXCHANGE_CONTEXT_OVERLAYS_TABLE,
                    BHAVCOPY_CONTEXT_OVERLAYS_TABLE,
                    THEME_CONTEXT_OVERLAYS_TABLE,
                    MACRO_CONTEXT_OVERLAYS_TABLE,
                ]
            ),
            "purpose": "review_only_context_pressure",
            "required_for_confident_upgrade": False,
        },
        "causal_event_memory": {
            "source_table": CAUSAL_EVENT_MEMORY_TABLE,
            "purpose": "merged_event_state_context",
            "required_for_confident_upgrade": False,
        },
    }
    rows: list[dict[str, Any]] = []
    missing_required: list[str] = []
    for key, metadata in sources.items():
        source_rows = context.get(key) or []
        count = len(source_rows)
        status = "present" if count > 0 else "missing"
        row = {
            "source": key,
            "source_table": metadata["source_table"],
            "purpose": metadata["purpose"],
            "row_count": int(count),
            "status": status,
            "required_for_confident_upgrade": bool(metadata["required_for_confident_upgrade"]),
        }
        if key == "announcement_evidence":
            storage_counts: dict[str, int] = {}
            llm_ready_count = 0
            for item in source_rows:
                if not isinstance(item, dict):
                    continue
                storage_form = str(item.get("announcement_storage_form") or "unknown")
                storage_counts[storage_form] = storage_counts.get(storage_form, 0) + 1
                if str(item.get("llm_review_ready", True)).strip().lower() not in {"0", "false", "f", "no", "n"}:
                    llm_ready_count += 1
            row["storage_form_counts"] = storage_counts
            row["llm_review_ready_count"] = int(llm_ready_count)
            row["archive_only_rows_excluded"] = True
        rows.append(row)
        if status == "missing" and row["required_for_confident_upgrade"]:
            missing_required.append(key)
    return {
        "schema_version": 1,
        "authority_scope": "review_input_only",
        "uses_compact_evidence": True,
        "raw_announcement_scan_allowed": False,
        "raw_bhavcopy_scan_allowed": False,
        "context_overlay_policy_effect": "review_input_only_no_trade_authority",
        "causal_event_memory_policy_effect": "memory_context_only_no_trade_authority",
        "sources": rows,
        "missing_required_sources": missing_required,
        "coverage_status": "complete" if not missing_required else "partial",
        "operator_note": (
            "Company-memory review used compact point-in-time evidence stores. "
            "Missing required sources should keep upgrades conservative and review-only."
        ),
    }


def _has_positive_event(context: dict[str, Any]) -> bool:
    for row in context.get("announcement_evidence") or []:
        if str(row.get("direction") or "").lower() == "positive" and _num(row.get("confidence")) >= 0.55:
            return True
    for row in context.get("event_policy") or []:
        if str(row.get("action_type") or "").upper() in {"BUY_WATCH", "NO_ACTION"} and _num(row.get("policy_score")) > 0:
            return True
    overlay_summary = context.get("context_overlay_summary") if isinstance(context.get("context_overlay_summary"), dict) else {}
    positive_count = int(overlay_summary.get("effective_positive_count", overlay_summary.get("positive_count") or 0) or 0)
    negative_count = int(overlay_summary.get("effective_negative_count", overlay_summary.get("negative_count") or 0) or 0)
    if positive_count > 0 and negative_count == 0:
        return True
    memory_summary = context.get("causal_event_memory_summary") if isinstance(context.get("causal_event_memory_summary"), dict) else {}
    memory_positive = int(memory_summary.get("positive_count") or 0)
    memory_negative = int(memory_summary.get("negative_count") or 0)
    memory_mixed = int(memory_summary.get("mixed_count") or 0) + int(memory_summary.get("contradiction_count") or 0)
    if memory_positive > 0 and memory_negative == 0 and memory_mixed == 0:
        return True
    return False


def _has_negative_event(context: dict[str, Any]) -> bool:
    for row in context.get("announcement_evidence") or []:
        if str(row.get("direction") or "").lower() == "negative" and _num(row.get("confidence")) >= 0.55:
            return True
    for row in context.get("event_policy") or []:
        if str(row.get("action_type") or "").upper() in {"REDUCE_EXPOSURE_REVIEW"}:
            return True
    overlay_summary = context.get("context_overlay_summary") if isinstance(context.get("context_overlay_summary"), dict) else {}
    if int(overlay_summary.get("effective_negative_count", overlay_summary.get("negative_count") or 0) or 0) > 0:
        return True
    memory_summary = context.get("causal_event_memory_summary") if isinstance(context.get("causal_event_memory_summary"), dict) else {}
    if int(memory_summary.get("negative_count") or 0) > 0:
        return True
    return False


def deterministic_review(context: dict[str, Any]) -> CompanyMemoryReview:
    latest_action = (context.get("actions") or [{}])[0]
    latest_candidate = (context.get("candidates") or [{}])[0]
    latest_technical = (context.get("technical") or [{}])[0]
    latest_bhav = (context.get("bhavcopy_evidence") or [{}])[0]
    action_code = str(latest_action.get("action_code") or "").upper()
    # candidate technical_score/setup_score are 0-1 (rule_engine normalizes technical_total_score/100);
    # scale to the 0-100 conviction/threshold scale. to_100 passes >1 values through, so any caller that
    # still supplies a 0-100 value is handled too. See advisory/score_scales.py.
    technical_strength = to_100(max(_num(latest_candidate.get("technical_score")), _num(latest_candidate.get("setup_score"))))
    rs_benchmark = _num(latest_technical.get("rs_vs_benchmark"))
    deal_pressure = str(latest_bhav.get("deal_pressure") or "neutral")
    positive_event = _has_positive_event(context)
    negative_event = _has_negative_event(context)

    signal = "NO_ACTION"
    confidence = 0.35
    conviction = max(0.0, min(100.0, technical_strength))
    evidence: list[str] = []
    risks: list[str] = []
    wait_for: list[str] = []
    source_contract = context.get("evidence_source_contract") if isinstance(context.get("evidence_source_contract"), dict) else {}
    missing_required_sources = [str(item) for item in source_contract.get("missing_required_sources") or []]
    overlay_summary = context.get("context_overlay_summary") if isinstance(context.get("context_overlay_summary"), dict) else {}
    memory_summary = context.get("causal_event_memory_summary") if isinstance(context.get("causal_event_memory_summary"), dict) else {}

    if action_code in {"SELL", "PARTIAL_SELL", "REDUCE_EXPOSURE_REVIEW", "BUY", "BUY_MORE", "HOLD", "WATCH"}:
        signal = "SELL_PARTIAL" if action_code == "PARTIAL_SELL" else action_code
        if action_code == "REDUCE_EXPOSURE_REVIEW":
            signal = "SELL_PARTIAL"
        evidence.append(f"latest consolidated action is {action_code}")
        confidence = 0.55
    if negative_event:
        signal = "SELL_PARTIAL" if signal in {"BUY", "BUY_MORE", "HOLD"} else "WATCH"
        risks.append("recent negative event-policy or announcement evidence needs review")
        confidence = max(confidence, 0.60)
    elif technical_strength >= 78 and positive_event:
        # Review-only: company-memory review is a watch/de-risk input, never a broker-capable BUY
        # (authority boundary; action_recommender demotes any BUY from here anyway). A strong technical
        # score is also the wrong-pond signal on its own (docs/specs/discovery_engine.md 9); with a
        # catalyst it is at most a WATCH here.
        signal = "WATCH" if signal in {"NO_ACTION", "HOLD"} else signal
        evidence.append("technical score is strong and recent event evidence is positive")
        confidence = max(confidence, 0.65)
    elif technical_strength >= 65:
        signal = "WATCH" if signal == "NO_ACTION" else signal
        evidence.append("technical score is constructive but not enough for an independent buy")
        wait_for.append("wait for confirmed breakout/retest or stronger event confirmation")
        confidence = max(confidence, 0.50)

    if int(overlay_summary.get("effective_positive_count", overlay_summary.get("positive_count") or 0) or 0) > 0:
        evidence.append("recent context-overlay pressure is positive or watch-supportive")
    if int(overlay_summary.get("effective_negative_count", overlay_summary.get("negative_count") or 0) or 0) > 0:
        risks.append("recent context-overlay pressure is negative and review-only")
    if int(overlay_summary.get("reliability_suppressed_count") or 0) > 0:
        risks.append("some context-overlay rows were ignored because family/class reliability is noisy or harmful")
    if overlay_summary.get("row_count"):
        evidence.append("company-memory review included review-only context overlays")
    if int(memory_summary.get("positive_count") or 0) > 0:
        evidence.append("causal event memory has current positive/watch event state")
    if int(memory_summary.get("negative_count") or 0) > 0:
        risks.append("causal event memory has current negative/de-risk event state")
    if int(memory_summary.get("contradiction_count") or 0) > 0 or int(memory_summary.get("mixed_count") or 0) > 0:
        risks.append("causal event memory contains mixed or contradictory event state")
    if memory_summary.get("row_count"):
        evidence.append("company-memory review included merged causal event memory")

    if deal_pressure == "distribution_or_pressure":
        risks.append("bhavcopy evidence shows distribution or short-selling pressure")
        if signal in {"BUY", "BUY_MORE"}:
            signal = "WATCH"
    elif deal_pressure == "accumulation":
        evidence.append("bhavcopy evidence shows accumulation pressure")
    if rs_benchmark < -0.05:
        risks.append("relative strength versus benchmark is weak")
    if not context.get("announcement_evidence"):
        wait_for.append("wait for fresh company-specific announcement/news evidence before upgrading confidence")
    if missing_required_sources:
        risks.append(f"company-memory evidence coverage is partial; missing {', '.join(missing_required_sources)}")

    summary = f"{context['symbol']} memory review suggests {signal}; confidence {confidence:.0%}."
    thesis = " | ".join(evidence or ["No strong compact company-memory signal was found; keep deterministic policy authoritative."])
    return CompanyMemoryReview(
        recommended_signal=signal,  # type: ignore[arg-type]
        confidence=round(min(1.0, confidence), 4),
        conviction_score=round(conviction, 2),
        summary=summary,
        thesis=thesis,
        risk_flags=risks[:8],
        evidence_used=evidence[:12],
        wait_for=wait_for[:8],
    )


def llm_review(context: dict[str, Any], *, model: str) -> CompanyMemoryReview:
    prompt = (
        "You are reviewing compact point-in-time company memory for an Indian equity. "
        "Return a review input only, not an executable trading instruction. "
        "The deterministic action engine remains authoritative. "
        "Use BUY/BUY_MORE/HOLD/WATCH/SELL_PARTIAL/SELL/NO_ACTION only when justified by the evidence.\n\n"
        f"Company memory JSON:\n{json.dumps(_json_ready(context), ensure_ascii=False, indent=2, default=str)}"
    )
    return run_codex_structured(prompt, response_model=CompanyMemoryReview, model=model, system_prompt="Return only the requested structured company-memory review.")


def build_review_row(
    *,
    review_date: pd.Timestamp,
    symbol: str,
    context: dict[str, Any],
    model: str,
    use_llm: bool,
) -> dict[str, Any]:
    fallback_used = False
    error: str | None = None
    review_status = "completed"
    try:
        review = llm_review(context, model=model) if use_llm else deterministic_review(context)
    except Exception as exc:
        fallback_used = True
        error = f"{type(exc).__name__}: {exc}"
        review_status = "fallback_completed"
        record_fallback_event(
            module="advisory.company_memory_review",
            source="company_memory_review",
            fallback_type="llm_deterministic_fallback",
            severity="warn",
            symbol=context.get("symbol"),
            reason="Company-memory LLM review failed; deterministic review was used.",
            deterministic_fallback=True,
            error=exc,
            metadata={"model": model},
        )
        review = deterministic_review(context)

    return {
        "review_date": review_date,
        "symbol": symbol.upper(),
        "recommended_signal": review.recommended_signal,
        "confidence": float(review.confidence),
        "conviction_score": float(review.conviction_score),
        "summary": review.summary,
        "thesis": review.thesis,
        "risk_flags_json": json_dumps(review.risk_flags),
        "evidence_used_json": json_dumps(review.evidence_used),
        "wait_for_json": json_dumps(review.wait_for),
        "deterministic_boundary": review.deterministic_boundary,
        "authority_scope": "review_input_only",
        "model_name": model if use_llm else "deterministic_company_memory_v1",
        "prompt_id": PROMPT_ID,
        "prompt_version": PROMPT_VERSION,
        "prompt_schema_version": PROMPT_SCHEMA_VERSION,
        "review_status": review_status,
        "fallback_used": bool(fallback_used),
        "error": error,
        "payload_json": json_dumps(context),
        "load_ts": pd.Timestamp.utcnow(),
    }


def build_company_memory_reviews(
    *,
    asof_date: Any = None,
    symbols: list[str] | None = None,
    limit: int = DEFAULT_MAX_SYMBOLS,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    model: str = DEFAULT_MODEL,
    use_llm: bool = LLM_ENABLED,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    review_date = _asof(asof_date)
    review_symbols = load_review_symbols(asof_date=review_date, symbols=symbols, limit=limit)
    rows: list[dict[str, Any]] = []
    for symbol in review_symbols:
        context = load_company_memory_context(symbol, asof_date=review_date, lookback_days=lookback_days)
        rows.append(build_review_row(review_date=review_date, symbol=symbol, context=context, model=model, use_llm=use_llm))
    df = pd.DataFrame(rows)
    meta = {
        "status": "ok",
        "review_date": review_date.isoformat(),
        "symbol_count": int(len(review_symbols)),
        "review_rows": int(len(df)),
        "llm_enabled": bool(use_llm),
        "model": model if use_llm else "deterministic_company_memory_v1",
        "authority_scope": "review_input_only",
        "table": TABLE_NAME,
    }
    return df, meta


def persist_company_memory_reviews(reviews: pd.DataFrame) -> None:
    ensure_table()
    if reviews.empty:
        return
    out = reviews.copy()
    out["review_date"] = pd.to_datetime(out["review_date"], utc=True, errors="coerce")
    out["load_ts"] = pd.to_datetime(out["load_ts"], utc=True, errors="coerce")
    for column in ["confidence", "conviction_score"]:
        out[column] = pd.to_numeric(out[column], errors="coerce")
    out["fallback_used"] = out["fallback_used"].astype("boolean")
    upsert_to_db(out, TABLE_NAME, unique_keys=["review_date", "symbol"])


def summarize(reviews: pd.DataFrame, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        **meta,
        "signal_counts": reviews["recommended_signal"].value_counts(dropna=False).to_dict() if not reviews.empty else {},
        "sample": _records(reviews, limit=10),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build review-only company-memory signal summaries from compact evidence.")
    parser.add_argument("--date", type=parse_datetime_arg, help="Review date, defaults to today UTC")
    parser.add_argument("--symbols", nargs="*", help="Optional symbols")
    parser.add_argument("--limit", type=int, default=DEFAULT_MAX_SYMBOLS)
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--llm", action="store_true", help="Use Codex structured review instead of deterministic V1")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    reviews, meta = build_company_memory_reviews(
        asof_date=args.date,
        symbols=args.symbols,
        limit=int(args.limit),
        lookback_days=int(args.lookback_days),
        model=str(args.model),
        use_llm=bool(args.llm),
    )
    if not args.dry_run:
        persist_company_memory_reviews(reviews)
    result = summarize(reviews, meta)
    result["dry_run"] = bool(args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
