"""OCR pipeline -- fundamental screener step 6, fetch+OCR half
(docs/FUNDAMENTAL_SCREENER_PRD.md sec 6). Wires the local GLM-OCR provider
(utils/ocr/llm_ocr.py's ocr_pdf_with_local) into the filing references L3 has already
produced: BSE announcement attachments (fundamentals/collectors/bse_announcements.py)
and ICRA rationale PDFs (fundamentals/collectors/rating_agencies.py).

Two document sources, both PDFs, both confirmed live 2026-08-10:

- BSE: `https://www.bseindia.com/xml-data/corpfiling/AttachLive/<attachment_name>` --
  attachment_name is already stored on every BSE-sourced fundamentals_events row.
  Rate-gated through the existing bse-domain exchange_request_gate.
- ICRA: rationale_pdf_url, already stored by rating_agencies.py's enrichment step
  (`https://www.icra.in/Rating/GetRationalReportFilePdf?Id=<id>`). Rate-gated through
  the existing icra-domain exchange_request_gate.

Only rows with a resolvable document reference are targeted -- everything else stays
ocr_status="no_document" (visible, not silently skipped). This is inherently a subset
of L3's detected events: NSE PIT rows are already fully structured (no PDF at all,
see nse_pit.py) and correctly never appear here; rows still awaiting rating-agency
enrichment (fundamentals_events.enrichment_status="pending"/"no_match"/
"unsupported_agency") have no rationale_pdf_url yet either.

Storage split per the source PRD: numeric/classification fields (once step 6's second
stage -- structured extraction -- lands) go to Postgres; the raw OCR'd text and the
source PDF itself go to S3 via utils/blob_store.py (already built for this exact
"S3 key + short excerpt in Postgres" pattern by the old advisory-era pipeline this
document referenced as precedent, `announcement_pipeline_documents`) and
utils/store.py. Structured extraction (OCR text -> typed fields via gpt-5.4-mini) is
NOT this module -- "GLM-OCR's own output is the input to structured extraction, not
an alternative to it" (source PRD sec 6); this module produces exactly that input.

Block-safety: same circuit-breaker discipline as every other L3/enrichment collector
this session built (explicit user instruction 2026-08-10) -- consecutive fetch
failures against either source stop that source's batch immediately.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import requests
from environs import Env
from pdf2image import pdfinfo_from_path
from psycopg2 import sql as psycopg2_sql

from fundamentals.collectors.events_store import RESULTS_TABLE, _ensure_events_schema
from fundamentals.collectors.security_master import UA
from utils.blob_store import metadata_to_row, put_text_blob
from utils.db import db_session, execute_db_operation, sql_to_df
from utils.schema_migrations import apply_schema_migration
from utils.exchange_rate_limiter import exchange_request_gate
from utils.fallback_telemetry import record_local_fallback_event
from utils.ocr.llm_ocr import ocr_page_with_local, render_pdf_pages
from utils.poppler import resolve_poppler_path
from utils.store import save_file_content

env = Env()
env.read_env()

SYNC_SOURCE_NAME = "fundamentals.collectors.ocr_pipeline"
STOCKEY_RUN_STATE: dict[str, object] = {}

BSE_ATTACHMENT_URL_TEMPLATE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/{attachment_name}"
BSE_PDF_HEADERS = {"User-Agent": UA, "Referer": "https://www.bseindia.com/", "Accept": "application/pdf,*/*"}
ICRA_PDF_HEADERS = {"User-Agent": UA, "Referer": "https://www.icra.in/Rating/AllRatingRationales"}
# Mirrors rating_agencies.py's own CARE_HEADERS (no Referer -- CARE's PDF endpoint
# doesn't require one, confirmed live 2026-08-12 there).
CARE_PDF_HEADERS = {"User-Agent": UA}
# Hostname -> domain label for resolve_document_source()'s rationale_pdf_url branch.
# BUG FOUND LIVE 2026-08-18 (re-audit): rationale_pdf_url is written by BOTH icra.in
# and careratings.com fetches (rating_agencies.py's ICRA_PDF_URL_TEMPLATE and
# CARE_PDF_BASE_URL both land in this one column), but resolve_document_source()
# hardcoded every non-null rationale_pdf_url as domain "icra" regardless of which
# agency actually issued it. That meant CARE traffic shared ICRA's rate-limiter gate
# and circuit-breaker counter (a run of CARE failures would falsely trip "ICRA is
# blocking us"), used ICRA's Referer header against a different host, and got
# mislabeled in telemetry/archival. Now derived from the URL's own hostname instead
# of assumed.
RATIONALE_PDF_HOST_DOMAINS = {"www.icra.in": "icra", "icra.in": "icra", "www.careratings.com": "care", "careratings.com": "care"}

CIRCUIT_BREAKER_THRESHOLD = 3
# 2026-08-15: raised 50 -> 200 (user request: "pace up so we don't have backlog" --
# confirmed live the real eligible backlog was 1060 documents across all filing types,
# and at 50/day this pipeline was barely keeping pace with new arrivals, not closing
# the gap). Still capped defensively so one run can't runaway-OCR an unexpectedly
# large backlog on a slow CPU-only machine (confirmed live: ~95-290s/page, and
# per-item cost is highly variable -- one observed run took 8.1 hours for 50 items,
# another took 7 minutes for the same count) -- MAX_RUNTIME_SECONDS below is the real
# safety net now, this limit is a sanity ceiling underneath it.
DEFAULT_BATCH_LIMIT = 200
# BUG FOUND LIVE 2026-08-17: DEFAULT_BATCH_LIMIT/MAX_RUNTIME_SECONDS bound one run's
# damage, but nothing ever checked whether the backlog was actually shrinking --
# at worst-case documented per-item latency, MAX_RUNTIME_SECONDS's 3h budget binds
# LONG before DEFAULT_BATCH_LIMIT's 200-item cap does (only ~18-37 items complete at
# the observed 289-583s/page worst case), silently undercutting the "pace up so we
# don't have backlog" intent this limit was raised for in the first place. Not fixed
# by raising either number here -- that's a real CPU-cost/pipeline-completion-time
# tradeoff, not something to guess at unilaterally. Instead, run_ocr_pipeline()
# records a distinct ocr_pipeline_backlog_not_clearing fallback event whenever the
# TRUE remaining backlog (count_pending_ocr_targets(), unbounded by this limit)
# still exceeds it after a run -- i.e. even a full, uninterrupted run at the item cap
# wouldn't have been enough to clear it -- so this stays a monitorable, visible
# trend rather than a one-time audit finding nobody re-checks.
#
# BUG FOUND LIVE 2026-08-18 (re-audit): this check used to compare against a
# module-level BACKLOG_NOT_CLEARING_THRESHOLD constant pinned to DEFAULT_BATCH_LIMIT,
# not the CALLER's actual effective limit -- harmless today since main() is the only
# caller and always uses the default, but wrong the moment a smaller-limit caller
# exists. run_ocr_pipeline() now compares against its own effective_limit directly
# instead of a separate constant that could drift out of sync with it.
# Wall-clock budget for one run, independent of item count -- protects the REST of
# that day's fundamentals pipeline (L1/L2/L3/watchlist/notifications all run after
# this step in run_pipeline.py's STEPS) from a bad day of large scanned documents
# consuming the entire run. Any row not reached before the cutoff stays pending and
# is picked up next run -- safe, idempotent, no partial/lost work.
MAX_RUNTIME_SECONDS = env.int("FUNDAMENTALS_OCR_MAX_RUNTIME_SECONDS", 3 * 60 * 60)
# BUG FOUND LIVE 2026-08-17: MAX_RUNTIME_SECONDS above is only checked BETWEEN rows
# in run_ocr_pipeline's loop -- a single document's OCR (ocr_pdf_bytes, page-by-page
# below) had no internal bound at all, so one large multi-page document, or one page
# the local model got stuck on, could consume the entire run's wall-clock budget (and
# run well past it) before the next between-row check ever got a chance to fire.
# Two bounds now apply within a single document:
# - PER_PAGE_OCR_TIMEOUT_SECONDS: each page's OCR call runs in a worker thread with a
#   hard result() timeout -- generous relative to the observed 95-290s/page so normal
#   pages are unaffected, but a genuinely stuck page no longer blocks the row forever.
#   The stuck worker thread itself can't be killed (transformers' generate() is a
#   blocking call), so it's abandoned (executor.shutdown(wait=False)) rather than
#   joined -- it still holds CPU until its own max_new_tokens bound lets it finish,
#   but the pipeline moves on instead of waiting on it.
# - MAX_DOCUMENT_OCR_SECONDS: checked between pages, bounds a many-page document's
#   TOTAL OCR time even when every individual page finishes within its own timeout.
PER_PAGE_OCR_TIMEOUT_SECONDS = env.int("FUNDAMENTALS_OCR_PAGE_TIMEOUT_SECONDS", 900)
# BUG FOUND LIVE 2026-08-18 (re-audit): 1800s was tight enough to permanently fail a
# real, legitimate document -- a census of the 52 documents already OCR'd
# successfully in production found a median of 7 pages but up to 31, and one real
# completed production run measured 73.2s/page overall; at the module's own more
# pessimistic documented range (95-290s/page), 1800s allows only ~6-19 pages,
# meaning roughly half of the ALREADY-SUCCEEDED real documents would now hard-fail
# on a slow day. Raised to comfortably cover the observed max (31 pages) even at a
# rate well above the real measured 73.2s/page (7200/31 =~ 232s/page), while still
# capping any single document at 2/3 of MAX_RUNTIME_SECONDS's 3h run budget -- large
# enough for real documents, still meaningfully short of "one document eats the
# whole run."
MAX_DOCUMENT_OCR_SECONDS = env.int("FUNDAMENTALS_OCR_MAX_DOCUMENT_SECONDS", 7200)

# BUG FOUND LIVE 2026-08-18 (re-audit): render_pdf_pages(path) with the default
# pages="all" calls pdf2image's convert_from_path with no page range, which rasterizes
# EVERY page into an in-memory PIL Image up front, before ocr_pdf_bytes even starts its
# MAX_DOCUMENT_OCR_SECONDS timer -- a large scanned filing can OOM the process before
# any of this module's bounds get a chance to apply. Live: the largest stored filing
# (24 pages) peaked at 988MB RSS; a 100-page attachment extrapolates to ~3GB in-process
# alongside the loaded OCR model. Capped well above the observed real max (31 pages,
# see MAX_DOCUMENT_OCR_SECONDS's own comment) -- ocr_pdf_bytes() below only takes the
# eager all-at-once render path when the document is at or under this cap; above it, it
# passes an explicit page range, which routes render_pdf_pages through its per-page loop
# (one page rasterized at a time, bounded peak memory) and truncates rather than OOMing.
MAX_OCR_PAGES = env.int("FUNDAMENTALS_OCR_MAX_PAGES", 60)

# Text layer first (2026-09-25). Most BSE filings are digital PDFs, or scans that already
# carry a text layer; `pdftotext` reads a page in milliseconds against 95-290s for the
# local model. Checked on 120 already-OCR'd filings (644 pages): where a page has a real
# text layer, pdftotext recovers 99% (median) of the words the model found. A page goes
# to the model only if its text layer is missing or looks like a poor scanner OCR:
# - under TEXT_LAYER_MIN_CHARS non-space characters (blank / image-only page), or
# - under TEXT_LAYER_MIN_DICT_RATIO of its 4+-letter words are English words
#   (/usr/share/dict), or
# - at least TEXT_LAYER_MAX_JUNK_RATIO of its tokens mix letters and symbols
#   ("#.4{,1,*,ffi", a scanner's OCR of a letterhead).
# On the sample that sends ~15% of pages to the model and catches 16 of the 19 pages
# whose text layer disagreed with the model's reading (2 of the other 3 were clean text
# the model misread).
TEXT_LAYER_MIN_CHARS = env.int("FUNDAMENTALS_OCR_TEXT_LAYER_MIN_CHARS", 200)
TEXT_LAYER_MIN_DICT_RATIO = 0.70
TEXT_LAYER_MAX_JUNK_RATIO = 0.15
TEXT_LAYER_TRUST_CHARS = 500
TRUSTED_MIN_DICT_RATIO = 0.60
TRUSTED_MAX_JUNK_RATIO = 0.30
PAGE_IMAGE_MIN_PIXELS = 50_000  # anything smaller is a logo or stamp, not a scanned page
DICTIONARY_PATH = Path("/usr/share/dict/american-english")
_CLEAN_TOKEN_RE = re.compile(
    r"^[\(\[\"']?([A-Za-z]+([\-'.][A-Za-z]+)*|[\d,.\-/%()₹:]+|[A-Za-z]+\d*|\d+[A-Za-z]{0,3})[\)\]\"'.,;:]*$"
)
_dictionary: set[str] | None = None


def _english_words() -> set[str]:
    global _dictionary
    if _dictionary is None:
        try:
            _dictionary = {w.strip().lower() for w in DICTIONARY_PATH.read_text(errors="ignore").splitlines()}
        except OSError:
            _record_fallback("ocr_text_layer_no_dictionary", source="ocr_pipeline",
                             reason=f"{DICTIONARY_PATH} missing; the text-layer quality check uses the junk-token test only.",
                             error="dictionary missing", severity="warn")
            _dictionary = set()
    return _dictionary


def _dictionary_ratio(text: str) -> float:
    words = _english_words()
    long_words = [w.lower() for w in re.findall(r"[A-Za-z]{4,}", text)]
    if not words or not long_words:
        return 1.0 if not words else 0.0
    return sum(w in words for w in long_words) / len(long_words)


def _junk_ratio(text: str) -> float:
    tokens = [t for t in re.split(r"\s+", text) if len(t) >= 2]
    return sum(1 for t in tokens if not _CLEAN_TOKEN_RE.match(t)) / len(tokens) if tokens else 1.0


def text_layer_is_usable(text: str, *, has_image: bool = True) -> bool:
    """Whether a page's own text layer can be used instead of the OCR model (2026-09-28,
    operator: "if we get 500+ characters, OCR only on solid indication the text can't be
    read"). Measured on 150 filings the model had read: 26% fewer model pages than the
    first version, trusted pages matching the model's reading at a median 98% of words.
      - under TEXT_LAYER_MIN_CHARS: the model only if the page has an image (a scan);
        otherwise it is a near-blank page (separator, signature block) -- keep its text.
      - TEXT_LAYER_TRUST_CHARS or more: trusted unless there is solid evidence it is
        unreadable -- broken-encoding characters, under TRUSTED_MIN_DICT_RATIO English
        words, or TRUSTED_MAX_JUNK_RATIO junk tokens (a scanner's poor OCR layer).
      - in between: the stricter checks (TEXT_LAYER_MIN_DICT_RATIO / MAX_JUNK_RATIO)."""
    chars = len(re.sub(r"\s", "", text))
    if chars < TEXT_LAYER_MIN_CHARS:
        return not has_image
    if chars >= TEXT_LAYER_TRUST_CHARS:
        if text.count("\ufffd") / chars >= 0.01:
            return False
        return _dictionary_ratio(text) >= TRUSTED_MIN_DICT_RATIO and _junk_ratio(text) < TRUSTED_MAX_JUNK_RATIO
    return _junk_ratio(text) < TEXT_LAYER_MAX_JUNK_RATIO and _dictionary_ratio(text) >= TEXT_LAYER_MIN_DICT_RATIO


def page_image_sizes(pdf_path: str) -> dict[int, int]:
    """page number -> pixel area of its largest embedded image (pdfimages -list)."""
    try:
        out = subprocess.run(["pdfimages", "-list", pdf_path], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    sizes: dict[int, int] = {}
    for line in out.stdout.splitlines()[2:]:
        parts = line.split()
        if len(parts) > 4 and parts[0].isdigit() and parts[3].isdigit() and parts[4].isdigit():
            page = int(parts[0])
            sizes[page] = max(sizes.get(page, 0), int(parts[3]) * int(parts[4]))
    return sizes


def text_layer_pages(pdf_path: str) -> list[str]:
    """One string per page from the PDF's own text layer (pdftotext, layout kept so
    result tables stay in columns). Empty list if pdftotext fails."""
    try:
        out = subprocess.run(["pdftotext", "-layout", pdf_path, "-"], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if out.returncode != 0:
        return []
    pages = out.stdout.split("\f")
    if pages and pages[-1] == "":
        pages = pages[:-1]
    return pages


OCR_COLUMN_TYPES = {
    "ocr_status": "TEXT",
    "source_pdf_s3_key": "TEXT",
    "source_pdf_sha256": "TEXT",
    "ocr_text_s3_key": "TEXT",
    "ocr_text_sha256": "TEXT",
    "ocr_text_chars": "BIGINT",
    "ocr_text_bytes": "BIGINT",
    "ocr_text_excerpt": "TEXT",
    "ocr_pages_total": "INTEGER",
    "ocr_pages_model": "INTEGER",
}


def _record_fallback(fallback_type: str, *, source: str, reason: str, error, severity: str = "warn", metadata=None) -> None:
    record_local_fallback_event(
        module=SYNC_SOURCE_NAME,
        source=source,
        fallback_type=fallback_type,
        severity=severity,
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def _bootstrap_ocr_columns() -> None:
    """Same idempotent-ALTER pattern as events_store._ensure_events_schema and
    rating_agencies._bootstrap_rating_columns, for this module's own columns."""

    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = %s", (RESULTS_TABLE,))
            if cur.fetchone() is None:
                return
            for column, pg_type in OCR_COLUMN_TYPES.items():
                cur.execute(
                    psycopg2_sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} {}").format(
                        psycopg2_sql.Identifier(RESULTS_TABLE), psycopg2_sql.Identifier(column), psycopg2_sql.SQL(pg_type)
                    )
                )

    execute_db_operation(_op, operation_name="fundamentals_events:ensure_ocr_columns")


