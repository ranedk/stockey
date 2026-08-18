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
checking... if not, those two stay a smaller post-hoc pass over the query's output"),
that's exactly what run_l1_universe_refresh does as of 2026-08-13: screener.in has
neither, but BSE announcement filings do (confirmed live -- both are real, exchange-
filed disclosures, see fundamentals/collectors/bse_announcements.py's AUDITOR_CHANGE_
KEYWORDS/RELATED_PARTY_TRANSACTION_KEYWORDS and structured_extraction.py's
AUDITOR_CHANGE_SCHEMA/RPT_SCHEMA). apply_post_hoc_exclusions() runs this pass over
screener.in's own candidate list after the query above, using events already
collected+extracted by the daily fundamentals pipeline -- not a live fetch here.
DEFERRED_CHECKS is now empty; see its own docstring for the exclusion policy
(missing/unbackfilled event history never excludes, only a positively-confirmed
violation does).

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
from utils.company_master import map_company_master_ids_nse_or_bse
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

# Empty as of 2026-08-13 -- both checks below are built. Kept as a named constant
# (not deleted) so a future genuinely-unsourceable check has somewhere to go, and so
# checks_deferred stays a meaningful field on the run summary rather than
# disappearing silently.
DEFERRED_CHECKS: tuple[str, ...] = ()


def _record_deferred_checks_fallback() -> None:
    """Only called when DEFERRED_CHECKS is non-empty (see run_l1_universe_refresh) --
    kept general (not auditor/RPT-specific wording) since those two are now built."""
    record_local_fallback_event(
        module=SYNC_SOURCE_NAME,
        source="screenerin",
        fallback_type="l1_checks_not_sourced",
        severity="warn",
        reason=f"L1 universe filter ran without: {', '.join(DEFERRED_CHECKS)} -- no source available yet.",
        error="no source available",
        metadata={"deferred_checks": list(DEFERRED_CHECKS)},
    )

# Auditor-change / RPT post-hoc exclusion (2026-08-13, built): screener.in has neither
# as structured/queryable data (confirmed live 2026-08-10), but BSE announcements do
# (confirmed live 2026-08-13 against 40 real companies' 3yr history) -- see
# fundamentals/collectors/bse_announcements.py's AUDITOR_CHANGE_KEYWORDS/RELATED_
# PARTY_TRANSACTION_KEYWORDS and structured_extraction.py's AUDITOR_CHANGE_SCHEMA/
# RPT_SCHEMA. L1 is therefore a two-stage filter: screener.in's arithmetic query
# first, then this post-hoc pass over its own candidates. Missing data (no
# auditor_change/RPT event on file for a company at all) NEVER excludes -- absence
# isn't evidence of a violation, same "null is correct far more often than a guessed
# value" rule the rest of this pipeline applies everywhere. This does mean a company
# whose 3yr BSE history hasn't been backfilled yet (fundamentals/collectors/
# bse_announcements.py's daily crawl only looks back 7 days -- see its own
# one-time-backfill mechanism) passes by default, not "confirmed clean" -- run_state's
# own excluded_* lists only ever show POSITIVE exclusions, never a coverage claim.
AUDITOR_CHANGE_LOOKBACK_YEARS = 3
# SEBI LODR Regulation 23's own materiality threshold for RPTs requiring shareholder
# approval (10% of annual consolidated turnover, or Rs 1000cr, whichever lower) --
# reused here rather than inventing a new number, since fundamental_basic_goal.md's
# own L1 table doesn't specify one.
RPT_PCT_OF_REVENUE_THRESHOLD = 10.0


def load_auditor_rpt_events_for_companies(company_master_ids: list[str]) -> pd.DataFrame:
    """Every structured-extracted auditor_change/related_party_transaction event for
    the given companies -- unfiltered by date/threshold here (that's
    apply_post_hoc_exclusions' job below), just the raw candidate rows."""
    if not company_master_ids:
        return pd.DataFrame()
    return sql_to_df(
        """
        SELECT company_master_id, filing_type, disclosure_date, structured_extraction_json
        FROM fundamentals_events
        WHERE company_master_id = ANY(%s) AND filing_type IN ('auditor_change', 'related_party_transaction')
          AND structured_extraction_status = 'done' AND structured_extraction_json IS NOT NULL
        """,
        params=(company_master_ids,),
    )


