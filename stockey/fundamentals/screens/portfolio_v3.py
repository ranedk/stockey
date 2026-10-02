"""Ruleset v3 -- the portfolio on the story score (docs/FUNDAMENTAL_REEVALUATION_PRD.md 5.2 /
5.3, build step 7; replaces v2 for new entries from go-live, operator 2026-09-30).

ENTRY: an active watchlist name whose live story score is in the band (reeval: enter 80, stay
to 65) with no flaw, at Weinstein stage 2 (systrader's read). The adjudicator may still veto.

SIZE: weight proportional to conviction x risk scaling -- story score / daily volatility --
normalised over the book (held + today's candidates, best TARGET names), clipped to
WEIGHT_MIN..WEIGHT_MAX per name, a sector capped at SECTOR_MAX, then the existing 10%-of-ADV
cap. Weights never sum past 100%; what the caps leave is cash.

REPLACEMENT: when the book is full, an accepted candidate replaces the weakest holding only if
it scores REPLACE_MARGIN points higher and the holding has been held MIN_HOLD_SESSIONS.

EXITS (checked daily; see v3_exit_action):
  stop          the initial vol-scaled stop, or once in profit a TRAILING stop -- highest close
                since entry minus TRAIL_MULTIPLE x the normal 20-session move, only ratcheting
                up; after a gain of BREAKEVEN_AFTER x the initial stop it never sits below
                entry. Unconditional.
  thesis_broken a flaw on the live score, or a hard negative alert since entry. Unconditional.
  story_fading  the daily story score below BAND_EXIT on FADE_RUNS consecutive runs; the
                adjudicator may defer once.
  trim          valuation >= TRIM_VALUATION_RATIO x its own history and the story not
                strengthening since entry: trim a third, once. Never a full exit.
  target_date   as before (adjudicator may defer once).
  TAX GUARD     a non-urgent exit (story_fading, target_date, replacement, trim) on a position
                in profit, within TAX_GUARD_DAYS of its first anniversary, waits until the gain
                is long-term. Stops and a broken thesis never wait.

Every exit and trim gets a counterfactual row: what the stock did over the next 20 / 40 / 60
sessions as if held (`fundamentals_portfolio_counterfactual`), so each rule's worth is measured.
Trims and rebalance signals are records in `fundamentals_portfolio_adjustment`; the book is
record-only, so a "trade" here is a record, and position_size_rs stays the entry size.

All numbers below are the PRD's defaults, fixed with the version; a change is a new version.
"""
from __future__ import annotations

import math

import pandas as pd

from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db

RULESET_V3 = 3
WEIGHT_MIN = 0.02
WEIGHT_MAX = 0.08
SECTOR_MAX = 0.25
REPLACE_MARGIN = 15.0
MIN_HOLD_SESSIONS = 20
BAND_EXIT = 65.0
FADE_RUNS = 3
TRAIL_MULTIPLE = 3.0
TRAIL_PCT_MIN = 15.0
TRAIL_PCT_MAX = 35.0
BREAKEVEN_AFTER = 2.0
TRIM_VALUATION_RATIO = 2.0
TRIM_FRACTION = 1.0 / 3.0
TAX_GUARD_DAYS = 30
LONG_TERM_DAYS = 365
REBALANCE_BAND = 0.5          # act only when a position is 50% away from its target value
COUNTERFACTUAL_HORIZONS = (20, 40, 60)
LIVE_MAX_AGE_DAYS = 3
URGENT_REASONS = ("stop_loss", "trailing_stop", "thesis_broken")
HARD_ALERT_COOLDOWN_DAYS = 14  # = portfolio_runner.STOP_COOLDOWN_DAYS's default
# the hard contradictions of portfolio_exit (kept in step by a test)
HARD_NEGATIVE_TRIGGERS = ("results_decline", "rating_downgrade", "pledge_increase", "auditor_change",
                          "insider_sell_surprise")

ADJUSTMENT_TABLE = "fundamentals_portfolio_adjustment"
COUNTERFACTUAL_TABLE = "fundamentals_portfolio_counterfactual"

