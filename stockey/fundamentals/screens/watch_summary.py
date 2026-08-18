"""Watch-summary synthesis -- fundamental screener step 11 (agreed in conversation
2026-08-11, following on from fundamentals/screens/watchlist.py's step 10). Extends
llm_triage.py's per-EVENT evidence-bundle pattern to the COMPANY level: one company on
the watchlist can have several L3 alerts accumulated over time (fundamentals_l3_alerts,
either origin), plus its own current L2 state, descriptive technicals
(fundamentals/screens/technicals.py), and sector context -- this module shows an LLM
ALL of that together and asks it to write the "why watch this, and for how long"
narrative a human sees first on the watchlist detail page, rather than making the
human re-read every individual alert.

Same guardrails as llm_triage.py, restated because this is a second, independent
LLM-authorship surface: never recommends a trade, position, or price target: technicals
are shown as descriptive context only (see technicals.py's own boundary docstring), and
the narrative must stay grounded in the specific evidence shown, not a generic
restatement of "this is a good company". suggested_watch_duration_days is the LLM's own
judgment call about how long the CURRENT situation stays relevant (a rating action's
signal plays out over quarters; a single insider trade's signal value fades faster) --
it is advisory framing for a human, never an auto-expiry or auto-removal trigger (the
user's own decision in conversation 2026-08-11: entry/exit stays fully manual via
fundamentals_l4_thesis's add/remove-from-portfolio actions).

Regeneration is gated on real new information, not a fixed schedule: a company is only
re-summarized when fundamentals_watchlist.last_alert_at is newer than its own
narrative_generated_at (or has never had one). This is deliberate, not just an
efficiency optimization -- it is also the exact signal the email step (planned next)
needs for "narrative changed": if there's no new alert, there's nothing new to say, so
the narrative doesn't change and no email fires. The old narrative_text is compared
against the newly generated one and returned so the caller can distinguish a
first-ever narrative (new candidate's first synthesis) from a genuine revision.

Narrative columns are added to fundamentals_watchlist via this module's own bootstrap
ALTER (narrative_text, narrative_model, narrative_prompt_version,
narrative_generated_at, suggested_watch_until) -- watchlist.py's own table statement
deliberately excludes them, per the "each module owns the columns it writes"
convention l3_triggers.py/llm_triage.py already use for rule_trigger_status/
llm_triage_status."""

from __future__ import annotations

import json

import pandas as pd
from environs import Env
from openai import OpenAI

from fundamentals.screens.signal_pointers import get_stock_signal_pointers
from fundamentals.screens.watchlist import _ensure_watchlist_table
from utils.company_master import build_l1_ticker_by_company_master_id
from utils.db import db_session, execute_db_operation, sql_to_df
from utils.fallback_telemetry import record_local_fallback_event

env = Env()
env.read_env()

SYNC_SOURCE_NAME = "fundamentals.screens.watch_summary"
STOCKEY_RUN_STATE: dict[str, object] = {}

DEFAULT_MODEL = env("WATCH_SUMMARY_MODEL", "gpt-5.4-mini")
PROMPT_VERSION = 1
CIRCUIT_BREAKER_THRESHOLD = 3
DEFAULT_BATCH_LIMIT = 50

WATCH_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "narrative": {
            "type": "string",
            "description": (
                "3-5 sentences, grounded ONLY in the evidence shown (the alerts, L2 state, technicals, and sector "
                "context provided) -- explain specifically why this company is on the watchlist and what, "
                "concretely, would change that. Never a generic restatement like 'this is a good company' or a "
                "trade/price recommendation."
            ),
        },
        "suggested_watch_duration_days": {
            "type": "integer",
            "description": (
                "How many days from today this situation likely stays worth a human's attention before it's "
                "stale or resolved, given the nature of the events shown -- e.g. a rating action or debt "
                "trajectory plays out over quarters, a single insider trade's signal value fades faster. This is "
                "advisory framing only, never an auto-expiry trigger."
            ),
        },
        "confidence": {"type": "string", "description": "high / medium / low"},
    },
    "required": ["narrative", "suggested_watch_duration_days", "confidence"],
    "additionalProperties": False,
}

