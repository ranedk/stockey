"""Watchlist -- fundamental screener step 10 (docs/FUNDAMENTAL_SCREENER_PRD.md's
successor step, agreed in conversation 2026-08-11): the running, fully-automatic list
of every company that has ever had an L3 alert (fundamentals/screens/l3_triggers.py
rule pass or llm_triage.py judgment pass -- either origin qualifies, this table
doesn't distinguish). Auto-add on any L3 alert was the explicit, user-confirmed
decision (no separate human "promote to watchlist" step) -- and distinct from the
machine portfolio (fundamentals_portfolio_position, portfolio_runner.py), which this
table never touches or implies. Being watched is not being held: the ruleset decides
which watched names become positions, and this table has no say in it.

sync_watchlist_from_alerts() is idempotent: first_seen_at/first_seen_price are set
once, the first time a company appears, and normally never overwritten on later
syncs (re-running this after new alerts land must not rewrite "when/at what price
did we start watching this" -- that's the whole point of the field) -- EXCEPT when
the true earliest DETECTED alert has moved (either direction, see
_record_first_seen_corrected_fallback's own docstring), in which case it's
corrected and the correction is recorded as a fallback event, never silent.
last_alert_at and alert_count are recomputed fresh from fundamentals_l3_alerts
every run, so they can never drift from the alert table itself.

BUG FOUND LIVE 2026-08-22: first_seen_at/last_alert_at used to be MIN/MAX(alert_date)
-- alert_date is the underlying disclosed EVENT's date (e.g. a BSE filing's own
disclosure date, l3_triggers.py sets it from event_dict["disclosure_date"]), not
when stockey's own pipeline actually detected/generated the alert. A disclosure can
be ingested and evaluated well after it happened (a BSE-announcement backfill, a
delayed collector run), so "watching since" could -- and did -- show a date long
before this feature existed. Confirmed live: GNRL's earliest alert has
alert_date=2025-11-01 (a capital-raise filing from that date) but load_ts=
2026-08-14 (the actual date l3_triggers.py generated the alert row) -- the
watchlist showed "watching since 2025-11-01", ten months before the L3 alerting
pipeline was even built (fundamentals screener PRD, 2026-08-11). Fixed by keying
off load_ts (when the ALERT ROW was written, i.e. when we actually found the
signal) instead of alert_date (when the underlying event happened in the real
world). The correction machinery below is unchanged in spirit -- MIN(load_ts) can
still shift backward (an identity-resolution backfill attaching an older alert
row, whose OWN load_ts predates the currently-stored value) or forward (the alert
that justified the stored value gets deleted/reclassified) -- just measuring
detection time now, not event time.

first_seen_price prefers advisory_adjusted_ohlcv_daily (stockey's own adjusted
PRIMARY series, keyed by bare ticker; EQ then BE series), falls back to
bse_advisory_adjusted_ohlcv_daily keyed by company_master.bse_scrip_code for
BSE-only companies with no NSE listing at all (added 2026-08-22 -- this function
never had technicals.py's BSE-only fallback; confirmed live: 5/7 watchlist rows
with a None first_seen_price were exactly this case, real BSE price history
existed the whole time), then raw nseindia_ohlcv close as a last resort; None (not
0, not a guess) if nothing has a price on or before the first-seen date.

The narrative/suggested-watch-duration columns (narrative_text, narrative_model,
narrative_prompt_version, narrative_generated_at, suggested_watch_until) are
DELIBERATELY NOT created by this module's own table statement -- they belong to the
watch-summary synthesis step (fundamentals/screens/watch_summary.py) that generates
them, added via its own bootstrap ALTER, same "each module owns the columns it
writes" convention fundamentals/screens/l3_triggers.py and llm_triage.py already use
for rule_trigger_status/llm_triage_status."""

from __future__ import annotations

import json

import pandas as pd

from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.fallback_telemetry import record_local_fallback_event

SYNC_SOURCE_NAME = "fundamentals.screens.watchlist"
RESULTS_TABLE = "fundamentals_watchlist"
STOCKEY_RUN_STATE: dict[str, object] = {}

