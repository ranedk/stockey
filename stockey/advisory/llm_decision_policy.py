"""LLM decision policy over the complete evidence packet ([P-LLM-AUTH].4b).

This is where the LLM actually decides. Given the point-in-time evidence packet built by
`advisory/llm_evidence_packet`, it asks the LLM for a structured proposal (action, conviction, the
evidence dimensions it cites, whether it matches a valid hypothesis, a rationale), then runs that
proposal through the deterministic guards:

1. `build_llm_decision_contract` (.1) -- completeness + sufficiency (hypothesis / dominant /
   aggregate, not a count) + beta guard -> `meets_data_grounding_for_live`.
2. `bound_position_size` (.2) -- if grounded, a survivable sizing plan within position / sector /
   total caps with a stop.

The LLM only proposes; the deterministic layers decide whether the proposal is data-grounded and how
large it may be. Output is review-only: `broker_execution_allowed` is always False here (the live
bridge is .5, behind the default-OFF master flag). On any LLM failure the policy degrades to a
deterministic WATCH (low conviction, no cited dimensions) and records fallback telemetry -- never a
silent crash, never a default BUY.

The LLM call is injectable (`llm_caller`) so the policy is unit-testable without a model.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from advisory.llm_decision_contract import build_llm_decision_contract
from advisory.llm_decision_risk_bounds import bound_position_size
from advisory.prompt_registry import (
    LLM_DECISION_POLICY_PROMPT_ID,
    prompt_version,
    response_schema_version,
)

DEFAULT_MODEL = os.getenv("LLM_DECISION_POLICY_MODEL", "codex")
PROMPT_ID = LLM_DECISION_POLICY_PROMPT_ID

PROPOSAL_ACTIONS = ("BUY", "BUY_MORE", "SELL", "PARTIAL_SELL", "WATCH", "NO_ACTION")

SYSTEM_PROMPT = (
    "You are a disciplined Indian-equity decision analyst. You are given a COMPLETE point-in-time "
    "evidence packet for one symbol. Decide an action only from the evidence shown -- never from "
    "outside knowledge, future data, or a single signal. A strong call rests on either a matched "
    "valid hypothesis or genuinely strong, corroborating evidence; when the evidence is thin or "
    "conflicting, choose WATCH or NO_ACTION. Cite ONLY the dimensions that actually support your "
    "decision (use the exact dimension keys). Set claims_hypothesis_match=true only if the packet's "
    "hypothesis_match is a validated/production match whose conditions are met. Be conservative: it "
    "is better to WATCH than to force a trade."
)


class LlmDecisionProposal(BaseModel):
    """The LLM's structured proposal. It is graded by the deterministic contract, not trusted as-is."""

    action: Literal["BUY", "BUY_MORE", "SELL", "PARTIAL_SELL", "WATCH", "NO_ACTION"]
    conviction: float = Field(ge=0.0, le=1.0, description="0..1 confidence in the action.")
    cited_dimensions: list[str] = Field(default_factory=list, description="Exact packet dimension keys that support the action.")
    claims_hypothesis_match: bool = Field(default=False, description="True only if a validated/production hypothesis is matched and its conditions are met.")
    rationale: str = Field(default="", description="Concise, evidence-grounded reasoning.")


def _packet_for_prompt(packet: dict[str, Any]) -> dict[str, Any]:
    """Trim the packet to the fields the LLM needs (drop noisy raw columns)."""
    keep = (
        "present", "fresh", "strength", "status", "classification", "excess_positive",
        "event_class", "regime_name", "macro_stress_score", "risk_bucket", "conviction_bucket",
        "excess_hit_rate_after_cost", "matured_count", "technical_state", "conditions_met",
        "hypothesis_id",
    )
    trimmed: dict[str, Any] = {"symbol": packet.get("symbol"), "asof_date": packet.get("asof_date")}
    for key, value in packet.items():
        if isinstance(value, dict):
            trimmed[key] = {k: v for k, v in value.items() if k in keep}
    return trimmed


def build_decision_prompt(packet: dict[str, Any]) -> str:
    import json

    payload = _packet_for_prompt(packet)
    return (
        "Evidence packet (point-in-time; decide only from this):\n"
        f"{json.dumps(payload, indent=2, default=str)}\n\n"
        "Propose action, conviction (0..1), cited_dimensions (exact keys above), "
        "claims_hypothesis_match, and a short rationale."
    )