def resolve_document_source(row: dict) -> tuple[str, str] | None:
    """(domain, url) for this row's source filing, or None if it has no resolvable
    document reference yet -- rationale_pdf_url checked first since it's a direct,
    already-verified filing (a matched rating rationale); BSE's attachment covers
    everything else. domain is derived from rationale_pdf_url's own hostname, not
    assumed to be ICRA -- see RATIONALE_PDF_HOST_DOMAINS' comment."""
    rationale_pdf_url = row.get("rationale_pdf_url")
    if rationale_pdf_url:
        hostname = urlparse(rationale_pdf_url).hostname or ""
        domain = RATIONALE_PDF_HOST_DOMAINS.get(hostname.lower(), "rating_agency_other")
        return domain, rationale_pdf_url
    attachment_name = row.get("attachment_name")
    if attachment_name:
        return "bse", BSE_ATTACHMENT_URL_TEMPLATE.format(attachment_name=attachment_name)
    return None


class DocumentFetchError(RuntimeError):
    """Raised when a source PDF can't be fetched -- caught per-row so the caller can
    count it towards that source's circuit breaker rather than crashing the run."""


class OcrTimeoutError(RuntimeError):
    """Raised when a single page's OCR exceeds PER_PAGE_OCR_TIMEOUT_SECONDS, or a
    document's total page-by-page OCR exceeds MAX_DOCUMENT_OCR_SECONDS.

    BUG FOUND LIVE 2026-08-18 (re-audit): used to be caught by the exact same
    except block as DocumentFetchError -- conflating "our CPU was too slow on a
    genuinely large document" with "the source is blocking us" (fetch failures).
    That meant a slow-but-legitimate document permanently failed the row (no
    automatic retry path exists) AND counted toward the same-domain circuit
    breaker, so a run of large documents could trip a false "bse/icra is blocking
    us" block for the REST of that day's batch. Now handled by its own except
    branch in run_ocr_pipeline() with its own, separate circuit-breaker counter --
    it's a signal about OUR pipeline's pace, not the source's health, and the row
    is left pending (not marked permanently failed) so it gets a fresh attempt on
    a future, hopefully-less-contended run rather than being given up on forever."""


