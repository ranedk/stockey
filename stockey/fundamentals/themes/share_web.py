"""Revenue shares from the web, for theme members that the filings leave without one (TODO H:
"a web pass, like the stock web check, with quoted sources"). docs/THEMATIC_WATCHLIST_PRD.md
section 4.2; the third pass after share_finder's sole-business and filing passes.

Per (company, theme) still unquantified on the latest watchlist, the model searches the web
(Claude CLI with WebSearch and WebFetch only) for a page that states the company's revenue split
by product or business -- annual report, investor presentation, rating rationale, results call
-- and returns the numbers with their verbatim sentences and the page's URL. The model's word is
not taken: this module downloads that URL itself and runs share_finder's checks against the
page's own text (every number in its quote, every quote on the page, the share computed in
code). A page it cannot download is stored as "unverified" and not used.

Calls run in the 22:00-08:00 IST night window unless --any-time.

    python -m fundamentals.themes.share_web [--any-time] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from contextlib import nullcontext
from datetime import datetime, timezone
from urllib.parse import urlparse

import pandas as pd
import requests

from fundamentals.themes import catalog, share_finder, web_check
from fundamentals.trigger_study.llm_runner import in_night_window
from utils.db import sql_to_df

METHOD = "web"
MAX_BYTES = 40_000_000
FETCH_TIMEOUT_S = 90
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130 Safari/537.36"
# themes first in the order the page shows them
_THEME_ORDER = {"shown": 0, "next": 1, "late": 2, "out": 3, "unranked": 4}

PROMPT = """You find how much of one Indian listed company's revenue comes from an investment theme, using web search and web fetch. Read public pages only: the company's annual report, investor presentation, results-call transcript, credit-rating rationale, stock-exchange filings, its own website. Never submit forms, log in, or send any personal information.

The input gives the company, its ticker, and the theme (name, definition, and "not_in_theme": look-alikes that do NOT count).
Find a page that states the company's revenue split BY PRODUCT OR BUSINESS: a percentage of revenue (or sales / turnover / income from operations) from the theme's products, or the revenue of the theme's products together with the total revenue for the same period and unit. A segment counts only if what it sells is inside the theme's definition; if several lines are inside it, give each in "theme_lines" (name and amount exactly as printed, with its line) and "theme_revenue" null, and the code adds them. A line that mixes theme products with a "not_in_theme" look-alike does not count unless the page splits it. Never use a geographic split (exports vs domestic). Prefer the latest full financial year.
Answer:
- "found": "pct" (the stated percentage) with "pct_quote" (the verbatim sentence holding it), OR "theme_revenue" and "total_revenue" (numbers exactly as printed) with "theme_quote" and "total_quote" (verbatim lines holding each); plus "period", "document" (what the page is) and "source_url" -- the exact URL of the page or PDF where EVERY quote appears (all quotes must come from that one page; it will be downloaded and checked).
- "none": no page states such a split.
Many company documents sit on nseindia.com / bseindia.com, which you often cannot open. Whatever the result, list in "documents" up to 4 URLs (PDF or page) of the company's own documents most likely to state the split -- latest annual report, investor presentation, results-call transcript, rating rationale -- especially ones you could not open; they will be downloaded and read separately.
Always give "note": one or two plain sentences on what you found (which pages you read and what split, if any, they give).
Never estimate or compute a share yourself, never use a news article's paraphrase, order book or capacity shares. Copy quotes exactly as printed."""

SCHEMA = {"type": "object", "properties": {
    "result": {"type": "string", "enum": ["found", "none"]},
    "pct": {"type": ["number", "null"]}, "pct_quote": {"type": ["string", "null"]},
    "theme_revenue": {"type": ["string", "null"]}, "total_revenue": {"type": ["string", "null"]},
    "theme_quote": {"type": ["string", "null"]}, "total_quote": {"type": ["string", "null"]},
    "theme_lines": {"type": ["array", "null"], "items": {"type": "object", "properties": {
        "name": {"type": "string"}, "amount": {"type": "string"}, "quote": {"type": "string"}},
        "required": ["name", "amount", "quote"]}},
    "period": {"type": ["string", "null"]}, "document": {"type": ["string", "null"]},
    "source_url": {"type": ["string", "null"]}, "note": {"type": ["string", "null"]},
    "documents": {"type": ["array", "null"], "items": {"type": "string"}}},
    "required": ["result", "note"]}
MAX_DOCS = 4
RETRY_DAYS = 90          # a member with no share found is asked again once new reports may be out


