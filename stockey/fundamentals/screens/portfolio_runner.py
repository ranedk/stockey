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
import hashlib
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
from fundamentals.screens.portfolio_buckets import (
    DEFAULT_BUCKET,
    bucket_config,
    capital_per_position_rs,
)
from fundamentals.screens.portfolio_exit import PRICE_LOOKBACK_DAYS
from fundamentals.screens.portfolio_ruleset import RULESET_VERSION, V2_RULESET_VERSION, evaluate_entry_candidates
from utils.db import db_session, sql_to_df

SYNC_SOURCE_NAME = "fundamentals.screens.portfolio_runner"

# A stop undone the same day is not a stop. CHEMBOND was stopped out 2026-09-21 at 187.04
# and re-accepted that same day at 187.04; LAMBODHARA the same on 09-22 at 116.08. The
# re-entry guard reads only OPEN positions, and the stop had just closed the row, so
# nothing stood in the way. A stopped name now waits out roughly the horizon its own stop
# was sized on (1.5x the 10-day sigma), so re-entry needs the price to have done something
# other than sit where the stop fired.
STOP_COOLDOWN_DAYS = int(os.getenv("PORTFOLIO_STOP_COOLDOWN_DAYS", "14"))

# One-time catch-up admission (operator, 2026-09-26; fundamentals/screens/catchup_admission.py).
# Companies new to the universe whose signals from the last 60 days were stored as history
# go through the SAME entry rules as of today, at today's price. They are a separate,
# capped cohort: a company counts as catch-up while every alert it has comes from a
# catch-up filing (a fresh filing later makes it an ordinary name). Late entries behave
# differently from fresh ones, so they are tagged (entry_cohort) and measured apart, and
# capped so a one-time burst cannot fill the book. Also skipped if the price already ran
# more than CATCHUP_MAX_RUN_PCT since the signal -- the move the signal pointed at is gone.
CATCHUP_COHORT = "catchup_2026_09"
CATCHUP_MAX_POSITIONS = int(os.getenv("PORTFOLIO_CATCHUP_MAX_POSITIONS", "15"))
CATCHUP_MAX_RUN_PCT = 25.0


def _catchup_signal_dates() -> dict[str, object]:
    """company_master_id -> earliest catch-up signal date, for companies whose EVERY
    alert comes from a filing tagged with CATCHUP_COHORT."""
    df = sql_to_df(
        """
        SELECT a.company_master_id,
               bool_and(e.admission_cohort IS NOT DISTINCT FROM %s) AS all_catchup,
               min(e.disclosure_date) AS first_signal
          FROM fundamentals_l3_alerts a
          JOIN fundamentals_events e ON e.source = a.source AND e.news_id = a.news_id
         GROUP BY a.company_master_id
        """,
        params=(CATCHUP_COHORT,),
    )
    if df.empty:
        return {}
    df = df[df["all_catchup"].astype(bool)]
    return dict(zip(df["company_master_id"].astype(str), df["first_signal"]))


def _open_catchup_count() -> int:
    df = sql_to_df(
        "SELECT count(*) AS n FROM fundamentals_portfolio_position "
        " WHERE status = 'open' AND entry_decision = 'accept' AND entry_cohort = %s",
        params=(CATCHUP_COHORT,),
    )
    return int(df.iloc[0]["n"]) if not df.empty else 0


def _price_run_since(ticker: str, since) -> float | None:
    """% change in adjusted close from the session on/before `since` to the latest."""
    df = sql_to_df(
        "SELECT date, adj_close FROM advisory_adjusted_ohlcv_daily WHERE symbol = %s "
        "  AND date >= %s::date - interval '10 days' ORDER BY date",
        params=(ticker, str(since)),
    )
    if df.empty:
        return None
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    base = df[df["date"] <= pd.Timestamp(str(since))]
    if base.empty:
        return None
    return (float(df["adj_close"].iloc[-1]) / float(base["adj_close"].iloc[-1]) - 1) * 100


