"""Read/write query functions backing the fundamentals screener API (fundamentals/
api/app.py) -- fundamental screener step 13, the API layer the new Nuxt/Vue frontend
(a new sibling repo, per the user's own decision in conversation 2026-08-11) will call.
Deliberately framework-free: every function here is a plain call taking/returning
dicts/lists, testable the same way as every other fundamentals/screens module (no
FastAPI TestClient needed to exercise the actual query logic) -- app.py's job is only
to wire these onto HTTP routes and translate ThesisValidationError into a 400.

Reuses the SAME evidence-loading functions watch_summary.py already built for the LLM
narrative (load_latest_l2_state_for_company, load_latest_technicals_for_company,
load_sector_context_for_company) so the API's "why" view shows a human EXACTLY what the
LLM saw -- not a second, possibly-drifted read of the same underlying state.

_clean_records() round-trips every DataFrame through pandas' own to_json rather than
.to_dict("records") directly: plain .to_dict() leaves NaN (from SQL NULL in a numeric
column) and NaT/Timestamp objects as pandas types. Python's json module happily emits
NaN as the bare (invalid-JSON) token `NaN`, which a browser's JSON.parse() rejects
outright -- confirmed a real risk here since several joined columns (first_seen_price,
capacity_growth_pct, narrative_generated_at, ...) can be legitimately NULL. pandas'
own to_json already knows how to turn NaN -> null and Timestamp -> ISO 8601 correctly,
so this reuses that instead of hand-rolling the same sanitization."""

from __future__ import annotations

import json

import pandas as pd

from fundamentals.collectors.rating_agencies import get_unsupported_rating_agencies
from fundamentals.screens.investor_classification import get_all_investor_classifications, set_investor_override
from fundamentals.screens.l1_universe import L1_QUERY, L1_QUERY_VERSION
from fundamentals.screens.l4_thesis import compute_quarterly_scoring, create_thesis, resolve_thesis
from fundamentals.screens.signal_pointers import get_stock_signal_pointers, load_satisfied_strategies_by_company
from fundamentals.screens.watch_summary import (
    load_latest_l2_state_for_company,
    load_latest_technicals_for_company,
    load_sector_context_for_company,
)
from utils.db import sql_to_df

_UNIVERSE_METRIC_KEYS = ("cmp_rs", "p_e", "mar_cap_rscr", "div_yld_pct", "roce_pct", "qtr_sales_var_pct", "avg_vol_1mth")


def _clean_records(df: pd.DataFrame) -> list[dict]:
    if df.empty:
        return []
    return json.loads(df.to_json(orient="records", date_format="iso"))


def get_universe() -> dict:
    """The L1 universe list plus the exact screening query/version used to produce
    it, shown on the same page per the user's own request (conversation 2026-08-11:
    "show what the screener returned as a list and the parameters used to screen on
    the same page")."""
    df = sql_to_df(
        """
        SELECT ticker, company_name, metrics_json, run_date
        FROM fundamentals_l1_universe
        WHERE run_date = (SELECT MAX(run_date) FROM fundamentals_l1_universe)
        ORDER BY ticker
        """
    )
    companies = []
    run_date = None
    for _, row in df.iterrows():
        run_date = row["run_date"]
        metrics = json.loads(row["metrics_json"]) if row["metrics_json"] else {}
        companies.append(
            {
                "ticker": row["ticker"],
                "company_name": row["company_name"],
                **{key: metrics.get(key) for key in _UNIVERSE_METRIC_KEYS},
            }
        )
    return {
        "query_text": L1_QUERY,
        "query_version": L1_QUERY_VERSION,
        "run_date": str(run_date) if run_date is not None else None,
        "companies": companies,
    }


