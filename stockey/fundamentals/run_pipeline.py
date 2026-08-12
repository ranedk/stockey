"""Fundamentals screener pipeline orchestrator -- runs every stage of the fundamental
screener (docs/FUNDAMENTAL_SCREENER_PRD.md) in dependency order as one command, for
cron (all_fundamentals_screener.sh) or manual use (`python -m fundamentals.run_pipeline`).

Mirrors data/download_runner.py's own "import the module, call main() directly,
extract STOCKEY_RUN_STATE" pattern (not subprocess or runpy -- a SystemExit loses
module globals under runpy, same reasoning download_runner.py's own
_execute_module_entrypoint documents) but stays self-contained rather than importing
download_runner's private helpers or its sync_state persistence: every fundamentals
module already produces its own complete STOCKEY_RUN_STATE + fallback_telemetry
trail, so there's nothing else to translate here.

Steps run in dependency order (docs/FUNDAMENTAL_SCREENER_PRD.md sec 8's numbered
steps): sector reference -> L1 universe -> L2 state -> event collectors -> OCR ->
structured extraction -> investor classification -> sector capital-cycle -> L3 rule
triggers -> L3 LLM triage -> descriptive technicals -> watchlist/narrative/email
pipeline. investor_classification runs right after structured_extraction (needs its
investor_names output); l3_triggers can run before OR after it since the
capital_raise trigger fires unconditionally, independent of classification status.
The last step (notifications.run_watchlist_notification_pipeline) MUST run last: it
depends on L3 alerts, L2 state, technicals, and sector context all being current for
that run.

One step failing does not abort the run: every fundamentals module already isolates
its own external-source failures via fallback_telemetry and (for the two LLM-calling
steps) its own circuit breaker, so a single step's exception here is caught, recorded
in the JSON summary, and the pipeline continues to the next step -- matching the
"non-critical failure is visible, not fatal" philosophy download_runner.py applies to
its own non-market-data steps. The process exit code is only non-zero if EVERY step
failed (a likely-systemic issue, e.g. the DB being down), not on an individual flaky
source."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from typing import Any

STEPS: list[str] = [
    "fundamentals.collectors.sector_data",
    "fundamentals.screens.l1_universe",
    "fundamentals.screens.l2_state",
    "fundamentals.collectors.bse_announcements",
    "fundamentals.collectors.nse_pit",
    "fundamentals.collectors.rating_agencies",
    "fundamentals.collectors.ocr_pipeline",
    "fundamentals.collectors.structured_extraction",
    "fundamentals.screens.investor_classification",  # needs structured_extraction's investor_names, runs right after it
    "fundamentals.screens.sector_cycle",
    "fundamentals.screens.l3_triggers",
    "fundamentals.screens.llm_triage",
    "fundamentals.screens.technicals",
    "fundamentals.screens.notifications",  # must run last -- see module docstring
]


def _normalize_exit_code(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    return 1


def run_step(module_name: str) -> dict[str, Any]:
    """Import module_name fresh and call its main() directly. Never raises -- any
    exception (including SystemExit) is caught and reported in the returned dict so
    run_pipeline() can move on to the next step."""
    started_at = time.monotonic()
    sys.modules.pop(module_name, None)
    print(f"[fundamentals.run_pipeline] step={module_name} start", file=sys.stderr, flush=True)

    code = 1
    run_state: dict[str, Any] = {}
    error: str | None = None
    try:
        module = importlib.import_module(module_name)
        code = _normalize_exit_code(module.main())
        run_state = getattr(module, "STOCKEY_RUN_STATE", {})
    except SystemExit as exc:
        code = _normalize_exit_code(exc.code)
        module = sys.modules.get(module_name)
        run_state = getattr(module, "STOCKEY_RUN_STATE", {}) if module else {}
    except Exception as exc:  # noqa: BLE001 -- one step's exception must not abort the pipeline
        error = f"{type(exc).__name__}: {exc}"

    elapsed = round(time.monotonic() - started_at, 2)
    status = "ok" if code == 0 else "failed"
    print(f"[fundamentals.run_pipeline] step={module_name} status={status} elapsed={elapsed:.2f}s", file=sys.stderr, flush=True)

    result: dict[str, Any] = {"module": module_name, "returncode": code, "elapsed_seconds": elapsed, "run_state": run_state}
    if error:
        result["error"] = error
    return result


def run_pipeline(steps: list[str] | None = None) -> dict[str, Any]:
    results = [run_step(module_name) for module_name in (steps or STEPS)]
    failed = [r["module"] for r in results if r["returncode"] != 0]
    return {"steps_run": len(results), "failed": failed, "results": results}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the fundamentals screener pipeline end to end.")
    parser.add_argument("--steps", nargs="*", default=None, help="Override the module list (defaults to every stage in dependency order).")
    args = parser.parse_args()

    summary = run_pipeline(args.steps)
    print(json.dumps({"source": "fundamentals.run_pipeline", **summary}, ensure_ascii=False, default=str), flush=True)

    if summary["failed"] and len(summary["failed"]) == summary["steps_run"]:
        return 1  # every step failed -- likely systemic, worth a non-zero exit
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
