"""News tagging (reevaluation PRD 4.2): each Economic Times item (fundamentals_news_item) is
matched to universe companies and sectors, and only matched items are read by a model.

Two stages, cheapest first:
  1. MECHANICAL candidates. A company is a candidate when its normalised name appears as whole
     words in the title or description (single-word names only when distinctive: >= 6
     letters and not a generic word), or its NSE ticker appears as an upper-case token in the
     title (>= 4 letters). A sector is a candidate from the feed's `sector_hint`. Items with
     neither -- cricket, world news, most macro -- are marked `no_match` and never cost a call.
  2. One MODEL call per batch of candidate items: for each item, which candidate companies it
     is actually ABOUT (a list of names in a market round-up is not news about them), the
     sector it bears on, direction, the dimension it touches, and materiality 1-5. The model
     may only pick from the candidates it is shown; it cannot add a company.

Output: `fundamentals_news_tag`, one row per (item, company) or (item, sector), dated by the
item's first_seen_at (the point-in-time clock). The story score reads company tags into the
events dimension and sector tags into the sector dimension; the re-evaluation job enqueues the
companies they touch.

    python -m fundamentals.screens.news_tagging [--limit N]
"""

from __future__ import annotations

import argparse
import json
import re

import pandas as pd
from environs import Env
from openai import OpenAI

from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.fallback_telemetry import record_local_fallback_event
from utils.json_safe import dumps_strict

env = Env()
env.read_env()

SYNC_SOURCE_NAME = "fundamentals.screens.news_tagging"
NEWS_TABLE = "fundamentals_news_item"
TAGS_TABLE = "fundamentals_news_tag"
DEFAULT_MODEL = env("NEWS_TAGGING_MODEL", "gpt-5.4-mini")
BATCH_SIZE = 15
STOCKEY_RUN_STATE: dict[str, object] = {}

DIMENSIONS = ["growth", "margins", "balance_sheet", "value", "ownership", "sector", "governance", "events", "none"]
# Single-word names that are ordinary words in news copy.
GENERIC_WORDS = frozenset({
    "global", "capital", "finance", "power", "energy", "infra", "steel", "metals", "motors", "foods", "pharma",
    "healthcare", "industries", "enterprises", "holdings", "ventures", "systems", "solutions", "technologies",
    "digital", "network", "gold", "silver", "diamond", "ocean", "sun", "star", "prime", "future", "active",
    "premier", "national", "united", "standard", "general", "federal", "central", "western", "eastern",
    "southern", "northern", "royal", "supreme", "super", "classic", "modern", "orient", "oriental", "indian",
    "bharat", "hindustan", "gujarat", "maharashtra", "kerala", "punjab", "bombay", "madras", "delhi",
})

# screener.in short names that are ordinary phrases ("European Central Bank", "Samsung Life
# Insurance"): these companies match by ticker only.
PHRASE_NAMES = frozenset({"central bank", "life insurance"})

_DDL = f"""
    CREATE TABLE IF NOT EXISTS {TAGS_TABLE} (
        link TEXT NOT NULL,
        target_type TEXT NOT NULL,
        target_id TEXT NOT NULL,
        target_name TEXT,
        direction TEXT,
        dimension TEXT,
        materiality INTEGER,
        first_seen_at TIMESTAMPTZ,
        model TEXT,
        tagged_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (link, target_type, target_id)
    )
"""

SYSTEM_PROMPT = """You tag Indian business news for a long-term fundamental stock screen.
For EACH item you get its title, description, feed, candidate companies (name + id) and a
candidate sector. Decide:
- companies: ids of candidates the item is ABOUT -- news that bears on that company's business,
  results, orders, management, balance sheet, ownership or regulation. A company merely listed
  in a round-up (top gainers, 52-week lows, "stocks to watch") or a broker price target alone is
  NOT about it: leave it out. Only ids from the item's candidates.
- sector_code: the ONE sector (code from the sector list; the feed's hint is only a hint) whose
  economics the item bears on -- demand, prices, regulation, capacity, input costs -- else null.
  Daily price chatter with no bearing on businesses is null.
- direction for the business (not the share price): positive, negative or neutral.
- dimension it touches most: one of growth, margins, balance_sheet, value, ownership, sector,
  governance, events, none.
- materiality 1-5 for a long-term holder: 1 trivial / routine, 3 worth knowing, 5 changes the
  investment case.
Return one entry per item, same order, echoing its link."""

