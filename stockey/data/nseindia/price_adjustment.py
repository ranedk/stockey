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
import re
from typing import Any

import numpy as np
import pandas as pd

# ---- declared corporate-action ratios (NSE Bc feed) -> confirm/supply the ratio the price-step heuristic
# ---- misses. A step that breaches the circuit band but does not snap to a round ratio (e.g. a 1:1 bonus
# ---- whose ex-date also moved a few %, or a 5:2 / 7:5 ratio absent from the round set) is left ambiguous
# ---- and UNADJUSTED by the price-only path; cross-referencing NSE's declared split/bonus fixes exactly
# ---- those. Bonus "X:Y" = X new shares per Y held -> price factor Y/(X+Y); FV split A->B -> factor B/A.
_BONUS_RE = re.compile(r"BON(?:US)?\.?\s*(\d+)\s*:\s*(\d+)")
_SPLIT_RE = re.compile(r"(?:F\.?\s*V\.?\s*)?(?:SPLI?T|SUB[\s-]*DIV)\D*?(\d+)\D+?TO\D*?(\d+)")


def _events_from_subject(subject: str) -> set[tuple[str, int, int]]:
    """Parse the canonical (kind, a, b) split/bonus events out of one Bc `subject` string. Splits on
    '/', ';', '|' so a combined 'BONUS1:1/FVSPLIT 10 TO 5' yields both events."""
    events: set[tuple[str, int, int]] = set()
    for part in re.split(r"[/;|]", str(subject).upper()):
        mb = _BONUS_RE.search(part)
        if mb:
            events.add(("bonus", int(mb.group(1)), int(mb.group(2))))
        ms = _SPLIT_RE.search(part)
        if ms:
            events.add(("split", int(ms.group(1)), int(ms.group(2))))
    return events


def _factor_for_events(events: set[tuple[str, int, int]]) -> float | None:
    """Combined price multiplier for a set of DISTINCT same-day events (duplicates already deduped by the
    set). None if no split/bonus event was found."""
    if not events:
        return None
    factor = 1.0
    for kind, a, b in events:
        if a <= 0 or b <= 0:
            return None
        factor *= (b / (a + b)) if kind == "bonus" else (b / a)
    return factor


