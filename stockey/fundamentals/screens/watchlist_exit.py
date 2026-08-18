"""Watchlist exit signals -- fundamental screener step 10.5 (2026-08-13, user
request: "for every signal we need to create a timeout or another signal or price
at which we need to remove it from the watchlist. This is as important as entry
signal else we will crowd the watchlist with no outcomes").

fundamentals/screens/watchlist.py's sync_watchlist_from_alerts() is purely
additive -- a company added once stays on the watchlist forever, no matter how
stale the original alert becomes. This module is the missing other half: three
independent exit conditions, evaluated per company, first match wins (priority
order below). NEVER deletes a row -- soft-status only (status/status_reason/
status_updated_at columns, this module's own bootstrap ALTER, same "each module
owns the columns it writes" convention watch_summary.py's own narrative columns
already use). Full history stays queryable; default views (fundamentals/api/
queries.py's get_watchlist(), the daily digest) just filter to status='active' by
default, which is what actually solves "crowding" without losing anything.

Three conditions, checked in this priority order (most specific/strongest evidence
first):

1. invalidated -- a LATER alert directly contradicts the trigger_type that
   originally justified watching this company (INVALIDATING_TRIGGER_TYPES below).
   Strongest evidence: a real, new, specific event saying the opposite of the
   original story, not just the absence of news.
2. price_flagged -- price has moved far enough (either direction) since
   first_seen_price that the original story likely needs a fresh human look: a
   large rally suggests the thesis may already be priced in, a large decline
   suggests it isn't playing out. Descriptive framing only -- this is a "look
   again" flag, never a sell/buy signal (see fundamental_basic_goal.md's own
   "never a price target" rule watch_summary.py already applies).
3. stale -- suggested_watch_until (the LLM's own estimate, watch_summary.py,
   previously computed and stored but never acted on) has passed with nothing new
   since it was set. Weakest evidence: mere passage of time, not a new fact.

A company with none of these becomes/stays 'active'. Re-evaluated every pipeline
run -- a company already marked invalidated/stale/price_flagged whose situation
later resolves (e.g. a fresh alert regenerates the narrative and pushes
suggested_watch_until forward) is NOT automatically reactivated here; the fresh
alert already re-enters it into the "worth a look" evidence trail via alert_count/
last_alert_at, and a human reviewing an old flagged entry can already see the new
alert in its evidence trail -- reactivation logic is a possible follow-up, not
built now (matches this pipeline's own "first-cut, tune once reviewed" precedent
elsewhere, not silently pretended to be solved)."""

from __future__ import annotations

import json

import pandas as pd

from fundamentals.screens.watchlist import _ensure_watchlist_table, load_price_near
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db

SYNC_SOURCE_NAME = "fundamentals.screens.watchlist_exit"
STOCKEY_RUN_STATE: dict[str, object] = {}

# Direct contradictions only -- deliberately NOT exhaustive. A trigger_type not
# listed here (institutional_first_entry, capital_raise, auditor_change,
# related_party_transaction, results_delayed, llm_flagged) has no single natural
# opposite in this trigger set; forcing one would be inventing a relationship the
# data doesn't actually support, the same trap this codebase avoids everywhere else
# (e.g. investor_classification.py's own "unknown" tier being an honest default
# rather than a guessed skill rating).
INVALIDATING_TRIGGER_TYPES: dict[str, set[str]] = {
    "rating_confirms_deleveraging": {"rating_downgrade"},
    "results_confirm_turnaround": {"results_decline"},
    "insider_buy": {"insider_sell_surprise"},
}

# First-cut, undocumented-in-any-spec placeholders, same "easy to tune once
# reviewed" treatment every other first-cut threshold in this pipeline gets.
PRICE_RALLY_THRESHOLD_PCT = 50.0
PRICE_DECLINE_THRESHOLD_PCT = -30.0

_STATUS_COLUMNS_STATEMENT = """
    ALTER TABLE fundamentals_watchlist ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active'
"""


def _bootstrap_status_columns() -> None:
    _ensure_watchlist_table()

    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute(_STATUS_COLUMNS_STATEMENT)
            cur.execute("ALTER TABLE fundamentals_watchlist ADD COLUMN IF NOT EXISTS status_reason TEXT")
            cur.execute("ALTER TABLE fundamentals_watchlist ADD COLUMN IF NOT EXISTS status_updated_at TIMESTAMPTZ")

    execute_db_operation(_op, operation_name="fundamentals_watchlist:ensure_status_columns")