def fetch_document_bytes(url: str, *, domain: str) -> bytes:
    if domain == "icra":
        headers = ICRA_PDF_HEADERS
    elif domain == "care":
        headers = CARE_PDF_HEADERS
    else:
        headers = BSE_PDF_HEADERS
    with exchange_request_gate(domain=domain):
        response = requests.get(url, headers=headers, timeout=60)
    if response.status_code != 200:
        raise DocumentFetchError(f"HTTP {response.status_code} fetching {url}")
    if not response.content:
        raise DocumentFetchError(f"empty response body fetching {url}")
    return response.content


BSE_ARCHIVE_URL_PREFIX = "https://www.bseindia.com/xml-data/corpfiling/AttachHis/"
BSE_LIVE_URL_PREFIX = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"


def fetch_document_bytes_with_archive_fallback(url: str, *, domain: str) -> bytes:
    """BSE moves older filings from AttachLive/ to AttachHis/. Only AttachLive was ever
    tried, so every older PDF 404'd and was marked failed PERMANENTLY -- 995 of the
    3-year backfill in its first week (2026-09-23 audit; confirmed by hand: a
    2024-12-30 filing 404s on AttachLive and returns a 982 KB PDF from AttachHis)."""
    try:
        return fetch_document_bytes(url, domain=domain)
    except DocumentFetchError as exc:
        if domain != "bse" or "HTTP 404" not in str(exc) or not url.startswith(BSE_LIVE_URL_PREFIX):
            raise
        return fetch_document_bytes(BSE_ARCHIVE_URL_PREFIX + url[len(BSE_LIVE_URL_PREFIX):], domain=domain)


