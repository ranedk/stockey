"""Every collected filing becomes a queryable record (docs/SCALE_OUT_PRD.md section 4.1):

  build    -- one record per disclosure: the NSE and BSE copies of the same document merge.
              A record whose text changed (OCR arrived) goes back to pending.
  embed    -- the full text is split into passages and each gets a vector (infra/embed.py), so
              a question can find the right page of a 300-page report.
  extract  -- Claude writes a short summary of each record and lists its facts in a fixed set of
              types (orders, preferential issues, promoter stake changes, board changes ...).
              A fact is "verified" only when its quote is in the text and its numbers are in
              the quote. Routine notices get a summary only.

    python -m infra.records build|embed|extract|all [--symbols KAYNES SOLARINDS] [--any-time]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from infra import catalog, schema, tasks
from infra import embed as emb
from utils.db import db_session, sql_to_df

VERSION = catalog.VERSION     # a catalogue change re-reads older records
CHUNK_CHARS = 1200
HEAD_CHARS = 12_000          # a long document: its opening ...
EXTRA_CHARS = 18_000         # ... plus the passages most like a fact, up to this much more
ROUTINE_CHARS = 2_500
BATCH_CHARS = 60_000
BATCH_ITEMS = 12
ROUTINE_BATCH = 25
WORKERS = 3

ROUTINE = re.compile(r"trading window|newspaper|reg\.? ?74 ?\(5\)|depositories and participants|commercial paper"
                     r"|loss of share certificate|duplicate share|transfer of shares|investor complaints", re.I)

FACT_TYPES = catalog.FACT_TYPES

PROMPT = """You read Indian stock-exchange filings for one listed company and turn each into a record an analyst can query later. Each item has id, date, the exchange's category, headline, text, and "partial" (true when only the first pages of a scanned document were read).

The exchange's category is often WRONG. Decide what the filing is from its text.

1. "kind": one of these filing kinds (with what its summary must say):
{kinds}
2. "summary": 2-4 plain sentences saying what the filing discloses, as its kind asks, with the key numbers and dates. Routine kinds: one sentence.
3. "facts": every material fact, each with one "type" from this list (with the "details" fields to fill when stated):
{types}

For each fact fill what the filing states and leave the rest null:
- "summary": one sentence stating the fact.
- "event_date" (YYYY-MM-DD: when it happened or takes effect; null if not stated), "period" (e.g. "Q2 FY26", "as of 2025-09-30").
- "amount_value" EXACTLY as printed (e.g. "1,250.5"), "amount_unit" ("crore", "lakh", "million", "billion", "thousand" or "" if written in full), "currency" (INR, USD, EUR ...).
- "pct", "shares", "price_inr" as plain numbers when stated.
- "counterparty", "person" and "role", "direction" (increase / decrease / new / exit) as the type's note says.
- "details": the type's detail fields that the filing states (short strings); anything else useful may be added.
- "quote": the sentence from the text that states the fact, copied verbatim. Every number you give must appear in this quote.
4. "read_more" (only when "partial" is true): true if the remaining pages probably hold material facts (results tables, order or allotment details, a presentation, an agreement); false if the first pages already show it is routine or complete (a one-page letter, a newspaper copy, a certificate).

Rules: use only the item's text, never memory. One fact per order, person or allotment; if a total is given, add it as its own fact. A repeat of something already announced is still a fact (details.repeat = "yes"). Routine kinds have no facts. Keep each "id" unchanged."""

SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {
    "type": "object", "properties": {
        "id": {"type": "string"}, "kind": {"type": "string", "enum": list(catalog.KINDS)},
        "summary": {"type": "string"}, "read_more": {"type": ["boolean", "null"]},
        "facts": {"type": "array", "items": {"type": "object", "properties": {
            "type": {"type": "string", "enum": list(FACT_TYPES)}, "summary": {"type": "string"},
            "event_date": {"type": ["string", "null"]}, "period": {"type": ["string", "null"]},
            "amount_value": {"type": ["string", "null"]}, "amount_unit": {"type": ["string", "null"]},
            "currency": {"type": ["string", "null"]}, "pct": {"type": ["number", "null"]},
            "shares": {"type": ["number", "null"]}, "price_inr": {"type": ["number", "null"]},
            "counterparty": {"type": ["string", "null"]}, "person": {"type": ["string", "null"]},
            "role": {"type": ["string", "null"]}, "direction": {"type": ["string", "null"]},
            "details": {"type": ["object", "null"], "additionalProperties": {"type": "string"}},
            "quote": {"type": "string"}},
            "required": ["type", "summary", "quote"]}}},
    "required": ["id", "kind", "summary", "facts"]}}}, "required": ["items"]}

