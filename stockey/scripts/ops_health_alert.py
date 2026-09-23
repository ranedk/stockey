"""Hourly out-of-band alarm: is Postgres up, and is go-crond running?

Registered in the OS-LEVEL user crontab (`crontab -e`), NEVER in go-crond's own
generated crontab. go-crond being dead is one of the two conditions this detects, so an
alarm scheduled by go-crond could not fire for exactly the failure it exists to catch --
the same reasoning that put scripts/ensure_go_crond_alive.sh there, and the same trap
that let go-crond die unnoticed for five days in August 2026.

    python -m scripts.ops_health_alert            # check, alert if unhealthy
    python -m scripts.ops_health_alert --dry-run  # report, never send
    python -m scripts.ops_health_alert --test     # force a send, to prove the path works

Exit: 0 healthy, 1 something is down (whether or not the email got through), 2 the check
itself could not run.

WHY IT REPEATS: an outage keeps sending on every run rather than alerting once and going
quiet. Postgres being down stops the entire platform, and a single email at 03:00 is easy
to miss; a recurring one is not. OPS_ALERT_REPEAT_HOURS throttles that if it becomes
noise (0 = every run, the default, matching an hourly cron). A RECOVERED notice is always
sent on the transition back, so a resolved alarm is never left looking open.

State lives in a plain file, not the database -- the database is one of the subjects.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_DIR))

from utils.ops_alert import send_ops_alert  # noqa: E402

STATE_PATH = Path(os.getenv("OPS_ALERT_STATE", str(REPO_DIR / "logs" / "cron" / "ops_alert_state.json")))
REPEAT_HOURS = float(os.getenv("OPS_ALERT_REPEAT_HOURS", "0"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def check_postgres() -> tuple[bool, str]:
    """A real query, not a port probe: a listening socket proves nothing about whether
    the server can actually answer (it accepts connections while still recovering, and
    during the 2026-08-31 OOM it was killed outright)."""
    env_file = REPO_DIR / ".env"
    cfg: dict[str, str] = {}
    try:
        for line in env_file.read_text().splitlines():
            if line.startswith(("POSTGRES_HOST=", "POSTGRES_PORT=", "POSTGRES_DB=", "POSTGRES_USER=", "POSTGRES_PASSWORD=")):
                k, _, v = line.partition("=")
                cfg[k] = v.strip().strip("'\"")
    except Exception as exc:  # noqa: BLE001
        return False, f"could not read .env: {type(exc).__name__}: {exc}"

    psql = shutil.which("psql")
    if not psql:
        return False, "psql not on PATH, cannot verify Postgres"
    try:
        proc = subprocess.run(
            [psql, "-X", "-q", "-tAc", "SELECT 1",
             "-h", cfg.get("POSTGRES_HOST", "localhost"),
             "-p", cfg.get("POSTGRES_PORT", "5432"),
             "-U", cfg.get("POSTGRES_USER", "stockey"),
             "-d", cfg.get("POSTGRES_DB", "stockey")],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "PGPASSWORD": cfg.get("POSTGRES_PASSWORD", ""),
                 "PGCONNECT_TIMEOUT": "10"},
        )
    except subprocess.TimeoutExpired:
        return False, "Postgres did not answer SELECT 1 within 30s"
    except Exception as exc:  # noqa: BLE001
        return False, f"psql failed: {type(exc).__name__}: {exc}"

    if proc.returncode == 0 and proc.stdout.strip() == "1":
        return True, "answering queries"
    return False, (proc.stderr or proc.stdout or "unknown psql failure").strip()[:300]


def check_go_crond() -> tuple[bool, str]:
    """pgrep -x matches the process NAME exactly -- deliberately not `pgrep -f <pattern>`,
    which also matches this checker's own argv (the self-match footgun this repo has hit
    before)."""
    pgrep = shutil.which("pgrep")
    if not pgrep:
        return False, "pgrep not on PATH, cannot verify go-crond"
    proc = subprocess.run([pgrep, "-x", "go-crond"], capture_output=True, text=True)
    if proc.returncode == 0 and proc.stdout.strip():
        return True, f"running (pid {proc.stdout.split()[0]})"
    if (REPO_DIR.parent / "stockey" / "..").exists() and Path("/tmp/stockey_cron_maintenance").exists():
        # A deliberate stop is not an incident. start_cron.sh clears this sentinel.
        return True, "not running, but /tmp/stockey_cron_maintenance is set (maintenance mode)"
    return False, "go-crond is not running -- no scheduled job is firing"


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:  # noqa: BLE001 -- a missing/corrupt state file must not stop an alert
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2, default=str))
    except Exception:  # noqa: BLE001
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="Report only, never send.")
    parser.add_argument("--test", action="store_true", help="Send a test alert to prove the path works.")
    args = parser.parse_args(argv)

    if args.test:
        ok = send_ops_alert(
            "TEST alert -- ignore",
            "This is a test from scripts/ops_health_alert.py --test.\n"
            "If you are reading it, the ops alert path works end to end.\n",
        )
        print(f"test alert {'sent' if ok else 'NOT sent (see logs/cron/ops_alert.log)'}")
        return 0 if ok else 1

    checks = {"postgres": check_postgres(), "go_crond": check_go_crond()}
    failures = {name: detail for name, (ok, detail) in checks.items() if not ok}

    for name, (ok, detail) in checks.items():
        print(f"  {'OK  ' if ok else 'DOWN'} {name}: {detail}")

    state = _load_state()
    was_down = bool(state.get("down"))
    now = _now()

    if not failures:
        if was_down and not args.dry_run:
            send_ops_alert(
                "RECOVERED: platform health is back to normal",
                "Everything this alarm watches is healthy again.\n\n"
                f"  postgres: {checks['postgres'][1]}\n"
                f"  go-crond: {checks['go_crond'][1]}\n\n"
                f"Was first reported down at {state.get('first_seen_at')}.\n",
            )
        _save_state({"down": False, "last_check_at": now})
        print("healthy")
        return 0

    # Throttle repeats only if the operator asked for it; default is every run.
    last_alert = state.get("last_alert_at")
    should_send = True
    if was_down and REPEAT_HOURS > 0 and last_alert:
        try:
            elapsed = now - datetime.fromisoformat(str(last_alert))
            should_send = elapsed >= timedelta(hours=REPEAT_HOURS)
        except Exception:  # noqa: BLE001 -- an unparseable timestamp must not suppress an alert
            should_send = True

    first_seen = state.get("first_seen_at") if was_down else now.isoformat()
    body_lines = [
        "The stockey data platform has a foundation-level failure.",
        "",
        *(f"  DOWN  {n}: {d}" for n, d in failures.items()),
        *(f"  ok    {n}: {checks[n][1]}" for n in checks if n not in failures),
        "",
        f"First seen down: {first_seen}",
        f"Checked at:      {now:%Y-%m-%dT%H:%M:%SZ}",
        "",
        "What to do:",
        "  Postgres down -> it does NOT restart itself after an OOM kill:",
        "      sudo systemctl reset-failed postgresql@16-main && sudo systemctl start postgresql@16-main",
        "  go-crond down -> scripts/ensure_go_crond_alive.sh should self-heal it within",
        "      15 minutes. If it has not, start it by hand:",
        "      cd ~/code/trading/stockey && setsid --fork ./start_cron.sh",
        "      (a deliberate stop should set /tmp/stockey_cron_maintenance, which this",
        "       alarm treats as expected and does not report)",
    ]
    body = "\n".join(body_lines)

    if args.dry_run:
        print("\n--- would send ---\n" + body)
    elif should_send:
        send_ops_alert(f"DOWN: {', '.join(sorted(failures))}", body)
        state["last_alert_at"] = now.isoformat()
    else:
        print(f"(alert throttled -- OPS_ALERT_REPEAT_HOURS={REPEAT_HOURS})")

    if not args.dry_run:
        state.update({"down": True, "first_seen_at": first_seen, "last_check_at": now.isoformat()})
        _save_state(state)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
