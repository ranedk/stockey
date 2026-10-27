"""Revenue shares from annual reports, for tags the profile could not quantify
(docs/THEMATIC_WATCHLIST_PRD.md section 4.2, the candidate-only document step).

Only companies that already hold an "unquantified" tag are read -- a few hundred reports,
not the whole universe. Per company:
  1. download its latest annual report from BSE (shared bse rate gate) and take the PDF's
     own text layer, page by page (`pdftotext -layout`); a scanned report with no text layer
     is recorded as `no_text`, not OCR'd.
  2. pick the pages worth reading: the segment-reporting note (Ind AS 108) and the pages
     that mention the tagged themes' keywords most (usually the MD&A).
  3. the model names, per tagged theme, the reported segment (or MD&A figure) that is that
     theme and copies the two numbers AS PRINTED -- the theme's revenue and the total -- with
     the short fragment each sits in. Or it says the theme is not reported separately.
  4. in code: both numbers must be in the report text and in their fragments; the share is
     computed here (theme / total), never taken from the model.
  5. a second prompt argues that the segment is NOT the theme (a segment that only contains
     the theme alongside bigger businesses); a rejection marks the row "disputed".

Rows go to their own table, keyed by report, so the tag rows stay point-in-time to the
profile they read. The ranking reads a tag's own share first, then this table's.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
from datetime import datetime, timezone

import pandas as pd

from fundamentals.collectors.ocr_pipeline import DocumentFetchError, fetch_document_bytes_with_archive_fallback
from fundamentals.themes import catalog
from fundamentals.themes.tagger import MODEL, PROFILE_TABLE, TAG_TABLE, _norm, call_claude
from fundamentals.trigger_study.llm_runner import in_night_window
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.fallback_telemetry import record_local_fallback_event

TABLE = "fundamentals_theme_exposure_ar"
SEGMENT_PAGES = 3
THEME_PAGES = 3
PAGE_CHARS = 9000
MIN_TEXT_CHARS = 20000     # a 100+ page report with less text than this is a scan

_DDL = f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
        company_master_id TEXT NOT NULL,
        theme_version INT NOT NULL,
        theme_id TEXT NOT NULL,
        report_url TEXT NOT NULL,
        ticker TEXT NOT NULL,
        report_label TEXT,
        status TEXT NOT NULL,          -- quantified | not_separate | disputed | invalid | no_text | fetch_error
        basis TEXT,                    -- segment | mdna_figure | single_segment | not_separate
        segment_name TEXT,
        fiscal_year TEXT,
        unit TEXT,
        theme_revenue DOUBLE PRECISION,
        total_revenue DOUBLE PRECISION,
        exposure_pct DOUBLE PRECISION,
        theme_quote TEXT,
        total_quote TEXT,
        segment_quote TEXT,
        pages TEXT,
        check_reason TEXT,
        dropped TEXT,
        model TEXT,
        read_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (company_master_id, theme_version, theme_id, report_url)
    )"""


def ensure_table() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute(_DDL)

    execute_db_operation(_op, operation_name=f"{TABLE}:ensure_table")


# --- the report -----------------------------------------------------------------------------

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


_SEGMENT_RX = re.compile(r"segment revenue|segment result|reportable segment|operating segment|ind as 108"
                         r"|segment information|single (business|reportable|operating) segment|one segment", re.I)
_SEGMENT_HEAD_RX = re.compile(r"segment (reporting|information)", re.I)


def pick_pages(pages: list[str], keywords: list[str]) -> list[int]:
    """Indexes of the segment note's pages and the pages richest in the themes' keywords."""
    seg = sorted(((len(_SEGMENT_RX.findall(p)) + 3 * bool(_SEGMENT_HEAD_RX.search(p)), i)
                  for i, p in enumerate(pages)), reverse=True)
    picked = [i for s, i in seg[:SEGMENT_PAGES] if s > 0]
    kws = [re.compile(r"\b" + re.escape(k) + r"\b", re.I) for k in keywords if k.strip()]
    if kws:
        hits = sorted(((sum(len(k.findall(p)) for k in kws), i) for i, p in enumerate(pages)), reverse=True)
        picked += [i for s, i in hits if s > 0 and i not in picked][:THEME_PAGES]
    return sorted(picked)


def excerpt(pages: list[str], picked: list[int]) -> str:
    """The picked pages with layout padding squeezed: column gaps stay visible as two
    spaces, which is what keeps a table's numbers apart."""
    parts = []
    for i in picked:
        body = "\n".join(re.sub(r" {3,}", "  ", ln).rstrip() for ln in pages[i].splitlines() if ln.strip())
        parts.append(f"=== page {i + 1} ===\n{body[:PAGE_CHARS]}")
    return "\n\n".join(parts)


