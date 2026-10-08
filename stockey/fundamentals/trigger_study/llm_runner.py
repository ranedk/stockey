"""Runs the trigger study's tagging batches through the Claude CLI on the operator's plan.

Why the CLI and not an API key: the operator asked (2026-10-08) to spend plan allowance, not
API money. Each call is `claude -p` with no tools, no MCP servers, no settings and a replaced
system prompt, from an empty directory -- measured overhead ~430 input tokens a call.

Usage limits: when the CLI reports the plan's limit, the runner records WHEN it resets in
fundamentals_trigger_llm_state and exits. Every later invocation (cron, every 15 minutes)
exits at once until then, so nothing is lost and nothing hammers the limit. A reset time it
cannot parse parks it for an hour, which costs one cheap refused call per hour at worst.

Night window: by default calls are made only 22:00-08:00 IST so the study does not eat the
operator's daytime allowance; `--any-time` overrides (the pilot).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from utils.db import db_session, sql_to_df

from fundamentals.trigger_study.prompts import PASS_PROMPTS, PASS_SCHEMAS

BATCHES_TABLE = "fundamentals_trigger_llm_batches"
TAGS_TABLE = "fundamentals_trigger_tags"
STATE_TABLE = "fundamentals_trigger_llm_state"

IST = ZoneInfo("Asia/Kolkata")
CLAUDE_BIN = os.environ.get("TRIGGER_STUDY_CLAUDE_BIN", os.path.expanduser("~/.local/bin/claude"))
DEFAULT_MODEL = os.environ.get("TRIGGER_STUDY_MODEL", "claude-haiku-4-5")
CALL_TIMEOUT_S = 300
MAX_ATTEMPTS = 3
NIGHT_START_H, NIGHT_END_H = 22, 8
UNPARSED_LIMIT_PARK = timedelta(hours=1)
RESET_BUFFER = timedelta(minutes=5)
TRANSIENT_PARK = timedelta(minutes=10)

_LIMIT_RX = re.compile(r"(usage limit|limit reached|hit your limit|session limit|weekly limit|out of extra usage)", re.I)
_RATE_RX = re.compile(r"(rate.?limit|overloaded|429|529)", re.I)


def ensure_tables() -> None:
    with db_session() as (_, cur):
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {BATCHES_TABLE} (
                batch_id TEXT PRIMARY KEY, pass TEXT NOT NULL, model TEXT NOT NULL,
                payload JSONB NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                attempts INT NOT NULL DEFAULT 0, last_error TEXT,
                input_tokens INT, output_tokens INT, list_cost_usd DOUBLE PRECISION,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(), done_at TIMESTAMPTZ
            )""")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {TAGS_TABLE} (
                filing_id TEXT NOT NULL, pass TEXT NOT NULL, batch_id TEXT NOT NULL,
                trigger TEXT NOT NULL, direction TEXT, value_cr DOUBLE PRECISION,
                importance INT, needs_document BOOLEAN, note TEXT, model TEXT,
                tagged_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (filing_id, pass)
            )""")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {STATE_TABLE} (
                id INT PRIMARY KEY DEFAULT 1, resume_at TIMESTAMPTZ, reason TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )""")


# --- limit handling -----------------------------------------------------------------------

