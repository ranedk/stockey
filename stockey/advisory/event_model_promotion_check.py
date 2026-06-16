from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import pandas as pd

from advisory import event_meta_model
from advisory.fallback_telemetry import record_local_fallback_event
from utils.db import sql_to_df


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG_PATH = REPO_ROOT / "logs" / "cron" / "all_ml.log"


MARKER_RE = re.compile(
    r"\[stockey\.script\]\s+name=all_ml\s+status=(?P<status>\w+)(?:\s+exit_code=(?P<exit_code>\d+))?\s+timestamp=(?P<timestamp>\S+)"
)


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _metric_number(metrics: dict[str, Any], key: str) -> float | None:
    value = metrics.get(key)
    if value is None:
        return None
    numeric = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric):
        return None
    return float(numeric)


def _int_or_zero(value: Any) -> int:
    numeric = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric):
        return 0
    return int(numeric)


def _record_event_model_promotion_fallback(
    *,
    fallback_type: str,
    source: str,
    reason: str,
    error: Exception,
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="advisory.event_model_promotion_check",
        fallback_type=fallback_type,
        source=source,
        severity="warn",
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def _check_gte(name: str, value: float | int | None, threshold: float | int, details: list[dict[str, Any]]) -> bool:
    passed = value is not None and float(value) >= float(threshold)
    details.append(
        {
            "gate": name,
            "passed": bool(passed),
            "value": value,
            "threshold": threshold,
        }
    )
    return bool(passed)


def _compact_json_text(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=_json_default).lower()
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.event_model_promotion_check",
            source="research_control_text",
            fallback_type="json_compaction_failed",
            severity="warn",
            reason="Event model promotion check could not serialize research-control metadata and used string fallback.",
            error=exc,
            metadata={"value_type": type(value).__name__},
        )
        return str(value).lower()


def _first_present_mapping_value(mapping: dict[str, Any], keys: list[str]) -> tuple[str | None, Any]:
    for key in keys:
        if key in mapping and mapping.get(key) not in (None, "", [], {}):
            return key, mapping.get(key)
    return None, None


def _find_research_control(metadata: dict[str, Any], aliases: list[str]) -> tuple[str | None, Any]:
    source, value = _first_present_mapping_value(metadata, aliases)
    if source:
        return source, value
    for container_key in (
        "research_controls",
        "research_safety",
        "safety_controls",
        "validation",
        "validation_controls",
        "promotion_evidence",
        "promotion_controls",
    ):
        container = metadata.get(container_key)
        if isinstance(container, dict):
            source, value = _first_present_mapping_value(container, aliases)
            if source:
                return f"{container_key}.{source}", value
    return None, None


def _explicit_pass_value(evidence: Any) -> bool | None:
    if isinstance(evidence, bool):
        return bool(evidence)
    if isinstance(evidence, dict):
        for key in ("passed", "pass", "ok", "validated", "enabled"):
            if key in evidence:
                value = evidence.get(key)
                if isinstance(value, bool):
                    return bool(value)
                if isinstance(value, str):
                    text = value.strip().lower()
                    if text in {"true", "yes", "y", "pass", "passed", "ok", "valid", "validated"}:
                        return True
                    if text in {"false", "no", "n", "fail", "failed", "invalid"}:
                        return False
    if isinstance(evidence, str):
        text = evidence.strip().lower()
        if text in {"true", "yes", "y", "pass", "passed", "ok", "valid", "validated"}:
            return True
        if text in {"false", "no", "n", "fail", "failed", "invalid"}:
            return False
    return None


def _research_control_summary(
    *,
    metadata: dict[str, Any],
    name: str,
    aliases: list[str],
    required_markers: list[str],
    missing_reason: str,
) -> dict[str, Any]:
    source, evidence = _find_research_control(metadata, aliases)
    if source is None:
        return {
            "passed": False,
            "source": None,
            "evidence": None,
            "reason": missing_reason,
        }
    explicit = _explicit_pass_value(evidence)
    if explicit is not None:
        return {
            "passed": bool(explicit),
            "source": source,
            "evidence": evidence,
            "reason": "explicit pass marker" if explicit else "explicit fail marker",
        }
    evidence_text = _compact_json_text(evidence)
    matched = [marker for marker in required_markers if marker in evidence_text]
    return {
        "passed": bool(matched),
        "source": source,
        "evidence": evidence,
        "reason": f"matched markers: {', '.join(matched)}" if matched else f"{name} evidence lacks required validation markers",
    }


