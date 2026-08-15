"""L3 LLM triage -- fundamental screener step 7, second half
(docs/FUNDAMENTAL_SCREENER_PRD.md sec 3.2/8 step 7, fundamentals/screens/
l3_triggers.py's rule-based half). A second, judgment-based path into
fundamentals_l3_alerts, running ALONGSIDE the deterministic rule pass, not
replacing it -- both can alert on the exact same event (they key on different
trigger_type values, so no collision), and this never bypasses L4: it only flags a
candidate for a human to review, it never opens or sizes a position.

Fires only on an actual event (a filing already in fundamentals_events), never a
standing scan -- same "event x primed state" shape the source PRD requires of the
rule-based triggers, just with an LLM's judgment standing in for the deterministic
rule. "Technical" context is stockey's OWN raw price data (nseindia_ohlcv), not
systrader's systematic signals -- importing those would recouple the two repos
(source PRD sec 3.2 is explicit about this boundary).

Covers results too, unlike the rule-based half (fundamentals/screens/l3_triggers.py
deliberately deferred rule-based results triggers -- reconciling multiple growth-rate
sources needed more design thought than fit that step). Triage doesn't need that
reconciliation; it just shows the LLM the filing content and lets it judge.

Every alert this produces is provenance-stamped -- model, prompt_version, and the
exact evidence bundle (event content + L2 state snapshot + price context) the LLM
was shown, all stored on the alert row -- so a later review can see exactly what the
LLM saw, not just its conclusion (fundamental_basic_goal.md sec 4: "version every
signal definition").
"""

from __future__ import annotations

import json

import pandas as pd
from environs import Env
from openai import OpenAI
from psycopg2 import sql as psycopg2_sql

from fundamentals.screens.l3_triggers import RESULTS_TABLE, _ensure_alerts_table, load_latest_l2_state
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.fallback_telemetry import record_local_fallback_event

env = Env()
env.read_env()

SYNC_SOURCE_NAME = "fundamentals.screens.llm_triage"
STOCKEY_RUN_STATE: dict[str, object] = {}

DEFAULT_MODEL = env("LLM_TRIAGE_MODEL", "gpt-5.4-mini")
PROMPT_VERSION = 1
CIRCUIT_BREAKER_THRESHOLD = 3
DEFAULT_BATCH_LIMIT = 50
PRICE_LOOKBACK_DAYS = 20

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "interesting": {"type": "boolean", "description": "true only if this event, in light of the company's own state and recent price action, is something a human fundamental-thesis reviewer should look at"},
        "reasoning": {"type": "string", "description": "2-3 sentences grounded in the specific evidence shown -- not a generic restatement of the filing"},
        "confidence": {"type": "string", "description": "high / medium / low"},
    },
    "required": ["interesting", "reasoning", "confidence"],
    "additionalProperties": False,
}

TRIAGE_SYSTEM_PROMPT = (
    "You are a triage assistant for a long-term (12-30 month) fundamental-investing screener "
    "focused on Indian smallcap/microcap turnaround and deleveraging theses. You will be shown "
    "one detected filing event for a company, the company's own computed state vector "
    "(debt trajectory, promoter holding trend, pledge, etc.), and simple recent price action. "
    "Judge whether this SPECIFIC event, in light of that state, is worth a human's attention -- "
    "not whether the company is generally interesting. A routine event that merely confirms an "
    "already-known trend is NOT interesting. An event that is surprising given the state, or that "
    "independently corroborates a thesis-relevant trend (e.g. a rating action or insider trade "
    "that lines up with an already-improving or deteriorating debt/pledge/holding trend), IS "
    "interesting. You never recommend a trade, a position, or a price target -- you only flag "
    "whether a human should look closer, and say why in terms of the specific evidence shown."
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


def _bootstrap_triage_column() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'fundamentals_events'")
            if cur.fetchone() is None:
                return
            cur.execute(
                psycopg2_sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS llm_triage_status TEXT").format(
                    psycopg2_sql.Identifier("fundamentals_events")
                )
            )

    execute_db_operation(_op, operation_name="fundamentals_events:ensure_llm_triage_column")


