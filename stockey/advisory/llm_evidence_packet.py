"""Evidence-packet loader for the LLM decision policy ([P-LLM-AUTH].4a).

Assembles, for a (symbol, as-of trading date), the complete point-in-time evidence packet that the
decision contract (`advisory/llm_decision_contract`) consumes: the 7 required dimensions plus a
`hypothesis_match`. Each dimension carries a structured VERDICT -- `direction` (supportive / neutral
/ contradicting) + `confidence` + `components` -- NOT a single magnitude score.

Why a verdict and not a scalar strength: a calibration over 3.6k matured stock-days showed the old
gate-fraction strength had ~zero/negative correlation with forward benchmark-excess (the highest
bucket had a sub-50% hit rate), so it cannot ground a decision. The verdict instead encodes LOGICAL
CONSISTENCY with the proposed action -- "does this dimension support or contradict the thesis" --
which does not depend on the feature being predictive, only on it being consistent. A name making
new highs but UNDERPERFORMING the benchmark (negative RS) is `technical: contradicting` for an alpha
thesis, even though every binary breakout gate passes. The contradiction is the useful signal.

`strength` (0..1) is kept as a DESCRIPTIVE field for the LLM prompt and audit only; the .1 contract
never grounds on it. Depth (`components`) is surfaced so the LLM can weigh market/fundamental/event/
technical together rather than collapse them.

Two layers: PURE normalizers + `assemble_evidence_packet(...)` (no DB; unit-tested), and thin
defensive DB loaders (`load_*`, `load_evidence_packet`) where any missing table/column/row degrades
to a `present=False` dimension (visible, never a crash).
"""

from __future__ import annotations

import math
import os
from typing import Any, Callable

# Reliability classifications that mean the family/class is a usable positive signal.
HELPFUL_RELIABILITY_CLASSIFICATIONS = {
    "candidate_helpful",
    "protective_candidate",
    "candidate_split_helpful",
}

# Reliability classifications that actively argue AGAINST the thesis (beta / negative after cost).
NEGATIVE_RELIABILITY_CLASSIFICATIONS = {
    "benchmark_beta_not_overlay_alpha",
    "benchmark_beta_not_policy_alpha",
    "benchmark_beta_not_memory_alpha",
    "benchmark_beta_not_transition_alpha",
    "benchmark_beta_not_split_alpha",
    "negative_after_cost",
    "hurts_or_no_lift",
}

# Market-context macro_risk_state values (real vocabulary: NORMAL / WATCH / ELEVATED / STRESS) that
# contradict a long thesis. NORMAL/WATCH are treated as non-headwinds (neutral, veto-only dimension)
# rather than cheap corroborators -- a benign market does not, on its own, support a name's alpha.
CONTRADICTING_MARKET_STATES = {"STRESS", "ELEVATED"}
# regime_name values that signal an active de-risk regime.
DERISK_REGIME_NAMES = {"RISK_OFF"}

# Relative strength below this (underperforming the benchmark) contradicts an ALPHA thesis even when
# absolute trend gates pass. This is a logical-consistency rail, not a magnitude prediction.
RS_CONTRADICTION_THRESHOLD = -0.02

# Technical entry gates whose pass/fail fraction is the DESCRIPTIVE technical strength (not used for
# grounding -- see module docstring).
TECHNICAL_ENTRY_GATES = (
    "pass_above_dma_50",
    "pass_above_dma_200",
    "pass_near_52w_high",
    "pass_breakout_extension",
    "pass_liquidity_20d",
    "pass_trend_alignment",
    "pass_gap_behavior",
)

CONVICTION_BUCKET_STRENGTH = {"high": 0.9, "medium": 0.6, "low": 0.3}
RISK_BUCKET_DISCOUNT = {"low": 1.0, "medium": 0.85, "medium_high": 0.65, "high": 0.4}

HYPOTHESIS_CONDITIONS_MIN_SCORE = float(os.getenv("LLM_DECISION_HYPOTHESIS_MIN_MATCH_SCORE", "0.5"))

