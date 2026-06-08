from __future__ import annotations

import argparse
import json
import re
from typing import Any

import pandas as pd

from utils.db import sql_to_df


DEFAULT_LIMIT = 20
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

ANNOUNCEMENT_DOCUMENTS_TABLE = "announcement_pipeline_documents"
ANNOUNCEMENT_REPORTS_TABLE = "announcement_pipeline_reports"
EXCHANGE_EVENTS_TABLE = "advisory_exchange_events"
EXCHANGE_FEATURES_TABLE = "advisory_exchange_features_daily"
BHAVCOPY_EVIDENCE_TABLE = "advisory_bhavcopy_evidence_daily"
ANNOUNCEMENT_EVIDENCE_TABLE = "advisory_announcement_evidence"

SOURCE_FRESHNESS_CHECKS = [
    {"name": "nse_ohlcv", "table": "nseindia_ohlcv", "date_columns": ["date", "asof_date"], "max_age_days": 10, "required": True},
    {"name": "nse_indices", "table": "nseindia_indices", "date_columns": ["date", "asof_date"], "max_age_days": 10, "required": True},
    {"name": "nse_cmvolt", "table": "nseindia_cmvolt", "date_columns": ["date", "asof_date"], "max_age_days": 10, "required": True},
    {"name": "nse_block_deals", "table": "nseindia_block_deals", "date_columns": ["date", "asof_date"], "max_age_days": 14, "required": True},
    {"name": "nse_bulk_deals", "table": "nseindia_bulk_deals", "date_columns": ["date", "asof_date"], "max_age_days": 14, "required": True},
    {"name": "nse_short_selling", "table": "nseindia_short_selling", "date_columns": ["date", "asof_date"], "max_age_days": 14, "required": True},
    {"name": "nse_circuit_hit", "table": "nseindia_circuit_hit", "date_columns": ["date", "asof_date"], "max_age_days": 30, "required": True},
    {"name": "nse_corporate_actions", "table": "nseindia_corporate_actions", "date_columns": ["date", "ex_date", "record_date"], "max_age_days": 365, "required": False, "sparse_event_source": True, "sync_source": "data.nseindia.corporate_actions", "max_poll_age_days": 10},
    {"name": "nse_corporate_actions_bc_raw", "table": "nseindia_corporate_actions_bc_raw", "date_columns": ["date", "record_date"], "max_age_days": 365, "required": False, "sparse_event_source": True},
    {"name": "nse_corporate_actions_normalized", "table": "nseindia_corporate_actions_normalized", "date_columns": ["date", "ex_date", "record_date"], "max_age_days": 365, "required": False, "sparse_event_source": True},
    {"name": "nse_earnings_events", "table": "nseindia_earnings_events", "date_columns": ["date", "event_date", "asof_date"], "max_age_days": 180, "required": False, "sync_source": "data.nseindia.earnings_events", "max_poll_age_days": 10},
    {"name": "nse_insider_deals", "table": "nseindia_insider_deals", "date_columns": ["date", "reporting_date", "trade_date_to"], "max_age_days": 180, "required": False, "sparse_event_source": True, "sync_source": "data.nseindia.insider_deals", "max_poll_age_days": 10},
    {"name": "announcement_documents", "table": ANNOUNCEMENT_DOCUMENTS_TABLE, "date_columns": ["published_on", "updated_at", "created_at"], "max_age_days": 10, "required": True},
    {"name": "announcement_reports", "table": ANNOUNCEMENT_REPORTS_TABLE, "date_columns": ["published_on", "updated_at", "created_at"], "max_age_days": 10, "required": True},
    {"name": "bhavcopy_evidence", "table": BHAVCOPY_EVIDENCE_TABLE, "date_columns": ["asof_date"], "max_age_days": 10, "required": True},
    {"name": "announcement_evidence", "table": ANNOUNCEMENT_EVIDENCE_TABLE, "date_columns": ["published_on", "asof_date"], "max_age_days": 10, "required": True},
]

