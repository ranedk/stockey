"""Builds the tagging queue: keyword pre-filter, then shuffled blind batches.

Keyword classes are decided from the filing's TEXT and the company's category together, and a
material word anywhere in the text always sends the filing to the tagger -- a company that
files an order win under "Trading Window" must not be dropped as routine.
"""
from __future__ import annotations

import hashlib
import json
import re

import pandas as pd

from utils.db import db_session, sql_to_df

from fundamentals.collectors import ocr_research_queue

from fundamentals.trigger_study.filings import FILINGS_TABLE
from fundamentals.trigger_study.events import EVENTS_TABLE, samples
from fundamentals.trigger_study.llm_runner import BATCHES_TABLE, DEFAULT_MODEL, TAGS_TABLE, ensure_tables

TAG_BATCH = 50
DOC_BATCH = 8
TEXT_CHARS = 600
DOC_CHARS = 5000
SEED = 1

MATERIAL_RX = re.compile(
    r"\b(order|contract|lo[ai]\b|letter of (award|intent)|bag|tender|capacity|expan|commission|plant|"
    r"capex|acqui|takeover|joint venture|\bjv\b|partner|merger|amalgamat|demerg|divest|slump sale|"
    r"approv|usfda|us fda|launch|patent|licen[cs]e|qip|preferential|warrant|rights issue|fund rais|"
    r"buyback|buy-back|bonus|split|rating|upgrade|downgrade|default|penalt|fraud|raid|search|"
    r"fire|accident|shutdown|strike|resign|appoint.*(ceo|managing director|cfo)|stake|"
    r"open offer|guidance|record (revenue|profit|sales)|business update|sales (volume|numbers|update))",
    re.I)

ROUTINE_RX = re.compile(
    r"(trading window|newspaper|loss of share|duplicate share|share certificate|record date|book closure|"
    r"annual general meeting|\bagm\b|\begm\b|postal ballot|e-voting|scrutini[sz]er|74\s*\(5\)|"
    r"depositories and participants|esop|esos|esps|investor complaint|secretarial compliance|"
    r"compliance certificate|registrar|\brta\b|kyc|unclaimed|iepf|shareholders meeting|"
    r"schedule of analyst|institutional investor meet|recording of analyst|audio recording|"
    r"intimation of (the )?board meeting|board meeting intimation|closure of trading|"
    r"change in (the )?(company secretary|compliance officer)|annual report|business responsibility)",
    re.I)

RESULTS_RX = re.compile(r"(financial result|unaudited|audited results|quarterly results|limited review)", re.I)
CALL_DOC_RX = re.compile(r"(investor presentation|transcript|earnings call|con\.? call|conference call|analyst meet)", re.I)


def keyword_class(category: str | None, text: str | None) -> str:
    cat, txt = category or "", text or ""
    both = f"{cat} || {txt}"
    if MATERIAL_RX.search(txt) and not RESULTS_RX.search(txt):
        return "tag"
    if RESULTS_RX.search(both) or cat in ("Financial Result Updates",):
        return "results_filed"
    if CALL_DOC_RX.search(both):
        return "presentation_or_call"
    if ROUTINE_RX.search(both):
        return "routine"
    return "tag"


def window_filings(sample: str = "pilot") -> pd.DataFrame:
    """Every filing of a sampled stock that falls inside one of ITS event windows."""
    return sql_to_df(f"""
        SELECT DISTINCT f.filing_id, f.symbol, f.announced_at, f.nse_category, f.text, f.attachment_url,
               f.company_name, coalesce(sr.description, e.sector_code) AS sector
          FROM {FILINGS_TABLE} f
          JOIN {EVENTS_TABLE} e ON e.symbol = f.symbol AND e.sample = ANY(%s)
               AND f.announced_at::date BETWEEN e.window_start AND e.window_end
          LEFT JOIN (SELECT DISTINCT ON (code) code, description FROM fundamentals_sector_reference
                      ORDER BY code, as_of_date DESC) sr ON sr.code = e.sector_code
    """, params=(samples(sample),))


