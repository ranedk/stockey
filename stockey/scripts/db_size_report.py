from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from typing import Any

from utils.db import sql_to_df


def _json_default(value: Any) -> str | int | float | None:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def build_size_report(*, limit: int = 30, schema: str = "public", include_text_columns: bool = False) -> dict[str, Any]:
    table_sql = """
        SELECT
            schemaname AS schema_name,
            relname AS table_name,
            n_live_tup::bigint AS estimated_rows,
            pg_total_relation_size(relid)::bigint AS total_bytes,
            pg_relation_size(relid)::bigint AS table_bytes,
            pg_indexes_size(relid)::bigint AS index_bytes,
            (pg_total_relation_size(relid) - pg_relation_size(relid) - pg_indexes_size(relid))::bigint AS toast_bytes,
            pg_size_pretty(pg_total_relation_size(relid)) AS total_size,
            pg_size_pretty(pg_relation_size(relid)) AS table_size,
            pg_size_pretty(pg_indexes_size(relid)) AS index_size
        FROM pg_stat_user_tables
        WHERE schemaname = %s
        ORDER BY pg_total_relation_size(relid) DESC
        LIMIT %s
    """
    index_sql = """
        SELECT
            schemaname AS schema_name,
            relname AS table_name,
            indexrelname AS index_name,
            idx_scan::bigint AS index_scans,
            pg_relation_size(indexrelid)::bigint AS index_bytes,
            pg_size_pretty(pg_relation_size(indexrelid)) AS index_size
        FROM pg_stat_user_indexes
        WHERE schemaname = %s
        ORDER BY pg_relation_size(indexrelid) DESC
        LIMIT %s
    """
    text_sql = """
        SELECT
            table_schema AS schema_name,
            table_name,
            column_name,
            data_type
        FROM information_schema.columns
        WHERE table_schema = %s
          AND data_type IN ('text', 'json', 'jsonb', 'character varying')
        ORDER BY table_name, column_name
    """
    text_summary_sql = """
        SELECT
            table_name,
            COUNT(*)::bigint AS text_column_count
        FROM information_schema.columns
        WHERE table_schema = %s
          AND data_type IN ('text', 'json', 'jsonb', 'character varying')
        GROUP BY table_name
        ORDER BY COUNT(*) DESC, table_name
        LIMIT %s
    """
    tables = sql_to_df(table_sql, params=(schema, int(limit)))
    indexes = sql_to_df(index_sql, params=(schema, int(limit)))
    text_summary = sql_to_df(text_summary_sql, params=(schema, int(limit)))
    text_columns = sql_to_df(text_sql, params=(schema,)) if include_text_columns else None
    text_column_count = int(text_summary["text_column_count"].sum()) if not text_summary.empty else 0
    return {
        "schema": schema,
        "table_count": int(len(tables)),
        "index_count": int(len(indexes)),
        "text_column_count_in_top_tables": text_column_count,
        "largest_tables": tables.to_dict(orient="records"),
        "largest_indexes": indexes.to_dict(orient="records"),
        "text_column_tables": text_summary.to_dict(orient="records"),
        **({"text_columns": text_columns.to_dict(orient="records")} if text_columns is not None else {}),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report largest Postgres tables/indexes and text-heavy columns.")
    parser.add_argument("--limit", type=int, default=30, help="Number of largest tables and indexes to show.")
    parser.add_argument("--schema", default="public", help="Postgres schema to inspect.")
    parser.add_argument("--include-text-columns", action="store_true", help="Include every text/json column in output.")
    args = parser.parse_args(argv)
    print(
        json.dumps(
            build_size_report(
                limit=args.limit,
                schema=args.schema,
                include_text_columns=bool(args.include_text_columns),
            ),
            indent=2,
            default=_json_default,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
