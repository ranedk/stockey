from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from advisory.fallback_telemetry import record_local_fallback_event
from advisory.performance_slowlog import DEFAULT_SLOW_STATE_FILE, load_slow_state
from scripts.api_latency_probe import DEFAULT_OUTPUT_PATH as DEFAULT_PROBE_PATH


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _record_api_performance_fallback(
    *,
    fallback_type: str,
    reason: str,
    error: Exception | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="scripts.api_performance_report",
        source="api_performance_report",
        fallback_type=fallback_type,
        severity="warn",
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def _read_json(path: str | Path) -> dict[str, Any]:
    resolved = Path(path)
    if not resolved.exists():
        return {}
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _record_api_performance_fallback(
            fallback_type="api_performance_probe_json_parse_failed",
            reason="API performance report could not parse the latest probe JSON; using slowlog-only evidence.",
            error=exc,
            metadata={"path": str(resolved)},
        )
        return {}
    return payload if isinstance(payload, dict) else {}


def _route_from_issue(issue: dict[str, Any]) -> str:
    details = issue.get("last_details") if isinstance(issue.get("last_details"), dict) else {}
    route = details.get("route") or details.get("endpoint")
    if route:
        return str(route).split("?", 1)[0]
    operation = str(issue.get("operation") or "").strip()
    if operation.upper().startswith("GET "):
        return operation.split(" ", 1)[1].split("?", 1)[0]
    return operation or "unknown"


def _int_or_none(value: Any, *, field: str | None = None, source: str | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        if value not in (None, ""):
            _record_api_performance_fallback(
                fallback_type="api_performance_numeric_parse_failed",
                reason="API performance report could not parse a numeric field; ignoring that value.",
                error=exc,
                metadata={"field": field, "source": source, "value": str(value)},
            )
        return None


def _route_recommendation(route: str, *, max_response_bytes: int, error_count: int, max_elapsed_ms: float) -> str:
    if error_count > 0:
        return "Fix endpoint errors first; latency data is not trustworthy while responses fail."
    if max_response_bytes >= 250_000:
        return "Split or compact this payload before adding indexes."
    if route in {"/api/health/details", "/api/data-health"}:
        return "Keep default response lightweight; move expensive diagnostics behind full/debug mode."
    if route in {"/api/actions", "/api/portfolio", "/api/events", "/api/manual-review"}:
        return "Prefer section snapshots, pagination, compact rows, and detail-only drilldowns."
    if max_elapsed_ms >= 2_000:
        return "Inspect query plan and add targeted indexes only after confirming the slow query."
    return "Monitor; optimize only if this stays above threshold in fresh probe data."


def build_api_performance_report(
    *,
    probe_path: str | Path = DEFAULT_PROBE_PATH,
    state_file: str | Path = DEFAULT_SLOW_STATE_FILE,
    status: str | None = "open",
    limit: int = 20,
) -> dict[str, Any]:
    probe = _read_json(probe_path)
    state = load_slow_state(state_file)
    route_rows: dict[str, dict[str, Any]] = {}

    for issue in (state.get("issues") or {}).values():
        if not isinstance(issue, dict):
            continue
        if status and str(issue.get("status") or "open") != status:
            continue
        route = _route_from_issue(issue)
        details = issue.get("last_details") if isinstance(issue.get("last_details"), dict) else {}
        row = route_rows.setdefault(
            route,
            {
                "route": route,
                "issue_count": 0,
                "occurrence_count": 0,
                "max_elapsed_ms": 0.0,
                "latest_elapsed_ms": None,
                "max_response_bytes": 0,
                "error_count": 0,
                "kinds": [],
                "fingerprints": [],
                "last_seen_at": None,
                "latest_probe_elapsed_ms": None,
                "latest_probe_error": None,
                "latest_probe_status_code": None,
            },
        )
        row["issue_count"] += 1
        row["occurrence_count"] += int(issue.get("count") or 0)
        row["max_elapsed_ms"] = max(float(row["max_elapsed_ms"] or 0.0), float(issue.get("max_elapsed_ms") or 0.0))
        row["latest_elapsed_ms"] = issue.get("last_elapsed_ms")
        row["last_seen_at"] = max(str(row.get("last_seen_at") or ""), str(issue.get("last_seen_at") or "")) or None
        fingerprint = issue.get("fingerprint")
        if fingerprint:
            row["fingerprints"].append(str(fingerprint))
        kind = str(issue.get("kind") or "")
        if kind and kind not in row["kinds"]:
            row["kinds"].append(kind)
        response_bytes = details.get("response_bytes", details.get("body_bytes", 0))
        parsed_response_bytes = _int_or_none(response_bytes, field="response_bytes", source=route)
        if parsed_response_bytes is not None:
            row["max_response_bytes"] = max(int(row["max_response_bytes"] or 0), parsed_response_bytes)
        status_code = _int_or_none(details.get("status_code"), field="status_code", source=route)
        if details.get("error") or (status_code is not None and status_code >= 400):
            row["error_count"] += int(issue.get("count") or 1)

    for probe_row in probe.get("rows") or []:
        if not isinstance(probe_row, dict):
            continue
        route = str(probe_row.get("endpoint") or "").split("?", 1)[0] or "unknown"
        row = route_rows.setdefault(
            route,
            {
                "route": route,
                "issue_count": 0,
                "occurrence_count": 0,
                "max_elapsed_ms": 0.0,
                "latest_elapsed_ms": None,
                "max_response_bytes": 0,
                "error_count": 0,
                "kinds": [],
                "fingerprints": [],
                "last_seen_at": None,
                "latest_probe_elapsed_ms": None,
                "latest_probe_error": None,
                "latest_probe_status_code": None,
            },
        )
        elapsed = float(probe_row.get("elapsed_ms") or 0.0)
        row["latest_probe_elapsed_ms"] = elapsed
        row["latest_probe_error"] = probe_row.get("error")
        row["latest_probe_status_code"] = probe_row.get("status_code")
        row["max_elapsed_ms"] = max(float(row["max_elapsed_ms"] or 0.0), elapsed)
        parsed_body_bytes = _int_or_none(probe_row.get("body_bytes"), field="body_bytes", source=route)
        if parsed_body_bytes is not None:
            row["max_response_bytes"] = max(int(row["max_response_bytes"] or 0), parsed_body_bytes)
        status_code = _int_or_none(probe_row.get("status_code"), field="status_code", source=route)
        if probe_row.get("error") or (status_code is not None and status_code >= 400):
            row["error_count"] += 1

    rows = []
    for row in route_rows.values():
        row["priority_score"] = round(
            float(row.get("max_elapsed_ms") or 0.0)
            + min(int(row.get("occurrence_count") or 0), 100) * 25.0
            + min(int(row.get("max_response_bytes") or 0), 2_000_000) / 1000.0
            + int(row.get("error_count") or 0) * 1000.0,
            2,
        )
        row["recommendation"] = _route_recommendation(
            str(row["route"]),
            max_response_bytes=int(row.get("max_response_bytes") or 0),
            error_count=int(row.get("error_count") or 0),
            max_elapsed_ms=float(row.get("max_elapsed_ms") or 0.0),
        )
        rows.append(row)
    rows.sort(key=lambda item: (float(item.get("priority_score") or 0.0), str(item.get("last_seen_at") or "")), reverse=True)
    rows = rows[: max(int(limit), 0)]

    return {
        "status": "warn" if rows else "ok",
        "probe_path": str(probe_path),
        "state_file": str(state_file),
        "probe_generated_at": probe.get("generated_at"),
        "slow_state_updated_at": state.get("updated_at"),
        "route_count": len(route_rows),
        "returned_count": len(rows),
        "rows": rows,
    }


def _format_text(report: dict[str, Any]) -> str:
    lines = [
        f"status: {report.get('status')}",
        f"probe_generated_at: {report.get('probe_generated_at') or 'missing'}",
        f"slow_state_updated_at: {report.get('slow_state_updated_at') or 'missing'}",
        f"returned_count: {report.get('returned_count', 0)}",
    ]
    for idx, row in enumerate(report.get("rows") or [], start=1):
        lines.append(
            f"{idx}. {row.get('route')} score={row.get('priority_score')} max_ms={row.get('max_elapsed_ms')} "
            f"count={row.get('occurrence_count')} bytes={row.get('max_response_bytes')} errors={row.get('error_count')}"
        )
        lines.append(f"   recommendation: {row.get('recommendation')}")
        fingerprints = row.get("fingerprints") or []
        if fingerprints:
            lines.append(f"   fingerprints: {', '.join(fingerprints[:5])}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rank operator API performance issues from latest probe and slow-operation state.")
    parser.add_argument("--probe-path", default=str(DEFAULT_PROBE_PATH))
    parser.add_argument("--state-file", default=str(DEFAULT_SLOW_STATE_FILE))
    parser.add_argument("--status", default="open", help="Slow issue status filter. Use empty string for all statuses.")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--format", choices=["json", "text"], default="text")
    args = parser.parse_args(argv)
    report = build_api_performance_report(
        probe_path=args.probe_path,
        state_file=args.state_file,
        status=args.status if str(args.status).strip() else None,
        limit=int(args.limit),
    )
    if args.format == "json":
        print(json.dumps(report, indent=2, default=_json_default))
    else:
        print(_format_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
