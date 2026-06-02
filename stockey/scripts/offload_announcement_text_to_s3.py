from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from typing import Any

from psycopg2 import sql

from data.announcements.state import DOCUMENT_TABLE, REPORT_TABLE
from utils.blob_store import put_text_blob, text_blob_metadata
from utils.db import db_session, qualified_identifier


DOCUMENT_TEXT_FIELDS = {
    "three_page_ocr_text": {
        "key_column": "ocr_s3_key",
        "prefix": "ocr",
        "filename": "ocr_first_3_pages.txt",
    },
    "full_ocr_text": {
        "key_column": "full_ocr_s3_key",
        "prefix": "full_ocr",
        "filename": "ocr_full_document.txt",
    },
    "audio_transcript_text": {
        "key_column": "audio_transcript_s3_key",
        "prefix": "audio_transcript",
        "filename": "audio_transcript.txt",
    },
}

REPORT_TEXT_FIELDS = {
    "report_json": {
        "key_column": "report_s3_key",
        "prefix": "report",
        "filename": None,
    }
}


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _split_table_name(table_name: str) -> tuple[str, str]:
    return tuple(table_name.split(".", 1)) if "." in table_name else ("public", table_name)


def _table_exists(cur, table_name: str) -> bool:
    schema_name, base_name = _split_table_name(table_name)
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = %s AND table_name = %s
        )
        """,
        (schema_name, base_name),
    )
    return bool(cur.fetchone()[0])


def _table_columns(cur, table_name: str) -> set[str]:
    schema_name, base_name = _split_table_name(table_name)
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        """,
        (schema_name, base_name),
    )
    return {row[0] for row in cur.fetchall()}


def ensure_migration_columns(cur) -> None:
    if _table_exists(cur, DOCUMENT_TABLE):
        for prefix, key_column in [
            ("ocr", "ocr_s3_key"),
            ("full_ocr", "full_ocr_s3_key"),
            ("audio_transcript", "audio_transcript_s3_key"),
        ]:
            cur.execute(
                sql.SQL(
                    """
                    ALTER TABLE {}
                    ADD COLUMN IF NOT EXISTS {} TEXT,
                    ADD COLUMN IF NOT EXISTS {} TEXT,
                    ADD COLUMN IF NOT EXISTS {} BIGINT,
                    ADD COLUMN IF NOT EXISTS {} BIGINT,
                    ADD COLUMN IF NOT EXISTS {} TEXT
                    """
                ).format(
                    qualified_identifier(DOCUMENT_TABLE),
                    sql.Identifier(key_column),
                    sql.Identifier(f"{prefix}_sha256"),
                    sql.Identifier(f"{prefix}_chars"),
                    sql.Identifier(f"{prefix}_bytes"),
                    sql.Identifier(f"{prefix}_excerpt"),
                )
            )
    if _table_exists(cur, REPORT_TABLE):
        cur.execute(
            sql.SQL(
                """
                ALTER TABLE {}
                ADD COLUMN IF NOT EXISTS report_s3_key TEXT,
                ADD COLUMN IF NOT EXISTS report_sha256 TEXT,
                ADD COLUMN IF NOT EXISTS report_chars BIGINT,
                ADD COLUMN IF NOT EXISTS report_bytes BIGINT,
                ADD COLUMN IF NOT EXISTS report_excerpt TEXT
                """
            ).format(qualified_identifier(REPORT_TABLE))
        )


def _document_storage_prefix(row: dict[str, Any]) -> str:
    existing = str(row.get("storage_prefix") or "").strip()
    if existing:
        return existing
    published = row.get("published_on") or row.get("exchange_published_on")
    if isinstance(published, str):
        published_date = published[:10].replace("-", "/")
    elif hasattr(published, "strftime"):
        published_date = published.strftime("%Y/%m/%d")
    else:
        published_date = "unknown-date"
    exchange = str(row.get("exchange") or "unknown").lower()
    ticker = str(row.get("ticker") or "unknown")
    unique_id = str(row.get("unique_id") or "unknown")
    return f"announcement-pipeline/{exchange}/{ticker}/{published_date}/{unique_id}"