_DDL = [
    f"""CREATE TABLE IF NOT EXISTS {ADJUSTMENT_TABLE} (
        position_id TEXT NOT NULL,
        adjusted_at TIMESTAMPTZ NOT NULL,
        kind TEXT NOT NULL,
        ticker TEXT,
        price DOUBLE PRECISION,
        value_before_rs DOUBLE PRECISION,
        value_after_rs DOUBLE PRECISION,
        target_value_rs DOUBLE PRECISION,
        reason TEXT,
        PRIMARY KEY (position_id, adjusted_at, kind)
    )""",
    f"""CREATE TABLE IF NOT EXISTS {COUNTERFACTUAL_TABLE} (
        position_id TEXT NOT NULL,
        event TEXT NOT NULL,
        event_at TIMESTAMPTZ NOT NULL,
        ticker TEXT,
        event_date DATE,
        price_at_event DOUBLE PRECISION,
        ret_20 DOUBLE PRECISION,
        ret_40 DOUBLE PRECISION,
        ret_60 DOUBLE PRECISION,
        PRIMARY KEY (position_id, event, event_at)
    )""",
]
# The position columns v3 writes are added by portfolio_adjudicator._ensure_tables with the rest.


def ensure_tables() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            for statement in _DDL:
                cur.execute(statement)

    execute_db_operation(_op, operation_name="portfolio_v3:ensure_tables")


# --- entry ------------------------------------------------------------------------------------

def load_candidates() -> pd.DataFrame:
    """Active watchlist names in the story band with no flaw, fresh live score."""
    return sql_to_df(
        """
        SELECT w.company_master_id, w.narrative_text, w.first_seen_price, w.first_seen_at,
               l.story_score, l.primary_dimension, l.score_version, l.scored_at,
               cs.sector_code,
               r.story AS read_story, r.deal_breaker AS read_deal_breaker, r.direction AS read_direction,
               r.materiality AS read_materiality, r.read_at
          FROM fundamentals_watchlist w
          JOIN fundamentals_story_score_live l USING (company_master_id)
          LEFT JOIN fundamentals_company_sector cs USING (company_master_id)
          LEFT JOIN LATERAL (SELECT story, deal_breaker, direction, materiality, read_at
                               FROM fundamentals_story_read sr
                              WHERE sr.company_master_id = w.company_master_id
                                AND sr.read_at >= now() - interval '30 days'
                              ORDER BY read_at DESC LIMIT 1) r ON TRUE
         WHERE w.status = 'active' AND l.in_band AND coalesce(l.flaws, '') = ''
           AND l.scored_at >= now() - make_interval(days => %s)
           -- a hard negative alert in the cooldown window blocks a NEW entry, as it would force an exit
           AND NOT EXISTS (SELECT 1 FROM fundamentals_l3_alerts a
                            WHERE a.company_master_id = w.company_master_id AND a.trigger_type = ANY(%s)
                              AND a.load_ts >= now() - make_interval(days => %s))
        """, params=(LIVE_MAX_AGE_DAYS, list(HARD_NEGATIVE_TRIGGERS), HARD_ALERT_COOLDOWN_DAYS))


