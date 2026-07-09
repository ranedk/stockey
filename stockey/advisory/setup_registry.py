from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from advisory.fallback_telemetry import record_local_fallback_event


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETUP_CONFIG = REPO_ROOT / "config" / "advisory_setups.yaml"
TS_FORECAST_RULE_STATUSES = {"disabled_review_candidate", "active_review", "trusted_overlay", "rejected", "archived"}
SIGNAL_QUALITY_RULE_STATUSES = {"disabled_review_candidate", "active_review", "trusted_overlay", "rejected", "archived"}


@lru_cache(maxsize=1)
def load_setup_registry(config_path: str | None = None) -> list[dict[str, Any]]:
    path = Path(config_path) if config_path else DEFAULT_SETUP_CONFIG
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    setups = payload.get("setups") or []
    normalized: list[dict[str, Any]] = []
    for setup in setups:
        if not isinstance(setup, dict):
            continue
        screeners = [
            str(value)
            for value in (
                setup.get("screeners")
                or setup.get("screener_slugs")
                or ([setup.get("screener_slug")] if setup.get("screener_slug") else [])
            )
            if value
        ]
        overlay_screeners: dict[str, dict[str, list[str]]] = {}
        for overlay_name, raw in (setup.get("overlay_screeners") or {}).items():
            if isinstance(raw, dict):
                add = [str(value) for value in (raw.get("add") or []) if value]
                remove = [str(value) for value in (raw.get("remove") or []) if value]
            else:
                add = [str(value) for value in (raw or []) if value]
                remove = []
            overlay_screeners[str(overlay_name).upper()] = {"add": add, "remove": remove}
        normalized.append(
            {
                "setup_id": str(setup["setup_id"]),
                "setup_name": str(setup.get("setup_name") or setup["setup_id"]),
                "setup_family": str(setup.get("setup_family") or ""),
                "holding_horizon_note": setup.get("holding_horizon_note"),
                "screener_slug": screeners[0] if screeners else None,
                "screener_slugs": screeners,
                "screeners": screeners,
                "screener_mode": str(setup.get("screener_mode") or "union").lower(),
                "dynamic_sources": [str(value).strip().lower() for value in (setup.get("dynamic_sources") or []) if str(value or "").strip()],
                "allowed_regimes": [str(value) for value in setup.get("allowed_regimes", [])],
                "blocked_regimes": [str(value) for value in setup.get("blocked_regimes", [])],
                "allowed_overlays": [str(value).upper() for value in setup.get("allowed_overlays", [])],
                "blocked_overlays": [str(value).upper() for value in setup.get("blocked_overlays", [])],
                "overlay_screeners": overlay_screeners,
                "market_cap_min": setup.get("market_cap_min"),
                "market_cap_max": setup.get("market_cap_max"),
                "min_avg_traded_value_20d": setup.get("min_avg_traded_value_20d"),
                "max_breakout_extension_pct": setup.get("max_breakout_extension_pct"),
                "watch_pullback_extension_pct": setup.get("watch_pullback_extension_pct"),
                "min_dist_52w_high": setup.get("min_dist_52w_high"),
                "scoring_weights": dict(setup.get("scoring_weights") or {}),
                "score_thresholds": dict(setup.get("score_thresholds") or {}),
                "regime_policy": dict(setup.get("regime_policy") or {}),
                "technical_rules": list(setup.get("technical_rules", [])),
                "intraday_rules": list(setup.get("intraday_rules", [])),
                "fundamental_rules": list(setup.get("fundamental_rules", [])),
                "watch_reasons": list(setup.get("watch_reasons", [])),
                "entry_styles": list(setup.get("entry_styles", [])),
                "freshness_policy": dict(setup.get("freshness_policy") or {}),
                "intraday_usage_mode": str(setup.get("intraday_usage_mode") or "confirm_only").lower(),
                "risk_profile": dict(setup.get("risk_profile") or {}),
                "portfolio_cap_pct": setup.get("portfolio_cap_pct"),
                "single_position_cap_pct": setup.get("single_position_cap_pct"),
                "research_only": bool(setup.get("research_only", False)),
            }
        )
    return normalized


def _coerce_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        record_local_fallback_event(
            module="advisory.setup_registry",
            source="setup_config",
            fallback_type="setup_float_parse_failed",
            severity="warn",
            reason="Setup registry could not parse a numeric config value and treated it as missing.",
            error=exc,
            metadata={"value": str(value)[:200]},
        )
        return None


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        record_local_fallback_event(
            module="advisory.setup_registry",
            source="setup_config",
            fallback_type="setup_int_parse_failed",
            severity="warn",
            reason="Setup registry could not parse an integer config value and treated it as missing.",
            error=exc,
            metadata={"value": str(value)[:200]},
        )
        return None


