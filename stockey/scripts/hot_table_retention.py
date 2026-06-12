from __future__ import annotations

import argparse
import gzip
import json
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from environs import Env
from psycopg2 import sql

from utils.db import db_session, execute_db_operation, qualified_identifier, sql_to_df


env = Env()
env.read_env()

DEFAULT_TRACE_RETENTION_DAYS = env.int("TRACE_RETENTION_DAYS", 90)
DEFAULT_INTRADAY_RETENTION_DAYS = env.int("INTRADAY_RETENTION_DAYS", 45)
DEFAULT_ARCHIVE_PREFIX = env.str("HOT_TABLE_ARCHIVE_PREFIX", "archives/hot_tables")


@dataclass(frozen=True)
class RetentionTable:
    table_name: str
    date_column: str
    group: str
    default_retention_days: int
    description: str


RETENTION_TABLES: dict[str, RetentionTable] = {
    "dhan_ohlcv_intraday": RetentionTable(
        table_name="dhan_ohlcv_intraday",
        date_column="timestamp",
        group="intraday",
        default_retention_days=DEFAULT_INTRADAY_RETENTION_DAYS,
        description="Raw Dhan intraday candles used by watchers, execution price checks, and intraday feature rebuilds.",
    ),
    "advisory_intraday_features_daily": RetentionTable(
        table_name="advisory_intraday_features_daily",
        date_column="asof_date",
        group="intraday",
        default_retention_days=DEFAULT_INTRADAY_RETENTION_DAYS,
        description="Daily derived intraday participation features.",
    ),
    "advisory_decision_traces": RetentionTable(
        table_name="advisory_decision_traces",
        date_column="updated_at",
        group="trace",
        default_retention_days=DEFAULT_TRACE_RETENTION_DAYS,
        description="Raw decision trace headers.",
    ),
    "advisory_decision_trace_steps": RetentionTable(
        table_name="advisory_decision_trace_steps",
        date_column="load_ts",
        group="trace",
        default_retention_days=DEFAULT_TRACE_RETENTION_DAYS,
        description="Raw decision trace stage details.",
    ),
    "advisory_event_processing_runs": RetentionTable(
        table_name="advisory_event_processing_runs",
        date_column="started_at",
        group="trace",
        default_retention_days=DEFAULT_TRACE_RETENTION_DAYS,
        description="Event-processing trace rows and errors.",
    ),
    "advisory_action_conflicts": RetentionTable(
        table_name="advisory_action_conflicts",
        date_column="load_ts",
        group="trace",
        default_retention_days=DEFAULT_TRACE_RETENTION_DAYS,
        description="Action conflict audit rows.",
    ),
    "advisory_trace_summaries": RetentionTable(
        table_name="advisory_trace_summaries",
        date_column="generated_at",
        group="trace",
        default_retention_days=DEFAULT_TRACE_RETENTION_DAYS,
        description="Materialized compact trace summary cache rows.",
    ),
}


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _log(message: str, **fields: Any) -> None:
    suffix = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
    line = f"[hot_table_retention] ts={datetime.now(timezone.utc).isoformat(timespec='seconds')} {message}"
    if suffix:
        line = f"{line} {suffix}"
    print(line, file=sys.stderr, flush=True)


def parse_cutoff(cutoff: str | None, retention_days: int) -> pd.Timestamp:
    if cutoff:
        parsed = pd.to_datetime(cutoff, utc=True, errors="coerce")
    else:
        parsed = pd.Timestamp.utcnow() - pd.Timedelta(days=int(retention_days))
    if pd.isna(parsed):
        raise ValueError(f"Invalid cutoff date: {cutoff}")
    return parsed.normalize()


def table_exists(table_name: str) -> bool:
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


def column_exists(table_name: str, column_name: str) -> bool:
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


