"""Event-driven re-evaluation (reevaluation PRD 4.2, build step 4).

Every 30 minutes (all_fundamentals_reeval.sh) after the intraday BSE pass, extraction, rule
triggers and news tagging have run, this job:

  1. ENQUEUES companies touched by anything new since its last pass (a watermark per source):
     - the company's own filing (fundamentals_events.detected_at) and any alert on it
       (fundamentals_l3_alerts.load_ts -- OCR is asynchronous, so an alert can come hours
       after its filing);
     - a new quarter in fundamentals_quarterly_results (first_seen_at);
     - a material, directional news tag (fundamentals_news_tag.tagged_at) naming the company;
     - READ-ACROSS: a peer's results filing or a sector news tag re-scores the sector's other
       companies only where the sector IS their story or the tag's dimension is their primary
       one (PRD 4.1 rule) -- everyone else waits for the nightly full run.
     One queue row per (company, cause, cause_ref); the PRD's "written by every collector" is
     done here by watermark instead, so no collector changes and nothing is missed when a
     collector runs from somewhere else.
  2. RE-SCORES: the story score is recomputed for the whole universe (percentiles need the
     group; ~2 s) and APPLIED only to the queued companies.
  3. RECORDS material changes against each company's live score
     (fundamentals_story_score_live), with hysteresis so small moves do not flicker:
       band_enter        score reaches BAND_ENTER from outside the band
       band_exit         an in-band score falls below BAND_EXIT
       story_strengthening / story_fading   a move of MATERIAL_MOVE points or more
       story_changed     a different primary dimension, at BAND_EXIT or above
       flaw_appeared / flaw_cleared
     Thresholds are defaults (PRD 3.3 learns them later). Nothing here trades: the portfolio
     still decides once a day at the close.

The nightly story_score step calls apply_scores() for every company with cause 'daily', so
the live table and the change record also cover what only the nightly data refresh moves.

    python -m fundamentals.screens.reeval             # one pass
"""

from __future__ import annotations

import argparse
import json

import pandas as pd

from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db

SYNC_SOURCE_NAME = "fundamentals.screens.reeval"
QUEUE_TABLE = "fundamentals_reeval_queue"
WATERMARK_TABLE = "fundamentals_reeval_watermark"
LIVE_TABLE = "fundamentals_story_score_live"
CHANGE_TABLE = "fundamentals_story_score_change"
STOCKEY_RUN_STATE: dict[str, object] = {}

BAND_ENTER = 80.0
BAND_EXIT = 65.0
MATERIAL_MOVE = 15.0
NEWS_MIN_MATERIALITY = 3
# first pass: look back this far instead of re-reading all history
FIRST_PASS_LOOKBACK_HOURS = 24

# filing type -> dimension it touches (PRD 4.1)
FILING_DIMENSION = {
    "results": "growth", "rating_action": "balance_sheet", "capital_raise": "balance_sheet",
    "pit_sast": "ownership", "bulk_deal_buy": "ownership", "bulk_deal_sell": "ownership",
    "institutional_entry": "ownership", "auditor_change": "governance", "related_party_transaction": "governance",
}

_DDL = [
    f"""CREATE TABLE IF NOT EXISTS {QUEUE_TABLE} (
        company_master_id TEXT NOT NULL,
        cause TEXT NOT NULL,
        cause_ref TEXT NOT NULL,
        dimension TEXT,
        enqueued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        processed_at TIMESTAMPTZ,
        PRIMARY KEY (company_master_id, cause, cause_ref)
    )""",
    f"""CREATE TABLE IF NOT EXISTS {WATERMARK_TABLE} (
        source TEXT PRIMARY KEY,
        last_seen_at TIMESTAMPTZ NOT NULL
    )""",
    f"""CREATE TABLE IF NOT EXISTS {LIVE_TABLE} (
        company_master_id TEXT PRIMARY KEY,
        symbol TEXT,
        story_score DOUBLE PRECISION,
        primary_dimension TEXT,
        flaws TEXT,
        in_band BOOLEAN,
        score_version INTEGER,
        scored_at TIMESTAMPTZ,
        cause TEXT
    )""",
    f"""CREATE TABLE IF NOT EXISTS {CHANGE_TABLE} (
        company_master_id TEXT NOT NULL,
        changed_at TIMESTAMPTZ NOT NULL,
        kind TEXT NOT NULL,
        symbol TEXT,
        score_before DOUBLE PRECISION,
        score_after DOUBLE PRECISION,
        primary_before TEXT,
        primary_after TEXT,
        flaws_before TEXT,
        flaws_after TEXT,
        causes TEXT,
        score_version INTEGER,
        PRIMARY KEY (company_master_id, changed_at, kind)
    )""",
]


