"""LLM story read (reevaluation PRD 3.4, build step 5): one structured model read of a company
when its story may have moved, replacing the per-filing yes/no `llm_triage`.

WHO gets read (never all 1,300): a company with
  - a material story-score change (fundamentals_story_score_change: band entry/exit, a 15-point
    move, a new primary story, a flaw appearing or clearing), or
  - a substantive filing of its own (results, capital raise, rating action, auditor change,
    related-party transaction) whose extraction is done -- this keeps what triage caught:
    a filing can matter before the numbers move (screener's quarter lands a night later),
and only for evidence not already read: each change / filing is recorded in
`fundamentals_story_read_seen` once a read has covered it. Capped per run (READ_CAP), results
filings and band entries first. The catch-up cohort and history rows are excluded.

WHAT the model sees: the story score with its strongest readings and flaws, the results
reading (latest quarter vs the company's own trend, one-offs), ownership and debt directions,
valuation vs own history, recent substantive filings with their extracted fields, and recent
material news. It returns: the ONE story, the ONE deal-breaker (or none), direction, materiality
1-5, horizon, the key numbers, and what the numbers miss. Stored in `fundamentals_story_read`
with the exact evidence it was shown.

WHAT it changes: a read with materiality >= ALERT_MIN_MATERIALITY becomes an L3 alert --
`story_read_positive` (improving, no deal-breaker) or `story_read_negative` (deteriorating) --
so the watchlist keeps a judged path in until it moves onto the score (step 6). These alerts do
NOT feed the story score (a read triggered by a score move must not push the score further),
nor confluence or the portfolio ruleset (frozen v2 inputs). Measured forward in event drift as
its own pre-registered family (systrader LEDGER row 51).

    python -m fundamentals.screens.story_read [--limit N] [--dry-run]
"""

from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd
from environs import Env
from openai import OpenAI

from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.fallback_telemetry import record_local_fallback_event
from utils.json_safe import dumps_strict

env = Env()
env.read_env()

SYNC_SOURCE_NAME = "fundamentals.screens.story_read"
READ_TABLE = "fundamentals_story_read"
SEEN_TABLE = "fundamentals_story_read_seen"
ALERTS_TABLE = "fundamentals_l3_alerts"
DEFAULT_MODEL = env("STORY_READ_MODEL", "gpt-5.4-mini")
PROMPT_VERSION = 1
READ_CAP = env.int("STORY_READ_CAP", 60)
ALERT_MIN_MATERIALITY = 4
# evidence older than this is not "new" any more (a first run must not read months of history)
FRESH_DAYS = 3
FILING_LOOKBACK_DAYS = 45
NEWS_LOOKBACK_DAYS = 30
# a filing is read once its extraction is done, or after this long on its headline alone (no
# attachment, OCR failed, or extraction lagging) -- never silently skipped
EXTRACTION_WAIT_HOURS = 12
SUBSTANTIVE_FILINGS = ("results", "capital_raise", "rating_action", "auditor_change", "related_party_transaction")
CIRCUIT_BREAKER_THRESHOLD = 3
STOCKEY_RUN_STATE: dict[str, object] = {}

_DDL = [
    f"""CREATE TABLE IF NOT EXISTS {READ_TABLE} (
        company_master_id TEXT NOT NULL,
        read_at TIMESTAMPTZ NOT NULL,
        symbol TEXT,
        trigger_refs TEXT,
        story TEXT,
        story_dimension TEXT,
        deal_breaker TEXT,
        deal_breaker_dimension TEXT,
        direction TEXT,
        materiality INTEGER,
        horizon TEXT,
        key_numbers_json TEXT,
        numbers_miss TEXT,
        confidence INTEGER,
        alert_trigger_type TEXT,
        story_score DOUBLE PRECISION,
        model TEXT,
        prompt_version INTEGER,
        evidence_json TEXT,
        PRIMARY KEY (company_master_id, read_at)
    )""",
    f"""CREATE TABLE IF NOT EXISTS {SEEN_TABLE} (
        company_master_id TEXT NOT NULL,
        ref TEXT NOT NULL,
        read_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (company_master_id, ref)
    )""",
]

DIMENSIONS = ["growth", "margins", "profitability", "balance_sheet", "value", "ownership", "sector",
              "governance", "events", "other"]

