from __future__ import annotations

import argparse
import json
import runpy
import sys
import time
import uuid
from datetime import datetime
from typing import Any

import pandas as pd

from utils.fallback_telemetry import record_local_fallback_event
from utils.sync import parse_datetime_arg
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration


TABLE_NAME = "advisory_external_task_queue"
EXTERNAL_TASK_QUEUE_SCHEMA_MIGRATION_ID = "20260611_advisory_external_task_queue_base"
EXTERNAL_TASK_QUEUE_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        task_id TEXT PRIMARY KEY,
        queue_name TEXT NOT NULL,
        task_type TEXT NOT NULL,
        task_args_json TEXT,
        priority BIGINT,
        status TEXT NOT NULL,
        attempt_count BIGINT,
        max_attempts BIGINT,
        next_attempt_at TIMESTAMPTZ,
        claimed_at TIMESTAMPTZ,
        claimed_by TEXT,
        completed_at TIMESTAMPTZ,
        last_error TEXT,
        result_json TEXT,
        created_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ,
        load_ts TIMESTAMPTZ
    )
    """,
    f"CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_claim ON {TABLE_NAME} (queue_name, status, next_attempt_at, priority DESC, created_at)",
]
SINGLE_CLIENT_QUEUES = {"nse", "dhan"}
ALLOWED_NSE_MODULES = {
    "data.nseindia.holidays",
    "data.nseindia.earnings_events",
    "data.nseindia.offmarket",
    "data.nseindia.bhavcopy_downloader",
    "data.nseindia.indices_downloader",
    "data.nseindia.recent_events",
    "data.nseindia.offmarket_parser",
    "data.nseindia.bhavcopy_parser",
    "data.nseindia.indices_parser",
}


def _record_queue_fallback(
    fallback_type: str,
    *,
    source: str,
    reason: str,
    error: Exception,
    severity: str = "warn",
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="utils.external_task_queue",
        fallback_type=fallback_type,
        source=source,
        severity=severity,
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def ensure_table() -> None:
    apply_schema_migration(
        migration_id=EXTERNAL_TASK_QUEUE_SCHEMA_MIGRATION_ID,
        description="Create serialized external task queue table.",
        statements=EXTERNAL_TASK_QUEUE_SCHEMA_STATEMENTS,
        metadata={"tables": [TABLE_NAME]},
    )


def enqueue_task(
    *,
    queue_name: str,
    task_type: str,
    task_args: dict[str, Any] | None = None,
    priority: int = 50,
    max_attempts: int = 3,
    task_id: str | None = None,
) -> dict[str, Any]:
    ensure_table()
    now = pd.Timestamp.utcnow()
    normalized_queue = str(queue_name).strip().lower()
    normalized_type = str(task_type).strip()
    if not normalized_queue or not normalized_type:
        raise ValueError("queue_name and task_type are required")
    row = {
        "task_id": task_id or str(uuid.uuid5(uuid.NAMESPACE_URL, json_dumps({"queue": normalized_queue, "type": normalized_type, "args": task_args or {}}))),
        "queue_name": normalized_queue,
        "task_type": normalized_type,
        "task_args_json": json_dumps(task_args or {}),
        "priority": int(priority),
        "status": "pending",
        "attempt_count": 0,
        "max_attempts": int(max_attempts),
        "next_attempt_at": now,
        "claimed_at": None,
        "claimed_by": None,
        "completed_at": None,
        "last_error": None,
        "result_json": None,
        "created_at": now,
        "updated_at": now,
        "load_ts": now,
    }
    df = pd.DataFrame([row])
    for column in ["next_attempt_at", "claimed_at", "completed_at", "created_at", "updated_at", "load_ts"]:
        df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    upsert_to_db(df, TABLE_NAME, unique_keys=["task_id"])
    return row


def claim_task(*, queue_name: str, worker_id: str, lease_seconds: int = 900) -> dict[str, Any] | None:
    ensure_table()
    now = pd.Timestamp.utcnow()

    def _claim() -> dict[str, Any] | None:
        with db_session(dict_factory=True) as (_, cur):
            cur.execute(
                f"""
                WITH candidate AS (
                    SELECT task_id
                    FROM {TABLE_NAME}
                    WHERE queue_name = %s
                      AND status IN ('pending', 'retry')
                      AND COALESCE(next_attempt_at, '-infinity'::timestamptz) <= %s
                    ORDER BY priority DESC NULLS LAST, created_at ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE {TABLE_NAME} q
                   SET status = 'claimed',
                       claimed_at = %s,
                       claimed_by = %s,
                       attempt_count = COALESCE(attempt_count, 0) + 1,
                       updated_at = %s,
                       load_ts = %s
                  FROM candidate
                 WHERE q.task_id = candidate.task_id
                 RETURNING q.*
                """,
                (str(queue_name).strip().lower(), now, now, worker_id, now, now),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    return execute_db_operation(
        _claim,
        operation_name="external_task_queue:claim_task",
    )


def complete_task(*, task_id: str, result: dict[str, Any] | None = None) -> None:
    ensure_table()
    now = pd.Timestamp.utcnow()

    def _complete() -> None:
        with db_session() as (_, cur):
            cur.execute(
                f"""
                UPDATE {TABLE_NAME}
                   SET status = 'completed',
                       completed_at = %s,
                       result_json = %s,
                       updated_at = %s,
                       load_ts = %s
                 WHERE task_id = %s
                """,
                (now, json_dumps(result or {}), now, now, str(task_id)),
            )

    execute_db_operation(
        _complete,
        operation_name="external_task_queue:complete_task",
    )


def fail_task(*, task_id: str, error: str, retry_delay_seconds: int = 60) -> None:
    ensure_table()
    now = pd.Timestamp.utcnow()

    def _fail() -> None:
        with db_session() as (_, cur):
            cur.execute(
                f"""
                UPDATE {TABLE_NAME}
                   SET status = CASE WHEN COALESCE(attempt_count, 0) >= COALESCE(max_attempts, 3) THEN 'failed' ELSE 'retry' END,
                       last_error = %s,
                       next_attempt_at = %s,
                       updated_at = %s,
                       load_ts = %s
                 WHERE task_id = %s
                """,
                (str(error), now + pd.Timedelta(seconds=max(1, int(retry_delay_seconds))), now, now, str(task_id)),
            )

    execute_db_operation(
        _fail,
        operation_name="external_task_queue:fail_task",
    )


def load_queue_status(*, queue_name: str | None = None, limit: int = 100) -> pd.DataFrame:
    ensure_table()
    clauses = ["1 = 1"]
    params: list[Any] = []
    if queue_name:
        clauses.append("queue_name = %s")
        params.append(str(queue_name).strip().lower())
    try:
        return sql_to_df(
            f"""
            SELECT *
            FROM {TABLE_NAME}
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC
            LIMIT %s
            """,
            params=tuple([*params, max(1, int(limit))]),
        )
    except Exception as exc:
        _record_queue_fallback(
            "external_task_queue_status_load_failed",
            source=TABLE_NAME,
            reason="External task queue status could not be loaded; operator queue visibility may be stale or unavailable.",
            error=exc,
            metadata={"queue_name": queue_name, "limit": int(limit)},
        )
        return pd.DataFrame()


def _parse_dt(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    return parse_datetime_arg(str(value))


def _symbols(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = [part.strip() for part in value.replace("\n", ",").split(",")]
    elif isinstance(value, list):
        raw = [str(part).strip() for part in value]
    else:
        raw = [str(value).strip()]
    return [part.upper() for part in raw if part]


def _run_module(module_name: str, args: list[str] | None = None) -> dict[str, Any]:
    started_at = time.monotonic()
    original_argv = sys.argv[:]
    try:
        sys.argv = [module_name, *(args or [])]
        sys.modules.pop(module_name, None)
        runpy.run_module(module_name, run_name="__main__")
        return {"status": "ok", "module": module_name, "args": args or [], "elapsed_seconds": round(time.monotonic() - started_at, 4)}
    finally:
        sys.argv = original_argv


def handle_dhan_daily_ohlcv(task_args: dict[str, Any]) -> dict[str, Any]:
    from data.dhanlive.ohlcv import sync_many_daily

    symbols = _symbols(task_args.get("symbols") or task_args.get("symbol"))
    if not symbols:
        raise ValueError("symbols is required for dhan_daily_ohlcv")
    results = sync_many_daily(
        symbols,
        exchange=str(task_args.get("exchange") or "NSE"),
        asset_type=str(task_args.get("asset_type") or "stock"),
        from_date=_parse_dt(task_args.get("from_date")),
        to_date=_parse_dt(task_args.get("to_date")),
    )
    return {"status": "ok", "handler": "dhan_daily_ohlcv", "symbols": symbols, "results": results}


def handle_dhan_intraday_ohlcv(task_args: dict[str, Any]) -> dict[str, Any]:
    from data.dhanlive.ohlcv import sync_many_intraday

    symbols = _symbols(task_args.get("symbols") or task_args.get("symbol"))
    if not symbols:
        raise ValueError("symbols is required for dhan_intraday_ohlcv")
    results = sync_many_intraday(
        symbols,
        exchange=str(task_args.get("exchange") or "NSE"),
        asset_type=str(task_args.get("asset_type") or "stock"),
        interval_minutes=int(task_args.get("interval_minutes") or task_args.get("intraday_interval") or 1),
        from_date=_parse_dt(task_args.get("from_date") or task_args.get("from_datetime")),
        to_date=_parse_dt(task_args.get("to_date") or task_args.get("to_datetime")),
    )
    return {"status": "ok", "handler": "dhan_intraday_ohlcv", "symbols": symbols, "results": results}


def handle_dhan_scrip_master(task_args: dict[str, Any]) -> dict[str, Any]:
    return _run_module("data.dhanlive.scrip_master", [str(value) for value in (task_args.get("args") or [])])


def handle_nse_module(task_args: dict[str, Any]) -> dict[str, Any]:
    module_name = str(task_args.get("module") or "").strip()
    if module_name not in ALLOWED_NSE_MODULES:
        raise ValueError(f"Unsupported NSE module for queue worker: {module_name}")
    module_args = [str(value) for value in (task_args.get("args") or [])]
    return _run_module(module_name, module_args)


def handle_download_module(task_args: dict[str, Any]) -> dict[str, Any]:
    from data.download_runner import run_download_module

    step = {
        "module": str(task_args.get("module") or ""),
        "args": [str(value) for value in (task_args.get("args") or [])],
        "purpose": str(task_args.get("purpose") or "queued"),
    }
    if not step["module"]:
        raise ValueError("module is required for download_module")
    if step["module"].startswith("data.nseindia.") and step["module"] not in ALLOWED_NSE_MODULES:
        raise ValueError(f"Unsupported NSE module for queue worker: {step['module']}")
    return run_download_module(step)


TASK_HANDLERS = {
    "dhan_daily_ohlcv": handle_dhan_daily_ohlcv,
    "dhan_intraday_ohlcv": handle_dhan_intraday_ohlcv,
    "dhan_scrip_master": handle_dhan_scrip_master,
    "nse_module": handle_nse_module,
    "download_module": handle_download_module,
}


def execute_task(task: dict[str, Any]) -> dict[str, Any]:
    task_type = str(task.get("task_type") or "").strip()
    handler = TASK_HANDLERS.get(task_type)
    if handler is None:
        raise ValueError(f"No external task handler registered for task_type={task_type}")
    task_args = json.loads(str(task.get("task_args_json") or "{}"))
    return handler(task_args)


def run_worker(
    *,
    queue_name: str,
    worker_id: str,
    once: bool = False,
    sleep_seconds: float = 5.0,
    max_tasks: int | None = None,
    idle_exit: bool = False,
) -> dict[str, Any]:
    ensure_table()
    processed = 0
    failed = 0
    while True:
        if max_tasks is not None and processed + failed >= int(max_tasks):
            break
        task = claim_task(queue_name=queue_name, worker_id=worker_id)
        if task is None:
            if once or idle_exit:
                break
            time.sleep(max(0.25, float(sleep_seconds)))
            continue
        try:
            result = execute_task(task)
            complete_task(task_id=str(task["task_id"]), result=result)
            processed += 1
        except Exception as exc:  # pragma: no cover - runtime guard
            failed += 1
            _record_queue_fallback(
                "external_task_queue_task_failed",
                source=str(task.get("queue_name") or queue_name),
                reason="Serialized external task failed in the worker and was marked for retry or failure according to its attempt policy.",
                error=exc,
                severity="error",
                metadata={
                    "task_id": task.get("task_id"),
                    "task_type": task.get("task_type"),
                    "attempt_count": task.get("attempt_count"),
                    "max_attempts": task.get("max_attempts"),
                    "worker_id": worker_id,
                },
            )
            fail_task(task_id=str(task["task_id"]), error=f"{exc.__class__.__name__}: {exc}")
        if once:
            break
    return {"status": "ok", "queue_name": queue_name, "worker_id": worker_id, "processed": processed, "failed": failed}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serialized external task queue for NSE/Dhan/Screener work.")
    parser.add_argument("--queue", default="nse", choices=sorted(SINGLE_CLIENT_QUEUES))
    parser.add_argument("--enqueue", action="store_true")
    parser.add_argument("--task-type", default="smoke")
    parser.add_argument("--task-args-json", default="{}")
    parser.add_argument("--priority", type=int, default=50)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--worker-id", default=f"worker-{uuid.uuid4().hex[:8]}")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--drain", action="store_true", help="Process until the queue is currently empty, then exit")
    parser.add_argument("--max-tasks", type=int, help="Maximum tasks to process before exiting")
    parser.add_argument("--sleep-seconds", type=float, default=5.0)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--limit", type=int, default=50)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.enqueue:
        payload = json.loads(args.task_args_json or "{}")
        result = enqueue_task(queue_name=args.queue, task_type=args.task_type, task_args=payload, priority=args.priority)
    elif args.worker:
        result = run_worker(
            queue_name=args.queue,
            worker_id=args.worker_id,
            once=bool(args.once),
            idle_exit=bool(args.drain),
            max_tasks=args.max_tasks,
            sleep_seconds=float(args.sleep_seconds),
        )
    else:
        df = load_queue_status(queue_name=args.queue if args.status else None, limit=args.limit)
        result = {"status": "ok", "rows": df.to_dict(orient="records") if not df.empty else []}
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
