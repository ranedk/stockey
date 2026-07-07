"""Cross-sectional relative strength: the selection layer for a 5-slot portfolio.

Discovery (screeners, market scan, rescues) produces ~100+ watch names; the portfolio holds
at most five. Existing `rs_vs_benchmark`/`rs_vs_sector` are pairwise 20d return spreads and
say nothing about how a name ranks against the whole market. This module computes a daily
full-market cross-sectional percentile (the IBD-style RS rank):

- 63 / 126 / 252 trading-day returns + 52-week-high proximity per symbol
  (whole NSE EQ universe from the already-ingested bhavcopy),
- each component percentile-ranked across the day's universe (rank-of-ranks is robust to
  outliers -- same pattern as price_factors.build_confidence_scores),
- weighted composite -> `rs_percentile` (0-100), higher = stronger,
- a liquidity floor keeps illiquid microcaps from distorting the ranks.

Consumers: rule-engine candidates carry `rs_percentile`; the portfolio engine adds a bounded
priority term so a stronger name wins the marginal slot; the recommendations queue exposes
and tie-breaks on it. Missing RS is always neutral (None -> no term) -- ranking helps choose
among candidates, it never blocks one.
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any

import pandas as pd

from advisory.fallback_telemetry import record_local_fallback_event
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration

TABLE_NAME = "advisory_relative_strength_daily"
RELATIVE_STRENGTH_MIGRATION_ID = "20260707_advisory_relative_strength_base"
RELATIVE_STRENGTH_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        date TIMESTAMPTZ NOT NULL,
        symbol TEXT NOT NULL,
        ret_63d DOUBLE PRECISION,
        ret_126d DOUBLE PRECISION,
        ret_252d DOUBLE PRECISION,
        high_52w_proximity DOUBLE PRECISION,
        rs_score DOUBLE PRECISION,
        rs_percentile DOUBLE PRECISION,
        rank_universe_size BIGINT,
        avg_turnover_inr DOUBLE PRECISION,
        load_ts TIMESTAMPTZ,
        UNIQUE (date, symbol)
    )
    """,
]

PRICES_TABLE = "nseindia_ohlcv"
MIN_HISTORY_ROWS = int(os.getenv("RS_RANK_MIN_HISTORY_ROWS", "64"))
MIN_TURNOVER_INR = float(os.getenv("RS_RANK_MIN_TURNOVER_INR", "10000000"))  # 1cr
WEIGHT_63D = float(os.getenv("RS_RANK_WEIGHT_63D", "0.4"))
WEIGHT_126D = float(os.getenv("RS_RANK_WEIGHT_126D", "0.3"))
WEIGHT_252D = float(os.getenv("RS_RANK_WEIGHT_252D", "0.2"))
WEIGHT_HIGH_PROXIMITY = float(os.getenv("RS_RANK_WEIGHT_HIGH_PROXIMITY", "0.1"))
_COMPONENT_WEIGHTS = {
    "ret_63d": WEIGHT_63D,
    "ret_126d": WEIGHT_126D,
    "ret_252d": WEIGHT_252D,
    "high_52w_proximity": WEIGHT_HIGH_PROXIMITY,
}


def ensure_table() -> None:
    apply_schema_migration(
        migration_id=RELATIVE_STRENGTH_MIGRATION_ID,
        description="Create the daily cross-sectional relative-strength percentile table.",
        statements=RELATIVE_STRENGTH_SCHEMA_STATEMENTS,
        metadata={"module": "advisory.relative_strength", "tables": [TABLE_NAME]},
    )


def _load_return_panel(asof_date: pd.Timestamp) -> pd.DataFrame:
    """One row per symbol: latest close, lagged closes at 63/126/252 trading rows,
    252-row max close, 20-row average turnover, and available history depth."""
    return sql_to_df(
        f"""
        WITH ranked AS (
            SELECT symbol, close, volume,
                   ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
            FROM {PRICES_TABLE}
            WHERE series = 'EQ' AND date <= %(asof)s AND date > %(asof)s - interval '420 days'
        )
        SELECT symbol,
               MAX(close) FILTER (WHERE rn = 1) AS c0,
               MAX(close) FILTER (WHERE rn = 64) AS c63,
               MAX(close) FILTER (WHERE rn = 127) AS c126,
               MAX(close) FILTER (WHERE rn = 253) AS c252,
               MAX(close) FILTER (WHERE rn <= 252) AS max_252,
               AVG(close * volume) FILTER (WHERE rn <= 20) AS avg_turnover_inr,
               COUNT(*) AS history_rows
        FROM ranked
        GROUP BY symbol
        """,
        params={"asof": asof_date},
    )


