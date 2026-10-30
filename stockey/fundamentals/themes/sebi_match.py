"""Match SEBI enforcement orders to listed companies by the name in the order's title, for the
theme watchlist's management gate (docs/THEMATIC_WATCHLIST_PRD.md section 4.4).

A company matches an order when its name (without "Ltd"/"Limited"/"Pvt") appears as whole
words in the order's "in the matter of ..." text. "Corporation"/"Company" stay in the name
(as "corp"/"co"): dropping them left "Delta Corp" as "delta", which matched "Delta Leasing and
Finance". The role says how it is named:
  subject  the matter is the company itself ("in the matter of Taurian MPS Ltd")
  scrip    the matter is trading in its shares ("insider trading in the scrip of Wipro Ltd")
  other    named some other way ("role of Axis Capital Limited in providing guarantee ...")
Promoters and directors are not matched yet: we hold no names for them.
"""
from __future__ import annotations

import re

import pandas as pd

_DROP = {"ltd", "limited", "the", "pvt", "private", "inc"}
_SAME = {"corporation": "corp", "company": "co"}
_SCRIP_RX = re.compile(r"\b(scrips?|shares?|securities|stocks?|trading|gdrs?|ipo|preferential allotment|unit holders?)\s+(?:issued\s+)?(?:of|in|by)\s*$")
MIN_KEY_CHARS = 5      # shorter names ("ITC", "UPL") are too often part of another name


def tokens(name: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", name.lower().replace("&", " and "))
    return [_SAME.get(t, t) for t in words if t not in _DROP]


def _find(needle: list[str], hay: list[str]) -> int:
    n = len(needle)
    for i in range(len(hay) - n + 1):
        if hay[i:i + n] == needle:
            return i
    return -1


def role_of(matter: str, name_tokens: list[str]) -> str | None:
    """How the company is named in the matter text, or None if it is not."""
    hay = tokens(matter)
    i = _find(name_tokens, hay)
    if i < 0:
        return None
    if i == 0:
        return "subject"
    # the words before the name, with "Ltd" etc. kept, to see "scrip of" / "shares of"
    words = [_SAME.get(t, t) for t in re.findall(r"[a-z0-9]+", matter.lower())]
    j = _find(name_tokens, words)
    before = " ".join(words[:j] if j >= 0 else words)
    return "scrip" if _SCRIP_RX.search(before) else "other"


def match_orders(orders: pd.DataFrame, companies: pd.DataFrame) -> pd.DataFrame:
    """orders: url, order_date, kind, matter. companies: company_master_id, company_name.
    One row per (order, company) match with its role."""
    names = []
    for c in companies.itertuples(index=False):
        tk = tokens(c.company_name)
        if tk and len("".join(tk)) >= MIN_KEY_CHARS:
            names.append((c.company_master_id, c.company_name, tk))
    out = []
    for o in orders.dropna(subset=["matter"]).itertuples(index=False):
        for cid, cname, tk in names:
            role = role_of(o.matter, tk)
            if role:
                out.append({"url": o.url, "order_date": o.order_date, "kind": o.kind, "matter": o.matter,
                            "company_master_id": cid, "company_name": cname, "role": role})
    return pd.DataFrame(out, columns=["url", "order_date", "kind", "matter", "company_master_id", "company_name", "role"])


def named_in(text: str | None, name_tokens: list[str]) -> bool:
    """Whether the company's name appears as whole words in a block of text (an order's parties)."""
    return bool(text) and _find(name_tokens, tokens(text)) >= 0