def parse_reset(message: str, now: datetime) -> datetime | None:
    """When the CLI says the limit resets, as an aware UTC datetime; None if it does not say.

    Seen forms: 'Claude AI usage limit reached|1760000000' (epoch), '... resets 3am',
    '... resets 4:30pm (Asia/Calcutta)', '... resets Oct 12, 3pm (Asia/Calcutta)'."""
    m = re.search(r"\|(\d{10})\b", message)
    if m:
        return datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc)
    tz_m = re.search(r"\(([A-Za-z_]+/[A-Za-z_]+)\)", message)
    try:
        tz = ZoneInfo(tz_m.group(1)) if tz_m else IST
    except Exception:  # noqa: BLE001 -- unknown zone name: assume the operator's
        tz = IST
    local_now = now.astimezone(tz)
    m = re.search(r"resets\s+(?:at\s+)?(?:([A-Z][a-z]{2})\s+(\d{1,2}),?\s+(?:at\s+)?)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)", message, re.I)
    if not m:
        return None
    mon, day, hh, mm, ap = m.groups()
    hour = int(hh) % 12 + (12 if ap.lower() == "pm" else 0)
    minute = int(mm or 0)
    if mon:
        month = datetime.strptime(mon, "%b").month
        cand = local_now.replace(month=month, day=int(day), hour=hour, minute=minute, second=0, microsecond=0)
        if cand < local_now - timedelta(days=1):
            cand = cand.replace(year=cand.year + 1)
    else:
        cand = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if cand <= local_now:
            cand += timedelta(days=1)
    return cand.astimezone(timezone.utc)


def _park(until: datetime, reason: str) -> None:
    with db_session() as (_, cur):
        cur.execute(f"""
            INSERT INTO {STATE_TABLE} (id, resume_at, reason, updated_at) VALUES (1, %s, %s, now())
            ON CONFLICT (id) DO UPDATE SET resume_at = EXCLUDED.resume_at, reason = EXCLUDED.reason,
                                           updated_at = now()""", (until, reason[:500]))


def parked_until(now: datetime) -> tuple[datetime | None, str | None]:
    df = sql_to_df(f"SELECT resume_at, reason FROM {STATE_TABLE} WHERE id = 1")
    if df.empty or pd.isna(df.iloc[0]["resume_at"]):
        return None, None
    at = pd.Timestamp(df.iloc[0]["resume_at"]).to_pydatetime()
    return (at, df.iloc[0]["reason"]) if at > now else (None, None)


def in_night_window(now: datetime) -> bool:
    h = now.astimezone(IST).hour
    return h >= NIGHT_START_H or h < NIGHT_END_H


# --- one call -------------------------------------------------------------------------------

def call_claude(pass_name: str, items: list[dict], model: str) -> dict:
    """Returns {'ok': True, 'items': [...], usage...} or {'ok': False, 'kind': 'limit'|'rate'|'error', 'message'}."""
    cmd = [CLAUDE_BIN, "-p", "--model", model, "--tools", "",
           "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence",
           "--system-prompt", PASS_PROMPTS[pass_name],
           "--json-schema", json.dumps(PASS_SCHEMAS[pass_name]), "--output-format", "json"]
    with tempfile.TemporaryDirectory(prefix="trigger_study_") as cwd:
        try:
            # thinking off: on a 20-item batch it cut 127 s / 11,495 output tokens to 19 s / 2,043
            # (measured 2026-10-08) -- this is classification against a fixed list, not reasoning
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
        return {"ok": False, "kind": kind, "message": text[:1000] or f"exit {proc.returncode}, no output"}
    if d.get("is_error") or d.get("subtype") != "success":
        text = str(d.get("result") or d.get("subtype") or "") + " " + str(d.get("api_error_status") or "")
        kind = "limit" if _LIMIT_RX.search(text) else "rate" if _RATE_RX.search(text) else "error"
        return {"ok": False, "kind": kind, "message": text[:1000]}
    out = d.get("structured_output")
    if not isinstance(out, dict) or not isinstance(out.get("items"), list):
        return {"ok": False, "kind": "error", "message": "no structured_output.items"}
    usage = d.get("usage") or {}
    return {"ok": True, "items": out["items"], "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"), "cost": d.get("total_cost_usd")}


