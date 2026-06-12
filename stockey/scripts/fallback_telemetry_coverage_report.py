from __future__ import annotations

import argparse
import ast
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_ROOTS = ("advisory", "data", "scripts", "utils")
FALLBACK_RECORDER_NAMES = {
    "_write_db_retry_telemetry",
    "record_fallback_event",
    "record_local_fallback_event",
}
LOG_METHOD_NAMES = {
    "critical",
    "debug",
    "error",
    "exception",
    "info",
    "warning",
}


@dataclass(frozen=True)
class ExceptionHandlerUse:
    path: str
    line: int
    function: str | None
    exception_type: str | None
    status: str
    reason: str


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return _call_name(node.func)
    return None


def _exception_type_name(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    if isinstance(node, ast.Tuple):
        names = [_exception_type_name(item) for item in node.elts]
        return ", ".join(name for name in names if name)
    return _call_name(node)


def _iter_python_files(root: Path, roots: Iterable[str]) -> list[Path]:
    files: list[Path] = []
    for relative_root in roots:
        base = root / relative_root
        if base.is_file() and base.suffix == ".py":
            files.append(base)
            continue
        if not base.exists():
            continue
        files.extend(path for path in base.rglob("*.py") if path.is_file())
    return sorted(files)


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current
    return None


def _handler_has_raise(handler: ast.ExceptHandler) -> bool:
    return any(isinstance(node, ast.Raise) for node in ast.walk(handler))


def _handler_records_fallback(handler: ast.ExceptHandler) -> bool:
    for node in ast.walk(handler):
        if not isinstance(node, ast.Call):
            continue
        name = (_call_name(node.func) or "").split(".")[-1]
        if name in FALLBACK_RECORDER_NAMES:
            return True
        if name.startswith("_record_") and "fallback" in name:
            return True
    return False


def _handler_logs(handler: ast.ExceptHandler) -> bool:
    for node in ast.walk(handler):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func) or ""
        if name == "print":
            return True
        if name.split(".")[-1] in LOG_METHOD_NAMES:
            return True
    return False


def _handler_returns_or_continues(handler: ast.ExceptHandler) -> bool:
    return any(isinstance(node, (ast.Return, ast.Continue, ast.Break)) for node in ast.walk(handler))


def _classify_handler(handler: ast.ExceptHandler) -> tuple[str, str]:
    if _handler_records_fallback(handler):
        return "records_fallback", "handler records a fallback/degraded-path telemetry event"
    if _handler_has_raise(handler):
        return "reraises", "handler raises after local handling"
    if _handler_logs(handler):
        if _handler_returns_or_continues(handler):
            return "logs_only", "handler logs but continues without durable fallback telemetry"
        return "logs_then_falls_through", "handler logs and falls through without durable fallback telemetry"
    if _handler_returns_or_continues(handler):
        return "silent_fallback", "handler continues without logging, raising, or fallback telemetry"
    return "silent_handler", "handler has no logging, raise, or fallback telemetry call"


def analyze_file(path: Path, *, root: Path) -> list[ExceptionHandlerUse]:
    relative_path = path.relative_to(root).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        from advisory.fallback_telemetry import record_local_fallback_event

        record_local_fallback_event(
            module="scripts.fallback_telemetry_coverage_report",
            source=relative_path,
            fallback_type="fallback_coverage_parse_failed",
            severity="warn",
            reason="Fallback telemetry coverage report could not parse a Python file and marked it as parse_error.",
            error=exc,
            metadata={"line": int(exc.lineno or 0)},
        )
        return [
            ExceptionHandlerUse(
                path=relative_path,
                line=int(exc.lineno or 0),
                function=None,
                exception_type=None,
                status="parse_error",
                reason=str(exc),
            )
        ]

    parents = _parent_map(tree)
    rows: list[ExceptionHandlerUse] = []
    for handler in ast.walk(tree):
        if not isinstance(handler, ast.ExceptHandler):
            continue
        fn = _enclosing_function(handler, parents)
        status, reason = _classify_handler(handler)
        rows.append(
            ExceptionHandlerUse(
                path=relative_path,
                line=int(getattr(handler, "lineno", 0) or 0),
                function=fn.name if fn else None,
                exception_type=_exception_type_name(handler.type),
                status=status,
                reason=reason,
            )
        )
    return sorted(rows, key=lambda row: (row.path, row.line))


def build_report(*, root: Path, roots: Iterable[str] = DEFAULT_ROOTS) -> dict[str, object]:
    rows: list[ExceptionHandlerUse] = []
    for path in _iter_python_files(root, roots):
        rows.extend(analyze_file(path, root=root))
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    return {
        "status": "ok",
        "root": str(root),
        "counts": dict(sorted(counts.items())),
        "rows": [asdict(row) for row in rows],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report exception handlers that may hide fallbacks without telemetry.")
    parser.add_argument("--root", default=".", help="Repository root")
    parser.add_argument("--roots", nargs="*", default=list(DEFAULT_ROOTS), help="Paths to scan relative to root")
    parser.add_argument("--format", choices=["json", "text"], default="text")
    parser.add_argument(
        "--fail-on-silent",
        action="store_true",
        help="Exit non-zero if silent fallback/handler rows are found",
    )
    args = parser.parse_args(argv)

    report = build_report(root=Path(args.root).resolve(), roots=args.roots)
    if args.format == "json":
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(f"status={report['status']} counts={report['counts']}")
        for row in report["rows"]:
            if row["status"] in {"silent_fallback", "silent_handler", "logs_only"}:
                print(
                    f"{row['path']}:{row['line']} "
                    f"function={row['function']} status={row['status']} reason={row['reason']}"
                )
    fail_statuses = {"silent_fallback", "silent_handler"}
    return 1 if args.fail_on_silent and any(int(report["counts"].get(status, 0)) for status in fail_statuses) else 0


if __name__ == "__main__":
    raise SystemExit(main())
