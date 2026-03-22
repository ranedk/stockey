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
    pdf_bytes: Optional[bytes] = field(default=None, repr=False)
    number_of_pages: Optional[int] = None
    three_page_ocr_text: str = ""
    ocr_error: Optional[str] = None
    categories: List[str] = field(default_factory=list)
    parsed_reports: List[ParsedReport] = field(default_factory=list)

    @property
    def combined_text(self) -> str:
        return "\n".join(part for part in [self.text, self.three_page_ocr_text] if part)
