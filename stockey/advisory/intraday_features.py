from __future__ import annotations

import argparse
from datetime import timedelta
from typing import Any

import numpy as np
import pandas as pd

from data.dhanlive.client import DhanAPIError
from data.dhanlive.ohlcv import sync_intraday_ohlcv
from utils.db import db_session, sql_to_df, upsert_to_db
from utils.sync import load_tracked_symbols, parse_datetime_arg


TABLE_NAME = "advisory_intraday_features_daily"
DEFAULT_LOOKBACK_DAYS = 180
DEFAULT_INTERVAL_MINUTES = 1
INTRADAY_READ_SYMBOL_CHUNK_SIZE = 40
SUPPORTED_INTERVAL_MINUTES = (1, 5, 15, 25, 60)
LOCAL_TIMEZONE = "Asia/Kolkata"


def normalize_timestamp(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce").dt.normalize()


def table_exists(table_name: str) -> bool:
    df = sql_to_df(
        """
        SELECT 1 AS exists_flag
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = %s
        LIMIT 1
        """,
        params=(table_name,),
    )
    return not df.empty


def ensure_intraday_features_table() -> None:
    with db_session() as (_, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                asof_date TIMESTAMPTZ NOT NULL,
                symbol TEXT NOT NULL,
                company_master_id TEXT,
                interval_minutes INTEGER NOT NULL,
                bar_count INTEGER,
                session_open DOUBLE PRECISION,
                session_high DOUBLE PRECISION,
                session_low DOUBLE PRECISION,
                session_close DOUBLE PRECISION,
                session_volume DOUBLE PRECISION,
                intraday_vwap DOUBLE PRECISION,
                intraday_range_pct DOUBLE PRECISION,
                intraday_open_to_close_pct DOUBLE PRECISION,
                intraday_close_vs_vwap_pct DOUBLE PRECISION,
                intraday_pct_bars_above_vwap DOUBLE PRECISION,
                intraday_close_location_pct DOUBLE PRECISION,
                intraday_opening_range_high DOUBLE PRECISION,
                intraday_opening_range_low DOUBLE PRECISION,
                intraday_opening_range_breakout_up BOOLEAN,
                intraday_opening_range_breakout_down BOOLEAN,
                intraday_prev_day_high DOUBLE PRECISION,
                intraday_prev_day_low DOUBLE PRECISION,
                intraday_prev_day_breakout_up BOOLEAN,
                intraday_failed_prev_day_breakout BOOLEAN,
                intraday_first_30m_return_pct DOUBLE PRECISION,
                intraday_last_60m_return_pct DOUBLE PRECISION,
                intraday_volume_vs_20d DOUBLE PRECISION,
                intraday_breakout_score DOUBLE PRECISION,
                intraday_pattern_label TEXT,
                model_name TEXT,
                model_score DOUBLE PRECISION,
                load_ts TIMESTAMPTZ NOT NULL,
                UNIQUE (asof_date, symbol, interval_minutes)
            )
            """
        )
        column_defs = {
            "company_master_id": "TEXT",
            "bar_count": "INTEGER",
            "session_open": "DOUBLE PRECISION",
            "session_high": "DOUBLE PRECISION",
            "session_low": "DOUBLE PRECISION",
            "session_close": "DOUBLE PRECISION",
            "session_volume": "DOUBLE PRECISION",
            "intraday_vwap": "DOUBLE PRECISION",
            "intraday_range_pct": "DOUBLE PRECISION",
            "intraday_open_to_close_pct": "DOUBLE PRECISION",
            "intraday_close_vs_vwap_pct": "DOUBLE PRECISION",
            "intraday_pct_bars_above_vwap": "DOUBLE PRECISION",
            "intraday_close_location_pct": "DOUBLE PRECISION",
            "intraday_opening_range_high": "DOUBLE PRECISION",
            "intraday_opening_range_low": "DOUBLE PRECISION",
            "intraday_opening_range_breakout_up": "BOOLEAN",
            "intraday_opening_range_breakout_down": "BOOLEAN",
            "intraday_prev_day_high": "DOUBLE PRECISION",
            "intraday_prev_day_low": "DOUBLE PRECISION",
            "intraday_prev_day_breakout_up": "BOOLEAN",
            "intraday_failed_prev_day_breakout": "BOOLEAN",
            "intraday_first_30m_return_pct": "DOUBLE PRECISION",
            "intraday_last_60m_return_pct": "DOUBLE PRECISION",
            "intraday_volume_vs_20d": "DOUBLE PRECISION",
            "intraday_breakout_score": "DOUBLE PRECISION",
            "intraday_pattern_label": "TEXT",
            "model_name": "TEXT",
            "model_score": "DOUBLE PRECISION",
            "load_ts": "TIMESTAMPTZ",
        }
        for column, sql_type in column_defs.items():
            cur.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS {column} {sql_type}")
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_dhan_ohlcv_intraday_ticker_window
            ON dhan_ohlcv_intraday (exchange, asset_type, interval_minutes, ticker, "timestamp")
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_dhan_ohlcv_daily_ticker_window
            ON dhan_ohlcv_daily (exchange, asset_type, ticker, date)
            """
        )