def _store(batch_id: str, pass_name: str, model: str, sent: list[dict], res: dict) -> int:
    sent_ids = {str(x["id"]) for x in sent}
    rows = []
    for it in res["items"]:
        fid = str(it.get("id"))
        if fid not in sent_ids:
            continue
        rows.append((fid, pass_name, batch_id, it.get("trigger") or "other_material", it.get("direction"),
                     it.get("value_cr"), it.get("importance"), it.get("needs_document"),
                     (it.get("note") or "")[:300], model))
    with db_session() as (_, cur):
        for r in rows:
            cur.execute(f"""
                INSERT INTO {TAGS_TABLE} (filing_id, pass, batch_id, trigger, direction, value_cr,
                                          importance, needs_document, note, model)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (filing_id, pass) DO UPDATE SET batch_id=EXCLUDED.batch_id,
                  trigger=EXCLUDED.trigger, direction=EXCLUDED.direction, value_cr=EXCLUDED.value_cr,
                  importance=EXCLUDED.importance, needs_document=EXCLUDED.needs_document,
                  note=EXCLUDED.note, model=EXCLUDED.model, tagged_at=now()""", r)
        missing = len(sent_ids) - len(rows)
        cur.execute(f"""
            UPDATE {BATCHES_TABLE} SET status='done', done_at=now(), attempts=attempts+1,
                   input_tokens=%s, output_tokens=%s, list_cost_usd=%s, last_error=%s
             WHERE batch_id=%s""",
                    (res.get("input_tokens"), res.get("output_tokens"), res.get("cost"),
                     f"{missing} item(s) not returned" if missing else None, batch_id))
    return len(rows)


def run(*, max_calls: int, any_time: bool = False, pass_name: str | None = None) -> dict:
    ensure_tables()
    now = datetime.now(timezone.utc)
    until, reason = parked_until(now)
    if until:
        return {"status": "parked", "resume_at": until.isoformat(), "reason": reason}
    if not any_time and not in_night_window(now):
        return {"status": "outside_window"}
    where = "status='pending'" + (" AND pass=%s" if pass_name else "")
    pending = sql_to_df(f"SELECT batch_id, pass, model, payload FROM {BATCHES_TABLE} WHERE {where} "
                        f"ORDER BY pass, batch_id LIMIT %s", params=((pass_name, max_calls) if pass_name else (max_calls,)))
    calls = tagged = 0
    for _, b in pending.iterrows():
        items = b["payload"] if isinstance(b["payload"], list) else json.loads(b["payload"])
        res = call_claude(b["pass"], items, b["model"])
        calls += 1
        if res["ok"]:
            tagged += _store(b["batch_id"], b["pass"], b["model"], items, res)
            continue
        if res["kind"] == "limit":
            at = parse_reset(res["message"], datetime.now(timezone.utc))
            until = (at + RESET_BUFFER) if at else datetime.now(timezone.utc) + UNPARSED_LIMIT_PARK
            _park(until, res["message"])
            return {"status": "limit", "calls": calls, "tagged": tagged, "resume_at": until.isoformat(),
                    "message": res["message"][:200]}
        if res["kind"] == "rate":
            _park(datetime.now(timezone.utc) + TRANSIENT_PARK, res["message"])
            return {"status": "rate_limited", "calls": calls, "tagged": tagged}
        with db_session() as (_, cur):
            cur.execute(f"""UPDATE {BATCHES_TABLE} SET attempts=attempts+1, last_error=%s,
                              status=CASE WHEN attempts+1 >= %s THEN 'failed' ELSE 'pending' END
                            WHERE batch_id=%s""", (res["message"][:1000], MAX_ATTEMPTS, b["batch_id"]))
    left = int(sql_to_df(f"SELECT count(*) AS n FROM {BATCHES_TABLE} WHERE status='pending'").iloc[0]["n"])
    return {"status": "ok", "calls": calls, "tagged": tagged, "pending_left": left}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Tag trigger-study batches through the Claude CLI.")
    ap.add_argument("--max-calls", type=int, default=40)
    ap.add_argument("--any-time", action="store_true", help="ignore the 22:00-08:00 IST night window")
    ap.add_argument("--pass", dest="pass_name", choices=sorted(PASS_PROMPTS))
    a = ap.parse_args(argv)
    print(json.dumps(run(max_calls=a.max_calls, any_time=a.any_time, pass_name=a.pass_name), default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
