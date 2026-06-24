"""Deterministic decision-contract scaffolding for LLM-direct trade authority ([P-LLM-AUTH].1).

The operator chose (2026-06-23) to let an LLM take trade decisions directly when it has reviewed
the complete, validated evidence and has strong, data-grounded reasons -- not on one news / one
announcement / one indicator. This module is the deterministic enforcement of that rule. It is:

- pure (no DB, no LLM call, no network) and fully unit-testable;
- audit/contract-producing only -- it grants NO broker authority and has no live consumer yet;
- the foundation a later LLM decision policy ([P-LLM-AUTH].4) fills, sized by the risk bounds
  ([P-LLM-AUTH].2), and a live bridge ([P-LLM-AUTH].5) would consume behind the default-OFF
  master flag.

It separates two questions that an earlier fixed "cite >= N dimensions" rule wrongly merged:

1. COMPLETENESS -- did we LOOK at every required dimension (regime, macro, sector, technical,
   event, benchmark)? A genuine data gap is recorded, not silently passed, so a decision is never
   made blind to a dimension that could contradict it.
2. SUFFICIENCY -- is the evidence CONSISTENT and corroborated enough to act? This is NOT a magnitude
   score and NOT a fixed count. Each packet dimension carries a structured VERDICT (direction
   supportive / neutral / contradicting + confidence). A directional decision is grounded when:
   - NO required dimension strongly CONTRADICTS the thesis (a confident `contradicting` verdict
     vetoes -- e.g. a breakout in a name that is underperforming the benchmark), AND
   - it matches a VALID HYPOTHESIS (a validated/production investor playbook whose conditions are
     met -- the operator's experience-encoded sufficiency rule) OR has multi-dimension
     CORROBORATION (>= min supportive, confident verdicts across distinct dimensions).
   An earlier per-dimension magnitude score was removed: a calibration showed it had ~zero/negative
   correlation with forward benchmark-excess, so it could not ground a decision. Verdicts encode
   logical consistency (does the evidence support or contradict), which is sound regardless of a
   feature's predictive power. Supportive/contradicting counts are taken over the PACKET's own
   verdicts (deterministic), so the LLM cannot ground a decision by choosing what to cite.

The BETA guard stays but is scoped: it blocks only when directional support rests on return-based
evidence that is market beta (excess-negative or explicitly unattributed). An event/hypothesis
thesis that makes no returns claim (excess_positive is None) is not beta and is not auto-failed.

`meets_data_grounding_for_live` is True only when the packet is complete, the decision is
sufficiently grounded (no strong contradiction + hypothesis-or-corroboration), and support is not
beta-only. That is the DATA bar -- separate from `live_authority_master_flag`
(LLM_DIRECT_AUTHORITY_ENABLED) and from outcome-monitoring graduation.
"""

from __future__ import annotations

import os
from typing import Any

