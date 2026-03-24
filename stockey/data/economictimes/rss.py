from __future__ import annotations

import argparse
import html
import json
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree import ElementTree as ET

import pandas as pd
import requests

from utils.db import db_session, upsert_to_db


TABLE_NAME = "economictimes_rss_items"
REQUEST_TIMEOUT = 30

DEFAULT_FEEDS = {
    "markets": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "stocks": "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
    "expert_views": "https://economictimes.indiatimes.com/markets/expert-view/rssfeeds/50649960.cms",
    "investment_ideas": "https://economictimes.indiatimes.com/markets/investment-ideas/rssfeeds/81409979.cms",
}


def ensure_table() -> None:
    with db_session() as (_, cur):
        cur.execute(
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
            """
        )
        cur.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS source_name TEXT")
        cur.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS categories_json TEXT")


def parse_datetime(value: str | None) -> pd.Timestamp | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except Exception:
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
    args = parse_args()
    df = build_feed_rows(feed_names=args.feeds)
    if not args.dry_run:
        persist_feed_rows(df)
    print(
        json.dumps(
            {
                "status": "ok",
                "table": TABLE_NAME,
                "feed_names": sorted(df["feed_name"].dropna().unique().tolist()) if not df.empty else [],
                "row_count": int(len(df)),
                "sample": df.head(10).to_dict(orient="records") if not df.empty else [],
                "dry_run": bool(args.dry_run),
            },
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
