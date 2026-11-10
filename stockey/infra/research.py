"""Answer a question about one company from its records (infra/records.py): the verified facts
and the passages that match the question, with every claim cited to a filing.
docs/SCALE_OUT_PRD.md section 4.1.

Two Claude calls (the tagger's CLI and model):
  1. plan   -- which fact types bear on the question, which phrases to search the full text for,
               and the date range.
  2. answer -- Claude reads those facts, the matching passages and the summaries of the records
               they come from, and answers citing record ids. The code drops any citation of a
               record it was not given and links the rest to their exchange URLs.

    python -m infra.research KAYNES "How have the order book and the board changed in 2 years?"
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pandas as pd

from fundamentals.themes.tagger import call_claude
from infra import embed as emb
from infra import schema
from infra.catalog import FACT_TYPES
from utils.db import db_session, sql_to_df

MAX_FACTS = 300
MAX_SEARCHES = 8
PER_SEARCH = 8
QUOTE_CHARS = 300
OUT_DIR = Path(__file__).resolve().parent.parent / "reports" / "research"

PLAN_PROMPT = """You plan how to answer an investor's question about one Indian listed company from a database of its stock-exchange filings. The database holds (a) facts extracted from every filing, each with one of these types:
{types}
and (b) the full text of every filing, searchable by meaning.

Return ONE item with: "fact_types" -- every type whose facts could help (be generous); "searches" -- up to 8 short phrases to search the full text for details the facts may miss (e.g. "order book as of quarter end", "reason for resignation"); "from_date" and "to_date" (YYYY-MM-DD or null) if the question names a period. Today is {today}."""

PLAN_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {
    "type": "object", "properties": {
        "fact_types": {"type": "array", "items": {"type": "string", "enum": list(FACT_TYPES)}},
        "searches": {"type": "array", "items": {"type": "string"}},
        "from_date": {"type": ["string", "null"]}, "to_date": {"type": ["string", "null"]}},
    "required": ["fact_types", "searches"]}}}, "required": ["items"]}

ANSWER_PROMPT = """You are a careful equity analyst. Answer the investor's question about one Indian listed company using ONLY what is given: records (id, date, headline, summary), facts extracted from them (each names its record), and passages of their text. Every factual sentence must end with the ids of the records that support it, like [R12] or [R3, R7]. If the material does not settle something, say so plainly; never fill gaps from memory. Write in plain, short sentences; give numbers with their dates. Structure: a 2-3 sentence summary, then sections that fit the question, then "What the filings do not say". Return the answer as markdown in "answer"."""

