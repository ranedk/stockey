from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from advisory.fallback_telemetry import record_local_fallback_event
from advisory.hypothesis_engine import ACTION_PLANS_TABLE, MATCHES_TABLE, parse_jsonish
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration


WAIT_SIGNALS_TABLE = "advisory_wait_signals"
WAIT_SIGNAL_MATCHES_TABLE = "advisory_wait_signal_matches"
WAIT_SIGNALS_SCHEMA_MIGRATION_ID = "20260611_advisory_wait_signals_base"
WAIT_SIGNALS_SCHEMA_STATEMENTS = [
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
    """,
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
    """,
    f"CREATE INDEX IF NOT EXISTS idx_{WAIT_SIGNALS_TABLE}_status_symbol ON {WAIT_SIGNALS_TABLE} (status, symbol, valid_until)",
    f"CREATE INDEX IF NOT EXISTS idx_{WAIT_SIGNAL_MATCHES_TABLE}_matched ON {WAIT_SIGNAL_MATCHES_TABLE} (matched_at DESC, symbol)",
]
POSITIVE_ACTIONS = {"BUY", "BUY_MORE", "BUY_WATCH", "WATCH_SYMBOLS", "ADD_TO_WATCHLIST", "WATCH"}
NEGATIVE_ACTIONS = {"REDUCE_EXPOSURE_REVIEW", "GO_CASH_REVIEW", "FULL_EXIT", "PARTIAL_EXIT", "SELL", "PARTIAL_SELL"}
PRICE_CONDITION_TYPES = {"price_level"}
EVENT_CONDITION_TYPES = {"event_keywords", "clarification_filing", "result_update", "management_commentary", "sector_event"}
PASSIVE_CONDITION_TYPES = {"expiry_only"}
WAIT_SIGNAL_CONDITION_TYPES = PRICE_CONDITION_TYPES | EVENT_CONDITION_TYPES | PASSIVE_CONDITION_TYPES
PRICE_OPERATORS = {"close_above", "close_below", "above", "below", "gte", "lte"}
DEFAULT_EVENT_KEYWORDS: dict[str, list[str]] = {
    "clarification_filing": ["clarification", "clarifies", "clarified", "response to clarification", "exchange clarification"],
    "result_update": ["financial results", "quarterly results", "earnings", "profit", "revenue", "margin"],
    "management_commentary": ["management commentary", "outlook", "guidance", "conference call", "investor presentation"],
    "sector_event": ["sector", "industry", "policy", "regulation", "demand", "prices"],
}
DEFAULT_EVENT_SOURCES = ["news", "announcement", "announcement_document"]


