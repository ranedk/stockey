from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import timedelta

import pandas as pd

from data.announcements.managed_pipeline import ManagedAnnouncementPipeline
from utils.db import db_session, sql_to_df, upsert_to_db
from utils.sync import parse_datetime_arg


WATCHLIST_TABLE = "advisory_watchlist"
EVENTS_TABLE = "advisory_watch_events"
INITIAL_INGEST_LOOKBACK_DAYS = 3


def normalize_timestamp(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce").dt.normalize()


def _emit_progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def ensure_watch_outputs_tables() -> None:
    with db_session() as (_, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {WATCHLIST_TABLE} (
                asof_date TIMESTAMPTZ NOT NULL,
                setup_id TEXT NOT NULL,
                setup_name TEXT,
                regime_name TEXT,
                symbol TEXT NOT NULL,
                company_master_id TEXT,
                screener_slug TEXT,
                rank BIGINT,
                candidate_state TEXT,
                current_state TEXT,
                watch_reason_detail TEXT,
                entry_style TEXT,
                attractive_price_low DOUBLE PRECISION,
                attractive_price_high DOUBLE PRECISION,
                invalidation_price DOUBLE PRECISION,
                entry_note TEXT,
                near_miss_flag BOOLEAN,
                last_event_class TEXT,
                last_state_transition_hint TEXT,
                last_event_score_impact DOUBLE PRECISION,
                watch_enabled BOOLEAN,
                watch_reasons_json TEXT,
                watch_status TEXT,
                state_updated_at TIMESTAMPTZ,
                watch_started_at TIMESTAMPTZ,
                last_checked_at TIMESTAMPTZ,
                last_document_published_on TIMESTAMPTZ,
                load_ts TIMESTAMPTZ,
                UNIQUE (asof_date, setup_id, symbol)
            )
            """
        )
        watchlist_columns = {
            "setup_name": "TEXT",
            "regime_name": "TEXT",
            "company_master_id": "TEXT",
            "screener_slug": "TEXT",
            "rank": "BIGINT",
            "candidate_state": "TEXT",
            "current_state": "TEXT",
            "watch_reason_detail": "TEXT",
            "entry_style": "TEXT",
            "attractive_price_low": "DOUBLE PRECISION",
            "attractive_price_high": "DOUBLE PRECISION",
            "invalidation_price": "DOUBLE PRECISION",
            "entry_note": "TEXT",
            "near_miss_flag": "BOOLEAN",
            "last_event_class": "TEXT",
            "last_state_transition_hint": "TEXT",
            "last_event_score_impact": "DOUBLE PRECISION",
            "watch_enabled": "BOOLEAN",
            "watch_reasons_json": "TEXT",
            "watch_status": "TEXT",
            "state_updated_at": "TIMESTAMPTZ",
            "watch_started_at": "TIMESTAMPTZ",
            "last_checked_at": "TIMESTAMPTZ",
            "last_document_published_on": "TIMESTAMPTZ",
            "load_ts": "TIMESTAMPTZ",
        }
        for column, sql_type in watchlist_columns.items():
            cur.execute(f"ALTER TABLE {WATCHLIST_TABLE} ADD COLUMN IF NOT EXISTS {column} {sql_type}")
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
                exchange TEXT,
                subject TEXT,
                filed_under_category TEXT,
                parse_status TEXT,
                concise_summary_text TEXT,
                categories_json TEXT,
                watch_reasons_json TEXT,
                event_status TEXT,
                load_ts TIMESTAMPTZ,
                UNIQUE (published_on, setup_id, symbol, unique_id)
            )
            """
        )


def load_watchlist(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
) -> pd.DataFrame:
    clauses = ["COALESCE(watch_enabled, TRUE) = TRUE", "COALESCE(watch_status, 'active') IN ('active', 'review_manual')"]
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
    ensure_watch_outputs_tables()
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
            unique_keys=["published_on", "setup_id", "symbol", "unique_id"],
            timescaledb_column="published_on",
        )