def ensure_tables() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            for statement in _DDL:
                cur.execute(statement)

    execute_db_operation(_op, operation_name=f"{QUEUE_TABLE}:ensure_tables")


# --- 1. enqueue ---------------------------------------------------------------------------------

def _watermark(source: str, now: pd.Timestamp) -> pd.Timestamp:
    wm = sql_to_df(f"SELECT last_seen_at FROM {WATERMARK_TABLE} WHERE source = %s", params=(source,))  # noqa: S608
    if wm.empty:
        return now - pd.Timedelta(hours=FIRST_PASS_LOOKBACK_HOURS)
    return pd.Timestamp(wm["last_seen_at"].iloc[0])


def _set_watermarks(values: dict[str, pd.Timestamp]) -> None:
    if values:
        upsert_to_db(pd.DataFrame([{"source": k, "last_seen_at": v} for k, v in values.items()]),
                     WATERMARK_TABLE, unique_keys=["source"])


def sector_followers(live: pd.DataFrame, sectors: pd.DataFrame, sector_code: str, dimension: str | None) -> list[str]:
    """Companies in the sector whose live story IS the sector, or whose primary dimension is
    the one the news touches."""
    if live.empty or sectors.empty:
        return []
    ids = set(sectors.loc[sectors["sector_code"] == sector_code, "company_master_id"])
    wanted = {"sector"} | ({dimension} if dimension else set())
    hit = live[live["company_master_id"].isin(ids) & live["primary_dimension"].isin(wanted)]
    return hit["company_master_id"].tolist()


def collect_causes(since: dict[str, pd.Timestamp], live: pd.DataFrame, sectors: pd.DataFrame) -> tuple[list[dict], dict[str, pd.Timestamp]]:
    """Queue rows for everything new since each source's watermark, and the new watermarks."""
    rows: list[dict] = []
    marks: dict[str, pd.Timestamp] = {}
    sector_of = dict(zip(sectors.get("company_master_id", []), sectors.get("sector_code", [])))

    ev = sql_to_df(
        "SELECT source, news_id, company_master_id, filing_type, detected_at FROM fundamentals_events "
        "WHERE detected_at > %s AND company_master_id IS NOT NULL AND filing_type <> 'results_calendar'",
        params=(since["events"].to_pydatetime(),))
    for r in ev.itertuples():
        dim = FILING_DIMENSION.get(r.filing_type)
        rows.append({"company_master_id": r.company_master_id, "cause": f"filing:{r.filing_type}",
                     "cause_ref": f"{r.source}:{r.news_id}", "dimension": dim})
        if r.filing_type == "results" and r.company_master_id in sector_of:
            for peer in sector_followers(live, sectors, sector_of[r.company_master_id], None):
                if peer != r.company_master_id:
                    rows.append({"company_master_id": peer, "cause": "peer_results", "cause_ref": f"{r.source}:{r.news_id}",
                                 "dimension": "sector"})
    if not ev.empty:
        marks["events"] = pd.Timestamp(ev["detected_at"].max())

    al = sql_to_df(
        "SELECT source, news_id, company_master_id, trigger_type, load_ts FROM fundamentals_l3_alerts "
        "WHERE load_ts > %s AND company_master_id IS NOT NULL", params=(since["alerts"].to_pydatetime(),))
    for r in al.itertuples():
        rows.append({"company_master_id": r.company_master_id, "cause": f"alert:{r.trigger_type}",
                     "cause_ref": f"{r.source}:{r.news_id}", "dimension": "events"})
    if not al.empty:
        marks["alerts"] = pd.Timestamp(al["load_ts"].max())

    if _exists("fundamentals_quarterly_results"):
        q = sql_to_df(
            """
            SELECT DISTINCT ON (q.company_id) s.company_master_id, q.period_end, q.first_seen_at
              FROM fundamentals_quarterly_results q
              JOIN (SELECT DISTINCT ON (screener_company_id) screener_company_id, company_master_id
                      FROM fundamentals_snapshot_daily ORDER BY screener_company_id, as_of_date DESC) s
                ON s.screener_company_id::bigint = q.company_id
             WHERE q.first_seen_at > %s
             ORDER BY q.company_id, q.period_end DESC
            """, params=(since["quarters"].to_pydatetime(),))
        for r in q.dropna(subset=["company_master_id"]).itertuples():
            rows.append({"company_master_id": r.company_master_id, "cause": "new_quarter",
                         "cause_ref": str(r.period_end), "dimension": "growth"})
        if not q.empty:
            marks["quarters"] = pd.Timestamp(q["first_seen_at"].max())

    if _exists("fundamentals_news_tag"):
        news = sql_to_df(
            "SELECT link, target_type, target_id, dimension, tagged_at FROM fundamentals_news_tag "
            "WHERE tagged_at > %s AND materiality >= %s AND direction IN ('positive', 'negative')",
            params=(since["news"].to_pydatetime(), NEWS_MIN_MATERIALITY))
        for r in news.itertuples():
            if r.target_type == "company":
                rows.append({"company_master_id": r.target_id, "cause": "news", "cause_ref": r.link, "dimension": r.dimension})
            else:
                for cid in sector_followers(live, sectors, r.target_id, r.dimension):
                    rows.append({"company_master_id": cid, "cause": "sector_news", "cause_ref": r.link, "dimension": "sector"})
        if not news.empty:
            marks["news"] = pd.Timestamp(news["tagged_at"].max())
    return rows, marks


