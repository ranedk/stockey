from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from utils.fallback_telemetry import record_local_fallback_event
from utils.db import sql_to_df


TRADING_DAYS_TABLE = "dim_trading_days"
DEFAULT_MARKET_TIMEZONE = "Asia/Kolkata"


def _market_calendar_date(value: Any | None = None, *, timezone: str = DEFAULT_MARKET_TIMEZONE) -> pd.Timestamp:
    if value is None:
        market_date = pd.Timestamp.now(tz=timezone).date()
    else:
        parsed = pd.to_datetime(value, errors="raise")
        if getattr(parsed, "tzinfo", None) is not None:
            market_date = pd.Timestamp(parsed).tz_convert(timezone).date()
        else:
            market_date = pd.Timestamp(parsed).date()
    return pd.Timestamp(market_date, tz="UTC")


def latest_trading_day_on_or_before(value: Any | None = None, *, timezone: str = DEFAULT_MARKET_TIMEZONE) -> pd.Timestamp:
    requested = _market_calendar_date(value, timezone=timezone)
    try:
        df = sql_to_df(
            f"""
            SELECT MAX(date) AS latest_trading_date
            FROM {TRADING_DAYS_TABLE}
            WHERE date <= %s
            """,
            params=(requested,),
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.advisory_date",
            fallback_type="advisory_date_latest_trading_day_lookup_failed",
            source=TRADING_DAYS_TABLE,
            severity="error",
            reason="Could not resolve latest trading day for advisory wrapper date pinning.",
            error=exc,
            metadata={"requested_date": requested.isoformat(), "timezone": timezone},
        )
        raise
    if df.empty:
        raise RuntimeError(f"No rows returned from {TRADING_DAYS_TABLE} while resolving latest trading day")
    latest = pd.to_datetime(df.iloc[0].get("latest_trading_date"), utc=True, errors="coerce")
    if pd.isna(latest):
        raise RuntimeError(f"No trading day found in {TRADING_DAYS_TABLE} on or before {requested.date().isoformat()}")
    return latest.normalize()


def build_payload(value: Any | None = None, *, timezone: str = DEFAULT_MARKET_TIMEZONE) -> dict[str, Any]:
    requested = _market_calendar_date(value, timezone=timezone)
    resolved = latest_trading_day_on_or_before(requested, timezone=timezone)
    return {
        "status": "ok",
        "requested_market_date": requested.date().isoformat(),
        "resolved_trading_date": resolved.date().isoformat(),
        "timezone": timezone,
        "source_table": TRADING_DAYS_TABLE,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve the latest trading-day advisory date.")
    parser.add_argument("--date", default=None, help="Calendar date to resolve. Defaults to today's market date.")
    parser.add_argument("--timezone", default=DEFAULT_MARKET_TIMEZONE, help="Market timezone for interpreting today's calendar date.")
    parser.add_argument("--format", choices=["date", "json"], default="date")
    args = parser.parse_args(argv)
    payload = build_payload(args.date, timezone=args.timezone)
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
    else:
        print(payload["resolved_trading_date"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
