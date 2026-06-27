"""Editable, versioned prompt store (UI v2 Phase 5).

The static `advisory/prompt_registry.py` is the audit metadata for every LLM prompt; the actual prompt
BODIES are hardcoded in the executors. This module adds a DB-backed, append-only VERSION store so an
operator can edit a prompt (which creates a NEW version, never an overwrite -- provenance is preserved)
and activate it. Executors resolve the active version's body via `resolve_active_prompt`, falling back
to the Python constant when no DB row exists, so wiring is safe to roll out incrementally and behavior
only changes when the LLM master flag is on.

Review-only: prompts never reach the broker. `broker_execution_allowed` stays False everywhere.
"""

from __future__ import annotations

import importlib
from typing import Any

from utils.schema_migrations import apply_schema_migration

PROMPT_VERSIONS_TABLE = "advisory_prompt_registry_versions"
SCHEMA_MIGRATION_ID = "20260627_advisory_prompt_registry_versions"
SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {PROMPT_VERSIONS_TABLE} (
        prompt_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        title TEXT,
        owner_area TEXT,
        authority_scope TEXT,
        system_prompt TEXT,
        user_prompt_template TEXT,
        response_schema_version TEXT,
        active BOOLEAN NOT NULL DEFAULT FALSE,
        created_by TEXT,
        notes TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (prompt_id, version)
    )
    """,
    f"CREATE INDEX IF NOT EXISTS idx_{PROMPT_VERSIONS_TABLE}_active ON {PROMPT_VERSIONS_TABLE} (prompt_id, active)",
]


def ensure_tables() -> None:
    apply_schema_migration(
        migration_id=SCHEMA_MIGRATION_ID,
        description="Editable, append-only prompt version store (review-only; bodies resolved by executors with Python fallback).",
        statements=SCHEMA_STATEMENTS,
        metadata={"tables": [PROMPT_VERSIONS_TABLE], "authority_scope": "prompt_review_only"},
    )


def _resolve_dotted_str(path: str | None) -> str | None:
    """Resolve a 'module.attr' dotted path to a string constant; None if it isn't a plain str."""
    if not path or "." not in path:
        return None
    module_path, _, attr = path.rpartition(".")
    try:
        module = importlib.import_module(module_path)
        value = getattr(module, attr, None)
    except Exception:
        return None
    return value if isinstance(value, str) else None


def seed_baseline() -> int:
    """Seed version 1 (active) for every registry contract whose system prompt is a static string.

    Idempotent: only inserts a prompt_id that has no rows yet. Returns rows seeded.
    """
    from advisory.prompt_registry import PROMPT_CONTRACTS
    from utils.db import db_session, execute_db_operation

    ensure_tables()
    rows = []
    for contract in PROMPT_CONTRACTS:
        system_prompt = _resolve_dotted_str(getattr(contract, "system_prompt_source", None))
        rows.append({
            "prompt_id": contract.prompt_id,
            "title": contract.title,
            "owner_area": contract.owner_area,
            "authority_scope": contract.authority_scope,
            "system_prompt": system_prompt,
            "response_schema_version": getattr(contract, "response_schema_version", None),
            "notes": "seeded from prompt_registry baseline",
        })

    def _seed() -> int:
        seeded = 0
        with db_session() as (_, cur):
            for row in rows:
                cur.execute(f"SELECT 1 FROM {PROMPT_VERSIONS_TABLE} WHERE prompt_id = %(prompt_id)s LIMIT 1", row)
                if cur.fetchone():
                    continue
                cur.execute(
                    f"""
                    INSERT INTO {PROMPT_VERSIONS_TABLE}
                        (prompt_id, version, title, owner_area, authority_scope, system_prompt,
                         response_schema_version, active, created_by, notes)
                    VALUES (%(prompt_id)s, 1, %(title)s, %(owner_area)s, %(authority_scope)s, %(system_prompt)s,
                            %(response_schema_version)s, TRUE, 'system', %(notes)s)
                    """,
                    row,
                )
                seeded += 1
        return seeded

    return execute_db_operation(_seed, operation_name="prompt_store:seed_baseline")