def evaluate_entry_candidates() -> dict[str, object]:
    """Same contract as portfolio_ruleset.evaluate_entry_candidates, for v3."""
    from fundamentals.screens.portfolio_adjudicator import numeric_l2_metrics
    from fundamentals.screens.portfolio_ruleset import (
        ENTRY_STAGE, compute_stop_pct, load_stage_keys, load_stage_reads, stage_for,
    )

    ensure_tables()
    rows = load_candidates()
    if rows.empty:
        return {"ruleset_version": RULESET_V3, "evaluated": 0, "candidates": [], "stage_reads": 0,
                "stage_api_available": True}
    stages = load_stage_reads()
    stage_keys = load_stage_keys(rows["company_master_id"].astype(str).tolist())
    candidates = []
    for r in rows.itertuples():
        stage = stage_for(r.company_master_id, stages, stage_keys)
        checks = {"in_story_band": True, "no_flaw": True, "stage_is_advancing": stage == ENTRY_STAGE}
        if not all(checks.values()):
            continue
        candidates.append({
            "company_master_id": r.company_master_id,
            "ticker": str(r.company_master_id).replace("nse:", ""),
            "stage": stage,
            "story_score": float(r.story_score),
            "primary_dimension": r.primary_dimension,
            "sector_code": r.sector_code,
            "story_read": None if pd.isna(r.read_at) else {
                "story": r.read_story, "deal_breaker": r.read_deal_breaker, "direction": r.read_direction,
                "materiality": None if pd.isna(r.read_materiality) else int(r.read_materiality)},
            "narrative_text": r.narrative_text,
            "first_seen_price": r.first_seen_price,
            "first_seen_at": r.first_seen_at,
            "score_version": int(r.score_version),
            # v2's confluence fields do not exist for v3; carried as None so shared code is explicit
            "confluence_count": None, "contradicting_count": None, "evaluable_count": None, "axes": None,
            "checks": checks,
        })
    stops = compute_stop_pct([c["ticker"] for c in candidates])
    metrics = numeric_l2_metrics() if candidates else []
    for c in candidates:
        c["stop"] = stops.get(c["ticker"])
        c["daily_vol_pct"] = (c["stop"] or {}).get("daily_vol_pct")
        c["available_l2_metrics"] = metrics
    return {"ruleset_version": RULESET_V3, "evaluated": int(len(rows)), "stage_reads": len(stages),
            "stage_api_available": bool(stages), "stale_confluence_skipped": [], "candidates": candidates}


# --- sizing -----------------------------------------------------------------------------------

