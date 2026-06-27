"""Operator-tracked holdings store (UI redesign WI-1).

When the operator manually marks a recommendation as "taken", it becomes a tracked holding here:
entry price + date, the originating recommendation/decision source, and an open/exited lifecycle. This
is review-only MONITORING, not paper trading -- there is NO simulated P&L. The outcome labeler can read
these rows to mature each holding's benchmark-excess the same way it does LLM decisions.

The table is created with an append-only migration (never edit an applied CREATE TABLE; follow the
pattern in `advisory/llm_decision_store.py`). `record_take` / `record_exit` / `load_holdings` are the
only write/read surface; everything stays point-in-time and broker-free.
"""

from __future__ import annotations

from typing import Any

from utils.schema_migrations import apply_schema_migration

HOLDINGS_TABLE = "advisory_operator_holdings"
HOLDING_EVENTS_TABLE = "advisory_operator_holding_events"
SCHEMA_MIGRATION_ID = "20260625_advisory_operator_holdings_base"
SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {HOLDINGS_TABLE} (
        symbol TEXT NOT NULL,
        action TEXT,
        entry_price DOUBLE PRECISION,
        entry_date DATE NOT NULL,
        source TEXT,
        status TEXT NOT NULL DEFAULT 'open',
        exit_price DOUBLE PRECISION,
        exit_date DATE,
        note TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ,
        UNIQUE (symbol, entry_date)
    )
    """,
]

# Append-only action log per holding (buy / buy_more / reduce_exposure / hold / sell). The PARENT
# holding keeps the single original entry_price/entry_date, so % since-entry stays anchored to the
# first entry -- buy_more / reduce_exposure are recorded here but never re-average the cost basis.
HOLDING_EVENTS_MIGRATION_ID = "20260627_advisory_operator_holding_events"
HOLDING_EVENTS_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {HOLDING_EVENTS_TABLE} (
        symbol TEXT NOT NULL,
        entry_date DATE NOT NULL,
        event_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        action TEXT NOT NULL,
        price DOUBLE PRECISION,
        quantity_delta_pct DOUBLE PRECISION,
        note TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    f"CREATE INDEX IF NOT EXISTS idx_{HOLDING_EVENTS_TABLE}_symbol_entry ON {HOLDING_EVENTS_TABLE} (symbol, entry_date)",
]

# Allowed action-log verbs. 'sell' also closes the parent holding via record_exit (the API layer).
HOLDING_EVENT_ACTIONS = {"buy", "buy_more", "reduce_exposure", "hold", "sell"}


def ensure_tables() -> None:
    apply_schema_migration(
        migration_id=SCHEMA_MIGRATION_ID,
        description="Create the operator-tracked holdings table (review-only monitoring; no paper P&L).",
        statements=SCHEMA_STATEMENTS,
        metadata={"tables": [HOLDINGS_TABLE], "authority_scope": "operator_holding_review_only"},
    )
    apply_schema_migration(
        migration_id=HOLDING_EVENTS_MIGRATION_ID,
        description="Append-only action log per holding (buy/buy_more/reduce_exposure/hold/sell); % stays anchored to original entry.",
        statements=HOLDING_EVENTS_STATEMENTS,
        metadata={"tables": [HOLDING_EVENTS_TABLE], "authority_scope": "operator_holding_review_only"},
    )


def _norm_symbol(symbol: Any) -> str:
    text = str(symbol or "").strip().upper()
    if not text:
        raise ValueError("symbol is required")
    return text


def record_take(
    *,
    symbol: str,
    entry_price: float | None,
    entry_date: Any,
    action: str | None = None,
    source: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Mark a recommendation as taken -> a new (or re-opened) tracked holding. Idempotent per (symbol, entry_date)."""
    import pandas as pd

    from utils.db import upsert_to_db

    sym = _norm_symbol(symbol)
    if entry_date is None or str(entry_date).strip() == "":
        raise ValueError("entry_date is required")
    row = {
        "symbol": sym,
        "action": (str(action).strip().upper() if action else None),
        "entry_price": (float(entry_price) if entry_price is not None and str(entry_price) != "" else None),
        "entry_date": pd.to_datetime(entry_date).date(),
        "source": (str(source) if source else None),
        "status": "open",
        "exit_price": None,
        "exit_date": None,
        "note": (str(note) if note else None),
    }
    ensure_tables()
    frame = pd.DataFrame([row])
    frame["updated_at"] = pd.Timestamp.utcnow()
    upsert_to_db(frame, HOLDINGS_TABLE, unique_keys=["symbol", "entry_date"])
    # Seed the first action-log event so the holding's history starts at the original entry.
    record_event(symbol=sym, entry_date=row["entry_date"], action="buy", price=row["entry_price"], note=note)
    return row


