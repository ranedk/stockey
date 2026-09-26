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
    BackfillSpec("nseindia_mcap", "symbol", exchange="NSE"),
    BackfillSpec("nseindia_corporate_actions_bc_raw", "symbol", exchange="NSE"),
    BackfillSpec("nseindia_ohlcv", "symbol", exchange="NSE"),
    BackfillSpec("nseindia_corporate_actions_normalized", "symbol", exchange="NSE"),
    BackfillSpec("dim_security_history", "symbol", exchange="NSE"),
    # Added 2026-09-24: these carry company_master_id too and had the same NSE-rename gap.
    BackfillSpec("dim_security", "symbol", exchange="NSE"),
    BackfillSpec("nseindia_mto", "symbol", exchange="NSE"),
    BackfillSpec("nseindia_circuit_hit", "symbol", exchange="NSE"),
    BackfillSpec("nseindia_short_selling", "symbol", exchange="NSE"),
    BackfillSpec("nseindia_bulk_deals", "symbol", exchange="NSE"),
    BackfillSpec("nseindia_block_deals", "symbol", exchange="NSE"),
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
    updated = cur.rowcount
    if (spec.exchange or "").upper() == "NSE" and not spec.exchange_column:
        # NSE renames: the new symbol matches no nse_ticker; company_master_nse_alias maps
        # it to the existing company by ISIN (utils.company_master, 2026-09-24).
        cur.execute("SELECT to_regclass('public.company_master_nse_alias') IS NOT NULL")
        if cur.fetchone()[0]:
            cur.execute(
                f"""
                UPDATE public."{spec.table_name}" AS dst
                SET company_master_id = a.company_master_id
                FROM public.company_master_nse_alias AS a
                WHERE dst."{spec.ticker_column}" = a.alias_ticker
                  AND dst.company_master_id IS NULL
                """
            )
            updated += cur.rowcount
    return updated
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


STOCKEY_RUN_STATE: dict[str, object] = {}


def main(argv: list[str] | None = None) -> int:
    global STOCKEY_RUN_STATE
    parser = argparse.ArgumentParser(description="Backfill company_master_id on historical tables")
    parser.add_argument("--table", action="append", dest="tables", help="Specific table(s) to backfill")
    args = parser.parse_args(argv if argv is not None else [])
    specs = SPECS
    if args.tables:
        wanted = set(args.tables)
        specs = tuple(spec for spec in SPECS if spec.table_name in wanted)
    results = backfill_company_master_ids(specs)
    for row in results:
        print(row, flush=True)
    updated = sum(int(r.get("updated", 0)) for r in results)
    STOCKEY_RUN_STATE = {"source": "data.backfill_company_master_ids", "rows": updated,
                         "rows_written": updated, "tables": len(results), "status": "ok",
                         "state_advanced": updated > 0}
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