def _report_key(row: dict[str, Any]) -> str:
    existing = str(row.get("report_s3_key") or "").strip()
    if existing:
        return existing
    published = row.get("published_on")
    if isinstance(published, str):
        published_date = published[:10].replace("-", "/")
    elif hasattr(published, "strftime"):
        published_date = published.strftime("%Y/%m/%d")
    else:
        published_date = "unknown-date"
    exchange = str(row.get("exchange") or "unknown").lower()
    ticker = str(row.get("ticker") or "unknown")
    unique_id = str(row.get("unique_id") or "unknown")
    report_name = str(row.get("report_name") or "report").replace("/", "_")
    return f"announcement-pipeline/{exchange}/{ticker}/{published_date}/{unique_id}/parsed_reports/{report_name}.json"


def _fetch_document_rows(cur, *, limit: int) -> list[dict[str, Any]]:
    columns = _table_columns(cur, DOCUMENT_TABLE)
    candidate_columns = [
        "unique_id",
        "exchange",
        "ticker",
        "published_on",
        "exchange_published_on",
        "storage_prefix",
        *DOCUMENT_TEXT_FIELDS.keys(),
        *[config["key_column"] for config in DOCUMENT_TEXT_FIELDS.values()],
    ]
    select_columns = [column for column in candidate_columns if column in columns]
    if "unique_id" not in select_columns:
        return []
    predicates = [
        sql.SQL("({text_col} IS NOT NULL AND {text_col} <> '' AND ({key_col} IS NULL OR {key_col} = ''))").format(
            text_col=sql.Identifier(text_column),
            key_col=sql.Identifier(config["key_column"]),
        )
        for text_column, config in DOCUMENT_TEXT_FIELDS.items()
        if text_column in columns and config["key_column"] in columns
    ]
    if not predicates:
        return []
    cur.execute(
        sql.SQL("SELECT {} FROM {} WHERE {} ORDER BY published_on NULLS LAST, unique_id LIMIT %s").format(
            sql.SQL(", ").join(sql.Identifier(column) for column in select_columns),
            qualified_identifier(DOCUMENT_TABLE),
            sql.SQL(" OR ").join(predicates),
        ),
        (int(limit),),
    )
    return [dict(zip(select_columns, row)) for row in cur.fetchall()]


def _fetch_report_rows(cur, *, limit: int) -> list[dict[str, Any]]:
    columns = _table_columns(cur, REPORT_TABLE)
    required = {"unique_id", "report_name", "report_json", "report_s3_key"}
    if not required.issubset(columns):
        return []
    select_columns = [
        column
        for column in [
            "unique_id",
            "company_master_id",
            "exchange",
            "ticker",
            "published_on",
            "report_name",
            "report_json",
            "report_s3_key",
        ]
        if column in columns
    ]
    cur.execute(
        sql.SQL(
            """
            SELECT {}
            FROM {}
            WHERE report_json IS NOT NULL
              AND report_json <> ''
              AND (report_s3_key IS NULL OR report_s3_key = '')
            ORDER BY published_on NULLS LAST, unique_id, report_name
            LIMIT %s
            """
        ).format(
            sql.SQL(", ").join(sql.Identifier(column) for column in select_columns),
            qualified_identifier(REPORT_TABLE),
        ),
        (int(limit),),
    )
    return [dict(zip(select_columns, row)) for row in cur.fetchall()]