def compute_rs_frame(panel: pd.DataFrame, *, asof_date: pd.Timestamp) -> pd.DataFrame:
    """Cross-sectional RS percentiles from the per-symbol return panel (pure; testable)."""
    if panel.empty:
        return pd.DataFrame()
    frame = panel.copy()
    for column in ("c0", "c63", "c126", "c252", "max_252", "avg_turnover_inr"):
        frame[column] = pd.to_numeric(frame.get(column), errors="coerce")
    frame = frame[
        (pd.to_numeric(frame["history_rows"], errors="coerce") >= MIN_HISTORY_ROWS)
        & frame["c0"].notna()
        & (frame["c0"] > 0)
        & (frame["avg_turnover_inr"].fillna(0) >= MIN_TURNOVER_INR)
    ].copy()
    if frame.empty:
        return pd.DataFrame()
    frame["ret_63d"] = frame["c0"] / frame["c63"] - 1.0
    frame["ret_126d"] = frame["c0"] / frame["c126"] - 1.0
    frame["ret_252d"] = frame["c0"] / frame["c252"] - 1.0
    frame["high_52w_proximity"] = frame["c0"] / frame["max_252"]

    # rank-of-ranks: each component percentile-ranked across the day's universe, composite =
    # weighted mean over AVAILABLE components (weights renormalized), final percentile of that.
    weighted_sum = pd.Series(0.0, index=frame.index)
    weight_total = pd.Series(0.0, index=frame.index)
    for column, weight in _COMPONENT_WEIGHTS.items():
        component_rank = frame[column].rank(pct=True)
        available = frame[column].notna()
        weighted_sum = weighted_sum + component_rank.fillna(0.0) * weight * available
        weight_total = weight_total + weight * available
    frame["rs_score"] = (weighted_sum / weight_total.replace(0, pd.NA)).astype(float)
    frame = frame[frame["rs_score"].notna()].copy()
    if frame.empty:
        return pd.DataFrame()
    frame["rs_percentile"] = (frame["rs_score"].rank(pct=True) * 100.0).round(2)
    frame["rank_universe_size"] = int(len(frame))
    frame["date"] = pd.Timestamp(asof_date).normalize()
    frame["symbol"] = frame["symbol"].astype("string").str.strip().str.upper()
    frame["load_ts"] = pd.Timestamp.utcnow()
    columns = [
        "date", "symbol", "ret_63d", "ret_126d", "ret_252d", "high_52w_proximity",
        "rs_score", "rs_percentile", "rank_universe_size", "avg_turnover_inr", "load_ts",
    ]
    return frame[columns].reset_index(drop=True)


def _latest_price_date(asof_date: Any | None = None) -> pd.Timestamp | None:
    clause = "" if asof_date is None else "AND date <= %(asof)s"
    frame = sql_to_df(
        f"SELECT MAX(date) AS d FROM {PRICES_TABLE} WHERE series = 'EQ' {clause}",
        params=None if asof_date is None else {"asof": pd.Timestamp(asof_date)},
    )
    value = pd.to_datetime(frame.iloc[0]["d"], errors="coerce") if not frame.empty else None
    return None if value is None or pd.isna(value) else pd.Timestamp(value)


def build_relative_strength(*, asof_date: Any | None = None, dry_run: bool = False) -> dict[str, Any]:
    ensure_table()
    effective = _latest_price_date(asof_date)
    if effective is None:
        return {"date": None, "rows": 0, "dry_run": dry_run}
    panel = _load_return_panel(effective)
    frame = compute_rs_frame(panel, asof_date=effective)
    if not dry_run and not frame.empty:
        upsert_to_db(frame, TABLE_NAME, unique_keys=["date", "symbol"], timescaledb_column="date")
    return {"date": effective.date().isoformat(), "rows": int(len(frame)), "dry_run": bool(dry_run)}


def load_rs_percentiles(symbols: list[str] | None = None, *, asof_date: Any | None = None) -> dict[str, float]:
    """Latest point-in-time rs_percentile per symbol (<= asof). Best-effort: {} on failure so
    missing RS is always neutral for consumers."""
    try:
        parsed = pd.to_datetime(asof_date, utc=True, errors="coerce")
        if pd.isna(parsed):
            parsed = pd.Timestamp.utcnow()
        clauses = ["date <= %(asof)s", "date > %(asof)s - interval '10 days'"]
        params: dict[str, Any] = {"asof": parsed}
        if symbols:
            clauses.append("UPPER(TRIM(symbol)) = ANY(%(symbols)s)")
            params["symbols"] = [str(value).strip().upper() for value in symbols]
        frame = sql_to_df(
            f"""
            SELECT DISTINCT ON (symbol) UPPER(TRIM(symbol)) AS symbol, rs_percentile
            FROM {TABLE_NAME}
            WHERE {' AND '.join(clauses)}
            ORDER BY symbol, date DESC
            """,
            params=params,
        )
        if frame.empty:
            return {}
        return {
            str(row.symbol): float(row.rs_percentile)
            for row in frame.itertuples(index=False)
            if row.rs_percentile is not None and not pd.isna(row.rs_percentile)
        }
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.relative_strength", source=TABLE_NAME, severity="warn",
            fallback_type="relative_strength_lookup_failed",
            reason="RS percentile lookup failed; consumers treat RS as neutral (no ranking term).",
            error=exc, metadata={"symbols": 0 if not symbols else len(symbols)},
        )
        return {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compute daily cross-sectional relative-strength percentiles.")
    parser.add_argument("--date", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--format", choices=["json", "text"], default="text")
    parser.add_argument("--top", type=int, default=0, help="Print the top-N symbols by rs_percentile")
    args = parser.parse_args(argv)
    result = build_relative_strength(asof_date=args.date, dry_run=bool(args.dry_run))
    if args.format == "json":
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"[relative_strength] date={result['date']} rows={result['rows']} dry_run={result['dry_run']}")
    if args.top > 0 and result.get("rows"):
        top = sql_to_df(
            f"SELECT symbol, rs_percentile, ret_63d, high_52w_proximity FROM {TABLE_NAME} "
            "WHERE date = %(d)s ORDER BY rs_percentile DESC LIMIT %(n)s",
            params={"d": result["date"], "n": int(args.top)},
        )
        for row in top.itertuples(index=False):
            print(f"  {row.symbol:<14} rs={row.rs_percentile:>6.2f} ret63={row.ret_63d:+.1%} highprox={row.high_52w_proximity:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