def resolve_symbol_universe(
    symbols: list[str] | None,
    *,
    asof_date: pd.Timestamp | None,
) -> list[str]:
    tracked = load_tracked_symbols(symbols)
    if tracked:
        return tracked

    clauses = ["symbol IS NOT NULL"]
    params: list[object] = []
    if asof_date is not None:
        clauses.append("date = (SELECT MAX(date) FROM advisory_screener_constituents WHERE date <= %s)")
        params.append(asof_date)
    else:
        clauses.append("date = (SELECT MAX(date) FROM advisory_screener_constituents)")
    df = sql_to_df(
        f"""
        SELECT DISTINCT symbol
        FROM advisory_screener_constituents
        WHERE {' AND '.join(clauses)}
        ORDER BY symbol
        """,
        params=tuple(params) if params else None,
    )
    if df.empty:
        return []
    return df["symbol"].astype("string").dropna().str.strip().str.upper().drop_duplicates().tolist()


def load_intraday_coverage(
    symbols: list[str],
    *,
    interval_minutes: int,
) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame(columns=["symbol", "min_timestamp", "max_timestamp"])
    return sql_to_df(
        """
        SELECT
            ticker AS symbol,
            MIN("timestamp") AS min_timestamp,
            MAX("timestamp") AS max_timestamp
        FROM dhan_ohlcv_intraday
        WHERE exchange = 'NSE'
          AND asset_type = 'stock'
          AND interval_minutes = %s
          AND ticker = ANY(%s)
        GROUP BY ticker
        """,
        params=(interval_minutes, symbols),
    )


