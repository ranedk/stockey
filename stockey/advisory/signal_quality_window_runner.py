from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Any

import pandas as pd

from advisory.fallback_telemetry import record_local_fallback_event
from advisory.signal_quality_evaluator import (
    DEFAULT_COST_BPS,
    DEFAULT_HORIZONS,
    DEFAULT_RETURN_THRESHOLD,
    evaluate_signal_quality,
    persist_outputs,
)
from advisory.signal_quality_family_report import build_family_report
from advisory.signal_quality_family_report import VARIANT_TO_SOURCE_FAMILY
from advisory.signal_quality_promotion import generate_promotion_review
from advisory.signal_quality_split_report import (
    DEFAULT_MIN_MATURED_ROWS as DEFAULT_SPLIT_MIN_MATURED_ROWS,
)
from advisory.signal_quality_split_report import DEFAULT_TOP_N as DEFAULT_SPLIT_TOP_N
from advisory.signal_quality_split_report import build_research_queue
from advisory.signal_quality_split_report import build_split_report, load_split_evidence
from utils.sync import parse_datetime_arg


DEFAULT_WINDOW_DAYS = 180
DEFAULT_STEP_DAYS = 90
DEFAULT_WINDOW_COUNT = 4
DEFAULT_MIN_STABLE_WINDOWS = 2
DEFAULT_MIN_STABLE_WINDOW_RATE = 0.50
SPLIT_DIAGNOSTIC_CLASSES = {
    "benchmark_or_attribution_blocked",
    "inconsistent_or_regime_sensitive",
    "one_window_candidate",
}
SOURCE_FAMILY_SPLIT_GUIDANCE: dict[str, dict[str, Any]] = {
    "announcement_context": {
        "primary_split_axes": ["event_class", "direction", "materiality_bucket", "sector_code"],
        "recommended_next_evaluator_variant": "technical_plus_announcement_context_by_event_class",
        "reason": "Official announcements mix very different event types; split order wins, regulatory notices, capital actions, and routine filings before trusting family-level lift.",
    },
    "exchange_context": {
        "primary_split_axes": ["event_type", "side", "participant_type", "sector_code"],
        "recommended_next_evaluator_variant": "technical_plus_exchange_context_by_event_type",
        "reason": "Exchange events combine insider deals, block/bulk deals, short pressure, and corporate actions; split by event type and side before promotion.",
    },
    "bhavcopy_context": {
        "primary_split_axes": ["deal_pressure", "turnover_class", "short_pressure", "circuit_risk"],
        "recommended_next_evaluator_variant": "technical_plus_bhavcopy_context_by_pressure_class",
        "reason": "Bhavcopy pressure can mean accumulation, distribution, churn, or circuit risk; split by pressure class before promotion.",
    },
    "theme_context": {
        "primary_split_axes": ["theme_id", "sector_code", "direction", "news_materiality"],
        "recommended_next_evaluator_variant": "technical_plus_theme_context_by_theme_sector",
        "reason": "Theme/news overlays are sector- and theme-sensitive; split by theme and affected sector before trusting family-level lift.",
    },
    "macro_context": {
        "primary_split_axes": ["macro_signal_id", "sector_code", "direction", "risk_level"],
        "recommended_next_evaluator_variant": "technical_plus_macro_context_by_signal_sector",
        "reason": "Macro shocks help some sectors and hurt others; split by macro signal and sector sensitivity instead of using one market regime.",
    },
    "trusted_context_rules": {
        "primary_split_axes": ["rule_id", "context_source_family", "action_policy_effect", "sector_code"],
        "recommended_next_evaluator_variant": "technical_after_trusted_context_rules_by_rule",
        "reason": "Trusted runtime rules should be evaluated by rule id and source family so one good rule does not hide another weak rule.",
    },
}


@dataclass(frozen=True)
class EvaluationWindow:
    label: str
    from_date: pd.Timestamp
    to_date: pd.Timestamp

    def as_dict(self) -> dict[str, str]:
        return {
            "label": self.label,
            "from_date": self.from_date.date().isoformat(),
            "to_date": self.to_date.date().isoformat(),
        }


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    return str(value)


