"""Business-description pages from each company's latest annual report, the theme tagger's
main evidence (docs/THEMATIC_WATCHLIST_PRD.md section 4.2).

Why: screener.in's "Key Points" need a premium account after 20 views a month, so most
profiles carry only a 2-3 line "About" paragraph (found 2026-10-27). The annual report is the
primary source anyway. Per company, from the PDF's own text layer (`pdftotext -layout`; a
scanned report is recorded as `no_text`, not OCR'd):
  front    up to 2 "at a glance / about us / business overview" pages from the front section
  mda      6 business-heavy pages of the Management Discussion and Analysis, which SEBI's LODR
           requires to cover industry structure and segment- or product-wise performance
  segment  the segment-reporting note (Ind AS 108) pages
Only these excerpts are stored, not the whole report. The report is the newest BSE-hosted one
on the company's screener.in profile; it is re-read when a newer one appears there.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile

import pandas as pd

from fundamentals.collectors.company_profile import _wait_out_pipeline_window
from fundamentals.collectors.ocr_pipeline import DocumentFetchError, fetch_document_bytes_with_archive_fallback
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.fallback_telemetry import record_local_fallback_event

TABLE = "fundamentals_annual_report_text"
PROFILE_TABLE = "fundamentals_company_profile"
MIN_TEXT_CHARS = 20000     # a 100+ page report with less text than this is a scan
FRONT_CHARS, MDA_CHARS, SEGMENT_CHARS = 4000, 14000, 5000

_DDL = f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
        company_master_id TEXT NOT NULL,
        report_url TEXT NOT NULL,
        ticker TEXT NOT NULL,
        report_label TEXT,
        status TEXT NOT NULL,          -- ok | no_text | fetch_error
        n_pages INT,
        pages TEXT,
        front TEXT,
        mda TEXT,
        segment TEXT,
        error TEXT,
        load_ts TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (company_master_id, report_url)
    )"""


def ensure_table() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute(_DDL)

    execute_db_operation(_op, operation_name=f"{TABLE}:ensure_table")


def latest_report(documents: dict | None) -> tuple[str, str] | None:
    """(label, url) of the newest BSE-hosted annual report on the profile; screener lists
    them newest first."""
    for item in (documents or {}).get("Annual reports") or []:
        for link in item.get("links") or []:
            url = link.get("url") or ""
            if url.startswith("https://www.bseindia.com/"):
                return link.get("label") or item.get("title"), url
    return None


def pdf_pages(pdf_bytes: bytes) -> list[str]:
    with tempfile.NamedTemporaryFile(suffix=".pdf") as fh:
        fh.write(pdf_bytes)
        fh.flush()
        out = subprocess.run(["pdftotext", "-layout", fh.name, "-"], capture_output=True, text=True, timeout=300)
    return out.stdout.split("\f") if out.returncode == 0 else []


def squeeze(page: str) -> str:
    """Layout padding squeezed: column gaps stay visible as two spaces, which is what keeps a
    table's numbers apart."""
    return "\n".join(re.sub(r" {3,}", "  ", ln).rstrip() for ln in page.splitlines() if ln.strip())


# --- page choice (pure) ---------------------------------------------------------------------

_MDA = re.compile(r"management\W{0,3}s?\W*discussion\s*(and|&)\s*analysis", re.I)
_MDA_BODY = re.compile(r"industry structure|opportunities\s*(and|&)\s*threats|economic (overview|scenario|review)"
                       r"|global economy|indian economy|segment[- ]wise|outlook|risks?\s*(and|&)\s*concerns", re.I)
_TOC = re.compile(r"contents|what.s inside|inside this report", re.I)
_GLANCE = re.compile(r"at a glance|about us|who we are|corporate overview|company overview|business overview"
                     r"|our business|business model", re.I)
_BIZ = re.compile(r"segment|product|revenue|capacity|customers?|order book|plant|manufactur|business|exports?"
                  r"|market share", re.I)
_SEGMENT = re.compile(r"segment revenue|segment result|reportable segment|operating segment|ind as 108"
                      r"|segment information|single (business|reportable|operating) segment|one segment", re.I)
_SEGMENT_HEAD = re.compile(r"segment (reporting|information)", re.I)


def front_pages(pages: list[str]) -> list[int]:
    return [i for i in range(1, min(40, len(pages)))
            if _GLANCE.search(pages[i][:1500]) and len(pages[i].strip()) > 800 and not _TOC.search(pages[i][:400])][:2]


