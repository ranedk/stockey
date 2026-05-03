from __future__ import annotations

import mimetypes
import json
import re
import sys
import tempfile
import textwrap
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Dict, List, Literal, Optional, Sequence, Type

import pytz
import requests
from jinja2 import Template
from openai import OpenAI
from pdf2image import pdfinfo_from_bytes
from pydantic import BaseModel, create_model

from .categorize import report_category_map
from .models import Announcement, CompanyMasterTarget, ParsedReport
from .prompts import CATEGORY_PROMPTS, REPORT_PROMPTS
from .schemas import DOCUMENT_PYDANTIC_MAP, MODEL_TYPE_MAP
from utils.http import get_dynamic_headers
from utils.log import setup_logger
from utils.ocr import ocr_pdf_with_gemini, ocr_pdf_with_openai
from utils.transcribe import transcribe_audio_bytes

from environs import Env

env = Env()
env.read_env()
logger = setup_logger("announcement_pipeline")

OPENAI_API_KEY = env("OPENAI_API_KEY")
OCR_USING = env("OCR_USING", default="gemini-3-flash-preview")
TRANSCRIBE_WITH = env("TRANSCRIBE_WITH", default="gemini-3-flash-preview")
SUMMARIZE_WITH = env("SUMMARIZE_WITH", default="gpt-5-mini-2025-08-07")
NSE_HTTP_MAX_ATTEMPTS = env.int("NSE_HTTP_MAX_ATTEMPTS", default=0)
NSE_HTTP_RETRY_SLEEP_SECONDS = env.float("NSE_HTTP_RETRY_SLEEP_SECONDS", default=5.0)
NSE_HTTP_RETRY_MAX_SLEEP_SECONDS = env.float("NSE_HTTP_RETRY_MAX_SLEEP_SECONDS", default=120.0)
NSE_HTTP_RETRY_STATUSES = {429, 500, 502, 503, 504}
Provider = Literal["openai", "gemini"]
_AUDIO_EXTENSIONS = {"mp3", "wav", "mp4", "m4a", "aac", "ogg", "webm"}
_AUDIO_LINK_PATTERN = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
_AUDIO_HINT_PATTERN = re.compile(r"(audio|recording|transcript|conference|earnings).{0,80}(mp3|wav|mp4|m4a|aac|ogg|webm)", re.IGNORECASE)


