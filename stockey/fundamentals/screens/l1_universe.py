"""L1 universe filter -- fundamental screener step 3 (docs/FUNDAMENTAL_SCREENER_PRD.md
sec 8 step 3, fundamental_basic_goal.md sec 1's L1 table).

Crawl-time filtering via one screener.in custom query (decided 2026-08-10), not a
post-hoc SQL pass over the full listed universe -- reuses
fundamentals.collectors.screenerin's auth/query/pagination plumbing
(build_authenticated_session, run_query), same query engine as the deleveraging screen,
different query.

Field-availability audit, done live 2026-08-10 via screener.in's own query-builder
typeahead endpoint (`/api/ratio/search/?q=<term>` -- found by reading
cdn-static.screener.in/js/{query.builder,ratio.gallery,utils}.js, the same JS the
`/screen/new/` page loads) and by testing arithmetic expressions against the raw query
endpoint directly. The query engine accepts arbitrary +,-,*,/ and parentheses between
fields, not just single-field comparisons -- confirmed live, not assumed.

Every filter in fundamental_basic_goal.md's L1 table except two is expressible this
way. Two are not:

- Auditor changes (last 3 years)
- Related-party transactions as % of revenue

`/api/ratio/search/` returns zero results for "auditor", "auditor change", "statutory",
"party", "transactions", "related" -- screener.in does not carry either as structured,
queryable data (footnote-level annual-report disclosure, not tabulated). Per
docs/FUNDAMENTAL_SCREENER_PRD.md sec 8 step 3's own anticipation ("two filters need
checking... if not, those two stay a smaller post-hoc pass over the query's output"):
confirmed unavailable, and no alternate structured source is wired in yet either (a
company's screener.in detail page doesn't carry these any more than the query engine
does). These two checks are DEFERRED, not silently skipped -- every run emits a
fallback_telemetry event recording that, and the run summary carries `checks_deferred`.

Three filters use a documented approximation because screener.in's data granularity
doesn't match the source spec exactly:

- Cash conversion: source spec wants rolling 3yr CFO/EBITDA; screener.in only exposes
  last-year + preceding-year CFO and operating-profit figures (2 data points, not 3) --
  approximated as sum(CFO last+preceding) / sum(operating profit last+preceding).
- Receivables trend: source spec wants an 8-quarter debtor-days trend; screener.in only
  exposes annual (not quarterly) debtor-days figures -- approximated as latest annual
  debtor days vs. debtor days 3 years back (non-rising required to pass).
- Coverage proxy (part of "Neglect"): source spec doesn't specify what to use;
  screener.in has no analyst-coverage field -- approximated as Number of Shareholders
  (a thinly-shareholder-based company is functionally uncovered).

Liquidity-floor and contingent-liabilities thresholds are first-cut placeholders (Rs 10
lakh/day traded value; contingent liabilities < 25% of net worth) -- not precisely
calibrated, easy to tune later, same caveat already carried by the deleveraging screen's
market-cap band. Live 2026-08-10: 188 companies passed (source PRD's own estimate is
"~500-700"; the gap is expected from these placeholder thresholds plus the 2yr-not-3yr
and annual-not-quarterly approximations above, not a bug -- tune thresholds once this is
reviewed, don't chase the PRD's number blindly).
"""

from __future__ import annotations

import json

import pandas as pd

from fundamentals.collectors.screenerin import build_authenticated_session, run_query
from utils.db import sql_to_df, upsert_to_db
from utils.fallback_telemetry import record_local_fallback_event

SYNC_SOURCE_NAME = "fundamentals.screens.l1_universe"
RESULTS_TABLE = "fundamentals_l1_universe"
L1_QUERY_NAME = "l1_universe"
L1_QUERY_VERSION = 1
STOCKEY_RUN_STATE: dict[str, object] = {}

