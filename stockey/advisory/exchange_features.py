from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from features.tutils import get_max_date
from utils.db import sql_to_df, upsert_to_db
from utils.sync import parse_datetime_arg


TABLE_NAME = "advisory_exchange_features_daily"
EVENTS_TABLE = "advisory_exchange_events"


def normalize_timestamp(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce").dt.normalize()


def table_exists(table_name: str) -> bool:
    try:
        df = sql_to_df(
            """
            SELECT 1 AS exists_flag
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = %s
            LIMIT 1
            """,
            params=(table_name,),
        )
    except Exception:
        return False
    return not df.empty


def _as_utc_timestamp(value: pd.Timestamp | None) -> pd.Timestamp | None:
    if value is None:
        return None
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC").normalize()
    return ts.tz_convert("UTC").normalize()


def load_target_dates(
    *,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    rebuild: bool = False,
) -> pd.DataFrame:
    effective_from = _as_utc_timestamp(from_date)
    effective_to = _as_utc_timestamp(to_date) or pd.Timestamp.utcnow().normalize()
    if not rebuild and from_date is None and table_exists(TABLE_NAME):
        max_date = get_max_date(TABLE_NAME, "asof_date")
        if max_date is not None:
            effective_from = _as_utc_timestamp(pd.Timestamp(max_date) + pd.Timedelta(days=1))

    clauses = ["date <= %(to_date)s"]
    params: dict[str, Any] = {"to_date": effective_to}
    if effective_from is not None:
        clauses.append("date >= %(from_date)s")
        params["from_date"] = effective_from

    dates = sql_to_df(
        f"""
        SELECT date
        FROM dim_trading_days
        WHERE {' AND '.join(clauses)}
        ORDER BY date
        """,
        params=params,
    )
    if dates.empty:
        return dates
    dates["asof_date"] = normalize_timestamp(dates["date"])
    return dates[["asof_date"]].drop_duplicates().sort_values("asof_date").reset_index(drop=True)


def load_exchange_events(
    *,
    start_date: pd.Timestamp | None = None,
    end_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
) -> pd.DataFrame:
    if not table_exists(EVENTS_TABLE):
        return pd.DataFrame()
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if start_date is not None:
        clauses.append("known_on >= %(start_date)s")
        params["start_date"] = start_date
    if end_date is not None:
        clauses.append("known_on <= %(end_date)s")
        params["end_date"] = end_date
    if symbols:
        clauses.append("symbol = ANY(%(symbols)s)")
        params["symbols"] = [symbol.upper() for symbol in symbols]
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    df = sql_to_df(
        f"""
        SELECT
            event_id,
            event_source,
            event_type,
            symbol,
            company_master_id,
            event_date,
            known_on,
            disclosure_date,
            participant,
            side,
            quantity,
            price,
            value_inr,
            holding_pct_before,
            holding_pct_after,
            event_summary
        FROM {EVENTS_TABLE}
        {where_sql}
        ORDER BY known_on, symbol, event_source
        """,
        params=params or None,
        retries=4,
        statement_timeout_ms=0,
        chunksize=50000,
    )
    if df.empty:
        return df
    df["known_on"] = normalize_timestamp(df["known_on"])
    df["event_date"] = normalize_timestamp(df["event_date"])
    df["symbol"] = df["symbol"].astype("string").str.upper()
    for col in ["quantity", "price", "value_inr", "holding_pct_before", "holding_pct_after"]:
        df[col] = pd.to_numeric(df.get(col), errors="coerce")
    return df


def _side_value(events: pd.DataFrame, source: str, side: str, window_days: int, asof_date: pd.Timestamp) -> float:
    start = asof_date - pd.Timedelta(days=window_days)
    mask = (
        events["event_source"].eq(source)
        & events["side"].astype("string").str.upper().eq(side)
        & events["known_on"].between(start, asof_date)
    )
    return float(events.loc[mask, "value_inr"].sum(skipna=True))


def _source_count(events: pd.DataFrame, source: str, window_days: int, asof_date: pd.Timestamp) -> int:
    start = asof_date - pd.Timedelta(days=window_days)
    return int((events["event_source"].eq(source) & events["known_on"].between(start, asof_date)).sum())


def _source_sum(events: pd.DataFrame, source: str, value_col: str, window_days: int, asof_date: pd.Timestamp) -> float:
    start = asof_date - pd.Timedelta(days=window_days)
    mask = events["event_source"].eq(source) & events["known_on"].between(start, asof_date)
    return float(events.loc[mask, value_col].sum(skipna=True))


def compute_exchange_feature_columns(events: pd.DataFrame, dates: pd.DataFrame) -> pd.DataFrame:
    if events.empty or dates.empty:
        return pd.DataFrame()
    events = events.copy()
    dates = dates.copy()
    events["known_on"] = normalize_timestamp(events["known_on"])
    events["symbol"] = events["symbol"].astype("string").str.upper()
    dates["asof_date"] = normalize_timestamp(dates["asof_date"])
    rows: list[dict[str, Any]] = []
    symbols = events["symbol"].dropna().drop_duplicates().sort_values().tolist()
    print(
        f"[advisory.exchange_features] compute start dates={len(dates)} symbols={len(symbols)} events={len(events)}",
        flush=True,
    )
    for symbol in symbols:
        symbol_events = events[events["symbol"].eq(symbol)].sort_values("known_on")
        if symbol_events.empty:
            continue
        if len(rows) % 250 == 0:
            print(
                f"[advisory.exchange_features] progress generated_rows={len(rows)} current_symbol={symbol}",
                flush=True,
            )
        for asof_date in dates["asof_date"]:
            known = symbol_events[symbol_events["known_on"] <= asof_date]
            if known.empty:
                continue
            latest = known.iloc[-1]
            block_buy_20d = _side_value(known, "nse_block_deal", "BUY", 20, asof_date)
            block_sell_20d = _side_value(known, "nse_block_deal", "SELL", 20, asof_date)
            bulk_buy_20d = _side_value(known, "nse_bulk_deal", "BUY", 20, asof_date)
            bulk_sell_20d = _side_value(known, "nse_bulk_deal", "SELL", 20, asof_date)
            insider_buy_90d = _side_value(known, "nse_insider_deal", "BUY", 90, asof_date)
            insider_sell_90d = _side_value(known, "nse_insider_deal", "SELL", 90, asof_date)
            short_qty_20d = _source_sum(known, "nse_short_selling", "quantity", 20, asof_date)
            deal_cluster_20d = _source_count(known, "nse_block_deal", 20, asof_date) + _source_count(known, "nse_bulk_deal", 20, asof_date)
            insider_count_90d = _source_count(known, "nse_insider_deal", 90, asof_date)
            earnings_known = known[known["event_source"].eq("nse_earnings_event")]
            future_earnings = earnings_known[earnings_known["event_date"].between(asof_date, asof_date + pd.Timedelta(days=14))]
            corporate_action_count_30d = _source_count(known, "nse_corporate_action", 30, asof_date)
            insider_net_90d = insider_buy_90d - insider_sell_90d
            deal_net_20d = block_buy_20d + bulk_buy_20d - block_sell_20d - bulk_sell_20d
            accumulation_score = 0.0
            distribution_score = 0.0
            if deal_net_20d > 0:
                accumulation_score += min(0.35, deal_net_20d / 100_000_000.0 * 0.05)
            if insider_net_90d > 0:
                accumulation_score += min(0.35, insider_net_90d / 50_000_000.0 * 0.05)
            if deal_net_20d < 0:
                distribution_score += min(0.35, abs(deal_net_20d) / 100_000_000.0 * 0.05)
            if insider_net_90d < 0:
                distribution_score += min(0.35, abs(insider_net_90d) / 50_000_000.0 * 0.05)
            if short_qty_20d > 0:
                distribution_score += min(0.20, short_qty_20d / 1_000_000.0 * 0.02)
            exchange_event_score = max(-1.0, min(1.0, accumulation_score - distribution_score))
            rows.append(
                {
                    "asof_date": asof_date,
                    "symbol": symbol,
                    "company_master_id": latest.get("company_master_id"),
                    "latest_exchange_event_date": latest.get("known_on"),
                    "latest_exchange_event_source": latest.get("event_source"),
                    "latest_exchange_event_type": latest.get("event_type"),
                    "latest_exchange_event_summary": latest.get("event_summary"),
                    "block_deal_buy_value_20d": block_buy_20d,
                    "block_deal_sell_value_20d": block_sell_20d,
                    "bulk_deal_buy_value_20d": bulk_buy_20d,
                    "bulk_deal_sell_value_20d": bulk_sell_20d,
                    "deal_net_value_20d": deal_net_20d,
                    "deal_cluster_count_20d": deal_cluster_20d,
                    "insider_buy_value_90d": insider_buy_90d,
                    "insider_sell_value_90d": insider_sell_90d,
                    "insider_net_value_90d": insider_net_90d,
                    "insider_event_count_90d": insider_count_90d,
                    "short_selling_quantity_20d": short_qty_20d,
                    "short_selling_event_count_20d": _source_count(known, "nse_short_selling", 20, asof_date),
                    "upcoming_earnings_14d": bool(not future_earnings.empty),
                    "days_to_earnings": (
                        int((future_earnings["event_date"].min() - asof_date).days)
                        if not future_earnings.empty
                        else None
                    ),
                    "corporate_action_count_30d": corporate_action_count_30d,
                    "exchange_accumulation_score": round(float(accumulation_score), 6),
                    "exchange_distribution_score": round(float(distribution_score), 6),
                    "exchange_event_score": round(float(exchange_event_score), 6),
                    "load_ts": pd.Timestamp.utcnow(),
                }
            )
    return pd.DataFrame(rows)


def build_exchange_features(
    *,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    rebuild: bool = False,
) -> pd.DataFrame:
    dates = load_target_dates(from_date=from_date, to_date=to_date, rebuild=rebuild)
    if dates.empty:
        return pd.DataFrame()
    start_date = dates["asof_date"].min() - pd.Timedelta(days=120)
    end_date = dates["asof_date"].max() + pd.Timedelta(days=14)
    print(
        f"[advisory.exchange_features] load start start_date={start_date} end_date={end_date} symbols={0 if not symbols else len(symbols)} rebuild={rebuild}",
        flush=True,
    )
    events = load_exchange_events(start_date=start_date, end_date=end_date, symbols=symbols)
    print(
        f"[advisory.exchange_features] load done events={len(events)} unique_symbols={0 if events.empty else int(events['symbol'].nunique())}",
        flush=True,
    )
    return compute_exchange_feature_columns(events, dates)


def persist_exchange_features(df: pd.DataFrame) -> None:
    if df.empty:
        return
    upsert_to_db(df, TABLE_NAME, unique_keys=["asof_date", "symbol"], timescaledb_column="asof_date")


def summarize(df: pd.DataFrame) -> dict[str, object]:
    if df.empty:
        return {"status": "ok", "table": TABLE_NAME, "row_count": 0, "sample": []}
    return {
        "status": "ok",
        "table": TABLE_NAME,
        "row_count": int(len(df)),
        "date_min": df["asof_date"].min().date().isoformat(),
        "date_max": df["asof_date"].max().date().isoformat(),
        "symbol_count": int(df["symbol"].nunique()),
        "sample": df.tail(5).to_dict(orient="records"),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build daily advisory exchange-event features from normalized NSE events.")
    parser.add_argument("--from-date", type=parse_datetime_arg)
    parser.add_argument("--to-date", type=parse_datetime_arg)
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    df = build_exchange_features(
        from_date=pd.Timestamp(args.from_date, tz="UTC") if args.from_date else None,
        to_date=pd.Timestamp(args.to_date, tz="UTC") if args.to_date else None,
        symbols=args.symbols,
        rebuild=bool(args.rebuild),
    )
    if not args.dry_run:
        persist_exchange_features(df)
    result = summarize(df)
    result["dry_run"] = bool(args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
