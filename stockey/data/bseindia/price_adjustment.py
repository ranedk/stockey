"""BSE split/bonus + total-return adjustment factors -- the BSE-only-company twin of
data/nseindia/price_adjustment.py, built for the same reason (return_3m/6m/12m_pct
and pct_vs_dma_50/200 windows in fundamentals/screens/technicals.py routinely cross a
corporate action, per that module's own docstring; an unadjusted close would corrupt
every one of those stats for a company that split/bonused inside the window).

Reuses data.nseindia.price_adjustment's PURE functions directly (adjust_frame,
compute_total_return_factor) rather than re-implementing the price-step-derived
detection algorithm a second time -- both are already parameterized by column name,
not hardcoded to nseindia_ohlcv. The one real difference from the NSE path: there is
no BSE-side corporate-actions feed to corroborate against (NSE's declared_ratios/
declared_non_split_dates come from nseindia_corporate_actions_bc_raw, which only
covers NSE-listed names) -- so a BSE-only company's split/bonus detection is
price-step-only, unconfirmed by a second source. This is reported plainly via
ca_flag ('split_bonus', never 'split_bonus_ca' or 'declared_non_split_ca' -- those
two flags require declared_ratios/declared_non_split_dates, which are always empty
here) rather than silently claimed as equally confirmed. Same for total-return:
events_dividend is NSE-corporate-action-sourced, so it corroborates a BSE-only name's
dividend history only when that name also has ANY NSE-side history (e.g. was
delisted from NSE, or a corporate action was captured before this company became
BSE-only) -- otherwise cum_total_return_factor stays 1.0 for it, not guessed.

bseindia_adjustment_factors is keyed by (scrip_code, date) -- scrip_code, not symbol,
since that's BSE's real identity key (see data/bseindia/bhavcopy.py's module
docstring: BSE's own ticker text does not match company_master.bse_ticker, which
stores the scrip code). bse_advisory_adjusted_ohlcv_daily is a VIEW over bseindia_
ohlcv x bseindia_adjustment_factors, same "compact factor table + view, never a
separately-maintained adjusted-price table" design NSE's own advisory_adjusted_
ohlcv_daily already uses -- filtered to PRIMARY_SERIES (confirmed live against the
real 43-company BSE-only target universe, see bhavcopy.py), not BSE's full series
list (BSE's govt-securities/preference-share/rights-entitlement series groups are not
equity price history and must not feed return/DMA stats)."""

from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from data.bseindia.bhavcopy import PRIMARY_SERIES
from data.bseindia.bhavcopy import ensure_ohlcv_table
from data.nseindia.price_adjustment import adjust_frame, compute_total_return_factor
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration

ADJUSTMENT_FACTORS_TABLE = "bseindia_adjustment_factors"
ADJUSTED_VIEW = "bse_advisory_adjusted_ohlcv_daily"
FACTORS_MIGRATION_ID = "20260815_bseindia_adjustment_factors"
VIEW_MIGRATION_ID = "20260815_bse_advisory_adjusted_ohlcv_daily_view"

