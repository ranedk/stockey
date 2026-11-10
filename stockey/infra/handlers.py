"""What a worker runs for each task kind. Worker-side only: no database, no keys, light imports
(requests; Playwright and the OCR model are imported only by the kinds that need them).

A handler takes the task payload and a Context and returns (result_dict, file_bytes_or_None).
It raises Blocked when the site refuses us (the orchestrator then holds this IP for that site)
and any other exception for an ordinary failure. Retries inside a task are done here, per kind
(RETRIES); the orchestrator counts the whole call as one attempt.
"""
from __future__ import annotations

import io
import subprocess
import tempfile
import time
import zipfile
from datetime import date, timedelta

import requests

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
# BSE's firewall answers the Linux UA above with 403 "Access Denied" and this one with data
# (checked from the main server 2026-11-09); the same string the BSE collectors send.
BSE_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
BSE_HEADERS = {"User-Agent": BSE_UA, "Referer": "https://www.bseindia.com/", "Accept": "application/json, text/plain, */*"}
BSE_API = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
BSE_MAX_RANGE_DAYS = 360          # BSE refuses a range over 12 months
BSE_MAX_PAGES = 40
MIN_GAP_S = 10.0                  # per site, inside this worker -- a second check behind the orchestrator's
MAX_FILE_BYTES = 60_000_000
RETRIES = {"nse_announcements": 1, "bse_announcements": 2, "fetch_attachment": 2, "ocr_pdf": 1}
OCR_MODEL = "zai-org/GLM-OCR"
OCR_PROMPT = "Text Recognition:"  # the model card's exact prompt (utils/ocr/llm_ocr.py)
OCR_MAX_IMAGE = 1280
OCR_MAX_NEW_TOKENS = 4096


class Blocked(Exception):
    """The site refused us: 401/403/429, a captcha or block page, or HTML where JSON belongs."""


class Context:
    """Per-worker state: the last request time per site, and a browser session for NSE."""

    def __init__(self) -> None:
        self._last: dict[str, float] = {}
        self._pw = None
        self._browser = None
        self._page = None
        self._ocr = None

    def gap(self, site: str) -> None:
        wait = self._last.get(site, 0.0) + MIN_GAP_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last[site] = time.monotonic()

    # --- NSE needs a real browser session (Akamai) ---
    def nse_page(self):
        if self._page is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            # headed under Xvfb on the worker (the bootstrap starts it); NSE turns headless away
            self._browser = self._pw.chromium.launch(headless=False, args=["--no-first-run"])
            self._page = self._browser.new_context(user_agent=UA, locale="en-IN").new_page()
            self.gap("nse")
            self._page.goto("https://www.nseindia.com", wait_until="domcontentloaded", timeout=60_000)
            self._page.wait_for_timeout(2000)
        return self._page

    def reset_nse(self) -> None:
        for obj in (self._browser, self._pw):
            try:
                obj and (obj.close() if hasattr(obj, "close") else obj.stop())
            except Exception:  # noqa: BLE001
                pass
        self._pw = self._browser = self._page = None

    def ocr_model(self):
        if self._ocr is None:
            from transformers import AutoModelForImageTextToText, AutoProcessor
            processor = AutoProcessor.from_pretrained(OCR_MODEL)
            model = AutoModelForImageTextToText.from_pretrained(OCR_MODEL, torch_dtype="auto", device_map="auto")
            self._ocr = (processor, model)
        return self._ocr


# --- NSE -------------------------------------------------------------------------------------

_NSE_JS = """async (url) => {
    const r = await fetch(url, { credentials: 'same-origin' });
    const t = await r.text();
    return { status: r.status, body: t };
}"""


def nse_announcements(p: dict, ctx: Context) -> tuple[dict, None]:
    """Every announcement for a symbol in [from, to] (dd-mm-YYYY), as NSE serves it."""
    import json
    url = (f"https://www.nseindia.com/api/corporate-announcements?index=equities&symbol={p['symbol']}"
           f"&from_date={p['from']}&to_date={p['to']}")
    page = ctx.nse_page()
    ctx.gap("nse")
    r = page.evaluate(_NSE_JS, url)
    if r["status"] in (401, 403, 429):
        ctx.reset_nse()
        raise Blocked(f"NSE HTTP {r['status']}")
    if r["status"] != 200:
        raise RuntimeError(f"NSE HTTP {r['status']}")
    try:
        rows = json.loads(r["body"])
    except ValueError as exc:
        ctx.reset_nse()
        raise Blocked(f"NSE returned non-JSON ({r['body'][:80]!r})") from exc
    if not isinstance(rows, list):
        raise RuntimeError(f"NSE unexpected payload {str(rows)[:120]}")
    return {"rows": rows}, None


# --- BSE -------------------------------------------------------------------------------------

def _bse_get(params: dict, ctx: Context) -> dict:
    ctx.gap("bse")
    r = requests.get(BSE_API, params=params, headers=BSE_HEADERS, timeout=30)
    if r.status_code in (401, 403, 429):
        raise Blocked(f"BSE HTTP {r.status_code}")
    if r.status_code != 200:
        raise RuntimeError(f"BSE HTTP {r.status_code}")
    try:
        d = r.json()
    except ValueError as exc:
        raise Blocked(f"BSE returned non-JSON ({r.text[:80]!r})") from exc
    if "Table" not in d:
        raise RuntimeError(f"BSE response without Table: {str(d.get('Message', ''))[:120]}")
    return d


