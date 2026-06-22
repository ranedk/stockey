from __future__ import annotations

import argparse
import hashlib
import json
from typing import Any

import pandas as pd

from advisory.causal_event_memory import SOURCE_TABLES, TABLE_NAME as CAUSAL_EVENT_MEMORY_TABLE
from advisory.causal_event_memory_evaluator import EVALUATIONS_TABLE as CAUSAL_EVENT_MEMORY_EVALUATIONS_TABLE
from advisory.event_evidence_store import table_exists
from advisory.fallback_telemetry import record_local_fallback_event
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration
from utils.sync import parse_datetime_arg


TABLE_NAME = "advisory_causal_event_provenance"
SCHEMA_MIGRATION_ID = "20260621_advisory_causal_event_provenance_base"
DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_LIMIT = 10000

SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        provenance_id TEXT PRIMARY KEY,
        asof_date TIMESTAMPTZ NOT NULL,
        symbol TEXT,
        sector_code TEXT,
        sector_name TEXT,
        context_source TEXT NOT NULL,
        context_class TEXT,
        event_type TEXT,
        direction TEXT,
        event_state TEXT,
        memory_id TEXT NOT NULL,
        source_table TEXT,
        source_key TEXT,
        source_node_type TEXT NOT NULL,
        memory_node_type TEXT NOT NULL,
        evaluation_node_type TEXT NOT NULL,
        horizon_days BIGINT,
        evaluated_at TIMESTAMPTZ,
        matured BOOLEAN,
        directional_helpfulness_score DOUBLE PRECISION,
        direction_hit_after_cost BOOLEAN,
        provenance_status TEXT NOT NULL,
        authority_scope TEXT NOT NULL,
        policy_effect TEXT NOT NULL,
        broker_execution_allowed BOOLEAN NOT NULL DEFAULT FALSE,
        policy_auto_promotion_allowed BOOLEAN NOT NULL DEFAULT FALSE,
        source_node_json TEXT,
        memory_node_json TEXT,
        evaluation_node_json TEXT,
        load_ts TIMESTAMPTZ NOT NULL
    )
    """,
]


def _record_fallback(
    *,
    fallback_type: str,
    source: str,
    reason: str,
    error: Exception,
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="advisory.causal_event_provenance",
        fallback_type=fallback_type,
        source=source,
        severity="warn",
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def _json_dumps(value: Any) -> str:
    return json.dumps(_json_clean(value if value is not None else {}), ensure_ascii=False, sort_keys=True, allow_nan=False, default=str)


def _json_clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_clean(item) for item in value]
    if isinstance(value, tuple):
        return [_json_clean(item) for item in value]
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if value is pd.NaT:
        return None
    if pd.api.types.is_scalar(value) and pd.isna(value):
        return None
    return value


def _parse_jsonish(value: Any, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if pd.api.types.is_scalar(value) and pd.isna(value):
        return default
    try:
        return json.loads(str(value))
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.causal_event_provenance",
            fallback_type="causal_event_provenance_json_parse_failed",
            source=TABLE_NAME,
            severity="warn",
            reason="Causal event provenance could not parse stored JSON context and used the provided default.",
            error=exc,
            metadata={"payload_length": len(str(value))},
        )
        return default


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    if pd.api.types.is_scalar(value) and pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _number(value: Any) -> float | None:
    numeric = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric):
        return None
    return float(numeric)


def _bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if pd.api.types.is_scalar(value) and pd.isna(value):
        return None
    text = str(value).strip().lower()
    if text in {"true", "t", "1", "yes", "y"}:
        return True
    if text in {"false", "f", "0", "no", "n"}:
        return False
    return None


def _provenance_id(row: dict[str, Any]) -> str:
    parts = [
        row.get("memory_id"),
        row.get("source_table"),
        row.get("source_key"),
        row.get("horizon_days"),
        row.get("evaluated_at"),
    ]
    digest = hashlib.sha256("|".join(str(part or "") for part in parts).encode("utf-8")).hexdigest()[:24]
    return f"cep_{digest}"


def ensure_table() -> None:
    apply_schema_migration(
        migration_id=SCHEMA_MIGRATION_ID,
        description="Create typed source-to-causal-memory-to-outcome provenance rows.",
        statements=SCHEMA_STATEMENTS,
        owner="advisory.causal_event_provenance",
        metadata={"tables": [TABLE_NAME], "authority": "research_only_no_broker_authority"},
    )


def load_memory_rows(
    *,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    limit: int = DEFAULT_LIMIT,
) -> pd.DataFrame:
    if not table_exists(CAUSAL_EVENT_MEMORY_TABLE):
        return pd.DataFrame()
    clauses = ["authority_scope = 'review_input_only'"]
    params: list[Any] = []
    if from_date is not None:
        clauses.append("asof_date >= %s")
        params.append(from_date)
    if to_date is not None:
        clauses.append("asof_date <= %s")
        params.append(to_date)
    try:
        return sql_to_df(
            f"""
            SELECT
                asof_date,
                memory_id,
                symbol,
                sector_code,
                sector_name,
                context_source,
                context_class,
                event_type,
                direction,
                event_state,
                pressure_score,
                decayed_pressure_score,
                event_count,
                contradiction_state,
                source_refs_json,
                source_summary_json,
                authority_scope,
                portfolio_authority,
                broker_execution_allowed,
                policy_effect
            FROM {CAUSAL_EVENT_MEMORY_TABLE}
            WHERE {' AND '.join(clauses)}
            ORDER BY asof_date DESC, context_source, symbol NULLS LAST, sector_code NULLS LAST, memory_id
            LIMIT %s
            """,
            params=tuple([*params, int(limit)]),
            retries=4,
            statement_timeout_ms=0,
            chunksize=50000,
        )
    except Exception as exc:
        _record_fallback(
            fallback_type="causal_event_provenance_memory_load_failed",
            source=CAUSAL_EVENT_MEMORY_TABLE,
            reason="Causal-event provenance could not load causal-memory rows.",
            error=exc,
            metadata={"from_date": str(from_date), "to_date": str(to_date), "limit": int(limit)},
        )
        raise


def load_evaluation_rows(
    *,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    limit: int = DEFAULT_LIMIT * 3,
) -> pd.DataFrame:
    if not table_exists(CAUSAL_EVENT_MEMORY_EVALUATIONS_TABLE):
        return pd.DataFrame()
    clauses = ["1=1"]
    params: list[Any] = []
    if from_date is not None:
        clauses.append("asof_date >= %s")
        params.append(from_date)
    if to_date is not None:
        clauses.append("asof_date <= %s")
        params.append(to_date)
    try:
        return sql_to_df(
            f"""
            SELECT DISTINCT ON (memory_id, horizon_days)
                evaluated_at,
                horizon_days,
                memory_id,
                asof_date,
                entry_date,
                exit_date,
                forward_return_after_cost,
                benchmark_forward_return,
                excess_return_after_cost,
                directional_helpfulness_score,
                excess_directional_helpfulness_score,
                direction_hit_after_cost,
                excess_direction_hit_after_cost,
                matured,
                evaluation_label
            FROM {CAUSAL_EVENT_MEMORY_EVALUATIONS_TABLE}
            WHERE {' AND '.join(clauses)}
            ORDER BY memory_id, horizon_days, evaluated_at DESC
            LIMIT %s
            """,
            params=tuple([*params, int(limit)]),
            retries=4,
            statement_timeout_ms=0,
            chunksize=50000,
        )
    except Exception as exc:
        _record_fallback(
            fallback_type="causal_event_provenance_evaluation_load_failed",
            source=CAUSAL_EVENT_MEMORY_EVALUATIONS_TABLE,
            reason="Causal-event provenance could not load causal-memory evaluation rows.",
            error=exc,
            metadata={"from_date": str(from_date), "to_date": str(to_date), "limit": int(limit)},
        )
        raise


def build_provenance_rows(memory: pd.DataFrame, evaluations: pd.DataFrame | None = None) -> pd.DataFrame:
    if memory.empty:
        return pd.DataFrame()
    evaluation_map: dict[str, list[dict[str, Any]]] = {}
    if isinstance(evaluations, pd.DataFrame) and not evaluations.empty:
        eval_frame = evaluations.copy()
        eval_frame["memory_id"] = eval_frame["memory_id"].astype("string").str.strip()
        for memory_id, group in eval_frame.groupby("memory_id", dropna=False):
            evaluation_map[str(memory_id)] = group.to_dict(orient="records")
    rows: list[dict[str, Any]] = []
    load_ts = pd.Timestamp.utcnow()
    for _, memory_row in memory.iterrows():
        memory_id = _clean_text(memory_row.get("memory_id"))
        if not memory_id:
            continue
        refs = _parse_jsonish(memory_row.get("source_refs_json"), default=[])
        if not isinstance(refs, list) or not refs:
            refs = [{"source": memory_row.get("context_source"), "overlay_id": None}]
        eval_rows = evaluation_map.get(memory_id) or [None]
        for ref in refs:
            if not isinstance(ref, dict):
                ref = {"raw_ref": ref}
            context_source = _clean_text(memory_row.get("context_source")) or _clean_text(ref.get("source")) or "unknown"
            source_table = SOURCE_TABLES.get(str(context_source), str(context_source))
            source_key = _clean_text(ref.get("overlay_id")) or _clean_text(ref.get("unique_id")) or _clean_text(ref.get("evidence_id"))
            source_node = {
                "node_type": "context_overlay",
                "source_table": source_table,
                "source_key": source_key,
                "context_source": context_source,
                "ref": ref,
                "authority_scope": "review_input_only",
                "policy_effect": "source_context_only_no_trade_authority",
            }
            memory_node = {
                "node_type": "causal_event_memory",
                "memory_id": memory_id,
                "event_state": memory_row.get("event_state"),
                "direction": memory_row.get("direction"),
                "pressure_score": _number(memory_row.get("pressure_score")),
                "decayed_pressure_score": _number(memory_row.get("decayed_pressure_score")),
                "event_count": _number(memory_row.get("event_count")),
                "contradiction_state": memory_row.get("contradiction_state"),
                "source_summary": _parse_jsonish(memory_row.get("source_summary_json"), default={}),
                "authority_scope": memory_row.get("authority_scope") or "review_input_only",
                "portfolio_authority": memory_row.get("portfolio_authority") or "none",
                "broker_execution_allowed": False,
                "policy_effect": memory_row.get("policy_effect") or "memory_only_no_trade_authority",
            }
            for evaluation_row in eval_rows:
                evaluation_node = _evaluation_node(evaluation_row)
                row = {
                    "asof_date": pd.to_datetime(memory_row.get("asof_date"), utc=True, errors="coerce"),
                    "symbol": _clean_text(memory_row.get("symbol")),
                    "sector_code": _clean_text(memory_row.get("sector_code")),
                    "sector_name": _clean_text(memory_row.get("sector_name")),
                    "context_source": context_source,
                    "context_class": _clean_text(memory_row.get("context_class")),
                    "event_type": _clean_text(memory_row.get("event_type")),
                    "direction": _clean_text(memory_row.get("direction")),
                    "event_state": _clean_text(memory_row.get("event_state")),
                    "memory_id": memory_id,
                    "source_table": source_table,
                    "source_key": source_key,
                    "source_node_type": "context_overlay",
                    "memory_node_type": "causal_event_memory",
                    "evaluation_node_type": evaluation_node["node_type"],
                    "horizon_days": evaluation_node.get("horizon_days"),
                    "evaluated_at": evaluation_node.get("evaluated_at"),
                    "matured": evaluation_node.get("matured"),
                    "directional_helpfulness_score": evaluation_node.get("directional_helpfulness_score"),
                    "direction_hit_after_cost": evaluation_node.get("direction_hit_after_cost"),
                    "provenance_status": "complete_with_evaluation" if evaluation_row is not None else "memory_without_evaluation",
                    "authority_scope": "research_only",
                    "policy_effect": "provenance_audit_only_no_trade_authority",
                    "broker_execution_allowed": False,
                    "policy_auto_promotion_allowed": False,
                    "source_node_json": _json_dumps(source_node),
                    "memory_node_json": _json_dumps(memory_node),
                    "evaluation_node_json": _json_dumps(evaluation_node),
                    "load_ts": load_ts,
                }
                row["provenance_id"] = _provenance_id(row)
                rows.append(row)
    return normalize_provenance_frame(pd.DataFrame(rows))


def _evaluation_node(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {
            "node_type": "not_evaluated",
            "authority_scope": "research_only",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
        }
    return {
        "node_type": "realized_outcome_label",
        "evaluated_at": pd.to_datetime(row.get("evaluated_at"), utc=True, errors="coerce"),
        "horizon_days": None if pd.isna(pd.to_numeric(row.get("horizon_days"), errors="coerce")) else int(row.get("horizon_days")),
        "entry_date": row.get("entry_date"),
        "exit_date": row.get("exit_date"),
        "forward_return_after_cost": _number(row.get("forward_return_after_cost")),
        "benchmark_forward_return": _number(row.get("benchmark_forward_return")),
        "excess_return_after_cost": _number(row.get("excess_return_after_cost")),
        "directional_helpfulness_score": _number(row.get("directional_helpfulness_score")),
        "excess_directional_helpfulness_score": _number(row.get("excess_directional_helpfulness_score")),
        "direction_hit_after_cost": _bool_or_none(row.get("direction_hit_after_cost")),
        "excess_direction_hit_after_cost": _bool_or_none(row.get("excess_direction_hit_after_cost")),
        "matured": _bool_or_none(row.get("matured")),
        "evaluation_label": row.get("evaluation_label"),
        "authority_scope": "research_only",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
    }


def normalize_provenance_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    for column in ["asof_date", "evaluated_at", "load_ts"]:
        if column in out.columns:
            out[column] = pd.to_datetime(out[column], utc=True, errors="coerce")
    for column in ["horizon_days"]:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce").astype("Int64")
    for column in ["directional_helpfulness_score"]:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce")
    for column in ["matured", "direction_hit_after_cost", "broker_execution_allowed", "policy_auto_promotion_allowed"]:
        if column in out.columns:
            out[column] = out[column].map(_bool_or_none).astype("boolean")
    if "symbol" in out.columns:
        out["symbol"] = out["symbol"].astype("string").str.strip().str.upper()
    return out.dropna(subset=["provenance_id", "asof_date", "memory_id", "context_source"])


def build_causal_event_provenance(
    *,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    end = pd.to_datetime(to_date or pd.Timestamp.utcnow(), utc=True).normalize()
    start = pd.to_datetime(from_date or (end - pd.Timedelta(days=DEFAULT_LOOKBACK_DAYS)), utc=True).normalize()
    memory = load_memory_rows(from_date=start, to_date=end, limit=limit)
    evaluations = load_evaluation_rows(from_date=start, to_date=end, limit=max(int(limit), 1) * 3)
    provenance = build_provenance_rows(memory, evaluations)
    by_status = provenance["provenance_status"].value_counts(dropna=False).to_dict() if not provenance.empty else {}
    by_source = provenance["context_source"].value_counts(dropna=False).to_dict() if not provenance.empty else {}
    meta = {
        "status": "ok",
        "from_date": start.isoformat(),
        "to_date": end.isoformat(),
        "memory_rows": int(len(memory)),
        "evaluation_rows": int(len(evaluations)),
        "provenance_rows": int(len(provenance)),
        "provenance_status_counts": {str(key): int(value) for key, value in by_status.items()},
        "context_source_counts": {str(key): int(value) for key, value in by_source.items()},
        "table": TABLE_NAME,
        "authority_scope": "research_only",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
    }
    return provenance, meta


def persist_provenance(df: pd.DataFrame) -> None:
    if df.empty:
        return
    ensure_table()
    upsert_to_db(normalize_provenance_frame(df), TABLE_NAME, unique_keys=["provenance_id"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build typed source-to-memory-to-outcome provenance for causal event memory.")
    parser.add_argument("--from-date", type=parse_datetime_arg)
    parser.add_argument("--to-date", type=parse_datetime_arg)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--format", choices=["json", "text"], default="json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    provenance, meta = build_causal_event_provenance(
        from_date=args.from_date,
        to_date=args.to_date,
        limit=max(1, int(args.limit)),
    )
    if not args.dry_run:
        persist_provenance(provenance)
    payload = {
        **meta,
        "persisted": not bool(args.dry_run),
        "sample": provenance.head(10).to_dict(orient="records") if not provenance.empty else [],
    }
    if args.format == "text":
        print(
            f"status={payload['status']} rows={payload['provenance_rows']} persisted={payload['persisted']} "
            f"from={payload['from_date']} to={payload['to_date']}"
        )
        print(f"status_counts={payload['provenance_status_counts']}")
        print(f"source_counts={payload['context_source_counts']}")
    else:
        print(_json_dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
