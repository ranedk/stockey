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

import json
import math
import os
from typing import Any, Callable

# Canonical authoritative hypothesis statuses -- MUST mirror
# advisory.hypothesis_engine.TRUSTED_OVERLAY_STATUSES. The engine normalizes authored statuses
# (validated -> active_review, production -> trusted_overlay), so the real promoted/trusted status is
# "trusted_overlay"; only these confer decision authority.
HYPOTHESIS_AUTHORITY_STATUSES = {"trusted_overlay", "production"}

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

# Technicals are TIMING, not the thesis (operator design 2026-06-24): only a real downtrend (below
# the 200DMA) or MATERIAL underperformance contradicts the timing. A mild relative lag while still in
# an uptrend is "quiet basing" = acceptable timing / room to grow, NOT a contradiction -- exactly the
# pre-move setups a thesis wants to catch. "Played out vs room" nuance is left to the LLM over the
# enriched timing components.
TECHNICAL_LAGGING_RS = -0.08

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
    "fundamental",
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


def technical_dimension(row: dict[str, Any] | None, asof_date: Any, *, series_factors: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(row, dict) or not row:
        if not series_factors:
            return _absent("missing")
        row = {}
    gates = {gate: _as_bool(row.get(gate)) for gate in TECHNICAL_ENTRY_GATES}
    known = [value for value in gates.values() if value is not None]
    strength = (sum(1 for value in known if value) / len(known)) if known else None
    rs = _num(row.get("rs_vs_benchmark"))
    above_50 = gates.get("pass_above_dma_50")
    above_200 = gates.get("pass_above_dma_200")
    near_high = gates.get("pass_near_52w_high")
    liquid = gates.get("pass_liquidity_20d")

    # Timing verdict: only a real downtrend or material underperformance is a contradiction. A mild
    # lag in an uptrend is neutral (room to grow), not a veto.
    if above_200 is False:
        direction, confidence = "contradicting", "medium"            # downtrend
    elif rs is not None and rs <= TECHNICAL_LAGGING_RS:
        direction, confidence = "contradicting", "high"              # materially lagging
    elif rs is not None and rs > 0 and above_50 and above_200:
        direction, confidence = "supportive", ("high" if rs > 0.05 and near_high else "medium")
    else:
        direction, confidence = "neutral", "low"                     # quiet basing / mild lag = ok timing

    # Fallback to series-computed factors (deep history / when the pre-computed columns are null):
    # only when the row-based inputs gave no decisive verdict, so the live row stays authoritative.
    if direction == "neutral" and series_factors:
        momentum = _num(series_factors.get("momentum"))
        trend = _num(series_factors.get("trend_following"))
        if (trend is not None and trend < 0.34) or (momentum is not None and momentum <= TECHNICAL_LAGGING_RS):
            direction, confidence = "contradicting", "medium"
        elif (momentum is not None and momentum > 0) and (trend is not None and trend >= 0.67):
            direction, confidence = "supportive", "medium"

    dim = {
        "present": True,
        "fresh": _same_day(row.get("asof_date"), asof_date),
        "strength": strength,
        "status": "ok" if known else "no_gates",
        "gates_passed": sum(1 for value in known if value),
        "gates_total": len(known),
        "rs_vs_benchmark": rs,
        "liquid": liquid,
        "technical_state": row.get("technical_state"),
    }
    # Timing depth for the LLM to judge "already played out vs room to grow" (point-in-time fields;
    # the deterministic verdict stays the consistency rail, the LLM weighs how extended the move is).
    components = {
        "rs_vs_benchmark": rs, "above_dma_50": above_50, "above_dma_200": above_200, "near_52w_high": near_high,
        "liquid": liquid,
        "breakout_extension_pct": _num(row.get("breakout_extension_pct")),
        "dist_52w_high": _num(row.get("dist_52w_high")),
        "stock_ret_60d": _num(row.get("stock_ret_60d")),
    }
    # Series-computed factor depth (momentum / trend / volatility / volume) for the LLM to weigh
    # "played out vs room", computed from price when the pre-computed columns are sparse.
    if series_factors:
        components.update({key: value for key, value in series_factors.items() if value is not None})
    return _verdict(dim, direction, confidence, components)


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

    # Regime is a conviction/size FACTOR, not a blanket veto (a broad macro label must not be the
    # sole trade gate -- CLAUDE.md). Only extreme STRESS hard-contradicts; ELEVATED / RISK_OFF is a
    # headwind that shrinks alpha sizing and blocks beta participation, but does not veto a real
    # idiosyncratic thesis. A benign tape is constructive.
    if state == "STRESS":
        regime_state, direction, confidence = "stress", "contradicting", "high"
    elif state == "ELEVATED" or regime in DERISK_REGIME_NAMES:
        regime_state, direction, confidence = "headwind", "neutral", "low"
    else:
        regime_state, direction, confidence = "constructive", "neutral", "low"

    dim = {
        "present": True,
        "fresh": _same_day(row.get("asof_date"), asof_date),
        "strength": max(0.0, min(1.0, risk_on)) if risk_on is not None else None,
        "status": "ok",
        "classification": row.get("macro_risk_state"),
        "regime_name": row.get("regime_name"),
        "regime_state": regime_state,
        "regime_headwind": regime_state == "headwind",
        "macro_stress_score": _num(row.get("macro_stress_score")),
    }
    return _verdict(dim, direction, confidence, {"macro_risk_state": state or None, "regime_name": regime or None, "risk_on_score": risk_on})


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


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _hypothesis_direction(expected_effect: Any) -> str:
    """'long' | 'reduce' | 'unknown' from a hypothesis expected_effect (so a de-risk playbook does
    not ground a BUY)."""
    effect = _as_dict(expected_effect)
    blob = " ".join(str(effect.get(key, "")) for key in ("effect", "market_direction", "action_bias", "direction")).lower()
    long = any(token in blob for token in ("increase", "accumulate", "buy", "add", "positive", "long", "upside"))
    reduce = any(token in blob for token in ("reduce", "exit", "sell", "trim", "derisk", "de-risk", "negative", "short", "downside", "avoid"))
    if long and not reduce:
        return "long"
    if reduce and not long:
        return "reduce"
    return "unknown"


def fundamental_dimension(row: dict[str, Any] | None, asof_date: Any) -> dict[str, Any]:
    """Fundamental verdict from point-in-time financials -- the INDEPENDENT axis the factor validation
    showed is what makes technical+fundamental confluence pay (earnings growth is the strongest single
    factor; leverage modifies). Anchored on earnings growth, with high leverage as a contradiction."""
    if not isinstance(row, dict) or not row:
        return _absent("missing")
    growth = _num(row.get("profit_after_tax_qoq_growth"))
    revenue_growth = _num(row.get("total_revenue_qoq_growth"))
    net_debt_to_equity = _num(row.get("net_debt_to_equity"))
    fcf_to_equity = _num(row.get("free_cash_flow_to_equity"))

    high_leverage = net_debt_to_equity is not None and net_debt_to_equity > 3.0
    if growth is not None and growth < 0:
        direction, confidence = "contradicting", ("high" if growth < -0.10 else "medium")
    elif high_leverage:
        direction, confidence = "contradicting", "medium"
    elif growth is not None and growth > 0 and (revenue_growth is None or revenue_growth >= 0):
        # strong earnings growth + not over-levered is supportive; high leverage downgrades to neutral.
        leveraged = net_debt_to_equity is not None and net_debt_to_equity > 1.5
        direction = "supportive" if not leveraged else "neutral"
        confidence = ("high" if (growth > 0.15 and not leveraged) else "medium") if direction == "supportive" else "low"
    else:
        direction, confidence = "neutral", "low"

    strength = max(0.0, min(1.0, 0.5 + growth)) if growth is not None else None
    dim = {
        "present": True,
        "fresh": True,  # fundamentals are as-of the latest filing, used point-in-time by the loader
        "strength": strength,
        "status": "ok",
        "earnings_growth": growth,
        "revenue_growth": revenue_growth,
        "net_debt_to_equity": net_debt_to_equity,
        "fcf_to_equity": fcf_to_equity,
    }
    return _verdict(dim, direction, confidence, {
        "earnings_growth": growth, "revenue_growth": revenue_growth,
        "net_debt_to_equity": net_debt_to_equity, "fcf_to_equity": fcf_to_equity,
    })


def hypothesis_match_dimension(
    rows: list[dict[str, Any]] | None,
    *,
    min_score: float = HYPOTHESIS_CONDITIONS_MIN_SCORE,
) -> dict[str, Any]:
    candidates = [r for r in (rows or []) if isinstance(r, dict)]
    valid = [r for r in candidates if str(r.get("status") or "").strip().lower() in HYPOTHESIS_AUTHORITY_STATUSES]
    if not valid:
        return {"present": bool(candidates), "status": None, "conditions_met": None, "hypothesis_id": None, "direction": "unknown", "conflict": False}
    valid.sort(key=lambda r: (_num(r.get("match_score")) or 0.0), reverse=True)
    best = valid[0]
    score = _num(best.get("match_score"))
    # Constraint #2: if trusted hypotheses disagree on direction for this symbol, WITHHOLD authority
    # (do not silently pick the higher score). The disagreement is recorded for visibility.
    directions = {_hypothesis_direction(r.get("expected_effect_json") or r.get("expected_effect")) for r in valid}
    if "long" in directions and "reduce" in directions:
        return {
            "present": True,
            "status": str(best.get("status")).strip().lower(),
            "conditions_met": False,
            "hypothesis_id": best.get("hypothesis_id"),
            "match_score": score,
            "direction": "conflicted",
            "conflict": True,
            "conflicting_directions": sorted(d for d in directions if d in {"long", "reduce"}),
        }
    explicit = _as_bool(_as_dict(best.get("decision_json")).get("conditions_met"))
    conditions_met = explicit if explicit is not None else (score is not None and score >= float(min_score))
    return {
        "present": True,
        "status": str(best.get("status")).strip().lower(),
        "conditions_met": bool(conditions_met),
        "hypothesis_id": best.get("hypothesis_id"),
        "match_score": score,
        "direction": _hypothesis_direction(best.get("expected_effect_json") or best.get("expected_effect")),
        "conflict": False,
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
    fundamental_row: dict[str, Any] | None = None,
    hypothesis_rows: list[dict[str, Any]] | None = None,
    price_closes: Any = None,
    price_volumes: Any = None,
) -> dict[str, Any]:
    """Pure assembly: row dicts -> the packet consumed by `advisory/llm_decision_contract`.

    A trailing `price_closes`/`price_volumes` window (oldest..newest) enriches the technical timing
    dimension with series-computed factors (momentum / trend / volatility / volume).
    """
    series_factors = None
    if price_closes is not None and len(list(price_closes)) >= 21:
        from advisory.price_factors import compute_price_series_factors

        series_factors = compute_price_series_factors(price_closes, volumes=price_volumes)
    event_source = event_row if event_row is not None else allocation_row
    return {
        "symbol": str(symbol or "").strip().upper(),
        "asof_date": str(asof_date)[:10] if asof_date is not None else None,
        "technical_confirmation": technical_dimension(technical_row, asof_date, series_factors=series_factors),
        "risk": risk_dimension(allocation_row, asof_date),
        "market_context": market_context_dimension(market_row, asof_date),
        "sector_reliability": sector_reliability_dimension(sector_reliability_row),
        "exact_class_reliability": exact_class_reliability_dimension(exact_class_row),
        "benchmark_excess": benchmark_excess_dimension(benchmark_row),
        "event_provenance": event_provenance_dimension(event_source, asof_date),
        "fundamental": fundamental_dimension(fundamental_row, asof_date),
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
    fundamental_row = row_loader(
        "SELECT * FROM advisory_fundamentals_daily WHERE symbol=%(symbol)s AND asof_date<=%(asof)s "
        "ORDER BY asof_date DESC LIMIT 1",
        params,
    )
    series_rows = rows_loader(
        "SELECT asof_date, adj_close, volume FROM advisory_technical_daily "
        "WHERE symbol=%(symbol)s AND asof_date<=%(asof)s AND adj_close IS NOT NULL "
        "ORDER BY asof_date DESC LIMIT 320",
        params,
    )
    series_rows = list(reversed(series_rows or []))  # chronological, oldest..newest
    price_closes = [r.get("adj_close") for r in series_rows] or None
    price_volumes = [r.get("volume") for r in series_rows] or None
    hypothesis_rows = rows_loader(
        "SELECT m.*, h.status AS status FROM advisory_hypothesis_matches m "
        "JOIN advisory_hypotheses h ON h.hypothesis_id=m.hypothesis_id "
        "WHERE m.symbol=%(symbol)s AND m.published_on<=%(asof)s "
        "AND h.status IN ('trusted_overlay','production') "
        # Constraint #3: only SYMBOL-scope hypotheses are a per-symbol thesis. Market-scope ones
        # (macro/regime) must not leak into a single name's grounding via a spurious symbol tag.
        "AND h.trigger_scope='symbol' "
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
        fundamental_row=fundamental_row,
        hypothesis_rows=hypothesis_rows,
        price_closes=price_closes,
        price_volumes=price_volumes,
    )