L1_QUERY = (
    "Market Capitalization > 100 AND\n"
    "Market Capitalization < 5000 AND\n"
    "Volume 1month average * Current Price > 1000000 AND\n"
    "FII holding + DII holding < 20 AND\n"
    "Number of Shareholders < 50000 AND\n"
    "(Cash from operations last year + Cash from operations preceding year) / "
    "(Operating profit last year + Operating profit preceding year) >= 0.6 AND\n"
    "Debtor days <= Debtor days 3years back AND\n"
    "Contingent liabilities / Net worth < 0.25"
)

# Not silently skipped -- see module docstring. Every run logs a fallback event naming
# these, and the run summary/STOCKEY_RUN_STATE carries checks_deferred.
DEFERRED_CHECKS = ("auditor_change_last_3_years", "related_party_transactions_pct_revenue")


def _record_deferred_checks_fallback() -> None:
    record_local_fallback_event(
        module=SYNC_SOURCE_NAME,
        source="screenerin",
        fallback_type="l1_checks_not_sourced",
        severity="warn",
        reason=(
            "L1 universe filter ran without auditor-change and related-party-transaction "
            "checks -- confirmed live 2026-08-10 that screener.in exposes neither as "
            "queryable/structured data, and no alternate source is wired in yet."
        ),
        error="no source available",
        metadata={"deferred_checks": list(DEFERRED_CHECKS)},
    )


def run_l1_universe_refresh(session=None) -> dict[str, object]:
    """Run L1_QUERY and upsert into RESULTS_TABLE, keyed by (query_name, query_version,
    run_date, company_id) -- append-only across quarterly refreshes (docs/
    FUNDAMENTAL_SCREENER_PRD.md sec 2: append-only, versioned state, always; a query
    text change bumps L1_QUERY_VERSION rather than silently reinterpreting old rows)."""
    session = session or build_authenticated_session()
    screener_url, companies = run_query(session, L1_QUERY)

    run_date = pd.Timestamp.now(tz="UTC").normalize()
    rows = [
        {
            "query_name": L1_QUERY_NAME,
            "query_version": L1_QUERY_VERSION,
            "query_text": L1_QUERY,
            "run_date": run_date,
            "company_id": company["company_id"],
            "company_name": company["name"],
            "ticker": company["ticker"],
            "company_url": company["url"],
            "metrics_json": json.dumps(company["metrics"], ensure_ascii=False, default=str),
            "screener_url": screener_url,
            "load_ts": pd.Timestamp.now(tz="UTC"),
        }
        for company in companies
    ]
    if rows:
        upsert_to_db(
            pd.DataFrame(rows),
            RESULTS_TABLE,
            unique_keys=["query_name", "query_version", "run_date", "company_id"],
        )
    _record_deferred_checks_fallback()
    return {
        "query_name": L1_QUERY_NAME,
        "query_version": L1_QUERY_VERSION,
        "rows": len(rows),
        "checks_deferred": list(DEFERRED_CHECKS),
        "companies": [c["name"] for c in companies][:20],
    }


def load_l1_universe_tickers() -> pd.DataFrame:
    """The latest L1 refresh's (company_id, company_name, ticker) rows -- shared reader
    for downstream steps (L2 state, L3 feeds) that need "which companies are we
    watching", not the full query-result metrics blob. Ticker here is the NSE symbol
    (screener.in's own ticker slug), the same identity callers already resolve to
    company_master via utils.company_master.map_company_master_ids(..., exchange="NSE")."""
    return sql_to_df(
        """
        SELECT company_id, company_name, ticker
        FROM fundamentals_l1_universe
        WHERE run_date = (SELECT MAX(run_date) FROM fundamentals_l1_universe)
        ORDER BY company_id
        """
    )


def main() -> int:
    global STOCKEY_RUN_STATE
    result = run_l1_universe_refresh()
    STOCKEY_RUN_STATE = {
        "source": SYNC_SOURCE_NAME,
        "rows": result["rows"],
        "rows_written": result["rows"],
        "query_name": result["query_name"],
        "query_version": result["query_version"],
        "checks_deferred": result["checks_deferred"],
        "fallback_used": True,  # deferred checks are a standing, visible fallback
        "state_advanced": result["rows"] > 0,
    }
    print(json.dumps({"status": "ok", **STOCKEY_RUN_STATE}, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
