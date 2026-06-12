from __future__ import annotations

import argparse
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pandas as pd
import requests

from data.screenerin.auth import build_authenticated_requests_session, ensure_screener_logged_in, open_screener_browser_session
from data.screenerin.failure_log import record_screener_failure
from data.screenerin.query_validation import assert_valid_screener_query
from data.screenerin.screener_parser import SOURCE_NAME, parse_screener_html
from utils.db import db_session, execute_db_operation, upsert_to_db
from utils.schema_migrations import apply_schema_migration

RAW_SCREEN_URL = "https://www.screener.in/screen/raw/"
RUNS_TABLE = "screenerin_ad_hoc_query_runs"
RESULTS_TABLE = "screenerin_ad_hoc_query_results"
AD_HOC_QUERY_SCHEMA_MIGRATION_ID = "20260611_screenerin_ad_hoc_query_base"
AD_HOC_QUERY_SCHEMA_STATEMENTS = [
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
    """,
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
    """,
    f"ALTER TABLE {RESULTS_TABLE} ADD COLUMN IF NOT EXISTS ticker TEXT",
]
SLUG_RE = re.compile(r"[^a-z0-9]+")
STOCKEY_RUN_STATE: dict[str, Any] = {}


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
    apply_schema_migration(
        migration_id=AD_HOC_QUERY_SCHEMA_MIGRATION_ID,
        description="Create Screener.in ad hoc query run and result tables.",
        statements=AD_HOC_QUERY_SCHEMA_STATEMENTS,
        metadata={"tables": [RUNS_TABLE, RESULTS_TABLE], "source": "screener.in"},
    )


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
                "raw_json": json.dumps(payload, ensure_ascii=False, default=str),
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

    def _delete_existing_results() -> None:
        with db_session() as (_, cur):
            cur.execute(f"DELETE FROM {RESULTS_TABLE} WHERE query_run_id = %s", (run_df.iloc[0]["query_run_id"],))

    execute_db_operation(
        _delete_existing_results,
        operation_name="screener_ad_hoc_query:delete_existing_results",
    )
    upsert_to_db(run_df, RUNS_TABLE, unique_keys=["query_run_id"])
    if not results_df.empty:
        upsert_to_db(results_df, RESULTS_TABLE, unique_keys=["query_run_id", "company_id", "company_name"])


def fetch_ad_hoc_payload(*, query_text: str, query_name: str | None = None, persist: bool = True) -> dict[str, Any]:
    query_name = clean_text(query_name) or "Ad hoc Screener query"
    query_slug = slugify(query_name)
    query_run_id = str(uuid.uuid4())
    run_ts = datetime.now(timezone.utc)
    screener_url = build_raw_screen_url(query_text)
    try:
        assert_valid_screener_query(query_text)
    except Exception as exc:
        record_screener_failure(
            failure_stage="ad_hoc_validation",
            error=exc,
            screener_url=screener_url,
            query_text=query_text,
            query_name=query_name,
        )
        raise

    browser_session = open_screener_browser_session()
    html: str | None = None
    try:
        ensure_screener_logged_in(browser_session.context, browser_session.page)
        session = build_authenticated_requests_session(browser_session.context)
        screener_url, html = execute_query(session, query_text)
    except Exception as exc:
        record_screener_failure(
            failure_stage="ad_hoc_fetch",
            error=exc,
            screener_url=screener_url,
            query_text=query_text,
            query_name=query_name,
            html=html,
        )
        raise
    finally:
        browser_session.close()

    try:
        payload = parse_screener_html(html or "", url=screener_url)
    except Exception as exc:
        record_screener_failure(
            failure_stage="ad_hoc_parse",
            error=exc,
            screener_url=screener_url,
            query_text=query_text,
            query_name=query_name,
            html=html,
        )
        raise
    payload["source_name"] = SOURCE_NAME
    payload["query_text"] = query_text
    payload["query_name"] = query_name
    payload["query_slug"] = query_slug
    payload["query_run_id"] = query_run_id
    payload["screener_url"] = screener_url
    payload["run_ts"] = run_ts
    run_df, results_df = payload_to_frames(
        payload=payload,
        query_run_id=query_run_id,
        query_slug=query_slug,
        query_name=query_name,
        query_text=query_text,
        screener_url=screener_url,
        run_ts=run_ts,
    )
    if persist:
        persist_query(run_df, results_df)
    return payload


def run_ad_hoc_query(*, query_text: str, query_name: str | None = None) -> dict[str, Any]:
    payload = fetch_ad_hoc_payload(query_text=query_text, query_name=query_name)
    return {
        "status": "ok",
        "query_run_id": payload.get("query_run_id"),
        "query_name": payload.get("query_name"),
        "query_slug": payload.get("query_slug"),
        "row_count": int(len(payload.get("companies") or [])),
        "screener_url": payload.get("screener_url"),
        "headers": payload.get("headers") or [],
        "companies": companies_preview(payload.get("companies") or []),
    }


def build_run_state(result: dict[str, Any], *, query_text: str, persisted: bool = True) -> dict[str, Any]:
    row_count = int(result.get("row_count") or 0)
    return {
        "source": "screener.in",
        "query_run_id": result.get("query_run_id"),
        "query_name": result.get("query_name"),
        "query_slug": result.get("query_slug"),
        "query_hash": hashlib.sha256(query_text.encode("utf-8")).hexdigest(),
        "screener_url": result.get("screener_url"),
        "rows": row_count,
        "rows_read": row_count,
        "rows_written": row_count if persisted else 0,
        "fallback_used": False,
        "state_advanced": bool(persisted and result.get("query_run_id")),
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
    global STOCKEY_RUN_STATE
    args = parse_args()
    query_text = load_query_text(args)
    result = run_ad_hoc_query(query_text=query_text, query_name=args.name)
    STOCKEY_RUN_STATE = build_run_state(result, query_text=query_text, persisted=True)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
