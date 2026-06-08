from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from advisory.operator_health import build_operator_health


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
    except Exception:
        pass
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
    visible_fix_hints = fix_hints[: max(0, int(fix_hint_limit))]
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
        "current_blockers": blocker_rows[: max(0, int(fix_hint_limit))],
        "fix_hints": visible_fix_hints,
        "next_commands": _next_commands(visible_fix_hints),
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
