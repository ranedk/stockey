from __future__ import annotations

from datetime import datetime
import sys

import pandas as pd

from data.dhanlive.client import DhanAPIError
from data.dhanlive.ohlcv import sync_daily_ohlcv
from data.sharpelydata.sharpely_data import sync_sharpely_data
from utils.sync import get_db_max_date


def _normalize_target_day(to_date: datetime | pd.Timestamp | None) -> datetime:
    ts = pd.Timestamp(to_date or datetime.today())
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts.normalize().to_pydatetime()


def _normalize_db_day(value: datetime | pd.Timestamp | None) -> datetime | None:
    if value is None:
        return None
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts.normalize().to_pydatetime()


def ensure_symbol_fundamentals(
    symbols: list[str],
    *,
    to_date: datetime | pd.Timestamp | None = None,
) -> list[dict[str, object]]:
    target_day = _normalize_target_day(to_date)
    results: list[dict[str, object]] = []
    for symbol in sorted({str(value).upper() for value in symbols if value}):
        latest_stmt = get_db_max_date("stmt_income", filters={"symbol": symbol})
        latest_meta = get_db_max_date("sharpely_stock_meta", date_column="as_on_date", filters={"symbol": symbol})
        latest_peers = get_db_max_date("sharpely_stock_peers", date_column="as_on_date", filters={"anchor_symbol": symbol})
        latest_meta_day = _normalize_db_day(latest_meta)
        latest_peers_day = _normalize_db_day(latest_peers)
        needs_sync = (
            latest_stmt is None
            or latest_meta is None
            or latest_peers is None
            or latest_meta_day < target_day
            or latest_peers_day < target_day
        )
        if not needs_sync:
            results.append({"symbol": symbol, "action": "skip", "reason": "fundamentals_present"})
            continue
        sync_sharpely_data([symbol], from_date=None, to_date=target_day)
        results.append({"symbol": symbol, "action": "sync", "reason": "fundamentals_missing_or_stale"})
    return results


def ensure_symbol_ohlcv(
    symbols: list[str],
    *,
    to_date: datetime | pd.Timestamp | None = None,
) -> list[dict[str, object]]:
    target_day = _normalize_target_day(to_date)
    results: list[dict[str, object]] = []
    for symbol in sorted({str(value).upper() for value in symbols if value}):
        latest = get_db_max_date(
            "dhan_ohlcv_daily",
            filters={"ticker": symbol, "asset_type": "stock", "exchange": "NSE"},
        )
        from_date = None
        if latest is not None:
            latest_day = _normalize_db_day(latest)
            from_date = latest_day + pd.Timedelta(days=1) if latest_day is not None else None
        if from_date is not None and from_date > target_day:
            results.append({"symbol": symbol, "action": "skip", "reason": "ohlcv_present"})
            continue
        try:
            df = sync_daily_ohlcv(
                symbol,
                exchange="NSE",
                asset_type="stock",
                from_date=from_date,
                to_date=target_day,
            )
            results.append(
                {
                    "symbol": symbol,
                    "action": "sync",
                    "rows": int(len(df)),
                    "reason": "ohlcv_missing_or_stale",
                }
            )
        except (DhanAPIError, ValueError) as exc:
            error_text = str(exc).lower()
            if "no data present" in error_text:
                results.append({"symbol": symbol, "action": "skip", "reason": "ohlcv_no_new_data"})
                continue
            result = {
                "symbol": symbol,
                "action": "issue",
                "reason": "dhan_daily_sync_failed",
                "error_type": exc.__class__.__name__,
                "error": str(exc),
            }
            print(
                f"[advisory.data_sync] daily OHLCV issue symbol={symbol} error_type={exc.__class__.__name__} error={exc}",
                file=sys.stderr,
                flush=True,
            )
            results.append(result)
            continue
    return results


def ensure_advisory_symbol_inputs(
    symbols: list[str],
    *,
    to_date: datetime | pd.Timestamp | None = None,
) -> dict[str, object]:
    return {
        "symbols": sorted({str(value).upper() for value in symbols if value}),
        "fundamentals": ensure_symbol_fundamentals(symbols, to_date=to_date),
        "ohlcv": ensure_symbol_ohlcv(symbols, to_date=to_date),
    }
