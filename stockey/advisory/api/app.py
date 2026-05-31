from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from advisory.decision_trace import load_event_trace, load_symbol_trace
from advisory.hypothesis_engine import create_hypothesis, latest_promotion_audit, load_action_plans, load_hypotheses, load_matches, preview_hypothesis_payload, run_hypothesis_scan, run_promotion_audit, update_hypothesis
from advisory.event_policy import TABLE_NAME as EVENT_POLICY_TABLE
from advisory.event_policy_evaluator import SUMMARY_TABLE as EVENT_POLICY_EVAL_SUMMARY_TABLE
from advisory.live_dashboard import DEFAULT_OUTPUT_DIR, build_live_dashboard_payload
from advisory.market_context import load_latest_market_context
from advisory.operator_health import build_operator_health
from advisory.technical_threshold_calibration import EVALUATIONS_TABLE as TECHNICAL_CALIBRATION_EVALUATIONS_TABLE
from advisory.technical_threshold_calibration import SUMMARY_TABLE as TECHNICAL_CALIBRATION_SUMMARY_TABLE
from advisory.technical_threshold_promotion import generate_promotion_review, load_promotion_reviews, record_manual_decision
from utils.db import sql_to_df


def _parse_asof_date(value: str | None) -> pd.Timestamp | None:
    if not value:
        return None
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        raise ValueError(f"Invalid asof_date: {value}")
    return ts.normalize()


def _parse_timestamp(value: str | None) -> pd.Timestamp | None:
    if not value:
        return None
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        raise ValueError(f"Invalid timestamp: {value}")
    return ts


def load_operator_payload(*, asof_date: str | pd.Timestamp | None = None) -> dict[str, Any]:
    parsed_asof = _parse_asof_date(asof_date) if isinstance(asof_date, str) else asof_date
    return build_live_dashboard_payload(asof_date=parsed_asof, output_dir=DEFAULT_OUTPUT_DIR)


def build_health_payload() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "stockey-operator-api",
        "read_only": True,
    }


def build_summary_payload(*, asof_date: str | None = None) -> dict[str, Any]:
    payload = load_operator_payload(asof_date=asof_date)
    return {
        "generated_at": payload.get("generated_at"),
        "asof_date": payload.get("asof_date"),
        "summary": payload.get("summary") or {},
        "runtime_processes": payload.get("runtime_processes") or [],
        "cron_status": payload.get("cron_status") or [],
        "sync_state": payload.get("sync_state") or [],
    }


def build_actions_payload(*, asof_date: str | None = None) -> dict[str, Any]:
    payload = load_operator_payload(asof_date=asof_date)
    return {
        "generated_at": payload.get("generated_at"),
        "asof_date": payload.get("asof_date"),
        "top_action_recommendations": payload.get("top_action_recommendations") or [],
        "action_recommendations": payload.get("action_recommendations") or [],
        "alerts": payload.get("alerts") or [],
    }


def build_portfolio_payload(*, asof_date: str | None = None) -> dict[str, Any]:
    payload = load_operator_payload(asof_date=asof_date)
    return {
        "generated_at": payload.get("generated_at"),
        "asof_date": payload.get("asof_date"),
        "today_recommendations": payload.get("today_recommendations") or [],
        "current_recommendations": payload.get("current_recommendations") or [],
        "exited_recommendations": payload.get("exited_recommendations") or [],
        "portfolio": payload.get("portfolio") or [],
        "lifecycle": payload.get("lifecycle") or [],
    }


def build_watchlist_payload(*, asof_date: str | None = None) -> dict[str, Any]:
    payload = load_operator_payload(asof_date=asof_date)
    return {
        "generated_at": payload.get("generated_at"),
        "asof_date": payload.get("asof_date"),
        "watch_recommendations": payload.get("watch_recommendations") or [],
        "watchlist": payload.get("watchlist") or [],
        "ts_watch_recommendations": payload.get("ts_watch_recommendations") or [],
        "ts_forecast_watch": payload.get("ts_forecast_watch") or [],
        "ts_forecast_eval_summary": payload.get("ts_forecast_eval_summary") or [],
    }