SYSTEM_PROMPT = """You are the analyst for a long-term, fundamentals-driven Indian equity portfolio.
You get one company's evidence: its story score (0-100: how exceptional its best fundamental
reading is within its peer group) and strongest readings, its flaws, its latest quarter read
against its own trend (one-off items already stripped), ownership and debt trends, valuation
versus its own history, recent exchange filings with extracted fields, and recent news.

Answer as a sceptical analyst:
- story: the ONE thing that would make this business worth owning now, in one sentence with
  numbers. If nothing stands out, say so.
- deal_breaker: the ONE thing that would stop you owning it, in one sentence -- or null if none.
  A flaw already listed counts only if it is real on the evidence.
- direction of the investment case given the NEW evidence: improving, deteriorating, unchanged.
- materiality 1-5 for a long-term holder: 1 noise, 3 worth knowing, 4 changes position sizing,
  5 changes the whole case. Routine filings are 1-2.
- horizon over which the story plays out: quarters, 1-2 years, 3+ years.
- key_numbers: up to 5 figures from the evidence that carry the case (label + value as text).
- numbers_miss: what the filings / news show that the numbers do not yet (a new order, product,
  regulation, management change) -- or null.
- confidence 1-5 in your read given the evidence quality.
Use only the evidence. Do not recommend buying or selling; do not guess at share-price moves."""

SCHEMA = {
    "type": "object",
    "properties": {
        "story": {"type": "string"},
        "story_dimension": {"type": "string", "enum": DIMENSIONS},
        "deal_breaker": {"type": ["string", "null"]},
        "deal_breaker_dimension": {"type": ["string", "null"], "enum": [*DIMENSIONS, None]},
        "direction": {"type": "string", "enum": ["improving", "deteriorating", "unchanged"]},
        "materiality": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "horizon": {"type": "string", "enum": ["quarters", "1-2 years", "3+ years"]},
        "key_numbers": {"type": "array", "maxItems": 5, "items": {
            "type": "object", "properties": {"label": {"type": "string"}, "value": {"type": "string"}},
            "required": ["label", "value"], "additionalProperties": False}},
        "numbers_miss": {"type": ["string", "null"]},
        "confidence": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
    },
    "required": ["story", "story_dimension", "deal_breaker", "deal_breaker_dimension", "direction", "materiality",
                 "horizon", "key_numbers", "numbers_miss", "confidence"],
    "additionalProperties": False,
}


def ensure_tables() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            for statement in _DDL:
                cur.execute(statement)

    execute_db_operation(_op, operation_name=f"{READ_TABLE}:ensure_tables")


# --- who ----------------------------------------------------------------------------------------

PRIORITY = {"filing:results": 0, "band_enter": 1, "story_changed": 2, "flaw_appeared": 3, "band_exit": 4,
            "story_strengthening": 5, "story_fading": 6}


def pending_triggers() -> pd.DataFrame:
    """(company_master_id, ref, kind, at) for unread material changes and substantive filings."""
    changes = sql_to_df(
        f"""
        SELECT c.company_master_id, 'change:' || c.kind || ':' || c.changed_at::text AS ref, c.kind, c.changed_at AS at
          FROM fundamentals_story_score_change c
         WHERE c.changed_at >= now() - make_interval(days => %s)
           AND NOT EXISTS (SELECT 1 FROM {SEEN_TABLE} s WHERE s.company_master_id = c.company_master_id
                            AND s.ref = 'change:' || c.kind || ':' || c.changed_at::text)
        """, params=(FRESH_DAYS,)) if _exists("fundamentals_story_score_change") else pd.DataFrame()
    filings = sql_to_df(
        f"""
        SELECT e.company_master_id, 'filing:' || e.source || ':' || e.news_id AS ref,
               'filing:' || e.filing_type AS kind, e.detected_at AS at
          FROM fundamentals_events e
         WHERE e.detected_at >= now() - make_interval(days => %s)
           AND e.filing_type = ANY(%s)
           AND e.company_master_id IS NOT NULL
           AND e.admission_cohort IS NULL
           AND coalesce(e.rule_trigger_status, '') <> 'historical'
           AND (e.structured_extraction_status = 'done' OR e.detected_at < now() - make_interval(hours => %s))
           AND NOT EXISTS (SELECT 1 FROM {SEEN_TABLE} s WHERE s.company_master_id = e.company_master_id
                            AND s.ref = 'filing:' || e.source || ':' || e.news_id)
        """, params=(FRESH_DAYS, list(SUBSTANTIVE_FILINGS), EXTRACTION_WAIT_HOURS))
    out = pd.concat([d for d in (changes, filings) if not d.empty], ignore_index=True) if not (changes.empty and filings.empty) \
        else pd.DataFrame(columns=["company_master_id", "ref", "kind", "at"])
    return out


def pick_companies(triggers: pd.DataFrame, universe_ids: set[str], cap: int) -> list[tuple[str, list[str], list[str]]]:
    """Companies to read, most important first: [(company_master_id, refs, kinds)]."""
    t = triggers[triggers["company_master_id"].isin(universe_ids)].copy()
    if t.empty:
        return []
    t["prio"] = t["kind"].map(lambda k: PRIORITY.get(k, 7))
    g = t.groupby("company_master_id").agg(prio=("prio", "min"), at=("at", "max"),
                                          refs=("ref", list), kinds=("kind", lambda k: sorted(set(k))))
    g = g.sort_values(["prio", "at"], ascending=[True, False]).head(cap)
    return [(cid, r.refs, r.kinds) for cid, r in g.iterrows()]


