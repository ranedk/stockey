from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from psycopg2.extras import Json

from utils.db import db_session, sql_to_df
from utils.http import get_dynamic_headers, get_with_retries


SNAPSHOT_TABLE = "public.screenerin_screener_snapshots"
REGISTRY_TABLE = "public.screenerin_screeners"
SOURCE_NAME = "screener.in"


def ensure_tables() -> None:
    with db_session() as (_, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SNAPSHOT_TABLE} (
                screener_slug TEXT NOT NULL,
                screener_name TEXT NOT NULL,
                screener_url TEXT NOT NULL,
                screen_id BIGINT,
                source_name TEXT NOT NULL,
                row_count BIGINT NOT NULL DEFAULT 0,
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
                screen_id BIGINT,
                source_name TEXT NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_ts TIMESTAMPTZ NOT NULL,
                updated_ts TIMESTAMPTZ NOT NULL
            )
            """
        )


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
        try:
            return int(value)
        except ValueError:
            return value
    if re.fullmatch(r"-?\d*\.\d+", value) or re.fullmatch(r"-?\d+\.\d*", value):
        try:
            return float(value)
        except ValueError:
            return value
    return value


def extract_screener_slug(url: str) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    if not parts:
        raise ValueError(f"Could not determine screener slug from URL: {url}")
    return parts[-1]


def extract_screen_id(url: str) -> int | None:
    parts = [part for part in urlparse(url).path.split("/") if part]
    if len(parts) >= 2 and parts[-2].isdigit():
        return int(parts[-2])
    for part in parts:
        if part.isdigit():
            return int(part)
    return None


def derive_screener_name(url: str) -> str:
    return extract_screener_slug(url).replace("-", " ").upper()


def _metric_key(label: str) -> str:
    return (
        label.lower()
        .replace(".", "")
        .replace("%", "pct")
        .replace("/", "_")
        .replace(" ", "_")
    )


def get_screener_html(url: str) -> str:
    response = get_with_retries(url, headers=get_dynamic_headers())
    response.raise_for_status()
    return response.text


def parse_screener_html(html: str, *, url: str | None = None) -> dict[str, object]:
    soup = BeautifulSoup(html, "html.parser")

    title_el = soup.select_one("#screen-info h1")
    desc_el = soup.select_one("div[style*='max-width: 750px'] > p")
    author_el = soup.select_one("div[style*='max-width: 750px'] .sub a")
    page_info_el = soup.select_one("[data-page-info]")
    query_el = soup.select_one("#query-builder textarea, #query-builder pre, #query-builder")
    table = soup.select_one("div[data-page-results] table")
    if not table:
        raise ValueError("Could not find Screener.in results table")

    headers: list[str] = []
    companies: list[dict[str, object]] = []
    for tr in table.select("tr"):
        ths = tr.find_all("th")
        if ths:
            current_headers = [clean_text(th.get_text(" ", strip=True)) for th in ths]
            headers = [value for value in current_headers if value]
            continue

        company_id = tr.get("data-row-company-id")
        if not company_id:
            continue

        tds = tr.find_all("td")
        if len(tds) < 2:
            continue

        serial_no = to_number(tds[0].get_text(" ", strip=True).rstrip("."))
        name_link = tds[1].find("a")
        company_url = name_link.get("href") if name_link else None
        page_slug = None
        ticker = None
        if company_url:
            company_parts = [part for part in company_url.split("/") if part]
            if len(company_parts) >= 2 and company_parts[0] == "company":
                page_slug = company_parts[1]
                ticker = company_parts[1]

        metrics: dict[str, object] = {}
        for header, td in zip(headers[2:], tds[2:]):
            metrics[_metric_key(header)] = to_number(td.get_text(" ", strip=True))

        companies.append(
            {
                "s_no": serial_no,
                "company_id": to_number(company_id),
                "name": clean_text(name_link.get_text(" ", strip=True)) if name_link else clean_text(tds[1].get_text(" ", strip=True)),
                "url": company_url,
                "ticker": ticker,
                "page_slug": page_slug,
                "metrics": metrics,
            }
        )

    parsed = {
        "source_name": SOURCE_NAME,
        "screen_id": extract_screen_id(url) if url else None,
        "screener_slug": extract_screener_slug(url) if url else None,
        "screener_name": clean_text(title_el.get_text(" ", strip=True)) if title_el else (derive_screener_name(url) if url else None),
        "screener_description": clean_text(desc_el.get_text(" ", strip=True)) if desc_el else None,
        "screener_author": clean_text(author_el.get_text(" ", strip=True)) if author_el else None,
        "page_info_json": page_info_el.get("data-page-info") if page_info_el else None,
        "query_text": clean_text(query_el.get_text("\n", strip=True)) if query_el else None,
        "headers": headers,
        "companies": companies,
    }
    return parsed


def upsert_registered_screener(
    *,
    screener_url: str,
    screener_name: str | None = None,
    is_active: bool = True,
) -> dict[str, object]:
    ensure_tables()
    screener_slug = extract_screener_slug(screener_url)
    now = datetime.now(timezone.utc)
    effective_name = (screener_name or derive_screener_name(screener_url)).strip()
    screen_id = extract_screen_id(screener_url)
    with db_session() as (_, cur):
        cur.execute(
            f"""
            INSERT INTO {REGISTRY_TABLE} (
                screener_slug,
                screener_name,
                screener_url,
                screen_id,
                source_name,
                is_active,
                created_ts,
                updated_ts
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (screener_slug)
            DO UPDATE SET
                screener_name = EXCLUDED.screener_name,
                screener_url = EXCLUDED.screener_url,
                screen_id = EXCLUDED.screen_id,
                source_name = EXCLUDED.source_name,
                is_active = EXCLUDED.is_active,
                updated_ts = EXCLUDED.updated_ts
            """,
            (
                screener_slug,
                effective_name,
                screener_url,
                screen_id,
                SOURCE_NAME,
                is_active,
                now,
                now,
            ),
        )
    return {
        "screener_slug": screener_slug,
        "screener_name": effective_name,
        "screener_url": screener_url,
        "screen_id": screen_id,
        "source_name": SOURCE_NAME,
        "is_active": is_active,
    }

def list_registered_screeners(*, active_only: bool = False):
    ensure_tables()
    where_clause = "WHERE is_active = TRUE" if active_only else ""
    return sql_to_df(
        f"""
        SELECT screener_slug, screener_name, screener_url, screen_id, source_name, is_active, created_ts, updated_ts
        FROM {REGISTRY_TABLE}
        {where_clause}
        ORDER BY screener_name, screener_slug
        """
    )


def remove_registered_screener(identifier: str) -> dict[str, object]:
    ensure_tables()
    with db_session() as (_, cur):
        cur.execute(
            f"DELETE FROM {REGISTRY_TABLE} WHERE screener_slug = %s OR screener_url = %s",
            (identifier, identifier),
        )
        removed = cur.rowcount
    return {"identifier": identifier, "removed": removed}


def load_registered_urls() -> list[str]:
    df = list_registered_screeners(active_only=True)
    if df.empty:
        return []
    return df["screener_url"].dropna().astype(str).tolist()


def upsert_snapshot(
    *,
    screener_slug: str,
    screener_name: str,
    screener_url: str,
    snapshot_date: datetime,
    payload: dict[str, object],
) -> None:
    with db_session() as (_, cur):
        cur.execute(
            f"""
            INSERT INTO {SNAPSHOT_TABLE} (
                screener_slug,
                screener_name,
                screener_url,
                screen_id,
                source_name,
                row_count,
                date,
                raw_json,
                load_ts
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (date, screener_slug)
            DO UPDATE SET
                screener_name = EXCLUDED.screener_name,
                screener_url = EXCLUDED.screener_url,
                screen_id = EXCLUDED.screen_id,
                source_name = EXCLUDED.source_name,
                row_count = EXCLUDED.row_count,
                raw_json = EXCLUDED.raw_json,
                load_ts = EXCLUDED.load_ts
            """,
            (
                screener_slug,
                screener_name,
                screener_url,
                payload.get("screen_id"),
                SOURCE_NAME,
                len(payload.get("companies", [])),
                snapshot_date.date(),
                Json(payload),
                datetime.now(timezone.utc),
            ),
        )


def sync_screener(url: str, snapshot_date: datetime | None = None) -> dict[str, object]:
    ensure_tables()
    html = get_screener_html(url)
    payload = parse_screener_html(html, url=url)
    screener_slug = payload.get("screener_slug") or extract_screener_slug(url)
    screener_name = payload.get("screener_name") or derive_screener_name(url)
    effective_date = snapshot_date or datetime.now(timezone.utc)
    upsert_snapshot(
        screener_slug=str(screener_slug),
        screener_name=str(screener_name),
        screener_url=url,
        snapshot_date=effective_date,
        payload=payload,
    )
    upsert_registered_screener(
        screener_url=url,
        screener_name=str(screener_name),
        is_active=True,
    )
    return {
        "source_name": SOURCE_NAME,
        "screener_slug": screener_slug,
        "screener_name": screener_name,
        "screen_id": payload.get("screen_id"),
        "date": effective_date.date().isoformat(),
        "row_count": len(payload.get("companies", [])),
    }


def sync_registered_screeners(snapshot_date: datetime | None = None) -> list[dict[str, object]]:
    urls = load_registered_urls()
    if not urls:
        seed_default_screeners()
        urls = load_registered_urls()
    return [sync_screener(url, snapshot_date=snapshot_date) for url in urls]


def load_latest_snapshots(screener_slug: str | None = None):
    ensure_tables()
    params: dict[str, object] = {}
    where_sql = ""
    if screener_slug:
        where_sql = "WHERE s.screener_slug = %(screener_slug)s"
        params["screener_slug"] = screener_slug
    return sql_to_df(
        f"""
        SELECT
            s.screener_slug,
            s.screener_name,
            s.screener_url,
            s.screen_id,
            s.source_name,
            s.row_count,
            s.date,
            s.raw_json,
            s.load_ts
        FROM {SNAPSHOT_TABLE} s
        JOIN (
            SELECT screener_slug, MAX(date) AS max_date
            FROM {SNAPSHOT_TABLE}
            GROUP BY screener_slug
        ) latest
          ON latest.screener_slug = s.screener_slug
         AND latest.max_date = s.date
        {where_sql}
        ORDER BY s.screener_slug
        """,
        params=params or None,
    )


def summarize_latest_snapshots(screener_slug: str | None = None):
    snapshots = load_latest_snapshots(screener_slug)
    if snapshots.empty:
        return snapshots
    summary = snapshots.copy()
    summary["top_level_keys"] = summary["raw_json"].map(
        lambda payload: list(payload.keys())[:10] if isinstance(payload, dict) else []
    )
    return summary.drop(columns=["raw_json"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch registered Screener.in screeners and store parsed snapshots.")
    parser.add_argument("urls", nargs="*", help="Optional Screener.in URLs to fetch. Defaults to active registry rows.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    urls = args.urls or load_registered_urls()
    for url in urls:
        print(json.dumps(sync_screener(url), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
