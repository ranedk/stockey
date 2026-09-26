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
#
# BUG FOUND LIVE 2026-08-19 (re-audit): both regexes required at least one separator character between
# the "BON"/"SPLIT" keyword (bonus) or between the first number and the literal "TO" (split) -- but NSE
# frequently writes these with NO separator at all: "BON-1:25" (hyphen, no space, for bonus) and
# "10TORS.2" / "10TORE 1" (split, the digit runs straight into "TO" with zero characters between them).
# Confirmed live: BAJFINANCE 2016-09-08's subject "BON 1:1/SPLIT RS.10TORS.2" (combined bonus+split) had
# its split half silently dropped -- declared_ratios supplied only the bonus factor (0.5) instead of the
# true combined ~0.1, and adjust_frame() applied that wrong-but-plausible-looking ratio with full
# confidence (ca_flag='split_bonus_ca'), corrupting the symbol's entire pre-event history in
# advisory_adjusted_ohlcv_daily (systrader's PRIMARY series) by ~5x. WELSPUNIND 2016-03-21's
# "DIV-RS6/SPLIT RS 10TORE 1" matched NEITHER regex at all, routing the date to declared_non_split_dates
# and leaving a real 10:1 split completely unadjusted. \D+? (one-or-more) loosened to \D*? (zero-or-more)
# for the split's digit-to-"TO" gap; bonus's separator widened to [.\s-]* (zero-or-more of period/
# whitespace/hyphen) to admit the hyphenated form.
_BONUS_RE = re.compile(r"BON(?:US)?[.\s-]*(\d+)\s*:\s*(\d+)")
_SPLIT_RE = re.compile(r"(?:F\.?\s*V\.?\s*)?(?:SPLI?T|SUB[\s-]*DIV)\D*?(\d+)\D*?TO\D*?(\d+)")


def _events_from_subject(subject: str) -> set[tuple[str, int, int]]:
    """Parse the canonical (kind, a, b) split/bonus events out of one Bc `subject` string. Splits on
    '/', ';', '|' so a combined 'BONUS1:1/FVSPLIT 10 TO 5' yields both events.

    BUG FOUND LIVE 2026-08-19 (re-audit): a bare '/' was always a split point -- but NSE also uses '/'
    inside its "Rs.X/-" face-value notation (meaning "Rupees X only"), which appears constantly in plain,
    single-event split subjects like "FV SPLIT RS.5/- TO RS.2/-" (ASTRAL 2013-09-05) or "FV SPLIT RS
    5/- TO RE 1/-" (HAVELLS 2014-08-26). Splitting blindly on every '/' shredded these into fragments
    ("FV SPLIT RS.5", "- TO RS.2", "-") none of which contain a complete digit-TO-digit match, even though
    the WHOLE, unsplit subject matches _SPLIT_RE cleanly. A '/' immediately followed by '-' is the "/-"
    face-value marker, not an event separator -- excluded from the split via a negative lookahead so it
    stays attached to its own event's text; a genuine combined-event '/' (not followed by '-') still
    splits normally."""
    events: set[tuple[str, int, int]] = set()
    for part in re.split(r"/(?!-)|[;|]", str(subject).upper()):
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


