from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import timedelta
from typing import Any

import pandas as pd

from advisory.announcement_watch import is_material_context_event, persist_watch_outputs, run_announcement_watch
from advisory.announcement_watch import DEFAULT_ANNOUNCEMENT_CONTEXT_LOOKBACK_DAYS
from advisory.announcement_watch import DEFAULT_ANNOUNCEMENT_CONTEXT_WATCH_LIMIT
from advisory.announcement_watch import DEFAULT_MARKET_CONTEXT_WATCH_LIMIT
from advisory.announcement_watch import DEFAULT_BHAVCOPY_CONTEXT_WATCH_LIMIT
from advisory.announcement_watch import DEFAULT_MACRO_CONTEXT_WATCH_LIMIT
from advisory.announcement_watch import WATCHLIST_CONTEXT_OVERLAY_EXCLUDED_RELIABILITY_CLASSES
from advisory.announcement_watch import load_context_family_reliability
from advisory.action_recommender import build_action_recommendations, persist_action_recommendations
from advisory.event_router import route_live_updates
from advisory.fallback_telemetry import record_local_fallback_event
from advisory.news_watch import persist_news_events, run_news_watch
from advisory.signal_refresh import refresh_from_causal_memory, refresh_from_theme_context
from advisory.sync_state import ensure_sync_state_table, load_sync_state, persist_sync_state, publish_bus_message
from data.dhanlive.ohlcv import sync_many_intraday
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration


WATCHLIST_TABLE = "advisory_watchlist"
EXCHANGE_CONTEXT_OVERLAYS_TABLE = "advisory_exchange_context_overlays"
EXCHANGE_CONTEXT_SOURCE_FAMILY = "exchange_context"
ALERTS_TABLE = "advisory_live_watch_alerts"
ALERTS_SCHEMA_MIGRATION_ID = "20260611_advisory_live_watch_alerts_base"
DEFAULT_ALERT_COOLDOWN_SECONDS = int(os.getenv("WATCHER_ALERT_COOLDOWN_SECONDS", "900"))
WATCHER_EXCHANGE_CONTEXT_PRIORITY_ENABLED = os.getenv(
    "WATCHER_EXCHANGE_CONTEXT_PRIORITY_ENABLED",
    "true",
).strip().lower() not in {"0", "false", "no"}
WATCHER_EXCHANGE_CONTEXT_PRIORITY_LOOKBACK_DAYS = int(os.getenv("WATCHER_EXCHANGE_CONTEXT_PRIORITY_LOOKBACK_DAYS", "5"))
WATCHER_ACTION_REFRESH_ENABLED = os.getenv("WATCHER_ACTION_REFRESH_ENABLED", "true").strip().lower() not in {"0", "false", "no"}
WATCHER_ACTION_REFRESH_MAX_SYMBOLS = int(os.getenv("WATCHER_ACTION_REFRESH_MAX_SYMBOLS", "150"))
WATCHER_CAUSAL_MEMORY_REFRESH_ENABLED = os.getenv("WATCHER_CAUSAL_MEMORY_REFRESH_ENABLED", "true").strip().lower() not in {"0", "false", "no"}
WATCHER_CAUSAL_MEMORY_REFRESH_LIMIT = int(os.getenv("WATCHER_CAUSAL_MEMORY_REFRESH_LIMIT", "25"))
WATCHER_ANNOUNCEMENT_MAX_INGEST_TARGETS = int(os.getenv("WATCHER_ANNOUNCEMENT_MAX_INGEST_TARGETS", "12"))
# Review-only intraday alerts when a material exchange filing lands for a monitored
# symbol, so the operator sees it within the day instead of only at EOD. Default on.
WATCHER_ANNOUNCEMENT_ALERTS_ENABLED = os.getenv("WATCHER_ANNOUNCEMENT_ALERTS_ENABLED", "true").strip().lower() not in {"0", "false", "no"}
WATCHER_ANNOUNCEMENT_ALERT_MAX = int(os.getenv("WATCHER_ANNOUNCEMENT_ALERT_MAX", "25"))
WATCHER_ANNOUNCEMENT_ALERT_MAX_AGE_HOURS = int(os.getenv("WATCHER_ANNOUNCEMENT_ALERT_MAX_AGE_HOURS", "36"))

