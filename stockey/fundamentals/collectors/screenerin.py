"""screener.in query infrastructure -- login, authenticated session, and paginated
HTML-table parsing, shared by fundamentals/screens/l1_universe.py and l2_state.py
(both import build_authenticated_session/run_query/clean_text/to_number from here).

Auth/query mechanics adapted from the pre-2026-07-27 advisory/ implementation
(data/screenerin/{auth,ad_hoc_query,screener_parser}.py -- deleted in the pure-TA cut,
still reachable via `git show fb84396^:data/screenerin/` since the old advisory-archive
branch was never pushed and didn't survive this workspace's machine migration). Reused
because the login flow and HTML table parsing were sound, already-tested plumbing, not
the decision logic that made the old system fail.

Query submission: GET https://www.screener.in/screen/raw/?query=<TEXT>, authenticated
via cookies copied from a logged-in Chrome CDP session (the same CDP-session pattern
every other stockey collector already uses -- confirmed 2026-08-10 that an
unauthenticated request redirects straight to screener.in's Register page, custom
queries are login-gated). Response is an HTML page with a results table
(<tr data-row-company-id=...>), not JSON -- parsed via BeautifulSoup.

This module used to also run its own standalone query (the "deleveraging screen",
fundamental screener step 2) and write fundamentals_screenerin_query_results. Retired
2026-08-15: that table had zero readers from the day it was first scheduled -- removed
per "unused tables and code should be removed" rather than keep running a screen
nothing consumes. The shared scraping infra below is unaffected; L1/L2 still depend
on it."""

from __future__ import annotations

import re
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup
from environs import Env
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from utils.cdp import connect_over_cdp as connect_over_cdp_guarded

from utils.exchange_rate_limiter import exchange_request_gate
from utils.fallback_telemetry import record_local_fallback_event
from utils.http import get_dynamic_headers

env = Env()
env.read_env()

CDP_ENDPOINT = env("CDP_ENDPOINT", "")
SCREENER_IN_LOGIN = env("SCREENER_IN_LOGIN", "")
SCREENER_IN_PASSWORD = env("SCREENER_IN_PASSWORD", "")

LOGIN_URL = "https://www.screener.in/login/"
RAW_SCREEN_URL = "https://www.screener.in/screen/raw/"
USERNAME_SELECTOR = "input#id_username[name='username']"
PASSWORD_SELECTOR = "input#id_password[name='password']"
SUBMIT_SELECTOR = "button[type='submit'].button-primary"

SYNC_SOURCE_NAME = "fundamentals.collectors.screenerin"


