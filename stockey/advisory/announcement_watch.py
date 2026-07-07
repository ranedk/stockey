from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import timedelta
from typing import Any

import pandas as pd
from environs import Env

from advisory.fallback_telemetry import record_local_fallback_event
from advisory.watchlist_builder import (
    WATCHLIST_CONTEXT_OVERLAY_EXCLUDED_RELIABILITY_CLASSES,
    load_context_family_reliability,
)
from advisory.event_evidence_store import ANNOUNCEMENT_CONTEXT_OVERLAYS_TABLE, BHAVCOPY_CONTEXT_OVERLAYS_TABLE
from advisory.macro_context_overlays import MACRO_CONTEXT_OVERLAYS_TABLE
from advisory.news_theme_engine import THEME_CONTEXT_OVERLAYS_TABLE, theme_sector_alias_values_sql
from data.announcements.managed_pipeline import ManagedAnnouncementPipeline
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration
from utils.sync import parse_datetime_arg


env = Env()
env.read_env()
WATCHLIST_TABLE = "advisory_watchlist"
EVENTS_TABLE = "advisory_watch_events"
WATCH_OUTPUTS_SCHEMA_MIGRATION_ID = "20260611_advisory_announcement_watch_outputs_base"
MARKET_CONTEXT_UNIVERSE_TABLE = "advisory_market_context_universe_daily"
MARKET_CONTEXT_SETUP_ID = "MARKET_CONTEXT_TOP50"
THEME_CONTEXT_SETUP_PREFIX = "THEME_CONTEXT"
ANNOUNCEMENT_CONTEXT_SETUP_PREFIX = "ANNOUNCEMENT_CONTEXT"
MACRO_CONTEXT_SETUP_PREFIX = "MACRO_CONTEXT"
BHAVCOPY_CONTEXT_SETUP_PREFIX = "BHAVCOPY_CONTEXT"
INITIAL_INGEST_LOOKBACK_DAYS = 3
# Overlap re-scanned before the durable watermark so a late/amended filing near the
# last checkpoint is not skipped. Matches the historical `last_checked - 1 day` logic.
INGEST_OVERLAP_DAYS = env.int("ANNOUNCEMENT_INGEST_OVERLAP_DAYS", default=1)
# EOD full-ingest age gate for shelf-tier watch rows: shelf names still ingest, just at
# most once per this many days; active tier ingests every run. Operational attention only.
SHELF_INGEST_MAX_AGE_DAYS = env.int("SHELF_INGEST_MAX_AGE_DAYS", default=2)
DEFAULT_MARKET_CONTEXT_WATCH_LIMIT = env.int("MARKET_CONTEXT_WATCH_LIMIT", default=50)
DEFAULT_THEME_CONTEXT_WATCH_LIMIT = env.int("THEME_CONTEXT_WATCH_LIMIT", default=50)
DEFAULT_ANNOUNCEMENT_CONTEXT_WATCH_LIMIT = env.int("ANNOUNCEMENT_CONTEXT_WATCH_LIMIT", default=50)
DEFAULT_ANNOUNCEMENT_CONTEXT_LOOKBACK_DAYS = env.int("ANNOUNCEMENT_CONTEXT_LOOKBACK_DAYS", default=7)
DEFAULT_MACRO_CONTEXT_WATCH_LIMIT = env.int("MACRO_CONTEXT_WATCH_LIMIT", default=50)
DEFAULT_BHAVCOPY_CONTEXT_WATCH_LIMIT = env.int("BHAVCOPY_CONTEXT_WATCH_LIMIT", default=50)
MATERIAL_EVENT_KEYWORDS = {
    "acquisition",
    "amalgamation",
    "approval",
    "arbitration",
    "bankruptcy",
    "block deal",
    "board meeting",
    "bonus",
    "buyback",
    "capex",
    "cbi",
    "ceo",
    "cfo",
    "chairman",
    "change in management",
    "commercial production",
    "contract",
    "credit rating",
    "default",
    "demerger",
    "director resignation",
    "dividend",
    "downgrade",
    "earnings",
    "enforcement directorate",
    "expansion",
    "fund raise",
    "fraud",
    "guidance",
    "income tax",
    "insolvency",
    "investigation",
    "joint venture",
    "large order",
    "license",
    "litigation",
    "merger",
    "nclt",
    "notice",
    "order win",
    "penalty",
    "pledge",
    "promoter",
    "qip",
    "quarterly results",
    "raid",
    "rating",
    "regulatory",
    "resignation",
    "results",
    "rights issue",
    "sebi",
    "split",
    "stake sale",
    "scheme of arrangement",
    "shutdown",
    "slump sale",
    "tax demand",
    "termination",
    "upgrade",
}
STRONG_MATERIAL_EVENT_KEYWORDS = MATERIAL_EVENT_KEYWORDS - {
    "board meeting",
    "notice",
    "results",
}
NON_MATERIAL_CONTEXT_PHRASES = {
    "analyst meeting",
    "closure of trading window",
    "compliance certificate",
    "copy of newspaper publication",
    "duplicate share certificate",
    "investor presentation",
    "investor/analyst meet",
    "intimation of analyst",
    "intimation of board meeting",
    "loss of share certificate",
    "newspaper advertisement",
    "newspaper publication",
    "no material trading information",
    "no material information",
    "non material",
    "non-material",
    "not material",
    "press release on newspaper publication",
    "record date for dividend already declared",
    "routine newspaper notice",
    "secretarial compliance report",
    "trading window closure",
    "transcript of earnings call",
}
WATCHLIST_EXTRA_COLUMNS = {
    "setup_name": "TEXT",
    "regime_name": "TEXT",
    "company_master_id": "TEXT",
    "screener_slug": "TEXT",
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
WATCH_OUTPUTS_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {WATCHLIST_TABLE} (
        asof_date TIMESTAMPTZ NOT NULL,
        setup_id TEXT NOT NULL,
        setup_name TEXT,
        regime_name TEXT,
        symbol TEXT NOT NULL,
        company_master_id TEXT,
        screener_slug TEXT,
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
        f"ALTER TABLE {WATCHLIST_TABLE} ADD COLUMN IF NOT EXISTS {column} {sql_type}"
        for column, sql_type in WATCHLIST_EXTRA_COLUMNS.items()
    ],
    f"""
    CREATE TABLE IF NOT EXISTS {EVENTS_TABLE} (
        published_on TIMESTAMPTZ NOT NULL,
        asof_date TIMESTAMPTZ,
        setup_id TEXT NOT NULL,
        setup_name TEXT,
        symbol TEXT NOT NULL,
        company_master_id TEXT,
        unique_id TEXT NOT NULL,
        exchange TEXT,
        subject TEXT,
        filed_under_category TEXT,
        parse_status TEXT,
        concise_summary_text TEXT,
        categories_json TEXT,
        watch_reasons_json TEXT,
        monitor_source TEXT,
        event_status TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (published_on, setup_id, symbol, unique_id)
    )
    """,
    f"ALTER TABLE {EVENTS_TABLE} ADD COLUMN IF NOT EXISTS monitor_source TEXT",
]


def normalize_timestamp(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce").dt.normalize()


def _emit_progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def inclusive_end_of_day(ts: pd.Timestamp | None) -> pd.Timestamp | None:
    if ts is None:
        return None
    value = pd.to_datetime(ts, utc=True, errors="coerce")
    if pd.isna(value):
        return None
    return value.normalize() + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)


def ensure_watch_outputs_tables() -> None:
    apply_schema_migration(
        migration_id=WATCH_OUTPUTS_SCHEMA_MIGRATION_ID,
        description="Create advisory announcement watchlist and event output tables.",
        statements=WATCH_OUTPUTS_SCHEMA_STATEMENTS,
        metadata={"tables": [WATCHLIST_TABLE, EVENTS_TABLE]},
    )


def load_watchlist(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
) -> pd.DataFrame:
    clauses = ["COALESCE(watch_enabled, TRUE) = TRUE", "COALESCE(watch_status, 'active') IN ('active', 'review_manual')"]
    params: list[object] = []
    if asof_date is not None:
        clauses.append("asof_date = %s")
        params.append(asof_date)
    else:
        clauses.append("asof_date = (SELECT MAX(asof_date) FROM advisory_watchlist)")
    if symbols:
        clauses.append("symbol = ANY(%s)")
        params.append([value.upper() for value in symbols])
    if setup_ids:
        clauses.append("setup_id = ANY(%s)")
        params.append([value.upper() for value in setup_ids])
    try:
        df = sql_to_df(
            f"""
            SELECT *
            FROM {WATCHLIST_TABLE}
            WHERE {' AND '.join(clauses)}
            ORDER BY setup_id, symbol
            """,
            params=tuple(params) if params else None,
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.announcement_watch",
            source=WATCHLIST_TABLE,
            fallback_type="announcement_watchlist_load_failed",
            severity="error",
            reason="Announcement watcher could not load the active watchlist and continued with an empty watchlist.",
            error=exc,
            metadata={
                "asof_date": None if asof_date is None else str(asof_date),
                "symbols_count": len(symbols or []),
                "setup_ids_count": len(setup_ids or []),
            },
        )
        return pd.DataFrame()
    if df.empty:
        return df
    df["asof_date"] = normalize_timestamp(df["asof_date"])
    df["symbol"] = df["symbol"].astype("string").str.upper()
    df["monitor_source"] = "watchlist"
    return df


def _table_exists(table_name: str) -> bool:
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
        record_local_fallback_event(
            module="advisory.announcement_watch",
            source=table_name,
            fallback_type="announcement_watch_table_lookup_failed",
            severity="warn",
            reason="Announcement watcher could not inspect source table availability and treated the table as unavailable.",
            error=exc,
            metadata={"table_name": table_name},
        )
        return False
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
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.announcement_watch",
            source=table_name,
            fallback_type="announcement_watch_table_columns_lookup_failed",
            severity="warn",
            reason="Announcement watcher could not inspect optional source table columns and used compatibility mode.",
            error=exc,
            metadata={"table_name": table_name},
        )
        return set()
    if df.empty or "column_name" not in df.columns:
        return set()
    return {str(value) for value in df["column_name"].dropna().tolist()}


def _is_missing_scalar(value: object) -> bool:
    return value is None or (pd.api.types.is_scalar(value) and pd.isna(value))


def _context_family_from_monitor_source(value: object) -> str | None:
    if _is_missing_scalar(value):
        return None
    text = str(value).strip().lower()
    if text in {"theme_context", "announcement_context", "macro_context", "bhavcopy_context"}:
        return text
    return None


def _parse_context_watch_reasons(value: object) -> list[dict[str, object]]:
    if _is_missing_scalar(value):
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        record_local_fallback_event(
            module="advisory.announcement_watch",
            fallback_type="announcement_watch_context_watch_reasons_parse_failed",
            source="watch_reasons_json",
            severity="warn",
            reason="Announcement watcher could not parse context watch reasons and ignored the malformed context-class payload.",
            error=exc,
            metadata={"value_type": value.__class__.__name__},
        )
        return []
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    if isinstance(parsed, dict):
        return [parsed]
    return []


