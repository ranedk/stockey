from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import List, Optional, Sequence

import pytz

from advisory.decision_trace import record_event_processing, safe_trace_call

from .db import load_company_master_targets
from .models import Announcement
from .pipeline import AnnouncementPipeline
from .state import (
    document_row_from_announcement,
    load_audio_transcript_text,
    get_existing_documents,
    get_existing_reports,
    load_attachment_bytes,
    load_concise_summary_text,
    load_full_ocr_text,
    load_ocr_text,
    report_rows_from_announcement,
    save_announcement_artifacts,
    upsert_documents,
    upsert_reports,
)
from utils.log import setup_logger

logger = setup_logger("announcement_pipeline.managed")


@dataclass(slots=True)
class IngestSummary:
    requested: int = 0
    discovered: int = 0
    downloaded: int = 0
    ocred: int = 0
    categorized: int = 0
    parsed: int = 0
    skipped: int = 0
    failed: int = 0


class ManagedAnnouncementPipeline:
    def __init__(self, pipeline: Optional[AnnouncementPipeline] = None) -> None:
        self.pipeline = pipeline or AnnouncementPipeline()

    def ingest_date_range(
        self,
        ticker: str,
        from_date: date,
        to_date: date,
        *,
        exchanges: Optional[Sequence[str]] = None,
        parse_reports: Optional[Sequence[str]] = None,
    ) -> IngestSummary:
        targets = list(load_company_master_targets(ticker=ticker, exchanges=exchanges))
        summary = IngestSummary(requested=len(targets))
        if not targets:
            raise ValueError(f"No company master mapping found for ticker: {ticker}")

        start_dt = self._date_start(from_date)
        end_dt = self._date_end(to_date)
        announcements = [
            item
            for item in self.pipeline.fetch_announcements(targets, since=start_dt)
            if start_dt <= item.published_on <= end_dt
        ]
        summary.discovered = len(announcements)

        existing_documents = get_existing_documents([item.unique_id for item in announcements])
        existing_reports = get_existing_reports([item.unique_id for item in announcements])

        for announcement in announcements:
            existing_document = existing_documents.get(announcement.unique_id, {})
            try:
                self._save_raw_metadata(announcement, existing_document)
                self._record_stage(
                    announcement,
                    "metadata",
                    "completed",
                    payload={
                        "exchange": announcement.exchange,
                        "attachment_url": announcement.attachment_url,
                        "existing_document": bool(existing_document),
                    },
                )
                self._hydrate_from_state(announcement, existing_document)
                self._record_stage(
                    announcement,
                    "hydrate",
                    "completed",
                    payload={
                        "has_three_page_ocr": bool(announcement.three_page_ocr_text),
                        "has_full_ocr": bool(announcement.full_ocr_text),
                        "has_summary": bool(announcement.concise_summary_text),
                        "category_count": len(announcement.categories or []),
                    },
                )

                if not announcement.three_page_ocr_text and announcement.attachment_url:
                    self._ensure_downloaded(announcement, existing_document)
                    summary.downloaded += 1
                    self._record_stage(
                        announcement,
                        "download",
                        "completed" if announcement.attachment_bytes else "skipped",
                        payload={
                            "attachment_url": announcement.attachment_url,
                            "content_type": announcement.attachment_content_type,
                            "bytes": len(announcement.attachment_bytes or b""),
                        },
                    )
                    self._persist_document(
                        announcement,
                        existing_document,
                        pdf_status="completed",
                    )

                if not announcement.three_page_ocr_text and announcement.attachment_bytes:
                    self.pipeline.ocr_first_pages([announcement])
                    if not announcement.ocr_error:
                        summary.ocred += 1
                    self._record_stage(
                        announcement,
                        "ocr_first_pages",
                        "failed" if announcement.ocr_error else "completed",
                        error=announcement.ocr_error,
                        payload={
                            "number_of_pages": announcement.number_of_pages,
                            "text_chars": len(announcement.three_page_ocr_text or ""),
                        },
                    )
                    self._persist_document(
                        announcement,
                        existing_document,
                        pdf_status="completed",
                        ocr_status="failed" if announcement.ocr_error else "completed",
                    )

                if announcement.combined_text and not announcement.categories:
                    self.pipeline.categorize([announcement])
                    summary.categorized += 1
                    self._record_stage(
                        announcement,
                        "categorize",
                        "completed",
                        output_payload=announcement.categories,
                        payload={
                            "category_count": len(announcement.categories or []),
                            "categories": announcement.categories or [],
                        },
                    )
                    self._persist_document(
                        announcement,
                        existing_document,
                        pdf_status=self._document_status(existing_document, "pdf_status", announcement.attachment_bytes),
                        ocr_status=self._document_status(
                            existing_document,
                            "ocr_status",
                            announcement.three_page_ocr_text or announcement.full_ocr_text or announcement.audio_transcript_text,
                        ),
                    )

                if announcement.categories and self.pipeline.requires_full_ocr(announcement):
                    if not announcement.full_ocr_text and announcement.attachment_bytes:
                        self.pipeline.ocr_full_documents([announcement])
                        self._record_stage(
                            announcement,
                            "ocr_full_document",
                            "failed" if announcement.ocr_error else "completed",
                            error=announcement.ocr_error,
                            payload={
                                "number_of_pages": announcement.number_of_pages,
                                "text_chars": len(announcement.full_ocr_text or ""),
                            },
                        )
                        self._persist_document(
                            announcement,
                            existing_document,
                            pdf_status=self._document_status(existing_document, "pdf_status", announcement.attachment_bytes),
                            ocr_status="failed" if announcement.ocr_error else "completed",
                        )
                    if self.pipeline.should_transcribe_earnings_audio(announcement) and not announcement.audio_transcript_text:
                        self.pipeline.transcribe_earnings_call_audio([announcement])
                        self._record_stage(
                            announcement,
                            "transcribe_audio",
                            "completed" if announcement.audio_transcript_text else "skipped",
                            payload={
                                "audio_attachment_url": announcement.audio_attachment_url,
                                "text_chars": len(announcement.audio_transcript_text or ""),
                            },
                        )
                        self._persist_document(
                            announcement,
                            existing_document,
                            pdf_status=self._document_status(existing_document, "pdf_status", announcement.attachment_bytes),
                            ocr_status="failed" if announcement.ocr_error else "completed",
                        )

                missing_reports = self._missing_reports(
                    announcement,
                    existing_reports=existing_reports,
                    parse_reports=parse_reports,
                )
                if missing_reports:
                    self.pipeline.parse_categories([announcement], report_names=missing_reports)
                    if announcement.parsed_reports:
                        upsert_reports(report_rows_from_announcement(announcement))
                        for report in announcement.parsed_reports:
                            existing_reports[(announcement.unique_id, report.report_name)] = {
                                "unique_id": announcement.unique_id,
                                "report_name": report.report_name,
                            }
                        summary.parsed += len(announcement.parsed_reports)
                    self._record_stage(
                        announcement,
                        "structured_parse",
                        "completed" if announcement.parsed_reports else "skipped",
                        input_payload={"requested_reports": missing_reports},
                        output_payload=[report.model_dump() for report in announcement.parsed_reports],
                        payload={
                            "requested_reports": missing_reports,
                            "parsed_reports": [report.report_name for report in announcement.parsed_reports],
                        },
                    )
                    self._persist_document(
                        announcement,
                        existing_document,
                        pdf_status=self._document_status(existing_document, "pdf_status", announcement.attachment_bytes),
                        ocr_status=self._document_status(
                            existing_document,
                            "ocr_status",
                            announcement.three_page_ocr_text or announcement.full_ocr_text or announcement.audio_transcript_text,
                        ),
                        parse_status="completed",
                    )
                elif announcement.categories:
                    self._persist_document(
                        announcement,
                        existing_document,
                        pdf_status=self._document_status(existing_document, "pdf_status", announcement.attachment_bytes),
                        ocr_status=self._document_status(
                            existing_document,
                            "ocr_status",
                            announcement.three_page_ocr_text or announcement.full_ocr_text or announcement.audio_transcript_text,
                        ),
                        parse_status="completed",
                    )
                    summary.skipped += 1
                else:
                    self._persist_document(
                        announcement,
                        existing_document,
                        pdf_status=self._document_status(existing_document, "pdf_status", announcement.attachment_bytes),
                        ocr_status=self._document_status(
                            existing_document,
                            "ocr_status",
                            announcement.three_page_ocr_text or announcement.full_ocr_text or announcement.audio_transcript_text,
                        ),
                        parse_status="skipped",
                    )
                    summary.skipped += 1

                if announcement.combined_text and not announcement.concise_summary_text:
                    self.pipeline.summarize_concisely([announcement])
                    self._record_stage(
                        announcement,
                        "summary",
                        "completed" if announcement.concise_summary_text else "skipped",
                        payload={
                            "summary_chars": len(announcement.concise_summary_text or ""),
                        },
                    )
                    self._persist_document(
                        announcement,
                        existing_document,
                        pdf_status=self._document_status(existing_document, "pdf_status", announcement.attachment_bytes),
                        ocr_status=self._document_status(existing_document, "ocr_status", announcement.three_page_ocr_text or announcement.full_ocr_text or announcement.audio_transcript_text),
                        parse_status=self._document_status(existing_document, "parse_status", announcement.concise_summary_text or announcement.parsed_reports),
                    )

                announcement.attachment_bytes = None
            except Exception as exc:
                logger.exception("Failed announcement %s", announcement.unique_id)
                self._record_stage(announcement, "managed_ingest", "error", error=str(exc))
                summary.failed += 1
                self._persist_document(
                    announcement,
                    existing_document,
                    pdf_status=existing_document.get("pdf_status"),
                    ocr_status=existing_document.get("ocr_status"),
                    parse_status="failed",
                    error=str(exc),
                )

        return summary

    def _record_stage(
        self,
        announcement: Announcement,
        stage: str,
        status: str,
        *,
        error: object = None,
        input_payload: object = None,
        output_payload: object = None,
        payload: dict | None = None,
    ) -> None:
        safe_trace_call(
            record_event_processing,
            unique_id=announcement.unique_id,
            symbol=announcement.ticker,
            source_type=f"{announcement.exchange}_announcement",
            stage=stage,
            status=status,
            error=error,
            input_payload=input_payload,
            output_payload=output_payload,
            payload=payload or {},
        )

    def _save_raw_metadata(self, announcement: Announcement, existing_document: dict) -> None:
        keys = save_announcement_artifacts(announcement)
        if not existing_document:
            upsert_documents(
                [
                    document_row_from_announcement(
                        announcement,
                        storage_keys=keys,
                        existing_row={},
                        pdf_status="pending" if announcement.attachment_url else "unavailable",
                        ocr_status="pending" if announcement.attachment_url else "skipped",
                        parse_status="pending",
                    )
                ]
            )

    def _hydrate_from_state(self, announcement: Announcement, existing_document: dict) -> None:
        if not existing_document:
            return
        categories_json = existing_document.get("categories_json")
        if categories_json:
            announcement.categories = json.loads(categories_json)
        announcement.three_page_ocr_text = load_ocr_text(existing_document)
        announcement.full_ocr_text = load_full_ocr_text(existing_document)
        announcement.audio_transcript_text = load_audio_transcript_text(existing_document)
        announcement.concise_summary_text = load_concise_summary_text(existing_document)
        if existing_document.get("ocr_status") == "completed":
            announcement.number_of_pages = existing_document.get("number_of_pages")
        announcement.audio_attachment_url = existing_document.get("audio_attachment_url")
        announcement.audio_attachment_name = existing_document.get("audio_attachment_name")

    def _ensure_downloaded(self, announcement: Announcement, existing_document: dict) -> None:
        if existing_document.get("pdf_status") == "completed" and existing_document.get("pdf_s3_key"):
            announcement.attachment_bytes = load_attachment_bytes(existing_document)
            announcement.attachment_content_type = existing_document.get("attachment_content_type")
            return
        self.pipeline.download_announcements([announcement])
        save_announcement_artifacts(announcement)

    def _missing_reports(
        self,
        announcement: Announcement,
        *,
        existing_reports: dict,
        parse_reports: Optional[Sequence[str]],
    ) -> List[str]:
        if not announcement.categories:
            return []
        target_reports = self.pipeline._reports_for_announcement(announcement, parse_reports)
        missing: List[str] = []
        for _, model in target_reports:
            key = (announcement.unique_id, model.__name__)
            if key not in existing_reports:
                missing.append(model.__name__)
        return missing

    def _persist_document(
        self,
        announcement: Announcement,
        existing_document: dict,
        *,
        pdf_status: Optional[str] = None,
        ocr_status: Optional[str] = None,
        parse_status: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        keys = save_announcement_artifacts(announcement)
        row = document_row_from_announcement(
            announcement,
            storage_keys=keys,
            existing_row=existing_document,
            pdf_status=pdf_status,
            ocr_status=ocr_status,
            parse_status=parse_status,
            error=error,
        )
        upsert_documents([row])
        existing_document.update(row)

    @staticmethod
    def _document_status(existing_document: dict, key: str, value) -> str:
        if value:
            return "completed"
        if existing_document.get(key):
            return existing_document[key]
        return "pending"

    @staticmethod
    def _date_start(value: date) -> datetime:
        return pytz.UTC.localize(datetime.combine(value, time.min))

    @staticmethod
    def _date_end(value: date) -> datetime:
        return pytz.UTC.localize(datetime.combine(value, time.max))