def load_declared_ca_ratios() -> tuple[dict[tuple[str, Any], float], set[tuple[str, Any]]]:
    """Map (symbol, ex-date) -> declared split/bonus price factor from nseindia_corporate_actions_bc_raw,
    plus the set of (symbol, ex-date) that had a declared corporate action but NOT a split/bonus one
    (demerger, rights issue, scheme of arrangement, ...). Duplicate declarations (same event listed per
    series) dedupe; genuinely distinct same-day events (a bonus AND a split) multiply. Only ratios that
    move the price >2% are kept (a real CA).

    The second set exists because a demerger, rights issue, or scheme of arrangement can ALSO produce a
    price step that happens to snap to a round ratio by coincidence (confirmed live: TATAMOTORS, SIEMENS,
    RAYMOND, IDFC and 17 more demergers, 9 rights issues, 11 schemes of arrangement were all found
    misclassified as split_bonus by the price-snap heuristic alone, in the PRIMARY series). A declared
    non-split reason for a circuit-breaching date is stronger evidence than a coincidental round ratio --
    adjust_frame() must not let the price-snap fallback override it.

    A (symbol, ex-date) can carry MULTIPLE distinct subjects (confirmed live: AHLEAST 2022-10-06 declared
    both 'DEMERGER' and 'BONUS 1:2' the same day, across its EQ and BE series rows). Unioning every
    subject's events at that key -- the pre-2026-08-14 behavior -- let the bonus event silently win: the
    date got the bonus-only factor (0.667) even though the real overnight step (0.528) also carried the
    demerger's value carve-out, permanently under-adjusting the symbol's pre-event history. A key is now
    tracked as "mixed" (has a genuine split/bonus event AND a subject that parsed to none) and routed to
    non_split_dates instead of ratios -- adjust_frame() then flags it 'declared_non_split_ca' (unadjusted,
    for manual review) rather than confidently applying a ratio that ignores the other action."""
    from utils.db import sql_to_df
    df = sql_to_df(
        "SELECT symbol, date, subject FROM nseindia_corporate_actions_bc_raw WHERE subject IS NOT NULL"
    )
    if df.empty:
        return {}, set()
    dates = pd.to_datetime(df["date"], utc=True, errors="coerce").dt.date
    events_by_key: dict[tuple[str, Any], set[tuple[str, int, int]]] = {}
    has_non_split_subject: dict[tuple[str, Any], bool] = {}
    for sym, dt, subj in zip(df["symbol"], dates, df["subject"]):
        if pd.isna(dt):
            continue
        key = (sym, dt)
        subject_events = _events_from_subject(subj)
        events_by_key.setdefault(key, set()).update(subject_events)
        if not subject_events:
            has_non_split_subject[key] = True  # this subject row carries no split/bonus event of its own
    ratios: dict[tuple[str, Any], float] = {}
    non_split_dates: set[tuple[str, Any]] = set()
    for key, events in events_by_key.items():
        f = _factor_for_events(events)
        if f is not None and f > 0 and abs(f - 1.0) > 0.02 and not has_non_split_subject.get(key, False):
            ratios[key] = f
        else:
            # either no split/bonus event was declared for this date at all, or one WAS declared but a
            # separate non-split subject was ALSO declared for the same date (mixed -- see docstring)
            non_split_dates.add(key)
    return ratios, non_split_dates

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


# BUG FOUND LIVE 2026-08-19 (re-audit): declared_ratios used to be trusted unconditionally -- any parse
# gap in _BONUS_RE/_SPLIT_RE (a future NSE wording change, a genuinely novel combined-event format the
# regex loosening above doesn't happen to cover) would silently produce a wrong-but-plausible-looking
# factor and apply it with full confidence (ca_flag='split_bonus_ca'), same failure shape as the
# BAJFINANCE case the regex fix above closes. This is looser than _SNAP_TOL (6%) deliberately -- the
# whole reason the declared path exists is to handle real CAs whose price step does NOT hit a clean round
# number (a bonus whose ex-date also moved a genuine few % intraday, see adjust_frame's own docstring),
# so it must not reject those. It exists only to catch a declared ratio that is off by a WHOLE separate
# missed event's worth of magnitude, not to second-guess a normal declared CA.
_DECLARED_RATIO_TOL = 0.15


def _declared_ratio_plausible(declared: float, price_ratio: float) -> bool:
    """Does a declared split/bonus ratio roughly explain the actual observed price step? Compared on the
    same >=1 "implied" scale _snap_event_ratio uses so direction (split vs reverse-split) doesn't matter."""
    if not np.isfinite(price_ratio) or price_ratio <= 0 or not np.isfinite(declared) or declared <= 0:
        return False
    declared_implied = declared if declared > 1.0 else 1.0 / declared
    price_implied = price_ratio if price_ratio > 1.0 else 1.0 / price_ratio
    return abs(declared_implied - price_implied) / price_implied <= _DECLARED_RATIO_TOL


