"""Point-in-time price/factor signals for the multi-factor confidence model (Phase 1).

The operator's confidence-score idea -- more factors agreeing -> higher confidence -- is sound ONLY
across INDEPENDENT factors. A correlation check on real data showed Momentum/Trend ~0.83 (one signal,
not two) and some factors anti-correlated, so a naive count double-counts the trend family and
inflates confidence. This module therefore: (a) computes each factor point-in-time, (b) tags it with
an independent COMPONENT, and (c) ships validation helpers so each factor must earn its weight from
forward benchmark-excess before it is trusted (same discipline as the event-hypothesis promotion
audit). Phase 2 combines validated, de-correlated components into a confidence score and validates
the combination. See docs/price_factor_model.md.

All factors are ORIENTED so higher = more bullish, and read from advisory_technical_daily (+
advisory_fundamentals_daily). Pure: factor functions take row dicts and return float | None.
"""

from __future__ import annotations

import math
from typing import Any, Callable

# Independent component each factor belongs to (used to avoid double-counting correlated factors).
COMPONENTS = ("trend", "reversion", "volatility", "volume", "liquidity", "fundamental")


def _n(row: dict[str, Any], key: str) -> float | None:
    value = row.get(key)
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _b(row: dict[str, Any], key: str) -> float | None:
    value = row.get(key)
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if value in (1, "1", "t", "true", "True"):
        return 1.0
    if value in (0, "0", "f", "false", "False"):
        return 0.0
    return None


def _momentum_acceleration(r: dict[str, Any]) -> float | None:
    ret60, ret120 = _n(r, "stock_ret_60d"), _n(r, "stock_ret_120d")
    return (2.0 * ret60 - ret120) if (ret60 is not None and ret120 is not None) else None


def _trend_following(r: dict[str, Any]) -> float | None:
    parts = [_b(r, "pass_above_dma_50"), _b(r, "pass_above_dma_200")]
    slope = _n(r, "dma_50_slope_20d_pct")
    if slope is not None:
        parts.append(1.0 if slope > 0 else 0.0)
    known = [p for p in parts if p is not None]
    return sum(known) / len(known) if known else None


def _multi_timeframe_alignment(r: dict[str, Any]) -> float | None:
    tps = [_n(r, "trend_persistence_20d"), _n(r, "trend_persistence_60d"), _n(r, "trend_persistence_120d")]
    known = [1.0 if t > 0 else 0.0 for t in tps if t is not None]
    return sum(known) / len(known) if known else None


def _mean_reversion(r: dict[str, Any]) -> float | None:
    # Oversold pullback within an intact uptrend: deeper pullback below the 20d high (more negative
    # dist) gated by being above the 200DMA. Higher = more oversold-but-structurally-sound.
    pull = _n(r, "dist_20d_high")
    if pull is None:
        return None
    uptrend = _b(r, "pass_above_dma_200")
    return (-pull) * (uptrend if uptrend is not None else 1.0)


def _volume_confirmation(r: dict[str, Any]) -> float | None:
    acc, dist = _n(r, "accumulation_days_20d"), _n(r, "distribution_days_20d")
    if acc is None and dist is None:
        return None
    return (acc or 0.0) - (dist or 0.0)


def _neg(key: str) -> Callable[[dict[str, Any]], float | None]:
    def fn(r: dict[str, Any]) -> float | None:
        value = _n(r, key)
        return -value if value is not None else None
    return fn


