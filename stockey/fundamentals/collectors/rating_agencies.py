"""Rating-agency enrichment -- fundamental screener step 5, last piece
(docs/FUNDAMENTAL_SCREENER_PRD.md sec 8 step 5, fundamental_basic_goal.md sec 3.3:
"the exchange feed detects the event; the agency site supplies the content...
detection must be reliable, enrichment may fail silently and retry tomorrow. This
removes the need for seven robust agency crawlers.").

Detection is already done: fundamentals/collectors/bse_announcements.py classifies
`rating_action` filing_type from exchange announcement text and stores the row with
`enrichment_status="pending"`. This module is the "one reliable detector per agency"
side of that -- ICRA, India Ratings, and CRISIL (2026-08-12) are implemented; CARE/
Acuité/Brickwork/Infomerics still need their own live endpoint discovery the way each
of these three was done, deliberately not attempted in this pass.

Multi-agency plugin shape (2026-08-12 refactor, was ICRA-only before): AGENCY_PLUGINS
maps a canonical agency name to `{"open_session": () -> session_state,
"search": (session_state, company_name) -> [{"date_text","headline","rationale_id",
"rationale_url","rationale_pdf_url"}, ...]}`. run_rating_agency_enrichment() loops
every registered agency's own pending rows through its plugin, with a PER-AGENCY
circuit breaker (a block against one agency's site must not stop trying the others --
different domains, no shared failure mode). date_text formats differ per agency
("11 Aug 2026" / "Aug 12, 2026" / ISO 8601) -- match_rationale() parses with pandas'
flexible parser rather than a per-agency strict format, confirmed live 2026-08-12 that
all three parse correctly through the same call.

ICRA's endpoint, reverse-engineered live 2026-08-10 by reading icra.in's own rendered
HTML/JS (no reference library existed for this one, unlike BSE's BseIndiaApi):

- `GET /Rating/AllRatingRationales` -- plain HTML page carrying a per-session ASP.NET
  anti-forgery token (`__RequestVerificationToken`, a hidden form field) that the
  search POST below requires. No login, no CDP session, no cookie-gate beyond this --
  a plain requests.Session() round-trip is enough.
- `POST /Rating/GetAllRatingRational?page=1&type=Search` with
  `{__RequestVerificationToken, CompanyName, FromDate, ToDate, RatingCategoryName}` --
  returns an HTML fragment (a partial view, not JSON) with a `table.table tbody tr`
  row per historical rationale for that company: date, sector, headline (which already
  states the rating action -- "reaffirmed" / "upgraded" / "downgraded" / etc., no PDF
  needed for this much), and a `/Rationale/ShowRationaleReport?Id=<id>` link.

The rationale DETAIL page, unlike the listing, embeds the actual rationale text inside
a PDF viewer iframe (`/Rating/ShowRationalReportFilePdfViewer/<id>`) -- confirmed live,
not HTML-extractable. Full rationale text is therefore step 6's job (OCR), not this
module's. What this module captures instead -- and it's real, useful enrichment on its
own -- is the MATCH itself: confirms which agency, the exact action taken (from the
listing headline), the published date (cross-checked against the exchange-detected
disclosure_date), and the rationale id/URL/PDF URL for step 6 to pick up later.
Live-validated 2026-08-10 against a real detected event (ZF Steering Gear, ICRA,
disclosed 2026-08-04) -- ICRA's own listing shows a rationale published the same day.

India Ratings, reverse-engineered live 2026-08-12 -- the LISTING endpoint's exact
shape was already given by fundamental_basic_goal.md sec 3.3, confirmed against a real
live call: `GET /pressReleases/GetListing_RAC?type=rac&year=&pageNo=1&searchText=
<company>` -- plain JSON, no session/token/login at all, `searchText` filters by
company name (confirmed exact-match live), each row's own `pressReleaseTitle` already
states the action ("Downgrades"/"Upgrades"/"Assigns"/"Withdraws"). The DETAIL page
(`/pressrelease/<urlKey>/<issuer_slug>`) is Angular-client-rendered -- confirmed live,
the raw HTML has no rationale content, only a JS shell -- and no working underlying
detail/PDF API was found in the time spent looking; rationale_url/rationale_pdf_url
are left None rather than guessed. The listing alone is still real, useful enrichment:
it's everything evaluate_rating_action_trigger's rule needs (fundamentals/screens/
l3_triggers.py's _resolve_rating_action_type fallback).

CRISIL, reverse-engineered live 2026-08-12 by driving the existing CDP browser session
against crisilratings.com's own "Latest Rating Rationales" page (found via its own
site nav, not guessed -- an initial guess at a "rating-list.html" URL 404'd) and
capturing its XHR calls: `GET /content/crisilratings/en/home/our-business/ratings/
rating-rationale/_jcr_content/wrapper_100_par/ratingresultlisting.results.json
?cmd=RR&start=0&limit=<n>&filters={"company_name":"<company>"}` -- plain JSON, no
auth, confirmed live to do partial/substring company-name matching (a "Adani
Renewable" filter matched "Adani Renewable Energy Thirty Seven Limited"). No-match
returns an EMPTY response body (not `{"docs": []}`) -- confirmed live, handled
explicitly rather than treated as a parse failure. Same DETAIL-page situation as
India Ratings: no working rationale endpoint found in the time spent, left None.

CARE, reverse-engineered live 2026-08-13, chosen over Acuité/Brickwork/Infomerics
based on real evidence, not reputation: scanning ~2500 archived NSE announcements in
the stockeydata S3 bucket (the old advisory pipeline's own OCR archive, a much larger
and broader-company sample than this module's own detection has accumulated yet) found
30 real historical rating-action disclosures with a resolvable agency -- CRISIL 11,
ICRA 6, CARE 5, India Ratings 5, Acuité/Brickwork/Infomerics 0. CARE was the one
agency with real observed volume and no plugin. Found via the CDP session: careratings.
com's "Find Ratings" page search box posts to `GET /rrcompany?companyName=<company>
&YearID=&fdate=&tdate=` (YearID confirmed live to not actually filter -- same result
count regardless of value) -- plain JSON, no auth, `{"data": [{"FileURL",
"PublishedDate", ...}]}`. Genuinely different shape from the other three: CARE's
listing has NO action-stating headline at all, just press-release PDF filenames + a
date -- rating_action_type is left unset here and relies entirely on l3_triggers.py's
own structured_extraction_json fallback (Part 1, 2026-08-12) once the PDF is OCR'd,
not guessed from nothing. The PDF itself IS directly fetchable, no auth: confirmed
live at `https://www.careratings.com/upload/CompanyFiles/PR/<FileURL, URL-quoted>`.

enrichment_status values this module can set (fundamentals_events, shared with
bse_announcements.py/nse_pit.py's "pending" default):
- "matched" -- found the corresponding agency rationale, structured fields filled in.
- "no_match" -- searched the agency's listing, found nothing for this company/date;
  retried on the next scheduled run per fundamental_basic_goal.md sec 3.3 (a rationale
  can simply not be indexed yet).
- "unsupported_agency" -- classify_announcement (bse_announcements.py) said
  rating_action, but the specific agency has no plugin yet (or no agency name was
  mentioned at all) -- left for a future agency plugin, not silently stuck as
  "pending" forever.
- "failed" -- the search request itself errored (network/site issue).

Block-safety: same discipline as bse_announcements.py/nse_pit.py (explicit user
instruction 2026-08-10) -- a consecutive-failure circuit breaker stops that agency's
remaining rows this run rather than retrying into a block, even though none of these
three sites have a known WAF history the way NSE/BSE do. Per-agency, not whole-run --
see the plugin-shape note above.

Development todo tracking (2026-08-13, user-requested): every unsupported-agency row
is also tallied into fundamentals_unsupported_rating_agencies (agency_name,
occurrence_count, first/last_seen_at, one example) -- track_unsupported_agencies().
This is the durable, queryable version of the agency_counts fallback_telemetry above;
fundamentals/api/queries.py's get_rating_agency_todos() exposes it to the frontend's
Todos page. Self-clearing, not a manual "done" flag: get_unsupported_rating_agencies()
filters against the LIVE AGENCY_PLUGINS registry at read time, so an agency drops off
the todo list the moment its plugin ships, even though its historical row (and the
count it accumulated before that) stays in the table.
"""