def _ensure_cohort_columns() -> None:
    with db_session() as (_conn, cur):
        cur.execute("ALTER TABLE fundamentals_portfolio_position ADD COLUMN IF NOT EXISTS entry_cohort TEXT")
        cur.execute("ALTER TABLE fundamentals_events ADD COLUMN IF NOT EXISTS admission_cohort TEXT")
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

    So a veto stands until the EVIDENCE moves.

    BUG FOUND IN THE 2026-09-23 DATA AUDIT: this used to compare the confluence run_date
    with the veto date. confluence_score writes a fresh row for every active name EVERY
    night, so from the day after any veto the date check always said "moved" -- the guard
    was a no-op. SHREEPUSHK was re-asked and re-vetoed 14 times in 14 sessions, and
    SAHYADRI/KAMDHENU/RANEHOLDIN were vetoed and later accepted on unchanged scorecards.
    Evidence now means CONTENT: the fingerprint of what the rule and the adjudicator
    actually weigh (scorer version, five axes, three counts, stage), compared with the
    fingerprint recorded on the last entry decision for the company.
    """
    if vetoed_at is None:
        return False        # cannot show anything changed -> the veto stands
    previous = candidate.get("_previous_fingerprint")
    if previous is None:
        return False        # no recorded evidence to compare with -> the veto stands
    return evidence_fingerprint(candidate) != previous


EVIDENCE_KEYS = ("score_version", "confluence_count", "contradicting_count", "evaluable_count", "stage")


# v3 (story score): the score in 10-point steps, its primary story, the stage and the latest
# story read's verdict. A continuous score moves a little every day; only a step counts as news.
V3_EVIDENCE_KEYS = ("score_version", "stage")


def evidence_fingerprint(candidate: dict) -> str | None:
    """Stable hash of the evidence a veto was given. None when the payload predates the
    fields (a pre-audit decision row), which the caller treats as "cannot show a change"."""
    if candidate.get("story_score") is not None:
        read = candidate.get("story_read") or {}
        material = {k: _as_int(candidate.get(k)) for k in V3_EVIDENCE_KEYS}
        material.update({"score_step": _as_int(float(candidate["story_score"]) // 10),
                         "primary": candidate.get("primary_dimension"),
                         "read": [read.get("direction"), _as_int(read.get("materiality"))]})
        return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:16]
    if any(candidate.get(k) is None for k in ("confluence_count", "contradicting_count", "evaluable_count")):
        return None
    axes = candidate.get("axes") or {}
    material = {k: _as_int(candidate.get(k)) for k in EVIDENCE_KEYS}
    material["axes"] = {k: _as_tristate(axes.get(k)) for k in sorted(axes)}
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:16]


def _as_tristate(value) -> bool | None:
    """True/False/None from whatever form an axis arrives in: a Python or numpy bool from
    pandas, NaN for SQL NULL, or -- in decision payloads written with default=str -- the
    STRINGS "True"/"False". Hashing those raw would make identical evidence differ, and
    the veto guard would silently stop guarding."""
    if value is None:
        return None
    if isinstance(value, str):
        return {"true": True, "false": False}.get(value.strip().lower())
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return bool(value)


def _as_int(value) -> int | None:
    try:
        return None if value is None or pd.isna(value) else int(value)
    except (TypeError, ValueError):
        return None


def _last_entry_fingerprints() -> dict[str, str]:
    """Fingerprint recorded on each company's most recent entry decision. Rows written
    before 2026-09-23 carry no fingerprint; it is recomputed from their payload, which
    holds the same fields (score_version is absent there and reads as None -- so the first
    v2 comparison differs and re-asks once, which is correct: the scorer changed)."""
    df = sql_to_df(
        "SELECT DISTINCT ON (company_master_id) company_master_id, payload_json "
        "  FROM fundamentals_portfolio_decision WHERE phase = 'entry' "
        " ORDER BY company_master_id, decided_at DESC"
    )
    out: dict[str, str] = {}
    for r in df.itertuples():
        try:
            payload = json.loads(r.payload_json or "{}")
        except (TypeError, ValueError):
            continue
        fp = payload.get("evidence_fingerprint") or evidence_fingerprint(payload)
        if fp is not None:
            out[str(r.company_master_id)] = fp
    return out


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


def _latest_price(ticker: str) -> tuple[float | None, object]:
    """(adj_close, bar date). The date is stored with the entry: positions opened
    2026-09-04..15 carry the PREVIOUS session's close (HESTERBIO entered at 2201.70, the
    09-03 close, on 09-04 when 09-04 closed at 2405.40) and nothing recorded that."""
    df = sql_to_df(
        "SELECT adj_close, date FROM advisory_adjusted_ohlcv_daily WHERE symbol = %s "
        "  AND date >= now() - make_interval(days => %s) "
        "ORDER BY date DESC LIMIT 1",
        params=(ticker, PRICE_LOOKBACK_DAYS),
    )
    if df.empty:
        return None, None
    return float(df.iloc[0]["adj_close"]), pd.Timestamp(df.iloc[0]["date"]).date()


def _size_for(candidate: dict, bucket: str = DEFAULT_BUCKET, weight: float | None = None) -> dict:
    """L5 sizing for a name about to be opened.

    get_position_size_recommendation() gates on an ALREADY-OPEN position, which this one
    is not yet, so the arithmetic is called directly. Falling back to the flat allocation
    when ADV is unknown matches compute_position_size's own rule: unknown liquidity is
    neither infinite nor zero.
    """
    from fundamentals.screens.l5_sizing import compute_position_size, load_adv_inputs

    adv = load_adv_inputs(candidate["company_master_id"])
    return compute_position_size(
        # The bucket's own share, not the flat per-position figure: a sleeve is a capital
        # envelope, so what one position may take is that envelope divided by how many
        # positions the sleeve is meant to hold.
        # v3: the name's target weight of the bucket (portfolio_v3.target_weights).
        target_capital_rs=(weight * float(bucket_config(bucket)["capital_rs"]) if weight
                           else capital_per_position_rs(bucket)),
        adv_value_rs=adv["adv_value_rs"] if adv else None,
    )


def _open_position(candidate: dict, verdict: dict, *, kind: str, dry_run: bool,
                   bucket: str = DEFAULT_BUCKET, entry_cohort: str | None = None,
                   ruleset_version: int | None = None, weight: float | None = None) -> dict:
    ruleset_version = V2_RULESET_VERSION if ruleset_version is None else ruleset_version
    price, price_date = _latest_price(candidate["ticker"])
    stop = candidate.get("stop") or {}
    sizing = _size_for(candidate, bucket, weight) if weight else _size_for(candidate, bucket)
    row = {
        "position_id": f"pos:{uuid.uuid4().hex[:16]}",
        "company_master_id": candidate["company_master_id"],
        "ticker": candidate["ticker"],
        "ruleset_version": ruleset_version,
        "kind": kind,
        # Which sleeve's capital this position spends. Recorded per position rather than
        # inferred from the engine that opened it, so a later review can ask "what did the
        # swing book do" without reconstructing it from dates and tickers.
        "bucket": bucket,
        "bucket_capital_rs": bucket_config(bucket)["capital_rs"],
        "status": "open",
        "entry_price": price,
        "entry_price_date": price_date,
        "stop_pct": stop.get("stop_pct"),
        "stop_basis": stop.get("basis"),
        "confluence_count": candidate["confluence_count"],
        "contradicting_count": candidate["contradicting_count"],
        "evaluable_count": candidate["evaluable_count"],
        "score_version": candidate.get("score_version"),
        # The exit compares against the baseline, never the frozen entry counts directly,
        # so a scorer-version change can re-baseline without restating what entry saw.
        "baseline_score_version": candidate.get("score_version"),
        "baseline_contradicting_count": candidate["contradicting_count"],
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
        "entry_cohort": entry_cohort,
    }
    if ruleset_version >= 3:
        row.update({"target_weight": weight, "story_score_at_entry": candidate.get("story_score"),
                    "primary_dimension_at_entry": candidate.get("primary_dimension"),
                    "sector_code": candidate.get("sector_code"), "daily_vol_pct": candidate.get("daily_vol_pct"),
                    "trail_high": price, "trim_count": 0})
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


def _close_replaced(position_id: str, exit_price: float | None) -> None:
    with db_session() as (_conn, cur):
        cur.execute(
            "UPDATE fundamentals_portfolio_position SET status = 'closed', closed_at = now(), "
            "       close_reason = 'replaced', exit_price = %s WHERE position_id = %s AND status = 'open'",
            (exit_price, position_id),
        )


def _close_superseded_veto(ticker: str, *, dry_run: bool) -> None:
    price, _ = _latest_price(ticker)
    if dry_run:
        return
    with db_session() as (_conn, cur):
        cur.execute(
            "UPDATE fundamentals_portfolio_position SET status = 'closed', closed_at = now(), "
            "       close_reason = 'superseded_by_accept', exit_price = %s "
            " WHERE ticker = %s AND status = 'open' AND entry_decision = 'reject'",
            (price, ticker),
        )


def run_portfolio(*, live: bool = False, dry_run: bool = False) -> dict[str, object]:
    _ensure_tables()
    _ensure_cohort_columns()
    evaluation = evaluate_entry_candidates()
    # An evaluation without a version is the pre-v3 contract (v1/v2 candidates, confluence).
    version = int(evaluation.get("ruleset_version") or V2_RULESET_VERSION)
    v3 = version >= 3

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
    # Only needed to judge a veto, so only read when some name carries one.
    fingerprints = _last_entry_fingerprints() if vetoed_at_by_ticker else {}
    kind = "real" if live else "shadow"
    entered, rejected, skipped, turned_away, re_vetoed, veto_stands = [], [], [], [], [], []
    stopped_out, superseded, below_liquidity_floor, outside_top = [], [], [], []

    # Only accepted positions consume capital, so only they fill the book, and the book
    # is the BUCKET's, not a global one: 100 x the old flat Rs 1 lakh happened to equal a
    # crore, but 25 x Rs 4 lakh is the same crore held in fewer, larger positions. Capping
    # on the old global count would let a bucket deploy four times its envelope.
    book_used = len(already_accepted)
    book_capacity = bucket_config(DEFAULT_BUCKET)["target_positions"] or MAX_POSITIONS
    catchup_signal = _catchup_signal_dates()
    catchup_open = _open_catchup_count() if catchup_signal else 0
    catchup_entered, catchup_cap_reached, catchup_price_ran = [], [], []
    # Strongest evidence first, so when a cap or the book binds it keeps the best names.
    if v3:
        candidates = sorted(evaluation["candidates"], key=lambda c: -float(c.get("story_score") or 0))
    else:
        candidates = sorted(evaluation["candidates"], key=lambda c: -int(c.get("confluence_count") or 0))
    weights: dict[str, float] = {}
    replaced: list[dict] = []
    v3_book = None
    if v3:
        from fundamentals.screens import portfolio_v3

        # Weights over the book as it could be today: holdings (live score) and candidates.
        v3_book = portfolio_v3.open_v3_book()
        pool = [{"company_master_id": r.company_master_id, "story_score": r.story_score,
                 "daily_vol_pct": r.daily_vol_pct, "sector_code": r.sector_code} for r in v3_book.itertuples()]
        held = {p["company_master_id"] for p in pool}
        pool += [{k: c.get(k) for k in ("company_master_id", "story_score", "daily_vol_pct", "sector_code")}
                 for c in candidates if c["company_master_id"] not in held]
        weights = portfolio_v3.target_weights(pool, book_capacity)
        today_ist = (pd.Timestamp.now(tz="UTC") + pd.Timedelta(hours=5, minutes=30)).normalize().tz_localize(None)

    for candidate in candidates:
        if candidate["ticker"] in already_accepted:
            skipped.append(candidate["ticker"])
            continue
        cohort = CATCHUP_COHORT if str(candidate["company_master_id"]) in catchup_signal else None
        if cohort:
            if catchup_open >= CATCHUP_MAX_POSITIONS:
                catchup_cap_reached.append(candidate["ticker"])
                continue
            ran = _price_run_since(candidate["ticker"], catchup_signal[str(candidate["company_master_id"])])
            if ran is not None and ran > CATCHUP_MAX_RUN_PCT:
                catchup_price_ran.append(candidate["ticker"])
                continue
        if candidate["ticker"] in stopped_recently:
            # Stopped out inside the cooldown. Skipping BEFORE adjudication also saves the
            # call, same as the veto branch below. Named, not silent: "we would not buy it"
            # and "we were not allowed to buy it yet" mean different things.
            stopped_out.append(candidate["ticker"])
            continue
        min_adv = bucket_config(DEFAULT_BUCKET).get("min_adv_rs")
        if min_adv:
            # The bucket's liquidity floor was configured but never applied (2026-09-23
            # audit). Checked before adjudication, like the other skips, and named.
            from fundamentals.screens.l5_sizing import load_adv_inputs

            adv = load_adv_inputs(candidate["company_master_id"])
            if adv is None or adv["adv_value_rs"] < min_adv:
                below_liquidity_floor.append(candidate["ticker"])
                continue
        vetoed_at = vetoed_at_by_ticker.get(candidate["ticker"])
        candidate["_previous_fingerprint"] = fingerprints.get(str(candidate["company_master_id"]))
        candidate["evidence_fingerprint"] = evidence_fingerprint(candidate)
        if vetoed_at is not None and not _evidence_moved_since(candidate, vetoed_at):
            # Vetoed, and nothing new has arrived. Do not re-ask -- see
            # _evidence_moved_since. Skipping BEFORE adjudication also saves the call.
            veto_stands.append(candidate["ticker"])
            continue
        if v3 and candidate["company_master_id"] not in weights:
            # Not among the book's best `book_capacity` names by score: no weight, no call.
            outside_top.append(candidate["ticker"])
            continue
        verdict = adjudicate_entry(candidate)
        # Recorded BEFORE any book/duplicate branch: the adjudication happened and cost a
        # call, so the reasoning is kept even when no position follows from it.
        if not dry_run:
            _record_decision(
                phase="entry",
                company_master_id=candidate["company_master_id"],
                ruleset_version=version,
                decision=verdict["decision"],
                reason=verdict.get("reason"),
                model=verdict.get("model"),
                payload={k: v for k, v in candidate.items() if k != "_previous_fingerprint"},
            )
        if verdict["decision"] == "accept":
            if book_used >= book_capacity and v3:
                # v3 replacement (PRD 5.2): only for a clearly stronger story, never churning a
                # young holding or one waiting on the tax guard.
                weakest = portfolio_v3.weakest_replaceable(
                    v3_book[~v3_book["position_id"].isin([r["position_id"] for r in replaced])],
                    float(candidate["story_score"]), today_ist)
                if weakest is None:
                    turned_away.append(candidate["ticker"])
                    continue
                price_w, _ = _latest_price(weakest["ticker"])
                if not dry_run:
                    _close_replaced(weakest["position_id"], price_w)
                    portfolio_v3.record_counterfactual(weakest["position_id"], "replaced", weakest["ticker"], price_w)
                    _record_decision(phase="exit", company_master_id=weakest["company_master_id"],
                                     ruleset_version=version, decision="exit",
                                     reason=(f"[replaced] by {candidate['ticker']} (story {candidate['story_score']:.0f} vs "
                                             f"{float(weakest['score']):.0f}, margin {portfolio_v3.REPLACE_MARGIN:g})"),
                                     model=None, payload={"replaced_by": candidate["company_master_id"]})
                replaced.append({"position_id": weakest["position_id"], "ticker": weakest["ticker"],
                                 "by": candidate["ticker"]})
                book_used -= 1
            if book_used >= book_capacity:
                # Named, not silent. "The book was full" and "the rule found nothing" look
                # identical in a position count and mean opposite things.
                turned_away.append(candidate["ticker"])
                continue
            if candidate["ticker"] in vetoed_at_by_ticker:
                # The same company must not sit in BOTH arms of the paired comparison
                # (SAHYADRI and KAMDHENU did, 2026-09). The vetoed shadow is closed at
                # today's price under its own reason, so its outcome up to now is kept
                # and it stops accruing the accepted arm's future.
                _close_superseded_veto(candidate["ticker"], dry_run=dry_run)
                superseded.append(candidate["ticker"])
            _open_position(candidate, verdict, kind=kind, dry_run=dry_run,
                           **({"ruleset_version": version, "weight": weights.get(candidate["company_master_id"])} if v3 else {}),
                           **({"entry_cohort": cohort} if cohort else {}))
            entered.append(candidate["ticker"])
            book_used += 1
            if cohort:
                catchup_entered.append(candidate["ticker"])
                catchup_open += 1
        else:
            if candidate["ticker"] in vetoed_at_by_ticker:
                # Same name, vetoed again. The decision is recorded above; writing a second
                # identical shadow every night would inflate the vetoed arm of the paired
                # comparison with duplicates of one company.
                re_vetoed.append(candidate["ticker"])
                continue
            # Rejected -> shadow, always. This is the measurement, not bookkeeping.
            _open_position(candidate, verdict, kind="shadow", dry_run=dry_run,
                           **({"ruleset_version": version, "weight": weights.get(candidate["company_master_id"])} if v3 else {}),
                           **({"entry_cohort": cohort} if cohort else {}))
            rejected.append(candidate["ticker"])
            vetoed_at_by_ticker[candidate["ticker"]] = pd.Timestamp.now(tz="UTC")

    return {
        "source": SYNC_SOURCE_NAME,
        "status": "ok",
        "mode": "live" if live else "record-only",
        "dry_run": dry_run,
        "ruleset_version": version,
        "evaluated": evaluation["evaluated"],
        "candidates": len(evaluation["candidates"]),
        "entered": len(entered),
        "rejected": len(rejected),
        "already_open_skipped": len(skipped),
        "entered_tickers": entered,
        "rejected_tickers": rejected,
        # The bucket's figure -- what _size_for actually used. CAPITAL_PER_POSITION_RS is
        # the pre-bucket flat Rs 1 lakh and reported the wrong number since 2026-09-23.
        "capital_per_position_rs": capital_per_position_rs(DEFAULT_BUCKET),
        "book_used": book_used,
        "book_capacity": book_capacity,
        "bucket": DEFAULT_BUCKET,
        "turned_away_book_full": turned_away,
        "re_vetoed_no_duplicate": re_vetoed,
        # Vetoed previously and NOT re-asked, because no new evidence has arrived.
        "veto_stands_no_new_evidence": veto_stands,
        # Stopped out within STOP_COOLDOWN_DAYS, so not re-entered at the stop price.
        "stopped_recently_skipped": stopped_out,
        "vetoed_shadow_superseded_by_accept": superseded,
        "below_bucket_liquidity_floor": below_liquidity_floor,
        "stale_confluence_skipped": evaluation.get("stale_confluence_skipped", []),
        "catchup_entered": catchup_entered,
        "catchup_cap_reached": catchup_cap_reached,
        "catchup_price_already_ran": catchup_price_ran,
        # v3: holdings closed to make room for a clearly stronger story, and candidates outside
        # the book's best names by score (never adjudicated).
        "replaced": replaced,
        "outside_top_by_score": outside_top,
        "target_weights": weights,
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
