"""Mechanical exit evaluator -- the other half of docs/PORTFOLIO_RULESET_PRD.md.

Like the entry ruleset, this module decides nothing on judgement. It answers one
question per open position: has a NAMED, versioned exit condition tripped? The LLM exit
adjudicator (portfolio_adjudicator.adjudicate_exit) may then defer some of those, and may
never defer a stop.

Three triggers, in strict priority order -- the order matters, because a position can
satisfy several at once and only the first is reported:

  1. stop_loss           entry_price * (1 - stop_pct/100) breached on adjusted close.
                         UNCONDITIONAL: never routed to the adjudicator at all.
  2. thesis_invalidation the entry rule stopped holding -- a contradicting axis appeared,
                         or the L4 thesis's own invalidation_criteria resolved false.
  3. target_date         the target date the entry adjudicator committed to has passed.
                         Staleness, not danger.

The target date is read off the POSITION. It was briefly bound to the human forecast
register (fundamentals_l4_thesis), which held zero rows -- that made this branch
permanently dead while looking implemented. That register was deleted 2026-09-04; the
adjudicator writes a clamped target_date onto every position it accepts instead.

FAIL-CLOSED CUTS THE OPPOSITE WAY HERE, and this is the trap worth naming. On ENTRY, a
missing stage read blocks the entry (safe). On EXIT, treating a missing read as a failed
condition would liquidate the entire book the first time systrader's API is down -- an
infrastructure outage turned into a market order. So every exit condition here requires
POSITIVE evidence that it tripped; absent data is never an exit.

SHADOW POSITIONS ARE MANAGED IDENTICALLY TO REAL ONES, adjudicated exits included. The
paired comparison in the PRD only isolates the ENTRY veto if everything downstream of
entry is the same for both arms; exiting shadows mechanically while exiting real
positions with an adjudicator would confound the two vetoes and answer neither question.
"""
from __future__ import annotations

import argparse
import json
import os

import numpy as np
import pandas as pd

from fundamentals.screens.portfolio_adjudicator import (
    UNCONDITIONAL_EXIT_REASONS,
    _ensure_tables,
    _record_decision,
    adjudicate_exit,
)
from fundamentals.screens.portfolio_ruleset import (
    CONFLUENCE_MAX_AGE_DAYS,
    ENTRY_STAGE,
    RULESET_VERSION,
    load_stage_keys,
    load_stage_reads,
    stage_for,
)
from utils.db import db_session, sql_to_df

SYNC_SOURCE_NAME = "fundamentals.screens.portfolio_exit"

# advisory_adjusted_ohlcv_daily is a VIEW over a TimescaleDB hypertable. Without a date
# bound, "latest close per symbol" has to visit every chunk to find each symbol's max
# date: measured 2026-09-04, 689 chunks and 2.4s for 100 symbols, against 0.2s bounded --
# and the unbounded cost grows with history while the bounded cost does not. This job runs
# nightly over the whole book, so it is exactly the shape this repo's hypertable rules
# warn about (DATA_CONTRACT.md; and the intraday OOM incident earlier this session).
#
# 90 days is deliberately generous: a name that has not traded in a quarter is a real
# condition worth SEEING, not a lookback to widen until it disappears.
PRICE_LOOKBACK_DAYS = int(os.getenv("PORTFOLIO_PRICE_LOOKBACK_DAYS", "90"))


def _open_positions() -> pd.DataFrame:
    return sql_to_df(
        """
        SELECT position_id, company_master_id, ticker, kind, entry_price, stop_pct,
               opened_at, deferral_count, confluence_count, contradicting_count,
               evaluable_count, stage_at_entry, target_date, prediction_text,
               invalidation_criteria, score_version, baseline_score_version,
               baseline_contradicting_count, ruleset_version, position_size_rs,
               target_weight, story_score_at_entry, sector_code, daily_vol_pct, trail_high, trim_count
          FROM fundamentals_portfolio_position
         WHERE status = 'open'
        """
    )


