from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from advisory.decision_trace import EVENT_PROCESSING_TABLE
from utils.db import db_session, sql_to_df


ANNOUNCEMENT_DOCUMENTS_TABLE = "announcement_pipeline_documents"
SUCCESS_PROCESSING_STATUSES = ("ok", "success", "completed", "processed")
RECOVERED_DOCUMENT_STATUSES = ("completed", "success", "ok", "processed", "skipped", "unavailable")


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df.empty:
        return []
    clean = df.copy().astype(object).where(pd.notna(df), None)
    return clean.to_dict(orient="records")


def _table_exists(table_name: str) -> bool:
    df = sql_to_df(
        """
        SELECT 1 AS exists_flag
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = %s
        LIMIT 1
        """,
        params=(table_name,),
        retries=2,
        statement_timeout_ms=5000,
    )
    return not df.empty


def _table_columns(table_name: str) -> set[str]:
    df = sql_to_df(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
        """,
        params=(table_name,),
        retries=2,
        statement_timeout_ms=5000,
    )
    return set(df["column_name"].astype(str).tolist()) if not df.empty else set()


def ensure_superseded_columns() -> None:
    with db_session() as (_, cur):
        if _table_exists(EVENT_PROCESSING_TABLE):
            cur.execute(
                f"""
                ALTER TABLE {EVENT_PROCESSING_TABLE}
                    ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS superseded_by_status TEXT,
                    ADD COLUMN IF NOT EXISTS superseded_by_completed_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS superseded_reason TEXT
                """
            )
        if _table_exists(ANNOUNCEMENT_DOCUMENTS_TABLE):
            cur.execute(
                f"""
                ALTER TABLE {ANNOUNCEMENT_DOCUMENTS_TABLE}
                    ADD COLUMN IF NOT EXISTS last_error_superseded_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS last_error_superseded_reason TEXT
                """
            )


def load_superseded_event_processing_failures(*, limit: int = 500) -> list[dict[str, Any]]:
    if not _table_exists(EVENT_PROCESSING_TABLE):
        return []
    columns = _table_columns(EVENT_PROCESSING_TABLE)
    superseded_filter = "AND failed.superseded_at IS NULL" if "superseded_at" in columns else ""
    df = sql_to_df(
        f"""
        SELECT
            failed.unique_id,
            failed.symbol,
            failed.source_type,
            failed.stage,
            failed.status,
            failed.started_at,
            failed.completed_at,
            failed.error,
            newer.status AS superseded_by_status,
            COALESCE(newer.completed_at, newer.started_at, newer.load_ts) AS superseded_by_completed_at
        FROM {EVENT_PROCESSING_TABLE} failed
        JOIN LATERAL (
            SELECT status, completed_at, started_at, load_ts
            FROM {EVENT_PROCESSING_TABLE} newer
            WHERE newer.unique_id = failed.unique_id
              AND newer.stage = failed.stage
              AND LOWER(newer.status) IN %(success_statuses)s
              AND COALESCE(newer.completed_at, newer.started_at, newer.load_ts)
                    > COALESCE(failed.completed_at, failed.started_at, failed.load_ts)
            ORDER BY COALESCE(newer.completed_at, newer.started_at, newer.load_ts) DESC NULLS LAST
            LIMIT 1
        ) newer ON TRUE
        WHERE (LOWER(failed.status) IN ('failed', 'error') OR failed.error IS NOT NULL)
          {superseded_filter}
        ORDER BY COALESCE(failed.completed_at, failed.started_at, failed.load_ts) DESC NULLS LAST
        LIMIT %(limit)s
        """,
        params={"success_statuses": tuple(SUCCESS_PROCESSING_STATUSES), "limit": max(1, int(limit))},
        retries=2,
        statement_timeout_ms=15000,
    )
    return _records(df)


def load_recovered_announcement_document_errors(*, limit: int = 500) -> list[dict[str, Any]]:
    if not _table_exists(ANNOUNCEMENT_DOCUMENTS_TABLE):
        return []
    columns = _table_columns(ANNOUNCEMENT_DOCUMENTS_TABLE)
    superseded_filter = "AND last_error_superseded_at IS NULL" if "last_error_superseded_at" in columns else ""
    optional_columns = [
        "unique_id",
        "ticker",
        "symbol",
        "ocr_status",
        "parse_status",
        "last_error",
        "updated_at",
        "load_ts",
    ]
    select_columns = [column for column in optional_columns if column in columns]
    if not {"unique_id", "last_error"}.issubset(set(select_columns)):
        return []
    order_candidates = [column for column in ["updated_at", "load_ts", "created_at", "published_on"] if column in columns]
    order_expr = "COALESCE(" + ", ".join(order_candidates) + ")" if order_candidates else "unique_id"
    df = sql_to_df(
        f"""
        SELECT {', '.join(select_columns)}
        FROM {ANNOUNCEMENT_DOCUMENTS_TABLE}
        WHERE last_error IS NOT NULL
          {superseded_filter}
          AND LOWER(COALESCE(ocr_status, '')) IN %(recovered_statuses)s
          AND LOWER(COALESCE(parse_status, '')) IN %(recovered_statuses)s
        ORDER BY {order_expr} DESC NULLS LAST
        LIMIT %(limit)s
        """,
        params={"recovered_statuses": tuple(RECOVERED_DOCUMENT_STATUSES), "limit": max(1, int(limit))},
        retries=2,
        statement_timeout_ms=15000,
    )
    return _records(df)


def mark_superseded_event_processing_failures(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    now = pd.Timestamp.utcnow().to_pydatetime()
    updated = 0
    with db_session() as (_, cur):
        for row in rows:
            cur.execute(
                f"""
                UPDATE {EVENT_PROCESSING_TABLE}
                SET superseded_at = %s,
                    superseded_by_status = %s,
                    superseded_by_completed_at = %s,
                    superseded_reason = %s
                WHERE unique_id = %s
                  AND stage = %s
                  AND started_at IS NOT DISTINCT FROM %s
                  AND superseded_at IS NULL
                """,
                (
                    now,
                    row.get("superseded_by_status"),
                    row.get("superseded_by_completed_at"),
                    "newer_successful_processing_run",
                    row.get("unique_id"),
                    row.get("stage"),
                    row.get("started_at"),
                ),
            )
            updated += int(getattr(cur, "rowcount", 0) or 0)
    return updated


def mark_recovered_announcement_document_errors(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    now = pd.Timestamp.utcnow().to_pydatetime()
    updated = 0
    with db_session() as (_, cur):
        for row in rows:
            cur.execute(
                f"""
                UPDATE {ANNOUNCEMENT_DOCUMENTS_TABLE}
                SET last_error_superseded_at = %s,
                    last_error_superseded_reason = %s
                WHERE unique_id = %s
                  AND last_error IS NOT NULL
                  AND last_error_superseded_at IS NULL
                  AND LOWER(COALESCE(ocr_status, '')) IN %s
                  AND LOWER(COALESCE(parse_status, '')) IN %s
                """,
                (
                    now,
                    "ocr_and_parse_status_recovered",
                    row.get("unique_id"),
                    tuple(RECOVERED_DOCUMENT_STATUSES),
                    tuple(RECOVERED_DOCUMENT_STATUSES),
                ),
            )
            updated += int(getattr(cur, "rowcount", 0) or 0)
    return updated


def cleanup_superseded_failures(
    *,
    apply: bool = False,
    limit: int = 500,
    include_event_processing: bool = True,
    include_announcement_documents: bool = True,
) -> dict[str, Any]:
    if apply:
        ensure_superseded_columns()
    event_rows = load_superseded_event_processing_failures(limit=limit) if include_event_processing else []
    document_rows = load_recovered_announcement_document_errors(limit=limit) if include_announcement_documents else []
    result = {
        "status": "applied" if apply else "dry_run",
        "event_processing": {
            "candidates": len(event_rows),
            "updated": mark_superseded_event_processing_failures(event_rows) if apply else 0,
            "sample": event_rows[:10],
        },
        "announcement_documents": {
            "candidates": len(document_rows),
            "updated": mark_recovered_announcement_document_errors(document_rows) if apply else 0,
            "sample": document_rows[:10],
        },
    }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preview or mark recovered processing failures as superseded.")
    parser.add_argument("--apply", action="store_true", help="Write superseded markers. Default is dry-run preview only.")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--skip-event-processing", action="store_true")
    parser.add_argument("--skip-announcement-documents", action="store_true")
    args = parser.parse_args(argv)
    result = cleanup_superseded_failures(
        apply=bool(args.apply),
        limit=max(1, int(args.limit)),
        include_event_processing=not bool(args.skip_event_processing),
        include_announcement_documents=not bool(args.skip_announcement_documents),
    )
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
