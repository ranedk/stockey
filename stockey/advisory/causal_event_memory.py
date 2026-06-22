from __future__ import annotations

import argparse
import hashlib
import json
import math
from typing import Any

import pandas as pd

from advisory.event_evidence_store import (
    ANNOUNCEMENT_CONTEXT_OVERLAYS_TABLE,
    BHAVCOPY_CONTEXT_OVERLAYS_TABLE,
    table_exists,
)
from advisory.exchange_context_overlays import EXCHANGE_CONTEXT_OVERLAYS_TABLE
from advisory.fallback_telemetry import record_local_fallback_event
from advisory.macro_context_overlays import MACRO_CONTEXT_OVERLAYS_TABLE
from advisory.news_theme_engine import THEME_CONTEXT_OVERLAYS_TABLE
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration
from utils.sync import parse_datetime_arg


TABLE_NAME = "advisory_causal_event_memory"
SCHEMA_MIGRATION_ID = "20260621_advisory_causal_event_memory_base"
DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_LIMIT_PER_SOURCE = 5000

SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        asof_date TIMESTAMPTZ NOT NULL,
        memory_id TEXT NOT NULL,
        symbol TEXT,
        sector_code TEXT,
        sector_name TEXT,
        context_source TEXT NOT NULL,
        event_group TEXT,
        event_type TEXT,
        context_class TEXT,
        direction TEXT,
        event_state TEXT NOT NULL,
        pressure_score DOUBLE PRECISION,
        decayed_pressure_score DOUBLE PRECISION,
        event_count INTEGER,
        first_seen_at TIMESTAMPTZ,
        last_seen_at TIMESTAMPTZ,
        expected_decay_days INTEGER,
        freshness_days DOUBLE PRECISION,
        contradiction_state TEXT,
        source_refs_json TEXT,
        source_summary_json TEXT,
        authority_scope TEXT NOT NULL DEFAULT 'review_input_only',
        portfolio_authority TEXT NOT NULL DEFAULT 'none',
        broker_execution_allowed BOOLEAN NOT NULL DEFAULT FALSE,
        policy_effect TEXT NOT NULL DEFAULT 'memory_only_no_trade_authority',
        load_ts TIMESTAMPTZ NOT NULL,
        UNIQUE (asof_date, memory_id)
    )
    """,
]


SOURCE_TABLES: dict[str, str] = {
    "announcement_context": ANNOUNCEMENT_CONTEXT_OVERLAYS_TABLE,
    "bhavcopy_context": BHAVCOPY_CONTEXT_OVERLAYS_TABLE,
    "exchange_context": EXCHANGE_CONTEXT_OVERLAYS_TABLE,
    "macro_context": MACRO_CONTEXT_OVERLAYS_TABLE,
    "theme_context": THEME_CONTEXT_OVERLAYS_TABLE,
}


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, default=str)


def _clean_text(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _norm_key(value: Any) -> str:
    text = _clean_text(value)
    return (text or "").strip().upper()


def _number(value: Any) -> float | None:
    numeric = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric):
        return None
    return float(numeric)


def _direction_bucket(value: Any) -> str:
    text = _norm_key(value)
    if text in {"POSITIVE", "BUY", "BULLISH", "ACCUMULATION", "WATCH"}:
        return "positive"
    if text in {"NEGATIVE", "SELL", "BEARISH", "DISTRIBUTION", "DERISK", "REDUCE"}:
        return "negative"
    if text in {"MIXED", "CONFLICT"}:
        return "mixed"
    return "watch"


def _event_state(direction: str, contradiction_state: str) -> str:
    if contradiction_state != "none":
        return "mixed_context"
    if direction == "positive":
        return "positive_watch_pressure"
    if direction == "negative":
        return "negative_derisk_pressure"
    if direction == "mixed":
        return "mixed_context"
    return "neutral_watch_context"


def _expected_decay_days(source: str, context_class: str | None, event_type: str | None) -> int:
    label = f"{context_class or ''} {event_type or ''}".upper()
    if source in {"macro_context", "theme_context"}:
        return 7
    if source == "bhavcopy_context":
        return 3
    if any(token in label for token in ("SPLIT", "BONUS", "DIVIDEND", "BUYBACK")):
        return 20
    if any(token in label for token in ("ORDER", "REGULATORY", "CAPEX", "MERGER", "ACQUISITION")):
        return 15
    return 10


def _memory_id(row: dict[str, Any]) -> str:
    parts = [
        str(row.get("symbol") or ""),
        str(row.get("sector_code") or ""),
        str(row.get("context_source") or ""),
        str(row.get("context_class") or ""),
        str(row.get("event_type") or ""),
        str(row.get("asof_date") or ""),
    ]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:20]
    return f"cem_{digest}"


def ensure_table() -> None:
    apply_schema_migration(
        migration_id=SCHEMA_MIGRATION_ID,
        statements=SCHEMA_STATEMENTS,
        owner="advisory.causal_event_memory",
        description="Create compact causal event memory rows from review-only context overlays.",
        metadata={
            "tables": [TABLE_NAME],
            "source_tables": sorted(SOURCE_TABLES.values()),
            "authority": "review_input_only_no_broker_authority",
        },
    )


def _record_fallback(
    *,
    fallback_type: str,
    source: str,
    reason: str,
    error: Exception,
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="advisory.causal_event_memory",
        fallback_type=fallback_type,
        source=source,
        severity="warn",
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def normalize_overlay_frame(source: str, frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()
    df = frame.copy()
    for column in [
        "asof_date",
        "observed_at",
        "overlay_id",
        "symbol",
        "sector_code",
        "sector_name",
        "event_type",
        "context_class",
        "direction",
        "pressure_score",
        "authority_scope",
        "matched_sources_json",
        "watch_reason_detail",
    ]:
        if column not in df.columns:
            df[column] = None
    df["context_source"] = source
    df["asof_date"] = pd.to_datetime(df["asof_date"], utc=True, errors="coerce").dt.normalize()
    df["observed_at"] = pd.to_datetime(df["observed_at"], utc=True, errors="coerce")
    df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()
    df.loc[df["symbol"].isin(["", "<NA>", "NAN", "NONE"]), "symbol"] = pd.NA
    df["sector_code"] = df["sector_code"].astype("string").str.strip()
    df["sector_name"] = df["sector_name"].astype("string").str.strip()
    df["event_type"] = df["event_type"].astype("string").str.strip()
    df["context_class"] = df["context_class"].astype("string").str.strip()
    df["direction"] = df["direction"].map(_direction_bucket)
    df["pressure_score"] = pd.to_numeric(df["pressure_score"], errors="coerce").fillna(0.0)
    df["authority_scope"] = df["authority_scope"].astype("string").str.strip().fillna("watchlist_pressure_only")
    return df.dropna(subset=["asof_date", "overlay_id"])[
        [
            "asof_date",
            "observed_at",
            "overlay_id",
            "symbol",
            "sector_code",
            "sector_name",
            "context_source",
            "event_type",
            "context_class",
            "direction",
            "pressure_score",
            "authority_scope",
            "matched_sources_json",
            "watch_reason_detail",
        ]
    ]


def build_causal_event_memory_from_overlays(
    overlays_by_source: dict[str, pd.DataFrame],
    *,
    asof_date: pd.Timestamp | None = None,
) -> pd.DataFrame:
    frames = [
        normalize_overlay_frame(source, frame)
        for source, frame in overlays_by_source.items()
        if isinstance(frame, pd.DataFrame) and not frame.empty
    ]
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True, sort=False)
    if combined.empty:
        return pd.DataFrame()
    combined = combined[
        combined["authority_scope"].astype("string").str.lower().isin({"watchlist_pressure_only", "review_input_only"})
    ].copy()
    if combined.empty:
        return pd.DataFrame()
    reference_asof = pd.to_datetime(asof_date, utc=True, errors="coerce") if asof_date is not None else combined["asof_date"].max()
    if pd.isna(reference_asof):
        reference_asof = pd.Timestamp.utcnow()
    reference_asof = reference_asof.normalize()

    group_cols = ["asof_date", "symbol", "sector_code", "sector_name", "context_source", "context_class", "event_type"]
    rows: list[dict[str, Any]] = []
    load_ts = pd.Timestamp.utcnow()
    for keys, group in combined.groupby(group_cols, dropna=False, sort=True):
        asof, symbol, sector_code, sector_name, source, context_class, event_type = keys
        directions = sorted({str(value) for value in group["direction"].dropna().tolist() if str(value)})
        has_positive = "positive" in directions
        has_negative = "negative" in directions
        contradiction_state = "mixed_direction" if has_positive and has_negative else "none"
        if len(directions) > 1 and contradiction_state == "none":
            contradiction_state = "same_day_conflict"
        if has_positive and not has_negative:
            direction = "positive"
        elif has_negative and not has_positive:
            direction = "negative"
        elif contradiction_state != "none":
            direction = "mixed"
        else:
            direction = directions[0] if directions else "watch"
        expected_decay_days = _expected_decay_days(str(source), _clean_text(context_class), _clean_text(event_type))
        observed = pd.to_datetime(group["observed_at"], utc=True, errors="coerce")
        first_seen = observed.min()
        last_seen = observed.max()
        if pd.isna(first_seen):
            first_seen = pd.to_datetime(group["asof_date"], utc=True, errors="coerce").min()
        if pd.isna(last_seen):
            last_seen = pd.to_datetime(group["asof_date"], utc=True, errors="coerce").max()
        freshness_days = max(0.0, float((reference_asof - pd.to_datetime(last_seen, utc=True).normalize()).days))
        pressure = float(pd.to_numeric(group["pressure_score"], errors="coerce").fillna(0.0).sum())
        pressure = round(min(1.0, pressure), 6)
        decayed = round(float(pressure * math.exp(-freshness_days / max(1, expected_decay_days))), 6)
        source_refs = [
            {
                "source": source,
                "overlay_id": _clean_text(row.get("overlay_id")),
                "asof_date": _clean_text(row.get("asof_date")),
                "observed_at": _clean_text(row.get("observed_at")),
                "direction": _clean_text(row.get("direction")),
                "pressure_score": _number(row.get("pressure_score")),
            }
            for row in group.to_dict(orient="records")
        ]
        summary = {
            "direction_counts": group["direction"].astype("string").value_counts(dropna=True).sort_index().to_dict(),
            "context_source": source,
            "source_row_count": int(len(group)),
            "unique_overlay_count": int(group["overlay_id"].nunique()),
            "authority_scope": "review_input_only",
        }
        row = {
            "asof_date": pd.to_datetime(asof, utc=True).normalize(),
            "symbol": None if pd.isna(symbol) else _clean_text(symbol),
            "sector_code": None if pd.isna(sector_code) else _clean_text(sector_code),
            "sector_name": None if pd.isna(sector_name) else _clean_text(sector_name),
            "context_source": str(source),
            "event_group": str(source).replace("_context", ""),
            "event_type": None if pd.isna(event_type) else _clean_text(event_type),
            "context_class": None if pd.isna(context_class) else _clean_text(context_class),
            "direction": direction,
            "event_state": _event_state(direction, contradiction_state),
            "pressure_score": pressure,
            "decayed_pressure_score": decayed,
            "event_count": int(group["overlay_id"].nunique()),
            "first_seen_at": pd.to_datetime(first_seen, utc=True, errors="coerce"),
            "last_seen_at": pd.to_datetime(last_seen, utc=True, errors="coerce"),
            "expected_decay_days": int(expected_decay_days),
            "freshness_days": freshness_days,
            "contradiction_state": contradiction_state,
            "source_refs_json": _json_dumps(source_refs),
            "source_summary_json": _json_dumps(summary),
            "authority_scope": "review_input_only",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "policy_effect": "memory_only_no_trade_authority",
            "load_ts": load_ts,
        }
        row["memory_id"] = _memory_id(row)
        rows.append(row)
    return pd.DataFrame(rows)


def _load_source_sql(
    source: str,
    table_name: str,
    *,
    from_date: pd.Timestamp,
    to_date: pd.Timestamp,
    limit: int,
) -> pd.DataFrame:
    if source == "announcement_context":
        return sql_to_df(
            f"""
            SELECT
                asof_date,
                published_on AS observed_at,
                overlay_id,
                symbol,
                NULL::text AS sector_code,
                NULL::text AS sector_name,
                event_class AS context_class,
                event_class AS event_type,
                direction,
                pressure_score,
                authority_scope,
                matched_sources_json,
                watch_reason_detail
            FROM {table_name}
            WHERE asof_date >= %s
              AND asof_date <= %s
              AND COALESCE(production_status, 'active') = 'active'
            ORDER BY asof_date DESC
            LIMIT %s
            """,
            params=(from_date, to_date, int(limit)),
        )
    if source == "bhavcopy_context":
        return sql_to_df(
            f"""
            SELECT
                asof_date,
                asof_date AS observed_at,
                overlay_id,
                symbol,
                NULL::text AS sector_code,
                NULL::text AS sector_name,
                deal_pressure AS context_class,
                deal_pressure AS event_type,
                direction,
                pressure_score,
                authority_scope,
                matched_sources_json,
                watch_reason_detail
            FROM {table_name}
            WHERE asof_date >= %s
              AND asof_date <= %s
              AND COALESCE(production_status, 'active') = 'active'
            ORDER BY asof_date DESC
            LIMIT %s
            """,
            params=(from_date, to_date, int(limit)),
        )
    if source == "exchange_context":
        return sql_to_df(
            f"""
            SELECT
                asof_date,
                COALESCE(known_on, event_date, asof_date) AS observed_at,
                overlay_id,
                symbol,
                NULL::text AS sector_code,
                NULL::text AS sector_name,
                event_type AS context_class,
                event_type,
                direction,
                pressure_score,
                authority_scope,
                matched_sources_json,
                watch_reason_detail
            FROM {table_name}
            WHERE asof_date >= %s
              AND asof_date <= %s
              AND COALESCE(production_status, 'active') = 'active'
            ORDER BY asof_date DESC
            LIMIT %s
            """,
            params=(from_date, to_date, int(limit)),
        )
    if source == "macro_context":
        return sql_to_df(
            f"""
            SELECT
                asof_date,
                asof_date AS observed_at,
                overlay_id,
                NULL::text AS symbol,
                sector_code,
                sector_name,
                macro_signal_name AS context_class,
                macro_signal_id AS event_type,
                direction,
                pressure_score,
                authority_scope,
                matched_sources_json,
                trigger_reason AS watch_reason_detail
            FROM {table_name}
            WHERE asof_date >= %s
              AND asof_date <= %s
              AND COALESCE(production_status, 'active') = 'active'
            ORDER BY asof_date DESC
            LIMIT %s
            """,
            params=(from_date, to_date, int(limit)),
        )
    if source == "theme_context":
        return sql_to_df(
            f"""
            SELECT
                asof_date,
                asof_date AS observed_at,
                overlay_id,
                NULL::text AS symbol,
                sector_code,
                sector_name,
                theme_name AS context_class,
                theme_id AS event_type,
                direction,
                pressure_score,
                authority_scope,
                matched_sources_json,
                theme_reason AS watch_reason_detail
            FROM {table_name}
            WHERE asof_date >= %s
              AND asof_date <= %s
              AND COALESCE(production_status, 'active') = 'active'
            ORDER BY asof_date DESC
            LIMIT %s
            """,
            params=(from_date, to_date, int(limit)),
        )
    return pd.DataFrame()


def load_context_overlay_frames(
    *,
    from_date: pd.Timestamp,
    to_date: pd.Timestamp,
    limit_per_source: int = DEFAULT_LIMIT_PER_SOURCE,
) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    frames: dict[str, pd.DataFrame] = {}
    meta: dict[str, Any] = {"sources": {}, "fallbacks": []}
    for source, table_name in SOURCE_TABLES.items():
        try:
            if not table_exists(table_name):
                frames[source] = pd.DataFrame()
                meta["sources"][source] = {"table": table_name, "rows": 0, "loaded": False, "missing_table": True}
                continue
            frame = _load_source_sql(source, table_name, from_date=from_date, to_date=to_date, limit=limit_per_source)
            frames[source] = frame
            meta["sources"][source] = {"table": table_name, "rows": int(len(frame)), "loaded": True, "missing_table": False}
        except Exception as exc:
            _record_fallback(
                fallback_type="causal_event_memory_source_load_failed",
                source=table_name,
                reason="Causal event memory could not load one context-overlay source.",
                error=exc,
                metadata={"context_source": source, "from_date": str(from_date), "to_date": str(to_date)},
            )
            frames[source] = pd.DataFrame()
            meta["sources"][source] = {
                "table": table_name,
                "rows": 0,
                "loaded": False,
                "error_type": exc.__class__.__name__,
                "error": str(exc)[:500],
            }
            meta["fallbacks"].append({"source": source, "table": table_name, "error_type": exc.__class__.__name__})
    return frames, meta


def build_causal_event_memory(
    *,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    asof_date: pd.Timestamp | None = None,
    limit_per_source: int = DEFAULT_LIMIT_PER_SOURCE,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    end = pd.to_datetime(to_date or asof_date or pd.Timestamp.utcnow(), utc=True).normalize()
    start = pd.to_datetime(from_date or (end - pd.Timedelta(days=DEFAULT_LOOKBACK_DAYS)), utc=True).normalize()
    frames, meta = load_context_overlay_frames(from_date=start, to_date=end, limit_per_source=limit_per_source)
    memory = build_causal_event_memory_from_overlays(frames, asof_date=asof_date or end)

    def _non_empty_text_mask(series: pd.Series) -> pd.Series:
        values = series.astype("string").str.strip()
        return values.notna() & ~values.isin(["", "<NA>", "<na>", "NAN", "nan", "NONE", "None"])

    source_coverage: dict[str, Any] = {}
    if not memory.empty:
        for source, group in memory.groupby("context_source", dropna=False, sort=True):
            symbol_series = group.get("symbol", pd.Series(dtype="object"))
            sector_series = group.get("sector_code", pd.Series(dtype="object"))
            symbol_mask = _non_empty_text_mask(symbol_series)
            sector_mask = _non_empty_text_mask(sector_series)
            source_coverage[str(source or "unknown")] = {
                "memory_rows": int(len(group)),
                "symbol_scoped_rows": int(symbol_mask.sum()),
                "sector_scoped_rows": int(sector_mask.sum()),
                "symbol_count": int(symbol_series.astype("string").str.strip()[symbol_mask].nunique()),
                "sector_count": int(sector_series.astype("string").str.strip()[sector_mask].nunique()),
            }
    symbol_rows = 0
    sector_rows = 0
    if not memory.empty:
        symbol_rows = int(_non_empty_text_mask(memory.get("symbol", pd.Series(dtype="object"))).sum())
        sector_rows = int(_non_empty_text_mask(memory.get("sector_code", pd.Series(dtype="object"))).sum())
    meta.update(
        {
            "status": "ok",
            "from_date": start.isoformat(),
            "to_date": end.isoformat(),
            "memory_rows": int(len(memory)),
            "symbol_scoped_memory_rows": symbol_rows,
            "sector_scoped_memory_rows": sector_rows,
            "source_coverage": source_coverage,
            "table": TABLE_NAME,
            "authority_scope": "review_input_only",
            "broker_execution_allowed": False,
        }
    )
    return memory, meta


def persist_causal_event_memory(df: pd.DataFrame) -> None:
    if df.empty:
        return
    ensure_table()
    out = df.copy()
    for column in ["pressure_score", "decayed_pressure_score", "freshness_days"]:
        out[column] = pd.to_numeric(out[column], errors="coerce")
    out["event_count"] = pd.to_numeric(out["event_count"], errors="coerce").fillna(0).astype(int)
    out["expected_decay_days"] = pd.to_numeric(out["expected_decay_days"], errors="coerce").fillna(0).astype(int)
    out["broker_execution_allowed"] = out["broker_execution_allowed"].map(lambda value: bool(value))
    for column in ["asof_date", "first_seen_at", "last_seen_at", "load_ts"]:
        out[column] = pd.to_datetime(out[column], utc=True, errors="coerce")
    upsert_to_db(out, TABLE_NAME, unique_keys=["asof_date", "memory_id"], timescaledb_column="asof_date")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build compact review-only causal event memory from context overlays.")
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    parser.add_argument("--asof-date")
    parser.add_argument("--limit-per-source", type=int, default=DEFAULT_LIMIT_PER_SOURCE)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--format", choices=["json", "text"], default="json")
    args = parser.parse_args(argv)

    memory, meta = build_causal_event_memory(
        from_date=parse_datetime_arg(args.from_date),
        to_date=parse_datetime_arg(args.to_date),
        asof_date=parse_datetime_arg(args.asof_date),
        limit_per_source=max(1, int(args.limit_per_source)),
    )
    if not args.dry_run:
        persist_causal_event_memory(memory)
    payload = {
        **meta,
        "persisted": not bool(args.dry_run),
        "sample": memory.head(10).to_dict(orient="records") if not memory.empty else [],
    }
    if args.format == "text":
        print(
            f"status={payload['status']} rows={payload['memory_rows']} persisted={payload['persisted']} "
            f"from={payload['from_date']} to={payload['to_date']}"
        )
        for row in payload["sample"][:5]:
            print(
                f"- {row.get('asof_date')} {row.get('symbol') or row.get('sector_name') or 'MARKET'} "
                f"{row.get('context_source')} {row.get('event_state')} score={row.get('decayed_pressure_score')}"
            )
    else:
        print(_json_dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