REVIEW_PROMPT = """You are a reviewer. Each item gives an Indian listed company, an investment theme (definition, plus "not_in_theme" look-alikes that do NOT count), a share of revenue found for it, and the quote it was computed from. Use the quote and what you know about the company. The share is a rough exposure figure: a single quarter or half-year, revenue net or gross of excise, a figure a year or two old, or a line that also holds a small (under ~10% of revenue) piece outside the theme are all FINE.

"reject" only for one of these, naming it in "reason" with roughly what the share should be if you know:
1. not revenue: a manufacturing, work, order-book, capacity, profit or geographic share, or a share WITHIN one segment rather than of the company's total revenue;
2. wrong line: the quoted line is mostly the company's business OUTSIDE the theme (e.g. its consumer business for a grid-equipment theme);
3. a big miss: another line that is inside the definition and is 10% or more of revenue is left out (e.g. gold jewellery alone for a jeweller that also sells diamond and silver jewellery);
4. the numbers cannot be what the labels say (unlabelled figures, or amounts from different periods divided).
Otherwise "confirm". Keep the item "id" unchanged."""

REVIEW_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {
    "type": "object", "properties": {"id": {"type": "string"}, "verdict": {"type": "string", "enum": ["confirm", "reject"]},
                                     "reason": {"type": "string"}},
    "required": ["id", "verdict", "reason"]}}}, "required": ["items"]}


def review(rows: list[dict], company: dict[str, str], themes: dict) -> None:
    """Second look at every quantified row, in place: a reject becomes status "rejected"."""
    todo = [(k, r) for k, r in enumerate(rows) if r.get("status") == "quantified"]
    if not todo:
        return
    t = {k: themes[r["theme_id"]] for k, r in todo}
    res = share_finder.call_claude(REVIEW_PROMPT, REVIEW_SCHEMA, [{
        "id": str(k), "company": company.get(r["company_master_id"], r["company_master_id"]), "theme": t[k].name,
        "definition": t[k].definition, "not_in_theme": list(t[k].exclude), "share_pct": r["exposure_pct"],
        "quote": r.get("quote"), "source": r.get("source_label")} for k, r in todo])
    got = {str(x.get("id")): x for x in res["items"]} if res["ok"] else {}
    for k, r in todo:
        v = got.get(str(k))
        if v is None or v.get("verdict") != "confirm":
            r.update(status="rejected" if v else "unverified",
                     note=f"review: {v.get('reason')}" if v else "review did not answer")


# --- the page, downloaded by us ---------------------------------------------------------------

def _gate(host: str):
    """Exchange and screener.in requests go through the workspace's per-domain gates."""
    if host.endswith("nseindia.com"):
        from utils.nse_rate_limiter import nse_request_gate
        return nse_request_gate()
    from utils.exchange_rate_limiter import exchange_request_gate
    for suffix, domain in (("bseindia.com", "bse"), ("screener.in", "screenerin"), ("sebi.gov.in", "sebi")):
        if host.endswith(suffix):
            return exchange_request_gate(domain=domain)
    return nullcontext()


