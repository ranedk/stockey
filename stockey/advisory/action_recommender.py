from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from advisory.portfolio_engine import PORTFOLIO_TABLE
from advisory.position_lifecycle import LIFECYCLE_TABLE, REBALANCE_TABLE
from advisory.watchlist_builder import TABLE_NAME as WATCHLIST_TABLE
from utils.db import db_session, sql_to_df, upsert_to_db
from utils.display_time import to_display_value
from utils.sync import parse_datetime_arg


TABLE_NAME = "advisory_action_recommendations"

ACTION_PRIORITY = {
    "SELL": 100,
    "PARTIAL_SELL": 90,
    "MANUAL_REVIEW": 80,
    "TIGHTEN_STOP": 70,
    "BUY_MORE": 60,
    "BUY": 50,
    "HOLD": 20,
    "WATCH": 10,
}


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


def ensure_actions_table() -> None:
    with db_session() as (_, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                asof_date TIMESTAMPTZ NOT NULL,
                published_on TIMESTAMPTZ,
                symbol TEXT NOT NULL,
                setup_id TEXT,
                unique_id TEXT,
                action_code TEXT NOT NULL,
                action_priority BIGINT,
                action_source TEXT,
                source_action TEXT,
                transaction_type TEXT,
                execution_mode TEXT,
                action_fraction DOUBLE PRECISION,
                approved_allocation_inr DOUBLE PRECISION,
                reference_price DOUBLE PRECISION,
                stop_price DOUBLE PRECISION,
                invalidation_price DOUBLE PRECISION,
                recommended_stop_price DOUBLE PRECISION,
                invest_score_pct DOUBLE PRECISION,
                action_reason TEXT,
                action_detail TEXT,
                raw_context_json TEXT,
                load_ts TIMESTAMPTZ,
                UNIQUE (asof_date, symbol)
            )
            """
        )


def _normalize_asof_date(asof_date: pd.Timestamp | None) -> pd.Timestamp:
    ts = pd.to_datetime(asof_date or pd.Timestamp.utcnow(), utc=True, errors="coerce")
    return ts.normalize()


def _coerce_ts(value: Any) -> pd.Timestamp | None:
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    return None if pd.isna(ts) else ts


def _text(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _num(value: Any) -> float | None:
    out = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(out) else float(out)


def _build_record(
    *,
    asof_date: pd.Timestamp,
    symbol: Any,
    setup_id: Any = None,
    unique_id: Any = None,
    published_on: Any = None,
    action_code: str,
    action_source: str,
    source_action: Any = None,
    transaction_type: Any = None,
    execution_mode: Any = None,
    action_fraction: Any = None,
    approved_allocation_inr: Any = None,
    reference_price: Any = None,
    stop_price: Any = None,
    invalidation_price: Any = None,
    recommended_stop_price: Any = None,
    invest_score_pct: Any = None,
    action_reason: Any = None,
    action_detail: Any = None,
    raw_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_action = str(action_code).upper()
    return {
        "asof_date": asof_date,
        "published_on": _coerce_ts(published_on),
        "symbol": str(symbol).upper(),
        "setup_id": _text(setup_id),
        "unique_id": _text(unique_id),
        "action_code": normalized_action,
        "action_priority": int(ACTION_PRIORITY.get(normalized_action, 0)),
        "action_source": action_source,
        "source_action": _text(source_action),
        "transaction_type": _text(transaction_type),
        "execution_mode": _text(execution_mode),
        "action_fraction": _num(action_fraction),
        "approved_allocation_inr": _num(approved_allocation_inr),
        "reference_price": _num(reference_price),
        "stop_price": _num(stop_price),
        "invalidation_price": _num(invalidation_price),
        "recommended_stop_price": _num(recommended_stop_price),
        "invest_score_pct": _num(invest_score_pct),
        "action_reason": _text(action_reason),
        "action_detail": _text(action_detail),
        "raw_context_json": json.dumps(raw_context or {}, ensure_ascii=False, default=str, sort_keys=True),
        "load_ts": pd.Timestamp.utcnow(),
    }


def load_portfolio_actions(*, asof_date: pd.Timestamp, symbols: list[str] | None = None, setup_ids: list[str] | None = None) -> pd.DataFrame:
    if not table_exists(PORTFOLIO_TABLE):
        return pd.DataFrame()
    clauses = ["asof_date = %s", "portfolio_status IN ('approved', 'trimmed')"]
    params: list[object] = [asof_date]
    if symbols:
        clauses.append("symbol = ANY(%s)")
        params.append([str(value).upper() for value in symbols])
    if setup_ids:
        clauses.append("setup_id = ANY(%s)")
        params.append([str(value).upper() for value in setup_ids])
    return sql_to_df(
        f"""
        SELECT
            asof_date,
            published_on,
            setup_id,
            symbol,
            unique_id,
            portfolio_status,
            portfolio_reason,
            approved_allocation_inr,
            stop_price,
            invalidation_price,
            invest_score_pct,
            execution_notes
        FROM {PORTFOLIO_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY published_on DESC, symbol
        """,
        params=tuple(params),
    )


def load_lifecycle_actions(*, asof_date: pd.Timestamp, symbols: list[str] | None = None, setup_ids: list[str] | None = None) -> pd.DataFrame:
    if not table_exists(LIFECYCLE_TABLE):
        return pd.DataFrame()
    clauses = ["asof_date = %s"]
    params: list[object] = [asof_date]
    if symbols:
        clauses.append("symbol = ANY(%s)")
        params.append([str(value).upper() for value in symbols])
    if setup_ids:
        clauses.append("setup_id = ANY(%s)")
        params.append([str(value).upper() for value in setup_ids])
    return sql_to_df(
        f"""
        SELECT
            asof_date,
            published_on,
            setup_id,
            symbol,
            unique_id,
            position_status,
            lifecycle_reason,
            next_action,
            next_action_reason,
            current_price,
            stop_price,
            invalidation_price
        FROM {LIFECYCLE_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY published_on DESC, symbol
        """,
        params=tuple(params),
    )


def load_rebalance_actions(*, asof_date: pd.Timestamp, symbols: list[str] | None = None, setup_ids: list[str] | None = None) -> pd.DataFrame:
    if not table_exists(REBALANCE_TABLE):
        return pd.DataFrame()
    clauses = ["asof_date = %s"]
    params: list[object] = [asof_date]
    if symbols:
        clauses.append("symbol = ANY(%s)")
        params.append([str(value).upper() for value in symbols])
    if setup_ids:
        clauses.append("setup_id = ANY(%s)")
        params.append([str(value).upper() for value in setup_ids])
    return sql_to_df(
        f"""
        SELECT
            asof_date,
            published_on,
            setup_id,
            symbol,
            unique_id,
            suggested_action,
            action_reason,
            reference_price,
            stop_price,
            invalidation_price,
            recommended_stop_price,
            action_fraction,
            execution_mode
        FROM {REBALANCE_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY published_on DESC, symbol
        """,
        params=tuple(params),
    )


def load_watch_actions(*, asof_date: pd.Timestamp, symbols: list[str] | None = None, setup_ids: list[str] | None = None) -> pd.DataFrame:
    if not table_exists(WATCHLIST_TABLE):
        return pd.DataFrame()
    clauses = ["asof_date = %s", "COALESCE(current_state, watch_status, '') NOT ILIKE 'abstain%%'"]
    params: list[object] = [asof_date]
    if symbols:
        clauses.append("symbol = ANY(%s)")
        params.append([str(value).upper() for value in symbols])
    if setup_ids:
        clauses.append("setup_id = ANY(%s)")
        params.append([str(value).upper() for value in setup_ids])
    return sql_to_df(
        f"""
        SELECT
            asof_date,
            state_updated_at AS published_on,
            setup_id,
            symbol,
            watch_status,
            current_state,
            candidate_state,
            watch_reason_detail,
            attractive_price_low,
            invalidation_price
        FROM {WATCHLIST_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY state_updated_at DESC, symbol
        """,
        params=tuple(params),
    )


def build_action_recommendations(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
) -> pd.DataFrame:
    monitor_date = _normalize_asof_date(asof_date)
    candidates: list[dict[str, Any]] = []

    portfolio_df = load_portfolio_actions(asof_date=monitor_date, symbols=symbols, setup_ids=setup_ids)
    for _, row in portfolio_df.iterrows():
        candidates.append(
            _build_record(
                asof_date=monitor_date,
                published_on=row.get("published_on"),
                setup_id=row.get("setup_id"),
                symbol=row.get("symbol"),
                unique_id=row.get("unique_id"),
                action_code="BUY",
                action_source="portfolio",
                source_action=row.get("portfolio_status"),
                transaction_type="BUY",
                execution_mode="broker_order",
                approved_allocation_inr=row.get("approved_allocation_inr"),
                stop_price=row.get("stop_price"),
                invalidation_price=row.get("invalidation_price"),
                invest_score_pct=row.get("invest_score_pct"),
                action_reason=row.get("portfolio_reason"),
                action_detail=row.get("execution_notes"),
                raw_context=row.to_dict(),
            )
        )

    lifecycle_df = load_lifecycle_actions(asof_date=monitor_date, symbols=symbols, setup_ids=setup_ids)
    for _, row in lifecycle_df.iterrows():
        if str(row.get("position_status") or "").lower() != "open":
            continue
        candidates.append(
            _build_record(
                asof_date=monitor_date,
                published_on=row.get("published_on"),
                setup_id=row.get("setup_id"),
                symbol=row.get("symbol"),
                unique_id=row.get("unique_id"),
                action_code="HOLD",
                action_source="lifecycle",
                source_action=row.get("next_action"),
                transaction_type=None,
                execution_mode="review_only",
                reference_price=row.get("current_price"),
                stop_price=row.get("stop_price"),
                invalidation_price=row.get("invalidation_price"),
                action_reason=row.get("lifecycle_reason"),
                action_detail=row.get("next_action_reason"),
                raw_context=row.to_dict(),
            )
        )

    rebalance_df = load_rebalance_actions(asof_date=monitor_date, symbols=symbols, setup_ids=setup_ids)
    action_map = {
        "exit_invalidation": ("SELL", "SELL"),
        "exit_stop": ("SELL", "SELL"),
        "exit_emergency": ("SELL", "SELL"),
        "exit_technical_failure": ("SELL", "SELL"),
        "trim_winner": ("PARTIAL_SELL", "SELL"),
        "add_on_pullback": ("BUY_MORE", "BUY"),
        "tighten_stop": ("TIGHTEN_STOP", None),
        "review_manual": ("MANUAL_REVIEW", None),
        "review_stale": ("MANUAL_REVIEW", None),
        "review_horizon": ("MANUAL_REVIEW", None),
        "review_target": ("MANUAL_REVIEW", None),
    }
    for _, row in rebalance_df.iterrows():
        source_action = str(row.get("suggested_action") or "").strip().lower()
        mapped = action_map.get(source_action, ("MANUAL_REVIEW", None))
        candidates.append(
            _build_record(
                asof_date=monitor_date,
                published_on=row.get("published_on"),
                setup_id=row.get("setup_id"),
                symbol=row.get("symbol"),
                unique_id=row.get("unique_id"),
                action_code=mapped[0],
                action_source="rebalance",
                source_action=row.get("suggested_action"),
                transaction_type=mapped[1],
                execution_mode=row.get("execution_mode") or ("broker_order" if mapped[1] else "review_only"),
                action_fraction=row.get("action_fraction"),
                reference_price=row.get("reference_price"),
                stop_price=row.get("stop_price"),
                invalidation_price=row.get("invalidation_price"),
                recommended_stop_price=row.get("recommended_stop_price"),
                action_reason=row.get("action_reason"),
                action_detail=row.get("suggested_action"),
                raw_context=row.to_dict(),
            )
        )

    watch_df = load_watch_actions(asof_date=monitor_date, symbols=symbols, setup_ids=setup_ids)
    for _, row in watch_df.iterrows():
        candidates.append(
            _build_record(
                asof_date=monitor_date,
                published_on=row.get("published_on"),
                setup_id=row.get("setup_id"),
                symbol=row.get("symbol"),
                unique_id=None,
                action_code="WATCH",
                action_source="watchlist",
                source_action=row.get("current_state") or row.get("watch_status"),
                transaction_type=None,
                execution_mode="review_only",
                reference_price=row.get("attractive_price_low"),
                invalidation_price=row.get("invalidation_price"),
                action_reason=row.get("watch_reason_detail"),
                action_detail=row.get("candidate_state"),
                raw_context=row.to_dict(),
            )
        )

    if not candidates:
        return pd.DataFrame()

    df = pd.DataFrame(candidates)
    df["symbol"] = df["symbol"].astype("string").str.upper()
    df["published_on"] = pd.to_datetime(df["published_on"], utc=True, errors="coerce")
    df["action_priority"] = pd.to_numeric(df["action_priority"], errors="coerce").fillna(0).astype(int)
    df["invest_score_pct"] = pd.to_numeric(df["invest_score_pct"], errors="coerce")
    df = df.sort_values(
        ["symbol", "action_priority", "published_on", "invest_score_pct", "setup_id"],
        ascending=[True, False, False, False, True],
        kind="stable",
    )
    return df.drop_duplicates(subset=["asof_date", "symbol"], keep="first").reset_index(drop=True)


def persist_action_recommendations(df: pd.DataFrame) -> None:
    ensure_actions_table()
    if df.empty:
        return
    out = df.copy()
    for column in [
        "action_fraction",
        "approved_allocation_inr",
        "reference_price",
        "stop_price",
        "invalidation_price",
        "recommended_stop_price",
        "invest_score_pct",
    ]:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce")
    for column in ["asof_date", "published_on", "load_ts"]:
        if column in out.columns:
            out[column] = pd.to_datetime(out[column], utc=True, errors="coerce")
    with db_session() as (_, cur):
        pairs = (
            out[["asof_date", "symbol"]]
            .dropna()
            .drop_duplicates()
            .to_dict(orient="records")
        )
        for item in pairs:
            cur.execute(
                f"DELETE FROM {TABLE_NAME} WHERE asof_date = %s AND symbol = %s",
                (
                    pd.to_datetime(item["asof_date"], utc=True, errors="coerce").to_pydatetime(),
                    str(item["symbol"]).upper(),
                ),
            )
    upsert_to_db(
        out,
        TABLE_NAME,
        unique_keys=["asof_date", "symbol"],
        timescaledb_column="asof_date",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build one consolidated action recommendation per symbol for operator and execution flows.")
    parser.add_argument("--date", type=parse_datetime_arg, help="Asof date in YYYY-MM-DD")
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument("--setup", dest="setup_ids", nargs="*")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    asof_date = pd.Timestamp(args.date, tz="UTC") if args.date else None
    df = build_action_recommendations(asof_date=asof_date, symbols=args.symbols, setup_ids=args.setup_ids)
    if not args.dry_run:
        persist_action_recommendations(df)
    result = {
        "status": "ok",
        "table": TABLE_NAME,
        "row_count": int(len(df)),
        "action_counts": df["action_code"].value_counts(dropna=False).to_dict() if not df.empty else {},
        "sample": to_display_value(df.head(10)),
        "dry_run": bool(args.dry_run),
    }
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