def _normalize_date(value: Any) -> pd.Timestamp:
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"Invalid date: {value}")
    return parsed.normalize()


def build_rolling_windows(
    *,
    to_date: Any,
    window_days: int = DEFAULT_WINDOW_DAYS,
    step_days: int = DEFAULT_STEP_DAYS,
    window_count: int = DEFAULT_WINDOW_COUNT,
) -> list[EvaluationWindow]:
    if int(window_days) <= 0:
        raise ValueError("window_days must be positive")
    if int(step_days) <= 0:
        raise ValueError("step_days must be positive")
    if int(window_count) <= 0:
        raise ValueError("window_count must be positive")
    end_anchor = _normalize_date(to_date)
    windows: list[EvaluationWindow] = []
    for index in range(int(window_count)):
        end_date = end_anchor - pd.Timedelta(days=int(step_days) * (int(window_count) - index - 1))
        start_date = end_date - pd.Timedelta(days=int(window_days) - 1)
        windows.append(
            EvaluationWindow(
                label=f"rolling_{int(window_days)}d_{index + 1:02d}",
                from_date=start_date,
                to_date=end_date,
            )
        )
    return windows


def build_explicit_window(*, from_date: Any, to_date: Any) -> list[EvaluationWindow]:
    start = _normalize_date(from_date)
    end = _normalize_date(to_date)
    if start > end:
        raise ValueError("from_date must be on or before to_date")
    return [EvaluationWindow(label="explicit", from_date=start, to_date=end)]


def _summary_evaluated_at(summary: pd.DataFrame) -> pd.Timestamp | None:
    if summary.empty or "evaluated_at" not in summary.columns:
        return None
    parsed = pd.to_datetime(summary["evaluated_at"], utc=True, errors="coerce").dropna()
    if parsed.empty:
        return None
    return parsed.max()


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


def _classify_stability(
    *,
    candidate_windows: int,
    evaluated_windows: int,
    hurt_windows: int,
    negative_windows: int,
    inconsistent_windows: int,
    benchmark_beta_windows: int,
    needs_benchmark_attribution_windows: int,
    min_stable_windows: int,
    min_stable_window_rate: float,
) -> str:
    if evaluated_windows <= 0:
        return "no_evidence"
    candidate_rate = candidate_windows / evaluated_windows
    if (
        candidate_windows >= int(min_stable_windows)
        and candidate_rate >= float(min_stable_window_rate)
        and hurt_windows == 0
        and negative_windows == 0
        and inconsistent_windows == 0
        and benchmark_beta_windows == 0
        and needs_benchmark_attribution_windows == 0
    ):
        return "stable_candidate"
    if benchmark_beta_windows > 0 or needs_benchmark_attribution_windows > 0:
        return "benchmark_or_attribution_blocked"
    if inconsistent_windows > 0:
        return "inconsistent_or_regime_sensitive"
    if candidate_windows > 0 and (hurt_windows > 0 or negative_windows > 0):
        return "inconsistent_or_regime_sensitive"
    if candidate_windows > 0:
        return "one_window_candidate"
    return "needs_more_data_or_monitor"


def split_guidance_for_source_family(source_family: str, classification: str) -> dict[str, Any]:
    clean_family = str(source_family or "").strip()
    guidance = dict(SOURCE_FAMILY_SPLIT_GUIDANCE.get(clean_family) or {})
    if not guidance:
        guidance = {
            "primary_split_axes": ["context_class", "sector_code", "direction"],
            "recommended_next_evaluator_variant": f"technical_plus_{clean_family or 'context'}_by_context_class",
            "reason": "Split the source family by context class, sector, and direction before promotion.",
        }
    guidance.update(
        {
            "source_family": clean_family,
            "classification": str(classification or "").strip(),
            "authority": "research_only",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
        }
    )
    return guidance


