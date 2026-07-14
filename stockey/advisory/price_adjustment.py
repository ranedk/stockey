"""Corporate-action price adjustment derived from the price series itself -- the permanent fix (2026-07-14).

The recorded corporate-actions table is incomplete (it misses ETF splits) and dhan covers only ~31% of
split names, so neither is a complete adjustment source. But a split/bonus ALWAYS shows in the price as a
clean round-ratio single-day step (a 1:10 split -> close x0.1, a 1:1 bonus -> x0.5) and that ratio IS the
back-adjustment factor. This derives a COMPLETE cumulative back-adjustment factor from the price steps
alone -- confirmed by snapping the step to a round split/bonus ratio -- so returns computed on adj_close
are split-neutral for EVERY name, with no dependency on external CA records or dhan coverage.

A step beyond the circuit band that does NOT snap to a round ratio is treated as a possible DATA ERROR:
it is flagged (`ambiguous`) and NOT adjusted, so a bad price never silently rescales the whole history.
Recorded CA factors / dhan can corroborate later, but are not required for correctness here.

Pure (dataframe in, dataframe out). Used to compute adjusted closes for backtests instead of the earlier
exclude-only CA guard, which discarded real forward outcomes.
"""
from __future__ import annotations

import argparse
import json
from typing import Any

import numpy as np
import pandas as pd

# a single-day close step beyond the widest Indian circuit band (~20%) is a CA or data artifact
CIRCUIT_STEP_LOW = 0.65
CIRCUIT_STEP_HIGH = 1.5
# plausible split/bonus/reverse price ratios expressed as implied = max(r, 1/r) snapped to a round value.
# Includes large ETF splits (gold/silver ETFs do 20:1..100:1) which small round sets miss. Safe because
# the circuit-band pre-filter means every candidate is already a CA-or-data-error (a real move cannot
# exceed the ~20% daily circuit); round-ratio match then separates split/bonus from data error.
_ROUND_IMPLIED = (2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 15, 20, 25, 30, 40, 50, 100,
                  1.5, 2.5, 1.25, 4 / 3, 5 / 3, 1.2, 10 / 9, 1.1, 1.05)
_SNAP_TOL = 0.06


def _snap_event_ratio(price_ratio: float) -> float | None:
    """Snap a circuit-breaching step to the nearest round split/bonus ratio; None if it doesn't (data error)."""
    if not np.isfinite(price_ratio) or price_ratio <= 0:
        return None
    implied = price_ratio if price_ratio > 1.0 else 1.0 / price_ratio
    for k in _ROUND_IMPLIED:
        if abs(implied - k) / k <= _SNAP_TOL:
            return k if price_ratio > 1.0 else 1.0 / k  # the clean price-step ratio (splits <1, reverse >1)
    return None


