from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from advisory.decision_trace import append_trace, append_trace_step, safe_trace_call
from advisory.technical_engine import evaluate_post_entry_state as evaluate_technical_post_entry_state
from data.dhanlive.dhan_db import resolve_dhan_identity
from utils.db import db_session, sql_to_df, upsert_to_db
from utils.display_time import to_display_value
from utils.sync import parse_datetime_arg


PORTFOLIO_TABLE = "advisory_portfolio_orders"
LIFECYCLE_TABLE = "advisory_position_lifecycle"
REBALANCE_TABLE = "advisory_rebalance_actions"

DEFAULT_REVIEW_STALE_DAYS = 20
DEFAULT_TIGHTEN_STOP_GAIN_PCT = 10.0
DEFAULT_TRIM_WINNER_GAIN_PCT = 15.0
DEFAULT_TARGET_BUCKET_HORIZON_DAYS = 90
DEFAULT_TIME_BUCKET_HORIZON_DAYS = 42
DEFAULT_DATA_BUCKET_HORIZON_DAYS = 60
DEFAULT_TARGET_R_MULTIPLE = 2.0
DEFAULT_TIME_R_MULTIPLE = 1.5
DEFAULT_DATA_R_MULTIPLE = 2.25
POST_ENTRY_TECHNICAL_CONTEXT_COLUMNS = {
    "pass_above_dma_20",
    "pass_above_dma_50",
    "pass_trend_alignment",
    "distribution_days_20d",
    "rs_vs_sector",
    "breakout_extension_pct",
    "support_distance_20d_pct",
    "pullback_volume_dryup_ratio_20d",
    "gap_pct",
}


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


def has_post_entry_technical_context(row: pd.Series) -> bool:
    return any(column in row.index and pd.notna(row.get(column)) for column in POST_ENTRY_TECHNICAL_CONTEXT_COLUMNS)


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
                thesis_bucket TEXT,
                bucket_reason TEXT,
                target_price DOUBLE PRECISION,
                target_basis TEXT,
                target_confidence DOUBLE PRECISION,
                expected_horizon_days BIGINT,
                target_review_date TIMESTAMPTZ,
                horizon_end_date TIMESTAMPTZ,
                exit_event_rules_json TEXT,
                active_exit_condition TEXT,
                exit_condition_status TEXT,
                bucket_status_note TEXT,
                next_action TEXT,
                next_action_reason TEXT,
                context_snapshot_json TEXT,
                load_ts TIMESTAMPTZ,
                UNIQUE (asof_date, published_on, setup_id, symbol, unique_id)
            )
            """
        )
        lifecycle_column_defs = {
            "thesis_bucket": "TEXT",
            "bucket_reason": "TEXT",
            "target_price": "DOUBLE PRECISION",
            "target_basis": "TEXT",
            "target_confidence": "DOUBLE PRECISION",
            "expected_horizon_days": "BIGINT",
            "target_review_date": "TIMESTAMPTZ",
            "horizon_end_date": "TIMESTAMPTZ",
            "exit_event_rules_json": "TEXT",
            "active_exit_condition": "TEXT",
            "exit_condition_status": "TEXT",
            "bucket_status_note": "TEXT",
        }
        for column, sql_type in lifecycle_column_defs.items():
            cur.execute(f"ALTER TABLE {LIFECYCLE_TABLE} ADD COLUMN IF NOT EXISTS {column} {sql_type}")
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
                target_price DOUBLE PRECISION,
                recommended_stop_price DOUBLE PRECISION,
                recommended_target_price DOUBLE PRECISION,
                expected_horizon_days BIGINT,
                action_fraction DOUBLE PRECISION,
                execution_mode TEXT,
                context_snapshot_json TEXT,
                load_ts TIMESTAMPTZ,
                UNIQUE (asof_date, published_on, setup_id, symbol, unique_id, suggested_action)
            )
            """
        )
        rebalance_column_defs = {
            "recommended_stop_price": "DOUBLE PRECISION",
            "target_price": "DOUBLE PRECISION",
            "recommended_target_price": "DOUBLE PRECISION",
            "expected_horizon_days": "BIGINT",
            "action_fraction": "DOUBLE PRECISION",
            "execution_mode": "TEXT",
        }
        for column, sql_type in rebalance_column_defs.items():
            cur.execute(f"ALTER TABLE {REBALANCE_TABLE} ADD COLUMN IF NOT EXISTS {column} {sql_type}")


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
    df = df.sort_values(
        ["symbol", "portfolio_status", "approved_allocation_inr", "priority_score", "published_on", "setup_id"],
        ascending=[True, True, False, False, False, True],
        kind="stable",
    )
    return df.drop_duplicates(subset=["symbol"], keep="first").reset_index(drop=True)


