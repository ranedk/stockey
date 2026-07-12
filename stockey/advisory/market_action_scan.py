"""Whole-market action discovery: breakout scan over the full NSE bhavcopy.

The configured Screener.in screeners are selection-first (each returns a ~50-name
elite), so the advisory universe covers only ~6% of the market and misses most real
breakouts (measured 2026-07-06: 43 of 51 liquid >=2x-volume 20d-high breakouts were
outside the universe). This module inverts the order for one lane: see ALL market
action first, eliminate junk (liquidity/price floors, chronic circuit-hitters), cap
by breakout quality, and emit the survivors as an ordinary screener-constituents
source (`market-action-scan-v1`). Everything downstream is unchanged -- candidates,
technicals, risk, lifecycle, and action gates still decide; discovery just stops
deciding for them.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import pandas as pd
from environs import Env

from advisory.fallback_telemetry import record_local_fallback_event
from utils.db import sql_to_df

env = Env()

SCAN_SLUG = "market-action-scan-v1"
SCAN_NAME = "Market action scan (whole-market breakouts)"
MARKET_ACTION_SCAN_ENABLED = env.bool("MARKET_ACTION_SCAN_ENABLED", default=True)
SCAN_LIMIT = env.int("MARKET_ACTION_SCAN_LIMIT", default=30)
SCAN_MIN_PRICE = env.float("MARKET_ACTION_SCAN_MIN_PRICE", default=50.0)
SCAN_MIN_TURNOVER_INR = env.float("MARKET_ACTION_SCAN_MIN_TURNOVER_INR", default=50_000_000.0)
SCAN_MIN_VOLUME_MULTIPLE = env.float("MARKET_ACTION_SCAN_MIN_VOLUME_MULTIPLE", default=2.0)
SCAN_MIN_BREAKOUT_PCT = env.float("MARKET_ACTION_SCAN_MIN_BREAKOUT_PCT", default=1.0)
SCAN_MAX_CIRCUIT_HITS_20D = env.int("MARKET_ACTION_SCAN_MAX_CIRCUIT_HITS_20D", default=2)

# Volume-surge sibling: catches the base-breakouts the new-high scan structurally misses
# (measured 2026-07-10: 43 of 65 confirmed movers were below their 52w highs, 80% outside
# our universe). A big single-day move on volume, explicitly NOT already at a 20d high, so
# the two scans partition the mover population and the regret ledger grades each cleanly.
SURGE_SLUG = "volume-surge-scan-v1"
SURGE_NAME = "Volume surge scan (base-breakout momentum)"
VOLUME_SURGE_SCAN_ENABLED = env.bool("VOLUME_SURGE_SCAN_ENABLED", default=True)
SURGE_LIMIT = env.int("VOLUME_SURGE_SCAN_LIMIT", default=30)
SURGE_MIN_CHANGE_PCT = env.float("VOLUME_SURGE_SCAN_MIN_CHANGE_PCT", default=4.0)
SURGE_MIN_VOLUME_MULTIPLE = env.float("VOLUME_SURGE_SCAN_MIN_VOLUME_MULTIPLE", default=3.0)
SURGE_MAX_DIST_BELOW_HIGH_PCT = env.float("VOLUME_SURGE_SCAN_MAX_DIST_BELOW_HIGH_PCT", default=25.0)

# Multi-day momentum: the quiet grind to new highs the single-day breakout/surge scans miss
# (measured 2026-07-10: the largest missed bucket -- 39 of 94 up-movers were multi-day moves
# to new highs, ONMOBILE +44% / SUVEN +34%, never in the funnel). A sustained cumulative move
# over N days, still trending (above DMA20), near the 52w high; not a single-day event.
MOMENTUM_SLUG = "momentum-trend-scan-v1"
MOMENTUM_NAME = "Momentum trend scan (multi-day grind to new highs)"
MOMENTUM_SCAN_ENABLED = env.bool("MOMENTUM_SCAN_ENABLED", default=True)
MOMENTUM_LIMIT = env.int("MOMENTUM_SCAN_LIMIT", default=30)
MOMENTUM_LOOKBACK_DAYS = env.int("MOMENTUM_SCAN_LOOKBACK_DAYS", default=10)
MOMENTUM_MIN_RETURN_PCT = env.float("MOMENTUM_SCAN_MIN_RETURN_PCT", default=15.0)
MOMENTUM_MIN_52W_PROXIMITY = env.float("MOMENTUM_SCAN_MIN_52W_PROXIMITY", default=0.90)

OHLCV_TABLE = "nseindia_ohlcv"
CIRCUIT_TABLE = "nseindia_circuit_hit"


def _scan_query() -> str:
    return f"""
    WITH hist AS (
        SELECT symbol, company_master_id, isin, date, close, volume,
               MAX(close)  OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS max20,
               AVG(volume) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS avgvol20,
               COUNT(*)    OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS nprev
        FROM {OHLCV_TABLE}
        WHERE series = 'EQ' AND date >= %(scan_date)s - interval '45 days' AND date <= %(scan_date)s
    ),
    circuit AS (
        SELECT symbol, COUNT(DISTINCT date) AS hits
        FROM {CIRCUIT_TABLE}
        WHERE date > %(scan_date)s - interval '20 days'
        GROUP BY symbol
    )
    SELECT h.symbol, h.company_master_id, h.isin, h.close,
           ((h.close / h.max20 - 1) * 100)::float AS breakout_pct,
           (h.volume / NULLIF(h.avgvol20, 0))::float AS volume_multiple,
           h.volume, (h.avgvol20 * h.close)::float AS avg_turnover_inr
    FROM hist h
    LEFT JOIN circuit c ON c.symbol = h.symbol
    WHERE h.date = %(scan_date)s
      AND h.nprev >= 15
      AND h.close > h.max20 * (1 + %(min_breakout_pct)s / 100.0)
      AND h.volume >= %(min_volume_multiple)s * h.avgvol20
      AND h.close >= %(min_price)s
      AND h.avgvol20 * h.close >= %(min_turnover)s
      AND COALESCE(c.hits, 0) <= %(max_circuit_hits)s
    ORDER BY (h.volume / NULLIF(h.avgvol20, 0)) DESC
    """


def _surge_query() -> str:
    return f"""
    WITH hist AS (
        SELECT symbol, company_master_id, isin, date, close, volume,
               LAG(close)  OVER (PARTITION BY symbol ORDER BY date) AS prev_close,
               MAX(close)  OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS max20,
               MAX(close)  OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 252 PRECEDING AND 1 PRECEDING) AS max252,
               AVG(volume) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS avgvol20,
               COUNT(*)    OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS nprev
        FROM {OHLCV_TABLE}
        WHERE series = 'EQ' AND date >= %(scan_date)s - interval '400 days' AND date <= %(scan_date)s
    ),
    circuit AS (
        SELECT symbol, COUNT(DISTINCT date) AS hits
        FROM {CIRCUIT_TABLE}
        WHERE date > %(scan_date)s - interval '20 days'
        GROUP BY symbol
    )
    SELECT h.symbol, h.company_master_id, h.isin, h.close,
           ((h.close / NULLIF(h.prev_close, 0) - 1) * 100)::float AS change_pct,
           ((h.close / NULLIF(h.max252, 0)))::float AS proximity_52w_high,
           (h.volume / NULLIF(h.avgvol20, 0))::float AS volume_multiple,
           h.volume, (h.avgvol20 * h.close)::float AS avg_turnover_inr
    FROM hist h
    LEFT JOIN circuit c ON c.symbol = h.symbol
    WHERE h.date = %(scan_date)s
      AND h.nprev >= 15
      AND h.prev_close IS NOT NULL
      AND (h.close / NULLIF(h.prev_close, 0) - 1) * 100 >= %(min_change_pct)s
      AND h.volume >= %(min_volume_multiple)s * h.avgvol20
      -- explicitly NOT a fresh 20d high: those belong to the breakout scan
      AND h.close <= h.max20
      -- but still within striking distance of the 52w high (a base, not a falling knife)
      AND h.max252 IS NOT NULL
      AND h.close >= h.max252 * (1 - %(max_dist_below_high_pct)s / 100.0)
      AND h.close >= %(min_price)s
      AND h.avgvol20 * h.close >= %(min_turnover)s
      AND COALESCE(c.hits, 0) <= %(max_circuit_hits)s
    ORDER BY (h.volume / NULLIF(h.avgvol20, 0)) DESC
    """


def _momentum_query() -> str:
    return f"""
    WITH hist AS (
        SELECT symbol, company_master_id, isin, date, close, volume,
               MAX(close)  OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 252 PRECEDING AND 1 PRECEDING) AS max252,
               NTH_VALUE(close, %(lookback)s) OVER (
                   PARTITION BY symbol ORDER BY date DESC ROWS BETWEEN CURRENT ROW AND %(lookback)s FOLLOWING
               ) AS close_n_ago,
               AVG(volume) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS avgvol20,
               AVG(close)  OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS dma20,
               COUNT(*)    OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 252 PRECEDING AND 1 PRECEDING) AS nprev
        FROM {OHLCV_TABLE}
        WHERE series = 'EQ' AND date >= %(scan_date)s - interval '400 days' AND date <= %(scan_date)s
    ),
    circuit AS (
        SELECT symbol, COUNT(DISTINCT date) AS hits
        FROM {CIRCUIT_TABLE}
        WHERE date > %(scan_date)s - interval '20 days'
        GROUP BY symbol
    )
    SELECT h.symbol, h.company_master_id, h.isin, h.close,
           ((h.close / NULLIF(h.close_n_ago, 0) - 1) * 100)::float AS momentum_return_pct,
           (h.close / NULLIF(h.max252, 0))::float AS proximity_52w_high,
           h.close AS last_price, h.volume, (h.avgvol20 * h.close)::float AS avg_turnover_inr
    FROM hist h
    LEFT JOIN circuit c ON c.symbol = h.symbol
    WHERE h.date = %(scan_date)s
      AND h.nprev >= 60
      AND h.close_n_ago IS NOT NULL
      -- sustained N-day cumulative advance
      AND (h.close / NULLIF(h.close_n_ago, 0) - 1) * 100 >= %(min_return_pct)s
      -- still trending: above a rising short MA
      AND h.close > h.dma20
      -- near the 52w high (a trend leader, not a dead-cat bounce off the lows)
      AND h.max252 IS NOT NULL
      AND h.close >= h.max252 * %(min_52w_proximity)s
      AND h.close >= %(min_price)s
      AND h.avgvol20 * h.close >= %(min_turnover)s
      AND COALESCE(c.hits, 0) <= %(max_circuit_hits)s
    ORDER BY (h.close / NULLIF(h.close_n_ago, 0) - 1) DESC
    """


def _latest_bhavcopy_date(asof_date: Any | None = None) -> pd.Timestamp | None:
    clause = "" if asof_date is None else "WHERE date <= %(asof)s"
    frame = sql_to_df(
        f"SELECT MAX(date) AS d FROM {OHLCV_TABLE} {clause}",
        params=None if asof_date is None else {"asof": pd.Timestamp(asof_date)},
    )
    value = pd.to_datetime(frame.iloc[0]["d"], errors="coerce") if not frame.empty else None
    return None if value is None or pd.isna(value) else pd.Timestamp(value)


def scan_market_action(*, asof_date: Any | None = None, limit: int | None = None) -> pd.DataFrame:
    """Return screener-constituent-shaped rows for the strongest whole-market breakouts.

    Filters are junk-ELIMINATING only (liquidity/price floors, chronic circuit-hitters);
    quality selection stays with the downstream candidate/technical/risk gates.
    """
    scan_date = _latest_bhavcopy_date(asof_date)
    if scan_date is None:
        return pd.DataFrame()
    rows = sql_to_df(
        _scan_query(),
        params={
            "scan_date": scan_date,
            "min_breakout_pct": float(SCAN_MIN_BREAKOUT_PCT),
            "min_volume_multiple": float(SCAN_MIN_VOLUME_MULTIPLE),
            "min_price": float(SCAN_MIN_PRICE),
            "min_turnover": float(SCAN_MIN_TURNOVER_INR),
            "max_circuit_hits": int(SCAN_MAX_CIRCUIT_HITS_20D),
        },
    )
    if rows.empty:
        return pd.DataFrame()
    if limit is None:
        # Daily cap flexes with the recorded regime admission state (risk_off 10 / neutral 30 /
        # risk_on 50 by default); fail-open inside resolve_active_policy returns the neutral cap.
        try:
            from advisory.regime_admission_policy import resolve_active_policy

            limit = int(resolve_active_policy(scan_date).get("scan_limit") or SCAN_LIMIT)
        except Exception:
            limit = None
    cap = int(SCAN_LIMIT if limit is None else limit)
    if cap > 0:
        rows = rows.head(cap)
    now = pd.Timestamp.utcnow()
    out = []
    for rank, row in enumerate(rows.itertuples(index=False), start=1):
        out.append(
            {
                "date": scan_date,
                "screener_slug": SCAN_SLUG,
                "screener_name": SCAN_NAME,
                "screener_url": None,
                "ticker": str(row.symbol).strip().upper(),
                "exchange": "NSE",
                "company_master_id": row.company_master_id,
                "security_id": None,
                "instrument": None,
                "isin": row.isin,
                "display_name": str(row.symbol).strip().upper(),
                "rank": rank,
                "last_price": float(row.close),
                "volume": float(row.volume),
                "raw_item_json": json.dumps(
                    {
                        "source": SCAN_SLUG,
                        "breakout_pct": round(float(row.breakout_pct), 3),
                        "volume_multiple": round(float(row.volume_multiple), 2),
                        "avg_turnover_inr": round(float(row.avg_turnover_inr), 0),
                        "junk_filters": {
                            "min_price": SCAN_MIN_PRICE,
                            "min_turnover_inr": SCAN_MIN_TURNOVER_INR,
                            "max_circuit_hits_20d": SCAN_MAX_CIRCUIT_HITS_20D,
                        },
                    },
                    ensure_ascii=False,
                ),
                "load_ts": now,
            }
        )
    return pd.DataFrame(out)


def safe_scan_market_action(*, asof_date: Any | None = None, limit: int | None = None) -> pd.DataFrame:
    """Scan wrapper for the constituents build path: any failure degrades to an empty
    frame with telemetry so screener parsing is never blocked by the scan."""
    try:
        return scan_market_action(asof_date=asof_date, limit=limit)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.market_action_scan",
            source=OHLCV_TABLE,
            fallback_type="market_action_scan_failed",
            severity="warn",
            reason="Whole-market action scan failed; constituents continue from Screener.in sources only.",
            error=exc,
            metadata={"asof_date": None if asof_date is None else str(asof_date)},
        )
        return pd.DataFrame()


def scan_volume_surge(*, asof_date: Any | None = None, limit: int | None = None) -> pd.DataFrame:
    """Base-breakout momentum: big single-day move on volume, not yet at a 20d high but
    within striking distance of the 52w high. Junk-eliminating filters only; the downstream
    candidate/technical/risk gates still decide quality."""
    scan_date = _latest_bhavcopy_date(asof_date)
    if scan_date is None:
        return pd.DataFrame()
    rows = sql_to_df(
        _surge_query(),
        params={
            "scan_date": scan_date,
            "min_change_pct": float(SURGE_MIN_CHANGE_PCT),
            "min_volume_multiple": float(SURGE_MIN_VOLUME_MULTIPLE),
            "max_dist_below_high_pct": float(SURGE_MAX_DIST_BELOW_HIGH_PCT),
            "min_price": float(SCAN_MIN_PRICE),
            "min_turnover": float(SCAN_MIN_TURNOVER_INR),
            "max_circuit_hits": int(SCAN_MAX_CIRCUIT_HITS_20D),
        },
    )
    if rows.empty:
        return pd.DataFrame()
    if limit is None:
        try:
            from advisory.regime_admission_policy import resolve_active_policy

            limit = int(resolve_active_policy(scan_date).get("scan_limit") or SURGE_LIMIT)
        except Exception:
            limit = None
    cap = int(SURGE_LIMIT if limit is None else limit)
    if cap > 0:
        rows = rows.head(cap)
    now = pd.Timestamp.utcnow()
    out = []
    for rank, row in enumerate(rows.itertuples(index=False), start=1):
        out.append(
            {
                "date": scan_date,
                "screener_slug": SURGE_SLUG,
                "screener_name": SURGE_NAME,
                "screener_url": None,
                "ticker": str(row.symbol).strip().upper(),
                "exchange": "NSE",
                "company_master_id": row.company_master_id,
                "security_id": None,
                "instrument": None,
                "isin": row.isin,
                "display_name": str(row.symbol).strip().upper(),
                "rank": rank,
                "last_price": float(row.close),
                "volume": float(row.volume),
                "raw_item_json": json.dumps(
                    {
                        "source": SURGE_SLUG,
                        "change_pct": round(float(row.change_pct), 3),
                        "volume_multiple": round(float(row.volume_multiple), 2),
                        "proximity_52w_high": round(float(row.proximity_52w_high), 3),
                        "avg_turnover_inr": round(float(row.avg_turnover_inr), 0),
                        "junk_filters": {
                            "min_price": SCAN_MIN_PRICE,
                            "min_turnover_inr": SCAN_MIN_TURNOVER_INR,
                            "max_circuit_hits_20d": SCAN_MAX_CIRCUIT_HITS_20D,
                            "max_dist_below_high_pct": SURGE_MAX_DIST_BELOW_HIGH_PCT,
                        },
                    },
                    ensure_ascii=False,
                ),
                "load_ts": now,
            }
        )
    return pd.DataFrame(out)


def safe_scan_volume_surge(*, asof_date: Any | None = None, limit: int | None = None) -> pd.DataFrame:
    try:
        return scan_volume_surge(asof_date=asof_date, limit=limit)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.market_action_scan",
            source=OHLCV_TABLE,
            fallback_type="volume_surge_scan_failed",
            severity="warn",
            reason="Volume-surge scan failed; constituents continue without the base-breakout lane.",
            error=exc,
            metadata={"asof_date": None if asof_date is None else str(asof_date)},
        )
        return pd.DataFrame()


def scan_momentum_trend(*, asof_date: Any | None = None, limit: int | None = None) -> pd.DataFrame:
    """Multi-day momentum leaders: sustained N-day advance, still above DMA20, near the 52w
    high. Junk-eliminating filters only; downstream candidate/technical/risk gates decide."""
    scan_date = _latest_bhavcopy_date(asof_date)
    if scan_date is None:
        return pd.DataFrame()
    rows = sql_to_df(
        _momentum_query(),
        params={
            "scan_date": scan_date,
            "lookback": int(MOMENTUM_LOOKBACK_DAYS),
            "min_return_pct": float(MOMENTUM_MIN_RETURN_PCT),
            "min_52w_proximity": float(MOMENTUM_MIN_52W_PROXIMITY),
            "min_price": float(SCAN_MIN_PRICE),
            "min_turnover": float(SCAN_MIN_TURNOVER_INR),
            "max_circuit_hits": int(SCAN_MAX_CIRCUIT_HITS_20D),
        },
    )
    if rows.empty:
        return pd.DataFrame()
    if limit is None:
        try:
            from advisory.regime_admission_policy import resolve_active_policy

            limit = int(resolve_active_policy(scan_date).get("scan_limit") or MOMENTUM_LIMIT)
        except Exception:
            limit = None
    cap = int(MOMENTUM_LIMIT if limit is None else limit)
    if cap > 0:
        rows = rows.head(cap)
    now = pd.Timestamp.utcnow()
    out = []
    for rank, row in enumerate(rows.itertuples(index=False), start=1):
        out.append(
            {
                "date": scan_date,
                "screener_slug": MOMENTUM_SLUG,
                "screener_name": MOMENTUM_NAME,
                "screener_url": None,
                "ticker": str(row.symbol).strip().upper(),
                "exchange": "NSE",
                "company_master_id": row.company_master_id,
                "security_id": None,
                "instrument": None,
                "isin": row.isin,
                "display_name": str(row.symbol).strip().upper(),
                "rank": rank,
                "last_price": float(row.close),
                "volume": float(row.volume),
                "raw_item_json": json.dumps(
                    {
                        "source": MOMENTUM_SLUG,
                        "momentum_return_pct": round(float(row.momentum_return_pct), 2),
                        "lookback_days": int(MOMENTUM_LOOKBACK_DAYS),
                        "proximity_52w_high": round(float(row.proximity_52w_high), 3),
                        "avg_turnover_inr": round(float(row.avg_turnover_inr), 0),
                        "junk_filters": {
                            "min_price": SCAN_MIN_PRICE,
                            "min_turnover_inr": SCAN_MIN_TURNOVER_INR,
                            "max_circuit_hits_20d": SCAN_MAX_CIRCUIT_HITS_20D,
                            "min_52w_proximity": MOMENTUM_MIN_52W_PROXIMITY,
                        },
                    },
                    ensure_ascii=False,
                ),
                "load_ts": now,
            }
        )
    return pd.DataFrame(out)


def safe_scan_momentum_trend(*, asof_date: Any | None = None, limit: int | None = None) -> pd.DataFrame:
    try:
        return scan_momentum_trend(asof_date=asof_date, limit=limit)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.market_action_scan",
            source=OHLCV_TABLE,
            fallback_type="momentum_trend_scan_failed",
            severity="warn",
            reason="Momentum-trend scan failed; constituents continue without the multi-day-momentum lane.",
            error=exc,
            metadata={"asof_date": None if asof_date is None else str(asof_date)},
        )
        return pd.DataFrame()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Whole-market breakout scan -> screener constituents source.")
    parser.add_argument("--date", default=None, help="Scan as-of date (default: latest bhavcopy date)")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true", help="Print rows without persisting")
    parser.add_argument("--format", choices=["json", "text"], default="text")
    args = parser.parse_args(argv)

    frame = scan_market_action(asof_date=args.date, limit=args.limit)
    if not args.dry_run and not frame.empty:
        from advisory.screener_parser import persist_constituents

        persist_constituents(frame)
    if args.format == "json":
        print(json.dumps({"rows": len(frame), "dry_run": bool(args.dry_run), "items": json.loads(frame.to_json(orient="records"))}, default=str))
    else:
        print(f"[market_action_scan] rows={len(frame)} dry_run={args.dry_run}")
        for _, row in frame.iterrows():
            meta = json.loads(row["raw_item_json"])
            print(f"  #{int(row['rank']):>2} {row['ticker']:<14} close={row['last_price']:>10.2f} brk={meta['breakout_pct']:>6.2f}% vol_x={meta['volume_multiple']:>5.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