def load_ts_forecast_review_rules(config_path: str | None = None) -> dict[str, Any]:
    path = Path(config_path) if config_path else DEFAULT_SETUP_CONFIG
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_rules = payload.get("ts_forecast_review_rules") or []
    if not isinstance(raw_rules, list):
        raw_rules = []
        source_issue = {
            "code": "invalid_rules_block",
            "severity": "error",
            "message": "ts_forecast_review_rules must be a list.",
        }
    else:
        source_issue = None

    rules: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    if source_issue:
        issues.append(source_issue)

    for idx, raw in enumerate(raw_rules):
        if not isinstance(raw, dict):
            issues.append(
                {
                    "code": "invalid_rule_type",
                    "severity": "error",
                    "index": idx,
                    "message": "TS forecast review rule must be a mapping.",
                }
            )
            continue
        model_name = str(raw.get("model_name") or raw.get("ts_forecast_model_name") or "").strip().lower()
        horizon_days = _coerce_int(raw.get("horizon_days") or raw.get("ts_forecast_horizon_days"))
        status = str(raw.get("status") or "disabled_review_candidate").strip().lower()
        authority = str(raw.get("authority") or "review_input_only").strip().lower()
        broker_execution_allowed = bool(raw.get("broker_execution_allowed", False))
        rule_issues: list[dict[str, Any]] = []
        if not model_name:
            rule_issues.append({"code": "missing_model_name", "severity": "error", "message": "model_name is required."})
        if horizon_days is None or horizon_days <= 0:
            rule_issues.append({"code": "invalid_horizon_days", "severity": "error", "message": "horizon_days must be a positive integer."})
        if status not in TS_FORECAST_RULE_STATUSES:
            rule_issues.append({"code": "invalid_status", "severity": "error", "message": f"Unsupported status: {status}."})
        if authority != "review_input_only":
            rule_issues.append(
                {
                    "code": "unsafe_authority",
                    "severity": "error",
                    "message": "TS forecast review rules must use authority=review_input_only.",
                }
            )
        if broker_execution_allowed:
            rule_issues.append(
                {
                    "code": "unsafe_broker_execution",
                    "severity": "error",
                    "message": "TS forecast review rules cannot enable broker execution.",
                }
            )
        normalized = {
            "rule_id": f"{model_name or 'unknown'}:{horizon_days or 'unknown'}:{idx}",
            "index": idx,
            "model_name": model_name,
            "horizon_days": horizon_days,
            "status": status,
            "authority": "review_input_only",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
            "minimum_evaluated_trades": _coerce_int(raw.get("minimum_evaluated_trades")),
            "minimum_win_rate": _coerce_float(raw.get("minimum_win_rate")),
            "minimum_avg_cost_adjusted_return": _coerce_float(raw.get("minimum_avg_cost_adjusted_return")),
            "minimum_lift_vs_momentum": _coerce_float(raw.get("minimum_lift_vs_momentum")),
            "maximum_exit_conflict_rate": _coerce_float(raw.get("maximum_exit_conflict_rate")),
            "raw_status": raw.get("status"),
            "issues": rule_issues,
            "valid": not any(issue.get("severity") == "error" for issue in rule_issues),
            "readable_as_review_input": status in {"active_review", "trusted_overlay"} and not rule_issues,
            "usable_for_live_policy": False,
        }
        rules.append(normalized)
        for issue in rule_issues:
            issues.append({"index": idx, "rule_id": normalized["rule_id"], **issue})

    return {
        "status": "ok" if not any(issue.get("severity") == "error" for issue in issues) else "issues_found",
        "config_path": str(path),
        "rules": rules,
        "issues": issues,
        "summary": {
            "row_count": len(rules),
            "valid_count": sum(1 for rule in rules if rule.get("valid")),
            "active_review_count": sum(1 for rule in rules if rule.get("status") == "active_review"),
            "trusted_overlay_count": sum(1 for rule in rules if rule.get("status") == "trusted_overlay"),
            "disabled_count": sum(1 for rule in rules if rule.get("status") == "disabled_review_candidate"),
            "issue_count": len(issues),
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
        },
        "operator_boundary": {
            "read_only": True,
            "broker_execution_enabled": False,
            "policy_auto_promotion_allowed": False,
            "note": "TS forecast review rules are config-visible review inputs only; no live action/risk/model layer consumes them automatically.",
        },
    }


