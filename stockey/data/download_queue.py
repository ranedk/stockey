from __future__ import annotations

import argparse
import json
from typing import Any

from advisory.external_task_queue import enqueue_task
from data.download_runner import DOWNLOADER_STEPS, PARSER_STEPS, run_download_module


NSE_PREFIX = "data.nseindia."
DHAN_MODULES = {"data.dhanlive.scrip_master", "data.dhanlive.ohlcv"}
SCREENER_MODULES = {"data.screenerin.screener_parser"}


def classify_step(step: dict[str, Any]) -> dict[str, Any] | None:
    module_name = str(step.get("module") or "")
    args = [str(value) for value in (step.get("args") or [])]
    purpose = str(step.get("purpose") or "")
    if module_name == "data.dhanlive.scrip_master":
        return {"queue": "dhan", "task_type": "dhan_scrip_master", "task_args": {"args": args, "purpose": purpose}}
    if module_name == "data.dhanlive.ohlcv":
        return {"queue": "dhan", "task_type": "download_module", "task_args": {"module": module_name, "args": args, "purpose": purpose}}
    if module_name in SCREENER_MODULES:
        return {"queue": "screener", "task_type": "download_module", "task_args": {"module": module_name, "args": args, "purpose": purpose}}
    if module_name.startswith(NSE_PREFIX):
        return {"queue": "nse", "task_type": "nse_module", "task_args": {"module": module_name, "args": args, "purpose": purpose}}
    return None


def steps_for_phase(phase: str) -> list[dict[str, Any]]:
    normalized = str(phase or "downloaders").strip().lower()
    if normalized == "downloaders":
        return DOWNLOADER_STEPS
    if normalized == "parsers":
        return PARSER_STEPS
    if normalized == "all":
        return [*DOWNLOADER_STEPS, *PARSER_STEPS]
    raise ValueError(f"Unsupported phase: {phase}")


def enqueue_download_work(*, phase: str = "downloaders", run_non_queued: bool = True, dry_run: bool = False) -> dict[str, Any]:
    queued: list[dict[str, Any]] = []
    inline_results: list[dict[str, Any]] = []
    skipped_inline: list[dict[str, Any]] = []
    for step in steps_for_phase(phase):
        queued_spec = classify_step(step)
        if queued_spec is not None:
            task_preview = {
                "queue_name": queued_spec["queue"],
                "task_type": queued_spec["task_type"],
                "task_args": queued_spec["task_args"],
                "source_module": step.get("module"),
            }
            if not dry_run:
                row = enqueue_task(
                    queue_name=queued_spec["queue"],
                    task_type=queued_spec["task_type"],
                    task_args=queued_spec["task_args"],
                    priority=60 if queued_spec["queue"] in {"nse", "dhan"} else 50,
                    max_attempts=3,
                )
                task_preview["task_id"] = row["task_id"]
            queued.append(task_preview)
            continue
        if run_non_queued:
            inline_results.append({"status": "skipped_dry_run", **step} if dry_run else run_download_module(step))
        else:
            skipped_inline.append(step)
    status = "ok"
    if any(row.get("status") == "failed" for row in inline_results):
        status = "warning"
    return {
        "status": status,
        "phase": phase,
        "queued_count": len(queued),
        "inline_count": len(inline_results),
        "skipped_inline_count": len(skipped_inline),
        "queued": queued,
        "inline_results": inline_results,
        "skipped_inline": skipped_inline,
        "dry_run": bool(dry_run),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Enqueue single-client downloader/parser work and optionally run safe inline modules.")
    parser.add_argument("--phase", choices=["downloaders", "parsers", "all"], default="downloaders")
    parser.add_argument("--no-inline", action="store_true", help="Only enqueue single-client work; skip non-queued modules")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = enqueue_download_work(
        phase=args.phase,
        run_non_queued=not bool(args.no_inline),
        dry_run=bool(args.dry_run),
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return 0 if payload.get("status") != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
