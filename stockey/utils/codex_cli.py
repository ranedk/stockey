from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable, TypeVar

from environs import Env
from pydantic import BaseModel, ValidationError


env = Env()
env.read_env()


DEFAULT_CODEX_BINARY = "codex"
DEFAULT_CODEX_MODEL = env("CODEX_CLI_MODEL", default="gpt-5.4-mini")
DEFAULT_CODEX_TIMEOUT_SECONDS = env.int("CODEX_CLI_TIMEOUT_SECONDS", default=300)


class CodexCLIError(RuntimeError):
    pass


ModelT = TypeVar("ModelT", bound=BaseModel)


def _candidate_codex_bins(configured: str) -> list[str]:
    candidates = [configured]
    home = Path.home()
    candidates.extend(
        str(path)
        for path in sorted((home / ".nvm" / "versions" / "node").glob("*/bin/codex"), reverse=True)
    )
    candidates.extend(
        [
            str(home / ".local" / "bin" / "codex"),
            str(home / ".npm-global" / "bin" / "codex"),
            "/opt/homebrew/bin/codex",
            "/usr/local/bin/codex",
        ]
    )
    deduped: list[str] = []
    for item in candidates:
        if item and item not in deduped:
            deduped.append(item)
    return deduped


def resolve_codex_binary(configured: str | None = None) -> str:
    effective = configured or env("CODEX_CLI_BIN", default=DEFAULT_CODEX_BINARY)
    for candidate in _candidate_codex_bins(effective):
        if "/" in candidate:
            if Path(candidate).exists():
                return candidate
            continue
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    from utils.fallback_telemetry import record_local_fallback_event

    searched = _candidate_codex_bins(effective)
    record_local_fallback_event(
        module="utils.codex_cli",
        source="codex_cli",
        fallback_type="codex_cli_binary_not_found",
        severity="error",
        reason="Codex CLI binary could not be found on PATH or common local install paths.",
        error=FileNotFoundError(effective),
        metadata={"configured_binary": effective, "searched": searched[:20]},
    )
    raise CodexCLIError(
        "Codex CLI binary was not found. Set CODEX_CLI_BIN to the absolute codex path "
        "or ensure cron/non-interactive PATH includes the Codex install directory."
    )


def run_codex_cli(
    prompt: str,
    *,
    model: str | None = None,
    images: Iterable[str | Path] | None = None,
    timeout_seconds: int | None = None,
    cwd: str | Path | None = None,
) -> str:
    effective_model = model or DEFAULT_CODEX_MODEL
    with tempfile.NamedTemporaryFile("r", encoding="utf-8", suffix=".txt", delete=True) as output_file:
        cmd = [
            resolve_codex_binary(),
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--model",
            effective_model,
            "--output-last-message",
            output_file.name,
        ]
        for image_path in images or []:
            cmd.extend(["--image", str(image_path)])
        cmd.append("-")
        proc = subprocess.run(
            cmd,
            input=prompt,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd) if cwd is not None else None,
            timeout=timeout_seconds or DEFAULT_CODEX_TIMEOUT_SECONDS,
            check=False,
        )
        if proc.returncode != 0:
            raise CodexCLIError(
                f"Codex CLI failed with exit code {proc.returncode}: {(proc.stderr or proc.stdout).strip()}"
            )
        output_file.seek(0)
        message = output_file.read().strip()
    if not message:
        message = (proc.stdout or "").strip()
    return _strip_codex_fences(message)


def run_codex_structured(
    prompt: str,
    *,
    response_model: type[ModelT],
    model: str | None = None,
    system_prompt: str | None = None,
    max_attempts: int = 2,
    timeout_seconds: int | None = None,
) -> ModelT:
    schema = response_model.model_json_schema()
    last_error: Exception | None = None
    previous_error = ""
    for attempt in range(1, max(int(max_attempts), 1) + 1):
        full_prompt = _build_structured_prompt(
            prompt,
            schema=schema,
            system_prompt=system_prompt,
            previous_error=previous_error,
        )
        text = run_codex_cli(full_prompt, model=model, timeout_seconds=timeout_seconds)
        try:
            payload = extract_json_object(text)
            return response_model.model_validate(payload)
        except (ValueError, TypeError, ValidationError) as exc:
            from utils.fallback_telemetry import record_local_fallback_event

            record_local_fallback_event(
                module="utils.codex_cli",
                source=response_model.__name__,
                fallback_type="codex_structured_validation_retry",
                severity="warn",
                reason="Codex structured response failed JSON/schema validation and will be retried if attempts remain.",
                error=exc,
                metadata={
                    "attempt": attempt,
                    "max_attempts": max(int(max_attempts), 1),
                    "model": model or DEFAULT_CODEX_MODEL,
                    "response_excerpt": text[:500],
                },
            )
            last_error = exc
            previous_error = (
                f"Previous attempt {attempt} failed validation: {exc}. "
                "Return corrected JSON only."
            )
    raise CodexCLIError(f"Codex structured response failed validation: {last_error}") from last_error


def extract_json_object(text: str) -> object:
    stripped = _strip_codex_fences(text).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        from utils.fallback_telemetry import record_local_fallback_event

        record_local_fallback_event(
            module="utils.codex_cli",
            source="extract_json_object",
            fallback_type="codex_json_direct_parse_failed",
            severity="warn",
            reason="Codex response was not direct JSON; attempting bounded JSON object extraction fallback.",
            error=exc,
            metadata={"response_excerpt": stripped[:500]},
        )
    match = re.search(r"(\{.*\}|\[.*\])", stripped, flags=re.DOTALL)
    if not match:
        raise ValueError("Codex response did not contain JSON")
    return json.loads(match.group(1))


def _build_structured_prompt(
    prompt: str,
    *,
    schema: dict,
    system_prompt: str | None,
    previous_error: str,
) -> str:
    sections = []
    if system_prompt:
        sections.append(f"# System instructions\n{system_prompt.strip()}")
    sections.append(
        "# Output contract\n"
        "Return exactly one valid JSON object. Do not wrap it in markdown. "
        "Do not include prose before or after the JSON. The JSON must validate against this schema:\n"
        f"{json.dumps(schema, ensure_ascii=False, indent=2, default=str)}"
    )
    if previous_error:
        sections.append(f"# Validation error to fix\n{previous_error}")
    sections.append(f"# Task\n{prompt.strip()}")
    return "\n\n".join(sections)


def _strip_codex_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 2:
            return "\n".join(lines[1:-1]).strip()
    return stripped
