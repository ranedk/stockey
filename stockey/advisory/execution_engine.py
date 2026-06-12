from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from typing import Any

import pandas as pd

from advisory.action_recommender import TABLE_NAME as ACTIONS_TABLE, build_action_recommendations
from advisory.decision_trace import append_trace, append_trace_step, safe_trace_call
from advisory.fallback_telemetry import record_local_fallback_event
from data.dhanlive.client import DhanAPIError, DhanTradingClient
from data.dhanlive.dhan_db import resolve_dhan_identity
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration
from utils.sync import parse_datetime_arg


PORTFOLIO_TABLE = "advisory_portfolio_orders"
REBALANCE_TABLE = "advisory_rebalance_actions"
EXECUTION_TABLE = "advisory_execution_orders"
FILLS_TABLE = "advisory_execution_fills"
EXECUTION_SCHEMA_MIGRATION_ID = "20260611_advisory_execution_orders_base"
EXECUTION_SCHEMA_STATEMENTS = [
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
        reference_price_source TEXT,
        reference_price_asof TIMESTAMPTZ,
        approved_allocation_inr DOUBLE PRECISION,
        invest_score_pct DOUBLE PRECISION,
        estimated_order_value_inr DOUBLE PRECISION,
        safety_checks_json TEXT,
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
    """,
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
    """,
    f"ALTER TABLE {EXECUTION_TABLE} ADD COLUMN IF NOT EXISTS invest_score_pct DOUBLE PRECISION",
    f"ALTER TABLE {EXECUTION_TABLE} ADD COLUMN IF NOT EXISTS reference_price_source TEXT",
    f"ALTER TABLE {EXECUTION_TABLE} ADD COLUMN IF NOT EXISTS reference_price_asof TIMESTAMPTZ",
    f"ALTER TABLE {EXECUTION_TABLE} ADD COLUMN IF NOT EXISTS estimated_order_value_inr DOUBLE PRECISION",
    f"ALTER TABLE {EXECUTION_TABLE} ADD COLUMN IF NOT EXISTS safety_checks_json TEXT",
]
DEFAULT_MAX_LIVE_ORDERS_PER_RUN = 5
DEFAULT_MAX_LIVE_ORDER_VALUE_INR = 50_000.0
DEFAULT_MAX_INTRADAY_PRICE_AGE_MINUTES = 30
LIVE_RUN_CONFIRMATION_ENV = "STOCKEY_EXECUTION_LIVE_RUN_CONFIRMATION"
EXECUTION_APPROVAL_APPROVED_STATUSES = {"approved", "operator_approved"}
EXECUTION_RECONCILIATION_PASSED_STATUSES = {"passed", "ok", "reconciled"}
ACTION_ROW_CLOSED_STATUSES = {
    "closed",
    "ignored",
    "manual_closed",
    "operator_closed",
    "superseded",
    "cancelled",
    "canceled",
    "rejected",
}
logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(float(str(raw).strip()))
    except ValueError as exc:
        record_local_fallback_event(
            module="advisory.execution_engine",
            fallback_type="execution_env_int_parse_failed",
            source=name,
            severity="warn",
            reason="Execution engine environment integer setting was invalid; default value was used.",
            error=exc,
            metadata={"env_var": name, "raw_value": str(raw), "default": int(default)},
        )
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(str(raw).strip())
    except ValueError as exc:
        record_local_fallback_event(
            module="advisory.execution_engine",
            fallback_type="execution_env_float_parse_failed",
            source=name,
            severity="warn",
            reason="Execution engine environment numeric setting was invalid; default value was used.",
            error=exc,
            metadata={"env_var": name, "raw_value": str(raw), "default": float(default)},
        )
        return default


DEFAULT_ALLOW_LEGACY_EXECUTION_FALLBACK = _env_bool("EXECUTION_ALLOW_LEGACY_PORTFOLIO_FALLBACK", False)