from __future__ import annotations

import json
import re
import time
from urllib.parse import quote

import pandas as pd
import requests
from bs4 import BeautifulSoup
from environs import Env
from psycopg2 import sql as psycopg2_sql

from fundamentals.collectors.bse_announcements import RATING_AGENCY_KEYWORDS
from fundamentals.collectors.events_store import RESULTS_TABLE, _ensure_events_schema
from fundamentals.collectors.security_master import UA
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.exchange_rate_limiter import exchange_request_gate
from utils.fallback_telemetry import record_local_fallback_event

env = Env()
env.read_env()

SYNC_SOURCE_NAME = "fundamentals.collectors.rating_agencies"
STOCKEY_RUN_STATE: dict[str, object] = {}

ICRA_HEADERS = {"User-Agent": UA, "Referer": "https://www.icra.in/Rating/AllRatingRationales", "X-Requested-With": "XMLHttpRequest"}
ICRA_LISTING_URL = "https://www.icra.in/Rating/AllRatingRationales"
ICRA_SEARCH_URL = "https://www.icra.in/Rating/GetAllRatingRational"
ICRA_DETAIL_URL_TEMPLATE = "https://www.icra.in/Rationale/ShowRationaleReport?Id={id}"
ICRA_PDF_URL_TEMPLATE = "https://www.icra.in/Rating/GetRationalReportFilePdf?Id={id}"

