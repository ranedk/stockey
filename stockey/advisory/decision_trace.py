from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from typing import Any

import pandas as pd

from advisory.fallback_telemetry import record_local_fallback_event
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.log import setup_logger
from utils.schema_migrations import apply_schema_migration


TRACES_TABLE = "advisory_decision_traces"
TRACE_STEPS_TABLE = "advisory_decision_trace_steps"
EVENT_PROCESSING_TABLE = "advisory_event_processing_runs"
ACTION_CONFLICTS_TABLE = "advisory_action_conflicts"
ACTION_CONFLICT_RULES_TABLE = "advisory_action_conflict_rules"
MANUAL_REVIEW_DECISIONS_TABLE = "advisory_manual_review_decisions"
WAIT_SIGNALS_TABLE = "advisory_wait_signals"
WAIT_SIGNAL_MATCHES_TABLE = "advisory_wait_signal_matches"
TRACE_SCHEMA_MIGRATION_ID = "20260611_advisory_decision_trace_base"

ACTION_CONFLICT_RULES = [
    {
        "rule_id": "EXIT_BEATS_ENTRY_OR_WATCH",
        "rule_name": "Exit/profit action beats entry, watch, and manual review",
        "rule_scope": "risk_management",
        "resolution_status": "resolved",
        "resolution_action": "keep_winner",
        "reason": "Risk management actions such as SELL, PARTIAL_SELL, TIGHTEN_STOP, or REDUCE_EXPOSURE_REVIEW override entry/watch/manual signals.",
    },
    {
        "rule_id": "MARKET_GATE_MANUAL_BEATS_POSITIVE",
        "rule_name": "Risk-off market gate keeps positive actions manual",
        "rule_scope": "market_context",
        "resolution_status": "resolved",
        "resolution_action": "keep_winner",
        "reason": "When broad market context blocks positive broker actions, MANUAL_REVIEW remains the winner until regime breadth improves.",
    },
    {
        "rule_id": "ADVERSARIAL_VETO_MANUAL_BEATS_POSITIVE_OR_WATCH",
        "rule_name": "Adversarial veto keeps event action manual",
        "rule_scope": "adversarial_review",
        "resolution_status": "resolved",
        "resolution_action": "keep_winner",
        "reason": "When adversarial review vetoes event evidence, MANUAL_REVIEW beats positive broker and watch candidates until the operator resolves the contradiction.",
    },
    {
        "rule_id": "EVENT_POLICY_REVIEW_BEATS_POSITIVE_ENTRY",
        "rule_name": "Event-policy review blocks positive entry/add-on",
        "rule_scope": "event_policy",
        "resolution_status": "resolved",
        "resolution_action": "keep_winner",
        "reason": "Event-policy MANUAL_REVIEW is an explicit evidence gate and beats BUY/BUY_MORE candidates until the event is reviewed.",
    },
    {
        "rule_id": "SAME_ACTION_DUPLICATE_COLLAPSE",
        "rule_name": "Same-action duplicate collapse",
        "rule_scope": "dedupe",
        "resolution_status": "resolved",
        "resolution_action": "collapse_duplicate",
        "reason": "Same final action from multiple sources is not a decision conflict; keep the ranked/freshest source.",
    },
    {
        "rule_id": "WATCH_LOSES_TO_HIGHER_PRIORITY",
        "rule_name": "Watch loses to higher-priority action",
        "rule_scope": "priority",
        "resolution_status": "resolved",
        "resolution_action": "keep_winner",
        "reason": "WATCH is informational and loses to any higher-priority action for the same symbol/date.",
    },
]
ACTION_CONFLICT_RULES_BY_ID = {str(rule["rule_id"]): rule for rule in ACTION_CONFLICT_RULES}

logger = setup_logger("advisory.decision_trace")

