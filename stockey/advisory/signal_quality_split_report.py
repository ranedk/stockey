from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from advisory.fallback_telemetry import record_local_fallback_event
from advisory.signal_quality_evaluator import EVALUATIONS_TABLE
from advisory.signal_quality_family_report import VARIANT_TO_SOURCE_FAMILY
from utils.db import sql_to_df
from utils.sync import parse_datetime_arg


DEFAULT_MIN_MATURED_ROWS = 5
DEFAULT_TOP_N = 25
DEFAULT_MIN_LIFT_VS_TECHNICAL_ONLY = 0.0


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    return str(value)


def _parse_jsonish(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    text = str(value or "").strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.signal_quality_split_report",
            fallback_type="signal_quality_split_report_json_parse_failed",
            source="signal_quality_split_report_json_fields",
            severity="warn",
            reason="Signal-quality split report could not parse a JSON field and used the default fallback.",
            error=exc,
            metadata={"value_type": type(value).__name__, "value_excerpt": text[:500], "default_type": type(default).__name__},
        )
        return default


def _number(value: Any) -> float | None:
    numeric = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric):
        return None
    return float(numeric)


def _int(value: Any) -> int:
    numeric = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric):
        return 0
    return int(numeric)


def _bool_or_default(value: Any, default: bool = True) -> bool:
    if value is None:
        return bool(default)
    try:
        if pd.isna(value):
            return bool(default)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.signal_quality_split_report",
            fallback_type="signal_quality_split_report_bool_missing_check_failed",
            source="signal_quality_split_report_bool_fields",
            severity="warn",
            reason="Signal-quality split report could not evaluate boolean-field missingness and used normal boolean coercion.",
            error=exc,
            metadata={"value_type": type(value).__name__, "default": bool(default)},
        )
        pass
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y"}:
            return True
        if text in {"0", "false", "no", "n"}:
            return False
    return bool(value)


def _slug(value: Any) -> str:
    text = str(value or "").strip().lower()
    out = []
    previous_underscore = False
    for char in text:
        if char.isalnum():
            out.append(char)
            previous_underscore = False
        elif not previous_underscore:
            out.append("_")
            previous_underscore = True
    return "".join(out).strip("_") or "unknown"


