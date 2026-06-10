from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd
from environs import Env

from utils.db import db_session, sql_to_df, upsert_to_db


env = Env()
env.read_env()

TABLE_NAME = "advisory_current_prices"
DEFAULT_MAX_AGE_SECONDS = env.int("OPERATOR_CURRENT_PRICE_MAX_AGE_SECONDS", 7 * 24 * 60 * 60)
_TABLE_READY = False


def ensure_table(*, force: bool = False) -> None:
    global _TABLE_READY
    if _TABLE_READY and not force:
        return
    with db_session() as (_, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                symbol TEXT PRIMARY KEY,
                price DOUBLE PRECISION,
                price_asof TIMESTAMPTZ,
                price_source TEXT,
                refreshed_at TIMESTAMPTZ
            )
            """
        )
        cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_refreshed ON {TABLE_NAME} (refreshed_at DESC)")
    _TABLE_READY = True


def _normalize_symbols(symbols: list[str] | None) -> list[str]:
    return sorted({str(symbol or "").strip().upper() for symbol in (symbols or []) if str(symbol or "").strip()})


def load_current_prices(symbols: list[str], *, max_age_seconds: int | None = DEFAULT_MAX_AGE_SECONDS) -> dict[str, dict[str, Any]]:
    normalized = _normalize_symbols(symbols)
    if not normalized:
        return {}
    try:
        clauses = ["UPPER(symbol) = ANY(%(symbols)s)", "price IS NOT NULL"]
        params: dict[str, Any] = {"symbols": normalized}
        if max_age_seconds is not None and int(max_age_seconds) > 0:
            clauses.append("refreshed_at >= NOW() - (%(max_age_seconds)s * INTERVAL '1 second')")
            params["max_age_seconds"] = int(max_age_seconds)
        df = sql_to_df(
            f"""
            SELECT UPPER(symbol) AS symbol, price, price_asof, price_source, refreshed_at
            FROM {TABLE_NAME}
            WHERE {' AND '.join(clauses)}
            """,
            params=params,
            retries=2,
            statement_timeout_ms=5000,
        )
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in df.to_dict(orient="records"):
        symbol = str(row.get("symbol") or "").strip().upper()
        if symbol:
            out[symbol] = {
                "price": row.get("price"),
                "price_asof": row.get("price_asof"),
                "price_source": row.get("price_source") or TABLE_NAME,
                "refreshed_at": row.get("refreshed_at"),
            }
    return out


def refresh_current_prices(symbols: list[str] | None = None, *, persist: bool = True) -> dict[str, Any]:
    ensure_table()
    normalized = _normalize_symbols(symbols)
    symbol_filter = "AND UPPER(ticker) = ANY(%(symbols)s)" if normalized else ""
    params: dict[str, Any] = {"symbols": normalized} if normalized else {}
    daily = sql_to_df(
        f"""
        SELECT DISTINCT ON (UPPER(ticker))
            UPPER(ticker) AS symbol,
            close AS price,
            date AS price_asof,
            'dhan_ohlcv_daily' AS price_source,
            NOW() AS refreshed_at
        FROM dhan_ohlcv_daily
        WHERE close IS NOT NULL
          {symbol_filter}
        ORDER BY UPPER(ticker), date DESC, load_ts DESC
        """,
        params=params,
        retries=2,
        statement_timeout_ms=30000,
    )
    if persist and not daily.empty:
        out = daily.copy()
        out["price"] = pd.to_numeric(out["price"], errors="coerce")
        out["price_asof"] = pd.to_datetime(out["price_asof"], utc=True, errors="coerce")
        out["refreshed_at"] = pd.to_datetime(out["refreshed_at"], utc=True, errors="coerce")
        upsert_to_db(out, TABLE_NAME, unique_keys=["symbol"])
    return {
        "status": "ok",
        "table": TABLE_NAME,
        "requested_symbols": len(normalized),
        "refreshed_rows": int(len(daily)),
        "persisted": bool(persist),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh the compact operator current-price cache.")
    parser.add_argument("--symbols", nargs="*", default=None, help="Optional symbols to refresh. Defaults to all Dhan daily symbols.")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = refresh_current_prices(symbols=args.symbols, persist=not bool(args.dry_run))
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