FACTORS_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {ADJUSTMENT_FACTORS_TABLE} (
        scrip_code TEXT NOT NULL,
        date TIMESTAMPTZ NOT NULL,
        cum_price_adjustment_factor DOUBLE PRECISION,
        ca_flag TEXT,
        cum_total_return_factor DOUBLE PRECISION,
        load_ts TIMESTAMPTZ,
        UNIQUE (scrip_code, date)
    )
    """,
]

_SERIES_LIST_SQL = ", ".join(f"'{s}'" for s in PRIMARY_SERIES)

VIEW_SCHEMA_STATEMENTS = [
    f"""
    CREATE OR REPLACE VIEW {ADJUSTED_VIEW} AS
    SELECT
        o.scrip_code,
        o.symbol,
        o.company_master_id,
        o.date,
        o.series,
        o.close,
        o.close * f.cum_price_adjustment_factor AS adj_close,
        o.open * f.cum_price_adjustment_factor AS adj_open,
        o.high * f.cum_price_adjustment_factor AS adj_high,
        o.low * f.cum_price_adjustment_factor AS adj_low,
        o.volume / NULLIF(f.cum_price_adjustment_factor, 0) AS adj_volume,
        o.close * f.cum_total_return_factor AS tr_adj_close,
        f.cum_price_adjustment_factor AS cum_adj_factor,
        f.cum_total_return_factor,
        f.ca_flag,
        f.load_ts
    FROM bseindia_ohlcv o
    JOIN {ADJUSTMENT_FACTORS_TABLE} f ON f.scrip_code = o.scrip_code AND f.date = o.date
    WHERE o.series IN ({_SERIES_LIST_SQL})
    """,
]


def ensure_factors_table() -> None:
    apply_schema_migration(
        migration_id=FACTORS_MIGRATION_ID,
        statements=FACTORS_SCHEMA_STATEMENTS,
        owner="data.bseindia.price_adjustment",
        description="Compact per-(scrip_code,date) BSE adjustment factors -- split/bonus (price-step-derived, unconfirmed by a second source) and total-return (dividend-derived where an NSE-side dividend record exists).",
        metadata={"tables": [ADJUSTMENT_FACTORS_TABLE], "workflow": "bse_price_adjustment"},
    )


def ensure_view() -> None:
    # A CREATE VIEW referencing bseindia_ohlcv fails outright if that table doesn't
    # exist yet (e.g. this is called, via technicals.py's fallback wiring, before
    # data.bseindia.bhavcopy has ever run on a fresh DB) -- ensure it first so this
    # function alone is enough to self-heal, matching the "self-heals a fresh DB or
    # an accidental drop" rationale nseindia's own price_adjustment.ensure_view has.
    ensure_ohlcv_table()
    ensure_factors_table()
    apply_schema_migration(
        migration_id=VIEW_MIGRATION_ID,
        statements=VIEW_SCHEMA_STATEMENTS,
        owner="data.bseindia.price_adjustment",
        description="bse_advisory_adjusted_ohlcv_daily as a view over bseindia_ohlcv x bseindia_adjustment_factors.",
        metadata={"tables": [ADJUSTED_VIEW], "workflow": "bse_price_adjustment"},
    )
    # BUG FOUND LIVE 2026-08-18 (re-audit): the "self-heals an accidental drop"
    # claim above was false once the migration is recorded -- apply_schema_
    # migration() skips re-executing its statements entirely once migration_id is
    # marked 'applied' in stockey_schema_migrations, regardless of whether the VIEW
    # itself still exists. If the view (or bseindia_ohlcv/bseindia_adjustment_
    # factors) is ever dropped after that first successful run, ensure_view() would
    # silently no-op forever after, and the next read would raise UndefinedTable,
    # failing the whole technicals step. CREATE OR REPLACE VIEW is itself already
    # idempotent and cheap, so it's run directly here too, unconditionally, every
    # call -- apply_schema_migration above still provides the checksum-tracked audit
    # trail (a genuine, reviewed change to the view's own SQL text bumps
    # VIEW_MIGRATION_ID), this direct run is what makes ensure_view() ACTUALLY
    # self-heal a drop, not just a fresh DB. Same latent gap in NSE's own
    # price_adjustment module -- not fixed there in this pass, this file only.
    def _create_or_replace_view() -> None:
        with db_session() as (_, cur):
            for statement in VIEW_SCHEMA_STATEMENTS:
                cur.execute(statement)

    execute_db_operation(_create_or_replace_view, operation_name=f"{ADJUSTED_VIEW}:ensure_view_direct")


def build_adjustment_factors(*, dry_run: bool = False) -> dict[str, Any]:
    """Populate bseindia_adjustment_factors for PRIMARY_SERIES. Split/bonus factor:
    price-step detection only (adjust_frame, no declared_ratios/declared_non_split_
    dates -- there is no BSE-side corporate-actions feed, see module docstring).
    Total-return factor: events_dividend-derived where it happens to have a matching
    (symbol, ex_date) row for this scrip's NSE-side history, else 1.0."""
    raw = sql_to_df(
        f"SELECT scrip_code, symbol, date, series, open, close, previous_close "
        f"FROM bseindia_ohlcv WHERE series IN ({_SERIES_LIST_SQL}) ORDER BY scrip_code, date"
    )
    if raw.empty:
        return {"rows": 0}
    raw["date"] = pd.to_datetime(raw["date"], utc=True, errors="coerce")
    adj = adjust_frame(raw, symbol_col="scrip_code", date_col="date", close_col="close", open_col="open")

    # events_dividend is NSE-symbol-keyed, not scrip-code-keyed -- join on the BSE
    # ticker text (symbol) as a best-effort corroboration; most BSE-only names will
    # simply have no match (cum_total_return_factor stays 1.0), which is honest, not
    # a bug -- see module docstring.
    dividends = sql_to_df("SELECT symbol, ex_date, dividend_amount FROM events_dividend")
    adj["cum_total_return_factor"] = compute_total_return_factor(
        adj, dividends, symbol_col="symbol", date_col="date", previous_close_col="previous_close"
    ).to_numpy()

    now = pd.Timestamp.now(tz="UTC")
    out = adj[["scrip_code", "date", "cum_adj_factor", "ca_flag", "cum_total_return_factor"]].rename(
        columns={"cum_adj_factor": "cum_price_adjustment_factor"}
    ).copy()
    out["load_ts"] = now
    summary = {
        "rows": int(len(out)),
        "scrips": int(out["scrip_code"].nunique()),
        "split_bonus_events": int((adj["ca_flag"] == "split_bonus").sum()),
        "ambiguous_flags": int((adj["ca_flag"] == "ambiguous").sum()),
        "price_adjusted_rows": int((out["cum_price_adjustment_factor"] != 1.0).sum()),
        "total_return_adjusted_rows": int((out["cum_total_return_factor"] != 1.0).sum()),
    }
    if not dry_run:
        ensure_factors_table()
        upsert_to_db(out, ADJUSTMENT_FACTORS_TABLE, unique_keys=["scrip_code", "date"])
        ensure_view()
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build BSE-only split/bonus + total-return adjustment factors from price steps.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(build_adjustment_factors(dry_run=bool(args.dry_run)), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
