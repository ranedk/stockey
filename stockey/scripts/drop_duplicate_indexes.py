from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from typing import Any

import psycopg2
from psycopg2 import sql

from utils.fallback_telemetry import record_local_fallback_event
from scripts.db_duplicate_index_report import build_duplicate_index_report
from utils.db import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _safe_candidates(report: dict[str, Any], *, min_bytes: int = 0) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for group in report.get("duplicate_groups", []):
        for index in group.get("indexes") or []:
            if not index.get("safe_drop_candidate"):
                continue
            index_bytes = int(index.get("index_bytes") or 0)
            if index_bytes < int(min_bytes):
                continue
            candidates.append(
                {
                    "schema_name": group["schema_name"],
                    "table_name": group["table_name"],
                    "index_name": index["index_name"],
                    "index_bytes": index_bytes,
                    "index_size": index.get("index_size"),
                    "columns": group.get("columns"),
                    "kept_constraint_index": next(
                        (
                            other.get("index_name")
                            for other in group.get("indexes") or []
                            if other.get("constraint_name") or other.get("is_primary")
                        ),
                        None,
                    ),
                }
            )
            break
    candidates.sort(key=lambda row: int(row["index_bytes"]), reverse=True)
    return candidates


def drop_duplicate_indexes(
    *,
    schema: str = "public",
    limit: int = 200,
    max_drops: int | None = None,
    min_bytes: int = 0,
    execute: bool = False,
) -> dict[str, Any]:
    report = build_duplicate_index_report(schema=schema, limit=limit)
    candidates = _safe_candidates(report, min_bytes=min_bytes)
    if max_drops is not None:
        candidates = candidates[: max(int(max_drops), 0)]

    output: dict[str, Any] = {
        "execute": bool(execute),
        "schema": schema,
        "candidate_count": len(candidates),
        "candidate_bytes": sum(int(row["index_bytes"]) for row in candidates),
        "candidates": candidates,
        "dropped": [],
        "skipped": [],
    }
    if not execute or not candidates:
        return output

    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )
    conn.autocommit = True
    try:
        cur = conn.cursor()
        for candidate in candidates:
            try:
                cur.execute(
                    sql.SQL("DROP INDEX CONCURRENTLY IF EXISTS {}.{}").format(
                        sql.Identifier(candidate["schema_name"]),
                        sql.Identifier(candidate["index_name"]),
                    )
                )
                output["dropped"].append(candidate)
                print(
                    f"[drop_duplicate_indexes] dropped {candidate['schema_name']}.{candidate['index_name']} size={candidate['index_size']}",
                    file=sys.stderr,
                    flush=True,
                )
            except Exception as exc:
                candidate = dict(candidate)
                candidate["error"] = f"{exc.__class__.__name__}: {exc}"
                output["skipped"].append(candidate)
                record_local_fallback_event(
                    module="scripts.drop_duplicate_indexes",
                    source="postgres_indexes",
                    fallback_type="drop_duplicate_index_failed",
                    severity="warn",
                    reason="Duplicate-index cleanup skipped one candidate because DROP INDEX failed.",
                    error=exc,
                    metadata={
                        "schema_name": candidate.get("schema_name"),
                        "table_name": candidate.get("table_name"),
                        "index_name": candidate.get("index_name"),
                        "index_bytes": candidate.get("index_bytes"),
                    },
                )
                print(
                    f"[drop_duplicate_indexes] failed {candidate['schema_name']}.{candidate['index_name']} error={exc.__class__.__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
        cur.close()
    finally:
        conn.close()
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Drop exact duplicate non-constraint Postgres indexes reported by db_duplicate_index_report.py."
    )
    parser.add_argument("--schema", default="public")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--max-drops", type=int, default=None, help="Limit number of indexes dropped.")
    parser.add_argument("--min-mb", type=float, default=0.0, help="Only include candidates at least this large.")
    parser.add_argument("--execute", action="store_true", help="Actually drop indexes. Without this, dry-run only.")
    args = parser.parse_args(argv)
    output = drop_duplicate_indexes(
        schema=args.schema,
        limit=int(args.limit),
        max_drops=args.max_drops,
        min_bytes=int(float(args.min_mb) * 1024 * 1024),
        execute=bool(args.execute),
    )
    print(json.dumps(output, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
