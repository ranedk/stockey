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
sec 2).

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

from fundamentals.collectors.events_store import RESULTS_TABLE, resolve_isin, upsert_events_with_dedup
from fundamentals.collectors.security_master import BSE_HEADERS
from fundamentals.screens.l1_universe import load_l1_universe_tickers
from utils.company_master import map_company_master_ids
from utils.db import sql_to_df
from utils.exchange_rate_limiter import exchange_request_gate
from utils.fallback_telemetry import record_local_fallback_event


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
    # and Reg. 29/31 SAST disclosures are the actual recurring phrasings BSE uses.
    "trading window",
    "regulation 29",
    "regulation 31",
    "reg. 29",
    "reg. 31",
    "reg.29",
    "reg.31",
)


def classify_announcement(subcategory: str | None, headline: str | None) -> str:
    """Classify one BSE announcement row into an L3 trigger type. Keys primarily off
    SUBCATNAME -- BSE's own assigned filing-type label, not free text -- rather than
    BSE's CATEGORYNAME field (comes back null on an unfiltered strCat=-1 request,
    confirmed against the reference sample response, see module docstring).

    Falls back to matching the same keywords against HEADLINE only for pit_sast/
    rating_action (subcategory is sometimes missing/generic for these two), NOT for
    results -- confirmed live 2026-08-10 that a routine "Newspaper Publication"
    filing's headline ("Newspaper Publication of the Unaudited Financial Results")
    contains "financial results" without being one; results has its own dedicated
    calendar endpoint (fetch_result_calendar) as a backstop, so it doesn't need a
    headline fallback and shouldn't inherit that false-positive risk.
    """
    subcategory_text = (subcategory or "").lower()
    headline_text = (headline or "").lower()

    if any(keyword in subcategory_text for keyword in PIT_SAST_KEYWORDS):
        return "pit_sast"
    if "financial result" in subcategory_text:
        return "results"
    if "credit rating" in subcategory_text or "rating action" in subcategory_text or any(
        keyword in subcategory_text for keyword in RATING_AGENCY_KEYWORDS
    ):
        return "rating_action"

    if any(keyword in headline_text for keyword in PIT_SAST_KEYWORDS):
        return "pit_sast"
    if "credit rating" in headline_text or "rating action" in headline_text or any(
        keyword in headline_text for keyword in RATING_AGENCY_KEYWORDS
    ):
        return "rating_action"
    return "other"


def resolve_company_identity(tickers: pd.Series) -> pd.DataFrame:
    """NSE ticker (as stored in fundamentals_l1_universe) -> (company_master_id,
    bse_scrip_code, isin), via company_master/dim_security -- reuses the step-0
    backfill (fundamentals/collectors/security_master.py) rather than looking scrip
    codes up again here, and fundamentals.collectors.events_store.resolve_isin for the
    cross-source dedup key. Returns a DataFrame indexed like `tickers`, so callers can
    `.join()` it straight onto their own frame."""
    company_master_ids = map_company_master_ids(tickers, exchange="NSE")
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


def fetch_company_announcements(scrip_code: str, *, from_date, to_date) -> list[dict]:
    payload = _bse_get(
        ANNOUNCEMENTS_URL,
        {
            "pageno": 1,
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
        raise BseBlockedError("response missing expected 'Table' key")
    return payload["Table"] or []


def fetch_result_calendar() -> list[dict]:
    payload = _bse_get(RESULT_CALENDAR_URL, {})
    if not isinstance(payload, list):
        raise BseBlockedError("result calendar response was not a list")
    return payload


def _parse_bse_timestamp(value: str | None):
    if not value:
        return None
    try:
        return pd.Timestamp(value, tz="UTC") if "T" in value else pd.Timestamp(value)
    except (ValueError, TypeError):
        return None


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
        "disclosure_date": disclosure_ts.date() if disclosure_ts is not None else None,
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
        "enrichment_status": "pending",
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
        "enrichment_status": "pending",
        "sources": "bse",
        "raw_json": json.dumps(raw, ensure_ascii=False, default=str),
        "load_ts": pd.Timestamp.now(tz="UTC"),
    }


def run_bse_l3_detection(*, limit: int | None = None, lookback_days: int | None = None) -> dict[str, object]:
    universe = load_l1_universe_tickers()
    if limit:
        universe = universe.head(limit)
    if universe.empty:
        _record_fallback(
            "l3_bse_no_l1_universe",
            reason="BSE L3 detection found no fundamentals_l1_universe rows to scope the crawl to.",
            error="empty L1 universe",
        )
        return {"rows": 0, "companies_scanned": 0, "failed_companies": [], "blocked": False}

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

    to_date = datetime.now(timezone.utc)
    from_date = to_date - timedelta(days=lookback_days if lookback_days is not None else LOOKBACK_DAYS)

    rows: list[dict] = []
    failed_companies: list[str] = []
    consecutive_failures = 0
    blocked = False
    companies_scanned = 0

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
                "l3_bse_announcement_fetch_failed",
                reason="BSE announcement fetch failed for this company; it is missing from this run.",
                error=exc,
                metadata={"ticker": company["ticker"], "scrip_code": scrip_code},
            )
            if consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
                blocked = True
                _record_fallback(
                    "l3_bse_circuit_breaker_tripped",
                    reason=(
                        f"{consecutive_failures} consecutive BSE requests failed -- stopping this run "
                        "immediately rather than continuing to hit a possibly-blocking BSE. The next "
                        "scheduled run will retry the remaining companies (upsert-keyed, idempotent)."
                    ),
                    error="circuit breaker",
                    severity="error",
                    metadata={"companies_scanned": companies_scanned, "companies_remaining": len(universe) - companies_scanned},
                )
                break
            continue

        consecutive_failures = 0
        companies_scanned += 1
        for raw in raw_rows:
            row = build_announcement_row(scrip_code, company_master_id, isin, raw)
            if row is not None:
                rows.append(row)

    result_calendar_rows: list[dict] = []
    if not blocked:
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

    return {
        "rows": len(all_rows),
        "announcement_rows": len(rows),
        "result_calendar_rows": len(result_calendar_rows),
        "merged_rows": upsert_result["merged"],
        "companies_scanned": companies_scanned,
        "companies_total": int(len(universe)),
        "failed_companies": failed_companies,
        "blocked": blocked,
    }


def main() -> int:
    global STOCKEY_RUN_STATE
    result = run_bse_l3_detection()
    STOCKEY_RUN_STATE = {
        "source": SYNC_SOURCE_NAME,
        "rows": result["rows"],
        "rows_written": result["rows"],
        "companies_scanned": result["companies_scanned"],
        "companies_total": result["companies_total"],
        "failed_companies": result["failed_companies"],
        "blocked": result["blocked"],
        "fallback_used": bool(result["failed_companies"]) or result["blocked"],
        "state_advanced": result["rows"] > 0,
        # a tripped breaker is a partial-success run, not a failed one -- whatever was
        # collected before the trip is real and already upserted; it just isn't "ok"
        # in the sense of having scanned the whole universe.
        "status": "blocked" if result["blocked"] else "ok",
    }
    print(json.dumps(STOCKEY_RUN_STATE, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
