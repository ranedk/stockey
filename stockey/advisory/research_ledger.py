from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pandas as pd

from utils.db import db_session, sql_to_df, upsert_to_db
from utils.sync import parse_datetime_arg


LEDGER_TABLE = "advisory_research_runs"
REPO_ROOT = Path(__file__).resolve().parent.parent


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if isinstance(value, pd.DataFrame):
        return value.to_dict(orient="records")
    if isinstance(value, dict):
        return {str(k): _json_default(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_default(v) for v in value]
    return value


def _json_text(value: Any) -> str:
    return json.dumps(_json_default(value), ensure_ascii=False, default=str, sort_keys=True)


def _try_git_rev() -> str | None:
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=str(REPO_ROOT),
                stderr=subprocess.DEVNULL,
                text=True,
            )
            .strip()
            or None
        )
    except Exception:
        return None


def _config_hash(config: dict[str, Any]) -> str:
    return hashlib.sha256(_json_text(config).encode("utf-8")).hexdigest()


def ensure_tables() -> None:
    with db_session() as (_, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (
                research_run_id TEXT PRIMARY KEY,
                parent_run_id TEXT,
                run_type TEXT NOT NULL,
                entrypoint TEXT NOT NULL,
                label TEXT,
                objective TEXT,
                asof_date TIMESTAMPTZ,
                status TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                config_json TEXT NOT NULL,
                validation_protocol_json TEXT,
                data_snapshot_json TEXT,
                result_metrics_json TEXT,
                notes_json TEXT,
                error_text TEXT,
                git_rev TEXT,
                started_ts TIMESTAMPTZ NOT NULL,
                completed_ts TIMESTAMPTZ,
                updated_ts TIMESTAMPTZ NOT NULL
            )
            """
        )


def start_research_run(
    *,
    run_type: str,
    entrypoint: str,
    config: dict[str, Any],
    asof_date: pd.Timestamp | None = None,
    label: str | None = None,
    objective: str | None = None,
    validation_protocol: dict[str, Any] | None = None,
    notes: dict[str, Any] | None = None,
    parent_run_id: str | None = None,
) -> str:
    ensure_tables()
    now = pd.Timestamp.utcnow()
    run_id = str(uuid.uuid4())
    row = pd.DataFrame(
        [
            {
                "research_run_id": run_id,
                "parent_run_id": parent_run_id,
                "run_type": str(run_type),
                "entrypoint": str(entrypoint),
                "label": label,
                "objective": objective,
                "asof_date": asof_date,
                "status": "running",
                "config_hash": _config_hash(config),
                "config_json": _json_text(config),
                "validation_protocol_json": None if validation_protocol is None else _json_text(validation_protocol),
                "data_snapshot_json": None,
                "result_metrics_json": None,
                "notes_json": None if notes is None else _json_text(notes),
                "error_text": None,
                "git_rev": _try_git_rev(),
                "started_ts": now,
                "completed_ts": None,
                "updated_ts": now,
            }
        ]
    )
    upsert_to_db(row, LEDGER_TABLE, unique_keys=["research_run_id"])
    return run_id


def finish_research_run(
    research_run_id: str,
    *,
    status: str,
    data_snapshot: dict[str, Any] | None = None,
    result_metrics: dict[str, Any] | None = None,
    notes: dict[str, Any] | None = None,
    error_text: str | None = None,
) -> None:
    ensure_tables()
    now = pd.Timestamp.utcnow()
    row = pd.DataFrame(
        [
            {
                "research_run_id": str(research_run_id),
                "status": str(status),
                "data_snapshot_json": None if data_snapshot is None else _json_text(data_snapshot),
                "result_metrics_json": None if result_metrics is None else _json_text(result_metrics),
                "notes_json": None if notes is None else _json_text(notes),
                "error_text": error_text,
                "completed_ts": now,
                "updated_ts": now,
            }
        ]
    )
    upsert_to_db(row, LEDGER_TABLE, unique_keys=["research_run_id"])


def build_data_snapshot(*, asof_date: pd.Timestamp | None, summary: dict[str, Any]) -> dict[str, Any]:
    stages = summary.get("stages") or {}
    return {
        "asof_date": asof_date,
        "stages_present": sorted(stages.keys()),
        "stage_row_counts": {
            stage: int(((payload or {}).get("row_count") or 0))
            for stage, payload in stages.items()
            if isinstance(payload, dict)
        },
    }


def build_result_metrics(*, status: str, summary: dict[str, Any]) -> dict[str, Any]:
    stages = summary.get("stages") or {}
    metrics: dict[str, Any] = {
        "status": status,
        "stage_count": len(stages),
        "stages_run": sorted(stages.keys()),
    }
    for key in ["rules", "watchlist", "risk", "portfolio", "lifecycle", "execution"]:
        payload = stages.get(key)
        if isinstance(payload, dict) and "row_count" in payload:
            metrics[f"{key}_row_count"] = payload.get("row_count")
    return metrics


def parse_validation_protocol(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    return json.loads(text)


def list_runs(limit: int = 50) -> pd.DataFrame:
    ensure_tables()
    return sql_to_df(
        f"""
        SELECT research_run_id, run_type, entrypoint, label, objective, asof_date, status, git_rev, started_ts, completed_ts
        FROM {LEDGER_TABLE}
        ORDER BY started_ts DESC
        LIMIT %s
        """,
        params=(int(limit),),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect advisory research ledger runs.")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--date", type=parse_datetime_arg, help="Optional asof date filter placeholder")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    df = list_runs(limit=int(args.limit))
    print(json.dumps(df.to_dict(orient="records"), indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