def build_market_context_payload(*, asof_date: str | None = None, limit: int = 50) -> dict[str, Any]:
    parsed_asof = _parse_asof_date(asof_date)
    payload = load_latest_market_context(parsed_asof, limit=max(0, int(limit)))
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "asof_date": asof_date,
        "summary": payload.get("summary") or {},
        "top_universe": payload.get("top_universe") or [],
    }


def _table_exists(table_name: str) -> bool:
    df = sql_to_df(
        """
        SELECT 1 AS exists_flag
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = %s
        LIMIT 1
        """,
        params=(table_name,),
        retries=2,
    )
    return not df.empty


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df.empty:
        return []
    out = df.copy()
    out = out.astype(object).where(pd.notna(out), None)
    return out.to_dict(orient="records")


def build_technical_calibration_payload(*, limit: int = 25) -> dict[str, Any]:
    if not _table_exists(TECHNICAL_CALIBRATION_SUMMARY_TABLE):
        return {
            "generated_at": pd.Timestamp.utcnow().isoformat(),
            "status": "missing_table",
            "summary": [],
            "top_configs": [],
        }
    summary = sql_to_df(
        f"""
        SELECT *
        FROM {TECHNICAL_CALIBRATION_SUMMARY_TABLE}
        WHERE evaluated_at = (
            SELECT MAX(evaluated_at)
            FROM {TECHNICAL_CALIBRATION_SUMMARY_TABLE}
        )
        ORDER BY horizon_days
        """,
        retries=3,
    )
    top_configs = pd.DataFrame()
    if _table_exists(TECHNICAL_CALIBRATION_EVALUATIONS_TABLE):
        top_configs = sql_to_df(
            f"""
            WITH latest AS (
                SELECT MAX(evaluated_at) AS evaluated_at
                FROM {TECHNICAL_CALIBRATION_EVALUATIONS_TABLE}
            ),
            ranked AS (
                SELECT
                    e.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY e.horizon_days
                        ORDER BY e.objective_score DESC NULLS LAST, e.eligible_count DESC NULLS LAST, e.hit_rate_after_cost DESC NULLS LAST
                    ) AS rn
                FROM {TECHNICAL_CALIBRATION_EVALUATIONS_TABLE} e
                JOIN latest ON latest.evaluated_at = e.evaluated_at
            )
            SELECT *
            FROM ranked
            WHERE rn <= %(limit)s
            ORDER BY horizon_days, rn
            """,
            params={"limit": max(1, int(limit))},
            retries=3,
        )
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "status": "ok",
        "summary": _records(summary),
        "top_configs": _records(top_configs),
    }


def build_technical_threshold_promotion_review_payload(payload: dict[str, Any]) -> dict[str, Any]:
    setup_id = str(payload.get("setup_id") or "").strip()
    config_id = str(payload.get("config_id") or "").strip()
    if not setup_id:
        raise ValueError("setup_id is required")
    if not config_id:
        raise ValueError("config_id is required")
    return generate_promotion_review(
        setup_id=setup_id,
        config_id=config_id,
        model=str(payload.get("model") or "") or None,
        use_llm=bool(payload.get("use_llm", True)),
        persist=True,
    )


def build_technical_threshold_reviews_payload(*, limit: int = 25) -> dict[str, Any]:
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "status": "ok",
        "reviews": load_promotion_reviews(limit=limit),
    }


def build_technical_threshold_review_decision_payload(payload: dict[str, Any]) -> dict[str, Any]:
    reviewed_at = payload.get("reviewed_at")
    setup_id = str(payload.get("setup_id") or "").strip()
    config_id = str(payload.get("config_id") or "").strip()
    decision = str(payload.get("decision") or "").strip().lower()
    if not reviewed_at:
        raise ValueError("reviewed_at is required")
    if not setup_id:
        raise ValueError("setup_id is required")
    if not config_id:
        raise ValueError("config_id is required")
    if decision not in {"approved", "rejected", "needs_more_data"}:
        raise ValueError("decision must be approved, rejected, or needs_more_data")
    return record_manual_decision(
        reviewed_at=reviewed_at,
        setup_id=setup_id,
        config_id=config_id,
        decision=decision,  # type: ignore[arg-type]
        operator_id=str(payload.get("operator_id") or "") or None,
        decision_reason=str(payload.get("decision_reason") or "") or None,
    )


