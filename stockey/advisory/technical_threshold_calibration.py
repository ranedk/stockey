from __future__ import annotations

import argparse
import itertools
import json
from typing import Any

import pandas as pd

from utils.db import db_session, sql_to_df, upsert_to_db
from utils.sync import parse_datetime_arg


EVALUATIONS_TABLE = "advisory_technical_threshold_evaluations"
SUMMARY_TABLE = "advisory_technical_threshold_eval_summary"
DEFAULT_HORIZONS = [5, 10, 20]
DEFAULT_RETURN_THRESHOLD = 0.03
DEFAULT_COST_BPS = 25.0
DEFAULT_MIN_SIGNALS = 10

DEFAULT_GRID = {
    "trend_min": [0.0, 12.0, 15.0, 18.0],
    "structure_min": [0.0, 15.0, 18.0, 21.0],
    "participation_min": [0.0, 8.0, 10.0, 12.0],
    "relative_strength_min": [0.0, 6.0, 8.0, 10.0],
    "tradability_min": [0.0, 5.0, 6.0, 7.0],
    "ready_total_min": [68.0, 70.0, 72.0],
    "buy_total_min": [76.0, 78.0, 80.0, 82.0],
}

SCORE_COLUMNS = {
    "trend_min": "technical_trend_score",
    "structure_min": "technical_structure_score",
    "participation_min": "technical_participation_score",
    "relative_strength_min": "technical_relative_strength_score",
    "tradability_min": "technical_tradability_score",
    "buy_total_min": "technical_total_score",
}

OPTIONAL_SIGNAL_COLUMNS = [
    "candidate_state",
    "technical_state",
    "technical_trigger_type",
    "technical_score",
    "technical_trend_score",
    "technical_structure_score",
    "technical_participation_score",
    "technical_relative_strength_score",
    "technical_tradability_score",
    "technical_total_score",
    "setup_score",
    "load_ts",
]


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def ensure_tables() -> None:
    with db_session() as (_, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {EVALUATIONS_TABLE} (
                evaluated_at TIMESTAMPTZ NOT NULL,
                config_id TEXT NOT NULL,
                horizon_days BIGINT NOT NULL,
                threshold_config_json TEXT NOT NULL,
                signal_count BIGINT,
                eligible_count BIGINT,
                trade_rate DOUBLE PRECISION,
                avg_forward_return DOUBLE PRECISION,
                median_forward_return DOUBLE PRECISION,
                hit_rate DOUBLE PRECISION,
                avg_forward_return_after_cost DOUBLE PRECISION,
                hit_rate_after_cost DOUBLE PRECISION,
                avg_rejected_forward_return DOUBLE PRECISION,
                spread_vs_rejected DOUBLE PRECISION,
                objective_score DOUBLE PRECISION,
                sample_start TIMESTAMPTZ,
                sample_end TIMESTAMPTZ,
                load_ts TIMESTAMPTZ,
                UNIQUE (config_id, horizon_days)
            )
            """
        )
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SUMMARY_TABLE} (
                evaluated_at TIMESTAMPTZ NOT NULL,
                horizon_days BIGINT NOT NULL,
                best_config_id TEXT,
                best_threshold_config_json TEXT,
                best_objective_score DOUBLE PRECISION,
                best_eligible_count BIGINT,
                best_hit_rate_after_cost DOUBLE PRECISION,
                best_avg_forward_return_after_cost DOUBLE PRECISION,
                baseline_signal_count BIGINT,
                baseline_avg_forward_return_after_cost DOUBLE PRECISION,
                baseline_hit_rate_after_cost DOUBLE PRECISION,
                recommendation TEXT,
                load_ts TIMESTAMPTZ,
                UNIQUE (evaluated_at, horizon_days)
            )
            """
        )


