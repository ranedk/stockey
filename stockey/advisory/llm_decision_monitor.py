"""Live LLM-decision outcome monitoring -- the systematic-error detector ([P-LLM-AUTH].3).

This is the safety floor's other half (with the risk bounds in `advisory/llm_decision_risk_bounds`).
The operator dropped paper-first graduation (2026-06-23: too many non-stationary dimensions for
paper P&L to be informative) and chose instead to let the LLM act on strong data-grounded calls and
catch *repeatable* mistakes live. This module is that detector.

Design constraints (non-negotiable, from CLAUDE.md):
- It ALERTS, it never BLOCKS. `report["blocking"]` is always False. A live decision is never gated
  by this module; it annotates patterns for the operator (and a future model-policy review).
- It judges on benchmark-EXCESS after cost, never raw return -- market beta is not alpha.
- It needs enough MATURED labels before trusting a pattern (point-in-time: only matured outcomes
  count). Thin groups produce an `under_baselined` info note, not an alert.

Pure and additive: no DB, no LLM call, no broker. Input is a list of decision+outcome records
(produced later by [P-LLM-AUTH].4's logging); output is a structured monitoring report. A thin
formatter maps findings into the Operator Health degradation-row shape so wiring is trivial once
real decision logs exist.

Record convention (lenient; missing fields are tolerated):
    {
      "symbol": str, "decided_at": iso8601, "matured": bool,
      "proposed_action": "BUY"|"SELL"|...,
      "sufficiency_path": "valid_hypothesis_match"|"dominant_single_signal"|"aggregate_corroboration",
      "event_class": str|None, "sector": str|None,
      "realized_excess_after_cost": float|None,   # benchmark-excess return, AFTER cost
      "excess_hit": bool|None,                      # realized excess > 0 (derived if absent)
      "resolved_beta_only": bool|None,              # outcome attribution: support turned out beta
    }
"""

from __future__ import annotations

import math
import os
from typing import Any

# Minimum matured decisions before the OVERALL live-decision health is trusted.
LLM_DECISION_MONITOR_MIN_MATURED = int(os.getenv("LLM_DECISION_MONITOR_MIN_MATURED", "20"))
# Minimum matured decisions in a GROUP (event class / sufficiency path) before it is trusted.
LLM_DECISION_MONITOR_MIN_GROUP_MATURED = int(os.getenv("LLM_DECISION_MONITOR_MIN_GROUP_MATURED", "8"))
# Excess-hit-rate at/above this is healthy; below it (with enough matured) is an alert.
LLM_DECISION_MONITOR_MIN_EXCESS_HIT_RATE = float(os.getenv("LLM_DECISION_MONITOR_MIN_EXCESS_HIT_RATE", "0.45"))
# Mean benchmark-excess after cost must exceed this to be healthy.
LLM_DECISION_MONITOR_MIN_MEAN_EXCESS = float(os.getenv("LLM_DECISION_MONITOR_MIN_MEAN_EXCESS", "0.0"))
# Fraction of matured decisions whose support RESOLVED as market beta that flags a systematic tilt.
LLM_DECISION_MONITOR_BETA_TILT_RATE = float(os.getenv("LLM_DECISION_MONITOR_BETA_TILT_RATE", "0.50"))

ENTRY_ACTIONS = {"BUY", "BUY_MORE"}


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _excess_hit(record: dict[str, Any]) -> bool | None:
    """Did the decision beat its benchmark after cost? Prefer the explicit flag, else derive."""
    explicit = record.get("excess_hit")
    if isinstance(explicit, bool):
        return explicit
    excess = _num(record.get("realized_excess_after_cost"))
    if excess is None:
        return None
    return excess > 0.0


def _matured(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in records if isinstance(r, dict) and bool(r.get("matured"))]


