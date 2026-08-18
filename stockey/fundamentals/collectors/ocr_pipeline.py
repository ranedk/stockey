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
import tempfile
import threading
import time
from pathlib import Path

import pandas as pd
import requests
from environs import Env
from psycopg2 import sql as psycopg2_sql

from fundamentals.collectors.events_store import RESULTS_TABLE, _ensure_events_schema
from fundamentals.collectors.security_master import UA
from utils.blob_store import metadata_to_row, put_text_blob
from utils.db import db_session, execute_db_operation, sql_to_df
from utils.exchange_rate_limiter import exchange_request_gate
from utils.fallback_telemetry import record_local_fallback_event
from utils.ocr.llm_ocr import ocr_page_with_local, render_pdf_pages
from utils.store import save_file_content

env = Env()
env.read_env()

SYNC_SOURCE_NAME = "fundamentals.collectors.ocr_pipeline"
STOCKEY_RUN_STATE: dict[str, object] = {}

BSE_ATTACHMENT_URL_TEMPLATE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/{attachment_name}"
BSE_PDF_HEADERS = {"User-Agent": UA, "Referer": "https://www.bseindia.com/", "Accept": "application/pdf,*/*"}
ICRA_PDF_HEADERS = {"User-Agent": UA, "Referer": "https://www.icra.in/Rating/AllRatingRationales"}

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
BACKLOG_NOT_CLEARING_THRESHOLD = DEFAULT_BATCH_LIMIT
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

OCR_COLUMN_TYPES = {
    "ocr_status": "TEXT",
    "source_pdf_s3_key": "TEXT",
    "source_pdf_sha256": "TEXT",
    "ocr_text_s3_key": "TEXT",
    "ocr_text_sha256": "TEXT",
    "ocr_text_chars": "BIGINT",
    "ocr_text_bytes": "BIGINT",
    "ocr_text_excerpt": "TEXT",
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
    document reference yet -- ICRA checked first since it's a direct, already-verified
    filing (a matched rating rationale); BSE's attachment covers everything else."""
    rationale_pdf_url = row.get("rationale_pdf_url")
    if rationale_pdf_url:
        return "icra", rationale_pdf_url
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
    headers = ICRA_PDF_HEADERS if domain == "icra" else BSE_PDF_HEADERS
    with exchange_request_gate(domain=domain):
        response = requests.get(url, headers=headers, timeout=60)
    if response.status_code != 200:
        raise DocumentFetchError(f"HTTP {response.status_code} fetching {url}")
    if not response.content:
        raise DocumentFetchError(f"empty response body fetching {url}")
    return response.content


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
        ORDER BY disclosure_date DESC NULLS LAST, load_ts ASC NULLS LAST
    """
    if limit:
        query += f" LIMIT {int(limit)}"
    return sql_to_df(query)


def count_pending_ocr_targets() -> int:
    """Cheap COUNT(*) mirroring load_pending_ocr_targets()'s own WHERE clause,
    unbounded by DEFAULT_BATCH_LIMIT -- used only to size the true remaining backlog
    for BACKLOG_NOT_CLEARING_THRESHOLD's own check below, not for selecting rows."""
    df = sql_to_df(
        """
        SELECT COUNT(*) AS n
        FROM fundamentals_events
        WHERE (ocr_status IS NULL OR ocr_status = 'pending')
          AND (attachment_name IS NOT NULL OR rationale_pdf_url IS NOT NULL)
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
    """Render every page of a PDF (already-downloaded bytes) and OCR each one through
    the local provider, joined into one document's worth of text. See
    PER_PAGE_OCR_TIMEOUT_SECONDS/MAX_DOCUMENT_OCR_SECONDS above for why each page runs
    with its own bounded timeout instead of one unbounded per-document call."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as handle:
        Path(handle.name).write_bytes(pdf_bytes)
        rendered_pages = render_pdf_pages(handle.name)

    texts: dict[int, str] = {}
    document_started = time.monotonic()
    for page_number, image in rendered_pages:
        if time.monotonic() - document_started >= MAX_DOCUMENT_OCR_SECONDS:
            raise OcrTimeoutError(
                f"document OCR exceeded MAX_DOCUMENT_OCR_SECONDS ({MAX_DOCUMENT_OCR_SECONDS}s) "
                f"with {len(texts)}/{len(rendered_pages)} pages done"
            )
        try:
            texts[page_number] = _run_with_timeout(ocr_page_with_local, (image,), timeout_seconds=PER_PAGE_OCR_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            raise OcrTimeoutError(f"page {page_number} OCR exceeded PER_PAGE_OCR_TIMEOUT_SECONDS ({PER_PAGE_OCR_TIMEOUT_SECONDS}s)") from exc

    return "\n\n".join(texts[page_number] for page_number in sorted(texts))


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

    pending = load_pending_ocr_targets(limit or DEFAULT_BATCH_LIMIT)
    if pending.empty:
        return {"ocred": 0, "failed": 0, "no_document": 0, "blocked": False, "time_budget_exceeded": False, "backlog_remaining": 0}

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
            pdf_bytes = fetch_document_bytes(url, domain=domain)
            ocr_text = ocr_pdf_bytes(pdf_bytes)
        except OcrTimeoutError as exc:
            consecutive_timeouts_by_domain[domain] = consecutive_timeouts_by_domain.get(domain, 0) + 1
            counts["failed"] += 1
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
        counts["ocred"] += 1
        _set_ocr_result(source=row["source"], news_id=row["news_id"], status="done", fields=fields)

    backlog_remaining = count_pending_ocr_targets()
    if backlog_remaining > BACKLOG_NOT_CLEARING_THRESHOLD:
        _record_fallback(
            "ocr_pipeline_backlog_not_clearing",
            source="ocr_pipeline",
            reason=(
                f"{backlog_remaining} rows still pending after this run -- more than BACKLOG_NOT_CLEARING_"
                f"THRESHOLD ({BACKLOG_NOT_CLEARING_THRESHOLD}), meaning even a full uninterrupted run at "
                "DEFAULT_BATCH_LIMIT wouldn't clear it. Not necessarily new/getting worse -- see metadata "
                "for this run's own throughput to judge the trend."
            ),
            error="backlog exceeds one run's own item cap",
            metadata={"backlog_remaining": backlog_remaining, "ocred_this_run": counts["ocred"], "failed_this_run": counts["failed"]},
        )

    return {**counts, "blocked": bool(blocked_domains), "time_budget_exceeded": time_budget_exceeded, "backlog_remaining": backlog_remaining}


def main() -> int:
    global STOCKEY_RUN_STATE
    result = run_ocr_pipeline()
    STOCKEY_RUN_STATE = {
        "source": SYNC_SOURCE_NAME,
        "ocred": result["ocred"],
        "rows_written": result["ocred"],
        "failed": result["failed"],
        "no_document": result["no_document"],
        "blocked": result["blocked"],
        "time_budget_exceeded": result["time_budget_exceeded"],
        "backlog_remaining": result["backlog_remaining"],
        "fallback_used": bool(result["failed"] or result["blocked"]),
        "state_advanced": result["ocred"] > 0,
        "status": "blocked" if result["blocked"] else "ok",
    }
    print(json.dumps(STOCKEY_RUN_STATE, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
