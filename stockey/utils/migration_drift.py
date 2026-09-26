"""Detect schema migrations whose DDL was edited after being applied.

The append-only migration guard (``utils.schema_migrations.apply_schema_migration``)
hard-fails when a migration's statements no longer hash to the checksum recorded when
it was applied. Editing an already-applied ``CREATE TABLE`` body (or appending an
``ALTER`` to a base migration) causes exactly that -- a latent failure that only fires
the next time the migration runs. This module finds such drift ahead of time.

It is read-only: it intercepts ``apply_schema_migration`` so nothing is applied, invokes
the zero-arg ``ensure_*`` entry points to capture the migrations the code *would* apply,
and compares each captured checksum to the value recorded in ``stockey_schema_migrations``.
"""
from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
_SEARCH_DIRS = ("utils", "data")


def _module_dotted_paths() -> list[str]:
    paths: set[str] = set()
    for directory in _SEARCH_DIRS:
        base = REPO_ROOT / directory
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            if "apply_schema_migration" in text:
                rel = path.relative_to(REPO_ROOT).with_suffix("")
                paths.add(".".join(rel.parts))
    return sorted(paths)


def _has_required_args(fn: Any) -> bool:
    for param in inspect.signature(fn).parameters.values():
        if param.default is inspect.Parameter.empty and param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            return True
    return False


def capture_code_migrations() -> dict[str, list[str]]:
    """Return ``{migration_id: statements}`` the current code would apply, applying nothing.

    Thin wrapper around :func:`capture_code_migrations_with_errors` for existing callers
    that only care about the successfully-captured migrations. Prefer the ``_with_errors``
    form for anything that reports status to a human -- a module whose ``ensure*()`` raises
    is silently excluded here, which reads as "no drift" even though the migration was
    never actually checked.
    """
    recorded, _errors = capture_code_migrations_with_errors()
    return recorded


def capture_code_migrations_with_errors() -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
    """Like ``capture_code_migrations`` but also returns the ``ensure*()`` calls that raised.

    BUG FOUND LIVE 2026-08-19 (re-audit): the original ``except Exception: continue`` here
    dropped a failing ``ensure*()`` on the floor with no record at all -- any migration whose
    entry point currently raises (a real code bug, a broken import, anything) was silently
    excluded from drift-checking rather than being reported as "could not verify". A
    genuinely drifted migration behind a broken ``ensure*()`` would report clean (``[]``)
    from ``find_migration_drift()`` even though nothing was actually checked. Callers that
    need the failures now get them explicitly instead of a false "ok".
    """
    recorded: dict[str, list[str]] = {}
    errors: list[dict[str, Any]] = []

    def _recorder(**kwargs: Any) -> dict[str, str]:
        migration_id = kwargs.get("migration_id")
        if migration_id:
            recorded[str(migration_id)] = list(kwargs.get("statements") or [])
        return {"status": "applied"}

    for dotted in _module_dotted_paths():
        try:
            module = importlib.import_module(dotted)
        except Exception as exc:
            errors.append({"module": dotted, "function": None, "error": f"{type(exc).__name__}: {exc}"})
            continue
        if not hasattr(module, "apply_schema_migration"):
            continue
        original = module.apply_schema_migration
        module.apply_schema_migration = _recorder  # type: ignore[attr-defined]
        try:
            for name, fn in inspect.getmembers(module, inspect.isfunction):
                if fn.__module__ != dotted or not name.startswith("ensure"):
                    continue
                if _has_required_args(fn):
                    continue
                try:
                    fn()
                except Exception as exc:
                    errors.append({"module": dotted, "function": name, "error": f"{type(exc).__name__}: {exc}"})
                    continue
        finally:
            module.apply_schema_migration = original  # type: ignore[attr-defined]
    return recorded, errors


def find_migration_drift() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(drift, capture_errors)`` -- applied migrations whose current code checksum
    differs from the DB record, plus any ``ensure*()`` that raised while capturing.

    An empty ``drift`` list means no drift among the migrations that *could* be captured --
    it does NOT mean everything was checked. A non-empty ``capture_errors`` means some
    migration(s) were never compared at all and their drift status is unknown; callers
    should treat that as a reportable problem in its own right, not silently fold it into
    "ok". Each ``drift`` entry has ``migration_id``, ``db_checksum``, ``code_checksum`` and
    ``status``. Returns ``([], capture_errors)`` if the migration ledger cannot be read
    (nothing to compare against).
    """
    from utils.db import sql_to_df
    from utils.schema_migrations import checksum_statements

    recorded, capture_errors = capture_code_migrations_with_errors()
    try:
        ledger = sql_to_df("SELECT migration_id, checksum, status FROM stockey_schema_migrations")
    except Exception:
        return [], capture_errors
    db_by_id = {
        str(row["migration_id"]): (str(row["checksum"]), str(row["status"]))
        for _, row in ledger.iterrows()
    }
    drift: list[dict[str, Any]] = []
    for migration_id, statements in sorted(recorded.items()):
        if migration_id not in db_by_id:
            continue  # code-only migration not applied here yet
        db_checksum, status = db_by_id[migration_id]
        code_checksum = checksum_statements(statements)
        if code_checksum != db_checksum:
            drift.append(
                {
                    "migration_id": migration_id,
                    "db_checksum": db_checksum,
                    "code_checksum": code_checksum,
                    "status": status,
                }
            )
    return drift, capture_errors


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Report migrations whose code checksum drifted from the DB.")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)

    drift, capture_errors = find_migration_drift()
    ok = not drift and not capture_errors
    if args.format == "json":
        print(json.dumps(
            {"status": "ok" if ok else "error", "drift": drift, "capture_errors": capture_errors},
            indent=2,
        ))
    else:
        if ok:
            print("status: ok - no applied migration has drifted from its recorded checksum")
        else:
            if drift:
                print(f"status: error - {len(drift)} migration(s) drifted (edited after being applied):")
                for row in drift:
                    print(f"  - {row['migration_id']}  db={row['db_checksum'][:16]} code={row['code_checksum'][:16]}")
            if capture_errors:
                print(f"status: error - {len(capture_errors)} ensure*() call(s) raised; drift for those migrations is UNKNOWN, not clean:")
                for row in capture_errors:
                    fn = f".{row['function']}" if row["function"] else ""
                    print(f"  - {row['module']}{fn}: {row['error']}")
    return 1 if not ok else 0


if __name__ == "__main__":
    raise SystemExit(main())
