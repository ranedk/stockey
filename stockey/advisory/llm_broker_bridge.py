"""LLM decision -> broker-capable action contract bridge ([P-LLM-AUTH].5).

The ONE place a graduated, data-grounded, risk-bounded LLM decision becomes a broker-capable action
contract -- the shape the existing execution engine (`advisory/execution_engine.py`) consumes. It is
deliberately a TRANSLATION layer, not a submit path:

- It NEVER calls the broker (`place_order` / `submit_live_orders`) and NEVER writes into
  `advisory_action_recommendations`. It builds the contract and decides `broker_execution_allowed`.
  Whether that contract ever reaches a broker still requires the full, unchanged execution-safety
  contract: `STOCKEY_LIVE_TRADING_ENABLED` (default OFF), the per-run confirmation token, operator
  approval, broker reconciliation, the evidence checklist, max-order-value, and the Dhan
  identity hard-fail in `apply_live_execution_safety()`.
- So two INDEPENDENT default-OFF master flags must BOTH be on for any LLM capital movement:
  `LLM_DIRECT_AUTHORITY_ENABLED` (LLM authority, gated here) AND `STOCKEY_LIVE_TRADING_ENABLED`
  (global live trading, in the execution engine) -- plus every gate above. Defense in depth.

Pure and additive: no DB, no broker, no submit. `broker_execution_allowed` is True only when the
master flag is on AND the decision is data-grounded (.1) AND graduated (.3 monitoring) AND sized
(.2) AND the action is broker-capable; otherwise False, with every blocking reason recorded.
"""

from __future__ import annotations

import json
from typing import Any

BROKER_CAPABLE_ACTIONS = {"BUY", "BUY_MORE", "SELL", "PARTIAL_SELL"}
ENTRY_ACTIONS = {"BUY", "BUY_MORE"}
EXIT_ACTIONS = {"SELL", "PARTIAL_SELL"}


def _master_flag(master_flag_enabled: bool | None) -> bool:
    if master_flag_enabled is not None:
        return bool(master_flag_enabled)
    from advisory import llm_decision_contract

    return bool(llm_decision_contract.LLM_DIRECT_AUTHORITY_ENABLED)


def evaluate_llm_broker_authority(
    result: dict[str, Any],
    *,
    master_flag_enabled: bool | None = None,
) -> dict[str, Any]:
    """Decide whether a decide() result may become a broker-capable action. Records every block.

    Eligibility is recomputed here (not trusted from the stale contract stamp) from the master flag
    + data bar + graduation, so it is robust to a master-flag override at bridge time.
    """
    master = _master_flag(master_flag_enabled)
    contract = result.get("contract") or {}
    sizing = result.get("sizing") or {}
    action = str((result.get("proposal") or {}).get("action") or "").strip().upper()

    data_bar = bool(contract.get("meets_data_grounding_for_live"))
    graduated = bool(contract.get("graduation_passed"))
    llm_status = result.get("llm_status")

    reasons: list[str] = []
    if not master:
        reasons.append("llm_direct_authority_master_flag_off")
    if not data_bar:
        reasons.append("decision_not_data_grounded")
    if not graduated:
        reasons.append("decision_not_graduated")
    if llm_status != "ok":
        reasons.append(f"llm_status_not_ok:{llm_status}")
    if action not in BROKER_CAPABLE_ACTIONS:
        reasons.append(f"action_not_broker_capable:{action or 'none'}")
    if action in ENTRY_ACTIONS and not sizing.get("allowed"):
        reasons.append("sizing_not_allowed")

    allowed = not reasons
    return {
        "broker_execution_allowed": allowed,
        "authority_scope": "llm_direct_broker" if allowed else "llm_decision_review_only",
        "master_flag_enabled": master,
        "data_grounded": data_bar,
        "graduated": graduated,
        "action": action,
        "blocked_reasons": reasons,
    }


def build_broker_action_contract(
    result: dict[str, Any],
    *,
    decided_at: Any,
    reference_price: float | None,
    setup_id: str = "LLM_DIRECT",
    soft_gate_overrides: list[dict[str, Any]] | None = None,
    master_flag_enabled: bool | None = None,
) -> dict[str, Any]:
    """Translate a graduated LLM decision into the action-recommendation contract execution consumes.

    Returns {"action_contract": <row dict>, "broker_authority": <authority dict>}. The row mirrors
    `advisory_action_recommendations` columns; `broker_execution_allowed` comes from the authority
    gate. Any deterministic soft-gate override the decision relied on must be passed in
    `soft_gate_overrides` so it is recorded in the reason contract (CLAUDE.md authority rule).
    """
    authority = evaluate_llm_broker_authority(result, master_flag_enabled=master_flag_enabled)
    allowed = authority["broker_execution_allowed"]

    proposal = result.get("proposal") or {}
    sizing = result.get("sizing") or {}
    contract = result.get("contract") or {}
    action = authority["action"]
    transaction_type = "BUY" if action in ENTRY_ACTIONS else ("SELL" if action in EXIT_ACTIONS else action)
    symbol = str(result.get("symbol") or "").strip().upper()
    grounded = bool(result.get("meets_data_grounding_for_live"))
    conviction = proposal.get("conviction")

    reason_contract = {
        "reason_contract_status": "complete" if grounded else "incomplete",
        "precondition_status": "complete" if grounded else "incomplete",
        "missing_preconditions": [] if grounded else ["data_grounding"],
        "source": "llm_direct_authority",
        "prompt_id": result.get("prompt_id"),
        "prompt_version": result.get("prompt_version"),
        "prompt_schema_version": result.get("prompt_schema_version"),
        "llm_model": result.get("llm_model"),
        "sufficiency_path": result.get("sufficiency_path"),
        "grounding": contract.get("grounding"),
        "evidence_completeness": contract.get("evidence_completeness"),
        "beta_guard": contract.get("beta_guard"),
        "sizing": sizing,
        "conviction": conviction,
        "rationale": proposal.get("rationale"),
        "soft_gate_overrides": list(soft_gate_overrides or []),
        "broker_authority": authority,
    }

    row = {
        "asof_date": result.get("asof_date"),
        "published_on": result.get("asof_date"),
        "decided_at": decided_at,
        "symbol": symbol,
        "setup_id": setup_id,
        "unique_id": f"llm-{symbol}-{decided_at}",
        "action_code": action,
        "transaction_type": transaction_type,
        "execution_mode": "broker_order",
        "action_status": "llm_direct_open",
        "action_source": "llm_direct_authority",
        "source_action": action,
        "approved_allocation_inr": sizing.get("position_inr") if action in ENTRY_ACTIONS else None,
        "action_fraction": None,
        "reference_price": reference_price,
        "stop_price": sizing.get("stop_price"),
        "recommended_stop_price": sizing.get("stop_price"),
        "invest_score_pct": (float(conviction) * 100.0) if conviction is not None else None,
        "action_reason": proposal.get("rationale"),
        "recommendation_reason_json": json.dumps(reason_contract, default=str),
        "reason_contract_status": "complete" if grounded else "incomplete",
        "authority_scope": authority["authority_scope"],
        "portfolio_authority": "llm_direct" if allowed else "none",
        "full_advisory_required": False,
        "broker_execution_allowed": allowed,
    }
    return {"action_contract": row, "broker_authority": authority}
