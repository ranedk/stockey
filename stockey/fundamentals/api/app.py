"""FastAPI app for the fundamentals screener -- fundamental screener step 13. Serves
the new Nuxt/Vue frontend (new sibling repo, per the user's own decision in
conversation 2026-08-11). Thin routing layer only: every route calls straight into
fundamentals/api/queries.py's plain functions, which do the actual DB work and are
independently unit-tested without any HTTP machinery.

Run with: uvicorn fundamentals.api.app:app --reload --port 8000

NOT hardened for public exposure -- no auth, permissive localhost-only CORS. This is a
personal single-user tool meant to run on localhost/trusted network alongside the Nuxt
dev server, same trust model as every other script in this repo (all of which already
assume local execution with a .env holding real credentials). Do not put this behind a
public endpoint without adding real auth first.

Write surface is deliberately narrow: the only mutations exposed are portfolio
the machine portfolio (fundamentals/screens/portfolio_runner.py opens positions,
portfolio_resolution.py resolves their forecasts -- no human act remains, per
fundamental_basic_goal.md sec 1). Nothing here can create an alert, a watchlist entry,
or a narrative -- those stay batch-job-only (fundamentals/screens/notifications.py's
pipeline), never a live HTTP write, so there's no path for the frontend to inject a
fabricated signal into the screener's own data.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from fundamentals.api import queries
from fundamentals.screens.investor_classification import INVESTOR_TIERS

app = FastAPI(title="Stockey Fundamentals Screener API")

# Nuxt's default dev ports (3000 for `nuxt dev`, 5173 if ever run under bare Vite) --
# widen this list once the frontend has a real deployed origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class InvestorOverrideRequest(BaseModel):
    tier: str
    notes: str | None = None


@app.get("/api/health")
def health() -> dict:
    """No DB/dependency check on purpose -- this only tells the cron-managed launcher
    (all_fundamentals_api.sh) whether a healthy instance already owns the port, not
    whether the DB is reachable (a query-level failure surfaces on the actual routes,
    where fallback_telemetry already applies)."""
    return {"status": "ok"}


@app.get("/api/universe")
def universe() -> dict:
    return queries.get_universe()


@app.get("/api/watchlist")
def watchlist(status: str = "active") -> list[dict]:
    # status="all" is the sentinel for "every row regardless of status" -- a bare
    # empty/missing query param is awkward to express unambiguously in a URL, "all"
    # reads clearly. Everything else (active/stale/invalidated/price_flagged) passes
    # straight through to queries.get_watchlist's own filter.
    return queries.get_watchlist(status=None if status == "all" else status)


@app.get("/api/watchlist/summary")
def watchlist_summary(status: str = "active") -> dict:
    # Declared BEFORE /api/watchlist/{company_master_id} -- FastAPI matches path
    # routes in declaration order, so a literal /summary segment must come first or
    # it would be captured as a company_master_id instead.
    return queries.get_watchlist_return_summary(status=None if status == "all" else status)


@app.get("/api/watchlist/{company_master_id}")
def watchlist_detail(company_master_id: str) -> dict:
    result = queries.get_watchlist_detail(company_master_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"{company_master_id} is not on the watchlist")
    return result


@app.get("/api/watchlist/{company_master_id}/sizing")
def watchlist_sizing(company_master_id: str, capital_per_position_rs: float | None = None) -> dict:
    # PRD §12 todo #8 -- a CALCULATOR, not a decision: 404s if this company has no
    # OPEN, ACCEPTED position in the machine portfolio.
    # capital_per_position_rs is OPTIONAL and defaults to the operator-set
    # PORTFOLIO_CAPITAL_PER_POSITION_RS (Rs 1,00,000, 2026-09-04). It used to be a
    # required total_capital/position_count pair with no default, because this API had
    # no business guessing the operator's sleeve. It is not guessing now -- that figure
    # is a recorded decision, and an override is still accepted.
    try:
        result = queries.get_position_sizing(company_master_id, capital_per_position_rs=capital_per_position_rs)
    except ValueError as exc:  # capital_per_position_rs <= 0
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail=f"{company_master_id} has no open accepted position to size")
    return result


@app.get("/api/drafts")
def drafts() -> list[dict]:
    """Every CANDIDATE-ONLY draft L4 thesis for a currently-active watchlist company
    -- see queries.get_draft_theses's own docstring for why this exists (same data
    the digest email already sends, previously not reachable via the API at all)."""
    return queries.get_draft_theses()


@app.get("/api/sectors")
def sectors() -> list[dict]:
    return queries.get_sectors()


@app.get("/api/portfolio")
def portfolio() -> list[dict]:
    """The machine portfolio (fundamentals_portfolio_position).

    The POST create/resolve endpoints that used to sit here were deleted 2026-09-04: the
    operator removed the human forecast entirely, so there is no human act left for this
    API to accept. Positions are opened by portfolio_runner and resolved by
    portfolio_resolution, both unattended."""
    return queries.get_portfolio()


@app.get("/api/portfolio/scoring")
def portfolio_scoring() -> dict:
    return queries.get_portfolio_scoring()


@app.get("/api/investors")
def investors() -> list[dict]:
    return queries.get_investor_classifications()


@app.post("/api/investors/{investor_key}/override")
def override_investor(investor_key: str, payload: InvestorOverrideRequest) -> dict:
    if payload.tier not in INVESTOR_TIERS:
        raise HTTPException(status_code=400, detail=f"tier must be one of {INVESTOR_TIERS}")
    queries.set_investor_classification_override(investor_key, payload.model_dump())
    return {"status": "updated", "investor_key": investor_key}


@app.get("/api/todos")
def todos() -> dict:
    return queries.get_todos()


@app.get("/api/strategies")
def strategies() -> list[dict]:
    return queries.get_strategies()


@app.get("/api/strategies/{trigger_type}")
def strategy_detail(trigger_type: str) -> dict:
    result = queries.get_strategy_detail(trigger_type)
    if result is None:
        raise HTTPException(status_code=404, detail=f"{trigger_type} has never alerted")
    return result


# --- Data-platform health ---------------------------------------------------
# Deliberately NOT a fundamentals concern, but this is stockey's only HTTP surface
# and the screener frontend already talks to it. It reports on the data platform's
# own completeness -- monitoring, not research/signals/LLM -- so it sits inside the
# pure-TA boundary (docs/DATA_INVENTORY.md) rather than crossing it.
#
# Cached: the intraday checks scan a 30-day window of a 528M-row compressed
# hypertable and take ~20s, which is far too slow to run on every page load. A short
# TTL keeps the dashboard responsive while staying fresh enough to be useful; the
# authoritative gate is still the nightly all_data_completeness.sh cron job.
_HEALTH_CACHE: dict[str, object] = {"at": 0.0, "payload": None, "window_days": None}
_HEALTH_TTL_SECONDS = 300


@app.get("/api/data-health")
def data_health(window_days: int = 30, refresh: bool = False) -> dict:
    """Data-platform completeness: is the data actually THERE, across the universe?

    Same implementation as the nightly cron gate (scripts/data_completeness.py's
    run_all_checks) -- one definition of "complete", so the dashboard and the gate
    cannot drift apart.
    """
    import time

    from scripts.data_completeness import ERROR, WARN, run_all_checks

    now = time.time()
    fresh_enough = (
        not refresh
        and _HEALTH_CACHE["payload"] is not None
        and _HEALTH_CACHE["window_days"] == window_days
        and now - float(_HEALTH_CACHE["at"]) < _HEALTH_TTL_SECONDS
    )
    if fresh_enough:
        payload = dict(_HEALTH_CACHE["payload"])  # type: ignore[arg-type]
        payload["cached"] = True
        payload["age_seconds"] = round(now - float(_HEALTH_CACHE["at"]))
        return payload

    try:
        findings = run_all_checks(window_days=window_days)
    except Exception as exc:  # noqa: BLE001 -- surface the failure, never a blank green page
        raise HTTPException(
            status_code=503,
            detail=f"data-health checks could not run: {type(exc).__name__}: {exc}",
        ) from exc

    errors = [f for f in findings.items if f["level"] == ERROR]
    warnings = [f for f in findings.items if f["level"] == WARN]
    payload = {
        "status": "error" if errors else ("warn" if warnings else "ok"),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "window_days": window_days,
        "error_count": len(errors),
        "warn_count": len(warnings),
        "findings": findings.items,
    }
    _HEALTH_CACHE.update({"at": now, "payload": payload, "window_days": window_days})
    return {**payload, "cached": False, "age_seconds": 0}


@app.get("/api/collectors")
def collectors() -> dict:
    """Per-collector status from advisory_sync_state, reconciled against the live
    registry so a deleted/renamed module's frozen error row is not reported as a live
    failure. See queries.get_collectors for why that reconciliation is the point."""
    return queries.get_collectors()


@app.get("/api/scheduler-health")
def scheduler_health() -> dict:
    """go-crond alive AND firing, stale locks, crontab drift, watchdog, Chrome CDP.

    Backed by scripts/is_cron_running.sh --json so the browser and the operator's shell
    answer the same question with one implementation. Note the inherent limit: an API
    served by a process the scheduler manages cannot fully report on that scheduler --
    this improves visibility, it does not replace the OS-crontab watchdog.
    """
    try:
        return queries.get_scheduler_health()
    except Exception as exc:  # noqa: BLE001 -- surface it, never a blank green panel
        raise HTTPException(
            status_code=503,
            detail=f"scheduler health check could not run: {type(exc).__name__}: {exc}",
        ) from exc


@app.get("/api/platform-issues")
def platform_issues(hours: int = 24, limit: int = 100) -> dict:
    """Open identity issues + a fallback-telemetry rollup. Read-only: unlike
    issue_digest's nightly pass this never rechecks or closes anything."""
    return queries.get_platform_issues(hours=hours, limit=limit)


@app.get("/api/coverage-report")
def coverage_report(history_days: int = 30) -> dict:
    """Per-table coverage/staleness from the nightly data_coverage_report, plus a short
    staleness trend -- a degrading feed shows up here before it fails outright."""
    return queries.get_coverage_report(history_days=history_days)


@app.get("/api/ruleset-portfolio")
def ruleset_portfolio() -> dict:
    """Positions from the mechanical ruleset + LLM adjudicator. Shadow (rejected-but-
    tracked) rows are included and labelled -- they are the adjudicator's own scorecard."""
    return queries.get_ruleset_positions()
