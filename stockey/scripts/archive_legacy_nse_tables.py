from __future__ import annotations

import argparse
import gzip
import json
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import psycopg2
from psycopg2 import extensions
from environs import Env
from psycopg2 import sql

from scripts.db_table_retention_report import DEFAULT_RETENTION_DAYS, LEGACY_NSE_TABLES
from advisory.fallback_telemetry import record_local_fallback_event
from utils.db import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER, qualified_identifier, sql_to_df
from utils.store import save_file


env = Env()
env.read_env()

DEFAULT_ARCHIVE_PREFIX = env.str("NSE_LEGACY_ARCHIVE_PREFIX", "archives/nseindia")


def _log_progress(message: str, **fields: Any) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    suffix = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
    line = f"[archive_legacy_nse_tables] ts={ts} {message}"
    if suffix:
        line = f"{line} {suffix}"
    print(line, file=sys.stderr, flush=True)


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _parse_cutoff(cutoff: str | None, retention_days: int) -> Any:
    if cutoff:
        ts = pd.to_datetime(cutoff, utc=True, errors="coerce")
    else:
        ts = pd.Timestamp.utcnow() - pd.Timedelta(days=int(retention_days))
    if pd.isna(ts):
        raise ValueError(f"Invalid cutoff date: {cutoff}")
    return ts.normalize()

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


def _date_bounds(table_name: str, date_column: str, cutoff) -> tuple[Any | None, Any | None]:
    min_df = sql_to_df(
        f"""
        SELECT {date_column} AS min_date
        FROM {table_name}
        WHERE {date_column} IS NOT NULL
          AND {date_column} < %s
        ORDER BY {date_column} ASC
        LIMIT 1
        """,
        params=(cutoff,),
    )
    max_df = sql_to_df(
        f"""
        SELECT {date_column} AS max_date
        FROM {table_name}
        WHERE {date_column} IS NOT NULL
          AND {date_column} < %s
        ORDER BY {date_column} DESC
        LIMIT 1
        """,
        params=(cutoff,),
    )
    min_date = None if min_df.empty else min_df.iloc[0]["min_date"]
    max_date = None if max_df.empty else max_df.iloc[0]["max_date"]
    return min_date, max_date


def _archive_chunks(table_name: str, date_column: str, cutoff, *, exact_counts: bool = False) -> list[dict[str, Any]]:
    min_date, max_date = _date_bounds(table_name, date_column, cutoff)
    if min_date is None or max_date is None:
        return []
    min_ts = pd.to_datetime(min_date, utc=True)
    current = pd.Timestamp(year=min_ts.year, month=min_ts.month, day=1, tz="UTC")
    max_ts = pd.to_datetime(max_date, utc=True)
    final = min(
        pd.Timestamp(year=max_ts.year, month=max_ts.month, day=1, tz="UTC") + pd.DateOffset(months=1),
        pd.to_datetime(cutoff, utc=True),
    )
    chunks: list[dict[str, Any]] = []
    while current < final:
        chunk_end = min(current + pd.DateOffset(months=1), pd.to_datetime(cutoff, utc=True))
        row: dict[str, Any] = {
            "chunk_month": current,
            "chunk_end": chunk_end,
            "min_date": current,
            "max_date": chunk_end,
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
                params=(current, chunk_end),
            )
            row["row_count"] = None if count_df.empty else int(count_df.iloc[0]["row_count"] or 0)
        chunks.append(row)
        current = chunk_end
    return chunks


def _copy_chunk_to_gzip(
    *,
    cur,
    table_name: str,
    date_column: str,
    chunk_start,
    chunk_end,
    tmp_dir: Path,
) -> Path:
    safe_start = str(chunk_start)[:10]
    path = tmp_dir / f"{table_name}_{safe_start}.csv.gz"
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
        table=qualified_identifier(table_name),
        date_column=sql.Identifier(date_column),
        chunk_start=sql.Literal(chunk_start),
        chunk_end=sql.Literal(chunk_end),
    )
    with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
        cur.copy_expert(query, handle)
    return path


def _connect_autocommit():
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )
    conn.autocommit = False
    conn.set_isolation_level(extensions.ISOLATION_LEVEL_REPEATABLE_READ)
    return conn


