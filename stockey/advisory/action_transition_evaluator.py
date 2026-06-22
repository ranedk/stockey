from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from advisory.action_recommender import TABLE_NAME as ACTION_RECOMMENDATIONS_TABLE
from advisory.fallback_telemetry import record_local_fallback_event
from advisory.return_attribution import attach_benchmark_forward_returns, load_benchmark_history_for_attribution
from advisory.technical_threshold_calibration import attach_forward_returns, load_price_history_for_returns
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration
from utils.sync import parse_datetime_arg


EVALUATIONS_TABLE = "advisory_action_transition_evaluations"
SUMMARY_TABLE = "advisory_action_transition_eval_summary"
SCHEMA_MIGRATION_ID = "20260621_advisory_action_transition_evaluator_base"
BENCHMARK_SCHEMA_MIGRATION_ID = "20260622_advisory_action_transition_evaluator_benchmark_attribution"
DEFAULT_HORIZONS = [5, 10, 20]
DEFAULT_COST_BPS = 25.0
DEFAULT_RETURN_THRESHOLD = 0.03
DEFAULT_MIN_MATURED_ROWS = 10

SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {EVALUATIONS_TABLE} (
        evaluated_at TIMESTAMPTZ NOT NULL,
        horizon_days BIGINT NOT NULL,
        asof_date TIMESTAMPTZ,
        published_on TIMESTAMPTZ,
        symbol TEXT NOT NULL,
        setup_id TEXT,
        unique_id TEXT,
        action_code TEXT,
        action_source TEXT,
        previous_action TEXT,
        action_family TEXT,
        previous_action_family TEXT,
        action_changed BOOLEAN,
        transition_stability_status TEXT,
        stable_for_ranking BOOLEAN,
        precondition_status TEXT,
        position_status TEXT,
        technical_state TEXT,
        technical_confirmation_class TEXT,
        transition_blockers_json TEXT,
        transition_warnings_json TEXT,
        missing_preconditions_json TEXT,
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
        forward_return_after_cost DOUBLE PRECISION,
        excess_forward_return_after_cost DOUBLE PRECISION,
        directional_helpfulness_score DOUBLE PRECISION,
        excess_directional_helpfulness_score DOUBLE PRECISION,
        direction_hit_after_cost BOOLEAN,
        excess_direction_hit_after_cost BOOLEAN,
        matured BOOLEAN,
        evaluation_label TEXT,
        authority TEXT,
        broker_execution_allowed BOOLEAN,
        policy_auto_promotion_allowed BOOLEAN,
        raw_context_json TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (evaluated_at, horizon_days, asof_date, symbol, setup_id, unique_id, action_code)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {SUMMARY_TABLE} (
        evaluated_at TIMESTAMPTZ NOT NULL,
        horizon_days BIGINT NOT NULL,
        group_type TEXT NOT NULL,
        group_value TEXT NOT NULL,
        sample_count BIGINT,
        matured_count BIGINT,
        direction_hit_rate_after_cost DOUBLE PRECISION,
        excess_direction_hit_rate_after_cost DOUBLE PRECISION,
        avg_forward_return_after_cost DOUBLE PRECISION,
        avg_benchmark_forward_return DOUBLE PRECISION,
        avg_excess_forward_return_after_cost DOUBLE PRECISION,
        avg_directional_helpfulness_score DOUBLE PRECISION,
        avg_excess_directional_helpfulness_score DOUBLE PRECISION,
        sample_start TIMESTAMPTZ,
        sample_end TIMESTAMPTZ,
        classification TEXT,
        recommendation TEXT,
        authority TEXT,
        broker_execution_allowed BOOLEAN,
        policy_auto_promotion_allowed BOOLEAN,
        load_ts TIMESTAMPTZ,
        UNIQUE (evaluated_at, horizon_days, group_type, group_value)
    )
    """,
]
BENCHMARK_SCHEMA_STATEMENTS = [
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS benchmark_name TEXT",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS benchmark_entry_date TIMESTAMPTZ",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS benchmark_exit_date TIMESTAMPTZ",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS benchmark_entry_close DOUBLE PRECISION",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS benchmark_exit_close DOUBLE PRECISION",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS benchmark_forward_return DOUBLE PRECISION",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS excess_forward_return_after_cost DOUBLE PRECISION",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS excess_directional_helpfulness_score DOUBLE PRECISION",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS excess_direction_hit_after_cost BOOLEAN",
    f"ALTER TABLE {SUMMARY_TABLE} ADD COLUMN IF NOT EXISTS excess_direction_hit_rate_after_cost DOUBLE PRECISION",
    f"ALTER TABLE {SUMMARY_TABLE} ADD COLUMN IF NOT EXISTS avg_benchmark_forward_return DOUBLE PRECISION",
    f"ALTER TABLE {SUMMARY_TABLE} ADD COLUMN IF NOT EXISTS avg_excess_forward_return_after_cost DOUBLE PRECISION",
    f"ALTER TABLE {SUMMARY_TABLE} ADD COLUMN IF NOT EXISTS avg_excess_directional_helpfulness_score DOUBLE PRECISION",
]

NUMERIC_COLUMNS = [
    "entry_close",
    "exit_close",
    "forward_return",
    "benchmark_entry_close",
    "benchmark_exit_close",
    "benchmark_forward_return",
    "forward_return_after_cost",
    "excess_forward_return_after_cost",
    "directional_helpfulness_score",
    "excess_directional_helpfulness_score",
]
INT_COLUMNS = ["horizon_days"]
BOOL_COLUMNS = [
    "action_changed",
    "stable_for_ranking",
    "direction_hit_after_cost",
    "excess_direction_hit_after_cost",
    "matured",
    "broker_execution_allowed",
    "policy_auto_promotion_allowed",
]
TS_COLUMNS = [
    "evaluated_at",
    "asof_date",
    "published_on",
    "entry_date",
    "exit_date",
    "benchmark_entry_date",
    "benchmark_exit_date",
    "load_ts",
]


def ensure_tables() -> None:
    apply_schema_migration(
        migration_id=SCHEMA_MIGRATION_ID,
        description="Create research-only action transition stability evaluator output tables.",
        statements=SCHEMA_STATEMENTS,
        metadata={"tables": [EVALUATIONS_TABLE, SUMMARY_TABLE], "authority": "research_only"},
    )
    apply_schema_migration(
        migration_id=BENCHMARK_SCHEMA_MIGRATION_ID,
        description="Add benchmark-excess attribution to action transition evaluator outputs.",
        statements=BENCHMARK_SCHEMA_STATEMENTS,
        metadata={
            "tables": [EVALUATIONS_TABLE, SUMMARY_TABLE],
            "authority_scope": "research_only_benchmark_attribution",
            "additive": True,
        },
    )


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=False, sort_keys=True, default=str)


def _parse_jsonish(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return default
    if pd.api.types.is_scalar(value) and pd.isna(value):
        return default
    try:
        parsed = json.loads(str(value))
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.action_transition_evaluator",
            fallback_type="action_transition_evaluator_json_parse_failed",
            source=ACTION_RECOMMENDATIONS_TABLE,
            severity="warn",
            reason="Action transition evaluator could not parse recommendation/action context JSON.",
            error=exc,
            metadata={"payload_length": len(str(value))},
        )
        return default
    return parsed


def _number(value: Any) -> float | None:
    numeric = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(numeric) else float(numeric)


def _boolish(value: Any) -> bool | None:
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


def _text(value: Any, default: str | None = None) -> str | None:
    if value is None:
        return default
    if pd.api.types.is_scalar(value) and pd.isna(value):
        return default
    text = str(value).strip()
    return text if text else default


def _json_clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_clean(item) for item in value]
    if isinstance(value, tuple):
        return [_json_clean(item) for item in value]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if value is pd.NaT:
        return None
    if pd.api.types.is_scalar(value) and pd.isna(value):
        return None
    return value


def normalize_evaluation_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    for column in NUMERIC_COLUMNS:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce")
    for column in INT_COLUMNS:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce").astype("Int64")
    for column in BOOL_COLUMNS:
        if column in out.columns:
            out[column] = out[column].map(_boolish).astype("boolean")
    for column in TS_COLUMNS:
        if column in out.columns:
            out[column] = pd.to_datetime(out[column], utc=True, errors="coerce")
    return out


def normalize_summary_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    for column in [
        "direction_hit_rate_after_cost",
        "excess_direction_hit_rate_after_cost",
        "avg_forward_return_after_cost",
        "avg_benchmark_forward_return",
        "avg_excess_forward_return_after_cost",
        "avg_directional_helpfulness_score",
        "avg_excess_directional_helpfulness_score",
    ]:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce")
    for column in ["horizon_days", "sample_count", "matured_count"]:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce").astype("Int64")
    for column in ["evaluated_at", "sample_start", "sample_end", "load_ts"]:
        if column in out.columns:
            out[column] = pd.to_datetime(out[column], utc=True, errors="coerce")
    for column in ["broker_execution_allowed", "policy_auto_promotion_allowed"]:
        if column in out.columns:
            out[column] = out[column].map(_boolish).astype("boolean")
    return out


def table_columns(table_name: str) -> set[str]:
    try:
        df = sql_to_df(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = %s
            """,
            params=(table_name,),
            retries=2,
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.action_transition_evaluator",
            fallback_type="action_transition_evaluator_schema_lookup_failed",
            source=table_name,
            severity="warn",
            reason="Action transition evaluator treated source table as unavailable because schema lookup failed.",
            error=exc,
        )
        return set()
    return set(df["column_name"].astype(str).tolist()) if not df.empty else set()


