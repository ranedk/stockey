from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from utils.db import db_session, sql_to_df, upsert_to_db
from utils.sync import parse_datetime_arg


PORTFOLIO_TABLE = "advisory_portfolio_orders"
LIFECYCLE_TABLE = "advisory_position_lifecycle"
REBALANCE_TABLE = "advisory_rebalance_actions"

DEFAULT_REVIEW_STALE_DAYS = 20
DEFAULT_TIGHTEN_STOP_GAIN_PCT = 10.0
DEFAULT_TRIM_WINNER_GAIN_PCT = 15.0


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


def ensure_lifecycle_tables() -> None:
    with db_session() as (_, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {LIFECYCLE_TABLE} (
                asof_date TIMESTAMPTZ NOT NULL,
                published_on TIMESTAMPTZ NOT NULL,
                setup_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                unique_id TEXT NOT NULL,
                position_status TEXT,
                lifecycle_reason TEXT,
                entry_date TIMESTAMPTZ,
                entry_price DOUBLE PRECISION,
                current_price DOUBLE PRECISION,
                pnl_pct DOUBLE PRECISION,
                days_held BIGINT,
                approved_allocation_inr DOUBLE PRECISION,
                overlap_group TEXT,
                stop_price DOUBLE PRECISION,
                invalidation_price DOUBLE PRECISION,
                next_action TEXT,
                next_action_reason TEXT,
                context_snapshot_json TEXT,
                load_ts TIMESTAMPTZ,
                UNIQUE (asof_date, published_on, setup_id, symbol, unique_id)
            )
            """
        )
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {REBALANCE_TABLE} (
                asof_date TIMESTAMPTZ NOT NULL,
                published_on TIMESTAMPTZ NOT NULL,
                setup_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                unique_id TEXT NOT NULL,
                suggested_action TEXT,
                action_reason TEXT,
                reference_price DOUBLE PRECISION,
                stop_price DOUBLE PRECISION,
                invalidation_price DOUBLE PRECISION,
                context_snapshot_json TEXT,
                load_ts TIMESTAMPTZ,
                UNIQUE (asof_date, published_on, setup_id, symbol, unique_id, suggested_action)
            )
            """
        )


def load_open_orders(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
) -> pd.DataFrame:
    if not table_exists(PORTFOLIO_TABLE):
        return pd.DataFrame()
    clauses = ["portfolio_status IN ('approved', 'trimmed')"]
    params: list[object] = []
    if asof_date is not None:
        clauses.append("asof_date <= %s")
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
        FROM {PORTFOLIO_TABLE}
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


def load_price_points(symbols: list[str], monitor_date: pd.Timestamp) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()
    df = sql_to_df(
        """
        SELECT ticker AS symbol, date, close
        FROM dhan_ohlcv_daily
        WHERE exchange = 'NSE'
          AND asset_type = 'stock'
          AND ticker = ANY(%(symbols)s)
          AND date <= %(monitor_date)s
        ORDER BY ticker, date
        """,
        params={"symbols": symbols, "monitor_date": monitor_date},
    )
    if df.empty:
        return df
    df["symbol"] = df["symbol"].astype("string").str.upper()
    df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df


def derive_entry_and_current_prices(orders: pd.DataFrame, monitor_date: pd.Timestamp) -> pd.DataFrame:
    prices = load_price_points(orders["symbol"].astype(str).unique().tolist(), monitor_date)
    if prices.empty:
        out = orders.copy()
        out["entry_date"] = pd.NaT
        out["entry_price"] = pd.NA
        out["current_price"] = pd.NA
        out["current_date"] = pd.NaT
        return out

    rows: list[dict[str, Any]] = []
    for _, row in orders.iterrows():
        symbol_prices = prices[prices["symbol"] == row["symbol"]].copy()
        entry_slice = symbol_prices[symbol_prices["date"] >= row["published_on"]]
        entry = entry_slice.iloc[0] if not entry_slice.empty else None
        current = symbol_prices.iloc[-1] if not symbol_prices.empty else None
        row_dict = row.to_dict()
        row_dict["entry_date"] = entry["date"] if entry is not None else pd.NaT
        row_dict["entry_price"] = entry["close"] if entry is not None else pd.NA
        row_dict["current_date"] = current["date"] if current is not None else pd.NaT
        row_dict["current_price"] = current["close"] if current is not None else pd.NA
        rows.append(row_dict)
    return pd.DataFrame(rows)


