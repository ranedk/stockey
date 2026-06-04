#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG_DIR = REPO_ROOT / "logs" / "codex_supervisor"


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def read_tail(path: Path, line_count: int) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        end = handle.tell()
        block_size = 8192
        data = b""
        while end > 0 and data.count(b"\n") <= line_count:
            read_size = min(block_size, end)
            end -= read_size
            handle.seek(end)
            data = handle.read(read_size) + data
    return b"\n".join(data.splitlines()[-line_count:]).decode("utf-8", errors="replace")


def run_command(command: list[str], *, cwd: Path, log_path: Path) -> int:
    print(f"[codex_supervisor] running command={' '.join(shlex.quote(part) for part in command)}", flush=True)
    print(f"[codex_supervisor] log={log_path}", flush=True)
    with log_path.open("w", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            print(line, end="")
            log_file.write(line)
            log_file.flush()
        return int(proc.wait())


def build_codex_prompt(
    *,
    command: list[str],
    attempt: int,
    returncode: int,
    log_tail: str,
    log_path: Path,
) -> str:
    command_text = " ".join(shlex.quote(part) for part in command)
    return f"""
You are Codex running inside the Stockey repository.

The supervised command failed.

Command:
{command_text}

Attempt: {attempt}
Return code: {returncode}
Full log path:
{log_path}

Last log lines:
```text
{log_tail}
```

Task:
- Diagnose the failure from the log and repository context.
- Make the smallest safe code/config/doc updates needed to fix the issue.
- Preserve user changes and do not revert unrelated work.
- Run targeted validation for the fix if feasible.
- Do not run the long supervised command yourself; this supervisor will rerun it after you exit.
- If the issue is external/transient and no code change is appropriate, add clearer handling/logging so the long run can continue or fail with an actionable message.
""".strip()


def run_codex_fix(
    *,
    prompt: str,
    cwd: Path,
    output_path: Path,
    timeout_seconds: int,
) -> int:
    codex_bin = os.environ.get("CODEX_SUPERVISOR_BIN") or os.environ.get("CODEX_CLI_BIN") or "codex"
    model = os.environ.get("CODEX_SUPERVISOR_MODEL") or os.environ.get("CODEX_CLI_MODEL")
    sandbox = os.environ.get("CODEX_SUPERVISOR_SANDBOX", "danger-full-access")
    cmd = [
        codex_bin,
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        sandbox,
        "--output-last-message",
        str(output_path),
    ]
    if model:
        cmd.extend(["--model", model])
    cmd.append("-")
    print(f"[codex_supervisor] invoking codex sandbox={sandbox} output={output_path}", flush=True)
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
    print(proc.stdout or "", end="")
    return int(proc.returncode)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a long command, ask Codex CLI to fix failures from the last log lines, then rerun."
    )
    parser.add_argument("--max-attempts", type=int, default=int(os.environ.get("CODEX_SUPERVISOR_MAX_ATTEMPTS", "3")))
    parser.add_argument("--tail-lines", type=int, default=int(os.environ.get("CODEX_SUPERVISOR_TAIL_LINES", "100")))
    parser.add_argument("--log-dir", default=os.environ.get("CODEX_SUPERVISOR_LOG_DIR", str(DEFAULT_LOG_DIR)))
    parser.add_argument("--codex-timeout-seconds", type=int, default=int(os.environ.get("CODEX_SUPERVISOR_TIMEOUT_SECONDS", "1800")))
    parser.add_argument("--no-fix", action="store_true", help="Only run and print the failure tail; do not invoke Codex.")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Command to supervise, after --")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("Provide a command after --, for example: scripts/codex_supervised_runner.py -- ./all_advisory.sh")

    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    run_id = timestamp()
    max_attempts = max(1, int(args.max_attempts))

    for attempt in range(1, max_attempts + 1):
        log_path = log_dir / f"{run_id}_attempt_{attempt}.log"
        returncode = run_command(command, cwd=REPO_ROOT, log_path=log_path)
        tail = read_tail(log_path, int(args.tail_lines))
        tail_path = log_dir / f"{run_id}_attempt_{attempt}_tail.txt"
        tail_path.write_text(tail, encoding="utf-8")
        print(f"[codex_supervisor] command exited returncode={returncode} attempt={attempt}/{max_attempts}", flush=True)
        print(f"[codex_supervisor] tail_path={tail_path}", flush=True)
        if returncode == 0:
            print("[codex_supervisor] success", flush=True)
            return 0
        if attempt >= max_attempts:
            print("[codex_supervisor] max attempts reached; last 100 lines follow", file=sys.stderr, flush=True)
            print(tail, file=sys.stderr)
            return returncode
        if args.no_fix:
            print("[codex_supervisor] --no-fix set; not invoking Codex", file=sys.stderr, flush=True)
            return returncode

        prompt = build_codex_prompt(
            command=command,
            attempt=attempt,
            returncode=returncode,
            log_tail=tail,
            log_path=log_path,
        )
        prompt_path = log_dir / f"{run_id}_attempt_{attempt}_codex_prompt.txt"
        output_path = log_dir / f"{run_id}_attempt_{attempt}_codex_output.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        codex_returncode = run_codex_fix(
            prompt=prompt,
            cwd=REPO_ROOT,
            output_path=output_path,
            timeout_seconds=int(args.codex_timeout_seconds),
        )
        if codex_returncode != 0:
            print(
                f"[codex_supervisor] codex failed returncode={codex_returncode}; not rerunning command",
                file=sys.stderr,
                flush=True,
            )
            return codex_returncode
        print("[codex_supervisor] codex completed; rerunning supervised command", flush=True)

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
