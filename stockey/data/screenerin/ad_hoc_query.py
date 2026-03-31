from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pandas as pd
import requests
from environs import Env
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from data.screenerin.screener_parser import SOURCE_NAME, parse_screener_html
from utils.db import db_session, upsert_to_db
from utils.http import get_dynamic_headers


env = Env()
env.read_env()

CDP_ENDPOINT = env("CDP_ENDPOINT")
DASHBOARD_URL = "https://www.screener.in/dash/"
RAW_SCREEN_URL = "https://www.screener.in/screen/raw/"
RUNS_TABLE = "screenerin_ad_hoc_query_runs"
RESULTS_TABLE = "screenerin_ad_hoc_query_results"
SLUG_RE = re.compile(r"[^a-z0-9]+")


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def slugify(value: str) -> str:
    lowered = clean_text(value).lower()
    slug = SLUG_RE.sub("-", lowered).strip("-")
    return slug or "ad-hoc-query"


def load_query_text(args: argparse.Namespace) -> str:
    if args.query:
        return clean_text(args.query).replace(" AND ", " AND\n")
    if args.query_file:
        return Path(args.query_file).read_text(encoding="utf-8").strip()
    raise SystemExit("Provide --query or --query-file")


def ensure_tables() -> None:
    with db_session() as (_, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {RUNS_TABLE} (
                query_run_id TEXT PRIMARY KEY,
                query_name TEXT,
                query_slug TEXT NOT NULL,
                query_text TEXT NOT NULL,
                query_hash TEXT NOT NULL,
                screener_url TEXT NOT NULL,
                row_count BIGINT NOT NULL DEFAULT 0,
                raw_json TEXT NOT NULL,
                run_ts TIMESTAMPTZ NOT NULL,
                load_ts TIMESTAMPTZ NOT NULL
            )
            """
        )
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {RESULTS_TABLE} (
                query_run_id TEXT NOT NULL,
                query_slug TEXT NOT NULL,
                query_name TEXT,
                run_ts TIMESTAMPTZ NOT NULL,
                company_id BIGINT,
                company_name TEXT NOT NULL,
                ticker TEXT,
                company_url TEXT,
                page_slug TEXT,
                rank BIGINT,
                metrics_json TEXT,
                load_ts TIMESTAMPTZ NOT NULL,
                UNIQUE (query_run_id, company_id, company_name)
            )
            """
        )
        cur.execute(f"ALTER TABLE {RESULTS_TABLE} ADD COLUMN IF NOT EXISTS ticker TEXT")


def open_logged_in_browser():
    playwright = sync_playwright().start()
    browser = playwright.chromium.connect_over_cdp(CDP_ENDPOINT)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()
    return playwright, browser, context, page


