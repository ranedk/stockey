from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from environs import Env
from psycopg2 import sql

from advisory.fallback_telemetry import record_local_fallback_event
from data.announcements.state import DOCUMENT_TABLE, REPORT_TABLE
from utils.db import db_session, execute_db_operation, qualified_identifier


env = Env()
env.read_env()

AWS_BUCKET_NAME = env.str("AWS_BUCKET_NAME", "stockeydata")

DOCUMENT_POINTER_FIELDS = [
    {
        "text_column": "three_page_ocr_text",
        "key_column": "ocr_s3_key",
        "sha_column": "ocr_sha256",
        "bytes_column": "ocr_bytes",
        "kind": "ocr",
    },
    {
        "text_column": "full_ocr_text",
        "key_column": "full_ocr_s3_key",
        "sha_column": "full_ocr_sha256",
        "bytes_column": "full_ocr_bytes",
        "kind": "full_ocr",
    },
    {
        "text_column": "audio_transcript_text",
        "key_column": "audio_transcript_s3_key",
        "sha_column": "audio_transcript_sha256",
        "bytes_column": "audio_transcript_bytes",
        "kind": "audio_transcript",
    },
]

REPORT_POINTER_FIELDS = [
    {
        "text_column": "report_json",
        "key_column": "report_s3_key",
        "sha_column": "report_sha256",
        "bytes_column": "report_bytes",
        "kind": "report",
    }
]


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _split_table_name(table_name: str) -> tuple[str, str]:
    return tuple(table_name.split(".", 1)) if "." in table_name else ("public", table_name)


def table_exists(cur, table_name: str) -> bool:
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


def table_columns(cur, table_name: str) -> set[str]:
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


def pointer_select_columns(columns: set[str], pointer_fields: list[dict[str, str]], base_columns: list[str]) -> list[str]:
    selected = [column for column in base_columns if column in columns]
    for field in pointer_fields:
        for column in [field["key_column"], field["sha_column"], field["bytes_column"], field["text_column"]]:
            if column in columns and column not in selected:
                selected.append(column)
    return selected


def pointer_predicates(columns: set[str], pointer_fields: list[dict[str, str]]) -> list[Any]:
    return [
        sql.SQL("({key_col} IS NOT NULL AND {key_col} <> '')").format(key_col=sql.Identifier(field["key_column"]))
        for field in pointer_fields
        if field["key_column"] in columns
    ]


def fetch_document_rows(cur, *, limit: int) -> list[dict[str, Any]]:
    if not table_exists(cur, DOCUMENT_TABLE):
        return []
    columns = table_columns(cur, DOCUMENT_TABLE)
    predicates = pointer_predicates(columns, DOCUMENT_POINTER_FIELDS)
    if not predicates:
        return []
    select_columns = pointer_select_columns(
        columns,
        DOCUMENT_POINTER_FIELDS,
        ["unique_id", "exchange", "ticker", "published_on"],
    )
    cur.execute(
        sql.SQL("SELECT {} FROM {} WHERE {} ORDER BY published_on NULLS LAST, unique_id LIMIT %s").format(
            sql.SQL(", ").join(sql.Identifier(column) for column in select_columns),
            qualified_identifier(DOCUMENT_TABLE),
            sql.SQL(" OR ").join(predicates),
        ),
        (int(limit),),
    )
    return [dict(zip(select_columns, row)) for row in cur.fetchall()]


def fetch_report_rows(cur, *, limit: int) -> list[dict[str, Any]]:
    if not table_exists(cur, REPORT_TABLE):
        return []
    columns = table_columns(cur, REPORT_TABLE)
    predicates = pointer_predicates(columns, REPORT_POINTER_FIELDS)
    if not predicates:
        return []
    select_columns = pointer_select_columns(
        columns,
        REPORT_POINTER_FIELDS,
        ["unique_id", "company_master_id", "exchange", "ticker", "published_on", "report_name"],
    )
    cur.execute(
        sql.SQL("SELECT {} FROM {} WHERE {} ORDER BY published_on NULLS LAST, unique_id, report_name LIMIT %s").format(
            sql.SQL(", ").join(sql.Identifier(column) for column in select_columns),
            qualified_identifier(REPORT_TABLE),
            sql.SQL(" OR ").join(predicates),
        ),
        (int(limit),),
    )
    return [dict(zip(select_columns, row)) for row in cur.fetchall()]


