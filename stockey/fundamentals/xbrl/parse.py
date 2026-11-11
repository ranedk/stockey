"""Read one XBRL result filing into plain facts (docs/XBRL_FUNDAMENTALS.md, "Traps found").

- The period of a context comes from the DateOfStart/EndOfReportingPeriod facts filed inside
  it, not from the context element: old filings label the year-to-date context with the
  quarter's dates.
- A fact whose context is not defined is still read (an HDFC Bank filing refers to an
  undefined "OneD"); its period then comes from those same facts, or from the id's convention.
- Facts in a context with dimensions (segments, "other expenses 01..N") are kept apart; the
  core statements use only the plain contexts.
Pure: no database, no network.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from lxml import etree

XBRLI = "{http://www.xbrl.org/2003/instance}"
XLINK = "{http://www.w3.org/1999/xlink}"
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, remove_comments=True)


@dataclass
class Fact:
    tag: str
    context: str
    value: float | None
    text: str | None
    unit: str | None
    decimals: str | None


@dataclass
class Context:
    id: str
    start: date | None = None
    end: date | None = None
    instant: date | None = None
    dims: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Filing:
    taxonomy: str                       # e.g. "Ind-AS_entry_point", "in-capmkt-ent"
    taxonomy_version: str | None
    contexts: dict[str, Context]
    facts: list[Fact]
    meta: dict[str, str]                # text facts of the main context (basis, rounding, dates ...)

    def plain(self) -> dict[str, dict[str, float]]:
        """{context id: {tag: value}} for numeric facts in contexts without dimensions. The
        first value of a repeated tag wins."""
        out: dict[str, dict[str, float]] = {}
        for f in self.facts:
            c = self.contexts.get(f.context)
            if f.value is None or (c is not None and c.dims):
                continue
            out.setdefault(f.context, {}).setdefault(f.tag, f.value)
        return out


def _date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s.strip()[:10])
    except ValueError:
        return None


def _num(s: str | None) -> float | None:
    if s is None:
        return None
    s = s.strip().replace(",", "")
    if not re.fullmatch(r"-?\d+(\.\d+)?([eE]-?\d+)?", s):
        return None
    return float(s)


def _quarter_start(end: date) -> date:
    m = end.month - 2
    return date(end.year if m > 0 else end.year - 1, m if m > 0 else m + 12, 1)


def parse(xml: bytes) -> Filing:
    root = etree.fromstring(xml, parser=_PARSER)
    href = next((e.get(XLINK + "href") for e in root if isinstance(e.tag, str) and e.tag.endswith("schemaRef")), "") or ""
    name = href.rsplit("/", 1)[-1]
    m = re.match(r"(.+?)[-_]?(\d{4}-\d{2}-\d{2})\.xsd$", name)
    taxonomy, version = (m.group(1), m.group(2)) if m else (name, None)

    contexts: dict[str, Context] = {}
    for c in root.iter(XBRLI + "context"):
        per = c.find(XBRLI + "period")
        ctx = Context(c.get("id"))
        if per is not None:
            ctx.start = _date(per.findtext(XBRLI + "startDate"))
            ctx.end = _date(per.findtext(XBRLI + "endDate"))
            ctx.instant = _date(per.findtext(XBRLI + "instant"))
        for d in c.iter():
            if isinstance(d.tag, str) and d.tag.endswith(("explicitMember", "typedMember")):
                ctx.dims.append((d.get("dimension", "").split(":")[-1], (d.text or "").strip().split(":")[-1]))
        contexts[ctx.id] = ctx

    facts = []
    for e in root:
        if not isinstance(e.tag, str) or e.get("contextRef") is None:
            continue
        tag = e.tag.split("}")[-1]
        text = (e.text or "").strip()
        facts.append(Fact(tag, e.get("contextRef"), _num(text) if e.get("unitRef") else None,
                          None if e.get("unitRef") else text[:500], e.get("unitRef"), e.get("decimals")))

    # the reporting-period facts inside each context are the truth about its period
    stated: dict[str, dict[str, date]] = {}
    for f in facts:
        if f.tag in ("DateOfStartOfReportingPeriod", "DateOfEndOfReportingPeriod") and _date(f.text):
            stated.setdefault(f.context, {})[f.tag] = _date(f.text)
    for cid, d in stated.items():
        ctx = contexts.setdefault(cid, Context(cid))
        if "DateOfStartOfReportingPeriod" in d and "DateOfEndOfReportingPeriod" in d and ctx.instant is None:
            ctx.start, ctx.end = d["DateOfStartOfReportingPeriod"], d["DateOfEndOfReportingPeriod"]

    main = "OneD" if any(f.context == "OneD" for f in facts) else None
    meta = {f.tag: f.text for f in facts if f.context == main and f.text}
    # Contexts used but not defined, or with no start date (old bank filings): the exchange's
    # template names them -- OneD the quarter, FourD the year to date, OneI the period end.
    end = _date(meta.get("DateOfEndOfReportingPeriod"))
    used = {f.context for f in facts}
    if end:
        fy_start = _date(meta.get("DateOfStartOfFinancialYear")) or \
            date(end.year if end.month >= 4 else end.year - 1, 4, 1)
        for cid, start in (("OneD", _quarter_start(end)), ("FourD", fy_start)):
            ctx = contexts.get(cid)
            if cid in used and (ctx is None or (ctx.start is None and ctx.instant is None) or
                                (cid in stated and "DateOfStartOfReportingPeriod" not in stated[cid])):
                contexts[cid] = Context(cid, start=start, end=end, dims=ctx.dims if ctx else [])
        if "OneI" in used and "OneI" not in contexts:
            contexts["OneI"] = Context("OneI", instant=end)
    return Filing(taxonomy, version, contexts, facts, meta)
