"""The orchestrator: the only holder of task state. Workers call it over HTTPS with their own
token, one task at a time (docs/SCALE_OUT_PRD.md section 2):

    POST /register            -> the worker's id and pool
    POST /next                -> one task, or {"wait": seconds}, or {"stop": true}
    POST /heartbeat           -> extends the running task's lease
    POST /report  (multipart) -> the outcome, result JSON and an optional file
    GET  /file/{task_id}      -> the input file of a task that needs one (OCR)
    GET  /bundle              -> the worker code (infra/__init__, handlers, worker)

Rate policy (section 2.3): a task for a site (nse, bse) goes only to a worker whose IP has no
other task in flight there and whose last one ended >= SITE_GAP_S ago; history (priority >= 50)
pauses in the main server's own NSE/BSE windows; a block holds that IP for the site, and blocks
from two IPs within an hour pause the site.

    uvicorn infra.orchestrator:app --host 127.0.0.1 --port 8100
"""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import Response

from infra import schema, tasks
from utils.db import db_session
from utils.fallback_telemetry import record_local_fallback_event

SITE_GAP_S = {"nse": 10.0, "bse": 10.0}
DEFAULT_SITE_GAP_S = 10.0
BLOCK_HOLD = timedelta(hours=1)
SITE_PAUSE = timedelta(hours=6)
WAIT_S = 15
LEASE_GRACE_S = 60
RETRY_BACKOFF_S = 120
HISTORY_PRIORITY = 50
# the main server's NSE/BSE windows (IST): morning catch-up, evening EOD
QUIET_IST = ((7 * 60 + 10, 8 * 60 + 30), (19 * 60 + 15, 20 * 60 + 30))
IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent.parent

app = FastAPI(title="stockey orchestrator", docs_url=None, redoc_url=None, openapi_url=None)


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _machine(cur, authorization: str | None) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "no token")
    cur.execute(f"SELECT id, pool, ip, status FROM {schema.MACHINE} WHERE token_sha256 = %s",
                (_sha(authorization[7:]),))
    row = cur.fetchone()
    if row is None or row[3] in ("deleted", "failed"):
        raise HTTPException(401, "unknown or retired worker")
    return {"id": row[0], "pool": row[1], "ip": row[2], "status": row[3]}


def quiet_now(now: datetime | None = None) -> bool:
    t = (now or datetime.now(timezone.utc)).astimezone(IST)
    m = t.hour * 60 + t.minute
    return any(a <= m < b for a, b in QUIET_IST)


# --- endpoints -----------------------------------------------------------------------------------

@app.post("/register")
def register(request: Request, authorization: str | None = Header(None)) -> dict:
    with db_session() as (_, cur):
        m = _machine(cur, authorization)
        seen_ip = request.headers.get("x-real-ip") or (request.client.host if request.client else None)
        cur.execute(f"""UPDATE {schema.MACHINE} SET status = CASE WHEN status = 'deleting' THEN status ELSE 'ready' END,
                               registered_at = coalesce(registered_at, now()),
                               last_seen = now(), idle_since = now(), ip = coalesce(ip, %s) WHERE id = %s""",
                    (seen_ip, m["id"]))
        # a worker that restarted mid-task: that task is lost, not running
        _lose_tasks(cur, f"t.machine_id = '{m['id']}' AND t.status = 'running'", "worker restarted")
    return {"id": m["id"], "pool": m["pool"]}


BUNDLE_FILES = ("infra/__init__.py", "infra/handlers.py", "infra/worker.py")


def bundle_version() -> str:
    h = hashlib.sha256()
    for rel in BUNDLE_FILES:
        h.update((ROOT / rel).read_bytes())
    return h.hexdigest()[:16]


