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
from fundamentals.screens.confluence_score import _ensure_confluence_score_table
from fundamentals.screens.investor_classification import get_all_investor_classifications, set_investor_override
from fundamentals.screens.l5_sizing import MAX_POSITIONS
from fundamentals.screens.portfolio_buckets import DEFAULT_BUCKET, bucket_config, capital_per_position_rs
from fundamentals.screens.portfolio_exit import PRICE_LOOKBACK_DAYS
from fundamentals.screens.portfolio_resolution import compute_portfolio_scoring
from fundamentals.screens.l4_thesis_draft import _ensure_draft_table
from fundamentals.screens.l5_sizing import get_position_size_recommendation as _get_position_size_recommendation
from fundamentals.screens.signal_pointers import get_stock_signal_pointers, load_satisfied_strategies_by_company
from fundamentals.screens.watch_summary import (
    load_latest_l2_state_for_company,
    load_latest_technicals_for_company,
    load_sector_context_for_company,
)
from utils.db import sql_to_df
from utils.json_safe import loads_lenient

_UNIVERSE_METRIC_KEYS = ("cmp_rs", "p_e", "mar_cap_rscr", "div_yld_pct", "roce_pct", "qtr_sales_var_pct", "avg_vol_1mth")


def _clean_records(df: pd.DataFrame) -> list[dict]:
    if df.empty:
        return []
    return json.loads(df.to_json(orient="records", date_format="iso"))


def get_universe() -> dict:
    """The current universe list plus how it was built, on one page (user request
    2026-08-11: "show what the screener returned as a list and the parameters used to
    screen on the same page"). Version and rule text come from the stored rows, never
    from code constants, so the page cannot describe a different screen from the one
    that produced the list (it did, for a day, after the 2026-09-25 switch to version 2).
    Version 2 rows also carry each stock's group and which allow-rule (if any) let it
    through Layer 2; `excluded` lists what Layer 2 removed on its latest run, and why."""
    df = sql_to_df(
        """
        SELECT ticker, company_name, metrics_json, run_date, query_version, query_text
        FROM fundamentals_l1_universe
        WHERE run_date = (SELECT MAX(run_date) FROM fundamentals_l1_universe)
        ORDER BY ticker
        """
    )
    companies = []
    for _, row in df.iterrows():
        metrics = json.loads(row["metrics_json"]) if row["metrics_json"] else {}
        companies.append(
            {
                "ticker": row["ticker"],
                "company_name": row["company_name"],
                **{key: metrics.get(key) for key in _UNIVERSE_METRIC_KEYS},
                "group": metrics.get("universe_group"),
                "allowed_by": metrics.get("layer2_allowed_by"),
            }
        )
    first = df.iloc[0] if not df.empty else None
    return {
        "query_text": first["query_text"] if first is not None else None,
        "query_version": int(first["query_version"]) if first is not None else None,
        "run_date": str(first["run_date"]) if first is not None else None,
        "companies": companies,
        "excluded": _latest_layer2_exclusions(),
    }


def _latest_layer2_exclusions() -> list[dict]:
    try:
        df = sql_to_df(
            """
            SELECT symbol, "group", layer2_reasons, run_date
            FROM fundamentals_universe_layer2_inputs
            WHERE run_date = (SELECT MAX(run_date) FROM fundamentals_universe_layer2_inputs)
              AND layer2_reasons <> ''
            ORDER BY symbol
            """
        )
    except Exception:  # noqa: BLE001 -- table absent before the first version-2 run
        return []
    return [
        {"ticker": r["symbol"], "group": r["group"], "reasons": [x for x in str(r["layer2_reasons"]).split("; ") if x]}
        for _, r in df.iterrows()
    ]


