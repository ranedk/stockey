from __future__ import annotations

import os
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from utils.fallback_telemetry import record_local_fallback_event


DEFAULT_CRONTAB_PATH = Path("config/stockey.generated.crontab")
DEFAULT_LOG_DIR = Path("logs/cron")
DEFAULT_STALE_LOCK_SECONDS = 6 * 60 * 60
CRON_FIELD_COUNT = 5


def _text(value: Any) -> str:
    return str(value or "").strip()


def _expand_field(field: str, minimum: int, maximum: int) -> set[int]:
    values: set[int] = set()
    text = _text(field)
    if not text or text == "*":
        return set(range(minimum, maximum + 1))
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            base, step_text = part.split("/", 1)
            part = base or "*"
            try:
                step = max(1, int(step_text))
            except ValueError as exc:
                record_local_fallback_event(
                    module="utils.cron_status",
                    fallback_type="cron_status_invalid_step_fallback",
                    source="crontab",
                    severity="warn",
                    reason="Cron status could not parse a step expression and defaulted the step to 1.",
                    error=exc,
                    metadata={"field": text, "part": base, "step_text": step_text},
                )
                step = 1
        if part == "*":
            start, end = minimum, maximum
        elif "-" in part:
            start_text, end_text = part.split("-", 1)
            start, end = int(start_text), int(end_text)
        else:
            value = int(part)
            start, end = value, value
        values.update(v for v in range(max(minimum, start), min(maximum, end) + 1, step))
    return values


def _cron_matches(dt: pd.Timestamp, fields: list[str]) -> bool:
    minute, hour, dom, month, dow = fields
    weekday_cron = (int(dt.dayofweek) + 1) % 7
    return (
        int(dt.minute) in _expand_field(minute, 0, 59)
        and int(dt.hour) in _expand_field(hour, 0, 23)
        and int(dt.day) in _expand_field(dom, 1, 31)
        and int(dt.month) in _expand_field(month, 1, 12)
        and weekday_cron in _expand_field(dow, 0, 7)
    )


def estimate_next_run(fields: list[str], *, now: pd.Timestamp | None = None, horizon_days: int = 14) -> str | None:
    # BUG FOUND LIVE 2026-08-20 (re-audit): this used to compare an IST wall-clock `now`
    # directly against the crontab's minute/hour/dom/month/dow fields -- but per CLAUDE.md
    # ("TIMEZONE" note in config/stockey.crontab.template, confirmed empirically 2026-08-13),
    # go-crond has no CRON_TZ/TZ support, so those fields are always written in UTC (IST -
    # 5:30), never IST. A job scheduled "40 01" (01:40 UTC = 07:10 IST) was being matched
    # against IST 01:40 instead -- off by 5.5 hours for every single job, every call.
    # Matching now happens in UTC (what the fields actually mean); the returned estimate is
    # converted back to IST since that's the wall-clock a human operator actually reads.
    effective_now = pd.to_datetime(now or pd.Timestamp.now(tz="Asia/Kolkata"))
    if effective_now.tzinfo is None:
        effective_now = effective_now.tz_localize("Asia/Kolkata")
    effective_now_utc = effective_now.tz_convert("UTC")
    cursor = effective_now_utc.floor("min") + pd.Timedelta(minutes=1)
    end = effective_now_utc + pd.Timedelta(days=max(1, int(horizon_days)))
    while cursor <= end:
        if _cron_matches(cursor, fields):
            return cursor.tz_convert("Asia/Kolkata").isoformat()
        cursor += pd.Timedelta(minutes=1)
    return None


def _extract_redirect_log(command: str) -> str | None:
    match = re.search(r'>>\s+"?\$LOG_DIR/([^"\s]+)"?', command)
    return match.group(1) if match else None


def _extract_lock_file(command: str) -> str | None:
    match = re.search(r"with_lock\.sh\"\s+([^\s;]+)|with_lock\.sh\s+([^\s;]+)", command)
    if match:
        return (match.group(1) or match.group(2) or "").strip('"')
    return None


def _job_name_from_command(command: str, log_file: str | None) -> str:
    if log_file:
        return Path(log_file).stem
    for match in re.finditer(r"(\./[A-Za-z0-9_\-.]+\.sh|advisory\.[A-Za-z0-9_]+)", command):
        return match.group(1).replace("./", "").replace(".sh", "").replace("advisory.", "")
    return "cron_job"


