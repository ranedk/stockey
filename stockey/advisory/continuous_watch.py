from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import timedelta
from typing import Any

import pandas as pd

from advisory.announcement_watch import persist_watch_outputs, run_announcement_watch
from advisory.announcement_watch import DEFAULT_MARKET_CONTEXT_WATCH_LIMIT
from advisory.event_router import route_live_updates
from advisory.news_watch import persist_news_events, run_news_watch
from advisory.sync_state import ensure_sync_state_table, load_sync_state, persist_sync_state, publish_bus_message
from data.dhanlive.ohlcv import sync_many_intraday
from utils.db import db_session, sql_to_df, upsert_to_db


WATCHLIST_TABLE = "advisory_watchlist"
ALERTS_TABLE = "advisory_live_watch_alerts"


def _emit(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def ensure_alerts_table() -> None:
    with db_session() as (_, cur):
        cur.execute(
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
                load_ts TIMESTAMPTZ,
                UNIQUE (observed_at, setup_id, symbol, alert_type)
            )
            """
        )
        cur.execute(f"ALTER TABLE {ALERTS_TABLE} ADD COLUMN IF NOT EXISTS monitor_source TEXT")
        cur.execute(f"ALTER TABLE {ALERTS_TABLE} ADD COLUMN IF NOT EXISTS stop_price DOUBLE PRECISION")


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
    for query in queries:
        try:
            df = sql_to_df(query, params=(asof_date,))
        except Exception:
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
    out = out.sort_values(
        ["symbol", "monitor_source", "rank", "state_updated_at", "setup_id"],
        ascending=[True, True, True, False, True],
        kind="stable",
    )
    return out.drop_duplicates(subset=["symbol"], keep="first").reset_index(drop=True)


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


def persist_alerts(df: pd.DataFrame) -> None:
    ensure_alerts_table()
    if df.empty:
        return
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
    upsert_to_db(out, ALERTS_TABLE, unique_keys=["observed_at", "setup_id", "symbol", "alert_type"], timescaledb_column="observed_at")
    publish_bus_message(
        "stockey:continuous_watch:alerts",
        {
            "published_at": pd.Timestamp.utcnow(),
            "alert_count": int(len(out)),
            "alerts": out.head(25).to_dict(orient="records"),
        },
    )


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
    symbols = sorted(watchlist["symbol"].dropna().astype(str).str.upper().unique().tolist())
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
    persist_alerts(alerts)
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
        },
        status="ok",
    )
    result = {
        "status": "ok",
        "symbol_count": len(symbols),
        "sync_results": sync_results,
        "alert_count": int(len(alerts)),
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
    )
    persist_news_events(events)
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
    }
    publish_bus_message("stockey:continuous_watch:news", {"published_at": pd.Timestamp.utcnow(), **result})
    return result


def run_announcement_cycle(*, interval_seconds: int, initial_lookback_minutes: int = 720, max_lookback_minutes: int | None = None) -> dict[str, Any]:
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
    )
    persist_watch_outputs(watch_updates, events)
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
) -> dict[str, Any]:
    ensure_sync_state_table()
    ensure_alerts_table()
    summary: dict[str, Any] = {"status": "ok", "cycles": {}}

    def _run_cycle(source_name: str, cycle_name: str, func, **kwargs) -> dict[str, Any]:
        try:
            return func(**kwargs)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            _emit(f"[advisory.continuous_watch] {cycle_name} failed error={error}")
            persist_sync_state(
                source_name=source_name,
                status="error",
                error_text=error,
                state={"cycle": cycle_name, "error": error},
            )
            publish_bus_message(
                f"stockey:continuous_watch:{cycle_name}",
                {"published_at": pd.Timestamp.utcnow(), "status": "error", "error": error},
            )
            summary["status"] = "error"
            return {"status": "error", "error": error}

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
        summary["cycles"]["announcements"] = _run_cycle("continuous_watch:announcements", "announcements", run_announcement_cycle, interval_seconds=announcement_interval_seconds)
    else:
        summary["cycles"]["announcements"] = _skip_cycle("announcements", "not_due")
    if _is_due("continuous_watch:news", news_interval_seconds):
        summary["cycles"]["news"] = _run_cycle("continuous_watch:news", "news", run_news_cycle, interval_seconds=news_interval_seconds)
    else:
        summary["cycles"]["news"] = _skip_cycle("news", "not_due")
    summary["cycles"]["router"] = _run_cycle("continuous_watch:router", "router", route_live_updates)
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
