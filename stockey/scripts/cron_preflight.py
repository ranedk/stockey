from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pandas as pd

from advisory import cron_status
from advisory.fallback_telemetry import record_local_fallback_event


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CRONTAB_PATH = REPO_ROOT / "config" / "stockey.generated.crontab"
DEFAULT_LOG_DIR = REPO_ROOT / "logs" / "cron"
DEFAULT_REQUIRED_ENV = ("STOCKEY_DIR", "LOG_DIR", "SHELL", "PATH")
DEFAULT_PORTS = (("operator_api", "127.0.0.1", 8765), ("operator_web", "127.0.0.1", 3000))
ENV_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
SHELL_VAR_RE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}|([A-Za-z_][A-Za-z0-9_]*))")
SCRIPT_TOKEN_RE = re.compile(r"(?P<quote>[\"']?)(?P<path>(?:\$STOCKEY_DIR/)?(?:\./)?[A-Za-z0-9_./-]+\.sh)(?P=quote)")


def _status(severity: str, check: str, message: str, **details: Any) -> dict[str, Any]:
    return {"status": severity, "check": check, "message": message, **{k: v for k, v in details.items() if v is not None}}


def _overall(rows: list[dict[str, Any]]) -> str:
    statuses = {str(row.get("status") or "unknown") for row in rows}
    if "error" in statuses:
        return "error"
    if "warn" in statuses:
        return "warn"
    return "ok"


def _parse_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    env: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = ENV_ASSIGN_RE.match(line)
        if not match:
            continue
        env[match.group(1)] = match.group(2).strip().strip('"').strip("'")
    return env


def _expand_path(value: str, env: dict[str, str]) -> Path:
    text = str(value or "")
    for key, item in env.items():
        text = text.replace(f"${key}", item).replace(f"${{{key}}}", item)
    return Path(os.path.expanduser(text))


def _script_paths(command: str, env: dict[str, str]) -> list[Path]:
    paths: list[Path] = []
    for match in SCRIPT_TOKEN_RE.finditer(command or ""):
        path_text = match.group("path")
        if path_text.startswith("./"):
            paths.append(Path(env.get("STOCKEY_DIR") or REPO_ROOT) / path_text[2:])
        else:
            paths.append(_expand_path(path_text, env))
    return paths


def _required_shell_vars(commands: list[str]) -> set[str]:
    required: set[str] = set()
    for command in commands:
        for match in SHELL_VAR_RE.finditer(command or ""):
            token = match.group(0)
            if ":-" in token:
                continue
            required.add(match.group(1) or match.group(2) or "")
    return {item for item in required if item}


def _check_port(host: str, port: int, *, timeout: float = 0.25) -> dict[str, Any]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        result = sock.connect_ex((host, int(port)))
    finally:
        sock.close()
    return {"host": host, "port": int(port), "state": "reachable" if result == 0 else "available"}


def _resolve_python(repo_root: Path, *, timeout_seconds: int = 10) -> dict[str, Any]:
    script = repo_root / "scripts" / "resolve_python.sh"
    if not script.exists():
        return _status("error", "python", "scripts/resolve_python.sh is missing", path=str(script))
    if not os.access(script, os.X_OK):
        return _status("error", "python", "scripts/resolve_python.sh is not executable", path=str(script))
    try:
        proc = subprocess.run([str(script)], cwd=str(repo_root), capture_output=True, text=True, timeout=timeout_seconds, check=False)
    except Exception as exc:
        record_local_fallback_event(
            module="scripts.cron_preflight",
            source="resolve_python",
            fallback_type="cron_preflight_resolve_python_failed",
            severity="error",
            reason="Cron preflight could not execute scripts/resolve_python.sh; cron Python resolution status is degraded.",
            error=exc,
            metadata={"repo_root": str(repo_root), "script": str(script), "timeout_seconds": int(timeout_seconds)},
        )
        return _status("error", "python", f"failed to run resolve_python.sh: {type(exc).__name__}: {exc}", path=str(script))
    resolved = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    if proc.returncode != 0 or not resolved:
        return _status("error", "python", "could not resolve a Python interpreter", returncode=proc.returncode, stderr=proc.stderr.strip())
    python_path = Path(resolved)
    if not python_path.exists():
        return _status("error", "python", "resolved Python path does not exist", python=str(python_path))
    return _status("ok", "python", "Python interpreter resolved", python=str(python_path))