def _latest_prices(tickers: list[str]) -> dict[str, float]:
    if not tickers:
        return {}
    df = sql_to_df(
        """
        SELECT DISTINCT ON (symbol) symbol, adj_close, date
          FROM advisory_adjusted_ohlcv_daily
         WHERE symbol = ANY(%s)
           AND date >= now() - make_interval(days => %s)
         ORDER BY symbol, date DESC
        """,
        params=(list(tickers), PRICE_LOOKBACK_DAYS),
    )
    return {str(r.symbol): float(r.adj_close) for r in df.itertuples()
            if r.adj_close is not None and pd.notna(r.adj_close)}


def _current_scores(company_ids: list[str]) -> dict[str, dict]:
    if not company_ids:
        return {}
    df = sql_to_df(
        """
        SELECT DISTINCT ON (company_master_id)
               company_master_id, contradicting_count, evaluable_count, run_date, score_version,
               axis_fundamentals_trajectory, axis_event_corroboration, axis_sector_cycle,
               axis_ownership, axis_valuation
          FROM fundamentals_confluence_score
         WHERE company_master_id = ANY(%s)
         ORDER BY company_master_id, run_date DESC, score_version DESC
        """,
        params=(list(company_ids),),
    )
    return {str(r.company_master_id): {"contradicting_count": r.contradicting_count,
                                       "evaluable_count": r.evaluable_count,
                                       "run_date": r.run_date,
                                       "score_version": int(r.score_version),
                                       "axes": {"fundamentals_trajectory": r.axis_fundamentals_trajectory,
                                                "event_corroboration": r.axis_event_corroboration,
                                                "sector_cycle": r.axis_sector_cycle,
                                                "ownership": r.axis_ownership,
                                                "valuation": r.axis_valuation}}
            for r in df.itertuples()}


# Every position opened before the 2026-09-23 audit was entered on confluence v1.
LEGACY_SCORE_VERSION = 1

# --- Ruleset v2 exits (2026-09-29, docs/FUNDAMENTAL_REEVALUATION_PRD.md 5.1) -----------------
# v1 closed a position the moment ANY axis flipped to contradicting, and the axes flicker:
# 11 closes after an average of 10 days held, 9 of them "thesis invalidation", against
# 60-365 day forecasts; the valuation axis flips when a stock rises ~10%, closing winners for
# winning. v2 exits a thesis only on:
#   - a HARD contradiction: a new alert of one of HARD_NEGATIVE_TRIGGERS since entry --
#     immediately, no minimum hold;
#   - a SOFT contradiction (an axis other than valuation) above baseline on each of the last
#     SOFT_PERSIST_RUNS scoring runs, and only after MIN_HOLD_SESSIONS;
#   - the Weinstein stage reaching EXIT_STAGE (declining), also after MIN_HOLD_SESSIONS --
#     not merely "no longer stage 2".
# Valuation is never an exit reason (an entry filter only). Stop and target date unchanged.
HARD_NEGATIVE_TRIGGERS = frozenset({"results_decline", "rating_downgrade", "pledge_increase",
                                    "auditor_change", "insider_sell_surprise"})
SOFT_PERSIST_RUNS = 3
MIN_HOLD_SESSIONS = 20
EXIT_STAGE = 4
EXIT_AXES = ("fundamentals_trajectory", "event_corroboration", "sector_cycle", "ownership")  # not valuation


def soft_contradicting(axes: dict) -> int:
    return sum(1 for a in EXIT_AXES if axes.get(a) is False)


def sessions_held(opened_at, today) -> int:
    start = pd.Timestamp(opened_at)
    start = (start.tz_convert("Asia/Kolkata") if start.tzinfo else start).date()
    return int(np.busday_count(start, pd.Timestamp(today).date()))