MAX_OCR_TIMEOUTS = env.int("FUNDAMENTALS_OCR_MAX_TIMEOUTS", 3)


def ensure_ocr_timeout_column() -> None:
    apply_schema_migration(
        migration_id="20260923_fundamentals_events_ocr_timeouts",
        description="fundamentals_events: ocr_timeouts counter.",
        owner=SYNC_SOURCE_NAME,
        metadata={"tables": ["fundamentals_events"]},
        statements=["ALTER TABLE fundamentals_events ADD COLUMN IF NOT EXISTS ocr_timeouts INTEGER"],
    )


def _record_ocr_timeout(*, source: str, news_id: str) -> None:
    """Count a timeout; after MAX_OCR_TIMEOUTS the row stops retrying as
    'timeout_exhausted' -- visible, and treated like a failure downstream."""
    def _update() -> None:
        with db_session() as (_, cur):
            cur.execute(
                "UPDATE fundamentals_events SET ocr_timeouts = COALESCE(ocr_timeouts, 0) + 1, "
                "  ocr_status = CASE WHEN COALESCE(ocr_timeouts, 0) + 1 >= %s THEN 'timeout_exhausted' ELSE ocr_status END "
                " WHERE source = %s AND news_id = %s",
                (MAX_OCR_TIMEOUTS, source, news_id),
            )

    execute_db_operation(_update, operation_name="fundamentals_events:ocr_timeout")