def _record_execution_fallback(
    fallback_type: str,
    *,
    source: str,
    reason: str,
    error: Exception,
    severity: str = "warn",
    symbol: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    logger.warning(
        "execution degraded fallback_type=%s source=%s symbol=%s error=%s: %s",
        fallback_type,
        source,
        symbol,
        type(error).__name__,
        error,
    )
    record_local_fallback_event(
        module="advisory.execution_engine",
        fallback_type=fallback_type,
        source=source,
        severity=severity,
        reason=reason,
        error=error,
        symbol=symbol,
        metadata=metadata or {},
    )


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
    apply_schema_migration(
        migration_id=EXECUTION_SCHEMA_MIGRATION_ID,
        description="Create dry-run execution order and fill handoff tables.",
        statements=EXECUTION_SCHEMA_STATEMENTS,
        metadata={"tables": [EXECUTION_TABLE, FILLS_TABLE]},
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


def load_exit_actions(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
) -> pd.DataFrame:
    if not table_exists(REBALANCE_TABLE):
        return pd.DataFrame()
    clauses = ["(LOWER(suggested_action) LIKE 'exit_%%' OR LOWER(suggested_action) IN ('trim_winner', 'add_on_pullback'))"]
    params: list[object] = []
    if asof_date is not None:
        clauses.append("asof_date = %s")
        params.append(asof_date)
    else:
        clauses.append(f"asof_date = (SELECT MAX(asof_date) FROM {REBALANCE_TABLE})")
    if symbols:
        clauses.append("symbol = ANY(%s)")
        params.append([value.upper() for value in symbols])
    if setup_ids:
        clauses.append("setup_id = ANY(%s)")
        params.append([value.upper() for value in setup_ids])
    df = sql_to_df(
        f"""
        SELECT *
        FROM {REBALANCE_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY published_on DESC, symbol
        """,
        params=tuple(params) if params else None,
    )
    if df.empty:
        return df
    df["published_on"] = pd.to_datetime(df["published_on"], utc=True, errors="coerce")
    df["asof_date"] = normalize_timestamp(df["asof_date"])
    df["symbol"] = df["symbol"].astype("string").str.upper()
    return df.drop_duplicates(subset=["symbol"], keep="first").reset_index(drop=True)


def load_action_recommendations(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
) -> pd.DataFrame:
    if not table_exists(ACTIONS_TABLE):
        return pd.DataFrame()
    clauses = []
    params: list[object] = []
    if asof_date is not None:
        clauses.append("asof_date = %s")
        params.append(asof_date)
    else:
        clauses.append(f"asof_date = (SELECT MAX(asof_date) FROM {ACTIONS_TABLE})")
    if symbols:
        clauses.append("symbol = ANY(%s)")
        params.append([value.upper() for value in symbols])
    if setup_ids:
        clauses.append("setup_id = ANY(%s)")
        params.append([value.upper() for value in setup_ids])
    df = sql_to_df(
        f"""
        SELECT *
        FROM {ACTIONS_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY published_on DESC, action_priority DESC, symbol
        """,
        params=tuple(params) if params else None,
    )
    if df.empty:
        return df
    for column in ["asof_date", "published_on", "load_ts"]:
        if column in df.columns:
            df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    for column in ["action_fraction", "approved_allocation_inr", "reference_price", "invest_score_pct"]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    df["symbol"] = df["symbol"].astype("string").str.upper()
    return df.drop_duplicates(subset=["symbol"], keep="first").reset_index(drop=True)


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


def _execution_price_cutoff(asof_date: pd.Timestamp) -> pd.Timestamp:
    timestamp = pd.to_datetime(asof_date, utc=True, errors="coerce")
    now = pd.Timestamp.utcnow()
    if pd.isna(timestamp):
        return now
    if timestamp.normalize() >= now.normalize():
        return now
    return timestamp.normalize() + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)


def load_latest_intraday_prices(
    symbols: list[str],
    asof_date: pd.Timestamp,
    *,
    max_age_minutes: int = DEFAULT_MAX_INTRADAY_PRICE_AGE_MINUTES,
) -> pd.DataFrame:
    if not symbols or not table_exists("dhan_ohlcv_intraday"):
        return pd.DataFrame(columns=["symbol", "price", "price_asof", "price_source"])
    cutoff = _execution_price_cutoff(asof_date)
    min_timestamp = cutoff - pd.Timedelta(minutes=max(int(max_age_minutes), 1))
    df = sql_to_df(
        """
        WITH ranked AS (
            SELECT
                ticker AS symbol,
                "timestamp" AS price_asof,
                close AS price,
                interval_minutes,
                ROW_NUMBER() OVER (
                    PARTITION BY ticker
                    ORDER BY "timestamp" DESC, interval_minutes ASC
                ) AS rn
            FROM dhan_ohlcv_intraday
            WHERE exchange = 'NSE'
              AND asset_type = 'stock'
              AND ticker = ANY(%(symbols)s)
              AND "timestamp" <= %(cutoff)s
              AND "timestamp" >= %(min_timestamp)s
              AND close IS NOT NULL
        )
        SELECT symbol, price_asof, price, interval_minutes
        FROM ranked
        WHERE rn = 1
        """,
        params={"symbols": symbols, "cutoff": cutoff, "min_timestamp": min_timestamp},
    )
    if df.empty:
        return pd.DataFrame(columns=["symbol", "price", "price_asof", "price_source"])
    df["symbol"] = df["symbol"].astype("string").str.upper()
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["price_asof"] = pd.to_datetime(df["price_asof"], utc=True, errors="coerce")
    df["price_source"] = "intraday"
    return df.dropna(subset=["symbol", "price"]).drop_duplicates(subset=["symbol"], keep="last")


def load_latest_execution_prices(
    symbols: list[str],
    asof_date: pd.Timestamp,
    *,
    max_intraday_age_minutes: int | None = None,
) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame(columns=["symbol", "price", "price_asof", "price_source"])
    clean_symbols = sorted({str(symbol).upper() for symbol in symbols if str(symbol or "").strip()})
    max_age = max_intraday_age_minutes
    if max_age is None:
        max_age = _env_int("STOCKEY_EXECUTION_MAX_INTRADAY_PRICE_AGE_MINUTES", DEFAULT_MAX_INTRADAY_PRICE_AGE_MINUTES)
    frames: list[pd.DataFrame] = []
    intraday = load_latest_intraday_prices(clean_symbols, asof_date, max_age_minutes=max_age)
    if not intraday.empty:
        frames.append(intraday[["symbol", "price", "price_asof", "price_source"]])
    missing = clean_symbols
    if not intraday.empty:
        missing = sorted(set(clean_symbols) - set(intraday["symbol"].dropna().astype(str).str.upper().tolist()))
    daily = load_latest_closes(missing, pd.to_datetime(asof_date, utc=True, errors="coerce").normalize())
    if not daily.empty:
        daily = daily.rename(columns={"close": "price", "date": "price_asof"})
        daily["price_source"] = "daily_close"
        frames.append(daily[["symbol", "price", "price_asof", "price_source"]])
    if not frames:
        return pd.DataFrame(columns=["symbol", "price", "price_asof", "price_source"])
    out = pd.concat(frames, ignore_index=True, sort=False)
    out["price"] = pd.to_numeric(out["price"], errors="coerce")
    out["price_asof"] = pd.to_datetime(out["price_asof"], utc=True, errors="coerce")
    return out.dropna(subset=["symbol", "price"]).drop_duplicates(subset=["symbol"], keep="first")


def _payload_rows(payload: list[dict[str, Any]] | dict[str, Any] | None) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ["data", "records", "results", "list", "holdings", "positions"]:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                return _payload_rows(value)
        return [payload]
    return []


def extract_available_cash(payload: list[dict[str, Any]] | dict[str, Any] | None) -> float | None:
    rows = _payload_rows(payload)
    candidate_keys = [
        "availabelBalance",
        "availableBalance",
        "withdrawableBalance",
        "clearBalance",
        "balanceAvailable",
        "tradingBalance",
        "cashAvailable",
    ]
    values: list[float] = []
    for row in rows:
        for key in candidate_keys:
            value = pd.to_numeric(row.get(key), errors="coerce")
            if pd.notna(value):
                values.append(float(value))
    if not values and isinstance(payload, dict):
        for key in candidate_keys:
            value = pd.to_numeric(payload.get(key), errors="coerce")
            if pd.notna(value):
                values.append(float(value))
    if not values:
        return None
    return max(values)


def normalize_live_inventory(payload: list[dict[str, Any]] | dict[str, Any] | None) -> pd.DataFrame:
    rows = _payload_rows(payload)
    if not rows:
        return pd.DataFrame(columns=["symbol", "security_id", "available_quantity"])
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["symbol", "security_id", "available_quantity"])
    symbol_cols = ["tradingSymbol", "trading_symbol", "securitySymbol", "security_symbol", "ticker", "symbol"]
    security_id_cols = ["securityId", "security_id"]
    quantity_cols = ["availableQty", "availableQuantity", "netQty", "netQuantity", "quantity", "qty"]
    symbol_series = None
    for col in symbol_cols:
        if col in df.columns:
            symbol_series = df[col].astype("string").str.upper()
            break
    if symbol_series is None:
        symbol_series = pd.Series([pd.NA] * len(df), dtype="string")
    security_series = None
    for col in security_id_cols:
        if col in df.columns:
            security_series = pd.to_numeric(df[col], errors="coerce")
            break
    if security_series is None:
        security_series = pd.Series([pd.NA] * len(df), dtype="float64")
    quantity_series = None
    for col in quantity_cols:
        if col in df.columns:
            quantity_series = pd.to_numeric(df[col], errors="coerce")
            break
    if quantity_series is None:
        quantity_series = pd.Series([pd.NA] * len(df), dtype="float64")
    out = pd.DataFrame(
        {
            "symbol": symbol_series,
            "security_id": security_series,
            "available_quantity": quantity_series.fillna(0).clip(lower=0),
        }
    )
    out = out.dropna(subset=["symbol"])
    if out.empty:
        return pd.DataFrame(columns=["symbol", "security_id", "available_quantity"])
    return out.groupby("symbol", as_index=False, dropna=False).agg(
        security_id=("security_id", "max"),
        available_quantity=("available_quantity", "sum"),
    )


