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

from infra import embed as emb
from infra import schema
from utils.db import db_session, sql_to_df

VERSION = 1
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

FACT_TYPES = {
    "order_won": "a new order, contract, LoA/LoI or work order received (counterparty = customer)",
    "order_book": "the order book / unexecuted orders stated as of a date (period = as-of date)",
    "l1_bid": "declared lowest bidder (L1) in a tender, not yet awarded",
    "results": "reported revenue / profit for a period (amount = revenue; details: profit, ebitda, margin)",
    "guidance": "management's forecast of revenue, margin, order inflow or capex",
    "preferential_issue": "shares allotted or proposed on a preferential basis (counterparty = allottee; price_inr, shares)",
    "warrants": "convertible warrants issued, allotted or converted",
    "public_issue": "QIP, IPO, FPO, OFS or rights issue",
    "debt_raise": "loans, NCDs, commercial paper or other borrowing raised or repaid",
    "esop": "employee stock options granted, vested or allotted",
    "buyback": "share buyback",
    "dividend": "dividend declared or recommended (amount = per share in Rs, put it in details too)",
    "bonus_split": "bonus issue or stock split",
    "promoter_stake": "promoter / founder holding change: buy, sell, transfer, creeping acquisition (pct = new holding)",
    "pledge": "promoter shares pledged, released or invoked",
    "insider_trade": "director, KMP or other insider buying or selling shares",
    "major_holder": "a non-promoter crossing or changing a disclosed holding (SAST 29, FII, fund)",
    "investment": "money put into a subsidiary, associate or other company, or a strategic stake taken",
    "acquisition": "buying a company, business or asset (counterparty = target)",
    "divestment": "selling a company, business, stake or asset",
    "subsidiary_jv": "a new subsidiary, joint venture or partnership entity formed or closed",
    "capex": "new plant, capacity expansion, capital spending approved or commissioned",
    "partnership": "MoU, technology tie-up, distribution or supply agreement without an order value",
    "product_approval": "product launch, certification, licence, regulatory approval",
    "credit_rating": "credit rating assigned, upgraded, downgraded or reaffirmed (counterparty = agency)",
    "director_joined": "director appointed or re-appointed (person, role)",
    "director_left": "director resigned, retired or ceased (person, role, details.reason)",
    "kmp_joined": "CEO, CFO, company secretary or other senior manager appointed",
    "kmp_left": "CEO, CFO, company secretary or other senior manager left",
    "auditor_change": "statutory or other auditor appointed, resigned or replaced",
    "litigation": "legal case, arbitration, tax demand or penalty against or by the company",
    "regulatory_action": "SEBI, exchange or other regulator action, inspection or show-cause",
    "related_party": "material related-party transaction",
    "other_material": "any other fact an investor would care about",
}

PROMPT = """You read Indian stock-exchange filings for one listed company and turn each into a record an analyst can query later. For each item (id, date, category, headline, text):

1. "summary": 2-4 plain sentences saying what the filing discloses, with the key numbers and dates. For a routine notice one sentence is enough.
2. "facts": every material fact in the filing, each with one "type" from this list:
{types}

For each fact fill what the filing states and leave the rest null:
- "summary": one sentence stating the fact.
- "event_date" (YYYY-MM-DD: when it happened or takes effect; null if not stated), "period" (e.g. "Q2 FY26", "as of 2025-09-30").
- "amount_value" EXACTLY as printed (e.g. "1,250.5"), "amount_unit" ("crore", "lakh", "million", "billion", "thousand" or "" if written in full), "currency" (INR, USD, EUR ...).
- "pct", "shares", "price_inr" as plain numbers when stated.
- "counterparty" (customer, allottee, target, investee, agency), "person" and "role" (for people), "direction" (increase / decrease / new / exit).
- "details": other short key-value facts (strings).
- "quote": the sentence from the text that states the fact, copied verbatim. Every number you give must appear in this quote.

Rules: use only the item's text, never memory. One fact per order, person or allotment; if a total is given, add it as its own fact. A repeat of something already announced is still a fact (add details.repeat = "yes"). Results filings: one "results" fact per period with revenue as the amount. Routine notices (trading window, newspaper copies, depository certificates) have no facts. Keep each "id" unchanged."""

SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {
    "type": "object", "properties": {
        "id": {"type": "string"}, "summary": {"type": "string"},
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
    "required": ["id", "summary", "facts"]}}}, "required": ["items"]}

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
                                a.attachment_url, d.text_layer, d.ocr_text, d.bytes, d.pages
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
        out.append({"record_key": key, "symbol": head.symbol, "announced_at": g.announced_at.min(),
                    "category": cat or None, "subject": subject,
                    "sources": [{"source": r.source, "filing_id": r.filing_id, "url": r.attachment_url}
                                for r in g.itertuples()],
                    "doc_url": next((u for u in g.attachment_url if isinstance(u, str) and u.startswith("http")), None),
                    "routine": bool(ROUTINE.search(f"{cat} {subject or ''}")),
                    "text": text, "text_sha": hashlib.sha1(text.encode()).hexdigest(), "text_chars": len(text)})
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
                                  routine = EXCLUDED.routine, text_sha = EXCLUDED.text_sha,
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


def _store(rec: dict, out: dict, fx: dict, read_chars: int, truncated: bool) -> int:
    facts = [validate(f, rec["text"], fx) for f in out.get("facts") or []]
    with db_session() as (_, cur):
        cur.execute(f"DELETE FROM {schema.FACT} WHERE record_key = %s", (rec["record_key"],))
        for f in facts:
            row = {**f, "record_key": rec["record_key"], "symbol": rec["symbol"], "version": VERSION,
                   "details": json.dumps(f["details"]) if f["details"] else None}
            cur.execute(f"INSERT INTO {schema.FACT} ({', '.join(_FACT_COLS)}) VALUES ({', '.join(['%s'] * len(_FACT_COLS))})",
                        [row[c] for c in _FACT_COLS])
        cur.execute(f"""UPDATE {schema.RECORD} SET summary = %s, status = 'done', version = %s, chars_read = %s,
                               truncated = %s, extracted_at = now(), error = NULL, updated_at = now()
                         WHERE record_key = %s""",
                    (out.get("summary"), VERSION, read_chars, truncated, rec["record_key"]))
    return len(facts)


def _fail(keys: list[str], why: str) -> None:
    with db_session() as (_, cur):
        cur.execute(f"UPDATE {schema.RECORD} SET status = 'failed', error = %s, updated_at = now() WHERE record_key = ANY(%s)",
                    (why[:500], keys))


def extract(symbols: list[str], limit: int | None = None, any_time: bool = False) -> dict:
    from fundamentals.themes.tagger import call_claude
    from fundamentals.trigger_study.llm_runner import in_night_window
    from fundamentals.trigger_study.orders import _fx
    if not any_time and not in_night_window(datetime.now(timezone.utc)):
        return {"skipped": "outside the 22:00-08:00 IST window (use --any-time)"}
    prompt = PROMPT.format(types="\n".join(f"- {k}: {v}" for k, v in FACT_TYPES.items()))
    probes = emb.embed(FACT_PROBES, query=True)
    counts = {"records": 0, "facts": 0, "failed": 0, "calls": 0}
    for sym in symbols:
        todo = sql_to_df(f"""SELECT record_key FROM {schema.RECORD} WHERE symbol = %(s)s
                               AND (status IN ('pending', 'failed') OR version IS DISTINCT FROM %(v)s)
                             ORDER BY announced_at""", params={"s": sym, "v": VERSION})
        if limit:
            todo = todo.head(limit)
        if todo.empty:
            continue
        recs = _records(sym)
        items = []
        for key in todo.record_key:
            r = recs.get(key)
            if r is None:
                continue
            text, cut = reading(r, probes)
            items.append({**r, "text_read": text, "truncated": cut})
        fx = _fx(sorted({r["announced_at"].date() for r in items if r["announced_at"] is not None}))

        def run(batch: list[dict]) -> tuple[list[dict], dict]:
            payload = [{"id": f"R{i + 1}", "date": str(r["announced_at"])[:10], "category": r["category"],
                        "headline": r["subject"], "routine": r["routine"], "text": r["text_read"]}
                       for i, r in enumerate(batch)]
            return batch, call_claude(prompt, SCHEMA, payload)

        sized = [{**it, "text": it["text_read"]} for it in items]          # batches() sizes by what is read
        full = {it["record_key"]: it["text"] for it in items}
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
                    counts["facts"] += _store(rec, out, fx.get(d, {}), len(r["text_read"]), r["truncated"])
                    counts["records"] += 1
    return counts


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
