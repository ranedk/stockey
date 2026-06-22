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
    if "median_volume_20d" in row and median_volume is not None and median_volume < float(cfg["min_median_volume_20d"]):
        failures.append("low_median_volume")
    if atr_pct is not None and atr_pct > float(cfg["max_atr_pct"]):
        failures.append("high_noise_atr")
    if gap_frequency is not None and gap_frequency > float(cfg["max_gap_frequency_60d"]):
        failures.append("excessive_gap_frequency")
    if base_depth is not None and base_depth > float(cfg["max_base_depth_60d_pct"]):
        failures.append("base_too_deep")
    if "pass_liquidity_20d" in row and not _bool(row.get("pass_liquidity_20d")):
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
    if breakout_vol >= 1.0 and pivot_distance is not None and abs(pivot_distance) <= float(cfg["retest_distance_pct"]) and support_hold >= 0.45 and dryup <= 0.8:
        return "breakout_retest", "Price is holding near pivot on controlled retest volume."
    if _bool(row.get("pass_trend_alignment")) and support_distance <= float(cfg["trend_add_on_pullback_distance_pct"]) and dryup <= 0.85 and close_location >= 0.55:
        return "trend_pullback", "Trend pullback is holding support with controlled selling."
    if breakout_vol >= 1.0 and _bool(row.get("pass_above_dma_20")) and close_location >= 0.75 and breakout_extension <= 4.0 and (safe_float(row.get("dist_20d_high")) or -99.0) >= -1.0:
        return "reclaim", "Price has reclaimed a key level and closed strongly."
    return None, None


def _trigger_blocker(
    *,
    code: str,
    archetype: str,
    metric: str,
    current: Any,
    operator: str,
    threshold: Any,
    description: str,
) -> dict[str, Any]:
    return {
        "code": code,
        "archetype": archetype,
        "metric": metric,
        "current": safe_float(current) if current is not None else None,
        "operator": operator,
        "threshold": safe_float(threshold) if threshold is not None else None,
        "description": description,
    }


def _bool_trigger_blocker(*, code: str, archetype: str, metric: str, current: Any, description: str) -> dict[str, Any]:
    return {
        "code": code,
        "archetype": archetype,
        "metric": metric,
        "current": bool(_bool(current)),
        "operator": "eq",
        "threshold": True,
        "description": description,
    }


