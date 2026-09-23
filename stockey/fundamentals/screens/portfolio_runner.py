"""Nightly portfolio run: mechanical ruleset -> LLM adjudication -> positions.

    python -m fundamentals.screens.portfolio_runner              # record-only (default)
    python -m fundamentals.screens.portfolio_runner --live       # real positions
    python -m fundamentals.screens.portfolio_runner --dry-run    # decide, write nothing

RECORD-ONLY IS THE DEFAULT, and --live must be passed explicitly every run. Per the PRD's
rollout, phase 1 is a shakedown: does this machinery produce a portfolio a human
recognises as sensible? That is not a backtest and does not pretend to be -- it costs
only calendar time, and it is the last point at which a design error is free.

REJECTED candidates are still written, as kind='shadow'. They are what makes the
adjudicator falsifiable: a paired taken-vs-rejected comparison under one ruleset over one
period says far more on a small sample than an absolute hit rate ever could. If the
rejected names outperform the taken ones, the veto layer is destroying value and should
drop to advisory.

SIZING (operator-set 2026-09-04): a FLAT Rs 1,00,000 per position, up to 100 names, so
the book can deploy at most Rs 1 crore. MAX_POSITIONS is therefore a CAPITAL constraint,
not the diversification heuristic an earlier draft carried from the source spec's 12-20
figure -- and that distinction decides how it is enforced. A full book STOPS entering and
records which names it turned away; it never silently truncates the candidate list, which
would make entry depend on iteration order rather than on anything about the companies.

Only OPEN ACCEPTED positions consume the book. Vetoed names are still recorded and still
sized -- they commit no capital, so they cannot fill the book, but they must be sized on
the same basis or the accepted-vs-vetoed comparison would be measuring position size
instead of selection.
"""
from __future__ import annotations

import argparse
import json
import os
import uuid

import pandas as pd

from fundamentals.screens.portfolio_adjudicator import (
    _ensure_tables,
    _record_decision,
    adjudicate_entry,
)
from fundamentals.screens.l5_sizing import (
    CAPITAL_PER_POSITION_RS,
    MAX_POSITIONS,
    get_position_size_recommendation,
)
from fundamentals.screens.portfolio_exit import PRICE_LOOKBACK_DAYS
from fundamentals.screens.portfolio_ruleset import RULESET_VERSION, evaluate_entry_candidates
from utils.db import db_session, sql_to_df

SYNC_SOURCE_NAME = "fundamentals.screens.portfolio_runner"

# A stop undone the same day is not a stop. CHEMBOND was stopped out 2026-09-21 at 187.04
# and re-accepted that same day at 187.04; LAMBODHARA the same on 09-22 at 116.08. The
# re-entry guard reads only OPEN positions, and the stop had just closed the row, so
# nothing stood in the way. A stopped name now waits out roughly the horizon its own stop
# was sized on (1.5x the 10-day sigma), so re-entry needs the price to have done something
# other than sit where the stop fired.
STOP_COOLDOWN_DAYS = int(os.getenv("PORTFOLIO_STOP_COOLDOWN_DAYS", "14"))
STOCKEY_RUN_STATE: dict[str, object] = {}


def _open_vetoed_tickers() -> dict:
    """Names carrying an open VETOED shadow, mapped to WHEN they were vetoed.

    The timestamp is what makes a veto revisitable without being meaningless -- see
    run_portfolio's evidence check.
    """
    df = sql_to_df(
        "SELECT ticker, max(opened_at) AS vetoed_at FROM fundamentals_portfolio_position "
        " WHERE status = 'open' AND entry_decision = 'reject' GROUP BY ticker"
    )
    return {} if df.empty else dict(zip(df["ticker"], df["vetoed_at"]))


def _evidence_moved_since(candidate: dict, vetoed_at) -> bool:
    """Has anything NEW arrived about this company since it was vetoed?

    THE PROBLEM THIS SOLVES (found in review, 2026-09-04, immediately after fixing the
    permanent-ban bug). Allowing a vetoed name to be re-adjudicated every night makes the
    veto meaningless in the other direction: the adjudicator is a sampled model, so asking
    it the same question about the same evidence repeatedly will eventually produce an
    accept. Observed live within one hour -- RANEHOLDIN was vetoed at 18:42 and accepted
    at 19:32 on an identical scorecard, with the confluence score unchanged. That turns the
    rule into "accept if the model EVER says yes", which is not a filter at all, and it
    quietly favours whichever names get re-asked most often.

    So a veto stands until the EVIDENCE moves. The confluence score's run_date is the
    evidence date: it is what the adjudicator was given and what would have to change for
    a different answer to mean anything.
    """
    scored_on = candidate.get("scored_on")
    if scored_on is None or vetoed_at is None:
        return False        # cannot show anything changed -> the veto stands
    return pd.Timestamp(scored_on).date() > pd.Timestamp(vetoed_at).date()


