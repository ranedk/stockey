from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from typing import Iterable, Optional

from utils.db import db_session, execute_db_operation
from utils.schema_migrations import apply_schema_migration


@dataclass(frozen=True)
class BackfillSpec:
    table_name: str
    ticker_column: str
    exchange: Optional[str] = None
    exchange_column: Optional[str] = None


SPECS: tuple[BackfillSpec, ...] = (
    BackfillSpec("historical_mcap", "symbol", exchange="NSE"),
    BackfillSpec("nseindia_events", "symbol", exchange="NSE"),
    BackfillSpec("nseindia_corporate_actions", "symbol", exchange="NSE"),
    BackfillSpec("nseindia_earnings_events", "symbol", exchange="NSE"),
    BackfillSpec("nseindia_block_deals", "symbol", exchange="NSE"),
    BackfillSpec("nseindia_bulk_deals", "symbol", exchange="NSE"),
    BackfillSpec("nseindia_short_selling", "symbol", exchange="NSE"),
    BackfillSpec("nseindia_mcap", "symbol", exchange="NSE"),
    BackfillSpec("nseindia_circuit_hit", "symbol", exchange="NSE"),
    BackfillSpec("nseindia_corporate_actions_bc_raw", "symbol", exchange="NSE"),
    BackfillSpec("nseindia_ohlcv", "symbol", exchange="NSE"),
    BackfillSpec("nseindia_reg", "symbol", exchange="NSE"),
    BackfillSpec("nseindia_pe", "symbol", exchange="NSE"),
    BackfillSpec("nseindia_mto", "symbol", exchange="NSE"),
    BackfillSpec("nseindia_csqr", "symbol", exchange="NSE"),
    BackfillSpec("nseindia_cmvolt", "symbol", exchange="NSE"),
    BackfillSpec("nseindia_var1", "symbol", exchange="NSE"),
    BackfillSpec("nseindia_catg", "symbol", exchange="NSE"),
    BackfillSpec("nseindia_corporate_actions_normalized", "symbol", exchange="NSE"),
    BackfillSpec("dim_security_history", "symbol", exchange="NSE"),
)


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _schema_migration_id_for_table(table_name: str) -> str:
    safe_name = re.sub(r"[^a-z0-9_]+", "_", table_name.lower()).strip("_")
    return f"20260611_company_master_id_backfill_{safe_name}"


def ensure_company_master_id_column(spec: BackfillSpec) -> None:
    apply_schema_migration(
        migration_id=_schema_migration_id_for_table(spec.table_name),
        description=f"Add company_master_id to {spec.table_name} for cross-source company joins.",
        owner="data.backfill_company_master_ids",
        metadata={
            "tables": [spec.table_name],
            "source": "company_master_id_backfill",
            "ticker_column": spec.ticker_column,
            "exchange": spec.exchange,
            "exchange_column": spec.exchange_column,
        },
        statements=[
            f'ALTER TABLE public.{_quote_identifier(spec.table_name)} ADD COLUMN IF NOT EXISTS company_master_id TEXT'
        ],
    )


def backfill_company_master_ids(specs: Iterable[BackfillSpec] = SPECS) -> list[dict[str, int | str]]:
    results: list[dict[str, int | str]] = []
    for spec in specs:
        def _backfill_table() -> dict[str, int | str]:
            with db_session() as (_, cur):
                cur.execute("SET LOCAL statement_timeout = 0")
                if not _table_exists(cur, spec.table_name):
                    return {"table": spec.table_name, "status": "missing"}
                if not _column_exists(cur, spec.table_name, spec.ticker_column):
                    return {"table": spec.table_name, "status": "missing_ticker_column"}
                if spec.exchange_column and not _column_exists(cur, spec.table_name, spec.exchange_column):
                    return {"table": spec.table_name, "status": "missing_exchange_column"}

                ensure_company_master_id_column(spec)
                updated = _run_update(cur, spec)
                return {
                    "table": spec.table_name,
                    "status": "ok",
                    "updated": updated,
                }

        results.append(
            execute_db_operation(
                _backfill_table,
                operation_name=f"backfill_company_master_ids:{spec.table_name}",
            )
        )
    return results


def _run_update(cur, spec: BackfillSpec) -> int:
    if spec.exchange_column:
        sql = f"""
            UPDATE public."{spec.table_name}" AS dst
            SET company_master_id = CASE
                WHEN UPPER(dst."{spec.exchange_column}") = 'BSE' THEN (
                    SELECT cm.company_master_id
                    FROM public.company_master AS cm
                    WHERE cm.bse_ticker = dst."{spec.ticker_column}"
                    LIMIT 1
                )
                WHEN UPPER(dst."{spec.exchange_column}") = 'NSE' THEN (
                    SELECT cm.company_master_id
                    FROM public.company_master AS cm
                    WHERE cm.nse_ticker = dst."{spec.ticker_column}"
                    LIMIT 1
                )
                ELSE NULL
            END
            WHERE dst.company_master_id IS NULL
        """
    else:
        ticker_field = "nse_ticker" if (spec.exchange or "").upper() == "NSE" else "bse_ticker"
        sql = f"""
            UPDATE public."{spec.table_name}" AS dst
            SET company_master_id = cm.company_master_id
            FROM public.company_master AS cm
            WHERE dst."{spec.ticker_column}" = cm.{ticker_field}
              AND dst.company_master_id IS NULL
        """
    cur.execute(sql)
    return cur.rowcount
def _table_exists(cur, table_name: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = %s
        """,
        (table_name,),
    )
    return cur.fetchone() is not None


def _column_exists(cur, table_name: str, column_name: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s AND column_name = %s
        """,
        (table_name, column_name),
    )
    return cur.fetchone() is not None


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill company_master_id on historical tables")
    parser.add_argument("--table", action="append", dest="tables", help="Specific table(s) to backfill")
    args = parser.parse_args()

    specs = SPECS
    if args.tables:
        wanted = set(args.tables)
        specs = tuple(spec for spec in SPECS if spec.table_name in wanted)

    for row in backfill_company_master_ids(specs):
        print(row, flush=True)


if __name__ == "__main__":
    main()
