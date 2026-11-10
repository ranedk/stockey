"""The worker loop: register, ask for ONE task, run it (retrying inside the task per kind),
report, ask again. A heartbeat thread keeps the task's lease alive while it runs. If the worker
dies, the orchestrator's lease runs out and only that one task goes back to the queue.

    ORCH_URL=https://.../orchestrator ORCH_TOKEN=... python -m infra.worker
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback

import requests

from infra import handlers

HEARTBEAT_S = 30
HTTP_TIMEOUT = 60


class Client:
    def __init__(self, url: str, token: str) -> None:
        self.url = url.rstrip("/")
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"

    def call(self, method: str, path: str, **kw):
        for i in range(6):                      # the orchestrator restarting must not kill the worker
            try:
                r = self.s.request(method, self.url + path, timeout=kw.pop("timeout", HTTP_TIMEOUT), **kw)
                if r.status_code == 401:
                    raise SystemExit(f"orchestrator refused our token: {r.text[:200]}")
                r.raise_for_status()
                return r
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
                if i == 5:
                    raise
                print(f"[worker] {path} failed ({exc!r}); retrying", flush=True)
                time.sleep(10 * (i + 1))


def _heartbeat(client: Client, task_id: int, stop: threading.Event) -> None:
    while not stop.wait(HEARTBEAT_S):
        try:
            client.call("POST", "/heartbeat", json={"task_id": task_id})
        except Exception as exc:  # noqa: BLE001 -- the next beat may get through
            print(f"[worker] heartbeat failed: {exc!r}", flush=True)


def run_one(client: Client, task: dict, ctx: handlers.Context) -> dict:
    kind, payload = task["kind"], task["payload"]
    fn = handlers.HANDLERS[kind]
    args = [payload, ctx]
    if kind in handlers.NEEDS_FILE:
        args.append(client.call("GET", f"/file/{task['task_id']}", timeout=300).content)
    started, last_error = time.monotonic(), None
    for i in range(handlers.RETRIES.get(kind, 1) + 1):
        try:
            result, blob = fn(*args)
            return {"outcome": "done", "result": result, "blob": blob, "seconds": time.monotonic() - started}
        except handlers.Blocked as exc:          # never retried from the same IP
            return {"outcome": "blocked", "error": str(exc), "seconds": time.monotonic() - started}
        except Exception as exc:  # noqa: BLE001
            last_error = f"{exc!r}\n{traceback.format_exc(limit=3)}"
            print(f"[worker] task {task['task_id']} try {i + 1} failed: {exc!r}", flush=True)
            time.sleep(15 * (i + 1))
    return {"outcome": "failed", "error": last_error, "seconds": time.monotonic() - started}


def deliver(client: Client, rep: dict, blob: bytes | None):
    """Send the report (a JSON file part, plus the task's file). If it cannot be delivered, report
    the task as failed with the reason instead -- a worker must not die on one task's report."""
    files = {"report": ("report.json", json.dumps(rep, default=str).encode(), "application/json")}
    if blob:
        files["file"] = ("file.bin", blob, "application/octet-stream")
    try:
        return client.call("POST", "/report", files=files, timeout=300)
    except (requests.RequestException, SystemExit) as exc:
        if isinstance(exc, SystemExit):
            raise
        small = {"task_id": rep["task_id"], "outcome": "failed", "seconds": rep.get("seconds"),
                 "error": f"report not delivered: {exc!r}"[:500]}
        return client.call("POST", "/report", files={"report": ("report.json", json.dumps(small).encode(),
                                                                 "application/json")})


def bundle_version() -> str:
    """The same hash the orchestrator computes over the files it ships."""
    import hashlib
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    h = hashlib.sha256()
    for rel in ("infra/__init__.py", "infra/handlers.py", "infra/worker.py"):
        h.update((root / rel).read_bytes())
    return h.hexdigest()[:16]


def main() -> int:
    client = Client(os.environ["ORCH_URL"], os.environ["ORCH_TOKEN"])
    client.s.headers["X-Bundle"] = bundle_version()
    me = client.call("POST", "/register").json()
    print(f"[worker] registered as {me['id']} in pool {me['pool']}", flush=True)
    ctx = handlers.Context()
    while True:
        task = client.call("POST", "/next").json()
        if task.get("stop"):
            print("[worker] told to " + ("restart for new code" if task.get("restart") else "stop"), flush=True)
            return 3 if task.get("restart") else 0
        if "wait" in task:
            time.sleep(task["wait"])
            continue
        stop = threading.Event()
        beat = threading.Thread(target=_heartbeat, args=(client, task["task_id"], stop), daemon=True)
        beat.start()
        try:
            out = run_one(client, task, ctx)
        finally:
            stop.set()
        rep = {"task_id": task["task_id"], "outcome": out["outcome"], "result": out.get("result"),
               "error": out.get("error"), "seconds": round(out["seconds"], 2)}
        r = deliver(client, rep, out.get("blob"))
        print(f"[worker] task {task['task_id']} {task['kind']}: {out['outcome']} in {rep['seconds']}s "
              f"-> {r.json()}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
