from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from advisory.causal_event_memory import TABLE_NAME as CAUSAL_EVENT_MEMORY_TABLE
from advisory.event_evidence_store import table_exists
from advisory.fallback_telemetry import record_local_fallback_event
from advisory.return_attribution import attach_benchmark_forward_returns, load_benchmark_history_for_attribution
from advisory.technical_threshold_calibration import attach_forward_returns, load_price_history_for_returns
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration
from utils.sync import parse_datetime_arg


EVALUATIONS_TABLE = "advisory_causal_event_memory_evaluations"
SUMMARY_TABLE = "advisory_causal_event_memory_eval_summary"
SCHEMA_MIGRATION_ID = "20260621_advisory_causal_event_memory_evaluator_base"
DEFAULT_HORIZONS = [5, 10, 20]
DEFAULT_COST_BPS = 25.0
DEFAULT_RETURN_THRESHOLD = 0.0
DEFAULT_MIN_MATURED_ROWS = 10

SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {EVALUATIONS_TABLE} (
        evaluated_at TIMESTAMPTZ NOT NULL,
        horizon_days BIGINT NOT NULL,
        memory_id TEXT NOT NULL,
        asof_date TIMESTAMPTZ NOT NULL,
        symbol TEXT NOT NULL,
        sector_code TEXT,
        sector_name TEXT,
        context_source TEXT,
        event_group TEXT,
        event_type TEXT,
        context_class TEXT,
        direction TEXT,
        event_state TEXT,
        pressure_score DOUBLE PRECISION,
        decayed_pressure_score DOUBLE PRECISION,
        event_count BIGINT,
        contradiction_state TEXT,
        entry_date TIMESTAMPTZ,
        exit_date TIMESTAMPTZ,
        entry_close DOUBLE PRECISION,
        exit_close DOUBLE PRECISION,
        forward_return DOUBLE PRECISION,
        forward_return_after_cost DOUBLE PRECISION,
        benchmark_name TEXT,
        benchmark_entry_date TIMESTAMPTZ,
        benchmark_exit_date TIMESTAMPTZ,
        benchmark_entry_close DOUBLE PRECISION,
        benchmark_exit_close DOUBLE PRECISION,
        benchmark_forward_return DOUBLE PRECISION,
        excess_return_after_cost DOUBLE PRECISION,
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
        load_ts TIMESTAMPTZ NOT NULL,
        UNIQUE (evaluated_at, horizon_days, memory_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {SUMMARY_TABLE} (
        evaluated_at TIMESTAMPTZ NOT NULL,
        horizon_days BIGINT NOT NULL,
        context_source TEXT NOT NULL,
        context_class TEXT NOT NULL,
        event_type TEXT NOT NULL,
        direction TEXT NOT NULL,
        event_state TEXT NOT NULL,
        sample_count BIGINT,
        matured_count BIGINT,
        symbol_count BIGINT,
        avg_forward_return DOUBLE PRECISION,
        avg_forward_return_after_cost DOUBLE PRECISION,
        avg_benchmark_forward_return DOUBLE PRECISION,
        avg_excess_return_after_cost DOUBLE PRECISION,
        avg_directional_helpfulness_score DOUBLE PRECISION,
        avg_excess_directional_helpfulness_score DOUBLE PRECISION,
        direction_hit_rate_after_cost DOUBLE PRECISION,
        excess_direction_hit_rate_after_cost DOUBLE PRECISION,
        avg_pressure_score DOUBLE PRECISION,
        avg_decayed_pressure_score DOUBLE PRECISION,
        sample_start TIMESTAMPTZ,
        sample_end TIMESTAMPTZ,
        classification TEXT,
        recommendation TEXT,
        authority TEXT,
        broker_execution_allowed BOOLEAN,
        policy_auto_promotion_allowed BOOLEAN,
        load_ts TIMESTAMPTZ NOT NULL,
        UNIQUE (evaluated_at, horizon_days, context_source, context_class, event_type, direction, event_state)
    )
    """,
]

EVALUATION_NUMERIC_COLUMNS = [
    "horizon_days",
    "pressure_score",
    "decayed_pressure_score",
    "event_count",
    "entry_close",
    "exit_close",
    "forward_return",
    "forward_return_after_cost",
    "benchmark_entry_close",
    "benchmark_exit_close",
    "benchmark_forward_return",
    "excess_return_after_cost",
    "directional_helpfulness_score",
    "excess_directional_helpfulness_score",
]
EVALUATION_BOOL_COLUMNS = [
    "direction_hit_after_cost",
    "excess_direction_hit_after_cost",
    "matured",
    "broker_execution_allowed",
    "policy_auto_promotion_allowed",
]
EVALUATION_TS_COLUMNS = ["evaluated_at", "asof_date", "entry_date", "exit_date", "benchmark_entry_date", "benchmark_exit_date", "load_ts"]
SUMMARY_NUMERIC_COLUMNS = [
    "horizon_days",
    "sample_count",
    "matured_count",
    "symbol_count",
    "avg_forward_return",
    "avg_forward_return_after_cost",
    "avg_benchmark_forward_return",
    "avg_excess_return_after_cost",
    "avg_directional_helpfulness_score",
    "avg_excess_directional_helpfulness_score",
    "direction_hit_rate_after_cost",
    "excess_direction_hit_rate_after_cost",
    "avg_pressure_score",
    "avg_decayed_pressure_score",
]
SUMMARY_BOOL_COLUMNS = ["broker_execution_allowed", "policy_auto_promotion_allowed"]
SUMMARY_TS_COLUMNS = ["evaluated_at", "sample_start", "sample_end", "load_ts"]


def _record_fallback(
    *,
    fallback_type: str,
    source: str,
    reason: str,
    error: Exception,
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="advisory.causal_event_memory_evaluator",
        fallback_type=fallback_type,
        source=source,
        severity="warn",
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, default=str)


def _json_ready(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if pd.api.types.is_scalar(value) and pd.isna(value):
        return None
    return value


def _number(value: Any) -> float | None:
    numeric = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric):
        return None
    return float(numeric)


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


def ensure_tables() -> None:
    apply_schema_migration(
        migration_id=SCHEMA_MIGRATION_ID,
        description="Create causal event memory realized-outcome evaluator tables.",
        statements=SCHEMA_STATEMENTS,
        metadata={"tables": [EVALUATIONS_TABLE, SUMMARY_TABLE], "authority": "research_only"},
        owner="advisory.causal_event_memory_evaluator",
    )


def load_memory_rows(
    *,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
) -> pd.DataFrame:
    if not table_exists(CAUSAL_EVENT_MEMORY_TABLE):
        return pd.DataFrame()
    clauses = [
        "NULLIF(TRIM(symbol), '') IS NOT NULL",
        "authority_scope = 'review_input_only'",
        "portfolio_authority = 'none'",
        "broker_execution_allowed = FALSE",
    ]
    params: list[Any] = []
    if from_date is not None:
        clauses.append("asof_date >= %s")
        params.append(from_date)
    if to_date is not None:
        clauses.append("asof_date <= %s")
        params.append(to_date)
    if symbols:
        clauses.append("UPPER(TRIM(symbol)) = ANY(%s)")
        params.append([str(value).strip().upper() for value in symbols if str(value).strip()])
    try:
        df = sql_to_df(
            f"""
            SELECT
                asof_date,
                memory_id,
                UPPER(TRIM(symbol)) AS symbol,
                sector_code,
                sector_name,
                context_source,
                event_group,
                event_type,
                context_class,
                direction,
                event_state,
                pressure_score,
                decayed_pressure_score,
                event_count,
                contradiction_state,
                source_refs_json,
                source_summary_json
            FROM {CAUSAL_EVENT_MEMORY_TABLE}
            WHERE {' AND '.join(clauses)}
            ORDER BY asof_date, symbol, context_source, context_class, event_type
            """,
            params=tuple(params) if params else None,
            retries=4,
            statement_timeout_ms=0,
            chunksize=50000,
        )
    except Exception as exc:
        _record_fallback(
            fallback_type="causal_event_memory_evaluator_memory_load_failed",
            source=CAUSAL_EVENT_MEMORY_TABLE,
            reason="Causal event memory evaluator could not load memory rows.",
            error=exc,
            metadata={
                "from_date": str(from_date) if from_date is not None else None,
                "to_date": str(to_date) if to_date is not None else None,
                "symbol_count": len(symbols or []),
            },
        )
        raise
    return normalize_memory_frame(df)


def normalize_memory_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["asof_date"] = pd.to_datetime(out["asof_date"], utc=True, errors="coerce").dt.normalize()
    out["symbol"] = out["symbol"].astype("string").str.strip().str.upper()
    for column in ["context_source", "event_group", "event_type", "context_class", "direction", "event_state", "contradiction_state"]:
        if column not in out.columns:
            out[column] = "unknown"
        out[column] = out[column].astype("string").str.strip().str.lower().fillna("unknown")
        out.loc[out[column].isin(["", "<na>", "nan", "none"]), column] = "unknown"
    for column in ["pressure_score", "decayed_pressure_score", "event_count"]:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce")
    return out.dropna(subset=["asof_date", "symbol", "memory_id"])


def _directional_scores(*, direction: str, forward_after_cost: float | None, excess_after_cost: float | None, threshold: float) -> dict[str, Any]:
    if forward_after_cost is None:
        return {
            "directional_helpfulness_score": None,
            "excess_directional_helpfulness_score": None,
            "direction_hit_after_cost": None,
            "excess_direction_hit_after_cost": None,
            "evaluation_label": "not_matured",
        }
    direction = str(direction or "").strip().lower()
    if direction in {"negative", "derisk", "sell", "reduce"}:
        helpful = -float(forward_after_cost)
        excess_helpful = None if excess_after_cost is None else -float(excess_after_cost)
        label = "negative_memory_protective_if_future_return_negative"
    elif direction in {"positive", "watch", "buy", "bullish"}:
        helpful = float(forward_after_cost)
        excess_helpful = None if excess_after_cost is None else float(excess_after_cost)
        label = "positive_memory_helpful_if_future_return_positive"
    else:
        helpful = None
        excess_helpful = None
        label = "mixed_memory_not_directionally_scored"
    return {
        "directional_helpfulness_score": helpful,
        "excess_directional_helpfulness_score": excess_helpful,
        "direction_hit_after_cost": None if helpful is None else bool(helpful > float(threshold)),
        "excess_direction_hit_after_cost": None if excess_helpful is None else bool(excess_helpful > float(threshold)),
        "evaluation_label": label,
    }


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
    cost = float(cost_bps) / 10000.0
    now = pd.to_datetime(evaluated_at or pd.Timestamp.utcnow(), utc=True, errors="coerce")
    rows: list[dict[str, Any]] = []
    for _, row in dataset.iterrows():
        for horizon in horizons:
            horizon = int(horizon)
            forward_return = _number(row.get(f"forward_return_h{horizon}"))
            benchmark_forward_return = _number(row.get(f"benchmark_forward_return_h{horizon}"))
            matured = forward_return is not None
            after_cost = None if forward_return is None else float(forward_return - cost)
            excess_after_cost = None
            if forward_return is not None and benchmark_forward_return is not None:
                excess_after_cost = float(forward_return - benchmark_forward_return - cost)
            scores = _directional_scores(
                direction=str(row.get("direction") or ""),
                forward_after_cost=after_cost,
                excess_after_cost=excess_after_cost,
                threshold=float(return_threshold),
            )
            raw_context = {
                "source_refs": _parse_jsonish(row.get("source_refs_json")),
                "source_summary": _parse_jsonish(row.get("source_summary_json")),
                "point_in_time_return_contract": _parse_jsonish(row.get("point_in_time_return_contract_json")),
                "benchmark_return_contract": _parse_jsonish(row.get("benchmark_return_contract_json")),
                "label_interpretation": (
                    "Positive/watch memory is helpful when later return after costs is positive; "
                    "negative/de-risk memory is helpful when later return after costs is negative. "
                    "Candidate-helpful memory also requires positive benchmark-excess directional helpfulness."
                ),
            }
            rows.append(
                {
                    "evaluated_at": now,
                    "horizon_days": horizon,
                    "memory_id": row.get("memory_id"),
                    "asof_date": row.get("asof_date"),
                    "symbol": row.get("symbol"),
                    "sector_code": row.get("sector_code"),
                    "sector_name": row.get("sector_name"),
                    "context_source": row.get("context_source"),
                    "event_group": row.get("event_group"),
                    "event_type": row.get("event_type"),
                    "context_class": row.get("context_class"),
                    "direction": row.get("direction"),
                    "event_state": row.get("event_state"),
                    "pressure_score": row.get("pressure_score"),
                    "decayed_pressure_score": row.get("decayed_pressure_score"),
                    "event_count": row.get("event_count"),
                    "contradiction_state": row.get("contradiction_state"),
                    "entry_date": row.get(f"entry_date_h{horizon}"),
                    "exit_date": row.get(f"exit_date_h{horizon}"),
                    "entry_close": row.get(f"entry_close_h{horizon}"),
                    "exit_close": row.get(f"exit_close_h{horizon}"),
                    "forward_return": forward_return,
                    "forward_return_after_cost": after_cost,
                    "benchmark_name": row.get("benchmark_name"),
                    "benchmark_entry_date": row.get(f"benchmark_entry_date_h{horizon}"),
                    "benchmark_exit_date": row.get(f"benchmark_exit_date_h{horizon}"),
                    "benchmark_entry_close": row.get(f"benchmark_entry_close_h{horizon}"),
                    "benchmark_exit_close": row.get(f"benchmark_exit_close_h{horizon}"),
                    "benchmark_forward_return": benchmark_forward_return,
                    "excess_return_after_cost": excess_after_cost,
                    **scores,
                    "matured": matured,
                    "authority": "research_only",
                    "broker_execution_allowed": False,
                    "policy_auto_promotion_allowed": False,
                    "raw_context_json": _json_dumps(raw_context),
                    "load_ts": pd.Timestamp.utcnow(),
                }
            )
    return normalize_evaluation_frame(pd.DataFrame(rows))


def _parse_jsonish(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if pd.api.types.is_scalar(value) and pd.isna(value):
        return None
    try:
        return json.loads(str(value))
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.causal_event_memory_evaluator",
            fallback_type="causal_event_memory_evaluator_json_parse_failed",
            source=CAUSAL_EVENT_MEMORY_TABLE,
            severity="warn",
            reason="Causal event memory evaluator could not parse stored JSON context and kept a text fallback.",
            error=exc,
            metadata={"payload_length": len(str(value))},
        )
        return str(value)


def _classification(group: pd.DataFrame, *, min_matured_rows: int) -> str:
    matured = group[group["matured"] == True]
    if len(matured) < int(min_matured_rows):
        return "needs_more_data"

    def _numeric_series(column: str) -> pd.Series:
        if column not in matured.columns:
            return pd.Series(dtype=float)
        return pd.to_numeric(matured[column], errors="coerce").dropna()

    score = _numeric_series("directional_helpfulness_score")
    hit = _numeric_series("direction_hit_after_cost")
    excess_score = _numeric_series("excess_directional_helpfulness_score")
    excess_hit = _numeric_series("excess_direction_hit_after_cost")
    if score.empty or hit.empty:
        return "not_directional"
    avg_score = float(score.mean())
    hit_rate = float(hit.mean())
    if avg_score > 0.01 and hit_rate >= 0.55:
        if excess_score.empty or excess_hit.empty:
            return "needs_benchmark_attribution"
        avg_excess_score = float(excess_score.mean())
        excess_hit_rate = float(excess_hit.mean())
        if avg_excess_score > 0.0 and excess_hit_rate >= 0.55:
            return "candidate_helpful"
        return "benchmark_beta_not_memory_alpha"
    if avg_score < -0.005 or hit_rate < 0.45:
        return "hurts_or_no_lift"
    return "mixed_or_horizon_sensitive"


def summarize_evaluations(evaluations: pd.DataFrame, *, min_matured_rows: int = DEFAULT_MIN_MATURED_ROWS) -> pd.DataFrame:
    if evaluations.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    keys = ["horizon_days", "context_source", "context_class", "event_type", "direction", "event_state"]
    for group_keys, group in evaluations.groupby(keys, dropna=False, sort=True):
        horizon, context_source, context_class, event_type, direction, event_state = group_keys
        matured = group[group["matured"] == True]
        classification = _classification(group, min_matured_rows=min_matured_rows)
        recommendation = (
            "review_for_low_weight_memory_influence"
            if classification == "candidate_helpful"
            else "keep_memory_explanation_only"
            if classification in {
                "mixed_or_horizon_sensitive",
                "hurts_or_no_lift",
                "not_directional",
                "needs_benchmark_attribution",
                "benchmark_beta_not_memory_alpha",
            }
            else "collect_more_matured_labels"
        )
        rows.append(
            {
                "evaluated_at": group["evaluated_at"].iloc[0],
                "horizon_days": int(horizon),
                "context_source": str(context_source or "unknown"),
                "context_class": str(context_class or "unknown"),
                "event_type": str(event_type or "unknown"),
                "direction": str(direction or "unknown"),
                "event_state": str(event_state or "unknown"),
                "sample_count": int(len(group)),
                "matured_count": int(len(matured)),
                "symbol_count": int(group["symbol"].nunique()),
                "avg_forward_return": _mean_or_none(matured.get("forward_return")),
                "avg_forward_return_after_cost": _mean_or_none(matured.get("forward_return_after_cost")),
                "avg_benchmark_forward_return": _mean_or_none(matured.get("benchmark_forward_return")),
                "avg_excess_return_after_cost": _mean_or_none(matured.get("excess_return_after_cost")),
                "avg_directional_helpfulness_score": _mean_or_none(matured.get("directional_helpfulness_score")),
                "avg_excess_directional_helpfulness_score": _mean_or_none(matured.get("excess_directional_helpfulness_score")),
                "direction_hit_rate_after_cost": _mean_or_none(matured.get("direction_hit_after_cost")),
                "excess_direction_hit_rate_after_cost": _mean_or_none(matured.get("excess_direction_hit_after_cost")),
                "avg_pressure_score": _mean_or_none(group.get("pressure_score")),
                "avg_decayed_pressure_score": _mean_or_none(group.get("decayed_pressure_score")),
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
            "status": "no_causal_memory_evidence",
            "ready_for_policy_review": False,
            "operator_action": "Collect causal event memory rows and realized labels before considering any memory influence rule.",
            "matured_rows_total": 0,
            "matured_rows_by_horizon": {},
            "candidate_group_count": 0,
            "harmful_group_count": 0,
            "required_min_matured_rows": int(min_matured_rows),
            "authority": "research_only",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
        }
    evals = normalize_evaluation_frame(evaluations)
    matured = evals[evals["matured"].fillna(False).astype(bool)] if "matured" in evals.columns else pd.DataFrame()
    matured_rows_by_horizon = {
        str(int(horizon)): int(group.shape[0])
        for horizon, group in matured.groupby("horizon_days", dropna=False)
        if pd.notna(horizon)
    }
    candidate_groups = pd.DataFrame()
    harmful_groups = pd.DataFrame()
    if not summary.empty and "classification" in summary.columns:
        classification = summary["classification"].astype("string").str.strip()
        candidate_groups = summary[classification.eq("candidate_helpful")]
        harmful_groups = summary[classification.isin(["hurts_or_no_lift", "benchmark_beta_not_memory_alpha"])]
    enough_any_horizon = any(count >= int(min_matured_rows) for count in matured_rows_by_horizon.values())
    if not matured_rows_by_horizon:
        status = "no_matured_labels"
        operator_action = "Wait for forward-return windows to mature before using causal memory evidence."
    elif not enough_any_horizon:
        status = "collect_more_matured_labels"
        operator_action = "Collect more matured causal-memory labels; current evidence is below the minimum row gate."
    elif candidate_groups.empty:
        status = "matured_but_no_candidate_memory_signal"
        operator_action = "Keep causal memory explanation-only; matured evidence has no helpful candidate group yet."
    else:
        status = "candidate_for_manual_policy_design"
        operator_action = (
            "Review candidate memory groups offline; any future runtime influence must be low-weight, deterministic, "
            "bounded by technical/risk/lifecycle gates, and separately tested."
        )
    return {
        "status": status,
        "ready_for_policy_review": bool(enough_any_horizon and not candidate_groups.empty),
        "operator_action": operator_action,
        "matured_rows_total": int(len(matured)),
        "matured_rows_by_horizon": matured_rows_by_horizon,
        "candidate_group_count": int(len(candidate_groups)),
        "harmful_group_count": int(len(harmful_groups)),
        "candidate_groups": candidate_groups.head(10).to_dict(orient="records") if not candidate_groups.empty else [],
        "harmful_groups": harmful_groups.head(10).to_dict(orient="records") if not harmful_groups.empty else [],
        "required_min_matured_rows": int(min_matured_rows),
        "authority": "research_only",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
    }


def generate_config_previews_from_readiness(
    readiness_contract: dict[str, Any],
    *,
    persist: bool = False,
    max_previews: int = 5,
    include_harmful: bool = False,
) -> dict[str, Any]:
    candidates = list(readiness_contract.get("candidate_groups") or [])
    harmful_candidates = list(readiness_contract.get("harmful_groups") or []) if include_harmful else []
    preview_groups = [("helpful", group) for group in candidates] + [("suppression", group) for group in harmful_candidates]
    if not preview_groups:
        return {
            "status": "skipped",
            "reason": readiness_contract.get("status") or "no_candidate_groups",
            "generated_count": 0,
            "error_count": 0,
            "persisted": bool(persist),
            "previews": [],
            "errors": [],
            "authority": "review_input_only",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
        }
    from advisory.config_change_assistant import build_causal_event_memory_rule_preview

    previews: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    helpful_generated = 0
    suppression_generated = 0
    for preview_kind, group in preview_groups[: max(0, int(max_previews))]:
        try:
            preview = build_causal_event_memory_rule_preview(
                horizon_days=int(group.get("horizon_days")),
                context_source=str(group.get("context_source") or "unknown"),
                context_class=str(group.get("context_class") or "unknown"),
                event_type=str(group.get("event_type") or "unknown"),
                direction=str(group.get("direction") or "unknown"),
                event_state=str(group.get("event_state") or "unknown"),
                matured_count=int(group.get("matured_count")) if group.get("matured_count") is not None else None,
                direction_hit_rate_after_cost=_number(group.get("direction_hit_rate_after_cost")),
                avg_directional_helpfulness_score=_number(group.get("avg_directional_helpfulness_score")),
                excess_direction_hit_rate_after_cost=_number(group.get("excess_direction_hit_rate_after_cost")),
                avg_excess_directional_helpfulness_score=_number(group.get("avg_excess_directional_helpfulness_score")),
                classification="candidate_helpful" if preview_kind == "helpful" else str(group.get("classification") or "hurts_or_no_lift"),
                persist=bool(persist),
            )
            if preview_kind == "helpful":
                helpful_generated += 1
            else:
                suppression_generated += 1
            previews.append(
                {
                    "preview_id": preview.get("preview_id"),
                    "preview_kind": preview_kind,
                    "source_type": preview.get("source_type"),
                    "source_key": preview.get("source_key"),
                    "applied": bool(preview.get("applied")),
                    "config_path": preview.get("config_path"),
                    "review_status": preview.get("review_status"),
                    "decision_status": preview.get("decision_status"),
                    "patch_payload": preview.get("patch_payload"),
                    "safety_checks": preview.get("safety_checks"),
                }
            )
        except Exception as exc:
            record_local_fallback_event(
                module="advisory.causal_event_memory_evaluator",
                fallback_type="causal_event_memory_config_preview_generation_failed",
                source="causal_event_memory_readiness_group",
                severity="warn",
                reason="Causal event memory evaluator skipped a config-preview candidate after preview generation failed.",
                error=exc,
                metadata={
                    "context_source": group.get("context_source"),
                    "context_class": group.get("context_class"),
                    "event_type": group.get("event_type"),
                    "direction": group.get("direction"),
                    "event_state": group.get("event_state"),
                },
            )
            errors.append(
                {
                    "context_source": group.get("context_source"),
                    "context_class": group.get("context_class"),
                    "event_type": group.get("event_type"),
                    "direction": group.get("direction"),
                    "event_state": group.get("event_state"),
                    "horizon_days": group.get("horizon_days"),
                    "preview_kind": preview_kind,
                    "error_type": exc.__class__.__name__,
                    "error": str(exc)[:500],
                }
            )
    return {
        "status": "ok" if not errors else "partial_error" if previews else "error",
        "generated_count": len(previews),
        "helpful_generated_count": helpful_generated,
        "suppression_generated_count": suppression_generated,
        "error_count": len(errors),
        "candidate_count": len(candidates),
        "suppression_candidate_count": len(harmful_candidates),
        "max_previews": int(max_previews),
        "persisted": bool(persist),
        "previews": previews,
        "errors": errors,
        "authority": "review_input_only",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        "runtime_consumer_created": False,
        "note": "Generated disabled config-review previews only; no action, portfolio, runtime policy, or broker behavior changed.",
    }


def _mean_or_none(series: Any) -> float | None:
    if series is None:
        return None
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return None
    return float(values.mean())


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
    return out


def evaluate_causal_event_memory(
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
    memory = load_memory_rows(from_date=from_date, to_date=to_date, symbols=symbols)
    if memory.empty or not effective_horizons:
        return pd.DataFrame(), pd.DataFrame(), {
            "status": "no_memory_rows",
            "memory_rows": int(len(memory)),
            "horizons": effective_horizons,
            "authority": "research_only",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
        }
    price_start = memory["asof_date"].min() - pd.Timedelta(days=5)
    price_end = memory["asof_date"].max() + pd.Timedelta(days=max(effective_horizons) * 3 + 15)
    prices = load_price_history_for_returns(
        symbols=memory["symbol"].dropna().astype(str).str.upper().drop_duplicates().tolist(),
        from_date=price_start,
        to_date=price_end,
    )
    dataset = attach_forward_returns(memory, prices, horizons=effective_horizons)
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
    summary = summarize_evaluations(evaluations, min_matured_rows=min_matured_rows)
    readiness_contract = build_readiness_contract(evaluations, summary, min_matured_rows=min_matured_rows)
    meta = {
        "status": "ok",
        "memory_rows": int(len(memory)),
        "price_rows": int(len(prices)),
        "benchmark_rows": int(len(benchmark)),
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
        "sector_scope_evaluated": False,
        "sector_scope_note": "V1 evaluates symbol-scoped memory only; sector-only memory remains explanation context until mapped separately.",
        "authority": "research_only",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        "readiness_contract": readiness_contract,
        "point_in_time_return_contract": (
            _parse_jsonish(dataset["point_in_time_return_contract_json"].dropna().iloc[0])
            if "point_in_time_return_contract_json" in dataset.columns and dataset["point_in_time_return_contract_json"].notna().any()
            else {}
        ),
        "benchmark_return_contract": (
            _parse_jsonish(dataset["benchmark_return_contract_json"].dropna().iloc[0])
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
        upsert_to_db(evaluations, EVALUATIONS_TABLE, unique_keys=["evaluated_at", "horizon_days", "memory_id"])
    if not summary.empty:
        upsert_to_db(
            summary,
            SUMMARY_TABLE,
            unique_keys=["evaluated_at", "horizon_days", "context_source", "context_class", "event_type", "direction", "event_state"],
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate research-only causal event memory against realized forward returns."
    )
    parser.add_argument("--from-date", type=parse_datetime_arg)
    parser.add_argument("--to-date", type=parse_datetime_arg)
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument("--horizons", type=int, nargs="*", default=DEFAULT_HORIZONS)
    parser.add_argument("--return-threshold", type=float, default=DEFAULT_RETURN_THRESHOLD)
    parser.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    parser.add_argument("--min-matured-rows", type=int, default=DEFAULT_MIN_MATURED_ROWS)
    parser.add_argument("--generate-config-previews", action="store_true")
    parser.add_argument(
        "--include-suppression-config-previews",
        action="store_true",
        help="Also generate disabled suppression-review previews for hurts_or_no_lift causal-memory groups.",
    )
    parser.add_argument("--max-config-previews", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--format", choices=["json", "text"], default="json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    evaluations, summary, meta = evaluate_causal_event_memory(
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
    config_preview_generation = (
        generate_config_previews_from_readiness(
            meta.get("readiness_contract") or {},
            persist=not bool(args.dry_run),
            max_previews=int(args.max_config_previews),
            include_harmful=bool(args.include_suppression_config_previews),
        )
        if bool(args.generate_config_previews)
        else {
            "status": "not_requested",
            "generated_count": 0,
            "persisted": False,
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
        }
    )
    payload = {
        "status": "ok",
        "readiness_status": (meta.get("readiness_contract") or {}).get("status"),
        "ready_for_policy_review": bool((meta.get("readiness_contract") or {}).get("ready_for_policy_review")),
        "evaluations_table": EVALUATIONS_TABLE,
        "summary_table": SUMMARY_TABLE,
        "evaluation_rows": int(len(evaluations)),
        "summary_rows": int(len(summary)),
        "meta": _json_ready(meta),
        "readiness_contract": _json_ready(meta.get("readiness_contract") or {}),
        "config_preview_generation": _json_ready(config_preview_generation),
        "summary_sample": _json_ready(summary.head(20).to_dict(orient="records") if not summary.empty else []),
        "dry_run": bool(args.dry_run),
    }
    if args.format == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    else:
        print(
            f"causal_event_memory_evaluator rows={len(evaluations)} summary={len(summary)} "
            f"readiness={payload['readiness_status']} previews={config_preview_generation.get('status')} "
            f"generated={config_preview_generation.get('generated_count')} dry_run={args.dry_run}"
        )
        print(f"meta={json.dumps(_json_ready(meta), ensure_ascii=False, default=str)}")
        if not summary.empty:
            print(
                summary[
                    [
                        "horizon_days",
                        "context_source",
                        "context_class",
                        "event_type",
                        "direction",
                        "matured_count",
                        "avg_directional_helpfulness_score",
                        "classification",
                    ]
                ]
                .head(20)
                .to_string(index=False)
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
