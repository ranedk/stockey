from __future__ import annotations

import re
from dataclasses import dataclass


MOVING_AVERAGE_ALIAS_RE = re.compile(
    r"\b(?P<days>20|30|50|100|150|200)\s*(?:day|days)\s+moving\s+average\b",
    flags=re.IGNORECASE,
)
COMPACT_DMA_RE = re.compile(r"\bDMA(?P<days>20|30|50|100|150|200)\b", flags=re.IGNORECASE)


@dataclass(frozen=True)
class QueryValidationIssue:
    code: str
    text: str
    suggestion: str


class ScreenerQueryValidationError(ValueError):
    def __init__(self, issues: list[QueryValidationIssue]):
        self.issues = issues
        message = "; ".join(f"{issue.code}: {issue.text} -> {issue.suggestion}" for issue in issues)
        super().__init__(f"Invalid Screener.in query syntax: {message}")


def validate_screener_query(query_text: str) -> list[QueryValidationIssue]:
    text = str(query_text or "")
    issues: list[QueryValidationIssue] = []
    seen: set[tuple[str, str]] = set()

    for match in MOVING_AVERAGE_ALIAS_RE.finditer(text):
        days = match.group("days")
        key = ("moving_average_alias", days)
        if key in seen:
            continue
        seen.add(key)
        issues.append(
            QueryValidationIssue(
                code="moving_average_alias",
                text=match.group(0),
                suggestion=f"Use `DMA {days}` instead of `{match.group(0)}`.",
            )
        )

    for match in COMPACT_DMA_RE.finditer(text):
        days = match.group("days")
        key = ("compact_dma", days)
        if key in seen:
            continue
        seen.add(key)
        issues.append(
            QueryValidationIssue(
                code="compact_dma",
                text=match.group(0),
                suggestion=f"Use `DMA {days}` with a space.",
            )
        )

    return issues


def assert_valid_screener_query(query_text: str) -> None:
    issues = validate_screener_query(query_text)
    if issues:
        raise ScreenerQueryValidationError(issues)