def classify_position(
    row: pd.Series,
    *,
    review_stale_days: int,
    tighten_stop_gain_pct: float,
    trim_winner_gain_pct: float,
) -> tuple[str, str, str, str]:
    entry_price = pd.to_numeric(row.get("entry_price"), errors="coerce")
    current_price = pd.to_numeric(row.get("current_price"), errors="coerce")
    stop_price = pd.to_numeric(row.get("stop_price"), errors="coerce")
    invalidation_price = pd.to_numeric(row.get("invalidation_price"), errors="coerce")
    pnl_pct = pd.to_numeric(row.get("pnl_pct"), errors="coerce")
    days_held_value = pd.to_numeric(row.get("days_held"), errors="coerce")
    days_held = 0 if pd.isna(days_held_value) else int(days_held_value)

    if pd.isna(entry_price) or pd.isna(current_price):
        return (
            "pending_entry",
            "No price history found on or after the planned entry date.",
            "review_manual",
            "Missing entry/current price prevents automated lifecycle management.",
        )
    if pd.notna(invalidation_price) and current_price <= invalidation_price:
        return ("exit_review", "Current price breached invalidation.", "exit_invalidation", "Price is below invalidation guidance.")
    if pd.notna(stop_price) and current_price <= stop_price:
        return ("exit_review", "Current price breached stop guidance.", "exit_stop", "Price is below stop guidance.")
    if pd.notna(pnl_pct) and pnl_pct >= trim_winner_gain_pct:
        return ("open", "Position is a strong winner.", "trim_winner", "Unrealized gain exceeded trim threshold.")
    if pd.notna(pnl_pct) and pnl_pct >= tighten_stop_gain_pct:
        return ("open", "Position is in profit and may warrant tighter risk.", "tighten_stop", "Unrealized gain exceeded stop-tightening threshold.")
    if days_held >= review_stale_days and (pd.isna(pnl_pct) or pnl_pct <= 0):
        return ("review", "Position is stale without positive mark-to-market.", "review_stale", "Holding period exceeded stale threshold without gains.")
    return ("open", "Position remains within expected risk bounds.", "hold", "No lifecycle trigger fired.")