def build_events_payload(*, asof_date: str | None = None, limit: int = 100) -> dict[str, Any]:
    payload = load_operator_payload(asof_date=asof_date)
    events = payload.get("watch_events") or []
    return {
        "generated_at": payload.get("generated_at"),
        "asof_date": payload.get("asof_date"),
        "events": events[: max(int(limit), 0)],
        "operator_feed": payload.get("operator_feed") or [],
        "alerts": payload.get("alerts") or [],
    }


def build_event_policy_payload(*, asof_date: str | None = None, action_type: str | None = None, limit: int = 100) -> dict[str, Any]:
    if not _table_exists(EVENT_POLICY_TABLE):
        return {
            "generated_at": pd.Timestamp.utcnow().isoformat(),
            "status": "missing_table",
            "summary": {"action_counts": {}, "policy_class_counts": {}},
            "rows": [],
        }
    clauses = ["1 = 1"]
    params: dict[str, Any] = {"limit": max(1, int(limit))}
    parsed_asof = _parse_asof_date(asof_date)
    if parsed_asof is not None:
        clauses.append("asof_date = %(asof_date)s")
        params["asof_date"] = parsed_asof
    else:
        clauses.append(f"asof_date = (SELECT MAX(asof_date) FROM {EVENT_POLICY_TABLE})")
    normalized_action = str(action_type or "").strip().upper()
    if normalized_action and normalized_action != "ALL":
        clauses.append("action_type = %(action_type)s")
        params["action_type"] = normalized_action
    where_sql = " AND ".join(clauses)
    rows = sql_to_df(
        f"""
        SELECT *
        FROM {EVENT_POLICY_TABLE}
        WHERE {where_sql}
        ORDER BY
            CASE action_type
                WHEN 'REDUCE_EXPOSURE_REVIEW' THEN 1
                WHEN 'BUY_WATCH' THEN 2
                WHEN 'MANUAL_REVIEW' THEN 3
                ELSE 4
            END,
            published_on DESC NULLS LAST,
            ABS(policy_score) DESC NULLS LAST
        LIMIT %(limit)s
        """,
        params=params,
        retries=3,
    )
    summary_df = sql_to_df(
        f"""
        SELECT action_type, policy_class, count(*) AS row_count
        FROM {EVENT_POLICY_TABLE}
        WHERE {where_sql}
        GROUP BY action_type, policy_class
        """,
        params={key: value for key, value in params.items() if key != "limit"},
        retries=3,
    )
    records = _records(rows)
    for row in records:
        row["checks"] = _jsonish(row.get("checks_json"))
        row["operator_notes"] = _jsonish(row.get("operator_notes_json"))
        row["llm_review"] = _jsonish(row.get("llm_review_json"))
        row["raw_context"] = _jsonish(row.get("raw_context_json"))
    action_counts: dict[str, int] = {}
    policy_class_counts: dict[str, int] = {}
    if not summary_df.empty:
        for item in summary_df.to_dict(orient="records"):
            count = int(item.get("row_count") or 0)
            action_counts[str(item.get("action_type") or "UNKNOWN")] = action_counts.get(str(item.get("action_type") or "UNKNOWN"), 0) + count
            policy_class_counts[str(item.get("policy_class") or "UNKNOWN")] = policy_class_counts.get(str(item.get("policy_class") or "UNKNOWN"), 0) + count
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "status": "ok",
        "asof_date": asof_date,
        "summary": {
            "action_counts": action_counts,
            "policy_class_counts": policy_class_counts,
            "row_count": int(sum(action_counts.values())),
        },
        "rows": records,
    }


