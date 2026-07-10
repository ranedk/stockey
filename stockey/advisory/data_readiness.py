"""Data readiness: verify the feeds long-running jobs depend on, and fix what's fixable.

A 4-hour advisory run on a frozen or half-captured feed is worse than no run: it evaluates
the whole universe with missing features, floods the ledger with artifact rejections, and
reads as "the market offered nothing" (observed 2026-07-09/10: a zero-byte bhavcopy zip +
staggered Dhan EOD publication left dynamic-source symbols featureless for two sessions).

Checks, in dependency order (each: ok / warn / error + metrics; every degradation visible):
  1. bhavcopy_current    -- NSE EQ bars exist for the expected complete trading day
  2. dhan_daily_coverage -- % of the reconcile universe with a Dhan bar at that day
  3. rs_panel_current    -- cross-sectional RS panel is at the bhavcopy date
  4. technicals_current  -- technical features exist near the expected day (report-only;
                            the pipeline's catch-up window rebuilds these itself)

Modes:
  --fix      run the bounded repairs for failing checks (bhavcopy download+parse ->
             OHLCV reconcile -> RS rebuild), then re-check.
  --require  exit 1 when any HARD error remains -- the gate for all_advisory / all_ml.
Thresholds are env-tunable; DATA_READINESS_BYPASS=true skips the gate (visible in output).
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any

import pandas as pd

from utils.db import sql_to_df

BHAVCOPY_MIN_ROWS = int(os.getenv("DATA_READINESS_BHAVCOPY_MIN_ROWS", "1500"))
DHAN_WARN_PCT = float(os.getenv("DATA_READINESS_DHAN_WARN_PCT", "90"))
DHAN_ERROR_PCT = float(os.getenv("DATA_READINESS_DHAN_ERROR_PCT", "60"))
TECHNICAL_MAX_AGE_DAYS = int(os.getenv("DATA_READINESS_TECHNICAL_MAX_AGE_DAYS", "2"))


def _expected_day() -> pd.Timestamp:
    from data.dhanlive.ohlcv_reconcile import expected_complete_trading_day

    return expected_complete_trading_day()


def _expected_dhan_day() -> pd.Timestamp:
    """Dhan publishes EOD bars staggered through the evening; expecting TODAY's bars right
    after the 18:00 close makes coverage read ~0% and would refuse the 19:10 advisory every
    night. Shift the completeness boundary so today's bars are only EXPECTED after
    DATA_READINESS_DHAN_TODAY_AFTER_HOUR_IST (default 23); before that, the previous
    trading day is the standard."""
    from data.dhanlive.ohlcv_reconcile import TODAY_COMPLETE_AFTER_HOUR_IST, expected_complete_trading_day

    grace_hour = int(os.getenv("DATA_READINESS_DHAN_TODAY_AFTER_HOUR_IST", "23"))
    shift_hours = max(0, grace_hour - int(TODAY_COMPLETE_AFTER_HOUR_IST))
    return expected_complete_trading_day(pd.Timestamp.utcnow() - pd.Timedelta(hours=shift_hours))


def check_bhavcopy(expected_day: pd.Timestamp) -> dict[str, Any]:
    frame = sql_to_df(
        "SELECT MAX(date) AS latest, COUNT(*) FILTER (WHERE date = (SELECT MAX(date) FROM nseindia_ohlcv WHERE series='EQ')) AS rows "
        "FROM nseindia_ohlcv WHERE series = 'EQ'"
    )
    latest = pd.to_datetime(frame.iloc[0]["latest"], utc=True, errors="coerce") if not frame.empty else None
    rows = int(frame.iloc[0]["rows"] or 0) if not frame.empty else 0
    current = latest is not None and not pd.isna(latest) and latest.normalize() >= expected_day.normalize()
    status = "ok" if (current and rows >= BHAVCOPY_MIN_ROWS) else "error"
    if status == "error" and latest is not None and not pd.isna(latest):
        # Publication grace: NSE posts the day's bhavcopy ~18:30-19:00 IST. Between the
        # 18:00 completeness boundary and the grace hour, being exactly one day behind is
        # a warn (the --fix download usually lands it); after the grace hour it is real.
        grace_hour = int(os.getenv("DATA_READINESS_BHAVCOPY_TODAY_AFTER_HOUR_IST", "20"))
        ist_now = pd.Timestamp.utcnow() + pd.Timedelta(hours=5, minutes=30)
        one_day_behind = (expected_day.normalize() - latest.normalize()).days <= 1
        if one_day_behind and rows >= BHAVCOPY_MIN_ROWS and ist_now.hour < grace_hour:
            status = "warn"
    return {
        "check": "bhavcopy_current", "status": status,
        "latest": None if latest is None or pd.isna(latest) else str(latest.date()),
        "expected": str(expected_day.date()), "latest_day_rows": rows,
        "fix": "python -m data.nseindia.bhavcopy_downloader && python -m data.nseindia.bhavcopy_parser",
    }


def check_dhan_coverage(expected_day: pd.Timestamp) -> dict[str, Any]:
    from data.dhanlive.ohlcv_reconcile import load_universe_symbols

    universe = load_universe_symbols()
    if not universe:
        return {"check": "dhan_daily_coverage", "status": "warn", "reason": "empty reconcile universe"}
    covered = sql_to_df(
        "SELECT COUNT(DISTINCT UPPER(TRIM(ticker))) AS n FROM dhan_ohlcv_daily "
        "WHERE date >= %s AND UPPER(TRIM(ticker)) = ANY(%s)",
        params=(expected_day.normalize(), [str(s).upper() for s in universe]),
    )
    n = int(covered.iloc[0]["n"] or 0) if not covered.empty else 0
    pct = round(100.0 * n / max(1, len(universe)), 1)
    status = "ok" if pct >= DHAN_WARN_PCT else ("warn" if pct >= DHAN_ERROR_PCT else "error")
    return {
        "check": "dhan_daily_coverage", "status": status, "expected": str(expected_day.date()),
        "covered": n, "universe": len(universe), "coverage_pct": pct,
        "fix": "./all_ohlcv_reconcile.sh",
    }


def check_rs_panel() -> dict[str, Any]:
    frame = sql_to_df(
        "SELECT (SELECT MAX(date) FROM advisory_relative_strength_daily) AS rs_latest, "
        "(SELECT MAX(date) FROM nseindia_ohlcv WHERE series='EQ') AS bhav_latest"
    )
    rs_latest = pd.to_datetime(frame.iloc[0]["rs_latest"], utc=True, errors="coerce") if not frame.empty else None
    bhav_latest = pd.to_datetime(frame.iloc[0]["bhav_latest"], utc=True, errors="coerce") if not frame.empty else None
    current = (
        rs_latest is not None and bhav_latest is not None
        and not pd.isna(rs_latest) and not pd.isna(bhav_latest)
        and rs_latest.normalize() >= bhav_latest.normalize()
    )
    return {
        "check": "rs_panel_current", "status": "ok" if current else "warn",
        "rs_latest": None if rs_latest is None or pd.isna(rs_latest) else str(rs_latest.date()),
        "bhavcopy_latest": None if bhav_latest is None or pd.isna(bhav_latest) else str(bhav_latest.date()),
        "fix": "python -m advisory.relative_strength",
    }


def check_technicals(expected_day: pd.Timestamp) -> dict[str, Any]:
    frame = sql_to_df("SELECT MAX(asof_date) AS latest, COUNT(DISTINCT symbol) FILTER (WHERE asof_date = (SELECT MAX(asof_date) FROM advisory_technical_daily)) AS symbols FROM advisory_technical_daily")
    latest = pd.to_datetime(frame.iloc[0]["latest"], utc=True, errors="coerce") if not frame.empty else None
    symbols = int(frame.iloc[0]["symbols"] or 0) if not frame.empty else 0
    age = None if latest is None or pd.isna(latest) else int((expected_day.normalize() - latest.normalize()).days)
    status = "ok" if age is not None and age <= TECHNICAL_MAX_AGE_DAYS else "warn"
    return {
        "check": "technicals_current", "status": status,
        "latest": None if latest is None or pd.isna(latest) else str(latest.date()),
        "age_days": age, "symbols": symbols,
        "note": "report-only: the advisory pipeline rebuilds technicals with its catch-up window",
    }


def run_checks() -> list[dict[str, Any]]:
    expected_day = _expected_day()
    results = [check_bhavcopy(expected_day)]
    results.append(check_dhan_coverage(_expected_dhan_day()))
    results.append(check_rs_panel())
    results.append(check_technicals(expected_day))
    return results


def _run_module(module: str) -> bool:
    import subprocess
    import sys

    proc = subprocess.run([sys.executable, "-m", module], check=False)
    return proc.returncode == 0


def run_fixes(results: list[dict[str, Any]]) -> list[str]:
    """Bounded repairs in dependency order; returns the fixes executed."""
    executed: list[str] = []
    by_check = {r["check"]: r for r in results}
    if by_check.get("bhavcopy_current", {}).get("status") == "error":
        _run_module("data.nseindia.bhavcopy_downloader")
        _run_module("data.nseindia.bhavcopy_parser")
        executed.append("bhavcopy_download_parse")
    if by_check.get("dhan_daily_coverage", {}).get("status") in {"warn", "error"}:
        from data.dhanlive.ohlcv_reconcile import run_reconcile

        run_reconcile()
        executed.append("ohlcv_reconcile")
    if by_check.get("rs_panel_current", {}).get("status") != "ok" or "bhavcopy_download_parse" in executed:
        from advisory.relative_strength import build_relative_strength

        build_relative_strength()
        executed.append("relative_strength")
    return executed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check (and optionally fix) data readiness for long-running jobs.")
    parser.add_argument("--fix", action="store_true", help="Run bounded repairs for failing checks, then re-check")
    parser.add_argument("--require", action="store_true", help="Exit 1 if any hard error remains (gate mode)")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)

    bypass = str(os.getenv("DATA_READINESS_BYPASS", "")).strip().lower() in {"1", "true", "yes"}
    results = run_checks()
    fixes: list[str] = []
    if args.fix and any(r["status"] != "ok" for r in results):
        fixes = run_fixes(results)
        results = run_checks()

    hard_errors = [r for r in results if r["status"] == "error"]
    payload = {
        "status": "error" if hard_errors else ("warn" if any(r["status"] == "warn" for r in results) else "ok"),
        "checks": results, "fixes_executed": fixes, "bypass": bypass,
    }
    if args.format == "json":
        print(json.dumps(payload, indent=2, default=str))
    else:
        for r in results:
            detail = {k: v for k, v in r.items() if k not in {"check", "status", "fix", "note"}}
            print(f"[data_readiness] {r['status']:<5} {r['check']:<22} {detail}")
        if fixes:
            print(f"[data_readiness] fixes_executed={fixes}")
        print(f"[data_readiness] overall={payload['status']} bypass={bypass}")
    if args.require and hard_errors and not bypass:
        print(
            f"[data_readiness] REFUSING to proceed: {[r['check'] for r in hard_errors]} "
            "(set DATA_READINESS_BYPASS=true to override; fixes: run with --fix)",
            flush=True,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