def archive_table(
    *,
    table_name: str,
    date_column: str,
    cutoff,
    archive_s3: bool,
    delete: bool,
    execute: bool,
    allow_delete_without_archive: bool,
    archive_prefix: str,
    max_chunks: int | None,
    exact_counts: bool,
) -> dict[str, Any]:
    table_started = time.monotonic()
    _log_progress("table_start", table=table_name, cutoff=cutoff.isoformat(), execute=execute, archive_s3=archive_s3, delete=delete)
    if not _table_exists(table_name):
        _log_progress("table_missing", table=table_name)
        return {"table_name": table_name, "date_column": date_column, "status": "missing_table"}
    chunks = _archive_chunks(table_name, date_column, cutoff, exact_counts=exact_counts)
    if max_chunks is not None:
        chunks = chunks[: max(int(max_chunks), 0)]
    _log_progress("table_chunks_ready", table=table_name, chunks=len(chunks), exact_counts=exact_counts)
    summary: dict[str, Any] = {
        "table_name": table_name,
        "date_column": date_column,
        "cutoff": cutoff.isoformat(),
        "archive_s3": bool(archive_s3),
        "delete": bool(delete),
        "execute": bool(execute),
        "chunk_count": len(chunks),
        "candidate_rows": sum(int(chunk.get("row_count") or 0) for chunk in chunks) if exact_counts else None,
        "exact_counts": bool(exact_counts),
        "chunks": [],
    }
    if delete and not archive_s3 and not allow_delete_without_archive:
        summary["status"] = "blocked_delete_without_archive"
        summary["message"] = "Pass --allow-delete-without-archive to delete without S3 archive."
        _log_progress("table_blocked_delete_without_archive", table=table_name)
        return summary
    if not execute:
        summary["status"] = "dry_run"
        summary["chunks"] = chunks
        _log_progress("table_dry_run_done", table=table_name, chunks=len(chunks), elapsed_seconds=round(time.monotonic() - table_started, 2))
        return summary

    conn = _connect_autocommit()
    tmp_root = Path(tempfile.mkdtemp(prefix="stockey_nse_archive_"))
    try:
        cur = conn.cursor()
        for idx, chunk in enumerate(chunks, start=1):
            chunk_started = time.monotonic()
            chunk_start = chunk["chunk_month"]
            chunk_end = chunk["chunk_end"]
            chunk_summary = dict(chunk)
            s3_key = None
            _log_progress(
                "chunk_start",
                table=table_name,
                chunk=f"{idx}/{len(chunks)}",
                start=str(chunk_start)[:10],
                end=str(chunk_end)[:10],
            )
            try:
                if archive_s3:
                    _log_progress("chunk_archive_start", table=table_name, chunk=f"{idx}/{len(chunks)}", start=str(chunk_start)[:10])
                    archive_path = _copy_chunk_to_gzip(
                        cur=cur,
                        table_name=table_name,
                        date_column=date_column,
                        chunk_start=chunk_start,
                        chunk_end=chunk_end,
                        tmp_dir=tmp_root,
                    )
                    s3_key = f"{archive_prefix.rstrip('/')}/{table_name}/{archive_path.name}"
                    archive_size = archive_path.stat().st_size if archive_path.exists() else None
                    _log_progress(
                        "chunk_upload_start",
                        table=table_name,
                        chunk=f"{idx}/{len(chunks)}",
                        key=s3_key,
                        bytes=archive_size,
                    )
                    save_file(archive_path, key=s3_key)
                    archive_path.unlink(missing_ok=True)
                    _log_progress("chunk_upload_done", table=table_name, chunk=f"{idx}/{len(chunks)}", key=s3_key)
                deleted_rows = 0
                if delete:
                    _log_progress("chunk_delete_start", table=table_name, chunk=f"{idx}/{len(chunks)}", start=str(chunk_start)[:10])
                    cur.execute(
                        sql.SQL("DELETE FROM {table} WHERE {date_column} >= %s AND {date_column} < %s").format(
                            table=qualified_identifier(table_name),
                            date_column=sql.Identifier(date_column),
                        ),
                        (chunk_start, chunk_end),
                    )
                    deleted_rows = int(cur.rowcount or 0)
                    _log_progress("chunk_delete_done", table=table_name, chunk=f"{idx}/{len(chunks)}", deleted_rows=deleted_rows)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            chunk_summary.update(
                {
                    "chunk_end": chunk_end.isoformat() if hasattr(chunk_end, "isoformat") else str(chunk_end),
                    "s3_key": s3_key,
                    "deleted_rows": deleted_rows,
                    "status": "ok",
                }
            )
            summary["chunks"].append(chunk_summary)
            _log_progress(
                "chunk_done",
                table=table_name,
                chunk=f"{idx}/{len(chunks)}",
                deleted_rows=deleted_rows,
                elapsed_seconds=round(time.monotonic() - chunk_started, 2),
            )
        cur.close()
    finally:
        conn.close()
        try:
            tmp_root.rmdir()
        except OSError as exc:
            record_local_fallback_event(
                module="scripts.archive_legacy_nse_tables",
                fallback_type="legacy_nse_archive_tempdir_cleanup_failed",
                source="archive_table",
                severity="warn",
                reason="Legacy NSE archive cleanup could not remove the temporary directory after archive processing.",
                error=exc,
                metadata={"tmp_root": str(tmp_root), "table": table_name},
            )
    summary["status"] = "ok"
    summary["deleted_rows"] = sum(int(chunk.get("deleted_rows") or 0) for chunk in summary["chunks"])
    summary["archived_chunks"] = sum(1 for chunk in summary["chunks"] if chunk.get("s3_key"))
    summary["note"] = "Run VACUUM FULL or pg_repack later if you need disk returned to the OS."
    _log_progress(
        "table_done",
        table=table_name,
        chunks=len(summary["chunks"]),
        archived_chunks=summary["archived_chunks"],
        deleted_rows=summary["deleted_rows"],
        elapsed_seconds=round(time.monotonic() - table_started, 2),
    )
    return summary


