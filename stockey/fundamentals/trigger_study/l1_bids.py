"""Lowest-bidder (L1) announcements for LEDGER row 89 (systrade/research/preregistrations/
2026-11-07_l1_bids.md -- read it before changing anything: a change to the rule is a new test).

Row 84's order prefilter needed "order" / "contract" style words, so most "declared L1 for ..."
filings were never kept. Step 1 refetches every universe company's NSE announcement list with
the L1 prefilter into its own tables (row 84's stay untouched); step 2 classifies outcome-blind
with row 84's value checks. No return is computed here.

    python -m fundamentals.trigger_study.l1_bids fetch [--limit N]
    python -m fundamentals.trigger_study.l1_bids classify [--limit N] [--any-time]
    python -m fundamentals.trigger_study.l1_bids attachments [--limit N]
    python -m fundamentals.trigger_study.l1_bids classify-attachments [--limit N] [--any-time]
"""
from __future__ import annotations

import argparse
import json
import re

import pandas as pd

from fundamentals.trigger_study import orders
from utils.db import db_session, sql_to_df, upsert_to_db

CANDIDATE_TABLE = "fundamentals_l1_candidate"
FETCH_TABLE = "fundamentals_l1_fetch"
ATTACH_TABLE = "fundamentals_l1_attachment"
CLASSIFIED_TABLE = "fundamentals_l1_classified"
CLASSIFY_VERSION = 1
CLASSIFY_BATCH = 15

# the pre-registration's L1 prefilter
_L1_RX = re.compile(r"\bL\s?-?\s?1\b|\blowest\s+(bid|bidder|tender|quote|offer|price)s?\b|\blowest\s+evaluated\b", re.I)


def is_l1_candidate(category: str | None, text: str | None) -> bool:
    t = (text or "").strip()
    if orders._BOILERPLATE.match(t):
        return False
    return bool(_L1_RX.search(f"{category or ''} {t}"))


def fetch(limit: int | None = None) -> dict:
    return orders.fetch(limit=limit, keep=is_l1_candidate, cand_table=CANDIDATE_TABLE, fetch_table=FETCH_TABLE)


CLASSIFY_PROMPT = """You read short Indian stock-exchange announcements. For each, decide whether it reports that the listed company ITSELF (or a joint venture it leads) has been DECLARED L1 / the LOWEST BIDDER for a NEW contract or project that has NOT yet been awarded to it.

Answer "l1": false for: an actual award, letter of award / acceptance or work order already received (that is an order win, not L1); a repeat mention or clarification of an L1 status already announced (say "repeat"); a bid that was only submitted or a tender it is merely participating in; a subsidiary's L1 status unless the text says it is the group's or consolidated; "L1" used in another sense (a level, a regulation, a product name).
When it is an L1 announcement, copy the contract's stated value EXACTLY as printed ("value") with its unit ("crore", "lakh", "million", "billion", "thousand" or "" if the number is written in full), the currency ("INR", "USD", "EUR", "GBP", "AED", "JPY" or other code) and the sentence that states it ("quote", verbatim). If a joint venture's share is stated, use the company's share; if several bids are listed with a total, use the total. If no value is stated, leave value empty. Keep the "id" unchanged."""

CLASSIFY_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {
    "type": "object", "properties": {
        "id": {"type": "string"}, "l1": {"type": "boolean"}, "repeat": {"type": "boolean"},
        "value": {"type": ["string", "null"]}, "unit": {"type": ["string", "null"]},
        "currency": {"type": ["string", "null"]}, "quote": {"type": ["string", "null"]}},
    "required": ["id", "l1"]}}}, "required": ["items"]}


def validate(x: dict, text: str, fx: dict[str, float]) -> dict:
    """Row 84's checks, with "l1" in place of "order_win"."""
    v = orders.validate_classified({**x, "order_win": x.get("l1")}, text, fx)
    return {"l1": v.pop("order_win"), **v}