INDIA_RATINGS_HEADERS = {"User-Agent": UA}
INDIA_RATINGS_LISTING_URL = "https://www.indiaratings.co.in/pressReleases/GetListing_RAC"

CRISIL_HEADERS = {"User-Agent": UA}
CRISIL_LISTING_URL = (
    "https://www.crisilratings.com/content/crisilratings/en/home/our-business/ratings/"
    "rating-rationale/_jcr_content/wrapper_100_par/ratingresultlisting.results.json"
)

CARE_HEADERS = {"User-Agent": UA}
CARE_SEARCH_URL = "https://www.careratings.com/rrcompany"
CARE_PDF_BASE_URL = "https://www.careratings.com/upload/CompanyFiles/PR/{filename}"

CIRCUIT_BREAKER_THRESHOLD = 3
# A rationale can be published a day or two either side of the exchange announcement
# (filing timing vs. agency publish timing don't always align to the minute) --
# matching within this window still counts as the same disclosure.
MATCH_DATE_TOLERANCE_DAYS = 2
# fundamental_basic_goal.md sec 3.3: "enrichment may fail silently and retry
# tomorrow." BUG FOUND LIVE 2026-08-15, fixed here: load_pending_rating_actions()
# only ever selected enrichment_status='pending' -- nothing anywhere ever reset a
# 'no_match'/'failed' row back to pending, so despite this module's own docstring
# and this exact spec line promising a next-day retry, a row that missed on its
# first attempt was permanently excluded from every future run. Confirmed live: a
# real 'no_match' row (a CRISIL upgrade) sat unchanged across at least one full
# pipeline cycle. RETRY_AFTER is the "tomorrow" in the spec -- a no_match/failed row
# is eligible again once this much time has passed since its last attempt, so a
# same-day re-run (or a re-run within one cron cycle) doesn't hammer the same
# not-yet-indexed rationale twice.
RETRY_AFTER = pd.Timedelta(days=1)

# BUG FOUND LIVE 2026-08-18 (re-audit): run_rating_agency_enrichment() had neither
# a batch cap nor a wall-clock budget -- main() calls it with limit=None, so every
# run processes the ENTIRE pending backlog. The eligible set grows monotonically
# (RETRY_AFTER re-admits a no_match/failed row daily, on top of genuinely new
# rating_action detections) at a >=10s/domain rate-gate floor, so an unbounded run
# risked the same "one step eats the whole complete_data.sh/fundamentals cron
# budget" failure mode already fixed elsewhere in this pipeline (ocr_pipeline.py's
# DEFAULT_BATCH_LIMIT/MAX_RUNTIME_SECONDS, bse_announcements.py's backfill cap).
# Same dual-bound shape here. A separate give-up counter for perpetually-no_match
# rows (so they stop being retried daily forever) is NOT added here -- it needs a
# real new column (a "first attempted at" timestamp; enrichment_attempted_at is
# overwritten on every attempt, so there's no way to tell how long a row has been
# retrying from what's already stored) and is a bigger schema change than this
# pass's own batch/runtime-bound fix, not attempted opportunistically alongside it.
DEFAULT_BATCH_LIMIT = env.int("RATING_ENRICHMENT_DEFAULT_BATCH_LIMIT", 200)
MAX_RUNTIME_SECONDS = env.int("RATING_ENRICHMENT_MAX_RUNTIME_SECONDS", 3 * 60 * 60)

# Extra columns rating-agency enrichment needs on the shared fundamentals_events table
# -- added to events_store's own bootstrap list rather than a second competing
# mechanism (same table, one schema-readiness function).
RATING_ENRICHMENT_COLUMN_TYPES = {
    "rating_agency": "TEXT",
    "rating_action_type": "TEXT",
    "rationale_headline": "TEXT",
    "rationale_id": "TEXT",
    "rationale_url": "TEXT",
    "rationale_pdf_url": "TEXT",
    "enrichment_attempted_at": "TIMESTAMPTZ",
}


