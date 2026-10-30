"""Revenue shares for theme members whose tags have none (operator, 2026-10-30: "if the revenue
share is unknown, then if the company has no other business assume 100% share, else look at
other resources"). docs/THEMATIC_WATCHLIST_PRD.md section 4.2.

Two passes, each per (company, theme) still without a share:
  1. sole business -- the model reads the company's profile and annual-report pages and says
     whether ALL its revenue comes from products or services inside the theme's definition,
     with a verbatim sentence that shows it (checked against the text). "sole" sets 100%.
     "other" names the other businesses and sends the company to pass 2.
  2. other sources -- the model reads the company's credit-rating rationales and its latest
     results filing (OCR'd, already collected) for a stated revenue split: a percentage, or
     the theme's revenue and the total in the same unit, each with its verbatim sentence.
     Code checks that every number is in its quote and every quote in the text, and computes
     the share itself (as the segment reader does).
What neither pass finds stays unquantified (watch-only). Results are stored per pass and
FINDER_VERSION; load_members reads them after the annual report and the tag.

Calls go through the Claude CLI like the tagger, in the night window unless --any-time.

    python -m fundamentals.themes.share_finder [--any-time] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime, timezone

import pandas as pd

from fundamentals.themes import catalog
from fundamentals.themes.segment_reader import grounded, parse_amount
from fundamentals.themes.tagger import MODEL, call_claude, quote_in
from fundamentals.trigger_study.llm_runner import in_night_window
from utils.blob_store import get_text_blob
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.fallback_telemetry import record_local_fallback_event

TABLE = "fundamentals_theme_share_extra"
FINDER_VERSION = 1
BATCH = 3
TEXT_CHARS = 24000
SOURCE_CHARS = 14000          # per document in pass 2
RATIONALES = 2

_DDL = f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
        company_master_id TEXT NOT NULL, theme_version INT NOT NULL, theme_id TEXT NOT NULL,
        method TEXT NOT NULL,              -- sole_business / other_source
        finder_version INT NOT NULL,
        status TEXT NOT NULL,              -- quantified / other_business / unclear / not_found / invalid
        exposure_pct DOUBLE PRECISION,
        quote TEXT, source_label TEXT, source_url TEXT, other_business TEXT, note TEXT,
        model TEXT, checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (company_master_id, theme_version, theme_id, method, finder_version)
    )"""


def ensure_table() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute(_DDL)

    execute_db_operation(_op, operation_name=f"{TABLE}:ensure_table")


SOLE_PROMPT = """You check Indian listed companies against an investment theme. Each item gives a company, a theme (name and definition: what a company must make or sell to count) and text about the company (its profile and pages from its latest annual report).

Decide whether ALL of the company's revenue comes from products or services inside the theme's definition.
- "sole": the text shows the company does only this -- one business, and that business is inside the theme. Give a verbatim sentence from the text that shows it (e.g. "The Company is engaged solely in the manufacture of transformers").
- "other": the company also earns revenue outside the theme (another product line, segment, trading, services, exports of other goods...). Name those other businesses briefly, and give a verbatim sentence that shows one of them.
- "unclear": the text does not say enough.
Be strict: a company "focused on" or "primarily" in the theme, or with any second product line, is "other". Copy quotes exactly from the text. Keep the item "id" unchanged."""

SOLE_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {
    "type": "object", "properties": {
        "id": {"type": "string"}, "verdict": {"type": "string", "enum": ["sole", "other", "unclear"]},
        "quote": {"type": "string"}, "other_business": {"type": "string"}},
    "required": ["id", "verdict", "quote"]}}}, "required": ["items"]}