def _open_accepted_tickers() -> set[str]:
    """Positions that consume the book. Vetoed names are recorded but commit no capital."""
    df = sql_to_df(
        "SELECT ticker FROM fundamentals_portfolio_position "
        " WHERE status = 'open' AND entry_decision = 'accept'"
    )
    return set(df["ticker"]) if not df.empty else set()


def _recently_stopped_tickers() -> dict:
    """Names stopped out inside the cooldown, mapped to WHEN the stop fired."""
    df = sql_to_df(
        "SELECT ticker, max(closed_at) AS stopped_at FROM fundamentals_portfolio_position "
        "  WHERE status = 'closed' AND close_reason = 'stop_loss' "
        "    AND closed_at >= now() - make_interval(days => %s) "
        "  GROUP BY ticker",
        params=(STOP_COOLDOWN_DAYS,),
    )
    return {} if df.empty else dict(zip(df["ticker"], df["stopped_at"]))


def _latest_price(ticker: str) -> float | None:
    df = sql_to_df(
        "SELECT adj_close FROM advisory_adjusted_ohlcv_daily WHERE symbol = %s "
        "  AND date >= now() - make_interval(days => %s) "
        "ORDER BY date DESC LIMIT 1",
        params=(ticker, PRICE_LOOKBACK_DAYS),
    )
    return float(df.iloc[0]["adj_close"]) if not df.empty else None


def _size_for(candidate: dict) -> dict:
    """L5 sizing for a name about to be opened.

    get_position_size_recommendation() gates on an ALREADY-OPEN position, which this one
    is not yet, so the arithmetic is called directly. Falling back to the flat allocation
    when ADV is unknown matches compute_position_size's own rule: unknown liquidity is
    neither infinite nor zero.
    """
    from fundamentals.screens.l5_sizing import compute_position_size, load_adv_inputs

    adv = load_adv_inputs(candidate["company_master_id"])
    return compute_position_size(
        target_capital_rs=CAPITAL_PER_POSITION_RS,
        adv_value_rs=adv["adv_value_rs"] if adv else None,
    )


def _open_position(candidate: dict, verdict: dict, *, kind: str, dry_run: bool) -> dict:
    price = _latest_price(candidate["ticker"])
    stop = candidate.get("stop") or {}
    sizing = _size_for(candidate)
    row = {
        "position_id": f"pos:{uuid.uuid4().hex[:16]}",
        "company_master_id": candidate["company_master_id"],
        "ticker": candidate["ticker"],
        "ruleset_version": RULESET_VERSION,
        "kind": kind,
        "status": "open",
        "entry_price": price,
        "stop_pct": stop.get("stop_pct"),
        "stop_basis": stop.get("basis"),
        "confluence_count": candidate["confluence_count"],
        "contradicting_count": candidate["contradicting_count"],
        "evaluable_count": candidate["evaluable_count"],
        "stage_at_entry": candidate["stage"],
        "adjudicator_model": verdict.get("model"),
        "adjudicator_prompt_version": verdict.get("prompt_version"),
        "adjudicator_reason": verdict.get("reason"),
        "entry_decision": verdict.get("decision"),
        "metric_name": verdict.get("metric_name"),
        "metric_operator": verdict.get("metric_operator"),
        "metric_threshold": verdict.get("metric_threshold"),
        "metric_rejected_reason": verdict.get("metric_rejected_reason"),
        "position_size_rs": sizing["recommended_size_rs"],
        "adv_cap_rs": sizing["adv_cap_rs"],
        "sizing_basis": sizing["binding_constraint"],
        "prediction_text": verdict.get("prediction_text"),
        "target_date": verdict.get("target_date"),
        "invalidation_criteria": verdict.get("invalidation_criteria"),
        "target_date_basis": verdict.get("target_date_basis"),
    }
    if dry_run:
        return row
    cols = ", ".join(row)
    marks = ", ".join(["%s"] * len(row))
    with db_session() as (_conn, cur):
        cur.execute(
            f"INSERT INTO fundamentals_portfolio_position ({cols}) VALUES ({marks}) "
            f"ON CONFLICT (position_id) DO NOTHING",
            tuple(row.values()),
        )
    return row


