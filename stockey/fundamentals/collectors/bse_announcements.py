"""BSE announcements/PIT-SAST/results-calendar crawler -- fundamental screener step 5,
BSE half (docs/FUNDAMENTAL_SCREENER_PRD.md sec 8 step 5, fundamental_basic_goal.md sec
3.2/3.3). BSE is the primary L3 feed per the source PRD ("no equivalent of NSE's
cookie/session gate", and a meaningful share of the microcap universe is BSE-only
listed) -- this module covers three of L3's four triggers in one crawler; the fourth
(rating-action *enrichment*, as opposed to detection) is a later, separate step.

Endpoints reverse-engineered from BennyThadikaran/BseIndiaApi (github.com/
BennyThadikaran/BseIndiaApi, `pip install bse`) per the source PRD's explicit
instruction -- reference material only, no runtime dependency taken; confirmed against
that repo's own committed sample JSON, not invented:

- `AnnSubCategoryGetData/w` (scripcode-scoped, `strCat=-1` for every category in one
  call -- BSE only echoes CATEGORYNAME back when you filter server-side by category,
  but SUBCATNAME is reliably populated either way per the reference sample, so
  classify_announcement() keys off SUBCATNAME/HEADLINE text instead of requiring a
  second, category-filtered request per company).
- `Corpforthresults/w` (market-wide, no scripcode -- the source of trigger #4's
  "calendar" half; filtered to the L1 universe in Python rather than one request per
  company, since this endpoint is already a bounded market-wide listing, not a
  firehose the way general announcements are).

Crawl-time scoped to the L1 universe (per company BSE scrip code, resolved via
company_master.bse_scrip_code -- the step-0 backfill this module now actually
consumes), not a full-market announcement pull -- matches the source PRD's explicit
"we will not look at ALL announcements but only specific ones" instruction and L1's
own crawl-time-filtering precedent. Only rows that classify into one of L3's three
detectable-here trigger types (pit_sast, rating_action, results) are stored; everything
else (AGM notices, routine compliance filings, etc.) is discarded, not archived --
"OCR only the specific filing types tied to the four L3 triggers... removes most of
what made the old announcement pipeline expensive" (docs/FUNDAMENTAL_SCREENER_PRD.md
sec 2). Beyond the source PRD's original four, three more types were added the same
discard-if-uninteresting way: capital_raise (2026-08-12, user request: "company
getting money through any means is an important signal"), and auditor_change/
related_party_transaction (2026-08-13, closing L1 universe filter's two
DEFERRED_CHECKS -- screener.in has neither as structured data, but BSE announcements
do, confirmed live; see fundamentals/screens/l1_universe.py's own docstring for why
these two were deferred in the first place).

Every row is detection only -- enrichment_status stays "pending" here always. Actual
content extraction (OCR + structured fields) is step 6, wired in separately per
build-order ("detection via exchange feed must be reliable; enrichment via agency site
may fail silently and retry tomorrow" -- fundamental_basic_goal.md sec 3.3).

Block-safety (explicit user instruction 2026-08-10, echoing the 2026-08-09/10 NSE WAF
incident): every request goes through the existing bse-domain
exchange_request_gate (10s floor, no parallel requests, same discipline that's
already proven against BSE for the scrip-code backfill). On top of that, this module
adds a circuit breaker -- CIRCUIT_BREAKER_THRESHOLD consecutive failed requests (non-
200, unparseable JSON, or a response missing the expected Table/Table1 shape) stops
the WHOLE run immediately, before touching BSE again. No retry-in-a-tight-loop, no
"just this one more attempt" -- a tripped breaker returns `blocked=True` and however
many companies were already completed; the next scheduled run picks up the rest
(idempotent, upsert-keyed). This is deliberately more conservative than a single
company's fetch failing (which just skips that company and keeps going, matching L2's
"one bad company can't sink the batch" -- see fundamentals/screens/l2_state.py).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
from environs import Env

from fundamentals.collectors.events_store import RESULTS_TABLE, _existing_event_keys, resolve_isin, upsert_events_with_dedup
from fundamentals.collectors.security_master import BSE_HEADERS
from fundamentals.screens.l1_universe import load_l1_universe_tickers
from fundamentals.screens.l2_state import pull_crawl_forward
from utils.company_master import map_company_master_ids_nse_or_bse
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.exchange_rate_limiter import exchange_request_gate
from utils.fallback_telemetry import record_local_fallback_event
from utils.schema_migrations import apply_schema_migration


env = Env()
env.read_env()

BSE_API_BASE = "https://api.bseindia.com/BseIndiaAPI/api"
ANNOUNCEMENTS_URL = f"{BSE_API_BASE}/AnnSubCategoryGetData/w"
RESULT_CALENDAR_URL = f"{BSE_API_BASE}/Corpforthresults/w"

SYNC_SOURCE_NAME = "fundamentals.collectors.bse_announcements"
STOCKEY_RUN_STATE: dict[str, object] = {}

LOOKBACK_DAYS = env.int("BSE_ANNOUNCEMENTS_LOOKBACK_DAYS", 7)
# Consecutive-failure trip wire -- see module docstring's "Block-safety" section.
# Deliberately small: a handful of genuinely-different failures (e.g. a couple of
# companies with malformed data) is normal noise; anything sustained past this many
# in a row is far more likely BSE pushing back than coincidence.
CIRCUIT_BREAKER_THRESHOLD = 3

# BSE's own category constant for this feed (confirmed against BennyThadikaran/
# BseIndiaApi's src/bse/constants.py -- CATEGORY.INSIDER) -- not used as a request
# filter here (see module docstring), kept only as the canonical string this module's
# classifier matches against SUBCATNAME/HEADLINE text.
RATING_AGENCY_KEYWORDS = (
    "crisil",
    "icra",
    "care ratings",
    "care rating",
    "india ratings",
    "acuite",
    "acuité",
    "brickwork",
    "infomerics",
)

# "Company getting money through any means is an important signal" (user, 2026-08-12)
# -- preferential allotment / QIP / rights issue / warrant conversion / FCCB. Checked
# against subcategory AND headline together (not subcategory-first-then-headline-
# fallback like PIT_SAST/rating_action above) because confirmed live 2026-08-12: a
# real warrant-conversion allotment ("Allotment of Equity Shares Pursuant to
# Conversion of Warrants") was filed under BSE's generic 'Outcome without intimation'
# subcategory -- SUBCATNAME alone would have missed it entirely, only the headline
# carried the signal.
CAPITAL_RAISE_KEYWORDS = (
    "preferential issue",
    "preferential allotment",
    "preferential basis",
    "qualified institutions placement",
    "qip",
    "rights issue",
    "conversion of warrant",
    "allotment of warrant",
    "allotment of equity share",
    "further issue of capital",
)

# Confirmed live 2026-08-12: a real "Allotment of NCDs to <lender>" headline was
# filed under BSE's 'Allotment of Equity Shares' subcategory despite being a debt
# instrument (non-convertible debenture), not equity -- CAPITAL_RAISE_KEYWORDS'
# subcategory match alone would have misclassified a lending event as the
# equity-investor-entry signal this trigger is for (the user's own framing was "high
# quality investor INVESTING", not a lender). Excluded via headline check regardless
# of which subcategory the row landed in.
DEBT_INSTRUMENT_EXCLUSION_KEYWORDS = ("ncd", "non-convertible debenture", "debenture")

# BUG FOUND LIVE 2026-08-17 (see classify_announcement's own capital_raise comment):
# forward-looking capital_raise-shaped filings -- nothing actually allotted/raised
# yet -- confirmed against 2 real rows, both a "General"-subcategory compliance
# certificate about a still-"Proposed Preferential Issue" and an intimation about a
# rights-issue-committee meeting "scheduled to be held" that was then postponed.
CAPITAL_RAISE_FORWARD_LOOKING_HEADLINE_KEYWORDS = ("proposed preferential", "scheduled to be held", "is scheduled on")

# BUG FOUND LIVE 2026-08-18: re-audit of all 24 stored capital_raise rows found at
# least 10 false positives the checks above miss, in three distinct shapes -- none
# of these represent money actually changing hands from a new investor:
#  1. Forward-looking, different wording than CAPITAL_RAISE_FORWARD_LOOKING_HEADLINE_
#     KEYWORDS above: an "in-principle" listing approval is exchange sign-off to
#     proceed, not an actual allotment (real row: BSE/NSE "have granted 'in-principle'
#     approvals for preferential issue of ... convertible warrants").
#  2. Administrative follow-ups to an allotment already captured under its own,
#     separate row: trading-approval receipt, listing-approval receipt, a rights-
#     issue closure notice, ISIN intimation for rights entitlements, and a proceeds-
#     utilization statement (including one filed under BSE's own "Reg. 32 (1),(3) --
#     Statement of Deviation & Variation" subcategory, which is itself specifically
#     about how ALREADY-raised proceeds are being spent, not a new raise) all
#     mention CAPITAL_RAISE_KEYWORDS phrases (a prior allotment's "preferential
#     basis"/"conversion of warrant" wording is quoted back in the follow-up text)
#     without describing a new one.
#  3. An RTA (registrar and transfer agent) re-appointment notice, real row: BSE
#     filed it under the "Allotment of Warrants" subcategory despite the headline
#     being entirely about appointing a new RTA -- unrelated to any raise.
#  4. Employee stock option exercises: real row's headline ("...allotment of equity
#     shares pursuant to exercise of options under Employee Stock option") matches
#     CAPITAL_RAISE_KEYWORDS' "allotment of equity share" phrase, but an employee
#     exercising options already granted is not the external-investor-entry signal
#     this trigger is for (compare CAPITAL_RAISE_KEYWORDS' own docstring: "high
#     quality investor INVESTING").
CAPITAL_RAISE_NON_EVENT_HEADLINE_KEYWORDS = (
    "in-principle",
    "in principle",
    "trading approval",
    "listing approval",
    "closure of rights issue",
    "for the rights entitlement",
    "utilization of proceeds",
    "utilization of fund",
    "employee stock option",
    "registrar and transfer agent",
    "registrar and share transfer agent",
    "new rta",
)

# L1 universe filter's two DEFERRED_CHECKS (auditor change, RPT) -- 2026-08-13, built
# once fundamentals/screens/l1_universe.py's own docstring turned out to be checking
# the wrong source (screener.in has neither; BSE announcements have both). Confirmed
# live against 25 real companies' 3yr BSE history.
#
# Auditor: "statutory auditor" alone catches every real shape seen (appointment,
# resignation, and a combined appointment-of-statutory-and-secretarial-auditor
# filing) -- BSE's SUBCATNAME is inconsistent about which of the two happened
# (appointment vs resignation), so change_type is left to structured_extraction.py's
# AUDITOR_CHANGE_SCHEMA, not this classifier.
AUDITOR_CHANGE_KEYWORDS = ("statutory auditor",)

# RPT: SEBI LODR Regulation 23 is exclusively "Related Party Transactions" (unlike
# Regulation 29 above, which the pit_sast lesson found covers multiple unrelated
# topics) -- both real matches found live cited "regulation 23" without the phrase
# "related party transaction" anywhere in the headline (both were non-applicability
# declarations: "non applicability of regulation 23(9) of SEBI LODR"), so the bare
# regulation-number match is kept here, not dropped the way the pit_sast lesson
# dropped Regulation 29. Lower stakes than that case anyway: a false-positive here
# only costs one wasted OCR+extraction call, not a spurious alert (l1_universe.py's
# exclusion only fires on a real extracted pct_of_revenue over threshold, not on
# filing_type alone).
RELATED_PARTY_TRANSACTION_KEYWORDS = ("related party transaction", "regulation 23")


def _record_fallback(fallback_type: str, *, reason: str, error, severity: str = "warn", metadata=None) -> None:
    record_local_fallback_event(
        module=SYNC_SOURCE_NAME,
        source="bse",
        fallback_type=fallback_type,
        severity=severity,
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


PIT_SAST_KEYWORDS = (
    "insider trading",
    "sast",
    "substantial acquisition",
    "prohibition of insider",
    # Confirmed live 2026-08-10 against real BSE announcement history (SUBCATNAME=
    # "Closure of Trading Window" appeared with no "insider"/"sast" substring at all --
    # the first classifier draft missed it entirely): trading-window control notices
    # are the other recurring PIT-related phrasing BSE uses.
    "trading window",
    # Deliberately NOT matching bare "regulation 29"/"regulation 31"/"reg. 29"/
    # "reg. 31" here -- found live 2026-08-11 via structured_extraction.py's own
    # output on a real row: those regulation NUMBERS are overloaded across two
    # unrelated regulation sets. SEBI (SAST) Regulations has a Reg 29 (substantial
    # acquisition disclosure), but SEBI LODR *also* has its own Reg 29 (prior
    # intimation of a board meeting -- a routine, frequent filing with nothing to do
    # with insider trading) and its own Reg 31 (routine quarterly shareholding-pattern
    # disclosure). A real "Prior Intimation of Board Meeting pursuant to Regulation 29"
    # notice (SUBCATNAME="Board Meeting", genuinely unrelated to PIT/SAST) was
    # misclassified as pit_sast purely because its headline cited "Regulation 29" --
    # BSE's own subcategory had already correctly said "Board Meeting", and the
    # headline-fallback keyword match overrode that with a false positive. Real SAST
    # filings reliably name "SAST" or "insider trading" explicitly in their own text
    # (confirmed: this doesn't cost real recall -- the existing regression test for a
    # genuine "Regulation 29(2) of SEBI (SAST) Regulations" headline still matches via
    # the "sast" keyword alone), so dropping the bare regulation-number match trades
    # nothing real away while removing the LODR-collision false-positive risk.
)


def classify_announcement(subcategory: str | None, headline: str | None) -> str:
    """Classify one BSE announcement row into an L3 trigger type. Keys primarily off
    SUBCATNAME -- BSE's own assigned filing-type label, not free text -- rather than
    BSE's CATEGORYNAME field (comes back null on an unfiltered strCat=-1 request,
    confirmed against the reference sample response, see module docstring).

    Falls back to matching the same keywords against HEADLINE only for pit_sast/
    rating_action (subcategory is sometimes missing/generic for these two).

    results DOES now have one narrow headline fallback (2026-08-13, added after a
    live audit of 30 companies' real BSE history found real results outcomes being
    silently dropped): BSE very commonly subcategorizes the actual results outcome
    as "Outcome of Board Meeting", not "Financial Results" -- not rare, the majority
    shape in that audit. Distinguished from a "Newspaper Publication" re-publication
    filing (subcategory "Newspaper Publication" of the same content, still excluded
    -- the original 2026-08-10 false-positive concern: its headline also contains
    "financial results" without being the source filing) and from a forward-looking
    notice (BSE uses the distinct subcategory "Board Meeting", without "Outcome of",
    for "meeting is scheduled on...to consider..." -- nothing to extract yet) by
    requiring subcategory to be EXACTLY the "outcome of" variant, not a bare
    headline match. This check runs before auditor_change/related_party_transaction
    below on purpose: a real row was found live matching both ("Approval of
    Standalone Audited Financial Results along with Statutory Auditor's Report...")
    and silently landing on auditor_change, dropping that quarter's numbers entirely
    -- results must win that overlap. Known smaller residual gap, not solved here:
    a "Revision of outcome" subcategory (re-submitted/corrected results) was seen
    once in the same audit -- much lower volume, not worth broadening this keyword
    set for on a single observation.

    capital_raise checks subcategory and headline together, not subcategory-first --
    confirmed live 2026-08-12 that BSE's subcategory for this type is unreliable (a
    real warrant-conversion allotment landed under a generic 'Outcome without
    intimation' bucket) -- and excludes debt-instrument headlines (NCD/debenture)
    even on a subcategory match, see CAPITAL_RAISE_KEYWORDS/
    DEBT_INSTRUMENT_EXCLUSION_KEYWORDS. Also excludes forward-looking/non-event
    headlines (in-principle approvals, post-allotment administrative follow-ups,
    RTA-appointment notices, ESOP exercises) even on a keyword match -- see
    CAPITAL_RAISE_NON_EVENT_HEADLINE_KEYWORDS.
    """
    subcategory_text = (subcategory or "").lower()
    headline_text = (headline or "").lower()

    if any(keyword in subcategory_text for keyword in PIT_SAST_KEYWORDS):
        return "pit_sast"
    if "financial result" in subcategory_text:
        return "results"
    if "outcome of board meeting" in subcategory_text and "financial result" in headline_text:
        return "results"
    if "credit rating" in subcategory_text or "rating action" in subcategory_text or any(
        keyword in subcategory_text for keyword in RATING_AGENCY_KEYWORDS
    ):
        return "rating_action"
    if any(keyword in subcategory_text for keyword in CAPITAL_RAISE_KEYWORDS) or any(
        keyword in headline_text for keyword in CAPITAL_RAISE_KEYWORDS
    ):
        # BUG FOUND LIVE 2026-08-17: unlike the sibling `results` check above (which
        # excludes a plain "Board Meeting" subcategory -- forward-looking, nothing
        # decided yet -- from a completed "Outcome of Board Meeting"), capital_raise
        # had no equivalent exclusion at all. Confirmed live: of 29 real capital_raise
        # rows, 2 were "Board Meeting" subcategory (scheduled, not yet held -- exact
        # match against subcategory_text, NOT a substring check, since "board
        # meeting" is also a substring of the genuinely-completed "outcome of board
        # meeting"), 1 was a "Postal Ballot" notice (seeking shareholder approval,
        # nothing approved yet), and 1 was a "General"-subcategory compliance
        # certificate headlined "...wrt the Proposed Preferential Issue" (still
        # proposed, nothing allotted). None of these four had actually raised
        # anything -- exactly the "forward-looking agenda, not completed" false
        # positive the results check next door already guards against.
        if subcategory_text.strip() == "board meeting":
            return "other"
        if subcategory_text.strip() == "postal ballot":
            return "other"
        if any(keyword in headline_text for keyword in CAPITAL_RAISE_FORWARD_LOOKING_HEADLINE_KEYWORDS):
            return "other"
        if any(keyword in headline_text for keyword in CAPITAL_RAISE_NON_EVENT_HEADLINE_KEYWORDS):
            return "other"
        if not any(keyword in headline_text for keyword in DEBT_INSTRUMENT_EXCLUSION_KEYWORDS):
            return "capital_raise"

    if any(keyword in headline_text for keyword in PIT_SAST_KEYWORDS):
        return "pit_sast"
    if "credit rating" in headline_text or "rating action" in headline_text or any(
        keyword in headline_text for keyword in RATING_AGENCY_KEYWORDS
    ):
        return "rating_action"

    # auditor_change / related_party_transaction (2026-08-13, L1 universe filter's
    # two DEFERRED_CHECKS -- see AUDITOR_CHANGE_KEYWORDS/RELATED_PARTY_TRANSACTION_
    # KEYWORDS docstrings). Checked against subcategory AND headline together, same
    # shape as capital_raise above -- "statutory auditor" appears in real filings
    # under SUBCATNAME values as specific as "Resignation of Statutory Auditors" and
    # as generic as "Change in Management"/"EGM", so subcategory alone would miss
    # real cases. Deliberately broad (not narrowed to "appointment of statutory
    # auditor"-style compound phrases): a real appointment headline was found live
    # phrased as "...approved the appointment of M/s. Borkar & Muzumdar...as the
    # Statutory Auditor" -- "appointment" and "statutory auditor" are not adjacent,
    # so a compound-phrase match would have missed it. The two confirmed-live
    # incidental-mention shapes (subcategory literally "Financial Results", and
    # "Outcome of Board Meeting" + a results-shaped headline) are now caught by the
    # results checks above this one and never reach here at all (2026-08-13 fix --
    # see this function's own docstring). Any OTHER incidental mention this
    # classifier still lets through (e.g. a "General"-subcategory row citing
    # "statutory auditor" with no results-shaped headline) is deliberately still
    # pushed to structured_extraction.py's AUDITOR_CHANGE_SCHEMA disclosure_type
    # field rather than solved here -- same "classify broadly, let extraction judge
    # precisely" split PIT_SAST_SCHEMA's own disclosure_type already uses.
    if any(keyword in subcategory_text for keyword in AUDITOR_CHANGE_KEYWORDS) or any(
        keyword in headline_text for keyword in AUDITOR_CHANGE_KEYWORDS
    ):
        return "auditor_change"
    if any(keyword in headline_text for keyword in RELATED_PARTY_TRANSACTION_KEYWORDS):
        return "related_party_transaction"
    return "other"


def resolve_company_identity(tickers: pd.Series) -> pd.DataFrame:
    """L1-universe ticker -> (company_master_id, bse_scrip_code, isin), via
    company_master/dim_security -- reuses the step-0 backfill (fundamentals/
    collectors/security_master.py) rather than looking scrip codes up again here, and
    fundamentals.collectors.events_store.resolve_isin for the cross-source dedup key.
    NSE first, BSE-ticker fallback (map_company_master_ids_nse_or_bse) since
    screener.in can report a company's BSE scrip code even when it's genuinely
    NSE-listed, and this module's whole job is BSE announcement crawling -- silently
    dropping the identity here means the company never gets crawled at all. Returns a
    DataFrame indexed like `tickers`, so callers can `.join()` it straight onto their
    own frame."""
    company_master_ids = map_company_master_ids_nse_or_bse(tickers)
    lookup_df = sql_to_df("SELECT company_master_id, bse_scrip_code FROM company_master WHERE bse_scrip_code IS NOT NULL")
    scrip_by_company_master_id = dict(zip(lookup_df["company_master_id"], lookup_df["bse_scrip_code"]))
    return pd.DataFrame(
        {
            "company_master_id": company_master_ids,
            "bse_scrip_code": company_master_ids.map(scrip_by_company_master_id),
            "isin": resolve_isin(company_master_ids),
        },
        index=tickers.index,
    )


class BseBlockedError(RuntimeError):
    """Raised internally when a single BSE request looks like a block (non-200,
    unparseable body, or an unexpected response shape) -- caught per-request so the
    caller can count it towards the circuit breaker rather than crashing the run."""


def _bse_get(url: str, params: dict[str, object]) -> dict:
    with exchange_request_gate(domain="bse"):
        response = requests.get(url, params=params, headers=BSE_HEADERS, timeout=30)
    if response.status_code != 200:
        raise BseBlockedError(f"HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise BseBlockedError(f"non-JSON response: {exc}") from exc


BSE_ANNOUNCEMENTS_PAGE_SIZE = 50
# Safety ceiling, not an expected case -- confirmed live 2026-08-18 that BACKFILL_
# LOOKBACK_DAYS=1095 windows for a real, fairly-active company return up to ~260
# rows (6 pages); this leaves generous headroom without risking an unbounded loop
# against a genuinely pathological scrip.
BSE_ANNOUNCEMENTS_MAX_PAGES = 40


def _fetch_company_announcements_page(scrip_code: str, *, from_date, to_date, page: int) -> tuple[list[dict], int | None]:
    payload = _bse_get(
        ANNOUNCEMENTS_URL,
        {
            "pageno": page,
            "strCat": "-1",
            "subcategory": "-1",
            "strPrevDate": from_date.strftime("%Y%m%d"),
            "strToDate": to_date.strftime("%Y%m%d"),
            "strSearch": "P",
            "strscrip": scrip_code,
            "strType": "C",
        },
    )
    if "Table" not in payload:
        raise BseBlockedError(f"response missing expected 'Table' key: {str(payload.get('Message', ''))[:120]}")
    row_count = None
    table1 = payload.get("Table1")
    if table1 and isinstance(table1, list) and "ROWCNT" in table1[0]:
        row_count = int(table1[0]["ROWCNT"])
    return payload["Table"] or [], row_count


# BSE refuses a range over 12 months ({"Status": false, "Message": "Date range cannot exceed
# 12 months."}, confirmed 2026-09-26) -- the 3-year backfill had been failing on every
# company. Longer ranges are fetched in chunks of at most this many days.
BSE_MAX_RANGE_DAYS = 360


def fetch_company_announcements(scrip_code: str, *, from_date, to_date) -> list[dict]:
    """All of a company's announcements between the two dates, in <= 12-month chunks."""
    rows: list[dict] = []
    chunk_end = to_date
    while chunk_end > from_date:
        chunk_start = max(from_date, chunk_end - timedelta(days=BSE_MAX_RANGE_DAYS))
        rows.extend(_fetch_company_range(scrip_code, from_date=chunk_start, to_date=chunk_end))
        chunk_end = chunk_start - timedelta(days=1)
    return rows


def _fetch_company_range(scrip_code: str, *, from_date, to_date) -> list[dict]:
    # BUG FOUND LIVE 2026-08-18: this endpoint paginates at BSE_ANNOUNCEMENTS_
    # PAGE_SIZE (50) rows/page and reports the true total in Table1[0].ROWCNT --
    # this function only ever requested page 1 and never read ROWCNT. Confirmed
    # live: every company tested returned exactly 50 rows with a real ROWCNT of
    # 95-293, i.e. 2-6x more exists. run_auditor_rpt_backfill's "3-year" lookback
    # was actually covering 5-15 months per company, and _mark_backfilled marks a
    # company permanently done after that one truncated fetch -- the missing
    # history was invisible forever. Now paginates until every row is fetched (or
    # BSE stops returning ROWCNT-worth of new rows / hits the safety ceiling).
    first_page, row_count = _fetch_company_announcements_page(scrip_code, from_date=from_date, to_date=to_date, page=1)
    all_rows = list(first_page)
    if row_count is None or len(all_rows) >= row_count:
        return all_rows

    page = 1
    while len(all_rows) < row_count and page < BSE_ANNOUNCEMENTS_MAX_PAGES:
        page += 1
        next_page, _ = _fetch_company_announcements_page(scrip_code, from_date=from_date, to_date=to_date, page=page)
        if not next_page:
            break  # BSE has nothing more, regardless of what ROWCNT claimed
        all_rows.extend(next_page)
    return all_rows


# Market-wide daily fetch (2026-09-25, universe rebuild step 6). The same endpoint with an
# empty strscrip returns EVERY company's announcements for a date (2026-09-24: 1,641 rows,
# 33 pages). ~33 requests a day regardless of universe size, against one request per
# company per run (1,386 companies at the 10s BSE gate = ~4h) for the per-company crawl.
# BSE refuses a multi-day range without a category (empty Table), so it is one day per
# call. Days are tracked in MARKET_DAYS_TABLE: a day counts as complete once fetched at
# least MARKET_DAY_SETTLE_HOURS after it ended (IST), so late disseminations are caught
# by one re-read; every run re-reads the days not yet complete.
MARKET_DAYS_TABLE = "fundamentals_bse_market_days"
MARKET_DAY_SETTLE_HOURS = 12
BSE_MARKET_MAX_PAGES = 200

_MARKET_DAYS_TABLE_STATEMENT = f"""
    CREATE TABLE IF NOT EXISTS {MARKET_DAYS_TABLE} (
        day DATE PRIMARY KEY,
        fetched_at TIMESTAMPTZ NOT NULL,
        rows_total INTEGER,
        rows_in_scope INTEGER
    )
"""


def _ensure_market_days_table() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute(_MARKET_DAYS_TABLE_STATEMENT)

    execute_db_operation(_op, operation_name=f"{MARKET_DAYS_TABLE}:ensure_table")


def fetch_market_announcements(day, *, stop_before=None) -> list[dict]:
    """Every company's BSE announcements disseminated on `day`, all pages. BSE lists newest
    first; with stop_before (a tz-aware timestamp) paging stops once a page reaches filings
    older than it -- the intraday pass reads only what is new since its last run."""
    params = {"pageno": 1, "strCat": "-1", "subcategory": "-1", "strPrevDate": day.strftime("%Y%m%d"),
              "strToDate": day.strftime("%Y%m%d"), "strSearch": "P", "strscrip": "", "strType": "C"}
    payload = _bse_get(ANNOUNCEMENTS_URL, params)
    if "Table" not in payload:
        raise BseBlockedError("response missing expected 'Table' key")
    rows = list(payload["Table"] or [])
    table1 = payload.get("Table1")
    row_count = int(table1[0]["ROWCNT"]) if table1 and isinstance(table1, list) and "ROWCNT" in table1[0] else None
    page = 1
    while row_count is not None and len(rows) < row_count and page < BSE_MARKET_MAX_PAGES:
        if stop_before is not None and rows:
            oldest = _parse_bse_timestamp(rows[-1].get("DissemDT") or rows[-1].get("NEWS_DT") or rows[-1].get("DT_TM"))
            if oldest is not None and oldest < stop_before:
                break
        page += 1
        next_payload = _bse_get(ANNOUNCEMENTS_URL, {**params, "pageno": page})
        next_rows = next_payload.get("Table") or []
        if not next_rows:
            break
        rows.extend(next_rows)
    return rows


def days_to_fetch(*, today, lookback_days: int) -> list:
    """IST dates in [today - lookback_days, today] not yet complete, oldest first."""
    fetched = sql_to_df(f"SELECT day, fetched_at FROM {MARKET_DAYS_TABLE} WHERE day >= %s",
                        params=(today - timedelta(days=lookback_days),))
    complete = set()
    for d, at in zip(fetched.get("day", []), fetched.get("fetched_at", [])):
        day = pd.Timestamp(d).date()
        settled = pd.Timestamp(day, tz="Asia/Kolkata") + pd.Timedelta(days=1, hours=MARKET_DAY_SETTLE_HOURS)
        if pd.Timestamp(at).tz_convert("Asia/Kolkata") >= settled:
            complete.add(day)
    window = [today - timedelta(days=n) for n in range(lookback_days, -1, -1)]
    return [d for d in window if d not in complete]


def _mark_market_day(day, *, rows_total: int, rows_in_scope: int) -> None:
    upsert_to_db(pd.DataFrame([{"day": day, "fetched_at": pd.Timestamp.now(tz="UTC"),
                                "rows_total": rows_total, "rows_in_scope": rows_in_scope}]),
                 MARKET_DAYS_TABLE, unique_keys=["day"])


def fetch_result_calendar() -> list[dict]:
    payload = _bse_get(RESULT_CALENDAR_URL, {})
    if not isinstance(payload, list):
        raise BseBlockedError("result calendar response was not a list")
    return payload


def _parse_bse_timestamp(value: str | None):
    """BUG FOUND LIVE 2026-08-18 (re-audit): the "T" branch used to tag the parsed
    timestamp tz="UTC" directly, but BSE's DissemDT/NEWS_DT/DT_TM values are IST
    wall-clock with no timezone marker of their own -- confirmed live, a real
    DissemDT of "2025-02-21T17:43:39.95" was being stored as 17:43 UTC (would be
    23:13 IST, implausible for a company's own disclosure timestamp) instead of the
    correct 12:13 UTC (17:43 IST, an ordinary post-market-hours filing). NSE's own
    timestamp parsing (nse_pit.py's _parse_nse_pit_timestamp) already localizes to
    Asia/Kolkata then converts to UTC; this now matches that. The cross-source
    dedup merge in events_store.py picks the "earliest" announcement_timestamp
    across sources, which was meaningless while the two sources' clocks disagreed
    by 5h30m -- disclosure_date's date part (parsed from the same value) survives
    either way, so this only affects the intraday time-of-day and the dedup
    ordering that depends on it."""
    if not value:
        return None
    try:
        # Both formats are IST wall clock; the non-"T" branch used to come back naive,
        # so its date was IST while the "T" branch's was UTC (2026-09-23 audit).
        return pd.Timestamp(value, tz="Asia/Kolkata").tz_convert("UTC")
    except (ValueError, TypeError):
        return None


def ist_disclosure_date(ts):
    """The IST calendar date of a disclosure. `.date()` on the UTC timestamp dated every
    filing disseminated 00:00-05:30 IST (late-night results) a day early -- 204 stored
    BSE rows, and a date mismatch against NSE's copy broke the cross-exchange dedup."""
    if ts is None:
        return None
    return ts.tz_convert("Asia/Kolkata").date() if ts.tzinfo is not None else ts.date()


def build_announcement_row(scrip_code: str, company_master_id: str, isin: str | None, raw: dict) -> dict | None:
    filing_type = classify_announcement(raw.get("SUBCATNAME"), raw.get("HEADLINE") or raw.get("NEWSSUB"))
    if filing_type == "other":
        return None
    disclosure_ts = _parse_bse_timestamp(raw.get("DissemDT") or raw.get("NEWS_DT") or raw.get("DT_TM"))
    return {
        "source": "bse",
        "news_id": str(raw.get("NEWSID")),
        "scrip_code": scrip_code,
        "company_master_id": company_master_id,
        "isin": isin,
        "filing_type": filing_type,
        "headline": raw.get("HEADLINE") or raw.get("NEWSSUB"),
        "subcategory": raw.get("SUBCATNAME"),
        "disclosure_date": ist_disclosure_date(disclosure_ts),
        "announcement_timestamp": disclosure_ts,
        # BSE's announcement text never carries structured PIT fields (see
        # fundamentals/collectors/events_store.py) -- left NULL here so a later NSE
        # corporates-pit row for the same disclosure can fill them in via merge.
        "quantity": None,
        "insider_name": None,
        "transaction_type": None,
        "attachment_name": raw.get("ATTACHMENTNAME"),
        "detail_url": raw.get("NSURL"),
        "detection_source": "bse_announcements",
        # Only rating rows are ever enriched; 'pending' on every other type never advanced
        # and read as a backlog that did not exist (2026-09-23 audit).
        "enrichment_status": "pending" if filing_type == "rating_action" else "not_applicable",
        "sources": "bse",
        "raw_json": json.dumps(raw, ensure_ascii=False, default=str),
        "load_ts": pd.Timestamp.now(tz="UTC"),
    }


def build_result_calendar_row(scrip_code: str, company_master_id: str, isin: str | None, raw: dict) -> dict | None:
    meeting_date = raw.get("meeting_date")
    parsed_date = None
    if meeting_date:
        try:
            parsed_date = datetime.strptime(meeting_date, "%d %b %Y").date()
        except ValueError:
            parsed_date = None
    return {
        "source": "bse",
        "news_id": f"resultcal:{scrip_code}:{meeting_date}",
        "scrip_code": scrip_code,
        "company_master_id": company_master_id,
        "isin": isin,
        "filing_type": "results_calendar",
        "headline": f"Expected results announcement around {meeting_date}" if meeting_date else "Expected results announcement",
        "subcategory": None,
        "disclosure_date": parsed_date,
        "announcement_timestamp": None,
        "quantity": None,
        "insider_name": None,
        "transaction_type": None,
        "attachment_name": None,
        "detail_url": raw.get("URL"),
        "detection_source": "bse_result_calendar",
        "enrichment_status": "not_applicable",
        "sources": "bse",
        "raw_json": json.dumps(raw, ensure_ascii=False, default=str),
        "load_ts": pd.Timestamp.now(tz="UTC"),
    }


# Intraday pass (reevaluation PRD 4.2): re-reads today back to this long before the newest
# filing already stored for today. The overlap covers filings BSE disseminates slightly out
# of order; the upsert dedups what is read twice.
INTRADAY_OVERLAP_MINUTES = 15


def intraday_stop_before(today):
    latest = sql_to_df(
        "SELECT max(announcement_timestamp) AS ts FROM fundamentals_events "
        "WHERE source = 'bse' AND detection_source = 'bse_announcements' AND disclosure_date = %s",
        params=(str(today),))
    ts = latest["ts"].iloc[0] if not latest.empty else None
    if ts is None or pd.isna(ts):
        return None
    return pd.Timestamp(ts) - pd.Timedelta(minutes=INTRADAY_OVERLAP_MINUTES)


def run_bse_l3_detection(*, limit: int | None = None, lookback_days: int | None = None,
                         intraday: bool = False) -> dict[str, object]:
    """intraday=True: today only, only the pages newer than what is stored, no result
    calendar, and the day is NOT marked fetched -- the nightly pass still reads it whole."""
    universe = load_l1_universe_tickers()
    if limit:
        universe = universe.head(limit)
    if universe.empty:
        _record_fallback(
            "l3_bse_no_l1_universe",
            reason="BSE L3 detection found no fundamentals_l1_universe rows to scope the crawl to.",
            error="empty L1 universe",
        )
        # BUG FOUND LIVE 2026-08-18 (re-audit): this dict was missing companies_total,
        # which main() reads unconditionally (result["companies_total"]) -- a plain
        # KeyError, crashing hardest exactly when the upstream L1 step has already
        # failed that day (the one scenario this early return exists to handle
        # gracefully). Reproduced live by monkeypatching an empty universe.
        return {"rows": 0, "announcement_rows": 0, "result_calendar_rows": 0, "merged_rows": 0, "companies_scanned": 0, "companies_total": 0, "failed_companies": [], "blocked": False}

    universe = universe.join(resolve_company_identity(universe["ticker"]))
    missing_scrip = universe[universe["bse_scrip_code"].isna()]
    if not missing_scrip.empty:
        _record_fallback(
            "l3_bse_scrip_code_missing",
            reason="Some L1-universe companies have no resolved BSE scrip code yet (step-0 backfill may still be running); they are skipped this run, not silently dropped.",
            error="unresolved bse_scrip_code",
            metadata={"tickers": list(missing_scrip["ticker"])[:50], "count": int(len(missing_scrip))},
        )
    universe = universe.dropna(subset=["bse_scrip_code"])

    _ensure_market_days_table()
    identity = {
        str(int(code)): (cmid, isin if pd.notna(isin) else None)
        for code, cmid, isin in zip(universe["bse_scrip_code"], universe["company_master_id"], universe["isin"])
    }
    today = pd.Timestamp.now(tz="Asia/Kolkata").date()
    if intraday:
        days, stop_before = [today], intraday_stop_before(today)
    else:
        days, stop_before = days_to_fetch(today=today, lookback_days=lookback_days if lookback_days is not None else LOOKBACK_DAYS), None

    rows: list[dict] = []
    failed_days: list[str] = []
    consecutive_failures = 0
    blocked = False
    days_fetched = 0

    for day in days:
        try:
            raw_rows = fetch_market_announcements(day, stop_before=stop_before) if intraday else fetch_market_announcements(day)
        except Exception as exc:  # noqa: BLE001 -- classified as a failure either way
            consecutive_failures += 1
            failed_days.append(str(day))
            _record_fallback(
                "l3_bse_announcement_fetch_failed",
                reason="BSE market-wide announcement fetch failed for this day; it is re-read next run (not marked complete).",
                error=exc,
                metadata={"day": str(day)},
            )
            if consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
                blocked = True
                _record_fallback(
                    "l3_bse_circuit_breaker_tripped",
                    reason=(
                        f"{consecutive_failures} consecutive BSE day fetches failed -- stopping this run "
                        "immediately rather than continuing to hit a possibly-blocking BSE."
                    ),
                    error="circuit breaker",
                    severity="error",
                    metadata={"days_fetched": days_fetched, "days_remaining": len(days) - days_fetched},
                )
                break
            continue

        consecutive_failures = 0
        days_fetched += 1
        in_scope = 0
        for raw in raw_rows:
            scrip_code = str(raw.get("SCRIP_CD") or "")
            if scrip_code not in identity:
                continue
            company_master_id, isin = identity[scrip_code]
            row = build_announcement_row(scrip_code, company_master_id, isin, raw)
            if row is not None:
                rows.append(row)
                in_scope += 1
        if not intraday:
            _mark_market_day(day, rows_total=len(raw_rows), rows_in_scope=in_scope)

    result_calendar_rows: list[dict] = []
    if not blocked and not intraday:
        try:
            calendar_identity = {
                str(int(v)): (cmid, isin)
                for v, cmid, isin in zip(universe["bse_scrip_code"], universe["company_master_id"], universe["isin"])
            }
            calendar_raw = fetch_result_calendar()
            for raw in calendar_raw:
                scrip_code = str(raw.get("scrip_Code") or "")
                if scrip_code not in calendar_identity:
                    continue
                company_master_id, isin = calendar_identity[scrip_code]
                row = build_result_calendar_row(scrip_code, company_master_id, isin if pd.notna(isin) else None, raw)
                if row is not None:
                    result_calendar_rows.append(row)
        except Exception as exc:  # noqa: BLE001 -- calendar is best-effort, does not block announcements
            _record_fallback(
                "l3_bse_result_calendar_fetch_failed",
                reason="BSE result calendar fetch failed; results_calendar rows are missing from this run.",
                error=exc,
            )

    all_rows = rows + result_calendar_rows
    upsert_result = upsert_events_with_dedup(all_rows)

    # 2026-08-13 (docs/FUNDAMENTAL_SCREENER_RESULTS_ARC.md): the moment a fresh
    # results filing (not results_calendar -- that's a forward prediction, not an
    # actual filing) is detected for a company, pull its L2 screener.in crawl
    # forward so L2 re-checks soon instead of waiting out its normal ~75-day
    # interval -- this is what ties stream B (fast, OCR'd) to scheduling stream A
    # (authoritative, slower). Best-effort: a failure here must not fail the whole
    # detection run, since the crawl just falls back to its normal cadence.
    results_company_master_ids = [row["company_master_id"] for row in rows if row.get("filing_type") == "results"]
    if results_company_master_ids:
        try:
            pull_crawl_forward(results_company_master_ids)
        except Exception as exc:  # noqa: BLE001 -- best-effort scheduling nudge only
            _record_fallback(
                "l3_bse_pull_crawl_forward_failed",
                reason="Pulling L2's screener.in crawl forward for freshly-detected results failed; L2 falls back to its normal cadence for these companies, not silently stuck forever.",
                error=exc,
            )

    return {
        "rows": len(all_rows),
        "announcement_rows": len(rows),
        "result_calendar_rows": len(result_calendar_rows),
        "merged_rows": upsert_result["merged"],
        "companies_scanned": int(len(universe)) if days_fetched and not blocked else 0,
        "companies_total": int(len(universe)),
        "days_fetched": days_fetched,
        "failed_days": failed_days,
        "failed_companies": [],
        "blocked": blocked,
    }


# One-time 3yr auditor/RPT backfill (2026-08-13) -- see run_auditor_rpt_backfill's own
# docstring for why this is separate from run_bse_l3_detection above (same crawl
# machinery, different lookback and different "which companies" selection). Not its
# own fundamentals/run_pipeline.py STEPS entry (a module can only have one
# STEPS-registered main()) -- main() below calls it directly too, bounded to
# BACKFILL_DAILY_LIMIT (re-audit 2026-08-18: this used to be genuinely manual-only,
# `python -m fundamentals.collectors.bse_announcements --backfill`, see this
# module's __main__ guard -- that CLI path still works for an operator wanting to
# push the backlog faster, but main()'s own daily nibble means it no longer NEEDS
# to be run manually). main() itself stays argparse-free regardless: run_pipeline.py
# calls main() as a direct Python function call (importlib.import_module +
# module.main()), not a subprocess, so adding argparse there would parse
# run_pipeline.py's OWN argv (e.g. its --steps flag) and crash on an unrecognized
# option -- the __main__ guard's argparse is for the standalone CLI path only.
BACKFILL_PROGRESS_TABLE = "fundamentals_bse_backfill_progress"
# ~3 years, matches fundamentals/screens/l1_universe.py's AUDITOR_CHANGE_LOOKBACK_YEARS.
BACKFILL_LOOKBACK_DAYS = 1095
# BUG FOUND LIVE 2026-08-17: run_auditor_rpt_backfill() reused build_announcement_row()
# unfiltered, so it classified and stored EVERY L3 filing_type found in the 3yr window,
# not just the two this backfill exists for. Confirmed live: of the 1,094 rows this
# backfill wrote on 2026-08-13, only 20 were actually auditor_change/related_party_
# transaction -- the other 1,074 were pit_sast/results/rating_action/capital_raise
# rows up to 3 years old that the regular 7-day daily crawl (run_bse_l3_detection) was
# always meant to be the sole source for. Those stale rows then queued into
# ocr_pipeline.py alongside fresh detections; live-tested 9 of the oldest-loaded
# pending OCR rows and 8 were already-404 (BSE's AttachLive endpoint doesn't retain
# attachments indefinitely) -- a contiguous run of these can trip ocr_pipeline.py's
# CIRCUIT_BREAKER_THRESHOLD=3 before any genuinely fresh, still-fetchable row behind
# them in the FIFO queue ever gets attempted (see load_pending_ocr_targets's own
# ordering fix in ocr_pipeline.py for the other half of this fix).
BACKFILL_FILING_TYPES = ("auditor_change", "related_party_transaction")
# 2026-09-25 (universe rebuild): the same per-company fetch also keeps EVERY filing type
# from the last RECENT_ALL_TYPES_DAYS, so a company new to the universe arrives with its
# latest quarter's results, ratings and deals -- not only whatever the daily crawl sees
# after it joined.
RECENT_ALL_TYPES_DAYS = 90
# Filings the backfill stores that are already older than this when fetched are HISTORY:
# kept for context (priors, auditor/RPT exclusion, the one-time catch-up admission) but
# marked rule_trigger_status='historical' so the nightly alert step never treats a
# months-old filing as news (2026-09-26, universe rebuild go-live).
HISTORY_AFTER_DAYS = 7
HISTORICAL_RULE_STATUS = "historical"
# BUG FOUND LIVE 2026-08-18 (re-audit): run_auditor_rpt_backfill() was manual-only
# -- no reference in run_pipeline.py's STEPS, the crontab, or docs/DATA_INVENTORY.md
# -- so the 43 BSE-only companies it exists to cover would never finish (confirmed
# live: 0/43 backfilled to date). run_pipeline.py's STEPS list can only call one
# main() per module, so this can't be its own STEPS entry without a new module;
# instead main() below also drives it directly, bounded to a small daily limit --
# the function's own docstring already establishes it as "resumable-across-
# invocations", exactly the shape a small-per-day cron nibble needs. At this limit
# the current 43-company backlog clears in ~9 daily runs; each company costs
# 1-6 BSE requests at the 10s rate-gate floor (BSE_ANNOUNCEMENTS_PAGE_SIZE
# pagination over a 3yr window), so 5/day bounds this step's added runtime to
# roughly 1-5 minutes worst case.
BACKFILL_DAILY_LIMIT = max(env.int("BSE_AUDITOR_RPT_BACKFILL_DAILY_LIMIT", 5), 1)

_BACKFILL_PROGRESS_TABLE_STATEMENT = """
    CREATE TABLE IF NOT EXISTS fundamentals_bse_backfill_progress (
        company_master_id TEXT PRIMARY KEY,
        backfilled_at TIMESTAMPTZ NOT NULL
    )
"""


def _ensure_backfill_progress_table() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute(_BACKFILL_PROGRESS_TABLE_STATEMENT)

    execute_db_operation(_op, operation_name="fundamentals_bse_backfill_progress:ensure_table")


def load_companies_needing_backfill(limit: int | None = None) -> pd.DataFrame:
    """L1 companies (with a resolved BSE scrip code) not yet in
    fundamentals_bse_backfill_progress -- each invocation processes a bounded SLICE OF
    WHAT'S REMAINING, not the same head-of-list every time the way run_bse_l3_
    detection's own `limit` does (fine there, since its 7-day incremental crawl scans
    the whole universe every run regardless; would never converge here)."""
    _ensure_backfill_progress_table()
    universe = load_l1_universe_tickers()
    if universe.empty:
        return universe
    universe = universe.join(resolve_company_identity(universe["ticker"]))
    universe = universe.dropna(subset=["bse_scrip_code"])
    done_df = sql_to_df(f"SELECT company_master_id FROM {BACKFILL_PROGRESS_TABLE}")  # noqa: S608 -- constant, not user input
    done = set(done_df["company_master_id"]) if not done_df.empty else set()
    remaining = universe[~universe["company_master_id"].isin(done)]
    if limit:
        remaining = remaining.head(limit)
    return remaining


def _mark_backfilled(company_master_ids: list[str]) -> None:
    if not company_master_ids:
        return
    upsert_to_db(
        pd.DataFrame({"company_master_id": company_master_ids, "backfilled_at": pd.Timestamp.now(tz="UTC")}),
        BACKFILL_PROGRESS_TABLE,
        unique_keys=["company_master_id"],
    )


def history_keys(rows: list[dict], *, existed_before: set, fetched_at) -> list[tuple[str, str]]:
    """(source, news_id) of rows this call INSERTED that were already older than
    HISTORY_AFTER_DAYS when fetched. A row that already existed (say, a recent filing the
    daily crawl stored that is still waiting for OCR) is never marked."""
    cutoff = pd.Timestamp(fetched_at).tz_localize(None).normalize() - pd.Timedelta(days=HISTORY_AFTER_DAYS)
    keys = []
    for row in rows:
        key = (row["source"], str(row["news_id"]))
        if key in existed_before or row.get("disclosure_date") is None:
            continue
        if pd.Timestamp(row["disclosure_date"]).tz_localize(None) < cutoff:
            keys.append(key)
    return keys


def mark_new_rows_historical(rows: list[dict], *, existed_before: set, fetched_at) -> int:
    keys = history_keys(rows, existed_before=existed_before, fetched_at=fetched_at)
    if not keys:
        return 0
    marked = {"n": 0}

    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute(
                f"UPDATE {RESULTS_TABLE} SET rule_trigger_status = %s "
                " WHERE (source, news_id) IN (SELECT * FROM unnest(%s::text[], %s::text[])) "
                "   AND rule_trigger_status IS NULL",
                (HISTORICAL_RULE_STATUS, [k[0] for k in keys], [k[1] for k in keys]),
            )
            marked["n"] = cur.rowcount

    execute_db_operation(_op, operation_name=f"{RESULTS_TABLE}:mark_historical")
    return marked["n"]


def mark_catchup_rows_historical() -> None:
    """One-time: filings the 2026-09-26 catch-up inserted before mark_new_rows_historical
    existed. Inserted since the catch-up began AND already over HISTORY_AFTER_DAYS old when
    stored; rows from earlier (the old universe) and the daily crawl's recent rows are
    untouched."""
    apply_schema_migration(
        migration_id="20260926_fundamentals_events_catchup_rows_historical",
        description="fundamentals_events: catch-up backfill rows older than 7 days at insert -> rule_trigger_status='historical'.",
        owner=SYNC_SOURCE_NAME,
        metadata={"tables": [RESULTS_TABLE]},
        statements=[
            f"UPDATE {RESULTS_TABLE} SET rule_trigger_status = 'historical' "
            " WHERE load_ts >= '2026-09-26 00:00:00+00' AND rule_trigger_status IS NULL "
            "   AND disclosure_date IS NOT NULL "
            f"  AND disclosure_date::date < (load_ts AT TIME ZONE 'Asia/Kolkata')::date - {HISTORY_AFTER_DAYS}"
        ],
    )


def run_auditor_rpt_backfill(*, limit: int | None = None, lookback_days: int = BACKFILL_LOOKBACK_DAYS) -> dict[str, object]:
    """Bounded, resumable-across-invocations 3yr historical backfill -- fundamentals/
    screens/l1_universe.py's post-hoc auditor-change/RPT exclusion needs 3 years of
    BSE history, but the regular daily crawl (run_bse_l3_detection, LOOKBACK_DAYS=7)
    only ever sees the last week. Same "bounded per-run, picks up where it left off
    next run" shape fundamentals/collectors/security_master.py's own --bse-limit
    already established for a structurally identical problem (a multi-hour full
    backfill that must fit inside a normal cron slot) -- reuses that module's own
    fetch_company_announcements/build_announcement_row/upsert_events_with_dedup/
    circuit-breaker machinery, differing only in WHICH companies get scanned
    (not-yet-backfilled, tracked here) and HOW FAR BACK (3yr, not 7 days). A company
    is marked backfilled only after a SUCCESSFUL fetch -- a failed/circuit-broken
    company stays eligible for retry next invocation, never silently marked done.

    Filtered to BACKFILL_FILING_TYPES only (see that constant's own comment) -- every
    other filing_type build_announcement_row can return is discarded here, same as
    "other" already is, since only auditor_change/related_party_transaction actually
    need 3 years of lookback; everything else is the daily crawl's job."""
    universe = load_companies_needing_backfill(limit)
    if universe.empty:
        return {"rows": 0, "companies_scanned": 0, "companies_remaining": 0, "failed_companies": [], "blocked": False}

    to_date = datetime.now(timezone.utc)
    from_date = to_date - timedelta(days=lookback_days)
    recent_cutoff = pd.Timestamp(to_date).tz_localize(None) - pd.Timedelta(days=RECENT_ALL_TYPES_DAYS)

    rows: list[dict] = []
    failed_companies: list[str] = []
    newly_backfilled: list[str] = []
    consecutive_failures = 0
    blocked = False
    companies_scanned = 0
    off_target_discarded = 0

    for _, company in universe.iterrows():
        scrip_code = str(int(company["bse_scrip_code"]))
        company_master_id = company["company_master_id"]
        isin = company["isin"] if pd.notna(company.get("isin")) else None
        try:
            raw_rows = fetch_company_announcements(scrip_code, from_date=from_date, to_date=to_date)
        except Exception as exc:  # noqa: BLE001 -- classified as a failure either way
            consecutive_failures += 1
            failed_companies.append(company["ticker"])
            _record_fallback(
                "backfill_bse_announcement_fetch_failed",
                reason="3yr auditor/RPT backfill fetch failed for this company; it stays eligible for retry next invocation (not marked backfilled).",
                error=exc,
                metadata={"ticker": company["ticker"], "scrip_code": scrip_code},
            )
            if consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
                blocked = True
                _record_fallback(
                    "backfill_bse_circuit_breaker_tripped",
                    reason=f"{consecutive_failures} consecutive backfill requests failed -- stopping this invocation immediately, same as run_bse_l3_detection's own breaker.",
                    error="circuit breaker",
                    severity="error",
                )
                break
            continue

        consecutive_failures = 0
        companies_scanned += 1
        newly_backfilled.append(company_master_id)
        for raw in raw_rows:
            row = build_announcement_row(scrip_code, company_master_id, isin, raw)
            if row is None:
                continue
            recent = (row.get("disclosure_date") is not None
                      and pd.Timestamp(row["disclosure_date"]).tz_localize(None) >= recent_cutoff)
            if row["filing_type"] not in BACKFILL_FILING_TYPES and not recent:
                off_target_discarded += 1
                continue
            rows.append(row)

    if rows:
        existed_before = _existing_event_keys(rows)
        upsert_events_with_dedup(rows)
        mark_new_rows_historical(rows, existed_before=existed_before, fetched_at=to_date)
    _mark_backfilled(newly_backfilled)

    return {
        "rows": len(rows),
        "companies_scanned": companies_scanned,
        "companies_remaining": len(load_companies_needing_backfill(None)),
        "failed_companies": failed_companies,
        "off_target_discarded": off_target_discarded,
        "blocked": blocked,
    }


def main() -> int:
    global STOCKEY_RUN_STATE
    mark_catchup_rows_historical()
    result = run_bse_l3_detection()

    # BUG FOUND LIVE 2026-08-18 (re-audit): see BACKFILL_DAILY_LIMIT's own comment --
    # this used to be manual-only and never ran. Best-effort, bounded, and must not
    # fail the regular daily crawl above if it errors -- the backfill has its own
    # circuit breaker and resumable progress tracking, so a failure here just means
    # zero progress this run, not lost state.
    backfill_result: dict[str, object] = {}
    try:
        backfill_result = run_auditor_rpt_backfill(limit=BACKFILL_DAILY_LIMIT)
    except Exception as exc:  # noqa: BLE001 -- best-effort daily nibble only
        _record_fallback(
            "l3_bse_auditor_rpt_backfill_failed",
            reason="The daily auditor/RPT 3yr backfill nibble failed; progress tracking is resumable, so this just means zero progress this run.",
            error=exc,
        )

    STOCKEY_RUN_STATE = {
        "source": SYNC_SOURCE_NAME,
        "rows": result["rows"],
        "rows_written": result["rows"],
        "companies_scanned": result["companies_scanned"],
        "companies_total": result["companies_total"],
        "days_fetched": result.get("days_fetched", 0),
        "failed_days": result.get("failed_days", []),
        "failed_companies": result["failed_companies"],
        "blocked": result["blocked"],
        "auditor_rpt_backfill": backfill_result,
        "fallback_used": bool(result.get("failed_days")) or result["blocked"] or bool(backfill_result.get("failed_companies")) or bool(backfill_result.get("blocked")),
        "state_advanced": result["rows"] > 0,
        # a tripped breaker is a partial-success run, not a failed one -- whatever was
        # collected before the trip is real and already upserted; it just isn't "ok"
        # in the sense of having scanned the whole universe.
        "status": "blocked" if result["blocked"] else "ok",
    }
    print(json.dumps(STOCKEY_RUN_STATE, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    import argparse
    import sys

    # Argparse lives ONLY inside this __main__ guard, deliberately not in main()
    # itself -- see run_auditor_rpt_backfill's own comment on why (run_pipeline.py
    # calls main() as a direct Python function call, not a subprocess, so main()
    # having its own argparse would choke on run_pipeline.py's actual argv).
    parser = argparse.ArgumentParser(description="BSE announcements crawler (daily) / one-time auditor-RPT backfill (--backfill).")
    parser.add_argument("--backfill", action="store_true", help="Run the one-time 3yr auditor/RPT backfill instead of the daily 7-day crawl.")
    parser.add_argument("--limit", type=int, default=20, help="Companies to backfill this invocation (bounded, resumable next invocation). 0 = unbounded.")
    parser.add_argument("--lookback-days", type=int, default=BACKFILL_LOOKBACK_DAYS)
    parser.add_argument("--intraday", action="store_true", help="Today only, newest pages only (all_fundamentals_reeval.sh).")
    args = parser.parse_args()

    if args.intraday:
        intraday_result = run_bse_l3_detection(intraday=True)
        print(json.dumps({"source": f"{SYNC_SOURCE_NAME}:intraday", **intraday_result}, ensure_ascii=False, default=str), flush=True)
        sys.exit(0)

    if args.backfill:
        backfill_result = run_auditor_rpt_backfill(limit=args.limit or None, lookback_days=args.lookback_days)
        print(json.dumps({"source": f"{SYNC_SOURCE_NAME}:backfill", **backfill_result}, ensure_ascii=False, default=str), flush=True)
        sys.exit(0)

    raise SystemExit(main())
