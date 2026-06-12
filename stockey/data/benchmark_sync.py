from __future__ import annotations

import argparse
import json

import pandas as pd

from advisory.fallback_telemetry import record_local_fallback_event
from data.dhanlive.ohlcv import DAILY_TABLE, ensure_ohlcv_tables
from utils.db import sql_to_df, upsert_to_db
from utils.sync import parse_datetime_arg


DEFAULT_BENCHMARKS = {
    "NIFTY": {
        "index_name": "Nifty 50",
        "security_id": 13,
        "exchange_segment": "IDX_I",
        "instrument": "INDEX",
    },
}
SYNC_SOURCE_NAME = "data.benchmark_sync"
STOCKEY_RUN_STATE: dict[str, object] = {}


def _record_benchmark_sync_fallback(
    *,
    symbol: str,
    error: Exception,
    from_date: pd.Timestamp | None,
    to_date: pd.Timestamp | None,
    dry_run: bool,
) -> None:
    record_local_fallback_event(
        module="data.benchmark_sync",
        fallback_type="benchmark_sync_symbol_failed",
        source="benchmark_sync",
        severity="warn",
        reason=(
            "Canonical benchmark sync failed for one symbol; regime, relative-strength, and "
            "benchmark comparison features may be stale for that benchmark."
        ),
        error=error,
        metadata={
            "symbol": str(symbol).upper(),
            "from_date": None if from_date is None else str(from_date),
            "to_date": None if to_date is None else str(to_date),
            "dry_run": bool(dry_run),
        },
    )