def _exists(table: str) -> bool:
    return not sql_to_df("SELECT 1 FROM information_schema.tables WHERE table_name = %s", params=(table,)).empty


# --- 3. material changes ------------------------------------------------------------------------

def _flaw_set(value) -> set[str]:
    return {f.strip() for f in str(value).split(";") if f.strip()} if isinstance(value, str) else set()


def changes_for(before: dict | None, after: dict) -> list[str]:
    """Material change kinds between a company's live score and its new one."""
    if before is None:
        return []  # first sighting is the baseline, not news
    kinds = []
    b, a = before.get("story_score"), after.get("story_score")
    was_in = bool(before.get("in_band"))
    if pd.notna(a):
        if not was_in and a >= BAND_ENTER:
            kinds.append("band_enter")
        elif was_in and a < BAND_EXIT:
            kinds.append("band_exit")
        if pd.notna(b) and a - b >= MATERIAL_MOVE:
            kinds.append("story_strengthening")
        elif pd.notna(b) and b - a >= MATERIAL_MOVE:
            kinds.append("story_fading")
        if (before.get("primary_dimension") and after.get("primary_dimension")
                and before["primary_dimension"] != after["primary_dimension"] and a >= BAND_EXIT):
            kinds.append("story_changed")
    fb, fa = _flaw_set(before.get("flaws")), _flaw_set(after.get("flaws"))
    if fa - fb:
        kinds.append("flaw_appeared")
    if fb - fa:
        kinds.append("flaw_cleared")
    return kinds


def next_in_band(before: dict | None, score) -> bool:
    """Hysteresis: enter at BAND_ENTER, stay until below BAND_EXIT."""
    if pd.isna(score):
        return False
    if before is not None and before.get("in_band"):
        return score >= BAND_EXIT
    return score >= BAND_ENTER