def table_size(table_name: str) -> dict[str, Any]:
    df = sql_to_df(
        """
        SELECT
            pg_total_relation_size(c.oid)::bigint AS total_bytes,
            pg_relation_size(c.oid)::bigint AS table_bytes,
            pg_indexes_size(c.oid)::bigint AS index_bytes,
            pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size,
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


def date_bounds(table_name: str, date_column: str, cutoff: pd.Timestamp | None = None) -> dict[str, Any]:
    params: tuple[Any, ...] | None = None
    cutoff_clause = ""
    if cutoff is not None:
        cutoff_clause = f"AND {date_column} < %s"
        params = (cutoff.to_pydatetime(),)
    min_df = sql_to_df(
        f"""
        SELECT {date_column} AS min_date
        FROM {table_name}
        WHERE {date_column} IS NOT NULL
        {cutoff_clause}
        ORDER BY {date_column} ASC
        LIMIT 1
        """,
        params=params,
    )
    max_df = sql_to_df(
        f"""
        SELECT {date_column} AS max_date
        FROM {table_name}
        WHERE {date_column} IS NOT NULL
        {cutoff_clause}
        ORDER BY {date_column} DESC
        LIMIT 1
        """,
        params=params,
    )
    return {
        "min_date": None if min_df.empty else min_df.iloc[0]["min_date"],
        "max_date": None if max_df.empty else max_df.iloc[0]["max_date"],
    }


def exact_candidate_count(table_name: str, date_column: str, cutoff: pd.Timestamp) -> int:
    df = sql_to_df(
        f"""
        SELECT COUNT(*)::bigint AS row_count
        FROM {table_name}
        WHERE {date_column} IS NOT NULL
          AND {date_column} < %s
        """,
        params=(cutoff.to_pydatetime(),),
    )
    return 0 if df.empty else int(df.iloc[0].get("row_count") or 0)


def month_chunks(
    table_name: str,
    date_column: str,
    cutoff: pd.Timestamp,
    *,
    exact_counts: bool,
) -> list[dict[str, Any]]:
    bounds = date_bounds(table_name, date_column, cutoff=cutoff)
    min_date = bounds.get("min_date")
    max_date = bounds.get("max_date")
    if min_date is None or max_date is None:
        return []
    min_ts = pd.to_datetime(min_date, utc=True)
    max_ts = pd.to_datetime(max_date, utc=True)
    current = pd.Timestamp(year=min_ts.year, month=min_ts.month, day=1, tz="UTC")
    final = min(
        pd.Timestamp(year=max_ts.year, month=max_ts.month, day=1, tz="UTC") + pd.DateOffset(months=1),
        cutoff,
    )
    chunks: list[dict[str, Any]] = []
    while current < final:
        chunk_end = min(current + pd.DateOffset(months=1), cutoff)
        row: dict[str, Any] = {
            "chunk_start": current,
            "chunk_end": chunk_end,
            "row_count": None,
        }
        if exact_counts:
            count_df = sql_to_df(
                f"""
                SELECT COUNT(*)::bigint AS row_count
                FROM {table_name}
                WHERE {date_column} >= %s
                  AND {date_column} < %s
                """,
                params=(current.to_pydatetime(), chunk_end.to_pydatetime()),
            )
            row["row_count"] = 0 if count_df.empty else int(count_df.iloc[0].get("row_count") or 0)
        chunks.append(row)
        current = chunk_end
    return chunks


def retention_report_row(spec: RetentionTable, *, retention_days: int | None = None, exact_counts: bool = False) -> dict[str, Any]:
    days = int(retention_days or spec.default_retention_days)
    cutoff = parse_cutoff(None, days)
    row: dict[str, Any] = {
        "table_name": spec.table_name,
        "date_column": spec.date_column,
        "group": spec.group,
        "description": spec.description,
        "retention_days": days,
        "cutoff": cutoff,
        "exact_counts": bool(exact_counts),
    }
    if not table_exists(spec.table_name):
        return {**row, "status": "missing_table"}
    if not column_exists(spec.table_name, spec.date_column):
        return {**row, "status": "missing_date_column"}
    row.update(table_size(spec.table_name))
    row.update(date_bounds(spec.table_name, spec.date_column))
    candidate_bounds = date_bounds(spec.table_name, spec.date_column, cutoff=cutoff)
    row["oldest_candidate_date"] = candidate_bounds.get("min_date")
    row["newest_candidate_date"] = candidate_bounds.get("max_date")
    row["candidate_rows"] = exact_candidate_count(spec.table_name, spec.date_column, cutoff) if exact_counts else None
    row["status"] = "ok"
    return row


def build_retention_report(
    *,
    table_names: list[str] | None = None,
    group: str | None = None,
    trace_retention_days: int = DEFAULT_TRACE_RETENTION_DAYS,
    intraday_retention_days: int = DEFAULT_INTRADAY_RETENTION_DAYS,
    exact_counts: bool = False,
) -> dict[str, Any]:
    specs = selected_specs(table_names=table_names, group=group)
    rows = []
    for spec in specs:
        days = trace_retention_days if spec.group == "trace" else intraday_retention_days
        rows.append(retention_report_row(spec, retention_days=days, exact_counts=exact_counts))
    candidate_rows = sum(int(row.get("candidate_rows") or 0) for row in rows) if exact_counts else None
    return {
        "status": "ok",
        "table_count": len(rows),
        "trace_retention_days": int(trace_retention_days),
        "intraday_retention_days": int(intraday_retention_days),
        "exact_counts": bool(exact_counts),
        "candidate_rows": candidate_rows,
        "rows": sorted(rows, key=lambda row: int(row.get("total_bytes") or 0), reverse=True),
        "note": "Use --execute with --archive-s3 before --delete for hot/cold movement. Deletes do not return disk to the OS until table rewrite/VACUUM FULL/pg_repack.",
    }


def selected_specs(table_names: list[str] | None = None, group: str | None = None) -> list[RetentionTable]:
    specs = list(RETENTION_TABLES.values())
    if group:
        wanted_group = str(group).strip().lower()
        specs = [spec for spec in specs if spec.group == wanted_group]
    if table_names:
        selected: list[RetentionTable] = []
        for name in table_names:
            if name not in RETENTION_TABLES:
                raise ValueError(f"Unknown retention table: {name}")
            selected.append(RETENTION_TABLES[name])
        if group:
            selected = [spec for spec in selected if spec.group == str(group).strip().lower()]
        specs = selected
    return specs


def copy_chunk_to_gzip(
    *,
    cur,
    spec: RetentionTable,
    chunk_start: pd.Timestamp,
    chunk_end: pd.Timestamp,
    tmp_dir: Path,
) -> Path:
    safe_start = str(chunk_start.date())
    path = tmp_dir / f"{spec.table_name}_{safe_start}.csv.gz"
    query = sql.SQL(
        """
        COPY (
            SELECT *
            FROM {table}
            WHERE {date_column} >= {chunk_start}
              AND {date_column} < {chunk_end}
            ORDER BY {date_column}
        ) TO STDOUT WITH (FORMAT CSV, HEADER TRUE)
        """
    ).format(
        table=qualified_identifier(spec.table_name),
        date_column=sql.Identifier(spec.date_column),
        chunk_start=sql.Literal(chunk_start.to_pydatetime()),
        chunk_end=sql.Literal(chunk_end.to_pydatetime()),
    )
    with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
        cur.copy_expert(query, handle)
    return path


def delete_chunk(*, cur, spec: RetentionTable, chunk_start: pd.Timestamp, chunk_end: pd.Timestamp) -> int:
    query = sql.SQL(
        """
        WITH deleted AS (
            DELETE FROM {table}
            WHERE {date_column} >= {chunk_start}
              AND {date_column} < {chunk_end}
            RETURNING 1
        )
        SELECT COUNT(*)::bigint AS deleted_rows FROM deleted
        """
    ).format(
        table=qualified_identifier(spec.table_name),
        date_column=sql.Identifier(spec.date_column),
        chunk_start=sql.Literal(chunk_start.to_pydatetime()),
        chunk_end=sql.Literal(chunk_end.to_pydatetime()),
    )
    cur.execute(query)
    result = cur.fetchone()
    return int(result[0] if result else 0)


def archive_or_delete_table(
    spec: RetentionTable,
    *,
    cutoff: pd.Timestamp,
    archive_s3: bool,
    delete: bool,
    execute: bool,
    allow_delete_without_archive: bool,
    archive_prefix: str,
    max_chunks: int | None,
    exact_counts: bool,
) -> dict[str, Any]:
    started = time.monotonic()
    if not table_exists(spec.table_name):
        return {"table_name": spec.table_name, "status": "missing_table"}
    if not column_exists(spec.table_name, spec.date_column):
        return {"table_name": spec.table_name, "date_column": spec.date_column, "status": "missing_date_column"}
    chunks = month_chunks(spec.table_name, spec.date_column, cutoff, exact_counts=exact_counts)
    if max_chunks is not None:
        chunks = chunks[: max(int(max_chunks), 0)]
    summary: dict[str, Any] = {
        "table_name": spec.table_name,
        "date_column": spec.date_column,
        "group": spec.group,
        "cutoff": cutoff,
        "archive_s3": bool(archive_s3),
        "delete": bool(delete),
        "execute": bool(execute),
        "chunk_count": len(chunks),
        "candidate_rows": sum(int(chunk.get("row_count") or 0) for chunk in chunks) if exact_counts else None,
        "exact_counts": bool(exact_counts),
        "chunks": [],
    }
    if delete and not archive_s3 and not allow_delete_without_archive:
        return {
            **summary,
            "status": "blocked_delete_without_archive",
            "message": "Pass --archive-s3 or --allow-delete-without-archive before deleting hot-table rows.",
        }
    if not execute:
        return {**summary, "status": "dry_run", "chunks": chunks}

    tmp_root = Path(tempfile.mkdtemp(prefix="stockey_hot_retention_"))

    def _archive_or_delete_chunks() -> list[dict[str, Any]]:
        chunk_summaries: list[dict[str, Any]] = []
        with db_session() as (_, cur):
            for idx, chunk in enumerate(chunks, start=1):
                chunk_start = pd.to_datetime(chunk["chunk_start"], utc=True)
                chunk_end = pd.to_datetime(chunk["chunk_end"], utc=True)
                chunk_summary = dict(chunk)
                _log("chunk_start", table=spec.table_name, chunk=f"{idx}/{len(chunks)}", start=str(chunk_start.date()), end=str(chunk_end.date()))
                if archive_s3:
                    from utils.store import save_file

                    archive_path = copy_chunk_to_gzip(cur=cur, spec=spec, chunk_start=chunk_start, chunk_end=chunk_end, tmp_dir=tmp_root)
                    s3_key = f"{archive_prefix.rstrip('/')}/{spec.group}/{spec.table_name}/{archive_path.name}"
                    save_file(archive_path, key=s3_key)
                    chunk_summary["archive_s3_key"] = s3_key
                    chunk_summary["archive_bytes"] = archive_path.stat().st_size
                if delete:
                    chunk_summary["deleted_rows"] = delete_chunk(cur=cur, spec=spec, chunk_start=chunk_start, chunk_end=chunk_end)
                chunk_summaries.append(chunk_summary)
        return chunk_summaries

    summary["chunks"] = execute_db_operation(
        _archive_or_delete_chunks,
        operation_name=f"hot_table_retention:archive_or_delete:{spec.table_name}",
    )
    summary["status"] = "ok"
    summary["elapsed_seconds"] = round(time.monotonic() - started, 2)
    summary["deleted_rows"] = sum(int(chunk.get("deleted_rows") or 0) for chunk in summary["chunks"])
    return summary


def run_archive_or_delete(
    *,
    table_names: list[str] | None,
    group: str | None,
    trace_retention_days: int,
    intraday_retention_days: int,
    cutoff: str | None,
    archive_s3: bool,
    delete: bool,
    execute: bool,
    allow_delete_without_archive: bool,
    archive_prefix: str,
    max_chunks: int | None,
    exact_counts: bool,
) -> dict[str, Any]:
    rows = []
    for spec in selected_specs(table_names=table_names, group=group):
        days = trace_retention_days if spec.group == "trace" else intraday_retention_days
        table_cutoff = parse_cutoff(cutoff, days)
        rows.append(
            archive_or_delete_table(
                spec,
                cutoff=table_cutoff,
                archive_s3=archive_s3,
                delete=delete,
                execute=execute,
                allow_delete_without_archive=allow_delete_without_archive,
                archive_prefix=archive_prefix,
                max_chunks=max_chunks,
                exact_counts=exact_counts,
            )
        )
    status = "ok"
    if any(row.get("status") not in {"ok", "dry_run", "missing_table"} for row in rows):
        status = "blocked"
    return {
        "status": status,
        "table_count": len(rows),
        "execute": bool(execute),
        "archive_s3": bool(archive_s3),
        "delete": bool(delete),
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report/archive/delete hot trace and intraday tables.")
    parser.add_argument("--table", action="append", choices=sorted(RETENTION_TABLES), help="Limit to a specific table. Can be repeated.")
    parser.add_argument("--group", choices=["trace", "intraday"], help="Limit to trace or intraday tables.")
    parser.add_argument("--trace-retention-days", type=int, default=DEFAULT_TRACE_RETENTION_DAYS)
    parser.add_argument("--intraday-retention-days", type=int, default=DEFAULT_INTRADAY_RETENTION_DAYS)
    parser.add_argument("--cutoff", help="Explicit cutoff date/timestamp. Overrides retention-day derived cutoff.")
    parser.add_argument("--exact-counts", action="store_true", help="Run exact COUNT scans. Can be slow on large tables.")
    parser.add_argument("--archive-s3", action="store_true", help="Archive candidate chunks to S3/object storage before optional delete.")
    parser.add_argument("--delete", action="store_true", help="Delete candidate chunks. Requires --archive-s3 unless --allow-delete-without-archive is set.")
    parser.add_argument("--allow-delete-without-archive", action="store_true", help="Explicitly allow deleting without S3 archive.")
    parser.add_argument("--archive-prefix", default=DEFAULT_ARCHIVE_PREFIX)
    parser.add_argument("--max-chunks", type=int, default=None)
    parser.add_argument("--execute", action="store_true", help="Actually archive/delete. Without this, only reports planned chunks.")
    parser.add_argument("--format", choices=["json", "text"], default="json")
    args = parser.parse_args(argv)

    if not args.archive_s3 and not args.delete:
        result = build_retention_report(
            table_names=args.table,
            group=args.group,
            trace_retention_days=args.trace_retention_days,
            intraday_retention_days=args.intraday_retention_days,
            exact_counts=bool(args.exact_counts),
        )
    else:
        result = run_archive_or_delete(
            table_names=args.table,
            group=args.group,
            trace_retention_days=args.trace_retention_days,
            intraday_retention_days=args.intraday_retention_days,
            cutoff=args.cutoff,
            archive_s3=bool(args.archive_s3),
            delete=bool(args.delete),
            execute=bool(args.execute),
            allow_delete_without_archive=bool(args.allow_delete_without_archive),
            archive_prefix=args.archive_prefix,
            max_chunks=args.max_chunks,
            exact_counts=bool(args.exact_counts),
        )
    if args.format == "json":
        print(json.dumps(result, indent=2, sort_keys=True, default=_json_default))
    else:
        print(f"status={result.get('status')} tables={result.get('table_count')} exact_counts={result.get('exact_counts', False)}")
        for row in result.get("rows", []):
            print(
                " ".join(
                    [
                        f"table={row.get('table_name')}",
                        f"status={row.get('status')}",
                        f"group={row.get('group')}",
                        f"candidate_rows={row.get('candidate_rows')}",
                        f"oldest_candidate={row.get('oldest_candidate_date')}",
                        f"newest_candidate={row.get('newest_candidate_date')}",
                    ]
                )
            )
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
