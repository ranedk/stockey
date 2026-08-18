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
create/resolve (wrapping fundamentals/screens/l4_thesis.py's create_thesis/
resolve_thesis, the ONE deliberate human act in this whole pipeline, per
fundamental_basic_goal.md sec 1). Nothing here can create an alert, a watchlist entry,
or a narrative -- those stay batch-job-only (fundamentals/screens/notifications.py's
pipeline), never a live HTTP write, so there's no path for the frontend to inject a
fabricated signal into the screener's own data.
"""

from __future__ import annotations

from datetime import date

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from fundamentals.api import queries
from fundamentals.screens.investor_classification import INVESTOR_TIERS
from fundamentals.screens.l4_thesis import ORIGIN_TAGS, ThesisValidationError

app = FastAPI(title="Stockey Fundamentals Screener API")

# Nuxt's default dev ports (3000 for `nuxt dev`, 5173 if ever run under bare Vite) --
# widen this list once the frontend has a real deployed origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class PortfolioCreateRequest(BaseModel):
    company_master_id: str
    prediction_text: str
    target_date: date
    invalidation_criteria: str
    origin_tag: str
    signal_definition_version: str | None = None
    source_alert_source: str | None = None
    source_alert_news_id: str | None = None
    source_alert_trigger_type: str | None = None
    metric_name: str | None = None
    metric_operator: str | None = None
    metric_threshold: float | None = None


class PortfolioResolveRequest(BaseModel):
    resolved_true: bool
    resolution_notes: str | None = None
    failure_attribution: str | None = None


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


@app.get("/api/watchlist/{company_master_id}")
def watchlist_detail(company_master_id: str) -> dict:
    result = queries.get_watchlist_detail(company_master_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"{company_master_id} is not on the watchlist")
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
    return queries.get_portfolio()


@app.get("/api/portfolio/scoring")
def portfolio_scoring() -> dict:
    return queries.get_portfolio_scoring()


@app.post("/api/portfolio")
def create_portfolio(payload: PortfolioCreateRequest) -> dict:
    if payload.origin_tag not in ORIGIN_TAGS:
        raise HTTPException(status_code=400, detail=f"origin_tag must be one of {ORIGIN_TAGS}")

    source_alert = None
    if payload.source_alert_source:
        source_alert = {
            "source": payload.source_alert_source,
            "news_id": payload.source_alert_news_id,
            "trigger_type": payload.source_alert_trigger_type,
        }

    fields = payload.model_dump(exclude={"source_alert_source", "source_alert_news_id", "source_alert_trigger_type"})
    try:
        return queries.create_portfolio_entry({**fields, "source_alert": source_alert})
    except ThesisValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/portfolio/{thesis_id}/resolve")
def resolve_portfolio(thesis_id: str, payload: PortfolioResolveRequest) -> dict:
    try:
        queries.resolve_portfolio_entry(thesis_id, payload.model_dump())
    except ThesisValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "resolved", "thesis_id": thesis_id}


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