def _extract_transition_contract(reason_json: Any) -> dict[str, Any]:
    contract = _parse_jsonish(reason_json, {})
    if not isinstance(contract, dict):
        return {}
    evidence = contract.get("evidence")
    if not isinstance(evidence, dict):
        return {}
    transition = evidence.get("action_transition")
    return transition if isinstance(transition, dict) else {}


def _transition_stability(transition: dict[str, Any]) -> dict[str, Any]:
    stability = transition.get("transition_stability")
    if isinstance(stability, dict):
        return stability
    return {}


def load_action_transition_rows(
    *,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
) -> pd.DataFrame:
    available = table_columns(ACTION_RECOMMENDATIONS_TABLE)
    required = {"asof_date", "symbol", "action_code", "recommendation_reason_json"}
    if not required.issubset(available):
        missing = sorted(required - available)
        record_local_fallback_event(
            module="advisory.action_transition_evaluator",
            fallback_type="action_transition_evaluator_required_columns_missing",
            source=ACTION_RECOMMENDATIONS_TABLE,
            severity="warn",
            reason="Action transition evaluator returned empty output because required action columns were missing.",
            metadata={"missing_columns": missing},
        )
        return pd.DataFrame()
    wanted = [
        "asof_date",
        "published_on",
        "symbol",
        "setup_id",
        "unique_id",
        "action_code",
        "action_source",
        "previous_action",
        "action_changed",
        "recommendation_reason_json",
        "raw_context_json",
        "load_ts",
    ]
    selected = [column for column in wanted if column in available]
    clauses = ["NULLIF(TRIM(symbol), '') IS NOT NULL", "recommendation_reason_json IS NOT NULL"]
    params: list[Any] = []
    if from_date is not None:
        clauses.append("asof_date >= %s")
        params.append(from_date)
    if to_date is not None:
        clauses.append("asof_date <= %s")
        params.append(to_date)
    if symbols:
        clauses.append("UPPER(TRIM(symbol)) = ANY(%s)")
        params.append([str(value).upper() for value in symbols])
    try:
        df = sql_to_df(
            f"""
            SELECT {', '.join(selected)}
            FROM {ACTION_RECOMMENDATIONS_TABLE}
            WHERE {' AND '.join(clauses)}
            ORDER BY asof_date, symbol, action_code, setup_id, unique_id
            """,
            params=tuple(params) if params else None,
            retries=4,
            statement_timeout_ms=0,
            chunksize=50000,
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.action_transition_evaluator",
            fallback_type="action_transition_evaluator_action_rows_load_failed",
            source=ACTION_RECOMMENDATIONS_TABLE,
            severity="warn",
            reason="Action transition evaluator returned empty output because action row loading failed.",
            error=exc,
            metadata={
                "from_date": str(from_date) if from_date is not None else None,
                "to_date": str(to_date) if to_date is not None else None,
                "symbol_count": len(symbols or []),
            },
        )
        return pd.DataFrame()
    if df.empty:
        return df
    out = df.copy()
    out["asof_date"] = pd.to_datetime(out["asof_date"], utc=True, errors="coerce").dt.normalize()
    out["published_on"] = pd.to_datetime(out.get("published_on"), utc=True, errors="coerce") if "published_on" in out.columns else out["asof_date"]
    out["symbol"] = out["symbol"].astype("string").str.strip().str.upper()
    for column in ["setup_id", "unique_id", "action_code", "action_source", "previous_action"]:
        if column not in out.columns:
            out[column] = None
        out[column] = out[column].astype("string").str.strip()
    parsed_rows: list[dict[str, Any]] = []
    for _, row in out.iterrows():
        transition = _extract_transition_contract(row.get("recommendation_reason_json"))
        stability = _transition_stability(transition)
        if not transition:
            continue
        parsed_rows.append(
            {
                **row.to_dict(),
                "previous_action": _text(stability.get("previous_action") or row.get("previous_action")),
                "action_family": _text(stability.get("current_action_family")),
                "previous_action_family": _text(stability.get("previous_action_family")),
                "action_changed": _boolish(stability.get("action_changed") if "action_changed" in stability else row.get("action_changed")),
                "transition_stability_status": _text(stability.get("stability_status") or transition.get("transition_stability_status"), "unknown"),
                "stable_for_ranking": _boolish(stability.get("stable_for_ranking") if "stable_for_ranking" in stability else transition.get("transition_stable_for_ranking")),
                "transition_blockers_json": _json_dumps(stability.get("transition_blockers") or []),
                "transition_warnings_json": _json_dumps(stability.get("transition_warnings") or []),
                "precondition_status": _text(transition.get("precondition_status"), "unknown"),
                "missing_preconditions_json": _json_dumps(transition.get("missing_preconditions") or []),
                "position_status": _text(stability.get("position_status") or transition.get("position_status")),
                "technical_state": _text((stability.get("technical_confirmation") or {}).get("technical_state") if isinstance(stability.get("technical_confirmation"), dict) else None),
                "technical_confirmation_class": _text((stability.get("technical_confirmation") or {}).get("technical_confirmation_class") if isinstance(stability.get("technical_confirmation"), dict) else None, "unknown"),
            }
        )
    parsed = pd.DataFrame(parsed_rows)
    if parsed.empty:
        return parsed
    parsed["action_code"] = parsed["action_code"].astype("string").str.upper()
    parsed["action_source"] = parsed["action_source"].astype("string").str.lower()
    return parsed.dropna(subset=["asof_date", "symbol", "action_code"])