def load_signal_quality_overlay_rules(config_path: str | None = None) -> dict[str, Any]:
    path = Path(config_path) if config_path else DEFAULT_SETUP_CONFIG
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_rules = payload.get("signal_quality_overlay_rules") or []
    if not isinstance(raw_rules, list):
        raw_rules = []
        source_issue = {
            "code": "invalid_rules_block",
            "severity": "error",
            "message": "signal_quality_overlay_rules must be a list.",
        }
    else:
        source_issue = None

    rules: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    if source_issue:
        issues.append(source_issue)

    for idx, raw in enumerate(raw_rules):
        if not isinstance(raw, dict):
            issues.append(
                {
                    "code": "invalid_rule_type",
                    "severity": "error",
                    "index": idx,
                    "message": "Signal-quality overlay rule must be a mapping.",
                }
            )
            continue
        overlay = str(raw.get("overlay") or raw.get("signal_quality_overlay") or "").strip()
        horizon_days = _coerce_int(raw.get("minimum_horizon_days") or raw.get("horizon_days"))
        status = str(raw.get("status") or "disabled_review_candidate").strip().lower()
        authority = str(raw.get("authority") or "review_input_only").strip().lower()
        broker_execution_allowed = bool(raw.get("broker_execution_allowed", False))
        policy_auto_promotion_allowed = bool(raw.get("policy_auto_promotion_allowed", False))
        context_source_family = str(raw.get("context_source_family") or "").strip().lower() or None
        split_axis = str(raw.get("split_axis") or "").strip() or None
        split_value = str(raw.get("split_value") or "").strip() or None
        context_class = str(raw.get("context_class") or "").strip() or None
        direction = str(raw.get("direction") or "").strip().lower() or None
        stability_classification = str(raw.get("stability_classification") or raw.get("classification") or "").strip() or None
        rule_issues: list[dict[str, Any]] = []
        if not overlay:
            rule_issues.append({"code": "missing_overlay", "severity": "error", "message": "overlay is required."})
        if horizon_days is None or horizon_days <= 0:
            rule_issues.append({"code": "invalid_minimum_horizon_days", "severity": "error", "message": "minimum_horizon_days must be a positive integer."})
        if status not in SIGNAL_QUALITY_RULE_STATUSES:
            rule_issues.append({"code": "invalid_status", "severity": "error", "message": f"Unsupported status: {status}."})
        if authority != "review_input_only":
            rule_issues.append(
                {
                    "code": "unsafe_authority",
                    "severity": "error",
                    "message": "Signal-quality overlay rules must use authority=review_input_only.",
                }
            )
        if broker_execution_allowed:
            rule_issues.append(
                {
                    "code": "unsafe_broker_execution",
                    "severity": "error",
                    "message": "Signal-quality overlay rules cannot enable broker execution.",
                }
            )
        if policy_auto_promotion_allowed:
            rule_issues.append(
                {
                    "code": "unsafe_policy_auto_promotion",
                    "severity": "error",
                    "message": "Signal-quality overlay rules cannot enable automatic policy promotion.",
                }
            )

        normalized = {
            "rule_id": f"{overlay or 'unknown'}:{horizon_days or 'unknown'}:{context_source_family or 'all'}:{idx}",
            "index": idx,
            "overlay": overlay,
            "minimum_horizon_days": horizon_days,
            "minimum_matured_rows": _coerce_int(raw.get("minimum_matured_rows")),
            "minimum_lift_vs_technical_only": _coerce_float(raw.get("minimum_lift_vs_technical_only")),
            "context_source_family": context_source_family,
            "minimum_context_source_families": _coerce_int(raw.get("minimum_context_source_families")),
            "minimum_context_family_selected_rows": _coerce_int(raw.get("minimum_context_family_selected_rows")),
            "minimum_context_family_matured_rows": _coerce_int(raw.get("minimum_context_family_matured_rows")),
            "minimum_context_family_symbols": _coerce_int(raw.get("minimum_context_family_symbols")),
            "split_axis": split_axis,
            "split_value": split_value,
            "context_class": context_class,
            "direction": direction,
            "stability_classification": stability_classification,
            "minimum_windows": _coerce_int(raw.get("minimum_windows")),
            "helpful_window_count": _coerce_int(raw.get("helpful_window_count")),
            "harmful_window_count": _coerce_int(raw.get("harmful_window_count")),
            "avg_lift_vs_technical_only": _coerce_float(raw.get("avg_lift_vs_technical_only")),
            "action_policy_effect": str(raw.get("action_policy_effect") or "review_input_only_no_live_policy").strip(),
            "status": status,
            "authority": "review_input_only",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
            "raw_status": raw.get("status"),
            "issues": rule_issues,
            "valid": not any(issue.get("severity") == "error" for issue in rule_issues),
            "readable_as_review_input": status in {"active_review", "trusted_overlay"} and not rule_issues,
            "usable_for_live_policy": False,
        }
        rules.append(normalized)
        for issue in rule_issues:
            issues.append({"index": idx, "rule_id": normalized["rule_id"], **issue})

    return {
        "status": "ok" if not any(issue.get("severity") == "error" for issue in issues) else "issues_found",
        "config_path": str(path),
        "rules": rules,
        "issues": issues,
        "summary": {
            "row_count": len(rules),
            "valid_count": sum(1 for rule in rules if rule.get("valid")),
            "active_review_count": sum(1 for rule in rules if rule.get("status") == "active_review"),
            "trusted_overlay_count": sum(1 for rule in rules if rule.get("status") == "trusted_overlay"),
            "disabled_count": sum(1 for rule in rules if rule.get("status") == "disabled_review_candidate"),
            "issue_count": len(issues),
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
        },
        "operator_boundary": {
            "read_only": True,
            "broker_execution_enabled": False,
            "policy_auto_promotion_allowed": False,
            "note": "Signal-quality overlay rules are config-visible review inputs only; no live action/risk/model layer consumes them automatically.",
        },
    }