def build_lifecycle_outputs(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
    review_stale_days: int = DEFAULT_REVIEW_STALE_DAYS,
    tighten_stop_gain_pct: float = DEFAULT_TIGHTEN_STOP_GAIN_PCT,
    trim_winner_gain_pct: float = DEFAULT_TRIM_WINNER_GAIN_PCT,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    monitor_date = pd.to_datetime(asof_date or pd.Timestamp.utcnow(), utc=True, errors="coerce").normalize()
    orders = load_open_orders(asof_date=monitor_date, symbols=symbols, setup_ids=setup_ids)
    if orders.empty:
        return pd.DataFrame(), pd.DataFrame()

    derived = derive_entry_and_current_prices(orders, monitor_date)
    rows: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    for _, row in derived.iterrows():
        entry_date = pd.to_datetime(row.get("entry_date"), utc=True, errors="coerce")
        current_date = pd.to_datetime(row.get("current_date"), utc=True, errors="coerce")
        entry_price = pd.to_numeric(row.get("entry_price"), errors="coerce")
        current_price = pd.to_numeric(row.get("current_price"), errors="coerce")
        pnl_pct = None
        if pd.notna(entry_price) and entry_price > 0 and pd.notna(current_price):
            pnl_pct = round(((float(current_price) / float(entry_price)) - 1.0) * 100.0, 4)
        days_held = None
        if pd.notna(entry_date) and pd.notna(current_date):
            days_held = int((current_date.normalize() - entry_date.normalize()).days)

        enriched = row.copy()
        enriched["pnl_pct"] = pnl_pct
        enriched["days_held"] = days_held
        position_status, lifecycle_reason, next_action, action_reason = classify_position(
            enriched,
            review_stale_days=review_stale_days,
            tighten_stop_gain_pct=tighten_stop_gain_pct,
            trim_winner_gain_pct=trim_winner_gain_pct,
        )

        context = {
            "published_on": str(row.get("published_on")),
            "entry_date": None if pd.isna(entry_date) else entry_date.isoformat(),
            "current_date": None if pd.isna(current_date) else current_date.isoformat(),
            "entry_price": None if pd.isna(entry_price) else float(entry_price),
            "current_price": None if pd.isna(current_price) else float(current_price),
            "pnl_pct": pnl_pct,
            "days_held": days_held,
            "portfolio_reason": row.get("portfolio_reason"),
            "overlap_group": row.get("overlap_group"),
        }

        rows.append(
            {
                "asof_date": monitor_date,
                "published_on": row["published_on"],
                "setup_id": row["setup_id"],
                "symbol": row["symbol"],
                "unique_id": row["unique_id"],
                "position_status": position_status,
                "lifecycle_reason": lifecycle_reason,
                "entry_date": entry_date,
                "entry_price": None if pd.isna(entry_price) else float(entry_price),
                "current_price": None if pd.isna(current_price) else float(current_price),
                "pnl_pct": pnl_pct,
                "days_held": days_held,
                "approved_allocation_inr": pd.to_numeric(row.get("approved_allocation_inr"), errors="coerce"),
                "overlap_group": row.get("overlap_group"),
                "stop_price": pd.to_numeric(row.get("stop_price"), errors="coerce"),
                "invalidation_price": pd.to_numeric(row.get("invalidation_price"), errors="coerce"),
                "next_action": next_action,
                "next_action_reason": action_reason,
                "context_snapshot_json": json.dumps(context, ensure_ascii=False, default=str, sort_keys=True),
                "load_ts": pd.Timestamp.utcnow(),
            }
        )

        if next_action != "hold":
            actions.append(
                {
                    "asof_date": monitor_date,
                    "published_on": row["published_on"],
                    "setup_id": row["setup_id"],
                    "symbol": row["symbol"],
                    "unique_id": row["unique_id"],
                    "suggested_action": next_action,
                    "action_reason": action_reason,
                    "reference_price": None if pd.isna(current_price) else float(current_price),
                    "stop_price": pd.to_numeric(row.get("stop_price"), errors="coerce"),
                    "invalidation_price": pd.to_numeric(row.get("invalidation_price"), errors="coerce"),
                    "context_snapshot_json": json.dumps(context, ensure_ascii=False, default=str, sort_keys=True),
                    "load_ts": pd.Timestamp.utcnow(),
                }
            )

    return pd.DataFrame(rows), pd.DataFrame(actions)


def persist_outputs(lifecycle_df: pd.DataFrame, actions_df: pd.DataFrame) -> None:
    ensure_lifecycle_tables()
    if not lifecycle_df.empty:
        lifecycle_out = lifecycle_df.copy()
        for column in [
            "entry_price",
            "current_price",
            "pnl_pct",
            "approved_allocation_inr",
            "stop_price",
            "invalidation_price",
        ]:
            if column in lifecycle_out.columns:
                lifecycle_out[column] = pd.to_numeric(lifecycle_out[column], errors="coerce")
        if "days_held" in lifecycle_out.columns:
            lifecycle_out["days_held"] = pd.to_numeric(lifecycle_out["days_held"], errors="coerce").astype("Int64")
        upsert_to_db(
            lifecycle_out,
            LIFECYCLE_TABLE,
            unique_keys=["asof_date", "published_on", "setup_id", "symbol", "unique_id"],
            timescaledb_column="asof_date",
        )
    if not actions_df.empty:
        actions_out = actions_df.copy()
        for column in ["reference_price", "stop_price", "invalidation_price"]:
            if column in actions_out.columns:
                actions_out[column] = pd.to_numeric(actions_out[column], errors="coerce")
        upsert_to_db(
            actions_out,
            REBALANCE_TABLE,
            unique_keys=["asof_date", "published_on", "setup_id", "symbol", "unique_id", "suggested_action"],
            timescaledb_column="asof_date",
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build paper-position lifecycle states and rebalance suggestions from approved advisory portfolio orders.")
    parser.add_argument("--date", type=parse_datetime_arg, help="Lifecycle asof date in YYYY-MM-DD")
    parser.add_argument("--symbols", nargs="*", help="Optional symbols")
    parser.add_argument("--setup", dest="setup_ids", nargs="*", help="Optional setup ids")
    parser.add_argument("--review-stale-days", type=int, default=DEFAULT_REVIEW_STALE_DAYS)
    parser.add_argument("--tighten-stop-gain-pct", type=float, default=DEFAULT_TIGHTEN_STOP_GAIN_PCT)
    parser.add_argument("--trim-winner-gain-pct", type=float, default=DEFAULT_TRIM_WINNER_GAIN_PCT)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def summarize(lifecycle_df: pd.DataFrame, actions_df: pd.DataFrame) -> dict[str, Any]:
    if lifecycle_df.empty:
        return {
            "status": "ok",
            "lifecycle_table": LIFECYCLE_TABLE,
            "actions_table": REBALANCE_TABLE,
            "row_count": 0,
            "action_count": 0,
            "sample": [],
        }
    return {
        "status": "ok",
        "lifecycle_table": LIFECYCLE_TABLE,
        "actions_table": REBALANCE_TABLE,
        "row_count": int(len(lifecycle_df)),
        "action_count": int(len(actions_df)),
        "status_counts": lifecycle_df["position_status"].value_counts(dropna=False).to_dict(),
        "action_counts": actions_df["suggested_action"].value_counts(dropna=False).to_dict() if not actions_df.empty else {},
        "sample": lifecycle_df.head(10).to_dict(orient="records"),
    }


def main() -> int:
    args = parse_args()
    asof_date = pd.Timestamp(args.date, tz="UTC") if args.date else None
    lifecycle_df, actions_df = build_lifecycle_outputs(
        asof_date=asof_date,
        symbols=args.symbols,
        setup_ids=args.setup_ids,
        review_stale_days=int(args.review_stale_days),
        tighten_stop_gain_pct=float(args.tighten_stop_gain_pct),
        trim_winner_gain_pct=float(args.trim_winner_gain_pct),
    )
    if not args.dry_run:
        persist_outputs(lifecycle_df, actions_df)
    result = summarize(lifecycle_df, actions_df)
    result["dry_run"] = bool(args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