def _directional_outcome(
    action_family: str,
    after_cost: float | None,
    excess_after_cost: float | None,
    threshold: float,
) -> tuple[float | None, bool | None, float | None, bool | None, str]:
    family = str(action_family or "").lower()
    if after_cost is None:
        return None, None, None, None, "not_matured"
    if family == "entry":
        excess_score = None if excess_after_cost is None else float(excess_after_cost)
        return (
            float(after_cost),
            bool(after_cost >= float(threshold)),
            excess_score,
            None if excess_score is None else bool(excess_score >= float(threshold)),
            "entry_helpful_if_future_return_positive",
        )
    if family in {"exit", "de_risk"}:
        score = -float(after_cost)
        excess_score = None if excess_after_cost is None else -float(excess_after_cost)
        return (
            score,
            bool(after_cost <= -float(threshold)),
            excess_score,
            None if excess_score is None else bool(excess_score >= float(threshold)),
            "derisk_helpful_if_future_return_negative",
        )
    if family in {"watch", "hold", "manual_review"}:
        excess_score = None if excess_after_cost is None else float(abs(excess_after_cost))
        return float(abs(after_cost)), None, excess_score, None, "non_executable_observation_only"
    return None, None, None, None, "unknown_action_family"


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
            h = int(horizon)
            forward_return = _number(row.get(f"forward_return_h{h}"))
            benchmark_forward_return = _number(row.get(f"benchmark_forward_return_h{h}"))
            matured = forward_return is not None
            after_cost = None if forward_return is None else float(forward_return) - cost
            excess_after_cost = None if forward_return is None or benchmark_forward_return is None else float(forward_return) - float(benchmark_forward_return) - cost
            helpfulness, hit, excess_helpfulness, excess_hit, label = _directional_outcome(
                str(row.get("action_family") or ""),
                after_cost,
                excess_after_cost,
                float(return_threshold),
            )
            benchmark_entry_date = pd.to_datetime(row.get(f"benchmark_entry_date_h{h}"), utc=True, errors="coerce")
            benchmark_exit_date = pd.to_datetime(row.get(f"benchmark_exit_date_h{h}"), utc=True, errors="coerce")
            rows.append(
                {
                    "evaluated_at": effective_evaluated_at,
                    "horizon_days": h,
                    "asof_date": row.get("asof_date"),
                    "published_on": row.get("published_on"),
                    "symbol": row.get("symbol"),
                    "setup_id": row.get("setup_id"),
                    "unique_id": row.get("unique_id"),
                    "action_code": row.get("action_code"),
                    "action_source": row.get("action_source"),
                    "previous_action": row.get("previous_action"),
                    "action_family": row.get("action_family"),
                    "previous_action_family": row.get("previous_action_family"),
                    "action_changed": row.get("action_changed"),
                    "transition_stability_status": row.get("transition_stability_status"),
                    "stable_for_ranking": row.get("stable_for_ranking"),
                    "precondition_status": row.get("precondition_status"),
                    "position_status": row.get("position_status"),
                    "technical_state": row.get("technical_state"),
                    "technical_confirmation_class": row.get("technical_confirmation_class"),
                    "transition_blockers_json": row.get("transition_blockers_json"),
                    "transition_warnings_json": row.get("transition_warnings_json"),
                    "missing_preconditions_json": row.get("missing_preconditions_json"),
                    "entry_date": row.get(f"entry_date_h{h}"),
                    "exit_date": row.get(f"exit_date_h{h}"),
                    "entry_close": row.get(f"entry_close_h{h}"),
                    "exit_close": row.get(f"exit_close_h{h}"),
                    "forward_return": forward_return,
                    "benchmark_name": row.get("benchmark_name"),
                    "benchmark_entry_date": None if pd.isna(benchmark_entry_date) else benchmark_entry_date,
                    "benchmark_exit_date": None if pd.isna(benchmark_exit_date) else benchmark_exit_date,
                    "benchmark_entry_close": row.get(f"benchmark_entry_close_h{h}"),
                    "benchmark_exit_close": row.get(f"benchmark_exit_close_h{h}"),
                    "benchmark_forward_return": benchmark_forward_return,
                    "forward_return_after_cost": after_cost,
                    "excess_forward_return_after_cost": excess_after_cost,
                    "directional_helpfulness_score": helpfulness,
                    "excess_directional_helpfulness_score": excess_helpfulness,
                    "direction_hit_after_cost": hit,
                    "excess_direction_hit_after_cost": excess_hit,
                    "matured": matured,
                    "evaluation_label": label,
                    "authority": "research_only",
                    "broker_execution_allowed": False,
                    "policy_auto_promotion_allowed": False,
                    "raw_context_json": row.get("raw_context_json"),
                    "load_ts": pd.Timestamp.utcnow(),
                }
            )
    return normalize_evaluation_frame(pd.DataFrame(rows))