def readmit_bse_attachlive_failures() -> None:
    """One-time: put BSE rows that failed before the AttachHis fallback existed back in
    the queue. The backlog drains under this module's own per-run limit and time budget,
    newest filings first, so this cannot turn one run into a thousand fetches.

    ~414 rows: results, ratings, auditor changes, capital raises, RPTs. The 584 failed
    pit_sast rows are deliberately NOT re-admitted: they are mostly routine trading-window
    notices, NSE's structured feed already carries recent insider trades, and at ~30
    OCR'd documents a day they would hold OCR at its 3-hour ceiling for weeks. Re-admit
    them separately if insider history before the NSE feed is ever needed."""
    apply_schema_migration(
        migration_id="20260923_fundamentals_events_readmit_bse_attachlive_ocr_failures",
        description="fundamentals_events: BSE ocr_status=failed -> NULL, retried with the AttachHis fallback.",
        owner=SYNC_SOURCE_NAME,
        metadata={"tables": ["fundamentals_events"]},
        statements=[
            "UPDATE fundamentals_events SET ocr_status = NULL "
            " WHERE source = 'bse' AND ocr_status = 'failed' AND attachment_name IS NOT NULL"
            "   AND filing_type IN ('results', 'rating_action', 'auditor_change', 'capital_raise',"
            "                       'related_party_transaction')",
        ],
    )


def readmit_bse_sast_failures() -> None:
    """One-time (2026-09-24): re-admit the failed BSE pit_sast rows that are SAST
    disclosures -- Reg 29 substantial acquisitions, Reg 31 promoter pledges/encumbrance,
    Reg 10 exemptions. The note above assumed NSE's structured feed covers these; it does
    not: nse_pit carries PIT insider trades only (and only since 2026-08-17), so for SAST
    the BSE PDF is the only source, and l3_triggers' pledge rules read it. ~190 rows, all
    NSE-listed companies, nearly all on the active watchlist. Trading-window closures and
    code-of-conduct notices (~390) stay out: they carry no holding information."""
    apply_schema_migration(
        migration_id="20260924_fundamentals_events_readmit_bse_sast_ocr_failures",
        description="fundamentals_events: BSE pit_sast SAST disclosures ocr_status=failed -> NULL.",
        owner=SYNC_SOURCE_NAME,
        metadata={"tables": ["fundamentals_events"]},
        statements=[
            "UPDATE fundamentals_events SET ocr_status = NULL "
            " WHERE source = 'bse' AND ocr_status = 'failed' AND attachment_name IS NOT NULL"
            "   AND filing_type = 'pit_sast' AND subcategory LIKE 'Disclosures under Reg.%%SAST%%'",
        ],
    )


# Operator, 2026-09-30: no OCR for an announcement older than this. Nothing downstream reads an
# old filing's text -- history is marked 'historical' and never alerts, the story read and the
# catch-up look back 3 and 60 days -- and 3,500 of 4,100 queued documents were 2 months to 3
# years old. Older rows get the visible terminal status below, never a silent skip.
OCR_MAX_AGE_DAYS = env.int("FUNDAMENTALS_OCR_MAX_AGE_DAYS", 183)
SKIPPED_TOO_OLD = "skipped_too_old"
# A malformed disclosure_date must not make the cast throw and stop OCR, and a row with no
# date at all counts as too old rather than waiting unqueued forever (review 2026-10-02).
_FILING_DATE_SQL = ("COALESCE(CASE WHEN disclosure_date ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}' "
                    "THEN substr(disclosure_date, 1, 10)::date END, "
                    "(announcement_timestamp AT TIME ZONE 'Asia/Kolkata')::date, DATE '1900-01-01')")
_IST_TODAY_SQL = "(now() AT TIME ZONE 'Asia/Kolkata')::date"


def mark_too_old_skipped() -> int:
    """Close out queued documents older than OCR_MAX_AGE_DAYS. Returns how many."""
    def _op() -> int:
        with db_session() as (_, cur):
            cur.execute(
                f"""
                UPDATE fundamentals_events SET ocr_status = %s
                 WHERE (ocr_status IS NULL OR ocr_status = 'pending')
                   AND (attachment_name IS NOT NULL OR rationale_pdf_url IS NOT NULL)
                   AND {_FILING_DATE_SQL} < {_IST_TODAY_SQL} - %s
                """,
                (SKIPPED_TOO_OLD, OCR_MAX_AGE_DAYS),
            )
            return cur.rowcount

    return int(execute_db_operation(_op, operation_name=f"{RESULTS_TABLE}:ocr_skip_too_old") or 0)


def load_pending_ocr_targets(limit: int | None = None) -> pd.DataFrame:
    # BUG FOUND LIVE 2026-08-17: plain load_ts ASC processes strictly oldest-inserted-
    # first, with no regard for how old the FILING itself is. bse_announcements.py's
    # 3yr auditor/RPT backfill (fixed the same day -- see BACKFILL_FILING_TYPES there)
    # had queued 1,074 off-target rows up to 3 years old ahead of every fresh daily-
    # crawl detection in this FIFO order; live-tested 9 of the oldest and 8 were
    # already-404 (BSE doesn't retain AttachLive documents indefinitely). A contiguous
    # run of those would trip CIRCUIT_BREAKER_THRESHOLD before any fresh, still-
    # fetchable row behind them was ever attempted -- silently starving genuinely
    # timely detections every single run. disclosure_date DESC prioritizes the
    # freshest filings first (also the ones most likely to still have a live
    # attachment); load_ts ASC is only the tiebreaker within same-day filings now,
    # not the primary order.
    query = """
        SELECT source, news_id, company_master_id, filing_type, attachment_name, rationale_pdf_url
        FROM fundamentals_events
        WHERE (ocr_status IS NULL OR ocr_status = 'pending')
          AND (attachment_name IS NOT NULL OR rationale_pdf_url IS NOT NULL)
          AND """ + _FILING_DATE_SQL + " >= " + _IST_TODAY_SQL + """ - """ + str(int(OCR_MAX_AGE_DAYS)) + """
        -- Documents that already timed out go to the BACK (2026-09-23 audit): they used
        -- to lead every run in the same order, tripping the timeout breaker before any
        -- fresh filing was reached.
        ORDER BY COALESCE(ocr_timeouts, 0) ASC, disclosure_date DESC NULLS LAST, load_ts ASC NULLS LAST
    """
    if limit:
        query += f" LIMIT {int(limit)}"
    return sql_to_df(query)


