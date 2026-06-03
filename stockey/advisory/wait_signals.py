from __future__ import annotations

import argparse
import json
import uuid
from typing import Any

import pandas as pd

from advisory.hypothesis_engine import ACTION_PLANS_TABLE, MATCHES_TABLE, parse_jsonish
from utils.db import db_session, sql_to_df, upsert_to_db


WAIT_SIGNALS_TABLE = "advisory_wait_signals"
WAIT_SIGNAL_MATCHES_TABLE = "advisory_wait_signal_matches"
POSITIVE_ACTIONS = {"BUY", "BUY_MORE", "BUY_WATCH", "WATCH_SYMBOLS", "ADD_TO_WATCHLIST", "WATCH"}
NEGATIVE_ACTIONS = {"REDUCE_EXPOSURE_REVIEW", "GO_CASH_REVIEW", "FULL_EXIT", "PARTIAL_EXIT", "SELL", "PARTIAL_SELL"}


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _text(value: Any, default: str | None = None) -> str | None:
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except Exception:
        pass
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


def _table_exists(table_name: str) -> bool:
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
    except Exception:
        return False


def ensure_tables() -> None:
    with db_session() as (_, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {WAIT_SIGNALS_TABLE} (
                signal_id TEXT PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL,
                hypothesis_id TEXT,
                hypothesis_title TEXT,
                source_table TEXT,
                source_key TEXT,
                symbol TEXT,
                scope TEXT,
                signal_type TEXT NOT NULL,
                status TEXT,
                priority BIGINT,
                expected_action TEXT,
                operator_summary TEXT,
                wait_question TEXT,
                condition_json TEXT,
                valid_from TIMESTAMPTZ,
                valid_until TIMESTAMPTZ,
                generated_by TEXT,
                load_ts TIMESTAMPTZ
            )
            """
        )
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {WAIT_SIGNAL_MATCHES_TABLE} (
                matched_at TIMESTAMPTZ NOT NULL,
                signal_id TEXT NOT NULL,
                hypothesis_id TEXT,
                symbol TEXT,
                signal_type TEXT,
                expected_action TEXT,
                match_status TEXT,
                match_score DOUBLE PRECISION,
                source_table TEXT,
                source_key TEXT,
                observed_at TIMESTAMPTZ,
                observed_value DOUBLE PRECISION,
                threshold_value DOUBLE PRECISION,
                match_reason TEXT,
                evidence_json TEXT,
                load_ts TIMESTAMPTZ,
                UNIQUE (signal_id, source_table, source_key)
            )
            """
        )
        cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{WAIT_SIGNALS_TABLE}_status_symbol ON {WAIT_SIGNALS_TABLE} (status, symbol, valid_until)")
        cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{WAIT_SIGNAL_MATCHES_TABLE}_matched ON {WAIT_SIGNAL_MATCHES_TABLE} (matched_at DESC, symbol)")


def make_signal_id(row: dict[str, Any]) -> str:
    payload = {
        "hypothesis_id": row.get("hypothesis_id"),
        "source_table": row.get("source_table"),
        "source_key": row.get("source_key"),
        "symbol": row.get("symbol"),
        "signal_type": row.get("signal_type"),
        "condition": row.get("condition_json") or row.get("condition"),
    }
    return str(uuid.uuid5(uuid.NAMESPACE_URL, json_dumps(payload)))


def load_recent_price(symbol: str, *, asof: pd.Timestamp | None = None) -> dict[str, Any] | None:
    if not _table_exists("dhan_ohlcv_daily"):
        return None
    clauses = ["exchange = 'NSE'", "UPPER(TRIM(ticker)) = %s"]
    params: list[Any] = [symbol.upper()]
    if asof is not None:
        clauses.append("date <= %s")
        params.append(asof)
    df = sql_to_df(
        f"""
        SELECT ticker AS symbol, date, open, high, low, close, volume
        FROM dhan_ohlcv_daily
        WHERE {' AND '.join(clauses)}
        ORDER BY date DESC
        LIMIT 1
        """,
        params=tuple(params),
    )
    if df.empty:
        return None
    return df.iloc[0].to_dict()


