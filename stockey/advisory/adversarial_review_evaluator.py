from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from advisory.adversarial_review import REVIEWS_TABLE
from advisory.fallback_telemetry import record_local_fallback_event
from advisory.return_attribution import attach_benchmark_forward_returns, load_benchmark_history_for_attribution
from advisory.technical_threshold_calibration import attach_forward_returns, load_price_history_for_returns
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration
from utils.sync import parse_datetime_arg


EVALUATIONS_TABLE = "advisory_adversarial_review_evaluations"
SUMMARY_TABLE = "advisory_adversarial_review_eval_summary"
ADVERSARIAL_REVIEW_EVAL_SCHEMA_MIGRATION_ID = "20260621_advisory_adversarial_review_evaluator_base"
ADVERSARIAL_REVIEW_BENCHMARK_SCHEMA_MIGRATION_ID = "20260622_adversarial_review_evaluator_benchmark_attribution"
DEFAULT_HORIZONS = [5, 10, 20]
DEFAULT_COST_BPS = 25.0
DEFAULT_FALSE_POSITIVE_RETURN_THRESHOLD = 0.03
DEFAULT_MIN_MATURED_ROWS = 10

SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {EVALUATIONS_TABLE} (
        evaluated_at TIMESTAMPTZ NOT NULL,
        horizon_days BIGINT NOT NULL,
        published_on TIMESTAMPTZ,
        asof_date TIMESTAMPTZ,
        setup_id TEXT,
        symbol TEXT NOT NULL,
        unique_id TEXT,
        event_source TEXT,
        review_action TEXT,
        review_score DOUBLE PRECISION,
        veto BOOLEAN,
        review_reason TEXT,
        review_flags_json TEXT,
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
        avoided_loss_return_after_cost DOUBLE PRECISION,
        excess_avoided_loss_return_after_cost DOUBLE PRECISION,
        would_have_been_false_positive BOOLEAN,
        would_have_been_excess_false_positive BOOLEAN,
        matured BOOLEAN,
        raw_context_json TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (evaluated_at, horizon_days, published_on, setup_id, symbol, unique_id)
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
        avg_forward_return_after_cost DOUBLE PRECISION,
        avg_benchmark_forward_return DOUBLE PRECISION,
        avg_avoided_loss_return_after_cost DOUBLE PRECISION,
        avg_excess_avoided_loss_return_after_cost DOUBLE PRECISION,
        false_positive_rate DOUBLE PRECISION,
        excess_false_positive_rate DOUBLE PRECISION,
        positive_return_rate DOUBLE PRECISION,
        positive_excess_return_rate DOUBLE PRECISION,
        avg_review_score DOUBLE PRECISION,
        sample_start TIMESTAMPTZ,
        sample_end TIMESTAMPTZ,
        recommendation TEXT,
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
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS excess_avoided_loss_return_after_cost DOUBLE PRECISION",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS would_have_been_excess_false_positive BOOLEAN",
    f"ALTER TABLE {SUMMARY_TABLE} ADD COLUMN IF NOT EXISTS avg_benchmark_forward_return DOUBLE PRECISION",
    f"ALTER TABLE {SUMMARY_TABLE} ADD COLUMN IF NOT EXISTS avg_excess_avoided_loss_return_after_cost DOUBLE PRECISION",
    f"ALTER TABLE {SUMMARY_TABLE} ADD COLUMN IF NOT EXISTS excess_false_positive_rate DOUBLE PRECISION",
    f"ALTER TABLE {SUMMARY_TABLE} ADD COLUMN IF NOT EXISTS positive_excess_return_rate DOUBLE PRECISION",
]