def load_versions(prompt_id: str | None = None) -> list[dict[str, Any]]:
    """All stored versions (optionally for one prompt_id), newest version first. Pure read."""
    from utils.db import sql_to_df

    ensure_tables()
    where = "WHERE prompt_id = %s" if prompt_id else ""
    params = (str(prompt_id),) if prompt_id else None
    df = sql_to_df(
        f"""
        SELECT prompt_id, version, title, owner_area, authority_scope, system_prompt,
               user_prompt_template, response_schema_version, active, created_by, notes, created_at
        FROM {PROMPT_VERSIONS_TABLE}
        {where}
        ORDER BY prompt_id ASC, version DESC
        """,
        params=params,
    )
    return [] if df.empty else df.to_dict(orient="records")


def create_version(*, prompt_id: str, system_prompt: str | None = None, user_prompt_template: str | None = None,
                   title: str | None = None, owner_area: str | None = None, authority_scope: str | None = None,
                   notes: str | None = None, created_by: str | None = None, activate: bool = False) -> dict[str, Any]:
    """Append a new (inactive unless activate=True) version of a prompt. Never overwrites prior versions."""
    from utils.db import db_session, execute_db_operation

    pid = str(prompt_id or "").strip()
    if not pid:
        raise ValueError("prompt_id is required")
    ensure_tables()

    def _create() -> dict[str, Any]:
        with db_session() as (_, cur):
            cur.execute(f"SELECT COALESCE(MAX(version), 0) FROM {PROMPT_VERSIONS_TABLE} WHERE prompt_id = %s", (pid,))
            next_version = int((cur.fetchone() or [0])[0]) + 1
            if activate:
                cur.execute(f"UPDATE {PROMPT_VERSIONS_TABLE} SET active = FALSE WHERE prompt_id = %s", (pid,))
            cur.execute(
                f"""
                INSERT INTO {PROMPT_VERSIONS_TABLE}
                    (prompt_id, version, title, owner_area, authority_scope, system_prompt,
                     user_prompt_template, active, created_by, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (pid, next_version, title, owner_area, authority_scope, system_prompt,
                 user_prompt_template, bool(activate), (created_by or "operator"), notes),
            )
            return {"prompt_id": pid, "version": next_version, "active": bool(activate)}

    return execute_db_operation(_create, operation_name=f"prompt_store:create:{pid}")


def activate_version(*, prompt_id: str, version: int) -> int:
    """Make one version active (deactivating the others for that prompt). Returns rows activated (0/1)."""
    from utils.db import db_session, execute_db_operation

    pid = str(prompt_id or "").strip()
    if not pid:
        raise ValueError("prompt_id is required")
    ensure_tables()

    def _activate() -> int:
        with db_session() as (_, cur):
            cur.execute(f"UPDATE {PROMPT_VERSIONS_TABLE} SET active = FALSE WHERE prompt_id = %s", (pid,))
            cur.execute(
                f"UPDATE {PROMPT_VERSIONS_TABLE} SET active = TRUE WHERE prompt_id = %s AND version = %s",
                (pid, int(version)),
            )
            return int(cur.rowcount or 0)

    return execute_db_operation(_activate, operation_name=f"prompt_store:activate:{pid}")


def resolve_active_prompt(prompt_id: str) -> dict[str, Any] | None:
    """The active version's bodies for a prompt, or None (caller falls back to its Python constant).

    Resilient by design: any DB error returns None so the executor keeps its hardcoded prompt.
    """
    try:
        from utils.db import sql_to_df

        ensure_tables()
        df = sql_to_df(
            f"""
            SELECT prompt_id, version, system_prompt, user_prompt_template
            FROM {PROMPT_VERSIONS_TABLE}
            WHERE prompt_id = %s AND active = TRUE
            ORDER BY version DESC LIMIT 1
            """,
            params=(str(prompt_id),), retries=1,
        )
        if df.empty:
            return None
        return df.iloc[0].to_dict()
    except Exception:
        return None
