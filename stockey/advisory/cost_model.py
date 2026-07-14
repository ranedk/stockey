"""Name-specific round-trip trading cost -- retires the flat 25bps fiction (spec discovery_engine.md 8.4).

The backtests scored every edge at a flat MOMENTUM_BACKTEST_COST_BPS=25. For Indian mid/small-caps
that is ~statutory-only: it ignores the bid-ask spread (wide on thin names) and market impact (grows
with position size relative to a name's turnover). The picks the coverage lanes surface have median
avg-daily turnover ~41cr and a 25th-percentile ~19cr; on those the true round-trip cost is materially
above 25bps, and the measured ~2-3%/10d edge is thin enough that this decides whether it survives.

The model is a research prior, NOT a calibrated microstructure model (Confidence: verify-first). Three
additive round-trip components, all as a fraction of notional:

  statutory  -- fixed regulatory + brokerage floor (STT/exchange/SEBI/stamp/GST), ~flat in bps.
  spread     -- crossing the bid-ask; inversely related to turnover (thin name => wide spread).
  impact     -- square-root market-impact law on participation = position_size / avg_daily_turnover.

Every coefficient is env-tunable so calibration is a knob, not a code change. Vectorized: accepts
scalars or numpy/pandas arrays so the backtest can price every pick cheaply.
"""
from __future__ import annotations

import os

import numpy as np

# Fixed regulatory + brokerage floor, round trip. STT 0.1% x2 delivery dominates; plus exchange txn,
# SEBI, stamp (buy), GST on brokerage. ~28bps is a conservative all-in for discount-broker delivery.
STATUTORY_BPS = float(os.getenv("COST_STATUTORY_BPS", "28"))

# Spread: full round-trip spread cost ~ COST_SPREAD_COEF / sqrt(turnover_in_crore), floored/capped.
# Calibrated so ~41cr -> ~15bps, ~19cr -> ~22bps, large-cap -> floor, illiquid -> cap.
SPREAD_COEF = float(os.getenv("COST_SPREAD_COEF", "96"))
SPREAD_FLOOR_BPS = float(os.getenv("COST_SPREAD_FLOOR_BPS", "6"))
SPREAD_CAP_BPS = float(os.getenv("COST_SPREAD_CAP_BPS", "80"))

# Impact: square-root law. impact_bps(round trip) = COST_IMPACT_COEF * sqrt(participation) * 10000,
# participation = position_size / avg_daily_turnover. Coef is small so a personal-size position in a
# liquid name is ~negligible and only grows as size approaches a thin name's daily turnover.
IMPACT_COEF = float(os.getenv("COST_IMPACT_COEF", "0.0128"))
IMPACT_CAP_BPS = float(os.getenv("COST_IMPACT_CAP_BPS", "150"))

# The position size the backtest prices impact for (INR). Personal-account default.
POSITION_SIZE_INR = float(os.getenv("BACKTEST_POSITION_SIZE_INR", "100000"))


def round_trip_cost_fraction(
    avg_turnover_inr,
    *,
    position_size_inr: float | None = None,
):
    """Round-trip cost as a fraction of notional (e.g. 0.0053 = 53bps), name-specific.

    ``avg_turnover_inr`` is the name's average daily traded value in INR (avgvol20 * close). Accepts a
    scalar or a numpy/pandas array; returns the same shape. Missing/non-positive turnover falls back to
    the spread cap (treat an untradeable/unknown name as maximally expensive, never cheap).
    """
    size = POSITION_SIZE_INR if position_size_inr is None else float(position_size_inr)
    turnover = np.asarray(avg_turnover_inr, dtype="float64")
    scalar = turnover.ndim == 0
    turnover = np.atleast_1d(turnover)

    valid = np.isfinite(turnover) & (turnover > 0)
    turnover_cr = np.where(valid, turnover / 1e7, np.nan)

    spread_bps = np.where(
        valid,
        np.clip(SPREAD_COEF / np.sqrt(np.where(valid, turnover_cr, 1.0)), SPREAD_FLOOR_BPS, SPREAD_CAP_BPS),
        SPREAD_CAP_BPS,  # unknown turnover -> most expensive, never cheapest
    )

    safe_turnover = np.where(valid, turnover, 1.0)  # avoid divide-by-zero on invalid rows (masked out next)
    participation = np.where(valid, size / safe_turnover, np.nan)
    impact_bps = np.where(
        valid,
        np.minimum(IMPACT_COEF * np.sqrt(np.where(valid, participation, 0.0)) * 10000.0, IMPACT_CAP_BPS),
        IMPACT_CAP_BPS,
    )

    total_bps = STATUTORY_BPS + spread_bps + impact_bps
    result = total_bps / 10000.0
    return float(result[0]) if scalar else result


def describe() -> dict[str, float]:
    """The active coefficients -- for provenance in reports."""
    return {
        "statutory_bps": STATUTORY_BPS,
        "spread_coef": SPREAD_COEF,
        "spread_floor_bps": SPREAD_FLOOR_BPS,
        "spread_cap_bps": SPREAD_CAP_BPS,
        "impact_coef": IMPACT_COEF,
        "impact_cap_bps": IMPACT_CAP_BPS,
        "position_size_inr": POSITION_SIZE_INR,
    }
