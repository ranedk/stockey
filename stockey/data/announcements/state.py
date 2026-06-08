from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence

import pandas as pd
from environs import Env
from sqlalchemy.exc import ProgrammingError

from .models import Announcement, ParsedReport
from advisory.prompt_registry import prompt_version as registry_prompt_version
from advisory.prompt_registry import response_schema_version
from utils.blob_store import text_blob_metadata
from utils.db import sql_to_df, upsert_to_db
from utils.store import get_file_content, save_file_content


DOCUMENT_TABLE = "announcement_pipeline_documents"
REPORT_TABLE = "announcement_pipeline_reports"

env = Env()
env.read_env()

POSTGRES_TEXT_MODE = env.str("ANNOUNCEMENT_POSTGRES_TEXT_MODE", "pointer").strip().lower()
POSTGRES_TEXT_EXCERPT_CHARS = env.int("ANNOUNCEMENT_POSTGRES_TEXT_EXCERPT_CHARS", 1200)
ANNOUNCEMENT_SUMMARY_PROMPT_ID = "announcement_summary"
ANNOUNCEMENT_SUMMARY_PROMPT_VERSION = registry_prompt_version(ANNOUNCEMENT_SUMMARY_PROMPT_ID)
ANNOUNCEMENT_SUMMARY_PROMPT_SCHEMA_VERSION = response_schema_version(ANNOUNCEMENT_SUMMARY_PROMPT_ID)
ANNOUNCEMENT_STRUCTURED_REPORT_PROMPT_ID = "announcement_structured_report"
ANNOUNCEMENT_STRUCTURED_REPORT_PROMPT_VERSION = registry_prompt_version(ANNOUNCEMENT_STRUCTURED_REPORT_PROMPT_ID)
ANNOUNCEMENT_STRUCTURED_REPORT_PROMPT_SCHEMA_VERSION = response_schema_version(ANNOUNCEMENT_STRUCTURED_REPORT_PROMPT_ID)
OCR_PDF_PAGE_PROMPT_ID = "ocr_pdf_page"
OCR_PDF_PAGE_PROMPT_VERSION = registry_prompt_version(OCR_PDF_PAGE_PROMPT_ID)
OCR_PDF_PAGE_PROMPT_SCHEMA_VERSION = response_schema_version(OCR_PDF_PAGE_PROMPT_ID)


def _inline_text_enabled() -> bool:
    return POSTGRES_TEXT_MODE in {"inline", "legacy", "full"}


def _excerpt(text: str | None) -> str | None:
    if not text:
        return None
    return text[: max(int(POSTGRES_TEXT_EXCERPT_CHARS), 0)]


def _text_metadata(prefix: str, key: str | None, text: str | None) -> Dict[str, object]:
    if not key or not text:
        return {
            f"{prefix}_sha256": None,
            f"{prefix}_chars": None,
            f"{prefix}_bytes": None,
            f"{prefix}_excerpt": _excerpt(text),
        }
    metadata = text_blob_metadata(text, key=key, excerpt_chars=POSTGRES_TEXT_EXCERPT_CHARS)
    return {
        f"{prefix}_sha256": metadata.sha256,
        f"{prefix}_chars": metadata.char_count,
        f"{prefix}_bytes": metadata.byte_count,
        f"{prefix}_excerpt": metadata.excerpt,
    }


def _postgres_text_value(
    *,
    new_text: str | None,
    existing_row: Dict,
    existing_text_column: str,
    s3_key: str | None,
) -> str | None:
    if _inline_text_enabled():
        return new_text or existing_row.get(existing_text_column)
    if s3_key:
        return None
    # Do not erase legacy rows before a migration has uploaded their text.
    return existing_row.get(existing_text_column)


def build_storage_prefix(announcement: Announcement) -> str:
    published = announcement.published_on.strftime("%Y/%m/%d")
    return (
        f"announcement-pipeline/{announcement.exchange.lower()}/"
        f"{announcement.ticker}/{published}/{announcement.unique_id}"
    )


def build_artifact_keys(announcement: Announcement) -> Dict[str, str]:
    prefix = build_storage_prefix(announcement)
    extension = "pdf"
    if announcement.attachment_name and "." in announcement.attachment_name:
        extension = announcement.attachment_name.rsplit(".", 1)[-1].lower()
    return {
        "prefix": prefix,
        "raw": f"{prefix}/raw_announcement.json",
        "pdf": f"{prefix}/source_document.{extension}",
        "ocr": f"{prefix}/ocr_first_3_pages.txt",
        "full_ocr": f"{prefix}/ocr_full_document.txt",
        "audio_transcript": f"{prefix}/audio_transcript.txt",
        "concise_summary": f"{prefix}/concise_summary.txt",
        "reports_prefix": f"{prefix}/parsed_reports",
    }