def _hard_events(company_ids: list[str]) -> dict[str, list[dict]]:
    if not company_ids:
        return {}
    df = sql_to_df(
        "SELECT company_master_id, trigger_type, load_ts, alert_date FROM fundamentals_l3_alerts "
        "WHERE company_master_id = ANY(%s) AND trigger_type = ANY(%s) AND load_ts >= now() - interval '400 days'",
        params=(list(company_ids), list(HARD_NEGATIVE_TRIGGERS)),
    )
    out: dict[str, list[dict]] = {}
    for r in df.itertuples():
        out.setdefault(str(r.company_master_id), []).append(
            {"trigger_type": r.trigger_type, "load_ts": r.load_ts, "alert_date": r.alert_date})
    return out


def _recent_scores(company_ids: list[str], runs: int = SOFT_PERSIST_RUNS) -> dict[str, list[dict]]:
    """The last `runs` scoring runs per company, newest first."""
    if not company_ids:
        return {}
    df = sql_to_df(
        """
        SELECT * FROM (
          SELECT company_master_id, run_date, score_version,
                 axis_fundamentals_trajectory, axis_event_corroboration, axis_sector_cycle,
                 axis_ownership, axis_valuation,
                 row_number() OVER (PARTITION BY company_master_id ORDER BY run_date DESC, score_version DESC) AS k
            FROM fundamentals_confluence_score
           WHERE company_master_id = ANY(%s) AND run_date >= now() - interval '60 days'
        ) x WHERE k <= %s ORDER BY company_master_id, k
        """,
        params=(list(company_ids), runs),
    )
    out: dict[str, list[dict]] = {}
    for r in df.itertuples():
        out.setdefault(str(r.company_master_id), []).append({
            "run_date": r.run_date, "score_version": int(r.score_version),
            "axes": {"fundamentals_trajectory": r.axis_fundamentals_trajectory,
                     "event_corroboration": r.axis_event_corroboration, "sector_cycle": r.axis_sector_cycle,
                     "ownership": r.axis_ownership, "valuation": r.axis_valuation}})
    return out


def v2_thesis_exit(position, *, hard_events: list[dict], recent: list[dict], stage_now, today) -> str | None:
    """The v2 thesis-invalidation decision for one position: a reason string, or None."""
    opened = pd.Timestamp(position.opened_at)
    for ev in hard_events:
        seen = pd.Timestamp(ev["load_ts"])
        if (seen.tz_localize("UTC") if seen.tzinfo is None else seen) > (opened.tz_localize("UTC") if opened.tzinfo is None else opened):
            return f"hard contradiction since entry: {ev['trigger_type']} (alert {ev['alert_date']})"
    if sessions_held(position.opened_at, today) < MIN_HOLD_SESSIONS:
        return None
    if len(recent) >= SOFT_PERSIST_RUNS and len({r["score_version"] for r in recent}) == 1:
        # v2 entries require zero contradicting axes, so the soft baseline is zero.
        if all(soft_contradicting(r["axes"]) > 0 for r in recent):
            names = sorted(a for a in EXIT_AXES if recent[0]["axes"].get(a) is False)
            return f"soft contradiction on {', '.join(names)} for {SOFT_PERSIST_RUNS} consecutive runs"
    if stage_now is not None and int(stage_now) == EXIT_STAGE:
        return f"Weinstein stage {EXIT_STAGE} (declining)"
    return None


