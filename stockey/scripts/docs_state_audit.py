from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from advisory.fallback_telemetry import record_local_fallback_event


REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_DOC_PATHS = (
    "README.md",
    "analysis.md",
    "todo.md",
    "docs",
    "apps/operator-web/README.md",
)

EXCLUDED_PARTS = {
    ".git",
    ".nuxt",
    ".output",
    ".pytest_cache",
    ".xstockey",
    "node_modules",
    "logs",
}


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    path: str
    line: int
    message: str
    context: str

    def as_dict(self) -> dict[str, object]:
        return {
            "severity": self.severity,
            "code": self.code,
            "path": self.path,
            "line": self.line,
            "message": self.message,
            "context": self.context,
        }


REQUIRED_COVERAGE = {
    "README.md": [
        ("Nuxt operator frontend", re.compile(r"\bNuxt\b.*\boperator frontend\b", re.IGNORECASE | re.DOTALL)),
        ("go-crond command", re.compile(r"\./go-crond\s+config/stockey\.generated\.crontab\s+--allow-unprivileged")),
        ("weekly all_ml", re.compile(r"\ball_ml\.sh\b.*\bweekly\b|\bweekly\b.*\ball_ml\.sh\b", re.IGNORECASE | re.DOTALL)),
        ("daily research evidence", re.compile(r"\ball_research_evidence\.sh\b.*\bresearch[- ]only\b|\bresearch[- ]only\b.*\ball_research_evidence\.sh\b", re.IGNORECASE | re.DOTALL)),
        ("daily all_advisory", re.compile(r"\ball_advisory\.sh\b.*\bonce daily\b|\bonce daily\b.*\ball_advisory\.sh\b", re.IGNORECASE | re.DOTALL)),
    ],
    "todo.md": [
        ("current roadmap date", re.compile(r"Updated:\s*`2026-06-\d{2}`")),
        ("UI-first operations priority", re.compile(r"Highest Priority:\s*UI-First Operations", re.IGNORECASE)),
        ("Nuxt replaces static dashboard", re.compile(r"Nuxt operator frontend replaces the old static HTML dashboard path", re.IGNORECASE)),
    ],
    "analysis.md": [
        ("analysis date", re.compile(r"Date:\s*2026-06-\d{2}")),
        ("recommended next development order", re.compile(r"Recommended Next Development Order", re.IGNORECASE)),
        ("confidence statement", re.compile(r"Immediate Confidence Statement", re.IGNORECASE)),
    ],
    "docs/operators_manual.md": [
        ("operator decision effects", re.compile(r"operator decision effects", re.IGNORECASE)),
        ("frontend replaces static dashboard", re.compile(r"Static `live_dashboard/` generation is deprecated|Nuxt operator frontend", re.IGNORECASE)),
        ("research evidence cron", re.compile(r"22:20.*all_research_evidence\.sh|all_research_evidence\.sh.*22:20", re.IGNORECASE | re.DOTALL)),
    ],
    "docs/scripts.md": [
        ("script inventory", re.compile(r"Script Inventory", re.IGNORECASE)),
        ("env audit", re.compile(r"env_example_audit\.py")),
        ("research evidence wrapper", re.compile(r"all_research_evidence\.sh.*advisory\.research_evidence_runner|advisory\.research_evidence_runner.*all_research_evidence\.sh", re.IGNORECASE | re.DOTALL)),
    ],
    "docs/index.md": [
        ("research evidence operator path", re.compile(r"all_research_evidence\.sh.*lighter daily evidence-refresh path|lighter daily evidence-refresh path.*all_research_evidence\.sh", re.IGNORECASE | re.DOTALL)),
    ],
    "docs/tool_registry.json": [
        ("research evidence operator command", re.compile(r'"name"\s*:\s*"run_research_evidence".*"command"\s*:\s*\[\s*"bash"\s*,\s*"all_research_evidence\.sh"\s*\]', re.IGNORECASE | re.DOTALL)),
    ],
    "config/stockey.crontab.template": [
        ("research evidence scheduled template", re.compile(r"all_research_evidence\.sh.*research_evidence\.log|research_evidence\.log.*all_research_evidence\.sh", re.IGNORECASE)),
    ],
    "config/stockey.generated.crontab": [
        ("research evidence scheduled generated", re.compile(r"all_research_evidence\.sh.*research_evidence\.log|research_evidence\.log.*all_research_evidence\.sh", re.IGNORECASE)),
    ],
}


STALE_ERROR_PATTERNS = [
    ("removed_all_full_advisory", re.compile(r"\ball_full_advisory\.sh\b")),
    ("removed_all_model_training", re.compile(r"\ball_model_training\.sh\b")),
    ("removed_static_dashboard_command", re.compile(r"python\s+-m\s+advisory\.live_dashboard")),
]

STALE_WARNING_PATTERNS = [
    ("legacy_promotion_audit_label", re.compile(r"\bpromotion[- ]audit\b", re.IGNORECASE)),
    ("legacy_live_dashboard_path", re.compile(r"\blive_dashboard/")),
]

WARNING_ALLOWLIST = {
    "legacy_promotion_audit_label": re.compile(
        r"\b(legacy|backend|table|endpoint|migration|rename|renamed|compatibility|audit row|promotion-audit)\b",
        re.IGNORECASE,
    ),
    "legacy_live_dashboard_path": re.compile(
        r"\b(deprecated|legacy|remove|removed|reuse|reuses|compatibility|not rebuild|operator-feed|if you run it directly)\b",
        re.IGNORECASE,
    ),
}