EVAL_NUMERIC_COLUMNS = [
    "review_score",
    "entry_close",
    "exit_close",
    "forward_return",
    "benchmark_entry_close",
    "benchmark_exit_close",
    "benchmark_forward_return",
    "forward_return_after_cost",
    "avoided_loss_return_after_cost",
    "excess_avoided_loss_return_after_cost",
]
EVAL_INT_COLUMNS = ["horizon_days"]
EVAL_BOOL_COLUMNS = ["veto", "would_have_been_false_positive", "would_have_been_excess_false_positive", "matured"]
EVAL_TS_COLUMNS = [
    "evaluated_at",
    "published_on",
    "asof_date",
    "entry_date",
    "exit_date",
    "benchmark_entry_date",
    "benchmark_exit_date",
    "load_ts",
]
SUMMARY_NUMERIC_COLUMNS = [
    "avg_forward_return_after_cost",
    "avg_benchmark_forward_return",
    "avg_avoided_loss_return_after_cost",
    "avg_excess_avoided_loss_return_after_cost",
    "false_positive_rate",
    "excess_false_positive_rate",
    "positive_return_rate",
    "positive_excess_return_rate",
    "avg_review_score",
]
SUMMARY_INT_COLUMNS = ["horizon_days", "sample_count", "matured_count"]
SUMMARY_TS_COLUMNS = ["evaluated_at", "sample_start", "sample_end", "load_ts"]


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


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
    for column in EVAL_NUMERIC_COLUMNS:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce")
    for column in EVAL_INT_COLUMNS:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce").astype("Int64")
    for column in EVAL_BOOL_COLUMNS:
        if column in out.columns:
            out[column] = _coerce_bool_series(out[column])
    for column in EVAL_TS_COLUMNS:
        if column in out.columns:
            out[column] = pd.to_datetime(out[column], utc=True, errors="coerce")
    return out


def normalize_summary_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    for column in SUMMARY_NUMERIC_COLUMNS:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce")
    for column in SUMMARY_INT_COLUMNS:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce").astype("Int64")
    for column in SUMMARY_TS_COLUMNS:
        if column in out.columns:
            out[column] = pd.to_datetime(out[column], utc=True, errors="coerce")
    return out


def ensure_tables() -> None:
    apply_schema_migration(
        migration_id=ADVERSARIAL_REVIEW_EVAL_SCHEMA_MIGRATION_ID,
        description="Create adversarial-review outcome evaluator tables.",
        statements=SCHEMA_STATEMENTS,
        metadata={"tables": [EVALUATIONS_TABLE, SUMMARY_TABLE], "authority_scope": "research_only_no_policy_change"},
    )
    apply_schema_migration(
        migration_id=ADVERSARIAL_REVIEW_BENCHMARK_SCHEMA_MIGRATION_ID,
        description="Add benchmark-excess attribution to adversarial-review evaluator outputs.",
        statements=BENCHMARK_SCHEMA_STATEMENTS,
        metadata={
            "tables": [EVALUATIONS_TABLE, SUMMARY_TABLE],
            "authority_scope": "research_only_benchmark_attribution",
            "additive": True,
        },
    )


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
            module="advisory.adversarial_review_evaluator",
            fallback_type="adversarial_review_evaluator_schema_lookup_failed",
            source=table_name,
            severity="warn",
            reason="Adversarial-review evaluator treated a source table as unavailable because schema lookup failed.",
            error=exc,
        )
        return set()
    return set(df["column_name"].astype(str).tolist()) if not df.empty else set()