def _record_fallback(fallback_type: str, *, source: str = "unspecified_agency", reason: str, error, severity: str = "warn", metadata=None) -> None:
    # BUG FOUND LIVE 2026-08-17: source used to be hardcoded "icra" unconditionally,
    # regardless of which agency the event was actually about -- every real call site
    # below is inside the per-agency enrichment loop and already has the true
    # agency_name in scope. A CRISIL or CARE search failure/circuit-breaker trip would
    # show up as an ICRA problem in source-grouped monitoring (the true agency was
    # still recoverable from metadata, just mislabeled on the field monitoring
    # actually groups by). Every call site now passes its own real source explicitly;
    # this default only covers a future caller that forgets to.
    record_local_fallback_event(
        module=SYNC_SOURCE_NAME,
        source=source,
        fallback_type=fallback_type,
        severity=severity,
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


# Explicit map, not a blind " rating(s)" suffix-strip -- found live while writing this
# module's own tests: stripping " ratings" from "india ratings" (a keyword where
# "Ratings" is part of the brand name, not a generic suffix like CARE's) mangles it to
# "india", which then can't match anything. "care rating"/"care ratings" both still
# collapse to the same canonical "care".
_AGENCY_CANONICAL_NAMES = {
    "crisil": "crisil",
    "icra": "icra",
    "care ratings": "care",
    "care rating": "care",
    "india ratings": "india ratings",
    "acuite": "acuite",
    "acuité": "acuite",
    "brickwork": "brickwork",
    "infomerics": "infomerics",
}


def detect_agency(headline: str | None, subcategory: str | None) -> str | None:
    """Which rating agency a detected rating_action row is about, from the same
    keyword list bse_announcements.py used to classify it as rating_action in the
    first place -- returns the canonical agency name ("icra", "crisil", ...) or None
    if no agency name is mentioned at all (classified by the generic "credit rating"/
    "rating action" phrase instead).

    BUG FOUND LIVE 2026-08-18 (re-audit): _AGENCY_CANONICAL_NAMES[keyword] was an
    unguarded dict lookup -- RATING_AGENCY_KEYWORDS (bse_announcements.py) and
    _AGENCY_CANONICAL_NAMES (this module) are two separately-maintained structures
    that currently happen to have the same keys; a future edit to one list without
    the other would raise KeyError and crash this whole enrichment step the moment
    the drifted keyword matched a real headline. Falls back to the raw keyword
    itself (still a usable, if non-canonical, agency label) and records fallback
    telemetry so the drift is visible and fixable rather than silently mislabeling
    forever."""
    text = f"{subcategory or ''} {headline or ''}".lower()
    for keyword in RATING_AGENCY_KEYWORDS:
        if keyword in text:
            canonical = _AGENCY_CANONICAL_NAMES.get(keyword)
            if canonical is None:
                _record_fallback(
                    "rating_agency_keyword_missing_canonical_name",
                    reason="A RATING_AGENCY_KEYWORDS entry has no matching _AGENCY_CANONICAL_NAMES mapping -- the two lists have drifted out of sync. Using the raw keyword as a non-canonical fallback.",
                    error="keyword not in _AGENCY_CANONICAL_NAMES",
                    metadata={"keyword": keyword},
                )
                return keyword
            return canonical
    return None


_ACTION_PATTERNS = (
    ("downgraded", re.compile(r"downgrad", re.IGNORECASE)),
    ("upgraded", re.compile(r"upgrad", re.IGNORECASE)),
    ("withdrawn", re.compile(r"withdraw", re.IGNORECASE)),
    ("suspended", re.compile(r"suspend", re.IGNORECASE)),
    ("placed_on_watch", re.compile(r"watch|credit\s*watch", re.IGNORECASE)),
    ("reaffirmed", re.compile(r"reaffirm", re.IGNORECASE)),
    ("assigned", re.compile(r"assign", re.IGNORECASE)),
)


def classify_rating_action_type(headline: str | None) -> str:
    text = headline or ""
    for label, pattern in _ACTION_PATTERNS:
        if pattern.search(text):
            return label
    return "other"


class IcraBlockedError(RuntimeError):
    """Raised internally when an ICRA request looks wrong (non-200, missing the
    anti-forgery token, or an unparseable listing fragment) -- counted towards the
    circuit breaker rather than crashing the run."""


def open_icra_session() -> tuple[requests.Session, str]:
    session = requests.Session()
    with exchange_request_gate(domain="icra"):
        response = session.get(ICRA_LISTING_URL, headers=ICRA_HEADERS, timeout=30)
    if response.status_code != 200:
        raise IcraBlockedError(f"HTTP {response.status_code} opening ICRA listing page")
    match = re.search(r'name="__RequestVerificationToken" type="hidden" value="([^"]+)"', response.text)
    if not match:
        raise IcraBlockedError("could not find __RequestVerificationToken on ICRA listing page")
    return session, match.group(1)


def parse_icra_search_results(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for tr in soup.select("table.table tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue
        link = tr.select_one('a[href*="ShowRationaleReport"]')
        if link is None:
            continue
        id_match = re.search(r"[Ii]d=(\d+)", link.get("href", ""))
        if id_match is None:
            continue
        results.append(
            {
                "date_text": tds[0].get_text(" ", strip=True),
                "sector": tds[1].get_text(" ", strip=True),
                "headline": link.get_text(" ", strip=True),
                "rationale_id": id_match.group(1),
            }
        )
    return results


def search_icra_rationales(session: requests.Session, token: str, company_name: str) -> list[dict]:
    with exchange_request_gate(domain="icra"):
        response = session.post(
            ICRA_SEARCH_URL,
            params={"page": 1, "type": "Search"},
            data={"__RequestVerificationToken": token, "CompanyName": company_name, "FromDate": "", "ToDate": "", "RatingCategoryName": ""},
            headers=ICRA_HEADERS,
            timeout=30,
        )
    if response.status_code != 200:
        raise IcraBlockedError(f"HTTP {response.status_code} searching ICRA rationales")
    results = parse_icra_search_results(response.text)
    for result in results:
        result["rationale_url"] = ICRA_DETAIL_URL_TEMPLATE.format(id=result["rationale_id"])
        result["rationale_pdf_url"] = ICRA_PDF_URL_TEMPLATE.format(id=result["rationale_id"])
    return results


class IndiaRatingsBlockedError(RuntimeError):
    """Raised internally when an India Ratings request looks wrong (non-200 or
    unparseable JSON) -- counted towards that agency's own circuit breaker."""


def search_india_ratings_rationales(company_name: str) -> list[dict]:
    with exchange_request_gate(domain="india_ratings"):
        response = requests.get(
            INDIA_RATINGS_LISTING_URL,
            params={"type": "rac", "year": "", "pageNo": 1, "searchText": company_name},
            headers=INDIA_RATINGS_HEADERS,
            timeout=30,
        )
    if response.status_code != 200:
        raise IndiaRatingsBlockedError(f"HTTP {response.status_code} searching India Ratings listing")
    try:
        rows = response.json()
    except ValueError as exc:
        raise IndiaRatingsBlockedError(f"non-JSON response: {exc}") from exc
    results = []
    for row in rows:
        url_key = row.get("urlKey")
        headline = row.get("pressReleaseTitle")
        if not url_key or not headline:
            continue
        results.append(
            {
                "date_text": row.get("effectiveDate"),
                "headline": headline,
                "rationale_id": str(row.get("pressReleaseID") or url_key),
                # Detail page is Angular-client-rendered -- see module docstring, no
                # working detail/PDF endpoint found live. Left None, not guessed.
                "rationale_url": None,
                "rationale_pdf_url": None,
            }
        )
    return results


class CrisilBlockedError(RuntimeError):
    """Raised internally when a CRISIL request looks wrong (non-200 or unparseable
    non-empty body) -- counted towards that agency's own circuit breaker."""


def search_crisil_rationales(company_name: str) -> list[dict]:
    with exchange_request_gate(domain="crisil"):
        response = requests.get(
            CRISIL_LISTING_URL,
            params={"cmd": "RR", "start": 0, "limit": 25, "filters": json.dumps({"company_name": company_name})},
            headers=CRISIL_HEADERS,
            timeout=30,
        )
    if response.status_code != 200:
        raise CrisilBlockedError(f"HTTP {response.status_code} searching CRISIL rating listing")
    if not response.text.strip():
        return []  # confirmed live 2026-08-12: no-match returns an empty body, not {"docs": []}
    try:
        data = response.json()
    except ValueError as exc:
        raise CrisilBlockedError(f"non-JSON response: {exc}") from exc
    results = []
    for doc in data.get("docs") or []:
        headline = doc.get("heading")
        if not headline:
            continue
        results.append(
            {
                "date_text": doc.get("ratingDate"),
                "headline": headline,
                "rationale_id": str(doc.get("prId") or doc.get("companyCode") or ""),
                # Same situation as India Ratings -- no working detail endpoint found
                # live, left None rather than guessed. See module docstring.
                "rationale_url": None,
                "rationale_pdf_url": None,
            }
        )
    return results


class CareBlockedError(RuntimeError):
    """Raised internally when a CARE request looks wrong (non-200 or unparseable
    JSON) -- counted towards that agency's own circuit breaker."""


def search_care_rationales(company_name: str) -> list[dict]:
    with exchange_request_gate(domain="care"):
        response = requests.get(
            CARE_SEARCH_URL,
            params={"companyName": company_name, "YearID": "", "fdate": "", "tdate": ""},
            headers=CARE_HEADERS,
            timeout=30,
        )
    if response.status_code != 200:
        raise CareBlockedError(f"HTTP {response.status_code} searching CARE rating listing")
    try:
        data = response.json()
    except ValueError as exc:
        raise CareBlockedError(f"non-JSON response: {exc}") from exc
    results = []
    for row in data.get("data") or []:
        file_url = row.get("FileURL")
        if not file_url:
            continue
        results.append(
            {
                "date_text": row.get("PublishedDate"),
                # CARE's listing has no action-stating headline at all (unlike
                # ICRA/India Ratings/CRISIL) -- confirmed live 2026-08-13, just
                # press-release PDF filenames + dates. rating_action_type is left
                # for l3_triggers.py's own structured_extraction_json fallback
                # (Part 1, 2026-08-12) to fill in once the PDF is OCR'd, not
                # guessed here.
                "headline": None,
                "rationale_id": file_url,
                "rationale_url": None,
                "rationale_pdf_url": CARE_PDF_BASE_URL.format(filename=quote(file_url)),
            }
        )
    return results


def match_rationale(results: list[dict], target_date) -> dict | None:
    """Flexible date parsing, not a per-agency strict format -- confirmed live
    2026-08-12 that pandas' own parser handles all three agencies' differing
    date_text formats correctly through one call ("11 Aug 2026" / "Aug 12, 2026" /
    ISO 8601), see module docstring."""
    if target_date is None:
        return None
    target = pd.Timestamp(target_date)
    best = None
    best_gap = None
    for result in results:
        result_date = pd.to_datetime(result.get("date_text"), errors="coerce")
        if pd.isna(result_date):
            continue
        gap = abs((result_date - target).days)
        if gap <= MATCH_DATE_TOLERANCE_DAYS and (best_gap is None or gap < best_gap):
            best, best_gap = result, gap
    return best


def _open_icra_session_state():
    return open_icra_session()


def _search_icra(session_state, company_name: str) -> list[dict]:
    session, token = session_state
    return search_icra_rationales(session, token, company_name)


def _open_stateless_session():
    return None


AGENCY_PLUGINS = {
    "icra": {"open_session": _open_icra_session_state, "search": _search_icra},
    "india ratings": {"open_session": _open_stateless_session, "search": lambda _session, company: search_india_ratings_rationales(company)},
    "crisil": {"open_session": _open_stateless_session, "search": lambda _session, company: search_crisil_rationales(company)},
    "care": {"open_session": _open_stateless_session, "search": lambda _session, company: search_care_rationales(company)},
}


def load_pending_rating_actions(limit: int | None = None) -> pd.DataFrame:
    """Fresh 'pending' rows, PLUS 'no_match'/'failed' rows whose last attempt is at
    least RETRY_AFTER old -- see that constant's docstring for the bug this fixes.
    Fresh pending rows sort first (CASE ... END) so a batch/rate limit always
    prioritizes never-yet-tried detections over a retry of one that already missed
    once."""
    query = """
        SELECT source, news_id, company_master_id, headline, subcategory, disclosure_date, enrichment_status
        FROM fundamentals_events
        WHERE filing_type = 'rating_action'
          AND (
            enrichment_status = 'pending'
            OR (
              enrichment_status IN ('no_match', 'failed')
              AND (enrichment_attempted_at IS NULL OR enrichment_attempted_at < %s)
            )
          )
        ORDER BY CASE WHEN enrichment_status = 'pending' THEN 0 ELSE 1 END, load_ts ASC NULLS LAST
    """
    if limit:
        query += f" LIMIT {int(limit)}"
    return sql_to_df(query, params=(pd.Timestamp.now(tz="UTC") - RETRY_AFTER,))


def _set_enrichment_status(*, source: str, news_id: str, status: str, fields: dict | None = None) -> None:
    fields = {**(fields or {}), "enrichment_attempted_at": pd.Timestamp.now(tz="UTC")}
    set_columns = ["enrichment_status = %s"] + [f"{col} = %s" for col in fields]
    params = [status, *fields.values(), source, news_id]

    def _update() -> None:
        with db_session() as (_, cur):
            cur.execute(
                f"UPDATE fundamentals_events SET {', '.join(set_columns)} WHERE source = %s AND news_id = %s",  # noqa: S608 -- column names are our own fixed constants, never user input
                params,
            )

    execute_db_operation(_update, operation_name="fundamentals_events:rating_enrichment_update")


def run_rating_agency_enrichment(*, limit: int | None = None) -> dict[str, object]:
    """Loops every registered agency (AGENCY_PLUGINS) over its own pending rows.
    Each agency gets its own circuit breaker -- a block against one agency's site
    must not stop trying the others, see module docstring.

    BUG FOUND LIVE 2026-08-18 (re-audit): see DEFAULT_BATCH_LIMIT/MAX_RUNTIME_
    SECONDS' own comment -- this used to have neither, so main()'s limit=None call
    processed the entire pending backlog every run with no bound. effective_limit
    defaults to DEFAULT_BATCH_LIMIT when the caller passes nothing; MAX_RUNTIME_
    SECONDS is checked between rows (any row not reached before the cutoff stays
    pending, picked up next run -- safe and idempotent, same shape ocr_pipeline.py
    already established)."""
    _ensure_events_schema()
    _bootstrap_rating_columns()

    effective_limit = limit or DEFAULT_BATCH_LIMIT
    pending = load_pending_rating_actions(effective_limit)
    if pending.empty:
        return {"matched": 0, "no_match": 0, "unsupported_agency": 0, "failed": 0, "blocked": False, "time_budget_exceeded": False}

    pending = pending.copy()
    pending["_agency"] = pending.apply(lambda row: detect_agency(row["headline"], row["subcategory"]), axis=1)

    counts = {"matched": 0, "no_match": 0, "unsupported_agency": 0, "failed": 0}
    any_blocked = False
    run_started = time.monotonic()
    time_budget_exceeded = False

    unsupported_rows = pending[~pending["_agency"].isin(AGENCY_PLUGINS.keys())]
    if not unsupported_rows.empty:
        for _, row in unsupported_rows.iterrows():
            _set_enrichment_status(source=row["source"], news_id=row["news_id"], status="unsupported_agency")
        counts["unsupported_agency"] = int(len(unsupported_rows))
        # Per-agency breakdown, not just a count -- this is the actual prioritization
        # signal for "which agency plugin to build next" (2026-08-13): read from
        # fallback_telemetry after enough real runs accumulate, rather than guessing
        # from general reputation. "unnamed" covers rows where classify_announcement
        # matched the generic "credit rating"/"rating action" phrase with no specific
        # agency name in the text at all.
        agency_counts = unsupported_rows["_agency"].fillna("unnamed").value_counts().to_dict()
        _record_fallback(
            "rating_enrichment_unsupported_agency",
            source="unsupported_agency",  # spans potentially several different unplugged agencies -- see agency_counts in metadata for the real breakdown
            reason="Some detected rating_action rows name an agency (or no agency at all) with no enrichment plugin yet.",
            error="no plugin for this agency",
            severity="warn",
            metadata={"count": int(len(unsupported_rows)), "agency_counts": {str(k): int(v) for k, v in agency_counts.items()}},
        )
        track_unsupported_agencies(unsupported_rows)

    for agency_name, plugin in AGENCY_PLUGINS.items():
        agency_rows = pending[pending["_agency"] == agency_name]
        if agency_rows.empty:
            continue

        if time_budget_exceeded:
            break

        session_state = None
        consecutive_failures = 0
        for _, row in agency_rows.iterrows():
            if time.monotonic() - run_started >= MAX_RUNTIME_SECONDS:
                time_budget_exceeded = True
                break

            issuer = _resolve_issuer_name(row["company_master_id"])
            if not issuer:
                # BUG FOUND LIVE 2026-08-18 (re-audit): the only failure path in this
                # loop with no _record_fallback call -- every other failure branch
                # here does. Silent otherwise: a company whose issuer name can't be
                # resolved just permanently fails enrichment with no trace.
                counts["failed"] += 1
                _set_enrichment_status(source=row["source"], news_id=row["news_id"], status="failed")
                _record_fallback(
                    "rating_enrichment_issuer_resolution_failed",
                    source=agency_name,
                    reason="Could not resolve an issuer name for this company_master_id; rating enrichment search was never attempted.",
                    error="no issuer name resolved",
                    metadata={"company_master_id": row["company_master_id"], "news_id": row["news_id"]},
                )
                continue
            try:
                if session_state is None:
                    session_state = plugin["open_session"]()
                results = plugin["search"](session_state, issuer)
            except Exception as exc:  # noqa: BLE001 -- classified as a failure either way
                consecutive_failures += 1
                counts["failed"] += 1
                _set_enrichment_status(source=row["source"], news_id=row["news_id"], status="failed")
                _record_fallback(
                    "rating_enrichment_search_failed",
                    source=agency_name,
                    reason=f"{agency_name} rationale search failed for this company; will retry on the next scheduled run.",
                    error=exc,
                    metadata={"company_master_id": row["company_master_id"], "agency": agency_name},
                )
                if consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
                    any_blocked = True
                    _record_fallback(
                        "rating_enrichment_circuit_breaker_tripped",
                        source=agency_name,
                        reason=f"{consecutive_failures} consecutive {agency_name} requests failed -- stopping {agency_name} for this run (other agencies unaffected).",
                        error="circuit breaker",
                        severity="error",
                        metadata={"agency": agency_name},
                    )
                    break
                session_state = None  # force a fresh session on the next attempt
                continue

            consecutive_failures = 0
            matched = match_rationale(results, row["disclosure_date"])
            if matched is None:
                counts["no_match"] += 1
                _set_enrichment_status(source=row["source"], news_id=row["news_id"], status="no_match")
                continue

            counts["matched"] += 1
            # Some agencies' listings (CARE, confirmed live 2026-08-13) carry no
            # action-stating headline at all -- classify_rating_action_type() would
            # otherwise return "other" for an empty string, which if written to the
            # rating_action_type COLUMN would block l3_triggers.py's own
            # structured_extraction_json fallback (Part 1) from ever running for
            # this row (a non-empty column always wins there). Leave the column
            # unset (None) instead when there's genuinely nothing to classify.
            headline = matched.get("headline")
            _set_enrichment_status(
                source=row["source"],
                news_id=row["news_id"],
                status="matched",
                fields={
                    "rating_agency": agency_name,
                    "rating_action_type": classify_rating_action_type(headline) if headline else None,
                    "rationale_headline": headline,
                    "rationale_id": matched["rationale_id"],
                    "rationale_url": matched.get("rationale_url"),
                    "rationale_pdf_url": matched.get("rationale_pdf_url"),
                },
            )

    return {**counts, "blocked": any_blocked, "time_budget_exceeded": time_budget_exceeded}


def _bootstrap_rating_columns() -> None:
    """Same idempotent-ALTER pattern as events_store._ensure_events_schema, for the
    columns this module specifically needs -- kept here rather than folded into
    events_store's own list so that module doesn't need to know about rating-agency-
    specific fields."""

    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = %s", (RESULTS_TABLE,))
            if cur.fetchone() is None:
                return
            for column, pg_type in RATING_ENRICHMENT_COLUMN_TYPES.items():
                cur.execute(
                    psycopg2_sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} {}").format(
                        psycopg2_sql.Identifier(RESULTS_TABLE), psycopg2_sql.Identifier(column), psycopg2_sql.SQL(pg_type)
                    )
                )

    execute_db_operation(_op, operation_name="fundamentals_events:ensure_rating_columns")


UNSUPPORTED_AGENCIES_TABLE = "fundamentals_unsupported_rating_agencies"

_UNSUPPORTED_AGENCIES_TABLE_STATEMENT = """
    CREATE TABLE IF NOT EXISTS fundamentals_unsupported_rating_agencies (
        agency_name TEXT PRIMARY KEY,
        occurrence_count INTEGER NOT NULL,
        first_seen_at TIMESTAMPTZ NOT NULL,
        last_seen_at TIMESTAMPTZ NOT NULL,
        example_headline TEXT,
        example_company_master_id TEXT,
        load_ts TIMESTAMPTZ
    )
"""


def _ensure_unsupported_agencies_table() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute(_UNSUPPORTED_AGENCIES_TABLE_STATEMENT)

    execute_db_operation(_op, operation_name="fundamentals_unsupported_rating_agencies:ensure_table")


def track_unsupported_agencies(unsupported_rows: pd.DataFrame) -> None:
    """Durable, queryable "development todo" record -- see module docstring.
    occurrence_count accumulates across every run (never reset); first_seen_at is
    set once and never overwritten, matching fundamentals/screens/watchlist.py's own
    "increment count, keep first_seen stable" pattern."""
    if unsupported_rows.empty:
        return
    _ensure_unsupported_agencies_table()

    grouped = unsupported_rows.copy()
    grouped["_agency"] = grouped["_agency"].fillna("unnamed")
    now = pd.Timestamp.now(tz="UTC")

    existing_df = sql_to_df("SELECT agency_name, occurrence_count, first_seen_at FROM fundamentals_unsupported_rating_agencies")
    existing = {row["agency_name"]: row for row in existing_df.to_dict("records")} if not existing_df.empty else {}

    rows = []
    for agency_name, group in grouped.groupby("_agency"):
        prior = existing.get(agency_name)
        example = group.iloc[-1]
        rows.append(
            {
                "agency_name": agency_name,
                "occurrence_count": int((prior["occurrence_count"] if prior else 0) + len(group)),
                "first_seen_at": prior["first_seen_at"] if prior else now,
                "last_seen_at": now,
                "example_headline": example.get("headline"),
                "example_company_master_id": example.get("company_master_id"),
                "load_ts": now,
            }
        )
    upsert_to_db(pd.DataFrame(rows), UNSUPPORTED_AGENCIES_TABLE, unique_keys=["agency_name"])


def get_unsupported_rating_agencies() -> pd.DataFrame:
    """Every tracked not-yet-supported agency, EXCLUDING whatever's currently in
    AGENCY_PLUGINS -- self-clearing, see module docstring. Read fresh each call, no
    caching, so a just-shipped plugin drops its agency off the list immediately."""
    _ensure_unsupported_agencies_table()
    df = sql_to_df(f"SELECT * FROM {UNSUPPORTED_AGENCIES_TABLE} ORDER BY occurrence_count DESC")  # noqa: S608 -- table name is our own fixed constant, never user input
    if df.empty:
        return df
    supported = set(AGENCY_PLUGINS.keys())
    return df[~df["agency_name"].isin(supported)].reset_index(drop=True)


def _resolve_issuer_name(company_master_id: str | None) -> str | None:
    if not company_master_id:
        return None
    df = sql_to_df(
        """
        SELECT display_name FROM dim_security
        WHERE company_master_id = %s AND display_name IS NOT NULL
        ORDER BY last_trade_date DESC NULLS LAST, effective_to DESC NULLS LAST
        LIMIT 1
        """,
        params=(company_master_id,),
    )
    return None if df.empty else df.iloc[0]["display_name"]


def main() -> int:
    global STOCKEY_RUN_STATE
    result = run_rating_agency_enrichment()
    STOCKEY_RUN_STATE = {
        "source": SYNC_SOURCE_NAME,
        "matched": result["matched"],
        "no_match": result["no_match"],
        "unsupported_agency": result["unsupported_agency"],
        "failed": result["failed"],
        "blocked": result["blocked"],
        "time_budget_exceeded": result["time_budget_exceeded"],
        "rows_written": result["matched"],
        "fallback_used": bool(result["unsupported_agency"] or result["failed"] or result["blocked"]),
        "state_advanced": result["matched"] > 0,
        "status": "blocked" if result["blocked"] else "ok",
    }
    print(json.dumps(STOCKEY_RUN_STATE, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