ALERTS_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {ALERTS_TABLE} (
        observed_at TIMESTAMPTZ NOT NULL,
        asof_date TIMESTAMPTZ,
        setup_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        alert_type TEXT NOT NULL,
        alert_reason TEXT,
        monitor_source TEXT,
        last_price DOUBLE PRECISION,
        attractive_price_low DOUBLE PRECISION,
        attractive_price_high DOUBLE PRECISION,
        stop_price DOUBLE PRECISION,
        invalidation_price DOUBLE PRECISION,
        current_state TEXT,
        alert_fingerprint TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (observed_at, setup_id, symbol, alert_type)
    )
    """,
    f"ALTER TABLE {ALERTS_TABLE} ADD COLUMN IF NOT EXISTS monitor_source TEXT",
    f"ALTER TABLE {ALERTS_TABLE} ADD COLUMN IF NOT EXISTS stop_price DOUBLE PRECISION",
    f"ALTER TABLE {ALERTS_TABLE} ADD COLUMN IF NOT EXISTS alert_fingerprint TEXT",
]


def _emit(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def ensure_alerts_table() -> None:
    apply_schema_migration(
        migration_id=ALERTS_SCHEMA_MIGRATION_ID,
        description="Create and normalize continuous-watch live alert table.",
        statements=ALERTS_SCHEMA_STATEMENTS,
        metadata={"module": "advisory.continuous_watch", "tables": [ALERTS_TABLE]},
    )


def _context_class_reliability_classification(family_row: dict[str, object] | None, context_class: object) -> str | None:
    if not isinstance(family_row, dict):
        return None
    target_class = str(context_class or "").strip().upper()
    if not target_class or target_class in {"<NA>", "NAN", "NONE"}:
        return None
    for item in family_row.get("context_class_diagnostics") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("context_class") or "").strip().upper() != target_class:
            continue
        classification = str(item.get("classification") or "").strip()
        return classification or None
    return None


def _reliability_families(reliability: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(reliability, dict):
        return {}
    families = reliability.get("families")
    if isinstance(families, dict):
        return families
    if isinstance(families, list):
        return {
            str(item.get("source_family")): item
            for item in families
            if isinstance(item, dict) and item.get("source_family")
        }
    return {}


def _runtime_contract_for_context_reliability(row: dict[str, Any] | None) -> dict[str, Any]:
    from advisory.context_overlay_reliability_report import reliability_runtime_policy_contract

    if not isinstance(row, dict):
        return reliability_runtime_policy_contract(None)
    contract = row.get("runtime_policy_contract")
    if isinstance(contract, dict) and contract:
        return contract
    return reliability_runtime_policy_contract(row.get("classification"))


def _runtime_contract_allows_context(
    row: dict[str, Any] | None,
    use_name: str,
    *,
    legacy_classification: str | None = None,
) -> bool:
    contract = _runtime_contract_for_context_reliability(row)
    allowed = contract.get("allowed_runtime_uses")
    if isinstance(allowed, dict) and use_name in allowed:
        return bool(allowed.get(use_name))
    classification = str((row or {}).get("classification") or legacy_classification or "").strip()
    if use_name == "watch_priority":
        return classification == "candidate_helpful"
    if use_name == "de_risk_review":
        return classification == "protective_candidate"
    return False


def _runtime_contract_allows_exchange_priority(row: dict[str, Any] | None) -> bool:
    if not isinstance(row, dict) or not row:
        return True
    classification = str(row.get("classification") or "").strip()
    return bool(
        _runtime_contract_allows_context(row, "watch_priority", legacy_classification=classification)
        or _runtime_contract_allows_context(row, "de_risk_review", legacy_classification=classification)
    )


def load_active_watchlist(asof_date: pd.Timestamp | None = None) -> pd.DataFrame:
    clauses = ["COALESCE(watch_enabled, TRUE) = TRUE", "COALESCE(watch_status, 'active') IN ('active', 'review_manual')"]
    params: list[object] = []
    if asof_date is not None:
        clauses.append("asof_date = %s")
        params.append(asof_date)
    else:
        clauses.append("asof_date = (SELECT MAX(asof_date) FROM advisory_watchlist)")
    df = sql_to_df(
        f"""
        SELECT *
        FROM {WATCHLIST_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY setup_id, symbol
        """,
        params=tuple(params) if params else None,
    )
    if df.empty:
        return df
    for column in ["asof_date", "state_updated_at", "watch_started_at", "last_checked_at", "last_document_published_on"]:
        if column in df.columns:
            df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    df["symbol"] = df["symbol"].astype("string").str.upper()
    df["monitor_source"] = "watchlist"
    return df


def load_open_positions(asof_date: pd.Timestamp | None = None) -> pd.DataFrame:
    queries = [
        """
        SELECT
            l.asof_date,
            l.setup_id,
            p.setup_name,
            p.symbol,
            p.company_master_id,
            p.invalidation_price,
            l.stop_price,
            l.position_status,
            l.next_action,
            l.next_action_reason,
            TRUE AS watch_enabled,
            'position_active' AS watch_status,
            'OPEN_POSITION' AS current_state,
            NULL::DOUBLE PRECISION AS attractive_price_low,
            NULL::DOUBLE PRECISION AS attractive_price_high,
            NULL::BIGINT AS rank
        FROM advisory_position_lifecycle l
        JOIN advisory_portfolio_orders p
          ON p.asof_date = l.asof_date
         AND p.published_on = l.published_on
         AND p.setup_id = l.setup_id
         AND p.symbol = l.symbol
         AND p.unique_id = l.unique_id
        WHERE l.asof_date = COALESCE(%s, (SELECT MAX(asof_date) FROM advisory_position_lifecycle))
          AND l.position_status IN ('open', 'review', 'pending_entry', 'exit_review')
        """,
        """
        SELECT
            asof_date,
            setup_id,
            setup_name,
            symbol,
            company_master_id,
            invalidation_price,
            stop_price,
            NULL::TEXT AS position_status,
            NULL::TEXT AS next_action,
            NULL::TEXT AS next_action_reason,
            TRUE AS watch_enabled,
            'position_active' AS watch_status,
            'OPEN_POSITION' AS current_state,
            NULL::DOUBLE PRECISION AS attractive_price_low,
            NULL::DOUBLE PRECISION AS attractive_price_high,
            NULL::BIGINT AS rank
        FROM advisory_portfolio_orders
        WHERE asof_date = COALESCE(%s, (SELECT MAX(asof_date) FROM advisory_portfolio_orders))
          AND portfolio_status IN ('approved', 'trimmed')
        """,
    ]
    for query_index, query in enumerate(queries, start=1):
        try:
            df = sql_to_df(query, params=(asof_date,))
        except Exception as exc:
            record_local_fallback_event(
                module="advisory.continuous_watch",
                source="open_positions",
                fallback_type="continuous_watch_open_positions_load_failed",
                severity="error",
                reason="Continuous watcher could not load open positions from one source query and tried the next source.",
                error=exc,
                metadata={
                    "query_index": query_index,
                    "asof_date": None if asof_date is None else str(asof_date),
                },
            )
            continue
        if df.empty:
            continue
        for column in ["asof_date"]:
            if column in df.columns:
                df[column] = pd.to_datetime(df[column], utc=True, errors="coerce").dt.normalize()
        for column in ["attractive_price_low", "attractive_price_high", "invalidation_price", "stop_price", "rank"]:
            if column in df.columns:
                df[column] = pd.to_numeric(df[column], errors="coerce")
        df["symbol"] = df["symbol"].astype("string").str.upper()
        df["monitor_source"] = "position"
        return df
    return pd.DataFrame()


def load_exchange_context_priorities(
    symbols: list[str],
    *,
    asof_date: pd.Timestamp | None = None,
) -> pd.DataFrame:
    if not WATCHER_EXCHANGE_CONTEXT_PRIORITY_ENABLED or not symbols:
        return pd.DataFrame()
    cutoff = pd.to_datetime(asof_date or pd.Timestamp.utcnow(), utc=True, errors="coerce")
    if pd.isna(cutoff):
        cutoff = pd.Timestamp.utcnow()
    cutoff = cutoff.normalize()
    from_date = cutoff - pd.Timedelta(days=max(1, int(WATCHER_EXCHANGE_CONTEXT_PRIORITY_LOOKBACK_DAYS)))
    normalized_symbols = sorted({str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()})
    if not normalized_symbols:
        return pd.DataFrame()
    family_row: dict[str, object] | None = None
    reliability_classification: str | None = None
    reliability_policy = "no_reliability_evidence_neutral"
    try:
        reliability = load_context_family_reliability(asof_date=cutoff)
        family_rows = _reliability_families(reliability)
        family_row = family_rows.get(EXCHANGE_CONTEXT_SOURCE_FAMILY) if isinstance(family_rows, dict) else None
        if isinstance(family_row, dict):
            reliability_classification = str(family_row.get("classification") or "").strip() or None
            if reliability_classification in WATCHLIST_CONTEXT_OVERLAY_EXCLUDED_RELIABILITY_CLASSES:
                return pd.DataFrame()
            if not _runtime_contract_allows_exchange_priority(family_row):
                return pd.DataFrame()
            reliability_policy = (
                "candidate_helpful_priority_allowed"
                if _runtime_contract_allows_context(
                    family_row,
                    "watch_priority",
                    legacy_classification=reliability_classification,
                )
                else "protective_candidate_priority_allowed"
                if _runtime_contract_allows_context(
                    family_row,
                    "de_risk_review",
                    legacy_classification=reliability_classification,
                )
                else "classification_neutral_priority_allowed"
            )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.continuous_watch",
            source="advisory_context_overlay_reliability_summary",
            fallback_type="continuous_watch_exchange_context_reliability_load_failed",
            severity="warn",
            reason="Continuous watcher could not load exchange-context reliability and kept priority ordering neutral/allowed.",
            error=exc,
            metadata={
                "asof_date": str(cutoff),
                "source_family": EXCHANGE_CONTEXT_SOURCE_FAMILY,
            },
        )
    try:
        df = sql_to_df(
            f"""
            SELECT
                symbol,
                UPPER(TRIM(COALESCE(event_type, 'UNSPECIFIED'))) AS exchange_context_class,
                MAX(known_on) AS latest_exchange_context_known_on,
                MAX(asof_date) AS latest_exchange_context_asof_date,
                MAX(ABS(COALESCE(pressure_score, 0.0))) AS exchange_context_priority_score,
                COUNT(*) AS exchange_context_event_count
            FROM {EXCHANGE_CONTEXT_OVERLAYS_TABLE}
            WHERE symbol = ANY(%(symbols)s)
              AND COALESCE(production_status, 'active') = 'active'
              AND COALESCE(authority_scope, 'watchlist_pressure_only') = 'watchlist_pressure_only'
              AND COALESCE(known_on, asof_date) >= %(from_date)s
              AND COALESCE(known_on, asof_date) <= %(cutoff)s + INTERVAL '1 day'
            GROUP BY symbol, UPPER(TRIM(COALESCE(event_type, 'UNSPECIFIED')))
            """,
            params={"symbols": normalized_symbols, "from_date": from_date, "cutoff": cutoff},
            retries=2,
            statement_timeout_ms=10000,
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.continuous_watch",
            source=EXCHANGE_CONTEXT_OVERLAYS_TABLE,
            fallback_type="continuous_watch_exchange_context_priority_load_failed",
            severity="warn",
            reason="Continuous watcher could not load recent exchange-context overlay priority and kept normal watch ordering.",
            error=exc,
            metadata={
                "asof_date": str(cutoff),
                "from_date": str(from_date),
                "symbols_count": len(normalized_symbols),
            },
        )
        return pd.DataFrame()
    if df.empty:
        return df
    df["symbol"] = df["symbol"].astype("string").str.upper()
    if "exchange_context_class" not in df.columns:
        df["exchange_context_class"] = "UNSPECIFIED"
    df["exchange_context_class"] = df["exchange_context_class"].astype("string").str.strip().str.upper()
    df.loc[df["exchange_context_class"].isin(["", "<NA>", "NAN", "NONE"]), "exchange_context_class"] = "UNSPECIFIED"
    df["exchange_context_priority_score"] = pd.to_numeric(df["exchange_context_priority_score"], errors="coerce").fillna(0.0)
    df["exchange_context_event_count"] = pd.to_numeric(df["exchange_context_event_count"], errors="coerce").fillna(0).astype("int64")
    df["exchange_context_class_reliability_classification"] = df["exchange_context_class"].map(
        lambda context_class: _context_class_reliability_classification(family_row, context_class)
    )
    excluded_class_mask = df["exchange_context_class_reliability_classification"].astype("string").isin(
        WATCHLIST_CONTEXT_OVERLAY_EXCLUDED_RELIABILITY_CLASSES
    )
    if excluded_class_mask.any():
        df = df.loc[~excluded_class_mask].copy()
    if isinstance(family_row, dict) and not df.empty:
        class_contract_blocked = []
        for _, row in df.iterrows():
            context_class = str(row.get("exchange_context_class") or "").strip().upper()
            class_row = None
            for item in family_row.get("context_class_diagnostics") or []:
                if not isinstance(item, dict):
                    continue
                if str(item.get("context_class") or "").strip().upper() == context_class:
                    class_row = item
                    break
            class_contract_blocked.append(bool(class_row and not _runtime_contract_allows_exchange_priority(class_row)))
        if any(class_contract_blocked):
            df = df.loc[[not value for value in class_contract_blocked]].copy()
    if df.empty:
        return pd.DataFrame()
    df = (
        df.sort_values(
            ["symbol", "exchange_context_priority_score", "exchange_context_event_count", "latest_exchange_context_known_on"],
            ascending=[True, False, False, False],
            na_position="last",
            kind="stable",
        )
        .drop_duplicates(subset=["symbol"], keep="first")
        .reset_index(drop=True)
    )
    df["exchange_context_reliability_classification"] = reliability_classification or pd.NA
    df["exchange_context_priority_policy"] = reliability_policy
    for column in ["latest_exchange_context_known_on", "latest_exchange_context_asof_date"]:
        df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    return df


def load_monitored_universe(asof_date: pd.Timestamp | None = None) -> pd.DataFrame:
    watchlist = load_active_watchlist(asof_date=asof_date)
    positions = load_open_positions(asof_date=asof_date)
    if watchlist.empty and positions.empty:
        return pd.DataFrame()
    if watchlist.empty:
        out = positions.copy()
    elif positions.empty:
        out = watchlist.copy()
    else:
        watchlist = watchlist.copy()
        positions = positions.copy()
        watchlist["_priority"] = 1
        positions["_priority"] = 2
        out = pd.concat([watchlist, positions], ignore_index=True, sort=False)
        out = out.sort_values(
            ["symbol", "_priority", "rank", "state_updated_at", "setup_id"],
            ascending=[True, False, True, False, True],
            kind="stable",
        )
        out = out.drop_duplicates(subset=["symbol"], keep="first")
        out = out.drop(columns=["_priority"], errors="ignore")
    if "rank" not in out.columns:
        out["rank"] = pd.NA
    out["rank"] = pd.to_numeric(out["rank"], errors="coerce")
    if "state_updated_at" not in out.columns:
        out["state_updated_at"] = pd.NaT
    out["state_updated_at"] = pd.to_datetime(out["state_updated_at"], utc=True, errors="coerce")
    priority = load_exchange_context_priorities(out["symbol"].dropna().astype(str).str.upper().tolist(), asof_date=asof_date)
    if not priority.empty:
        out = out.merge(priority, on="symbol", how="left")
    if "exchange_context_priority_score" not in out.columns:
        out["exchange_context_priority_score"] = 0.0
    out["exchange_context_priority_score"] = pd.to_numeric(out["exchange_context_priority_score"], errors="coerce").fillna(0.0)
    if "exchange_context_event_count" not in out.columns:
        out["exchange_context_event_count"] = 0
    out["exchange_context_event_count"] = pd.to_numeric(out["exchange_context_event_count"], errors="coerce").fillna(0).astype("int64")
    out["_exchange_context_priority_active"] = out["exchange_context_priority_score"] > 0
    out["exchange_context_priority_reason"] = out["exchange_context_priority_score"].map(
        lambda value: "fresh_exchange_event_activity" if float(value or 0.0) > 0 else "normal_watch_order"
    )
    out = out.sort_values(
        [
            "_exchange_context_priority_active",
            "exchange_context_priority_score",
            "exchange_context_event_count",
            "symbol",
            "monitor_source",
            "rank",
            "state_updated_at",
            "setup_id",
        ],
        ascending=[False, False, False, True, True, True, False, True],
        kind="stable",
    )
    return out.drop(columns=["_exchange_context_priority_active"], errors="ignore").drop_duplicates(subset=["symbol"], keep="first").reset_index(drop=True)


def load_latest_intraday_prices(symbols: list[str], *, interval_minutes: int = 1) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()
    df = sql_to_df(
        """
        SELECT DISTINCT ON (ticker)
            ticker AS symbol,
            timestamp,
            close
        FROM dhan_ohlcv_intraday
        WHERE interval_minutes = %s
          AND exchange = 'NSE'
          AND ticker = ANY(%s)
        ORDER BY ticker, timestamp DESC
        """,
        params=(int(interval_minutes), [str(value).upper() for value in symbols]),
    )
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    df["symbol"] = df["symbol"].astype("string").str.upper()
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df


def build_price_alerts(watchlist: pd.DataFrame, latest_prices: pd.DataFrame, *, observed_at: pd.Timestamp | None = None) -> pd.DataFrame:
    if watchlist.empty or latest_prices.empty:
        return pd.DataFrame()
    merged = watchlist.merge(latest_prices, how="left", on="symbol")
    observed = pd.to_datetime(observed_at or pd.Timestamp.utcnow(), utc=True, errors="coerce")
    rows: list[dict[str, Any]] = []
    for _, row in merged.iterrows():
        last_price = pd.to_numeric(row.get("close"), errors="coerce")
        low = pd.to_numeric(row.get("attractive_price_low"), errors="coerce")
        high = pd.to_numeric(row.get("attractive_price_high"), errors="coerce")
        stop_price = pd.to_numeric(row.get("stop_price"), errors="coerce")
        invalidation = pd.to_numeric(row.get("invalidation_price"), errors="coerce")
        current_state = str(row.get("current_state") or row.get("candidate_state") or "")
        monitor_source = str(row.get("monitor_source") or "watchlist").lower()
        if monitor_source == "position":
            if pd.notna(last_price) and pd.notna(invalidation) and float(last_price) <= float(invalidation):
                rows.append(
                    {
                        "observed_at": observed,
                        "asof_date": row.get("asof_date"),
                        "setup_id": row.get("setup_id"),
                        "symbol": row.get("symbol"),
                        "alert_type": "POSITION_INVALIDATION_HIT",
                        "alert_reason": f"last_price={float(last_price):.2f} is below position invalidation",
                        "monitor_source": monitor_source,
                        "last_price": float(last_price),
                        "attractive_price_low": None if pd.isna(low) else float(low),
                        "attractive_price_high": None if pd.isna(high) else float(high),
                        "stop_price": None if pd.isna(stop_price) else float(stop_price),
                        "invalidation_price": None if pd.isna(invalidation) else float(invalidation),
                        "current_state": current_state,
                        "load_ts": pd.Timestamp.utcnow(),
                    }
                )
            elif pd.notna(last_price) and pd.notna(stop_price) and float(last_price) <= float(stop_price):
                rows.append(
                    {
                        "observed_at": observed,
                        "asof_date": row.get("asof_date"),
                        "setup_id": row.get("setup_id"),
                        "symbol": row.get("symbol"),
                        "alert_type": "STOP_HIT",
                        "alert_reason": f"last_price={float(last_price):.2f} is below stop guidance",
                        "monitor_source": monitor_source,
                        "last_price": float(last_price),
                        "attractive_price_low": None if pd.isna(low) else float(low),
                        "attractive_price_high": None if pd.isna(high) else float(high),
                        "stop_price": None if pd.isna(stop_price) else float(stop_price),
                        "invalidation_price": None if pd.isna(invalidation) else float(invalidation),
                        "current_state": current_state,
                        "load_ts": pd.Timestamp.utcnow(),
                    }
                )
            continue
        if pd.notna(last_price) and pd.notna(low) and pd.notna(high) and float(low) <= float(last_price) <= float(high):
            rows.append(
                {
                    "observed_at": observed,
                    "asof_date": row.get("asof_date"),
                    "setup_id": row.get("setup_id"),
                    "symbol": row.get("symbol"),
                    "alert_type": "ENTRY_ZONE_HIT",
                    "alert_reason": f"last_price={float(last_price):.2f} is within attractive range",
                    "monitor_source": monitor_source,
                    "last_price": float(last_price),
                    "attractive_price_low": None if pd.isna(low) else float(low),
                    "attractive_price_high": None if pd.isna(high) else float(high),
                    "stop_price": None if pd.isna(stop_price) else float(stop_price),
                    "invalidation_price": None if pd.isna(invalidation) else float(invalidation),
                    "current_state": current_state,
                    "load_ts": pd.Timestamp.utcnow(),
                }
            )
        if pd.notna(last_price) and pd.notna(invalidation) and float(last_price) <= float(invalidation):
            rows.append(
                {
                    "observed_at": observed,
                    "asof_date": row.get("asof_date"),
                    "setup_id": row.get("setup_id"),
                    "symbol": row.get("symbol"),
                    "alert_type": "INVALIDATION_HIT",
                    "alert_reason": f"last_price={float(last_price):.2f} is below invalidation",
                    "monitor_source": monitor_source,
                    "last_price": float(last_price),
                    "attractive_price_low": None if pd.isna(low) else float(low),
                    "attractive_price_high": None if pd.isna(high) else float(high),
                    "stop_price": None if pd.isna(stop_price) else float(stop_price),
                    "invalidation_price": None if pd.isna(invalidation) else float(invalidation),
                    "current_state": current_state,
                    "load_ts": pd.Timestamp.utcnow(),
                }
            )
        if current_state in {"WATCH_BREAKOUT", "WATCH_PULLBACK", "WATCH_EVENT"} and pd.notna(last_price) and pd.notna(high) and float(last_price) > float(high):
            rows.append(
                {
                    "observed_at": observed,
                    "asof_date": row.get("asof_date"),
                    "setup_id": row.get("setup_id"),
                    "symbol": row.get("symbol"),
                    "alert_type": "BREAKOUT_ABOVE_RANGE",
                    "alert_reason": f"last_price={float(last_price):.2f} is above attractive range",
                    "monitor_source": monitor_source,
                    "last_price": float(last_price),
                    "attractive_price_low": None if pd.isna(low) else float(low),
                    "attractive_price_high": None if pd.isna(high) else float(high),
                    "stop_price": None if pd.isna(stop_price) else float(stop_price),
                    "invalidation_price": None if pd.isna(invalidation) else float(invalidation),
                    "current_state": current_state,
                    "load_ts": pd.Timestamp.utcnow(),
                }
            )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).drop_duplicates(subset=["symbol", "alert_type"], keep="last")


def build_announcement_alerts(
    events: pd.DataFrame,
    *,
    observed_at: pd.Timestamp | None = None,
    max_alerts: int | None = None,
    max_age_hours: int | None = None,
) -> pd.DataFrame:
    """Review-only alerts for material exchange filings surfaced by the intraday
    announcement ingest.

    A material filing on a monitored symbol becomes visible within the day instead of
    only after the EOD `evaluate` stage. Materiality uses the deterministic pre-LLM
    keyword signal (``is_material_context_event``). These alerts are informational
    review signals only: they never confer BUY/SELL or broker authority.
    """
    if events is None or events.empty:
        return pd.DataFrame()
    observed = pd.to_datetime(observed_at or pd.Timestamp.utcnow(), utc=True, errors="coerce")
    working = events.copy()
    if "published_on" in working.columns:
        working["published_on"] = pd.to_datetime(working["published_on"], utc=True, errors="coerce")
        age_hours = int(max_age_hours if max_age_hours is not None else WATCHER_ANNOUNCEMENT_ALERT_MAX_AGE_HOURS)
        if age_hours > 0:
            cutoff = observed - pd.Timedelta(hours=age_hours)
            working = working[working["published_on"].isna() | (working["published_on"] >= cutoff)]
    if working.empty:
        return pd.DataFrame()
    material = working[working.apply(is_material_context_event, axis=1)].copy()
    if material.empty:
        return pd.DataFrame()
    if "published_on" in material.columns:
        material = material.sort_values("published_on", ascending=False, na_position="last")
    subset = [column for column in ["symbol", "unique_id"] if column in material.columns]
    if subset:
        material = material.drop_duplicates(subset=subset, keep="first")
    cap = int(max_alerts if max_alerts is not None else WATCHER_ANNOUNCEMENT_ALERT_MAX)
    if cap > 0:
        material = material.head(cap)
    rows: list[dict[str, Any]] = []
    for _, row in material.iterrows():
        subject = str(row.get("subject") or "").strip()
        category = str(row.get("filed_under_category") or "").strip()
        published_on = pd.to_datetime(row.get("published_on"), utc=True, errors="coerce")
        detail = " ".join(bit for bit in [subject, f"[{category}]" if category else ""] if bit)
        reason = f"Material filing: {detail}" if detail else "Material exchange filing"
        if not pd.isna(published_on):
            reason = f"{reason} (filed {published_on.strftime('%Y-%m-%d %H:%M')} UTC)"
        rows.append(
            {
                "observed_at": observed,
                "asof_date": row.get("asof_date"),
                "setup_id": row.get("setup_id"),
                "symbol": str(row.get("symbol") or "").upper(),
                "alert_type": "MATERIAL_ANNOUNCEMENT",
                "alert_reason": reason[:500],
                "monitor_source": str(row.get("monitor_source") or "announcement").lower(),
                "current_state": str(row.get("event_status") or "REVIEW").upper(),
                "alert_fingerprint_key": str(row.get("unique_id") or "").strip(),
                "load_ts": pd.Timestamp.utcnow(),
            }
        )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def _alert_fingerprint(row: pd.Series | dict[str, Any]) -> str:
    get = row.get if isinstance(row, dict) else row.get
    parts = [
        str(get("setup_id") or "").strip().upper(),
        str(get("symbol") or "").strip().upper(),
        str(get("alert_type") or "").strip().upper(),
        str(get("monitor_source") or "").strip().lower(),
        str(get("current_state") or "").strip().upper(),
    ]
    # Optional per-item key (e.g. a specific filing's unique_id) so distinct events for
    # the same symbol/alert_type each alert once instead of colliding on the cooldown.
    # Absent for price alerts, keeping their fingerprint byte-identical to before.
    extra = str(get("alert_fingerprint_key") or "").strip()
    if extra:
        parts.append(extra)
    return "|".join(parts)


def _load_recent_alert_fingerprints(*, cutoff: pd.Timestamp) -> set[str]:
    df = sql_to_df(
        f"""
        SELECT DISTINCT
            COALESCE(
                alert_fingerprint,
                UPPER(COALESCE(setup_id, '')) || '|' ||
                UPPER(COALESCE(symbol, '')) || '|' ||
                UPPER(COALESCE(alert_type, '')) || '|' ||
                LOWER(COALESCE(monitor_source, '')) || '|' ||
                UPPER(COALESCE(current_state, ''))
            ) AS alert_fingerprint
        FROM {ALERTS_TABLE}
        WHERE observed_at >= %s
        """,
        params=(cutoff,),
    )
    if df.empty or "alert_fingerprint" not in df.columns:
        return set()
    return {
        str(value).strip()
        for value in df["alert_fingerprint"].dropna().tolist()
        if str(value).strip()
    }


def persist_alerts(df: pd.DataFrame, *, cooldown_seconds: int | None = None) -> dict[str, Any]:
    ensure_alerts_table()
    if df.empty:
        return {
            "input_count": 0,
            "persisted_count": 0,
            "suppressed_count": 0,
            "cooldown_seconds": int(cooldown_seconds if cooldown_seconds is not None else DEFAULT_ALERT_COOLDOWN_SECONDS),
        }
    out = df.copy()
    for column in [
        "last_price",
        "attractive_price_low",
        "attractive_price_high",
        "stop_price",
        "invalidation_price",
    ]:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce")
    for column in ["observed_at", "asof_date", "load_ts"]:
        if column in out.columns:
            out[column] = pd.to_datetime(out[column], utc=True, errors="coerce")
    out["alert_fingerprint"] = out.apply(_alert_fingerprint, axis=1)
    # Helper-only column used for fingerprinting; not a persisted alert column.
    out = out.drop(columns=["alert_fingerprint_key"], errors="ignore")
    input_count = int(len(out))
    effective_cooldown = max(0, int(cooldown_seconds if cooldown_seconds is not None else DEFAULT_ALERT_COOLDOWN_SECONDS))
    recent_fingerprints: set[str] = set()
    if effective_cooldown > 0:
        observed_max = pd.to_datetime(out["observed_at"], utc=True, errors="coerce").max() if "observed_at" in out.columns else pd.Timestamp.utcnow()
        if pd.isna(observed_max):
            observed_max = pd.Timestamp.utcnow()
        cutoff = pd.Timestamp(observed_max) - pd.Timedelta(seconds=effective_cooldown)
        try:
            recent_fingerprints = _load_recent_alert_fingerprints(cutoff=cutoff)
        except Exception as exc:
            record_local_fallback_event(
                module="advisory.continuous_watch",
                source=ALERTS_TABLE,
                fallback_type="continuous_watch_alert_dedupe_lookup_failed",
                severity="warn",
                reason="Continuous watch could not load recent alert fingerprints; alerts will be persisted fail-open to avoid missing evidence.",
                error=exc,
                metadata={"cooldown_seconds": effective_cooldown, "input_count": input_count},
            )
            recent_fingerprints = set()
    if recent_fingerprints:
        keep_mask = ~out["alert_fingerprint"].isin(recent_fingerprints)
        suppressed_count = int((~keep_mask).sum())
        out = out.loc[keep_mask].copy()
    else:
        suppressed_count = 0
    persisted_count = int(len(out))
    if out.empty:
        publish_bus_message(
            "stockey:continuous_watch:alerts",
            {
                "published_at": pd.Timestamp.utcnow(),
                "alert_count": 0,
                "input_count": input_count,
                "persisted_count": 0,
                "suppressed_count": suppressed_count,
                "cooldown_seconds": effective_cooldown,
                "alerts": [],
            },
        )
        return {
            "input_count": input_count,
            "persisted_count": 0,
            "suppressed_count": suppressed_count,
            "cooldown_seconds": effective_cooldown,
        }
    upsert_to_db(out, ALERTS_TABLE, unique_keys=["observed_at", "setup_id", "symbol", "alert_type"], timescaledb_column="observed_at")
    publish_bus_message(
        "stockey:continuous_watch:alerts",
        {
            "published_at": pd.Timestamp.utcnow(),
            "alert_count": persisted_count,
            "input_count": input_count,
            "persisted_count": persisted_count,
            "suppressed_count": suppressed_count,
            "cooldown_seconds": effective_cooldown,
            "alerts": out.head(25).to_dict(orient="records"),
        },
    )
    return {
        "input_count": input_count,
        "persisted_count": persisted_count,
        "suppressed_count": suppressed_count,
        "cooldown_seconds": effective_cooldown,
    }


def _is_due(source_name: str, interval_seconds: int) -> bool:
    state = load_sync_state(source_name)
    if state is None:
        return True
    last_success_at = pd.to_datetime(state.get("last_success_at"), utc=True, errors="coerce")
    if pd.isna(last_success_at):
        return True
    return (pd.Timestamp.utcnow() - last_success_at).total_seconds() >= int(interval_seconds)


def _next_cursor_after_pull(previous_cursor: pd.Timestamp | None, requested_from: pd.Timestamp, candidate_latest: object) -> pd.Timestamp:
    previous = pd.to_datetime(previous_cursor, utc=True, errors="coerce")
    requested = pd.to_datetime(requested_from, utc=True, errors="coerce")
    candidate = pd.to_datetime(candidate_latest, utc=True, errors="coerce")
    if pd.notna(candidate) and (pd.isna(previous) or candidate > previous):
        return pd.Timestamp(candidate)
    if pd.notna(previous):
        return pd.Timestamp(previous)
    return pd.Timestamp(requested)


def _bounded_intraday_from_cursor(from_cursor: pd.Timestamp, to_cursor: pd.Timestamp, max_lookback_minutes: int) -> tuple[pd.Timestamp, bool]:
    max_lookback = max(1, int(max_lookback_minutes))
    earliest_allowed = pd.Timestamp(to_cursor) - pd.Timedelta(minutes=max_lookback)
    if pd.Timestamp(from_cursor) < earliest_allowed:
        return earliest_allowed, True
    return pd.Timestamp(from_cursor), False


def _bounded_event_from_cursor(
    previous_cursor: pd.Timestamp | None,
    to_cursor: pd.Timestamp,
    *,
    initial_lookback_minutes: int,
    replay_minutes: int,
    max_lookback_minutes: int,
) -> tuple[pd.Timestamp, bool]:
    to_ts = pd.to_datetime(to_cursor, utc=True, errors="coerce")
    if pd.isna(to_ts):
        to_ts = pd.Timestamp.utcnow()
    previous = pd.to_datetime(previous_cursor, utc=True, errors="coerce")
    if pd.isna(previous):
        from_cursor = to_ts - pd.Timedelta(minutes=max(1, int(initial_lookback_minutes)))
    else:
        from_cursor = pd.Timestamp(previous) - pd.Timedelta(minutes=max(0, int(replay_minutes)))
    earliest_allowed = to_ts - pd.Timedelta(minutes=max(1, int(max_lookback_minutes)))
    if from_cursor < earliest_allowed:
        return earliest_allowed, True
    return pd.Timestamp(from_cursor), False


def _safe_count(value: Any) -> int:
    try:
        parsed = pd.to_numeric(value, errors="coerce")
        if pd.isna(parsed):
            return 0
        return int(parsed)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.continuous_watch",
            source="watcher_source_counters",
            fallback_type="watcher_count_parse_failed",
            severity="warn",
            reason="Continuous watcher could not parse a source counter and used zero.",
            error=exc,
            metadata={"value": str(value)[:200]},
        )
        return 0


def _watcher_substep(name: str, status: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "status": status, "detail": detail, **extra}


def _watcher_empty_reason(source: str, counters: dict[str, Any]) -> tuple[str | None, list[dict[str, Any]]]:
    source = str(source or "").lower()
    substeps: list[dict[str, Any]] = []
    if source == "ohlcv":
        symbol_count = _safe_count(counters.get("symbol_count"))
        latest_price_count = _safe_count(counters.get("latest_price_count"))
        sync_failure_count = _safe_count(counters.get("sync_failure_count"))
        alert_input_count = _safe_count(counters.get("alert_input_count"))
        alert_persisted_count = _safe_count(counters.get("alert_persisted_count"))
        alert_suppressed_count = _safe_count(counters.get("alert_suppressed_count"))
        substeps.extend(
            [
                _watcher_substep("watchlist", "ok" if symbol_count else "empty", "Monitored symbols loaded.", count=symbol_count),
                _watcher_substep(
                    "dhan_intraday_sync",
                    "warn" if sync_failure_count else "ok",
                    "Intraday sync completed with source-level failures." if sync_failure_count else "Intraday sync completed without reported source failures.",
                    failure_count=sync_failure_count,
                    result_count=_safe_count(counters.get("sync_result_count")),
                ),
                _watcher_substep("latest_prices", "ok" if latest_price_count else "empty", "Latest intraday prices loaded.", count=latest_price_count),
                _watcher_substep(
                    "price_alerts",
                    "ok" if alert_persisted_count else "empty",
                    "Price alerts persisted." if alert_persisted_count else "No new price alerts persisted.",
                    input_count=alert_input_count,
                    persisted_count=alert_persisted_count,
                    suppressed_count=alert_suppressed_count,
                ),
            ]
        )
        if symbol_count == 0:
            return "no_watchlist_symbols", substeps
        if latest_price_count == 0:
            return "no_latest_intraday_prices", substeps
        if alert_input_count == 0:
            return "no_price_alerts_generated", substeps
        if alert_persisted_count == 0 and alert_suppressed_count > 0:
            return "alerts_suppressed_by_cooldown", substeps
        if alert_persisted_count == 0:
            return "no_price_alerts_persisted", substeps
        return None, substeps
    if source == "news":
        watch_count = _safe_count(counters.get("watch_count"))
        item_count = _safe_count(counters.get("news_item_count"))
        matched_count = _safe_count(counters.get("matched_event_count"))
        triggered_count = _safe_count(counters.get("triggered_event_count"))
        observed_count = _safe_count(counters.get("context_observed_count"))
        persisted_count = _safe_count(counters.get("persisted_event_count"))
        substeps.extend(
            [
                _watcher_substep("watch_targets", "ok" if watch_count else "empty", "News watch targets loaded.", count=watch_count),
                _watcher_substep("feed_items", "ok" if item_count else "empty", "News feed items loaded.", count=item_count),
                _watcher_substep("symbol_match", "ok" if matched_count else "empty", "News items matched watched symbols.", count=matched_count),
                _watcher_substep(
                    "materiality",
                    "ok" if triggered_count else "context_only" if observed_count else "empty",
                    "News matches produced triggered events." if triggered_count else "News matches were context-only or non-material." if observed_count else "No material news matches.",
                    triggered_count=triggered_count,
                    context_observed_count=observed_count,
                ),
                _watcher_substep("persistence", "ok" if persisted_count else "empty", "News events persisted.", count=persisted_count),
            ]
        )
        if watch_count == 0:
            return "no_news_watch_targets", substeps
        if item_count == 0:
            return "no_news_feed_items", substeps
        if matched_count == 0:
            return "no_news_symbol_matches", substeps
        if triggered_count == 0 and observed_count > 0:
            return "context_observed_only", substeps
        if persisted_count == 0:
            return "no_news_events_persisted", substeps
        return None, substeps
    if source == "announcements":
        watch_count = _safe_count(counters.get("watch_count"))
        targets = _safe_count(counters.get("unique_ingest_targets"))
        discovered = _safe_count(counters.get("discovered_count"))
        parsed = _safe_count(counters.get("parsed_count"))
        failed = _safe_count(counters.get("failed_count"))
        issue_count = _safe_count(counters.get("ingest_issue_count"))
        skipped_targets = _safe_count(counters.get("skipped_ingest_target_count"))
        unresolved_targets = _safe_count(counters.get("unresolved_ingest_target_count"))
        unresolved_watch_rows = _safe_count(counters.get("unresolved_watch_row_count"))
        managed_ingest_failed = _safe_count(counters.get("managed_ingest_failed_count"))
        document_lookup_failed = _safe_count(counters.get("document_lookup_failed_count"))
        degraded_total = issue_count + unresolved_targets + unresolved_watch_rows + managed_ingest_failed + document_lookup_failed
        match_count = _safe_count(counters.get("match_count"))
        triggered_count = _safe_count(counters.get("triggered_event_count"))
        observed_count = _safe_count(counters.get("context_observed_count"))
        persisted_count = _safe_count(counters.get("persisted_event_count"))
        substeps.extend(
            [
                _watcher_substep("watch_targets", "ok" if watch_count else "empty", "Announcement watch targets loaded.", count=watch_count),
                _watcher_substep("ingest_targets", "ok" if targets else "empty", "Unique announcement ingest targets prepared.", count=targets),
                _watcher_substep(
                    "nse_ingest",
                    "warn" if failed else "backlog" if skipped_targets else "ok" if discovered or parsed else "empty",
                    "Announcement ingest had failed items." if failed else "Announcement ingest processed a bounded subset; skipped targets remain due." if skipped_targets else "Announcement ingest completed.",
                    discovered_count=discovered,
                    parsed_count=parsed,
                    failed_count=failed,
                    skipped_ingest_target_count=skipped_targets,
                    total_ingest_targets=_safe_count(counters.get("total_ingest_targets")),
                    processed_ingest_targets=_safe_count(counters.get("processed_ingest_targets")),
                    max_ingest_targets=_safe_count(counters.get("max_ingest_targets")),
                ),
                _watcher_substep(
                    "degraded_evidence",
                    "warn" if degraded_total else "ok",
                    "Announcement evidence was degraded by unresolved mappings or lookup/ingest issues."
                    if degraded_total
                    else "Announcement evidence had no reported degraded-ingest counters.",
                    ingest_issue_count=issue_count,
                    unresolved_ingest_target_count=unresolved_targets,
                    unresolved_watch_row_count=unresolved_watch_rows,
                    managed_ingest_failed_count=managed_ingest_failed,
                    document_lookup_failed_count=document_lookup_failed,
                ),
                _watcher_substep("symbol_match", "ok" if match_count else "empty", "Announcements matched watched symbols.", count=match_count),
                _watcher_substep(
                    "materiality",
                    "ok" if triggered_count else "context_only" if observed_count else "empty",
                    "Announcement matches produced triggered events." if triggered_count else "Announcement matches were context-only or non-material." if observed_count else "No material announcement matches.",
                    triggered_count=triggered_count,
                    context_observed_count=observed_count,
                ),
                _watcher_substep("persistence", "ok" if persisted_count else "empty", "Announcement watch outputs persisted.", count=persisted_count),
            ]
        )
        if watch_count == 0:
            return "no_announcement_watch_targets", substeps
        if targets == 0:
            return "no_announcement_ingest_targets", substeps
        if discovered == 0 and parsed == 0 and failed == 0:
            return "no_announcements_discovered", substeps
        if failed > 0 and parsed == 0:
            return "announcement_ingest_failed", substeps
        if degraded_total > 0 and match_count == 0:
            return "announcement_evidence_degraded", substeps
        if match_count == 0:
            return "no_announcement_symbol_matches", substeps
        if triggered_count == 0 and observed_count > 0:
            return "context_observed_only", substeps
        if persisted_count == 0:
            return "no_announcement_events_persisted", substeps
        return None, substeps
    return None, substeps


def watcher_source_counters(
    *,
    source: str,
    meta: dict[str, Any] | None = None,
    events: pd.DataFrame | None = None,
    watch_updates: pd.DataFrame | None = None,
    latest_prices: pd.DataFrame | None = None,
    alerts: pd.DataFrame | None = None,
    sync_results: list[dict[str, Any]] | None = None,
    alert_persist: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = meta if isinstance(meta, dict) else {}
    counters: dict[str, Any] = {"source": source}
    if source == "ohlcv":
        sync_results = sync_results or []
        failed_sync = [
            row for row in sync_results
            if isinstance(row, dict) and str(row.get("status") or "").lower() not in {"ok", "success", "skipped_no_data", "no_data", ""}
        ]
        counters.update(
            {
                "symbol_count": _safe_count(meta.get("symbol_count")),
                "sync_result_count": len(sync_results),
                "sync_failure_count": len(failed_sync),
                "latest_price_count": 0 if latest_prices is None else int(len(latest_prices)),
                "alert_input_count": _safe_count((alert_persist or {}).get("input_count") if alert_persist else (0 if alerts is None else len(alerts))),
                "alert_persisted_count": _safe_count((alert_persist or {}).get("persisted_count")),
                "alert_suppressed_count": _safe_count((alert_persist or {}).get("suppressed_count")),
                "alert_cooldown_seconds": _safe_count((alert_persist or {}).get("cooldown_seconds") or DEFAULT_ALERT_COOLDOWN_SECONDS),
            }
        )
    elif source == "news":
        counters.update(
            {
                "watch_count": _safe_count(meta.get("watch_count")),
                "market_context_watch_count": _safe_count(meta.get("market_context_watch_count")),
                "theme_context_watch_count": _safe_count(meta.get("theme_context_watch_count")),
                "announcement_context_watch_count": _safe_count(meta.get("announcement_context_watch_count")),
                "bhavcopy_context_watch_count": _safe_count(meta.get("bhavcopy_context_watch_count")),
                "macro_context_watch_count": _safe_count(meta.get("macro_context_watch_count")),
                "news_item_count": _safe_count(meta.get("news_item_count")),
                "matched_event_count": _safe_count(meta.get("matched_event_count")),
                "triggered_event_count": _safe_count(meta.get("triggered_event_count")),
                "context_observed_count": _safe_count(meta.get("context_observed_count")),
                "persisted_event_count": 0 if events is None else int(len(events)),
                "feed_count": len(meta.get("feed_names") or []) if isinstance(meta.get("feed_names"), list) else 0,
            }
        )
    elif source == "announcements":
        ingest_runs = meta.get("ingest_runs") if isinstance(meta.get("ingest_runs"), list) else []
        failed = sum(_safe_count(row.get("failed")) for row in ingest_runs if isinstance(row, dict))
        discovered = sum(_safe_count(row.get("discovered")) for row in ingest_runs if isinstance(row, dict))
        parsed = sum(_safe_count(row.get("parsed")) for row in ingest_runs if isinstance(row, dict))
        issue_count = sum(_safe_count(row.get("issue_count")) for row in ingest_runs if isinstance(row, dict))
        counters.update(
            {
                "watch_count": _safe_count(meta.get("watch_count")),
                "unique_ingest_targets": _safe_count(meta.get("unique_ingest_targets")),
                "ingest_run_count": len(ingest_runs),
                "discovered_count": discovered,
                "parsed_count": parsed,
                "failed_count": failed,
                "ingest_issue_count": max(issue_count, _safe_count(meta.get("ingest_issue_count"))),
                "total_ingest_targets": _safe_count(meta.get("total_ingest_targets") or meta.get("unique_ingest_targets")),
                "processed_ingest_targets": _safe_count(meta.get("processed_ingest_targets") or meta.get("unique_ingest_targets")),
                "skipped_ingest_target_count": _safe_count(meta.get("skipped_ingest_target_count")),
                "max_ingest_targets": _safe_count(meta.get("max_ingest_targets")),
                "unresolved_ingest_target_count": _safe_count(meta.get("unresolved_ingest_target_count")),
                "unresolved_watch_row_count": _safe_count(meta.get("unresolved_watch_row_count")),
                "managed_ingest_failed_count": _safe_count(meta.get("managed_ingest_failed_count")),
                "document_lookup_failed_count": _safe_count(meta.get("document_lookup_failed_count")),
                "match_count": _safe_count(meta.get("match_count")),
                "triggered_event_count": _safe_count(meta.get("triggered_event_count")),
                "context_observed_count": _safe_count(meta.get("context_observed_count")),
                "watch_update_count": 0 if watch_updates is None else int(len(watch_updates)),
                "persisted_event_count": 0 if events is None else int(len(events)),
                "market_context_watch_count": _safe_count(meta.get("market_context_watch_count")),
                "theme_context_watch_count": _safe_count(meta.get("theme_context_watch_count")),
                "announcement_context_watch_count": _safe_count(meta.get("announcement_context_watch_count")),
                "bhavcopy_context_watch_count": _safe_count(meta.get("bhavcopy_context_watch_count")),
                "macro_context_watch_count": _safe_count(meta.get("macro_context_watch_count")),
                "capped_watch_rows": _safe_count(meta.get("capped_watch_rows")),
            }
        )
    else:
        counters.update({key: _safe_count(value) for key, value in meta.items() if isinstance(value, (int, float))})
    empty_reason, substeps = _watcher_empty_reason(source, counters)
    counters["empty_reason"] = empty_reason
    counters["substeps"] = substeps
    return counters


def run_ohlcv_cycle(
    *,
    interval_seconds: int,
    intraday_interval_minutes: int = 1,
    initial_lookback_minutes: int = 120,
    max_lookback_minutes: int | None = None,
) -> dict[str, Any]:
    source_name = "continuous_watch:ohlcv"
    watchlist = load_monitored_universe()
    if watchlist.empty:
        persist_sync_state(source_name=source_name, status="ok", state={"reason": "no_watchlist_symbols"}, last_success_at=pd.Timestamp.utcnow())
        return {"status": "ok", "symbol_count": 0, "sync_results": [], "alert_count": 0}
    # Intraday sync = active tier + open positions only (operational attention, not
    # authority). Shelf names ride daily bars via the nightly reconcile; rows without a
    # tier (pre-tier builds) stay synced (fail-open to previous behavior).
    total_universe = int(watchlist["symbol"].nunique())
    if "watch_tier" in watchlist.columns:
        tier = watchlist["watch_tier"].astype("string").str.lower()
        is_position = watchlist.get("monitor_source", pd.Series("", index=watchlist.index)).astype("string").str.lower().eq("position")
        watchlist = watchlist[tier.isna() | tier.ne("shelf") | is_position]
    symbols = sorted(watchlist["symbol"].dropna().astype(str).str.upper().unique().tolist())
    if len(symbols) < total_universe:
        _emit(
            f"[advisory.continuous_watch] ohlcv tier gate: syncing {len(symbols)}/{total_universe} symbols "
            "(active tier + positions); shelf rides daily bars"
        )
    state = load_sync_state(source_name) or {}
    previous_cursor = pd.to_datetime(state.get("last_item_ts"), utc=True, errors="coerce")
    if pd.isna(previous_cursor):
        from_cursor = pd.Timestamp.utcnow() - pd.Timedelta(minutes=int(initial_lookback_minutes))
    else:
        from_cursor = previous_cursor - pd.Timedelta(minutes=5)
    to_cursor = pd.Timestamp.utcnow()
    effective_max_lookback = int(max_lookback_minutes or os.getenv("WATCHER_OHLCV_MAX_LOOKBACK_MINUTES", "240"))
    from_cursor, catchup_truncated = _bounded_intraday_from_cursor(from_cursor, to_cursor, effective_max_lookback)
    if catchup_truncated:
        _emit(
            "[advisory.continuous_watch] ohlcv catchup truncated "
            f"symbols={len(symbols)} max_lookback_minutes={effective_max_lookback} "
            f"from={from_cursor.isoformat()} to={to_cursor.isoformat()}"
        )
    _emit(f"[advisory.continuous_watch] ohlcv start symbols={len(symbols)} from={from_cursor.isoformat()} to={to_cursor.isoformat()}")
    sync_results = sync_many_intraday(
        symbols,
        exchange="NSE",
        asset_type="stock",
        interval_minutes=int(intraday_interval_minutes),
        from_date=from_cursor.to_pydatetime(),
        to_date=to_cursor.to_pydatetime(),
    )
    latest_prices = load_latest_intraday_prices(symbols, interval_minutes=intraday_interval_minutes)
    alerts = build_price_alerts(watchlist, latest_prices, observed_at=to_cursor)
    alert_persist = persist_alerts(alerts)
    source_counters = watcher_source_counters(
        source="ohlcv",
        meta={"symbol_count": len(symbols)},
        latest_prices=latest_prices,
        alerts=alerts,
        sync_results=sync_results,
        alert_persist=alert_persist,
    )
    candidate_latest = latest_prices["timestamp"].max() if not latest_prices.empty else pd.NaT
    last_item_ts = _next_cursor_after_pull(previous_cursor, from_cursor, candidate_latest)
    persist_sync_state(
        source_name=source_name,
        last_success_at=to_cursor,
        last_item_ts=last_item_ts,
        cursor_value=None if pd.isna(last_item_ts) else pd.Timestamp(last_item_ts).isoformat(),
        state={
            "symbol_count": len(symbols),
            "interval_minutes": int(intraday_interval_minutes),
            "max_lookback_minutes": effective_max_lookback,
            "catchup_truncated": bool(catchup_truncated),
            "alert_input_count": int(alert_persist.get("input_count") or 0),
            "alert_persisted_count": int(alert_persist.get("persisted_count") or 0),
            "alert_suppressed_count": int(alert_persist.get("suppressed_count") or 0),
            "alert_cooldown_seconds": int(alert_persist.get("cooldown_seconds") or DEFAULT_ALERT_COOLDOWN_SECONDS),
            "source_counters": source_counters,
        },
        status="ok",
    )
    result = {
        "status": "ok",
        "symbol_count": len(symbols),
        "sync_results": sync_results,
        "alert_count": int(len(alerts)),
        "alert_input_count": int(alert_persist.get("input_count") or 0),
        "alert_persisted_count": int(alert_persist.get("persisted_count") or 0),
        "alert_suppressed_count": int(alert_persist.get("suppressed_count") or 0),
        "alert_cooldown_seconds": int(alert_persist.get("cooldown_seconds") or DEFAULT_ALERT_COOLDOWN_SECONDS),
        "source_counters": source_counters,
        "catchup_truncated": bool(catchup_truncated),
        "max_lookback_minutes": effective_max_lookback,
    }
    publish_bus_message("stockey:continuous_watch:ohlcv", {"published_at": pd.Timestamp.utcnow(), **result})
    return result


def run_news_cycle(*, interval_seconds: int, lookback_minutes: int = 90, max_lookback_minutes: int | None = None) -> dict[str, Any]:
    source_name = "continuous_watch:news"
    now = pd.Timestamp.utcnow()
    state = load_sync_state(source_name) or {}
    previous_cursor = pd.to_datetime(state.get("last_item_ts"), utc=True, errors="coerce")
    effective_max_lookback = int(max_lookback_minutes or os.getenv("WATCHER_NEWS_MAX_LOOKBACK_MINUTES", "1440"))
    published_from, catchup_truncated = _bounded_event_from_cursor(
        previous_cursor,
        now,
        initial_lookback_minutes=int(lookback_minutes),
        replay_minutes=15,
        max_lookback_minutes=effective_max_lookback,
    )
    if catchup_truncated:
        _emit(
            "[advisory.continuous_watch] news catchup truncated "
            f"max_lookback_minutes={effective_max_lookback} from={published_from.isoformat()} to={now.isoformat()}"
        )
    _emit(f"[advisory.continuous_watch] news start from={published_from.isoformat()} to={now.isoformat()}")
    events, meta = run_news_watch(
        lookback_days=1,
        to_date=now,
        published_from=published_from,
        refresh_feeds=True,
        include_market_context=True,
        market_context_limit=DEFAULT_MARKET_CONTEXT_WATCH_LIMIT,
        market_context_last_checked_at=published_from,
        include_theme_context=True,
        theme_context_last_checked_at=published_from,
        include_announcement_context=True,
        announcement_context_limit=DEFAULT_ANNOUNCEMENT_CONTEXT_WATCH_LIMIT,
        announcement_context_last_checked_at=published_from,
        announcement_context_lookback_days=DEFAULT_ANNOUNCEMENT_CONTEXT_LOOKBACK_DAYS,
        include_macro_context=True,
        macro_context_limit=DEFAULT_MACRO_CONTEXT_WATCH_LIMIT,
        macro_context_last_checked_at=published_from,
        include_bhavcopy_context=True,
        bhavcopy_context_limit=DEFAULT_BHAVCOPY_CONTEXT_WATCH_LIMIT,
        bhavcopy_context_last_checked_at=published_from,
    )
    persist_news_events(events)
    source_counters = watcher_source_counters(source="news", meta=meta, events=events)
    last_item_ts = events["published_on"].max() if not events.empty else now
    persist_sync_state(
        source_name=source_name,
        last_success_at=now,
        last_item_ts=last_item_ts,
        cursor_value=None if pd.isna(last_item_ts) else pd.Timestamp(last_item_ts).isoformat(),
        state={
            "matched_event_count": int(meta.get("matched_event_count") or 0),
            "requested_from": published_from.isoformat(),
            "requested_to": now.isoformat(),
            "replay_minutes": 15,
            "max_lookback_minutes": effective_max_lookback,
            "catchup_truncated": bool(catchup_truncated),
            "source_counters": source_counters,
        },
        status="ok",
    )
    result = {
        "status": "ok",
        **meta,
        "requested_from": published_from.isoformat(),
        "requested_to": now.isoformat(),
        "catchup_truncated": bool(catchup_truncated),
        "max_lookback_minutes": effective_max_lookback,
        "source_counters": source_counters,
    }
    publish_bus_message("stockey:continuous_watch:news", {"published_at": pd.Timestamp.utcnow(), **result})
    return result


def run_announcement_cycle(
    *,
    interval_seconds: int,
    initial_lookback_minutes: int = 720,
    max_lookback_minutes: int | None = None,
    max_ingest_targets: int | None = None,
) -> dict[str, Any]:
    source_name = "continuous_watch:announcements"
    now = pd.Timestamp.utcnow()
    state = load_sync_state(source_name) or {}
    previous_cursor = pd.to_datetime(state.get("last_item_ts"), utc=True, errors="coerce")
    effective_max_lookback = int(max_lookback_minutes or os.getenv("WATCHER_ANNOUNCEMENT_MAX_LOOKBACK_MINUTES", "1440"))
    last_checked_at, catchup_truncated = _bounded_event_from_cursor(
        previous_cursor,
        now,
        initial_lookback_minutes=int(initial_lookback_minutes),
        replay_minutes=15,
        max_lookback_minutes=effective_max_lookback,
    )
    if catchup_truncated:
        _emit(
            "[advisory.continuous_watch] announcements catchup truncated "
            f"max_lookback_minutes={effective_max_lookback} from={last_checked_at.isoformat()} to={now.isoformat()}"
        )
    _emit(f"[advisory.continuous_watch] announcements start from={last_checked_at.isoformat()} to={now.isoformat()}")
    watch_updates, events, meta = run_announcement_watch(
        to_date=now,
        include_market_context=True,
        market_context_limit=DEFAULT_MARKET_CONTEXT_WATCH_LIMIT,
        market_context_last_checked_at=last_checked_at,
        include_theme_context=True,
        theme_context_last_checked_at=last_checked_at,
        include_announcement_context=True,
        announcement_context_limit=DEFAULT_ANNOUNCEMENT_CONTEXT_WATCH_LIMIT,
        announcement_context_last_checked_at=last_checked_at,
        announcement_context_lookback_days=DEFAULT_ANNOUNCEMENT_CONTEXT_LOOKBACK_DAYS,
        include_macro_context=True,
        macro_context_limit=DEFAULT_MACRO_CONTEXT_WATCH_LIMIT,
        macro_context_last_checked_at=last_checked_at,
        include_bhavcopy_context=True,
        bhavcopy_context_limit=DEFAULT_BHAVCOPY_CONTEXT_WATCH_LIMIT,
        bhavcopy_context_last_checked_at=last_checked_at,
        max_ingest_targets=WATCHER_ANNOUNCEMENT_MAX_INGEST_TARGETS if max_ingest_targets is None else int(max_ingest_targets),
    )
    persist_watch_outputs(watch_updates, events)
    announcement_alert_summary = {"input_count": 0, "persisted_count": 0, "suppressed_count": 0}
    if WATCHER_ANNOUNCEMENT_ALERTS_ENABLED:
        announcement_alerts = build_announcement_alerts(events, observed_at=now)
        announcement_alert_summary = persist_alerts(announcement_alerts)
    source_counters = watcher_source_counters(source="announcements", meta=meta, events=events, watch_updates=watch_updates)
    last_item_ts = events["published_on"].max() if not events.empty else now
    persist_sync_state(
        source_name=source_name,
        last_success_at=now,
        last_item_ts=last_item_ts,
        cursor_value=None if pd.isna(last_item_ts) else pd.Timestamp(last_item_ts).isoformat(),
        state={
            "match_count": int(meta.get("match_count") or 0),
            "requested_from": last_checked_at.isoformat(),
            "requested_to": now.isoformat(),
            "replay_minutes": 15,
            "max_lookback_minutes": effective_max_lookback,
            "catchup_truncated": bool(catchup_truncated),
            "max_ingest_targets": int(meta.get("max_ingest_targets") or 0),
            "skipped_ingest_target_count": int(meta.get("skipped_ingest_target_count") or 0),
            "source_counters": source_counters,
        },
        status="ok",
    )
    result = {
        "status": "ok",
        **meta,
        "requested_from": last_checked_at.isoformat(),
        "requested_to": now.isoformat(),
        "catchup_truncated": bool(catchup_truncated),
        "max_lookback_minutes": effective_max_lookback,
        "max_ingest_targets": int(meta.get("max_ingest_targets") or 0),
        "skipped_ingest_target_count": int(meta.get("skipped_ingest_target_count") or 0),
        "source_counters": source_counters,
        "announcement_alerts": announcement_alert_summary,
    }
    publish_bus_message("stockey:continuous_watch:announcements", {"published_at": pd.Timestamp.utcnow(), **result})
    return result


def run_operator_frontend_cycle() -> dict[str, Any]:
    source_name = "continuous_watch:operator_frontend"
    now = pd.Timestamp.utcnow()
    result = {
        "status": "ok",
        "mode": "api_frontend",
        "detail": "Static dashboard generation is disabled; operator UI reads from advisory.api.app.",
    }
    persist_sync_state(
        source_name=source_name,
        last_success_at=now,
        last_item_ts=now,
        cursor_value=now.isoformat(),
        state=result,
        status="ok",
    )
    publish_bus_message("stockey:continuous_watch:operator_frontend", {"published_at": pd.Timestamp.utcnow(), **result})
    return result


def run_theme_context_cycle(*, limit: int | None = None) -> dict[str, Any]:
    effective_limit = int(limit or os.getenv("THEME_CONTEXT_DERISK_LIMIT", "50"))
    _emit(f"[advisory.continuous_watch] theme_context start limit={effective_limit}")
    result = refresh_from_theme_context(limit=effective_limit, dry_run=False)
    try:
        result["action_refresh"] = run_action_refresh_from_signal_result(result)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.continuous_watch",
            fallback_type="continuous_watch_action_refresh_failed",
            source="continuous_watch:action_refresh",
            severity="warn",
            reason="Context-overlay signal rows were refreshed, but bounded Action Queue refresh failed.",
            error=exc,
            metadata={"signal_rows": result.get("signal_rows"), "affected_symbol_count": result.get("affected_symbol_count")},
        )
        result["action_refresh"] = {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "signal_rows_persisted": result.get("signal_rows"),
            "affected_symbol_count": result.get("affected_symbol_count"),
            "broker_execution_allowed": False,
            "portfolio_authority": "none",
        }
    publish_bus_message("stockey:continuous_watch:theme_context", {"published_at": pd.Timestamp.utcnow(), **result})
    return result


def run_causal_memory_cycle(*, limit: int | None = None) -> dict[str, Any]:
    if not WATCHER_CAUSAL_MEMORY_REFRESH_ENABLED:
        result = {
            "status": "skipped",
            "reason": "WATCHER_CAUSAL_MEMORY_REFRESH_ENABLED=false",
            "broker_execution_allowed": False,
            "portfolio_authority": "none",
        }
        publish_bus_message("stockey:continuous_watch:causal_memory", {"published_at": pd.Timestamp.utcnow(), **result})
        return result
    effective_limit = int(limit or WATCHER_CAUSAL_MEMORY_REFRESH_LIMIT)
    _emit(f"[advisory.continuous_watch] causal_memory start limit={effective_limit}")
    result = refresh_from_causal_memory(limit=effective_limit, dry_run=False)
    try:
        result["action_refresh"] = run_action_refresh_from_signal_result(result)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.continuous_watch",
            fallback_type="continuous_watch_causal_memory_action_refresh_failed",
            source="continuous_watch:causal_memory",
            severity="warn",
            reason="Causal-memory signal rows were refreshed, but bounded Action Queue refresh failed.",
            error=exc,
            metadata={"signal_rows": result.get("signal_rows"), "affected_symbol_count": result.get("affected_symbol_count")},
        )
        result["action_refresh"] = {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "signal_rows_persisted": result.get("signal_rows"),
            "affected_symbol_count": result.get("affected_symbol_count"),
            "broker_execution_allowed": False,
            "portfolio_authority": "none",
        }
    publish_bus_message("stockey:continuous_watch:causal_memory", {"published_at": pd.Timestamp.utcnow(), **result})
    return result


def run_router_cycle() -> dict[str, Any]:
    result = route_live_updates()
    try:
        result["action_refresh"] = run_action_refresh_from_signal_result(result)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.continuous_watch",
            fallback_type="continuous_watch_router_action_refresh_failed",
            source="continuous_watch:router",
            severity="warn",
            reason="Router signal rows were refreshed, but bounded Action Queue refresh failed.",
            error=exc,
            metadata={"executed_actions": result.get("executed_actions"), "affected_symbol_count": result.get("affected_symbol_count")},
        )
        result["action_refresh"] = {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "executed_actions": result.get("executed_actions"),
            "affected_symbol_count": result.get("affected_symbol_count"),
            "broker_execution_allowed": False,
            "portfolio_authority": "none",
        }
    return result


def run_action_refresh_from_signal_result(signal_result: dict[str, Any]) -> dict[str, Any]:
    if not WATCHER_ACTION_REFRESH_ENABLED:
        return {
            "status": "skipped",
            "reason": "WATCHER_ACTION_REFRESH_ENABLED=false",
            "broker_execution_allowed": False,
            "portfolio_authority": "none",
        }
    source_symbols = signal_result.get("action_refresh_symbols") if "action_refresh_symbols" in signal_result else signal_result.get("affected_symbols", [])
    symbols = sorted(
        {
            str(symbol or "").strip().upper()
            for symbol in (source_symbols or [])
            if str(symbol or "").strip()
        }
    )
    if not symbols:
        return {
            "status": "skipped",
            "reason": "no_signal_refresh_symbols",
            "broker_execution_allowed": False,
            "portfolio_authority": "none",
        }
    if len(symbols) > WATCHER_ACTION_REFRESH_MAX_SYMBOLS:
        symbols = symbols[:WATCHER_ACTION_REFRESH_MAX_SYMBOLS]
        truncated = True
    else:
        truncated = False
    asof_date = pd.Timestamp.utcnow().normalize()
    _emit(f"[advisory.continuous_watch] action_refresh start symbols={len(symbols)} asof={asof_date.date()}")
    refreshed = build_action_recommendations(asof_date=asof_date, symbols=symbols)
    persist_action_recommendations(refreshed)
    now = pd.Timestamp.utcnow()
    result = {
        "status": "ok",
        "asof_date": asof_date.isoformat(),
        "symbol_count": int(len(symbols)),
        "symbols_truncated": bool(truncated),
        "recommendation_rows": int(len(refreshed)),
        "action_counts": refreshed["action_code"].value_counts().to_dict() if not refreshed.empty and "action_code" in refreshed.columns else {},
        "source": signal_result.get("action_refresh_source") or "signal_refresh_context_overlays",
        "full_advisory_required": True,
        "broker_execution_allowed": False,
        "portfolio_authority": "none",
        "authority_scope": "review_input_only",
    }
    persist_sync_state(
        source_name="continuous_watch:action_refresh",
        last_success_at=now,
        last_item_ts=now,
        cursor_value=now.isoformat(),
        state=result,
        status="ok",
    )
    publish_bus_message("stockey:continuous_watch:action_refresh", {"published_at": now, **result})
    return result


def publish_lock_skipped_cycle(*, lock_file: str | None = None, lock_pid: str | None = None) -> dict[str, Any]:
    published_at = pd.Timestamp.utcnow()
    result = {
        "status": "skipped",
        "reason": "lock_already_running",
        "lock_file": lock_file,
        "lock_pid": lock_pid,
    }
    cycles = {
        "ohlcv": result.copy(),
        "announcements": result.copy(),
        "news": result.copy(),
        "router": result.copy(),
        "theme_context": result.copy(),
        "causal_memory": result.copy(),
        "operator_frontend": result.copy(),
        "wait_signals": result.copy(),
        "operator_snapshot": result.copy(),
        "trace_summary_store": result.copy(),
    }
    for cycle_name, payload in cycles.items():
        publish_bus_message(
            f"stockey:continuous_watch:{cycle_name}",
            {"published_at": published_at, **payload},
        )
    summary = {"status": "skipped", "reason": "lock_already_running", "cycles": cycles}
    publish_bus_message("stockey:continuous_watch:summary", {"published_at": published_at, **summary})
    return summary


def run_once(
    *,
    ohlcv_interval_seconds: int,
    news_interval_seconds: int,
    announcement_interval_seconds: int,
    intraday_interval_minutes: int,
    ohlcv_max_lookback_minutes: int,
    announcement_max_ingest_targets: int | None = None,
) -> dict[str, Any]:
    ensure_sync_state_table()
    ensure_alerts_table()
    summary: dict[str, Any] = {"status": "ok", "cycles": {}}

    def _run_cycle(source_name: str, cycle_name: str, func, **kwargs) -> dict[str, Any]:
        def _record_cycle_error(exc: BaseException, *, interrupted: bool = False) -> dict[str, Any]:
            fallback_type = "continuous_watch_cycle_interrupted" if interrupted else "continuous_watch_cycle_failed"
            reason = (
                "Continuous watcher cycle was interrupted; status was persisted as error before re-raising."
                if interrupted
                else "Continuous watcher cycle failed; status was persisted as error and later cycles may continue."
            )
            error = f"{type(exc).__name__}: {exc}"
            _emit(f"[advisory.continuous_watch] {cycle_name} failed error={error}")
            record_local_fallback_event(
                module="advisory.continuous_watch",
                source=source_name,
                fallback_type=fallback_type,
                severity="error",
                reason=reason,
                error=exc,
                metadata={"cycle": cycle_name},
            )
            persist_sync_state(
                source_name=source_name,
                status="error",
                error_text=error,
                state={"cycle": cycle_name, "error": error, "interrupted": bool(interrupted)},
            )
            publish_bus_message(
                f"stockey:continuous_watch:{cycle_name}",
                {"published_at": pd.Timestamp.utcnow(), "status": "error", "error": error, "interrupted": bool(interrupted)},
            )
            summary["status"] = "error"
            return {"status": "error", "error": error, "interrupted": bool(interrupted)}

        try:
            return func(**kwargs)
        except KeyboardInterrupt as exc:
            _record_cycle_error(exc, interrupted=True)
            raise
        except Exception as exc:
            return _record_cycle_error(exc)

    def _skip_cycle(cycle_name: str, reason: str) -> dict[str, Any]:
        result = {"status": "skipped", "reason": reason}
        publish_bus_message(
            f"stockey:continuous_watch:{cycle_name}",
            {"published_at": pd.Timestamp.utcnow(), **result},
        )
        return result

    if _is_due("continuous_watch:ohlcv", ohlcv_interval_seconds):
        summary["cycles"]["ohlcv"] = _run_cycle(
            "continuous_watch:ohlcv",
            "ohlcv",
            run_ohlcv_cycle,
            interval_seconds=ohlcv_interval_seconds,
            intraday_interval_minutes=intraday_interval_minutes,
            max_lookback_minutes=ohlcv_max_lookback_minutes,
        )
    else:
        summary["cycles"]["ohlcv"] = _skip_cycle("ohlcv", "not_due")
    if _is_due("continuous_watch:announcements", announcement_interval_seconds):
        summary["cycles"]["announcements"] = _run_cycle(
            "continuous_watch:announcements",
            "announcements",
            run_announcement_cycle,
            interval_seconds=announcement_interval_seconds,
            max_ingest_targets=announcement_max_ingest_targets,
        )
    else:
        summary["cycles"]["announcements"] = _skip_cycle("announcements", "not_due")
    if _is_due("continuous_watch:news", news_interval_seconds):
        summary["cycles"]["news"] = _run_cycle("continuous_watch:news", "news", run_news_cycle, interval_seconds=news_interval_seconds)
    else:
        summary["cycles"]["news"] = _skip_cycle("news", "not_due")
    summary["cycles"]["router"] = _run_cycle("continuous_watch:router", "router", run_router_cycle)
    summary["cycles"]["theme_context"] = _run_cycle("continuous_watch:theme_context", "theme_context", run_theme_context_cycle)
    summary["cycles"]["causal_memory"] = _run_cycle("continuous_watch:causal_memory", "causal_memory", run_causal_memory_cycle)
    summary["cycles"]["operator_frontend"] = _run_cycle("continuous_watch:operator_frontend", "operator_frontend", run_operator_frontend_cycle)
    publish_bus_message("stockey:continuous_watch:summary", {"published_at": pd.Timestamp.utcnow(), **summary})
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the lightweight continuous watch loop for OHLCV, news, announcements, routing, and operator frontend status.")
    parser.add_argument("--publish-lock-skipped", action="store_true", help="Publish watcher skip events when an external lock prevents this run.")
    parser.add_argument("--lock-file", default=None, help="Lock file responsible for a published lock-skipped watcher cycle.")
    parser.add_argument("--lock-pid", default=None, help="PID currently holding the published watcher lock, when known.")
    parser.add_argument("--loop", action="store_true", help="Run continuously instead of once")
    parser.add_argument("--sleep-seconds", type=int, default=300, help="Loop sleep interval")
    parser.add_argument("--ohlcv-interval-seconds", type=int, default=300)
    parser.add_argument("--news-interval-seconds", type=int, default=1800)
    parser.add_argument("--announcement-interval-seconds", type=int, default=1800)
    parser.add_argument("--intraday-interval-minutes", type=int, default=1)
    parser.add_argument("--ohlcv-max-lookback-minutes", type=int, default=int(os.getenv("WATCHER_OHLCV_MAX_LOOKBACK_MINUTES", "240")))
    parser.add_argument("--announcement-max-ingest-targets", type=int, default=WATCHER_ANNOUNCEMENT_MAX_INGEST_TARGETS, help="Maximum unique announcement ingest targets per watcher run. Skipped targets remain due for later runs.")
    parser.add_argument("--output-dir", default=None, help="Deprecated; static dashboard generation has moved to the Nuxt operator frontend.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.publish_lock_skipped:
        summary = publish_lock_skipped_cycle(lock_file=args.lock_file, lock_pid=args.lock_pid)
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, default=str))
        return 0
    while True:
        started_at = time.monotonic()
        summary = run_once(
            ohlcv_interval_seconds=int(args.ohlcv_interval_seconds),
            news_interval_seconds=int(args.news_interval_seconds),
            announcement_interval_seconds=int(args.announcement_interval_seconds),
            intraday_interval_minutes=int(args.intraday_interval_minutes),
            ohlcv_max_lookback_minutes=int(args.ohlcv_max_lookback_minutes),
            announcement_max_ingest_targets=int(args.announcement_max_ingest_targets),
        )
        print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
        if not args.loop:
            return 1 if summary.get("status") == "error" else 0
        elapsed = time.monotonic() - started_at
        remaining = max(0, int(args.sleep_seconds) - elapsed)
        _emit(f"[advisory.continuous_watch] sleeping seconds={remaining}")
        time.sleep(remaining)


if __name__ == "__main__":
    raise SystemExit(main())