# what "material" looks like, for picking a long document's passages
FACT_PROBES = [
    "order received from customer worth crore", "order book stands at", "allotment of equity shares on preferential basis",
    "promoter shareholding acquisition of shares", "appointment of director with effect from",
    "resignation of independent director", "acquisition of stake in company for consideration",
    "revenue and profit for the quarter", "board approved capacity expansion capex", "credit rating reaffirmed",
]


# --- build ---------------------------------------------------------------------------------------

def _clean(s: str | None) -> str:
    return re.sub(r"[ \t]+", " ", s or "").strip()


def _doc_text(r) -> str:
    ocr = r.ocr_text if isinstance(r.ocr_text, str) else ""
    layer = r.text_layer if isinstance(r.text_layer, str) else ""
    return _clean(ocr if ocr.strip() else layer)


def record_key(r) -> str:
    """Same document on both exchanges -> same key. The text layer never changes after the fetch,
    so the key survives OCR arriving later; a scan (no text layer) keys on its size and pages."""
    layer = re.sub(r"\s+", "", (r.text_layer if isinstance(r.text_layer, str) else "").lower())
    if len(layer) >= 200:
        basis = f"{r.symbol}|t|{hashlib.md5(layer.encode()).hexdigest()}"
    elif isinstance(r.bytes, (int, float)) and r.bytes == r.bytes and r.bytes > 0:
        basis = f"{r.symbol}|b|{int(r.bytes)}|{r.pages}"
    else:
        basis = f"{r.symbol}|f|{r.source}|{r.filing_id}"
    return hashlib.sha1(basis.encode()).hexdigest()[:20]


def _filings(symbol: str) -> pd.DataFrame:
    return sql_to_df(f"""SELECT a.source, a.filing_id, a.symbol, a.announced_at, a.category, a.subject, a.text,
                                a.attachment_url, d.text_layer, d.ocr_text, d.bytes, d.pages,
                                d.ocr_status, d.blob_key
                           FROM {schema.ANNOUNCEMENT} a LEFT JOIN {schema.DOCUMENT} d ON d.url = a.attachment_url
                          WHERE a.symbol = %(s)s ORDER BY a.announced_at""", params={"s": symbol})


def group(df: pd.DataFrame) -> list[dict]:
    """Records from one company's filings, each with its full text."""
    if df.empty:
        return []
    df = df.assign(key=[record_key(r) for r in df.itertuples()])
    out = []
    for key, g in df.groupby("key", sort=False):
        g = g.sort_values("announced_at")
        bse = g[g.source == "bse"]
        head = (bse.iloc[0] if len(bse) else g.iloc[0])
        notes = list(dict.fromkeys(_clean(t) for t in g.text if isinstance(t, str) and t.strip()))
        doc = max((_doc_text(r) for r in g.itertuples()), key=len)
        subject = head.subject if head.source == "bse" else (notes[0][:300] if notes else head.subject)
        text = "\n".join(notes + ([doc] if doc else []))
        cat = " / ".join(dict.fromkeys(c for c in g.category if isinstance(c, str)))
        probe = next((r for r in g.itertuples() if getattr(r, "ocr_status", None) == "probe_done"), None)
        out.append({"record_key": key, "symbol": head.symbol, "announced_at": g.announced_at.min(),
                    "category": cat or None, "subject": subject,
                    "sources": [{"source": r.source, "filing_id": r.filing_id, "url": r.attachment_url}
                                for r in g.itertuples()],
                    "doc_url": next((u for u in g.attachment_url if isinstance(u, str) and u.startswith("http")), None),
                    "routine": bool(ROUTINE.search(f"{cat} {subject or ''}")),
                    "text": text, "text_sha": hashlib.sha1(text.encode()).hexdigest(), "text_chars": len(text),
                    # a scan of which only the first pages were OCR'd: Claude says whether to read the rest
                    "partial": None if probe is None else {"url": probe.attachment_url, "blob_key": probe.blob_key,
                                                           "pages": int(probe.pages or 0)}})
    return out