class AnnouncementPipeline:
    def __init__(
        self,
        openai_client: Optional[OpenAI] = None,
        summarize_model: str = SUMMARIZE_WITH,
        request_timeout: int = 60,
    ) -> None:
        self.openai_client = openai_client
        self.summarize_model = summarize_model
        self.ocr_model = OCR_USING
        self.transcribe_model = TRANSCRIBE_WITH
        self.request_timeout = request_timeout
        self._bse_headers = self._build_bse_headers()
        self._nse_headers, self._nse_cookies = self._build_nse_session()

    def _nse_get_with_retry(
        self,
        url: str,
        *,
        params: Dict | None = None,
        headers: Dict | None = None,
        cookies: Dict | None = None,
        max_attempts: int | None = None,
    ) -> requests.Response:
        attempt = 0
        last_error: Exception | None = None
        attempt_cap = NSE_HTTP_MAX_ATTEMPTS if max_attempts is None else int(max_attempts)
        while True:
            attempt += 1
            try:
                request_headers = headers if headers is not None else self._nse_headers
                request_cookies = cookies if cookies is not None else self._nse_cookies
                response = requests.get(
                    url,
                    params=params,
                    headers=request_headers,
                    cookies=request_cookies,
                    timeout=self.request_timeout,
                )
                if response.status_code in NSE_HTTP_RETRY_STATUSES:
                    raise requests.HTTPError(f"NSE transient HTTP {response.status_code}", response=response)
                response.raise_for_status()
                return response
            except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
                last_error = exc
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                is_retryable_http = status_code in NSE_HTTP_RETRY_STATUSES
                is_retryable_network = isinstance(exc, (requests.Timeout, requests.ConnectionError))
                if not (is_retryable_http or is_retryable_network):
                    raise
                if attempt_cap > 0 and attempt >= attempt_cap:
                    raise
                sleep_for = min(
                    NSE_HTTP_RETRY_SLEEP_SECONDS * max(attempt, 1),
                    NSE_HTTP_RETRY_MAX_SLEEP_SECONDS,
                )
                reset_state = headers is None and cookies is None
                if reset_state:
                    self._reset_nse_http_state(reason=f"{exc.__class__.__name__}: {exc}")
                self._log_nse_wait(
                    "NSE request failed; retrying url=%s attempt=%s max_attempts=%s sleep=%.1fs error=%s: %s",
                    url,
                    attempt,
                    "infinite" if attempt_cap <= 0 else attempt_cap,
                    sleep_for,
                    exc.__class__.__name__,
                    exc,
                )
                time.sleep(sleep_for)
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"NSE request failed without exception: {url}")

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
            if announcement.attachment_bytes or not announcement.attachment_url:
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
            announcement.attachment_bytes = response.content
            announcement.attachment_content_type = response.headers.get("Content-Type")
            announcement.ocr_error = None
        return list(announcements)

    def ocr_first_pages(self, announcements: Sequence[Announcement], max_pages: int = 3) -> List[Announcement]:
        for announcement in announcements:
            if not announcement.attachment_bytes:
                continue
            try:
                if announcement.is_audio_attachment():
                    announcement.three_page_ocr_text = self._transcribe_attachment_bytes(announcement).strip()
                    announcement.number_of_pages = None
                    continue

                if not announcement.is_pdf_attachment():
                    continue

                pdf_info = pdfinfo_from_bytes(announcement.attachment_bytes)
                announcement.number_of_pages = int(pdf_info.get("Pages", 0))
                if max_pages <= 0 or not announcement.number_of_pages:
                    page_spec = "all"
                else:
                    page_spec = f"1-{min(max_pages, announcement.number_of_pages)}"
                page_texts = self._ocr_pdf_bytes(announcement.attachment_bytes, pages=page_spec)
                announcement.three_page_ocr_text = "\n\n".join(
                    text for _, text in sorted(page_texts.items()) if text
                )
                if not announcement.three_page_ocr_text:
                    retry_page_texts = self._ocr_pdf_bytes(announcement.attachment_bytes, pages=page_spec)
                    announcement.three_page_ocr_text = "\n\n".join(
                        text for _, text in sorted(retry_page_texts.items()) if text
                    )
            except Exception as exc:
                announcement.ocr_error = str(exc)
                logger.warning("OCR/transcription failed for %s: %s", announcement.unique_id, exc)
        return list(announcements)

    def ocr_full_documents(self, announcements: Sequence[Announcement]) -> List[Announcement]:
        for announcement in announcements:
            if announcement.full_ocr_text or not announcement.attachment_bytes:
                continue
            try:
                if announcement.is_audio_attachment():
                    announcement.full_ocr_text = self._transcribe_attachment_bytes(announcement).strip()
                    continue
                if not announcement.is_pdf_attachment():
                    continue
                page_texts = self._ocr_pdf_bytes(announcement.attachment_bytes, pages="all")
                announcement.full_ocr_text = "\n\n".join(
                    text for _, text in sorted(page_texts.items()) if text
                )
            except Exception as exc:
                announcement.ocr_error = str(exc)
                logger.warning("Full OCR failed for %s: %s", announcement.unique_id, exc)
        return list(announcements)

    def categorize(self, announcements: Sequence[Announcement]) -> List[Announcement]:
        for announcement in announcements:
            announcement.categories = report_category_map(announcement)
        return list(announcements)

    def requires_full_ocr(self, announcement: Announcement) -> bool:
        return any(category in DOCUMENT_PYDANTIC_MAP for category in announcement.categories)

    def should_transcribe_earnings_audio(self, announcement: Announcement) -> bool:
        return "EARNINGS_CALL" in announcement.categories

    def transcribe_earnings_call_audio(self, announcements: Sequence[Announcement]) -> List[Announcement]:
        for announcement in announcements:
            if not self.should_transcribe_earnings_audio(announcement) or announcement.audio_transcript_text:
                continue
            audio_url = self._find_audio_link(announcement)
            if not audio_url:
                continue
            try:
                response = requests.get(
                    audio_url,
                    headers=self._headers_for_download(announcement.exchange),
                    cookies=self._cookies_for_download(announcement.exchange),
                    timeout=max(self.request_timeout, 120),
                )
                response.raise_for_status()
                announcement.audio_attachment_url = audio_url
                announcement.audio_attachment_name = audio_url.split("?", 1)[0].rsplit("/", 1)[-1] or None
                announcement.audio_transcript_text = self._transcribe_audio_bytes(
                    response.content,
                    mime_type=response.headers.get("Content-Type", "application/octet-stream"),
                    suffix=self._suffix_for_audio_url(audio_url, response.headers.get("Content-Type")),
                ).strip()
            except Exception as exc:
                logger.warning("Audio transcription failed for %s: %s", announcement.unique_id, exc)
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
                    model=self.summarize_model,
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

    def summarize_concisely(self, announcements: Sequence[Announcement]) -> List[Announcement]:
        for announcement in announcements:
            prompt = textwrap.dedent(
                """\
                Summarize this stock exchange announcement in a short concise paragraph.
                Include the most important business points and the most relevant numbers.
                Do not use bullets. Do not add headings. Do not invent facts.
                """
            )
            announcement.concise_summary_text = self._generate_text(
                prompt=prompt,
                context=self._build_document_context(announcement),
                model=self.summarize_model,
            ).strip()
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
            self.ocr_full_documents([item for item in announcements if self.requires_full_ocr(item)])
            self.transcribe_earnings_call_audio([item for item in announcements if self.should_transcribe_earnings_audio(item)])
        if do_parse:
            self.parse_categories(announcements, report_names=parse_reports)
            self.summarize_concisely(announcements)
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
        response = self._nse_get_with_retry(
            "https://www.nseindia.com/api/corporate-disclosure-getquote",
            params={
                "symbol": company.ticker,
                "corpType": "announcement",
                "market": "equities",
                "from_date": since.astimezone(pytz.UTC).strftime("%d-%m-%Y"),
                "to_date": datetime.now().strftime("%d-%m-%Y"),
            },
        )
        return [self._normalize_nse_announcement(company, row) for row in response.json()]

    def _normalize_bse_announcement(self, company: CompanyMasterTarget, row: Dict) -> Announcement:
        attachment_name = (row.get("ATTACHMENTNAME") or "").strip() or None
        attachment_url = None
        if attachment_name:
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

            # OCR text from full document
            ```text
            {announcement.full_ocr_text or ""}
            ```

            # Audio transcription
            ```text
            {announcement.audio_transcript_text or ""}
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

    def _build_nse_headers(self) -> Dict[str, str]:
        headers = get_dynamic_headers()
        headers.update(
            {
                "accept": "*/*",
                "origin": "https://www.nseindia.com",
                "referer": "https://www.nseindia.com",
            }
        )
        return headers

    def _log_nse_wait(self, message: str, *args) -> None:
        rendered = message % args if args else message
        logger.warning(rendered)
        print(f"[announcement_pipeline.nse] {rendered}", file=sys.stderr, flush=True)

    def _bootstrap_nse_cookies_once(self, headers: Dict[str, str]) -> Dict[str, str]:
        bootstrap_urls = [
            "https://www.nseindia.com",
            "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
        ]
        last_error: Exception | None = None
        for bootstrap_url in bootstrap_urls:
            try:
                response = requests.get(
                    bootstrap_url,
                    headers=headers,
                    cookies={},
                    timeout=self.request_timeout,
                )
                response.raise_for_status()
                cookies = response.cookies.get_dict()
                logger.info("NSE session bootstrap succeeded url=%s cookies=%s", bootstrap_url, len(cookies))
                return cookies
            except requests.RequestException as exc:
                last_error = exc
                logger.warning("NSE session bootstrap failed url=%s error=%s: %s", bootstrap_url, exc.__class__.__name__, exc)
        if last_error is not None:
            raise last_error
        return {}

    def _reset_nse_http_state(self, *, reason: str) -> None:
        self._nse_headers = self._build_nse_headers()
        self._nse_cookies = {}
        self._log_nse_wait("Reset NSE HTTP session state; cleared cookies reason=%s", reason)
        try:
            self._nse_cookies = self._bootstrap_nse_cookies_once(self._nse_headers)
        except requests.RequestException as exc:
            self._log_nse_wait(
                "NSE cookie bootstrap after reset failed; continuing with empty cookies error=%s: %s",
                exc.__class__.__name__,
                exc,
            )

    def _build_nse_session(self):
        attempt = 0
        while True:
            attempt += 1
            headers = self._build_nse_headers()
            try:
                return headers, self._bootstrap_nse_cookies_once(headers)
            except requests.RequestException as exc:
                self._log_nse_wait(
                    "NSE session bootstrap failed; retrying attempt=%s max_attempts=%s error=%s: %s",
                    attempt,
                    "infinite" if NSE_HTTP_MAX_ATTEMPTS <= 0 else NSE_HTTP_MAX_ATTEMPTS,
                    exc.__class__.__name__,
                    exc,
                )
            if NSE_HTTP_MAX_ATTEMPTS > 0 and attempt >= NSE_HTTP_MAX_ATTEMPTS:
                self._log_nse_wait("Proceeding without NSE bootstrap cookies after repeated failures")
                return headers, {}
            sleep_for = min(
                NSE_HTTP_RETRY_SLEEP_SECONDS * max(attempt, 1),
                NSE_HTTP_RETRY_MAX_SLEEP_SECONDS,
            )
            self._log_nse_wait("Waiting before NSE session bootstrap retry sleep=%.1fs", sleep_for)
            time.sleep(sleep_for)

    def _get_openai_client(self) -> OpenAI:
        if self.openai_client is None:
            self.openai_client = OpenAI(api_key=OPENAI_API_KEY)
        return self.openai_client

    def _ocr_pdf_bytes(self, pdf_bytes: bytes, *, pages: str) -> Dict[int, str]:
        provider = self._model_provider(self.ocr_model)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as handle:
            handle.write(pdf_bytes)
            handle.flush()
            if provider == "gemini":
                return ocr_pdf_with_gemini(handle.name, pages=pages, model=self.ocr_model)
            return ocr_pdf_with_openai(handle.name, pages=pages, model=self.ocr_model)

    def _transcribe_attachment_bytes(self, announcement: Announcement) -> str:
        suffix = f".{announcement.attachment_extension() or 'bin'}"
        return self._transcribe_audio_bytes(
            announcement.attachment_bytes or b"",
            mime_type=announcement.attachment_content_type or "application/octet-stream",
            suffix=suffix,
        )

    def _transcribe_audio_bytes(self, audio_bytes: bytes, *, mime_type: str, suffix: str) -> str:
        provider = self._model_provider(self.transcribe_model)
        kwargs = {
            "provider": provider,
            "suffix": suffix,
            "mime_type": mime_type,
        }
        if provider == "gemini":
            kwargs["gemini_model"] = self.transcribe_model
        else:
            kwargs["openai_model"] = self.transcribe_model
        transcript = transcribe_audio_bytes(audio_bytes, **kwargs)
        return (transcript.get(provider) or "").strip()

    def _find_audio_link(self, announcement: Announcement) -> Optional[str]:
        candidates = OrderedDict()
        search_blobs = [
            announcement.attachment_url or "",
            announcement.text or "",
            announcement.subject or "",
            announcement.three_page_ocr_text or "",
            announcement.full_ocr_text or "",
            json.dumps(announcement.raw, ensure_ascii=True, default=str),
        ]
        for blob in search_blobs:
            for match in _AUDIO_LINK_PATTERN.findall(blob):
                cleaned = match.rstrip(").,;]'\"")
                if self._looks_like_audio_url(cleaned):
                    candidates[cleaned] = None
        for key, value in announcement.raw.items():
            if isinstance(value, str) and self._looks_like_audio_url(value):
                candidates[value] = None
            if isinstance(value, str) and _AUDIO_HINT_PATTERN.search(value):
                for match in _AUDIO_LINK_PATTERN.findall(value):
                    cleaned = match.rstrip(").,;]'\"")
                    if self._looks_like_audio_url(cleaned):
                        candidates[cleaned] = None
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and self._looks_like_audio_url(item):
                        candidates[item] = None
        return next(iter(candidates.keys()), None)

    def _looks_like_audio_url(self, value: str) -> bool:
        if not value.lower().startswith(("http://", "https://")):
            return False
        stripped = value.split("?", 1)[0].lower()
        extension = stripped.rsplit(".", 1)[-1] if "." in stripped else ""
        if extension in _AUDIO_EXTENSIONS:
            return True
        mime_type = mimetypes.guess_type(stripped)[0] or ""
        return mime_type.startswith("audio/") or mime_type.startswith("video/")

    def _suffix_for_audio_url(self, audio_url: str, content_type: Optional[str]) -> str:
        path = audio_url.split("?", 1)[0]
        if "." in path:
            return f".{path.rsplit('.', 1)[-1].lower()}"
        guessed = mimetypes.guess_extension((content_type or "").split(";", 1)[0].strip() or "")
        return guessed or ".bin"

    def _generate_text(self, *, prompt: str, context: str, model: str) -> str:
        provider = self._model_provider(model)
        if provider != "openai":
            raise ValueError(f"Unsupported summarization model provider for {model}")
        response = self._get_openai_client().responses.create(
            model=model,
            input=[
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "You summarize stock exchange announcements faithfully and concisely.",
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_text", "text": context},
                    ],
                },
            ],
        )
        return (response.output_text or "").strip()

    @staticmethod
    def _model_provider(model_name: str) -> Provider:
        if model_name.startswith("gemini"):
            return "gemini"
        if model_name.startswith("gpt-") or model_name.startswith("o"):
            return "openai"
        raise ValueError(f"Could not infer provider for model: {model_name}")


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