SOURCE_PROMPT = """You find how much of an Indian listed company's revenue comes from an investment theme. Each item gives the company, the theme (name and definition) and documents: credit-rating rationales and a quarterly results filing (OCR text, tables may be garbled).

Look for a stated split of revenue: a percentage of revenue (or of sales / turnover / income from operations) that comes from the theme's products, or the revenue of the theme's products together with the total revenue for the same period and unit. A segment counts only if what it sells is inside the theme's definition.
Answer per item:
- "found": "pct" (the stated percentage) with "pct_quote" (the verbatim sentence holding it), OR "theme_revenue" and "total_revenue" (numbers exactly as printed) with "theme_quote" and "total_quote" (verbatim lines holding each), plus "period" and "document" (which document).
- "none": no such split in the documents.
Never estimate or compute a share yourself, and never use order book or capacity shares. Copy quotes exactly. Keep the item "id" unchanged."""

SOURCE_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {
    "type": "object", "properties": {
        "id": {"type": "string"}, "result": {"type": "string", "enum": ["found", "none"]},
        "pct": {"type": ["number", "null"]}, "pct_quote": {"type": ["string", "null"]},
        "theme_revenue": {"type": ["string", "null"]}, "total_revenue": {"type": ["string", "null"]},
        "theme_quote": {"type": ["string", "null"]}, "total_quote": {"type": ["string", "null"]},
        "period": {"type": ["string", "null"]}, "document": {"type": ["string", "null"]}},
    "required": ["id", "result"]}}}, "required": ["items"]}


# --- validation (pure) ----------------------------------------------------------------------

def validate_sole(x: dict, text: str) -> dict:
    v, q = x.get("verdict"), (x.get("quote") or "").strip()
    out = {"quote": q or None, "other_business": (x.get("other_business") or None)}
    if v not in ("sole", "other", "unclear"):
        return {**out, "status": "invalid", "note": f"verdict {v!r}"}
    if v != "unclear" and not (q and quote_in(q, text)):
        return {**out, "status": "unclear", "note": "quote not in the text"}
    if v == "sole":
        return {**out, "status": "quantified", "exposure_pct": 100.0}
    return {**out, "status": "other_business" if v == "other" else "unclear"}


# a theme's revenue line must name a business, not a total ("| Total | 320.96", Le Travenues) or
# the company's continuing operations (Axiscades: "Continuing operations 12,498.90")
_NOT_A_LINE = re.compile(r"^[\W\d]*(?:\(?[a-z0-9]\)\s*)?(total|continuing operations|revenue from operations|net sales)\b", re.I)


def _pct_in(pct: float, quote: str) -> bool:
    return any(abs(float(n) - pct) < 0.51 for n in re.findall(r"(\d+(?:\.\d+)?)\s*%", quote))


def validate_source(x: dict, text: str) -> dict:
    if x.get("result") != "found":
        return {"status": "not_found"}
    pct, pq = x.get("pct"), (x.get("pct_quote") or "").strip()
    label = f"{x.get('document') or 'filing'} {x.get('period') or ''}".strip()
    if isinstance(pct, (int, float)) and pq:
        if not (0 < pct <= 100 and _pct_in(float(pct), pq) and quote_in(pq, text)):
            return {"status": "invalid", "note": "percentage not in its quote, or the quote not in the documents"}
        return {"status": "quantified", "exposure_pct": float(pct), "quote": pq, "source_label": label}
    a, b = parse_amount(x.get("theme_revenue")), parse_amount(x.get("total_revenue"))
    tq, sq = (x.get("theme_quote") or "").strip(), (x.get("total_quote") or "").strip()
    if a is None or b is None or b <= 0 or not (grounded(tq, x["theme_revenue"], text)
                                                  and grounded(sq, x["total_revenue"], text)):
        return {"status": "invalid", "note": "amounts not grounded in the documents"}
    if _NOT_A_LINE.search(tq.split("  ")[0][:60]) or tq == sq:
        return {"status": "invalid", "note": "the theme's line is a total or continuing operations, not a business line"}
    share = 100.0 * a / b
    if not 0 < share <= 100:
        return {"status": "invalid", "note": f"share {share:.1f}% out of range"}
    return {"status": "quantified", "exposure_pct": round(share, 1), "quote": f"{tq} / {sq}", "source_label": label}