def _action_direction(action: Any) -> str:
    normalized = str(action or "").strip().upper()
    if normalized in NEGATIVE_ACTIONS or "REDUCE" in normalized or "EXIT" in normalized or "SELL" in normalized:
        return "negative"
    if normalized in POSITIVE_ACTIONS or "BUY" in normalized or "WATCH" in normalized:
        return "positive"
    return "review"


def _valid_until(planned_at: pd.Timestamp, action_plan_json: Any) -> pd.Timestamp:
    payload = parse_jsonish(action_plan_json, {})
    days = 7
    if isinstance(payload, dict):
        days = int(payload.get("follow_up_window_days") or days)
    return planned_at + pd.Timedelta(days=max(1, min(days, 60)))


def build_wait_signals_from_action_plans(action_plans: pd.DataFrame, matches: pd.DataFrame) -> pd.DataFrame:
    if action_plans.empty:
        return pd.DataFrame()
    match_lookup = {
        (str(row.get("hypothesis_id")), str(row.get("source_table")), str(row.get("source_key"))): row
        for _, row in matches.iterrows()
    } if not matches.empty else {}
    rows: list[dict[str, Any]] = []
    now = pd.Timestamp.utcnow()
    for _, plan in action_plans.iterrows():
        hypothesis_id = _text(plan.get("hypothesis_id"))
        source_table = _text(plan.get("source_table"))
        source_key = _text(plan.get("source_key"))
        matched = match_lookup.get((str(hypothesis_id), str(source_table), str(source_key)))
        symbol = (_text(plan.get("symbol")) or _text(matched.get("symbol") if matched is not None else None) or "").upper()
        action = _text(plan.get("action_type")) or _text(plan.get("suggested_action")) or "MANUAL_REVIEW"
        direction = _action_direction(action)
        planned_at = _ts(plan.get("planned_at")) or now
        valid_until = _valid_until(planned_at, plan.get("action_plan_json"))
        matched_terms = parse_jsonish(matched.get("matched_terms_json") if matched is not None else None, [])
        hypothesis_title = _text(matched.get("hypothesis_title") if matched is not None else None) or hypothesis_id
        summary = _text(plan.get("operator_summary")) or _text(plan.get("decision_reason")) or "Wait for follow-up confirmation before action."
        base = {
            "created_at": now,
            "hypothesis_id": hypothesis_id,
            "hypothesis_title": hypothesis_title,
            "source_table": source_table,
            "source_key": source_key,
            "symbol": symbol or None,
            "scope": "symbol" if symbol else "market",
            "status": "active",
            "priority": 80 if direction == "negative" else 60,
            "expected_action": action,
            "operator_summary": summary,
            "valid_from": planned_at,
            "valid_until": valid_until,
            "generated_by": "hypothesis_action_plan",
            "load_ts": now,
        }
        if symbol:
            price = load_recent_price(symbol, asof=planned_at)
            close = _num((price or {}).get("close"))
            if close:
                if direction == "negative":
                    operator = "close_below"
                    threshold = round(close * 0.98, 4)
                    question = f"Has {symbol} closed below {threshold}, confirming adverse price reaction?"
                else:
                    operator = "close_above"
                    threshold = round(close * 1.02, 4)
                    question = f"Has {symbol} closed above {threshold}, confirming constructive price reaction?"
                condition = {
                    "operator": operator,
                    "threshold": threshold,
                    "anchor_close": close,
                    "anchor_date": str((price or {}).get("date") or ""),
                    "price_source": "dhan_ohlcv_daily",
                }
                row = {**base, "signal_type": "price_close", "wait_question": question, "condition_json": json_dumps(condition)}
                row["signal_id"] = make_signal_id(row)
                rows.append(row)
        terms = [str(term).strip().lower() for term in matched_terms if str(term).strip()]
        if terms:
            condition = {
                "keywords": terms[:12],
                "sources": ["news", "announcements", "announcement_documents"],
                "match_after_source_key": source_key,
                "direction": direction,
            }
            question = "Has fresh news or announcement evidence confirmed or contradicted this playbook after the original match?"
            row = {**base, "signal_type": "event_keywords", "wait_question": question, "condition_json": json_dumps(condition)}
            row["signal_id"] = make_signal_id(row)
            rows.append(row)
    return pd.DataFrame(rows)