# --- the prompts ----------------------------------------------------------------------------

READ_PROMPT = """You read pages from an Indian listed company's annual report (text extracted from the PDF, columns may be interleaved) and find how much of its revenue comes from given investment themes.

For each theme in the input, decide:
- basis "segment": the company reports one or more segments (segment note, Ind AS 108) whose business IS this theme (mostly or entirely). List each such segment's revenue for the latest year (several when the theme spans segments, e.g. freight and passenger rail), and the company's total revenue for the same year.
- basis "mdna_figure": no segment matches, but the report states this theme's revenue in rupees elsewhere (e.g. "revenue from data centres was Rs 2,850 crore"). Copy it and the total revenue for the same year.
- basis "single_segment": the company says it has one segment AND that one business is this theme. theme_revenues holds the total revenue once, and total_revenue is the same total.
- basis "not_separate": the theme sits inside a larger segment with other businesses, or no number is given. Numbers null.

Rules:
- Copy numbers EXACTLY as printed (e.g. "3,65,820"), never compute, convert or estimate. Prefer consolidated figures; use revenue from operations / external revenue.
- theme_quotes (one per number in theme_revenues, same order) and total_quote: a short fragment (a few words plus the number) copied verbatim from ONE line of the text, containing that number.
- segment_quote: a short verbatim fragment naming or describing the segment or business that is the theme.
- unit: as printed (e.g. "Rs crore", "Rs million", "Rs lakh"). fiscal_year: e.g. "FY2025-26".
Return one item per theme id in the input."""

READ_SCHEMA = {
    "type": "object", "required": ["items"],
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "required": ["theme_id", "basis", "segment_name", "theme_revenues", "total_revenue", "unit", "fiscal_year",
                     "theme_quotes", "total_quote", "segment_quote"],
        "properties": {
            "theme_id": {"type": "string"},
            "basis": {"type": "string", "enum": ["segment", "mdna_figure", "single_segment", "not_separate"]},
            "segment_name": {"type": ["string", "null"]},
            "theme_revenues": {"type": "array", "items": {"type": "string"}},
            "total_revenue": {"type": ["string", "null"]},
            "unit": {"type": ["string", "null"]},
            "fiscal_year": {"type": ["string", "null"]},
            "theme_quotes": {"type": "array", "items": {"type": "string"}},
            "total_quote": {"type": ["string", "null"]},
            "segment_quote": {"type": ["string", "null"]},
        }}}},
}

CHECK_PROMPT = """You are a sceptical reviewer. Each item says an Indian listed company's reported segment (or stated revenue line) IS an investment theme, so that the segment's revenue share can be counted as the company's exposure to the theme.
Argue against it: does that segment also hold other, unrelated businesses big enough to overstate the exposure, or is it a different business that only sounds similar? Decide.
verdict "keep" if the segment's revenue is fairly the theme's revenue; "reject" if not. reason: one short sentence."""

CHECK_SCHEMA = {
    "type": "object", "required": ["items"],
    "properties": {"items": {"type": "array", "items": {
        "type": "object", "required": ["id", "verdict", "reason"],
        "properties": {"id": {"type": "string"}, "verdict": {"type": "string", "enum": ["keep", "reject"]},
                       "reason": {"type": "string"}}}}},
}


# --- validation (pure) ----------------------------------------------------------------------

_NUM_RX = re.compile(r"\d[\d,]*(?:\.\d+)?")


def parse_amount(s: str | None) -> float | None:
    """'3,65,820' -> 365820.0; '(1,234)' and anything that is not one plain number -> None."""
    if not s:
        return None
    m = _NUM_RX.fullmatch(s.strip().lstrip("₹").replace("Rs.", "").replace("Rs", "").strip())
    return float(m.group(0).replace(",", "")) if m else None


def _numbers(text: str) -> set[str]:
    return {x.replace(",", "") for x in _NUM_RX.findall(text)}


def _digits(raw: str) -> str:
    return raw.strip().lstrip("₹").replace("Rs.", "").replace("Rs", "").strip().replace(",", "")


def _words(s: str) -> list[str]:
    return re.findall(r"[a-z]{3,}", s.lower())


def grounded(quote: str, raw: str, text: str) -> bool:
    """The number is in the quote, and the quote is in the text -- verbatim, or (layout text
    wraps a sentence across interleaved columns) with most of its words within 300
    characters of one place the number is printed."""
    digits = _digits(raw)
    if not quote or digits not in _numbers(quote):
        return False
    if _norm(quote) in _norm(text):
        return True
    words = set(_words(quote))
    if not words:
        return False
    for m in _NUM_RX.finditer(text):
        if m.group(0).replace(",", "") == digits:
            near = set(_words(text[max(0, m.start() - 300):m.end() + 300]))
            if len(words & near) >= 0.7 * len(words):
                return True
    return False