def build_stability_report(
    window_results: list[dict[str, Any]],
    *,
    min_stable_windows: int = DEFAULT_MIN_STABLE_WINDOWS,
    min_stable_window_rate: float = DEFAULT_MIN_STABLE_WINDOW_RATE,
) -> dict[str, Any]:
    family_rows: dict[str, list[dict[str, Any]]] = {}
    for result in window_results:
        window = result.get("window") if isinstance(result.get("window"), dict) else {}
        report = result.get("family_report") if isinstance(result.get("family_report"), dict) else {}
        for family in report.get("families") or []:
            if not isinstance(family, dict):
                continue
            source_family = str(family.get("source_family") or "").strip()
            if not source_family:
                continue
            family_rows.setdefault(source_family, []).append(
                {
                    "window": window,
                    "classification": family.get("classification"),
                    "total_selected_count": _int(family.get("total_selected_count")),
                    "total_matured_count": _int(family.get("total_matured_count")),
                    "avg_lift_vs_technical_only": _number(family.get("avg_lift_vs_technical_only")),
                    "avg_forward_return_after_cost": _number(family.get("avg_forward_return_after_cost")),
                    "avg_excess_forward_return_after_cost": _number(family.get("avg_excess_forward_return_after_cost")),
                    "avg_excess_hit_rate_after_cost": _number(family.get("avg_excess_hit_rate_after_cost")),
                }
            )

    rows: list[dict[str, Any]] = []
    for source_family, observations in sorted(family_rows.items()):
        classifications = [str(item.get("classification") or "").strip() for item in observations]
        lifts = [item["avg_lift_vs_technical_only"] for item in observations if item.get("avg_lift_vs_technical_only") is not None]
        returns = [item["avg_forward_return_after_cost"] for item in observations if item.get("avg_forward_return_after_cost") is not None]
        candidate_count = classifications.count("candidate_helpful")
        hurt_count = classifications.count("hurts_or_no_lift")
        negative_count = classifications.count("negative_after_cost")
        inconsistent_count = classifications.count("inconsistent_or_horizon_sensitive")
        benchmark_beta_count = classifications.count("benchmark_beta_not_overlay_alpha")
        needs_benchmark_count = classifications.count("needs_benchmark_attribution")
        evaluated_count = len(observations)
        excess_returns = [
            item["avg_excess_forward_return_after_cost"]
            for item in observations
            if item.get("avg_excess_forward_return_after_cost") is not None
        ]
        excess_hit_rates = [
            item["avg_excess_hit_rate_after_cost"]
            for item in observations
            if item.get("avg_excess_hit_rate_after_cost") is not None
        ]
        rows.append(
            {
                "source_family": source_family,
                "classification": _classify_stability(
                    candidate_windows=candidate_count,
                    evaluated_windows=evaluated_count,
                    hurt_windows=hurt_count,
                    negative_windows=negative_count,
                    inconsistent_windows=inconsistent_count,
                    benchmark_beta_windows=benchmark_beta_count,
                    needs_benchmark_attribution_windows=needs_benchmark_count,
                    min_stable_windows=min_stable_windows,
                    min_stable_window_rate=min_stable_window_rate,
                ),
                "window_count": evaluated_count,
                "candidate_helpful_window_count": candidate_count,
                "candidate_helpful_window_rate": None if evaluated_count == 0 else candidate_count / evaluated_count,
                "hurt_or_no_lift_window_count": hurt_count,
                "negative_after_cost_window_count": negative_count,
                "inconsistent_or_horizon_sensitive_window_count": inconsistent_count,
                "benchmark_beta_not_overlay_alpha_window_count": benchmark_beta_count,
                "needs_benchmark_attribution_window_count": needs_benchmark_count,
                "needs_more_data_window_count": classifications.count("needs_more_data"),
                "monitor_window_count": classifications.count("monitor"),
                "avg_lift_vs_technical_only": None if not lifts else sum(lifts) / len(lifts),
                "min_lift_vs_technical_only": None if not lifts else min(lifts),
                "max_lift_vs_technical_only": None if not lifts else max(lifts),
                "avg_forward_return_after_cost": None if not returns else sum(returns) / len(returns),
                "avg_excess_forward_return_after_cost": None if not excess_returns else sum(excess_returns) / len(excess_returns),
                "min_excess_forward_return_after_cost": None if not excess_returns else min(excess_returns),
                "avg_excess_hit_rate_after_cost": None if not excess_hit_rates else sum(excess_hit_rates) / len(excess_hit_rates),
                "windows": observations,
            }
        )
    rows.sort(
        key=lambda row: (
            row["classification"] == "stable_candidate",
            row["candidate_helpful_window_count"],
            row["avg_lift_vs_technical_only"] if row.get("avg_lift_vs_technical_only") is not None else -999.0,
        ),
        reverse=True,
    )
    stable = [row for row in rows if row.get("classification") == "stable_candidate"]
    inconsistent = [row for row in rows if row.get("classification") == "inconsistent_or_regime_sensitive"]
    benchmark_blocked = [row for row in rows if row.get("classification") == "benchmark_or_attribution_blocked"]
    return {
        "status": "ok",
        "authority": "research_only",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        "min_stable_windows": int(min_stable_windows),
        "min_stable_window_rate": float(min_stable_window_rate),
        "family_count": len(rows),
        "stable_candidate_count": len(stable),
        "inconsistent_count": len(inconsistent),
        "benchmark_or_attribution_blocked_count": len(benchmark_blocked),
        "stable_families": stable,
        "inconsistent_families": inconsistent,
        "benchmark_or_attribution_blocked_families": benchmark_blocked,
        "families": rows,
        "headline": (
            "At least one context family is repeatedly helpful across windows."
            if stable
            else "No context family is repeatedly helpful across windows yet."
        ),
        "next_actions": [
            "Only consider trusted influence for stable_candidate families after manual review.",
            "Treat one_window_candidate families as watch/review-only evidence until another window confirms lift.",
            "Treat benchmark_or_attribution_blocked families as research-only until every otherwise-helpful window has benchmark-excess attribution.",
            "Do not promote inconsistent_or_regime_sensitive families without a narrower source/regime split.",
        ],
    }


