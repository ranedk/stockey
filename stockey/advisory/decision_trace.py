from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from typing import Any

import pandas as pd

from utils.db import db_session, sql_to_df, upsert_to_db
from utils.log import setup_logger


TRACES_TABLE = "advisory_decision_traces"
TRACE_STEPS_TABLE = "advisory_decision_trace_steps"
EVENT_PROCESSING_TABLE = "advisory_event_processing_runs"
ACTION_CONFLICTS_TABLE = "advisory_action_conflicts"

logger = setup_logger("advisory.decision_trace")


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def stable_hash(value: Any) -> str:
    return hashlib.sha256(json_dumps(value).encode("utf-8")).hexdigest()


def safe_trace_call(func, **kwargs):
    try:
        return func(**kwargs)
    except Exception as exc:
        logger.warning("decision trace write failed func=%s error=%s", getattr(func, "__name__", str(func)), exc)
        return None


def ensure_trace_tables() -> None:
    with db_session() as (_, cur):
        cur.execute(
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
            """
        )
        cur.execute(
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
            """
        )
        cur.execute(
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
            """
        )
        cur.execute(
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
            """
        )


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


def persist_action_conflicts(df: pd.DataFrame) -> None:
    ensure_trace_tables()
    if df.empty:
        return
    out = df.copy()
    for column in ["asof_date", "load_ts"]:
        out[column] = pd.to_datetime(out[column], utc=True, errors="coerce")
    for column in ["winning_priority", "losing_priority"]:
        out[column] = pd.to_numeric(out[column], errors="coerce").astype("Int64")
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
    }


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
