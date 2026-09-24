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
               baseline_contradicting_count
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
                "stage_api_available": True}

    tickers = sorted({str(t) for t in positions["ticker"]})
    company_ids = sorted({str(c) for c in positions["company_master_id"]})
    prices = _latest_prices(tickers)
    scores = _current_scores(company_ids)
    stages = load_stage_reads()
    stage_keys = load_stage_keys([str(c) for c in positions["company_master_id"]])
    # IST, not the server's clock: between 18:30 UTC and midnight the two are different
    # calendar days, so a system-local date fires the target_date exit a day early or late
    # depending only on when cron happened to run.
    today = (pd.Timestamp.now(tz="UTC") + pd.Timedelta(hours=5, minutes=30)).normalize().tz_localize(None)

    score_cutoff = pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=CONFLUENCE_MAX_AGE_DAYS)
    triggers = []
    unpriced, stop_unchecked, stale_scores, rebaselines = [], [], [], []
    for p in positions.itertuples():
        price = prices.get(str(p.ticker))
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

        # 2. Thesis invalidation. POSITIVE evidence only: a contradicting axis that is
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
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate and act on portfolio exits.")
    parser.add_argument("--dry-run", action="store_true", help="Decide but write nothing.")
    args = parser.parse_args(argv)
    print(json.dumps(run_exits(dry_run=args.dry_run), ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