def load_action_plans_for_wait_signals(*, hypothesis_id: str | None = None, limit: int = 100) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not _table_exists(ACTION_PLANS_TABLE):
        return pd.DataFrame(), pd.DataFrame()
    clauses = ["1 = 1"]
    params: list[Any] = []
    if hypothesis_id:
        clauses.append("p.hypothesis_id = %s")
        params.append(hypothesis_id)
    plans = sql_to_df(
        f"""
        SELECT p.*
        FROM {ACTION_PLANS_TABLE} p
        WHERE {' AND '.join(clauses)}
        ORDER BY p.planned_at DESC
        LIMIT %s
        """,
        params=tuple([*params, max(1, int(limit))]),
    )
    if plans.empty or not _table_exists(MATCHES_TABLE):
        return plans, pd.DataFrame()
    keys = plans[["hypothesis_id", "source_table", "source_key"]].dropna().drop_duplicates()
    matches = sql_to_df(
        f"""
        SELECT m.*
        FROM {MATCHES_TABLE} m
        JOIN (
            SELECT * FROM (VALUES {', '.join(['(%s, %s, %s)'] * len(keys))}) AS v(hypothesis_id, source_table, source_key)
        ) k
          ON k.hypothesis_id = m.hypothesis_id
         AND k.source_table = m.source_table
         AND k.source_key = m.source_key
        """,
        params=tuple(value for row in keys.to_dict(orient="records") for value in [row["hypothesis_id"], row["source_table"], row["source_key"]]),
    ) if not keys.empty else pd.DataFrame()
    return plans, matches


def persist_wait_signals(signals: pd.DataFrame) -> None:
    ensure_tables()
    if signals.empty:
        return
    out = signals.copy()
    for column in ["created_at", "valid_from", "valid_until", "load_ts"]:
        if column in out.columns:
            out[column] = pd.to_datetime(out[column], utc=True, errors="coerce")
    if "priority" in out.columns:
        out["priority"] = pd.to_numeric(out["priority"], errors="coerce").astype("Int64")
    upsert_to_db(out, WAIT_SIGNALS_TABLE, unique_keys=["signal_id"])


def generate_wait_signals(*, hypothesis_id: str | None = None, limit: int = 100, persist: bool = True) -> dict[str, Any]:
    ensure_tables()
    plans, matches = load_action_plans_for_wait_signals(hypothesis_id=hypothesis_id, limit=limit)
    signals = build_wait_signals_from_action_plans(plans, matches)
    if persist:
        persist_wait_signals(signals)
    return {
        "status": "ok",
        "action_plan_rows": int(len(plans)),
        "signal_rows": int(len(signals)),
        "signals": signals.head(100).to_dict(orient="records") if not signals.empty else [],
        "dry_run": not persist,
    }


def load_active_wait_signals(*, symbols: list[str] | None = None, limit: int = 250) -> pd.DataFrame:
    ensure_tables()
    clauses = ["status = 'active'", "(valid_until IS NULL OR valid_until >= %s)"]
    params: list[Any] = [pd.Timestamp.utcnow()]
    normalized_symbols = sorted({str(symbol).strip().upper() for symbol in (symbols or []) if str(symbol or "").strip()})
    if normalized_symbols:
        clauses.append("(symbol IS NULL OR UPPER(TRIM(symbol)) = ANY(%s))")
        params.append(normalized_symbols)
    df = sql_to_df(
        f"""
        SELECT *
        FROM {WAIT_SIGNALS_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY priority DESC NULLS LAST, valid_until ASC NULLS LAST, created_at DESC
        LIMIT %s
        """,
        params=tuple([*params, max(1, min(int(limit), 1000))]),
    )
    return df


