"""Daily coverage report across every KEEP table in docs/DATA_INVENTORY.md.

For each table: row count, symbol/entity coverage, date range, and a staleness
verdict for tables that should advance every trading day (rows currently dated
in the future -- e.g. announced-but-not-yet-effective corporate actions -- are
excluded from the staleness check, only counted for range/coverage). Tables
that update on real-world events rather than a daily cadence (rbi_bank_rates)
or are forward-looking reference data (holidays, dim_trading_days) are
reported informationally only -- a sparse update history there is normal, not
a data-quality issue in itself (see the 2026-08-05 completeness sweep that
first established this: rbi_bank_rates hasn't "gone stale" just because RBI
hasn't changed the repo rate).

Writes to `data_coverage_report` (upserted daily, one row per table per day)
so trends are queryable over time, and prints a text/JSON summary for cron
logs. Run standalone or via `all_data_coverage_report.sh` (not yet scheduled
by default -- add it to the crontab once you've reviewed a few days of output).
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from typing import Any

import pandas as pd

from utils.db import sql_to_df, upsert_to_db
from utils.fallback_telemetry import record_local_fallback_event
from utils.schema_migrations import apply_schema_migration

STOCKEY_RUN_STATE: dict[str, object] = {}

REPORT_TABLE = "data_coverage_report"
SCHEMA_MIGRATION_ID = "20260805_data_coverage_report_base"
SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {REPORT_TABLE} (
        report_date DATE NOT NULL,
        table_name TEXT NOT NULL,
        category TEXT,
        check_kind TEXT,
        rows BIGINT,
        symbols BIGINT,
        min_date TIMESTAMPTZ,
        max_date TIMESTAMPTZ,
        max_past_or_present_date TIMESTAMPTZ,
        staleness_days INTEGER,
        status TEXT,
        detail TEXT,
        load_ts TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (report_date, table_name)
    )
    """,
]

# (table, date_col, symbol_col, check_kind, category)
#   check_kind:
#     "daily"        -- must advance ~every trading day; staleness triggers warn/error
#     "informational" -- report stats only, no automatic status beyond existence/emptiness
TABLES: list[tuple[str, str | None, str | None, str, str]] = [
    ("nseindia_ohlcv", "date", "symbol", "daily", "NSE bhavcopy"),
    ("nseindia_mcap", "date", "symbol", "daily", "NSE bhavcopy"),
    # nseindia_mto/52wk/cmvolt/circuit_hit/cat_turnover/catg/var1 and the offmarket-sourced
    # short_selling/block_deals/bulk_deals were retired 2026-08-15 (write-only, zero readers
    # anywhere -- see docs/DATA_COVERAGE.md) and dropped from the DB; removed from here with them.
    ("nseindia_corporate_actions_bc_raw", "date", "symbol", "informational", "NSE corporate actions"),
    ("nseindia_corporate_actions_normalized", "date", "symbol", "informational", "NSE corporate actions"),
    ("events_dividend", "ex_date", "symbol", "informational", "NSE corporate actions"),
    ("events_capital_change", "ex_date", "symbol", "informational", "NSE corporate actions"),
    ("advisory_adjusted_ohlcv_daily", "date", "symbol", "daily", "Price adjustment (PRIMARY series)"),
    ("nseindia_indices", "date", "index_name", "daily", "NSE indices"),
    ("nseindia_holidays", "date", None, "informational", "NSE calendar"),
    ("dim_trading_days", "date", None, "informational", "NSE calendar"),
    ("master_dhan_instruments", "load_ts", "symbol_name", "informational", "Dhan broker"),
    ("dhan_ohlcv_daily", "date", "ticker", "daily", "Dhan broker"),
    ("dhan_ohlcv_intraday", "timestamp", "ticker", "informational", "Dhan broker"),
    ("rbi_bank_rates", "date", None, "informational", "RBI/FBIL"),
    ("rbi_currency_rates", "date", None, "daily", "RBI/FBIL"),
    ("fbil_gsec_par", "date", None, "daily", "RBI/FBIL"),
    # fbil_gsec_quote retired 2026-08-15 (zero readers, confirmed live) and dropped from the DB.
    ("company_master", None, "company_master_id", "informational", "Identity"),
    ("dim_security", "effective_from", "symbol", "informational", "Identity"),
    ("historical_mcap", "date", "symbol", "daily", "Sharpely (mcap slice)"),
    ("advisory_sync_state", "updated_at", "source_name", "informational", "Download run state"),
]

# Trading-day-ish staleness thresholds for "daily" tables. Generous on purpose --
# this is a coverage report, not a live gate (data_readiness.py already gates
# the pipeline on bhavcopy/Dhan/benchmark specifically).
WARN_STALENESS_DAYS = 3
ERROR_STALENESS_DAYS = 7


def _today() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC").normalize()


def _report_fallback(fallback_type: str, *, source: str, error: Exception, reason: str) -> None:
    record_local_fallback_event(
        module="scripts.data_coverage_report",
        source=source,
        fallback_type=fallback_type,
        severity="warn",
        reason=reason,
        error=error,
        metadata={"table": source},
    )