def record_event(
    *,
    symbol: str,
    entry_date: Any,
    action: str,
    price: float | None = None,
    quantity_delta_pct: float | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Append one action to a holding's log (buy/buy_more/reduce_exposure/hold/sell). Does NOT touch the
    parent holding's entry_price -- % since-entry stays anchored to the original entry. Pure append."""
    import pandas as pd

    from utils.db import db_session, execute_db_operation

    sym = _norm_symbol(symbol)
    verb = str(action or "").strip().lower()
    if verb not in HOLDING_EVENT_ACTIONS:
        raise ValueError(f"unknown holding action: {action!r} (allowed: {sorted(HOLDING_EVENT_ACTIONS)})")
    row = {
        "symbol": sym,
        "entry_date": pd.to_datetime(entry_date).date(),
        "action": verb,
        "price": (float(price) if price is not None and str(price) != "" else None),
        "quantity_delta_pct": (float(quantity_delta_pct) if quantity_delta_pct is not None and str(quantity_delta_pct) != "" else None),
        "note": (str(note) if note else None),
    }
    ensure_tables()

    def _insert_event() -> None:
        with db_session() as (_, cur):
            cur.execute(
                f"""
                INSERT INTO {HOLDING_EVENTS_TABLE} (symbol, entry_date, action, price, quantity_delta_pct, note)
                VALUES (%(symbol)s, %(entry_date)s, %(action)s, %(price)s, %(quantity_delta_pct)s, %(note)s)
                """,
                row,
            )

    execute_db_operation(_insert_event, operation_name=f"operator_holdings:event:{sym}:{verb}")
    return row


def load_events(*, symbols: list[str] | None = None) -> list[dict[str, Any]]:
    """Load the action log, optionally for a set of symbols (newest first). Pure read."""
    from utils.db import sql_to_df

    ensure_tables()
    clauses: list[str] = []
    params: list[Any] = []
    if symbols:
        norm = [_norm_symbol(s) for s in symbols if str(s or "").strip()]
        if norm:
            clauses.append("symbol = ANY(%s)")
            params.append(norm)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    df = sql_to_df(
        f"""
        SELECT symbol, entry_date, event_at, action, price, quantity_delta_pct, note
        FROM {HOLDING_EVENTS_TABLE}
        {where}
        ORDER BY event_at DESC
        """,
        params=tuple(params) if params else None,
    )
    if df.empty:
        return []
    return df.to_dict(orient="records")


def record_exit(
    *,
    symbol: str,
    entry_date: Any,
    exit_price: float | None,
    exit_date: Any,
    note: str | None = None,
) -> int:
    """Close an open holding. Returns rows updated (0 if no matching open holding)."""
    import pandas as pd

    from utils.db import db_session, execute_db_operation

    sym = _norm_symbol(symbol)
    params = {
        "symbol": sym,
        "entry_date": pd.to_datetime(entry_date).date(),
        "exit_price": (float(exit_price) if exit_price is not None and str(exit_price) != "" else None),
        "exit_date": pd.to_datetime(exit_date).date() if exit_date is not None and str(exit_date) != "" else None,
        "note": (str(note) if note else None),
    }
    ensure_tables()

    def _record_exit() -> int:
        with db_session() as (_, cur):
            cur.execute(
                f"""
                UPDATE {HOLDINGS_TABLE}
                SET status = 'exited',
                    exit_price = %(exit_price)s,
                    exit_date = %(exit_date)s,
                    note = COALESCE(%(note)s, note),
                    updated_at = NOW()
                WHERE symbol = %(symbol)s AND entry_date = %(entry_date)s AND status = 'open'
                """,
                params,
            )
            return int(cur.rowcount or 0)

    return execute_db_operation(_record_exit, operation_name=f"operator_holdings:exit:{sym}")


def load_holdings(*, status: str | None = None, symbol: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    """Load tracked holdings, optionally filtered by status ('open'/'exited') and/or symbol. Pure read."""
    from utils.db import sql_to_df

    ensure_tables()
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = %s")
        params.append(str(status).strip().lower())
    if symbol:
        clauses.append("symbol = %s")
        params.append(_norm_symbol(symbol))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT %s"
        params.append(int(limit))
    df = sql_to_df(
        f"""
        SELECT symbol, action, entry_price, entry_date, source, status,
               exit_price, exit_date, note, created_at, updated_at
        FROM {HOLDINGS_TABLE}
        {where}
        ORDER BY entry_date DESC, symbol ASC
        {limit_sql}
        """,
        params=tuple(params) if params else None,
    )
    if df.empty:
        return []
    return df.to_dict(orient="records")
