from __future__ import annotations

import argparse
import json
import re
from typing import Any

import pandas as pd

from data.dhanlive.client import DhanAPIError, DhanTradingClient
from data.dhanlive.dhan_db import resolve_dhan_identity
from utils.db import db_session, sql_to_df, upsert_to_db
from utils.sync import parse_datetime_arg


PORTFOLIO_TABLE = "advisory_portfolio_orders"
EXECUTION_TABLE = "advisory_execution_orders"
FILLS_TABLE = "advisory_execution_fills"


def normalize_timestamp(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce").dt.normalize()


def table_exists(table_name: str) -> bool:
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
    return not df.empty


def ensure_execution_tables() -> None:
    with db_session() as (_, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {EXECUTION_TABLE} (
                asof_date TIMESTAMPTZ NOT NULL,
                published_on TIMESTAMPTZ NOT NULL,
                setup_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                unique_id TEXT NOT NULL,
                company_master_id TEXT,
                correlation_id TEXT NOT NULL,
                security_id BIGINT,
                exchange_segment TEXT,
                transaction_type TEXT,
                product_type TEXT,
                order_type TEXT,
                validity TEXT,
                quantity BIGINT,
                filled_quantity BIGINT,
                limit_price DOUBLE PRECISION,
                trigger_price DOUBLE PRECISION,
                reference_price DOUBLE PRECISION,
                approved_allocation_inr DOUBLE PRECISION,
                broker_order_id TEXT,
                exchange_order_id TEXT,
                execution_status TEXT,
                execution_reason TEXT,
                broker_order_status TEXT,
                submitted_at TIMESTAMPTZ,
                broker_update_time TIMESTAMPTZ,
                live_mode BOOLEAN,
                raw_broker_json TEXT,
                raw_trade_json TEXT,
                load_ts TIMESTAMPTZ,
                UNIQUE (asof_date, published_on, setup_id, symbol, unique_id)
            )
            """
        )
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {FILLS_TABLE} (
                asof_date TIMESTAMPTZ NOT NULL,
                published_on TIMESTAMPTZ NOT NULL,
                setup_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                unique_id TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                broker_order_id TEXT,
                exchange_trade_id TEXT,
                traded_quantity BIGINT,
                traded_price DOUBLE PRECISION,
                exchange_time TIMESTAMPTZ,
                raw_trade_json TEXT,
                load_ts TIMESTAMPTZ,
                UNIQUE (asof_date, correlation_id, exchange_trade_id)
            )
            """
        )


def load_portfolio_orders(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
    include_existing: bool = False,
) -> pd.DataFrame:
    if not table_exists(PORTFOLIO_TABLE):
        return pd.DataFrame()
    clauses = ["p.portfolio_status IN ('approved', 'trimmed')", "COALESCE(p.approved_allocation_inr, 0) > 0"]
    params: list[object] = []
    if asof_date is not None:
        clauses.append("p.asof_date = %s")
        params.append(asof_date)
    else:
        clauses.append("p.asof_date = (SELECT MAX(asof_date) FROM advisory_portfolio_orders)")
    if symbols:
        clauses.append("p.symbol = ANY(%s)")
        params.append([value.upper() for value in symbols])
    if setup_ids:
        clauses.append("p.setup_id = ANY(%s)")
        params.append([value.upper() for value in setup_ids])

    join_sql = ""
    select_sql = ""
    if table_exists(EXECUTION_TABLE):
        join_sql = f"""
        LEFT JOIN {EXECUTION_TABLE} e
          ON e.asof_date = p.asof_date
         AND e.published_on = p.published_on
         AND e.setup_id = p.setup_id
         AND e.symbol = p.symbol
         AND e.unique_id = p.unique_id
        """
        select_sql = ", e.correlation_id AS existing_correlation_id"
        if not include_existing:
            clauses.append("e.correlation_id IS NULL")

    df = sql_to_df(
        f"""
        SELECT p.* {select_sql}
        FROM {PORTFOLIO_TABLE} p
        {join_sql}
        WHERE {' AND '.join(clauses)}
        ORDER BY p.plan_rank, p.symbol
        """,
        params=tuple(params) if params else None,
    )
    if df.empty:
        return df
    df["published_on"] = pd.to_datetime(df["published_on"], utc=True, errors="coerce")
    df["asof_date"] = normalize_timestamp(df["asof_date"])
    df["symbol"] = df["symbol"].astype("string").str.upper()
    return df


def load_latest_closes(symbols: list[str], asof_date: pd.Timestamp) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()
    df = sql_to_df(
        """
        WITH latest AS (
            SELECT ticker, MAX(date) AS max_date
            FROM dhan_ohlcv_daily
            WHERE exchange = 'NSE'
              AND asset_type = 'stock'
              AND ticker = ANY(%(symbols)s)
              AND date <= %(asof_date)s
            GROUP BY ticker
        )
        SELECT d.ticker AS symbol, d.date, d.close
        FROM dhan_ohlcv_daily d
        JOIN latest l
          ON l.ticker = d.ticker
         AND l.max_date = d.date
        """,
        params={"symbols": symbols, "asof_date": asof_date},
    )
    if df.empty:
        return df
    df["symbol"] = df["symbol"].astype("string").str.upper()
    df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df.drop_duplicates(subset=["symbol"], keep="last")


def sanitize_correlation_id(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9 _-]", "-", value.strip())
    return cleaned[:30]


def map_execution_status(order_status: str | None) -> str:
    status = str(order_status or "").upper()
    if status in {"TRADED", "FILLED"}:
        return "filled"
    if "PART" in status:
        return "partial_filled"
    if status in {"PENDING", "TRANSIT", "OPEN"}:
        return "submitted"
    if status in {"CANCELLED", "EXPIRED"}:
        return "cancelled"
    if status in {"REJECTED"}:
        return "rejected"
    return "planned"


def build_execution_orders(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
    include_existing: bool = False,
    transaction_type: str = "BUY",
    product_type: str = "CNC",
    order_type: str = "MARKET",
    validity: str = "DAY",
) -> pd.DataFrame:
    monitor_date = pd.to_datetime(asof_date or pd.Timestamp.utcnow(), utc=True, errors="coerce").normalize()
    portfolio_orders = load_portfolio_orders(
        asof_date=monitor_date,
        symbols=symbols,
        setup_ids=setup_ids,
        include_existing=include_existing,
    )
    if portfolio_orders.empty:
        return pd.DataFrame()

    closes = load_latest_closes(portfolio_orders["symbol"].astype(str).unique().tolist(), monitor_date)
    close_map = closes.set_index("symbol")["close"].to_dict() if not closes.empty else {}

    rows: list[dict[str, Any]] = []
    for _, row in portfolio_orders.iterrows():
        reference_price = pd.to_numeric(close_map.get(str(row["symbol"]).upper()), errors="coerce")
        approved_allocation = pd.to_numeric(row.get("approved_allocation_inr"), errors="coerce")
        quantity = 0
        execution_status = "planned"
        execution_reason = None
        security_id = None
        exchange_segment = None
        try:
            identity = resolve_dhan_identity(str(row["symbol"]), "NSE", asset_type="stock")
            security_id = int(identity["security_id"])
            exchange_segment = str(identity["exchange_segment"])
        except Exception as exc:
            execution_status = "submit_blocked"
            execution_reason = f"Identity resolution failed: {exc}"
            identity = None

        if pd.isna(reference_price) or reference_price <= 0:
            execution_status = "submit_blocked"
            execution_reason = (execution_reason + " " if execution_reason else "") + "Missing reference close price."
        elif pd.isna(approved_allocation) or approved_allocation <= 0:
            execution_status = "submit_blocked"
            execution_reason = (execution_reason + " " if execution_reason else "") + "Approved allocation is not positive."
        else:
            quantity = int(float(approved_allocation) // float(reference_price))
            if quantity <= 0:
                execution_status = "submit_blocked"
                execution_reason = (execution_reason + " " if execution_reason else "") + "Approved allocation is too small for one share."

        correlation_id = sanitize_correlation_id(f"{row['setup_id']}-{row['symbol']}-{row['unique_id']}")
        limit_price = None if order_type.upper() == "MARKET" else float(reference_price) if pd.notna(reference_price) else None

        rows.append(
            {
                "asof_date": row["asof_date"],
                "published_on": row["published_on"],
                "setup_id": row["setup_id"],
                "symbol": row["symbol"],
                "unique_id": row["unique_id"],
                "company_master_id": row.get("company_master_id"),
                "correlation_id": correlation_id,
                "security_id": security_id,
                "exchange_segment": exchange_segment,
                "transaction_type": transaction_type.upper(),
                "product_type": product_type.upper(),
                "order_type": order_type.upper(),
                "validity": validity.upper(),
                "quantity": quantity,
                "filled_quantity": 0,
                "limit_price": limit_price,
                "trigger_price": None,
                "reference_price": None if pd.isna(reference_price) else float(reference_price),
                "approved_allocation_inr": None if pd.isna(approved_allocation) else float(approved_allocation),
                "broker_order_id": None,
                "exchange_order_id": None,
                "execution_status": execution_status,
                "execution_reason": execution_reason.strip() if isinstance(execution_reason, str) else execution_reason,
                "broker_order_status": None,
                "submitted_at": None,
                "broker_update_time": None,
                "live_mode": False,
                "raw_broker_json": None,
                "raw_trade_json": None,
                "load_ts": pd.Timestamp.utcnow(),
            }
        )
    return pd.DataFrame(rows)


def persist_execution_orders(df: pd.DataFrame) -> None:
    ensure_execution_tables()
    if df.empty:
        return
    out = df.copy()
    numeric_cols = [
        "security_id",
        "quantity",
        "filled_quantity",
        "limit_price",
        "trigger_price",
        "reference_price",
        "approved_allocation_inr",
    ]
    for col in numeric_cols:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    datetime_cols = ["asof_date", "published_on", "submitted_at", "broker_update_time", "load_ts"]
    for col in datetime_cols:
        if col in out.columns:
            out[col] = pd.to_datetime(out[col], utc=True, errors="coerce")
    upsert_to_db(
        out,
        EXECUTION_TABLE,
        unique_keys=["asof_date", "published_on", "setup_id", "symbol", "unique_id"],
        timescaledb_column="asof_date",
    )


def submit_live_orders(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    client = DhanTradingClient()
    out = df.copy()
    for idx, row in out.iterrows():
        if str(row.get("execution_status")) != "planned":
            continue
        try:
            response = client.place_order(
                correlation_id=str(row["correlation_id"]),
                transaction_type=str(row["transaction_type"]),
                exchange_segment=str(row["exchange_segment"]),
                product_type=str(row["product_type"]),
                order_type=str(row["order_type"]),
                validity=str(row["validity"]),
                security_id=int(row["security_id"]),
                quantity=int(row["quantity"]),
                price=float(row["limit_price"] or 0),
                trigger_price=float(row["trigger_price"] or 0),
            )
            out.at[idx, "broker_order_id"] = response.get("orderId")
            out.at[idx, "exchange_order_id"] = response.get("exchangeOrderId")
            out.at[idx, "broker_order_status"] = response.get("orderStatus") or response.get("status")
            out.at[idx, "execution_status"] = map_execution_status(out.at[idx, "broker_order_status"]) or "submitted"
            out.at[idx, "submitted_at"] = pd.Timestamp.utcnow()
            out.at[idx, "broker_update_time"] = pd.Timestamp.utcnow()
            out.at[idx, "live_mode"] = True
            out.at[idx, "raw_broker_json"] = json.dumps(response, ensure_ascii=False, default=str, sort_keys=True)
        except Exception as exc:
            out.at[idx, "execution_status"] = "submit_error"
            out.at[idx, "execution_reason"] = str(exc)
            out.at[idx, "live_mode"] = True
    return out


def load_recon_targets(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
) -> pd.DataFrame:
    if not table_exists(EXECUTION_TABLE):
        return pd.DataFrame()
    clauses = ["execution_status IN ('planned', 'submitted', 'partial_filled', 'submit_error', 'submit_blocked', 'reconcile_error', 'filled')"]
    params: list[object] = []
    if asof_date is not None:
        clauses.append("asof_date = %s")
        params.append(asof_date)
    if symbols:
        clauses.append("symbol = ANY(%s)")
        params.append([value.upper() for value in symbols])
    if setup_ids:
        clauses.append("setup_id = ANY(%s)")
        params.append([value.upper() for value in setup_ids])
    df = sql_to_df(
        f"""
        SELECT *
        FROM {EXECUTION_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY published_on, setup_id, symbol
        """,
        params=tuple(params) if params else None,
    )
    if df.empty:
        return df
    df["published_on"] = pd.to_datetime(df["published_on"], utc=True, errors="coerce")
    df["asof_date"] = normalize_timestamp(df["asof_date"])
    df["symbol"] = df["symbol"].astype("string").str.upper()
    return df


def reconcile_live_orders(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    targets = load_recon_targets(asof_date=asof_date, symbols=symbols, setup_ids=setup_ids)
    if targets.empty:
        return pd.DataFrame(), pd.DataFrame()

    client = DhanTradingClient()
    order_rows: list[dict[str, Any]] = []
    fill_rows: list[dict[str, Any]] = []
    for _, row in targets.iterrows():
        row_dict = row.to_dict()
        try:
            if row.get("correlation_id"):
                broker_order = client.get_order_by_correlation_id(str(row["correlation_id"]))
            elif row.get("broker_order_id"):
                broker_order = client.get_order_by_id(str(row["broker_order_id"]))
            else:
                broker_order = {}
            broker_status = broker_order.get("orderStatus") or broker_order.get("status")
            row_dict["broker_order_id"] = broker_order.get("orderId") or row.get("broker_order_id")
            row_dict["exchange_order_id"] = broker_order.get("exchangeOrderId") or row.get("exchange_order_id")
            row_dict["broker_order_status"] = broker_status
            row_dict["execution_status"] = map_execution_status(broker_status) if broker_status else row.get("execution_status")
            row_dict["filled_quantity"] = broker_order.get("filled_qty") or broker_order.get("filledQuantity") or row.get("filled_quantity") or 0
            row_dict["broker_update_time"] = pd.Timestamp.utcnow()
            row_dict["raw_broker_json"] = json.dumps(broker_order, ensure_ascii=False, default=str, sort_keys=True)

            order_id = row_dict.get("broker_order_id")
            trades_payload = []
            if order_id:
                trades_payload = client.get_trades_by_order_id(str(order_id))
                if isinstance(trades_payload, dict):
                    trades_payload = [trades_payload]
            row_dict["raw_trade_json"] = json.dumps(trades_payload, ensure_ascii=False, default=str, sort_keys=True)
            for trade in trades_payload:
                fill_rows.append(
                    {
                        "asof_date": row["asof_date"],
                        "published_on": row["published_on"],
                        "setup_id": row["setup_id"],
                        "symbol": row["symbol"],
                        "unique_id": row["unique_id"],
                        "correlation_id": row["correlation_id"],
                        "broker_order_id": order_id,
                        "exchange_trade_id": trade.get("exchangeTradeId") or trade.get("exchange_trade_id") or str(order_id),
                        "traded_quantity": trade.get("tradedQuantity") or trade.get("traded_quantity") or 0,
                        "traded_price": trade.get("tradedPrice") or trade.get("traded_price"),
                        "exchange_time": pd.to_datetime(trade.get("exchangeTime"), utc=True, errors="coerce"),
                        "raw_trade_json": json.dumps(trade, ensure_ascii=False, default=str, sort_keys=True),
                        "load_ts": pd.Timestamp.utcnow(),
                    }
                )
        except Exception as exc:
            row_dict["execution_status"] = "reconcile_error"
            row_dict["execution_reason"] = str(exc)
            row_dict["broker_update_time"] = pd.Timestamp.utcnow()
        row_dict["load_ts"] = pd.Timestamp.utcnow()
        order_rows.append(row_dict)
    return pd.DataFrame(order_rows), pd.DataFrame(fill_rows)


def persist_reconciliation(order_df: pd.DataFrame, fills_df: pd.DataFrame) -> None:
    ensure_execution_tables()
    if not order_df.empty:
        order_out = order_df.copy()
        for col in ["security_id", "quantity", "filled_quantity", "limit_price", "trigger_price", "reference_price", "approved_allocation_inr"]:
            if col in order_out.columns:
                order_out[col] = pd.to_numeric(order_out[col], errors="coerce")
        for col in ["asof_date", "published_on", "submitted_at", "broker_update_time", "load_ts"]:
            if col in order_out.columns:
                order_out[col] = pd.to_datetime(order_out[col], utc=True, errors="coerce")
        upsert_to_db(
            order_out,
            EXECUTION_TABLE,
            unique_keys=["asof_date", "published_on", "setup_id", "symbol", "unique_id"],
            timescaledb_column="asof_date",
        )
    if not fills_df.empty:
        fills_out = fills_df.copy()
        for col in ["traded_quantity", "traded_price"]:
            if col in fills_out.columns:
                fills_out[col] = pd.to_numeric(fills_out[col], errors="coerce")
        for col in ["asof_date", "published_on", "exchange_time", "load_ts"]:
            if col in fills_out.columns:
                fills_out[col] = pd.to_datetime(fills_out[col], utc=True, errors="coerce")
        upsert_to_db(
            fills_out,
            FILLS_TABLE,
            unique_keys=["asof_date", "correlation_id", "exchange_trade_id"],
            timescaledb_column="asof_date",
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build broker handoff orders and reconcile execution state for advisory portfolio orders.")
    parser.add_argument("--date", type=parse_datetime_arg, help="Execution asof date in YYYY-MM-DD")
    parser.add_argument("--symbols", nargs="*", help="Optional symbols")
    parser.add_argument("--setup", dest="setup_ids", nargs="*", help="Optional setup ids")
    parser.add_argument("--live", action="store_true", help="Submit planned orders live through Dhan")
    parser.add_argument("--reconcile-only", action="store_true", help="Skip planning/submission and only reconcile existing execution orders")
    parser.add_argument("--include-existing", action="store_true", help="Include already planned execution rows when building orders")
    parser.add_argument("--product-type", default="CNC")
    parser.add_argument("--order-type", default="MARKET")
    parser.add_argument("--validity", default="DAY")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def summarize(planned_df: pd.DataFrame, reconciled_df: pd.DataFrame, fills_df: pd.DataFrame) -> dict[str, Any]:
    combined = reconciled_df if not reconciled_df.empty else planned_df
    return {
        "status": "ok",
        "execution_table": EXECUTION_TABLE,
        "fills_table": FILLS_TABLE,
        "planned_count": int(len(planned_df)),
        "reconciled_count": int(len(reconciled_df)),
        "fill_count": int(len(fills_df)),
        "status_counts": combined["execution_status"].value_counts(dropna=False).to_dict() if not combined.empty else {},
        "sample": combined.head(10).to_dict(orient="records") if not combined.empty else [],
    }


def main() -> int:
    args = parse_args()
    asof_date = pd.Timestamp(args.date, tz="UTC") if args.date else None
    planned_df = pd.DataFrame()
    reconciled_df = pd.DataFrame()
    fills_df = pd.DataFrame()

    if not args.reconcile_only:
        planned_df = build_execution_orders(
            asof_date=asof_date,
            symbols=args.symbols,
            setup_ids=args.setup_ids,
            include_existing=bool(args.include_existing),
            product_type=args.product_type,
            order_type=args.order_type,
            validity=args.validity,
        )
        if args.live:
            planned_df = submit_live_orders(planned_df)
        if not args.dry_run:
            persist_execution_orders(planned_df)

    if args.live or args.reconcile_only:
        reconciled_df, fills_df = reconcile_live_orders(
            asof_date=asof_date,
            symbols=args.symbols,
            setup_ids=args.setup_ids,
        )
        if not args.dry_run:
            persist_reconciliation(reconciled_df, fills_df)

    result = summarize(planned_df, reconciled_df, fills_df)
    result["dry_run"] = bool(args.dry_run)
    result["live"] = bool(args.live)
    result["reconcile_only"] = bool(args.reconcile_only)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
