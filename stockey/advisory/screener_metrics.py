"""Snapshot metrics for dynamically-materialized constituents.

Screener.in rows carry market_cap/last_price from their snapshots, but scan/hypothesis/
theme constituents are assembled from bhavcopy and event data and had no market cap at
all -- so the rule engine's size bands could only soft-flag them as `market_cap:unknown`.
sharpely_stock_meta (rebuilt 2026-07-11 after the cache-poisoning fix, refreshed weekly)
provides a trustworthy per-symbol mcap in the same crore units as Screener.in snapshots.
"""
from __future__ import annotations

import pandas as pd

from advisory.fallback_telemetry import record_local_fallback_event
from utils.db import sql_to_df


def load_market_cap_map(tickers: list[str] | None = None) -> dict[str, dict[str, float | None]]:
    """Latest Sharpely snapshot per symbol: {SYMBOL: {market_cap, last_price}}.
    Best-effort ({} on failure) -- absence stays a visible soft flag downstream."""
    try:
        clauses = ["mcap IS NOT NULL"]
        params: list[object] = []
        if tickers:
            clauses.append("UPPER(TRIM(symbol)) = ANY(%s)")
            params.append(sorted({str(t).strip().upper() for t in tickers if str(t or "").strip()}))
        frame = sql_to_df(
            f"""
            SELECT DISTINCT ON (symbol) UPPER(TRIM(symbol)) AS symbol, mcap, price
            FROM sharpely_stock_meta
            WHERE {' AND '.join(clauses)}
            ORDER BY symbol, as_on_date DESC
            """,
            params=tuple(params) if params else None,
        )
        if frame.empty:
            return {}
        return {
            str(row.symbol): {
                "market_cap": None if pd.isna(row.mcap) else float(row.mcap),
                "last_price": None if pd.isna(row.price) else float(row.price),
            }
            for row in frame.itertuples(index=False)
        }
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.screener_metrics",
            source="sharpely_stock_meta",
            fallback_type="screener_metrics_mcap_lookup_failed",
            severity="warn",
            reason="Sharpely market-cap lookup failed; dynamic constituents stay market_cap:unknown (soft flag).",
            error=exc,
            metadata={"tickers": 0 if not tickers else len(tickers)},
        )
        return {}


def attach_snapshot_metrics(frame: pd.DataFrame) -> pd.DataFrame:
    """Fill missing market_cap/last_price on a constituents frame from the Sharpely map."""
    if frame is None or frame.empty or "ticker" not in frame.columns:
        return frame
    out = frame.copy()
    for column in ("market_cap", "last_price"):
        if column not in out.columns:
            out[column] = None
    tickers = out["ticker"].astype("string").str.strip().str.upper().dropna().unique().tolist()
    metrics = load_market_cap_map(tickers)
    if not metrics:
        return out
    upper = out["ticker"].astype("string").str.strip().str.upper()
    for column in ("market_cap", "last_price"):
        fallback = upper.map(lambda s: (metrics.get(str(s)) or {}).get(column))
        out[column] = out[column].where(out[column].notna(), fallback)
    return out