ANSWER_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {
    "type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}}}, "required": ["items"]}


def cite_check(answer: str, given: set[str], urls: dict[str, str]) -> tuple[str, list[str]]:
    """Unknown ids are removed (and listed); known ones are linked at the end."""
    bad = sorted({c for c in re.findall(r"\bR\d+\b", answer) if c not in given})
    for b in bad:
        answer = re.sub(rf"\b{b}\b,?\s*", "", answer)
    used = sorted({c for c in re.findall(r"\bR\d+\b", answer) if c in given}, key=lambda x: int(x[1:]))
    refs = "\n".join(f"- **{c}**: {urls[c]}" for c in used)
    return answer + ("\n\n## Sources\n" + refs if refs else ""), bad


def _date(s):
    try:
        return pd.Timestamp(s).date() if s else None
    except (ValueError, TypeError):
        return None


def facts(symbol: str, types: list[str], since, until) -> pd.DataFrame:
    return sql_to_df(f"""SELECT f.record_key, f.fact_type, coalesce(f.event_date, r.announced_at::date) AS d, f.period,
                                f.summary, f.amount_cr, f.pct, f.shares, f.price_inr, f.counterparty, f.person,
                                f.role, f.direction, f.details, f.quote
                           FROM {schema.FACT} f JOIN {schema.RECORD} r USING (record_key)
                          WHERE f.symbol = %(s)s AND f.verified AND f.fact_type = ANY(%(t)s)
                            AND (%(a)s::date IS NULL OR coalesce(f.event_date, r.announced_at::date) >= %(a)s::date)
                            AND (%(b)s::date IS NULL OR coalesce(f.event_date, r.announced_at::date) <= %(b)s::date)
                          ORDER BY d LIMIT {MAX_FACTS}""",
                     params={"s": symbol, "t": types, "a": since, "b": until})


def passages(symbol: str, searches: list[str], since, until) -> pd.DataFrame:
    if not searches:
        return pd.DataFrame(columns=["record_key", "seq", "text"])
    vecs = emb.embed(searches, query=True)
    rows = []
    with db_session() as (_, cur):
        cur.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")   # keep scanning past other companies
        for v in vecs:
            cur.execute(f"""SELECT record_key, seq, text FROM {schema.PASSAGE}
                             WHERE symbol = %s AND (%s::date IS NULL OR announced_at::date >= %s::date)
                               AND (%s::date IS NULL OR announced_at::date <= %s::date)
                             ORDER BY embedding <=> %s::vector LIMIT {PER_SEARCH}""",
                        (symbol, since, since, until, until, emb.to_pg(v)))
            rows += cur.fetchall()
    return pd.DataFrame(rows, columns=["record_key", "seq", "text"]).drop_duplicates(["record_key", "seq"])


def _present(v) -> bool:
    return v is not None and v == v


def ask(symbol: str, question: str) -> dict:
    n_records = int(sql_to_df(f"SELECT count(*) n FROM {schema.RECORD} WHERE symbol = %(s)s AND status = 'done'",
                              params={"s": symbol}).iloc[0, 0])
    if not n_records:
        return {"error": f"no extracted records for {symbol}"}
    today = pd.Timestamp.now(tz="Asia/Kolkata").date()
    plan = call_claude(PLAN_PROMPT.format(types="\n".join(f"- {k}: {what}" for k, (what, _) in FACT_TYPES.items()), today=today),
                       PLAN_SCHEMA, [{"company": symbol, "question": question}])
    if not plan["ok"]:
        return {"error": plan["message"]}
    p = plan["items"][0]
    since, until = _date(p.get("from_date")), _date(p.get("to_date"))
    fx = facts(symbol, p.get("fact_types") or list(FACT_TYPES), since, until)
    px = passages(symbol, (p.get("searches") or [])[:MAX_SEARCHES], since, until)
    keys = list(dict.fromkeys(list(fx.record_key) + list(px.record_key)))
    recs = sql_to_df(f"""SELECT record_key, announced_at, subject, summary, doc_url, sources FROM {schema.RECORD}
                          WHERE record_key = ANY(%(k)s) ORDER BY announced_at""", params={"k": keys})
    rid = {k: f"R{i + 1}" for i, k in enumerate(recs.record_key)}
    keep = ["fact_type", "period", "summary", "amount_cr", "pct", "shares", "price_inr", "counterparty", "person",
            "role", "direction", "details"]
    payload = {"company": symbol, "question": question,
               "records": [{"id": rid[r.record_key], "date": str(r.announced_at)[:10], "headline": r.subject,
                            "summary": r.summary} for r in recs.itertuples()],
               "facts": [{"record": rid[f.record_key], "date": str(f.d),
                          **{k: getattr(f, k) for k in keep if _present(getattr(f, k))},
                          "quote": (f.quote or "")[:QUOTE_CHARS]} for f in fx.itertuples()],
               "passages": [{"record": rid[x.record_key], "text": x.text} for x in px.itertuples()]}
    ans = call_claude(ANSWER_PROMPT, ANSWER_SCHEMA, [payload])
    if not ans["ok"]:
        return {"error": ans["message"]}
    urls = {rid[r.record_key]: (r.doc_url or f"{r.sources[0]['source']} filing {r.sources[0]['filing_id']}")
            for r in recs.itertuples()}
    text, bad = cite_check(ans["items"][0]["answer"], set(rid.values()), urls)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{symbol}_{pd.Timestamp.now(tz='Asia/Kolkata'):%Y%m%d_%H%M}.md"
    path.write_text(f"# {symbol}: {question}\n\n{text}\n")
    return {"records": n_records, "plan": p, "facts": len(fx), "passages": len(px), "cited_records": len(recs),
            "chars": len(json.dumps(payload, default=str)), "dropped_citations": bad, "path": str(path)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol")
    ap.add_argument("question")
    a = ap.parse_args(argv)
    print(json.dumps(ask(a.symbol, a.question), indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
