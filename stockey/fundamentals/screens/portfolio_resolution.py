"""Machine forecast resolution and scoring -- docs/PORTFOLIO_RULESET_PRD.md.

Replaces the human forecast register (`fundamentals_l4_thesis`, deleted 2026-09-04 at the
operator's instruction: "remove the human forecast completely"). Nothing here waits on a
person. A forecast is created by the entry adjudicator, resolved here at its target date,
and scored -- end to end, unattended.

WHY DELETING THE HUMAN RESOLVER FORCED THIS MODULE TO EXIST. The old design's one human
act was resolution: a person decided whether a prediction came true and, when it did not,
whether it was `thesis_wrong` or `thesis_right_market_hasnt_paid` (the source spec is
explicit these "look identical in P&L and demand opposite corrections"). Remove the human
and put nothing in their place and the register simply never resolves -- the calibration
measure does not break loudly, it just reports "0 resolved" forever.

THE SELF-GRADING PROBLEM, AND WHAT IS DONE ABOUT IT. A model that both writes and grades
its own forecasts will report a flattering hit rate, and that number is worse than no
number because it looks like evidence. So:

  1. The entry adjudicator must reduce its prediction to a machine-checkable comparison
     against a real fundamentals_l2_state column wherever it honestly reduces. Those
     resolve MECHANICALLY -- the model's prose is not consulted at all.
  2. Where it does not reduce, a judged read happens, but `resolution_method` records
     which path was taken and every hit rate is reported SPLIT by it.

If the judged hit rate runs well above the mechanical one, that gap is the self-grading
bias, made visible rather than argued about. It is a measurement, not a guarantee.

FORECAST RESOLUTION IS INDEPENDENT OF POSITION CLOSURE, deliberately. A name stopped out
in month two still has a forecast that resolves at its target date. Collapsing the two
would destroy the only distinction that matters for correction: whether the fundamental
call was wrong, or right and unpaid.
"""
from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd
from environs import Env
from openai import OpenAI

from fundamentals.screens.portfolio_adjudicator import _ensure_tables, _record_decision
from fundamentals.screens.portfolio_exit import PRICE_LOOKBACK_DAYS
from utils.company_master import build_l1_ticker_by_company_master_id
from utils.db import db_session, sql_to_df

env = Env()
env.read_env()

SYNC_SOURCE_NAME = "fundamentals.screens.portfolio_resolution"


def _ist_today():
    """The market's calendar day, not the server's.

    Everything else in this repo anchors on IST (see data/bseindia/bhavcopy.py). Between
    18:30 UTC and midnight UTC the two differ, so a system-local date would resolve a
    forecast a day early or late depending on when the job happened to run.
    """
    return (pd.Timestamp.now(tz="UTC") + pd.Timedelta(hours=5, minutes=30)).date()
DEFAULT_MODEL = env("PORTFOLIO_ADJUDICATOR_MODEL", "gpt-5.4-mini")
RESOLVER_PROMPT_VERSION = 1

# Below this, a breakdown group reports hit_rate None rather than a number. A hit rate off
# two samples is noise wearing a percentage sign.
MIN_SAMPLE_SIZE_FOR_BREAKDOWN = 5

FAILURE_ATTRIBUTIONS = ("thesis_wrong", "thesis_right_market_hasnt_paid")

RESOLVER_SYSTEM_PROMPT = """You grade a fundamental forecast that was committed to months \
ago. You did not write it and you may not reinterpret it.

You are given the prediction as written, the company's current L2 fundamental state, and \
the date the forecast was made. Answer one question: as stated, did it come true?

Judge the prediction AS WRITTEN. If it was vague, that is a fact about the forecast and \
counts against it -- do not resolve a vague prediction charitably, and do not substitute a \
weaker claim the evidence does happen to support. If the available state genuinely cannot \
settle it either way, say so via resolved_true = null rather than guessing; an honest \
"unresolvable" is data about forecast quality, a guess is not.

Do not consider whether the position made money. That is a different question and it is \
answered elsewhere."""

RESOLVER_SCHEMA = {
    "type": "object",
    "properties": {
        "resolved_true": {"type": ["boolean", "null"]},
        "reason": {"type": "string"},
    },
    "required": ["resolved_true", "reason"],
    "additionalProperties": False,
}


def _latest_l2_state(company_master_id: str):
    """Latest L2 row for a company, with its run_date. None if there is none."""
    ticker = build_l1_ticker_by_company_master_id().get(company_master_id)
    if ticker is None:
        return None
    df = sql_to_df(
        "SELECT * FROM fundamentals_l2_state WHERE ticker = %s ORDER BY run_date DESC LIMIT 1",
        params=(ticker,),
    )
    return None if df.empty else df.iloc[0]


