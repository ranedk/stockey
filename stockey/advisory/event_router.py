from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from advisory.fallback_telemetry import record_local_fallback_event
from advisory.sync_state import load_sync_state, persist_sync_state, publish_bus_message
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration


ALERTS_TABLE = "advisory_live_watch_alerts"
ANNOUNCEMENT_EVENTS_TABLE = "advisory_watch_events"
NEWS_EVENTS_TABLE = "advisory_news_events"
ACTIONS_TABLE = "advisory_live_router_actions"
EVENT_ROUTER_SCHEMA_MIGRATION_ID = "20260611_advisory_event_router_actions_base"
EVENT_ROUTER_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {ACTIONS_TABLE} (
        routed_at TIMESTAMPTZ NOT NULL,
        asof_date TIMESTAMPTZ,
        symbol TEXT NOT NULL,
        setup_ids_json TEXT,
        source_type TEXT NOT NULL,
        action_type TEXT NOT NULL,
        action_status TEXT,
        action_reason TEXT,
        summary_json TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (routed_at, symbol, source_type, action_type)
    )
    """,
]
STATE_SOURCE_NAME = "continuous_watch:router"
DEFAULT_MAX_ACTIONS_PER_CYCLE: int | None = None
DEFAULT_MAX_EVENT_ONLY_ACTIONS: int | None = None


def _record_router_fallback(
    fallback_type: str,
    *,
    source: str,
    reason: str,
    error: Exception,
    severity: str = "warn",
    symbol: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="advisory.event_router",
        fallback_type=fallback_type,
        source=source,
        severity=severity,
        reason=reason,
        error=error,
        symbol=symbol,
        metadata=metadata or {},
    )


def ensure_actions_table() -> None:
    apply_schema_migration(
        migration_id=EVENT_ROUTER_SCHEMA_MIGRATION_ID,
        description="Create live event-router action table.",
        statements=EVENT_ROUTER_SCHEMA_STATEMENTS,
        metadata={"tables": [ACTIONS_TABLE]},
    )


def _normalize_series_ts(df: pd.DataFrame, column: str) -> pd.DataFrame:
    if column in df.columns and not df.empty:
        df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    return df


def load_recent_source_rows(table_name: str, *, load_from: pd.Timestamp | None) -> pd.DataFrame:
    clauses = ["1 = 1"]
    params: list[object] = []
    if load_from is not None and not pd.isna(load_from):
        clauses.append("load_ts > %s")
        params.append(load_from)
    if table_name in {ANNOUNCEMENT_EVENTS_TABLE, NEWS_EVENTS_TABLE}:
        clauses.append("COALESCE(event_status, 'triggered') = 'triggered'")
    try:
        df = sql_to_df(
            f"""
            SELECT *
            FROM {table_name}
            WHERE {' AND '.join(clauses)}
            ORDER BY load_ts, symbol
            """,
            params=tuple(params) if params else None,
        )
    except Exception as exc:
        _record_router_fallback(
            "event_router_source_rows_load_failed",
            source=table_name,
            reason="Event router could not load recent watcher source rows; affected updates may not trigger fast symbol refresh until a later run or full advisory.",
            error=exc,
            metadata={"load_from": None if load_from is None or pd.isna(load_from) else pd.Timestamp(load_from).isoformat()},
        )
        return pd.DataFrame()
    for column in ["load_ts", "observed_at", "published_on", "asof_date"]:
        if column in df.columns:
            df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    if "symbol" in df.columns:
        df["symbol"] = df["symbol"].astype("string").str.upper()
    return df


def load_watchlist_priority() -> pd.DataFrame:
    try:
        df = sql_to_df(
            """
            SELECT
                asof_date,
                setup_id,
                symbol,
                rank,
                candidate_state,
                current_state,
                theme_ids
            FROM advisory_watchlist
            WHERE asof_date = (SELECT MAX(asof_date) FROM advisory_watchlist)
            """
        )
    except Exception as exc:
        _record_router_fallback(
            "event_router_watchlist_priority_load_failed",
            source="advisory_watchlist",
            reason="Event router could not load watchlist priority; routing still proceeds but may lose setup rank prioritization.",
            error=exc,
        )
        return pd.DataFrame()
    if df.empty:
        return df
    df["symbol"] = df["symbol"].astype("string").str.upper()
    df["rank"] = pd.to_numeric(df["rank"], errors="coerce")
    return df


def build_routing_plan(
    *,
    alerts: pd.DataFrame,
    announcement_events: pd.DataFrame,
    news_events: pd.DataFrame,
    watchlist_priority: pd.DataFrame | None = None,
    max_actions: int | None = DEFAULT_MAX_ACTIONS_PER_CYCLE,
    max_event_only_actions: int | None = DEFAULT_MAX_EVENT_ONLY_ACTIONS,
) -> list[dict[str, Any]]:
    combined: dict[str, dict[str, Any]] = {}
    rank_map: dict[tuple[str, str], float] = {}
    if isinstance(watchlist_priority, pd.DataFrame) and not watchlist_priority.empty:
        rank_map = {
            (str(row["symbol"]).upper(), str(row["setup_id"]).upper()): float(pd.to_numeric(row.get("rank"), errors="coerce") or 9999.0)
            for _, row in watchlist_priority.iterrows()
        }

    def _touch(symbol: str) -> dict[str, Any]:
        return combined.setdefault(
            symbol,
            {
                "symbol": symbol,
                "setup_ids": set(),
                "has_price_alert": False,
                "has_event": False,
                "source_types": set(),
                "reasons": [],
                "asof_date": pd.NaT,
                "priority_score": 0.0,
                "best_rank": 9999.0,
            },
        )

    for frame, source_type, is_price_alert in (
        (alerts, "price_alert", True),
        (announcement_events, "announcement_event", False),
        (news_events, "news_event", False),
    ):
        if frame.empty:
            continue
        for _, row in frame.iterrows():
            symbol = str(row.get("symbol") or "").upper().strip()
            if not symbol:
                continue
            item = _touch(symbol)
            setup_id = str(row.get("setup_id") or "").upper().strip()
            if setup_id:
                item["setup_ids"].add(setup_id)
                item["best_rank"] = min(item["best_rank"], rank_map.get((symbol, setup_id), 9999.0))
            item["source_types"].add(source_type)
            if is_price_alert:
                item["has_price_alert"] = True
                alert_type = str(row.get("alert_type") or "price_alert")
                item["reasons"].append(alert_type)
                item["priority_score"] += {
                    "POSITION_INVALIDATION_HIT": 5.0,
                    "STOP_HIT": 4.5,
                    "INVALIDATION_HIT": 4.0,
                    "ENTRY_ZONE_HIT": 3.0,
                    "BREAKOUT_ABOVE_RANGE": 2.0,
                }.get(alert_type, 1.0)
            else:
                item["has_event"] = True
                item["reasons"].append(source_type)
                item["priority_score"] += 1.5 if source_type == "announcement_event" else 1.0
            asof_date = pd.to_datetime(row.get("asof_date"), utc=True, errors="coerce")
            if pd.notna(asof_date):
                current = pd.to_datetime(item.get("asof_date"), utc=True, errors="coerce")
                item["asof_date"] = asof_date if pd.isna(current) else max(current, asof_date)

    plan: list[dict[str, Any]] = []
    for symbol, item in sorted(combined.items()):
        if item["has_price_alert"] and item["has_event"]:
            action_type = "refresh_symbol_full"
            start_at = "technicals"
            include_watch = True
            include_lifecycle = True
            stop_at = "lifecycle"
        elif item["has_price_alert"]:
            action_type = "refresh_symbol_price"
            start_at = "technicals"
            include_watch = False
            include_lifecycle = any(reason in {"POSITION_INVALIDATION_HIT", "STOP_HIT", "INVALIDATION_HIT"} for reason in item["reasons"])
            stop_at = "lifecycle" if include_lifecycle else "portfolio"
        else:
            action_type = "refresh_symbol_event"
            start_at = "evaluate"
            include_watch = True
            include_lifecycle = False
            stop_at = "portfolio"
        plan.append(
            {
                "symbol": symbol,
                "setup_ids": sorted(item["setup_ids"]),
                "source_types": sorted(item["source_types"]),
                "action_type": action_type,
                "start_at": start_at,
                "stop_at": stop_at,
                "include_watch": include_watch,
                "include_lifecycle": include_lifecycle,
                "asof_date": None if pd.isna(item["asof_date"]) else pd.Timestamp(item["asof_date"]).isoformat(),
                "reasons": sorted(set(item["reasons"])),
                "priority_score": round(float(item["priority_score"]), 4),
                "best_rank": None if item["best_rank"] >= 9999.0 else int(item["best_rank"]),
            }
        )
    ordered = sorted(
        plan,
        key=lambda row: (
            0 if "price_alert" in (row.get("source_types") or []) else 1,
            -(float(row.get("priority_score") or 0.0)),
            int(row.get("best_rank") or 9999),
            row.get("symbol") or "",
        ),
    )
    event_only_kept = 0
    limited: list[dict[str, Any]] = []
    for row in ordered:
        is_event_only = row.get("action_type") == "refresh_symbol_event"
        if max_event_only_actions is not None and is_event_only and event_only_kept >= int(max_event_only_actions):
            continue
        limited.append(row)
        if is_event_only:
            event_only_kept += 1
        if max_actions is not None and len(limited) >= int(max_actions):
            break
    return limited


def execute_routing_plan(plan: list[dict[str, Any]]) -> pd.DataFrame:
    from advisory.signal_refresh import refresh_symbol

    rows: list[dict[str, Any]] = []
    for item in plan:
        routed_at = pd.Timestamp.utcnow()
        asof_date = pd.to_datetime(item.get("asof_date"), utc=True, errors="coerce")
        try:
            summary = refresh_symbol(
                symbol=item["symbol"],
                reason=f"router:{','.join(item.get('source_types') or [])}:{item.get('action_type')}",
                asof_date=None if pd.isna(asof_date) else asof_date,
                dry_run=False,
                refresh_trace_summary=False,
                router_context={
                    "source_types": item.get("source_types") or [],
                    "action_type": item.get("action_type"),
                    "reasons": item.get("reasons") or [],
                    "setup_ids": item.get("setup_ids") or [],
                    "priority_score": item.get("priority_score"),
                    "best_rank": item.get("best_rank"),
                    "start_at": item.get("start_at"),
                    "stop_at": item.get("stop_at"),
                    "include_watch": item.get("include_watch"),
                    "include_lifecycle": item.get("include_lifecycle"),
                },
            )
            status = "ok"
            action_reason = ",".join(item.get("reasons") or [])
        except Exception as exc:  # pragma: no cover - runtime guard
            summary = {"status": "error", "error": f"{exc.__class__.__name__}: {exc}"}
            status = "error"
            action_reason = f"router_error:{exc.__class__.__name__}"
            _record_router_fallback(
                "event_router_symbol_refresh_failed",
                source="advisory.signal_refresh",
                reason="Event router could not refresh a symbol after watcher evidence; the router action row was marked error.",
                error=exc,
                severity="error",
                symbol=str(item.get("symbol") or "").strip().upper() or None,
                metadata={"action_type": item.get("action_type"), "source_types": item.get("source_types") or []},
            )
        rows.append(
            {
                "routed_at": routed_at,
                "asof_date": asof_date,
                "symbol": item["symbol"],
                "setup_ids_json": json.dumps(item.get("setup_ids") or [], ensure_ascii=False, sort_keys=True),
                "source_type": ",".join(item.get("source_types") or []),
                "action_type": item["action_type"],
                "action_status": status,
                "action_reason": action_reason,
                "summary_json": json.dumps(summary, ensure_ascii=False, sort_keys=True, default=str),
                "load_ts": pd.Timestamp.utcnow(),
            }
        )
    return pd.DataFrame(rows)


def persist_actions(df: pd.DataFrame) -> None:
    ensure_actions_table()
    if df.empty:
        return
    upsert_to_db(df, ACTIONS_TABLE, unique_keys=["routed_at", "symbol", "source_type", "action_type"], timescaledb_column="routed_at")


def route_live_updates(
    *,
    max_actions: int | None = DEFAULT_MAX_ACTIONS_PER_CYCLE,
    max_event_only_actions: int | None = DEFAULT_MAX_EVENT_ONLY_ACTIONS,
) -> dict[str, Any]:
    ensure_actions_table()
    state = load_sync_state(STATE_SOURCE_NAME) or {}
    load_from = pd.to_datetime(state.get("last_item_ts"), utc=True, errors="coerce")
    alerts = load_recent_source_rows(ALERTS_TABLE, load_from=load_from)
    announcement_events = load_recent_source_rows(ANNOUNCEMENT_EVENTS_TABLE, load_from=load_from)
    news_events = load_recent_source_rows(NEWS_EVENTS_TABLE, load_from=load_from)
    watchlist_priority = load_watchlist_priority()
    plan = build_routing_plan(
        alerts=alerts,
        announcement_events=announcement_events,
        news_events=news_events,
        watchlist_priority=watchlist_priority,
        max_actions=None if max_actions is None else int(max_actions),
        max_event_only_actions=None if max_event_only_actions is None else int(max_event_only_actions),
    )
    actions = execute_routing_plan(plan) if plan else pd.DataFrame()
    persist_actions(actions)
    affected_symbols = sorted(
        {
            str(symbol or "").strip().upper()
            for symbol in (
                actions["symbol"].tolist() if not actions.empty and "symbol" in actions.columns else [item.get("symbol") for item in plan]
            )
            if str(symbol or "").strip()
        }
    )
    if not actions.empty and "symbol" in actions.columns and "action_status" in actions.columns:
        ok_actions = actions[actions["action_status"].astype("string").str.lower().eq("ok")]
        action_refresh_symbols = sorted({str(symbol or "").strip().upper() for symbol in ok_actions["symbol"].tolist() if str(symbol or "").strip()})
    elif actions.empty:
        action_refresh_symbols = []
    else:
        action_refresh_symbols = affected_symbols
    candidates = [frame["load_ts"].max() for frame in (alerts, announcement_events, news_events) if not frame.empty and "load_ts" in frame.columns]
    last_item_ts = max(candidates) if candidates else pd.Timestamp.utcnow()
    persist_sync_state(
        source_name=STATE_SOURCE_NAME,
        last_success_at=pd.Timestamp.utcnow(),
        last_item_ts=last_item_ts,
        cursor_value=None if pd.isna(last_item_ts) else pd.Timestamp(last_item_ts).isoformat(),
        state={
            "alert_rows": int(len(alerts)),
            "announcement_rows": int(len(announcement_events)),
            "news_rows": int(len(news_events)),
            "planned_actions": int(len(plan)),
            "executed_actions": int(len(actions)),
            "affected_symbols": affected_symbols,
            "affected_symbol_count": int(len(affected_symbols)),
            "action_refresh_symbols": action_refresh_symbols,
            "action_refresh_symbol_count": int(len(action_refresh_symbols)),
            "max_actions": None if max_actions is None else int(max_actions),
            "max_event_only_actions": None if max_event_only_actions is None else int(max_event_only_actions),
        },
        status="ok",
    )
    result = {
        "status": "ok",
        "alert_rows": int(len(alerts)),
        "announcement_rows": int(len(announcement_events)),
        "news_rows": int(len(news_events)),
        "planned_actions": int(len(plan)),
        "executed_actions": int(len(actions)),
        "affected_symbols": affected_symbols,
        "affected_symbol_count": int(len(affected_symbols)),
        "action_refresh_symbols": action_refresh_symbols,
        "action_refresh_symbol_count": int(len(action_refresh_symbols)),
        "action_refresh_source": "event_router",
        "plan": plan,
        "action_sample": actions.head(10).to_dict(orient="records") if not actions.empty else [],
    }
    publish_bus_message("stockey:continuous_watch:router", {"published_at": pd.Timestamp.utcnow(), **result})
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Route live continuous-watch updates into symbol-level advisory reevaluation.")
    parser.add_argument("--max-actions", type=int, default=0, help="0 means unlimited")
    parser.add_argument("--max-event-only-actions", type=int, default=0, help="0 means unlimited")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.dry_run:
        state = load_sync_state(STATE_SOURCE_NAME) or {}
        load_from = pd.to_datetime(state.get("last_item_ts"), utc=True, errors="coerce")
        alerts = load_recent_source_rows(ALERTS_TABLE, load_from=load_from)
        announcement_events = load_recent_source_rows(ANNOUNCEMENT_EVENTS_TABLE, load_from=load_from)
        news_events = load_recent_source_rows(NEWS_EVENTS_TABLE, load_from=load_from)
        max_actions = None if int(args.max_actions) <= 0 else int(args.max_actions)
        max_event_only_actions = None if int(args.max_event_only_actions) <= 0 else int(args.max_event_only_actions)
        result = {
            "status": "ok",
            "plan": build_routing_plan(
                alerts=alerts,
                announcement_events=announcement_events,
                news_events=news_events,
                watchlist_priority=load_watchlist_priority(),
                max_actions=max_actions,
                max_event_only_actions=max_event_only_actions,
            ),
        }
    else:
        max_actions = None if int(args.max_actions) <= 0 else int(args.max_actions)
        max_event_only_actions = None if int(args.max_event_only_actions) <= 0 else int(args.max_event_only_actions)
        result = route_live_updates(
            max_actions=max_actions,
            max_event_only_actions=max_event_only_actions,
        )
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