def get_watchlist(status: str | None = "active") -> list[dict]:
    """status='active' by default (2026-08-13, docs/FUNDAMENTAL_SCREENER_RESULTS_ARC.md
    watchlist-exit build) -- this is the default "current watchlist" view, which is
    exactly where the "crowding" complaint that feature fixes was actually about.
    Nothing is ever deleted: pass status=None for every row regardless of status, or
    a specific status ('stale'/'invalidated'/'price_flagged') to see just that
    bucket -- fundamentals/screens/watchlist_exit.py's own status/status_reason
    columns are the source of truth, not re-derived here."""
    params: tuple = ()
    where_clause = ""
    if status is not None:
        where_clause = "WHERE w.status = %s"
        params = (status,)
    df = sql_to_df(
        f"""
        SELECT w.company_master_id, w.first_seen_at, w.first_seen_price, w.last_alert_at,
               w.alert_count, w.narrative_text, w.suggested_watch_until, w.narrative_generated_at,
               w.status, w.status_reason, l1.company_name, tech.close AS current_price
        FROM fundamentals_watchlist w
        LEFT JOIN LATERAL (
            SELECT company_name FROM fundamentals_l1_universe
            WHERE ticker = REPLACE(w.company_master_id, 'nse:', '')
            ORDER BY run_date DESC LIMIT 1
        ) l1 ON TRUE
        LEFT JOIN LATERAL (
            SELECT close FROM fundamentals_technicals
            WHERE company_master_id = w.company_master_id
            ORDER BY run_date DESC LIMIT 1
        ) tech ON TRUE
        {where_clause}
        ORDER BY w.last_alert_at DESC NULLS LAST
        """,  # noqa: S608 -- where_clause is a fixed internal string, params are parameterized
        params=params,
    )
    watchlist = _clean_records(df)
    if not watchlist:
        return []

    # 2026-08-13: strategy badges per row -- was previously just a bare alert_count,
    # the same gap fixed on the digest email (notifications.py's load_full_watchlist).
    strategies_by_company = load_satisfied_strategies_by_company()
    # BUG FOUND LIVE 2026-08-17: draft L4 theses (fundamentals_l4_thesis_draft) are
    # generated daily and shown in the digest email (notifications.py's own
    # load_full_watchlist does this exact join), but were never exposed anywhere in
    # this API -- the frontend had no way to show what the email already tells a
    # human. CANDIDATE ONLY, same as the email: never implies anything was saved to
    # fundamentals_l4_thesis.
    drafts_by_company = _load_draft_theses_by_company()
    for row in watchlist:
        row["strategies"] = strategies_by_company.get(row["company_master_id"], [])
        row["draft_thesis"] = drafts_by_company.get(row["company_master_id"])
    return watchlist


def _load_draft_theses_by_company() -> dict[str, dict]:
    """JSON-safe (see module docstring's _clean_records rationale -- confidence_score/
    target_date/generated_at can round-trip as NaN/Timestamp otherwise) draft L4
    theses for every currently-active watchlist company, keyed by company_master_id.
    Same join fundamentals/screens/l4_thesis_draft.py's own load_current_drafts_by_
    company() uses for the digest email; not reused directly here because that
    function's raw .to_dict("records") isn't run through _clean_records()."""
    df = sql_to_df(
        """
        SELECT d.* FROM fundamentals_l4_thesis_draft d
        JOIN fundamentals_watchlist w ON w.company_master_id = d.company_master_id
        WHERE w.status = 'active'
        """
    )
    return {record["company_master_id"]: record for record in _clean_records(df)}


def get_draft_theses() -> list[dict]:
    """Every CANDIDATE-ONLY draft L4 thesis for a currently-active watchlist company
    (fundamentals_l4_thesis_draft, generated daily by fundamentals/screens/
    l4_thesis_draft.py) -- same data the digest email already shows, now reachable
    without waiting for the next email. Never implies anything was saved to the real
    fundamentals_l4_thesis register; l4_thesis_draft.py never writes that table."""
    return list(_load_draft_theses_by_company().values())