def parse_crontab(path: str | Path = DEFAULT_CRONTAB_PATH) -> list[dict[str, Any]]:
    crontab_path = Path(path)
    if not crontab_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_no, raw_line in enumerate(crontab_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" in line.split()[0]:
            continue
        parts = line.split(maxsplit=6)
        if len(parts) < 7:
            continue
        fields = parts[:CRON_FIELD_COUNT]
        user = parts[CRON_FIELD_COUNT]
        command = parts[CRON_FIELD_COUNT + 1]
        log_file = _extract_redirect_log(command)
        lock_file = _extract_lock_file(command)
        rows.append(
            {
                "line_no": line_no,
                "job_name": _job_name_from_command(command, log_file),
                "schedule": " ".join(fields),
                "cron_fields": fields,
                "user": user,
                "command": command,
                "log_file": log_file,
                "lock_file": lock_file,
            }
        )
    return rows


def _tail(path: Path, lines: int) -> list[str]:
    if not path.exists() or not path.is_file():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()[-max(0, int(lines)) :]


def _latest_marker(lines: list[str]) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    for line in lines:
        if "[stockey.script]" not in line:
            continue
        parts: dict[str, str] = {}
        for token in line.split():
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            parts[key] = value
        if parts.get("status"):
            latest = parts
    return latest


def _pid_running(pid_text: str | None) -> bool:
    try:
        pid = int(str(pid_text or "").strip())
    except ValueError as exc:
        record_local_fallback_event(
            module="utils.cron_status",
            fallback_type="cron_status_invalid_pid_file",
            source="cron_lock_pid",
            severity="warn",
            reason="Cron lock PID file did not contain a valid process id; lock may be treated as stale.",
            error=exc,
            metadata={"pid_text": str(pid_text or "")[:120]},
        )
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if isinstance(exc, ProcessLookupError):
            return False
        record_local_fallback_event(
            module="utils.cron_status",
            fallback_type="cron_status_pid_check_failed",
            source="cron_lock_pid",
            severity="warn",
            reason="Cron lock PID liveness check failed; lock status may be unreliable.",
            error=exc,
            metadata={"pid": pid},
        )
        return False
    return True


def inspect_lock(lock_file: str | None, *, now: pd.Timestamp | None = None, stale_after_seconds: int = DEFAULT_STALE_LOCK_SECONDS) -> dict[str, Any]:
    if not lock_file:
        return {"lock_file": None, "lock_active": False, "lock_stale": False}
    lock_dir = Path(f"{lock_file}.d")
    if not lock_dir.exists():
        return {"lock_file": lock_file, "lock_dir": str(lock_dir), "lock_active": False, "lock_stale": False}
    effective_now = pd.to_datetime(now or pd.Timestamp.utcnow(), utc=True)
    stat = lock_dir.stat()
    modified_at = pd.to_datetime(stat.st_mtime, unit="s", utc=True)
    age_seconds = max(0.0, (effective_now - modified_at).total_seconds())
    pid_path = lock_dir / "pid"
    command_path = lock_dir / "command"
    pid_text = pid_path.read_text(encoding="utf-8", errors="replace").strip() if pid_path.exists() else None
    running = _pid_running(pid_text)
    stale = bool(age_seconds > max(60, int(stale_after_seconds)) or (pid_text and not running))
    return {
        "lock_file": lock_file,
        "lock_dir": str(lock_dir),
        "lock_active": True,
        "lock_stale": stale,
        "lock_pid": pid_text,
        "lock_pid_running": running,
        "lock_command": command_path.read_text(encoding="utf-8", errors="replace").strip() if command_path.exists() else None,
        "lock_modified_at": modified_at.isoformat(),
        "lock_age_seconds": age_seconds,
    }


def build_cron_status(
    *,
    crontab_path: str | Path = DEFAULT_CRONTAB_PATH,
    log_dir: str | Path = DEFAULT_LOG_DIR,
    now: pd.Timestamp | None = None,
    tail_lines: int = 12,
    stale_lock_seconds: int = DEFAULT_STALE_LOCK_SECONDS,
) -> dict[str, Any]:
    effective_now = pd.to_datetime(now or pd.Timestamp.now(tz="Asia/Kolkata"))
    if effective_now.tzinfo is None:
        effective_now = effective_now.tz_localize("Asia/Kolkata")
    root = Path(log_dir)
    jobs: list[dict[str, Any]] = []
    for row in parse_crontab(crontab_path):
        log_path = root / row["log_file"] if row.get("log_file") else None
        tail = _tail(log_path, tail_lines) if log_path else []
        marker = _latest_marker(tail)
        log_modified_at = None
        log_age_seconds = None
        if log_path and log_path.exists():
            modified = pd.to_datetime(log_path.stat().st_mtime, unit="s", utc=True)
            log_modified_at = modified.isoformat()
            log_age_seconds = max(0.0, (pd.Timestamp.utcnow() - modified).total_seconds())
        lower_tail = "\n".join(tail).lower()
        status = "ok"
        if marker.get("status") == "failed" or "traceback" in lower_tail:
            status = "error"
        elif marker.get("status") == "interrupted" or "error" in lower_tail:
            status = "warning"
        lock = inspect_lock(row.get("lock_file"), now=pd.Timestamp.utcnow(), stale_after_seconds=stale_lock_seconds)
        if lock.get("lock_stale"):
            status = "error"
        elif lock.get("lock_active") and status == "ok":
            status = "running"
        jobs.append(
            {
                **row,
                "status": status,
                "next_run_estimate": estimate_next_run(row["cron_fields"], now=effective_now),
                "log_path": str(log_path) if log_path else None,
                "log_exists": bool(log_path and log_path.exists()),
                "last_log_at": log_modified_at,
                "log_age_seconds": log_age_seconds,
                "latest_marker": marker,
                "latest_run_status": marker.get("status") or status,
                "latest_duration_seconds": marker.get("elapsed"),
                "tail": tail,
                **lock,
            }
        )
    counts: dict[str, int] = {}
    for row in jobs:
        counts[str(row.get("status") or "unknown")] = counts.get(str(row.get("status") or "unknown"), 0) + 1
    return {
        "status": "error" if counts.get("error") else ("warning" if counts.get("warning") else "ok"),
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "crontab_path": str(crontab_path),
        "log_dir": str(log_dir),
        "counts": counts,
        "jobs": jobs,
    }
