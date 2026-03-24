from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd
import talib

from advisory.data_sync import ensure_advisory_symbol_inputs
from advisory.peer_sync import sync_peer_data
from features.tutils import get_max_date
from utils.company_master import map_company_master_ids
from utils.db import db_session, sql_to_df, upsert_to_db
from utils.sync import load_tracked_symbols, parse_datetime_arg


TABLE_NAME = "advisory_technical_daily"
DEFAULT_BENCHMARK_NAME = "NIFTY"
LOOKBACK_BUFFER_DAYS = 400
MIN_AVG_TRADED_VALUE_20D = 1_00_00_000.0
MAX_BREAKOUT_EXTENSION_PCT = 10.0
MIN_SECTOR_PEER_COUNT = 3


def normalize_timestamp(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce").dt.normalize()


def resolve_symbol_universe(symbols: list[str] | None) -> list[str]:
    values = load_tracked_symbols(symbols)
    if values:
        return values
    df = sql_to_df(
        """
        SELECT DISTINCT symbol
        FROM nseindia_ohlcv_adjusted
        ORDER BY symbol
        """
    )
    if df.empty:
        return []
    return (
        df["symbol"]
        .astype("string")
        .dropna()
        .str.strip()
        .str.upper()
        .drop_duplicates()
        .tolist()
    )


def load_benchmark_series(
    *,
    benchmark_name: str = DEFAULT_BENCHMARK_NAME,
    start_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
) -> pd.DataFrame:
    clauses = ["ticker = %(benchmark_name)s", "asset_type = 'benchmark'"]
    params: dict[str, object] = {"benchmark_name": benchmark_name}
    if start_date is not None:
        clauses.append("date >= %(start_date)s")
        params["start_date"] = start_date
    if to_date is not None:
        clauses.append("date <= %(to_date)s")
        params["to_date"] = to_date
    df = sql_to_df(
        f"""
        SELECT ticker, date, close
        FROM dhan_ohlcv_daily
        WHERE {' AND '.join(clauses)}
        ORDER BY date
        """,
        params=params,
    )
    if df.empty and benchmark_name.upper() == "NIFTY":
        fallback_params: dict[str, object] = {"benchmark_name": "Nifty 50"}
        fallback_clauses = ["index_name = %(benchmark_name)s"]
        if start_date is not None:
            fallback_clauses.append("date >= %(start_date)s")
            fallback_params["start_date"] = start_date
        if to_date is not None:
            fallback_clauses.append("date <= %(to_date)s")
            fallback_params["to_date"] = to_date
        df = sql_to_df(
            f"""
            SELECT index_name AS ticker, date, close
            FROM nseindia_indices
            WHERE {' AND '.join(fallback_clauses)}
            ORDER BY date
            """,
            params=fallback_params,
        )
    if df.empty:
        return df
    df["date"] = normalize_timestamp(df["date"])
    df["benchmark_close"] = pd.to_numeric(df["close"], errors="coerce")
    df["benchmark_ret_20d"] = df["benchmark_close"].pct_change(20)
    return df[["date", "benchmark_close", "benchmark_ret_20d"]]


def load_price_history(
    *,
    symbols: list[str],
    start_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()

    clauses = ["symbol = ANY(%s)"]
    params: list[object] = [symbols]
    if start_date is not None:
        clauses.append("date >= %s")
        params.append(start_date)
    if to_date is not None:
        clauses.append("date <= %s")
        params.append(to_date)

    adjusted = sql_to_df(
        f"""
        SELECT
            symbol,
            series,
            security_id,
            isin,
            date,
            adj_open,
            adj_high,
            adj_low,
            adj_close,
            volume,
            total_value
        FROM nseindia_ohlcv_adjusted
        WHERE {' AND '.join(clauses)}
          AND series = 'EQ'
        ORDER BY symbol, series, date
        """,
        params=tuple(params),
    )
    if not adjusted.empty:
        adjusted["date"] = normalize_timestamp(adjusted["date"])
    adjusted_symbols = (
        adjusted["symbol"].astype("string").str.strip().str.upper().dropna().unique().tolist()
        if not adjusted.empty
        else []
    )
    missing_symbols = [symbol for symbol in symbols if symbol not in set(adjusted_symbols)]

    dhan = pd.DataFrame()
    if missing_symbols:
        dhan_clauses = ["ticker = ANY(%s)", "asset_type = 'stock'", "exchange = 'NSE'"]
        dhan_params: list[object] = [missing_symbols]
        if start_date is not None:
            dhan_clauses.append("date >= %s")
            dhan_params.append(start_date)
        if to_date is not None:
            dhan_clauses.append("date <= %s")
            dhan_params.append(to_date)
        dhan = sql_to_df(
            f"""
            SELECT
                ticker AS symbol,
                'EQ' AS series,
                security_id,
                NULL::text AS isin,
                date,
                open AS adj_open,
                high AS adj_high,
                low AS adj_low,
                close AS adj_close,
                volume,
                close * volume AS total_value
            FROM dhan_ohlcv_daily
            WHERE {' AND '.join(dhan_clauses)}
            ORDER BY ticker, date
            """,
            params=tuple(dhan_params),
        )
        if not dhan.empty:
            dhan["date"] = normalize_timestamp(dhan["date"])

    if adjusted.empty and dhan.empty:
        return pd.DataFrame()
    if adjusted.empty:
        df = dhan.copy()
    elif dhan.empty:
        df = adjusted.copy()
    else:
        df = pd.concat([adjusted, dhan], ignore_index=True)
    if df.empty:
        return df
    numeric_cols = [
        "adj_open",
        "adj_high",
        "adj_low",
        "adj_close",
        "volume",
        "total_value",
    ]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()
    return df


def load_sector_mapping(symbols: list[str]) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame(columns=["symbol", "sector_code"])
    df = sql_to_df(
        """
        SELECT symbol, sector_code
        FROM master_sharpely_equity
        WHERE symbol = ANY(%s)
        """,
        params=(symbols,),
    )
    if df.empty:
        return df
    df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()
    df["sector_code"] = df["sector_code"].astype("string").str.strip()
    return df.drop_duplicates(subset=["symbol"], keep="last")


def load_latest_peer_memberships(anchor_symbols: list[str]) -> pd.DataFrame:
    if not anchor_symbols:
        return pd.DataFrame()
    df = sql_to_df(
        """
        SELECT p.anchor_symbol, p.peer_symbol, p.sector_code, p.industry_code, p.nse_basic_ind_code
        FROM sharpely_stock_peers p
        JOIN (
            SELECT anchor_symbol, MAX(as_on_date) AS max_as_on_date
            FROM sharpely_stock_peers
            WHERE anchor_symbol = ANY(%s)
            GROUP BY anchor_symbol
        ) latest
          ON latest.anchor_symbol = p.anchor_symbol
         AND latest.max_as_on_date = p.as_on_date
        WHERE p.anchor_symbol = ANY(%s)
          AND COALESCE(p.is_self_peer, FALSE) = FALSE
          AND COALESCE(p.nse_active, 1) = 1
          AND COALESCE(p.is_exclusion_list, 0) = 0
        ORDER BY p.anchor_symbol, p.peer_rank, p.peer_symbol
        """,
        params=(anchor_symbols, anchor_symbols),
    )
    if df.empty:
        return df
    df["anchor_symbol"] = df["anchor_symbol"].astype("string").str.upper()
    df["peer_symbol"] = df["peer_symbol"].astype("string").str.upper()
    return df.drop_duplicates(subset=["anchor_symbol", "peer_symbol"], keep="last")


def load_peer_ohlcv(
    peer_symbols: list[str],
    *,
    start_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
) -> pd.DataFrame:
    if not peer_symbols:
        return pd.DataFrame()
    clauses = ["ticker = ANY(%s)", "asset_type = 'stock'", "exchange = 'NSE'"]
    params: list[object] = [peer_symbols]
    if start_date is not None:
        clauses.append("date >= %s")
        params.append(start_date)
    if to_date is not None:
        clauses.append("date <= %s")
        params.append(to_date)
    df = sql_to_df(
        f"""
        SELECT ticker, date, close
        FROM dhan_ohlcv_daily
        WHERE {' AND '.join(clauses)}
        ORDER BY ticker, date
        """,
        params=tuple(params),
    )
    if df.empty:
        return df
    df["date"] = normalize_timestamp(df["date"])
    df["ticker"] = df["ticker"].astype("string").str.upper()
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["peer_ret_20d"] = df.groupby("ticker", dropna=False)["close"].pct_change(20)
    return df


def choose_effective_start(
    from_date: pd.Timestamp | None,
    *,
    rebuild: bool,
) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    if rebuild:
        effective_from = from_date
    else:
        max_date = get_max_date(TABLE_NAME, "asof_date")
        effective_from = from_date or (
            max_date + pd.Timedelta(days=1) if max_date is not None else None
        )
    if effective_from is None:
        return None, None
    history_start = effective_from - pd.Timedelta(days=LOOKBACK_BUFFER_DAYS)
    return effective_from, history_start


def compute_group_features(group: pd.DataFrame) -> pd.DataFrame:
    group = group.sort_values("date").copy()
    close = group["adj_close"].astype(float).to_numpy()
    high = group["adj_high"].astype(float).to_numpy()
    low = group["adj_low"].astype(float).to_numpy()
    volume = group["volume"].astype(float)
    traded_value = group["total_value"].astype(float)

    group["dma_20"] = talib.SMA(close, timeperiod=20)
    group["dma_50"] = talib.SMA(close, timeperiod=50)
    group["dma_200"] = talib.SMA(close, timeperiod=200)
    group["atr_20"] = talib.ATR(high, low, close, timeperiod=20)

    bb_upper, bb_middle, bb_lower = talib.BBANDS(
        close,
        timeperiod=20,
        nbdevup=2,
        nbdevdn=2,
        matype=0,
    )
    group["bb_upper"] = bb_upper
    group["bb_middle"] = bb_middle
    group["bb_lower"] = bb_lower
    group["bb_width"] = np.where(
        pd.Series(bb_middle).replace(0, np.nan).notna(),
        (pd.Series(bb_upper) - pd.Series(bb_lower)) / pd.Series(bb_middle),
        np.nan,
    )

    atr_series = pd.Series(group["atr_20"], index=group.index, dtype="float64")
    group["atr_compression_pct"] = atr_series.rolling(252, min_periods=50).rank(pct=True)

    high_20 = group["adj_high"].rolling(20, min_periods=20).max()
    high_50 = group["adj_high"].rolling(50, min_periods=50).max()
    high_252 = group["adj_high"].rolling(252, min_periods=100).max()
    prev_high_20 = high_20.shift(1)

    group["dist_20d_high"] = (group["adj_close"] / high_20 - 1.0) * 100.0
    group["dist_50d_high"] = (group["adj_close"] / high_50 - 1.0) * 100.0
    group["dist_52w_high"] = (group["adj_close"] / high_252 - 1.0) * 100.0
    group["breakout_extension_pct"] = (group["adj_close"] / prev_high_20 - 1.0) * 100.0

    group["avg_traded_value_20d"] = traded_value.rolling(20, min_periods=20).mean()
    group["avg_traded_value_60d"] = traded_value.rolling(60, min_periods=40).mean()
    group["stock_ret_20d"] = group["adj_close"].pct_change(20)
    group["stock_ret_60d"] = group["adj_close"].pct_change(60)

    group["pass_above_dma_20"] = group["adj_close"] >= group["dma_20"]
    group["pass_above_dma_50"] = group["adj_close"] >= group["dma_50"]
    group["pass_above_dma_200"] = group["adj_close"] >= group["dma_200"]
    group["pass_liquidity_20d"] = group["avg_traded_value_20d"] >= MIN_AVG_TRADED_VALUE_20D
    group["pass_near_52w_high"] = group["dist_52w_high"] >= -15.0
    group["pass_breakout_extension"] = group["breakout_extension_pct"] <= MAX_BREAKOUT_EXTENSION_PCT
    return group


def build_technical_features(
    *,
    symbols: list[str] | None = None,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    rebuild: bool = False,
    benchmark_name: str = DEFAULT_BENCHMARK_NAME,
) -> pd.DataFrame:
    symbol_universe = resolve_symbol_universe(symbols)
    if not symbol_universe:
        return pd.DataFrame()

    effective_from, history_start = choose_effective_start(from_date, rebuild=rebuild)
    effective_to = to_date or pd.Timestamp.now(tz="UTC").normalize()

    prices = load_price_history(
        symbols=symbol_universe,
        start_date=history_start,
        to_date=effective_to,
    )
    if prices.empty:
        return prices

    frames = [
        compute_group_features(group)
        for _, group in prices.groupby(["symbol", "series"], dropna=False)
    ]
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if out.empty:
        return out

    benchmark = load_benchmark_series(
        benchmark_name=benchmark_name,
        start_date=history_start,
        to_date=effective_to,
    )
    if not benchmark.empty:
        out = out.merge(benchmark, on="date", how="left")
        out["rs_vs_benchmark"] = out["stock_ret_20d"] - out["benchmark_ret_20d"]
    else:
        out["benchmark_close"] = np.nan
        out["benchmark_ret_20d"] = np.nan
        out["rs_vs_benchmark"] = np.nan

    sector_mapping = load_sector_mapping(symbol_universe)
    if not sector_mapping.empty:
        out = out.merge(sector_mapping, on="symbol", how="left")
        out["sector_name"] = out["sector_code"]
        memberships = load_latest_peer_memberships(symbol_universe)
        if not memberships.empty:
            peer_prices = load_peer_ohlcv(
                memberships["peer_symbol"].drop_duplicates().tolist(),
                start_date=history_start,
                to_date=effective_to,
            )
            if not peer_prices.empty:
                peer_basket = (
                    memberships.merge(
                        peer_prices.rename(columns={"ticker": "peer_symbol"}),
                        on="peer_symbol",
                        how="inner",
                    )
                    .groupby(["anchor_symbol", "date"], dropna=False)["peer_ret_20d"]
                    .agg(
                        sector_peer_ret_20d="median",
                        sector_peer_count="count",
                    )
                    .reset_index()
                )
                out = out.merge(
                    peer_basket,
                    left_on=["symbol", "date"],
                    right_on=["anchor_symbol", "date"],
                    how="left",
                )
                out["rs_vs_sector"] = np.where(
                    out["sector_peer_count"] >= MIN_SECTOR_PEER_COUNT,
                    out["stock_ret_20d"] - out["sector_peer_ret_20d"],
                    np.nan,
                )
                out = out.drop(columns=["anchor_symbol"], errors="ignore")
            else:
                out["sector_peer_ret_20d"] = np.nan
                out["sector_peer_count"] = 0
                out["rs_vs_sector"] = np.nan
        else:
            out["sector_peer_ret_20d"] = np.nan
            out["sector_peer_count"] = 0
            out["rs_vs_sector"] = np.nan
    else:
        out["sector_code"] = pd.NA
        out["sector_name"] = pd.NA
        out["sector_peer_ret_20d"] = np.nan
        out["sector_peer_count"] = 0
        out["rs_vs_sector"] = np.nan
    out["benchmark_name"] = benchmark_name
    out["company_master_id"] = map_company_master_ids(out["symbol"], exchange="NSE")
    out["asof_date"] = out["date"]
    out["load_ts"] = pd.Timestamp.utcnow()

    if effective_from is not None:
        out = out[out["asof_date"] >= effective_from]
    if effective_to is not None:
        out = out[out["asof_date"] <= effective_to]

    ordered_cols = [
        "asof_date",
        "company_master_id",
        "symbol",
        "series",
        "security_id",
        "isin",
        "benchmark_name",
        "sector_code",
        "sector_name",
        "adj_close",
        "adj_high",
        "adj_low",
        "volume",
        "total_value",
        "dma_20",
        "dma_50",
        "dma_200",
        "atr_20",
        "atr_compression_pct",
        "bb_width",
        "dist_20d_high",
        "dist_50d_high",
        "dist_52w_high",
        "avg_traded_value_20d",
        "avg_traded_value_60d",
        "rs_vs_benchmark",
        "sector_peer_ret_20d",
        "sector_peer_count",
        "rs_vs_sector",
        "breakout_extension_pct",
        "pass_above_dma_20",
        "pass_above_dma_50",
        "pass_above_dma_200",
        "pass_liquidity_20d",
        "pass_near_52w_high",
        "pass_breakout_extension",
        "load_ts",
    ]
    out = out[ordered_cols]
    return out.drop_duplicates(
        subset=["asof_date", "symbol", "series"],
        keep="last",
    ).reset_index(drop=True)


def persist_technical_features(
    df: pd.DataFrame,
    *,
    rebuild: bool = False,
    symbols: list[str] | None = None,
) -> None:
    if df.empty:
        return
    if rebuild:
        with db_session() as (_, cur):
            if symbols:
                cur.execute(
                    f"DELETE FROM {TABLE_NAME} WHERE symbol = ANY(%s)",
                    (symbols,),
                )
            else:
                cur.execute(f"DELETE FROM {TABLE_NAME}")
    upsert_to_db(
        df,
        TABLE_NAME,
        unique_keys=["asof_date", "symbol", "series"],
        timescaledb_column="asof_date",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build TA-Lib based advisory technical features."
    )
    parser.add_argument("--symbols", nargs="*", help="Symbols, comma-separated or repeated")
    parser.add_argument("--from-date", type=parse_datetime_arg)
    parser.add_argument("--to-date", type=parse_datetime_arg)
    parser.add_argument("--benchmark", default=DEFAULT_BENCHMARK_NAME)
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--skip-peer-sync",
        action="store_true",
        help="Do not refresh Sharpely peer snapshots or Dhan peer OHLCV before building.",
    )
    return parser.parse_args()


def summarize(df: pd.DataFrame) -> dict[str, object]:
    if df.empty:
        return {
            "status": "ok",
            "table": TABLE_NAME,
            "row_count": 0,
            "symbol_count": 0,
            "date_min": None,
            "date_max": None,
            "sample": [],
        }
    preview_cols = [
        "asof_date",
        "company_master_id",
        "symbol",
        "adj_close",
        "dma_20",
        "dma_50",
        "dma_200",
        "atr_20",
        "bb_width",
        "dist_52w_high",
        "avg_traded_value_20d",
        "rs_vs_benchmark",
        "sector_peer_count",
        "sector_peer_ret_20d",
        "rs_vs_sector",
        "breakout_extension_pct",
        "pass_liquidity_20d",
        "pass_breakout_extension",
    ]
    return {
        "status": "ok",
        "table": TABLE_NAME,
        "row_count": int(len(df)),
        "symbol_count": int(df["symbol"].nunique()),
        "date_min": df["asof_date"].min().date().isoformat(),
        "date_max": df["asof_date"].max().date().isoformat(),
        "rs_vs_sector_row_count": int(df["rs_vs_sector"].notna().sum()),
        "sample": df[preview_cols].head(5).to_dict(orient="records"),
    }


def main() -> int:
    args = parse_args()
    symbol_universe = resolve_symbol_universe(args.symbols)
    peer_sync_result: dict[str, object] | None = None
    data_sync_result: dict[str, object] | None = None
    if symbol_universe and not args.skip_peer_sync and not args.dry_run:
        data_sync_result = ensure_advisory_symbol_inputs(
            symbol_universe,
            to_date=args.to_date,
        )
        peer_sync_result = sync_peer_data(
            symbols=symbol_universe,
            to_date=args.to_date,
        )
    df = build_technical_features(
        symbols=symbol_universe,
        from_date=pd.Timestamp(args.from_date, tz="UTC") if args.from_date else None,
        to_date=pd.Timestamp(args.to_date, tz="UTC") if args.to_date else None,
        rebuild=args.rebuild,
        benchmark_name=args.benchmark,
    )
    if not args.dry_run:
        persist_technical_features(
            df,
            rebuild=args.rebuild,
            symbols=symbol_universe,
        )
    result = summarize(df)
    result["dry_run"] = bool(args.dry_run)
    result["data_sync"] = data_sync_result
    result["peer_sync"] = peer_sync_result
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