def load_live_account_budget(client: DhanTradingClient, *, strict: bool = False) -> tuple[float | None, pd.DataFrame]:
    available_cash = None
    inventory_frames: list[pd.DataFrame] = []
    errors: list[str] = []
    try:
        available_cash = extract_available_cash(client.get_fund_limits())
    except Exception as exc:
        errors.append(f"fund_limits:{exc.__class__.__name__}:{exc}")
        _record_execution_fallback(
            "execution_broker_fund_limits_failed",
            source="dhan_fund_limits",
            reason="Execution planning could not read broker fund limits; account-aware sizing may be unavailable.",
            error=exc,
            metadata={"strict": bool(strict)},
        )
        available_cash = None
    for loader in [client.get_holdings, client.get_positions]:
        try:
            frame = normalize_live_inventory(loader())
        except Exception as exc:
            loader_name = getattr(loader, "__name__", "inventory_loader")
            errors.append(f"{loader_name}:{exc.__class__.__name__}:{exc}")
            _record_execution_fallback(
                "execution_broker_inventory_failed",
                source=f"dhan_{loader_name}",
                reason="Execution planning could not read broker inventory; sell sizing and reconciliation context may be unavailable.",
                error=exc,
                metadata={"strict": bool(strict), "loader": loader_name},
            )
            continue
        if not frame.empty:
            inventory_frames.append(frame)
    if errors:
        print(
            "[advisory.execution_engine] broker account fetch warnings: " + " | ".join(errors),
            file=sys.stderr,
            flush=True,
        )
    if strict and available_cash is None and not inventory_frames:
        raise RuntimeError("Dhan broker account data unavailable; refusing broker-account sizing.")
    if not inventory_frames:
        return available_cash, pd.DataFrame(columns=["symbol", "security_id", "available_quantity"])
    inventory = pd.concat(inventory_frames, ignore_index=True, sort=False)
    inventory["symbol"] = inventory["symbol"].astype("string").str.upper()
    inventory["available_quantity"] = pd.to_numeric(inventory["available_quantity"], errors="coerce").fillna(0).clip(lower=0)
    return available_cash, inventory.groupby("symbol", as_index=False, dropna=False).agg(
        security_id=("security_id", "max"),
        available_quantity=("available_quantity", "sum"),
    )


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


