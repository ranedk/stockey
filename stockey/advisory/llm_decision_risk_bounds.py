"""Deterministic risk / position-sizing bounds for LLM-direct trade decisions ([P-LLM-AUTH].2).

The safety floor the operator chose (2026-06-23) in place of paper-first graduation: the LLM may
make strong, data-grounded calls and act, but every live entry is sized within deterministic
position / sector / total-exposure caps with a stop, so no single bad call is catastrophic
(risk-of-ruin protection). This is survival math, not LLM distrust — every durable discretionary
investor trades inside risk limits.

Pure and additive: no DB, no LLM call, no broker. It turns a data-grounded decision contract
(`advisory/llm_decision_contract`) + account state into a bounded sizing plan. It produces a plan
only; `broker_execution_allowed` is always False here (the live bridge is [P-LLM-AUTH].5).
"""

from __future__ import annotations

import math
import os
from typing import Any

# Caps as fractions of total capital. Conservative defaults; operator tunes per deployment.
LLM_DECISION_MAX_POSITION_PCT = float(os.getenv("LLM_DECISION_MAX_POSITION_PCT", "0.05"))
LLM_DECISION_MIN_POSITION_PCT = float(os.getenv("LLM_DECISION_MIN_POSITION_PCT", "0.01"))
LLM_DECISION_MAX_SECTOR_EXPOSURE_PCT = float(os.getenv("LLM_DECISION_MAX_SECTOR_EXPOSURE_PCT", "0.25"))
LLM_DECISION_MAX_TOTAL_EXPOSURE_PCT = float(os.getenv("LLM_DECISION_MAX_TOTAL_EXPOSURE_PCT", "1.0"))
LLM_DECISION_STOP_ATR_MULT = float(os.getenv("LLM_DECISION_STOP_ATR_MULT", "2.0"))

ENTRY_ACTIONS = {"BUY", "BUY_MORE"}
EXIT_ACTIONS = {"SELL", "PARTIAL_SELL"}


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def bound_position_size(
    *,
    contract: dict[str, Any],
    capital: float,
    price: float | None,
    conviction: float | None = None,
    atr: float | None = None,
    current_total_exposure_inr: float = 0.0,
    current_sector_exposure_inr: float = 0.0,
    max_position_pct: float | None = None,
    max_sector_exposure_pct: float | None = None,
    max_total_exposure_pct: float | None = None,
    min_position_pct: float | None = None,
    stop_atr_mult: float | None = None,
    size_multiplier: float = 1.0,
) -> dict[str, Any]:
    """Bound a (data-grounded) LLM entry decision to a survivable position size + stop.

    Only entries (BUY / BUY_MORE) consume risk budget. Exits (SELL / PARTIAL_SELL) reduce risk and
    are passed through (sizing is the position being reduced, decided elsewhere). A decision that
    does not meet the data-grounding bar (`meets_data_grounding_for_live`) is not sized at all.
    """
    max_position_pct = float(LLM_DECISION_MAX_POSITION_PCT if max_position_pct is None else max_position_pct)
    min_position_pct = float(LLM_DECISION_MIN_POSITION_PCT if min_position_pct is None else min_position_pct)
    max_sector_exposure_pct = float(LLM_DECISION_MAX_SECTOR_EXPOSURE_PCT if max_sector_exposure_pct is None else max_sector_exposure_pct)
    max_total_exposure_pct = float(LLM_DECISION_MAX_TOTAL_EXPOSURE_PCT if max_total_exposure_pct is None else max_total_exposure_pct)
    stop_atr_mult = float(LLM_DECISION_STOP_ATR_MULT if stop_atr_mult is None else stop_atr_mult)

    action = str((contract or {}).get("proposed_action") or "").strip().upper()
    conviction_value = _num(conviction)
    if conviction_value is None:
        conviction_value = _num((contract or {}).get("conviction"))
    conviction_clamped = 0.5 if conviction_value is None else max(0.0, min(1.0, conviction_value))

    plan: dict[str, Any] = {
        "schema_version": 1,
        "symbol": (contract or {}).get("symbol"),
        "proposed_action": action,
        "conviction": conviction_clamped,
        "allowed": False,
        "position_inr": 0.0,
        "position_pct_of_capital": 0.0,
        "quantity": 0,
        "stop_price": None,
        "binding_constraint": None,
        "caps": {
            "max_position_pct": max_position_pct,
            "min_position_pct": min_position_pct,
            "max_sector_exposure_pct": max_sector_exposure_pct,
            "max_total_exposure_pct": max_total_exposure_pct,
            "stop_atr_mult": stop_atr_mult,
        },
        "authority_scope": "llm_decision_sizing_plan_review_input_only",
        "broker_execution_allowed": False,
        "reasons": [],
    }

    if not (contract or {}).get("meets_data_grounding_for_live"):
        plan["binding_constraint"] = "decision_not_data_grounded"
        plan["reasons"].append("decision_not_data_grounded")
        return plan
    if action in EXIT_ACTIONS:
        # Exits reduce risk; they are not capital-budget bounded. Pass through with a stop note.
        plan["allowed"] = True
        plan["binding_constraint"] = "exit_not_capital_bounded"
        plan["reasons"].append("exit_action_reduces_risk_not_sized_here")
        return plan
    if action not in ENTRY_ACTIONS:
        plan["binding_constraint"] = "non_entry_action"
        plan["reasons"].append(f"action_not_entry:{action or 'none'}")
        return plan

    capital_value = _num(capital)
    price_value = _num(price)
    if not capital_value or capital_value <= 0 or not price_value or price_value <= 0:
        plan["binding_constraint"] = "missing_capital_or_price"
        plan["reasons"].append("missing_or_nonpositive_capital_or_price")
        return plan

    effective_pct = min_position_pct + conviction_clamped * max(0.0, max_position_pct - min_position_pct)
    effective_pct = max(0.0, min(max_position_pct, effective_pct))
    # Regime headwind (or other caller-supplied) size penalty: a real thesis can trade into a weak
    # tape, but smaller. Multiplier in [0, 1]; 1.0 is no penalty.
    multiplier = max(0.0, min(1.0, float(size_multiplier)))
    conviction_sized_inr = capital_value * effective_pct * multiplier
    sector_headroom = max(0.0, capital_value * max_sector_exposure_pct - max(0.0, _num(current_sector_exposure_inr) or 0.0))
    total_headroom = max(0.0, capital_value * max_total_exposure_pct - max(0.0, _num(current_total_exposure_inr) or 0.0))

    candidates = {
        "conviction_position_cap": conviction_sized_inr,
        "sector_exposure_cap": sector_headroom,
        "total_exposure_cap": total_headroom,
    }
    binding_constraint = min(candidates, key=lambda key: candidates[key])
    position_inr = max(0.0, min(candidates.values()))

    plan["size_multiplier"] = multiplier
    plan["binding_constraint"] = binding_constraint
    plan["position_inr"] = round(position_inr, 2)
    plan["position_pct_of_capital"] = round(position_inr / capital_value, 6)
    plan["quantity"] = int(position_inr // price_value)
    atr_value = _num(atr)
    if atr_value is not None and atr_value > 0:
        plan["stop_price"] = round(price_value - stop_atr_mult * atr_value, 4)
    plan["allowed"] = position_inr > 0 and plan["quantity"] > 0
    if not plan["allowed"]:
        plan["reasons"].append("bounded_size_rounds_to_zero")
    return plan
