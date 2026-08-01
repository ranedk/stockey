from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from utils.fallback_telemetry import record_local_fallback_event
from data.dhanlive.client import DhanHistoricalClient, candles_to_df
from data.dhanlive.dhan_db import (
    get_dhan_ohlcv_daily,
    get_dhan_ohlcv_intraday,
    resolve_dhan_identity,
)
from data.dhanlive.ohlcv import SUPPORTED_INTRADAY_INTERVALS
from utils.sync import parse_datetime_arg


DEFAULT_LAST_MINUTES = 60
DEFAULT_INTERVAL_MINUTES = 5
DEFAULT_DAILY_LOOKBACK_DAYS = 30
DISPLAY_TIMEZONE = "Asia/Kolkata"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Quick OHLCV pull utility for Dhan instruments. "
            "Defaults to NSE equity, 5-minute candles, and the last 60 minutes."
        ),
        epilog=(
            "Examples:\n"
            "  python -m data.dhanlive.ohlcv_pull RELIANCE\n"
            "  python -m data.dhanlive.ohlcv_pull RELIANCE --last-minutes 180\n"
            "  python -m data.dhanlive.ohlcv_pull HDFCBANK --interval-minutes 1\n"
            "  python -m data.dhanlive.ohlcv_pull NIFTY --asset-type benchmark --mode intraday\n"
            "  python -m data.dhanlive.ohlcv_pull RELIANCE --mode daily --last-days 90\n"
            "  python -m data.dhanlive.ohlcv_pull RELIANCE --source db --format json\n"
            "  python -m data.dhanlive.ohlcv_pull RELIANCE --from-datetime 2026-04-09T09:15:00+05:30 --to-datetime 2026-04-09T10:15:00+05:30\n"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("ticker", help="Ticker or identifier, for example RELIANCE, HDFCBANK, NIFTY")
    parser.add_argument("--exchange", default="NSE", choices=["NSE", "BSE"], help="Exchange. Default: NSE")
    parser.add_argument(
        "--asset-type",
        default="stock",
        choices=["stock", "index", "benchmark"],
        help="Instrument type. Default: stock",
    )
    parser.add_argument(
        "--mode",
        default="intraday",
        choices=["intraday", "daily"],
        help="Pull intraday or daily OHLCV. Default: intraday",
    )
    parser.add_argument(
        "--source",
        default="api",
        choices=["api", "db"],
        help="Read directly from Dhan API or from local DB cache. Default: api",
    )
    parser.add_argument(
        "--interval-minutes",
        type=int,
        default=DEFAULT_INTERVAL_MINUTES,
        choices=list(SUPPORTED_INTRADAY_INTERVALS),
        help="Intraday candle interval. Default: 5",
    )
    parser.add_argument(
        "--last-minutes",
        type=int,
        default=DEFAULT_LAST_MINUTES,
        help="Default intraday lookback when explicit datetimes are omitted. Default: 60",
    )
    parser.add_argument(
        "--last-days",
        type=int,
        default=DEFAULT_DAILY_LOOKBACK_DAYS,
        help="Default daily lookback when explicit dates are omitted. Default: 30",
    )
    parser.add_argument("--from-datetime", type=parse_datetime_arg, help="Intraday start timestamp")
    parser.add_argument("--to-datetime", type=parse_datetime_arg, help="Intraday end timestamp")
    parser.add_argument("--from-date", type=parse_datetime_arg, help="Daily start date")
    parser.add_argument("--to-date", type=parse_datetime_arg, help="Daily end date")
    parser.add_argument("--format", default="text", choices=["text", "json"], help="Output format. Default: text")
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max rows shown in text output. Full row count is still reported. Default: 20",
    )
    return parser.parse_args()


