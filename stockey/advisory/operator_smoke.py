from __future__ import annotations

import argparse
import json
from typing import Any

from environs import Env
import pandas as pd

from advisory.fallback_telemetry import record_local_fallback_event
from advisory.operator_health import build_operator_health


env = Env()
env.read_env()

OPERATOR_SMOKE_COMPACT_LIST_LIMIT = env.int("OPERATOR_SMOKE_COMPACT_LIST_LIMIT", default=8)
OPERATOR_SMOKE_COMPACT_STRING_CHARS = env.int("OPERATOR_SMOKE_COMPACT_STRING_CHARS", default=1_000)


def _json_ready(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    try:
        if pd.isna(value):
            return None
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.operator_smoke",
            fallback_type="operator_smoke_json_ready_missing_check_failed",
            source="operator_smoke_payload",
            severity="warn",
            reason="Operator smoke could not evaluate a value for missingness while preparing JSON output and kept the original value.",
            error=exc,
            metadata={"value_type": type(value).__name__},
        )
    return value


def _as_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _next_commands(fix_hints: list[dict[str, Any]], *, limit: int = 8) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for hint in fix_hints:
        for command in hint.get("commands") or []:
            text = str(command or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            out.append(text)
            if len(out) >= max(1, int(limit)):
                return out
    return out


def _compact_value(value: Any, *, list_limit: int, string_chars: int, stats: dict[str, int]) -> Any:
    ready = _json_ready(value)
    if isinstance(ready, str):
        if len(ready) > string_chars:
            stats["truncated_strings"] = int(stats.get("truncated_strings") or 0) + 1
            return ready[:string_chars] + "...[truncated]"
        return ready
    if isinstance(ready, list):
        compacted = [_compact_value(item, list_limit=list_limit, string_chars=string_chars, stats=stats) for item in ready[:list_limit]]
        omitted = max(0, len(ready) - list_limit)
        if omitted:
            stats["truncated_lists"] = int(stats.get("truncated_lists") or 0) + 1
            stats["omitted_list_items"] = int(stats.get("omitted_list_items") or 0) + omitted
        return compacted
    if isinstance(ready, dict):
        return {str(key): _compact_value(val, list_limit=list_limit, string_chars=string_chars, stats=stats) for key, val in ready.items()}
    return ready


def _compact_rows(rows: list[dict[str, Any]], *, limit: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    stats = {"truncated_lists": 0, "omitted_list_items": max(0, len(rows) - max(0, int(limit))), "truncated_strings": 0}
    visible = rows[: max(0, int(limit))]
    compacted = [
        _compact_value(
            row,
            list_limit=max(1, int(OPERATOR_SMOKE_COMPACT_LIST_LIMIT)),
            string_chars=max(200, int(OPERATOR_SMOKE_COMPACT_STRING_CHARS)),
            stats=stats,
        )
        for row in visible
    ]
    if stats["omitted_list_items"]:
        stats["truncated_lists"] += 1
    return compacted, stats


def build_operator_smoke(
    *,
    log_dir: str = "logs/cron",
    include_dhan: bool = False,
    fix_hint_limit: int = 8,
) -> dict[str, Any]:
    health = build_operator_health(log_dir=log_dir, include_dhan=include_dhan)
    sections = health.get("sections") if isinstance(health.get("sections"), dict) else {}
    trust_gate = sections.get("trust_gate") if isinstance(sections.get("trust_gate"), dict) else {}
    current_blockers = health.get("current_blockers") if isinstance(health.get("current_blockers"), dict) else {}
    fix_hints = _as_list(health.get("fix_hints"))
    blocker_rows = _as_list(current_blockers.get("rows"))
    dhan = sections.get("dhan") if isinstance(sections.get("dhan"), dict) else {}
    dhan_cache = sections.get("dhan_cache") if isinstance(sections.get("dhan_cache"), dict) else {}
    dhan_cdp = dhan_cache.get("cdp_status") if isinstance(dhan_cache.get("cdp_status"), dict) else {}
    visible_fix_hints, fix_hint_compact = _compact_rows(fix_hints, limit=max(0, int(fix_hint_limit)))
    visible_blockers, blocker_compact = _compact_rows(blocker_rows, limit=max(0, int(fix_hint_limit)))
    payload = {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "status": health.get("status") or "unknown",
        "trust_level": trust_gate.get("trust_level") or "unknown",
        "trust_status": trust_gate.get("status") or health.get("status") or "unknown",
        "recommendation": trust_gate.get("recommendation") or "Run operator health for details.",
        "read_only": True,
        "broker_execution_enabled": False,
        "checks": {
            "database": (sections.get("database") or {}).get("status") if isinstance(sections.get("database"), dict) else None,
            "operator_api": (sections.get("operator_api") or {}).get("status") if isinstance(sections.get("operator_api"), dict) else None,
            "frontend": (sections.get("frontend") or {}).get("status") if isinstance(sections.get("frontend"), dict) else None,
            "frontend_runtime": (sections.get("frontend_runtime") or {}).get("status") if isinstance(sections.get("frontend_runtime"), dict) else None,
            "dhan": dhan.get("status") if dhan else None,
            "dhan_cache": dhan_cache.get("status") if dhan_cache else None,
            "dhan_cdp": dhan_cdp.get("status") if dhan_cdp else None,
            "operator_snapshot": (sections.get("operator_snapshot") or {}).get("status") if isinstance(sections.get("operator_snapshot"), dict) else None,
            "signal_quality": (sections.get("signal_quality") or {}).get("status") if isinstance(sections.get("signal_quality"), dict) else None,
            "identity_issues": (sections.get("identity_issues") or {}).get("status") if isinstance(sections.get("identity_issues"), dict) else None,
            "cron_logs": "error"
            if any(row.get("status") == "error" for row in _as_list(sections.get("cron_logs")))
            else "warn"
            if any(row.get("status") == "warn" for row in _as_list(sections.get("cron_logs")))
            else "ok",
        },
        "counts": {
            "fix_hints": len(fix_hints),
            "current_blockers": int(current_blockers.get("count") or 0),
            "trust_checks": int(trust_gate.get("count") or 0),
            "trust_errors": int(trust_gate.get("error_count") or 0),
            "trust_warnings": int(trust_gate.get("warn_count") or 0),
        },
        "dhan_readiness": {
            "included_token_validation": bool(include_dhan),
            "token_validation_status": dhan.get("status") if dhan else None,
            "cache_status": dhan_cache.get("status") if dhan_cache else None,
            "auth_refresh_ready": dhan_cache.get("auth_refresh_ready") if dhan_cache else None,
            "auto_login_configured": dhan_cache.get("auto_login_configured") if dhan_cache else None,
            "cdp_status": dhan_cdp.get("status") if dhan_cdp else None,
            "cdp_recovery_required": bool(
                dhan_cache
                and dhan_cache.get("auto_login_configured")
                and not dhan_cache.get("auth_refresh_ready")
                and dhan_cdp.get("status") != "ok"
            ),
        },
        "current_blockers": visible_blockers,
        "fix_hints": visible_fix_hints,
        "next_commands": _next_commands(visible_fix_hints),
        "compact": True,
        "compact_meta": {
            "fix_hint_limit": max(0, int(fix_hint_limit)),
            "list_limit": max(1, int(OPERATOR_SMOKE_COMPACT_LIST_LIMIT)),
            "string_chars": max(200, int(OPERATOR_SMOKE_COMPACT_STRING_CHARS)),
            "fix_hints": fix_hint_compact,
            "current_blockers": blocker_compact,
        },
        "health_status": health.get("status"),
        "operator_health_generated_at": health.get("generated_at"),
    }
    return _json_ready(payload)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one compact, read-only Stockey operator smoke check.")
    parser.add_argument("--log-dir", default="logs/cron")
    parser.add_argument("--include-dhan", action="store_true", help="Validate Dhan token too. Default skips Dhan to avoid login side effects.")
    parser.add_argument("--fix-hint-limit", type=int, default=8)
    parser.add_argument("--format", choices=["json", "text"], default="json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = build_operator_smoke(
        log_dir=str(args.log_dir),
        include_dhan=bool(args.include_dhan),
        fix_hint_limit=max(1, int(args.fix_hint_limit)),
    )
    if args.format == "text":
        print(f"status={payload.get('status')} trust_level={payload.get('trust_level')}")
        print(payload.get("recommendation") or "")
        for command in payload.get("next_commands") or []:
            print(f"- {command}")
    else:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return 1 if payload.get("trust_status") == "error" or payload.get("status") == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