def build_event_policy_evaluation_payload(*, limit: int = 100) -> dict[str, Any]:
    if not _table_exists(EVENT_POLICY_EVAL_SUMMARY_TABLE):
        return {
            "generated_at": pd.Timestamp.utcnow().isoformat(),
            "status": "missing_table",
            "summary": [],
        }
    df = sql_to_df(
        f"""
        SELECT *
        FROM {EVENT_POLICY_EVAL_SUMMARY_TABLE}
        WHERE evaluated_at = (
            SELECT MAX(evaluated_at)
            FROM {EVENT_POLICY_EVAL_SUMMARY_TABLE}
        )
        ORDER BY
            horizon_days,
            CASE recommendation
                WHEN 'candidate_policy_tighten_or_downgrade' THEN 1
                WHEN 'candidate_policy_strengthen' THEN 2
                WHEN 'monitor' THEN 3
                ELSE 4
            END,
            matured_count DESC NULLS LAST,
            avg_forward_return_after_cost DESC NULLS LAST
        LIMIT %(limit)s
        """,
        params={"limit": max(1, int(limit))},
        retries=3,
    )
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "status": "ok",
        "summary": _records(df),
    }


def build_event_trace_payload(unique_id: str) -> dict[str, Any]:
    return load_event_trace(unique_id)


def build_symbol_trace_payload(symbol: str, *, limit: int = 200) -> dict[str, Any]:
    return load_symbol_trace(symbol, limit=limit)


def _jsonish(value: Any) -> Any:
    if value is None:
        return {}
    if isinstance(value, (dict, list)):
        return value
    try:
        if pd.isna(value):
            return {}
    except Exception:
        pass
    try:
        return json.loads(str(value))
    except Exception:
        return {}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    text = str(value).strip()
    return text or None


def _ts(value: Any) -> str | None:
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.isoformat()


def _boolish(value: Any) -> bool | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "t", "1", "yes"}:
            return True
        if normalized in {"false", "f", "0", "no"}:
            return False
    return bool(value)


def _compact_payload(payload: Any, keys: list[str]) -> dict[str, Any]:
    parsed = _jsonish(payload)
    if not isinstance(parsed, dict):
        return {}
    return {key: parsed.get(key) for key in keys if parsed.get(key) is not None}


def _domain_for_stage(stage: Any) -> str:
    text = str(stage or "").strip().lower()
    if "event_evaluation" in text or "event_tensor" in text:
        return "event"
    if "adversarial" in text or "review" in text:
        return "review"
    if "playbook" in text or "hypothesis" in text:
        return "playbook"
    if "technical" in text:
        return "technical"
    if "lifecycle" in text:
        return "lifecycle"
    if "rebalance" in text:
        return "lifecycle"
    if "risk" in text or "sizing" in text:
        return "risk"
    if "portfolio" in text or "allocation" in text:
        return "portfolio"
    if "macro" in text or "regime" in text:
        return "macro"
    if "exchange" in text or "insider" in text or "short_selling" in text:
        return "exchange"
    if "execution" in text:
        return "execution"
    if "action_consolidation" in text or "action" in text:
        return "action"
    if "ocr" in text or "summary" in text or "categor" in text or "parse" in text or "download" in text:
        return "processing"
    return "generic"


