from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_EXCLUDE_PARTS = {
    ".git",
    ".mypy_cache",
    ".nuxt",
    ".output",
    ".pytest_cache",
    ".ruff_cache",
    ".xstockey",
    "__pycache__",
    "http_cache",
    "live_dashboard",
    "logs",
    "node_modules",
}

DEFAULT_EXTENSIONS = {
    ".crontab",
    ".env",
    ".example",
    ".js",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".template",
    ".ts",
    ".vue",
    ".yaml",
    ".yml",
}

IGNORED_ENV_NAMES = {
    "BASH_SOURCE",
    "CDPATH",
    "COMPONENT",
    "HOME",
    "IFS",
    "OLDPWD",
    "OPTARG",
    "OPTIND",
    "PATH",
    "PWD",
    "RANDOM",
    "SECONDS",
    "SHELL",
    "SHLVL",
    "TMPDIR",
    "UID",
    "USER",
    # Local shell/script variables derived from documented runtime settings.
    "API_BASE",
    "API_HOST",
    "API_PORT",
    "API_TIMEOUT_MS",
    "COMMAND_FILE",
    "HEALTH_FAILURE_LIMIT",
    "HEALTH_TIMEOUT_SECONDS",
    "INITIAL_MEMORY",
    "INSTALL_DEPS",
    "LIMIT",
    "LOCK_DIR",
    "LOCK_FILE",
    "MAX_ATTEMPTS",
    "MAX_CYCLES",
    "MAX_TASKS",
    "NPM_INSTALL_ARGS",
    "NPM_LEGACY_PEER_DEPS",
    "NVM_DIR",
    "NVM_VERSION",
    "PID_FILE",
    "REPO_ROOT",
    "REPO_VENV_PYTHON",
    "SCRIPT_DIR",
    "SCRIPT_NAME",
    "SLEEP_SECONDS",
    "TAIL_LINES",
    "TIMEOUT_SECONDS",
    "USE_NVM",
    "WEB_DIR",
    "WEB_HOST",
    "WEB_PORT",
    "WATCHER_LOCK_COMMAND_FILE",
    "WATCHER_LOCK_DIR",
    "WATCHER_LOCK_EXPECTED_COMMAND",
    "WATCHER_LOCK_FILE",
    "WATCHER_LOCK_IS_CURRENT",
    "WATCHER_LOCK_PID",
    "WATCHER_LOCK_PID_FILE",
    "WATCHER_LOCK_PROCESS_COMMAND",
}

ENV_PATTERNS = [
    re.compile(r"\benv\.(?:str|int|bool|float|list|path|url)\(\s*['\"](?P<name>[A-Z][A-Z0-9_]*)['\"]"),
    re.compile(r"\bos\.getenv\(\s*['\"](?P<name>[A-Z][A-Z0-9_]*)['\"]"),
    re.compile(r"\bos\.environ\.get\(\s*['\"](?P<name>[A-Z][A-Z0-9_]*)['\"]"),
    re.compile(r"\bos\.environ\[\s*['\"](?P<name>[A-Z][A-Z0-9_]*)['\"]\s*\]"),
    re.compile(r"\$\{(?P<name>[A-Z][A-Z0-9_]*)(?::-[^}]*)?\}"),
    re.compile(r"(?<![A-Za-z0-9_])\$(?P<name>[A-Z][A-Z0-9_]*)\b"),
]


@dataclass(frozen=True)
class EnvUsage:
    name: str
    path: str
    line: int
    context: str


def parse_env_example_names(text: str) -> set[str]:
    names: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name = line.split("=", 1)[0].strip()
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            names.add(name)
    return names