def _coerce_timestamp(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    parsed = pd.Timestamp(value)
    if parsed.tzinfo is None:
        parsed = parsed.tz_localize(DISPLAY_TIMEZONE)
    return parsed.tz_convert("UTC").to_pydatetime()


def resolve_intraday_window(args: argparse.Namespace) -> tuple[datetime, datetime]:
    end_dt = _coerce_timestamp(args.to_datetime) or datetime.now(timezone.utc)
    start_dt = _coerce_timestamp(args.from_datetime)
    if start_dt is None:
        start_dt = end_dt - timedelta(minutes=max(int(args.last_minutes), 1))
    return start_dt, end_dt


def resolve_daily_window(args: argparse.Namespace) -> tuple[datetime, datetime]:
    end_dt = _coerce_timestamp(args.to_date) or datetime.now(timezone.utc)
    start_dt = _coerce_timestamp(args.from_date)
    if start_dt is None:
        start_dt = end_dt - timedelta(days=max(int(args.last_days), 1))
    return start_dt, end_dt


def load_api_intraday(args: argparse.Namespace) -> tuple[pd.DataFrame, dict[str, Any]]:
    identity = resolve_dhan_identity(args.ticker, args.exchange, asset_type=args.asset_type)
    start_dt, end_dt = resolve_intraday_window(args)
    client = DhanHistoricalClient()
    payload = client.fetch_intraday(
        security_id=identity["security_id"],
        exchange_segment=str(identity["exchange_segment"]),
        instrument=str(identity["instrument"]),
        interval_minutes=int(args.interval_minutes),
        from_datetime=start_dt,
        to_datetime=end_dt,
    )
    df = candles_to_df(payload)
    return df, {
        "identity": identity,
        "from_datetime": start_dt,
        "to_datetime": end_dt,
        "interval_minutes": int(args.interval_minutes),
    }


def load_api_daily(args: argparse.Namespace) -> tuple[pd.DataFrame, dict[str, Any]]:
    identity = resolve_dhan_identity(args.ticker, args.exchange, asset_type=args.asset_type)
    start_dt, end_dt = resolve_daily_window(args)
    client = DhanHistoricalClient()
    payload = client.fetch_daily(
        security_id=identity["security_id"],
        exchange_segment=str(identity["exchange_segment"]),
        instrument=str(identity["instrument"]),
        from_date=start_dt,
        to_date=end_dt,
    )
    df = candles_to_df(payload)
    return df, {
        "identity": identity,
        "from_date": start_dt,
        "to_date": end_dt,
    }


def load_db_intraday(args: argparse.Namespace) -> tuple[pd.DataFrame, dict[str, Any]]:
    identity = resolve_dhan_identity(args.ticker, args.exchange, asset_type=args.asset_type)
    start_dt, end_dt = resolve_intraday_window(args)
    df = get_dhan_ohlcv_intraday(
        args.ticker,
        exchange=args.exchange,
        asset_type=args.asset_type,
        interval_minutes=int(args.interval_minutes),
        from_timestamp=start_dt.isoformat(),
        to_timestamp=end_dt.isoformat(),
    )
    if "timestamp" in df.columns and "source_timestamp" not in df.columns:
        df["source_timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    return df, {
        "identity": identity,
        "from_datetime": start_dt,
        "to_datetime": end_dt,
        "interval_minutes": int(args.interval_minutes),
    }


def load_db_daily(args: argparse.Namespace) -> tuple[pd.DataFrame, dict[str, Any]]:
    identity = resolve_dhan_identity(args.ticker, args.exchange, asset_type=args.asset_type)
    start_dt, end_dt = resolve_daily_window(args)
    df = get_dhan_ohlcv_daily(
        args.ticker,
        exchange=args.exchange,
        asset_type=args.asset_type,
        from_date=start_dt.isoformat(),
        to_date=end_dt.isoformat(),
    )
    if "date" in df.columns and "source_timestamp" not in df.columns:
        df["source_timestamp"] = pd.to_datetime(df["date"], utc=True, errors="coerce")
    return df, {
        "identity": identity,
        "from_date": start_dt,
        "to_date": end_dt,
    }


def _format_timestamp_for_display(value: object) -> str:
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        return "-"
    return ts.tz_convert(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %Z")


def _text_cell(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        if pd.isna(value):
            return "-"
        return f"{value:.2f}".rstrip("0").rstrip(".")
    text = str(value)
    return text if text else "-"


def render_text(df: pd.DataFrame, summary: dict[str, Any], limit: int) -> str:
    lines = [
        f"ticker: {summary['requested_ticker']}",
        f"resolved: {summary['resolved_exchange']}:{summary['resolved_ticker']} security_id={summary['security_id']}",
        f"asset_type: {summary['asset_type']}",
        f"mode: {summary['mode']}",
        f"source: {summary['source']}",
        f"row_count: {summary['row_count']}",
    ]
    if summary["mode"] == "intraday":
        lines.append(f"interval_minutes: {summary['interval_minutes']}")
        lines.append(f"window: {summary['from_datetime']} -> {summary['to_datetime']}")
    else:
        lines.append(f"window: {summary['from_date']} -> {summary['to_date']}")

    if df.empty:
        lines.append("No rows found.")
        return "\n".join(lines)

    preview = df.copy()
    if "source_timestamp" in preview.columns:
        preview["time"] = preview["source_timestamp"].map(_format_timestamp_for_display)
    elif "date" in preview.columns:
        preview["time"] = preview["date"].map(_format_timestamp_for_display)
    else:
        preview["time"] = "-"
    columns = [("time", 23), ("open", 10), ("high", 10), ("low", 10), ("close", 10), ("volume", 12)]
    header = " ".join(label.ljust(width) for label, width in columns)
    separator = " ".join("-" * width for _, width in columns)
    lines.extend(["", header, separator])
    for _, row in preview.head(max(int(limit), 1)).iterrows():
        values = [
            _text_cell(row.get("time"))[:23],
            _text_cell(pd.to_numeric(row.get("open"), errors="coerce"))[:10],
            _text_cell(pd.to_numeric(row.get("high"), errors="coerce"))[:10],
            _text_cell(pd.to_numeric(row.get("low"), errors="coerce"))[:10],
            _text_cell(pd.to_numeric(row.get("close"), errors="coerce"))[:10],
            _text_cell(pd.to_numeric(row.get("volume"), errors="coerce"))[:12],
        ]
        lines.append(" ".join(value.ljust(width) for value, (_, width) in zip(values, columns)))
    if len(preview) > int(limit):
        lines.append(f"... showing first {limit} of {len(preview)} rows")
    return "\n".join(lines)


def build_summary(df: pd.DataFrame, args: argparse.Namespace, meta: dict[str, Any]) -> dict[str, Any]:
    identity = meta["identity"]
    summary = {
        "status": "ok",
        "requested_ticker": args.ticker.upper(),
        "resolved_ticker": str(identity["ticker"]).upper(),
        "resolved_exchange": str(identity["exchange"]).upper(),
        "security_id": int(identity["security_id"]),
        "asset_type": str(identity["asset_type"]),
        "mode": args.mode,
        "source": args.source,
        "row_count": int(len(df)),
    }
    if args.mode == "intraday":
        summary["interval_minutes"] = int(meta["interval_minutes"])
        summary["from_datetime"] = pd.Timestamp(meta["from_datetime"]).tz_convert(DISPLAY_TIMEZONE).isoformat()
        summary["to_datetime"] = pd.Timestamp(meta["to_datetime"]).tz_convert(DISPLAY_TIMEZONE).isoformat()
    else:
        summary["from_date"] = pd.Timestamp(meta["from_date"]).tz_convert(DISPLAY_TIMEZONE).isoformat()
        summary["to_date"] = pd.Timestamp(meta["to_date"]).tz_convert(DISPLAY_TIMEZONE).isoformat()
    summary["sample"] = df.head(10).to_dict(orient="records")
    return summary


def main() -> int:
    args = parse_args()
    if args.mode == "intraday":
        loader = load_db_intraday if args.source == "db" else load_api_intraday
    else:
        loader = load_db_daily if args.source == "db" else load_api_daily

    try:
        df, meta = loader(args)
        summary = build_summary(df, args, meta)
        if args.format == "json":
            print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
        else:
            print(render_text(df, summary, args.limit))
        return 0
    except Exception as exc:
        record_local_fallback_event(
            module="data.dhanlive.ohlcv_pull",
            source=f"dhan_ohlcv_pull:{args.source}:{args.mode}:{args.exchange}:{args.ticker.upper()}",
            fallback_type="dhan_ohlcv_pull_failed",
            severity="warn",
            symbol=args.ticker.upper(),
            reason=(
                "Manual Dhan OHLCV pull failed; requested price data may require Dhan token, identity, "
                "or source-data repair before it can be inspected."
            ),
            error=exc,
            metadata={
                "ticker": args.ticker.upper(),
                "exchange": args.exchange,
                "asset_type": args.asset_type,
                "mode": args.mode,
                "source": args.source,
                "interval_minutes": int(args.interval_minutes),
            },
        )
        payload = {
            "status": "error",
            "requested_ticker": args.ticker.upper(),
            "mode": args.mode,
            "source": args.source,
            "error": f"{exc.__class__.__name__}: {exc}",
        }
        if args.format == "json":
            print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        else:
            print(f"OHLCV pull failed: {payload['error']}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
