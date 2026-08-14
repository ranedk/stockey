from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import pandas as pd

from utils.db import sql_to_df


ANNOUNCEMENT_TABLES = {"announcement_pipeline_documents", "announcement_pipeline_reports"}
HIGH_RISK_NAME_PARTS = ("raw", "payload", "summary", "context", "response", "report", "transcript", "ocr", "json")
CONTROL_PLANE_PREFIXES = (
    "advisory_action_",
    "advisory_decision_",
    "advisory_event_",
    "advisory_trace_",
    "advisory_operator_",
    "advisory_signal_",
)
KNOWN_POINTER_SUFFIXES = ("_s3_key", "_sha256", "_excerpt")


@dataclass(frozen=True)
class PayloadColumn:
    schema_name: str
    table_name: str
    column_name: str
    data_type: str
    estimated_rows: int
    total_bytes: int
    toast_bytes: int


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def is_pointer_metadata_column(column_name: str) -> bool:
    normalized = str(column_name or "").lower()
    return normalized.endswith(KNOWN_POINTER_SUFFIXES) or normalized.endswith(("_chars", "_bytes"))


def column_risk_score(column: PayloadColumn) -> int:
    name = column.column_name.lower()
    score = 0
    if any(part in name for part in HIGH_RISK_NAME_PARTS):
        score += 4
    if column.data_type.lower() in {"json", "jsonb"}:
        score += 2
    if column.toast_bytes > 100 * 1024 * 1024:
        score += 3
    elif column.toast_bytes > 10 * 1024 * 1024:
        score += 1
    if column.total_bytes > 1024 * 1024 * 1024:
        score += 3
    elif column.total_bytes > 100 * 1024 * 1024:
        score += 1
    if column.estimated_rows > 1_000_000:
        score += 2
    elif column.estimated_rows > 100_000:
        score += 1
    if is_pointer_metadata_column(column.column_name):
        score -= 5
    return max(score, 0)


def recommended_action(column: PayloadColumn) -> str:
    name = column.column_name.lower()
    risk = column_risk_score(column)
    if column.table_name in ANNOUNCEMENT_TABLES:
        return "handled_by_announcement_offload"
    if is_pointer_metadata_column(name):
        return "retain_pointer_metadata"
    if "raw_json" in name and column.table_name.startswith(("screenerin_", "sharpely_")):
        return "retain_raw_source_snapshot"
    if column.table_name.startswith("advisory_operator_snapshots"):
        return "compact_or_section_snapshot"
    if column.table_name.startswith(CONTROL_PLANE_PREFIXES) and any(part in name for part in ("payload", "context", "summary", "raw")):
        return "compact_or_retention"
    if risk >= 7:
        return "review_for_offload"
    if risk >= 4:
        return "review_for_compaction"
    return "retain"


def classify_column(row: dict[str, Any]) -> dict[str, Any]:
    column = PayloadColumn(
        schema_name=str(row.get("schema_name") or "public"),
        table_name=str(row.get("table_name") or ""),
        column_name=str(row.get("column_name") or ""),
        data_type=str(row.get("data_type") or ""),
        estimated_rows=int(row.get("estimated_rows") or 0),
        total_bytes=int(row.get("total_bytes") or 0),
        toast_bytes=int(row.get("toast_bytes") or 0),
    )
    risk = column_risk_score(column)
    action = recommended_action(column)
    return {
        "schema_name": column.schema_name,
        "table_name": column.table_name,
        "column_name": column.column_name,
        "data_type": column.data_type,
        "estimated_rows": column.estimated_rows,
        "total_bytes": column.total_bytes,
        "toast_bytes": column.toast_bytes,
        "risk_score": risk,
        "recommended_action": action,
        "announcement_table": column.table_name in ANNOUNCEMENT_TABLES,
    }


def fetch_payload_columns(*, schema: str, include_announcement: bool) -> list[dict[str, Any]]:
    announcement_filter = "" if include_announcement else "AND c.table_name NOT IN ('announcement_pipeline_documents', 'announcement_pipeline_reports')"
    df = sql_to_df(
        f"""
        SELECT
            c.table_schema AS schema_name,
            c.table_name,
            c.column_name,
            c.data_type,
            COALESCE(s.n_live_tup, 0)::bigint AS estimated_rows,
            COALESCE(pg_total_relation_size(pc.oid), 0)::bigint AS total_bytes,
            GREATEST(
                COALESCE(pg_total_relation_size(pc.reltoastrelid), 0),
                0
            )::bigint AS toast_bytes
        FROM information_schema.columns c
        JOIN pg_class pc ON pc.relname = c.table_name
        JOIN pg_namespace pn ON pn.oid = pc.relnamespace AND pn.nspname = c.table_schema
        LEFT JOIN pg_stat_user_tables s ON s.relid = pc.oid
        WHERE c.table_schema = %s
          AND c.data_type IN ('text', 'json', 'jsonb', 'character varying')
          {announcement_filter}
        ORDER BY pg_total_relation_size(pc.oid) DESC, c.table_name, c.column_name
        """,
        params=(schema,),
    )
    return df.to_dict(orient="records") if not df.empty else []


def build_inventory(
    *,
    schema: str = "public",
    include_announcement: bool = False,
    min_risk_score: int = 0,
    limit: int = 200,
) -> dict[str, Any]:
    raw_rows = fetch_payload_columns(schema=schema, include_announcement=include_announcement)
    rows = [classify_column(row) for row in raw_rows]
    rows = [row for row in rows if int(row["risk_score"]) >= int(min_risk_score)]
    rows.sort(key=lambda row: (int(row["risk_score"]), int(row.get("total_bytes") or 0)), reverse=True)
    rows = rows[: max(int(limit), 0)]
    actions: dict[str, int] = {}
    for row in rows:
        action = str(row["recommended_action"])
        actions[action] = actions.get(action, 0) + 1
    return {
        "status": "ok",
        "schema": schema,
        "include_announcement": bool(include_announcement),
        "min_risk_score": int(min_risk_score),
        "returned_count": len(rows),
        "recommended_action_counts": actions,
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Discover non-announcement heavy text/blob payload columns in Postgres.")
    parser.add_argument("--schema", default="public")
    parser.add_argument("--include-announcement", action="store_true", help="Include announcement tables already covered by text offload tooling.")
    parser.add_argument("--min-risk-score", type=int, default=0)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--format", choices=["json", "text"], default="json")
    args = parser.parse_args(argv)
    result = build_inventory(
        schema=args.schema,
        include_announcement=bool(args.include_announcement),
        min_risk_score=int(args.min_risk_score),
        limit=int(args.limit),
    )
    if args.format == "json":
        print(json.dumps(result, indent=2, sort_keys=True, default=_json_default))
    else:
        print(f"status={result['status']} returned={result['returned_count']} actions={result['recommended_action_counts']}")
        for row in result["rows"]:
            print(
                f"risk={row['risk_score']} action={row['recommended_action']} table={row['table_name']} column={row['column_name']} rows={row['estimated_rows']} bytes={row['total_bytes']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
