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
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
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
IST_OFFSET = timedelta(hours=5, minutes=30)


def _ist_now() -> datetime:
    """Current moment as naive IST wall-clock digits. This module treats every
    naive datetime it handles AS IF it's IST (MARKET_CLOSE_HOUR/MINUTE=15:30 are
    IST values; with_market_close() just does a bare .replace() on whatever it's
    given, with no tz-awareness at all) -- a convention that only holds when the
    host machine's own clock is also IST, or when every naive datetime is
    produced deliberately in IST.

    BUG FOUND LIVE 2026-08-30 (adversarial review, before the first commit of the
    intraday feature): every "live now" fallback in this module used bare
    `datetime.now()` instead. This host's system clock is UTC, so that silently
    returned naive UTC digits, 5.5 hours behind the naive-IST value the rest of
    this module's logic assumes -- and Dhan's intraday chart API reads a naive
    `toDate` as IST wall-clock, not UTC. Confirmed against real stored data:
    4,211 ticker-day rows in dhan_ohlcv_intraday since 2026-08-14 fall short of a
    full session's 375 one-minute bars (as few as 89), every one of them a day
    whose sync ran through this exact untimezoned `datetime.now()` fallback --
    the daily incremental sync (choose_intraday_refresh_end's to_date=None path)
    had been silently truncating each day's close, by an amount that varied with
    exactly when that day's sync happened to run, ever since the feature shipped.
    The one-time 5-year historical backfill is NOT affected by this specific bug
    for its bulk history (each historical day gets an explicit end-of-window
    datetime already correctly stamped to market close by with_market_close());
    only the "live, no explicit to_date" fallback path was ever wrong.

    A raw daily/multi-year lookback (choose_daily_refresh_end's own default, or
    scripts/backfill_intraday_5yr.py's `years` window) is far less sensitive to a
    5.5-hour error, but this is used everywhere "now" means "the actual current
    moment" for consistency -- one correct definition of "now" for this whole
    module, not two."""
    return datetime.now(timezone.utc).replace(tzinfo=None) + IST_OFFSET


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
        WHERE ticker = %s
          AND exchange = %s
          AND asset_type = %s
        """,
        # By the company's ticker, not the security_id (2026-09-24): a stock moving between
        # EQ and BE switches Dhan ids, the new id had no bars, and every switch re-fetched 5
        # years that Dhan serves identically under either id -- 47,376 duplicated
        # company-days across 258 tickers.
        params=(str(identity["ticker"]), str(identity["exchange"]).upper(), asset_type.lower()),
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
            return _ist_now() - timedelta(days=365 * years)
        return latest_stored - timedelta(days=max(0, DEFAULT_DAILY_OVERLAP_DAYS))
    return _ist_now() - timedelta(days=365 * years)


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


def load_special_weekend_trading_days(target_date: date, *, window_days: int = 10) -> set[date] | None:
    """dim_trading_days rows within [target_date - window_days, target_date] -- the codebase's
    own authoritative trading calendar (already used by utils/advisory_date.py and
    ohlcv_reconcile.py). Returns None (not an empty set) on any lookup failure, so callers can
    distinguish "checked, no special sessions in range" from "couldn't check, fall back to the
    plain weekday heuristic" -- same tolerant-degrade shape load_nse_holidays() already uses for
    an unavailable holidays table. Bounded window, not the whole table, since this is called per
    symbol during bulk OHLCV syncs (same repeated-per-call-symbol pattern load_nse_holidays()
    already has -- not a new inefficiency introduced here)."""
    try:
        df = sql_to_df(
            "SELECT date::date AS trading_date FROM dim_trading_days WHERE date BETWEEN %s AND %s",
            params=(target_date - timedelta(days=window_days), target_date),
        )
    except Exception as exc:
        message = str(exc).lower()
        if exc.__class__.__name__ in {"UndefinedTable", "UndefinedColumn"} or "does not exist" in message:
            print(f"[dhan.ohlcv] dim_trading_days table unavailable; continuing with plain weekday clamp: {exc}", flush=True)
            return None
        raise
    if df.empty or "trading_date" not in df.columns:
        return set()
    values = pd.to_datetime(df["trading_date"], errors="coerce").dropna()
    return {value.date() for value in values}


def clamp_to_last_trading_day(target: datetime, *, exchange: str = "NSE") -> datetime:
    # BUG FOUND LIVE 2026-08-19 (re-audit): unconditionally treated every Saturday/Sunday as
    # non-trading, with no reference to dim_trading_days -- which has real weekend NSE sessions
    # (Budget-day/Muhurat/DR-drill sessions), including an upcoming one on 2026-11-08 (confirmed
    # live against the real DB). On a real weekend trading day, this clamped the sync window's
    # end back to the prior Friday, so choose_daily_refresh_end/choose_intraday_refresh_end never
    # requested that day's data on the day itself -- a same-day poll silently looked like
    # "no_data" for what was actually a live session. ohlcv_reconcile.py's own staleness check
    # correctly uses dim_trading_days and would flag the symbol stale, but its sync call went
    # through this same buggy clamp, so even the gap-closer couldn't close this specific gap on
    # the day it mattered.
    #
    # First fix attempt OR'd the special-session check into the weekend branch only, leaving
    # `holidays` free to clamp it anyway -- and immediately failed live on the real 2026-11-08
    # case: NSE's own holiday calendar (nseindia_holidays) lists 2026-11-08 as a holiday AND
    # dim_trading_days lists it as a real trading day, simultaneously true -- it's a Diwali
    # Muhurat trading session, a real, well-known pattern (an evening session on an otherwise-
    # holiday date). dim_trading_days is treated as authoritative here, same as
    # utils/advisory_date.py already treats it elsewhere -- a date it confirms as a real trading
    # day is never clamped, regardless of what the holiday calendar separately says. Falls back
    # to the plain weekday+holiday heuristic if the dim_trading_days lookup itself fails, same
    # tolerant-degrade shape already used for the holidays table.
    current = target
    holidays = load_nse_holidays() if exchange.upper() == "NSE" else set()
    confirmed_trading_days = load_special_weekend_trading_days(target.date()) if exchange.upper() == "NSE" else None
    while True:
        if confirmed_trading_days is not None and current.date() in confirmed_trading_days:
            break
        if current.date() in holidays or current.weekday() >= 5:
            current = current - timedelta(days=1)
            continue
        break
    return current


def choose_daily_refresh_end(
    to_date: datetime | None,
    *,
    exchange: str,
) -> datetime:
    requested = to_date or _ist_now()
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
    requested = to_date or _ist_now()
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
    # BUG FOUND LIVE 2026-08-19 (re-audit): candles_to_df builds source_timestamp with
    # pd.to_datetime(..., errors="coerce"), so a malformed timestamp entry from Dhan becomes NaT
    # rather than raising. date/adj_close's NOT NULL 'date' column was derived from
    # source_timestamp with no dropna -- a single malformed candle would surface as a Postgres
    # NotNullViolation from upsert_to_db, aborting the WHOLE symbol's batch (that exception isn't
    # DhanAPIError/ValueError, so it also hit the sync_many_daily/sync_many_intraday gap fixed
    # above) instead of just dropping the one bad row and keeping the rest. I could not force Dhan
    # to return a malformed payload to reproduce this end-to-end live; fixing the code-verified gap
    # regardless.
    normalized = normalized.dropna(subset=["source_timestamp"])
    if normalized.empty:
        return normalized
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
    # BUG FOUND LIVE 2026-08-19 (re-audit): same gap as normalize_daily_frame above -- a malformed
    # source_timestamp (NaT, from candles_to_df's errors="coerce") flowed straight into the NOT
    # NULL 'timestamp' column with no dropna, risking a NotNullViolation on upsert_to_db that would
    # abort the whole symbol's batch instead of just dropping the one bad row.
    normalized = normalized.dropna(subset=["source_timestamp"])
    if normalized.empty:
        return normalized
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


def alternate_nse_equity_security_ids(identity: dict) -> list[int]:
    """Other ACTIVE Dhan EQUITY ids for the same NSE symbol (the EQ/BE pair)."""
    if str(identity.get("exchange")) != "NSE" or identity.get("asset_type") != "stock":
        return []
    df = sql_to_df(
        """
        SELECT security_id FROM master_dhan_instruments
         WHERE exch_id = 'NSE' AND segment = 'E' AND instrument = 'EQUITY' AND valid_to IS NULL
           AND underlying_symbol = %s AND security_id <> %s
         ORDER BY (series = 'BE') DESC, load_ts DESC
        """,
        params=(str(identity.get("ticker")), int(identity["security_id"])),
    )
    return [] if df.empty else [int(v) for v in df["security_id"].tolist()]


def _fetch_daily_with_alternate_id(api_client, identity: dict, first_error: Exception, *,
                                   from_date, to_date, ticker: str):
    """A stock moved from EQ to BE keeps two active Dhan ids. company_master stores the EQ
    id because Dhan serves INTRADAY under it, but its DAILY endpoint answers 400 "Missing
    required fields" for that id and serves the BE one (measured 2026-09-24; 115 of the
    178 symbols the 2026-09-23 evening reconcile could not fetch). On such an error, try
    the symbol's other active id once; bars are stored under the id that served them."""
    for alternate in alternate_nse_equity_security_ids(identity):
        try:
            payload = api_client.fetch_daily(
                security_id=alternate,
                exchange_segment=str(identity["exchange_segment"]),
                instrument=str(identity["instrument"]),
                from_date=from_date,
                to_date=to_date,
            )
        except DhanAPIError:
            continue
        print({"mode": "daily", "ticker": ticker, "warning": "dhan_daily_served_by_alternate_id",
               "primary_security_id": identity["security_id"], "alternate_security_id": alternate},
              file=sys.stderr, flush=True)
        return payload, {**identity, "security_id": alternate}
    raise first_error


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
        try:
            payload = api_client.fetch_daily(
                security_id=identity["security_id"],
                exchange_segment=str(identity["exchange_segment"]),
                instrument=str(identity["instrument"]),
                from_date=effective_from_date,
                to_date=effective_to_date,
            )
        except DhanAPIError as exc:
            if is_no_data_error(exc) or classify_symbol_sync_error(exc) == "auth_unavailable":
                raise
            payload, identity = _fetch_daily_with_alternate_id(
                api_client, identity, exc, from_date=effective_from_date, to_date=effective_to_date, ticker=ticker)
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
        # update_if_changed, NOT "nothing" -- deliberately different from the intraday
        # write below. choose_daily_refresh_end clamps to the last trading day, which
        # during a live session is TODAY, so a sync running in market hours (the
        # 12:30 IST downloader queue does) stores a PARTIAL bar for today. The
        # post-close reconcile then has to overwrite it with the settled one. Under
        # DO NOTHING that correction is silently dropped and the partial bar is
        # permanent. The predicate still skips the no-op rewrite in the normal case,
        # where a re-fetched settled bar carries values identical to what is stored.
        on_conflict="update_if_changed",
    )
    _drop_same_day_bars_under_other_ids(df, identity)
    return df


