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
            browser = playwright.chromium.connect_over_cdp(CDP_ENDPOINT)
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
    for tr in table.select("tr"):
        ths = tr.find_all("th")
        if ths:
            headers = [value for value in (clean_text(th.get_text(" ", strip=True)) for th in ths) if value]
            continue

        company_id = tr.get("data-row-company-id")
        if not company_id:
            continue
        tds = tr.find_all("td")
        if len(tds) < 2:
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


def run_query(
    session: requests.Session, query_text: str, *, max_pages: int = 100
) -> tuple[str, list[dict[str, object]]]:
    """Fetch every page of results (screener.in paginates at 50/page, confirmed live
    2026-08-10 -- a single-page fetch silently truncates a screen with more than 50
    matches). Loops until a page comes back short of a full page (the natural end of
    results), capped at max_pages as a safety backstop against an unbounded loop from
    a parsing bug rather than a genuine 5000+ match screen."""
    first_url, first_page = _fetch_query_page(session, query_text, page=1)
    all_companies = list(first_page)
    page_size = len(first_page)

    page = 1
    while page_size > 0 and len(first_page) == page_size and page < max_pages:
        page += 1
        _, page_companies = _fetch_query_page(session, query_text, page=page)
        all_companies.extend(page_companies)
        if len(page_companies) < page_size:
            break
        first_page = page_companies  # reuse as "last page fetched" for the loop condition

    return first_url, all_companies
