#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from utils.fallback_telemetry import record_local_fallback_event


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG_DIR = REPO_ROOT / "logs" / "analysis_agents"
DEFAULT_HARD_MAX_CYCLES = 3
SAFETY_STOP_MARKERS = [
    "live broker",
    "broker execution",
    "submit order",
    "destructive",
    "drop table",
    "delete from",
    "truncate",
]
ROOT_ARTIFACT_GLOBS = [
    "block_deals_*.csv",
    "bulk_deals_*.csv",
    "calendar_*.csv",
    "short_selling_*.csv",
]
PYTHON_RELEVANT_PREFIXES = (
    "advisory/",
    "data/",
    "features/",
    "scripts/",
    "tests/",
    "utils/",
)
FRONTEND_RELEVANT_PREFIXES = ("apps/operator-web/",)


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def read_text(path: Path, limit_chars: int = 60_000) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[-limit_chars:]


def run_local_command(args: list[str], *, cwd: Path = REPO_ROOT, timeout_seconds: int = 900) -> tuple[int, str]:
    proc = subprocess.run(
        args,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout_seconds,
        check=False,
    )
    return int(proc.returncode), proc.stdout or ""


def git_output(args: list[str]) -> str:
    return subprocess.check_output(["git", *args], cwd=str(REPO_ROOT), text=True, stderr=subprocess.DEVNULL)


def changed_files() -> list[str]:
    files = git_output(["diff", "--name-only"]).splitlines()
    files.extend(git_output(["ls-files", "--others", "--exclude-standard"]).splitlines())
    return sorted(dict.fromkeys(path for path in files if path.strip()))


def untracked_root_artifacts() -> list[str]:
    artifacts: list[str] = []
    for pattern in ROOT_ARTIFACT_GLOBS:
        artifacts.extend(git_output(["ls-files", "--others", "--exclude-standard", "--", pattern]).splitlines())
    return sorted(dict.fromkeys(path for path in artifacts if path and "/" not in path))


def file_fingerprint(path: str) -> str:
    full_path = REPO_ROOT / path
    if full_path.exists() and full_path.is_file():
        return hashlib.sha256(full_path.read_bytes()).hexdigest()
    return hashlib.sha256(git_output(["diff", "--", path]).encode("utf-8", errors="replace")).hexdigest()


def snapshot_changed_files() -> dict[str, str]:
    return {path: file_fingerprint(path) for path in changed_files()}


def changed_since(before: dict[str, str], after: dict[str, str]) -> list[str]:
    paths = sorted(set(before) | set(after))
    return [path for path in paths if before.get(path) != after.get(path)]


def build_prompt(*, cycle: int, max_cycles: int, stop_on_safety: bool, agent_mode: str) -> str:
    safety_text = "Stop and report instead of editing" if stop_on_safety else "Proceed carefully"
    return f"""
Use $stockey-analysis-flow.

You are running an autonomous-but-bounded Stockey analysis development cycle.

Cycle: {cycle}/{max_cycles}
Repository: {REPO_ROOT}

Read:
- todo.md
- docs/analysis_agent_board.md
- relevant code/tests/docs for the next open slice

Required working mode:
- Agent mode: {agent_mode}
- Use distinct Planner, Builder, Reviewer, and Integrator phases in the final message.
- If real subagent tools are available, use them only for bounded planner/reviewer sidecars; keep final integration in the main workspace.
- If subagent tools are unavailable, do an explicit internal reviewer pass before finalizing.

Task:
1. Pick the next highest-priority bounded open slice from todo.md and docs/analysis_agent_board.md.
2. Implement only that slice.
3. Run focused validation. Prefer:
   - python -m py_compile for changed Python modules
   - pytest -q tests/test_advisory_regression.py -k '<focused expression>'
   - npm --prefix apps/operator-web run typecheck if frontend changed
4. Run a reviewer pass against the full local diff for regressions, stale docs, unsafe side effects, generated artifacts, lockfile drift, and missing tests.
5. Update todo.md and docs/analysis_agent_board.md.
6. Keep broker execution disabled unless explicitly requested by the human operator.
7. Do not perform destructive DB/data operations.
8. {safety_text} if the selected slice requires live broker execution, destructive DB cleanup, unclear production safety, credentials, or broad refactoring.

Output requirements:
- Start final answer with one of these exact markers:
  - CYCLE_STATUS: complete
  - CYCLE_STATUS: blocked
  - CYCLE_STATUS: no_open_slices
- Include changed files, validations run, and next slice.
""".strip()


def run_codex(*, prompt: str, cwd: Path, output_path: Path, timeout_seconds: int) -> tuple[int, str]:
    codex_bin = os.environ.get("ANALYSIS_AGENT_CODEX_BIN") or os.environ.get("CODEX_CLI_BIN") or "codex"
    model = os.environ.get("ANALYSIS_AGENT_CODEX_MODEL") or os.environ.get("CODEX_CLI_MODEL")
    sandbox = os.environ.get("ANALYSIS_AGENT_CODEX_SANDBOX", "danger-full-access")
    cmd = [
        codex_bin,
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        sandbox,
        "--output-last-message",
        str(output_path),
        "-",
    ]
    if model:
        cmd[5:5] = ["--model", model]
    proc = subprocess.run(
        cmd,
        input=prompt,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout_seconds,
        check=False,
    )
    return int(proc.returncode), proc.stdout or ""