def adjust_frame(df: pd.DataFrame, *, symbol_col: str = "symbol", date_col: str = "date",
                 close_col: str = "close", open_col: str = "open",
                 declared_ratios: dict[tuple[str, Any], float] | None = None,
                 declared_non_split_dates: set[tuple[str, Any]] | None = None) -> pd.DataFrame:
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
    double-adjust).

    `declared_non_split_dates` (also from the Bc feed) is the opposite guard: a circuit-breaching step whose
    date has a declared corporate action that is NOT a split/bonus (demerger, rights issue, scheme of
    arrangement, ...) must not be guessed at by the round-ratio snap just because the ratio happens to look
    clean -- confirmed live: TATAMOTORS/SIEMENS/RAYMOND/IDFC and 17 more demergers, 9 rights issues, and 11
    schemes of arrangement were all being misclassified as split_bonus this way. Checked AFTER
    `declared_ratios` (a declared split/bonus always wins) and BEFORE the price-snap fallback.

    `ca_flag`: 'split_bonus_ca' (declared-confirmed split/bonus -- the declared ratio also plausibly
    explains the observed price step, see _declared_ratio_plausible), 'split_bonus' (price-snapped, no
    declared CA either way), 'declared_non_split_ca' (breach, but NSE declared a different, non-split
    reason -- NOT adjusted), 'declared_ca_mismatch' (breach, a declared ratio existed but didn't plausibly
    explain the actual step and the step doesn't snap either -- likely a parse gap on a combined-event
    subject, NOT adjusted, flagged for review), 'ambiguous' (breach, no declared CA and no round-ratio
    match -> possible data error, NOT adjusted), else ''.
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

    row_keys = list(zip(out[symbol_col].to_numpy(), pd.to_datetime(out[date_col], utc=True).dt.date.to_numpy()))

    # per-row declared-CA ratio, aligned to `out` (NaN where none) -- vectorized left-merge preserves order
    if declared_ratios:
        keys = pd.DataFrame({"__sym": [k[0] for k in row_keys], "__dt": [k[1] for k in row_keys]})
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
        has_declared = np.isfinite(declared) and declared > 0
        if has_declared and _declared_ratio_plausible(float(declared), float(price_ratio[i])):
            event_ratio[i] = declared            # NSE-declared split/bonus -> exact, ground-truth ratio
            ca_flag[i] = "split_bonus_ca"
            continue
        if declared_non_split_dates and row_keys[i] in declared_non_split_dates:
            ca_flag[i] = "declared_non_split_ca"  # NSE declared a non-split reason -- do not guess "split"
            continue
        snapped = _snap_event_ratio(float(price_ratio[i]))
        if snapped is None:
            # a declared ratio existed but didn't plausibly explain the actual price step (and the step
            # doesn't snap cleanly either) -- likely a parse gap on a combined-event subject, not a clean
            # miss. Flagged distinctly from plain 'ambiguous' so it's easy to find for manual review,
            # rather than silently applying a ratio that only explains part of the real move.
            ca_flag[i] = "declared_ca_mismatch" if has_declared else "ambiguous"
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
# BUG FOUND LIVE 2026-08-19 (re-audit): apply_schema_migration() checksums its statements and refuses to
# silently re-apply a migration_id whose recorded checksum no longer matches (a deliberate anti-drift
# guard) -- so composing tr_adj_* with cum_price_adjustment_factor (see VIEW_SCHEMA_STATEMENTS below)
# needed a new migration_id, not a mutation of the 2026-08-14 one. Confirmed live: reusing the old ID
# raised ValueError("Migration checksum mismatch...") exactly as designed.
VIEW_MIGRATION_ID = "20260924_advisory_adjusted_ohlcv_daily_view_sme_series"

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
        -- BUG FOUND LIVE 2026-08-19 (re-audit): these used to multiply raw close/open/high/low by
        -- cum_total_return_factor ALONE -- cum_total_return_factor is purely dividend-derived (see
        -- compute_total_return_factor) and knows nothing about splits/bonuses, so any split/bonus event
        -- produced a fake discontinuity in the total-return columns at the exact same magnitude as the
        -- (correctly-adjusted-elsewhere) split ratio. Confirmed live on IRCTC's 2021-10-28 5:1 split:
        -- adj_close stayed correctly smooth (826.03 -> 913.50) while tr_adj_close faked a ~4.5x "crash"
        -- (3950.82 -> 873.84) on the same date. Composed with cum_price_adjustment_factor, same as
        -- adj_close/adj_open/etc. already are, so both corrections stack instead of only one applying.
        o.close * f.cum_price_adjustment_factor * f.cum_total_return_factor AS tr_adj_close,
        o.open * f.cum_price_adjustment_factor * f.cum_total_return_factor AS tr_adj_open,
        o.high * f.cum_price_adjustment_factor * f.cum_total_return_factor AS tr_adj_high,
        o.low * f.cum_price_adjustment_factor * f.cum_total_return_factor AS tr_adj_low,
        f.cum_price_adjustment_factor AS cum_adj_factor,
        f.cum_total_return_factor,
        f.ca_flag,
        f.load_ts
    FROM nseindia_ohlcv o
    JOIN {ADJUSTMENT_FACTORS_TABLE} f ON f.symbol = o.symbol AND f.date = o.date
    -- SM/ST (NSE SME) added 2026-09-24: SME stocks had raw bhavcopy prices but no adjusted
    -- series at all, so no stage read and nothing downstream (stockey's watchlist holds SME
    -- names -- Happy Steels, Mos Utility, TechEra). systrader's backtests filter series='EQ'.
    WHERE o.series IN ('EQ', 'BE', 'SM', 'ST')
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
    # BUG FOUND LIVE 2026-08-18 (re-audit): "self-heals an accidental drop" is false
    # once the migration is recorded -- apply_schema_migration() skips re-executing
    # its statements entirely once migration_id is marked 'applied', regardless of
    # whether the VIEW itself still exists. advisory_adjusted_ohlcv_daily is
    # systrader's own PRIMARY series (see CLAUDE.md), so this is the highest-traffic
    # place this gap could bite. CREATE OR REPLACE VIEW is itself already idempotent
    # and cheap, so it's run directly here too, unconditionally, every call --
    # apply_schema_migration above still provides the checksum-tracked audit trail,
    # this direct run is what makes ensure_view() ACTUALLY self-heal a drop, not
    # just a fresh DB. Same fix as data/bseindia/price_adjustment.py's own
    # ensure_view, see its comment for the full rationale.
    from utils.db import db_session, execute_db_operation

    def _create_or_replace_view() -> None:
        with db_session() as (_, cur):
            for statement in VIEW_SCHEMA_STATEMENTS:
                cur.execute(statement)

    execute_db_operation(_create_or_replace_view, operation_name=f"{ADJUSTED_VIEW}:ensure_view_direct")


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
    # NSE republishes/reformats the same declared dividend under different subject wording (confirmed live
    # 2026-08-14: 1,764 (symbol, ex_date) groups carry an identical amount under >=2 distinct subject
    # strings, e.g. SYMPHONY 2017-08-23's Rs 1 dividend appears 3x). events_dividend's uniqueness key is
    # (symbol, ex_date, subject), so those wording-variant duplicates survive into this table as separate
    # rows. Dedupe on the (symbol, ex_date, amount) triple BEFORE summing, so a same-day reworded republish
    # collapses to one payout while two genuinely distinct same-day dividends (different amounts) still
    # both count.
    div = div.drop_duplicates(subset=["symbol", "ex_date", "dividend_amount"])
    div = div.groupby(["symbol", "ex_date"], as_index=False)["dividend_amount"].sum()  # distinct same-day amounts -> one combined event

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
    """Populate nseindia_adjustment_factors for the whole EQ+BE+SM+ST universe (treated as one series per
    symbol, so a T2T or SME->main-board migration stays continuous). Split/bonus factor: price-step detection (adjust_frame,
    unchanged). Total-return factor: events_dividend-derived (compute_total_return_factor). Returns a summary."""
    from utils.db import sql_to_df, upsert_to_db
    raw = sql_to_df(
        "SELECT symbol, date, series, open, close, previous_close FROM nseindia_ohlcv WHERE series IN ('EQ','BE','SM','ST') "
        "ORDER BY symbol, date"
    )
    if raw.empty:
        return {"rows": 0}
    raw["date"] = pd.to_datetime(raw["date"], utc=True, errors="coerce")
    # NSE Bc feed confirms/supplies ratios the price snap misses, AND vetoes the price snap where it would
    # otherwise misclassify a demerger/rights issue/scheme of arrangement as a split (see adjust_frame's docstring)
    declared_ratios, declared_non_split_dates = load_declared_ca_ratios()
    adj = adjust_frame(raw, declared_ratios=declared_ratios, declared_non_split_dates=declared_non_split_dates)

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
        "declared_non_split_dates_loaded": int(len(declared_non_split_dates)),
        "dividend_events_loaded": int(len(dividends)),
        "split_bonus_events": int((adj["ca_flag"] == "split_bonus").sum()),
        "ca_confirmed_events": int((adj["ca_flag"] == "split_bonus_ca").sum()),
        "declared_non_split_events": int((adj["ca_flag"] == "declared_non_split_ca").sum()),
        "ambiguous_flags": int((adj["ca_flag"] == "ambiguous").sum()),
        "price_adjusted_rows": int((out["cum_price_adjustment_factor"] != 1.0).sum()),
        "total_return_adjusted_rows": int((out["cum_total_return_factor"] != 1.0).sum()),
    }
    if not dry_run:
        ensure_factors_table()
        # This rebuilds the FULL history every night, and load_ts is stamped fresh on
        # every row -- so a plain DO UPDATE rewrote all 5.86M rows across 688 chunks
        # nightly (~2-4 GB WAL) for factors that almost never move. update_if_changed
        # compares the real columns only (load_ts is excluded by default) and writes
        # just the rows whose factor actually changed.
        upsert_to_db(
            out,
            ADJUSTMENT_FACTORS_TABLE,
            unique_keys=["symbol", "date"],
            timescaledb_column="date",
            on_conflict="update_if_changed",
        )
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