def run_archive(
    *,
    tables: list[str],
    retention_days: int,
    cutoff: str | None,
    archive_s3: bool,
    delete: bool,
    execute: bool,
    allow_delete_without_archive: bool,
    archive_prefix: str,
    max_chunks: int | None,
    exact_counts: bool,
) -> dict[str, Any]:
    parsed_cutoff = _parse_cutoff(cutoff, retention_days)
    selected = tables or list(LEGACY_NSE_TABLES.keys())
    results = []
    _log_progress("run_start", tables=",".join(selected), cutoff=parsed_cutoff.isoformat(), execute=execute, archive_s3=archive_s3, delete=delete)
    for table in selected:
        if table not in LEGACY_NSE_TABLES:
            _log_progress("table_unknown", table=table)
            results.append({"table_name": table, "status": "unknown_table"})
            continue
        results.append(
            archive_table(
                table_name=table,
                date_column=LEGACY_NSE_TABLES[table],
                cutoff=parsed_cutoff,
                archive_s3=archive_s3,
                delete=delete,
                execute=execute,
                allow_delete_without_archive=allow_delete_without_archive,
                archive_prefix=archive_prefix,
                max_chunks=max_chunks,
                exact_counts=exact_counts,
            )
        )
    result = {
        "status": "ok",
        "execute": bool(execute),
        "cutoff": parsed_cutoff.isoformat(),
        "archive_s3": bool(archive_s3),
        "delete": bool(delete),
        "table_count": len(results),
        "candidate_rows": sum(int(row.get("candidate_rows") or 0) for row in results if row.get("candidate_rows") is not None) if exact_counts else None,
        "exact_counts": bool(exact_counts),
        "deleted_rows": sum(int(row.get("deleted_rows") or 0) for row in results),
        "results": results,
    }
    _log_progress("run_done", tables=len(results), deleted_rows=result["deleted_rows"])
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dry-run, archive, and optionally delete old rows from legacy NSE tables."
    )
    parser.add_argument("--table", action="append", default=[], help="Table to process. Can be repeated.")
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS)
    parser.add_argument("--cutoff-date", default=None, help="Override cutoff date, e.g. 2025-06-01.")
    parser.add_argument("--archive-s3", action="store_true", help="Archive monthly CSV.GZ chunks to S3-compatible storage.")
    parser.add_argument("--delete", action="store_true", help="Delete archived/candidate rows.")
    parser.add_argument("--allow-delete-without-archive", action="store_true")
    parser.add_argument("--archive-prefix", default=DEFAULT_ARCHIVE_PREFIX)
    parser.add_argument("--max-chunks", type=int, default=None, help="Limit monthly chunks per table.")
    parser.add_argument("--exact-counts", action="store_true", help="Count each monthly chunk. Slow on large tables.")
    parser.add_argument("--execute", action="store_true", help="Actually archive/delete. Without this, dry-run only.")
    args = parser.parse_args(argv)
    result = run_archive(
        tables=args.table,
        retention_days=int(args.retention_days),
        cutoff=args.cutoff_date,
        archive_s3=bool(args.archive_s3),
        delete=bool(args.delete),
        execute=bool(args.execute),
        allow_delete_without_archive=bool(args.allow_delete_without_archive),
        archive_prefix=args.archive_prefix,
        max_chunks=args.max_chunks,
        exact_counts=bool(args.exact_counts),
    )
    print(json.dumps(result, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