def summarize_research_safety_controls(metadata: dict[str, Any] | None) -> dict[str, Any]:
    meta = metadata or {}
    return {
        "leakage_control": _research_control_summary(
            metadata=meta,
            name="leakage_control",
            aliases=[
                "leakage_control",
                "leakage_controls",
                "point_in_time_validation",
                "point_in_time_controls",
                "no_lookahead_validation",
            ],
            required_markers=["point_in_time", "point-in-time", "purged", "embargo", "cpcv", "no_lookahead", "no-lookahead", "causal"],
            missing_reason="missing point-in-time/leakage-control validation evidence",
        ),
        "false_discovery_control": _research_control_summary(
            metadata=meta,
            name="false_discovery_control",
            aliases=[
                "false_discovery_control",
                "false_discovery_controls",
                "multiple_testing_control",
                "research_ledger_control",
                "trial_ledger",
            ],
            required_markers=["research_ledger", "ledger", "false_discovery", "fdr", "multiple_testing", "q_value", "q-value", "trial_count", "cpcv"],
            missing_reason="missing false-discovery/research-ledger validation evidence",
        ),
        "cost_adjusted_baseline": _research_control_summary(
            metadata=meta,
            name="cost_adjusted_baseline",
            aliases=[
                "cost_adjusted_baseline",
                "cost_adjusted_baselines",
                "transaction_cost_baseline",
                "after_cost_baseline",
                "baseline_after_costs",
            ],
            required_markers=[
                "cost_adjusted",
                "cost-adjusted",
                "transaction_cost",
                "after_cost",
                "after-cost",
                "slippage",
                "brokerage",
                "passive_baseline",
                "baseline",
            ],
            missing_reason="missing transaction-cost-adjusted baseline evidence",
        ),
    }


