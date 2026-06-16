from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from datetime import timedelta

from psycopg2 import sql

from advisory.fallback_telemetry import record_local_fallback_event
from utils.db import db_session, execute_db_operation
from utils.schema_migrations import apply_schema_migration


TABLE_NAME = "ingestion_file_state"
CLASSIFICATION_RE = re.compile(r"(?:^|;\s*)classification=([^;]+)")
OBJECT_DATE_RE = re.compile(r"(20\d{2})[-_](\d{2})[-_](\d{2})")
CLASSIFICATION_CATALOG: dict[str, dict[str, str]] = {
    "ok": {
        "label": "Completed",
        "meaning": "The source ran successfully and wrote or confirmed usable data.",
        "operator_action": "No action required.",
        "trust_impact": "does_not_block_current_trust",
    },
    "no_data": {
        "label": "No new data",
        "meaning": "The source ran successfully but had no new rows to write for the requested window.",
        "operator_action": "No action required unless you expected this source to publish new data for the window.",
        "trust_impact": "does_not_block_current_trust",
    },
    "empty_valid_source": {
        "label": "Valid empty source",
        "meaning": "The source file or response was readable but contained no usable rows. This is completed no-data, not a parser failure.",
        "operator_action": "Do not retry unless you know the exchange/source should have published rows for that date.",
        "trust_impact": "does_not_block_current_trust",
    },
    "bad_file_retryable": {
        "label": "Retryable bad file",
        "meaning": "The downloaded file was corrupt, incomplete, or unreadable. The parser is probably fine, but the file should be downloaded again.",
        "operator_action": "Clear or redownload this object, rerun the downloader/parser, then refresh Health.",
        "trust_impact": "active_recent_failures_warn",
    },
    "schema_changed": {
        "label": "Schema changed",
        "meaning": "The source file parsed but expected columns or layout changed. Parser/schema mapping needs code review before trusting this source.",
        "operator_action": "Inspect a fresh source sample, update the parser/schema mapping, rerun the parser, then refresh Health.",
        "trust_impact": "active_recent_failures_block_trust",
    },
    "parser_bug": {
        "label": "Parser bug",
        "meaning": "The parser hit an unexpected branch or exception not explained by an empty source or known schema change.",
        "operator_action": "Fix parser logic or add a more specific classification, rerun the parser, then refresh Health.",
        "trust_impact": "active_recent_failures_block_trust",
    },
    "source_unavailable": {
        "label": "Source unavailable",
        "meaning": "The upstream website/API was unavailable, blocked, or timed out. This is not valid no-data.",
        "operator_action": "Retry later or through the serialized source queue; do not treat the empty output as complete data.",
        "trust_impact": "active_recent_failures_warn",
    },
    "auth_unavailable": {
        "label": "Authentication unavailable",
        "meaning": "The source requires login/token/cookies and the current credentials were unavailable or expired.",
        "operator_action": "Refresh the relevant credentials/session, rerun the source, then refresh Health.",
        "trust_impact": "active_recent_failures_block_trust",
    },
    "reference_mapping_missing": {
        "label": "Reference mapping missing",
        "meaning": "The source ran, but a required local reference/master mapping was missing for one or more symbols.",
        "operator_action": "Refresh the relevant master/reference data or add the missing symbol mapping, then rerun the affected source.",
        "trust_impact": "active_recent_failures_block_trust",
    },
    "partial_failed": {
        "label": "Partially failed",
        "meaning": "The source wrote some usable rows, but one or more requested symbols/windows failed and may be stale.",
        "operator_action": "Inspect failed symbols/windows in run-state, fix the root cause, and rerun the affected source.",
        "trust_impact": "active_recent_failures_warn",
    },
    "parse_failed": {
        "label": "Parse failed",
        "meaning": "Parsing failed but the source did not provide a more specific parser classification.",
        "operator_action": "Inspect the error and source sample, classify the failure more specifically, then rerun.",
        "trust_impact": "active_recent_failures_warn",
    },
}
INGESTION_STATE_SCHEMA_MIGRATION_ID = "20260611_ingestion_file_state_base"
INGESTION_STATE_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        source_prefix TEXT NOT NULL,
        object_key TEXT NOT NULL,
        status TEXT NOT NULL,
        error_message TEXT NULL,
        processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (source_prefix, object_key)
    )
    """,
    f"""
    ALTER TABLE {TABLE_NAME}
    ADD COLUMN IF NOT EXISTS error_message TEXT NULL
    """,
]


def ensure_ingestion_state_table() -> None:
    apply_schema_migration(
        migration_id=INGESTION_STATE_SCHEMA_MIGRATION_ID,
        statements=INGESTION_STATE_SCHEMA_STATEMENTS,
        owner="utils.ingestion_state",
        description="Create file-level ingestion state table.",
        metadata={"tables": [TABLE_NAME], "workflow": "file_ingestion_state"},
    )


def get_processed_keys(source_prefix: str, *, status: str = "processed") -> set[str]:
    ensure_ingestion_state_table()

    def _load_keys() -> set[str]:
        with db_session() as (_, cur):
            cur.execute(
                sql.SQL(
                    """
                    SELECT object_key
                    FROM {}
                    WHERE source_prefix = %s
                      AND status = %s
                    """
                ).format(sql.Identifier(TABLE_NAME)),
                (source_prefix, status),
            )
            return {row[0] for row in cur.fetchall()}

    return execute_db_operation(
        _load_keys,
        operation_name="ingestion_state:get_processed_keys",
    )


def mark_processed(source_prefix: str, object_key: str, *, status: str = "processed") -> None:
    mark_state(source_prefix, object_key, status=status)


def mark_failed(source_prefix: str, object_key: str, error_message: str) -> None:
    mark_state(source_prefix, object_key, status="failed", error_message=error_message)


def mark_state(
    source_prefix: str,
    object_key: str,
    *,
    status: str,
    error_message: str | None = None,
) -> None:
    ensure_ingestion_state_table()
    processed_at = datetime.now(timezone.utc)

    def _upsert_state() -> None:
        with db_session() as (_, cur):
            cur.execute(
                sql.SQL(
                    """
                    INSERT INTO {} (source_prefix, object_key, status, error_message, processed_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (source_prefix, object_key)
                    DO UPDATE
                    SET status = EXCLUDED.status,
                        error_message = EXCLUDED.error_message,
                        processed_at = EXCLUDED.processed_at
                    """
                ).format(sql.Identifier(TABLE_NAME)),
                (source_prefix, object_key, status, error_message, processed_at),
            )

    execute_db_operation(
        _upsert_state,
        operation_name="ingestion_state:mark_state",
    )


def get_failed_entries(source_prefix: str) -> list[dict[str, object]]:
    ensure_ingestion_state_table()

    def _load_failed() -> list[dict[str, object]]:
        with db_session(dict_factory=True) as (_, cur):
            cur.execute(
                sql.SQL(
                    """
                    SELECT object_key, status, error_message, processed_at
                    FROM {}
                    WHERE source_prefix = %s
                      AND status = 'failed'
                    ORDER BY processed_at DESC, object_key
                    """
                ).format(sql.Identifier(TABLE_NAME)),
                (source_prefix,),
            )
            return list(cur.fetchall())

    return execute_db_operation(
        _load_failed,
        operation_name="ingestion_state:get_failed_entries",
    )


def get_state_entries(
    source_prefix: str | None = None,
    *,
    status: str | None = None,
    limit: int | None = None,
) -> list[dict[str, object]]:
    ensure_ingestion_state_table()
    clauses: list[str] = []
    params: list[object] = []
    if source_prefix is not None:
        clauses.append("source_prefix = %s")
        params.append(source_prefix)
    if status is not None:
        clauses.append("status = %s")
        params.append(status)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit_sql = "LIMIT %s" if limit is not None else ""
    if limit is not None:
        params.append(int(limit))

    def _load_entries() -> list[dict[str, object]]:
        with db_session(dict_factory=True) as (_, cur):
            cur.execute(
                f"""
                SELECT source_prefix, object_key, status, error_message, processed_at
                FROM {TABLE_NAME}
                {where_sql}
                ORDER BY processed_at DESC, source_prefix, object_key
                {limit_sql}
                """,
                tuple(params),
            )
            return list(cur.fetchall())

    return execute_db_operation(
        _load_entries,
        operation_name="ingestion_state:get_state_entries",
    )


def extract_failure_classification(error_message: object) -> str | None:
    if not error_message:
        return None
    match = CLASSIFICATION_RE.search(str(error_message))
    if not match:
        return None
    return match.group(1).strip() or None


def classification_metadata(classification: object) -> dict[str, str]:
    key = str(classification or "unknown").strip() or "unknown"
    if key in CLASSIFICATION_CATALOG:
        return {"classification": key, **CLASSIFICATION_CATALOG[key]}
    return {
        "classification": key,
        "label": key.replace("_", " ").title(),
        "meaning": "This ingestion classification is not yet in the shared catalog.",
        "operator_action": "Inspect the source error, add a specific classification if this recurs, and rerun the affected source.",
        "trust_impact": "unknown",
    }


def extract_object_date(object_key: object) -> datetime | None:
    if not object_key:
        return None
    match = OBJECT_DATE_RE.search(str(object_key))
    if not match:
        return None
    try:
        return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=timezone.utc)
    except ValueError as exc:
        record_local_fallback_event(
            module="utils.ingestion_state",
            source="ingestion_object_date",
            fallback_type="ingestion_object_date_invalid",
            severity="warn",
            reason="Ingestion state could not parse an object date and left lifecycle classification date-free.",
            error=exc,
            metadata={"object_key": str(object_key)[:300]},
        )
        return None


def classify_failure_lifecycle(
    row: dict[str, object],
    *,
    active_days: int = 30,
    now: datetime | None = None,
) -> str:
    if str(row.get("status") or "").lower() != "failed":
        return "not_failed"
    effective_now = now or datetime.now(timezone.utc)
    cutoff = effective_now - timedelta(days=max(1, int(active_days)))
    object_date = extract_object_date(row.get("object_key"))
    if object_date is not None and object_date < cutoff:
        return "stale_historical"
    processed_at = row.get("processed_at")
    try:
        processed_ts = getattr(processed_at, "to_pydatetime", lambda: processed_at)()
        if processed_ts.tzinfo is None:
            processed_ts = processed_ts.replace(tzinfo=timezone.utc)
        if processed_ts < cutoff:
            return "stale_historical"
    except Exception as exc:
        record_local_fallback_event(
            module="utils.ingestion_state",
            source="failure_lifecycle",
            fallback_type="processed_at_parse_failed",
            severity="warn",
            reason="Ingestion state could not parse processed_at and kept the failed item active.",
            error=exc,
            metadata={"source_prefix": row.get("source_prefix"), "object_key": str(row.get("object_key") or "")[:300]},
        )
        pass
    return "active"


def summarize_state_entries(
    rows: list[dict[str, object]],
    *,
    sample_limit: int = 20,
    active_failure_days: int = 30,
) -> dict[str, object]:
    status_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    classification_counts: Counter[str] = Counter()
    failure_lifecycle_counts: Counter[str] = Counter()
    active_failure_rows: list[dict[str, object]] = []
    stale_failure_rows: list[dict[str, object]] = []
    enriched_rows: list[dict[str, object]] = []

    for row in rows:
        status = str(row.get("status") or "unknown")
        source_prefix = str(row.get("source_prefix") or "unknown")
        status_counts[status] += 1
        source_counts[source_prefix] += 1

        classification = extract_failure_classification(row.get("error_message"))
        if classification:
            classification_counts[classification] += 1
        lifecycle = classify_failure_lifecycle(row, active_days=active_failure_days)
        failure_lifecycle_counts[lifecycle] += 1
        classification_detail = classification_metadata(classification) if classification else None
        enriched = {
            **row,
            "failure_lifecycle": lifecycle,
            "classification": classification,
            "classification_detail": classification_detail,
        }
        enriched_rows.append(enriched)
        if lifecycle == "active":
            active_failure_rows.append(enriched)
        elif lifecycle == "stale_historical":
            stale_failure_rows.append(enriched)

    return {
        "status": "ok",
        "count": len(rows),
        "status_counts": dict(sorted(status_counts.items())),
        "source_counts": dict(sorted(source_counts.items())),
        "classification_counts": dict(sorted(classification_counts.items())),
        "classification_details": {
            key: classification_metadata(key)
            for key in sorted(classification_counts)
        },
        "failure_lifecycle_counts": dict(sorted(failure_lifecycle_counts.items())),
        "active_failure_count": len(active_failure_rows),
        "stale_historical_failure_count": len(stale_failure_rows),
        "active_failure_sample_rows": active_failure_rows[: max(0, int(sample_limit))],
        "stale_historical_failure_sample_rows": stale_failure_rows[: max(0, int(sample_limit))],
        "enriched_sample_rows": enriched_rows[: max(0, int(sample_limit))],
        "sample_rows": rows[: max(0, int(sample_limit))],
    }


def clear_state(source_prefix: str, object_key: str) -> None:
    ensure_ingestion_state_table()

    def _delete_state() -> None:
        with db_session() as (_, cur):
            cur.execute(
                sql.SQL("DELETE FROM {} WHERE source_prefix = %s AND object_key = %s").format(sql.Identifier(TABLE_NAME)),
                (source_prefix, object_key),
            )

    execute_db_operation(
        _delete_state,
        operation_name="ingestion_state:clear_state",
    )