def diagnose_entry_trigger_blockers(row: pd.Series, thresholds: dict[str, Any] | None = None) -> dict[str, Any]:
    """Mirror entry-trigger rules and explain why confirmation is still blocked."""
    cfg = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    trigger_type, trigger_note = identify_entry_trigger(row, thresholds=thresholds)
    breakout_vol = safe_float(row.get("breakout_day_volume_vs_20d")) or 0.0
    pivot_distance = safe_float(row.get("pivot_distance_20d_pct"))
    support_distance = safe_float(row.get("support_distance_20d_pct")) or 99.0
    support_hold = safe_float(row.get("support_hold_rate_20d")) or 0.0
    dryup = safe_float(row.get("pullback_volume_dryup_ratio_20d")) or 9.0
    close_location = safe_float(row.get("close_location_pct")) or 0.0
    breakout_extension = safe_float(row.get("breakout_extension_pct")) or 99.0
    dist_20d_high = safe_float(row.get("dist_20d_high")) or -99.0

    by_archetype: dict[str, list[dict[str, Any]]] = {
        "breakout": [],
        "breakout_retest": [],
        "trend_pullback": [],
        "reclaim": [],
    }

    if breakout_vol < float(cfg["breakout_volume_min"]):
        by_archetype["breakout"].append(
            _trigger_blocker(
                code="breakout_volume_below_min",
                archetype="breakout",
                metric="breakout_day_volume_vs_20d",
                current=breakout_vol,
                operator="gte",
                threshold=cfg["breakout_volume_min"],
                description="Breakout volume is below the configured confirmation threshold.",
            )
        )
    if pivot_distance is None or pivot_distance > 0.5:
        by_archetype["breakout"].append(
            _trigger_blocker(
                code="pivot_not_cleared",
                archetype="breakout",
                metric="pivot_distance_20d_pct",
                current=pivot_distance,
                operator="lte",
                threshold=0.5,
                description="Price has not cleared the pivot tightly enough for breakout confirmation.",
            )
        )
    if close_location < 0.7:
        by_archetype["breakout"].append(
            _trigger_blocker(
                code="close_not_strong_enough",
                archetype="breakout",
                metric="close_location_pct",
                current=close_location,
                operator="gte",
                threshold=0.7,
                description="Close location is not strong enough for breakout confirmation.",
            )
        )

    if breakout_vol < 1.0:
        by_archetype["breakout_retest"].append(
            _trigger_blocker(
                code="retest_volume_too_weak",
                archetype="breakout_retest",
                metric="breakout_day_volume_vs_20d",
                current=breakout_vol,
                operator="gte",
                threshold=1.0,
                description="Retest needs at least normal participation.",
            )
        )
    if pivot_distance is None or abs(pivot_distance) > float(cfg["retest_distance_pct"]):
        by_archetype["breakout_retest"].append(
            _trigger_blocker(
                code="not_near_retest_zone",
                archetype="breakout_retest",
                metric="pivot_distance_20d_pct",
                current=pivot_distance,
                operator="abs_lte",
                threshold=cfg["retest_distance_pct"],
                description="Price is not close enough to the pivot/retest zone.",
            )
        )
    if support_hold < 0.45:
        by_archetype["breakout_retest"].append(
            _trigger_blocker(
                code="support_hold_too_weak",
                archetype="breakout_retest",
                metric="support_hold_rate_20d",
                current=support_hold,
                operator="gte",
                threshold=0.45,
                description="Support has not been respected often enough.",
            )
        )
    if dryup > 0.8:
        by_archetype["breakout_retest"].append(
            _trigger_blocker(
                code="selling_volume_not_dry",
                archetype="breakout_retest",
                metric="pullback_volume_dryup_ratio_20d",
                current=dryup,
                operator="lte",
                threshold=0.8,
                description="Pullback/retest selling volume has not dried up enough.",
            )
        )

    if not _bool(row.get("pass_trend_alignment")):
        by_archetype["trend_pullback"].append(
            _bool_trigger_blocker(
                code="trend_alignment_missing",
                archetype="trend_pullback",
                metric="pass_trend_alignment",
                current=row.get("pass_trend_alignment"),
                description="Trend-pullback entries require aligned moving-average trend context.",
            )
        )
    if support_distance > float(cfg["trend_add_on_pullback_distance_pct"]):
        by_archetype["trend_pullback"].append(
            _trigger_blocker(
                code="too_far_from_support",
                archetype="trend_pullback",
                metric="support_distance_20d_pct",
                current=support_distance,
                operator="lte",
                threshold=cfg["trend_add_on_pullback_distance_pct"],
                description="Price is too far from support for a pullback entry.",
            )
        )
    if dryup > 0.85:
        by_archetype["trend_pullback"].append(
            _trigger_blocker(
                code="pullback_selling_volume_not_controlled",
                archetype="trend_pullback",
                metric="pullback_volume_dryup_ratio_20d",
                current=dryup,
                operator="lte",
                threshold=0.85,
                description="Pullback selling volume is not controlled enough.",
            )
        )
    if close_location < 0.55:
        by_archetype["trend_pullback"].append(
            _trigger_blocker(
                code="pullback_close_not_confirmed",
                archetype="trend_pullback",
                metric="close_location_pct",
                current=close_location,
                operator="gte",
                threshold=0.55,
                description="Close quality has not confirmed the pullback hold.",
            )
        )

    if breakout_vol < 1.0:
        by_archetype["reclaim"].append(
            _trigger_blocker(
                code="reclaim_volume_too_weak",
                archetype="reclaim",
                metric="breakout_day_volume_vs_20d",
                current=breakout_vol,
                operator="gte",
                threshold=1.0,
                description="Reclaim entries need at least normal participation.",
            )
        )
    if not _bool(row.get("pass_above_dma_20")):
        by_archetype["reclaim"].append(
            _bool_trigger_blocker(
                code="not_above_dma_20",
                archetype="reclaim",
                metric="pass_above_dma_20",
                current=row.get("pass_above_dma_20"),
                description="Reclaim entries require price above the short-term trend line.",
            )
        )
    if close_location < 0.75:
        by_archetype["reclaim"].append(
            _trigger_blocker(
                code="reclaim_close_not_strong_enough",
                archetype="reclaim",
                metric="close_location_pct",
                current=close_location,
                operator="gte",
                threshold=0.75,
                description="Close quality is not strong enough for a reclaim entry.",
            )
        )
    if breakout_extension > 4.0:
        by_archetype["reclaim"].append(
            _trigger_blocker(
                code="reclaim_too_extended",
                archetype="reclaim",
                metric="breakout_extension_pct",
                current=breakout_extension,
                operator="lte",
                threshold=4.0,
                description="Price is too extended for a low-risk reclaim entry.",
            )
        )
    if dist_20d_high < -1.0:
        by_archetype["reclaim"].append(
            _trigger_blocker(
                code="not_near_recent_high",
                archetype="reclaim",
                metric="dist_20d_high",
                current=dist_20d_high,
                operator="gte",
                threshold=-1.0,
                description="Price has not reclaimed close enough to the recent high.",
            )
        )

    ranked = sorted(by_archetype.items(), key=lambda item: (len(item[1]), item[0]))
    nearest_archetype, nearest_blockers = ranked[0]
    return {
        "schema_version": 1,
        "status": "confirmed" if trigger_type else "blocked",
        "confirmed_trigger_type": trigger_type,
        "confirmed_trigger_note": trigger_note,
        "nearest_trigger_type": trigger_type or nearest_archetype,
        "nearest_trigger_blocker_count": 0 if trigger_type else len(nearest_blockers),
        "nearest_trigger_blockers": [] if trigger_type else nearest_blockers,
        "all_trigger_blockers": by_archetype,
        "authority_scope": "technical_diagnostics_only",
        "action_policy_effect": "explain_only_no_scoring_change",
        "broker_execution_allowed": False,
    }


