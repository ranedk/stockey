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

    ``apply_schema_migration`` is temporarily rebound in each module to a recorder, then
    every zero-arg ``ensure*`` function is invoked to register its migrations. The original
    binding is always restored.
    """
    recorded: dict[str, list[str]] = {}

    def _recorder(**kwargs: Any) -> dict[str, str]:
        migration_id = kwargs.get("migration_id")
        if migration_id:
            recorded[str(migration_id)] = list(kwargs.get("statements") or [])
        return {"status": "applied"}

    for dotted in _module_dotted_paths():
        try:
            module = importlib.import_module(dotted)
        except Exception:
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
                except Exception:
                    continue
        finally:
            module.apply_schema_migration = original  # type: ignore[attr-defined]
    return recorded


def find_migration_drift() -> list[dict[str, Any]]:
    """Return applied migrations whose current code checksum differs from the DB record.

    An empty list means no drift. Each entry has ``migration_id``, ``db_checksum``,
    ``code_checksum`` and ``status``. Returns ``[]`` if the migration ledger cannot be
    read (nothing to compare against).
    """
    from utils.db import sql_to_df
    from utils.schema_migrations import checksum_statements

    recorded = capture_code_migrations()
    try:
        ledger = sql_to_df("SELECT migration_id, checksum, status FROM stockey_schema_migrations")
    except Exception:
        return []
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
    return drift


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Report migrations whose code checksum drifted from the DB.")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)

    drift = find_migration_drift()
    if args.format == "json":
        print(json.dumps({"status": "error" if drift else "ok", "drift": drift}, indent=2))
    else:
        if not drift:
            print("status: ok - no applied migration has drifted from its recorded checksum")
        else:
            print(f"status: error - {len(drift)} migration(s) drifted (edited after being applied):")
            for row in drift:
                print(f"  - {row['migration_id']}  db={row['db_checksum'][:16]} code={row['code_checksum'][:16]}")
    return 1 if drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
