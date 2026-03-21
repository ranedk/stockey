from __future__ import annotations

import re
from typing import List

from .models import Announcement
from .schemas import EACategoryChoices


def anyinstr(blob: str, *matches: str) -> bool:
    return any(x.lower() in blob.lower() for x in matches)


def allinstr(blob: str, *matches: str) -> bool:
    return all(x.lower() in blob.lower() for x in matches)


def allregex(blob: str, *regexes: str) -> bool:
    return all(re.search(pattern, blob.lower(), re.IGNORECASE) for pattern in regexes)


def anyregex(blob: str, *regexes: str) -> bool:
    return any(re.search(pattern, blob.lower(), re.IGNORECASE) for pattern in regexes)


def report_category_map(announcement: Announcement) -> List[str]:
    text = announcement.combined_text
    categories = set()

    if allinstr(text, "brsr") or allregex(text, r"responsib.{0,10}?sustain"):
        return [EACategoryChoices.IGNORE]
    if allregex(text, "newspa.{0,100}(publica|adver)"):
        return [EACategoryChoices.IGNORE]
    if anyregex(text, r"(book|register|trad).{0,100}clos", r"clos.{0,10}(book|register|trad)"):
        return [EACategoryChoices.IGNORE]
    if allinstr(text, "intimation of"):
        categories.add(EACategoryChoices.INTIMATION_MEETING)
    if allinstr(text, "acquisition"):
        if allregex(text, r"acquisition\s+of.{0,50}?share"):
            categories.add(EACategoryChoices.ACQUISITION_OF_COMPANY)
        elif allregex(text, r"acquisition\s+of.{0,50}?(land|property|premises|properties)?"):
            categories.add(EACategoryChoices.ACQUISITION_OF_PROPERTY)
        else:
            categories.add(EACategoryChoices.ACQUISITION_OF_COMPANY)
    if allinstr(text, "allotment"):
        if allregex(text, r"allotment\s+of.{0,100}?share"):
            categories.add(EACategoryChoices.ALLOTMENT_OF_SHARES)
        elif allregex(text, r"allotment\s+of.{0,100}?(debenture|securi|ncd)"):
            categories.add(EACategoryChoices.ALLOTMENT_OF_DEBENTURES)
        elif allregex(text, r"preferential\s+allotment"):
            categories.add(EACategoryChoices.PREFERENTIAL_ISSUE)
    if allregex(text, r"change in RTA"):
        categories.add(EACategoryChoices.CHANGE_IN_RTA)
    if allregex(text, "(tribunal|nclt)"):
        categories.add(EACategoryChoices.AMALGAMATION)
    if anyregex(
        text,
        r"(got|received|award|receiving|bagged|receipt|reciept).{0,100}(order|contract)",
        r"(order|contract)s?\s+(is|of|worth)\s+(inr|rs|rupees|usd|us dollar|dollar|crore|lakh|lac)",
        r"work.{0,10}(order|contract)",
        r"order.{0,50}(secured|received)",
    ):
        categories.add(EACategoryChoices.WORK_ORDER_CONTRACT)
    if allregex(text, r"l.1\s+bid"):
        categories.add(EACategoryChoices.L1_BIDDER)
    if anyregex(text, r"(inti|notice).{0,100}meeting", r"(meeting).{0,100}(schedu|)"):
        categories.add(EACategoryChoices.INTIMATION_MEETING)
    if anyregex(text, r"bonus\s*(issue)?.{0,50}(equity)?\s*share", r"bonus\s*issue"):
        categories.add(EACategoryChoices.BONUS)
    if anyregex(text, r"buyback", r"buy back", r"buy-back"):
        categories.add(EACategoryChoices.BUYBACK)
    if anyregex(text, r"annnual.{0,10}report") and (announcement.number_of_pages or 0) > 5:
        categories.add(EACategoryChoices.ANNUAL_REPORT)
    if anyregex(
        text,
        r"(investor|analyst).{0,50}(call|meet)",
        r"(transcript|conference).{0,50}(call|meet|transcript)",
        r"call.{0,50}transcript",
        r"audio.{0,50}(recording|link).{0,50}call",
        r"call.{0,50}(recording|link)",
        r"earnings.{0,50}call",
    ):
        categories.add(EACategoryChoices.EARNINGS_CALL)
    if (
        anyregex(
            text,
            r"(investor|earning|sharehold|corporate).{0,50}(presentation|ppt)",
            r"(presentation).{0,50}(investor|sharehold|results)",
            r"(invest).{0,50}(presentation|release|communication)",
        )
        and (announcement.number_of_pages or 0) > 5
    ):
        categories.add(EACategoryChoices.INVESTOR_PRESENTATION)
    if anyregex(text, r"financ.{0,100}(result|statement|report)") and (announcement.number_of_pages or 0) > 6:
        month = announcement.published_on.month
        if 7 <= month <= 9:
            categories.add(EACategoryChoices.QUARTER_FINANCIAL_RESULT)
        elif 10 <= month <= 12:
            if anyregex(text, r"(financ).{0,10}(result|report|statement).{0,30}half.{0,10}year"):
                categories.add(EACategoryChoices.HALF_YEARLY_FINANCIAL_RESULT)
            else:
                categories.add(EACategoryChoices.QUARTER_FINANCIAL_RESULT)
        elif 1 <= month <= 3:
            categories.add(EACategoryChoices.QUARTER_FINANCIAL_RESULT)
        elif 4 <= month <= 6:
            categories.add(EACategoryChoices.ANNUAL_FINANCIAL_RESULT)
    if anyregex(text, "sharehold.{0,30}(pattern|table)"):
        categories.add(EACategoryChoices.SHAREHOLDING)
    if anyinstr(text, "resignation", "resigned", "appoint", "re-appoint"):
        categories.add(EACategoryChoices.PERSONNEL_CHANGE)
    if anyinstr(text, "dividend", "divident"):
        categories.add(EACategoryChoices.DIVIDEND)
    if anyregex(text, r"default.{0,100}(payment|interest|loan)"):
        categories.add(EACategoryChoices.LOAN_DEFAULT)
    if allinstr(text, "sast"):
        categories.add(EACategoryChoices.SAST)
    if anyregex(
        text,
        r"(credit|care|moody|crisil|irca|fitch|india|short term|long term).{0,50}rating",
        r"rating.{0,50}(credit|care|moody|crisil|irca|fitch|india|short term|long term)",
        r"rating.{0,50}(change|outlook)",
        r"(change|outlook).{0,50}rating",
    ):
        categories.add(EACategoryChoices.CREDIT_RATING)
    if anyinstr(text, "preferential"):
        categories.add(EACategoryChoices.PREFERENTIAL_ISSUE)
    if anyregex(text, r"qualif.{0,10}institu.{0,10}placement", r"qip", r"qib"):
        categories.add(EACategoryChoices.QIP)

    if not categories:
        return [EACategoryChoices.IGNORE]
    return list(categories)