def _prepare_watchlist_for_ingest(
    watchlist: pd.DataFrame,
    *,
    effective_to: pd.Timestamp,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    working = watchlist.copy()
    working["company_master_id"] = working["company_master_id"].astype("string")
    working["symbol"] = working["symbol"].astype("string").str.upper()
    working["published_from"] = pd.Series(pd.NaT, index=working.index, dtype="datetime64[ns, UTC]")
    working["published_from_capped"] = False
    default_floor = (effective_to - pd.Timedelta(days=INITIAL_INGEST_LOOKBACK_DAYS)).normalize()

    for index, row in working.iterrows():
        last_checked = pd.to_datetime(row.get("last_checked_at"), utc=True, errors="coerce")
        if pd.isna(last_checked):
            published_from = max(pd.to_datetime(row["asof_date"], utc=True, errors="coerce"), default_floor)
            if published_from > pd.to_datetime(row["asof_date"], utc=True, errors="coerce"):
                working.at[index, "published_from_capped"] = True
        else:
            published_from = last_checked - pd.Timedelta(days=1)
        working.at[index, "published_from"] = published_from

    unique_ingest_targets = (
        working.groupby(["company_master_id", "symbol"], dropna=False, sort=True)
        .agg(
            published_from=("published_from", "min"),
            watch_rows=("setup_id", "count"),
        )
        .reset_index()
    )
    return working, unique_ingest_targets


def run_announcement_ingest(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
    to_date: pd.Timestamp | None = None,
) -> dict[str, object]:
    watchlist = load_watchlist(asof_date=asof_date, symbols=symbols, setup_ids=setup_ids)
    if watchlist.empty:
        return {
            "watchlist": pd.DataFrame(),
            "effective_to": pd.to_datetime(to_date or pd.Timestamp.utcnow(), utc=True, errors="coerce"),
            "docs_by_company": {},
            "last_published_by_company": {},
            "meta": {"watch_count": 0, "unique_ingest_targets": 0, "ingest_runs": []},
        }

    pipeline = ManagedAnnouncementPipeline()
    effective_to = pd.to_datetime(to_date or pd.Timestamp.utcnow(), utc=True, errors="coerce")
    ingest_runs: list[dict[str, object]] = []
    watchlist, unique_ingest_targets = _prepare_watchlist_for_ingest(watchlist, effective_to=effective_to)
    total_targets = int(len(unique_ingest_targets))
    docs_by_company: dict[str, pd.DataFrame] = {}
    last_published_by_company: dict[str, pd.Timestamp | None] = {}

    for position, target in enumerate(unique_ingest_targets.itertuples(index=False), start=1):
        company_master_id = str(target.company_master_id or "")
        symbol = str(target.symbol or "").upper()
        published_from = pd.to_datetime(target.published_from, utc=True, errors="coerce")
        published_to = effective_to
        target_started = time.monotonic()
        _emit_progress(
            f"[advisory.announcement_watch] ingest {position}/{total_targets} symbol={symbol} from={published_from.date()} to={published_to.date()} watch_rows={int(target.watch_rows)}"
        )
        summary = pipeline.ingest_date_range(
            ticker=symbol,
            from_date=published_from.date(),
            to_date=published_to.date(),
            exchanges=["NSE"],
        )
        ingest_runs.append(
            {
                "symbol": symbol,
                "company_master_id": company_master_id,
                "requested": summary.requested,
                "discovered": summary.discovered,
                "downloaded": summary.downloaded,
                "ocred": summary.ocred,
                "categorized": summary.categorized,
                "parsed": summary.parsed,
                "skipped": summary.skipped,
                "failed": summary.failed,
                "elapsed_seconds": round(time.monotonic() - target_started, 4),
            }
        )
        _emit_progress(
            f"[advisory.announcement_watch] ingest done symbol={symbol} elapsed={time.monotonic() - target_started:.2f}s discovered={summary.discovered} parsed={summary.parsed} failed={summary.failed}"
        )

        docs = load_documents_for_company(
            company_master_id,
            published_from=published_from,
            published_to=published_to,
        )
        docs_by_company[company_master_id] = docs
        last_published_by_company[company_master_id] = (
            docs["published_on"].max() if not docs.empty else None
        )

    return {
        "watchlist": watchlist,
        "effective_to": effective_to,
        "docs_by_company": docs_by_company,
        "last_published_by_company": last_published_by_company,
        "meta": {
            "watch_count": int(len(watchlist)),
            "unique_ingest_targets": total_targets,
            "initial_lookback_days": INITIAL_INGEST_LOOKBACK_DAYS,
            "capped_watch_rows": int(pd.Series(watchlist["published_from_capped"]).fillna(False).astype(bool).sum()),
            "ingest_runs": ingest_runs,
        },
    }


def build_watch_updates_from_ingest(ingest_state: dict[str, object]) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, object]]:
    watchlist = ingest_state.get("watchlist")
    if not isinstance(watchlist, pd.DataFrame) or watchlist.empty:
        return pd.DataFrame(), pd.DataFrame(), {"watch_count": 0, "match_count": 0}

    effective_to = pd.to_datetime(ingest_state.get("effective_to"), utc=True, errors="coerce")
    docs_by_company = ingest_state.get("docs_by_company") if isinstance(ingest_state.get("docs_by_company"), dict) else {}
    last_published_by_company = ingest_state.get("last_published_by_company") if isinstance(ingest_state.get("last_published_by_company"), dict) else {}
    watch_updates: list[dict[str, object]] = []
    event_frames: list[pd.DataFrame] = []

    for _, row in watchlist.iterrows():
        company_master_id = str(row["company_master_id"] or "")
        published_from = pd.to_datetime(row.get("published_from"), utc=True, errors="coerce")
        all_docs = docs_by_company.get(company_master_id, pd.DataFrame())
        if not all_docs.empty:
            docs = all_docs[all_docs["published_on"] >= published_from].copy()
        else:
            docs = pd.DataFrame()
        event_df = build_event_rows(row, docs)
        if not event_df.empty:
            event_frames.append(event_df)
            last_published = event_df["published_on"].max()
        else:
            last_published = pd.to_datetime(row.get("last_document_published_on"), utc=True, errors="coerce")
            cached_last_published = last_published_by_company.get(company_master_id)
            if pd.isna(last_published) and cached_last_published is not None:
                last_published = pd.to_datetime(cached_last_published, utc=True, errors="coerce")

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
                "candidate_state": row.get("candidate_state"),
                "current_state": row.get("current_state"),
                "watch_reason_detail": row.get("watch_reason_detail"),
                "entry_style": row.get("entry_style"),
                "attractive_price_low": row.get("attractive_price_low"),
                "attractive_price_high": row.get("attractive_price_high"),
                "invalidation_price": row.get("invalidation_price"),
                "entry_note": row.get("entry_note"),
                "near_miss_flag": row.get("near_miss_flag"),
                "last_event_class": row.get("last_event_class"),
                "last_state_transition_hint": row.get("last_state_transition_hint"),
                "last_event_score_impact": row.get("last_event_score_impact"),
                "watch_enabled": row.get("watch_enabled", True),
                "watch_reasons_json": row["watch_reasons_json"],
                "watch_status": row.get("watch_status", "active"),
                "state_updated_at": pd.to_datetime(row.get("state_updated_at"), utc=True, errors="coerce"),
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
        "match_count": int(len(events_df)),
    }
    return watch_update_df, events_df, meta


def run_announcement_watch(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
    to_date: pd.Timestamp | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, object]]:
    ingest_state = run_announcement_ingest(
        asof_date=asof_date,
        symbols=symbols,
        setup_ids=setup_ids,
        to_date=to_date,
    )
    watch_update_df, events_df, match_meta = build_watch_updates_from_ingest(ingest_state)
    meta = dict(ingest_state.get("meta") or {})
    meta.update(match_meta)
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