def _exists(table: str) -> bool:
    return not sql_to_df("SELECT 1 FROM information_schema.tables WHERE table_name = %s", params=(table,)).empty


# --- what the model sees ------------------------------------------------------------------------

def _clean(v):
    if isinstance(v, (float, np.floating)):
        return None if np.isnan(v) else round(float(v), 2)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (pd.Timestamp,)):
        return str(v)
    return v


def top_readings(readings_json: str | None, n: int = 6) -> list[dict]:
    try:
        readings = json.loads(readings_json) if readings_json else {}
    except (TypeError, ValueError):
        return []
    flat = [{"dimension": d, "reading": name, "percentile_in_group": r.get("pct"), "kind": r.get("kind")}
            for d, rs in readings.items() for name, r in rs.items()]
    return sorted(flat, key=lambda x: -(x["percentile_in_group"] or 0))[:n]


def evidence_for(cid: str, scored: pd.Series | None, inputs: pd.Series | None, kinds: list[str]) -> dict:
    filings = sql_to_df(
        """
        SELECT filing_type, headline, detected_at::date AS detected, structured_extraction_json
          FROM fundamentals_events
         WHERE company_master_id = %s AND filing_type = ANY(%s)
           AND detected_at >= now() - make_interval(days => %s)
         ORDER BY detected_at DESC LIMIT 6
        """, params=(cid, list(SUBSTANTIVE_FILINGS), FILING_LOOKBACK_DAYS))
    news = sql_to_df(
        """
        SELECT n.title, t.direction, t.materiality, n.first_seen_at::date AS seen
          FROM fundamentals_news_tag t JOIN fundamentals_news_item n USING (link)
         WHERE t.target_type = 'company' AND t.target_id = %s AND t.materiality >= 3
           AND n.first_seen_at >= now() - make_interval(days => %s)
         ORDER BY n.first_seen_at DESC LIMIT 8
        """, params=(cid, NEWS_LOOKBACK_DAYS)) if _exists("fundamentals_news_tag") else pd.DataFrame()
    inp = inputs if inputs is not None else pd.Series(dtype=object)
    sc = scored if scored is not None else pd.Series(dtype=object)
    rr_fields = ["rr_latest_period_end", "rr_sales_q_cr", "rr_sales_yoy_pct", "rr_sales_trend_yoy_pct", "rr_sales_direction",
                 "rr_profit_yoy_pct", "rr_profit_trend_yoy_pct", "rr_profit_direction", "rr_one_off_cr", "rr_one_off_share",
                 "rr_opm_pct", "rr_margin_change_pp", "rr_margin_vs_trend_pp"]
    return {
        "company": {"id": cid, "symbol": _clean(sc.get("symbol")), "group": _clean(inp.get("group")),
                    "market_cap_cr": _clean(inp.get("market_cap_cr"))},
        "why_now": kinds,
        "story_score": {"score": _clean(sc.get("story_score")), "primary": _clean(sc.get("primary_dimension")),
                        "flaws": _clean(sc.get("flaws")), "strongest_readings": top_readings(sc.get("readings_json"))},
        "latest_quarter_vs_own_trend": {k.removeprefix("rr_"): _clean(inp.get(k)) for k in rr_fields},
        "long_run": {k: _clean(inp.get(k)) for k in ("sales_growth_5y_pct", "profit_growth_5y_pct", "roce_5y_avg_pct",
                                                     "roe_5y_avg_pct", "debt_to_equity", "interest_cover")},
        "ownership_and_debt": {k: _clean(inp.get(k)) for k in ("promoter_stake_direction", "institutional_stake_direction",
                                                               "promoter_pledged_pct", "net_debt_trend_direction")},
        "valuation": {"pe": _clean(inp.get("pe")), "vs_own_history_ratio": _clean(inp.get("valuation_vs_own_history_ratio")),
                      "earnings_yield_pct": _clean(inp.get("earnings_yield_pct"))},
        "recent_filings": [{"type": r.filing_type, "headline": r.headline, "detected": str(r.detected),
                            "extracted": (r.structured_extraction_json or "")[:1500] or None} for r in filings.itertuples()],
        "recent_news": [{"title": r.title, "direction": r.direction, "materiality": int(r.materiality), "seen": str(r.seen)}
                        for r in news.itertuples()] if not news.empty else [],
    }


def read_company(evidence: dict, *, model: str = DEFAULT_MODEL) -> dict:
    client = OpenAI(api_key=env("OPENAI_API_KEY"))
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": dumps_strict(evidence)}],
        response_format={"type": "json_schema", "json_schema": {"name": "story_read", "schema": SCHEMA, "strict": True}},
    )
    return json.loads(response.choices[0].message.content)