def l2_is_newer_than_forecast(row: dict) -> tuple[bool, str]:
    """Has the fundamental state actually MOVED since the forecast was written?

    THE BUG THIS EXISTS TO PREVENT (found in review, 2026-09-04, before any forecast had
    resolved). Resolution reads the LATEST L2 row, and L2 refreshes on the screener's own
    cadence -- on the 2026-09-04 book every L2 row predated its forecast by ~15 days. So a
    forecast would have been graded against THE VERY DATA THE ADJUDICATOR READ WHEN
    WRITING IT. That is not merely circular, it is systematically wrong in one direction:
    these forecasts predict a CHANGE ("net debt keeps falling"), so scoring them against
    the pre-forecast snapshot marks them wrong before the company has reported anything.
    Measured on BALPHARMA it returned False on a +9 reading the adjudicator had already
    seen and was predicting would reverse. Left in, the mechanical hit rate -- the one
    number this design trusts precisely because a model cannot influence it -- would have
    converged on zero and looked like evidence.

    Compared by DATE, and strictly: an L2 row crawled the same day carries the same
    reporting period.
    """
    state = _latest_l2_state(row.get("company_master_id"))
    if state is None:
        return False, "no L2 state for this company at all"
    run_date = state.get("run_date")
    opened_at = row.get("opened_at")
    if run_date is None or opened_at is None:
        return False, "cannot establish whether L2 state postdates the forecast"
    run_d, open_d = pd.Timestamp(run_date).date(), pd.Timestamp(opened_at).date()
    if run_d <= open_d:
        return False, (f"L2 state has not refreshed since the forecast was made "
                       f"(state {run_d}, forecast {open_d}) -- resolving now would grade "
                       f"the prediction against the data that produced it")
    return True, f"L2 state {run_d} postdates the forecast {open_d}"


def check_structured_prediction(row: dict) -> bool | None:
    """Evaluate a structured (metric_name, metric_operator, metric_threshold) forecast
    against current L2 state. None means "cannot be evaluated", never False.

    Salvaged from the deleted l4_thesis.py, which accumulated four separate live bug fixes
    that are worth keeping rather than rediscovering:

    - BUG (2026-08-18): naive removeprefix("nse:") only recovers the right
      fundamentals_l2_state.ticker when the company IS its own NSE symbol -- wrong for the
      ~22% BSE-only cohort. Use build_l1_ticker_by_company_master_id().
    - BUG (2026-08-17): metric_name named a NON-NUMERIC L2 column (trend_direction,
      company_name, ...) and `value < threshold` raised an uncaught str-vs-number
      TypeError instead of the graceful None every other branch returns.
    - BUG (2026-08-18): the bool guard leaked numpy.bool_ -- isinstance(v, bool) is False
      for numpy's boolean scalar, so a pandas boolean column sailed through and got
      coerced to 1.0/0.0 and compared as a real metric. np.bool_ is checked explicitly.
    - float(value), not isinstance(value, (int, float)): a genuinely numeric L2 column
      arrives as numpy.int64/float64, neither of which subclasses Python int/float, so an
      isinstance check alone would wrongly reject real values.
    """
    metric_name = row.get("metric_name")
    operator = row.get("metric_operator")
    threshold = row.get("metric_threshold")
    if not metric_name or not operator or threshold is None:
        return None

    # Never grade against state that predates the forecast -- see l2_is_newer_than_
    # forecast. Callers that already checked pay only a cached-ish second read; callers
    # that did not are protected anyway, which is the point of putting it here.
    if "opened_at" in row and not l2_is_newer_than_forecast(row)[0]:
        return None
    state = _latest_l2_state(row.get("company_master_id"))
    if state is None or metric_name not in state.index:
        return None
    value = state[metric_name]
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (bool, str, np.bool_)):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None

    if operator == "<":
        return bool(value < threshold)
    if operator == "<=":
        return bool(value <= threshold)
    if operator == ">":
        return bool(value > threshold)
    if operator == ">=":
        return bool(value >= threshold)
    if operator == "==":
        return bool(value == threshold)
    return None