def apply_scores(scored: pd.DataFrame, *, causes: dict[str, str] | str, score_version: int) -> dict[str, int]:
    """Write new live scores for the companies in `scored` and record material changes.
    causes: per company_master_id, or one cause for all."""
    ensure_tables()
    if scored.empty:
        return {"applied": 0, "changes": 0}
    ids = scored["company_master_id"].dropna().unique().tolist()
    live = sql_to_df(f"SELECT * FROM {LIVE_TABLE} WHERE company_master_id = ANY(%s)", params=(ids,))  # noqa: S608
    before_by_id = {r["company_master_id"]: r for r in live.to_dict("records")}
    now = pd.Timestamp.now(tz="UTC")
    live_rows, change_rows = [], []
    for r in scored.dropna(subset=["company_master_id"]).to_dict("records"):
        cid = r["company_master_id"]
        before = before_by_id.get(cid)
        # a version bump re-baselines rather than reading as a jump
        if before is not None and before.get("score_version") != score_version:
            before = None
        cause = causes if isinstance(causes, str) else causes.get(cid, "")
        after = {"story_score": r.get("story_score"), "primary_dimension": r.get("primary_dimension"), "flaws": r.get("flaws")}
        in_band = next_in_band(before, r.get("story_score"))
        live_rows.append({"company_master_id": cid, "symbol": r.get("symbol"), **after, "in_band": in_band,
                          "score_version": score_version, "scored_at": now, "cause": cause})
        for kind in changes_for(before, after):
            change_rows.append({"company_master_id": cid, "changed_at": now, "kind": kind, "symbol": r.get("symbol"),
                                "score_before": before.get("story_score"), "score_after": after["story_score"],
                                "primary_before": before.get("primary_dimension"), "primary_after": after["primary_dimension"],
                                "flaws_before": before.get("flaws"), "flaws_after": after["flaws"],
                                "causes": cause, "score_version": score_version})
    upsert_to_db(pd.DataFrame(live_rows), LIVE_TABLE, unique_keys=["company_master_id"])
    if change_rows:
        upsert_to_db(pd.DataFrame(change_rows), CHANGE_TABLE, unique_keys=["company_master_id", "changed_at", "kind"])
    return {"applied": len(live_rows), "changes": len(change_rows)}


# --- the pass -----------------------------------------------------------------------------------

def run_pass() -> dict[str, object]:
    from fundamentals.screens import story_score

    ensure_tables()
    now = pd.Timestamp.now(tz="UTC")
    since = {s: _watermark(s, now) for s in ("events", "alerts", "quarters", "news")}
    live = sql_to_df(f"SELECT company_master_id, primary_dimension FROM {LIVE_TABLE}")  # noqa: S608
    sectors = sql_to_df("SELECT company_master_id, sector_code FROM fundamentals_company_sector")
    rows, marks = collect_causes(since, live, sectors)
    if rows:
        upsert_to_db(pd.DataFrame(rows).drop_duplicates(["company_master_id", "cause", "cause_ref"]),
                     QUEUE_TABLE, unique_keys=["company_master_id", "cause", "cause_ref"], on_conflict="nothing")
    _set_watermarks(marks)

    queue = sql_to_df(f"SELECT company_master_id, cause FROM {QUEUE_TABLE} WHERE processed_at IS NULL")  # noqa: S608
    if queue.empty:
        return {"enqueued": len(rows), "rescored": 0, "changes": 0}
    as_of = pd.Timestamp.now(tz="Asia/Kolkata").date()
    scored = story_score.score(story_score.load_inputs(as_of))
    scored = scored[scored["company_master_id"].isin(set(queue["company_master_id"]))]
    causes = queue.groupby("company_master_id")["cause"].apply(lambda c: ",".join(sorted(set(c)))).to_dict()
    applied = apply_scores(scored, causes=causes, score_version=story_score.SCORE_VERSION)

    def _mark(_cur_ids=tuple(queue["company_master_id"].unique())) -> None:
        with db_session() as (_, cur):
            cur.execute(f"UPDATE {QUEUE_TABLE} SET processed_at = now() "  # noqa: S608
                        "WHERE processed_at IS NULL AND company_master_id = ANY(%s)", (list(_cur_ids),))

    execute_db_operation(_mark, operation_name=f"{QUEUE_TABLE}:mark_processed")
    # queued companies outside today's universe are processed (nothing to score), not stuck
    return {"enqueued": len(rows), "queued_companies": int(queue["company_master_id"].nunique()),
            "rescored": applied["applied"], "changes": applied["changes"]}


def main(argv: list[str] | None = None) -> int:
    global STOCKEY_RUN_STATE
    argparse.ArgumentParser(description="One event-driven re-evaluation pass.").parse_args(argv)
    result = run_pass()
    STOCKEY_RUN_STATE = {"source": SYNC_SOURCE_NAME, "rows": result["rescored"], "rows_written": result["rescored"],
                         **result, "fallback_used": False, "state_advanced": result["rescored"] > 0}
    print(json.dumps({"status": "ok", **STOCKEY_RUN_STATE}, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