TRACE_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {TRACES_TABLE} (
        trace_id TEXT PRIMARY KEY,
        asof_date TIMESTAMPTZ,
        symbol TEXT NOT NULL,
        unique_id TEXT,
        setup_id TEXT,
        trigger_type TEXT,
        previous_action TEXT,
        new_action TEXT,
        action_changed BOOLEAN,
        final_action TEXT,
        final_reason TEXT,
        source_table TEXT,
        source_key TEXT,
        payload_json TEXT,
        created_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {TRACE_STEPS_TABLE} (
        trace_id TEXT NOT NULL,
        step_idx BIGINT NOT NULL,
        stage TEXT NOT NULL,
        status TEXT NOT NULL,
        reason TEXT,
        input_hash TEXT,
        output_hash TEXT,
        payload_json TEXT,
        started_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ,
        load_ts TIMESTAMPTZ,
        UNIQUE (trace_id, step_idx, stage)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {EVENT_PROCESSING_TABLE} (
        unique_id TEXT NOT NULL,
        symbol TEXT,
        source_type TEXT,
        stage TEXT NOT NULL,
        status TEXT NOT NULL,
        started_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ,
        error TEXT,
        input_hash TEXT,
        output_hash TEXT,
        payload_json TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (unique_id, stage, started_at)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {ACTION_CONFLICTS_TABLE} (
        asof_date TIMESTAMPTZ NOT NULL,
        symbol TEXT NOT NULL,
        winning_action_code TEXT,
        losing_action_code TEXT,
        winning_priority BIGINT,
        losing_priority BIGINT,
        winning_source TEXT,
        losing_source TEXT,
        losing_setup_id TEXT,
        losing_unique_id TEXT,
        lost_reason TEXT,
        raw_context_json TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (asof_date, symbol, losing_action_code, losing_source, losing_setup_id, losing_unique_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {ACTION_CONFLICT_RULES_TABLE} (
        rule_id TEXT PRIMARY KEY,
        rule_name TEXT NOT NULL,
        rule_scope TEXT,
        resolution_action TEXT NOT NULL,
        resolution_reason TEXT,
        enabled BOOLEAN DEFAULT TRUE,
        priority BIGINT DEFAULT 100,
        created_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ
    )
    """,
    f"ALTER TABLE {ACTION_CONFLICTS_TABLE} ADD COLUMN IF NOT EXISTS resolution_status TEXT",
    f"ALTER TABLE {ACTION_CONFLICTS_TABLE} ADD COLUMN IF NOT EXISTS resolution_rule_id TEXT",
    f"ALTER TABLE {ACTION_CONFLICTS_TABLE} ADD COLUMN IF NOT EXISTS resolution_action TEXT",
    f"ALTER TABLE {ACTION_CONFLICTS_TABLE} ADD COLUMN IF NOT EXISTS resolution_reason TEXT",
    f"ALTER TABLE {ACTION_CONFLICTS_TABLE} ADD COLUMN IF NOT EXISTS requires_manual_resolution BOOLEAN",
    f"ALTER TABLE {ACTION_CONFLICT_RULES_TABLE} ADD COLUMN IF NOT EXISTS condition_json TEXT",
    f"ALTER TABLE {ACTION_CONFLICT_RULES_TABLE} ADD COLUMN IF NOT EXISTS promoted_from_conflict_key TEXT",
    f"ALTER TABLE {ACTION_CONFLICT_RULES_TABLE} ADD COLUMN IF NOT EXISTS promoted_by TEXT",
    f"ALTER TABLE {ACTION_CONFLICT_RULES_TABLE} ADD COLUMN IF NOT EXISTS promotion_note TEXT",
]


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def stable_hash(value: Any) -> str:
    return hashlib.sha256(json_dumps(value).encode("utf-8")).hexdigest()


def safe_trace_call(func, **kwargs):
    try:
        return func(**kwargs)
    except Exception as exc:
        func_name = getattr(func, "__name__", str(func))
        logger.warning("decision trace write failed func=%s error=%s", func_name, exc)
        record_local_fallback_event(
            module="advisory.decision_trace",
            source="decision_trace",
            fallback_type="decision_trace_write_failed",
            severity="warn",
            reason="Decision trace write failed; pipeline continues but trace/audit evidence may be incomplete.",
            error=exc,
            metadata={
                "function": func_name,
                "kwargs_keys": sorted(str(key) for key in kwargs.keys()),
                "symbol": str(kwargs.get("symbol") or "").upper() or None,
                "unique_id": kwargs.get("unique_id"),
                "setup_id": kwargs.get("setup_id"),
                "trigger_type": kwargs.get("trigger_type"),
            },
        )
        return None


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
            module="advisory.decision_trace",
            source=table_name,
            fallback_type="decision_trace_table_lookup_failed",
            severity="warn",
            reason="Decision trace table existence check failed; trace/manual-review links may be incomplete.",
            error=exc,
            metadata={"table_name": table_name},
        )
        return False


def ensure_trace_tables() -> None:
    apply_schema_migration(
        migration_id=TRACE_SCHEMA_MIGRATION_ID,
        description="Create decision trace, trace step, event processing, and action conflict audit tables.",
        statements=TRACE_SCHEMA_STATEMENTS,
        metadata={
            "module": "advisory.decision_trace",
            "tables": [
                TRACES_TABLE,
                TRACE_STEPS_TABLE,
                EVENT_PROCESSING_TABLE,
                ACTION_CONFLICTS_TABLE,
                ACTION_CONFLICT_RULES_TABLE,
            ],
        },
    )
    def _seed_action_conflict_rules() -> None:
        with db_session() as (_, cur):
            now = pd.Timestamp.utcnow()
            for idx, rule in enumerate(ACTION_CONFLICT_RULES):
                cur.execute(
                    f"""
                    INSERT INTO {ACTION_CONFLICT_RULES_TABLE}
                        (rule_id, rule_name, rule_scope, resolution_action, resolution_reason, enabled, priority, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, TRUE, %s, %s, %s)
                    ON CONFLICT (rule_id) DO UPDATE SET
                        rule_name = EXCLUDED.rule_name,
                        rule_scope = EXCLUDED.rule_scope,
                        resolution_action = EXCLUDED.resolution_action,
                        resolution_reason = COALESCE({ACTION_CONFLICT_RULES_TABLE}.resolution_reason, EXCLUDED.resolution_reason),
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        str(rule["rule_id"]),
                        str(rule["rule_name"]),
                        str(rule["rule_scope"]),
                        str(rule["resolution_action"]),
                        str(rule["reason"]),
                        int(100 - idx),
                        now.to_pydatetime(),
                        now.to_pydatetime(),
                    ),
                )

    execute_db_operation(
        _seed_action_conflict_rules,
        operation_name="decision_trace:seed_action_conflict_rules",
    )


def _parse_jsonish(value: Any, default: Any = None, *, source: str = "json_context") -> Any:
    if value is None:
        return default
    if isinstance(value, float) and pd.isna(value):
        return default
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return default
    text = value.strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.decision_trace",
            fallback_type="decision_trace_json_parse_failed",
            source=source,
            severity="warn",
            reason="Decision trace could not parse stored JSON context and used the provided default.",
            error=exc,
            metadata={
                "default_type": type(default).__name__,
                "value_length": len(text),
                "value_excerpt": text[:240],
            },
        )
        return default


def _norm(value: Any, *, upper: bool = True) -> str:
    text = "" if value is None else str(value).strip()
    return text.upper() if upper else text.lower()


def _dynamic_rule_matches(row: dict[str, Any], rule: dict[str, Any]) -> bool:
    condition = _parse_jsonish(rule.get("condition_json"), {}, source="conflict_rule_condition_json")
    if not isinstance(condition, dict):
        return False
    condition_type = str(condition.get("condition_type") or "action_pair_exact").strip().lower()
    if condition_type not in {"action_pair", "action_pair_exact"}:
        return False
    checks = [("winning_action_code", True), ("losing_action_code", True)]
    if condition_type == "action_pair_exact":
        checks.extend([("winning_source", False), ("losing_source", False)])
    for field, upper in checks:
        expected = condition.get(field)
        if expected in (None, ""):
            continue
        if _norm(row.get(field), upper=upper) != _norm(expected, upper=upper):
            return False
    return True


def load_enabled_dynamic_action_conflict_rules() -> list[dict[str, Any]]:
    ensure_trace_tables()
    df = sql_to_df(
        f"""
        SELECT *
        FROM {ACTION_CONFLICT_RULES_TABLE}
        WHERE COALESCE(enabled, TRUE)
          AND condition_json IS NOT NULL
        ORDER BY priority DESC, rule_id
        """,
        retries=2,
    )
    return df.to_dict(orient="records") if not df.empty else []


def make_trace_id(*, asof_date: Any, symbol: Any, unique_id: Any = None, trigger_type: Any = None) -> str:
    key = json_dumps(
        {
            "asof_date": str(asof_date),
            "symbol": str(symbol).upper(),
            "unique_id": unique_id,
            "trigger_type": trigger_type,
        }
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


def append_trace(
    *,
    asof_date: Any,
    symbol: Any,
    trigger_type: str,
    final_action: Any,
    final_reason: Any = None,
    unique_id: Any = None,
    setup_id: Any = None,
    previous_action: Any = None,
    new_action: Any = None,
    source_table: Any = None,
    source_key: Any = None,
    payload: dict[str, Any] | None = None,
) -> str:
    ensure_trace_tables()
    now = pd.Timestamp.utcnow()
    trace_id = make_trace_id(asof_date=asof_date, symbol=symbol, unique_id=unique_id, trigger_type=trigger_type)
    row = pd.DataFrame(
        [
            {
                "trace_id": trace_id,
                "asof_date": pd.to_datetime(asof_date, utc=True, errors="coerce"),
                "symbol": str(symbol).upper(),
                "unique_id": None if pd.isna(unique_id) else str(unique_id) if unique_id is not None else None,
                "setup_id": None if pd.isna(setup_id) else str(setup_id) if setup_id is not None else None,
                "trigger_type": trigger_type,
                "previous_action": None if previous_action is None else str(previous_action),
                "new_action": None if new_action is None else str(new_action),
                "action_changed": None if previous_action is None or new_action is None else str(previous_action) != str(new_action),
                "final_action": None if final_action is None else str(final_action),
                "final_reason": None if final_reason is None else str(final_reason),
                "source_table": None if source_table is None else str(source_table),
                "source_key": None if source_key is None else str(source_key),
                "payload_json": json_dumps(payload or {}),
                "created_at": now,
                "updated_at": now,
            }
        ]
    )
    row["action_changed"] = row["action_changed"].astype("boolean")
    upsert_to_db(row, TRACES_TABLE, unique_keys=["trace_id"])
    return trace_id


def append_trace_step(
    *,
    trace_id: str,
    step_idx: int,
    stage: str,
    status: str,
    reason: Any = None,
    input_payload: Any = None,
    output_payload: Any = None,
    payload: dict[str, Any] | None = None,
    started_at: Any = None,
    completed_at: Any = None,
) -> None:
    ensure_trace_tables()
    now = pd.Timestamp.utcnow()
    row = pd.DataFrame(
        [
            {
                "trace_id": trace_id,
                "step_idx": int(step_idx),
                "stage": stage,
                "status": status,
                "reason": None if reason is None else str(reason),
                "input_hash": stable_hash(input_payload) if input_payload is not None else None,
                "output_hash": stable_hash(output_payload) if output_payload is not None else None,
                "payload_json": json_dumps(payload or {}),
                "started_at": pd.to_datetime(started_at or now, utc=True, errors="coerce"),
                "completed_at": pd.to_datetime(completed_at or now, utc=True, errors="coerce"),
                "load_ts": now,
            }
        ]
    )
    upsert_to_db(row, TRACE_STEPS_TABLE, unique_keys=["trace_id", "step_idx", "stage"])


def record_event_processing(
    *,
    unique_id: Any,
    stage: str,
    status: str,
    symbol: Any = None,
    source_type: Any = None,
    error: Any = None,
    input_payload: Any = None,
    output_payload: Any = None,
    payload: dict[str, Any] | None = None,
    started_at: Any = None,
    completed_at: Any = None,
) -> None:
    ensure_trace_tables()
    now = pd.Timestamp.utcnow()
    row = pd.DataFrame(
        [
            {
                "unique_id": str(unique_id),
                "symbol": None if symbol is None else str(symbol).upper(),
                "source_type": None if source_type is None else str(source_type),
                "stage": stage,
                "status": status,
                "started_at": pd.to_datetime(started_at or now, utc=True, errors="coerce"),
                "completed_at": pd.to_datetime(completed_at or now, utc=True, errors="coerce"),
                "error": None if error is None else str(error),
                "input_hash": stable_hash(input_payload) if input_payload is not None else None,
                "output_hash": stable_hash(output_payload) if output_payload is not None else None,
                "payload_json": json_dumps(payload or {}),
                "load_ts": now,
            }
        ]
    )
    upsert_to_db(row, EVENT_PROCESSING_TABLE, unique_keys=["unique_id", "stage", "started_at"])


def build_action_conflicts(all_candidates: pd.DataFrame, winners: pd.DataFrame) -> pd.DataFrame:
    if all_candidates.empty or winners.empty:
        return pd.DataFrame()
    all_df = all_candidates.copy()
    winners_df = winners.copy()
    for frame in [all_df, winners_df]:
        frame["symbol"] = frame["symbol"].astype("string").str.upper()
        frame["asof_date"] = pd.to_datetime(frame["asof_date"], utc=True, errors="coerce")
    winner_map = {
        (row["asof_date"], row["symbol"]): row
        for _, row in winners_df.iterrows()
    }
    rows: list[dict[str, Any]] = []
    for _, row in all_df.iterrows():
        key = (row["asof_date"], row["symbol"])
        winner = winner_map.get(key)
        if winner is None:
            continue
        same_row = (
            str(row.get("action_code")) == str(winner.get("action_code"))
            and str(row.get("action_source")) == str(winner.get("action_source"))
            and str(row.get("setup_id")) == str(winner.get("setup_id"))
            and str(row.get("unique_id")) == str(winner.get("unique_id"))
        )
        if same_row:
            continue
        rows.append(
            {
                "asof_date": row.get("asof_date"),
                "symbol": row.get("symbol"),
                "winning_action_code": winner.get("action_code"),
                "losing_action_code": row.get("action_code"),
                "winning_priority": winner.get("action_priority"),
                "losing_priority": row.get("action_priority"),
                "winning_source": winner.get("action_source"),
                "losing_source": row.get("action_source"),
                "losing_setup_id": row.get("setup_id"),
                "losing_unique_id": row.get("unique_id"),
                "lost_reason": f"Lost to {winner.get('action_code')} from {winner.get('action_source')} by action priority/tie-break.",
                "raw_context_json": row.get("raw_context_json"),
                "load_ts": pd.Timestamp.utcnow(),
            }
        )
    return pd.DataFrame(rows)


def classify_action_conflict(row: dict[str, Any], dynamic_rules: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    winning = str(row.get("winning_action_code") or "").strip().upper()
    losing = str(row.get("losing_action_code") or "").strip().upper()
    winning_source = str(row.get("winning_source") or "").strip().lower()
    context_text = " ".join(str(row.get(key) or "") for key in ["lost_reason", "raw_context_json"]).lower()
    exit_actions = {"SELL", "PARTIAL_SELL", "TIGHTEN_STOP", "EXIT", "REDUCE_EXPOSURE", "REDUCE_EXPOSURE_REVIEW"}
    low_priority_actions = {"WATCH", "HOLD", "MANUAL_REVIEW"}
    entry_actions = {"BUY", "BUY_MORE"}

    if winning == losing and winning:
        rule = ACTION_CONFLICT_RULES_BY_ID["SAME_ACTION_DUPLICATE_COLLAPSE"]
    elif winning in exit_actions and losing in (low_priority_actions | entry_actions):
        rule = ACTION_CONFLICT_RULES_BY_ID["EXIT_BEATS_ENTRY_OR_WATCH"]
    elif winning == "MANUAL_REVIEW" and winning_source == "event_policy" and losing in {"BUY", "BUY_MORE"}:
        rule = ACTION_CONFLICT_RULES_BY_ID["EVENT_POLICY_REVIEW_BEATS_POSITIVE_ENTRY"]
    elif winning == "MANUAL_REVIEW" and ("market_context_adjustment" in context_text or "risk-off" in context_text or "risk_off" in context_text or winning_source in {"portfolio", "event_policy"}):
        rule = ACTION_CONFLICT_RULES_BY_ID["MARKET_GATE_MANUAL_BEATS_POSITIVE"]
    elif losing == "WATCH" and winning:
        rule = ACTION_CONFLICT_RULES_BY_ID["WATCH_LOSES_TO_HIGHER_PRIORITY"]
    else:
        for rule_row in dynamic_rules or []:
            if not _dynamic_rule_matches(row, rule_row):
                continue
            return {
                "resolution_status": "resolved",
                "resolution_rule_id": str(rule_row.get("rule_id")),
                "resolution_action": str(rule_row.get("resolution_action") or "keep_winner"),
                "resolution_reason": str(rule_row.get("resolution_reason") or "Matched operator-promoted conflict rule."),
                "requires_manual_resolution": False,
            }
        return {
            "resolution_status": "unresolved",
            "resolution_rule_id": None,
            "resolution_action": "manual_resolution_required",
            "resolution_reason": "No deterministic conflict rule matched this action combination.",
            "requires_manual_resolution": True,
        }
    return {
        "resolution_status": str(rule["resolution_status"]),
        "resolution_rule_id": str(rule["rule_id"]),
        "resolution_action": str(rule["resolution_action"]),
        "resolution_reason": str(rule["reason"]),
        "requires_manual_resolution": False,
    }


def apply_action_conflict_resolution_rules(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    dynamic_rules = load_enabled_dynamic_action_conflict_rules()
    resolutions = [classify_action_conflict(row, dynamic_rules=dynamic_rules) for row in out.to_dict(orient="records")]
    for key in ["resolution_status", "resolution_rule_id", "resolution_action", "resolution_reason", "requires_manual_resolution"]:
        out[key] = [item.get(key) for item in resolutions]
    return out


def persist_action_conflicts(df: pd.DataFrame) -> None:
    ensure_trace_tables()
    if df.empty:
        return
    out = apply_action_conflict_resolution_rules(df)
    for column in ["asof_date", "load_ts"]:
        out[column] = pd.to_datetime(out[column], utc=True, errors="coerce")
    for column in ["winning_priority", "losing_priority"]:
        out[column] = pd.to_numeric(out[column], errors="coerce").astype("Int64")
    if "requires_manual_resolution" in out.columns:
        out["requires_manual_resolution"] = out["requires_manual_resolution"].astype("boolean")
    upsert_to_db(
        out,
        ACTION_CONFLICTS_TABLE,
        unique_keys=["asof_date", "symbol", "losing_action_code", "losing_source", "losing_setup_id", "losing_unique_id"],
        timescaledb_column="asof_date",
    )


def load_event_trace(unique_id: str) -> dict[str, Any]:
    ensure_trace_tables()
    processing = sql_to_df(
        f"""
        SELECT *
        FROM {EVENT_PROCESSING_TABLE}
        WHERE unique_id = %s
        ORDER BY started_at DESC, stage
        """,
        params=(unique_id,),
    )
    traces = sql_to_df(
        f"""
        SELECT *
        FROM {TRACES_TABLE}
        WHERE unique_id = %s
        ORDER BY updated_at DESC
        """,
        params=(unique_id,),
    )
    steps = pd.DataFrame()
    if not traces.empty:
        steps = sql_to_df(
            f"""
            SELECT *
            FROM {TRACE_STEPS_TABLE}
            WHERE trace_id = ANY(%s)
            ORDER BY trace_id, step_idx, stage
            """,
            params=(traces["trace_id"].dropna().astype(str).tolist(),),
        )
    return {
        "unique_id": unique_id,
        "processing": processing.to_dict(orient="records") if not processing.empty else [],
        "traces": traces.to_dict(orient="records") if not traces.empty else [],
        "steps": steps.to_dict(orient="records") if not steps.empty else [],
        "manual_review_wait_signal_links": load_manual_review_wait_signal_links(unique_id=unique_id, limit=50),
    }


def load_manual_review_wait_signal_links(
    *,
    symbol: str | None = None,
    unique_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    if not table_exists(MANUAL_REVIEW_DECISIONS_TABLE) or not table_exists(WAIT_SIGNALS_TABLE):
        return []
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_unique_id = str(unique_id or "").strip()
    if not normalized_symbol and not normalized_unique_id:
        return []
    row_limit = max(1, min(int(limit), 1000))
    has_matches = table_exists(WAIT_SIGNAL_MATCHES_TABLE)
    match_select = (
        """
            m.matched_at,
            m.match_status,
            m.match_score,
            m.source_table AS match_source_table,
            m.source_key AS match_source_key,
            m.observed_at,
            m.match_reason,
            m.evidence_json
        """
        if has_matches
        else """
            NULL::timestamptz AS matched_at,
            NULL::text AS match_status,
            NULL::double precision AS match_score,
            NULL::text AS match_source_table,
            NULL::text AS match_source_key,
            NULL::timestamptz AS observed_at,
            NULL::text AS match_reason,
            NULL::jsonb AS evidence_json
        """
    )
    match_join = (
        f"""
        LEFT JOIN {WAIT_SIGNAL_MATCHES_TABLE} m
          ON m.signal_id = s.signal_id
        """
        if has_matches
        else ""
    )
    order_expr = "COALESCE(m.matched_at, s.created_at, d.decided_at)" if has_matches else "COALESCE(s.created_at, d.decided_at)"
    filters: list[str] = []
    params: list[Any] = []
    if normalized_symbol:
        filters.append("COALESCE(s.symbol, d.symbol, m.symbol) = %s" if has_matches else "COALESCE(s.symbol, d.symbol) = %s")
        params.append(normalized_symbol)
    if normalized_unique_id:
        filters.append("(d.unique_id = %s OR m.source_key = %s)" if has_matches else "d.unique_id = %s")
        params.extend([normalized_unique_id, normalized_unique_id] if has_matches else [normalized_unique_id])
    where_clause = " OR ".join(f"({item})" for item in filters)
    query = f"""
        SELECT
            d.decided_at,
            d.item_id AS manual_review_item_id,
            d.item_type AS manual_review_item_type,
            d.source_table AS manual_review_source_table,
            d.source_key AS manual_review_source_key,
            d.symbol AS manual_review_symbol,
            d.unique_id AS manual_review_unique_id,
            d.setup_id AS manual_review_setup_id,
            d.decision,
            d.rationale,
            d.follow_up_event,
            d.operator_id,
            d.note_json,
            s.created_at AS wait_signal_created_at,
            s.signal_id,
            s.status AS wait_signal_status,
            s.signal_type,
            s.expected_action,
            s.operator_summary,
            s.wait_question,
            s.condition_json,
            s.valid_until,
            s.generated_by,
            {match_select}
        FROM {MANUAL_REVIEW_DECISIONS_TABLE} d
        LEFT JOIN {WAIT_SIGNALS_TABLE} s
          ON s.source_key = d.item_id
         AND s.source_table = %s
         AND s.generated_by = 'manual_review_decision'
        {match_join}
        WHERE {where_clause}
        ORDER BY {order_expr} DESC NULLS LAST
        LIMIT %s
    """
    df = sql_to_df(query, params=tuple([MANUAL_REVIEW_DECISIONS_TABLE, *params, row_limit]))
    return df.to_dict(orient="records") if not df.empty else []


def load_symbol_trace(symbol: str, *, limit: int = 200) -> dict[str, Any]:
    ensure_trace_tables()
    normalized_symbol = str(symbol).upper()
    row_limit = max(1, min(int(limit), 1000))
    processing = sql_to_df(
        f"""
        SELECT *
        FROM {EVENT_PROCESSING_TABLE}
        WHERE symbol = %s
        ORDER BY started_at DESC, stage
        LIMIT %s
        """,
        params=(normalized_symbol, row_limit),
    )
    traces = sql_to_df(
        f"""
        SELECT *
        FROM {TRACES_TABLE}
        WHERE symbol = %s
        ORDER BY updated_at DESC
        LIMIT %s
        """,
        params=(normalized_symbol, row_limit),
    )
    steps = pd.DataFrame()
    if not traces.empty:
        steps = sql_to_df(
            f"""
            SELECT *
            FROM {TRACE_STEPS_TABLE}
            WHERE trace_id = ANY(%s)
            ORDER BY completed_at DESC, trace_id, step_idx, stage
            LIMIT %s
            """,
            params=(traces["trace_id"].dropna().astype(str).tolist(), row_limit * 5),
        )
    conflicts = sql_to_df(
        f"""
        SELECT *
        FROM {ACTION_CONFLICTS_TABLE}
        WHERE symbol = %s
        ORDER BY asof_date DESC, load_ts DESC
        LIMIT %s
        """,
        params=(normalized_symbol, row_limit),
    )
    return {
        "symbol": normalized_symbol,
        "processing": processing.to_dict(orient="records") if not processing.empty else [],
        "traces": traces.to_dict(orient="records") if not traces.empty else [],
        "steps": steps.to_dict(orient="records") if not steps.empty else [],
        "action_conflicts": conflicts.to_dict(orient="records") if not conflicts.empty else [],
        "manual_review_wait_signal_links": load_manual_review_wait_signal_links(symbol=normalized_symbol, limit=row_limit),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect advisory decision traces.")
    parser.add_argument("--unique-id")
    parser.add_argument("--symbol")
    parser.add_argument("--limit", type=int, default=200)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.symbol:
        print(json.dumps(load_symbol_trace(args.symbol, limit=int(args.limit)), indent=2, ensure_ascii=False, default=str))
        return 0
    if not args.unique_id:
        ensure_trace_tables()
        print(json.dumps({"status": "ok", "tables": [TRACES_TABLE, TRACE_STEPS_TABLE, EVENT_PROCESSING_TABLE, ACTION_CONFLICTS_TABLE]}, indent=2))
        return 0
    print(json.dumps(load_event_trace(args.unique_id), indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