def resolve_price_identities(symbols: list[str]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for symbol in sorted({str(value).upper() for value in symbols if str(value).strip()}):
        try:
            identity = resolve_dhan_identity(symbol, "NSE", asset_type="stock")
        except Exception:
            continue
        rows.append(
            {
                "symbol": symbol,
                "resolved_ticker": str(identity["ticker"]).strip().upper(),
                "exchange": str(identity["exchange"]).upper(),
                "security_id": int(identity["security_id"]),
            }
        )
    return pd.DataFrame(rows)


def load_price_points(symbols: list[str], monitor_date: pd.Timestamp) -> pd.DataFrame:
    identities = resolve_price_identities(symbols)
    if identities.empty:
        return pd.DataFrame()
    security_ids = identities["security_id"].dropna().astype(int).unique().tolist()
    df = sql_to_df(
        """
        SELECT exchange, security_id, ticker, date, close
        FROM dhan_ohlcv_daily
        WHERE asset_type = 'stock'
          AND security_id = ANY(%(security_ids)s)
          AND date <= %(monitor_date)s
        ORDER BY exchange, security_id, date
        """,
        params={"security_ids": security_ids, "monitor_date": monitor_date},
    )
    if df.empty:
        return df
    merged = df.merge(
        identities,
        how="inner",
        on=["exchange", "security_id"],
    )
    if merged.empty:
        return merged
    merged["symbol"] = merged["symbol"].astype("string").str.upper()
    merged["date"] = pd.to_datetime(merged["date"], utc=True, errors="coerce")
    merged["close"] = pd.to_numeric(merged["close"], errors="coerce")
    return merged[["symbol", "date", "close", "exchange", "security_id", "ticker"]]


def load_latest_technical_context(symbols: list[str], monitor_date: pd.Timestamp) -> pd.DataFrame:
    normalized = sorted({str(value).upper() for value in symbols if str(value).strip()})
    if not normalized or not table_exists("advisory_technical_daily"):
        return pd.DataFrame()
    df = sql_to_df(
        """
        SELECT DISTINCT ON (symbol)
            *
        FROM advisory_technical_daily
        WHERE symbol = ANY(%(symbols)s)
          AND asof_date <= %(monitor_date)s
        ORDER BY symbol, asof_date DESC
        """,
        params={"symbols": normalized, "monitor_date": monitor_date},
    )
    if df.empty:
        return df
    df["symbol"] = df["symbol"].astype("string").str.upper()
    if "asof_date" in df.columns:
        df["asof_date"] = pd.to_datetime(df["asof_date"], utc=True, errors="coerce")
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
        published_on = pd.to_datetime(row.get("published_on"), utc=True, errors="coerce")
        entry_anchor = published_on.normalize() if not pd.isna(published_on) else pd.NaT
        entry_slice = symbol_prices[symbol_prices["date"] >= entry_anchor] if not pd.isna(entry_anchor) else symbol_prices
        entry = entry_slice.iloc[0] if not entry_slice.empty else None
        current = symbol_prices.iloc[-1] if not symbol_prices.empty else None
        row_dict = row.to_dict()
        row_dict["entry_date"] = entry["date"] if entry is not None else pd.NaT
        row_dict["entry_price"] = entry["close"] if entry is not None else pd.NA
        row_dict["current_date"] = current["date"] if current is not None else pd.NaT
        row_dict["current_price"] = current["close"] if current is not None else pd.NA
        rows.append(row_dict)
    return pd.DataFrame(rows)


def compute_recommended_stop_price(row: pd.Series) -> float | None:
    current_price = pd.to_numeric(row.get("current_price"), errors="coerce")
    stop_price = pd.to_numeric(row.get("stop_price"), errors="coerce")
    atr_20 = pd.to_numeric(row.get("atr_20"), errors="coerce")
    dma_20 = pd.to_numeric(row.get("dma_20"), errors="coerce")

    candidates: list[float] = []
    if pd.notna(stop_price):
        candidates.append(float(stop_price))
    if pd.notna(dma_20):
        candidates.append(float(dma_20))
    if pd.notna(current_price) and pd.notna(atr_20):
        candidates.append(float(current_price - (1.25 * atr_20)))
    if not candidates:
        return None
    recommended = max(candidates)
    if pd.notna(current_price):
        recommended = min(recommended, float(current_price))
    return round(recommended, 2)


def default_horizon_days(row: pd.Series) -> int:
    explicit = pd.to_numeric(row.get("expected_horizon_days"), errors="coerce")
    if pd.notna(explicit) and int(explicit) > 0:
        return int(explicit)
    thesis_bucket = str(row.get("thesis_bucket") or "").upper()
    setup_id = str(row.get("setup_id") or "").upper()
    if thesis_bucket == "TARGET":
        return DEFAULT_TARGET_BUCKET_HORIZON_DAYS
    if thesis_bucket == "TIME_HORIZON" or "EVENT" in setup_id or "INTRADAY" in setup_id:
        return DEFAULT_TIME_BUCKET_HORIZON_DAYS
    return DEFAULT_DATA_BUCKET_HORIZON_DAYS


def risk_multiple_for_row(row: pd.Series) -> float:
    thesis_bucket = str(row.get("thesis_bucket") or "").upper()
    if thesis_bucket == "TARGET":
        return DEFAULT_TARGET_R_MULTIPLE
    if thesis_bucket == "TIME_HORIZON":
        return DEFAULT_TIME_R_MULTIPLE
    return DEFAULT_DATA_R_MULTIPLE


def compute_recommended_target_price(row: pd.Series) -> float | None:
    target_price = pd.to_numeric(row.get("target_price"), errors="coerce")
    if pd.notna(target_price) and float(target_price) > 0:
        return round(float(target_price), 2)

    entry_price = pd.to_numeric(row.get("entry_price"), errors="coerce")
    current_price = pd.to_numeric(row.get("current_price"), errors="coerce")
    stop_price = pd.to_numeric(row.get("stop_price"), errors="coerce")
    recommended_stop = compute_recommended_stop_price(row)
    atr_20 = pd.to_numeric(row.get("atr_20"), errors="coerce")
    if pd.isna(entry_price) or float(entry_price) <= 0:
        return None

    stop_anchor = recommended_stop if recommended_stop is not None else (float(stop_price) if pd.notna(stop_price) else None)
    if stop_anchor is not None and stop_anchor < float(entry_price):
        risk_per_share = float(entry_price) - float(stop_anchor)
    elif pd.notna(atr_20) and float(atr_20) > 0:
        risk_per_share = float(atr_20) * 1.5
    else:
        risk_per_share = float(entry_price) * 0.08

    multiple = risk_multiple_for_row(row)
    target = float(entry_price) + (risk_per_share * multiple)
    if pd.notna(current_price) and float(current_price) > target:
        target = float(current_price)
    return round(target, 2)


def enrich_management_policy(row: pd.Series, monitor_date: pd.Timestamp) -> pd.Series:
    out = row.copy()
    entry_date = pd.to_datetime(out.get("entry_date"), utc=True, errors="coerce")
    horizon_days = default_horizon_days(out)
    target_price = compute_recommended_target_price(out)
    recommended_stop = compute_recommended_stop_price(out)

    if pd.isna(out.get("expected_horizon_days")):
        out["expected_horizon_days"] = horizon_days
    if pd.isna(pd.to_datetime(out.get("horizon_end_date"), utc=True, errors="coerce")) and pd.notna(entry_date):
        out["horizon_end_date"] = entry_date.normalize() + pd.Timedelta(days=horizon_days)
    if pd.isna(pd.to_datetime(out.get("target_review_date"), utc=True, errors="coerce")) and pd.notna(entry_date):
        out["target_review_date"] = entry_date.normalize() + pd.Timedelta(days=max(1, int(horizon_days * 0.75)))
    if target_price is not None and pd.isna(pd.to_numeric(out.get("target_price"), errors="coerce")):
        out["target_price"] = target_price
        out["target_basis"] = f"{risk_multiple_for_row(out):.2f}R target from entry risk."
    if recommended_stop is not None:
        out["recommended_stop_price"] = recommended_stop
    out["recommended_target_price"] = target_price
    out["management_policy_json"] = json.dumps(
        {
            "target_price": target_price,
            "recommended_stop_price": recommended_stop,
            "expected_horizon_days": horizon_days,
            "horizon_end_date": None if pd.isna(pd.to_datetime(out.get("horizon_end_date"), utc=True, errors="coerce")) else pd.to_datetime(out.get("horizon_end_date"), utc=True, errors="coerce").isoformat(),
            "target_review_date": None if pd.isna(pd.to_datetime(out.get("target_review_date"), utc=True, errors="coerce")) else pd.to_datetime(out.get("target_review_date"), utc=True, errors="coerce").isoformat(),
            "basis": out.get("target_basis"),
        },
        ensure_ascii=False,
        default=str,
        sort_keys=True,
    )
    return out


def compute_action_fraction(row: pd.Series, next_action: str) -> float | None:
    action = str(next_action or "").lower()
    if not action:
        return None

    thesis_bucket = str(row.get("thesis_bucket") or "").upper()
    setup_id = str(row.get("setup_id") or "").upper()
    pnl_pct = pd.to_numeric(row.get("pnl_pct"), errors="coerce")

    if action == "trim_winner":
        fraction = 0.33
        if thesis_bucket == "TIME_HORIZON" or "EVENT" in setup_id:
            fraction = 0.50
        elif thesis_bucket == "DATA_DEPENDENT":
            fraction = 0.25
        if pd.notna(pnl_pct) and float(pnl_pct) >= 25.0:
            fraction = max(fraction, 0.50)
        elif pd.notna(pnl_pct) and float(pnl_pct) >= 18.0:
            fraction = max(fraction, 0.40)
        return round(min(max(fraction, 0.10), 0.75), 2)

    if action == "add_on_pullback":
        fraction = 0.20
        if thesis_bucket == "DATA_DEPENDENT":
            fraction = 0.25
        elif thesis_bucket == "TIME_HORIZON" or "EVENT" in setup_id:
            fraction = 0.15
        if pd.notna(pnl_pct) and float(pnl_pct) < 2.0:
            fraction = min(fraction + 0.05, 0.30)
        return round(min(max(fraction, 0.10), 0.33), 2)

    if action in {"exit_invalidation", "exit_stop", "exit_emergency", "exit_technical_failure", "exit_time_stop"}:
        return 1.0
    return None


def apply_tightened_stop_baseline(actions_df: pd.DataFrame) -> int:
    if actions_df.empty:
        return 0
    required_columns = {"published_on", "setup_id", "symbol", "unique_id", "suggested_action", "recommended_stop_price", "stop_price"}
    if not required_columns.issubset(actions_df.columns):
        return 0

    tighten_rows = actions_df.copy()
    tighten_rows["suggested_action"] = tighten_rows["suggested_action"].astype("string")
    tighten_rows = tighten_rows[tighten_rows["suggested_action"].str.lower() == "tighten_stop"].copy()
    if tighten_rows.empty:
        return 0

    tighten_rows["recommended_stop_price"] = pd.to_numeric(tighten_rows["recommended_stop_price"], errors="coerce")
    tighten_rows["stop_price"] = pd.to_numeric(tighten_rows["stop_price"], errors="coerce")
    tighten_rows["published_on"] = pd.to_datetime(tighten_rows["published_on"], utc=True, errors="coerce")
    tighten_rows["symbol"] = tighten_rows["symbol"].astype("string").str.upper()
    tighten_rows["setup_id"] = tighten_rows["setup_id"].astype("string")
    tighten_rows["unique_id"] = tighten_rows["unique_id"].astype("string")
    tighten_rows = tighten_rows[
        tighten_rows["published_on"].notna()
        & tighten_rows["recommended_stop_price"].notna()
        & (
            tighten_rows["stop_price"].isna()
            | (tighten_rows["recommended_stop_price"] > tighten_rows["stop_price"])
        )
    ].copy()
    if tighten_rows.empty:
        return 0

    updated = 0
    with db_session() as (_, cur):
        for _, row in tighten_rows.iterrows():
            cur.execute(
                f"""
                UPDATE {PORTFOLIO_TABLE}
                SET stop_price = %s
                WHERE published_on = %s
                  AND setup_id = %s
                  AND symbol = %s
                  AND unique_id = %s
                  AND (
                      stop_price IS NULL
                      OR %s > stop_price
                  )
                """,
                (
                    float(row["recommended_stop_price"]),
                    pd.to_datetime(row["published_on"], utc=True, errors="coerce").to_pydatetime(),
                    str(row["setup_id"]),
                    str(row["symbol"]).upper(),
                    str(row["unique_id"]),
                    float(row["recommended_stop_price"]),
                ),
            )
            updated += int(cur.rowcount or 0)
    return updated


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
    current_date = pd.to_datetime(row.get("current_date"), utc=True, errors="coerce")
    horizon_end_date = pd.to_datetime(row.get("horizon_end_date"), utc=True, errors="coerce")
    target_review_date = pd.to_datetime(row.get("target_review_date"), utc=True, errors="coerce")
    thesis_bucket = str(row.get("thesis_bucket") or "").upper()
    target_price = pd.to_numeric(row.get("recommended_target_price"), errors="coerce")
    if pd.isna(target_price):
        target_price = pd.to_numeric(row.get("target_price"), errors="coerce")

    if has_post_entry_technical_context(row):
        technical_eval = evaluate_technical_post_entry_state(row)
        technical_state = str(technical_eval.get("technical_state") or "").upper()
        technical_reasons = [str(value) for value in (technical_eval.get("technical_reasons") or []) if str(value).strip()]
        technical_reason_text = ", ".join(technical_reasons) if technical_reasons else None
    else:
        technical_state = "HOLD"
        technical_reason_text = None

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
    if technical_state == "EMERGENCY_EXIT":
        return (
            "exit_review",
            "Technical emergency exit fired.",
            "exit_emergency",
            f"Technical engine flagged EMERGENCY_EXIT{': ' + technical_reason_text if technical_reason_text else ''}.",
        )
    if technical_state == "FULL_EXIT":
        return (
            "exit_review",
            "Technical thesis failure detected.",
            "exit_technical_failure",
            f"Technical engine flagged FULL_EXIT{': ' + technical_reason_text if technical_reason_text else ''}.",
        )
    if technical_state == "PARTIAL_EXIT":
        return (
            "open",
            "Position is extended or showing distribution.",
            "trim_winner",
            f"Technical engine flagged PARTIAL_EXIT{': ' + technical_reason_text if technical_reason_text else ''}.",
        )
    if technical_state == "ADD_ON_PULLBACK":
        return (
            "open",
            "Constructive pullback is holding.",
            "add_on_pullback",
            f"Technical engine flagged ADD_ON_PULLBACK{': ' + technical_reason_text if technical_reason_text else ''}.",
        )
    if pd.notna(target_price) and current_price >= target_price:
        return (
            "open",
            "Target zone reached.",
            "trim_winner",
            f"Current price reached target zone {float(target_price):.2f}; book partial profit and trail the remainder.",
        )
    if thesis_bucket == "TIME_HORIZON" and pd.notna(current_date) and pd.notna(horizon_end_date) and current_date.normalize() >= horizon_end_date.normalize():
        return (
            "review",
            "Holding window ended.",
            "exit_time_stop" if pd.notna(pnl_pct) and float(pnl_pct) <= 0 else "review_horizon",
            "Time-horizon thesis reached its planned window; exit if there is no positive follow-through.",
        )
    if thesis_bucket == "TARGET" and pd.notna(current_date) and pd.notna(target_review_date) and current_date.normalize() >= target_review_date.normalize():
        return (
            "review",
            "Target review date reached.",
            "review_target",
            "Target bucket reached its scheduled review date.",
        )
    if pd.notna(pnl_pct) and pnl_pct >= trim_winner_gain_pct:
        return ("open", "Position is a strong winner.", "trim_winner", "Unrealized gain exceeded trim threshold.")
    if pd.notna(pnl_pct) and pnl_pct >= tighten_stop_gain_pct:
        return ("open", "Position is in profit and may warrant tighter risk.", "tighten_stop", "Unrealized gain exceeded stop-tightening threshold.")
    if thesis_bucket != "TIME_HORIZON" and days_held >= review_stale_days and (pd.isna(pnl_pct) or pnl_pct <= 0):
        return ("review", "Position is stale without positive mark-to-market.", "review_stale", "Holding period exceeded stale threshold without gains.")
    return ("open", "Position remains within expected risk bounds.", "hold", "No lifecycle trigger fired.")


def derive_bucket_lifecycle_fields(row: pd.Series, monitor_date: pd.Timestamp, next_action: str) -> tuple[str | None, str, str]:
    thesis_bucket = str(row.get("thesis_bucket") or "").upper()
    target_review_date = pd.to_datetime(row.get("target_review_date"), utc=True, errors="coerce")
    horizon_end_date = pd.to_datetime(row.get("horizon_end_date"), utc=True, errors="coerce")
    current_price = pd.to_numeric(row.get("current_price"), errors="coerce")
    target_price = pd.to_numeric(row.get("recommended_target_price"), errors="coerce")
    if pd.isna(target_price):
        target_price = pd.to_numeric(row.get("target_price"), errors="coerce")

    if next_action in {"exit_invalidation", "exit_stop", "exit_time_stop"}:
        active = {"exit_invalidation": "INVALIDATION_HIT", "exit_stop": "STOP_HIT", "exit_time_stop": "TIME_STOP"}.get(next_action)
        return active, "triggered", "Exit event overrides target and horizon policy."
    if next_action == "trim_winner" and pd.notna(target_price) and pd.notna(current_price) and float(current_price) >= float(target_price):
        return "TARGET_REACHED", "partial_exit_due", "Target zone has been reached; book partial profit and trail the rest."
    if thesis_bucket == "TIME_HORIZON" and pd.notna(horizon_end_date):
        if monitor_date >= horizon_end_date.normalize():
            return "HORIZON_REVIEW", "review_due", "The planned holding window has ended and needs review."
        return None, "within_window", "Still inside the intended holding window."
    if thesis_bucket == "TARGET" and pd.notna(target_review_date):
        if monitor_date >= target_review_date.normalize():
            return "TARGET_REVIEW", "review_due", "The target review date has been reached."
        return None, "awaiting_target_review", "Target bucket remains active until review date or exit event."
    if thesis_bucket == "DATA_DEPENDENT":
        return None, "evidence_active", "Continue while evidence remains supportive and no exit event fires."
    return None, "active", "Bucket remains active."


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
    technical_context = load_latest_technical_context(orders["symbol"].astype(str).unique().tolist(), monitor_date)
    if not technical_context.empty:
        derived = derived.merge(technical_context, how="left", on="symbol", suffixes=("", "_tech"))
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
        enriched = enrich_management_policy(enriched, monitor_date)
        position_status, lifecycle_reason, next_action, action_reason = classify_position(
            enriched,
            review_stale_days=review_stale_days,
            tighten_stop_gain_pct=tighten_stop_gain_pct,
            trim_winner_gain_pct=trim_winner_gain_pct,
        )

        recommended_stop_price = pd.to_numeric(enriched.get("recommended_stop_price"), errors="coerce")
        recommended_stop_price = None if pd.isna(recommended_stop_price) else float(recommended_stop_price)
        recommended_target_price = pd.to_numeric(enriched.get("recommended_target_price"), errors="coerce")
        recommended_target_price = None if pd.isna(recommended_target_price) else float(recommended_target_price)
        price_status = "priced"
        if pd.isna(entry_price) or pd.isna(current_price):
            price_status = "missing_entry_or_current_price"
        entry_assumption = (
            "Paper entry uses the first available close on or after portfolio published_on."
            if pd.notna(entry_date)
            else "No paper entry assumed because no close exists on or after portfolio published_on."
        )
        current_stop_price = pd.to_numeric(row.get("stop_price"), errors="coerce")
        stop_status = "stop_available" if pd.notna(current_stop_price) else "stop_missing"
        if recommended_stop_price is not None and pd.notna(current_stop_price) and recommended_stop_price > float(current_stop_price):
            stop_status = "stop_tightening_available"
        target_status = "target_available" if recommended_target_price is not None else "target_missing"
        operator_question = None
        if next_action in {"review_manual", "review_stale", "review_horizon", "review_target"}:
            operator_question = action_reason

        context = {
            "published_on": str(row.get("published_on")),
            "entry_date": None if pd.isna(entry_date) else entry_date.isoformat(),
            "current_date": None if pd.isna(current_date) else current_date.isoformat(),
            "entry_price": None if pd.isna(entry_price) else float(entry_price),
            "current_price": None if pd.isna(current_price) else float(current_price),
            "price_status": price_status,
            "entry_assumption": entry_assumption,
            "pnl_pct": pnl_pct,
            "days_held": days_held,
            "portfolio_reason": row.get("portfolio_reason"),
            "overlap_group": row.get("overlap_group"),
            "target_price": enriched.get("recommended_target_price"),
            "recommended_stop_price": enriched.get("recommended_stop_price"),
            "stop_status": stop_status,
            "target_status": target_status,
            "expected_horizon_days": enriched.get("expected_horizon_days"),
            "horizon_end_date": enriched.get("horizon_end_date"),
            "target_review_date": enriched.get("target_review_date"),
            "operator_question": operator_question,
        }
        action_fraction = compute_action_fraction(enriched, next_action)
        execution_mode = "review_only"
        if next_action == "trim_winner":
            execution_mode = "broker_order"
        elif next_action == "add_on_pullback":
            execution_mode = "broker_order"
        elif next_action in {"exit_invalidation", "exit_stop", "exit_emergency", "exit_technical_failure", "exit_time_stop"}:
            execution_mode = "broker_order"
        elif next_action == "tighten_stop":
            execution_mode = "review_only"
            current_stop_price = pd.to_numeric(row.get("stop_price"), errors="coerce")
            if pd.notna(recommended_stop_price) and (pd.isna(current_stop_price) or float(recommended_stop_price) > float(current_stop_price)):
                row["stop_price"] = float(recommended_stop_price)
                enriched["stop_price"] = float(recommended_stop_price)
                action_reason = (
                    f"{action_reason.rstrip('.')} Updated stop baseline to {float(recommended_stop_price):.2f}."
                )

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
                "thesis_bucket": row.get("thesis_bucket"),
                "bucket_reason": row.get("bucket_reason"),
                "target_price": recommended_target_price,
                "target_basis": enriched.get("target_basis"),
                "target_confidence": pd.to_numeric(enriched.get("target_confidence"), errors="coerce"),
                "expected_horizon_days": pd.to_numeric(enriched.get("expected_horizon_days"), errors="coerce"),
                "target_review_date": pd.to_datetime(enriched.get("target_review_date"), utc=True, errors="coerce"),
                "horizon_end_date": pd.to_datetime(enriched.get("horizon_end_date"), utc=True, errors="coerce"),
                "exit_event_rules_json": row.get("exit_event_rules_json"),
                "next_action": next_action,
                "next_action_reason": action_reason,
                "context_snapshot_json": json.dumps(context, ensure_ascii=False, default=str, sort_keys=True),
                "load_ts": pd.Timestamp.utcnow(),
            }
        )
        active_exit_condition, exit_condition_status, bucket_status_note = derive_bucket_lifecycle_fields(enriched, monitor_date, next_action)
        rows[-1]["active_exit_condition"] = active_exit_condition
        rows[-1]["exit_condition_status"] = exit_condition_status
        rows[-1]["bucket_status_note"] = bucket_status_note

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
                    "target_price": pd.to_numeric(enriched.get("target_price"), errors="coerce"),
                    "recommended_stop_price": recommended_stop_price,
                    "recommended_target_price": recommended_target_price,
                    "expected_horizon_days": pd.to_numeric(enriched.get("expected_horizon_days"), errors="coerce"),
                    "action_fraction": action_fraction,
                    "execution_mode": execution_mode,
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
            "target_price",
            "target_confidence",
        ]:
            if column in lifecycle_out.columns:
                lifecycle_out[column] = pd.to_numeric(lifecycle_out[column], errors="coerce")
        if "days_held" in lifecycle_out.columns:
            lifecycle_out["days_held"] = pd.to_numeric(lifecycle_out["days_held"], errors="coerce").astype("Int64")
        if "expected_horizon_days" in lifecycle_out.columns:
            lifecycle_out["expected_horizon_days"] = pd.to_numeric(lifecycle_out["expected_horizon_days"], errors="coerce").astype("Int64")
        for column in [
            "asof_date",
            "published_on",
            "entry_date",
            "target_review_date",
            "horizon_end_date",
            "load_ts",
        ]:
            if column in lifecycle_out.columns:
                lifecycle_out[column] = pd.to_datetime(lifecycle_out[column], utc=True, errors="coerce")
        with db_session() as (_, cur):
            pairs = (
                lifecycle_out[["asof_date", "symbol"]]
                .dropna()
                .drop_duplicates()
                .to_dict(orient="records")
            )
            for item in pairs:
                cur.execute(
                    f"DELETE FROM {LIFECYCLE_TABLE} WHERE asof_date = %s AND symbol = %s",
                    (
                        pd.to_datetime(item["asof_date"], utc=True, errors="coerce").to_pydatetime(),
                        str(item["symbol"]).upper(),
                    ),
                )
        upsert_to_db(
            lifecycle_out,
            LIFECYCLE_TABLE,
            unique_keys=["asof_date", "published_on", "setup_id", "symbol", "unique_id"],
            timescaledb_column="asof_date",
        )
        _trace_lifecycle_rows(lifecycle_out)
    if not actions_df.empty:
        actions_out = actions_df.copy()
        for column in [
            "reference_price",
            "stop_price",
            "invalidation_price",
            "target_price",
            "recommended_stop_price",
            "recommended_target_price",
            "action_fraction",
        ]:
            if column in actions_out.columns:
                actions_out[column] = pd.to_numeric(actions_out[column], errors="coerce")
        if "expected_horizon_days" in actions_out.columns:
            actions_out["expected_horizon_days"] = pd.to_numeric(actions_out["expected_horizon_days"], errors="coerce").astype("Int64")
        for column in ["asof_date", "published_on", "load_ts"]:
            if column in actions_out.columns:
                actions_out[column] = pd.to_datetime(actions_out[column], utc=True, errors="coerce")
        with db_session() as (_, cur):
            pairs = (
                actions_out[["asof_date", "symbol"]]
                .dropna()
                .drop_duplicates()
                .to_dict(orient="records")
            )
            for item in pairs:
                cur.execute(
                    f"DELETE FROM {REBALANCE_TABLE} WHERE asof_date = %s AND symbol = %s",
                    (
                        pd.to_datetime(item["asof_date"], utc=True, errors="coerce").to_pydatetime(),
                        str(item["symbol"]).upper(),
                    ),
                )
        upsert_to_db(
            actions_out,
            REBALANCE_TABLE,
            unique_keys=["asof_date", "published_on", "setup_id", "symbol", "unique_id", "suggested_action"],
            timescaledb_column="asof_date",
        )
        _trace_rebalance_rows(actions_out)
        apply_tightened_stop_baseline(actions_out)


