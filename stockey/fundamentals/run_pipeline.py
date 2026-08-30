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
structured extraction -> deal flow -> investor classification -> sector
capital-cycle -> L3 rule triggers -> L3 LLM triage -> descriptive technicals ->
watchlist/narrative/email pipeline. investor_classification runs right after
structured_extraction AND deal_flow (needs both their investor_names output).
l3_triggers's capital_raise/bulk_deal_buy/bulk_deal_sell triggers still fire
unconditionally regardless of classification status (alert-worthy either way -- see
those evaluators), but their REASONING also names the investor tier when known
(fundamentals_investor_classification), which is why investor_classification staying
before l3_triggers in this list is a soft ordering preference, not just incidental --
running l3_triggers first would still work, just with tiers_by_key empty for that
pass (evaluated again, richer, on the next run once investor_classification catches
up).
L2 state and deal_flow MUST both stay before l3_triggers (already true) for a second
reason beyond "state used to corroborate events": both write SYNTHETIC events
straight into fundamentals_events (institutional_first_entry/pledge_increase from
L2, bulk_deal_buy/bulk_deal_sell from deal_flow -- no exchange filing exists for any
of these, see each module's own docstring), which l3_triggers then has to pick up in
the SAME run. deal_flow also needs L1 universe (the step above it) already refreshed
to know which companies' deals are in scope. The last step
(notifications.run_watchlist_notification_pipeline) MUST run last: it depends on L3
alerts, L2 state, technicals, and sector context all being current for that run.
That step internally chains watchlist sync -> narrative regen -> fundamentals.
screens.watchlist_exit's status evaluation (2026-08-13, docs/
FUNDAMENTAL_SCREENER_RESULTS_ARC.md -- the "we will crowd the watchlist" fix) ->
notify -> daily digest; watchlist_exit is NOT its own STEPS entry, it's called from
inside notifications.py so it always runs against a freshly-regenerated narrative
and before the digest's own default active-only filter reads it.

One step failing does not abort the run: every fundamentals module already isolates
its own external-source failures via fallback_telemetry and (for the two LLM-calling
steps) its own circuit breaker, so a single step's exception here is caught, recorded
in the JSON summary, and the pipeline continues to the next step -- matching the
"non-critical failure is visible, not fatal" philosophy download_runner.py applies to
its own non-market-data steps. The process exit code is only non-zero if EVERY step
failed (a likely-systemic issue, e.g. the DB being down), not on an individual flaky
source.

IMPLICIT CROSS-PIPELINE DEPENDENCY (documented 2026-08-17, not fixed by adding a
step here -- see rationale below): technicals.py's BSE-only-company fallback reads
bse_advisory_adjusted_ohlcv_daily, which is populated by data/bseindia/bhavcopy.py +
data/bseindia/price_adjustment.py -- both PURE-TA cron jobs (stockey/CLAUDE.md's
core 7, specifically job 4 / all_price_adjustment.sh, 18:50 UTC), not steps in this
STEPS list. all_fundamentals_screener.sh happens to run after all_price_adjustment.sh
today (19:15 UTC), but that ordering lives entirely in two separate crontab lines,
not anything this module enforces or checks -- a manual `python -m
fundamentals.run_pipeline` invocation, or any future crontab reordering, has no
guarantee that BSE price data is fresh when technicals.py runs. Deliberately NOT
fixed by adding a bhavcopy/price-adjustment step here: that would duplicate
collection work the pure-TA pipeline already does and blur the pure-TA/fundamentals
boundary stockey/CLAUDE.md establishes ("screens/ modules stay independent of data/
collection logic, not the reverse"). Instead, technicals.py's own
check_bse_price_pipeline_freshness() records a distinct
technicals_bse_price_pipeline_stale_or_never_run fallback event when the whole
bseindia_ohlcv table looks stale/empty, so this gap is visible in telemetry rather
than silently indistinguishable from any one company's own insufficient-history
gap."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from typing import Any

STEPS: list[str] = [
    "fundamentals.collectors.sector_data",
    # fundamentals.collectors.screenerin's deleveraging screen (was here 2026-08-14 to 08-15) is retired --
    # fundamentals_screenerin_query_results had zero readers from the day it was scheduled. The module
    # itself stays (l1_universe/l2_state still import its shared screener.in scraping infra).
    "fundamentals.screens.l1_universe",
    "fundamentals.screens.l2_state",
    "fundamentals.collectors.bse_announcements",
    "fundamentals.collectors.nse_pit",
    "fundamentals.collectors.rating_agencies",
    "fundamentals.collectors.ocr_pipeline",
    "fundamentals.collectors.structured_extraction",
    # Needs fundamentals_l1_universe (l1_universe step above) to scope which
    # companies' deals matter; writes synthetic bulk_deal_buy/bulk_deal_sell events
    # (PRD §12 todo #3, 2026-08-29) that investor_classification below discovers
    # investor names from, same as structured_extraction's capital_raise events.
    "fundamentals.screens.deal_flow",
    "fundamentals.screens.investor_classification",  # needs structured_extraction's + deal_flow's investor_names, runs right after both
    "fundamentals.screens.sector_cycle",
    "fundamentals.screens.l3_triggers",
    # Needs sector_cycle's fresh phases and l3_triggers' fresh alerts (both above).
    # Scoped to fundamentals_watchlist WHERE status='active' -- reads that table's
    # state as of the END of the PREVIOUS run (notifications' own internal
    # watchlist-sync, this run's copy, hasn't happened yet -- it's deliberately
    # last). One-cycle staleness, same "eventually consistent next run" tolerance
    # investor_classification's own catch-up already has -- not worth reaching
    # into notifications.py's internal call chain to close for a confluence READ,
    # not a decision (PRD §12 todo #4, 2026-08-29).
    "fundamentals.screens.confluence_score",
    "fundamentals.screens.llm_triage",
    "fundamentals.screens.technicals",
    "fundamentals.screens.notifications",  # must run last -- see module docstring
]


def _normalize_exit_code(value: object) -> int:
    # BUG FOUND LIVE 2026-08-20 (re-audit, LOW; same fix as data/download_runner.py's copy
    # of this helper): `isinstance(value, int)` is also True for `bool` (bool is an int
    # subclass in Python), so a module returning a bare True/False from main() used to pass
    # through as a bool rather than being normalized to 0/1 -- comparisons against 0 still
    # work, but this function's own contract is `-> int`, and json.dumps would serialize a
    # bool exit code as literal `true`/`false` instead of `1`/`0`.
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
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