def count_pending_ocr_targets() -> int:
    """Cheap COUNT(*) mirroring load_pending_ocr_targets()'s own WHERE clause,
    unbounded by DEFAULT_BATCH_LIMIT -- used only to size the true remaining backlog
    for run_ocr_pipeline()'s own backlog-not-clearing check, not for selecting rows."""
    df = sql_to_df(
        """
        SELECT COUNT(*) AS n
        FROM fundamentals_events
        WHERE (ocr_status IS NULL OR ocr_status = 'pending')
          AND (attachment_name IS NOT NULL OR rationale_pdf_url IS NOT NULL)
          AND """ + _FILING_DATE_SQL + " >= " + _IST_TODAY_SQL + """ - """ + str(int(OCR_MAX_AGE_DAYS)) + """
        """
    )
    return int(df.iloc[0]["n"]) if not df.empty else 0


def _run_with_timeout(fn, args, *, timeout_seconds: float):
    """Runs fn(*args) with a hard wall-clock timeout, raising TimeoutError if it
    doesn't finish in time.

    BUG FOUND LIVE 2026-08-18 (re-audit): the previous implementation used
    concurrent.futures.ThreadPoolExecutor, whose worker threads are created
    WITHOUT daemon=True -- Python's own threading._register_atexit hook joins
    every one of them at interpreter shutdown regardless of executor.shutdown
    (wait=False). Since run_pipeline.py runs every fundamentals step IN-PROCESS
    (importlib + module.main(), not a subprocess), a timed-out OCR page's
    abandoned worker thread would still be joined at the very end of that whole
    day's run -- competing for CPU with every later step in the meantime, and
    delaying the orchestrator process's own exit by however long the stuck
    generate() call takes to naturally finish. Live-reproduced: a thread
    "abandoned" via executor.shutdown(wait=False) still added its full runtime to
    total wall-clock time at process exit. A plain daemon thread has no such
    hook -- the OS kills it outright when the process exits, so an abandoned OCR
    call can genuinely never block anything downstream from finishing."""
    result_box: dict[str, object] = {}

    def _target():
        try:
            result_box["value"] = fn(*args)
        except Exception as exc:  # noqa: BLE001 -- surfaced to the caller via result_box, not swallowed
            result_box["error"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)
    if thread.is_alive():
        raise TimeoutError(f"{getattr(fn, '__name__', fn)} exceeded {timeout_seconds}s")
    if "error" in result_box:
        raise result_box["error"]
    return result_box["value"]


def ocr_pdf_bytes(pdf_bytes: bytes) -> str:
    return extract_pdf_text(pdf_bytes)[0]


