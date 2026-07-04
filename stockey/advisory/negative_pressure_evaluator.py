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


EVALUATIONS_TABLE = "advisory_negative_pressure_evaluations"
SUMMARY_TABLE = "advisory_negative_pressure_eval_summary"
SCHEMA_MIGRATION_ID = "20260620_advisory_negative_pressure_evaluator"
CONTEXT_CLASS_SCHEMA_MIGRATION_ID = "20260621_advisory_negative_pressure_evaluator_context_class"
BENCHMARK_ATTRIBUTION_SCHEMA_MIGRATION_ID = "20260621_advisory_negative_pressure_evaluator_benchmark_attribution"
SECTOR_DIAGNOSTICS_SCHEMA_MIGRATION_ID = "20260621_advisory_negative_pressure_evaluator_sector_diagnostics"
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
        confidence DOUBLE PRECISION,
        previous_action TEXT,
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
        avoided_return_after_cost DOUBLE PRECISION,
        excess_avoided_return_after_cost DOUBLE PRECISION,
        protective_hit_after_cost BOOLEAN,
        excess_protective_hit_after_cost BOOLEAN,
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
        sample_count BIGINT,
        matured_count BIGINT,
        symbol_count BIGINT,
        avg_forward_return DOUBLE PRECISION,
        avg_benchmark_forward_return DOUBLE PRECISION,
        avg_avoided_return_after_cost DOUBLE PRECISION,
        avg_excess_avoided_return_after_cost DOUBLE PRECISION,
        protective_hit_rate_after_cost DOUBLE PRECISION,
        excess_protective_hit_rate_after_cost DOUBLE PRECISION,
        false_positive_rate_after_cost DOUBLE PRECISION,
        false_positive_excess_rate_after_cost DOUBLE PRECISION,
        avg_confidence DOUBLE PRECISION,
        sample_start TIMESTAMPTZ,
        sample_end TIMESTAMPTZ,
        classification TEXT,
        recommendation TEXT,
        authority TEXT,
        broker_execution_allowed BOOLEAN,
        policy_auto_promotion_allowed BOOLEAN,
        load_ts TIMESTAMPTZ,
        UNIQUE (evaluated_at, horizon_days, signal_source, effect_type, source_context, context_class)
    )
    """,
]

CONTEXT_CLASS_SCHEMA_STATEMENTS = [
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS context_class TEXT",
    f"ALTER TABLE {SUMMARY_TABLE} ADD COLUMN IF NOT EXISTS context_class TEXT",
]

BENCHMARK_ATTRIBUTION_SCHEMA_STATEMENTS = [
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS benchmark_name TEXT",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS benchmark_entry_date TIMESTAMPTZ",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS benchmark_exit_date TIMESTAMPTZ",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS benchmark_entry_close DOUBLE PRECISION",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS benchmark_exit_close DOUBLE PRECISION",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS benchmark_forward_return DOUBLE PRECISION",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS excess_avoided_return_after_cost DOUBLE PRECISION",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS excess_protective_hit_after_cost BOOLEAN",
    f"ALTER TABLE {SUMMARY_TABLE} ADD COLUMN IF NOT EXISTS avg_benchmark_forward_return DOUBLE PRECISION",
    f"ALTER TABLE {SUMMARY_TABLE} ADD COLUMN IF NOT EXISTS avg_excess_avoided_return_after_cost DOUBLE PRECISION",
    f"ALTER TABLE {SUMMARY_TABLE} ADD COLUMN IF NOT EXISTS excess_protective_hit_rate_after_cost DOUBLE PRECISION",
    f"ALTER TABLE {SUMMARY_TABLE} ADD COLUMN IF NOT EXISTS false_positive_excess_rate_after_cost DOUBLE PRECISION",
]

SECTOR_DIAGNOSTICS_SCHEMA_STATEMENTS = [
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS sector_name TEXT",
    f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS sector_code TEXT",
    f"ALTER TABLE {SUMMARY_TABLE} ADD COLUMN IF NOT EXISTS sector_diagnostics_json TEXT",
]


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
        _record_negative_pressure_fallback(
            fallback_type="negative_pressure_json_ready_missing_check_failed",
            source="json_ready",
            reason="Negative-pressure evaluator could not evaluate missingness while preparing JSON output.",
            error=exc,
            metadata={"value_type": type(value).__name__},
        )
    return value


def _record_negative_pressure_fallback(
    *,
    fallback_type: str,
    source: str,
    reason: str,
    error: Exception,
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="advisory.negative_pressure_evaluator",
        fallback_type=fallback_type,
        source=source,
        severity="warn",
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def _parse_jsonish(value: Any, default: Any, *, source: str) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        if pd.isna(value):
            return default
    except Exception as exc:
        _record_negative_pressure_fallback(
            fallback_type="negative_pressure_json_missing_check_failed",
            source=source,
            reason="Negative-pressure evaluator could not evaluate missingness while parsing JSON.",
            error=exc,
            metadata={"value_type": type(value).__name__, "default_type": type(default).__name__},
        )
    try:
        return json.loads(str(value))
    except Exception as exc:
        if str(value or "").strip():
            _record_negative_pressure_fallback(
                fallback_type="negative_pressure_json_parse_failed",
                source=source,
                reason="Negative-pressure evaluator could not parse JSON payload; using default.",
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
    if isinstance(payload.get("theme_context"), dict):
        return "theme_context"
    if isinstance(payload.get("direct_context"), dict):
        direct = payload["direct_context"]
        return str(direct.get("context_source") or "").strip().lower() or "direct_context"
    return None


def _context_class(row: pd.Series) -> str | None:
    payload = _parse_jsonish(row.get("action_payload_json"), {}, source="signal_refresh.action_payload_json")
    if not isinstance(payload, dict):
        return None
    explicit = str(payload.get("context_class") or "").strip().upper()
    if explicit:
        return explicit
    direct = payload.get("direct_context")
    if isinstance(direct, dict):
        return str(direct.get("context_class") or "").strip().upper() or None
    theme = payload.get("theme_context")
    if isinstance(theme, dict):
        return str(theme.get("theme_id") or theme.get("theme_name") or "").strip().upper() or None
    watch = payload.get("context_watch")
    if isinstance(watch, dict):
        return str(watch.get("context_class") or "").strip().upper() or None
    return None


def _sector_context(row: pd.Series) -> tuple[str | None, str | None]:
    payload = _parse_jsonish(row.get("action_payload_json"), {}, source="signal_refresh.action_payload_json")
    if not isinstance(payload, dict):
        return None, None
    candidates: list[dict[str, Any]] = [payload]
    for key in ["direct_context", "theme_context", "context_watch"]:
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


def ensure_tables() -> None:
    apply_schema_migration(
        migration_id=SCHEMA_MIGRATION_ID,
        description="Create research-only negative-pressure outcome evaluator tables.",
        statements=SCHEMA_STATEMENTS,
        metadata={
            "tables": [EVALUATIONS_TABLE, SUMMARY_TABLE],
            "workflow": "negative_pressure_evaluator",
            "authority": "research_only",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
        },
    )
    apply_schema_migration(
        migration_id=CONTEXT_CLASS_SCHEMA_MIGRATION_ID,
        description="Add context class dimension to negative-pressure evaluator outputs.",
        statements=CONTEXT_CLASS_SCHEMA_STATEMENTS,
        metadata={
            "tables": [EVALUATIONS_TABLE, SUMMARY_TABLE],
            "workflow": "negative_pressure_evaluator",
            "authority": "research_only",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
        },
    )
    apply_schema_migration(
        migration_id=BENCHMARK_ATTRIBUTION_SCHEMA_MIGRATION_ID,
        description="Add benchmark attribution to negative-pressure evaluator outputs.",
        statements=BENCHMARK_ATTRIBUTION_SCHEMA_STATEMENTS,
        metadata={
            "tables": [EVALUATIONS_TABLE, SUMMARY_TABLE],
            "workflow": "negative_pressure_evaluator",
            "authority": "research_only",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
            "attribution": "benchmark_excess_return",
        },
    )
    apply_schema_migration(
        migration_id=SECTOR_DIAGNOSTICS_SCHEMA_MIGRATION_ID,
        description="Add compact sector diagnostics to negative-pressure evaluator outputs.",
        statements=SECTOR_DIAGNOSTICS_SCHEMA_STATEMENTS,
        metadata={
            "tables": [EVALUATIONS_TABLE, SUMMARY_TABLE],
            "workflow": "negative_pressure_evaluator",
            "authority": "research_only",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
            "dimension": "sector",
        },
    )


def load_negative_pressure_signals(
    *,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
) -> pd.DataFrame:
    clauses = [
        "signal_action = 'REDUCE_EXPOSURE_REVIEW'",
        "COALESCE(dry_run, false) IS FALSE",
        "COALESCE(broker_execution_allowed, false) IS FALSE",
        "effect_type IN ('direct_context_derisk_pressure', 'theme_context_derisk_pressure')",
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
                effect_type,
                confidence,
                previous_action,
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
        _record_negative_pressure_fallback(
            fallback_type="negative_pressure_signal_rows_load_failed",
            source=SIGNAL_REFRESH_TABLE,
            reason="Negative-pressure evaluator could not load review-only signal-refresh de-risk rows.",
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
            avoided = None if forward_return is None else float(-forward_return - cost)
            excess_avoided = (
                None
                if forward_return is None or benchmark_forward_return is None
                else float(benchmark_forward_return - forward_return - cost)
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
                    "confidence": row.get("confidence"),
                    "previous_action": row.get("previous_action"),
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
                    "avoided_return_after_cost": avoided,
                    "excess_avoided_return_after_cost": excess_avoided,
                    "protective_hit_after_cost": None if avoided is None else bool(avoided >= float(return_threshold)),
                    "excess_protective_hit_after_cost": None if excess_avoided is None else bool(excess_avoided >= float(return_threshold)),
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
                            "label_interpretation": "For negative pressure, positive avoided_return_after_cost means exiting/reducing would have helped versus holding. Positive excess_avoided_return_after_cost means the target also underperformed the benchmark after costs.",
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
    avg_avoided = _number(row.get("avg_avoided_return_after_cost"))
    avg_excess_avoided = _number(row.get("avg_excess_avoided_return_after_cost"))
    hit_rate = _number(row.get("protective_hit_rate_after_cost"))
    excess_hit_rate = _number(row.get("excess_protective_hit_rate_after_cost"))
    false_positive_rate = _number(row.get("false_positive_rate_after_cost"))
    false_positive_excess_rate = _number(row.get("false_positive_excess_rate_after_cost"))
    if matured < int(min_matured_rows):
        return "needs_more_data"
    if (
        avg_avoided is not None
        and avg_avoided > 0
        and avg_excess_avoided is not None
        and avg_excess_avoided > 0
        and hit_rate is not None
        and hit_rate >= 0.45
        and excess_hit_rate is not None
        and excess_hit_rate >= 0.45
    ):
        return "protective_candidate"
    if (
        avg_avoided is not None
        and avg_avoided > 0
        and hit_rate is not None
        and hit_rate >= 0.45
        and (avg_excess_avoided is not None and avg_excess_avoided <= 0)
    ):
        return "benchmark_beta_not_derisk_alpha"
    if (false_positive_rate is not None and false_positive_rate >= 0.60) or (
        false_positive_excess_rate is not None and false_positive_excess_rate >= 0.60
    ):
        return "harmful_false_positive_pressure"
    return "mixed_or_weak"


def _summary_recommendation(classification: str) -> str:
    if classification == "protective_candidate":
        return "eligible_for_review_only_derisk_policy_research"
    if classification == "harmful_false_positive_pressure":
        return "suppress_or_rework_negative_pressure_source_before_policy_influence"
    if classification == "benchmark_beta_not_derisk_alpha":
        return "keep_research_only_collect_excess_derisk_evidence"
    if classification == "mixed_or_weak":
        return "keep_review_only_and_split_by_context_class"
    return "collect_more_matured_negative_pressure_labels"


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
        avoided = pd.to_numeric(matured.get("avoided_return_after_cost"), errors="coerce").dropna()
        excess_avoided = pd.to_numeric(matured.get("excess_avoided_return_after_cost"), errors="coerce").dropna()
        hits = matured.get("protective_hit_after_cost", pd.Series(dtype=bool)).dropna().astype(bool)
        excess_hits = matured.get("excess_protective_hit_after_cost", pd.Series(dtype=bool)).dropna().astype(bool)
        false_positive = avoided.lt(0) if not avoided.empty else pd.Series(dtype=bool)
        false_positive_excess = excess_avoided.lt(0) if not excess_avoided.empty else pd.Series(dtype=bool)
        summary_row = {
            "sector_key": str(sector_key),
            "sector_name": _first_text(matured["sector_name"]) if "sector_name" in matured.columns and not matured.empty else None,
            "sector_code": (_first_text(matured["sector_code"]) or "").upper() or None if "sector_code" in matured.columns and not matured.empty else None,
            "sample_count": int(len(sector_group)),
            "matured_count": int(len(matured)),
            "symbol_count": int(matured["symbol"].nunique()) if not matured.empty else 0,
            "avg_forward_return": None if forward.empty else float(forward.mean()),
            "avg_benchmark_forward_return": None if benchmark_forward.empty else float(benchmark_forward.mean()),
            "avg_avoided_return_after_cost": None if avoided.empty else float(avoided.mean()),
            "avg_excess_avoided_return_after_cost": None if excess_avoided.empty else float(excess_avoided.mean()),
            "protective_hit_rate_after_cost": None if hits.empty else float(hits.mean()),
            "excess_protective_hit_rate_after_cost": None if excess_hits.empty else float(excess_hits.mean()),
            "false_positive_rate_after_cost": None if false_positive.empty else float(false_positive.mean()),
            "false_positive_excess_rate_after_cost": None if false_positive_excess.empty else float(false_positive_excess.mean()),
            "authority": "research_only",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
        }
        summary_row["classification"] = _summary_classification(summary_row, min_matured_rows=min_matured_rows)
        summary_row["recommendation"] = _summary_recommendation(summary_row["classification"])
        diagnostics.append(summary_row)
    diagnostics.sort(
        key=lambda item: (
            item.get("classification") == "protective_candidate",
            item.get("classification") == "harmful_false_positive_pressure",
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
    group_cols = ["evaluated_at", "horizon_days", "signal_source", "effect_type", "source_context", "context_class"]
    for keys, group in frame.groupby(group_cols, dropna=False, sort=True):
        evaluated_at, horizon_days, signal_source, effect_type, source_context, context_class = keys
        matured = group[group["matured"].fillna(False).astype(bool)]
        forward = pd.to_numeric(matured.get("forward_return"), errors="coerce").dropna()
        benchmark_forward = pd.to_numeric(matured.get("benchmark_forward_return"), errors="coerce").dropna()
        avoided = pd.to_numeric(matured.get("avoided_return_after_cost"), errors="coerce").dropna()
        excess_avoided = pd.to_numeric(matured.get("excess_avoided_return_after_cost"), errors="coerce").dropna()
        hits = matured.get("protective_hit_after_cost", pd.Series(dtype=bool)).dropna().astype(bool)
        excess_hits = matured.get("excess_protective_hit_after_cost", pd.Series(dtype=bool)).dropna().astype(bool)
        false_positive = avoided.lt(0) if not avoided.empty else pd.Series(dtype=bool)
        false_positive_excess = excess_avoided.lt(0) if not excess_avoided.empty else pd.Series(dtype=bool)
        summary_row = {
            "evaluated_at": evaluated_at,
            "horizon_days": int(horizon_days),
            "signal_source": str(signal_source),
            "effect_type": None if pd.isna(effect_type) else str(effect_type),
            "source_context": None if pd.isna(source_context) else str(source_context),
            "context_class": None if pd.isna(context_class) else str(context_class),
            "sector_diagnostics_json": "[]",
            "sample_count": int(len(group)),
            "matured_count": int(len(matured)),
            "symbol_count": int(matured["symbol"].nunique()) if not matured.empty else 0,
            "avg_forward_return": None if forward.empty else float(forward.mean()),
            "avg_benchmark_forward_return": None if benchmark_forward.empty else float(benchmark_forward.mean()),
            "avg_avoided_return_after_cost": None if avoided.empty else float(avoided.mean()),
            "avg_excess_avoided_return_after_cost": None if excess_avoided.empty else float(excess_avoided.mean()),
            "protective_hit_rate_after_cost": None if hits.empty else float(hits.mean()),
            "excess_protective_hit_rate_after_cost": None if excess_hits.empty else float(excess_hits.mean()),
            "false_positive_rate_after_cost": None if false_positive.empty else float(false_positive.mean()),
            "false_positive_excess_rate_after_cost": None if false_positive_excess.empty else float(false_positive_excess.mean()),
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
    "avoided_return_after_cost",
    "excess_avoided_return_after_cost",
]
EVALUATION_BOOL_COLUMNS = [
    "action_changed",
    "protective_hit_after_cost",
    "excess_protective_hit_after_cost",
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
    "avg_avoided_return_after_cost",
    "avg_excess_avoided_return_after_cost",
    "protective_hit_rate_after_cost",
    "excess_protective_hit_rate_after_cost",
    "false_positive_rate_after_cost",
    "false_positive_excess_rate_after_cost",
    "avg_confidence",
]
SUMMARY_BOOL_COLUMNS = ["broker_execution_allowed", "policy_auto_promotion_allowed"]
SUMMARY_TS_COLUMNS = ["evaluated_at", "sample_start", "sample_end", "load_ts"]


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
    return out


def evaluate_negative_pressure(
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
    signals = load_negative_pressure_signals(from_date=from_date, to_date=to_date, symbols=symbols)
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
            unique_keys=["evaluated_at", "horizon_days", "signal_source", "effect_type", "source_context", "context_class"],
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate whether review-only negative context/de-risk pressure would have protected capital after costs."
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
    evaluations, summary, meta = evaluate_negative_pressure(
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
        print(f"negative_pressure_evaluator rows={len(evaluations)} summary={len(summary)} dry_run={args.dry_run}")
        if not summary.empty:
            print(summary[["horizon_days", "signal_source", "source_context", "context_class", "matured_count", "avg_avoided_return_after_cost", "classification"]].head(20).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
