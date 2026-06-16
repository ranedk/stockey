from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
from typing import Any

from advisory.fallback_telemetry import record_local_fallback_event


DEFAULT_LOG_PATH = Path("logs/cron/all_advisory.log")
DEFAULT_STAGE_BUDGET_SECONDS = float(os.getenv("ADVISORY_STAGE_BUDGET_SECONDS", "900") or 0)
DEFAULT_STAGE_BUDGET_OVERRIDES = os.getenv("ADVISORY_STAGE_BUDGET_OVERRIDES", "")
SCRIPT_START_RE = re.compile(r"\[stockey\.script\]\s+name=all_advisory\s+status=start\s+timestamp=(?P<ts>\S+)")
SCRIPT_DONE_RE = re.compile(r"\[stockey\.script\]\s+name=all_advisory\s+status=(?P<status>done|failed)\s+exit_code=(?P<exit_code>-?\d+)\s+timestamp=(?P<ts>\S+)")
STAGE_START_RE = re.compile(r"\[advisory\.pipeline\]\s+stage=(?P<stage>[A-Za-z0-9_]+)\s+start")
STAGE_RUNNING_RE = re.compile(r"\[advisory\.pipeline\]\s+stage=(?P<stage>[A-Za-z0-9_]+)\s+running\s+elapsed=(?P<elapsed>[0-9.]+)s")
STAGE_DONE_RE = re.compile(r"\[advisory\.pipeline\]\s+stage=(?P<stage>[A-Za-z0-9_]+)\s+done\s+elapsed=(?P<elapsed>[0-9.]+)s(?P<detail>.*)")
MASTER_DONE_RE = re.compile(r"\[advisory\.master_pipeline\]\s+advisory run done\s+elapsed=(?P<elapsed>[0-9.]+)s\s+stages=(?P<stages>\d+)")