def get_watchlist(status: str | None = "active") -> list[dict]:
    """status='active' by default (2026-08-13, docs/FUNDAMENTAL_SCREENER_RESULTS_ARC.md
    watchlist-exit build) -- this is the default "current watchlist" view, which is
    exactly where the "crowding" complaint that feature fixes was actually about.
    Nothing is ever deleted: pass status=None for every row regardless of status, or
    a specific status ('stale'/'invalidated'/'price_flagged') to see just that
    bucket -- fundamentals/screens/watchlist_exit.py's own status/status_reason
    columns are the source of truth, not re-derived here.

    BUG FOUND LIVE 2026-08-18 (re-audit): current_price used to be rendered at face
    value with no indication it could be stale -- watchlist_exit.py was already
    fixed (2026-08-15) to refuse judging a price move when fundamentals_
    technicals.price_data_stale is set, but that fix stopped at the status-
    evaluation layer; this list view (and the digest email's identical join) never
    selected the flag at all. Confirmed live: a real watchlist company showed a
    current_price frozen 3.6 months stale with nothing marking it as such. Now
    selected so callers (the frontend, the digest) can render it distinctly rather
    than as if it were today's close.

    BUG FOUND LIVE 2026-08-29: the confluence-score LEFT JOIN LATERAL added just
    below 500'd every request on a DB where fundamentals/screens/confluence_score.py
    has never completed a real run yet (UndefinedTable -- upsert_to_db's own
    CREATE TABLE IF NOT EXISTS only runs on a WRITE, never a read). Same failure
    class _load_draft_theses_by_company already hit and fixed 2026-08-18 for
    fundamentals_l4_thesis_draft; same fix here."""
    _ensure_confluence_score_table()
    from fundamentals.screens.watchlist import _ensure_membership_columns

    _ensure_membership_columns()  # read before the first story-score sync must not 500
    params: tuple = ()
    where_clause = ""
    if status is not None:
        where_clause = "WHERE w.status = %s"
        params = (status,)
    df = sql_to_df(
        f"""
        SELECT w.company_master_id, w.first_seen_at, w.first_seen_price, w.last_alert_at,
               w.alert_count, w.narrative_text, w.suggested_watch_until, w.narrative_generated_at,
               w.status, w.status_reason, l1.company_name, tech.close AS current_price,
               tech.price_data_stale, tech.as_of_date AS current_price_as_of,
               conf.confluence_count, conf.contradicting_count, conf.evaluable_count,
               w.story_score, w.primary_dimension, w.entry_basis, w.flaws
        FROM fundamentals_watchlist w
        LEFT JOIN LATERAL (
            SELECT company_name FROM fundamentals_l1_universe
            WHERE ticker IN (
                -- Through company_master, not string surgery (2026-09-23 audit): the id's
                -- suffix is not the L1 slug for BSE-only names ('nse:543531-BOM' vs
                -- '543531') or NSE names whose slug is a BSE code (ALUFLUOR = 524634),
                -- so ~22 percent of the watchlist showed no company name.
                SELECT cm.nse_ticker FROM company_master cm WHERE cm.company_master_id = w.company_master_id
                UNION SELECT cm.bse_scrip_code FROM company_master cm WHERE cm.company_master_id = w.company_master_id
                UNION SELECT REPLACE(w.company_master_id, 'nse:', ''))
            ORDER BY run_date DESC LIMIT 1
        ) l1 ON TRUE
        LEFT JOIN LATERAL (
            SELECT close, price_data_stale, as_of_date FROM fundamentals_technicals
            WHERE company_master_id = w.company_master_id
            ORDER BY run_date DESC LIMIT 1
        ) tech ON TRUE
        LEFT JOIN LATERAL (
            -- PRD §12 todo #6 (2026-08-29): fundamentals/screens/confluence_score.py's
            -- mechanical axis count, latest run only. NULL (not 0) for a company
            -- confluence_score.py hasn't scored yet -- see _clean_records' own
            -- NaN-to-null handling; a real 0 (every axis unclear/contradicting)
            -- must stay distinguishable from "never scored".
            SELECT confluence_count, contradicting_count, evaluable_count
            FROM fundamentals_confluence_score
            WHERE company_master_id = w.company_master_id
            ORDER BY run_date DESC, score_version DESC LIMIT 1
        ) conf ON TRUE
        {where_clause}
        -- strongest story first (step 6, 2026-09-29); the alert date only breaks ties
        ORDER BY w.story_score DESC NULLS LAST, w.last_alert_at DESC NULLS LAST
        """,  # noqa: S608 -- where_clause is a fixed internal string, params are parameterized
        params=params,
    )
    watchlist = _clean_records(df)
    if not watchlist:
        return []

    # BUG FOUND LIVE 2026-08-26: the digest email showed a real watchlist company
    # (DBCORP) as "Rs.206.80 -> Rs.206.80 (+0.0%)" -- a confident-looking "no move"
    # that was actually a routine ~1-day publish lag (advisory_adjusted_ohlcv_daily
    # always runs a trading day behind nseindia_ohlcv) coinciding with the
    # company's first_seen_at date, so entry price and "current" price resolved to
    # the literal same row. price_data_stale doesn't catch this (the gap is 1 day,
    # not >7). Fixed in the digest (notifications.py's _no_move_data_yet); this API
    # response is the frontend watchlist page's identical gap -- same underlying
    # query/join, so the same false-zero was reachable there too. Computed here,
    # not left for the frontend to reimplement, same reasoning as price_data_stale
    # already being a precomputed boolean rather than a raw date pair to diff.
    for row in watchlist:
        first_seen_at = row.get("first_seen_at")
        current_price_as_of = row.get("current_price_as_of")
        row["no_fresh_price_yet"] = bool(
            first_seen_at and current_price_as_of and str(first_seen_at)[:10] == str(current_price_as_of)[:10]
        )

    # 2026-08-13: strategy badges per row -- was previously just a bare alert_count,
    # the same gap fixed on the digest email (notifications.py's load_full_watchlist).
    strategies_by_company = load_satisfied_strategies_by_company()
    # BUG FOUND LIVE 2026-08-17: draft L4 theses (fundamentals_l4_thesis_draft) are
    # generated daily and shown in the digest email (notifications.py's own
    # load_full_watchlist does this exact join), but were never exposed anywhere in
    # this API -- the frontend had no way to show what the email already tells a
    # human. CANDIDATE ONLY, same as the email: never implies anything was saved to
    # the machine portfolio.
    # BUG FOUND LIVE 2026-08-18 (re-audit): draft_thesis was silently absent on
    # every non-active row in this list view -- _load_draft_theses_by_company()
    # unconditionally gated on w.status='active' regardless of the `status` param
    # this function was actually called with, unlike get_watchlist_detail() below,
    # which deliberately does NOT status-gate its own draft lookup (a company that
    # has since left the active watchlist still shows its alerts/portfolio there,
    # so its draft shouldn't disappear either -- same reasoning applies here).
    # active_only=False when status != "active": for status="active" itself the
    # gate is a no-op anyway (every row in `watchlist` already IS active), so this
    # only changes behavior for status=None/"stale"/"invalidated"/"price_flagged".
    drafts_by_company = _load_draft_theses_by_company(active_only=(status == "active"))
    for row in watchlist:
        row["strategies"] = strategies_by_company.get(row["company_master_id"], [])
        row["draft_thesis"] = drafts_by_company.get(row["company_master_id"])
    return watchlist