def _classification(
    matured_count: int,
    avg_helpfulness: float | None,
    hit_rate: float | None,
    avg_excess_helpfulness: float | None,
    excess_hit_rate: float | None,
    min_rows: int,
) -> tuple[str, str]:
    if matured_count < int(min_rows):
        return "needs_more_data", "collect_more_transition_outcomes"
    if avg_excess_helpfulness is None or excess_hit_rate is None:
        return "needs_benchmark_attribution", "collect_benchmark_excess_transition_outcomes"
    if (
        avg_helpfulness is not None
        and avg_helpfulness > 0
        and hit_rate is not None
        and hit_rate >= 0.55
        and avg_excess_helpfulness > 0
        and excess_hit_rate >= 0.55
    ):
        return "candidate_stable_policy_signal", "review_for_future_downgrade_or_ranking_policy"
    if (
        avg_helpfulness is not None
        and avg_helpfulness > 0
        and hit_rate is not None
        and hit_rate >= 0.55
        and (avg_excess_helpfulness <= 0 or (excess_hit_rate is not None and excess_hit_rate < 0.55))
    ):
        return "benchmark_beta_not_transition_alpha", "keep_research_only_collect_excess_transition_evidence"
    if avg_helpfulness is not None and (avg_helpfulness <= 0 or avg_excess_helpfulness <= 0):
        return "not_helpful", "do_not_use_for_policy"
    return "monitor", "keep_research_only"