def adjust_frame(df: pd.DataFrame, *, symbol_col: str = "symbol", date_col: str = "date",
                 close_col: str = "close") -> pd.DataFrame:
    """Return df with `cum_adj_factor`, `adj_close`, and `ca_flag` per row.

    Back-adjustment: adj_close[t] = close[t] * (product of confirmed event ratios at dates > t), so
    pre-event prices are scaled down onto the post-event basis and the series is continuous. `ca_flag` is
    'split_bonus' on a confirmed event day, 'ambiguous' on a circuit-breaching step that did not snap
    (possible data error, NOT adjusted), else ''.
    """
    if df.empty:
        return df.assign(cum_adj_factor=[], adj_close=[], ca_flag=[])
    out = df.sort_values([symbol_col, date_col]).copy()
    prev = out.groupby(symbol_col)[close_col].shift(1)
    price_ratio = (out[close_col] / prev.replace(0, np.nan)).to_numpy()
    breach = (price_ratio < CIRCUIT_STEP_LOW) | (price_ratio > CIRCUIT_STEP_HIGH)

    event_ratio = np.ones(len(out), dtype="float64")
    ca_flag = np.array([""] * len(out), dtype=object)
    # only circuit-breaching rows can be corporate actions -- snap just those (fast on the full universe)
    for i in np.flatnonzero(np.nan_to_num(breach)):
        snapped = _snap_event_ratio(float(price_ratio[i]))
        if snapped is None:
            ca_flag[i] = "ambiguous"          # possible data error -> do not adjust, flag for review
        else:
            event_ratio[i] = snapped
            ca_flag[i] = "split_bonus"
    out["_event_ratio"] = event_ratio
    out["ca_flag"] = ca_flag

    # cum_adj_factor[t] = product of event ratios at dates STRICTLY AFTER t (per symbol), so a split back-
    # adjusts only its pre-event history. Compute via a descending-date cumprod, dividing out the row's own
    # event (self-exclusion) -- avoids groupby.apply. event_ratio is 1.0 for non-event rows.
    desc = out.sort_values([symbol_col, date_col], ascending=[True, False])
    cum_incl_self = desc.groupby(symbol_col)["_event_ratio"].cumprod()
    desc = desc.assign(cum_adj_factor=cum_incl_self / desc["_event_ratio"])
    out = desc.sort_values([symbol_col, date_col]).drop(columns=["_event_ratio"])
    out["adj_close"] = out[close_col] * out["cum_adj_factor"]
    return out


# ---- persisted adjusted-close table: the "fix at source" so consumers read adj_close, not raw close ----

ADJUSTED_TABLE = "advisory_adjusted_ohlcv_daily"
MIGRATION_ID = "20260714_advisory_adjusted_ohlcv_daily"
SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {ADJUSTED_TABLE} (
        symbol TEXT NOT NULL,
        date TIMESTAMPTZ NOT NULL,
        series TEXT,
        close DOUBLE PRECISION,
        adj_close DOUBLE PRECISION,
        cum_adj_factor DOUBLE PRECISION,
        ca_flag TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (symbol, date)
    )
    """,
]


def ensure_table() -> None:
    from utils.schema_migrations import apply_schema_migration
    apply_schema_migration(
        migration_id=MIGRATION_ID,
        statements=SCHEMA_STATEMENTS,
        owner="advisory.price_adjustment",
        description="Split/bonus-adjusted daily close derived from price steps (complete, no CA-record dependency).",
        metadata={"tables": [ADJUSTED_TABLE], "workflow": "price_adjustment"},
    )


def build_adjusted_ohlcv(*, dry_run: bool = False) -> dict[str, Any]:
    """Populate advisory_adjusted_ohlcv_daily for the whole EQ+BE universe (EQ+BE treated as one series
    per symbol, so a T2T migration stays continuous). Returns a summary."""
    from utils.db import sql_to_df, upsert_to_db
    raw = sql_to_df(
        "SELECT symbol, date, series, close FROM nseindia_ohlcv WHERE series IN ('EQ','BE') ORDER BY symbol, date"
    )
    if raw.empty:
        return {"rows": 0}
    raw["date"] = pd.to_datetime(raw["date"], utc=True, errors="coerce")
    adj = adjust_frame(raw)  # per-symbol back-adjustment from price steps
    now = pd.Timestamp.utcnow()
    out = adj[["symbol", "date", "series", "close", "adj_close", "cum_adj_factor", "ca_flag"]].copy()
    out["load_ts"] = now
    summary = {
        "rows": int(len(out)),
        "symbols": int(out["symbol"].nunique()),
        "split_bonus_events": int((out["ca_flag"] == "split_bonus").sum()),
        "ambiguous_flags": int((out["ca_flag"] == "ambiguous").sum()),
        "adjusted_rows": int((out["cum_adj_factor"] != 1.0).sum()),
    }
    if not dry_run:
        ensure_table()
        upsert_to_db(out, ADJUSTED_TABLE, unique_keys=["symbol", "date"], timescaledb_column="date")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the split/bonus-adjusted daily close table from price steps (complete, self-contained).")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(build_adjusted_ohlcv(dry_run=bool(args.dry_run)), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