def _domain_payload(stage: Any, payload: Any) -> dict[str, Any]:
    parsed = _jsonish(payload)
    if not isinstance(parsed, dict):
        return {}
    domain = _domain_for_stage(stage)
    if domain == "event":
        event_tensor = parsed.get("event_tensor") if isinstance(parsed.get("event_tensor"), dict) else parsed
        return {
            key: event_tensor.get(key)
            for key in [
                "event_class",
                "direction",
                "materiality",
                "surprise",
                "novelty",
                "contradiction",
                "confidence",
                "expected_decay_days",
                "source_reliability",
                "state_transition_hint",
                "score_impact",
                "verdict",
                "what_happened",
                "rationale",
            ]
            if event_tensor.get(key) is not None
        }
    if domain == "review":
        return {
            key: parsed.get(key)
            for key in ["review_action", "review_score", "veto", "review_reason", "flags"]
            if parsed.get(key) is not None
        }
    if domain == "playbook":
        return {
            key: parsed.get(key)
            for key in [
                "playbook_id",
                "source_key",
                "source_action",
                "mapped_action",
                "production_allowed",
                "execution_mode",
                "confidence",
                "operator_summary",
                "review_boundary",
                "manual_revision_summary",
                "manual_revision_pointers",
            ]
            if parsed.get(key) is not None
        }
    if domain == "technical":
        return {
            key: parsed.get(key)
            for key in [
                "technical_state",
                "technical_score",
                "technical_total_score",
                "technical_trend_score",
                "technical_structure_score",
                "technical_participation_score",
                "technical_relative_strength_score",
                "technical_tradability_score",
                "technical_trigger_type",
                "technical_trigger_note",
                "pivot_price",
                "support_price",
                "stop_price",
                "invalidation_price",
            ]
            if parsed.get(key) is not None
        }
    if domain == "lifecycle":
        return {
            key: parsed.get(key)
            for key in [
                "position_status",
                "next_action",
                "suggested_action",
                "pnl_pct",
                "days_held",
                "bucket_status_note",
                "execution_mode",
                "action_fraction",
                "reference_price",
                "recommended_stop_price",
                "recommended_target_price",
                "stop_price",
                "invalidation_price",
                "expected_horizon_days",
                "active_exit_condition",
                "exit_condition_status",
            ]
            if parsed.get(key) is not None
        }
    if domain == "risk":
        return {
            key: parsed.get(key)
            for key in [
                "allocation_status",
                "risk_bucket",
                "conviction_bucket",
                "suggested_allocation_inr",
                "allocation_pct_of_adv20d",
                "stop_price",
                "invalidation_price",
                "invalidation_rule",
                "confidence",
                "score_impact",
                "review_action",
                "review_score",
            ]
            if parsed.get(key) is not None
        }
    if domain == "portfolio":
        return {
            key: parsed.get(key)
            for key in [
                "portfolio_status",
                "portfolio_reason",
                "plan_rank",
                "priority_score",
                "invest_score_pct",
                "requested_allocation_inr",
                "approved_allocation_inr",
                "remaining_capital_after_inr",
                "overlap_group",
                "overlap_reason",
                "thesis_bucket",
                "bucket_reason",
                "expected_horizon_days",
                "target_price",
                "stop_price",
                "invalidation_price",
            ]
            if parsed.get(key) is not None
        }
    if domain == "macro":
        return {
            key: parsed.get(key)
            for key in ["macro_asof_date", "macro_stress_score", "macro_risk_state", "macro_sizing_multiplier"]
            if parsed.get(key) is not None
        }
    if domain == "exchange":
        return {
            key: parsed.get(key)
            for key in [
                "exchange_asof_date",
                "deal_net_value_20d",
                "deal_cluster_count_20d",
                "insider_net_value_90d",
                "insider_event_count_90d",
                "short_selling_quantity_20d",
                "short_selling_event_count_20d",
                "upcoming_earnings_14d",
                "days_to_earnings",
                "corporate_action_count_30d",
                "exchange_accumulation_score",
                "exchange_distribution_score",
                "exchange_event_score",
            ]
            if parsed.get(key) is not None
        }
    if domain == "action":
        return {
            key: parsed.get(key)
            for key in [
                "winner_action",
                "winner_source",
                "action_source",
                "action_priority",
                "candidate_count",
                "conflict_count",
                "execution_mode",
                "transaction_type",
                "action_fraction",
                "reason_contract_status",
                "recommendation_reason",
                "manual_revision_summary",
                "manual_revision_pointers",
                "manual_revision_status",
            ]
            if parsed.get(key) is not None
        }
    if domain == "execution":
        return {
            key: parsed.get(key)
            for key in [
                "execution_status",
                "execution_reason",
                "transaction_type",
                "quantity",
                "filled_quantity",
                "estimated_order_value_inr",
                "order_value_inr",
                "reference_price",
                "reference_price_source",
                "reference_price_asof",
                "product_type",
                "order_type",
                "validity",
                "execution_mode",
                "security_id",
                "exchange_segment",
                "broker_order_id",
                "exchange_order_id",
                "broker_order_status",
                "submitted_at",
                "broker_update_time",
                "live_mode",
                "safety_checks",
                "raw_broker_action",
                "raw_broker_source",
                "raw_broker_execution_mode",
            ]
            if parsed.get(key) is not None
        }
    return _compact_payload(
        parsed,
        ["status", "source_type", "event_status", "parse_status", "summary", "error", "model", "prompt_version"],
    )