WATCH_SUMMARY_SYSTEM_PROMPT = (
    "You are writing the single explanatory narrative a human investor sees when they open a company on their "
    "long-term (12-30 month) fundamental-investing watchlist, focused on Indian smallcap/microcap turnaround and "
    "deleveraging theses. You will be shown every alert accumulated for this company so far (each already judged "
    "worth a human's attention by an earlier triage step), the company's current state vector, simple descriptive "
    "price technicals, its sector's capital-cycle context, and a list of structured signal pointers (promoter/ "
    "institutional holding direction, rating-agency identity and action, named investor tiers, sector growth "
    "classification) -- use the pointers to make specific, grounded claims (e.g. which agency, which investor "
    "tier) instead of vague ones. Synthesize WHY this company is being watched and what would change that -- not "
    "a restatement of each alert, a synthesis across them. Null is correct far more often than a guessed value: "
    "if the evidence doesn't support a claim, don't make it. You never recommend a trade, a position, or a price "
    "target -- you only explain the evidence and its trajectory. Price technicals shown to you are descriptive "
    "facts (e.g. 'up 12% over 3 months'), never a signal to act on -- treat them the same way, as corroborating "
    "or contradicting context, not as a basis for a buy/sell view."
)


def _record_fallback(fallback_type: str, *, reason: str, error, severity: str = "warn", metadata=None) -> None:
    record_local_fallback_event(
        module=SYNC_SOURCE_NAME,
        source="openai",
        fallback_type=fallback_type,
        severity=severity,
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def _bootstrap_narrative_columns() -> None:
    _ensure_watchlist_table()

    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute("ALTER TABLE fundamentals_watchlist ADD COLUMN IF NOT EXISTS narrative_text TEXT")
            cur.execute("ALTER TABLE fundamentals_watchlist ADD COLUMN IF NOT EXISTS narrative_model TEXT")
            cur.execute("ALTER TABLE fundamentals_watchlist ADD COLUMN IF NOT EXISTS narrative_prompt_version INTEGER")
            cur.execute("ALTER TABLE fundamentals_watchlist ADD COLUMN IF NOT EXISTS narrative_generated_at TIMESTAMPTZ")
            cur.execute("ALTER TABLE fundamentals_watchlist ADD COLUMN IF NOT EXISTS suggested_watch_until DATE")

    execute_db_operation(_op, operation_name="fundamentals_watchlist:ensure_narrative_columns")


def load_companies_needing_narrative_refresh(limit: int | None = None) -> pd.DataFrame:
    """Only companies whose alert history moved since their last narrative -- a
    company with no new alert has nothing new to synthesize, see module docstring."""
    query = """
        SELECT company_master_id, last_alert_at, narrative_generated_at, narrative_text
        FROM fundamentals_watchlist
        WHERE narrative_generated_at IS NULL
           OR last_alert_at > narrative_generated_at::date
        ORDER BY last_alert_at ASC NULLS LAST
    """
    if limit:
        query += f" LIMIT {int(limit)}"
    return sql_to_df(query)


def load_company_alerts(company_master_id: str) -> pd.DataFrame:
    return sql_to_df(
        """
        SELECT trigger_type, origin, alert_date, reasoning
        FROM fundamentals_l3_alerts
        WHERE company_master_id = %s
        ORDER BY alert_date ASC NULLS LAST
        """,
        params=(company_master_id,),
    )


def load_latest_l2_state_for_company(company_master_id: str) -> dict | None:
    # BUG FOUND LIVE 2026-08-18: naive removeprefix("nse:") only recovers the
    # correct fundamentals_l2_state.ticker when the company IS its own NSE symbol
    # -- wrong for the ~22% BSE-only cohort, whose L2 ticker is a raw BSE scrip
    # code, not the symbol in their canonical company_master_id. Left this
    # function's evidence bundle silently missing L2 state for that cohort's
    # watchlisted companies, exactly the gap signal_pointers.py was built to
    # close for other fields.
    ticker = build_l1_ticker_by_company_master_id().get(company_master_id)
    if ticker is None:
        return None
    df = sql_to_df(
        """
        SELECT ticker, company_name, net_debt_rscr, net_debt_yoy_delta_rscr,
               net_debt_consecutive_declining_years, net_debt_trend_direction, interest_coverage,
               debt_to_ebitda, cwip_ratio, cwip_ratio_yoy_delta, cwip_ratio_consecutive_declining_years,
               cwip_ratio_trend_direction, pledge_pct, promoter_pct,
               promoter_stake_direction, run_date
        FROM fundamentals_l2_state
        WHERE ticker = %s
        ORDER BY run_date DESC
        LIMIT 1
        """,
        params=(ticker,),
    )
    return df.iloc[0].to_dict() if not df.empty else None


def load_latest_technicals_for_company(company_master_id: str) -> dict | None:
    df = sql_to_df(
        "SELECT * FROM fundamentals_technicals WHERE company_master_id = %s ORDER BY run_date DESC LIMIT 1",
        params=(company_master_id,),
    )
    return df.iloc[0].to_dict() if not df.empty else None


def load_sector_context_for_company(company_master_id: str) -> dict | None:
    df = sql_to_df(
        """
        SELECT ds.sector_code, sr.description AS sector_name, sc.phase,
               sc.capacity_growth_pct, sc.demand_growth_pct, sc.sample_size_confidence
        FROM dim_security ds
        LEFT JOIN LATERAL (
            SELECT description FROM fundamentals_sector_reference
            WHERE code = ds.sector_code AND level = 'sector'
            ORDER BY as_of_date DESC LIMIT 1
        ) sr ON TRUE
        LEFT JOIN LATERAL (
            SELECT phase, capacity_growth_pct, demand_growth_pct, sample_size_confidence
            FROM fundamentals_sector_cycle
            WHERE sector_code = ds.sector_code
            ORDER BY run_date DESC LIMIT 1
        ) sc ON TRUE
        WHERE ds.company_master_id = %s AND ds.sector_code IS NOT NULL
        ORDER BY ds.last_trade_date DESC NULLS LAST, ds.effective_to DESC NULLS LAST
        LIMIT 1
        """,
        params=(company_master_id,),
    )
    return df.iloc[0].to_dict() if not df.empty else None


def build_company_evidence_bundle(
    company_master_id: str,
    alerts: pd.DataFrame,
    l2_state: dict | None,
    technicals: dict | None,
    sector_context: dict | None,
    signal_pointers: list[dict] | None = None,
) -> dict:
    """signal_pointers (2026-08-13, fundamentals/screens/signal_pointers.py) closes a
    real gap found auditing this pipeline: l2_state above only carries the fixed
    column list this function's own SELECT lists, which predates institutional_pct/
    institutional_stake_direction and never included rating_agency or investor tier
    at all -- alerts fired for those events, but the LLM only ever saw the alert's
    fixed reasoning string, not the underlying numbers/identity behind it. Defaults
    to None/[] so existing callers/tests with the old 5-arg shape keep working."""
    return {
        "company_master_id": company_master_id,
        "alerts": alerts.to_dict("records") if not alerts.empty else [],
        "l2_state": l2_state,
        "technicals": technicals,
        "sector_context": sector_context,
        "signal_pointers": signal_pointers or [],
    }


def generate_watch_summary(evidence_bundle: dict, *, model: str = DEFAULT_MODEL) -> dict:
    client = OpenAI(api_key=env("OPENAI_API_KEY"))
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": WATCH_SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(evidence_bundle, ensure_ascii=False, default=str)},
        ],
        response_format={"type": "json_schema", "json_schema": {"name": "watch_summary", "schema": WATCH_SUMMARY_SCHEMA, "strict": True}},
    )
    return json.loads(response.choices[0].message.content)


