from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import os
import sys
from typing import Iterable

import pandas as pd

from data.dhanlive.auth import DhanAuthError
from data.dhanlive.client import DhanAPIError, DhanHistoricalClient, candles_to_df
from data.dhanlive.dhan_db import resolve_dhan_identity
from utils.fallback_telemetry import record_local_fallback_event
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration
from utils.sync import load_tracked_symbols, normalize_date_window, parse_datetime_arg


DEFAULT_DAILY_YEARS = 5
DEFAULT_INTRADAY_DAYS = 1
DEFAULT_DAILY_OVERLAP_DAYS = int(os.getenv("DHAN_DAILY_REFRESH_OVERLAP_DAYS", "7"))
DEFAULT_INTRADAY_OVERLAP_MINUTES = int(os.getenv("DHAN_INTRADAY_REFRESH_OVERLAP_MINUTES", "10"))
INTRADAY_MAX_WINDOW_DAYS = 90
SUPPORTED_INTRADAY_INTERVALS = (1, 5, 15, 25, 60)
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MINUTE = 30


DAILY_TABLE = "dhan_ohlcv_daily"
INTRADAY_TABLE = "dhan_ohlcv_intraday"
OHLCV_SCHEMA_MIGRATION_ID = "20260611_dhan_ohlcv_base_asset_type"
_OHLCV_TABLES_ENSURED = False
STOCKEY_RUN_STATE: dict[str, object] = {}