def check_table(table: str, date_col: str | None, symbol_col: str | None, check_kind: str, category: str) -> dict[str, Any]:
    today = _today()
    select_parts = ["count(*) as rows"]
    if date_col:
        select_parts.append(f"min({date_col}) as min_date")
        select_parts.append(f"max({date_col}) as max_date")
        select_parts.append(f"max({date_col}) filter (where {date_col} <= now()) as max_past_or_present_date")
    if symbol_col:
        select_parts.append(f"count(distinct {symbol_col}) as symbols")
    sql = f"select {', '.join(select_parts)} from {table}"

    row: dict[str, Any] = {
        "table_name": table,
        "category": category,
        "check_kind": check_kind,
        "rows": 0,
        "symbols": None,
        "min_date": None,
        "max_date": None,
        "max_past_or_present_date": None,
        "staleness_days": None,
        "status": "error",
        "detail": "",
    }
    try:
        df = sql_to_df(sql)
    except Exception as exc:
        _report_fallback(
            "data_coverage_query_failed",
            source=table,
            error=exc,
            reason="Data coverage report could not query a KEEP table; treated as a hard error.",
        )
        row["detail"] = f"{type(exc).__name__}: {exc}"
        return row

    if df.empty:
        row["status"] = "error"
        row["detail"] = "query returned no rows (unexpected)"
        return row

    r = df.iloc[0]
    rows = int(r.get("rows") or 0)
    row["rows"] = rows
    if symbol_col:
        row["symbols"] = int(r.get("symbols") or 0) if not pd.isna(r.get("symbols")) else 0
    if date_col:
        min_date = pd.to_datetime(r.get("min_date"), utc=True, errors="coerce")
        max_date = pd.to_datetime(r.get("max_date"), utc=True, errors="coerce")
        max_past = pd.to_datetime(r.get("max_past_or_present_date"), utc=True, errors="coerce")
        row["min_date"] = None if pd.isna(min_date) else min_date.isoformat()
        row["max_date"] = None if pd.isna(max_date) else max_date.isoformat()
        row["max_past_or_present_date"] = None if pd.isna(max_past) else max_past.isoformat()
        if not pd.isna(max_past):
            row["staleness_days"] = int((today - max_past.normalize()).days)

    if rows <= 0:
        row["status"] = "error"
        row["detail"] = "table is empty"
    elif check_kind == "daily":
        staleness = row["staleness_days"]
        if staleness is None:
            row["status"] = "warn"
            row["detail"] = "no date column result to assess staleness"
        elif staleness > ERROR_STALENESS_DAYS:
            row["status"] = "error"
            row["detail"] = f"{staleness}d stale (>{ERROR_STALENESS_DAYS}d threshold)"
        elif staleness > WARN_STALENESS_DAYS:
            row["status"] = "warn"
            row["detail"] = f"{staleness}d stale (>{WARN_STALENESS_DAYS}d threshold)"
        else:
            row["status"] = "ok"
    else:
        row["status"] = "ok"
        row["detail"] = "informational only, no staleness check"

    return row


def build_report() -> dict[str, Any]:
    today = date.today().isoformat()
    results = [check_table(*spec) for spec in TABLES]
    counts = {"ok": 0, "warn": 0, "error": 0}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    overall = "error" if counts["error"] else ("warn" if counts["warn"] else "ok")
    return {"report_date": today, "overall": overall, "counts": counts, "tables": results}


def persist_report(report: dict[str, Any]) -> None:
    apply_schema_migration(
        migration_id=SCHEMA_MIGRATION_ID,
        statements=SCHEMA_STATEMENTS,
        owner="scripts.data_coverage_report",
        description="Daily per-table coverage/staleness snapshot across the KEEP tables.",
        metadata={"tables": [REPORT_TABLE]},
    )
    rows = []
    now = datetime.now(timezone.utc)
    for r in report["tables"]:
        rows.append(
            {
                "report_date": report["report_date"],
                "table_name": r["table_name"],
                "category": r["category"],
                "check_kind": r["check_kind"],
                "rows": r["rows"],
                "symbols": r["symbols"],
                "min_date": r["min_date"],
                "max_date": r["max_date"],
                "max_past_or_present_date": r["max_past_or_present_date"],
                "staleness_days": r["staleness_days"],
                "status": r["status"],
                "detail": r["detail"],
                "load_ts": now,
            }
        )
    df = pd.DataFrame(rows)
    for col in ["min_date", "max_date", "max_past_or_present_date", "load_ts"]:
        df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")
    df["report_date"] = pd.to_datetime(df["report_date"]).dt.date
    upsert_to_db(df, REPORT_TABLE, unique_keys=["report_date", "table_name"])


def format_text_report(report: dict[str, Any]) -> str:
    lines = [f"[data_coverage] report_date={report['report_date']} overall={report['overall']} counts={report['counts']}"]
    for r in report["tables"]:
        lines.append(
            f"[data_coverage] {r['status']:<5} {r['table_name']:<42} rows={r['rows']:>10,} "
            f"symbols={r['symbols'] if r['symbols'] is not None else '-':>7} "
            f"max_date={str(r['max_date'])[:10] if r['max_date'] else '-':>10} "
            f"staleness_days={r['staleness_days'] if r['staleness_days'] is not None else '-'} "
            f"{('- ' + r['detail']) if r['detail'] else ''}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    global STOCKEY_RUN_STATE
    parser = argparse.ArgumentParser(description="Daily coverage/staleness report across the KEEP tables.")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--no-persist", action="store_true", help="Skip writing to data_coverage_report")
    parser.add_argument("--require", action="store_true", help="Exit 1 if overall status is error")
    args = parser.parse_args(argv)

    report = build_report()
    if not args.no_persist:
        persist_report(report)
    STOCKEY_RUN_STATE = report

    if args.format == "json":
        print(json.dumps(report, indent=2, default=str))
    else:
        print(format_text_report(report))

    if args.require and report["overall"] == "error":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
