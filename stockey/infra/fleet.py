"""Creates and deletes workers: Hetzner Cloud servers (pool "hetzner", crawling) in the worker
project, and RunPod GPU pods (pool "runpod", OCR). Run once a minute (`tick`).

Caps (operator, 2026-11-09): at most 8 Hetzner servers and 3 RunPod pods, and under USD 100 a
month per pool. A pool is grown only while this month's spend plus one more hour of every
machine (the new one included) stays under MONTHLY_CAP_USD; then it stops and records why.
Every machine is labelled/named "stockey-w-..."; a machine the table does not know, or one
that never registered or stopped calling in, is deleted.

    python -m infra.fleet tick            # the autoscaler step
    python -m infra.fleet status
    python -m infra.fleet down            # delete every worker now
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import secrets
from datetime import datetime, timezone
from pathlib import Path

import requests
from environs import Env

from infra import schema
from utils.db import db_session, sql_to_df
from utils.fallback_telemetry import record_local_fallback_event

env = Env()
env.read_env()

MAX_CAPS = {"hetzner": 8, "runpod": 3}
# a run may lower a cap (INFRA_HETZNER_CAP=2 for a pilot), never raise it
CAPS = {p: max(0, min(c, env.int(f"INFRA_{p.upper()}_CAP", c))) for p, c in MAX_CAPS.items()}
MONTHLY_CAP_USD = 95.0                    # "below USD 100" per pool, with room for rounding
ORCH_URL = env("INFRA_ORCH_URL", "https://stockey.singularity45.ai/orchestrator")
IDLE_DELETE_MIN = 10
BOOT_DEADLINE_MIN = {"hetzner": 15, "runpod": 30}
SILENT_DELETE_MIN = 15
PREFIX = "stockey-w-"

HETZNER_API = "https://api.hetzner.cloud/v1"
HETZNER_TYPE = "cx23"
HETZNER_LOCATIONS = ("fsn1", "nbg1", "hel1")
EUR_USD = 1.2                             # deliberately high: spend is over-counted, never under
RUNPOD_API = "https://rest.runpod.io/v1"
RUNPOD_IMAGE = "runpod/pytorch:2.8.0-py3.11-cuda12.8.1-cudnn-devel-ubuntu22.04"
# GLM-OCR is 0.9B: any 16 GB+ card will do; cheapest first (secure-cloud prices 2026-11-09, $/hr)
RUNPOD_GPUS = ["NVIDIA RTX 2000 Ada Generation",    # 0.24
               "NVIDIA RTX A4000",                  # 0.25
               "NVIDIA RTX A4500",                  # 0.25
               "NVIDIA RTX A5000",                  # 0.27
               "NVIDIA RTX 4000 Ada Generation",    # 0.28
               "NVIDIA GeForce RTX 3090",           # 0.50
               "NVIDIA L4",                         # 0.59
               "NVIDIA RTX A6000",                  # 0.59
               "NVIDIA A40"]                        # 0.59
RUNPOD_FALLBACK_USD_HR = 0.60             # used if the API does not return costPerHr
TASKS_PER_GPU = 20                        # pending OCR tasks per extra pod


def _sha(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


# --- bootstrap scripts ---------------------------------------------------------------------------

def hetzner_user_data(token: str) -> str:
    return f"""#!/bin/bash
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3-venv poppler-utils xvfb curl libatomic1
python3 -m venv /opt/w
/opt/w/bin/pip install -q requests playwright
/opt/w/bin/playwright install --with-deps chromium
mkdir -p /opt/worker
curl -fsS -H "Authorization: Bearer {token}" {ORCH_URL}/bundle | tar xz -C /opt/worker
cat > /etc/systemd/system/worker.service <<'UNIT'
[Unit]
Description=stockey worker
After=network-online.target
[Service]
WorkingDirectory=/opt/worker
Environment=ORCH_URL={ORCH_URL}
Environment=ORCH_TOKEN={token}
ExecStartPre=/bin/sh -c 'curl -fsS -H "Authorization: Bearer {token}" {ORCH_URL}/bundle | tar xz -C /opt/worker'
ExecStart=/usr/bin/xvfb-run -a /opt/w/bin/python -m infra.worker
Restart=always
RestartSec=20
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now worker
"""


def runpod_start_cmd() -> list[str]:
    script = ("set -e; apt-get update -qq && apt-get install -y -qq poppler-utils curl; "
              "pip install -q 'transformers==5.15.0' accelerate pdf2image requests; "
              "mkdir -p /w; cd /w; while true; do "
              "curl -fsS -H \"Authorization: Bearer $ORCH_TOKEN\" $ORCH_URL/bundle | tar xz -C /w && "
              "python -m infra.worker; [ $? -eq 0 ] && break; sleep 10; done; sleep infinity")
    return ["bash", "-c", script]


# --- providers -----------------------------------------------------------------------------------

class Hetzner:
    def __init__(self) -> None:
        self.h = {"Authorization": f"Bearer {env('HETZNER_KEY')}"}

    def price_usd_hr(self) -> float:
        r = requests.get(f"{HETZNER_API}/server_types", params={"name": HETZNER_TYPE}, headers=self.h, timeout=30).json()
        prices = r["server_types"][0]["prices"]
        return max(float(p["price_hourly"]["gross"]) for p in prices) * EUR_USD

    def create(self, name: str, token: str) -> dict:
        """A full location ("resource_unavailable", HTTP 412) is not a refusal: the next one is tried."""
        for loc in random.sample(HETZNER_LOCATIONS, len(HETZNER_LOCATIONS)):
            body = {"name": name, "server_type": HETZNER_TYPE, "image": "ubuntu-24.04",
                    "location": loc, "user_data": hetzner_user_data(token),
                    "labels": {"app": "stockey-infra", "pool": "hetzner"},
                    "public_net": {"enable_ipv4": True, "enable_ipv6": False}}
            r = requests.post(f"{HETZNER_API}/servers", json=body, headers=self.h, timeout=60)
            if r.status_code != 412 or "resource_unavailable" not in r.text:
                break
        if r.status_code >= 300:
            raise RuntimeError(f"hetzner create HTTP {r.status_code}: {r.text[:300]}")
        s = r.json()["server"]
        return {"provider_id": str(s["id"]), "ip": s["public_net"]["ipv4"]["ip"]}

    def delete(self, provider_id: str) -> None:
        r = requests.delete(f"{HETZNER_API}/servers/{provider_id}", headers=self.h, timeout=60)
        if r.status_code not in (200, 204, 404):
            raise RuntimeError(f"hetzner delete HTTP {r.status_code}: {r.text[:200]}")

    def list(self) -> list[dict]:
        r = requests.get(f"{HETZNER_API}/servers", params={"label_selector": "app=stockey-infra", "per_page": 50},
                         headers=self.h, timeout=30).json()
        return [{"provider_id": str(s["id"]), "name": s["name"]} for s in r.get("servers", [])]


class RunPod:
    def __init__(self) -> None:
        self.h = {"Authorization": f"Bearer {env('RUNPOD_API_KEY')}"}

    def create(self, name: str, token: str) -> dict:
        body = {"name": name, "imageName": RUNPOD_IMAGE, "gpuTypeIds": RUNPOD_GPUS, "gpuTypePriority": "custom", "gpuCount": 1,
                "cloudType": "SECURE", "containerDiskInGb": 40, "volumeInGb": 0, "ports": [],
                "env": {"ORCH_URL": ORCH_URL, "ORCH_TOKEN": token, "HF_HUB_DISABLE_TELEMETRY": "1"},
                "dockerStartCmd": runpod_start_cmd()}
        r = requests.post(f"{RUNPOD_API}/pods", json=body, headers=self.h, timeout=60)
        if r.status_code >= 300:
            raise RuntimeError(f"runpod create HTTP {r.status_code}: {r.text[:300]}")
        p = r.json()
        return {"provider_id": p["id"], "ip": None,
                "hourly_usd": float(p.get("costPerHr") or p.get("adjustedCostPerHr") or RUNPOD_FALLBACK_USD_HR)}

    def delete(self, provider_id: str) -> None:
        r = requests.delete(f"{RUNPOD_API}/pods/{provider_id}", headers=self.h, timeout=60)
        if r.status_code not in (200, 204, 404):
            raise RuntimeError(f"runpod delete HTTP {r.status_code}: {r.text[:200]}")

    def list(self) -> list[dict]:
        r = requests.get(f"{RUNPOD_API}/pods", headers=self.h, timeout=30).json()
        return [{"provider_id": p["id"], "name": p.get("name", "")} for p in r if p.get("name", "").startswith(PREFIX)]


PROVIDERS = {"hetzner": Hetzner, "runpod": RunPod}


# --- RunPod balance ------------------------------------------------------------------------------

RUNPOD_GRAPHQL = "https://api.runpod.io/graphql"
BALANCE_ALERT_USD = 20.0          # email the operator once when the prepaid balance drops below this
BALANCE_REARM_USD = 25.0          # ... and again only after it has been topped up past this
BALANCE_FLOOR_USD = 5.0           # no new pods below this
BALANCE_CHECK_MIN = 30
BALANCE_STATE = Path(__file__).resolve().parent.parent / ".cache" / "infra_runpod_balance.json"


def runpod_balance() -> float:
    r = requests.post(RUNPOD_GRAPHQL, json={"query": "query { myself { clientBalance } }"},
                      headers={"Authorization": f"Bearer {env('RUNPOD_API_KEY')}"}, timeout=30)
    r.raise_for_status()
    return float(r.json()["data"]["myself"]["clientBalance"])


def _balance_state() -> dict:
    try:
        return json.loads(BALANCE_STATE.read_text())
    except (OSError, ValueError):
        return {}


def check_runpod_balance(now: datetime, send=None) -> dict:
    """Every BALANCE_CHECK_MIN: read the balance; below BALANCE_ALERT_USD email once (re-armed
    by a top-up). Returns the state, whose "balance" gates new pods."""
    st = _balance_state()
    if st.get("checked_at") and (now - datetime.fromisoformat(st["checked_at"])).total_seconds() < BALANCE_CHECK_MIN * 60:
        return st
    try:
        bal = runpod_balance()
    except Exception as exc:  # noqa: BLE001 -- a failed read keeps the last known balance
        record_local_fallback_event(module="infra.fleet", fallback_type="balance_unread", source="runpod",
                                    severity="warn", reason=str(exc)[:300])
        return st
    st.update(balance=bal, checked_at=now.isoformat())
    if bal >= BALANCE_REARM_USD:
        st["alerted"] = False
    elif bal < BALANCE_ALERT_USD and not st.get("alerted"):
        if send is None:
            from utils.ops_alert import send_ops_alert as send
        st["alerted"] = bool(send(f"RunPod balance ${bal:.2f} -- below ${BALANCE_ALERT_USD:.0f}",
                                  f"The RunPod prepaid balance is ${bal:.2f}. New GPU pods stop below "
                                  f"${BALANCE_FLOOR_USD:.0f}; running pods stop at $0.\n"
                                  "Top up at https://www.runpod.io/console/user/billing"))
    BALANCE_STATE.parent.mkdir(parents=True, exist_ok=True)
    BALANCE_STATE.write_text(json.dumps(st))
    return st


# --- spend ---------------------------------------------------------------------------------------

def month_spend(pool: str) -> float:
    """USD this calendar month: every started hour of every machine, at its hourly price."""
    df = sql_to_df(f"""SELECT created_at, deleted_at, hourly_usd FROM {schema.MACHINE}
                        WHERE pool = %(p)s AND provider <> 'local' AND provider_id IS NOT NULL
                          AND coalesce(deleted_at, now()) >= date_trunc('month', now())""", params={"p": pool})
    now = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total = 0.0
    for r in df.itertuples():
        a = max(r.created_at.to_pydatetime(), start)
        b = r.deleted_at.to_pydatetime() if r.deleted_at is not None and str(r.deleted_at) != "NaT" else now
        total += math.ceil(max((b - a).total_seconds(), 1) / 3600) * r.hourly_usd
    return total


# --- the autoscaler step -------------------------------------------------------------------------

def _alive(pool: str):
    return sql_to_df(f"""SELECT id, provider_id, status, created_at, registered_at, last_seen, idle_since, hourly_usd
                           FROM {schema.MACHINE} WHERE pool = %(p)s AND status NOT IN ('deleted', 'failed')""",
                     params={"p": pool})


def _pending(pool: str) -> int:
    return int(sql_to_df(f"SELECT count(*) AS n FROM {schema.TASK} WHERE pool = %(p)s AND status = 'pending'",
                         params={"p": pool}).iloc[0]["n"])


def _running(machine_id: str) -> bool:
    return bool(sql_to_df(f"SELECT 1 FROM {schema.TASK} WHERE machine_id = %(m)s AND status = 'running'",
                          params={"m": machine_id}).shape[0])


REFUSAL_BACKOFF_MIN = 30          # an account limit or no money: fixed by the operator, not by waiting
UNAVAILABLE_BACKOFF_MIN = 5       # no machine free right now


def _refused_recently(pool: str) -> bool:
    """A refused create is not retried every minute."""
    return bool(sql_to_df(f"""SELECT 1 FROM {schema.MACHINE} WHERE pool = %(p)s AND status = 'failed'
                                AND provider_id IS NULL AND created_at > now() - CASE
                                    WHEN note ILIKE '%%limit%%' OR note ILIKE '%%balance%%'
                                    THEN interval '{REFUSAL_BACKOFF_MIN} minutes'
                                    ELSE interval '{UNAVAILABLE_BACKOFF_MIN} minutes' END LIMIT 1""",
                          params={"p": pool}).shape[0])


def want(pool: str, pending: int) -> int:
    if pending == 0:
        return 0
    if pool == "runpod":
        return min(CAPS[pool], max(1, math.ceil(pending / TASKS_PER_GPU)))
    return min(CAPS[pool], pending)


def create_machine(pool: str) -> str:
    prov = PROVIDERS[pool]()
    mid = f"{PREFIX}{pool[:2]}-{secrets.token_hex(3)}"
    token = secrets.token_urlsafe(32)
    hourly = prov.price_usd_hr() if pool == "hetzner" else RUNPOD_FALLBACK_USD_HR
    with db_session() as (_, cur):
        cur.execute(f"""INSERT INTO {schema.MACHINE} (id, provider, pool, token_sha256, hourly_usd, status)
                        VALUES (%s, %s, %s, %s, %s, 'creating')""", (mid, pool, pool, _sha(token), hourly))
    try:
        got = prov.create(mid, token)
    except Exception as exc:
        with db_session() as (_, cur):
            cur.execute(f"UPDATE {schema.MACHINE} SET status = 'failed', deleted_at = now(), note = %s WHERE id = %s",
                        (str(exc)[:500], mid))
        raise
    with db_session() as (_, cur):
        cur.execute(f"""UPDATE {schema.MACHINE} SET provider_id = %s, ip = %s, hourly_usd = %s WHERE id = %s""",
                    (got["provider_id"], got.get("ip"), got.get("hourly_usd", hourly), mid))
    return mid


def delete_machine(mid: str, why: str) -> None:
    df = sql_to_df(f"SELECT pool, provider_id FROM {schema.MACHINE} WHERE id = %(m)s", params={"m": mid})
    if df.empty:
        return
    pool, pid = df.iloc[0]["pool"], df.iloc[0]["provider_id"]
    if pid:
        PROVIDERS[pool]().delete(pid)
    with db_session() as (_, cur):
        cur.execute(f"UPDATE {schema.MACHINE} SET status = 'deleted', deleted_at = now(), note = %s WHERE id = %s",
                    (why[:300], mid))


def tick(pools=("hetzner", "runpod")) -> dict:
    schema.ensure()
    now = datetime.now(timezone.utc)
    out = {}
    for pool in pools:
        alive = _alive(pool)
        pending = _pending(pool)
        acts = []
        # retire: never registered, gone silent, or idle with nothing to do
        for m in alive.itertuples():
            age_min = (now - m.created_at.to_pydatetime()).total_seconds() / 60
            if m.registered_at is None or str(m.registered_at) == "NaT":
                if age_min > BOOT_DEADLINE_MIN[pool]:
                    delete_machine(m.id, "never registered")
                    acts.append(f"deleted {m.id} (never registered)")
                continue
            silent = (now - m.last_seen.to_pydatetime()).total_seconds() / 60
            idle = m.idle_since is not None and str(m.idle_since) != "NaT" \
                and (now - m.idle_since.to_pydatetime()).total_seconds() / 60 > IDLE_DELETE_MIN
            if silent > SILENT_DELETE_MIN:
                delete_machine(m.id, f"silent {silent:.0f} min")
                acts.append(f"deleted {m.id} (silent)")
            elif idle and pending == 0 and not _running(m.id):
                delete_machine(m.id, "idle")
                acts.append(f"deleted {m.id} (idle)")
        alive = _alive(pool)
        need = want(pool, pending) - len(alive)
        spend = month_spend(pool)
        if need > 0 and _refused_recently(pool):
            acts.append("not creating: the provider refused a recent create (see infra_machine.note)")
            need = 0
        if pool == "runpod":
            bal = check_runpod_balance(now).get("balance")
            if need > 0 and bal is not None and bal < BALANCE_FLOOR_USD:
                acts.append(f"not creating: RunPod balance ${bal:.2f}")
                need = 0
        for _ in range(max(need, 0)):
            hourly_all = float(alive["hourly_usd"].sum()) + (RUNPOD_FALLBACK_USD_HR if pool == "runpod" else 0.02)
            if spend + hourly_all > MONTHLY_CAP_USD:
                acts.append(f"spend cap: month so far ${spend:.2f}, not creating")
                record_local_fallback_event(module="infra.fleet", fallback_type="spend_cap", source=pool,
                                            severity="warn", reason=f"{pool}: ${spend:.2f} this month, cap ${MONTHLY_CAP_USD}")
                break
            try:
                acts.append(f"created {create_machine(pool)}")
            except Exception as exc:  # noqa: BLE001 -- e.g. no GPU free right now; try next minute
                acts.append(f"create failed: {exc}"[:300])
                break
            alive = _alive(pool)
        # orphans: provider machines we do not know as alive
        try:
            known = set(_alive(pool)["provider_id"].dropna().astype(str))
            for o in PROVIDERS[pool]().list():
                if o["provider_id"] not in known and o["name"].startswith(PREFIX):
                    PROVIDERS[pool]().delete(o["provider_id"])
                    acts.append(f"deleted orphan {o['name']}")
        except Exception as exc:  # noqa: BLE001
            acts.append(f"orphan sweep failed: {exc}"[:200])
        out[pool] = {"pending": pending, "alive": len(_alive(pool)), "month_usd": round(month_spend(pool), 2),
                     "actions": acts}
    return out


def status() -> dict:
    m = sql_to_df(f"""SELECT pool, status, count(*) AS n FROM {schema.MACHINE} GROUP BY 1, 2""")
    t = sql_to_df(f"""SELECT kind, status, count(*) AS n FROM {schema.TASK} GROUP BY 1, 2 ORDER BY 1, 2""")
    return {"machines": m.to_dict("records"), "tasks": t.to_dict("records"),
            "month_usd": {p: round(month_spend(p), 2) for p in CAPS}}


def down() -> list[str]:
    gone = []
    for pool in CAPS:
        for m in _alive(pool).itertuples():
            delete_machine(m.id, "fleet down")
            gone.append(m.id)
    return gone


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Worker fleet: Hetzner (crawl) and RunPod (OCR).")
    ap.add_argument("cmd", choices=["tick", "status", "down"])
    ap.add_argument("--pool", choices=list(CAPS), default=None)
    a = ap.parse_args(argv)
    out = {"tick": lambda: tick((a.pool,) if a.pool else tuple(CAPS)), "status": status, "down": down}[a.cmd]()
    print(json.dumps(out, default=str, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