def normalize_trace_payload(raw: dict[str, Any]) -> dict[str, Any]:
    processing_rows = list(raw.get("processing") or [])
    trace_rows = list(raw.get("traces") or [])
    step_rows = list(raw.get("steps") or [])
    conflict_rows = list(raw.get("action_conflicts") or [])

    processing = []
    for row in processing_rows:
        payload = _domain_payload(row.get("stage"), row.get("payload_json"))
        processing.append(
            {
                "domain": _domain_for_stage(row.get("stage")),
                "stage": _text(row.get("stage")) or "processing",
                "status": _text(row.get("status")) or "unknown",
                "symbol": _text(row.get("symbol")),
                "source_type": _text(row.get("source_type")),
                "started_at": _ts(row.get("started_at")),
                "completed_at": _ts(row.get("completed_at")),
                "error": _text(row.get("error")),
                "input_hash": _text(row.get("input_hash")),
                "output_hash": _text(row.get("output_hash")),
                "payload": payload,
            }
        )

    steps_by_trace: dict[str, list[dict[str, Any]]] = {}
    for row in step_rows:
        trace_id = _text(row.get("trace_id"))
        if not trace_id:
            continue
        payload = _domain_payload(row.get("stage"), row.get("payload_json"))
        steps_by_trace.setdefault(trace_id, []).append(
            {
                "domain": _domain_for_stage(row.get("stage")),
                "step_idx": row.get("step_idx"),
                "stage": _text(row.get("stage")) or "stage",
                "status": _text(row.get("status")) or "unknown",
                "reason": _text(row.get("reason")),
                "started_at": _ts(row.get("started_at")),
                "completed_at": _ts(row.get("completed_at")),
                "input_hash": _text(row.get("input_hash")),
                "output_hash": _text(row.get("output_hash")),
                "payload": payload,
            }
        )

    decisions = []
    for row in trace_rows:
        trace_id = _text(row.get("trace_id")) or ""
        payload = _domain_payload(row.get("trigger_type"), row.get("payload_json"))
        decisions.append(
            {
                "domain": _domain_for_stage(row.get("trigger_type")),
                "trace_id": trace_id,
                "asof_date": _ts(row.get("asof_date")),
                "updated_at": _ts(row.get("updated_at")),
                "symbol": _text(row.get("symbol")),
                "unique_id": _text(row.get("unique_id")),
                "setup_id": _text(row.get("setup_id")),
                "trigger_type": _text(row.get("trigger_type")) or "decision",
                "previous_action": _text(row.get("previous_action")),
                "new_action": _text(row.get("new_action")),
                "action_changed": _boolish(row.get("action_changed")),
                "final_action": _text(row.get("final_action")),
                "final_reason": _text(row.get("final_reason")),
                "source_table": _text(row.get("source_table")),
                "source_key": _text(row.get("source_key")),
                "payload": payload,
                "steps": steps_by_trace.get(trace_id, []),
            }
        )

    conflicts = []
    for row in conflict_rows:
        conflicts.append(
            {
                "asof_date": _ts(row.get("asof_date")),
                "symbol": _text(row.get("symbol")),
                "winning_action_code": _text(row.get("winning_action_code")),
                "losing_action_code": _text(row.get("losing_action_code")),
                "winning_source": _text(row.get("winning_source")),
                "losing_source": _text(row.get("losing_source")),
                "losing_setup_id": _text(row.get("losing_setup_id")),
                "losing_unique_id": _text(row.get("losing_unique_id")),
                "lost_reason": _text(row.get("lost_reason")),
            }
        )

    return {
        "symbol": raw.get("symbol"),
        "unique_id": raw.get("unique_id"),
        "processing": processing,
        "decisions": decisions,
        "action_conflicts": conflicts,
        "raw_counts": {
            "processing": len(processing_rows),
            "traces": len(trace_rows),
            "steps": len(step_rows),
            "action_conflicts": len(conflict_rows),
        },
    }


def build_event_trace_summary_payload(unique_id: str) -> dict[str, Any]:
    return normalize_trace_payload(load_event_trace(unique_id))


def build_symbol_trace_summary_payload(symbol: str, *, limit: int = 200) -> dict[str, Any]:
    return normalize_trace_payload(load_symbol_trace(symbol, limit=limit))


