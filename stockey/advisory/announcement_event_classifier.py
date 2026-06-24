"""Proximity/structural event classifier for Indian exchange announcements.

Adapted from operator domain work (a regex categorizer over announcement text + OCR). It classifies
an announcement's text into a small taxonomy of EVENT CLASSES using PROXIMITY and STRUCTURAL patterns
(e.g. "received ... order/contract", "acquisition of ... shares" vs "... property",
"credit/CRISIL ... rating") rather than bare keyword presence.

Why it matters here (hypothesis-matching constraint #4): bare keyword overlap matches incidental
mentions -- "rating upgrade" inside an ED-search story, "acquisition" inside SEBI takeover boilerplate.
Classifying the event's *type* first is a far more precise relevance gate: a hypothesis can declare
the event classes it triggers on, and an event only matches when it actually classifies into one.

Pure and dependency-free (just `re`). `classify_announcement(text)` returns a set of event-class
strings; IGNORE-only means boilerplate (newspaper notices, BRSR, book closure) or nothing material.
The per-class structured-extraction field taxonomy lives in docs/announcement_event_taxonomy.md.
"""

from __future__ import annotations

import re
from typing import Any

# --- Event-class taxonomy (strings; hypotheses reference these in trigger_patterns.event_classes) ---
WORK_ORDER_CONTRACT = "WORK_ORDER_CONTRACT"
L1_BIDDER = "L1_BIDDER"
CREDIT_RATING = "CREDIT_RATING"
BUYBACK = "BUYBACK"
BONUS = "BONUS"
DIVIDEND = "DIVIDEND"
ACQUISITION_OF_COMPANY = "ACQUISITION_OF_COMPANY"
ACQUISITION_OF_PROPERTY = "ACQUISITION_OF_PROPERTY"
AMALGAMATION = "AMALGAMATION"               # merger / demerger / scheme of arrangement
ALLOTMENT_OF_SHARES = "ALLOTMENT_OF_SHARES"
ALLOTMENT_OF_DEBENTURES = "ALLOTMENT_OF_DEBENTURES"
PREFERENTIAL_ISSUE = "PREFERENTIAL_ISSUE"
QIP = "QIP"
LOAN_DEFAULT = "LOAN_DEFAULT"
PERSONNEL_CHANGE = "PERSONNEL_CHANGE"
SAST = "SAST"
FINANCIAL_RESULT = "FINANCIAL_RESULT"
EARNINGS_CALL = "EARNINGS_CALL"
SHAREHOLDING = "SHAREHOLDING"
INTIMATION_MEETING = "INTIMATION_MEETING"
CHANGE_IN_RTA = "CHANGE_IN_RTA"
IGNORE = "IGNORE"

ALL_EVENT_CLASSES = frozenset({
    WORK_ORDER_CONTRACT, L1_BIDDER, CREDIT_RATING, BUYBACK, BONUS, DIVIDEND,
    ACQUISITION_OF_COMPANY, ACQUISITION_OF_PROPERTY, AMALGAMATION, ALLOTMENT_OF_SHARES,
    ALLOTMENT_OF_DEBENTURES, PREFERENTIAL_ISSUE, QIP, LOAN_DEFAULT, PERSONNEL_CHANGE, SAST,
    FINANCIAL_RESULT, EARNINGS_CALL, SHAREHOLDING, INTIMATION_MEETING, CHANGE_IN_RTA, IGNORE,
})


def _anyinstr(blob: str, *matches: str) -> bool:
    return any(token.lower() in blob for token in matches)


def _allregex(blob: str, *patterns: str) -> bool:
    return all(re.search(pattern, blob, re.IGNORECASE) for pattern in patterns)


def _anyregex(blob: str, *patterns: str) -> bool:
    return any(re.search(pattern, blob, re.IGNORECASE) for pattern in patterns)