def _drop_same_day_bars_under_other_ids(df: pd.DataFrame, identity: dict) -> None:
    """One bar per company per day: the id that just served it wins. The unique key is
    per security_id, so a company whose Dhan id changed (EQ <-> BE) would otherwise keep
    the overlap days twice -- Dhan serves identical bars under both (2026-09-24)."""
    if df.empty or "date" not in df.columns:
        return
    dates = sorted({pd.Timestamp(d) for d in df["date"].dropna()})
    if not dates:
        return

    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute(
                f"DELETE FROM {DAILY_TABLE} WHERE exchange = %s AND ticker = %s AND security_id <> %s "
                "  AND date >= %s AND date <= %s AND date = ANY(%s)",
                (str(identity["exchange"]).upper(), str(identity["ticker"]), int(identity["security_id"]),
                 dates[0], dates[-1], [d.to_pydatetime() for d in dates]),
            )

    execute_db_operation(_op, operation_name=f"{DAILY_TABLE}:drop_other_id_duplicates")


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
            effective_from_date = _ist_now() - timedelta(days=DEFAULT_INTRADAY_DAYS)
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
        # A one-minute bar is immutable once its minute has passed, and the key
        # includes the timestamp -- so a partial session is simply FEWER ROWS, never a
        # wrong row, and the missing minutes insert later as new keys. There is no
        # correction to preserve here, unlike the daily write above. DO NOTHING skips
        # the conflicting rows outright; a DO UPDATE against a COMPRESSED chunk forces
        # decompress -> update -> recompress, all WAL-logged -- the mechanism behind
        # the ~1.6 TB/day WAL and the 2026-08-31 OOM on this 525M-row table. To
        # genuinely rewrite a stored bar, delete it first.
        on_conflict="nothing",
    )
    return df


# BUG FOUND LIVE 2026-08-19 (re-audit): sync_many_daily/sync_many_intraday's per-symbol try/except
# only caught (DhanAPIError, ValueError) -- but DhanHistoricalClient._request has no try/except of
# its own around the actual network call, so a raw requests exception (ConnectionError, Timeout,
# SSLError) propagates uncaught, and candles_to_df can raise KeyError on a malformed/schema-changed
# Dhan payload (missing "timestamp" while other candle arrays are non-empty). Neither is
# DhanAPIError/ValueError, so either one escaped this try/except and aborted the WHOLE remaining
# ticker list instead of being recorded via _record_ohlcv_bulk_sync_fallback for just that one
# symbol -- contradicting this module's own stated design (the fallback reason text literally says
# "the batch continues"). Broadened to Exception so no future exception type can silently reopen
# this same gap; classify_symbol_sync_error's own text-matching already degrades gracefully to a
# generic "failed" classification for anything it doesn't recognize, same as it already does today.
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
        except Exception as exc:  # noqa: BLE001 -- see comment above sync_many_daily/sync_many_intraday
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
        except Exception as exc:  # noqa: BLE001 -- see comment above sync_many_daily/sync_many_intraday
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