def extract_pdf_text(pdf_bytes: bytes, *, max_pages: int | None = None,
                     max_document_seconds: int | None = None) -> tuple[str, dict[str, int]]:
    """One document's text: each page from its own text layer when that layer is usable
    (see TEXT_LAYER_MIN_CHARS), otherwise through the local OCR model. Returns the text
    and {pages_total, pages_model}. The model-side bounds are unchanged:
    PER_PAGE_OCR_TIMEOUT_SECONDS per page, MAX_DOCUMENT_OCR_SECONDS per document, and at
    most MAX_OCR_PAGES pages rendered for the model (text-layer pages are not capped).
    `max_pages` reads only the first N pages (research documents need pages 1-2)."""
    document_started = time.monotonic()
    doc_budget = max_document_seconds or MAX_DOCUMENT_OCR_SECONDS
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as handle:
        Path(handle.name).write_bytes(pdf_bytes)
        layer = text_layer_pages(handle.name)
        if max_pages:
            layer = layer[:max_pages]
        total_pages = len(layer) or None
        if total_pages is None:
            try:
                total_pages = pdfinfo_from_path(handle.name, poppler_path=resolve_poppler_path()).get("Pages")
            except Exception:  # noqa: BLE001 -- unknown page count: model on every page it renders
                total_pages = None

        texts: dict[int, str] = {}
        model_pages: list[int] = []
        if layer:
            images = page_image_sizes(handle.name)
            for number, page_text in enumerate(layer, start=1):
                if text_layer_is_usable(page_text, has_image=images.get(number, 0) >= PAGE_IMAGE_MIN_PIXELS):
                    texts[number] = page_text
                else:
                    model_pages.append(number)

        if not layer and max_pages:
            last = min(max_pages, int(total_pages)) if total_pages else max_pages
            rendered_pages = render_pdf_pages(handle.name, pages=list(range(1, last + 1)))
        elif not layer:
            if total_pages is not None and total_pages > MAX_OCR_PAGES:
                _record_truncation(total_pages)
                rendered_pages = render_pdf_pages(handle.name, pages=list(range(1, MAX_OCR_PAGES + 1)))
            else:
                rendered_pages = render_pdf_pages(handle.name)
        elif model_pages:
            if len(model_pages) > MAX_OCR_PAGES:
                _record_truncation(len(model_pages))
                model_pages = model_pages[:MAX_OCR_PAGES]
            rendered_pages = render_pdf_pages(handle.name, pages=model_pages)
        else:
            rendered_pages = []

    for page_number, image in rendered_pages:
        if time.monotonic() - document_started >= doc_budget:
            raise OcrTimeoutError(
                f"document OCR exceeded MAX_DOCUMENT_OCR_SECONDS ({doc_budget}s) "
                f"with {len(texts)} pages done"
            )
        try:
            texts[page_number] = _run_with_timeout(ocr_page_with_local, (image,), timeout_seconds=PER_PAGE_OCR_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            raise OcrTimeoutError(f"page {page_number} OCR exceeded PER_PAGE_OCR_TIMEOUT_SECONDS ({PER_PAGE_OCR_TIMEOUT_SECONDS}s)") from exc

    stats = {"pages_total": int(total_pages or len(texts)), "pages_model": len(rendered_pages)}
    return "\n\n".join(texts[n] for n in sorted(texts)), stats


def _record_truncation(pages: int) -> None:
    _record_fallback(
        "ocr_pipeline_document_truncated",
        source="ocr_pipeline",
        reason=(f"{pages} pages need the OCR model, over MAX_OCR_PAGES ({MAX_OCR_PAGES}) -- OCR'ing only the "
                "first MAX_OCR_PAGES rather than rasterizing them all into memory."),
        error="page count exceeds MAX_OCR_PAGES",
        severity="warn",
        metadata={"pages": pages, "max_ocr_pages": MAX_OCR_PAGES},
    )


def _set_ocr_result(*, source: str, news_id: str, status: str, fields: dict | None = None) -> None:
    fields = fields or {}
    set_columns = ["ocr_status = %s"] + [f"{col} = %s" for col in fields]
    params = [status, *fields.values(), source, news_id]

    def _update() -> None:
        with db_session() as (_, cur):
            cur.execute(
                f"UPDATE fundamentals_events SET {', '.join(set_columns)} WHERE source = %s AND news_id = %s",  # noqa: S608 -- column names are our own fixed constants, never user input
                params,
            )

    execute_db_operation(_update, operation_name="fundamentals_events:ocr_update")


def run_ocr_pipeline(*, limit: int | None = None) -> dict[str, object]:
    _ensure_events_schema()
    _bootstrap_ocr_columns()

    skipped_too_old = mark_too_old_skipped()
    effective_limit = limit or DEFAULT_BATCH_LIMIT
    pending = load_pending_ocr_targets(effective_limit)
    if pending.empty:
        return {"ocred": 0, "failed": 0, "no_document": 0, "blocked": False, "time_budget_exceeded": False,
                "backlog_remaining": 0, "skipped_too_old": skipped_too_old}

    counts = {"ocred": 0, "failed": 0, "no_document": 0}
    consecutive_failures_by_domain: dict[str, int] = {}
    # BUG FOUND LIVE 2026-08-18 (re-audit): a document-level OCR timeout used to
    # share consecutive_failures_by_domain with genuine fetch failures -- a run of
    # large-but-legitimate documents could trip a false "this domain is blocking
    # us" circuit-breaker block. Separate counter: a timeout means "our pipeline
    # is slow today," not "the source is unreachable."
    consecutive_timeouts_by_domain: dict[str, int] = {}
    blocked_domains: set[str] = set()
    run_started = time.monotonic()
    time_budget_exceeded = False

    for _, row in pending.iterrows():
        if time.monotonic() - run_started >= MAX_RUNTIME_SECONDS:
            time_budget_exceeded = True
            break

        source_info = resolve_document_source(row.to_dict())
        if source_info is None:
            counts["no_document"] += 1
            _set_ocr_result(source=row["source"], news_id=row["news_id"], status="no_document")
            continue

        domain, url = source_info
        if domain in blocked_domains:
            continue

        try:
            pdf_bytes = fetch_document_bytes_with_archive_fallback(url, domain=domain)
            ocr_text, page_stats = extract_pdf_text(pdf_bytes)
        except OcrTimeoutError as exc:
            consecutive_timeouts_by_domain[domain] = consecutive_timeouts_by_domain.get(domain, 0) + 1
            counts["failed"] += 1
            _record_ocr_timeout(source=row["source"], news_id=row["news_id"])
            # Deliberately NOT _set_ocr_result(status="failed") here -- see
            # OcrTimeoutError's own docstring for why a timeout stays retryable
            # (ocr_status untouched, so load_pending_ocr_targets re-selects it on
            # a future run) rather than being given up on permanently.
            _record_fallback(
                "ocr_pipeline_document_timeout",
                source=domain,
                reason="OCR'ing this row's source document exceeded its page/document timeout; left ocr_status pending (not permanently failed) so it retries on a future, hopefully-less-contended run.",
                error=exc,
                metadata={"news_id": row["news_id"], "url": url},
            )
            if consecutive_timeouts_by_domain[domain] >= CIRCUIT_BREAKER_THRESHOLD:
                blocked_domains.add(domain)
                _record_fallback(
                    "ocr_pipeline_timeout_circuit_breaker_tripped",
                    source=domain,
                    reason=f"{consecutive_timeouts_by_domain[domain]} consecutive {domain} document timeouts -- stopping this source's batch for the rest of this run (not a source-health signal, just pacing).",
                    error="circuit breaker",
                    severity="warn",
                )
            continue
        except Exception as exc:  # noqa: BLE001 -- classified as a failure either way
            consecutive_failures_by_domain[domain] = consecutive_failures_by_domain.get(domain, 0) + 1
            counts["failed"] += 1
            _set_ocr_result(source=row["source"], news_id=row["news_id"], status="failed")
            _record_fallback(
                "ocr_pipeline_document_failed",
                source=domain,
                reason="Fetching or OCR'ing this row's source document failed; it stays ocr_status=failed permanently -- the pending-selection query only re-selects NULL/pending, so this needs a manual UPDATE to retry, not an automatic one.",
                error=exc,
                metadata={"news_id": row["news_id"], "url": url},
            )
            if consecutive_failures_by_domain[domain] >= CIRCUIT_BREAKER_THRESHOLD:
                blocked_domains.add(domain)
                _record_fallback(
                    "ocr_pipeline_circuit_breaker_tripped",
                    source=domain,
                    reason=f"{consecutive_failures_by_domain[domain]} consecutive {domain} document failures -- stopping this source's batch immediately.",
                    error="circuit breaker",
                    severity="error",
                )
            continue

        consecutive_failures_by_domain[domain] = 0
        consecutive_timeouts_by_domain[domain] = 0
        pdf_key = f"fundamentals/filings/{domain}/{row['news_id']}.pdf"
        save_file_content(pdf_key, pdf_bytes)
        text_key = f"fundamentals/ocr/{domain}/{row['news_id']}.txt"
        text_metadata = put_text_blob(ocr_text, key=text_key)

        fields = metadata_to_row("ocr_text", text_metadata)
        fields["source_pdf_s3_key"] = pdf_key
        fields["source_pdf_sha256"] = hashlib.sha256(pdf_bytes).hexdigest()
        fields["ocr_pages_total"] = page_stats["pages_total"]
        fields["ocr_pages_model"] = page_stats["pages_model"]
        counts["ocred"] += 1
        _set_ocr_result(source=row["source"], news_id=row["news_id"], status="done", fields=fields)

    backlog_remaining = count_pending_ocr_targets()
    # BUG FOUND LIVE 2026-08-18 (re-audit): this used to compare against the module-
    # level BACKLOG_NOT_CLEARING_THRESHOLD (== DEFAULT_BATCH_LIMIT) regardless of what
    # limit this particular call actually used -- only main() calls this with no limit
    # today, so it happened to always match, but a smaller-limit caller would get a
    # telemetry reason text ("even a full run at DEFAULT_BATCH_LIMIT wouldn't clear
    # it") that's wrong for its own, smaller effective cap.
    if backlog_remaining > effective_limit:
        _record_fallback(
            "ocr_pipeline_backlog_not_clearing",
            source="ocr_pipeline",
            reason=(
                f"{backlog_remaining} rows still pending after this run -- more than this run's own "
                f"effective limit ({effective_limit}), meaning even a full uninterrupted run at that limit "
                "wouldn't clear it. Not necessarily new/getting worse -- see metadata for this run's own "
                "throughput to judge the trend."
            ),
            error="backlog exceeds one run's own item cap",
            metadata={"backlog_remaining": backlog_remaining, "effective_limit": effective_limit, "ocred_this_run": counts["ocred"], "failed_this_run": counts["failed"]},
        )

    return {**counts, "blocked": bool(blocked_domains), "time_budget_exceeded": time_budget_exceeded, "backlog_remaining": backlog_remaining,
            "skipped_too_old": skipped_too_old}


RESEARCH_TAIL_SECONDS = env.int("FUNDAMENTALS_OCR_RESEARCH_TAIL_SECONDS", 25 * 60)
RESEARCH_DOC_SECONDS = env.int("FUNDAMENTALS_OCR_RESEARCH_DOC_SECONDS", 15 * 60)


def run_research_tail(*, seconds: int = RESEARCH_TAIL_SECONDS) -> dict[str, int]:
    """Research documents (fundamentals/collectors/ocr_research_queue.py), ONLY while the
    daily queue is empty -- re-checked before every document, so a filing that arrives
    mid-tail waits for at most one research document (<= RESEARCH_DOC_SECONDS). Same
    process and the same cached model as the daily work: never a second copy in memory."""
    from fundamentals.collectors import ocr_research_queue as rq

    started = time.monotonic()
    done = failed = 0
    stopped_for_daily = False
    while time.monotonic() - started < seconds:
        if count_pending_ocr_targets() > 0:
            stopped_for_daily = True
            break
        item = rq.next_pending()
        if item is None:
            break
        try:
            text, stats = extract_pdf_text(Path(item["pdf_path"]).read_bytes(), max_pages=int(item["max_pages"]),
                                           max_document_seconds=RESEARCH_DOC_SECONDS)
            rq.mark(item["doc_id"], text=text, pages_model=stats["pages_model"])
            done += 1
        except Exception as exc:  # noqa: BLE001 -- a research document never fails the daily job
            rq.mark(item["doc_id"], error=f"{type(exc).__name__}: {exc}")
            failed += 1
    return {"research_ocred": done, "research_failed": failed, "research_stopped_for_daily": int(stopped_for_daily)}


def main() -> int:
    global STOCKEY_RUN_STATE
    ensure_ocr_timeout_column()
    readmit_bse_attachlive_failures()
    readmit_bse_sast_failures()
    result = run_ocr_pipeline()
    # research reads only after the daily work is finished and the budget allows it
    research = {}
    if result["backlog_remaining"] == 0 and not result["time_budget_exceeded"]:
        try:
            research = run_research_tail()
        except Exception as exc:  # noqa: BLE001 -- the daily result stands whatever happens here
            _record_fallback("ocr_research_tail_failed", source="ocr_research_queue",
                             reason="Research OCR tail raised; daily OCR results are unaffected.", error=exc)
    STOCKEY_RUN_STATE = {
        "source": SYNC_SOURCE_NAME,
        "ocred": result["ocred"],
        "rows_written": result["ocred"],
        "failed": result["failed"],
        "no_document": result["no_document"],
        "blocked": result["blocked"],
        "time_budget_exceeded": result["time_budget_exceeded"],
        "backlog_remaining": result["backlog_remaining"],
        "skipped_too_old": result.get("skipped_too_old", 0),
        "fallback_used": bool(result["failed"] or result["blocked"]),
        "state_advanced": result["ocred"] > 0,
        "status": "blocked" if result["blocked"] else "ok",
        **research,
    }
    print(json.dumps(STOCKEY_RUN_STATE, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