def _already_batched(pass_name: str) -> set[str]:
    df = sql_to_df(f"""SELECT jsonb_array_elements(payload)->>'id' AS id FROM {BATCHES_TABLE}
                        WHERE pass = %s AND status <> 'failed'""", params=(pass_name,))
    return set(df["id"])


def _insert_batches(pass_name: str, items: list[dict], size: int, model: str) -> int:
    n = 0
    with db_session() as (_, cur):
        for i in range(0, len(items), size):
            chunk = items[i:i + size]
            bid = f"{pass_name}-" + hashlib.sha1(",".join(x["id"] for x in chunk).encode()).hexdigest()[:16]
            cur.execute(f"""INSERT INTO {BATCHES_TABLE} (batch_id, pass, model, payload)
                            VALUES (%s, %s, %s, %s) ON CONFLICT (batch_id) DO NOTHING""",
                        (bid, pass_name, model, json.dumps(chunk, ensure_ascii=False)))
            n += cur.rowcount
    return n


def build_tag_queue(sample: str = "pilot", model: str = DEFAULT_MODEL) -> dict:
    ensure_tables()
    df = window_filings(sample)
    df["kw"] = [keyword_class(c, t) for c, t in zip(df["nse_category"], df["text"])]
    kw_rows = df[df["kw"] != "tag"]
    with db_session() as (_, cur):
        for r in kw_rows.itertuples():
            cur.execute(f"""INSERT INTO {TAGS_TABLE} (filing_id, pass, batch_id, trigger, model)
                            VALUES (%s, 'keyword', 'keyword', %s, 'keyword')
                            ON CONFLICT (filing_id, pass) DO UPDATE SET trigger = EXCLUDED.trigger""",
                        (r.filing_id, r.kw))
    done = _already_batched("tag")
    todo = df[(df["kw"] == "tag") & ~df["filing_id"].isin(done)].sample(frac=1.0, random_state=SEED)
    items = [{"id": r.filing_id, "company": r.company_name or r.symbol, "sector": r.sector,
              "date": str(pd.Timestamp(r.announced_at).date()), "nse_category": r.nse_category,
              "text": (r.text or "")[:TEXT_CHARS]} for r in todo.itertuples()]
    batches = _insert_batches("tag", items, TAG_BATCH, model)
    return {"window_filings": len(df), "keyword_classes": df["kw"].value_counts().to_dict(),
            "queued_items": len(items), "new_batches": batches}


DOC_TRIGGERS = ("order_win", "capacity_expansion", "acquisition_or_jv", "large_investor_entry",
                "fundraise", "new_product_or_approval", "divestment_or_demerger")


def document_candidates() -> pd.DataFrame:
    """Tagged filings worth reading the attachment for: too vague to tag, or a notable
    trigger whose amount the subject line did not state."""
    return sql_to_df(f"""
        SELECT t.filing_id, f.symbol, f.company_name, f.announced_at, f.nse_category, f.text, f.attachment_url
          FROM {TAGS_TABLE} t JOIN {FILINGS_TABLE} f USING (filing_id)
         WHERE t.pass = 'tag' AND f.attachment_url ILIKE '%%.pdf'
           AND NOT EXISTS (SELECT 1 FROM {TAGS_TABLE} s WHERE s.filing_id = t.filing_id AND s.pass = 'document_skip')
           AND (t.needs_document OR (t.trigger = ANY(%s) AND t.value_cr IS NULL AND t.importance >= 2))
    """, params=(list(DOC_TRIGGERS),))


OCR_REQUESTER = "trigger_study"


def queue_ocred_documents(model: str = DEFAULT_MODEL) -> dict:
    """Scans the OCR job has read since the last call -> document batches."""
    done = ocr_research_queue.done_for(OCR_REQUESTER)
    if done.empty:
        return {"ocred_queued": 0}
    done = done[~done["doc_id"].isin(_already_batched("document"))]
    if done.empty:
        return {"ocred_queued": 0}
    meta = sql_to_df(f"""SELECT filing_id, symbol, company_name, announced_at, nse_category, text
                           FROM {FILINGS_TABLE} WHERE filing_id = ANY(%s)""", params=(list(done["doc_id"]),))
    doc = dict(zip(done["doc_id"], done["text"]))
    items = [{"id": r.filing_id, "company": r.company_name or r.symbol, "date": str(pd.Timestamp(r.announced_at).date()),
              "nse_category": r.nse_category, "text": (r.text or "")[:TEXT_CHARS],
              "document": re.sub(r"[ \t]+", " ", doc.get(r.filing_id) or "").strip()[:DOC_CHARS]}
             for r in meta.itertuples() if len((doc.get(r.filing_id) or "").strip()) >= 100]
    return {"ocred_queued": len(items), "ocred_batches": _insert_batches("document", items, DOC_BATCH, model)}