def ensure_intraday_history(
    symbols: list[str],
    *,
    asof_date: pd.Timestamp,
    lookback_days: int,
    interval_minutes: int | None = None,
    intervals: tuple[int, ...] | None = None,
) -> list[dict[str, Any]]:
    if not symbols:
        return []

    interval_values = intervals or (() if interval_minutes is None else (interval_minutes,))
    interval_values = tuple(sorted({int(value) for value in interval_values if int(value) in SUPPORTED_INTERVAL_MINUTES}))
    if not interval_values:
        return []

    target_start = (asof_date - pd.Timedelta(days=lookback_days)).to_pydatetime()
    target_end = (asof_date + pd.Timedelta(days=1)).to_pydatetime()
    results: list[dict[str, Any]] = []
    for current_interval in interval_values:
        coverage = load_intraday_coverage(symbols, interval_minutes=current_interval)
        if not coverage.empty:
            coverage["symbol"] = coverage["symbol"].astype("string").str.upper()
            coverage["min_timestamp"] = pd.to_datetime(coverage["min_timestamp"], utc=True, errors="coerce")
            coverage["max_timestamp"] = pd.to_datetime(coverage["max_timestamp"], utc=True, errors="coerce")
            coverage_map = coverage.set_index("symbol").to_dict(orient="index")
        else:
            coverage_map = {}

        for symbol in symbols:
            snapshot = coverage_map.get(symbol)
            needs_sync = snapshot is None
            if snapshot is not None:
                min_timestamp = snapshot.get("min_timestamp")
                max_timestamp = snapshot.get("max_timestamp")
                if pd.isna(min_timestamp) or pd.isna(max_timestamp):
                    needs_sync = True
                else:
                    needs_sync = bool(min_timestamp > target_start or max_timestamp < target_end)
            if not needs_sync:
                results.append({"symbol": symbol, "interval_minutes": current_interval, "action": "skip", "reason": "intraday_present"})
                continue
            try:
                frame = sync_intraday_ohlcv(
                    symbol,
                    exchange="NSE",
                    asset_type="stock",
                    interval_minutes=current_interval,
                    from_date=target_start,
                    to_date=target_end,
                )
            except (DhanAPIError, ValueError) as exc:
                results.append(
                    {
                        "symbol": symbol,
                        "interval_minutes": current_interval,
                        "action": "issue",
                        "reason": "dhan_intraday_sync_failed",
                        "error_type": exc.__class__.__name__,
                        "error": str(exc),
                    }
                )
                continue
            results.append(
                {
                    "symbol": symbol,
                    "interval_minutes": current_interval,
                    "action": "sync",
                    "rows": int(len(frame)),
                    "from_timestamp": None if frame.empty else str(frame["timestamp"].min()),
                    "to_timestamp": None if frame.empty else str(frame["timestamp"].max()),
                }
            )
    return results


def load_intraday_history(
    symbols: list[str],
    *,
    start_timestamp: pd.Timestamp,
    end_timestamp: pd.Timestamp,
    interval_minutes: int,
) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()
    unique_symbols = sorted({str(symbol).upper() for symbol in symbols if str(symbol).strip()})
    frames: list[pd.DataFrame] = []
    for offset in range(0, len(unique_symbols), INTRADAY_READ_SYMBOL_CHUNK_SIZE):
        symbol_chunk = unique_symbols[offset : offset + INTRADAY_READ_SYMBOL_CHUNK_SIZE]
        frame = sql_to_df(
            """
            SELECT
                company_master_id,
                ticker AS symbol,
                interval_minutes,
                "timestamp",
                open,
                high,
                low,
                close,
                volume
            FROM dhan_ohlcv_intraday
            WHERE exchange = 'NSE'
              AND asset_type = 'stock'
              AND interval_minutes = %s
              AND ticker = ANY(%s)
              AND "timestamp" >= %s
              AND "timestamp" <= %s
            ORDER BY ticker, "timestamp"
            """,
            params=(interval_minutes, symbol_chunk, start_timestamp, end_timestamp),
            chunksize=25_000,
        )
        if not frame.empty:
            frames.append(frame)
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if df.empty:
        return df
    df["symbol"] = df["symbol"].astype("string").str.upper()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    df["asof_date"] = pd.to_datetime(
        df["timestamp"].dt.tz_convert(LOCAL_TIMEZONE).dt.strftime("%Y-%m-%d"),
        utc=True,
        errors="coerce",
    )
    numeric_columns = ["open", "high", "low", "close", "volume"]
    for column in numeric_columns:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    return df.dropna(subset=["timestamp", "asof_date"])


