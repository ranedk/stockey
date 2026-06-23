"""Deterministic decision-contract scaffolding for LLM-direct trade authority ([P-LLM-AUTH].1).

The operator chose (2026-06-23) to let an LLM take trade decisions directly when it has reviewed
the complete, validated evidence and has strong, data-grounded reasons — not on one news / one
announcement / one indicator. This module is the deterministic enforcement of that rule. It is:

- pure (no DB, no LLM call, no network) and fully unit-testable;
- audit/contract-producing only — it grants NO broker authority and has no live consumer yet;
- the foundation a later LLM decision policy ([P-LLM-AUTH].2) fills and a live bridge
  ([P-LLM-AUTH].4) would consume behind the default-OFF master flag + paper graduation.

It answers three deterministic questions about a decision:
1. Is the evidence packet COMPLETE (all required dimensions present and fresh)?
2. Is a proposed decision DATA-GROUNDED (cites benchmark-excess + >= N independent dimensions,
   i.e. not a single-signal/intuition call)?
3. Is the directional support market BETA rather than alpha (excess-negative / unattributed)?

`meets_data_grounding_for_live` is True only when all three pass. That is the DATA bar — separate
from `live_authority_master_flag` (LLM_DIRECT_AUTHORITY_ENABLED) and from paper graduation.
"""

from __future__ import annotations

import os
from typing import Any