def _context_class_from_row(row: pd.Series) -> str | None:
    for column in ["context_class", "event_class", "theme_id", "macro_signal_id", "deal_pressure"]:
        if column in row.index:
            value = row.get(column)
            if _is_missing_scalar(value):
                continue
            text = str(value).strip().upper()
            if text and text not in {"<NA>", "NAN", "NONE"}:
                return text
    for column in ["watch_reasons_json", "watch_reasons"]:
        if column not in row.index:
            continue
        for item in _parse_context_watch_reasons(row.get(column)):
            for key in ["context_class", "class", "event_class", "theme_id", "macro_signal_id", "deal_pressure"]:
                value = item.get(key)
                text = str(value or "").strip().upper()
                if text and text not in {"<NA>", "NAN", "NONE"}:
                    return text
    return None


def _context_class_reliability_classification(family_row: dict[str, object] | None, context_class: str | None) -> str | None:
    if not isinstance(family_row, dict) or not context_class:
        return None
    target_class = str(context_class).strip().upper()
    for item in family_row.get("context_class_diagnostics") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("context_class") or "").strip().upper() != target_class:
            continue
        classification = str(item.get("classification") or "").strip()
        return classification or None
    return None


def _sector_key_from_values(*values: object) -> str | None:
    for value in values:
        if pd.api.types.is_scalar(value) and pd.isna(value):
            continue
        text = str(value or "").strip()
        if not text or text.upper() in {"<NA>", "NAN", "NONE"}:
            continue
        return "".join(ch for ch in text.upper() if ch.isalnum())
    return None


def _context_sector_values_from_row(row: pd.Series) -> tuple[object | None, object | None]:
    sector_name_values: list[object] = []
    sector_code_values: list[object] = []
    for column in ["universe_sector_name", "overlay_sector_name", "sector_name"]:
        if column in row.index:
            sector_name_values.append(row.get(column))
    for column in ["universe_sector_code", "overlay_sector_code", "sector_code"]:
        if column in row.index:
            sector_code_values.append(row.get(column))
    for column in ["watch_reasons_json", "watch_reasons"]:
        if column not in row.index:
            continue
        for item in _parse_context_watch_reasons(row.get(column)):
            for key in ["universe_sector_name", "overlay_sector_name", "sector_name"]:
                if key in item:
                    sector_name_values.append(item.get(key))
            for key in ["universe_sector_code", "overlay_sector_code", "sector_code"]:
                if key in item:
                    sector_code_values.append(item.get(key))
    return (
        next((value for value in sector_name_values if _sector_key_from_values(value)), None),
        next((value for value in sector_code_values if _sector_key_from_values(value)), None),
    )


def _context_sector_reliability_classification(family_row: dict[str, object] | None, row: pd.Series) -> str | None:
    if not isinstance(family_row, dict):
        return None
    sector_name, sector_code = _context_sector_values_from_row(row)
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


def _reliability_families(reliability: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(reliability, dict):
        return {}
    families = reliability.get("families")
    if isinstance(families, dict):
        return families
    if isinstance(families, list):
        return {
            str(item.get("source_family")): item
            for item in families
            if isinstance(item, dict) and item.get("source_family")
        }
    return {}


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


def _context_direction_from_row(row: pd.Series) -> str | None:
    for column in ["direction", "context_direction"]:
        if column in row.index:
            text = str(row.get(column) or "").strip().lower()
            if text in {"positive", "negative", "watch"}:
                return text
    for column in ["candidate_state", "current_state", "setup_id", "watch_status", "monitor_source"]:
        if column not in row.index:
            continue
        text = str(row.get(column) or "").strip().lower()
        if "negative" in text:
            return "negative"
        if "positive" in text:
            return "positive"
        if "watch" in text:
            return "watch"
    for column in ["watch_reasons_json", "watch_reasons"]:
        if column not in row.index:
            continue
        for item in _parse_context_watch_reasons(row.get(column)):
            text = str(item.get("direction") or item.get("context_direction") or "").strip().lower()
            if text in {"positive", "negative", "watch"}:
                return text
    return None


def _runtime_contract_blocks_context_target(row: pd.Series, family_row: dict[str, Any] | None) -> bool:
    if not isinstance(family_row, dict) or not family_row:
        return False
    direction = _context_direction_from_row(row)
    if direction in {"positive", "watch"}:
        return not _runtime_contract_allows_context(
            family_row,
            "watch_priority",
            legacy_classification=str(family_row.get("classification") or "").strip(),
        )
    if direction == "negative":
        if _runtime_contract_allows_context(
            family_row,
            "de_risk_review",
            legacy_classification=str(family_row.get("classification") or "").strip(),
        ):
            return False
        class_row = None
        target_class = row.get("_context_class")
        if target_class:
            target = str(target_class).strip().upper()
            for item in family_row.get("context_class_diagnostics") or []:
                if not isinstance(item, dict):
                    continue
                if str(item.get("context_class") or "").strip().upper() == target:
                    class_row = item
                    break
        return not _runtime_contract_allows_context(
            class_row if isinstance(class_row, dict) else None,
            "de_risk_review",
            legacy_classification=str((class_row or {}).get("classification") or "").strip() if isinstance(class_row, dict) else None,
        )
    return False


def _filter_context_targets_by_reliability(df: pd.DataFrame, *, asof_date: pd.Timestamp | None = None) -> pd.DataFrame:
    if df.empty or "monitor_source" not in df.columns:
        return df
    families = {
        family
        for family in df["monitor_source"].map(_context_family_from_monitor_source).dropna().astype(str).tolist()
        if family
    }
    if not families:
        return df
    try:
        reliability = load_context_family_reliability(asof_date=asof_date)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.announcement_watch",
            source="advisory_context_overlay_reliability_summary",
            fallback_type="announcement_watch_context_reliability_filter_failed",
            severity="warn",
            reason="Announcement/news watcher could not load context source-family reliability and kept context targets neutral.",
            error=exc,
            metadata={
                "asof_date": None if asof_date is None else str(asof_date),
                "families": sorted(families),
            },
        )
        return df
    family_rows = _reliability_families(reliability)
    if not family_rows:
        return df

    out = df.copy()
    out["_context_source_family"] = out["monitor_source"].map(_context_family_from_monitor_source)
    out["_context_reliability_classification"] = out["_context_source_family"].map(
        lambda family: (family_rows.get(str(family)) or {}).get("classification") if family else None
    )
    out["_context_class"] = out.apply(_context_class_from_row, axis=1)
    out["_context_class_reliability_classification"] = out.apply(
        lambda row: _context_class_reliability_classification(
            family_rows.get(str(row.get("_context_source_family"))),
            row.get("_context_class"),
        ),
        axis=1,
    )
    out["_context_sector_reliability_classification"] = out.apply(
        lambda row: _context_sector_reliability_classification(
            family_rows.get(str(row.get("_context_source_family"))),
            row,
        ),
        axis=1,
    )
    mask_excluded = out["_context_reliability_classification"].astype("string").isin(
        WATCHLIST_CONTEXT_OVERLAY_EXCLUDED_RELIABILITY_CLASSES
    ) | out["_context_class_reliability_classification"].astype("string").isin(
        WATCHLIST_CONTEXT_OVERLAY_EXCLUDED_RELIABILITY_CLASSES
    ) | out["_context_sector_reliability_classification"].astype("string").isin(
        WATCHLIST_CONTEXT_OVERLAY_EXCLUDED_RELIABILITY_CLASSES
    )
    mask_runtime_blocked = out.apply(
        lambda row: _runtime_contract_blocks_context_target(
            row,
            family_rows.get(str(row.get("_context_source_family"))),
        ),
        axis=1,
    )
    mask_excluded = mask_excluded | mask_runtime_blocked
    if not mask_excluded.any():
        return out.drop(
            columns=[
                "_context_source_family",
                "_context_reliability_classification",
                "_context_class",
                "_context_class_reliability_classification",
                "_context_sector_reliability_classification",
            ],
            errors="ignore",
        )
    return (
        out.loc[~mask_excluded]
        .drop(
            columns=[
                "_context_source_family",
                "_context_reliability_classification",
                "_context_class",
                "_context_class_reliability_classification",
                "_context_sector_reliability_classification",
            ],
            errors="ignore",
        )
        .reset_index(drop=True)
    )


def load_market_context_watchlist(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    limit: int = DEFAULT_MARKET_CONTEXT_WATCH_LIMIT,
    last_checked_at: pd.Timestamp | None = None,
) -> pd.DataFrame:
    if int(limit) <= 0 or not _table_exists(MARKET_CONTEXT_UNIVERSE_TABLE):
        return pd.DataFrame()
    effective_asof = pd.to_datetime(asof_date or pd.Timestamp.utcnow(), utc=True, errors="coerce").normalize()
    clauses = [
        "asof_date = (SELECT MAX(asof_date) FROM advisory_market_context_universe_daily WHERE asof_date <= %s)",
        "in_top_context = TRUE",
        "NULLIF(TRIM(symbol), '') IS NOT NULL",
        "NULLIF(TRIM(company_master_id), '') IS NOT NULL",
    ]
    params: list[Any] = [effective_asof]
    if symbols:
        clauses.append("UPPER(TRIM(symbol)) = ANY(%s)")
        params.append([str(value).upper() for value in symbols])
    df = sql_to_df(
        f"""
        SELECT
            asof_date,
            %s::text AS setup_id,
            'Top 50%% market context'::text AS setup_name,
            NULL::text AS regime_name,
            UPPER(TRIM(symbol)) AS symbol,
            company_master_id,
            NULL::text AS screener_slug,
            context_rank::bigint AS rank,
            'MARKET_CONTEXT'::text AS candidate_state,
            'MARKET_CONTEXT'::text AS current_state,
            'Top-50 market context intake; cheap materiality filter required before LLM evaluation.'::text AS watch_reason_detail,
            NULL::text AS entry_style,
            NULL::double precision AS attractive_price_low,
            NULL::double precision AS attractive_price_high,
            NULL::double precision AS invalidation_price,
            NULL::text AS entry_note,
            FALSE::boolean AS near_miss_flag,
            NULL::text AS last_event_class,
            NULL::text AS last_state_transition_hint,
            NULL::double precision AS last_event_score_impact,
            TRUE::boolean AS watch_enabled,
            json_build_object(
                'source', 'market_context_top50',
                'context_rank', context_rank,
                'sector_code', sector_code,
                'sector_name', sector_name,
                'technical_leadership_score', technical_leadership_score,
                'macro_sensitivity_tag', macro_sensitivity_tag
            )::text AS watch_reasons_json,
            'market_context'::text AS watch_status,
            asof_date AS state_updated_at,
            asof_date AS watch_started_at,
            %s::timestamptz AS last_checked_at,
            NULL::timestamptz AS last_document_published_on,
            load_ts,
            'market_context'::text AS monitor_source
        FROM {MARKET_CONTEXT_UNIVERSE_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY context_rank ASC, symbol
        LIMIT %s
        """,
        params=(MARKET_CONTEXT_SETUP_ID, last_checked_at, *params, int(limit)),
    )
    if df.empty:
        return df
    for column in ["asof_date", "state_updated_at", "watch_started_at", "last_checked_at", "last_document_published_on", "load_ts"]:
        df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    df["asof_date"] = normalize_timestamp(df["asof_date"])
    df["symbol"] = df["symbol"].astype("string").str.upper()
    df["rank"] = pd.to_numeric(df["rank"], errors="coerce")
    return _filter_context_targets_by_reliability(df, asof_date=effective_asof)