def _load_source_events_for_match(*, from_ts: pd.Timestamp, to_ts: pd.Timestamp, symbol: str | None = None) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for table, source_type in [("advisory_news_events", "news"), ("advisory_watch_events", "announcement"), ("announcement_pipeline_documents", "announcement_document")]:
        if not _table_exists(table):
            continue
        symbol_column = "ticker" if table == "announcement_pipeline_documents" else "symbol"
        text_column = "concise_summary_text"
        key_column = "unique_id"
        url_select = "attachment_url AS source_url" if table == "announcement_pipeline_documents" else "source_url" if table == "advisory_news_events" else "NULL::TEXT AS source_url"
        clauses = ["published_on >= %s", "published_on <= %s"]
        params: list[Any] = [from_ts, to_ts]
        if symbol:
            clauses.append(f"UPPER(TRIM({symbol_column})) = %s")
            params.append(symbol.upper())
        frames.append(
            sql_to_df(
                f"""
                SELECT
                    %s AS source_type,
                    %s AS source_table,
                    {key_column} AS source_key,
                    published_on,
                    {symbol_column} AS symbol,
                    subject,
                    {text_column} AS concise_summary_text,
                    {url_select}
                FROM {table}
                WHERE {' AND '.join(clauses)}
                ORDER BY published_on DESC
                LIMIT 200
                """,
                params=tuple([source_type, table, *params]),
            )
        )
    frames = [frame for frame in frames if not frame.empty]
    return pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()


def _match_price_signal(signal: dict[str, Any]) -> dict[str, Any] | None:
    symbol = _text(signal.get("symbol"))
    if not symbol:
        return None
    condition = parse_jsonish(signal.get("condition_json"), {})
    operator = str(condition.get("operator") or "").strip().lower()
    threshold = _num(condition.get("threshold"))
    if not operator or threshold is None:
        return None
    price = load_recent_price(symbol)
    close = _num((price or {}).get("close"))
    observed_at = _ts((price or {}).get("date"))
    if close is None or observed_at is None:
        return None
    matched = (operator == "close_above" and close >= threshold) or (operator == "close_below" and close <= threshold)
    if not matched:
        return None
    return {
        "source_table": "dhan_ohlcv_daily",
        "source_key": f"{symbol}:{observed_at.date()}",
        "observed_at": observed_at,
        "observed_value": close,
        "threshold_value": threshold,
        "match_score": 1.0,
        "match_reason": f"{symbol} {operator.replace('_', ' ')} {threshold}; latest close {close}.",
        "evidence": price or {},
    }


def _match_event_signal(signal: dict[str, Any]) -> dict[str, Any] | None:
    condition = parse_jsonish(signal.get("condition_json"), {})
    keywords = [str(term).strip().lower() for term in condition.get("keywords", []) if str(term).strip()]
    if not keywords:
        return None
    valid_from = _ts(signal.get("valid_from")) or (pd.Timestamp.utcnow() - pd.Timedelta(days=7))
    valid_until = _ts(signal.get("valid_until")) or pd.Timestamp.utcnow()
    events = _load_source_events_for_match(from_ts=valid_from, to_ts=min(valid_until, pd.Timestamp.utcnow()), symbol=_text(signal.get("symbol")))
    if events.empty:
        return None
    for _, row in events.iterrows():
        text = " ".join(str(row.get(column) or "").lower() for column in ["subject", "concise_summary_text"])
        matched_terms = [term for term in keywords if term in text]
        if not matched_terms:
            continue
        return {
            "source_table": row.get("source_table"),
            "source_key": row.get("source_key"),
            "observed_at": _ts(row.get("published_on")),
            "observed_value": None,
            "threshold_value": None,
            "match_score": min(1.0, len(matched_terms) / max(len(keywords), 1)),
            "match_reason": f"Matched wait-signal term(s): {', '.join(matched_terms[:6])}.",
            "evidence": row.to_dict() | {"matched_terms": matched_terms},
        }
    return None


