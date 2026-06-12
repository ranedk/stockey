from __future__ import annotations

import argparse
import html
import json
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree import ElementTree as ET

import pandas as pd
import requests

from advisory.fallback_telemetry import record_local_fallback_event
from utils.db import upsert_to_db
from utils.schema_migrations import apply_schema_migration


TABLE_NAME = "economictimes_rss_items"
ECONOMICTIMES_RSS_SCHEMA_MIGRATION_ID = "20260611_economictimes_rss_items_base"
ECONOMICTIMES_RSS_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        source_name TEXT,
        feed_name TEXT NOT NULL,
        feed_url TEXT NOT NULL,
        guid TEXT NOT NULL,
        title TEXT,
        link TEXT,
        description TEXT,
        categories_json TEXT,
        published_on TIMESTAMPTZ,
        raw_item_json TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (feed_name, guid)
    )
    """,
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS source_name TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS categories_json TEXT",
]
REQUEST_TIMEOUT = 30
SYNC_SOURCE_NAME = "data.economictimes.rss"
STOCKEY_RUN_STATE: dict[str, object] = {}

DEFAULT_FEEDS = {
    "markets": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "stocks": "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
    "expert_views": "https://economictimes.indiatimes.com/markets/expert-view/rssfeeds/50649960.cms",
    "investment_ideas": "https://economictimes.indiatimes.com/markets/investment-ideas/rssfeeds/81409979.cms",
}


def ensure_table() -> None:
    apply_schema_migration(
        migration_id=ECONOMICTIMES_RSS_SCHEMA_MIGRATION_ID,
        description="Create Economic Times RSS item table.",
        statements=ECONOMICTIMES_RSS_SCHEMA_STATEMENTS,
        metadata={"tables": [TABLE_NAME], "source": SYNC_SOURCE_NAME},
    )


def parse_datetime(value: str | None) -> pd.Timestamp | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except Exception as exc:
        record_local_fallback_event(
            module="data.economictimes.rss",
            source="rss_pubdate",
            fallback_type="economictimes_rss_pubdate_email_parse_failed",
            severity="warn",
            reason="Economic Times RSS pubDate was not RFC/email-date parseable; falling back to pandas timestamp parsing.",
            error=exc,
            metadata={"value_excerpt": str(value)[:240]},
        )
        dt = pd.to_datetime(value, utc=True, errors="coerce")
        if pd.isna(dt):
            return None
        return dt
    return pd.Timestamp(dt).tz_convert("UTC") if pd.Timestamp(dt).tzinfo else pd.Timestamp(dt, tz="UTC")


def normalize_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = html.unescape(str(value)).strip()
    return text or None


def fetch_feed(feed_name: str, feed_url: str) -> pd.DataFrame:
    response = requests.get(feed_url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    rows: list[dict[str, Any]] = []
    for item in root.findall("./channel/item"):
        title = normalize_text(item.findtext("title"))
        link = normalize_text(item.findtext("link"))
        description = normalize_text(item.findtext("description"))
        categories = [value for value in (normalize_text(node.text) for node in item.findall("category")) if value]
        guid = normalize_text(item.findtext("guid")) or link or title
        if not guid:
            continue
        published_on = parse_datetime(item.findtext("pubDate"))
        rows.append(
            {
                "source_name": "Economic Times RSS",
                "feed_name": feed_name,
                "feed_url": feed_url,
                "guid": guid,
                "title": title,
                "link": link,
                "description": description,
                "categories_json": json.dumps(categories, ensure_ascii=False, sort_keys=True),
                "published_on": published_on,
                "raw_item_json": json.dumps(
                    {
                        "source_name": "Economic Times RSS",
                        "title": title,
                        "link": link,
                        "description": description,
                        "categories": categories,
                        "guid": guid,
                        "pubDate": item.findtext("pubDate"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "load_ts": pd.Timestamp.utcnow(),
            }
        )
    return pd.DataFrame(rows)


def build_feed_rows(feed_names: list[str] | None = None) -> pd.DataFrame:
    selected = DEFAULT_FEEDS if not feed_names else {name: DEFAULT_FEEDS[name] for name in feed_names}
    frames = [fetch_feed(feed_name, feed_url) for feed_name, feed_url in selected.items()]
    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).drop_duplicates(subset=["feed_name", "guid"], keep="last")


def build_feed_rows_with_state(feed_names: list[str] | None = None) -> tuple[pd.DataFrame, dict[str, object]]:
    selected = DEFAULT_FEEDS if not feed_names else {name: DEFAULT_FEEDS[name] for name in feed_names}
    frames: list[pd.DataFrame] = []
    failed_feeds: list[dict[str, str]] = []
    empty_feed_count = 0

    for feed_name, feed_url in selected.items():
        try:
            frame = fetch_feed(feed_name, feed_url)
        except requests.RequestException as exc:
            failed_feeds.append(
                {
                    "feed_name": feed_name,
                    "error": f"{type(exc).__name__}: {exc}",
                    "classification": "source_unavailable",
                }
            )
            record_local_fallback_event(
                module=SYNC_SOURCE_NAME,
                source=f"{SYNC_SOURCE_NAME}:{feed_name}",
                fallback_type="economictimes_rss_feed_source_unavailable",
                severity="warn",
                reason="Economic Times RSS feed fetch failed; news/event context may be incomplete for this cycle.",
                error=exc,
                metadata={
                    "feed_name": feed_name,
                    "feed_url": feed_url,
                    "classification": "source_unavailable",
                },
            )
            print(f"Economic Times RSS feed failed feed={feed_name} error={exc}", flush=True)
            continue
        except Exception as exc:
            failed_feeds.append(
                {
                    "feed_name": feed_name,
                    "error": f"{type(exc).__name__}: {exc}",
                    "classification": "parse_failed",
                }
            )
            record_local_fallback_event(
                module=SYNC_SOURCE_NAME,
                source=f"{SYNC_SOURCE_NAME}:{feed_name}",
                fallback_type="economictimes_rss_feed_parse_failed",
                severity="error",
                reason="Economic Times RSS feed parse failed; news/event context may be incomplete for this cycle.",
                error=exc,
                metadata={
                    "feed_name": feed_name,
                    "feed_url": feed_url,
                    "classification": "parse_failed",
                },
            )
            print(f"Economic Times RSS feed failed feed={feed_name} error={exc}", flush=True)
            continue

        if frame.empty:
            empty_feed_count += 1
            continue
        frames.append(frame)

    if frames:
        df = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["feed_name", "guid"], keep="last")
    else:
        df = pd.DataFrame()

    source_unavailable_count = sum(1 for item in failed_feeds if item["classification"] == "source_unavailable")
    state: dict[str, object] = {
        "source": SYNC_SOURCE_NAME,
        "rows": int(len(df)),
        "rows_read": int(len(df)),
        "rows_written": 0,
        "feed_count": int(len(selected)),
        "succeeded_feed_count": int(len(frames)),
        "empty_feed_count": int(empty_feed_count),
        "failed_feed_count": int(len(failed_feeds)),
        "attempt_count": int(len(selected)),
        "download_attempts": int(len(selected)),
        "failed_attempt_count": int(len(failed_feeds)),
        "source_unavailable_count": int(source_unavailable_count),
        "parse_failed_count": int(len(failed_feeds) - source_unavailable_count),
        "no_data_count": int(empty_feed_count),
        "fallback_used": False,
        "state_advanced": False,
        "feed_names": sorted(df["feed_name"].dropna().unique().tolist()) if not df.empty else [],
    }
    if failed_feeds:
        state["failed_feeds"] = failed_feeds[:10]
    if not df.empty and "published_on" in df.columns:
        published = pd.to_datetime(df["published_on"], utc=True, errors="coerce").dropna()
        if not published.empty:
            state["from_datetime"] = published.min().isoformat()
            state["to_datetime"] = published.max().isoformat()
    return df, state


def persist_feed_rows(df: pd.DataFrame) -> None:
    ensure_table()
    if df.empty:
        return
    upsert_to_db(
        df,
        TABLE_NAME,
        unique_keys=["feed_name", "guid"],
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch and store Economic Times RSS items.")
    parser.add_argument("--feeds", nargs="*", choices=sorted(DEFAULT_FEEDS), help="Subset of configured feed names")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    global STOCKEY_RUN_STATE
    args = parse_args()
    df, state = build_feed_rows_with_state(feed_names=args.feeds)
    if not args.dry_run:
        persist_feed_rows(df)
        state["rows_written"] = int(len(df))
        state["state_advanced"] = bool(len(df) > 0)
    state["dry_run"] = bool(args.dry_run)
    STOCKEY_RUN_STATE = state
    status = "partial" if int(state.get("failed_feed_count") or 0) else "ok"
    print(
        json.dumps(
            {
                "status": status,
                "table": TABLE_NAME,
                "feed_names": sorted(df["feed_name"].dropna().unique().tolist()) if not df.empty else [],
                "row_count": int(len(df)),
                "sample": df.head(10).to_dict(orient="records") if not df.empty else [],
                "dry_run": bool(args.dry_run),
                **state,
            },
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
