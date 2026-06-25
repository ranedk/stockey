"""Wide-universe validation of the price/factor model (docs/price_factor_model.md).

Uses dhan_ohlcv_daily (the deep+wide price source, keyed by company_master_id) joined to
advisory_fundamentals_daily for the fundamental factors -- ~178 securities with deep history vs the
~20 in advisory_technical_daily. Computes factors point-in-time from the raw price series, attaches
forward 20d cross-sectional excess, and reports per-factor IC + the combined confidence-score IC
against single components (the test of whether confluence adds value).

Research/analysis only -- no DB writes. Run:  python scripts/validate_price_factors.py
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from utils.db import sql_to_df
from advisory.price_factors import (
    compute_price_series_factors, build_confidence_scores, factor_ic_report,
    DEFAULT_COMPONENT_WEIGHTS,
)

FROM_DATE, TO_DATE = "2017-01-01", "2024-12-31"
SAMPLE_EVERY, HORIZON, MIN_HISTORY, MIN_CROSS_SECTION = 15, 20, 300, 20


def _spearman(frame: pd.DataFrame, col: str) -> float:
    mask = frame[col].notna() & frame["forward_excess"].notna()
    return round(float(frame.loc[mask, col].corr(frame.loc[mask, "forward_excess"], method="spearman")), 4)


def build_validation_frame() -> pd.DataFrame:
    px = sql_to_df(
        "SELECT company_master_id, date, close, volume FROM dhan_ohlcv_daily "
        "WHERE date BETWEEN %(f)s AND %(t)s AND close IS NOT NULL "
        "AND company_master_id IN (SELECT DISTINCT company_master_id FROM advisory_fundamentals_daily)",
        params={"f": FROM_DATE, "t": TO_DATE},
    )
    px["date"] = pd.to_datetime(px["date"], utc=True).dt.normalize()
    px = px.sort_values(["company_master_id", "date"]).reset_index(drop=True)
    fund = sql_to_df(
        "SELECT company_master_id, asof_date, profit_after_tax_qoq_growth, total_revenue_qoq_growth, "
        "net_debt_to_equity, free_cash_flow_to_equity FROM advisory_fundamentals_daily "
        "WHERE asof_date BETWEEN '2016-06-01' AND %(t)s",
        params={"t": TO_DATE},
    )
    fund["asof_date"] = pd.to_datetime(fund["asof_date"], utc=True).dt.normalize()

    records = []
    for cid, group in px.groupby("company_master_id"):
        group = group.reset_index(drop=True)
        n = len(group)
        if n < MIN_HISTORY:
            continue
        closes = group["close"].to_numpy(float)
        volumes = group["volume"].to_numpy(float)
        dates = group["date"].to_numpy()
        daily_abs = np.abs(np.diff(closes) / closes[:-1])
        for i in range(252, n - HORIZON - 1, SAMPLE_EVERY):
            gap = (pd.Timestamp(dates[i + HORIZON]) - pd.Timestamp(dates[i])).days
            if not (HORIZON <= gap <= 45):
                continue
            forward = closes[i + HORIZON] / closes[i] - 1.0
            if abs(forward) > 1.0 or (i >= HORIZON and daily_abs[i - HORIZON:i].max() > 0.5):
                continue  # split / corporate-action guard
            factors = compute_price_series_factors(closes[: i + 1], volumes=volumes[: i + 1])
            factors.update(company_master_id=cid, asof_date=pd.Timestamp(dates[i]),
                           liquidity=float(np.nanmean(volumes[max(0, i - 19): i + 1])), fwd=forward)
            records.append(factors)

    frame = pd.DataFrame(records)
    frame = pd.merge_asof(frame.sort_values("asof_date"), fund.sort_values("asof_date"),
                          on="asof_date", by="company_master_id", direction="backward")
    frame["earnings_growth"] = frame["profit_after_tax_qoq_growth"]
    frame["revenue_growth"] = frame["total_revenue_qoq_growth"]
    frame["leverage"] = -frame["net_debt_to_equity"]
    frame["cash_generation"] = frame["free_cash_flow_to_equity"]
    counts = frame.groupby("asof_date")["company_master_id"].transform("count")
    frame = frame[counts >= MIN_CROSS_SECTION].copy()
    frame["forward_excess"] = frame["fwd"] - frame.groupby("asof_date")["fwd"].transform("mean")
    return frame


def main() -> int:
    frame = build_validation_frame()
    print(f"WIDE universe: obs {len(frame):,}  dates {frame['asof_date'].nunique()}  "
          f"securities {frame['company_master_id'].nunique()}")
    report = factor_ic_report(frame).dropna(subset=["spearman_ic"]).sort_values("spearman_ic", ascending=False)
    print("\n=== per-factor IC (Spearman vs forward 20d cross-sectional excess) ===")
    print(report[["component", "n", "spearman_ic"]].to_string())

    scored = build_confidence_scores(frame, weights=DEFAULT_COMPONENT_WEIGHTS)
    print("\n=== confidence vs single components (default weights) ===")
    for col in ["confidence", "score_fundamental", "score_trend", "score_volume",
                "score_liquidity", "score_reversion", "score_volatility"]:
        if col in scored.columns:
            print(f"  {col:<20} IC = {_spearman(scored, col):+.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