def load_watchlist_for_exit_evaluation() -> pd.DataFrame:
    """Every current watchlist row plus the two fields evaluate_exit_status needs
    that aren't already on fundamentals_watchlist: current price (fundamentals_
    technicals, same source get_watchlist()'s own query already uses) and
    first_seen_price/first_seen_at/suggested_watch_until/narrative_generated_at/
    last_alert_at, already columns on the table itself.

    first_seen_at is included so the caller can re-derive first_seen_price live
    (see run_watchlist_exit_evaluation) rather than trust the frozen stored value
    -- confirmed live 2026-08-14: both first_seen_price and current_price are
    adj_close from advisory_adjusted_ohlcv_daily, but first_seen_price is written
    ONCE at watchlist-add time and never refreshed, while current_price is
    recomputed daily from the latest cum_adj_factor. A split/bonus for a
    watchlisted symbol AFTER it was first seen changes cum_adj_factor for every
    date strictly before the event -- current_price picks that up immediately,
    the frozen first_seen_price does not, silently producing a wrong change_pct
    (and therefore a wrong/missed price_flagged exit) the first time this happens
    to any watchlisted name.

    latest_alert_load_ts (2026-08-18 re-audit fix) is the wall-clock time the most
    recent alert was actually WRITTEN (fundamentals_l3_alerts.load_ts), for
    _check_stale's own "has a fresher alert landed since the narrative" guard --
    see that function's docstring for why comparing it against last_alert_at (a
    filing's disclosure date) instead was the same event-date-vs-processing-time
    bug watch_summary.py's narrative refresh gate had."""
    return sql_to_df(
        """
        SELECT w.company_master_id, w.first_seen_price, w.first_seen_at, w.last_alert_at,
               w.suggested_watch_until, w.narrative_generated_at,
               tech.close AS current_price, tech.price_data_stale,
               a.latest_alert_load_ts
        FROM fundamentals_watchlist w
        LEFT JOIN LATERAL (
            SELECT close, price_data_stale FROM fundamentals_technicals
            WHERE company_master_id = w.company_master_id
            ORDER BY run_date DESC LIMIT 1
        ) tech ON TRUE
        LEFT JOIN LATERAL (
            SELECT MAX(load_ts) AS latest_alert_load_ts
            FROM fundamentals_l3_alerts
            WHERE company_master_id = w.company_master_id
        ) a ON TRUE
        """
    )


def load_trigger_type_history_by_company() -> dict[str, list[dict]]:
    """company_master_id -> every (trigger_type, alert_date) this company has ever
    had, oldest first -- bulk query (all companies at once), not per-company, same
    "bulk-load once" pattern fundamentals/screens/l3_triggers.py's own l2_by_ticker
    already uses. Needed for the invalidation check: was the trigger_type that
    justified watching this company later contradicted by a LATER alert."""
    df = sql_to_df(
        """
        SELECT company_master_id, trigger_type, alert_date
        FROM fundamentals_l3_alerts
        WHERE company_master_id IS NOT NULL
        ORDER BY company_master_id, alert_date ASC NULLS LAST
        """
    )
    if df.empty:
        return {}
    by_company: dict[str, list[dict]] = {}
    for row in df.to_dict("records"):
        by_company.setdefault(row["company_master_id"], []).append(row)
    return by_company


def _check_invalidated(trigger_history: list[dict]) -> str | None:
    """None if not invalidated, else a human-readable reason. Checks every
    (original, later) pair in chronological order -- the first genuine contradiction
    found wins, not the most recent one, since the ORIGINAL story being contradicted
    is what matters, not which pair happens to sort last."""
    for i, original in enumerate(trigger_history):
        opposing = INVALIDATING_TRIGGER_TYPES.get(original["trigger_type"])
        if not opposing:
            continue
        for later in trigger_history[i + 1 :]:
            if later["trigger_type"] in opposing:
                return (
                    f"{later['trigger_type']} ({later.get('alert_date')}) contradicts the earlier "
                    f"{original['trigger_type']} ({original.get('alert_date')}) this company was watchlisted for."
                )
    return None


def _check_price_flagged(first_seen_price, current_price, *, price_data_stale=False) -> str | None:
    """None if not flagged, else a human-readable reason. None (not a guessed 0%)
    when either price is missing -- same discipline every price computation in this
    pipeline already applies.

    BUG FOUND LIVE 2026-08-15, fixed here: current_price used to be trusted at face
    value even when fundamentals_technicals.price_data_stale says the underlying
    price feed hasn't updated in weeks (or, in one real case, years) -- confirmed
    live, a stale current_price anchored to an old value made a real price move look
    like ~0% change, masking exactly the "look again" signal this check exists to
    raise. A stale price can't be trusted to judge a real move either way, so this
    now declines to flag OR clear a flag from it -- same as the existing
    missing-price case just above, not a new failure mode."""
    if price_data_stale:
        return None
    if not isinstance(first_seen_price, (int, float)) or not isinstance(current_price, (int, float)) or first_seen_price == 0:
        return None
    change_pct = round((current_price - first_seen_price) / abs(first_seen_price) * 100, 2)
    if change_pct >= PRICE_RALLY_THRESHOLD_PCT:
        return f"Price up {change_pct}% since first seen -- the original story may already be priced in, worth a fresh look."
    if change_pct <= PRICE_DECLINE_THRESHOLD_PCT:
        return f"Price down {abs(change_pct)}% since first seen -- the original thesis doesn't appear to be playing out, worth a fresh look."
    return None


