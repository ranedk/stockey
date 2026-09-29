"""Economic Times RSS news -> fundamentals_news_item (operator, 2026-09-29).

Part of the event-driven re-evaluation design: a company's announcements re-score that
company; sector and macro NEWS re-score the companies for which that dimension is a story
or a flaw. This collector only gathers and stores; tagging an item to companies/sectors
and deciding what to re-score is the re-evaluation step's job.

Inside the fundamentals carve-out on purpose (stockey/CLAUDE.md): the pure-data
collectors in data/ carry no news. Hourly (all_fundamentals_news.sh). Each ET feed keeps
only its latest 50 items, so a missed hour can lose items on a busy day -- the job is not
gated on .pause_fundamentals. Each item is stored once, keyed by its link, with
first_seen_at (when we saw it -- the point-in-time clock) never updated.

Feeds were checked live 2026-09-29: industrial goods returns nothing and the telecom
address serves only 2020-21 'Software' items, so both are left out. sector_hint maps a feed's section to stockey's sector codes (Sharpely/NSE scheme)
as a starting hint only; macro feeds carry none.

    python -m fundamentals.collectors.et_news
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import pandas as pd
import requests

from utils.db import db_session, execute_db_operation, upsert_to_db
from utils.fallback_telemetry import record_local_fallback_event

SYNC_SOURCE_NAME = "fundamentals.collectors.et_news"
RESULTS_TABLE = "fundamentals_news_item"
BASE = "https://economictimes.indiatimes.com"
HEADERS = {"User-Agent": "Mozilla/5.0"}
STOCKEY_RUN_STATE: dict[str, object] = {}

# feed name -> (path, sector_hint or None for market/macro feeds)
FEEDS: dict[str, tuple[str, str | None]] = {
    "top_stories": ("rssfeedstopstories.cms", None),
    "markets": ("markets/rssfeeds/1977021501.cms", None),
    "stocks": ("markets/stocks/rssfeeds/2146842.cms", None),
    "commodities": ("markets/commodities/rssfeeds/1808152121.cms", "IN0103"),
    "economy": ("news/economy/rssfeeds/1373380680.cms", None),
    "policy": ("news/economy/policy/rssfeeds/1106944246.cms", None),
    "industry": ("industry/rssfeeds/13352306.cms", None),
    "auto": ("industry/auto/rssfeeds/13359412.cms", "IN0201"),
    "banking_finance": ("industry/banking/finance/rssfeeds/13358259.cms", "IN0501"),
    "consumer_products": ("industry/cons-products/rssfeeds/13358759.cms", "IN0401"),
    "energy": ("industry/energy/rssfeeds/13358350.cms", "IN0301"),
    "healthcare": ("industry/healthcare/biotech/rssfeeds/13358050.cms", "IN0601"),
    "services": ("industry/services/rssfeeds/13354120.cms", "IN0901"),
    "media": ("industry/media/entertainment/rssfeeds/13357212.cms", "IN0204"),
    "transportation": ("industry/transportation/rssfeeds/13353990.cms", "IN0901"),
    "tech": ("tech/rssfeeds/13357270.cms", "IN0801"),
    "renewables": ("industry/renewables/rssfeeds/81585238.cms", "IN1101"),
}

_DDL = f"""
    CREATE TABLE IF NOT EXISTS {RESULTS_TABLE} (
        link TEXT PRIMARY KEY,
        feed TEXT NOT NULL,
        sector_hint TEXT,
        title TEXT,
        description TEXT,
        published_at TIMESTAMPTZ,
        first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        load_ts TIMESTAMPTZ
    )
"""


def ensure_table() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute(_DDL)

    execute_db_operation(_op, operation_name=f"{RESULTS_TABLE}:ensure_table")


def parse_feed(xml_bytes: bytes, feed: str, sector_hint: str | None) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    items = []
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        if not link:
            continue
        published = None
        raw = (it.findtext("pubDate") or "").strip()
        if raw:
            try:
                published = parsedate_to_datetime(raw)
            except (TypeError, ValueError):
                published = None
        items.append({
            "link": link, "feed": feed, "sector_hint": sector_hint,
            "title": (it.findtext("title") or "").strip(),
            "description": (it.findtext("description") or "").strip(),
            "published_at": published,
        })
    return items


def collect() -> dict[str, object]:
    ensure_table()
    session = requests.Session()
    rows, failed = [], []
    for feed, (path, hint) in FEEDS.items():
        try:
            resp = session.get(f"{BASE}/{path}", headers=HEADERS, timeout=30)
            resp.raise_for_status()
            items = parse_feed(resp.content, feed, hint)
            if not items:
                raise ValueError("feed returned no items")
            rows.extend(items)
        except Exception as exc:  # noqa: BLE001 -- one feed failing must not stop the others
            failed.append(feed)
            record_local_fallback_event(
                module=SYNC_SOURCE_NAME, source="economictimes", fallback_type="et_feed_failed", severity="warn",
                reason=f"ET RSS feed {feed} failed this run", error=repr(exc), metadata={"feed": feed, "path": path},
            )
    written = 0
    if rows:
        # The same article often sits in several feeds; keep the first (most specific
        # sector hint wins, since sector feeds are listed after the general ones only
        # when they have a hint -- prefer a row with a hint).
        df = pd.DataFrame(rows).sort_values("sector_hint", na_position="last").drop_duplicates("link")
        df["load_ts"] = pd.Timestamp.now(tz="UTC")
        upsert_to_db(df, RESULTS_TABLE, unique_keys=["link"], on_conflict="nothing")
        written = len(df)
    return {"feeds": len(FEEDS), "failed_feeds": failed, "items_seen": written}


def main() -> int:
    global STOCKEY_RUN_STATE
    result = collect()
    STOCKEY_RUN_STATE = {"source": SYNC_SOURCE_NAME, "rows": result["items_seen"], "rows_written": result["items_seen"],
                         **result, "fallback_used": bool(result["failed_feeds"]), "state_advanced": result["items_seen"] > 0}
    print(json.dumps({"status": "ok", **STOCKEY_RUN_STATE}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
