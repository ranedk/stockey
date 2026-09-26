from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from utils.db import db_session, execute_db_operation, sql_to_df


TABLE_NAME = "stockey_schema_migrations"


def ensure_schema_migrations_table() -> None:
    def _ensure_schema_migrations_table() -> None:
        with db_session() as (_, cur):
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                    migration_id TEXT PRIMARY KEY,
                    checksum TEXT NOT NULL,
                    description TEXT,
                    status TEXT NOT NULL,
                    started_at TIMESTAMPTZ,
                    applied_at TIMESTAMPTZ,
                    finished_at TIMESTAMPTZ,
                    error_text TEXT,
                    statements_json TEXT,
                    metadata_json TEXT
                )
                """
            )
            cur.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS metadata_json TEXT")

    execute_db_operation(
        _ensure_schema_migrations_table,
        operation_name="schema_migrations:ensure_table",
    )


def checksum_statements(statements: Iterable[str]) -> str:
    normalized = "\n\n".join(statement.strip() for statement in statements if statement and statement.strip())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def apply_schema_migration(
    *,
    migration_id: str,
    description: str,
    statements: list[str],
    owner: str | None = None,
    metadata: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    if not migration_id.strip():
        raise ValueError("migration_id is required")
    executable_statements = [statement.strip() for statement in statements if statement and statement.strip()]
    if not executable_statements:
        raise ValueError("at least one SQL statement is required")

    checksum = checksum_statements(executable_statements)
    ensure_schema_migrations_table()
    existing = load_schema_migration(migration_id)
    if existing:
        existing_checksum = str(existing.get("checksum") or "")
        if existing_checksum and existing_checksum != checksum:
            raise ValueError(
                f"Migration checksum mismatch for {migration_id}: existing={existing_checksum} new={checksum}"
            )
        if str(existing.get("status") or "").lower() == "applied":
            return {
                "status": "skipped_already_applied",
                "migration_id": migration_id,
                "checksum": checksum,
                "dry_run": bool(dry_run),
            }

    normalized_metadata = dict(metadata or {})
    if owner:
        normalized_metadata.setdefault("owner", owner)

    payload = {
        "migration_id": migration_id,
        "checksum": checksum,
        "description": description,
        "statements_json": json.dumps(executable_statements, ensure_ascii=False),
        "metadata_json": json.dumps(normalized_metadata, ensure_ascii=False, sort_keys=True),
    }
    if dry_run:
        return {
            "status": "dry_run",
            **payload,
            "statement_count": len(executable_statements),
            "dry_run": True,
        }

    try:
        def _apply_schema_migration() -> None:
            with db_session() as (_, cur):
                cur.execute(
                    f"""
                    INSERT INTO {TABLE_NAME}
                        (migration_id, checksum, description, status, started_at, statements_json, metadata_json)
                    VALUES (%(migration_id)s, %(checksum)s, %(description)s, 'running', NOW(), %(statements_json)s, %(metadata_json)s)
                    ON CONFLICT (migration_id) DO UPDATE SET
                        checksum = EXCLUDED.checksum,
                        description = EXCLUDED.description,
                        status = 'running',
                        started_at = NOW(),
                        finished_at = NULL,
                        error_text = NULL,
                        statements_json = EXCLUDED.statements_json,
                        metadata_json = EXCLUDED.metadata_json
                    """,
                    payload,
                )
                for statement in executable_statements:
                    cur.execute(statement)
                cur.execute(
                    f"""
                    UPDATE {TABLE_NAME}
                    SET status = 'applied',
                        applied_at = NOW(),
                        finished_at = NOW(),
                        error_text = NULL
                    WHERE migration_id = %(migration_id)s
                    """,
                    {"migration_id": migration_id},
                )

        execute_db_operation(
            _apply_schema_migration,
            operation_name=f"schema_migrations:apply:{migration_id}",
        )
    except Exception as exc:
        record_schema_migration_failure(
            migration_id=migration_id,
            checksum=checksum,
            description=description,
            statements=executable_statements,
            metadata=normalized_metadata,
            error_text=f"{type(exc).__name__}: {exc}",
        )
        raise

    return {
        "status": "applied",
        "migration_id": migration_id,
        "checksum": checksum,
        "statement_count": len(executable_statements),
        "dry_run": False,
    }


def record_schema_migration_failure(
    *,
    migration_id: str,
    checksum: str,
    description: str,
    statements: list[str],
    metadata: dict[str, Any],
    error_text: str,
) -> None:
    ensure_schema_migrations_table()
    payload = {
        "migration_id": migration_id,
        "checksum": checksum,
        "description": description,
        "error_text": error_text,
        "statements_json": json.dumps(statements, ensure_ascii=False),
        "metadata_json": json.dumps(metadata, ensure_ascii=False, sort_keys=True),
    }

    def _record_schema_migration_failure() -> None:
        with db_session() as (_, cur):
            cur.execute(
                f"""
                INSERT INTO {TABLE_NAME}
                    (migration_id, checksum, description, status, started_at, finished_at, error_text, statements_json, metadata_json)
                VALUES (%(migration_id)s, %(checksum)s, %(description)s, 'failed', NOW(), NOW(), %(error_text)s, %(statements_json)s, %(metadata_json)s)
                ON CONFLICT (migration_id) DO UPDATE SET
                    checksum = EXCLUDED.checksum,
                    description = EXCLUDED.description,
                    status = 'failed',
                    finished_at = NOW(),
                    error_text = EXCLUDED.error_text,
                    statements_json = EXCLUDED.statements_json,
                    metadata_json = EXCLUDED.metadata_json
                """,
                payload,
            )

    execute_db_operation(
        _record_schema_migration_failure,
        operation_name=f"schema_migrations:record_failure:{migration_id}",
    )


def load_schema_migration(migration_id: str) -> dict[str, Any] | None:
    ensure_schema_migrations_table()
    df = sql_to_df(
        f"""
        SELECT *
        FROM {TABLE_NAME}
        WHERE migration_id = %s
        LIMIT 1
        """,
        params=(migration_id,),
    )
    if df.empty:
        return None
    return df.iloc[0].to_dict()


def list_schema_migrations(limit: int = 100) -> pd.DataFrame:
    ensure_schema_migrations_table()
    return sql_to_df(
        f"""
        SELECT migration_id, status, checksum, description, started_at, applied_at, finished_at, error_text, metadata_json
        FROM {TABLE_NAME}
        ORDER BY COALESCE(finished_at, started_at) DESC NULLS LAST, migration_id DESC
        LIMIT %s
        """,
        params=(int(limit),),
    )


def read_sql_file(path: str) -> list[str]:
    text = Path(path).read_text(encoding="utf-8")
    return [chunk.strip() for chunk in text.split(";") if chunk.strip()]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect or apply Stockey schema migrations.")
    parser.add_argument("--ensure-table", action="store_true", help="Create the migration registry table if needed.")
    parser.add_argument("--list", action="store_true", help="List recent migration registry rows.")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--migration-id")
    parser.add_argument("--description", default="")
    parser.add_argument("--sql-file")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.ensure_table:
        ensure_schema_migrations_table()
        print(json.dumps({"status": "ok", "table": TABLE_NAME}, ensure_ascii=False))
        return 0
    if args.list:
        rows = list_schema_migrations(limit=args.limit).to_dict(orient="records")
        print(json.dumps({"status": "ok", "table": TABLE_NAME, "rows": rows}, ensure_ascii=False, default=str))
        return 0
    if args.migration_id and args.sql_file:
        result = apply_schema_migration(
            migration_id=args.migration_id,
            description=args.description,
            statements=read_sql_file(args.sql_file),
            metadata={"sql_file": args.sql_file},
            dry_run=bool(args.dry_run),
        )
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0
    raise SystemExit("Use --ensure-table, --list, or --migration-id with --sql-file.")


if __name__ == "__main__":
    raise SystemExit(main())
