from __future__ import annotations

import argparse
import json
from typing import Any, Iterable

import numpy as np
import pandas as pd

from advisory.ts_forecast_features import TABLE_NAME as FORECAST_TABLE
from utils.db import db_session, sql_to_df, upsert_to_db
from utils.sync import parse_datetime_arg


EVALUATIONS_TABLE = "advisory_ts_forecast_evaluations"
SUMMARY_TABLE = "advisory_ts_forecast_eval_summary"
DEFAULT_COST_BPS = 25.0


def _normalize_timestamp(value: Any | None) -> pd.Timestamp | None:
    if value is None:
        return None
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.normalize()


def _as_symbol_list(symbols: Iterable[str] | None) -> list[str]:
    out: list[str] = []
    for value in symbols or []:
        for part in str(value).split(","):
            symbol = part.strip().upper()
            if symbol:
                out.append(symbol)
    return sorted(set(out))


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _annualized_sharpe(returns: pd.Series, horizon_days: int) -> float | None:
    clean = pd.to_numeric(returns, errors="coerce").dropna()
    if len(clean) < 2:
        return None
    std = float(clean.std(ddof=0))
    if std <= 0:
        return None
    scale = np.sqrt(252.0 / max(int(horizon_days), 1))
    return float((clean.mean() / std) * scale)


def ensure_tables() -> None:
    with db_session() as (_, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {EVALUATIONS_TABLE} (
                asof_date TIMESTAMPTZ NOT NULL,
                symbol TEXT NOT NULL,
                model_name TEXT NOT NULL,
                forecast_horizon_days BIGINT NOT NULL,
                action_hint TEXT,
                forecast_return DOUBLE PRECISION,
                realized_return DOUBLE PRECISION,
                cost_adjusted_return DOUBLE PRECISION,
                forecast_direction BIGINT,
                realized_direction BIGINT,
                direction_hit BOOLEAN,
                positive_realized BOOLEAN,
                absolute_error DOUBLE PRECISION,
                squared_error DOUBLE PRECISION,
                future_date TIMESTAMPTZ,
                entry_price DOUBLE PRECISION,
                exit_price DOUBLE PRECISION,
                evaluation_status TEXT,
                evaluation_detail TEXT,
                load_ts TIMESTAMPTZ,
                UNIQUE (asof_date, symbol, model_name, forecast_horizon_days)
            )
            """
        )
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SUMMARY_TABLE} (
                evaluated_at TIMESTAMPTZ NOT NULL,
                from_date TIMESTAMPTZ,
                to_date TIMESTAMPTZ,
                model_name TEXT NOT NULL,
                forecast_horizon_days BIGINT NOT NULL,
                action_hint TEXT NOT NULL,
                row_count BIGINT,
                hit_rate DOUBLE PRECISION,
                positive_rate DOUBLE PRECISION,
                avg_realized_return DOUBLE PRECISION,
                avg_cost_adjusted_return DOUBLE PRECISION,
                median_cost_adjusted_return DOUBLE PRECISION,
                avg_absolute_error DOUBLE PRECISION,
                rmse DOUBLE PRECISION,
                sharpe_like DOUBLE PRECISION,
                max_drawdown_proxy DOUBLE PRECISION,
                config_json TEXT,
                load_ts TIMESTAMPTZ,
                UNIQUE (evaluated_at, model_name, forecast_horizon_days, action_hint)
            )
            """
        )


def load_forecasts(
    *,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    model_names: list[str] | None = None,
) -> pd.DataFrame:
    clauses = ["1 = 1"]
    params: list[object] = []
    if from_date is not None:
        clauses.append("asof_date >= %s")
        params.append(from_date)
    if to_date is not None:
        clauses.append("asof_date <= %s")
        params.append(to_date)
    if symbols:
        clauses.append("symbol = ANY(%s)")
        params.append(_as_symbol_list(symbols))
    if model_names:
        clauses.append("model_name = ANY(%s)")
        params.append([str(value).strip().lower() for value in model_names if str(value).strip()])
    df = sql_to_df(
        f"""
        SELECT
            asof_date,
            symbol,
            model_name,
            forecast_horizon_days,
            action_hint,
            forecast_return,
            forecast_price,
            probability_positive,
            signal_quality
        FROM {FORECAST_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY asof_date, model_name, forecast_horizon_days, symbol
        """,
        params=tuple(params) if params else None,
    )
    if df.empty:
        return df
    df["asof_date"] = pd.to_datetime(df["asof_date"], utc=True, errors="coerce").dt.normalize()
    df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()
    df["model_name"] = df["model_name"].astype("string").str.strip().str.lower()
    for col in ["forecast_horizon_days", "forecast_return", "forecast_price", "probability_positive", "signal_quality"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["asof_date", "symbol", "model_name", "forecast_horizon_days", "forecast_return"])


