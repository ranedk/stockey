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
2. SUFFICIENCY -- is the evidence consistent and corroborated enough to act, and in what MODE? The
   dimensions are NOT a flat symmetric vote (operator design 2026-06-24): non-technical evidence is
   the THESIS/filter, technicals are the TIMING, and benchmark-excess CLASSIFIES the mode rather than
   blocking. Each dimension carries a structured VERDICT (direction supportive/neutral/contradicting
   + confidence). A directional decision grounds in one of two modes, and is blocked by a confident
   contradiction in any required dimension (a RISK_OFF tape, a weakening event, broken risk):
   - ALPHA: there is a non-technical THESIS (a confidently supportive thesis dimension --
     event/announcement/reliability -- OR a valid hypothesis), technical TIMING is not bad (not
     played-out / not lagging), the regime is not contradicting, AND support is not market-beta-only.
   - PARTICIPATION (beta): no thesis required, but the technical TREND is confidently supportive and
     the regime is constructive (not contradicting). Labeled beta -- captures the market without
     pretending to be alpha -- so the system can participate in a rally instead of sitting in cash.
   An earlier per-dimension magnitude score was removed: a calibration showed it had ~zero/negative
   correlation with forward benchmark-excess. Verdicts encode logical consistency (support vs
   contradict), which is sound regardless of predictive power; counts are taken over the PACKET's own
   verdicts (deterministic), so the LLM cannot ground a decision by choosing what to cite.

The BETA guard no longer blocks -- it sets the mode. Beta-only support disqualifies ALPHA but still
permits PARTICIPATION (so we don't refuse to invest just because we can't prove alpha). An event/
hypothesis thesis that makes no returns claim (excess_positive is None) is not beta.

`meets_data_grounding_for_live` is True when the packet is complete and the decision grounds in
either mode. `decision_mode` records which. This is the DATA bar -- separate from
`live_authority_master_flag` (LLM_DIRECT_AUTHORITY_ENABLED) and from outcome-monitoring graduation.
"""

from __future__ import annotations

import os
from typing import Any

# Master switch for any LIVE LLM->broker decision authority. DEFAULTS OFF. Building the contract is
# always allowed (audit-only); no contract grants broker authority unless this is enabled AND the
# policy passed outcome-monitoring graduation ([P-LLM-AUTH].3/.4), neither of which exists yet.
LLM_DIRECT_AUTHORITY_ENABLED = os.getenv("LLM_DIRECT_AUTHORITY_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}

# A verdict counts as decisive (supportive or contradicting) only at this confidence or above.
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
DECISIVE_CONFIDENCE = "medium"

# Dimension ROLES (operator design 2026-06-24): non-technical evidence is the thesis/filter,
# technicals are the timing, market context is the regime gate.
THESIS_DIMENSIONS = ("event_provenance", "sector_reliability", "exact_class_reliability")
TIMING_DIMENSION = "technical_confirmation"
REGIME_DIMENSION = "market_context"

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


def _direction(packet: dict[str, Any], dimension: str) -> str:
    """Decisive direction of a present dimension's verdict, else 'neutral'."""
    value = packet.get(dimension) if isinstance(packet, dict) else None
    if not isinstance(value, dict) or not _dimension_available(packet, dimension) or not _decisive(value):
        return "neutral"
    direction = str(value.get("direction") or "").strip().lower()
    return direction if direction in {"supportive", "contradicting"} else "neutral"


# Size penalty applied to an ALPHA decision taken into a regime headwind (a real thesis can trade,
# but smaller). Participation is not allowed under a headwind at all.
REGIME_HEADWIND_SIZE_MULTIPLIER = 0.5


def _regime_state(packet: dict[str, Any]) -> str:
    """'constructive' | 'headwind' | 'stress' | 'unknown' from the market_context dimension."""
    market = packet.get(REGIME_DIMENSION) if isinstance(packet, dict) else None
    if not isinstance(market, dict) or not _dimension_available(packet, REGIME_DIMENSION):
        return "unknown"
    state = str(market.get("regime_state") or "").strip().lower()
    if state in {"constructive", "headwind", "stress"}:
        return state
    # Fallback for synthetic packets without an explicit regime_state.
    return "stress" if _direction(packet, REGIME_DIMENSION) == "contradicting" else "constructive"


