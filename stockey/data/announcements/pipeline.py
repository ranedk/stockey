from __future__ import annotations

import json
import tempfile
import textwrap
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Type

import pytz
import requests
from jinja2 import Template
from openai import OpenAI
from pdf2image import convert_from_bytes, pdfinfo_from_bytes
from pydantic import BaseModel, create_model

from .categorize import report_category_map
from .models import Announcement, CompanyMasterTarget, ParsedReport
from .prompts import CATEGORY_PROMPTS, REPORT_PROMPTS
from .schemas import DOCUMENT_PYDANTIC_MAP, MODEL_TYPE_MAP
from utils.http import get_dynamic_headers
from utils.log import setup_logger

from environs import Env

env = Env()
env.read_env()
logger = setup_logger("announcement_pipeline")

OPENAI_API_KEY = env("OPENAI_API_KEY")


class AnnouncementPipeline:
    def __init__(
        self,
        openai_client: Optional[OpenAI] = None,
        ocr_engine: Optional[object] = None,
        openai_model: str = "gpt-4o-2024-08-06",
        request_timeout: int = 60,
    ) -> None:
        self.openai_client = openai_client
        self.ocr_engine = ocr_engine
        self.openai_model = openai_model
        self.request_timeout = request_timeout
        self._bse_headers = self._build_bse_headers()
        self._nse_headers, self._nse_cookies = self._build_nse_session()

    def fetch_announcements(
        self,
        companies: Sequence[CompanyMasterTarget],
        since: Optional[datetime] = None,
    ) -> List[Announcement]:
        since = since or (datetime.now(tz=pytz.UTC) - timedelta(days=30))
        announcements: List[Announcement] = []
        for company in companies:
            exchange = company.normalized_exchange()
            if exchange == "BSE":
                announcements.extend(self._fetch_bse_announcements(company, since))
            elif exchange == "NSE":
                announcements.extend(self._fetch_nse_announcements(company, since))
            else:
                raise ValueError(f"Unsupported exchange: {company.exchange}")
        return self._dedupe_announcements(announcements)

    def download_announcements(self, announcements: Sequence[Announcement]) -> List[Announcement]:
        for announcement in announcements:
            if announcement.pdf_bytes or not announcement.attachment_url:
                continue
            response = requests.get(
                announcement.attachment_url,
                headers=self._headers_for_download(announcement.exchange),
                cookies=self._cookies_for_download(announcement.exchange),
                timeout=self.request_timeout,
            )
            if (
                announcement.exchange.upper() == "BSE"
                and response.status_code == 404
                and announcement.attachment_name
            ):
                response = requests.get(
                    f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{announcement.attachment_name}",
                    headers=self._headers_for_download(announcement.exchange),
                    timeout=self.request_timeout,
                )
            response.raise_for_status()
            announcement.pdf_bytes = response.content
            announcement.ocr_error = None
        return list(announcements)

    def ocr_first_pages(self, announcements: Sequence[Announcement], max_pages: int = 3) -> List[Announcement]:
        for announcement in announcements:
            if not announcement.pdf_bytes:
                continue
            images = convert_from_bytes(announcement.pdf_bytes, first_page=1, last_page=max_pages)
            pdf_info = pdfinfo_from_bytes(announcement.pdf_bytes)
            announcement.number_of_pages = int(pdf_info.get("Pages", 0))
            page_texts: List[str] = []
            ocr_engine = None
            for image in images:
                with tempfile.NamedTemporaryFile(suffix=".png", delete=True) as handle:
                    image.save(handle, format="PNG")
                    handle.flush()
                    try:
                        if ocr_engine is None:
                            ocr_engine = self._get_ocr_engine()
                        text = ocr_engine.ocr_page(handle.name)
                    except Exception as exc:
                        announcement.ocr_error = str(exc)
                        logger.warning("OCR failed for %s: %s", announcement.unique_id, exc)
                        break
                    if text:
                        page_texts.append(text)
            announcement.three_page_ocr_text = "\n\n".join(page_texts)
        return list(announcements)

    def categorize(self, announcements: Sequence[Announcement]) -> List[Announcement]:
        for announcement in announcements:
            announcement.categories = report_category_map(announcement)
        return list(announcements)

    def parse_categories(
        self,
        announcements: Sequence[Announcement],
        report_names: Optional[Sequence[str]] = None,
    ) -> List[Announcement]:
        common_prompt = REPORT_PROMPTS["COMMON"].prompt.strip()
        for announcement in announcements:
            parsed_reports: List[ParsedReport] = []
            for category, report_model in self._reports_for_announcement(announcement, report_names):
                report_prompt = REPORT_PROMPTS.get(report_model.__name__)
                if report_prompt is None:
                    continue
                prompt_parts = [
                    common_prompt,
                    CATEGORY_PROMPTS.get(category, "").strip(),
                    report_prompt.prompt.strip(),
                ]
                rendered_prompt = self._render_prompt(
                    announcement,
                    "\n\n".join(part for part in prompt_parts if part),
                )
                response_model = create_model(
                    "AnnouncementParse",
                    **{report_model.__name__: (report_model, ...)},
                )
                completion = self._get_openai_client().beta.chat.completions.parse(
                    model=self.openai_model,
                    temperature=0,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You extract structured data from stock exchange announcements "
                                "and OCR text. Return only values that are explicitly supported "
                                "by the provided document."
                            ),
                        },
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": rendered_prompt},
                                {"type": "text", "text": self._build_document_context(announcement)},
                            ],
                        },
                    ],
                    response_format=response_model,
                )
                parsed = getattr(completion.choices[0].message.parsed, report_model.__name__)
                parsed_reports.append(
                    ParsedReport(
                        category=category,
                        report_name=report_model.__name__,
                        model_name=MODEL_TYPE_MAP[report_model],
                        data=parsed.model_dump(),
                    )
                )
            announcement.parsed_reports = parsed_reports
        return list(announcements)

    def process(
        self,
        companies: Sequence[CompanyMasterTarget],
        since: Optional[datetime] = None,
        parse_reports: Optional[Sequence[str]] = None,
        download_files: bool = True,
        do_ocr: bool = True,
        do_categorize: bool = True,
        do_parse: bool = True,
    ) -> List[Announcement]:
        announcements = self.fetch_announcements(companies, since=since)
        if download_files:
            self.download_announcements(announcements)
        if do_ocr:
            self.ocr_first_pages(announcements)
        if do_categorize:
            self.categorize(announcements)
        if do_parse:
            self.parse_categories(announcements, report_names=parse_reports)
        return announcements

    def _fetch_bse_announcements(self, company: CompanyMasterTarget, since: datetime) -> List[Announcement]:
        from_date = datetime.now().strftime("%Y%m%d")
        to_date = since.astimezone(pytz.UTC).strftime("%Y%m%d")
        page_num = 1
        total_pages = None
        rows: List[Dict] = []
        while True:
            response = requests.get(
                "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w",
                params={
                    "pageno": str(page_num),
                    "strCat": "-1",
                    "strPrevDate": to_date,
                    "strScrip": company.ticker,
                    "strSearch": "P",
                    "strToDate": from_date,
                    "strType": "EDDA",
                    "subcategory": "-1",
                },
                headers=self._bse_headers,
                timeout=self.request_timeout,
            )
            response.raise_for_status()
            payload = response.json()
            table = payload.get("Table", [])
            if not table:
                break
            rows.extend(table)
            if total_pages is None:
                total_pages = int(table[0].get("TotalPageCnt", 1))
            if page_num >= total_pages:
                break
            page_num += 1
        return [self._normalize_bse_announcement(company, row) for row in rows]

    def _fetch_nse_announcements(self, company: CompanyMasterTarget, since: datetime) -> List[Announcement]:
        response = requests.get(
            "https://www.nseindia.com/api/corporate-disclosure-getquote",
            params={
                "symbol": company.ticker,
                "corpType": "announcement",
                "market": "equities",
                "from_date": since.astimezone(pytz.UTC).strftime("%d-%m-%Y"),
                "to_date": datetime.now().strftime("%d-%m-%Y"),
            },
            headers=self._nse_headers,
            cookies=self._nse_cookies,
            timeout=self.request_timeout,
        )
        response.raise_for_status()
        return [self._normalize_nse_announcement(company, row) for row in response.json()]

    def _normalize_bse_announcement(self, company: CompanyMasterTarget, row: Dict) -> Announcement:
        attachment_name = (row.get("ATTACHMENTNAME") or "").strip() or None
        attachment_url = None
        if attachment_name and attachment_name.lower().endswith(".pdf"):
            attachment_url = f"https://www.bseindia.com/xml-data/corpfiling/AttachHis/{attachment_name}"
        published_on = self._parse_bse_datetime(row["DT_TM"])
        exchange_published_on = self._parse_bse_datetime(row["DissemDT"])
        return Announcement(
            company_master_id=company.company_master_id,
            exchange="BSE",
            ticker=company.ticker,
            company_name=company.company_name,
            unique_id=f"{row['SCRIP_CD']}-{published_on.strftime('%Y%m%d%H%M%S')}",
            subject=row.get("NEWSSUB", "") or "",
            text=row.get("MORE") or row.get("HEADLINE", "") or "",
            filed_under_category=row.get("CATEGORYNAME", "") or "",
            exchange_category_id=str(row.get("AGENDA_ID", "") or ""),
            raw=row,
            published_on=published_on,
            exchange_published_on=exchange_published_on,
            attachment_url=attachment_url,
            attachment_name=attachment_name,
        )

    def _normalize_nse_announcement(self, company: CompanyMasterTarget, row: Dict) -> Announcement:
        attachment_url = (row.get("attchmntFile") or "").strip() or None
        attachment_name = attachment_url.rsplit("/", 1)[-1] if attachment_url else None
        if attachment_name and not attachment_name.lower().endswith(".pdf"):
            attachment_url = None
        published_on = self._parse_nse_datetime(row["exchdisstime"])
        exchange_published_on = self._parse_nse_datetime(row["sort_date"])
        return Announcement(
            company_master_id=company.company_master_id,
            exchange="NSE",
            ticker=company.ticker,
            company_name=company.company_name,
            unique_id=f"{company.ticker}-{published_on.strftime('%Y%m%d%H%M%S')}",
            subject=row.get("desc", "") or "",
            text=row.get("attchmntText", "") or "",
            filed_under_category=row.get("desc", "") or "",
            exchange_category_id=row.get("desc", "") or "",
            raw=row,
            published_on=published_on,
            exchange_published_on=exchange_published_on,
            attachment_url=attachment_url,
            attachment_name=attachment_name,
        )

    def _reports_for_announcement(
        self,
        announcement: Announcement,
        report_names: Optional[Sequence[str]],
    ) -> List[tuple[str, Type[BaseModel]]]:
        allowed = set(report_names or [])
        reports: List[tuple[str, Type[BaseModel]]] = []
        for category in announcement.categories:
            for report_model in DOCUMENT_PYDANTIC_MAP.get(category, []):
                if allowed and report_model.__name__ not in allowed:
                    continue
                reports.append((category, report_model))
        unique: List[tuple[str, Type[BaseModel]]] = []
        seen: set[str] = set()
        for category, report_model in reports:
            key = f"{category}:{report_model.__name__}"
            if key in seen:
                continue
            seen.add(key)
            unique.append((category, report_model))
        return unique

    def _build_document_context(self, announcement: Announcement) -> str:
        payload = {
            "company_master_id": announcement.company_master_id,
            "exchange": announcement.exchange,
            "ticker": announcement.ticker,
            "company_name": announcement.company_name or "",
            "subject": announcement.subject,
            "filed_under_category": announcement.filed_under_category,
            "published_on": announcement.published_on.isoformat(),
            "exchange_published_on": announcement.exchange_published_on.isoformat(),
            "number_of_pages": announcement.number_of_pages,
            "categories": announcement.categories,
        }
        return textwrap.dedent(
            f"""\
            # Announcement metadata
            ```json
            {json.dumps(payload, indent=2, default=str)}
            ```

            # Exchange text
            ```text
            {announcement.text or ""}
            ```

            # OCR text from first three pages
            ```text
            {announcement.three_page_ocr_text or ""}
            ```
            """
        )

    def _render_prompt(self, announcement: Announcement, prompt_template: str) -> str:
        month = announcement.published_on.month
        year = announcement.published_on.year
        current_year = f"{year - 1}-{year}"
        last_year = f"{year - 2}-{year - 1}"
        if 1 <= month <= 3:
            current_quarter = f"December {year - 1}"
            last_quarter = f"September {year - 1}"
        elif 4 <= month <= 6:
            current_quarter = f"March {year}"
            last_quarter = f"December {year - 1}"
        elif 7 <= month <= 9:
            current_quarter = f"June {year}"
            last_quarter = f"March {year}"
        else:
            current_quarter = f"September {year}"
            last_quarter = f"June {year}"
        return Template(prompt_template).render(
            current_quarter=current_quarter,
            last_quarter=last_quarter,
            current_year=current_year,
            last_year=last_year,
            company=announcement.company_name or announcement.ticker,
        )

    def _headers_for_download(self, exchange: str) -> Dict[str, str]:
        return self._nse_headers if exchange.upper() == "NSE" else self._bse_headers

    def _cookies_for_download(self, exchange: str):
        return self._nse_cookies if exchange.upper() == "NSE" else None

    def _build_bse_headers(self) -> Dict[str, str]:
        headers = get_dynamic_headers()
        headers.update({"origin": "https://www.bseindia.com", "referer": "https://www.bseindia.com/"})
        return headers

    def _build_nse_session(self):
        last_error: Exception | None = None
        bootstrap_urls = [
            "https://www.nseindia.com",
            "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
        ]
        for attempt in range(1, 6):
            headers = get_dynamic_headers()
            headers.update(
                {
                    "accept": "*/*",
                    "origin": "https://www.nseindia.com",
                    "referer": "https://www.nseindia.com",
                }
            )
            for bootstrap_url in bootstrap_urls:
                try:
                    response = requests.get(bootstrap_url, headers=headers, timeout=self.request_timeout)
                    response.raise_for_status()
                    return headers, response.cookies.get_dict()
                except requests.RequestException as exc:
                    last_error = exc
                    logger.warning(
                        "NSE session bootstrap failed on attempt %s for %s: %s",
                        attempt,
                        bootstrap_url,
                        exc,
                    )
            if attempt < 5:
                sleep_for = min(2 ** (attempt - 1), 8)
                time.sleep(sleep_for)
        logger.warning("Proceeding without NSE bootstrap cookies after repeated failures")
        return headers, {}

    def _get_openai_client(self) -> OpenAI:
        if self.openai_client is None:
            self.openai_client = OpenAI(api_key=OPENAI_API_KEY)
        return self.openai_client

    def _get_ocr_engine(self):
        if self.ocr_engine is None:
            from .ocr import POCR

            self.ocr_engine = POCR()
        return self.ocr_engine

    @staticmethod
    def _dedupe_announcements(items: Sequence[Announcement]) -> List[Announcement]:
        seen: set[str] = set()
        output: List[Announcement] = []
        for item in sorted(items, key=lambda x: x.published_on, reverse=True):
            if item.unique_id in seen:
                continue
            seen.add(item.unique_id)
            output.append(item)
        return output

    @staticmethod
    def _parse_bse_datetime(value: str) -> datetime:
        for pattern in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
            try:
                parsed = datetime.strptime(value, pattern)
                return pytz.UTC.localize(parsed)
            except ValueError:
                continue
        raise ValueError(f"Unsupported BSE datetime: {value}")

    @staticmethod
    def _parse_nse_datetime(value: str) -> datetime:
        for pattern in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(value, pattern)
                return pytz.UTC.localize(parsed)
            except ValueError:
                continue
        raise ValueError(f"Unsupported NSE datetime: {value}")
