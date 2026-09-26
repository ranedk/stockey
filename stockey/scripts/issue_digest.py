"""Digest of recorded-but-unread issues: open identity-mapping issues
(utils/identity_issues.py) and recent fallback/degraded-path telemetry
(utils/fallback_telemetry.py).

Both write paths are live and mandated by CLAUDE.md's "no silent fallback" rule --
every degraded source, mapping miss, and fallback path records itself instead of
failing silently. But neither had a reader wired into any scheduled job (confirmed
live 2026-08-14, docs/DATA_COVERAGE.md's "Still open" list): issues accumulated with
no scheduled review, the same failure shape as the go-crond incident (recorded, never
read) just one layer up. This script is that missing reader.

Two halves:
  1. identity_issues -- rechecks every open advisory_identity_issues row and closes
     whatever now resolves cleanly (resolve_open_identity_issues(apply=True) only ever
     marks an issue resolved when a live recheck confirms the underlying mapping/
     history now exists; it never invents data), then reports what's left.
  2. fallback_events  -- a 24h summarize_fallback_events() snapshot (read-only).

Run standalone or via `all_issue_digest.sh`.
"""
from __future__ import annotations

import argparse
import json
from typing import Any

from utils.fallback_telemetry import summarize_fallback_events
from utils.identity_issues import load_open_identity_issues, resolve_open_identity_issues

STOCKEY_RUN_STATE: dict[str, object] = {}

FALLBACK_WINDOW_HOURS = 24
IDENTITY_ISSUE_RECHECK_LIMIT = 200
# A handful of in-flight identity mappings (new listings, freshly delisted symbols) is
# normal churn, not a data-quality incident -- only escalate past "warn" once open
# issues pile up past what routine churn would explain. Tune alongside universe size.
IDENTITY_ISSUE_ERROR_THRESHOLD = 25


def build_identity_issue_report(*, limit: int = IDENTITY_ISSUE_RECHECK_LIMIT, apply: bool = True) -> dict[str, Any]:
    recheck = resolve_open_identity_issues(limit=limit, apply=apply)
    remaining = load_open_identity_issues(limit=limit)
    still_open = 0 if remaining.empty else len(remaining)
    by_type: dict[str, int] = {}
    if not remaining.empty and "issue_type" in remaining.columns:
        for issue_type, count in remaining["issue_type"].fillna("unknown").value_counts().items():
            by_type[str(issue_type)] = int(count)
    status = "ok" if still_open == 0 else ("error" if still_open > IDENTITY_ISSUE_ERROR_THRESHOLD else "warn")
    return {
        "status": status,
        "recheck_mode": recheck["mode"],
        "recheck_counts": recheck["counts"],
        "still_open": still_open,
        "still_open_by_type": by_type,
    }


def build_report(*, hours: int = FALLBACK_WINDOW_HOURS, apply: bool = True) -> dict[str, Any]:
    identity = build_identity_issue_report(apply=apply)
    fallback = summarize_fallback_events(hours=hours)
    statuses = {identity["status"], fallback["status"]}
    overall = "error" if "error" in statuses else "warn" if "warn" in statuses else "ok"
    return {"overall": overall, "identity_issues": identity, "fallback_events": fallback}


def format_text_report(report: dict[str, Any]) -> str:
    identity = report["identity_issues"]
    fallback = report["fallback_events"]
    return "\n".join([
        f"[issue_digest] overall={report['overall']}",
        f"[issue_digest] identity_issues status={identity['status']} still_open={identity['still_open']} "
        f"recheck={identity['recheck_mode']} recheck_counts={identity['recheck_counts']} by_type={identity['still_open_by_type']}",
        f"[issue_digest] fallback_events status={fallback['status']} window_hours={fallback['window_hours']} "
        f"active={fallback['active_count']} errors={fallback['error_count']} warns={fallback['warn_count']} "
        f"by_type={fallback['counts_by_type']}",
    ])


def main(argv: list[str] | None = None) -> int:
    global STOCKEY_RUN_STATE
    parser = argparse.ArgumentParser(
        description="Digest of open identity issues and recent fallback telemetry -- the "
        "reader half of CLAUDE.md's 'no silent fallback' write paths."
    )
    parser.add_argument("--hours", type=int, default=FALLBACK_WINDOW_HOURS, help="Fallback telemetry lookback window.")
    parser.add_argument(
        "--no-apply", action="store_true",
        help="Skip auto-closing identity issues that have since resolved themselves (report only).",
    )
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--require", action="store_true", help="Exit 1 if overall status is error.")
    args = parser.parse_args(argv)

    report = build_report(hours=max(1, int(args.hours)), apply=not args.no_apply)
    STOCKEY_RUN_STATE = report

    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, default=str))
    else:
        print(format_text_report(report))

    if args.require and report["overall"] == "error":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