def _record_wait_signal_source_fallback(
    *,
    fallback_type: str,
    source: str,
    reason: str,
    error: Exception | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="advisory.wait_signals",
        fallback_type=fallback_type,
        source=source,
        severity="warn",
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


@dataclass(frozen=True)
class WaitSignalCondition:
    condition_type: str
    valid: bool
    issue_reason: str | None = None
    operator: str | None = None
    threshold: float | None = None
    source: str | None = None
    observed_field: str = "close"
    keywords: list[str] = field(default_factory=list)
    required_keywords: list[str] = field(default_factory=list)
    context_keywords: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    direction: str | None = None
    source_quality: str | None = None
    required_evidence_fields: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        payload = dict(self.raw)
        payload["condition_type"] = self.condition_type
        if self.operator:
            payload["operator"] = self.operator
        if self.threshold is not None:
            payload["threshold"] = self.threshold
        if self.source:
            payload["source"] = self.source
        if self.observed_field:
            payload["observed_field"] = self.observed_field
        if self.keywords:
            payload["keywords"] = self.keywords
        if self.required_keywords:
            payload["required_keywords"] = self.required_keywords
        if self.context_keywords:
            payload["context_keywords"] = self.context_keywords
        if self.sources:
            payload["sources"] = self.sources
        if self.direction:
            payload["direction"] = self.direction
        if self.source_quality:
            payload["source_quality"] = self.source_quality
        if self.required_evidence_fields:
            payload["required_evidence_fields"] = self.required_evidence_fields
        if self.issue_reason:
            payload["issue_reason"] = self.issue_reason
        return payload


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
            module="advisory.wait_signals",
            fallback_type="wait_signals_text_missing_check_failed",
            source="text",
            severity="warn",
            reason="Wait-signal normalization could not evaluate missingness while normalizing text and kept string conversion fallback.",
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


def infer_event_condition_type(text: str | None) -> str:
    normalized = str(text or "").lower()
    if any(term in normalized for term in ["clarification", "clarify", "exchange query", "reply to query", "filing"]):
        return "clarification_filing"
    if any(term in normalized for term in ["result", "results", "earnings", "quarter", "q1", "q2", "q3", "q4"]):
        return "result_update"
    if any(term in normalized for term in ["management", "commentary", "guidance", "concalls", "concall", "investor call"]):
        return "management_commentary"
    if any(term in normalized for term in ["sector", "policy", "regulation", "regulatory", "government", "ministry"]):
        return "sector_event"
    return "event_keywords"


def _dedupe_text(values: list[Any], *, limit: int = 24) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _text(value)
        if not text:
            continue
        normalized = " ".join(text.lower().split())
        if normalized and normalized not in seen:
            out.append(normalized)
            seen.add(normalized)
    return out[:limit]


def _normalize_event_sources(values: Any) -> list[str]:
    if isinstance(values, str):
        raw_values = [values]
    elif isinstance(values, list):
        raw_values = values
    else:
        raw_values = DEFAULT_EVENT_SOURCES
    aliases = {
        "announcements": "announcement",
        "announcement_documents": "announcement_document",
        "documents": "announcement_document",
    }
    normalized = [aliases.get(str(value).strip().lower(), str(value).strip().lower()) for value in raw_values if str(value or "").strip()]
    return _dedupe_text(normalized, limit=8) or list(DEFAULT_EVENT_SOURCES)


def _normalize_keyword_input(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [value]
    return []


def build_price_level_condition(
    *,
    operator: str,
    threshold: Any,
    source: str = "dhan_ohlcv_daily",
    observed_field: str = "close",
    **extra: Any,
) -> dict[str, Any]:
    condition = normalize_wait_condition(
        {
            **extra,
            "condition_type": "price_level",
            "operator": operator,
            "threshold": threshold,
            "source": source,
            "observed_field": observed_field,
        }
    )
    return condition.to_json_dict()


def build_event_condition(
    *,
    condition_type: str = "event_keywords",
    keywords: list[Any] | None = None,
    sources: list[Any] | None = None,
    direction: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    condition = normalize_wait_condition(
        {
            **extra,
            "condition_type": condition_type,
            "keywords": keywords or [],
            "sources": sources or DEFAULT_EVENT_SOURCES,
            "direction": direction,
        }
    )
    return condition.to_json_dict()


def normalize_wait_condition(raw_condition: Any, *, signal_type: Any = None) -> WaitSignalCondition:
    raw = parse_jsonish(raw_condition, {}) if not isinstance(raw_condition, dict) else dict(raw_condition)
    if not isinstance(raw, dict):
        raw = {}
    raw_type = str(raw.get("condition_type") or "").strip().lower()
    legacy_signal_type = str(signal_type or raw.get("signal_type") or "").strip().lower()
    condition_type = raw_type
    if not condition_type:
        if legacy_signal_type in {"price_close", "price_level"} or raw.get("operator") is not None or raw.get("threshold") is not None:
            condition_type = "price_level"
        elif legacy_signal_type in EVENT_CONDITION_TYPES or raw.get("keywords") is not None:
            condition_type = legacy_signal_type if legacy_signal_type in EVENT_CONDITION_TYPES else "event_keywords"
        elif legacy_signal_type == "expiry_only":
            condition_type = "expiry_only"
    if condition_type not in WAIT_SIGNAL_CONDITION_TYPES:
        label = raw_type or legacy_signal_type or "missing"
        return WaitSignalCondition(
            condition_type=label,
            valid=False,
            issue_reason=f"Unknown wait-signal condition_type: {label}",
            raw=raw,
        )
    if condition_type == "price_level":
        operator = str(raw.get("operator") or "").strip().lower()
        threshold = _num(raw.get("threshold"))
        if operator == "close_above":
            operator = "gte"
        elif operator == "close_below":
            operator = "lte"
        if operator not in PRICE_OPERATORS:
            return WaitSignalCondition(condition_type=condition_type, valid=False, issue_reason="price_level condition missing valid operator", raw=raw)
        if threshold is None:
            return WaitSignalCondition(condition_type=condition_type, valid=False, issue_reason="price_level condition missing numeric threshold", raw=raw)
        return WaitSignalCondition(
            condition_type=condition_type,
            valid=True,
            operator=operator,
            threshold=threshold,
            source=_text(raw.get("source")) or _text(raw.get("price_source")) or "dhan_ohlcv_daily",
            observed_field=_text(raw.get("observed_field")) or "close",
            raw=raw,
        )
    if condition_type in EVENT_CONDITION_TYPES:
        defaults = _dedupe_text(DEFAULT_EVENT_KEYWORDS.get(condition_type, []), limit=12)
        if raw.get("required_keywords") is not None:
            required_keywords = _dedupe_text(_normalize_keyword_input(raw.get("required_keywords")), limit=24)
        else:
            required_keywords = _dedupe_text(_normalize_keyword_input(raw.get("keywords")), limit=24)
        context_keywords = _dedupe_text(_normalize_keyword_input(raw.get("context_keywords")) or defaults, limit=12)
        keywords = _dedupe_text([*required_keywords, *context_keywords], limit=36)
        if not required_keywords:
            return WaitSignalCondition(condition_type=condition_type, valid=False, issue_reason=f"{condition_type} condition missing required operator keywords", raw=raw)
        return WaitSignalCondition(
            condition_type=condition_type,
            valid=True,
            keywords=keywords,
            required_keywords=required_keywords,
            context_keywords=context_keywords,
            sources=_normalize_event_sources(raw.get("sources")),
            direction=_text(raw.get("direction")),
            source_quality=_text(raw.get("source_quality")) or "fresh timestamped source with readable subject or summary",
            required_evidence_fields=["source_table", "source_key", "published_on", "subject", "concise_summary_text"],
            raw=raw,
        )
    return WaitSignalCondition(
        condition_type="expiry_only",
        valid=True,
        issue_reason="expiry_only waits do not actively match; they close by valid_until.",
        raw=raw,
    )


def wait_signal_condition_from_row(signal: dict[str, Any]) -> WaitSignalCondition:
    return normalize_wait_condition(signal.get("condition_json"), signal_type=signal.get("signal_type"))


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
    except Exception as exc:
        _record_wait_signal_source_fallback(
            fallback_type="wait_signal_source_table_lookup_failed",
            source=table_name,
            reason="Wait-signal matching skipped a source because table existence lookup failed.",
            error=exc,
        )
        return False


def _table_columns(table_name: str) -> set[str]:
    try:
        df = sql_to_df(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = %s
            """,
            params=(table_name,),
        )
        if df.empty or "column_name" not in df.columns:
            return set()
        return set(df["column_name"].dropna().astype(str).tolist())
    except Exception as exc:
        print(f"[advisory.wait_signals] source_column_check_failed table={table_name} error={type(exc).__name__}:{exc}", file=sys.stderr)
        _record_wait_signal_source_fallback(
            fallback_type="wait_signal_source_column_check_failed",
            source=table_name,
            reason="Wait-signal matching skipped a source because source-column lookup failed.",
            error=exc,
        )
        return set()


def ensure_tables() -> None:
    apply_schema_migration(
        migration_id=WAIT_SIGNALS_SCHEMA_MIGRATION_ID,
        description="Create wait-signal and wait-signal match tables.",
        statements=WAIT_SIGNALS_SCHEMA_STATEMENTS,
        metadata={"tables": [WAIT_SIGNALS_TABLE, WAIT_SIGNAL_MATCHES_TABLE]},
    )


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
                condition = build_price_level_condition(
                    operator=operator,
                    threshold=threshold,
                    anchor_close=close,
                    anchor_date=str((price or {}).get("date") or ""),
                )
                row = {**base, "signal_type": "price_level", "wait_question": question, "condition_json": json_dumps(condition)}
                row["signal_id"] = make_signal_id(row)
                rows.append(row)
        terms = [str(term).strip().lower() for term in matched_terms if str(term).strip()]
        if terms:
            condition = build_event_condition(
                condition_type="event_keywords",
                keywords=terms[:12],
                sources=DEFAULT_EVENT_SOURCES,
                match_after_source_key=source_key,
                direction=direction,
            )
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


def load_active_wait_signals(*, symbols: list[str] | None = None, limit: int = 250, include_market: bool = False) -> pd.DataFrame:
    ensure_tables()
    clauses = ["status = 'active'", "(valid_until IS NULL OR valid_until >= %s)"]
    params: list[Any] = [pd.Timestamp.utcnow()]
    normalized_symbols = sorted({str(symbol).strip().upper() for symbol in (symbols or []) if str(symbol or "").strip()})
    if normalized_symbols:
        clauses.append("(symbol IS NULL OR UPPER(TRIM(symbol)) = ANY(%s))" if include_market else "UPPER(TRIM(symbol)) = ANY(%s)")
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
        columns = _table_columns(table)
        required_columns = {symbol_column, text_column, key_column, "published_on", "subject"}
        missing_columns = sorted(required_columns - columns)
        if missing_columns:
            print(f"[advisory.wait_signals] source_table_skipped table={table} missing_columns={','.join(missing_columns)}", file=sys.stderr)
            _record_wait_signal_source_fallback(
                fallback_type="wait_signal_source_table_missing_columns",
                source=table,
                reason="Wait-signal matching skipped a source because required columns were missing.",
                metadata={"missing_columns": missing_columns},
            )
            continue
        if table == "announcement_pipeline_documents" and "attachment_url" in columns:
            url_select = "attachment_url AS source_url"
        elif "source_url" in columns:
            url_select = "source_url"
        else:
            url_select = "NULL::TEXT AS source_url"
        clauses = ["published_on >= %s", "published_on <= %s"]
        params: list[Any] = [from_ts, to_ts]
        if symbol:
            clauses.append(f"UPPER(TRIM({symbol_column})) = %s")
            params.append(symbol.upper())
        try:
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
        except Exception as exc:
            print(f"[advisory.wait_signals] source_event_load_failed table={table} error={type(exc).__name__}:{exc}", file=sys.stderr)
            _record_wait_signal_source_fallback(
                fallback_type="wait_signal_source_event_load_failed",
                source=table,
                reason="Wait-signal matching skipped source events because event evidence loading failed.",
                error=exc,
                metadata={"symbol": symbol, "from_ts": str(from_ts), "to_ts": str(to_ts)},
            )
    frames = [frame for frame in frames if not frame.empty]
    return pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()


def _match_price_signal(signal: dict[str, Any], condition: WaitSignalCondition) -> dict[str, Any] | None:
    symbol = _text(signal.get("symbol"))
    if not symbol:
        return None
    operator = condition.operator
    threshold = condition.threshold
    if not operator or threshold is None:
        return None
    price = load_recent_price(symbol)
    close = _num((price or {}).get(condition.observed_field or "close"))
    observed_at = _ts((price or {}).get("date"))
    if close is None or observed_at is None:
        return None
    matched = (operator in {"close_above", "above", "gte"} and close >= threshold) or (operator in {"close_below", "below", "lte"} and close <= threshold)
    if not matched:
        return None
    readable_operator = "above" if operator in {"close_above", "above", "gte"} else "below"
    return {
        "source_table": condition.source or "dhan_ohlcv_daily",
        "source_key": f"{symbol}:{observed_at.date()}",
        "observed_at": observed_at,
        "observed_value": close,
        "threshold_value": threshold,
        "match_score": 1.0,
        "match_reason": f"{symbol} latest {condition.observed_field or 'close'} is {readable_operator} {threshold}; observed {close}.",
        "evidence": (price or {}) | {"condition_type": condition.condition_type, "operator": operator, "observed_field": condition.observed_field},
    }


def _match_event_signal(signal: dict[str, Any], condition: WaitSignalCondition) -> dict[str, Any] | None:
    keywords = condition.keywords
    required_keywords = condition.required_keywords or condition.keywords
    if not keywords or not required_keywords:
        return None
    valid_from = _ts(signal.get("valid_from")) or (pd.Timestamp.utcnow() - pd.Timedelta(days=7))
    valid_until = _ts(signal.get("valid_until")) or pd.Timestamp.utcnow()
    events = _load_source_events_for_match(from_ts=valid_from, to_ts=min(valid_until, pd.Timestamp.utcnow()), symbol=_text(signal.get("symbol")))
    if events.empty:
        return None
    if condition.sources and "source_type" in events.columns:
        events = events[events["source_type"].astype(str).str.lower().isin(condition.sources)]
        if events.empty:
            return None
    for _, row in events.iterrows():
        text = " ".join(str(row.get(column) or "").lower() for column in ["subject", "concise_summary_text"])
        matched_required_terms = [term for term in required_keywords if term in text]
        if not matched_required_terms:
            continue
        matched_terms = [term for term in keywords if term in text]
        if not matched_terms:
            continue
        return {
            "source_table": row.get("source_table"),
            "source_key": row.get("source_key"),
            "observed_at": _ts(row.get("published_on")),
            "observed_value": None,
            "threshold_value": None,
            "match_score": min(1.0, len(matched_required_terms) / max(len(required_keywords), 1)),
            "match_reason": f"Matched {condition.condition_type.replace('_', ' ')} required wait term(s): {', '.join(matched_required_terms[:6])}.",
            "evidence": row.to_dict()
            | {
                "condition_type": condition.condition_type,
                "matched_terms": matched_terms,
                "matched_required_terms": matched_required_terms,
                "source_quality": condition.source_quality,
                "required_evidence_fields": condition.required_evidence_fields,
            },
        }
    return None


def match_wait_signals(*, symbols: list[str] | None = None, limit: int = 250, persist: bool = True, include_market: bool = False) -> dict[str, Any]:
    ensure_tables()
    active = load_active_wait_signals(symbols=symbols, limit=limit, include_market=include_market)
    rows: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    now = pd.Timestamp.utcnow()
    for _, signal in active.iterrows():
        signal_row = signal.to_dict()
        condition = wait_signal_condition_from_row(signal_row)
        if not condition.valid:
            issues.append(
                {
                    "signal_id": signal_row.get("signal_id"),
                    "symbol": signal_row.get("symbol"),
                    "condition_type": condition.condition_type,
                    "issue_reason": condition.issue_reason,
                }
            )
            continue
        if condition.condition_type == "expiry_only":
            issues.append(
                {
                    "signal_id": signal_row.get("signal_id"),
                    "symbol": signal_row.get("symbol"),
                    "condition_type": condition.condition_type,
                    "issue_reason": condition.issue_reason,
                }
            )
            continue
        match = _match_price_signal(signal_row, condition) if condition.condition_type in PRICE_CONDITION_TYPES else _match_event_signal(signal_row, condition)
        if not match:
            continue
        manual_review_item_id = _text(condition.raw.get("manual_review_item_id"))
        if not manual_review_item_id and _text(signal_row.get("generated_by")) == "manual_review_decision":
            manual_review_item_id = _text(signal_row.get("source_key"))
        rows.append(
            {
                "matched_at": now,
                "signal_id": signal_row.get("signal_id"),
                "hypothesis_id": signal_row.get("hypothesis_id"),
                "symbol": signal_row.get("symbol"),
                "signal_type": condition.condition_type,
                "expected_action": signal_row.get("expected_action"),
                "match_status": "matched",
                "match_score": match.get("match_score"),
                "source_table": match.get("source_table"),
                "source_key": match.get("source_key"),
                "observed_at": match.get("observed_at"),
                "observed_value": match.get("observed_value"),
                "threshold_value": match.get("threshold_value"),
                "match_reason": match.get("match_reason"),
                "evidence_json": json_dumps(
                    (match.get("evidence") or {})
                    | {
                        "wait_signal": {
                            "signal_id": signal_row.get("signal_id"),
                            "source_table": signal_row.get("source_table"),
                            "source_key": signal_row.get("source_key"),
                            "generated_by": signal_row.get("generated_by"),
                            "manual_review_item_id": manual_review_item_id,
                            "manual_review_source_table": signal_row.get("source_table"),
                            "manual_review_source_key": signal_row.get("source_key"),
                            "wait_question": signal_row.get("wait_question"),
                            "operator_summary": signal_row.get("operator_summary"),
                            "expected_action": signal_row.get("expected_action"),
                            "condition_type": condition.condition_type,
                        }
                    }
                ),
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
        signal_ids = matches["signal_id"].dropna().astype(str).tolist()

        def _mark_wait_signals_matched() -> None:
            with db_session() as (_, cur):
                cur.execute(
                    f"UPDATE {WAIT_SIGNALS_TABLE} SET status = 'matched', load_ts = %s WHERE signal_id = ANY(%s)",
                    (now, signal_ids),
                )

        execute_db_operation(
            _mark_wait_signals_matched,
            operation_name="wait_signals:mark_matched",
        )
    return {
        "status": "ok",
        "active_signal_rows": int(len(active)),
        "matched_rows": int(len(matches)),
        "issue_rows": int(len(issues)),
        "issues": issues[:100],
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


def load_wait_signal_matches(*, signal_id: str | None = None, signal_ids: list[str] | None = None, symbol: str | None = None, limit: int = 100) -> pd.DataFrame:
    ensure_tables()
    clauses = ["1 = 1"]
    params: list[Any] = []
    if signal_id:
        clauses.append("signal_id = %s")
        params.append(signal_id)
    normalized_signal_ids = sorted({str(item).strip() for item in (signal_ids or []) if str(item or "").strip()})
    if normalized_signal_ids:
        clauses.append("signal_id = ANY(%s)")
        params.append(normalized_signal_ids)
    if symbol:
        clauses.append("UPPER(TRIM(symbol)) = %s")
        params.append(str(symbol).strip().upper())
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