def _aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Excess-based health of a set of matured decisions (no raw-return shortcuts)."""
    excesses = [_num(r.get("realized_excess_after_cost")) for r in records]
    excesses = [value for value in excesses if value is not None]
    hits = [_excess_hit(r) for r in records]
    hits = [value for value in hits if value is not None]
    beta = [bool(r.get("resolved_beta_only")) for r in records if r.get("resolved_beta_only") is not None]
    matured_count = len(records)
    return {
        "matured_count": matured_count,
        "excess_labeled_count": len(excesses),
        "excess_hit_rate": (sum(1 for h in hits if h) / len(hits)) if hits else None,
        "mean_excess_after_cost": (sum(excesses) / len(excesses)) if excesses else None,
        "beta_resolved_rate": (sum(1 for b in beta if b) / len(beta)) if beta else None,
    }


def summarize_decision_outcomes(records: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Overall matured-decision health, excess-based."""
    records = [r for r in (records or []) if isinstance(r, dict)]
    matured = _matured(records)
    summary = _aggregate(matured)
    summary["total_decisions"] = len(records)
    return summary


def _group_findings(
    matured: list[dict[str, Any]],
    key_field: str,
    scope: str,
    *,
    min_group_matured: int,
    min_excess_hit_rate: float,
    min_mean_excess: float,
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in matured:
        key = record.get(key_field)
        if key is None or str(key).strip() == "":
            continue
        groups.setdefault(str(key), []).append(record)
    findings: list[dict[str, Any]] = []
    for key in sorted(groups):
        members = groups[key]
        stats = _aggregate(members)
        if stats["matured_count"] < int(min_group_matured):
            findings.append({
                "kind": f"llm_decision_{scope}_under_baselined",
                "severity": "info",
                "scope": scope,
                "key": key,
                "matured_count": stats["matured_count"],
                "excess_hit_rate": stats["excess_hit_rate"],
                "mean_excess_after_cost": stats["mean_excess_after_cost"],
                "suggestion": f"Not enough matured decisions for {scope} '{key}' "
                              f"({stats['matured_count']} < {int(min_group_matured)}); collect more before trusting.",
            })
            continue
        hit_rate = stats["excess_hit_rate"]
        mean_excess = stats["mean_excess_after_cost"]
        low_hit = hit_rate is not None and hit_rate < float(min_excess_hit_rate)
        negative = mean_excess is not None and mean_excess <= float(min_mean_excess)
        if low_hit or negative:
            findings.append({
                "kind": f"llm_decision_{scope}_misjudged",
                "severity": "alert",
                "scope": scope,
                "key": key,
                "matured_count": stats["matured_count"],
                "excess_hit_rate": hit_rate,
                "mean_excess_after_cost": mean_excess,
                "suggestion": f"{scope} '{key}' is systematically underperforming after cost "
                              f"(hit_rate={hit_rate}, mean_excess={mean_excess}); review the policy for this group.",
            })
    return findings


def detect_systematic_errors(
    records: list[dict[str, Any]] | None,
    *,
    min_matured: int = LLM_DECISION_MONITOR_MIN_MATURED,
    min_group_matured: int = LLM_DECISION_MONITOR_MIN_GROUP_MATURED,
    min_excess_hit_rate: float = LLM_DECISION_MONITOR_MIN_EXCESS_HIT_RATE,
    min_mean_excess: float = LLM_DECISION_MONITOR_MIN_MEAN_EXCESS,
    beta_tilt_rate: float = LLM_DECISION_MONITOR_BETA_TILT_RATE,
) -> list[dict[str, Any]]:
    """Detect repeatable LLM-decision error patterns. Alerts only -- never blocks."""
    records = [r for r in (records or []) if isinstance(r, dict)]
    matured = _matured(records)
    overall = _aggregate(matured)
    findings: list[dict[str, Any]] = []

    if overall["matured_count"] < int(min_matured):
        findings.append({
            "kind": "llm_decisions_under_baselined",
            "severity": "info",
            "scope": "overall",
            "key": "overall",
            "matured_count": overall["matured_count"],
            "suggestion": f"Only {overall['matured_count']} matured live decisions "
                          f"(< {int(min_matured)}); overall health is not yet trustworthy.",
        })
        return findings

    hit_rate = overall["excess_hit_rate"]
    if hit_rate is not None and hit_rate < float(min_excess_hit_rate):
        findings.append({
            "kind": "llm_decisions_low_excess_hit_rate",
            "severity": "alert",
            "scope": "overall",
            "key": "overall",
            "matured_count": overall["matured_count"],
            "excess_hit_rate": hit_rate,
            "suggestion": f"Live LLM decisions beat benchmark after cost only {hit_rate:.0%} of the time "
                          f"(< {float(min_excess_hit_rate):.0%}); the policy may be systematically wrong.",
        })
    mean_excess = overall["mean_excess_after_cost"]
    if mean_excess is not None and mean_excess <= float(min_mean_excess):
        findings.append({
            "kind": "llm_decisions_negative_mean_excess",
            "severity": "alert",
            "scope": "overall",
            "key": "overall",
            "matured_count": overall["matured_count"],
            "mean_excess_after_cost": mean_excess,
            "suggestion": f"Mean benchmark-excess after cost is {mean_excess} (<= {float(min_mean_excess)}); "
                          f"live decisions are not adding alpha.",
        })
    beta_rate = overall["beta_resolved_rate"]
    if beta_rate is not None and beta_rate >= float(beta_tilt_rate):
        findings.append({
            "kind": "llm_decisions_systematic_beta_tilt",
            "severity": "alert",
            "scope": "overall",
            "key": "overall",
            "matured_count": overall["matured_count"],
            "beta_resolved_rate": beta_rate,
            "suggestion": f"{beta_rate:.0%} of matured decisions resolved as market beta rather than alpha "
                          f"(>= {float(beta_tilt_rate):.0%}); the grounding may be admitting beta tilt.",
        })

    findings.extend(_group_findings(
        matured, "event_class", "event_class",
        min_group_matured=min_group_matured, min_excess_hit_rate=min_excess_hit_rate, min_mean_excess=min_mean_excess,
    ))
    findings.extend(_group_findings(
        matured, "sufficiency_path", "sufficiency_path",
        min_group_matured=min_group_matured, min_excess_hit_rate=min_excess_hit_rate, min_mean_excess=min_mean_excess,
    ))
    return findings


def build_llm_decision_monitor_report(
    records: list[dict[str, Any]] | None,
    **thresholds: Any,
) -> dict[str, Any]:
    """Assemble the full monitoring report. `blocking` is always False (alert, never block)."""
    summary = summarize_decision_outcomes(records)
    findings = detect_systematic_errors(records, **thresholds)
    alerts = [f for f in findings if f["severity"] == "alert"]
    return {
        "schema_version": 1,
        "summary": summary,
        "findings": findings,
        "alert_count": len(alerts),
        "has_systematic_error": bool(alerts),
        "blocking": False,
        "authority_scope": "llm_decision_monitoring_alert_only_never_blocks",
    }


def format_monitor_findings_for_operator_health(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Map monitor findings into the Operator Health degradation-row shape ({kind, severity, ...}).

    Operator Health uses severity in {"error", "warn"}; an alert maps to "error", info to "warn".
    """
    rows: list[dict[str, Any]] = []
    for finding in (report or {}).get("findings", []):
        if finding.get("severity") == "info":
            continue  # under-baselined notes are not operator-actionable degradations
        rows.append({
            "kind": finding["kind"],
            "severity": "error" if finding["severity"] == "alert" else "warn",
            "title": f"LLM decision monitor: {finding['kind'].replace('_', ' ')}"
                     + (f" ({finding['key']})" if finding.get("key") not in (None, "overall") else ""),
            "suggested_fix": finding.get("suggestion", ""),
            "scope": finding.get("scope"),
            "matured_count": finding.get("matured_count"),
        })
    return rows