def table_columns(table_name: str) -> set[str]:
    try:
        df = sql_to_df(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = %s
            """,
            params=(table_name,),
            retries=2,
        )
    except Exception:
        return set()
    return set(df["column_name"].astype(str).tolist()) if not df.empty else set()


def load_technical_signal_rows(
    *,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
) -> pd.DataFrame:
    available = table_columns("advisory_candidates")
    if not available:
        return pd.DataFrame()
    score_source_col = "technical_total_score" if "technical_total_score" in available else "technical_score" if "technical_score" in available else None
    if score_source_col is None:
        return pd.DataFrame()
    clauses = [f"{score_source_col} IS NOT NULL", "NULLIF(TRIM(symbol), '') IS NOT NULL"]
    params: list[Any] = []
    if from_date is not None:
        clauses.append("asof_date >= %s")
        params.append(from_date)
    if to_date is not None:
        clauses.append("asof_date <= %s")
        params.append(to_date)
    if symbols:
        clauses.append("UPPER(TRIM(symbol)) = ANY(%s)")
        params.append([str(value).upper() for value in symbols])
    select_exprs = ["asof_date", "setup_id", "symbol"]
    for col in OPTIONAL_SIGNAL_COLUMNS:
        if col in {"technical_total_score", "technical_score"}:
            if col in available:
                select_exprs.append(col)
            continue
        select_exprs.append(col if col in available else f"NULL AS {col}")
    if "technical_score" not in available:
        select_exprs.append("NULL::double precision AS technical_score")
    if "technical_total_score" not in available:
        if "technical_score" in available:
            select_exprs.append("(technical_score * 100.0) AS technical_total_score")
        else:
            select_exprs.append("NULL::double precision AS technical_total_score")
    df = sql_to_df(
        f"""
        SELECT
            {', '.join(select_exprs)}
        FROM advisory_candidates
        WHERE {' AND '.join(clauses)}
        ORDER BY asof_date, symbol, setup_id
        """,
        params=tuple(params) if params else None,
        retries=4,
        statement_timeout_ms=0,
        chunksize=50000,
    )
    if df.empty:
        return df
    df["asof_date"] = pd.to_datetime(df["asof_date"], utc=True, errors="coerce").dt.normalize()
    df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()
    numeric_cols = [
        "technical_score",
        "technical_trend_score",
        "technical_structure_score",
        "technical_participation_score",
        "technical_relative_strength_score",
        "technical_tradability_score",
        "technical_total_score",
        "setup_score",
    ]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["asof_date", "symbol", "technical_total_score"])


def load_price_history_for_returns(
    *,
    symbols: list[str],
    from_date: pd.Timestamp,
    to_date: pd.Timestamp,
) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()
    df = sql_to_df(
        """
        SELECT ticker AS symbol, date, close
        FROM dhan_ohlcv_daily
        WHERE asset_type = 'stock'
          AND exchange = 'NSE'
          AND ticker = ANY(%s)
          AND date >= %s
          AND date <= %s
        ORDER BY ticker, date
        """,
        params=([str(value).upper() for value in symbols], from_date, to_date),
        retries=4,
        statement_timeout_ms=0,
        chunksize=100000,
    )
    if df.empty:
        return df
    df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()
    df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce").dt.normalize()
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df.dropna(subset=["symbol", "date", "close"])


def attach_forward_returns(signals: pd.DataFrame, prices: pd.DataFrame, *, horizons: list[int]) -> pd.DataFrame:
    if signals.empty or prices.empty:
        return signals.copy()
    out = signals.copy().reset_index(drop=True)
    price_map = {
        symbol: group.sort_values("date").reset_index(drop=True)
        for symbol, group in prices.groupby("symbol", dropna=False)
    }
    for horizon in horizons:
        out[f"entry_date_h{horizon}"] = pd.Series([None] * len(out), dtype="object")
        out[f"exit_date_h{horizon}"] = pd.Series([None] * len(out), dtype="object")
        out[f"entry_close_h{horizon}"] = pd.NA
        out[f"exit_close_h{horizon}"] = pd.NA
        out[f"forward_return_h{horizon}"] = pd.NA

    for idx, row in out.iterrows():
        history = price_map.get(str(row.get("symbol") or "").upper())
        if history is None or history.empty:
            continue
        asof_date = pd.to_datetime(row.get("asof_date"), utc=True, errors="coerce")
        if pd.isna(asof_date):
            continue
        eligible = history[history["date"] > asof_date.normalize()].reset_index(drop=True)
        if eligible.empty:
            continue
        for horizon in horizons:
            exit_idx = int(horizon) - 1
            if len(eligible) <= exit_idx:
                continue
            entry = eligible.iloc[0]
            exit_row = eligible.iloc[exit_idx]
            entry_close = pd.to_numeric(entry.get("close"), errors="coerce")
            exit_close = pd.to_numeric(exit_row.get("close"), errors="coerce")
            if pd.isna(entry_close) or pd.isna(exit_close) or float(entry_close) <= 0:
                continue
            out.at[idx, f"entry_date_h{horizon}"] = entry["date"]
            out.at[idx, f"exit_date_h{horizon}"] = exit_row["date"]
            out.at[idx, f"entry_close_h{horizon}"] = float(entry_close)
            out.at[idx, f"exit_close_h{horizon}"] = float(exit_close)
            out.at[idx, f"forward_return_h{horizon}"] = (float(exit_close) / float(entry_close)) - 1.0
    return out


def build_threshold_grid(grid: dict[str, list[float]] | None = None) -> list[dict[str, float]]:
    raw = grid or DEFAULT_GRID
    keys = list(raw.keys())
    configs: list[dict[str, float]] = []
    for values in itertools.product(*(raw[key] for key in keys)):
        cfg = {key: float(value) for key, value in zip(keys, values, strict=True)}
        if cfg["buy_total_min"] < cfg["ready_total_min"]:
            continue
        configs.append(cfg)
    return configs


def _eligible_mask(dataset: pd.DataFrame, config: dict[str, float]) -> pd.Series:
    mask = pd.Series(True, index=dataset.index)
    for threshold_key, column in SCORE_COLUMNS.items():
        if column not in dataset.columns:
            continue
        values = pd.to_numeric(dataset[column], errors="coerce")
        if values.notna().sum() == 0:
            continue
        mask &= values.ge(float(config[threshold_key]))
    trigger = dataset.get("technical_trigger_type", pd.Series("", index=dataset.index)).astype("string").fillna("")
    state = dataset.get("technical_state", pd.Series("", index=dataset.index)).astype("string").fillna("").str.upper()
    has_trigger_context = bool(trigger.ne("").any() or state.eq("BUY_TRIGGERED").any())
    if has_trigger_context:
        mask &= trigger.ne("") | state.eq("BUY_TRIGGERED")
    return mask.fillna(False)


def evaluate_threshold_config(
    dataset: pd.DataFrame,
    config: dict[str, float],
    *,
    horizon_days: int,
    return_threshold: float = DEFAULT_RETURN_THRESHOLD,
    cost_bps: float = DEFAULT_COST_BPS,
    min_signals: int = DEFAULT_MIN_SIGNALS,
    evaluated_at: pd.Timestamp | None = None,
) -> dict[str, Any]:
    return_col = f"forward_return_h{int(horizon_days)}"
    matured = dataset[return_col].notna() if return_col in dataset.columns else pd.Series(False, index=dataset.index)
    sample = dataset[matured].copy()
    signal_count = int(len(sample))
    eligible = _eligible_mask(sample, config) if not sample.empty else pd.Series(False, index=sample.index)
    selected = sample[eligible]
    rejected = sample[~eligible]
    cost = float(cost_bps) / 10000.0
    selected_returns = pd.to_numeric(selected.get(return_col), errors="coerce").dropna()
    rejected_returns = pd.to_numeric(rejected.get(return_col), errors="coerce").dropna()
    after_cost = selected_returns - cost
    hit_after_cost = after_cost.ge(float(return_threshold))
    eligible_count = int(len(selected_returns))
    avg_after_cost = float(after_cost.mean()) if not after_cost.empty else None
    hit_rate_after_cost = float(hit_after_cost.mean()) if not hit_after_cost.empty else None
    avg_rejected = float((rejected_returns - cost).mean()) if not rejected_returns.empty else None
    spread = None if avg_after_cost is None or avg_rejected is None else float(avg_after_cost - avg_rejected)
    sample_start = sample["asof_date"].min() if not sample.empty else pd.NaT
    sample_end = sample["asof_date"].max() if not sample.empty else pd.NaT
    objective = None
    if eligible_count >= int(min_signals) and avg_after_cost is not None and hit_rate_after_cost is not None:
        objective = float((avg_after_cost * 100.0) + (hit_rate_after_cost * 0.50) + min(eligible_count, 200) / 1000.0)
        if spread is not None:
            objective += float(spread * 25.0)
    config_id = (
        f"h{int(horizon_days)}_t{config['trend_min']:.0f}_s{config['structure_min']:.0f}_p{config['participation_min']:.0f}_"
        f"rs{config['relative_strength_min']:.0f}_tr{config['tradability_min']:.0f}_r{config['ready_total_min']:.0f}_b{config['buy_total_min']:.0f}"
    )
    return {
        "evaluated_at": pd.to_datetime(evaluated_at or pd.Timestamp.utcnow(), utc=True, errors="coerce"),
        "config_id": config_id,
        "horizon_days": int(horizon_days),
        "threshold_config_json": json_dumps(config),
        "signal_count": signal_count,
        "eligible_count": eligible_count,
        "trade_rate": None if signal_count == 0 else eligible_count / signal_count,
        "avg_forward_return": float(selected_returns.mean()) if not selected_returns.empty else None,
        "median_forward_return": float(selected_returns.median()) if not selected_returns.empty else None,
        "hit_rate": float(selected_returns.ge(float(return_threshold)).mean()) if not selected_returns.empty else None,
        "avg_forward_return_after_cost": avg_after_cost,
        "hit_rate_after_cost": hit_rate_after_cost,
        "avg_rejected_forward_return": avg_rejected,
        "spread_vs_rejected": spread,
        "objective_score": objective,
        "sample_start": sample_start,
        "sample_end": sample_end,
        "load_ts": pd.Timestamp.utcnow(),
    }


def build_summary(evaluations: pd.DataFrame, *, evaluated_at: pd.Timestamp) -> pd.DataFrame:
    if evaluations.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for horizon, group in evaluations.groupby("horizon_days", dropna=False):
        ranked = group.dropna(subset=["objective_score"]).sort_values(
            ["objective_score", "eligible_count", "hit_rate_after_cost"],
            ascending=[False, False, False],
            kind="stable",
        )
        best = ranked.iloc[0] if not ranked.empty else group.sort_values("eligible_count", ascending=False).iloc[0]
        baseline = group.sort_values("eligible_count", ascending=False).iloc[0]
        recommendation = (
            "review_for_promotion"
            if pd.notna(best.get("objective_score")) and int(best.get("eligible_count") or 0) >= DEFAULT_MIN_SIGNALS
            else "insufficient_matured_signals"
        )
        rows.append(
            {
                "evaluated_at": evaluated_at,
                "horizon_days": int(horizon),
                "best_config_id": best.get("config_id"),
                "best_threshold_config_json": best.get("threshold_config_json"),
                "best_objective_score": best.get("objective_score"),
                "best_eligible_count": best.get("eligible_count"),
                "best_hit_rate_after_cost": best.get("hit_rate_after_cost"),
                "best_avg_forward_return_after_cost": best.get("avg_forward_return_after_cost"),
                "baseline_signal_count": baseline.get("eligible_count"),
                "baseline_avg_forward_return_after_cost": baseline.get("avg_forward_return_after_cost"),
                "baseline_hit_rate_after_cost": baseline.get("hit_rate_after_cost"),
                "recommendation": recommendation,
                "load_ts": pd.Timestamp.utcnow(),
            }
        )
    return pd.DataFrame(rows)


def calibrate_thresholds(
    *,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    horizons: list[int] | None = None,
    return_threshold: float = DEFAULT_RETURN_THRESHOLD,
    cost_bps: float = DEFAULT_COST_BPS,
    min_signals: int = DEFAULT_MIN_SIGNALS,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    effective_horizons = sorted({int(value) for value in (horizons or DEFAULT_HORIZONS) if int(value) > 0})
    signals = load_technical_signal_rows(from_date=from_date, to_date=to_date, symbols=symbols)
    if signals.empty or not effective_horizons:
        return pd.DataFrame(), pd.DataFrame(), {"signal_rows": int(len(signals)), "matured_rows": 0}
    price_start = signals["asof_date"].min() - pd.Timedelta(days=5)
    price_end = signals["asof_date"].max() + pd.Timedelta(days=max(effective_horizons) * 3 + 15)
    prices = load_price_history_for_returns(
        symbols=signals["symbol"].dropna().astype(str).str.upper().drop_duplicates().tolist(),
        from_date=price_start,
        to_date=price_end,
    )
    dataset = attach_forward_returns(signals, prices, horizons=effective_horizons)
    evaluated_at = pd.Timestamp.utcnow()
    rows: list[dict[str, Any]] = []
    for horizon in effective_horizons:
        for config in build_threshold_grid():
            rows.append(
                evaluate_threshold_config(
                    dataset,
                    config,
                    horizon_days=horizon,
                    return_threshold=return_threshold,
                    cost_bps=cost_bps,
                    min_signals=min_signals,
                    evaluated_at=evaluated_at,
                )
            )
    evaluations = pd.DataFrame(rows)
    summary = build_summary(evaluations, evaluated_at=evaluated_at)
    meta = {
        "signal_rows": int(len(signals)),
        "price_rows": int(len(prices)),
        "matured_rows_by_horizon": {
            str(horizon): int(dataset[f"forward_return_h{horizon}"].notna().sum())
            for horizon in effective_horizons
            if f"forward_return_h{horizon}" in dataset.columns
        },
        "grid_count": len(build_threshold_grid()),
        "horizons": effective_horizons,
        "return_threshold": float(return_threshold),
        "cost_bps": float(cost_bps),
        "min_signals": int(min_signals),
    }
    return evaluations, summary, meta


def persist_outputs(evaluations: pd.DataFrame, summary: pd.DataFrame) -> None:
    ensure_tables()
    if not evaluations.empty:
        upsert_to_db(evaluations, EVALUATIONS_TABLE, unique_keys=["config_id", "horizon_days"], timescaledb_column="evaluated_at")
    if not summary.empty:
        upsert_to_db(summary, SUMMARY_TABLE, unique_keys=["evaluated_at", "horizon_days"], timescaledb_column="evaluated_at")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Calibrate swing technical thresholds against realized forward returns.")
    parser.add_argument("--from-date", type=parse_datetime_arg)
    parser.add_argument("--to-date", type=parse_datetime_arg)
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument("--horizons", nargs="*", type=int, default=DEFAULT_HORIZONS)
    parser.add_argument("--return-threshold", type=float, default=DEFAULT_RETURN_THRESHOLD)
    parser.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    parser.add_argument("--min-signals", type=int, default=DEFAULT_MIN_SIGNALS)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    evaluations, summary, meta = calibrate_thresholds(
        from_date=pd.Timestamp(args.from_date, tz="UTC") if args.from_date else None,
        to_date=pd.Timestamp(args.to_date, tz="UTC") if args.to_date else None,
        symbols=args.symbols,
        horizons=args.horizons,
        return_threshold=float(args.return_threshold),
        cost_bps=float(args.cost_bps),
        min_signals=int(args.min_signals),
    )
    if not args.dry_run:
        persist_outputs(evaluations, summary)
    print(
        json.dumps(
            {
                "status": "ok",
                "evaluations_table": EVALUATIONS_TABLE,
                "summary_table": SUMMARY_TABLE,
                "evaluation_rows": int(len(evaluations)),
                "summary_rows": int(len(summary)),
                "meta": meta,
                "summary_sample": summary.head(10).to_dict(orient="records") if not summary.empty else [],
                "dry_run": bool(args.dry_run),
            },
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