def get_existing_documents(unique_ids: Sequence[str]) -> Dict[str, Dict]:
    if not unique_ids:
        return {}
    query = f"""
        SELECT *
        FROM {DOCUMENT_TABLE}
        WHERE unique_id = ANY(%s)
    """
    try:
        df = sql_to_df(query, params=(list(unique_ids),))
    except ProgrammingError as exc:
        if 'relation "announcement_pipeline_documents" does not exist' in str(exc):
            return {}
        raise
    if df.empty:
        return {}
    rows = df.to_dict(orient="records")
    return {row["unique_id"]: row for row in rows}


def get_existing_reports(unique_ids: Sequence[str]) -> Dict[tuple[str, str], Dict]:
    if not unique_ids:
        return {}
    query = f"""
        SELECT *
        FROM {REPORT_TABLE}
        WHERE unique_id = ANY(%s)
    """
    try:
        df = sql_to_df(query, params=(list(unique_ids),))
    except ProgrammingError as exc:
        if 'relation "announcement_pipeline_reports" does not exist' in str(exc):
            return {}
        raise
    if df.empty:
        return {}
    rows = df.to_dict(orient="records")
    return {(row["unique_id"], row["report_name"]): row for row in rows}


def load_attachment_bytes(document_row: Dict) -> Optional[bytes]:
    key = document_row.get("pdf_s3_key")
    if not key:
        return None
    return get_file_content(key)


def load_ocr_text(document_row: Dict) -> str:
    if document_row.get("three_page_ocr_text"):
        return document_row["three_page_ocr_text"]
    key = document_row.get("ocr_s3_key")
    if not key:
        return ""
    return get_file_content(key).decode("utf-8")


def load_full_ocr_text(document_row: Dict) -> str:
    if document_row.get("full_ocr_text"):
        return document_row["full_ocr_text"]
    key = document_row.get("full_ocr_s3_key")
    if not key:
        return ""
    return get_file_content(key).decode("utf-8")


def load_audio_transcript_text(document_row: Dict) -> str:
    if document_row.get("audio_transcript_text"):
        return document_row["audio_transcript_text"]
    key = document_row.get("audio_transcript_s3_key")
    if not key:
        return ""
    return get_file_content(key).decode("utf-8")


def load_concise_summary_text(document_row: Dict) -> str:
    if document_row.get("concise_summary_text"):
        return document_row["concise_summary_text"]
    key = document_row.get("concise_summary_s3_key")
    if not key:
        return ""
    return get_file_content(key).decode("utf-8")


def save_announcement_artifacts(announcement: Announcement) -> Dict[str, str]:
    keys = build_artifact_keys(announcement)
    save_file_content(
        keys["raw"],
        json.dumps(
            {
                "unique_id": announcement.unique_id,
                "company_master_id": announcement.company_master_id,
                "exchange": announcement.exchange,
                "ticker": announcement.ticker,
                "company_name": announcement.company_name,
                "subject": announcement.subject,
                "text": announcement.text,
                "filed_under_category": announcement.filed_under_category,
                "exchange_category_id": announcement.exchange_category_id,
                "published_on": announcement.published_on.isoformat(),
                "exchange_published_on": announcement.exchange_published_on.isoformat(),
                "raw": announcement.raw,
            },
            ensure_ascii=True,
            default=str,
        ),
    )
    if announcement.attachment_bytes:
        save_file_content(keys["pdf"], announcement.attachment_bytes)
    if announcement.three_page_ocr_text:
        save_file_content(keys["ocr"], announcement.three_page_ocr_text)
    if announcement.full_ocr_text:
        save_file_content(keys["full_ocr"], announcement.full_ocr_text)
    if announcement.audio_transcript_text:
        save_file_content(keys["audio_transcript"], announcement.audio_transcript_text)
    if announcement.concise_summary_text:
        save_file_content(keys["concise_summary"], announcement.concise_summary_text)
    return keys


def save_report_artifact(announcement: Announcement, report: ParsedReport) -> str:
    keys = build_artifact_keys(announcement)
    key = f"{keys['reports_prefix']}/{report.report_name}.json"
    save_file_content(key, json.dumps(report.data, ensure_ascii=True, default=str))
    return key