def load_artifact_metadata(artifact_dir: Path, model_basename: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    model_path, meta_path = event_meta_model._artifact_paths(artifact_dir, model_basename)
    info = {
        "artifact_dir": str(artifact_dir),
        "model_path": str(model_path),
        "meta_path": str(meta_path),
        "model_exists": model_path.exists(),
        "meta_exists": meta_path.exists(),
        "meta_mtime": pd.Timestamp(meta_path.stat().st_mtime, unit="s", tz="UTC").isoformat() if meta_path.exists() else None,
    }
    if not model_path.exists() or not meta_path.exists():
        return None, info
    return event_meta_model.load_model_metadata(artifact_dir, model_basename), info


def summarize_successful_runs(log_path: Path, *, since_days: int) -> dict[str, Any]:
    cutoff = pd.Timestamp.utcnow() - pd.Timedelta(days=int(since_days))
    if not log_path.exists():
        return {
            "log_path": str(log_path),
            "exists": False,
            "since_days": int(since_days),
            "successful_runs": 0,
            "latest_status": None,
            "latest_timestamp": None,
        }
    successful = 0
    latest_status = None
    latest_timestamp = None
    latest_exit_code = None
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = MARKER_RE.search(line)
        if not match:
            continue
        ts = pd.to_datetime(match.group("timestamp"), utc=True, errors="coerce")
        if pd.isna(ts):
            continue
        status = match.group("status")
        latest_status = status
        latest_timestamp = ts.isoformat()
        latest_exit_code = match.group("exit_code")
        if status == "done" and ts >= cutoff:
            successful += 1
    return {
        "log_path": str(log_path),
        "exists": True,
        "since_days": int(since_days),
        "successful_runs": int(successful),
        "latest_status": latest_status,
        "latest_exit_code": latest_exit_code,
        "latest_timestamp": latest_timestamp,
    }


def summarize_label_coverage(*, horizon_days: int, return_threshold: float) -> dict[str, Any]:
    try:
        dataset = event_meta_model.build_labeled_event_dataset(
            horizon_days=int(horizon_days),
            return_threshold=float(return_threshold),
        )
    except Exception as exc:
        _record_event_model_promotion_fallback(
            fallback_type="event_model_promotion_label_coverage_load_failed",
            source="event_meta_model_labeled_dataset",
            reason="Event-model promotion check could not build labeled event coverage evidence.",
            error=exc,
            metadata={"horizon_days": int(horizon_days), "return_threshold": float(return_threshold)},
        )
        raise
    if dataset.empty or "target_label" not in dataset.columns:
        return {
            "horizon_days": int(horizon_days),
            "dataset_rows": int(len(dataset)),
            "labeled_rows": 0,
            "date_count": 0,
            "symbol_count": 0,
            "event_class_count": 0,
            "positive_rate": None,
        }
    labeled = dataset.dropna(subset=["target_label"]).copy()
    date_count = 0
    if "published_on" in labeled.columns and not labeled.empty:
        date_count = int(pd.to_datetime(labeled["published_on"], utc=True, errors="coerce").dt.date.nunique())
    return {
        "horizon_days": int(horizon_days),
        "dataset_rows": int(len(dataset)),
        "labeled_rows": int(len(labeled)),
        "date_count": date_count,
        "symbol_count": int(labeled["symbol"].astype("string").str.upper().nunique()) if "symbol" in labeled.columns and not labeled.empty else 0,
        "event_class_count": int(labeled["event_class"].astype("string").nunique()) if "event_class" in labeled.columns and not labeled.empty else 0,
        "positive_rate": round(float(pd.to_numeric(labeled["target_label"], errors="coerce").mean()), 6) if not labeled.empty else None,
    }


def summarize_score_freshness(*, model_name: str | None, model_version: str | None, max_score_age_days: int) -> dict[str, Any]:
    try:
        if not event_meta_model.table_exists(event_meta_model.SCORES_TABLE):
            return {"table_exists": False, "score_rows": 0, "latest_scored_at": None, "age_days": None, "fresh": False}
        clauses = ["1 = 1"]
        params: list[object] = []
        if model_name:
            clauses.append("model_name = %s")
            params.append(model_name)
        if model_version:
            clauses.append("model_version = %s")
            params.append(model_version)
        df = sql_to_df(
            f"""
            SELECT COUNT(*) AS score_rows, MAX(scored_at) AS latest_scored_at
            FROM {event_meta_model.SCORES_TABLE}
            WHERE {' AND '.join(clauses)}
            """,
            params=tuple(params) if params else None,
        )
    except Exception as exc:
        _record_event_model_promotion_fallback(
            fallback_type="event_model_promotion_score_freshness_load_failed",
            source=event_meta_model.SCORES_TABLE,
            reason="Event-model promotion check could not load latest model score freshness evidence.",
            error=exc,
            metadata={
                "model_name": model_name,
                "model_version": model_version,
                "max_score_age_days": int(max_score_age_days),
            },
        )
        return {
            "table_exists": None,
            "score_rows": 0,
            "latest_scored_at": None,
            "age_days": None,
            "fresh": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    if df.empty:
        return {"table_exists": True, "score_rows": 0, "latest_scored_at": None, "age_days": None, "fresh": False}
    score_rows = _int_or_zero(df.iloc[0].get("score_rows"))
    latest = pd.to_datetime(df.iloc[0].get("latest_scored_at"), utc=True, errors="coerce")
    if pd.isna(latest):
        return {"table_exists": True, "score_rows": score_rows, "latest_scored_at": None, "age_days": None, "fresh": False}
    age_days = (pd.Timestamp.utcnow() - latest).total_seconds() / 86400.0
    return {
        "table_exists": True,
        "score_rows": score_rows,
        "latest_scored_at": latest.isoformat(),
        "age_days": round(float(age_days), 3),
        "fresh": bool(age_days <= float(max_score_age_days)),
    }


def build_research_scorecard(
    *,
    decision: str,
    gates: list[dict[str, Any]],
    metrics: dict[str, Any],
    coverage: dict[str, Any],
    weekly_runs: dict[str, Any],
    score_freshness: dict[str, Any],
    research_safety: dict[str, Any],
) -> dict[str, Any]:
    failed_gates = [str(gate.get("gate")) for gate in gates if not gate.get("passed")]
    ready = decision == "review_candidate" and not failed_gates
    precision = _metric_number(metrics, "precision")
    positive_rate_test = _metric_number(metrics, "positive_rate_test")
    precision_lift = None
    if precision is not None and positive_rate_test is not None:
        precision_lift = round(float(precision - positive_rate_test), 6)
    usability_status = "usable_for_manual_review" if ready else "not_usable"
    if ready:
        operator_action = "Review manually as a low-weight research input; do not auto-promote."
        headline = "Model evidence passed the configured research gates."
    else:
        operator_action = "Keep model research-only; improve labels, scores, weekly runs, or validation evidence before review."
        headline = "Model evidence is not usable for operator review yet."
    return {
        "status": usability_status,
        "usable": bool(ready),
        "headline": headline,
        "operator_action": operator_action,
        "authority": "research_only_manual_review",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        "failed_gate_count": len(failed_gates),
        "failed_gates": failed_gates,
        "key_metrics": {
            "train_rows": _metric_number(metrics, "train_rows"),
            "test_rows": _metric_number(metrics, "test_rows"),
            "precision": precision,
            "positive_rate_test": positive_rate_test,
            "precision_lift_vs_positive_rate_test": precision_lift,
            "roc_auc": _metric_number(metrics, "roc_auc"),
            "labeled_rows": coverage.get("labeled_rows"),
            "date_count": coverage.get("date_count"),
            "symbol_count": coverage.get("symbol_count"),
            "event_class_count": coverage.get("event_class_count"),
            "successful_runs": weekly_runs.get("successful_runs"),
            "score_rows": score_freshness.get("score_rows"),
            "score_age_days": score_freshness.get("age_days"),
        },
        "research_safety": {
            key: {
                "passed": bool((value or {}).get("passed")),
                "source": (value or {}).get("source"),
                "reason": (value or {}).get("reason"),
            }
            for key, value in (research_safety or {}).items()
        },
    }


def build_promotion_check(args: argparse.Namespace) -> dict[str, Any]:
    artifact_dir = Path(args.artifact_dir)
    metadata, artifact_info = load_artifact_metadata(artifact_dir, args.model_basename)
    gates: list[dict[str, Any]] = []
    artifact_present = metadata is not None
    gates.append({"gate": "artifact_present", "passed": bool(artifact_present), "value": artifact_info})

    metrics = (metadata or {}).get("metrics") or {}
    model_horizon = int((metadata or {}).get("horizon_days") or args.horizon_days)
    return_threshold = float((metadata or {}).get("return_threshold") or args.return_threshold)
    precision = _metric_number(metrics, "precision")
    positive_rate_test = _metric_number(metrics, "positive_rate_test")
    precision_lift = None
    if precision is not None and positive_rate_test is not None:
        precision_lift = round(float(precision - positive_rate_test), 6)

    if artifact_present:
        _check_gte("train_rows", _metric_number(metrics, "train_rows"), args.min_train_rows, gates)
        _check_gte("test_rows", _metric_number(metrics, "test_rows"), args.min_test_rows, gates)
        _check_gte("precision", precision, args.min_precision, gates)
        _check_gte("precision_lift_vs_positive_rate_test", precision_lift, args.min_precision_lift, gates)
        _check_gte("roc_auc", _metric_number(metrics, "roc_auc"), args.min_roc_auc, gates)

    coverage = summarize_label_coverage(horizon_days=model_horizon, return_threshold=return_threshold)
    _check_gte("labeled_rows", coverage.get("labeled_rows"), args.min_labeled_rows, gates)
    _check_gte("date_count", coverage.get("date_count"), args.min_dates, gates)
    _check_gte("symbol_count", coverage.get("symbol_count"), args.min_symbols, gates)
    _check_gte("event_class_count", coverage.get("event_class_count"), args.min_event_classes, gates)

    run_summary = summarize_successful_runs(Path(args.log_path), since_days=int(args.run_window_days))
    _check_gte("successful_weekly_runs", run_summary.get("successful_runs"), args.min_successful_runs, gates)
    gates.append(
        {
            "gate": "latest_ml_run_not_failed",
            "passed": run_summary.get("latest_status") in {None, "done"},
            "value": run_summary.get("latest_status"),
            "threshold": "done or no marker",
        }
    )

    score_summary = summarize_score_freshness(
        model_name=(metadata or {}).get("model_name"),
        model_version=(metadata or {}).get("model_version"),
        max_score_age_days=int(args.max_score_age_days),
    )
    _check_gte("score_rows", score_summary.get("score_rows"), args.min_score_rows, gates)
    gates.append(
        {
            "gate": "score_freshness",
            "passed": bool(score_summary.get("fresh")),
            "value": score_summary,
            "threshold": f"latest score <= {int(args.max_score_age_days)} days old",
        }
    )

    research_safety = summarize_research_safety_controls(metadata)
    for gate_name, summary in research_safety.items():
        gates.append(
            {
                "gate": f"{gate_name}_evidence",
                "passed": bool((summary or {}).get("passed")),
                "value": summary,
                "threshold": "explicit pass marker or recognized validation evidence",
            }
        )

    failed = [gate for gate in gates if not gate.get("passed")]
    decision = "review_candidate" if not failed else "hold_research_only"
    scorecard = build_research_scorecard(
        decision=decision,
        gates=gates,
        metrics=metrics,
        coverage=coverage,
        weekly_runs=run_summary,
        score_freshness=score_summary,
        research_safety=research_safety,
    )
    return {
        "status": "ok",
        "decision": decision,
        "ready_for_operator_review": decision == "review_candidate",
        "scorecard": scorecard,
        "promotion_mode": "manual_low_weight_input_only",
        "artifact": artifact_info,
        "metadata": metadata,
        "coverage": coverage,
        "weekly_runs": run_summary,
        "score_freshness": score_summary,
        "research_safety": research_safety,
        "gates": gates,
        "failed_gates": [gate["gate"] for gate in failed],
        "notes": [
            "This check never promotes the event model automatically.",
            "If it passes, review the evidence and consider using the model only as a low-weight review/risk/action input.",
            "If it fails, keep weekly all_ml.sh runs as label-quality and feature-quality research evidence.",
        ],
    }


def format_text(payload: dict[str, Any]) -> str:
    lines = [
        "Event Model Promotion Check",
        f"Decision: {payload.get('decision')}",
        f"Ready for operator review: {payload.get('ready_for_operator_review')}",
        "",
        "Failed gates:",
    ]
    failed = payload.get("failed_gates") or []
    if failed:
        lines.extend(f"- {gate}" for gate in failed)
    else:
        lines.append("- none")
    lines.extend(["", "Key evidence:"])
    coverage = payload.get("coverage") or {}
    runs = payload.get("weekly_runs") or {}
    score = payload.get("score_freshness") or {}
    safety = payload.get("research_safety") or {}
    metrics = ((payload.get("metadata") or {}).get("metrics") or {})
    lines.extend(
        [
            f"- train_rows: {metrics.get('train_rows')}",
            f"- test_rows: {metrics.get('test_rows')}",
            f"- precision: {metrics.get('precision')}",
            f"- positive_rate_test: {metrics.get('positive_rate_test')}",
            f"- roc_auc: {metrics.get('roc_auc')}",
            f"- labeled_rows: {coverage.get('labeled_rows')}",
            f"- dates/symbols/event_classes: {coverage.get('date_count')}/{coverage.get('symbol_count')}/{coverage.get('event_class_count')}",
            f"- successful weekly runs: {runs.get('successful_runs')} in last {runs.get('since_days')} days",
            f"- score rows/latest score: {score.get('score_rows')}/{score.get('latest_scored_at')}",
        ]
    )
    lines.extend(["", "Research safety:"])
    for key in ("leakage_control", "false_discovery_control", "cost_adjusted_baseline"):
        value = safety.get(key) or {}
        lines.append(f"- {key}: passed={bool(value.get('passed'))} source={value.get('source')} reason={value.get('reason')}")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only gate check for event-model promotion readiness.")
    parser.add_argument("--format", choices=["json", "text"], default="text")
    parser.add_argument("--artifact-dir", default=str(event_meta_model.DEFAULT_ARTIFACT_DIR))
    parser.add_argument("--model-basename", default=event_meta_model.DEFAULT_MODEL_BASENAME)
    parser.add_argument("--horizon-days", type=int, default=event_meta_model.DEFAULT_HORIZON_DAYS)
    parser.add_argument("--return-threshold", type=float, default=event_meta_model.DEFAULT_RETURN_THRESHOLD)
    parser.add_argument("--log-path", default=str(DEFAULT_LOG_PATH))
    parser.add_argument("--run-window-days", type=int, default=35)
    parser.add_argument("--max-score-age-days", type=int, default=14)
    parser.add_argument("--min-successful-runs", type=int, default=3)
    parser.add_argument("--min-train-rows", type=int, default=80)
    parser.add_argument("--min-test-rows", type=int, default=20)
    parser.add_argument("--min-labeled-rows", type=int, default=100)
    parser.add_argument("--min-dates", type=int, default=20)
    parser.add_argument("--min-symbols", type=int, default=25)
    parser.add_argument("--min-event-classes", type=int, default=4)
    parser.add_argument("--min-score-rows", type=int, default=10)
    parser.add_argument("--min-precision", type=float, default=0.55)
    parser.add_argument("--min-precision-lift", type=float, default=0.10)
    parser.add_argument("--min-roc-auc", type=float, default=0.55)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = build_promotion_check(args)
    if args.format == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default))
    else:
        print(format_text(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