def load_review_rows(
    *,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
) -> pd.DataFrame:
    available = table_columns(REVIEWS_TABLE)
    if not available:
        return pd.DataFrame()
    required = {"published_on", "symbol", "review_action", "veto"}
    if not required.issubset(available):
        missing = sorted(required - available)
        record_local_fallback_event(
            module="advisory.adversarial_review_evaluator",
            fallback_type="adversarial_review_evaluator_required_columns_missing",
            source=REVIEWS_TABLE,
            severity="warn",
            reason="Adversarial-review evaluator returned empty output because required source columns were missing.",
            metadata={"missing_columns": missing},
        )
        return pd.DataFrame()
    wanted = [
        "published_on",
        "asof_date",
        "reviewed_at",
        "setup_id",
        "symbol",
        "unique_id",
        "event_source",
        "review_action",
        "review_score",
        "veto",
        "review_reason",
        "review_flags_json",
        "feature_snapshot_json",
        "load_ts",
    ]
    selected = [column for column in wanted if column in available]
    clauses = ["NULLIF(TRIM(symbol), '') IS NOT NULL", "published_on IS NOT NULL"]
    params: list[Any] = []
    if from_date is not None:
        clauses.append("COALESCE(asof_date, published_on) >= %s")
        params.append(from_date)
    if to_date is not None:
        clauses.append("COALESCE(asof_date, published_on) <= %s")
        params.append(to_date)
    if symbols:
        clauses.append("UPPER(TRIM(symbol)) = ANY(%s)")
        params.append([str(value).upper() for value in symbols])
    try:
        df = sql_to_df(
            f"""
            SELECT {', '.join(selected)}
            FROM {REVIEWS_TABLE}
            WHERE {' AND '.join(clauses)}
            ORDER BY COALESCE(asof_date, published_on), symbol, setup_id, unique_id
            """,
            params=tuple(params) if params else None,
            retries=4,
            statement_timeout_ms=0,
            chunksize=50000,
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.adversarial_review_evaluator",
            fallback_type="adversarial_review_evaluator_review_rows_load_failed",
            source=REVIEWS_TABLE,
            severity="warn",
            reason="Adversarial-review evaluator returned empty output because review row loading failed.",
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
    df["published_on"] = pd.to_datetime(df["published_on"], utc=True, errors="coerce")
    if "asof_date" not in df.columns:
        df["asof_date"] = df["published_on"].dt.normalize()
    else:
        df["asof_date"] = pd.to_datetime(df["asof_date"], utc=True, errors="coerce").fillna(df["published_on"]).dt.normalize()
    df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()
    for column in ["setup_id", "unique_id", "event_source", "review_action", "review_reason"]:
        if column not in df.columns:
            df[column] = None
        df[column] = df[column].astype("string").str.strip()
    if "review_score" not in df.columns:
        df["review_score"] = pd.NA
    df["review_score"] = pd.to_numeric(df["review_score"], errors="coerce")
    df["veto"] = _coerce_bool_series(df["veto"]).fillna(False)
    if "review_flags_json" not in df.columns:
        df["review_flags_json"] = None
    if "feature_snapshot_json" not in df.columns:
        df["feature_snapshot_json"] = None
    return df.dropna(subset=["published_on", "asof_date", "symbol"])


def _parse_flags(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    try:
        if pd.isna(value):
            return []
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.adversarial_review_evaluator",
            fallback_type="adversarial_review_evaluator_flags_missing_check_failed",
            source=REVIEWS_TABLE,
            severity="warn",
            reason="Adversarial-review evaluator could not evaluate missingness for review_flags_json and continued parsing.",
            error=exc,
            metadata={"value_type": type(value).__name__},
        )
    try:
        parsed = json.loads(str(value))
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.adversarial_review_evaluator",
            fallback_type="adversarial_review_evaluator_flags_parse_failed",
            source=REVIEWS_TABLE,
            severity="warn",
            reason="Adversarial-review evaluator could not parse review_flags_json; using an empty flag set.",
            error=exc,
            metadata={"payload_length": len(str(value))},
        )
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


def _parse_jsonish(value: Any, default: Any | None = None) -> Any:
    if default is None:
        default = {}
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.adversarial_review_evaluator",
            fallback_type="adversarial_review_evaluator_json_missing_check_failed",
            source="adversarial_review_json_fields",
            severity="warn",
            reason="Adversarial-review evaluator could not evaluate JSON-field missingness and continued parsing.",
            error=exc,
            metadata={"value_type": type(value).__name__, "default_type": type(default).__name__},
        )
        pass
    try:
        parsed = json.loads(str(value))
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.adversarial_review_evaluator",
            fallback_type="adversarial_review_evaluator_json_parse_failed",
            source="adversarial_review_json_fields",
            severity="warn",
            reason="Adversarial-review evaluator could not parse a JSON field and used the default fallback.",
            error=exc,
            metadata={
                "value_type": type(value).__name__,
                "payload_length": len(str(value)),
                "default_type": type(default).__name__,
            },
        )
        return default
    return parsed