def _v3_state(positions: pd.DataFrame) -> dict[str, dict]:
    """Per v3 position: live story score/flaws, the last FADE_RUNS daily scores (newest first),
    valuation vs own history, and the highest close since entry (for the trailing stop)."""
    from fundamentals.screens import portfolio_v3
    from fundamentals.screens.story_score import SCORE_VERSION

    if positions.empty:
        return {}
    ids = sorted({str(c) for c in positions["company_master_id"]})
    live = sql_to_df("SELECT company_master_id, story_score, flaws FROM fundamentals_story_score_live "
                     "WHERE company_master_id = ANY(%s)", params=(ids,))
    daily = sql_to_df(
        """
        SELECT company_master_id, story_score FROM (
          SELECT company_master_id, story_score,
                 row_number() OVER (PARTITION BY company_master_id ORDER BY as_of_date DESC) AS k
            FROM fundamentals_story_score
           WHERE company_master_id = ANY(%s) AND score_version = %s
             AND as_of_date >= now()::date - 30) x
         WHERE k <= %s ORDER BY company_master_id, k
        """, params=(ids, SCORE_VERSION, portfolio_v3.FADE_RUNS))
    valuation = sql_to_df(
        """
        SELECT DISTINCT ON (company_id) cm.company_master_id, s.valuation_vs_own_history_ratio
          FROM fundamentals_l2_state s
          JOIN (SELECT DISTINCT ON (screener_company_id) screener_company_id, company_master_id
                  FROM fundamentals_snapshot_daily ORDER BY screener_company_id, as_of_date DESC) cm
            ON cm.screener_company_id::bigint = s.company_id
         WHERE cm.company_master_id = ANY(%s)
         ORDER BY company_id, run_date DESC
        """, params=(ids,))
    highs = {}
    for r in positions.itertuples():
        opened = pd.Timestamp(r.opened_at)
        df = sql_to_df("SELECT max(adj_close) AS hi FROM advisory_adjusted_ohlcv_daily "
                       "WHERE symbol = %s AND date >= greatest(%s::date, now()::date - interval '800 days') "
                       "AND date <= now()::date",
                       params=(str(r.ticker), str(opened.date())))
        hi = None if df.empty or pd.isna(df.iloc[0]["hi"]) else float(df.iloc[0]["hi"])
        stored = r.trail_high if r.trail_high is not None and not pd.isna(r.trail_high) else None
        highs[str(r.position_id)] = max([x for x in (hi, stored) if x is not None], default=None)
    live_by = {str(r.company_master_id): {"story_score": r.story_score, "flaws": r.flaws} for r in live.itertuples()}
    daily_by: dict[str, list[float]] = {}
    for r in daily.itertuples():
        daily_by.setdefault(str(r.company_master_id), []).append(float(r.story_score))
    val_by = {str(r.company_master_id): r.valuation_vs_own_history_ratio for r in valuation.itertuples()}
    return {str(r.position_id): {"live": live_by.get(str(r.company_master_id)),
                                 "recent": daily_by.get(str(r.company_master_id), []),
                                 "valuation_ratio": val_by.get(str(r.company_master_id)),
                                 "trail_high": highs.get(str(r.position_id))}
            for r in positions.itertuples()}


def _baseline(position) -> tuple[int, int]:
    """(scorer version, contradicting count) the exit compares against. Pre-audit rows have
    no baseline columns: their entry counts are the baseline, on v1."""
    version = getattr(position, "baseline_score_version", None)
    count = getattr(position, "baseline_contradicting_count", None)
    if version is None or pd.isna(version):
        version = getattr(position, "score_version", None)
    if version is None or pd.isna(version):
        version = LEGACY_SCORE_VERSION
    if count is None or pd.isna(count):
        count = position.contradicting_count or 0
    return int(version), int(count)


def _rebaseline(position_id: str, score_version: int, contradicting: int) -> None:
    with db_session() as (_conn, cur):
        cur.execute(
            "UPDATE fundamentals_portfolio_position "
            "   SET baseline_score_version = %s, baseline_contradicting_count = %s "
            " WHERE position_id = %s AND status = 'open'",
            (score_version, contradicting, position_id),
        )


