from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from typing import Any

from environs import Env

from utils.db import sql_to_df


env = Env()
env.read_env()

DEFAULT_RETENTION_DAYS = env.int("NSE_LEGACY_RETENTION_DAYS", 365)

LEGACY_NSE_TABLES: dict[str, str] = {
    "nseindia_var1": "for_date",
    "nseindia_ohlcv": "date",
    "nseindia_cmvolt": "date",
    "nseindia_catg": "for_month",
    "nseindia_mcap": "date",
    "nseindia_circuit_hit": "date",
    "nseindia_bulk_deals": "date",
    "nseindia_block_deals": "date",
    "nseindia_short_selling": "date",
    "nseindia_cat_turnover": "trade_date",
    "nseindia_events": "date",
    "nseindia_indices": "date",
}


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _table_exists(table_name: str) -> bool:
    df = sql_to_df(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = %s
        LIMIT 1
        """,
        params=(table_name,),
    )
    return not df.empty


def _column_exists(table_name: str, column_name: str) -> bool:
    df = sql_to_df(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
          AND column_name = %s
        LIMIT 1
        """,
        params=(table_name, column_name),
    )
    return not df.empty


def _table_size_rows(table_name: str) -> dict[str, Any]:
    df = sql_to_df(
        """
        SELECT
            pg_total_relation_size(c.oid)::bigint AS total_bytes,
            pg_relation_size(c.oid)::bigint AS table_bytes,
            pg_indexes_size(c.oid)::bigint AS index_bytes,
            pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size,
            pg_size_pretty(pg_relation_size(c.oid)) AS table_size,
            pg_size_pretty(pg_indexes_size(c.oid)) AS index_size,
            COALESCE(s.n_live_tup, 0)::bigint AS estimated_rows
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
        WHERE n.nspname = 'public'
          AND c.relname = %s
        LIMIT 1
        """,
        params=(table_name,),
    )
    return {} if df.empty else df.iloc[0].to_dict()


def _date_bounds(table_name: str, date_column: str) -> dict[str, Any]:
    min_df = sql_to_df(
        f"""
        SELECT {date_column} AS min_date
        FROM {table_name}
        WHERE {date_column} IS NOT NULL
        ORDER BY {date_column} ASC
        LIMIT 1
        """
    )
    max_df = sql_to_df(
        f"""
        SELECT {date_column} AS max_date
        FROM {table_name}
        WHERE {date_column} IS NOT NULL
        ORDER BY {date_column} DESC
        LIMIT 1
        """
    )
    return {
        "min_date": None if min_df.empty else min_df.iloc[0]["min_date"],
        "max_date": None if max_df.empty else max_df.iloc[0]["max_date"],
    }


def retention_row(table_name: str, date_column: str, *, retention_days: int, exact_counts: bool = False) -> dict[str, Any]:
    if not _table_exists(table_name):
        return {"table_name": table_name, "date_column": date_column, "status": "missing_table"}
    if not _column_exists(table_name, date_column):
        return {"table_name": table_name, "date_column": date_column, "status": "missing_date_column"}
    row = _date_bounds(table_name, date_column)
    if exact_counts:
        query = f"""
            SELECT
                COUNT(*)::bigint AS exact_rows,
                COUNT(*) FILTER (WHERE {date_column} < NOW() - (%s * INTERVAL '1 day'))::bigint AS rows_older_than_retention,
                MIN({date_column}) FILTER (WHERE {date_column} < NOW() - (%s * INTERVAL '1 day')) AS oldest_archive_date,
                MAX({date_column}) FILTER (WHERE {date_column} < NOW() - (%s * INTERVAL '1 day')) AS newest_archive_date
            FROM {table_name}
        """
        df = sql_to_df(query, params=(int(retention_days), int(retention_days), int(retention_days)))
        row.update(df.iloc[0].to_dict() if not df.empty else {})
    else:
        row.update(
            {
                "exact_rows": None,
                "rows_older_than_retention": None,
                "oldest_archive_date": row.get("min_date"),
                "newest_archive_date": None,
            }
        )
    size = _table_size_rows(table_name)
    return {
        "table_name": table_name,
        "date_column": date_column,
        "status": "ok",
        "retention_days": int(retention_days),
        "exact_counts": bool(exact_counts),
        **size,
        **row,
    }


def build_retention_report(
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    tables: dict[str, str] | None = None,
    exact_counts: bool = False,
) -> dict[str, Any]:
    rows = [
        retention_row(table, column, retention_days=retention_days, exact_counts=exact_counts)
        for table, column in (tables or LEGACY_NSE_TABLES).items()
    ]
    reclaimable_rows = (
        sum(int(row.get("rows_older_than_retention") or 0) for row in rows)
        if exact_counts
        else None
    )
    return {
        "status": "ok",
        "retention_days": int(retention_days),
        "exact_counts": bool(exact_counts),
        "table_count": len(rows),
        "rows_older_than_retention": reclaimable_rows,
        "rows": sorted(rows, key=lambda row: int(row.get("total_bytes") or 0), reverse=True),
        "note": "DELETE reduces live rows but does not return disk to the OS until table rewrite/VACUUM FULL/pg_repack.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report legacy NSE table retention candidates.")
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS)
    parser.add_argument("--table", action="append", help="Limit to specific table. Can be repeated.")
    parser.add_argument("--exact-counts", action="store_true", help="Run exact COUNT/MIN/MAX scans. Slow on large tables.")
    args = parser.parse_args(argv)
    selected = LEGACY_NSE_TABLES
    if args.table:
        selected = {table: LEGACY_NSE_TABLES[table] for table in args.table if table in LEGACY_NSE_TABLES}
    print(
        json.dumps(
            build_retention_report(
                retention_days=args.retention_days,
                tables=selected,
                exact_counts=bool(args.exact_counts),
            ),
            indent=2,
            default=_json_default,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
