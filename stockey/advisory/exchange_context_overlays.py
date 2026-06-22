from __future__ import annotations

import argparse
import hashlib
import json
from typing import Any

import pandas as pd

from advisory.exchange_events import TABLE_NAME as EXCHANGE_EVENTS_TABLE
from advisory.exchange_events import table_exists
from advisory.fallback_telemetry import record_local_fallback_event
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration
from utils.sync import parse_datetime_arg


EXCHANGE_CONTEXT_OVERLAYS_TABLE = "advisory_exchange_context_overlays"
EXCHANGE_CONTEXT_OVERLAY_SCHEMA_MIGRATION_ID = "20260620_advisory_exchange_context_overlays_base"
DEFAULT_LOOKBACK_DAYS = 20

EXCHANGE_CONTEXT_OVERLAY_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {EXCHANGE_CONTEXT_OVERLAYS_TABLE} (
        asof_date TIMESTAMPTZ NOT NULL,
        overlay_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        event_source TEXT,
        event_type TEXT,
        symbol TEXT NOT NULL,
        company_master_id TEXT,
        known_on TIMESTAMPTZ,
        event_date TIMESTAMPTZ,
        direction TEXT NOT NULL,
        pressure_score DOUBLE PRECISION,
        materiality_score DOUBLE PRECISION,
        event_side TEXT,
        event_value_inr DOUBLE PRECISION,
        participant TEXT,
        watch_reason_detail TEXT,
        matched_sources_json TEXT,
        authority_scope TEXT NOT NULL DEFAULT 'watchlist_pressure_only',
        production_status TEXT NOT NULL DEFAULT 'active',
        load_ts TIMESTAMPTZ NOT NULL,
        UNIQUE (asof_date, overlay_id)
    )
    """,
]


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, default=str)


def _clean_text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _number(value: Any) -> float | None:
    numeric = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric):
        return None
    return float(numeric)


def _score_value(value_inr: float | None) -> float:
    if value_inr is None or value_inr <= 0:
        return 0.2
    if value_inr >= 50_00_00_000:
        return 1.0
    if value_inr >= 10_00_00_000:
        return 0.8
    if value_inr >= 2_00_00_000:
        return 0.6
    return 0.35


def _asof_event_window(asof_date: pd.Timestamp | None, *, lookback_days: int) -> tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]:
    effective_asof = pd.to_datetime(asof_date or pd.Timestamp.utcnow(), utc=True, errors="coerce")
    if pd.isna(effective_asof):
        effective_asof = pd.Timestamp.utcnow()
    effective_asof = effective_asof.tz_convert("UTC") if effective_asof.tzinfo else effective_asof.tz_localize("UTC")
    effective_day = effective_asof.normalize()
    start_date = effective_day - pd.Timedelta(days=max(1, int(lookback_days)))
    if effective_asof == effective_day:
        # Daily advisory runs pass date-normalized timestamps; include the full as-of date without crossing into tomorrow.
        upper_bound = effective_day + pd.Timedelta(days=1)
    else:
        upper_bound = effective_asof
    return start_date, upper_bound, effective_day


def ensure_exchange_context_overlay_table() -> None:
    apply_schema_migration(
        migration_id=EXCHANGE_CONTEXT_OVERLAY_SCHEMA_MIGRATION_ID,
        statements=EXCHANGE_CONTEXT_OVERLAY_SCHEMA_STATEMENTS,
        owner="advisory.exchange_context_overlays",
        description="Create review-only exchange-event context overlay table.",
        metadata={"tables": [EXCHANGE_CONTEXT_OVERLAYS_TABLE], "workflow": "exchange_context_overlays"},
    )


def classify_exchange_event(row: dict[str, Any]) -> dict[str, Any] | None:
    event_type = _clean_text(row.get("event_type")).upper()
    side = _clean_text(row.get("side")).upper()
    value_inr = _number(row.get("value_inr"))
    value_score = _score_value(value_inr)

    direction = "watch"
    base_score = 0.25
    reason = "Exchange event needs context review before it changes watchlist pressure."

    if event_type == "INSIDER_DEAL":
        if side == "BUY":
            direction = "positive"
            base_score = 0.65
            reason = "Insider acquisition can indicate alignment or confidence; confirm size, role, and price context."
        elif side == "SELL":
            direction = "negative"
            base_score = 0.6
            reason = "Insider disposal can weaken conviction; confirm size, recurrence, role, and whether it is routine."
    elif event_type in {"BLOCK_DEAL", "BULK_DEAL"}:
        if side == "BUY":
            direction = "positive"
            base_score = 0.45
            reason = "Large market purchase can indicate institutional participation; confirm buyer quality and price action."
        elif side == "SELL":
            direction = "negative"
            base_score = 0.45
            reason = "Large market sale can create supply pressure; confirm seller identity, size, and price absorption."
    elif event_type == "SHORT_SELLING":
        direction = "negative"
        base_score = 0.55
        reason = "Short-selling disclosure is negative watch pressure; confirm whether it is isolated or persistent."
    elif "BUYBACK" in event_type:
        direction = "positive"
        base_score = 0.55
        reason = "Buyback-related corporate action can support shareholder returns; confirm size, mode, and valuation context."
    elif event_type in {"BONUS", "SPLIT", "DIVIDEND"} or "BONUS" in event_type or "SPLIT" in event_type or "DIVIDEND" in event_type:
        direction = "watch"
        base_score = 0.3
        reason = "Corporate action affects interpretation of price history or investor interest; adjust for action before reading signals."
    elif event_type in {"EARNINGS_EVENT", "NSE_EVENT"}:
        direction = "watch"
        base_score = 0.25
        reason = "Upcoming or recent exchange calendar event should be considered before entry, add, or exit decisions."

    if not event_type:
        return None
    pressure_score = round(min(1.0, base_score + (0.25 * value_score)), 6)
    return {
        "direction": direction,
        "pressure_score": pressure_score,
        "materiality_score": round(value_score, 6),
        "watch_reason_detail": reason,
    }


def _overlay_id(asof_date: pd.Timestamp, row: dict[str, Any], classification: dict[str, Any]) -> str:
    event_id = _clean_text(row.get("event_id"))
    if event_id:
        return f"{asof_date.date()}:{event_id}:{classification['direction']}"
    payload = json.dumps(
        {
            "asof_date": asof_date.isoformat(),
            "symbol": _clean_text(row.get("symbol")).upper(),
            "event_type": _clean_text(row.get("event_type")).upper(),
            "known_on": str(row.get("known_on")),
            "side": _clean_text(row.get("side")).upper(),
            "participant": _clean_text(row.get("participant")),
        },
        sort_keys=True,
        default=str,
    )
    return f"{asof_date.date()}:{hashlib.sha1(payload.encode('utf-8')).hexdigest()}:{classification['direction']}"


def build_exchange_context_overlays_from_events(
    events: pd.DataFrame,
    *,
    asof_date: pd.Timestamp | None = None,
) -> pd.DataFrame:
    if events.empty:
        return pd.DataFrame()
    resolved = pd.to_datetime(asof_date or events.get("known_on", pd.Series([pd.Timestamp.utcnow()])).max(), utc=True, errors="coerce")
    if pd.isna(resolved):
        resolved = pd.Timestamp.utcnow()
    resolved = resolved.normalize()
    load_ts = pd.Timestamp.utcnow()
    rows: list[dict[str, Any]] = []
    for raw in events.to_dict(orient="records"):
        symbol = _clean_text(raw.get("symbol")).upper()
        event_id = _clean_text(raw.get("event_id"))
        if not symbol or not event_id:
            continue
        classification = classify_exchange_event(raw)
        if not classification:
            continue
        matched = {
            "source": "exchange_event",
            "event_id": event_id,
            "event_source": _clean_text(raw.get("event_source")),
            "event_type": _clean_text(raw.get("event_type")).upper(),
            "side": _clean_text(raw.get("side")).upper() or None,
            "summary": _clean_text(raw.get("event_summary"))[:500] or None,
        }
        rows.append(
            {
                "asof_date": resolved,
                "overlay_id": _overlay_id(resolved, raw, classification),
                "event_id": event_id,
                "event_source": _clean_text(raw.get("event_source")) or None,
                "event_type": _clean_text(raw.get("event_type")).upper() or None,
                "symbol": symbol,
                "company_master_id": _clean_text(raw.get("company_master_id")) or None,
                "known_on": pd.to_datetime(raw.get("known_on"), utc=True, errors="coerce"),
                "event_date": pd.to_datetime(raw.get("event_date"), utc=True, errors="coerce"),
                "direction": classification["direction"],
                "pressure_score": classification["pressure_score"],
                "materiality_score": classification["materiality_score"],
                "event_side": _clean_text(raw.get("side")).upper() or None,
                "event_value_inr": _number(raw.get("value_inr")),
                "participant": _clean_text(raw.get("participant"))[:500] or None,
                "watch_reason_detail": classification["watch_reason_detail"],
                "matched_sources_json": _json_dumps([matched]),
                "authority_scope": "watchlist_pressure_only",
                "production_status": "active",
                "load_ts": load_ts,
            }
        )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    for column in ["asof_date", "known_on", "event_date", "load_ts"]:
        df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    for column in ["pressure_score", "materiality_score", "event_value_inr"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    return df.drop_duplicates(subset=["asof_date", "overlay_id"], keep="last")


def load_recent_exchange_events_for_overlays(
    *,
    asof_date: pd.Timestamp | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> pd.DataFrame:
    if not table_exists(EXCHANGE_EVENTS_TABLE):
        return pd.DataFrame()
    start_date, upper_bound, effective_day = _asof_event_window(asof_date, lookback_days=lookback_days)
    try:
        return sql_to_df(
            f"""
            SELECT *
            FROM {EXCHANGE_EVENTS_TABLE}
            WHERE known_on >= %(start_date)s
              AND known_on < %(upper_bound)s
            ORDER BY known_on DESC, symbol, event_type
            """,
            params={"start_date": start_date, "upper_bound": upper_bound},
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.exchange_context_overlays",
            fallback_type="exchange_context_events_load_failed",
            source=EXCHANGE_EVENTS_TABLE,
            severity="warn",
            reason="Exchange context overlay builder could not load recent exchange events.",
            error=exc,
            metadata={"asof_date": str(effective_day), "upper_bound": str(upper_bound), "lookback_days": int(lookback_days)},
        )
        return pd.DataFrame()


def build_exchange_context_overlays(
    *,
    asof_date: pd.Timestamp | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    events: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    effective_events = events if events is not None else load_recent_exchange_events_for_overlays(asof_date=asof_date, lookback_days=lookback_days)
    overlays = build_exchange_context_overlays_from_events(effective_events, asof_date=asof_date)
    meta = {
        "asof_date": pd.to_datetime(asof_date or pd.Timestamp.utcnow(), utc=True, errors="coerce").normalize(),
        "source_event_count": int(len(effective_events)),
        "overlay_count": int(len(overlays)),
        "authority_scope": "watchlist_pressure_only",
        "lookback_days": int(lookback_days),
    }
    return overlays, meta


def persist_exchange_context_overlays(df: pd.DataFrame) -> None:
    ensure_exchange_context_overlay_table()
    if df.empty:
        return
    upsert_to_db(
        df,
        EXCHANGE_CONTEXT_OVERLAYS_TABLE,
        unique_keys=["asof_date", "overlay_id"],
        timescaledb_column="asof_date",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build review-only exchange-event context overlays.")
    parser.add_argument("--date", default=None, help="As-of date, default today UTC.")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--format", choices=["json", "text"], default="text")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    asof_date = pd.to_datetime(parse_datetime_arg(args.date) if args.date else pd.Timestamp.utcnow(), utc=True, errors="coerce").normalize()
    overlays, meta = build_exchange_context_overlays(asof_date=asof_date, lookback_days=args.lookback_days)
    if not args.dry_run:
        persist_exchange_context_overlays(overlays)
    payload = {
        "status": "ok",
        "table": EXCHANGE_CONTEXT_OVERLAYS_TABLE,
        "rows": int(len(overlays)),
        "dry_run": bool(args.dry_run),
        "meta": meta,
    }
    if args.format == "json":
        print(json.dumps(payload, default=str, ensure_ascii=False, sort_keys=True))
    else:
        print(f"exchange_context_overlays rows={len(overlays)} dry_run={args.dry_run} table={EXCHANGE_CONTEXT_OVERLAYS_TABLE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