def build(symbols: list[str]) -> dict:
    schema.ensure()
    n = 0
    with db_session() as (_, cur):
        for sym in symbols:
            for r in group(_filings(sym)):
                cur.execute(f"""INSERT INTO {schema.RECORD} AS x (record_key, symbol, announced_at, category, subject, sources,
                                       doc_url, routine, text_sha, text_chars)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                ON CONFLICT (record_key) DO UPDATE SET
                                  announced_at = EXCLUDED.announced_at, category = EXCLUDED.category,
                                  subject = EXCLUDED.subject, sources = EXCLUDED.sources, doc_url = EXCLUDED.doc_url,
                                  routine = CASE WHEN x.kind IS NULL THEN EXCLUDED.routine ELSE x.routine END,
                                  text_sha = EXCLUDED.text_sha,
                                  text_chars = EXCLUDED.text_chars,
                                  status = CASE WHEN x.text_sha IS DISTINCT FROM EXCLUDED.text_sha
                                                THEN 'pending' ELSE x.status END,
                                  updated_at = now()""",
                            (r["record_key"], r["symbol"], r["announced_at"], r["category"], r["subject"],
                             json.dumps(r["sources"]), r["doc_url"], r["routine"], r["text_sha"], r["text_chars"]))
                n += 1
    return {"records": n}


def _records(symbol: str) -> dict[str, dict]:
    return {r["record_key"]: r for r in group(_filings(symbol))}


# --- passages ------------------------------------------------------------------------------------

def chunks(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """Pack lines into passages of about `size` characters; a longer line is cut."""
    out, cur = [], ""
    for line in text.splitlines():
        line = line.strip()
        while len(line) > size:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:size])
            line = line[size:]
        if not line:
            continue
        if len(cur) + len(line) + 1 > size and cur:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


def embed(symbols: list[str]) -> dict:
    from psycopg2.extras import execute_values
    n_rec = n_pass = 0
    for sym in symbols:
        todo = sql_to_df(f"""SELECT record_key FROM {schema.RECORD}
                              WHERE symbol = %(s)s AND embedded_sha IS DISTINCT FROM text_sha""", params={"s": sym})
        if todo.empty:
            continue
        recs = _records(sym)
        for key in todo.record_key:
            r = recs.get(key)
            if r is None:
                continue
            parts = chunks(r["text"])
            vecs = emb.embed([f"{(r['subject'] or '')[:150]}\n{p}" for p in parts]) if parts else []
            with db_session() as (_, cur):
                cur.execute(f"DELETE FROM {schema.PASSAGE} WHERE record_key = %s", (key,))
                if parts:
                    execute_values(cur, f"""INSERT INTO {schema.PASSAGE} (record_key, seq, symbol, announced_at, text, embedding)
                                            VALUES %s""",
                                   [(key, i, sym, r["announced_at"], p, emb.to_pg(v))
                                    for i, (p, v) in enumerate(zip(parts, vecs))],
                                   template="(%s, %s, %s, %s, %s, %s::vector)", page_size=500)
                cur.execute(f"UPDATE {schema.RECORD} SET embedded_sha = %s WHERE record_key = %s", (r["text_sha"], key))
            n_rec += 1
            n_pass += len(parts)
    return {"records": n_rec, "passages": n_pass}


def _vectors(key: str) -> np.ndarray:
    df = sql_to_df(f"SELECT seq, embedding::text AS e FROM {schema.PASSAGE} WHERE record_key = %(k)s ORDER BY seq",
                   params={"k": key})
    return np.array([json.loads(e) for e in df.e], dtype=np.float32) if len(df) else np.zeros((0, emb.DIM))


def reading(r: dict, probes: np.ndarray | None) -> tuple[str, bool]:
    """What Claude reads for one record, and whether it was cut. A long document: its opening,
    then the passages closest to a material fact, in document order."""
    text = r["text"]
    cap = ROUTINE_CHARS if r["routine"] else HEAD_CHARS + EXTRA_CHARS
    if len(text) <= cap:
        return text, False
    if r["routine"] or probes is None:
        return text[:cap], True
    head = text[:HEAD_CHARS]
    parts = chunks(text)
    vecs = _vectors(r["record_key"])
    if len(vecs) != len(parts):               # not embedded yet (or stale): the opening only
        return text[:cap], True
    score = (vecs @ probes.T).max(axis=1)
    pos, taken, used = 0, [], 0
    for i, p in enumerate(parts):              # skip the passages already inside the opening
        pos += len(p) + 1
        if pos <= HEAD_CHARS:
            score[i] = -1
    for i in np.argsort(-score):
        if score[i] < 0 or used + len(parts[i]) > EXTRA_CHARS:
            continue
        taken.append(i)
        used += len(parts[i])
    return head + "".join(f"\n[...]\n{parts[i]}" for i in sorted(taken)), True


# --- extraction ----------------------------------------------------------------------------------

_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(s: str) -> list[float]:
    out = []
    for m in _NUM.findall(s or ""):
        try:
            out.append(float(m.replace(",", "")))
        except ValueError:
            pass
    return out