def build_promotion_gate(stability_report: dict[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for family in stability_report.get("families") or []:
        if not isinstance(family, dict):
            continue
        source_family = str(family.get("source_family") or "").strip()
        classification = str(family.get("classification") or "").strip()
        eligible = classification == "stable_candidate"
        if eligible:
            decision = "eligible_for_manual_promotion_review"
            reason = "Family is stable_candidate across the configured rolling windows."
        elif classification == "one_window_candidate":
            decision = "hold_for_more_windows"
            reason = "Family was helpful in only one window; keep as research/watch evidence."
        elif classification == "inconsistent_or_regime_sensitive":
            decision = "do_not_promote_without_narrower_split"
            reason = "Family has both helpful and harmful/negative windows; split by event class, sector, or market context before review."
        elif classification == "benchmark_or_attribution_blocked":
            decision = "do_not_promote_without_benchmark_attribution"
            reason = "Family has at least one window that is benchmark-beta-only or lacks benchmark-excess attribution; raw positive windows are not enough."
        else:
            decision = "needs_more_data"
            reason = "Family does not have enough stable positive after-cost evidence across windows."
        rows.append(
            {
                "source_family": source_family,
                "stability_classification": classification,
                "eligible_for_promotion_review": eligible,
                "decision": decision,
                "reason": reason,
                "split_guidance": split_guidance_for_source_family(source_family, classification)
                if classification in SPLIT_DIAGNOSTIC_CLASSES
                else None,
                "candidate_helpful_window_count": _int(family.get("candidate_helpful_window_count")),
                "window_count": _int(family.get("window_count")),
                "candidate_helpful_window_rate": _number(family.get("candidate_helpful_window_rate")),
                "avg_lift_vs_technical_only": _number(family.get("avg_lift_vs_technical_only")),
                "min_lift_vs_technical_only": _number(family.get("min_lift_vs_technical_only")),
                "avg_excess_forward_return_after_cost": _number(family.get("avg_excess_forward_return_after_cost")),
                "min_excess_forward_return_after_cost": _number(family.get("min_excess_forward_return_after_cost")),
                "avg_excess_hit_rate_after_cost": _number(family.get("avg_excess_hit_rate_after_cost")),
                "benchmark_beta_not_overlay_alpha_window_count": _int(family.get("benchmark_beta_not_overlay_alpha_window_count")),
                "needs_benchmark_attribution_window_count": _int(family.get("needs_benchmark_attribution_window_count")),
                "authority": "manual_config_review_only" if eligible else "research_only",
                "broker_execution_allowed": False,
                "policy_auto_promotion_allowed": False,
            }
        )
    eligible_rows = [row for row in rows if row.get("eligible_for_promotion_review")]
    return {
        "status": "ok",
        "authority": "research_only",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        "eligible_family_count": len(eligible_rows),
        "eligible_families": [row["source_family"] for row in eligible_rows],
        "families": rows,
        "headline": (
            "Stable context families are eligible for manual promotion review."
            if eligible_rows
            else "No context family is eligible for manual promotion review."
        ),
    }


def build_split_diagnostics(
    stability_report: dict[str, Any],
    *,
    evaluated_at: Any | None = None,
    horizons: list[int] | None = None,
    min_matured_rows: int = DEFAULT_SPLIT_MIN_MATURED_ROWS,
    top_n: int = DEFAULT_SPLIT_TOP_N,
) -> dict[str, Any]:
    reports: dict[str, Any] = {}
    skipped: list[dict[str, Any]] = []
    aggregate_recommendations: list[dict[str, Any]] = []
    for family in stability_report.get("families") or []:
        if not isinstance(family, dict):
            continue
        source_family = str(family.get("source_family") or "").strip()
        classification = str(family.get("classification") or "").strip()
        if not source_family or classification not in SPLIT_DIAGNOSTIC_CLASSES:
            continue
        try:
            evidence = load_split_evidence(
                evaluated_at=evaluated_at,
                horizons=horizons,
                source_family=source_family,
            )
            report = build_split_report(
                evidence,
                min_matured_rows=int(min_matured_rows),
                top_n=int(top_n),
            )
            report["source_family"] = source_family
            report["stability_classification"] = classification
            report["split_guidance"] = split_guidance_for_source_family(source_family, classification)
            for recommendation in report.get("research_recommendations") or []:
                if not isinstance(recommendation, dict):
                    continue
                aggregate_recommendations.append(
                    {
                        **recommendation,
                        "stability_classification": classification,
                    }
                )
            reports[source_family] = report
        except Exception as exc:
            record_local_fallback_event(
                module="advisory.signal_quality_window_runner",
                fallback_type="signal_quality_window_runner_split_diagnostic_failed",
                source="signal_quality_split_diagnostics",
                severity="warn",
                reason="Signal-quality window runner could not build split diagnostics for a source family.",
                error=exc,
                metadata={"source_family": source_family, "stability_classification": classification},
            )
            skipped.append(
                {
                    "source_family": source_family,
                    "stability_classification": classification,
                    "reason": f"split_diagnostic_failed:{exc.__class__.__name__}",
                    "error": str(exc)[:500],
                }
            )
    candidate_reports = [
        report
        for report in reports.values()
        if isinstance(report, dict) and int(report.get("candidate_split_helpful_count") or 0) > 0
    ]
    research_queue = build_research_queue(aggregate_recommendations)
    return {
        "status": "ok",
        "mode": "unstable_family_split_diagnostics",
        "authority": "research_only",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        "evaluated_at": evaluated_at,
        "horizons": [int(value) for value in horizons] if horizons else None,
        "family_count": len(reports),
        "candidate_report_count": len(candidate_reports),
        "research_recommendation_count": len(aggregate_recommendations),
        "research_queue": research_queue,
        "reports": reports,
        "skipped": skipped,
        "next_actions": [
            "Use these split diagnostics only to design narrower evaluator variants or hypotheses.",
            "Do not promote a whole source family when only a split looks useful.",
            "Keep any split-derived influence research-only until it passes separate out-of-sample window validation.",
        ],
    }


def build_preflight_report(result: dict[str, Any]) -> dict[str, Any]:
    stability = result.get("stability_report") if isinstance(result.get("stability_report"), dict) else {}
    promotion_gate = result.get("promotion_gate") if isinstance(result.get("promotion_gate"), dict) else {}
    windows: list[dict[str, Any]] = []
    windows_with_summary = 0
    windows_with_candidates = 0
    for item in result.get("windows") or []:
        if not isinstance(item, dict):
            continue
        family_report = item.get("family_report") if isinstance(item.get("family_report"), dict) else {}
        summary_rows = _int(item.get("summary_rows"))
        candidate_count = _int(family_report.get("candidate_helpful_count"))
        if summary_rows > 0:
            windows_with_summary += 1
        if candidate_count > 0:
            windows_with_candidates += 1
        windows.append(
            {
                "window": item.get("window"),
                "evaluation_rows": _int(item.get("evaluation_rows")),
                "summary_rows": summary_rows,
                "candidate_helpful_count": candidate_count,
                "family_count": _int(family_report.get("family_count")),
                "headline": family_report.get("headline"),
            }
        )

    stable_count = _int(stability.get("stable_candidate_count"))
    eligible_count = _int(promotion_gate.get("eligible_family_count"))
    if stable_count > 0 and eligible_count > 0:
        decision = "ready_for_persisted_stable_review_run"
        reason = "At least one context source family is stable across windows and eligible for review-row creation."
    elif windows_with_candidates > 0:
        decision = "hold_for_more_windows_or_narrower_split"
        reason = "Some windows have helpful families, but none are stable enough across windows for review-row creation."
    elif windows_with_summary > 0:
        decision = "coverage_available_no_helpful_family"
        reason = "Signal-quality coverage exists, but no source family is repeatedly helpful after costs."
    else:
        decision = "insufficient_window_coverage"
        reason = "No evaluated window produced enough summary evidence for a stable-family decision."

    return {
        "status": "ok",
        "mode": "multi_window_signal_quality_preflight",
        "decision": decision,
        "reason": reason,
        "ready_for_persisted_window_run": decision == "ready_for_persisted_stable_review_run",
        "window_count": len(windows),
        "windows_with_summary": windows_with_summary,
        "windows_with_candidate_families": windows_with_candidates,
        "stable_candidate_family_count": stable_count,
        "eligible_family_count": eligible_count,
        "stable_families": [
            row.get("source_family")
            for row in stability.get("stable_families") or []
            if isinstance(row, dict) and row.get("source_family")
        ],
        "windows": windows,
        "authority": "research_only",
        "dry_run": True,
        "persisted": False,
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        "next_actions": [
            "Run persisted window evaluation only when this preflight says ready_for_persisted_window_run=true.",
            "Keep one-window or inconsistent families as watch/research evidence, not action-policy influence.",
            "Do not enable trusted context rules until stable-family review rows are inspected and config changes are explicitly applied.",
        ],
    }


def run_window(
    window: EvaluationWindow,
    *,
    horizons: list[int],
    return_threshold: float = DEFAULT_RETURN_THRESHOLD,
    cost_bps: float = DEFAULT_COST_BPS,
    min_matured_rows: int = 10,
    dry_run: bool = False,
) -> dict[str, Any]:
    evaluations, summary, meta = evaluate_signal_quality(
        from_date=window.from_date,
        to_date=window.to_date,
        horizons=horizons,
        return_threshold=float(return_threshold),
        cost_bps=float(cost_bps),
        min_matured_rows=int(min_matured_rows),
    )
    if not dry_run:
        persist_outputs(evaluations, summary)
    family_report = build_family_report(summary, min_matured_rows=int(min_matured_rows))
    evaluated_at = _summary_evaluated_at(summary)
    return {
        "window": window.as_dict(),
        "evaluated_at": evaluated_at,
        "evaluation_rows": int(len(evaluations)),
        "summary_rows": int(len(summary)),
        "meta": meta,
        "family_report": family_report,
        "promotion_review": None,
        "dry_run": bool(dry_run),
        "persisted": not bool(dry_run),
        "authority": "research_only",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
    }


def generate_stable_family_promotion_reviews(
    window_results: list[dict[str, Any]],
    stability_report: dict[str, Any],
    *,
    persist: bool = True,
) -> dict[str, Any]:
    stable_families = {
        str(row.get("source_family") or "").strip()
        for row in stability_report.get("stable_families") or []
        if isinstance(row, dict) and str(row.get("source_family") or "").strip()
    }
    reviews: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    if not stable_families:
        return {
            "status": "no_stable_families",
            "mode": "stable_family_candidates_only",
            "persisted": bool(persist),
            "review_count": 0,
            "reviews": [],
            "skipped": [],
            "authority": "manual_config_review_only",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
        }
    for result in window_results:
        evaluated_at = result.get("evaluated_at")
        family_report = result.get("family_report") if isinstance(result.get("family_report"), dict) else {}
        if evaluated_at is None:
            continue
        for family in family_report.get("families") or []:
            if not isinstance(family, dict):
                continue
            source_family = str(family.get("source_family") or "").strip()
            if source_family not in stable_families:
                if source_family:
                    skipped.append(
                        {
                            "source_family": source_family,
                            "reason": "not_stable_candidate",
                            "window": result.get("window"),
                        }
                    )
                continue
            for horizon in family.get("horizons") or []:
                if not isinstance(horizon, dict):
                    continue
                if horizon.get("classification") != "candidate_helpful":
                    continue
                variant = str(horizon.get("variant") or "").strip()
                horizon_days = int(horizon.get("horizon_days") or 0)
                if not variant or horizon_days <= 0:
                    continue
                if VARIANT_TO_SOURCE_FAMILY.get(variant) != source_family:
                    skipped.append(
                        {
                            "source_family": source_family,
                            "variant": variant,
                            "horizon_days": horizon_days,
                            "reason": "variant_family_mismatch",
                            "window": result.get("window"),
                        }
                    )
                    continue
                reviews.append(
                    generate_promotion_review(
                        evaluated_at=evaluated_at,
                        horizon_days=horizon_days,
                        variant=variant,
                        persist=persist,
                    )
                )
    return {
        "status": "ok",
        "mode": "stable_family_candidates_only",
        "persisted": bool(persist),
        "stable_families": sorted(stable_families),
        "review_count": len(reviews),
        "reviews": reviews,
        "skipped": skipped[:50],
        "authority": "manual_config_review_only",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        "note": "Promotion review rows were generated only for families classified as stable_candidate across windows.",
    }


def run_windows(
    windows: list[EvaluationWindow],
    *,
    horizons: list[int],
    return_threshold: float = DEFAULT_RETURN_THRESHOLD,
    cost_bps: float = DEFAULT_COST_BPS,
    min_matured_rows: int = 10,
    min_stable_windows: int = DEFAULT_MIN_STABLE_WINDOWS,
    min_stable_window_rate: float = DEFAULT_MIN_STABLE_WINDOW_RATE,
    dry_run: bool = False,
    auto_promotion: bool = True,
    include_split_reports: bool = False,
    split_report_min_matured_rows: int = DEFAULT_SPLIT_MIN_MATURED_ROWS,
    split_report_top_n: int = DEFAULT_SPLIT_TOP_N,
) -> dict[str, Any]:
    results = [
        run_window(
            window,
            horizons=horizons,
            return_threshold=return_threshold,
            cost_bps=cost_bps,
            min_matured_rows=min_matured_rows,
            dry_run=dry_run,
        )
        for window in windows
    ]
    candidate_windows = [
        result
        for result in results
        if int(((result.get("family_report") or {}).get("candidate_helpful_count") or 0)) > 0
    ]
    stability_report = build_stability_report(
        results,
        min_stable_windows=int(min_stable_windows),
        min_stable_window_rate=float(min_stable_window_rate),
    )
    promotion_gate = build_promotion_gate(stability_report)
    promotion_review = None
    if auto_promotion and not dry_run:
        promotion_review = generate_stable_family_promotion_reviews(
            results,
            stability_report,
            persist=True,
        )
    split_diagnostics = None
    if include_split_reports:
        if dry_run:
            split_diagnostics = {
                "status": "skipped",
                "reason": "split diagnostics require persisted evaluation rows; dry-run/preflight mode does not write them",
                "authority": "research_only",
                "broker_execution_allowed": False,
                "policy_auto_promotion_allowed": False,
            }
        else:
            evaluated_ats = [
                pd.to_datetime(result.get("evaluated_at"), utc=True, errors="coerce")
                for result in results
                if result.get("evaluated_at") is not None
            ]
            valid_evaluated_ats = [value for value in evaluated_ats if not pd.isna(value)]
            latest_evaluated_at = max(valid_evaluated_ats) if valid_evaluated_ats else None
            split_diagnostics = build_split_diagnostics(
                stability_report,
                evaluated_at=latest_evaluated_at,
                horizons=horizons,
                min_matured_rows=int(split_report_min_matured_rows),
                top_n=int(split_report_top_n),
            )
    return {
        "status": "ok",
        "mode": "multi_window_signal_quality",
        "window_count": len(results),
        "candidate_window_count": len(candidate_windows),
        "stable_candidate_family_count": int(stability_report.get("stable_candidate_count") or 0),
        "stability_report": stability_report,
        "promotion_gate": promotion_gate,
        "windows": results,
        "dry_run": bool(dry_run),
        "persisted": not bool(dry_run),
        "auto_promotion_requested": bool(auto_promotion),
        "auto_promotion_executed": bool(auto_promotion and not dry_run),
        "stable_family_promotion_review": promotion_review,
        "split_diagnostics_requested": bool(include_split_reports),
        "split_diagnostics": split_diagnostics,
        "authority": "research_only",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        "next_actions": [
            "Compare candidate_helpful families across windows before trusting any context-family influence.",
            "Only consider trusted review rules when a family is repeatedly positive after costs with enough matured rows.",
            "Keep context overlays watch/review-only until separate config review and action-policy gates are approved.",
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run signal-quality evaluation across multiple historical windows before considering context-family influence."
    )
    parser.add_argument("--from-date", type=parse_datetime_arg)
    parser.add_argument("--to-date", type=parse_datetime_arg, required=True)
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument("--step-days", type=int, default=DEFAULT_STEP_DAYS)
    parser.add_argument("--windows", type=int, default=DEFAULT_WINDOW_COUNT)
    parser.add_argument("--horizons", nargs="*", type=int, default=DEFAULT_HORIZONS)
    parser.add_argument("--return-threshold", type=float, default=DEFAULT_RETURN_THRESHOLD)
    parser.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    parser.add_argument("--min-matured-rows", type=int, default=10)
    parser.add_argument("--min-stable-windows", type=int, default=DEFAULT_MIN_STABLE_WINDOWS)
    parser.add_argument("--min-stable-window-rate", type=float, default=DEFAULT_MIN_STABLE_WINDOW_RATE)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-auto-promotion", action="store_true")
    parser.add_argument(
        "--include-split-reports",
        action="store_true",
        help="Attach research-only context-class/direction split diagnostics for unstable source families. Requires persisted rows.",
    )
    parser.add_argument("--split-report-min-matured-rows", type=int, default=DEFAULT_SPLIT_MIN_MATURED_ROWS)
    parser.add_argument("--split-report-top-n", type=int, default=DEFAULT_SPLIT_TOP_N)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.from_date is not None:
        windows = build_explicit_window(from_date=args.from_date, to_date=args.to_date)
    else:
        windows = build_rolling_windows(
            to_date=args.to_date,
            window_days=int(args.window_days),
            step_days=int(args.step_days),
            window_count=int(args.windows),
        )
    result = run_windows(
        windows,
        horizons=[int(value) for value in args.horizons],
        return_threshold=float(args.return_threshold),
        cost_bps=float(args.cost_bps),
        min_matured_rows=int(args.min_matured_rows),
        min_stable_windows=int(args.min_stable_windows),
        min_stable_window_rate=float(args.min_stable_window_rate),
        dry_run=bool(args.dry_run or args.preflight_only),
        auto_promotion=not bool(args.skip_auto_promotion or args.preflight_only),
        include_split_reports=bool(args.include_split_reports),
        split_report_min_matured_rows=int(args.split_report_min_matured_rows),
        split_report_top_n=int(args.split_report_top_n),
    )
    if args.preflight_only:
        result = build_preflight_report(result)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