def summarize_evaluations(
    evaluations: pd.DataFrame,
    *,
    evaluated_at: pd.Timestamp | None = None,
    min_matured_rows: int = DEFAULT_MIN_MATURED_ROWS,
) -> pd.DataFrame:
    if evaluations.empty:
        return pd.DataFrame()
    evals = normalize_evaluation_frame(evaluations)
    effective_evaluated_at = pd.to_datetime(evaluated_at or evals["evaluated_at"].dropna().max() or pd.Timestamp.utcnow(), utc=True, errors="coerce")
    group_specs = [
        ("transition_stability_status", ["transition_stability_status"]),
        ("stable_for_ranking", ["stable_for_ranking"]),
        ("action_family", ["action_family"]),
        ("action_code", ["action_code"]),
        ("stability_action_family", ["transition_stability_status", "action_family"]),
        ("precondition_status", ["precondition_status"]),
        ("technical_confirmation_class", ["technical_confirmation_class"]),
    ]
    rows: list[dict[str, Any]] = []
    for horizon, horizon_group in evals.groupby("horizon_days", dropna=False):
        for group_type, columns in group_specs:
            available = [column for column in columns if column in horizon_group.columns]
            if not available:
                continue
            for keys, group in horizon_group.groupby(available, dropna=False):
                key_values = keys if isinstance(keys, tuple) else (keys,)
                group_value = "|".join("unknown" if pd.isna(value) else str(value) for value in key_values)
                matured = group[group["matured"].fillna(False).astype(bool)]
                returns = pd.to_numeric(matured.get("forward_return_after_cost"), errors="coerce").dropna()
                benchmark_returns = pd.to_numeric(matured.get("benchmark_forward_return"), errors="coerce").dropna()
                excess_returns = pd.to_numeric(matured.get("excess_forward_return_after_cost"), errors="coerce").dropna()
                helpfulness = pd.to_numeric(matured.get("directional_helpfulness_score"), errors="coerce").dropna()
                excess_helpfulness = pd.to_numeric(matured.get("excess_directional_helpfulness_score"), errors="coerce").dropna()
                hits = matured["direction_hit_after_cost"].dropna().astype(bool) if "direction_hit_after_cost" in matured.columns else pd.Series(dtype=bool)
                excess_hits = (
                    matured["excess_direction_hit_after_cost"].dropna().astype(bool)
                    if "excess_direction_hit_after_cost" in matured.columns
                    else pd.Series(dtype=bool)
                )
                avg_helpfulness = float(helpfulness.mean()) if not helpfulness.empty else None
                avg_excess_helpfulness = float(excess_helpfulness.mean()) if not excess_helpfulness.empty else None
                hit_rate = float(hits.mean()) if not hits.empty else None
                excess_hit_rate = float(excess_hits.mean()) if not excess_hits.empty else None
                classification, recommendation = _classification(
                    int(len(matured)),
                    avg_helpfulness,
                    hit_rate,
                    avg_excess_helpfulness,
                    excess_hit_rate,
                    int(min_matured_rows),
                )
                rows.append(
                    {
                        "evaluated_at": effective_evaluated_at,
                        "horizon_days": int(horizon),
                        "group_type": group_type,
                        "group_value": group_value,
                        "sample_count": int(len(group)),
                        "matured_count": int(len(matured)),
                        "direction_hit_rate_after_cost": hit_rate,
                        "excess_direction_hit_rate_after_cost": excess_hit_rate,
                        "avg_forward_return_after_cost": float(returns.mean()) if not returns.empty else None,
                        "avg_benchmark_forward_return": float(benchmark_returns.mean()) if not benchmark_returns.empty else None,
                        "avg_excess_forward_return_after_cost": float(excess_returns.mean()) if not excess_returns.empty else None,
                        "avg_directional_helpfulness_score": avg_helpfulness,
                        "avg_excess_directional_helpfulness_score": avg_excess_helpfulness,
                        "sample_start": matured["asof_date"].min() if not matured.empty else pd.NaT,
                        "sample_end": matured["asof_date"].max() if not matured.empty else pd.NaT,
                        "classification": classification,
                        "recommendation": recommendation,
                        "authority": "research_only",
                        "broker_execution_allowed": False,
                        "policy_auto_promotion_allowed": False,
                        "load_ts": pd.Timestamp.utcnow(),
                    }
                )
    return normalize_summary_frame(pd.DataFrame(rows))