def match_wait_signals(*, symbols: list[str] | None = None, limit: int = 250, persist: bool = True) -> dict[str, Any]:
    ensure_tables()
    active = load_active_wait_signals(symbols=symbols, limit=limit)
    rows: list[dict[str, Any]] = []
    now = pd.Timestamp.utcnow()
    for _, signal in active.iterrows():
        signal_row = signal.to_dict()
        signal_type = str(signal_row.get("signal_type") or "").strip().lower()
        match = _match_price_signal(signal_row) if signal_type == "price_close" else _match_event_signal(signal_row) if signal_type == "event_keywords" else None
        if not match:
            continue
        rows.append(
            {
                "matched_at": now,
                "signal_id": signal_row.get("signal_id"),
                "hypothesis_id": signal_row.get("hypothesis_id"),
                "symbol": signal_row.get("symbol"),
                "signal_type": signal_row.get("signal_type"),
                "expected_action": signal_row.get("expected_action"),
                "match_status": "matched",
                "match_score": match.get("match_score"),
                "source_table": match.get("source_table"),
                "source_key": match.get("source_key"),
                "observed_at": match.get("observed_at"),
                "observed_value": match.get("observed_value"),
                "threshold_value": match.get("threshold_value"),
                "match_reason": match.get("match_reason"),
                "evidence_json": json_dumps(match.get("evidence") or {}),
                "load_ts": now,
            }
        )
    matches = pd.DataFrame(rows)
    if persist and not matches.empty:
        out = matches.copy()
        for column in ["matched_at", "observed_at", "load_ts"]:
            out[column] = pd.to_datetime(out[column], utc=True, errors="coerce")
        for column in ["match_score", "observed_value", "threshold_value"]:
            out[column] = pd.to_numeric(out[column], errors="coerce")
        upsert_to_db(out, WAIT_SIGNAL_MATCHES_TABLE, unique_keys=["signal_id", "source_table", "source_key"])
        with db_session() as (_, cur):
            cur.execute(
                f"UPDATE {WAIT_SIGNALS_TABLE} SET status = 'matched', load_ts = %s WHERE signal_id = ANY(%s)",
                (now, matches["signal_id"].dropna().astype(str).tolist()),
            )
    return {
        "status": "ok",
        "active_signal_rows": int(len(active)),
        "matched_rows": int(len(matches)),
        "matches": matches.head(100).to_dict(orient="records") if not matches.empty else [],
        "dry_run": not persist,
    }


def load_wait_signals(*, status: str | None = None, symbol: str | None = None, limit: int = 100) -> pd.DataFrame:
    ensure_tables()
    clauses = ["1 = 1"]
    params: list[Any] = []
    if status and str(status).lower() not in {"all", "*"}:
        clauses.append("status = %s")
        params.append(str(status).lower())
    if symbol:
        clauses.append("UPPER(TRIM(symbol)) = %s")
        params.append(str(symbol).strip().upper())
    return sql_to_df(
        f"""
        SELECT *
        FROM {WAIT_SIGNALS_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY
          CASE status WHEN 'matched' THEN 0 WHEN 'active' THEN 1 ELSE 2 END,
          COALESCE(valid_until, created_at) DESC NULLS LAST,
          priority DESC NULLS LAST
        LIMIT %s
        """,
        params=tuple([*params, max(1, int(limit))]),
    )


def load_wait_signal_matches(*, signal_id: str | None = None, limit: int = 100) -> pd.DataFrame:
    ensure_tables()
    clauses = ["1 = 1"]
    params: list[Any] = []
    if signal_id:
        clauses.append("signal_id = %s")
        params.append(signal_id)
    return sql_to_df(
        f"""
        SELECT *
        FROM {WAIT_SIGNAL_MATCHES_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY matched_at DESC
        LIMIT %s
        """,
        params=tuple([*params, max(1, int(limit))]),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate and match hypothesis wait signals.")
    parser.add_argument("--generate", action="store_true", help="Generate wait signals from playbook action plans.")
    parser.add_argument("--match", action="store_true", help="Match active wait signals against current data.")
    parser.add_argument("--hypothesis-id")
    parser.add_argument("--symbol", action="append", default=[])
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--format", choices=["json", "text"], default="json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    results: dict[str, Any] = {}
    if args.generate or not args.match:
        results["generate"] = generate_wait_signals(hypothesis_id=args.hypothesis_id, limit=args.limit, persist=not bool(args.dry_run))
    if args.match:
        results["match"] = match_wait_signals(symbols=args.symbol, limit=args.limit, persist=not bool(args.dry_run))
    if args.format == "text":
        for key, payload in results.items():
            print(f"{key}: status={payload.get('status')} signals={payload.get('signal_rows', payload.get('active_signal_rows', 0))} matches={payload.get('matched_rows', 0)} dry_run={payload.get('dry_run')}")
    else:
        print(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
