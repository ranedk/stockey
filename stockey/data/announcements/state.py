from __future__ import annotations

import json
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence

import pandas as pd
from sqlalchemy.exc import ProgrammingError

from .models import Announcement, ParsedReport
from utils.db import sql_to_df, upsert_to_db
from utils.store import get_file_content, save_file_content


DOCUMENT_TABLE = "announcement_pipeline_documents"
REPORT_TABLE = "announcement_pipeline_reports"


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


def load_pdf_bytes(document_row: Dict) -> Optional[bytes]:
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
    if announcement.pdf_bytes:
        save_file_content(keys["pdf"], announcement.pdf_bytes)
    if announcement.three_page_ocr_text:
        save_file_content(keys["ocr"], announcement.three_page_ocr_text)
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
    now = datetime.utcnow()
    storage_keys = storage_keys or build_artifact_keys(announcement)
    existing_row = existing_row or {}
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
        "storage_prefix": storage_keys["prefix"],
        "raw_s3_key": storage_keys["raw"],
        "pdf_s3_key": existing_row.get("pdf_s3_key") or (storage_keys["pdf"] if announcement.pdf_bytes else None),
        "ocr_s3_key": existing_row.get("ocr_s3_key") or (storage_keys["ocr"] if announcement.three_page_ocr_text else None),
        "number_of_pages": announcement.number_of_pages,
        "three_page_ocr_text": announcement.three_page_ocr_text or existing_row.get("three_page_ocr_text"),
        "categories_json": json.dumps(announcement.categories),
        "pdf_status": pdf_status or existing_row.get("pdf_status") or ("completed" if announcement.pdf_bytes else "pending"),
        "ocr_status": ocr_status or existing_row.get("ocr_status") or ("completed" if announcement.three_page_ocr_text else "pending"),
        "parse_status": parse_status or existing_row.get("parse_status") or "pending",
        "last_error": error,
        "created_at": existing_row.get("created_at") or now,
        "updated_at": now,
    }


def report_rows_from_announcement(announcement: Announcement) -> List[Dict]:
    now = datetime.utcnow()
    rows: List[Dict] = []
    for report in announcement.parsed_reports:
        report_s3_key = save_report_artifact(announcement, report)
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
                "report_json": json.dumps(report.data, ensure_ascii=True, default=str),
                "report_s3_key": report_s3_key,
                "created_at": now,
                "updated_at": now,
            }
        )
    return rows