def build_readiness_contract(
    evaluations: pd.DataFrame,
    summary: pd.DataFrame,
    *,
    min_matured_rows: int = DEFAULT_MIN_MATURED_ROWS,
) -> dict[str, Any]:
    if evaluations.empty:
        return {
            "status": "no_transition_evidence",
            "ready_for_policy_review": False,
            "operator_action": "Collect action-transition rows before considering any stability-based policy consumer.",
            "matured_rows_total": 0,
            "matured_rows_by_horizon": {},
            "candidate_group_count": 0,
            "classification_counts": {},
            "benchmark_beta_not_transition_alpha_count": 0,
            "needs_benchmark_attribution_count": 0,
            "required_min_matured_rows": int(min_matured_rows),
            "authority": "research_only",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
        }
    evals = normalize_evaluation_frame(evaluations)
    matured = evals[evals["matured"].fillna(False).astype(bool)] if "matured" in evals.columns else pd.DataFrame()
    matured_by_horizon = {
        str(int(horizon)): int(group.shape[0])
        for horizon, group in matured.groupby("horizon_days", dropna=False)
        if pd.notna(horizon)
    }
    candidate_groups = pd.DataFrame()
    classification_counts: dict[str, int] = {}
    if not summary.empty and "classification" in summary.columns:
        classifications = summary["classification"].astype("string").str.strip()
        classification_counts = {
            str(key): int(value)
            for key, value in classifications.value_counts(dropna=True).to_dict().items()
            if str(key) and str(key) != "<NA>"
        }
        candidate_groups = summary[
            classifications.eq("candidate_stable_policy_signal")
        ]
    beta_only_count = int(classification_counts.get("benchmark_beta_not_transition_alpha", 0))
    needs_benchmark_count = int(classification_counts.get("needs_benchmark_attribution", 0))
    enough_any_horizon = any(count >= int(min_matured_rows) for count in matured_by_horizon.values())
    has_candidate = not candidate_groups.empty
    if not matured_by_horizon:
        status = "no_matured_labels"
        operator_action = "Wait for forward-return windows to mature before using transition stability evidence."
    elif not enough_any_horizon:
        status = "collect_more_matured_labels"
        operator_action = "Collect more matured transition labels; current evidence is below the minimum row gate."
    elif not has_candidate:
        if beta_only_count:
            status = "matured_but_benchmark_beta_only"
            operator_action = (
                "Keep transition stability research-only; raw helpful transition groups did not beat benchmark movement."
            )
        elif needs_benchmark_count:
            status = "matured_but_missing_benchmark_attribution"
            operator_action = (
                "Collect benchmark-excess transition attribution before considering any transition-stability policy."
            )
        else:
            status = "matured_but_no_candidate_policy_signal"
            operator_action = "Keep transition stability research-only; matured evidence does not identify a stable helpful group yet."
    else:
        status = "candidate_for_manual_policy_design"
        operator_action = "Review candidate groups offline; any future runtime consumer must remain deterministic, bounded, and separately tested."
    return {
        "status": status,
        "ready_for_policy_review": bool(enough_any_horizon and has_candidate),
        "operator_action": operator_action,
        "matured_rows_total": int(len(matured)),
        "matured_rows_by_horizon": matured_by_horizon,
        "candidate_group_count": int(len(candidate_groups)),
        "candidate_groups": candidate_groups.head(10).to_dict(orient="records") if not candidate_groups.empty else [],
        "classification_counts": classification_counts,
        "benchmark_beta_not_transition_alpha_count": beta_only_count,
        "needs_benchmark_attribution_count": needs_benchmark_count,
        "required_min_matured_rows": int(min_matured_rows),
        "authority": "research_only",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
    }