def upsert_documents(rows: Iterable[Dict]) -> None:
    data = list(rows)
    if not data:
        return
    df = pd.DataFrame(data)
    upsert_to_db(df, DOCUMENT_TABLE, ["unique_id"])


def upsert_reports(rows: Iterable[Dict]) -> None:
    data = list(rows)
    if not data:
        return
    df = pd.DataFrame(data)
    if {"unique_id", "report_name"}.issubset(df.columns):
        df["unique_id"] = df["unique_id"].astype("string")
        df["report_name"] = df["report_name"].astype("string")
        df = df.dropna(subset=["unique_id", "report_name"])
        df = df.drop_duplicates(subset=["unique_id", "report_name"], keep="last")
    if df.empty:
        return
    upsert_to_db(df, REPORT_TABLE, ["unique_id", "report_name"])


def document_row_from_announcement(
    announcement: Announcement,
    *,
    storage_keys: Optional[Dict[str, str]] = None,
    existing_row: Optional[Dict] = None,
    pdf_status: Optional[str] = None,
    ocr_status: Optional[str] = None,
    parse_status: Optional[str] = None,
    error: Optional[str] = None,
) -> Dict:
    now = datetime.now(timezone.utc)
    storage_keys = storage_keys or build_artifact_keys(announcement)
    existing_row = existing_row or {}
    ocr_s3_key = existing_row.get("ocr_s3_key") or (storage_keys["ocr"] if announcement.three_page_ocr_text else None)
    full_ocr_s3_key = existing_row.get("full_ocr_s3_key") or (storage_keys["full_ocr"] if announcement.full_ocr_text else None)
    audio_transcript_s3_key = existing_row.get("audio_transcript_s3_key") or (
        storage_keys["audio_transcript"] if announcement.audio_transcript_text else None
    )
    concise_summary_s3_key = existing_row.get("concise_summary_s3_key") or (
        storage_keys["concise_summary"] if announcement.concise_summary_text else None
    )
    return {
        "unique_id": announcement.unique_id,
        "company_master_id": announcement.company_master_id,
        "exchange": announcement.exchange,
        "ticker": announcement.ticker,
        "company_name": announcement.company_name,
        "subject": announcement.subject,
        "text": announcement.text,
        "filed_under_category": announcement.filed_under_category,
        "exchange_category_id": announcement.exchange_category_id,
        "published_on": announcement.published_on,
        "exchange_published_on": announcement.exchange_published_on,
        "attachment_url": announcement.attachment_url,
        "attachment_name": announcement.attachment_name,
        "attachment_content_type": announcement.attachment_content_type,
        "audio_attachment_url": announcement.audio_attachment_url,
        "audio_attachment_name": announcement.audio_attachment_name,
        "storage_prefix": storage_keys["prefix"],
        "raw_s3_key": storage_keys["raw"],
        "pdf_s3_key": existing_row.get("pdf_s3_key")
        or (storage_keys["pdf"] if announcement.attachment_bytes else None),
        "ocr_s3_key": ocr_s3_key,
        "full_ocr_s3_key": full_ocr_s3_key,
        "audio_transcript_s3_key": audio_transcript_s3_key,
        "concise_summary_s3_key": concise_summary_s3_key,
        "number_of_pages": announcement.number_of_pages,
        "three_page_ocr_text": _postgres_text_value(
            new_text=announcement.three_page_ocr_text,
            existing_row=existing_row,
            existing_text_column="three_page_ocr_text",
            s3_key=ocr_s3_key,
        ),
        "full_ocr_text": _postgres_text_value(
            new_text=announcement.full_ocr_text,
            existing_row=existing_row,
            existing_text_column="full_ocr_text",
            s3_key=full_ocr_s3_key,
        ),
        "audio_transcript_text": _postgres_text_value(
            new_text=announcement.audio_transcript_text,
            existing_row=existing_row,
            existing_text_column="audio_transcript_text",
            s3_key=audio_transcript_s3_key,
        ),
        "concise_summary_text": announcement.concise_summary_text
        or existing_row.get("concise_summary_text"),
        **_text_metadata("ocr", ocr_s3_key, announcement.three_page_ocr_text or existing_row.get("three_page_ocr_text")),
        **_text_metadata("full_ocr", full_ocr_s3_key, announcement.full_ocr_text or existing_row.get("full_ocr_text")),
        **_text_metadata(
            "audio_transcript",
            audio_transcript_s3_key,
            announcement.audio_transcript_text or existing_row.get("audio_transcript_text"),
        ),
        **_text_metadata(
            "concise_summary",
            concise_summary_s3_key,
            announcement.concise_summary_text or existing_row.get("concise_summary_text"),
        ),
        "ocr_model_name": announcement.ocr_model_name or existing_row.get("ocr_model_name"),
        "ocr_prompt_id": announcement.ocr_prompt_id or existing_row.get("ocr_prompt_id") or (OCR_PDF_PAGE_PROMPT_ID if announcement.three_page_ocr_text or announcement.full_ocr_text else None),
        "ocr_prompt_version": announcement.ocr_prompt_version or existing_row.get("ocr_prompt_version") or (OCR_PDF_PAGE_PROMPT_VERSION if announcement.three_page_ocr_text or announcement.full_ocr_text else None),
        "ocr_prompt_schema_version": announcement.ocr_prompt_schema_version
        or existing_row.get("ocr_prompt_schema_version")
        or (OCR_PDF_PAGE_PROMPT_SCHEMA_VERSION if announcement.three_page_ocr_text or announcement.full_ocr_text else None),
        "concise_summary_model_name": announcement.concise_summary_model_name or existing_row.get("concise_summary_model_name"),
        "concise_summary_prompt_id": announcement.concise_summary_prompt_id
        or existing_row.get("concise_summary_prompt_id")
        or (ANNOUNCEMENT_SUMMARY_PROMPT_ID if announcement.concise_summary_text else None),
        "concise_summary_prompt_version": announcement.concise_summary_prompt_version
        or existing_row.get("concise_summary_prompt_version")
        or (ANNOUNCEMENT_SUMMARY_PROMPT_VERSION if announcement.concise_summary_text else None),
        "concise_summary_prompt_schema_version": announcement.concise_summary_prompt_schema_version
        or existing_row.get("concise_summary_prompt_schema_version")
        or (ANNOUNCEMENT_SUMMARY_PROMPT_SCHEMA_VERSION if announcement.concise_summary_text else None),
        "categories_json": json.dumps(announcement.categories),
        "parsed_reports_json": json.dumps(
            [
                {
                    "category": report.category,
                    "report_name": report.report_name,
                    "model_name": report.model_name,
                    "prompt_id": report.prompt_id,
                    "prompt_version": report.prompt_version,
                    "prompt_schema_version": report.prompt_schema_version,
                    "data": report.data,
                }
                for report in announcement.parsed_reports
            ],
            ensure_ascii=True,
            default=str,
        )
        if announcement.parsed_reports
        else existing_row.get("parsed_reports_json"),
        "pdf_status": pdf_status
        or existing_row.get("pdf_status")
        or ("completed" if announcement.attachment_bytes else "pending"),
        "ocr_status": ocr_status
        or existing_row.get("ocr_status")
        or ("completed" if announcement.three_page_ocr_text else "pending"),
        "parse_status": parse_status or existing_row.get("parse_status") or "pending",
        "last_error": error,
        "created_at": existing_row.get("created_at") or now,
        "updated_at": now,
    }