def load_declared_ca_ratios() -> dict[tuple[str, Any], float]:
    """Map (symbol, ex-date) -> declared split/bonus price factor from nseindia_corporate_actions_bc_raw.
    Duplicate declarations (same event listed per series) dedupe; genuinely distinct same-day events
    (a bonus AND a split) multiply. Only ratios that move the price >2% are kept (a real CA)."""
    from utils.db import sql_to_df
    df = sql_to_df(
        "SELECT symbol, date, subject FROM nseindia_corporate_actions_bc_raw WHERE subject IS NOT NULL"
    )
    if df.empty:
        return {}
    dates = pd.to_datetime(df["date"], utc=True, errors="coerce").dt.date
    events_by_key: dict[tuple[str, Any], set[tuple[str, int, int]]] = {}
    for sym, dt, subj in zip(df["symbol"], dates, df["subject"]):
        if pd.isna(dt):
            continue
        events_by_key.setdefault((sym, dt), set()).update(_events_from_subject(subj))
    ratios: dict[tuple[str, Any], float] = {}
    for key, events in events_by_key.items():
        f = _factor_for_events(events)
        if f is not None and f > 0 and abs(f - 1.0) > 0.02:
            ratios[key] = f
    return ratios

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
                 close_col: str = "close", open_col: str = "open",
                 declared_ratios: dict[tuple[str, Any], float] | None = None) -> pd.DataFrame:
    """Return df with `cum_adj_factor`, `adj_close`, and `ca_flag` per row.

    CA detection uses the ex-date OVERNIGHT gap `open[t] / close[t-1]` -- the PURE split/bonus factor, since
    the open trades at the already-adjusted level while the prior close is raw. That is cleaner than a
    close-to-close step, which mixes in the ex-date's intraday move (e.g. IRCTC's 5:1: close/prev=0.221 fails
    to snap, but open/prev=0.198 snaps to 1/5). Falls back to the close step where the open is missing, and
    to close-only if the frame carries no `open` column (backward compatible).

    When `declared_ratios` (from NSE's Bc corporate-action feed, keyed by (symbol, ex-date)) is supplied, a
    circuit-breaching step whose date matches a declared split/bonus uses that EXACT ratio -- fixing the
    cases the price-only snap misses (a 1:1 bonus that also moved a few %, or ratios absent from the round
    set). Only breaching rows consult it, so a declared CA with no price step is never applied (no
    double-adjust). `ca_flag`: 'split_bonus_ca' (declared-confirmed), 'split_bonus' (price-snapped),
    'ambiguous' (breach, neither -> possible data error, NOT adjusted), else ''.
    """
    if df.empty:
        return df.assign(cum_adj_factor=[], adj_close=[], ca_flag=[])
    out = df.sort_values([symbol_col, date_col]).copy()
    prev = out.groupby(symbol_col)[close_col].shift(1).replace(0, np.nan)
    close_ratio = (out[close_col] / prev).to_numpy()
    if open_col in out.columns:
        open_ratio = (out[open_col] / prev).to_numpy()
        valid_open = (out[open_col].to_numpy() > 0) & np.isfinite(open_ratio)
        price_ratio = np.where(valid_open, open_ratio, close_ratio)   # overnight gap, else close step
    else:
        price_ratio = close_ratio
    breach = (price_ratio < CIRCUIT_STEP_LOW) | (price_ratio > CIRCUIT_STEP_HIGH)

    # per-row declared-CA ratio, aligned to `out` (NaN where none) -- vectorized left-merge preserves order
    if declared_ratios:
        keys = pd.DataFrame({
            "__sym": out[symbol_col].to_numpy(),
            "__dt": pd.to_datetime(out[date_col], utc=True).dt.date.to_numpy(),
        })
        dr = pd.DataFrame([(s, d, f) for (s, d), f in declared_ratios.items()],
                          columns=["__sym", "__dt", "__declared"])
        declared_col = keys.merge(dr, on=["__sym", "__dt"], how="left", sort=False)["__declared"].to_numpy()
    else:
        declared_col = np.full(len(out), np.nan)

    event_ratio = np.ones(len(out), dtype="float64")
    ca_flag = np.array([""] * len(out), dtype=object)
    # only circuit-breaching rows can be corporate actions -- resolve just those (fast on the full universe)
    for i in np.flatnonzero(np.nan_to_num(breach)):
        declared = declared_col[i]
        if np.isfinite(declared) and declared > 0:
            event_ratio[i] = declared            # NSE-declared split/bonus -> exact, ground-truth ratio
            ca_flag[i] = "split_bonus_ca"
            continue
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


# ---- adjustment factors (redesigned 2026-08-14): store ONLY the compact per-(symbol,date) factor,
# never a second, duplicated "adjusted price" table. advisory_adjusted_ohlcv_daily is a VIEW joining
# nseindia_ohlcv (raw) x this table on read -- one source for a symbol with all adjustments already
# applied, instead of a separately-written price series that can drift stale. Replaces the old
# advisory_adjusted_ohlcv_daily TABLE (same name, now a view) and supersedes the never-fully-wired
# nseindia_ohlcv_adjusted table entirely -- its total-return columns are folded in here instead,
# sourced from events_dividend (the existing structured dividend-events table) rather than re-parsing
# corporate-action subject text a second time.

ADJUSTMENT_FACTORS_TABLE = "nseindia_adjustment_factors"
ADJUSTED_VIEW = "advisory_adjusted_ohlcv_daily"
FACTORS_MIGRATION_ID = "20260814_nseindia_adjustment_factors"
VIEW_MIGRATION_ID = "20260814_advisory_adjusted_ohlcv_daily_view"