def has_safety_marker(text: str) -> str | None:
    lower = text.lower()
    for marker in SAFETY_STOP_MARKERS:
        if marker in lower:
            return marker
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run bounded Codex development cycles from todo.md.")
    parser.add_argument("--max-cycles", type=int, default=int(os.environ.get("ANALYSIS_AGENT_MAX_CYCLES", "1")))
    parser.add_argument("--hard-max-cycles", type=int, default=int(os.environ.get("ANALYSIS_AGENT_HARD_MAX_CYCLES", str(DEFAULT_HARD_MAX_CYCLES))))
    parser.add_argument("--max-files-per-cycle", type=int, default=int(os.environ.get("ANALYSIS_AGENT_MAX_FILES_PER_CYCLE", "20")))
    parser.add_argument("--allow-large-runs", action="store_true", default=os.environ.get("ANALYSIS_AGENT_ALLOW_LARGE_RUNS", "").lower() in {"1", "true", "yes"})
    parser.add_argument("--allow-lockfile-drift", action="store_true", default=os.environ.get("ANALYSIS_AGENT_ALLOW_LOCKFILE_DRIFT", "").lower() in {"1", "true", "yes"})
    parser.add_argument("--log-dir", default=os.environ.get("ANALYSIS_AGENT_LOG_DIR", str(DEFAULT_LOG_DIR)))
    parser.add_argument("--codex-timeout-seconds", type=int, default=int(os.environ.get("ANALYSIS_AGENT_CODEX_TIMEOUT_SECONDS", "3600")))
    parser.add_argument("--post-check-timeout-seconds", type=int, default=int(os.environ.get("ANALYSIS_AGENT_POST_CHECK_TIMEOUT_SECONDS", "1800")))
    parser.add_argument("--agent-mode", default=os.environ.get("ANALYSIS_AGENT_MODE", "planner-builder-reviewer"))
    parser.add_argument("--stop-on-safety-marker", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--post-checks", action=argparse.BooleanOptionalAction, default=os.environ.get("ANALYSIS_AGENT_POST_CHECKS", "1").lower() not in {"0", "false", "no"})
    parser.add_argument("--dry-run", action="store_true", help="Write prompts but do not invoke Codex.")
    return parser.parse_args()


def post_cycle_checks(
    *,
    cycle: int,
    log_path: Path,
    timeout_seconds: int,
    cycle_files: list[str],
    max_files_per_cycle: int,
    allow_lockfile_drift: bool,
) -> bool:
    files = changed_files()
    lines: list[str] = [f"[analysis_agent_loop] post-check cycle={cycle} changed_files={len(files)}"]
    ok = True

    artifacts = untracked_root_artifacts()
    if artifacts:
        ok = False
        lines.append("[analysis_agent_loop] root artifacts must be moved/ignored before continuing:")
        lines.extend(f"  - {path}" for path in artifacts)
    if max_files_per_cycle > 0 and len(cycle_files) > max_files_per_cycle:
        ok = False
        lines.append(
            f"[analysis_agent_loop] cycle touched {len(cycle_files)} files, above max_files_per_cycle={max_files_per_cycle}; split the slice."
        )
        lines.extend(f"  - {path}" for path in cycle_files)
    lockfiles_touched = {
        path
        for path in cycle_files
        if path in {"apps/operator-web/package-lock.json", "apps/operator-web/yarn.lock"}
    }
    if len(lockfiles_touched) > 1 and not allow_lockfile_drift:
        ok = False
        lines.append(
            "[analysis_agent_loop] both npm and yarn lockfiles changed in one cycle; choose one package-manager path or pass --allow-lockfile-drift."
        )

    commands: list[tuple[str, list[str]]] = [("diff_check", ["git", "diff", "--check"])]
    py_files = [path for path in files if path.endswith(".py")]
    if py_files:
        commands.append(("py_compile", [sys.executable, "-m", "py_compile", *py_files]))
    if any(path.startswith(PYTHON_RELEVANT_PREFIXES) for path in files):
        commands.append(("backend_regression", [sys.executable, "-m", "pytest", "-q", "tests/test_advisory_regression.py"]))
    if any(path.startswith(FRONTEND_RELEVANT_PREFIXES) for path in files):
        commands.append(("frontend_typecheck", ["npm", "--prefix", "apps/operator-web", "run", "typecheck"]))
        commands.append(("frontend_tests", ["npm", "--prefix", "apps/operator-web", "test", "--", "--run"]))

    for label, command in commands:
        lines.append(f"\n[analysis_agent_loop] post-check {label}: {' '.join(command)}")
        try:
            returncode, output = run_local_command(command, timeout_seconds=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            record_local_fallback_event(
                module="scripts.analysis_agent_loop",
                source=label,
                fallback_type="analysis_agent_post_check_timeout",
                severity="warn",
                reason="Analysis agent post-cycle validation command timed out; the cycle will stop with failed post-check status.",
                error=exc,
                metadata={"cycle": int(cycle), "timeout_seconds": int(timeout_seconds), "command": command},
            )
            ok = False
            lines.append(f"[analysis_agent_loop] post-check {label} timed out after {timeout_seconds}s")
            if exc.stdout:
                lines.append(str(exc.stdout))
            continue
        lines.append(output)
        lines.append(f"[analysis_agent_loop] post-check {label} returncode={returncode}")
        if returncode != 0:
            ok = False

    log_path.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines), flush=True)
    return ok


