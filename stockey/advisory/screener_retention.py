"""Watch retention: enter fast, exit slow -- with every exit recorded.

Event-day screener sources (the market-action scan, and any source that emits only on its
trigger day) admit a symbol for exactly one constituents date; the rule engine reads a
single snapshot date, so the name silently vanishes from the entire funnel the next day.
This layer re-emits recently-admitted constituents for a per-source retention window and
records an exit row when the window lapses without re-admission:

- Expiry is anchored on the ADMISSION date (carried forward in raw_item_json), not on
  last-seen -- a retained row can never extend its own life.
- Windows are per-slug (env): the scan gets a longer window (a breakout thesis deserves
  ~2 trading weeks of watching); everything else gets a short grace so a quality screen
  dropping a name still exits it, just visibly and a few days later.
- Exits land in advisory_watch_exits and are enrolled in the regret ledger
  (gate = watch_exit:<slug>), so the forward benchmark-excess of names we stopped watching
  measures whether the windows are too short.
"""
from __future__ import annotations

import json
import os
from typing import Any

import pandas as pd

from advisory.fallback_telemetry import record_local_fallback_event
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration

CONSTITUENTS_TABLE = "advisory_screener_constituents"
WATCH_EXITS_TABLE = "advisory_watch_exits"
WATCH_EXITS_MIGRATION_ID = "20260708_advisory_watch_exits_base"
WATCH_EXITS_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {WATCH_EXITS_TABLE} (
        asof_date TIMESTAMPTZ NOT NULL,
        symbol TEXT NOT NULL,
        screener_slug TEXT NOT NULL,
        last_seen_date TIMESTAMPTZ,
        admitted_date TIMESTAMPTZ,
        exit_reason TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (asof_date, symbol, screener_slug)
    )
    """,
]

RETENTION_DAYS_DEFAULT = int(os.getenv("SCREENER_RETENTION_DAYS_DEFAULT", "5"))
RETENTION_DAYS_SCAN = int(os.getenv("SCREENER_RETENTION_DAYS_SCAN", "10"))
SCAN_SLUG = "market-action-scan-v1"


def retention_days_for_slug(slug: str) -> int:
    if str(slug or "").strip() == SCAN_SLUG:
        return max(0, RETENTION_DAYS_SCAN)
    return max(0, RETENTION_DAYS_DEFAULT)


def ensure_exits_table() -> None:
    apply_schema_migration(
        migration_id=WATCH_EXITS_MIGRATION_ID,
        description="Record every screener-membership exit (retention lapse) so unwatch is never silent.",
        statements=WATCH_EXITS_SCHEMA_STATEMENTS,
        metadata={"module": "advisory.screener_retention", "tables": [WATCH_EXITS_TABLE]},
    )


def _admitted_date(raw_item_json: Any, row_date: pd.Timestamp) -> pd.Timestamp:
    """Original admission date: carried forward on retained rows, else the row's own date."""
    try:
        parsed = json.loads(raw_item_json) if raw_item_json else {}
        if isinstance(parsed, dict) and parsed.get("admitted_date"):
            value = pd.to_datetime(parsed["admitted_date"], utc=True, errors="coerce")
            if not pd.isna(value):
                return value.normalize()
    except Exception:
        pass
    return pd.Timestamp(row_date).normalize()


def apply_retention(frame: pd.DataFrame, *, snapshot_date: Any | None = None) -> pd.DataFrame:
    """Union retained rows into today's constituents frame and record lapsed exits.

    For every retention-eligible slug: prior members (within the lookback) that are absent
    from today's frame are re-emitted re-dated to today while `today - admitted_date` stays
    inside the slug's window; members whose window lapsed get an exit row instead.
    Best-effort: any failure returns the frame unchanged with telemetry.
    """
    try:
        return _apply_retention(frame, snapshot_date=snapshot_date)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.screener_retention",
            source=CONSTITUENTS_TABLE,
            fallback_type="screener_retention_failed",
            severity="warn",
            reason="Watch retention failed; constituents continue without retained members (previous hard-drop behavior).",
            error=exc,
            metadata={"rows": 0 if frame is None else int(len(frame))},
        )
        return frame


