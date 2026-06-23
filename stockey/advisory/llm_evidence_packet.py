"""Evidence-packet loader for the LLM decision policy ([P-LLM-AUTH].4a).

Assembles, for a (symbol, as-of trading date), the complete point-in-time evidence packet that the
decision contract (`advisory/llm_decision_contract`) consumes: the 7 required dimensions plus a
`hypothesis_match`, each normalized to the contract convention `{"present", "fresh", "strength",
...}`. `strength` is a 0..1 signal-strength used by the revised .1 sufficiency model (dominant /
aggregate paths); `classification` + `excess_positive` on `benchmark_excess` feed the beta guard.

Two layers, deliberately separated:
- PURE normalizers + `assemble_evidence_packet(...)` -- no DB; take already-loaded row dicts and
  produce the packet. This is the modeling surface and is unit-tested.
- Thin defensive DB loaders (`load_*_row`, `load_evidence_packet`) -- point-in-time SELECTs against
  existing feature tables; any missing table/column/row degrades to a `present=False` dimension
  (visible, never a silent crash), per the no-silent-fallback rule.

Strength formulas here are v1 and intentionally simple/tunable; they are pinned by tests so a later
calibration can change them deliberately. No LLM, no broker authority -- this only builds the input.
"""

from __future__ import annotations

import math
import os
from typing import Any, Callable

# Reliability classifications that mean the family/class is a usable positive signal (else strength 0).
HELPFUL_RELIABILITY_CLASSIFICATIONS = {
    "candidate_helpful",
    "protective_candidate",
    "candidate_split_helpful",
}

# Technical entry gates whose pass/fail fraction is the technical_confirmation strength.
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

# A hypothesis match counts as "conditions met" when its match score reaches this (unless the
# match's decision_json carries an explicit conditions_met).
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
    """Fresh iff the row's date is the as-of date (point-in-time; no stale-row reuse)."""
    try:
        import pandas as pd

        left = pd.Timestamp(row_date)
        right = pd.Timestamp(asof_date)
        if pd.isna(left) or pd.isna(right):
            return False
        return left.normalize() == right.normalize()
    except Exception:
        return str(row_date)[:10] == str(asof_date)[:10] if row_date and asof_date else False


def _absent(reason: str) -> dict[str, Any]:
    return {"present": False, "fresh": False, "strength": None, "status": reason}


def technical_dimension(row: dict[str, Any] | None, asof_date: Any) -> dict[str, Any]:
    if not isinstance(row, dict) or not row:
        return _absent("missing")
    gates = {gate: _as_bool(row.get(gate)) for gate in TECHNICAL_ENTRY_GATES}
    known = [value for value in gates.values() if value is not None]
    strength = (sum(1 for value in known if value) / len(known)) if known else None
    return {
        "present": True,
        "fresh": _same_day(row.get("asof_date"), asof_date),
        "strength": strength,
        "status": "ok" if known else "no_gates",
        "gates_passed": sum(1 for value in known if value),
        "gates_total": len(known),
        "rs_vs_benchmark": _num(row.get("rs_vs_benchmark")),
        "technical_state": row.get("technical_state"),
    }


def risk_dimension(row: dict[str, Any] | None, asof_date: Any) -> dict[str, Any]:
    if not isinstance(row, dict) or not row:
        return _absent("missing")
    status = str(row.get("allocation_status") or "").strip().lower()
    conviction = CONVICTION_BUCKET_STRENGTH.get(str(row.get("conviction_bucket") or "").strip().lower())
    discount = RISK_BUCKET_DISCOUNT.get(str(row.get("risk_bucket") or "").strip().lower())
    strength = None
    if conviction is not None and discount is not None:
        strength = round(conviction * discount, 4)
    blocked = status in {"rejected", "abstained"}
    return {
        "present": not blocked,
        "fresh": _same_day(row.get("asof_date"), asof_date),
        "strength": None if blocked else strength,
        "status": status or "unknown",
        "risk_bucket": row.get("risk_bucket"),
        "conviction_bucket": row.get("conviction_bucket"),
        "stop_price": _num(row.get("stop_price")),
        "invalidation_price": _num(row.get("invalidation_price")),
    }


def market_context_dimension(row: dict[str, Any] | None, asof_date: Any) -> dict[str, Any]:
    if not isinstance(row, dict) or not row:
        return _absent("missing")
    risk_on = _num(row.get("risk_on_score"))
    return {
        "present": True,
        "fresh": _same_day(row.get("asof_date"), asof_date),
        "strength": max(0.0, min(1.0, risk_on)) if risk_on is not None else None,
        "status": "ok",
        "classification": row.get("macro_risk_state"),
        "regime_name": row.get("regime_name"),
        "macro_stress_score": _num(row.get("macro_stress_score")),
    }