def finalize_execution_plan_safety_contract(
    contract: dict[str, Any],
    *,
    execution_status: str,
    execution_reason: object = None,
    identity_status: str = "not_checked",
    identity_error: object = None,
    security_id: object = None,
) -> dict[str, Any]:
    out = dict(contract or {})
    issues = list(out.get("issues") or [])
    reason_text = str(execution_reason or "").strip()
    if execution_status != "planned" and reason_text and reason_text not in issues:
        issues.append(reason_text)
    out["issues"] = issues
    out["execution_plan_status"] = execution_status
    out["broker_identity_required"] = True
    out["broker_identity_status"] = identity_status
    out["broker_identity_resolved"] = identity_status == "resolved" and pd.notna(pd.to_numeric(security_id, errors="coerce"))
    if identity_error:
        out["broker_identity_error"] = str(identity_error)
    out["live_submission_allowed"] = False
    return out


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
    use_broker_account: bool = False,
) -> pd.DataFrame:
    monitor_date = pd.to_datetime(asof_date or pd.Timestamp.utcnow(), utc=True, errors="coerce").normalize()
    action_rows = load_action_recommendations(asof_date=monitor_date, symbols=symbols, setup_ids=setup_ids)
    if action_rows.empty:
        action_rows = build_action_recommendations(asof_date=monitor_date, symbols=symbols, setup_ids=setup_ids)
    use_action_table = not action_rows.empty
    portfolio_orders = pd.DataFrame()
    exit_actions = pd.DataFrame()
    if not use_action_table:
        if not DEFAULT_ALLOW_LEGACY_EXECUTION_FALLBACK:
            return pd.DataFrame()
        portfolio_orders = load_portfolio_orders(
            asof_date=monitor_date,
            symbols=symbols,
            setup_ids=setup_ids,
            include_existing=include_existing,
        )
        exit_actions = load_exit_actions(asof_date=monitor_date, symbols=symbols, setup_ids=setup_ids)
        if portfolio_orders.empty and exit_actions.empty:
            return pd.DataFrame()
        if not exit_actions.empty and not portfolio_orders.empty:
            rebalance_symbols = set(exit_actions["symbol"].dropna().astype(str).str.upper().tolist())
            if rebalance_symbols:
                portfolio_orders = portfolio_orders[
                    ~portfolio_orders["symbol"].astype("string").str.upper().isin(rebalance_symbols)
                ].copy()

    all_symbols = sorted(
        {
            *action_rows.get("symbol", pd.Series(dtype="object")).dropna().astype(str).str.upper().tolist(),
            *portfolio_orders.get("symbol", pd.Series(dtype="object")).dropna().astype(str).str.upper().tolist(),
            *exit_actions.get("symbol", pd.Series(dtype="object")).dropna().astype(str).str.upper().tolist(),
        }
    )
    prices = load_latest_execution_prices(all_symbols, monitor_date)
    price_map = prices.set_index("symbol")["price"].to_dict() if not prices.empty else {}
    price_source_map = prices.set_index("symbol")["price_source"].to_dict() if not prices.empty else {}
    price_asof_map = prices.set_index("symbol")["price_asof"].to_dict() if not prices.empty else {}
    available_cash = None
    live_inventory = pd.DataFrame(columns=["symbol", "security_id", "available_quantity"])
    if use_broker_account:
        client = DhanTradingClient()
        available_cash, live_inventory = load_live_account_budget(client, strict=True)
    inventory_map = (
        live_inventory.set_index("symbol")[["security_id", "available_quantity"]].to_dict(orient="index")
        if not live_inventory.empty
        else {}
    )

    rows: list[dict[str, Any]] = []
    remaining_cash = None if available_cash is None else float(max(available_cash, 0.0))
    if use_action_table:
        for _, row in action_rows.iterrows():
            symbol = str(row["symbol"]).upper()
            action_code = str(row.get("action_code") or "").upper()
            transaction = str(row.get("transaction_type") or "").upper()
            execution_mode = str(row.get("execution_mode") or "").lower()
            reason_contract_status = str(row.get("reason_contract_status") or "").strip().lower()
            reference_price = pd.to_numeric(price_map.get(symbol), errors="coerce")
            reference_price_source = price_source_map.get(symbol)
            reference_price_asof = price_asof_map.get(symbol)
            if pd.isna(reference_price):
                reference_price = pd.to_numeric(row.get("reference_price"), errors="coerce")
                if pd.notna(reference_price):
                    reference_price_source = "action_reference"
                    reference_price_asof = row.get("published_on") or row.get("asof_date")
            quantity = 0
            execution_status = "planned"
            execution_reason = None
            security_id = None
            exchange_segment = None
            identity_status = "not_checked"
            identity_error = None
            inventory = inventory_map.get(symbol, {})
            available_qty = pd.to_numeric(inventory.get("available_quantity"), errors="coerce")
            action_fraction = pd.to_numeric(row.get("action_fraction"), errors="coerce")
            row_block_reasons = _action_row_block_reasons(row, monitor_date=monitor_date)
            safety_contract = build_execution_plan_safety_contract(
                source="action_recommendation",
                action_status=row.get("action_status") if "action_status" in row.index else row.get("status"),
                issues=row_block_reasons.copy(),
            )

            if execution_mode not in {"", "broker_order"} or transaction not in {"BUY", "SELL"}:
                continue
            if row_block_reasons:
                execution_status = "submit_blocked"
                execution_reason = " ".join(row_block_reasons)
                identity_status = "skipped"
            elif reason_contract_status != "complete":
                execution_status = "submit_blocked"
                execution_reason = f"Reason contract is not complete: {reason_contract_status or 'missing'}."
                identity_status = "skipped"
            else:
                try:
                    identity = resolve_dhan_identity(symbol, "NSE", asset_type="stock")
                    security_id = int(identity["security_id"])
                    exchange_segment = str(identity["exchange_segment"])
                    identity_status = "resolved"
                except Exception as exc:
                    execution_status = "submit_blocked"
                    execution_reason = f"Identity resolution failed: {exc}"
                    identity_status = "failed"
                    identity_error = exc
                    _record_execution_fallback(
                        "execution_identity_resolution_failed",
                        source="dhan_identity",
                        reason="Execution planning blocked a broker-capable action because Dhan security identity could not be resolved.",
                        error=exc,
                        severity="error",
                        symbol=symbol,
                        metadata={
                            "action_code": action_code,
                            "transaction_type": transaction,
                            "execution_mode": execution_mode,
                        },
                    )

            if execution_status == "planned" and transaction == "BUY" and action_code == "BUY":
                approved_allocation = pd.to_numeric(row.get("approved_allocation_inr"), errors="coerce")
                if pd.isna(reference_price) or reference_price <= 0:
                    execution_status = "submit_blocked"
                    execution_reason = (execution_reason + " " if execution_reason else "") + "Missing reference close price."
                elif pd.isna(approved_allocation) or approved_allocation <= 0:
                    execution_status = "submit_blocked"
                    execution_reason = (execution_reason + " " if execution_reason else "") + "Approved allocation is not positive."
                else:
                    budget = float(approved_allocation)
                    if remaining_cash is not None:
                        budget = min(budget, remaining_cash)
                    quantity = int(budget // float(reference_price))
                    if quantity <= 0:
                        execution_status = "submit_blocked"
                        execution_reason = (execution_reason + " " if execution_reason else "") + "Approved allocation or available cash is too small for one share."
                    elif remaining_cash is not None:
                        remaining_cash = max(remaining_cash - (quantity * float(reference_price)), 0.0)
            elif execution_status == "planned" and transaction == "BUY" and action_code == "BUY_MORE":
                if pd.isna(available_qty) or float(available_qty) <= 0:
                    execution_status = "submit_blocked"
                    execution_reason = (execution_reason + " " if execution_reason else "") + "No live holding quantity available to size add-on."
                elif pd.isna(reference_price) or reference_price <= 0:
                    execution_status = "submit_blocked"
                    execution_reason = (execution_reason + " " if execution_reason else "") + "Missing reference close price for add-on order."
                else:
                    requested_fraction = float(action_fraction) if pd.notna(action_fraction) else 0.20
                    quantity = max(1, int(round(int(float(available_qty)) * requested_fraction)))
                    if remaining_cash is not None:
                        cash_limited_qty = int(float(remaining_cash) // float(reference_price))
                        quantity = min(quantity, cash_limited_qty)
                        if quantity > 0:
                            remaining_cash = max(remaining_cash - (quantity * float(reference_price)), 0.0)
                    if quantity <= 0:
                        execution_status = "submit_blocked"
                        execution_reason = (execution_reason + " " if execution_reason else "") + "Available cash is too small for one add-on share."
                    else:
                        execution_reason = (execution_reason + " " if execution_reason else "") + f"Add-on sized at {requested_fraction:.2f} of live holdings."
            elif execution_status == "planned" and transaction == "SELL":
                if pd.isna(available_qty) or float(available_qty) <= 0:
                    execution_status = "submit_blocked"
                    execution_reason = (execution_reason + " " if execution_reason else "") + "No live holding quantity available to exit."
                else:
                    available_qty_int = int(float(available_qty))
                    requested_fraction = float(action_fraction) if pd.notna(action_fraction) else 1.0
                    if 0 < requested_fraction < 1:
                        quantity = max(1, int(round(available_qty_int * requested_fraction)))
                        quantity = min(quantity, available_qty_int)
                        execution_reason = (execution_reason + " " if execution_reason else "") + f"Partial sell sized at {requested_fraction:.2f} of live holdings."
                    else:
                        quantity = available_qty_int

            safety_contract = finalize_execution_plan_safety_contract(
                safety_contract,
                execution_status=execution_status,
                execution_reason=execution_reason,
                identity_status=identity_status,
                identity_error=identity_error,
                security_id=security_id or inventory.get("security_id"),
            )
            order_intent_lineage = _build_action_order_intent_lineage(
                row,
                safety_contract=safety_contract,
                reference_price=reference_price,
                reference_price_source=reference_price_source,
                reference_price_asof=reference_price_asof,
            )
            safety_contract["order_intent_lineage"] = order_intent_lineage
            correlation_id = sanitize_correlation_id(f"{action_code}-{symbol}-{row.get('unique_id') or 'na'}")
            limit_price = None if order_type.upper() == "MARKET" else float(reference_price) if pd.notna(reference_price) else None
            rows.append(
                {
                    "asof_date": row["asof_date"],
                    "published_on": row.get("published_on") or row["asof_date"],
                    "setup_id": row.get("setup_id"),
                    "symbol": symbol,
                    "unique_id": row.get("unique_id") or f"{symbol}-{action_code}",
                    "company_master_id": None,
                    "correlation_id": correlation_id,
                    "security_id": security_id or inventory.get("security_id"),
                    "exchange_segment": exchange_segment or "NSE_EQ",
                    "transaction_type": transaction or None,
                    "product_type": product_type.upper(),
                    "order_type": order_type.upper(),
                    "validity": validity.upper(),
                    "quantity": quantity,
                    "filled_quantity": 0,
                    "limit_price": limit_price,
                    "trigger_price": None,
                    "reference_price": None if pd.isna(reference_price) else float(reference_price),
                    "reference_price_source": reference_price_source,
                    "reference_price_asof": reference_price_asof,
                    "approved_allocation_inr": pd.to_numeric(row.get("approved_allocation_inr"), errors="coerce"),
                    "invest_score_pct": pd.to_numeric(row.get("invest_score_pct"), errors="coerce"),
                    "estimated_order_value_inr": None if pd.isna(reference_price) or quantity <= 0 else float(reference_price) * int(quantity),
                    "safety_checks_json": json.dumps(safety_contract, ensure_ascii=False, default=str, sort_keys=True),
                    "broker_order_id": None,
                    "exchange_order_id": None,
                    "execution_status": execution_status,
                    "execution_reason": execution_reason.strip() if isinstance(execution_reason, str) else execution_reason,
                    "broker_order_status": None,
                    "submitted_at": None,
                    "broker_update_time": None,
                    "live_mode": False,
                    "raw_broker_json": json.dumps(
                        {
                            "action_code": row.get("action_code"),
                            "action_source": row.get("action_source"),
                            "source_action": row.get("source_action"),
                            "action_fraction": pd.to_numeric(row.get("action_fraction"), errors="coerce"),
                            "execution_mode": row.get("execution_mode"),
                            "reason_contract_status": row.get("reason_contract_status"),
                            "recommendation_reason_json": row.get("recommendation_reason_json"),
                            "order_intent_lineage": order_intent_lineage,
                            "recommended_stop_price": pd.to_numeric(row.get("recommended_stop_price"), errors="coerce"),
                            "recommended_target_price": pd.to_numeric(row.get("recommended_target_price"), errors="coerce"),
                            "stop_price": pd.to_numeric(row.get("stop_price"), errors="coerce"),
                            "target_price": pd.to_numeric(row.get("target_price"), errors="coerce"),
                            "invalidation_price": pd.to_numeric(row.get("invalidation_price"), errors="coerce"),
                            "reference_price_source": reference_price_source,
                            "reference_price_asof": reference_price_asof,
                            "execution_safety_contract": safety_contract,
                        },
                        ensure_ascii=False,
                        default=str,
                        sort_keys=True,
                    ),
                    "raw_trade_json": None,
                    "load_ts": pd.Timestamp.utcnow(),
                }
            )
        return pd.DataFrame(rows)

    for _, row in portfolio_orders.iterrows():
        symbol = str(row["symbol"]).upper()
        reference_price = pd.to_numeric(price_map.get(symbol), errors="coerce")
        reference_price_source = price_source_map.get(symbol)
        reference_price_asof = price_asof_map.get(symbol)
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
            _record_execution_fallback(
                "execution_portfolio_identity_resolution_failed",
                source="dhan_identity",
                reason="portfolio execution planning could not resolve broker security identity",
                error=exc,
                symbol=symbol,
                metadata={
                    "action_path": "portfolio_order",
                    "setup_id": str(row.get("setup_id") or ""),
                    "unique_id": str(row.get("unique_id") or ""),
                },
            )
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
            budget = float(approved_allocation)
            if remaining_cash is not None:
                budget = min(budget, remaining_cash)
            quantity = int(budget // float(reference_price))
            if quantity <= 0:
                execution_status = "submit_blocked"
                execution_reason = (execution_reason + " " if execution_reason else "") + "Approved allocation or available cash is too small for one share."
            elif remaining_cash is not None:
                remaining_cash = max(remaining_cash - (quantity * float(reference_price)), 0.0)

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
                "reference_price_source": reference_price_source,
                "reference_price_asof": reference_price_asof,
                "approved_allocation_inr": None if pd.isna(approved_allocation) else float(approved_allocation),
                "invest_score_pct": pd.to_numeric(row.get("invest_score_pct"), errors="coerce"),
                "estimated_order_value_inr": None if pd.isna(reference_price) or quantity <= 0 else float(reference_price) * int(quantity),
                "safety_checks_json": None,
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
    for _, row in exit_actions.iterrows():
        symbol = str(row["symbol"]).upper()
        reference_price = pd.to_numeric(price_map.get(symbol), errors="coerce")
        reference_price_source = price_source_map.get(symbol)
        reference_price_asof = price_asof_map.get(symbol)
        execution_status = "planned"
        execution_reason = None
        quantity = 0
        security_id = None
        exchange_segment = None
        inventory = inventory_map.get(symbol, {})
        suggested_action = str(row.get("suggested_action") or "").lower()
        action_fraction = pd.to_numeric(row.get("action_fraction"), errors="coerce")
        execution_mode = str(row.get("execution_mode") or "").lower()
        try:
            identity = resolve_dhan_identity(symbol, "NSE", asset_type="stock")
            security_id = int(identity["security_id"])
            exchange_segment = str(identity["exchange_segment"])
        except Exception as exc:
            _record_execution_fallback(
                "execution_exit_identity_resolution_failed",
                source="dhan_identity",
                reason="exit/add-on execution planning could not resolve broker security identity",
                error=exc,
                symbol=symbol,
                metadata={
                    "action_path": "exit_action",
                    "suggested_action": suggested_action,
                    "setup_id": str(row.get("setup_id") or ""),
                    "unique_id": str(row.get("unique_id") or ""),
                },
            )
            execution_status = "submit_blocked"
            execution_reason = f"Identity resolution failed: {exc}"
        available_qty = pd.to_numeric(inventory.get("available_quantity"), errors="coerce")
        is_add_on = suggested_action == "add_on_pullback"
        if execution_mode not in {"", "broker_order"} and suggested_action not in {"exit_invalidation", "exit_stop", "exit_emergency", "exit_technical_failure", "exit_time_stop", "trim_winner", "add_on_pullback"}:
            execution_status = "submit_blocked"
            execution_reason = (execution_reason + " " if execution_reason else "") + f"Execution mode {execution_mode} is not broker-submittable."
        else:
            if pd.isna(available_qty) or float(available_qty) <= 0:
                execution_status = "submit_blocked"
                execution_reason = (execution_reason + " " if execution_reason else "") + (
                    "No live holding quantity available to scale into."
                    if is_add_on
                    else "No live holding quantity available to exit."
                )
            else:
                available_qty_int = int(float(available_qty))
                requested_fraction = float(action_fraction) if pd.notna(action_fraction) else (0.33 if suggested_action == "trim_winner" else (0.20 if is_add_on else 1.0))
                if is_add_on:
                    quantity = max(1, int(round(available_qty_int * requested_fraction)))
                    if pd.isna(reference_price) or float(reference_price) <= 0:
                        execution_status = "submit_blocked"
                        execution_reason = (execution_reason + " " if execution_reason else "") + "Missing reference close price for add-on order."
                    elif remaining_cash is not None:
                        cash_limited_qty = int(float(remaining_cash) // float(reference_price))
                        quantity = min(quantity, cash_limited_qty)
                        if quantity <= 0:
                            execution_status = "submit_blocked"
                            execution_reason = (execution_reason + " " if execution_reason else "") + "Available cash is too small for one add-on share."
                        else:
                            remaining_cash = max(remaining_cash - (quantity * float(reference_price)), 0.0)
                    execution_reason = (execution_reason + " " if execution_reason else "") + f"Add-on pullback sized at {requested_fraction:.2f} of live holdings."
                elif 0 < requested_fraction < 1:
                    quantity = max(1, int(round(available_qty_int * requested_fraction)))
                    quantity = min(quantity, available_qty_int)
                    execution_reason = (execution_reason + " " if execution_reason else "") + f"Partial exit sized at {requested_fraction:.2f} of live holdings."
                else:
                    quantity = available_qty_int
        correlation_id = sanitize_correlation_id(f"EXIT-{row['symbol']}-{row['unique_id']}")
        rows.append(
            {
                "asof_date": row["asof_date"],
                "published_on": row["published_on"],
                "setup_id": row["setup_id"],
                "symbol": symbol,
                "unique_id": row["unique_id"],
                "company_master_id": None,
                "correlation_id": correlation_id,
                "security_id": security_id or inventory.get("security_id"),
                "exchange_segment": exchange_segment or "NSE_EQ",
                "transaction_type": "BUY" if is_add_on else "SELL",
                "product_type": "CNC",
                "order_type": "MARKET",
                "validity": "DAY",
                "quantity": quantity,
                "filled_quantity": 0,
                "limit_price": None,
                "trigger_price": None,
                "reference_price": None if pd.isna(reference_price) else float(reference_price),
                "reference_price_source": reference_price_source,
                "reference_price_asof": reference_price_asof,
                "approved_allocation_inr": None,
                "invest_score_pct": None,
                "estimated_order_value_inr": None if pd.isna(reference_price) or quantity <= 0 else float(reference_price) * int(quantity),
                "safety_checks_json": None,
                "broker_order_id": None,
                "exchange_order_id": None,
                "execution_status": execution_status,
                "execution_reason": execution_reason.strip() if isinstance(execution_reason, str) else execution_reason,
                "broker_order_status": None,
                "submitted_at": None,
                "broker_update_time": None,
                "live_mode": False,
                "raw_broker_json": json.dumps(
                    {
                        "action_source": row.get("suggested_action"),
                        "exit_fraction": float(action_fraction) if pd.notna(action_fraction) else (0.33 if suggested_action == "trim_winner" else (0.20 if is_add_on else 1.0)),
                        "execution_mode": row.get("execution_mode"),
                        "recommended_stop_price": pd.to_numeric(row.get("recommended_stop_price"), errors="coerce"),
                    },
                    ensure_ascii=False,
                    default=str,
                    sort_keys=True,
                ),
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
        "invest_score_pct",
        "estimated_order_value_inr",
    ]
    for col in numeric_cols:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    datetime_cols = ["asof_date", "published_on", "reference_price_asof", "submitted_at", "broker_update_time", "load_ts"]
    for col in datetime_cols:
        if col in out.columns:
            out[col] = pd.to_datetime(out[col], utc=True, errors="coerce")
    upsert_to_db(
        out,
        EXECUTION_TABLE,
        unique_keys=["asof_date", "published_on", "setup_id", "symbol", "unique_id"],
        timescaledb_column="asof_date",
    )
    _trace_execution_rows(out, stage="execution_planning", step_idx=60)


def _jsonish(value: Any, *, source: str = "execution_json_context", symbol: str | None = None) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        if pd.isna(value):
            return {}
    except Exception as exc:
        _record_execution_fallback(
            "execution_json_missing_check_failed",
            source=source,
            symbol=symbol,
            reason="Execution engine could not evaluate missingness for stored JSON context and continued parsing.",
            error=exc,
            metadata={"value_type": type(value).__name__},
        )
    try:
        parsed = json.loads(str(value or "{}"))
        return parsed if isinstance(parsed, dict) else {}
    except Exception as exc:
        text = str(value or "")
        _record_execution_fallback(
            "execution_json_parse_failed",
            source=source,
            symbol=symbol,
            reason="Execution engine could not parse stored JSON context and used an empty object fallback.",
            error=exc,
            metadata={"value_length": len(text), "value_excerpt": text[:240]},
        )
        return {}


def _trace_execution_rows(df: pd.DataFrame, *, stage: str, step_idx: int) -> None:
    if df.empty:
        return
    for _, row in df.iterrows():
        symbol = _clean_optional_text(row.get("symbol")).upper() or None
        safety_checks = _jsonish(row.get("safety_checks_json"), source="safety_checks_json", symbol=symbol)
        raw_broker = _jsonish(row.get("raw_broker_json"), source="raw_broker_json", symbol=symbol)
        payload = {
            "execution_status": row.get("execution_status"),
            "execution_reason": row.get("execution_reason"),
            "transaction_type": row.get("transaction_type"),
            "quantity": row.get("quantity"),
            "filled_quantity": row.get("filled_quantity"),
            "estimated_order_value_inr": row.get("estimated_order_value_inr"),
            "reference_price": row.get("reference_price"),
            "reference_price_source": row.get("reference_price_source"),
            "reference_price_asof": row.get("reference_price_asof"),
            "product_type": row.get("product_type"),
            "order_type": row.get("order_type"),
            "validity": row.get("validity"),
            "security_id": row.get("security_id"),
            "exchange_segment": row.get("exchange_segment"),
            "broker_order_id": row.get("broker_order_id"),
            "exchange_order_id": row.get("exchange_order_id"),
            "broker_order_status": row.get("broker_order_status"),
            "submitted_at": row.get("submitted_at"),
            "broker_update_time": row.get("broker_update_time"),
            "live_mode": row.get("live_mode"),
            "safety_checks": safety_checks,
            "raw_broker_action": raw_broker.get("action_code") or raw_broker.get("action_source"),
            "raw_broker_source": raw_broker.get("action_source"),
            "raw_broker_execution_mode": raw_broker.get("execution_mode"),
        }
        trace_id = safe_trace_call(
            append_trace,
            asof_date=row.get("asof_date"),
            symbol=row.get("symbol"),
            unique_id=row.get("unique_id"),
            setup_id=row.get("setup_id"),
            trigger_type=stage,
            final_action=row.get("execution_status"),
            final_reason=row.get("execution_reason"),
            source_table=EXECUTION_TABLE,
            source_key=f"{row.get('asof_date')}:{row.get('published_on')}:{row.get('setup_id')}:{row.get('symbol')}:{row.get('unique_id')}",
            payload=payload,
        )
        if trace_id:
            safe_trace_call(
                append_trace_step,
                trace_id=trace_id,
                step_idx=step_idx,
                stage=stage,
                status=str(row.get("execution_status") or "planned"),
                reason=row.get("execution_reason"),
                input_payload=raw_broker,
                output_payload=row.to_dict(),
                payload=payload,
            )


def _append_reason(existing: object, reason: str) -> str:
    text = str(existing or "").strip()
    return f"{text} {reason}".strip() if text else reason


def build_execution_plan_safety_contract(
    *,
    source: str,
    action_status: object = None,
    approval_status: str = "missing",
    reconciliation_status: str = "not_run",
    approval_required: bool = True,
    reconciliation_required: bool = True,
    live_submission_allowed: bool = False,
    issues: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "source": source,
        "action_status": None if pd.isna(action_status) else action_status,
        "operator_approval_required": bool(approval_required),
        "operator_approval_status": approval_status,
        "broker_reconciliation_required": bool(reconciliation_required),
        "broker_reconciliation_status": reconciliation_status,
        "live_submission_allowed": bool(live_submission_allowed),
        "issues": issues or [],
    }


def _optional_number(value: object) -> float | None:
    numeric = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(numeric) else float(numeric)


def _build_action_order_intent_lineage(
    row: pd.Series,
    *,
    safety_contract: dict[str, Any],
    reference_price: object = None,
    reference_price_source: object = None,
    reference_price_asof: object = None,
) -> dict[str, Any]:
    reason_contract = _jsonish(
        row.get("recommendation_reason_json"),
        source="recommendation_reason_json",
        symbol=_clean_optional_text(row.get("symbol")).upper() or None,
    )
    lineage = {
        "source": "advisory_action_recommendations",
        "asof_date": row.get("asof_date"),
        "published_on": row.get("published_on"),
        "setup_id": row.get("setup_id"),
        "symbol": row.get("symbol"),
        "unique_id": row.get("unique_id"),
        "action_code": row.get("action_code"),
        "action_source": row.get("action_source"),
        "source_action": row.get("source_action"),
        "transaction_type": row.get("transaction_type"),
        "execution_mode": row.get("execution_mode"),
        "action_status": row.get("action_status") if "action_status" in row.index else row.get("status"),
        "reason_contract_status": row.get("reason_contract_status"),
        "reason_contract_available": bool(reason_contract),
        "risk_sizing": {
            "approved_allocation_inr": _optional_number(row.get("approved_allocation_inr")),
            "invest_score_pct": _optional_number(row.get("invest_score_pct")),
            "action_fraction": _optional_number(row.get("action_fraction")),
        },
        "risk_levels": {
            "reference_price": _optional_number(reference_price),
            "reference_price_source": reference_price_source,
            "reference_price_asof": reference_price_asof,
            "stop_price": _optional_number(row.get("stop_price")),
            "target_price": _optional_number(row.get("target_price")),
            "invalidation_price": _optional_number(row.get("invalidation_price")),
            "recommended_stop_price": _optional_number(row.get("recommended_stop_price")),
            "recommended_target_price": _optional_number(row.get("recommended_target_price")),
        },
        "approval": {
            "operator_approval_required": safety_contract.get("operator_approval_required"),
            "operator_approval_status": safety_contract.get("operator_approval_status"),
            "broker_reconciliation_required": safety_contract.get("broker_reconciliation_required"),
            "broker_reconciliation_status": safety_contract.get("broker_reconciliation_status"),
        },
    }
    if reason_contract:
        lineage["reason_contract"] = {
            "status": reason_contract.get("status"),
            "action": reason_contract.get("action"),
            "summary": reason_contract.get("summary") or reason_contract.get("headline"),
            "missing_fields": reason_contract.get("missing_fields"),
            "manual_review_boundary": reason_contract.get("manual_review_boundary"),
            "sections_present": sorted([key for key, value in reason_contract.items() if isinstance(value, dict) and value]),
        }
    return lineage


def _safety_contract_from_row(row: pd.Series) -> dict[str, Any]:
    symbol = _clean_optional_text(row.get("symbol")).upper() or None
    parsed = _jsonish(row.get("safety_checks_json"), source="safety_checks_json", symbol=symbol)
    if parsed:
        return parsed
    raw = _jsonish(row.get("raw_broker_json"), source="raw_broker_json", symbol=symbol)
    return raw.get("execution_safety_contract") if isinstance(raw.get("execution_safety_contract"), dict) else {}


def _clean_optional_text(value: object) -> str:
    try:
        if pd.isna(value):
            return ""
    except Exception as exc:
        _record_execution_fallback(
            "execution_text_missing_check_failed",
            source="clean_optional_text",
            symbol=None,
            reason="Execution engine could not evaluate missingness for optional text and kept string conversion fallback.",
            error=exc,
            metadata={"value_type": type(value).__name__},
        )
    return str(value or "").strip()


def _annotate_reconciliation_contract(row: pd.Series) -> dict[str, Any]:
    status = _clean_optional_text(row.get("broker_order_status"))
    broker_order_id = _clean_optional_text(row.get("broker_order_id"))
    if not status and not broker_order_id:
        return {}
    return {
        "broker_reconciliation_status": "reconciled",
        "broker_reconciliation_source": "broker_order_state",
        "broker_reconciliation_broker_order_status": status or None,
        "broker_reconciliation_broker_order_id": broker_order_id or None,
    }


def annotate_reconciliation_safety_contracts(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    for idx, row in out.iterrows():
        updates = _annotate_reconciliation_contract(row)
        if not updates:
            continue
        contract = _safety_contract_from_row(row)
        if not contract:
            contract = build_execution_plan_safety_contract(
                source="execution_reconciliation",
                approval_required=True,
                reconciliation_status="reconciled",
                live_submission_allowed=False,
            )
        contract = {**contract, **updates, "live_submission_allowed": False}
        out.at[idx, "safety_checks_json"] = json.dumps(contract, ensure_ascii=False, default=str, sort_keys=True)
        raw = _jsonish(row.get("raw_broker_json"), source="raw_broker_json", symbol=_clean_optional_text(row.get("symbol")).upper() or None)
        if raw:
            raw["execution_safety_contract"] = contract
            out.at[idx, "raw_broker_json"] = json.dumps(raw, ensure_ascii=False, default=str, sort_keys=True)
    return out


def _action_row_block_reasons(row: pd.Series, *, monitor_date: pd.Timestamp) -> list[str]:
    reasons: list[str] = []
    for column in ["action_status", "status", "source_status", "resolution_status", "operator_status"]:
        if column not in row.index:
            continue
        value = str(row.get(column) or "").strip().lower()
        if value in ACTION_ROW_CLOSED_STATUSES:
            reasons.append(f"Action row is {value}.")
            break
    for column in ["closed_at", "manual_closed_at", "operator_closed_at", "superseded_at", "cancelled_at", "canceled_at"]:
        if column in row.index and pd.notna(row.get(column)):
            reasons.append(f"Action row has {column}.")
            break
    published_on = pd.to_datetime(row.get("published_on"), utc=True, errors="coerce")
    if pd.notna(published_on) and published_on.normalize() > monitor_date:
        reasons.append("Action row is dated after the execution as-of date.")
    expiry_value = row.get("expires_at")
    if pd.isna(expiry_value):
        expiry_value = row.get("valid_until")
    expires_at = pd.to_datetime(expiry_value, utc=True, errors="coerce")
    if pd.notna(expires_at) and expires_at < monitor_date:
        reasons.append("Action row is expired for the execution as-of date.")
    return reasons


def build_live_execution_confirmation_token(df: pd.DataFrame) -> str:
    planned_count = int(len(df))
    date_value = None
    if "asof_date" in df.columns and not df.empty:
        dates = pd.to_datetime(df["asof_date"], utc=True, errors="coerce").dropna()
        if not dates.empty:
            date_value = dates.min().normalize().date().isoformat()
    if not date_value:
        date_value = pd.Timestamp.utcnow().normalize().date().isoformat()
    return f"STOCKEY-LIVE-{date_value}-{planned_count}"


def apply_live_execution_safety(df: pd.DataFrame, *, live_confirmation: str | None = None) -> pd.DataFrame:
    out = df.copy()
    if out.empty:
        return out
    planned_mask = out["execution_status"].astype(str).eq("planned") if "execution_status" in out.columns else pd.Series(False, index=out.index)
    if not planned_mask.any():
        return out

    live_enabled = _env_bool("STOCKEY_LIVE_TRADING_ENABLED", False)
    max_orders = _env_int("STOCKEY_EXECUTION_MAX_LIVE_ORDERS_PER_RUN", DEFAULT_MAX_LIVE_ORDERS_PER_RUN)
    max_value = _env_float("STOCKEY_EXECUTION_MAX_ORDER_VALUE_INR", DEFAULT_MAX_LIVE_ORDER_VALUE_INR)
    require_fresh_intraday = _env_bool("STOCKEY_EXECUTION_REQUIRE_FRESH_INTRADAY_PRICE", True)
    max_price_age_minutes = _env_int("STOCKEY_EXECUTION_MAX_INTRADAY_PRICE_AGE_MINUTES", DEFAULT_MAX_INTRADAY_PRICE_AGE_MINUTES)
    require_operator_approval = _env_bool("STOCKEY_EXECUTION_REQUIRE_OPERATOR_APPROVAL", True)
    require_reconciliation = _env_bool("STOCKEY_EXECUTION_REQUIRE_RECONCILIATION", True)
    provided_confirmation = (live_confirmation or os.getenv(LIVE_RUN_CONFIRMATION_ENV) or "").strip()

    def block(idx: int, reason: str, checks: dict[str, Any]) -> None:
        out.at[idx, "execution_status"] = "submit_blocked"
        out.at[idx, "execution_reason"] = _append_reason(out.at[idx, "execution_reason"], reason)
        out.at[idx, "live_mode"] = False
        out.at[idx, "safety_checks_json"] = json.dumps(checks, ensure_ascii=False, default=str, sort_keys=True)

    planned_indexes = out.index[planned_mask].tolist()
    confirmation_token = build_live_execution_confirmation_token(out.loc[planned_indexes])
    base_checks = {
        "live_enabled": live_enabled,
        "max_orders_per_run": max_orders,
        "max_order_value_inr": max_value,
        "require_fresh_intraday_price": require_fresh_intraday,
        "max_intraday_price_age_minutes": max_price_age_minutes,
        "require_operator_approval": require_operator_approval,
        "require_reconciliation": require_reconciliation,
        "live_run_confirmation_required": True,
        "live_run_confirmation_env": LIVE_RUN_CONFIRMATION_ENV,
        "live_run_confirmation_expected": confirmation_token,
        "live_run_confirmation_provided": bool(provided_confirmation),
    }
    if not live_enabled:
        for idx in planned_indexes:
            block(idx, "Live trading disabled; set STOCKEY_LIVE_TRADING_ENABLED=true to submit.", base_checks)
        return out
    if provided_confirmation != confirmation_token:
        for idx in planned_indexes:
            block(
                idx,
                f"Live run confirmation is required; pass --live-confirmation {confirmation_token} or set {LIVE_RUN_CONFIRMATION_ENV}.",
                base_checks,
            )
        return out
    if max_orders > 0 and len(planned_indexes) > max_orders:
        checks = {**base_checks, "planned_order_count": len(planned_indexes)}
        for idx in planned_indexes:
            block(idx, f"Live order count {len(planned_indexes)} exceeds cap {max_orders}; refusing partial submission.", checks)
        return out

    now = pd.Timestamp.utcnow()
    for idx in planned_indexes:
        quantity = pd.to_numeric(out.at[idx, "quantity"], errors="coerce")
        reference_price = pd.to_numeric(out.at[idx, "reference_price"], errors="coerce")
        estimated_value = pd.to_numeric(out.at[idx, "estimated_order_value_inr"], errors="coerce")
        if pd.isna(estimated_value) and pd.notna(quantity) and pd.notna(reference_price):
            estimated_value = float(quantity) * float(reference_price)
            out.at[idx, "estimated_order_value_inr"] = estimated_value
        checks = {
            **base_checks,
            "estimated_order_value_inr": None if pd.isna(estimated_value) else float(estimated_value),
            "reference_price_source": out.at[idx, "reference_price_source"] if "reference_price_source" in out.columns else None,
            "reference_price_asof": out.at[idx, "reference_price_asof"] if "reference_price_asof" in out.columns else None,
        }
        plan_contract = _safety_contract_from_row(out.loc[idx])
        approval_status = str(plan_contract.get("operator_approval_status") or "").strip().lower()
        reconciliation_status = str(plan_contract.get("broker_reconciliation_status") or "").strip().lower()
        checks["execution_safety_contract"] = plan_contract
        if pd.isna(quantity) or int(quantity) <= 0:
            block(idx, "Quantity is not positive.", checks)
            continue
        if pd.isna(reference_price) or float(reference_price) <= 0:
            block(idx, "Missing positive reference price.", checks)
            continue
        if pd.isna(out.at[idx, "security_id"]):
            block(idx, "Missing Dhan security id.", checks)
            continue
        if str(out.at[idx, "transaction_type"]).upper() not in {"BUY", "SELL"}:
            block(idx, "Unsupported transaction type.", checks)
            continue
        if max_value > 0 and pd.notna(estimated_value) and float(estimated_value) > max_value:
            block(idx, f"Estimated order value exceeds live cap {max_value:.2f}.", checks)
            continue
        if require_fresh_intraday:
            price_source = str(out.at[idx, "reference_price_source"] or "")
            price_asof = pd.to_datetime(out.at[idx, "reference_price_asof"], utc=True, errors="coerce")
            price_age_minutes = None if pd.isna(price_asof) else (now - price_asof).total_seconds() / 60.0
            checks["price_age_minutes"] = price_age_minutes
            if price_source != "intraday":
                block(idx, "Live submission requires fresh intraday reference price.", checks)
                continue
            if price_age_minutes is None or price_age_minutes > max_price_age_minutes:
                block(idx, f"Intraday reference price is older than {max_price_age_minutes} minutes.", checks)
                continue
        if require_operator_approval and approval_status not in EXECUTION_APPROVAL_APPROVED_STATUSES:
            block(idx, "Operator approval is required before live submission.", checks)
            continue
        if require_reconciliation and reconciliation_status not in EXECUTION_RECONCILIATION_PASSED_STATUSES:
            block(idx, "Broker account reconciliation is required before live submission.", checks)
            continue
        out.at[idx, "safety_checks_json"] = json.dumps({**checks, "passed": True}, ensure_ascii=False, default=str, sort_keys=True)
    return out


def submit_live_orders(df: pd.DataFrame, *, live_confirmation: str | None = None) -> pd.DataFrame:
    if df.empty:
        return df
    out = apply_live_execution_safety(df, live_confirmation=live_confirmation)
    if not out["execution_status"].astype(str).eq("planned").any():
        _trace_execution_rows(out, stage="execution_safety", step_idx=61)
        return out
    client = DhanTradingClient()
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
            _record_execution_fallback(
                "execution_live_submit_failed",
                source="dhan_place_order",
                reason="Live broker order submission failed; row was marked submit_error and no success was assumed.",
                error=exc,
                severity="error",
                symbol=str(row.get("symbol") or "").strip().upper() or None,
                metadata={
                    "correlation_id": row.get("correlation_id"),
                    "transaction_type": row.get("transaction_type"),
                    "security_id": row.get("security_id"),
                },
            )
    _trace_execution_rows(out, stage="execution_submission", step_idx=62)
    return out


def load_recon_targets(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
) -> pd.DataFrame:
    try:
        exists = table_exists(EXECUTION_TABLE)
    except Exception as exc:
        _record_execution_fallback(
            "execution_recon_table_lookup_failed",
            source=EXECUTION_TABLE,
            reason="Execution reconciliation could not check the execution-order table; reconciliation target load was skipped.",
            error=exc,
        )
        return pd.DataFrame()
    if not exists:
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
    try:
        df = sql_to_df(
            f"""
            SELECT *
            FROM {EXECUTION_TABLE}
            WHERE {' AND '.join(clauses)}
            ORDER BY published_on, setup_id, symbol
            """,
            params=tuple(params) if params else None,
        )
    except Exception as exc:
        _record_execution_fallback(
            "execution_recon_targets_load_failed",
            source=EXECUTION_TABLE,
            reason="Execution reconciliation could not load target rows; broker reconciliation may be stale or unavailable.",
            error=exc,
            metadata={"symbols": symbols or [], "setup_ids": setup_ids or [], "asof_date": str(asof_date) if asof_date is not None else None},
        )
        return pd.DataFrame()
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
            _record_execution_fallback(
                "execution_broker_reconcile_failed",
                source="dhan_order_reconciliation",
                reason="Execution reconciliation failed for a broker order; row was marked reconcile_error and no success was assumed.",
                error=exc,
                severity="error",
                symbol=str(row.get("symbol") or "").strip().upper() or None,
                metadata={
                    "correlation_id": row.get("correlation_id"),
                    "broker_order_id": row.get("broker_order_id"),
                    "execution_status": row.get("execution_status"),
                },
            )
        row_dict["load_ts"] = pd.Timestamp.utcnow()
        order_rows.append(row_dict)
    return pd.DataFrame(order_rows), pd.DataFrame(fill_rows)


def persist_reconciliation(order_df: pd.DataFrame, fills_df: pd.DataFrame) -> None:
    ensure_execution_tables()
    if not order_df.empty:
        order_out = annotate_reconciliation_safety_contracts(order_df)
        for col in ["security_id", "quantity", "filled_quantity", "limit_price", "trigger_price", "reference_price", "approved_allocation_inr", "invest_score_pct", "estimated_order_value_inr"]:
            if col in order_out.columns:
                order_out[col] = pd.to_numeric(order_out[col], errors="coerce")
        for col in ["asof_date", "published_on", "reference_price_asof", "submitted_at", "broker_update_time", "load_ts"]:
            if col in order_out.columns:
                order_out[col] = pd.to_datetime(order_out[col], utc=True, errors="coerce")
        upsert_to_db(
            order_out,
            EXECUTION_TABLE,
            unique_keys=["asof_date", "published_on", "setup_id", "symbol", "unique_id"],
            timescaledb_column="asof_date",
        )
        _trace_execution_rows(order_out, stage="execution_reconciliation", step_idx=63)
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
    parser.add_argument("--use-broker-account", action="store_true", help="Use Dhan cash/holdings to cap staged order quantities")
    parser.add_argument("--live-confirmation", help="Per-run confirmation token required for --live submissions; preview token is shown in blocked safety checks")
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
            use_broker_account=bool(args.use_broker_account or (args.live and _env_bool("STOCKEY_LIVE_TRADING_ENABLED", False))),
        )
        if args.live:
            planned_df = submit_live_orders(planned_df, live_confirmation=args.live_confirmation)
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
