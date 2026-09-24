"""One answer to "what sector is this company in" for every fundamentals reader.

Six readers (sector_cycle, confluence_score, l2_state, watch_summary, signal_pointers,
the API's sectors page) each took the latest dim_security row with a sector_code. But
dim_security is built from NSE price history, so a company that trades only on BSE has
no row there and never got a sector: 16 of the 17 active watchlist names without a sector
on 2026-09-24 were BSE-only, and Sharpely's own master lists every one of them with a
sector under the same ticker company_master uses.

The view keeps dim_security first (it already falls back to Sharpely by ISIN for NSE
names), then Sharpely by NSE ticker, then by BSE ticker. `sector_source` says which one
answered. company_master.sharpely_id would be the natural key, but it is NULL on every
row, so the ticker joins are what works.
"""
from __future__ import annotations

COMPANY_SECTOR_VIEW = "fundamentals_company_sector"
VIEW_MIGRATION_ID = "20260924_fundamentals_company_sector_view"
VIEW_STATEMENTS = [
    f"""
    CREATE OR REPLACE VIEW {COMPANY_SECTOR_VIEW} AS
    SELECT DISTINCT ON (company_master_id) company_master_id, sector_code, sector_source
    FROM (
        SELECT ds.company_master_id, ds.sector_code, 'dim_security' AS sector_source, 0 AS preference,
               ds.last_trade_date, ds.effective_to
          FROM dim_security ds
         WHERE ds.company_master_id IS NOT NULL AND ds.sector_code IS NOT NULL
        UNION ALL
        SELECT cm.company_master_id, m.sector_code, 'sharpely_nse_ticker', 1, NULL, NULL
          FROM company_master cm
          JOIN master_sharpely_equity m ON m.symbol = cm.nse_ticker
         WHERE m.sector_code IS NOT NULL
        UNION ALL
        SELECT cm.company_master_id, m.sector_code, 'sharpely_bse_ticker', 2, NULL, NULL
          FROM company_master cm
          JOIN master_sharpely_equity m ON m.bse_ticker = cm.bse_ticker
         WHERE m.sector_code IS NOT NULL AND cm.bse_ticker IS NOT NULL
    ) candidates
    ORDER BY company_master_id, preference, last_trade_date DESC NULLS LAST,
             effective_to DESC NULLS LAST, sector_code
    """
]


def ensure_company_sector_view() -> None:
    from utils.schema_migrations import apply_schema_migration
    apply_schema_migration(
        migration_id=VIEW_MIGRATION_ID,
        statements=VIEW_STATEMENTS,
        owner="fundamentals.screens.company_sector",
        description="Company -> sector: dim_security first, then Sharpely by NSE ticker, then BSE ticker.",
        metadata={"tables": [COMPANY_SECTOR_VIEW]},
    )