def should_scan_path(path: Path, repo_root: Path = REPO_ROOT) -> bool:
    try:
        relative = path.relative_to(repo_root)
    except ValueError as exc:
        record_local_fallback_event(
            module="scripts.docs_state_audit",
            fallback_type="docs_state_audit_relative_path_failed",
            source="docs_state_audit",
            severity="warn",
            reason="Docs state audit received a path outside the repo root and will evaluate it as provided.",
            error=exc,
            metadata={"path": str(path), "repo_root": str(repo_root)},
        )
        relative = path
    if any(part in EXCLUDED_PARTS for part in relative.parts):
        return False
    return path.is_file() and path.suffix == ".md"


def iter_doc_files(paths: Iterable[str], repo_root: Path = REPO_ROOT) -> Iterable[Path]:
    seen: set[Path] = set()
    for raw in paths:
        path = (repo_root / raw).resolve()
        if path.is_dir():
            candidates = sorted(path.rglob("*.md"))
        else:
            candidates = [path]
        for candidate in candidates:
            if not candidate.exists() or not should_scan_path(candidate, repo_root=repo_root):
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            yield candidate


def line_finding(
    *,
    severity: str,
    code: str,
    path: Path,
    line_number: int,
    message: str,
    line: str,
    repo_root: Path = REPO_ROOT,
) -> Finding:
    return Finding(
        severity=severity,
        code=code,
        path=path.relative_to(repo_root).as_posix(),
        line=line_number,
        message=message,
        context=line.strip()[:240],
    )


def check_stale_terms(path: Path, text: str, repo_root: Path = REPO_ROOT) -> list[Finding]:
    findings: list[Finding] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for code, pattern in STALE_ERROR_PATTERNS:
            if pattern.search(line):
                findings.append(
                    line_finding(
                        severity="error",
                        code=code,
                        path=path,
                        line_number=line_number,
                        message="Removed operator script/static-dashboard command is still documented.",
                        line=line,
                        repo_root=repo_root,
                    )
                )
        for code, pattern in STALE_WARNING_PATTERNS:
            if not pattern.search(line):
                continue
            allowlist = WARNING_ALLOWLIST.get(code)
            if allowlist and allowlist.search(line):
                continue
            findings.append(
                line_finding(
                    severity="warning",
                    code=code,
                    path=path,
                    line_number=line_number,
                    message="Legacy term is still visible without an explicit compatibility/deprecation note.",
                    line=line,
                    repo_root=repo_root,
                )
            )
    return findings


def check_required_coverage(repo_root: Path = REPO_ROOT) -> list[Finding]:
    findings: list[Finding] = []
    for relative_path, checks in REQUIRED_COVERAGE.items():
        path = repo_root / relative_path
        if not path.exists():
            findings.append(
                Finding(
                    severity="error",
                    code="missing_required_doc",
                    path=relative_path,
                    line=0,
                    message="Required documentation file is missing.",
                    context="",
                )
            )
            continue
        text = path.read_text(encoding="utf-8")
        for label, pattern in checks:
            if not pattern.search(text):
                findings.append(
                    Finding(
                        severity="error",
                        code="missing_required_coverage",
                        path=relative_path,
                        line=0,
                        message=f"Missing canonical documentation coverage: {label}.",
                        context="",
                    )
                )
    return findings


def build_report(paths: Iterable[str], repo_root: Path = REPO_ROOT) -> dict[str, object]:
    findings: list[Finding] = []
    scanned: list[str] = []
    for path in iter_doc_files(paths, repo_root=repo_root):
        scanned.append(path.relative_to(repo_root).as_posix())
        text = path.read_text(encoding="utf-8")
        findings.extend(check_stale_terms(path, text, repo_root=repo_root))
    findings.extend(check_required_coverage(repo_root=repo_root))
    errors = [finding for finding in findings if finding.severity == "error"]
    warnings = [finding for finding in findings if finding.severity == "warning"]
    return {
        "status": "ok" if not errors else "error",
        "scanned_files": len(scanned),
        "errors": len(errors),
        "warnings": len(warnings),
        "findings": [finding.as_dict() for finding in findings],
    }


def format_text_report(report: dict[str, object]) -> str:
    lines = [
        f"status: {report['status']}",
        f"scanned files: {report['scanned_files']}",
        f"errors: {report['errors']}",
        f"warnings: {report['warnings']}",
    ]
    findings = report["findings"]
    if findings:
        lines.append("findings:")
        for item in findings:
            line = item["line"]
            location = f"{item['path']}:{line}" if line else item["path"]
            lines.append(f"- {item['severity']} {item['code']} {location}: {item['message']}")
            if item["context"]:
                lines.append(f"  {item['context']}")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit Stockey docs for stale operator terminology and required roadmap coverage. "
            "This does not rewrite files."
        )
    )
    parser.add_argument("--format", choices=["json", "text"], default="text")
    parser.add_argument(
        "--paths",
        nargs="*",
        default=list(DEFAULT_DOC_PATHS),
        help="Doc files/directories to scan. Defaults to README, analysis, todo, docs, and operator-web README.",
    )
    parser.add_argument("--strict", action="store_true", help="Exit non-zero on errors.")
    parser.add_argument("--strict-warnings", action="store_true", help="Exit non-zero on warnings too.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_report(args.paths)
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_text_report(report))
    if args.strict and report["errors"]:
        return 1
    if args.strict_warnings and (report["errors"] or report["warnings"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
