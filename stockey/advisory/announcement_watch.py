from __future__ import annotations

import argparse
import json
from datetime import timedelta

import pandas as pd

from data.announcements.managed_pipeline import ManagedAnnouncementPipeline
from utils.db import db_session, sql_to_df, upsert_to_db
from utils.sync import parse_datetime_arg


WATCHLIST_TABLE = "advisory_watchlist"
EVENTS_TABLE = "advisory_watch_events"


def normalize_timestamp(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce").dt.normalize()


def load_watchlist(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
) -> pd.DataFrame:
    clauses = ["watch_status = 'active'"]
    params: list[object] = []
    if asof_date is not None:
        clauses.append("asof_date = %s")
        params.append(asof_date)
    else:
        clauses.append("asof_date = (SELECT MAX(asof_date) FROM advisory_watchlist)")
    if symbols:
        clauses.append("symbol = ANY(%s)")
        params.append([value.upper() for value in symbols])
    if setup_ids:
        clauses.append("setup_id = ANY(%s)")
        params.append([value.upper() for value in setup_ids])
    try:
        df = sql_to_df(
            f"""
            SELECT *
            FROM {WATCHLIST_TABLE}
            WHERE {' AND '.join(clauses)}
            ORDER BY setup_id, symbol
            """,
            params=tuple(params) if params else None,
        )
    except Exception:
        return pd.DataFrame()
    if df.empty:
        return df
    df["asof_date"] = normalize_timestamp(df["asof_date"])
    df["symbol"] = df["symbol"].astype("string").str.upper()
    return df


def load_documents_for_company(
    company_master_id: str,
    *,
    published_from: pd.Timestamp,
    published_to: pd.Timestamp,
) -> pd.DataFrame:
    df = sql_to_df(
        """
        SELECT
            unique_id,
            company_master_id,
            exchange,
            ticker,
            company_name,
            subject,
            filed_under_category,
            published_on,
            parse_status,
            concise_summary_text,
            categories_json
        FROM announcement_pipeline_documents
        WHERE company_master_id = %s
          AND published_on >= %s
          AND published_on <= %s
        ORDER BY published_on, unique_id
        """,
        params=(company_master_id, published_from, published_to),
    )
    if df.empty:
        return df
    df["published_on"] = pd.to_datetime(df["published_on"], utc=True, errors="coerce")
    return df


def build_event_rows(watch_row: pd.Series, docs: pd.DataFrame) -> pd.DataFrame:
    if docs.empty:
        return pd.DataFrame()
    rows = []
    for _, doc in docs.iterrows():
        rows.append(
            {
                "asof_date": watch_row["asof_date"],
                "setup_id": watch_row["setup_id"],
                "setup_name": watch_row["setup_name"],
                "symbol": watch_row["symbol"],
                "company_master_id": watch_row["company_master_id"],
                "unique_id": doc["unique_id"],
                "exchange": doc["exchange"],
                "subject": doc["subject"],
                "filed_under_category": doc["filed_under_category"],
                "published_on": doc["published_on"],
                "parse_status": doc["parse_status"],
                "concise_summary_text": doc["concise_summary_text"],
                "categories_json": doc["categories_json"],
                "watch_reasons_json": watch_row["watch_reasons_json"],
                "event_status": "triggered",
                "load_ts": pd.Timestamp.utcnow(),
            }
        )
    return pd.DataFrame(rows)


def persist_watch_outputs(watchlist_updates: pd.DataFrame, events: pd.DataFrame) -> None:
    if not watchlist_updates.empty:
        upsert_to_db(
            watchlist_updates,
            WATCHLIST_TABLE,
            unique_keys=["asof_date", "setup_id", "symbol"],
            timescaledb_column="asof_date",
        )
    if not events.empty:
        upsert_to_db(
            events,
            EVENTS_TABLE,
            unique_keys=["setup_id", "symbol", "unique_id"],
            timescaledb_column="published_on",
        )


def run_announcement_watch(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
    to_date: pd.Timestamp | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, object]]:
    watchlist = load_watchlist(asof_date=asof_date, symbols=symbols, setup_ids=setup_ids)
    if watchlist.empty:
        return pd.DataFrame(), pd.DataFrame(), {"watch_count": 0, "ingest_runs": []}

    pipeline = ManagedAnnouncementPipeline()
    effective_to = pd.Timestamp(to_date or pd.Timestamp.utcnow(), tz="UTC")
    watch_updates: list[dict[str, object]] = []
    event_frames: list[pd.DataFrame] = []
    ingest_runs: list[dict[str, object]] = []

    for _, row in watchlist.iterrows():
        last_checked = pd.to_datetime(row.get("last_checked_at"), utc=True, errors="coerce")
        if pd.isna(last_checked):
            published_from = row["asof_date"]
        else:
            published_from = last_checked - pd.Timedelta(days=1)
        published_to = effective_to

        summary = pipeline.ingest_date_range(
            ticker=str(row["symbol"]),
            from_date=published_from.date(),
            to_date=published_to.date(),
            exchanges=["NSE"],
        )
        ingest_runs.append(
            {
                "symbol": row["symbol"],
                "setup_id": row["setup_id"],
                "requested": summary.requested,
                "discovered": summary.discovered,
                "downloaded": summary.downloaded,
                "parsed": summary.parsed,
                "failed": summary.failed,
            }
        )

        docs = load_documents_for_company(
            str(row["company_master_id"]),
            published_from=published_from,
            published_to=published_to,
        )
        event_df = build_event_rows(row, docs)
        if not event_df.empty:
            event_frames.append(event_df)
            last_published = event_df["published_on"].max()
        else:
            last_published = pd.to_datetime(row.get("last_document_published_on"), utc=True, errors="coerce")

        watch_updates.append(
            {
                "asof_date": row["asof_date"],
                "setup_id": row["setup_id"],
                "setup_name": row["setup_name"],
                "regime_name": row["regime_name"],
                "symbol": row["symbol"],
                "company_master_id": row["company_master_id"],
                "screener_slug": row.get("screener_slug"),
                "rank": row.get("rank"),
                "watch_enabled": row.get("watch_enabled", True),
                "watch_reasons_json": row["watch_reasons_json"],
                "watch_status": row.get("watch_status", "active"),
                "watch_started_at": pd.to_datetime(row.get("watch_started_at"), utc=True, errors="coerce"),
                "last_checked_at": effective_to,
                "last_document_published_on": last_published,
                "load_ts": pd.Timestamp.utcnow(),
            }
        )

    watch_update_df = pd.DataFrame(watch_updates)
    events_df = pd.concat(event_frames, ignore_index=True) if event_frames else pd.DataFrame()
    meta = {
        "watch_count": int(len(watchlist)),
        "ingest_runs": ingest_runs,
    }
    return watch_update_df, events_df, meta


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run announcement watch for advisory watchlist rows.")
    parser.add_argument("--date", type=parse_datetime_arg, help="Watchlist asof date in YYYY-MM-DD")
    parser.add_argument("--symbols", nargs="*", help="Optional symbols")
    parser.add_argument("--setup", dest="setup_ids", nargs="*", help="Optional setup ids")
    parser.add_argument("--to-date", type=parse_datetime_arg, help="End date in YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def summarize(watchlist_updates: pd.DataFrame, events: pd.DataFrame, meta: dict[str, object]) -> dict[str, object]:
    return {
        "status": "ok",
        "watchlist_table": WATCHLIST_TABLE,
        "events_table": EVENTS_TABLE,
        "watch_count": meta.get("watch_count", 0),
        "event_count": int(len(events)),
        "ingest_runs": meta.get("ingest_runs", []),
        "event_sample": events.head(10).to_dict(orient="records") if not events.empty else [],
    }


def main() -> int:
    args = parse_args()
    asof_date = pd.Timestamp(args.date, tz="UTC") if args.date else None
    to_date = pd.Timestamp(args.to_date, tz="UTC") if args.to_date else None
    watchlist_updates, events, meta = run_announcement_watch(
        asof_date=asof_date,
        symbols=args.symbols,
        setup_ids=args.setup_ids,
        to_date=to_date,
    )
    if not args.dry_run:
        persist_watch_outputs(watchlist_updates, events)
    result = summarize(watchlist_updates, events, meta)
    result["dry_run"] = bool(args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