def get_watchlist_detail(company_master_id: str) -> dict | None:
    watchlist_df = sql_to_df("SELECT * FROM fundamentals_watchlist WHERE company_master_id = %s", params=(company_master_id,))
    if watchlist_df.empty:
        return None

    alerts_df = sql_to_df(
        """
        SELECT source, news_id, trigger_type, origin, alert_date, reasoning, status, evidence_bundle_json
        FROM fundamentals_l3_alerts
        WHERE company_master_id = %s
        ORDER BY alert_date ASC NULLS LAST
        """,
        params=(company_master_id,),
    )
    alerts = []
    for alert in _clean_records(alerts_df):
        raw_bundle = alert.pop("evidence_bundle_json", None)
        alert["evidence_bundle"] = json.loads(raw_bundle) if raw_bundle else None
        alerts.append(alert)

    thesis_df = sql_to_df(
        "SELECT * FROM fundamentals_l4_thesis WHERE company_master_id = %s ORDER BY created_date DESC",
        params=(company_master_id,),
    )

    # BUG FOUND LIVE 2026-08-17: unlike get_watchlist()'s list view, the detail page
    # didn't even try to show a draft L4 thesis -- not status-gated the way the list
    # view's own draft lookup is (this is a single-company detail page: a company
    # that has since left the active watchlist still shows its alerts/portfolio here,
    # so its draft shouldn't disappear either).
    draft_df = sql_to_df("SELECT * FROM fundamentals_l4_thesis_draft WHERE company_master_id = %s", params=(company_master_id,))
    draft_records = _clean_records(draft_df)

    return {
        "watchlist": _clean_records(watchlist_df)[0],
        "alerts": alerts,
        "l2_state": load_latest_l2_state_for_company(company_master_id),
        "technicals": load_latest_technicals_for_company(company_master_id),
        "sector_context": load_sector_context_for_company(company_master_id),
        "portfolio": _clean_records(thesis_df),
        "draft_thesis": draft_records[0] if draft_records else None,
        # 2026-08-13: structured pointers (fundamentals/screens/signal_pointers.py),
        # same aggregator watch_summary.py's narrative LLM sees -- so a human viewing
        # this detail page gets the same agency-name/investor-tier/sector-growth
        # context, not a thinner read of the same facts.
        "signal_pointers": get_stock_signal_pointers(company_master_id),
    }


def get_sectors() -> list[dict]:
    sector_df = sql_to_df(
        """
        SELECT sc.sector_code, sc.capacity_growth_pct, sc.demand_growth_pct, sc.phase,
               sc.growth_classification, sc.sample_size_confidence, sc.n_companies_in_l1,
               sr.description AS sector_name
        FROM fundamentals_sector_cycle sc
        LEFT JOIN LATERAL (
            SELECT description FROM fundamentals_sector_reference
            WHERE code = sc.sector_code AND level = 'sector'
            ORDER BY as_of_date DESC LIMIT 1
        ) sr ON TRUE
        WHERE sc.run_date = (SELECT MAX(run_date) FROM fundamentals_sector_cycle)
        ORDER BY sc.sector_code
        """
    )
    sectors = _clean_records(sector_df)
    if not sectors:
        return []

    watched_df = sql_to_df(
        """
        SELECT ds.sector_code, w.company_master_id, w.alert_count, w.narrative_text
        FROM fundamentals_watchlist w
        JOIN dim_security ds ON ds.company_master_id = w.company_master_id
        WHERE ds.sector_code IS NOT NULL
        """
    )
    watched_by_sector: dict[str, list[dict]] = {}
    for row in _clean_records(watched_df):
        narrative_text = row.pop("narrative_text", None) or ""
        row["narrative_snippet"] = narrative_text[:200] or None
        watched_by_sector.setdefault(row["sector_code"], []).append(row)

    for sector in sectors:
        sector["watched_companies"] = watched_by_sector.get(sector["sector_code"], [])
    return sectors


def get_portfolio() -> list[dict]:
    df = sql_to_df("SELECT * FROM fundamentals_l4_thesis ORDER BY created_date DESC")
    return _clean_records(df)


