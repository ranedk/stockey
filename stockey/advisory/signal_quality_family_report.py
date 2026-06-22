from __future__ import annotations

import argparse
import json
from collections import Counter
from typing import Any

import pandas as pd

from advisory.fallback_telemetry import record_local_fallback_event
from advisory.signal_quality_evaluator import CONTEXT_SOURCE_FAMILY_VARIANTS, SUMMARY_TABLE
from utils.db import sql_to_df


DEFAULT_MIN_MATURED_ROWS = 10
DEFAULT_MIN_SELECTED_ROWS = 10
DEFAULT_MIN_LIFT_VS_TECHNICAL_ONLY = 0.01
INCONSISTENT_CLASSIFICATION = "inconsistent_or_horizon_sensitive"
SOURCE_FAMILY_VARIANTS = dict(CONTEXT_SOURCE_FAMILY_VARIANTS)
SOURCE_FAMILY_VARIANTS["technical_after_trusted_context_rules"] = "trusted_context_rules"
VARIANT_TO_SOURCE_FAMILY = {variant: family for variant, family in SOURCE_FAMILY_VARIANTS.items()}


def family_report_authority_contract(source_family: str | None = None) -> dict[str, Any]:
    return {
        "authority_scope": "research_only",
        "action_policy_effect": "source_family_outcome_summary_only_no_live_policy_change",
        "portfolio_authority": "none",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        "requires_rolling_window_validation": True,
        "requires_manual_review_before_config": True,
        "requires_disabled_config_preview_before_runtime": True,
        "baseline_variant": "technical_only",
        "source_family": source_family,
    }


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    return str(value)


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