def focus(text: str, keys=("segment", "revenue from", "% of", "per cent", "share of", "contribut"), limit=SOURCE_CHARS) -> str:
    """The parts of a long document around revenue-split words, up to `limit` characters."""
    if len(text) <= limit:
        return text
    spans = []
    for m in re.finditer("|".join(re.escape(k) for k in keys), text, re.I):
        spans.append((max(0, m.start() - 1500), min(len(text), m.end() + 1500)))
    out, last = [], -1
    for a, b in sorted(spans):
        a = max(a, last)
        if a < b:
            out.append(text[a:b])
            last = b
        if sum(map(len, out)) >= limit:
            break
    return ("\n...\n".join(out) or text)[:limit]


# --- inputs ---------------------------------------------------------------------------------

def targets(as_of: date, version: int = 1) -> pd.DataFrame:
    from fundamentals.themes.strength import load_members

    m = load_members(as_of, version)
    return m[m["share"].isna()][["company_master_id", "theme_id"]].drop_duplicates()


def company_text(ids: list[str]) -> dict[str, str]:
    prof = sql_to_df("""SELECT DISTINCT ON (company_master_id) company_master_id, about, key_points
                          FROM fundamentals_company_profile WHERE status = 'ok' AND company_master_id = ANY(%(i)s)
                         ORDER BY company_master_id, fetched_on DESC""", params={"i": ids})
    ar = sql_to_df("""SELECT DISTINCT ON (company_master_id) company_master_id, front, mda, segment
                        FROM fundamentals_annual_report_text WHERE status = 'ok' AND company_master_id = ANY(%(i)s)
                       ORDER BY company_master_id, load_ts DESC""", params={"i": ids}).set_index("company_master_id")
    out = {}
    for r in prof.itertuples():
        parts = [r.about, r.key_points]
        if r.company_master_id in ar.index:
            a = ar.loc[r.company_master_id]
            parts.append("From the latest annual report:\n" + "\n\n".join(x for x in (a["front"], a["mda"], a["segment"])
                                                                         if isinstance(x, str) and x))
        out[r.company_master_id] = "\n".join(x for x in parts if isinstance(x, str) and x)[:TEXT_CHARS]
    return out


def source_docs(cmid: str) -> list[tuple[str, str]]:
    """(label, text) for the company's latest rating rationales and results filing."""
    ev = sql_to_df("""SELECT filing_type, rating_agency, rationale_headline, left(disclosure_date, 10) AS d, ocr_text_s3_key
                        FROM fundamentals_events WHERE company_master_id = %(c)s AND ocr_status = 'done'
                         AND ocr_text_s3_key IS NOT NULL AND filing_type IN ('rating_action', 'results')
                       ORDER BY disclosure_date DESC""", params={"c": cmid})
    docs = []
    picks = pd.concat([ev[ev["filing_type"] == "rating_action"].head(RATIONALES), ev[ev["filing_type"] == "results"].head(1)])
    for r in picks.itertuples():
        try:
            text = get_text_blob(r.ocr_text_s3_key)
        except Exception as exc:  # noqa: BLE001 -- a missing blob skips that document only
            record_local_fallback_event(module="fundamentals.themes.share_finder", fallback_type="ocr_text_missing",
                                        source="blob_store", symbol=cmid, severity="warn", reason=repr(exc)[:300])
            continue
        label = (f"{r.rating_agency} rating rationale {r.d}" if r.filing_type == "rating_action"
                 else f"results filing {r.d}")
        docs.append((label, focus(text)))
    return docs


# --- the run ---------------------------------------------------------------------------------

def _done(method: str) -> pd.DataFrame:
    return sql_to_df(f"SELECT company_master_id, theme_id, status FROM {TABLE}"  # noqa: S608
                     " WHERE method = %(m)s AND finder_version = %(v)s", params={"m": method, "v": FINDER_VERSION})


