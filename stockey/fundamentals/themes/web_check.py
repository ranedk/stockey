"""Web check of every stock on the theme watchlist (docs/THEMATIC_WATCHLIST_PRD.md section 6:
"every stock on the final watchlist gets a web check, shown with its sources: it catches what
no filing yet says, e.g. a business sold last month").

Per (company, theme) on a shown theme's list, the model searches the web (Claude CLI with
WebSearch and WebFetch only; no other tools, no files) and answers, each point with the URLs
it used:
  - is the company still in the theme's business, and its share of revenue from it;
  - anything in the last 12 months that changes that (a business sold, demerged or bought, a
    plant shut, a key contract lost);
  - governance red flags in the last 5 years (fraud or forensic-audit findings, ED/CBI/SEBI/RBI
    action, auditor resignation or a qualified opinion, a default, promoter arrest).
Verdict: clear / watch (worth knowing, not disqualifying) / red_flag (a serious, documented
problem). A red_flag removes the company from the list on the next ranking, with the finding
and its sources shown; watch is a note. A verdict with no source URL is stored as failed and
retried. Each check is kept RECHECK_DAYS; a company newly on the list is checked on the next
run.

Calls run in the 22:00-08:00 IST night window unless --any-time.

    python -m fundamentals.themes.web_check [--any-time] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone

import pandas as pd

from fundamentals.themes import catalog
from fundamentals.themes.tagger import CLAUDE_BIN, MODEL
from fundamentals.trigger_study.llm_runner import _LIMIT_RX, _RATE_RX, in_night_window
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db

TABLE = "fundamentals_theme_web_check"
# 2 (2026-10-31): the hard red flags spelled out to match the PRD gate -- v1 called an
# income-tax finding of unaccounted sales and a qualified audit opinion "watch"
CHECK_VERSION = 2
SHARE_DISPUTE_PP = 25.0     # a web share this far below ours makes the share "not known" until resolved
RECHECK_DAYS = 60
CALL_TIMEOUT_S = 900
VERDICTS = ("clear", "watch", "red_flag")

_DDL = f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
        company_master_id TEXT NOT NULL, theme_id TEXT NOT NULL, checked_at TIMESTAMPTZ NOT NULL,
        check_version INT NOT NULL,
        status TEXT NOT NULL,              -- ok / failed
        verdict TEXT,                      -- clear / watch / red_flag
        in_theme BOOLEAN, share_estimate_pct DOUBLE PRECISION,
        summary TEXT, recent_changes TEXT, red_flags TEXT,
        sources TEXT,                      -- JSON list of URLs
        model TEXT, error TEXT,
        PRIMARY KEY (company_master_id, theme_id, checked_at)
    )"""

PROMPT = """You check one Indian listed company for an investor's thematic watchlist, using web search and web fetch. Read public pages only: company website, annual report, investor presentation, stock-exchange filings, credit-rating rationales, reputable news. Never submit forms, log in, or send any personal information.

Answer three questions about the company named in the input, for the theme given (name and definition):
1. Is it in the theme's business today, and roughly what share of its revenue comes from products or services inside the theme's definition? Give the source.
2. In the last 12 months, did anything change that: a business sold, demerged or bought, a plant shut, a key contract or licence lost, a big new line outside the theme?
3. In the last 5 years, any serious governance problem: fraud allegations or forensic-audit findings, ED/CBI/income-tax action with findings, SEBI/RBI enforcement against the company or its promoters, statutory auditor resignation or a qualified opinion, a loan default or a rating cut to default, promoter arrest?

Verdict (it decides whether the company stays on the list):
- "red_flag" -- any of these in the last 5 years, documented by a credible source: a qualified, adverse or disclaimer audit opinion (on standalone or consolidated accounts); a statutory auditor resignation; income-tax, ED, CBI or DRI action WITH findings (e.g. the department said it found unaccounted sales or undisclosed income), not a search alone; fraud allegations confirmed by a regulator, court or forensic audit; a SEBI or RBI order against the company or its promoters (an order against unrelated traders in its shares does not count); a loan default or a rating cut to default; a promoter arrested or charged.
- "watch" -- worth knowing but not disqualifying: a search or notice with no finding yet, a routine tax dispute or demand under appeal, an allegation without findings, a pending case, a business change that lowers the theme share, a large acquisition or demerger.
- "clear" -- nothing of the above. "in_theme" false if the company is no longer in the theme's business. Every point must come from a page you read; list all URLs used in "sources". Plain, short sentences."""

SCHEMA = {"type": "object", "properties": {
    "verdict": {"type": "string", "enum": list(VERDICTS)},
    "in_theme": {"type": "boolean"},
    "share_estimate_pct": {"type": ["number", "null"]},
    "summary": {"type": "string"},
    "recent_changes": {"type": "string"},
    "red_flags": {"type": "string"},
    "sources": {"type": "array", "items": {"type": "string"}}},
    "required": ["verdict", "in_theme", "summary", "recent_changes", "red_flags", "sources"]}


def ensure_table() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute(_DDL)

    execute_db_operation(_op, operation_name=f"{TABLE}:ensure_table")


def table_exists() -> bool:
    return sql_to_df(f"SELECT to_regclass('{TABLE}') AS t")["t"].iloc[0] is not None  # noqa: S608