def mda_pages(pages: list[str]) -> list[int]:
    """The section starts at a page carrying its title with at least two of its required
    headings on it or the next two pages (a contents page only names it; a title page can
    stand alone); failing that, the first title page past the front section. Then the 6 most
    business-heavy of its next 14 pages, since it opens with the economy."""
    titled = [i for i, p in enumerate(pages) if i > 1 and _MDA.search(p) and not _TOC.search(p[:400])]
    start = next((i for i in titled if sum(len(_MDA_BODY.findall(pages[j])) for j in range(i, min(i + 3, len(pages)))) >= 2),
                 next((i for i in titled if i >= 10), None))
    if start is None:
        return []
    window = range(start, min(start + 14, len(pages)))
    return sorted(sorted(window, key=lambda i: -len(_BIZ.findall(pages[i])))[:6])


def segment_pages(pages: list[str]) -> list[int]:
    scored = sorted(((len(_SEGMENT.findall(p)) + 3 * bool(_SEGMENT_HEAD.search(p)), i) for i, p in enumerate(pages)),
                    reverse=True)
    return sorted(i for s, i in scored[:2] if s > 0)


def _join(pages: list[str], idx: list[int], cap: int) -> str | None:
    text = "\n\n".join(f"=== page {i + 1} ===\n{squeeze(pages[i])}" for i in idx)
    return text[:cap] or None


def extract(pages: list[str]) -> dict:
    f, m, s = front_pages(pages), mda_pages(pages), segment_pages(pages)
    return {"n_pages": len(pages), "pages": json.dumps({"front": [i + 1 for i in f], "mda": [i + 1 for i in m],
                                                        "segment": [i + 1 for i in s]}),
            "front": _join(pages, f, FRONT_CHARS), "mda": _join(pages, m, MDA_CHARS),
            "segment": _join(pages, s, SEGMENT_CHARS)}


# --- run ------------------------------------------------------------------------------------

def due(limit: int | None = None, only: list[str] | None = None) -> pd.DataFrame:
    """Companies whose newest BSE annual report (per their latest OK profile) has no row yet,
    or only a fetch error."""
    prof = sql_to_df(f"""
        SELECT DISTINCT ON (company_master_id) company_master_id, ticker, documents
          FROM {PROFILE_TABLE} WHERE status = 'ok'
         ORDER BY company_master_id, fetched_on DESC""")
    try:
        done = sql_to_df(f"SELECT company_master_id, report_url FROM {TABLE} WHERE status <> 'fetch_error'")
    except Exception:  # first run, table not created yet
        done = pd.DataFrame(columns=["company_master_id", "report_url"])
    seen = set(zip(done["company_master_id"], done["report_url"]))
    rows = []
    for r in prof.itertuples(index=False):
        rep = latest_report(r.documents)
        if rep and (r.company_master_id, rep[1]) not in seen:
            rows.append({"company_master_id": r.company_master_id, "ticker": r.ticker,
                         "report_label": rep[0], "report_url": rep[1]})
    out = pd.DataFrame(rows, columns=["company_master_id", "ticker", "report_label", "report_url"])
    if only:
        out = out[out["ticker"].isin(only)]
    return out.head(limit) if limit else out


def run(limit: int | None = None, only: list[str] | None = None) -> dict:
    ensure_table()
    todo = due(limit, only)
    counts = {"due": len(todo), "ok": 0, "no_text": 0, "fetch_error": 0}
    for r in todo.itertuples(index=False):
        _wait_out_pipeline_window()
        row = {"company_master_id": r.company_master_id, "report_url": r.report_url, "ticker": r.ticker,
               "report_label": r.report_label, "n_pages": None, "pages": None, "front": None, "mda": None,
               "segment": None, "error": None}
        try:
            pages = pdf_pages(fetch_document_bytes_with_archive_fallback(r.report_url, domain="bse"))
        except (DocumentFetchError, OSError, subprocess.TimeoutExpired) as exc:
            row.update(status="fetch_error", error=f"{type(exc).__name__}: {exc}"[:500])
            record_local_fallback_event(module="fundamentals.collectors.annual_report_text",
                                        fallback_type="annual_report_fetch_failed", source="bse",
                                        symbol=r.ticker, severity="warn", reason=row["error"])
        else:
            if sum(len(p.strip()) for p in pages) < MIN_TEXT_CHARS:
                row.update(status="no_text", n_pages=len(pages))
            else:
                row.update(status="ok", **extract(pages))
        upsert_to_db(pd.DataFrame([row]), TABLE, ["company_master_id", "report_url"])
        counts[row["status"]] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Store business-description pages from the latest annual reports.")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--only", nargs="*", help="only these tickers")
    a = ap.parse_args(argv)
    print(json.dumps(run(limit=a.limit, only=a.only)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