def load_daily_reference(
    symbols: list[str],
    *,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()
    df = sql_to_df(
        """
        SELECT
            ticker AS symbol,
            date,
            high,
            low,
            close,
            volume
        FROM dhan_ohlcv_daily
        WHERE exchange = 'NSE'
          AND asset_type = 'stock'
          AND ticker = ANY(%s)
          AND date >= %s
          AND date <= %s
        ORDER BY ticker, date
        """,
        params=(symbols, start_date, end_date),
    )
    if df.empty:
        return df
    df["symbol"] = df["symbol"].astype("string").str.upper()
    df["date"] = normalize_timestamp(df["date"])
    for column in ["high", "low", "close", "volume"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df = (
        df.sort_values(["symbol", "date"])
        .drop_duplicates(subset=["symbol", "date"], keep="last")
        .reset_index(drop=True)
    )
    df["prev_day_high"] = df.groupby("symbol", dropna=False)["high"].shift(1)
    df["prev_day_low"] = df.groupby("symbol", dropna=False)["low"].shift(1)
    df["avg_daily_volume_20d"] = df.groupby("symbol", dropna=False)["volume"].transform(lambda values: values.rolling(20, min_periods=5).mean())
    return df


def _safe_pct_change(current: float | None, reference: float | None) -> float | None:
    if current is None or reference is None or reference == 0:
        return None
    return ((current / reference) - 1.0) * 100.0


def compute_intraday_session_features(
    intraday: pd.DataFrame,
    *,
    daily_reference: pd.DataFrame,
) -> pd.DataFrame:
    if intraday.empty:
        return intraday

    daily_ref = daily_reference.rename(columns={"date": "asof_date"}).copy()
    if not daily_ref.empty:
        daily_ref["symbol"] = daily_ref["symbol"].astype("string").str.upper()
        daily_ref["asof_date"] = normalize_timestamp(daily_ref["asof_date"])
        daily_ref = daily_ref.drop_duplicates(subset=["symbol", "asof_date"], keep="last")
    ref_lookup = daily_ref.set_index(["symbol", "asof_date"]).to_dict(orient="index") if not daily_ref.empty else {}

    rows: list[dict[str, Any]] = []
    for (symbol, asof_date), group in intraday.groupby(["symbol", "asof_date"], dropna=False, sort=False):
        group = group.sort_values("timestamp").copy()
        if group.empty:
            continue
        interval_minutes = int(pd.to_numeric(group["interval_minutes"].iloc[0], errors="coerce") or DEFAULT_INTERVAL_MINUTES)
        orb_bars = max(1, int(round(30 / max(interval_minutes, 1))))
        last_60_bars = max(1, int(round(60 / max(interval_minutes, 1))))

        session_open = pd.to_numeric(group["open"].iloc[0], errors="coerce")
        session_high = pd.to_numeric(group["high"].max(), errors="coerce")
        session_low = pd.to_numeric(group["low"].min(), errors="coerce")
        session_close = pd.to_numeric(group["close"].iloc[-1], errors="coerce")
        session_volume = pd.to_numeric(group["volume"].fillna(0).sum(), errors="coerce")

        weighted_close = group["close"].fillna(0) * group["volume"].fillna(0)
        session_vwap = weighted_close.sum() / session_volume if pd.notna(session_volume) and session_volume > 0 else np.nan

        close_location_pct = None
        if pd.notna(session_high) and pd.notna(session_low) and session_high != session_low and pd.notna(session_close):
            close_location_pct = float((session_close - session_low) / (session_high - session_low))

        cumulative_volume = group["volume"].fillna(0).cumsum().replace(0, np.nan)
        cumulative_weighted_close = weighted_close.cumsum()
        rolling_vwap = cumulative_weighted_close / cumulative_volume
        pct_bars_above_vwap = float((group["close"] >= rolling_vwap).mean()) if not group.empty else np.nan

        opening_range = group.head(orb_bars)
        opening_range_high = pd.to_numeric(opening_range["high"].max(), errors="coerce")
        opening_range_low = pd.to_numeric(opening_range["low"].min(), errors="coerce")
        opening_range_breakout_up = bool(pd.notna(opening_range_high) and pd.notna(session_high) and pd.notna(session_close) and session_high > opening_range_high and session_close >= opening_range_high)
        opening_range_breakout_down = bool(pd.notna(opening_range_low) and pd.notna(session_low) and pd.notna(session_close) and session_low < opening_range_low and session_close <= opening_range_low)

        first_30m_close = pd.to_numeric(opening_range["close"].iloc[-1], errors="coerce") if not opening_range.empty else np.nan
        last_60m_open = pd.to_numeric(group["open"].iloc[-last_60_bars], errors="coerce") if len(group) >= last_60_bars else pd.to_numeric(group["open"].iloc[0], errors="coerce")

        ref_row = ref_lookup.get((symbol, asof_date), {})
        prev_day_high = pd.to_numeric(ref_row.get("prev_day_high"), errors="coerce")
        prev_day_low = pd.to_numeric(ref_row.get("prev_day_low"), errors="coerce")
        avg_daily_volume_20d = pd.to_numeric(ref_row.get("avg_daily_volume_20d"), errors="coerce")

        prev_day_breakout_up = bool(pd.notna(prev_day_high) and pd.notna(session_high) and pd.notna(session_close) and session_high > prev_day_high and session_close >= prev_day_high)
        failed_prev_day_breakout = bool(pd.notna(prev_day_high) and pd.notna(session_high) and pd.notna(session_close) and session_high > prev_day_high and session_close < prev_day_high)

        rows.append(
            {
                "asof_date": asof_date,
                "symbol": symbol,
                "company_master_id": group["company_master_id"].dropna().iloc[-1] if group["company_master_id"].notna().any() else None,
                "interval_minutes": interval_minutes,
                "bar_count": int(len(group)),
                "session_open": None if pd.isna(session_open) else float(session_open),
                "session_high": None if pd.isna(session_high) else float(session_high),
                "session_low": None if pd.isna(session_low) else float(session_low),
                "session_close": None if pd.isna(session_close) else float(session_close),
                "session_volume": None if pd.isna(session_volume) else float(session_volume),
                "intraday_vwap": None if pd.isna(session_vwap) else float(session_vwap),
                "intraday_range_pct": _safe_pct_change(float(session_high), float(session_low)) if pd.notna(session_high) and pd.notna(session_low) else None,
                "intraday_open_to_close_pct": _safe_pct_change(float(session_close), float(session_open)) if pd.notna(session_close) and pd.notna(session_open) else None,
                "intraday_close_vs_vwap_pct": _safe_pct_change(float(session_close), float(session_vwap)) if pd.notna(session_close) and pd.notna(session_vwap) else None,
                "intraday_pct_bars_above_vwap": None if pd.isna(pct_bars_above_vwap) else round(float(pct_bars_above_vwap), 6),
                "intraday_close_location_pct": None if close_location_pct is None else round(float(close_location_pct), 6),
                "intraday_opening_range_high": None if pd.isna(opening_range_high) else float(opening_range_high),
                "intraday_opening_range_low": None if pd.isna(opening_range_low) else float(opening_range_low),
                "intraday_opening_range_breakout_up": opening_range_breakout_up,
                "intraday_opening_range_breakout_down": opening_range_breakout_down,
                "intraday_prev_day_high": None if pd.isna(prev_day_high) else float(prev_day_high),
                "intraday_prev_day_low": None if pd.isna(prev_day_low) else float(prev_day_low),
                "intraday_prev_day_breakout_up": prev_day_breakout_up,
                "intraday_failed_prev_day_breakout": failed_prev_day_breakout,
                "intraday_first_30m_return_pct": _safe_pct_change(float(first_30m_close), float(session_open)) if pd.notna(first_30m_close) and pd.notna(session_open) else None,
                "intraday_last_60m_return_pct": _safe_pct_change(float(session_close), float(last_60m_open)) if pd.notna(session_close) and pd.notna(last_60m_open) else None,
                "intraday_volume_vs_20d": None if pd.isna(avg_daily_volume_20d) or avg_daily_volume_20d == 0 or pd.isna(session_volume) else round(float(session_volume / avg_daily_volume_20d), 6),
                "intraday_breakout_score": None,
                "intraday_pattern_label": "INTRADAY_NEUTRAL",
                "model_name": None,
                "model_score": None,
                "load_ts": pd.Timestamp.utcnow(),
            }
        )

    out = pd.DataFrame(rows)
    if out.empty:
        return out

    score_parts = pd.concat(
        [
            pd.to_numeric(out["intraday_close_vs_vwap_pct"], errors="coerce").gt(0).astype(float),
            pd.to_numeric(out["intraday_pct_bars_above_vwap"], errors="coerce").ge(0.55).astype(float),
            out["intraday_opening_range_breakout_up"].fillna(False).astype(bool).astype(float),
            out["intraday_prev_day_breakout_up"].fillna(False).astype(bool).astype(float),
            pd.to_numeric(out["intraday_close_location_pct"], errors="coerce").ge(0.65).astype(float),
            pd.to_numeric(out["intraday_volume_vs_20d"], errors="coerce").ge(0.8).astype(float),
        ],
        axis=1,
    )
    out["intraday_breakout_score"] = score_parts.mean(axis=1).clip(lower=0.0, upper=1.0)
    out.loc[out["intraday_failed_prev_day_breakout"].fillna(False), "intraday_breakout_score"] = (
        out.loc[out["intraday_failed_prev_day_breakout"].fillna(False), "intraday_breakout_score"].fillna(0.0) * 0.25
    )
    out.loc[out["intraday_failed_prev_day_breakout"].fillna(False), "intraday_pattern_label"] = "FAILED_BREAKOUT"
    out.loc[out["intraday_opening_range_breakout_up"].fillna(False), "intraday_pattern_label"] = "OPENING_RANGE_BREAKOUT"
    out.loc[out["intraday_prev_day_breakout_up"].fillna(False), "intraday_pattern_label"] = "PREV_DAY_BREAKOUT"
    confirmed_mask = (
        out["intraday_prev_day_breakout_up"].fillna(False)
        & ~out["intraday_failed_prev_day_breakout"].fillna(False)
        & pd.to_numeric(out["intraday_close_vs_vwap_pct"], errors="coerce").gt(0)
    )
    out.loc[confirmed_mask, "intraday_pattern_label"] = "BREAKOUT_CONFIRMATION"
    return out


def build_intraday_features(
    *,
    symbols: list[str] | None = None,
    asof_date: pd.Timestamp | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
    intervals: tuple[int, ...] | None = None,
    ensure_history: bool = True,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    ensure_intraday_features_table()
    effective_asof = (asof_date or pd.Timestamp.utcnow()).normalize()
    symbol_universe = resolve_symbol_universe(symbols, asof_date=effective_asof)
    interval_values = intervals or (interval_minutes,)
    interval_values = tuple(sorted({int(value) for value in interval_values if int(value) in SUPPORTED_INTERVAL_MINUTES}))
    if not symbol_universe:
        return pd.DataFrame(), {
            "asof_date": effective_asof.isoformat(),
            "symbol_count": 0,
            "lookback_days": lookback_days,
            "interval_minutes": interval_minutes,
            "intervals": list(interval_values),
            "sync_results": [],
        }

    sync_results = []
    if ensure_history:
        sync_results = ensure_intraday_history(
            symbol_universe,
            asof_date=effective_asof,
            lookback_days=lookback_days,
            intervals=interval_values,
        )

    start_timestamp = effective_asof - pd.Timedelta(days=lookback_days)
    end_timestamp = effective_asof + pd.Timedelta(days=1)
    daily_reference = load_daily_reference(
        symbol_universe,
        start_date=effective_asof - pd.Timedelta(days=lookback_days + 30),
        end_date=effective_asof,
    )
    frames: list[pd.DataFrame] = []
    for current_interval in interval_values:
        intraday = load_intraday_history(
            symbol_universe,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            interval_minutes=current_interval,
        )
        if intraday.empty:
            continue
        features = compute_intraday_session_features(intraday, daily_reference=daily_reference)
        if not features.empty:
            frames.append(features[features["asof_date"] == effective_asof].copy())

    features = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return features, {
        "asof_date": effective_asof.isoformat(),
        "symbol_count": len(symbol_universe),
        "lookback_days": lookback_days,
        "interval_minutes": interval_minutes,
        "intervals": list(interval_values),
        "sync_results": sync_results,
    }


def persist_intraday_features(
    df: pd.DataFrame,
    *,
    rebuild: bool = False,
    asof_date: pd.Timestamp | None = None,
    intervals: tuple[int, ...] | None = None,
) -> None:
    ensure_intraday_features_table()
    if df.empty:
        return
    df = df.copy()
    numeric_columns = [
        "session_open",
        "session_high",
        "session_low",
        "session_close",
        "session_volume",
        "intraday_vwap",
        "intraday_range_pct",
        "intraday_open_to_close_pct",
        "intraday_close_vs_vwap_pct",
        "intraday_pct_bars_above_vwap",
        "intraday_close_location_pct",
        "intraday_opening_range_high",
        "intraday_opening_range_low",
        "intraday_prev_day_high",
        "intraday_prev_day_low",
        "intraday_first_30m_return_pct",
        "intraday_last_60m_return_pct",
        "intraday_volume_vs_20d",
        "intraday_breakout_score",
        "model_score",
    ]
    for column in numeric_columns:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    effective_asof = asof_date or pd.to_datetime(df["asof_date"].max(), utc=True, errors="coerce")
    if rebuild and pd.notna(effective_asof):
        with db_session() as (_, cur):
            if intervals:
                cur.execute(
                    f"DELETE FROM {TABLE_NAME} WHERE asof_date = %s AND interval_minutes = ANY(%s)",
                    (effective_asof, list(intervals)),
                )
            else:
                cur.execute(f"DELETE FROM {TABLE_NAME} WHERE asof_date = %s", (effective_asof,))
    upsert_to_db(df, TABLE_NAME, unique_keys=["asof_date", "symbol", "interval_minutes"], timescaledb_column="asof_date")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build daily advisory intraday features from stored Dhan candles.")
    parser.add_argument("--date", type=parse_datetime_arg, help="Feature asof date in YYYY-MM-DD")
    parser.add_argument("--symbols", nargs="*", help="Optional symbols")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--interval-minutes", type=int, default=DEFAULT_INTERVAL_MINUTES)
    parser.add_argument("--intervals", nargs="*", type=int, help="Optional list of candle intervals to build together")
    parser.add_argument("--skip-sync", action="store_true", help="Use already stored intraday data only")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    intervals = tuple(sorted({int(value) for value in (args.intervals or [args.interval_minutes]) if int(value) in SUPPORTED_INTERVAL_MINUTES}))
    features, meta = build_intraday_features(
        symbols=args.symbols,
        asof_date=pd.Timestamp(args.date, tz="UTC") if args.date else None,
        lookback_days=int(args.lookback_days),
        interval_minutes=int(args.interval_minutes),
        intervals=intervals,
        ensure_history=not bool(args.skip_sync),
    )
    if not args.dry_run:
        persist_intraday_features(
            features,
            rebuild=bool(args.rebuild),
            asof_date=pd.Timestamp(args.date, tz="UTC") if args.date else None,
            intervals=intervals,
        )
    print(
        {
            "table": TABLE_NAME,
            "row_count": int(len(features)),
            "meta": meta,
        }
    )


if __name__ == "__main__":
    main()