def _deterministic_watch(reason: str) -> LlmDecisionProposal:
    return LlmDecisionProposal(action="WATCH", conviction=0.2, cited_dimensions=[], claims_hypothesis_match=False, rationale=reason)


def _run_llm(prompt: str, *, model: str, llm_caller: Callable[..., LlmDecisionProposal] | None) -> LlmDecisionProposal:
    if llm_caller is not None:
        return llm_caller(prompt, response_model=LlmDecisionProposal, model=model, system_prompt=SYSTEM_PROMPT)
    from utils.codex_cli import run_codex_structured

    return run_codex_structured(prompt, response_model=LlmDecisionProposal, model=model, system_prompt=SYSTEM_PROMPT, max_attempts=2)


def decide(
    packet: dict[str, Any],
    *,
    capital: float | None = None,
    price: float | None = None,
    atr: float | None = None,
    current_total_exposure_inr: float = 0.0,
    current_sector_exposure_inr: float = 0.0,
    model: str | None = None,
    use_llm: bool = True,
    llm_caller: Callable[..., LlmDecisionProposal] | None = None,
) -> dict[str, Any]:
    """Produce a graded, sized, review-only decision from an evidence packet.

    Returns a dict with the proposal, the .1 contract, the .2 sizing plan, the LLM status/provenance,
    and `broker_execution_allowed=False` (always). Never raises on LLM failure -- degrades to WATCH.
    """
    model = model or DEFAULT_MODEL
    llm_status = "ok"
    llm_error: str | None = None

    if not use_llm:
        proposal = _deterministic_watch("LLM disabled; deterministic WATCH.")
        llm_status = "disabled"
    else:
        try:
            proposal = _run_llm(build_decision_prompt(packet), model=model, llm_caller=llm_caller)
        except Exception as exc:  # noqa: BLE001 - degrade, never crash the pipeline
            llm_status = "fallback_after_error"
            llm_error = f"{type(exc).__name__}: {exc}"
            proposal = _deterministic_watch("LLM failed; deterministic WATCH fallback.")
            try:
                from advisory.fallback_telemetry import record_fallback_event

                record_fallback_event(
                    module="advisory.llm_decision_policy",
                    fallback_type="llm_decision_policy_failed",
                    source=PROMPT_ID,
                    severity="warn",
                    symbol=str(packet.get("symbol") or "") or None,
                    reason="LLM decision policy failed; deterministic WATCH fallback used.",
                    deterministic_fallback=True,
                    error=exc,
                    metadata={"model": model},
                )
            except Exception:
                pass  # telemetry must never mask the original degradation

    # The LLM's hypothesis claim never overrides the packet: .1 validates the actual match.
    matched_hypothesis = packet.get("hypothesis_match") if isinstance(packet, dict) else None
    contract = build_llm_decision_contract(
        symbol=str(packet.get("symbol") or ""),
        proposed_action=proposal.action,
        packet=packet,
        cited_dimensions=list(proposal.cited_dimensions),
        matched_hypothesis=matched_hypothesis,
        conviction=proposal.conviction,
        rationale=proposal.rationale,
    )

    sizing = bound_position_size(
        contract=contract,
        capital=capital or 0.0,
        price=price,
        conviction=proposal.conviction,
        atr=atr,
        current_total_exposure_inr=current_total_exposure_inr,
        current_sector_exposure_inr=current_sector_exposure_inr,
    )

    return {
        "schema_version": 1,
        "symbol": contract["symbol"],
        "asof_date": packet.get("asof_date") if isinstance(packet, dict) else None,
        "proposal": proposal.model_dump(),
        "contract": contract,
        "sizing": sizing,
        "meets_data_grounding_for_live": contract["meets_data_grounding_for_live"],
        "sufficiency_path": contract.get("sufficiency_path"),
        "broker_execution_allowed": False,
        "authority_scope": "llm_decision_policy_review_only",
        "llm_status": llm_status,
        "llm_error": llm_error,
        "prompt_id": PROMPT_ID,
        "prompt_version": prompt_version(PROMPT_ID),
        "prompt_schema_version": response_schema_version(PROMPT_ID),
        "llm_model": model,
    }