def evaluate_exit_triggers() -> dict[str, object]:
    """Pure: which open positions have tripped a named exit condition, and why."""
    positions = _open_positions()
    if positions.empty:
        return {"ruleset_version": RULESET_VERSION, "open": 0, "triggers": [],
                "stage_api_available": True, "trims": [], "tax_guard_waits": [], "trail_updates": []}

    tickers = sorted({str(t) for t in positions["ticker"]})
    company_ids = sorted({str(c) for c in positions["company_master_id"]})
    prices = _latest_prices(tickers)
    scores = _current_scores(company_ids)
    v2_ids = sorted({str(r.company_master_id) for r in positions.itertuples()
                     if int(getattr(r, "ruleset_version", 1) or 1) >= 2})
    hard = _hard_events(v2_ids)
    recent = _recent_scores(v2_ids)
    stages = load_stage_reads()
    stage_keys = load_stage_keys([str(c) for c in positions["company_master_id"]])
    # IST, not the server's clock: between 18:30 UTC and midnight the two are different
    # calendar days, so a system-local date fires the target_date exit a day early or late
    # depending only on when cron happened to run.
    today = (pd.Timestamp.now(tz="UTC") + pd.Timedelta(hours=5, minutes=30)).normalize().tz_localize(None)

    score_cutoff = pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=CONFLUENCE_MAX_AGE_DAYS)
    triggers = []
    unpriced, stop_unchecked, stale_scores, rebaselines = [], [], [], []
    trims, tax_waits, trail_updates = [], [], []
    v3_mask = positions["ruleset_version"].fillna(1).astype(int) >= 3 if "ruleset_version" in positions else None
    v3_state = _v3_state(positions[v3_mask]) if v3_mask is not None and v3_mask.any() else {}
    for p in positions.itertuples():
        price = prices.get(str(p.ticker))
        if int(getattr(p, "ruleset_version", 1) or 1) >= 3:
            # Ruleset v3 (portfolio_v3.py): stops incl. trailing, thesis broken, story fading,
            # valuation trim, target date, tax guard.
            from fundamentals.screens import portfolio_v3

            if price is None:
                unpriced.append(str(p.ticker))
            st = v3_state.get(str(p.position_id), {})
            if st.get("trail_high") is not None and st["trail_high"] != getattr(p, "trail_high", None):
                trail_updates.append({"position_id": p.position_id, "trail_high": st["trail_high"]})
            act = portfolio_v3.v3_exit_action(
                p, price=price, trail_high=st.get("trail_high"), live=st.get("live"),
                hard_events=hard.get(str(p.company_master_id), []), recent_scores=st.get("recent", []),
                valuation_ratio=st.get("valuation_ratio"), today=today)
            if act is None:
                continue
            if act["action"] == "exit":
                trig = _trigger(p, act["reason"], price, act["detail"])
                trig["current_story"] = st.get("live")
                trig["story_score_at_entry"] = getattr(p, "story_score_at_entry", None)
                triggers.append(trig)
            elif act["action"] == "trim":
                trims.append({"position_id": p.position_id, "ticker": str(p.ticker), "price": price,
                              "detail": act["detail"], "position_size_rs": getattr(p, "position_size_rs", None),
                              "entry_price": p.entry_price, "trim_count": getattr(p, "trim_count", None)})
            else:
                tax_waits.append({"ticker": str(p.ticker), "reason": act["reason"], "detail": act["detail"]})
            continue
        if price is None:
            # No trade in PRICE_LOOKBACK_DAYS. The stop CANNOT be evaluated for this
            # position, and a stop nobody is checking must never be silently assumed safe
            # -- "no silent fallback" (CLAUDE.md). It is reported, not skipped quietly.
            unpriced.append(str(p.ticker))
        stop_level = None
        if p.entry_price and p.stop_pct:
            stop_level = float(p.entry_price) * (1 - float(p.stop_pct) / 100.0)
        else:
            # No entry price or no stop: the stop can NEVER fire for this position. Used
            # to be silent unless today's price was also missing.
            stop_unchecked.append(str(p.ticker))

        # 1. Stop. Requires a real price -- a missing quote is not a breach.
        if price is not None and stop_level is not None and price <= stop_level:
            triggers.append(_trigger(p, "stop_loss", price,
                                     f"adjusted close {price:.2f} <= stop {stop_level:.2f} "
                                     f"({p.stop_pct}% below entry {float(p.entry_price):.2f})"))
            continue

        # 2v2. Ruleset v2 positions: hard contradictions, persistent soft ones, stage 4.
        if int(getattr(p, "ruleset_version", 1) or 1) >= 2:
            stage_now = stage_for(p.company_master_id, stages, stage_keys)
            score = scores.get(str(p.company_master_id)) or {}
            reason = v2_thesis_exit(p, hard_events=hard.get(str(p.company_master_id), []),
                                    recent=recent.get(str(p.company_master_id), []), stage_now=stage_now, today=today)
            if reason:
                triggers.append(_trigger(p, "thesis_invalidation", price, reason, score=score, stage_now=stage_now))
                continue
            target = getattr(p, "target_date", None)
            if target is not None and pd.notna(target) and pd.Timestamp(target).normalize() <= today:
                triggers.append(_trigger(p, "target_date", price,
                                         f"target date {pd.Timestamp(target).date()} has passed without the thesis resolving",
                                         score=score, stage_now=stage_now))
            continue

        # 2. Thesis invalidation (ruleset v1). POSITIVE evidence only: a contradicting axis that is
        #    actually present, or a stage read that exists and is no longer advancing.
        #
        #    Compared WITHIN one scorer version (2026-09-23 audit). The check used to be
        #    "any contradicting axis on the newest score", so shipping confluence v2 -- which
        #    revived a dead axis -- closed ASHIANA as "a contradicting axis appeared since
        #    entry" when only the scorer had changed. A version change now RE-BASELINES the
        #    position (named in the run output); only a rise above the baseline on the same
        #    version is evidence about the company. A stale score is reported, never used.
        score = scores.get(str(p.company_master_id)) or {}
        contradicting = score.get("contradicting_count")
        if contradicting is not None and score.get("run_date") is not None \
                and pd.Timestamp(score["run_date"]) < score_cutoff:
            stale_scores.append(str(p.ticker))
            contradicting = None
        if contradicting is not None:
            base_version, base_count = _baseline(p)
            if (score.get("score_version") or LEGACY_SCORE_VERSION) != base_version:
                rebaselines.append({"position_id": p.position_id, "ticker": str(p.ticker),
                                    "from_version": base_version, "to_version": score.get("score_version"),
                                    "contradicting_count": int(contradicting)})
            elif int(contradicting) > base_count:
                triggers.append(_trigger(p, "thesis_invalidation", price,
                                         f"{int(contradicting)} contradicting axis/axes on score "
                                         f"v{base_version}, up from {base_count} at baseline",
                                         score=score, stage_now=stage_for(p.company_master_id, stages, stage_keys)))
                continue
        stage_now = stage_for(p.company_master_id, stages, stage_keys)
        if stage_now is not None and int(stage_now) != ENTRY_STAGE:
            triggers.append(_trigger(p, "thesis_invalidation", price,
                                     f"Weinstein stage left {ENTRY_STAGE} (now {int(stage_now)})",
                                     score=score, stage_now=stage_now))
            continue

        # 3. Target date reached -- the date the adjudicator committed to at entry.
        target = getattr(p, "target_date", None)
        if target is not None and pd.notna(target) and pd.Timestamp(target).normalize() <= today:
            triggers.append(_trigger(p, "target_date", price,
                                     f"target date {pd.Timestamp(target).date()} has passed "
                                     f"without the thesis resolving",
                                     score=score, stage_now=stage_now))

    return {
        "ruleset_version": RULESET_VERSION,
        "open": int(len(positions)),
        "priced": len(prices),
        "unpriced": unpriced,
        "stop_unchecked": stop_unchecked,
        "stale_confluence": stale_scores,
        "rebaselines": rebaselines,
        "stage_reads": len(stages),
        "stage_api_available": bool(stages),
        "triggers": triggers,
        "trims": trims,
        "tax_guard_waits": tax_waits,
        "trail_updates": trail_updates,
    }