def ensure_logged_in(context, page) -> None:
    page.goto(DASHBOARD_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    if "login" not in page.url and "/dash/" in page.url:
        return

    print("Screener.in login required. Complete login in the opened browser tab, then press Enter here.", flush=True)
    print(f"Current page: {page.url}", flush=True)
    input()
    try:
        page.goto(DASHBOARD_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
    except PlaywrightTimeoutError:
        pass
    if "login" in page.url or "/dash/" not in page.url:
        raise SystemExit(f"Screener.in login not detected. Current page: {page.url}")


def build_requests_session_from_context(context) -> requests.Session:
    session = requests.Session()
    session.headers.update(get_dynamic_headers())
    for cookie in context.cookies():
        session.cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain"), path=cookie.get("path"))
    return session


def build_raw_screen_url(query_text: str) -> str:
    return f"{RAW_SCREEN_URL}?{urlencode({'sort': '', 'order': '', 'source_id': '', 'query': query_text})}"


def execute_query(session: requests.Session, query_text: str) -> tuple[str, str]:
    screener_url = build_raw_screen_url(query_text)
    response = session.get(screener_url, timeout=60, allow_redirects=True)
    response.raise_for_status()
    if "login" in response.url:
        raise RuntimeError("Screener.in redirected to login while executing raw query")
    return screener_url, response.text


def payload_to_frames(
    *,
    payload: dict[str, Any],
    query_run_id: str,
    query_slug: str,
    query_name: str,
    query_text: str,
    screener_url: str,
    run_ts: datetime,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    query_hash = hashlib.sha256(query_text.encode("utf-8")).hexdigest()
    companies = payload.get("companies") or []
    run_df = pd.DataFrame(
        [
            {
                "query_run_id": query_run_id,
                "query_name": query_name,
                "query_slug": query_slug,
                "query_text": query_text,
                "query_hash": query_hash,
                "screener_url": screener_url,
                "row_count": len(companies),
                "raw_json": json.dumps(payload, ensure_ascii=False),
                "run_ts": run_ts,
                "load_ts": pd.Timestamp.utcnow(),
            }
        ]
    )
    result_rows: list[dict[str, Any]] = []
    for company in companies:
        result_rows.append(
            {
                "query_run_id": query_run_id,
                "query_slug": query_slug,
                "query_name": query_name,
                "run_ts": run_ts,
                "company_id": company.get("company_id"),
                "company_name": company.get("name"),
                "ticker": company.get("ticker"),
                "company_url": company.get("url"),
                "page_slug": company.get("page_slug"),
                "rank": company.get("s_no"),
                "metrics_json": json.dumps(company.get("metrics") or {}, ensure_ascii=False),
                "load_ts": pd.Timestamp.utcnow(),
            }
        )
    results_df = pd.DataFrame(result_rows)
    return run_df, results_df


def persist_query(run_df: pd.DataFrame, results_df: pd.DataFrame) -> None:
    ensure_tables()
    with db_session() as (_, cur):
        cur.execute(f"DELETE FROM {RESULTS_TABLE} WHERE query_run_id = %s", (run_df.iloc[0]["query_run_id"],))
    upsert_to_db(run_df, RUNS_TABLE, unique_keys=["query_run_id"])
    if not results_df.empty:
        upsert_to_db(results_df, RESULTS_TABLE, unique_keys=["query_run_id", "company_id", "company_name"])


def run_ad_hoc_query(*, query_text: str, query_name: str | None = None) -> dict[str, Any]:
    query_name = clean_text(query_name) or "Ad hoc Screener query"
    query_slug = slugify(query_name)
    query_run_id = str(uuid.uuid4())
    run_ts = datetime.now(timezone.utc)

    playwright, browser, context, page = open_logged_in_browser()
    try:
        ensure_logged_in(context, page)
        session = build_requests_session_from_context(context)
        screener_url, html = execute_query(session, query_text)
    finally:
        page.close()
        context.close()
        browser.close()
        playwright.stop()

    payload = parse_screener_html(html, url=screener_url)
    payload["source_name"] = SOURCE_NAME
    payload["query_text"] = query_text
    payload["query_name"] = query_name
    payload["query_slug"] = query_slug
    run_df, results_df = payload_to_frames(
        payload=payload,
        query_run_id=query_run_id,
        query_slug=query_slug,
        query_name=query_name,
        query_text=query_text,
        screener_url=screener_url,
        run_ts=run_ts,
    )
    persist_query(run_df, results_df)
    return {
        "status": "ok",
        "query_run_id": query_run_id,
        "query_name": query_name,
        "query_slug": query_slug,
        "row_count": int(len(results_df)),
        "screener_url": screener_url,
        "headers": payload.get("headers") or [],
        "companies": companies_preview(payload.get("companies") or []),
    }


def companies_preview(companies: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for company in companies[:limit]:
        rows.append(
            {
                "company_name": company.get("name"),
                "ticker": company.get("ticker"),
                "company_url": company.get("url"),
                "rank": company.get("s_no"),
                "metrics": company.get("metrics") or {},
            }
        )
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run an ad hoc Screener.in raw query and store the results.")
    parser.add_argument("--query", help="Screener.in query text")
    parser.add_argument("--query-file", help="Path to a file containing Screener.in query text")
    parser.add_argument("--name", help="Friendly name for this ad hoc query")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    query_text = load_query_text(args)
    result = run_ad_hoc_query(query_text=query_text, query_name=args.name)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
