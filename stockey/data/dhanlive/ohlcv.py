from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from typing import Iterable

import pandas as pd

from data.dhanlive.auth import DhanAuthError
from data.dhanlive.client import DhanAPIError, DhanHistoricalClient, candles_to_df
from data.dhanlive.dhan_db import get_company_master_equity
from utils.db import db_session, sql_to_df, upsert_to_db
from utils.sync import load_tracked_symbols, normalize_date_window, parse_datetime_arg


DEFAULT_HISTORY_START = datetime(2000, 1, 1)
DEFAULT_DAILY_OVERLAP_DAYS = 30
INTRADAY_MAX_WINDOW_DAYS = 90
SUPPORTED_INTRADAY_INTERVALS = (1, 5, 15, 25, 60)


DAILY_TABLE = "dhan_ohlcv_daily"
INTRADAY_TABLE = "dhan_ohlcv_intraday"


def ensure_ohlcv_tables() -> None:
    with db_session() as (_, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {DAILY_TABLE} (
                company_master_id TEXT NOT NULL,
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
            """
        )
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {INTRADAY_TABLE} (
                company_master_id TEXT NOT NULL,
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
            """
        )


def resolve_equity_identity(ticker: str, exchange: str) -> dict[str, object]:
    company = get_company_master_equity(ticker, exchange)
    exchange_upper = exchange.upper()
    if exchange_upper == "NSE":
        security_id = company.get("dhan_nse_id")
        resolved_ticker = company.get("nse_ticker")
        exchange_segment = "NSE_EQ"
    elif exchange_upper == "BSE":
        security_id = company.get("dhan_bse_id")
        resolved_ticker = company.get("bse_ticker")
        exchange_segment = "BSE_EQ"
    else:
        raise ValueError(f"Unsupported exchange: {exchange}")

    if pd.isna(security_id):
        raise ValueError(f"No Dhan security id mapped for {exchange_upper}:{ticker}")

    return {
        "company_master_id": company["company_master_id"],
        "exchange": exchange_upper,
        "ticker": str(resolved_ticker).strip(),
        "security_id": int(security_id),
        "exchange_segment": exchange_segment,
        "instrument": "EQUITY",
    }


def latest_daily_snapshot(ticker: str, exchange: str) -> dict[str, datetime | None]:
    company = get_company_master_equity(ticker, exchange)
    df = sql_to_df(
        f"""
        SELECT
            MIN(date) AS min_date,
            MAX(date) AS max_date,
            MAX(load_ts) AS max_load_ts
        FROM {DAILY_TABLE}
        WHERE company_master_id = %s
          AND exchange = %s
        """,
        params=(company["company_master_id"], exchange.upper()),
    )
    if df.empty:
        return {"min_date": None, "max_date": None, "max_load_ts": None}
    row = df.iloc[0]
    result: dict[str, datetime | None] = {}
    for key in ["min_date", "max_date", "max_load_ts"]:
        value = pd.to_datetime(row.get(key), utc=True, errors="coerce")
        result[key] = None if pd.isna(value) else value.to_pydatetime()
    return result


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
    except Exception:
        return False
    return not df.empty


def choose_daily_refresh_start(
    ticker: str,
    exchange: str,
    explicit_from_date: datetime | None,
    overlap_days: int = DEFAULT_DAILY_OVERLAP_DAYS,
) -> datetime:
    if explicit_from_date is not None:
        return explicit_from_date

    snapshot = latest_daily_snapshot(ticker, exchange)
    min_date = snapshot["min_date"]
    max_date = snapshot["max_date"]
    if max_date is None:
        return DEFAULT_HISTORY_START

    if exchange.upper() == "NSE" and has_recent_adjustment(ticker, max_date):
        return min_date or DEFAULT_HISTORY_START

    overlap_start = max_date - timedelta(days=overlap_days)
    return max(overlap_start, min_date or DEFAULT_HISTORY_START)


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
    normalized["exchange"] = identity["exchange"]
    normalized["ticker"] = identity["ticker"]
    normalized["security_id"] = identity["security_id"]
    normalized["exchange_segment"] = identity["exchange_segment"]
    normalized["instrument"] = identity["instrument"]
    normalized["load_ts"] = pd.Timestamp.utcnow()
    ordered_columns = [
        "company_master_id",
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
    normalized["exchange"] = identity["exchange"]
    normalized["ticker"] = identity["ticker"]
    normalized["security_id"] = identity["security_id"]
    normalized["exchange_segment"] = identity["exchange_segment"]
    normalized["instrument"] = identity["instrument"]
    normalized["interval_minutes"] = interval_minutes
    normalized["load_ts"] = pd.Timestamp.utcnow()
    ordered_columns = [
        "company_master_id",
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
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    client: DhanHistoricalClient | None = None,
) -> pd.DataFrame:
    ensure_ohlcv_tables()
    identity = resolve_equity_identity(ticker, exchange)
    effective_from_date, effective_to_date = normalize_date_window(
        choose_daily_refresh_start(ticker, exchange, from_date),
        to_date,
    )
    payload = (client or DhanHistoricalClient()).fetch_daily(
        security_id=identity["security_id"],
        exchange_segment=str(identity["exchange_segment"]),
        instrument=str(identity["instrument"]),
        from_date=effective_from_date,
        to_date=effective_to_date + timedelta(days=1),
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
    interval_minutes: int = 1,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    client: DhanHistoricalClient | None = None,
) -> pd.DataFrame:
    if interval_minutes not in SUPPORTED_INTRADAY_INTERVALS:
        raise ValueError(f"Unsupported interval: {interval_minutes}")

    ensure_ohlcv_tables()
    identity = resolve_equity_identity(ticker, exchange)
    effective_from_date, effective_to_date = normalize_date_window(from_date, to_date)
    if to_date is not None and effective_to_date.time() == datetime.min.time():
        effective_to_date = effective_to_date + timedelta(days=1)
    api_client = client or DhanHistoricalClient()
    frames: list[pd.DataFrame] = []
    for window_start, window_end in _intraday_windows(effective_from_date, effective_to_date):
        payload = api_client.fetch_intraday(
            security_id=identity["security_id"],
            exchange_segment=str(identity["exchange_segment"]),
            instrument=str(identity["instrument"]),
            interval_minutes=interval_minutes,
            from_datetime=window_start,
            to_datetime=window_end,
        )
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
    from_date: datetime | None = None,
    to_date: datetime | None = None,
) -> list[dict[str, object]]:
    client = DhanHistoricalClient()
    results: list[dict[str, object]] = []
    for ticker in tickers:
        df = sync_daily_ohlcv(
            ticker,
            exchange=exchange,
            from_date=from_date,
            to_date=to_date,
            client=client,
        )
        results.append(
            {
                "ticker": ticker,
                "exchange": exchange.upper(),
                "rows": len(df),
                "from_date": None if df.empty else str(df["date"].min()),
                "to_date": None if df.empty else str(df["date"].max()),
            }
        )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Dhan OHLCV history for tracked symbols")
    parser.add_argument("--symbols", nargs="*", help="Symbols, comma-separated or repeated")
    parser.add_argument("--exchange", default="NSE", choices=["NSE", "BSE"], help="Cash equity exchange")
    parser.add_argument("--from-date", dest="from_date", help="Start date in YYYY-MM-DD")
    parser.add_argument("--to-date", dest="to_date", help="End date in YYYY-MM-DD")
    parser.add_argument(
        "--intraday-interval",
        dest="intraday_interval",
        type=int,
        choices=SUPPORTED_INTRADAY_INTERVALS,
        help="Optional intraday interval to sync instead of daily",
    )
    args = parser.parse_args()

    symbols = load_tracked_symbols(args.symbols)
    if not symbols:
        raise SystemExit("No symbols provided. Use --symbols, STOCKEY_SYMBOLS, or config/tracked_symbols.txt")

    ensure_ohlcv_tables()
    from_date = parse_datetime_arg(args.from_date)
    to_date = parse_datetime_arg(args.to_date)

    try:
        if args.intraday_interval:
            for symbol in symbols:
                df = sync_intraday_ohlcv(
                    symbol,
                    exchange=args.exchange,
                    interval_minutes=args.intraday_interval,
                    from_date=from_date,
                    to_date=to_date,
                )
                print(
                    {
                        "ticker": symbol,
                        "exchange": args.exchange,
                        "interval_minutes": args.intraday_interval,
                        "rows": len(df),
                    },
                    flush=True,
                )
            return

        for row in sync_many_daily(
            symbols,
            exchange=args.exchange,
            from_date=from_date,
            to_date=to_date,
        ):
            print(row, flush=True)
    except (DhanAPIError, DhanAuthError) as exc:
        raise SystemExit(str(exc))


if __name__ == "__main__":
    main()