def call(item: dict) -> dict:
    cmd = [CLAUDE_BIN, "-p", "--model", MODEL, "--tools", "WebSearch,WebFetch", "--allowedTools", "WebSearch,WebFetch",
           "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence",
           "--system-prompt", PROMPT, "--json-schema", json.dumps(SCHEMA), "--output-format", "json"]
    with tempfile.TemporaryDirectory(prefix="theme_web_check_") as cwd:
        try:
            proc = subprocess.run(cmd, input=json.dumps(item, ensure_ascii=False), capture_output=True, text=True,
                                  timeout=CALL_TIMEOUT_S, cwd=cwd, env={**os.environ, "MAX_THINKING_TOKENS": "0"})
        except subprocess.TimeoutExpired:
            return {"ok": False, "kind": "error", "message": f"timeout after {CALL_TIMEOUT_S}s"}
    try:
        d = json.loads((proc.stdout or "").strip())
    except json.JSONDecodeError:
        text = f"{proc.stdout} {proc.stderr}"
        kind = "limit" if _LIMIT_RX.search(text) else "rate" if _RATE_RX.search(text) else "error"
        return {"ok": False, "kind": kind, "message": text[:500]}
    out = d.get("structured_output")
    if d.get("is_error") or not isinstance(out, dict):
        text = str(d.get("result") or d.get("subtype"))
        kind = "limit" if _LIMIT_RX.search(text) else "rate" if _RATE_RX.search(text) else "error"
        return {"ok": False, "kind": kind, "message": text[:500]}
    return {"ok": True, "out": out}


def validate(out: dict) -> tuple[dict, str | None]:
    src = [s for s in (out.get("sources") or []) if isinstance(s, str) and s.startswith("http")]
    if out.get("verdict") not in VERDICTS:
        return {}, f"verdict {out.get('verdict')!r}"
    if not src:
        return {}, "no source URL"
    share = out.get("share_estimate_pct")
    return {"verdict": out["verdict"], "in_theme": bool(out.get("in_theme")),
            "share_estimate_pct": float(share) if isinstance(share, (int, float)) and 0 <= share <= 100 else None,
            "summary": out.get("summary"), "recent_changes": out.get("recent_changes"),
            "red_flags": out.get("red_flags"), "sources": json.dumps(src[:12])}, None


def due(limit: int | None = None) -> pd.DataFrame:
    """Stocks on a shown theme's list or "also strong" whose last good check is older than RECHECK_DAYS."""
    st = sql_to_df("""
        SELECT s.company_master_id, s.theme_id, s.company_name, s.ticker
          FROM fundamentals_theme_watchlist_stock s
          JOIN fundamentals_theme_watchlist_theme t USING (as_of_date, theme_version, theme_id, watchlist_version)
         WHERE s.as_of_date = (SELECT max(as_of_date) FROM fundamentals_theme_watchlist_stock)
           AND ((s.on_watchlist AND t.status = 'shown') OR s.also_strong)
         ORDER BY t.position, s.position""")
    if table_exists():
        last = sql_to_df(f"SELECT company_master_id, theme_id, max(checked_at) AS last FROM {TABLE}"  # noqa: S608
                         " WHERE status = 'ok' AND check_version = %(v)s GROUP BY 1, 2", params={"v": CHECK_VERSION})
        st = st.merge(last, on=["company_master_id", "theme_id"], how="left")
        fresh = pd.to_datetime(st["last"], utc=True) > pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=RECHECK_DAYS)
        st = st[~fresh.fillna(False)]
    return st.head(limit) if limit else st


def latest_checks() -> pd.DataFrame:
    """The newest good check per (company, theme), within 1.5 x RECHECK_DAYS; empty before the first."""
    if not table_exists():
        return pd.DataFrame(columns=["company_master_id", "theme_id", "verdict", "checked_at", "summary",
                                     "recent_changes", "red_flags", "sources", "share_estimate_pct", "in_theme"])
    return sql_to_df(f"""SELECT DISTINCT ON (company_master_id, theme_id) * FROM {TABLE}
                          WHERE status = 'ok' AND check_version = %(v)s
                            AND checked_at > now() - interval '{int(RECHECK_DAYS * 1.5)} days'
                          ORDER BY company_master_id, theme_id, checked_at DESC""", params={"v": CHECK_VERSION})  # noqa: S608


def run(limit: int | None = None, any_time: bool = False) -> dict:
    if not any_time and not in_night_window(datetime.now(timezone.utc)):
        return {"skipped": "outside the 22:00-08:00 IST window (use --any-time)"}
    ensure_table()
    themes = {t.id: t for t in catalog.load(1)}
    todo = due(limit)
    counts = {"due": len(todo), "ok": 0, "failed": 0}
    for r in todo.itertuples(index=False):
        t = themes[r.theme_id]
        res = call({"company": r.company_name, "ticker": r.ticker, "theme": t.name, "definition": t.definition,
                    "not_in_theme": list(t.exclude)})
        row = {"company_master_id": r.company_master_id, "theme_id": r.theme_id,
               "checked_at": pd.Timestamp.now(tz="UTC"), "check_version": CHECK_VERSION, "model": MODEL}
        if not res["ok"]:
            if res["kind"] == "limit":
                counts["stopped"] = res["message"][:200]
                break
            row.update(status="failed", error=res["message"])
        else:
            fields, err = validate(res["out"])
            row.update(status="failed", error=err) if err else row.update(status="ok", **fields)
        counts[row["status"]] += 1
        upsert_to_db(pd.DataFrame([row]), TABLE, ["company_master_id", "theme_id", "checked_at"])
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Web-check the stocks on the theme watchlist.")
    ap.add_argument("--any-time", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args(argv)
    print(json.dumps(run(limit=a.limit, any_time=a.any_time), default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