def _record_stage_report_fallback(
    *,
    fallback_type: str,
    reason: str,
    error: Exception | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="scripts.advisory_stage_report",
        source="advisory_stage_report",
        fallback_type=fallback_type,
        severity="warn",
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def _read_tail(path: Path, *, max_bytes: int) -> str:
    if not path.exists():
        return ""
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
    except OSError as exc:
        _record_stage_report_fallback(
            fallback_type="advisory_stage_report_log_read_failed",
            reason="Advisory stage report could not read the requested log file.",
            error=exc,
            metadata={"path": str(path), "max_bytes": max_bytes},
        )
        return ""
    return data.decode("utf-8", errors="replace")


def _candidate_json_objects(text: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    parse_failures_recorded = 0
    for idx, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError as exc:
            if parse_failures_recorded < 3:
                _record_stage_report_fallback(
                    fallback_type="advisory_stage_report_candidate_json_parse_failed",
                    reason="Advisory stage report skipped a non-JSON brace while scanning mixed log text for the final summary.",
                    error=exc,
                    metadata={"offset": idx},
                )
                parse_failures_recorded += 1
            continue
        if isinstance(value, dict):
            objects.append(value)
    return objects


def extract_latest_stage_summary(text: str) -> dict[str, Any]:
    candidates = [
        item
        for item in _candidate_json_objects(text)
        if isinstance(item.get("stage_timings"), list) and isinstance(item.get("stage_budget"), dict)
    ]
    return candidates[-1] if candidates else {}


def _parse_stage_budget_overrides(value: Any) -> dict[str, float]:
    out: dict[str, float] = {}
    if not value:
        return out
    for part in str(value).split(","):
        if "=" not in part:
            continue
        stage, seconds = part.split("=", 1)
        stage = str(stage or "").strip()
        try:
            parsed = float(seconds)
        except ValueError as exc:
            _record_stage_report_fallback(
                fallback_type="advisory_stage_report_budget_override_parse_failed",
                reason="Advisory stage report ignored a malformed stage-budget override.",
                error=exc,
                metadata={"override": part[:120], "stage": stage},
            )
            continue
        if stage and parsed > 0:
            out[stage] = parsed
    return out


def _stage_budget_seconds(stage: str, *, default_budget_seconds: float, overrides: dict[str, float]) -> float | None:
    if stage in overrides:
        return overrides[stage]
    return default_budget_seconds if default_budget_seconds > 0 else None


def _latest_run_text(text: str) -> str:
    matches = list(SCRIPT_START_RE.finditer(text))
    if not matches:
        return text
    return text[matches[-1].start() :]


def _detail_from_done_suffix(suffix: str) -> str | None:
    detail = str(suffix or "").strip()
    if not detail:
        return None
    return detail[:500]


def _summary_from_stage_markers(text: str) -> dict[str, Any]:
    run_text = _latest_run_text(text)
    default_budget_seconds = DEFAULT_STAGE_BUDGET_SECONDS
    overrides = _parse_stage_budget_overrides(DEFAULT_STAGE_BUDGET_OVERRIDES)
    stages: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    pipeline_status = "running"
    exit_code = None
    completed_at = None
    started_at = None
    master_elapsed = None

    for line in run_text.splitlines():
        if match := SCRIPT_START_RE.search(line):
            started_at = match.group("ts")
            pipeline_status = "running"
            continue
        if match := SCRIPT_DONE_RE.search(line):
            pipeline_status = match.group("status")
            exit_code = int(match.group("exit_code"))
            completed_at = match.group("ts")
            continue
        if match := MASTER_DONE_RE.search(line):
            master_elapsed = _num(match.group("elapsed"))
            continue
        if match := STAGE_START_RE.search(line):
            stage = match.group("stage")
            if stage not in stages:
                order.append(stage)
            stages[stage] = {
                "stage": stage,
                "elapsed_seconds": 0.0,
                "status": "started",
                "budget_seconds": _stage_budget_seconds(stage, default_budget_seconds=default_budget_seconds, overrides=overrides),
                "detail": None,
                "source": "log_marker",
            }
            continue
        if match := STAGE_RUNNING_RE.search(line):
            stage = match.group("stage")
            if stage not in stages:
                order.append(stage)
                stages[stage] = {
                    "stage": stage,
                    "budget_seconds": _stage_budget_seconds(stage, default_budget_seconds=default_budget_seconds, overrides=overrides),
                    "detail": None,
                    "source": "log_marker",
                }
            stages[stage]["elapsed_seconds"] = round(_num(match.group("elapsed")), 4)
            stages[stage]["status"] = "running"
            continue
        if match := STAGE_DONE_RE.search(line):
            stage = match.group("stage")
            if stage not in stages:
                order.append(stage)
            elapsed = _num(match.group("elapsed"))
            budget = _stage_budget_seconds(stage, default_budget_seconds=default_budget_seconds, overrides=overrides)
            stages[stage] = {
                "stage": stage,
                "elapsed_seconds": round(elapsed, 4),
                "budget_seconds": None if budget is None else round(float(budget), 4),
                "over_budget": bool(budget is not None and elapsed > budget),
                "detail": _detail_from_done_suffix(match.group("detail")),
                "status": "done",
                "source": "log_marker",
            }

    if not stages:
        return {}
    rows = []
    for stage in order:
        row = stages[stage]
        elapsed = _num(row.get("elapsed_seconds"))
        budget = row.get("budget_seconds")
        row["over_budget"] = bool(budget is not None and elapsed > float(budget))
        rows.append(row)
    slow_rows = [row for row in rows if row.get("over_budget")]
    total_elapsed = sum(_num(row.get("elapsed_seconds")) for row in rows)
    return {
        "status": "ok" if pipeline_status == "done" else pipeline_status,
        "exit_code": exit_code,
        "started_at": started_at,
        "completed_at": completed_at,
        "asof_date": None,
        "stage_timings": rows,
        "slow_stages": slow_rows,
        "failed_stage": next((row.get("stage") for row in reversed(rows) if row.get("status") != "done"), None) if pipeline_status == "failed" else None,
        "stage_budget": {
            "default_budget_seconds": None if default_budget_seconds <= 0 else round(float(default_budget_seconds), 4),
            "active_budget_seconds": None if default_budget_seconds <= 0 else round(float(default_budget_seconds), 4),
            "overrides": overrides,
            "slow_stage_count": len(slow_rows),
            "measured_stage_count": len(rows),
            "total_measured_elapsed_seconds": round(total_elapsed, 4),
            "master_elapsed_seconds": master_elapsed,
            "reporting_only": True,
            "source": "log_markers",
        },
    }


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        if value not in (None, ""):
            _record_stage_report_fallback(
                fallback_type="advisory_stage_report_numeric_parse_failed",
                reason="Advisory stage report could not parse a numeric stage value and used zero for ranking.",
                error=exc,
                metadata={"value": str(value)[:120]},
            )
        return 0.0


def build_stage_report(
    *,
    log_path: str | Path = DEFAULT_LOG_PATH,
    max_bytes: int = 5_000_000,
    limit: int = 20,
) -> dict[str, Any]:
    resolved = Path(log_path)
    text = _read_tail(resolved, max_bytes=max(1, int(max_bytes)))
    summary = extract_latest_stage_summary(text)
    summary_source = "json_summary"
    if not summary:
        summary = _summary_from_stage_markers(text)
        summary_source = "log_markers" if summary else "missing"
    timings = summary.get("stage_timings") if isinstance(summary.get("stage_timings"), list) else []
    timing_rows = [row for row in timings if isinstance(row, dict)]
    ranked = sorted(timing_rows, key=lambda row: _num(row.get("elapsed_seconds")), reverse=True)
    slow = [row for row in timing_rows if row.get("over_budget")]
    bounded_limit = max(1, min(int(limit), 200))
    pipeline_status = str(summary.get("status") or "") if summary else ""
    status = pipeline_status if pipeline_status in {"failed", "running"} else "ok" if summary else "missing"
    return {
        "status": status,
        "log_path": str(resolved),
        "max_bytes": int(max_bytes),
        "pipeline_status": summary.get("status") if summary else None,
        "exit_code": summary.get("exit_code") if summary else None,
        "started_at": summary.get("started_at") if summary else None,
        "completed_at": summary.get("completed_at") if summary else None,
        "failed_stage": summary.get("failed_stage") if summary else None,
        "summary_source": summary_source,
        "asof_date": summary.get("asof_date") if summary else None,
        "stage_budget": summary.get("stage_budget") if summary else {},
        "slow_stages": slow,
        "ranked_stages": ranked[:bounded_limit],
        "stage_count": len(timing_rows),
        "slow_stage_count": len(slow),
        "operator_action": (
            "Review slow stages and move external/source repair work to queues or add targeted query/index fixes."
            if slow
            else f"Latest advisory run failed in or near stage `{summary.get('failed_stage')}`; inspect the traceback below that stage marker."
            if summary and summary.get("status") == "failed"
            else "No over-budget stages found in the latest parsed advisory summary."
            if summary
            else "No advisory stage summary found. Run all_advisory.sh after the stage-budget instrumentation is deployed, or increase --max-bytes."
        ),
    }


def _format_text(report: dict[str, Any]) -> str:
    lines = [
        f"status={report.get('status')} log={report.get('log_path')} stages={report.get('stage_count', 0)} slow={report.get('slow_stage_count', 0)}",
        f"operator_action={report.get('operator_action')}",
    ]
    budget = report.get("stage_budget") if isinstance(report.get("stage_budget"), dict) else {}
    if budget:
        lines.append(
            "budget="
            f"default={budget.get('default_budget_seconds')}s "
            f"active={budget.get('active_budget_seconds')}s "
            f"overrides={budget.get('overrides') or {}}"
        )
    rows = report.get("ranked_stages") if isinstance(report.get("ranked_stages"), list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        marker = "SLOW" if row.get("over_budget") else "ok"
        lines.append(
            f"{marker} stage={row.get('stage')} elapsed={row.get('elapsed_seconds')}s "
            f"budget={row.get('budget_seconds')}s detail={row.get('detail') or ''}"
        )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract advisory pipeline stage-budget timing from a mixed cron/stdout log.")
    parser.add_argument("--log-path", default=str(DEFAULT_LOG_PATH))
    parser.add_argument("--max-bytes", type=int, default=5_000_000)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--format", choices=["text", "json"], default="text")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_stage_report(log_path=args.log_path, max_bytes=args.max_bytes, limit=args.limit)
    if args.format == "json":
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        print(_format_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