def get_watchlist_return_summary(status: str | None = "active") -> dict:
    """Equal-weight average return across the watchlist since each company's own
    first_seen_at -- a cheap directional gut-check ("is this list net up or down"),
    requested 2026-08-28 to show at the top of the screener/ watchlist page. status
    defaults to 'active' same as get_watchlist() itself; pass None/'stale'/etc. the
    same way to scope the summary to a different bucket.

    Deliberately NOT a portfolio return: no time-weighting (a company added
    yesterday counts the same as one added six months ago), no rebalancing, and no
    correction for a company that has since left this status bucket -- same
    survivorship-flavored caveat as any watchlist-return read. Good enough for
    "which direction is this list moving", not a backtest result.

    Reuses get_watchlist()'s own rows rather than a second query so the exclusion
    rules can't drift from what the list view already shows: a row flagged
    no_fresh_price_yet (the DBCORP-class bug, 2026-08-26) is excluded -- its
    current_price is a stale artifact of first_seen_at coinciding with the
    adjusted-price series' ~1-day publish lag, not a real reading, and would
    silently drag the average toward 0%. A row missing either price (no technicals
    row joined yet) is excluded too. price_data_stale rows are NOT excluded --
    same precedent as the per-row price-change cell, which shows a stale move
    rather than hiding it."""
    watchlist = get_watchlist(status=status)
    returns_pct = []
    excluded_count = 0
    for row in watchlist:
        first_seen_price = row.get("first_seen_price")
        current_price = row.get("current_price")
        if row.get("no_fresh_price_yet") or first_seen_price in (None, 0) or current_price is None:
            excluded_count += 1
            continue
        returns_pct.append((current_price - first_seen_price) / first_seen_price * 100.0)
    return {
        "status": status,
        "net_return_pct": (sum(returns_pct) / len(returns_pct)) if returns_pct else None,
        "included_count": len(returns_pct),
        "excluded_count": excluded_count,
        "total_count": len(watchlist),
    }