HEAVY_TABLE_CHECKS = [
    {"name": "announcement_documents", "table": ANNOUNCEMENT_DOCUMENTS_TABLE, "warn_rows": 1000},
    {"name": "announcement_reports", "table": ANNOUNCEMENT_REPORTS_TABLE, "warn_rows": 1000},
    {"name": "nse_ohlcv", "table": "nseindia_ohlcv", "warn_rows": 500000},
    {"name": "nse_cmvolt", "table": "nseindia_cmvolt", "warn_rows": 500000},
    {"name": "nse_var1", "table": "nseindia_var1", "warn_rows": 500000},
]

EXCHANGE_FEATURE_COLUMNS = {
    "nonzero_score_rows": "exchange_event_score",
    "deal_feature_rows": "deal_cluster_count_20d",
    "short_feature_rows": "short_selling_event_count_20d",
    "corporate_action_feature_rows": "corporate_action_count_30d",
    "earnings_feature_rows": "upcoming_earnings_14d",
}


def _quote_identifier(name: str) -> str:
    if not IDENTIFIER_RE.match(str(name)):
        raise ValueError(f"unsafe SQL identifier: {name}")
    return f'"{name}"'


def _json_ready(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
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


def _status(severity: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"status": severity, "message": message, **extra}


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
    except Exception:
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
    except Exception:
        return set()
    return set(df["column_name"].astype(str).tolist()) if not df.empty else set()


def _choose_column(columns: set[str], candidates: list[str]) -> str | None:
    for column in candidates:
        if column in columns:
            return column
    return None


def _age_days(latest_at: Any, now: pd.Timestamp) -> float | None:
    latest = pd.to_datetime(latest_at, utc=True, errors="coerce")
    if pd.isna(latest):
        return None
    return round(float((now - latest).total_seconds() / 86400.0), 3)


def _row_count(table_name: str) -> int | None:
    try:
        df = sql_to_df(
            f"SELECT COUNT(*) AS row_count FROM {_quote_identifier(table_name)}",
            retries=2,
            statement_timeout_ms=15000,
        )
    except Exception:
        return None
    if df.empty:
        return 0
    return int(df.iloc[0].get("row_count") or 0)


def _distinct_entity_expr(columns: set[str]) -> str:
    column = _choose_column(columns, ["symbol", "ticker", "company_master_id", "isin", "security"])
    if column is None:
        return "NULL::bigint AS distinct_entities"
    return f"COUNT(DISTINCT {_quote_identifier(column)}) AS distinct_entities"


def _coalesce_text_condition(columns: set[str], candidates: list[str]) -> tuple[str, list[str]]:
    usable = [column for column in candidates if column in columns]
    if not usable:
        return "FALSE", []
    checks = [f"NULLIF(TRIM(COALESCE({_quote_identifier(column)}::text, '')), '') IS NOT NULL" for column in usable]
    return " OR ".join(checks), usable


def _status_from_source_row(row: dict[str, Any], *, required: bool, max_age_days: float, age_days: float | None) -> str:
    row_count = int(row.get("row_count") or 0)
    if row_count == 0:
        return "error" if required else "warn"
    if age_days is None or age_days > max_age_days:
        return "warn"
    return "ok"


def _load_sync_state(source_name: str) -> dict[str, Any] | None:
    if not source_name or not table_exists("advisory_sync_state"):
        return None
    try:
        df = sql_to_df(
            """
            SELECT source_name, scope_key, last_success_at, last_item_ts, status, error_text, updated_at, state_json
            FROM advisory_sync_state
            WHERE source_name = %s
            ORDER BY updated_at DESC NULLS LAST
            LIMIT 1
            """,
            params=(source_name,),
            retries=2,
            statement_timeout_ms=5000,
        )
    except Exception:
        return None
    if df.empty:
        return None
    row = df.iloc[0].to_dict()
    for column in ["last_success_at", "last_item_ts", "updated_at"]:
        row[column] = pd.to_datetime(row.get(column), utc=True, errors="coerce")
    return row


def check_source_freshness(now: pd.Timestamp | None = None) -> list[dict[str, Any]]:
    effective_now = pd.to_datetime(now or pd.Timestamp.utcnow(), utc=True, errors="coerce")
    rows: list[dict[str, Any]] = []
    for spec in SOURCE_FRESHNESS_CHECKS:
        name = str(spec["name"])
        table = str(spec["table"])
        required = bool(spec.get("required"))
        max_age_days = float(spec["max_age_days"])
        sparse_event_source = bool(spec.get("sparse_event_source"))
        sync_source = str(spec.get("sync_source") or "")
        max_poll_age_days = float(spec.get("max_poll_age_days") or max_age_days)
        if not table_exists(table):
            rows.append(_status("error" if required else "warn", "Source table is missing.", name=name, table=table, required=required, suggested_fix="Run complete_data.sh or the specific source parser/downloader."))
            continue
        columns = table_columns(table)
        date_column = _choose_column(columns, list(spec.get("date_columns") or []))
        if date_column is None:
            rows.append(
                _status(
                    "error" if required else "warn",
                    "No known freshness column exists on source table.",
                    name=name,
                    table=table,
                    candidate_columns=list(spec.get("date_columns") or []),
                    required=required,
                    suggested_fix="Inspect the source schema and update advisory.event_data_quality date column mapping.",
                )
            )
            continue
        try:
            df = sql_to_df(
                f"""
                SELECT
                    COUNT(*) AS row_count,
                    MIN({_quote_identifier(date_column)}) AS min_at,
                    MAX({_quote_identifier(date_column)}) AS latest_at,
                    {_distinct_entity_expr(columns)}
                FROM {_quote_identifier(table)}
                """,
                retries=2,
                statement_timeout_ms=15000,
            )
        except Exception as exc:
            rows.append(_status("error" if required else "warn", "Freshness query failed.", name=name, table=table, error=f"{type(exc).__name__}: {exc}", required=required, suggested_fix="Check table schema, DB connectivity, and query timeout."))
            continue
        row = df.iloc[0].to_dict() if not df.empty else {}
        age = _age_days(row.get("latest_at"), effective_now)
        severity = _status_from_source_row(row, required=required, max_age_days=max_age_days, age_days=age)
        sync_state = _load_sync_state(sync_source) if sync_source else None
        sync_last_success_at = sync_state.get("last_success_at") if sync_state else None
        sync_age_days = _age_days(sync_last_success_at, effective_now) if sync_state else None
        sync_status = str(sync_state.get("status") or "") if sync_state else None
        if severity == "warn" and sparse_event_source and sync_state and sync_status == "ok" and sync_age_days is not None and sync_age_days <= max_poll_age_days:
            severity = "ok"
        rows.append(
            _status(
                severity,
                "Source is fresh enough." if severity == "ok" else "Source is stale, empty, or incomplete.",
                name=name,
                table=table,
                column=date_column,
                row_count=int(row.get("row_count") or 0),
                distinct_entities=None if pd.isna(row.get("distinct_entities")) else int(row.get("distinct_entities") or 0),
                min_at=_json_ready(pd.to_datetime(row.get("min_at"), utc=True, errors="coerce")),
                latest_at=_json_ready(pd.to_datetime(row.get("latest_at"), utc=True, errors="coerce")),
                age_days=age,
                max_age_days=max_age_days,
                required=required,
                sparse_event_source=sparse_event_source,
                sync_source=sync_source or None,
                sync_status=sync_status,
                sync_last_success_at=_json_ready(sync_last_success_at),
                sync_age_days=sync_age_days,
                max_poll_age_days=max_poll_age_days if sync_source else None,
                suggested_fix="Run complete_data.sh, then rerun python -m advisory.event_data_quality --format json.",
            )
        )
    return rows


def check_announcement_readiness() -> dict[str, Any]:
    table = ANNOUNCEMENT_DOCUMENTS_TABLE
    if not table_exists(table):
        return _status("error", "Announcement document table is missing.", table=table, suggested_fix="Run data.announcements.cli or complete_data.sh.")
    columns = table_columns(table)
    date_column = _choose_column(columns, ["published_on", "updated_at", "created_at"])
    entity_column = _choose_column(columns, ["ticker", "symbol", "company_master_id"])
    if date_column is None:
        return _status("error", "Announcement table has no known date column.", table=table, suggested_fix="Update announcement schema or event_data_quality column mapping.")
    parse_status_expr = _quote_identifier("parse_status") if "parse_status" in columns else "NULL::text"
    ocr_status_expr = _quote_identifier("ocr_status") if "ocr_status" in columns else "NULL::text"
    entity_expr = f"COUNT(DISTINCT {_quote_identifier(entity_column)})" if entity_column else "NULL::bigint"
    text_expr, text_columns = _coalesce_text_condition(columns, ["concise_summary_text", "full_ocr_text", "audio_transcript_text", "text", "ocr_excerpt", "summary"])
    s3_expr, s3_columns = _coalesce_text_condition(columns, ["concise_summary_s3_key", "full_ocr_s3_key", "audio_transcript_s3_key", "raw_s3_key", "ocr_s3_key", "pdf_s3_key"])
    try:
        df = sql_to_df(
            f"""
            SELECT
                COUNT(*) AS row_count,
                MIN({_quote_identifier(date_column)}) AS min_published_on,
                MAX({_quote_identifier(date_column)}) AS max_published_on,
                {entity_expr} AS distinct_entities,
                SUM(CASE WHEN LOWER(COALESCE({parse_status_expr}, '')) IN ('completed', 'success', 'ok', 'processed', 'parsed') THEN 1 ELSE 0 END) AS parsed_rows,
                SUM(CASE WHEN LOWER(COALESCE({parse_status_expr}, '')) IN ('failed', 'error') THEN 1 ELSE 0 END) AS parse_failed_rows,
                SUM(CASE WHEN LOWER(COALESCE({ocr_status_expr}, '')) IN ('failed', 'error') THEN 1 ELSE 0 END) AS ocr_failed_rows,
                SUM(CASE WHEN {text_expr} THEN 1 ELSE 0 END) AS rows_with_text,
                SUM(CASE WHEN {s3_expr} THEN 1 ELSE 0 END) AS rows_with_s3_pointer
            FROM {_quote_identifier(table)}
            """,
            retries=2,
            statement_timeout_ms=15000,
        )
    except Exception as exc:
        return _status("error", "Announcement readiness query failed.", table=table, error=f"{type(exc).__name__}: {exc}", suggested_fix="Inspect announcement_pipeline_documents schema and DB connectivity.")
    row = df.iloc[0].to_dict() if not df.empty else {}
    total = int(row.get("row_count") or 0)
    parse_failed = int(row.get("parse_failed_rows") or 0)
    ocr_failed = int(row.get("ocr_failed_rows") or 0)
    rows_with_text = int(row.get("rows_with_text") or 0)
    failure_rate = round((parse_failed + ocr_failed) / total, 4) if total else None
    text_coverage = round(rows_with_text / total, 4) if total else None
    severity = "ok"
    reasons: list[str] = []
    if total == 0:
        severity = "error"
        reasons.append("no announcement documents")
    if failure_rate is not None and failure_rate > 0.20:
        severity = "warn" if severity == "ok" else severity
        reasons.append("high parse/OCR failure rate")
    if text_coverage is not None and text_coverage < 0.50:
        severity = "warn" if severity == "ok" else severity
        reasons.append("low text/S3 evidence coverage")
    return _status(
        severity,
        "Announcement evidence is usable." if not reasons else "; ".join(reasons),
        table=table,
        date_column=date_column,
        entity_column=entity_column,
        row_count=total,
        distinct_entities=None if pd.isna(row.get("distinct_entities")) else int(row.get("distinct_entities") or 0),
        min_published_on=_json_ready(pd.to_datetime(row.get("min_published_on"), utc=True, errors="coerce")),
        max_published_on=_json_ready(pd.to_datetime(row.get("max_published_on"), utc=True, errors="coerce")),
        parsed_rows=int(row.get("parsed_rows") or 0),
        parse_failed_rows=parse_failed,
        ocr_failed_rows=ocr_failed,
        rows_with_text=rows_with_text,
        rows_with_s3_pointer=int(row.get("rows_with_s3_pointer") or 0),
        failure_rate=failure_rate,
        text_coverage=text_coverage,
        text_columns=text_columns,
        s3_columns=s3_columns,
        suggested_fix="Run announcement ingest/OCR repair or offload missing text to object storage, then rerun this check.",
    )


def check_exchange_event_readiness(limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    table = EXCHANGE_EVENTS_TABLE
    if not table_exists(table):
        return _status("error", "Exchange event table is missing.", table=table, suggested_fix="Run all_advisory.sh after complete_data.sh has parsed NSE source data.")
    columns = table_columns(table)
    required_columns = {"symbol", "known_on", "event_source", "event_type"}
    missing = sorted(required_columns - columns)
    if missing:
        return _status("error", "Exchange event table is missing required columns.", table=table, missing_columns=missing, suggested_fix="Run migrations or rebuild exchange event table schema.")
    try:
        summary = sql_to_df(
            f"""
            SELECT
                COUNT(*) AS row_count,
                COUNT(DISTINCT "symbol") AS distinct_symbols,
                MIN("known_on") AS min_known_on,
                MAX("known_on") AS max_known_on,
                SUM(CASE WHEN NULLIF(TRIM(COALESCE("event_source", '')), '') IS NULL THEN 1 ELSE 0 END) AS null_source_rows,
                SUM(CASE WHEN NULLIF(TRIM(COALESCE("event_type", '')), '') IS NULL THEN 1 ELSE 0 END) AS null_type_rows,
                SUM(CASE WHEN "event_source" = 'nse_corporate_action' THEN 1 ELSE 0 END) AS corporate_action_rows,
                SUM(CASE WHEN "event_source" = 'nse_earnings_event' THEN 1 ELSE 0 END) AS earnings_rows
            FROM {_quote_identifier(table)}
            """,
            retries=2,
            statement_timeout_ms=15000,
        )
        counts = sql_to_df(
            f"""
            SELECT COALESCE("event_source", 'missing') AS event_source, COALESCE("event_type", 'missing') AS event_type, COUNT(*) AS rows
            FROM {_quote_identifier(table)}
            GROUP BY COALESCE("event_source", 'missing'), COALESCE("event_type", 'missing')
            ORDER BY rows DESC
            LIMIT %(limit)s
            """,
            params={"limit": max(1, int(limit))},
            retries=2,
            statement_timeout_ms=15000,
        )
    except Exception as exc:
        return _status("error", "Exchange event readiness query failed.", table=table, error=f"{type(exc).__name__}: {exc}", suggested_fix="Check DB timeout and exchange event schema.")
    row = summary.iloc[0].to_dict() if not summary.empty else {}
    total = int(row.get("row_count") or 0)
    null_source = int(row.get("null_source_rows") or 0)
    null_type = int(row.get("null_type_rows") or 0)
    severity = "ok"
    reasons: list[str] = []
    if total == 0:
        severity = "error"
        reasons.append("exchange event table is empty")
    if null_source or null_type:
        severity = "warn" if severity == "ok" else severity
        reasons.append("exchange events have missing source/type rows")
    if int(row.get("corporate_action_rows") or 0) == 0:
        severity = "warn" if severity == "ok" else severity
        reasons.append("corporate actions are not influencing exchange events")
    return _status(
        severity,
        "Exchange events are typed." if not reasons else "; ".join(reasons),
        table=table,
        row_count=total,
        distinct_symbols=int(row.get("distinct_symbols") or 0),
        min_known_on=_json_ready(pd.to_datetime(row.get("min_known_on"), utc=True, errors="coerce")),
        max_known_on=_json_ready(pd.to_datetime(row.get("max_known_on"), utc=True, errors="coerce")),
        null_source_rows=null_source,
        null_type_rows=null_type,
        null_type_rate=round(max(null_source, null_type) / total, 4) if total else None,
        corporate_action_rows=int(row.get("corporate_action_rows") or 0),
        earnings_rows=int(row.get("earnings_rows") or 0),
        counts_by_source_type=_records(counts),
        suggested_fix="Inspect advisory.exchange_events normalization and rerun all_advisory.sh after source parsing.",
    )


def check_exchange_feature_readiness(now: pd.Timestamp | None = None) -> dict[str, Any]:
    table = EXCHANGE_FEATURES_TABLE
    if not table_exists(table):
        return _status("error", "Exchange feature table is missing.", table=table, suggested_fix="Run all_advisory.sh to build advisory_exchange_features_daily.")
    columns = table_columns(table)
    required_columns = {"symbol", "asof_date"}
    missing = sorted(required_columns - columns)
    if missing:
        return _status("error", "Exchange feature table is missing required columns.", table=table, missing_columns=missing, suggested_fix="Run migrations or rebuild exchange feature table schema.")
    feature_exprs: list[str] = []
    missing_feature_columns: list[str] = []
    for alias, column in EXCHANGE_FEATURE_COLUMNS.items():
        if column in columns:
            if column == "upcoming_earnings_14d":
                feature_exprs.append(f'SUM(CASE WHEN COALESCE({_quote_identifier(column)}, FALSE) THEN 1 ELSE 0 END) AS "{alias}"')
            else:
                feature_exprs.append(f'SUM(CASE WHEN COALESCE({_quote_identifier(column)}, 0) <> 0 THEN 1 ELSE 0 END) AS "{alias}"')
        else:
            missing_feature_columns.append(column)
            feature_exprs.append(f'NULL::bigint AS "{alias}"')
    try:
        df = sql_to_df(
            f"""
            SELECT
                COUNT(*) AS row_count,
                COUNT(DISTINCT "symbol") AS distinct_symbols,
                MIN("asof_date") AS min_asof_date,
                MAX("asof_date") AS max_asof_date,
                {", ".join(feature_exprs)}
            FROM {_quote_identifier(table)}
            """,
            retries=2,
            statement_timeout_ms=15000,
        )
    except Exception as exc:
        return _status("error", "Exchange feature readiness query failed.", table=table, error=f"{type(exc).__name__}: {exc}", suggested_fix="Check DB timeout and exchange feature schema.")
    row = df.iloc[0].to_dict() if not df.empty else {}
    total = int(row.get("row_count") or 0)
    age = _age_days(row.get("max_asof_date"), pd.to_datetime(now or pd.Timestamp.utcnow(), utc=True, errors="coerce"))
    severity = "ok"
    messages: list[str] = []
    if total == 0:
        severity = "error"
        messages.append("feature table is empty")
    if age is None or age > 10:
        severity = "warn" if severity == "ok" else severity
        messages.append("exchange features are stale")
    if missing_feature_columns:
        severity = "warn" if severity == "ok" else severity
        messages.append("expected feature columns are missing")
    corporate_action_rows = row.get("corporate_action_feature_rows")
    if corporate_action_rows is not None and not pd.isna(corporate_action_rows) and int(corporate_action_rows or 0) == 0:
        severity = "warn" if severity == "ok" else severity
        messages.append("corporate-action feature influence is zero")
    return _status(
        severity,
        "Exchange features are ready." if not messages else "; ".join(messages),
        table=table,
        row_count=total,
        distinct_symbols=int(row.get("distinct_symbols") or 0),
        min_asof_date=_json_ready(pd.to_datetime(row.get("min_asof_date"), utc=True, errors="coerce")),
        max_asof_date=_json_ready(pd.to_datetime(row.get("max_asof_date"), utc=True, errors="coerce")),
        age_days=age,
        missing_feature_columns=missing_feature_columns,
        nonzero_score_rows=None if pd.isna(row.get("nonzero_score_rows")) else int(row.get("nonzero_score_rows") or 0),
        deal_feature_rows=None if pd.isna(row.get("deal_feature_rows")) else int(row.get("deal_feature_rows") or 0),
        short_feature_rows=None if pd.isna(row.get("short_feature_rows")) else int(row.get("short_feature_rows") or 0),
        corporate_action_feature_rows=None if pd.isna(corporate_action_rows) else int(corporate_action_rows or 0),
        earnings_feature_rows=None if pd.isna(row.get("earnings_feature_rows")) else int(row.get("earnings_feature_rows") or 0),
        suggested_fix="Run all_advisory.sh after source parsing; if still zero, inspect advisory.exchange_features mappings.",
    )


def check_raw_table_risk() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for spec in HEAVY_TABLE_CHECKS:
        table = str(spec["table"])
        warn_rows = int(spec["warn_rows"])
        if not table_exists(table):
            rows.append(_status("warn", "Heavy source table is missing.", name=spec["name"], table=table, suggested_fix="Run complete_data.sh if this source is expected."))
            continue
        row_count = _row_count(table)
        if row_count is None:
            rows.append(_status("warn", "Could not count heavy source table.", name=spec["name"], table=table, suggested_fix="Check DB timeout or table availability."))
            continue
        severity = "warn" if row_count >= warn_rows else "ok"
        rows.append(
            _status(
                severity,
                "Use compact cached evidence tables for UI/advisory access." if severity == "warn" else "Table size is below raw-scan warning threshold.",
                name=spec["name"],
                table=table,
                row_count=row_count,
                warn_rows=warn_rows,
                suggested_fix="Build compact evidence caches before querying this table from UI or LLM prompts." if severity == "warn" else None,
            )
        )
    return rows


def summarize_status(sections: dict[str, Any]) -> str:
    statuses: list[str] = []
    for value in sections.values():
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict):
                statuses.append(str(item.get("status") or "ok"))
    if "error" in statuses:
        return "error"
    if "warn" in statuses:
        return "warn"
    return "ok"


def _issue_code(section_name: str, row: dict[str, Any]) -> str:
    name = str(row.get("name") or row.get("table") or section_name)
    return f"{section_name}:{name}"


def build_event_data_quality_report(*, limit: int = DEFAULT_LIMIT, now: pd.Timestamp | None = None) -> dict[str, Any]:
    effective_now = pd.to_datetime(now or pd.Timestamp.utcnow(), utc=True, errors="coerce")
    sections: dict[str, Any] = {
        "source_freshness": check_source_freshness(now=effective_now),
        "announcement_readiness": check_announcement_readiness(),
        "exchange_event_readiness": check_exchange_event_readiness(limit=limit),
        "exchange_feature_readiness": check_exchange_feature_readiness(now=effective_now),
        "raw_table_risk": check_raw_table_risk(),
    }
    issue_rows: list[dict[str, Any]] = []
    for section_name, value in sections.items():
        rows = value if isinstance(value, list) else [value]
        for row in rows:
            if isinstance(row, dict) and row.get("status") in {"error", "warn"}:
                issue_rows.append({"section": section_name, "issue_code": _issue_code(section_name, row), **row})
    issue_rows.sort(key=lambda row: (0 if row.get("status") == "error" else 1, str(row.get("issue_code") or "")))
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "status": summarize_status(sections),
        "message": "Announcement and NSE/bhavcopy evidence quality gate completed.",
        "summary": {
            "issue_count": len(issue_rows),
            "error_count": sum(1 for row in issue_rows if row.get("status") == "error"),
            "warn_count": sum(1 for row in issue_rows if row.get("status") == "warn"),
            "compact_evidence_required": True,
            "llm_signal_authority": "proposed_signal_only",
        },
        "sections": sections,
        "issues": issue_rows[: max(1, int(limit))],
    }


def build_event_data_quality(*, limit: int = DEFAULT_LIMIT, now: pd.Timestamp | None = None) -> dict[str, Any]:
    return build_event_data_quality_report(limit=limit, now=now)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only quality gate for announcement and NSE/bhavcopy evidence readiness.")
    parser.add_argument("--format", choices=["json", "text"], default="json")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = build_event_data_quality_report(limit=max(1, int(args.limit)))
    if args.format == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    else:
        print(f"event_data_quality status={payload['status']} issues={payload['summary']['issue_count']}")
        for row in payload["issues"]:
            print(f"- {row.get('status')} {row.get('section')} {row.get('name') or row.get('table')}: {row.get('message')}")
    return 0 if payload["status"] in {"ok", "warn"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