def pointer_items_from_row(row: dict[str, Any], *, table_name: str, pointer_fields: list[dict[str, str]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for field in pointer_fields:
        key = str(row.get(field["key_column"]) or "").strip()
        if not key:
            continue
        items.append(
            {
                "table_name": table_name,
                "unique_id": None if row.get("unique_id") is None else str(row.get("unique_id")),
                "ticker": None if row.get("ticker") is None else str(row.get("ticker")),
                "report_name": None if row.get("report_name") is None else str(row.get("report_name")),
                "kind": field["kind"],
                "key_column": field["key_column"],
                "s3_key": key,
                "expected_sha256": row.get(field["sha_column"]),
                "expected_bytes": row.get(field["bytes_column"]),
                "inline_text_present": bool(row.get(field["text_column"])),
            }
        )
    return items


def fetch_pointer_items(*, limit: int, include_documents: bool = True, include_reports: bool = True) -> list[dict[str, Any]]:
    def _fetch_pointer_items() -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        with db_session() as (_, cur):
            if include_documents:
                for row in fetch_document_rows(cur, limit=limit):
                    items.extend(pointer_items_from_row(row, table_name=DOCUMENT_TABLE, pointer_fields=DOCUMENT_POINTER_FIELDS))
            if include_reports:
                for row in fetch_report_rows(cur, limit=limit):
                    items.extend(pointer_items_from_row(row, table_name=REPORT_TABLE, pointer_fields=REPORT_POINTER_FIELDS))
        return items[: int(limit)]

    return execute_db_operation(
        _fetch_pointer_items,
        operation_name="validate_announcement_s3_pointers:fetch_pointer_items",
    )


def _object_body_bytes(s3_client: Any, *, bucket: str, key: str) -> bytes:
    response = s3_client.get_object(Bucket=bucket, Key=key)
    body = response["Body"]
    return body.read()


def validate_pointer_item(
    item: dict[str, Any],
    *,
    s3_client: Any,
    bucket: str,
    verify_hash: bool = False,
) -> dict[str, Any]:
    key = str(item.get("s3_key") or "")
    result = dict(item)
    result["bucket"] = bucket
    result["status"] = "error"
    try:
        head = s3_client.head_object(Bucket=bucket, Key=key)
        actual_bytes = int(head.get("ContentLength") or 0)
        result["actual_bytes"] = actual_bytes
        result["etag"] = head.get("ETag")
        expected_bytes = item.get("expected_bytes")
        result["bytes_match"] = None if expected_bytes in (None, "") else int(expected_bytes) == actual_bytes
        if verify_hash:
            payload = _object_body_bytes(s3_client, bucket=bucket, key=key)
            actual_sha = hashlib.sha256(payload).hexdigest()
            result["actual_sha256"] = actual_sha
            expected_sha = item.get("expected_sha256")
            result["sha256_match"] = None if expected_sha in (None, "") else str(expected_sha) == actual_sha
        else:
            result["sha256_match"] = None
        if result["bytes_match"] is False or result["sha256_match"] is False:
            result["status"] = "mismatch"
        else:
            result["status"] = "ok"
    except (BotoCoreError, ClientError, KeyError, OSError) as exc:
        record_local_fallback_event(
            module="scripts.validate_announcement_s3_pointers",
            fallback_type="announcement_s3_pointer_validation_failed",
            source="announcement_s3_pointer",
            severity="warn",
            reason="Announcement S3 pointer validation could not read pointer metadata or object content.",
            error=exc,
            metadata={
                "bucket": bucket,
                "s3_key": key,
                "kind": item.get("kind"),
                "table": item.get("table_name"),
                "column": item.get("column_name"),
                "verify_hash": bool(verify_hash),
            },
        )
        result["status"] = "missing_or_unreadable"
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def build_validation_summary(results: list[dict[str, Any]], *, verify_hash: bool, dry_run: bool) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for row in results:
        status = str(row.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return {
        "status": "ok" if not any(status in counts for status in ["mismatch", "missing_or_unreadable", "error"]) else "issues_found",
        "dry_run": bool(dry_run),
        "verify_hash": bool(verify_hash),
        "checked_count": len(results),
        "status_counts": counts,
        "results": results,
    }


def run_validation(
    *,
    limit: int,
    bucket: str,
    include_documents: bool,
    include_reports: bool,
    verify_hash: bool,
    dry_run: bool,
) -> dict[str, Any]:
    items = fetch_pointer_items(limit=limit, include_documents=include_documents, include_reports=include_reports)
    if dry_run:
        planned = [{**item, "bucket": bucket, "status": "planned"} for item in items]
        return build_validation_summary(planned, verify_hash=verify_hash, dry_run=True)
    from utils.store import _get_client

    s3_client = _get_client()
    results = [
        validate_pointer_item(item, s3_client=s3_client, bucket=bucket, verify_hash=verify_hash)
        for item in items
    ]
    return build_validation_summary(results, verify_hash=verify_hash, dry_run=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate announcement S3/object-store pointers without mutating Postgres.")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--bucket", default=AWS_BUCKET_NAME)
    parser.add_argument("--skip-documents", action="store_true")
    parser.add_argument("--skip-reports", action="store_true")
    parser.add_argument("--verify-hash", action="store_true", help="Download each object and compare SHA-256. Slower but stronger than HEAD-only checks.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch pointer rows and report planned validations without contacting object storage.")
    parser.add_argument("--format", choices=["json", "text"], default="json")
    args = parser.parse_args(argv)

    result = run_validation(
        limit=int(args.limit),
        bucket=str(args.bucket),
        include_documents=not bool(args.skip_documents),
        include_reports=not bool(args.skip_reports),
        verify_hash=bool(args.verify_hash),
        dry_run=bool(args.dry_run),
    )
    if args.format == "json":
        print(json.dumps(result, indent=2, sort_keys=True, default=_json_default))
    else:
        print(f"status={result.get('status')} checked={result.get('checked_count')} counts={result.get('status_counts')}")
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