def build_data_health_payload(*, asof_date: str | None = None) -> dict[str, Any]:
    payload = load_operator_payload(asof_date=asof_date)
    summary = payload.get("summary") or {}
    return {
        "generated_at": payload.get("generated_at"),
        "asof_date": payload.get("asof_date"),
        "summary": {
            "alert_count": summary.get("alert_count"),
            "action_count": summary.get("action_count"),
            "ts_eval_summary_count": summary.get("ts_eval_summary_count"),
        },
        "sync_state": payload.get("sync_state") or [],
        "runtime_processes": payload.get("runtime_processes") or [],
        "cron_status": payload.get("cron_status") or [],
    }


def build_operator_health_payload() -> dict[str, Any]:
    return build_operator_health()


def build_hypotheses_payload(*, limit: int = 100) -> dict[str, Any]:
    hypotheses = load_hypotheses().head(max(0, int(limit)))
    matches = load_matches(limit=max(0, int(limit)))
    action_plans = load_action_plans(limit=max(0, int(limit)))
    promotion_audits = []
    if not hypotheses.empty:
        for hypothesis_id in hypotheses["hypothesis_id"].dropna().astype(str).head(max(0, int(limit))).tolist():
            audit = latest_promotion_audit(hypothesis_id)
            if audit:
                promotion_audits.append(audit)
    return {
        "hypotheses": hypotheses.to_dict(orient="records") if not hypotheses.empty else [],
        "matches": matches.to_dict(orient="records") if not matches.empty else [],
        "action_plans": action_plans.to_dict(orient="records") if not action_plans.empty else [],
        "promotion_audits": promotion_audits,
    }


def create_hypothesis_payload(payload: dict[str, Any]) -> dict[str, Any]:
    row = create_hypothesis(payload)
    return {"status": "ok", "hypothesis": row}


def preview_hypothesis_create_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return preview_hypothesis_payload(payload)