def _auditor_change_excludes(events: list[dict], *, as_of: pd.Timestamp) -> bool:
    cutoff = as_of - pd.DateOffset(years=AUDITOR_CHANGE_LOOKBACK_YEARS)
    for event in events:
        try:
            extracted = json.loads(event["structured_extraction_json"])
        except (TypeError, ValueError):
            continue
        # Only a CONFIRMED change excludes -- a proposed_change_agenda, incidental_
        # mention, or other never does. See AUDITOR_CHANGE_SCHEMA's own docstring for
        # why the classifier deliberately over-matches and this field is the real gate.
        if extracted.get("disclosure_type") != "confirmed_change":
            continue
        # utc=True: disclosure_date is a plain "YYYY-MM-DD" text column (no offset of
        # its own) -- without this, to_datetime returns a tz-naive Timestamp that
        # can't be compared against `cutoff` (tz-aware, from `as_of`), which is a real
        # bug this module's own tests caught live, not a hypothetical.
        disclosure_date = pd.to_datetime(event.get("disclosure_date"), errors="coerce", utc=True)
        if pd.notna(disclosure_date) and disclosure_date >= cutoff:
            return True
    return False


def rpt_is_material(extracted: dict) -> tuple[bool, str]:
    """True if an already-extracted RPT_SCHEMA payload crosses SEBI LODR Reg 23's
    materiality threshold, plus which field it was decided on ("pct"/"amount"/
    "not_applicable"/"unknown") so a caller can be honest in its own reasoning text.

    BUG FOUND LIVE 2026-08-18 (re-audit): both callers of this logic (this function's
    predecessor here, and l3_triggers.py's evaluate_related_party_transaction_
    trigger) used to gate purely on pct_of_revenue -- but RPT_SCHEMA's own
    description says that field is populated "only if the filing itself states this
    percentage", while rpt_amount_rs_cr (what filings actually carry) was read only
    for display text, never for the materiality decision itself. A material RPT
    stated as an amount, not a percentage, silently never excluded/alerted. Falls
    back to "material because we can't rule it out": any positive rpt_amount_rs_cr
    with no stated percentage counts as material -- favors recall over precision,
    matching fundamental_basic_goal.md's own "judge it on recall and falsifiability"
    framing already cited elsewhere in this codebase, rather than inventing an
    amount-only threshold with no revenue figure to compute a true percentage from."""
    if not extracted.get("is_applicable"):
        return False, "not_applicable"  # non-applicability declaration -- the common real case
    pct = extracted.get("pct_of_revenue")
    if isinstance(pct, (int, float)):
        return pct >= RPT_PCT_OF_REVENUE_THRESHOLD, "pct"
    amount = extracted.get("rpt_amount_rs_cr")
    if isinstance(amount, (int, float)) and amount > 0:
        return True, "amount"
    return False, "unknown"


def _rpt_excludes(events: list[dict]) -> bool:
    for event in events:
        try:
            extracted = json.loads(event["structured_extraction_json"])
        except (TypeError, ValueError):
            continue
        material, _basis = rpt_is_material(extracted)
        if material:
            return True
    return False