@app.post("/next")
def next_task(authorization: str | None = Header(None), x_bundle: str | None = Header(None)) -> dict:
    with db_session() as (_, cur):
        m = _machine(cur, authorization)
        cur.execute(f"UPDATE {schema.MACHINE} SET last_seen = now() WHERE id = %s", (m["id"],))
        if m["status"] == "deleting":
            return {"stop": True}
        if x_bundle and x_bundle != bundle_version():
            return {"stop": True, "restart": True}     # new worker code: restart and download it
        task, soonest = _claim(cur, m)
        if task is None:
            cur.execute(f"UPDATE {schema.MACHINE} SET status = 'ready', idle_since = coalesce(idle_since, now())"
                        f" WHERE id = %s", (m["id"],))
            return {"wait": WAIT_S if soonest is None else min(WAIT_S, max(1.0, round(soonest, 1)))}
        cur.execute(f"UPDATE {schema.MACHINE} SET status = 'busy', idle_since = NULL WHERE id = %s", (m["id"],))
        return task


def _claim(cur, m: dict) -> tuple[dict | None, float | None]:
    """One task this worker may run now, marked running under a lease -- or (None, seconds until
    this worker's IP is next free for a site that has work waiting, if that is all that stops it)."""
    soonest = None
    now = datetime.now(timezone.utc)
    cur.execute(f"SELECT domain FROM {schema.DOMAIN} WHERE paused_until > now()")
    paused = {r[0] for r in cur.fetchall()}
    quiet = quiet_now(now)
    cur.execute(f"""SELECT id, kind, payload, domain, priority, attempt, run_tag FROM {schema.TASK}
                     WHERE pool = %s AND status = 'pending' AND (not_before IS NULL OR not_before <= now())
                     ORDER BY priority, id LIMIT 200 FOR UPDATE SKIP LOCKED""", (m["pool"],))
    for tid, kind, payload, domain, priority, attempt, run_tag in cur.fetchall():
        if domain:
            if domain in paused or (quiet and priority >= HISTORY_PRIORITY) or not m["ip"]:
                continue
            cur.execute(f"""INSERT INTO {schema.IP_DOMAIN} (ip, domain) VALUES (%s, %s)
                            ON CONFLICT (ip, domain) DO NOTHING""", (m["ip"], domain))
            cur.execute(f"""SELECT next_at, busy_task, held_until FROM {schema.IP_DOMAIN}
                             WHERE ip = %s AND domain = %s FOR UPDATE""", (m["ip"], domain))
            next_at, busy, held = cur.fetchone()
            if busy is None and (held is None or held <= now) and next_at > now:
                wait = (next_at - now).total_seconds()
                soonest = wait if soonest is None else min(soonest, wait)
            if busy is not None or next_at > now or (held is not None and held > now):
                continue
            cur.execute(f"UPDATE {schema.IP_DOMAIN} SET busy_task = %s WHERE ip = %s AND domain = %s",
                        (tid, m["ip"], domain))
        spec = tasks.SPECS[kind]
        cur.execute(f"""UPDATE {schema.TASK} SET status = 'running', attempt = attempt + 1, machine_id = %s,
                               started_at = now(), lease_until = now() + %s * interval '1 second', error = NULL
                         WHERE id = %s""", (m["id"], spec.timeout_s + LEASE_GRACE_S, tid))
        cur.execute(f"""INSERT INTO {schema.ATTEMPT} (task_id, attempt, machine_id, ip, started_at)
                        VALUES (%s, %s, %s, %s, now())
                        ON CONFLICT (task_id, attempt) DO UPDATE SET machine_id = EXCLUDED.machine_id,
                               ip = EXCLUDED.ip, started_at = now(), finished_at = NULL, outcome = NULL,
                               error = 'attempt number reused (task requeued by hand)'""",
                    (tid, attempt + 1, m["id"], m["ip"]))
        return {"task_id": tid, "kind": kind, "payload": payload, "attempt": attempt + 1,
                "timeout_s": spec.timeout_s, "run_tag": run_tag}, None
    return None, soonest