def update_hypothesis_payload(hypothesis_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    row = update_hypothesis(hypothesis_id, payload)
    return {"status": "ok", "hypothesis": row}


def run_promotion_audit_payload(hypothesis_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return run_promotion_audit(
        hypothesis_id,
        lookback_days=int(payload.get("lookback_days") or 365),
        min_matches=int(payload.get("min_matches") or 3),
        operator_notes=payload.get("operator_notes"),
        approved_by=payload.get("approved_by"),
        persist=bool(payload.get("persist", True)),
    )


def run_hypothesis_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return run_hypothesis_scan(
        hypothesis_id=payload.get("hypothesis_id"),
        from_date=_parse_timestamp(payload.get("from_date")) if payload.get("from_date") else None,
        to_date=_parse_timestamp(payload.get("to_date")) if payload.get("to_date") else None,
        sources=payload.get("sources") or ["news", "announcements", "announcement_documents"],
        persist=bool(payload.get("persist", True)),
        build_actions=bool(payload.get("build_actions", True)),
        use_llm=bool(payload.get("use_llm", True)),
        model=payload.get("model"),
    )


def create_app():
    try:
        from fastapi import Body, FastAPI, HTTPException, Query
        from fastapi.middleware.cors import CORSMiddleware
    except ImportError as exc:
        raise RuntimeError("FastAPI is required for the operator API. Install requirements.txt first.") from exc

    app = FastAPI(title="Stockey Operator API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    def _guard(callable_obj, **kwargs):
        try:
            return callable_obj(**kwargs)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/api/health")
    def health():
        return build_health_payload()

    @app.get("/api/health/details")
    def health_details():
        return _guard(build_operator_health_payload)

    @app.get("/api/summary")
    def summary(asof_date: str | None = None):
        return _guard(build_summary_payload, asof_date=asof_date)

    @app.get("/api/actions")
    def actions(asof_date: str | None = None):
        return _guard(build_actions_payload, asof_date=asof_date)

    @app.get("/api/portfolio")
    def portfolio(asof_date: str | None = None):
        return _guard(build_portfolio_payload, asof_date=asof_date)

    @app.get("/api/watchlist")
    def watchlist(asof_date: str | None = None):
        return _guard(build_watchlist_payload, asof_date=asof_date)

    @app.get("/api/market-context")
    def market_context(asof_date: str | None = None, limit: int = Query(default=50, ge=0, le=500)):
        return _guard(build_market_context_payload, asof_date=asof_date, limit=limit)

    @app.get("/api/technical-calibration")
    def technical_calibration(limit: int = Query(default=25, ge=1, le=100)):
        return _guard(build_technical_calibration_payload, limit=limit)

    @app.post("/api/technical-calibration/promotion-review")
    def technical_calibration_promotion_review(payload: dict[str, Any]):
        return _guard(build_technical_threshold_promotion_review_payload, payload=payload)

    @app.get("/api/technical-calibration/promotion-reviews")
    def technical_calibration_promotion_reviews(limit: int = Query(default=25, ge=1, le=100)):
        return _guard(build_technical_threshold_reviews_payload, limit=limit)

    @app.post("/api/technical-calibration/promotion-review/decision")
    def technical_calibration_promotion_review_decision(payload: dict[str, Any]):
        return _guard(build_technical_threshold_review_decision_payload, payload=payload)

    @app.get("/api/events")
    def events(asof_date: str | None = None, limit: int = Query(default=100, ge=0, le=500)):
        return _guard(build_events_payload, asof_date=asof_date, limit=limit)

    @app.get("/api/event-policy")
    def event_policy(asof_date: str | None = None, action_type: str | None = None, limit: int = Query(default=100, ge=1, le=500)):
        return _guard(build_event_policy_payload, asof_date=asof_date, action_type=action_type, limit=limit)

    @app.get("/api/event-policy/evaluation")
    def event_policy_evaluation(limit: int = Query(default=100, ge=1, le=500)):
        return _guard(build_event_policy_evaluation_payload, limit=limit)

    @app.get("/api/events/{unique_id}/trace")
    def event_trace(unique_id: str):
        return _guard(build_event_trace_payload, unique_id=unique_id)

    @app.get("/api/events/{unique_id}/trace/summary")
    def event_trace_summary(unique_id: str):
        return _guard(build_event_trace_summary_payload, unique_id=unique_id)

    @app.get("/api/symbols/{symbol}/trace")
    def symbol_trace(symbol: str, limit: int = Query(default=200, ge=1, le=1000)):
        return _guard(build_symbol_trace_payload, symbol=symbol, limit=limit)

    @app.get("/api/symbols/{symbol}/trace/summary")
    def symbol_trace_summary(symbol: str, limit: int = Query(default=200, ge=1, le=1000)):
        return _guard(build_symbol_trace_summary_payload, symbol=symbol, limit=limit)

    @app.get("/api/data-health")
    def data_health(asof_date: str | None = None):
        return _guard(build_data_health_payload, asof_date=asof_date)

    @app.get("/api/hypotheses")
    def hypotheses(limit: int = Query(default=100, ge=0, le=500)):
        return _guard(build_hypotheses_payload, limit=limit)

    @app.post("/api/hypotheses")
    def hypothesis_create(payload: dict[str, Any] = Body(...)):
        return _guard(create_hypothesis_payload, payload=payload)

    @app.post("/api/hypotheses/preview")
    def hypothesis_preview(payload: dict[str, Any] = Body(...)):
        return _guard(preview_hypothesis_create_payload, payload=payload)

    @app.post("/api/hypotheses/{hypothesis_id}")
    def hypothesis_update(hypothesis_id: str, payload: dict[str, Any] = Body(...)):
        return _guard(update_hypothesis_payload, hypothesis_id=hypothesis_id, payload=payload)

    @app.post("/api/hypotheses/{hypothesis_id}/promotion-audit")
    def hypothesis_promotion_audit(hypothesis_id: str, payload: dict[str, Any] = Body(default={})):
        return _guard(run_promotion_audit_payload, hypothesis_id=hypothesis_id, payload=payload)

    @app.post("/api/hypotheses/run")
    def hypothesis_run(payload: dict[str, Any] = Body(default={})):
        return _guard(run_hypothesis_payload, payload=payload)

    return app


try:
    app = create_app()
except RuntimeError:
    app = None


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the read-only Stockey operator API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if app is None:
        raise SystemExit("FastAPI is required for the operator API. Install requirements.txt first.")
    import uvicorn

    uvicorn.run("advisory.api.app:app", host=args.host, port=args.port, reload=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