FACTORS_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {ADJUSTMENT_FACTORS_TABLE} (
        symbol TEXT NOT NULL,
        date TIMESTAMPTZ NOT NULL,
        cum_price_adjustment_factor DOUBLE PRECISION,
        ca_flag TEXT,
        cum_total_return_factor DOUBLE PRECISION,
        load_ts TIMESTAMPTZ,
        UNIQUE (symbol, date)
    )
    """,
]

# CREATE OR REPLACE VIEW is safe/idempotent -- but only once the OLD advisory_adjusted_ohlcv_daily
# TABLE is gone (a view can't replace a table of the same name). That drop is a one-time, manual,
# reviewed step (see the 2026-08-14 cutover), never baked into this idempotent migration.
VIEW_SCHEMA_STATEMENTS = [
    f"""
    CREATE OR REPLACE VIEW {ADJUSTED_VIEW} AS
    SELECT
        o.symbol,
        o.date,
        o.series,
        o.close,
        o.close * f.cum_price_adjustment_factor AS adj_close,
        o.open * f.cum_price_adjustment_factor AS adj_open,
        o.high * f.cum_price_adjustment_factor AS adj_high,
        o.low * f.cum_price_adjustment_factor AS adj_low,
        o.volume / NULLIF(f.cum_price_adjustment_factor, 0) AS adj_volume,
        o.close * f.cum_total_return_factor AS tr_adj_close,
        o.open * f.cum_total_return_factor AS tr_adj_open,
        o.high * f.cum_total_return_factor AS tr_adj_high,
        o.low * f.cum_total_return_factor AS tr_adj_low,
        f.cum_price_adjustment_factor AS cum_adj_factor,
        f.cum_total_return_factor,
        f.ca_flag,
        f.load_ts
    FROM nseindia_ohlcv o
    JOIN {ADJUSTMENT_FACTORS_TABLE} f ON f.symbol = o.symbol AND f.date = o.date
    WHERE o.series IN ('EQ', 'BE')
    """,
]


def ensure_factors_table() -> None:
    from utils.schema_migrations import apply_schema_migration
    apply_schema_migration(
        migration_id=FACTORS_MIGRATION_ID,
        statements=FACTORS_SCHEMA_STATEMENTS,
        owner="data.nseindia.price_adjustment",
        description="Compact per-(symbol,date) adjustment factors -- split/bonus (price-step-derived) and total-return (dividend-derived).",
        metadata={"tables": [ADJUSTMENT_FACTORS_TABLE], "workflow": "price_adjustment"},
    )


def ensure_view() -> None:
    from utils.schema_migrations import apply_schema_migration
    apply_schema_migration(
        migration_id=VIEW_MIGRATION_ID,
        statements=VIEW_SCHEMA_STATEMENTS,
        owner="data.nseindia.price_adjustment",
        description="advisory_adjusted_ohlcv_daily as a view over nseindia_ohlcv x nseindia_adjustment_factors (no longer a written table).",
        metadata={"tables": [ADJUSTED_VIEW], "workflow": "price_adjustment"},
    )


def compute_total_return_factor(prices: pd.DataFrame, dividends: pd.DataFrame, *,
                                 symbol_col: str = "symbol", date_col: str = "date",
                                 previous_close_col: str = "previous_close") -> pd.Series:
    """Cumulative total-return (dividend-reinvested) factor per row of `prices`, aligned to its index.

    Daily TR factor on an ex-dividend date is (prev_close - dividend_amount) / prev_close; 1.0 on every
    other date. cum_total_return_factor[t] = product of daily factors STRICTLY AFTER t (per symbol), same
    convention as adjust_frame's cum_adj_factor -- so a dividend back-adjusts only its pre-ex-date history.
    Sourced from events_dividend (the existing structured dividend-events table), not re-parsed CA text.
    """
    if prices.empty:
        return pd.Series(dtype="float64", index=prices.index)
    if dividends.empty:
        return pd.Series(1.0, index=prices.index)

    div = dividends[["symbol", "ex_date", "dividend_amount"]].dropna(subset=["dividend_amount"]).copy()
    div["ex_date"] = pd.to_datetime(div["ex_date"], utc=True, errors="coerce").dt.normalize()
    div = div.groupby(["symbol", "ex_date"], as_index=False)["dividend_amount"].sum()  # multiple same-day entries -> one combined event

    work = prices[[symbol_col, date_col, previous_close_col]].copy()
    work["__date_norm"] = pd.to_datetime(work[date_col], utc=True, errors="coerce").dt.normalize()
    merged = work.merge(
        div.rename(columns={"symbol": symbol_col, "ex_date": "__date_norm"}),
        on=[symbol_col, "__date_norm"],
        how="left",
    )
    valid = merged["dividend_amount"].notna() & merged[previous_close_col].notna() & (merged[previous_close_col] > 0)
    daily_factor = pd.Series(1.0, index=merged.index)
    daily_factor.loc[valid] = (
        (merged.loc[valid, previous_close_col] - merged.loc[valid, "dividend_amount"]) / merged.loc[valid, previous_close_col]
    ).clip(lower=0.0)

    ordered = pd.DataFrame({symbol_col: prices[symbol_col].to_numpy(), date_col: prices[date_col].to_numpy(),
                            "_daily_factor": daily_factor.to_numpy()}, index=prices.index)
    ordered = ordered.sort_values([symbol_col, date_col])
    desc = ordered.sort_values([symbol_col, date_col], ascending=[True, False])
    cum_incl_self = desc.groupby(symbol_col)["_daily_factor"].cumprod()
    desc = desc.assign(cum_total_return_factor=cum_incl_self / desc["_daily_factor"])
    return desc.sort_index()["cum_total_return_factor"]


def build_adjustment_factors(*, dry_run: bool = False) -> dict[str, Any]:
    """Populate nseindia_adjustment_factors for the whole EQ+BE universe (EQ+BE treated as one series per
    symbol, so a T2T migration stays continuous). Split/bonus factor: price-step detection (adjust_frame,
    unchanged). Total-return factor: events_dividend-derived (compute_total_return_factor). Returns a summary."""
    from utils.db import sql_to_df, upsert_to_db
    raw = sql_to_df(
        "SELECT symbol, date, series, open, close, previous_close FROM nseindia_ohlcv WHERE series IN ('EQ','BE') "
        "ORDER BY symbol, date"
    )
    if raw.empty:
        return {"rows": 0}
    raw["date"] = pd.to_datetime(raw["date"], utc=True, errors="coerce")
    declared_ratios = load_declared_ca_ratios()  # NSE Bc feed confirms/supplies ratios the price snap misses
    adj = adjust_frame(raw, declared_ratios=declared_ratios)  # price-step + declared-CA back-adjustment

    dividends = sql_to_df("SELECT symbol, ex_date, dividend_amount FROM events_dividend")
    adj["cum_total_return_factor"] = compute_total_return_factor(adj, dividends).to_numpy()

    now = pd.Timestamp.utcnow()
    out = adj[["symbol", "date", "cum_adj_factor", "ca_flag", "cum_total_return_factor"]].rename(
        columns={"cum_adj_factor": "cum_price_adjustment_factor"}
    ).copy()
    out["load_ts"] = now
    summary = {
        "rows": int(len(out)),
        "symbols": int(out["symbol"].nunique()),
        "declared_ca_ratios_loaded": int(len(declared_ratios)),
        "dividend_events_loaded": int(len(dividends)),
        "split_bonus_events": int((adj["ca_flag"] == "split_bonus").sum()),
        "ca_confirmed_events": int((adj["ca_flag"] == "split_bonus_ca").sum()),
        "ambiguous_flags": int((adj["ca_flag"] == "ambiguous").sum()),
        "price_adjusted_rows": int((out["cum_price_adjustment_factor"] != 1.0).sum()),
        "total_return_adjusted_rows": int((out["cum_total_return_factor"] != 1.0).sum()),
    }
    if not dry_run:
        ensure_factors_table()
        upsert_to_db(out, ADJUSTMENT_FACTORS_TABLE, unique_keys=["symbol", "date"], timescaledb_column="date")
        ensure_view()  # CREATE OR REPLACE is cheap/idempotent -- self-heals a fresh DB or an accidental drop
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build split/bonus + total-return adjustment factors from price steps and dividend events (complete, self-contained).")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(build_adjustment_factors(dry_run=bool(args.dry_run)), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