def build_cron_preflight(
    *,
    crontab_path: str | Path = DEFAULT_CRONTAB_PATH,
    log_dir: str | Path | None = None,
    stale_lock_seconds: int = cron_status.DEFAULT_STALE_LOCK_SECONDS,
    required_env: tuple[str, ...] = DEFAULT_REQUIRED_ENV,
    check_ports: bool = True,
    resolve_python: bool = True,
    now: pd.Timestamp | None = None,
) -> dict[str, Any]:
    crontab = Path(crontab_path)
    checks: list[dict[str, Any]] = []
    env = _parse_env(crontab)
    effective_log_dir = Path(log_dir) if log_dir else _expand_path(env.get("LOG_DIR") or str(DEFAULT_LOG_DIR), env)
    effective_repo = _expand_path(env.get("STOCKEY_DIR") or str(REPO_ROOT), env)

    if not crontab.exists():
        checks.append(_status("error", "crontab", "generated crontab is missing", path=str(crontab)))
        return {
            "status": "error",
            "generated_at": pd.Timestamp.utcnow().isoformat(),
            "crontab_path": str(crontab),
            "log_dir": str(effective_log_dir),
            "checks": checks,
            "jobs": [],
            "ports": [],
        }

    jobs = cron_status.parse_crontab(crontab)
    checks.append(
        _status(
            "ok" if jobs else "error",
            "crontab",
            f"parsed {len(jobs)} scheduled jobs" if jobs else "generated crontab has no scheduled jobs",
            path=str(crontab),
            job_count=len(jobs),
        )
    )

    for key in required_env:
        value = env.get(key)
        checks.append(
            _status(
                "ok" if value else "error",
                "environment",
                f"{key} is configured" if value else f"{key} is missing from generated crontab",
                variable=key,
                value=value,
            )
        )

    checks.append(
        _status(
            "ok" if effective_repo.exists() and effective_repo.is_dir() else "error",
            "stockey_dir",
            "STOCKEY_DIR exists" if effective_repo.exists() else "STOCKEY_DIR does not exist",
            path=str(effective_repo),
        )
    )
    checks.append(
        _status(
            "ok" if effective_log_dir.exists() and effective_log_dir.is_dir() else "warn",
            "log_dir",
            "LOG_DIR exists" if effective_log_dir.exists() else "LOG_DIR is missing; cron commands create it before writing logs",
            path=str(effective_log_dir),
        )
    )

    commands = [str(row.get("command") or "") for row in jobs]
    required_shell_vars = _required_shell_vars(commands)
    missing_shell_vars = sorted(required_shell_vars - set(env) - set(os.environ))
    checks.append(
        _status(
            "ok" if not missing_shell_vars else "error",
            "shell_variables",
            "all non-default shell variables are configured" if not missing_shell_vars else "some non-default shell variables are missing",
            missing=missing_shell_vars,
        )
    )

    missing_scripts: list[str] = []
    non_executable_scripts: list[str] = []
    seen_scripts: set[Path] = set()
    for command in commands:
        for path in _script_paths(command, env):
            if path in seen_scripts:
                continue
            seen_scripts.add(path)
            if not path.exists():
                missing_scripts.append(str(path))
            elif not os.access(path, os.X_OK):
                non_executable_scripts.append(str(path))
    script_status = "ok" if not missing_scripts and not non_executable_scripts else "error"
    checks.append(
        _status(
            script_status,
            "scripts",
            "all referenced shell scripts exist and are executable" if script_status == "ok" else "one or more referenced shell scripts are missing or not executable",
            checked_count=len(seen_scripts),
            missing=missing_scripts,
            non_executable=non_executable_scripts,
        )
    )

    lock_rows: list[dict[str, Any]] = []
    for job in jobs:
        lock = cron_status.inspect_lock(
            job.get("lock_file"),
            now=now or pd.Timestamp.utcnow(),
            stale_after_seconds=stale_lock_seconds,
        )
        lock_rows.append({"job_name": job.get("job_name"), **lock})
    stale_locks = [row for row in lock_rows if row.get("lock_stale")]
    active_locks = [row for row in lock_rows if row.get("lock_active")]
    checks.append(
        _status(
            "warn" if stale_locks else "ok",
            "locks",
            "stale lock directories detected" if stale_locks else "no stale lock directories detected",
            active_count=len(active_locks),
            stale_count=len(stale_locks),
            stale_locks=stale_locks[:10],
        )
    )

    if resolve_python:
        checks.append(_resolve_python(effective_repo))

    ports: list[dict[str, Any]] = []
    if check_ports:
        for name, host, port in DEFAULT_PORTS:
            port_state = _check_port(host, port)
            ports.append({"name": name, **port_state})
        checks.append(
            _status(
                "ok",
                "ports",
                "operator API/web ports are either available or already reachable",
                ports=ports,
            )
        )

    status = _overall(checks)
    return {
        "status": status,
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "crontab_path": str(crontab),
        "log_dir": str(effective_log_dir),
        "stockey_dir": str(effective_repo),
        "job_count": len(jobs),
        "checks": checks,
        "jobs": jobs,
        "locks": lock_rows,
        "ports": ports,
        "next_command": "./go-crond config/stockey.generated.crontab --allow-unprivileged" if status != "error" else "fix preflight errors before starting go-crond",
    }


def _format_text(payload: dict[str, Any]) -> str:
    lines = [
        f"status: {payload.get('status')}",
        f"crontab: {payload.get('crontab_path')}",
        f"jobs: {payload.get('job_count')}",
    ]
    for check in payload.get("checks") or []:
        lines.append(f"- {check.get('status')}: {check.get('check')} - {check.get('message')}")
        if check.get("missing"):
            lines.append(f"  missing: {', '.join(map(str, check.get('missing') or []))}")
        if check.get("stale_count"):
            lines.append(f"  stale_locks: {check.get('stale_count')}")
    if payload.get("next_command"):
        lines.append(f"next: {payload.get('next_command')}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only preflight for Stockey go-crond setup.")
    parser.add_argument("--crontab-path", default=str(DEFAULT_CRONTAB_PATH), help="Generated crontab path to validate.")
    parser.add_argument("--log-dir", default=None, help="Override cron log directory for validation.")
    parser.add_argument("--stale-lock-seconds", type=int, default=cron_status.DEFAULT_STALE_LOCK_SECONDS)
    parser.add_argument("--skip-port-check", action="store_true")
    parser.add_argument("--skip-python-check", action="store_true")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)

    payload = build_cron_preflight(
        crontab_path=args.crontab_path,
        log_dir=args.log_dir,
        stale_lock_seconds=args.stale_lock_seconds,
        check_ports=not args.skip_port_check,
        resolve_python=not args.skip_python_check,
    )
    if args.format == "json":
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(_format_text(payload))
    return 1 if payload.get("status") == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