def should_scan_path(path: Path, repo_root: Path = REPO_ROOT) -> bool:
    try:
        relative = path.relative_to(repo_root)
    except ValueError as exc:
        from advisory.fallback_telemetry import record_local_fallback_event

        record_local_fallback_event(
            module="scripts.env_example_audit",
            fallback_type="env_example_audit_relative_path_failed",
            source="env_example_audit",
            severity="warn",
            reason="Env example audit received a path outside the repo root and will evaluate it as provided.",
            error=exc,
            metadata={"path": str(path), "repo_root": str(repo_root)},
        )
        relative = path
    if any(part in DEFAULT_EXCLUDE_PARTS for part in relative.parts):
        return False
    if path.name == ".env":
        return False
    if path.name == ".env.example":
        return False
    return path.suffix in DEFAULT_EXTENSIONS or any(path.name.endswith(ext) for ext in DEFAULT_EXTENSIONS)


def iter_scan_files(repo_root: Path = REPO_ROOT) -> Iterable[Path]:
    for path in repo_root.rglob("*"):
        if path.is_file() and should_scan_path(path, repo_root=repo_root):
            yield path


def extract_env_usages_from_text(text: str, path: str) -> list[EnvUsage]:
    usages: list[EnvUsage] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for pattern in ENV_PATTERNS:
            for match in pattern.finditer(line):
                name = match.group("name")
                if name in IGNORED_ENV_NAMES:
                    continue
                usages.append(
                    EnvUsage(
                        name=name,
                        path=path,
                        line=line_number,
                        context=line.strip()[:240],
                    )
                )
    return usages


def collect_env_usages(repo_root: Path = REPO_ROOT) -> list[EnvUsage]:
    usages: list[EnvUsage] = []
    for path in iter_scan_files(repo_root):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            from advisory.fallback_telemetry import record_local_fallback_event

            record_local_fallback_event(
                module="scripts.env_example_audit",
                source=path.relative_to(repo_root).as_posix() if path.is_relative_to(repo_root) else str(path),
                fallback_type="env_example_audit_decode_failed",
                severity="warn",
                reason="Env example audit skipped a non-UTF-8 file while scanning runtime environment usage.",
                error=exc,
                metadata={"path": str(path)},
            )
            continue
        relative = path.relative_to(repo_root).as_posix()
        usages.extend(extract_env_usages_from_text(text, relative))
    return usages


def build_report(repo_root: Path = REPO_ROOT, env_example_path: Path | None = None) -> dict[str, object]:
    env_example = env_example_path or repo_root / ".env.example"
    documented = parse_env_example_names(env_example.read_text(encoding="utf-8")) if env_example.exists() else set()
    usages = collect_env_usages(repo_root)
    used_names = {usage.name for usage in usages}
    by_name: dict[str, list[dict[str, object]]] = {}
    for usage in usages:
        by_name.setdefault(usage.name, []).append(
            {
                "path": usage.path,
                "line": usage.line,
                "context": usage.context,
            }
        )
    missing = sorted(used_names - documented)
    unused = sorted(documented - used_names)
    return {
        "status": "ok" if not missing else "missing_env_example_entries",
        "documented_count": len(documented),
        "used_count": len(used_names),
        "missing": missing,
        "unused_documented": unused,
        "usage": {name: by_name[name][:10] for name in sorted(by_name)},
    }


def format_text_report(report: dict[str, object]) -> str:
    lines = [
        f"status: {report['status']}",
        f"used env vars: {report['used_count']}",
        f"documented env vars: {report['documented_count']}",
    ]
    missing = report["missing"]
    if missing:
        lines.append("missing from .env.example:")
        lines.extend(f"- {name}" for name in missing)
    else:
        lines.append("missing from .env.example: none")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit environment variables used by Stockey against .env.example.")
    parser.add_argument("--repo-root", default=str(REPO_ROOT), help="Repository root to scan.")
    parser.add_argument("--env-example", default=None, help="Path to .env.example. Defaults to <repo-root>/.env.example.")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="Output format.")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when used variables are missing from .env.example.")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    env_example_path = Path(args.env_example).resolve() if args.env_example else None
    report = build_report(repo_root=repo_root, env_example_path=env_example_path)

    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_text_report(report))
    return 1 if args.strict and report["missing"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