def _trigger(position, reason: str, price: float | None, detail: str, *,
             score: dict | None = None, stage_now: int | None = None) -> dict:
    return {
        "position_id": position.position_id,
        "company_master_id": position.company_master_id,
        "ticker": position.ticker,
        "kind": position.kind,
        "exit_reason": reason,
        "detail": detail,
        "price": price,
        "entry_price": position.entry_price,
        "stop_pct": position.stop_pct,
        "opened_at": position.opened_at,
        "deferral_count": int(position.deferral_count or 0),
        "confluence_count": position.confluence_count,
        "contradicting_count": position.contradicting_count,
        "evaluable_count": position.evaluable_count,
        "stage_at_entry": position.stage_at_entry,
        "target_date": getattr(position, "target_date", None),
        "prediction_text": getattr(position, "prediction_text", None),
        "invalidation_criteria": getattr(position, "invalidation_criteria", None),
        "score_version_at_entry": getattr(position, "score_version", None),
        "current_score": score or None,
        "stage_now": stage_now,
    }


def _close(position_id: str, reason: str, exit_price: float | None) -> None:
    with db_session() as (_conn, cur):
        cur.execute(
            "UPDATE fundamentals_portfolio_position "
            "   SET status = 'closed', closed_at = now(), close_reason = %s, exit_price = %s "
            " WHERE position_id = %s AND status = 'open'",
            (reason, exit_price, position_id),
        )