def get_portfolio_scoring() -> dict:
    """Thin wrapper over l4_thesis.compute_quarterly_scoring -- fundamental_basic_
    goal.md sec 5's actual outcome measure (forecast hit rate, failure-attribution
    breakdown, time-to-confirmation), not returns. Read-only, computed fresh from
    whatever's resolved so far -- nothing here is cached or precomputed."""
    return compute_quarterly_scoring()


def create_portfolio_entry(payload: dict) -> dict:
    """Thin wrapper over l4_thesis.create_thesis -- ThesisValidationError propagates
    to the caller (app.py translates it to a 400, this layer does no validation of
    its own so there's exactly one place mandatory-field rules live)."""
    return create_thesis(**payload)


def resolve_portfolio_entry(thesis_id: str, payload: dict) -> None:
    resolve_thesis(thesis_id=thesis_id, **payload)


def get_investor_classifications() -> list[dict]:
    """Every classified investor -- override_tier included as-is so the frontend can
    show both the LLM's original suggestion and any human correction, not just the
    effective one."""
    return _clean_records(get_all_investor_classifications())


def set_investor_classification_override(investor_key: str, payload: dict) -> None:
    """Thin wrapper over investor_classification.set_investor_override -- ValueError
    (bad tier) propagates to the caller (app.py translates it to a 400)."""
    set_investor_override(investor_key, **payload)


def get_strategies() -> list[dict]:
    """Top-level strategy registry (2026-08-13, user request: "the web app can start
    with the list of strategies and inside each one the list which is being watched
    under it"). No new backend concept needed -- trigger_type in fundamentals_l3_
    alerts already IS the strategy dimension, already many-rows-per-company (a
    company can satisfy several trigger_types at once). This just groups the
    existing table by that column instead of adding a registry table."""
    df = sql_to_df(
        """
        SELECT trigger_type, COUNT(DISTINCT company_master_id) AS company_count, MAX(alert_date) AS last_alert_date
        FROM fundamentals_l3_alerts
        WHERE company_master_id IS NOT NULL
        GROUP BY trigger_type
        ORDER BY trigger_type
        """
    )
    return _clean_records(df)


def get_strategy_detail(trigger_type: str) -> dict | None:
    """Every company currently alerted under one trigger_type, most recent alert per
    company. None (-> app.py 404) when this trigger_type has never fired, same
    not-found convention get_watchlist_detail already uses."""
    df = sql_to_df(
        """
        SELECT DISTINCT ON (a.company_master_id)
               a.company_master_id, a.alert_date, a.reasoning, a.origin,
               l1.company_name, tech.close AS current_price
        FROM fundamentals_l3_alerts a
        LEFT JOIN LATERAL (
            SELECT company_name FROM fundamentals_l1_universe
            WHERE ticker = REPLACE(a.company_master_id, 'nse:', '')
            ORDER BY run_date DESC LIMIT 1
        ) l1 ON TRUE
        LEFT JOIN LATERAL (
            SELECT close FROM fundamentals_technicals
            WHERE company_master_id = a.company_master_id
            ORDER BY run_date DESC LIMIT 1
        ) tech ON TRUE
        WHERE a.trigger_type = %s AND a.company_master_id IS NOT NULL
        ORDER BY a.company_master_id, a.alert_date DESC NULLS LAST
        """,
        params=(trigger_type,),
    )
    if df.empty:
        return None
    return {"trigger_type": trigger_type, "companies": _clean_records(df)}


def get_todos() -> dict:
    """Development todo board (2026-08-13, user-requested) -- currently one
    category: rating agencies detected in real data with no enrichment plugin yet,
    ranked by how often they've actually shown up. Self-clearing: an agency drops
    off the moment its plugin ships (fundamentals/collectors/rating_agencies.py's
    get_unsupported_rating_agencies() filters against the live AGENCY_PLUGINS
    registry, not a stored "done" flag). A dict, not a list, so more todo
    categories can be added later without changing this endpoint's shape."""
    return {"rating_agencies": _clean_records(get_unsupported_rating_agencies())}
