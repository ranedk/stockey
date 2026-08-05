from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from typing import Any

from utils.fallback_telemetry import record_local_fallback_event
from utils.db import sql_to_df


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def build_duplicate_index_report(*, schema: str = "public", limit: int = 50) -> dict[str, Any]:
    query = """
        WITH index_cols AS (
            SELECT
                n.nspname AS schema_name,
                t.relname AS table_name,
                ix.relname AS index_name,
                i.indisunique AS is_unique,
                i.indisprimary AS is_primary,
                c.conname AS constraint_name,
                pg_relation_size(ix.oid)::bigint AS index_bytes,
                COALESCE(s.idx_scan, 0)::bigint AS index_scans,
                (
                    SELECT array_agg(a.attname::text ORDER BY key_cols.ord)
                    FROM unnest(i.indkey) WITH ORDINALITY AS key_cols(attnum, ord)
                    JOIN pg_attribute a
                      ON a.attrelid = t.oid
                     AND a.attnum = key_cols.attnum
                ) AS columns
            FROM pg_index i
            JOIN pg_class t ON t.oid = i.indrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            JOIN pg_class ix ON ix.oid = i.indexrelid
            LEFT JOIN pg_constraint c ON c.conindid = ix.oid
            LEFT JOIN pg_stat_user_indexes s ON s.indexrelid = ix.oid
            WHERE n.nspname = %s
              AND i.indisvalid
              AND i.indkey IS NOT NULL
        ),
        duplicate_groups AS (
            SELECT
                schema_name,
                table_name,
                is_unique,
                columns,
                COUNT(*) AS duplicate_count,
                SUM(index_bytes)::bigint AS total_index_bytes
            FROM index_cols
            WHERE columns IS NOT NULL
            GROUP BY schema_name, table_name, is_unique, columns
            HAVING COUNT(*) > 1
        )
        SELECT
            g.schema_name,
            g.table_name,
            g.is_unique,
            g.columns::text AS columns,
            g.duplicate_count,
            g.total_index_bytes,
            pg_size_pretty(g.total_index_bytes) AS total_duplicate_size,
            json_agg(
                json_build_object(
                    'index_name', i.index_name,
                    'constraint_name', i.constraint_name,
                    'is_primary', i.is_primary,
                    'index_bytes', i.index_bytes,
                    'index_size', pg_size_pretty(i.index_bytes),
                    'index_scans', i.index_scans,
                    'safe_drop_candidate', (i.constraint_name IS NULL AND NOT i.is_primary)
                )
                ORDER BY
                    (i.constraint_name IS NULL AND NOT i.is_primary) DESC,
                    i.index_scans ASC,
                    i.index_bytes DESC
            ) AS indexes
        FROM duplicate_groups g
        JOIN index_cols i
          ON i.schema_name = g.schema_name
         AND i.table_name = g.table_name
         AND i.is_unique = g.is_unique
         AND i.columns = g.columns
        GROUP BY
            g.schema_name,
            g.table_name,
            g.is_unique,
            g.columns,
            g.duplicate_count,
            g.total_index_bytes
        ORDER BY g.total_index_bytes DESC
        LIMIT %s
    """
    df = sql_to_df(query, params=(schema, int(limit)))
    rows = df.to_dict(orient="records")
    drop_candidates: list[str] = []
    for row in rows:
        indexes = row.get("indexes") or []
        if isinstance(indexes, str):
            try:
                indexes = json.loads(indexes)
            except json.JSONDecodeError as exc:
                record_local_fallback_event(
                    module="scripts.db_duplicate_index_report",
                    fallback_type="duplicate_index_report_indexes_json_parse_failed",
                    source="pg_catalog_duplicate_index_report",
                    severity="warn",
                    reason="Duplicate-index report could not parse index metadata JSON and skipped that duplicate group.",
                    error=exc,
                    metadata={
                        "schema_name": row.get("schema_name"),
                        "table_name": row.get("table_name"),
                        "indexes_length": len(indexes),
                        "indexes_excerpt": indexes[:240],
                    },
                )
                indexes = []
        for item in indexes:
            if item.get("safe_drop_candidate"):
                drop_candidates.append(
                    f'DROP INDEX CONCURRENTLY IF EXISTS "{row["schema_name"]}"."{item["index_name"]}";'
                )
                break
    return {
        "schema": schema,
        "duplicate_group_count": len(rows),
        "duplicate_groups": rows,
        "suggested_drop_candidates": drop_candidates,
        "note": "Review before executing. Constraint-backed and primary indexes are intentionally not suggested as drop candidates.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report exact duplicate Postgres indexes and safe drop candidates.")
    parser.add_argument("--schema", default="public")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args(argv)
    print(json.dumps(build_duplicate_index_report(schema=args.schema, limit=args.limit), indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