def target_weights(book: list[dict], target_positions: int) -> dict[str, float]:
    """company_master_id -> weight. book: dicts with company_master_id, story_score,
    daily_vol_pct (may be None), sector_code. The best `target_positions` by score count."""
    if not book:
        return {}
    names = sorted(book, key=lambda b: -(b.get("story_score") or 0))[:max(target_positions, 1)]
    vols = [float(b["daily_vol_pct"]) for b in names if b.get("daily_vol_pct")]
    fallback_vol = sorted(vols)[len(vols) // 2] if vols else 2.0
    raw = {b["company_master_id"]: max(float(b.get("story_score") or 0), 0.0)
           / max(float(b.get("daily_vol_pct") or fallback_vol), 0.25) for b in names}
    mean_raw = sum(raw.values()) / len(raw) if raw else 0.0
    if mean_raw <= 0:
        return {k: WEIGHT_MIN for k in raw}
    # an average name gets 1/target of the book; conviction and calm tilt it within the caps
    w = {k: min(max(v / mean_raw / target_positions, WEIGHT_MIN), WEIGHT_MAX) for k, v in raw.items()}
    sector = {b["company_master_id"]: b.get("sector_code") or "unknown" for b in names}
    for code in set(sector.values()):
        members = [k for k in w if sector[k] == code]
        total = sum(w[k] for k in members)
        if total > SECTOR_MAX:
            for k in members:
                w[k] *= SECTOR_MAX / total
    total = sum(w.values())
    if total > 1.0:
        w = {k: v / total for k, v in w.items()}
    return {k: round(v, 5) for k, v in w.items()}


def open_v3_book() -> pd.DataFrame:
    """Open ACCEPTED v3 positions with their live score (for weights and replacement)."""
    ensure_tables()
    return sql_to_df(
        """
        SELECT p.position_id, p.company_master_id, p.ticker, p.opened_at, p.entry_price,
               p.position_size_rs, p.target_weight, p.sector_code, p.daily_vol_pct,
               p.story_score_at_entry, p.trim_count, l.story_score
          FROM fundamentals_portfolio_position p
          LEFT JOIN fundamentals_story_score_live l USING (company_master_id)
         WHERE p.status = 'open' AND p.entry_decision = 'accept' AND p.ruleset_version >= %s
        """, params=(RULESET_V3,))


def weakest_replaceable(book: pd.DataFrame, candidate_score: float, today) -> dict | None:
    """The weakest holding the candidate may replace, or None: it must score REPLACE_MARGIN
    higher, and the holding must be past MIN_HOLD_SESSIONS and not waiting on the tax guard."""
    from fundamentals.screens.portfolio_exit import sessions_held

    if book.empty:
        return None
    ranked = book.assign(score=book["story_score"].fillna(-1)).sort_values("score")
    for r in ranked.itertuples():
        if candidate_score < r.score + REPLACE_MARGIN:
            return None  # sorted ascending: nobody weaker is left
        if sessions_held(r.opened_at, today) < MIN_HOLD_SESSIONS:
            continue
        if tax_guard_holds(r.opened_at, r.entry_price, None, today):
            continue
        return r._asdict()
    return None


# --- exits ------------------------------------------------------------------------------------

def trail_pct(daily_vol_pct) -> float:
    """TRAIL_MULTIPLE x the normal 20-session move (daily sigma x sqrt(20)), clamped."""
    if daily_vol_pct is None or pd.isna(daily_vol_pct) or float(daily_vol_pct) <= 0:
        return (TRAIL_PCT_MIN + TRAIL_PCT_MAX) / 2
    return min(max(TRAIL_MULTIPLE * float(daily_vol_pct) * math.sqrt(20), TRAIL_PCT_MIN), TRAIL_PCT_MAX)


def stop_levels(entry_price: float, stop_pct: float | None, trail_high: float | None, daily_vol_pct) -> dict:
    """{'initial': level, 'trailing': level or None, 'effective': max, 'binding': reason}."""
    initial = entry_price * (1 - float(stop_pct) / 100.0) if stop_pct else None
    trailing = None
    if trail_high is not None and trail_high > entry_price:
        trailing = trail_high * (1 - trail_pct(daily_vol_pct) / 100.0)
        if stop_pct and trail_high >= entry_price * (1 + BREAKEVEN_AFTER * float(stop_pct) / 100.0):
            trailing = max(trailing, entry_price)
    levels = [x for x in (initial, trailing) if x is not None]
    effective = max(levels) if levels else None
    binding = None if effective is None else ("trailing_stop" if trailing is not None and effective == trailing
                                              and (initial is None or trailing > initial) else "stop_loss")
    return {"initial": initial, "trailing": trailing, "effective": effective, "binding": binding}


def days_held(opened_at, today) -> int:
    opened = pd.Timestamp(opened_at)
    opened = opened.tz_convert(None) if opened.tzinfo else opened
    return int((pd.Timestamp(today) - opened.normalize()).days)


def tax_guard_holds(opened_at, entry_price, price, today) -> bool:
    """A non-urgent exit waits while held in the last TAX_GUARD_DAYS before the first
    anniversary with a gain (a loss gains nothing by waiting). Unknown price -> assume a gain."""
    held = days_held(opened_at, today)
    # long-term needs MORE than 12 months: a sale on the anniversary itself is still short-term
    if not (LONG_TERM_DAYS - TAX_GUARD_DAYS <= held <= LONG_TERM_DAYS):
        return False
    if price is None or entry_price is None or pd.isna(entry_price):
        return True
    return float(price) > float(entry_price)


def fading(recent_scores: list[float]) -> bool:
    return len(recent_scores) >= FADE_RUNS and all(s < BAND_EXIT for s in recent_scores[:FADE_RUNS])


def v3_exit_action(p, *, price, trail_high, live: dict | None, hard_events: list[dict],
                   recent_scores: list[float], valuation_ratio, today, entry_adjusted=None) -> dict | None:
    """One v3 position's action today: {'action': 'exit'|'trim'|'wait', 'reason', 'detail'}
    or None. `p` needs entry_price, stop_pct, opened_at, daily_vol_pct, story_score_at_entry,
    trim_count, target_date."""
    # entry on today's adjusted basis when known (a split since entry rescales it), else as stored
    entry = float(entry_adjusted) if entry_adjusted is not None and not pd.isna(entry_adjusted) else (
        float(p.entry_price) if p.entry_price is not None and not pd.isna(p.entry_price) else None)
    if entry and price is not None:
        lv = stop_levels(entry, p.stop_pct, trail_high, p.daily_vol_pct)
        if lv["effective"] is not None and price <= lv["effective"]:
            kind = lv["binding"]
            what = "trailing stop" if kind == "trailing_stop" else "stop"
            return {"action": "exit", "reason": kind,
                    "detail": f"adjusted close {price:.2f} <= {what} {lv['effective']:.2f}"
                              + (f" (high since entry {trail_high:.2f}, trail {trail_pct(p.daily_vol_pct):.1f}%)"
                                 if kind == "trailing_stop" else f" ({p.stop_pct}% below entry {entry:.2f})")}
    flaws = (live or {}).get("flaws")
    if isinstance(flaws, str) and flaws.strip():
        return {"action": "exit", "reason": "thesis_broken", "detail": f"flaw appeared: {flaws}"}
    opened = pd.Timestamp(p.opened_at)
    opened = opened if opened.tzinfo else opened.tz_localize("UTC")
    for ev in hard_events:
        seen = pd.Timestamp(ev["load_ts"])
        seen = seen if seen.tzinfo else seen.tz_localize("UTC")
        if seen > opened:
            return {"action": "exit", "reason": "thesis_broken",
                    "detail": f"hard contradiction since entry: {ev['trigger_type']} (alert {ev['alert_date']})"}

    guarded = tax_guard_holds(p.opened_at, entry, price, today)
    if fading(recent_scores):
        detail = f"story score below {BAND_EXIT:g} on {FADE_RUNS} consecutive runs ({', '.join(f'{s:.0f}' for s in recent_scores[:FADE_RUNS])})"
        return ({"action": "wait", "reason": "story_fading", "detail": f"tax guard: {detail}"} if guarded
                else {"action": "exit", "reason": "story_fading", "detail": detail})
    target = getattr(p, "target_date", None)
    if target is not None and not pd.isna(target) and pd.Timestamp(target).normalize() <= pd.Timestamp(today):
        detail = f"target date {pd.Timestamp(target).date()} has passed without the thesis resolving"
        return ({"action": "wait", "reason": "target_date", "detail": f"tax guard: {detail}"} if guarded
                else {"action": "exit", "reason": "target_date", "detail": detail})
    trims = int(p.trim_count or 0) if getattr(p, "trim_count", None) is not None and not pd.isna(p.trim_count) else 0
    score_now = (live or {}).get("story_score")
    at_entry = p.story_score_at_entry
    if (trims == 0 and valuation_ratio is not None and not pd.isna(valuation_ratio)
            and float(valuation_ratio) >= TRIM_VALUATION_RATIO
            and (score_now is None or at_entry is None or pd.isna(at_entry) or float(score_now) <= float(at_entry))):
        detail = (f"valuation {float(valuation_ratio):.2f}x its own history and the story is not strengthening "
                  f"(score {score_now if score_now is None else round(float(score_now))} vs "
                  f"{at_entry if at_entry is None or pd.isna(at_entry) else round(float(at_entry))} at entry)")
        return ({"action": "wait", "reason": "trim", "detail": f"tax guard: {detail}"} if guarded
                else {"action": "trim", "reason": "valuation_trim", "detail": detail})
    return None


REBALANCE_QUIET_DAYS = 20  # one signal per position per month, not the same drift every night


def rebalance_signals(book: pd.DataFrame, prices: dict[str, float], capital_rs: float, target_positions: int) -> list[dict]:
    """Positions whose value has drifted REBALANCE_BAND away from today's target weight.
    book: open v3 positions (open_v3_book columns + trim_count). Value = entry size x price
    move, less recorded trims."""
    if book.empty:
        return []
    weights = target_weights([{"company_master_id": r.company_master_id, "story_score": r.story_score,
                               "daily_vol_pct": r.daily_vol_pct, "sector_code": r.sector_code}
                              for r in book.itertuples()], target_positions)
    out = []
    for r in book.itertuples():
        price = prices.get(str(r.ticker))
        if price is None or not r.position_size_rs or not r.entry_price:
            continue
        trims = int(getattr(r, "trim_count", 0) or 0) if not pd.isna(getattr(r, "trim_count", 0) or 0) else 0
        value = float(r.position_size_rs) * float(price) / float(r.entry_price) * (1 - TRIM_FRACTION) ** trims
        target = weights.get(r.company_master_id, 0.0) * capital_rs
        if target <= 0:
            continue
        if value > target * (1 + REBALANCE_BAND) or value < target * (1 - REBALANCE_BAND):
            out.append({"position_id": r.position_id, "ticker": str(r.ticker), "price": price,
                        "value_rs": round(value), "target_rs": round(target),
                        "kind": "rebalance_down" if value > target else "rebalance_up"})
    return out


def recent_rebalanced(position_ids: list[str]) -> set[str]:
    if not position_ids:
        return set()
    ensure_tables()
    df = sql_to_df(f"SELECT DISTINCT position_id FROM {ADJUSTMENT_TABLE} WHERE kind LIKE 'rebalance%%' "  # noqa: S608
                   "AND adjusted_at >= now() - make_interval(days => %s) AND position_id = ANY(%s)",
                   params=(REBALANCE_QUIET_DAYS, position_ids))
    return set(df["position_id"]) if not df.empty else set()


# --- records ----------------------------------------------------------------------------------

def record_counterfactual(position_id: str, event: str, ticker: str, price: float | None) -> None:
    ensure_tables()
    now = pd.Timestamp.now(tz="UTC")
    upsert_to_db(pd.DataFrame([{"position_id": position_id, "event": event, "event_at": now, "ticker": ticker,
                                "event_date": (now + pd.Timedelta(hours=5, minutes=30)).date(),
                                "price_at_event": price}]),
                 COUNTERFACTUAL_TABLE, unique_keys=["position_id", "event", "event_at"], on_conflict="nothing")


def record_adjustment(position_id: str, kind: str, ticker: str, price, before, after, target, reason: str) -> None:
    ensure_tables()
    upsert_to_db(pd.DataFrame([{"position_id": position_id, "adjusted_at": pd.Timestamp.now(tz="UTC"), "kind": kind,
                                "ticker": ticker, "price": price, "value_before_rs": before, "value_after_rs": after,
                                "target_value_rs": target, "reason": reason}]),
                 ADJUSTMENT_TABLE, unique_keys=["position_id", "adjusted_at", "kind"])


def fill_counterfactuals() -> int:
    """Fill 20/40/60-session returns (as if held) once each horizon has passed."""
    from fundamentals.screens.event_drift import _forward_return

    ensure_tables()
    pending = sql_to_df(f"SELECT * FROM {COUNTERFACTUAL_TABLE} WHERE ret_60 IS NULL")  # noqa: S608
    filled = 0
    for r in pending.itertuples():
        updates = {}
        for h in COUNTERFACTUAL_HORIZONS:
            if getattr(r, f"ret_{h}") is None or pd.isna(getattr(r, f"ret_{h}")):
                ret = _forward_return(r.ticker, r.event_date, h)
                if ret is not None:
                    updates[f"ret_{h}"] = ret
        if updates:
            sets = ", ".join(f"{k} = %s" for k in updates)

            def _op(sets=sets, vals=tuple(updates.values()), r=r) -> None:
                with db_session() as (_, cur):
                    cur.execute(f"UPDATE {COUNTERFACTUAL_TABLE} SET {sets} "  # noqa: S608
                                "WHERE position_id = %s AND event = %s AND event_at = %s",
                                (*vals, r.position_id, r.event, r.event_at))

            execute_db_operation(_op, operation_name=f"{COUNTERFACTUAL_TABLE}:fill")
            filled += 1
    return filled