def classify(limit: int | None = None, any_time: bool = False) -> dict:
    from datetime import datetime, timezone

    from fundamentals.themes.tagger import call_claude
    from fundamentals.trigger_study.llm_runner import in_night_window
    if not any_time and not in_night_window(datetime.now(timezone.utc)):
        return {"skipped": "outside the 22:00-08:00 IST window (use --any-time)"}
    with db_session() as (_, cur):
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {CLASSIFIED_TABLE} (
                filing_id TEXT NOT NULL, classify_version INT NOT NULL, symbol TEXT NOT NULL,
                announced_at TIMESTAMP NOT NULL, l1 BOOLEAN NOT NULL, repeat BOOLEAN,
                value_raw TEXT, unit TEXT, currency TEXT, quote TEXT, value_cr DOUBLE PRECISION,
                value_note TEXT, value_source TEXT, classified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (filing_id, classify_version))""")
    todo = sql_to_df(f"""SELECT c.filing_id, c.symbol, c.announced_at, c.nse_category, c.text
                           FROM {CANDIDATE_TABLE} c
                          WHERE NOT EXISTS (SELECT 1 FROM {CLASSIFIED_TABLE} k
                                             WHERE k.filing_id = c.filing_id AND k.classify_version = %(v)s)
                          ORDER BY c.filing_id""", params={"v": CLASSIFY_VERSION})  # noqa: S608
    if todo.empty:
        return {"todo": 0}
    todo = todo.head(limit) if limit else todo
    fx = orders._fx(sorted(set(pd.to_datetime(todo["announced_at"]).dt.normalize())))
    counts = {"todo": len(todo), "done": 0, "l1": 0, "with_value": 0, "failed_calls": 0}
    rows = list(todo.itertuples(index=False))
    for i in range(0, len(rows), CLASSIFY_BATCH):
        batch = rows[i:i + CLASSIFY_BATCH]
        res = call_claude(CLASSIFY_PROMPT, CLASSIFY_SCHEMA,
                          [{"id": str(k), "category": r.nse_category, "text": (r.text or "")[:3000]} for k, r in enumerate(batch)])
        if not res["ok"]:
            counts["failed_calls"] += 1
            if res["kind"] == "limit":
                counts["stopped"] = res["message"][:200]
                break
            continue
        got = {str(x.get("id")): x for x in res["items"]}
        out = []
        for k, r in enumerate(batch):
            if str(k) not in got:
                continue
            v = validate(got[str(k)], r.text or "", fx.get(pd.Timestamp(r.announced_at).normalize(), {}))
            out.append({"filing_id": r.filing_id, "classify_version": CLASSIFY_VERSION, "symbol": r.symbol,
                        "announced_at": r.announced_at, **v, "value_source": "summary",
                        "classified_at": pd.Timestamp.now(tz="UTC")})
            counts["l1"] += v["l1"]
            counts["with_value"] += v["value_cr"] is not None
        if out:
            upsert_to_db(pd.DataFrame(out), CLASSIFIED_TABLE, ["filing_id", "classify_version"])
        counts["done"] += len(out)
    return counts


# Pre-registration change log 2026-11-08: the summary text states a value for only 30% (2018-22)
# and 15% (2023-25) of L1 announcements, under the pre-registered 70%, so the attached PDF is read
# for every L1 announcement without a value -- row 84's rule and code, before any return.
_TABLES = {"classified_table": CLASSIFIED_TABLE, "cand_table": CANDIDATE_TABLE, "attach_table": ATTACH_TABLE, "flag": "l1"}


def attachments(limit: int | None = None) -> dict:
    return orders.attachments(limit=limit, **_TABLES)


def classify_attachments(limit: int | None = None, any_time: bool = False) -> dict:
    return orders.classify_attachments(limit=limit, any_time=any_time, prompt=CLASSIFY_PROMPT,
                                       schema=CLASSIFY_SCHEMA, validate=validate, **_TABLES)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LEDGER row 89: lowest-bidder announcements.")
    ap.add_argument("step", choices=["fetch", "classify", "attachments", "classify-attachments"])
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--any-time", action="store_true")
    a = ap.parse_args(argv)
    out = {"fetch": lambda: fetch(limit=a.limit), "classify": lambda: classify(limit=a.limit, any_time=a.any_time),
           "attachments": lambda: attachments(limit=a.limit),
           "classify-attachments": lambda: classify_attachments(limit=a.limit, any_time=a.any_time)}[a.step]()
    print(json.dumps(out, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
