from __future__ import annotations

import argparse
import json
from typing import Any

import numpy as np
import pandas as pd
import talib

from advisory.data_sync import ensure_advisory_symbol_inputs
from advisory.fallback_telemetry import record_local_fallback_event
from advisory.peer_sync import sync_peer_data
from features.tutils import get_max_date
from utils.company_master import map_company_master_ids
import os

from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration
from utils.sync import load_tracked_symbols, parse_datetime_arg


# Gap-fill daily price history from the whole-market bhavcopy where Dhan daily is missing, so
# newly-admitted dynamic names get features on their admission day (default on).
_BHAVCOPY_TECHNICAL_FALLBACK_ENABLED = os.getenv(
    "TECHNICAL_BHAVCOPY_FALLBACK_ENABLED", "true"
).strip().lower() not in {"0", "false", "no"}

TABLE_NAME = "advisory_technical_daily"
REFRESH_STATUS_TABLE = "advisory_technical_feature_refresh_status"
TECHNICAL_REFRESH_STATUS_SCHEMA_MIGRATION_ID = "20260622_advisory_technical_feature_refresh_status"
TECHNICAL_STOCK_RET_60D_SCHEMA_MIGRATION_ID = "20260622_advisory_technical_daily_stock_ret_60d"
DEFAULT_BENCHMARK_NAME = "NIFTY"
LOOKBACK_BUFFER_DAYS = 400
MIN_AVG_TRADED_VALUE_20D = 1_00_00_000.0
MAX_BREAKOUT_EXTENSION_PCT = 10.0
MIN_SECTOR_PEER_COUNT = 3
MAX_GAP_FREQ_60D = 0.15
TECHNICAL_REFRESH_STATUS_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {REFRESH_STATUS_TABLE} (
        asof_date TIMESTAMPTZ NOT NULL,
        symbol TEXT NOT NULL,
        stage TEXT NOT NULL,
        status TEXT NOT NULL,
        action TEXT,
        reason TEXT,
        rows BIGINT,
        error_type TEXT,
        error_text TEXT,
        from_date TIMESTAMPTZ,
        to_date TIMESTAMPTZ,
        raw_json TEXT,
        load_ts TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (asof_date, symbol, stage)
    )
    """,
    f"ALTER TABLE {REFRESH_STATUS_TABLE} ADD COLUMN IF NOT EXISTS raw_json TEXT",
    f"""
    CREATE INDEX IF NOT EXISTS idx_{REFRESH_STATUS_TABLE}_symbol_asof
        ON {REFRESH_STATUS_TABLE} (UPPER(TRIM(symbol)), asof_date DESC)
    """,
    f"""
    CREATE INDEX IF NOT EXISTS idx_{REFRESH_STATUS_TABLE}_status_asof
        ON {REFRESH_STATUS_TABLE} (status, asof_date DESC)
    """,
]
TECHNICAL_STOCK_RET_60D_SCHEMA_STATEMENTS = [
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS stock_ret_60d DOUBLE PRECISION",
]
# Cross-sectional relative-strength percentile (T1 2026-07-14): the 60/120d cross-sectional return RANK
# out-predicts the engine's 20d rs_vs_benchmark and is the signal that drives the paper loop's edge.
# Persisted so score_relative_strength can consume it under a reviewed default-OFF flag. Own migration id.
TECHNICAL_RS_PERCENTILE_SCHEMA_MIGRATION_ID = "20260714_advisory_technical_daily_rs_percentile"
TECHNICAL_RS_PERCENTILE_SCHEMA_STATEMENTS = [
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS rs_percentile DOUBLE PRECISION",
]


def _record_technical_features_fallback(
    *,
    fallback_type: str,
    source: str,
    reason: str,
    error: Exception,
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="advisory.technical_features",
        fallback_type=fallback_type,
        source=source,
        severity="warn",
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def ensure_refresh_status_table() -> None:
    apply_schema_migration(
        migration_id=TECHNICAL_REFRESH_STATUS_SCHEMA_MIGRATION_ID,
        description="Create technical feature input/build refresh status table.",
        statements=TECHNICAL_REFRESH_STATUS_SCHEMA_STATEMENTS,
        metadata={"module": "advisory.technical_features", "tables": [REFRESH_STATUS_TABLE]},
    )


def ensure_technical_feature_schema() -> None:
    apply_schema_migration(
        migration_id=TECHNICAL_STOCK_RET_60D_SCHEMA_MIGRATION_ID,
        description="Add persisted 60-day stock return for technical relative-strength scoring.",
        statements=TECHNICAL_STOCK_RET_60D_SCHEMA_STATEMENTS,
        metadata={"module": "advisory.technical_features", "tables": [TABLE_NAME]},
    )
    apply_schema_migration(
        migration_id=TECHNICAL_RS_PERCENTILE_SCHEMA_MIGRATION_ID,
        description="Add cross-sectional RS percentile (60/120d rank) for the reviewed RS sub-score swap.",
        statements=TECHNICAL_RS_PERCENTILE_SCHEMA_STATEMENTS,
        metadata={"module": "advisory.technical_features", "tables": [TABLE_NAME]},
    )


def _normalize_asof(value: Any | None) -> pd.Timestamp:
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        ts = pd.Timestamp.utcnow()
    return ts.normalize()


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def normalize_timestamp(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce").dt.normalize()


def resolve_symbol_universe(symbols: list[str] | None) -> list[str]:
    values = load_tracked_symbols(symbols)
    if values:
        return values
    try:
        df = sql_to_df(
            """
            SELECT DISTINCT ticker AS symbol
            FROM dhan_ohlcv_daily
            WHERE asset_type = 'stock'
              AND exchange = 'NSE'
            ORDER BY symbol
            """
        )
    except Exception as exc:
        _record_technical_features_fallback(
            fallback_type="technical_features_universe_load_failed",
            source="dhan_ohlcv_daily",
            reason="Technical feature builder could not load the default stock universe from Dhan daily OHLCV.",
            error=exc,
            metadata={"provided_symbol_count": 0 if symbols is None else len(symbols)},
        )
        raise
    if df.empty:
        return []
    return (
        df["symbol"]
        .astype("string")
        .dropna()
        .str.strip()
        .str.upper()
        .drop_duplicates()
        .tolist()
    )


def load_benchmark_series(
    *,
    benchmark_name: str = DEFAULT_BENCHMARK_NAME,
    start_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
) -> pd.DataFrame:
    if benchmark_name.upper() == "NIFTY":
        fallback_params: dict[str, object] = {"benchmark_name": "Nifty 50"}
        fallback_clauses = ["index_name = %(benchmark_name)s"]
        if start_date is not None:
            fallback_clauses.append("date >= %(start_date)s")
            fallback_params["start_date"] = start_date
        if to_date is not None:
            fallback_clauses.append("date <= %(to_date)s")
            fallback_params["to_date"] = to_date
        try:
            nse_df = sql_to_df(
                f"""
                SELECT index_name AS ticker, date, close
                FROM nseindia_indices
                WHERE {' AND '.join(fallback_clauses)}
                ORDER BY date
                """,
                params=fallback_params,
            )
        except Exception as exc:
            _record_technical_features_fallback(
                fallback_type="technical_features_nse_benchmark_load_failed",
                source="nseindia_indices",
                reason="Technical feature builder could not load the preferred NSE NIFTY benchmark and will try Dhan benchmark rows.",
                error=exc,
                metadata={
                    "benchmark_name": benchmark_name,
                    "start_date": str(start_date) if start_date is not None else None,
                    "to_date": str(to_date) if to_date is not None else None,
                },
            )
            nse_df = pd.DataFrame()
        if not nse_df.empty:
            df = nse_df
            df["date"] = normalize_timestamp(df["date"])
            df["benchmark_close"] = pd.to_numeric(df["close"], errors="coerce")
            df["benchmark_ret_20d"] = df["benchmark_close"].pct_change(20)
            return df[["date", "benchmark_close", "benchmark_ret_20d"]]

    clauses = ["ticker = %(benchmark_name)s", "asset_type = 'benchmark'"]
    params: dict[str, object] = {"benchmark_name": benchmark_name}
    if start_date is not None:
        clauses.append("date >= %(start_date)s")
        params["start_date"] = start_date
    if to_date is not None:
        clauses.append("date <= %(to_date)s")
        params["to_date"] = to_date
    try:
        df = sql_to_df(
            f"""
            SELECT ticker, date, close
            FROM dhan_ohlcv_daily
            WHERE {' AND '.join(clauses)}
            ORDER BY date
            """,
            params=params,
        )
    except Exception as exc:
        _record_technical_features_fallback(
            fallback_type="technical_features_dhan_benchmark_load_failed",
            source="dhan_ohlcv_daily",
            reason="Technical feature builder could not load benchmark rows from Dhan daily OHLCV.",
            error=exc,
            metadata={
                "benchmark_name": benchmark_name,
                "start_date": str(start_date) if start_date is not None else None,
                "to_date": str(to_date) if to_date is not None else None,
            },
        )
        raise
    if df.empty:
        return df
    df["date"] = normalize_timestamp(df["date"])
    df["benchmark_close"] = pd.to_numeric(df["close"], errors="coerce")
    df["benchmark_ret_20d"] = df["benchmark_close"].pct_change(20)
    return df[["date", "benchmark_close", "benchmark_ret_20d"]]


def load_price_history(
    *,
    symbols: list[str],
    start_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()

    clauses = ["ticker = ANY(%s)", "asset_type = 'stock'", "exchange = 'NSE'"]
    params: list[object] = [symbols]
    if start_date is not None:
        clauses.append("date >= %s")
        params.append(start_date)
    if to_date is not None:
        clauses.append("date <= %s")
        params.append(to_date)

    # bhavcopy clauses (nseindia_ohlcv uses `symbol`, series='EQ', no security_id)
    bhav_clauses = ["UPPER(TRIM(symbol)) = ANY(%s)", "series = 'EQ'"]
    bhav_params: list[object] = [[str(s).strip().upper() for s in symbols]]
    if start_date is not None:
        bhav_clauses.append("date >= %s")
        bhav_params.append(start_date)
    if to_date is not None:
        bhav_clauses.append("date <= %s")
        bhav_params.append(to_date)
    try:
        df = sql_to_df(
            f"""
            SELECT
                ticker AS symbol,
                'EQ' AS series,
                security_id,
                NULL::text AS isin,
                date,
                open AS adj_open,
                high AS adj_high,
                low AS adj_low,
                close AS adj_close,
                volume,
                close * volume AS total_value
            FROM dhan_ohlcv_daily
            WHERE {' AND '.join(clauses)}
            ORDER BY ticker, date
            """,
            params=tuple(params),
        )
    except Exception as exc:
        _record_technical_features_fallback(
            fallback_type="technical_features_price_history_load_failed",
            source="dhan_ohlcv_daily",
            reason="Technical feature builder could not load stock OHLCV history.",
            error=exc,
            metadata={
                "symbol_count": len(symbols),
                "start_date": str(start_date) if start_date is not None else None,
                "to_date": str(to_date) if to_date is not None else None,
            },
        )
        raise
    # Gap-fill from the whole-market bhavcopy: dhan_ohlcv_daily only covers already-synced
    # universe symbols, so newly-admitted dynamic names (scan/surge/hypothesis/theme) have
    # no Dhan bars on their admission day and can never get features in time. nseindia_ohlcv
    # is complete for every NSE name the day it publishes; fill only the (symbol, date) pairs
    # Dhan lacks (Dhan stays primary where present, preserving any adjustments).
    if _BHAVCOPY_TECHNICAL_FALLBACK_ENABLED:
        try:
            bhav = sql_to_df(
                f"""
                SELECT
                    UPPER(TRIM(symbol)) AS symbol, 'EQ' AS series,
                    NULL::bigint AS security_id, NULL::text AS isin, date,
                    open AS adj_open, high AS adj_high, low AS adj_low, close AS adj_close,
                    volume, close * volume AS total_value
                FROM nseindia_ohlcv
                WHERE {' AND '.join(bhav_clauses)}
                ORDER BY symbol, date
                """,
                params=tuple(bhav_params),
            )
        except Exception as exc:
            _record_technical_features_fallback(
                fallback_type="technical_features_bhavcopy_fallback_failed",
                source="nseindia_ohlcv",
                reason="Bhavcopy gap-fill for technical features failed; using Dhan coverage only.",
                error=exc,
                metadata={"symbol_count": len(symbols)},
            )
            bhav = pd.DataFrame()
        if not bhav.empty:
            if df.empty:
                df = bhav
            else:
                have = set(
                    zip(
                        df["symbol"].astype("string").str.strip().str.upper(),
                        pd.to_datetime(df["date"], errors="coerce").dt.normalize(),
                    )
                )
                bkey = list(
                    zip(
                        bhav["symbol"].astype("string").str.strip().str.upper(),
                        pd.to_datetime(bhav["date"], errors="coerce").dt.normalize(),
                    )
                )
                fill = bhav[[k not in have for k in bkey]]
                if not fill.empty:
                    df = pd.concat([df, fill], ignore_index=True)
    if df.empty:
        return df
    df["date"] = normalize_timestamp(df["date"])
    numeric_cols = [
        "adj_open",
        "adj_high",
        "adj_low",
        "adj_close",
        "volume",
        "total_value",
    ]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()
    return df


def load_sector_mapping(symbols: list[str]) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame(columns=["symbol", "sector_code"])
    try:
        df = sql_to_df(
            """
            SELECT symbol, sector_code
            FROM master_sharpely_equity
            WHERE symbol = ANY(%s)
            """,
            params=(symbols,),
        )
    except Exception as exc:
        _record_technical_features_fallback(
            fallback_type="technical_features_sector_mapping_load_failed",
            source="master_sharpely_equity",
            reason="Technical feature builder could not load sector mapping for RS-vs-sector features.",
            error=exc,
            metadata={"symbol_count": len(symbols)},
        )
        raise
    if df.empty:
        return df
    df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()
    df["sector_code"] = df["sector_code"].astype("string").str.strip()
    return df.drop_duplicates(subset=["symbol"], keep="last")


def load_latest_peer_memberships(anchor_symbols: list[str]) -> pd.DataFrame:
    if not anchor_symbols:
        return pd.DataFrame()
    try:
        df = sql_to_df(
            """
            WITH sector_counts AS (
                SELECT
                    UPPER(TRIM(symbol)) AS symbol,
                    TRIM(sector_code) AS sector_code,
                    COUNT(*) AS row_count
                FROM master_sharpely_equity
                WHERE NULLIF(TRIM(symbol), '') IS NOT NULL
                  AND NULLIF(TRIM(sector_code), '') IS NOT NULL
                  AND UPPER(TRIM(symbol)) = ANY(%s)
                GROUP BY UPPER(TRIM(symbol)), TRIM(sector_code)
            ),
            ranked AS (
                SELECT
                    symbol,
                    sector_code,
                    ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY row_count DESC, sector_code) AS rn
                FROM sector_counts
            ),
            anchors AS (
                SELECT symbol AS anchor_symbol, sector_code
                FROM ranked
                WHERE rn = 1
            )
            SELECT
                anchors.anchor_symbol,
                ranked.symbol AS peer_symbol,
                anchors.sector_code,
                NULL::text AS industry_code,
                NULL::text AS nse_basic_ind_code
            FROM anchors
            JOIN ranked
              ON ranked.rn = 1
             AND ranked.sector_code = anchors.sector_code
             AND ranked.symbol <> anchors.anchor_symbol
            ORDER BY anchors.anchor_symbol, ranked.symbol
            """,
            params=(anchor_symbols,),
        )
    except Exception as exc:
        _record_technical_features_fallback(
            fallback_type="technical_features_peer_membership_load_failed",
            source="master_sharpely_equity",
            reason="Technical feature builder could not load peer memberships for RS-vs-sector features.",
            error=exc,
            metadata={"anchor_symbol_count": len(anchor_symbols)},
        )
        raise
    if df.empty:
        return df
    df["anchor_symbol"] = df["anchor_symbol"].astype("string").str.upper()
    df["peer_symbol"] = df["peer_symbol"].astype("string").str.upper()
    return df.drop_duplicates(subset=["anchor_symbol", "peer_symbol"], keep="last")


def load_peer_ohlcv(
    peer_symbols: list[str],
    *,
    start_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
) -> pd.DataFrame:
    if not peer_symbols:
        return pd.DataFrame()
    clauses = ["ticker = ANY(%s)", "asset_type = 'stock'", "exchange = 'NSE'"]
    params: list[object] = [peer_symbols]
    if start_date is not None:
        clauses.append("date >= %s")
        params.append(start_date)
    if to_date is not None:
        clauses.append("date <= %s")
        params.append(to_date)
    try:
        df = sql_to_df(
            f"""
            SELECT ticker, date, close
            FROM dhan_ohlcv_daily
            WHERE {' AND '.join(clauses)}
            ORDER BY ticker, date
            """,
            params=tuple(params),
        )
    except Exception as exc:
        _record_technical_features_fallback(
            fallback_type="technical_features_peer_ohlcv_load_failed",
            source="dhan_ohlcv_daily",
            reason="Technical feature builder could not load peer OHLCV for RS-vs-sector features.",
            error=exc,
            metadata={
                "peer_symbol_count": len(peer_symbols),
                "start_date": str(start_date) if start_date is not None else None,
                "to_date": str(to_date) if to_date is not None else None,
            },
        )
        raise
    if df.empty:
        return df
    df["date"] = normalize_timestamp(df["date"])
    df["ticker"] = df["ticker"].astype("string").str.upper()
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["peer_ret_20d"] = df.groupby("ticker", dropna=False)["close"].pct_change(20)
    return df


def choose_effective_start(
    from_date: pd.Timestamp | None,
    *,
    rebuild: bool,
) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    if rebuild:
        effective_from = from_date
    else:
        max_date = get_max_date(TABLE_NAME, "asof_date")
        effective_from = from_date or (
            max_date + pd.Timedelta(days=1) if max_date is not None else None
        )
    if effective_from is None:
        return None, None
    history_start = effective_from - pd.Timedelta(days=LOOKBACK_BUFFER_DAYS)
    return effective_from, history_start


def compute_group_features(group: pd.DataFrame) -> pd.DataFrame:
    group = group.sort_values("date").copy()
    close = group["adj_close"].astype(float).to_numpy()
    high = group["adj_high"].astype(float).to_numpy()
    low = group["adj_low"].astype(float).to_numpy()
    open_ = group["adj_open"].astype(float)
    volume = group["volume"].astype(float)
    traded_value = group["total_value"].astype(float)
    close_series = group["adj_close"].astype(float)
    high_series = group["adj_high"].astype(float)
    low_series = group["adj_low"].astype(float)
    close_nonzero = close_series.replace(0, np.nan)
    volume_nonzero = volume.replace(0, np.nan)
    traded_value_nonzero = traded_value.replace(0, np.nan)

    group["dma_20"] = talib.SMA(close, timeperiod=20)
    group["dma_50"] = talib.SMA(close, timeperiod=50)
    group["dma_150"] = talib.SMA(close, timeperiod=150)
    group["dma_200"] = talib.SMA(close, timeperiod=200)
    group["atr_20"] = talib.ATR(high, low, close, timeperiod=20)
    group["atr_pct"] = (pd.Series(group["atr_20"], index=group.index) / close_nonzero) * 100.0

    for period in [20, 50, 150]:
        dma_col = f"dma_{period}"
        dma_series = pd.Series(group[dma_col], index=group.index, dtype="float64")
        group[f"{dma_col}_slope_20d_pct"] = ((dma_series / dma_series.shift(20)) - 1.0) * 100.0

    bb_upper, bb_middle, bb_lower = talib.BBANDS(
        close,
        timeperiod=20,
        nbdevup=2,
        nbdevdn=2,
        matype=0,
    )
    group["bb_upper"] = bb_upper
    group["bb_middle"] = bb_middle
    group["bb_lower"] = bb_lower
    group["bb_width"] = np.where(
        pd.Series(bb_middle).replace(0, np.nan).notna(),
        (pd.Series(bb_upper) - pd.Series(bb_lower)) / pd.Series(bb_middle),
        np.nan,
    )
    group["bb_width_rank_252d"] = group["bb_width"].rolling(252, min_periods=60).rank(pct=True)

    atr_series = pd.Series(group["atr_20"], index=group.index, dtype="float64")
    group["atr_compression_pct"] = atr_series.rolling(252, min_periods=50).rank(pct=True)

    high_20 = group["adj_high"].rolling(20, min_periods=20).max()
    high_50 = group["adj_high"].rolling(50, min_periods=50).max()
    high_60 = group["adj_high"].rolling(60, min_periods=40).max()
    high_252 = group["adj_high"].rolling(252, min_periods=100).max()
    low_20 = group["adj_low"].rolling(20, min_periods=20).min()
    low_60 = group["adj_low"].rolling(60, min_periods=40).min()
    prev_high_20 = high_20.shift(1)
    prev_high_60 = high_60.shift(1)

    group["dist_20d_high"] = (group["adj_close"] / high_20 - 1.0) * 100.0
    group["dist_50d_high"] = (group["adj_close"] / high_50 - 1.0) * 100.0
    group["dist_52w_high"] = (group["adj_close"] / high_252 - 1.0) * 100.0
    group["breakout_extension_pct"] = (group["adj_close"] / prev_high_20 - 1.0) * 100.0
    group["base_depth_20d_pct"] = ((high_20 - low_20) / high_20.replace(0, np.nan)) * 100.0
    group["base_depth_60d_pct"] = ((high_60 - low_60) / high_60.replace(0, np.nan)) * 100.0
    group["pivot_distance_20d_pct"] = ((prev_high_20 - close_series) / prev_high_20.replace(0, np.nan)) * 100.0
    group["pivot_distance_60d_pct"] = ((prev_high_60 - close_series) / prev_high_60.replace(0, np.nan)) * 100.0

    group["avg_traded_value_20d"] = traded_value.rolling(20, min_periods=20).mean()
    group["avg_traded_value_60d"] = traded_value.rolling(60, min_periods=40).mean()
    group["median_volume_20d"] = volume.rolling(20, min_periods=20).median()
    group["median_volume_60d"] = volume.rolling(60, min_periods=40).median()
    group["stock_ret_20d"] = group["adj_close"].pct_change(20)
    group["stock_ret_60d"] = group["adj_close"].pct_change(60)
    group["stock_ret_120d"] = group["adj_close"].pct_change(120)

    tr_pct = ((high_series - low_series) / close_nonzero) * 100.0
    group["daily_range_pct"] = tr_pct
    group["range_contraction_20d_pct"] = tr_pct.rolling(20, min_periods=20).mean()
    group["range_contraction_60d_pct"] = tr_pct.rolling(60, min_periods=40).mean()
    group["range_contraction_ratio"] = (
        group["range_contraction_20d_pct"] / group["range_contraction_60d_pct"].replace(0, np.nan)
    )

    close_location_pct = (close_series - low_series) / (high_series - low_series).replace(0, np.nan)
    group["close_location_pct"] = close_location_pct.clip(lower=0.0, upper=1.0)
    group["tight_close_upper_half_20d"] = group["close_location_pct"].rolling(20, min_periods=20).mean()
    group["tight_close_upper_half_60d"] = group["close_location_pct"].rolling(60, min_periods=40).mean()

    higher_high = high_series.gt(high_series.shift(1))
    higher_low = low_series.gt(low_series.shift(1))
    group["higher_high_count_20d"] = higher_high.rolling(20, min_periods=20).sum()
    group["higher_low_count_20d"] = higher_low.rolling(20, min_periods=20).sum()
    group["trend_persistence_20d"] = ((close_series > group["dma_50"]) & (group["dma_50"] > group["dma_150"])).rolling(20, min_periods=20).mean()
    group["trend_persistence_60d"] = ((close_series > group["dma_50"]) & (group["dma_50"] > group["dma_150"])).rolling(60, min_periods=40).mean()
    group["trend_persistence_120d"] = ((close_series > group["dma_50"]) & (group["dma_50"] > group["dma_150"])).rolling(120, min_periods=80).mean()

    breakout_day = close_series.gt(prev_high_20.fillna(np.inf))
    group["breakout_day_volume_vs_20d"] = np.where(
        breakout_day,
        volume / volume.rolling(20, min_periods=20).mean().replace(0, np.nan),
        np.nan,
    )
    up_day = close_series.gt(close_series.shift(1))
    down_day = close_series.lt(close_series.shift(1))
    group["up_volume_20d"] = volume.where(up_day).rolling(20, min_periods=20).sum()
    group["down_volume_20d"] = volume.where(down_day).rolling(20, min_periods=20).sum()
    group["up_down_volume_ratio_20d"] = group["up_volume_20d"] / group["down_volume_20d"].replace(0, np.nan)
    distribution_day = down_day & (volume > volume.rolling(20, min_periods=20).mean()) & (close_location_pct < 0.4)
    accumulation_day = up_day & (volume > volume.rolling(20, min_periods=20).mean()) & (close_location_pct > 0.6)
    group["distribution_days_20d"] = distribution_day.rolling(20, min_periods=20).sum()
    group["accumulation_days_20d"] = accumulation_day.rolling(20, min_periods=20).sum()
    group["pullback_volume_dryup_ratio_20d"] = (
        volume.where(down_day).rolling(10, min_periods=5).mean() / volume.rolling(20, min_periods=20).mean().replace(0, np.nan)
    )

    gap_pct = ((open_ / close_series.shift(1).replace(0, np.nan)) - 1.0).abs() * 100.0
    group["gap_pct"] = gap_pct
    gap_flag = gap_pct >= 3.0
    group["gap_frequency_60d"] = gap_flag.rolling(60, min_periods=40).mean()

    group["support_distance_20d_pct"] = ((close_series - low_20) / close_nonzero) * 100.0
    support_touch = low_series.le(group["dma_20"] * 1.01) | low_series.le(group["dma_50"] * 1.01)
    support_hold = support_touch & close_location_pct.ge(0.5)
    group["support_hold_rate_20d"] = support_hold.rolling(20, min_periods=20).mean()
    group["volatility_contraction_flag"] = (
        group["range_contraction_ratio"].lt(0.85)
        & group["atr_compression_pct"].lt(0.4)
        & group["bb_width_rank_252d"].lt(0.4)
    )

    group["pass_above_dma_20"] = group["adj_close"] >= group["dma_20"]
    group["pass_above_dma_50"] = group["adj_close"] >= group["dma_50"]
    group["pass_above_dma_150"] = group["adj_close"] >= group["dma_150"]
    group["pass_above_dma_200"] = group["adj_close"] >= group["dma_200"]
    group["pass_liquidity_20d"] = group["avg_traded_value_20d"] >= MIN_AVG_TRADED_VALUE_20D
    group["pass_near_52w_high"] = group["dist_52w_high"] >= -15.0
    group["pass_breakout_extension"] = group["breakout_extension_pct"] <= MAX_BREAKOUT_EXTENSION_PCT
    group["pass_gap_behavior"] = group["gap_frequency_60d"] <= MAX_GAP_FREQ_60D
    group["pass_trend_alignment"] = (
        group["adj_close"].ge(group["dma_50"])
        & group["dma_50"].ge(group["dma_150"])
        & group["dma_150"].ge(group["dma_200"])
    )
    return group


def build_technical_features(
    *,
    symbols: list[str] | None = None,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    rebuild: bool = False,
    benchmark_name: str = DEFAULT_BENCHMARK_NAME,
) -> pd.DataFrame:
    symbol_universe = resolve_symbol_universe(symbols)
    if not symbol_universe:
        return pd.DataFrame()

    effective_from, history_start = choose_effective_start(from_date, rebuild=rebuild)
    effective_to = to_date or pd.Timestamp.now(tz="UTC").normalize()

    prices = load_price_history(
        symbols=symbol_universe,
        start_date=history_start,
        to_date=effective_to,
    )
    if prices.empty:
        return prices

    frames = [
        compute_group_features(group)
        for _, group in prices.groupby(["symbol", "series"], dropna=False)
    ]
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if out.empty:
        return out

    benchmark = load_benchmark_series(
        benchmark_name=benchmark_name,
        start_date=history_start,
        to_date=effective_to,
    )
    if not benchmark.empty:
        out = out.merge(benchmark, on="date", how="left")
        out["rs_vs_benchmark"] = out["stock_ret_20d"] - out["benchmark_ret_20d"]
    else:
        out["benchmark_close"] = np.nan
        out["benchmark_ret_20d"] = np.nan
        out["rs_vs_benchmark"] = np.nan

    sector_mapping = load_sector_mapping(symbol_universe)
    if not sector_mapping.empty:
        out = out.merge(sector_mapping, on="symbol", how="left")
        out["sector_name"] = out["sector_code"]
        memberships = load_latest_peer_memberships(symbol_universe)
        if not memberships.empty:
            peer_prices = load_peer_ohlcv(
                memberships["peer_symbol"].drop_duplicates().tolist(),
                start_date=history_start,
                to_date=effective_to,
            )
            if not peer_prices.empty:
                peer_basket = (
                    memberships.merge(
                        peer_prices.rename(columns={"ticker": "peer_symbol"}),
                        on="peer_symbol",
                        how="inner",
                    )
                    .groupby(["anchor_symbol", "date"], dropna=False)["peer_ret_20d"]
                    .agg(
                        sector_peer_ret_20d="median",
                        sector_peer_count="count",
                    )
                    .reset_index()
                )
                out = out.merge(
                    peer_basket,
                    left_on=["symbol", "date"],
                    right_on=["anchor_symbol", "date"],
                    how="left",
                )
                out["rs_vs_sector"] = np.where(
                    out["sector_peer_count"] >= MIN_SECTOR_PEER_COUNT,
                    out["stock_ret_20d"] - out["sector_peer_ret_20d"],
                    np.nan,
                )
                out = out.drop(columns=["anchor_symbol"], errors="ignore")
            else:
                out["sector_peer_ret_20d"] = np.nan
                out["sector_peer_count"] = 0
                out["rs_vs_sector"] = np.nan
        else:
            out["sector_peer_ret_20d"] = np.nan
            out["sector_peer_count"] = 0
            out["rs_vs_sector"] = np.nan
    else:
        out["sector_code"] = pd.NA
        out["sector_name"] = pd.NA
        out["sector_peer_ret_20d"] = np.nan
        out["sector_peer_count"] = 0
        out["rs_vs_sector"] = np.nan
    out["benchmark_name"] = benchmark_name
    out["company_master_id"] = map_company_master_ids(out["symbol"], exchange="NSE")
    out["asof_date"] = out["date"]
    out["load_ts"] = pd.Timestamp.utcnow()

    if effective_from is not None:
        out = out[out["asof_date"] >= effective_from]
    if effective_to is not None:
        out = out[out["asof_date"] <= effective_to]

    # Cross-sectional relative-strength percentile: rank each name's 60d and 120d trailing return within
    # its asof-date cross-section, blend, re-rank to 0-100. Point-in-time (trailing returns only; ranked
    # against the same-day universe). T1 showed this out-predicts the engine's 20d rs_vs_benchmark; the
    # 252d rank inverts, so 60/120 is the improved blend. Consumed by score_relative_strength iff its
    # reviewed default-OFF flag is enabled (otherwise this is a diagnostic column only).
    if not out.empty and "stock_ret_60d" in out.columns:
        r60 = out.groupby("asof_date")["stock_ret_60d"].rank(pct=True)
        r120 = out.groupby("asof_date")["stock_ret_120d"].rank(pct=True) if "stock_ret_120d" in out.columns else r60
        blend = pd.concat([r60, r120], axis=1).mean(axis=1)
        out["rs_percentile"] = blend.groupby(out["asof_date"]).rank(pct=True) * 100.0
    else:
        out["rs_percentile"] = np.nan

    ordered_cols = [
        "asof_date",
        "company_master_id",
        "symbol",
        "series",
        "security_id",
        "isin",
        "benchmark_name",
        "sector_code",
        "sector_name",
        "adj_close",
        "adj_high",
        "adj_low",
        "volume",
        "total_value",
        "dma_20",
        "dma_50",
        "dma_150",
        "dma_200",
        "dma_20_slope_20d_pct",
        "dma_50_slope_20d_pct",
        "dma_150_slope_20d_pct",
        "atr_20",
        "atr_pct",
        "atr_compression_pct",
        "bb_width",
        "bb_width_rank_252d",
        "dist_20d_high",
        "dist_50d_high",
        "dist_52w_high",
        "base_depth_20d_pct",
        "base_depth_60d_pct",
        "pivot_distance_20d_pct",
        "pivot_distance_60d_pct",
        "avg_traded_value_20d",
        "avg_traded_value_60d",
        "median_volume_20d",
        "median_volume_60d",
        "stock_ret_60d",
        "stock_ret_120d",
        "rs_vs_benchmark",
        "rs_percentile",
        "sector_peer_ret_20d",
        "sector_peer_count",
        "rs_vs_sector",
        "breakout_extension_pct",
        "daily_range_pct",
        "range_contraction_20d_pct",
        "range_contraction_60d_pct",
        "range_contraction_ratio",
        "close_location_pct",
        "tight_close_upper_half_20d",
        "tight_close_upper_half_60d",
        "higher_high_count_20d",
        "higher_low_count_20d",
        "trend_persistence_20d",
        "trend_persistence_60d",
        "trend_persistence_120d",
        "breakout_day_volume_vs_20d",
        "up_volume_20d",
        "down_volume_20d",
        "up_down_volume_ratio_20d",
        "distribution_days_20d",
        "accumulation_days_20d",
        "pullback_volume_dryup_ratio_20d",
        "gap_pct",
        "gap_frequency_60d",
        "support_distance_20d_pct",
        "support_hold_rate_20d",
        "volatility_contraction_flag",
        "pass_above_dma_20",
        "pass_above_dma_50",
        "pass_above_dma_150",
        "pass_above_dma_200",
        "pass_liquidity_20d",
        "pass_near_52w_high",
        "pass_breakout_extension",
        "pass_gap_behavior",
        "pass_trend_alignment",
        "load_ts",
    ]
    out = out[ordered_cols]
    return out.drop_duplicates(
        subset=["asof_date", "symbol", "series"],
        keep="last",
    ).reset_index(drop=True)


def persist_technical_features(
    df: pd.DataFrame,
    *,
    rebuild: bool = False,
    symbols: list[str] | None = None,
) -> None:
    if df.empty:
        return
    ensure_technical_feature_schema()
    if rebuild:
        def _delete_existing_technical_features() -> None:
            with db_session() as (_, cur):
                if symbols:
                    cur.execute(
                        f"DELETE FROM {TABLE_NAME} WHERE symbol = ANY(%s)",
                        (symbols,),
                    )
                else:
                    cur.execute(f"DELETE FROM {TABLE_NAME}")

        execute_db_operation(
            _delete_existing_technical_features,
            operation_name="technical_features:delete_rebuild_features",
        )
    upsert_to_db(
        df,
        TABLE_NAME,
        unique_keys=["asof_date", "symbol", "series"],
        timescaledb_column="asof_date",
    )


def build_refresh_status_rows(
    *,
    symbols: list[str],
    asof_date: pd.Timestamp | None,
    data_sync_result: dict[str, object] | None,
    feature_df: pd.DataFrame,
) -> pd.DataFrame:
    effective_asof = _normalize_asof(asof_date)
    normalized_symbols = sorted({str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()})
    rows: list[dict[str, object]] = []
    sync_result = data_sync_result if isinstance(data_sync_result, dict) else {}
    stage_status_by_symbol: dict[tuple[str, str], dict[str, object]] = {}
    for stage in ["ohlcv", "fundamentals"]:
        stage_items = sync_result.get(stage)
        if not isinstance(stage_items, list):
            continue
        for item in stage_items:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            error_text = item.get("error")
            status = "issue" if item.get("action") == "issue" or error_text else "ok"
            reason = str(item.get("reason") or "").strip()
            row_count = pd.to_numeric(item.get("rows"), errors="coerce")
            if (
                stage == "ohlcv"
                and status == "ok"
                and str(item.get("action") or "").strip().lower() == "sync"
                and not pd.isna(row_count)
                and float(row_count) <= 0
            ):
                status = "skipped_with_warning"
                reason = "ohlcv_sync_zero_rows"
            if str(item.get("action") or "").strip().lower() == "skip" and reason not in {"ohlcv_present", "fundamentals_present"}:
                status = "skipped_with_warning"
            stage_status_by_symbol[(symbol, stage)] = {
                "status": status,
                "reason": reason or None,
                "rows": row_count,
                "error_type": item.get("error_type"),
                "error_text": error_text,
            }
            rows.append(
                {
                    "asof_date": effective_asof,
                    "symbol": symbol,
                    "stage": stage,
                    "status": status,
                    "action": item.get("action"),
                    "reason": reason or None,
                    "rows": row_count,
                    "error_type": item.get("error_type"),
                    "error_text": None if error_text is None else str(error_text)[:2000],
                    "from_date": pd.to_datetime(item.get("from_date"), utc=True, errors="coerce"),
                    "to_date": pd.to_datetime(item.get("to_date"), utc=True, errors="coerce"),
                    "raw_json": _json_text(item),
                    "load_ts": pd.Timestamp.utcnow(),
                }
            )

    feature_counts: dict[str, int] = {}
    latest_feature_dates: dict[str, pd.Timestamp] = {}
    latest_benchmark_rs_dates: dict[str, pd.Timestamp] = {}
    benchmark_rs_counts: dict[str, int] = {}
    if not feature_df.empty and "symbol" in feature_df.columns:
        working = feature_df.copy()
        working["symbol"] = working["symbol"].astype("string").str.strip().str.upper()
        if "asof_date" in working.columns:
            working["asof_date"] = pd.to_datetime(working["asof_date"], utc=True, errors="coerce").dt.normalize()
        if "rs_vs_benchmark" in working.columns:
            working["rs_vs_benchmark"] = pd.to_numeric(working["rs_vs_benchmark"], errors="coerce")
        for symbol, group in working.groupby("symbol", dropna=False):
            symbol_text = str(symbol or "").strip().upper()
            if not symbol_text:
                continue
            feature_counts[symbol_text] = int(len(group))
            if "asof_date" in group.columns:
                valid_dates = pd.to_datetime(group["asof_date"], utc=True, errors="coerce").dropna()
                if not valid_dates.empty:
                    latest_feature_dates[symbol_text] = valid_dates.max().normalize()
                if "rs_vs_benchmark" in group.columns:
                    valid_rs = group.loc[group["rs_vs_benchmark"].notna()].copy()
                    benchmark_rs_counts[symbol_text] = int(len(valid_rs))
                    valid_rs_dates = pd.to_datetime(valid_rs.get("asof_date"), utc=True, errors="coerce").dropna()
                    if not valid_rs_dates.empty:
                        latest_benchmark_rs_dates[symbol_text] = valid_rs_dates.max().normalize()

    for symbol in normalized_symbols:
        row_count = int(feature_counts.get(symbol, 0))
        latest_feature_date = latest_feature_dates.get(symbol)
        if row_count <= 0:
            status = "issue"
            reason = "technical_rows_missing_after_build"
            ohlcv_status = stage_status_by_symbol.get((symbol, "ohlcv"), {})
            if ohlcv_status.get("status") == "issue":
                reason = "technical_rows_missing_after_ohlcv_issue"
            elif ohlcv_status.get("reason") == "ohlcv_sync_zero_rows":
                reason = "technical_rows_missing_after_ohlcv_zero_rows"
            elif ohlcv_status.get("reason") == "ohlcv_no_new_data":
                reason = "technical_rows_missing_after_ohlcv_no_new_data"
        elif latest_feature_date is not None and latest_feature_date < effective_asof:
            status = "stale"
            reason = "technical_rows_stale_after_build"
        else:
            status = "ok"
            reason = "technical_rows_built"
        rows.append(
            {
                "asof_date": effective_asof,
                "symbol": symbol,
                "stage": "technical_build",
                "status": status,
                "action": "build",
                "reason": reason,
                "rows": row_count,
                "error_type": None,
                "error_text": None,
                "from_date": None,
                "to_date": latest_feature_date,
                "raw_json": _json_text(
                    {
                        "symbol": symbol,
                        "row_count": row_count,
                        "latest_feature_asof_date": None if latest_feature_date is None else latest_feature_date.isoformat(),
                        "target_asof_date": effective_asof.isoformat(),
                    }
                ),
                "load_ts": pd.Timestamp.utcnow(),
            }
        )

        benchmark_rs_count = int(benchmark_rs_counts.get(symbol, 0))
        latest_benchmark_rs_date = latest_benchmark_rs_dates.get(symbol)
        if row_count <= 0:
            status = "issue"
            reason = "benchmark_rs_not_evaluated_no_technical_rows"
        elif benchmark_rs_count <= 0:
            status = "issue"
            reason = "benchmark_rs_missing_all_rows"
        elif latest_feature_date is not None and latest_benchmark_rs_date is not None and latest_benchmark_rs_date < latest_feature_date:
            status = "stale"
            reason = "benchmark_rs_stale_for_latest_feature_date"
        else:
            status = "ok"
            reason = "benchmark_rs_available"
        rows.append(
            {
                "asof_date": effective_asof,
                "symbol": symbol,
                "stage": "benchmark_rs",
                "status": status,
                "action": "validate",
                "reason": reason,
                "rows": benchmark_rs_count,
                "error_type": None,
                "error_text": None,
                "from_date": None,
                "to_date": latest_benchmark_rs_date,
                "raw_json": _json_text(
                    {
                        "symbol": symbol,
                        "benchmark_rs_row_count": benchmark_rs_count,
                        "latest_benchmark_rs_asof_date": None if latest_benchmark_rs_date is None else latest_benchmark_rs_date.isoformat(),
                        "latest_feature_asof_date": None if latest_feature_date is None else latest_feature_date.isoformat(),
                        "target_asof_date": effective_asof.isoformat(),
                        "authority": "technical_feature_input_diagnostic_no_portfolio_no_broker",
                    }
                ),
                "load_ts": pd.Timestamp.utcnow(),
            }
        )

    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    frame["asof_date"] = pd.to_datetime(frame["asof_date"], utc=True, errors="coerce")
    frame["symbol"] = frame["symbol"].astype("string").str.strip().str.upper()
    frame["stage"] = frame["stage"].astype("string").str.strip()
    frame["status"] = frame["status"].astype("string").str.strip()
    for column in ["from_date", "to_date", "load_ts"]:
        frame[column] = pd.to_datetime(frame[column], utc=True, errors="coerce")
    frame["rows"] = pd.to_numeric(frame["rows"], errors="coerce")
    return frame.drop_duplicates(subset=["asof_date", "symbol", "stage"], keep="last")


def persist_refresh_status(rows: pd.DataFrame) -> None:
    if rows.empty:
        return
    ensure_refresh_status_table()
    upsert_to_db(
        rows,
        REFRESH_STATUS_TABLE,
        unique_keys=["asof_date", "symbol", "stage"],
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build TA-Lib based advisory technical features."
    )
    parser.add_argument("--symbols", nargs="*", help="Symbols, comma-separated or repeated")
    parser.add_argument("--from-date", type=parse_datetime_arg)
    parser.add_argument("--to-date", type=parse_datetime_arg)
    parser.add_argument("--benchmark", default=DEFAULT_BENCHMARK_NAME)
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--skip-peer-sync",
        action="store_true",
        help="Do not refresh Sharpely peer snapshots or Dhan peer OHLCV before building.",
    )
    parser.add_argument(
        "--skip-fundamentals-sync",
        action="store_true",
        help="Do not refresh Sharpely fundamentals before building technical features.",
    )
    parser.add_argument(
        "--targeted-context-refresh",
        action="store_true",
        help="Fast context-watch refresh: refresh Dhan OHLCV and technical rows only; skip Sharpely fundamentals and peer sync.",
    )
    return parser.parse_args()


def summarize(df: pd.DataFrame) -> dict[str, object]:
    if df.empty:
        return {
            "status": "ok",
            "table": TABLE_NAME,
            "row_count": 0,
            "symbol_count": 0,
            "date_min": None,
            "date_max": None,
            "sample": [],
        }
    preview_cols = [
        "asof_date",
        "company_master_id",
        "symbol",
        "adj_close",
        "dma_20",
        "dma_50",
        "dma_150",
        "dma_200",
        "dma_20_slope_20d_pct",
        "dma_50_slope_20d_pct",
        "dma_150_slope_20d_pct",
        "atr_20",
        "atr_pct",
        "bb_width",
        "range_contraction_ratio",
        "dist_52w_high",
        "base_depth_60d_pct",
        "pivot_distance_20d_pct",
        "avg_traded_value_20d",
        "median_volume_20d",
        "stock_ret_60d",
        "rs_vs_benchmark",
        "sector_peer_count",
        "sector_peer_ret_20d",
        "rs_vs_sector",
        "breakout_extension_pct",
        "trend_persistence_60d",
        "breakout_day_volume_vs_20d",
        "up_down_volume_ratio_20d",
        "distribution_days_20d",
        "accumulation_days_20d",
        "gap_frequency_60d",
        "pass_liquidity_20d",
        "pass_breakout_extension",
        "pass_gap_behavior",
        "pass_trend_alignment",
    ]
    return {
        "status": "ok",
        "table": TABLE_NAME,
        "row_count": int(len(df)),
        "symbol_count": int(df["symbol"].nunique()),
        "date_min": df["asof_date"].min().date().isoformat(),
        "date_max": df["asof_date"].max().date().isoformat(),
        "rs_vs_sector_row_count": int(df["rs_vs_sector"].notna().sum()),
        "sample": df[preview_cols].head(5).to_dict(orient="records"),
    }


def main() -> int:
    args = parse_args()
    symbol_universe = resolve_symbol_universe(args.symbols)
    peer_sync_result: dict[str, object] | None = None
    data_sync_result: dict[str, object] | None = None
    skip_peer_sync = bool(args.skip_peer_sync or args.targeted_context_refresh)
    skip_fundamentals_sync = bool(args.skip_fundamentals_sync or args.targeted_context_refresh)
    if symbol_universe and not args.dry_run:
        data_sync_result = ensure_advisory_symbol_inputs(
            symbol_universe,
            to_date=args.to_date,
            include_fundamentals=not skip_fundamentals_sync,
            include_ohlcv=True,
        )
    if symbol_universe and not skip_peer_sync and not args.dry_run:
        peer_sync_result = sync_peer_data(
            symbols=symbol_universe,
            to_date=args.to_date,
        )
    df = build_technical_features(
        symbols=symbol_universe,
        from_date=_normalize_asof(args.from_date) if args.from_date else None,
        to_date=_normalize_asof(args.to_date) if args.to_date else None,
        rebuild=args.rebuild,
        benchmark_name=args.benchmark,
    )
    refresh_status = build_refresh_status_rows(
        symbols=symbol_universe,
        asof_date=_normalize_asof(args.to_date) if args.to_date else None,
        data_sync_result=data_sync_result,
        feature_df=df,
    )
    if not args.dry_run:
        persist_technical_features(
            df,
            rebuild=args.rebuild,
            symbols=symbol_universe,
        )
        persist_refresh_status(refresh_status)
    result = summarize(df)
    result["dry_run"] = bool(args.dry_run)
    result["targeted_context_refresh"] = bool(args.targeted_context_refresh)
    result["skip_peer_sync"] = bool(skip_peer_sync)
    result["skip_fundamentals_sync"] = bool(skip_fundamentals_sync)
    result["data_sync"] = data_sync_result
    result["peer_sync"] = peer_sync_result
    result["refresh_status"] = {
        "table": REFRESH_STATUS_TABLE,
        "row_count": int(len(refresh_status)),
        "issue_count": int(refresh_status["status"].astype("string").str.lower().isin(["issue", "stale", "skipped_with_warning"]).sum()) if not refresh_status.empty else 0,
        "status_counts": refresh_status["status"].astype("string").value_counts(dropna=False).to_dict() if not refresh_status.empty else {},
    }
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
