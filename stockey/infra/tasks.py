"""Task kinds, orchestrator side: which pool and site each runs against, how long it may take,
and what happens to its result -- stored, plus the follow-up tasks it creates. The worker side
of each kind is infra/handlers.py.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable

import pandas as pd

from infra import schema
from utils.db import db_session, upsert_to_db

TEXT_LAYER_MIN_CHARS_PER_PAGE = 200      # below this a PDF is a scan and needs OCR
OCR_MAX_PAGES = 40                       # long annual reports: the first 40 pages
OCR_PROBE_PAGES = 3                      # a scan is OCR'd this far first; Claude decides on the rest
BSE_LIVE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
BSE_HIS = "https://www.bseindia.com/xml-data/corpfiling/AttachHis/"


@dataclass(frozen=True)
class Spec:
    pool: str
    timeout_s: int
    max_attempts: int
    domain: Callable[[dict], str | None]
    ingest: Callable[[dict, dict, bytes | None], list[dict]]   # (task, result, file) -> follow-ups


def new_task(kind: str, payload: dict, *, dedup_key: str | None = None, priority: int = 50,
             parent_id: int | None = None, run_tag: str | None = None) -> dict:
    spec = SPECS[kind]
    return {"kind": kind, "payload": payload, "dedup_key": dedup_key, "pool": spec.pool,
            "domain": spec.domain(payload), "priority": priority, "max_attempts": spec.max_attempts,
            "parent_id": parent_id, "run_tag": run_tag}


def enqueue(tasks: list[dict], cur=None) -> int:
    """Insert tasks; one already queued under the same dedup_key is skipped. Returns how many
    were added."""
    if not tasks:
        return 0

    def _run(c) -> int:
        n = 0
        for t in tasks:
            c.execute(f"""INSERT INTO {schema.TASK} (kind, payload, dedup_key, pool, domain, priority, max_attempts,
                                                     parent_id, run_tag)
                          VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (dedup_key) DO NOTHING""",
                      (t["kind"], json.dumps(t["payload"]), t["dedup_key"], t["pool"], t["domain"], t["priority"],
                       t["max_attempts"], t["parent_id"], t["run_tag"]))
            n += c.rowcount
        return n

    if cur is not None:
        return _run(cur)
    with db_session() as (_, c):
        return _run(c)


def _site(url: str | None) -> str | None:
    url = url or ""
    return "bse" if "bseindia.com" in url else "nse" if "nseindia.com" in url else None


def _child(task: dict, kind: str, payload: dict, key: str) -> dict:
    return new_task(kind, payload, dedup_key=key, priority=task["priority"], parent_id=task["id"],
                    run_tag=task.get("run_tag"))


# --- announcements -----------------------------------------------------------------------------

def nse_rows(symbol: str, rows: list[dict]) -> pd.DataFrame:
    out = []
    for r in rows:
        when = pd.to_datetime(r.get("an_dt") or r.get("sort_date"), format="mixed", dayfirst=True, errors="coerce")
        key = "|".join([symbol, str(r.get("an_dt")), r.get("desc") or "", r.get("attchmntFile") or "",
                        r.get("attchmntText") or ""])
        out.append({"source": "nse", "filing_id": hashlib.sha1(key.encode()).hexdigest(), "symbol": symbol,
                    "announced_at": None if pd.isna(when) else when.to_pydatetime(), "category": r.get("desc"),
                    "subject": r.get("desc"), "text": (r.get("attchmntText") or "").strip() or None,
                    "attachment_url": r.get("attchmntFile") or None, "raw": json.dumps(r)})
    return pd.DataFrame(out).drop_duplicates(["source", "filing_id"]) if out else pd.DataFrame()


def bse_rows(symbol: str, rows: list[dict]) -> pd.DataFrame:
    out = []
    for r in rows:
        when = pd.to_datetime(r.get("DissemDT") or r.get("NEWS_DT") or r.get("DT_TM"), errors="coerce")
        att = r.get("ATTACHMENTNAME")
        out.append({"source": "bse", "filing_id": str(r.get("NEWSID")), "symbol": symbol,
                    "announced_at": None if pd.isna(when) else when.to_pydatetime(), "category": r.get("SUBCATNAME"),
                    "subject": r.get("HEADLINE") or r.get("NEWSSUB"), "text": (r.get("MORE") or "").strip() or None,
                    "attachment_url": (BSE_LIVE + att) if att else None, "raw": json.dumps(r)})
    return pd.DataFrame(out).drop_duplicates(["source", "filing_id"]) if out else pd.DataFrame()


def _ingest_announcements(task: dict, result: dict, _file: bytes | None) -> list[dict]:
    p = task["payload"]
    df = (nse_rows if task["kind"] == "nse_announcements" else bse_rows)(p["symbol"], result.get("rows") or [])
    if df.empty:
        return []
    upsert_to_db(df.assign(task_id=task["id"]), schema.ANNOUNCEMENT, ["source", "filing_id"])
    if not p.get("attachments", True):
        return []
    kids = []
    for r in df.dropna(subset=["attachment_url"]).itertuples():
        url = r.attachment_url
        if not url.startswith("http"):           # NSE writes "-" when there is no attachment
            continue
        alt = BSE_HIS + url[len(BSE_LIVE):] if url.startswith(BSE_LIVE) else None
        kids.append(_child(task, "fetch_attachment", {"url": url, "alt": alt, "source": r.source, "symbol": r.symbol,
                                                      "filing_id": r.filing_id, "ocr": p.get("ocr", True)},
                           f"att:{url}"))
    return kids


# --- documents ---------------------------------------------------------------------------------

def blob_key(url: str) -> str:
    return f"infra/docs/{hashlib.sha1(url.encode()).hexdigest()}.pdf"


def _ingest_attachment(task: dict, result: dict, file: bytes | None) -> list[dict]:
    from utils.store import save_file_content
    p = task["payload"]
    row = {"url": p["url"], "source": p.get("source"), "symbol": p.get("symbol"), "filing_id": p.get("filing_id"),
           "status": result.get("status", "failed"), "bytes": result.get("bytes"), "pages": result.get("pages"),
           "task_id": task["id"], "updated_at": pd.Timestamp.now(tz="UTC")}
    kids = []
    if file is not None and row["status"] == "fetched":
        key = blob_key(p["url"])
        save_file_content(key, file)
        text = result.get("text_layer") or ""
        per_page = len(text.strip()) / max(row["pages"] or 1, 1)
        needs_ocr = per_page < TEXT_LAYER_MIN_CHARS_PER_PAGE
        row.update(blob_key=key, text_layer=text or None, text_layer_chars=len(text.strip()),
                   ocr_status=("pending" if needs_ocr and p.get("ocr", True) else
                               "skipped" if needs_ocr else "not_needed"))
        if row["ocr_status"] == "pending":
            kids.append(_child(task, "ocr_pdf", {"url": p["url"], "blob_key": key, "first_page": 1,
                                                 "max_pages": OCR_PROBE_PAGES, "probe": True}, f"ocr:{p['url']}"))
    upsert_to_db(pd.DataFrame([row]), schema.DOCUMENT, ["url"])
    return kids


def ocr_rest(url: str, blob_key: str) -> dict:
    """The pages after the probe, when Claude found the first ones worth continuing
    (infra/records.py)."""
    return new_task("ocr_pdf", {"url": url, "blob_key": blob_key, "first_page": OCR_PROBE_PAGES + 1,
                                "max_pages": OCR_MAX_PAGES}, dedup_key=f"ocr-rest:{url}")


def _ingest_ocr(task: dict, result: dict, _file: bytes | None) -> list[dict]:
    """A probe stores its pages as 'probe_done' when the PDF has more; the rest is appended."""
    p = task["payload"]
    rest = (p.get("first_page") or 1) > 1
    with db_session() as (_, cur):
        cur.execute(f"""UPDATE {schema.DOCUMENT} SET
                               ocr_text = CASE WHEN %(rest)s THEN coalesce(ocr_text || E'\n\n', '') || %(t)s ELSE %(t)s END,
                               ocr_pages = CASE WHEN %(rest)s THEN coalesce(ocr_pages, 0) + %(n)s ELSE %(n)s END,
                               ocr_status = CASE WHEN %(probe)s AND coalesce(pages, 0) > %(n)s THEN 'probe_done' ELSE 'done' END,
                               ocr_seconds = coalesce(ocr_seconds, 0) + coalesce(%(sec)s, 0), ocr_model = %(m)s,
                               updated_at = now()
                         WHERE url = %(u)s""",
                    {"rest": rest, "t": result.get("text"), "n": result.get("pages") or 0, "probe": bool(p.get("probe")),
                     "sec": result.get("seconds"), "m": f"{result.get('model')} on {result.get('gpu')}", "u": p["url"]})
    return []


def _ingest_results_listing(task: dict, result: dict, file: bytes | None) -> list[dict]:
    from fundamentals.xbrl import store
    return store.ingest_listing(task, result, file)


def _ingest_xbrl(task: dict, result: dict, file: bytes | None) -> list[dict]:
    from fundamentals.xbrl import store
    return store.ingest_xbrl(task, result, file)


def _ingest_echo(task: dict, result: dict, _file: bytes | None) -> list[dict]:
    return []


SPECS: dict[str, Spec] = {
    "echo": Spec("test", 60, 2, lambda p: p.get("domain"), _ingest_echo),   # protocol smoke test
    "nse_announcements": Spec("hetzner", 240, 3, lambda p: "nse", _ingest_announcements),
    "bse_announcements": Spec("hetzner", 900, 3, lambda p: "bse", _ingest_announcements),
    "fetch_attachment": Spec("hetzner", 300, 3, lambda p: _site(p.get("url")), _ingest_attachment),
    "ocr_pdf": Spec("runpod", 2400, 2, lambda p: None, _ingest_ocr),
    "nse_results_listing": Spec("hetzner", 240, 3, lambda p: "nse", _ingest_results_listing),
    "fetch_xbrl": Spec("hetzner", 180, 3, lambda p: "nse", _ingest_xbrl),
}