def _load_draft_theses_by_company(*, active_only: bool = True) -> dict[str, dict]:
    """JSON-safe (see module docstring's _clean_records rationale -- confidence_score/
    target_date/generated_at can round-trip as NaN/Timestamp otherwise) draft L4
    theses, keyed by company_master_id. active_only=True (the default, and
    get_draft_theses()'s own always-on scope: "every draft for a CURRENTLY-ACTIVE
    watchlist company") restricts to companies whose fundamentals_watchlist.status
    is 'active'; get_watchlist() passes active_only=False whenever it itself isn't
    scoped to status="active", so a non-active row's real draft isn't silently
    hidden (see its own call site comment). Same join fundamentals/screens/
    l4_thesis_draft.py's own load_current_drafts_by_company() uses for the digest
    email; not reused directly here because that function's raw .to_dict("records")
    isn't run through _clean_records().

    BUG FOUND LIVE 2026-08-18 (re-audit): this used to query fundamentals_l4_
    thesis_draft directly with no _ensure_draft_table() call first -- the screens-
    side reader this was copied from calls it; this one didn't. Verified live by
    monkeypatching the table name: /api/watchlist, /api/drafts, and
    /api/watchlist/{id} all 500'd on a fresh DB that had never run l4_thesis_
    draft.py's own step yet. Before this fix, get_watchlist() never touched this
    table at all, so the regression was invisible until this endpoint was actually
    exercised against a DB missing the table."""
    _ensure_draft_table()
    active_filter = "WHERE w.status = 'active'" if active_only else ""
    df = sql_to_df(
        f"""
        SELECT d.* FROM fundamentals_l4_thesis_draft d
        JOIN fundamentals_watchlist w ON w.company_master_id = d.company_master_id
        {active_filter}
        ORDER BY d.generated_at DESC NULLS LAST
        """  # noqa: S608 -- active_filter is a fixed internal string, no user input
    )
    return {record["company_master_id"]: record for record in _clean_records(df)}


def get_draft_theses() -> list[dict]:
    """Every CANDIDATE-ONLY draft L4 thesis for a currently-active watchlist company
    (fundamentals_l4_thesis_draft, generated daily by fundamentals/screens/
    l4_thesis_draft.py) -- same data the digest email already shows, now reachable
    without waiting for the next email. Never implies anything was saved to the real
    fundamentals_l4_thesis register; l4_thesis_draft.py never writes that table."""
    return list(_load_draft_theses_by_company().values())



# The five axes confluence_score.py evaluates, in the order its own docstring lists
# them. Kept here as data rather than five ad-hoc keys so the API shape stays stable if
# a sixth axis is ever added (SCORE_VERSION exists for exactly that).
CONFLUENCE_AXES = (
    ("fundamentals_trajectory", "Debt or CWIP ratio improving, with neither worsening"),
    ("event_corroboration", "Recent alerts lean corroborating rather than contradicting"),
    ("sector_cycle", "Sector in capacity discipline, not expansion"),
    ("ownership", "Institutional accumulation; promoter stake steady, pledge not rising"),
    ("valuation", "Cheap against its OWN history, not merely cheap in absolute terms"),
)