def diagnose_buy_readiness(
    *,
    score: dict[str, Any],
    technical_reasons: list[str],
    technical_state: str,
    thresholds: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Explain why a technically constructive row is not a BUY_TRIGGERED row."""
    cfg = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    blockers: list[dict[str, Any]] = []
    if not bool(score.get("hard_filter_pass")):
        for reason in score.get("hard_filter_reasons") or []:
            blockers.append(
                {
                    "code": str(reason),
                    "category": "hard_filter",
                    "description": "Hard technical/tradability filter failed.",
                }
            )
    if not score.get("entry_trigger_type"):
        blockers.append(
            {
                "code": "entry_trigger_missing",
                "category": "trigger",
                "description": "No breakout, retest, pullback, or reclaim trigger is confirmed.",
            }
        )
    component_thresholds = {
        "trend_score": ("trend_min", "trend_below_minimum"),
        "structure_score": ("structure_min", "structure_below_minimum"),
        "participation_score": ("participation_min", "participation_below_minimum"),
        "relative_strength_score": ("relative_strength_min", "relative_strength_below_minimum"),
        "tradability_score": ("tradability_min", "tradability_below_minimum"),
    }
    for metric, (threshold_key, code) in component_thresholds.items():
        current = safe_float(score.get(metric))
        threshold = safe_float(cfg.get(threshold_key))
        if current is not None and threshold is not None and current < threshold:
            blockers.append(
                {
                    "code": code,
                    "category": "component_threshold",
                    "metric": metric,
                    "current": current,
                    "operator": "gte",
                    "threshold": threshold,
                    "gap_to_threshold": round(threshold - current, 6),
                    "description": f"{metric} is below the configured buy precondition.",
                }
            )
    total = safe_float(score.get("technical_total_score"))
    buy_min = safe_float(cfg.get("buy_total_min"))
    if total is not None and buy_min is not None and total < buy_min:
        blockers.append(
            {
                "code": "technical_total_below_buy_min",
                "category": "total_score",
                "metric": "technical_total_score",
                "current": total,
                "operator": "gte",
                "threshold": buy_min,
                "gap_to_threshold": round(buy_min - total, 6),
                "description": "Total technical score is below the configured BUY_TRIGGERED threshold.",
            }
        )
    for reason in technical_reasons:
        if not any(item.get("code") == reason for item in blockers):
            blockers.append(
                {
                    "code": str(reason),
                    "category": "technical_reason",
                    "description": "Technical engine emitted this reason while classifying the setup.",
                }
            )
    status = "buy_triggered" if str(technical_state or "").upper() == "BUY_TRIGGERED" else "blocked"
    return {
        "schema_version": 1,
        "status": status,
        "technical_state": str(technical_state or "").upper() or None,
        "blocker_count": 0 if status == "buy_triggered" else len(blockers),
        "blockers": [] if status == "buy_triggered" else blockers,
        "authority_scope": "technical_diagnostics_only",
        "action_policy_effect": "explain_only_no_scoring_change",
        "broker_execution_allowed": False,
    }


def classify_setup_archetype(
    row: pd.Series,
    *,
    technical_state: str | None = None,
    entry_trigger_type: str | None = None,
    technical_total_score: float | None = None,
    thresholds: dict[str, Any] | None = None,
) -> dict[str, Any]:
    flags: list[str] = []
    risks: list[str] = []
    trigger = str(entry_trigger_type or "").strip().lower()
    state = str(technical_state or "").strip().upper()
    total_score = safe_float(technical_total_score)
    pivot_distance = safe_float(row.get("pivot_distance_20d_pct"))
    support_distance = safe_float(row.get("support_distance_20d_pct"))
    range_ratio = safe_float(row.get("range_contraction_ratio"))
    dryup = safe_float(row.get("pullback_volume_dryup_ratio_20d"))
    breakout_vol = safe_float(row.get("breakout_day_volume_vs_20d"))
    breakout_extension = safe_float(row.get("breakout_extension_pct"))
    distribution = safe_float(row.get("distribution_days_20d")) or 0.0
    accumulation = safe_float(row.get("accumulation_days_20d")) or 0.0
    rs_benchmark = safe_float(row.get("rs_vs_benchmark"))
    rs_sector = safe_float(row.get("rs_vs_sector"))
    gap_frequency = safe_float(row.get("gap_frequency_60d"))
    atr_pct = safe_float(row.get("atr_pct"))
    base_depth = safe_float(row.get("base_depth_60d_pct"))

    if _bool(row.get("volatility_contraction_flag")):
        flags.append("volatility_compression")
    if range_ratio is not None and range_ratio < 0.9:
        flags.append("range_contraction")
    if dryup is not None and dryup <= 0.8:
        flags.append("pullback_volume_dryup")
    if breakout_vol is not None and breakout_vol >= 1.8:
        flags.append("breakout_volume_confirmation")
    if accumulation >= distribution + 1:
        flags.append("accumulation_outweighs_distribution")
    if rs_benchmark is not None and rs_benchmark >= 0.03:
        flags.append("benchmark_leadership")
    if rs_sector is not None and rs_sector >= 0.0:
        flags.append("sector_resilience")

    if breakout_extension is not None and breakout_extension >= 10.0:
        risks.append("extended_from_base_or_pivot")
    if distribution >= 4:
        risks.append("distribution_pressure")
    if gap_frequency is not None and gap_frequency > DEFAULT_FILTERS["max_gap_frequency_60d"]:
        risks.append("gap_frequency_risk")
    if atr_pct is not None and atr_pct > DEFAULT_FILTERS["max_atr_pct"]:
        risks.append("high_atr_noise")
    if base_depth is not None and base_depth > DEFAULT_FILTERS["max_base_depth_60d_pct"]:
        risks.append("loose_or_deep_base")

    if trigger in {"breakout", "breakout_retest", "trend_pullback", "reclaim"}:
        archetype = trigger
        maturity = "triggered"
    elif state == "NEAR_PIVOT":
        archetype = "constructive_base_near_pivot"
        maturity = "near_trigger"
    elif state == "READY":
        archetype = "constructive_base_ready"
        maturity = "setup_ready"
    elif state == "WATCHLIST":
        archetype = "base_forming_watchlist"
        maturity = "forming"
    elif state in {"REJECT", "IGNORE"}:
        archetype = "not_tradable_or_low_quality"
        maturity = "invalid_or_low_quality"
    elif support_distance is not None and support_distance <= DEFAULT_THRESHOLDS["trend_add_on_pullback_distance_pct"]:
        archetype = "trend_pullback_watch"
        maturity = "watch"
    elif pivot_distance is not None and abs(pivot_distance) <= DEFAULT_THRESHOLDS["near_pivot_distance_pct"]:
        archetype = "near_pivot_watch"
        maturity = "watch"
    else:
        archetype = "unclassified_technical_setup"
        maturity = "unknown"
    trigger_diagnostics = diagnose_entry_trigger_blockers(row, thresholds=thresholds)

    return {
        "schema_version": 1,
        "archetype": archetype,
        "maturity": maturity,
        "quality_flags": sorted(set(flags)),
        "risk_flags": sorted(set(risks)),
        "trigger_type": trigger or None,
        "technical_state": state or None,
        "technical_total_score": total_score,
        "entry_trigger_status": trigger_diagnostics["status"],
        "nearest_entry_trigger_type": trigger_diagnostics["nearest_trigger_type"],
        "nearest_entry_trigger_blocker_count": trigger_diagnostics["nearest_trigger_blocker_count"],
        "entry_trigger_blockers": trigger_diagnostics["nearest_trigger_blockers"],
        "entry_trigger_diagnostics": trigger_diagnostics,
        "authority_scope": "technical_evidence_only",
        "action_policy_effect": "explain_only_no_scoring_change",
        "broker_execution_allowed": False,
    }


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
    setup_quality = classify_setup_archetype(
        row,
        technical_state=None,
        entry_trigger_type=entry_type,
        technical_total_score=total_score,
        thresholds=thresholds,
    )
    trigger_diagnostics = diagnose_entry_trigger_blockers(row, thresholds=thresholds)
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
        "entry_trigger_diagnostics": trigger_diagnostics,
        "technical_setup_archetype": setup_quality["archetype"],
        "technical_setup_quality": setup_quality,
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

    setup_quality = classify_setup_archetype(
        row,
        technical_state=state,
        entry_trigger_type=score.get("entry_trigger_type"),
        technical_total_score=score.get("technical_total_score"),
        thresholds=thresholds,
    )
    setup_quality["buy_readiness"] = diagnose_buy_readiness(
        score=score,
        technical_reasons=reasons,
        technical_state=state,
        thresholds=thresholds,
    )

    return {
        **score,
        "technical_state": state,
        "technical_reasons": reasons,
        "technical_setup_archetype": setup_quality["archetype"],
        "technical_setup_quality": setup_quality,
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
