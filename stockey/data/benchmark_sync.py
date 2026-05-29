from __future__ import annotations

import argparse
import json

import pandas as pd

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
    args = parse_args()
    from_date = parse_datetime_arg(args.from_date)
    to_date = parse_datetime_arg(args.to_date)
    results = [
        sync_benchmark(symbol, from_date=from_date, to_date=to_date, dry_run=bool(args.dry_run))
        for symbol in args.symbols
    ]
    payload = {"status": "ok", "results": results}
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