def load_confluence_for_company(company_master_id: str) -> dict | None:
    """Latest confluence score for one company, with each axis spelled out.

    The list view only carries the three counts, which is what made the watchlist badge
    ambiguous: "1/4" reads as "1 of 4, rest unknown" when it actually means 1 supportive
    and 3 ACTIVELY CONTRADICTING (evaluable_count == confluence_count +
    contradicting_count). The per-axis detail is what lets a human see WHICH signal
    disagrees rather than just how many.

    A None axis means "not enough data to call either way" and is deliberately never
    coerced to a side -- see confluence_score.py's own docstring.
    """
    _ensure_confluence_score_table()
    df = sql_to_df(
        """
        SELECT run_date, score_version,
               axis_fundamentals_trajectory, axis_event_corroboration, axis_sector_cycle,
               axis_ownership, axis_valuation,
               confluence_count, contradicting_count, evaluable_count
          FROM fundamentals_confluence_score
         WHERE company_master_id = %s
         ORDER BY run_date DESC, score_version DESC
         LIMIT 1
        """,
        params=(company_master_id,),
    )
    records = _clean_records(df)
    if not records:
        return None
    row = records[0]
    return {
        "run_date": row.get("run_date"),
        "score_version": row.get("score_version"),
        "confluence_count": row.get("confluence_count"),
        "contradicting_count": row.get("contradicting_count"),
        "evaluable_count": row.get("evaluable_count"),
        "axes": [
            {
                "key": key,
                "description": description,
                # True supports, False contradicts, None = not evaluable.
                "verdict": row.get(f"axis_{key}"),
            }
            for key, description in CONFLUENCE_AXES
        ],
    }


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
        alert["evidence_bundle"] = loads_lenient(raw_bundle)  # pre-2026-09-23 rows may hold a bare NaN
        alerts.append(alert)

    thesis_df = sql_to_df(
        "SELECT * FROM fundamentals_portfolio_position WHERE company_master_id = %s "
        "ORDER BY opened_at DESC",
        params=(company_master_id,),
    )

    # BUG FOUND LIVE 2026-08-17: unlike get_watchlist()'s list view, the detail page
    # didn't even try to show a draft L4 thesis -- not status-gated the way the list
    # view's own draft lookup is (this is a single-company detail page: a company
    # that has since left the active watchlist still shows its alerts/portfolio here,
    # so its draft shouldn't disappear either).
    # BUG FOUND LIVE 2026-08-18 (re-audit): same missing _ensure_draft_table() call
    # as _load_draft_theses_by_company() -- see its own docstring.
    _ensure_draft_table()
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
        # PRD §12 todo #6 follow-up (2026-09-02): the five axes behind the watchlist's
        # confluence badge. None until confluence_score.py has scored this company.
        "confluence": load_confluence_for_company(company_master_id),
    }


def get_story_scores() -> list[dict]:
    """symbol, story score, primary story, flaws, in-band and watchlist status for every
    company with a live score. Empty (not an error) before the first story-score run."""
    exists = sql_to_df("SELECT 1 FROM information_schema.tables WHERE table_name = 'fundamentals_story_score_live'")
    if exists.empty:
        return []
    df = sql_to_df(
        """
        SELECT coalesce(cm.nse_ticker, replace(l.company_master_id, 'nse:', '')) AS symbol,
               l.company_master_id, l.story_score, l.primary_dimension, l.flaws, l.in_band,
               w.status AS watchlist_status
          FROM fundamentals_story_score_live l
          LEFT JOIN company_master cm ON cm.company_master_id = l.company_master_id
          LEFT JOIN fundamentals_watchlist w ON w.company_master_id = l.company_master_id
         WHERE l.scored_at >= now() - interval '7 days'
        """
    )
    return _clean_records(df)


