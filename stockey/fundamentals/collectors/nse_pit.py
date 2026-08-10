"""NSE PIT (Prohibition of Insider Trading) collector -- fundamental screener step 5,
NSE half (docs/FUNDAMENTAL_SCREENER_PRD.md sec 8 step 5). Structured, no PDF: NSE's
`api/corporates-pit` endpoint returns typed insider-trading disclosures directly --
acquirer name, transaction type, quantity (secAcq), value, holding % before/after --
everything fundamentals/collectors/events_store.py's cross-source dedup needs, unlike
BSE's announcement-text detection (fundamentals/collectors/bse_announcements.py) which
only tells you a PIT filing HAPPENED, not its quantity.

Adapted from the pre-2026-07-27 pure-TA collector (data/nseindia/insider_deals.py --
deleted in the REMOVE-scope cut, `git show fb84396^:data/nseindia/insider_deals.py`;
removed for lacking backtestable trading-signal value in the OLD advisory pipeline
("nseindia/insider_deals ... sweep showed IC ~ 0", docs/DATA_INVENTORY.md) -- not
because the data or the fetch mechanism were broken. Reused here for a fundamentally
different purpose (forecast calibration, not backtested alpha) where that finding
doesn't apply. One correction versus the old version: every `corporates-pit` fetch is
now wrapped in nse_request_gate(), not just page navigation -- the old version relied
on ad-hoc jitter between symbols, predating the 2026-08-09/10 NSE WAF lesson that the
whole exchange_rate_limiter module exists to fix.

Same crawl-time scoping and block-safety discipline as the BSE collector (explicit
user instruction 2026-08-10 -- do not hammer NSE/BSE, wait out blocking rather than
retry aggressively): one company at a time through the L1 universe, a consecutive-
failure circuit breaker that stops the whole run rather than retrying into a block,
and STOCKEY_RUN_STATE/fallback_telemetry throughout.

Identity: issuer name resolved via dim_security.display_name (NSE's API requires both
`symbol` and the registered `issuer` name), isin via the same table -- both through
fundamentals/collectors/events_store.py, not a new Dhan dependency (the old collector
used data.dhanlive.dhan_db.get_nse_equity for this; fundamentals doesn't otherwise
touch Dhan internals).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import pandas as pd
from environs import Env
from playwright.sync_api import sync_playwright

from fundamentals.collectors.events_store import resolve_isin, resolve_issuer_names, upsert_events_with_dedup
from fundamentals.collectors.screenerin import to_number
from fundamentals.screens.l1_universe import load_l1_universe_tickers
from utils.company_master import map_company_master_ids
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
# Re-warm the session against the homepage every this-many companies, matching the old
# collector's own cadence (data/nseindia/insider_deals.py) -- long-lived NSE sessions
# occasionally need a fresh cookie round-trip.
SESSION_REFRESH_EVERY = 10


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
    """NSE ticker -> (company_master_id, isin, issuer) -- issuer is NSE's own
    registered-company-name param for corporates-pit, isin is the cross-source dedup
    key (fundamentals/collectors/events_store.py)."""
    company_master_ids = map_company_master_ids(tickers, exchange="NSE")
    return pd.DataFrame(
        {
            "company_master_id": company_master_ids,
            "isin": resolve_isin(company_master_ids),
            "issuer": resolve_issuer_names(company_master_ids),
        },
        index=tickers.index,
    )


class NsePitBlockedError(RuntimeError):
    """Raised internally when a single corporates-pit fetch looks like a block (fetch
    threw, or the response is missing the expected `data` list) -- caught per-request
    so the caller can count it towards the circuit breaker rather than crashing."""


def fetch_company_pit(page, *, symbol: str, issuer: str, from_date: datetime, to_date: datetime) -> list[dict]:
    params = {
        "index": "equities",
        "from_date": from_date.strftime("%d-%m-%Y"),
        "to_date": to_date.strftime("%d-%m-%Y"),
        "symbol": symbol,
        "issuer": issuer,
    }
    url = f"https://www.nseindia.com/api/corporates-pit?{urlencode(params)}"
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


def _parse_nse_pit_date(value: str | None):
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%d-%b-%Y").date()
    except ValueError:
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


def build_pit_row(symbol: str, company_master_id: str, isin: str | None, raw: dict) -> dict:
    quantity = to_number(raw.get("secAcq"))
    insider_name = raw.get("acqName")
    transaction_type = raw.get("tdpTransactionType")
    disclosure_date = _parse_nse_pit_date(raw.get("intimDt"))
    announcement_timestamp = _parse_nse_pit_timestamp(raw.get("date"))
    return {
        "source": "nse",
        # NSE's own (disclosure_id, person_id) pair is already a stable natural key
        # (the old collector's own unique_keys leaned on `did`/`pid`); date appended
        # defensively in case NSE ever reuses an id pair across a resubmission.
        "news_id": f"nse-pit:{raw.get('did')}:{raw.get('pid')}:{raw.get('intimDt')}",
        "scrip_code": symbol,
        "company_master_id": company_master_id,
        "isin": isin,
        "filing_type": "pit_sast",
        "headline": f"{insider_name or 'Insider'} -- {transaction_type or 'transaction'} of {raw.get('secAcq') or '?'} shares",
        "subcategory": raw.get("personCategory"),
        "disclosure_date": disclosure_date,
        "announcement_timestamp": announcement_timestamp,
        "quantity": quantity,
        "insider_name": insider_name,
        "transaction_type": transaction_type,
        "attachment_name": None,
        "detail_url": None,
        "detection_source": "nse_corporates_pit",
        # Already fully structured -- no OCR/extraction step needed for this row,
        # unlike a BSE announcement-text detection row (stays "pending" there).
        "enrichment_status": "structured",
        "sources": "nse",
        "raw_json": json.dumps(raw, ensure_ascii=False, default=str),
        "load_ts": pd.Timestamp.now(tz="UTC"),
    }


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
        return {"rows": 0, "companies_scanned": 0, "failed_companies": [], "blocked": False}

    universe = universe.join(resolve_company_identity(universe["ticker"]))
    missing_issuer = universe[universe["issuer"].isna()]
    if not missing_issuer.empty:
        _record_fallback(
            "l3_nse_issuer_missing",
            reason="Some L1-universe companies have no resolved dim_security display_name (NSE's issuer param); they are skipped this run, not silently dropped.",
            error="unresolved issuer name",
            metadata={"tickers": list(missing_issuer["ticker"])[:50], "count": int(len(missing_issuer))},
        )
    universe = universe.dropna(subset=["issuer"])

    to_date = datetime.now(timezone.utc)
    from_date = to_date - timedelta(days=lookback_days if lookback_days is not None else LOOKBACK_DAYS)

    rows: list[dict] = []
    failed_companies: list[str] = []
    consecutive_failures = 0
    blocked = False
    companies_scanned = 0

    if not CDP_ENDPOINT:
        _record_fallback(
            "l3_nse_no_cdp_endpoint",
            reason="CDP_ENDPOINT is not configured; NSE PIT detection cannot open a browser session.",
            error="missing CDP_ENDPOINT",
            severity="error",
        )
        return {"rows": 0, "companies_scanned": 0, "failed_companies": [], "blocked": False}

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(CDP_ENDPOINT)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        try:
            nse_goto(page, "https://www.nseindia.com")
            page.wait_for_timeout(1500)

            for position, (_, company) in enumerate(universe.iterrows()):
                if position % SESSION_REFRESH_EVERY == 0 and position > 0:
                    nse_goto(page, "https://www.nseindia.com")
                    page.wait_for_timeout(1000)

                symbol = company["ticker"]
                isin = company["isin"] if pd.notna(company.get("isin")) else None
                try:
                    raw_rows = fetch_company_pit(page, symbol=symbol, issuer=company["issuer"], from_date=from_date, to_date=to_date)
                except Exception as exc:  # noqa: BLE001 -- classified as a failure either way
                    consecutive_failures += 1
                    failed_companies.append(symbol)
                    _record_fallback(
                        "l3_nse_pit_fetch_failed",
                        reason="NSE corporates-pit fetch failed for this company; it is missing from this run.",
                        error=exc,
                        metadata={"ticker": symbol},
                    )
                    if consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
                        blocked = True
                        _record_fallback(
                            "l3_nse_circuit_breaker_tripped",
                            reason=(
                                f"{consecutive_failures} consecutive NSE requests failed -- stopping this run "
                                "immediately rather than continuing to hit a possibly-blocking NSE. The next "
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
                    rows.append(build_pit_row(symbol, company["company_master_id"], isin, raw))
        finally:
            page.close()

    upsert_result = upsert_events_with_dedup(rows)

    return {
        "rows": len(rows),
        "merged_rows": upsert_result["merged"],
        "companies_scanned": companies_scanned,
        "companies_total": int(len(universe)),
        "failed_companies": failed_companies,
        "blocked": blocked,
    }


def main() -> int:
    global STOCKEY_RUN_STATE
    result = run_nse_pit_detection()
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
        "status": "blocked" if result["blocked"] else "ok",
    }
    print(json.dumps(STOCKEY_RUN_STATE, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
