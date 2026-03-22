from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Iterable, Optional

from utils.db import db_session


@dataclass(frozen=True)
class BackfillSpec:
    table_name: str
    ticker_column: str
    exchange: Optional[str] = None
    exchange_column: Optional[str] = None


SPECS: tuple[BackfillSpec, ...] = (
    BackfillSpec("announcement_pipeline_documents", "ticker", exchange_column="exchange"),
    BackfillSpec("announcement_pipeline_reports", "ticker", exchange_column="exchange"),
    BackfillSpec("stmt_income", "symbol", exchange="NSE"),
    BackfillSpec("stmt_balancesheet", "symbol", exchange="NSE"),
    BackfillSpec("stmt_cashflow", "symbol", exchange="NSE"),
    BackfillSpec("shareholding_category", "symbol", exchange="NSE"),
    BackfillSpec("shareholding_top_holders", "symbol", exchange="NSE"),
    BackfillSpec("historical_mcap", "symbol", exchange="NSE"),
    BackfillSpec("nseindia_events", "symbol", exchange="NSE"),
    BackfillSpec("nseindia_corporate_actions", "symbol", exchange="NSE"),
    BackfillSpec("nseindia_earnings_events", "symbol", exchange="NSE"),
    BackfillSpec("nseindia_insider_deals", "symbol", exchange="NSE"),
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
    BackfillSpec("nseindia_ohlcv_adjusted", "symbol", exchange="NSE"),
    BackfillSpec("dim_security_history", "symbol", exchange="NSE"),
)


def backfill_company_master_ids(specs: Iterable[BackfillSpec] = SPECS) -> list[dict[str, int | str]]:
    results: list[dict[str, int | str]] = []
    for spec in specs:
        with db_session() as (_, cur):
            cur.execute("SET LOCAL statement_timeout = 0")
            if not _table_exists(cur, spec.table_name):
                results.append({"table": spec.table_name, "status": "missing"})
                continue
            if not _column_exists(cur, spec.table_name, spec.ticker_column):
                results.append({"table": spec.table_name, "status": "missing_ticker_column"})
                continue
            if spec.exchange_column and not _column_exists(cur, spec.table_name, spec.exchange_column):
                results.append({"table": spec.table_name, "status": "missing_exchange_column"})
                continue

            cur.execute(f'ALTER TABLE public."{spec.table_name}" ADD COLUMN IF NOT EXISTS company_master_id TEXT')
            updated = _run_update(cur, spec)
            results.append(
                {
                    "table": spec.table_name,
                    "status": "ok",
                    "updated": updated,
                }
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
