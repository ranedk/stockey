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
from pathlib import Path

import pandas as pd
import requests
from psycopg2 import sql as psycopg2_sql

from fundamentals.collectors.events_store import RESULTS_TABLE, _ensure_events_schema
from fundamentals.collectors.security_master import UA
from utils.blob_store import metadata_to_row, put_text_blob
from utils.db import db_session, execute_db_operation, sql_to_df
from utils.exchange_rate_limiter import exchange_request_gate
from utils.fallback_telemetry import record_local_fallback_event
from utils.ocr.llm_ocr import ocr_pdf_with_local
from utils.store import save_file_content

SYNC_SOURCE_NAME = "fundamentals.collectors.ocr_pipeline"
STOCKEY_RUN_STATE: dict[str, object] = {}

BSE_ATTACHMENT_URL_TEMPLATE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/{attachment_name}"
BSE_PDF_HEADERS = {"User-Agent": UA, "Referer": "https://www.bseindia.com/", "Accept": "application/pdf,*/*"}
ICRA_PDF_HEADERS = {"User-Agent": UA, "Referer": "https://www.icra.in/Rating/AllRatingRationales"}

CIRCUIT_BREAKER_THRESHOLD = 3
# A daily batch is bounded by L3's own scope (only pit_sast/rating_action/results
# filings ever reach fundamentals_events at all -- see bse_announcements.py's
# docstring), not by anything in this module; still capped defensively so one run
# can't runaway-OCR an unexpectedly large backlog on a slow CPU-only machine
# (confirmed live: ~95-290s/page).
DEFAULT_BATCH_LIMIT = 50

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
    query = """
        SELECT source, news_id, company_master_id, filing_type, attachment_name, rationale_pdf_url
        FROM fundamentals_events
        WHERE (ocr_status IS NULL OR ocr_status = 'pending')
          AND (attachment_name IS NOT NULL OR rationale_pdf_url IS NOT NULL)
        ORDER BY load_ts ASC NULLS LAST
    """
    if limit:
        query += f" LIMIT {int(limit)}"
    return sql_to_df(query)


def ocr_pdf_bytes(pdf_bytes: bytes) -> str:
    """Render every page of a PDF (already-downloaded bytes) and OCR each one through
    the local provider, joined into one document's worth of text."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as handle:
        Path(handle.name).write_bytes(pdf_bytes)
        pages = ocr_pdf_with_local(handle.name)
    return "\n\n".join(pages[page_number] for page_number in sorted(pages))


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
        return {"ocred": 0, "failed": 0, "no_document": 0, "blocked": False}

    counts = {"ocred": 0, "failed": 0, "no_document": 0}
    consecutive_failures_by_domain: dict[str, int] = {}
    blocked_domains: set[str] = set()

    for _, row in pending.iterrows():
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
        pdf_key = f"fundamentals/filings/{domain}/{row['news_id']}.pdf"
        save_file_content(pdf_key, pdf_bytes)
        text_key = f"fundamentals/ocr/{domain}/{row['news_id']}.txt"
        text_metadata = put_text_blob(ocr_text, key=text_key)

        fields = metadata_to_row("ocr_text", text_metadata)
        fields["source_pdf_s3_key"] = pdf_key
        fields["source_pdf_sha256"] = hashlib.sha256(pdf_bytes).hexdigest()
        counts["ocred"] += 1
        _set_ocr_result(source=row["source"], news_id=row["news_id"], status="done", fields=fields)

    return {**counts, "blocked": bool(blocked_domains)}


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
        "fallback_used": bool(result["failed"] or result["blocked"]),
        "state_advanced": result["ocred"] > 0,
        "status": "blocked" if result["blocked"] else "ok",
    }
    print(json.dumps(STOCKEY_RUN_STATE, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