def build_evaluation_rows(
    dataset: pd.DataFrame,
    *,
    horizons: list[int],
    cost_bps: float = DEFAULT_COST_BPS,
    false_positive_return_threshold: float = DEFAULT_FALSE_POSITIVE_RETURN_THRESHOLD,
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
            return_col = f"forward_return_h{horizon}"
            forward_return = pd.to_numeric(row.get(return_col), errors="coerce")
            matured = bool(pd.notna(forward_return))
            benchmark_forward_return = pd.to_numeric(row.get(f"benchmark_forward_return_h{horizon}"), errors="coerce")
            has_benchmark = bool(pd.notna(benchmark_forward_return))
            after_cost = None if not matured else float(forward_return) - cost
            excess_avoided = None if not matured or not has_benchmark else float(benchmark_forward_return) - float(forward_return) - cost
            benchmark_entry_date = pd.to_datetime(row.get(f"benchmark_entry_date_h{horizon}"), utc=True, errors="coerce")
            benchmark_exit_date = pd.to_datetime(row.get(f"benchmark_exit_date_h{horizon}"), utc=True, errors="coerce")
            rows.append(
                {
                    "evaluated_at": effective_evaluated_at,
                    "horizon_days": horizon,
                    "published_on": row.get("published_on"),
                    "asof_date": row.get("asof_date"),
                    "setup_id": row.get("setup_id"),
                    "symbol": row.get("symbol"),
                    "unique_id": row.get("unique_id"),
                    "event_source": row.get("event_source"),
                    "review_action": row.get("review_action"),
                    "review_score": row.get("review_score"),
                    "veto": row.get("veto"),
                    "review_reason": row.get("review_reason"),
                    "review_flags_json": row.get("review_flags_json"),
                    "entry_date": row.get(f"entry_date_h{horizon}"),
                    "exit_date": row.get(f"exit_date_h{horizon}"),
                    "entry_close": row.get(f"entry_close_h{horizon}"),
                    "exit_close": row.get(f"exit_close_h{horizon}"),
                    "forward_return": None if not matured else float(forward_return),
                    "benchmark_name": row.get("benchmark_name"),
                    "benchmark_entry_date": None if pd.isna(benchmark_entry_date) else benchmark_entry_date,
                    "benchmark_exit_date": None if pd.isna(benchmark_exit_date) else benchmark_exit_date,
                    "benchmark_entry_close": row.get(f"benchmark_entry_close_h{horizon}"),
                    "benchmark_exit_close": row.get(f"benchmark_exit_close_h{horizon}"),
                    "benchmark_forward_return": None if not has_benchmark else float(benchmark_forward_return),
                    "forward_return_after_cost": after_cost,
                    "avoided_loss_return_after_cost": None if after_cost is None else -float(after_cost),
                    "excess_avoided_loss_return_after_cost": excess_avoided,
                    "would_have_been_false_positive": None if after_cost is None else bool(after_cost >= float(false_positive_return_threshold)),
                    "would_have_been_excess_false_positive": None
                    if excess_avoided is None
                    else bool(-float(excess_avoided) >= float(false_positive_return_threshold)),
                    "matured": matured,
                    "raw_context_json": json_dumps(
                        {
                            "review_flags": _parse_flags(row.get("review_flags_json")),
                            "feature_snapshot": row.get("feature_snapshot_json"),
                            "benchmark_return_contract": _parse_jsonish(row.get("benchmark_return_contract_json"), {}),
                            "authority_scope": "research_only_no_policy_change",
                            "interpretation": "Positive avoided_loss_return_after_cost means the adversarial review avoided a bad forward return. Positive excess_avoided_loss_return_after_cost means the reviewed stock also underperformed the benchmark after costs.",
                        }
                    ),
                    "load_ts": pd.Timestamp.utcnow(),
                }
            )
    return normalize_evaluation_frame(pd.DataFrame(rows))


def _summary_recommendation(
    *,
    group_type: str,
    group_value: str,
    matured_count: int,
    avg_avoided: float | None,
    avg_excess_avoided: float | None,
    false_positive_rate: float | None,
    excess_false_positive_rate: float | None,
    min_rows: int,
) -> str:
    if group_type == "review_action" and group_value == "clear":
        return "baseline_clear_not_adversarial_intervention"
    if group_type == "veto" and group_value == "no_veto":
        return "baseline_no_veto_not_adversarial_intervention"
    if matured_count < int(min_rows):
        return "insufficient_matured_rows"
    if avg_excess_avoided is None or excess_false_positive_rate is None:
        return "needs_benchmark_attribution"
    if (
        avg_avoided is not None
        and false_positive_rate is not None
        and avg_avoided > 0.0
        and false_positive_rate <= 0.40
        and avg_excess_avoided > 0.0
        and excess_false_positive_rate <= 0.40
    ):
        return "candidate_veto_policy_keep_or_tighten"
    if (
        avg_avoided is not None
        and false_positive_rate is not None
        and avg_avoided > 0.0
        and false_positive_rate <= 0.40
        and avg_excess_avoided <= 0.0
    ):
        return "benchmark_beta_not_veto_alpha"
    if (
        avg_avoided is not None
        and false_positive_rate is not None
        and (
            avg_avoided < 0.0
            or false_positive_rate >= 0.60
            or avg_excess_avoided < 0.0
            or excess_false_positive_rate >= 0.60
        )
    ):
        return "candidate_veto_policy_relax_or_review_false_positives"
    return "monitor"


def summarize_evaluations(
    evaluations: pd.DataFrame,
    *,
    evaluated_at: pd.Timestamp,
    min_matured_rows: int = DEFAULT_MIN_MATURED_ROWS,
) -> pd.DataFrame:
    if evaluations.empty:
        return pd.DataFrame()
    evals = evaluations.copy()
    flag_rows: list[pd.DataFrame] = []
    if "review_flags_json" in evals.columns:
        for flag, group in evals.groupby(evals["review_flags_json"].map(lambda value: tuple(_parse_flags(value))), dropna=False):
            for item in flag if isinstance(flag, tuple) else ():
                if item:
                    temp = group.copy()
                    temp["review_flag"] = item
                    flag_rows.append(temp)
    grouped_specs: list[tuple[str, pd.DataFrame, list[str]]] = [
        ("review_action", evals, ["review_action"]),
        ("veto", evals.assign(veto_label=evals["veto"].map(lambda value: "veto" if bool(value) else "no_veto")), ["veto_label"]),
    ]
    if flag_rows:
        grouped_specs.append(("review_flag", pd.concat(flag_rows, ignore_index=True), ["review_flag"]))
    rows: list[dict[str, Any]] = []
    for horizon, horizon_group in evals.groupby("horizon_days", dropna=False):
        for group_type, source, columns in grouped_specs:
            source_horizon = source[source["horizon_days"] == horizon]
            if source_horizon.empty:
                continue
            for keys, group in source_horizon.groupby(columns, dropna=False):
                key_values = keys if isinstance(keys, tuple) else (keys,)
                group_value = "|".join(str(value) for value in key_values)
                matured = group[group["matured"].fillna(False).astype(bool)]
                after_cost = pd.to_numeric(matured.get("forward_return_after_cost"), errors="coerce").dropna()
                benchmark_forward = pd.to_numeric(matured.get("benchmark_forward_return"), errors="coerce").dropna()
                avoided = pd.to_numeric(matured.get("avoided_loss_return_after_cost"), errors="coerce").dropna()
                excess_avoided = pd.to_numeric(matured.get("excess_avoided_loss_return_after_cost"), errors="coerce").dropna()
                false_positive = (
                    matured["would_have_been_false_positive"].dropna().astype(bool)
                    if "would_have_been_false_positive" in matured.columns
                    else pd.Series(dtype=bool)
                )
                excess_false_positive = (
                    matured["would_have_been_excess_false_positive"].dropna().astype(bool)
                    if "would_have_been_excess_false_positive" in matured.columns
                    else pd.Series(dtype=bool)
                )
                avg_avoided = float(avoided.mean()) if not avoided.empty else None
                avg_excess_avoided = float(excess_avoided.mean()) if not excess_avoided.empty else None
                false_positive_rate = float(false_positive.mean()) if not false_positive.empty else None
                excess_false_positive_rate = float(excess_false_positive.mean()) if not excess_false_positive.empty else None
                rows.append(
                    {
                        "evaluated_at": evaluated_at,
                        "horizon_days": int(horizon),
                        "group_type": group_type,
                        "group_value": group_value,
                        "sample_count": int(len(group)),
                        "matured_count": int(len(matured)),
                        "avg_forward_return_after_cost": float(after_cost.mean()) if not after_cost.empty else None,
                        "avg_benchmark_forward_return": float(benchmark_forward.mean()) if not benchmark_forward.empty else None,
                        "avg_avoided_loss_return_after_cost": avg_avoided,
                        "avg_excess_avoided_loss_return_after_cost": avg_excess_avoided,
                        "false_positive_rate": false_positive_rate,
                        "excess_false_positive_rate": excess_false_positive_rate,
                        "positive_return_rate": float(after_cost.gt(0).mean()) if not after_cost.empty else None,
                        "positive_excess_return_rate": float(excess_avoided.lt(0).mean()) if not excess_avoided.empty else None,
                        "avg_review_score": None if group["review_score"].dropna().empty else float(group["review_score"].mean()),
                        "sample_start": matured["asof_date"].min() if not matured.empty else pd.NaT,
                        "sample_end": matured["asof_date"].max() if not matured.empty else pd.NaT,
                        "recommendation": _summary_recommendation(
                            group_type=group_type,
                            group_value=group_value,
                            matured_count=int(len(matured)),
                            avg_avoided=avg_avoided,
                            avg_excess_avoided=avg_excess_avoided,
                            false_positive_rate=false_positive_rate,
                            excess_false_positive_rate=excess_false_positive_rate,
                            min_rows=min_matured_rows,
                        ),
                        "load_ts": pd.Timestamp.utcnow(),
                    }
                )
    return normalize_summary_frame(pd.DataFrame(rows))


def build_research_queue(summary: pd.DataFrame, *, top_n: int = 20) -> dict[str, Any]:
    if summary.empty:
        return {
            "status": "no_summary_rows",
            "authority_scope": "research_only_no_policy_change",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
            "candidate_keep_or_tighten_count": 0,
            "candidate_relax_or_review_count": 0,
            "needs_more_data_count": 0,
            "baseline_count": 0,
            "items": [],
        }
    rows: list[dict[str, Any]] = []
    counts = {
        "candidate_keep_or_tighten_count": 0,
        "candidate_relax_or_review_count": 0,
        "needs_more_data_count": 0,
        "baseline_count": 0,
    }
    for _, row in summary.iterrows():
        recommendation = str(row.get("recommendation") or "").strip()
        if recommendation.startswith("baseline_"):
            bucket = "baseline"
            counts["baseline_count"] += 1
            priority = 3
            action = "keep_as_baseline_context"
            reason = "This row is a non-intervention baseline, not a veto-quality rule candidate."
        elif recommendation == "candidate_veto_policy_keep_or_tighten":
            bucket = "candidate_keep_or_tighten"
            counts["candidate_keep_or_tighten_count"] += 1
            priority = 0
            action = "consider_keep_or_tighten_adversarial_rule"
            reason = "The intervention avoided losses after costs with acceptable false-positive rate."
        elif recommendation == "candidate_veto_policy_relax_or_review_false_positives":
            bucket = "candidate_relax_or_review_false_positives"
            counts["candidate_relax_or_review_count"] += 1
            priority = 1
            action = "review_false_positive_or_relax_adversarial_rule"
            reason = "The intervention may be blocking rows that later rose after costs."
        elif recommendation == "benchmark_beta_not_veto_alpha":
            bucket = "candidate_relax_or_review_false_positives"
            counts["candidate_relax_or_review_count"] += 1
            priority = 1
            action = "keep_research_only_collect_excess_veto_evidence"
            reason = "The intervention looked useful in raw returns but did not beat benchmark-relative protection."
        else:
            bucket = "needs_more_data"
            counts["needs_more_data_count"] += 1
            priority = 2
            action = "collect_more_matured_adversarial_review_evidence"
            reason = "The group does not yet have enough stable matured evidence."
        rows.append(
            {
                "queue_id": "adversarial_{group_type}_{group_value}_h{horizon}".format(
                    group_type=str(row.get("group_type") or "unknown").lower().replace(" ", "_"),
                    group_value=str(row.get("group_value") or "unknown").lower().replace(" ", "_"),
                    horizon=int(row.get("horizon_days") or 0),
                ),
                "bucket": bucket,
                "recommended_next_step": action,
                "reason": reason,
                "horizon_days": int(row.get("horizon_days") or 0),
                "group_type": row.get("group_type"),
                "group_value": row.get("group_value"),
                "sample_count": int(row.get("sample_count") or 0),
                "matured_count": int(row.get("matured_count") or 0),
                "avg_avoided_loss_return_after_cost": (
                    None
                    if pd.isna(row.get("avg_avoided_loss_return_after_cost"))
                    else float(row.get("avg_avoided_loss_return_after_cost"))
                ),
                "avg_excess_avoided_loss_return_after_cost": (
                    None
                    if pd.isna(row.get("avg_excess_avoided_loss_return_after_cost"))
                    else float(row.get("avg_excess_avoided_loss_return_after_cost"))
                ),
                "false_positive_rate": (
                    None
                    if pd.isna(row.get("false_positive_rate"))
                    else float(row.get("false_positive_rate"))
                ),
                "excess_false_positive_rate": (
                    None
                    if pd.isna(row.get("excess_false_positive_rate"))
                    else float(row.get("excess_false_positive_rate"))
                ),
                "recommendation": recommendation,
                "priority": priority,
                "authority_scope": "research_only_no_policy_change",
                "broker_execution_allowed": False,
                "policy_auto_promotion_allowed": False,
            }
        )
    rows.sort(
        key=lambda item: (
            int(item["priority"]),
            -int(item["matured_count"]),
            -(item["avg_avoided_loss_return_after_cost"] or -999.0),
            float(item["false_positive_rate"] if item["false_positive_rate"] is not None else 999.0),
            str(item["queue_id"]),
        )
    )
    return {
        "status": "ok",
        "authority_scope": "research_only_no_policy_change",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        **counts,
        "item_count": len(rows),
        "items": rows[: max(1, int(top_n))],
    }


def evaluate_adversarial_reviews(
    *,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    horizons: list[int] | None = None,
    cost_bps: float = DEFAULT_COST_BPS,
    false_positive_return_threshold: float = DEFAULT_FALSE_POSITIVE_RETURN_THRESHOLD,
    min_matured_rows: int = DEFAULT_MIN_MATURED_ROWS,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    effective_horizons = sorted({int(value) for value in (horizons or DEFAULT_HORIZONS) if int(value) > 0})
    reviews = load_review_rows(from_date=from_date, to_date=to_date, symbols=symbols)
    if reviews.empty or not effective_horizons:
        return pd.DataFrame(), pd.DataFrame(), {"review_rows": int(len(reviews)), "matured_rows_by_horizon": {}}
    price_start = reviews["asof_date"].min() - pd.Timedelta(days=5)
    price_end = reviews["asof_date"].max() + pd.Timedelta(days=max(effective_horizons) * 3 + 15)
    prices = load_price_history_for_returns(
        symbols=reviews["symbol"].dropna().astype(str).str.upper().drop_duplicates().tolist(),
        from_date=price_start,
        to_date=price_end,
    )
    dataset = attach_forward_returns(reviews, prices, horizons=effective_horizons)
    benchmark = load_benchmark_history_for_attribution(from_date=price_start, to_date=price_end)
    dataset = attach_benchmark_forward_returns(dataset, benchmark, horizons=effective_horizons)
    evaluated_at = pd.Timestamp.utcnow()
    evaluations = build_evaluation_rows(
        dataset,
        horizons=effective_horizons,
        cost_bps=cost_bps,
        false_positive_return_threshold=false_positive_return_threshold,
        evaluated_at=evaluated_at,
    )
    summary = summarize_evaluations(evaluations, evaluated_at=evaluated_at, min_matured_rows=min_matured_rows)
    meta = {
        "review_rows": int(len(reviews)),
        "price_rows": int(len(prices)),
        "benchmark_rows": int(len(benchmark)),
        "evaluation_rows": int(len(evaluations)),
        "matured_rows_by_horizon": {
            str(horizon): int(evaluations[(evaluations["horizon_days"] == int(horizon)) & (evaluations["matured"] == True)].shape[0])
            for horizon in effective_horizons
        },
        "horizons": effective_horizons,
        "cost_bps": float(cost_bps),
        "false_positive_return_threshold": float(false_positive_return_threshold),
        "min_matured_rows": int(min_matured_rows),
        "authority_scope": "research_only_no_policy_change",
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
        upsert_to_db(
            evaluations,
            EVALUATIONS_TABLE,
            unique_keys=["evaluated_at", "horizon_days", "published_on", "setup_id", "symbol", "unique_id"],
            timescaledb_column="evaluated_at",
        )
    if not summary.empty:
        upsert_to_db(
            summary,
            SUMMARY_TABLE,
            unique_keys=["evaluated_at", "horizon_days", "group_type", "group_value"],
            timescaledb_column="evaluated_at",
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate adversarial review veto/penalty rows against realized Dhan OHLCV forward returns.")
    parser.add_argument("--from-date", type=parse_datetime_arg)
    parser.add_argument("--to-date", type=parse_datetime_arg)
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument("--horizons", nargs="*", type=int, default=DEFAULT_HORIZONS)
    parser.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    parser.add_argument("--false-positive-return-threshold", type=float, default=DEFAULT_FALSE_POSITIVE_RETURN_THRESHOLD)
    parser.add_argument("--min-matured-rows", type=int, default=DEFAULT_MIN_MATURED_ROWS)
    parser.add_argument("--queue-top-n", type=int, default=20)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--ensure-schema-only",
        action="store_true",
        help="Apply evaluator schema migrations and exit without loading review rows or writing evaluation rows.",
    )
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
                    "authority": "research_only_schema_maintenance",
                    "broker_execution_allowed": False,
                    "policy_auto_promotion_allowed": False,
                },
                indent=2,
                ensure_ascii=False,
                default=str,
            )
        )
        return 0
    evaluations, summary, meta = evaluate_adversarial_reviews(
        from_date=pd.Timestamp(args.from_date, tz="UTC") if args.from_date else None,
        to_date=pd.Timestamp(args.to_date, tz="UTC") if args.to_date else None,
        symbols=args.symbols,
        horizons=args.horizons,
        cost_bps=float(args.cost_bps),
        false_positive_return_threshold=float(args.false_positive_return_threshold),
        min_matured_rows=int(args.min_matured_rows),
    )
    if not args.dry_run:
        persist_outputs(evaluations, summary)
    research_queue = build_research_queue(summary, top_n=max(1, int(args.queue_top_n)))
    print(
        json.dumps(
            {
                "status": "ok",
                "evaluations_table": EVALUATIONS_TABLE,
                "summary_table": SUMMARY_TABLE,
                **meta,
                "summary_rows": int(len(summary)),
                "summary_sample": summary.head(20).to_dict(orient="records") if not summary.empty else [],
                "research_queue": research_queue,
                "dry_run": bool(args.dry_run),
            },
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