def load_theme_context_watchlist(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    limit: int = DEFAULT_THEME_CONTEXT_WATCH_LIMIT,
    last_checked_at: pd.Timestamp | None = None,
) -> pd.DataFrame:
    if int(limit) <= 0 or not _table_exists(THEME_CONTEXT_OVERLAYS_TABLE) or not _table_exists(MARKET_CONTEXT_UNIVERSE_TABLE):
        return pd.DataFrame()
    effective_asof = pd.to_datetime(asof_date or pd.Timestamp.utcnow(), utc=True, errors="coerce").normalize()
    symbol_clause = ""
    params: dict[str, Any] = {
        "asof_date": effective_asof,
        "last_checked_at": last_checked_at,
        "limit": int(limit),
    }
    if symbols:
        symbol_clause = "AND UPPER(TRIM(u.symbol)) = ANY(%(symbols)s)"
        params["symbols"] = [str(value).upper() for value in symbols]
    try:
        df = sql_to_df(
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
                    WHERE asof_date <= %(asof_date)s
                )
                  AND production_status = 'active'
                  AND authority_scope = 'watchlist_pressure_only'
                  AND direction IN ('positive', 'negative')
                  AND NULLIF(TRIM(COALESCE(sector_name, sector_code, '')), '') IS NOT NULL
            ),
            latest_universe AS (
                SELECT *
                FROM {MARKET_CONTEXT_UNIVERSE_TABLE}
                WHERE asof_date = (
                    SELECT MAX(asof_date)
                    FROM {MARKET_CONTEXT_UNIVERSE_TABLE}
                    WHERE asof_date <= %(asof_date)s
                )
                  AND in_top_context = TRUE
                  AND NULLIF(TRIM(symbol), '') IS NOT NULL
                  AND NULLIF(TRIM(company_master_id), '') IS NOT NULL
            ),
            joined AS (
                SELECT
                    u.asof_date AS universe_asof_date,
                    o.asof_date AS overlay_asof_date,
                    UPPER(TRIM(u.symbol)) AS symbol,
                    u.company_master_id,
                    u.context_rank,
                    u.sector_code AS universe_sector_code,
                    u.sector_name AS universe_sector_name,
                    u.technical_leadership_score,
                    u.macro_sensitivity_tag,
                    o.overlay_id,
                    o.theme_id,
                    o.theme_name,
                    o.sector_name AS overlay_sector_name,
                    o.sector_code AS overlay_sector_code,
                    o.direction,
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
                            o.pressure_score DESC NULLS LAST,
                            CASE WHEN o.direction = 'positive' THEN 0 ELSE 1 END,
                            u.context_rank ASC NULLS LAST,
                            o.theme_id
                    ) AS rn
                FROM latest_universe u
                JOIN latest_overlays o
                  ON TRUE
                LEFT JOIN theme_sector_alias tsa
                  ON tsa.overlay_sector_key = regexp_replace(upper(coalesce(o.sector_name, o.sector_code, '')), '[^A-Z0-9]', '', 'g')
                WHERE (
                    regexp_replace(upper(coalesce(u.sector_name, '')), '[^A-Z0-9]', '', 'g') = regexp_replace(upper(coalesce(o.sector_name, '')), '[^A-Z0-9]', '', 'g')
                    OR regexp_replace(upper(coalesce(u.sector_code, '')), '[^A-Z0-9]', '', 'g') = regexp_replace(upper(coalesce(o.sector_code, '')), '[^A-Z0-9]', '', 'g')
                    OR upper(coalesce(u.sector_code, '')) = tsa.universe_sector_code
                )
                  {symbol_clause}
            )
            SELECT
                overlay_asof_date AS asof_date,
                (%(setup_prefix)s || '_' || theme_id || '_' || upper(direction))::text AS setup_id,
                ('Theme context: ' || theme_name || ' / ' || direction)::text AS setup_name,
                NULL::text AS regime_name,
                symbol,
                company_master_id,
                NULL::text AS screener_slug,
                context_rank::bigint AS rank,
                ('THEME_CONTEXT_' || upper(direction))::text AS candidate_state,
                ('THEME_CONTEXT_' || upper(direction))::text AS current_state,
                CASE
                    WHEN direction = 'positive' THEN 'Active news theme creates positive sector watchlist pressure; technical and liquidity confirmation still required.'
                    ELSE 'Active news theme creates negative sector watchlist pressure; monitor for thesis invalidation, de-risking, or exit evidence.'
                END::text AS watch_reason_detail,
                NULL::text AS entry_style,
                NULL::double precision AS attractive_price_low,
                NULL::double precision AS attractive_price_high,
                NULL::double precision AS invalidation_price,
                NULL::text AS entry_note,
                FALSE::boolean AS near_miss_flag,
                NULL::text AS last_event_class,
                NULL::text AS last_state_transition_hint,
                NULL::double precision AS last_event_score_impact,
                TRUE::boolean AS watch_enabled,
                json_build_object(
                    'source', 'news_theme_context_overlay',
                    'overlay_id', overlay_id,
                    'theme_id', theme_id,
                    'theme_name', theme_name,
                    'direction', direction,
                    'pressure_score', pressure_score,
                    'theme_intensity', theme_intensity,
                    'hit_score', hit_score,
                    'overlay_sector_name', overlay_sector_name,
                    'overlay_sector_code', overlay_sector_code,
                    'universe_sector_name', universe_sector_name,
                    'universe_sector_code', universe_sector_code,
                    'context_rank', context_rank,
                    'technical_leadership_score', technical_leadership_score,
                    'macro_sensitivity_tag', macro_sensitivity_tag,
                    'holding_profile', holding_profile,
                    'risk_level', risk_level,
                    'ideal_screener_logic', ideal_screener_logic,
                    'theme_reason', theme_reason,
                    'invalidation_signals_json', invalidation_signals_json,
                    'matched_sources_json', matched_sources_json,
                    'suggested_screeners_json', suggested_screeners_json,
                    'authority_scope', 'watchlist_pressure_only'
                )::text AS watch_reasons_json,
                'theme_context'::text AS watch_status,
                overlay_asof_date AS state_updated_at,
                overlay_asof_date AS watch_started_at,
                %(last_checked_at)s::timestamptz AS last_checked_at,
                NULL::timestamptz AS last_document_published_on,
                GREATEST(overlay_load_ts, universe_load_ts) AS load_ts,
                'theme_context'::text AS monitor_source
            FROM joined
            WHERE rn = 1
            ORDER BY pressure_score DESC NULLS LAST, context_rank ASC NULLS LAST, symbol
            LIMIT %(limit)s
            """,
            params={**params, "setup_prefix": THEME_CONTEXT_SETUP_PREFIX},
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.announcement_watch",
            source=THEME_CONTEXT_OVERLAYS_TABLE,
            fallback_type="theme_context_watchlist_load_failed",
            severity="warn",
            reason="Announcement/news watcher skipped theme-context watch targets because overlay-to-universe mapping failed.",
            error=exc,
            metadata={
                "asof_date": str(effective_asof),
                "symbols_count": len(symbols or []),
                "limit": int(limit),
            },
        )
        return pd.DataFrame()
    if df.empty:
        return df
    for column in ["asof_date", "state_updated_at", "watch_started_at", "last_checked_at", "last_document_published_on", "load_ts"]:
        df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    df["asof_date"] = normalize_timestamp(df["asof_date"])
    df["symbol"] = df["symbol"].astype("string").str.upper()
    df["rank"] = pd.to_numeric(df["rank"], errors="coerce")
    return _filter_context_targets_by_reliability(df, asof_date=effective_asof)


def load_announcement_context_watchlist(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    limit: int = DEFAULT_ANNOUNCEMENT_CONTEXT_WATCH_LIMIT,
    last_checked_at: pd.Timestamp | None = None,
    lookback_days: int = DEFAULT_ANNOUNCEMENT_CONTEXT_LOOKBACK_DAYS,
) -> pd.DataFrame:
    if int(limit) <= 0 or not _table_exists(ANNOUNCEMENT_CONTEXT_OVERLAYS_TABLE):
        return pd.DataFrame()
    available_columns = _table_columns(ANNOUNCEMENT_CONTEXT_OVERLAYS_TABLE)
    taxonomy_filters: list[str] = []
    if "announcement_storage_form" in available_columns:
        taxonomy_filters.append("COALESCE(NULLIF(TRIM(announcement_storage_form), ''), 'compact_structured_event') <> 'archived_raw_reference_only'")
    if "llm_review_ready" in available_columns:
        taxonomy_filters.append("COALESCE(llm_review_ready, TRUE) IS TRUE")
    optional_selects = {
        "announcement_storage_form": "announcement_storage_form",
        "llm_evidence_mode": "llm_evidence_mode",
        "llm_review_ready": "llm_review_ready",
        "raw_archive_required": "raw_archive_required",
    }
    taxonomy_json_fields = ",\n                    ".join(
        f"'{key}', {expression if key in available_columns else 'NULL'}"
        for key, expression in optional_selects.items()
    )
    taxonomy_where = "\n                  " + "\n                  ".join(f"AND {clause}" for clause in taxonomy_filters) if taxonomy_filters else ""
    effective_asof = pd.to_datetime(asof_date or pd.Timestamp.utcnow(), utc=True, errors="coerce")
    if pd.isna(effective_asof):
        effective_asof = pd.Timestamp.utcnow()
    effective_asof = effective_asof.normalize()
    lookback_start = effective_asof - pd.Timedelta(days=max(1, int(lookback_days)))
    symbol_clause = ""
    params: dict[str, Any] = {
        "asof_date": effective_asof,
        "lookback_start": lookback_start,
        "last_checked_at": last_checked_at,
        "limit": int(limit),
        "setup_prefix": ANNOUNCEMENT_CONTEXT_SETUP_PREFIX,
        "source_table": ANNOUNCEMENT_CONTEXT_OVERLAYS_TABLE,
        "taxonomy_filter_applied": bool(taxonomy_filters),
        "archive_only_rows_excluded": "announcement_storage_form" in available_columns,
    }
    if symbols:
        symbol_clause = "AND UPPER(TRIM(symbol)) = ANY(%(symbols)s)"
        params["symbols"] = [str(value).upper() for value in symbols]
    try:
        df = sql_to_df(
            f"""
            WITH recent_overlays AS (
                SELECT *
                FROM {ANNOUNCEMENT_CONTEXT_OVERLAYS_TABLE}
                WHERE published_on >= %(lookback_start)s
                  AND published_on < (%(asof_date)s + interval '1 day')
                  AND production_status = 'active'
                  AND authority_scope = 'watchlist_pressure_only'
                  AND direction IN ('positive', 'negative', 'watch')
                  AND NULLIF(TRIM(symbol), '') IS NOT NULL
                  {taxonomy_where}
                  {symbol_clause}
            ),
            ranked AS (
                SELECT
                    *,
                    ROW_NUMBER() OVER (
                        PARTITION BY UPPER(TRIM(symbol))
                        ORDER BY pressure_score DESC NULLS LAST, published_on DESC NULLS LAST, overlay_id
                    ) AS rn
                FROM recent_overlays
            )
            SELECT
                asof_date,
                (%(setup_prefix)s || '_' || upper(direction))::text AS setup_id,
                ('Announcement context: ' || coalesce(event_class, direction))::text AS setup_name,
                NULL::text AS regime_name,
                UPPER(TRIM(symbol)) AS symbol,
                company_master_id,
                NULL::text AS screener_slug,
                ROW_NUMBER() OVER (ORDER BY pressure_score DESC NULLS LAST, published_on DESC NULLS LAST, symbol)::bigint AS rank,
                ('ANNOUNCEMENT_CONTEXT_' || upper(direction))::text AS candidate_state,
                ('ANNOUNCEMENT_CONTEXT_' || upper(direction))::text AS current_state,
                watch_reason_detail,
                NULL::text AS entry_style,
                NULL::double precision AS attractive_price_low,
                NULL::double precision AS attractive_price_high,
                NULL::double precision AS invalidation_price,
                NULL::text AS entry_note,
                FALSE::boolean AS near_miss_flag,
                event_class AS last_event_class,
                CASE
                    WHEN direction = 'positive' THEN 'Official filing supports watchlist escalation only after technical confirmation.'
                    WHEN direction = 'negative' THEN 'Official filing supports de-risk review only after lifecycle/risk confirmation.'
                    ELSE 'Official filing needs follow-up evidence before escalation.'
                END::text AS last_state_transition_hint,
                pressure_score AS last_event_score_impact,
                TRUE::boolean AS watch_enabled,
                json_build_object(
                    'source', 'announcement_context_overlay',
                    'overlay_id', overlay_id,
                    'evidence_id', evidence_id,
                    'unique_id', unique_id,
                    'event_class', event_class,
                    'direction', direction,
                    'pressure_score', pressure_score,
                    'materiality', materiality,
                    'surprise', surprise,
                    'novelty', novelty,
                    'contradiction', contradiction,
                    'confidence', confidence,
                    'verdict', verdict,
                    'source_reliability', source_reliability,
                    'compact_evidence_contract', json_build_object(
                        'source_table', %(source_table)s,
                        'taxonomy_filter_applied', %(taxonomy_filter_applied)s,
                        'archive_only_rows_excluded', %(archive_only_rows_excluded)s,
                        {taxonomy_json_fields}
                    ),
                    'matched_sources_json', matched_sources_json,
                    'authority_scope', 'watchlist_pressure_only'
                )::text AS watch_reasons_json,
                'announcement_context'::text AS watch_status,
                published_on AS state_updated_at,
                published_on AS watch_started_at,
                %(last_checked_at)s::timestamptz AS last_checked_at,
                published_on AS last_document_published_on,
                load_ts,
                'announcement_context'::text AS monitor_source
            FROM ranked
            WHERE rn = 1
            ORDER BY pressure_score DESC NULLS LAST, published_on DESC NULLS LAST, symbol
            LIMIT %(limit)s
            """,
            params=params,
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.announcement_watch",
            source=ANNOUNCEMENT_CONTEXT_OVERLAYS_TABLE,
            fallback_type="announcement_context_watchlist_load_failed",
            severity="warn",
            reason="Announcement/news watcher skipped announcement-context watch targets because official-filing overlay load failed.",
            error=exc,
            metadata={
                "asof_date": str(effective_asof),
                "lookback_days": int(lookback_days),
                "symbols_count": len(symbols or []),
                "limit": int(limit),
            },
        )
        return pd.DataFrame()
    if df.empty:
        return df
    for column in ["asof_date", "state_updated_at", "watch_started_at", "last_checked_at", "last_document_published_on", "load_ts"]:
        df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    df["asof_date"] = normalize_timestamp(df["asof_date"])
    df["symbol"] = df["symbol"].astype("string").str.upper()
    df["rank"] = pd.to_numeric(df["rank"], errors="coerce")
    return _filter_context_targets_by_reliability(df, asof_date=effective_asof)


def load_macro_context_watchlist(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    limit: int = DEFAULT_MACRO_CONTEXT_WATCH_LIMIT,
    last_checked_at: pd.Timestamp | None = None,
) -> pd.DataFrame:
    if int(limit) <= 0 or not _table_exists(MACRO_CONTEXT_OVERLAYS_TABLE) or not _table_exists(MARKET_CONTEXT_UNIVERSE_TABLE):
        return pd.DataFrame()
    effective_asof = pd.to_datetime(asof_date or pd.Timestamp.utcnow(), utc=True, errors="coerce").normalize()
    symbol_clause = ""
    params: dict[str, Any] = {
        "asof_date": effective_asof,
        "last_checked_at": last_checked_at,
        "limit": int(limit),
    }
    if symbols:
        symbol_clause = "AND UPPER(TRIM(u.symbol)) = ANY(%(symbols)s)"
        params["symbols"] = [str(value).upper() for value in symbols]
    try:
        df = sql_to_df(
            f"""
            WITH latest_overlays AS (
                SELECT *
                FROM {MACRO_CONTEXT_OVERLAYS_TABLE}
                WHERE asof_date = (
                    SELECT MAX(asof_date)
                    FROM {MACRO_CONTEXT_OVERLAYS_TABLE}
                    WHERE asof_date <= %(asof_date)s
                )
                  AND production_status = 'active'
                  AND authority_scope = 'watchlist_pressure_only'
                  AND direction IN ('positive', 'negative', 'watch')
                  AND NULLIF(TRIM(COALESCE(sector_name, sector_code, '')), '') IS NOT NULL
            ),
            latest_universe AS (
                SELECT *
                FROM {MARKET_CONTEXT_UNIVERSE_TABLE}
                WHERE asof_date = (
                    SELECT MAX(asof_date)
                    FROM {MARKET_CONTEXT_UNIVERSE_TABLE}
                    WHERE asof_date <= %(asof_date)s
                )
                  AND in_top_context = TRUE
                  AND NULLIF(TRIM(symbol), '') IS NOT NULL
                  AND NULLIF(TRIM(company_master_id), '') IS NOT NULL
            ),
            joined AS (
                SELECT
                    u.asof_date AS universe_asof_date,
                    o.asof_date AS overlay_asof_date,
                    UPPER(TRIM(u.symbol)) AS symbol,
                    u.company_master_id,
                    u.context_rank,
                    u.sector_code AS universe_sector_code,
                    u.sector_name AS universe_sector_name,
                    u.technical_leadership_score,
                    u.macro_sensitivity_tag,
                    o.overlay_id,
                    o.macro_signal_id,
                    o.macro_signal_name,
                    o.sector_name AS overlay_sector_name,
                    o.sector_code AS overlay_sector_code,
                    o.direction,
                    o.pressure_score,
                    o.macro_stress_score,
                    o.macro_risk_state,
                    o.trigger_reason,
                    o.matched_sources_json,
                    o.load_ts AS overlay_load_ts,
                    u.load_ts AS universe_load_ts,
                    ROW_NUMBER() OVER (
                        PARTITION BY UPPER(TRIM(u.symbol))
                        ORDER BY
                            o.pressure_score DESC NULLS LAST,
                            CASE WHEN o.direction = 'negative' THEN 0 WHEN o.direction = 'positive' THEN 1 ELSE 2 END,
                            u.context_rank ASC NULLS LAST,
                            o.macro_signal_id
                    ) AS rn
                FROM latest_universe u
                JOIN latest_overlays o
                  ON regexp_replace(upper(coalesce(u.sector_name, '')), '[^A-Z0-9]', '', 'g') = regexp_replace(upper(coalesce(o.sector_name, '')), '[^A-Z0-9]', '', 'g')
                  OR regexp_replace(upper(coalesce(u.sector_code, '')), '[^A-Z0-9]', '', 'g') = regexp_replace(upper(coalesce(o.sector_code, '')), '[^A-Z0-9]', '', 'g')
                WHERE 1 = 1
                  {symbol_clause}
            )
            SELECT
                overlay_asof_date AS asof_date,
                (%(setup_prefix)s || '_' || macro_signal_id || '_' || upper(direction))::text AS setup_id,
                ('Macro context: ' || macro_signal_name || ' / ' || direction)::text AS setup_name,
                NULL::text AS regime_name,
                symbol,
                company_master_id,
                NULL::text AS screener_slug,
                context_rank::bigint AS rank,
                ('MACRO_CONTEXT_' || upper(direction))::text AS candidate_state,
                ('MACRO_CONTEXT_' || upper(direction))::text AS current_state,
                CASE
                    WHEN direction = 'positive' THEN 'Macro context creates positive sector watch pressure; technical and liquidity confirmation still required.'
                    WHEN direction = 'negative' THEN 'Macro context creates negative sector pressure; monitor for thesis invalidation, de-risking, or exit evidence.'
                    ELSE 'Macro context creates watch-only sector pressure; inspect symbol evidence before escalation.'
                END::text AS watch_reason_detail,
                NULL::text AS entry_style,
                NULL::double precision AS attractive_price_low,
                NULL::double precision AS attractive_price_high,
                NULL::double precision AS invalidation_price,
                NULL::text AS entry_note,
                FALSE::boolean AS near_miss_flag,
                NULL::text AS last_event_class,
                NULL::text AS last_state_transition_hint,
                NULL::double precision AS last_event_score_impact,
                TRUE::boolean AS watch_enabled,
                json_build_object(
                    'source', 'macro_context_overlay',
                    'overlay_id', overlay_id,
                    'macro_signal_id', macro_signal_id,
                    'macro_signal_name', macro_signal_name,
                    'direction', direction,
                    'pressure_score', pressure_score,
                    'macro_stress_score', macro_stress_score,
                    'macro_risk_state', macro_risk_state,
                    'overlay_sector_name', overlay_sector_name,
                    'overlay_sector_code', overlay_sector_code,
                    'universe_sector_name', universe_sector_name,
                    'universe_sector_code', universe_sector_code,
                    'context_rank', context_rank,
                    'technical_leadership_score', technical_leadership_score,
                    'macro_sensitivity_tag', macro_sensitivity_tag,
                    'trigger_reason', trigger_reason,
                    'matched_sources_json', matched_sources_json,
                    'authority_scope', 'watchlist_pressure_only'
                )::text AS watch_reasons_json,
                'macro_context'::text AS watch_status,
                overlay_asof_date AS state_updated_at,
                overlay_asof_date AS watch_started_at,
                %(last_checked_at)s::timestamptz AS last_checked_at,
                NULL::timestamptz AS last_document_published_on,
                GREATEST(overlay_load_ts, universe_load_ts) AS load_ts,
                'macro_context'::text AS monitor_source
            FROM joined
            WHERE rn = 1
            ORDER BY pressure_score DESC NULLS LAST, context_rank ASC NULLS LAST, symbol
            LIMIT %(limit)s
            """,
            params={**params, "setup_prefix": MACRO_CONTEXT_SETUP_PREFIX},
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.announcement_watch",
            source=MACRO_CONTEXT_OVERLAYS_TABLE,
            fallback_type="macro_context_watchlist_load_failed",
            severity="warn",
            reason="Announcement/news watcher skipped macro-context watch targets because overlay-to-universe mapping failed.",
            error=exc,
            metadata={
                "asof_date": str(effective_asof),
                "symbols_count": len(symbols or []),
                "limit": int(limit),
            },
        )
        return pd.DataFrame()
    if df.empty:
        return df
    for column in ["asof_date", "state_updated_at", "watch_started_at", "last_checked_at", "last_document_published_on", "load_ts"]:
        df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    df["asof_date"] = normalize_timestamp(df["asof_date"])
    df["symbol"] = df["symbol"].astype("string").str.upper()
    df["rank"] = pd.to_numeric(df["rank"], errors="coerce")
    return _filter_context_targets_by_reliability(df, asof_date=effective_asof)


def load_bhavcopy_context_watchlist(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    limit: int = DEFAULT_BHAVCOPY_CONTEXT_WATCH_LIMIT,
    last_checked_at: pd.Timestamp | None = None,
) -> pd.DataFrame:
    if int(limit) <= 0 or not _table_exists(BHAVCOPY_CONTEXT_OVERLAYS_TABLE):
        return pd.DataFrame()
    effective_asof = pd.to_datetime(asof_date or pd.Timestamp.utcnow(), utc=True, errors="coerce").normalize()
    symbol_clause = ""
    params: dict[str, Any] = {"asof_date": effective_asof, "last_checked_at": last_checked_at, "limit": int(limit)}
    if symbols:
        symbol_clause = "AND UPPER(TRIM(symbol)) = ANY(%(symbols)s)"
        params["symbols"] = [str(value).upper() for value in symbols]
    try:
        df = sql_to_df(
            f"""
            WITH latest_overlays AS (
                SELECT *
                FROM {BHAVCOPY_CONTEXT_OVERLAYS_TABLE}
                WHERE asof_date = (
                    SELECT MAX(asof_date)
                    FROM {BHAVCOPY_CONTEXT_OVERLAYS_TABLE}
                    WHERE asof_date <= %(asof_date)s
                )
                  AND production_status = 'active'
                  AND authority_scope = 'watchlist_pressure_only'
                  AND NULLIF(TRIM(symbol), '') IS NOT NULL
                  {symbol_clause}
            ),
            ranked AS (
                SELECT
                    *,
                    ROW_NUMBER() OVER (
                        PARTITION BY UPPER(TRIM(symbol))
                        ORDER BY pressure_score DESC NULLS LAST, direction, overlay_id
                    ) AS rn
                FROM latest_overlays
            )
            SELECT
                asof_date,
                (%(setup_prefix)s || '_' || upper(direction))::text AS setup_id,
                ('Bhavcopy context: ' || coalesce(deal_pressure, direction))::text AS setup_name,
                NULL::text AS regime_name,
                UPPER(TRIM(symbol)) AS symbol,
                company_master_id,
                NULL::text AS screener_slug,
                ROW_NUMBER() OVER (ORDER BY pressure_score DESC NULLS LAST, symbol)::bigint AS rank,
                ('BHAVCOPY_CONTEXT_' || upper(direction))::text AS candidate_state,
                ('BHAVCOPY_CONTEXT_' || upper(direction))::text AS current_state,
                watch_reason_detail,
                NULL::text AS entry_style,
                NULL::double precision AS attractive_price_low,
                NULL::double precision AS attractive_price_high,
                NULL::double precision AS invalidation_price,
                NULL::text AS entry_note,
                FALSE::boolean AS near_miss_flag,
                NULL::text AS last_event_class,
                NULL::text AS last_state_transition_hint,
                NULL::double precision AS last_event_score_impact,
                TRUE::boolean AS watch_enabled,
                json_build_object(
                    'source', 'bhavcopy_context_overlay',
                    'overlay_id', overlay_id,
                    'direction', direction,
                    'pressure_score', pressure_score,
                    'evidence_score', evidence_score,
                    'deal_pressure', deal_pressure,
                    'deal_net_value_inr', deal_net_value_inr,
                    'short_selling_quantity', short_selling_quantity,
                    'circuit_hit_count', circuit_hit_count,
                    'turnover_value_inr', turnover_value_inr,
                    'avg_turnover_value_20d', avg_turnover_value_20d,
                    'matched_sources_json', matched_sources_json,
                    'compact_evidence_contract', json_build_object(
                        'source_table', %(source_table)s,
                        'source_family', 'bhavcopy_context',
                        'raw_bhavcopy_scan_allowed', false,
                        'authority_scope', 'watchlist_pressure_only'
                    ),
                    'authority_scope', 'watchlist_pressure_only'
                )::text AS watch_reasons_json,
                'bhavcopy_context'::text AS watch_status,
                asof_date AS state_updated_at,
                asof_date AS watch_started_at,
                %(last_checked_at)s::timestamptz AS last_checked_at,
                NULL::timestamptz AS last_document_published_on,
                load_ts,
                'bhavcopy_context'::text AS monitor_source
            FROM ranked
            WHERE rn = 1
            ORDER BY pressure_score DESC NULLS LAST, symbol
            LIMIT %(limit)s
            """,
            params={**params, "setup_prefix": BHAVCOPY_CONTEXT_SETUP_PREFIX, "source_table": BHAVCOPY_CONTEXT_OVERLAYS_TABLE},
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.announcement_watch",
            source=BHAVCOPY_CONTEXT_OVERLAYS_TABLE,
            fallback_type="bhavcopy_context_watchlist_load_failed",
            severity="warn",
            reason="Announcement/news watcher skipped bhavcopy-context watch targets because compact evidence overlay load failed.",
            error=exc,
            metadata={"asof_date": str(effective_asof), "symbols_count": len(symbols or []), "limit": int(limit)},
        )
        return pd.DataFrame()
    if df.empty:
        return df
    for column in ["asof_date", "state_updated_at", "watch_started_at", "last_checked_at", "last_document_published_on", "load_ts"]:
        df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    df["asof_date"] = normalize_timestamp(df["asof_date"])
    df["symbol"] = df["symbol"].astype("string").str.upper()
    df["rank"] = pd.to_numeric(df["rank"], errors="coerce")
    return df


def merge_watch_targets(primary: pd.DataFrame, secondary: pd.DataFrame) -> pd.DataFrame:
    primary = primary.copy() if isinstance(primary, pd.DataFrame) and not primary.empty else pd.DataFrame()
    secondary = secondary.copy() if isinstance(secondary, pd.DataFrame) and not secondary.empty else pd.DataFrame()
    if primary.empty and secondary.empty:
        return pd.DataFrame()
    if primary.empty:
        out = secondary
    elif secondary.empty:
        out = primary
    else:
        primary["symbol"] = primary["symbol"].astype("string").str.upper()
        secondary["symbol"] = secondary["symbol"].astype("string").str.upper()
        primary_symbols = set(primary["symbol"].dropna().astype(str).str.upper())
        secondary = secondary[~secondary["symbol"].astype("string").str.upper().isin(primary_symbols)].copy()
        out = pd.concat([primary, secondary], ignore_index=True, sort=False)
    out["symbol"] = out["symbol"].astype("string").str.upper()
    if "monitor_source" not in out.columns:
        out["monitor_source"] = "watchlist"
    out["monitor_source"] = out["monitor_source"].fillna("watchlist")
    return out.reset_index(drop=True)


def is_material_context_event(row: pd.Series) -> bool:
    text = " ".join(
        str(row.get(column) or "")
        for column in ["subject", "filed_under_category", "concise_summary_text", "categories_json"]
    ).lower()
    def _has_keyword(keyword: str) -> bool:
        normalized = str(keyword or "").strip().lower()
        if not normalized:
            return False
        if " " in normalized:
            return normalized in text
        return re.search(rf"\b{re.escape(normalized)}\b", text) is not None

    if any(phrase in text for phrase in NON_MATERIAL_CONTEXT_PHRASES):
        return any(_has_keyword(keyword) for keyword in STRONG_MATERIAL_EVENT_KEYWORDS)
    if "meeting" in text and not any(_has_keyword(keyword) for keyword in STRONG_MATERIAL_EVENT_KEYWORDS):
        return False
    return any(_has_keyword(keyword) for keyword in MATERIAL_EVENT_KEYWORDS)


def load_documents_for_company(
    company_master_id: str,
    *,
    published_from: pd.Timestamp,
    published_to: pd.Timestamp,
) -> pd.DataFrame:
    df = sql_to_df(
        """
        SELECT
            unique_id,
            company_master_id,
            exchange,
            ticker,
            company_name,
            subject,
            filed_under_category,
            published_on,
            parse_status,
            concise_summary_text,
            categories_json
        FROM announcement_pipeline_documents
        WHERE company_master_id = %s
          AND published_on >= %s
          AND published_on <= %s
        ORDER BY published_on, unique_id
        """,
        params=(company_master_id, published_from, published_to),
    )
    if df.empty:
        return df
    df["published_on"] = pd.to_datetime(df["published_on"], utc=True, errors="coerce")
    return df


def build_event_rows(watch_row: pd.Series, docs: pd.DataFrame) -> pd.DataFrame:
    if docs.empty:
        return pd.DataFrame()
    rows = []
    for _, doc in docs.iterrows():
        rows.append(
            {
                "asof_date": watch_row["asof_date"],
                "setup_id": watch_row["setup_id"],
                "setup_name": watch_row["setup_name"],
                "symbol": watch_row["symbol"],
                "company_master_id": watch_row["company_master_id"],
                "unique_id": doc["unique_id"],
                "exchange": doc["exchange"],
                "subject": doc["subject"],
                "filed_under_category": doc["filed_under_category"],
                "published_on": doc["published_on"],
                "parse_status": doc["parse_status"],
                "concise_summary_text": doc["concise_summary_text"],
                "categories_json": doc["categories_json"],
                "watch_reasons_json": watch_row["watch_reasons_json"],
                "monitor_source": watch_row.get("monitor_source") or "watchlist",
                "event_status": "triggered",
                "load_ts": pd.Timestamp.utcnow(),
            }
        )
    out = pd.DataFrame(rows)
    context_mask = out["monitor_source"].astype("string").str.lower().isin(
        ["market_context", "theme_context", "announcement_context", "bhavcopy_context", "macro_context"]
    )
    if context_mask.any():
        material_mask = out.apply(is_material_context_event, axis=1)
        out.loc[context_mask & ~material_mask, "event_status"] = "context_observed"
    return out


def persist_watch_outputs(watchlist_updates: pd.DataFrame, events: pd.DataFrame) -> None:
    ensure_watch_outputs_tables()
    if not watchlist_updates.empty:
        upsert_to_db(
            watchlist_updates,
            WATCHLIST_TABLE,
            unique_keys=["asof_date", "setup_id", "symbol"],
            timescaledb_column="asof_date",
        )
    if not events.empty:
        upsert_to_db(
            events,
            EVENTS_TABLE,
            unique_keys=["published_on", "setup_id", "symbol", "unique_id"],
            timescaledb_column="published_on",
        )


def load_company_ingest_watermarks(company_master_ids: Any) -> dict[str, pd.Timestamp]:
    """Return a durable per-company "already ingested up to" watermark.

    The per-asof-date watchlist rows reset ``last_checked_at`` every run (each new
    advisory date builds fresh rows), so anchoring the incremental ingest window to
    a single row makes almost every company fall back to the initial lookback floor
    and re-discover/re-parse the same announcements daily. This looks up the freshest
    prior checkpoint for each company across all asof-date rows (both the last scan
    time and the newest announcement we already stored) so the window only covers the
    delta. Best-effort: any failure yields an empty map and the caller falls back to
    the initial lookback floor.
    """
    ids = sorted(
        {
            str(value).strip()
            for value in (company_master_ids or [])
            if value is not None and str(value).strip()
        }
    )
    if not ids:
        return {}
    watermarks: dict[str, pd.Timestamp] = {}
    try:
        rows = sql_to_df(
            f"""
            SELECT company_master_id,
                   MAX(last_checked_at) AS last_checked_at,
                   MAX(last_document_published_on) AS last_document_published_on
            FROM {WATCHLIST_TABLE}
            WHERE company_master_id = ANY(%s)
              AND (last_checked_at IS NOT NULL OR last_document_published_on IS NOT NULL)
            GROUP BY company_master_id
            """,
            params=(ids,),
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.announcement_watch",
            source=WATCHLIST_TABLE,
            fallback_type="announcement_ingest_watermark_load_failed",
            severity="warn",
            reason=(
                "Announcement watcher could not load durable per-company ingest watermarks and "
                "fell back to the initial lookback floor, which may re-scan already-ingested announcements."
            ),
            error=exc,
            metadata={"company_count": len(ids)},
        )
        return {}
    if rows.empty:
        return {}
    for _, row in rows.iterrows():
        key = str(row.get("company_master_id") or "").strip()
        if not key:
            continue
        candidates = [
            pd.to_datetime(row.get("last_checked_at"), utc=True, errors="coerce"),
            pd.to_datetime(row.get("last_document_published_on"), utc=True, errors="coerce"),
        ]
        valid = [ts for ts in candidates if not pd.isna(ts)]
        if valid:
            watermarks[key] = max(valid)
    return watermarks


def _prepare_watchlist_for_ingest(
    watchlist: pd.DataFrame,
    *,
    effective_to: pd.Timestamp,
    company_watermarks: dict[str, pd.Timestamp] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    working = watchlist.copy()
    working["company_master_id"] = working["company_master_id"].astype("string")
    working["symbol"] = working["symbol"].astype("string").str.upper()
    working["published_from"] = pd.Series(pd.NaT, index=working.index, dtype="datetime64[ns, UTC]")
    working["published_from_capped"] = False
    default_floor = (effective_to - pd.Timedelta(days=INITIAL_INGEST_LOOKBACK_DAYS)).normalize()
    if company_watermarks is None:
        company_watermarks = load_company_ingest_watermarks(working["company_master_id"].tolist())

    for index, row in working.iterrows():
        last_checked = pd.to_datetime(row.get("last_checked_at"), utc=True, errors="coerce")
        company_id = str(row.get("company_master_id") or "").strip()
        durable = company_watermarks.get(company_id) if company_id else None
        if durable is not None and (pd.isna(last_checked) or durable > last_checked):
            last_checked = durable
        if pd.isna(last_checked):
            published_from = default_floor
            working.at[index, "published_from_capped"] = True
        else:
            published_from = last_checked - pd.Timedelta(days=INGEST_OVERLAP_DAYS)
        working.at[index, "published_from"] = published_from

    if "watch_tier" not in working.columns:
        working["watch_tier"] = pd.NA
    # a target counts as active-tier when ANY of its watch rows is active or untiered (fail-open)
    working["_tier_active"] = ~working["watch_tier"].astype("string").str.lower().eq("shelf")
    unique_ingest_targets = (
        working.groupby(["company_master_id", "symbol"], dropna=False, sort=True)
        .agg(
            published_from=("published_from", "min"),
            watch_rows=("setup_id", "count"),
            tier_active=("_tier_active", "max"),
        )
        .reset_index()
    )
    return working, unique_ingest_targets


def run_announcement_ingest(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
    to_date: pd.Timestamp | None = None,
    include_market_context: bool = False,
    market_context_limit: int = DEFAULT_MARKET_CONTEXT_WATCH_LIMIT,
    market_context_last_checked_at: pd.Timestamp | None = None,
    include_theme_context: bool = False,
    theme_context_limit: int = DEFAULT_THEME_CONTEXT_WATCH_LIMIT,
    theme_context_last_checked_at: pd.Timestamp | None = None,
    include_announcement_context: bool = False,
    announcement_context_limit: int = DEFAULT_ANNOUNCEMENT_CONTEXT_WATCH_LIMIT,
    announcement_context_last_checked_at: pd.Timestamp | None = None,
    announcement_context_lookback_days: int = DEFAULT_ANNOUNCEMENT_CONTEXT_LOOKBACK_DAYS,
    include_macro_context: bool = False,
    macro_context_limit: int = DEFAULT_MACRO_CONTEXT_WATCH_LIMIT,
    macro_context_last_checked_at: pd.Timestamp | None = None,
    include_bhavcopy_context: bool = False,
    bhavcopy_context_limit: int = DEFAULT_BHAVCOPY_CONTEXT_WATCH_LIMIT,
    bhavcopy_context_last_checked_at: pd.Timestamp | None = None,
    max_ingest_targets: int | None = None,
) -> dict[str, object]:
    watchlist = load_watchlist(asof_date=asof_date, symbols=symbols, setup_ids=setup_ids)
    market_context_watchlist = (
        load_market_context_watchlist(
            asof_date=asof_date,
            symbols=symbols,
            limit=market_context_limit,
            last_checked_at=market_context_last_checked_at,
        )
        if include_market_context
        else pd.DataFrame()
    )
    theme_context_watchlist = (
        load_theme_context_watchlist(
            asof_date=asof_date,
            symbols=symbols,
            limit=theme_context_limit,
            last_checked_at=theme_context_last_checked_at,
        )
        if include_theme_context
        else pd.DataFrame()
    )
    announcement_context_watchlist = (
        load_announcement_context_watchlist(
            asof_date=asof_date,
            symbols=symbols,
            limit=announcement_context_limit,
            last_checked_at=announcement_context_last_checked_at,
            lookback_days=announcement_context_lookback_days,
        )
        if include_announcement_context
        else pd.DataFrame()
    )
    macro_context_watchlist = (
        load_macro_context_watchlist(
            asof_date=asof_date,
            symbols=symbols,
            limit=macro_context_limit,
            last_checked_at=macro_context_last_checked_at,
        )
        if include_macro_context
        else pd.DataFrame()
    )
    bhavcopy_context_watchlist = (
        load_bhavcopy_context_watchlist(
            asof_date=asof_date,
            symbols=symbols,
            limit=bhavcopy_context_limit,
            last_checked_at=bhavcopy_context_last_checked_at,
        )
        if include_bhavcopy_context
        else pd.DataFrame()
    )
    watchlist = merge_watch_targets(watchlist, announcement_context_watchlist)
    watchlist = merge_watch_targets(watchlist, theme_context_watchlist)
    watchlist = merge_watch_targets(watchlist, bhavcopy_context_watchlist)
    watchlist = merge_watch_targets(watchlist, macro_context_watchlist)
    watchlist = merge_watch_targets(watchlist, market_context_watchlist)
    if watchlist.empty:
        return {
            "watchlist": pd.DataFrame(),
            "effective_to": pd.to_datetime(to_date or pd.Timestamp.utcnow(), utc=True, errors="coerce"),
            "docs_by_company": {},
            "last_published_by_company": {},
            "meta": {
                "watch_count": 0,
                "unique_ingest_targets": 0,
                "market_context_enabled": bool(include_market_context),
                "market_context_limit": int(market_context_limit),
                "market_context_watch_count": int(len(market_context_watchlist)),
                "theme_context_enabled": bool(include_theme_context),
                "theme_context_limit": int(theme_context_limit),
                "theme_context_watch_count": int(len(theme_context_watchlist)),
                "announcement_context_enabled": bool(include_announcement_context),
                "announcement_context_limit": int(announcement_context_limit),
                "announcement_context_watch_count": int(len(announcement_context_watchlist)),
                "announcement_context_lookback_days": int(announcement_context_lookback_days),
                "macro_context_enabled": bool(include_macro_context),
                "macro_context_limit": int(macro_context_limit),
                "macro_context_watch_count": int(len(macro_context_watchlist)),
                "bhavcopy_context_enabled": bool(include_bhavcopy_context),
                "bhavcopy_context_limit": int(bhavcopy_context_limit),
                "bhavcopy_context_watch_count": int(len(bhavcopy_context_watchlist)),
                "ingest_runs": [],
            },
        }

    pipeline = ManagedAnnouncementPipeline()
    effective_to = pd.to_datetime(to_date or pd.Timestamp.utcnow(), utc=True, errors="coerce")
    if to_date is not None:
        effective_to = inclusive_end_of_day(effective_to)
    ingest_runs: list[dict[str, object]] = []
    watchlist, unique_ingest_targets = _prepare_watchlist_for_ingest(watchlist, effective_to=effective_to)
    total_targets = int(len(unique_ingest_targets))
    effective_max_ingest_targets = int(max_ingest_targets or 0)
    skipped_ingest_target_count = 0
    original_watch_count = int(len(watchlist))
    if effective_max_ingest_targets == 0 and total_targets > 0 and "tier_active" in unique_ingest_targets.columns:
        # EOD full-ingest path: active-tier targets always ingest; shelf targets only when
        # not checked within SHELF_INGEST_MAX_AGE_DAYS (published_from = last_checked -
        # overlap, so recently-checked shelf targets have a recent published_from).
        # Operational attention only -- every shelf name still ingests every ~N days.
        shelf_recent_cutoff = effective_to - pd.Timedelta(days=SHELF_INGEST_MAX_AGE_DAYS) - pd.Timedelta(days=INGEST_OVERLAP_DAYS)
        published_from_ts = pd.to_datetime(unique_ingest_targets["published_from"], utc=True, errors="coerce")
        shelf_and_recent = (
            ~unique_ingest_targets["tier_active"].fillna(True).astype(bool)
            & published_from_ts.notna()
            & (published_from_ts > shelf_recent_cutoff)
        )
        if bool(shelf_and_recent.any()):
            skipped = int(shelf_and_recent.sum())
            unique_ingest_targets = unique_ingest_targets[~shelf_and_recent].reset_index(drop=True)
            _emit_progress(
                "[advisory.announcement_watch] shelf-tier ingest age gate "
                f"skipped={skipped} of {total_targets} targets (checked within {SHELF_INGEST_MAX_AGE_DAYS}d); "
                f"remaining={len(unique_ingest_targets)}"
            )
            total_targets = int(len(unique_ingest_targets))
    if effective_max_ingest_targets > 0 and total_targets > effective_max_ingest_targets:
        # Active tier wins the capped slots first; within a tier, longest-unchecked first.
        tier_rank = (
            ~unique_ingest_targets.get("tier_active", pd.Series(True, index=unique_ingest_targets.index)).fillna(True).astype(bool)
        ).astype(int)
        unique_ingest_targets = (
            unique_ingest_targets
            .assign(_tier_rank=tier_rank)
            .sort_values(["_tier_rank", "published_from", "symbol"], ascending=[True, True, True], na_position="first")
            .drop(columns=["_tier_rank"])
            .head(effective_max_ingest_targets)
            .reset_index(drop=True)
        )
        def _target_key(company_master_id: object, symbol: object) -> tuple[str, str]:
            company_text = "" if pd.isna(company_master_id) else str(company_master_id or "").strip()
            symbol_text = "" if pd.isna(symbol) else str(symbol or "").strip().upper()
            return company_text, symbol_text

        selected_pairs = {
            _target_key(row.company_master_id, row.symbol)
            for row in unique_ingest_targets.itertuples(index=False)
        }
        watchlist = watchlist[
            watchlist.apply(
                lambda row: _target_key(row.get("company_master_id"), row.get("symbol")) in selected_pairs,
                axis=1,
            )
        ].copy()
        skipped_ingest_target_count = total_targets - int(len(unique_ingest_targets))
        _emit_progress(
            "[advisory.announcement_watch] ingest target cap applied "
            f"max_ingest_targets={effective_max_ingest_targets} total_targets={total_targets} "
            f"processed_targets={len(unique_ingest_targets)} skipped_targets={skipped_ingest_target_count}"
        )
    processed_targets = int(len(unique_ingest_targets))
    docs_by_company: dict[str, pd.DataFrame] = {}
    last_published_by_company: dict[str, pd.Timestamp | None] = {}
    unresolved_ingest_target_count = 0
    document_lookup_failed_count = 0
    managed_ingest_failed_count = 0

    for position, target in enumerate(unique_ingest_targets.itertuples(index=False), start=1):
        raw_company_master_id = target.company_master_id
        company_master_id = "" if pd.isna(raw_company_master_id) else str(raw_company_master_id or "").strip()
        symbol = str(target.symbol or "").upper()
        published_from = pd.to_datetime(target.published_from, utc=True, errors="coerce")
        published_to = effective_to
        target_started = time.monotonic()
        _emit_progress(
            f"[advisory.announcement_watch] ingest {position}/{total_targets} symbol={symbol} from={published_from.date()} to={published_to.date()} watch_rows={int(target.watch_rows)}"
        )
        try:
            summary = pipeline.ingest_date_range(
                ticker=symbol,
                from_date=published_from.date(),
                to_date=published_to.date(),
                exchanges=["NSE"],
            )
        except Exception as exc:
            managed_ingest_failed_count += 1
            issue = {
                "ticker": symbol,
                "issue_type": "announcement_managed_ingest_target_failed",
                "severity": "warn",
                "message": f"Announcement managed ingest failed before returning a summary: {type(exc).__name__}: {exc}",
                "company_master_id": company_master_id,
            }
            ingest_runs.append(
                {
                    "symbol": symbol,
                    "company_master_id": company_master_id,
                    "requested": 0,
                    "discovered": 0,
                    "downloaded": 0,
                    "ocred": 0,
                    "categorized": 0,
                    "parsed": 0,
                    "skipped": 0,
                    "failed": 1,
                    "issue_count": 1,
                    "issues": [issue],
                    "elapsed_seconds": round(time.monotonic() - target_started, 4),
                }
            )
            record_local_fallback_event(
                module="advisory.announcement_watch",
                fallback_type="announcement_watch_managed_ingest_target_failed",
                source="data.announcements.managed_pipeline",
                severity="warn",
                symbol=symbol,
                reason=(
                    "Announcement watcher managed ingest failed for one target before returning a summary; "
                    "the batch continues, but this symbol may use stale or missing announcement evidence."
                ),
                error=exc,
                metadata={
                    "company_master_id": company_master_id,
                    "published_from": published_from.isoformat() if hasattr(published_from, "isoformat") else str(published_from),
                    "published_to": published_to.isoformat() if hasattr(published_to, "isoformat") else str(published_to),
                },
            )
            _emit_progress(
                f"[advisory.announcement_watch] ingest failed symbol={symbol} error={type(exc).__name__}: {exc}"
            )
            continue
        ingest_runs.append(
            {
                "symbol": symbol,
                "company_master_id": company_master_id,
                "requested": summary.requested,
                "discovered": summary.discovered,
                "downloaded": summary.downloaded,
                "ocred": summary.ocred,
                "categorized": summary.categorized,
                "parsed": summary.parsed,
                "skipped": summary.skipped,
                "failed": summary.failed,
                "issue_count": getattr(summary, "issue_count", 0),
                "issues": getattr(summary, "issues", []),
                "elapsed_seconds": round(time.monotonic() - target_started, 4),
            }
        )
        if not company_master_id:
            unresolved_ingest_target_count += 1
            _emit_progress(
                f"[advisory.announcement_watch] ingest skipped document lookup symbol={symbol} reason=missing_company_master_id"
            )
            continue
        _emit_progress(
            f"[advisory.announcement_watch] ingest done symbol={symbol} elapsed={time.monotonic() - target_started:.2f}s discovered={summary.discovered} parsed={summary.parsed} failed={summary.failed}"
        )

        try:
            docs = load_documents_for_company(
                company_master_id,
                published_from=published_from,
                published_to=published_to,
            )
        except Exception as exc:
            document_lookup_failed_count += 1
            issue = {
                "ticker": symbol,
                "issue_type": "announcement_document_lookup_failed",
                "severity": "warn",
                "message": f"Announcement document history lookup failed: {type(exc).__name__}: {exc}",
                "company_master_id": company_master_id,
            }
            ingest_runs[-1]["failed"] = int(ingest_runs[-1].get("failed") or 0) + 1
            ingest_runs[-1]["issue_count"] = int(ingest_runs[-1].get("issue_count") or 0) + 1
            issues = ingest_runs[-1].get("issues")
            if not isinstance(issues, list):
                issues = []
                ingest_runs[-1]["issues"] = issues
            issues.append(issue)
            record_local_fallback_event(
                module="advisory.announcement_watch",
                fallback_type="announcement_watch_document_lookup_failed",
                source="announcement_pipeline_documents",
                severity="warn",
                symbol=symbol,
                reason=(
                    "Announcement watcher could not load persisted document history for one target; "
                    "the batch continues, but watch-event matching may miss fresh announcement evidence for this symbol."
                ),
                error=exc,
                metadata={
                    "company_master_id": company_master_id,
                    "published_from": published_from.isoformat() if hasattr(published_from, "isoformat") else str(published_from),
                    "published_to": published_to.isoformat() if hasattr(published_to, "isoformat") else str(published_to),
                },
            )
            _emit_progress(
                f"[advisory.announcement_watch] ingest document lookup failed symbol={symbol} error={type(exc).__name__}: {exc}"
            )
            continue
        docs_by_company[company_master_id] = docs
        last_published_by_company[company_master_id] = (
            docs["published_on"].max() if not docs.empty else None
        )

    return {
        "watchlist": watchlist,
        "effective_to": effective_to,
        "docs_by_company": docs_by_company,
        "last_published_by_company": last_published_by_company,
        "meta": {
            "watch_count": int(len(watchlist)),
            "total_watch_count": int(original_watch_count),
            "unique_ingest_targets": processed_targets,
            "total_ingest_targets": total_targets,
            "processed_ingest_targets": processed_targets,
            "skipped_ingest_target_count": int(skipped_ingest_target_count),
            "max_ingest_targets": int(effective_max_ingest_targets),
            "initial_lookback_days": INITIAL_INGEST_LOOKBACK_DAYS,
            "market_context_enabled": bool(include_market_context),
            "market_context_limit": int(market_context_limit),
            "market_context_watch_count": int(len(market_context_watchlist)),
            "theme_context_enabled": bool(include_theme_context),
            "theme_context_limit": int(theme_context_limit),
            "theme_context_watch_count": int(len(theme_context_watchlist)),
            "announcement_context_enabled": bool(include_announcement_context),
            "announcement_context_limit": int(announcement_context_limit),
            "announcement_context_watch_count": int(len(announcement_context_watchlist)),
            "announcement_context_lookback_days": int(announcement_context_lookback_days),
            "macro_context_enabled": bool(include_macro_context),
            "macro_context_limit": int(macro_context_limit),
            "macro_context_watch_count": int(len(macro_context_watchlist)),
            "bhavcopy_context_enabled": bool(include_bhavcopy_context),
            "bhavcopy_context_limit": int(bhavcopy_context_limit),
            "bhavcopy_context_watch_count": int(len(bhavcopy_context_watchlist)),
            "capped_watch_rows": int(pd.Series(watchlist["published_from_capped"]).fillna(False).astype(bool).sum()),
            "unresolved_ingest_target_count": int(unresolved_ingest_target_count),
            "unresolved_watch_row_count": int(watchlist["company_master_id"].astype("string").fillna("").str.strip().eq("").sum()),
            "document_lookup_failed_count": int(document_lookup_failed_count),
            "managed_ingest_failed_count": int(managed_ingest_failed_count),
            "ingest_runs": ingest_runs,
        },
    }


def build_watch_updates_from_ingest(ingest_state: dict[str, object]) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, object]]:
    watchlist = ingest_state.get("watchlist")
    if not isinstance(watchlist, pd.DataFrame) or watchlist.empty:
        return pd.DataFrame(), pd.DataFrame(), {"watch_count": 0, "match_count": 0}

    effective_to = pd.to_datetime(ingest_state.get("effective_to"), utc=True, errors="coerce")
    docs_by_company = ingest_state.get("docs_by_company") if isinstance(ingest_state.get("docs_by_company"), dict) else {}
    last_published_by_company = ingest_state.get("last_published_by_company") if isinstance(ingest_state.get("last_published_by_company"), dict) else {}
    watch_updates: list[dict[str, object]] = []
    event_frames: list[pd.DataFrame] = []

    for _, row in watchlist.iterrows():
        company_master_id = str(row["company_master_id"] or "")
        published_from = pd.to_datetime(row.get("published_from"), utc=True, errors="coerce")
        all_docs = docs_by_company.get(company_master_id, pd.DataFrame())
        if not all_docs.empty:
            docs = all_docs[all_docs["published_on"] >= published_from].copy()
        else:
            docs = pd.DataFrame()
        event_df = build_event_rows(row, docs)
        if not event_df.empty:
            event_frames.append(event_df)
            last_published = event_df["published_on"].max()
        else:
            last_published = pd.to_datetime(row.get("last_document_published_on"), utc=True, errors="coerce")
            cached_last_published = last_published_by_company.get(company_master_id)
            if pd.isna(last_published) and cached_last_published is not None:
                last_published = pd.to_datetime(cached_last_published, utc=True, errors="coerce")

        watch_updates.append(
            {
                "asof_date": row["asof_date"],
                "setup_id": row["setup_id"],
                "setup_name": row["setup_name"],
                "regime_name": row["regime_name"],
                "symbol": row["symbol"],
                "company_master_id": row["company_master_id"],
                "screener_slug": row.get("screener_slug"),
                "rank": row.get("rank"),
                "candidate_state": row.get("candidate_state"),
                "current_state": row.get("current_state"),
                "watch_reason_detail": row.get("watch_reason_detail"),
                "entry_style": row.get("entry_style"),
                "attractive_price_low": row.get("attractive_price_low"),
                "attractive_price_high": row.get("attractive_price_high"),
                "invalidation_price": row.get("invalidation_price"),
                "entry_note": row.get("entry_note"),
                "near_miss_flag": row.get("near_miss_flag"),
                "last_event_class": row.get("last_event_class"),
                "last_state_transition_hint": row.get("last_state_transition_hint"),
                "last_event_score_impact": row.get("last_event_score_impact"),
                "watch_enabled": row.get("watch_enabled", True),
                "watch_reasons_json": row["watch_reasons_json"],
                "watch_status": row.get("watch_status", "active"),
                "state_updated_at": pd.to_datetime(row.get("state_updated_at"), utc=True, errors="coerce"),
                "watch_started_at": pd.to_datetime(row.get("watch_started_at"), utc=True, errors="coerce"),
                "last_checked_at": effective_to,
                "last_document_published_on": last_published,
                "load_ts": pd.Timestamp.utcnow(),
            }
        )

    watch_update_df = pd.DataFrame(watch_updates)
    if not watch_update_df.empty:
        watch_update_df["rank"] = pd.to_numeric(watch_update_df["rank"], errors="coerce")
        for column in [
            "attractive_price_low",
            "attractive_price_high",
            "invalidation_price",
            "last_event_score_impact",
        ]:
            watch_update_df[column] = pd.to_numeric(watch_update_df[column], errors="coerce")
        for column in [
            "near_miss_flag",
            "watch_enabled",
        ]:
            watch_update_df[column] = (
                watch_update_df[column]
                .map(lambda value: None if pd.isna(value) else bool(value))
                .astype("boolean")
            )
        for column in [
            "asof_date",
            "state_updated_at",
            "watch_started_at",
            "last_checked_at",
            "last_document_published_on",
            "load_ts",
        ]:
            watch_update_df[column] = pd.to_datetime(watch_update_df[column], utc=True, errors="coerce")
    events_df = pd.concat(event_frames, ignore_index=True) if event_frames else pd.DataFrame()
    meta = {
        "watch_count": int(len(watchlist)),
        "match_count": int(len(events_df)),
        "triggered_event_count": int(events_df["event_status"].astype(str).eq("triggered").sum()) if not events_df.empty and "event_status" in events_df.columns else 0,
        "context_observed_count": int(events_df["event_status"].astype(str).eq("context_observed").sum()) if not events_df.empty and "event_status" in events_df.columns else 0,
    }
    return watch_update_df, events_df, meta


def run_announcement_watch(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
    to_date: pd.Timestamp | None = None,
    include_market_context: bool = False,
    market_context_limit: int = DEFAULT_MARKET_CONTEXT_WATCH_LIMIT,
    market_context_last_checked_at: pd.Timestamp | None = None,
    include_theme_context: bool = False,
    theme_context_limit: int = DEFAULT_THEME_CONTEXT_WATCH_LIMIT,
    theme_context_last_checked_at: pd.Timestamp | None = None,
    include_announcement_context: bool = False,
    announcement_context_limit: int = DEFAULT_ANNOUNCEMENT_CONTEXT_WATCH_LIMIT,
    announcement_context_last_checked_at: pd.Timestamp | None = None,
    announcement_context_lookback_days: int = DEFAULT_ANNOUNCEMENT_CONTEXT_LOOKBACK_DAYS,
    include_macro_context: bool = False,
    macro_context_limit: int = DEFAULT_MACRO_CONTEXT_WATCH_LIMIT,
    macro_context_last_checked_at: pd.Timestamp | None = None,
    include_bhavcopy_context: bool = False,
    bhavcopy_context_limit: int = DEFAULT_BHAVCOPY_CONTEXT_WATCH_LIMIT,
    bhavcopy_context_last_checked_at: pd.Timestamp | None = None,
    max_ingest_targets: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, object]]:
    ingest_state = run_announcement_ingest(
        asof_date=asof_date,
        symbols=symbols,
        setup_ids=setup_ids,
        to_date=to_date,
        include_market_context=include_market_context,
        market_context_limit=market_context_limit,
        market_context_last_checked_at=market_context_last_checked_at,
        include_theme_context=include_theme_context,
        theme_context_limit=theme_context_limit,
        theme_context_last_checked_at=theme_context_last_checked_at,
        include_announcement_context=include_announcement_context,
        announcement_context_limit=announcement_context_limit,
        announcement_context_last_checked_at=announcement_context_last_checked_at,
        announcement_context_lookback_days=announcement_context_lookback_days,
        include_macro_context=include_macro_context,
        macro_context_limit=macro_context_limit,
        macro_context_last_checked_at=macro_context_last_checked_at,
        include_bhavcopy_context=include_bhavcopy_context,
        bhavcopy_context_limit=bhavcopy_context_limit,
        bhavcopy_context_last_checked_at=bhavcopy_context_last_checked_at,
        max_ingest_targets=max_ingest_targets,
    )
    watch_update_df, events_df, match_meta = build_watch_updates_from_ingest(ingest_state)
    meta = dict(ingest_state.get("meta") or {})
    meta.update(match_meta)
    return watch_update_df, events_df, meta


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run announcement watch for advisory watchlist rows.")
    parser.add_argument("--date", type=parse_datetime_arg, help="Watchlist asof date in YYYY-MM-DD")
    parser.add_argument("--symbols", nargs="*", help="Optional symbols")
    parser.add_argument("--setup", dest="setup_ids", nargs="*", help="Optional setup ids")
    parser.add_argument("--to-date", type=parse_datetime_arg, help="End date in YYYY-MM-DD")
    parser.add_argument("--max-ingest-targets", type=int, default=None, help="Maximum unique company/symbol ingest targets to process in this run. Skipped targets remain due for later runs.")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def summarize(watchlist_updates: pd.DataFrame, events: pd.DataFrame, meta: dict[str, object]) -> dict[str, object]:
    ingest_runs = meta.get("ingest_runs", [])
    ingest_rows = ingest_runs if isinstance(ingest_runs, list) else []
    ingest_failed_count = sum(int(row.get("failed") or 0) for row in ingest_rows if isinstance(row, dict))
    ingest_issue_count = sum(int(row.get("issue_count") or 0) for row in ingest_rows if isinstance(row, dict))
    return {
        "status": "ok",
        "watchlist_table": WATCHLIST_TABLE,
        "events_table": EVENTS_TABLE,
        "watch_count": meta.get("watch_count", 0),
        "watch_update_count": int(len(watchlist_updates)),
        "event_count": int(len(events)),
        "triggered_event_count": meta.get("triggered_event_count", 0),
        "context_observed_count": meta.get("context_observed_count", 0),
        "unique_ingest_targets": meta.get("unique_ingest_targets", 0),
        "total_ingest_targets": meta.get("total_ingest_targets", meta.get("unique_ingest_targets", 0)),
        "processed_ingest_targets": meta.get("processed_ingest_targets", meta.get("unique_ingest_targets", 0)),
        "skipped_ingest_target_count": meta.get("skipped_ingest_target_count", 0),
        "max_ingest_targets": meta.get("max_ingest_targets", 0),
        "unresolved_ingest_target_count": meta.get("unresolved_ingest_target_count", 0),
        "unresolved_watch_row_count": meta.get("unresolved_watch_row_count", 0),
        "managed_ingest_failed_count": meta.get("managed_ingest_failed_count", 0),
        "document_lookup_failed_count": meta.get("document_lookup_failed_count", 0),
        "ingest_failed_count": int(ingest_failed_count),
        "ingest_issue_count": int(ingest_issue_count),
        "ingest_runs": ingest_rows,
        "event_sample": events.head(10).to_dict(orient="records") if not events.empty else [],
    }


def main() -> int:
    args = parse_args()
    asof_date = pd.Timestamp(args.date, tz="UTC") if args.date else None
    to_date = pd.Timestamp(args.to_date, tz="UTC") if args.to_date else None
    watchlist_updates, events, meta = run_announcement_watch(
        asof_date=asof_date,
        symbols=args.symbols,
        setup_ids=args.setup_ids,
        to_date=to_date,
        max_ingest_targets=args.max_ingest_targets,
    )
    if not args.dry_run:
        persist_watch_outputs(watchlist_updates, events)
    result = summarize(watchlist_updates, events, meta)
    result["dry_run"] = bool(args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