def _trace_lifecycle_rows(df: pd.DataFrame) -> None:
    for _, row in df.iterrows():
        trace_id = safe_trace_call(
            append_trace,
            asof_date=row.get("asof_date"),
            symbol=row.get("symbol"),
            unique_id=row.get("unique_id"),
            setup_id=row.get("setup_id"),
            trigger_type="lifecycle",
            final_action=row.get("next_action"),
            final_reason=row.get("next_action_reason") or row.get("lifecycle_reason"),
            source_table=LIFECYCLE_TABLE,
            source_key=f"{row.get('asof_date')}:{row.get('symbol')}:{row.get('unique_id')}",
            payload={
                "position_status": row.get("position_status"),
                "pnl_pct": row.get("pnl_pct"),
                "days_held": row.get("days_held"),
                "active_exit_condition": row.get("active_exit_condition"),
                "exit_condition_status": row.get("exit_condition_status"),
            },
        )
        if trace_id:
            safe_trace_call(
                append_trace_step,
                trace_id=trace_id,
                step_idx=40,
                stage="lifecycle",
                status="completed",
                reason=row.get("next_action_reason") or row.get("lifecycle_reason"),
                input_payload=row.get("context_snapshot_json"),
                output_payload=row.to_dict(),
                payload={
                    "position_status": row.get("position_status"),
                    "next_action": row.get("next_action"),
                    "pnl_pct": row.get("pnl_pct"),
                    "days_held": row.get("days_held"),
                    "bucket_status_note": row.get("bucket_status_note"),
                },
            )