@app.post("/heartbeat")
def heartbeat(body: dict, authorization: str | None = Header(None)) -> dict:
    with db_session() as (_, cur):
        m = _machine(cur, authorization)
        cur.execute(f"UPDATE {schema.MACHINE} SET last_seen = now() WHERE id = %s", (m["id"],))
        cur.execute(f"SELECT kind FROM {schema.TASK} WHERE id = %s AND machine_id = %s AND status = 'running'",
                    (body.get("task_id"), m["id"]))
        row = cur.fetchone()
        if row is None:
            return {"ok": False}      # the lease already expired; the worker's report will be refused
        cur.execute(f"UPDATE {schema.TASK} SET lease_until = now() + %s * interval '1 second' WHERE id = %s",
                    (tasks.SPECS[row[0]].timeout_s + LEASE_GRACE_S, body.get("task_id")))
        return {"ok": True}


@app.post("/report")
async def report(report: UploadFile = File(...), file: UploadFile | None = File(None),
                 authorization: str | None = Header(None)) -> dict:
    # the report is a file part, not a form field: an annual report's text runs to megabytes,
    # and form fields are capped at 1 MB (that cap crashed workers on 2026-11-09)
    rep = json.loads(await report.read())
    blob = await file.read() if file is not None else None
    with db_session() as (_, cur):
        m = _machine(cur, authorization)
        cur.execute(f"""SELECT id, kind, payload, domain, priority, attempt, max_attempts, run_tag, status, machine_id
                          FROM {schema.TASK} WHERE id = %s FOR UPDATE""", (rep["task_id"],))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, "no such task")
        tid, kind, payload, domain, priority, attempt, max_attempts, run_tag, status, owner = row
        if status != "running" or owner != m["id"]:
            return {"ok": False, "note": "task is no longer yours (lease expired?)"}
        task = {"id": tid, "kind": kind, "payload": payload, "priority": priority, "run_tag": run_tag}
        outcome, error = rep.get("outcome"), rep.get("error")
        follow = []
        if outcome == "done":
            try:
                follow = tasks.SPECS[kind].ingest(task, rep.get("result") or {}, blob)
            except Exception as exc:  # noqa: BLE001 -- a bad result fails the task, not the server
                outcome, error = "failed", f"ingest: {exc!r}"[:500]
        result = {k: v for k, v in (rep.get("result") or {}).items() if k not in ("rows", "text", "text_layer")}
        if outcome == "done":
            cur.execute(f"""UPDATE {schema.TASK} SET status = 'done', finished_at = now(), lease_until = NULL,
                                   result = %s WHERE id = %s""", (json.dumps(result), tid))
            tasks.enqueue(follow, cur)
        else:
            retry = attempt < max_attempts
            cur.execute(f"""UPDATE {schema.TASK} SET status = %s, error = %s, lease_until = NULL, machine_id = NULL,
                                   not_before = now() + %s * interval '1 second',
                                   finished_at = CASE WHEN %s THEN NULL ELSE now() END WHERE id = %s""",
                        ("pending" if retry else "failed", (error or outcome)[:1000], RETRY_BACKOFF_S * attempt,
                         retry, tid))
        cur.execute(f"""UPDATE {schema.ATTEMPT} SET finished_at = now(), outcome = %s, error = %s, seconds = %s
                         WHERE task_id = %s AND attempt = %s""",
                    (outcome, (error or None) and error[:1000], rep.get("seconds"), tid, attempt))
        if domain and m["ip"]:
            gap = SITE_GAP_S.get(domain, DEFAULT_SITE_GAP_S)
            cur.execute(f"""UPDATE {schema.IP_DOMAIN} SET busy_task = NULL, next_at = now() + %s * interval '1 second'
                             WHERE ip = %s AND domain = %s""", (gap, m["ip"], domain))
            if outcome == "blocked":
                _block(cur, m["ip"], domain, error or "blocked")
        cur.execute(f"UPDATE {schema.MACHINE} SET last_seen = now() WHERE id = %s", (m["id"],))
    return {"ok": True, "follow_ups": len(follow)}