def classify_announcement(text: Any) -> set[str]:
    """Classify announcement text into a set of event classes (proximity/structural patterns).

    Returns {IGNORE} for boilerplate (newspaper notices, BRSR/sustainability, book closure) or when
    nothing material is detected.
    """
    blob = str(text or "").lower()
    if not blob.strip():
        return {IGNORE}
    categories: set[str] = set()

    # Boilerplate -> IGNORE outright.
    if _anyinstr(blob, "brsr") or _allregex(blob, r"responsib.{0,10}?sustain"):
        return {IGNORE}
    if _allregex(blob, r"newspa.{0,100}(publica|adver)"):
        return {IGNORE}
    if _anyregex(blob, r"(book|register|trad).{0,100}clos", r"clos.{0,10}(book|register|trad)"):
        return {IGNORE}

    # Work order / contract win (proximity: got/received/award/bagged ... order/contract; amount-near).
    if _anyregex(
        blob,
        r"(got|received|award|receiving|bagged|receipt)\w*.{0,100}(order|contract)",
        r"(order|contract)s?\s+(is|of|worth)\s+(inr|rs|rupees|usd|us dollar|dollar|crore|lakh|lac)",
        r"work.{0,10}(order|contract)",
        r"order.{0,50}(secured|received)",
    ):
        categories.add(WORK_ORDER_CONTRACT)
    if _allregex(blob, r"l.?1\s+bid"):
        categories.add(L1_BIDDER)

    # Acquisition: of shares/company vs property.
    if _anyinstr(blob, "acquisition"):
        if _allregex(blob, r"acquisition\s+of.{0,50}?share"):
            categories.add(ACQUISITION_OF_COMPANY)
        elif _allregex(blob, r"acquisition\s+of.{0,50}?(land|property|premises|properties)"):
            categories.add(ACQUISITION_OF_PROPERTY)
        else:
            categories.add(ACQUISITION_OF_COMPANY)

    # Allotment / preferential.
    if _anyinstr(blob, "allotment"):
        if _allregex(blob, r"allotment\s+of.{0,100}?share"):
            categories.add(ALLOTMENT_OF_SHARES)
        elif _allregex(blob, r"allotment\s+of.{0,100}?(debenture|securi|ncd)"):
            categories.add(ALLOTMENT_OF_DEBENTURES)
    if _anyinstr(blob, "preferential"):
        categories.add(PREFERENTIAL_ISSUE)
    if _anyregex(blob, r"qualif.{0,10}institu.{0,10}placement", r"\bqip\b", r"\bqib\b"):
        categories.add(QIP)

    # Merger / demerger / scheme of arrangement (tribunal/NCLT or scheme language).
    if _anyregex(blob, r"(tribunal|nclt)", r"scheme\s+of\s+arrangement", r"demerger", r"amalgamation"):
        categories.add(AMALGAMATION)

    if _anyregex(blob, r"buy[\s-]?back"):
        categories.add(BUYBACK)
    if _anyregex(blob, r"bonus\s*(issue)?.{0,50}(equity)?\s*share", r"bonus\s*issue"):
        categories.add(BONUS)
    if _anyinstr(blob, "dividend", "divident"):
        categories.add(DIVIDEND)

    # Credit rating (proximity: agency/term ... rating; rating ... change/outlook).
    # Tight proximity (agency/term near "rating", or rating-change/outlook): a bare "rating upgrade"
    # mention in an unrelated story must NOT classify (the VEDL ED-search false positive).
    if _anyregex(
        blob,
        r"(credit|care|moody|crisil|icra|fitch|india ratings|short term|long term).{0,50}rating",
        r"rating.{0,50}(credit|care|moody|crisil|icra|fitch|india ratings|short term|long term)",
        r"rating.{0,50}(change|outlook)",
        r"(change|outlook).{0,50}rating",
    ):
        categories.add(CREDIT_RATING)

    if _anyregex(blob, r"default.{0,100}(payment|interest|loan|repay)"):
        categories.add(LOAN_DEFAULT)
    if _anyinstr(blob, "resignation", "resigned", "appoint", "re-appoint"):
        categories.add(PERSONNEL_CHANGE)
    if _allregex(blob, r"\bsast\b"):
        categories.add(SAST)
    if _anyregex(blob, r"sharehold.{0,30}(pattern|table)"):
        categories.add(SHAREHOLDING)
    if _anyregex(blob, r"change in rta"):
        categories.add(CHANGE_IN_RTA)

    if _anyregex(blob, r"financ.{0,100}(result|statement|report)"):
        categories.add(FINANCIAL_RESULT)
    if _anyregex(
        blob,
        r"(investor|analyst).{0,50}(call|meet)",
        r"(transcript|conference).{0,50}(call|meet|transcript)",
        r"call.{0,50}transcript",
        r"earnings.{0,50}call",
    ):
        categories.add(EARNINGS_CALL)
    if _anyregex(blob, r"(inti|notice).{0,100}meeting", r"meeting.{0,100}(schedul|inti|notice)"):
        categories.add(INTIMATION_MEETING)

    return categories or {IGNORE}