def load_candidate_events_for_triage(limit: int | None = None) -> pd.DataFrame:
    query = """
        SELECT source, news_id, company_master_id, filing_type, headline, subcategory,
               disclosure_date, rating_action_type, transaction_type, insider_name,
               quantity, structured_extraction_json
        FROM fundamentals_events
        WHERE filing_type IN ('rating_action', 'pit_sast', 'results')
          AND (llm_triage_status IS NULL OR llm_triage_status = 'pending')
        ORDER BY load_ts ASC NULLS LAST
    """
    if limit:
        query += f" LIMIT {int(limit)}"
    return sql_to_df(query)


def load_price_context(company_master_id: str, event_date, *, lookback_days: int = PRICE_LOOKBACK_DAYS) -> dict:
    """Simple, observable price action around the event -- stockey's own raw
    nseindia_ohlcv, not systrader's systematic signals (see module docstring)."""
    if event_date is None:
        return {}
    before = sql_to_df(
        "SELECT date, close, volume FROM nseindia_ohlcv WHERE company_master_id = %s AND date <= %s ORDER BY date DESC LIMIT %s",
        params=(company_master_id, event_date, lookback_days),
    )
    after = sql_to_df(
        "SELECT date, close, volume FROM nseindia_ohlcv WHERE company_master_id = %s AND date > %s ORDER BY date ASC LIMIT %s",
        params=(company_master_id, event_date, lookback_days),
    )
    if before.empty:
        return {}

    close_on_event = before.iloc[0]["close"]
    close_lookback_ago = before.iloc[-1]["close"]
    volume_on_event = before.iloc[0]["volume"]
    # 2026-08-15 bug found live: averaging over `before` (which includes the event day itself,
    # row 0) dilutes the ratio by the event's own spike -- confirmed live, e.g. nse:ONWARDTEC's
    # 2026-06-26 event stored 18.1x when the true prior-19-day baseline ratio was 180.9x, a ~10x
    # understatement. Prior-days-only average excludes row 0.
    prior_volume = before["volume"].iloc[1:]
    avg_volume_before = prior_volume.mean() if not prior_volume.empty else None

    context = {
        "close_on_or_before_event": float(close_on_event),
        f"price_pct_change_last_{len(before)}_sessions": round((close_on_event - close_lookback_ago) / close_lookback_ago * 100, 2) if close_lookback_ago else None,
        "volume_on_event_vs_avg_ratio": round(volume_on_event / avg_volume_before, 2) if avg_volume_before else None,
    }
    if not after.empty:
        close_after = after.iloc[-1]["close"]
        context["price_pct_change_since_event"] = round((close_after - close_on_event) / close_on_event * 100, 2) if close_on_event else None
        context["sessions_since_event_available"] = int(len(after))
    return context


def build_evidence_bundle(event: dict, l2_row: dict | None, price_context: dict) -> dict:
    return {
        "event": {
            "filing_type": event.get("filing_type"),
            "headline": event.get("headline"),
            "subcategory": event.get("subcategory"),
            "disclosure_date": str(event.get("disclosure_date")),
            "rating_action_type": event.get("rating_action_type"),
            "transaction_type": event.get("transaction_type"),
            "insider_name": event.get("insider_name"),
            "quantity": event.get("quantity"),
            "structured_extraction": json.loads(event["structured_extraction_json"]) if event.get("structured_extraction_json") else None,
        },
        "l2_state": l2_row,
        "price_context": price_context,
    }


def triage_event(evidence_bundle: dict, *, model: str = DEFAULT_MODEL) -> dict:
    client = OpenAI(api_key=env("OPENAI_API_KEY"))
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": TRIAGE_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(evidence_bundle, ensure_ascii=False, default=str)},
        ],
        response_format={"type": "json_schema", "json_schema": {"name": "triage_judgment", "schema": TRIAGE_SCHEMA, "strict": True}},
    )
    return json.loads(response.choices[0].message.content)


def _set_triage_status(*, source: str, news_id: str, status: str) -> None:
    def _update() -> None:
        with db_session() as (_, cur):
            cur.execute(
                "UPDATE fundamentals_events SET llm_triage_status = %s WHERE source = %s AND news_id = %s",
                (status, source, news_id),
            )

    execute_db_operation(_update, operation_name="fundamentals_events:llm_triage_status_update")


