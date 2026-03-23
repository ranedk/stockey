from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from environs import Env
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright
from psycopg2.extras import Json

from utils.db import db_session, sql_to_df


env = Env()
env.read_env()

CDP_ENDPOINT = env("CDP_ENDPOINT")
SNAPSHOT_TABLE = "public.dhan_screener_snapshots"
REGISTRY_TABLE = "public.dhan_screeners"


def ensure_tables() -> None:
    with db_session() as (_, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SNAPSHOT_TABLE} (
                screener_slug TEXT NOT NULL,
                screener_name TEXT NOT NULL,
                screener_url TEXT NOT NULL,
                date DATE NOT NULL,
                raw_json JSONB NOT NULL,
                load_ts TIMESTAMPTZ NOT NULL,
                UNIQUE (date, screener_slug)
            )
            """
        )
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {REGISTRY_TABLE} (
                screener_slug TEXT PRIMARY KEY,
                screener_name TEXT NOT NULL,
                screener_url TEXT NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_ts TIMESTAMPTZ NOT NULL,
                updated_ts TIMESTAMPTZ NOT NULL
            )
            """
        )


def fetch_rendered_html(url: str) -> str:
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(CDP_ENDPOINT)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()
            try:
                page.goto(url, wait_until="networkidle")
                return page.content()
            finally:
                page.close()
                browser.close()
    except PlaywrightError as exc:
        raise RuntimeError(
            f"Could not connect to Chrome debugging endpoint at {CDP_ENDPOINT}. "
            "Start Chrome with remote debugging enabled before running this screener fetch."
        ) from exc


def parse_ng_state_from_html(html: str) -> tuple[dict, BeautifulSoup]:
    soup = BeautifulSoup(html, "html.parser")
    tag = (
        soup.find("div", id="ng-state", attrs={"type": "application/json"})
        or soup.find("script", id="ng-state", attrs={"type": "application/json"})
    )
    if not tag:
        raise RuntimeError("ng-state JSON tag not found after JS render")
    payload = json.loads(tag.string or tag.get_text())
    return payload, soup