OHLCV_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {DAILY_TABLE} (
        company_master_id TEXT,
        asset_type TEXT NOT NULL,
        exchange TEXT NOT NULL,
        ticker TEXT NOT NULL,
        security_id BIGINT NOT NULL,
        exchange_segment TEXT NOT NULL,
        instrument TEXT NOT NULL,
        date TIMESTAMPTZ NOT NULL,
        open DOUBLE PRECISION,
        high DOUBLE PRECISION,
        low DOUBLE PRECISION,
        close DOUBLE PRECISION,
        volume DOUBLE PRECISION,
        open_interest DOUBLE PRECISION,
        source_timestamp TIMESTAMPTZ,
        load_ts TIMESTAMPTZ NOT NULL,
        UNIQUE (exchange, security_id, date)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {INTRADAY_TABLE} (
        company_master_id TEXT,
        asset_type TEXT NOT NULL,
        exchange TEXT NOT NULL,
        ticker TEXT NOT NULL,
        security_id BIGINT NOT NULL,
        exchange_segment TEXT NOT NULL,
        instrument TEXT NOT NULL,
        interval_minutes INTEGER NOT NULL,
        "timestamp" TIMESTAMPTZ NOT NULL,
        open DOUBLE PRECISION,
        high DOUBLE PRECISION,
        low DOUBLE PRECISION,
        close DOUBLE PRECISION,
        volume DOUBLE PRECISION,
        open_interest DOUBLE PRECISION,
        load_ts TIMESTAMPTZ NOT NULL,
        UNIQUE (exchange, security_id, interval_minutes, "timestamp")
    )
    """,
    f"ALTER TABLE {DAILY_TABLE} ALTER COLUMN company_master_id DROP NOT NULL",
    f"ALTER TABLE {INTRADAY_TABLE} ALTER COLUMN company_master_id DROP NOT NULL",
    f"ALTER TABLE {DAILY_TABLE} ADD COLUMN IF NOT EXISTS asset_type TEXT",
    f"ALTER TABLE {INTRADAY_TABLE} ADD COLUMN IF NOT EXISTS asset_type TEXT",
    f"UPDATE {DAILY_TABLE} SET asset_type = 'stock' WHERE asset_type IS NULL",
    f"UPDATE {INTRADAY_TABLE} SET asset_type = 'stock' WHERE asset_type IS NULL",
    f"ALTER TABLE {DAILY_TABLE} ALTER COLUMN asset_type SET NOT NULL",
    f"ALTER TABLE {INTRADAY_TABLE} ALTER COLUMN asset_type SET NOT NULL",
]


def _record_ohlcv_bulk_sync_fallback(
    *,
    fallback_type: str,
    source: str,
    ticker: str,
    exchange: str,
    asset_type: str,
    error: Exception,
    metadata: dict[str, object] | None = None,
) -> None:
    record_local_fallback_event(
        module="data.dhanlive.ohlcv",
        fallback_type=fallback_type,
        source=source,
        severity="warn",
        reason=(
            "Dhan OHLCV bulk sync failed for one symbol; the batch continues, but downstream "
            "prices, technical features, and advisory decisions may be stale for that symbol."
        ),
        error=error,
        metadata={
            "ticker": ticker,
            "exchange": exchange.upper(),
            "asset_type": asset_type,
            **(metadata or {}),
        },
    )


def _to_naive_utc_datetime(value: object) -> datetime | None:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        return None
    return timestamp.tz_convert("UTC").tz_localize(None).to_pydatetime()


def ensure_ohlcv_tables() -> None:
    global _OHLCV_TABLES_ENSURED
    if _OHLCV_TABLES_ENSURED:
        return
    apply_schema_migration(
        migration_id=OHLCV_SCHEMA_MIGRATION_ID,
        description="Create and normalize Dhan daily/intraday OHLCV tables.",
        statements=OHLCV_SCHEMA_STATEMENTS,
        metadata={"module": "data.dhanlive.ohlcv", "tables": [DAILY_TABLE, INTRADAY_TABLE]},
    )
    _OHLCV_TABLES_ENSURED = True


def latest_daily_snapshot(identifier: str, exchange: str, asset_type: str) -> dict[str, datetime | None]:
    identity = resolve_dhan_identity(identifier, exchange, asset_type=asset_type)
    df = sql_to_df(
        f"""
        SELECT
            MIN(date) AS min_date,
            MAX(date) AS max_date,
            MAX(load_ts) AS max_load_ts
        FROM {DAILY_TABLE}
        WHERE security_id = %s
          AND exchange = %s
          AND asset_type = %s
        """,
        params=(identity["security_id"], exchange.upper(), asset_type.lower()),
    )
    if df.empty:
        return {"min_date": None, "max_date": None, "max_load_ts": None}
    row = df.iloc[0]
    result: dict[str, datetime | None] = {}
    for key in ["min_date", "max_date", "max_load_ts"]:
        result[key] = _to_naive_utc_datetime(row.get(key))
    return result


def latest_intraday_timestamp(
    *,
    security_id: object,
    exchange: str,
    asset_type: str,
    interval_minutes: int,
) -> datetime | None:
    df = sql_to_df(
        f"""
        SELECT MAX("timestamp") AS max_timestamp
        FROM {INTRADAY_TABLE}
        WHERE security_id = %s
          AND exchange = %s
          AND asset_type = %s
          AND interval_minutes = %s
        """,
        params=(security_id, exchange.upper(), asset_type.lower(), int(interval_minutes)),
    )
    if df.empty:
        return None
    return _to_naive_utc_datetime(df.iloc[0].get("max_timestamp"))


def has_recent_adjustment(symbol: str, latest_stored_date: datetime | None) -> bool:
    if latest_stored_date is None:
        return False

    try:
        df = sql_to_df(
            """
            SELECT 1
            FROM nseindia_corporate_actions_normalized
            WHERE symbol = %s
              AND action_type IN ('split', 'bonus')
              AND date >= %s
            LIMIT 1
            """,
            params=(symbol, latest_stored_date),
        )
    except Exception as exc:
        message = str(exc).lower()
        if exc.__class__.__name__ in {"UndefinedTable", "UndefinedColumn"} or "does not exist" in message:
            print(f"[dhan.ohlcv] corporate actions normalized table unavailable for adjustment check: {exc}", flush=True)
            return False
        raise
    return not df.empty


def choose_daily_refresh_start(
    ticker: str,
    exchange: str,
    asset_type: str,
    explicit_from_date: datetime | None,
    years: int = DEFAULT_DAILY_YEARS,
) -> datetime:
    if explicit_from_date is not None:
        return explicit_from_date
    snapshot = latest_daily_snapshot(ticker, exchange, asset_type)
    latest_stored = snapshot.get("max_date")
    if latest_stored is not None:
        if has_recent_adjustment(ticker, latest_stored):
            return datetime.now() - timedelta(days=365 * years)
        return latest_stored - timedelta(days=max(0, DEFAULT_DAILY_OVERLAP_DAYS))
    return datetime.now() - timedelta(days=365 * years)


def load_nse_holidays() -> set[date]:
    try:
        df = sql_to_df(
            """
            SELECT DISTINCT date::date AS holiday_date
            FROM nseindia_holidays
            WHERE type = 'CM'
            """
        )
    except Exception as exc:
        message = str(exc).lower()
        if exc.__class__.__name__ in {"UndefinedTable", "UndefinedColumn"} or "does not exist" in message:
            print(f"[dhan.ohlcv] nseindia_holidays table unavailable; continuing without holiday calendar: {exc}", flush=True)
            return set()
        raise

    if df.empty or "holiday_date" not in df.columns:
        return set()

    values = pd.to_datetime(df["holiday_date"], errors="coerce").dropna()
    return {value.date() for value in values}


def clamp_to_last_trading_day(target: datetime, *, exchange: str = "NSE") -> datetime:
    current = target
    holidays = load_nse_holidays() if exchange.upper() == "NSE" else set()
    while current.weekday() >= 5 or current.date() in holidays:
        current = current - timedelta(days=1)
    return current


def choose_daily_refresh_end(
    to_date: datetime | None,
    *,
    exchange: str,
) -> datetime:
    requested = to_date or datetime.now()
    return clamp_to_last_trading_day(requested, exchange=exchange)


def is_no_data_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "no data present" in message or "incorrect parameters" in message


def classify_symbol_sync_error(error: object) -> str:
    text = str(error or "").lower()
    if "access token is invalid or expired" in text or "status 401" in text or "unauthorized" in text:
        return "auth_unavailable"
    if "no dhan security id mapped" in text or "no dhan index security id mapped" in text:
        return "reference_mapping_missing"
    if "timed out" in text or "timeout" in text or "status 502" in text or "status 503" in text or "status 504" in text:
        return "source_unavailable"
    if "no data present" in text or "incorrect parameters" in text:
        return "no_data"
    return "failed"


def with_market_close(target: datetime) -> datetime:
    return target.replace(
        hour=MARKET_CLOSE_HOUR,
        minute=MARKET_CLOSE_MINUTE,
        second=0,
        microsecond=0,
    )


def choose_intraday_refresh_end(
    to_date: datetime | None,
    *,
    exchange: str,
) -> datetime:
    requested = to_date or datetime.now()
    clamped = clamp_to_last_trading_day(requested, exchange=exchange)
    if clamped.date() != requested.date():
        return with_market_close(clamped)
    if to_date is not None and requested.time() == datetime.min.time():
        return with_market_close(clamped)
    return requested


def normalize_daily_frame(df: pd.DataFrame, identity: dict[str, object]) -> pd.DataFrame:
    if df.empty:
        return df

    normalized = df.copy()
    trade_dates = (
        normalized["source_timestamp"]
        .dt.tz_convert("Asia/Kolkata")
        .dt.strftime("%Y-%m-%d")
    )
    normalized["date"] = pd.to_datetime(trade_dates, utc=True)
    normalized["company_master_id"] = identity["company_master_id"]
    normalized["asset_type"] = identity["asset_type"]
    normalized["exchange"] = identity["exchange"]
    normalized["ticker"] = identity["ticker"]
    normalized["security_id"] = identity["security_id"]
    normalized["exchange_segment"] = identity["exchange_segment"]
    normalized["instrument"] = identity["instrument"]
    normalized["load_ts"] = pd.Timestamp.utcnow()
    ordered_columns = [
        "company_master_id",
        "asset_type",
        "exchange",
        "ticker",
        "security_id",
        "exchange_segment",
        "instrument",
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "open_interest",
        "source_timestamp",
        "load_ts",
    ]
    for column in ordered_columns:
        if column not in normalized.columns:
            normalized[column] = pd.NA
    for column in ["open", "high", "low", "close", "volume", "open_interest"]:
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    normalized = normalized[ordered_columns]
    return normalized.drop_duplicates(subset=["exchange", "security_id", "date"], keep="last")


def normalize_intraday_frame(
    df: pd.DataFrame,
    identity: dict[str, object],
    interval_minutes: int,
) -> pd.DataFrame:
    if df.empty:
        return df

    normalized = df.copy()
    normalized['timestamp'] = normalized["source_timestamp"]
    normalized["company_master_id"] = identity["company_master_id"]
    normalized["asset_type"] = identity["asset_type"]
    normalized["exchange"] = identity["exchange"]
    normalized["ticker"] = identity["ticker"]
    normalized["security_id"] = identity["security_id"]
    normalized["exchange_segment"] = identity["exchange_segment"]
    normalized["instrument"] = identity["instrument"]
    normalized["interval_minutes"] = interval_minutes
    normalized["load_ts"] = pd.Timestamp.utcnow()
    ordered_columns = [
        "company_master_id",
        "asset_type",
        "exchange",
        "ticker",
        "security_id",
        "exchange_segment",
        "instrument",
        "interval_minutes",
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "open_interest",
        "load_ts",
    ]
    for column in ordered_columns:
        if column not in normalized.columns:
            normalized[column] = pd.NA
    for column in ["open", "high", "low", "close", "volume", "open_interest"]:
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    normalized = normalized[ordered_columns]
    return normalized.drop_duplicates(
        subset=["exchange", "security_id", "interval_minutes", "timestamp"],
        keep="last",
    )


def sync_daily_ohlcv(
    ticker: str,
    *,
    exchange: str = "NSE",
    asset_type: str = "stock",
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    client: DhanHistoricalClient | None = None,
) -> pd.DataFrame:
    ensure_ohlcv_tables()
    identity = resolve_dhan_identity(ticker, exchange, asset_type=asset_type)
    effective_from_date, requested_to_date = normalize_date_window(
        choose_daily_refresh_start(ticker, exchange, asset_type, from_date),
        to_date,
    )
    resolved_exchange = str(identity["exchange"])
    effective_to_date = choose_daily_refresh_end(requested_to_date, exchange=resolved_exchange)
    if effective_from_date > effective_to_date:
        return pd.DataFrame()

    api_client = client or DhanHistoricalClient()
    try:
        payload = api_client.fetch_daily(
            security_id=identity["security_id"],
            exchange_segment=str(identity["exchange_segment"]),
            instrument=str(identity["instrument"]),
            from_date=effective_from_date,
            to_date=effective_to_date,
        )
    except DhanAPIError as exc:
        if not is_no_data_error(exc):
            raise
        fallback_to_date = clamp_to_last_trading_day(
            effective_to_date - timedelta(days=1),
            exchange=resolved_exchange,
        )
        if fallback_to_date >= effective_to_date or effective_from_date > fallback_to_date:
            return pd.DataFrame()
        print(
            {
                "mode": "daily",
                "ticker": ticker,
                "asset_type": asset_type,
                "exchange": exchange.upper(),
                "resolved_exchange": resolved_exchange,
                "warning": "dhan_no_data_retry",
                "from_date": effective_from_date.strftime("%Y-%m-%d"),
                "requested_to_date": requested_to_date.strftime("%Y-%m-%d"),
                "retry_to_date": fallback_to_date.strftime("%Y-%m-%d"),
            },
            file=sys.stderr,
            flush=True,
        )
        payload = api_client.fetch_daily(
            security_id=identity["security_id"],
            exchange_segment=str(identity["exchange_segment"]),
            instrument=str(identity["instrument"]),
            from_date=effective_from_date,
            to_date=fallback_to_date,
        )
    df = normalize_daily_frame(candles_to_df(payload), identity)
    if df.empty:
        return df

    upsert_to_db(
        df,
        DAILY_TABLE,
        unique_keys=["exchange", "security_id", "date"],
        timescaledb_column="date",
    )
    return df


def _intraday_windows(from_date: datetime, to_date: datetime) -> Iterable[tuple[datetime, datetime]]:
    current = from_date
    while current < to_date:
        window_end = min(current + timedelta(days=INTRADAY_MAX_WINDOW_DAYS), to_date)
        yield current, window_end
        current = window_end


def sync_intraday_ohlcv(
    ticker: str,
    *,
    exchange: str = "NSE",
    asset_type: str = "stock",
    interval_minutes: int = 1,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    client: DhanHistoricalClient | None = None,
) -> pd.DataFrame:
    if interval_minutes not in SUPPORTED_INTRADAY_INTERVALS:
        raise ValueError(f"Unsupported interval: {interval_minutes}")

    ensure_ohlcv_tables()
    identity = resolve_dhan_identity(ticker, exchange, asset_type=asset_type)
    effective_from_date, effective_to_date = normalize_date_window(from_date, to_date)
    if from_date is None:
        latest_stored = latest_intraday_timestamp(
            security_id=identity["security_id"],
            exchange=str(identity["exchange"]),
            asset_type=asset_type,
            interval_minutes=interval_minutes,
        )
        if latest_stored is not None:
            effective_from_date = latest_stored - timedelta(minutes=max(0, DEFAULT_INTRADAY_OVERLAP_MINUTES))
        else:
            effective_from_date = datetime.now() - timedelta(days=DEFAULT_INTRADAY_DAYS)
    resolved_exchange = str(identity["exchange"])
    effective_to_date = choose_intraday_refresh_end(effective_to_date if to_date is not None else None, exchange=resolved_exchange)
    if effective_from_date > effective_to_date:
        return pd.DataFrame()
    api_client = client or DhanHistoricalClient()
    frames: list[pd.DataFrame] = []
    for window_start, window_end in _intraday_windows(effective_from_date, effective_to_date):
        try:
            payload = api_client.fetch_intraday(
                security_id=identity["security_id"],
                exchange_segment=str(identity["exchange_segment"]),
                instrument=str(identity["instrument"]),
                interval_minutes=interval_minutes,
                from_datetime=window_start,
                to_datetime=window_end,
            )
        except DhanAPIError as exc:
            if not is_no_data_error(exc):
                raise
            print(
                {
                    "mode": "intraday",
                    "ticker": ticker,
                    "asset_type": asset_type,
                    "exchange": exchange.upper(),
                    "resolved_exchange": resolved_exchange,
                    "interval_minutes": interval_minutes,
                    "warning": "dhan_no_data_skip",
                    "from_datetime": window_start.strftime("%Y-%m-%d %H:%M:%S"),
                    "to_datetime": window_end.strftime("%Y-%m-%d %H:%M:%S"),
                },
                file=sys.stderr,
                flush=True,
            )
            continue
        frame = normalize_intraday_frame(candles_to_df(payload), identity, interval_minutes)
        if not frame.empty:
            frames.append(frame)

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(
        subset=["exchange", "security_id", "interval_minutes", "timestamp"],
        keep="last",
    )
    upsert_to_db(
        df,
        INTRADAY_TABLE,
        unique_keys=["exchange", "security_id", "interval_minutes", "timestamp"],
        timescaledb_column="timestamp",
    )
    return df


def sync_many_daily(
    tickers: Iterable[str],
    *,
    exchange: str = "NSE",
    asset_type: str = "stock",
    from_date: datetime | None = None,
    to_date: datetime | None = None,
) -> list[dict[str, object]]:
    client = DhanHistoricalClient()
    results: list[dict[str, object]] = []
    for ticker in tickers:
        try:
            df = sync_daily_ohlcv(
                ticker,
                exchange=exchange,
                asset_type=asset_type,
                from_date=from_date,
                to_date=to_date,
                client=client,
            )
            results.append(
                {
                    "ticker": ticker,
                    "asset_type": asset_type,
                    "exchange": exchange.upper(),
                    "rows": len(df),
                    "from_date": None if df.empty else str(df["date"].min()),
                    "to_date": None if df.empty else str(df["date"].max()),
                }
            )
        except (DhanAPIError, ValueError) as exc:
            error_text = f"{exc.__class__.__name__}: {exc}"
            classification = classify_symbol_sync_error(error_text)
            _record_ohlcv_bulk_sync_fallback(
                fallback_type="dhan_ohlcv_daily_symbol_sync_failed",
                source=DAILY_TABLE,
                ticker=ticker,
                exchange=exchange,
                asset_type=asset_type,
                error=exc,
                metadata={
                    "from_date": None if from_date is None else str(from_date),
                    "to_date": None if to_date is None else str(to_date),
                },
            )
            print(
                {
                    "mode": "daily",
                    "ticker": ticker,
                    "asset_type": asset_type,
                    "exchange": exchange.upper(),
                    "warning": "symbol_sync_failed",
                    "classification": classification,
                    "error": error_text,
                },
                file=sys.stderr,
                flush=True,
            )
            results.append(
                {
                    "ticker": ticker,
                    "asset_type": asset_type,
                    "exchange": exchange.upper(),
                    "rows": 0,
                    "from_date": None,
                    "to_date": None,
                    "classification": classification,
                    "error": error_text,
                }
            )
    return results


def sync_many_intraday(
    tickers: Iterable[str],
    *,
    exchange: str = "NSE",
    asset_type: str = "stock",
    interval_minutes: int = 1,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
) -> list[dict[str, object]]:
    client = DhanHistoricalClient()
    results: list[dict[str, object]] = []
    for ticker in tickers:
        try:
            df = sync_intraday_ohlcv(
                ticker,
                exchange=exchange,
                asset_type=asset_type,
                interval_minutes=interval_minutes,
                from_date=from_date,
                to_date=to_date,
                client=client,
            )
            results.append(
                {
                    "ticker": ticker,
                    "asset_type": asset_type,
                    "exchange": exchange.upper(),
                    "interval_minutes": interval_minutes,
                    "rows": len(df),
                    "from_timestamp": None if df.empty else str(df["timestamp"].min()),
                    "to_timestamp": None if df.empty else str(df["timestamp"].max()),
                }
            )
        except (DhanAPIError, ValueError) as exc:
            error_text = f"{exc.__class__.__name__}: {exc}"
            classification = classify_symbol_sync_error(error_text)
            _record_ohlcv_bulk_sync_fallback(
                fallback_type="dhan_ohlcv_intraday_symbol_sync_failed",
                source=INTRADAY_TABLE,
                ticker=ticker,
                exchange=exchange,
                asset_type=asset_type,
                error=exc,
                metadata={
                    "interval_minutes": interval_minutes,
                    "from_date": None if from_date is None else str(from_date),
                    "to_date": None if to_date is None else str(to_date),
                },
            )
            print(
                {
                    "mode": "intraday",
                    "ticker": ticker,
                    "asset_type": asset_type,
                    "exchange": exchange.upper(),
                    "interval_minutes": interval_minutes,
                    "warning": "symbol_sync_failed",
                    "classification": classification,
                    "error": error_text,
                },
                file=sys.stderr,
                flush=True,
            )
            results.append(
                {
                    "ticker": ticker,
                    "asset_type": asset_type,
                    "exchange": exchange.upper(),
                    "interval_minutes": interval_minutes,
                    "rows": 0,
                    "from_timestamp": None,
                    "to_timestamp": None,
                    "classification": classification,
                    "error": error_text,
                }
            )
    return results


def classify_run_state(rows: list[dict[str, object]], *, rows_written: int) -> str:
    errors = [row for row in rows if row.get("error")]
    if errors:
        classifications = [str(row.get("classification") or classify_symbol_sync_error(row.get("error"))) for row in errors]
        if rows_written > 0:
            return "partial_failed"
        if classifications and all(item == "auth_unavailable" for item in classifications):
            return "auth_unavailable"
        if classifications and all(item == "reference_mapping_missing" for item in classifications):
            return "reference_mapping_missing"
        if classifications and all(item == "source_unavailable" for item in classifications):
            return "source_unavailable"
        if classifications and all(item == "no_data" for item in classifications):
            return "no_data"
        return "failed"
    return "ok" if rows_written > 0 else "no_data"


def build_run_state(
    *,
    daily_results: list[dict[str, object]],
    intraday_results: list[dict[str, object]],
    only: str,
    symbols: list[str],
    exchange: str,
    asset_type: str,
    interval_minutes: int,
) -> dict[str, object]:
    rows = [*daily_results, *intraday_results]
    dates = [
        str(value)
        for row in daily_results
        for value in [row.get("from_date"), row.get("to_date")]
        if value
    ]
    timestamps = [
        str(value)
        for row in intraday_results
        for value in [row.get("from_timestamp"), row.get("to_timestamp")]
        if value
    ]
    errors = [row for row in rows if row.get("error")]
    rows_written = int(sum(int(row.get("rows") or 0) for row in rows))
    classifications = [
        str(row.get("classification") or classify_symbol_sync_error(row.get("error")))
        for row in errors
    ]
    classification_counts = {key: classifications.count(key) for key in sorted(set(classifications))}
    no_data_count = sum(1 for row in rows if not row.get("error") and int(row.get("rows") or 0) == 0)
    source_unavailable_count = classification_counts.get("source_unavailable", 0)
    auth_unavailable_count = classification_counts.get("auth_unavailable", 0)
    reference_mapping_missing_count = classification_counts.get("reference_mapping_missing", 0)
    classification = classify_run_state(rows, rows_written=rows_written)
    return {
        "from_date": min(dates) if dates else None,
        "to_date": max(dates) if dates else None,
        "from_datetime": min(timestamps) if timestamps else None,
        "to_datetime": max(timestamps) if timestamps else None,
        "rows": rows_written,
        "rows_written": rows_written,
        "rows_read": rows_written,
        "classification": classification,
        "status": "ok" if classification in {"ok", "no_data"} else "failed",
        "fallback_used": False,
        "daily_rows": int(sum(int(row.get("rows") or 0) for row in daily_results)),
        "intraday_rows": int(sum(int(row.get("rows") or 0) for row in intraday_results)),
        "symbol_count": len(symbols),
        "error_count": len(errors),
        "no_data_count": no_data_count,
        "source_unavailable_count": source_unavailable_count,
        "auth_unavailable_count": auth_unavailable_count,
        "reference_mapping_missing_count": reference_mapping_missing_count,
        "classification_counts": classification_counts,
        "state_advanced": rows_written > 0,
        "failed_symbols": [str(row.get("ticker") or "") for row in errors if row.get("ticker")],
        "only": only,
        "exchange": exchange.upper(),
        "asset_type": asset_type,
        "interval_minutes": int(interval_minutes),
    }


def main() -> None:
    global STOCKEY_RUN_STATE
    parser = argparse.ArgumentParser(description="Sync Dhan OHLCV history for tracked symbols")
    parser.add_argument("--symbols", nargs="*", help="Symbols, comma-separated or repeated")
    parser.add_argument("--exchange", default="NSE", choices=["NSE", "BSE"], help="Cash equity exchange")
    parser.add_argument(
        "--asset-type",
        default="stock",
        choices=["stock", "index", "benchmark"],
        help="What the provided symbol(s) represent",
    )
    parser.add_argument("--from-date", dest="from_date", help="Start date in YYYY-MM-DD")
    parser.add_argument("--to-date", dest="to_date", help="End date in YYYY-MM-DD")
    parser.add_argument(
        "--intraday-interval",
        dest="intraday_interval",
        type=int,
        choices=SUPPORTED_INTRADAY_INTERVALS,
        default=1,
        help="Intraday interval to sync when intraday is enabled",
    )
    parser.add_argument(
        "--only",
        choices=["daily", "intraday", "both"],
        default="both",
        help="Whether to sync only daily candles, only intraday candles, or both",
    )
    args = parser.parse_args()

    symbols = load_tracked_symbols(args.symbols)
    if not symbols:
        raise SystemExit("No symbols provided. Use --symbols or STOCKEY_SYMBOLS.")

    ensure_ohlcv_tables()
    from_date = parse_datetime_arg(args.from_date)
    to_date = parse_datetime_arg(args.to_date)

    try:
        daily_results: list[dict[str, object]] = []
        intraday_results: list[dict[str, object]] = []
        if args.only in {"daily", "both"}:
            daily_results = sync_many_daily(
                symbols,
                exchange=args.exchange,
                asset_type=args.asset_type,
                from_date=from_date,
                to_date=to_date,
            )
            for row in daily_results:
                print({"mode": "daily", **row}, flush=True)

        if args.only in {"intraday", "both"}:
            intraday_results = sync_many_intraday(
                symbols,
                exchange=args.exchange,
                asset_type=args.asset_type,
                interval_minutes=args.intraday_interval,
                from_date=from_date,
                to_date=to_date,
            )
            for row in intraday_results:
                print({"mode": "intraday", **row}, flush=True)
        STOCKEY_RUN_STATE = build_run_state(
            daily_results=daily_results,
            intraday_results=intraday_results,
            only=str(args.only),
            symbols=symbols,
            exchange=str(args.exchange),
            asset_type=str(args.asset_type),
            interval_minutes=int(args.intraday_interval),
        )
    except (DhanAPIError, DhanAuthError) as exc:
        raise SystemExit(str(exc))


if __name__ == "__main__":
    main()