def _trace_rebalance_rows(df: pd.DataFrame) -> None:
    for _, row in df.iterrows():
        trace_id = safe_trace_call(
            append_trace,
            asof_date=row.get("asof_date"),
            symbol=row.get("symbol"),
            unique_id=row.get("unique_id"),
            setup_id=row.get("setup_id"),
            trigger_type="rebalance",
            final_action=row.get("suggested_action"),
            final_reason=row.get("action_reason"),
            source_table=REBALANCE_TABLE,
            source_key=f"{row.get('asof_date')}:{row.get('symbol')}:{row.get('unique_id')}:{row.get('suggested_action')}",
            payload={
                "execution_mode": row.get("execution_mode"),
                "action_fraction": row.get("action_fraction"),
                "reference_price": row.get("reference_price"),
                "recommended_stop_price": row.get("recommended_stop_price"),
                "recommended_target_price": row.get("recommended_target_price"),
            },
        )
        if trace_id:
            safe_trace_call(
                append_trace_step,
                trace_id=trace_id,
                step_idx=45,
                stage="rebalance_action",
                status="completed",
                reason=row.get("action_reason"),
                input_payload=row.get("context_snapshot_json"),
                output_payload=row.to_dict(),
                payload={
                    "suggested_action": row.get("suggested_action"),
                    "execution_mode": row.get("execution_mode"),
                    "action_fraction": row.get("action_fraction"),
                },
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
        "sample": to_display_value(lifecycle_df.head(10)),
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
    print(json.dumps(to_display_value(result), indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