def offload_documents(*, limit: int, dry_run: bool, null_after_upload: bool) -> dict[str, Any]:
    summary = {"scanned": 0, "uploaded": 0, "nulled_fields": 0, "bytes": 0}
    with db_session() as (conn, cur):
        if not _table_exists(cur, DOCUMENT_TABLE):
            return {**summary, "status": "missing_table"}
        ensure_migration_columns(cur)
        rows = _fetch_document_rows(cur, limit=limit)
        summary["scanned"] = len(rows)
        for row in rows:
            prefix = _document_storage_prefix(row)
            updates: dict[str, Any] = {}
            for text_column, config in DOCUMENT_TEXT_FIELDS.items():
                text = row.get(text_column)
                if not text:
                    continue
                key_column = str(config["key_column"])
                key = row.get(key_column) or f"{prefix}/{config['filename']}"
                metadata = text_blob_metadata(str(text), key=key)
                summary["bytes"] += metadata.byte_count
                if not dry_run:
                    metadata = put_text_blob(str(text), key=key)
                summary["uploaded"] += 1
                updates[key_column] = key
                updates[f"{config['prefix']}_sha256"] = metadata.sha256
                updates[f"{config['prefix']}_chars"] = metadata.char_count
                updates[f"{config['prefix']}_bytes"] = metadata.byte_count
                updates[f"{config['prefix']}_excerpt"] = metadata.excerpt
                if null_after_upload:
                    updates[text_column] = None
                    summary["nulled_fields"] += 1
            if updates and not dry_run:
                set_clause = sql.SQL(", ").join(
                    sql.SQL("{} = %s").format(sql.Identifier(column)) for column in updates
                )
                cur.execute(
                    sql.SQL("UPDATE {} SET {} WHERE unique_id = %s").format(
                        qualified_identifier(DOCUMENT_TABLE),
                        set_clause,
                    ),
                    [*updates.values(), row["unique_id"]],
                )
        if dry_run:
            conn.rollback()
    return summary


def offload_reports(*, limit: int, dry_run: bool, null_after_upload: bool) -> dict[str, Any]:
    summary = {"scanned": 0, "uploaded": 0, "nulled_fields": 0, "bytes": 0}
    with db_session() as (conn, cur):
        if not _table_exists(cur, REPORT_TABLE):
            return {**summary, "status": "missing_table"}
        ensure_migration_columns(cur)
        rows = _fetch_report_rows(cur, limit=limit)
        summary["scanned"] = len(rows)
        for row in rows:
            text = row.get("report_json")
            if not text:
                continue
            key = _report_key(row)
            metadata = text_blob_metadata(str(text), key=key)
            summary["bytes"] += metadata.byte_count
            if not dry_run:
                metadata = put_text_blob(str(text), key=key)
            summary["uploaded"] += 1
            updates: dict[str, Any] = {
                "report_s3_key": key,
                "report_sha256": metadata.sha256,
                "report_chars": metadata.char_count,
                "report_bytes": metadata.byte_count,
                "report_excerpt": metadata.excerpt,
            }
            if null_after_upload:
                updates["report_json"] = None
                summary["nulled_fields"] += 1
            if not dry_run:
                set_clause = sql.SQL(", ").join(
                    sql.SQL("{} = %s").format(sql.Identifier(column)) for column in updates
                )
                cur.execute(
                    sql.SQL("UPDATE {} SET {} WHERE unique_id = %s AND report_name = %s").format(
                        qualified_identifier(REPORT_TABLE),
                        set_clause,
                    ),
                    [*updates.values(), row["unique_id"], row["report_name"]],
                )
        if dry_run:
            conn.rollback()
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offload heavy announcement OCR/transcript/report text from Postgres to S3-compatible storage."
    )
    parser.add_argument("--limit", type=int, default=500, help="Maximum rows to scan per table in one run.")
    parser.add_argument("--dry-run", action="store_true", help="Inspect work without uploading or updating Postgres.")
    parser.add_argument(
        "--keep-inline",
        action="store_true",
        help="Upload and add metadata, but keep the heavy text columns populated.",
    )
    parser.add_argument("--skip-documents", action="store_true", help="Skip announcement_pipeline_documents.")
    parser.add_argument("--skip-reports", action="store_true", help="Skip announcement_pipeline_reports.")
    args = parser.parse_args(argv)

    output: dict[str, Any] = {"dry_run": bool(args.dry_run), "limit": int(args.limit)}
    null_after_upload = not bool(args.keep_inline)
    if not args.skip_documents:
        output["documents"] = offload_documents(
            limit=int(args.limit),
            dry_run=bool(args.dry_run),
            null_after_upload=null_after_upload,
        )
    if not args.skip_reports:
        output["reports"] = offload_reports(
            limit=int(args.limit),
            dry_run=bool(args.dry_run),
            null_after_upload=null_after_upload,
        )
    print(json.dumps(output, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