def _block(cur, ip: str, domain: str, why: str) -> None:
    cur.execute(f"""UPDATE {schema.IP_DOMAIN} SET held_until = now() + %s * interval '1 second', hold_reason = %s
                     WHERE ip = %s AND domain = %s""", (BLOCK_HOLD.total_seconds(), why[:300], ip, domain))
    cur.execute(f"""SELECT count(DISTINCT ip) FROM {schema.IP_DOMAIN}
                     WHERE domain = %s AND held_until > now() + interval '0 seconds'""", (domain,))
    if cur.fetchone()[0] >= 2:
        cur.execute(f"""INSERT INTO {schema.DOMAIN} (domain, paused_until, reason, updated_at)
                        VALUES (%s, now() + %s * interval '1 second', %s, now())
                        ON CONFLICT (domain) DO UPDATE SET paused_until = EXCLUDED.paused_until,
                               reason = EXCLUDED.reason, updated_at = now()""",
                    (domain, SITE_PAUSE.total_seconds(), f"2+ IPs blocked within the hour; last: {why}"[:300]))
        record_local_fallback_event(module="infra.orchestrator", fallback_type="site_paused", source=domain,
                                    severity="error", reason=f"{domain} paused for {SITE_PAUSE}: two IPs blocked ({why})"[:300])


@app.get("/file/{task_id}")
def task_file(task_id: int, authorization: str | None = Header(None)) -> Response:
    from utils.store import get_file_content
    with db_session() as (_, cur):
        m = _machine(cur, authorization)
        cur.execute(f"SELECT payload, machine_id, status FROM {schema.TASK} WHERE id = %s", (task_id,))
        row = cur.fetchone()
    if row is None or row[1] != m["id"] or row[2] != "running" or not row[0].get("blob_key"):
        raise HTTPException(404, "no file for you here")
    return Response(get_file_content(row[0]["blob_key"]), media_type="application/pdf")


@app.get("/bundle")
def bundle(authorization: str | None = Header(None)) -> Response:
    with db_session() as (_, cur):
        _machine(cur, authorization)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in BUNDLE_FILES:
            tar.add(ROOT / rel, arcname=rel)
    return Response(buf.getvalue(), media_type="application/gzip")


@app.get("/health")
def health() -> dict:
    return {"ok": True}


# --- lost tasks ----------------------------------------------------------------------------------

def _lose_tasks(cur, where: str, why: str) -> int:
    """Running tasks matching `where` (a constant clause) go back to pending, or fail after their
    last attempt; their IP-site slots are freed."""
    cur.execute(f"""SELECT t.id, t.attempt, t.max_attempts, t.domain, a.ip FROM {schema.TASK} t
                      LEFT JOIN {schema.ATTEMPT} a ON a.task_id = t.id AND a.attempt = t.attempt
                     WHERE {where} FOR UPDATE OF t""")
    rows = cur.fetchall()
    for tid, attempt, max_attempts, domain, ip in rows:
        retry = attempt < max_attempts
        cur.execute(f"""UPDATE {schema.TASK} SET status = %s, error = %s, machine_id = NULL, lease_until = NULL,
                               finished_at = CASE WHEN %s THEN NULL ELSE now() END WHERE id = %s""",
                    ("pending" if retry else "failed", f"lost: {why}", retry, tid))
        cur.execute(f"""UPDATE {schema.ATTEMPT} SET finished_at = now(), outcome = 'lost', error = %s
                         WHERE task_id = %s AND attempt = %s""", (why, tid, attempt))
        if domain and ip:
            cur.execute(f"""UPDATE {schema.IP_DOMAIN} SET busy_task = NULL,
                                   next_at = greatest(next_at, now() + interval '10 seconds')
                             WHERE ip = %s AND domain = %s AND busy_task = %s""", (ip, domain, tid))
    return len(rows)


def sweep_once() -> int:
    with db_session() as (_, cur):
        return _lose_tasks(cur, "t.status = 'running' AND t.lease_until < now()", "lease expired (no heartbeat)")


def _sweeper() -> None:
    while True:
        try:
            sweep_once()
        except Exception as exc:  # noqa: BLE001 -- keep sweeping
            print(f"[orchestrator] sweep failed: {exc!r}", flush=True)
        time.sleep(30)


@app.on_event("startup")
def _start() -> None:
    schema.ensure()
    threading.Thread(target=_sweeper, daemon=True, name="lease-sweeper").start()