def validate_read(item: dict, text: str) -> tuple[dict, str | None]:
    """The row's measured fields, and why a share was not accepted (None if it was)."""
    basis = item.get("basis")
    revs, quotes = list(item.get("theme_revenues") or []), list(item.get("theme_quotes") or [])
    out = {"basis": basis, "segment_name": item.get("segment_name"), "unit": item.get("unit"),
           "fiscal_year": item.get("fiscal_year"), "theme_quote": " | ".join(quotes) or None,
           "total_quote": item.get("total_quote"), "segment_quote": item.get("segment_quote"),
           "theme_revenue": None, "total_revenue": None, "exposure_pct": None}
    if basis == "not_separate":
        return {**out, "status": "not_separate"}, None
    seg_q = (item.get("segment_quote") or "").strip()
    if not seg_q or _norm(seg_q) not in _norm(text):
        return {**out, "status": "invalid"}, "segment quote not in the report text"
    if not revs or len(revs) != len(quotes):
        return {**out, "status": "invalid"}, "theme revenues missing or not one quote each"
    nums = _numbers(text)
    pairs = [("theme_revenue", r, q) for r, q in zip(revs, quotes)]
    pairs.append(("total_revenue", item.get("total_revenue"), item.get("total_quote")))
    theme, total = 0.0, None
    for key, raw, quote in pairs:
        v = parse_amount(raw)
        if v is None or v <= 0:
            return {**out, "status": "invalid"}, f"{key} is not a plain positive number"
        if _digits(raw) not in nums:
            return {**out, "status": "invalid"}, f"{key} not in the report text"
        if not grounded((quote or "").strip(), raw, text):
            return {**out, "status": "invalid"}, f"{key}'s quote not in the report text or lacks its number"
        if key == "total_revenue":
            total = v
        else:
            theme += v
    if theme > total * 1.001:
        return {**out, "status": "invalid"}, "theme revenue above the total"
    pct = round(100 * theme / total, 1)
    return {**out, "theme_revenue": theme, "total_revenue": total, "exposure_pct": pct, "status": "quantified"}, None


# --- run ------------------------------------------------------------------------------------

def due(version: int, limit: int | None, only: list[str] | None = None) -> pd.DataFrame:
    """One row per company: its unquantified, undisputed tags on its latest profile, and the
    latest annual report not yet read for those themes."""
    df = sql_to_df(f"""
        WITH latest AS (
            SELECT DISTINCT ON (company_master_id) company_master_id, fetched_on, ticker, documents
              FROM {PROFILE_TABLE} WHERE status = 'ok'
             ORDER BY company_master_id, fetched_on DESC)
        SELECT l.company_master_id, l.ticker, c.company_name, l.documents, t.theme_id
          FROM {TAG_TABLE} t
          JOIN latest l ON l.company_master_id = t.company_master_id AND l.fetched_on = t.profile_fetched_on
          JOIN company_master c ON c.company_master_id = t.company_master_id
         WHERE t.theme_version = %(v)s AND t.status = 'unquantified'""", params={"v": version})
    if df.empty:
        return df
    try:
        read = sql_to_df(f"SELECT company_master_id, theme_id, report_url FROM {TABLE} WHERE theme_version = %(v)s"
                         f" AND status <> 'fetch_error'", params={"v": version})
    except Exception:  # first run, table not created yet
        read = pd.DataFrame(columns=["company_master_id", "theme_id", "report_url"])
    seen = set(zip(read["company_master_id"], read["theme_id"], read["report_url"]))
    rows = []
    for cid, g in df.groupby("company_master_id", sort=False):
        first = g.iloc[0]
        rep = latest_report(first["documents"])
        if rep is None:
            continue
        themes = [t for t in g["theme_id"] if (cid, t, rep[1]) not in seen]
        if themes:
            rows.append({"company_master_id": cid, "ticker": first["ticker"], "company_name": first["company_name"],
                         "report_label": rep[0], "report_url": rep[1], "themes": themes})
    out = pd.DataFrame(rows)
    if only and not out.empty:
        out = out[out["ticker"].isin(only)]
    return out.head(limit) if limit else out


def _rows(r, version: int, themes: list[str], **fields) -> list[dict]:
    return [{"company_master_id": r.company_master_id, "theme_version": version, "theme_id": t,
             "report_url": r.report_url, "ticker": r.ticker, "report_label": r.report_label,
             "model": MODEL, **fields} for t in themes]


