"""NSE PIT (Prohibition of Insider Trading) collector -- fundamental screener step 5,
NSE half (docs/FUNDAMENTAL_SCREENER_PRD.md sec 8 step 5). Structured, no PDF/OCR: NSE's
`api/corporates-pit-gg` endpoint lists insider-trading disclosure filings, and each
filing's own XBRL XML document (linked from that list, fetched separately) carries
typed fields -- acquirer name, transaction type, quantity, value, holding % before/
after -- everything fundamentals/collectors/events_store.py's cross-source dedup
needs, unlike BSE's announcement-text detection (fundamentals/collectors/
bse_announcements.py) which only tells you a PIT filing HAPPENED, not its quantity.

Adapted from the pre-2026-07-27 pure-TA collector (data/nseindia/insider_deals.py --
deleted in the REMOVE-scope cut, `git show fb84396^:data/nseindia/insider_deals.py`;
removed for lacking backtestable trading-signal value in the OLD advisory pipeline
("nseindia/insider_deals ... sweep showed IC ~ 0", docs/DATA_INVENTORY.md) -- not
because the data or the fetch mechanism were broken. Reused here for a fundamentally
different purpose (forecast calibration, not backtested alpha) where that finding
doesn't apply.

NSE endpoint migration (found live 2026-08-15, fixing a zero-rows regression):
the old `api/corporates-pit` endpoint (this module's original implementation, and
still what `data/nseindia/insider_deals.py` used) returned `{"acqNameList":[],
"data":[]}` for every query, including a market-wide, no-filter query spanning a
window with a real, BSE-confirmed disclosure -- an NSE-side deprecation, not a
parameter bug (ruled out live via CDP: same empty result from a real NSE-UI-driven
session, not just this collector's own construction of the URL). The real NSE
insider-trading UI page (companies-listing/corporate-filings-insider-trading) now
calls `api/corporates-pit-gg` instead. That endpoint's list rows dropped the old
endpoint's embedded flat acquirer/quantity/transaction-type fields (still present in
tests/test_data_platform.py's `test_build_pit_row_matches_a_real_captured_disclosure`
2026-08-10 fixture -- kept as a dated historical record, not exercising current code)
-- they're metadata only (symbol, companyName, links to an ixbrl HTML rendering and
an xmlFileName XBRL document). fetch_pit_disclosure_xml()/parse_pit_xbrl() below fetch
and parse that XBRL document per matched filing to recover the same fields.

This migration also changed this collector's own shape for the better: the old
endpoint required one filtered request per company (`symbol`+`issuer` params) --
200+ NSE requests a run, each spaced NSE_MIN_REQUEST_INTERVAL_SECONDS apart, most
returning nothing. `corporates-pit-gg` still accepts those filters (confirmed live)
but doesn't need to: a single market-wide request (index=equities, from/to date, no
symbol filter) returns every NSE-listed company's disclosures for the window in one
call, matched locally against the L1 universe by symbol. The only remaining
per-company-scale request volume is the XBRL XML detail fetch, and only for filings
that actually matched the universe -- typically a small fraction of the market-wide
list, not one request per universe company regardless of whether it filed anything.

Every corporates-pit-gg/XBRL fetch is wrapped in nse_request_gate() (via
fetch_pit_disclosures()/nse_goto()), same discipline the old version already had --
predating the 2026-08-09/10 NSE WAF lesson that the whole exchange_rate_limiter
module exists to fix. Do not nest nse_request_gate()/nse_goto() calls inside each
other -- a second flock() acquisition from the same process on the same lock file is
a different open-file-description and will not recognize the first as already held,
so it spins until the 900s timeout rather than reentering.

Same crawl-time scoping and block-safety discipline as the BSE collector (explicit
user instruction 2026-08-10 -- do not hammer NSE/BSE, wait out blocking rather than
retry aggressively): a consecutive-failure circuit breaker (now scoped to the XBRL
detail-fetch loop, the actual per-item request loop under this design) that stops the
whole run rather than retrying into a block, and STOCKEY_RUN_STATE/fallback_telemetry
throughout.

Identity: isin resolved via dim_security, company_master_id via
map_company_master_ids_nse_or_bse -- both through fundamentals/collectors/
events_store.py, not a new Dhan dependency (the old collector used
data.dhanlive.dhan_db.get_nse_equity for this; fundamentals doesn't otherwise touch
Dhan internals). No longer resolves an `issuer` display name: that was only ever
needed as a query param for the old per-company-filtered endpoint call, which this
module no longer makes.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import pandas as pd
from environs import Env
from playwright.sync_api import sync_playwright

from fundamentals.collectors.events_store import resolve_isin, upsert_events_with_dedup
from fundamentals.collectors.screenerin import to_number
from fundamentals.screens.l1_universe import load_l1_universe_tickers
from utils.company_master import map_company_master_ids_nse_or_bse
from utils.fallback_telemetry import record_local_fallback_event
from utils.nse_rate_limiter import nse_goto, nse_request_gate

env = Env()
env.read_env()

CDP_ENDPOINT = env("CDP_ENDPOINT", "")
SYNC_SOURCE_NAME = "fundamentals.collectors.nse_pit"
STOCKEY_RUN_STATE: dict[str, object] = {}

LOOKBACK_DAYS = env.int("NSE_PIT_LOOKBACK_DAYS", 7)
# Same rationale as bse_announcements.py's CIRCUIT_BREAKER_THRESHOLD.
CIRCUIT_BREAKER_THRESHOLD = 3
# Re-warm the session against the homepage every this-many XBRL detail fetches,
# matching the old collector's own cadence (data/nseindia/insider_deals.py) -- long-
# lived NSE sessions occasionally need a fresh cookie round-trip.
SESSION_REFRESH_EVERY = 10

# Matches one <in-bse-co:Tag attr="..." contextRef="...">text</in-bse-co:Tag> element
# out of the XBRL body Chrome's built-in XML viewer serializes into page.content()
# (see fetch_pit_disclosure_xml/parse_pit_xbrl) -- attribute order in real XBRL isn't
# guaranteed, so contextRef is pulled separately from the captured attribute blob
# rather than assumed to sit right after the tag name; re.DOTALL because a person
# name has been observed live wrapping onto a second line (e.g. an Employee Welfare
# Trust's registered name).
_XBRL_ELEMENT_RE = re.compile(r'<in-bse-co:([A-Za-z]+)([^>]*)>(.*?)</in-bse-co:\1>', re.DOTALL)
_XBRL_CONTEXT_REF_RE = re.compile(r'contextRef="([^"]*)"')


def _record_fallback(fallback_type: str, *, reason: str, error, severity: str = "warn", metadata=None) -> None:
    record_local_fallback_event(
        module=SYNC_SOURCE_NAME,
        source="nse",
        fallback_type=fallback_type,
        severity=severity,
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def resolve_company_identity(tickers: pd.Series) -> pd.DataFrame:
    """L1-universe ticker -> (company_master_id, isin), for matching NSE's market-wide
    corporates-pit-gg disclosure list locally by symbol and for the cross-source dedup
    key (fundamentals/collectors/events_store.py). NSE first, BSE-ticker fallback
    (map_company_master_ids_nse_or_bse) since screener.in can report a company's BSE
    scrip code even when it's genuinely NSE-listed -- see that function's docstring."""
    company_master_ids = map_company_master_ids_nse_or_bse(tickers)
    return pd.DataFrame(
        {
            "company_master_id": company_master_ids,
            "isin": resolve_isin(company_master_ids),
        },
        index=tickers.index,
    )


class NsePitBlockedError(RuntimeError):
    """Raised internally when a single corporates-pit-gg/XBRL fetch looks like a block
    (fetch threw, or the response is missing the expected shape) -- caught per-request
    so the caller can count it towards the circuit breaker rather than crashing."""


def fetch_pit_disclosures(page, *, from_date: datetime, to_date: datetime) -> list[dict]:
    """Market-wide (no symbol/issuer filter) corporates-pit-gg list -- one request
    covers every NSE-listed company's disclosures in the window; matched against the
    L1 universe locally (see run_nse_pit_detection) instead of looping one filtered
    request per company the way the old corporates-pit endpoint required (confirmed
    live 2026-08-15 that corporates-pit-gg still accepts symbol/issuer filters, but
    unfiltered is both simpler and far cheaper in request volume). Each row is
    filing-level metadata only, not the flat acquirer/quantity fields -- see module
    docstring."""
    params = {
        "index": "equities",
        "from_date": from_date.strftime("%d-%m-%Y"),
        "to_date": to_date.strftime("%d-%m-%Y"),
    }
    url = f"https://www.nseindia.com/api/corporates-pit-gg?{urlencode(params)}"
    with nse_request_gate():
        try:
            payload = page.evaluate(
                """async (url) => {
                    const res = await fetch(url, { credentials: 'same-origin' });
                    if (!res.ok) throw new Error('HTTP ' + res.status);
                    return await res.json();
                }""",
                url,
            )
        except Exception as exc:  # noqa: BLE001 -- any JS/network failure is a block signal here
            raise NsePitBlockedError(str(exc)) from exc
    if not isinstance(payload, dict) or "data" not in payload:
        raise NsePitBlockedError("response missing expected 'data' key")
    return payload["data"] or []


def fetch_pit_disclosure_xml(page, *, xml_url: str) -> str:
    """Fetch one filing's XBRL XML body via navigation, not fetch(): nsearchives.
    nseindia.com is a different origin than www.nseindia.com, and a same-origin
    fetch() call from a www.nseindia.com page throws for a cross-origin URL
    (confirmed live 2026-08-15). nse_goto() already applies nse_request_gate() --
    do not wrap this call in a second one (see module docstring's nesting warning)."""
    try:
        nse_goto(page, xml_url, wait_until="domcontentloaded")
        return page.content()
    except Exception as exc:  # noqa: BLE001 -- any navigation failure is a block signal here
        raise NsePitBlockedError(str(exc)) from exc


def parse_pit_xbrl(xml_text: str) -> dict:
    """One corporates-pit-gg filing's XBRL XML -> filing-level fields (XBRL context
    'MainI') plus a list of per-person/per-transaction disclosure dicts (contexts
    'Disclosure1', 'Disclosure2', ... -- a single filing can cover more than one
    person/transaction, confirmed live 2026-08-15 against a real multi-context Sonata
    Software Employee Welfare Trust filing). Regex-based rather than xml.etree:
    page.content() returns Chrome's live DOM serialization of its built-in XML viewer
    (a style/div wrapper around the real elements, not the original response bytes),
    so the tag/contextRef/text triple is matched directly instead of first
    reconstructing a standalone XML document to hand to a strict parser. That
    serialization DOUBLY HTML-escapes text content (found live 2026-08-15: a real
    company name came back as "AVONMORE CAPITAL &amp;amp; MANAGEMENT SERVICES
    LIMITED", i.e. `&amp;amp;` -- the XBRL source's own `&amp;` escaping, escaped a
    second time by Chrome's HTML-based XML-viewer serialization), so every captured
    text value is unescaped TWICE before use -- a single pass only recovers `&amp;`,
    not the literal `&`. Names/text containing &, <, >, ', or " would otherwise
    corrupt insider_name/headline/raw_json downstream."""
    fields: dict[str, dict[str, str]] = {}
    for tag, attrs, text in _XBRL_ELEMENT_RE.findall(xml_text):
        context_match = _XBRL_CONTEXT_REF_RE.search(attrs)
        if not context_match:
            continue
        fields.setdefault(context_match.group(1), {})[tag] = html.unescape(html.unescape(text.strip()))
    # Numeric sort, not lexicographic -- found live 2026-08-15 on a real 11-disclosure
    # filing (JSW Steel Employees Welfare Trust ESOP accounts): plain sorted() on the
    # string "Disclosure10"/"Disclosure11" orders them before "Disclosure2".."Disclosure9",
    # scrambling the natural person-order used for the news_id idx suffix.
    disclosure_contexts = sorted(
        (c for c in fields if c.startswith("Disclosure")),
        key=lambda c: int(re.sub(r"\D", "", c) or 0),
    )
    return {"filing": fields.get("MainI", {}), "disclosures": [fields[c] for c in disclosure_contexts]}


def _parse_nse_pit_date(value: str | None):
    """Filing-metadata dates come as either NSE's `DD-Mon-YYYY` list-row format or
    the XBRL document's own ISO `YYYY-MM-DD` -- try both rather than assuming."""
    if not value:
        return None
    value = value.strip()
    for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _parse_nse_pit_timestamp(value: str | None):
    if not value:
        return None
    for fmt in ("%d-%b-%Y %H:%M", "%d-%b-%Y %H:%M:%S"):
        try:
            return pd.Timestamp(datetime.strptime(value.strip(), fmt), tz="Asia/Kolkata").tz_convert("UTC")
        except ValueError:
            continue
    return None


def build_pit_rows(*, symbol: str, company_master_id: str, isin: str | None, app_id: str | None, xml_url: str | None = None, detail_url: str | None, broadcast_datetime: str | None, filing: dict, disclosures: list[dict]) -> list[dict]:
    """One corporates-pit-gg filing -> one fundamentals_events row per XBRL
    'Disclosure' context (see parse_pit_xbrl). isin prefers the XBRL filing's own
    ISINCode over the L1-identity-resolved one when both are present -- NSE's own
    filing is the more authoritative, live source for it."""
    resolved_isin = filing.get("ISINCode") or isin
    filing_date = _parse_nse_pit_date(filing.get("DateOfFiling"))
    announcement_timestamp = _parse_nse_pit_timestamp(broadcast_datetime)
    # MEDIUM FINDING (re-audit 2026-08-18): disclosure_date here used to be
    # DateOfIntimationToCompany (per-disclosure, when the insider told the company
    # about their trade) while bse_announcements.py's own disclosure_date is
    # DissemDT's date (when the exchange disseminated/broadcast the announcement) --
    # two genuinely different dates for the SAME underlying disclosure, confirmed
    # live to differ by 3 days on a real filing. events_store.py's cross-source
    # dedup match key is (isin, filing_type, disclosure_date) with EXACT date
    # equality (see its own docstring on why -- disclosure_date is TEXT, not DATE,
    # so even a tolerance-window match would need a real migration first), so the
    # intended "BSE detects, NSE fills in quantity" merge missed in the normal
    # case. Now uses announcement_timestamp's own date (broadcast/dissemination),
    # matching BSE's semantic exactly, falling back to the filing's own
    # DateOfFiling when broadcast_datetime isn't parseable. The more precise
    # DateOfIntimationToCompany is NOT lost -- it's still in raw_json's own
    # disclosure dict below for anyone who needs the finer distinction.
    disclosure_date = (announcement_timestamp.date() if announcement_timestamp is not None else None) or filing_date
    rows = []
    for idx, disclosure in enumerate(disclosures, start=1):
        insider_name = disclosure.get("NameOfThePerson")
        transaction_type = disclosure.get("SecuritiesAcquiredOrDisposedTransactionType")
        quantity_raw = disclosure.get("SecuritiesAcquiredOrDisposedNumberOfSecurity")
        rows.append(
            {
                "source": "nse",
                # The XBRL document URL (xmlFileName) is the natural key, not appId:
                # confirmed live 2026-08-18 that appId is a small, market-wide daily
                # counter (~19/day, observed ranging ~1000 in mid-June 2026 to ~2300
                # in mid-August 2026 -- consistent with an annual reset near the
                # fiscal year boundary) rather than a globally unique document id, so
                # two unrelated filings months apart can share an appId and silently
                # overwrite each other under fundamentals_events' (source, news_id)
                # upsert key. xml_url is per-document (the caller only reaches this
                # function after successfully fetching that exact document, so it is
                # always present in practice) and revision-safe the same way appId
                # was intended to be: a revised filing gets its own xmlFileName just
                # as it gets its own appId (prevAppId points back to the original).
                # app_id is kept as a defense-in-depth fallback only, for the
                # theoretical case xml_url is missing.
                "news_id": f"nse-pit:{xml_url or app_id}:{idx}",
                "scrip_code": symbol,
                "company_master_id": company_master_id,
                "isin": resolved_isin,
                "filing_type": "pit_sast",
                "headline": f"{insider_name or 'Insider'} -- {transaction_type or 'transaction'} of {quantity_raw or '?'} shares",
                "subcategory": disclosure.get("CategoryOfPerson"),
                "disclosure_date": disclosure_date,
                "announcement_timestamp": announcement_timestamp,
                "quantity": to_number(quantity_raw),
                "insider_name": insider_name,
                "transaction_type": transaction_type,
                "attachment_name": None,
                "detail_url": detail_url,
                "detection_source": "nse_corporates_pit",
                # Already fully structured -- no OCR/extraction step needed for this row,
                # unlike a BSE announcement-text detection row (stays "pending" there).
                "enrichment_status": "structured",
                "sources": "nse",
                "raw_json": json.dumps({"filing": filing, "disclosure": disclosure}, ensure_ascii=False, default=str),
                "load_ts": pd.Timestamp.now(tz="UTC"),
            }
        )
    return rows


def run_nse_pit_detection(*, limit: int | None = None, lookback_days: int | None = None) -> dict[str, object]:
    universe = load_l1_universe_tickers()
    if limit:
        universe = universe.head(limit)
    if universe.empty:
        _record_fallback(
            "l3_nse_no_l1_universe",
            reason="NSE PIT detection found no fundamentals_l1_universe rows to scope the crawl to.",
            error="empty L1 universe",
        )
        return {"rows": 0, "filings_scanned": 0, "failed_filings": [], "blocked": False}

    universe = universe.join(resolve_company_identity(universe["ticker"]))
    universe_by_symbol = {
        str(ticker).upper(): (company_master_id, isin if pd.notna(isin) else None)
        for ticker, company_master_id, isin in zip(universe["ticker"], universe["company_master_id"], universe["isin"])
    }

    to_date = datetime.now(timezone.utc)
    from_date = to_date - timedelta(days=lookback_days if lookback_days is not None else LOOKBACK_DAYS)

    rows: list[dict] = []
    failed_filings: list[str] = []
    consecutive_failures = 0
    blocked = False
    filings_scanned = 0
    matched: list[dict] = []

    if not CDP_ENDPOINT:
        _record_fallback(
            "l3_nse_no_cdp_endpoint",
            reason="CDP_ENDPOINT is not configured; NSE PIT detection cannot open a browser session.",
            error="missing CDP_ENDPOINT",
            severity="error",
        )
        return {"rows": 0, "filings_scanned": 0, "failed_filings": [], "blocked": False}

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(CDP_ENDPOINT)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        try:
            nse_goto(page, "https://www.nseindia.com")
            page.wait_for_timeout(1500)

            try:
                filings = fetch_pit_disclosures(page, from_date=from_date, to_date=to_date)
            except Exception as exc:  # noqa: BLE001 -- the list fetch is a single hard dependency
                _record_fallback(
                    "l3_nse_pit_list_fetch_failed",
                    reason="NSE corporates-pit-gg market-wide list fetch failed; no PIT detection this run.",
                    error=exc,
                    severity="error",
                )
                return {"rows": 0, "filings_scanned": 0, "failed_filings": [], "blocked": True}

            # MEDIUM FINDING (re-audit 2026-08-18): an empty market-wide list used to
            # report status:"ok" identically to "we got real data but nothing matched
            # our universe today" (both being ordinary, expected outcomes on plenty of
            # days) -- nothing distinguished a genuinely healthy zero from NSE's own
            # endpoint silently regressing to empty (the exact pre-migration failure
            # mode this module's own docstring already documents for the OLD
            # corporates-pit endpoint). Confirmed live: fundamentals_events has zero
            # NSE-sourced rows in production to date, so nothing would have caught a
            # silent regression here yet.
            if not filings:
                _record_fallback(
                    "l3_nse_pit_empty_market_wide_list",
                    reason="NSE corporates-pit-gg returned zero filings market-wide for this window -- possibly a genuine quiet window, but also the exact shape the old corporates-pit endpoint's silent deprecation looked like.",
                    error="empty filings list",
                    severity="warn",
                    metadata={"from_date": str(from_date.date()), "to_date": str(to_date.date())},
                )

            matched = [f for f in filings if str(f.get("symbol", "")).upper() in universe_by_symbol]

            for position, filing_meta in enumerate(matched):
                if position % SESSION_REFRESH_EVERY == 0 and position > 0:
                    nse_goto(page, "https://www.nseindia.com")
                    page.wait_for_timeout(1000)

                symbol = str(filing_meta.get("symbol", "")).upper()
                company_master_id, isin = universe_by_symbol[symbol]
                xml_url = filing_meta.get("xmlFileName")
                if not xml_url:
                    # MEDIUM FINDING (re-audit 2026-08-18): used to skip silently -- a
                    # matched filing with no xmlFileName at all is unexpected (every
                    # real filing observed live has had one), worth surfacing rather
                    # than silently vanishing with no trace in filings_scanned or
                    # failed_filings.
                    _record_fallback(
                        "l3_nse_pit_missing_xml_url",
                        reason="A matched NSE PIT filing had no xmlFileName in its market-wide list metadata -- skipped, not counted as scanned or failed.",
                        error="missing xmlFileName",
                        metadata={"ticker": symbol, "app_id": filing_meta.get("appId")},
                    )
                    continue
                try:
                    xml_text = fetch_pit_disclosure_xml(page, xml_url=xml_url)
                    parsed = parse_pit_xbrl(xml_text)
                except Exception as exc:  # noqa: BLE001 -- classified as a failure either way
                    consecutive_failures += 1
                    failed_filings.append(symbol)
                    _record_fallback(
                        "l3_nse_pit_fetch_failed",
                        reason="NSE PIT XBRL detail fetch failed for this filing; it is missing from this run.",
                        error=exc,
                        metadata={"ticker": symbol, "app_id": filing_meta.get("appId")},
                    )
                    if consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
                        blocked = True
                        _record_fallback(
                            "l3_nse_circuit_breaker_tripped",
                            reason=(
                                f"{consecutive_failures} consecutive NSE requests failed -- stopping this run "
                                "immediately rather than continuing to hit a possibly-blocking NSE. The next "
                                "scheduled run will retry the remaining filings (upsert-keyed, idempotent)."
                            ),
                            error="circuit breaker",
                            severity="error",
                            metadata={"filings_scanned": filings_scanned, "filings_remaining": len(matched) - filings_scanned},
                        )
                        break
                    continue

                consecutive_failures = 0
                filings_scanned += 1
                # MEDIUM FINDING (re-audit 2026-08-18): a successfully-fetched filing
                # whose XBRL parsed to zero disclosure contexts used to silently
                # contribute zero rows with no trace -- could be a genuinely
                # disclosure-less filing, but could also be parse_pit_xbrl's regex-
                # based extraction breaking on an NSE XBRL/serialization shape change
                # (see that function's own docstring on how fragile the Chrome-DOM-
                # serialization workaround is). Worth surfacing either way.
                if not parsed["disclosures"]:
                    _record_fallback(
                        "l3_nse_pit_no_disclosure_contexts",
                        reason="A matched, successfully-fetched NSE PIT filing's XBRL parsed to zero Disclosure* contexts -- contributed zero rows.",
                        error="no disclosure contexts found",
                        metadata={"ticker": symbol, "app_id": filing_meta.get("appId"), "xml_url": xml_url},
                    )
                rows.extend(
                    build_pit_rows(
                        symbol=symbol,
                        company_master_id=company_master_id,
                        isin=isin,
                        app_id=filing_meta.get("appId"),
                        xml_url=xml_url,
                        detail_url=filing_meta.get("ixbrl"),
                        broadcast_datetime=filing_meta.get("broadcastDateTime"),
                        filing=parsed["filing"],
                        disclosures=parsed["disclosures"],
                    )
                )
        finally:
            page.close()

    upsert_result = upsert_events_with_dedup(rows)

    return {
        "rows": len(rows),
        "merged_rows": upsert_result["merged"],
        "filings_scanned": filings_scanned,
        "filings_matched": len(matched),
        "failed_filings": failed_filings,
        "blocked": blocked,
    }


def main() -> int:
    global STOCKEY_RUN_STATE
    result = run_nse_pit_detection()
    STOCKEY_RUN_STATE = {
        "source": SYNC_SOURCE_NAME,
        "rows": result["rows"],
        "rows_written": result["rows"],
        "filings_scanned": result["filings_scanned"],
        "filings_matched": result.get("filings_matched", 0),
        "failed_filings": result["failed_filings"],
        "blocked": result["blocked"],
        "fallback_used": bool(result["failed_filings"]) or result["blocked"],
        "state_advanced": result["rows"] > 0,
        "status": "blocked" if result["blocked"] else "ok",
    }
    print(json.dumps(STOCKEY_RUN_STATE, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