def bse_announcements(p: dict, ctx: Context) -> tuple[dict, None]:
    """Every announcement for a BSE scrip code in [from, to] (YYYY-MM-DD), in <= 12-month
    chunks, every page (ROWCNT in Table1)."""
    start, end = date.fromisoformat(p["from"]), date.fromisoformat(p["to"])
    rows: list[dict] = []
    chunk_end = end
    while chunk_end >= start:
        chunk_start = max(start, chunk_end - timedelta(days=BSE_MAX_RANGE_DAYS))
        page, total, got = 1, None, []
        while page <= BSE_MAX_PAGES:
            d = _bse_get({"pageno": page, "strCat": "-1", "subcategory": "-1", "strPrevDate": chunk_start.strftime("%Y%m%d"),
                          "strToDate": chunk_end.strftime("%Y%m%d"), "strSearch": "P", "strScrip": p["scrip"],
                          "strType": "C"}, ctx)
            batch = d.get("Table") or []
            t1 = d.get("Table1") or []
            if total is None and t1 and "ROWCNT" in t1[0]:
                total = int(t1[0]["ROWCNT"])
            got.extend(batch)
            if not batch or total is None or len(got) >= total:
                break
            page += 1
        rows.extend(got)
        chunk_end = chunk_start - timedelta(days=1)
    return {"rows": rows}, None


# --- attachments -------------------------------------------------------------------------------

def _site(url: str) -> str:
    return "bse" if "bseindia.com" in url else "nse" if "nseindia.com" in url else "web"


def pdf_info(pdf: bytes) -> tuple[int | None, str]:
    """(pages, text layer) via poppler's pdfinfo / pdftotext."""
    with tempfile.NamedTemporaryFile(suffix=".pdf") as fh:
        fh.write(pdf)
        fh.flush()
        info = subprocess.run(["pdfinfo", fh.name], capture_output=True, text=True, timeout=60)
        pages = next((int(ln.split()[-1]) for ln in info.stdout.splitlines() if ln.startswith("Pages:")), None)
        txt = subprocess.run(["pdftotext", "-layout", fh.name, "-"], capture_output=True, text=True, timeout=300)
    return pages, (txt.stdout if txt.returncode == 0 else "")


def fetch_attachment(p: dict, ctx: Context) -> tuple[dict, bytes | None]:
    """The file at p["url"]; on a 404, p["alt"] if given (BSE moves older filings from
    AttachLive/ to AttachHis/)."""
    got = _fetch_one(p["url"], ctx)
    if got[0].get("status") == "not_found" and p.get("alt"):
        got = _fetch_one(p["alt"], ctx)
        got[0]["url_used"] = p["alt"]
    return got


def _fetch_one(url: str, ctx: Context) -> tuple[dict, bytes | None]:
    site = _site(url)
    ctx.gap(site)
    headers = BSE_HEADERS if site == "bse" else {"User-Agent": BSE_UA}
    r = requests.get(url, headers=headers, timeout=90, stream=True)
    if r.status_code in (401, 403, 429) and site != "web":
        raise Blocked(f"{site} HTTP {r.status_code}")
    if r.status_code == 404:
        return {"status": "not_found"}, None
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    body = r.raw.read(MAX_FILE_BYTES + 1, decode_content=True)
    if len(body) > MAX_FILE_BYTES:
        return {"status": "failed", "error": "file too large"}, None
    if body.startswith(b"PK"):                       # older NSE filings: a zip holding the PDF
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".pdf")]
            body = z.read(names[0]) if names else b""
    if not body.startswith(b"%PDF"):
        if b"<html" in body[:500].lower() and site != "web":
            raise Blocked(f"{site} returned an HTML page for a file")
        return {"status": "not_pdf", "bytes": len(body)}, None
    pages, text = pdf_info(body)
    return {"status": "fetched", "bytes": len(body), "pages": pages, "text_layer": text}, body


# --- OCR (GPU) ---------------------------------------------------------------------------------

def ocr_pdf(p: dict, ctx: Context, pdf: bytes) -> tuple[dict, None]:
    """GLM-OCR on pages first_page..max_pages, with the local CPU path's prompt and image size."""
    import torch
    from pdf2image import convert_from_bytes
    processor, model = ctx.ocr_model()
    started = time.monotonic()
    first = p.get("first_page") or 1
    pages = convert_from_bytes(pdf, dpi=150, first_page=first, last_page=p.get("max_pages") or None)
    out = []
    for img in pages:
        img = img.convert("RGB")
        img.thumbnail((OCR_MAX_IMAGE, OCR_MAX_IMAGE))
        msgs = [{"role": "user", "content": [{"type": "image", "image": img}, {"type": "text", "text": OCR_PROMPT}]}]
        inputs = processor.apply_chat_template(msgs, tokenize=True, add_generation_prompt=True, return_dict=True,
                                               return_tensors="pt").to(model.device)
        inputs.pop("token_type_ids", None)
        with torch.inference_mode():
            ids = model.generate(**inputs, max_new_tokens=OCR_MAX_NEW_TOKENS)
        out.append(processor.decode(ids[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True).strip())
    return {"pages": len(out), "text": "\n\n".join(f"--- page {first + i} ---\n{t}" for i, t in enumerate(out)),
            "seconds": round(time.monotonic() - started, 1), "model": OCR_MODEL,
            "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu"}, None


def echo(p: dict, ctx: Context) -> tuple[dict, bytes | None]:
    """Protocol smoke test: optionally fail, report a block, sleep, or return a file."""
    if p.get("fail"):
        raise RuntimeError("asked to fail")
    if p.get("block"):
        raise Blocked("asked to report a block")
    time.sleep(p.get("sleep", 0))
    return {"echo": p.get("msg")}, (p["file"].encode() if p.get("file") else None)


HANDLERS = {"echo": echo, "nse_announcements": nse_announcements, "bse_announcements": bse_announcements,
            "fetch_attachment": fetch_attachment, "ocr_pdf": ocr_pdf}
NEEDS_FILE = {"ocr_pdf"}          # the worker downloads the task's file from the orchestrator first