def load_price_window(
    *,
    symbols: list[str],
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()
    df = sql_to_df(
        """
        SELECT ticker AS symbol, date, close
        FROM dhan_ohlcv_daily
        WHERE ticker = ANY(%s)
          AND asset_type = 'stock'
          AND exchange = 'NSE'
          AND date >= %s
          AND date <= %s
        ORDER BY ticker, date
        """,
        params=(_as_symbol_list(symbols), start_date, end_date),
    )
    if df.empty:
        return df
    df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()
    df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce").dt.normalize()
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df.dropna(subset=["symbol", "date", "close"]).drop_duplicates(subset=["symbol", "date"], keep="last")


def _nth_future_price(price_group: pd.DataFrame, asof_date: pd.Timestamp, horizon_days: int) -> tuple[pd.Timestamp | None, float | None, float | None, str]:
    future = price_group[price_group["date"].ge(asof_date)].sort_values("date").reset_index(drop=True)
    if future.empty:
        return None, None, None, "missing_entry_price"
    entry_row = future.iloc[0]
    target_idx = int(horizon_days)
    if len(future) <= target_idx:
        return None, float(entry_row["close"]), None, "not_matured"
    exit_row = future.iloc[target_idx]
    return pd.Timestamp(exit_row["date"]), float(entry_row["close"]), float(exit_row["close"]), "evaluated"


def build_forecast_evaluations(
    *,
    forecasts: pd.DataFrame | None = None,
    prices: pd.DataFrame | None = None,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    model_names: list[str] | None = None,
    cost_bps: float = DEFAULT_COST_BPS,
) -> pd.DataFrame:
    forecasts_df = forecasts if forecasts is not None else load_forecasts(
        from_date=from_date,
        to_date=to_date,
        symbols=symbols,
        model_names=model_names,
    )
    if forecasts_df.empty:
        return pd.DataFrame()
    forecasts_df = forecasts_df.copy()
    forecasts_df["asof_date"] = pd.to_datetime(forecasts_df["asof_date"], utc=True, errors="coerce").dt.normalize()
    forecasts_df["symbol"] = forecasts_df["symbol"].astype("string").str.strip().str.upper()
    for col in ["forecast_horizon_days", "forecast_return"]:
        forecasts_df[col] = pd.to_numeric(forecasts_df[col], errors="coerce")
    max_horizon = int(forecasts_df["forecast_horizon_days"].max())
    min_date = forecasts_df["asof_date"].min()
    max_date = forecasts_df["asof_date"].max() + pd.Timedelta(days=max_horizon * 3)
    price_df = prices if prices is not None else load_price_window(
        symbols=forecasts_df["symbol"].dropna().astype(str).unique().tolist(),
        start_date=min_date,
        end_date=max_date,
    )
    if price_df.empty:
        return pd.DataFrame()
    price_df = price_df.copy()
    price_df["date"] = pd.to_datetime(price_df["date"], utc=True, errors="coerce").dt.normalize()
    price_df["symbol"] = price_df["symbol"].astype("string").str.strip().str.upper()
    price_map = {symbol: group.sort_values("date").reset_index(drop=True) for symbol, group in price_df.groupby("symbol", sort=False)}
    cost_rate = float(cost_bps) / 10000.0
    rows: list[dict[str, Any]] = []
    now = pd.Timestamp.utcnow()
    for _, forecast in forecasts_df.iterrows():
        symbol = str(forecast["symbol"]).upper()
        horizon_days = int(forecast["forecast_horizon_days"])
        future_date, entry_price, exit_price, status = _nth_future_price(
            price_map.get(symbol, pd.DataFrame(columns=["date", "close"])),
            pd.Timestamp(forecast["asof_date"]),
            horizon_days,
        )
        realized_return = None
        cost_adjusted_return = None
        forecast_return = float(forecast["forecast_return"])
        if status == "evaluated" and entry_price and exit_price is not None:
            realized_return = (exit_price / entry_price) - 1.0
            cost_adjusted_return = realized_return - cost_rate
        forecast_direction = 1 if forecast_return > 0 else -1 if forecast_return < 0 else 0
        realized_direction = (
            None
            if realized_return is None
            else 1 if realized_return > 0
            else -1 if realized_return < 0
            else 0
        )
        direction_hit = None if realized_direction is None else bool(forecast_direction == realized_direction)
        absolute_error = None if realized_return is None else abs(forecast_return - realized_return)
        rows.append(
            {
                "asof_date": forecast["asof_date"],
                "symbol": symbol,
                "model_name": forecast["model_name"],
                "forecast_horizon_days": horizon_days,
                "action_hint": forecast.get("action_hint"),
                "forecast_return": forecast_return,
                "realized_return": realized_return,
                "cost_adjusted_return": cost_adjusted_return,
                "forecast_direction": forecast_direction,
                "realized_direction": realized_direction,
                "direction_hit": direction_hit,
                "positive_realized": None if realized_return is None else bool(realized_return > 0),
                "absolute_error": absolute_error,
                "squared_error": None if absolute_error is None else absolute_error * absolute_error,
                "future_date": future_date,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "evaluation_status": status,
                "evaluation_detail": f"horizon={horizon_days}; cost_bps={float(cost_bps):.2f}",
                "load_ts": now,
            }
        )
    return pd.DataFrame(rows)


def build_evaluation_summary(
    evaluations: pd.DataFrame,
    *,
    cost_bps: float = DEFAULT_COST_BPS,
    evaluated_at: pd.Timestamp | None = None,
) -> pd.DataFrame:
    if evaluations.empty:
        return pd.DataFrame()
    evaluated = evaluations[evaluations["evaluation_status"].eq("evaluated")].copy()
    if evaluated.empty:
        return pd.DataFrame()
    evaluated_at = pd.to_datetime(evaluated_at or pd.Timestamp.utcnow(), utc=True, errors="coerce")
    rows: list[dict[str, Any]] = []
    grouping_cols = ["model_name", "forecast_horizon_days", "action_hint"]
    for keys, group in evaluated.groupby(grouping_cols, dropna=False, sort=True):
        model_name, horizon_days, action_hint = keys
        realized = pd.to_numeric(group["realized_return"], errors="coerce")
        adjusted = pd.to_numeric(group["cost_adjusted_return"], errors="coerce")
        squared = pd.to_numeric(group["squared_error"], errors="coerce")
        hit = group["direction_hit"].astype("boolean")
        positive = group["positive_realized"].astype("boolean")
        rows.append(
            {
                "evaluated_at": evaluated_at,
                "from_date": group["asof_date"].min(),
                "to_date": group["asof_date"].max(),
                "model_name": str(model_name),
                "forecast_horizon_days": int(horizon_days),
                "action_hint": str(action_hint or "UNKNOWN"),
                "row_count": int(len(group)),
                "hit_rate": float(hit.mean()) if len(hit.dropna()) else None,
                "positive_rate": float(positive.mean()) if len(positive.dropna()) else None,
                "avg_realized_return": float(realized.mean()) if len(realized.dropna()) else None,
                "avg_cost_adjusted_return": float(adjusted.mean()) if len(adjusted.dropna()) else None,
                "median_cost_adjusted_return": float(adjusted.median()) if len(adjusted.dropna()) else None,
                "avg_absolute_error": float(pd.to_numeric(group["absolute_error"], errors="coerce").mean()),
                "rmse": float(np.sqrt(squared.mean())) if len(squared.dropna()) else None,
                "sharpe_like": _annualized_sharpe(adjusted, int(horizon_days)),
                "max_drawdown_proxy": float(adjusted.min()) if len(adjusted.dropna()) else None,
                "config_json": _json_text({"cost_bps": float(cost_bps)}),
                "load_ts": pd.Timestamp.utcnow(),
            }
        )
    return pd.DataFrame(rows)


def persist_evaluations(evaluations: pd.DataFrame, summary: pd.DataFrame) -> None:
    ensure_tables()
    if not evaluations.empty:
        upsert_to_db(
            evaluations,
            EVALUATIONS_TABLE,
            unique_keys=["asof_date", "symbol", "model_name", "forecast_horizon_days"],
            timescaledb_column="asof_date",
        )
    if not summary.empty:
        upsert_to_db(
            summary,
            SUMMARY_TABLE,
            unique_keys=["evaluated_at", "model_name", "forecast_horizon_days", "action_hint"],
            timescaledb_column="evaluated_at",
        )


def summarize(evaluations: pd.DataFrame, summary: pd.DataFrame) -> dict[str, Any]:
    return {
        "status": "ok",
        "evaluations_table": EVALUATIONS_TABLE,
        "summary_table": SUMMARY_TABLE,
        "evaluation_rows": int(len(evaluations)),
        "evaluated_rows": int(evaluations["evaluation_status"].eq("evaluated").sum()) if not evaluations.empty else 0,
        "not_matured_rows": int(evaluations["evaluation_status"].eq("not_matured").sum()) if not evaluations.empty else 0,
        "summary_rows": int(len(summary)),
        "summary_sample": summary.head(20).to_dict(orient="records") if not summary.empty else [],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate matured experimental TS forecast rows against future Dhan OHLCV returns.")
    parser.add_argument("--from-date", type=parse_datetime_arg)
    parser.add_argument("--to-date", type=parse_datetime_arg)
    parser.add_argument("--symbols", nargs="*", help="Symbols, comma-separated or repeated")
    parser.add_argument("--model-name", dest="model_names", nargs="*", help="Model names to evaluate")
    parser.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    evaluations = build_forecast_evaluations(
        from_date=_normalize_timestamp(args.from_date),
        to_date=_normalize_timestamp(args.to_date),
        symbols=_as_symbol_list(args.symbols),
        model_names=args.model_names,
        cost_bps=float(args.cost_bps),
    )
    summary = build_evaluation_summary(evaluations, cost_bps=float(args.cost_bps))
    if not args.dry_run:
        persist_evaluations(evaluations, summary)
    result = summarize(evaluations, summary)
    result["dry_run"] = bool(args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
