from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import threading
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from environs import Env

from advisory.fallback_telemetry import record_local_fallback_event


env = Env()
env.read_env()

DEFAULT_LOG_DIR = Path(env.str("PERFORMANCE_LOG_DIR", "logs/performance"))
DEFAULT_SLOW_LOG_FILE = Path(env.str("SLOW_OPERATION_LOG_FILE", str(DEFAULT_LOG_DIR / "slow_operations.jsonl")))
DEFAULT_SLOW_STATE_FILE = Path(env.str("SLOW_OPERATION_STATE_FILE", str(DEFAULT_LOG_DIR / "slow_operation_state.json")))
DEFAULT_THRESHOLD_MS = env.float("SLOW_OPERATION_THRESHOLD_MS", 1000.0)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _safe_json(value: Any) -> Any:
    try:
        json.dumps(value, default=_json_default)
        return value
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.performance_slowlog",
            fallback_type="performance_slowlog_json_safety_failed",
            source="slow_operation_details",
            severity="warn",
            reason="Slow-operation details were not JSON serializable and were stringified.",
            error=exc,
            metadata={"value_type": type(value).__name__},
        )
        return str(value)


def operation_fingerprint(kind: str, operation: str, details: dict[str, Any] | None = None) -> str:
    stable_details = {}
    for key, value in (details or {}).items():
        if key in {"route", "method", "endpoint", "script", "stage", "source", "snapshot_key", "snapshot_name"}:
            stable_details[key] = value
    raw = json.dumps(
        {
            "kind": str(kind),
            "operation": str(operation),
            "details": stable_details,
        },
        sort_keys=True,
        default=_json_default,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def load_slow_state(state_file: str | Path = DEFAULT_SLOW_STATE_FILE) -> dict[str, Any]:
    path = Path(state_file)
    if not path.exists():
        return {"version": 1, "issues": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        record_local_fallback_event(
            module="advisory.performance_slowlog",
            fallback_type="performance_slowlog_state_parse_failed",
            source=str(path),
            severity="warn",
            reason="Slow-operation state file could not be parsed; an empty state was used.",
            error=exc,
            metadata={"state_file": str(path)},
        )
        return {"version": 1, "issues": {}}
    if not isinstance(payload, dict):
        return {"version": 1, "issues": {}}
    issues = payload.get("issues")
    if not isinstance(issues, dict):
        payload["issues"] = {}
    payload.setdefault("version", 1)
    return payload


def save_slow_state(state: dict[str, Any], state_file: str | Path = DEFAULT_SLOW_STATE_FILE) -> None:
    path = Path(state_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp")
    tmp_path.write_text(json.dumps(state, indent=2, sort_keys=True, default=_json_default), encoding="utf-8")
    tmp_path.replace(path)


def record_slow_operation(
    *,
    kind: str,
    operation: str,
    elapsed_ms: float,
    threshold_ms: float | None = None,
    details: dict[str, Any] | None = None,
    log_file: str | Path = DEFAULT_SLOW_LOG_FILE,
    state_file: str | Path = DEFAULT_SLOW_STATE_FILE,
) -> dict[str, Any] | None:
    threshold = DEFAULT_THRESHOLD_MS if threshold_ms is None else float(threshold_ms)
    if float(elapsed_ms) < threshold:
        return None

    fingerprint = operation_fingerprint(kind, operation, details)
    now = _now_iso()
    state = load_slow_state(state_file)
    issues = state.setdefault("issues", {})
    existing = issues.get(fingerprint)
    is_new = not isinstance(existing, dict)
    if is_new:
        existing = {
            "fingerprint": fingerprint,
            "kind": str(kind),
            "operation": str(operation),
            "status": "open",
            "first_seen_at": now,
            "count": 0,
            "max_elapsed_ms": 0.0,
            "last_details": {},
        }
    existing["last_seen_at"] = now
    existing["count"] = int(existing.get("count") or 0) + 1
    existing["last_elapsed_ms"] = round(float(elapsed_ms), 2)
    existing["max_elapsed_ms"] = round(max(float(existing.get("max_elapsed_ms") or 0.0), float(elapsed_ms)), 2)
    existing["threshold_ms"] = round(float(threshold), 2)
    existing["last_details"] = _safe_json(details or {})
    issues[fingerprint] = existing
    state["updated_at"] = now
    save_slow_state(state, state_file)

    event = {
        "ts": now,
        "fingerprint": fingerprint,
        "is_new": is_new,
        "kind": str(kind),
        "operation": str(operation),
        "elapsed_ms": round(float(elapsed_ms), 2),
        "threshold_ms": round(float(threshold), 2),
        "occurrence_count": int(existing["count"]),
        "status": existing.get("status", "open"),
        "details": _safe_json(details or {}),
    }
    path = Path(log_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True, default=_json_default) + "\n")
    return event


@contextlib.contextmanager
def slow_operation(
    kind: str,
    operation: str,
    *,
    threshold_ms: float | None = None,
    details: dict[str, Any] | None = None,
) -> Iterator[None]:
    started = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        try:
            record_slow_operation(
                kind=kind,
                operation=operation,
                elapsed_ms=elapsed_ms,
                threshold_ms=threshold_ms,
                details=details,
            )
        except Exception as exc:
            record_local_fallback_event(
                module="advisory.performance_slowlog",
                fallback_type="performance_slowlog_context_record_failed",
                source=str(operation),
                severity="warn",
                reason="Slow-operation context manager could not record elapsed operation telemetry.",
                error=exc,
                metadata={
                    "kind": str(kind),
                    "operation": str(operation),
                    "elapsed_ms": round(float(elapsed_ms), 2),
                },
            )


def summarize_slow_operations(
    *,
    state_file: str | Path = DEFAULT_SLOW_STATE_FILE,
    status: str | None = "open",
    limit: int = 50,
) -> dict[str, Any]:
    state = load_slow_state(state_file)
    rows = list((state.get("issues") or {}).values())
    if status:
        rows = [row for row in rows if str(row.get("status") or "open") == status]
    rows.sort(key=lambda row: (float(row.get("max_elapsed_ms") or 0.0), str(row.get("last_seen_at") or "")), reverse=True)
    rows = rows[: max(int(limit), 0)]
    filtered_status = str(status or "").lower()
    return {
        "status": "warn" if filtered_status == "open" and rows else "ok",
        "state_file": str(state_file),
        "updated_at": state.get("updated_at"),
        "issue_count": len(state.get("issues") or {}),
        "returned_count": len(rows),
        "issues": rows,
    }


def update_slow_issue_status(
    fingerprint: str,
    *,
    status: str,
    note: str | None = None,
    state_file: str | Path = DEFAULT_SLOW_STATE_FILE,
) -> dict[str, Any]:
    if status not in {"open", "triaged", "fixed", "ignored"}:
        raise ValueError("status must be one of: open, triaged, fixed, ignored")
    state = load_slow_state(state_file)
    issues = state.setdefault("issues", {})
    issue = issues.get(fingerprint)
    if not isinstance(issue, dict):
        raise KeyError(f"Unknown slow issue fingerprint: {fingerprint}")
    issue["status"] = status
    issue["status_updated_at"] = _now_iso()
    if note:
        issue["status_note"] = note
    state["updated_at"] = _now_iso()
    save_slow_state(state, state_file)
    return issue


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect and manage Stockey slow-operation logs.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    report_p = subparsers.add_parser("report", help="Show deduped slow-operation issues.")
    report_p.add_argument("--status", default="open", help="Filter by status. Use empty string for all statuses.")
    report_p.add_argument("--limit", type=int, default=50)

    mark_p = subparsers.add_parser("mark", help="Mark a slow issue as triaged/fixed/ignored/open.")
    mark_p.add_argument("fingerprint")
    mark_p.add_argument("status", choices=["open", "triaged", "fixed", "ignored"])
    mark_p.add_argument("--note", default=None)

    args = parser.parse_args(argv)
    if args.command == "report":
        status = args.status if str(args.status).strip() else None
        print(json.dumps(summarize_slow_operations(status=status, limit=args.limit), indent=2, default=_json_default))
        return 0
    if args.command == "mark":
        print(json.dumps(update_slow_issue_status(args.fingerprint, status=args.status, note=args.note), indent=2, default=_json_default))
        return 0
    raise AssertionError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