# Sharpely/NSE sector codes (fundamentals_company_sector's scheme).
SECTORS = {
    "IN0101": "Chemicals", "IN0102": "Construction Materials", "IN0103": "Metals & Mining",
    "IN0104": "Forest Materials", "IN0201": "Automobile and Auto Components", "IN0202": "Consumer Durables",
    "IN0203": "Textiles", "IN0204": "Media, Entertainment & Publication", "IN0205": "Realty",
    "IN0206": "Consumer Services", "IN0301": "Oil, Gas & Consumable Fuels", "IN0401": "Fast Moving Consumer Goods",
    "IN0501": "Financial Services", "IN0601": "Healthcare", "IN0701": "Construction", "IN0702": "Capital Goods",
    "IN0801": "Information Technology", "IN0901": "Services", "IN1001": "Telecommunication", "IN1101": "Power",
    "IN1102": "Utilities", "IN1201": "Diversified",
}

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "link": {"type": "string"},
                    "companies": {"type": "array", "items": {"type": "string"}},
                    "sector_code": {"type": ["string", "null"], "enum": [*SECTORS, None]},
                    "direction": {"type": "string", "enum": ["positive", "negative", "neutral"]},
                    "dimension": {"type": "string", "enum": DIMENSIONS},
                    "materiality": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
                },
                "required": ["link", "companies", "sector_code", "direction", "dimension", "materiality"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["items"],
    "additionalProperties": False,
}


def ensure_tables() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute(_DDL)
            cur.execute(f"ALTER TABLE {NEWS_TABLE} ADD COLUMN IF NOT EXISTS tag_status TEXT")
            cur.execute(f"ALTER TABLE {NEWS_TABLE} ADD COLUMN IF NOT EXISTS tagged_at TIMESTAMPTZ")

    execute_db_operation(_op, operation_name=f"{TAGS_TABLE}:ensure_tables")


# --- stage 1: mechanical candidates ------------------------------------------------------------

def load_companies() -> pd.DataFrame:
    """Latest universe: company_master_id, name, ticker, sector_code."""
    from utils.company_master import map_company_master_ids_nse_or_bse

    u = sql_to_df(
        "SELECT company_name, ticker FROM fundamentals_l1_universe "
        "WHERE run_date = (SELECT max(run_date) FROM fundamentals_l1_universe)")
    if u.empty:
        return u
    u["company_master_id"] = map_company_master_ids_nse_or_bse(u["ticker"].astype("string")).to_numpy()
    sectors = sql_to_df("SELECT company_master_id, sector_code FROM fundamentals_company_sector")
    return u.dropna(subset=["company_master_id"]).merge(sectors, on="company_master_id", how="left")


def match_text(value) -> str:
    """Lower-case words for matching. Unlike universe.normalise_company_name this KEEPS
    "india" / "corporation": stripped, "Bank of India" became "bank of" and matched every
    Reserve Bank of India story, and "Central Bank" matched every central-bank story."""
    s = str(value or "").lower().replace("&amp;", "&")
    s = re.sub(r"\(formerly.*$", "", s).replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\bcorpn\b", "corporation", s)
    s = re.sub(r"\b(limited|ltd|pvt|private)\b", " ", s)
    s = re.sub(r"^\s*the\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def build_matchers(companies: pd.DataFrame) -> list[tuple[re.Pattern, re.Pattern | None, str, str]]:
    out = []
    for r in companies.itertuples():
        words = match_text(r.company_name).split()
        while words and words[-1] in ("of", "and", "co", "company"):
            words = words[:-1]
        name_rx = None
        if " ".join(words) in PHRASE_NAMES:
            pass
        elif len(words) >= 2 or (len(words) == 1 and len(words[0]) >= 6 and words[0] not in GENERIC_WORDS):
            # "reserve / state bank of india" is never the listed Bank of India
            name_rx = re.compile(r"(?<!reserve )(?<!state )\b" + r"\s+".join(map(re.escape, words)) + r"\b")
        ticker = str(r.ticker or "")
        ticker_rx = re.compile(r"(?<![A-Za-z0-9])" + re.escape(ticker) + r"(?![A-Za-z0-9])") \
            if len(ticker) >= 4 and ticker.isalpha() else None
        if name_rx or ticker_rx:
            out.append((name_rx, ticker_rx, r.company_master_id, r.company_name))
    return out


def candidates_for(title: str, description: str, matchers) -> list[tuple[str, str]]:
    text = match_text(f"{title} {description}")
    found = {}
    for name_rx, ticker_rx, cmid, cname in matchers:
        if (name_rx is not None and name_rx.search(text)) or (ticker_rx is not None and ticker_rx.search(title or "")):
            found[cmid] = cname
    return list(found.items())


# --- stage 2: the model read ------------------------------------------------------------------

def classify(batch: list[dict], *, model: str = DEFAULT_MODEL) -> list[dict]:
    client = OpenAI(api_key=env("OPENAI_API_KEY"))
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": SYSTEM_PROMPT},
                  {"role": "user", "content": dumps_strict({"sectors": SECTORS, "items": batch})}],
        response_format={"type": "json_schema", "json_schema": {"name": "news_tags", "schema": SCHEMA, "strict": True}},
    )
    return json.loads(response.choices[0].message.content)["items"]