def normalize_timestamp(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce").dt.normalize()


def load_nse_index_history(
    *,
    index_name: str,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
) -> pd.DataFrame:
    clauses = ["index_name = %(index_name)s"]
    params: dict[str, object] = {"index_name": index_name}
    if from_date is not None:
        clauses.append("date >= %(from_date)s")
        params["from_date"] = from_date
    if to_date is not None:
        clauses.append("date <= %(to_date)s")
        params["to_date"] = to_date
    df = sql_to_df(
        f"""
        SELECT date, open, high, low, close, volume
        FROM nseindia_indices
        WHERE {' AND '.join(clauses)}
        ORDER BY date
        """,
        params=params,
    )
    if df.empty:
        return df
    df["date"] = normalize_timestamp(df["date"])
    for column in ["open", "high", "low", "close", "volume"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    return df.dropna(subset=["date", "close"]).drop_duplicates(subset=["date"], keep="last")


def normalize_benchmark_rows(
    df: pd.DataFrame,
    *,
    ticker: str,
    security_id: int,
    exchange_segment: str,
    instrument: str,
) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    out = df.copy()
    out["company_master_id"] = None
    out["asset_type"] = "benchmark"
    out["exchange"] = "NSE"
    out["ticker"] = ticker
    out["security_id"] = int(security_id)
    out["exchange_segment"] = exchange_segment
    out["instrument"] = instrument
    out["open_interest"] = pd.NA
    out["source_timestamp"] = out["date"]
    out["load_ts"] = pd.Timestamp.utcnow()
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
        if column not in out.columns:
            out[column] = pd.NA
    for column in ["open", "high", "low", "close", "volume", "open_interest"]:
        out[column] = pd.to_numeric(out[column], errors="coerce").astype("Float64")
    return out[ordered_columns].drop_duplicates(subset=["exchange", "security_id", "date"], keep="last")


def latest_dates(ticker: str, index_name: str) -> dict[str, object]:
    df = sql_to_df(
        """
        SELECT
            (SELECT MAX(date) FROM dhan_ohlcv_daily WHERE exchange = 'NSE' AND asset_type = 'benchmark' AND ticker = %(ticker)s) AS dhan_max_date,
            (SELECT MAX(date) FROM nseindia_indices WHERE index_name = %(index_name)s) AS nse_max_date
        """,
        params={"ticker": ticker, "index_name": index_name},
    )
    if df.empty:
        return {"dhan_max_date": None, "nse_max_date": None}
    return df.iloc[0].to_dict()


def sync_benchmark(
    ticker: str,
    *,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    ticker = ticker.strip().upper()
    config = DEFAULT_BENCHMARKS.get(ticker)
    if not config:
        raise ValueError(f"Unsupported benchmark: {ticker}")
    ensure_ohlcv_tables()
    before = latest_dates(ticker, str(config["index_name"]))
    history = load_nse_index_history(
        index_name=str(config["index_name"]),
        from_date=from_date,
        to_date=to_date,
    )
    rows = normalize_benchmark_rows(
        history,
        ticker=ticker,
        security_id=int(config["security_id"]),
        exchange_segment=str(config["exchange_segment"]),
        instrument=str(config["instrument"]),
    )
    if not dry_run and not rows.empty:
        upsert_to_db(
            rows,
            DAILY_TABLE,
            unique_keys=["exchange", "security_id", "date"],
            timescaledb_column="date",
        )
    after = before if dry_run else latest_dates(ticker, str(config["index_name"]))
    return {
        "status": "ok",
        "ticker": ticker,
        "source_index_name": config["index_name"],
        "rows_loaded": int(len(rows)),
        "from_date": None if rows.empty else str(rows["date"].min()),
        "to_date": None if rows.empty else str(rows["date"].max()),
        "before": before,
        "after": after,
        "dry_run": bool(dry_run),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync canonical advisory benchmark rows from parsed NSE index history.")
    parser.add_argument("--symbols", nargs="*", default=["NIFTY"], help="Benchmark symbols to sync. Currently supports NIFTY.")
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    global STOCKEY_RUN_STATE
    args = parse_args()
    from_date = parse_datetime_arg(args.from_date)
    to_date = parse_datetime_arg(args.to_date)
    results = []
    failures = []
    for symbol in args.symbols:
        try:
            results.append(sync_benchmark(symbol, from_date=from_date, to_date=to_date, dry_run=bool(args.dry_run)))
        except Exception as exc:
            _record_benchmark_sync_fallback(
                symbol=str(symbol),
                error=exc,
                from_date=from_date,
                to_date=to_date,
                dry_run=bool(args.dry_run),
            )
            failures.append({"symbol": str(symbol).upper(), "error": f"{type(exc).__name__}: {exc}"})
            print(f"benchmark sync failed symbol={symbol} error={type(exc).__name__}: {exc}", flush=True)
    rows_loaded = sum(int(result.get("rows_loaded") or 0) for result in results)
    STOCKEY_RUN_STATE = {
        "source": SYNC_SOURCE_NAME,
        "rows": int(rows_loaded),
        "rows_read": int(rows_loaded),
        "rows_written": 0 if args.dry_run else int(rows_loaded),
        "symbol_count": int(len(args.symbols)),
        "succeeded_symbol_count": int(len(results)),
        "failed_symbol_count": int(len(failures)),
        "attempt_count": int(len(args.symbols)),
        "failed_attempt_count": int(len(failures)),
        "source_unavailable_count": 0,
        "no_data_count": sum(1 for result in results if int(result.get("rows_loaded") or 0) == 0),
        "fallback_used": False,
        "state_advanced": bool((not args.dry_run) and rows_loaded > 0),
        "dry_run": bool(args.dry_run),
    }
    if from_date is not None:
        STOCKEY_RUN_STATE["from_date"] = from_date.date().isoformat()
    if to_date is not None:
        STOCKEY_RUN_STATE["to_date"] = to_date.date().isoformat()
    if failures:
        STOCKEY_RUN_STATE["failed_symbols"] = failures[:20]
    status = "partial" if failures and results else "failed" if failures else "ok"
    payload = {"status": status, "results": results, **STOCKEY_RUN_STATE}
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