def get_sectors() -> list[dict]:
    sector_df = sql_to_df(
        """
        SELECT sc.sector_code, sc.capacity_growth_pct, sc.demand_growth_pct, sc.phase,
               sc.capacity_minus_demand_pts, sc.n_companies_with_demand_data,
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
        -- one row per company (dim_security had one per listing), BSE-only names included
        JOIN fundamentals_company_sector ds ON ds.company_master_id = w.company_master_id
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
    """The machine portfolio. There is no human register any more (2026-09-04) -- every
    row here was opened by the ruleset and judged by the entry adjudicator."""
    df = sql_to_df("SELECT * FROM fundamentals_portfolio_position ORDER BY opened_at DESC")
    return _clean_records(df)


def get_portfolio_scoring() -> dict:
    """Thin wrapper over portfolio_resolution.compute_portfolio_scoring -- forecast hit
    rate, failure attribution, time-to-confirmation. Not returns. Read-only and computed
    fresh; nothing here is cached or precomputed.

    Every breakdown is split by resolution_method, because a blended hit rate would let
    judged resolutions flatter the mechanical ones."""
    return compute_portfolio_scoring()


def get_position_sizing(company_master_id: str, *, capital_per_position_rs: float | None = None) -> dict | None:
    """Thin wrapper over l5_sizing.get_position_size_recommendation -- PRD §12
    todo #8. None (-> app.py 404) if this company has no OPEN, ACCEPTED position: the
    calculator refuses to size a name nothing has committed to yet.
    capital_per_position_rs defaults to the operator-set flat allocation."""
    return _get_position_size_recommendation(
        company_master_id, capital_per_position_rs=capital_per_position_rs
    )


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
            WHERE ticker IN (
                -- Through company_master, not string surgery (2026-09-23 audit): the id's
                -- suffix is not the L1 slug for BSE-only names ('nse:543531-BOM' vs
                -- '543531') or NSE names whose slug is a BSE code (ALUFLUOR = 524634),
                -- so ~22 percent of the watchlist showed no company name.
                SELECT cm.nse_ticker FROM company_master cm WHERE cm.company_master_id = a.company_master_id
                UNION SELECT cm.bse_scrip_code FROM company_master cm WHERE cm.company_master_id = a.company_master_id
                UNION SELECT REPLACE(a.company_master_id, 'nse:', ''))
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


# --- Data-platform health: collectors ----------------------------------------
# Reads advisory_sync_state, the per-collector status table. Rides the fundamentals
# API only because it is stockey's sole HTTP surface -- this is monitoring of the data
# platform's own state, not research, so it stays inside the pure-TA boundary (see
# docs/DATA_INVENTORY.md and CLAUDE.md's note on /api/data-health).

# Deliberately unscheduled modules. They are NOT broken -- docs/DATA_INVENTORY.md's
# BORDERLINE section keeps the files for a possible future FnO event-vol use but does
# not run them, so any error they left behind is frozen history, not a live failure.
FROZEN_BY_DESIGN_MODULES = frozenset({
    "data.nseindia.recent_events",
    "data.nseindia.earnings_events",
})


def _live_registry_modules() -> set[str]:
    """Every module the pipeline actually runs today, from the registry itself rather
    than a hand-maintained list -- so this cannot drift the way a copy would."""
    from data.download_runner import DOWNLOADER_STEPS, PARSER_STEPS

    return {step["module"] for step in (*DOWNLOADER_STEPS, *PARSER_STEPS)}


def _live_registry_scopes() -> set[tuple[str, str]]:
    """(module, purpose) pairs the pipeline runs today.

    BUG FOUND LIVE 2026-09-06: advisory_sync_state is keyed by (source_name, scope_key)
    where scope_key is the step's PURPOSE, but the reconcile above matched on module name
    alone. So renaming a step's purpose orphans its old row at whatever status it last
    held -- and that row can never be updated again, because nothing writes that
    (module, purpose) pair any more.

    data.nseindia.offmarket moved from purpose 'market_wide' to 'fundamentals_deal_flow'
    on 2026-08-14. Its market_wide row froze at 'error' and showed as the ONE failing
    collector on the Data Health page for three weeks, while the module itself ran green
    every single day. A permanently-red row for a healthy collector is precisely the
    alert fatigue this whole view exists to prevent.
    """
    from data.download_runner import DOWNLOADER_STEPS, PARSER_STEPS

    return {
        (step["module"], str(step.get("purpose") or ""))
        for step in (*DOWNLOADER_STEPS, *PARSER_STEPS)
    }


def _module_of(source_name: str) -> str:
    """advisory_sync_state keys look like 'download_runner:data.rbi.download_bank_rates',
    'data.nseindia.holidays', or 'continuous_watch:announcements'. The module is the part
    after the last ':'."""
    return source_name.rsplit(":", 1)[-1]


def get_collectors() -> dict:
    """Per-collector status, reconciled against the live registry.

    The reconciliation is the point, not a detail. advisory_sync_state is never pruned,
    so a module that was deleted or renamed keeps its last error row forever -- on
    2026-08-31, 9 rows read 'error' but only 3 were live failures: 3 were phantoms
    (continuous_watch, data.mospi.cpi, data.sharpelydata.sharpely_data -- all removed or
    renamed) and 1 was a frozen-by-design module. A page that shows 9 red rows when 3 are
    real and 3 can never go green trains the operator to ignore it, which is the exact
    alert-fatigue failure this whole view exists to prevent.
    """
    df = sql_to_df(
        """
        SELECT source_name, scope_key, status, error_text,
               last_success_at, last_item_ts, updated_at
          FROM advisory_sync_state
         ORDER BY source_name
        """
    )
    live = _live_registry_modules()
    live_scopes = _live_registry_scopes()
    cleaned = _clean_records(df)
    # Modules that DO have a row under a scope the registry still runs. Only these can
    # have a retired-scope row demoted (below): the pairing is what proves the purpose was
    # renamed rather than the row simply being the module's only record. Without this, a
    # collector whose scope_key never matched a registry purpose -- 8 such values exist,
    # written by the fundamentals and continuous_watch paths -- would have a GENUINE
    # failure demoted to "orphaned" and hidden, which is worse than the stale-red row this
    # demotion exists to remove.
    modules_with_a_current_scope = {
        _module_of(str(r.get("source_name") or ""))
        for r in cleaned
        if (_module_of(str(r.get("source_name") or "")), str(r.get("scope_key") or "")) in live_scopes
    }
    rows: list[dict] = []
    for record in cleaned:
        module = _module_of(str(record.get("source_name") or ""))
        scope = str(record.get("scope_key") or "")
        status = str(record.get("status") or "unknown")
        if module in FROZEN_BY_DESIGN_MODULES:
            classification = "frozen"
        elif module not in live:
            classification = "orphaned"
        elif (
            status != "ok"
            and (module, scope) not in live_scopes
            and module in modules_with_a_current_scope
        ):
            # The module is live, it HAS a row under a scope the registry still runs, and
            # this row's scope is not one of them -- the signature of a renamed purpose.
            # The row is frozen at its last status and can never go green again.
            # Deliberately narrow: only a non-ok row, and only when the replacement row
            # exists. A lone row under an unrecognised scope stays visible as failing,
            # because showing a possibly-stale red is safer than hiding a real one.
            classification = "orphaned"
        elif status == "ok":
            classification = "ok"
        else:
            classification = "failing"
        rows.append({**record, "module": module, "classification": classification})

    # failing first, then frozen/orphaned noise, then healthy.
    order = {"failing": 0, "frozen": 1, "orphaned": 2, "ok": 3}
    rows.sort(key=lambda r: (order.get(r["classification"], 4), r["source_name"]))
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["classification"]] = counts.get(r["classification"], 0) + 1
    return {
        "counts": counts,
        "failing_count": counts.get("failing", 0),
        "registry_module_count": len(live),
        "collectors": rows,
    }


# --- Data-platform health: scheduler ------------------------------------------

def get_scheduler_health() -> dict:
    """Shell out to scripts/is_cron_running.sh --json.

    Deliberately NOT reimplemented in Python. The shell script is the source of truth
    for these checks and is what an operator runs on the box; a second copy here would
    drift from it, which is precisely the class of failure this whole health surface
    exists to prevent (see docs/FRONTEND_COMPLETION_PLAN.md, B3).

    Exit code 1 means "findings present", not "the check failed" -- the payload is
    still valid and is returned. Only an unparseable/absent payload is an error.
    """
    import json
    import subprocess
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "scripts" / "is_cron_running.sh"
    if not script.is_file():
        raise FileNotFoundError(f"{script} is missing")
    proc = subprocess.run(
        [str(script), "--json"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    stdout = (proc.stdout or "").strip()
    if not stdout:
        raise RuntimeError(
            f"is_cron_running.sh produced no output (exit {proc.returncode}): "
            f"{(proc.stderr or '').strip()[:300]}"
        )
    return json.loads(stdout)


# --- Data-platform health: open issues + fallback telemetry -------------------

def get_platform_issues(*, hours: int = 24, limit: int = 100) -> dict:
    """Open identity issues + a fallback-telemetry rollup.

    READ-ONLY on purpose. scripts/issue_digest.py builds a similar report but gets
    there via resolve_open_identity_issues(apply=True), which RECHECKS and CLOSES rows
    as a side effect. An HTTP GET must never mutate pipeline state, so this reuses the
    read helpers (load_open_identity_issues / summarize_fallback_events) and leaves the
    recheck to the nightly job that owns it.

    The identity issues here are the same population the completeness check reports as
    a bare "N symbols have no intraday data" -- this gives that number names and
    reasons.
    """
    from utils.fallback_telemetry import summarize_fallback_events
    from utils.identity_issues import load_open_identity_issues

    issues_df = load_open_identity_issues(limit=limit)
    issues = _clean_records(issues_df)

    by_type: dict[str, int] = {}
    for row in issues:
        key = str(row.get("issue_type") or "unknown")
        by_type[key] = by_type.get(key, 0) + 1

    fallback = summarize_fallback_events(hours=hours)
    return {
        "identity_issues": {
            "open_count": len(issues),
            "by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
            "issues": issues,
            "limit": limit,
        },
        "fallback_events": {"window_hours": hours, **fallback},
    }


# --- Data-platform health: coverage trend -------------------------------------

def get_coverage_report(*, history_days: int = 30) -> dict:
    """Latest per-table coverage/staleness, plus a short staleness history.

    data_coverage_report has been written nightly for 16 runs and read only as
    docs/DATA_COVERAGE.md. The trend is the useful part: a feed degrading shows up as
    rising staleness several days before it fails outright.
    """
    latest = sql_to_df(
        """
        SELECT table_name, category, check_kind, status, rows, symbols,
               min_date, max_date, staleness_days, detail, report_date
          FROM data_coverage_report
         WHERE report_date = (SELECT max(report_date) FROM data_coverage_report)
         ORDER BY (status <> 'ok') DESC, table_name
        """
    )
    trend = sql_to_df(
        """
        SELECT table_name, report_date, staleness_days
          FROM data_coverage_report
         WHERE report_date >= (SELECT max(report_date) FROM data_coverage_report)
                              - make_interval(days => %s)
         ORDER BY table_name, report_date
        """,
        params=(int(history_days),),
    )
    series: dict[str, list[dict]] = {}
    for row in _clean_records(trend):
        series.setdefault(str(row["table_name"]), []).append(
            {"report_date": row["report_date"], "staleness_days": row["staleness_days"]}
        )
    rows = _clean_records(latest)
    return {
        "report_date": rows[0]["report_date"] if rows else None,
        "not_ok_count": sum(1 for r in rows if str(r.get("status")) != "ok"),
        "tables": rows,
        "staleness_history": series,
    }


# --- Ruleset portfolio (docs/PORTFOLIO_RULESET_PRD.md) ------------------------

def get_ruleset_positions() -> dict:
    """Positions opened by the mechanical ruleset + LLM adjudicator.

    Shadow rows are returned alongside real ones and clearly separated: they are
    REJECTED candidates tracked as if taken, which is what makes the adjudicator itself
    falsifiable. A UI that hides them, or blends them into P&L, throws away the only
    paired comparison this design has.
    """
    from fundamentals.screens.portfolio_adjudicator import _ensure_tables

    _ensure_tables()
    df = sql_to_df(
        """
        SELECT position_id, ticker, company_master_id, ruleset_version, kind, status,
               opened_at, entry_price, stop_pct, stop_basis,
               confluence_count, contradicting_count, evaluable_count, stage_at_entry,
               adjudicator_model, adjudicator_prompt_version, adjudicator_reason,
               deferral_count, closed_at, close_reason, exit_price,
               entry_decision, prediction_text, target_date, invalidation_criteria,
               target_date_basis, position_size_rs, adv_cap_rs, sizing_basis,
               -- The live distance to the stop, computed here rather than in the client
               -- so the page and the exit evaluator cannot disagree about what "close to
               -- the stop" means.
               -- Date-bounded: the view sits on a hypertable, and an unbounded "latest
               -- per symbol" per row is the shape behind this workspace's OOM incident.
               (SELECT adj_close FROM advisory_adjusted_ohlcv_daily a
                 WHERE a.symbol = p.ticker
                   AND a.date >= now() - make_interval(days => %s)
                 ORDER BY a.date DESC LIMIT 1) AS last_price,
               entry_price_date, score_version
          FROM fundamentals_portfolio_position p
         ORDER BY (status = 'open') DESC, opened_at DESC
        """,
        params=(PRICE_LOOKBACK_DAYS,),
    )
    rows = _clean_records(df)
    real = [r for r in rows if r["kind"] == "real"]
    shadow = [r for r in rows if r["kind"] == "shadow"]
    # Counted on entry_decision, not kind: in record-only mode EVERY row is kind='shadow'
    # regardless of what the adjudicator said, so kind cannot carry this distinction.
    accepted = [r for r in rows if r["entry_decision"] == "accept"]
    rejected = [r for r in rows if r["entry_decision"] == "reject"]
    return {
        "ruleset_version": max((r["ruleset_version"] for r in rows), default=None),
        "real_count": len(real),
        "shadow_count": len(shadow),
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        # Only ACCEPTED positions consume capital -- vetoed ones are counterfactuals.
        "capital_committed_rs": sum(r["position_size_rs"] or 0 for r in accepted
                                    if r["status"] == "open"),
        # The bucket's figures -- what the runner sizes and caps with. The flat Rs 1 lakh /
        # 100-name constants predate buckets and were shown here after they stopped applying.
        "capital_per_position_rs": capital_per_position_rs(DEFAULT_BUCKET),
        "book_used": sum(1 for r in accepted if r["status"] == "open"),
        "book_capacity": bucket_config(DEFAULT_BUCKET)["target_positions"] or MAX_POSITIONS,
        # record-only until at least one real position exists
        "mode": "live" if real else "record-only",
        "positions": rows,
    }
