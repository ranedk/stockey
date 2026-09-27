"""One-time catch-up admission for companies new to the universe (operator, 2026-09-26).

The universe rebuild (docs/UNIVERSE_PRD.md, l1_universe version 2) added ~1,100 companies.
Their filings from before we covered them were loaded by the history backfill and marked
rule_trigger_status='historical' so they would not fire as news. Some of those companies
had a real signal in the last CATCHUP_WINDOW_DAYS that may still be live. This pass gives
exactly those filings one run through the ordinary chain -- rule triggers, LLM triage,
watchlist, confluence, then the portfolio ruleset + adjudicator -- judged as of TODAY at
today's price. Nothing here decides an entry: the same rules and adjudicator do, and
portfolio_runner tags the resulting positions CATCHUP_COHORT, caps the cohort, and skips
names whose price already ran (see portfolio_runner.CATCHUP_*).

Waits for OCR: most signals exist only once a filing has been read and extracted, and the
rule step skips unread filings, so running early would silently miss them. The default
run reports readiness; --apply refuses while any selected filing is still unread.

    python -m fundamentals.screens.catchup_admission            # readiness report
    python -m fundamentals.screens.catchup_admission --apply    # tag, then run the chain
"""

from __future__ import annotations

import argparse
import json

import pandas as pd

from fundamentals.screens.portfolio_runner import CATCHUP_COHORT, _ensure_cohort_columns
from utils.company_master import map_company_master_ids_nse_or_bse
from utils.db import db_session, sql_to_df

CATCHUP_WINDOW_DAYS = 60
# Companies in any version-1 universe run in this window were already covered: their
# filings were judged as news at the time. Only names new with version 2 are caught up.
V1_LOOKBACK_DAYS = 120
CHAIN = ("fundamentals.collectors.structured_extraction", "fundamentals.screens.l3_triggers",
         "fundamentals.screens.llm_triage", "fundamentals.screens.watchlist",
         "fundamentals.screens.confluence_score")


def new_company_ids() -> set[str]:
    df = sql_to_df(
        """
        SELECT DISTINCT ticker, query_version FROM fundamentals_l1_universe
         WHERE (query_version = 2 AND run_date = (SELECT max(run_date) FROM fundamentals_l1_universe WHERE query_version = 2))
            OR (query_version = 1 AND run_date >= now() - make_interval(days => %s))
        """,
        params=(V1_LOOKBACK_DAYS,),
    )
    if df.empty:
        return set()
    df["cmid"] = map_company_master_ids_nse_or_bse(df["ticker"].astype("string"))
    v2 = set(df.loc[df["query_version"] == 2, "cmid"].dropna())
    v1 = set(df.loc[df["query_version"] == 1, "cmid"].dropna())
    return v2 - v1


def load_selection() -> pd.DataFrame:
    df = sql_to_df(
        """
        SELECT source, news_id, company_master_id, filing_type, disclosure_date, ocr_status,
               structured_extraction_status,
               (attachment_name IS NOT NULL OR rationale_pdf_url IS NOT NULL) AS has_document
          FROM fundamentals_events
         WHERE rule_trigger_status = 'historical'
           AND disclosure_date IS NOT NULL
           AND disclosure_date::date >= current_date - %s
        """,
        params=(CATCHUP_WINDOW_DAYS,),
    )
    if df.empty:
        return df
    return df[df["company_master_id"].isin(new_company_ids())].reset_index(drop=True)


def unread(selection: pd.DataFrame) -> pd.DataFrame:
    """Filings with a document that OCR has not finished (done, failed and no_document are finished)."""
    if selection.empty:
        return selection
    pending = selection["ocr_status"].isna() | selection["ocr_status"].eq("pending")
    return selection[selection["has_document"].astype(bool) & pending]


def readiness(selection: pd.DataFrame) -> dict[str, object]:
    waiting = unread(selection)
    return {
        "cohort": CATCHUP_COHORT,
        "window_days": CATCHUP_WINDOW_DAYS,
        "filings": int(len(selection)),
        "companies": int(selection["company_master_id"].nunique()) if not selection.empty else 0,
        "by_filing_type": selection["filing_type"].value_counts().to_dict() if not selection.empty else {},
        "waiting_for_ocr": int(len(waiting)),
        "ready": bool(len(selection)) and waiting.empty,
    }


def tag_selection(selection: pd.DataFrame) -> int:
    """Tag the filings with the cohort and hand them back to the rule step (status NULL).
    Only rows still 'historical' are touched, so a re-run cannot re-open anything."""
    _ensure_cohort_columns()
    tagged = {"n": 0}
    with db_session() as (_conn, cur):
        cur.execute(
            "UPDATE fundamentals_events SET admission_cohort = %s, rule_trigger_status = NULL "
            " WHERE (source, news_id) IN (SELECT * FROM unnest(%s::text[], %s::text[])) "
            "   AND rule_trigger_status = 'historical'",
            (CATCHUP_COHORT, selection["source"].tolist(), selection["news_id"].astype(str).tolist()),
        )
        tagged["n"] = cur.rowcount
    return tagged["n"]


def run_chain() -> list[dict]:
    from fundamentals.run_pipeline import run_step

    results = [run_step(module) for module in CHAIN]
    from fundamentals.screens.portfolio_runner import run_portfolio

    # Record-only, exactly like the nightly job (which never passes --live).
    results.append({"module": "portfolio_runner", "result": run_portfolio(live=False)})
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One-time catch-up admission (see module docstring).")
    parser.add_argument("--apply", action="store_true", help="tag the filings and run the chain")
    parser.add_argument("--force", action="store_true", help="apply even if some filings are unread (they are missed)")
    args = parser.parse_args(argv)
    selection = load_selection()
    report = readiness(selection)
    print(json.dumps(report, default=str), flush=True)
    if not args.apply:
        return 0
    if not report["ready"] and not args.force:
        print("not ready: OCR has not finished every selected filing; re-run later or pass --force", flush=True)
        return 1
    tagged = tag_selection(selection)
    print(json.dumps({"tagged": tagged}), flush=True)
    results = run_chain()
    print(json.dumps({"chain": results}, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
