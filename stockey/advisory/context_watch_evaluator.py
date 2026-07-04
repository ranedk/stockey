from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from advisory.fallback_telemetry import record_local_fallback_event
from advisory.return_attribution import attach_benchmark_forward_returns, load_benchmark_history_for_attribution
from advisory.signal_refresh import TABLE_NAME as SIGNAL_REFRESH_TABLE
from advisory.technical_threshold_calibration import attach_forward_returns, load_price_history_for_returns
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration
from utils.sync import parse_datetime_arg


EVALUATIONS_TABLE = "advisory_context_watch_evaluations"
SUMMARY_TABLE = "advisory_context_watch_eval_summary"
SCHEMA_MIGRATION_ID = "20260621_advisory_context_watch_evaluator"
CONTEXT_CLASS_SCHEMA_MIGRATION_ID = "20260621_advisory_context_watch_evaluator_context_class"
WATCH_STATE_SCHEMA_MIGRATION_ID = "20260621_advisory_context_watch_evaluator_watch_state"
WATCH_BREAKOUT_BLOCKER_SCHEMA_MIGRATION_ID = "20260621_advisory_context_watch_evaluator_breakout_blocker"
BENCHMARK_ATTRIBUTION_SCHEMA_MIGRATION_ID = "20260621_advisory_context_watch_evaluator_benchmark_attribution"
SECTOR_DIAGNOSTICS_SCHEMA_MIGRATION_ID = "20260621_advisory_context_watch_evaluator_sector_diagnostics"
DEFAULT_HORIZONS = [5, 10, 20]
DEFAULT_RETURN_THRESHOLD = 0.03
DEFAULT_COST_BPS = 25.0
DEFAULT_MIN_MATURED_ROWS = 10

SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {EVALUATIONS_TABLE} (
        evaluated_at TIMESTAMPTZ NOT NULL,
        horizon_days BIGINT NOT NULL,
        refresh_id TEXT NOT NULL,
        refreshed_at TIMESTAMPTZ NOT NULL,
        asof_date TIMESTAMPTZ,
        symbol TEXT NOT NULL,
        unique_id TEXT,
        signal_source TEXT,
        effect_type TEXT,
        source_context TEXT,
        context_class TEXT,
        context_candidate_state TEXT,
        context_policy_effect TEXT,
        watch_breakout_blocker TEXT,
        confidence DOUBLE PRECISION,
        action_changed BOOLEAN,
        entry_date TIMESTAMPTZ,
        exit_date TIMESTAMPTZ,
        entry_close DOUBLE PRECISION,
        exit_close DOUBLE PRECISION,
        forward_return DOUBLE PRECISION,
        benchmark_name TEXT,
        benchmark_entry_date TIMESTAMPTZ,
        benchmark_exit_date TIMESTAMPTZ,
        benchmark_entry_close DOUBLE PRECISION,
        benchmark_exit_close DOUBLE PRECISION,
        benchmark_forward_return DOUBLE PRECISION,
        watch_return_after_cost DOUBLE PRECISION,
        excess_watch_return_after_cost DOUBLE PRECISION,
        opportunity_hit_after_cost BOOLEAN,
        excess_opportunity_hit_after_cost BOOLEAN,
        matured BOOLEAN,
        raw_context_json TEXT,
        authority TEXT,
        broker_execution_allowed BOOLEAN,
        policy_auto_promotion_allowed BOOLEAN,
        load_ts TIMESTAMPTZ,
        UNIQUE (evaluated_at, horizon_days, refresh_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {SUMMARY_TABLE} (
        evaluated_at TIMESTAMPTZ NOT NULL,
        horizon_days BIGINT NOT NULL,
        signal_source TEXT NOT NULL,
        effect_type TEXT,
        source_context TEXT,
        context_class TEXT,
        context_candidate_state TEXT,
        context_policy_effect TEXT,
        watch_breakout_blocker TEXT,
        sample_count BIGINT,
        matured_count BIGINT,
        symbol_count BIGINT,
        avg_forward_return DOUBLE PRECISION,
        avg_benchmark_forward_return DOUBLE PRECISION,
        avg_watch_return_after_cost DOUBLE PRECISION,
        avg_excess_watch_return_after_cost DOUBLE PRECISION,
        opportunity_hit_rate_after_cost DOUBLE PRECISION,
        excess_opportunity_hit_rate_after_cost DOUBLE PRECISION,
        negative_after_cost_rate DOUBLE PRECISION,
        negative_excess_after_cost_rate DOUBLE PRECISION,
        avg_confidence DOUBLE PRECISION,
        sample_start TIMESTAMPTZ,
        sample_end TIMESTAMPTZ,
        classification TEXT,
        recommendation TEXT,
        authority TEXT,
        broker_execution_allowed BOOLEAN,
        policy_auto_promotion_allowed BOOLEAN,
        load_ts TIMESTAMPTZ,
        UNIQUE (evaluated_at, horizon_days, signal_source, effect_type, source_context, context_class, context_candidate_state, context_policy_effect, watch_breakout_blocker)
    )
    """,
]

BENCHMARK_ATTRIBUTION_SCHEMA_STATEMENTS = [
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS benchmark_name TEXT",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS benchmark_entry_date TIMESTAMPTZ",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS benchmark_exit_date TIMESTAMPTZ",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS benchmark_entry_close DOUBLE PRECISION",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS benchmark_exit_close DOUBLE PRECISION",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS benchmark_forward_return DOUBLE PRECISION",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS excess_watch_return_after_cost DOUBLE PRECISION",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS excess_opportunity_hit_after_cost BOOLEAN",
    f"ALTER TABLE {SUMMARY_TABLE} ADD COLUMN IF NOT EXISTS avg_benchmark_forward_return DOUBLE PRECISION",
    f"ALTER TABLE {SUMMARY_TABLE} ADD COLUMN IF NOT EXISTS avg_excess_watch_return_after_cost DOUBLE PRECISION",
    f"ALTER TABLE {SUMMARY_TABLE} ADD COLUMN IF NOT EXISTS excess_opportunity_hit_rate_after_cost DOUBLE PRECISION",
    f"ALTER TABLE {SUMMARY_TABLE} ADD COLUMN IF NOT EXISTS negative_excess_after_cost_rate DOUBLE PRECISION",
]

SECTOR_DIAGNOSTICS_SCHEMA_STATEMENTS = [
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS sector_name TEXT",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS sector_code TEXT",
    f"ALTER TABLE {SUMMARY_TABLE} ADD COLUMN IF NOT EXISTS sector_diagnostics_json TEXT",
]

CONTEXT_CLASS_SCHEMA_STATEMENTS = [
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS context_class TEXT",
    f"ALTER TABLE {SUMMARY_TABLE} ADD COLUMN IF NOT EXISTS context_class TEXT",
]

WATCH_STATE_SCHEMA_STATEMENTS = [
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS context_candidate_state TEXT",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS context_policy_effect TEXT",
    f"ALTER TABLE {SUMMARY_TABLE} ADD COLUMN IF NOT EXISTS context_candidate_state TEXT",
    f"ALTER TABLE {SUMMARY_TABLE} ADD COLUMN IF NOT EXISTS context_policy_effect TEXT",
    f"""
    CREATE UNIQUE INDEX IF NOT EXISTS advisory_context_watch_eval_summary_state_uidx
    ON {SUMMARY_TABLE} (
        evaluated_at,
        horizon_days,
        signal_source,
        effect_type,
        source_context,
        context_class,
        context_candidate_state,
        context_policy_effect
    )
    """,
]

WATCH_BREAKOUT_BLOCKER_SCHEMA_STATEMENTS = [
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS watch_breakout_blocker TEXT",
    f"ALTER TABLE {SUMMARY_TABLE} ADD COLUMN IF NOT EXISTS watch_breakout_blocker TEXT",
    f"UPDATE {EVALUATIONS_TABLE} SET watch_breakout_blocker = 'none' WHERE watch_breakout_blocker IS NULL",
    f"UPDATE {SUMMARY_TABLE} SET watch_breakout_blocker = 'none' WHERE watch_breakout_blocker IS NULL",
    f"""
    DO $$
    DECLARE
        rec RECORD;
    BEGIN
        FOR rec IN
            SELECT
                con.conname AS constraint_name,
                idx.relname AS index_name
            FROM pg_index i
            JOIN pg_class tbl ON tbl.oid = i.indrelid
            JOIN pg_namespace ns ON ns.oid = tbl.relnamespace
            JOIN pg_class idx ON idx.oid = i.indexrelid
            LEFT JOIN pg_constraint con ON con.conindid = i.indexrelid
            WHERE ns.nspname = 'public'
              AND tbl.relname = '{SUMMARY_TABLE}'
              AND i.indisunique
              AND (
                  SELECT array_agg(att.attname::text ORDER BY keys.ord)
                  FROM unnest(i.indkey) WITH ORDINALITY AS keys(attnum, ord)
                  JOIN pg_attribute att
                    ON att.attrelid = tbl.oid
                   AND att.attnum = keys.attnum
              ) = ARRAY['evaluated_at', 'horizon_days', 'signal_source', 'effect_type', 'source_context', 'context_class', 'context_candidate_state', 'context_policy_effect']::text[]
        LOOP
            IF rec.constraint_name IS NOT NULL THEN
                EXECUTE format('ALTER TABLE %I DROP CONSTRAINT IF EXISTS %I', '{SUMMARY_TABLE}', rec.constraint_name);
            ELSE
                EXECUTE format('DROP INDEX IF EXISTS %I', rec.index_name);
            END IF;
        END LOOP;
    END $$;
    """,
    f"""
    CREATE UNIQUE INDEX IF NOT EXISTS advisory_context_watch_eval_summary_blocker_uidx
    ON {SUMMARY_TABLE} (
        evaluated_at,
        horizon_days,
        signal_source,
        effect_type,
        source_context,
        context_class,
        context_candidate_state,
        context_policy_effect,
        watch_breakout_blocker
    )
    """,
]


def _record_context_watch_fallback(
    *,
    fallback_type: str,
    source: str,
    reason: str,
    error: Exception,
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="advisory.context_watch_evaluator",
        fallback_type=fallback_type,
        source=source,
        severity="warn",
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def _json_ready(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    try:
        if pd.isna(value):
            return None
    except Exception as exc:
        _record_context_watch_fallback(
            fallback_type="context_watch_json_ready_missing_check_failed",
            source="json_ready",
            reason="Context-watch evaluator could not evaluate missingness while preparing JSON output.",
            error=exc,
            metadata={"value_type": type(value).__name__},
        )
    return value


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _parse_jsonish(value: Any, default: Any, *, source: str) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        if pd.isna(value):
            return default
    except Exception as exc:
        _record_context_watch_fallback(
            fallback_type="context_watch_json_missing_check_failed",
            source=source,
            reason="Context-watch evaluator could not evaluate missingness while parsing JSON.",
            error=exc,
            metadata={"value_type": type(value).__name__, "default_type": type(default).__name__},
        )
    try:
        return json.loads(str(value))
    except Exception as exc:
        if str(value or "").strip():
            _record_context_watch_fallback(
                fallback_type="context_watch_json_parse_failed",
                source=source,
                reason="Context-watch evaluator could not parse JSON payload; using default.",
                error=exc,
                metadata={"payload_length": len(str(value))},
            )
        return default


def _boolish(value: Any) -> bool | None:
    if value is None:
        return None
    if value is True:
        return True
    if value is False:
        return False
    text = str(value).strip().lower()
    if text in {"true", "t", "1", "yes", "y"}:
        return True
    if text in {"false", "f", "0", "no", "n"}:
        return False
    return None


def _number(value: Any) -> float | None:
    numeric = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(numeric) else float(numeric)


def _benchmark_attribution_coverage(evaluations: pd.DataFrame) -> dict[str, Any]:
    if evaluations.empty:
        return {
            "matured_rows": 0,
            "benchmark_attributed_matured_rows": 0,
            "benchmark_missing_matured_rows": 0,
            "benchmark_attribution_status": "no_matured_labels",
        }
    frame = evaluations.copy()
    matured = frame[frame.get("matured", pd.Series(dtype=bool)).fillna(False).astype(bool)]
    if matured.empty:
        return {
            "matured_rows": 0,
            "benchmark_attributed_matured_rows": 0,
            "benchmark_missing_matured_rows": 0,
            "benchmark_attribution_status": "no_matured_labels",
        }
    benchmark_available = pd.to_numeric(matured.get("benchmark_forward_return"), errors="coerce").notna()
    attributed = int(benchmark_available.sum())
    missing = int((~benchmark_available).sum())
    return {
        "matured_rows": int(len(matured)),
        "benchmark_attributed_matured_rows": attributed,
        "benchmark_missing_matured_rows": missing,
        "benchmark_attribution_status": "available" if missing == 0 else "missing" if attributed == 0 else "partial",
    }


def _source_context(row: pd.Series) -> str | None:
    payload = _parse_jsonish(row.get("action_payload_json"), {}, source="signal_refresh.action_payload_json")
    if not isinstance(payload, dict):
        return None
    explicit = str(payload.get("source_context") or "").strip().lower()
    if explicit:
        return explicit
    watch = payload.get("context_watch")
    if isinstance(watch, dict):
        return str(watch.get("context_source") or watch.get("source_family") or "").strip().lower() or "context_watch"
    return None


def _context_class(row: pd.Series) -> str | None:
    payload = _parse_jsonish(row.get("action_payload_json"), {}, source="signal_refresh.action_payload_json")
    if not isinstance(payload, dict):
        return None
    explicit = str(payload.get("context_class") or "").strip().upper()
    if explicit:
        return explicit
    watch = payload.get("context_watch")
    if isinstance(watch, dict):
        return str(watch.get("context_class") or "").strip().upper() or None
    direct = payload.get("direct_context")
    if isinstance(direct, dict):
        return str(direct.get("context_class") or "").strip().upper() or None
    theme = payload.get("theme_context")
    if isinstance(theme, dict):
        return str(theme.get("theme_id") or theme.get("theme_name") or "").strip().upper() or None
    return None


def _sector_context(row: pd.Series) -> tuple[str | None, str | None]:
    payload = _parse_jsonish(row.get("action_payload_json"), {}, source="signal_refresh.action_payload_json")
    if not isinstance(payload, dict):
        return None, None
    candidates: list[dict[str, Any]] = [payload]
    for key in ["context_watch", "direct_context", "theme_context"]:
        item = payload.get(key)
        if isinstance(item, dict):
            candidates.append(item)
    sector_name = None
    sector_code = None
    for candidate in candidates:
        if sector_name is None:
            for key in ["sector_name", "universe_sector_name", "overlay_sector_name", "affected_sector", "affected_sector_name"]:
                value = str(candidate.get(key) or "").strip()
                if value:
                    sector_name = value
                    break
        if sector_code is None:
            for key in ["sector_code", "universe_sector_code", "overlay_sector_code", "affected_sector_code"]:
                value = str(candidate.get(key) or "").strip().upper()
                if value:
                    sector_code = value
                    break
        if sector_name or sector_code:
            break
    return sector_name, sector_code


def _context_candidate_state(row: pd.Series) -> str:
    payload = _parse_jsonish(row.get("action_payload_json"), {}, source="signal_refresh.action_payload_json")
    values: list[Any] = []
    if isinstance(payload, dict):
        values.extend(
            [
                payload.get("context_candidate_state"),
                payload.get("candidate_state"),
                payload.get("current_state"),
            ]
        )
        watch = payload.get("context_watch")
        if isinstance(watch, dict):
            values.extend([watch.get("candidate_state"), watch.get("current_state")])
    values.extend([row.get("signal_status"), row.get("action_detail")])
    for value in values:
        text = str(value or "").strip().upper()
        if text:
            return text
    return "WATCH_EVENT"


def _context_policy_effect(row: pd.Series) -> str:
    payload = _parse_jsonish(row.get("action_payload_json"), {}, source="signal_refresh.action_payload_json")
    values: list[Any] = []
    if isinstance(payload, dict):
        values.extend([payload.get("context_policy_effect"), payload.get("policy_effect")])
        watch = payload.get("context_watch")
        if isinstance(watch, dict):
            values.append(watch.get("context_policy_effect") or watch.get("policy_effect"))
    for value in values:
        text = str(value or "").strip().lower()
        if text:
            return text
    return "watch_only_no_buy_authority"


def _watch_breakout_blocker(row: pd.Series) -> str:
    payload = _parse_jsonish(row.get("action_payload_json"), {}, source="signal_refresh.action_payload_json")
    if not isinstance(payload, dict):
        return "none"
    audit = payload.get("watch_reason_audit")
    if not isinstance(audit, dict):
        audit = {}
    if bool(audit.get("context_class_blocks_breakout")):
        return "context_class"
    if bool(audit.get("watch_breakout_priority_blocked")):
        return "watch_state"
    watch = payload.get("context_watch")
    if isinstance(watch, dict):
        reasons = _parse_jsonish(watch.get("watch_reasons") or watch.get("watch_reasons_json"), [], source="context_watch.watch_reasons")
        reason = reasons if isinstance(reasons, dict) else reasons[0] if isinstance(reasons, list) and reasons and isinstance(reasons[0], dict) else {}
        if isinstance(reason, dict):
            if bool(reason.get("context_class_blocks_breakout")):
                return "context_class"
            if bool(reason.get("watch_breakout_priority_blocked")):
                return "watch_state"
    return "none"


def ensure_tables() -> None:
    apply_schema_migration(
        migration_id=SCHEMA_MIGRATION_ID,
        description="Create research-only positive context-watch outcome evaluator tables.",
        statements=SCHEMA_STATEMENTS,
        metadata={
            "tables": [EVALUATIONS_TABLE, SUMMARY_TABLE],
            "workflow": "context_watch_evaluator",
            "authority": "research_only",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
        },
    )
    apply_schema_migration(
        migration_id=CONTEXT_CLASS_SCHEMA_MIGRATION_ID,
        description="Add context class dimension to positive context-watch evaluator outputs.",
        statements=CONTEXT_CLASS_SCHEMA_STATEMENTS,
        metadata={
            "tables": [EVALUATIONS_TABLE, SUMMARY_TABLE],
            "workflow": "context_watch_evaluator",
            "authority": "research_only",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
        },
    )
    apply_schema_migration(
        migration_id=WATCH_STATE_SCHEMA_MIGRATION_ID,
        description="Add watch candidate state and policy effect dimensions to context-watch evaluator outputs.",
        statements=WATCH_STATE_SCHEMA_STATEMENTS,
        metadata={
            "tables": [EVALUATIONS_TABLE, SUMMARY_TABLE],
            "workflow": "context_watch_evaluator",
            "authority": "research_only",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
        },
    )
    apply_schema_migration(
        migration_id=WATCH_BREAKOUT_BLOCKER_SCHEMA_MIGRATION_ID,
        description="Add watch-breakout blocker dimension to context-watch evaluator outputs.",
        statements=WATCH_BREAKOUT_BLOCKER_SCHEMA_STATEMENTS,
        metadata={
            "tables": [EVALUATIONS_TABLE, SUMMARY_TABLE],
            "workflow": "context_watch_evaluator",
            "authority": "research_only",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
        },
    )
    apply_schema_migration(
        migration_id=BENCHMARK_ATTRIBUTION_SCHEMA_MIGRATION_ID,
        description="Add benchmark attribution to context-watch evaluator outputs.",
        statements=BENCHMARK_ATTRIBUTION_SCHEMA_STATEMENTS,
        metadata={
            "tables": [EVALUATIONS_TABLE, SUMMARY_TABLE],
            "workflow": "context_watch_evaluator",
            "authority": "research_only",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
            "attribution": "benchmark_excess_return",
        },
    )
    apply_schema_migration(
        migration_id=SECTOR_DIAGNOSTICS_SCHEMA_MIGRATION_ID,
        description="Add compact sector diagnostics to context-watch evaluator outputs.",
        statements=SECTOR_DIAGNOSTICS_SCHEMA_STATEMENTS,
        metadata={
            "tables": [EVALUATIONS_TABLE, SUMMARY_TABLE],
            "workflow": "context_watch_evaluator",
            "authority": "research_only",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
            "dimension": "sector",
        },
    )


def load_context_watch_signals(
    *,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
) -> pd.DataFrame:
    clauses = [
        "signal_action = 'WATCH'",
        "COALESCE(dry_run, false) IS FALSE",
        "COALESCE(broker_execution_allowed, false) IS FALSE",
        "effect_type = 'context_overlay_watch_pressure'",
    ]
    params: list[Any] = []
    if from_date is not None:
        clauses.append("refreshed_at >= %s")
        params.append(from_date)
    if to_date is not None:
        clauses.append("refreshed_at <= %s")
        params.append(to_date)
    if symbols:
        clauses.append("UPPER(TRIM(symbol)) = ANY(%s)")
        params.append([str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()])
    try:
        df = sql_to_df(
            f"""
            SELECT
                refresh_id,
                refreshed_at,
                COALESCE(asof_date, refreshed_at) AS asof_date,
                UPPER(TRIM(symbol)) AS symbol,
                unique_id,
                signal_source,
                signal_status,
                effect_type,
                confidence,
                action_changed,
                action_reason,
                action_payload_json
            FROM {SIGNAL_REFRESH_TABLE}
            WHERE {' AND '.join(clauses)}
            ORDER BY refreshed_at, symbol, refresh_id
            """,
            params=tuple(params) if params else None,
            retries=4,
            statement_timeout_ms=0,
            chunksize=50000,
        )
    except Exception as exc:
        _record_context_watch_fallback(
            fallback_type="context_watch_signal_rows_load_failed",
            source=SIGNAL_REFRESH_TABLE,
            reason="Context-watch evaluator could not load review-only signal-refresh WATCH rows.",
            error=exc,
            metadata={
                "from_date": str(from_date) if from_date is not None else None,
                "to_date": str(to_date) if to_date is not None else None,
                "symbol_count": len(symbols or []),
            },
        )
        raise
    if df.empty:
        return df
    out = df.copy()
    out["symbol"] = out["symbol"].astype("string").str.strip().str.upper()
    out["refreshed_at"] = pd.to_datetime(out["refreshed_at"], utc=True, errors="coerce")
    out["asof_date"] = pd.to_datetime(out["asof_date"], utc=True, errors="coerce").dt.normalize()
    out["confidence"] = pd.to_numeric(out.get("confidence"), errors="coerce")
    out["source_context"] = out.apply(_source_context, axis=1)
    out["source_context"] = out["source_context"].fillna(out.get("signal_source", "")).astype("string").str.strip().str.lower()
    out["context_class"] = out.apply(_context_class, axis=1)
    out["context_class"] = out["context_class"].astype("string").str.strip().str.upper()
    out.loc[out["context_class"].isin(["", "<NA>", "NAN", "NONE"]), "context_class"] = "UNSPECIFIED"
    out["context_class"] = out["context_class"].fillna("UNSPECIFIED")
    sectors = out.apply(_sector_context, axis=1, result_type="expand")
    out["sector_name"] = sectors[0].astype("string").str.strip()
    out.loc[out["sector_name"].isin(["", "<NA>", "NAN", "NONE"]), "sector_name"] = pd.NA
    out["sector_code"] = sectors[1].astype("string").str.strip().str.upper()
    out.loc[out["sector_code"].isin(["", "<NA>", "NAN", "NONE"]), "sector_code"] = pd.NA
    out["context_candidate_state"] = out.apply(_context_candidate_state, axis=1)
    out["context_candidate_state"] = out["context_candidate_state"].astype("string").str.strip().str.upper()
    out.loc[out["context_candidate_state"].isin(["", "<NA>", "NAN", "NONE"]), "context_candidate_state"] = "WATCH_EVENT"
    out["context_candidate_state"] = out["context_candidate_state"].fillna("WATCH_EVENT")
    out["context_policy_effect"] = out.apply(_context_policy_effect, axis=1)
    out["context_policy_effect"] = out["context_policy_effect"].astype("string").str.strip().str.lower()
    out.loc[out["context_policy_effect"].isin(["", "<na>", "nan", "none"]), "context_policy_effect"] = "watch_only_no_buy_authority"
    out["context_policy_effect"] = out["context_policy_effect"].fillna("watch_only_no_buy_authority")
    out["watch_breakout_blocker"] = out.apply(_watch_breakout_blocker, axis=1)
    out["watch_breakout_blocker"] = out["watch_breakout_blocker"].astype("string").str.strip().str.lower()
    out.loc[out["watch_breakout_blocker"].isin(["", "<na>", "nan"]), "watch_breakout_blocker"] = "none"
    out["watch_breakout_blocker"] = out["watch_breakout_blocker"].fillna("none")
    out["action_changed"] = out.get("action_changed", pd.Series(dtype=object)).map(_boolish)
    return out.dropna(subset=["refresh_id", "refreshed_at", "asof_date", "symbol"])


def build_evaluation_rows(
    dataset: pd.DataFrame,
    *,
    horizons: list[int],
    cost_bps: float = DEFAULT_COST_BPS,
    return_threshold: float = DEFAULT_RETURN_THRESHOLD,
    evaluated_at: pd.Timestamp | None = None,
) -> pd.DataFrame:
    if dataset.empty:
        return pd.DataFrame()
    effective_evaluated_at = pd.to_datetime(evaluated_at or pd.Timestamp.utcnow(), utc=True, errors="coerce")
    cost = float(cost_bps) / 10000.0
    rows: list[dict[str, Any]] = []
    for _, row in dataset.iterrows():
        for horizon in horizons:
            horizon = int(horizon)
            forward_return = _number(row.get(f"forward_return_h{horizon}"))
            benchmark_forward_return = _number(row.get(f"benchmark_forward_return_h{horizon}"))
            entry_date = pd.to_datetime(row.get(f"entry_date_h{horizon}"), utc=True, errors="coerce")
            exit_date = pd.to_datetime(row.get(f"exit_date_h{horizon}"), utc=True, errors="coerce")
            benchmark_entry_date = pd.to_datetime(row.get(f"benchmark_entry_date_h{horizon}"), utc=True, errors="coerce")
            benchmark_exit_date = pd.to_datetime(row.get(f"benchmark_exit_date_h{horizon}"), utc=True, errors="coerce")
            matured = forward_return is not None
            watch_return = None if forward_return is None else float(forward_return - cost)
            excess_watch_return = (
                None
                if forward_return is None or benchmark_forward_return is None
                else float(forward_return - benchmark_forward_return - cost)
            )
            rows.append(
                {
                    "evaluated_at": effective_evaluated_at,
                    "horizon_days": horizon,
                    "refresh_id": row.get("refresh_id"),
                    "refreshed_at": row.get("refreshed_at"),
                    "asof_date": row.get("asof_date"),
                    "symbol": row.get("symbol"),
                    "unique_id": row.get("unique_id"),
                    "signal_source": row.get("signal_source"),
                    "effect_type": row.get("effect_type"),
                    "source_context": row.get("source_context"),
                    "context_class": row.get("context_class"),
                    "sector_name": row.get("sector_name"),
                    "sector_code": row.get("sector_code"),
                    "context_candidate_state": row.get("context_candidate_state") or "WATCH_EVENT",
                    "context_policy_effect": row.get("context_policy_effect") or "watch_only_no_buy_authority",
                    "watch_breakout_blocker": row.get("watch_breakout_blocker") or "none",
                    "confidence": row.get("confidence"),
                    "action_changed": row.get("action_changed"),
                    "entry_date": None if pd.isna(entry_date) else entry_date,
                    "exit_date": None if pd.isna(exit_date) else exit_date,
                    "entry_close": row.get(f"entry_close_h{horizon}"),
                    "exit_close": row.get(f"exit_close_h{horizon}"),
                    "forward_return": forward_return,
                    "benchmark_name": row.get("benchmark_name"),
                    "benchmark_entry_date": None if pd.isna(benchmark_entry_date) else benchmark_entry_date,
                    "benchmark_exit_date": None if pd.isna(benchmark_exit_date) else benchmark_exit_date,
                    "benchmark_entry_close": row.get(f"benchmark_entry_close_h{horizon}"),
                    "benchmark_exit_close": row.get(f"benchmark_exit_close_h{horizon}"),
                    "benchmark_forward_return": benchmark_forward_return,
                    "watch_return_after_cost": watch_return,
                    "excess_watch_return_after_cost": excess_watch_return,
                    "opportunity_hit_after_cost": None if watch_return is None else bool(watch_return >= float(return_threshold)),
                    "excess_opportunity_hit_after_cost": None if excess_watch_return is None else bool(excess_watch_return >= float(return_threshold)),
                    "matured": bool(matured),
                    "raw_context_json": json_dumps(
                        {
                            "action_reason": row.get("action_reason"),
                            "action_payload": _parse_jsonish(row.get("action_payload_json"), {}, source="signal_refresh.action_payload_json"),
                            "point_in_time_return_contract": _parse_jsonish(
                                row.get("point_in_time_return_contract_json"),
                                {},
                                source="point_in_time_return_contract_json",
                            ),
                            "benchmark_return_contract": _parse_jsonish(
                                row.get("benchmark_return_contract_json"),
                                {},
                                source="benchmark_return_contract_json",
                            ),
                            "label_interpretation": "For context WATCH pressure, positive watch_return_after_cost means watching this overlay family found later upside after estimated costs. Positive excess_watch_return_after_cost means it also beat the benchmark after costs.",
                            "candidate_state_interpretation": "WATCH_BREAKOUT rows are still research-only WATCH rows; this field only lets validation measure whether reduced screening friction worked after costs.",
                            "watch_breakout_blocker_interpretation": "none means no blocker was recorded; context_class or watch_state means breakout urgency was intentionally downgraded before evaluation.",
                        }
                    ),
                    "authority": "research_only",
                    "broker_execution_allowed": False,
                    "policy_auto_promotion_allowed": False,
                    "load_ts": pd.Timestamp.utcnow(),
                }
            )
    return normalize_evaluation_frame(pd.DataFrame(rows))


def _summary_classification(row: dict[str, Any], *, min_matured_rows: int) -> str:
    matured = int(_number(row.get("matured_count")) or 0)
    avg_watch_return = _number(row.get("avg_watch_return_after_cost"))
    avg_excess_watch_return = _number(row.get("avg_excess_watch_return_after_cost"))
    hit_rate = _number(row.get("opportunity_hit_rate_after_cost"))
    excess_hit_rate = _number(row.get("excess_opportunity_hit_rate_after_cost"))
    negative_rate = _number(row.get("negative_after_cost_rate"))
    negative_excess_rate = _number(row.get("negative_excess_after_cost_rate"))
    if matured < int(min_matured_rows):
        return "needs_more_data"
    if (
        avg_watch_return is not None
        and avg_watch_return > 0
        and avg_excess_watch_return is not None
        and avg_excess_watch_return > 0
        and hit_rate is not None
        and hit_rate >= 0.45
        and excess_hit_rate is not None
        and excess_hit_rate >= 0.45
    ):
        return "opportunity_candidate"
    if (negative_rate is not None and negative_rate >= 0.60) or (negative_excess_rate is not None and negative_excess_rate >= 0.60):
        return "harmful_watch_noise"
    return "mixed_or_weak"


def _summary_recommendation(classification: str) -> str:
    if classification == "opportunity_candidate":
        return "eligible_for_review_only_watch_policy_research"
    if classification == "harmful_watch_noise":
        return "suppress_or_rework_context_watch_source_before_policy_influence"
    if classification == "mixed_or_weak":
        return "keep_watch_only_and_split_by_context_class"
    return "collect_more_matured_context_watch_labels"


def _na_safe_text(value: Any) -> str:
    # Nullable (pd.NA) columns raise "boolean value of NA is ambiguous" on `value or ""`,
    # so coerce to text without any truthiness test on a possibly-NA scalar.
    if value is None or pd.isna(value):
        return ""
    return str(value)


def _sector_key(row: pd.Series) -> str:
    sector_code = _na_safe_text(row.get("sector_code")).strip().upper()
    if sector_code and sector_code not in {"<NA>", "NAN", "NONE"}:
        return sector_code
    sector_name = _na_safe_text(row.get("sector_name")).strip()
    if sector_name and sector_name.upper() not in {"<NA>", "NAN", "NONE"}:
        return sector_name.upper()
    return "UNSPECIFIED"


def _first_text(series: pd.Series) -> str | None:
    for value in series.dropna().tolist():
        text = str(value).strip()
        if text and text.upper() not in {"<NA>", "NAN", "NONE"}:
            return text
    return None


def _sector_diagnostics(group: pd.DataFrame, *, min_matured_rows: int) -> list[dict[str, Any]]:
    if "sector_name" not in group.columns and "sector_code" not in group.columns:
        return []
    working = group.copy()
    working["_sector_key"] = working.apply(_sector_key, axis=1)
    diagnostics: list[dict[str, Any]] = []
    for sector_key, sector_group in working.groupby("_sector_key", dropna=False, sort=True):
        matured = sector_group[sector_group["matured"].fillna(False).astype(bool)]
        forward = pd.to_numeric(matured.get("forward_return"), errors="coerce").dropna()
        benchmark_forward = pd.to_numeric(matured.get("benchmark_forward_return"), errors="coerce").dropna()
        watch_return = pd.to_numeric(matured.get("watch_return_after_cost"), errors="coerce").dropna()
        excess_watch_return = pd.to_numeric(matured.get("excess_watch_return_after_cost"), errors="coerce").dropna()
        hits = matured.get("opportunity_hit_after_cost", pd.Series(dtype=bool)).dropna().astype(bool)
        excess_hits = matured.get("excess_opportunity_hit_after_cost", pd.Series(dtype=bool)).dropna().astype(bool)
        negative = watch_return.lt(0) if not watch_return.empty else pd.Series(dtype=bool)
        negative_excess = excess_watch_return.lt(0) if not excess_watch_return.empty else pd.Series(dtype=bool)
        summary_row = {
            "sector_key": str(sector_key),
            "sector_name": _first_text(matured["sector_name"]) if "sector_name" in matured.columns and not matured.empty else None,
            "sector_code": (_first_text(matured["sector_code"]) or "").upper() or None if "sector_code" in matured.columns and not matured.empty else None,
            "sample_count": int(len(sector_group)),
            "matured_count": int(len(matured)),
            "symbol_count": int(matured["symbol"].nunique()) if not matured.empty else 0,
            "avg_forward_return": None if forward.empty else float(forward.mean()),
            "avg_benchmark_forward_return": None if benchmark_forward.empty else float(benchmark_forward.mean()),
            "avg_watch_return_after_cost": None if watch_return.empty else float(watch_return.mean()),
            "avg_excess_watch_return_after_cost": None if excess_watch_return.empty else float(excess_watch_return.mean()),
            "opportunity_hit_rate_after_cost": None if hits.empty else float(hits.mean()),
            "excess_opportunity_hit_rate_after_cost": None if excess_hits.empty else float(excess_hits.mean()),
            "negative_after_cost_rate": None if negative.empty else float(negative.mean()),
            "negative_excess_after_cost_rate": None if negative_excess.empty else float(negative_excess.mean()),
            "authority": "research_only",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
        }
        summary_row["classification"] = _summary_classification(summary_row, min_matured_rows=min_matured_rows)
        summary_row["recommendation"] = _summary_recommendation(summary_row["classification"])
        diagnostics.append(summary_row)
    diagnostics.sort(
        key=lambda item: (
            item.get("classification") == "opportunity_candidate",
            item.get("classification") == "harmful_watch_noise",
            int(item.get("matured_count") or 0),
        ),
        reverse=True,
    )
    return diagnostics


def summarize_evaluations(evaluations: pd.DataFrame, *, min_matured_rows: int = DEFAULT_MIN_MATURED_ROWS) -> pd.DataFrame:
    if evaluations.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    frame = normalize_evaluation_frame(evaluations)
    group_cols = [
        "evaluated_at",
        "horizon_days",
        "signal_source",
        "effect_type",
        "source_context",
        "context_class",
        "context_candidate_state",
        "context_policy_effect",
        "watch_breakout_blocker",
    ]
    for keys, group in frame.groupby(group_cols, dropna=False, sort=True):
        evaluated_at, horizon_days, signal_source, effect_type, source_context, context_class, context_candidate_state, context_policy_effect, watch_breakout_blocker = keys
        matured = group[group["matured"].fillna(False).astype(bool)]
        forward = pd.to_numeric(matured.get("forward_return"), errors="coerce").dropna()
        benchmark_forward = pd.to_numeric(matured.get("benchmark_forward_return"), errors="coerce").dropna()
        watch_return = pd.to_numeric(matured.get("watch_return_after_cost"), errors="coerce").dropna()
        excess_watch_return = pd.to_numeric(matured.get("excess_watch_return_after_cost"), errors="coerce").dropna()
        hits = matured.get("opportunity_hit_after_cost", pd.Series(dtype=bool)).dropna().astype(bool)
        excess_hits = matured.get("excess_opportunity_hit_after_cost", pd.Series(dtype=bool)).dropna().astype(bool)
        negative = watch_return.lt(0) if not watch_return.empty else pd.Series(dtype=bool)
        negative_excess = excess_watch_return.lt(0) if not excess_watch_return.empty else pd.Series(dtype=bool)
        summary_row = {
            "evaluated_at": evaluated_at,
            "horizon_days": int(horizon_days),
            "signal_source": str(signal_source),
            "effect_type": None if pd.isna(effect_type) else str(effect_type),
            "source_context": None if pd.isna(source_context) else str(source_context),
            "context_class": None if pd.isna(context_class) else str(context_class),
            "sector_diagnostics_json": "[]",
            "context_candidate_state": None if pd.isna(context_candidate_state) else str(context_candidate_state),
            "context_policy_effect": None if pd.isna(context_policy_effect) else str(context_policy_effect),
            "watch_breakout_blocker": None if pd.isna(watch_breakout_blocker) else str(watch_breakout_blocker),
            "sample_count": int(len(group)),
            "matured_count": int(len(matured)),
            "symbol_count": int(matured["symbol"].nunique()) if not matured.empty else 0,
            "avg_forward_return": None if forward.empty else float(forward.mean()),
            "avg_benchmark_forward_return": None if benchmark_forward.empty else float(benchmark_forward.mean()),
            "avg_watch_return_after_cost": None if watch_return.empty else float(watch_return.mean()),
            "avg_excess_watch_return_after_cost": None if excess_watch_return.empty else float(excess_watch_return.mean()),
            "opportunity_hit_rate_after_cost": None if hits.empty else float(hits.mean()),
            "excess_opportunity_hit_rate_after_cost": None if excess_hits.empty else float(excess_hits.mean()),
            "negative_after_cost_rate": None if negative.empty else float(negative.mean()),
            "negative_excess_after_cost_rate": None if negative_excess.empty else float(negative_excess.mean()),
            "avg_confidence": None if matured["confidence"].dropna().empty else float(pd.to_numeric(matured["confidence"], errors="coerce").mean()),
            "sample_start": matured["asof_date"].min() if not matured.empty else pd.NaT,
            "sample_end": matured["asof_date"].max() if not matured.empty else pd.NaT,
            "authority": "research_only",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
            "load_ts": pd.Timestamp.utcnow(),
        }
        summary_row["sector_diagnostics_json"] = json_dumps(_sector_diagnostics(group, min_matured_rows=min_matured_rows))
        summary_row["classification"] = _summary_classification(summary_row, min_matured_rows=min_matured_rows)
        summary_row["recommendation"] = _summary_recommendation(summary_row["classification"])
        rows.append(summary_row)
    return normalize_summary_frame(pd.DataFrame(rows))


EVALUATION_NUMERIC_COLUMNS = [
    "horizon_days",
    "confidence",
    "entry_close",
    "exit_close",
    "forward_return",
    "benchmark_entry_close",
    "benchmark_exit_close",
    "benchmark_forward_return",
    "watch_return_after_cost",
    "excess_watch_return_after_cost",
]
EVALUATION_BOOL_COLUMNS = [
    "action_changed",
    "opportunity_hit_after_cost",
    "excess_opportunity_hit_after_cost",
    "matured",
    "broker_execution_allowed",
    "policy_auto_promotion_allowed",
]
EVALUATION_TS_COLUMNS = ["evaluated_at", "refreshed_at", "asof_date", "entry_date", "exit_date", "benchmark_entry_date", "benchmark_exit_date", "load_ts"]
SUMMARY_NUMERIC_COLUMNS = [
    "horizon_days",
    "sample_count",
    "matured_count",
    "symbol_count",
    "avg_forward_return",
    "avg_benchmark_forward_return",
    "avg_watch_return_after_cost",
    "avg_excess_watch_return_after_cost",
    "opportunity_hit_rate_after_cost",
    "excess_opportunity_hit_rate_after_cost",
    "negative_after_cost_rate",
    "negative_excess_after_cost_rate",
    "avg_confidence",
]
SUMMARY_BOOL_COLUMNS = ["broker_execution_allowed", "policy_auto_promotion_allowed"]
SUMMARY_TS_COLUMNS = ["evaluated_at", "sample_start", "sample_end", "load_ts"]


def _normalize_watch_state_columns(out: pd.DataFrame) -> pd.DataFrame:
    if "context_candidate_state" not in out.columns:
        out["context_candidate_state"] = "WATCH_EVENT"
    if "context_candidate_state" in out.columns:
        out["context_candidate_state"] = out["context_candidate_state"].astype("string").str.strip().str.upper()
        out.loc[out["context_candidate_state"].isin(["", "<NA>", "NAN", "NONE"]), "context_candidate_state"] = "WATCH_EVENT"
        out["context_candidate_state"] = out["context_candidate_state"].fillna("WATCH_EVENT")
    if "context_policy_effect" not in out.columns:
        out["context_policy_effect"] = "watch_only_no_buy_authority"
    if "context_policy_effect" in out.columns:
        out["context_policy_effect"] = out["context_policy_effect"].astype("string").str.strip().str.lower()
        out.loc[out["context_policy_effect"].isin(["", "<na>", "nan", "none"]), "context_policy_effect"] = "watch_only_no_buy_authority"
        out["context_policy_effect"] = out["context_policy_effect"].fillna("watch_only_no_buy_authority")
    if "watch_breakout_blocker" not in out.columns:
        out["watch_breakout_blocker"] = "none"
    if "watch_breakout_blocker" in out.columns:
        out["watch_breakout_blocker"] = out["watch_breakout_blocker"].astype("string").str.strip().str.lower()
        out.loc[out["watch_breakout_blocker"].isin(["", "<na>", "nan"]), "watch_breakout_blocker"] = "none"
        out["watch_breakout_blocker"] = out["watch_breakout_blocker"].fillna("none")
    return out


def _coerce_bool_series(series: pd.Series) -> pd.Series:
    if pd.api.types.is_bool_dtype(series):
        return series.astype("boolean")
    normalized = series.astype("string").str.strip().str.lower()
    mapped = normalized.map(
        {
            "true": True,
            "t": True,
            "1": True,
            "yes": True,
            "y": True,
            "false": False,
            "f": False,
            "0": False,
            "no": False,
            "n": False,
        }
    )
    return mapped.astype("boolean")


def normalize_evaluation_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    for column in EVALUATION_NUMERIC_COLUMNS:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce")
    for column in EVALUATION_BOOL_COLUMNS:
        if column in out.columns:
            out[column] = _coerce_bool_series(out[column])
    for column in EVALUATION_TS_COLUMNS:
        if column in out.columns:
            out[column] = pd.to_datetime(out[column], utc=True, errors="coerce")
    if "symbol" in out.columns:
        out["symbol"] = out["symbol"].astype("string").str.strip().str.upper()
    if "context_class" in out.columns:
        out["context_class"] = out["context_class"].astype("string").str.strip().str.upper()
        out.loc[out["context_class"].isin(["", "<NA>", "NAN", "NONE"]), "context_class"] = "UNSPECIFIED"
        out["context_class"] = out["context_class"].fillna("UNSPECIFIED")
    for column in ["sector_name", "sector_code"]:
        if column in out.columns:
            out[column] = out[column].astype("string").str.strip()
            out.loc[out[column].isin(["", "<NA>", "NAN", "NONE"]), column] = pd.NA
    if "sector_code" in out.columns:
        out["sector_code"] = out["sector_code"].astype("string").str.upper()
    out = _normalize_watch_state_columns(out)
    return out


def normalize_summary_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    for column in SUMMARY_NUMERIC_COLUMNS:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce")
    for column in SUMMARY_BOOL_COLUMNS:
        if column in out.columns:
            out[column] = _coerce_bool_series(out[column])
    for column in SUMMARY_TS_COLUMNS:
        if column in out.columns:
            out[column] = pd.to_datetime(out[column], utc=True, errors="coerce")
    if "context_class" in out.columns:
        out["context_class"] = out["context_class"].astype("string").str.strip().str.upper()
        out.loc[out["context_class"].isin(["", "<NA>", "NAN", "NONE"]), "context_class"] = "UNSPECIFIED"
        out["context_class"] = out["context_class"].fillna("UNSPECIFIED")
    if "sector_diagnostics_json" in out.columns:
        out["sector_diagnostics_json"] = out["sector_diagnostics_json"].fillna("[]").astype("string")
    out = _normalize_watch_state_columns(out)
    return out


def evaluate_context_watch(
    *,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    horizons: list[int] | None = None,
    return_threshold: float = DEFAULT_RETURN_THRESHOLD,
    cost_bps: float = DEFAULT_COST_BPS,
    min_matured_rows: int = DEFAULT_MIN_MATURED_ROWS,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    effective_horizons = sorted({int(value) for value in (horizons or DEFAULT_HORIZONS) if int(value) > 0})
    signals = load_context_watch_signals(from_date=from_date, to_date=to_date, symbols=symbols)
    if signals.empty or not effective_horizons:
        return pd.DataFrame(), pd.DataFrame(), {"signal_rows": int(len(signals)), "matured_rows_by_horizon": {}}
    price_start = signals["asof_date"].min() - pd.Timedelta(days=5)
    price_end = signals["asof_date"].max() + pd.Timedelta(days=max(effective_horizons) * 3 + 15)
    prices = load_price_history_for_returns(
        symbols=signals["symbol"].dropna().astype(str).str.upper().drop_duplicates().tolist(),
        from_date=price_start,
        to_date=price_end,
    )
    dataset = attach_forward_returns(signals, prices, horizons=effective_horizons)
    benchmark = load_benchmark_history_for_attribution(
        from_date=price_start,
        to_date=price_end,
    )
    dataset = attach_benchmark_forward_returns(dataset, benchmark, horizons=effective_horizons)
    evaluations = build_evaluation_rows(
        dataset,
        horizons=effective_horizons,
        cost_bps=cost_bps,
        return_threshold=return_threshold,
        evaluated_at=pd.Timestamp.utcnow(),
    )
    summary = summarize_evaluations(evaluations, min_matured_rows=min_matured_rows)
    benchmark_attribution_coverage = _benchmark_attribution_coverage(evaluations)
    meta = {
        "signal_rows": int(len(signals)),
        "price_rows": int(len(prices)),
        "benchmark_rows": int(len(benchmark)),
        "benchmark_attribution_coverage": benchmark_attribution_coverage,
        "evaluation_rows": int(len(evaluations)),
        "summary_rows": int(len(summary)),
        "horizons": effective_horizons,
        "return_threshold": float(return_threshold),
        "cost_bps": float(cost_bps),
        "min_matured_rows": int(min_matured_rows),
        "matured_rows_by_horizon": {
            str(horizon): int(evaluations[(evaluations["horizon_days"] == int(horizon)) & (evaluations["matured"] == True)].shape[0])
            for horizon in effective_horizons
        },
        "authority": "research_only",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        "point_in_time_return_contract": (
            json.loads(dataset["point_in_time_return_contract_json"].dropna().iloc[0])
            if "point_in_time_return_contract_json" in dataset.columns and dataset["point_in_time_return_contract_json"].notna().any()
            else {}
        ),
        "benchmark_return_contract": (
            json.loads(dataset["benchmark_return_contract_json"].dropna().iloc[0])
            if "benchmark_return_contract_json" in dataset.columns and dataset["benchmark_return_contract_json"].notna().any()
            else {}
        ),
    }
    return evaluations, summary, meta


def persist_outputs(evaluations: pd.DataFrame, summary: pd.DataFrame) -> None:
    ensure_tables()
    evaluations = normalize_evaluation_frame(evaluations)
    summary = normalize_summary_frame(summary)
    if not evaluations.empty:
        upsert_to_db(evaluations, EVALUATIONS_TABLE, unique_keys=["evaluated_at", "horizon_days", "refresh_id"])
    if not summary.empty:
        upsert_to_db(
            summary,
            SUMMARY_TABLE,
            unique_keys=[
                "evaluated_at",
                "horizon_days",
                "signal_source",
                "effect_type",
                "source_context",
                "context_class",
                "context_candidate_state",
                "context_policy_effect",
                "watch_breakout_blocker",
            ],
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate whether review-only positive context WATCH pressure later found opportunity after costs."
    )
    parser.add_argument("--from-date", type=parse_datetime_arg)
    parser.add_argument("--to-date", type=parse_datetime_arg)
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument("--horizons", type=int, nargs="*", default=DEFAULT_HORIZONS)
    parser.add_argument("--return-threshold", type=float, default=DEFAULT_RETURN_THRESHOLD)
    parser.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    parser.add_argument("--min-matured-rows", type=int, default=DEFAULT_MIN_MATURED_ROWS)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--format", choices=["json", "text"], default="json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    evaluations, summary, meta = evaluate_context_watch(
        from_date=args.from_date,
        to_date=args.to_date,
        symbols=args.symbols,
        horizons=args.horizons,
        return_threshold=float(args.return_threshold),
        cost_bps=float(args.cost_bps),
        min_matured_rows=int(args.min_matured_rows),
    )
    if not args.dry_run:
        persist_outputs(evaluations, summary)
    payload = {
        "status": "ok",
        "evaluations_table": EVALUATIONS_TABLE,
        "summary_table": SUMMARY_TABLE,
        "evaluation_rows": int(len(evaluations)),
        "summary_rows": int(len(summary)),
        "meta": _json_ready(meta),
        "summary_sample": _json_ready(summary.head(20).to_dict(orient="records") if not summary.empty else []),
        "dry_run": bool(args.dry_run),
    }
    if args.format == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    else:
        print(f"context_watch_evaluator rows={len(evaluations)} summary={len(summary)} dry_run={args.dry_run}")
        if not summary.empty:
            print(summary[["horizon_days", "signal_source", "source_context", "context_class", "context_candidate_state", "matured_count", "avg_watch_return_after_cost", "classification"]].head(20).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