def _apply_retention(frame: pd.DataFrame, *, snapshot_date: Any | None = None) -> pd.DataFrame:
    max_window = max(RETENTION_DAYS_SCAN, RETENTION_DAYS_DEFAULT)
    if max_window <= 0:
        return frame
    today = pd.to_datetime(snapshot_date, utc=True, errors="coerce")
    if pd.isna(today):
        if frame is not None and not frame.empty and "date" in frame.columns:
            today = pd.to_datetime(frame["date"], utc=True, errors="coerce").max()
        if pd.isna(today):
            today = pd.Timestamp.utcnow()
    today = today.normalize()

    prior = sql_to_df(
        f"""
        SELECT DISTINCT ON (screener_slug, ticker)
            date, screener_slug, screener_name, screener_url, ticker, exchange,
            company_master_id, security_id, instrument, isin, display_name, rank,
            raw_item_json
        FROM {CONSTITUENTS_TABLE}
        WHERE date < %(today)s AND date >= %(today)s - interval '{int(max_window) + 5} days'
        ORDER BY screener_slug, ticker, date DESC
        """,
        params={"today": today},
    )

    frame = frame if frame is not None else pd.DataFrame()
    # Members AT the target date. Frame rows dated older than the target (sources anchored to
    # an older bhavcopy date while another source has a newer snapshot) are treated as prior
    # members so they get a retained twin at the target date -- otherwise the rule engine's
    # single-MAX-date universe read would not see them at all.
    today_members: set[tuple[str, str]] = set()
    frame_prior_rows = pd.DataFrame()
    if not frame.empty and "screener_slug" in frame.columns and "ticker" in frame.columns:
        frame_dates = pd.to_datetime(frame["date"], utc=True, errors="coerce").dt.normalize()
        at_target = frame_dates >= today
        today_members = {
            (str(slug), str(ticker).strip().upper())
            for slug, ticker in zip(frame.loc[at_target, "screener_slug"], frame.loc[at_target, "ticker"])
        }
        prior_columns = [c for c in prior.columns] if not prior.empty else [
            "date", "screener_slug", "screener_name", "screener_url", "ticker", "exchange",
            "company_master_id", "security_id", "instrument", "isin", "display_name", "rank", "raw_item_json",
        ]
        frame_prior_rows = frame.loc[~at_target, [c for c in prior_columns if c in frame.columns]].copy()
    if not frame_prior_rows.empty:
        prior = pd.concat([frame_prior_rows, prior], ignore_index=True) if not prior.empty else frame_prior_rows
        prior["_ticker_norm"] = prior["ticker"].astype("string").str.strip().str.upper()
        prior = prior.sort_values("date", ascending=False).drop_duplicates(
            subset=["screener_slug", "_ticker_norm"], keep="first"
        ).drop(columns=["_ticker_norm"])
    if prior.empty:
        return frame

    now = pd.Timestamp.utcnow()
    retained_rows: list[dict[str, Any]] = []
    exit_rows: list[dict[str, Any]] = []
    for row in prior.itertuples(index=False):
        slug = str(row.screener_slug or "")
        ticker = str(row.ticker or "").strip().upper()
        if not slug or not ticker or (slug, ticker) in today_members:
            continue
        window = retention_days_for_slug(slug)
        last_seen = pd.to_datetime(row.date, utc=True, errors="coerce")
        if pd.isna(last_seen):
            continue
        last_seen = last_seen.normalize()
        admitted = _admitted_date(row.raw_item_json, last_seen)
        age_days = int((today - admitted).days)
        if window > 0 and age_days <= window:
            try:
                raw = json.loads(row.raw_item_json) if row.raw_item_json else {}
                if not isinstance(raw, dict):
                    raw = {}
            except Exception:
                raw = {}
            raw["retained"] = True
            raw["admitted_date"] = admitted.date().isoformat()
            raw["retention_window_days"] = window
            retained_rows.append(
                {
                    "date": today,
                    "screener_slug": slug,
                    "screener_name": row.screener_name,
                    "screener_url": row.screener_url,
                    "ticker": ticker,
                    "exchange": row.exchange or "NSE",
                    "company_master_id": row.company_master_id,
                    "security_id": row.security_id,
                    "instrument": row.instrument,
                    "isin": row.isin,
                    "display_name": row.display_name,
                    "rank": row.rank,
                    "raw_item_json": json.dumps(raw, ensure_ascii=False),
                    "load_ts": now,
                }
            )
        elif last_seen >= today - pd.Timedelta(days=max(1, window) + 5):
            # window lapsed without re-admission: recorded exit, exactly once (upsert key)
            exit_rows.append(
                {
                    "asof_date": today,
                    "symbol": ticker,
                    "screener_slug": slug,
                    "last_seen_date": last_seen,
                    "admitted_date": admitted,
                    "exit_reason": "retention_lapsed",
                    "load_ts": now,
                }
            )
    if exit_rows:
        ensure_exits_table()
        upsert_to_db(
            pd.DataFrame(exit_rows), WATCH_EXITS_TABLE,
            unique_keys=["asof_date", "symbol", "screener_slug"], timescaledb_column="asof_date",
        )
    if retained_rows or exit_rows:
        print(
            f"[screener_retention] date={today.date()} retained={len(retained_rows)} exits_recorded={len(exit_rows)}",
            flush=True,
        )
    if not retained_rows:
        return frame
    retained_frame = pd.DataFrame(retained_rows)
    return pd.concat([frame, retained_frame], ignore_index=True) if not frame.empty else retained_frame