def build_document_queue(model: str = DEFAULT_MODEL, limit: int | None = None) -> dict:
    """Downloads each candidate's attachment (through the NSE gate), keeps the first pages'
    text, and queues it. Scanned PDFs go to the low-priority research OCR queue
    (fundamentals/collectors/ocr_research_queue.py) and come back through
    queue_ocred_documents() once the daily OCR job has read them."""
    import subprocess
    import tempfile

    from environs import Env
    from playwright.sync_api import sync_playwright

    from utils.cdp import connect_over_cdp
    from utils.nse_rate_limiter import nse_goto, nse_request_gate

    from fundamentals.trigger_study.filings import nse_busy
    ensure_tables()
    ocred = queue_ocred_documents(model)
    if nse_busy():
        return {"skipped": "nse_busy_window", **ocred}
    cand = document_candidates()
    cand = cand[~cand["filing_id"].isin(_already_batched("document"))]
    if limit:
        cand = cand.head(limit)
    if cand.empty:
        return {"candidates": 0, **ocred}
    env = Env()
    env.read_env()
    items, skipped = [], []   # skipped: (filing_id, reason) -- recorded so cron never re-downloads them
    with sync_playwright() as p:
        browser = connect_over_cdp(p, env("CDP_ENDPOINT"), caller="fundamentals.trigger_study.documents")
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()
        try:
            nse_goto(page, "https://www.nseindia.com")
            for r in cand.sample(frac=1.0, random_state=SEED).itertuples():
                if nse_busy():   # stop downloading; what is queued so far is kept
                    break
                try:
                    with nse_request_gate():
                        resp = ctx.request.get(r.attachment_url, timeout=60000)
                    if not resp.ok:
                        skipped.append((r.filing_id, f"download_failed_http_{resp.status}"))
                        continue
                    pdf = resp.body()
                    with tempfile.NamedTemporaryFile(suffix=".pdf") as fh:
                        fh.write(pdf)
                        fh.flush()
                        out = subprocess.run(["pdftotext", "-l", "2", "-layout", fh.name, "-"],
                                             capture_output=True, text=True, timeout=60)
                    doc = re.sub(r"[ \t]+", " ", out.stdout or "").strip()
                except Exception:  # noqa: BLE001 -- one bad attachment must not stop the queue
                    skipped.append((r.filing_id, "download_failed"))
                    continue
                if len(doc) < 200:
                    # a scan: the daily OCR job reads pages 1-2 when its own queue is empty
                    ocr_research_queue.enqueue(r.filing_id, OCR_REQUESTER, pdf, max_pages=2)
                    skipped.append((r.filing_id, "sent_to_ocr"))
                    continue
                items.append({"id": r.filing_id, "company": r.company_name or r.symbol,
                              "date": str(pd.Timestamp(r.announced_at).date()),
                              "nse_category": r.nse_category, "text": (r.text or "")[:TEXT_CHARS],
                              "document": doc[:DOC_CHARS]})
        finally:
            page.close()
    with db_session() as (_, cur):
        for fid, why in skipped:
            cur.execute(f"""INSERT INTO {TAGS_TABLE} (filing_id, pass, batch_id, trigger, model)
                            VALUES (%s, 'document_skip', 'document_skip', %s, 'none')
                            ON CONFLICT (filing_id, pass) DO NOTHING""", (fid, why))
    batches = _insert_batches("document", items, DOC_BATCH, model)
    reasons = pd.Series([w for _, w in skipped]).value_counts().to_dict() if skipped else {}
    return {"candidates": len(cand), "queued": len(items), "skipped": reasons, "new_batches": batches, **ocred}