def run_portfolio(*, live: bool = False, dry_run: bool = False) -> dict[str, object]:
    _ensure_tables()
    evaluation = evaluate_entry_candidates()

    # A dead stage API rejects EVERY candidate for a reason that has nothing to do with
    # the companies. Refuse the run rather than record a day of spurious zero-entries
    # that would later read as "the rule found nothing".
    if not evaluation.get("stage_api_available"):
        return {
            "source": SYNC_SOURCE_NAME,
            "status": "blocked",
            "reason": "systrader stage API unavailable -- every candidate would fail the "
                      "stage condition for an infrastructure reason, not a real one",
            "ruleset_version": RULESET_VERSION,
            "entered": 0, "rejected": 0,
        }

    # Only an open ACCEPTED position blocks re-entry. A vetoed shadow must NOT
    # (review finding, 2026-09-04): _open_tickers() included vetoed rows, and a vetoed
    # shadow never closes on its own, so a single day's veto silently became a PERMANENT
    # ban -- the name could never be reconsidered however much its evidence improved.
    # Nobody decided that, and it is the opposite of "the adjudicator filters this
    # decision".
    already_accepted = _open_accepted_tickers()
    vetoed_at_by_ticker = _open_vetoed_tickers()
    stopped_recently = _recently_stopped_tickers()
    kind = "real" if live else "shadow"
    entered, rejected, skipped, turned_away, re_vetoed, veto_stands = [], [], [], [], [], []
    stopped_out = []

    # Only accepted positions consume capital, so only they fill the book.
    book_used = len(already_accepted)

    for candidate in evaluation["candidates"]:
        if candidate["ticker"] in already_accepted:
            skipped.append(candidate["ticker"])
            continue
        if candidate["ticker"] in stopped_recently:
            # Stopped out inside the cooldown. Skipping BEFORE adjudication also saves the
            # call, same as the veto branch below. Named, not silent: "we would not buy it"
            # and "we were not allowed to buy it yet" mean different things.
            stopped_out.append(candidate["ticker"])
            continue
        vetoed_at = vetoed_at_by_ticker.get(candidate["ticker"])
        if vetoed_at is not None and not _evidence_moved_since(candidate, vetoed_at):
            # Vetoed, and nothing new has arrived. Do not re-ask -- see
            # _evidence_moved_since. Skipping BEFORE adjudication also saves the call.
            veto_stands.append(candidate["ticker"])
            continue
        verdict = adjudicate_entry(candidate)
        # Recorded BEFORE any book/duplicate branch: the adjudication happened and cost a
        # call, so the reasoning is kept even when no position follows from it.
        if not dry_run:
            _record_decision(
                phase="entry",
                company_master_id=candidate["company_master_id"],
                ruleset_version=RULESET_VERSION,
                decision=verdict["decision"],
                reason=verdict.get("reason"),
                model=verdict.get("model"),
                payload=candidate,
            )
        if verdict["decision"] == "accept":
            if book_used >= MAX_POSITIONS:
                # Named, not silent. "The book was full" and "the rule found nothing" look
                # identical in a position count and mean opposite things.
                turned_away.append(candidate["ticker"])
                continue
            _open_position(candidate, verdict, kind=kind, dry_run=dry_run)
            entered.append(candidate["ticker"])
            book_used += 1
        else:
            if candidate["ticker"] in vetoed_at_by_ticker:
                # Same name, vetoed again. The decision is recorded above; writing a second
                # identical shadow every night would inflate the vetoed arm of the paired
                # comparison with duplicates of one company.
                re_vetoed.append(candidate["ticker"])
                continue
            # Rejected -> shadow, always. This is the measurement, not bookkeeping.
            _open_position(candidate, verdict, kind="shadow", dry_run=dry_run)
            rejected.append(candidate["ticker"])
            vetoed_at_by_ticker[candidate["ticker"]] = pd.Timestamp.now(tz="UTC")

    return {
        "source": SYNC_SOURCE_NAME,
        "status": "ok",
        "mode": "live" if live else "record-only",
        "dry_run": dry_run,
        "ruleset_version": RULESET_VERSION,
        "evaluated": evaluation["evaluated"],
        "candidates": len(evaluation["candidates"]),
        "entered": len(entered),
        "rejected": len(rejected),
        "already_open_skipped": len(skipped),
        "entered_tickers": entered,
        "rejected_tickers": rejected,
        "capital_per_position_rs": CAPITAL_PER_POSITION_RS,
        "book_used": book_used,
        "book_capacity": MAX_POSITIONS,
        "turned_away_book_full": turned_away,
        "re_vetoed_no_duplicate": re_vetoed,
        # Vetoed previously and NOT re-asked, because no new evidence has arrived.
        "veto_stands_no_new_evidence": veto_stands,
        # Stopped out within STOP_COOLDOWN_DAYS, so not re-entered at the stop price.
        "stopped_recently_skipped": stopped_out,
    }


def main(argv: list[str] | None = None) -> int:
    global STOCKEY_RUN_STATE
    parser = argparse.ArgumentParser(description="Run the portfolio ruleset + adjudicator.")
    parser.add_argument("--live", action="store_true",
                        help="Open REAL positions. Without this, everything is recorded as shadow.")
    parser.add_argument("--dry-run", action="store_true", help="Decide but write nothing.")
    args = parser.parse_args(argv)

    STOCKEY_RUN_STATE = run_portfolio(live=args.live, dry_run=args.dry_run)
    print(json.dumps(STOCKEY_RUN_STATE, ensure_ascii=False, default=str), flush=True)
    return 0 if STOCKEY_RUN_STATE.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