def alert_type(read: dict) -> str | None:
    if int(read.get("materiality") or 0) < ALERT_MIN_MATERIALITY:
        return None
    if read.get("direction") == "improving" and not read.get("deal_breaker"):
        return "story_read_positive"
    if read.get("direction") == "deteriorating":
        return "story_read_negative"
    return None


# --- the run ------------------------------------------------------------------------------------

def run(limit: int | None = None, *, model: str = DEFAULT_MODEL, dry_run: bool = False) -> dict[str, object]:
    from fundamentals.screens import story_score

    ensure_tables()
    as_of = pd.Timestamp.now(tz="Asia/Kolkata").date()
    inputs = story_score.load_inputs(as_of)
    if inputs.empty:
        return {"read": 0, "alerts": 0, "failed": 0, "candidates": 0}
    scored = story_score.score(inputs).set_index("company_master_id")
    inputs = inputs.drop_duplicates("company_master_id").set_index("company_master_id")
    picks = pick_companies(pending_triggers(), set(scored.index), limit or READ_CAP)
    if dry_run:
        return {"candidates": len(picks), "picks": [(c, k) for c, _, k in picks][:20]}

    reads, alerts, seen = [], [], []
    failed = consecutive = 0
    for cid, refs, kinds in picks:
        evidence = evidence_for(cid, scored.loc[cid] if cid in scored.index else None,
                                inputs.loc[cid] if cid in inputs.index else None, kinds)
        try:
            read = read_company(evidence, model=model)
        except Exception as exc:  # noqa: BLE001 -- unread evidence stays pending for the next run
            failed += 1
            consecutive += 1
            record_local_fallback_event(module=SYNC_SOURCE_NAME, source="openai", fallback_type="story_read_failed",
                                        severity="warn", reason="story read failed; retried next run",
                                        error=repr(exc), metadata={"company_master_id": cid})
            if consecutive >= CIRCUIT_BREAKER_THRESHOLD:
                break
            continue
        consecutive = 0
        now = pd.Timestamp.now(tz="UTC")
        trig = alert_type(read)
        symbol = evidence["company"]["symbol"]
        reads.append({"company_master_id": cid, "read_at": now, "symbol": symbol, "trigger_refs": json.dumps(refs),
                      "story": read["story"], "story_dimension": read["story_dimension"],
                      "deal_breaker": read["deal_breaker"], "deal_breaker_dimension": read["deal_breaker_dimension"],
                      "direction": read["direction"], "materiality": read["materiality"], "horizon": read["horizon"],
                      "key_numbers_json": json.dumps(read["key_numbers"]), "numbers_miss": read["numbers_miss"],
                      "confidence": read["confidence"], "alert_trigger_type": trig,
                      "story_score": evidence["story_score"]["score"], "model": model, "prompt_version": PROMPT_VERSION,
                      "evidence_json": dumps_strict(evidence)})
        seen.extend({"company_master_id": cid, "ref": r, "read_at": now} for r in refs)
        if trig:
            alerts.append({"source": "story_read", "news_id": f"{cid}:{now.isoformat()}", "trigger_type": trig,
                           "origin": "llm_story_read", "company_master_id": cid, "alert_date": as_of,
                           "reasoning": read["story"] if trig == "story_read_positive" else (read["deal_breaker"] or read["story"]),
                           "status": "new", "model": model, "prompt_version": PROMPT_VERSION,
                           "evidence_bundle_json": dumps_strict(evidence), "load_ts": now})
    if reads:
        upsert_to_db(pd.DataFrame(reads), READ_TABLE, unique_keys=["company_master_id", "read_at"])
        if alerts:
            upsert_to_db(pd.DataFrame(alerts), ALERTS_TABLE, unique_keys=["source", "news_id", "trigger_type"])
        # evidence counts as read only once the read itself is stored
        upsert_to_db(pd.DataFrame(seen), SEEN_TABLE, unique_keys=["company_master_id", "ref"], on_conflict="nothing")
    return {"candidates": len(picks), "read": len(reads), "alerts": len(alerts), "failed": failed}


def main(argv: list[str] | None = None) -> int:
    global STOCKEY_RUN_STATE
    parser = argparse.ArgumentParser(description="LLM story read for companies whose story may have moved.")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true", help="list who would be read; no model calls")
    args = parser.parse_args(argv)
    result = run(args.limit, dry_run=args.dry_run)
    STOCKEY_RUN_STATE = {"source": SYNC_SOURCE_NAME, "rows": result.get("read", 0), "rows_written": result.get("read", 0),
                         **result, "fallback_used": bool(result.get("failed")), "state_advanced": bool(result.get("read"))}
    print(json.dumps({"status": "ok", **STOCKEY_RUN_STATE}, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