REQUIRED_DIMENSIONS = (
    "technical_confirmation",
    "risk",
    "market_context",
    "sector_reliability",
    "exact_class_reliability",
    "benchmark_excess",
    "event_provenance",
)


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "t", "1", "yes", "y"}:
        return True
    if text in {"false", "f", "0", "no", "n"}:
        return False
    return None


def _same_day(row_date: Any, asof_date: Any) -> bool:
    try:
        import pandas as pd

        left = pd.Timestamp(row_date)
        right = pd.Timestamp(asof_date)
        if pd.isna(left) or pd.isna(right):
            return False
        return left.normalize() == right.normalize()
    except Exception:
        return str(row_date)[:10] == str(asof_date)[:10] if row_date and asof_date else False


def _verdict(dimension: dict[str, Any], direction: str, confidence: str, components: dict[str, Any]) -> dict[str, Any]:
    dimension["direction"] = direction
    dimension["confidence"] = confidence
    dimension["components"] = components
    return dimension


def _absent(reason: str) -> dict[str, Any]:
    return {"present": False, "fresh": False, "strength": None, "status": reason,
            "direction": "neutral", "confidence": "low", "components": {}}


def technical_dimension(row: dict[str, Any] | None, asof_date: Any) -> dict[str, Any]:
    if not isinstance(row, dict) or not row:
        return _absent("missing")
    gates = {gate: _as_bool(row.get(gate)) for gate in TECHNICAL_ENTRY_GATES}
    known = [value for value in gates.values() if value is not None]
    strength = (sum(1 for value in known if value) / len(known)) if known else None
    rs = _num(row.get("rs_vs_benchmark"))
    above_50 = gates.get("pass_above_dma_50")
    above_200 = gates.get("pass_above_dma_200")
    near_high = gates.get("pass_near_52w_high")

    # Verdict encodes alpha-consistency: underperforming the benchmark or being below the 200DMA
    # contradicts a long alpha thesis regardless of how many breakout gates pass.
    if rs is not None and rs <= RS_CONTRADICTION_THRESHOLD:
        direction, confidence = "contradicting", "high"
    elif above_200 is False:
        direction, confidence = "contradicting", "medium"
    elif rs is not None and rs > 0 and above_50 and above_200:
        direction, confidence = "supportive", ("high" if rs > 0.05 and near_high else "medium")
    else:
        direction, confidence = "neutral", "low"

    dim = {
        "present": True,
        "fresh": _same_day(row.get("asof_date"), asof_date),
        "strength": strength,
        "status": "ok" if known else "no_gates",
        "gates_passed": sum(1 for value in known if value),
        "gates_total": len(known),
        "rs_vs_benchmark": rs,
        "technical_state": row.get("technical_state"),
    }
    return _verdict(dim, direction, confidence, {"rs_vs_benchmark": rs, "above_dma_50": above_50, "above_dma_200": above_200, "near_52w_high": near_high})


def risk_dimension(row: dict[str, Any] | None, asof_date: Any) -> dict[str, Any]:
    if not isinstance(row, dict) or not row:
        return _absent("missing")
    status = str(row.get("allocation_status") or "").strip().lower()
    risk_bucket = str(row.get("risk_bucket") or "").strip().lower()
    conviction_bucket = str(row.get("conviction_bucket") or "").strip().lower()
    conviction = CONVICTION_BUCKET_STRENGTH.get(conviction_bucket)
    discount = RISK_BUCKET_DISCOUNT.get(risk_bucket)
    strength = round(conviction * discount, 4) if (conviction is not None and discount is not None) else None
    blocked = status in {"rejected", "abstained"}

    if blocked:
        direction, confidence = "contradicting", "high"
    elif risk_bucket == "high":
        direction, confidence = "contradicting", "medium"
    elif risk_bucket in {"low", "medium"} and conviction_bucket in {"high", "medium"}:
        direction, confidence = "supportive", ("high" if risk_bucket == "low" and conviction_bucket == "high" else "medium")
    else:
        direction, confidence = "neutral", "low"

    dim = {
        "present": not blocked,
        "fresh": _same_day(row.get("asof_date"), asof_date),
        "strength": None if blocked else strength,
        "status": status or "unknown",
        "risk_bucket": row.get("risk_bucket"),
        "conviction_bucket": row.get("conviction_bucket"),
        "stop_price": _num(row.get("stop_price")),
        "invalidation_price": _num(row.get("invalidation_price")),
    }
    return _verdict(dim, direction, confidence, {"risk_bucket": risk_bucket, "conviction_bucket": conviction_bucket, "allocation_status": status})


