from __future__ import annotations

import argparse
import json
import uuid
from typing import Any

import pandas as pd

from advisory.action_recommender import TABLE_NAME as ACTION_RECOMMENDATIONS_TABLE
from advisory.current_prices import load_current_prices
from utils.db import db_session, sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration


LEDGER_TABLE = "advisory_operator_portfolio_ledger"
BROKER_ACTION_TO_OPERATOR_ACTION = {
    "BUY": "buy",
    "BUY_MORE": "buy_50%",
    "SELL": "sell",
    "PARTIAL_SELL": "sell_50%",
}
OPERATOR_ACTION_TO_LEDGER_ACTION = {
    "buy": "buy",
    "buy_50": "buy_50",
    "buy_50%": "buy_50",
    "sell": "sell",
    "sell_50": "sell_50",
    "sell_50%": "sell_50",
}
SCHEMA_MIGRATION_ID = "20260616_advisory_operator_portfolio_ledger"
SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (
        event_id TEXT NOT NULL,
        event_at TIMESTAMPTZ NOT NULL,
        symbol TEXT NOT NULL,
        action TEXT NOT NULL,
        price DOUBLE PRECISION,
        recommendation_id TEXT,
        recommendation_action TEXT,
        source_json TEXT,
        operator_id TEXT,
        note TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (event_id)
    )
    """,
    f"CREATE INDEX IF NOT EXISTS idx_{LEDGER_TABLE}_symbol_event_at ON {LEDGER_TABLE} (symbol, event_at DESC)",
    f"CREATE INDEX IF NOT EXISTS idx_{LEDGER_TABLE}_event_at ON {LEDGER_TABLE} (event_at DESC)",
]


def ensure_operator_portfolio_tables() -> None:
    apply_schema_migration(
        migration_id=SCHEMA_MIGRATION_ID,
        description="Create operator-managed paper portfolio ledger.",
        statements=SCHEMA_STATEMENTS,
        metadata={"tables": [LEDGER_TABLE], "broker_execution": False},
    )


def _normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def _to_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if pd.notna(out) and out > 0 else None


def _recommendation_id(row: dict[str, Any]) -> str:
    parts = [
        row.get("asof_date"),
        row.get("published_on"),
        row.get("symbol"),
        row.get("setup_id"),
        row.get("unique_id"),
        row.get("action_code"),
    ]
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "|".join("" if part is None else str(part) for part in parts)))


def _latest_recommendations(limit: int = 100, symbol: str | None = None) -> list[dict[str, Any]]:
    ensure_operator_portfolio_tables()
    clauses = [
        "asof_date = (SELECT MAX(asof_date) FROM advisory_action_recommendations)",
        "UPPER(COALESCE(action_code, '')) IN ('BUY', 'BUY_MORE', 'SELL', 'PARTIAL_SELL')",
    ]
    params: list[Any] = []
    normalized_symbol = _normalize_symbol(symbol)
    if normalized_symbol:
        clauses.append("UPPER(TRIM(symbol)) = %s")
        params.append(normalized_symbol)
    rows = sql_to_df(
        f"""
        SELECT
            asof_date,
            published_on,
            UPPER(TRIM(symbol)) AS symbol,
            setup_id,
            unique_id,
            action_code,
            action_source,
            transaction_type,
            reference_price,
            stop_price,
            invalidation_price,
            recommended_stop_price,
            recommended_target_price,
            expected_horizon_days,
            invest_score_pct,
            action_reason,
            action_detail,
            reason_contract_status,
            action_fraction,
            raw_context_json,
            load_ts
        FROM {ACTION_RECOMMENDATIONS_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY published_on DESC NULLS LAST, action_priority DESC NULLS LAST, load_ts DESC NULLS LAST
        LIMIT %s
        """,
        params=tuple([*params, max(1, min(int(limit), 500))]),
        retries=2,
    )
    out = rows.to_dict(orient="records") if not rows.empty else []
    prices = load_current_prices([str(row.get("symbol") or "") for row in out], max_age_seconds=None)
    for row in out:
        symbol_value = _normalize_symbol(row.get("symbol"))
        price_payload = prices.get(symbol_value) or {}
        row["recommendation_id"] = _recommendation_id(row)
        row["current_price"] = row.get("current_price") or price_payload.get("price")
        row["current_price_asof"] = price_payload.get("price_asof")
        row["current_price_source"] = price_payload.get("price_source")
        action_code = str(row.get("action_code") or "").strip().upper()
        operator_action = BROKER_ACTION_TO_OPERATOR_ACTION.get(action_code)
        row["operator_recommended_action"] = operator_action
        row["operator_recommended_action_label"] = operator_action or "-"
        row["operator_ledger_action"] = OPERATOR_ACTION_TO_LEDGER_ACTION.get(str(operator_action or "").lower())
        row["operator_display_action"] = operator_action or action_code
        row["operator_portfolio_boundary"] = {
            "paper_portfolio_only": True,
            "mutates_advisory_recommendations": False,
            "mutates_broker_execution": False,
            "submits_order": False,
        }
    return out


def _load_ledger() -> list[dict[str, Any]]:
    ensure_operator_portfolio_tables()
    df = sql_to_df(
        f"""
        SELECT *
        FROM {LEDGER_TABLE}
        ORDER BY event_at ASC, load_ts ASC
        """,
        retries=2,
    )
    return df.to_dict(orient="records") if not df.empty else []


def build_positions() -> list[dict[str, Any]]:
    rows = _load_ledger()
    positions: dict[str, dict[str, Any]] = {}
    closed: list[dict[str, Any]] = []
    for row in rows:
        symbol = _normalize_symbol(row.get("symbol"))
        action = str(row.get("action") or "").strip().lower()
        price = _to_float(row.get("price"))
        if not symbol or action not in {"buy", "buy_50", "sell", "sell_50"}:
            continue
        if action in {"buy", "buy_50"}:
            if symbol in positions:
                positions[symbol]["last_add_price"] = price
                positions[symbol]["last_add_at"] = row.get("event_at")
                positions[symbol]["last_add_action"] = action
                positions[symbol]["add_count"] = int(positions[symbol].get("add_count") or 0) + 1
            else:
                positions[symbol] = {
                    "symbol": symbol,
                    "status": "open",
                    "entry_price": price,
                    "entry_at": row.get("event_at"),
                    "entry_event_id": row.get("event_id"),
                    "entry_recommendation_id": row.get("recommendation_id"),
                    "entry_recommendation_action": row.get("recommendation_action"),
                    "entry_operator_action": action,
                    "operator_id": row.get("operator_id"),
                    "note": row.get("note"),
                }
        elif action == "sell_50" and symbol in positions:
            positions[symbol]["last_partial_exit_price"] = price
            positions[symbol]["last_partial_exit_at"] = row.get("event_at")
            positions[symbol]["partial_exit_count"] = int(positions[symbol].get("partial_exit_count") or 0) + 1
        elif action == "sell" and symbol in positions:
            pos = positions.pop(symbol)
            pos.update(
                {
                    "status": "closed",
                    "exit_price": price,
                    "exit_at": row.get("event_at"),
                    "exit_event_id": row.get("event_id"),
                    "exit_recommendation_id": row.get("recommendation_id"),
                    "exit_recommendation_action": row.get("recommendation_action"),
                }
            )
            closed.append(pos)
    all_positions = [*positions.values(), *closed]
    prices = load_current_prices([row["symbol"] for row in all_positions], max_age_seconds=None)
    for row in all_positions:
        current = prices.get(row["symbol"]) or {}
        current_price = _to_float(current.get("price"))
        row["current_price"] = current_price
        row["current_price_asof"] = current.get("price_asof")
        row["current_price_source"] = current.get("price_source")
        basis = row.get("exit_price") if row.get("status") == "closed" else current_price
        entry = _to_float(row.get("entry_price"))
        exit_or_current = _to_float(basis)
        row["pnl_pct"] = ((exit_or_current - entry) / entry * 100.0) if entry and exit_or_current else None
    all_positions.sort(key=lambda row: str(row.get("entry_at") or ""), reverse=True)
    return all_positions


def _position_by_symbol(symbol: str) -> dict[str, Any] | None:
    normalized = _normalize_symbol(symbol)
    for row in build_positions():
        if row.get("symbol") == normalized and row.get("status") == "open":
            return row
    return None


def build_recommendations_payload(*, limit: int = 100, symbol: str | None = None) -> dict[str, Any]:
    recommendations = _latest_recommendations(limit=limit, symbol=symbol)
    open_by_symbol = {row["symbol"]: row for row in build_positions() if row.get("status") == "open"}
    for row in recommendations:
        pos = open_by_symbol.get(_normalize_symbol(row.get("symbol")))
        row["operator_position_status"] = "open" if pos else "not_in_portfolio"
        row["operator_position"] = pos or None
        ledger_action = str(row.get("operator_ledger_action") or "")
        if ledger_action == "buy" and pos:
            row["operator_action_disabled_reason"] = "Already open in the operator paper portfolio."
        elif ledger_action in {"sell", "sell_50"} and not pos:
            row["operator_action_disabled_reason"] = "No open paper position to sell."
        else:
            row["operator_action_disabled_reason"] = None
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "status": "ok",
        "recommendations": recommendations,
        "summary": {
            "recommendation_count": len(recommendations),
            "open_position_count": len(open_by_symbol),
            "broker_execution_enabled": False,
            "paper_portfolio_only": True,
        },
    }


def build_portfolio_payload() -> dict[str, Any]:
    positions = build_positions()
    open_positions = [row for row in positions if row.get("status") == "open"]
    closed_positions = [row for row in positions if row.get("status") == "closed"]
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "status": "ok",
        "positions": positions,
        "open_positions": open_positions,
        "closed_positions": closed_positions,
        "summary": {
            "open_count": len(open_positions),
            "closed_count": len(closed_positions),
            "broker_execution_enabled": False,
            "paper_portfolio_only": True,
        },
    }


def apply_operator_action(
    *,
    symbol: str,
    action: str,
    price: float | None = None,
    recommendation_id: str | None = None,
    recommendation_action: str | None = None,
    source: dict[str, Any] | None = None,
    operator_id: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    ensure_operator_portfolio_tables()
    normalized_symbol = _normalize_symbol(symbol)
    requested_action = str(action or "").strip().lower()
    normalized_action = OPERATOR_ACTION_TO_LEDGER_ACTION.get(requested_action)
    if not normalized_symbol:
        raise ValueError("symbol is required")
    if normalized_action not in {"buy", "buy_50", "sell", "sell_50"}:
        raise ValueError("action must be buy, sell, buy_50%, or sell_50%")
    open_position = _position_by_symbol(normalized_symbol)
    if normalized_action == "buy" and open_position:
        raise ValueError(f"{normalized_symbol} is already open in the operator portfolio")
    if normalized_action in {"sell", "sell_50"} and not open_position:
        raise ValueError(f"{normalized_symbol} is not open in the operator portfolio")
    effective_price = _to_float(price)
    if effective_price is None:
        current = load_current_prices([normalized_symbol], max_age_seconds=None).get(normalized_symbol) or {}
        effective_price = _to_float(current.get("price"))
    if effective_price is None:
        raise ValueError("price is required when current price is unavailable")
    now = pd.Timestamp.utcnow()
    row = {
        "event_id": str(uuid.uuid4()),
        "event_at": now,
        "symbol": normalized_symbol,
        "action": normalized_action,
        "price": effective_price,
        "recommendation_id": recommendation_id,
        "recommendation_action": recommendation_action,
        "source_json": json.dumps(source or {}, ensure_ascii=False, sort_keys=True, default=str),
        "operator_id": operator_id or "operator",
        "note": note,
        "load_ts": now,
    }
    df = pd.DataFrame([row])
    for column in ["event_at", "load_ts"]:
        df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    upsert_to_db(df, LEDGER_TABLE, unique_keys=["event_id"])
    return {
        "status": "ok",
        "event": row,
        "portfolio": build_portfolio_payload(),
        "boundary": {
            "paper_portfolio_only": True,
            "mutates_advisory_recommendations": False,
            "mutates_broker_execution": False,
            "submits_order": False,
        },
    }


def reset_operator_portfolio(*, confirm: bool = False) -> dict[str, Any]:
    if not confirm:
        raise ValueError("confirm=true is required to reset the operator portfolio")
    ensure_operator_portfolio_tables()
    deleted = 0
    with db_session() as (_, cur):
        cur.execute(f"DELETE FROM {LEDGER_TABLE}")
        deleted = int(cur.rowcount or 0)
    return {
        "status": "ok",
        "deleted_rows": deleted,
        "table": LEDGER_TABLE,
        "boundary": {
            "paper_portfolio_only": True,
            "mutates_advisory_recommendations": False,
            "mutates_broker_execution": False,
            "submits_order": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset the operator-managed paper portfolio ledger.")
    parser.add_argument("--confirm", action="store_true", help="Required. Deletes all operator paper-portfolio ledger rows.")
    args = parser.parse_args()
    print(json.dumps(reset_operator_portfolio(confirm=bool(args.confirm)), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
