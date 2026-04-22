from __future__ import annotations

from typing import Any

import pandas as pd


PRE_ENTRY_STATES = {
    "REJECT",
    "IGNORE",
    "WATCHLIST",
    "NEAR_PIVOT",
    "READY",
    "BUY_TRIGGERED",
}

POST_ENTRY_STATES = {
    "HOLD",
    "ADD_ON_PULLBACK",
    "PARTIAL_EXIT",
    "FULL_EXIT",
    "EMERGENCY_EXIT",
}

DEFAULT_FILTERS = {
    "min_avg_traded_value_20d": 10_000_000.0,
    "min_price": 20.0,
    "min_median_volume_20d": 100_000.0,
    "max_atr_pct": 8.0,
    "max_gap_frequency_60d": 0.15,
    "max_base_depth_60d_pct": 35.0,
}

DEFAULT_THRESHOLDS = {
    "trend_min": 15.0,
    "structure_min": 18.0,
    "participation_min": 10.0,
    "relative_strength_min": 8.0,
    "tradability_min": 6.0,
    "ready_total_min": 70.0,
    "buy_total_min": 78.0,
    "watch_total_min": 55.0,
    "near_pivot_distance_pct": 3.0,
    "breakout_volume_min": 1.8,
    "retest_distance_pct": 2.5,
    "extension_partial_exit_pct": 12.0,
    "trend_add_on_pullback_distance_pct": 3.0,
}


def safe_float(value: Any) -> float | None:
    out = pd.to_numeric(value, errors="coerce")
    if pd.isna(out):
        return None
    return float(out)


def _clip_score(value: float, maximum: float) -> float:
    return round(max(0.0, min(maximum, value)), 4)


def _bool(value: Any) -> bool:
    if value is None or pd.isna(value):
        return False
    return bool(value)


