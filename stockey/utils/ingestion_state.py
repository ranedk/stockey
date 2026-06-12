from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone

from psycopg2 import sql

from utils.db import db_session, execute_db_operation
from utils.schema_migrations import apply_schema_migration


TABLE_NAME = "ingestion_file_state"
CLASSIFICATION_RE = re.compile(r"(?:^|;\s*)classification=([^;]+)")
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


def summarize_state_entries(rows: list[dict[str, object]], *, sample_limit: int = 20) -> dict[str, object]:
    status_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    classification_counts: Counter[str] = Counter()

    for row in rows:
        status = str(row.get("status") or "unknown")
        source_prefix = str(row.get("source_prefix") or "unknown")
        status_counts[status] += 1
        source_counts[source_prefix] += 1

        classification = extract_failure_classification(row.get("error_message"))
        if classification:
            classification_counts[classification] += 1

    return {
        "status": "ok",
        "count": len(rows),
        "status_counts": dict(sorted(status_counts.items())),
        "source_counts": dict(sorted(source_counts.items())),
        "classification_counts": dict(sorted(classification_counts.items())),
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