def evaluate_action_transitions(
    *,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    horizons: list[int] | None = None,
    cost_bps: float = DEFAULT_COST_BPS,
    return_threshold: float = DEFAULT_RETURN_THRESHOLD,
    min_matured_rows: int = DEFAULT_MIN_MATURED_ROWS,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    effective_horizons = sorted({int(value) for value in (horizons or DEFAULT_HORIZONS) if int(value) > 0})
    signals = load_action_transition_rows(from_date=from_date, to_date=to_date, symbols=symbols)
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
    benchmark = load_benchmark_history_for_attribution(from_date=price_start, to_date=price_end)
    dataset = attach_benchmark_forward_returns(dataset, benchmark, horizons=effective_horizons)
    evaluated_at = pd.Timestamp.utcnow()
    evaluations = build_evaluation_rows(
        dataset,
        horizons=effective_horizons,
        cost_bps=cost_bps,
        return_threshold=return_threshold,
        evaluated_at=evaluated_at,
    )
    summary = summarize_evaluations(evaluations, evaluated_at=evaluated_at, min_matured_rows=min_matured_rows)
    return_contract = (
        json.loads(dataset["point_in_time_return_contract_json"].dropna().iloc[0])
        if "point_in_time_return_contract_json" in dataset.columns and dataset["point_in_time_return_contract_json"].notna().any()
        else {}
    )
    meta = {
        "signal_rows": int(len(signals)),
        "price_rows": int(len(prices)),
        "benchmark_rows": int(len(benchmark)),
        "evaluation_rows": int(len(evaluations)),
        "summary_rows": int(len(summary)),
        "matured_rows_by_horizon": {
            str(horizon): int(evaluations[(evaluations["horizon_days"] == int(horizon)) & (evaluations["matured"] == True)].shape[0])
            for horizon in effective_horizons
        },
        "horizons": effective_horizons,
        "cost_bps": float(cost_bps),
        "return_threshold": float(return_threshold),
        "min_matured_rows": int(min_matured_rows),
        "point_in_time_return_contract": return_contract,
        "benchmark_return_contract": (
            json.loads(dataset["benchmark_return_contract_json"].dropna().iloc[0])
            if "benchmark_return_contract_json" in dataset.columns and dataset["benchmark_return_contract_json"].notna().any()
            else {}
        ),
        "authority": "research_only",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
    }
    meta["readiness_contract"] = build_readiness_contract(
        evaluations,
        summary,
        min_matured_rows=int(min_matured_rows),
    )
    return evaluations, summary, meta


def persist_outputs(evaluations: pd.DataFrame, summary: pd.DataFrame) -> None:
    ensure_tables()
    evaluations = normalize_evaluation_frame(evaluations)
    summary = normalize_summary_frame(summary)
    if not evaluations.empty:
        upsert_to_db(
            evaluations,
            EVALUATIONS_TABLE,
            unique_keys=["evaluated_at", "horizon_days", "asof_date", "symbol", "setup_id", "unique_id", "action_code"],
        )
    if not summary.empty:
        upsert_to_db(
            summary,
            SUMMARY_TABLE,
            unique_keys=["evaluated_at", "horizon_days", "group_type", "group_value"],
        )


def summarize_payload(evaluations: pd.DataFrame, summary: pd.DataFrame, meta: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    readiness = meta.get("readiness_contract") or build_readiness_contract(
        evaluations,
        summary,
        min_matured_rows=int(meta.get("min_matured_rows") or DEFAULT_MIN_MATURED_ROWS),
    )
    return _json_clean(
        {
        "status": "ok",
        "readiness_status": readiness.get("status"),
        "ready_for_policy_review": bool(readiness.get("ready_for_policy_review")),
        "evaluations_table": EVALUATIONS_TABLE,
        "summary_table": SUMMARY_TABLE,
        "evaluation_rows": int(len(evaluations)),
        "summary_rows": int(len(summary)),
        "evaluated_rows": int(evaluations["matured"].fillna(False).astype(bool).sum()) if not evaluations.empty else 0,
        "not_matured_rows": int((~evaluations["matured"].fillna(False).astype(bool)).sum()) if not evaluations.empty else 0,
        "summary_sample": summary.head(10).to_dict(orient="records") if not summary.empty else [],
        "readiness_contract": readiness,
        "meta": meta,
        "dry_run": bool(dry_run),
        "authority": "research_only",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        }
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate action-transition stability against realized Dhan OHLCV forward returns.")
    parser.add_argument("--from-date", type=parse_datetime_arg)
    parser.add_argument("--to-date", type=parse_datetime_arg)
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument("--horizons", nargs="*", type=int, default=DEFAULT_HORIZONS)
    parser.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    parser.add_argument("--return-threshold", type=float, default=DEFAULT_RETURN_THRESHOLD)
    parser.add_argument("--min-matured-rows", type=int, default=DEFAULT_MIN_MATURED_ROWS)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--ensure-schema-only",
        action="store_true",
        help="Apply evaluator schema migrations and exit without loading data or writing evaluation rows.",
    )
    parser.add_argument("--format", choices=["json", "text"], default="json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.ensure_schema_only:
        ensure_tables()
        print(
            json.dumps(
                {
                    "status": "ok",
                    "mode": "ensure_schema_only",
                    "evaluations_table": EVALUATIONS_TABLE,
                    "summary_table": SUMMARY_TABLE,
                    "broker_execution_allowed": False,
                    "policy_auto_promotion_allowed": False,
                    "authority": "research_only_schema_maintenance",
                },
                indent=2,
                ensure_ascii=False,
                default=str,
            )
        )
        return 0
    evaluations, summary, meta = evaluate_action_transitions(
        from_date=pd.Timestamp(args.from_date, tz="UTC") if args.from_date else None,
        to_date=pd.Timestamp(args.to_date, tz="UTC") if args.to_date else None,
        symbols=args.symbols,
        horizons=args.horizons,
        cost_bps=float(args.cost_bps),
        return_threshold=float(args.return_threshold),
        min_matured_rows=int(args.min_matured_rows),
    )
    if not args.dry_run:
        persist_outputs(evaluations, summary)
    payload = summarize_payload(evaluations, summary, meta, dry_run=bool(args.dry_run))
    if args.format == "json":
        print(json.dumps(payload, allow_nan=False, default=str, sort_keys=True))
    else:
        print(
            f"status={payload['status']} evaluations={payload['evaluation_rows']} "
            f"summary={payload['summary_rows']} matured={payload['evaluated_rows']} "
            f"readiness={payload['readiness_status']} dry_run={payload['dry_run']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