def _set_trail_high(position_id: str, value: float) -> None:
    with db_session() as (_conn, cur):
        cur.execute("UPDATE fundamentals_portfolio_position SET trail_high = %s "
                    "WHERE position_id = %s AND status = 'open' AND (trail_high IS NULL OR trail_high < %s)",
                    (value, position_id, value))


def _increment_trim(position_id: str) -> None:
    with db_session() as (_conn, cur):
        cur.execute("UPDATE fundamentals_portfolio_position SET trim_count = COALESCE(trim_count, 0) + 1 "
                    "WHERE position_id = %s AND status = 'open'", (position_id,))


def _record_rebalances() -> list[dict]:
    """v3 rebalance bands (PRD 5.2): record a signal when a position drifts 50% from target."""
    from fundamentals.screens import portfolio_v3
    from fundamentals.screens.portfolio_buckets import DEFAULT_BUCKET, bucket_config

    try:
        book = portfolio_v3.open_v3_book()
        if book.empty:
            return []
        cfg = bucket_config(DEFAULT_BUCKET)
        prices = _latest_prices(sorted({str(t) for t in book["ticker"]}))
        signals = portfolio_v3.rebalance_signals(book, prices, float(cfg["capital_rs"]), int(cfg["target_positions"]))
        quiet = portfolio_v3.recent_rebalanced([s["position_id"] for s in signals])
        fresh = [s for s in signals if s["position_id"] not in quiet]
        for s in fresh:
            portfolio_v3.record_adjustment(s["position_id"], s["kind"], s["ticker"], s["price"], s["value_rs"],
                                           s["target_rs"], s["target_rs"], "drifted 50% from target weight")
        return fresh
    except Exception as exc:  # noqa: BLE001 -- a signal record must not block exits
        from utils.fallback_telemetry import record_local_fallback_event

        record_local_fallback_event(module=SYNC_SOURCE_NAME, source="db", fallback_type="rebalance_signals_failed",
                                    severity="warn", reason="rebalance signals not recorded this run", error=repr(exc))
        return []


def _fill_counterfactuals() -> int:
    from fundamentals.screens import portfolio_v3

    try:
        return portfolio_v3.fill_counterfactuals()
    except Exception as exc:  # noqa: BLE001 -- measurement must not block exits
        from utils.fallback_telemetry import record_local_fallback_event

        record_local_fallback_event(module=SYNC_SOURCE_NAME, source="db", fallback_type="counterfactual_fill_failed",
                                    severity="warn", reason="counterfactual returns not filled this run", error=repr(exc))
        return 0


def _defer(position_id: str) -> None:
    with db_session() as (_conn, cur):
        cur.execute(
            "UPDATE fundamentals_portfolio_position "
            "   SET deferral_count = COALESCE(deferral_count, 0) + 1, last_deferred_at = now() "
            " WHERE position_id = %s AND status = 'open'",
            (position_id,),
        )


