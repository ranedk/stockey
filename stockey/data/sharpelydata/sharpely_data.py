import argparse
import json
from datetime import datetime

import pandas as pd
from environs import Env

from utils.fallback_telemetry import record_local_fallback_event
from utils.company_master import attach_company_master_id
from utils.db import upsert_to_db
from utils.http import get_with_retries
from utils.sync import choose_from_date, get_db_max_date, load_tracked_symbols, normalize_date_window, parse_datetime_arg

from . import sharpely_utils as su

env = Env()
env.read_env()
HEADERS = su.get_sharpely_headers()
STOCKEY_RUN_STATE: dict[str, object] = {}


def _record_sharpely_fallback(
    *,
    fallback_type: str,
    reason: str,
    error: Exception | None = None,
    symbol: str | None = None,
    metadata: dict[str, object] | None = None,
) -> None:
    record_local_fallback_event(
        module="data.sharpelydata.sharpely_data",
        source="sharpely",
        fallback_type=fallback_type,
        severity="warn",
        symbol=symbol,
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def classify_sharpely_sync_error(error: object) -> str:
    text = str(error or "").lower()
    if "unauthorized" in text or "forbidden" in text or "status 401" in text or "status 403" in text:
        return "auth_unavailable"
    if "timed out" in text or "timeout" in text or "connection" in text or "status 502" in text or "status 503" in text or "status 504" in text:
        return "source_unavailable"
    if "json" in text or "keyerror" in text or "columns" in text:
        return "parse_failed"
    return "failed"


def classify_sharpely_run_state(*, rows_written: int, failures: list[dict[str, object]], symbol_count: int) -> str:
    if symbol_count <= 0:
        return "no_data"
    if failures:
        classifications = [str(row.get("classification") or classify_sharpely_sync_error(row.get("error"))) for row in failures]
        if rows_written > 0:
            return "partial_failed"
        if classifications and all(item == "auth_unavailable" for item in classifications):
            return "auth_unavailable"
        if classifications and all(item == "source_unavailable" for item in classifications):
            return "source_unavailable"
        if classifications and all(item == "parse_failed" for item in classifications):
            return "parse_failed"
        return "failed"
    return "ok" if rows_written > 0 else "no_data"


def filter_by_date_range(df: pd.DataFrame, from_date: datetime | None, to_date: datetime | None) -> pd.DataFrame:
    if df.empty or "date" not in df.columns:
        return df

    series = pd.to_datetime(df["date"], errors="coerce")
    if from_date is not None:
        df = df[series >= pd.Timestamp(from_date)]
        series = pd.to_datetime(df["date"], errors="coerce")
    if to_date is not None:
        df = df[series <= pd.Timestamp(to_date)]
    return df.reset_index(drop=True)


def get_historical_mcap(symbol: str, from_date: datetime | None = None, to_date: datetime | None = None) -> dict[str, object]:
    json_data = {
        "stock": symbol,
        "metric_code": "mcap",
        "frequency": "D",
        "start_date": from_date.strftime("%Y-%m-%d") if from_date else None,
        "end_date": to_date.strftime("%Y-%m-%d") if to_date else None,
    }
    resp = get_with_retries(
        "https://pyapiv2.mintbox.ai/api/core/getHistoricalMetricData",
        headers=HEADERS,
        method="POST",
        json_data=json_data,
    ).json()
    mcap_data = json.loads(resp)

    records = []
    for row in mcap_data:
        records.append({"date": row["timestamp"], "mcap": row["mcap"]})

    df = pd.DataFrame(records)
    if df.empty:
        return {"symbol": symbol, "rows": 0}
    df["date"] = pd.to_datetime(df["date"])
    df["mcap"] = pd.to_numeric(df["mcap"], errors="coerce")
    df["symbol"] = symbol
    df = filter_by_date_range(df, from_date, to_date)
    if df.empty:
        return {"symbol": symbol, "rows": 0}
    df = attach_company_master_id(df, ticker_column="symbol", exchange="NSE")
    upsert_to_db(
        df,
        "historical_mcap",
        unique_keys=["symbol", "date"],
        timescaledb_column="date",
    )
    return {"symbol": symbol, "rows": int(len(df))}


def sync_sharpely_data(symbols: list[str], from_date: datetime | None = None, to_date: datetime | None = None) -> dict[str, object]:
    effective_from_date, to_date = normalize_date_window(from_date, to_date)
    snapshot_target = pd.Timestamp(to_date)
    if snapshot_target.tzinfo is not None:
        snapshot_target = snapshot_target.tz_convert("UTC").tz_localize(None)
    snapshot_target = snapshot_target.normalize()
    to_date = snapshot_target.to_pydatetime()
    normalized_symbols = [str(symbol).strip().upper() for symbol in symbols if str(symbol or "").strip()]
    summary: dict[str, object] = {
        "source": "sharpely_mcap",
        "symbols": normalized_symbols[:100],
        "symbol_count": len(normalized_symbols),
        "from_date": pd.Timestamp(effective_from_date).date().isoformat() if effective_from_date else None,
        "to_date": pd.Timestamp(to_date).date().isoformat() if to_date else None,
        "rows": 0,
        "rows_read": len(normalized_symbols),
        "rows_written": 0,
        "historical_mcap_rows": 0,
        "mcap_refreshed_count": 0,
        "failed_symbol_count": 0,
        "failed_symbols": [],
        "classification_counts": {},
        "no_data_count": 0,
        "source_unavailable_count": 0,
        "auth_unavailable_count": 0,
        "parse_failed_count": 0,
        "symbols_skipped_no_work": [],
        "fallback_used": False,
        "state_advanced": False,
    }
    failures: list[dict[str, object]] = []

    for symbol in normalized_symbols:
        try:
            symbol_work_count = 0
            mcap_from_date = choose_from_date(
                from_date,
                [get_db_max_date("historical_mcap", filters={"symbol": symbol})],
            )
            if mcap_from_date <= to_date:
                # Sharpely's getHistoricalMetricData 400s on a same-day (start_date == end_date)
                # window -- which is exactly what a normal incremental day produces once
                # yesterday is already synced (get_db_max_date returns yesterday, "today" is the
                # only new day). Always request at least a 1-day window; get_historical_mcap
                # upserts, so re-fetching an already-synced day is harmless.
                effective_from_date = min(mcap_from_date, to_date - pd.Timedelta(days=1))
                mcap_result = get_historical_mcap(symbol, effective_from_date, to_date)
                mcap_rows = int(mcap_result.get("rows") or 0)
                summary["historical_mcap_rows"] = int(summary["historical_mcap_rows"]) + mcap_rows
                summary["mcap_refreshed_count"] = int(summary["mcap_refreshed_count"]) + 1
                symbol_work_count += mcap_rows
            if symbol_work_count <= 0:
                skipped = list(summary["symbols_skipped_no_work"])
                if len(skipped) < 100:
                    skipped.append(symbol)
                summary["symbols_skipped_no_work"] = skipped
        except Exception as exc:
            error_text = f"{type(exc).__name__}: {exc}"
            classification = classify_sharpely_sync_error(error_text)
            failures.append({"symbol": symbol, "classification": classification, "error": error_text})
            _record_sharpely_fallback(
                fallback_type="sharpely_symbol_sync_failed",
                reason="Sharpely mcap sync failed for one symbol; the batch continued with classified run-state.",
                error=exc,
                symbol=symbol,
                metadata={"classification": classification},
            )
    rows_written = int(summary["historical_mcap_rows"])
    summary["rows"] = rows_written
    summary["rows_written"] = rows_written
    failure_classifications = [str(row.get("classification") or "failed") for row in failures]
    classification_counts = {key: failure_classifications.count(key) for key in sorted(set(failure_classifications))}
    summary["classification"] = classify_sharpely_run_state(
        rows_written=rows_written,
        failures=failures,
        symbol_count=len(normalized_symbols),
    )
    summary["status"] = "ok" if summary["classification"] in {"ok", "no_data"} else "failed"
    summary["failed_symbol_count"] = len(failures)
    summary["failed_symbols"] = [str(row.get("symbol") or "") for row in failures[:100]]
    summary["classification_counts"] = classification_counts
    summary["source_unavailable_count"] = int(classification_counts.get("source_unavailable", 0))
    summary["auth_unavailable_count"] = int(classification_counts.get("auth_unavailable", 0))
    summary["parse_failed_count"] = int(classification_counts.get("parse_failed", 0))
    summary["no_data_count"] = len(summary["symbols_skipped_no_work"]) if rows_written <= 0 else 0
    summary["state_advanced"] = rows_written > 0
    return summary


def main() -> int:
    global STOCKEY_RUN_STATE
    parser = argparse.ArgumentParser(description="Sync Sharpely historical market-cap for tracked symbols")
    parser.add_argument("--symbols", nargs="*", help="Symbols, comma-separated or repeated")
    parser.add_argument("--from-date", dest="from_date", help="Start date in YYYY-MM-DD")
    parser.add_argument("--to-date", dest="to_date", help="End date in YYYY-MM-DD")
    args = parser.parse_args()

    symbols = load_tracked_symbols(args.symbols)
    if not symbols:
        raise SystemExit("No symbols provided. Use --symbols, STOCKEY_SYMBOLS, or config/tracked_symbols.txt")

    STOCKEY_RUN_STATE = sync_sharpely_data(
        symbols=symbols,
        from_date=parse_datetime_arg(args.from_date),
        to_date=parse_datetime_arg(args.to_date),
    )
    print(json.dumps({"status": STOCKEY_RUN_STATE.get("status", "ok"), **STOCKEY_RUN_STATE}, ensure_ascii=False, default=str), flush=True)
    return 1 if STOCKEY_RUN_STATE.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
