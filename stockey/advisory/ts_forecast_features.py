from __future__ import annotations

import argparse
import json
import math
import os
from typing import Any, Iterable

import numpy as np
import pandas as pd

from advisory.fallback_telemetry import record_local_fallback_event
from data.dhanlive.ohlcv import sync_many_daily
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration
from utils.sync import load_tracked_symbols, parse_datetime_arg


TABLE_NAME = "advisory_ts_forecasts_daily"
TS_FORECAST_FEATURES_SCHEMA_MIGRATION_ID = "20260611_advisory_ts_forecast_features_base"
TS_FORECAST_FEATURES_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        asof_date TIMESTAMPTZ NOT NULL,
        symbol TEXT NOT NULL,
        model_name TEXT NOT NULL,
        model_version TEXT,
        forecast_horizon_days BIGINT NOT NULL,
        forecast_return DOUBLE PRECISION,
        forecast_price DOUBLE PRECISION,
        downside_return_p10 DOUBLE PRECISION,
        upside_return_p90 DOUBLE PRECISION,
        probability_positive DOUBLE PRECISION,
        realized_volatility_20d DOUBLE PRECISION,
        momentum_return_20d DOUBLE PRECISION,
        momentum_return_60d DOUBLE PRECISION,
        signal_quality DOUBLE PRECISION,
        action_hint TEXT,
        feature_context_json TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (asof_date, symbol, model_name, forecast_horizon_days)
    )
    """,
]
DEFAULT_MODEL_NAME = "naive_momentum_v1"
TIMESFM_MODEL_NAME = "timesfm_2p5_200m"
DEFAULT_TIMESFM_CHECKPOINT = os.getenv("TS_TIMESFM_CHECKPOINT", "google/timesfm-2.5-200m-pytorch")
DEFAULT_HORIZONS = (5, 10, 20)
DEFAULT_LOOKBACK_DAYS = 260
MIN_HISTORY_ROWS = 80


def _record_ts_forecast_feature_fallback(
    *,
    fallback_type: str,
    source: str,
    reason: str,
    error: Exception,
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="advisory.ts_forecast_features",
        fallback_type=fallback_type,
        source=source,
        severity="warn",
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def _normalize_asof_date(value: Any | None) -> pd.Timestamp:
    ts = pd.to_datetime(value or pd.Timestamp.utcnow(), utc=True, errors="coerce")
    if pd.isna(ts):
        raise ValueError(f"Invalid asof date: {value}")
    return ts.normalize()


def _as_symbol_list(symbols: Iterable[str] | None) -> list[str]:
    out: list[str] = []
    for value in symbols or []:
        for part in str(value).split(","):
            symbol = part.strip().upper()
            if symbol:
                out.append(symbol)
    return sorted(set(out))


def resolve_symbol_universe(symbols: list[str] | None) -> list[str]:
    tracked = load_tracked_symbols(symbols)
    if tracked:
        return sorted({str(value).strip().upper() for value in tracked if str(value).strip()})
    try:
        df = sql_to_df(
            """
            SELECT DISTINCT ticker AS symbol
            FROM dhan_ohlcv_daily
            WHERE asset_type = 'stock'
              AND exchange = 'NSE'
            ORDER BY ticker
            """
        )
    except Exception as exc:
        _record_ts_forecast_feature_fallback(
            fallback_type="ts_forecast_features_universe_load_failed",
            source="dhan_ohlcv_daily",
            reason="TS forecast feature builder could not load the default Dhan OHLCV symbol universe.",
            error=exc,
        )
        raise
    if df.empty:
        return []
    return _as_symbol_list(df["symbol"].dropna().astype(str).tolist())


def load_ohlcv_history(
    *,
    symbols: list[str],
    asof_date: pd.Timestamp,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()
    start_date = asof_date - pd.Timedelta(days=max(int(lookback_days), 1) * 2)
    try:
        df = sql_to_df(
            """
            SELECT
                ticker AS symbol,
                date,
                open,
                high,
                low,
                close,
                volume
            FROM dhan_ohlcv_daily
            WHERE ticker = ANY(%s)
              AND asset_type = 'stock'
              AND exchange = 'NSE'
              AND date >= %s
              AND date <= %s
            ORDER BY ticker, date
            """,
            params=(symbols, start_date, asof_date),
        )
    except Exception as exc:
        _record_ts_forecast_feature_fallback(
            fallback_type="ts_forecast_features_ohlcv_load_failed",
            source="dhan_ohlcv_daily",
            reason="TS forecast feature builder could not load Dhan daily OHLCV history.",
            error=exc,
            metadata={
                "symbol_count": len(symbols),
                "from_date": str(start_date),
                "to_date": str(asof_date),
                "lookback_days": int(lookback_days),
            },
        )
        raise
    if df.empty:
        return df
    df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()
    df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce").dt.normalize()
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["symbol", "date", "close"]).drop_duplicates(subset=["symbol", "date"], keep="last")


def _normal_probability_positive(mean_return: float, volatility: float) -> float:
    if not np.isfinite(mean_return):
        return 0.5
    if not np.isfinite(volatility) or volatility <= 0:
        return 1.0 if mean_return > 0 else 0.0 if mean_return < 0 else 0.5
    z = mean_return / volatility
    return float(0.5 * (1.0 + math.erf(z / math.sqrt(2.0))))


def _score_signal_quality(history_rows: int, forecast_return: float, horizon_volatility: float) -> float:
    history_score = min(max((history_rows - MIN_HISTORY_ROWS) / 120.0, 0.0), 1.0)
    signal_score = min(abs(forecast_return) / max(horizon_volatility, 0.01), 1.0)
    return round(float((0.60 * history_score) + (0.40 * signal_score)), 6)


def _action_hint(probability_positive: float, forecast_return: float, signal_quality: float) -> str:
    if signal_quality < 0.35:
        return "EXPERIMENTAL_WEAK"
    if probability_positive >= 0.62 and forecast_return > 0:
        return "EXPERIMENTAL_POSITIVE"
    if probability_positive <= 0.38 and forecast_return < 0:
        return "EXPERIMENTAL_NEGATIVE"
    return "EXPERIMENTAL_NEUTRAL"


def _json_number(value: Any) -> float | None:
    num = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(num) else float(num)


def _build_naive_momentum_rows(
    *,
    history: pd.DataFrame,
    asof_date: pd.Timestamp,
    horizons: tuple[int, ...],
    model_name: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for symbol, group in history.sort_values(["symbol", "date"]).groupby("symbol", sort=True):
        group = group[group["date"].le(asof_date)].tail(DEFAULT_LOOKBACK_DAYS).copy()
        if group.empty:
            continue
        close = group["close"].astype(float)
        returns = close.pct_change()
        latest_close = float(close.iloc[-1])
        history_rows = int(close.notna().sum())
        momentum_20d = float(close.iloc[-1] / close.iloc[-21] - 1.0) if history_rows >= 21 and close.iloc[-21] else np.nan
        momentum_60d = float(close.iloc[-1] / close.iloc[-61] - 1.0) if history_rows >= 61 and close.iloc[-61] else np.nan
        daily_drift_20d = momentum_20d / 20.0 if np.isfinite(momentum_20d) else 0.0
        daily_drift_60d = momentum_60d / 60.0 if np.isfinite(momentum_60d) else 0.0
        daily_vol_20d = float(returns.tail(20).std(ddof=0)) if history_rows >= 21 else np.nan
        latest_volume = float(group["volume"].iloc[-1]) if pd.notna(group["volume"].iloc[-1]) else np.nan
        avg_volume_20d = float(group["volume"].tail(20).mean()) if history_rows >= 20 else np.nan

        for horizon in horizons:
            horizon_int = int(horizon)
            horizon_volatility = float(daily_vol_20d * math.sqrt(horizon_int)) if np.isfinite(daily_vol_20d) else np.nan
            raw_forecast_return = ((0.60 * daily_drift_20d) + (0.40 * daily_drift_60d)) * horizon_int
            forecast_return = float(np.clip(raw_forecast_return, -0.35, 0.35))
            forecast_price = latest_close * (1.0 + forecast_return)
            downside_return_p10 = forecast_return - (1.2816 * horizon_volatility) if np.isfinite(horizon_volatility) else np.nan
            upside_return_p90 = forecast_return + (1.2816 * horizon_volatility) if np.isfinite(horizon_volatility) else np.nan
            probability_positive = _normal_probability_positive(forecast_return, horizon_volatility)
            signal_quality = _score_signal_quality(history_rows, forecast_return, horizon_volatility)
            rows.append(
                {
                    "asof_date": asof_date,
                    "symbol": str(symbol).upper(),
                    "model_name": model_name,
                    "model_version": "v1",
                    "forecast_horizon_days": horizon_int,
                    "forecast_return": round(forecast_return, 8),
                    "forecast_price": round(float(forecast_price), 6),
                    "downside_return_p10": round(float(downside_return_p10), 8) if np.isfinite(downside_return_p10) else None,
                    "upside_return_p90": round(float(upside_return_p90), 8) if np.isfinite(upside_return_p90) else None,
                    "probability_positive": round(float(probability_positive), 8),
                    "realized_volatility_20d": round(float(daily_vol_20d), 8) if np.isfinite(daily_vol_20d) else None,
                    "momentum_return_20d": round(float(momentum_20d), 8) if np.isfinite(momentum_20d) else None,
                    "momentum_return_60d": round(float(momentum_60d), 8) if np.isfinite(momentum_60d) else None,
                    "signal_quality": signal_quality,
                    "action_hint": _action_hint(probability_positive, forecast_return, signal_quality),
                    "feature_context_json": json.dumps(
                        {
                            "source": "dhan_ohlcv_daily",
                            "latest_close": _json_number(latest_close),
                            "latest_volume": _json_number(latest_volume),
                            "avg_volume_20d": _json_number(avg_volume_20d),
                            "history_rows": history_rows,
                            "notes": "Experimental forecast feature only; not a standalone trading action.",
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    "load_ts": pd.Timestamp.utcnow(),
                }
            )
    return rows


def _load_timesfm_model(*, max_context: int, max_horizon: int):
    try:
        import torch
        import timesfm
    except ImportError as exc:
        raise RuntimeError(
            "TimesFM is not installed. Install the optional dependency with "
            "`pip install 'timesfm[torch]' torch` or install google-research/timesfm with the torch extra."
        ) from exc

    torch.set_float32_matmul_precision("high")
    model = timesfm.TimesFM_2p5_200M_torch.from_pretrained(DEFAULT_TIMESFM_CHECKPOINT)
    model.compile(
        timesfm.ForecastConfig(
            max_context=max_context,
            max_horizon=max_horizon,
            normalize_inputs=True,
            use_continuous_quantile_head=True,
            force_flip_invariance=True,
            infer_is_positive=True,
            fix_quantile_crossing=True,
        )
    )
    return model


def _build_timesfm_rows(
    *,
    history: pd.DataFrame,
    asof_date: pd.Timestamp,
    horizons: tuple[int, ...],
    model_name: str,
    lookback_days: int,
) -> list[dict[str, Any]]:
    horizon_values = tuple(sorted({int(value) for value in horizons if int(value) > 0}))
    if not horizon_values:
        return []
    max_horizon = max(horizon_values)
    series_meta: list[dict[str, Any]] = []
    inputs: list[np.ndarray] = []
    for symbol, group in history.sort_values(["symbol", "date"]).groupby("symbol", sort=True):
        group = group[group["date"].le(asof_date)].tail(max(int(lookback_days), MIN_HISTORY_ROWS)).copy()
        close = pd.to_numeric(group["close"], errors="coerce").dropna()
        if len(close) < MIN_HISTORY_ROWS:
            continue
        input_values = close.astype(float).to_numpy()
        inputs.append(input_values)
        latest_volume = pd.to_numeric(group["volume"], errors="coerce").dropna().iloc[-1] if pd.to_numeric(group["volume"], errors="coerce").dropna().size else np.nan
        avg_volume_20d = pd.to_numeric(group["volume"], errors="coerce").tail(20).mean()
        returns = close.pct_change()
        series_meta.append(
            {
                "symbol": str(symbol).upper(),
                "latest_close": float(close.iloc[-1]),
                "history_rows": int(len(close)),
                "latest_volume": _json_number(latest_volume),
                "avg_volume_20d": _json_number(avg_volume_20d),
                "realized_volatility_20d": _json_number(returns.tail(20).std(ddof=0)),
                "momentum_return_20d": _json_number(close.iloc[-1] / close.iloc[-21] - 1.0) if len(close) >= 21 and close.iloc[-21] else None,
                "momentum_return_60d": _json_number(close.iloc[-1] / close.iloc[-61] - 1.0) if len(close) >= 61 and close.iloc[-61] else None,
            }
        )
    if not inputs:
        return []

    model = _load_timesfm_model(
        max_context=min(max(int(lookback_days), MIN_HISTORY_ROWS), 16_000),
        max_horizon=max_horizon,
    )
    point_forecast, quantile_forecast = model.forecast(horizon=max_horizon, inputs=inputs)
    point_array = np.asarray(point_forecast, dtype=float)
    quantile_array = np.asarray(quantile_forecast, dtype=float) if quantile_forecast is not None else np.empty((0,))

    rows: list[dict[str, Any]] = []
    for idx, meta in enumerate(series_meta):
        latest_close = float(meta["latest_close"])
        for horizon in horizon_values:
            horizon_idx = int(horizon) - 1
            forecast_price = float(point_array[idx, horizon_idx])
            forecast_return = (forecast_price / latest_close) - 1.0 if latest_close else np.nan
            downside = None
            upside = None
            if quantile_array.ndim == 3 and quantile_array.shape[0] > idx and quantile_array.shape[1] > horizon_idx:
                horizon_quantiles = quantile_array[idx, horizon_idx]
                if len(horizon_quantiles) >= 10:
                    downside = (float(horizon_quantiles[1]) / latest_close) - 1.0 if latest_close else None
                    upside = (float(horizon_quantiles[-1]) / latest_close) - 1.0 if latest_close else None
            horizon_volatility = abs((upside or forecast_return) - (downside or forecast_return)) / 2.5632
            probability_positive = _normal_probability_positive(float(forecast_return), float(horizon_volatility))
            signal_quality = _score_signal_quality(
                int(meta["history_rows"]),
                float(forecast_return),
                float(horizon_volatility),
            )
            rows.append(
                {
                    "asof_date": asof_date,
                    "symbol": meta["symbol"],
                    "model_name": model_name,
                    "model_version": "2.5-200m",
                    "forecast_horizon_days": int(horizon),
                    "forecast_return": round(float(forecast_return), 8),
                    "forecast_price": round(float(forecast_price), 6),
                    "downside_return_p10": None if downside is None else round(float(downside), 8),
                    "upside_return_p90": None if upside is None else round(float(upside), 8),
                    "probability_positive": round(float(probability_positive), 8),
                    "realized_volatility_20d": meta["realized_volatility_20d"],
                    "momentum_return_20d": meta["momentum_return_20d"],
                    "momentum_return_60d": meta["momentum_return_60d"],
                    "signal_quality": signal_quality,
                    "action_hint": _action_hint(probability_positive, float(forecast_return), signal_quality),
                    "feature_context_json": json.dumps(
                        {
                            "source": "dhan_ohlcv_daily",
                            "model_checkpoint": DEFAULT_TIMESFM_CHECKPOINT,
                            "latest_close": _json_number(latest_close),
                            "latest_volume": meta["latest_volume"],
                            "avg_volume_20d": meta["avg_volume_20d"],
                            "history_rows": meta["history_rows"],
                            "notes": "Experimental TimesFM forecast feature only; not a standalone trading action.",
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    "load_ts": pd.Timestamp.utcnow(),
                }
            )
    return rows


def build_ts_forecasts(
    *,
    symbols: list[str] | None = None,
    asof_date: pd.Timestamp | None = None,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    model_name: str = DEFAULT_MODEL_NAME,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    history: pd.DataFrame | None = None,
) -> pd.DataFrame:
    monitor_date = _normalize_asof_date(asof_date)
    symbol_universe = _as_symbol_list(symbols) if symbols else resolve_symbol_universe(None)
    if history is None:
        history = load_ohlcv_history(symbols=symbol_universe, asof_date=monitor_date, lookback_days=lookback_days)
    if history.empty:
        return pd.DataFrame()
    model_key = str(model_name or DEFAULT_MODEL_NAME).strip().lower()
    if model_key == DEFAULT_MODEL_NAME:
        rows = _build_naive_momentum_rows(
            history=history,
            asof_date=monitor_date,
            horizons=tuple(sorted({int(value) for value in horizons if int(value) > 0})),
            model_name=model_key,
        )
    elif model_key in {TIMESFM_MODEL_NAME, "timesfm"}:
        rows = _build_timesfm_rows(
            history=history,
            asof_date=monitor_date,
            horizons=tuple(sorted({int(value) for value in horizons if int(value) > 0})),
            model_name=TIMESFM_MODEL_NAME,
            lookback_days=lookback_days,
        )
    else:
        raise ValueError(f"Unsupported ts forecast model for this runner: {model_name}")
    return pd.DataFrame(rows)


def ensure_ts_forecast_table() -> None:
    apply_schema_migration(
        migration_id=TS_FORECAST_FEATURES_SCHEMA_MIGRATION_ID,
        description="Create advisory TS forecast feature table.",
        statements=TS_FORECAST_FEATURES_SCHEMA_STATEMENTS,
        metadata={"tables": [TABLE_NAME], "workflow": "ts_forecast_features"},
    )


def persist_ts_forecasts(df: pd.DataFrame) -> None:
    ensure_ts_forecast_table()
    if df.empty:
        return
    upsert_to_db(
        df,
        TABLE_NAME,
        unique_keys=["asof_date", "symbol", "model_name", "forecast_horizon_days"],
        timescaledb_column="asof_date",
    )


def summarize(df: pd.DataFrame) -> dict[str, Any]:
    if df.empty:
        return {
            "status": "ok",
            "table": TABLE_NAME,
            "row_count": 0,
            "symbol_count": 0,
            "horizons": [],
            "sample": [],
        }
    return {
        "status": "ok",
        "table": TABLE_NAME,
        "row_count": int(len(df)),
        "symbol_count": int(df["symbol"].nunique()),
        "horizons": sorted(int(value) for value in df["forecast_horizon_days"].dropna().unique().tolist()),
        "action_hint_counts": df["action_hint"].value_counts(dropna=False).to_dict(),
        "sample": df.head(10).to_dict(orient="records"),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build experimental time-series forecast features from Dhan OHLCV.")
    parser.add_argument("--symbols", nargs="*", help="Symbols, comma-separated or repeated")
    parser.add_argument("--date", type=parse_datetime_arg, help="Forecast asof date in YYYY-MM-DD")
    parser.add_argument("--horizons", nargs="*", type=int, default=list(DEFAULT_HORIZONS), help="Forecast horizons in trading days")
    parser.add_argument("--model-name", default=DEFAULT_MODEL_NAME, help="Forecast model adapter. Currently: naive_momentum_v1")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--refresh-ohlcv", action="store_true", help="Refresh Dhan daily OHLCV for selected symbols before forecasting")
    parser.add_argument("--refresh-from-date", type=parse_datetime_arg, help="Optional Dhan daily OHLCV refresh start date")
    parser.add_argument("--refresh-to-date", type=parse_datetime_arg, help="Optional Dhan daily OHLCV refresh end date")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    symbol_universe = resolve_symbol_universe(args.symbols)
    refresh_result = None
    if bool(args.refresh_ohlcv) and symbol_universe and not bool(args.dry_run):
        try:
            refresh_result = sync_many_daily(
                symbol_universe,
                exchange="NSE",
                asset_type="stock",
                from_date=args.refresh_from_date,
                to_date=args.refresh_to_date or args.date,
            )
        except Exception as exc:
            _record_ts_forecast_feature_fallback(
                fallback_type="ts_forecast_features_ohlcv_refresh_failed",
                source="dhan_ohlcv_daily",
                reason="TS forecast feature builder could not refresh Dhan daily OHLCV before forecasting.",
                error=exc,
                metadata={"symbol_count": len(symbol_universe)},
            )
            raise
    df = build_ts_forecasts(
        symbols=symbol_universe,
        asof_date=pd.Timestamp(args.date, tz="UTC") if args.date else None,
        horizons=tuple(args.horizons),
        model_name=args.model_name,
        lookback_days=int(args.lookback_days),
    )
    if not args.dry_run:
        persist_ts_forecasts(df)
    result = summarize(df)
    result["dry_run"] = bool(args.dry_run)
    result["refresh_ohlcv"] = refresh_result
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
