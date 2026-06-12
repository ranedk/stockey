from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from psycopg2.extras import Json

from advisory.fallback_telemetry import record_local_fallback_event
from data.screenerin.auth import ensure_authenticated_requests_session
from data.screenerin.failure_log import record_screener_failure
from utils.db import db_session, execute_db_operation, sql_to_df
from utils.schema_migrations import apply_schema_migration


SNAPSHOT_TABLE = "public.screenerin_screener_snapshots"
REGISTRY_TABLE = "public.screenerin_screeners"
SOURCE_NAME = "screener.in"
SCREENER_PARSER_SCHEMA_MIGRATION_ID = "20260611_screenerin_registered_screeners_base"
SCREENER_PARSER_SCHEMA_STATEMENTS = [
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
    """,
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
    """,
]
STOCKEY_RUN_STATE: dict[str, object] = {}


def ensure_tables() -> None:
    apply_schema_migration(
        migration_id=SCREENER_PARSER_SCHEMA_MIGRATION_ID,
        description="Create Screener.in registered screener registry and snapshot tables.",
        statements=SCREENER_PARSER_SCHEMA_STATEMENTS,
        metadata={"tables": [SNAPSHOT_TABLE, REGISTRY_TABLE], "source": SOURCE_NAME},
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
        except ValueError as exc:
            record_local_fallback_event(
                module="data.screenerin.screener_parser",
                source="screener_numeric_parse",
                fallback_type="screener_integer_parse_failed",
                severity="warn",
                reason="Screener.in parser could not parse an integer-looking value and kept the original text.",
                error=exc,
                metadata={"value_excerpt": value[:240]},
            )
            return value
    if re.fullmatch(r"-?\d*\.\d+", value) or re.fullmatch(r"-?\d+\.\d*", value):
        try:
            return float(value)
        except ValueError as exc:
            record_local_fallback_event(
                module="data.screenerin.screener_parser",
                source="screener_numeric_parse",
                fallback_type="screener_float_parse_failed",
                severity="warn",
                reason="Screener.in parser could not parse a float-looking value and kept the original text.",
                error=exc,
                metadata={"value_excerpt": value[:240]},
            )
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
    session = ensure_authenticated_requests_session()
    response = session.get(url, timeout=60, allow_redirects=True)
    response.raise_for_status()
    if "login" in str(response.url).lower():
        raise RuntimeError("Screener.in redirected to login while fetching registered screener")
    return response.text


def parse_screener_html(html: str, *, url: str | None = None) -> dict[str, object]:
    soup = BeautifulSoup(html, "html.parser")

    page_title = clean_text(soup.title.get_text(" ", strip=True)) if soup.title else None
    title_el = soup.select_one("#screen-info h1")
    desc_el = soup.select_one("div[style*='max-width: 750px'] > p")
    author_el = soup.select_one("div[style*='max-width: 750px'] .sub a")
    page_info_el = soup.select_one("[data-page-info]")
    query_el = soup.select_one("#query-builder textarea, #query-builder pre, #query-builder")
    table = soup.select_one("div[data-page-results] table")
    if not table:
        body_text = clean_text(soup.get_text(" ", strip=True)) or ""
        context = {
            "url": url,
            "page_title": page_title,
            "has_login_form": bool(soup.select_one("form[action*='login'], input[name='username'], input[name='password']")),
            "has_query_builder": bool(query_el),
            "has_page_results_container": bool(soup.select_one("div[data-page-results]")),
            "body_excerpt": body_text[:240],
        }
        details = " ".join(f"{key}={value!r}" for key, value in context.items() if value not in (None, "", False))
        raise ValueError(f"Could not find Screener.in results table; {details}")

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

    def _upsert_registered_screener() -> None:
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

    execute_db_operation(
        _upsert_registered_screener,
        operation_name=f"screener_parser:upsert_registered_screener:{screener_slug}",
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
    removed = 0

    def _remove_registered_screener() -> None:
        nonlocal removed
        with db_session() as (_, cur):
            cur.execute(
                f"DELETE FROM {REGISTRY_TABLE} WHERE screener_slug = %s OR screener_url = %s",
                (identifier, identifier),
            )
            removed = cur.rowcount

    execute_db_operation(
        _remove_registered_screener,
        operation_name="screener_parser:remove_registered_screener",
    )
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
    def _upsert_snapshot() -> None:
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

    execute_db_operation(
        _upsert_snapshot,
        operation_name=f"screener_parser:upsert_snapshot:{screener_slug}",
    )


def sync_screener(url: str, snapshot_date: datetime | None = None) -> dict[str, object]:
    ensure_tables()
    html: str | None = None
    try:
        html = get_screener_html(url)
    except Exception as exc:
        record_screener_failure(
            failure_stage="registered_fetch",
            error=exc,
            screener_url=url,
            html=html,
        )
        raise
    try:
        payload = parse_screener_html(html, url=url)
    except Exception as exc:
        record_screener_failure(
            failure_stage="registered_parse",
            error=exc,
            screener_url=url,
            html=html,
        )
        raise
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
        # No implicit seed helper is defined in this module. Keep the sync
        # explicit so the central runner can surface an actionable no-data state.
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


def build_run_state(*, results: list[dict[str, object]], urls: list[str], snapshot_date: datetime | None = None) -> dict[str, object]:
    row_counts = [int(row.get("row_count") or 0) for row in results]
    no_data_screeners = [
        str(row.get("screener_slug") or row.get("screener_name") or "")
        for row in results
        if int(row.get("row_count") or 0) <= 0
    ]
    dates = [str(row.get("date")) for row in results if row.get("date")]
    effective_date = snapshot_date.date().isoformat() if snapshot_date else (dates[0] if dates else None)
    return {
        "source": "screener.in",
        "rows": int(sum(row_counts)),
        "rows_read": int(sum(row_counts)),
        "rows_written": int(sum(row_counts)),
        "screener_count": int(len(urls)),
        "screeners_synced": int(len(results)),
        "empty_screener_count": int(len(no_data_screeners)),
        "empty_screeners": [value for value in no_data_screeners if value][:20],
        "from_date": effective_date,
        "to_date": effective_date,
        "fallback_used": False,
        "state_advanced": bool(results),
    }


def main() -> int:
    global STOCKEY_RUN_STATE
    args = parse_args()
    urls = args.urls or load_registered_urls()
    results: list[dict[str, object]] = []
    for url in urls:
        result = sync_screener(url)
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    STOCKEY_RUN_STATE = build_run_state(results=results, urls=urls)
    if not results:
        print(json.dumps({"status": "no_data", "reason": "no_registered_screener_urls"}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
