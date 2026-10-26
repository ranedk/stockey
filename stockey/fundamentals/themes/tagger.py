"""Tags companies to the theme map (docs/THEMATIC_WATCHLIST_PRD.md section 4.2, step 2).

MSCI's design: the model judges ASSOCIATION, never invents the number.
  1. tag pass   -- for each company's stored screener.in profile, the model names the themes
                   it belongs to, with a verbatim evidence quote, and copies the revenue share
                   ONLY if the text states one (a segment share, or "the company's entire
                   business is ..."), with the verbatim sentence that states it.
  2. validation -- in code: an evidence or share quote that is not in the profile text is
                   dropped; a share whose number is not in its quote is dropped. A tag with no
                   surviving share is "unquantified" (watch-only, never ranked).
  3. check pass -- a second prompt argues against each surviving tag against the theme's
                   definition and exclusions; a rejection marks the tag "disputed" for the
                   operator's exceptions list instead of silently deleting it.

Calls go through the Claude CLI on the operator's plan, same flags as the trigger study's
runner (no tools, replaced system prompt, empty cwd), inside the 22:00-08:00 IST night window
unless --any-time. Each tag row keeps the profile date it read (`profile_fetched_on`), so a
tag is point-in-time to its evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone

import pandas as pd

from fundamentals.themes import catalog
from fundamentals.trigger_study.llm_runner import _LIMIT_RX, _RATE_RX, in_night_window
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db

TAG_TABLE = "fundamentals_theme_tag"
RUN_TABLE = "fundamentals_theme_tag_run"
PROFILE_TABLE = "fundamentals_company_profile"
CLAUDE_BIN = os.environ.get("THEME_TAGGER_CLAUDE_BIN", os.path.expanduser("~/.local/bin/claude"))
MODEL = os.environ.get("THEME_TAGGER_MODEL", "claude-sonnet-5-5")
BATCH = 5
TEXT_CHARS = 7000
CALL_TIMEOUT_S = 400

_DDL = [f"""
    CREATE TABLE IF NOT EXISTS {TAG_TABLE} (
        company_master_id TEXT NOT NULL,
        theme_version INT NOT NULL,
        theme_id TEXT NOT NULL,
        profile_fetched_on DATE NOT NULL,
        role TEXT,
        evidence_quote TEXT,
        exposure_pct DOUBLE PRECISION,
        exposure_quote TEXT,
        exposure_basis TEXT,
        status TEXT NOT NULL,          -- quantified | unquantified | disputed | invalid
        check_reason TEXT,
        dropped TEXT,                  -- what validation removed, if anything
        model TEXT NOT NULL,
        tagged_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (company_master_id, theme_version, theme_id, profile_fetched_on)
    )""", f"""
    CREATE TABLE IF NOT EXISTS {RUN_TABLE} (
        company_master_id TEXT NOT NULL,
        theme_version INT NOT NULL,
        profile_fetched_on DATE NOT NULL,
        n_tags INT NOT NULL,
        model TEXT NOT NULL,
        done_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (company_master_id, theme_version, profile_fetched_on)
    )"""]


def ensure_tables() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            for ddl in _DDL:
                cur.execute(ddl)

    execute_db_operation(_op, operation_name=f"{TAG_TABLE}:ensure_tables")


def _theme_brief(themes: list[catalog.Theme]) -> str:
    return "\n".join(
        f"- {t.id}: {t.definition}" + (f" NOT: {'; '.join(t.exclude)}." if t.exclude else "")
        for t in themes)


def tag_prompt(themes: list[catalog.Theme]) -> str:
    return f"""You classify Indian listed companies into investment themes from their business description.

THEMES (id: what a company must make, build or sell to count; NOT: look-alikes that do not count):
{_theme_brief(themes)}