def _reliability_dimension(row: dict[str, Any] | None) -> dict[str, Any]:
    """Shared shape for sector / exact-class reliability from an eval-summary row."""
    if not isinstance(row, dict) or not row:
        return _absent("missing")
    classification = str(row.get("classification") or "").strip()
    helpful = classification in HELPFUL_RELIABILITY_CLASSIFICATIONS
    hit_rate = _num(row.get("excess_opportunity_hit_rate_after_cost"))
    strength = (max(0.0, min(1.0, hit_rate)) if hit_rate is not None else None) if helpful else 0.0
    return {
        "present": True,
        "fresh": True,  # reliability summaries are as-of their latest evaluation, used point-in-time by the loader
        "strength": strength,
        "status": "ok",
        "classification": classification or None,
        "matured_count": _num(row.get("matured_count")),
        "excess_hit_rate_after_cost": hit_rate,
    }


def sector_reliability_dimension(row: dict[str, Any] | None) -> dict[str, Any]:
    return _reliability_dimension(row)


def exact_class_reliability_dimension(row: dict[str, Any] | None) -> dict[str, Any]:
    return _reliability_dimension(row)


def benchmark_excess_dimension(row: dict[str, Any] | None) -> dict[str, Any]:
    """Feeds the beta guard: present + classification + excess_positive (sign of after-cost excess)."""
    if not isinstance(row, dict) or not row:
        return {"present": False, "fresh": False, "strength": None, "status": "missing",
                "classification": None, "excess_positive": None}
    avg_excess = _num(row.get("avg_excess_watch_return_after_cost"))
    excess_positive = None if avg_excess is None else (avg_excess > 0.0)
    return {
        "present": True,
        "fresh": True,
        "status": "ok",
        "classification": (str(row.get("classification")).strip() or None) if row.get("classification") else None,
        "excess_positive": excess_positive,
        "avg_excess_after_cost": avg_excess,
    }


def event_provenance_dimension(row: dict[str, Any] | None, asof_date: Any) -> dict[str, Any]:
    if not isinstance(row, dict) or not row:
        return _absent("missing")
    event_class = row.get("event_class")
    if event_class is None or str(event_class).strip() == "":
        return {"present": False, "fresh": False, "strength": None, "status": "no_event_class"}
    confidence = _num(row.get("confidence"))
    return {
        "present": True,
        "fresh": _same_day(row.get("asof_date"), asof_date),
        "strength": max(0.0, min(1.0, confidence)) if confidence is not None else None,
        "status": "ok",
        "event_class": str(event_class).strip(),
        "setup_effect": row.get("setup_effect"),
        "score_impact": _num(row.get("score_impact")),
    }


def hypothesis_match_dimension(
    rows: list[dict[str, Any]] | None,
    *,
    min_score: float = HYPOTHESIS_CONDITIONS_MIN_SCORE,
) -> dict[str, Any]:
    """Best validated/production hypothesis match -> the dict the .1 contract consumes.

    conditions_met is the match's explicit flag (decision_json) if present, else match_score >=
    min_score. Only validated/production hypotheses can confer authority (.1 enforces this too).
    """
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
    """Pure assembly: row dicts -> the packet consumed by `advisory/llm_decision_contract`.

    `risk` and `event_provenance` both read the allocation row by default (event_row falls back to
    allocation_row); pass a distinct event_row to source provenance elsewhere.
    """
    event_source = event_row if event_row is not None else allocation_row
    packet: dict[str, Any] = {
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
    return packet


# --------------------------------------------------------------------------------------------------
# Thin defensive DB loaders. Any failure/absence yields a present=False dimension (never a crash).
# Exact strength semantics are validated end-to-end when .4b/.4c run; loaders are v1.
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
    """Load the point-in-time packet for symbol+asof_date from existing feature tables.

    Loaders are injectable so the wiring is testable without a DB. Reliability/benchmark rows are
    keyed off the event's context_class (from the allocation row).
    """
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
        # Sector/family reliability: same table at the source_context (family) grain when available.
        family = (exact_class_row or {}).get("source_context") if isinstance(exact_class_row, dict) else None
        if family:
            sector_row = row_loader(
                "SELECT * FROM advisory_context_watch_eval_summary WHERE source_context=%(family)s "
                "AND evaluated_at<=%(asof)s ORDER BY evaluated_at DESC, matured_count DESC LIMIT 1",
                {"asof": params["asof"], "family": str(family).strip()},
            )
        benchmark_row = exact_class_row  # carries classification + avg_excess_watch_return_after_cost

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