def _check_stale(suggested_watch_until, latest_alert_load_ts, narrative_generated_at, *, today) -> str | None:
    """None if not stale, else a human-readable reason. "Nothing new since" is
    checked as latest_alert_load_ts <= narrative_generated_at -- if a fresh alert
    had landed after the narrative that produced this suggested_watch_until,
    watch_summary.py's own regeneration condition would already have refreshed the
    narrative (and pushed suggested_watch_until forward) before this ever runs, so
    this guard is mostly redundant with that mechanism, not a separate assumption --
    kept explicit anyway rather than relying on ordering between two different
    modules holding forever.

    BUG FOUND LIVE 2026-08-18 (re-audit): used to compare last_alert_at (a filing's
    disclosure date, from fundamentals_watchlist) against narrative_generated_at
    (a wall-clock processing timestamp) -- the same event-date-vs-processing-time
    mismatch watch_summary.py's own narrative refresh gate had (alert_date lags
    real alert creation by a median of 15 days), which defeated the "mostly
    redundant" assumption above: the upstream gate wasn't actually catching this
    before it got here. Now compares latest_alert_load_ts (fundamentals_l3_
    alerts.load_ts, the wall-clock time the alert was actually written) against
    narrative_generated_at -- both processing timestamps."""
    if suggested_watch_until is None:
        return None
    watch_until = pd.to_datetime(suggested_watch_until, errors="coerce")
    if pd.isna(watch_until) or watch_until.date() >= today:
        return None
    generated_at = pd.to_datetime(narrative_generated_at, errors="coerce")
    latest_alert = pd.to_datetime(latest_alert_load_ts, errors="coerce")
    if pd.notna(generated_at) and pd.notna(latest_alert) and latest_alert > generated_at:
        return None  # a fresher alert exists than the narrative that set this timeout -- not actually stale
    return f"Suggested watch window ended {suggested_watch_until} with no new alert since."


def evaluate_exit_status(row: dict, trigger_history: list[dict], *, today) -> tuple[str, str | None]:
    """(status, reason) for one company -- 'active' with reason=None unless one of
    the three conditions fires, checked in priority order (see module docstring)."""
    invalidated_reason = _check_invalidated(trigger_history)
    if invalidated_reason:
        return "invalidated", invalidated_reason

    price_reason = _check_price_flagged(row.get("first_seen_price"), row.get("current_price"), price_data_stale=bool(row.get("price_data_stale")))
    if price_reason:
        return "price_flagged", price_reason

    stale_reason = _check_stale(row.get("suggested_watch_until"), row.get("latest_alert_load_ts"), row.get("narrative_generated_at"), today=today)
    if stale_reason:
        return "stale", stale_reason

    return "active", None


def run_watchlist_exit_evaluation() -> dict[str, object]:
    _bootstrap_status_columns()

    watchlist = load_watchlist_for_exit_evaluation()
    if watchlist.empty:
        return {"companies": 0, "active": 0, "invalidated": 0, "price_flagged": 0, "stale": 0}

    trigger_history_by_company = load_trigger_type_history_by_company()
    today = pd.Timestamp.now(tz="UTC").date()
    load_ts = pd.Timestamp.now(tz="UTC")

    rows = []
    counts = {"active": 0, "invalidated": 0, "price_flagged": 0, "stale": 0}
    for _, row in watchlist.iterrows():
        row_dict = row.to_dict()
        company_master_id = row_dict["company_master_id"]
        # re-derive first_seen_price live (see load_watchlist_for_exit_evaluation's docstring) instead of
        # trusting the frozen stored column, so it stays on the SAME cum_adj_factor basis as current_price
        # even if a split/bonus happened after this company was first seen. Falls back to the stored value
        # if the live lookup comes back empty (e.g. a gap in the adjusted series) -- never guessed/defaulted.
        live_first_seen_price = load_price_near(company_master_id, row_dict.get("first_seen_at"))
        if live_first_seen_price is not None:
            row_dict["first_seen_price"] = live_first_seen_price
        status, reason = evaluate_exit_status(row_dict, trigger_history_by_company.get(company_master_id, []), today=today)
        counts[status] += 1
        rows.append(
            {
                "company_master_id": company_master_id,
                "status": status,
                "status_reason": reason,
                "status_updated_at": load_ts,
            }
        )

    upsert_to_db(pd.DataFrame(rows), "fundamentals_watchlist", unique_keys=["company_master_id"])
    return {"companies": len(rows), **counts}


def main() -> int:
    global STOCKEY_RUN_STATE
    result = run_watchlist_exit_evaluation()
    STOCKEY_RUN_STATE = {
        "source": SYNC_SOURCE_NAME,
        "rows": result["companies"],
        "rows_written": result["companies"],
        "active": result["active"],
        "invalidated": result["invalidated"],
        "price_flagged": result["price_flagged"],
        "stale": result["stale"],
        "fallback_used": False,
        "state_advanced": result["companies"] > 0,
        "status": "ok",
    }
    print(json.dumps(STOCKEY_RUN_STATE, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
