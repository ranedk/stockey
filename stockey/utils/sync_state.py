from __future__ import annotations

import json
import os
from typing import Any

import pandas as pd
import redis

from utils.fallback_telemetry import record_local_fallback_event
from utils.db import sql_to_df, upsert_to_db
from utils.redis_utils import get_redis_client
from utils.schema_migrations import apply_schema_migration


TABLE_NAME = "advisory_sync_state"
SYNC_STATE_SCHEMA_MIGRATION_ID = "20260611_advisory_sync_state_base"
DEFAULT_REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
DEFAULT_REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

SYNC_STATE_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        source_name TEXT NOT NULL,
        scope_key TEXT NOT NULL,
        cursor_value TEXT,
        last_success_at TIMESTAMPTZ,
        last_item_ts TIMESTAMPTZ,
        state_json TEXT,
        status TEXT,
        error_text TEXT,
        updated_at TIMESTAMPTZ,
        load_ts TIMESTAMPTZ,
        UNIQUE (source_name, scope_key)
    )
    """,
]


def ensure_sync_state_table() -> None:
    apply_schema_migration(
        migration_id=SYNC_STATE_SCHEMA_MIGRATION_ID,
        description="Create advisory sync-state cursor/status table.",
        statements=SYNC_STATE_SCHEMA_STATEMENTS,
        metadata={"module": "advisory.sync_state", "tables": [TABLE_NAME]},
    )


def load_sync_states(*, source_name: str | None = None) -> pd.DataFrame:
    ensure_sync_state_table()
    clauses = ["1 = 1"]
    params: list[object] = []
    if source_name:
        clauses.append("source_name = %s")
        params.append(source_name)
    df = sql_to_df(
        f"""
        SELECT *
        FROM {TABLE_NAME}
        WHERE {' AND '.join(clauses)}
        ORDER BY source_name, scope_key
        """,
        params=tuple(params) if params else None,
    )
    if df.empty:
        return df
    for column in ["last_success_at", "last_item_ts", "updated_at", "load_ts"]:
        if column in df.columns:
            df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    return df


def load_sync_state(source_name: str, scope_key: str = "default") -> dict[str, Any] | None:
    df = load_sync_states(source_name=source_name)
    if df.empty:
        return None
    filtered = df[df["scope_key"].astype(str) == str(scope_key)]
    if filtered.empty:
        return None
    row = filtered.iloc[0].to_dict()
    state_json = row.get("state_json")
    if state_json:
        try:
            row["state"] = json.loads(str(state_json))
        except json.JSONDecodeError as exc:
            record_local_fallback_event(
                module="utils.sync_state",
                fallback_type="sync_state_state_json_parse_failed",
                source=str(source_name),
                severity="warn",
                reason="Sync-state row contains malformed JSON; falling back to an empty state payload.",
                error=exc,
                metadata={
                    "source_name": str(source_name),
                    "scope_key": str(scope_key),
                    "state_json_length": len(str(state_json)),
                },
            )
            row["state"] = {}
    else:
        row["state"] = {}
    return row


def _utc_timestamp_series(value: Any) -> pd.Series:
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    return pd.Series([parsed], dtype="datetime64[ns, UTC]")


def persist_sync_state(
    *,
    source_name: str,
    scope_key: str = "default",
    cursor_value: str | None = None,
    last_success_at: pd.Timestamp | None = None,
    last_item_ts: pd.Timestamp | None = None,
    state: dict[str, Any] | None = None,
    status: str = "ok",
    error_text: str | None = None,
) -> None:
    ensure_sync_state_table()
    df = pd.DataFrame(
        [
            {
                "source_name": str(source_name),
                "scope_key": str(scope_key),
                "cursor_value": None if cursor_value is None else str(cursor_value),
                "state_json": json.dumps(state or {}, ensure_ascii=False, sort_keys=True, default=str),
                "status": str(status),
                "error_text": None if error_text is None else str(error_text),
            }
        ]
    )
    now = pd.Timestamp.utcnow()
    df["last_success_at"] = _utc_timestamp_series(last_success_at)
    df["last_item_ts"] = _utc_timestamp_series(last_item_ts)
    df["updated_at"] = _utc_timestamp_series(now)
    df["load_ts"] = _utc_timestamp_series(now)
    upsert_to_db(df, TABLE_NAME, unique_keys=["source_name", "scope_key"])


def publish_bus_message(channel: str, payload: dict[str, Any]) -> bool:
    try:
        client = get_redis_client(host=DEFAULT_REDIS_HOST, port=DEFAULT_REDIS_PORT, decode_responses=True)
        client.publish(str(channel), json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
        client.close()
        return True
    except Exception as exc:
        record_local_fallback_event(
            module="utils.sync_state",
            fallback_type="sync_state_bus_publish_failed",
            source=str(channel),
            severity="warn",
            reason="Sync-state bus publish failed; database state remains authoritative but live subscribers may miss this update.",
            error=exc,
            metadata={
                "channel": str(channel),
                "payload_keys": sorted(str(key) for key in payload.keys()) if isinstance(payload, dict) else [],
                "payload_type": type(payload).__name__,
                "redis_host": str(DEFAULT_REDIS_HOST),
                "redis_port": int(DEFAULT_REDIS_PORT),
            },
        )
        return False
