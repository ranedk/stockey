from __future__ import annotations

import argparse
import json
import re
from typing import Any

import pandas as pd

from advisory.announcement_watch import (
    DEFAULT_MARKET_CONTEXT_WATCH_LIMIT,
    is_material_context_event,
    load_market_context_watchlist,
    load_watchlist,
    merge_watch_targets,
    normalize_timestamp,
)
from data.economictimes.rss import build_feed_rows, persist_feed_rows
from utils.db import db_session, sql_to_df, upsert_to_db
from utils.sync import parse_datetime_arg


EVENTS_TABLE = "advisory_news_events"
NEWS_TABLE = "economictimes_rss_items"
DEFAULT_LOOKBACK_DAYS = 7

_SPACE_RE = re.compile(r"[^a-z0-9]+")
_COMMON_SUFFIXES = (
    " limited",
    " ltd",
    " bank",
    " industries",
    " industry",
    " corporation",
    " corp",
    " company",
    " co",
)


def normalize_text(value: object) -> str:
    text = str(value or "").strip().lower()
    text = _SPACE_RE.sub(" ", text)
    return " ".join(part for part in text.split() if part)


def strip_company_suffixes(value: str) -> str:
    out = value
    changed = True
    while changed and out:
        changed = False
        for suffix in _COMMON_SUFFIXES:
            if out.endswith(suffix):
                out = out[: -len(suffix)].strip()
                changed = True
    return out


