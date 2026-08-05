from __future__ import annotations

import json
from pathlib import Path
import sys
import uuid
from typing import Any

import pandas as pd
from environs import Env

from utils.db import read_db_retry_telemetry_events, sql_to_df, upsert_to_db
from utils.redaction import redact_json_text, redact_mapping, redact_text
from utils.schema_migrations import apply_schema_migration


env = Env()
env.read_env()

TABLE_NAME = "advisory_fallback_events"
LOCAL_FALLBACK_TELEMETRY_FILE = Path(env.str("LOCAL_FALLBACK_TELEMETRY_FILE", "logs/fallback/local_fallback_events.jsonl"))
FALLBACK_TELEMETRY_SCHEMA_MIGRATION_ID = "20260611_advisory_fallback_telemetry_base"
FALLBACK_TELEMETRY_SCHEMA_STATEMENTS = [
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
    """,
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS source TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS fallback_used BOOLEAN",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS deterministic_fallback BOOLEAN",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS metadata_json TEXT",
]


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
    apply_schema_migration(
        migration_id=FALLBACK_TELEMETRY_SCHEMA_MIGRATION_ID,
        description="Create runtime fallback/degraded-path telemetry table.",
        statements=FALLBACK_TELEMETRY_SCHEMA_STATEMENTS,
        metadata={"tables": [TABLE_NAME]},
    )


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
        "reason": redact_text(reason),
        "fallback_used": bool(fallback_used),
        "deterministic_fallback": bool(deterministic_fallback),
        "error_type": error_type,
        "error_message": redact_text(error_message),
        "metadata_json": redact_json_text(redact_mapping(metadata or {})),
        "load_ts": now,
    }
    try:
        ensure_table()
        upsert_to_db(pd.DataFrame([row]), TABLE_NAME, unique_keys=["event_id"])
    except Exception as exc:
        record_local_fallback_event(
            module="utils.fallback_telemetry",
            source=TABLE_NAME,
            fallback_type="fallback_telemetry_db_write_failed",
            severity="error",
            reason="DB-backed fallback telemetry write failed; the failure was spooled locally so Operator Health can still surface telemetry degradation.",
            error=exc,
            metadata={
                "original_event_id": row["event_id"],
                "original_module": row["module"],
                "original_source": row["source"],
                "original_fallback_type": row["fallback_type"],
            },
        )
        print(
            f"[advisory.fallback_telemetry] failed to record fallback event module={module} type={fallback_type} error={type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
    return row


def record_local_fallback_event(
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
    """Record fallback telemetry locally when DB-backed telemetry would be unsafe."""
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
    observed = pd.to_datetime(observed_at, utc=True, errors="coerce") if observed_at is not None else now
    row = {
        "event_id": str(uuid.uuid4()),
        "observed_at": observed.isoformat() if hasattr(observed, "isoformat") else str(observed),
        "module": str(module or "unknown"),
        "source": source,
        "fallback_type": str(fallback_type or "unknown"),
        "severity": str(severity or "warn"),
        "status": str(status or "active"),
        "symbol": None if symbol is None else str(symbol).upper(),
        "unique_id": unique_id,
        "reason": redact_text(reason),
        "fallback_used": bool(fallback_used),
        "deterministic_fallback": bool(deterministic_fallback),
        "error_type": error_type,
        "error_message": redact_text(error_message),
        "metadata_json": redact_json_text(redact_mapping(metadata or {})),
        "load_ts": now.isoformat(),
    }
    try:
        path = Path(LOCAL_FALLBACK_TELEMETRY_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) + "\n")
    except Exception:
        pass
    return row


def read_local_fallback_events(*, hours: int = 24, limit: int = 100) -> list[dict[str, Any]]:
    path = Path(LOCAL_FALLBACK_TELEMETRY_FILE)
    if not path.exists():
        return []
    cutoff = pd.Timestamp.utcnow() - pd.Timedelta(hours=max(1, int(hours)))
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception as exc:
                    record_local_fallback_event(
                        module="utils.fallback_telemetry",
                        source=str(path),
                        fallback_type="local_fallback_telemetry_line_parse_failed",
                        severity="warn",
                        reason="A local fallback telemetry JSONL row could not be parsed and was skipped.",
                        error=exc,
                        metadata={
                            "line_number": line_number,
                            "line_length": len(line),
                            "line_excerpt": line[:240],
                        },
                    )
                    continue
                observed_at = pd.to_datetime(row.get("observed_at"), utc=True, errors="coerce")
                if pd.isna(observed_at) or observed_at < cutoff:
                    continue
                row["observed_at"] = observed_at.isoformat()
                rows.append(row)
    except Exception as exc:
        record_local_fallback_event(
            module="utils.fallback_telemetry",
            source=str(path),
            fallback_type="local_fallback_telemetry_read_failed",
            severity="error",
            reason="Local fallback telemetry spool could not be read.",
            error=exc,
            metadata={"path": str(path)},
        )
        return []
    rows.sort(key=lambda item: str(item.get("observed_at") or ""), reverse=True)
    return rows[: max(1, int(limit))]


def _db_retry_spool_summary(*, hours: int, limit: int) -> tuple[list[dict[str, Any]], int, int, int, dict[str, int], dict[str, int]]:
    db_retry_rows = read_db_retry_telemetry_events(hours=hours, limit=limit)
    db_retry_count = len(db_retry_rows)
    db_retry_error_count = sum(1 for row in db_retry_rows if str(row.get("severity") or "").lower() == "error")
    db_retry_warn_count = sum(1 for row in db_retry_rows if str(row.get("severity") or "").lower() == "warn")
    db_counts_by_type: dict[str, int] = {}
    db_counts_by_module: dict[str, int] = {}
    for row in db_retry_rows:
        fallback_type = str(row.get("fallback_type") or "db_retry")
        module = str(row.get("module") or "utils.db")
        db_counts_by_type[fallback_type] = db_counts_by_type.get(fallback_type, 0) + 1
        db_counts_by_module[module] = db_counts_by_module.get(module, 0) + 1
    return db_retry_rows, db_retry_count, db_retry_error_count, db_retry_warn_count, db_counts_by_type, db_counts_by_module


def _local_fallback_spool_summary(*, hours: int, limit: int) -> tuple[list[dict[str, Any]], int, int, int, dict[str, int], dict[str, int]]:
    local_rows = read_local_fallback_events(hours=hours, limit=limit)
    local_count = len(local_rows)
    local_error_count = sum(1 for row in local_rows if str(row.get("severity") or "").lower() == "error")
    local_warn_count = sum(1 for row in local_rows if str(row.get("severity") or "").lower() == "warn")
    counts_by_type: dict[str, int] = {}
    counts_by_module: dict[str, int] = {}
    for row in local_rows:
        fallback_type = str(row.get("fallback_type") or "local_fallback")
        module = str(row.get("module") or "unknown")
        counts_by_type[fallback_type] = counts_by_type.get(fallback_type, 0) + 1
        counts_by_module[module] = counts_by_module.get(module, 0) + 1
    return local_rows, local_count, local_error_count, local_warn_count, counts_by_type, counts_by_module


def _merge_counts(*sources: dict[str, int]) -> dict[str, int]:
    out: dict[str, int] = {}
    for source in sources:
        for key, value in source.items():
            out[str(key)] = int(out.get(str(key)) or 0) + int(value or 0)
    return out


def _spool_only_summary(
    *,
    hours: int,
    limit: int,
    message: str,
    error: str | None = None,
    empty_status: str = "ok",
) -> dict[str, Any]:
    db_retry_rows, db_retry_count, db_retry_error_count, db_retry_warn_count, db_counts_by_type, db_counts_by_module = _db_retry_spool_summary(hours=hours, limit=limit)
    local_rows, local_count, local_error_count, local_warn_count, local_counts_by_type, local_counts_by_module = _local_fallback_spool_summary(hours=hours, limit=limit)
    active_count = db_retry_count + local_count
    error_count = db_retry_error_count + local_error_count
    warn_count = db_retry_warn_count + local_warn_count
    status = "error" if error_count else "warn" if active_count else empty_status
    out = {
        "status": status,
        "message": message if active_count else ("Could not inspect fallback telemetry." if error else "No fallback telemetry table exists yet."),
        "window_hours": int(hours),
        "active_count": active_count if active_count or not error else None,
        "error_count": error_count,
        "warn_count": warn_count,
        "counts_by_type": _merge_counts(db_counts_by_type, local_counts_by_type),
        "counts_by_module": _merge_counts(db_counts_by_module, local_counts_by_module),
        "db_retry_count": db_retry_count,
        "db_retry_error_count": db_retry_error_count,
        "local_fallback_count": local_count,
        "local_fallback_error_count": local_error_count,
        "nse_session_reset_count": 0,
        "nse_retry_count": 0,
        "nse_http_count": 0,
        "rows": (db_retry_rows + local_rows)[:limit],
    }
    if error:
        out["error"] = error
    return out


def summarize_fallback_events(*, hours: int = 24, limit: int = 25) -> dict[str, Any]:
    db_retry_rows, db_retry_count, db_retry_error_count, db_retry_warn_count, db_counts_by_type, db_counts_by_module = _db_retry_spool_summary(hours=hours, limit=limit)
    local_rows, local_count, local_error_count, local_warn_count, local_counts_by_type, local_counts_by_module = _local_fallback_spool_summary(hours=hours, limit=limit)
    try:
        fallback_table_exists = table_exists(TABLE_NAME)
    except Exception as exc:
        record_local_fallback_event(
            module="utils.fallback_telemetry",
            source=TABLE_NAME,
            fallback_type="fallback_telemetry_table_check_failed",
            severity="error",
            reason="Fallback telemetry summary could not inspect the fallback telemetry table and used local spool-only evidence.",
            error=exc,
            metadata={"hours": max(1, int(hours)), "limit": max(1, int(limit))},
        )
        return _spool_only_summary(
            hours=hours,
            limit=limit,
            message="Could not inspect fallback telemetry table; local fallback spools were inspected.",
            error=f"{type(exc).__name__}: {exc}",
            empty_status="error",
        )
    if not fallback_table_exists:
        return _spool_only_summary(
            hours=hours,
            limit=limit,
            message="Recent local fallback events found.",
        )
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
        record_local_fallback_event(
            module="utils.fallback_telemetry",
            source=TABLE_NAME,
            fallback_type="fallback_telemetry_table_query_failed",
            severity="error",
            reason="Fallback telemetry summary could not query the fallback telemetry table and used local spool-only evidence.",
            error=exc,
            metadata={"hours": max(1, int(hours)), "limit": max(1, int(limit))},
        )
        return _spool_only_summary(
            hours=hours,
            limit=limit,
            message="Could not inspect fallback telemetry table; local fallback spools were inspected.",
            error=f"{type(exc).__name__}: {exc}",
            empty_status="error",
        )
    count_row = counts.iloc[0].to_dict() if not counts.empty else {}
    active_count = int(count_row.get("active_count") or 0) + db_retry_count + local_count
    error_count = int(count_row.get("error_count") or 0) + db_retry_error_count + local_error_count
    warn_count = int(count_row.get("warn_count") or 0) + db_retry_warn_count + local_warn_count
    status = "error" if error_count else "warn" if active_count else "ok"
    counts_by_type = {str(row["fallback_type"]): int(row["count"] or 0) for row in by_type.to_dict(orient="records")} if not by_type.empty else {}
    counts_by_module = {str(row["module"]): int(row["count"] or 0) for row in by_module.to_dict(orient="records")} if not by_module.empty else {}
    counts_by_type = _merge_counts(counts_by_type, db_counts_by_type, local_counts_by_type)
    counts_by_module = _merge_counts(counts_by_module, db_counts_by_module, local_counts_by_module)
    nse_session_reset_count = int(counts_by_type.get("nse_session_reset") or 0)
    nse_retry_count = int(counts_by_type.get("nse_retry") or 0)
    return {
        "status": status,
        "message": "Recent fallback/degraded-path events found." if active_count else "No recent fallback/degraded-path events.",
        "window_hours": int(hours),
        "active_count": active_count,
        "error_count": error_count,
        "warn_count": warn_count,
        "counts_by_type": counts_by_type,
        "counts_by_module": counts_by_module,
        "nse_session_reset_count": nse_session_reset_count,
        "nse_retry_count": nse_retry_count,
        "nse_http_count": nse_session_reset_count + nse_retry_count,
        "db_retry_count": db_retry_count,
        "db_retry_error_count": db_retry_error_count,
        "local_fallback_count": local_count,
        "local_fallback_error_count": local_error_count,
        "rows": (db_retry_rows + local_rows + _records(rows))[:limit],
    }
