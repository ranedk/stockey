"""L3 rule-based triggers -- fundamental screener step 7, rule half
(docs/FUNDAMENTAL_SCREENER_PRD.md sec 8 step 7 / sec 4's package layout, which puts
"daily rule-based triggers + LLM triage" in the same l3_triggers.py -- step 7's prose
is titled "LLM triage" but its own text says triage "augments a working alert
surface, it doesn't bootstrap one", meaning the rule-based alert surface has to exist
first. No earlier numbered step built it -- steps 0-6 built detection (fundamentals_
events) and state (fundamentals_l2_state) as two separate tables, but nothing yet
combined them. This module is that combination.

Core rule, straight from fundamental_basic_goal.md's L3 section: "Alert fires only on
trigger x primed L2 state. Never on trigger alone." A rating action or an insider
trade is not automatically alert-worthy -- it's alert-worthy when it lines up with (or
contradicts) what the company's own state vector already suggested. This is a real
design decision, not something the PRD spelled out field-by-field, so the specific
priming rules are recorded here, not assumed:

- rating_action: a downgrade is always alerted (new risk information regardless of
  prior state). An upgrade/reaffirmation is alerted only when L2's own net_debt_yoy_
  delta_rscr is already negative (deleveraging) -- that's an independent rating-agency
  confirmation of the exact turnaround thesis this screener exists to find (source PRD
  sec 1: this system grew out of the deleveraging screen, step 2). A reaffirmation
  with no such confirmation is treated as already-priced-in, not alert-worthy.
- pit_sast: a promoter/KMP buy is always alerted (rarer, higher-signal than a sell).
  A promoter/KMP sell is alerted only when L2's promoter_stake_direction is NOT
  already "decreasing" -- i.e. this transaction is NEW information, not a data point
  that merely confirms a trend L2 had already captured from the quarterly
  shareholding pattern. Rows with no transaction data at all (the common case --
  most BSE-detected pit_sast rows are trading-window-closure procedural notices, not
  actual trades) never alert.
- results: deliberately NOT built in this pass -- reconciling screener.in's own
  quarter-over-quarter growth fields (already in fundamentals_l1_universe's
  metrics_json) against a specific filing's OCR'd figures needs more design thought
  than fits this step; noted as a follow-up, not silently skipped.

Identity bridge: fundamentals_events is keyed by company_master_id ("nse:<ticker>",
stockey's own identity layer); fundamentals_l2_state is keyed by ticker alone
(inherited from fundamentals_l1_universe's screener.in identity) -- joined on
company_master_id = 'nse:' || ticker, confirmed live 2026-08-11 against a real
company (ZFSTEERING) present in both tables under that exact convention.

Idempotent by design: alerts upsert-key on (source, news_id, trigger_type), so
re-running this rule pass (e.g. after L2 refreshes) never duplicates an alert --
matches the append-only-but-idempotent pattern every other fundamentals table in this
codebase already uses.
"""

from __future__ import annotations

import json

import pandas as pd
from psycopg2 import sql as psycopg2_sql

from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.fallback_telemetry import record_local_fallback_event

SYNC_SOURCE_NAME = "fundamentals.screens.l3_triggers"
RESULTS_TABLE = "fundamentals_l3_alerts"
STOCKEY_RUN_STATE: dict[str, object] = {}

# Trigger types this rule pass evaluates -- "results" deliberately not included yet,
# see module docstring.
SUPPORTED_FILING_TYPES = ("rating_action", "pit_sast")

_ALERTS_TABLE_STATEMENT = """
    CREATE TABLE IF NOT EXISTS fundamentals_l3_alerts (
        source TEXT NOT NULL,
        news_id TEXT NOT NULL,
        trigger_type TEXT NOT NULL,
        origin TEXT NOT NULL,
        company_master_id TEXT,
        alert_date DATE,
        reasoning TEXT,
        l2_run_date TEXT,
        l2_state_snapshot_json TEXT,
        status TEXT,
        model TEXT,
        prompt_version INTEGER,
        evidence_bundle_json TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (source, news_id, trigger_type)
    )
"""