def report_rows_from_announcement(announcement: Announcement) -> List[Dict]:
    now = datetime.now(timezone.utc)
    rows: List[Dict] = []
    for report in announcement.parsed_reports:
        report_s3_key = save_report_artifact(announcement, report)
        report_json = json.dumps(report.data, ensure_ascii=True, default=str)
        report_metadata = text_blob_metadata(
            report_json,
            key=report_s3_key,
            excerpt_chars=POSTGRES_TEXT_EXCERPT_CHARS,
        )
        rows.append(
            {
                "unique_id": announcement.unique_id,
                "company_master_id": announcement.company_master_id,
                "exchange": announcement.exchange,
                "ticker": announcement.ticker,
                "company_name": announcement.company_name,
                "published_on": announcement.published_on,
                "category": report.category,
                "report_name": report.report_name,
                "model_name": report.model_name,
                "prompt_id": report.prompt_id or ANNOUNCEMENT_STRUCTURED_REPORT_PROMPT_ID,
                "prompt_version": report.prompt_version or ANNOUNCEMENT_STRUCTURED_REPORT_PROMPT_VERSION,
                "prompt_schema_version": report.prompt_schema_version or ANNOUNCEMENT_STRUCTURED_REPORT_PROMPT_SCHEMA_VERSION,
                "report_json": report_json if _inline_text_enabled() else None,
                "report_s3_key": report_s3_key,
                "report_sha256": report_metadata.sha256,
                "report_chars": report_metadata.char_count,
                "report_bytes": report_metadata.byte_count,
                "report_excerpt": report_metadata.excerpt,
                "created_at": now,
                "updated_at": now,
            }
        )
    return rows
