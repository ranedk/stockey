from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from advisory.decision_trace import ACTION_CONFLICTS_TABLE, EVENT_PROCESSING_TABLE, TRACES_TABLE, load_event_trace, load_symbol_trace
from utils.db import db_session, sql_to_df, upsert_to_db


TABLE_NAME = "advisory_trace_summaries"
DEFAULT_LIMIT = 100


def ensure_table() -> None:
    with db_session() as (_, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                entity_type TEXT NOT NULL,
                entity_key TEXT NOT NULL,
                limit_rows BIGINT NOT NULL,
                source_max_ts TIMESTAMPTZ,
                summary_json TEXT NOT NULL,
                raw_counts_json TEXT,
                generated_at TIMESTAMPTZ NOT NULL,
                load_ts TIMESTAMPTZ,
                UNIQUE (entity_type, entity_key, limit_rows)
            )
            """
        )


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def json_loads(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except Exception:
        return None


def normalize_trace_payload(raw: dict[str, Any]) -> dict[str, Any]:
    # Local import avoids a hard module cycle: advisory.api.app imports this store.
    from advisory.api.app import normalize_trace_payload as normalize

    return normalize(raw)


def _max_timestamp_from_raw(raw: dict[str, Any]) -> pd.Timestamp | None:
    candidates: list[Any] = []
    for section in ["processing", "traces", "steps", "action_conflicts"]:
        for row in raw.get(section) or []:
            if not isinstance(row, dict):
                continue
            for key in ["updated_at", "completed_at", "started_at", "load_ts", "asof_date"]:
                value = row.get(key)
                if value is not None:
                    candidates.append(value)
    if not candidates:
        return None
    parsed = pd.to_datetime(candidates, utc=True, errors="coerce")
    parsed = parsed[~pd.isna(parsed)]
    if len(parsed) == 0:
        return None
    return parsed.max()


def _persist_summary(entity_type: str, entity_key: str, limit_rows: int, raw: dict[str, Any]) -> dict[str, Any]:
    ensure_table()
    generated_at = pd.Timestamp.utcnow()
    summary = normalize_trace_payload(raw)
    source_max_ts = _max_timestamp_from_raw(raw)
    row = {
        "entity_type": entity_type,
        "entity_key": entity_key,
        "limit_rows": int(limit_rows),
        "source_max_ts": source_max_ts,
        "summary_json": json_dumps(summary),
        "raw_counts_json": json_dumps(summary.get("raw_counts") or {}),
        "generated_at": generated_at,
        "load_ts": generated_at,
    }
    upsert_to_db(pd.DataFrame([row]), TABLE_NAME, unique_keys=["entity_type", "entity_key", "limit_rows"])
    return summary


def build_symbol_summary(symbol: str, *, limit: int = DEFAULT_LIMIT, persist: bool = True) -> dict[str, Any]:
    normalized = str(symbol or "").strip().upper()
    if not normalized:
        raise ValueError("symbol is required")
    raw = load_symbol_trace(normalized, limit=int(limit))
    if not persist:
        return normalize_trace_payload(raw)
    return _persist_summary("symbol", normalized, int(limit), raw)


def build_event_summary(unique_id: str, *, persist: bool = True) -> dict[str, Any]:
    normalized = str(unique_id or "").strip()
    if not normalized:
        raise ValueError("unique_id is required")
    raw = load_event_trace(normalized)
    if not persist:
        return normalize_trace_payload(raw)
    return _persist_summary("event", normalized, DEFAULT_LIMIT, raw)


def load_summary(entity_type: str, entity_key: str, *, limit: int = DEFAULT_LIMIT) -> dict[str, Any] | None:
    ensure_table()
    normalized_type = str(entity_type or "").strip().lower()
    normalized_key = str(entity_key or "").strip().upper() if normalized_type == "symbol" else str(entity_key or "").strip()
    df = sql_to_df(
        f"""
        SELECT *
        FROM {TABLE_NAME}
        WHERE entity_type = %(entity_type)s
          AND entity_key = %(entity_key)s
          AND limit_rows = %(limit_rows)s
        ORDER BY generated_at DESC
        LIMIT 1
        """,
        params={"entity_type": normalized_type, "entity_key": normalized_key, "limit_rows": int(limit)},
        retries=3,
    )
    if df.empty:
        return None
    row = df.iloc[0].to_dict()
    summary = json_loads(row.get("summary_json"))
    if not isinstance(summary, dict):
        return None
    summary["_trace_summary_cache"] = {
        "source": "materialized",
        "entity_type": row.get("entity_type"),
        "entity_key": row.get("entity_key"),
        "limit_rows": row.get("limit_rows"),
        "source_max_ts": row.get("source_max_ts"),
        "generated_at": row.get("generated_at"),
    }
    return summary


def recent_entities(*, symbol_limit: int = 100, event_limit: int = 100) -> tuple[list[str], list[str]]:
    symbols = sql_to_df(
        f"""
        SELECT symbol, max(updated_at) AS latest_at
        FROM {TRACES_TABLE}
        WHERE symbol IS NOT NULL
        GROUP BY symbol
        UNION
        SELECT symbol, max(load_ts) AS latest_at
        FROM {EVENT_PROCESSING_TABLE}
        WHERE symbol IS NOT NULL
        GROUP BY symbol
        UNION
        SELECT symbol, max(load_ts) AS latest_at
        FROM {ACTION_CONFLICTS_TABLE}
        WHERE symbol IS NOT NULL
        GROUP BY symbol
        ORDER BY latest_at DESC NULLS LAST
        LIMIT %(limit)s
        """,
        params={"limit": max(1, int(symbol_limit))},
        retries=3,
    )
    events = sql_to_df(
        f"""
        SELECT unique_id, max(updated_at) AS latest_at
        FROM {TRACES_TABLE}
        WHERE unique_id IS NOT NULL
        GROUP BY unique_id
        UNION
        SELECT unique_id, max(load_ts) AS latest_at
        FROM {EVENT_PROCESSING_TABLE}
        WHERE unique_id IS NOT NULL
        GROUP BY unique_id
        ORDER BY latest_at DESC NULLS LAST
        LIMIT %(limit)s
        """,
        params={"limit": max(1, int(event_limit))},
        retries=3,
    )
    symbol_rows = [] if symbols.empty else symbols["symbol"].dropna().astype(str).str.upper().drop_duplicates().tolist()
    event_rows = [] if events.empty else events["unique_id"].dropna().astype(str).drop_duplicates().tolist()
    return symbol_rows, event_rows


def rebuild_summaries(*, symbol_limit: int = 100, event_limit: int = 100, trace_limit: int = DEFAULT_LIMIT, dry_run: bool = False) -> dict[str, Any]:
    ensure_table()
    symbols, events = recent_entities(symbol_limit=symbol_limit, event_limit=event_limit)
    summary = {
        "status": "ok",
        "symbols_found": len(symbols),
        "events_found": len(events),
        "symbols_built": 0,
        "events_built": 0,
        "errors": [],
        "dry_run": bool(dry_run),
    }
    if dry_run:
        summary["sample_symbols"] = symbols[:10]
        summary["sample_events"] = events[:10]
        return summary
    for symbol in symbols:
        try:
            build_symbol_summary(symbol, limit=trace_limit, persist=True)
            summary["symbols_built"] += 1
        except Exception as exc:
            summary["errors"].append({"entity_type": "symbol", "entity_key": symbol, "error": f"{type(exc).__name__}: {exc}"})
    for unique_id in events:
        try:
            build_event_summary(unique_id, persist=True)
            summary["events_built"] += 1
        except Exception as exc:
            summary["errors"].append({"entity_type": "event", "entity_key": unique_id, "error": f"{type(exc).__name__}: {exc}"})
    if summary["errors"]:
        summary["status"] = "partial"
    return summary


def cleanup_summaries(*, keep_latest_per_entity: int = 1, older_than_days: int | None = None, dry_run: bool = False) -> dict[str, Any]:
    ensure_table()
    keep = max(1, int(keep_latest_per_entity))
    params: dict[str, Any] = {"keep": keep}
    age_clause = ""
    if older_than_days is not None and int(older_than_days) > 0:
        age_clause = "AND generated_at < now() - (%(older_than_days)s || ' days')::interval"
        params["older_than_days"] = int(older_than_days)
    candidates = sql_to_df(
        f"""
        WITH ranked AS (
            SELECT
                entity_type,
                entity_key,
                limit_rows,
                generated_at,
                row_number() OVER (
                    PARTITION BY entity_type, entity_key
                    ORDER BY generated_at DESC
                ) AS rn
            FROM {TABLE_NAME}
        )
        SELECT *
        FROM ranked
        WHERE rn > %(keep)s
        {age_clause}
        """,
        params=params,
        retries=3,
    )
    result = {
        "status": "ok",
        "candidate_rows": 0 if candidates.empty else int(len(candidates)),
        "deleted_rows": 0,
        "keep_latest_per_entity": keep,
        "older_than_days": older_than_days,
        "dry_run": bool(dry_run),
    }
    if dry_run or candidates.empty:
        result["sample"] = [] if candidates.empty else candidates.head(10).to_dict(orient="records")
        return result
    with db_session() as (_, cur):
        cur.execute(
            f"""
            WITH ranked AS (
                SELECT
                    entity_type,
                    entity_key,
                    limit_rows,
                    generated_at,
                    row_number() OVER (
                        PARTITION BY entity_type, entity_key
                        ORDER BY generated_at DESC
                    ) AS rn
                FROM {TABLE_NAME}
            ),
            doomed AS (
                SELECT entity_type, entity_key, limit_rows, generated_at
                FROM ranked
                WHERE rn > %(keep)s
                {age_clause}
            )
            DELETE FROM {TABLE_NAME} t
            USING doomed d
            WHERE t.entity_type = d.entity_type
              AND t.entity_key = d.entity_key
              AND t.limit_rows = d.limit_rows
              AND t.generated_at = d.generated_at
            """,
            params,
        )
        result["deleted_rows"] = int(cur.rowcount or 0)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Materialize compact decision trace summaries for the operator UI.")
    parser.add_argument("--symbol", action="append", default=[])
    parser.add_argument("--unique-id", action="append", default=[])
    parser.add_argument("--symbol-limit", type=int, default=100)
    parser.add_argument("--event-limit", type=int, default=100)
    parser.add_argument("--trace-limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--cleanup", action="store_true", help="Cleanup old duplicate trace summary cache rows instead of rebuilding.")
    parser.add_argument("--keep-latest-per-entity", type=int, default=1)
    parser.add_argument("--older-than-days", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--format", choices=["json", "text"], default="json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_table()
    if args.cleanup:
        result = cleanup_summaries(
            keep_latest_per_entity=args.keep_latest_per_entity,
            older_than_days=args.older_than_days,
            dry_run=bool(args.dry_run),
        )
    elif args.dry_run:
        result = rebuild_summaries(symbol_limit=args.symbol_limit, event_limit=args.event_limit, trace_limit=args.trace_limit, dry_run=True)
    elif args.symbol or args.unique_id:
        result = {"status": "ok", "symbols_built": 0, "events_built": 0, "errors": []}
        for symbol in args.symbol:
            build_symbol_summary(symbol, limit=args.trace_limit, persist=True)
            result["symbols_built"] += 1
        for unique_id in args.unique_id:
            build_event_summary(unique_id, persist=True)
            result["events_built"] += 1
    else:
        result = rebuild_summaries(symbol_limit=args.symbol_limit, event_limit=args.event_limit, trace_limit=args.trace_limit, dry_run=False)
    if args.format == "json":
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    else:
        print(f"status={result.get('status')} symbols={result.get('symbols_built', 0)} events={result.get('events_built', 0)} errors={len(result.get('errors') or [])}")
    return 0 if result.get("status") in {"ok", "partial"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