def run_llm_triage(*, limit: int | None = None, model: str = DEFAULT_MODEL) -> dict[str, object]:
    _bootstrap_triage_column()
    _ensure_alerts_table()

    events = load_candidate_events_for_triage(limit or DEFAULT_BATCH_LIMIT)
    if events.empty:
        return {"flagged": 0, "not_interesting": 0, "failed": 0, "blocked": False}

    l2_state = load_latest_l2_state()
    l2_by_ticker = {row["ticker"]: row for row in l2_state.to_dict("records")}

    counts = {"flagged": 0, "not_interesting": 0, "failed": 0}
    consecutive_failures = 0
    blocked = False
    alert_rows: list[dict] = []

    for _, event in events.iterrows():
        if blocked:
            continue
        event_dict = event.to_dict()
        ticker = str(event_dict.get("company_master_id") or "").removeprefix("nse:")
        l2_row = l2_by_ticker.get(ticker)
        price_context = load_price_context(event_dict.get("company_master_id"), event_dict.get("disclosure_date"))
        evidence_bundle = build_evidence_bundle(event_dict, l2_row, price_context)

        try:
            judgment = triage_event(evidence_bundle, model=model)
        except Exception as exc:  # noqa: BLE001 -- classified as a failure either way
            consecutive_failures += 1
            counts["failed"] += 1
            _set_triage_status(source=event_dict["source"], news_id=event_dict["news_id"], status="failed")
            _record_fallback(
                "llm_triage_failed",
                reason="LLM triage call failed for this event; it stays llm_triage_status=failed permanently -- load_candidate_events_for_triage only re-selects NULL/pending, so this needs a manual UPDATE to retry, not an automatic one.",
                error=exc,
                metadata={"news_id": event_dict["news_id"]},
            )
            if consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
                blocked = True
                _record_fallback(
                    "llm_triage_circuit_breaker_tripped",
                    reason=f"{consecutive_failures} consecutive LLM triage failures -- stopping this run immediately.",
                    error="circuit breaker",
                    severity="error",
                )
            continue

        consecutive_failures = 0
        if not judgment.get("interesting"):
            counts["not_interesting"] += 1
            _set_triage_status(source=event_dict["source"], news_id=event_dict["news_id"], status="not_interesting")
            continue

        counts["flagged"] += 1
        alert_rows.append(
            {
                "source": event_dict["source"],
                "news_id": event_dict["news_id"],
                "trigger_type": "llm_flagged",
                "origin": "llm_triage",
                "company_master_id": event_dict.get("company_master_id"),
                "alert_date": event_dict.get("disclosure_date"),
                "reasoning": judgment.get("reasoning"),
                "l2_run_date": str(l2_row.get("run_date")) if l2_row is not None else None,
                "l2_state_snapshot_json": json.dumps(l2_row, ensure_ascii=False, default=str) if l2_row is not None else None,
                "status": "new",
                "model": model,
                "prompt_version": PROMPT_VERSION,
                "evidence_bundle_json": json.dumps(evidence_bundle, ensure_ascii=False, default=str),
                "load_ts": pd.Timestamp.now(tz="UTC"),
            }
        )
        _set_triage_status(source=event_dict["source"], news_id=event_dict["news_id"], status="flagged")

    if alert_rows:
        upsert_to_db(pd.DataFrame(alert_rows), RESULTS_TABLE, unique_keys=["source", "news_id", "trigger_type"])

    return {**counts, "blocked": blocked}


def main() -> int:
    global STOCKEY_RUN_STATE
    result = run_llm_triage()
    STOCKEY_RUN_STATE = {
        "source": SYNC_SOURCE_NAME,
        "flagged": result["flagged"],
        "rows_written": result["flagged"],
        "not_interesting": result["not_interesting"],
        "failed": result["failed"],
        "blocked": result["blocked"],
        "fallback_used": bool(result["failed"] or result["blocked"]),
        "state_advanced": result["flagged"] > 0,
        "status": "blocked" if result["blocked"] else "ok",
    }
    print(json.dumps(STOCKEY_RUN_STATE, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