def _record_split_fallback(
    *,
    fallback_type: str,
    source: str,
    reason: str,
    error: Exception,
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="advisory.signal_quality_split_report",
        fallback_type=fallback_type,
        source=source,
        severity="warn",
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def _is_missing_table_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return exc.__class__.__name__ in {"UndefinedTable", "ProgrammingError"} and (
        "does not exist" in text or "undefinedtable" in text
    )


def build_unavailable_report(error: Exception) -> dict[str, Any]:
    empty_queue = build_research_queue([])
    return {
        "status": "evidence_unavailable",
        "authority": "research_only",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        "headline": "Signal-quality evaluation rows are unavailable.",
        "evidence_table": EVALUATIONS_TABLE,
        "evidence_available": False,
        "evidence_error_type": error.__class__.__name__,
        "evidence_error": str(error)[:500],
        "splits": [],
        "research_recommendations": [],
        "research_queue": empty_queue,
        "next_actions": [
            "Run advisory.signal_quality_evaluator before asking for source-family split diagnostics.",
            "Keep context overlays watch-only until split evidence exists and is validated out of sample.",
        ],
    }


def _variants_for_source_family(source_family: str | None) -> list[str]:
    if not source_family:
        return sorted(VARIANT_TO_SOURCE_FAMILY)
    clean = str(source_family).strip()
    return sorted(variant for variant, family in VARIANT_TO_SOURCE_FAMILY.items() if family == clean)


def load_split_evidence(
    *,
    evaluated_at: Any | None = None,
    horizons: list[int] | None = None,
    source_family: str | None = None,
) -> pd.DataFrame:
    context_variants = _variants_for_source_family(source_family)
    if not context_variants:
        return pd.DataFrame()
    variants = sorted(set(context_variants + ["technical_only"]))
    params: dict[str, Any] = {"variants": variants}
    clauses = ["variant = ANY(%(variants)s)", "selected = true", "matured = true"]
    parsed_evaluated_at = None
    if evaluated_at is not None:
        parsed_evaluated_at = pd.to_datetime(evaluated_at, utc=True, errors="coerce")
        if pd.isna(parsed_evaluated_at):
            raise ValueError(f"Invalid evaluated_at: {evaluated_at}")
        clauses.append("evaluated_at = %(evaluated_at)s")
        params["evaluated_at"] = parsed_evaluated_at
    if horizons:
        params["horizons"] = [int(item) for item in horizons]
        clauses.append("horizon_days = ANY(%(horizons)s)")
    try:
        if parsed_evaluated_at is None:
            latest = sql_to_df(
                f"""
                SELECT MAX(evaluated_at) AS evaluated_at
                FROM {EVALUATIONS_TABLE}
                WHERE variant = ANY(%(context_variants)s)
                """,
                params={"context_variants": context_variants},
                retries=3,
            )
            if latest.empty or pd.isna(latest.iloc[0].get("evaluated_at")):
                return pd.DataFrame()
            parsed_evaluated_at = pd.to_datetime(latest.iloc[0]["evaluated_at"], utc=True, errors="coerce")
            clauses.append("evaluated_at = %(evaluated_at)s")
            params["evaluated_at"] = parsed_evaluated_at
        return sql_to_df(
            f"""
            SELECT
                evaluated_at,
                horizon_days,
                variant,
                asof_date,
                setup_id,
                symbol,
                forward_return_after_cost,
                hit_after_cost,
                context_sources_json,
                raw_context_json
            FROM {EVALUATIONS_TABLE}
            WHERE {' AND '.join(clauses)}
            ORDER BY horizon_days, variant, symbol
            """,
            params=params,
            retries=3,
        )
    except Exception as exc:
        _record_split_fallback(
            fallback_type="signal_quality_split_report_load_failed",
            source=EVALUATIONS_TABLE,
            reason="Signal-quality split report could not load selected matured evaluation rows.",
            error=exc,
            metadata={
                "evaluated_at": str(evaluated_at) if evaluated_at is not None else None,
                "horizons": horizons,
                "source_family": source_family,
                "context_variants": context_variants,
                "loaded_variants": variants,
            },
        )
        raise


def explode_context_splits(rows: pd.DataFrame) -> pd.DataFrame:
    if rows.empty:
        return pd.DataFrame()
    out: list[dict[str, Any]] = []
    frame = rows.copy()
    frame["horizon_days"] = pd.to_numeric(frame.get("horizon_days"), errors="coerce")
    frame["forward_return_after_cost"] = pd.to_numeric(frame.get("forward_return_after_cost"), errors="coerce")
    for _, row in frame.iterrows():
        variant = str(row.get("variant") or "").strip()
        family = VARIANT_TO_SOURCE_FAMILY.get(variant)
        if not family:
            continue
        sources = _parse_jsonish(row.get("context_sources_json"), [])
        if not isinstance(sources, list):
            sources = []
        for item in sources:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source") or item.get("context_source") or "").strip()
            if source != family:
                continue
            context_class = str(item.get("class") or item.get("context_class") or "unknown").strip() or "unknown"
            direction = str(item.get("direction") or "unknown").strip().lower() or "unknown"
            split_value = f"{context_class}|{direction}"
            out.append(
                {
                    "evaluated_at": row.get("evaluated_at"),
                    "horizon_days": _int(row.get("horizon_days")),
                    "variant": variant,
                    "source_family": family,
                    "split_axis": "context_class_direction",
                    "split_value": split_value,
                    "context_class": context_class,
                    "direction": direction,
                    "symbol": str(row.get("symbol") or "").strip().upper(),
                    "forward_return_after_cost": _number(row.get("forward_return_after_cost")),
                    "hit_after_cost": bool(row.get("hit_after_cost")) if pd.notna(row.get("hit_after_cost")) else None,
                }
            )
    return pd.DataFrame(out)


def classify_split(
    row: dict[str, Any],
    *,
    min_matured_rows: int = DEFAULT_MIN_MATURED_ROWS,
    min_lift_vs_technical_only: float = DEFAULT_MIN_LIFT_VS_TECHNICAL_ONLY,
) -> str:
    matured = _int(row.get("matured_count"))
    avg_return = _number(row.get("avg_forward_return_after_cost"))
    lift = _number(row.get("lift_vs_technical_only"))
    if matured < int(min_matured_rows):
        return "needs_more_data"
    if lift is None:
        return "technical_baseline_unavailable"
    if avg_return is not None and avg_return <= 0:
        return "negative_after_cost"
    if lift <= float(min_lift_vs_technical_only):
        return "hurts_or_no_lift"
    return "candidate_split_helpful"


def _technical_baseline_by_horizon(rows: pd.DataFrame) -> dict[int, float]:
    if rows.empty or "variant" not in rows.columns:
        return {}
    frame = rows.copy()
    frame["horizon_days"] = pd.to_numeric(frame.get("horizon_days"), errors="coerce")
    frame["forward_return_after_cost"] = pd.to_numeric(frame.get("forward_return_after_cost"), errors="coerce")
    if "selected" in frame.columns:
        selected = frame["selected"].map(lambda value: _bool_or_default(value, True))
    else:
        selected = pd.Series([True] * len(frame), index=frame.index)
    if "matured" in frame.columns:
        matured = frame["matured"].map(lambda value: _bool_or_default(value, True))
    else:
        matured = pd.Series([True] * len(frame), index=frame.index)
    baseline = frame[
        frame["variant"].astype("string").eq("technical_only")
        & selected
        & matured
        & frame["horizon_days"].notna()
        & frame["forward_return_after_cost"].notna()
    ]
    if baseline.empty:
        return {}
    return {
        int(horizon): float(group["forward_return_after_cost"].mean())
        for horizon, group in baseline.groupby("horizon_days", dropna=True)
    }


def _technical_baseline_diagnostic(rows: pd.DataFrame, baseline_by_horizon: dict[int, float]) -> dict[str, Any]:
    positive_horizons = [horizon for horizon, value in sorted(baseline_by_horizon.items()) if float(value) > 0]
    negative_horizons = [horizon for horizon, value in sorted(baseline_by_horizon.items()) if float(value) <= 0]
    return {
        "status": "ok" if baseline_by_horizon else "missing",
        "baseline_variant": "technical_only",
        "avg_baseline_return_after_cost": None if not baseline_by_horizon else float(pd.Series(baseline_by_horizon).mean()),
        "horizon_count": len(baseline_by_horizon),
        "positive_horizon_count": len(positive_horizons),
        "negative_horizon_count": len(negative_horizons),
        "positive_horizons": positive_horizons,
        "negative_horizons": negative_horizons,
        "technical_only_selected_matured_rows": int(
            rows[
                rows.get("variant", pd.Series(dtype=object)).astype("string").eq("technical_only")
                & rows.get("forward_return_after_cost", pd.Series(dtype=object)).notna()
            ].shape[0]
        )
        if not rows.empty
        else 0,
        "operator_action": (
            "Split classifications use lift over technical_only. If baseline is missing, run advisory.signal_quality_evaluator before trusting split diagnostics."
            if not baseline_by_horizon
            else "Use split candidates only when they beat matching technical_only returns after costs; positive raw returns alone are not enough."
        ),
        "authority_scope": "research_only",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
    }


def build_research_recommendations(splits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    recommendations: list[dict[str, Any]] = []
    for row in splits:
        classification = str(row.get("classification") or "").strip()
        source_family = str(row.get("source_family") or "").strip()
        split_value = str(row.get("split_value") or "").strip()
        horizon_days = _int(row.get("horizon_days"))
        if not source_family or not split_value or horizon_days <= 0:
            continue
        if classification == "candidate_split_helpful":
            action = "build_narrower_evaluator_variant"
            priority = "high"
            reason = "This split has enough matured rows, positive after-cost return, and positive lift over technical_only; test it as its own source-family hypothesis across windows."
        elif classification == "hurts_or_no_lift":
            action = "track_as_negative_control"
            priority = "medium"
            reason = "This split has enough matured rows but does not beat the matching technical_only baseline after costs; do not promote it from raw positive returns."
        elif classification == "negative_after_cost":
            action = "track_as_negative_control"
            priority = "medium"
            reason = "This split has enough matured rows but negative after-cost return; do not use it for positive watchlist pressure without separate evidence."
        elif classification == "technical_baseline_unavailable":
            action = "collect_more_data"
            priority = "low"
            reason = "This split cannot be evaluated for lift because matching technical_only baseline rows are missing."
        else:
            action = "collect_more_data"
            priority = "low"
            reason = "This split does not yet have enough matured rows for a reliable recommendation."
        recommendations.append(
            {
                "recommendation_id": "split_{family}_{split}_h{horizon}".format(
                    family=_slug(source_family),
                    split=_slug(split_value),
                    horizon=horizon_days,
                ),
                "source_family": source_family,
                "horizon_days": horizon_days,
                "split_axis": row.get("split_axis"),
                "split_value": split_value,
                "context_class": row.get("context_class"),
                "direction": row.get("direction"),
                "classification": classification,
                "recommended_research_action": action,
                "priority": priority,
                "reason": reason,
                "matured_count": _int(row.get("matured_count")),
                "symbol_count": _int(row.get("symbol_count")),
                "avg_forward_return_after_cost": _number(row.get("avg_forward_return_after_cost")),
                "baseline_avg_forward_return_after_cost": _number(row.get("baseline_avg_forward_return_after_cost")),
                "lift_vs_technical_only": _number(row.get("lift_vs_technical_only")),
                "hit_rate_after_cost": _number(row.get("hit_rate_after_cost")),
                "authority": "research_only",
                "broker_execution_allowed": False,
                "policy_auto_promotion_allowed": False,
            }
        )
    recommendations.sort(
        key=lambda item: (
            item["priority"] == "high",
            item["priority"] == "medium",
            item.get("avg_forward_return_after_cost") if item.get("avg_forward_return_after_cost") is not None else -999.0,
            item.get("matured_count") or 0,
        ),
        reverse=True,
    )
    return recommendations


def build_research_queue(recommendations: list[dict[str, Any]]) -> dict[str, Any]:
    buckets = {
        "build_narrower_evaluator_variant": [],
        "track_as_negative_control": [],
        "collect_more_data": [],
    }
    for row in recommendations:
        action = str(row.get("recommended_research_action") or "").strip()
        if action not in buckets:
            continue
        buckets[action].append(row)
    return {
        "status": "ok",
        "authority": "research_only",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        "candidate_build_count": len(buckets["build_narrower_evaluator_variant"]),
        "negative_control_count": len(buckets["track_as_negative_control"]),
        "data_collection_count": len(buckets["collect_more_data"]),
        "build_narrower_evaluator_variant": buckets["build_narrower_evaluator_variant"],
        "track_as_negative_control": buckets["track_as_negative_control"],
        "collect_more_data": buckets["collect_more_data"],
        "next_actions": [
            "Build narrower evaluator variants only for high-priority split candidates, then rerun rolling-window validation.",
            "Use negative controls to block broad source-family promotion and to test whether exclusion improves after-cost results.",
            "Keep low-data splits as watch/research evidence until more labels mature.",
        ],
    }


def build_split_report(
    rows: pd.DataFrame,
    *,
    min_matured_rows: int = DEFAULT_MIN_MATURED_ROWS,
    min_lift_vs_technical_only: float = DEFAULT_MIN_LIFT_VS_TECHNICAL_ONLY,
    top_n: int = DEFAULT_TOP_N,
) -> dict[str, Any]:
    exploded = explode_context_splits(rows)
    baseline_by_horizon = _technical_baseline_by_horizon(rows)
    baseline_diagnostic = _technical_baseline_diagnostic(rows, baseline_by_horizon)
    if exploded.empty:
        empty_queue = build_research_queue([])
        return {
            "status": "no_data",
            "authority": "research_only",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
            "headline": "No selected matured context-source rows were available for split diagnostics.",
            "evaluated_at": None,
            "split_count": 0,
            "technical_baseline": baseline_diagnostic,
            "splits": [],
            "research_recommendations": [],
            "research_queue": empty_queue,
            "next_actions": ["Run signal-quality evaluation with context overlays before reviewing split diagnostics."],
        }
    splits: list[dict[str, Any]] = []
    group_cols = ["source_family", "horizon_days", "split_axis", "split_value", "context_class", "direction"]
    for keys, group in exploded.groupby(group_cols, sort=True, dropna=False):
        source_family, horizon_days, split_axis, split_value, context_class, direction = keys
        returns = pd.to_numeric(group["forward_return_after_cost"], errors="coerce").dropna()
        hits = group["hit_after_cost"].dropna()
        avg_return = None if returns.empty else float(returns.mean())
        baseline_avg = baseline_by_horizon.get(_int(horizon_days))
        lift = None if baseline_avg is None or avg_return is None else float(avg_return - baseline_avg)
        row = {
            "source_family": source_family,
            "horizon_days": _int(horizon_days),
            "split_axis": split_axis,
            "split_value": split_value,
            "context_class": context_class,
            "direction": direction,
            "matured_count": int(len(group)),
            "symbol_count": int(group["symbol"].nunique()),
            "avg_forward_return_after_cost": avg_return,
            "baseline_avg_forward_return_after_cost": baseline_avg,
            "lift_vs_technical_only": lift,
            "hit_rate_after_cost": None if hits.empty else float(hits.astype(bool).mean()),
            "sample_symbols": sorted(group["symbol"].dropna().astype(str).unique().tolist())[:10],
            "authority": "research_only",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
        }
        row["classification"] = classify_split(
            row,
            min_matured_rows=min_matured_rows,
            min_lift_vs_technical_only=min_lift_vs_technical_only,
        )
        splits.append(row)
    splits.sort(
        key=lambda item: (
            item["classification"] == "candidate_split_helpful",
            item.get("lift_vs_technical_only") if item.get("lift_vs_technical_only") is not None else -999.0,
            item.get("avg_forward_return_after_cost") if item.get("avg_forward_return_after_cost") is not None else -999.0,
            item.get("matured_count") or 0,
        ),
        reverse=True,
    )
    trimmed = splits[: max(1, int(top_n))]
    helpful = [row for row in trimmed if row.get("classification") == "candidate_split_helpful"]
    recommendations = build_research_recommendations(trimmed)
    research_queue = build_research_queue(recommendations)
    return {
        "status": "ok",
        "authority": "research_only",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        "headline": (
            "At least one source-family split has positive after-cost evidence."
            if helpful
            else "No source-family split has enough positive after-cost evidence yet."
        ),
        "evaluated_at": None if pd.to_datetime(exploded["evaluated_at"], utc=True, errors="coerce").dropna().empty else pd.to_datetime(exploded["evaluated_at"], utc=True, errors="coerce").max(),
        "min_matured_rows": int(min_matured_rows),
        "min_lift_vs_technical_only": float(min_lift_vs_technical_only),
        "split_count": len(splits),
        "candidate_split_helpful_count": len(helpful),
        "technical_baseline": baseline_diagnostic,
        "splits": trimmed,
        "research_recommendation_count": len(recommendations),
        "research_recommendations": recommendations,
        "research_queue": research_queue,
        "next_actions": [
            "Use candidate_split_helpful rows as research hypotheses only when they beat technical_only after costs.",
            "Do not apply split findings to action policy until they pass rolling-window validation after costs.",
        ],
    }


def format_text_report(report: dict[str, Any]) -> str:
    lines = [
        f"status: {report.get('status')}",
        f"headline: {report.get('headline')}",
        "authority: research_only; no policy or broker behavior changed",
    ]
    for row in report.get("splits") or []:
        lines.append(
            " - {family} h{horizon} {split}: {classification}, avg={avg}, matured={matured}, hit={hit}".format(
                family=row.get("source_family"),
                horizon=row.get("horizon_days"),
                split=row.get("split_value"),
                classification=row.get("classification"),
                avg=row.get("avg_forward_return_after_cost"),
                matured=row.get("matured_count"),
                hit=row.get("hit_rate_after_cost"),
            )
        )
        if row.get("baseline_avg_forward_return_after_cost") is not None or row.get("lift_vs_technical_only") is not None:
            lines[-1] += "; baseline={baseline}, lift={lift}".format(
                baseline=row.get("baseline_avg_forward_return_after_cost"),
                lift=row.get("lift_vs_technical_only"),
            )
    recommendations = report.get("research_recommendations") or []
    if recommendations:
        lines.append("research recommendations:")
        for row in recommendations:
            lines.append(
                " * {family} h{horizon} {split}: {action} ({priority})".format(
                    family=row.get("source_family"),
                    horizon=row.get("horizon_days"),
                    split=row.get("split_value"),
                    action=row.get("recommended_research_action"),
                    priority=row.get("priority"),
                )
            )
    return "\n".join(lines)


def build_queue_only_report(report: dict[str, Any]) -> dict[str, Any]:
    queue = report.get("research_queue") if isinstance(report.get("research_queue"), dict) else build_research_queue([])
    return {
        "status": report.get("status"),
        "headline": report.get("headline"),
        "evaluated_at": report.get("evaluated_at"),
        "authority": "research_only",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        "source_family": report.get("source_family"),
        "research_queue": queue,
        "evidence_available": report.get("evidence_available", report.get("status") not in {"evidence_unavailable", "no_data"}),
        "next_actions": [
            "Use this queue to choose the next research-only evaluator split to implement.",
            "Do not apply queued split candidates to action policy until rolling-window validation passes after costs.",
        ],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a research-only split report for noisy signal-quality source families.")
    parser.add_argument("--evaluated-at", type=parse_datetime_arg)
    parser.add_argument("--horizons", nargs="*", type=int)
    parser.add_argument("--source-family")
    parser.add_argument("--min-matured-rows", type=int, default=DEFAULT_MIN_MATURED_ROWS)
    parser.add_argument("--min-lift-vs-technical-only", type=float, default=DEFAULT_MIN_LIFT_VS_TECHNICAL_ONLY)
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    parser.add_argument("--format", choices=["json", "text"], default="json")
    parser.add_argument(
        "--queue-only",
        action="store_true",
        help="Return only the research queue and high-level metadata for automation.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        rows = load_split_evidence(
            evaluated_at=args.evaluated_at,
            horizons=args.horizons,
            source_family=args.source_family,
        )
        report = build_split_report(
            rows,
            min_matured_rows=max(1, int(args.min_matured_rows)),
            min_lift_vs_technical_only=float(args.min_lift_vs_technical_only),
            top_n=max(1, int(args.top_n)),
        )
    except Exception as exc:
        if _is_missing_table_error(exc):
            report = build_unavailable_report(exc)
        else:
            raise
    if args.queue_only:
        report = build_queue_only_report(report)
    if args.format == "text":
        print(format_text_report(report))
    else:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