def _judge_prediction(row: dict, *, model: str = DEFAULT_MODEL) -> dict:
    """Judged fallback for a forecast that never reduced to an L2 comparison.

    Never raises: an unreachable resolver must leave the forecast OPEN for the next run,
    not silently mark it false. A failed grading is missing data, not a failed forecast.
    """
    ticker = build_l1_ticker_by_company_master_id().get(row.get("company_master_id"))
    state = {}
    if ticker:
        df = sql_to_df(
            "SELECT * FROM fundamentals_l2_state WHERE ticker = %s ORDER BY run_date DESC LIMIT 1",
            params=(ticker,),
        )
        if not df.empty:
            state = {k: v for k, v in df.iloc[0].to_dict().items()
                     if not isinstance(v, (bytes, bytearray))}
    payload = {
        "prediction": row.get("prediction_text"),
        "invalidation_criteria": row.get("invalidation_criteria"),
        "forecast_made_on": row.get("opened_at"),
        "target_date": row.get("target_date"),
        "current_l2_state": state,
    }
    try:
        client = OpenAI(api_key=env("OPENAI_API_KEY"))
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": RESOLVER_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, default=str)},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "forecast_resolution", "schema": RESOLVER_SCHEMA,
                                "strict": True},
            },
        )
        result = json.loads(response.choices[0].message.content)
    except Exception as exc:  # noqa: BLE001 -- see docstring
        return {"resolved_true": None,
                "reason": f"resolver unavailable ({type(exc).__name__}); left open for the next run"}
    return result


def _price_outcome(row: dict) -> float | None:
    """Return since entry, as a fraction. Uses the realised exit price for a closed
    position and the latest adjusted close for one still open."""
    entry = row.get("entry_price")
    if not entry:
        return None
    exit_price = row.get("exit_price")
    if exit_price is None or pd.isna(exit_price):
        df = sql_to_df(
            "SELECT adj_close FROM advisory_adjusted_ohlcv_daily WHERE symbol = %s "
            "  AND date >= now() - make_interval(days => %s) "
            "ORDER BY date DESC LIMIT 1",
            params=(row.get("ticker"), PRICE_LOOKBACK_DAYS),
        )
        if df.empty or df.iloc[0]["adj_close"] is None:
            return None
        exit_price = float(df.iloc[0]["adj_close"])
    return float(exit_price) / float(entry) - 1


def _attribute_failure(resolved_true: bool, price_return: float | None) -> str | None:
    """The distinction the source spec calls out as demanding opposite corrections.

    Now derived rather than judged: the forecast outcome and the price outcome are both
    known, and their disagreement IS the attribution. A wrong call is thesis_wrong however
    the price behaved -- a wrong forecast that happened to make money is still a wrong
    forecast, and recording it as a success is how a process learns the opposite of the
    truth.
    """
    if not resolved_true:
        return "thesis_wrong"
    if price_return is not None and price_return <= 0:
        return "thesis_right_market_hasnt_paid"
    return None


def load_due_forecasts(*, as_of_date=None) -> pd.DataFrame:
    as_of_date = as_of_date or _ist_today()
    return sql_to_df(
        """
        SELECT * FROM fundamentals_portfolio_position
         WHERE target_date IS NOT NULL
           AND target_date <= %s
           AND resolution_date IS NULL
         ORDER BY target_date ASC
        """,
        params=(as_of_date,),
    )


def _write_resolution(position_id, resolved_true, method, notes, attribution) -> None:
    with db_session() as (_conn, cur):
        cur.execute(
            "UPDATE fundamentals_portfolio_position "
            "   SET resolved_true = %s, resolution_date = now()::date, resolution_method = %s, "
            "       resolution_notes = %s, failure_attribution = %s "
            " WHERE position_id = %s",
            (resolved_true, method, notes, attribution, position_id),
        )


def resolve_due_forecasts(*, as_of_date=None, dry_run: bool = False) -> dict[str, object]:
    _ensure_tables()
    due = load_due_forecasts(as_of_date=as_of_date)
    resolved, unresolved = [], []

    for row in due.to_dict("records"):
        # Cheapest guard first, and it also saves a pointless LLM call: if the fundamental
        # state has not moved since the forecast, NEITHER path can honestly settle it.
        fresh, freshness_note = l2_is_newer_than_forecast(row)
        if not fresh:
            unresolved.append({"ticker": row["ticker"], "why": freshness_note})
            continue

        mechanical = check_structured_prediction(row)
        if mechanical is not None:
            outcome, method = mechanical, "mechanical"
            notes = (f"{row['metric_name']} {row['metric_operator']} "
                     f"{row['metric_threshold']} evaluated against current L2 state")
        else:
            judged = _judge_prediction(row)
            outcome, method = judged.get("resolved_true"), "judged"
            notes = judged.get("reason")
            if row.get("metric_name"):
                # Had a metric but it could not be read -- a missing L2 row, not a
                # forecast that failed. Worth distinguishing from never having one.
                method = "judged_after_metric_unreadable"

        if outcome is None:
            unresolved.append({"ticker": row["ticker"], "why": notes})
            continue

        price_return = _price_outcome(row)
        attribution = _attribute_failure(bool(outcome), price_return)
        if not dry_run:
            _write_resolution(row["position_id"], bool(outcome), method, notes, attribution)
            _record_decision(
                phase="exit", company_master_id=row["company_master_id"],
                ruleset_version=row["ruleset_version"],
                decision=f"forecast_{'true' if outcome else 'false'}",
                reason=f"[{method}] {notes}", model=None,
                payload={"position_id": row["position_id"], "price_return": price_return,
                         "failure_attribution": attribution},
            )
        resolved.append({"ticker": row["ticker"], "resolved_true": bool(outcome),
                         "method": method, "failure_attribution": attribution})

    return {
        "source": SYNC_SOURCE_NAME, "status": "ok", "dry_run": dry_run,
        "due": int(len(due)), "resolved": len(resolved), "left_open": len(unresolved),
        "resolved_detail": resolved, "unresolved_detail": unresolved,
    }