# Master switch for any LIVE LLM->broker decision authority. DEFAULTS OFF. Building the contract is
# always allowed (audit-only); no contract grants broker authority unless this is enabled AND the
# policy passed paper graduation ([P-LLM-AUTH].3/.4), neither of which exists yet.
LLM_DIRECT_AUTHORITY_ENABLED = os.getenv("LLM_DIRECT_AUTHORITY_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}

# Minimum independent evidence dimensions a directional (BUY/SELL/size) decision must cite, so a
# call cannot rest on one news / one announcement / one indicator.
DEFAULT_MIN_INDEPENDENT_CONFIRMATIONS = int(os.getenv("LLM_DECISION_MIN_INDEPENDENT_CONFIRMATIONS", "3"))

# Evidence dimensions an informed (non-single-signal) decision must rest on. The goal: a decision
# over the full packet should match what a competent analyst would conclude given the same data.
REQUIRED_EVIDENCE_DIMENSIONS = (
    "technical_confirmation",
    "risk",
    "market_context",
    "sector_reliability",
    "exact_class_reliability",
    "benchmark_excess",
    "event_provenance",
)

DIRECTIONAL_ACTIONS = {"BUY", "BUY_MORE", "SELL", "PARTIAL_SELL"}

# Benchmark-excess classifications that mean "market beta, not alpha": a directional decision
# resting on them is not data-grounded for live authority.
BETA_ONLY_CLASSIFICATIONS = {
    "benchmark_beta_not_overlay_alpha",
    "benchmark_beta_not_policy_alpha",
    "benchmark_beta_not_memory_alpha",
    "benchmark_beta_not_transition_alpha",
    "benchmark_beta_not_split_alpha",
    "needs_benchmark_attribution",
}


def _dimension_available(packet: dict[str, Any], dimension: str) -> bool:
    """A dimension counts only when present AND fresh (point-in-time), per the packet convention.

    Convention: packet[dimension] is a dict with at least {"present": bool, "fresh": bool}. A bare
    truthy value is treated as present+fresh for convenience; None/absent/explicit-missing is not.
    """
    value = packet.get(dimension) if isinstance(packet, dict) else None
    if value is None:
        return False
    if isinstance(value, dict):
        if value.get("present") is False:
            return False
        if str(value.get("status") or "").strip().lower() in {"missing", "unavailable", "stale", "error"}:
            return False
        fresh = value.get("fresh")
        if fresh is False:
            return False
        return bool(value.get("present", True))
    return bool(value)


def build_evidence_completeness(
    packet: dict[str, Any],
    *,
    required: tuple[str, ...] = REQUIRED_EVIDENCE_DIMENSIONS,
) -> dict[str, Any]:
    """Which required evidence dimensions are present+fresh, and is the packet complete?"""
    available = [dimension for dimension in required if _dimension_available(packet, dimension)]
    missing = [dimension for dimension in required if dimension not in available]
    return {
        "required_dimensions": list(required),
        "available_dimensions": available,
        "missing_dimensions": missing,
        "evidence_complete": not missing,
    }


def evaluate_beta_guard(packet: dict[str, Any]) -> dict[str, Any]:
    """Is the directional support market beta (excess-negative/unattributed) rather than alpha?"""
    benchmark = packet.get("benchmark_excess") if isinstance(packet, dict) else None
    if not isinstance(benchmark, dict) or not _dimension_available(packet, "benchmark_excess"):
        return {"benchmark_excess_present": False, "beta_only_support": True, "classification": None}
    classification = str(benchmark.get("classification") or "").strip()
    excess_positive = benchmark.get("excess_positive")
    beta_only = classification in BETA_ONLY_CLASSIFICATIONS or excess_positive is False
    return {
        "benchmark_excess_present": True,
        "beta_only_support": bool(beta_only),
        "classification": classification or None,
        "excess_positive": bool(excess_positive) if excess_positive is not None else None,
    }


def validate_decision_grounding(
    *,
    action: str,
    cited_dimensions: list[str] | None,
    packet: dict[str, Any],
    min_confirmations: int = DEFAULT_MIN_INDEPENDENT_CONFIRMATIONS,
) -> dict[str, Any]:
    """Is a proposed decision data-grounded (not a single-signal / intuition call)?

    Directional decisions must cite benchmark-excess plus >= min_confirmations independent
    dimensions that are actually present in the packet, and must not rest on beta-only support.
    """
    normalized_action = str(action or "").strip().upper()
    completeness = build_evidence_completeness(packet)
    available = set(completeness["available_dimensions"])
    cited = [dimension for dimension in (cited_dimensions or []) if dimension in available]
    cited_unique = sorted(set(cited))
    beta_guard = evaluate_beta_guard(packet)
    directional = normalized_action in DIRECTIONAL_ACTIONS
    reasons: list[str] = []
    if directional:
        if not completeness["evidence_complete"]:
            reasons.append(f"evidence_incomplete: missing {completeness['missing_dimensions']}")
        if "benchmark_excess" not in cited_unique:
            reasons.append("must_cite_benchmark_excess")
        if len(cited_unique) < int(min_confirmations):
            reasons.append(f"single_or_thin_signal: cited {len(cited_unique)} < {int(min_confirmations)} independent dimensions")
        if beta_guard["beta_only_support"]:
            reasons.append(f"beta_only_support: benchmark-excess classification {beta_guard.get('classification')}")
    else:
        # Non-directional (WATCH / NO_ACTION / HOLD / REDUCE_EXPOSURE_REVIEW): still not a
        # single-signal call, but no benchmark-excess/completeness requirement.
        if len(cited_unique) < 1:
            reasons.append("no_cited_evidence")
    grounded = not reasons
    return {
        "action": normalized_action,
        "directional": directional,
        "cited_dimensions": cited_unique,
        "independent_confirmation_count": len(cited_unique),
        "min_independent_confirmations": int(min_confirmations),
        "single_signal": len(cited_unique) <= 1,
        "data_grounded": grounded,
        "grounding_failures": reasons,
    }


def build_llm_decision_contract(
    *,
    symbol: str,
    proposed_action: str,
    packet: dict[str, Any],
    cited_dimensions: list[str] | None = None,
    conviction: float | None = None,
    rationale: str | None = None,
    graduation_passed: bool = False,
    min_confirmations: int = DEFAULT_MIN_INDEPENDENT_CONFIRMATIONS,
) -> dict[str, Any]:
    """Assemble the full deterministic decision contract for an LLM-proposed action.

    Audit-only: `broker_execution_allowed` is always False here (no live bridge exists). The
    contract records `meets_data_grounding_for_live` (the DATA bar), the master flag, and whether
    paper graduation passed, so a future live bridge ([P-LLM-AUTH].4) has everything it needs while
    nothing in .1 can move capital.
    """
    completeness = build_evidence_completeness(packet)
    beta_guard = evaluate_beta_guard(packet)
    grounding = validate_decision_grounding(
        action=proposed_action,
        cited_dimensions=cited_dimensions,
        packet=packet,
        min_confirmations=min_confirmations,
    )
    meets_data_bar = bool(
        grounding["data_grounded"]
        and completeness["evidence_complete"]
        and not beta_guard["beta_only_support"]
    )
    live_eligible = bool(meets_data_bar and LLM_DIRECT_AUTHORITY_ENABLED and graduation_passed)
    return {
        "schema_version": 1,
        "symbol": str(symbol or "").strip().upper(),
        "proposed_action": grounding["action"],
        "conviction": None if conviction is None else float(conviction),
        "rationale": rationale,
        "evidence_completeness": completeness,
        "beta_guard": beta_guard,
        "grounding": grounding,
        "meets_data_grounding_for_live": meets_data_bar,
        # authority stamp — .1 never grants broker authority
        "authority_scope": "llm_decision_contract_review_input_only",
        "broker_execution_allowed": False,
        "live_authority_master_flag": bool(LLM_DIRECT_AUTHORITY_ENABLED),
        "graduation_passed": bool(graduation_passed),
        "eligible_for_live_authority": live_eligible,
    }