def evaluate_hard_filters(row: pd.Series, config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = {**DEFAULT_FILTERS, **(config or {})}
    failures: list[str] = []
    avg_traded_value = safe_float(row.get("avg_traded_value_20d"))
    price = safe_float(row.get("adj_close"))
    median_volume = safe_float(row.get("median_volume_20d"))
    atr_pct = safe_float(row.get("atr_pct"))
    gap_frequency = safe_float(row.get("gap_frequency_60d"))
    base_depth = safe_float(row.get("base_depth_60d_pct"))

    if avg_traded_value is None or avg_traded_value < float(cfg["min_avg_traded_value_20d"]):
        failures.append("low_liquidity")
    if price is None or price < float(cfg["min_price"]):
        failures.append("low_price")
    if median_volume is None or median_volume < float(cfg["min_median_volume_20d"]):
        failures.append("low_median_volume")
    if atr_pct is not None and atr_pct > float(cfg["max_atr_pct"]):
        failures.append("high_noise_atr")
    if gap_frequency is not None and gap_frequency > float(cfg["max_gap_frequency_60d"]):
        failures.append("excessive_gap_frequency")
    if base_depth is not None and base_depth > float(cfg["max_base_depth_60d_pct"]):
        failures.append("base_too_deep")
    if not _bool(row.get("pass_liquidity_20d")):
        failures.append("liquidity_filter_failed")
    if "pass_gap_behavior" in row and not _bool(row.get("pass_gap_behavior")):
        failures.append("gap_behavior_filter_failed")

    return {
        "passed": not failures,
        "reasons": sorted(set(failures)),
    }


def score_trend_regime(row: pd.Series) -> float:
    score = 0.0
    if _bool(row.get("pass_above_dma_50")):
        score += 6.0
    if _bool(row.get("pass_above_dma_150")):
        score += 4.0
    if _bool(row.get("pass_above_dma_200")):
        score += 2.0
    if _bool(row.get("pass_trend_alignment")):
        score += 4.0
    if (safe_float(row.get("dma_50_slope_20d_pct")) or -999.0) > 0:
        score += 3.0
    if (safe_float(row.get("dma_150_slope_20d_pct")) or -999.0) > 0:
        score += 2.0
    if (safe_float(row.get("dist_52w_high")) or -100.0) >= -10.0:
        score += 2.0
    persistence_60 = safe_float(row.get("trend_persistence_60d")) or 0.0
    persistence_120 = safe_float(row.get("trend_persistence_120d")) or 0.0
    score += min(2.0, persistence_60 * 2.0)
    score += min(2.0, persistence_120 * 2.0)
    hh = safe_float(row.get("higher_high_count_20d")) or 0.0
    hl = safe_float(row.get("higher_low_count_20d")) or 0.0
    if hh >= 10 and hl >= 10:
        score += 2.0
    return _clip_score(score, 25.0)


def score_structure_quality(row: pd.Series) -> float:
    score = 0.0
    base_depth_60 = safe_float(row.get("base_depth_60d_pct"))
    pivot_distance = abs(safe_float(row.get("pivot_distance_20d_pct")) or 999.0)
    range_ratio = safe_float(row.get("range_contraction_ratio"))
    upper_half_20 = safe_float(row.get("tight_close_upper_half_20d")) or 0.0
    support_hold = safe_float(row.get("support_hold_rate_20d")) or 0.0
    extension = safe_float(row.get("breakout_extension_pct"))

    if base_depth_60 is not None and 8.0 <= base_depth_60 <= 30.0:
        score += 8.0
    elif base_depth_60 is not None and base_depth_60 <= 35.0:
        score += 4.0
    if pivot_distance <= 6.0:
        score += 6.0
    if range_ratio is not None and range_ratio < 0.9:
        score += 5.0
    if _bool(row.get("volatility_contraction_flag")):
        score += 4.0
    if upper_half_20 >= 0.58:
        score += 3.0
    if support_hold >= 0.45:
        score += 2.0
    if (safe_float(row.get("bb_width_rank_252d")) or 1.0) <= 0.4:
        score += 2.0
    if extension is not None and extension <= 8.0:
        score += 2.0
    return _clip_score(score, 30.0)


def score_participation(row: pd.Series) -> float:
    score = 0.0
    breakout_vol = safe_float(row.get("breakout_day_volume_vs_20d")) or 0.0
    up_down_vol = safe_float(row.get("up_down_volume_ratio_20d")) or 0.0
    accumulation = safe_float(row.get("accumulation_days_20d")) or 0.0
    distribution = safe_float(row.get("distribution_days_20d")) or 0.0
    dryup = safe_float(row.get("pullback_volume_dryup_ratio_20d"))

    if breakout_vol >= 2.0:
        score += 7.0
    elif breakout_vol >= 1.4:
        score += 4.0
    if up_down_vol >= 1.2:
        score += 4.0
    elif up_down_vol >= 1.0:
        score += 2.0
    if accumulation >= distribution + 1:
        score += 4.0
    elif accumulation >= distribution:
        score += 2.0
    if dryup is not None and dryup <= 0.75:
        score += 3.0
    distribution_penalty = min(4.0, max(0.0, distribution - accumulation))
    return _clip_score(score - distribution_penalty, 20.0)


def score_relative_strength(row: pd.Series) -> float:
    score = 0.0
    rs_benchmark = safe_float(row.get("rs_vs_benchmark")) or 0.0
    rs_sector = safe_float(row.get("rs_vs_sector")) or 0.0
    ret_60 = safe_float(row.get("stock_ret_60d")) or 0.0
    ret_120 = safe_float(row.get("stock_ret_120d")) or 0.0
    dist_52w = safe_float(row.get("dist_52w_high")) or -100.0

    if rs_benchmark >= 0.08:
        score += 5.0
    elif rs_benchmark >= 0.03:
        score += 3.0
    if rs_sector >= 0.05:
        score += 4.0
    elif rs_sector >= 0.0:
        score += 2.0
    if ret_60 > 0:
        score += 2.0
    if ret_120 > 0:
        score += 2.0
    if dist_52w >= -8.0:
        score += 2.0
    return _clip_score(score, 15.0)


def score_tradability(row: pd.Series) -> float:
    score = 0.0
    avg_value = safe_float(row.get("avg_traded_value_20d")) or 0.0
    atr_pct = safe_float(row.get("atr_pct")) or 99.0
    gap_frequency = safe_float(row.get("gap_frequency_60d")) or 1.0
    support_distance = safe_float(row.get("support_distance_20d_pct")) or 99.0

    if avg_value >= 50_000_000.0:
        score += 4.0
    elif avg_value >= 20_000_000.0:
        score += 3.0
    elif avg_value >= 10_000_000.0:
        score += 2.0
    if atr_pct <= 4.5:
        score += 2.0
    elif atr_pct <= 6.5:
        score += 1.0
    if gap_frequency <= 0.08:
        score += 2.0
    elif gap_frequency <= 0.15:
        score += 1.0
    if support_distance <= 8.0:
        score += 2.0
    elif support_distance <= 12.0:
        score += 1.0
    return _clip_score(score, 10.0)


def identify_entry_trigger(row: pd.Series, thresholds: dict[str, Any] | None = None) -> tuple[str | None, str | None]:
    cfg = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    breakout_vol = safe_float(row.get("breakout_day_volume_vs_20d")) or 0.0
    pivot_distance = safe_float(row.get("pivot_distance_20d_pct"))
    support_distance = safe_float(row.get("support_distance_20d_pct")) or 99.0
    support_hold = safe_float(row.get("support_hold_rate_20d")) or 0.0
    dryup = safe_float(row.get("pullback_volume_dryup_ratio_20d")) or 9.0
    close_location = safe_float(row.get("close_location_pct")) or 0.0
    breakout_extension = safe_float(row.get("breakout_extension_pct")) or 99.0

    if breakout_vol >= float(cfg["breakout_volume_min"]) and (pivot_distance is not None and pivot_distance <= 0.5) and close_location >= 0.7:
        return "breakout", "Price is pushing through pivot with strong breakout participation."
    if pivot_distance is not None and abs(pivot_distance) <= float(cfg["retest_distance_pct"]) and support_hold >= 0.45 and dryup <= 0.8:
        return "breakout_retest", "Price is holding near pivot on controlled retest volume."
    if _bool(row.get("pass_trend_alignment")) and support_distance <= float(cfg["trend_add_on_pullback_distance_pct"]) and dryup <= 0.85 and close_location >= 0.55:
        return "trend_pullback", "Trend pullback is holding support with controlled selling."
    if _bool(row.get("pass_above_dma_20")) and close_location >= 0.75 and breakout_extension <= 4.0 and (safe_float(row.get("dist_20d_high")) or -99.0) >= -1.0:
        return "reclaim", "Price has reclaimed a key level and closed strongly."
    return None, None


def score_row(row: pd.Series, *, thresholds: dict[str, Any] | None = None) -> dict[str, Any]:
    filter_result = evaluate_hard_filters(row, config=thresholds)
    trend_score = score_trend_regime(row)
    structure_score = score_structure_quality(row)
    participation_score = score_participation(row)
    relative_strength_score = score_relative_strength(row)
    tradability_score = score_tradability(row)
    total_score = round(
        trend_score + structure_score + participation_score + relative_strength_score + tradability_score,
        4,
    )
    entry_type, trigger_note = identify_entry_trigger(row, thresholds=thresholds)
    return {
        "hard_filter_pass": filter_result["passed"],
        "hard_filter_reasons": filter_result["reasons"],
        "trend_score": trend_score,
        "structure_score": structure_score,
        "participation_score": participation_score,
        "relative_strength_score": relative_strength_score,
        "tradability_score": tradability_score,
        "technical_total_score": total_score,
        "entry_trigger_type": entry_type,
        "entry_trigger_note": trigger_note,
    }


def evaluate_pre_entry_state(row: pd.Series, *, thresholds: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    score = score_row(row, thresholds=thresholds)
    reasons: list[str] = []

    if not score["hard_filter_pass"]:
        state = "REJECT"
        reasons.extend(score["hard_filter_reasons"])
    else:
        if score["trend_score"] < float(cfg["trend_min"]):
            reasons.append("trend_below_minimum")
        if score["structure_score"] < float(cfg["structure_min"]):
            reasons.append("structure_below_minimum")
        if score["participation_score"] < float(cfg["participation_min"]):
            reasons.append("participation_below_minimum")
        if score["relative_strength_score"] < float(cfg["relative_strength_min"]):
            reasons.append("relative_strength_below_minimum")
        if score["tradability_score"] < float(cfg["tradability_min"]):
            reasons.append("tradability_below_minimum")

        total = score["technical_total_score"]
        pivot_distance = abs(safe_float(row.get("pivot_distance_20d_pct")) or 999.0)
        if reasons and total < float(cfg["watch_total_min"]):
            state = "IGNORE"
        elif total >= float(cfg["buy_total_min"]) and score["entry_trigger_type"] is not None and not reasons:
            state = "BUY_TRIGGERED"
        elif total >= float(cfg["ready_total_min"]) and not reasons:
            state = "NEAR_PIVOT" if pivot_distance <= float(cfg["near_pivot_distance_pct"]) else "READY"
        elif total >= float(cfg["watch_total_min"]):
            state = "WATCHLIST" if pivot_distance > float(cfg["near_pivot_distance_pct"]) else "NEAR_PIVOT"
        else:
            state = "IGNORE"

    return {
        **score,
        "technical_state": state,
        "technical_reasons": reasons,
        "conviction_bucket": (
            "HIGH_CONVICTION"
            if score["technical_total_score"] >= 85.0
            else "MEDIUM_CONVICTION"
            if score["technical_total_score"] >= 76.0
            else "LOW_CONVICTION"
            if score["technical_total_score"] >= 70.0
            else None
        ),
    }


def evaluate_post_entry_state(row: pd.Series, *, thresholds: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    score = score_row(row, thresholds=thresholds)
    reasons: list[str] = []
    breakout_extension = safe_float(row.get("breakout_extension_pct")) or 0.0
    distribution = safe_float(row.get("distribution_days_20d")) or 0.0
    rs_sector = safe_float(row.get("rs_vs_sector")) or 0.0
    trend_alignment = _bool(row.get("pass_trend_alignment"))
    above_dma_20 = _bool(row.get("pass_above_dma_20"))
    above_dma_50 = _bool(row.get("pass_above_dma_50"))
    support_distance = safe_float(row.get("support_distance_20d_pct")) or 99.0
    dryup = safe_float(row.get("pullback_volume_dryup_ratio_20d")) or 9.0
    gap_pct = safe_float(row.get("gap_pct")) or 0.0

    if not score["hard_filter_pass"] and "excessive_gap_frequency" in score["hard_filter_reasons"]:
        reasons.append("abnormal_gap_risk")
        state = "EMERGENCY_EXIT"
    elif gap_pct >= 8.0 and not above_dma_20:
        reasons.append("severe_gap_break")
        state = "EMERGENCY_EXIT"
    elif not above_dma_50 or (distribution >= 5 and rs_sector < 0):
        reasons.append("technical_thesis_failure")
        state = "FULL_EXIT"
    elif breakout_extension >= float(cfg["extension_partial_exit_pct"]) or distribution >= 4:
        reasons.append("extension_or_distribution")
        state = "PARTIAL_EXIT"
    elif trend_alignment and support_distance <= float(cfg["trend_add_on_pullback_distance_pct"]) and dryup <= 0.85 and above_dma_20:
        reasons.append("constructive_pullback")
        state = "ADD_ON_PULLBACK"
    else:
        state = "HOLD"
        if score["technical_total_score"] < float(cfg["ready_total_min"]):
            reasons.append("trend_intact_but_quality_softened")

    return {
        **score,
        "technical_state": state,
        "technical_reasons": reasons,
    }