def market_context_dimension(row: dict[str, Any] | None, asof_date: Any) -> dict[str, Any]:
    if not isinstance(row, dict) or not row:
        return _absent("missing")
    risk_on = _num(row.get("risk_on_score"))
    state = str(row.get("macro_risk_state") or "").strip().upper()
    regime = str(row.get("regime_name") or "").strip().upper()

    # Veto-only dimension: a stressed / de-risk tape contradicts a long thesis; a benign tape is
    # neutral (not a cheap corroborator). Sign is reasoned -- too few matured rows to calibrate yet.
    if state == "STRESS" or regime in DERISK_REGIME_NAMES:
        direction, confidence = "contradicting", "high"
    elif state == "ELEVATED":
        direction, confidence = "contradicting", "medium"
    else:
        direction, confidence = "neutral", "low"

    dim = {
        "present": True,
        "fresh": _same_day(row.get("asof_date"), asof_date),
        "strength": max(0.0, min(1.0, risk_on)) if risk_on is not None else None,
        "status": "ok",
        "classification": row.get("macro_risk_state"),
        "regime_name": row.get("regime_name"),
        "macro_stress_score": _num(row.get("macro_stress_score")),
    }
    return _verdict(dim, direction, confidence, {"macro_risk_state": state or None, "risk_on_score": risk_on})


