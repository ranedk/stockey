"""Watchlist -- fundamental screener step 10 (docs/FUNDAMENTAL_SCREENER_PRD.md's
successor step, agreed in conversation 2026-08-11): the running, fully-automatic list
of every company that has ever had an L3 alert (fundamentals/screens/l3_triggers.py
rule pass or llm_triage.py judgment pass -- either origin qualifies, this table
doesn't distinguish). Auto-add on any L3 alert was the explicit, user-confirmed
decision (no separate human "promote to watchlist" step) -- distinct from
fundamentals_l4_thesis (l4_thesis.py), which stays the deliberate, human-only
"portfolio" act (create_thesis/resolve_thesis) this table never touches or implies.

sync_watchlist_from_alerts() is idempotent: first_seen_at/first_seen_price are set
once, the first time a company appears, and normally never overwritten on later
syncs (re-running this after new alerts land must not rewrite "when/at what price
did we start watching this" -- that's the whole point of the field) -- EXCEPT when
the true earliest alert has moved (either direction, see
_record_first_seen_corrected_fallback's own docstring), in which case it's
corrected and the correction is recorded as a fallback event, never silent.
last_alert_at and alert_count are recomputed fresh from fundamentals_l3_alerts
every run, so they can never drift from the alert table itself.

first_seen_price prefers advisory_adjusted_ohlcv_daily (stockey's own adjusted
PRIMARY series, keyed by bare ticker -- see fundamentals/screens/technicals.py's own
docstring for the coverage caveat), falling back to raw nseindia_ohlcv close for the
companies technicals.py already documented as missing an adjusted series; None (not
0, not a guess) if neither source has a price on or before the first-seen date.

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


def load_l3_alert_summary_by_company() -> pd.DataFrame:
    return sql_to_df(
        """
        SELECT company_master_id,
               MIN(alert_date) AS first_alert_date,
               MAX(alert_date) AS last_alert_date,
               COUNT(*) AS alert_count
        FROM fundamentals_l3_alerts
        WHERE company_master_id IS NOT NULL
        GROUP BY company_master_id
        """
    )


def load_existing_watchlist() -> pd.DataFrame:
    return sql_to_df("SELECT company_master_id, first_seen_at, first_seen_price FROM fundamentals_watchlist")


def _record_first_seen_corrected_fallback(company_master_id: str, *, old_first_seen_at, new_first_seen_at, direction: str) -> None:
    if direction == "backward":
        reason = (
            "A newly-visible alert (usually an identity-resolution backfill on an "
            "older fundamentals_l3_alerts row) is earlier than this company's "
            "previously-frozen first_seen_at -- corrected backward to the true "
            "earliest alert date."
        )
        error = "first_seen_at moved earlier"
    else:
        # BUG FOUND LIVE 2026-08-18 (re-audit): the original backward-only design
        # (de4aab6) deliberately never moved first_seen_at forward, reasoning that
        # MIN(alert_date) only ever reveals a genuinely EARLIER true history (an
        # identity backfill attaching an older alert). But alert_date is mutable for
        # a fixed (source, news_id, trigger_type) key -- a later reclassification or
        # deletion of the alert that originally justified first_seen_at (e.g. this
        # session's own capital_raise classifier false-positive remediation, which
        # deleted 10 spurious alerts) can make the true earliest SURVIVING alert
        # later than the frozen stored value, with no alert left to support it.
        # Confirmed live: 3 real companies (post-remediation) now predate every
        # alert that still exists. Allowed forward now too -- staying frozen at a
        # date nothing supports anymore is worse than moving to the true earliest
        # remaining alert -- but always via this fallback event, never silently.
        reason = (
            "The true earliest alert for this company is now LATER than the "
            "previously-frozen first_seen_at -- the alert that originally justified "
            "it no longer exists (reclassified or deleted), most likely by a data "
            "remediation. Corrected forward to the true earliest surviving alert."
        )
        error = "first_seen_at moved later"
    _record_fallback(
        "watchlist_first_seen_at_corrected",
        reason=reason,
        error=error,
        metadata={"company_master_id": company_master_id, "old_first_seen_at": str(old_first_seen_at), "new_first_seen_at": str(new_first_seen_at), "direction": direction},
    )


def load_price_near(company_master_id: str, as_of_date) -> float | None:
    """Close price on or before as_of_date -- adjusted series preferred, raw
    nseindia_ohlcv as fallback, None if neither has anything that early (never
    guessed or defaulted to 0)."""
    ticker = str(company_master_id or "").removeprefix("nse:")
    if not ticker or as_of_date is None:
        return None

    adjusted = sql_to_df(
        "SELECT adj_close FROM advisory_adjusted_ohlcv_daily WHERE symbol = %s AND series = 'EQ' AND date <= %s ORDER BY date DESC LIMIT 1",
        params=(ticker, as_of_date),
    )
    if not adjusted.empty:
        return float(adjusted.iloc[0]["adj_close"])

    # BUG FOUND LIVE 2026-08-18 (re-audit): series='EQ' hardcode drops BE-series
    # (trade-to-trade / restricted-segment) companies the adjusted view actually
    # covers -- same fix as technicals.py's load_adjusted_price_history, see its own
    # comment for the live-confirmed VHLTD example (531 real BE-series rows, zero EQ).
    adjusted_be = sql_to_df(
        "SELECT adj_close FROM advisory_adjusted_ohlcv_daily WHERE symbol = %s AND series = 'BE' AND date <= %s ORDER BY date DESC LIMIT 1",
        params=(ticker, as_of_date),
    )
    if not adjusted_be.empty:
        return float(adjusted_be.iloc[0]["adj_close"])

    raw = sql_to_df(
        "SELECT close FROM nseindia_ohlcv WHERE company_master_id = %s AND date <= %s ORDER BY date DESC LIMIT 1",
        params=(company_master_id, as_of_date),
    )
    if not raw.empty:
        return float(raw.iloc[0]["close"])
    return None


def sync_watchlist_from_alerts() -> dict[str, object]:
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
        true_first_alert_date = alert_row["first_alert_date"]
        if company_master_id in existing.index:
            stored_first_seen_at = existing.loc[company_master_id, "first_seen_at"]
            # BUG FOUND LIVE 2026-08-15, fixed here: first_seen_at used to be frozen
            # forever once a watchlist row existed, with no re-check against the
            # freshly-recomputed aggregate. fundamentals_l3_alerts.company_master_id
            # can be backfilled onto an OLDER alert row after this company's
            # watchlist row was already created (an identity-resolution fix landing
            # after the fact) -- MIN(alert_date) then reveals a genuinely earlier
            # true first alert that this row's frozen value never reflected.
            # Confirmed live: 9/38 watchlist rows had first_seen_at == last_alert_at
            # (i.e. frozen at the MOST RECENT alert, not the first) -- one case
            # (AAREYDRUGS) was off by over a year. Only ever corrected BACKWARD
            # (earlier) when true history surfaces -- see _record_first_seen_
            # corrected_fallback's own docstring for the forward direction, added
            # 2026-08-18, and why it's needed too now.
            if pd.notna(stored_first_seen_at) and true_first_alert_date != stored_first_seen_at:
                first_seen_at = true_first_alert_date
                first_seen_price = load_price_near(company_master_id, first_seen_at)
                direction = "backward" if true_first_alert_date < stored_first_seen_at else "forward"
                _record_first_seen_corrected_fallback(company_master_id, old_first_seen_at=stored_first_seen_at, new_first_seen_at=first_seen_at, direction=direction)
            else:
                first_seen_at = stored_first_seen_at
                first_seen_price = existing.loc[company_master_id, "first_seen_price"]
        else:
            first_seen_at = true_first_alert_date
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
                "last_alert_at": alert_row["last_alert_date"],
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
    result = sync_watchlist_from_alerts()
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