def run_exits(*, dry_run: bool = False) -> dict[str, object]:
    _ensure_tables()
    evaluation = evaluate_exit_triggers()
    closed, deferred = [], []
    if not dry_run:
        for rb in evaluation.get("rebaselines", []):
            _rebaseline(rb["position_id"], rb["to_version"], rb["contradicting_count"])
        for tu in evaluation.get("trail_updates", []):
            _set_trail_high(tu["position_id"], tu["trail_high"])
    trimmed = []
    for t in evaluation.get("trims", []):
        trimmed.append({"ticker": t["ticker"], "detail": t["detail"]})
        if dry_run:
            continue
        from fundamentals.screens import portfolio_v3

        size = t.get("position_size_rs")
        value = (float(size) * float(t["price"]) / float(t["entry_price"])
                 if size and t.get("price") and t.get("entry_price") else None)
        portfolio_v3.record_adjustment(t["position_id"], "trim", t["ticker"], t.get("price"), value,
                                       None if value is None else value * (1 - portfolio_v3.TRIM_FRACTION),
                                       None, t["detail"])
        portfolio_v3.record_counterfactual(t["position_id"], "trim", t["ticker"], t.get("price"))
        _increment_trim(t["position_id"])

    for trigger in evaluation["triggers"]:
        reason = trigger["exit_reason"]
        if reason in UNCONDITIONAL_EXIT_REASONS:
            verdict = {"decision": "exit", "reason": trigger["detail"], "model": None,
                       "prompt_version": None}
        else:
            verdict = adjudicate_exit(trigger, reason)

        if not dry_run:
            _record_decision(
                phase="exit",
                company_master_id=trigger["company_master_id"],
                ruleset_version=RULESET_VERSION,
                decision=verdict["decision"],
                reason=f"[{reason}] {trigger['detail']} | {verdict.get('reason')}",
                model=verdict.get("model"),
                payload=trigger,
            )
        if verdict["decision"] == "exit":
            if not dry_run:
                _close(trigger["position_id"], reason, trigger.get("price"))
                from fundamentals.screens import portfolio_v3

                # what the stock did after we left, per exit rule (PRD 5.3 counterfactuals)
                portfolio_v3.record_counterfactual(trigger["position_id"], reason, str(trigger["ticker"]),
                                                   trigger.get("price"))
            closed.append({"ticker": trigger["ticker"], "kind": trigger["kind"], "reason": reason})
        else:
            if not dry_run:
                _defer(trigger["position_id"])
            deferred.append({"ticker": trigger["ticker"], "kind": trigger["kind"], "reason": reason})

    return {
        "source": SYNC_SOURCE_NAME,
        "status": "ok",
        "dry_run": dry_run,
        "ruleset_version": RULESET_VERSION,
        "open_evaluated": evaluation["open"],
        "stage_api_available": evaluation["stage_api_available"],
        "triggered": len(evaluation["triggers"]),
        # Positions whose stop could not be checked at all. Non-empty here is a real
        # finding, not noise.
        "unpriced": evaluation.get("unpriced", []),
        "stop_unchecked": evaluation.get("stop_unchecked", []),
        "stale_confluence": evaluation.get("stale_confluence", []),
        "rebaselined": evaluation.get("rebaselines", []),
        "closed": len(closed),
        "deferred": len(deferred),
        "closed_detail": closed,
        "deferred_detail": deferred,
        "trimmed": trimmed,
        "rebalance_signals": [] if dry_run else _record_rebalances(),
        "tax_guard_waits": evaluation.get("tax_guard_waits", []),
        "counterfactuals_filled": 0 if dry_run else _fill_counterfactuals(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate and act on portfolio exits.")
    parser.add_argument("--dry-run", action="store_true", help="Decide but write nothing.")
    args = parser.parse_args(argv)
    print(json.dumps(run_exits(dry_run=args.dry_run), ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