def apply_post_hoc_exclusions(companies: list[dict]) -> tuple[list[dict], dict[str, list[str]]]:
    """Filters screener.in's own candidate list against real auditor-change/RPT
    events -- see module-level comment above for the exclusion policy.

    BUG FOUND LIVE 2026-08-15, fixed here: company_master_id used to be built as a
    naive 'nse:'+ticker string -- wrong for the ~22% of tickers that are actually
    raw BSE numeric scrip codes (screener.in's own company-URL slug for some
    listings), confirmed live via load_sector_codes_for_tickers' own docstring in
    fundamentals/screens/l2_state.py (same bug, same root cause, fixed there too).
    A mismatched company_master_id here means load_auditor_rpt_events_for_companies
    would query the WRONG id and silently find nothing, letting a company that
    should be excluded for a real auditor-change/RPT violation stay in L1 forever.
    Resolved via map_company_master_ids_nse_or_bse instead, the same helper every
    other identity lookup in this codebase already uses."""
    tickers_with_value = [c["ticker"] for c in companies if c.get("ticker")]
    cmid_by_ticker: dict[str, str] = {}
    if tickers_with_value:
        resolved = map_company_master_ids_nse_or_bse(pd.Series(tickers_with_value, dtype="string"))
        cmid_by_ticker = {t: cmid for t, cmid in zip(tickers_with_value, resolved) if pd.notna(cmid)}

    company_master_ids = list(dict.fromkeys(cmid_by_ticker.values()))
    events_df = load_auditor_rpt_events_for_companies(company_master_ids)

    events_by_company: dict[str, list[dict]] = {}
    if not events_df.empty:
        for row in events_df.to_dict("records"):
            events_by_company.setdefault(row["company_master_id"], []).append(row)

    as_of = pd.Timestamp.now(tz="UTC")
    excluded_auditor_change: list[str] = []
    excluded_related_party_transaction: list[str] = []
    survivors: list[dict] = []
    for company in companies:
        ticker = company.get("ticker")
        cmid = cmid_by_ticker.get(ticker) if ticker else None
        events = events_by_company.get(cmid, []) if cmid else []
        auditor_events = [e for e in events if e["filing_type"] == "auditor_change"]
        rpt_events = [e for e in events if e["filing_type"] == "related_party_transaction"]

        if _auditor_change_excludes(auditor_events, as_of=as_of):
            excluded_auditor_change.append(company["name"])
            continue
        if _rpt_excludes(rpt_events):
            excluded_related_party_transaction.append(company["name"])
            continue
        survivors.append(company)

    return survivors, {
        "excluded_auditor_change": excluded_auditor_change,
        "excluded_related_party_transaction": excluded_related_party_transaction,
    }


def run_l1_universe_refresh(session=None) -> dict[str, object]:
    """Run L1_QUERY, then apply_post_hoc_exclusions over its own candidates, and
    upsert survivors into RESULTS_TABLE, keyed by (query_name, query_version,
    run_date, company_id) -- append-only across quarterly refreshes (docs/
    FUNDAMENTAL_SCREENER_PRD.md sec 2: append-only, versioned state, always; a query
    text change bumps L1_QUERY_VERSION rather than silently reinterpreting old rows).
    Excluded companies are logged (excluded_auditor_change/excluded_related_party_
    transaction) but not written anywhere -- same "flags are exclusions, not scores"
    treatment the rest of L1 already applies, nothing downstream needs to know why a
    company isn't there."""
    session = session or build_authenticated_session()
    screener_url, screened_companies = run_query(session, L1_QUERY)
    companies, exclusions = apply_post_hoc_exclusions(screened_companies)

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
    if DEFERRED_CHECKS:
        _record_deferred_checks_fallback()
    return {
        "query_name": L1_QUERY_NAME,
        "query_version": L1_QUERY_VERSION,
        "rows": len(rows),
        "checks_deferred": list(DEFERRED_CHECKS),
        "excluded_auditor_change": exclusions["excluded_auditor_change"],
        "excluded_related_party_transaction": exclusions["excluded_related_party_transaction"],
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
        # 2026-08-13 gap fixes (audit finding): these were computed by
        # run_l1_universe_refresh already but never forwarded here, so a company's
        # exclusion reason was invisible past the return dict, not even logged.
        # Real exclusions are the filter working as intended, not a degradation --
        # kept as their own fields, NOT folded into fallback_used below.
        "excluded_auditor_change": result["excluded_auditor_change"],
        "excluded_related_party_transaction": result["excluded_related_party_transaction"],
        # Was hardcoded True with a comment claiming "deferred checks are a standing
        # fallback" -- stale the moment DEFERRED_CHECKS became empty this session
        # (both checks it referred to are now built), so this was permanently wrong
        # (always signaling "something needs attention" on every clean run).
        "fallback_used": bool(result["checks_deferred"]),
        "state_advanced": result["rows"] > 0,
    }
    print(json.dumps({"status": "ok", **STOCKEY_RUN_STATE}, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