def _record_fallback(fallback_type: str, *, reason: str, error, severity: str = "warn", metadata=None) -> None:
    record_local_fallback_event(
        module=SYNC_SOURCE_NAME,
        source="l3_triggers",
        fallback_type=fallback_type,
        severity=severity,
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def _ensure_alerts_table() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute(_ALERTS_TABLE_STATEMENT)

    execute_db_operation(_op, operation_name="fundamentals_l3_alerts:ensure_table")


def load_candidate_events(limit: int | None = None) -> pd.DataFrame:
    """rating_action/pit_sast events not yet evaluated by this rule pass (an event
    already alerted, or already evaluated and found not alert-worthy, is tracked via
    fundamentals_events.rule_trigger_status so re-runs don't redo work every day)."""
    placeholders = ",".join(f"'{ft}'" for ft in SUPPORTED_FILING_TYPES)
    query = f"""
        SELECT source, news_id, company_master_id, filing_type, headline,
               rating_action_type, transaction_type, insider_name, quantity,
               disclosure_date
        FROM fundamentals_events
        WHERE filing_type IN ({placeholders})
          AND (rule_trigger_status IS NULL OR rule_trigger_status = 'pending')
        ORDER BY load_ts ASC NULLS LAST
    """  # noqa: S608 -- placeholders built from SUPPORTED_FILING_TYPES, a fixed internal constant, never user input
    if limit:
        query += f" LIMIT {int(limit)}"
    return sql_to_df(query)


def load_latest_l2_state() -> pd.DataFrame:
    return sql_to_df(
        """
        SELECT DISTINCT ON (ticker) ticker, company_name, net_debt_rscr, net_debt_yoy_delta_rscr,
               interest_coverage, debt_to_ebitda, cwip_ratio, cwip_ratio_yoy_delta,
               pledge_pct, promoter_pct, promoter_stake_direction, run_date
        FROM fundamentals_l2_state
        ORDER BY ticker, run_date DESC
        """
    )


def evaluate_rating_action_trigger(event: dict, l2_row: dict | None) -> dict | None:
    action = (event.get("rating_action_type") or "").lower()
    if action == "downgraded":
        return {
            "trigger_type": "rating_downgrade",
            "reasoning": "Rating downgrade is alert-worthy regardless of prior L2 state -- new risk information.",
        }
    if action in ("upgraded", "reaffirmed"):
        net_debt_delta = l2_row.get("net_debt_yoy_delta_rscr") if l2_row else None
        if net_debt_delta is not None and pd.notna(net_debt_delta) and net_debt_delta < 0:
            return {
                "trigger_type": "rating_confirms_deleveraging",
                "reasoning": (
                    f"Rating {action} lines up with L2's own net_debt_yoy_delta_rscr={net_debt_delta} "
                    "(declining) -- independent rating-agency confirmation of a deleveraging trend already "
                    "visible in state."
                ),
            }
        return None  # reaffirmed/upgraded with no independent confirmation -- already priced in
    return None


def evaluate_pit_sast_trigger(event: dict, l2_row: dict | None) -> dict | None:
    transaction_type = (event.get("transaction_type") or "").lower()
    if not transaction_type:
        return None  # the common case -- a procedural notice with no actual trade
    if "buy" in transaction_type:
        return {
            "trigger_type": "insider_buy",
            "reasoning": f"Promoter/insider buy ({event.get('insider_name')}) -- rarer and higher-signal than a sell, alerted regardless of prior L2 state.",
        }
    if "sell" in transaction_type:
        direction = (l2_row.get("promoter_stake_direction") if l2_row else None) or ""
        if direction != "decreasing":
            return {
                "trigger_type": "insider_sell_surprise",
                "reasoning": (
                    f"Promoter/insider sell ({event.get('insider_name')}) while L2's own promoter_stake_direction "
                    f"was '{direction or 'unknown'}', not already 'decreasing' -- this is new information, not "
                    "confirmation of a trend L2 had already captured."
                ),
            }
        return None  # sell confirms an already-known declining trend -- not new information
    return None


TRIGGER_EVALUATORS = {
    "rating_action": evaluate_rating_action_trigger,
    "pit_sast": evaluate_pit_sast_trigger,
}


def _set_rule_trigger_status(*, source: str, news_id: str, status: str) -> None:
    def _update() -> None:
        with db_session() as (_, cur):
            cur.execute(
                "UPDATE fundamentals_events SET rule_trigger_status = %s WHERE source = %s AND news_id = %s",
                (status, source, news_id),
            )

    execute_db_operation(_update, operation_name="fundamentals_events:rule_trigger_status_update")


def _bootstrap_rule_trigger_column() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'fundamentals_events'")
            if cur.fetchone() is None:
                return
            cur.execute(
                psycopg2_sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS rule_trigger_status TEXT").format(
                    psycopg2_sql.Identifier("fundamentals_events")
                )
            )

    execute_db_operation(_op, operation_name="fundamentals_events:ensure_rule_trigger_column")