def main() -> int:
    args = parse_args()
    max_cycles = max(1, int(args.max_cycles))
    hard_max_cycles = max(1, int(args.hard_max_cycles))
    if max_cycles > hard_max_cycles and not args.allow_large_runs:
        print(
            f"[analysis_agent_loop] refusing max_cycles={max_cycles}; hard cap is {hard_max_cycles}. "
            "Use --allow-large-runs only for supervised runs.",
            file=sys.stderr,
            flush=True,
        )
        return 2
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    run_id = timestamp()

    artifacts = untracked_root_artifacts()
    if artifacts:
        print("[analysis_agent_loop] untracked root CSV artifacts detected; move/ignore them before running:", file=sys.stderr, flush=True)
        for path in artifacts:
            print(f"  - {path}", file=sys.stderr, flush=True)
        return 2

    for cycle in range(1, max_cycles + 1):
        before_snapshot = snapshot_changed_files()
        prompt = build_prompt(
            cycle=cycle,
            max_cycles=max_cycles,
            stop_on_safety=bool(args.stop_on_safety_marker),
            agent_mode=str(args.agent_mode),
        )
        prompt_path = log_dir / f"{run_id}_cycle_{cycle}_prompt.txt"
        output_path = log_dir / f"{run_id}_cycle_{cycle}_codex_output.txt"
        stdout_path = log_dir / f"{run_id}_cycle_{cycle}_stdout.log"
        postcheck_path = log_dir / f"{run_id}_cycle_{cycle}_postcheck.log"
        prompt_path.write_text(prompt, encoding="utf-8")

        print(f"[analysis_agent_loop] cycle={cycle}/{max_cycles} prompt={prompt_path}", flush=True)
        if args.dry_run:
            print("[analysis_agent_loop] dry-run; not invoking Codex", flush=True)
            continue

        returncode, stdout = run_codex(
            prompt=prompt,
            cwd=REPO_ROOT,
            output_path=output_path,
            timeout_seconds=int(args.codex_timeout_seconds),
        )
        stdout_path.write_text(stdout, encoding="utf-8")
        print(stdout, end="")
        print(f"[analysis_agent_loop] cycle={cycle} returncode={returncode} output={output_path}", flush=True)
        if returncode != 0:
            print(f"[analysis_agent_loop] Codex failed; stopping at cycle {cycle}", file=sys.stderr, flush=True)
            return returncode

        last_message = read_text(output_path)
        if not (
            "CYCLE_STATUS: complete" in last_message
            or "CYCLE_STATUS: blocked" in last_message
            or "CYCLE_STATUS: no_open_slices" in last_message
        ):
            print("[analysis_agent_loop] Codex output did not include a required CYCLE_STATUS marker; stopping", file=sys.stderr, flush=True)
            return 2
        marker = has_safety_marker(last_message) if args.stop_on_safety_marker else None
        if marker:
            print(f"[analysis_agent_loop] safety marker '{marker}' found in Codex output; stopping", file=sys.stderr, flush=True)
            return 2
        if "CYCLE_STATUS: blocked" in last_message or "CYCLE_STATUS: no_open_slices" in last_message:
            print("[analysis_agent_loop] terminal cycle status reached; stopping", flush=True)
            return 0
        changed_this_cycle = changed_since(before_snapshot, snapshot_changed_files())
        if changed_this_cycle:
            print("[analysis_agent_loop] files changed this cycle:", flush=True)
            for path in changed_this_cycle:
                print(f"  - {path}", flush=True)
        else:
            print("[analysis_agent_loop] no file changes detected for completed cycle", flush=True)
        if args.post_checks and not post_cycle_checks(
            cycle=cycle,
            log_path=postcheck_path,
            timeout_seconds=int(args.post_check_timeout_seconds),
            cycle_files=changed_this_cycle,
            max_files_per_cycle=int(args.max_files_per_cycle),
            allow_lockfile_drift=bool(args.allow_lockfile_drift),
        ):
            print(f"[analysis_agent_loop] post-checks failed; stopping at cycle {cycle}", file=sys.stderr, flush=True)
            return 2

    print("[analysis_agent_loop] max cycles completed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
