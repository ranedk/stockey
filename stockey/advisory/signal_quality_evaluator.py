from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from advisory.company_memory_review import TABLE_NAME as COMPANY_MEMORY_TABLE
from advisory.event_evidence_store import BHAVCOPY_EVIDENCE_TABLE
from advisory.fallback_telemetry import record_local_fallback_event
from advisory.event_policy import TABLE_NAME as EVENT_POLICY_TABLE
from advisory.technical_threshold_calibration import (
    attach_forward_returns,
    load_price_history_for_returns,
    load_technical_signal_rows,
)
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration
from utils.sync import parse_datetime_arg


EVALUATIONS_TABLE = "advisory_signal_quality_evaluations"
SUMMARY_TABLE = "advisory_signal_quality_eval_summary"
SIGNAL_QUALITY_SCHEMA_MIGRATION_ID = "20260611_advisory_signal_quality_evaluator_base"
DEFAULT_HORIZONS = [5, 10, 20]
DEFAULT_RETURN_THRESHOLD = 0.03
DEFAULT_COST_BPS = 25.0
DEFAULT_MIN_MATURED_ROWS = 10
DEFAULT_TECHNICAL_MIN = 70.0
DEFAULT_NEAR_TECHNICAL_MIN = 65.0
SIGNAL_QUALITY_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {EVALUATIONS_TABLE} (
        evaluated_at TIMESTAMPTZ NOT NULL,
        horizon_days BIGINT NOT NULL,
        asof_date TIMESTAMPTZ NOT NULL,
        setup_id TEXT,
        symbol TEXT NOT NULL,
        variant TEXT NOT NULL,
        technical_pass BOOLEAN,
        near_technical_pass BOOLEAN,
        event_positive BOOLEAN,
        event_negative BOOLEAN,
        bhavcopy_positive BOOLEAN,
        bhavcopy_negative BOOLEAN,
        company_memory_positive BOOLEAN,
        company_memory_negative BOOLEAN,
        selected BOOLEAN,
        technical_total_score DOUBLE PRECISION,
        technical_state TEXT,
        candidate_state TEXT,
        technical_trigger_type TEXT,
        event_action_type TEXT,
        event_policy_class TEXT,
        event_policy_score DOUBLE PRECISION,
        event_confidence DOUBLE PRECISION,
        bhavcopy_deal_pressure TEXT,
        bhavcopy_evidence_score DOUBLE PRECISION,
        bhavcopy_deal_net_value_inr DOUBLE PRECISION,
        company_memory_signal TEXT,
        company_memory_confidence DOUBLE PRECISION,
        company_memory_conviction_score DOUBLE PRECISION,
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
        UNIQUE (evaluated_at, horizon_days, asof_date, setup_id, symbol, variant)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {SUMMARY_TABLE} (
        evaluated_at TIMESTAMPTZ NOT NULL,
        horizon_days BIGINT NOT NULL,
        variant TEXT NOT NULL,
        sample_count BIGINT,
        selected_count BIGINT,
        matured_count BIGINT,
        selection_rate DOUBLE PRECISION,
        avg_forward_return DOUBLE PRECISION,
        median_forward_return DOUBLE PRECISION,
        avg_forward_return_after_cost DOUBLE PRECISION,
        hit_rate_after_cost DOUBLE PRECISION,
        positive_return_rate DOUBLE PRECISION,
        baseline_avg_forward_return_after_cost DOUBLE PRECISION,
        lift_vs_technical_only DOUBLE PRECISION,
        avg_selected_technical_score DOUBLE PRECISION,
        sample_start TIMESTAMPTZ,
        sample_end TIMESTAMPTZ,
        recommendation TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (evaluated_at, horizon_days, variant)
    )
    """,
]

VARIANTS = [
    "technical_only",
    "technical_plus_event",
    "technical_plus_bhavcopy",
    "technical_plus_company_memory",
    "technical_plus_all",
]

EVALUATION_NUMERIC_COLUMNS = [
    "technical_total_score",
    "event_policy_score",
    "event_confidence",
    "bhavcopy_evidence_score",
    "bhavcopy_deal_net_value_inr",
    "company_memory_confidence",
    "company_memory_conviction_score",
    "entry_close",
    "exit_close",
    "forward_return",
    "forward_return_after_cost",
]
EVALUATION_INT_COLUMNS = ["horizon_days"]
EVALUATION_BOOL_COLUMNS = [
    "technical_pass",
    "near_technical_pass",
    "event_positive",
    "event_negative",
    "bhavcopy_positive",
    "bhavcopy_negative",
    "company_memory_positive",
    "company_memory_negative",
    "selected",
    "matured",
    "hit_after_cost",
]
EVALUATION_TS_COLUMNS = ["evaluated_at", "asof_date", "entry_date", "exit_date", "load_ts"]

SUMMARY_NUMERIC_COLUMNS = [
    "selection_rate",
    "avg_forward_return",
    "median_forward_return",
    "avg_forward_return_after_cost",
    "hit_rate_after_cost",
    "positive_return_rate",
    "baseline_avg_forward_return_after_cost",
    "lift_vs_technical_only",
    "avg_selected_technical_score",
]
SUMMARY_INT_COLUMNS = ["horizon_days", "sample_count", "selected_count", "matured_count"]
SUMMARY_TS_COLUMNS = ["evaluated_at", "sample_start", "sample_end", "load_ts"]


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


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
        _record_signal_quality_fallback(
            fallback_type="signal_quality_json_ready_missing_check_failed",
            source="json_ready",
            reason="Signal-quality evaluator could not evaluate missingness while preparing JSON and kept the original value.",
            error=exc,
            metadata={"value_type": type(value).__name__},
        )
    return value


def _record_signal_quality_fallback(
    *,
    fallback_type: str,
    source: str,
    reason: str,
    error: Exception,
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="advisory.signal_quality_evaluator",
        fallback_type=fallback_type,
        source=source,
        severity="warn",
        reason=reason,
        error=error,
        metadata=metadata or {},
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
            statement_timeout_ms=5000,
        )
    except Exception as exc:
        _record_signal_quality_fallback(
            fallback_type="signal_quality_evaluator_schema_lookup_failed",
            source=table_name,
            reason="Signal-quality evaluator could not inspect source table columns.",
            error=exc,
            metadata={"table_name": table_name},
        )
        return set()
    return set(df["column_name"].astype(str).tolist()) if not df.empty else set()


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
        migration_id=SIGNAL_QUALITY_SCHEMA_MIGRATION_ID,
        description="Create signal-quality evaluator output tables.",
        statements=SIGNAL_QUALITY_SCHEMA_STATEMENTS,
        metadata={"tables": [EVALUATIONS_TABLE, SUMMARY_TABLE]},
    )


def _load_latest_event_policy(
    *,
    from_date: pd.Timestamp,
    to_date: pd.Timestamp,
    symbols: list[str],
    lookback_days: int,
) -> pd.DataFrame:
    columns = table_columns(EVENT_POLICY_TABLE)
    if not columns or not symbols:
        return pd.DataFrame(columns=["symbol", "asof_date"])
    select_exprs = [
        "UPPER(TRIM(symbol)) AS symbol",
        "asof_date",
        "action_type AS event_action_type" if "action_type" in columns else "NULL::text AS event_action_type",
        "policy_class AS event_policy_class" if "policy_class" in columns else "NULL::text AS event_policy_class",
        "policy_score AS event_policy_score" if "policy_score" in columns else "NULL::double precision AS event_policy_score",
        "confidence AS event_confidence" if "confidence" in columns else "NULL::double precision AS event_confidence",
        "event_class AS event_class" if "event_class" in columns else "NULL::text AS event_class",
    ]
    query_params = {
        "from_date": from_date - pd.Timedelta(days=max(0, int(lookback_days))),
        "to_date": to_date,
        "symbols": [str(symbol).upper() for symbol in symbols],
    }
    try:
        df = sql_to_df(
            f"""
            SELECT {', '.join(select_exprs)}
            FROM {EVENT_POLICY_TABLE}
            WHERE asof_date >= %(from_date)s
              AND asof_date <= %(to_date)s
              AND UPPER(TRIM(symbol)) = ANY(%(symbols)s)
            ORDER BY symbol, asof_date
            """,
            params=query_params,
            retries=4,
            statement_timeout_ms=0,
            chunksize=50000,
        )
    except Exception as exc:
        _record_signal_quality_fallback(
            fallback_type="signal_quality_evaluator_event_policy_load_failed",
            source=EVENT_POLICY_TABLE,
            reason="Signal-quality evaluator could not load point-in-time event-policy overlay rows.",
            error=exc,
            metadata={
                "from_date": str(query_params["from_date"]),
                "to_date": str(query_params["to_date"]),
                "symbol_count": len(query_params["symbols"]),
                "lookback_days": int(lookback_days),
            },
        )
        raise
    if df.empty:
        return df
    df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()
    df["asof_date"] = pd.to_datetime(df["asof_date"], utc=True, errors="coerce").dt.normalize()
    for column in ["event_policy_score", "event_confidence"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    return df.dropna(subset=["symbol", "asof_date"])


def _load_latest_bhavcopy(
    *,
    from_date: pd.Timestamp,
    to_date: pd.Timestamp,
    symbols: list[str],
    lookback_days: int,
) -> pd.DataFrame:
    columns = table_columns(BHAVCOPY_EVIDENCE_TABLE)
    if not columns or not symbols:
        return pd.DataFrame(columns=["symbol", "asof_date"])
    select_exprs = [
        "UPPER(TRIM(symbol)) AS symbol",
        "asof_date",
        "deal_pressure AS bhavcopy_deal_pressure" if "deal_pressure" in columns else "NULL::text AS bhavcopy_deal_pressure",
        "evidence_score AS bhavcopy_evidence_score" if "evidence_score" in columns else "NULL::double precision AS bhavcopy_evidence_score",
        "deal_net_value_inr AS bhavcopy_deal_net_value_inr" if "deal_net_value_inr" in columns else "NULL::double precision AS bhavcopy_deal_net_value_inr",
        "evidence_summary AS bhavcopy_evidence_summary" if "evidence_summary" in columns else "NULL::text AS bhavcopy_evidence_summary",
    ]
    query_params = {
        "from_date": from_date - pd.Timedelta(days=max(0, int(lookback_days))),
        "to_date": to_date,
        "symbols": [str(symbol).upper() for symbol in symbols],
    }
    try:
        df = sql_to_df(
            f"""
            SELECT {', '.join(select_exprs)}
            FROM {BHAVCOPY_EVIDENCE_TABLE}
            WHERE asof_date >= %(from_date)s
              AND asof_date <= %(to_date)s
              AND UPPER(TRIM(symbol)) = ANY(%(symbols)s)
            ORDER BY symbol, asof_date
            """,
            params=query_params,
            retries=4,
            statement_timeout_ms=0,
            chunksize=50000,
        )
    except Exception as exc:
        _record_signal_quality_fallback(
            fallback_type="signal_quality_evaluator_bhavcopy_load_failed",
            source=BHAVCOPY_EVIDENCE_TABLE,
            reason="Signal-quality evaluator could not load point-in-time bhavcopy evidence overlay rows.",
            error=exc,
            metadata={
                "from_date": str(query_params["from_date"]),
                "to_date": str(query_params["to_date"]),
                "symbol_count": len(query_params["symbols"]),
                "lookback_days": int(lookback_days),
            },
        )
        raise
    if df.empty:
        return df
    df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()
    df["asof_date"] = pd.to_datetime(df["asof_date"], utc=True, errors="coerce").dt.normalize()
    for column in ["bhavcopy_evidence_score", "bhavcopy_deal_net_value_inr"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    return df.dropna(subset=["symbol", "asof_date"])


def _load_latest_company_memory(
    *,
    from_date: pd.Timestamp,
    to_date: pd.Timestamp,
    symbols: list[str],
    lookback_days: int,
) -> pd.DataFrame:
    columns = table_columns(COMPANY_MEMORY_TABLE)
    if not columns or not symbols:
        return pd.DataFrame(columns=["symbol", "asof_date"])
    select_exprs = [
        "UPPER(TRIM(symbol)) AS symbol",
        "review_date AS asof_date",
        "recommended_signal AS company_memory_signal" if "recommended_signal" in columns else "NULL::text AS company_memory_signal",
        "confidence AS company_memory_confidence" if "confidence" in columns else "NULL::double precision AS company_memory_confidence",
        "conviction_score AS company_memory_conviction_score" if "conviction_score" in columns else "NULL::double precision AS company_memory_conviction_score",
        "summary AS company_memory_summary" if "summary" in columns else "NULL::text AS company_memory_summary",
    ]
    query_params = {
        "from_date": from_date - pd.Timedelta(days=max(0, int(lookback_days))),
        "to_date": to_date,
        "symbols": [str(symbol).upper() for symbol in symbols],
    }
    try:
        df = sql_to_df(
            f"""
            SELECT {', '.join(select_exprs)}
            FROM {COMPANY_MEMORY_TABLE}
            WHERE review_date >= %(from_date)s
              AND review_date <= %(to_date)s
              AND UPPER(TRIM(symbol)) = ANY(%(symbols)s)
            ORDER BY symbol, review_date
            """,
            params=query_params,
            retries=4,
            statement_timeout_ms=0,
            chunksize=50000,
        )
    except Exception as exc:
        _record_signal_quality_fallback(
            fallback_type="signal_quality_evaluator_company_memory_load_failed",
            source=COMPANY_MEMORY_TABLE,
            reason="Signal-quality evaluator could not load point-in-time company-memory overlay rows.",
            error=exc,
            metadata={
                "from_date": str(query_params["from_date"]),
                "to_date": str(query_params["to_date"]),
                "symbol_count": len(query_params["symbols"]),
                "lookback_days": int(lookback_days),
            },
        )
        raise
    if df.empty:
        return df
    df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()
    df["asof_date"] = pd.to_datetime(df["asof_date"], utc=True, errors="coerce").dt.normalize()
    for column in ["company_memory_confidence", "company_memory_conviction_score"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    return df.dropna(subset=["symbol", "asof_date"])


def _merge_latest_asof(base: pd.DataFrame, overlay: pd.DataFrame, *, suffix: str) -> pd.DataFrame:
    if base.empty or overlay.empty:
        return base.copy()
    frames: list[pd.DataFrame] = []
    overlay_cols = [column for column in overlay.columns if column not in {"symbol", "asof_date"}]
    for symbol, group in base.groupby("symbol", dropna=False):
        left = group.sort_values("asof_date").copy()
        right = overlay[overlay["symbol"] == symbol].sort_values("asof_date").copy()
        if right.empty:
            frames.append(left)
            continue
        merged = pd.merge_asof(
            left,
            right[["asof_date", *overlay_cols]],
            on="asof_date",
            direction="backward",
            suffixes=("", f"_{suffix}"),
        )
        frames.append(merged)
    return pd.concat(frames, ignore_index=True, sort=False) if frames else base.copy()


def enrich_technical_signals(
    signals: pd.DataFrame,
    *,
    event_lookback_days: int = 30,
    bhavcopy_lookback_days: int = 30,
    company_memory_lookback_days: int = 180,
) -> pd.DataFrame:
    if signals.empty:
        return signals.copy()
    base = signals.copy()
    base["symbol"] = base["symbol"].astype("string").str.strip().str.upper()
    base["asof_date"] = pd.to_datetime(base["asof_date"], utc=True, errors="coerce").dt.normalize()
    base = base.dropna(subset=["symbol", "asof_date"])
    if base.empty:
        return base

    symbols = base["symbol"].dropna().astype(str).drop_duplicates().tolist()
    from_date = base["asof_date"].min()
    to_date = base["asof_date"].max()
    event_rows = _load_latest_event_policy(from_date=from_date, to_date=to_date, symbols=symbols, lookback_days=event_lookback_days)
    bhavcopy_rows = _load_latest_bhavcopy(from_date=from_date, to_date=to_date, symbols=symbols, lookback_days=bhavcopy_lookback_days)
    memory_rows = _load_latest_company_memory(from_date=from_date, to_date=to_date, symbols=symbols, lookback_days=company_memory_lookback_days)

    out = _merge_latest_asof(base, event_rows, suffix="event")
    out = _merge_latest_asof(out, bhavcopy_rows, suffix="bhavcopy")
    out = _merge_latest_asof(out, memory_rows, suffix="memory")
    for column in [
        "event_policy_score",
        "event_confidence",
        "bhavcopy_evidence_score",
        "bhavcopy_deal_net_value_inr",
        "company_memory_confidence",
        "company_memory_conviction_score",
    ]:
        if column not in out.columns:
            out[column] = pd.NA
        out[column] = pd.to_numeric(out[column], errors="coerce")
    for column in [
        "event_action_type",
        "event_policy_class",
        "event_class",
        "bhavcopy_deal_pressure",
        "bhavcopy_evidence_summary",
        "company_memory_signal",
        "company_memory_summary",
    ]:
        if column not in out.columns:
            out[column] = None
    return out


def _clean_text(value: Any) -> str:
    try:
        if pd.isna(value):
            return ""
    except Exception as exc:
        _record_signal_quality_fallback(
            fallback_type="signal_quality_clean_text_missing_check_failed",
            source="clean_text",
            reason="Signal-quality evaluator could not evaluate missingness while normalizing text and kept string conversion fallback.",
            error=exc,
            metadata={"value_type": type(value).__name__},
        )
    return str(value or "").strip().upper()


def _technical_flags(row: pd.Series, *, technical_min: float, near_technical_min: float) -> tuple[bool, bool]:
    total = pd.to_numeric(row.get("technical_total_score"), errors="coerce")
    state = _clean_text(row.get("technical_state"))
    candidate_state = _clean_text(row.get("candidate_state"))
    trigger = _clean_text(row.get("technical_trigger_type"))
    score = 0.0 if pd.isna(total) else float(total)
    technical_pass = score >= float(technical_min) or state == "BUY_TRIGGERED" or candidate_state in {"READY", "BUY_TRIGGERED", "PASS_NOW"}
    near_pass = score >= float(near_technical_min) or technical_pass or bool(trigger)
    return bool(technical_pass), bool(near_pass)


def _overlay_flags(row: pd.Series) -> dict[str, bool]:
    action = _clean_text(row.get("event_action_type"))
    policy_class = _clean_text(row.get("event_policy_class"))
    event_score = pd.to_numeric(row.get("event_policy_score"), errors="coerce")
    event_score_value = 0.0 if pd.isna(event_score) else float(event_score)
    event_positive = action in {"BUY_WATCH", "WATCH", "BUY", "BUY_MORE"} or policy_class in {"POSITIVE", "BUY_WATCH"} or event_score_value >= 0.25
    event_negative = action in {"REDUCE_EXPOSURE_REVIEW", "SELL", "SELL_PARTIAL"} or policy_class in {"NEGATIVE", "RISK"} or event_score_value <= -0.25

    pressure = _clean_text(row.get("bhavcopy_deal_pressure"))
    bhav_score = pd.to_numeric(row.get("bhavcopy_evidence_score"), errors="coerce")
    bhav_score_value = 0.0 if pd.isna(bhav_score) else float(bhav_score)
    bhav_positive = pressure == "ACCUMULATION" or bhav_score_value >= 0.15
    bhav_negative = pressure in {"DISTRIBUTION_OR_PRESSURE", "CIRCUIT_RISK"} or bhav_score_value <= -0.15

    memory_signal = _clean_text(row.get("company_memory_signal"))
    memory_conf = pd.to_numeric(row.get("company_memory_confidence"), errors="coerce")
    memory_conf_value = 0.0 if pd.isna(memory_conf) else float(memory_conf)
    memory_positive = memory_signal in {"BUY", "BUY_MORE", "WATCH", "HOLD"} and memory_conf_value >= 0.50
    memory_negative = memory_signal in {"SELL", "SELL_PARTIAL", "NO_ACTION"} and memory_conf_value >= 0.50
    return {
        "event_positive": bool(event_positive),
        "event_negative": bool(event_negative),
        "bhavcopy_positive": bool(bhav_positive),
        "bhavcopy_negative": bool(bhav_negative),
        "company_memory_positive": bool(memory_positive),
        "company_memory_negative": bool(memory_negative),
    }


def _variant_selected(variant: str, *, technical_pass: bool, near_pass: bool, flags: dict[str, bool]) -> bool:
    if variant == "technical_only":
        return bool(technical_pass)
    if variant == "technical_plus_event":
        return bool((technical_pass and not flags["event_negative"]) or (near_pass and flags["event_positive"]))
    if variant == "technical_plus_bhavcopy":
        return bool((technical_pass and not flags["bhavcopy_negative"]) or (near_pass and flags["bhavcopy_positive"]))
    if variant == "technical_plus_company_memory":
        return bool((technical_pass and not flags["company_memory_negative"]) or (near_pass and flags["company_memory_positive"]))
    if variant == "technical_plus_all":
        positives = int(flags["event_positive"]) + int(flags["bhavcopy_positive"]) + int(flags["company_memory_positive"])
        negatives = flags["event_negative"] or flags["bhavcopy_negative"] or flags["company_memory_negative"]
        return bool((technical_pass and not negatives) or (near_pass and positives >= 2 and not negatives))
    return False


def build_evaluation_rows(
    dataset: pd.DataFrame,
    *,
    horizons: list[int],
    cost_bps: float = DEFAULT_COST_BPS,
    return_threshold: float = DEFAULT_RETURN_THRESHOLD,
    technical_min: float = DEFAULT_TECHNICAL_MIN,
    near_technical_min: float = DEFAULT_NEAR_TECHNICAL_MIN,
    evaluated_at: pd.Timestamp | None = None,
) -> pd.DataFrame:
    if dataset.empty:
        return pd.DataFrame()
    effective_evaluated_at = pd.to_datetime(evaluated_at or pd.Timestamp.utcnow(), utc=True, errors="coerce")
    cost = float(cost_bps) / 10000.0
    rows: list[dict[str, Any]] = []
    for _, row in dataset.iterrows():
        technical_pass, near_pass = _technical_flags(row, technical_min=technical_min, near_technical_min=near_technical_min)
        flags = _overlay_flags(row)
        context = {
            "event": {
                "action_type": _json_ready(row.get("event_action_type")),
                "policy_class": _json_ready(row.get("event_policy_class")),
                "event_class": _json_ready(row.get("event_class")),
                "policy_score": _json_ready(row.get("event_policy_score")),
            },
            "bhavcopy": {
                "deal_pressure": _json_ready(row.get("bhavcopy_deal_pressure")),
                "evidence_score": _json_ready(row.get("bhavcopy_evidence_score")),
                "summary": _json_ready(row.get("bhavcopy_evidence_summary")),
            },
            "company_memory": {
                "signal": _json_ready(row.get("company_memory_signal")),
                "confidence": _json_ready(row.get("company_memory_confidence")),
                "summary": _json_ready(row.get("company_memory_summary")),
            },
        }
        for horizon in horizons:
            return_col = f"forward_return_h{int(horizon)}"
            forward_return = pd.to_numeric(row.get(return_col), errors="coerce")
            matured = bool(pd.notna(forward_return))
            after_cost = None if not matured else float(forward_return) - cost
            for variant in VARIANTS:
                selected = _variant_selected(variant, technical_pass=technical_pass, near_pass=near_pass, flags=flags)
                rows.append(
                    {
                        "evaluated_at": effective_evaluated_at,
                        "horizon_days": int(horizon),
                        "asof_date": row.get("asof_date"),
                        "setup_id": row.get("setup_id"),
                        "symbol": row.get("symbol"),
                        "variant": variant,
                        "technical_pass": technical_pass,
                        "near_technical_pass": near_pass,
                        **flags,
                        "selected": selected,
                        "technical_total_score": row.get("technical_total_score"),
                        "technical_state": row.get("technical_state"),
                        "candidate_state": row.get("candidate_state"),
                        "technical_trigger_type": row.get("technical_trigger_type"),
                        "event_action_type": row.get("event_action_type"),
                        "event_policy_class": row.get("event_policy_class"),
                        "event_policy_score": row.get("event_policy_score"),
                        "event_confidence": row.get("event_confidence"),
                        "bhavcopy_deal_pressure": row.get("bhavcopy_deal_pressure"),
                        "bhavcopy_evidence_score": row.get("bhavcopy_evidence_score"),
                        "bhavcopy_deal_net_value_inr": row.get("bhavcopy_deal_net_value_inr"),
                        "company_memory_signal": row.get("company_memory_signal"),
                        "company_memory_confidence": row.get("company_memory_confidence"),
                        "company_memory_conviction_score": row.get("company_memory_conviction_score"),
                        "entry_date": row.get(f"entry_date_h{int(horizon)}"),
                        "exit_date": row.get(f"exit_date_h{int(horizon)}"),
                        "entry_close": row.get(f"entry_close_h{int(horizon)}"),
                        "exit_close": row.get(f"exit_close_h{int(horizon)}"),
                        "forward_return": None if not matured else float(forward_return),
                        "forward_return_after_cost": after_cost,
                        "hit_after_cost": None if after_cost is None else bool(after_cost >= float(return_threshold)),
                        "matured": matured,
                        "raw_context_json": json_dumps(context),
                        "load_ts": pd.Timestamp.utcnow(),
                    }
                )
    return normalize_evaluation_frame(pd.DataFrame(rows))


def _summary_recommendation(
    *,
    variant: str,
    matured_count: int,
    avg_after_cost: float | None,
    hit_rate: float | None,
    lift: float | None,
    min_rows: int,
) -> str:
    if matured_count < int(min_rows):
        return "insufficient_matured_rows"
    if variant == "technical_only":
        return "baseline"
    if lift is not None and avg_after_cost is not None and hit_rate is not None and lift > 0.01 and avg_after_cost > 0 and hit_rate >= 0.50:
        return "candidate_overlay_improves"
    if lift is not None and lift < -0.01:
        return "candidate_overlay_worse"
    return "monitor"


def summarize_evaluations(
    evaluations: pd.DataFrame,
    *,
    evaluated_at: pd.Timestamp,
    min_matured_rows: int = DEFAULT_MIN_MATURED_ROWS,
) -> pd.DataFrame:
    if evaluations.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for horizon, horizon_group in evaluations.groupby("horizon_days", dropna=False):
        baseline_selected = horizon_group[
            (horizon_group["variant"] == "technical_only")
            & horizon_group["selected"].fillna(False).astype(bool)
            & horizon_group["matured"].fillna(False).astype(bool)
        ]
        baseline_after_cost = pd.to_numeric(baseline_selected.get("forward_return_after_cost"), errors="coerce").dropna()
        baseline_avg = float(baseline_after_cost.mean()) if not baseline_after_cost.empty else None
        for variant, group in horizon_group.groupby("variant", dropna=False):
            selected = group[group["selected"].fillna(False).astype(bool)]
            matured = selected[selected["matured"].fillna(False).astype(bool)]
            returns = pd.to_numeric(matured.get("forward_return"), errors="coerce").dropna()
            after_cost = pd.to_numeric(matured.get("forward_return_after_cost"), errors="coerce").dropna()
            hit = matured["hit_after_cost"].dropna().astype(bool) if "hit_after_cost" in matured.columns else pd.Series(dtype=bool)
            avg_after_cost = float(after_cost.mean()) if not after_cost.empty else None
            lift = None if baseline_avg is None or avg_after_cost is None else float(avg_after_cost - baseline_avg)
            hit_rate = float(hit.mean()) if not hit.empty else None
            rows.append(
                {
                    "evaluated_at": evaluated_at,
                    "horizon_days": int(horizon),
                    "variant": str(variant),
                    "sample_count": int(len(group)),
                    "selected_count": int(len(selected)),
                    "matured_count": int(len(matured)),
                    "selection_rate": None if len(group) == 0 else float(len(selected) / len(group)),
                    "avg_forward_return": float(returns.mean()) if not returns.empty else None,
                    "median_forward_return": float(returns.median()) if not returns.empty else None,
                    "avg_forward_return_after_cost": avg_after_cost,
                    "hit_rate_after_cost": hit_rate,
                    "positive_return_rate": float(returns.gt(0).mean()) if not returns.empty else None,
                    "baseline_avg_forward_return_after_cost": baseline_avg,
                    "lift_vs_technical_only": lift,
                    "avg_selected_technical_score": (
                        None
                        if selected["technical_total_score"].dropna().empty
                        else float(pd.to_numeric(selected["technical_total_score"], errors="coerce").mean())
                    ),
                    "sample_start": matured["asof_date"].min() if not matured.empty else pd.NaT,
                    "sample_end": matured["asof_date"].max() if not matured.empty else pd.NaT,
                    "recommendation": _summary_recommendation(
                        variant=str(variant),
                        matured_count=int(len(matured)),
                        avg_after_cost=avg_after_cost,
                        hit_rate=hit_rate,
                        lift=lift,
                        min_rows=min_matured_rows,
                    ),
                    "load_ts": pd.Timestamp.utcnow(),
                }
            )
    return normalize_summary_frame(pd.DataFrame(rows))


def evaluate_signal_quality(
    *,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    horizons: list[int] | None = None,
    return_threshold: float = DEFAULT_RETURN_THRESHOLD,
    cost_bps: float = DEFAULT_COST_BPS,
    min_matured_rows: int = DEFAULT_MIN_MATURED_ROWS,
    technical_min: float = DEFAULT_TECHNICAL_MIN,
    near_technical_min: float = DEFAULT_NEAR_TECHNICAL_MIN,
    event_lookback_days: int = 30,
    bhavcopy_lookback_days: int = 30,
    company_memory_lookback_days: int = 180,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    effective_horizons = sorted({int(value) for value in (horizons or DEFAULT_HORIZONS) if int(value) > 0})
    signals = load_technical_signal_rows(from_date=from_date, to_date=to_date, symbols=symbols)
    if signals.empty or not effective_horizons:
        return pd.DataFrame(), pd.DataFrame(), {"signal_rows": int(len(signals)), "matured_rows_by_horizon": {}}
    enriched = enrich_technical_signals(
        signals,
        event_lookback_days=event_lookback_days,
        bhavcopy_lookback_days=bhavcopy_lookback_days,
        company_memory_lookback_days=company_memory_lookback_days,
    )
    price_start = enriched["asof_date"].min() - pd.Timedelta(days=5)
    price_end = enriched["asof_date"].max() + pd.Timedelta(days=max(effective_horizons) * 3 + 15)
    prices = load_price_history_for_returns(
        symbols=enriched["symbol"].dropna().astype(str).str.upper().drop_duplicates().tolist(),
        from_date=price_start,
        to_date=price_end,
    )
    dataset = attach_forward_returns(enriched, prices, horizons=effective_horizons)
    evaluated_at = pd.Timestamp.utcnow()
    evaluations = build_evaluation_rows(
        dataset,
        horizons=effective_horizons,
        cost_bps=cost_bps,
        return_threshold=return_threshold,
        technical_min=technical_min,
        near_technical_min=near_technical_min,
        evaluated_at=evaluated_at,
    )
    summary = summarize_evaluations(evaluations, evaluated_at=evaluated_at, min_matured_rows=min_matured_rows)
    meta = {
        "signal_rows": int(len(signals)),
        "enriched_rows": int(len(enriched)),
        "price_rows": int(len(prices)),
        "evaluation_rows": int(len(evaluations)),
        "summary_rows": int(len(summary)),
        "overlay_coverage": {
            "event_policy_rows": int(enriched["event_action_type"].notna().sum()) if "event_action_type" in enriched.columns else 0,
            "bhavcopy_rows": int(enriched["bhavcopy_deal_pressure"].notna().sum()) if "bhavcopy_deal_pressure" in enriched.columns else 0,
            "company_memory_rows": int(enriched["company_memory_signal"].notna().sum()) if "company_memory_signal" in enriched.columns else 0,
        },
        "matured_rows_by_horizon": {
            str(horizon): int(evaluations[(evaluations["horizon_days"] == int(horizon)) & (evaluations["matured"] == True)].shape[0])
            for horizon in effective_horizons
        },
        "selected_rows_by_variant": evaluations.groupby("variant")["selected"].sum().to_dict() if not evaluations.empty else {},
        "horizons": effective_horizons,
        "return_threshold": float(return_threshold),
        "cost_bps": float(cost_bps),
        "technical_min": float(technical_min),
        "near_technical_min": float(near_technical_min),
        "event_lookback_days": int(event_lookback_days),
        "bhavcopy_lookback_days": int(bhavcopy_lookback_days),
        "company_memory_lookback_days": int(company_memory_lookback_days),
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
            unique_keys=["evaluated_at", "horizon_days", "asof_date", "setup_id", "symbol", "variant"],
        )
    if not summary.empty:
        upsert_to_db(
            summary,
            SUMMARY_TABLE,
            unique_keys=["evaluated_at", "horizon_days", "variant"],
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare realized outcomes for technical-only signals versus technical signals enriched with event, bhavcopy, and company-memory overlays."
    )
    parser.add_argument("--from-date", type=parse_datetime_arg)
    parser.add_argument("--to-date", type=parse_datetime_arg)
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument("--horizons", nargs="*", type=int, default=DEFAULT_HORIZONS)
    parser.add_argument("--return-threshold", type=float, default=DEFAULT_RETURN_THRESHOLD)
    parser.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    parser.add_argument("--min-matured-rows", type=int, default=DEFAULT_MIN_MATURED_ROWS)
    parser.add_argument("--technical-min", type=float, default=DEFAULT_TECHNICAL_MIN)
    parser.add_argument("--near-technical-min", type=float, default=DEFAULT_NEAR_TECHNICAL_MIN)
    parser.add_argument("--event-lookback-days", type=int, default=30)
    parser.add_argument("--bhavcopy-lookback-days", type=int, default=30)
    parser.add_argument("--company-memory-lookback-days", type=int, default=180)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def _arg_timestamp(value: Any) -> pd.Timestamp | None:
    if value is None:
        return None
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.normalize()


def main() -> int:
    args = parse_args()
    evaluations, summary, meta = evaluate_signal_quality(
        from_date=_arg_timestamp(args.from_date),
        to_date=_arg_timestamp(args.to_date),
        symbols=args.symbols,
        horizons=args.horizons,
        return_threshold=float(args.return_threshold),
        cost_bps=float(args.cost_bps),
        min_matured_rows=int(args.min_matured_rows),
        technical_min=float(args.technical_min),
        near_technical_min=float(args.near_technical_min),
        event_lookback_days=max(0, int(args.event_lookback_days)),
        bhavcopy_lookback_days=max(0, int(args.bhavcopy_lookback_days)),
        company_memory_lookback_days=max(0, int(args.company_memory_lookback_days)),
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
                "summary_sample": summary.head(10).to_dict(orient="records") if not summary.empty else [],
                "dry_run": bool(args.dry_run),
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