def _hit_rate_breakdown(resolved: pd.DataFrame, column: str) -> dict[str, dict[str, object]]:
    """Hit rate per group, with the sample size always shown.

    A group below MIN_SAMPLE_SIZE_FOR_BREAKDOWN reports hit_rate None rather than being
    hidden, so a thin sample is visibly thin instead of absent -- absence reads as "no
    data collected", thinness reads as "not enough yet", and they call for different
    responses.
    """
    out: dict[str, dict[str, object]] = {}
    if resolved.empty or column not in resolved.columns:
        return out
    for key, group in resolved.fillna({column: "none"}).groupby(column):
        n = int(len(group))
        out[str(key)] = {
            "count": n,
            "hit_rate": (round(float(group["resolved_true"].mean()) * 100, 1)
                         if n >= MIN_SAMPLE_SIZE_FOR_BREAKDOWN else None),
        }
    return out


def compute_portfolio_scoring(*, as_of_date=None) -> dict[str, object]:
    """Forecast accuracy, not returns -- the primary outcome measure.

    Every breakdown is reported SPLIT BY resolution_method. A single blended hit rate
    would let judged resolutions (where the model grades prose) flatter the mechanical
    ones (where data decides), and the whole point of separating the two paths is that the
    gap between them is visible.

    hit_rate_by_entry_decision is the PRD's paired comparison: accepted names versus the
    ones the adjudicator vetoed, under one ruleset over one period. It is the only thing
    that can say whether the veto layer adds value or destroys it.
    """
    as_of_date = as_of_date or _ist_today()
    positions = sql_to_df("SELECT * FROM fundamentals_portfolio_position")
    empty = {
        "as_of_date": str(as_of_date), "total_forecasts": 0, "open": 0, "resolved": 0,
        "hit_rate": None, "hit_rate_by_resolution_method": {},
        "hit_rate_by_entry_decision": {}, "hit_rate_by_confluence_count": {},
        "failure_attribution_breakdown": {}, "time_to_confirmation_days": {},
    }
    if positions.empty:
        return empty

    resolved = positions[positions["resolution_date"].notna()]
    if resolved.empty:
        return {**empty, "total_forecasts": int(len(positions)), "open": int(len(positions))}

    hit_rate = round(float(resolved["resolved_true"].mean()) * 100, 1)
    false_resolved = resolved[resolved["resolved_true"] == False]  # noqa: E712
    failure_breakdown = false_resolved["failure_attribution"].value_counts().to_dict()

    time_to_confirmation: dict[str, object] = {}
    true_resolved = resolved[resolved["resolved_true"] == True]  # noqa: E712
    if not true_resolved.empty:
        days = (pd.to_datetime(true_resolved["resolution_date"])
                - pd.to_datetime(true_resolved["opened_at"]).dt.tz_localize(None)).dt.days
        time_to_confirmation = {"median_days": float(days.median()),
                                "mean_days": round(float(days.mean()), 1),
                                "count": int(len(days))}

    scored = resolved.copy()
    scored["confluence_count"] = scored["confluence_count"].apply(
        lambda v: str(int(v)) if pd.notna(v) else None)
    return {
        "as_of_date": str(as_of_date),
        "total_forecasts": int(len(positions)),
        "open": int(len(positions) - len(resolved)),
        "resolved": int(len(resolved)),
        "hit_rate": hit_rate,
        "hit_rate_by_resolution_method": _hit_rate_breakdown(scored, "resolution_method"),
        "hit_rate_by_entry_decision": _hit_rate_breakdown(scored, "entry_decision"),
        "hit_rate_by_confluence_count": _hit_rate_breakdown(scored, "confluence_count"),
        "failure_attribution_breakdown": failure_breakdown,
        "time_to_confirmation_days": time_to_confirmation,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve due machine forecasts and score them.")
    parser.add_argument("--dry-run", action="store_true", help="Decide but write nothing.")
    args = parser.parse_args(argv)
    out = resolve_due_forecasts(dry_run=args.dry_run)
    out["scoring"] = compute_portfolio_scoring()
    print(json.dumps(out, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