def _record_fallback(fallback_type: str, *, reason: str, error: Exception | str, severity: str = "warn", metadata=None) -> None:
    record_local_fallback_event(
        module=SYNC_SOURCE_NAME,
        source="screenerin",
        fallback_type=fallback_type,
        severity=severity,
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def _is_logged_in(page) -> bool:
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
    except PlaywrightTimeoutError as exc:
        _record_fallback(
            "screenerin_login_status_timeout",
            reason="Screener.in login-status check timed out; current page state was used.",
            error=exc,
            metadata={"login_url": LOGIN_URL},
        )
    return "login" not in str(page.url).lower() and "/dash/" in str(page.url)


def _auto_login(page) -> None:
    if not SCREENER_IN_LOGIN or not SCREENER_IN_PASSWORD:
        raise RuntimeError("SCREENER_IN_LOGIN and SCREENER_IN_PASSWORD are required for screener.in login")
    if _is_logged_in(page):
        return
    page.fill(USERNAME_SELECTOR, SCREENER_IN_LOGIN)
    page.fill(PASSWORD_SELECTOR, SCREENER_IN_PASSWORD)
    page.click(SUBMIT_SELECTOR)
    try:
        page.wait_for_url("**/dash/**", timeout=30000)
    except PlaywrightTimeoutError as exc:
        _record_fallback(
            "screenerin_dashboard_wait_timeout",
            reason="Screener.in automated login did not observe the dashboard URL before timeout.",
            error=exc,
            metadata={"current_url": str(getattr(page, "url", ""))},
        )
        page.wait_for_timeout(3000)
    if not _is_logged_in(page):
        raise RuntimeError(f"Screener.in login did not reach the dashboard. Current page: {page.url}")


def build_authenticated_session() -> requests.Session:
    """Log in via the shared Chrome CDP session, then copy cookies into a plain
    requests.Session -- subsequent queries don't need Playwright at all."""
    if not CDP_ENDPOINT:
        raise RuntimeError("CDP_ENDPOINT is required for screener.in login")
    with exchange_request_gate(domain="screenerin"):
        with sync_playwright() as playwright:
            browser = connect_over_cdp_guarded(playwright, CDP_ENDPOINT, caller="fundamentals.collectors.screenerin")
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()
            try:
                page.goto(LOGIN_URL, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
                _auto_login(page)
                session = requests.Session()
                session.headers.update(get_dynamic_headers())
                for cookie in context.cookies():
                    session.cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain"), path=cookie.get("path"))
                return session
            finally:
                page.close()


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    return cleaned or None


def to_number(value: str | None):
    value = clean_text(value)
    if value in (None, "", "-", "--", "NA", "N/A"):
        return None
    value = value.replace(",", "")
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?\d*\.\d+", value) or re.fullmatch(r"-?\d+\.\d*", value):
        return float(value)
    return value


def _metric_key(label: str) -> str:
    return label.lower().replace(".", "").replace("%", "pct").replace("/", "_").replace(" ", "_")


def parse_screener_results(html: str) -> list[dict[str, object]]:
    """Parse the raw-query results table: header row defines metric column names,
    each data row carries data-row-company-id, company name+link in the 2nd column,
    remaining columns mapped to metrics by header label."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("div[data-page-results] table")
    if table is None:
        raise ValueError("Could not find screener.in results table in the response")

    headers: list[str] = []
    companies: list[dict[str, object]] = []
    # MEDIUM FINDING (re-audit 2026-08-18): run_query()'s pagination loop decides
    # "was the last page genuinely full" purely from len(companies) == SCREENER_
    # PAGE_SIZE -- if a single row on an otherwise-full page fails to parse here
    # (missing data-row-company-id, or fewer than 2 <td>s -- an unexpected HTML
    # shape, not the header row, which is already excluded by the `if ths` branch
    # above), the returned count silently comes back short with no signal, and
    # run_query would wrongly treat a genuinely-full page as the final one,
    # dropping every company on every page after it. Tracked here and surfaced via
    # fallback telemetry so this is visible rather than a silent undercount.
    skipped_row_count = 0
    for tr in table.select("tr"):
        ths = tr.find_all("th")
        if ths:
            headers = [value for value in (clean_text(th.get_text(" ", strip=True)) for th in ths) if value]
            continue

        company_id = tr.get("data-row-company-id")
        if not company_id:
            skipped_row_count += 1
            continue
        tds = tr.find_all("td")
        if len(tds) < 2:
            skipped_row_count += 1
            continue

        name_link = tds[1].find("a")
        company_url = name_link.get("href") if name_link else None
        ticker = None
        if company_url:
            parts = [p for p in company_url.split("/") if p]
            if len(parts) >= 2 and parts[0] == "company":
                ticker = parts[1]

        metrics: dict[str, object] = {}
        for header, td in zip(headers[2:], tds[2:]):
            metrics[_metric_key(header)] = to_number(td.get_text(" ", strip=True))

        companies.append(
            {
                "company_id": to_number(company_id),
                "name": clean_text(name_link.get_text(" ", strip=True)) if name_link else clean_text(tds[1].get_text(" ", strip=True)),
                "url": company_url,
                "ticker": ticker,
                "metrics": metrics,
            }
        )
    if skipped_row_count:
        _record_fallback(
            "screenerin_query_rows_skipped_during_parse",
            reason=(
                f"{skipped_row_count} <tr> row(s) in this page's results table had neither a "
                "data-row-company-id attribute nor >=2 <td>s -- excluded from the returned company "
                "list. If this page was otherwise full, run_query()'s pagination loop may wrongly "
                "treat it as the final page (len(companies) came back short of SCREENER_PAGE_SIZE) "
                "and silently drop every company on every subsequent page."
            ),
            error="unparseable row(s) in results table",
            metadata={"skipped_row_count": skipped_row_count, "parsed_row_count": len(companies)},
        )
    return companies


def _fetch_query_page(session: requests.Session, query_text: str, *, page: int) -> tuple[str, list[dict[str, object]]]:
    params = {"sort": "", "order": "", "source_id": "", "query": query_text}
    if page > 1:
        params["page"] = page
    screener_url = f"{RAW_SCREEN_URL}?{urlencode(params)}"
    with exchange_request_gate(domain="screenerin"):
        response = session.get(screener_url, timeout=60, allow_redirects=True)
    response.raise_for_status()
    if "login" in str(response.url).lower():
        raise RuntimeError("Screener.in redirected to login while executing a query -- session likely expired")
    return screener_url, parse_screener_results(response.text)


# Confirmed live 2026-08-10 (module docstring's own basis for run_query's pagination
# loop). A fixed expectation, not re-derived from the first page's own length -- see
# run_query's docstring for why that self-referential comparison was the bug.
SCREENER_PAGE_SIZE = 50


def run_query(
    session: requests.Session, query_text: str, *, max_pages: int = 100
) -> tuple[str, list[dict[str, object]]]:
    """Fetch every page of results (screener.in paginates at SCREENER_PAGE_SIZE/page).
    Loops only while the most recently fetched page was actually FULL (a short first
    page means there's nothing more to fetch, full stop), capped at max_pages as a
    safety backstop against a genuine 5000+ match screen.

    BUG FOUND LIVE 2026-08-15, fixed here: the entry/continue condition used to
    compare `len(first_page) == page_size`, where `page_size` was itself just
    `len(first_page)` from the very first fetch -- trivially true on the very first
    check, so the loop always attempted a page 2 fetch even when page 1 already had
    fewer than SCREENER_PAGE_SIZE results (i.e. was already the complete set).
    Confirmed live: a real 4-company query issued a wasted page-2 request, and the
    old `len(page_companies) < page_size` break condition (comparing two equal-
    length pages) never fired either, looping all the way to max_pages issuing up
    to 99 unnecessary requests and duplicating every company in the result. Fixed
    two ways: (1) the loop only continues when the LAST fetched page was genuinely
    full (SCREENER_PAGE_SIZE items), so a short-first-page query never even
    attempts page 2; (2) a company_id-set comparison against the previous page as a
    backstop, catching the remaining edge case of a query with an exact multiple of
    SCREENER_PAGE_SIZE total matches (page 1 full, but no real page 2 either).

    CORRECTED 2026-08-18 (re-audit): the comment above used to claim screener.in
    "silently RE-SERVES page 1's own content again" for an out-of-range page --
    live re-tested against a real multi-page query (Market Cap > 1000, true last
    page = page 40 with 38 results) and requesting pages 60/80/100 all returned
    page 40's own content, NOT page 1's -- screener.in actually CLAMPS TO THE LAST
    VALID PAGE, not page 1 specifically. This loop's own behavior is unaffected by
    the correction (it never requests more than one page past the last full one,
    so the two hypotheses were indistinguishable from this function's own call
    pattern -- fix (2) above's company_id-set backstop still works correctly under
    the true "clamps to last valid page" behavior, since for THIS loop the "last
    valid page" and "page 1" happen to be the same page whenever the exact-multiple
    edge case fires), but the old comment's reasoning would have misled the next
    reader who tried to reason about a DIFFERENT out-of-range page pattern."""
    first_url, first_page = _fetch_query_page(session, query_text, page=1)
    all_companies = list(first_page)

    page = 1
    last_page = first_page
    while len(last_page) == SCREENER_PAGE_SIZE and page < max_pages:
        page += 1
        _, page_companies = _fetch_query_page(session, query_text, page=page)
        if {c.get("company_id") for c in page_companies} == {c.get("company_id") for c in last_page}:
            break  # screener.in re-served the same page -- no real next page exists
        all_companies.extend(page_companies)
        last_page = page_companies

    return first_url, all_companies