_COLUMNS = ["status", "basis", "segment_name", "fiscal_year", "unit", "theme_revenue", "total_revenue",
            "exposure_pct", "theme_quote", "total_quote", "segment_quote", "pages", "check_reason", "dropped"]


def _store(rows: list[dict]) -> None:
    if rows:
        df = pd.DataFrame(rows)
        for c in _COLUMNS:
            if c not in df:
                df[c] = None
        upsert_to_db(df, TABLE, ["company_master_id", "theme_version", "theme_id", "report_url"])


def read_company(r, version: int, by_id: dict[str, catalog.Theme]) -> tuple[list[dict], dict | None]:
    """Rows for one company, and the stop reason if the model is out of allowance."""
    try:
        pdf = fetch_document_bytes_with_archive_fallback(r.report_url, domain="bse")
    except (DocumentFetchError, OSError) as exc:
        record_local_fallback_event(module="fundamentals.themes.segment_reader", fallback_type="annual_report_fetch_failed",
                                    source="bse", symbol=r.ticker, severity="warn", reason=str(exc)[:500])
        return _rows(r, version, r.themes, status="fetch_error", dropped=str(exc)[:500], model=None), None
    pages = pdf_pages(pdf)
    if sum(len(p.strip()) for p in pages) < MIN_TEXT_CHARS:
        return _rows(r, version, r.themes, status="no_text", model=None), None
    keywords = [k for t in r.themes for k in by_id[t].include]
    picked = pick_pages(pages, keywords)
    text = excerpt(pages, picked)
    page_list = ",".join(str(i + 1) for i in picked)
    res = call_claude(READ_PROMPT, READ_SCHEMA, [{
        "company": r.company_name, "report": r.report_label,
        "themes": [{"theme_id": t, "definition": by_id[t].definition} for t in r.themes], "text": text}])
    if not res["ok"]:
        return [], res
    got = {str(x.get("theme_id")): x for x in res["items"]}
    rows, checks = [], []
    for t in r.themes:
        item = got.get(t)
        if item is None:
            rows += _rows(r, version, [t], status="invalid", dropped="no answer for this theme", pages=page_list)
            continue
        fields, dropped = validate_read(item, text)
        rows += _rows(r, version, [t], **fields, dropped=dropped, pages=page_list)
        if fields["status"] == "quantified" and fields["basis"] != "single_segment":
            checks.append({"id": str(len(rows) - 1), "company": r.company_name, "theme_id": t,
                           "definition": by_id[t].definition, "exclusions": list(by_id[t].exclude),
                           "segment": fields["segment_name"], "segment_text": fields["segment_quote"],
                           "share_pct": fields["exposure_pct"]})
        elif fields["status"] == "quantified":
            # one segment for the whole company: the tag's own check already judged the
            # company to be in the theme, the question here is only whether it is ALL of it
            checks.append({"id": str(len(rows) - 1), "company": r.company_name, "theme_id": t,
                           "definition": by_id[t].definition, "exclusions": list(by_id[t].exclude),
                           "segment": "the whole company (single reported segment)",
                           "segment_text": fields["segment_quote"], "share_pct": 100.0})
    if checks:
        chk = call_claude(CHECK_PROMPT, CHECK_SCHEMA, checks)
        if not chk["ok"]:
            return [], chk
        verdicts = {str(x.get("id")): x for x in chk["items"]}
        for c in checks:
            row, v = rows[int(c["id"])], verdicts.get(c["id"])
            if v is None:
                row["status"], row["check_reason"] = "disputed", "no verdict returned"
            elif v["verdict"] == "reject":
                row["status"], row["check_reason"] = "disputed", v["reason"]
            else:
                row["check_reason"] = v["reason"]
    return rows, None


def run(*, version: int = 1, limit: int | None = None, any_time: bool = False, only: list[str] | None = None) -> dict:
    if not any_time and not in_night_window(datetime.now(timezone.utc)):
        return {"skipped": "outside the 22:00-08:00 IST window (use --any-time)"}
    ensure_table()
    by_id = {t.id: t for t in catalog.load(version)}
    todo = due(version, limit, only)
    counts: dict = {"companies": 0}
    for r in todo.itertuples(index=False):
        rows, stop = read_company(r, version, by_id)
        if stop is not None:
            counts["stopped"] = f"{stop['kind']}: {stop['message'][:200]}"
            break
        _store(rows)
        counts["companies"] += 1
        for row in rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Read revenue shares for unquantified theme tags from annual reports.")
    ap.add_argument("--version", type=int, default=1)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--any-time", action="store_true", help="ignore the night window (pilots)")
    ap.add_argument("--only", nargs="*", help="only these tickers")
    a = ap.parse_args(argv)
    print(json.dumps(run(version=a.version, limit=a.limit, any_time=a.any_time, only=a.only), default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