def page_text(body: bytes, content_type: str = "") -> str:
    if body.startswith(b"%PDF"):
        with tempfile.NamedTemporaryFile(suffix=".pdf") as fh:
            fh.write(body)
            fh.flush()
            out = subprocess.run(["pdftotext", "-layout", fh.name, "-"], capture_output=True, text=True, timeout=300)
        return out.stdout if out.returncode == 0 else ""
    if "html" in content_type or body.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(body, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return soup.get_text("\n")
    return body.decode("utf-8", errors="replace") if content_type.startswith("text/") else ""


def fetch_page(url: str) -> tuple[str | None, str | None]:
    """(text, error). Plain GET with the request gates; nothing is sent but the URL."""
    p = urlparse(url or "")
    if p.scheme not in ("http", "https") or not p.netloc:
        return None, "no usable URL"
    try:
        with _gate(p.netloc.lower()):
            r = requests.get(url, headers={"User-Agent": UA}, timeout=FETCH_TIMEOUT_S, stream=True)
            body = r.raw.read(MAX_BYTES + 1, decode_content=True) if r.ok else b""
    except Exception as exc:  # noqa: BLE001 -- a bad page leaves this one unverified
        return None, f"fetch failed: {exc!r}"[:300]
    if not r.ok:
        return None, f"HTTP {r.status_code}"
    if len(body) > MAX_BYTES:
        return None, "page too large"
    text = page_text(body, r.headers.get("Content-Type", "").lower())
    return (text, None) if len(text.strip()) > 200 else (None, "no text on the page (scan or script-only)")


def verify(out: dict, text: str | None, error: str | None) -> dict:
    """share_finder's filing checks, run on the downloaded page."""
    if out.get("result") != "found":
        return {"status": "not_found", "note": (out.get("note") or None)}
    url = out.get("source_url")
    if text is None:
        return {"status": "unverified", "source_url": url, "note": error}
    v = share_finder.validate_source(out, text)
    host = urlparse(url).netloc.removeprefix("www.")
    label = f"web: {host} {out.get('period') or ''}".strip()
    return {**v, "source_url": url, **({"source_label": label} if v["status"] == "quantified" else {})}


def read_documents(urls: list[str], r, t) -> dict | None:
    """Download the documents the model named and read each with share_finder's filing pass
    (no web tools; quotes and numbers checked against the downloaded text). The first
    quantified share wins; None when none gives one."""
    for url in [u for u in urls if isinstance(u, str)][:MAX_DOCS]:
        text, _ = fetch_page(url)
        if not text:
            continue
        res = share_finder.call_claude(share_finder.SOURCE_PROMPT, share_finder.SOURCE_SCHEMA, [{
            "id": "0", "company": r.company_name, "theme": t.name, "definition": t.definition,
            "not_in_theme": list(t.exclude), "documents": share_finder.focus(text)}])
        if not res["ok"] or not res["items"]:
            continue
        v = share_finder.validate_source(res["items"][0], text)
        if v["status"] == "quantified":
            host = urlparse(url).netloc.removeprefix("www.")
            return {**v, "source_url": url, "source_label": f"web: {host} {res['items'][0].get('period') or ''}".strip(),
                    "note": "read from a document the web search named"}
    return None


# --- the run ---------------------------------------------------------------------------------

def targets() -> pd.DataFrame:
    """Unquantified members on the latest watchlist, shown themes first."""
    df = sql_to_df("""
        SELECT s.company_master_id, s.theme_id, s.company_name, s.ticker, t.status AS theme_status, t.position
          FROM fundamentals_theme_watchlist_stock s
          JOIN fundamentals_theme_watchlist_theme t USING (as_of_date, theme_version, theme_id, watchlist_version)
         WHERE s.as_of_date = (SELECT max(as_of_date) FROM fundamentals_theme_watchlist_stock)
           AND s.status = 'unquantified' AND s.share_pct IS NULL""")
    df["o"] = df["theme_status"].map(_THEME_ORDER).fillna(9)
    return df.sort_values(["o", "position"]).drop(columns="o")


def run(limit: int | None = None, any_time: bool = False) -> dict:
    if not any_time and not in_night_window(datetime.now(timezone.utc)):
        return {"skipped": "outside the 22:00-08:00 IST window (use --any-time)"}
    share_finder.ensure_table()
    themes = {t.id: t for t in catalog.load(1)}
    done = sql_to_df(f"SELECT company_master_id, theme_id, status FROM {share_finder.TABLE}"  # noqa: S608
                     " WHERE method = %(m)s AND finder_version = %(v)s"
                     f" AND (status = 'quantified' OR checked_at > now() - interval '{RETRY_DAYS} days')",
                     params={"m": METHOD, "v": share_finder.FINDER_VERSION})
    todo = targets().merge(done, on=["company_master_id", "theme_id"], how="left")
    todo = todo[todo["status"].isna()]
    todo = todo.head(limit) if limit else todo
    counts = {"due": len(todo)}
    for r in todo.itertuples(index=False):
        t = themes[r.theme_id]
        res = web_check.call({"company": r.company_name, "ticker": r.ticker, "theme": t.name,
                              "definition": t.definition, "not_in_theme": list(t.exclude)}, PROMPT, SCHEMA)
        if not res["ok"]:
            if res["kind"] == "limit":
                counts["stopped"] = res["message"][:200]
                break
            counts["failed_calls"] = counts.get("failed_calls", 0) + 1
            continue
        out = res["out"]
        text, err = fetch_page(out.get("source_url")) if out.get("result") == "found" else (None, None)
        row = {"company_master_id": r.company_master_id, "theme_id": r.theme_id, "method": METHOD,
               **verify(out, text, err)}
        if row["status"] != "quantified":
            got = read_documents(out.get("documents") or [], r, t)
            if got is not None:
                row = {**row, **got}
            elif out.get("documents"):
                row["note"] = f"{row.get('note') or ''} [documents read: none stated a usable split]".strip()
        review([row], {r.company_master_id: r.company_name}, themes)
        share_finder._store([row])
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Find theme revenue shares on the web, checked against the page.")
    ap.add_argument("--any-time", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args(argv)
    print(json.dumps(run(limit=a.limit, any_time=a.any_time), default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