def run_l3_rule_triggers(*, limit: int | None = None) -> dict[str, object]:
    _bootstrap_rule_trigger_column()
    _ensure_alerts_table()

    events = load_candidate_events(limit)
    if events.empty:
        return {"alerted": 0, "not_alert_worthy": 0, "no_l2_state": 0}

    l2_state = load_latest_l2_state()
    l2_by_ticker = {row["ticker"]: row for row in l2_state.to_dict("records")}

    counts = {"alerted": 0, "not_alert_worthy": 0, "no_l2_state": 0}
    alert_rows: list[dict] = []

    for _, event in events.iterrows():
        event_dict = event.to_dict()
        ticker = str(event_dict.get("company_master_id") or "").removeprefix("nse:")
        l2_row = l2_by_ticker.get(ticker)
        if l2_row is None:
            counts["no_l2_state"] += 1
            _record_fallback(
                "l3_trigger_no_l2_state",
                reason="No L2 state found for this event's company; evaluated with L2 context unavailable (treated as None, may under-alert).",
                error="missing L2 state",
                metadata={"company_master_id": event_dict.get("company_master_id")},
            )

        evaluator = TRIGGER_EVALUATORS[event_dict["filing_type"]]
        result = evaluator(event_dict, l2_row)

        if result is None:
            counts["not_alert_worthy"] += 1
            _set_rule_trigger_status(source=event_dict["source"], news_id=event_dict["news_id"], status="not_alert_worthy")
            continue

        counts["alerted"] += 1
        alert_rows.append(
            {
                "source": event_dict["source"],
                "news_id": event_dict["news_id"],
                "trigger_type": result["trigger_type"],
                "origin": "rule",
                "company_master_id": event_dict.get("company_master_id"),
                "alert_date": event_dict.get("disclosure_date"),
                "reasoning": result["reasoning"],
                "l2_run_date": str(l2_row.get("run_date")) if l2_row is not None else None,
                "l2_state_snapshot_json": json.dumps(l2_row, ensure_ascii=False, default=str) if l2_row is not None else None,
                "status": "new",
                "load_ts": pd.Timestamp.now(tz="UTC"),
            }
        )
        _set_rule_trigger_status(source=event_dict["source"], news_id=event_dict["news_id"], status="alerted")

    if alert_rows:
        upsert_to_db(pd.DataFrame(alert_rows), RESULTS_TABLE, unique_keys=["source", "news_id", "trigger_type"])

    return counts


def main() -> int:
    global STOCKEY_RUN_STATE
    result = run_l3_rule_triggers()
    STOCKEY_RUN_STATE = {
        "source": SYNC_SOURCE_NAME,
        "alerted": result["alerted"],
        "rows_written": result["alerted"],
        "not_alert_worthy": result["not_alert_worthy"],
        "no_l2_state": result["no_l2_state"],
        "fallback_used": result["no_l2_state"] > 0,
        "state_advanced": result["alerted"] > 0,
        "status": "ok",
    }
    print(json.dumps(STOCKEY_RUN_STATE, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