For each company in the input, return the themes it genuinely belongs to (most companies belong to none or one).
Rules:
- A theme counts only if the company itself makes, builds, owns or sells what the definition says. Selling TO the theme's companies counts only if the definition says so.
- evidence_quote: copy one sentence or phrase VERBATIM from the company's text that shows it belongs. Never paraphrase.
- exposure_pct: the share of revenue from the theme ONLY when the text states it -- a segment's share (e.g. "Wires & Cables (87% in FY26)") or a statement that the whole business is the theme (e.g. "X is a defence-electronics company", "the company manufactures only solar modules" -- basis "whole_business", exposure_pct 100). Then copy the exact sentence that states it into exposure_quote and set exposure_basis to "segment_share" or "whole_business". If the text gives no number and does not say the whole business is the theme, set exposure_pct null, exposure_quote null, exposure_basis "none". Never estimate from your own knowledge.
- role: a few words on what it does in the theme (e.g. "makes TOPCon cells").
Return items in input order, one per company id, with an empty tags list when none apply."""


CHECK_PROMPT = """You are a sceptical reviewer of theme tags for Indian listed companies.
For each item you get a theme (id, definition, exclusions) and the evidence that a company belongs.
Argue against the tag: is this company really making/building/owning/selling what the definition says, or is it a look-alike, a customer, a distributor, a tiny side line, or excluded? Decide.
verdict "keep" if the tag holds; "reject" if it does not. reason: one short sentence."""

TAG_SCHEMA = {
    "type": "object", "required": ["items"],
    "properties": {"items": {"type": "array", "items": {
        "type": "object", "required": ["id", "tags"],
        "properties": {
            "id": {"type": "string"},
            "tags": {"type": "array", "items": {
                "type": "object",
                "required": ["theme_id", "role", "evidence_quote", "exposure_pct", "exposure_quote", "exposure_basis"],
                "properties": {
                    "theme_id": {"type": "string"},
                    "role": {"type": "string"},
                    "evidence_quote": {"type": "string"},
                    "exposure_pct": {"type": ["number", "null"]},
                    "exposure_quote": {"type": ["string", "null"]},
                    "exposure_basis": {"type": "string", "enum": ["segment_share", "whole_business", "none"]},
                }}},
        }}}},
}

CHECK_SCHEMA = {
    "type": "object", "required": ["items"],
    "properties": {"items": {"type": "array", "items": {
        "type": "object", "required": ["id", "verdict", "reason"],
        "properties": {"id": {"type": "string"}, "verdict": {"type": "string", "enum": ["keep", "reject"]},
                       "reason": {"type": "string"}}}}},
}


def call_claude(system_prompt: str, schema: dict, items: list[dict]) -> dict:
    cmd = [CLAUDE_BIN, "-p", "--model", MODEL, "--tools", "",
           "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence",
           "--system-prompt", system_prompt,
           "--json-schema", json.dumps(schema), "--output-format", "json"]
    with tempfile.TemporaryDirectory(prefix="theme_tagger_") as cwd:
        try:
            proc = subprocess.run(cmd, input=json.dumps(items, ensure_ascii=False), capture_output=True,
                                  text=True, timeout=CALL_TIMEOUT_S, cwd=cwd,
                                  env={**os.environ, "MAX_THINKING_TOKENS": "0"})
        except subprocess.TimeoutExpired:
            return {"ok": False, "kind": "error", "message": f"timeout after {CALL_TIMEOUT_S}s"}
    raw = (proc.stdout or "").strip()
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        text = (raw + " " + (proc.stderr or "")).strip()
        kind = "limit" if _LIMIT_RX.search(text) else "rate" if _RATE_RX.search(text) else "error"
        return {"ok": False, "kind": kind, "message": text[:1000] or f"exit {proc.returncode}"}
    if d.get("is_error") or d.get("subtype") != "success":
        text = str(d.get("result") or d.get("subtype") or "")
        kind = "limit" if _LIMIT_RX.search(text) else "rate" if _RATE_RX.search(text) else "error"
        return {"ok": False, "kind": kind, "message": text[:1000]}
    out = d.get("structured_output")
    if not isinstance(out, dict) or not isinstance(out.get("items"), list):
        return {"ok": False, "kind": "error", "message": "no structured_output.items"}
    return {"ok": True, "items": out["items"]}


# --- validation (pure) ----------------------------------------------------------------------

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("’", "'").replace("–", "-")).strip().lower()


def validate_tag(tag: dict, text: str, theme_ids: set[str]) -> tuple[dict | None, str | None]:
    """The tag as stored, or None when it cannot stand; plus a note of anything dropped."""
    if tag.get("theme_id") not in theme_ids:
        return None, f"unknown theme {tag.get('theme_id')!r}"
    body = _norm(text)
    ev = (tag.get("evidence_quote") or "").strip()
    if not ev or _norm(ev) not in body:
        return None, "evidence quote not in the profile text"
    out = {"theme_id": tag["theme_id"], "role": tag.get("role"), "evidence_quote": ev,
           "exposure_pct": None, "exposure_quote": None, "exposure_basis": "none"}
    pct, quote, basis = tag.get("exposure_pct"), (tag.get("exposure_quote") or "").strip(), tag.get("exposure_basis")
    dropped = None
    if basis in ("segment_share", "whole_business") and quote:
        if _norm(quote) not in body:
            dropped = "exposure quote not in the profile text"
        elif basis == "whole_business":
            out.update(exposure_pct=100.0, exposure_quote=quote, exposure_basis=basis)
        elif pct is None or not 0 < float(pct) <= 100:
            dropped = "exposure missing or out of range"
        elif not _number_in(pct, quote):
            dropped = "exposure number not in its quote"
        else:
            out.update(exposure_pct=float(pct), exposure_quote=quote, exposure_basis=basis)
    elif pct is not None:
        dropped = "exposure given without a quote"
    return out, dropped


def _number_in(pct: float, quote: str) -> bool:
    nums = {float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*%", quote)}
    return any(abs(n - float(pct)) < 0.51 for n in nums)


# --- run ------------------------------------------------------------------------------------

def due_profiles(version: int, limit: int | None, only: list[str] | None = None) -> pd.DataFrame:
    """Latest OK profile per company that this theme version has not tagged yet."""
    df = sql_to_df(f"""
        SELECT DISTINCT ON (p.company_master_id) p.company_master_id, p.fetched_on, p.ticker,
               c.company_name, p.peers_path, p.about, p.key_points
          FROM {PROFILE_TABLE} p JOIN company_master c USING (company_master_id)
         WHERE p.status = 'ok'
         ORDER BY p.company_master_id, p.fetched_on DESC""")
    done = sql_to_df(f"SELECT company_master_id, profile_fetched_on FROM {RUN_TABLE} WHERE theme_version = %(v)s",
                     params={"v": version})
    key = set(zip(done["company_master_id"], pd.to_datetime(done["profile_fetched_on"]).dt.date))
    df = df[[(c, pd.Timestamp(f).date()) not in key for c, f in zip(df["company_master_id"], df["fetched_on"])]]
    if only:
        df = df[df["ticker"].isin(only)]
    return df.head(limit) if limit else df


def profile_text(row) -> str:
    return "\n".join(x for x in (row.about, row.key_points) if x)[:TEXT_CHARS]


def run(*, version: int = 1, limit: int | None = None, any_time: bool = False, only: list[str] | None = None) -> dict:
    if not any_time and not in_night_window(datetime.now(timezone.utc)):
        return {"skipped": "outside the 22:00-08:00 IST window (use --any-time)"}
    ensure_tables()
    themes = catalog.load(version)
    by_id = {t.id: t for t in themes}
    todo = due_profiles(version, limit, only)
    counts = {"companies": 0, "tags": 0, "quantified": 0, "unquantified": 0, "disputed": 0, "rejected_by_validation": 0}
    system = tag_prompt(themes)
    for i in range(0, len(todo), BATCH):
        chunk = todo.iloc[i:i + BATCH]
        texts = {r.company_master_id: profile_text(r) for r in chunk.itertuples()}
        items = [{"id": r.company_master_id, "company": r.company_name, "screener_industry": r.peers_path,
                  "text": texts[r.company_master_id]} for r in chunk.itertuples()]
        res = call_claude(system, TAG_SCHEMA, items)
        if not res["ok"]:
            counts["stopped"] = f"{res['kind']}: {res['message'][:200]}"
            break
        tagged = {str(x.get("id")): x.get("tags") or [] for x in res["items"]}
        rows, checks = [], []
        for r in chunk.itertuples():
            for tag in tagged.get(r.company_master_id, []):
                t, dropped = validate_tag(tag, texts[r.company_master_id], set(by_id))
                if t is None:
                    # kept, not silently dropped: the operator's exceptions list shows these
                    counts["rejected_by_validation"] += 1
                    t = {"theme_id": str(tag.get("theme_id")), "role": tag.get("role"),
                         "evidence_quote": tag.get("evidence_quote"), "exposure_pct": None,
                         "exposure_quote": tag.get("exposure_quote"), "exposure_basis": "none"}
                    rows.append({"company_master_id": r.company_master_id, "theme_version": version,
                                 "profile_fetched_on": r.fetched_on, "dropped": dropped, "model": MODEL,
                                 "status": "invalid", "check_reason": None, **t})
                    continue
                row = {"company_master_id": r.company_master_id, "theme_version": version,
                       "profile_fetched_on": r.fetched_on, "dropped": dropped, "model": MODEL,
                       "status": "quantified" if t["exposure_pct"] is not None else "unquantified",
                       "check_reason": None, **t}
                rows.append(row)
                th = by_id[t["theme_id"]]
                checks.append({"id": f"{len(rows) - 1}", "company": r.company_name, "theme_id": th.id,
                               "definition": th.definition, "exclusions": list(th.exclude),
                               "role": t["role"], "evidence": t["evidence_quote"]})
        if checks:
            chk = call_claude(CHECK_PROMPT, CHECK_SCHEMA, checks)
            if not chk["ok"]:
                counts["stopped"] = f"check {chk['kind']}: {chk['message'][:200]}"
                break
            verdicts = {str(x.get("id")): x for x in chk["items"]}
            for k, row in enumerate(rows):
                if row["status"] == "invalid":
                    continue
                v = verdicts.get(str(k))
                if v is None:
                    row["status"], row["check_reason"] = "disputed", "no verdict returned"
                elif v["verdict"] == "reject":
                    row["status"], row["check_reason"] = "disputed", v["reason"]
                else:
                    row["check_reason"] = v["reason"]
        if rows:
            upsert_to_db(pd.DataFrame(rows), TAG_TABLE, ["company_master_id", "theme_version", "theme_id", "profile_fetched_on"])
        upsert_to_db(pd.DataFrame([{"company_master_id": r.company_master_id, "theme_version": version,
                                    "profile_fetched_on": r.fetched_on, "model": MODEL,
                                    "n_tags": sum(x["company_master_id"] == r.company_master_id for x in rows)}
                                   for r in chunk.itertuples()]),
                     RUN_TABLE, ["company_master_id", "theme_version", "profile_fetched_on"])
        counts["companies"] += len(chunk)
        counts["tags"] += len(rows)
        for row in rows:
            if row["status"] != "invalid":
                counts[row["status"]] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Tag companies to the theme map from their stored profiles.")
    ap.add_argument("--version", type=int, default=1)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--any-time", action="store_true", help="ignore the night window (pilots)")
    ap.add_argument("--only", nargs="*", help="only these tickers")
    a = ap.parse_args(argv)
    print(json.dumps(run(version=a.version, limit=a.limit, any_time=a.any_time, only=a.only), default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