def _num_in(x: float | None, quote: str) -> bool:
    return x is None or any(abs(n - x) <= max(0.005 * abs(x), 0.01) for n in _numbers(quote))


def _date(s: str | None):
    try:
        return pd.Timestamp(s).date() if s else None
    except (ValueError, TypeError):
        return None


def validate(f: dict, text: str, fx: dict) -> dict:
    """The fact as stored. verified = the quote is in the text and every number is in the quote."""
    from fundamentals.themes.tagger import quote_in
    from fundamentals.trigger_study.orders import to_crore
    q = (f.get("quote") or "").strip()
    notes = []
    if not q or not quote_in(q, text):
        notes.append("quote not in the text")
    raw = f.get("amount_value")
    amount = None
    if raw:
        digits = re.sub(r"[^0-9.]", "", raw).rstrip(".")
        if not digits or digits not in re.sub(r"[^0-9.]", "", q):
            notes.append("amount not in the quote")
        amount = to_crore(raw, f.get("amount_unit"), f.get("currency"), fx)
        if amount is None:
            notes.append(f"amount not converted ({f.get('amount_unit')!r}, {f.get('currency')!r})")
    for k in ("pct", "shares", "price_inr"):
        if not _num_in(f.get(k), q):
            notes.append(f"{k} not in the quote")
    bad = [n for n in notes if not n.startswith("amount not converted")]
    return {"fact_type": f["type"], "event_date": _date(f.get("event_date")), "period": f.get("period"),
            "summary": f.get("summary"), "amount_cr": amount, "amount_raw": raw, "amount_unit": f.get("amount_unit"),
            "currency": f.get("currency"), "pct": f.get("pct"), "shares": f.get("shares"),
            "price_inr": f.get("price_inr"), "counterparty": f.get("counterparty"), "person": f.get("person"),
            "role": f.get("role"), "direction": f.get("direction"), "details": f.get("details") or None,
            "quote": q, "verified": not bad, "note": "; ".join(notes) or None}


def batches(items: list[dict]) -> list[list[dict]]:
    out, cur, size = [], [], 0
    for it in items:
        cap = ROUTINE_BATCH if it["routine"] else BATCH_ITEMS
        if cur and (size + len(it["text"]) > BATCH_CHARS or len(cur) >= cap or cur[0]["routine"] != it["routine"]):
            out.append(cur)
            cur, size = [], 0
        cur.append(it)
        size += len(it["text"])
    if cur:
        out.append(cur)
    return out


_FACT_COLS = ["record_key", "symbol", "fact_type", "event_date", "period", "summary", "amount_cr", "amount_raw",
              "amount_unit", "currency", "pct", "shares", "price_inr", "counterparty", "person", "role", "direction",
              "details", "quote", "verified", "note", "version"]


def _store(rec: dict, out: dict, fx: dict, read_chars: int, truncated: bool) -> tuple[int, bool]:
    """Save one record's summary and facts. Returns (facts, promoted): promoted when the record
    was read as routine (from the exchange's category) but its content says otherwise, so it
    must be read again in full."""
    kind = out.get("kind") if out.get("kind") in catalog.KINDS else "other"
    routine = kind in catalog.ROUTINE_KINDS
    promoted = rec["routine"] and not routine and truncated
    facts = [] if routine else [validate(f, rec["text"], fx) for f in out.get("facts") or []]
    with db_session() as (_, cur):
        cur.execute(f"DELETE FROM {schema.FACT} WHERE record_key = %s", (rec["record_key"],))
        for f in facts:
            row = {**f, "record_key": rec["record_key"], "symbol": rec["symbol"], "version": VERSION,
                   "details": json.dumps(f["details"]) if f["details"] else None}
            cur.execute(f"INSERT INTO {schema.FACT} ({', '.join(_FACT_COLS)}) VALUES ({', '.join(['%s'] * len(_FACT_COLS))})",
                        [row[c] for c in _FACT_COLS])
        cur.execute(f"""UPDATE {schema.RECORD} SET summary = %s, kind = %s, routine = %s,
                               status = %s, version = %s, chars_read = %s,
                               truncated = %s, extracted_at = now(), error = NULL, updated_at = now()
                         WHERE record_key = %s""",
                    (out.get("summary"), kind, routine, "pending" if promoted else "done", VERSION, read_chars,
                     truncated, rec["record_key"]))
        part = rec.get("partial")
        if part:
            more = bool(out.get("read_more")) and part["pages"] > tasks.OCR_PROBE_PAGES
            if more:
                tasks.enqueue([tasks.ocr_rest(part["url"], part["blob_key"])], cur)
            cur.execute(f"UPDATE {schema.DOCUMENT} SET ocr_status = %s, updated_at = now() WHERE url = %s",
                        ("more_queued" if more else "rest_skipped", part["url"]))
    return len(facts), promoted


