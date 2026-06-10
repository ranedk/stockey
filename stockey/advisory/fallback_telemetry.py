from __future__ import annotations

import json
import sys
import uuid
from typing import Any

import pandas as pd

from utils.db import db_session, sql_to_df, upsert_to_db


TABLE_NAME = "advisory_fallback_events"


def _json_dumps(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, default=str)


def _json_ready(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return value


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df.empty:
        return []
    clean = df.copy().astype(object).where(pd.notna(df), None)
    return [{key: _json_ready(value) for key, value in row.items()} for row in clean.to_dict(orient="records")]


def table_exists(table_name: str = TABLE_NAME) -> bool:
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


def ensure_table() -> None:
    with db_session() as (_, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                event_id TEXT PRIMARY KEY,
                observed_at TIMESTAMPTZ NOT NULL,
                module TEXT NOT NULL,
                source TEXT,
                fallback_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                status TEXT NOT NULL,
                symbol TEXT,
                unique_id TEXT,
                reason TEXT,
                fallback_used BOOLEAN,
                deterministic_fallback BOOLEAN,
                error_type TEXT,
                error_message TEXT,
                metadata_json TEXT,
                load_ts TIMESTAMPTZ
            )
            """
        )
        for column, sql_type in {
            "source": "TEXT",
            "fallback_used": "BOOLEAN",
            "deterministic_fallback": "BOOLEAN",
            "metadata_json": "TEXT",
        }.items():
            cur.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS {column} {sql_type}")


def record_fallback_event(
    *,
    module: str,
    fallback_type: str,
    source: str | None = None,
    severity: str = "warn",
    status: str = "active",
    symbol: str | None = None,
    unique_id: str | None = None,
    reason: str | None = None,
    fallback_used: bool = True,
    deterministic_fallback: bool = False,
    error: Exception | str | None = None,
    metadata: dict[str, Any] | None = None,
    observed_at: Any | None = None,
) -> dict[str, Any]:
    """Persist a runtime fallback/degraded-path marker without breaking caller flow."""
    now = pd.Timestamp.utcnow()
    error_type = None
    error_message = None
    if error is not None:
        if isinstance(error, Exception):
            error_type = type(error).__name__
            error_message = str(error)
        else:
            error_type = "Error"
            error_message = str(error)
    row = {
        "event_id": str(uuid.uuid4()),
        "observed_at": pd.to_datetime(observed_at, utc=True, errors="coerce") if observed_at is not None else now,
        "module": str(module or "unknown"),
        "source": source,
        "fallback_type": str(fallback_type or "unknown"),
        "severity": str(severity or "warn"),
        "status": str(status or "active"),
        "symbol": None if symbol is None else str(symbol).upper(),
        "unique_id": unique_id,
        "reason": reason,
        "fallback_used": bool(fallback_used),
        "deterministic_fallback": bool(deterministic_fallback),
        "error_type": error_type,
        "error_message": error_message,
        "metadata_json": _json_dumps(metadata or {}),
        "load_ts": now,
    }
    try:
        ensure_table()
        upsert_to_db(pd.DataFrame([row]), TABLE_NAME, unique_keys=["event_id"])
    except Exception as exc:
        print(
            f"[advisory.fallback_telemetry] failed to record fallback event module={module} type={fallback_type} error={type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
    return row


def summarize_fallback_events(*, hours: int = 24, limit: int = 25) -> dict[str, Any]:
    if not table_exists(TABLE_NAME):
        return {
            "status": "ok",
            "message": "No fallback telemetry table exists yet.",
            "window_hours": int(hours),
            "active_count": 0,
            "error_count": 0,
            "warn_count": 0,
            "counts_by_type": {},
            "counts_by_module": {},
            "rows": [],
        }
    try:
        rows = sql_to_df(
            f"""
            SELECT *
            FROM {TABLE_NAME}
            WHERE observed_at >= now() - (%(hours)s || ' hours')::interval
            ORDER BY observed_at DESC
            LIMIT %(limit)s
            """,
            params={"hours": max(1, int(hours)), "limit": max(1, int(limit))},
            retries=3,
            statement_timeout_ms=10000,
        )
        counts = sql_to_df(
            f"""
            SELECT
                COUNT(*) AS active_count,
                SUM(CASE WHEN severity = 'error' THEN 1 ELSE 0 END) AS error_count,
                SUM(CASE WHEN severity = 'warn' THEN 1 ELSE 0 END) AS warn_count
            FROM {TABLE_NAME}
            WHERE observed_at >= now() - (%(hours)s || ' hours')::interval
            """,
            params={"hours": max(1, int(hours))},
            retries=3,
            statement_timeout_ms=10000,
        )
        by_type = sql_to_df(
            f"""
            SELECT fallback_type, COUNT(*) AS count
            FROM {TABLE_NAME}
            WHERE observed_at >= now() - (%(hours)s || ' hours')::interval
            GROUP BY fallback_type
            ORDER BY count DESC, fallback_type
            """,
            params={"hours": max(1, int(hours))},
            retries=3,
            statement_timeout_ms=10000,
        )
        by_module = sql_to_df(
            f"""
            SELECT module, COUNT(*) AS count
            FROM {TABLE_NAME}
            WHERE observed_at >= now() - (%(hours)s || ' hours')::interval
            GROUP BY module
            ORDER BY count DESC, module
            """,
            params={"hours": max(1, int(hours))},
            retries=3,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        return {
            "status": "error",
            "message": "Could not inspect fallback telemetry.",
            "error": f"{type(exc).__name__}: {exc}",
            "window_hours": int(hours),
            "active_count": None,
            "rows": [],
        }
    count_row = counts.iloc[0].to_dict() if not counts.empty else {}
    active_count = int(count_row.get("active_count") or 0)
    error_count = int(count_row.get("error_count") or 0)
    warn_count = int(count_row.get("warn_count") or 0)
    status = "error" if error_count else "warn" if active_count else "ok"
    return {
        "status": status,
        "message": "Recent fallback/degraded-path events found." if active_count else "No recent fallback/degraded-path events.",
        "window_hours": int(hours),
        "active_count": active_count,
        "error_count": error_count,
        "warn_count": warn_count,
        "counts_by_type": {str(row["fallback_type"]): int(row["count"] or 0) for row in by_type.to_dict(orient="records")} if not by_type.empty else {},
        "counts_by_module": {str(row["module"]): int(row["count"] or 0) for row in by_module.to_dict(orient="records")} if not by_module.empty else {},
        "rows": _records(rows),
    }