def extract_screener_slug(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    slug = path.rsplit("/", 1)[-1]
    if not slug:
        raise ValueError(f"Could not determine screener slug from URL: {url}")
    return slug


def extract_screener_name(soup: BeautifulSoup, url: str) -> str:
    title = soup.title.string.strip() if soup.title and soup.title.string else None
    if title:
        return title.split("|", 1)[0].strip()
    slug = extract_screener_slug(url)
    return slug.replace("-", " ").upper()


def derive_screener_name(url: str) -> str:
    slug = extract_screener_slug(url)
    return slug.replace("-", " ").upper()


def upsert_snapshot(
    *,
    screener_slug: str,
    screener_name: str,
    screener_url: str,
    snapshot_date: datetime,
    payload: dict,
) -> None:
    with db_session() as (_, cur):
        cur.execute(
            f"""
            INSERT INTO {SNAPSHOT_TABLE} (
                screener_slug,
                screener_name,
                screener_url,
                date,
                raw_json,
                load_ts
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (date, screener_slug)
            DO UPDATE SET
                screener_name = EXCLUDED.screener_name,
                screener_url = EXCLUDED.screener_url,
                raw_json = EXCLUDED.raw_json,
                load_ts = EXCLUDED.load_ts
            """,
            (
                screener_slug,
                screener_name,
                screener_url,
                snapshot_date.date(),
                Json(payload),
                datetime.now(timezone.utc),
            ),
        )


def sync_screener(url: str, snapshot_date: datetime | None = None) -> dict[str, object]:
    ensure_tables()
    html = fetch_rendered_html(url)
    payload, soup = parse_ng_state_from_html(html)
    screener_slug = extract_screener_slug(url)
    screener_name = extract_screener_name(soup, url)
    effective_date = snapshot_date or datetime.now(timezone.utc)
    upsert_snapshot(
        screener_slug=screener_slug,
        screener_name=screener_name,
        screener_url=url,
        snapshot_date=effective_date,
        payload=payload,
    )
    upsert_registered_screener(
        screener_url=url,
        screener_name=screener_name,
        is_active=True,
    )
    return {
        "screener_slug": screener_slug,
        "screener_name": screener_name,
        "date": effective_date.date().isoformat(),
        "top_level_keys": list(payload.keys())[:10],
    }


def load_snapshot(screener_slug: str, snapshot_date: str | None = None):
    conditions = ["screener_slug = %(screener_slug)s"]
    params: dict[str, object] = {"screener_slug": screener_slug}
    if snapshot_date:
        conditions.append("date = %(snapshot_date)s")
        params["snapshot_date"] = snapshot_date
    where_clause = " AND ".join(conditions)
    return sql_to_df(
        f"""
        SELECT screener_slug, screener_name, screener_url, date, raw_json, load_ts
        FROM {SNAPSHOT_TABLE}
        WHERE {where_clause}
        ORDER BY date DESC
        """,
        params=params,
    )


def upsert_registered_screener(
    *,
    screener_url: str,
    screener_name: str | None = None,
    is_active: bool = True,
) -> dict[str, object]:
    ensure_tables()
    screener_slug = extract_screener_slug(screener_url)
    effective_name = (screener_name or derive_screener_name(screener_url)).strip()
    now = datetime.now(timezone.utc)
    with db_session() as (_, cur):
        cur.execute(
            f"""
            INSERT INTO {REGISTRY_TABLE} (
                screener_slug,
                screener_name,
                screener_url,
                is_active,
                created_ts,
                updated_ts
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (screener_slug)
            DO UPDATE SET
                screener_name = EXCLUDED.screener_name,
                screener_url = EXCLUDED.screener_url,
                is_active = EXCLUDED.is_active,
                updated_ts = EXCLUDED.updated_ts
            """,
            (
                screener_slug,
                effective_name,
                screener_url,
                is_active,
                now,
                now,
            ),
        )
    return {
        "screener_slug": screener_slug,
        "screener_name": effective_name,
        "screener_url": screener_url,
        "is_active": is_active,
    }


def list_registered_screeners(*, active_only: bool = False):
    ensure_tables()
    where_clause = "WHERE is_active = TRUE" if active_only else ""
    return sql_to_df(
        f"""
        SELECT screener_slug, screener_name, screener_url, is_active, created_ts, updated_ts
        FROM {REGISTRY_TABLE}
        {where_clause}
        ORDER BY screener_name, screener_slug
        """
    )


def remove_registered_screener(identifier: str) -> dict[str, object]:
    ensure_tables()
    with db_session() as (_, cur):
        cur.execute(
            f"""
            DELETE FROM {REGISTRY_TABLE}
            WHERE screener_slug = %s OR screener_url = %s
            """,
            (identifier, identifier),
        )
        removed = cur.rowcount
    return {"identifier": identifier, "removed": removed}


def load_registered_urls() -> list[str]:
    df = list_registered_screeners(active_only=True)
    if df.empty:
        return []
    return df["screener_url"].dropna().astype(str).tolist()


def sync_registered_screeners(snapshot_date: datetime | None = None) -> list[dict[str, object]]:
    urls = load_registered_urls()
    results: list[dict[str, object]] = []
    for url in urls:
        results.append(sync_screener(url, snapshot_date=snapshot_date))
    return results


def load_latest_snapshots(screener_slug: str | None = None):
    ensure_tables()
    params: dict[str, object] = {}
    slug_filter = ""
    if screener_slug:
        slug_filter = "WHERE s.screener_slug = %(screener_slug)s"
        params["screener_slug"] = screener_slug
    return sql_to_df(
        f"""
        SELECT s.screener_slug, s.screener_name, s.screener_url, s.date, s.raw_json, s.load_ts
        FROM {SNAPSHOT_TABLE} AS s
        JOIN (
            SELECT screener_slug, MAX(date) AS max_date
            FROM {SNAPSHOT_TABLE}
            GROUP BY screener_slug
        ) latest
          ON latest.screener_slug = s.screener_slug
         AND latest.max_date = s.date
        {slug_filter}
        ORDER BY s.screener_slug
        """,
        params=params,
    )


def summarize_latest_snapshots(screener_slug: str | None = None):
    snapshots = load_latest_snapshots(screener_slug)
    if snapshots.empty:
        return snapshots
    summarized = snapshots.copy()
    summarized["top_level_keys"] = summarized["raw_json"].map(
        lambda payload: list(payload.keys())[:10] if isinstance(payload, dict) else []
    )
    summarized["raw_json_size"] = summarized["raw_json"].map(
        lambda payload: len(json.dumps(payload)) if payload is not None else 0
    )
    return summarized.drop(columns=["raw_json"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch registered Dhan ScanX screener pages and store ng-state JSON")
    parser.add_argument(
        "urls",
        nargs="*",
        help="Optional screener URLs to fetch. If omitted, syncs active registered screeners.",
    )
    args = parser.parse_args()

    urls = args.urls or load_registered_urls()
    if not urls:
        print("No registered screeners found. Add one with `python -m data.dhanlive.screener_registry add <url>`.", flush=True)
        return
    for url in urls:
        print(sync_screener(url), flush=True)


if __name__ == "__main__":
    main()