def _fail(keys: list[str], why: str) -> None:
    with db_session() as (_, cur):
        cur.execute(f"UPDATE {schema.RECORD} SET status = 'failed', error = %s, updated_at = now() WHERE record_key = ANY(%s)",
                    (why[:500], keys))


def extract(symbols: list[str], limit: int | None = None, any_time: bool = False) -> dict:
    from fundamentals.trigger_study.llm_runner import in_night_window
    if not any_time and not in_night_window(datetime.now(timezone.utc)):
        return {"skipped": "outside the 22:00-08:00 IST window (use --any-time)"}
    prompt = PROMPT.format(kinds=catalog.prompt_kinds(), types=catalog.prompt_facts())
    probes = emb.embed(FACT_PROBES, query=True)
    counts = {"records": 0, "facts": 0, "failed": 0, "calls": 0, "promoted": 0, "ocr_more": 0}
    for sym in symbols:
        todo = sql_to_df(f"""SELECT record_key, routine FROM {schema.RECORD} WHERE symbol = %(s)s
                               AND (status IN ('pending', 'failed') OR version IS DISTINCT FROM %(v)s)
                             ORDER BY announced_at""", params={"s": sym, "v": VERSION})
        if limit:
            todo = todo.head(limit)
        if todo.empty:
            continue
        recs = _records(sym)
        routine = dict(zip(todo.record_key, todo.routine))
        keys = [k for k in todo.record_key if k in recs]
        for _ in range(2):                       # the second pass re-reads records promoted out of routine
            if not keys:
                break
            keys = _extract_pass(prompt, [{**recs[k], "routine": bool(routine[k])} for k in keys], probes, counts)
            for k in keys:
                routine[k] = False
            counts["promoted"] += len(keys)
        if counts.get("stopped"):
            break
    return counts


def _extract_pass(prompt: str, recs: list[dict], probes: np.ndarray, counts: dict) -> list[str]:
    from fundamentals.themes.tagger import call_claude
    from fundamentals.trigger_study.orders import _fx
    items = []
    for r in recs:
        text, cut = reading(r, probes)
        items.append({**r, "text_read": text, "truncated": cut})
    fx = _fx(sorted({r["announced_at"].date() for r in items if r["announced_at"] is not None}))

    def run(batch: list[dict]) -> tuple[list[dict], dict]:
        payload = [{"id": f"R{i + 1}", "date": str(r["announced_at"])[:10], "category": r["category"],
                    "headline": r["subject"], "partial": bool(r.get("partial")), "text": r["text_read"]}
                   for i, r in enumerate(batch)]
        return batch, call_claude(prompt, SCHEMA, payload)

    sized = [{**it, "text": it["text_read"]} for it in items]          # batches() sizes by what is read
    full = {it["record_key"]: it["text"] for it in items}
    promoted = []
    with ThreadPoolExecutor(WORKERS) as pool:
        for batch, res in pool.map(run, batches(sorted(sized, key=lambda x: x["routine"]))):
            counts["calls"] += 1
            keys = [r["record_key"] for r in batch]
            if not res["ok"]:
                _fail(keys, f"{res['kind']}: {res['message']}")
                counts["failed"] += len(keys)
                if res["kind"] == "limit":
                    counts["stopped"] = res["message"][:200]
                continue
            got = {o.get("id"): o for o in res["items"]}
            for i, r in enumerate(batch):
                out = got.get(f"R{i + 1}")
                if out is None:
                    _fail([r["record_key"]], "missing from the answer")
                    counts["failed"] += 1
                    continue
                rec = {**r, "text": full[r["record_key"]]}
                d = r["announced_at"].date() if r["announced_at"] is not None else None
                n, up = _store(rec, out, fx.get(d, {}), len(r["text_read"]), r["truncated"])
                counts["facts"] += n
                counts["records"] += 1
                counts["ocr_more"] += bool(r.get("partial") and out.get("read_more"))
                if up:
                    promoted.append(r["record_key"])
    return promoted


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["build", "embed", "extract", "all"])
    ap.add_argument("--symbols", nargs="+", required=True)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--any-time", action="store_true")
    a = ap.parse_args(argv)
    out = {}
    if a.step in ("build", "all"):
        out["build"] = build(a.symbols)
    if a.step in ("embed", "all"):
        out["embed"] = embed(a.symbols)
    if a.step in ("extract", "all"):
        out["extract"] = extract(a.symbols, a.limit, a.any_time)
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
