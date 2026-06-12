from __future__ import annotations

import argparse
import ast
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_ROOTS = ("advisory", "data", "scripts", "utils")
RETRY_WRAPPER_NAMES = {"execute_db_operation", "with_db_retries"}
CORE_DB_HELPERS = {
    "utils/db.py",
}


@dataclass(frozen=True)
class DbSessionUse:
    path: str
    line: int
    function: str | None
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


def _is_db_session_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    name = _call_name(node.func)
    return name == "db_session" or (name or "").endswith(".db_session")


def _is_retry_wrapper_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    name = _call_name(node.func)
    return (name or "").split(".")[-1] in RETRY_WRAPPER_NAMES


def _called_name_args(node: ast.Call) -> set[str]:
    names: set[str] = set()
    for arg in node.args:
        if isinstance(arg, ast.Name):
            names.add(arg.id)
    return names


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


def analyze_file(path: Path, *, root: Path) -> list[DbSessionUse]:
    relative_path = path.relative_to(root).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        from advisory.fallback_telemetry import record_local_fallback_event

        record_local_fallback_event(
            module="scripts.db_retry_coverage_report",
            source=relative_path,
            fallback_type="db_retry_coverage_parse_failed",
            severity="warn",
            reason="DB retry coverage report could not parse a Python file and marked it as parse_error.",
            error=exc,
            metadata={"line": int(exc.lineno or 0)},
        )
        return [
            DbSessionUse(
                path=relative_path,
                line=int(exc.lineno or 0),
                function=None,
                status="parse_error",
                reason=str(exc),
            )
        ]

    parents = _parent_map(tree)
    retry_wrapped_functions: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_retry_wrapper_call(node):
            retry_wrapped_functions.update(_called_name_args(node))

    rows: list[DbSessionUse] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        if not any(_is_db_session_call(item.context_expr) for item in node.items):
            continue
        fn = _enclosing_function(node, parents)
        fn_name = fn.name if fn else None
        if relative_path in CORE_DB_HELPERS:
            status = "core_helper"
            reason = "core db helper implements shared retry behavior"
        elif fn_name and fn_name in retry_wrapped_functions:
            status = "wrapped"
            reason = f"{fn_name} is passed to a retry wrapper"
        else:
            status = "direct"
            reason = "no enclosing function was found as a retry-wrapper argument"
        rows.append(
            DbSessionUse(
                path=relative_path,
                line=int(getattr(node, "lineno", 0) or 0),
                function=fn_name,
                status=status,
                reason=reason,
            )
        )
    return sorted(rows, key=lambda row: (row.path, row.line))


def build_report(*, root: Path, roots: Iterable[str] = DEFAULT_ROOTS) -> dict[str, object]:
    rows: list[DbSessionUse] = []
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
    parser = argparse.ArgumentParser(description="Report direct db_session() usage and retry-wrapper coverage.")
    parser.add_argument("--root", default=".", help="Repository root")
    parser.add_argument("--roots", nargs="*", default=list(DEFAULT_ROOTS), help="Paths to scan relative to root")
    parser.add_argument("--format", choices=["json", "text"], default="text")
    parser.add_argument("--fail-on-direct", action="store_true", help="Exit non-zero if direct db_session usage is found")
    args = parser.parse_args(argv)

    report = build_report(root=Path(args.root).resolve(), roots=args.roots)
    if args.format == "json":
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(f"status={report['status']} counts={report['counts']}")
        for row in report["rows"]:
            if row["status"] == "direct":
                print(f"{row['path']}:{row['line']} function={row['function']} reason={row['reason']}")
    return 1 if args.fail_on_direct and int(report["counts"].get("direct", 0)) else 0


if __name__ == "__main__":
    raise SystemExit(main())
