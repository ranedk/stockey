from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from bs4 import BeautifulSoup

from utils.db import upsert_to_db
from utils.redaction import redact_json_text, redact_text
from utils.schema_migrations import apply_schema_migration


SOURCE_NAME = "screener.in"
FAILURES_TABLE = "screenerin_parse_failures"
SCREENER_FAILURES_SCHEMA_MIGRATION_ID = "20260611_screenerin_parse_failures_base"
SCREENER_FAILURES_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {FAILURES_TABLE} (
        failure_id TEXT PRIMARY KEY,
        observed_at TIMESTAMPTZ NOT NULL,
        source_name TEXT NOT NULL,
        failure_stage TEXT NOT NULL,
        query_name TEXT,
        query_hash TEXT,
        query_text TEXT,
        screener_url TEXT,
        final_url TEXT,
        http_status BIGINT,
        error_type TEXT NOT NULL,
        error_message TEXT,
        body_hash TEXT,
        body_excerpt TEXT,
        has_login_form BOOLEAN,
        has_query_builder BOOLEAN,
        has_page_results_container BOOLEAN,
        raw_context_json TEXT,
        load_ts TIMESTAMPTZ NOT NULL
    )
    """,
    f"ALTER TABLE {FAILURES_TABLE} ADD COLUMN IF NOT EXISTS query_name TEXT",
    f"ALTER TABLE {FAILURES_TABLE} ADD COLUMN IF NOT EXISTS query_hash TEXT",
    f"ALTER TABLE {FAILURES_TABLE} ADD COLUMN IF NOT EXISTS query_text TEXT",
    f"ALTER TABLE {FAILURES_TABLE} ADD COLUMN IF NOT EXISTS final_url TEXT",
    f"ALTER TABLE {FAILURES_TABLE} ADD COLUMN IF NOT EXISTS http_status BIGINT",
    f"ALTER TABLE {FAILURES_TABLE} ADD COLUMN IF NOT EXISTS raw_context_json TEXT",
]


def ensure_tables() -> None:
    apply_schema_migration(
        migration_id=SCREENER_FAILURES_SCHEMA_MIGRATION_ID,
        description="Create Screener.in parse/fetch failure audit table.",
        statements=SCREENER_FAILURES_SCHEMA_STATEMENTS,
        metadata={"tables": [FAILURES_TABLE], "source": SOURCE_NAME},
    )


def _clean_text(value: str | None) -> str:
    return " ".join(str(value or "").split())


def _html_context(html: str | None) -> dict[str, Any]:
    if not html:
        return {
            "body_hash": None,
            "body_excerpt": None,
            "has_login_form": None,
            "has_query_builder": None,
            "has_page_results_container": None,
        }
    soup = BeautifulSoup(html, "html.parser")
    body_text = _clean_text(soup.get_text(" ", strip=True))
    return {
        "body_hash": hashlib.sha256(html.encode("utf-8", errors="replace")).hexdigest(),
        "body_excerpt": redact_text(body_text[:500]),
        "has_login_form": bool(soup.select_one("form[action*='login'], input[name='username'], input[name='password']")),
        "has_query_builder": bool(soup.select_one("#query-builder textarea, #query-builder pre, #query-builder")),
        "has_page_results_container": bool(soup.select_one("div[data-page-results]")),
    }


def record_screener_failure(
    *,
    failure_stage: str,
    error: Exception | str,
    screener_url: str | None = None,
    final_url: str | None = None,
    query_text: str | None = None,
    query_name: str | None = None,
    html: str | None = None,
    http_status: int | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    html_context = _html_context(html)
    error_type = type(error).__name__ if isinstance(error, Exception) else "Error"
    error_message = str(error)
    query_hash = hashlib.sha256(query_text.encode("utf-8")).hexdigest() if query_text else None
    row = {
        "failure_id": str(uuid.uuid4()),
        "observed_at": now,
        "source_name": SOURCE_NAME,
        "failure_stage": str(failure_stage or "unknown"),
        "query_name": redact_text(query_name),
        "query_hash": query_hash,
        "query_text": redact_text(query_text),
        "screener_url": redact_text(screener_url),
        "final_url": redact_text(final_url),
        "http_status": http_status,
        "error_type": error_type,
        "error_message": redact_text(error_message),
        **html_context,
        "raw_context_json": redact_json_text(context or {}),
        "load_ts": now,
    }
    ensure_tables()
    upsert_to_db(pd.DataFrame([row]), FAILURES_TABLE, unique_keys=["failure_id"])
    return row