_WATCHLIST_TABLE_STATEMENT = """
    CREATE TABLE IF NOT EXISTS fundamentals_watchlist (
        company_master_id TEXT PRIMARY KEY,
        first_seen_at DATE,
        first_seen_price DOUBLE PRECISION,
        last_alert_at DATE,
        alert_count INTEGER,
        load_ts TIMESTAMPTZ
    )
"""


def _ensure_watchlist_table() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute(_WATCHLIST_TABLE_STATEMENT)

    execute_db_operation(_op, operation_name="fundamentals_watchlist:ensure_table")


def _record_fallback(fallback_type: str, *, reason: str, error, severity: str = "warn", metadata=None) -> None:
    record_local_fallback_event(
        module=SYNC_SOURCE_NAME,
        source="fundamentals_l3_alerts",
        fallback_type=fallback_type,
        severity=severity,
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


# Alerts that are only ever evidence AGAINST a company (2026-09-24, operator decision:
# only positive alerts add a name). Before, a lone results_decline or bulk_deal_sell
# added the name, and invalidation only fires when a POSITIVE original story is later
# contradicted -- so negative-only names sat 'active' forever. They still count against
# a name already on the list.
NEGATIVE_TRIGGER_TYPES = frozenset({
    "rating_downgrade", "results_decline", "insider_sell_surprise", "pledge_increase",
    "bulk_deal_sell", "auditor_change", "related_party_transaction", "results_delayed",
    "story_read_negative",  # the story read's deteriorating verdict (2026-09-29)
})


# --- membership on the story score (reevaluation PRD 4.3, build step 6, 2026-09-29) -------------
# A company is watched when it QUALIFIES today, on either basis:
#   story -- its live story score is in the band (reeval: enter at 80, stay until below 65) and
#            it has no flaw;
#   event -- a positive alert detected in the last EVENT_ENTRY_DAYS while its score is at or above
#            the day's median, and no flaw.
# Before, any positive alert ever added a name for good; 109 of 132 active names on 2026-09-29
# had weak stories (median-or-lower scores) and were there only because something was filed.
# watchlist_exit turns a flaw into 'flawed' and a lost qualification into 'faded'; both are
# re-evaluated every run, so a name that qualifies again comes back.
EVENT_ENTRY_DAYS = 60
# live scores older than this are not a basis for membership: fall back to the alert rules
LIVE_SCORE_MAX_AGE_DAYS = 3
MEMBERSHIP_VERSION = 2


def load_story_qualification() -> pd.DataFrame | None:
    """Per company: story_score, primary_dimension, flaws, entry_basis ('story', 'event',
    'story+event' or 'none'). None when live scores are missing or stale -- callers then keep
    the alert rules and the fallback is recorded, never an empty watchlist."""
    try:
        live = sql_to_df(
            "SELECT company_master_id, story_score, primary_dimension, flaws, in_band, scored_at "
            "FROM fundamentals_story_score_live WHERE scored_at >= now() - make_interval(days => %s)",
            params=(LIVE_SCORE_MAX_AGE_DAYS,))
    except Exception as exc:  # noqa: BLE001 -- table not created yet on a fresh DB
        live = pd.DataFrame()
        _record_fallback("watchlist_story_scores_unreadable", reason="Live story scores could not be read; the watchlist keeps the alert rules this run.", error=exc)
        return None
    if live.empty:
        _record_fallback("watchlist_story_scores_stale", reason=f"No live story score newer than {LIVE_SCORE_MAX_AGE_DAYS} days; the watchlist keeps the alert rules this run.", error="stale or empty fundamentals_story_score_live")
        return None
    positive = sql_to_df(
        """
        SELECT DISTINCT company_master_id FROM fundamentals_l3_alerts
         WHERE load_ts >= now() - make_interval(days => %s) AND company_master_id IS NOT NULL
           AND trigger_type <> ALL(%s) AND status NOT LIKE 'superseded%%'
        """, params=(EVENT_ENTRY_DAYS, sorted(NEGATIVE_TRIGGER_TYPES)))
    return qualify(live, set(positive["company_master_id"]))


def qualify(live: pd.DataFrame, recent_positive: set[str]) -> pd.DataFrame:
    q = live.copy()
    clean = q["flaws"].isna() | (q["flaws"].astype(str).str.strip() == "")
    median = float(q["story_score"].median())
    story = q["in_band"].fillna(False).astype(bool) & clean
    event = q["company_master_id"].isin(recent_positive) & (q["story_score"] >= median) & clean
    q["entry_basis"] = [("story+event" if s and e else "story" if s else "event" if e else "none")
                        for s, e in zip(story, event)]
    return q[["company_master_id", "story_score", "primary_dimension", "flaws", "entry_basis"]]


def _ensure_membership_columns() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            for col, typ in (("entry_basis", "TEXT"), ("story_score", "DOUBLE PRECISION"),
                             ("primary_dimension", "TEXT"), ("flaws", "TEXT"), ("membership_version", "INTEGER")):
                cur.execute(f"ALTER TABLE fundamentals_watchlist ADD COLUMN IF NOT EXISTS {col} {typ}")

    execute_db_operation(_op, operation_name="fundamentals_watchlist:ensure_membership_columns")


def load_l3_alert_summary_by_company() -> pd.DataFrame:
    """first/last DETECTED date -- MIN/MAX(load_ts), not alert_date. alert_date is
    the underlying disclosed event's own date and can predate detection by months
    (see module docstring's 2026-08-22 bug note); "watching since" must mean when
    WE found the signal, not when the underlying event happened."""
    return sql_to_df(
        """
        SELECT company_master_id,
               count(*) FILTER (WHERE trigger_type <> ALL(%s)) AS positive_alerts,
               (MIN(load_ts))::date AS first_detected_date,
               (MAX(load_ts))::date AS last_detected_date,
               COUNT(*) AS alert_count
        FROM fundamentals_l3_alerts
        WHERE company_master_id IS NOT NULL
          AND status NOT LIKE 'superseded%%'   -- round-trip deals are not evidence (2026-09-23)
        GROUP BY company_master_id
        """,
        params=(sorted(NEGATIVE_TRIGGER_TYPES),),
    )


def load_existing_watchlist() -> pd.DataFrame:
    return sql_to_df("SELECT company_master_id, first_seen_at, first_seen_price FROM fundamentals_watchlist")


def _record_first_seen_corrected_fallback(company_master_id: str, *, old_first_seen_at, new_first_seen_at, direction: str) -> None:
    if direction == "backward":
        reason = (
            "A newly-visible alert (usually an identity-resolution backfill on an "
            "older fundamentals_l3_alerts row whose OWN load_ts predates what's "
            "currently stored) is earlier than this company's previously-frozen "
            "first_seen_at -- corrected backward to the true earliest DETECTED date."
        )
        error = "first_seen_at moved earlier"
    else:
        # BUG FOUND LIVE 2026-08-18 (re-audit): the original backward-only design
        # (de4aab6) deliberately never moved first_seen_at forward, reasoning that
        # MIN() only ever reveals a genuinely EARLIER true history (an identity
        # backfill attaching an older alert row). But the alert set for a fixed
        # (source, news_id, trigger_type) key is mutable -- a later reclassification
        # or deletion of the alert that originally justified first_seen_at (e.g. this
        # session's own capital_raise classifier false-positive remediation, which
        # deleted 10 spurious alerts) can make the true earliest SURVIVING alert's
        # load_ts later than the frozen stored value, with no alert left to support
        # it. Confirmed live: 3 real companies (post-remediation) now predate every
        # alert that still exists. Allowed forward now too -- staying frozen at a
        # date nothing supports anymore is worse than moving to the true earliest
        # remaining alert -- but always via this fallback event, never silently.
        # (2026-08-22: reasoning carries over unchanged after switching MIN/MAX from
        # alert_date to load_ts -- the same backfill/deletion drift applies to
        # either field, only WHICH date moves is different.)
        reason = (
            "The true earliest DETECTED alert for this company is now LATER than "
            "the previously-frozen first_seen_at -- the alert that originally "
            "justified it no longer exists (reclassified or deleted), most likely "
            "by a data remediation. Corrected forward to the true earliest "
            "surviving alert's detection date."
        )
        error = "first_seen_at moved later"
    _record_fallback(
        "watchlist_first_seen_at_corrected",
        reason=reason,
        error=error,
        metadata={"company_master_id": company_master_id, "old_first_seen_at": str(old_first_seen_at), "new_first_seen_at": str(new_first_seen_at), "direction": direction},
    )


# Every read below is date-bounded (2026-09-23 audit): these are views over hypertables,
# called once per watchlist row per run, and an unbounded "latest on or before" walks
# every chunk. A name with no trade in this window has no honest "price near" anyway.
PRICE_NEAR_MAX_GAP_DAYS = 30


def load_price_near(company_master_id: str, as_of_date) -> float | None:
    """Close price on or before as_of_date -- adjusted series preferred, raw
    nseindia_ohlcv as fallback, None if neither has anything that early (never
    guessed or defaulted to 0)."""
    ticker = str(company_master_id or "").removeprefix("nse:")
    if not ticker or as_of_date is None:
        return None
    # The id's suffix is not always the NSE symbol ("nse:543531-BOM" is BSE-only); use
    # company_master's recorded nse_ticker when there is one.
    recorded = sql_to_df("SELECT nse_ticker FROM company_master WHERE company_master_id = %s",
                         params=(company_master_id,))
    if not recorded.empty and pd.notna(recorded.iloc[0]["nse_ticker"]):
        ticker = str(recorded.iloc[0]["nse_ticker"])

    adjusted = sql_to_df(
        "SELECT adj_close FROM advisory_adjusted_ohlcv_daily WHERE symbol = %s AND series = 'EQ' AND date <= %s "
        "  AND date >= %s::date - %s ORDER BY date DESC LIMIT 1",
        params=(ticker, as_of_date, as_of_date, PRICE_NEAR_MAX_GAP_DAYS),
    )
    if not adjusted.empty:
        return float(adjusted.iloc[0]["adj_close"])

    # BUG FOUND LIVE 2026-08-18 (re-audit): series='EQ' hardcode drops BE-series
    # (trade-to-trade / restricted-segment) companies the adjusted view actually
    # covers -- same fix as technicals.py's load_adjusted_price_history, see its own
    # comment for the live-confirmed VHLTD example (531 real BE-series rows, zero EQ).
    adjusted_be = sql_to_df(
        "SELECT adj_close FROM advisory_adjusted_ohlcv_daily WHERE symbol = %s AND series = 'BE' AND date <= %s "
        "  AND date >= %s::date - %s ORDER BY date DESC LIMIT 1",
        params=(ticker, as_of_date, as_of_date, PRICE_NEAR_MAX_GAP_DAYS),
    )
    if not adjusted_be.empty:
        return float(adjusted_be.iloc[0]["adj_close"])

    # BUG FOUND LIVE 2026-08-22: this function never had technicals.py's BSE-only
    # fallback (bse_advisory_adjusted_ohlcv_daily, keyed by scrip_code) -- a company
    # with no NSE listing at all has nothing in the two queries above no matter what
    # as_of_date is, even when real BSE price history exists and covers that date.
    # Confirmed live: 5/7 watchlist rows with first_seen_price=None were exactly
    # this (543531-BOM, NAPL, JYOTI, BGWTATO, KESARPE) -- all zero rows in
    # advisory_adjusted_ohlcv_daily, all 246-248 rows in bse_advisory_adjusted_
    # ohlcv_daily covering their first_seen_at comfortably.
    scrip = sql_to_df(
        "SELECT bse_scrip_code FROM company_master WHERE company_master_id = %s",
        params=(company_master_id,),
    )
    if not scrip.empty and pd.notna(scrip.iloc[0]["bse_scrip_code"]):
        bse = sql_to_df(
            "SELECT adj_close FROM bse_advisory_adjusted_ohlcv_daily WHERE scrip_code = %s AND date <= %s "
            "  AND date >= %s::date - %s ORDER BY date DESC LIMIT 1",
            params=(str(scrip.iloc[0]["bse_scrip_code"]), as_of_date, as_of_date, PRICE_NEAR_MAX_GAP_DAYS),
        )
        if not bse.empty:
            return float(bse.iloc[0]["adj_close"])

    raw = sql_to_df(
        "SELECT close FROM nseindia_ohlcv WHERE company_master_id = %s AND date <= %s "
        "  AND date >= %s::date - %s ORDER BY date DESC LIMIT 1",
        params=(company_master_id, as_of_date, as_of_date, PRICE_NEAR_MAX_GAP_DAYS),
    )
    if not raw.empty:
        return float(raw.iloc[0]["close"])
    return None


def sync_watchlist() -> dict[str, object]:
    """Membership on the story score (see EVENT_ENTRY_DAYS above); the alert rules only when
    live scores are unavailable."""
    _ensure_watchlist_table()
    _ensure_membership_columns()
    qualification = load_story_qualification()
    if qualification is None:
        return {**sync_watchlist_from_alerts(), "membership": "alerts (live story scores unavailable)"}

    alert_summary = load_l3_alert_summary_by_company().set_index("company_master_id")
    existing_df = load_existing_watchlist()
    existing = set(existing_df["company_master_id"]) if not existing_df.empty else set()
    qualified = qualification[qualification["entry_basis"] != "none"]
    load_ts = pd.Timestamp.now(tz="UTC")
    today = pd.Timestamp.now(tz="Asia/Kolkata").date()
    rows, new_candidates, no_price = [], [], 0
    for q in qualification.to_dict("records"):
        cid = q["company_master_id"]
        if cid not in existing and q["entry_basis"] == "none":
            continue
        row = {"company_master_id": cid, "entry_basis": q["entry_basis"], "story_score": q["story_score"],
               "primary_dimension": q["primary_dimension"], "flaws": q["flaws"], "load_ts": load_ts}
        if cid in alert_summary.index:
            row["last_alert_at"] = alert_summary.at[cid, "last_detected_date"]
            row["alert_count"] = int(alert_summary.at[cid, "alert_count"])
        if cid not in existing:
            # watched from the day it first qualified, at that day's price
            price = load_price_near(cid, today)
            row.update({"first_seen_at": today, "first_seen_price": price, "membership_version": MEMBERSHIP_VERSION,
                        "last_alert_at": row.get("last_alert_at"), "alert_count": row.get("alert_count", 0)})
            new_candidates.append(cid)
            if price is None:
                no_price += 1
        rows.append(row)
    # existing members with no live score today (left the universe) are no longer qualified
    for cid in existing - set(qualification["company_master_id"]):
        rows.append({"company_master_id": cid, "entry_basis": "none", "load_ts": load_ts})
    if rows:
        df = pd.DataFrame(rows)
        for col in ("story_score", "primary_dimension", "flaws"):
            if col not in df:
                df[col] = None
        upsert_to_db(df, RESULTS_TABLE, unique_keys=["company_master_id"])
    return {"companies": len(rows), "qualified": int(len(qualified)), "new_candidates": len(new_candidates),
            "new_candidate_ids": new_candidates, "no_price_at_first_seen": no_price, "membership": "story score"}


def sync_watchlist_from_alerts() -> dict[str, object]:
    """The pre-2026-09-29 rule (any positive alert ever adds a name), kept as the fallback
    when live story scores are unavailable."""
    _ensure_watchlist_table()

    alert_summary = load_l3_alert_summary_by_company()
    if alert_summary.empty:
        return {"companies": 0, "new_candidates": 0, "no_price_at_first_seen": 0}

    existing_df = load_existing_watchlist()
    existing = existing_df.set_index("company_master_id") if not existing_df.empty else pd.DataFrame().set_index(pd.Index([], name="company_master_id"))

    rows = []
    new_candidates: list[str] = []
    no_price_count = 0
    load_ts = pd.Timestamp.now(tz="UTC")

    for _, alert_row in alert_summary.iterrows():
        company_master_id = alert_row["company_master_id"]
        true_first_detected_date = alert_row["first_detected_date"]
        if company_master_id in existing.index:
            stored_first_seen_at = existing.loc[company_master_id, "first_seen_at"]
            # BUG FOUND LIVE 2026-08-15, fixed here: first_seen_at used to be frozen
            # forever once a watchlist row existed, with no re-check against the
            # freshly-recomputed aggregate. fundamentals_l3_alerts.company_master_id
            # can be backfilled onto an OLDER alert row after this company's
            # watchlist row was already created (an identity-resolution fix landing
            # after the fact) -- MIN(load_ts) then reveals a genuinely earlier true
            # first detection that this row's frozen value never reflected.
            # Confirmed live: 9/38 watchlist rows had first_seen_at == last_alert_at
            # (i.e. frozen at the MOST RECENT alert, not the first) -- one case
            # (AAREYDRUGS) was off by over a year. Only ever corrected BACKWARD
            # (earlier) when true history surfaces -- see _record_first_seen_
            # corrected_fallback's own docstring for the forward direction, added
            # 2026-08-18, and why it's needed too now.
            if pd.notna(stored_first_seen_at) and true_first_detected_date != stored_first_seen_at:
                first_seen_at = true_first_detected_date
                first_seen_price = load_price_near(company_master_id, first_seen_at)
                direction = "backward" if true_first_detected_date < stored_first_seen_at else "forward"
                _record_first_seen_corrected_fallback(company_master_id, old_first_seen_at=stored_first_seen_at, new_first_seen_at=first_seen_at, direction=direction)
            else:
                first_seen_at = stored_first_seen_at
                first_seen_price = existing.loc[company_master_id, "first_seen_price"]
        else:
            if int(alert_row.get("positive_alerts") or 0) == 0:
                continue  # negative-only: evidence against, never a reason to start watching
            first_seen_at = true_first_detected_date
            first_seen_price = load_price_near(company_master_id, first_seen_at)
            new_candidates.append(company_master_id)
            if first_seen_price is None:
                no_price_count += 1
                _record_fallback(
                    "watchlist_no_price_at_first_seen",
                    reason="No adjusted or raw OHLCV close on/before first_seen_at for a new watchlist candidate; first_seen_price left None rather than guessed.",
                    error="no price data",
                    metadata={"company_master_id": company_master_id, "first_seen_at": str(first_seen_at)},
                )

        rows.append(
            {
                "company_master_id": company_master_id,
                "first_seen_at": first_seen_at,
                "first_seen_price": first_seen_price,
                "last_alert_at": alert_row["last_detected_date"],
                "alert_count": int(alert_row["alert_count"]),
                "load_ts": load_ts,
            }
        )

    result_df = pd.DataFrame(rows)
    upsert_to_db(result_df, RESULTS_TABLE, unique_keys=["company_master_id"])

    return {
        "companies": int(len(result_df)),
        "new_candidates": len(new_candidates),
        "new_candidate_ids": new_candidates,
        "no_price_at_first_seen": no_price_count,
    }


def main() -> int:
    global STOCKEY_RUN_STATE
    result = sync_watchlist()
    STOCKEY_RUN_STATE = {
        "source": SYNC_SOURCE_NAME,
        "rows": result["companies"],
        "rows_written": result["companies"],
        "new_candidates": result["new_candidates"],
        "no_price_at_first_seen": result["no_price_at_first_seen"],
        "fallback_used": result["no_price_at_first_seen"] > 0,
        "state_advanced": result["companies"] > 0,
        "status": "ok",
    }
    print(json.dumps(STOCKEY_RUN_STATE, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