# name -> (component, compute_fn). All oriented so higher = more bullish.
FACTORS: dict[str, tuple[str, Callable[[dict[str, Any]], float | None]]] = {
    # Trend / momentum family (highly correlated -- collapses to ~1-2 independent signals).
    "momentum": ("trend", lambda r: _n(r, "stock_ret_60d")),
    "momentum_acceleration": ("trend", _momentum_acceleration),
    "trend_following": ("trend", _trend_following),
    "trend_quality": ("trend", lambda r: _n(r, "trend_persistence_60d")),
    "relative_strength": ("trend", lambda r: _n(r, "rs_vs_benchmark")),
    "distance_from_52w_high": ("trend", lambda r: _n(r, "dist_52w_high")),  # less negative = nearer high
    "breakout": ("trend", lambda r: _n(r, "breakout_extension_pct")),
    "multi_timeframe_alignment": ("trend", _multi_timeframe_alignment),
    "price_efficiency": ("trend", lambda r: _n(r, "trend_persistence_120d")),
    # Mean reversion (counter-trend; regime-dependent, NOT additive with trend).
    "mean_reversion": ("reversion", _mean_reversion),
    "drawdown_recovery": ("reversion", lambda r: _n(r, "close_location_pct")),
    # Volatility regime (compression vs expansion are inverses).
    "volatility": ("volatility", _neg("atr_pct")),                 # lower vol oriented bullish
    "volatility_compression": ("volatility", lambda r: (1.0 - v) if (v := _n(r, "bb_width_rank_252d")) is not None else None),
    "volatility_expansion": ("volatility", lambda r: _n(r, "bb_width_rank_252d")),
    # Volume confirmation overlay.
    "volume_confirmation": ("volume", _volume_confirmation),
    "relative_volume": ("volume", lambda r: _n(r, "breakout_day_volume_vs_20d")),
    "up_down_volume_ratio": ("volume", lambda r: _n(r, "up_down_volume_ratio_20d")),
    # Tradability gate.
    "liquidity": ("liquidity", lambda r: _n(r, "avg_traded_value_20d")),
    # Fundamental (independent of technical): growth / leverage / cash.
    "earnings_growth": ("fundamental", lambda r: _n(r, "profit_after_tax_qoq_growth")),
    "revenue_growth": ("fundamental", lambda r: _n(r, "total_revenue_qoq_growth")),
    "leverage": ("fundamental", _neg("net_debt_to_equity")),       # lower debt oriented bullish
    "cash_generation": ("fundamental", lambda r: _n(r, "free_cash_flow_to_equity")),
}

# Beta / market sensitivity is intentionally NOT here -- it needs a return-vs-benchmark regression
# over a window, not a single row (Phase 1b).


def compute_factors(technical_row: dict[str, Any] | None, fundamental_row: dict[str, Any] | None = None) -> dict[str, float | None]:
    """All factor values for one (symbol, as-of date) from its point-in-time rows (oriented bullish)."""
    merged: dict[str, Any] = {}
    if isinstance(technical_row, dict):
        merged.update(technical_row)
    if isinstance(fundamental_row, dict):
        merged.update(fundamental_row)
    if not merged:
        return {name: None for name in FACTORS}
    return {name: fn(merged) for name, (_component, fn) in FACTORS.items()}


def factors_by_component() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {component: [] for component in COMPONENTS}
    for name, (component, _fn) in FACTORS.items():
        out.setdefault(component, []).append(name)
    return out


# --------------------------------------------------------------------------------------------------
# Validation helpers (pure): each factor must earn forward-excess edge before it is trusted.
# --------------------------------------------------------------------------------------------------

def factor_ic_report(frame: Any, *, forward_col: str = "forward_excess", quantiles: int = 5) -> Any:
    """Per-factor information coefficient (Spearman rank corr vs forward excess) + top-bottom spread.

    `frame` is a pandas DataFrame with one column per factor plus `forward_col`. Returns a DataFrame
    indexed by factor with: n, spearman_ic, top_minus_bottom (mean forward excess of the most-bullish
    quantile minus the least), and the factor's component.
    """
    import pandas as pd

    rows = []
    target = pd.to_numeric(frame[forward_col], errors="coerce") if forward_col in getattr(frame, "columns", []) else None
    for name, (component, _fn) in FACTORS.items():
        if target is None or name not in frame.columns:
            rows.append({"factor": name, "component": component, "n": 0, "spearman_ic": None, "top_minus_bottom": None})
            continue
        values = pd.to_numeric(frame[name], errors="coerce")
        mask = values.notna() & target.notna()
        n = int(mask.sum())
        ic = None
        if n >= 50 and values[mask].nunique() > 1:
            ic = values[mask].corr(target[mask], method="spearman")
        spread = None
        if n >= 50 and values[mask].nunique() >= quantiles:
            try:
                buckets = pd.qcut(values[mask].rank(method="first"), quantiles, labels=False)
                means = target[mask].groupby(buckets).mean()
                spread = float(means.iloc[-1] - means.iloc[0])
            except (ValueError, IndexError):
                spread = None
        rows.append({"factor": name, "component": component, "n": n,
                     "spearman_ic": round(float(ic), 4) if (ic is not None and pd.notna(ic)) else None,
                     "top_minus_bottom": round(spread, 4) if spread is not None else None})
    return pd.DataFrame(rows).set_index("factor")


def factor_correlation(frame: Any) -> Any:
    """Spearman correlation matrix among factor columns present in `frame` (the independence map)."""
    import pandas as pd

    present = [name for name in FACTORS if name in frame.columns]
    return frame[present].apply(pd.to_numeric, errors="coerce").corr(method="spearman")
