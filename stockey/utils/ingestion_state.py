from __future__ import annotations

from datetime import datetime, timezone

from psycopg2 import sql

from utils.db import db_session


TABLE_NAME = "ingestion_file_state"


def ensure_ingestion_state_table() -> None:
    with db_session() as (_, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                source_prefix TEXT NOT NULL,
                object_key TEXT NOT NULL,
                status TEXT NOT NULL,
                error_message TEXT NULL,
                processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (source_prefix, object_key)
            )
            """
        )
        cur.execute(
            f"""
            ALTER TABLE {TABLE_NAME}
            ADD COLUMN IF NOT EXISTS error_message TEXT NULL
            """
        )


def get_processed_keys(source_prefix: str, *, status: str = "processed") -> set[str]:
    ensure_ingestion_state_table()
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


def get_failed_entries(source_prefix: str) -> list[dict[str, object]]:
    ensure_ingestion_state_table()
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


def clear_state(source_prefix: str, object_key: str) -> None:
    ensure_ingestion_state_table()
    with db_session() as (_, cur):
        cur.execute(
            sql.SQL("DELETE FROM {} WHERE source_prefix = %s AND object_key = %s").format(sql.Identifier(TABLE_NAME)),
            (source_prefix, object_key),
        )