def _reliability_dimension(row: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(row, dict) or not row:
        return _absent("missing")
    classification = str(row.get("classification") or "").strip()
    helpful = classification in HELPFUL_RELIABILITY_CLASSIFICATIONS
    negative = classification in NEGATIVE_RELIABILITY_CLASSIFICATIONS
    hit_rate = _num(row.get("excess_opportunity_hit_rate_after_cost"))
    matured = _num(row.get("matured_count"))
    strength = (max(0.0, min(1.0, hit_rate)) if hit_rate is not None else None) if helpful else (0.0 if negative else None)

    if negative:
        direction, confidence = "contradicting", ("high" if (matured or 0) >= 20 else "medium")
    elif helpful:
        direction, confidence = "supportive", ("high" if (hit_rate or 0) >= 0.55 and (matured or 0) >= 20 else "medium")
    else:
        direction, confidence = "neutral", "low"

    dim = {
        "present": True,
        "fresh": True,
        "strength": strength,
        "status": "ok",
        "classification": classification or None,
        "matured_count": matured,
        "excess_hit_rate_after_cost": hit_rate,
    }
    return _verdict(dim, direction, confidence, {"classification": classification or None, "excess_hit_rate_after_cost": hit_rate, "matured_count": matured})


def sector_reliability_dimension(row: dict[str, Any] | None) -> dict[str, Any]:
    return _reliability_dimension(row)


def exact_class_reliability_dimension(row: dict[str, Any] | None) -> dict[str, Any]:
    return _reliability_dimension(row)


def benchmark_excess_dimension(row: dict[str, Any] | None) -> dict[str, Any]:
    """Feeds the beta guard AND a verdict: positive after-cost excess supports, beta/negative contradicts."""
    if not isinstance(row, dict) or not row:
        return {"present": False, "fresh": False, "strength": None, "status": "missing",
                "classification": None, "excess_positive": None, "direction": "neutral", "confidence": "low", "components": {}}
    avg_excess = _num(row.get("avg_excess_watch_return_after_cost"))
    classification = (str(row.get("classification")).strip() or None) if row.get("classification") else None
    excess_positive = None if avg_excess is None else (avg_excess > 0.0)

    if excess_positive is True:
        direction, confidence = "supportive", ("high" if (avg_excess or 0) > 0.01 else "medium")
    elif excess_positive is False:
        direction, confidence = "contradicting", "high"
    else:
        direction, confidence = "neutral", "low"  # no return claim (event thesis) -> not beta, not support

    dim = {
        "present": True,
        "fresh": True,
        "status": "ok",
        "classification": classification,
        "excess_positive": excess_positive,
        "avg_excess_after_cost": avg_excess,
    }
    return _verdict(dim, direction, confidence, {"classification": classification, "avg_excess_after_cost": avg_excess})


def event_provenance_dimension(row: dict[str, Any] | None, asof_date: Any) -> dict[str, Any]:
    if not isinstance(row, dict) or not row:
        return _absent("missing")
    event_class = row.get("event_class")
    if event_class is None or str(event_class).strip() == "":
        return {"present": False, "fresh": False, "strength": None, "status": "no_event_class",
                "direction": "neutral", "confidence": "low", "components": {}}
    confidence_value = _num(row.get("confidence"))
    # Real setup_effect vocabulary: strengthens / weakens / neutral.
    setup_effect = str(row.get("setup_effect") or "").strip().lower()

    if setup_effect == "weakens":
        direction, confidence = "contradicting", ("high" if (confidence_value or 0) >= 0.6 else "medium")
    elif setup_effect == "strengthens":
        direction, confidence = "supportive", ("high" if (confidence_value or 0) >= 0.6 else "medium")
    else:
        direction, confidence = "neutral", "low"

    dim = {
        "present": True,
        "fresh": _same_day(row.get("asof_date"), asof_date),
        "strength": max(0.0, min(1.0, confidence_value)) if confidence_value is not None else None,
        "status": "ok",
        "event_class": str(event_class).strip(),
        "setup_effect": row.get("setup_effect"),
        "score_impact": _num(row.get("score_impact")),
    }
    return _verdict(dim, direction, confidence, {"setup_effect": setup_effect or None, "confidence": confidence_value})


def hypothesis_match_dimension(
    rows: list[dict[str, Any]] | None,
    *,
    min_score: float = HYPOTHESIS_CONDITIONS_MIN_SCORE,
) -> dict[str, Any]:
    candidates = [r for r in (rows or []) if isinstance(r, dict)]
    valid = [r for r in candidates if str(r.get("status") or "").strip().lower() in {"validated", "production"}]
    if not valid:
        return {"present": bool(candidates), "status": None, "conditions_met": None, "hypothesis_id": None}
    valid.sort(key=lambda r: (_num(r.get("match_score")) or 0.0), reverse=True)
    best = valid[0]
    score = _num(best.get("match_score"))
    explicit = None
    decision = best.get("decision_json")
    if isinstance(decision, dict):
        explicit = _as_bool(decision.get("conditions_met"))
    conditions_met = explicit if explicit is not None else (score is not None and score >= float(min_score))
    return {
        "present": True,
        "status": str(best.get("status")).strip().lower(),
        "conditions_met": bool(conditions_met),
        "hypothesis_id": best.get("hypothesis_id"),
        "match_score": score,
    }


def assemble_evidence_packet(
    *,
    symbol: str,
    asof_date: Any,
    technical_row: dict[str, Any] | None = None,
    allocation_row: dict[str, Any] | None = None,
    market_row: dict[str, Any] | None = None,
    sector_reliability_row: dict[str, Any] | None = None,
    exact_class_row: dict[str, Any] | None = None,
    benchmark_row: dict[str, Any] | None = None,
    event_row: dict[str, Any] | None = None,
    hypothesis_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Pure assembly: row dicts -> the packet consumed by `advisory/llm_decision_contract`."""
    event_source = event_row if event_row is not None else allocation_row
    return {
        "symbol": str(symbol or "").strip().upper(),
        "asof_date": str(asof_date)[:10] if asof_date is not None else None,
        "technical_confirmation": technical_dimension(technical_row, asof_date),
        "risk": risk_dimension(allocation_row, asof_date),
        "market_context": market_context_dimension(market_row, asof_date),
        "sector_reliability": sector_reliability_dimension(sector_reliability_row),
        "exact_class_reliability": exact_class_reliability_dimension(exact_class_row),
        "benchmark_excess": benchmark_excess_dimension(benchmark_row),
        "event_provenance": event_provenance_dimension(event_source, asof_date),
        "hypothesis_match": hypothesis_match_dimension(hypothesis_rows),
    }


# --------------------------------------------------------------------------------------------------
# Thin defensive DB loaders. Any failure/absence yields a present=False dimension (never a crash).
# --------------------------------------------------------------------------------------------------

def _first_row(query: str, params: dict[str, Any]) -> dict[str, Any] | None:
    try:
        from utils.db import sql_to_df

        df = sql_to_df(query, params=params)
        if df is None or df.empty:
            return None
        return df.iloc[0].to_dict()
    except Exception:
        return None


def _all_rows(query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        from utils.db import sql_to_df

        df = sql_to_df(query, params=params)
        if df is None or df.empty:
            return []
        return [row.to_dict() for _, row in df.iterrows()]
    except Exception:
        return []


def load_evidence_packet(
    symbol: str,
    asof_date: Any,
    *,
    row_loader: Callable[[str, dict[str, Any]], dict[str, Any] | None] = _first_row,
    rows_loader: Callable[[str, dict[str, Any]], list[dict[str, Any]]] = _all_rows,
) -> dict[str, Any]:
    sym = str(symbol or "").strip().upper()
    params = {"symbol": sym, "asof": str(asof_date)[:10]}

    technical_row = row_loader(
        "SELECT * FROM advisory_technical_daily WHERE symbol=%(symbol)s AND asof_date=%(asof)s "
        "ORDER BY asof_date DESC LIMIT 1",
        params,
    )
    allocation_row = row_loader(
        "SELECT * FROM advisory_allocations WHERE symbol=%(symbol)s AND asof_date<=%(asof)s "
        "ORDER BY asof_date DESC, allocated_at DESC LIMIT 1",
        params,
    )
    market_row = row_loader(
        "SELECT * FROM advisory_market_context_summary_daily WHERE asof_date<=%(asof)s "
        "ORDER BY asof_date DESC LIMIT 1",
        params,
    )
    hypothesis_rows = rows_loader(
        "SELECT m.*, h.status AS status FROM advisory_hypothesis_matches m "
        "JOIN advisory_hypotheses h ON h.hypothesis_id=m.hypothesis_id "
        "WHERE m.symbol=%(symbol)s AND m.published_on<=%(asof)s "
        "AND h.status IN ('validated','production') "
        "ORDER BY m.matched_at DESC LIMIT 25",
        params,
    )

    context_class = (allocation_row or {}).get("event_class") if isinstance(allocation_row, dict) else None
    sector_row = exact_class_row = benchmark_row = None
    if context_class:
        rel_params = {"asof": params["asof"], "context_class": str(context_class).strip()}
        exact_class_row = row_loader(
            "SELECT * FROM advisory_context_watch_eval_summary WHERE context_class=%(context_class)s "
            "AND evaluated_at<=%(asof)s ORDER BY evaluated_at DESC, matured_count DESC LIMIT 1",
            rel_params,
        )
        family = (exact_class_row or {}).get("source_context") if isinstance(exact_class_row, dict) else None
        if family:
            sector_row = row_loader(
                "SELECT * FROM advisory_context_watch_eval_summary WHERE source_context=%(family)s "
                "AND evaluated_at<=%(asof)s ORDER BY evaluated_at DESC, matured_count DESC LIMIT 1",
                {"asof": params["asof"], "family": str(family).strip()},
            )
        benchmark_row = exact_class_row

    return assemble_evidence_packet(
        symbol=sym,
        asof_date=asof_date,
        technical_row=technical_row,
        allocation_row=allocation_row,
        market_row=market_row,
        sector_reliability_row=sector_row,
        exact_class_row=exact_class_row,
        benchmark_row=benchmark_row,
        event_row=allocation_row,
        hypothesis_rows=hypothesis_rows,
    )
