"""Event-driven fundamentals refresh: results filings trigger Sharpely statement pulls.

Quarterly statements change on EVENTS (results announcements, shareholding filings), not
on calendar days -- a daily sweep is almost always a no-op API call, while a purely
weekly sweep can lag a just-reported company by days during results season. This module
closes the loop: companies whose announcements were classified into the results family
(RESULTS_POSITIVE / RESULTS_NEGATIVE / RESULTS_MIXED / DIVIDEND) within the lookback get
a targeted sync_sharpely_data pass. Meta/peers stay on their weekly cadence inside the
sync (SHARPELY_SNAPSHOT_REFRESH_DAYS); statement fetches follow their date windows.

Runs nightly from all_data_readiness.sh (22:30 slot); failures are classified and
recorded by the sync, never fatal to the readiness pass.
"""
from __future__ import annotations

import argparse
import json
import os

import pandas as pd

from utils.db import sql_to_df

RESULTS_EVENT_CLASSES = tuple(
    value.strip().upper()
    for value in os.getenv(
        "FUNDAMENTALS_REFRESH_EVENT_CLASSES",
        "RESULTS_POSITIVE,RESULTS_NEGATIVE,RESULTS_MIXED,DIVIDEND",
    ).split(",")
    if value.strip()
)
LOOKBACK_DAYS = int(os.getenv("FUNDAMENTALS_REFRESH_LOOKBACK_DAYS", "3"))
MAX_SYMBOLS = int(os.getenv("FUNDAMENTALS_REFRESH_MAX_SYMBOLS", "60"))


def load_recent_results_symbols(lookback_days: int | None = None) -> list[str]:
    days = int(lookback_days if lookback_days is not None else LOOKBACK_DAYS)
    frame = sql_to_df(
        """
        SELECT DISTINCT UPPER(TRIM(symbol)) AS symbol
        FROM advisory_event_evaluations
        WHERE evaluated_at > NOW() - make_interval(days => %s)
          AND UPPER(COALESCE(event_class, '')) = ANY(%s)
          AND COALESCE(symbol, '') <> ''
        ORDER BY symbol
        """,
        params=(days, list(RESULTS_EVENT_CLASSES)),
    )
    symbols = [] if frame.empty else [str(s) for s in frame["symbol"].dropna().tolist()]
    return symbols[: max(1, MAX_SYMBOLS)] if symbols else []


NEWLY_ADMITTED_LOOKBACK_DAYS = int(os.getenv("FUNDAMENTALS_REFRESH_NEW_ADMISSION_LOOKBACK_DAYS", "3"))
NEWLY_ADMITTED_MAX_SYMBOLS = int(os.getenv("FUNDAMENTALS_REFRESH_NEW_ADMISSION_MAX_SYMBOLS", "60"))


def load_newly_admitted_symbols(lookback_days: int | None = None) -> list[str]:
    """Dynamic-source names (scan/surge/hypothesis/theme) admitted in the recent window that
    have NO Sharpely fundamentals yet. The quality setups hard-require a fundamental
    snapshot, and these names are admitted faster than the weekly/results-driven Sharpely
    refresh reaches them -- fast-sourcing here lets the next advisory run's fundamentals
    stage build their snapshot so the gate can pass on evidence, not lag."""
    days = int(lookback_days if lookback_days is not None else NEWLY_ADMITTED_LOOKBACK_DAYS)
    frame = sql_to_df(
        """
        SELECT DISTINCT UPPER(TRIM(c.ticker)) AS symbol
        FROM advisory_screener_constituents c
        WHERE c.date > NOW() - make_interval(days => %s)
          AND (c.screener_slug = 'market-action-scan-v1'
               OR c.screener_slug = 'volume-surge-scan-v1'
               OR c.screener_slug LIKE 'hypothesis-%%'
               OR c.screener_slug LIKE 'theme-%%')
          AND COALESCE(c.ticker, '') <> ''
          -- lacks a usable fundamental snapshot (the table the quality gate actually reads),
          -- so meta-only names still get their statements sourced
          AND NOT EXISTS (
              SELECT 1 FROM advisory_fundamentals_daily f
              WHERE UPPER(TRIM(f.symbol)) = UPPER(TRIM(c.ticker))
          )
        ORDER BY symbol
        """,
        params=(days,),
    )
    symbols = [] if frame.empty else [str(s) for s in frame["symbol"].dropna().tolist()]
    return symbols[: max(1, NEWLY_ADMITTED_MAX_SYMBOLS)] if symbols else []


def refresh_from_recent_results(lookback_days: int | None = None, *, dry_run: bool = False) -> dict:
    results_symbols = load_recent_results_symbols(lookback_days)
    admitted_symbols = load_newly_admitted_symbols()
    # union, preserving the results filers first (they carry fresh statements)
    seen: set[str] = set()
    symbols: list[str] = []
    for source in (results_symbols, admitted_symbols):
        for s in source:
            if s not in seen:
                seen.add(s)
                symbols.append(s)
    summary: dict = {
        "trigger_classes": list(RESULTS_EVENT_CLASSES),
        "results_filers": len(results_symbols),
        "newly_admitted": len(admitted_symbols),
        "symbols": symbols,
        "symbol_count": len(symbols),
    }
    if not symbols or dry_run:
        summary["dry_run"] = dry_run
        return summary
    from data.sharpelydata.sharpely_data import sync_sharpely_data

    sync = sync_sharpely_data(symbols)
    summary["rows_written"] = sync.get("rows_written")
    summary["statement_rows"] = sync.get("statement_rows")
    summary["failed_symbol_count"] = sync.get("failed_symbol_count")
    summary["classification_counts"] = sync.get("classification_counts")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh Sharpely fundamentals for results filers + newly-admitted dynamic names.")
    parser.add_argument("--lookback-days", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    summary = refresh_from_recent_results(args.lookback_days, dry_run=bool(args.dry_run))
    print(f"[fundamentals_refresh] {json.dumps(summary, default=str)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