def _update_narrative(*, company_master_id: str, narrative_text: str, model: str, suggested_watch_until, generated_at) -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute(
                """
                UPDATE fundamentals_watchlist
                   SET narrative_text = %s, narrative_model = %s, narrative_prompt_version = %s,
                       narrative_generated_at = %s, suggested_watch_until = %s
                 WHERE company_master_id = %s
                """,
                (narrative_text, model, PROMPT_VERSION, generated_at, suggested_watch_until, company_master_id),
            )

    execute_db_operation(_op, operation_name="fundamentals_watchlist:update_narrative")


def run_watch_summary_refresh(*, limit: int | None = None, model: str = DEFAULT_MODEL) -> dict[str, object]:
    _bootstrap_narrative_columns()

    candidates = load_companies_needing_narrative_refresh(limit or DEFAULT_BATCH_LIMIT)
    if candidates.empty:
        return {"generated": 0, "failed": 0, "blocked": False, "narrative_events": []}

    counts = {"generated": 0, "failed": 0}
    consecutive_failures = 0
    blocked = False
    narrative_events: list[dict] = []
    now = pd.Timestamp.now(tz="UTC")

    for _, candidate in candidates.iterrows():
        if blocked:
            continue
        company_master_id = candidate["company_master_id"]
        old_narrative_text = candidate.get("narrative_text")
        is_new_candidate = candidate.get("narrative_generated_at") is None

        alerts = load_company_alerts(company_master_id)
        l2_state = load_latest_l2_state_for_company(company_master_id)
        technicals = load_latest_technicals_for_company(company_master_id)
        sector_context = load_sector_context_for_company(company_master_id)
        signal_pointers = get_stock_signal_pointers(company_master_id)
        evidence_bundle = build_company_evidence_bundle(company_master_id, alerts, l2_state, technicals, sector_context, signal_pointers)

        try:
            summary = generate_watch_summary(evidence_bundle, model=model)
            # 2026-08-15 bug found live: summary["narrative"]/["suggested_watch_duration_days"] used
            # to be indexed OUTSIDE this try block -- a malformed/non-conforming LLM response would
            # raise an uncaught KeyError there and abort the ENTIRE remaining batch with no
            # fallback-telemetry record, instead of being handled as this one company's failure like
            # every other error from this same call already is.
            new_narrative_text = summary["narrative"]
            suggested_watch_duration_days = int(summary["suggested_watch_duration_days"])
        except Exception as exc:  # noqa: BLE001 -- classified as a failure either way
            consecutive_failures += 1
            counts["failed"] += 1
            _record_fallback(
                "watch_summary_generation_failed",
                reason="Watch-summary LLM call failed for this company; it stays eligible for retry next run (narrative_generated_at unchanged).",
                error=exc,
                metadata={"company_master_id": company_master_id},
            )
            if consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
                blocked = True
                _record_fallback(
                    "watch_summary_circuit_breaker_tripped",
                    reason=f"{consecutive_failures} consecutive watch-summary failures -- stopping this run immediately.",
                    error="circuit breaker",
                    severity="error",
                )
            continue

        consecutive_failures = 0
        suggested_watch_until = (now.normalize() + pd.Timedelta(days=suggested_watch_duration_days)).date()
        _update_narrative(
            company_master_id=company_master_id,
            narrative_text=new_narrative_text,
            model=model,
            suggested_watch_until=suggested_watch_until,
            generated_at=now,
        )
        counts["generated"] += 1
        narrative_events.append(
            {
                "company_master_id": company_master_id,
                "is_new_candidate": bool(is_new_candidate),
                "narrative_changed": is_new_candidate or (new_narrative_text != old_narrative_text),
                "narrative_text": new_narrative_text,
                "suggested_watch_until": str(suggested_watch_until),
                "confidence": summary.get("confidence"),
            }
        )

    return {**counts, "blocked": blocked, "narrative_events": narrative_events}


def main() -> int:
    global STOCKEY_RUN_STATE
    result = run_watch_summary_refresh()
    STOCKEY_RUN_STATE = {
        "source": SYNC_SOURCE_NAME,
        "generated": result["generated"],
        "rows_written": result["generated"],
        "failed": result["failed"],
        "blocked": result["blocked"],
        "fallback_used": bool(result["failed"] or result["blocked"]),
        "state_advanced": result["generated"] > 0,
        "status": "blocked" if result["blocked"] else "ok",
    }
    print(json.dumps(STOCKEY_RUN_STATE, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