# Master switch for any LIVE LLM->broker decision authority. DEFAULTS OFF. Building the contract is
# always allowed (audit-only); no contract grants broker authority unless this is enabled AND the
# policy passed outcome-monitoring graduation ([P-LLM-AUTH].3/.4), neither of which exists yet.
LLM_DIRECT_AUTHORITY_ENABLED = os.getenv("LLM_DIRECT_AUTHORITY_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}

# Minimum distinct dimensions whose verdict is supportive (and confident) required for the
# corroboration path -- so a call cannot rest on one news / one announcement / one indicator. The
# valid-hypothesis path bypasses this (the operator has encoded that those conditions suffice).
DEFAULT_MIN_SUPPORTIVE_DIMENSIONS = int(os.getenv("LLM_DECISION_MIN_SUPPORTIVE_DIMENSIONS", "2"))

# A verdict counts as decisive (supportive or contradicting) only at this confidence or above.
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
DECISIVE_CONFIDENCE = "medium"

# Evidence dimensions a decision must have LOOKED AT (completeness). Looking at a dimension and
# finding it neutral/no-signal still counts as looked-at; a genuine data gap does not.
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

# Investor-playbook (Valid Hypothesis) statuses trusted to define sufficiency on their own.
VALID_HYPOTHESIS_STATUSES = {"validated", "production"}

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
    """A dimension counts as LOOKED-AT only when present AND fresh (point-in-time).

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


def _decisive(dimension: dict[str, Any]) -> bool:
    """A verdict counts only at >= DECISIVE_CONFIDENCE (low-confidence verdicts are advisory)."""
    rank = _CONFIDENCE_RANK.get(str(dimension.get("confidence") or "").strip().lower(), 0)
    return rank >= _CONFIDENCE_RANK[DECISIVE_CONFIDENCE]


def _classify_verdicts(packet: dict[str, Any], required: tuple[str, ...]) -> dict[str, list[str]]:
    """Supportive / contradicting dimensions among the present required ones (deterministic).

    Counts are taken over the PACKET's verdicts, not over what the LLM cited, so the LLM has no
    leverage over grounding -- only over the proposed action/conviction, which is then gated.
    """
    supportive: list[str] = []
    contradicting: list[str] = []
    for dimension in required:
        value = packet.get(dimension) if isinstance(packet, dict) else None
        if not isinstance(value, dict) or not _dimension_available(packet, dimension):
            continue
        if not _decisive(value):
            continue
        direction = str(value.get("direction") or "").strip().lower()
        if direction == "supportive":
            supportive.append(dimension)
        elif direction == "contradicting":
            contradicting.append(dimension)
    return {"supportive": supportive, "contradicting": contradicting}


def build_evidence_completeness(
    packet: dict[str, Any],
    *,
    required: tuple[str, ...] = REQUIRED_EVIDENCE_DIMENSIONS,
) -> dict[str, Any]:
    """Which required evidence dimensions were looked at (present+fresh), and is the packet complete?"""
    available = [dimension for dimension in required if _dimension_available(packet, dimension)]
    missing = [dimension for dimension in required if dimension not in available]
    return {
        "required_dimensions": list(required),
        "available_dimensions": available,
        "missing_dimensions": missing,
        "evidence_complete": not missing,
    }


def evaluate_beta_guard(packet: dict[str, Any]) -> dict[str, Any]:
    """Is the directional RETURN-based support market beta rather than alpha?

    Beta-only is flagged only when benchmark-excess was evaluated AND it is a beta classification or
    an explicitly negative excess. Absence of benchmark-excess is handled by completeness, not here;
    and `excess_positive is None` (an event thesis making no returns claim) is NOT beta.
    """
    benchmark = packet.get("benchmark_excess") if isinstance(packet, dict) else None
    if not isinstance(benchmark, dict) or not _dimension_available(packet, "benchmark_excess"):
        return {"benchmark_excess_present": False, "beta_only_support": False, "classification": None, "excess_positive": None}
    classification = str(benchmark.get("classification") or "").strip()
    excess_positive = benchmark.get("excess_positive")
    beta_only = classification in BETA_ONLY_CLASSIFICATIONS or excess_positive is False
    return {
        "benchmark_excess_present": True,
        "beta_only_support": bool(beta_only),
        "classification": classification or None,
        "excess_positive": bool(excess_positive) if excess_positive is not None else None,
    }


def evaluate_hypothesis_match(
    packet: dict[str, Any],
    matched_hypothesis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Does the decision match a VALID HYPOTHESIS (validated/production playbook, conditions met)?

    Source is the explicit `matched_hypothesis` arg, else `packet["hypothesis_match"]`. A valid
    match is itself sufficient grounding -- it is the operator's experience-encoded statement that
    these conditions are enough, even on a thin set of dimensions.
    """
    source = matched_hypothesis if matched_hypothesis is not None else (
        packet.get("hypothesis_match") if isinstance(packet, dict) else None
    )
    if not isinstance(source, dict):
        return {"present": False, "is_valid_authority": False, "status": None, "hypothesis_id": None, "conditions_met": None}
    status = str(source.get("status") or "").strip().lower()
    conditions_met = source.get("conditions_met")
    valid = status in VALID_HYPOTHESIS_STATUSES and bool(conditions_met)
    return {
        "present": True,
        "is_valid_authority": bool(valid),
        "status": status or None,
        "hypothesis_id": source.get("hypothesis_id") or source.get("id"),
        "conditions_met": None if conditions_met is None else bool(conditions_met),
    }


def validate_decision_grounding(
    *,
    action: str,
    cited_dimensions: list[str] | None,
    packet: dict[str, Any],
    matched_hypothesis: dict[str, Any] | None = None,
    min_supportive: int = DEFAULT_MIN_SUPPORTIVE_DIMENSIONS,
) -> dict[str, Any]:
    """Is a proposed decision data-grounded -- complete, not beta-only, not contradicted, corroborated?

    A directional decision is grounded when the packet is complete, support is not beta-only, NO
    required dimension confidently contradicts the thesis, AND (a valid hypothesis matches OR
    >= min_supportive distinct dimensions confidently support it). Supportive/contradicting counts
    come from the packet's own verdicts (deterministic) -- the LLM cannot ground a call by what it
    cites. `cited_dimensions` is recorded for audit only.
    """
    normalized_action = str(action or "").strip().upper()
    completeness = build_evidence_completeness(packet)
    available = set(completeness["available_dimensions"])
    cited_unique = sorted({d for d in (cited_dimensions or []) if d in available})
    beta_guard = evaluate_beta_guard(packet)
    hypothesis = evaluate_hypothesis_match(packet, matched_hypothesis)
    directional = normalized_action in DIRECTIONAL_ACTIONS

    verdicts = _classify_verdicts(packet, REQUIRED_EVIDENCE_DIMENSIONS)
    supportive = verdicts["supportive"]
    contradicting = verdicts["contradicting"]

    hypothesis_path = bool(hypothesis["is_valid_authority"])
    corroboration_path = len(supportive) >= int(min_supportive)
    sufficient = hypothesis_path or corroboration_path
    if hypothesis_path:
        sufficiency_path = "valid_hypothesis_match"
    elif corroboration_path:
        sufficiency_path = "multi_dimension_corroboration"
    else:
        sufficiency_path = None

    reasons: list[str] = []
    if directional:
        if not completeness["evidence_complete"]:
            reasons.append(f"evidence_incomplete: missing {completeness['missing_dimensions']}")
        if beta_guard["beta_only_support"]:
            reasons.append(f"beta_only_support: benchmark-excess classification {beta_guard.get('classification')}")
        if contradicting:
            reasons.append(f"contradicting_evidence: {contradicting}")
        if not sufficient:
            reasons.append(
                f"insufficient_corroboration: {len(supportive)} supportive dimensions "
                f"(< {int(min_supportive)}) and no valid hypothesis match"
            )
    else:
        # Non-directional (WATCH / NO_ACTION / HOLD / REDUCE_EXPOSURE_REVIEW): review-only, no bar.
        pass

    grounded = not reasons
    return {
        "action": normalized_action,
        "directional": directional,
        "cited_dimensions": cited_unique,
        "supporting_dimensions": supportive,
        "contradicting_dimensions": contradicting,
        "supportive_count": len(supportive),
        "min_supportive_dimensions": int(min_supportive),
        "sufficiency_path": sufficiency_path,
        "hypothesis_match": hypothesis,
        "data_grounded": grounded,
        "grounding_failures": reasons,
    }


def build_llm_decision_contract(
    *,
    symbol: str,
    proposed_action: str,
    packet: dict[str, Any],
    cited_dimensions: list[str] | None = None,
    matched_hypothesis: dict[str, Any] | None = None,
    conviction: float | None = None,
    rationale: str | None = None,
    graduation_passed: bool = False,
    min_supportive: int = DEFAULT_MIN_SUPPORTIVE_DIMENSIONS,
) -> dict[str, Any]:
    """Assemble the full deterministic decision contract for an LLM-proposed action.

    Audit-only: `broker_execution_allowed` is always False here (no live bridge exists). The
    contract records `meets_data_grounding_for_live` (the DATA bar), the master flag, and whether
    outcome-monitoring graduation passed, so a future live bridge ([P-LLM-AUTH].5) has everything it
    needs while nothing in .1 can move capital.
    """
    completeness = build_evidence_completeness(packet)
    beta_guard = evaluate_beta_guard(packet)
    grounding = validate_decision_grounding(
        action=proposed_action,
        cited_dimensions=cited_dimensions,
        packet=packet,
        matched_hypothesis=matched_hypothesis,
        min_supportive=min_supportive,
    )
    meets_data_bar = bool(
        grounding["data_grounded"]
        and completeness["evidence_complete"]
        and not beta_guard["beta_only_support"]
    )
    live_eligible = bool(meets_data_bar and LLM_DIRECT_AUTHORITY_ENABLED and graduation_passed)
    return {
        "schema_version": 2,
        "symbol": str(symbol or "").strip().upper(),
        "proposed_action": grounding["action"],
        "conviction": None if conviction is None else float(conviction),
        "rationale": rationale,
        "evidence_completeness": completeness,
        "beta_guard": beta_guard,
        "grounding": grounding,
        "sufficiency_path": grounding["sufficiency_path"],
        "hypothesis_match": grounding["hypothesis_match"],
        "meets_data_grounding_for_live": meets_data_bar,
        # authority stamp -- .1 never grants broker authority
        "authority_scope": "llm_decision_contract_review_input_only",
        "broker_execution_allowed": False,
        "live_authority_master_flag": bool(LLM_DIRECT_AUTHORITY_ENABLED),
        "graduation_passed": bool(graduation_passed),
        "eligible_for_live_authority": live_eligible,
    }
