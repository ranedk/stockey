from __future__ import annotations

import argparse
import json
import uuid
from typing import Any

import pandas as pd

from advisory.action_recommender import TABLE_NAME as ACTIONS_TABLE
from advisory.decision_trace import append_trace, append_trace_step, safe_trace_call
from advisory.event_policy import TABLE_NAME as EVENT_POLICY_TABLE
from advisory.fallback_telemetry import record_local_fallback_event
from advisory.position_lifecycle import LIFECYCLE_TABLE, REBALANCE_TABLE
from advisory.sync_state import persist_sync_state, publish_bus_message
from advisory.trace_summary_store import build_event_summary, build_symbol_summary
from advisory.wait_signals import WAIT_SIGNAL_MATCHES_TABLE, match_wait_signals
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration


TABLE_NAME = "advisory_signal_refresh_actions"
SIGNAL_REFRESH_SCHEMA_MIGRATION_ID = "20260611_advisory_signal_refresh_actions_base"
ROUTER_ACTIONS_TABLE = "advisory_live_router_actions"
STATE_SOURCE_NAME = "advisory:signal_refresh"

SIGNAL_REFRESH_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        refresh_id TEXT PRIMARY KEY,
        refreshed_at TIMESTAMPTZ NOT NULL,
        asof_date TIMESTAMPTZ,
        symbol TEXT NOT NULL,
        unique_id TEXT,
        reason TEXT,
        signal_action TEXT NOT NULL,
        signal_status TEXT,
        signal_source TEXT,
        confidence DOUBLE PRECISION,
        action_reason TEXT,
        effect_type TEXT,
        effect_summary TEXT,
        action_payload_json TEXT,
        trace_id TEXT,
        dry_run BOOLEAN,
        load_ts TIMESTAMPTZ
    )
    """,
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS effect_type TEXT",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS effect_summary TEXT",
    f"CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_symbol_refreshed ON {TABLE_NAME} (symbol, refreshed_at DESC)",
    f"CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_unique_id ON {TABLE_NAME} (unique_id)",
]

EXIT_ACTIONS = {"SELL", "FULL_EXIT", "EMERGENCY_EXIT", "PARTIAL_SELL", "PARTIAL_EXIT", "REDUCE", "REDUCE_REVIEW", "REDUCE_EXPOSURE_REVIEW", "GO_CASH_REVIEW"}
BUY_ACTIONS = {"BUY", "BUY_MORE", "ADD_ON_PULLBACK", "BUY_TRIGGERED"}
WATCH_ACTIONS = {"WATCH", "WATCHLIST", "NEAR_PIVOT", "READY", "MANUAL_REVIEW"}
WAIT_SIGNAL_NEGATIVE_ACTIONS = {"REDUCE_EXPOSURE_REVIEW", "GO_CASH_REVIEW", "FULL_EXIT", "PARTIAL_EXIT", "SELL", "PARTIAL_SELL", "REDUCE", "REDUCE_REVIEW"}
WAIT_SIGNAL_POSITIVE_ACTIONS = {"BUY", "BUY_MORE", "BUY_TRIGGERED", "ADD_ON_PULLBACK", "BUY_WATCH", "WATCH_SYMBOLS", "ADD_TO_WATCHLIST", "WATCH"}


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _text(value: Any, default: str | None = None) -> str | None:
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.signal_refresh",
            fallback_type="signal_refresh_text_missing_check_failed",
            source="text",
            severity="warn",
            reason="Signal-refresh normalization could not evaluate missingness while normalizing text and kept string conversion fallback.",
            error=exc,
            metadata={"value_type": type(value).__name__},
        )
    text = str(value).strip()
    if text.lower() in {"", "nan", "none", "null", "<na>"}:
        return default
    return text


def _num(value: Any) -> float | None:
    out = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(out) else float(out)


def _ts(value: Any) -> pd.Timestamp | None:
    out = pd.to_datetime(value, utc=True, errors="coerce")
    return None if pd.isna(out) else out


def _date(value: Any = None) -> pd.Timestamp:
    out = pd.to_datetime(value or pd.Timestamp.utcnow(), utc=True, errors="coerce")
    if pd.isna(out):
        out = pd.Timestamp.utcnow()
    return out.normalize()


def _jsonish(value: Any, default: Any = None, *, source: str = "signal_refresh_json_context") -> Any:
    if isinstance(value, (dict, list)):
        return value
    text = _text(value)
    if not text:
        return {} if default is None else default
    try:
        return json.loads(text)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.signal_refresh",
            fallback_type="signal_refresh_json_parse_failed",
            source=source,
            severity="warn",
            reason="Signal refresh could not parse stored JSON context and used the provided default.",
            error=exc,
            metadata={
                "default_type": type(default).__name__,
                "value_length": len(text),
                "value_excerpt": text[:240],
            },
        )
        return {} if default is None else default


def table_exists(table_name: str) -> bool:
    try:
        df = sql_to_df(
            """
            SELECT 1 AS exists_flag
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = %s
            LIMIT 1
            """,
            params=(table_name,),
        )
        return not df.empty
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.signal_refresh",
            fallback_type="signal_refresh_table_exists_check_failed",
            source=table_name,
            severity="warn",
            reason="Signal refresh could not verify whether a source table exists and will treat it as unavailable.",
            error=exc,
            metadata={"table_name": table_name},
        )
        return False


def ensure_table() -> None:
    apply_schema_migration(
        migration_id=SIGNAL_REFRESH_SCHEMA_MIGRATION_ID,
        description="Create and normalize incremental signal refresh action table.",
        statements=SIGNAL_REFRESH_SCHEMA_STATEMENTS,
        metadata={"module": "advisory.signal_refresh", "tables": [TABLE_NAME]},
    )


def _latest_row(table_name: str, symbol: str, *, asof_date: pd.Timestamp | None = None, date_column: str = "asof_date") -> dict[str, Any] | None:
    if not table_exists(table_name):
        return None
    clauses = ["UPPER(TRIM(symbol)) = %s"]
    params: list[Any] = [symbol.upper()]
    if asof_date is not None:
        clauses.append(f"{date_column} <= %s")
        params.append(asof_date)
    try:
        df = sql_to_df(
            f"""
            SELECT *
            FROM {table_name}
            WHERE {' AND '.join(clauses)}
            ORDER BY {date_column} DESC NULLS LAST, load_ts DESC NULLS LAST
            LIMIT 1
            """,
            params=tuple(params),
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.signal_refresh",
            source=table_name,
            fallback_type="signal_refresh_latest_row_unavailable",
            severity="warn",
            symbol=symbol,
            reason="Signal refresh continued without latest source row because lookup failed.",
            error=exc,
            metadata={"date_column": date_column, "asof_date": asof_date},
        )
        return None
    if df.empty:
        return None
    return df.iloc[0].to_dict()


def load_latest_action(symbol: str, *, asof_date: pd.Timestamp | None = None) -> dict[str, Any] | None:
    return _latest_row(ACTIONS_TABLE, symbol, asof_date=asof_date, date_column="asof_date")


def load_latest_lifecycle(symbol: str, *, asof_date: pd.Timestamp | None = None) -> dict[str, Any] | None:
    return _latest_row(LIFECYCLE_TABLE, symbol, asof_date=asof_date, date_column="asof_date")


def load_latest_rebalance(symbol: str, *, asof_date: pd.Timestamp | None = None) -> dict[str, Any] | None:
    return _latest_row(REBALANCE_TABLE, symbol, asof_date=asof_date, date_column="asof_date")


def load_event_policy(symbol: str, *, unique_id: str | None = None, asof_date: pd.Timestamp | None = None, limit: int = 5) -> list[dict[str, Any]]:
    if not table_exists(EVENT_POLICY_TABLE):
        return []
    clauses = ["UPPER(TRIM(symbol)) = %s"]
    params: list[Any] = [symbol.upper()]
    if unique_id:
        clauses.append("unique_id = %s")
        params.append(unique_id)
    if asof_date is not None:
        clauses.append("COALESCE(asof_date, published_on) <= %s")
        params.append(asof_date)
    try:
        df = sql_to_df(
            f"""
            SELECT *
            FROM {EVENT_POLICY_TABLE}
            WHERE {' AND '.join(clauses)}
            ORDER BY COALESCE(policy_at, asof_date, published_on) DESC NULLS LAST, load_ts DESC NULLS LAST
            LIMIT %s
            """,
            params=tuple([*params, max(1, min(int(limit), 50))]),
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.signal_refresh",
            source=EVENT_POLICY_TABLE,
            fallback_type="signal_refresh_event_policy_unavailable",
            severity="warn",
            symbol=symbol,
            unique_id=unique_id,
            reason="Signal refresh continued without event-policy rows because lookup failed.",
            error=exc,
            metadata={"asof_date": asof_date, "limit": limit},
        )
        return []
    return df.to_dict(orient="records") if not df.empty else []


def load_recent_router_actions(*, limit: int = 25) -> list[dict[str, Any]]:
    if not table_exists(ROUTER_ACTIONS_TABLE):
        return []
    try:
        df = sql_to_df(
            f"""
            SELECT *
            FROM {ROUTER_ACTIONS_TABLE}
            WHERE symbol IS NOT NULL
            ORDER BY routed_at DESC NULLS LAST, load_ts DESC NULLS LAST
            LIMIT %s
            """,
            params=(max(1, min(int(limit), 250)),),
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.signal_refresh",
            source=ROUTER_ACTIONS_TABLE,
            fallback_type="signal_refresh_router_actions_unavailable",
            severity="warn",
            reason="Signal refresh continued without recent router actions because lookup failed.",
            error=exc,
            metadata={"limit": limit},
        )
        return []
    if df.empty:
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in df.to_dict(orient="records"):
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        rows.append(item)
    return rows


def _normalized_action(value: Any) -> str | None:
    text = _text(value)
    return None if text is None else text.upper()


def _row_confidence(row: dict[str, Any] | None, *columns: str) -> float | None:
    if not row:
        return None
    for column in columns:
        value = _num(row.get(column))
        if value is not None:
            return value / 100.0 if value > 1.0 and column.endswith("_pct") else value
    return None


def _extract_action_payload(*, action: dict[str, Any] | None, lifecycle: dict[str, Any] | None, rebalance: dict[str, Any] | None, events: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "action": action or {},
        "lifecycle": lifecycle or {},
        "rebalance": rebalance or {},
        "event_policy": events,
    }


def wait_signal_escalation(match: dict[str, Any]) -> dict[str, Any]:
    expected = _normalized_action(match.get("expected_action")) or "MANUAL_REVIEW"
    evidence = _jsonish(match.get("evidence_json"), {}, source="wait_signal_evidence_json")
    wait_context = evidence.get("wait_signal") if isinstance(evidence, dict) else {}
    if not isinstance(wait_context, dict):
        wait_context = {}
    condition_type = _text(wait_context.get("condition_type")) or _text(match.get("signal_type")) or "wait_signal"
    wait_question = _text(wait_context.get("wait_question"))
    match_reason = _text(match.get("match_reason")) or "A wait signal matched fresh evidence."
    if expected in WAIT_SIGNAL_NEGATIVE_ACTIONS or "EXIT" in expected or "SELL" in expected or "REDUCE" in expected:
        action = "REDUCE_EXPOSURE_REVIEW" if expected not in {"GO_CASH_REVIEW"} else expected
    elif expected in WAIT_SIGNAL_POSITIVE_ACTIONS or "BUY" in expected or "WATCH" in expected:
        action = "WATCH"
    else:
        action = "MANUAL_REVIEW"
    reason_bits = [f"Matched {condition_type.replace('_', ' ')} wait signal.", match_reason]
    if expected != action:
        reason_bits.append(f"Escalated safely from expected {expected} to review-only {action}.")
    else:
        reason_bits.append(f"Expected follow-up action is {expected}.")
    if wait_question:
        reason_bits.append(f"Original wait: {wait_question}")
    return {
        "action": action,
        "reason": " ".join(reason_bits),
        "confidence": _row_confidence(match, "match_score"),
        "condition_type": condition_type,
        "expected_action": expected,
    }


def choose_signal(
    *,
    symbol: str,
    reason: str,
    action: dict[str, Any] | None,
    lifecycle: dict[str, Any] | None,
    rebalance: dict[str, Any] | None,
    events: list[dict[str, Any]],
    wait_matches: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    lifecycle_action = _normalized_action((lifecycle or {}).get("next_action"))
    rebalance_action = _normalized_action((rebalance or {}).get("suggested_action"))
    action_code = _normalized_action((action or {}).get("action_code"))

    wait_matches = wait_matches or []
    if lifecycle_action in EXIT_ACTIONS or rebalance_action in EXIT_ACTIONS:
        source = "lifecycle"
        signal_action = rebalance_action or lifecycle_action or "SELL"
        action_reason = _text((rebalance or {}).get("action_reason")) or _text((lifecycle or {}).get("next_action_reason")) or _text((lifecycle or {}).get("active_exit_condition"))
        confidence = _row_confidence(lifecycle, "target_confidence") or _row_confidence(action, "invest_score_pct")
    elif wait_matches:
        top_wait = wait_matches[0]
        escalation = wait_signal_escalation(top_wait)
        source = "wait_signal"
        signal_action = escalation["action"]
        action_reason = escalation["reason"]
        confidence = escalation["confidence"]
    elif events:
        top_event = events[0]
        event_action = _normalized_action(top_event.get("action_type")) or "MANUAL_REVIEW"
        source = "event_policy"
        signal_action = "WATCH" if event_action == "BUY_WATCH" else "MANUAL_REVIEW" if event_action in {"MANUAL_REVIEW", "REDUCE_EXPOSURE_REVIEW"} else event_action
        action_reason = _text(top_event.get("action_reason")) or _text(top_event.get("action_detail")) or f"Latest event policy action: {event_action}"
        confidence = _row_confidence(top_event, "confidence", "policy_score")
    elif action_code:
        source = "action_recommendation"
        signal_action = action_code
        action_reason = _text(action.get("action_reason")) or _text(action.get("action_detail")) or f"Latest consolidated advisory action: {action_code}"
        confidence = _row_confidence(action, "invest_score_pct")
    elif lifecycle_action:
        source = "lifecycle"
        signal_action = lifecycle_action
        action_reason = _text((lifecycle or {}).get("next_action_reason")) or _text((lifecycle or {}).get("lifecycle_reason"))
        confidence = _row_confidence(lifecycle, "target_confidence")
    else:
        source = "none"
        signal_action = "NO_CHANGE"
        action_reason = "No latest action, lifecycle, or event-policy row found for symbol."
        confidence = None

    normalized = _normalized_action(signal_action) or "NO_CHANGE"
    if normalized in EXIT_ACTIONS:
        status = "exit_or_reduce"
    elif normalized in BUY_ACTIONS:
        status = "entry_or_add"
    elif normalized in WATCH_ACTIONS:
        status = "watch_or_review"
    elif normalized == "NO_CHANGE":
        status = "no_change"
    else:
        status = "review"

    return {
        "symbol": symbol.upper(),
        "signal_action": normalized,
        "signal_status": status,
        "signal_source": source,
        "confidence": confidence,
        "action_reason": action_reason or f"Signal refresh from {reason}.",
    }


def classify_signal_effect(
    *,
    signal: dict[str, Any],
    action: dict[str, Any] | None,
    wait_matches: list[dict[str, Any]] | None = None,
) -> dict[str, str | None]:
    wait_matches = wait_matches or []
    signal_action = _normalized_action(signal.get("signal_action")) or "NO_CHANGE"
    previous_action = _normalized_action((action or {}).get("action_code"))
    signal_source = _text(signal.get("signal_source")) or "none"

    if wait_matches:
        top_match = wait_matches[0]
        condition_type = _text(top_match.get("signal_type")) or "wait_signal"
        return {
            "effect_type": "wait_match_created",
            "effect_summary": f"Detected a wait-signal match for {condition_type}; fast refresh stayed review-only until operator/advisory reconciliation.",
            "previous_action": previous_action,
        }
    if previous_action and signal_action != previous_action and signal_action != "NO_CHANGE":
        return {
            "effect_type": "action_changed",
            "effect_summary": f"Fast refresh changed the symbol-level action from {previous_action} to {signal_action}; daily advisory remains authoritative.",
            "previous_action": previous_action,
        }
    return {
        "effect_type": "evidence_only",
        "effect_summary": f"Watcher refreshed {signal_source} evidence without changing the latest consolidated action.",
        "previous_action": previous_action,
    }


def make_refresh_id(*, refreshed_at: Any, symbol: str, unique_id: str | None, reason: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, json_dumps({"refreshed_at": str(refreshed_at), "symbol": symbol.upper(), "unique_id": unique_id, "reason": reason})))


def persist_signal_rows(rows: list[dict[str, Any]]) -> None:
    ensure_table()
    if not rows:
        return
    df = pd.DataFrame(rows)
    for column in ["refreshed_at", "asof_date", "load_ts"]:
        if column in df.columns:
            df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    if "confidence" in df.columns:
        df["confidence"] = pd.to_numeric(df["confidence"], errors="coerce")
    if "dry_run" in df.columns:
        df["dry_run"] = df["dry_run"].astype("boolean")
    upsert_to_db(df, TABLE_NAME, unique_keys=["refresh_id"])


def refresh_symbol(
    *,
    symbol: str,
    reason: str = "manual",
    unique_id: str | None = None,
    asof_date: Any = None,
    dry_run: bool = False,
    refresh_trace_summary: bool = True,
) -> dict[str, Any]:
    ensure_table()
    normalized_symbol = str(symbol).strip().upper()
    if not normalized_symbol:
        raise ValueError("symbol is required")
    effective_asof = _date(asof_date)
    action = load_latest_action(normalized_symbol, asof_date=effective_asof)
    lifecycle = load_latest_lifecycle(normalized_symbol, asof_date=effective_asof)
    rebalance = load_latest_rebalance(normalized_symbol, asof_date=effective_asof)
    events = load_event_policy(normalized_symbol, unique_id=unique_id, asof_date=effective_asof)
    wait_result = match_wait_signals(symbols=[normalized_symbol], limit=100, persist=not bool(dry_run))
    wait_matches = wait_result.get("matches") or []
    signal = choose_signal(symbol=normalized_symbol, reason=reason, action=action, lifecycle=lifecycle, rebalance=rebalance, events=events, wait_matches=wait_matches)
    effect = classify_signal_effect(signal=signal, action=action, wait_matches=wait_matches)
    refreshed_at = pd.Timestamp.utcnow()
    refresh_id = make_refresh_id(refreshed_at=refreshed_at, symbol=normalized_symbol, unique_id=unique_id, reason=reason)
    payload = _extract_action_payload(action=action, lifecycle=lifecycle, rebalance=rebalance, events=events)
    payload["wait_signal_matches"] = wait_matches
    payload["wait_signal_match_table"] = WAIT_SIGNAL_MATCHES_TABLE
    payload["watcher_effect"] = effect
    trace_id = None
    if not dry_run:
        trace_id = safe_trace_call(
            append_trace,
            asof_date=effective_asof,
            symbol=normalized_symbol,
            unique_id=unique_id,
            trigger_type=f"signal_refresh:{reason}",
            previous_action=None,
            new_action=signal["signal_action"],
            final_action=signal["signal_action"],
            final_reason=signal["action_reason"],
            source_table=TABLE_NAME,
            source_key=refresh_id,
            payload={"signal": signal, "context": payload},
        )
        if trace_id:
            safe_trace_call(
                append_trace_step,
                trace_id=trace_id,
                step_idx=1,
                stage="signal_refresh",
                status=signal["signal_status"],
                reason=signal["action_reason"],
                input_payload={"symbol": normalized_symbol, "reason": reason, "unique_id": unique_id, "asof_date": str(effective_asof)},
                output_payload=signal,
                payload={"source_tables": [ACTIONS_TABLE, LIFECYCLE_TABLE, REBALANCE_TABLE, EVENT_POLICY_TABLE]},
            )

    row = {
        "refresh_id": refresh_id,
        "refreshed_at": refreshed_at,
        "asof_date": effective_asof,
        "symbol": normalized_symbol,
        "unique_id": unique_id,
        "reason": reason,
        "signal_action": signal["signal_action"],
        "signal_status": signal["signal_status"],
        "signal_source": signal["signal_source"],
        "confidence": signal["confidence"],
        "action_reason": signal["action_reason"],
        "effect_type": effect["effect_type"],
        "effect_summary": effect["effect_summary"],
        "action_payload_json": json_dumps(payload),
        "trace_id": trace_id,
        "dry_run": bool(dry_run),
        "load_ts": pd.Timestamp.utcnow(),
    }
    if not dry_run:
        persist_signal_rows([row])
        if refresh_trace_summary:
            try:
                build_symbol_summary(normalized_symbol, persist=True)
                if unique_id:
                    build_event_summary(str(unique_id), persist=True)
            except Exception as exc:
                record_local_fallback_event(
                    module="advisory.signal_refresh",
                    fallback_type="signal_refresh_trace_summary_refresh_failed",
                    source="advisory.trace_summary_store",
                    severity="warn",
                    symbol=normalized_symbol,
                    unique_id=unique_id,
                    reason="Signal refresh row was persisted but trace-summary cache refresh failed.",
                    error=exc,
                    metadata={"refresh_id": refresh_id, "reason": reason},
                )
    return row


def refresh_from_router(*, limit: int = 25, dry_run: bool = False) -> dict[str, Any]:
    router_rows = load_recent_router_actions(limit=limit)
    output_rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for item in router_rows:
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        reason_bits = [_text(item.get("source_type")), _text(item.get("action_type"))]
        reason = "router:" + ":".join([bit for bit in reason_bits if bit])
        try:
            output_rows.append(
                refresh_symbol(
                    symbol=symbol,
                    reason=reason,
                    asof_date=_ts(item.get("asof_date")),
                    dry_run=dry_run,
                    refresh_trace_summary=False,
                )
            )
        except Exception as exc:  # pragma: no cover - runtime guard
            record_local_fallback_event(
                module="advisory.signal_refresh",
                fallback_type="signal_refresh_router_item_failed",
                source=ROUTER_ACTIONS_TABLE,
                severity="warn",
                symbol=symbol or None,
                reason="Router-triggered signal refresh failed for one symbol while the batch continued.",
                error=exc,
                metadata={
                    "router_reason": reason,
                    "unique_id": _text(item.get("unique_id")),
                    "asof_date": str(item.get("asof_date")) if item.get("asof_date") is not None else None,
                },
            )
            errors.append({"symbol": symbol, "error": f"{exc.__class__.__name__}: {exc}"})
    if not dry_run:
        persist_sync_state(
            source_name=STATE_SOURCE_NAME,
            last_success_at=pd.Timestamp.utcnow(),
            last_item_ts=max([pd.to_datetime(row.get("refreshed_at"), utc=True, errors="coerce") for row in output_rows], default=pd.Timestamp.utcnow()),
            state={"router_rows": len(router_rows), "signal_rows": len(output_rows), "errors": errors[:20]},
            status="ok" if not errors else "partial",
            error_text=json_dumps(errors[:20]) if errors else None,
        )
        publish_bus_message(
            "stockey:signal_refresh",
            {"published_at": pd.Timestamp.utcnow(), "signal_rows": len(output_rows), "errors": errors[:20], "sample": output_rows[:10]},
        )
    return {
        "status": "ok" if not errors else "partial",
        "router_rows": len(router_rows),
        "signal_rows": len(output_rows),
        "errors": errors,
        "sample": output_rows[:10],
        "dry_run": bool(dry_run),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fast symbol-scoped signal refresh for watcher/router events.")
    parser.add_argument("--symbol", action="append", default=[], help="Symbol to refresh. Can be passed multiple times.")
    parser.add_argument("--unique-id", default=None, help="Optional event unique id for event-scoped refresh.")
    parser.add_argument("--reason", default="manual", help="Refresh reason, for example ohlcv, announcement, news, router.")
    parser.add_argument("--asof-date", default=None, help="Advisory date. Defaults to today UTC-normalized.")
    parser.add_argument("--from-router", action="store_true", help="Refresh latest symbols from advisory_live_router_actions.")
    parser.add_argument("--limit", type=int, default=25, help="Router symbol limit for --from-router.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--format", choices=["json", "text"], default="json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.from_router:
        result = refresh_from_router(limit=args.limit, dry_run=bool(args.dry_run))
    else:
        symbols = [str(value).strip().upper() for value in args.symbol if str(value).strip()]
        if not symbols:
            raise SystemExit("--symbol is required unless --from-router is used")
        rows = [
            refresh_symbol(
                symbol=symbol,
                reason=args.reason,
                unique_id=args.unique_id,
                asof_date=args.asof_date,
                dry_run=bool(args.dry_run),
            )
            for symbol in symbols
        ]
        result = {"status": "ok", "signal_rows": len(rows), "rows": rows, "dry_run": bool(args.dry_run)}
    if args.format == "text":
        print(f"status={result.get('status')} signal_rows={result.get('signal_rows', 0)} dry_run={result.get('dry_run')}")
        for row in result.get("rows") or result.get("sample") or []:
            print(f"{row.get('symbol')} {row.get('signal_action')} {row.get('signal_status')} source={row.get('signal_source')} reason={row.get('action_reason')}")
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
