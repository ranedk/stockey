from __future__ import annotations

import json
from typing import Any

import pandas as pd

from advisory.technical_features import DEFAULT_BENCHMARK_NAME, load_benchmark_series


def _number(value: Any) -> float | None:
    numeric = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric):
        return None
    return float(numeric)


def load_benchmark_history_for_attribution(
    *,
    from_date: pd.Timestamp,
    to_date: pd.Timestamp,
    benchmark_name: str = DEFAULT_BENCHMARK_NAME,
) -> pd.DataFrame:
    df = load_benchmark_series(benchmark_name=benchmark_name, start_date=from_date, to_date=to_date)
    if df.empty:
        return df
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"], utc=True, errors="coerce").dt.normalize()
    out["benchmark_close"] = pd.to_numeric(out["benchmark_close"], errors="coerce")
    return out.dropna(subset=["date", "benchmark_close"]).sort_values("date").reset_index(drop=True)


def attach_benchmark_forward_returns(
    signals: pd.DataFrame,
    benchmark_prices: pd.DataFrame,
    *,
    horizons: list[int],
    benchmark_name: str = DEFAULT_BENCHMARK_NAME,
) -> pd.DataFrame:
    if signals.empty or benchmark_prices.empty:
        return signals.copy()
    out = signals.copy().reset_index(drop=True)
    history = benchmark_prices.copy()
    history["date"] = pd.to_datetime(history["date"], utc=True, errors="coerce").dt.normalize()
    history["benchmark_close"] = pd.to_numeric(history["benchmark_close"], errors="coerce")
    history = history.dropna(subset=["date", "benchmark_close"]).sort_values("date").reset_index(drop=True)
    if history.empty:
        return out

    out["benchmark_name"] = benchmark_name
    out["benchmark_return_contract_json"] = json.dumps(
        {
            "benchmark": benchmark_name,
            "entry_rule": "Use first available benchmark close strictly after signal asof_date.",
            "exit_rule": "Use the horizon-th available benchmark close from the same strictly-after-asof benchmark window.",
            "same_day_benchmark_allowed": False,
            "benchmark_returns_are_attribution_only": True,
        },
        sort_keys=True,
    )
    for horizon in horizons:
        horizon = int(horizon)
        out[f"benchmark_entry_date_h{horizon}"] = pd.Series([None] * len(out), dtype="object")
        out[f"benchmark_exit_date_h{horizon}"] = pd.Series([None] * len(out), dtype="object")
        out[f"benchmark_entry_close_h{horizon}"] = pd.NA
        out[f"benchmark_exit_close_h{horizon}"] = pd.NA
        out[f"benchmark_forward_return_h{horizon}"] = pd.NA

    for idx, row in out.iterrows():
        asof_date = pd.to_datetime(row.get("asof_date"), utc=True, errors="coerce")
        if pd.isna(asof_date):
            continue
        eligible = history[history["date"] > asof_date.normalize()].reset_index(drop=True)
        if eligible.empty:
            continue
        for horizon in horizons:
            horizon = int(horizon)
            exit_idx = horizon - 1
            if len(eligible) <= exit_idx:
                continue
            entry = eligible.iloc[0]
            exit_row = eligible.iloc[exit_idx]
            entry_close = _number(entry.get("benchmark_close"))
            exit_close = _number(exit_row.get("benchmark_close"))
            if entry_close is None or exit_close is None or entry_close <= 0:
                continue
            out.at[idx, f"benchmark_entry_date_h{horizon}"] = entry["date"]
            out.at[idx, f"benchmark_exit_date_h{horizon}"] = exit_row["date"]
            out.at[idx, f"benchmark_entry_close_h{horizon}"] = entry_close
            out.at[idx, f"benchmark_exit_close_h{horizon}"] = exit_close
            out.at[idx, f"benchmark_forward_return_h{horizon}"] = (exit_close / entry_close) - 1.0
    return out
