"""Supervisor for the trigger study's continuous pipeline (operator, 2026-10-11: "make sure
something is always running and resources are not wasted").

Every 15 minutes, for each stage: is there work, is the stage ALLOWED to run right now, and
has it moved? A stage is STALLED when it has work and has been allowed to run for
STALL_MINUTES without any progress. Idle for a legitimate reason (night window, NSE busy
window, usage-limit park, OCR serving daily filings) is reported, never alerted.

A new stall emails the operator once (utils/ops_alert.py, operator-only); the recovery sends
one "recovered" email. State lives in a small JSON file next to the cron logs.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from utils.db import sql_to_df

from fundamentals.trigger_study.filings import BUSY_UTC, nse_busy
from fundamentals.trigger_study.llm_runner import BATCHES_TABLE, IST, NIGHT_END_H, NIGHT_START_H, STATE_TABLE
from fundamentals.trigger_study.nse_results import RESULTS_TABLE

STALL_MINUTES = 45
STATE_PATH = Path(__file__).resolve().parents[2] / "logs" / "cron" / "trigger_study_supervisor.json"


def _ts(v) -> datetime | None:
    if v is None or pd.isna(v):
        return None
    t = pd.Timestamp(v)
    return (t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")).to_pydatetime()


def night_allowed_since(now: datetime) -> datetime | None:
    """Start of the current 22:00-08:00 IST window, or None outside it."""
    local = now.astimezone(IST)
    if local.hour >= NIGHT_START_H:
        start = local.replace(hour=NIGHT_START_H, minute=0, second=0, microsecond=0)
    elif local.hour < NIGHT_END_H:
        start = (local - timedelta(days=1)).replace(hour=NIGHT_START_H, minute=0, second=0, microsecond=0)
    else:
        return None
    return start.astimezone(timezone.utc)


def nse_allowed_since(now: datetime) -> datetime | None:
    """End of the most recent NSE busy window, or None while inside one."""
    if nse_busy(now):
        return None
    ends = []
    for day in (0, 1):
        base = (now - timedelta(days=day)).replace(hour=0, minute=0, second=0, microsecond=0)
        ends += [base + timedelta(minutes=b) for _, b in BUSY_UTC]
    return max(e for e in ends if e <= now)


def _verdict(work: int, allowed_since: datetime | None, last_progress: datetime | None, now: datetime,
             idle_reason: str) -> dict:
    if work <= 0:
        return {"state": "done_or_empty", "work": work}
    if allowed_since is None:
        return {"state": "waiting", "why": idle_reason, "work": work}
    window = timedelta(minutes=STALL_MINUTES)
    if now - allowed_since < window:
        return {"state": "starting", "work": work}
    moving = last_progress is not None and now - last_progress < window
    return {"state": "running" if moving else "STALLED", "work": work,
            "last_progress": last_progress.isoformat() if last_progress else None}


def check(now: datetime | None = None) -> dict:
    from fundamentals.collectors import ocr_research_queue as rq
    from fundamentals.collectors.ocr_pipeline import count_pending_ocr_targets
    from fundamentals.trigger_study.events import EVENTS_TABLE, samples
    from fundamentals.trigger_study.queue import _already_batched, document_candidates

    now = now or datetime.now(timezone.utc)
    out = {}

    # AI tagging -- night window, unless parked on a usage limit
    b = sql_to_df(f"""SELECT count(*) FILTER (WHERE status = 'pending') AS pending, max(done_at) AS last
                        FROM {BATCHES_TABLE}""").iloc[0]
    park = sql_to_df(f"SELECT resume_at FROM {STATE_TABLE} WHERE id = 1")
    resume = _ts(park.iloc[0]["resume_at"]) if not park.empty else None
    since = night_allowed_since(now)
    why = "outside 22:00-08:00 IST"
    if resume and resume > now:
        since, why = None, f"usage limit, resumes {resume.astimezone(IST):%H:%M IST}"
    elif resume and since and resume > since:
        since = resume
    out["tagging"] = _verdict(int(b["pending"]), since, _ts(b["last"]), now, why)

    # NSE downloads -- any time outside the busy windows
    cand = document_candidates()
    todo = int((~cand["filing_id"].isin(_already_batched("document"))).sum())
    last_dl = sql_to_df(f"""SELECT greatest(
            (SELECT max(created_at) FROM {BATCHES_TABLE} WHERE pass = 'document'),
            (SELECT max(tagged_at) FROM fundamentals_trigger_tags WHERE pass = 'document_skip')) AS t""").iloc[0]["t"]
    nse_since = nse_allowed_since(now)
    out["downloads"] = _verdict(todo, nse_since, _ts(last_dl), now, "NSE busy window (production jobs)")

    # NSE results -- the same job fetches them once no attachment is left to download
    syms = sql_to_df(f"SELECT DISTINCT symbol FROM {EVENTS_TABLE} WHERE sample = ANY(%s)",
                     params=(samples("full"),))["symbol"]
    have = set(sql_to_df(f"SELECT DISTINCT symbol FROM {RESULTS_TABLE}")["symbol"])
    missing = int((~syms.isin(have)).sum())
    last_res = sql_to_df(f"SELECT max(fetched_at) AS t FROM {RESULTS_TABLE}").iloc[0]["t"]
    if todo > 0:
        out["results"] = {"state": "waiting", "why": "runs after the attachment downloads", "work": missing}
    else:
        out["results"] = _verdict(missing, nse_since, _ts(last_res), now, "NSE busy window (production jobs)")

    # research OCR -- always allowed, but daily filings come first
    pend = int(rq.pending_count())
    oldest = sql_to_df(f"SELECT min(created_at) AS t FROM {rq.TABLE} WHERE status = 'pending'").iloc[0]["t"]
    last_ocr = sql_to_df(f"SELECT max(done_at) AS t FROM {rq.TABLE}").iloc[0]["t"]
    daily = count_pending_ocr_targets()
    out["research_ocr"] = _verdict(pend, None if daily else _ts(oldest), _ts(last_ocr), now,
                                   f"{daily} daily filing(s) take priority")
    return out


def supervise(now: datetime | None = None, send=None) -> dict:
    from utils.ops_alert import send_ops_alert
    send = send or send_ops_alert
    now = now or datetime.now(timezone.utc)
    stages = check(now)
    try:
        state = json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    sent = []
    for name, v in stages.items():
        was = state.get(name, {}).get("stalled_since")
        if v["state"] == "STALLED" and not was:
            state[name] = {"stalled_since": now.isoformat()}
            send(f"[trigger study] {name} stalled",
                 f"Stage '{name}' has work ({v['work']}) and has been allowed to run for "
                 f"{STALL_MINUTES}+ minutes, but nothing has moved since {v.get('last_progress')}.\n"
                 f"Check logs/cron/trigger_study*.log and fundamentals_ocr.log.\n\n{json.dumps(stages, indent=1)}")
            sent.append(f"stalled:{name}")
        elif v["state"] != "STALLED" and was:
            state[name] = {}
            send(f"[trigger study] {name} recovered",
                 f"Stage '{name}' is {v['state']} again (stalled since {was}).")
            sent.append(f"recovered:{name}")
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state))
    return {"stages": stages, "alerts": sent}