def validate_decision_grounding(
    *,
    action: str,
    cited_dimensions: list[str] | None,
    packet: dict[str, Any],
    matched_hypothesis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Is a proposed decision grounded, and in what MODE (alpha vs market participation)?

    Hierarchical, not a flat vote: non-technical dimensions are the thesis/filter, technicals are
    the timing, market context is the regime gate, benchmark-excess classifies the mode. A confident
    contradiction in any required dimension vetoes both modes. Counts come from the packet's verdicts
    (deterministic); `cited_dimensions` is recorded for audit only.
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

    thesis_dimensions = [d for d in THESIS_DIMENSIONS if _direction(packet, d) == "supportive"]
    hypothesis_path = bool(hypothesis["is_valid_authority"])
    has_thesis = bool(thesis_dimensions) or hypothesis_path
    technical_direction = _direction(packet, TIMING_DIMENSION)
    timing_ok = technical_direction != "contradicting"          # not played-out / not lagging
    technical_supportive = technical_direction == "supportive"
    beta_only = bool(beta_guard["beta_only_support"])

    # Regime is a size factor, not a blanket veto: extreme stress hard-blocks (via the contradiction
    # in `contradicting`), a headwind shrinks alpha sizing and forbids participation, constructive
    # is clear. (`unknown` is treated as a headwind -- size down, no participation.)
    regime_state = _regime_state(packet)
    regime_constructive = regime_state == "constructive"
    regime_headwind = regime_state in {"headwind", "unknown"}
    size_multiplier = REGIME_HEADWIND_SIZE_MULTIPLIER if regime_headwind else 1.0

    base_ok = completeness["evidence_complete"] and not contradicting
    alpha_ok = base_ok and has_thesis and timing_ok and not beta_only          # allowed into a headwind, sized down
    participation_ok = base_ok and technical_supportive and regime_constructive  # beta only in a constructive tape

    if alpha_ok:
        decision_mode = "alpha"
        sufficiency_path = "valid_hypothesis_match" if hypothesis_path else "alpha_thesis_timing"
    elif participation_ok:
        decision_mode = "participation"
        sufficiency_path = "market_participation"
    else:
        decision_mode = None
        sufficiency_path = None

    reasons: list[str] = []
    if directional and decision_mode is None:
        if not completeness["evidence_complete"]:
            reasons.append(f"evidence_incomplete: missing {completeness['missing_dimensions']}")
        if contradicting:
            reasons.append(f"contradicting_evidence: {contradicting}")
        if not has_thesis and not technical_supportive:
            reasons.append("no_thesis_and_no_participation_trend: needs a non-technical thesis or a supportive trend")
        elif has_thesis and not timing_ok:
            reasons.append("timing_contradicts: thesis present but technical timing is played-out/lagging")
        elif has_thesis and timing_ok and beta_only:
            reasons.append("alpha_blocked_beta_only_and_no_participation_trend")
        elif not has_thesis and technical_supportive and not regime_constructive:
            reasons.append(f"participation_blocked_by_regime: {regime_state}")

    grounded = directional and decision_mode is not None
    if not directional:
        grounded = True  # non-directional (WATCH/NO_ACTION/...) is review-only, no bar
    return {
        "action": normalized_action,
        "directional": directional,
        "decision_mode": decision_mode,
        "is_beta_participation": decision_mode == "participation",
        "cited_dimensions": cited_unique,
        "thesis_dimensions": thesis_dimensions,
        "has_thesis": has_thesis,
        "technical_direction": technical_direction,
        "timing_ok": timing_ok,
        "regime_state": regime_state,
        "regime_headwind": regime_headwind,
        "size_multiplier": size_multiplier,
        "beta_only_support": beta_only,
        "supporting_dimensions": supportive,
        "contradicting_dimensions": contradicting,
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
) -> dict[str, Any]:
    """Assemble the full deterministic decision contract for an LLM-proposed action.

    Audit-only: `broker_execution_allowed` is always False here (no live bridge exists). The
    contract records `meets_data_grounding_for_live` (the DATA bar), `decision_mode` (alpha vs
    market participation), the master flag, and whether outcome-monitoring graduation passed, so a
    future live bridge ([P-LLM-AUTH].5) has everything it needs while nothing in .1 can move capital.
    """
    completeness = build_evidence_completeness(packet)
    beta_guard = evaluate_beta_guard(packet)
    grounding = validate_decision_grounding(
        action=proposed_action,
        cited_dimensions=cited_dimensions,
        packet=packet,
        matched_hypothesis=matched_hypothesis,
    )
    # Beta no longer blocks -- it sets the mode. The data bar is met when the decision grounds in
    # either mode (alpha or participation) over a complete packet.
    meets_data_bar = bool(grounding["data_grounded"] and completeness["evidence_complete"])
    live_eligible = bool(meets_data_bar and LLM_DIRECT_AUTHORITY_ENABLED and graduation_passed)
    return {
        "schema_version": 3,
        "symbol": str(symbol or "").strip().upper(),
        "proposed_action": grounding["action"],
        "conviction": None if conviction is None else float(conviction),
        "rationale": rationale,
        "evidence_completeness": completeness,
        "beta_guard": beta_guard,
        "grounding": grounding,
        "decision_mode": grounding["decision_mode"],
        "is_beta_participation": grounding["is_beta_participation"],
        "regime_state": grounding["regime_state"],
        "recommended_size_multiplier": grounding["size_multiplier"],
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