def ensure_output_table() -> None:
    with db_session() as (_, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {EVENTS_TABLE} (
                published_on TIMESTAMPTZ NOT NULL,
                asof_date TIMESTAMPTZ,
                setup_id TEXT NOT NULL,
                setup_name TEXT,
                symbol TEXT NOT NULL,
                company_master_id TEXT,
                unique_id TEXT NOT NULL,
                event_source TEXT,
                source_name TEXT,
                feed_name TEXT,
                source_url TEXT,
                subject TEXT,
                concise_summary_text TEXT,
                categories_json TEXT,
                watch_reasons_json TEXT,
                match_reasons_json TEXT,
                match_score DOUBLE PRECISION,
                monitor_source TEXT,
                event_status TEXT,
                load_ts TIMESTAMPTZ,
                UNIQUE (published_on, setup_id, symbol, unique_id)
            )
            """
        )
        cur.execute(f"ALTER TABLE {EVENTS_TABLE} ADD COLUMN IF NOT EXISTS event_source TEXT")
        cur.execute(f"ALTER TABLE {EVENTS_TABLE} ADD COLUMN IF NOT EXISTS source_name TEXT")
        cur.execute(f"ALTER TABLE {EVENTS_TABLE} ADD COLUMN IF NOT EXISTS feed_name TEXT")
        cur.execute(f"ALTER TABLE {EVENTS_TABLE} ADD COLUMN IF NOT EXISTS source_url TEXT")
        cur.execute(f"ALTER TABLE {EVENTS_TABLE} ADD COLUMN IF NOT EXISTS match_reasons_json TEXT")
        cur.execute(f"ALTER TABLE {EVENTS_TABLE} ADD COLUMN IF NOT EXISTS match_score DOUBLE PRECISION")
        cur.execute(f"ALTER TABLE {EVENTS_TABLE} ADD COLUMN IF NOT EXISTS monitor_source TEXT")


def load_watch_company_meta(company_master_ids: list[str]) -> pd.DataFrame:
    if not company_master_ids:
        return pd.DataFrame(columns=["company_master_id", "nse_ticker", "bse_ticker", "company_name"])
    df = sql_to_df(
        """
        SELECT company_master_id, nse_ticker, bse_ticker, company_name
        FROM company_master
        WHERE company_master_id = ANY(%s)
        """,
        params=(company_master_ids,),
    )
    if df.empty:
        return pd.DataFrame(columns=["company_master_id", "nse_ticker", "bse_ticker", "company_name"])
    for col in ("company_master_id", "nse_ticker", "bse_ticker", "company_name"):
        if col in df.columns:
            df[col] = df[col].astype("string")
    return df


def load_recent_news(
    *,
    published_from: pd.Timestamp,
    published_to: pd.Timestamp | None = None,
    feed_names: list[str] | None = None,
) -> pd.DataFrame:
    clauses = ["published_on >= %s"]
    params: list[object] = [published_from]
    if published_to is not None:
        clauses.append("published_on <= %s")
        params.append(published_to)
    if feed_names:
        clauses.append("feed_name = ANY(%s)")
        params.append(feed_names)
    try:
        df = sql_to_df(
            f"""
            SELECT source_name, feed_name, feed_url, guid, title, link, description, categories_json, published_on
            FROM {NEWS_TABLE}
            WHERE {' AND '.join(clauses)}
            ORDER BY published_on, feed_name, guid
            """,
            params=tuple(params),
        )
    except Exception:
        return pd.DataFrame()
    if df.empty:
        return df
    df["published_on"] = pd.to_datetime(df["published_on"], utc=True, errors="coerce")
    return df


def build_alias_bundle(watch_row: pd.Series, meta_row: pd.Series | None) -> dict[str, object]:
    symbol = str(watch_row["symbol"]).upper().strip()
    company_name = str(meta_row.get("company_name") if meta_row is not None else "" or "").strip()
    normalized_company_name = normalize_text(company_name)
    stripped_company_name = strip_company_suffixes(normalized_company_name)
    token_pattern = re.compile(rf"(?<![A-Z0-9]){re.escape(symbol)}(?![A-Z0-9])", flags=re.IGNORECASE)
    return {
        "symbol": symbol,
        "token_pattern": token_pattern,
        "normalized_company_name": normalized_company_name,
        "stripped_company_name": stripped_company_name,
    }


def score_match(news_row: pd.Series, alias_bundle: dict[str, object]) -> tuple[float, list[str]]:
    title = str(news_row.get("title") or "")
    description = str(news_row.get("description") or "")
    title_norm = normalize_text(title)
    desc_norm = normalize_text(description)
    combined_norm = f"{title_norm} {desc_norm}".strip()
    reasons: list[str] = []
    score = 0.0

    token_pattern = alias_bundle["token_pattern"]
    if isinstance(token_pattern, re.Pattern) and token_pattern.search(title):
        score += 4.0
        reasons.append("symbol_in_title")
    elif isinstance(token_pattern, re.Pattern) and token_pattern.search(description):
        score += 2.0
        reasons.append("symbol_in_description")

    normalized_company_name = str(alias_bundle.get("normalized_company_name") or "")
    stripped_company_name = str(alias_bundle.get("stripped_company_name") or "")
    for name, title_weight, desc_weight, tag in (
        (normalized_company_name, 4.0, 2.0, "company_name"),
        (stripped_company_name, 3.0, 1.5, "company_name_stripped"),
    ):
        if len(name) < 6:
            continue
        if name and name in title_norm:
            score += title_weight
            reasons.append(f"{tag}_in_title")
            break
        if name and name in desc_norm:
            score += desc_weight
            reasons.append(f"{tag}_in_description")
            break

    symbol = str(alias_bundle.get("symbol") or "")
    if score <= 0.0 and symbol and symbol.lower() in combined_norm:
        score += 1.0
        reasons.append("symbol_normalized_fallback")

    return score, list(dict.fromkeys(reasons))


def build_news_events(
    *,
    watchlist: pd.DataFrame,
    news_items: pd.DataFrame,
) -> pd.DataFrame:
    if watchlist.empty or news_items.empty:
        return pd.DataFrame()

    meta = load_watch_company_meta(
        watchlist["company_master_id"].dropna().astype(str).drop_duplicates().tolist()
    )
    meta_map = {
        str(row["company_master_id"]): row
        for _, row in meta.iterrows()
        if pd.notna(row.get("company_master_id"))
    }

    rows: list[dict[str, object]] = []
    for _, watch_row in watchlist.iterrows():
        alias_bundle = build_alias_bundle(watch_row, meta_map.get(str(watch_row.get("company_master_id"))))
        lower_bound = pd.to_datetime(watch_row["asof_date"], utc=True, errors="coerce")
        symbol_news = news_items[news_items["published_on"] >= lower_bound]
        for _, news_row in symbol_news.iterrows():
            score, reasons = score_match(news_row, alias_bundle)
            if score < 3.0 or not reasons:
                continue
            rows.append(
                {
                    "published_on": news_row["published_on"],
                    "asof_date": watch_row["asof_date"],
                    "setup_id": watch_row["setup_id"],
                    "setup_name": watch_row.get("setup_name"),
                    "symbol": watch_row["symbol"],
                    "company_master_id": watch_row.get("company_master_id"),
                    "unique_id": news_row["guid"],
                    "event_source": "economic_times_rss",
                    "source_name": news_row.get("source_name") or "Economic Times RSS",
                    "feed_name": news_row.get("feed_name"),
                    "source_url": news_row.get("link"),
                    "subject": news_row.get("title"),
                    "concise_summary_text": news_row.get("description"),
                    "categories_json": news_row.get("categories_json"),
                    "watch_reasons_json": watch_row.get("watch_reasons_json"),
                    "match_reasons_json": json.dumps(reasons, ensure_ascii=False, sort_keys=True),
                    "match_score": float(score),
                    "monitor_source": watch_row.get("monitor_source") or "watchlist",
                    "event_status": "triggered",
                    "load_ts": pd.Timestamp.utcnow(),
                }
            )

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    market_mask = df["monitor_source"].astype("string").str.lower().eq("market_context")
    if market_mask.any():
        material_mask = df.apply(is_material_context_event, axis=1)
        df.loc[market_mask & ~material_mask, "event_status"] = "context_observed"
    df["asof_date"] = normalize_timestamp(df["asof_date"])
    df["published_on"] = pd.to_datetime(df["published_on"], utc=True, errors="coerce")
    return df.drop_duplicates(subset=["published_on", "setup_id", "symbol", "unique_id"], keep="last")


def persist_news_events(events: pd.DataFrame) -> None:
    ensure_output_table()
    if events.empty:
        return
    upsert_to_db(
        events,
        EVENTS_TABLE,
        unique_keys=["published_on", "setup_id", "symbol", "unique_id"],
        timescaledb_column="published_on",
    )


def run_news_watch(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    to_date: pd.Timestamp | None = None,
    published_from: pd.Timestamp | None = None,
    feed_names: list[str] | None = None,
    refresh_feeds: bool = False,
    include_market_context: bool = False,
    market_context_limit: int = DEFAULT_MARKET_CONTEXT_WATCH_LIMIT,
    market_context_last_checked_at: pd.Timestamp | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    if refresh_feeds:
        persist_feed_rows(build_feed_rows(feed_names=feed_names))

    watchlist = load_watchlist(asof_date=asof_date, symbols=symbols, setup_ids=setup_ids)
    market_context_watchlist = (
        load_market_context_watchlist(
            asof_date=asof_date,
            symbols=symbols,
            limit=market_context_limit,
            last_checked_at=market_context_last_checked_at,
        )
        if include_market_context
        else pd.DataFrame()
    )
    watchlist = merge_watch_targets(watchlist, market_context_watchlist)
    if watchlist.empty:
        return pd.DataFrame(), {"watch_count": 0, "news_item_count": 0, "matched_event_count": 0}

    effective_to = pd.to_datetime(to_date or pd.Timestamp.utcnow(), utc=True, errors="coerce")
    if not pd.isna(effective_to) and effective_to == effective_to.normalize():
        effective_to = effective_to + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
    lower_bound = pd.to_datetime(published_from, utc=True, errors="coerce")
    if pd.isna(lower_bound):
        lower_bound = watchlist["asof_date"].min()
        lower_bound = max(lower_bound, effective_to - pd.Timedelta(days=int(lookback_days)))
    news_items = load_recent_news(published_from=lower_bound, published_to=effective_to, feed_names=feed_names)
    events = build_news_events(watchlist=watchlist, news_items=news_items)
    meta = {
        "watch_count": int(len(watchlist)),
        "news_item_count": int(len(news_items)),
        "matched_event_count": int(len(events)),
        "triggered_event_count": int(events["event_status"].astype(str).eq("triggered").sum()) if not events.empty and "event_status" in events.columns else 0,
        "context_observed_count": int(events["event_status"].astype(str).eq("context_observed").sum()) if not events.empty and "event_status" in events.columns else 0,
        "lookback_days": int(lookback_days),
        "market_context_enabled": bool(include_market_context),
        "market_context_limit": int(market_context_limit),
        "market_context_watch_count": int(len(market_context_watchlist)),
        "feed_names": sorted(news_items["feed_name"].dropna().astype(str).unique().tolist()) if not news_items.empty else [],
    }
    return events, meta


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Match Economic Times RSS items onto the advisory watchlist.")
    parser.add_argument("--date", type=parse_datetime_arg, help="Watchlist asof date in YYYY-MM-DD")
    parser.add_argument("--symbols", nargs="*", help="Optional symbols")
    parser.add_argument("--setup", dest="setup_ids", nargs="*", help="Optional setup ids")
    parser.add_argument("--feeds", nargs="*", choices=["markets", "stocks", "expert_views", "investment_ideas"])
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--refresh-feeds", action="store_true", help="Fetch ET RSS feeds before matching")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    asof_date = pd.Timestamp(args.date, tz="UTC") if args.date else None
    events, meta = run_news_watch(
        asof_date=asof_date,
        symbols=args.symbols,
        setup_ids=args.setup_ids,
        lookback_days=int(args.lookback_days),
        to_date=asof_date,
        feed_names=args.feeds,
        refresh_feeds=bool(args.refresh_feeds),
    )
    if not args.dry_run:
        persist_news_events(events)
    print(
        json.dumps(
            {
                "status": "ok",
                "table": EVENTS_TABLE,
                **meta,
                "sample": events.head(10).to_dict(orient="records") if not events.empty else [],
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
