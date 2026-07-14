"""Score-scale registry -- the single source of truth for which advisory score fields are 0-1 vs 0-100.

Born from a silent bug: `technical_score`/`setup_score` are **0-1** (rule_engine normalizes the engine's
0-100 `technical_total_score` /100 at `advisory/rule_engine.py:1217`), but several consumers compared them
against **0-100** thresholds (the 78 buy bar) -- permanently dead branches, `conviction_score` persisted
~0 vs its 0-100 schema. Unit tests missed it because their fixtures encoded the same wrong scale.

This module declares each field's expected range so (a) consumers scale correctly via `to_100`, and
(b) `scripts/funnel_invariants.py` can flag out-of-range values AND impossible gates (a threshold the
field's range can never cross). Keep this list in sync with the engine caps in
`advisory/technical_engine.py` (`_clip_score`) and the normalization in `advisory/rule_engine.py`.
"""
from __future__ import annotations

# field name -> (min, max) inclusive expected range. Sub-score maxima are the engine _clip_score caps
# (slightly generous where an archetype variant caps lower -- generous bounds still catch 100x scale
# errors, which is the class we hunt).
SCORE_RANGES: dict[str, tuple[float, float]] = {
    # 0-100 point-sum scores (engine scale)
    "technical_total_score": (0.0, 100.0),
    "technical_trend_score": (0.0, 25.0),
    "technical_structure_score": (0.0, 30.0),
    "technical_participation_score": (0.0, 30.0),
    "technical_relative_strength_score": (0.0, 20.0),
    "technical_tradability_score": (0.0, 15.0),
    "conviction_score": (0.0, 100.0),
    "rs_percentile": (0.0, 100.0),
    # 0-1 normalized fractions (rule_engine scale)
    "technical_score": (0.0, 1.0),
    "setup_score": (0.0, 1.0),
    "fundamental_score": (0.0, 1.0),
    "regime_fit_score": (0.0, 1.0),
    "event_score": (0.0, 1.0),
}

# The technical_total_score buy bar (0-100), from advisory/technical_engine.py DEFAULT_THRESHOLDS.
BUY_BAR = 78.0

# Fields that are on the 0-1 scale -- comparing any of these against a threshold > 1.0 is a scale bug
# (the impossible gate that hid). Derived from SCORE_RANGES so there is one source of truth.
UNIT_SCALE_FIELDS = frozenset(name for name, (lo, hi) in SCORE_RANGES.items() if hi <= 1.0)
HUNDRED_SCALE_FIELDS = frozenset(name for name, (lo, hi) in SCORE_RANGES.items() if hi > 1.0)


def is_unit_scale(field: str) -> bool:
    """True if the field is a 0-1 fraction (must be scaled before comparing to a 0-100 threshold)."""
    return field in UNIT_SCALE_FIELDS


def to_100(value_0_1: float) -> float:
    """Scale a 0-1 fraction to the 0-100 scale. Pass-through if already > 1.0 (defensive for callers
    that may receive either scale, e.g. a 0-1 candidate field or the engine's 0-100 total)."""
    v = float(value_0_1)
    return v * 100.0 if 0.0 <= v <= 1.0 else v


def in_range(field: str, value: float, *, tol: float = 1e-6) -> bool:
    """True if value is within the declared range for field (unknown field -> True, not our concern)."""
    bounds = SCORE_RANGES.get(field)
    if bounds is None:
        return True
    lo, hi = bounds
    return (lo - tol) <= float(value) <= (hi + tol)
