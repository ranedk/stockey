from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass(slots=True)
class CompanyMasterTarget:
    company_master_id: str
    ticker: str
    exchange: str
    company_name: Optional[str] = None
    nse_ticker: Optional[str] = None
    bse_ticker: Optional[str] = None
    sharpely_id: Optional[str] = None
    dhan_nse_id: Optional[int] = None
    dhan_bse_id: Optional[int] = None

    def normalized_exchange(self) -> str:
        return self.exchange.upper()


@dataclass(slots=True)
class ParsedReport:
    category: str
    report_name: str
    model_name: str
    data: Dict[str, Any]
    prompt_id: Optional[str] = None
    prompt_version: Optional[str] = None
    prompt_schema_version: Optional[str] = None


@dataclass(slots=True)
class Announcement:
    company_master_id: str
    exchange: str
    ticker: str
    company_name: Optional[str]
    unique_id: str
    subject: str
    text: str
    filed_under_category: str
    exchange_category_id: str
    raw: Dict[str, Any]
    published_on: datetime
    exchange_published_on: datetime
    attachment_url: Optional[str] = None
    attachment_name: Optional[str] = None
    attachment_content_type: Optional[str] = None
    attachment_bytes: Optional[bytes] = field(default=None, repr=False)
    number_of_pages: Optional[int] = None
    three_page_ocr_text: str = ""
    full_ocr_text: str = ""
    audio_attachment_url: Optional[str] = None
    audio_attachment_name: Optional[str] = None
    audio_transcript_text: str = ""
    concise_summary_text: str = ""
    concise_summary_model_name: Optional[str] = None
    concise_summary_prompt_id: Optional[str] = None
    concise_summary_prompt_version: Optional[str] = None
    concise_summary_prompt_schema_version: Optional[str] = None
    ocr_model_name: Optional[str] = None
    ocr_prompt_id: Optional[str] = None
    ocr_prompt_version: Optional[str] = None
    ocr_prompt_schema_version: Optional[str] = None
    ocr_error: Optional[str] = None
    categories: List[str] = field(default_factory=list)
    parsed_reports: List[ParsedReport] = field(default_factory=list)

    @property
    def combined_text(self) -> str:
        return "\n".join(
            part
            for part in [
                self.text,
                self.three_page_ocr_text,
                self.full_ocr_text,
                self.audio_transcript_text,
            ]
            if part
        )

    def attachment_extension(self) -> str:
        if self.attachment_name and "." in self.attachment_name:
            return self.attachment_name.rsplit(".", 1)[-1].lower()
        if not self.attachment_content_type:
            return ""
        if "pdf" in self.attachment_content_type:
            return "pdf"
        if "mpeg" in self.attachment_content_type or "mp3" in self.attachment_content_type:
            return "mp3"
        if "wav" in self.attachment_content_type:
            return "wav"
        if "mp4" in self.attachment_content_type:
            return "mp4"
        if "ogg" in self.attachment_content_type:
            return "ogg"
        if "webm" in self.attachment_content_type:
            return "webm"
        return ""

    def is_pdf_attachment(self) -> bool:
        return self.attachment_extension() == "pdf"

    def is_audio_attachment(self) -> bool:
        return self.attachment_extension() in {"mp3", "wav", "mp4", "m4a", "aac", "ogg", "webm"}