def tag_rows(item: dict, verdict: dict, *, model: str) -> list[dict]:
    """Keep only what the item's candidates allow -- the model cannot add a company or sector."""
    allowed = {c["id"]: c["name"] for c in item["candidate_companies"]}
    base = {"link": item["link"], "direction": verdict["direction"], "dimension": verdict["dimension"],
            "materiality": int(verdict["materiality"]), "first_seen_at": item["first_seen_at"], "model": model}
    rows = [{**base, "target_type": "company", "target_id": cid, "target_name": allowed[cid]}
            for cid in dict.fromkeys(verdict.get("companies") or []) if cid in allowed]
    sector = verdict.get("sector_code")
    if sector in SECTORS:
        rows.append({**base, "target_type": "sector", "target_id": sector, "target_name": None})
    return rows


def _set_status(links: list[str], status: str) -> None:
    if not links:
        return

    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute(f"UPDATE {NEWS_TABLE} SET tag_status = %s, tagged_at = now() WHERE link = ANY(%s)",  # noqa: S608
                        (status, links))

    execute_db_operation(_op, operation_name=f"{NEWS_TABLE}:tag_status")


def run(limit: int | None = None, *, model: str = DEFAULT_MODEL) -> dict[str, object]:
    ensure_tables()
    items = sql_to_df(
        f"SELECT link, feed, sector_hint, title, description, first_seen_at FROM {NEWS_TABLE} "  # noqa: S608
        "WHERE tag_status IS NULL OR tag_status = 'failed' ORDER BY first_seen_at LIMIT %s",
        params=(limit or 100000,))
    if items.empty:
        return {"items": 0, "matched": 0, "tags": 0, "failed_batches": 0}
    matchers = build_matchers(load_companies())
    matched, unmatched = [], []
    for r in items.itertuples():
        cands = candidates_for(r.title or "", r.description or "", matchers)
        hint = r.sector_hint if isinstance(r.sector_hint, str) and r.sector_hint else None
        if not cands and not hint:
            unmatched.append(r.link)
            continue
        matched.append({"link": r.link, "feed": r.feed, "title": r.title, "description": (r.description or "")[:600],
                        "candidate_companies": [{"id": c, "name": n} for c, n in cands],
                        "candidate_sector": hint, "first_seen_at": r.first_seen_at})
    _set_status(unmatched, "no_match")

    tags, failed = 0, 0
    for i in range(0, len(matched), BATCH_SIZE):
        batch = matched[i:i + BATCH_SIZE]
        prompt_batch = [{k: v for k, v in m.items() if k != "first_seen_at"} for m in batch]
        try:
            verdicts = {v["link"]: v for v in classify(prompt_batch, model=model)}
        except Exception as exc:  # noqa: BLE001 -- a failed batch is retried next run
            failed += 1
            _set_status([m["link"] for m in batch], "failed")
            record_local_fallback_event(module=SYNC_SOURCE_NAME, source="openai", fallback_type="news_tagging_failed",
                                        severity="warn", reason="news tagging batch failed; retried next run",
                                        error=repr(exc), metadata={"items": len(batch)})
            continue
        rows = [row for m in batch if m["link"] in verdicts for row in tag_rows(m, verdicts[m["link"]], model=model)]
        if rows:
            upsert_to_db(pd.DataFrame(rows), TAGS_TABLE, unique_keys=["link", "target_type", "target_id"])
            tags += len(rows)
        _set_status([m["link"] for m in batch if m["link"] in verdicts], "tagged")
        _set_status([m["link"] for m in batch if m["link"] not in verdicts], "failed")
    return {"items": len(items), "matched": len(matched), "tags": tags, "failed_batches": failed}


def main(argv: list[str] | None = None) -> int:
    global STOCKEY_RUN_STATE
    parser = argparse.ArgumentParser(description="Tag ET news to universe companies and sectors.")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    result = run(args.limit)
    STOCKEY_RUN_STATE = {"source": SYNC_SOURCE_NAME, "rows": result["tags"], "rows_written": result["tags"], **result,
                         "fallback_used": result["failed_batches"] > 0, "state_advanced": result["items"] > 0}
    print(json.dumps({"status": "ok", **STOCKEY_RUN_STATE}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