def _record_report_fallback(
    *,
    fallback_type: str,
    source: str,
    reason: str,
    error: Exception,
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="advisory.signal_quality_family_report",
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
    authority_contract = family_report_authority_contract()
    return {
        "status": "evidence_unavailable",
        "authority": "research_only",
        "authority_contract": authority_contract,
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        "headline": "Signal-quality source-family evidence table is unavailable.",
        "evidence_table": SUMMARY_TABLE,
        "evidence_available": False,
        "evidence_error_type": error.__class__.__name__,
        "evidence_error": str(error)[:500],
        "families": [],
        "best_family": None,
        "worst_family": None,
        "next_actions": [
            "Run advisory.signal_quality_evaluator after technical/context data has matured.",
            "Keep context overlays explanation-only until source-family evidence exists and passes promotion review.",
        ],
    }


def load_family_summary(
    *,
    evaluated_at: Any | None = None,
    horizons: list[int] | None = None,
) -> pd.DataFrame:
    variants = sorted(VARIANT_TO_SOURCE_FAMILY)
    params: dict[str, Any] = {"variants": variants}
    clauses = ["variant = ANY(%(variants)s)"]
    parsed_evaluated_at = None
    if evaluated_at is not None:
        parsed_evaluated_at = pd.to_datetime(evaluated_at, utc=True, errors="coerce")
        if pd.isna(parsed_evaluated_at):
            raise ValueError(f"Invalid evaluated_at: {evaluated_at}")
        clauses.append("evaluated_at = %(evaluated_at)s")
        params["evaluated_at"] = parsed_evaluated_at
    if horizons:
        parsed_horizons = [int(item) for item in horizons]
        clauses.append("horizon_days = ANY(%(horizons)s)")
        params["horizons"] = parsed_horizons
    try:
        if parsed_evaluated_at is None:
            latest = sql_to_df(
                f"""
                SELECT MAX(evaluated_at) AS evaluated_at
                FROM {SUMMARY_TABLE}
                WHERE variant = ANY(%(variants)s)
                """,
                params={"variants": variants},
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
                sample_count,
                selected_count,
                matured_count,
                selection_rate,
                avg_forward_return_after_cost,
                hit_rate_after_cost,
                positive_return_rate,
                avg_benchmark_forward_return,
                avg_excess_forward_return_after_cost,
                excess_hit_rate_after_cost,
                positive_excess_return_rate,
                baseline_avg_forward_return_after_cost,
                lift_vs_technical_only,
                recommendation,
                sample_start,
                sample_end,
                load_ts
            FROM {SUMMARY_TABLE}
            WHERE {' AND '.join(clauses)}
            ORDER BY horizon_days, variant
            """,
            params=params,
            retries=3,
        )
    except Exception as exc:
        _record_report_fallback(
            fallback_type="signal_quality_family_report_load_failed",
            source=SUMMARY_TABLE,
            reason="Signal-quality family report could not load source-family summary rows.",
            error=exc,
            metadata={
                "evaluated_at": str(evaluated_at) if evaluated_at is not None else None,
                "horizons": horizons,
                "variants": variants,
            },
        )
        raise


def classify_family_row(
    row: dict[str, Any],
    *,
    min_matured_rows: int = DEFAULT_MIN_MATURED_ROWS,
    min_selected_rows: int = DEFAULT_MIN_SELECTED_ROWS,
    min_lift_vs_technical_only: float = DEFAULT_MIN_LIFT_VS_TECHNICAL_ONLY,
) -> str:
    matured = _int(row.get("matured_count"))
    selected = _int(row.get("selected_count"))
    lift = _number(row.get("lift_vs_technical_only"))
    avg_return = _number(row.get("avg_forward_return_after_cost"))
    avg_excess_return = _number(row.get("avg_excess_forward_return_after_cost"))
    excess_hit_rate = _number(row.get("excess_hit_rate_after_cost"))
    if matured < int(min_matured_rows) or selected < int(min_selected_rows):
        return "needs_more_data"
    if avg_excess_return is None or excess_hit_rate is None:
        return "needs_benchmark_attribution"
    if lift is not None and lift <= 0:
        return "hurts_or_no_lift"
    if avg_return is not None and avg_return <= 0:
        return "negative_after_cost"
    if avg_excess_return <= 0:
        return "benchmark_beta_not_overlay_alpha"
    if lift is not None and lift >= float(min_lift_vs_technical_only) and excess_hit_rate >= 0.50:
        return "candidate_helpful"
    return "monitor"


def classify_family_aggregate(
    aggregate: dict[str, Any],
    *,
    min_matured_rows: int = DEFAULT_MIN_MATURED_ROWS,
    min_selected_rows: int = DEFAULT_MIN_SELECTED_ROWS,
    min_lift_vs_technical_only: float = DEFAULT_MIN_LIFT_VS_TECHNICAL_ONLY,
) -> str:
    row_class = classify_family_row(
        {
            "matured_count": aggregate.get("total_matured_count"),
            "selected_count": aggregate.get("total_selected_count"),
            "lift_vs_technical_only": aggregate.get("avg_lift_vs_technical_only"),
            "avg_forward_return_after_cost": aggregate.get("avg_forward_return_after_cost"),
            "avg_excess_forward_return_after_cost": aggregate.get("avg_excess_forward_return_after_cost"),
            "excess_hit_rate_after_cost": aggregate.get("avg_excess_hit_rate_after_cost"),
        },
        min_matured_rows=min_matured_rows,
        min_selected_rows=min_selected_rows,
        min_lift_vs_technical_only=min_lift_vs_technical_only,
    )
    if row_class == "needs_more_data":
        return row_class

    horizon_classes = {
        str(row.get("classification"))
        for row in aggregate.get("horizons") or []
        if _int(row.get("matured_count")) >= int(min_matured_rows)
        and _int(row.get("selected_count")) >= int(min_selected_rows)
    }
    helpful = "candidate_helpful" in horizon_classes
    harmful = bool({"hurts_or_no_lift", "negative_after_cost", "benchmark_beta_not_overlay_alpha"} & horizon_classes)
    if "needs_benchmark_attribution" in horizon_classes and not helpful:
        return "needs_benchmark_attribution"
    if helpful and harmful:
        return INCONSISTENT_CLASSIFICATION
    return row_class


def _build_technical_baseline_diagnostic(frame: pd.DataFrame, *, candidate_helpful_count: int) -> dict[str, Any]:
    baseline = frame.dropna(subset=["baseline_avg_forward_return_after_cost"]).copy()
    if baseline.empty:
        return {
            "status": "missing",
            "interpretation": "technical_baseline_unavailable",
            "avg_baseline_return_after_cost": None,
            "positive_horizon_count": 0,
            "negative_horizon_count": 0,
            "operator_action": "Run advisory.signal_quality_evaluator with technical_only rows before drawing conclusions about context-family lift.",
            "authority_scope": "research_only",
            "policy_auto_promotion_allowed": False,
            "broker_execution_allowed": False,
        }
    by_horizon = (
        baseline.groupby("horizon_days", dropna=True)["baseline_avg_forward_return_after_cost"]
        .mean()
        .dropna()
        .sort_index()
    )
    positive_horizons = [int(horizon) for horizon, value in by_horizon.items() if float(value) > 0]
    negative_horizons = [int(horizon) for horizon, value in by_horizon.items() if float(value) <= 0]
    avg_baseline = _number(by_horizon.mean()) if not by_horizon.empty else None
    if avg_baseline is not None and avg_baseline > 0 and candidate_helpful_count <= 0:
        interpretation = "technical_baseline_positive_but_no_context_family_ready"
        operator_action = (
            "Do not solve this by loosening a global regime gate. Inspect whether technical candidates are being lost after "
            "screening/risk/lifecycle, and keep context families research-only until they add lift over the positive technical baseline."
        )
    elif candidate_helpful_count > 0:
        interpretation = "context_family_candidates_exist"
        operator_action = "Review candidate-helpful families through rolling-window promotion gates before any disabled config preview."
    elif avg_baseline is not None and avg_baseline <= 0:
        interpretation = "technical_baseline_not_positive"
        operator_action = "Market participation is not explained by the technical baseline in this report; inspect broader market/context evidence separately."
    else:
        interpretation = "technical_baseline_inconclusive"
        operator_action = "Collect more matured technical and context-family rows before changing live policy."
    return {
        "status": "ok",
        "interpretation": interpretation,
        "avg_baseline_return_after_cost": avg_baseline,
        "horizon_count": int(len(by_horizon)),
        "positive_horizon_count": int(len(positive_horizons)),
        "negative_horizon_count": int(len(negative_horizons)),
        "positive_horizons": positive_horizons,
        "negative_horizons": negative_horizons,
        "operator_action": operator_action,
        "authority_scope": "research_only",
        "policy_auto_promotion_allowed": False,
        "broker_execution_allowed": False,
    }


def build_promotion_readiness(families: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize whether source-family context evidence is ready for review.

    This is a research gate only. It does not promote config, rank actions,
    mutate portfolio state, or create broker authority.
    """

    family_rows = [row for row in families if isinstance(row, dict)]
    counts = Counter(str(row.get("classification") or "missing").strip() for row in family_rows)
    candidate_families = [
        str(row.get("source_family") or "").strip()
        for row in family_rows
        if str(row.get("classification") or "") == "candidate_helpful"
    ]
    benchmark_blocked_classes = {"benchmark_beta_not_overlay_alpha", "needs_benchmark_attribution"}
    suppressed_classes = {
        "hurts_or_no_lift",
        "negative_after_cost",
        "benchmark_beta_not_overlay_alpha",
        "needs_benchmark_attribution",
        INCONSISTENT_CLASSIFICATION,
    }
    benchmark_blocked = [
        str(row.get("source_family") or "").strip()
        for row in family_rows
        if str(row.get("classification") or "") in benchmark_blocked_classes
    ]
    suppressed = [
        str(row.get("source_family") or "").strip()
        for row in family_rows
        if str(row.get("classification") or "") in suppressed_classes
    ]
    low_data = [
        str(row.get("source_family") or "").strip()
        for row in family_rows
        if str(row.get("classification") or "") == "needs_more_data"
    ]
    inconsistent = [
        str(row.get("source_family") or "").strip()
        for row in family_rows
        if str(row.get("classification") or "") == INCONSISTENT_CLASSIFICATION
    ]
    if candidate_families:
        status = "candidate_families_ready_for_review"
        operator_action = (
            "Run rolling-window promotion review only for candidate_helpful families; keep all runtime effects disabled "
            "until manual config review passes."
        )
    elif benchmark_blocked:
        status = "blocked_by_benchmark_or_attribution"
        operator_action = (
            "Do not promote context overlays from raw positive returns. Run split diagnostics and require benchmark-excess "
            "evidence before any reviewed rule can affect watch priority."
        )
    elif low_data and len(low_data) == len(family_rows):
        status = "needs_more_matured_data"
        operator_action = "Collect more matured context-family rows before changing regime, context, or LLM policy."
    elif inconsistent:
        status = "blocked_by_horizon_instability"
        operator_action = "Run narrowed split diagnostics by event class, direction, sector, or macro signal before promotion review."
    elif family_rows:
        status = "no_family_ready_for_review"
        operator_action = "Keep context overlays explanation-only; no source family has proven lift over technical-only after costs."
    else:
        status = "no_family_evidence"
        operator_action = "Run signal-quality evaluation before trusting context-overlay families."
    research_queue: list[dict[str, Any]] = []
    if candidate_families:
        research_queue.append(
            {
                "task": "promotion_review",
                "families": candidate_families,
                "command": "python -m advisory.signal_quality_promotion --auto-family-candidates --format text",
                "authority_scope": "research_review_only",
            }
        )
    if benchmark_blocked or inconsistent:
        research_queue.append(
            {
                "task": "split_diagnostics",
                "families": sorted(set(benchmark_blocked + inconsistent)),
                "command": "python -m advisory.signal_quality_window_runner --include-signal-quality-split-reports",
                "authority_scope": "research_only",
            }
        )
    if low_data:
        research_queue.append(
            {
                "task": "collect_more_data",
                "families": low_data,
                "command": "./all_ml.sh",
                "authority_scope": "research_only",
            }
        )
    return {
        "status": status,
        "authority_contract": family_report_authority_contract(),
        "policy_auto_promotion_allowed": False,
        "broker_execution_allowed": False,
        "family_count": int(len(family_rows)),
        "classification_counts": dict(counts.most_common()),
        "candidate_helpful_families": candidate_families,
        "benchmark_or_attribution_blocked_families": benchmark_blocked,
        "suppressed_families": suppressed,
        "low_data_families": low_data,
        "inconsistent_families": inconsistent,
        "operator_action": operator_action,
        "research_queue": research_queue,
    }


def build_family_report(
    rows: pd.DataFrame,
    *,
    min_matured_rows: int = DEFAULT_MIN_MATURED_ROWS,
    min_selected_rows: int = DEFAULT_MIN_SELECTED_ROWS,
    min_lift_vs_technical_only: float = DEFAULT_MIN_LIFT_VS_TECHNICAL_ONLY,
) -> dict[str, Any]:
    if rows.empty:
        authority_contract = family_report_authority_contract()
        return {
            "status": "no_data",
            "authority": "research_only",
            "authority_contract": authority_contract,
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
            "promotion_readiness": build_promotion_readiness([]),
            "headline": "No source-family signal-quality summary rows were found.",
            "evaluated_at": None,
            "families": [],
            "best_family": None,
            "worst_family": None,
            "next_actions": [
                "Run advisory.signal_quality_evaluator after enough context-overlay data has matured.",
                "Keep source-family overlays review-only until this report shows stable positive lift after costs.",
            ],
        }
    frame = rows.copy()
    frame["source_family"] = frame["variant"].map(VARIANT_TO_SOURCE_FAMILY)
    for column in [
        "horizon_days",
        "sample_count",
        "selected_count",
        "matured_count",
    ]:
        frame[column] = pd.to_numeric(frame.get(column), errors="coerce")
    for column in [
        "selection_rate",
        "avg_forward_return_after_cost",
        "hit_rate_after_cost",
        "positive_return_rate",
        "baseline_avg_forward_return_after_cost",
        "lift_vs_technical_only",
        "avg_benchmark_forward_return",
        "avg_excess_forward_return_after_cost",
        "excess_hit_rate_after_cost",
        "positive_excess_return_rate",
    ]:
        frame[column] = pd.to_numeric(frame.get(column), errors="coerce")
    frame["evaluated_at"] = pd.to_datetime(frame["evaluated_at"], utc=True, errors="coerce")
    frame["sample_start"] = pd.to_datetime(frame.get("sample_start"), utc=True, errors="coerce")
    frame["sample_end"] = pd.to_datetime(frame.get("sample_end"), utc=True, errors="coerce")

    family_rows: list[dict[str, Any]] = []
    for source_family, group in frame.groupby("source_family", sort=True, dropna=True):
        family_contract = family_report_authority_contract(str(source_family))
        horizons: list[dict[str, Any]] = []
        for _, item in group.sort_values("horizon_days").iterrows():
            horizon_row = {
                "horizon_days": _int(item.get("horizon_days")),
                "variant": item.get("variant"),
                "authority_contract": family_contract,
                "sample_count": _int(item.get("sample_count")),
                "selected_count": _int(item.get("selected_count")),
                "matured_count": _int(item.get("matured_count")),
                "selection_rate": _number(item.get("selection_rate")),
                "avg_forward_return_after_cost": _number(item.get("avg_forward_return_after_cost")),
                "hit_rate_after_cost": _number(item.get("hit_rate_after_cost")),
                "positive_return_rate": _number(item.get("positive_return_rate")),
                "avg_benchmark_forward_return": _number(item.get("avg_benchmark_forward_return")),
                "avg_excess_forward_return_after_cost": _number(item.get("avg_excess_forward_return_after_cost")),
                "excess_hit_rate_after_cost": _number(item.get("excess_hit_rate_after_cost")),
                "positive_excess_return_rate": _number(item.get("positive_excess_return_rate")),
                "baseline_avg_forward_return_after_cost": _number(item.get("baseline_avg_forward_return_after_cost")),
                "lift_vs_technical_only": _number(item.get("lift_vs_technical_only")),
                "recommendation": item.get("recommendation"),
            }
            horizon_row["classification"] = classify_family_row(
                horizon_row,
                min_matured_rows=min_matured_rows,
                min_selected_rows=min_selected_rows,
                min_lift_vs_technical_only=min_lift_vs_technical_only,
            )
            horizons.append(horizon_row)
        matured_group = group[group["matured_count"].fillna(0).astype(float) > 0]
        aggregate = {
            "source_family": source_family,
            "authority_contract": family_contract,
            "horizon_count": int(group["horizon_days"].nunique()),
            "total_selected_count": _int(group["selected_count"].sum()),
            "total_matured_count": _int(group["matured_count"].sum()),
            "avg_lift_vs_technical_only": _number(group["lift_vs_technical_only"].mean()),
            "best_lift_vs_technical_only": _number(group["lift_vs_technical_only"].max()),
            "worst_lift_vs_technical_only": _number(group["lift_vs_technical_only"].min()),
            "avg_forward_return_after_cost": _number(matured_group["avg_forward_return_after_cost"].mean()) if not matured_group.empty else None,
            "avg_benchmark_forward_return": _number(matured_group["avg_benchmark_forward_return"].mean()) if not matured_group.empty else None,
            "avg_excess_forward_return_after_cost": _number(matured_group["avg_excess_forward_return_after_cost"].mean()) if not matured_group.empty else None,
            "avg_hit_rate_after_cost": _number(matured_group["hit_rate_after_cost"].mean()) if not matured_group.empty else None,
            "avg_excess_hit_rate_after_cost": _number(matured_group["excess_hit_rate_after_cost"].mean()) if not matured_group.empty else None,
            "sample_start": None if group["sample_start"].dropna().empty else group["sample_start"].min(),
            "sample_end": None if group["sample_end"].dropna().empty else group["sample_end"].max(),
            "horizons": horizons,
        }
        aggregate["classification"] = classify_family_aggregate(
            aggregate,
            min_matured_rows=min_matured_rows,
            min_selected_rows=min_selected_rows,
            min_lift_vs_technical_only=min_lift_vs_technical_only,
        )
        family_rows.append(aggregate)

    family_rows.sort(
        key=lambda row: (
            row["classification"] == "candidate_helpful",
            row.get("avg_lift_vs_technical_only") if row.get("avg_lift_vs_technical_only") is not None else -999.0,
            row.get("total_matured_count") or 0,
        ),
        reverse=True,
    )
    best = family_rows[0] if family_rows else None
    worst = min(
        family_rows,
        key=lambda row: row.get("avg_lift_vs_technical_only") if row.get("avg_lift_vs_technical_only") is not None else 999.0,
    ) if family_rows else None
    ready = [row for row in family_rows if row.get("classification") == "candidate_helpful"]
    technical_baseline = _build_technical_baseline_diagnostic(frame, candidate_helpful_count=len(ready))
    promotion_readiness = build_promotion_readiness(family_rows)
    return {
        "status": "ok",
        "authority": "research_only",
        "authority_contract": family_report_authority_contract(),
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        "headline": (
            "At least one context source family shows positive cost-adjusted lift."
            if ready
            else "No context source family has enough positive evidence for promotion review."
        ),
        "evaluated_at": None if frame["evaluated_at"].dropna().empty else frame["evaluated_at"].max(),
        "min_matured_rows": int(min_matured_rows),
        "min_selected_rows": int(min_selected_rows),
        "min_lift_vs_technical_only": float(min_lift_vs_technical_only),
        "family_count": len(family_rows),
        "candidate_helpful_count": len(ready),
        "promotion_readiness": promotion_readiness,
        "technical_baseline": technical_baseline,
        "market_participation_diagnostic": technical_baseline,
        "best_family": best,
        "worst_family": worst,
        "families": family_rows,
        "next_actions": [
            "Run advisory.signal_quality_promotion only for families classified as candidate_helpful.",
            "Keep all source-family overlays review-only until promotion review and manual config diff review pass.",
            "Treat inconsistent_or_horizon_sensitive families as watch/context evidence only until rolling-window checks prove stability.",
            "Treat hurts_or_no_lift families as explanation-only evidence and do not add action influence rules.",
        ],
    }


def format_text_report(report: dict[str, Any]) -> str:
    participation = report.get("market_participation_diagnostic") or {}
    readiness = report.get("promotion_readiness") if isinstance(report.get("promotion_readiness"), dict) else {}
    lines = [
        f"status: {report.get('status')}",
        f"headline: {report.get('headline')}",
        f"evaluated_at: {report.get('evaluated_at')}",
        "authority: research_only; no policy or broker behavior changed",
        "promotion_readiness: {status}; candidates={candidates}; blocked={blocked}; low_data={low_data}".format(
            status=readiness.get("status"),
            candidates=readiness.get("candidate_helpful_families", []),
            blocked=readiness.get("benchmark_or_attribution_blocked_families", []),
            low_data=readiness.get("low_data_families", []),
        ),
        "market_participation: {interpretation}; avg_technical_baseline_after_cost={avg}".format(
            interpretation=participation.get("interpretation"),
            avg=participation.get("avg_baseline_return_after_cost"),
        ),
    ]
    for row in report.get("families") or []:
        lines.append(
            " - {family}: {classification}, avg_lift={lift}, matured={matured}, selected={selected}".format(
                family=row.get("source_family"),
                classification=row.get("classification"),
                lift=row.get("avg_lift_vs_technical_only"),
                matured=row.get("total_matured_count"),
                selected=row.get("total_selected_count"),
            )
        )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize realized outcomes for context-overlay source-family signal variants.")
    parser.add_argument("--evaluated-at")
    parser.add_argument("--horizons", type=int, nargs="*")
    parser.add_argument("--min-matured-rows", type=int, default=DEFAULT_MIN_MATURED_ROWS)
    parser.add_argument("--min-selected-rows", type=int, default=DEFAULT_MIN_SELECTED_ROWS)
    parser.add_argument("--min-lift-vs-technical-only", type=float, default=DEFAULT_MIN_LIFT_VS_TECHNICAL_ONLY)
    parser.add_argument("--format", choices=["json", "text"], default="json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        rows = load_family_summary(evaluated_at=args.evaluated_at, horizons=args.horizons)
        report = build_family_report(
            rows,
            min_matured_rows=int(args.min_matured_rows),
            min_selected_rows=int(args.min_selected_rows),
            min_lift_vs_technical_only=float(args.min_lift_vs_technical_only),
        )
    except Exception as exc:
        if not _is_missing_table_error(exc):
            raise
        report = build_unavailable_report(exc)
    if args.format == "text":
        print(format_text_report(report))
    else:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