def _store(rows: list[dict]) -> None:
    if rows:
        df = pd.DataFrame(rows)
        for c in ("exposure_pct", "quote", "source_label", "source_url", "other_business", "note"):
            if c not in df:
                df[c] = None
        upsert_to_db(df.assign(finder_version=FINDER_VERSION, theme_version=1, model=MODEL,
                               checked_at=pd.Timestamp.now(tz="UTC")),
                     TABLE, ["company_master_id", "theme_version", "theme_id", "method", "finder_version"])


def run(as_of: date | None = None, limit: int | None = None, any_time: bool = False) -> dict:
    if not any_time and not in_night_window(datetime.now(timezone.utc)):
        return {"skipped": "outside the 22:00-08:00 IST window (use --any-time)"}
    ensure_table()
    as_of = as_of or pd.Timestamp.now(tz="Asia/Kolkata").date()
    themes = {t.id: t for t in catalog.load(1)}
    t = targets(as_of)
    counts = {"targets": len(t)}

    # pass 1: sole business
    done = _done("sole_business")
    todo = t.merge(done, on=["company_master_id", "theme_id"], how="left")
    todo = todo[todo["status"].isna()].head(limit or len(todo))
    texts = company_text(list(todo["company_master_id"].unique()))
    rows1 = list(todo.itertuples(index=False))
    for i in range(0, len(rows1), BATCH):
        batch = [r for r in rows1[i:i + BATCH] if texts.get(r.company_master_id)]
        if not batch:
            continue
        ask = [{"id": str(k), "company": r.company_master_id, "theme": themes[r.theme_id].name,
                "definition": themes[r.theme_id].definition, "text": texts[r.company_master_id]}
               for k, r in enumerate(batch)]
        res = call_claude(SOLE_PROMPT, SOLE_SCHEMA, ask)
        if not res["ok"]:
            if res["kind"] == "limit":
                return {**counts, "stopped": res["message"][:200]}
            continue
        got = {str(x.get("id")): x for x in res["items"]}
        _store([{"company_master_id": r.company_master_id, "theme_id": r.theme_id, "method": "sole_business",
                 **validate_sole(got.get(str(k), {}), texts[r.company_master_id]),
                 "source_label": "profile and annual report"} for k, r in enumerate(batch)])
    s1 = _done("sole_business")
    counts["sole"] = s1["status"].value_counts().to_dict()

    # pass 2: other sources, for those not settled as sole
    open_ = s1[s1["status"].isin(["other_business", "unclear", "invalid"])][["company_master_id", "theme_id"]]
    open_ = open_.merge(t, on=["company_master_id", "theme_id"])
    done2 = _done("other_source")
    open_ = open_.merge(done2, on=["company_master_id", "theme_id"], how="left")
    open_ = open_[open_["status"].isna()].head(limit or len(open_))
    rows2 = []
    for r in open_.itertuples(index=False):
        docs = source_docs(r.company_master_id)
        if not docs:
            rows2.append({"company_master_id": r.company_master_id, "theme_id": r.theme_id, "method": "other_source",
                          "status": "not_found", "note": "no rating rationale or results filing text"})
            continue
        text = "\n\n".join(f"=== {lab} ===\n{tx}" for lab, tx in docs)
        res = call_claude(SOURCE_PROMPT, SOURCE_SCHEMA, [{"id": "0", "company": r.company_master_id,
                                                          "theme": themes[r.theme_id].name,
                                                          "definition": themes[r.theme_id].definition,
                                                          "documents": text}])
        if not res["ok"]:
            if res["kind"] == "limit":
                break
            continue
        x = (res["items"] or [{}])[0]
        rows2.append({"company_master_id": r.company_master_id, "theme_id": r.theme_id, "method": "other_source",
                      **validate_source(x, text)})
    _store(rows2)
    counts["other_source"] = _done("other_source")["status"].value_counts().to_dict()
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Find revenue shares for theme members without one.")
    ap.add_argument("--any-time", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args(argv)
    print(json.dumps(run(limit=a.limit, any_time=a.any_time), default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
