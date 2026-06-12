from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from advisory.event_policy import TABLE_NAME as EVENT_POLICY_TABLE
from advisory.fallback_telemetry import record_local_fallback_event
from advisory.technical_threshold_calibration import attach_forward_returns, load_price_history_for_returns
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration
from utils.sync import parse_datetime_arg


EVALUATIONS_TABLE = "advisory_event_policy_evaluations"
SUMMARY_TABLE = "advisory_event_policy_eval_summary"
EVENT_POLICY_EVAL_SCHEMA_MIGRATION_ID = "20260611_advisory_event_policy_evaluator_base"
DEFAULT_HORIZONS = [5, 10, 20]
DEFAULT_RETURN_THRESHOLD = 0.03
DEFAULT_COST_BPS = 25.0
DEFAULT_MIN_MATURED_ROWS = 10
EVENT_POLICY_EVAL_SCHEMA_STATEMENTS = [
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
        event_class TEXT,
        policy_class TEXT,
        action_type TEXT,
        action_status TEXT,
        policy_score DOUBLE PRECISION,
        confidence DOUBLE PRECISION,
        score_bucket TEXT,
        confidence_bucket TEXT,
        entry_date TIMESTAMPTZ,
        exit_date TIMESTAMPTZ,
        entry_close DOUBLE PRECISION,
        exit_close DOUBLE PRECISION,
        forward_return DOUBLE PRECISION,
        forward_return_after_cost DOUBLE PRECISION,
        hit_after_cost BOOLEAN,
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
        avg_forward_return DOUBLE PRECISION,
        median_forward_return DOUBLE PRECISION,
        avg_forward_return_after_cost DOUBLE PRECISION,
        hit_rate_after_cost DOUBLE PRECISION,
        positive_return_rate DOUBLE PRECISION,
        avg_policy_score DOUBLE PRECISION,
        avg_confidence DOUBLE PRECISION,
        sample_start TIMESTAMPTZ,
        sample_end TIMESTAMPTZ,
        recommendation TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (evaluated_at, horizon_days, group_type, group_value)
    )
    """,
]
EVALUATION_NUMERIC_COLUMNS = [
    "policy_score",
    "confidence",
    "entry_close",
    "exit_close",
    "forward_return",
    "forward_return_after_cost",
]
EVALUATION_INT_COLUMNS = ["horizon_days"]
EVALUATION_BOOL_COLUMNS = ["hit_after_cost", "matured"]
EVALUATION_TS_COLUMNS = ["evaluated_at", "published_on", "asof_date", "entry_date", "exit_date", "load_ts"]
SUMMARY_NUMERIC_COLUMNS = [
    "avg_forward_return",
    "median_forward_return",
    "avg_forward_return_after_cost",
    "hit_rate_after_cost",
    "positive_return_rate",
    "avg_policy_score",
    "avg_confidence",
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
    for column in EVALUATION_NUMERIC_COLUMNS:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce")
    for column in EVALUATION_INT_COLUMNS:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce").astype("Int64")
    for column in EVALUATION_BOOL_COLUMNS:
        if column in out.columns:
            out[column] = _coerce_bool_series(out[column])
    for column in EVALUATION_TS_COLUMNS:
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
        migration_id=EVENT_POLICY_EVAL_SCHEMA_MIGRATION_ID,
        description="Create event-policy evaluator output tables.",
        statements=EVENT_POLICY_EVAL_SCHEMA_STATEMENTS,
        metadata={"tables": [EVALUATIONS_TABLE, SUMMARY_TABLE]},
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
            module="advisory.event_policy_evaluator",
            fallback_type="event_policy_evaluator_schema_lookup_failed",
            source=table_name,
            severity="warn",
            reason="Event-policy evaluator treated source table as unavailable because schema lookup failed.",
            error=exc,
        )
        return set()
    return set(df["column_name"].astype(str).tolist()) if not df.empty else set()


def _score_bucket(value: Any) -> str:
    num = pd.to_numeric(value, errors="coerce")
    if pd.isna(num):
        return "unknown"
    score = float(num)
    if score >= 0.75:
        return "score_high"
    if score >= 0.50:
        return "score_medium"
    if score >= 0.25:
        return "score_low"
    return "score_very_low"


def _confidence_bucket(value: Any) -> str:
    num = pd.to_numeric(value, errors="coerce")
    if pd.isna(num):
        return "unknown"
    confidence = float(num)
    if confidence > 1.0:
        confidence = confidence / 100.0
    if confidence >= 0.80:
        return "confidence_high"
    if confidence >= 0.60:
        return "confidence_medium"
    if confidence >= 0.40:
        return "confidence_low"
    return "confidence_very_low"


def _parse_raw_context(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    try:
        if pd.isna(value):
            return {}
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.event_policy_evaluator",
            fallback_type="event_policy_evaluator_raw_context_missing_check_failed",
            source=EVENT_POLICY_TABLE,
            severity="warn",
            reason="Event-policy evaluator could not evaluate missingness for raw_context_json and continued parsing.",
            error=exc,
            metadata={"value_type": type(value).__name__},
        )
    try:
        parsed = json.loads(str(value))
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.event_policy_evaluator",
            fallback_type="event_policy_evaluator_raw_context_parse_failed",
            source=EVENT_POLICY_TABLE,
            severity="warn",
            reason="Event-policy evaluator could not parse raw_context_json; using empty context for grouping.",
            error=exc,
            metadata={"payload_length": len(str(value))},
        )
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _nested_text(payload: dict[str, Any], path: list[str], default: str = "unknown") -> str:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    text = str(current or "").strip()
    return text if text and text.lower() not in {"none", "nan", "null", "<na>"} else default


def _actionability_group_value(raw_context_json: Any, path: list[str]) -> str:
    raw_context = _parse_raw_context(raw_context_json)
    actionability = raw_context.get("actionability")
    if not isinstance(actionability, dict):
        return "unknown"
    return _nested_text(actionability, path)


def load_event_policy_rows(
    *,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
) -> pd.DataFrame:
    available = table_columns(EVENT_POLICY_TABLE)
    if not available:
        return pd.DataFrame()
    required = {"symbol", "published_on", "action_type", "policy_class"}
    if not required.issubset(available):
        missing = sorted(required - available)
        record_local_fallback_event(
            module="advisory.event_policy_evaluator",
            fallback_type="event_policy_evaluator_required_columns_missing",
            source=EVENT_POLICY_TABLE,
            severity="warn",
            reason="Event-policy evaluator returned empty output because required source columns were missing.",
            metadata={"missing_columns": missing},
        )
        return pd.DataFrame()
    wanted = [
        "published_on",
        "asof_date",
        "policy_at",
        "setup_id",
        "symbol",
        "unique_id",
        "event_source",
        "event_class",
        "policy_class",
        "action_type",
        "action_status",
        "policy_score",
        "confidence",
        "raw_context_json",
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
            FROM {EVENT_POLICY_TABLE}
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
            module="advisory.event_policy_evaluator",
            fallback_type="event_policy_evaluator_policy_rows_load_failed",
            source=EVENT_POLICY_TABLE,
            severity="warn",
            reason="Event-policy evaluator returned empty output because policy row loading failed.",
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
    for column in ["setup_id", "unique_id", "event_source", "event_class", "policy_class", "action_type", "action_status"]:
        if column not in df.columns:
            df[column] = None
        df[column] = df[column].astype("string").str.strip().str.upper()
    for column in ["policy_score", "confidence"]:
        if column not in df.columns:
            df[column] = pd.NA
        df[column] = pd.to_numeric(df[column], errors="coerce")
    if "raw_context_json" not in df.columns:
        df["raw_context_json"] = None
    df["score_bucket"] = df["policy_score"].map(_score_bucket)
    df["confidence_bucket"] = df["confidence"].map(_confidence_bucket)
    return df.dropna(subset=["published_on", "asof_date", "symbol"])


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
            return_col = f"forward_return_h{int(horizon)}"
            forward_return = pd.to_numeric(row.get(return_col), errors="coerce")
            matured = bool(pd.notna(forward_return))
            after_cost = None if not matured else float(forward_return) - cost
            rows.append(
                {
                    "evaluated_at": effective_evaluated_at,
                    "horizon_days": int(horizon),
                    "published_on": row.get("published_on"),
                    "asof_date": row.get("asof_date"),
                    "setup_id": row.get("setup_id"),
                    "symbol": row.get("symbol"),
                    "unique_id": row.get("unique_id"),
                    "event_source": row.get("event_source"),
                    "event_class": row.get("event_class"),
                    "policy_class": row.get("policy_class"),
                    "action_type": row.get("action_type"),
                    "action_status": row.get("action_status"),
                    "policy_score": row.get("policy_score"),
                    "confidence": row.get("confidence"),
                    "score_bucket": row.get("score_bucket"),
                    "confidence_bucket": row.get("confidence_bucket"),
                    "entry_date": row.get(f"entry_date_h{int(horizon)}"),
                    "exit_date": row.get(f"exit_date_h{int(horizon)}"),
                    "entry_close": row.get(f"entry_close_h{int(horizon)}"),
                    "exit_close": row.get(f"exit_close_h{int(horizon)}"),
                    "forward_return": None if not matured else float(forward_return),
                    "forward_return_after_cost": after_cost,
                    "hit_after_cost": None if after_cost is None else bool(after_cost >= float(return_threshold)),
                    "matured": matured,
                    "raw_context_json": row.get("raw_context_json"),
                    "load_ts": pd.Timestamp.utcnow(),
                }
            )
    return normalize_evaluation_frame(pd.DataFrame(rows))


def _summary_recommendation(matured_count: int, avg_after_cost: float | None, hit_rate: float | None, min_rows: int) -> str:
    if matured_count < int(min_rows):
        return "insufficient_matured_rows"
    if avg_after_cost is not None and hit_rate is not None and avg_after_cost > 0.0 and hit_rate >= 0.50:
        return "candidate_policy_strengthen"
    if avg_after_cost is not None and hit_rate is not None and avg_after_cost < 0.0 and hit_rate < 0.40:
        return "candidate_policy_tighten_or_downgrade"
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
    if "raw_context_json" in evals.columns:
        evals["source_quality_bucket"] = evals["raw_context_json"].map(
            lambda value: _actionability_group_value(value, ["source_quality", "quality"])
        )
        evals["source_family"] = evals["raw_context_json"].map(
            lambda value: _actionability_group_value(value, ["source_quality", "source_family"])
        )
        evals["source_authority"] = evals["raw_context_json"].map(
            lambda value: _actionability_group_value(value, ["source_quality", "authority"])
        )
        evals["source_confirmation_required"] = evals["raw_context_json"].map(
            lambda value: _actionability_group_value(value, ["source_quality", "confirmation_required"])
        )
        evals["market_scope_type"] = evals["raw_context_json"].map(
            lambda value: _actionability_group_value(value, ["market_scope", "scope_type"])
        )
    group_specs = [
        ("action_type", ["action_type"]),
        ("policy_class", ["policy_class"]),
        ("event_class", ["event_class"]),
        ("score_bucket", ["score_bucket"]),
        ("confidence_bucket", ["confidence_bucket"]),
        ("source_quality", ["source_quality_bucket"]),
        ("source_family", ["source_family"]),
        ("source_authority", ["source_authority"]),
        ("source_confirmation_required", ["source_confirmation_required"]),
        ("market_scope", ["market_scope_type"]),
        ("policy_class_action", ["policy_class", "action_type"]),
        ("action_score_bucket", ["action_type", "score_bucket"]),
    ]
    rows: list[dict[str, Any]] = []
    for horizon, horizon_group in evals.groupby("horizon_days", dropna=False):
        for group_type, columns in group_specs:
            available = [column for column in columns if column in horizon_group.columns]
            if not available:
                continue
            for keys, group in horizon_group.groupby(available, dropna=False):
                key_values = keys if isinstance(keys, tuple) else (keys,)
                group_value = "|".join(str(value) for value in key_values)
                matured = group[group["matured"].fillna(False).astype(bool)]
                returns = pd.to_numeric(matured.get("forward_return"), errors="coerce").dropna()
                after_cost = pd.to_numeric(matured.get("forward_return_after_cost"), errors="coerce").dropna()
                hit = matured["hit_after_cost"].dropna().astype(bool) if "hit_after_cost" in matured.columns else pd.Series(dtype=bool)
                avg_after_cost = float(after_cost.mean()) if not after_cost.empty else None
                hit_rate = float(hit.mean()) if not hit.empty else None
                rows.append(
                    {
                        "evaluated_at": evaluated_at,
                        "horizon_days": int(horizon),
                        "group_type": group_type,
                        "group_value": group_value,
                        "sample_count": int(len(group)),
                        "matured_count": int(len(matured)),
                        "avg_forward_return": float(returns.mean()) if not returns.empty else None,
                        "median_forward_return": float(returns.median()) if not returns.empty else None,
                        "avg_forward_return_after_cost": avg_after_cost,
                        "hit_rate_after_cost": hit_rate,
                        "positive_return_rate": float(returns.gt(0).mean()) if not returns.empty else None,
                        "avg_policy_score": None if group["policy_score"].dropna().empty else float(group["policy_score"].mean()),
                        "avg_confidence": None if group["confidence"].dropna().empty else float(group["confidence"].mean()),
                        "sample_start": matured["asof_date"].min() if not matured.empty else pd.NaT,
                        "sample_end": matured["asof_date"].max() if not matured.empty else pd.NaT,
                        "recommendation": _summary_recommendation(int(len(matured)), avg_after_cost, hit_rate, min_matured_rows),
                        "load_ts": pd.Timestamp.utcnow(),
                    }
                )
    return normalize_summary_frame(pd.DataFrame(rows))


def evaluate_event_policies(
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
    signals = load_event_policy_rows(from_date=from_date, to_date=to_date, symbols=symbols)
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
    evaluated_at = pd.Timestamp.utcnow()
    evaluations = build_evaluation_rows(
        dataset,
        horizons=effective_horizons,
        cost_bps=cost_bps,
        return_threshold=return_threshold,
        evaluated_at=evaluated_at,
    )
    summary = summarize_evaluations(evaluations, evaluated_at=evaluated_at, min_matured_rows=min_matured_rows)
    meta = {
        "signal_rows": int(len(signals)),
        "price_rows": int(len(prices)),
        "evaluation_rows": int(len(evaluations)),
        "matured_rows_by_horizon": {
            str(horizon): int(evaluations[(evaluations["horizon_days"] == int(horizon)) & (evaluations["matured"] == True)].shape[0])
            for horizon in effective_horizons
        },
        "horizons": effective_horizons,
        "return_threshold": float(return_threshold),
        "cost_bps": float(cost_bps),
        "min_matured_rows": int(min_matured_rows),
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
    parser = argparse.ArgumentParser(description="Evaluate event-policy classes against realized Dhan OHLCV forward returns.")
    parser.add_argument("--from-date", type=parse_datetime_arg)
    parser.add_argument("--to-date", type=parse_datetime_arg)
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument("--horizons", nargs="*", type=int, default=DEFAULT_HORIZONS)
    parser.add_argument("--return-threshold", type=float, default=DEFAULT_RETURN_THRESHOLD)
    parser.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    parser.add_argument("--min-matured-rows", type=int, default=DEFAULT_MIN_MATURED_ROWS)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    evaluations, summary, meta = evaluate_event_policies(
        from_date=pd.Timestamp(args.from_date, tz="UTC") if args.from_date else None,
        to_date=pd.Timestamp(args.to_date, tz="UTC") if args.to_date else None,
        symbols=args.symbols,
        horizons=args.horizons,
        return_threshold=float(args.return_threshold),
        cost_bps=float(args.cost_bps),
        min_matured_rows=int(args.min_matured_rows),
    )
    if not args.dry_run:
        persist_outputs(evaluations, summary)
    print(
        json.dumps(
            {
                "status": "ok",
                "evaluations_table": EVALUATIONS_TABLE,
                "summary_table": SUMMARY_TABLE,
                "evaluation_rows": int(len(evaluations)),
                "summary_rows": int(len(summary)),
                "meta": meta,
                "summary_sample": summary.head(20).to_dict(orient="records") if not summary.empty else [],
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
